"""Daily harness integration against real local HTTP and an in-memory game.

No provider or Minecraft connection is made. Skills, collectors, native model
feedback, the uncertainty gate, memory and the persistent journal are real.
"""

import copy
import json
import math
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from agent.brain import OpenAIBrain
from agent.llm_planner import LLMSurvivalPlanner
from agent.survival_planner import Decision
from core.client import APIError
from core.config import DEFAULTS
from core.daily_observation import DailyObservationCollector
from core.model_gate import ModelRequestGate
from core.survival_harness import SurvivalHarness
from skills.daily_survival import DailySurvivalSkills
from test_daily_skills import DailyWorld, envelope
from test_llm_runtime import FakeChatEndpoint, tool_response


class RuntimeDailyWorld(DailyWorld):
    def __init__(self):
        super().__init__()
        self.base_url = "http://offline-game.invalid/api/v1"
        self.username = "OfflineDailyBot"
        self.password = "offline-password"
        self.token = "offline-token"
        self.status.update(is_day=True, thundering=False, time_of_day=1000, is_sleeping=False)

    def get_blocks(self, radius=16, types=None, limit=64):
        rows, seen = [], set()
        entries = sorted(self.blocks.items(), key=lambda pair: math.dist(pair[0], self.status["pos"]))
        for pos, value in entries:
            name = value["name"]
            if name == "air" or math.dist(pos, self.status["pos"]) > radius or (types is not None and name not in types):
                continue
            if types is None and name in seen:
                continue
            seen.add(name)
            rows.append({"pos": list(pos), "properties": {}, "diggable": True, **copy.deepcopy(value)})
        return rows[:limit]

    def window_view(self, pos, **kwargs):
        result = super().window_view(pos, **kwargs)
        result["result"]["target"] = self.blocks[tuple(pos)]["name"]
        return result

    def use_item(self, item):
        self.calls.append(("use_item", item))
        if self.counts.get(item, 0) < 1:
            raise APIError("missing_item", "No food")
        self.counts[item] -= 1
        self.status["food"] = min(20, self.status["food"] + 5)
        return envelope({"consumed": item})

    def place_block(self, x, y, z, item=None):
        if item.endswith("_bed") or item.endswith("_door"):
            result = super().place_block(x, y, z, item)
            if item.endswith("_bed"):
                for row in self.blocks.values():
                    if row["name"] == item:
                        row["properties"].setdefault("occupied", False)
            return result
        self.calls.append(("place", [x, y, z], item))
        if self.get_block([x, y, z])["result"]["name"] != "air":
            raise APIError("occupied", "The fixture forbids replacing a block")
        if self.counts.get(item, 0) < 1:
            raise APIError("missing_item", "The fixture forbids material grants")
        self.block([x, y, z], item)
        self.counts[item] -= 1
        return envelope({"placed": item})

    def interact(self, pos=None, item=None, **kwargs):
        if item is not None:
            return super().interact(pos=pos, item=item, **kwargs)
        self.calls.append(("interact", list(pos), item))
        block = self.blocks[tuple(pos)]
        if not block["name"].endswith("_door"):
            raise APIError("unsupported", "Fixture interaction only toggles a real door")
        lower = [pos[0], pos[1] - int(block["properties"]["half"] == "upper"), pos[2]]
        opened = not block["properties"]["open"]
        for dy in (0, 1):
            self.blocks[(lower[0], lower[1] + dy, lower[2])]["properties"]["open"] = opened
        return envelope({"interacted": True})

    def furnace(self, pos=(2, 64, 0)):
        self.block(pos, "furnace")
        self.window_type = "furnace"
        self.sections = {"input": [], "fuel": [], "output": []}
        self.info = {"burning": False}

    def finish_smelting(self):
        """Advance the fake world's furnace, consuming its actual loaded items."""
        row = self.sections["input"][0]
        self.sections["output"] = [{"name": DailySurvivalSkills.SMELTABLE[row["name"]], "count": row["count"]}]
        self.sections["input"] = []
        self.sections["fuel"] = []
        self.info["burning"] = False


class DailyRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.world = RuntimeDailyWorld()
        self.index = 0

    def make_harness(self, replies=None, *, state_dir=None, timeout=1):
        self.index += 1
        settings = {**copy.deepcopy(DEFAULTS["survival"]), "objective": "daily",
            "state_dir": state_dir or str(Path(self.temp.name) / str(self.index)),
            "poll_interval_seconds": .001, "retry_seconds": .001, "max_steps": 30, "max_runtime_seconds": 30}
        endpoint, planner = None, None
        if replies is not None:
            endpoint = FakeChatEndpoint(replies)
            self.addCleanup(endpoint.close)
            gate = ModelRequestGate(endpoint.base_url, "offline-model-key", directory=Path(self.temp.name) / "gates")
            brain = OpenAIBrain(endpoint.base_url, "offline-model-key", "offline-model", timeout_seconds=timeout, request_gate=gate)
            planner = LLMSurvivalPlanner({"enabled": True, "model": "offline-model", "api_key": "offline-model-key",
                "max_repair_attempts": 0}, settings, brain=brain)
        harness = SurvivalHarness(self.world, settings, DEFAULTS["sleep"], planner=planner)
        self.assertIsInstance(harness.collector, DailyObservationCollector)
        self.assertIsInstance(harness.skills, DailySurvivalSkills)
        self.addCleanup(harness.close)
        return harness, endpoint

    def step(self, harness, manual=None):
        self.assertTrue(harness._start(manual))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = harness.tick(automatic=False)
            if result is not None:
                return result
            time.sleep(.001)
        self.fail("Offline daily cycle did not finish")

    @staticmethod
    def prompt(request):
        message = next(message for message in reversed(request["body"]["messages"]) if message["role"] == "user")
        return json.loads(message["content"])

    def test_native_daily_tools_smelt_job_fresh_slots_feedback_and_real_output_gain(self):
        self.world.furnace()
        self.world.counts = {"raw_iron": 3, "coal": 1}
        harness, endpoint = self.make_harness([
            tool_response("smelt_start", {"pos": [2, 64, 0], "input_item": "raw_iron", "count": 3,
                "fuel_item": "coal", "fuel_count": 1}, "start"),
            tool_response("inspect_container", {"pos": [2, 64, 0]}, "inspect"),
            tool_response("smelt_collect", {"pos": [2, 64, 0], "item": "iron_ingot", "count": 3}, "collect"),
        ])
        result = self.step(harness)
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["stage"], "started")
        self.assertFalse(result.data["completed"])
        self.assertEqual(self.world.counts["raw_iron"], 0)
        self.assertEqual(self.world.counts.get("iron_ingot", 0), 0)
        self.assertEqual(harness.context["smelting_jobs"][0]["expected_output"], "iron_ingot")
        tools = {entry["function"]["name"] for entry in endpoint.requests[0]["body"]["tools"]}
        self.assertTrue(set(DailySurvivalSkills.ARGUMENTS).issubset(tools))
        self.assertIn("plan_shelter", tools)
        self.assertNotIn("goal_complete", tools)
        self.assertTrue(self.step(harness).success)
        prompt = self.prompt(endpoint.requests[1])
        self.assertEqual(prompt["observation"]["furnaces"][0]["input"], {"name": "raw_iron", "count": 3})
        feedback_rows = [m for m in endpoint.requests[1]["body"]["messages"] if m["role"] == "tool"]
        self.assertTrue(feedback_rows, "The next native request must retain the actual previous tool result")
        feedback = next(m for m in feedback_rows if m["tool_call_id"] == "start")
        self.assertEqual(feedback["tool_call_id"], "start")
        self.assertFalse(json.loads(feedback["content"])["result"]["data"]["completed"])
        self.world.finish_smelting()
        result = self.step(harness)
        self.assertTrue(result.success, result)
        self.assertEqual(self.world.counts["iron_ingot"], 3)
        self.assertEqual(self.prompt(endpoint.requests[2])["observation"]["furnaces"][0]["output"]["count"], 3)
        self.assertIn("inspect", {m["tool_call_id"] for m in endpoint.requests[2]["body"]["messages"] if m["role"] == "tool"})
        self.assertEqual(harness.context["smelting_jobs"], [])
        self.assertEqual(len([call for call in self.world.calls if call[0] == "window_act"]), 2)

    def test_current_window_change_after_model_request_prevents_stale_smelt_start(self):
        self.world.furnace()
        self.world.counts = {"raw_iron": 3, "coal": 1}
        harness, endpoint = self.make_harness([tool_response("smelt_start", {
            "pos": [2, 64, 0], "input_item": "raw_iron", "count": 3, "fuel_item": "coal", "fuel_count": 1})])
        endpoint.before_response = lambda: self.world.sections["input"].append({"name": "beef", "count": 1})
        result = self.step(harness)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "container_busy")
        self.assertEqual(self.world.counts["raw_iron"], 3)
        self.assertFalse([call for call in self.world.calls if call[0] == "window_act"])

    def test_collector_burning_info_cannot_be_planned_as_an_empty_idle_furnace(self):
        self.world.furnace()
        self.world.counts = {"raw_iron": 3, "coal": 1}
        self.world.info["burning"] = True
        harness, _ = self.make_harness()
        observation = harness.collector.collect()
        self.assertTrue(observation["furnaces"][0]["info"]["burning"])
        board = harness.daily_goals.build(observation)
        iron = next(row for row in board["tasks"] if row["id"] == "iron_tools")
        self.assertIsNone(iron["next_action"])
        self.assertFalse([call for call in self.world.calls if call[0] == "window_act"])

    def test_rules_mode_verified_collection_clears_the_started_job(self):
        self.world.furnace()
        self.world.counts = {"raw_iron": 3, "coal": 1}
        harness, _ = self.make_harness()
        start = Decision("smelt_start", {"pos": [2, 64, 0], "input_item": "raw_iron", "count": 3,
            "fuel_item": "coal", "fuel_count": 1}, "Load the fixture furnace", "iron_tools")
        self.assertTrue(self.step(harness, start).success)
        self.assertEqual(len(harness.context["smelting_jobs"]), 1)
        self.world.finish_smelting()
        take = Decision("smelt_collect", {"pos": [2, 64, 0], "item": "iron_ingot", "count": 3}, "Collect actual output", "iron_tools")
        self.assertTrue(self.step(harness, take).success)
        self.assertEqual(self.world.counts["iron_ingot"], 3)
        self.assertEqual(harness.context["smelting_jobs"], [])

    def test_unknown_partial_container_write_retains_pending_and_is_not_replayed(self):
        self.world.block([2, 64, 0], "chest")
        self.world.counts = {"cobblestone": 4}
        def lost_response(ops):
            self.world.counts["cobblestone"] -= 1
            self.world.sections["container"] = [{"name": "cobblestone", "count": 1}]
            raise APIError("request_timeout", "The transfer response was lost")
        self.world.effects["window"] = lost_response
        harness, endpoint = self.make_harness([tool_response("store_items", {"pos": [2, 64, 0], "item": "cobblestone", "count": 4})])
        result = self.step(harness)
        self.assertEqual(result.error_code, "request_timeout")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertTrue(harness.paused)
        self.assertEqual(self.world.counts["cobblestone"], 3)
        pending = copy.deepcopy(harness.journal.state["pending"])
        self.assertEqual(pending["decision"]["name"], "store_items")
        harness.close()
        restored, _ = self.make_harness(state_dir=harness.settings["state_dir"])
        restored.tick()
        self.assertTrue(restored.paused)
        self.assertEqual(restored.journal.state["pending"], pending)
        self.assertEqual(len([c for c in self.world.calls if c[0] == "window_act"]), 1)
        self.assertEqual(len(endpoint.requests), 1)

    def test_custom_unknown_result_skips_post_observation_and_preserves_pending(self):
        self.world.block([2, 64, 0], "chest")
        self.world.counts = {"cobblestone": 4}
        def partial_write(ops):
            self.world.counts["cobblestone"] -= 1
            self.world.sections["container"] = [{"name": "cobblestone", "count": 1}]
            raise APIError("request_timeout", "Actual fixture transfer happened but response was lost")
        self.world.effects["window"] = partial_write
        harness, endpoint = self.make_harness([tool_response("store_items", {"pos": [2, 64, 0], "item": "cobblestone", "count": 4})])
        actual_execute = harness.skills.execute
        def custom_error(*args, **kwargs):
            result = actual_execute(*args, **kwargs)
            self.assertEqual(result.data["action_state"], "unknown")
            result.error_code = "future_transfer_outcome_unconfirmed"
            return result
        with patch.object(harness.skills, "execute", side_effect=custom_error), \
                patch.object(harness.collector, "collect", wraps=harness.collector.collect) as collect:
            result = self.step(harness)
            self.assertEqual(collect.call_count, 2, "Only planning and pre-action recheck may collect; unknown writes forbid post-collection")
        self.assertEqual(result.error_code, "future_transfer_outcome_unconfirmed")
        self.assertTrue(harness.paused)
        self.assertFalse(harness.last_observation["fresh"])
        self.assertEqual(harness.journal.state["pending"]["decision"]["name"], "store_items")
        self.assertEqual(self.world.counts["cobblestone"], 3)
        self.assertIsNone(harness.tick())
        self.assertEqual(len([call for call in self.world.calls if call[0] == "window_act"]), 1)
        self.assertEqual(len(endpoint.requests), 1)

    def test_shelter_plan_persists_project_then_fresh_geometry_drives_one_real_block(self):
        self.world.status["pos"] = [-.5, 64, -.5]
        self.world.counts = {"oak_planks": 55}
        harness, endpoint = self.make_harness([
            tool_response("plan_shelter", {"pos": [0, 64, 0], "material": "oak_planks"}, "plan"),
            tool_response("build_shelter_step", {"pos": [0, 64, 0], "material": "oak_planks"}, "build"),
        ])
        result = self.step(harness)
        self.assertTrue(result.success, result)
        self.assertFalse(result.data["shelter_verified"])
        self.assertEqual(harness.context["base_project"]["pos"], [0, 64, 0])
        self.assertFalse([c for c in self.world.calls if c[0] == "place"])
        result = self.step(harness)
        self.assertTrue(result.success, result)
        self.assertEqual(self.world.counts["oak_planks"], 54)
        self.assertEqual(len([c for c in self.world.calls if c[0] == "place"]), 1)
        prompt = self.prompt(endpoint.requests[1])
        self.assertTrue(prompt["observation"]["daily_evidence"]["base_site"]["fresh"])
        self.assertEqual(prompt["observation"]["shelter"]["missing_blocks"], 55)
        self.assertFalse(harness.last_observation["shelter"]["shelter_verified"])
        self.assertEqual(harness.last_observation["shelter"]["missing_blocks"], 54)
        self.assertFalse(harness.completed)

    def test_starter_inventory_in_daily_rules_mode_continues_real_maintenance_actions(self):
        self.world.counts = {"stone_pickaxe": 1, "stone_axe": 1, "stone_sword": 1, "furnace": 1}
        harness, _ = self.make_harness()
        result = self.step(harness)
        self.assertTrue(result.success, result)
        self.assertFalse(harness.completed)
        self.assertFalse(harness.finished)
        self.assertEqual(self.world.counts["furnace"], 0)
        self.assertTrue(any(block["name"] == "furnace" for block in self.world.blocks.values()))

    def test_actual_home_door_opens_then_moves_inside_then_closes_with_fresh_evidence(self):
        self.world.status.update(pos=[.5, 64, -2.5], time_of_day=11000)
        harness, _ = self.make_harness()
        walls, roof, _ = harness.shelter_skills._blueprint([0, 64, 0])
        for pos in walls + roof:
            self.world.block(pos, "cobblestone")
        for dy, half in ((0, "lower"), (1, "upper")):
            self.world.block([0, 64 + dy, -2], "oak_door",
                             properties={"half": half, "facing": "north", "hinge": "left", "open": False})
        harness.context["base_project"] = {"pos": [0, 64, 0], "material": "cobblestone", "dimension": "overworld"}
        opened = self.step(harness)
        self.assertTrue(opened.success, opened)
        self.assertIn("door", opened.data, opened)
        self.assertTrue(opened.data["door"]["open"])
        self.assertEqual(self.world.status["pos"], [.5, 64, -2.5])
        home = harness.last_observation["daily_evidence"]["shelter"]
        self.assertTrue(home["structure_complete"])
        self.assertFalse(home["shelter_verified"])
        self.assertTrue(home["return_route"]["verified"])
        moved = self.step(harness)
        self.assertTrue(moved.success, moved)
        self.assertEqual(self.world.status["pos"], [.5, 64, .5])
        self.assertTrue(harness.last_observation["daily_evidence"]["shelter"]["inside"])
        self.assertTrue(self.world.blocks[(0, 64, -2)]["properties"]["open"])
        closed = self.step(harness)
        self.assertTrue(closed.success, closed)
        self.assertFalse(closed.data["door"]["open"])
        self.assertTrue(harness.last_observation["daily_evidence"]["shelter"]["shelter_verified"])
        self.assertTrue(self.step(harness).data["waiting"])
        self.assertEqual(len([call for call in self.world.calls if call[0] == "interact"]), 2)
        self.assertEqual(len([call for call in self.world.calls if call[0] == "move"]), 1)

    def test_bed_goal_positions_inside_real_house_then_places_and_observes_both_halves(self):
        self.world.counts = {"white_bed": 1}
        harness, _ = self.make_harness()
        walls, roof, _ = harness.shelter_skills._blueprint([0, 64, 0])
        for pos in walls + roof:
            self.world.block(pos, "cobblestone")
        for dy, half in ((0, "lower"), (1, "upper")):
            self.world.block([0, 64 + dy, -2], "oak_door",
                             properties={"half": half, "facing": "north", "hinge": "left", "open": False})
        harness.context["base_project"] = {"pos": [0, 64, 0], "material": "cobblestone", "dimension": "overworld"}
        observation = harness._observe()
        bed = next(row for row in harness.daily_goals.build(observation)["tasks"] if row["id"] == "bed")
        self.assertEqual(bed["next_action"]["name"], "position_for_task")
        stance = bed["next_action"]["args"]["pos"]
        positioned = self.step(harness, Decision(**bed["next_action"]))
        self.assertTrue(positioned.success, positioned)
        self.assertEqual(self.world.status["pos"], stance)
        movement = next(call for call in self.world.calls if call[0] == "move")
        self.assertEqual(movement[2]["range"], 0)
        self.assertEqual(self.world.counts["white_bed"], 1)
        bed = next(row for row in harness.daily_goals.build(harness.last_observation)["tasks"] if row["id"] == "bed")
        self.assertEqual(bed["next_action"]["name"], "place_bed")
        foot = bed["next_action"]["args"]["pos"]
        placed = self.step(harness, Decision(**bed["next_action"]))
        self.assertTrue(placed.success, placed)
        self.assertEqual(placed.data["facing"], "south")
        self.assertEqual(placed.data["second_pos"], [foot[0], foot[1], foot[2] + 1])
        self.assertEqual(self.world.counts["white_bed"], 0)
        self.assertEqual(self.world.blocks[tuple(foot)]["properties"]["part"], "foot")
        self.assertEqual(self.world.blocks[tuple(placed.data["second_pos"])]["properties"]["part"], "head")
        bed = next(row for row in harness.daily_goals.build(harness.last_observation)["tasks"] if row["id"] == "bed")
        self.assertEqual(bed["status"], "satisfied")
        self.assertEqual(self.world.get_block([0, 64, 0])["result"]["name"], "air")
        self.assertEqual(self.world.get_block([0, 64, -1])["result"]["name"], "air")
        self.assertEqual(len([call for call in self.world.calls if call[0] == "place"]), 1)

    def test_uncertain_model_http_blocks_new_requests_but_local_eating_and_retreat_continue(self):
        harness, endpoint = self.make_harness([tool_response("wait", {"seconds": 1})], timeout=.05)
        endpoint.release.clear()
        result = self.step(harness)
        self.assertEqual(result.error_code, "llm_request_uncertain")
        self.assertFalse(harness.paused)
        self.assertEqual(harness.snapshot()["model_blocked"], "llm_request_uncertain")
        self.assertEqual(harness.planner.brain.request_gate.snapshot()["state"], "uncertain")
        self.world.status["food"] = 10
        self.world.counts["bread"] = 1
        result = self.step(harness)
        self.assertTrue(result.success, result)
        self.assertEqual(self.world.counts["bread"], 0)
        self.assertEqual(self.world.status["food"], 15)
        self.world.status["food"] = 20
        self.world.entities = [{"id": 8, "name": "zombie", "type": "hostile", "pos": [-3, 64, .5]}]
        before = copy.deepcopy(self.world.status["pos"])
        result = self.step(harness)
        self.assertTrue(result.success, result)
        self.assertNotEqual(self.world.status["pos"], before)
        self.assertEqual(len(endpoint.requests), 1)
        self.assertTrue(any(call[0] == "move" for call in self.world.calls))
        self.assertTrue(any(call[0] == "use_item" for call in self.world.calls))
        self.world.entities = []
        self.assertTrue(self.step(harness).success)  # Finish the already verified relay obligation.
        before_writes = len([c for c in self.world.calls if c[0] in {"move", "use_item", "place", "window_act"}])
        self.assertTrue(self.step(harness).data["waiting"])
        self.assertEqual(len(endpoint.requests), 1)
        self.assertEqual(len([c for c in self.world.calls if c[0] in {"move", "use_item", "place", "window_act"}]), before_writes)

    def test_nearby_enemy_preempts_pending_model_before_response_and_discards_old_action(self):
        self.world.counts = {"cobblestone": 1}
        harness, endpoint = self.make_harness([
            tool_response("place_item", {"item": "cobblestone", "pos": [2, 64, 0]}, "obsolete"),
            tool_response("wait", {"seconds": 1}, "current"),
        ], timeout=2)
        endpoint.release.clear()
        self.addCleanup(endpoint.release.set)  # Release before harness.close even if an assertion fails.
        self.assertTrue(harness._start())
        self.assertTrue(endpoint.entered.wait(1), "The fake provider did not receive its first request")
        self.world.entities = [{"id": 9, "name": "zombie", "type": "hostile", "pos": [-3, 64, .5]}]
        self.world.status["nearest_hostile"] = {"name": "zombie", "dist": 3.5}
        deadline = time.monotonic() + 1
        preempted = None
        while time.monotonic() < deadline and preempted is None:
            preempted = harness.tick(automatic=False)
            time.sleep(.001)
        self.assertIsNotNone(preempted, "Local monitoring remained blocked behind the provider")
        self.assertTrue(preempted.success, preempted)
        self.assertTrue(preempted.data["model_pending"])
        self.assertFalse(preempted.data["executed"])
        self.assertTrue(harness._model_worker.cancelled)
        self.assertFalse(harness.paused)
        before = list(self.world.status["pos"])
        retreat = self.step(harness)
        self.assertTrue(retreat.success, retreat)
        self.assertNotEqual(self.world.status["pos"], before)
        self.assertFalse(endpoint.release.is_set())
        self.assertEqual(len(endpoint.requests), 1)
        self.assertEqual(len(endpoint.responses), 2, "The provider must still be withholding the first response")
        self.assertTrue(harness.snapshot()["model_pending"])
        self.assertFalse([call for call in self.world.calls if call[0] == "place"])
        self.world.entities = []
        self.world.status["nearest_hostile"] = None
        self.assertTrue(self.step(harness).success)  # Complete the local retreat's fresh ground check.
        pending = self.step(harness)
        self.assertTrue(pending.data["model_pending"])
        self.assertEqual(len(endpoint.requests), 1)
        endpoint.release.set()
        deadline = time.monotonic() + 1
        while not harness._model_worker.done and time.monotonic() < deadline:
            time.sleep(.001)
        self.assertTrue(harness._model_worker.done)
        current = self.step(harness)
        self.assertTrue(current.success, current)
        self.assertTrue(current.data["waiting"])
        self.assertEqual(len(endpoint.requests), 2)
        self.assertEqual(self.world.counts["cobblestone"], 1)
        self.assertFalse([call for call in self.world.calls if call[0] == "place"])
        self.assertIsNone(harness.planner._pending)


if __name__ == "__main__":
    unittest.main()
