"""Real task harness/skills against an in-memory game, never a live service."""

import copy
import json
import math
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from agent.llm_planner import LLMSurvivalPlanner
from core.client import APIError
from core.config import DEFAULTS
from core.block_geometry import trace_block_visibility
from core.survival_harness import SurvivalHarness
from skills import SkillResult
from test_daily_runtime import RuntimeDailyWorld
from test_daily_skills import envelope
from test_llm_planner import Brain, response


class TaskWorld(RuntimeDailyWorld):
    """Small flat fake world with inventory-conserving wood/craft/furnace writes."""

    def __init__(self):
        super().__init__()
        self.fail_paths = set()
        self.unknown_digs = set()
        self.craft_no_progress = False
        self.dig_options = []
        self.natural_pickup = True

    def get_status(self, **kwargs):
        hostiles = [row for row in self.entities if row.get("type") == "hostile"]
        nearest = min(hostiles, key=lambda row: math.dist(row["pos"], self.status["pos"]), default=None)
        self.status["nearest_hostile"] = ({"id": nearest["id"], "name": nearest["name"],
            "dist": math.dist(nearest["pos"], self.status["pos"])} if nearest else None)
        return super().get_status(**kwargs)

    def get_block(self, pos, **kwargs):
        result = super().get_block(pos, **kwargs)
        result["result"].update(can_see=True, can_dig=math.dist(pos, self.status["pos"]) <= 4)
        return result

    def get_blocks(self, *args, **kwargs):
        rows = super().get_blocks(*args, **kwargs)
        for row in rows:
            row.update(can_see=True, can_dig=math.dist(row["pos"], self.status["pos"]) <= 4)
        return rows

    def move_to(self, x, y, z, **kwargs):
        if (x, y, z) in self.fail_paths:
            self.calls.append(("move_failed", [x, y, z]))
            raise APIError("stalled", "Fake path cannot reach this target")
        if self.blocks.get((x, y, z), {}).get("name", "air") != "air":
            # The game stops near a solid resource; never teleport inside it.
            target=[x,y,z]
            origin=self.status["pos"]
            distance=math.dist(origin,target)
            stop_range=min(float(kwargs.get("range",2)),distance)
            self.calls.append(("move",target,kwargs))
            self.status["pos"]=[v+(a-v)*stop_range/distance for a,v in zip(origin,target)]
            return envelope({"pos":self.status["pos"]})
        return super().move_to(x, y, z, **kwargs)

    def dig(self, pos, collect=True):
        name = self.blocks[tuple(pos)]["name"]
        if not name.endswith("_log") and name not in {"stone", "coal_ore"}:
            return super().dig(pos, collect=collect)
        self.calls.append(("dig", list(pos)))
        self.dig_options.append((list(pos), collect))
        item = {"stone": "cobblestone", "coal_ore": "coal"}.get(name, name)
        self.block(pos, "air")
        nearby_drop = math.hypot(self.status["pos"][0] - pos[0] - .5,
                                self.status["pos"][2] - pos[2] - .5) <= 1.25
        picked_up = collect or self.natural_pickup and nearby_drop
        if picked_up:
            self.counts[item] = self.counts.get(item, 0) + 1
        if tuple(pos) in self.unknown_digs:
            raise APIError("not_connected", "Write applied in fake world, acknowledgement lost")
        return envelope({"dug": name, "gained": {item: 1} if picked_up else {}})

    def craft(self, item, count=1):
        self.calls.append(("craft", item, count))
        if self.craft_no_progress:
            return envelope({"crafted": item})  # An acknowledgement cannot prove output.
        woods = ("oak", "birch", "spruce", "jungle", "acacia", "dark_oak", "mangrove", "cherry", "pale_oak")
        recipes = {**{wood + "_planks": ({wood + "_logs": 1}, 4) for wood in woods},
                   "stick": ({"planks": 2}, 4), "crafting_table": ({"planks": 4}, 1),
                   "wooden_pickaxe": ({"planks": 3, "stick": 2}, 1),
                   "stone_pickaxe": ({"cobblestone": 3, "stick": 2}, 1),
                   "stone_axe": ({"cobblestone": 3, "stick": 2}, 1),
                   "stone_sword": ({"cobblestone": 2, "stick": 1}, 1),
                   "furnace": ({"cobblestone": 8}, 1)}
        if item not in recipes:
            raise APIError("unsupported_recipe", "Recipe absent from this fake world")
        ingredients, produced = recipes[item]
        repetitions = math.ceil(count / produced)
        def alternatives(name):
            if name == "planks":
                return tuple(wood + "_planks" for wood in woods)
            if name.endswith("_logs"):
                wood = name[:-5]
                return (wood + "_log", wood + "_wood", "stripped_" + wood + "_log", "stripped_" + wood + "_wood")
            return (name,)
        consumed = {}
        for name, amount in ingredients.items():
            needed = amount * repetitions
            for actual in alternatives(name):
                used = min(needed, self.counts.get(actual, 0))
                if used:
                    consumed[actual] = used
                    needed -= used
            if needed:
                raise APIError("missing_ingredients", "Fake game requires real input items")
        if item not in {"stick", "crafting_table", *(wood + "_planks" for wood in woods)} and not any(row["name"] == "crafting_table"
                and math.dist(pos, self.status["pos"]) <= 4 for pos, row in self.blocks.items()):
            raise APIError("missing_workstation", "A placed nearby table is required")
        for name, amount in consumed.items():
            self.counts[name] -= amount
        self.counts[item] = self.counts.get(item, 0) + produced * repetitions
        return envelope({"crafted": item, "count": produced * repetitions})

    def window_act(self, pos, ops, **kwargs):
        result = super().window_act(pos, ops, **kwargs)
        if self.window_type == "furnace" and self.sections["input"] and self.sections["fuel"]:
            self.info["burning"] = True
        return result


class ForestWorld(TaskWorld):
    """Real ray geometry with stacked trunks and leaves; writes conserve items."""

    def get_block(self, pos, **kwargs):
        result = super().get_block(pos, **kwargs)
        sight = trace_block_visibility(self.status["pos"], list(pos),
            lambda point: RuntimeDailyWorld.get_block(self, point)["result"],
            max_queries=32, max_distance=8)
        result["result"]["can_see"] = sight["visible"]
        return result

    def dig(self, pos, collect=True):
        row = self.get_block(pos)["result"]
        if row["can_see"] is not True:
            raise APIError("target_occluded", "Fake tree geometry blocks this dig")
        if row["name"].endswith("_leaves"):
            self.calls.append(("clear", list(pos), collect))
            self.block(pos, "air")
            return envelope({"dug": row["name"]})
        return super().dig(pos, collect=collect)


class TaskHarnessTests(unittest.TestCase):
    def setUp(self):
        guard = patch.dict(os.environ, {"BOTSLEEP_RUN_LIVE_TESTS": "0"})
        guard.start(); self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.world = TaskWorld()
        self.settings = copy.deepcopy(DEFAULTS["survival"])
        self.settings.update(objective="daily", state_dir=self.temp.name, max_steps=80,
                             max_runtime_seconds=60, poll_interval_seconds=.001, retry_seconds=.001)

    def logs(self, n=4, *, far=False, mixed=True):
        positions = ([6, 64, 0], [7, 64, 2], [8, 64, -2], [9, 64, 0]) if far else (
            [2, 64, 0], [0, 64, 2], [-2, 64, 0], [0, 64, -2])
        names = ("oak_log", "birch_log", "spruce_log", "acacia_log") if mixed else ("oak_log",) * 4
        for pos, name in list(zip(positions, names))[:n]: self.world.block(pos, name)
        return list(positions[:n])

    def harness(self, *replies):
        brain = Brain(*replies)
        planner = LLMSurvivalPlanner({"enabled": True, "max_repair_attempts": 0}, self.settings, brain=brain)
        harness = SurvivalHarness(self.world, self.settings, DEFAULTS["sleep"], planner=planner)
        self.addCleanup(harness.close)
        return harness, brain

    def cycle(self, harness, *, accelerate=True):
        """Automatic scheduling; only shorten wall-clock waits, never resume/reset failures."""
        self.assertFalse(harness.busy)
        if accelerate: harness._next_attempt = 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = harness.tick(automatic=True)
            if result is not None: return result
            if harness.paused or harness.finished:
                self.fail(f"Cycle cannot start: {harness.pause_reason}; task={harness.snapshot()['task_execution']}")
            time.sleep(.001)
        self.fail(f"Fake task cycle did not finish: {harness.snapshot()}")

    def until(self, harness, predicate, *, limit=30):
        results=[]
        for _ in range(limit):
            if predicate(): return results
            results.append(self.cycle(harness))
        self.fail(f"Task did not reach expected state: {harness.snapshot()}; results={results[-3:]}")

    @staticmethod
    def feedback(planner):
        return [row for row in planner.history if row["role"] == "tool"]

    @staticmethod
    def events(harness):
        return [json.loads(row) for row in harness.journal.events_path.read_text(encoding="utf-8").splitlines()]

    def test_gather_four_mixed_logs_uses_one_model_request_and_one_final_pair(self):
        positions = self.logs()
        self.world.counts["oak_log"] = 2
        harness, brain = self.harness(response("run_survival_task", {"task": "gather", "resource": "logs", "count": 4}, "gather4"))
        self.assertTrue(self.cycle(harness).data["accepted"])
        self.assertEqual(self.feedback(harness.planner), [])
        self.until(harness, lambda: harness.snapshot()["task_execution"]["status"] == "completed")
        self.assertEqual(sum(n for name,n in self.world.counts.items() if name.endswith("_log")), 6)
        digs = [call[1] for call in self.world.calls if call[0] == "dig"]
        self.assertEqual(sorted(digs), sorted(positions))
        self.assertEqual(len(brain.requests), 1)
        tools = self.feedback(harness.planner)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["tool_call_id"], "gather4")
        result=json.loads(tools[0]["content"])["result"]
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["task_execution"]["progress"], {"current":6,"target":6,"unit":"logs"})
        self.assertEqual(harness.snapshot()["task_execution"]["steps"], 4)
        self.assertIsNone(harness.journal.state["pending"])

    def test_opt_in_local_opening_uses_existing_controller_without_model_feedback(self):
        self.settings["autonomy"]["local_opening"] = True
        self.world.counts.update(cobblestone=8, stick=6, wooden_pickaxe=1)
        self.world.block([2, 64, 0], "crafting_table")
        harness, brain = self.harness()
        self.assertTrue(self.cycle(harness).data["accepted"])
        self.assertEqual(harness.snapshot()["task_execution"]["task"], "starter_tools")
        self.until(harness, lambda: harness.snapshot()["task_execution"]["status"] == "completed")
        for name in ("stone_pickaxe", "stone_axe", "stone_sword"):
            self.assertGreaterEqual(self.world.counts.get(name, 0), 1)
        self.assertEqual(brain.requests, [])
        self.assertEqual(self.feedback(harness.planner), [])
        self.assertIsNone(harness.journal.state["pending"])

    def test_local_opening_does_not_bypass_model_gate_or_pending(self):
        self.settings["autonomy"]["local_opening"] = True
        self.world.counts.update(cobblestone=8, stick=6, wooden_pickaxe=1)
        self.world.block([2, 64, 0], "crafting_table")
        harness, brain = self.harness()
        obs = harness.collector.collect()
        harness._prepare_context(obs)
        self.assertIsNotNone(harness._local_opening(obs))
        brain.request_gate = Mock()
        brain.request_gate.snapshot.return_value = {"state": "uncertain"}
        self.assertIsNone(harness._local_opening(obs))
        brain.request_gate.snapshot.return_value = {"state": "idle"}
        harness._checkpoint(pending={"decision": {"name": "gather_block"}})
        self.assertIsNone(harness._local_opening(obs))

    def test_known_path_failure_switches_target_without_model_or_global_failure_reset(self):
        positions=self.logs(2, far=True)
        self.world.fail_paths.add(tuple(positions[0]))
        harness,brain=self.harness(response("run_survival_task", {"task":"gather","resource":"logs","count":1}))
        self.cycle(harness)
        harness.failures=self.settings["max_consecutive_failures"]-1
        failure=self.cycle(harness)
        self.assertEqual(failure.error_code,"stalled")
        self.assertFalse(harness.paused)
        self.assertEqual(harness.failures,self.settings["max_consecutive_failures"]-1)
        self.until(harness,lambda:harness.snapshot()["task_execution"]["status"]=="completed")
        self.assertEqual(len(brain.requests),1)
        self.assertEqual([call[1] for call in self.world.calls if call[0]=="dig"],[positions[1]])
        self.assertEqual(len([call for call in self.world.calls if call[0]=="move_failed"]),1)
        self.assertGreaterEqual(harness.snapshot()["task_execution"]["local_recoveries"],1)

    def test_no_progress_escalates_before_second_model_request(self):
        self.world.counts={"oak_log":2}
        self.world.craft_no_progress=True
        self.settings["autonomy"].update(max_no_progress_steps=2,max_local_failures=4)
        harness,brain=self.harness(response("run_survival_task",{"task":"craft","item":"oak_planks","count":4},"craft_goal"),
                                   response("wait",call_id="review"))
        self.cycle(harness)
        self.until(harness,lambda:harness.snapshot()["task_execution"]["status"]=="needs_ai")
        self.assertEqual(len(brain.requests),1)
        self.assertEqual(harness.snapshot()["task_execution"]["steps"],2)
        self.assertEqual(self.world.counts.get("oak_planks",0),0)
        self.cycle(harness)
        self.assertEqual(len(brain.requests),2)
        feedback=[r for r in brain.requests[-1]["messages"] if r["role"]=="tool"]
        self.assertEqual(feedback[0]["tool_call_id"],"craft_goal")
        self.assertFalse(json.loads(feedback[0]["content"])["result"]["success"])

    def test_food_emergency_preempts_and_then_resumes_same_goal_without_model(self):
        self.logs(2)
        self.world.counts["bread"]=1
        harness,brain=self.harness(response("run_survival_task",{"task":"gather","resource":"logs","count":2}))
        self.cycle(harness)
        task_id=harness.snapshot()["task_execution"]["task_id"]
        self.world.status["food"]=12
        self.assertTrue(self.cycle(harness).success)
        self.assertEqual(len([c for c in self.world.calls if c[0]=="use_item"]),1)
        self.assertEqual(self.world.status["food"],17)
        self.assertEqual(harness.snapshot()["task_execution"]["status"],"suspended")
        self.until(harness,lambda:harness.snapshot()["task_execution"]["status"]=="completed")
        self.assertEqual(harness.snapshot()["task_execution"]["task_id"],task_id)
        self.assertEqual(len(brain.requests),1)

    def test_near_hostile_preempts_then_fresh_safe_state_resumes_goal(self):
        self.logs(2)
        harness,brain=self.harness(response("run_survival_task",{"task":"gather","resource":"logs","count":2}))
        self.cycle(harness)
        task_id=harness.snapshot()["task_execution"]["task_id"]
        self.world.entities=[{"id":44,"name":"skeleton","type":"hostile","pos":[3.5,64,.5]}]
        result=self.cycle(harness)
        self.assertTrue(result.success,result)
        last_step=next(e for e in reversed(self.events(harness)) if e["event"]=="STEP_RESULT")
        self.assertEqual(last_step["data"]["decision"]["name"],"retreat_from")
        self.assertIn(harness.snapshot()["task_execution"]["status"],{"suspended","running"})
        self.assertEqual(len([c for c in self.world.calls if c[0]=="dig"]),0)
        self.assertTrue(any(e["event"]=="TASK_SUSPENDED" for e in self.events(harness)))
        self.world.entities=[]
        self.until(harness,lambda:harness.snapshot()["task_execution"]["status"]=="completed")
        self.assertEqual(harness.snapshot()["task_execution"]["task_id"],task_id)
        self.assertEqual(len(brain.requests),1)

    def test_unknown_write_preserves_child_pending_and_restart_resume_never_replays(self):
        positions=self.logs(2)
        self.world.unknown_digs.update(map(tuple,positions))
        harness,brain=self.harness(response("run_survival_task",{"task":"gather","resource":"logs","count":2}))
        self.cycle(harness)
        result=self.cycle(harness)
        self.assertEqual(result.error_code,"action_unconfirmed")
        self.assertTrue(harness.paused)
        self.assertEqual(harness.snapshot()["task_execution"]["status"],"unknown")
        pending=copy.deepcopy(harness.journal.state["pending"])
        self.assertEqual(pending["decision"]["name"],"gather_block")
        self.assertEqual(len(self.feedback(harness.planner)),1)
        harness.tick();harness.close()
        restarted,second=self.harness(response("wait"))
        restarted.tick()
        self.assertTrue(restarted.paused)
        self.assertEqual(restarted.journal.state["pending"],pending)
        self.assertTrue(restarted.resume())
        self.assertTrue(self.cycle(restarted).data["task_abandoned"])
        self.assertIsNone(restarted.journal.state["pending"])
        self.assertEqual(restarted.snapshot()["task_execution"]["status"],"cancelled")
        self.assertEqual(len(second.requests),0)
        self.assertEqual(len([c for c in self.world.calls if c[0]=="dig"]),1)
        self.assertEqual(len([c for c in self.world.calls if c[0]=="stop"]),0)

    def test_pause_and_restart_reobserve_goal_without_replaying_completed_child(self):
        self.logs(2)
        harness,brain=self.harness(response("run_survival_task",{"task":"gather","resource":"logs","count":2},"original"))
        self.cycle(harness);self.cycle(harness)
        first=[c[1] for c in self.world.calls if c[0]=="dig"]
        self.assertEqual(len(first),1)
        harness.pause("offline_test_pause")
        self.assertEqual(len(self.feedback(harness.planner)),1)
        harness.tick()
        self.assertEqual(len([c for c in self.world.calls if c[0]=="dig"]),1)
        harness.close()
        restarted,second=self.harness()
        self.assertTrue(restarted.resume())
        self.until(restarted,lambda:restarted.snapshot()["task_execution"]["status"]=="completed")
        digs=[tuple(c[1]) for c in self.world.calls if c[0]=="dig"]
        self.assertEqual(len(digs),len(set(digs)))
        self.assertEqual(len(digs),2)
        self.assertEqual(len(second.requests),0)

    def test_explicit_resume_with_incomplete_observation_keeps_unknown_pending_paused(self):
        positions=self.logs(2)
        self.world.unknown_digs.update(map(tuple,positions))
        harness,brain=self.harness(response("run_survival_task",{"task":"gather","resource":"logs","count":2}))
        self.cycle(harness)
        self.assertEqual(self.cycle(harness).error_code,"action_unconfirmed")
        pending=copy.deepcopy(harness.journal.state["pending"])
        complete=harness.collector.collect(None)
        for change in ({"inventory_fresh":False},{"counts":None}):
            with self.subTest(change=change):
                partial=copy.deepcopy(complete)
                partial.update(change,fresh=True,observation_scope="full")
                with patch.object(harness,"_observe",return_value=partial):
                    self.assertTrue(harness.resume())
                    self.assertFalse(self.cycle(harness).success)
                self.assertTrue(harness.paused)
                self.assertEqual(harness.snapshot()["task_execution"]["status"],"unknown")
                self.assertEqual(harness.journal.state["pending"],pending)
                self.assertEqual(len(brain.requests),1)
                self.assertEqual(len([c for c in self.world.calls if c[0]=="dig"]),1)
                self.assertEqual(len([c for c in self.world.calls if c[0]=="stop"]),0)
                self.assertEqual(len(self.feedback(harness.planner)),1)

    def test_craft_prerequisites_are_real_inventory_transitions_without_extra_model(self):
        self.logs(4,mixed=False)
        harness,brain=self.harness(response("run_survival_task",{"task":"craft","item":"wooden_pickaxe","count":1}))
        self.cycle(harness)
        self.until(harness,lambda:harness.snapshot()["task_execution"]["status"]=="completed")
        self.assertEqual(self.world.counts.get("wooden_pickaxe"),1)
        self.assertTrue(any(row["name"]=="crafting_table" for row in self.world.blocks.values()))
        crafted={c[1] for c in self.world.calls if c[0]=="craft"}
        self.assertTrue({"oak_planks","stick","crafting_table","wooden_pickaxe"}<=crafted)
        self.assertTrue(all(count>=0 for count in self.world.counts.values()))
        self.assertEqual(len(brain.requests),1)
        self.assertEqual(len(self.feedback(harness.planner)),1)

    def forest(self):
        self.world = ForestWorld()
        for height in (64, 65, 66):
            self.world.block([2, height, 0], "oak_log")
        self.world.block([1, 65, 0], "oak_leaves", properties={"waterlogged": False})
        self.world.block([-2, 64, 0], "birch_log")
        self.world.block([0, 64, 2], "spruce_log")

    def test_stacked_trunk_and_mixed_species_clear_observed_leaf_then_gather_five(self):
        self.forest()
        harness, brain = self.harness(response("run_survival_task",
            {"task": "gather", "resource": "logs", "count": 5}, "forest_logs"))
        harness.resume()
        results = self.until(harness, lambda: harness.snapshot()["task_execution"]["status"] == "completed")
        self.assertTrue(all(result.success for result in results), results)
        self.assertEqual({name: n for name, n in self.world.counts.items() if n},
                         {"oak_log": 3, "birch_log": 1, "spruce_log": 1})
        self.assertEqual([call for call in self.world.calls if call[0] == "clear"],
                         [("clear", [1, 65, 0], False)])
        self.assertEqual(len({tuple(call[1]) for call in self.world.calls if call[0] == "dig"}), 5)
        self.assertEqual(len(brain.requests), 1)
        self.assertEqual([row["tool_call_id"] for row in self.feedback(harness.planner)], ["forest_logs"])

    def test_one_model_goal_makes_pickaxe_from_empty_mixed_occluded_forest(self):
        self.forest()
        harness, brain = self.harness(response("run_survival_task",
            {"task": "craft", "item": "wooden_pickaxe", "count": 1}, "forest_tool"))
        harness.resume()
        results = self.until(harness, lambda: harness.snapshot()["task_execution"]["status"] == "completed")
        self.assertTrue(all(result.success for result in results), results)
        self.assertEqual(self.world.counts.get("wooden_pickaxe"), 1)
        self.assertTrue(any(row["name"] == "crafting_table" for row in self.world.blocks.values()))
        crafted = {call[1] for call in self.world.calls if call[0] == "craft"}
        self.assertTrue({"oak_planks", "birch_planks", "spruce_planks", "stick", "crafting_table", "wooden_pickaxe"} <= crafted)
        self.assertEqual(len([call for call in self.world.calls if call[0] == "dig"]), 3)
        self.assertEqual(len([call for call in self.world.calls if call[0] == "clear"]), 1)
        # Three actual logs yield twelve planks: 4 table + 2 sticks + 3 pick.
        self.assertEqual(sum(n for name, n in self.world.counts.items() if name.endswith("_planks")), 3)
        self.assertEqual(self.world.counts.get("stick"), 2)
        self.assertTrue(all(n >= 0 for n in self.world.counts.values()))
        self.assertEqual(len(brain.requests), 1)
        self.assertEqual([row["tool_call_id"] for row in self.feedback(harness.planner)], ["forest_tool"])

    def test_plank_goal_uses_output_increment_and_existing_stripped_species(self):
        self.world.counts = {"stripped_birch_log": 2, "birch_planks": 1}
        harness, brain = self.harness(response("run_survival_task",
            {"task": "craft", "item": "birch_planks", "count": 5}, "planks5"))
        harness.resume()
        self.until(harness, lambda: harness.snapshot()["task_execution"]["status"] == "completed")
        self.assertEqual(self.world.counts["birch_planks"], 9)
        self.assertEqual(self.world.counts["stripped_birch_log"], 0)
        self.assertEqual([call for call in self.world.calls if call[0] == "craft"],
                         [("craft", "birch_planks", 4), ("craft", "birch_planks", 1)])
        self.assertFalse(any(call[0] in {"dig", "move"} for call in self.world.calls))
        self.assertEqual(len(brain.requests), 1)
        self.assertEqual(len(self.feedback(harness.planner)),1)

    def test_workstation_prerequisite_path_failure_recovers_within_single_goal(self):
        positions = self.logs(far=True)
        self.world.counts = {"oak_planks": 3, "stick": 2}
        self.world.fail_paths.add(tuple(positions[0]))
        harness, brain = self.harness(response("run_survival_task",
            {"task": "craft", "item": "wooden_pickaxe", "count": 1}, "recover_craft"))
        harness.resume()
        results = self.until(harness, lambda: harness.snapshot()["task_execution"]["status"] == "completed")
        self.assertEqual([result.error_code for result in results if not result.success], ["stalled"])
        self.assertEqual(self.world.counts.get("wooden_pickaxe"), 1)
        self.assertEqual([call[1] for call in self.world.calls if call[0] == "move_failed"], [positions[0]])
        self.assertFalse(any(call[0] == "dig" and call[1] == positions[0] for call in self.world.calls))
        self.assertEqual(harness.snapshot()["task_execution"]["local_recoveries"], 1)
        self.assertEqual(len(brain.requests), 1)
        feedback = self.feedback(harness.planner)
        self.assertEqual([row["tool_call_id"] for row in feedback], ["recover_craft"])
        self.assertTrue(json.loads(feedback[0]["content"])["result"]["success"])

    def test_starter_tool_set_uses_one_model_goal_and_actual_mining_crafting(self):
        self.forest()
        for pos in ([3, 64, 2], [3, 64, -2], [-3, 64, 2], [-3, 64, -2],
                    [0, 64, 3], [0, 64, -3], [2, 64, -3], [-2, 64, -3]):
            self.world.block(pos, "stone")
        harness, brain = self.harness(response("run_survival_task", {"task": "starter_tools"}, "starter_set"))
        harness.resume()
        results = self.until(harness, lambda: harness.snapshot()["task_execution"]["status"] == "completed", limit=40)
        self.assertTrue(all(result.success for result in results), results)
        for tool in ("wooden_pickaxe", "stone_pickaxe", "stone_axe", "stone_sword"):
            self.assertEqual(self.world.counts.get(tool), 1, self.world.counts)
        self.assertEqual(self.world.counts.get("cobblestone"), 0)
        self.assertEqual(len([call for call in self.world.calls if call[0] == "dig"]), 11)
        self.assertEqual(len(brain.requests), 1)
        self.assertEqual([row["tool_call_id"] for row in self.feedback(harness.planner)], ["starter_set"])

    def test_low_health_bootstrap_crafts_from_near_trunk_without_implicit_pickup_path(self):
        self.world = ForestWorld()
        self.world.status.update(health=6, food=17)
        for height in (64, 65, 66):
            self.world.block([1, height, 0], "oak_log")
        harness, brain = self.harness(response("run_survival_task",
            {"task": "craft", "item": "wooden_pickaxe", "count": 1}, "cautious_tool"))
        harness.context["food_recovery"] = {"needs_ai": True, "steps": 8,
            "dimension": self.world.status["dimension"], "life": self.world.status.get("last_death"),
            "origin": list(self.world.status["pos"]), "visited": [], "excluded_until": {}}
        harness.resume()
        results = self.until(harness, lambda: harness.snapshot()["task_execution"]["status"] == "completed")
        self.assertTrue(all(result.success for result in results), results)
        self.assertEqual(self.world.counts.get("wooden_pickaxe"), 1)
        self.assertEqual(len(self.world.dig_options), 3)
        self.assertTrue(all(collect is False for _, collect in self.world.dig_options))
        self.assertFalse(any(call[0] in {"move", "move_failed", "clear"} for call in self.world.calls))
        self.assertEqual(self.world.status["health"], 6)
        self.assertEqual(self.world.status["food"], 17)
        self.assertEqual(len(brain.requests), 1)
        self.assertEqual([row["tool_call_id"] for row in self.feedback(harness.planner)], ["cautious_tool"])

    def test_low_health_bootstrap_without_inventory_pickup_returns_failure_not_fake_success(self):
        self.world.status.update(health=6, food=17)
        self.world.block([2, 64, 0], "oak_log")
        self.world.natural_pickup = False
        harness, brain = self.harness(response("run_survival_task",
            {"task": "gather", "resource": "logs", "count": 1}, "no_pickup"))
        harness.context["food_recovery"] = {"needs_ai": True, "steps": 8,
            "dimension": self.world.status["dimension"], "life": self.world.status.get("last_death"),
            "origin": list(self.world.status["pos"]), "visited": [], "excluded_until": {}}
        harness.resume()
        results = self.until(harness, lambda: harness.snapshot()["task_execution"]["status"] == "needs_ai")
        self.assertIn("gather_unverified", [result.error_code for result in results])
        self.assertEqual(self.world.blocks[(2, 64, 0)]["name"], "air")
        self.assertEqual(self.world.counts.get("oak_log", 0), 0)
        self.assertEqual(self.world.dig_options, [([2, 64, 0], False)])
        self.assertFalse(any(call[0] in {"move", "move_failed", "clear"} for call in self.world.calls))
        self.assertEqual(harness.snapshot()["task_execution"]["progress"]["current"], 0)
        self.assertEqual(len(brain.requests), 1)
        feedback = self.feedback(harness.planner)
        self.assertEqual([row["tool_call_id"] for row in feedback], ["no_pickup"])
        self.assertFalse(json.loads(feedback[0]["content"])["result"]["success"])

    def test_smelting_waits_for_actual_output_without_repeated_loading_or_model(self):
        self.world.furnace();self.world.counts={"raw_iron":3,"coal":1}
        harness,brain=self.harness(response("run_survival_task",{"task":"smelt","item":"iron_ingot","count":3}))
        self.cycle(harness)
        self.until(harness,lambda:bool(self.world.sections["input"]))
        self.assertEqual(self.world.counts.get("iron_ingot",0),0)
        for _ in range(3): self.cycle(harness)
        self.assertEqual(len(brain.requests),1)
        self.assertEqual(len(self.feedback(harness.planner)),0)
        self.assertEqual(len([c for c in self.world.calls if c[0]=="window_act"]),1)
        self.world.finish_smelting()
        self.until(harness,lambda:harness.snapshot()["task_execution"]["status"]=="completed")
        self.assertEqual(self.world.counts.get("iron_ingot"),3)
        self.assertEqual(len([c for c in self.world.calls if c[0]=="window_act"]),2)
        self.assertEqual(len(brain.requests),1)
        self.assertEqual(len(self.feedback(harness.planner)),1)

    def test_global_step_budget_counts_goal_acceptance_and_each_child(self):
        self.logs();self.settings["max_steps"]=2
        harness,brain=self.harness(response("run_survival_task",{"task":"gather","resource":"logs","count":4}))
        self.cycle(harness);self.cycle(harness);harness.tick()
        self.assertTrue(harness.finished)
        self.assertEqual(harness.pause_reason,"budget_exhausted")
        self.assertEqual(harness.steps,2)
        self.assertEqual(len([c for c in self.world.calls if c[0]=="dig"]),1)
        self.assertEqual(len(brain.requests),1)
        feedback=self.feedback(harness.planner)
        self.assertEqual(len(feedback),1)
        self.assertFalse(json.loads(feedback[0]["content"])["result"]["success"])
        self.assertEqual(harness.snapshot()["task_execution"]["status"],"needs_ai")
        self.assertEqual(harness.snapshot()["task_execution"]["reason"],"global_budget_exhausted")
        self.assertIsNone(harness.planner._pending)
        harness.close()
        self.assertEqual(len(self.feedback(harness.planner)),1)

    def test_global_time_budget_closes_task_feedback_before_any_extra_child(self):
        self.logs()
        harness,brain=self.harness(response("run_survival_task",{"task":"gather","resource":"logs","count":4},"timed_goal"))
        self.cycle(harness)
        harness._started-=self.settings["max_runtime_seconds"]+1
        harness.tick()
        self.assertTrue(harness.finished)
        self.assertEqual(harness.steps,1)
        self.assertEqual(harness.snapshot()["task_execution"]["status"],"needs_ai")
        self.assertEqual(len([c for c in self.world.calls if c[0]=="dig"]),0)
        self.assertEqual(len(brain.requests),1)
        feedback=self.feedback(harness.planner)
        self.assertEqual(len(feedback),1)
        self.assertEqual(feedback[0]["tool_call_id"],"timed_goal")
        self.assertFalse(json.loads(feedback[0]["content"])["result"]["success"])
        self.assertIsNone(harness.planner._pending)
        harness.close()
        self.assertEqual(len(self.feedback(harness.planner)),1)

    def test_request_step_budget_boundary_closes_pending_native_feedback_once(self):
        self.logs();self.settings["max_steps"]=2
        harness,brain=self.harness(response("run_survival_task",{"task":"gather","resource":"logs","count":4},"manual_budget"))
        self.cycle(harness);self.cycle(harness)
        self.assertEqual(self.feedback(harness.planner),[])
        self.assertIsNotNone(harness.planner._pending)
        self.assertFalse(harness.request_step())
        self.assertFalse(harness.busy)
        self.assertTrue(harness.finished)
        self.assertEqual(harness.steps,2)
        self.assertEqual(harness.snapshot()["task_execution"]["status"],"needs_ai")
        feedback=self.feedback(harness.planner)
        self.assertEqual(len(feedback),1)
        self.assertEqual(feedback[0]["tool_call_id"],"manual_budget")
        self.assertFalse(json.loads(feedback[0]["content"])["result"]["success"])
        self.assertIsNone(harness.planner._pending)
        self.assertFalse(harness.request_step())
        harness.tick();harness.close()
        self.assertEqual(len(self.feedback(harness.planner)),1)
        self.assertEqual(len(brain.requests),1)
        self.assertEqual(len([c for c in self.world.calls if c[0]=="dig"]),1)

    def test_budget_exhaustion_cannot_downgrade_unknown_or_clear_pending(self):
        positions=self.logs(2)
        self.world.unknown_digs.update(map(tuple,positions))
        self.settings["max_steps"]=2
        harness,brain=self.harness(response("run_survival_task",{"task":"gather","resource":"logs","count":2},"unknown_goal"))
        self.cycle(harness)
        self.assertEqual(self.cycle(harness).error_code,"action_unconfirmed")
        pending=copy.deepcopy(harness.journal.state["pending"])
        harness.tick()
        self.assertTrue(harness.finished)
        self.assertEqual(harness.snapshot()["task_execution"]["status"],"unknown")
        self.assertEqual(harness.journal.state["pending"],pending)
        self.assertEqual(len(brain.requests),1)
        self.assertEqual(len([c for c in self.world.calls if c[0]=="dig"]),1)
        self.assertEqual(len([c for c in self.world.calls if c[0]=="stop"]),0)
        feedback=self.feedback(harness.planner)
        self.assertEqual(len(feedback),1)
        self.assertEqual(json.loads(feedback[0]["content"])["result"]["error_code"],"action_unconfirmed")
        harness.close()
        self.assertEqual(harness.journal.state["pending"],pending)
        self.assertEqual(len(self.feedback(harness.planner)),1)

    def test_last_global_step_can_verify_goal_completed_before_budget_stop(self):
        self.logs(1)
        self.settings["max_steps"]=2
        harness,brain=self.harness(response("run_survival_task",{"task":"gather","resource":"logs","count":1},"exact_budget_goal"))
        self.cycle(harness)
        self.cycle(harness)
        self.assertEqual(harness.snapshot()["task_execution"]["status"],"completed")
        harness.tick(automatic=False)
        self.assertTrue(harness.finished)
        feedback=self.feedback(harness.planner)
        self.assertEqual(len(feedback),1)
        self.assertTrue(json.loads(feedback[0]["content"])["result"]["success"])
        self.assertEqual(len(brain.requests),1)

    def test_idle_death_limit_closes_active_goal_without_waiting_for_shutdown(self):
        self.logs()
        harness,brain=self.harness(response("run_survival_task",{"task":"gather","resource":"logs","count":4},"life_goal"))
        self.cycle(harness)
        harness.context["death_count"]=self.settings["max_deaths"]-1
        self.world.status["alive"]=False
        harness._next_attempt=time.monotonic()+10
        harness._next_idle_monitor=0
        harness.tick(automatic=False)
        self.assertTrue(harness.paused)
        self.assertEqual(harness.pause_reason,"death_limit")
        self.assertEqual(harness.snapshot()["task_execution"]["status"],"needs_ai")
        self.assertEqual(harness.snapshot()["task_execution"]["reason"],"death_limit")
        self.assertEqual(len(brain.requests),1)
        self.assertEqual(len(self.feedback(harness.planner)),1)
        self.assertIsNone(harness.planner._pending)
        self.assertEqual(len([c for c in self.world.calls if c[0]=="dig"]),0)

    def test_local_child_budget_returns_partial_summary_before_extra_child(self):
        self.logs();self.settings["autonomy"]["max_steps"]=2
        harness,brain=self.harness(response("run_survival_task",{"task":"gather","resource":"logs","count":4}))
        self.cycle(harness)
        self.until(harness,lambda:harness.snapshot()["task_execution"]["status"]=="needs_ai")
        self.assertEqual(harness.snapshot()["task_execution"]["steps"],2)
        self.assertEqual(harness.steps,4)
        self.assertEqual(len([c for c in self.world.calls if c[0]=="dig"]),2)
        self.assertEqual(len(brain.requests),1)
        feedback=self.feedback(harness.planner)
        self.assertEqual(len(feedback),1)
        self.assertFalse(json.loads(feedback[0]["content"])["result"]["success"])

    def test_death_without_marker_ends_old_inventory_goal_with_one_native_feedback(self):
        self.logs()
        self.world.counts["oak_log"] = 10
        harness, brain = self.harness(response("run_survival_task", {
            "task": "gather", "resource": "logs", "count": 2}, "old_life_goal"))
        self.cycle(harness)
        self.assertEqual(harness.snapshot()["task_execution"]["progress"]["target"], 12)
        self.world.status.update(alive=False, health=0, last_death=None)
        self.cycle(harness)
        self.assertEqual(harness.context["life_generation"], 1)
        self.world.counts.clear()
        self.world.status.update(alive=True, health=20, last_death=None)
        self.cycle(harness)
        self.assertEqual(harness.snapshot()["task_execution"]["status"], "needs_ai")
        self.assertEqual(harness.snapshot()["task_execution"]["reason"], "life_or_dimension_changed")
        self.assertFalse([call for call in self.world.calls if call[0] == "dig"])
        self.assertIsNone(harness.journal.state["pending"])
        self.assertEqual(len(brain.requests), 1)
        feedback = self.feedback(harness.planner)
        self.assertEqual(len(feedback), 1)
        self.assertEqual(feedback[0]["tool_call_id"], "old_life_goal")
        self.assertFalse(json.loads(feedback[0]["content"])["result"]["success"])
        harness._finish_task_feedback(harness.last_observation)
        self.assertEqual(len(self.feedback(harness.planner)), 1)

    def test_unknown_child_keeps_pending_and_feedback_after_unmarked_death(self):
        positions = self.logs(2)
        self.world.unknown_digs.update(map(tuple, positions))
        harness, brain = self.harness(response("run_survival_task", {
            "task": "gather", "resource": "logs", "count": 2}, "unknown_life_goal"))
        self.cycle(harness)
        self.cycle(harness)
        pending = copy.deepcopy(harness.journal.state["pending"])
        evidence = copy.deepcopy(harness.snapshot()["task_execution"]["recent_results"])
        self.world.status.update(alive=False, health=0, last_death=None)
        harness._update_lifecycle(self.world.status)
        self.world.status.update(alive=True, health=20, last_death=None)
        harness._update_lifecycle(self.world.status)
        self.assertEqual(harness.context["life_generation"], 1)
        self.assertIsNone(harness.task_controller.next_action(harness.last_observation, harness.context))
        self.assertFalse(harness.task_controller.confirm_completion(harness.last_observation, harness.context))
        harness._finish_task_feedback(harness.last_observation)
        self.assertEqual(harness.snapshot()["task_execution"]["status"], "unknown")
        self.assertEqual(harness.snapshot()["task_execution"]["recent_results"], evidence)
        self.assertEqual(harness.journal.state["pending"], pending)
        self.assertEqual(len(self.feedback(harness.planner)), 1)
        self.assertEqual(len(brain.requests), 1)
        self.assertEqual(len([call for call in self.world.calls if call[0] == "dig"]), 1)
        self.assertFalse([call for call in self.world.calls if call[0] == "stop"])

    def test_deep_mine_dispatches_mineshaft_skill_and_completes_diamond_goal(self):
        self.world.counts["iron_pickaxe"] = 2
        seen_targets = []

        def fake_mine(miner_self, *, targets=None, **kwargs):
            seen_targets.append(copy.deepcopy(targets))
            self.world.counts["diamond"] = self.world.counts.get("diamond", 0) + 2
            return SkillResult(True, "Mined 2 diamond", {
                "executed": True, "gained": {"diamond": 2}, "surface_returned": True})

        harness, brain = self.harness(response("run_survival_task", {
            "task": "gather", "resource": "diamond", "count": 2}, "diamond_goal"))
        with patch("skills.mining.MineShaftSkill.mine_targets_from_surface", new=fake_mine):
            self.cycle(harness)
            self.until(harness, lambda: harness.snapshot()["task_execution"]["status"] == "completed")
        self.assertEqual(len(seen_targets), 1)
        self.assertIn("diamond", seen_targets[0])
        feedback = self.feedback(harness.planner)
        self.assertEqual(len(feedback), 1)
        self.assertTrue(json.loads(feedback[0]["content"])["result"]["success"])
        self.assertEqual(len(brain.requests), 1)

    def test_deep_mine_unknown_action_state_stops_harness_without_retry(self):
        self.world.counts["iron_pickaxe"] = 2
        calls = []

        def fake_unknown(miner_self, *, targets=None, **kwargs):
            calls.append(copy.deepcopy(targets))
            return SkillResult(False, "Connection dropped mid-dig", {
                "executed": True, "action_state": "unknown"}, "action_unconfirmed")

        harness, _ = self.harness(response("run_survival_task", {
            "task": "gather", "resource": "diamond", "count": 1}, "diamond_unknown"))
        with patch("skills.mining.MineShaftSkill.mine_targets_from_surface", new=fake_unknown):
            self.cycle(harness)
            self.until(harness, lambda: harness.snapshot()["task_execution"]["status"] == "unknown")
        self.assertEqual(len(calls), 1)
        self.assertTrue(harness.paused)
        self.assertIsNotNone(harness.journal.state.get("pending"))

    def test_remote_log_waypoint_steers_toward_distant_tree(self):
        harness, _ = self.harness(response("wait", {"seconds": 1}))
        obs = {"status": {"pos": [0.5, 64.0, 0.5]}, "blocks": []}
        with patch.object(harness.client, "get_blocks", return_value=[{"name": "oak_log", "pos": [50, 64, 0]}]):
            wp = harness._remote_log_waypoint(obs)
        self.assertIsNotNone(wp)
        self.assertGreater(wp[0], 20.0)

    def test_discard_mining_clutter_drops_excess_junk_when_crowded(self):
        harness, _ = self.harness(response("wait", {"seconds": 1}))
        dropped = []
        harness.client.drop = lambda item, count=None: dropped.append((item, count))
        harness.client.counts = {"cobblestone": 640, "granite": 324}
        harness.client.inventory = lambda timeout_seconds=None: {
            "ok": True,
            "result": {
                "items": [{"name": "cobblestone", "count": 64, "slot": i} for i in range(10)]
                + [{"name": "granite", "count": 18, "slot": 10 + i} for i in range(18)]
            },
        }
        harness._discard_mining_clutter(status_pos=[0.5, 64.0, 0.5])
        self.assertIn(("granite", 324), dropped)
        self.assertIn(("cobblestone", 576), dropped)

    def test_scout_task_routes_to_surface_scout_and_reports_findings_to_llm(self):
        def fake_scout(scout_self, **kwargs):
            return SkillResult(True, "Scout found village", {
                "executed": True,
                "estimated_reach": 112.0,
                "early_stop_reason": "structure_or_village_found",
                "aborted_empty": False,
                "found_structures": [{"name": "chest", "pos": [60, 64, 0]}],
            })

        harness, brain = self.harness(response("run_survival_task", {"task": "scout"}, "scout_goal"))
        with patch("skills.scouting.SurfaceScoutSkill.execute", new=fake_scout):
            self.cycle(harness)
            self.until(harness, lambda: harness.snapshot()["task_execution"]["status"] == "completed")
        feedback = self.feedback(harness.planner)
        self.assertEqual(len(feedback), 1)
        payload = json.loads(feedback[0]["content"])
        self.assertTrue(payload["result"]["success"])
        self.assertEqual(payload["result"]["data"]["scout_report"]["found_structures"][0]["name"], "chest")
        self.assertEqual(len(brain.requests), 1)

    def test_nether_portal_task_routes_to_speedrun_skill_with_lava_search_and_completes(self):
        self.world.counts = {"water_bucket": 1, "flint_and_steel": 1, "cobblestone": 16, "iron_pickaxe": 1}
        calls = []

        def fake_build_portal(portal_self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return SkillResult(False, "No pool within 36 blocks", {"executed": False}, "no_portal_site_found")
            return SkillResult(True, "Portal ignited", {"executed": True, "portal_active": True, "structure_verified": True})

        harness, brain = self.harness(response("run_survival_task", {"task": "nether_portal"}, "portal_goal"))
        with patch("skills.portal_speedrun.NetherPortalSpeedrunSkill.build_portal", new=fake_build_portal), \
             patch.object(harness.client, "find_blocks", return_value=[{"name": "lava", "pos": [25, 63, 0]}]), \
             patch("skills.survival.SurvivalSkills.execute", return_value=SkillResult(True, "Moved", {"executed": True})):
            self.cycle(harness)
            self.until(harness, lambda: harness.snapshot()["task_execution"]["status"] == "completed")
        self.assertEqual(len(calls), 2)
        feedback = self.feedback(harness.planner)
        payload = json.loads(feedback[0]["content"])
        self.assertTrue(payload["result"]["success"])
        self.assertEqual(harness.snapshot()["task_execution"]["progress"]["current"], 1)
        self.assertEqual(len(brain.requests), 1)

    def test_cobblestone_deep_mine_passes_origin_none_to_auto_select_clean_site(self):
        self.world.counts = {"wooden_pickaxe": 1}
        calls = []

        def fake_build(mine_self, **kwargs):
            calls.append(kwargs)
            self.world.counts["cobblestone"] = 12
            return SkillResult(True, "Mined cobblestone", {"executed": True, "surface_entry": [4, 64, 0]})

        harness, brain = self.harness(response("run_survival_task", {"task": "gather", "resource": "cobblestone", "count": 8}, "cobble"))
        with patch("skills.mining.MineShaftSkill.build_mine_shaft", new=fake_build), \
             patch("skills.mining.MineShaftSkill._ensure_returned_to_surface", return_value={"on_surface": True, "pos": [4, 64, 0], "escaped": False}):
            self.cycle(harness)
            self.until(harness, lambda: harness.snapshot()["task_execution"]["status"] == "completed")
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0]["origin"])
        self.assertEqual(calls[0]["radius"], 24)
        self.assertEqual(len(brain.requests), 1)

    def test_diamond_deep_mine_skips_surface_caves_and_keeps_deep_subgraph(self):
        self.world.counts = {"iron_pickaxe": 2}
        calls = []

        def fake_mine(mine_self, **kwargs):
            calls.append(kwargs)
            self.world.counts["diamond"] = 3
            return SkillResult(True, "Mined diamond", {"executed": True, "stop_reason": "target_reached"})

        harness, _ = self.harness(response("run_survival_task", {"task": "gather", "resource": "diamond", "count": 3}, "diamond"))
        with patch("skills.mining.MineShaftSkill.mine_targets_from_surface", new=fake_mine):
            self.cycle(harness)
            self.until(harness, lambda: harness.snapshot()["task_execution"]["status"] == "completed")
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["prefer_caves_first"])
        self.assertTrue(calls[0]["cave_policy"]["allow_tall_cave_subgraph"])


if __name__=="__main__":
    unittest.main()
