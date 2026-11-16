"""Offline contract tests for staged competition choices and selection gates."""

import copy
import json
import unittest

from core.competition_rules import scoring_catalog
from core.competition_tasks import CompetitionTasks


MINE = "minecraft:story/mine_stone"
UPGRADE = "minecraft:story/upgrade_tools"
SMELT = "minecraft:story/smelt_iron"
TOOLS = "minecraft:story/iron_tools"
ARMOR = "minecraft:story/obtain_armor"
SLEEP = "minecraft:adventure/sleep_in_bed"
PLANT = "minecraft:husbandry/plant_seed"
MAPPED = {MINE, UPGRADE, SMELT, TOOLS, ARMOR, SLEEP, PLANT}
CAPABILITIES = {
    "gather_block", "gather_resource", "craft_item", "place_item", "smelt_start",
    "smelt_collect", "equip_armor", "auto_sleep", "plant_crop", "eat_food",
    "place_bed", "gather_seeds", "harvest_crop", "till_soil", "hunt_animal", "wait",
}


def observation(counts=None, blocks=(), entities=(), **status):
    """Build a fresh observation without a client or game connection."""
    return {
        "fresh": True,
        "status": {"ready": True, "alive": True, "busy": False,
                   "pos": [0.5, 64, 0.5], "dimension": "overworld",
                   "is_day": True, "thundering": False, **status},
        "counts": dict(counts or {}),
        "blocks": [{"name": name, "pos": [1, 64, 1]} for name in blocks],
        "entities": [{"name": name, "pos": [1, 64, 1]} for name in entities],
    }


class CompetitionTaskViewTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_700_000_000.0
        self.catalog = scoring_catalog()
        self.manager = self.new_manager()

    def new_manager(self, catalog=None):
        return CompetitionTasks(catalog=self.catalog if catalog is None else catalog,
                                clock=lambda: self.now)

    def advancements(self, completed=()):
        """Synthetic full default tree, including all 125 catalog rows."""
        completed = set(completed)
        rows = [{"id": row["id"], "title": row["title"], "frame": "task",
                 "status": "completed" if row["id"] in completed else "not_started",
                 "completed_at": None,
                 "progress": {"criteria_done": int(row["id"] in completed),
                              "criteria_total": 1}}
                for row in self.catalog]
        return {"ok": True, "result": {
            "ready": True, "updated_at": self.now * 1000, "packets_received": 1,
            "summary": {"total": len(rows), "completed": len(completed),
                        "in_progress": 0, "not_started": len(rows) - len(completed)},
            "advancements": rows,
        }}

    def refresh(self, world=None, completed=(), capabilities=CAPABILITIES):
        self.manager.update(observation() if world is None else world,
                            self.advancements(completed), capabilities=capabilities)
        return self.manager.model_view()

    def assert_choices(self, view, expected):
        ids = [row["id"] for row in view["tasks"]]
        self.assertEqual(set(ids), set(expected))
        self.assertEqual(len(ids), len(expected), "Candidates must not repeat")
        self.assertEqual(view["selectable_goal_ids"], ids)
        for row in view["tasks"]:
            self.assertEqual(set(row), {"id", "title", "points", "availability"})

    def test_bootstrap_projects_one_choice_but_keeps_full_audit_catalog(self):
        view = self.refresh()
        self.assertEqual(view["phase"], "selection")
        self.assertEqual(view["stage"], "bootstrap")
        self.assertIsNone(view["active_goal"])
        self.assert_choices(view, {MINE})
        self.assertEqual(len(self.manager.snapshot()["tasks"]), 125)
        assessment = view["catalog_assessment"]
        self.assertEqual(set(assessment), {"checked", "total", "known_count", "complete"})
        self.assertTrue(assessment["complete"])
        self.assertEqual(assessment["checked"], assessment["total"])
        self.assertEqual(assessment["known_count"], assessment["total"])
        json.dumps(view, allow_nan=False)
        self.assertTrue(self.manager.select(MINE)["success"])

    def test_tool_and_iron_gates_apply_to_display_and_direct_selection(self):
        cases = [
            ({}, {MINE}, "bootstrap"),
            ({"wooden_pickaxe": 1}, {MINE, UPGRADE}, "stone"),
            ({"golden_pickaxe": 1}, {MINE, UPGRADE}, "stone"),
            ({"cobblestone": 1}, {MINE, UPGRADE}, "stone"),
            ({"stone_pickaxe": 1}, {MINE, UPGRADE, SMELT}, "iron"),
            ({"iron_pickaxe": 1}, {MINE, UPGRADE, SMELT}, "iron"),
            ({"diamond_pickaxe": 1}, {MINE, UPGRADE, SMELT}, "iron"),
            ({"netherite_pickaxe": 1}, {MINE, UPGRADE, SMELT}, "iron"),
            ({"raw_iron": 1}, {MINE, SMELT}, "iron"),
            ({"iron_ingot": 1}, {MINE, SMELT, TOOLS, ARMOR}, "iron"),
            ({"raw_iron": 1, "furnace": 1}, {MINE, SMELT, TOOLS, ARMOR}, "iron"),
            ({"wooden_pickaxe": 0, "cobblestone": 0, "raw_iron": 0,
              "iron_ingot": 0}, {MINE}, "bootstrap"),
        ]
        for counts, expected, stage in cases:
            with self.subTest(counts=counts):
                self.manager = self.new_manager()
                view = self.refresh(observation(counts))
                self.assertEqual(view["stage"], stage)
                self.assert_choices(view, expected)
                for hidden in MAPPED - expected:
                    self.assertFalse(self.manager.select(hidden)["success"], hidden)
                self.assertEqual(self.manager.model_view()["phase"], "selection")
        for pos, expected in [([1, 64, 1], {MINE, SMELT, TOOLS, ARMOR}),
                              ([100, 64, 100], {MINE, SMELT})]:
            with self.subTest(furnace_pos=pos):
                self.manager = self.new_manager()
                world = observation({"raw_iron": 1})
                world["blocks"] = [{"name": "furnace", "pos": pos}]
                self.assert_choices(self.refresh(world), expected)
                if TOOLS not in expected:
                    self.assertFalse(self.manager.select(TOOLS)["success"])
                    self.assertFalse(self.manager.select(ARMOR)["success"])

    def test_sleep_and_plant_require_current_materials_or_visible_sources(self):
        cases = [
            (observation({"white_wool": 2}), set()),
            (observation({"white_wool": 3}), {SLEEP}),
            (observation({"white_bed": 1}), {SLEEP}),
            (observation(entities=("sheep",)), {SLEEP}),
            (observation(entities=("minecraft:sheep",)), {SLEEP}),
            (observation(entities=("cow",)), set()),
            (observation({"wheat_seeds": 0}), set()),
            (observation({"wheat_seeds": 1}), {PLANT}),
            (observation({"beetroot_seeds": 1}), {PLANT}),
            (observation({"carrot": 1}), {PLANT}),
            (observation({"potato": 1}), {PLANT}),
            (observation(blocks=("short_grass",)), {PLANT}),
            (observation(blocks=("tall_grass",)), {PLANT}),
            (observation(blocks=("wheat",)), {PLANT}),
            (observation(blocks=("carrots",)), {PLANT}),
            (observation(blocks=("potatoes",)), {PLANT}),
            (observation(blocks=("beetroots",)), {PLANT}),
            (observation(blocks=("grass_block",)), set()),
        ]
        for world, side_goals in cases:
            with self.subTest(world=world):
                self.manager = self.new_manager()
                self.assert_choices(self.refresh(world), {MINE} | side_goals)
                for hidden in {SLEEP, PLANT} - side_goals:
                    self.assertFalse(self.manager.select(hidden)["success"], hidden)
                for shown in side_goals:
                    self.assertTrue(self.manager.select(shown)["success"], shown)

    def test_ready_sleep_and_plant_are_visible_even_without_tool_progression(self):
        view = self.refresh(observation({"wheat_seeds": 1},
                                        blocks=("white_bed", "farmland"), is_day=False))
        self.assertEqual(view["stage"], "bootstrap")
        self.assert_choices(view, {MINE, SLEEP, PLANT})
        ready = {row["id"] for row in view["tasks"] if row["availability"] == "ready"}
        self.assertEqual(ready, {SLEEP, PLANT})

    def test_all_seven_unlocked_candidates_are_returned_without_top_n_limit(self):
        view = self.refresh(observation({"stone_pickaxe": 1, "iron_ingot": 4,
                                        "white_bed": 1, "wheat_seeds": 1}))
        self.assert_choices(view, MAPPED)
        self.assertEqual(len(view["tasks"]), 7)
        for row in self.catalog:
            if row["id"] in MAPPED:
                candidate = next(task for task in view["tasks"] if task["id"] == row["id"])
                self.assertEqual(candidate["title"], row["title"])
                self.assertEqual(candidate["points"], row["points"])

    def test_pending_excluded_and_unsupported_goals_cannot_be_selected(self):
        self.refresh(observation({"iron_ingot": 4, "stone_pickaxe": 1}))
        board = self.manager.snapshot()
        blocked = [row for row in board["tasks"]
                   if row["eligibility"] in {"pending", "excluded"}
                   or row["availability"] == "unsupported"]
        self.assertTrue(any(row["eligibility"] == "pending" for row in blocked))
        self.assertTrue(any(row["eligibility"] == "excluded" for row in blocked))
        self.assertTrue(any(row["availability"] == "unsupported" for row in blocked))
        visible = set(self.manager.model_view()["selectable_goal_ids"])
        for row in blocked:
            with self.subTest(goal=row["id"]):
                self.assertNotIn(row["id"], visible)
                self.assertFalse(self.manager.select(row["id"])["success"])
        self.assert_choices(self.refresh(capabilities=set()), set())
        self.assertFalse(self.manager.select(MINE)["success"])

    def test_active_goal_has_full_rules_plan_and_criteria_with_detached_results(self):
        catalog = copy.deepcopy(self.catalog)
        source = next(row for row in catalog if row["id"] == MINE)
        source["condition"] = "Local rule requiring explicit verification. " * 8 + "END OF RULE"
        self.manager = self.new_manager(catalog)
        self.refresh()
        self.assertTrue(self.manager.select(MINE)["success"])
        view = self.manager.model_view()
        self.assertEqual(view["phase"], "active")
        self.assert_choices(view, set())
        active = view["active_goal"]
        self.assertEqual(active["id"], MINE)
        self.assertEqual(active["title"], source["title"])
        self.assertEqual(active["points"], source["points"])
        self.assertEqual(active["condition"], source["condition"])
        for key in ("missing", "next_steps", "required_skills"):
            self.assertIsInstance(active[key], list)
            self.assertTrue(active[key], key)
        for key in ("completion_criteria", "failure_criteria", "abort_criteria"):
            self.assertIsInstance(active[key], str)
            self.assertTrue(active[key].strip(), key)
        json.dumps(view, allow_nan=False)
        active["condition"] = "corrupted"
        active["missing"].clear()
        fresh = self.manager.model_view()["active_goal"]
        self.assertEqual(fresh["condition"], source["condition"])
        self.assertTrue(fresh["missing"])
        self.assertEqual(len(self.manager.snapshot()["tasks"]), 125)

    def test_finish_returns_selection_and_recomputes_from_new_inventory(self):
        self.refresh()
        self.assertTrue(self.manager.select(MINE)["success"])
        self.refresh(observation({"stone_pickaxe": 1}), completed={MINE})
        result = self.manager.finish("Fresh server evidence confirms Stone Age")
        self.assertTrue(result["success"])
        self.assertTrue(result["data"]["completed"])
        view = self.manager.model_view()
        self.assertEqual(view["phase"], "selection")
        self.assertEqual(view["stage"], "iron")
        self.assertIsNone(view["active_goal"])
        self.assert_choices(view, {UPGRADE, SMELT})
        self.assertFalse(self.manager.select(MINE)["success"])

    def test_force_exit_recomputes_choices_and_cooldown_blocks_reselection(self):
        self.refresh()
        self.assertTrue(self.manager.select(MINE)["success"])
        self.refresh(observation({"iron_ingot": 1}))
        self.assertTrue(self.manager.force_exit("Leave blocked stone search")["success"])
        view = self.manager.model_view()
        self.assertEqual(view["phase"], "selection")
        self.assertEqual(view["stage"], "iron")
        self.assertIsNone(view["active_goal"])
        self.assert_choices(view, {SMELT, TOOLS, ARMOR})
        self.assertFalse(self.manager.select(MINE)["success"])
        self.now += 299
        self.assert_choices(self.refresh(observation({"iron_ingot": 1})), {SMELT, TOOLS, ARMOR})
        self.assertFalse(self.manager.select(MINE)["success"])
        self.now += 2
        self.assert_choices(self.refresh(observation({"iron_ingot": 1})), {MINE, SMELT, TOOLS, ARMOR})
        self.assertTrue(self.manager.select(MINE)["success"])

    def test_unobserved_and_terminal_states_have_no_candidates_or_selection_bypass(self):
        unknowns = [None, {}, {**observation({"iron_ingot": 4}), "fresh": False},
                    {**observation({"iron_ingot": 4}), "stale": True}]
        for world in unknowns:
            with self.subTest(world=world):
                self.manager = self.new_manager()
                if world is not None:
                    self.manager.update(world, self.advancements(), capabilities=CAPABILITIES)
                view = self.manager.model_view()
                self.assertEqual(view["stage"], "unobserved")
                self.assertIsNone(view["active_goal"])
                self.assert_choices(view, set())
                for goal in MAPPED:
                    self.assertFalse(self.manager.select(goal)["success"], goal)
        self.manager = self.new_manager()
        self.refresh()
        self.assertTrue(self.manager.end_competition("Stop after full catalog assessment")["success"])
        view = self.manager.model_view()
        self.assertEqual(view["phase"], "completed")
        self.assertIsNone(view["active_goal"])
        self.assert_choices(view, set())
        self.refresh(observation({"iron_ingot": 4, "white_bed": 1, "wheat_seeds": 1}))
        self.assert_choices(self.manager.model_view(), set())
        for goal in MAPPED:
            self.assertFalse(self.manager.select(goal)["success"], goal)


if __name__ == "__main__":
    unittest.main()
