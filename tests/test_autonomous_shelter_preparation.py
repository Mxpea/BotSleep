"""Internal preparation is a bounded hint/read/dig/resurvey chain."""

import unittest

from core.autonomous_tasks import AutonomousTaskController
from skills import SkillResult
from skills.positioning import PositioningSkills
from skills.shelter import ShelterSkills
from skills.shelter_preparation import ShelterPreparationSkills
import test_autonomous_shelter as fixtures
from test_autonomous_tasks import apply_atomic


class ShelterPreparationControllerTests(unittest.TestCase):
    def setUp(self):
        self.obs = fixtures.AutonomousShelterTests.survey_observation()
        self.controller = AutonomousTaskController()
        self.controller.start({"task": "shelter"}, self.obs)

    def rejected_site(self):
        decision = self.controller.next_action(self.obs)
        self.assertEqual(decision.name, "plan_shelter")
        result = fixtures.AutonomousShelterTests.survey_rejection(decision, floor=True,
            occupied_blueprint_cells=[[2, 64, 0]])
        self.controller.record_result(decision, result, self.obs)
        return decision.args["pos"]

    def proof(self, decision, *, executed=False, remaining=1, **changes):
        return {"fresh": True, "verified": True, "dimension": "overworld",
                "pos": list(decision.args["pos"]), "orientation": decision.args.get("orientation", "north"),
                "executed": executed, "preparation_plan_verified": True,
                "clear_cells": [[2, 64, 0]] if remaining else [], "clear_remaining": remaining,
                "preparation_complete": remaining == 0, "shelter_verified": False, **changes}

    def plan_preparation(self):
        decision = self.controller.next_action(self.obs)
        self.assertEqual(decision.name, "plan_shelter_preparation")
        self.controller.record_result(decision, SkillResult(True, "Read plan", self.proof(decision)), self.obs)
        return decision

    def test_rejected_survey_only_requests_read_plan_then_one_dig_then_new_survey(self):
        anchor = self.rejected_site()
        self.plan_preparation()
        dig = self.controller.next_action(self.obs)
        self.assertEqual(dig.name, "prepare_shelter_step")
        self.controller.record_result(dig, SkillResult(True, "One natural block removed",
            self.proof(dig, executed=True, remaining=0, block_removed=True,
                       cleared_pos=[2, 64, 0], cleared_name="grass_block")), self.obs)
        self.assertFalse(self.obs["daily_evidence"])
        self.assertEqual(self.controller.snapshot()["shelter_search"]["rejected_center_count"], 0)
        survey = self.controller.next_action(self.obs)
        self.assertEqual((survey.name, survey.args["pos"]), ("plan_shelter", anchor))

    def test_unknown_or_unverified_preparation_never_authorizes_resurvey_as_changed_site(self):
        for change, code in (({"action_state": "unknown"}, "dig_unconfirmed"),
                             ({"fresh": False}, "unverified"), ({"block_removed": False}, "unverified")):
            with self.subTest(change=change):
                self.setUp(); self.rejected_site(); self.plan_preparation()
                dig = self.controller.next_action(self.obs)
                proof = self.proof(dig, executed=True, block_removed=True,
                    cleared_pos=[2,64,0], cleared_name="dirt")
                proof.update(change)
                self.controller.record_result(dig, SkillResult(False, "Not proven", proof, code), self.obs)
                self.assertEqual(self.controller.snapshot()["shelter_search"]["rejected_center_count"], 1)
                if code == "dig_unconfirmed":
                    self.assertEqual(self.controller.snapshot()["status"], "unknown")
                    self.assertIsNone(self.controller.next_action(self.obs))
                else:
                    self.assertIsNone(self.controller.state()["preparation_site"])

    def test_ineligible_read_plan_keeps_negative_hint_and_is_not_repeated(self):
        anchor = self.rejected_site()
        plan = self.controller.next_action(self.obs)
        self.controller.record_result(plan, SkillResult(False, "Natural plan rejected",
            {"executed": False, "fresh": True}, "unsafe_site"), self.obs)
        self.assertIsNone(self.controller.state()["preparation_site"])
        next_decision = self.controller.next_action(self.obs)
        self.assertNotEqual(next_decision.args.get("pos"), anchor)
        self.assertEqual(self.controller.snapshot()["shelter_search"]["rejected_center_count"], 1)

    def test_strict_preparation_rejection_reads_other_orientation_before_asking_ai(self):
        self.rejected_site()
        self.controller._data["local_failures"] = 2
        for orientation in ("north", "east"):
            plan = self.controller.next_action(self.obs)
            self.assertEqual((plan.name, plan.args["orientation"]), ("plan_shelter_preparation", orientation))
            self.controller.record_result(plan, SkillResult(False, "Known unsupported doorway",
                self.proof(plan, preparation_plan_verified=False, preparation_rejected=True), "unsafe_site"), self.obs)
        south = self.controller.next_action(self.obs)
        self.assertEqual(south.args["orientation"], "south")
        self.assertEqual(self.controller.state()["survey_steps"], 3)
        self.assertEqual(self.controller.state()["local_failures"], 2)
        self.assertEqual(self.controller.snapshot()["ai_escalations"], 0)

    def test_needs_position_uses_only_fresh_closer_stance_and_existing_explore_budget(self):
        self.rejected_site(); self.plan_preparation()
        dig = self.controller.next_action(self.obs)
        proof = self.proof(dig, clear_cells=[[4,64,0]])
        self.controller.record_result(dig, SkillResult(False, "Approach first", proof, "needs_position"), self.obs)
        self.obs["local_geometry_origin"] = list(self.obs["status"]["pos"])
        self.obs["safe_stand_candidates"] = [{"pos": [2.5,64,.5], "support": "grass_block"}]
        stance = self.controller.next_action(self.obs)
        self.assertEqual((stance.name, stance.goal), ("position_for_task", "shelter_preparation_position"))
        result = apply_atomic(self.obs, stance)
        self.controller.record_result(stance, result, self.obs)
        self.assertEqual(self.controller.state()["explore_steps"], 1)
        self.assertEqual(self.controller.next_action(self.obs).name, "plan_shelter_preparation")

    def test_stale_approach_or_spent_budget_does_not_move_for_preparation(self):
        for invalid in ("stale", "budget", "not_closer"):
            with self.subTest(invalid=invalid):
                self.setUp()
                self.obs["local_geometry_origin"] = list(self.obs["status"]["pos"])
                self.obs["safe_stand_candidates"] = [{"pos": [2.5,64,.5], "support": "grass_block"}]
                if invalid == "stale": self.obs["local_geometry_origin"] = [9.5,64,.5]
                elif invalid == "budget": self.controller._data["explore_steps"] = 8
                else: self.obs["safe_stand_candidates"][0]["pos"] = [-1.5,64,.5]
                self.assertIsNone(self.controller._preparation_stance(self.obs, {}, [[4,64,0]]))

    def test_preparation_attempts_are_bounded_per_anchor_and_internal_caps_stay_shelter_only(self):
        self.rejected_site()
        anchor = self.controller.state()["preparation_site"]["pos"]
        self.controller._data["preparation_attempts"][self.controller.rules._key(anchor)] = 7
        self.plan_preparation()
        dig = self.controller.next_action(self.obs)
        self.controller.record_result(dig, SkillResult(True, "Removed one",
            self.proof(dig, executed=True, block_removed=True, cleared_pos=[2,64,0], cleared_name="dirt")), self.obs)
        self.rejected_site()
        self.assertIsNone(self.controller.state()["preparation_site"])
        self.assertIn("prepare_shelter_step", self.controller._context({})["capabilities"])
        other = AutonomousTaskController(); other.start({"task":"gather", "resource":"logs"}, self.obs)
        self.assertNotIn("prepare_shelter_step", other._context({})["capabilities"])

    def test_actual_skill_chain_removes_one_natural_block_then_surveys_and_starts_building(self):
        class TerrainWorld(fixtures.ConstructionWorld):
            def get_block(self, pos, **kwargs):
                row = super().get_block(pos, **kwargs)
                row["result"].update(diggable=True, harvest_tool_ok=True)
                return row

            def dig(self, pos, collect=True):
                self.assert_natural = self.blocks[tuple(pos)]["name"] in {"grass_block", "dirt"}
                assert self.assert_natural
                self.actions.append(("dig", list(pos), collect))
                self.blocks[tuple(pos)] = {"name": "air", "properties": {}}
                return {"ok": True, "result": {"dug": True}}

        world = TerrainWorld()
        world.status.update(speed=0, velocity=[0,0,0], busy=None, jumping=False)
        world.blocks[(2,64,0)] = {"name": "grass_block", "properties": {}}
        controller = AutonomousTaskController()
        approved = False
        def observe():
            obs = world.observe()
            if not approved: obs["daily_evidence"] = {}
            return obs
        controller.start({"task":"shelter"}, observe())
        names = []
        for _ in range(12):
            decision = controller.next_action(observe())
            self.assertIsNotNone(decision, controller.snapshot())
            names.append(decision.name)
            runner = (ShelterPreparationSkills(world) if "preparation" in decision.name or decision.name == "prepare_shelter_step"
                      else PositioningSkills(world) if decision.name == "position_for_task" else ShelterSkills(world))
            result = runner.execute(decision.name, decision.args)
            if decision.name == "plan_shelter" and result.success: approved = True
            controller.record_result(decision, result, observe())
            if decision.name == "build_shelter_step":
                self.assertTrue(result.success, result)
                break
        self.assertEqual(names[:4], ["plan_shelter", "plan_shelter_preparation", "prepare_shelter_step", "plan_shelter"])
        self.assertIn("build_shelter_step", names)
        self.assertEqual([row for row in world.actions if row[0] == "dig"], [("dig", [2,64,0], False)])
        self.assertEqual(controller.snapshot()["ai_escalations"], 0)


if __name__ == "__main__":
    unittest.main()
