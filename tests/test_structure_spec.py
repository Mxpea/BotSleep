"""Offline unit tests for core.structure_spec."""

import math
import unittest

from core.structure_spec import (
    CARDINAL_ORIENTATIONS,
    NETHER_PORTAL_SPEC,
    inspect_structure,
    normalize_orientation,
    rotate_facing,
    transform_rel_pos,
    transform_rel_vec,
)


class StructureSpecTests(unittest.TestCase):
    def test_orientation_aliases_and_validation(self):
        self.assertEqual(normalize_orientation("x"), "south")
        self.assertEqual(normalize_orientation("z"), "east")
        self.assertEqual(normalize_orientation("NORTH"), "north")
        with self.assertRaises(ValueError):
            normalize_orientation("up")

    def test_four_orientation_rotations_preserve_voxel_cell_consistency(self):
        origin = [100, 64, -20]
        stances = [anchor.rel_pos for anchor in NETHER_PORTAL_SPEC.anchors.values()]
        for orientation in CARDINAL_ORIENTATIONS:
            for sx, sy, sz in stances:
                world_float = transform_rel_pos((sx, sy, sz), origin, orientation, integer=False)
                expected_cell = transform_rel_pos(
                    (math.floor(sx), math.floor(sy), math.floor(sz)),
                    origin,
                    orientation,
                    integer=True,
                )
                actual_cell = [math.floor(v + 0.0001) for v in world_float]
                self.assertEqual(
                    actual_cell,
                    expected_cell,
                    f"Float stance {(sx, sy, sz)} drifted out of rotated block cell under {orientation}",
                )

    def test_vector_and_facing_rotations_across_all_cardinals(self):
        self.assertEqual(transform_rel_vec((-1, 0, 0), "south"), [-1, 0, 0])
        self.assertEqual(transform_rel_vec((-1, 0, 0), "west"), [0, 0, -1])
        self.assertEqual(transform_rel_vec((-1, 0, 0), "north"), [1, 0, 0])
        self.assertEqual(transform_rel_vec((-1, 0, 0), "east"), [0, 0, 1])

        self.assertEqual(rotate_facing("north", "south"), "north")
        self.assertEqual(rotate_facing("north", "west"), "east")
        self.assertEqual(rotate_facing("north", "north"), "south")
        self.assertEqual(rotate_facing("north", "east"), "west")

    def test_inspect_nether_portal_reports_complete_missing_conflicts_and_hazards(self):
        origin = [10, 64, 20]
        orientation = "west"
        blocks = {}

        # Populate a complete portal without scaffold, except one conflict & one hazard
        for rule in NETHER_PORTAL_SPEC.cells:
            wp = tuple(transform_rel_pos(rule.rel_pos, origin, orientation, integer=True))
            if rule.stage in {"base", "frame"}:
                blocks[wp] = {"name": "obsidian"}
            elif rule.stage == "interior":
                blocks[wp] = {"name": "nether_portal"}

        def reader(pos):
            return {"ok": True, "result": blocks.get(tuple(pos), {"name": "air"})}

        report_ok = inspect_structure(
            NETHER_PORTAL_SPEC,
            origin,
            reader,
            orientation=orientation,
            stages=("base", "frame", "interior"),
        )
        self.assertTrue(report_ok["complete"])
        self.assertEqual(report_ok["present_count"], 16)
        self.assertEqual(report_ok["total_count"], 16)
        self.assertEqual(report_ok["missing"], [])

        # Introduce one missing frame cell blocked by stone and one interior cell with lava
        top_right = tuple(transform_rel_pos((2, 3, 0), origin, orientation, integer=True))
        inner_cell = tuple(transform_rel_pos((1, 0, 0), origin, orientation, integer=True))
        blocks[top_right] = {"name": "stone"}
        blocks[inner_cell] = {"name": "lava"}

        report_bad = inspect_structure(
            NETHER_PORTAL_SPEC,
            origin,
            reader,
            orientation=orientation,
            stages=("base", "frame", "interior"),
        )
        self.assertFalse(report_bad["complete"])
        self.assertEqual(report_bad["present_count"], 14)
        self.assertEqual(len(report_bad["missing"]), 2)
        self.assertEqual(len(report_bad["conflicts"]), 1)
        self.assertEqual(report_bad["conflicts"][0]["world_pos"], list(top_right))
        self.assertEqual(len(report_bad["hazards"]), 1)
        self.assertEqual(report_bad["hazards"][0]["world_pos"], list(inner_cell))


if __name__ == "__main__":
    unittest.main()
