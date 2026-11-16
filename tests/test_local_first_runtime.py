"""Local-first acceptance: actual harness/skills, no real game or provider."""

import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from agent.llm_planner import LLMSurvivalPlanner
from core.client import APIError
from core.config import DEFAULTS
from core.survival_harness import SurvivalHarness
from skills import SkillResult
from test_llm_runtime import FakeMinecraftClient
from test_llm_planner import Brain, observation, response
from test_daily_runtime import RuntimeDailyWorld


class OpeningWorld(FakeMinecraftClient):
    """Resource-conserving starter world for the real skills and collector."""

    def __init__(self):
        super().__init__()
        self.blocks = {(x, 64, z): "oak_log" for x, z in ((2, 0), (2, 1), (2, -1), (3, 0), (3, 1), (3, -1))}
        self.blocks.update({(x, y, z): "stone" for x in (-2, -1) for y in (64, 65) for z in (-1, 0, 1, 2)})

    def dig(self, pos, collect=True):
        if self.blocks.get(tuple(pos)) == "stone" and not any(self.counts.get(tier + "_pickaxe", 0) for tier in ("wooden", "stone")):
            raise AssertionError("Stone requires a real pickaxe")
        answer = super().dig(pos, collect=collect)
        if answer["result"]["dug"] == "stone":
            self.counts["stone"] -= 1
            self.counts["cobblestone"] = self.counts.get("cobblestone", 0) + 1
        return answer

    def craft(self, item, count=1):
        recipes = {
            "oak_planks": (4, {"oak_log": 1}), "stick": (4, {"oak_planks": 2}),
            "crafting_table": (1, {"oak_planks": 4}),
            "wooden_pickaxe": (1, {"oak_planks": 3, "stick": 2}),
            "stone_pickaxe": (1, {"cobblestone": 3, "stick": 2}),
            "stone_axe": (1, {"cobblestone": 3, "stick": 2}),
            "stone_sword": (1, {"cobblestone": 2, "stick": 1}),
            "furnace": (1, {"cobblestone": 8}),
        }
        output, materials = recipes[item]
        batches = (count + output - 1) // output
        for name, amount in materials.items():
            if self.counts.get(name, 0) < amount * batches:
                raise AssertionError(f"Missing {name} for {item}")
        if item in {"wooden_pickaxe", "stone_pickaxe", "stone_axe", "stone_sword", "furnace"}:
            import math
            if not any(name == "crafting_table" and math.dist(pos, self.status["pos"]) <= 4 for pos, name in self.blocks.items()):
                raise AssertionError("Tool recipe requires a nearby placed crafting table")
        for name, amount in materials.items():
            self.counts[name] -= amount * batches
        self.counts[item] = self.counts.get(item, 0) + output * batches
        self.calls.append(("craft", item, count))
        return {"ok": True, "result": {"crafted": item}}

    def place_block(self, x, y, z, item):
        if self.counts.get(item, 0) < 1 or (x, y, z) in self.blocks:
            raise AssertionError("Placement requires an item and empty cell")
        self.counts[item] -= 1
        self.blocks[(x, y, z)] = item
        self.calls.append(("place", item, [x, y, z]))
        return {"ok": True, "result": {"placed": item}}


class LocalFirstRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.counter = 0

    def harness(self, *, objective="starter", world=None, collector=None, brain=None, **limits):
        self.counter += 1
        settings = copy.deepcopy(DEFAULTS["survival"])
        settings.update(objective=objective, state_dir=str(Path(self.directory.name) / str(self.counter)),
                        max_steps=200, max_runtime_seconds=120, poll_interval_seconds=.001, retry_seconds=.001)
        settings["local_execution"].update(enabled=True, **limits)
        brain = brain or Brain(response())
        planner = LLMSurvivalPlanner({"enabled": True, "max_repair_attempts": 0}, settings, brain=brain)
        runner = SurvivalHarness(world or FakeMinecraftClient(), settings, DEFAULTS["sleep"],
                                 planner=planner, collector=collector)
        self.addCleanup(runner.close)
        return runner, brain

    def step(self, runner):
        runner._next_attempt = 0
        self.assertTrue(runner._start())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = runner.tick(automatic=False)
            if result is not None:
                return result
            time.sleep(.001)
        self.fail("Local-first step did not finish")

    def test_local_relay_wakes_promptly_but_honors_pause_and_retry_delay(self):
        runner, _ = self.harness()
        runner.settings["poll_interval_seconds"] = 3
        runner.context["local_execution"] = {"phase": "running"}
        runner._next_attempt = 0
        self.assertEqual(runner.next_poll_seconds, .05)
        runner._next_attempt = time.monotonic() + 20
        self.assertEqual(runner.next_poll_seconds, 3)
        runner._paused = True
        runner._next_attempt = 0
        self.assertEqual(runner.next_poll_seconds, 3)

    def test_opening_tool_chain_finishes_without_any_model_request(self):
        runner, brain = self.harness(world=OpeningWorld())
        for _ in range(64):
            result = self.step(runner)
            self.assertTrue(result.success, result)
            if runner.finished:
                break
        self.assertTrue(runner.completed)
        self.assertEqual(brain.requests, [])
        self.assertTrue(runner.planner.progress(runner.last_observation)["starter_goal_complete"])
        self.assertIsNone(runner.journal.state["pending"])
        self.assertTrue(runner.snapshot()["local_execution"]["recent"])

    def test_known_failed_local_action_yields_to_model_once_instead_of_replaying(self):
        world = FakeMinecraftClient()
        world.dig_error = "no_path"
        runner, brain = self.harness(world=world)
        first = self.step(runner)
        self.assertFalse(first.success)
        self.assertEqual(brain.requests, [])
        second = self.step(runner)
        self.assertTrue(second.success, second)
        self.assertEqual(len(brain.requests), 1)
        body = json.loads(brain.requests[0]["messages"][-1]["content"])
        self.assertEqual(body["context"]["local_execution"]["phase"], "model_takeover")
        self.assertEqual(len([row for row in world.calls if row[0] == "dig"]), 1)

    def test_local_resource_failure_tries_another_observed_target_without_model(self):
        world = FakeMinecraftClient()
        world.blocks[(2, 64, 1)] = "oak_log"
        original = world.dig
        def dig(pos, **kwargs):
            if list(pos) == [2, 64, 0]:
                world.calls.append(("dig", list(pos), True))
                return {"ok": False, "error": {"code": "no_path", "message": "One target unreachable"}}
            return original(pos, **kwargs)
        world.dig = dig
        runner, brain = self.harness(world=world)
        self.assertFalse(self.step(runner).success)
        self.assertTrue(self.step(runner).success)
        self.assertEqual(world.counts.get("oak_log"), 1)
        self.assertEqual(brain.requests, [])
        self.assertEqual([row[1] for row in world.calls if row[0] == "dig"], [[2, 64, 0], [2, 64, 1]])

    def test_unknown_local_write_pauses_without_calling_model_or_replaying(self):
        world = FakeMinecraftClient()
        original = world.dig
        def unknown(*args, **kwargs):
            original(*args, **kwargs)
            raise APIError("request_timeout", "Unknown local write")
        world.dig = unknown
        runner, brain = self.harness(world=world)
        first = self.step(runner)
        self.assertFalse(first.success)
        self.assertTrue(runner.paused)
        self.assertIsNotNone(runner.journal.state["pending"])
        for _ in range(3): runner.tick(automatic=True)
        self.assertEqual(brain.requests, [])
        self.assertEqual(len([row for row in world.calls if row[0] == "dig"]), 1)

    def test_local_step_budget_hands_over_with_verified_feedback_summary(self):
        runner, brain = self.harness(max_steps=1)
        self.assertTrue(self.step(runner).success)
        self.assertEqual(brain.requests, [])
        self.assertTrue(self.step(runner).success)
        self.assertEqual(len(brain.requests), 1)
        body = json.loads(brain.requests[0]["messages"][-1]["content"])
        local = body["context"]["local_execution"]
        self.assertEqual(local["reason"], "local_budget_exhausted")
        self.assertEqual(local["steps"], 1)
        self.assertTrue(local["recent"][0]["success"])
        self.assertEqual(body["observation"]["counts"].get("oak_log"), 1)

    def test_local_canopy_clearance_and_descent_do_not_wait_for_model(self):
        world = RuntimeDailyWorld()
        world.status.update(is_day=False, time_of_day=14000)
        world.block([0, 63, 0], "birch_leaves", properties={"waterlogged": False})
        world.block([1, 64, 0], "birch_leaves", properties={"waterlogged": False})
        def clear():
            world.block([1, 64, 0], "air")
            return {"ok": True, "result": {"block_removed": True}}
        world.effects["dig"] = clear
        runner, brain = self.harness(objective="daily", world=world)
        observed = runner.collector.collect()
        retreat = runner._safety_rules.choose(observed, runner.context)
        runner._retreat_state(observed, [-3, 64, .5])
        runner.context["retreat_recovery"].update(evacuating=True, canopy_blocked=True,
            retry_after=time.time()+30, blocked_cells=[{"name":"birch_leaves","pos":[1,64,0]}])
        self.assertTrue(self.step(runner).success)
        self.assertEqual(world.blocks[(1,64,0)]["name"], "air")
        self.assertTrue(self.step(runner).success)
        self.assertTrue(self.step(runner).success)
        self.assertNotIn("retreat_recovery", runner.context)
        self.assertEqual(brain.requests, [])

    def test_restored_pending_never_starts_local_or_model_work(self):
        runner, brain = self.harness()
        runner._checkpoint(context=runner.context, pending={"decision":{"name":"gather_block"}})
        runner._paused=True
        runner.tick(automatic=True)
        self.assertFalse(runner.busy)
        self.assertEqual(brain.requests, [])

    def test_uncertain_provider_gate_never_triggers_local_rule_fallback(self):
        runner, brain = self.harness()
        gate = Mock()
        gate.snapshot.return_value = {"state":"uncertain"}
        brain.request_gate = gate
        runner._model_blocked = "llm_request_uncertain"
        result = self.step(runner)
        self.assertTrue(result.data["waiting"])
        self.assertEqual(brain.requests, [])
        self.assertEqual(runner.client.writes(), [])


if __name__ == "__main__":
    unittest.main()
