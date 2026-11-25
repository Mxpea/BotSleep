"""Leaf litter is empty collision, never support; no game/model connection."""

import unittest

from core.client import APIError
from skills.survival import SurvivalSkills
from test_survival_skills import World


class LeafLitterRetreatTests(unittest.TestCase):
    def world(self):
        world = World()
        world.ground = "air"
        world.status["pos"] = [2.2, 64, .5]
        world.set_block([2, 63, 0], "leaf_litter",
                        properties={"facing": "north", "segment_amount": "4"})
        for x in (1, 0, -1):
            world.set_block([x, 63, 0], "dirt")
        return world

    def retreat(self, world, threat=None):
        return SurvivalSkills(world).execute("retreat_from", {"pos": threat or [6, 64, .5]})

    def test_litter_center_uses_verified_neighbor_contact_not_litter_as_ground(self):
        world = self.world()
        result = self.retreat(world)
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["edge_start"])
        self.assertEqual(result.data["origin_support"]["center_name"], "leaf_litter")
        self.assertEqual(result.data["origin_support"]["state"], "edge_entry_verified")
        self.assertEqual(result.data["origin_support"]["contacts"], [
            {"pos": [1, 63, 0], "name": "dirt", "minimum_overlap": [.0501, .6002]}])
        self.assertEqual(result.data["target"], [1.5, 64, .5])
        self.assertEqual([call[0] for call in world.writes()], ["move_to"])
        self.assertLessEqual(result.data["terrain_search"]["block_reads"], 256)
        self.assertNotIn("leaf_litter", SurvivalSkills.SUPPORT)

    def test_underlying_block_one_level_below_feet_is_not_current_support(self):
        world = self.world()
        world.set_block([1, 63, 0], "air")
        world.set_block([2, 62, 0], "dirt")
        result = self.retreat(world)
        self.assertEqual(result.error_code, "unsafe")
        self.assertEqual(result.data["origin_support"]["contacts"], [])
        self.assertFalse(world.writes())

    def test_litter_without_guaranteed_footprint_overlap_does_not_authorize_motion(self):
        world = self.world()
        world.status["pos"][0] = 2.3
        result = self.retreat(world)
        self.assertEqual(result.error_code, "unsafe")
        self.assertFalse(result.data["edge_start"])
        self.assertFalse(world.writes())

    def test_unstable_or_mismatched_height_has_no_support_authority(self):
        for change in ({"on_ground": False}, {"on_ground": None}, {"pos": [2.2, 64.1, .5]}):
            with self.subTest(change=change):
                world = self.world()
                world.status.update(change)
                result = self.retreat(world)
                self.assertEqual(result.error_code, "unsafe")
                self.assertFalse(world.writes())

    def test_unknown_hazard_or_colliding_center_is_not_an_edge(self):
        for name, properties in (("water", {}), ("lava", {}), ("powder_snow", {}),
                                 ("moss_carpet", {}), ("snow", {"layers": "1"}),
                                 ("pink_petals", {}), ("unknown_decoration", {}),
                                 ("leaf_litter", {"waterlogged": True})):
            with self.subTest(name=name):
                world = self.world()
                world.set_block([2, 63, 0], name, properties=properties)
                result = self.retreat(world)
                self.assertEqual(result.error_code, "unsafe")
                self.assertEqual(result.data["origin_support"]["state"], "unverified")
                self.assertFalse(world.writes())

    def test_missing_or_hazardous_neighbor_cannot_support_litter_edge(self):
        for name in ("leaf_litter", "water", "lava", "unknown_block"):
            with self.subTest(name=name):
                world = self.world()
                world.set_block([1, 63, 0], name)
                result = self.retreat(world)
                self.assertEqual(result.error_code, "unsafe")
                self.assertEqual(result.data["origin_support"]["contacts"], [])
                self.assertFalse(world.writes())

    def test_unknown_center_or_neighbor_read_refuses_motion(self):
        for missing in ([2, 63, 0], [1, 63, 0]):
            with self.subTest(missing=missing):
                world = self.world()
                original = world.get_block

                def read(pos, **kwargs):
                    if pos == missing:
                        raise APIError("not_loaded", "Unobserved support")
                    return original(pos, **kwargs)

                world.get_block = read
                result = self.retreat(world)
                self.assertEqual(result.error_code, "unsafe")
                self.assertFalse(world.writes())

    def test_body_clearance_and_threat_nonapproach_still_apply(self):
        world = self.world()
        world.set_block([1, 65, 0], "oak_leaves")
        self.assertEqual(self.retreat(world).error_code, "unsafe")
        self.assertFalse(world.writes())
        world = self.world()
        result = self.retreat(world, [0, 64, .5])
        self.assertEqual(result.error_code, "unsafe")
        self.assertEqual(result.data["origin_support"]["entry_rejections"]["threat_proximity"], 1)
        self.assertFalse(world.writes())

    def test_latest_status_must_still_be_grounded_at_verified_height(self):
        for change in ({"on_ground": False}, {"pos": [2.2, 64.1, .5]}):
            with self.subTest(change=change):
                world = self.world()
                original = world.get_status
                reads = 0

                def status(**kwargs):
                    nonlocal reads
                    reads += 1
                    if reads == 4:
                        world.status.update(change)
                    return original(**kwargs)

                world.get_status = status
                result = self.retreat(world)
                self.assertEqual(result.error_code, "unsafe")
                self.assertFalse(world.writes())

    def test_changed_center_or_contact_is_reobserved_before_movement(self):
        for changed in ([2, 63, 0], [1, 63, 0]):
            with self.subTest(changed=changed):
                world = self.world()
                original = world.get_block
                reads = 0

                def read(pos, **kwargs):
                    nonlocal reads
                    if pos == changed:
                        reads += 1
                        if reads == 2:
                            world.set_block(changed, "lava")
                    return original(pos, **kwargs)

                world.get_block = read
                result = self.retreat(world)
                self.assertEqual(result.error_code, "unsafe")
                self.assertIn("changed before movement", result.message)
                self.assertFalse(world.writes())


if __name__ == "__main__":
    unittest.main()
