"""Offline sight rays against small voxel fixtures; no game or model calls."""

import unittest

from core.block_geometry import _ray_cells, trace_block_visibility
from core.client import APIError


class BlockGeometryTests(unittest.TestCase):
    def reader(self, blocks=None):
        blocks = blocks or {}
        reads = []

        def get_block(pos):
            reads.append(tuple(pos))
            return {"name": blocks.get(tuple(pos), "air")}

        return get_block, reads

    def test_near_visible_target_does_not_query_or_treat_target_as_an_obstacle(self):
        reader, reads = self.reader({(3, 65, 0): "oak_log"})
        result = trace_block_visibility([0.5, 64, 0.5], [3, 65, 0], reader)
        self.assertIs(result["visible"], True)
        self.assertNotIn((3, 65, 0), reads)
        self.assertEqual(result["queries"], len(reads))

    def test_dirt_wall_blocks_the_real_high_log_fixture(self):
        wall = {(5, y, z): "dirt" for y in range(109, 114) for z in range(-7, -2)}
        reader, reads = self.reader(wall)
        result = trace_block_visibility([4.3, 109, -5.5], [6, 112, -4], reader)
        self.assertEqual(result["state"], "occluded")
        self.assertIs(result["visible"], False)
        self.assertEqual(result["blocker"][0], 5)
        self.assertEqual(len(reads), len(set(reads)))

    def test_same_high_log_is_visible_when_the_wall_is_absent(self):
        reader, _ = self.reader()
        self.assertIs(trace_block_visibility([4.3, 109, -5.5], [6, 112, -4], reader)["visible"], True)

    def test_visible_facing_surface_can_succeed_when_center_ray_is_blocked(self):
        reader, reads = self.reader({(2, 65, 2): "dirt"})
        result = trace_block_visibility([0.5, 64, 0.5], [3, 65, 2], reader)
        self.assertIn((2, 65, 2), reads)  # Center ray first reaches this obstacle.
        self.assertIs(result["visible"], True)  # The nearer Z face remains visible.
        self.assertEqual(len(reads), len(set(reads)))

    def test_leaf_litter_and_short_grass_pass_but_unknown_shapes_are_opaque(self):
        for name, visible in (("leaf_litter", True), ("short_grass", True),
                              ("oak_leaves", False), ("oak_stairs", False), ("future_shape", False)):
            with self.subTest(name=name):
                reader, _ = self.reader({(1, 65, 0): name})
                self.assertIs(trace_block_visibility([0.5, 64, 0.5], [3, 65, 0], reader)["visible"], visible)

    def test_diagonal_corner_contacts_cover_side_cells_not_only_diagonal_cell(self):
        cells = list(_ray_cells((0.5, 65.5, 0.5), (2.5, 65.5, 2.5)))
        self.assertTrue({(1, 65, 0), (0, 65, 1), (1, 65, 1)} <= set(cells))
        reader, _ = self.reader({(1, 65, 0): "dirt", (0, 65, 1): "dirt"})
        self.assertIs(trace_block_visibility([0.5, 63.88, 0.5], [2, 65, 2], reader)["visible"], False)

    def test_negative_grid_boundary_terminates_and_reaches_target(self):
        reader, reads = self.reader()
        result = trace_block_visibility([0, 64, 0], [-3, 65, -1], reader)
        self.assertIs(result["visible"], True)
        self.assertLessEqual(len(reads), 10)
        self.assertNotIn((-3, 65, -1), reads)

    def test_eye_inside_target_is_visible_without_a_read(self):
        reader, reads = self.reader()
        result = trace_block_visibility([1.5, 64, 0.5], [1, 65, 0], reader)
        self.assertIs(result["visible"], True)
        self.assertEqual(reads, [])

    def test_shared_cache_is_reused_without_exceeding_new_read_budget(self):
        reader, reads = self.reader()
        cache = {}
        first = trace_block_visibility([0.5, 64, 0.5], [3, 65, 0], reader, cache=cache)
        second = trace_block_visibility([0.5, 64, 0.5], [3, 65, 0], reader, cache=cache, max_queries=0)
        self.assertIs(first["visible"], True)
        self.assertIs(second["visible"], True)
        self.assertEqual(second["queries"], 0)
        self.assertEqual(len(reads), len(set(reads)))

    def test_budget_and_far_distance_are_unknown_never_visible(self):
        reader, reads = self.reader()
        result = trace_block_visibility([0.5, 64, 0.5], [7, 65, 0], reader, max_queries=2)
        self.assertEqual((result["state"], result["reason"]), ("unknown", "query_budget_exhausted"))
        self.assertIsNone(result["visible"])
        self.assertEqual(len(reads), 2)
        reader, reads = self.reader()
        result = trace_block_visibility([0.5, 64, 0.5], [20, 65, 0], reader)
        self.assertEqual(result["reason"], "out_of_range")
        self.assertIsNone(result["visible"])
        self.assertEqual(reads, [])

    def test_unloaded_or_malformed_blocks_are_unknown(self):
        for response in (None, {}, {"name": None}):
            with self.subTest(response=response):
                result = trace_block_visibility([0.5, 64, 0.5], [3, 65, 0], lambda _: response)
                self.assertEqual(result["state"], "unknown")

        def unloaded(_):
            raise APIError("not_loaded", "offline unloaded fixture")

        self.assertEqual(trace_block_visibility([0.5, 64, 0.5], [3, 65, 0], unloaded)["state"], "unknown")

    def test_callback_cancellation_and_network_errors_propagate(self):
        for code in ("cancelled", "network_error"):
            def fail(_):
                raise APIError(code, "offline fixture")

            with self.assertRaises(APIError) as caught:
                trace_block_visibility([0.5, 64, 0.5], [3, 65, 0], fail)
            self.assertEqual(caught.exception.code, code)

    def test_invalid_positions_and_limits_are_rejected(self):
        reader, _ = self.reader()
        for origin, target, kwargs in (([0, float("nan"), 0], [1, 64, 0], {}),
                                       ([0, 64, 0], [1.5, 64, 0], {}),
                                       ([0, 64, 0], [1, 64, 0], {"max_queries": -1}),
                                       ([0, 64, 0], [1, 64, 0], {"max_distance": float("inf")})):
            with self.subTest(origin=origin, target=target, kwargs=kwargs), self.assertRaises(ValueError):
                trace_block_visibility(origin, target, reader, **kwargs)


if __name__ == "__main__":
    unittest.main()
