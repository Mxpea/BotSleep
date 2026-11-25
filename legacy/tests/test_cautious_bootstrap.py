"""Final-write guards for the trusted low-health bootstrap permission."""

import unittest

from skills.daily_survival import DailySurvivalSkills
from test_survival_skills import World, envelope


class CautiousBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.world.status.update(health=6, food=17, is_day=True, thundering=False,
                                 time_of_day=1000, gamemode="survival", last_death=None)
        self.target = [2, 64, 0]
        self.world.set_block(self.target, "oak_log", can_dig=True, can_see=True)
        self.skills = DailySurvivalSkills(self.world)

    def gather(self):
        return self.skills.execute("gather_block", {"pos": self.target, "expected_name": "oak_log"}, cautious=True)

    def test_reachable_log_uses_one_dig_without_pickup_navigation(self):
        result = self.gather()
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["cautious_bootstrap"])
        self.assertEqual(result.data["gained"], {"oak_log": 1})
        self.assertEqual(self.world.writes(), [("dig", self.target, False)])

    def test_actual_no_pickup_is_unverified_even_when_backend_claims_gained(self):
        def dig(pos):
            self.world.set_block(pos, "air")
            return envelope({"dug": "oak_log", "gained": {"oak_log": 99}})
        self.world.dig_effect = dig
        result = self.gather()
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "gather_unverified")
        self.assertTrue(result.data["block_removed"])
        self.assertEqual(result.data["gained"], {})
        self.assertEqual([call for call in self.world.calls if call[0] == "dig"], [("dig", self.target, False)])
        self.assertFalse(any(call[0] == "move_to" for call in self.world.calls))

    def test_gamemode_change_only_at_last_status_prevents_write(self):
        original = self.world.get_status
        reads = 0
        def status(**kwargs):
            nonlocal reads
            reads += 1
            if reads >= 3:
                self.world.status["gamemode"] = "creative"
            return original(**kwargs)
        self.world.get_status = status
        result = self.gather()
        self.assertEqual(result.error_code, "unsafe_state")
        self.assertEqual(reads, 3)
        self.assertFalse(self.world.writes())

    def test_visibility_change_in_last_target_read_prevents_write(self):
        original = self.world.get_block
        reads = 0
        def block(pos, **kwargs):
            nonlocal reads
            if pos == self.target:
                reads += 1
                # Initial cautious guard and both normal gather guards have
                # already seen clear sight; this is the final write guard.
                if reads >= 6:
                    self.world.blocks[tuple(self.target)]["can_see"] = False
            return original(pos, **kwargs)
        self.world.get_block = block
        result = self.gather()
        self.assertFalse(result.success)
        self.assertIn(result.error_code, {"unsafe_state", "target_occluded"})
        self.assertGreaterEqual(reads, 6)
        self.assertFalse(self.world.writes())

    def test_missing_api_visibility_requires_real_unobstructed_geometry(self):
        self.world.blocks[tuple(self.target)].pop("can_see")
        self.world.set_block([1, 65, 0], "stone")
        result = self.gather()
        self.assertFalse(result.success)
        self.assertFalse(self.world.writes())
        self.world.set_block([1, 65, 0], "air")
        result = self.gather()
        self.assertTrue(result.success, result)
        self.assertEqual([call[0] for call in self.world.writes()], ["dig"])

    def test_far_target_is_rejected_without_approach(self):
        self.target = [4, 64, 0]
        self.world.set_block(self.target, "oak_log", can_dig=True, can_see=True)
        self.assertEqual(self.gather().error_code, "unsafe_state")
        self.assertFalse(self.world.writes())

    def test_tree_trunk_support_is_not_safe_ground_for_cautious_production(self):
        self.world.set_block([0, 63, 0], "oak_log")
        self.assertEqual(self.gather().error_code, "unsafe_state")
        self.assertFalse(self.world.writes())

    def test_near_workstation_crafts_but_far_workstation_never_writes(self):
        for pos in ([2, 64, 1], [8, 64, 1]):
            with self.subTest(pos=pos):
                self.world.calls.clear()
                self.world.counts.clear()
                self.world.get_blocks = lambda **kwargs: [{"name": "crafting_table", "pos": pos}]
                result = self.skills.execute("craft_item", {"item": "wooden_pickaxe", "count": 1}, cautious=True)
                if pos[0] == 2:
                    self.assertTrue(result.success, result)
                    self.assertEqual([call[0] for call in self.world.writes()], ["craft"])
                else:
                    self.assertEqual(result.error_code, "missing_workstation")
                    self.assertFalse(self.world.writes())

    def test_two_by_two_craft_does_not_search_for_a_distant_table(self):
        def tables(**kwargs):
            self.fail("A 2x2 recipe must not require a workstation lookup")
        self.world.get_blocks = tables
        result = self.skills.execute("craft_item", {"item": "stick", "count": 1}, cautious=True)
        self.assertTrue(result.success, result)
        self.assertEqual([call[0] for call in self.world.writes()], ["craft"])


if __name__ == "__main__":
    unittest.main()
