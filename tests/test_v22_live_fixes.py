"""Offline regressions for all 6 user-reported live test issues and nether_bench_v22 fixes."""

import copy
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent.llm_planner import LLMSurvivalPlanner
from agent.survival_planner import Decision, SurvivalPlanner
from core.autonomous_tasks import AutonomousTaskController
from core.config import DEFAULTS
from core.daily_policy import DailySafetyPolicy
from core.intent_compiler import should_prefer_local_workstation
from core.loop_supervisor import LoopSupervisor
from core.memory import MemoryEngine
from core.safety_arbiter import SafetyArbiter
from core.starter_mining_task import StarterMiningTask
from skills import SkillResult
from skills.cave_explore import CaveExploreSkill
from skills.mining import MineShaftSkill
from skills.survival import SurvivalSkills
from test_autonomous_tasks import world as auto_world
from test_cave_explore import CaveSimWorld
from test_llm_planner import Brain, observation, response
from test_mining_workflow import WorkflowMineWorld


class _BurrowMineWorld(WorkflowMineWorld):
    """Adapter supporting both SurvivalSkills and pillar_up client calls."""

    def get_block(self, pos, y=None, z=None, *, timeout_seconds=None, **kwargs):
        return super().get_block(pos, y, z)

    def get_status(self, *, timeout_seconds=None, **kwargs):
        return super().get_status()

    def inventory(self, *, timeout_seconds=None, **kwargs):
        return super().inventory()

    def dig(self, pos, y=None, z=None, collect=False, **kwargs):
        if y is None and isinstance(pos, (list, tuple)):
            return super().dig(pos[0], pos[1], pos[2], collect=collect, **kwargs)
        return super().dig(pos, y, z, collect=collect, **kwargs)

    def place_block(self, *args, **kwargs):
        res = super().place_block(*args, **kwargs)
        if res.get("ok"):
            bx, bz = self.status["pos"][0], self.status["pos"][2]
            for action in reversed(self.actions):
                if action[0] == "place":
                    px, py, pz = action[1]
                    if abs(px + 0.5 - bx) <= 0.6 and abs(pz + 0.5 - bz) <= 0.6 and py >= int(self.status["pos"][1]):
                        self.status["pos"][1] = float(py + 1)
                    break
        return res

    def stop(self, *, timeout_seconds=None, **kwargs):
        return {"ok": True, "result": {"stopped": True}}


def _status(**overrides):
    base = {
        "username": "TestBot",
        "ready": True,
        "alive": True,
        "busy": None,
        "gamemode": "survival",
        "dimension": "overworld",
        "pos": [0.5, 61.0, 0.5],
        "health": 20.0,
        "food": 20,
        "oxygen": 20,
        "on_ground": True,
        "is_day": False,
        "time_of_day": 18000,
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


def _obs(**status_overrides):
    return {
        "status": _status(**status_overrides),
        "counts": {},
        "blocks": [],
        "entities": [],
        "fresh": True,
    }


def _last_tool_feedback(brain):
    msgs = brain.requests[-1]["messages"]
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    if tool_msgs:
        return str(tool_msgs[-1].get("content", ""))
    return json.dumps(msgs[-1])


class V22LiveFixesTests(unittest.TestCase):
    # ------------------------------------------------------------------
    # Issue 1: Reuse old mine shafts & starter staircases
    # ------------------------------------------------------------------
    def test_mine_targets_reuses_nearby_recorded_shaft_and_falls_back_when_unsafe(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = MemoryEngine(tmp)
            mem.append_episode(
                "mine_shaft",
                "previous shaft",
                pos=[4, 64, 4],
                details={"origin": [4, 64, 4], "orientation": "east", "stop_reason": "branch_done"},
            )
            world = WorkflowMineWorld(bot_pos=(0.5, 64.0, 0.5), surface_y=64)
            skill = MineShaftSkill(world, memory=mem)
            calls = []

            def fake_build(*, origin=None, orientation=None, **kwargs):
                calls.append((origin, orientation))
                if origin == [4, 64, 4]:
                    return SkillResult(
                        True,
                        "Reused",
                        data={
                            "origin": [4, 64, 4],
                            "orientation": "east",
                            "stop_reason": "target_reached",
                            "mining_progress": {"deep_trail": []},
                        },
                    )
                return SkillResult(False, "Unexpected fresh dig", error_code="internal_error")

            with patch.object(skill, "build_mine_shaft", side_effect=fake_build), patch.object(
                skill, "_ensure_returned_to_surface", return_value={"on_surface": True, "pos": [0.5, 64.0, 0.5]}
            ):
                res = skill.mine_targets_from_surface(
                    targets={"iron": {"count": 4, "min_tool": "stone_pickaxe"}},
                    prefer_caves_first=False,
                )

            self.assertTrue(res.success, res)
            self.assertEqual(calls, [([4, 64, 4], "east")])
            self.assertEqual(res.data.get("origin"), [4, 64, 4])
            self.assertEqual(res.data.get("orientation"), "east")

            # If saved shaft is unsafe, mine_targets_from_surface falls back to fresh plot search.
            calls.clear()

            def fake_build_fallback(*, origin=None, orientation=None, **kwargs):
                calls.append((origin, orientation))
                if origin == [4, 64, 4]:
                    return SkillResult(False, "Blocked", error_code="unsafe_site")
                return SkillResult(
                    True,
                    "Fresh",
                    data={
                        "origin": [12, 64, 0],
                        "orientation": "south",
                        "stop_reason": "target_reached",
                        "mining_progress": {"deep_trail": []},
                    },
                )

            with patch.object(skill, "build_mine_shaft", side_effect=fake_build_fallback), patch.object(
                skill, "_ensure_returned_to_surface", return_value={"on_surface": True, "pos": [0.5, 64.0, 0.5]}
            ):
                fallback_res = skill.mine_targets_from_surface(
                    targets={"iron": {"count": 4, "min_tool": "stone_pickaxe"}},
                    prefer_caves_first=False,
                )
            self.assertTrue(fallback_res.success, fallback_res)
            self.assertEqual(calls[0], ([4, 64, 4], "east"))
            self.assertIsNone(calls[-1][0])
            self.assertEqual(fallback_res.data.get("origin"), [12, 64, 0])

    def test_starter_mining_task_reuses_nearby_last_site(self):
        task = StarterMiningTask(
            saved={
                "last_site": {
                    "entry": [10, 64, 10],
                    "direction": "west",
                }
            }
        )
        obs = auto_world({"wooden_pickaxe": 1})
        obs["status"]["pos"] = [0.5, 64.0, 0.5]
        obs["local_geometry_origin"] = [0.5, 64.0, 0.5]
        action = task.survey(obs, {}, 4)
        self.assertIsNotNone(action)
        self.assertEqual(action.name, "step_starter_staircase")
        self.assertEqual(action.args["entry"], [10, 64, 10])
        self.assertEqual(action.args["direction"], "west")

    # ------------------------------------------------------------------
    # Issue 2: Stop duplicate crafting_table manufacturing
    # ------------------------------------------------------------------
    def test_duplicate_crafting_table_is_rejected_and_local_table_preferred(self):
        obs = observation({"crafting_table": 1, "oak_planks": 8})
        obs["blocks"] = []
        brain1 = Brain(
            response("craft_item", {"item": "crafting_table", "count": 1}, "dup_craft"),
            response("wait", {"seconds": 1}, "fallback_wait"),
        )
        planner = LLMSurvivalPlanner(
            {"enabled": True, "api_key": "k", "model": "m"},
            {"objective": "daily", "autonomy": {"enabled": False}},
            Mock(),
            brain1,
        )
        # Held crafting_table rejects atomic duplicate craft before accepting fallback wait.
        chosen = planner.choose(obs, {})
        self.assertEqual(chosen.name, "wait")
        self.assertIn("crafting_table", _last_tool_feedback(brain1))

        # Nearby placed crafting_table within 16 horizontal / 4 vertical blocks disables local duplication & blocks task crafting.
        nearby_obs = observation({"oak_planks": 8})
        nearby_obs["blocks"] = [{"name": "crafting_table", "pos": [12, 65, 0], "can_see": True}]
        self.assertFalse(should_prefer_local_workstation("crafting_table", nearby_obs))
        far_obs = observation({"oak_planks": 8})
        far_obs["blocks"] = [{"name": "crafting_table", "pos": [20, 64, 0], "can_see": True}]
        self.assertTrue(should_prefer_local_workstation("crafting_table", far_obs))

        brain2 = Brain(
            response("run_survival_task", {"task": "craft", "item": "crafting_table", "count": 1}, "dup_task"),
            response("wait", {"seconds": 1}, "fallback_wait_2"),
        )
        daily_planner = LLMSurvivalPlanner(
            {"enabled": True, "api_key": "k", "model": "m"},
            {"objective": "daily"},
            Mock(),
            brain2,
        )
        chosen2 = daily_planner.choose(nearby_obs, {})
        self.assertEqual(chosen2.name, "wait")
        self.assertIn("crafting_table", _last_tool_feedback(brain2))

    # ------------------------------------------------------------------
    # Issue 3: Stop endless placement retries without space
    # ------------------------------------------------------------------
    def test_placement_space_failure_cooldowns_and_foliage_clearing(self):
        sup = LoopSupervisor()
        ctx = {}
        obs = observation({"crafting_table": 1})
        place_bad = Decision("place_item", {"pos": [2, 64, 0], "item": "crafting_table"}, "Place", "craft")
        fail = SkillResult(False, "No free placement space", {"executed": False}, "no_place_candidate")
        sup.record_step_outcome(1.0, 100.0, place_bad, fail, obs, ctx)
        self.assertIsNone(sup.is_signature_blocked(place_bad, 101.0))
        sup.record_step_outcome(2.0, 102.0, place_bad, fail, obs, ctx)
        self.assertIsNotNone(sup.is_signature_blocked(place_bad, 103.0))

        # When placement_candidates is empty during crafting_table placement, controller clears adjacent foliage first.
        w_obs = auto_world({"crafting_table": 1, "cobblestone": 3, "stick": 2})
        w_obs["local_geometry_origin"] = list(w_obs["status"]["pos"])
        w_obs["placement_candidates"] = []
        w_obs["blocks"] = [{"name": "oak_leaves", "pos": [1, 64, 0], "can_see": True}]
        ctl = AutonomousTaskController({})
        ctl.start({"task": "craft", "item": "stone_pickaxe", "count": 1}, w_obs)
        step = ctl.next_action(w_obs)
        self.assertIsNotNone(step, ctl.snapshot())
        self.assertEqual(step.name, "clear_foliage")
        self.assertEqual(step.args["pos"], [1, 64, 0])

    # ------------------------------------------------------------------
    # Issue 4: Never hand-mine rock or ore without pickaxe
    # ------------------------------------------------------------------
    def test_barehanded_rock_or_ore_gathering_is_blocked(self):
        no_pick_obs = observation({"oak_log": 2})
        no_pick_obs["blocks"] = [{"name": "stone", "pos": [2, 64, 0], "can_see": True}]
        brain = Brain(
            response("gather_block", {"pos": [2, 64, 0], "expected_name": "stone"}, "hand_stone"),
            response("wait", {"seconds": 1}, "fallback_wait"),
        )
        planner = LLMSurvivalPlanner(
            {"enabled": True, "api_key": "k", "model": "m"},
            {"objective": "daily", "autonomy": {"enabled": False}},
            Mock(),
            brain,
        )
        chosen = planner.choose(no_pick_obs, {})
        self.assertEqual(chosen.name, "wait")
        self.assertIn("pickaxe", _last_tool_feedback(brain))

        # MineShaftSkill._dig_cell_safely aborts on barehanded rock even during excavation.
        mine_world = WorkflowMineWorld(bot_pos=(0.5, 64.0, 0.5), surface_y=64)
        mine_world.items.pop("stone_pickaxe", None)
        mine_world.status["held"] = {}
        mine_world.blocks[(1, 64, 0)] = {"name": "stone", "properties": {}}
        skill = MineShaftSkill(mine_world, memory=Mock())
        from skills.action_workflow import WorkflowContext, WorkflowGuard, WorkflowStepError
        from core.structure_spec import MINE_SHAFT_SPEC

        ctx = WorkflowContext(
            guard=WorkflowGuard(mine_world),
            spec=MINE_SHAFT_SPEC,
            origin=[0, 64, 0],
            orientation="south",
            extra={"require_pickaxe": True},
        )
        with self.assertRaises(WorkflowStepError) as dig_err:
            skill._dig_cell_safely(ctx, (1, 64, 0), relative=False)
        self.assertEqual(dig_err.exception.code, "missing_pickaxe")

    # ------------------------------------------------------------------
    # Issue 5: Finish opened ore veins instead of stopping mid-vein
    # ------------------------------------------------------------------
    def test_opened_ore_vein_finishes_connected_blocks_past_exact_quota(self):
        w = CaveSimWorld(bot_pos=(0.5, 60.0, 0.5))
        w.carve([-1, 0, 1], range(60, 63), range(0, 4), "air")
        for x in (-1, 0, 1):
            w.set_block(x, 59, 0, "stone")
            w.set_block(x, 59, 1, "stone")
        # Connected 3-block exposed iron_ore vein while quota only asks for 1.
        w.set_block(0, 60, 2, "iron_ore")
        w.set_block(0, 61, 2, "iron_ore")
        w.set_block(1, 61, 2, "iron_ore")

        res = CaveExploreSkill(w, memory=Mock()).explore_cave(
            entry_pos=[0, 60, 0],
            origin_air=[0, 60, 1],
            targets={"iron": {"count": 1, "min_tool": "stone_pickaxe"}},
            cave_policy={"explore_max_visited": 12, "explore_radius": 6},
        )
        self.assertTrue(res.success, res)
        self.assertEqual(w.blocks[(0, 60, 2)]["name"], "air")
        self.assertEqual(w.blocks[(0, 61, 2)]["name"], "air")
        self.assertEqual(w.blocks[(1, 61, 2)]["name"], "air")
        self.assertGreaterEqual(res.data["harvested"].get("raw_iron", 0), 3)

    # ------------------------------------------------------------------
    # Issue 6 & v22 fixes: Burrow pit loop + portal move range & goal
    # ------------------------------------------------------------------
    def test_sealed_night_burrow_ignores_surface_zombie_and_shield_reads_preempt_code(self):
        rules = SurvivalPlanner(copy.deepcopy(DEFAULTS["survival"]))
        arbiter = SafetyArbiter(rules, copy.deepcopy(DEFAULTS["survival"]), daily=True, llm_override_on_block=True)
        burrow_ctx = {
            "life_generation": 0,
            "night_burrow": {
                "burrow_feet": [0, 61, 0],
                "cap_pos": [0, 63, 0],
                "cap_item": "dirt",
                "dimension": "overworld",
                "generation": 0,
            },
        }
        zombie_above = _status(
            pos=[0.5, 61.0, 0.5],
            nearest_hostile={"id": 42, "type": "zombie", "dist": 2.6, "pos": [0.5, 64.0, 0.5]},
        )
        res = arbiter.evaluate_monitor(
            zombie_above,
            zombie_above,
            decision=None,
            planning=True,
            context=burrow_ctx,
        )
        self.assertFalse(res.danger, res)

        events = []
        sup = LoopSupervisor(record_fn=lambda l, e, d: events.append((e, d)))
        ctx = {}
        d = Decision("wait", {"seconds": 0.25}, "interrupted", "safety")
        preempted = SkillResult(False, "interrupted", {"executed": False, "preempt_code": "llm_preempted"}, None)
        for t in (10.0, 12.0, 14.0):
            sup.record_step_outcome(t, 100.0 + t, d, preempted, _obs(), ctx)
        self.assertTrue(sup.preemption_shield_active(15.0))
        self.assertTrue(any(e == "LOOP_PREEMPT_SHIELD_ACTIVE" for e, _ in events))

    def test_daytime_exit_burrow_triggers_and_exits_sealed_pit(self):
        policy = DailySafetyPolicy(copy.deepcopy(DEFAULTS["survival"]))
        ctx = {
            "life_generation": 0,
            "night_burrow": {
                "burrow_feet": [0, 61, 0],
                "cap_pos": [0, 63, 0],
                "cap_item": "dirt",
                "dimension": "overworld",
                "generation": 0,
            },
        }
        day_obs = auto_world({"dirt": 2}, pos=[0.5, 61.0, 0.5], is_day=True, time_of_day=1000)
        day_obs["blocks"] = [
            {"name": "dirt", "pos": [0, 63, 0]},
            {"name": "dirt", "pos": [1, 61, 0]},
        ]
        chosen = policy.choose(day_obs, Decision("wait", {}, "", "idle"), context=ctx)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.name, "exit_burrow")
        self.assertIsNone(policy.validate(chosen, day_obs, ctx))

        w = _BurrowMineWorld(bot_pos=(0.5, 61.0, 0.5), surface_y=64)
        w.blocks.clear()
        w.items = {"dirt": 4, "cobblestone": 4}
        w.status["held"] = {"name": "dirt", "count": 4}
        w.blocks[(0, 60, 0)] = {"name": "stone", "properties": {}}
        w.blocks[(0, 63, 0)] = {"name": "dirt", "properties": {}}
        w.blocks[(0, 64, 0)] = {"name": "oak_leaves", "properties": {}}
        for y in (61, 62, 63):
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                w.blocks[(dx, y, dz)] = {"name": "grass_block" if y == 63 else "dirt", "properties": {}}
        res = SurvivalSkills(w).execute("exit_burrow", {})
        self.assertTrue(res.success, res)
        self.assertNotEqual(w.blocks.get((0, 63, 0), {}).get("name"), "dirt")
        self.assertNotIn((0, 64, 0), w.blocks)
        self.assertGreaterEqual(w.status["pos"][1], 64.0)

    def test_nether_portal_water_approach_uses_distinct_goal_and_range_limit(self):
        obs = auto_world({"bucket": 1, "iron_ingot": 1, "flint": 1, "cobblestone": 16, "iron_pickaxe": 1})
        obs["blocks"] = [{"name": "water", "pos": [12, 63, 0], "can_see": True}]
        ctl = AutonomousTaskController({})
        ctl.start({"task": "nether_portal"}, obs)
        step = ctl.next_action(obs)
        self.assertIsNotNone(step)
        self.assertEqual(step.name, "move_to")
        self.assertEqual(step.goal, "nether_portal_approach")
        self.assertLessEqual(step.args["range"], 4)


if __name__ == "__main__":
    unittest.main()
