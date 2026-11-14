"""Model decisions cannot bypass fresh life, target safety or secret boundaries."""

import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from agent.llm_planner import LLMSurvivalPlanner
from core.config import DEFAULTS
from core.survival_harness import SurvivalHarness
from test_survival_skills import World


class ObservedWorld(World):
    def __init__(self):
        super().__init__()
        self.base_url = "http://fake-game.invalid/api/v1"
        self.username = "SafetyTestBot"
        self.status.update(gamemode="survival", is_day=True, thundering=False, food=20)
        self.entities = []
        self.set_block([2, 64, 0], "oak_log")

    def collect(self, cancel_event=None):
        return copy.deepcopy({"fresh": True, "status": self.status, "counts": self.counts,
                "inventory": [{"name": name, "count": count} for name, count in self.counts.items()],
                "blocks": [{"pos": list(pos), **block} for pos, block in self.blocks.items()],
                "entities": self.entities})


class Model:
    def __init__(self, name, args, before_response=None, content=None):
        self.name, self.args = name, args
        self.before_response, self.content = before_response, content

    def chat_completion(self, messages, tools=None):
        if self.before_response:
            self.before_response()
        return {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": self.content, "tool_calls": [{"id": "safety_call", "type": "function",
                "function": {"name": self.name, "arguments": json.dumps(self.args)}}]}}]}


class LLMSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.world = ObservedWorld()
        self.counter = 0

    def harness(self, name, args, before_response=None, content=None, **settings):
        self.counter += 1
        survival = {**copy.deepcopy(DEFAULTS["survival"]), "state_dir": str(Path(self.temp.name) / str(self.counter)),
                    "poll_interval_seconds": 0.001, "max_runtime_seconds": 10, **settings}
        planner = LLMSurvivalPlanner({"enabled": True, "model": "fake", "api_key": "private-key-sentinel"}, survival,
                                    brain=Model(name, args, before_response, content))
        harness = SurvivalHarness(self.world, survival, DEFAULTS["sleep"], planner=planner, collector=self.world)
        self.addCleanup(harness.close)
        return harness

    def run_one(self, harness):
        self.assertTrue(harness.request_step())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = harness.tick(automatic=False)
            if result is not None:
                return result
            time.sleep(0.001)
        self.fail("Offline safety step did not finish")

    def assert_no_world_action(self):
        self.assertFalse([call for call in self.world.writes() if call[0] != "stop"])

    def test_respawn_during_model_request_invalidates_the_old_decision(self):
        def respawn():
            self.world.status["last_death"] = {"time": "new-death", "cause": "fixture"}

        harness = self.harness("gather_block", {"pos": [2, 64, 0], "expected_name": "oak_log"}, respawn)
        result = self.run_one(harness)
        self.assertEqual(result.error_code, "safety_changed")
        self.assertEqual(harness.context["death_count"], 1)
        self.assert_no_world_action()
        self.assertIsNone(harness.planner._pending)
        self.assertFalse(json.loads(harness.planner.history[-1]["content"])["result"]["success"])

    def test_death_limit_is_rechecked_after_model_request(self):
        def respawn():
            self.world.status["last_death"] = {"time": "new-death", "cause": "fixture"}

        harness = self.harness("gather_block", {"pos": [2, 64, 0], "expected_name": "oak_log"}, respawn, max_deaths=1)
        result = self.run_one(harness)
        self.assertEqual(result.error_code, "death_limit")
        self.assertTrue(harness.paused)
        self.assert_no_world_action()

    def test_dimension_marker_change_invalidates_even_an_allowed_new_dimension(self):
        def change_dimension_marker():
            self.world.status["dimension"] = "minecraft:overworld"

        harness = self.harness("gather_block", {"pos": [2, 64, 0], "expected_name": "oak_log"}, change_dimension_marker)
        self.assertEqual(self.run_one(harness).error_code, "safety_changed")
        self.assert_no_world_action()

    def test_move_gather_and_place_cannot_target_an_observed_hostile_area(self):
        for name, args in (("move_to", {"pos": [20, 64, 0]}),
                           ("gather_block", {"pos": [20, 64, 0], "expected_name": "oak_log"}),
                           ("place_item", {"pos": [20, 64, 0], "item": "crafting_table"})):
            with self.subTest(name=name):
                self.world = ObservedWorld()
                self.world.entities = [{"type": "hostile", "name": "zombie", "pos": [21, 64, 0]}]
                if name == "gather_block":
                    self.world.set_block([20, 64, 0], "oak_log")
                self.world.counts["crafting_table"] = 1
                harness = self.harness(name, args)
                result = self.run_one(harness)
                self.assertFalse(result.success)
                self.assertEqual(result.error_code, "safety_changed")
                self.assert_no_world_action()

    def test_observed_lava_next_to_a_move_target_is_rejected_before_movement(self):
        self.world.set_block([21, 64, 0], "lava")
        harness = self.harness("move_to", {"pos": [20, 64, 0]})
        self.assertEqual(self.run_one(harness).error_code, "safety_changed")
        self.assert_no_world_action()

    def test_model_reason_cannot_put_configured_key_in_raw_survival_journal(self):
        for use_content in (False, True):
            with self.subTest(content_fallback=use_content):
                self.world = ObservedWorld()
                args = {"pos": [2, 64, 0], "expected_name": "oak_log"}
                secret_reason = "Collect wood private-key-sentinel"
                if not use_content:
                    args["reason"] = secret_reason
                harness = self.harness("gather_block", args, content=secret_reason if use_content else None)
                self.assertTrue(self.run_one(harness).success)
                checkpoint = harness.journal.path.read_text(encoding="utf-8")
                episodes = harness.journal.events_path.read_text(encoding="utf-8")
                self.assertNotIn("private-key-sentinel", checkpoint + episodes)
                self.assertIn("REDACTED", episodes)


if __name__ == "__main__":
    unittest.main()
