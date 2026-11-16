"""Offline competition lifecycle through real planner/worker/journal; no remote calls."""

import copy
import json
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from agent.llm_planner import LLMSurvivalPlanner
from agent.survival_planner import Decision
from agent.tools import tools_for_objective
from core.client import APIError, MinecraftAgentClient
from core.config import DEFAULTS, ConfigError, load_config
from core.survival_harness import SurvivalHarness
from main import main
from skills import SkillResult
from test_daily_survival import ready_world
from test_llm_planner import Brain, response

GOAL = "minecraft:story/upgrade_tools"


class CompetitionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = copy.deepcopy(DEFAULTS["survival"])
        self.settings["local_execution"]["enabled"] = False  # Exercise native model protocol explicitly.
        self.settings.update(objective="competition", state_dir=self.directory.name)
        self.settings["competition"]["advancement_poll_seconds"] = .001
        self.obs = ready_world()
        self.obs["status"].update(time_of_day=1000)
        self.client = Mock(username="OfflineContestBot", base_url="http://offline.invalid/api/v1", password="", token="test")
        self.client.get_status.side_effect = lambda **kwargs: {"ok": True, "result": copy.deepcopy(self.obs["status"])}
        self.completed = False
        self.client.advancements.side_effect = lambda **kwargs: self.advancement_envelope()
        self.collector = Mock()
        self.collector.collect.side_effect = lambda *args: copy.deepcopy(self.obs)
        self.skills = Mock()
        self.skills.execute.return_value = SkillResult(True, "verified", {"executed": True})

    def advancement_envelope(self):
        from core.competition_rules import scoring_catalog
        rows = [{"id": row["id"], "title": row["title"], "frame": "task",
                 "status": "completed" if self.completed and row["id"] == GOAL else "not_started",
                 "completed_at": None, "progress": {"criteria_done": int(self.completed and row["id"] == GOAL), "criteria_total": 1}}
                for row in scoring_catalog()]
        counts = {key: sum(row["status"] == key for row in rows)
                  for key in ("completed", "in_progress", "not_started")}
        return {"ok": True, "result": {"ready": True, "updated_at": 1794700000000, "packets_received": 1,
                "summary": {"total": len(rows), **counts}, "advancements": rows}}

    def harness(self, *messages):
        brain = Brain(*messages)
        planner = LLMSurvivalPlanner({"enabled": True, "max_repair_attempts": 0}, self.settings, brain=brain)
        runner = SurvivalHarness(self.client, self.settings, DEFAULTS["sleep"], planner=planner,
                                 skills=self.skills, collector=self.collector)
        self.addCleanup(runner.close)
        return runner, brain

    def step(self, runner):
        runner._next_attempt = 0
        self.assertTrue(runner.request_step())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = runner.tick(automatic=False)
            if result is not None:
                return result
            time.sleep(.002)
        self.fail("competition worker did not finish")

    def test_native_selection_progress_and_server_finish_are_checkpointed(self):
        runner, brain = self.harness(response("select_task", {"goal_id": GOAL}),
            response("update_progress", {"summary": "Prepare tools; completion still requires server evidence"}, call_id="p2"),
            response("finish_task", {"reason": "server reports completed"}, call_id="p3"),
            response("end_competition", {"reason": "All preset goals have been assessed"}, call_id="p4"))
        result = self.step(runner)
        self.assertTrue(result.success, result)
        self.assertEqual(runner.competition.snapshot()["active_goal_id"], GOAL)
        self.assertTrue(self.step(runner).success)
        self.completed = True
        runner._competition_next_advancement = 0
        result = self.step(runner)
        self.assertTrue(result.success, result)
        self.assertEqual(runner.competition.snapshot()["phase"], "selection")
        self.assertFalse(runner.finished, "finishing a goal returns to competition selection")
        self.assertEqual(runner.competition.snapshot()["score"]["raw"], 5)
        result = self.step(runner)  # Explicit session end is separate.
        self.assertTrue(result.success, result)
        self.assertTrue(runner.finished)
        self.assertTrue(runner.completed)
        self.assertEqual(runner.competition.snapshot()["phase"], "completed")
        self.skills.execute.assert_not_called()
        state = json.loads(runner.journal.path.read_text(encoding="utf-8"))
        self.assertEqual(state["context"]["competition"]["phase"], "completed")
        self.assertIsNone(state["pending"])
        messages = brain.requests[-1]["messages"]
        feedback = [json.loads(row["content"]) for row in messages if row["role"] == "tool"]
        self.assertTrue(feedback and feedback[-1]["result"]["success"])
        ctx = json.loads([row["content"] for row in messages if row["role"] == "user"][-1])["context"]["competition"]
        self.assertLessEqual(len(ctx["tasks"]), 7)
        self.assertTrue(all(set(row) == {"id", "title", "points", "availability"} for row in ctx["tasks"]))
        self.assertIsNone(ctx["active_goal"])
        active_context = json.loads(brain.requests[1]["messages"][-1]["content"])["context"]["competition"]
        self.assertEqual(active_context["tasks"], [])
        self.assertEqual(active_context["active_goal"]["id"], GOAL)
        self.assertIn("condition", active_context["active_goal"])
        self.assertTrue(active_context["active_goal"]["next_steps"])
        self.assertIn("selectable_goal_ids", ctx)
        self.assertEqual(ctx["catalog_assessment"]["total"], 120)
        self.assertTrue(ctx["catalog_assessment"]["complete"])
        restored, restored_brain = self.harness()
        self.assertTrue(restored.finished)
        self.assertTrue(restored.completed)
        self.assertIsNone(restored.tick(automatic=True))
        self.assertEqual(restored_brain.requests, [])
        self.skills.execute.assert_not_called()

    def test_night_preparation_allows_local_task_selection_and_progress_tools(self):
        self.obs["status"].update(is_day=False, time_of_day=14000)
        self.obs["blocks"] = [row for row in self.obs["blocks"] if not row["name"].endswith("_bed")]
        self.obs["daily_evidence"].pop("shelter", None)
        runner, brain = self.harness(response("select_task", {"goal_id": GOAL}),
            response("update_progress", {"summary": "夜间先做附近单步准备"}, call_id="night_card"))
        self.assertTrue(self.step(runner).success)
        self.assertTrue(self.step(runner).success)
        self.assertEqual(runner.competition.snapshot()["active_goal_id"], GOAL)
        current = json.loads(brain.requests[-1]["messages"][-1]["content"])
        limits = current["context"]["survival_constraints"]
        self.assertEqual(limits["scenario"], "night_preparation")
        self.assertIn("update_progress", limits["allowed_actions"])
        self.skills.execute.assert_not_called()

    def test_canopy_bound_recovery_is_available_before_competition_selection(self):
        self.obs["status"].update(is_day=False, time_of_day=14000)
        self.obs["blocks"] = [{"name": "birch_leaves", "pos": [1, 64, 0],
            "properties": {"waterlogged": False}, "can_see": True, "can_dig": True}]
        self.obs["daily_evidence"] = {}
        runner, brain = self.harness(response("force_clear_foliage", {}, "bound_clear"))
        runner.context["retreat_recovery"] = {"dimension": "overworld", "evacuating": True,
            "canopy_blocked": True, "evacuation_origin": [.5, 64, .5], "threat_pos": [-3, 64, .5],
            "excluded_until": {}, "retry_after": time.time() + 30,
            "blocked_cells": [{"name": "birch_leaves", "pos": [1, 64, 0]}],
            "search_basis": {"pos": [.5, 64, .5], "threat_pos": [-3, 64, .5]}}
        result = self.step(runner)
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["forced_attempt"])
        self.assertIsNone(runner.competition.snapshot()["active_goal_id"])
        self.assertEqual(runner.competition.snapshot()["score"]["raw"], 0)
        self.skills.execute.assert_called_once()
        self.assertEqual(self.skills.execute.call_args.args[:2],
                         ("clear_foliage", {"pos": [1, 64, 0], "expected_name": "birch_leaves"}))
        self.assertEqual({row["function"]["name"] for row in brain.requests[0]["tools"]},
                         {"force_clear_foliage", "report_blocked", "wait"})

    def test_production_requires_goal_but_existing_safety_is_not_bypassed(self):
        runner, _ = self.harness(response("craft_item", {"item": "stick"}))
        result = self.step(runner)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "llm_unavailable_tool")
        direct = runner._competition_tool(Decision("craft_item", {"item": "stick"}, "Cannot bypass selection", "tools"), self.obs)
        self.assertEqual(direct.error_code, "task_selection_required")
        self.skills.execute.assert_not_called()

    def test_repeated_blockage_reports_exit_task_without_awarding_points(self):
        runner, brain = self.harness(response("select_task", {"goal_id": GOAL}),
            *(response("report_blocked", {"reason": f"No route, attempt {index}", "resolution": "replan"},
                       call_id=f"blocked_{index}") for index in range(3)))
        self.assertTrue(self.step(runner).success)
        for _ in range(3):
            result = self.step(runner)
            self.assertTrue(result.success, result)
        self.assertTrue(result.data["task_exited"])
        self.assertEqual(runner.competition.snapshot()["phase"], "selection")
        self.assertEqual(runner.competition.snapshot()["score"]["raw"], 0)
        self.assertEqual(runner.context["last_blockage"]["attempts"], 3)
        self.assertEqual(runner.competition.select(GOAL)["error_code"], "goal_deferred")
        self.skills.execute.assert_not_called()

    def test_explicit_blockage_exit_returns_selection_and_preserves_failure_history(self):
        runner, _ = self.harness(response("select_task", {"goal_id": GOAL}),
            response("report_blocked", {"reason": "No useful nearby action", "resolution": "exit_task"}, call_id="exit"))
        self.assertTrue(self.step(runner).success)
        runner.context["failures"]["known-target"] = 2
        result = self.step(runner)
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["task_exited"])
        self.assertEqual(runner.context["failures"]["known-target"], 2)
        self.assertFalse(runner.finished)
        self.assertIsNone(runner.journal.state["pending"])
        self.skills.execute.assert_not_called()

    def test_force_exit_defers_goal_and_does_not_call_game_tools(self):
        runner, _ = self.harness(response("select_task", {"goal_id": GOAL}),
            response("force_exit_task", {"reason": "no usable path"}, call_id="exit"),
            response("select_task", {"goal_id": GOAL}, call_id="repeat"))
        result = self.step(runner)
        self.assertTrue(result.success, result)
        result = self.step(runner)
        self.assertTrue(result.success, result)
        result = self.step(runner)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "goal_deferred")
        self.skills.execute.assert_not_called()

    def test_server_read_failure_does_not_authorize_finish(self):
        runner, _ = self.harness(response("select_task", {"goal_id": GOAL}),
            response("finish_task", {"reason": "check unavailable"}, call_id="fail"))
        self.assertTrue(self.step(runner).success)
        self.client.advancements.side_effect = APIError("not_ready", "No tree")
        runner._competition_next_advancement = 0
        result = self.step(runner)
        self.assertFalse(result.success)
        self.assertEqual(runner.competition.snapshot()["phase"], "active")
        self.assertEqual(runner.competition.snapshot()["score"]["raw"], 0)

    def test_restart_restores_intent_but_never_replays_pending(self):
        runner, _ = self.harness(response("select_task", {"goal_id": GOAL}))
        self.assertTrue(self.step(runner).success)
        runner._checkpoint(context=runner.context, pending={"decision": {"name": "craft_item", "args": {"item": "stick"}}})
        runner.close()
        restored, brain = self.harness()
        self.assertTrue(restored.paused)
        self.assertEqual(restored.competition.snapshot()["active_goal_id"], GOAL)
        self.assertEqual(restored.snapshot()["pending"]["decision"]["name"], "craft_item")
        self.assertFalse(restored.competition.snapshot()["assessment_complete"])
        restored.tick(automatic=True)
        self.assertTrue(restored.paused)
        self.assertFalse(restored.busy)
        self.assertEqual(brain.requests, [])
        self.skills.execute.assert_not_called()

    def test_cached_advancement_evidence_expires_and_cannot_end_session(self):
        runner, _ = self.harness()
        runner._competition_advancements = self.advancement_envelope()
        runner._competition_advancements_fetched_at = time.time() - 6
        runner._competition_advancements_fetched_monotonic = time.monotonic() - 6
        runner._competition_next_advancement = time.monotonic() + 20
        runner._competition_context(self.obs)
        board = runner.competition.snapshot()
        self.assertTrue(board["advancements"]["stale"])
        self.assertFalse(board["catalog_assessment"]["complete"])
        result = runner.competition.end_competition("cached evidence expired")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "assessment_not_ready")

    def test_automatic_tick_uses_competition_budget_instead_of_daily_limit(self):
        runner, _ = self.harness()
        runner.steps = self.settings["max_steps"]
        with patch.object(runner, "_start", return_value=False) as start:
            runner.tick(automatic=True)
            self.assertFalse(runner.finished)
            start.assert_called_once_with()
        runner.steps = self.settings["competition"]["max_steps"]
        runner.tick(automatic=True)
        self.assertTrue(runner.finished)
        self.assertFalse(runner.completed)
        self.assertEqual(runner.pause_reason, "budget_exhausted")

    def test_dense_observation_and_feedback_fit_budget_with_staged_task_details(self):
        # Exercise the actual collector -> harness -> planner input boundary, not
        # a hand-built empty catalogue. This shape exceeded 48KB with all 125 rows.
        names = ["oak_log", "stone", "crafting_table", "wheat", "oak_leaves", "water"]
        self.obs["blocks"] = [{"name": name, "pos": [index, 64, group], "dist": float(index + group),
            "can_dig": True, "can_see": True, "properties": {"age": "7"},
            "visibility": {"visible": True, "reason": "clear", "queries": 8}}
            for group, name in enumerate(names) for index in range(8)]
        self.obs["terrain"] = [{"name": "grass_block", "pos": [index, 63, 0]} for index in range(16)]
        self.obs["entities"] = [{"id": index, "name": "sheep", "type": "passive", "pos": [index, 64, 10]}
                                for index in range(12)]
        self.obs["safe_stand_candidates"] = [{"pos": [index, 64, 0], "support": "stone"} for index in range(8)]
        self.obs["placement_candidates"] = [{"pos": [index, 64, 1], "support": "stone"} for index in range(8)]
        runner, brain = self.harness(response("select_task", {"goal_id": GOAL}),
            response("update_progress", {"summary": "准备石镐，按新鲜服务器状态核验"}, call_id="card"),
            *(response("report_blocked", {"reason": "No useful nearby action; reconsider the plan"}, call_id=f"wait_{index}")
              if index == 3 else response("wait", call_id=f"wait_{index}") for index in range(6)))
        # Include recalled Chinese evidence so the regression covers wire ASCII
        # escaping as well as the empty-memory opening used by earlier tests.
        recalled = {"places": [{"name": "crafting_table", "pos": [index, 64, 0],
            "source": "api_verified", "summary": "工作台位置需要重新观察后才能使用。" * 5}
            for index in range(4)]}
        self.assertLess(len(json.dumps(recalled, ensure_ascii=False).encode("utf-8")), 2500)
        runner.memory.recall = Mock(return_value=recalled)
        for _ in range(8):
            result = self.step(runner)
            self.assertTrue(result.success, result)
        self.assertEqual(len(runner.competition.snapshot()["tasks"]), 125)
        for index, request in enumerate(brain.requests):
            self.assertLessEqual(runner.planner._request_bytes(request["messages"], request["tools"]), 48000)
            current = json.loads([message for message in request["messages"] if message["role"] == "user"][-1]["content"])
            view = current["context"]["competition"]
            self.assertLessEqual(len(view["tasks"]), 7)
            self.assertNotIn("task_columns", view)
            if index:
                self.assertEqual(view["tasks"], [])
                self.assertEqual(view["active_goal"]["id"], GOAL)
                self.assertIn("completion_criteria", view["active_goal"])
                # The immediately previous tool result must survive history trimming.
                paired = [m for m in request["messages"] if m["role"] == "tool"]
                self.assertTrue(paired)
                self.assertEqual(paired[-1]["tool_call_id"], "call_1" if index == 1 else "card" if index == 2 else f"wait_{index-3}")
            else:
                self.assertIsNone(view["active_goal"])
            self.assertNotIn("minecraft:end/elytra", json.dumps(view))

    def test_task_tools_do_not_enter_starter_daily_catalogs(self):
        for objective in ("starter", "daily"):
            names = {row["function"]["name"] for row in tools_for_objective(objective)}
            self.assertNotIn("select_task", names)
        competition = {row["function"]["name"] for row in tools_for_objective("competition")}
        self.assertTrue({"select_task", "update_progress", "finish_task", "force_exit_task", "end_competition"} <= competition)
        self.assertNotIn("calculate_stronghold", competition)


class CompetitionClientTests(unittest.TestCase):
    @patch("core.client.requests.request")
    def test_advancement_wrapper_sends_all_visible_records_without_criteria(self, request):
        request.return_value.status_code = 200
        request.return_value.json.return_value = {"ok": True, "result": {"ready": True, "advancements": []}}
        client = MinecraftAgentClient()
        client.advancements(timeout_seconds=3)
        args, kwargs = request.call_args
        self.assertEqual(args[0], "POST")
        self.assertTrue(args[1].endswith("/advancements"))
        self.assertEqual(kwargs["json"], {"status": "all", "include_recipes": False, "include_criteria": False})
        self.assertEqual(kwargs["timeout"], 3)

    @patch("core.client.requests.request")
    def test_malformed_ready_is_not_empty_success(self, request):
        request.return_value.status_code = 200
        request.return_value.json.return_value = {"ok": True, "result": {"advancements": []}}
        with self.assertRaises(APIError):
            MinecraftAgentClient().advancements()


class CompetitionConfigTests(unittest.TestCase):
    def test_config_requires_valid_conditions_and_bounded_clock(self):
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            path = __import__("pathlib").Path(directory) / "config.yaml"
            for competition in ({"max_seconds": 28801}, {"eligibility_overrides": {"minecraft:story/root": True}},
                                {"advancement_poll_seconds": 0}):
                path.write_text(yaml.safe_dump({"survival": {"competition": competition}}), encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(path, environ={})

    def test_main_check_config_cannot_advertise_competition_without_model(self):
        with patch("main.load_config", return_value=copy.deepcopy(DEFAULTS)), patch("sys.stderr"):
            self.assertEqual(main(["--mode", "survival", "--objective", "competition", "--check-config"]), 1)

    def test_competition_disables_chat_debug_even_when_allowlisted(self):
        config = copy.deepcopy(DEFAULTS)
        config["agent"]["mode"] = "survival"
        config["llm"]["enabled"] = True
        config["debug"].update(enable_in_game_commands=True, allowed_usernames=["Tester"])
        with patch("main.load_config", return_value=config), patch("sys.stderr"):
            self.assertEqual(main(["--objective", "competition", "--check-config"]), 1)


if __name__ == "__main__":
    unittest.main()
