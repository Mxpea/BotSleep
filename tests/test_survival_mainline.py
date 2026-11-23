import copy
import tempfile
import time
import unittest

from agent.brain import LLMError
from agent.llm_planner import LLMSurvivalPlanner
from core.config import DEFAULTS
from core.survival_harness import SurvivalHarness
from core.survival_mainline import CHAPTERS, choose_chapter
from test_llm_planner import Brain, observation, response
from test_task_harness import TaskWorld


class MainlineTests(unittest.TestCase):
    def test_current_tools_select_the_next_chapter_and_death_rebuilds(self):
        for inventory, chapter in (({}, "wooden_pickaxe"), ({"wooden_pickaxe": 1}, "stone_pickaxe"),
                                   ({"iron_pickaxe": 1}, "first_food")):
            view, action = choose_chapter(observation(inventory), {}, {}, {})
            self.assertEqual(view["stage"], chapter)
            self.assertEqual(action.name, "run_survival_task")
            self.assertEqual(view["goal"], action.args)

    def test_sufficient_inventory_and_facilities_reach_maintenance(self):
        counts = {"cooked_beef": 8, "birch_log": 8, "coal": 8, "iron_pickaxe": 1, "torch": 4}
        board = {"tasks": [{"id": stage, "status": "satisfied"} for stage, _ in CHAPTERS]}
        self.assertEqual(choose_chapter(observation(counts), board, {}, {})[0]["stage"], "maintenance")

    def test_each_chapter_advances_only_after_its_inventory_or_facility_evidence(self):
        milestones = [
            ("wooden_pickaxe", {"wooden_pickaxe": 1}), ("stone_pickaxe", {"stone_pickaxe": 1}),
            ("first_food", {"cooked_beef": 4}), ("shelter", None), ("starter_tools", None),
            ("furnace", None), ("fuel_reserve", {"charcoal": 8}), ("torches", {"torch": 4}),
            ("bed", None), ("storage", None), ("iron_tools", None), ("food_production", None),
            ("food_reserve", {"cooked_beef": 8}), ("wood_reserve", {"birch_planks": 32}),
        ]
        counts, board = {}, {"tasks": []}
        for chapter, proof in milestones:
            with self.subTest(chapter=chapter):
                view, action = choose_chapter(observation(counts), board, {}, {})
                self.assertEqual(view["stage"], chapter)
                self.assertEqual(action.args, view["goal"])
            if proof is None:
                board["tasks"].append({"id": chapter, "status": "satisfied"})
            else:
                counts.update(proof)
        final, _ = choose_chapter(observation(counts), board, {}, {})
        self.assertEqual((final["stage"], final["chapter"]), ("maintenance", 15))

    def test_deployed_furnace_can_be_remembered_until_it_is_in_range_again(self):
        counts = {"stone_pickaxe": 1, "cooked_beef": 8}
        rows = [{"id": key, "status": "satisfied"} for key in ("shelter", "starter_tools")]
        sites = {}
        furnace = {"id": "furnace", "status": "satisfied", "evidence": {"positions": [[0, 64, 0]]}}
        choose_chapter(observation(counts), {"tasks": rows + [furnace]}, {}, sites)
        stage = choose_chapter(observation(counts, pos=[32, 64, 0]), {"tasks": rows}, {}, sites)[0]
        self.assertEqual(stage["stage"], "fuel_reserve")
        stage = choose_chapter(observation(counts), {"tasks": rows}, {}, sites)[0]
        self.assertEqual(stage["stage"], "furnace")

    def test_owned_furnace_is_deployed_through_smelting_instead_of_crafted_again(self):
        counts = {"stone_pickaxe": 1, "cooked_beef": 4, "furnace": 1}
        board = {"tasks": [{"id": key, "status": "satisfied"} for key in ("starter_tools","shelter")]}
        view, step = choose_chapter(observation(counts), board, {}, {})
        self.assertEqual(view["stage"], "furnace")
        self.assertEqual(step.args, {"task": "smelt", "item": "charcoal", "count": 1})

    def test_real_harness_issues_stone_goal_without_model_call(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = copy.deepcopy(DEFAULTS["survival"])
            settings.update(objective="daily", state_dir=temp)
            settings["autonomy"]["mainline"] = True
            world = TaskWorld()
            world.counts = {"wooden_pickaxe": 1, "stick": 2}
            brain = Brain()
            planner = LLMSurvivalPlanner({"enabled": True}, settings, brain=brain)
            h = SurvivalHarness(world, settings, DEFAULTS["sleep"], planner=planner)
            try:
                result = h.tick(automatic=True)
                deadline = time.monotonic() + 5
                while result is None and time.monotonic() < deadline:
                    time.sleep(.005)
                    result = h.tick(automatic=True)
                self.assertTrue(result.data.get("accepted"), result)
                self.assertEqual(h.snapshot()["mainline"]["stage"], "stone_pickaxe")
                self.assertEqual(h.snapshot()["task_execution"]["args"]["item"], "stone_pickaxe")
                self.assertEqual(brain.requests, [])
                # A lost wooden pick regresses the chapter before any mine is entered.
                world.counts.clear()
                now = h._observe()
                h._prepare_context(now)
                self.assertEqual(h.snapshot()["mainline"]["stage"], "wooden_pickaxe")
                self.assertEqual(h._task_next(now).name, "wait")
                self.assertEqual(h._task_snapshot()["status"], "cancelled")
                self.assertEqual(h._mainline_action(now).args["item"], "wooden_pickaxe")
                self.assertEqual(brain.requests, [])
            finally:
                h.close()

    def test_iron_chapter_uses_a_local_deep_mine_for_missing_ore(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = copy.deepcopy(DEFAULTS["survival"])
            settings.update(objective="daily", state_dir=temp)
            settings["autonomy"]["mainline"] = True
            world = TaskWorld()
            world.counts.update(stone_pickaxe=2, stone_axe=1, stone_sword=1, stick=4,
                                cooked_beef=8, torch=4, coal=8, birch_planks=8)
            world.block([2, 64, 0], "crafting_table")
            world.furnace()
            brain = Brain()
            planner = LLMSurvivalPlanner({"enabled": True}, settings, brain=brain)
            harness = SurvivalHarness(world, settings, DEFAULTS["sleep"], planner=planner)
            try:
                harness.context["mainline_sites"] = {
                    key: {"pos": [35, 64, 0], "dimension": world.status["dimension"]}
                    for key in ("shelter", "bed", "storage")}
                obs = harness._observe()
                harness._prepare_context(obs)
                goal = harness._mainline_action(obs)
                self.assertEqual(goal.args, {"task": "iron_tools"})
                self.assertTrue(harness._task_start(goal, obs).success)
                self.assertEqual(harness._task_next(obs).goal, "deep_mine")
                self.assertEqual(harness.task_controller.state()["pending_deep_mine"]["resource"], "raw_iron")
                self.assertEqual(brain.requests, [])
            finally:
                harness.close()

    def test_native_wood_reserve_is_normalized_once_and_policy_still_applies(self):
        settings = dict(DEFAULTS["survival"], objective="daily")
        args = {"task": "wood_reserve", "count": 1, "reason": "Gather a log"}
        brain = Brain(response("run_survival_task", args))
        p = LLMSurvivalPlanner({"enabled": True, "max_repair_attempts": 0}, settings, brain=brain)
        step = p.choose(observation(), {})
        self.assertEqual((step.args["task"], step.args["resource"], step.args["count"]), ("gather", "logs", 1))
        self.assertEqual(len(brain.requests), 1)
        p = LLMSurvivalPlanner({"enabled": True, "max_repair_attempts": 0}, settings,
                               brain=Brain(response("run_survival_task", args)))
        with self.assertRaises(LLMError):
            p.choose(observation(), {"mainline": {"fresh": True, "goal": {"task":"craft",
                         "item":"stone_pickaxe","count":1}}})
