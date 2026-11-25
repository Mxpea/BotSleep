"""A placed shelter survives remote gathering, without remembered build rights."""

import copy
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from agent.survival_planner import Decision
from core.autonomous_tasks import AutonomousTaskController
from core.config import DEFAULTS
from core.daily_observation import DailyObservationCollector
from core.shelter_project import project_anchor, recover_project, verified_build_anchor
from core.survival_harness import SurvivalHarness
from core.survival_state import SurvivalJournal
from skills import SkillResult
from skills.shelter import ShelterSkills
from test_autonomous_tasks import apply_atomic, world
from test_shelter import ShelterClient


class ShelterProjectTests(unittest.TestCase):
    SCOPE = {"endpoint": "http://offline-project.invalid/api/v1", "username": "Builder", "world_id": "project-test"}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = ShelterClient()
        self.client.base_url, self.client.username = self.SCOPE["endpoint"], self.SCOPE["username"]
        self.client.status.update(busy=None, is_day=True, time_of_day=1000, thundering=False)
        self.client.items = {"oak_planks": 64, "dirt": 64}
        self.skills = ShelterSkills(self.client)
        self.args = {"pos": [0, 64, 0], "orientation": "east", "material": "dirt"}

    def build(self):
        decision = Decision("build_shelter_step", self.args, "Build checked shell", "shelter")
        for _ in range(3):
            result = self.skills.execute(decision.name, decision.args)
            if result.error_code == "needs_position":
                self.client.status["pos"] = list(result.data["stand_candidates"][0])
                continue
            self.assertTrue(result.success, result)
            self.assertEqual(result.data["used"], 1)
            return decision, result
        self.fail("No reachable fixture stance")

    def journal(self, **scope):
        return SurvivalJournal(self.temp.name, **{**self.SCOPE, **scope})

    def harness(self, journal=None):
        settings = {**copy.deepcopy(DEFAULTS["survival"]), "objective": "daily",
                    "world_id": self.SCOPE["world_id"], "state_dir": self.temp.name}
        result = SurvivalHarness(self.client, settings, copy.deepcopy(DEFAULTS["sleep"]), journal=journal)
        self.addCleanup(result.close)
        return result

    def anchor(self):
        dec, res = self.build()
        return verified_build_anchor(dec, res, self.SCOPE)

    def remote_goal(self, counts=None):
        anchor = self.anchor()
        anchor["missing_blocks_hint"] = 43
        obs = world({"oak_planks": 7} if counts is None else counts, pos=[20.5, 64, .5])
        obs["blocks"] = [{"name": "oak_log", "pos": [22, 64, z], "can_see": True} for z in range(12)]
        obs["local_geometry_origin"] = list(obs["status"]["pos"])
        obs["safe_stand_candidates"] = [{"pos": [18.5, 64, .5], "support": "grass_block"}]
        context = {"shelter_project": anchor}
        controller = AutonomousTaskController(scope=self.SCOPE)
        controller.start({"task": "shelter"}, obs, context)
        return controller, obs, context

    def test_real_placement_is_persisted_while_successful_survey_is_not_started(self):
        harness = self.harness()
        obs = world()
        plan = Decision("plan_shelter", self.args, "Survey", "shelter")
        result = self.skills.execute(plan.name, plan.args)
        harness._daily_result(plan, result, obs)
        self.assertNotIn("shelter_project", harness.context)
        dec, res = self.build()
        harness._daily_result(dec, res, obs)
        saved = self.journal().state["context"]["shelter_project"]
        self.assertEqual(saved["missing_blocks_hint"], 54)
        self.assertEqual(saved["orientation"], "east")
        self.assertTrue(saved["requires_revalidation"])
        harness.close()
        restarted = self.harness()
        self.assertEqual(restarted.context["shelter_project"], saved)

    def test_migration_uses_real_scoped_event_not_later_surveys_and_keeps_pending(self):
        journal = self.journal()
        dec, res = self.build()
        journal.record("STEP_RESULT", {"decision": asdict(dec), "result": asdict(res)})
        journal.record("STEP_RESULT", {"decision": {"name": "plan_shelter", "args": {"pos": [20, 64, 0]}},
                                       "result": {"success": True}})
        with journal.events_path.open("ab") as stream:
            stream.write(b'{"incomplete":')
        pending = {"decision": {"name": "gather_block", "args": {"pos": [21, 64, 0]}}}
        journal.update(context={"base_project": {"pos": [20, 64, 0], "dimension": "overworld"}}, pending=pending)
        before = journal.events_path.read_bytes()
        migrated = self.harness(journal)
        anchor = migrated.context["shelter_project"]
        self.assertEqual(anchor["pos"], [0, 64, 0])
        self.assertEqual(anchor["source"], "verified_build_event")
        self.assertEqual(journal.state["pending"], pending)
        self.assertTrue(migrated.paused)
        self.assertTrue(journal.events_path.read_bytes().startswith(before))
        self.assertIn(b"SHELTER_PROJECT_RECOVERED", journal.events_path.read_bytes())
        self.assertIsNone(recover_project(self.journal(world_id="another-world")))

    def test_ack_only_unknown_failed_or_wrong_blueprint_cannot_anchor(self):
        dec, res = self.build()
        for change in ({"executed": False}, {"step_complete": False}, {"used": True}, {"used": 0},
                       {"placed_pos": [0, 64, 0]}, {"placed": "stone"}, {"orientation": "west"},
                       {"fresh": False}, {"action_state": "unknown"}, {"missing_blocks": True},
                       {"hazards": [[1, 64, 0]]}, {"geometry": {"floor_verified": False}}, {"door": []}):
            with self.subTest(change=change):
                candidate = SkillResult(True, "Changed evidence", {**copy.deepcopy(res.data), **change})
                self.assertIsNone(verified_build_anchor(dec, candidate, self.SCOPE))
        self.assertIsNone(verified_build_anchor(dec, SkillResult(False, "Failed", res.data), self.SCOPE))
        self.assertIsNone(verified_build_anchor(dec, SkillResult(True, "Unknown", res.data, "action_unconfirmed"), self.SCOPE))

    def test_remote_stockpile_finishes_then_returns_without_new_site_or_ai(self):
        controller, obs, context = self.remote_goal()
        for _ in range(12):
            action = controller.next_action(obs, context)
            self.assertEqual(action.name, "gather_block", action)
            controller.record_result(action, apply_atomic(obs, action), obs, context)
        self.assertEqual(obs["counts"]["oak_log"], 12)
        move = controller.next_action(obs, context)
        self.assertEqual((move.name, move.goal), ("position_for_task", "shelter_return"))
        self.assertEqual(move.args["pos"], [18.5, 64, .5])
        controller.record_result(move, apply_atomic(obs, move), obs, context)
        self.assertEqual(controller.state()["shelter_return_steps"], 1)
        self.assertEqual(controller.state()["shelter_return_visited"], [[20.5, 64, .5], [18.5, 64, .5]])
        self.assertEqual(controller.snapshot()["ai_escalations"], 0)
        self.assertEqual(obs["daily_evidence"], {})

    def test_missing_fresh_return_cannot_fall_back_to_unrelated_survey(self):
        controller, obs, context = self.remote_goal({"oak_planks": 64, "oak_door": 1})
        obs["safe_stand_candidates"] = []
        obs["placement_candidates"] = [{"pos": [20, 64, 0], "fresh": True}]
        self.assertIsNone(controller.next_action(obs, context))
        self.assertEqual(controller.snapshot()["status"], "needs_ai")
        self.assertIn("shelter_project", controller.snapshot()["last_escalation_reason"])

    def test_return_budget_pending_death_and_stale_guards_remain(self):
        for changed in ("budget", "pending", "stale", "death"):
            with self.subTest(changed=changed):
                controller, obs, context = self.remote_goal({"oak_planks": 64, "oak_door": 1})
                if changed == "budget":
                    controller._data["shelter_return_steps"] = 48
                elif changed == "pending":
                    context["pending"] = {"decision": "unknown"}
                elif changed == "stale":
                    obs["fresh"] = False
                else:
                    context["life_generation"] = 1
                self.assertIsNone(controller.next_action(obs, context))

    def test_collector_revisits_old_project_and_requires_fresh_geometry_after_return(self):
        anchor = self.anchor()
        collector = DailyObservationCollector(self.client)
        collector.set_context({"shelter_project": anchor,
            "base_project": {"pos": [20, 64, 0], "dimension": "overworld"}})
        self.client.status["pos"] = [20.5, 64, .5]
        remote = {"status": copy.deepcopy(self.client.status), "daily_evidence": {}}
        collector._home(remote, None)
        self.assertEqual(remote["daily_evidence"], {})
        self.client.status["pos"] = [.5, 64, .5]
        local = {"status": copy.deepcopy(self.client.status), "daily_evidence": {}}
        collector._home(local, None)
        self.assertEqual(local["daily_evidence"]["base_site"]["pos"], [0, 64, 0])
        self.assertEqual(local["shelter"]["missing_blocks"], 54)
        self.client.blocks[(1, 64, 0)] = {"name": "grass_block", "properties": {}}
        obstructed = {"status": copy.deepcopy(self.client.status), "daily_evidence": {}}
        collector._home(obstructed, None)
        self.assertNotIn("base_site", obstructed["daily_evidence"])
        self.assertEqual(len(self.client.actions), 1)

    def test_harness_rejects_replacement_before_pending_or_game_call(self):
        harness = self.harness()
        harness.context["shelter_project"] = self.anchor()
        original = copy.deepcopy(harness.context["shelter_project"])
        harness.task_controller.start({"task": "shelter"}, world(), harness.context)
        for name in ("plan_shelter", "prepare_shelter_step", "build_shelter_step"):
            decision = Decision(name, {**self.args, "pos": [20, 64, 0]}, "Try other site", "shelter")
            def choose(_):
                harness._task_child = True
                return decision
            with patch.object(harness, "_observe", return_value=world()), \
                    patch.object(harness, "_task_next", side_effect=choose), \
                    patch.object(harness, "_mandatory_safety", return_value=None), \
                    patch.object(harness, "_local_task_food", return_value=None), \
                    patch.object(harness, "_unsafe_destination", return_value=None), \
                    patch.object(harness.daily_policy, "validate", return_value=None):
                _, result, _ = harness._cycle(None)
            self.assertEqual(result.error_code, "shelter_project_active", result)
            self.assertFalse(result.data["executed"])
            self.assertIsNone(harness.journal.state["pending"])
        self.assertEqual(harness.context["shelter_project"], original)
        self.assertEqual(len(self.client.actions), 1)

    def completed_geometry(self):
        walls, roof, door = self.skills._blueprint(self.args["pos"], "east")
        for pos in walls + roof:
            self.client.blocks[pos] = {"name": "dirt", "properties": {}}
        for dy, half in ((0, "lower"), (1, "upper")):
            self.client.blocks[(door[0], door[1] + dy, door[2])] = {"name": "oak_door",
                "properties": {"half": half, "facing": "east", "hinge": "left", "open": False}}
        result = self.skills.execute("inspect_shelter", {"pos": self.args["pos"], "orientation": "east"})
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["shelter_verified"], result)
        return result

    def test_fresh_closed_geometry_releases_lock_preserving_home(self):
        harness = self.harness()
        harness.context["shelter_project"] = self.anchor()
        complete = self.completed_geometry()
        obs = world()
        obs["shelter"] = complete.data
        harness._observe_shelter_project({**obs, "fresh": False})
        self.assertFalse(harness.context["shelter_project"]["complete"])
        opened = copy.deepcopy(obs)
        opened["shelter"]["door"]["open"] = True
        harness._observe_shelter_project(opened)
        self.assertFalse(harness.context["shelter_project"]["complete"])
        harness._observe_shelter_project(obs)
        self.assertTrue(harness.context["shelter_project"]["complete"])
        self.assertEqual(harness.context["base_project"]["pos"], self.args["pos"])
        self.assertIsNone(harness._shelter_project_lock("overworld"))

    def test_legacy_complete_checkpoint_or_later_inspection_is_not_restored_as_building(self):
        dec, res = self.build()
        complete = self.completed_geometry()
        for mode in ("checkpoint", "event"):
            with self.subTest(mode=mode):
                journal = self.journal(world_id=mode)
                journal.record("STEP_RESULT", {"decision": asdict(dec), "result": asdict(res)})
                if mode == "checkpoint":
                    journal.update(last_result=asdict(complete))
                else:
                    journal.record("STEP_RESULT", {"decision": {"name": "inspect_shelter", "args": self.args},
                                                   "result": asdict(complete)})
                recovered = recover_project(journal)
                self.assertTrue(recovered["complete"])

    def test_lock_is_limited_to_same_dimension_daily_autonomous_shelter(self):
        harness = self.harness()
        harness.context["shelter_project"] = self.anchor()
        harness.task_controller.start({"task": "shelter"}, world(), harness.context)
        harness._task_child = True
        self.assertIsNotNone(harness._shelter_project_lock("overworld"))
        self.assertIsNone(harness._shelter_project_lock("the_nether"))
        harness._task_child = False
        self.assertIsNone(harness._shelter_project_lock("overworld"))
        harness._task_child = True
        harness.competition_mode = True
        self.assertIsNone(harness._shelter_project_lock("overworld"))

    def test_within_sixteen_return_can_take_small_side_step_toward_checked_stance(self):
        controller, obs, context = self.remote_goal({"oak_planks": 64, "oak_door": 1})
        obs["status"]["pos"] = [12.5, 64, .5]
        obs["local_geometry_origin"] = list(obs["status"]["pos"])
        obs["safe_stand_candidates"] = [{"pos": [12.5, 64, 1.5], "support": "grass_block"}]
        from test_autonomous_tasks import verified
        obs["daily_evidence"]["base_site"] = verified(pos=[0, 64, 0], orientation="east")
        obs["shelter"] = {"fresh": True, "dimension": "overworld", "pos": [0, 64, 0], "orientation": "east",
            "hazards": [], "occupied_blueprint_cells": [], "geometry": {"floor_verified": True, "exit_verified": True},
            "needs_position": True, "stand_candidates": [[1.5, 64, .5]]}
        move = controller._shelter_stance(Decision("move_to", {"pos": [1.5, 64, .5]}, "Work stance", "shelter"), obs, context)
        self.assertEqual(move.goal, "shelter_return")
        self.assertEqual(move.args["pos"], [12.5, 64, 1.5])

    def test_malformed_or_foreign_anchor_never_validates(self):
        anchor = self.anchor()
        for change in ({"scope": {"world_id": "wrong"}}, {"orientation": []}, {"source": []},
                       {"pos": [True, 64, 0]}, {"missing_blocks_hint": 56}, {"door_needed_hint": "false"}):
            with self.subTest(change=change):
                self.assertIsNone(project_anchor({**anchor, **change}, scope=self.SCOPE))


if __name__ == "__main__":
    unittest.main()
