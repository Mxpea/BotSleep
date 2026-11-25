"""Rounded wall contact from the 2026-11-18 scene, using only saved local cells."""

import copy
import threading
import unittest

from core.client import APIError
from skills.positioning import PositioningSkills
from test_daily_skills import DailyWorld, envelope


class WallScene(DailyWorld):
    """Twelve cells read from live-recovery-scene-20261118T060138Z.json.

    Unrecorded coordinates remain unknown; no synthetic empty route or floor.
    The fake move is the sole write, and tests independently change its outcome.
    """

    ORIGIN = [-6.5, 67, -3.3]
    TARGET = [-7.5, 67, -3.5]

    def __init__(self):
        super().__init__()
        self.status.update(pos=list(self.ORIGIN), speed=0, velocity=[0, 0, 0],
                           is_day=True, time_of_day=10612, jumping=False, held_controls=None)
        for x, z, names in (
            (-8, -4, ("grass_block", "short_grass", "air")),
            (-8, -3, ("grass_block", "air", "air")),
            (-7, -4, ("grass_block", "air", "air")),
            (-7, -3, ("dirt", "dirt", "oak_planks")),
        ):
            for y, name in zip((66, 67, 68), names):
                self.block([x, y, z], name, properties={"snowy": False} if name == "grass_block" else {})

    def get_block(self, pos, **kwargs):
        if tuple(pos) not in self.blocks:
            raise APIError("not_loaded", "Outside captured wall fragment")
        return super().get_block(pos, **kwargs)

    def get_entities(self, **kwargs):
        rows = super().get_entities(**kwargs)
        return [row for row in rows if row.get("type") in kwargs["types"]] if "types" in kwargs else rows


class WallMarginPositioningTests(unittest.TestCase):
    def run_move(self, world, target=None, cancel=None):
        return PositioningSkills(world).execute("position_for_task", {"pos": target or world.TARGET}, cancel)

    def writes(self, world):
        return [row for row in world.calls if row[0] in {"move", "stop", "dig", "place", "control"}]

    def test_captured_wall_contact_can_leave_westward_with_one_verified_move(self):
        world = WallScene()
        before = copy.deepcopy(world.blocks)
        result = self.run_move(world)
        self.assertTrue(result.success, result)
        self.assertEqual(self.writes(world), [("move", world.TARGET, {"range": 0, "timeout_ms": 8000})])
        self.assertTrue(result.data["start_margin_recovery"])
        self.assertEqual(sorted(result.data["start_margin_cells"]), [[-7, 67, -3], [-7, 68, -3]])
        self.assertTrue(result.data["landing_stable"])
        self.assertFalse(result.data["already_positioned"])
        self.assertFalse(result.data["actual_path_verified"])
        self.assertLessEqual(result.data["read_calls"], 96)
        self.assertEqual(world.blocks, before)

    def test_same_cell_near_centre_and_exact_point36_contact_are_not_already_safe(self):
        for gap in (.3, .36):
            with self.subTest(gap=gap):
                world = WallScene()
                world.status["pos"][2] = -3 - gap
                result = self.run_move(world, [-6.5, 67, -3.5])
                self.assertEqual(result.error_code, "unsafe_route", result)
                self.assertFalse(result.data.get("already_positioned"))
                self.assertFalse(self.writes(world))

    def test_pure_geometry_requires_outward_motion_and_a_distinct_clear_neighbour(self):
        origin, target = WallScene.ORIGIN, WallScene.TARGET
        for turns in range(4):
            def rotate(pos):
                x, y, z = pos
                for _ in range(turns):
                    x, z = -z, x
                return [x, y, z]
            a, b = rotate(origin), rotate(target)
            centre = rotate([-6.5, 67, -2.5])
            column = (int(centre[0] // 1), int(centre[2] // 1))
            self.assertTrue(PositioningSkills.leaves_start_margin(a, b, column))
        for target, column in (([-7.5, 67, -3.5], (-8, -3)),
                               ([-7.5, 67, -3.3], (-7, -3)),
                               ([-6.5, 67, -2.5], (-7, -3)),
                               ([-6.5, 67, -3.5], (-7, -3)),
                               ([-7.5, 68, -3.5], (-7, -3)),
                               ([-8.5, 67, -3.5], (-7, -3))):
            self.assertFalse(PositioningSkills.leaves_start_margin(origin, target, column))

    def test_measured_body_penetration_and_new_swept_wall_are_not_margin_contacts(self):
        for change in ("penetration", "new_wall", "unknown", "missing_floor", "leaf_floor"):
            with self.subTest(change=change):
                world = WallScene()
                if change == "penetration":
                    world.status["pos"][2] = -3.29
                elif change == "new_wall":
                    world.block([-8, 67, -3], "stone")
                elif change == "unknown":
                    world.blocks.pop((-8, 68, -4))
                elif change == "missing_floor":
                    world.block([-8, 66, -4], "air")
                else:
                    world.block([-8, 66, -4], "oak_leaves")
                self.assertFalse(self.run_move(world).success)
                self.assertFalse(self.writes(world))

    def test_margin_exception_never_applies_to_partial_unknown_or_wet_shapes(self):
        for name, props in (("oak_door", {"open": True}), ("stone_slab", {"type": "bottom"}),
                            ("oak_leaves", {}), ("dirt", {"waterlogged": True})):
            world = WallScene()
            world.block([-7, 67, -3], name, properties=props)
            result = self.run_move(world)
            self.assertFalse(result.success, result)
            self.assertFalse(self.writes(world))

    def test_margin_recovery_requires_stable_motion_fields_and_complete_enemy_scan(self):
        for change in ("speed", "velocity", "busy", "jumping", "unknown_speed", "skeleton", "saturated"):
            with self.subTest(change=change):
                world = WallScene()
                if change == "speed": world.status["speed"] = .2
                elif change == "velocity": world.status["velocity"] = [.2, 0, 0]
                elif change == "busy": world.status["busy"] = {"action": "moving"}
                elif change == "jumping": world.status["jumping"] = True
                elif change == "unknown_speed": world.status.pop("speed")
                elif change == "skeleton":
                    world.entities = [{"id": 1, "type": "hostile", "name": "skeleton", "pos": [5, 67, -3]}]
                else:
                    world.entities = [{"id": i, "type": "hostile", "name": "zombie", "pos": [20, 67, 20]} for i in range(40)]
                result = self.run_move(world)
                self.assertFalse(result.success, result)
                self.assertFalse(self.writes(world))

    def test_last_geometry_and_source_change_before_send_prevent_movement(self):
        for change in ("wall", "source"):
            world = WallScene()
            get_block = world.get_block
            reads = 0
            def block(pos, **kwargs):
                nonlocal reads
                if pos == [-8, 67, -4]:
                    reads += 1
                    if reads == 3:
                        if change == "wall": world.block(pos, "stone")
                        else: world.status["pos"][0] -= .2
                return get_block(pos, **kwargs)
            world.get_block = block
            result = self.run_move(world)
            self.assertFalse(result.success, result)
            self.assertFalse(self.writes(world))

    def test_ack_or_point36_final_contact_remains_unknown_without_cleanup_or_replay(self):
        for change in ("unchanged", "point36", "unknown_geometry", "transport", "death", "cancel"):
            with self.subTest(change=change):
                world = WallScene()
                cancel = threading.Event()
                def after_send():
                    if change == "unchanged": world.status["pos"] = list(world.ORIGIN)
                    elif change == "point36":
                        world.status["pos"] = [-7.5, 67, -3.36]
                        world.block([-8, 67, -3], "stone")
                    elif change == "unknown_geometry": world.blocks.pop((-8, 68, -4))
                    elif change == "transport": raise APIError("request_timeout", "Outcome unknown")
                    elif change == "death": world.status.update(alive=False, health=0)
                    else: cancel.set()
                    return envelope({"pos": world.TARGET})
                world.effects["move"] = after_send
                result = self.run_move(world, cancel=cancel)
                self.assertFalse(result.success, result)
                self.assertEqual(result.data["action_state"], "unknown")
                self.assertEqual(len(self.writes(world)), 1)
                self.assertEqual(self.writes(world)[0][0], "move")


if __name__ == "__main__":
    unittest.main()
