"""Unit and harness-integration tests for `core.safety_arbiter.SafetyArbiter`."""

import copy
import tempfile
import unittest
from unittest.mock import Mock

from agent.llm_planner import LLMSurvivalPlanner
from agent.survival_planner import Decision, SurvivalPlanner
from core.config import DEFAULTS
from core.safety_arbiter import SafetyArbiter
from core.survival_harness import SurvivalHarness
from skills import SkillResult
from test_llm_planner import Brain, response


def _status(**overrides):
    base = {
        "username": "TestBot",
        "ready": True,
        "alive": True,
        "busy": None,
        "gamemode": "survival",
        "dimension": "overworld",
        "pos": [0.5, 64.0, 0.5],
        "health": 20.0,
        "food": 20,
        "oxygen": 20,
        "on_ground": True,
        "is_day": True,
        "time_of_day": 2000,
        "thundering": False,
        "is_sleeping": False,
        "in_vehicle": False,
        "in_water": False,
        "submerged": False,
        "in_lava": False,
        "on_fire": False,
        "last_death": None,
        "nearest_hostile": None,
    }
    base.update(overrides)
    return base


class SafetyArbiterTests(unittest.TestCase):
    def _arbiter(self, *, llm_override_on_block=True):
        settings = copy.deepcopy(DEFAULTS["survival"])
        settings["llm_override_on_block"] = llm_override_on_block
        rules = SurvivalPlanner(settings)
        return SafetyArbiter(
            rules,
            settings,
            daily=True,
            competition_mode=False,
            llm_override_on_block=llm_override_on_block,
        )

    def test_low_health_planning_does_not_self_cancel_when_override_enabled(self):
        arbiter = self._arbiter(llm_override_on_block=True)
        baseline = _status(health=7.3, is_day=False, time_of_day=18000)
        current = _status(health=7.3, is_day=False, time_of_day=18020)

        stable = arbiter.evaluate_monitor(
            current,
            baseline,
            decision=None,
            planning=True,
        )
        self.assertFalse(stable.danger)
        self.assertTrue(stable.low_health_allowed)

        # Any real health drop still triggers immediate preemption.
        hurt = _status(health=5.5, is_day=False, time_of_day=18025)
        worsened = arbiter.evaluate_monitor(
            hurt,
            baseline,
            decision=None,
            planning=True,
        )
        self.assertTrue(worsened.danger)
        self.assertIn("low_health_deteriorated", worsened.reasons)

    def test_low_health_planning_strict_mode_remains_protected(self):
        arbiter = self._arbiter(llm_override_on_block=False)
        baseline = _status(health=6.0)
        strict = arbiter.evaluate_monitor(
            _status(health=6.0),
            baseline,
            decision=None,
            planning=True,
        )
        self.assertTrue(strict.danger)
        self.assertIn("low_health_deteriorated", strict.reasons)

    def test_short_safety_wait_streak_upgrades_to_full_observation(self):
        arbiter = self._arbiter(llm_override_on_block=True)
        short = {
            "status": _status(nearest_hostile={"id": 1, "name": "skeleton", "dist": 14.0}),
            "entities": [],
            "observation_scope": "safety",
        }
        use_short_0, close_0 = arbiter.should_use_short_safety(short, {}, safety_wait_streak=0)
        self.assertTrue(use_short_0)
        self.assertTrue(close_0)  # skeleton threat radius is 16m

        use_short_2, _ = arbiter.should_use_short_safety(short, {}, safety_wait_streak=2)
        self.assertFalse(use_short_2)

        # Head burial must stay on short emergency channel.
        buried = {**short, "buried_head": [0, 65, 0]}
        use_short_buried, _ = arbiter.should_use_short_safety(buried, {}, safety_wait_streak=5)
        self.assertTrue(use_short_buried)

    def test_admitted_short_decision_includes_burrow_and_clear_burial(self):
        self.assertTrue(SafetyArbiter.is_admitted_short_decision(Decision("emergency_burrow", {}, "", "safety")))
        self.assertTrue(SafetyArbiter.is_admitted_short_decision(Decision("clear_burial", {"pos": [0, 65, 0]}, "", "safety")))
        self.assertFalse(SafetyArbiter.is_admitted_short_decision(Decision("gather_block", {"pos": [1, 64, 1]}, "", "wood")))

    def test_harness_breaks_short_safety_wait_loop_after_two_passive_waits(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmpdir = tmp.name
        settings = copy.deepcopy(DEFAULTS["survival"])
        settings.update(
            objective="daily",
            state_dir=tmpdir,
            llm_override_on_block=True,
            disable_sleep=True,
            autonomy={"enabled": False},
        )
        full_obs = {
            "status": _status(nearest_hostile={"id": 77, "name": "skeleton", "dist": 15.0}),
            "inventory": [],
            "counts": {"oak_log": 1},
            "blocks": [{"name": "oak_log", "pos": [2, 64, 0], "dist": 1.6, "can_see": True, "can_dig": True}],
            "entities": [{"id": 77, "type": "hostile", "name": "skeleton", "pos": [-14.5, 64.0, 0.5]}],
            "fresh": True,
            "observation_scope": "full",
            "inventory_fresh": True,
            "terrain_fresh": True,
        }
        short_obs = {
            "status": _status(nearest_hostile={"id": 77, "name": "skeleton", "dist": 15.0}),
            "inventory": [],
            "counts": {},
            "blocks": [],
            "entities": [],
            "fresh": True,
            "observation_scope": "safety",
            "inventory_fresh": False,
            "terrain_fresh": False,
        }
        client = Mock(username="ArbiterBot", base_url="http://offline.invalid/api/v1", password="", token="t")
        client.get_status.side_effect = lambda **kw: {"ok": True, "result": copy.deepcopy(full_obs["status"])}
        client.events.side_effect = lambda **kw: {"ok": True, "result": {"events": [], "last_seq": 0, "has_more": False}}
        client.advancements.side_effect = lambda **kw: {"ok": True, "result": {"ready": True, "advancements": []}}

        class StubCollector:
            def __init__(self):
                self.full_calls = 0

            def collect_safety(self, cancel_event=None, *, force=False, threat_radius=8):
                return copy.deepcopy(short_obs)

            def collect(self, cancel_event=None):
                self.full_calls += 1
                return copy.deepcopy(full_obs)

        collector = StubCollector()
        brain = Brain(response("gather_block", {"pos": [2, 64, 0], "expected_name": "oak_log"}))
        planner = LLMSurvivalPlanner({"enabled": True, "max_repair_attempts": 0}, settings, brain=brain)

        skills = Mock()
        skills.execute.return_value = SkillResult(True, "Chopped", {"executed": True})
        harness = SurvivalHarness(
            client,
            settings,
            copy.deepcopy(DEFAULTS["sleep"]),
            planner=planner,
            skills=skills,
            collector=collector,
        )
        self.addCleanup(harness.close)

        def step():
            harness.request_step()
            harness._future.result(timeout=5)
            return harness.tick(automatic=False)

        # Ticks 1 and 2 hit passive wait(1) on short safety snapshot; tick 3 upgrades to full observation.
        r1 = step()
        self.assertTrue(r1.success, r1)
        self.assertEqual(harness.last_observation.get("observation_scope"), "safety")
        self.assertEqual(harness._safety_wait_streak, 1)

        step()
        self.assertEqual(harness._safety_wait_streak, 2)
        self.assertEqual(collector.full_calls, 0)

        r3 = step()
        self.assertTrue(r3.success, r3)
        self.assertGreaterEqual(collector.full_calls, 1)
        self.assertEqual(harness._safety_wait_streak, 0)


if __name__ == "__main__":
    unittest.main()
