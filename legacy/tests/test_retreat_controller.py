"""A failed local search expires on new danger, never on a blind retry loop."""

import copy
import unittest
from unittest.mock import patch

from agent.survival_planner import Decision
from core.retreat_controller import RetreatController
from skills import SkillResult


class RetreatCooldownTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        timer = patch("core.retreat_controller.time.time", side_effect=lambda: self.now)
        timer.start()
        self.addCleanup(timer.stop)
        self.context = {}
        self.controller = RetreatController(self.context, {"threat_radius": 8, "target_cooldown_seconds": 30})
        self.observation = {"status": {"pos": [.5, 64, .5], "dimension": "overworld", "health": 20,
            "nearest_hostile": {"id": 1, "name": "pillager", "dist": 3}}, "entities": [
            {"id": 1, "name": "pillager", "type": "hostile", "pos": [3.5, 64, .5]}], "fresh": True}
        self.decision = Decision("retreat_from", {"pos": [3.5, 64, .5]}, "near hostile", "safety")
        self.failure = SkillResult(False, "No verified route", {"executed": False}, "unsafe")
        self.fail_search()

    def fail_search(self):
        self.controller.state(self.observation, self.decision.args["pos"])
        self.assertTrue(self.controller.handle_result(self.decision, self.failure, self.observation,
                                                     uncertain_codes=set(), fatal_codes=set()))

    def choose(self):
        return self.controller.choose_decision(self.observation, self.decision)

    def test_unchanged_scene_observes_at_one_second_without_researching(self):
        for second in range(1, 30):
            self.now = 100 + second
            decision = self.choose()
            self.assertEqual(decision.name, "wait")
            self.assertEqual(decision.args["seconds"], 1)
            self.assertEqual(self.context["retreat_recovery"]["retry_after"], 130)
        self.now = 130
        self.assertEqual(self.choose().name, "retreat_from")

    def test_damage_invalidates_old_search_then_same_injury_does_not_spin(self):
        self.now = 101
        self.observation["status"]["health"] = 16.3333
        self.assertEqual(self.choose().name, "retreat_from")
        self.fail_search()
        self.assertEqual(self.context["retreat_recovery"]["search_basis"]["health"], 16.3333)
        for _ in range(5):
            self.assertEqual(self.choose().name, "wait")
        self.observation["status"]["health"] = 17
        self.assertEqual(self.choose().name, "wait")

    def test_new_secondary_shooter_invalidates_even_when_nearest_is_unchanged(self):
        self.observation["entities"].append({"id": 2, "name": "skeleton", "type": "hostile", "pos": [-8, 64, .5]})
        self.assertEqual(self.choose().name, "retreat_from")
        self.fail_search()
        self.assertEqual(self.choose().name, "wait")

    def test_distant_enemy_does_not_invalidate_until_it_enters_its_circle(self):
        secondary = {"id": 2, "name": "skeleton", "type": "hostile", "pos": [18, 64, .5]}
        self.observation["entities"].append(secondary)
        self.assertEqual(self.choose().name, "wait")
        secondary["pos"] = [16, 64, .5]
        self.assertEqual(self.choose().name, "retreat_from")

    def test_known_secondary_must_approach_materially_not_jitter(self):
        secondary = {"id": 2, "name": "skeleton", "type": "hostile", "pos": [10.5, 64, .5]}
        self.observation["entities"].append(secondary)
        self.fail_search()
        secondary["pos"] = [10.4, 64, .5]
        self.assertEqual(self.choose().name, "wait")
        secondary["pos"] = [8.5, 64, .5]
        self.assertEqual(self.choose().name, "retreat_from")

    def test_new_nearest_identity_is_evidence_even_without_entity_row(self):
        self.observation["status"]["nearest_hostile"] = {"id": 2, "name": "skeleton", "dist": 2.5}
        self.assertEqual(self.choose().name, "retreat_from")

    def test_unknown_failure_does_not_replace_the_search_basis(self):
        before = copy.deepcopy(self.context)
        self.observation["status"]["health"] = 10
        result = SkillResult(False, "Lost reply", {"action_state": "unknown", "candidate_failed": True}, "unsafe")
        self.assertFalse(self.controller.handle_result(self.decision, result, self.observation,
                                                      uncertain_codes=set(), fatal_codes=set()))
        self.assertEqual(self.context, before)

    def test_melee_only_cooldown_keeps_ordinary_observation_interval(self):
        self.observation["entities"][0]["name"] = "zombie"
        self.observation["status"]["nearest_hostile"]["name"] = "zombie"
        self.fail_search()
        self.assertEqual(self.choose().args["seconds"], 5)


if __name__ == "__main__":
    unittest.main()
