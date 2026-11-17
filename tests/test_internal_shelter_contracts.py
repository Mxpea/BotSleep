"""Procedural shelter preparation stays off the model's advertised tool surface."""

import copy
import tempfile
import unittest
from unittest.mock import Mock

from agent.brain import LLMError
from agent.daily_survival import DAILY_SKILLS
from agent.llm_planner import LLMSurvivalPlanner
from agent.tools import tools_for_objective
from core.config import DEFAULTS
from core.skill_registry import (INTERNAL_SKILLS, SKILL_REGISTRY, destination_checked_skills,
                                 internal_skills_for_objective, resolve_skill_runner, skills_for_objective)
from core.survival_harness import SurvivalHarness
from test_daily_runtime import RuntimeDailyWorld
from test_llm_planner import observation, response


class InternalShelterContractTests(unittest.TestCase):
    def test_internal_names_never_expand_public_catalogs(self):
        expected = {"plan_shelter_preparation", "prepare_shelter_step"}
        self.assertEqual(INTERNAL_SKILLS, expected)
        self.assertEqual(internal_skills_for_objective("starter"), set())
        self.assertEqual(internal_skills_for_objective("daily"), expected)
        self.assertEqual(internal_skills_for_objective("competition"), expected)
        self.assertFalse(INTERNAL_SKILLS & DAILY_SKILLS)
        for objective, count in (("starter", 9), ("daily", 35), ("competition", 40)):
            names = {row["function"]["name"] for row in tools_for_objective(objective)}
            self.assertEqual(len(names), count)
            self.assertEqual(names, skills_for_objective(objective))
            self.assertFalse(names & INTERNAL_SKILLS)

    def test_model_cannot_invent_an_internal_tool_call(self):
        brain = Mock()
        for objective in ("daily", "competition"):
            planner = LLMSurvivalPlanner({"enabled": True, "model": "offline"},
                                         {"objective": objective}, brain=brain)
            for name in INTERNAL_SKILLS:
                message = response(name, {"pos": [0, 64, 0]})["choices"][0]["message"]
                with self.assertRaises(LLMError) as error:
                    planner._decision(message, observation(), {})
                self.assertEqual(error.exception.code, "llm_invalid_arguments")
        brain.chat_completion.assert_not_called()

    def test_registry_runner_and_argument_contract_are_exact(self):
        from skills.shelter_preparation import ShelterPreparationSkills
        runner = object()
        harness = Mock(daily=True, skills=object(), shelter_preparation_skills=runner)
        self.assertEqual(set(ShelterPreparationSkills.ARGUMENTS), INTERNAL_SKILLS)
        for name in INTERNAL_SKILLS:
            self.assertIs(resolve_skill_runner(harness, name), runner)
            self.assertEqual(SKILL_REGISTRY[name].runner_slot, "shelter_preparation_skills")
            self.assertEqual(ShelterPreparationSkills.ARGUMENTS[name], ({"pos", "orientation"}, {"pos"}))
        self.assertIn("prepare_shelter_step", destination_checked_skills(daily=True))
        self.assertNotIn("plan_shelter_preparation", destination_checked_skills(daily=True))
        self.assertFalse(INTERNAL_SKILLS & destination_checked_skills(daily=False))

    def test_daily_harness_binds_internal_runner_without_any_game_action(self):
        from skills.shelter_preparation import ShelterPreparationSkills
        world = RuntimeDailyWorld()
        with tempfile.TemporaryDirectory() as directory:
            settings = {**copy.deepcopy(DEFAULTS["survival"]), "objective": "daily", "state_dir": directory}
            harness = SurvivalHarness(world, settings, DEFAULTS["sleep"])
            try:
                self.assertIsInstance(harness.shelter_preparation_skills, ShelterPreparationSkills)
                self.assertIs(harness.shelter_preparation_skills.client, world)
                for name in INTERNAL_SKILLS:
                    self.assertIs(resolve_skill_runner(harness, name), harness.shelter_preparation_skills)
                self.assertEqual(world.calls, [])
            finally:
                harness.close()


if __name__ == "__main__":
    unittest.main()
