"""Bounded batch integration with the existing verified children, never a game."""

import threading
import unittest
from unittest.mock import patch

from core.client import APIError
from skills import SkillResult
from skills.batch import BatchSkills
from skills.shelter import ShelterSkills
from test_shelter import ShelterClient
from test_survival_skills import World, envelope


class MaterialWorld(World):
    def __init__(self):
        super().__init__()
        self.status.update(gamemode="survival", is_day=True, thundering=False, time_of_day=5000)
        self.scan_effect = None
        self.scan_count = 0

    def get_blocks(self, radius=8, types=None, limit=64):
        self.scan_count += 1
        self.calls.append(("scan", radius, types))
        if self.scan_effect:
            return self.scan_effect()
        return [{"name": block["name"], "pos": list(pos), **block} for pos, block in self.blocks.items()
                if block["name"] in types][:limit]

    def resource(self, pos, name="oak_log"):
        self.set_block(pos, name, harvest_tool_ok=True, can_see=True)


class BatchGatherTests(unittest.TestCase):
    def setUp(self):
        self.world = MaterialWorld()
        self.skills = BatchSkills(self.world)
        for pos in ([2, 64, 0], [2, 64, 1], [1, 64, 2]):
            self.world.resource(pos)

    def gather(self, resource="logs", count=3, radius=8, cancel=None):
        return self.skills.execute("gather_materials", {"resource": resource, "count": count, "radius": radius}, cancel)

    def digs(self):
        return [call for call in self.world.calls if call[0] == "dig"]

    def test_three_fresh_verified_blocks_and_total_inventory_gain(self):
        result = self.gather()
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["completed_blocks"], 3)
        self.assertEqual(result.data["gained"], {"oak_log": 3})
        self.assertEqual(result.data["gained_count"], 3)
        self.assertEqual(len(result.data["steps"]), 3)
        self.assertEqual(self.world.scan_count, 3)
        self.assertEqual(len(self.digs()), 3)
        self.assertEqual(len({tuple(call[1]) for call in self.digs()}), 3)
        self.assertTrue(result.data["executed"])

    def test_count_is_inventory_increment_not_exact_number_of_ore_blocks(self):
        self.world.blocks.clear()
        self.world.resource([2, 64, 0], "coal_ore")
        self.world.counts["stone_pickaxe"] = 1
        def dig(pos):
            self.world.set_block(pos, "air")
            self.world.counts["coal"] = 4
            return envelope({"gained": {"coal": 4}})
        self.world.dig_effect = dig
        result = self.gather("coal", 3)
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["completed_blocks"], 1)
        self.assertEqual(result.data["gained_count"], 4)
        self.assertEqual(len(self.digs()), 1)

    def test_maximum_batch_stops_at_eight_even_with_more_fresh_candidates(self):
        self.world.blocks.clear()
        for x, z in ((1, 0), (1, 1), (1, -1), (1, 2), (2, 0), (2, 1), (2, -1), (2, 2), (0, 1)):
            self.world.resource([x, 64, z])
        result = self.gather(count=8)
        self.assertTrue(result.success, result)
        self.assertEqual(len(self.digs()), 8)
        self.assertEqual(len(result.data["steps"]), 8)

    def test_collection_movement_does_not_expand_the_original_scan_area(self):
        self.world.blocks.clear()
        self.world.resource([2, 64, 0])
        self.world.resource([10, 64, 0])
        def dig(pos):
            self.world.set_block(pos, "air")
            self.world.counts["oak_log"] = 1
            self.world.status["pos"] = [7.5, 64, .5]
            return envelope({"gained": {"oak_log": 1}})
        self.world.dig_effect = dig
        result = self.gather(count=2)
        self.assertEqual(result.error_code, "partial_completion")
        self.assertEqual(len(self.digs()), 1)
        self.assertEqual(result.data["origin"], [.5, 64, .5])

    def test_cobblestone_accepts_stone_target_but_checks_actual_item(self):
        self.world.blocks.clear()
        self.world.resource([2, 64, 0], "stone")
        self.world.counts["stone_pickaxe"] = 1
        result = self.gather("cobblestone", 1)
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["gained"], {"cobblestone": 1})

    def test_missing_candidate_after_one_block_reports_partial_not_complete(self):
        self.world.blocks = {(2, 64, 0): self.world.blocks[(2, 64, 0)]}
        result = self.gather(count=3)
        self.assertEqual(result.error_code, "partial_completion")
        self.assertTrue(result.data["partial"])
        self.assertFalse(result.data["completed"])
        self.assertEqual(result.data["completed_blocks"], 1)
        self.assertEqual(result.data["gained_count"], 1)

    def test_readonly_candidate_scan_does_not_authorize_foot_overhead_or_outside_radius(self):
        self.world.blocks.clear()
        for pos in ([0, 63, 0], [0, 67, 0], [9, 64, 0]):
            self.world.resource(pos)
        result = self.gather(count=1, radius=8)
        self.assertEqual(result.error_code, "no_safe_candidate")
        self.assertFalse(self.digs())
        self.assertFalse(result.data["executed"])

    def test_each_scan_is_fresh_and_does_not_reuse_initial_candidates(self):
        def scan():
            if self.world.scan_count == 1:
                return [{"name": "oak_log", "pos": [2, 64, 0]}]
            return []
        self.world.scan_effect = scan
        result = self.gather(count=3)
        self.assertEqual(result.error_code, "partial_completion")
        self.assertEqual(len(self.digs()), 1)

    def test_malformed_or_unloaded_target_is_not_mined(self):
        self.world.scan_effect = lambda: [{"name": "oak_log", "pos": [float("nan"), 64, 0]}]
        self.assertFalse(self.gather(count=1).success)
        self.assertFalse(self.digs())
        self.world.scan_effect = lambda: [{"name": "oak_log", "pos": [2, 64, 0]}]
        original = self.world.get_block
        def block(pos, **kwargs):
            if pos == [2, 64, 0]:
                raise APIError("not_loaded", "No chunk")
            return original(pos, **kwargs)
        self.world.get_block = block
        self.assertEqual(self.gather(count=1).error_code, "not_loaded")
        self.assertFalse(self.digs())

    def test_hazard_and_target_near_hostile_are_rejected_before_dig(self):
        self.world.blocks = {(2, 64, 0): self.world.blocks[(2, 64, 0)]}
        self.world.set_block([2, 63, 0], "lava")
        self.assertEqual(self.gather(count=1).error_code, "unsafe")
        self.assertFalse(self.digs())
        self.world.set_block([2, 63, 0], "stone")
        self.world.entities = [{"type": "hostile", "pos": [9, 64, 0], "name": "zombie"}]
        self.assertEqual(self.gather(count=1).error_code, "no_safe_candidate")
        self.assertFalse(self.digs())

    def test_last_child_status_rechecks_safety_after_candidate_scan(self):
        def scan():
            self.world.status["nearest_hostile"] = {"name": "zombie", "dist": 2}
            return [{"name": "oak_log", "pos": [2, 64, 0]}]
        self.world.scan_effect = scan
        result = self.gather(count=1)
        self.assertEqual(result.error_code, "unsafe")
        self.assertFalse(self.digs())

    def test_life_change_after_first_verified_block_stops_before_second(self):
        original_scan = self.world.get_blocks
        def scan(**kwargs):
            if self.world.scan_count == 1:
                self.world.status["last_death"] = {"t": 12}
            return original_scan(**kwargs)
        self.world.get_blocks = scan
        result = self.gather(count=3)
        self.assertEqual(result.error_code, "life_changed")
        self.assertTrue(result.data["partial"])
        self.assertEqual(result.data["completed_blocks"], 1)
        self.assertEqual(len(self.digs()), 1)

    def test_damage_during_first_dig_is_unknown_and_no_second_action_runs(self):
        def dig(pos):
            self.world.set_block(pos, "air")
            self.world.counts["oak_log"] = 1
            self.world.status["health"] = 19
            return envelope({"gained": {"oak_log": 1}})
        self.world.dig_effect = dig
        result = self.gather(count=3)
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual(len(self.digs()), 1)

    def test_unknown_second_write_preserves_first_gain_without_replay_or_extra_reads(self):
        def dig(pos):
            if len(self.digs()) == 2:
                raise APIError("request_timeout", "Unknown dig result")
            self.world.set_block(pos, "air")
            self.world.counts["oak_log"] = 1
            return envelope({"gained": {"oak_log": 1}})
        self.world.dig_effect = dig
        result = self.gather(count=3)
        self.assertEqual(result.error_code, "request_timeout")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual(result.data["gained"], {"oak_log": 1})
        self.assertTrue(result.data["partial"])
        self.assertEqual(len(self.digs()), 2)
        after = self.world.calls[self.world.calls.index(self.digs()[-1]) + 1:]
        self.assertEqual([call[0] for call in after], ["stop"])

    def test_cancel_before_and_during_write_does_not_replay(self):
        event = threading.Event()
        event.set()
        result = self.gather(cancel=event)
        self.assertEqual(result.error_code, "cancelled")
        self.assertFalse(self.digs())
        event.clear()
        def dig(pos):
            event.set()
            return envelope({"gained": {"oak_log": 99}})
        self.world.dig_effect = dig
        result = self.gather(cancel=event)
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(len(self.digs()), 1)

    def test_no_reuse_even_if_successful_child_lies_about_inventory(self):
        class LyingChild:
            def execute(self, *args, **kwargs):
                return SkillResult(True, data={"gained": {"oak_log": 99}, "executed": True})
        result = BatchSkills(self.world, gather_skills=LyingChild()).execute("gather_materials", {"resource": "logs", "count": 2})
        self.assertEqual(result.error_code, "batch_unverified")
        self.assertEqual(len(result.data["steps"]), 1)
        self.assertEqual(result.data["gained_count"], 0)

    def test_bad_arguments_and_expired_soft_budget_never_write(self):
        for args in ({"resource": "iron", "count": 1}, {"resource": "logs", "count": 9},
                     {"resource": "logs", "count": True}, {"resource": "logs", "count": 1, "radius": 9}):
            self.assertEqual(self.skills.execute("gather_materials", args).error_code, "bad_args")
        with patch.object(BatchSkills, "MAX_SECONDS", 0):
            self.assertEqual(self.gather(count=1).error_code, "batch_budget_exhausted")
        self.assertFalse(self.digs())

    def test_night_low_health_low_food_or_not_grounded_prevents_batch(self):
        for key, value in (("time_of_day", 11000), ("is_day", False), ("health", 8), ("food", 6), ("on_ground", False)):
            saved = self.world.status[key]
            self.world.status[key] = value
            with self.subTest(key=key):
                self.assertFalse(self.gather(count=1).success)
                self.assertFalse(self.digs())
            self.world.status[key] = saved


class BatchBuildTests(unittest.TestCase):
    def setUp(self):
        self.world = ShelterClient()
        self.world.status.update(is_day=True, thundering=False, time_of_day=5000)
        # A freshly valid interior stance reaches the first two blueprint corners;
        # the batch must never insert the otherwise required stance movement.
        self.world.status["pos"] = [-.5, 64, -.5]
        self.skills = BatchSkills(self.world)
        self.args = {"pos": [0, 64, 0], "material": "cobblestone", "max_blocks": 2}

    def test_two_actual_single_block_verifications_without_model_or_move(self):
        result = self.skills.execute("build_shelter_batch", self.args)
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["completed_blocks"], 2)
        self.assertEqual(result.data["used"], 2)
        self.assertFalse(result.data["shelter_verified"])
        self.assertEqual([action[0] for action in self.world.actions], ["place", "place"])

    def test_needs_position_stops_and_never_inserts_movement(self):
        self.world.status["pos"] = [.5, 65, .5]
        result = self.skills.execute("build_shelter_batch", self.args)
        self.assertEqual(result.error_code, "needs_position")
        self.assertFalse(result.data["executed"])
        self.assertEqual(self.world.actions, [])

    def test_unknown_place_stops_immediately_and_preserves_child_evidence(self):
        def fail(*args, **kwargs):
            self.world.actions.append(("place",))
            raise APIError("request_timeout", "Unknown place result")
        self.world.place_block = fail
        result = self.skills.execute("build_shelter_batch", self.args)
        self.assertEqual(result.error_code, "request_timeout")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual([a[0] for a in self.world.actions], ["place", "stop"])

    def test_finished_shell_does_not_claim_completed_shelter_without_door(self):
        walls, roof, _ = ShelterSkills._blueprint([0, 64, 0])
        for pos in walls + roof:
            self.world.blocks[pos] = {"name": "cobblestone", "properties": {}}
        result = self.skills.execute("build_shelter_batch", {"pos": [0, 64, 0], "material": "cobblestone"})
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["shell_complete"])
        self.assertFalse(result.data["shelter_verified"])
        self.assertTrue(result.data["needs_door"])
        self.assertFalse(result.data["executed"])


if __name__ == "__main__":
    unittest.main()
