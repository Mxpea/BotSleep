"""Real canopy geometry and skill regressions with a local, mutable world."""

import copy
import math
import threading
import unittest
from unittest.mock import patch

from core.client import APIError
from skills.canopy import CanopyRecoverySkill


def envelope(result):
    return {"ok": True, "result": copy.deepcopy(result)}


def leaf(name="birch_leaves"):
    return {"name": name, "properties": {"waterlogged": False},
            "diggable": True, "can_see": True}


class CanopyWorld:
    """Explicit canopy cells; all unspecified cells are known air."""

    def __init__(self, drop=1, *, cut=True, support="dirt", relay=False):
        self.status = {"ready": True, "alive": True, "busy": None, "health": 20,
                       "food": 20, "dimension": "overworld", "gamemode": "survival",
                       "pos": [.5, 82, .5], "on_ground": True, "thundering": False,
                       "in_water": False, "in_lava": False, "on_fire": False,
                       "nearest_hostile": None, "last_death": None}
        self.blocks = {(0, 81, 0): leaf()}
        self.target = [1.5, 82 - drop, .5]
        self.clear = [1, 81, 0] if cut else None
        self.blocks[(1, 81 - drop, 0)] = leaf(support) if support.endswith("_leaves") else {"name": support}
        if cut:
            self.blocks[tuple(self.clear)] = leaf()
        if relay:
            self.blocks[(1, 81, 0)] = leaf("oak_leaves")
            self.blocks[(2, 80, 0)] = {"name": "dirt"}
            self.target = [1.5, 82, .5]
            self.clear = None
        self.entities = []
        self.calls = []
        self.after_dig = self.after_move = self.on_status = self.on_block = self.on_entities = None
        self.dig_error = self.move_error = None
        self.dig_result = {"dug": "birch_leaves"}
        self.move_result = {"moved": True}
        self.remove_leaf = True
        self.moved = False
        self.frames = []
        self.landing_reads = 0
        self.entity_reads = 0

    def args(self):
        return {"pos": list(self.target), "clear_pos": self.clear,
                "expected_name": "birch_leaves" if self.clear is not None else None}

    def get_status(self, *, timeout_seconds=None):
        self.calls.append(("status", timeout_seconds))
        if self.moved:
            if self.frames:
                self.status.update(copy.deepcopy(self.frames[min(self.landing_reads, len(self.frames) - 1)]))
            self.landing_reads += 1
        if self.on_status:
            self.on_status(self)
        return envelope(self.status)

    def get_block(self, pos, *, timeout_seconds=None):
        key = tuple(pos)
        self.calls.append(("block", key, timeout_seconds))
        if self.on_block:
            self.on_block(self, key)
        return envelope(self.blocks.get(key, {"name": "air", "properties": {}}))

    def get_entities(self, radius=16, types=None, *, timeout_seconds=None):
        self.calls.append(("entities", radius, types, timeout_seconds))
        self.entity_reads += 1
        if self.on_entities:
            self.on_entities(self)
        return copy.deepcopy(self.entities)

    def dig(self, pos, collect=True):
        self.calls.append(("dig", list(pos), collect))
        if self.remove_leaf:
            self.blocks[tuple(pos)] = {"name": "air"}
        if self.after_dig:
            self.after_dig(self)
        if self.dig_error:
            raise self.dig_error
        return envelope(self.dig_result)

    def move_to(self, x, y, z, **kwargs):
        self.calls.append(("move_to", [x, y, z], kwargs))
        self.moved = True
        self.status["pos"] = [x, y, z]
        if self.after_move:
            self.after_move(self)
        if self.move_error:
            raise self.move_error
        return envelope(self.move_result)

    def stop(self):
        self.calls.append(("stop",))
        return envelope({"stopped": True})

    def writes(self, name=None):
        return [call for call in self.calls if call[0] in {"dig", "move_to"}
                and (name is None or call[0] == name)]


class CanopySkillTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        for target, replacement in (("monotonic", lambda: self.now), ("sleep", self.advance)):
            mocked = patch("skills.canopy.time." + target, replacement)
            mocked.start()
            self.addCleanup(mocked.stop)

    def advance(self, seconds):
        self.now += seconds

    def run_skill(self, world, args=None, cancel=None):
        return CanopyRecoverySkill(world).execute("descend_canopy", world.args() if args is None else args, cancel)

    def assert_unknown(self, result, code):
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, code, result.message)
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertTrue(result.data["executed"])
        self.assertFalse(result.data["retreat_complete"])

    def test_cut_one_leaf_then_drop_one_or_two(self):
        for drop in (1, 2):
            with self.subTest(drop=drop):
                world = CanopyWorld(drop)
                result = self.run_skill(world)
                self.assertTrue(result.success, (result.error_code, result.message))
                self.assertEqual(world.writes(), [("dig", [1, 81, 0], False),
                    ("move_to", world.target, {"range": 0, "timeout_ms": 5000})])
                self.assertTrue(result.data["block_removed"])
                self.assertEqual(result.data["target"], world.target)
                self.assertEqual(result.data["support"], "dirt")
                self.assertAlmostEqual(result.data["moved"], math.sqrt(1 + drop * drop), places=3)
                self.assertTrue(result.data["retreat_complete"])
                self.assertEqual(result.data["evacuation_stage"], "ground")
                self.assertEqual(result.data["action_state"], "verified")
                self.assertEqual(world.blocks[(0, 81, 0)]["name"], "birch_leaves")

    def test_pure_descent_never_digs(self):
        world = CanopyWorld(2, cut=False)
        result = self.run_skill(world)
        self.assertTrue(result.success, result.message)
        self.assertEqual(len(world.writes()), 1)
        self.assertFalse(result.data["block_removed"])

    def test_horizontal_relay_uses_actual_support(self):
        world = CanopyWorld(cut=False, relay=True)
        result = self.run_skill(world)
        self.assertTrue(result.success, result.message)
        self.assertEqual(world.writes(), [("move_to", [1.5, 82, .5], {"range": 0, "timeout_ms": 5000})])
        self.assertEqual(result.data["support"], "oak_leaves")
        self.assertFalse(result.data["retreat_complete"])
        self.assertEqual(result.data["evacuation_stage"], "relay")
        self.assertEqual(result.data["route_end"], [2.5, 81, .5])

    def test_leaf_landing_is_relay(self):
        result = self.run_skill(CanopyWorld(support="oak_leaves"))
        self.assertTrue(result.success, result.message)
        self.assertFalse(result.data["retreat_complete"])
        self.assertEqual(result.data["evacuation_stage"], "relay")

    def test_target_mismatch_no_plan_and_own_support_never_write(self):
        for change in ({"pos": [2.5, 81, .5]}, {"expected_name": "oak_leaves"},
                       {"clear_pos": [0, 81, 0]}, {"clear_pos": None, "expected_name": None}):
            with self.subTest(change=change):
                world = CanopyWorld()
                result = self.run_skill(world, {**world.args(), **change})
                self.assertFalse(result.success)
                self.assertIn(result.error_code, {"no_plan", "target_changed"})
                self.assertEqual(world.writes(), [])
        world = CanopyWorld()
        world.blocks[(1, 80, 0)] = {"name": "air"}
        self.assertEqual(self.run_skill(world).error_code, "no_plan")
        self.assertEqual(world.writes(), [])

    def test_no_wall_jump_large_drop_or_unproven_relay(self):
        for kind in ("wall", "drop3", "jump", "unproven_relay", "off_center"):
            with self.subTest(kind=kind):
                world = CanopyWorld()
                if kind == "wall":
                    world.blocks[(1, 82, 0)] = {"name": "stone"}
                elif kind == "drop3":
                    world = CanopyWorld(3)
                elif kind == "jump":
                    world.target[1] = 83
                elif kind == "unproven_relay":
                    world = CanopyWorld(cut=False, relay=True)
                    world.blocks[(2, 80, 0)] = {"name": "air"}
                else:
                    world.status["pos"][0] = .9
                self.assertFalse(self.run_skill(world).success)
                self.assertEqual(world.writes(), [])

    def test_lava_and_waterlogged_terrain_never_write(self):
        for pos, block in (((1, 80, 1), {"name": "lava"}), ((1, 81, 1), {"name": "lava"}),
                           ((1, 80, 0), {**leaf(), "properties": {"waterlogged": True}}),
                           ((1, 81, 0), {**leaf(), "properties": {"waterlogged": "true"}}),
                           ((0, 81, 0), {**leaf(), "properties": {}})):
            with self.subTest(pos=pos, block=block):
                world = CanopyWorld()
                world.blocks[pos] = block
                self.assertFalse(self.run_skill(world).success)
                self.assertEqual(world.writes(), [])

    def test_status_safety_gates(self):
        for change in ({"ready": False}, {"alive": False}, {"busy": "dig"}, {"health": 11},
                       {"food": 7}, {"dimension": "the_nether"}, {"gamemode": "creative"},
                       {"thundering": True}, {"in_water": True}, {"in_lava": True},
                       {"on_fire": True}, {"submerged": True}, {"on_ground": False},
                       {"is_sleeping": True}, {"in_vehicle": True}):
            with self.subTest(change=change):
                world = CanopyWorld()
                world.status.update(change)
                self.assertFalse(self.run_skill(world).success)
                self.assertEqual(world.writes(), [])

    def test_nearest_and_entity_threats_both_block(self):
        for source in ("nearest", "entities"):
            with self.subTest(source=source):
                world = CanopyWorld()
                if source == "nearest":
                    world.status["nearest_hostile"] = {"dist": 12}
                else:
                    world.entities = [{"type": "hostile", "pos": [12.5, 82, .5]}]
                self.assertEqual(self.run_skill(world).error_code, "unsafe")
                self.assertEqual(world.writes(), [])

    def test_source_thirteen_target_eleven_threat_prevents_all_writes(self):
        world = CanopyWorld(2)
        enemy = [1.5, 69, .5]
        self.assertGreater(math.dist(world.status["pos"], enemy), 13)
        self.assertEqual(math.dist(world.target, enemy), 11)
        world.status["nearest_hostile"] = {"dist": math.dist(world.status["pos"], enemy)}
        world.entities = [{"type": "hostile", "pos": enemy}]
        self.assertEqual(self.run_skill(world).error_code, "unsafe")
        self.assertEqual(world.writes(), [])
        self.assertTrue(all(call[1] == 16 for call in world.calls if call[0] == "entities"))

    def test_far_hostile_does_not_impose_old_threat_direction(self):
        world = CanopyWorld()
        world.status["nearest_hostile"] = {"dist": 15.9}
        world.entities = [{"type": "hostile", "pos": [16.4, 82, .5]}]
        result = self.run_skill(world)
        self.assertTrue(result.success, result.message)

    def test_fresh_entities_before_dig_block_without_write(self):
        world = CanopyWorld()
        def enter(world):
            if world.entity_reads == 2:
                world.entities = [{"type": "hostile", "pos": [2.5, 82, .5]}]
        world.on_entities = enter
        self.assertEqual(self.run_skill(world).error_code, "unsafe")
        self.assertEqual(world.writes(), [])

    def test_support_and_support_layer_hazards_change_before_dig(self):
        for key, block in (((1, 80, 0), {"name": "air"}), ((1, 80, 0), {"name": "stone"}),
                           ((0, 81, 0), {"name": "air"}), ((1, 80, 1), {"name": "lava"}),
                           ((1, 81, 0), leaf("oak_leaves"))):
            with self.subTest(key=key, block=block):
                world = CanopyWorld()
                def change(world):
                    if world.entity_reads == 2:
                        world.blocks[key] = block
                world.on_entities = change
                self.assertFalse(self.run_skill(world).success)
                self.assertEqual(world.writes(), [])

    def test_terrain_mutation_after_helper_is_caught_by_local_terrain(self):
        # Run the real helper, then mutate before the independent skill check.
        from core.canopy_geometry import plan_canopy_step
        world = CanopyWorld()
        def plan_then_lava(*args, **kwargs):
            plan = plan_canopy_step(*args, **kwargs)
            world.blocks[(1, 80, 1)] = {"name": "lava"}
            return plan
        with patch("skills.canopy.plan_canopy_step", side_effect=plan_then_lava):
            result = self.run_skill(world)
        self.assertEqual(result.error_code, "unsafe")
        self.assertEqual(world.writes(), [])

    def test_last_status_before_write_rejects_health_life_and_position_changes(self):
        for change in ({"health": 19}, {"last_death": "new"}, {"pos": [.6, 82, .5]}):
            with self.subTest(change=change):
                world = CanopyWorld()
                def update(world):
                    if sum(call[0] == "status" for call in world.calls) == 4:
                        world.status.update(change)
                world.on_status = update
                self.assertFalse(self.run_skill(world).success)
                self.assertEqual(world.writes(), [])

    def test_dig_timeout_leaf_remaining_and_interruption_never_move(self):
        for kind in ("timeout", "unconfirmed", "leaf_remains", "interrupted"):
            with self.subTest(kind=kind):
                world = CanopyWorld()
                if kind == "timeout":
                    world.dig_error = APIError("request_timeout", "unknown outcome")
                elif kind == "unconfirmed":
                    world.dig_error = APIError("dig_unconfirmed", "unknown outcome")
                elif kind == "leaf_remains":
                    world.remove_leaf = False
                else:
                    world.dig_result = {"interrupted_by": "hurt"}
                self.assert_unknown(self.run_skill(world), "dig_unconfirmed")
                self.assertEqual(len(world.writes("dig")), 1)
                self.assertEqual(world.writes("move_to"), [])

    def test_unknown_post_dig_read_never_moves(self):
        world = CanopyWorld()
        def unreadable(world, key):
            if world.writes("dig") and key == (1, 81, 0):
                raise APIError("not_loaded", "unknown block")
        world.on_block = unreadable
        self.assert_unknown(self.run_skill(world), "dig_unconfirmed")
        self.assertEqual(world.writes("move_to"), [])

    def test_post_dig_damage_cancel_respawn_and_airborne_never_move(self):
        for change in ({"health": 19}, {"last_death": "respawned"}, {"alive": False},
                       {"on_ground": False}, {"nearest_hostile": {"dist": 4}}, {"pos": [.7, 82, .5]}):
            with self.subTest(change=change):
                world = CanopyWorld()
                world.after_dig = lambda world: world.status.update(change)
                self.assert_unknown(self.run_skill(world), "dig_unconfirmed")
                self.assertEqual(world.writes("move_to"), [])
        world = CanopyWorld()
        cancel = threading.Event()
        world.after_dig = lambda world: cancel.set()
        self.assert_unknown(self.run_skill(world, cancel=cancel), "dig_unconfirmed")
        self.assertEqual(world.writes("move_to"), [])

    def test_entities_and_support_change_immediately_before_move(self):
        for kind in ("enemy", "support", "lava"):
            with self.subTest(kind=kind):
                world = CanopyWorld()
                def change(world):
                    if world.entity_reads == 4:
                        if kind == "enemy":
                            world.entities = [{"type": "hostile", "pos": [2, 82, .5]}]
                        else:
                            world.blocks[(1, 80, 0) if kind == "support" else (1, 80, 1)] = {"name": "air" if kind == "support" else "lava"}
                world.on_entities = change
                self.assert_unknown(self.run_skill(world), "dig_unconfirmed")
                self.assertEqual(world.writes("move_to"), [])

    def test_move_timeout_or_cancel_never_retries(self):
        for kind in ("timeout", "cancel", "interrupted"):
            with self.subTest(kind=kind):
                world = CanopyWorld()
                cancel = threading.Event()
                if kind == "timeout":
                    world.move_error = APIError("request_timeout", "unknown move")
                elif kind == "cancel":
                    world.after_move = lambda world: cancel.set()
                else:
                    world.move_result = {"interrupted_by": "hurt"}
                self.assert_unknown(self.run_skill(world, cancel=cancel), "action_unconfirmed")
                self.assertEqual(len(world.writes("move_to")), 1)

    def test_floating_never_succeeds_and_polling_is_bounded(self):
        world = CanopyWorld(cut=False)
        world.frames = [{"pos": [1.5, 81.2, .5], "on_ground": False}]
        self.assert_unknown(self.run_skill(world), "action_unconfirmed")
        self.assertEqual(world.landing_reads, 6)
        self.assertLessEqual(self.now, 1.25)
        self.assertEqual(len(world.writes("move_to")), 1)

    def test_airborne_then_landing_with_one_move(self):
        world = CanopyWorld(2, cut=False)
        world.frames = [{"pos": [1.5, 81.5, .5], "on_ground": False},
                        {"pos": [1.5, 80, .5], "on_ground": True}]
        result = self.run_skill(world)
        self.assertTrue(result.success, result.message)
        self.assertLessEqual(world.landing_reads, 6)
        self.assertEqual(len(world.writes("move_to")), 1)

    def test_landing_rounding_per_axis_is_safe(self):
        for point in ([1.4, 81, .6], [1.625, 81, .625], [1.5, 81.05, .5]):
            with self.subTest(point=point):
                world = CanopyWorld()
                world.after_move = lambda world: world.status.update(pos=point)
                result = self.run_skill(world)
                self.assertTrue(result.success, (result.error_code, result.message))
                self.assertEqual(result.data["pos"], point)

    def test_horizontal_relay_also_accepts_safe_api_rounding(self):
        world = CanopyWorld(cut=False, relay=True)
        world.after_move = lambda world: world.status.update(pos=[1.4, 82.05, .6])
        result = self.run_skill(world)
        self.assertTrue(result.success, (result.error_code, result.message))
        self.assertEqual(result.data["support"], "oak_leaves")
        self.assertEqual(result.data["evacuation_stage"], "relay")

    def test_landing_edge_wrong_cell_and_y_error_remain_unknown(self):
        for point in ([1.7, 81, .5], [1.626, 81, .5], [2.5, 81, .5], [1.5, 81.06, .5], [1.5, 80, .5]):
            with self.subTest(point=point):
                world = CanopyWorld()
                world.after_move = lambda world: world.status.update(pos=point)
                self.assert_unknown(self.run_skill(world), "action_unconfirmed")

    def test_landing_health_life_danger_or_support_changes_fail(self):
        for change in ({"health": 19}, {"last_death": "respawned"}, {"nearest_hostile": {"dist": 4}}):
            with self.subTest(change=change):
                world = CanopyWorld()
                world.after_move = lambda world: world.status.update(change)
                self.assert_unknown(self.run_skill(world), "action_unconfirmed")
        for key, block in (((1, 80, 0), {"name": "air"}), ((1, 82, 0), {"name": "stone"}), ((1, 80, 1), {"name": "lava"})):
            with self.subTest(key=key):
                world = CanopyWorld()
                world.after_move = lambda world: world.blocks.update({key: block})
                self.assert_unknown(self.run_skill(world), "action_unconfirmed")

    def test_damage_during_final_terrain_read_is_not_success(self):
        world = CanopyWorld()
        def hurt(world, key):
            if world.moved:
                world.status["health"] = 19
        world.on_block = hurt
        self.assert_unknown(self.run_skill(world), "action_unconfirmed")

    def test_late_response_cannot_confirm_expired_landing(self):
        world = CanopyWorld()
        world.on_status = lambda world: self.advance(1.3) if world.moved else None
        self.assert_unknown(self.run_skill(world), "action_unconfirmed")
        self.assertEqual(len(world.writes("move_to")), 1)

    def test_cancel_before_any_write_and_strict_argument_surface(self):
        world = CanopyWorld()
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(self.run_skill(world, cancel=cancel).error_code, "cancelled")
        skill = CanopyRecoverySkill(world)
        self.assertEqual(set(skill.ARGUMENTS), {"descend_canopy"})
        self.assertEqual(skill.execute("gather_block", {}).error_code, "unknown_skill")
        for args in ({"pos": world.target}, {**world.args(), "force": True},
                     {**world.args(), "pos": [float("nan"), 81, .5]}, {**world.args(), "clear_pos": [1.1, 81, 0]}):
            with self.subTest(args=args):
                self.assertEqual(self.run_skill(world, args).error_code, "bad_args")
        self.assertEqual(world.writes(), [])


if __name__ == "__main__":
    unittest.main()
