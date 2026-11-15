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
        self.assertGreater(len(ctx["tasks"]), 100, "all goals must survive prompt list limits")
        self.assertIn("selectable_goal_ids", ctx)
        self.assertEqual(ctx["catalog_assessment"]["total"], 120)
        self.assertTrue(ctx["catalog_assessment"]["complete"])
        restored, restored_brain = self.harness()
        self.assertTrue(restored.finished)
        self.assertTrue(restored.completed)
        self.assertIsNone(restored.tick(automatic=True))
        self.assertEqual(restored_brain.requests, [])
        self.skills.execute.assert_not_called()

    def test_production_requires_goal_but_existing_safety_is_not_bypassed(self):
        runner, _ = self.harness(response("craft_item", {"item": "stick"}))
        result = self.step(runner)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "task_selection_required")
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

    def test_full_catalogue_prompt_fits_competition_request_budget(self):
        from core.competition_rules import scoring_catalog
        from core.competition_tasks import CompetitionTasks
        planner = LLMSurvivalPlanner({"enabled": True}, self.settings, brain=Brain(response("wait")))
        manager = CompetitionTasks({"max_seconds": 28800})
        snapshot = manager.snapshot()
        context = {"competition": {"phase": snapshot["phase"], "active_goal_id": None,
            "progress_card": snapshot["progress_card"], "score": snapshot["score"],
            "remaining_seconds": snapshot["remaining_seconds"],
            "advancements": snapshot["advancements"],
            "selectable_goal_ids": [row["id"] for row in snapshot["tasks"] if row["selectable"]],
            "catalog_assessment": {key: snapshot["catalog_assessment"][key]
                                   for key in ("checked", "total", "known_count", "complete")},
            "task_columns": ["id", "title", "difficulty", "points", "eligibility", "availability", "completion"],
            "tasks": [[row.get(key) for key in ("id", "title", "difficulty", "points", "eligibility", "availability", "completion")]
                      for row in snapshot["tasks"]]}}
        compact = planner._compact_value(context)
        self.assertEqual(len(compact["competition"]["tasks"]), 125)
        message = {"role": "user", "content": json.dumps({"observation": self.obs,
            "progress": planner.progress(self.obs), "context": compact}, ensure_ascii=False)}
        self.assertLessEqual(planner._request_bytes([{"role": "system", "content": planner.system_prompt}, message]),
                             planner.MAX_REQUEST_BYTES)

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
