"""Unit tests for `core.intent_compiler` normalization, prompt alignment, and local workstation fallback."""

import copy
import unittest

from agent.llm_planner import LLMSurvivalPlanner
from core.autonomous_tasks import AutonomousTaskController
from core.config import DEFAULTS
from core.intent_compiler import IntentCompiler, should_prefer_local_workstation
from skills.daily_survival import DailySurvivalSkills
from test_llm_planner import Brain, response


def _obs(pos=None, counts=None, blocks=None, placement=None):
    return {
        "status": {
            "ready": True,
            "alive": True,
            "busy": None,
            "gamemode": "survival",
            "dimension": "overworld",
            "pos": pos or [0.5, 64.0, 0.5],
            "health": 20.0,
            "food": 20,
            "is_day": True,
            "time_of_day": 3000,
            "thundering": False,
            "on_ground": True,
        },
        "counts": counts or {},
        "blocks": blocks or [],
        "entities": [],
        "placement_candidates": placement or [{"pos": [2, 64, 0], "support": "stone"}],
        "fresh": True,
        "observation_scope": "full",
        "inventory_fresh": True,
        "terrain_fresh": True,
    }


class IntentCompilerTests(unittest.TestCase):
    def _compiler(self, intent="speedrun"):
        return IntentCompiler(
            intent=intent,
            smelt_items=frozenset(DailySurvivalSkills.SMELTABLE.values()),
        )

    def test_normalizes_plurals_and_smelt_raw_iron_deterministically(self):
        compiler = self._compiler("speedrun")
        obs = _obs(counts={"spruce_log": 2})

        sticks_args, err1 = compiler.compile_tool_call("run_survival_task", {"task": "craft", "item": "sticks", "count": 4}, obs, {})
        self.assertIsNone(err1)
        self.assertEqual(sticks_args["item"], "stick")

        planks_args, err2 = compiler.compile_tool_call("run_survival_task", {"task": "craft", "item": "planks", "count": 4}, obs, {})
        self.assertIsNone(err2)
        self.assertEqual(planks_args["item"], "spruce_planks")

        smelt_args, err3 = compiler.compile_tool_call("run_survival_task", {"task": "smelt", "item": "raw_iron", "count": 4}, obs, {})
        self.assertIsNone(err3)
        self.assertEqual(smelt_args["item"], "iron_ingot")

        gather_args, err4 = compiler.compile_tool_call("run_survival_task", {"task": "gather", "item": "iron_ore", "count": 8}, obs, {})
        self.assertIsNone(err4)
        self.assertEqual(gather_args["resource"], "raw_iron")
        self.assertNotIn("item", gather_args)

    def test_speedrun_rejects_copper_smelt_and_blocks_repeated_cooldown_signature(self):
        compiler = self._compiler("speedrun")
        obs = _obs()

        _, err_cu = compiler.compile_tool_call("run_survival_task", {"task": "smelt", "item": "copper_ingot", "count": 4}, obs, {})
        self.assertIsNotNone(err_cu)
        self.assertEqual(err_cu[0], "llm_invalid_arguments")
        self.assertIn("iron_ingot", err_cu[1])

        ctx = {"blocked_task_signatures": {"run_survival_task:craft:stick": 95.0}}
        _, err_cd = compiler.compile_tool_call("run_survival_task", {"task": "craft", "item": "sticks", "count": 4}, obs, ctx)
        self.assertIsNotNone(err_cd)
        self.assertEqual(err_cd[0], "llm_repeated_failure")

    def test_filter_prompt_context_hides_unavailable_playbook_tools(self):
        ctx = {
            "playbook": {
                "next_actions": [
                    {"name": "inspect_container", "args": {"pos": [1, 64, 1]}},
                    {"name": "run_survival_task", "args": {"task": "smelt", "item": "iron_ingot"}},
                ],
                "bindings": ["inspect_container", "run_survival_task"],
            }
        }
        IntentCompiler.filter_prompt_context(ctx, {"run_survival_task", "wait"})
        self.assertEqual([a["name"] for a in ctx["playbook"]["next_actions"]], ["run_survival_task"])
        self.assertEqual(ctx["playbook"]["bindings"], ["run_survival_task"])

    def test_planner_end_to_end_compiles_sticks_tool_call(self):
        settings = copy.deepcopy(DEFAULTS["survival"])
        settings.update(objective="daily", intent="speedrun")
        brain = Brain(response("run_survival_task", {"task": "craft", "item": "sticks", "count": 4}))
        planner = LLMSurvivalPlanner({"enabled": True, "max_repair_attempts": 0}, settings, brain=brain)
        decision = planner.choose(_obs(counts={"oak_planks": 2}), {"milestones": {}})
        self.assertEqual(decision.name, "run_survival_task")
        self.assertEqual(decision.args["item"], "stick")

    def test_underground_remote_table_triggers_local_crafting_table(self):
        # Simulate bot_2 scenario: underground at y=69 with cobblestone/planks/sticks,
        # while the only known crafting_table is 16 blocks up on surface at y=85.
        obs = _obs(
            pos=[2022.5, 69.0, 29.5],
            counts={"cobblestone": 149, "oak_planks": 4, "stick": 4},
            blocks=[{"name": "crafting_table", "pos": [2021, 85, 18], "dist": 19.7}],
            placement=[{"pos": [2023, 69, 29], "support": "stone"}],
        )
        self.assertTrue(should_prefer_local_workstation("crafting_table", obs, {}))

        ctl = AutonomousTaskController()
        ctl.start({"task": "craft", "item": "stone_pickaxe", "count": 1}, obs)
        step = ctl.next_action(obs)
        # Should craft a local crafting_table immediately instead of pathing up to y=85.
        self.assertEqual(step.name, "craft_item")
        self.assertEqual(step.args["item"], "crafting_table")


if __name__ == "__main__":
    unittest.main()
