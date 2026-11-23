import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.autonomous_tasks import AutonomousTaskController
from core.starter_mining_task import recover_interrupted_mine
from skills.starter_staircase import StarterStaircaseSkills
from test_autonomous_tasks import world
from test_starter_staircase import StairWorld


class InterruptedStaircaseTests(unittest.TestCase):
    def setUp(self):
        # ShelterTestBot steps 53--55 on 2026-11-18. Only fields used for
        # cross-checking the original dig, descent and readback are retained.
        self.site = {"entry": [4, 69, 29], "direction": "north"}
        self.action = {"name": "step_starter_staircase", "args": {**self.site, "protected_pos": [-5, 67, -1]}}
        self.goal = {"task": "craft", "item": "stone_pickaxe", "count": 1, "radius": 32}
        self.generation = {"life_generation": 20}
        self.old = {**self.action, "generation": 20, "observed": {"dimension": "overworld", "life": None,
                    "cell": [4, 66, 26], "counts": {"leaf_litter": 1, "dirt": 6, "wooden_pickaxe": 1,
                        "stick": 2, "oak_planks": 3}}}
        scope = {"world_id": "botsleep-shelter-validation-20261117", "username": "ShelterTestBot",
                 "endpoint": "http://127.0.0.1:18921/api/v1"}
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.journal = SimpleNamespace(scope=scope, state={"scope": scope.copy(), "context": self.generation},
                                       events_path=Path(folder.name, "episodes.jsonl"))
        task_id = "task-1794993508468-44"
        unknown = {"success": False, "error_code": "action_unconfirmed", "data": {**self.site,
            "action_state": "unknown", "operation": "descend", "executed": True, "pos_before": [4.5, 67, 27.4]}}
        self.rows = [
            ("STEP_RESULT", {"step": 53, "decision": self.action, "result": {"success": True,
                "data": {**self.site, "operation": "dig", "verified": True, "dug_pos": [4, 66, 26]}}}),
            ("DECISION", {"decision": self.action, "status": {"pos": [4.5, 67, 27.4],
                "dimension": "overworld", "last_death": None}}),
            ("STEP_RESULT", {"step": 54, "decision": self.action, "result": unknown}),
            ("TASK_CANCELLED", {"task_execution": {"task_id": task_id, "args": self.goal, "starter_mine": {
                "site": self.site, "cobblestone_target": 3, "returning": False}}}),
            ("CONTINUOUS_INTENT_ABANDONED", {"decision": self.action, "prior_result": unknown, "task_id": task_id,
                "pos": [4.5, 66, 26.3], "generation": 20}),
            ("STEP_RESULT", {"step": 55, "decision": {"name": "observe"}, "result": {"success": True,
                "data": {"executed": False, "reobserved": True, "original_action_confirmed": False}}}),
        ]

    def migrate(self, rows):
        self.journal.events_path.write_text("".join(json.dumps({"event": key, "data": value}) + "\n"
                                            for key, value in rows), encoding="utf-8")
        return recover_interrupted_mine(self.journal, self.old)

    def new_task(self, position=None):
        obs = world(self.old["observed"]["counts"], pos=position or [4.5, 66, 26.3], busy=False, last_death=None)
        obs["local_geometry_origin"] = list(obs["status"]["pos"])
        ctl = AutonomousTaskController({"objective": "daily"})
        ctl.start(self.goal, obs, self.generation)
        return obs, ctl

    def test_live_rungs_continue_from_new_cell_without_credit_for_old_action(self):
        anchor = self.migrate(self.rows)
        self.assertEqual(anchor, {"site": self.site, "target": 3, "from": [4, 67, 27],
                                  "operation": "descend", "goal": self.goal})
        obs, ctl = self.new_task()
        self.assertTrue(ctl.restore_interrupted_mine(obs, self.generation, {**self.old, "mine_landmark": anchor}))
        self.assertIsNone(ctl.state()["pending_digest"])
        self.assertEqual(ctl.snapshot()["progress"]["current"], 0)
        step = ctl.next_action(obs, self.generation)
        self.assertEqual(step.name, "step_starter_staircase")
        self.assertEqual(step.args, {**self.site, "from_cell": [4, 66, 26]})
        self.assertEqual(ctl.snapshot()["starter_mine"]["cobblestone_target"], 3)

    def test_original_rung_after_an_unknown_descent_returns_to_surface(self):
        obs, ctl = self.new_task([4.5, 67, 27.4])
        self.assertTrue(ctl.restore_interrupted_mine(obs, self.generation,
                        {**self.old, "mine_landmark": self.migrate(self.rows)}))
        step = ctl.next_action(obs, self.generation)
        self.assertTrue(step.args["returning"])
        self.assertEqual(step.args["from_cell"], [4, 67, 27])

    def test_legacy_hint_needs_exact_scope_life_prior_proof_and_readback(self):
        self.assertIsNotNone(self.migrate(self.rows))
        for mutation in ("scope", "generation", "task", "death", "dimension", "source_cell", "unverified", "rejected"):
            with self.subTest(mutation=mutation):
                rows = copy.deepcopy(self.rows)
                self.journal.state["scope"] = self.journal.scope.copy()
                self.journal.state["context"] = self.generation.copy()
                if mutation == "scope":
                    self.journal.state["scope"]["username"] = "OtherBot"
                elif mutation == "generation":
                    self.journal.state["context"]["life_generation"] += 1
                elif mutation == "task":
                    rows[3][1]["task_execution"]["task_id"] = "other"
                elif mutation in ("death", "dimension"):
                    rows.append(("DEATH_OBSERVED" if mutation == "death" else "DIMENSION_CHANGED", {}))
                elif mutation == "source_cell":
                    rows[1][1]["status"]["pos"] = [9.5, 67, 27.4]
                elif mutation == "unverified":
                    rows[0][1]["result"]["data"]["verified"] = False
                else:
                    rows[4][1]["prior_result"]["data"]["executed"] = False
                self.assertIsNone(self.migrate(rows))

    def test_no_adoption_outside_original_life_rungs_freshness_or_goal(self):
        old = {**self.old, "mine_landmark": self.migrate(self.rows)}
        for mutation in ("dimension", "death", "generation", "off_stair", "airborne", "stale", "geometry",
                         "wrong_goal", "repeat_ascend", "pending", "no_busy"):
            with self.subTest(mutation=mutation):
                obs, ctl = self.new_task()
                context, marker = self.generation.copy(), copy.deepcopy(old)
                if mutation == "dimension":
                    obs["status"]["dimension"] = "the_end"
                elif mutation == "death":
                    obs["status"]["last_death"] = {"t": 123}
                elif mutation == "generation":
                    context["life_generation"] += 1
                elif mutation == "off_stair":
                    obs["status"]["pos"] = obs["local_geometry_origin"] = [5.5, 66, 26.3]
                elif mutation == "airborne":
                    obs["status"]["on_ground"] = False
                elif mutation == "stale":
                    obs["observation_scope"] = "safety"
                elif mutation == "geometry":
                    obs.pop("local_geometry_origin")
                elif mutation == "wrong_goal":
                    marker["mine_landmark"]["goal"] = {"task": "wood_reserve"}
                elif mutation == "repeat_ascend":
                    marker["mine_landmark"].update(operation="ascend", **{"from": [4, 66, 26]})
                elif mutation == "pending":
                    ctl.next_action(obs, context)
                else:
                    obs["status"].pop("busy")
                self.assertFalse(ctl.restore_interrupted_mine(obs, context, marker))
                self.assertIsNone(ctl.state()["starter_mine"]["site"])

    def test_executor_rechecks_bound_source_before_dig_or_navigation(self):
        server = StairWorld()
        previous = list(server.status["pos"])
        response = StarterStaircaseSkills(server).execute("step_starter_staircase", {
            "entry": [0, 64, 0], "direction": "north", "from_cell": [0, 63, -1]})
        self.assertEqual(response.error_code, "staircase_blocked")
        self.assertIs(response.data["executed"], False)
        self.assertEqual(server.status["pos"], previous)
