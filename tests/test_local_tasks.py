"""Offline contracts for deterministic planning; no game/model/RCON calls."""

import copy
import unittest
from unittest.mock import patch

from agent.daily_survival import DailySurvivalGoals
from core.competition_rules import scoring_catalog
from core.competition_tasks import CompetitionTasks, _MAPPED
from core.local_tasks import LocalTaskPlanner


MINE = "minecraft:story/mine_stone"
UPGRADE = "minecraft:story/upgrade_tools"
SMELT = "minecraft:story/smelt_iron"
TOOLS = "minecraft:story/iron_tools"
ARMOR = "minecraft:story/obtain_armor"
SLEEP = "minecraft:adventure/sleep_in_bed"
PLANT = "minecraft:husbandry/plant_seed"


def block(name, pos, **properties):
    return {"name": name, "pos": list(pos), "fresh": True, "can_see": True,
            "can_dig": True, "properties": properties}


def observation(counts=None, blocks=None, **status):
    return {"fresh": True, "observation_scope": "full", "status": {
        "ready": True, "alive": True, "busy": False, "on_ground": True,
        "pos": [0.5, 64, 0.5], "health": 20, "food": 20, "gamemode": "survival",
        "dimension": "overworld", "is_day": True, "thundering": False, **status},
        "counts": dict(counts or {}), "inventory": [], "entities": [],
        "blocks": blocks if blocks is not None else [block("oak_log", [2, 64, 0])],
        "furnaces": [], "terrain": []}


def active(goal=MINE, **changes):
    board = {"phase": "active", "stale": False, "remaining_seconds": 600,
             "active_goal_id": goal, "active_goal": {"id": goal, "eligibility": "scored",
                 "availability": "needs_prerequisites", "completion": "not_completed"},
             "assessment_complete": True, "advancements": {"ready": True, "stale": False}}
    board.update(changes)
    return board


def furnace(pos=(2, 64, 0), **changes):
    result = {"pos": list(pos), "name": "furnace", "fresh": True, "verified": True,
              "dimension": "overworld", "burning": False,
              "input": {"name": None, "count": 0}, "fuel": {"name": None, "count": 0},
              "output": {"name": None, "count": 0}}
    result.update(changes)
    return result


def task_board(name, args, goal="wood_reserve", **changes):
    result = {"fresh": True, "active_task_id": goal, "tasks": [{"id": goal, "status": "ready",
              "next_action": {"name": name, "args": args, "goal": goal, "reason": "Observed next step"}}]}
    result.update(changes)
    return result


def night_context(allowed=None):
    return {"survival_constraints": {"scenario": "night_preparation", "max_action_distance": 3,
        "allowed_actions": ["gather_block", "craft_item", "place_item", "select_task", "finish_task"]
        if allowed is None else allowed}}


class LocalTaskPlannerTests(unittest.TestCase):
    def setUp(self):
        self.planner = LocalTaskPlanner()

    def competition(self, obs=None, goal=MINE, context=None, board=None):
        return self.planner.choose(observation() if obs is None else obs, context or {},
                                   competition=active(goal) if board is None else board)

    def test_empty_backpack_bootstraps_one_observed_log(self):
        for objective in ("starter", "daily", "competition"):
            with self.subTest(objective=objective):
                planner = LocalTaskPlanner({"objective": objective, "local_execution": {"enabled": False}})
                decision = planner.choose(observation(), {}, competition=active() if objective == "competition" else None)
                self.assertEqual(decision.name, "gather_block")
                self.assertEqual(decision.args, {"pos": [2, 64, 0], "expected_name": "oak_log"})

    def test_existing_log_is_crafted_before_searching(self):
        decision = self.competition(observation({"oak_log": 1}))
        self.assertEqual((decision.name, decision.args), ("craft_item", {"item": "oak_planks", "count": 4}))

    def test_upgrade_reuses_craft_goal_for_existing_materials(self):
        obs = observation({"cobblestone": 3, "stick": 2}, [block("crafting_table", [2, 64, 0])])
        decision = self.competition(obs, UPGRADE)
        self.assertEqual(decision.args, {"item": "stone_pickaxe", "count": 1})

    def test_mining_never_targets_support_or_player_column(self):
        obs = observation({"wooden_pickaxe": 1}, [block("stone", [0, 63, 0]), block("stone", [0, 66, 0]),
                          block("stone", [2, 64, 0])])
        self.assertEqual(self.competition(obs).args["pos"], [2, 64, 0])
        obs["blocks"] = obs["blocks"][:2]
        self.assertIsNone(self.competition(obs))

    def test_no_observed_route_and_exhausted_planner_handoff(self):
        for context in ({}, {"explore_index": 1000}):
            self.assertIsNone(self.planner.choose(observation(blocks=[]), context))
            self.assertIsNone(self.competition(observation(blocks=[]), context=context))

    def test_starter_finishes_locally_without_model_confirmation(self):
        obs = observation({"stone_pickaxe": 1, "stone_axe": 1, "stone_sword": 1, "furnace": 1})
        decision = self.planner.choose(obs, {})
        self.assertEqual((decision.name, decision.args), ("goal_complete", {}))
        self.assertIsNone(self.competition(obs, UPGRADE))

    def test_ready_selection_considers_all_candidates_and_preserves_ties(self):
        goals = [MINE, UPGRADE, SMELT, TOOLS, ARMOR, SLEEP, PLANT]
        board = {"phase": "selection", "stale": False, "selectable_goal_ids": goals,
                 "tasks": [{"id": goal, "availability": "ready" if goal == PLANT else "needs_prerequisites"}
                           for goal in goals]}
        self.assertEqual(self.competition(board=board).args["goal_id"], PLANT)
        board["tasks"][3]["availability"] = "ready"
        self.assertEqual(self.competition(board=board).args["goal_id"], TOOLS)

    def test_selection_rejects_hidden_unscored_and_unsupported(self):
        bad = "minecraft:story/enter_the_nether"
        board = {"phase": "selection", "stale": False, "selectable_goal_ids": [MINE, SMELT, bad],
                 "tasks": [{"id": UPGRADE, "availability": "ready"},
                           {"id": MINE, "availability": "ready", "eligibility": "conditional_pending"},
                           {"id": SMELT, "availability": "ready", "selectable": False},
                           {"id": bad, "availability": "ready"}]}
        self.assertIsNone(self.competition(board=board))
        board["tasks"].append({"id": PLANT, "availability": "needs_prerequisites"})
        board["selectable_goal_ids"].append(PLANT)
        self.assertEqual(self.competition(board=board).args["goal_id"], PLANT)

    def test_finish_requires_fresh_server_completion_and_assessment(self):
        board = active()
        board["active_goal"]["completion"] = "completed"
        self.assertEqual(self.competition(board=board).name, "finish_task")
        for change in ({"stale": True}, {"assessment_complete": False}, {"assessment_fresh": False},
                       {"advancements": {"ready": False, "stale": False}},
                       {"advancements": {"ready": True, "stale": True}}, {"advancements": {}},
                       {"phase": "completed"}, {"active_goal_id": UPGRADE}, {"remaining_seconds": 0}):
            with self.subTest(change=change):
                invalid = copy.deepcopy(board)
                invalid.update(change)
                self.assertIsNone(self.competition(board=invalid))
        board["active_goal"]["completion"] = "unknown"
        self.assertIsNone(self.competition(observation({"cobblestone": 3}), board=board))

    def test_only_seven_mapped_active_tasks_have_plans(self):
        self.assertEqual(len(_MAPPED), 7)
        self.assertIsNone(self.competition(goal="minecraft:story/enter_the_nether"))

    def test_unknown_or_partial_observation_cannot_plan(self):
        for change in ({"fresh": False}, {"fresh": None}, {"stale": True}, {"observation_scope": "safety"},
                       {"counts": None}, {"counts": {"oak_log": True}}, {"counts": {"oak_log": -1}}):
            with self.subTest(change=change):
                obs = observation()
                obs.update(change)
                self.assertIsNone(self.competition(obs))
        obs = observation()
        del obs["counts"]
        del obs["inventory"]
        self.assertIsNone(self.competition(obs))

    def test_inventory_snapshot_without_counts_is_supported(self):
        obs = observation()
        del obs["counts"]
        obs["inventory"] = [{"name": "oak_log", "count": 1}]
        self.assertEqual(self.competition(obs).args["item"], "oak_planks")

    def test_dead_busy_unsafe_and_unstable_defer_to_recovery(self):
        for change in ({"alive": False}, {"busy": "crafting"}, {"on_ground": False}, {"health": 0},
                       {"health": 4}, {"food": 4}, {"submerged": True}, {"on_fire": True},
                       {"gamemode": "creative"}, {"dimension": "the_nether"}):
            with self.subTest(change=change):
                self.assertIsNone(self.competition(observation(**change)))
        obs = observation()
        obs["entities"] = [{"type": "hostile", "name": "zombie", "pos": [2, 64, 0]}]
        self.assertIsNone(self.competition(obs))

    def test_smelt_inspects_then_loads_actual_empty_slots(self):
        obs = observation({"raw_iron": 12, "coal": 1}, [block("furnace", [2, 64, 0])])
        self.assertEqual(self.competition(obs, SMELT).name, "inspect_container")
        obs["furnaces"] = [furnace()]
        decision = self.competition(obs, SMELT)
        self.assertEqual(decision.name, "smelt_start")
        self.assertEqual(decision.args, {"pos": [2, 64, 0], "input_item": "raw_iron", "count": 8,
                                        "fuel_item": "coal", "fuel_count": 1})

    def test_smelt_missing_slot_or_burning_evidence_is_not_empty(self):
        obs = observation({"raw_iron": 1, "coal": 1}, [block("furnace", [2, 64, 0])])
        for key in ("input", "fuel", "output", "burning"):
            with self.subTest(key=key):
                checked = furnace()
                del checked[key]
                obs["furnaces"] = [checked]
                self.assertEqual(self.competition(obs, SMELT).name, "inspect_container")

    def test_burning_or_occupied_furnace_is_not_refilled_or_waited_forever(self):
        obs = observation({"raw_iron": 3, "coal": 1}, [block("furnace", [2, 64, 0])])
        for change in ({"burning": True}, {"input": {"name": "raw_iron", "count": 1}},
                       {"fuel": {"name": "coal", "count": 1}}, {"output": {"name": "charcoal", "count": 1}}):
            obs["furnaces"] = [furnace(**change)]
            with self.subTest(change=change):
                self.assertIsNone(self.competition(obs, SMELT))

    def test_pending_smelting_job_is_inspected_not_replayed(self):
        obs = observation(blocks=[block("furnace", [2, 64, 0])])
        context = {"smelting_jobs": [{"pos": [2, 64, 0], "dimension": "overworld", "expected_output": "iron_ingot"}]}
        self.assertEqual(self.competition(obs, SMELT, context).name, "inspect_container")
        obs["furnaces"] = [furnace(input={"name": "raw_iron", "count": 3}, burning=True)]
        self.assertIsNone(self.competition(obs, SMELT, context))

    def test_collect_only_current_matching_output_with_exact_count(self):
        obs = observation(blocks=[block("furnace", [2, 64, 0])])
        obs["furnaces"] = [furnace(output={"name": "iron_ingot", "count": 3})]
        decision = self.competition(obs, SMELT)
        self.assertEqual((decision.name, decision.args), ("smelt_collect", {"pos": [2, 64, 0], "item": "iron_ingot", "count": 3}))
        obs["furnaces"][0]["fresh"] = False
        self.assertIsNone(self.competition(obs, SMELT))

    def test_iron_pick_and_boots_use_held_materials_not_unrelated_daily_tool(self):
        for goal, item, count in ((TOOLS, "iron_pickaxe", 3), (ARMOR, "iron_boots", 4)):
            with self.subTest(goal=goal):
                obs = observation({"iron_ingot": count, "stick": 2}, [block("crafting_table", [2, 64, 0])])
                self.assertEqual(self.competition(obs, goal).args, {"item": item, "count": 1})
        obs["counts"]["iron_ingot"] = 3
        self.assertIsNone(self.competition(obs, ARMOR))

    def test_iron_tools_prepare_sticks_and_workbench_in_separate_steps(self):
        obs = observation({"iron_ingot": 3, "oak_planks": 6})
        self.assertEqual(self.competition(obs, TOOLS).args, {"item": "stick", "count": 4})
        obs["counts"]["stick"] = 2
        self.assertEqual(self.competition(obs, TOOLS).args, {"item": "crafting_table", "count": 1})

    def test_raw_iron_needs_safe_observed_ore_and_stone_tool(self):
        obs = observation({"stone_pickaxe": 1}, [block("iron_ore", [2, 64, 0])])
        self.assertEqual(self.competition(obs, SMELT).name, "gather_resource")
        obs["blocks"] = []
        self.assertIsNone(self.competition(obs, SMELT))

    def test_sleep_needs_current_complete_free_bed_and_sleep_window(self):
        bed = [block("white_bed", [2, 64, 0], part="foot", facing="south", occupied=False),
               block("white_bed", [2, 64, 1], part="head", facing="south", occupied=False)]
        self.assertIsNone(self.competition(observation(blocks=bed), SLEEP))
        obs = observation(blocks=bed, is_day=False)
        self.assertEqual(self.competition(obs, SLEEP).name, "auto_sleep")
        obs["blocks"].pop()
        self.assertIsNone(self.competition(obs, SLEEP))
        self.assertIsNone(self.competition(observation({"white_bed": 1}, [], is_day=False), SLEEP))

    def test_plant_uses_existing_seed_observed_soil_and_air(self):
        obs = observation({"wheat_seeds": 1}, [block("farmland", [2, 63, 0]), block("air", [2, 64, 0])])
        self.assertEqual(self.competition(obs, PLANT).args, {"pos": [2, 63, 0], "item": "wheat_seeds"})
        obs["blocks"].pop()
        self.assertIsNone(self.competition(obs, PLANT))
        obs["counts"] = {}
        self.assertIsNone(self.competition(obs, PLANT))

    def test_daily_taskboard_action_runs_before_a_model(self):
        obs = observation({"oak_log": 1})
        board = DailySurvivalGoals().build(obs)
        expected = next(row for row in board["tasks"] if row["id"] == board["active_task_id"])["next_action"]
        decision = self.planner.choose(obs, {}, daily_board=board)
        self.assertEqual((decision.name, decision.args), (expected["name"], expected["args"]))

    def test_daily_honors_allowed_actions_and_current_capabilities(self):
        board = task_board("craft_item", {"item": "oak_planks", "count": 4})
        obs = observation({"oak_log": 1})
        self.assertIsNone(self.planner.choose(obs, {"survival_constraints": {"allowed_actions": []}}, daily_board=board))
        self.assertIsNone(self.planner.choose(obs, {"capabilities": ["gather_block"]}, daily_board=board))
        decision = self.planner.choose(obs, {"survival_constraints": {"allowed_actions": ["craft_item"]}}, daily_board=board)
        self.assertEqual(decision.name, "craft_item")

    def test_daily_rejects_stale_inactive_wait_and_unregistered_actions(self):
        obs = observation({"oak_log": 1})
        for name, args in (("wait", {"seconds": 5}), ("force_attempt", {"action": "craft_item"}),
                           ("report_blocked", {"reason": "blocked"}), ("teleport", {"pos": [2, 64, 0]}),
                           ("craft_item", {"item": "oak_planks", "count": True}),
                           ("craft_item", {"item": "oak_planks", "count": 4, "unknown": 1})):
            with self.subTest(name=name, args=args):
                self.assertIsNone(self.planner.choose(obs, {}, daily_board=task_board(name, args)))
        board = task_board("craft_item", {"item": "oak_planks", "count": 4}, fresh=False)
        self.assertIsNone(self.planner.choose(obs, {}, daily_board=board))
        board["fresh"] = True
        board["tasks"][0]["status"] = "blocked"
        self.assertIsNone(self.planner.choose(obs, {}, daily_board=board))

    def test_night_log_batch_becomes_one_visible_nearby_log(self):
        obs = observation(is_day=False)
        board = task_board("gather_materials", {"resource": "logs", "count": 8, "radius": 8})
        decision = self.planner.choose(obs, night_context(), daily_board=board)
        self.assertEqual(decision.name, "gather_block")
        self.assertEqual(decision.args, {"pos": [2, 64, 0], "expected_name": "oak_log"})
        self.assertIsNone(self.planner.choose(obs, night_context(["craft_item"]), daily_board=board))

    def test_night_bootstrap_can_craft_but_never_batch_explore_or_mine_stone(self):
        context = night_context()
        decision = self.competition(observation({"oak_log": 1}, is_day=False), context=context)
        self.assertEqual(decision.args["item"], "oak_planks")
        self.assertEqual(self.competition(observation(is_day=False), context=context).name, "gather_block")
        self.assertIsNone(self.competition(observation({"wooden_pickaxe": 1}, [block("stone", [2, 64, 0])], is_day=False), context=context))
        for name, args in (("gather_materials", {"resource": "stone", "count": 2}),
                           ("build_shelter_batch", {"pos": [2, 64, 0], "material": "cobblestone"}),
                           ("move_to", {"pos": [2, 64, 0]})):
            with self.subTest(name=name):
                broad = night_context([name, "gather_block"])
                self.assertIsNone(self.planner.choose(observation(is_day=False), broad, daily_board=task_board(name, args)))

    def test_night_requires_policy_radius_visibility_and_full_health(self):
        for change in ({"health": 19}, {"food": 11}, {"thundering": True},
                       {"nearest_hostile": {"dist": 11}}):
            self.assertIsNone(self.competition(observation(is_day=False, **change), context=night_context()))
        self.assertIsNone(self.competition(observation(is_day=False)))
        for change in ({"pos": [4, 64, 0]}, {"can_see": False}, {"fresh": False}):
            obs = observation(is_day=False)
            obs["blocks"][0].update(change)
            self.assertIsNone(self.competition(obs, context=night_context()))

    def test_cooldown_and_distance_are_enforced_after_taskboard_selection(self):
        board = task_board("gather_block", {"pos": [2, 64, 0], "expected_name": "oak_log"})
        for context in ({"blocked_targets": ["2,64,0"]}, {"survival_constraints": {"max_action_distance": 1}}):
            self.assertIsNone(self.planner.choose(observation(), context, daily_board=board))

    def test_choose_is_repeatable_does_not_mutate_and_performs_no_io(self):
        obs = observation({"raw_iron": 3, "coal": 1}, [block("furnace", [2, 64, 0])])
        obs["furnaces"] = [furnace()]
        context, board = {"blocked_targets": []}, active(SMELT)
        daily = task_board("craft_item", {"item": "oak_planks", "count": 4})
        before = copy.deepcopy((obs, context, board, daily))
        with patch("builtins.open", side_effect=AssertionError("file I/O")), \
             patch("requests.sessions.Session.request", side_effect=AssertionError("HTTP")), \
             patch("socket.create_connection", side_effect=AssertionError("network")), \
             patch("subprocess.Popen", side_effect=AssertionError("process")), \
             patch("skills.survival.SurvivalSkills.execute", side_effect=AssertionError("skill execution")), \
             patch("skills.daily_survival.DailySurvivalSkills.execute", side_effect=AssertionError("skill execution")):
            planner = LocalTaskPlanner()
            first = planner.choose(obs, context, competition=board, daily_board=daily)
            second = planner.choose(obs, context, competition=board, daily_board=daily)
        self.assertEqual(first, second)
        self.assertEqual((obs, context, board, daily), before)
        first.args["pos"][0] = 99
        self.assertEqual((obs, context, board, daily), before)
        self.assertEqual(second.args["pos"], [2, 64, 0])

    def test_real_competition_model_view_selection_and_finish_contract(self):
        now = 1_700_000_000
        manager = CompetitionTasks(clock=lambda: now)
        catalog = scoring_catalog()
        def refresh(completed=False):
            rows = [{"id": row["id"], "title": row["title"], "frame": "task", "completed_at": None,
                     "progress": {"criteria_done": int(completed and row["id"] == MINE), "criteria_total": 1},
                     "status": "completed" if completed and row["id"] == MINE else "not_started"}
                    for row in catalog]
            manager.update(observation(), {"ok": True, "result": {"ready": True, "updated_at": now * 1000,
                "packets_received": 1, "summary": {"total": len(rows), "completed": int(completed),
                    "in_progress": 0, "not_started": len(rows) - int(completed)}, "advancements": rows}}, capabilities=set(self.planner.schemas["competition"]))
            return manager.model_view()
        board = refresh()
        decision = self.competition(board=board)
        self.assertEqual(decision.args, {"goal_id": MINE})
        self.assertTrue(manager.select(MINE)["success"])
        board = refresh(completed=True)
        decision = self.competition(board=board)
        self.assertEqual(decision.name, "finish_task")
        self.assertTrue(manager.finish(**decision.args)["success"])


if __name__ == "__main__":
    unittest.main()
