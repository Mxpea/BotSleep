"""Offline goal programs use changed world/inventory evidence, never fake queues."""

import copy
import json
import math
import unittest
from unittest.mock import patch

from agent.survival_planner import Decision, LOG_NAMES, PLANK_NAMES
from core.autonomous_tasks import AutonomousTaskController, CRAFT_ITEMS, RECIPES, SMELT_ITEMS
from skills import SkillResult


def world(counts=None, **status):
    return {"fresh": True, "inventory_fresh": True,
            "status": {"ready": True, "alive": True, "gamemode": "survival", "dimension": "overworld",
                       "pos": [.5, 64, .5], "health": 20, "food": 20, "on_ground": True,
                       "is_day": True, "time_of_day": 1000, "thundering": False, **status},
            "counts": dict(counts or {}), "inventory": [], "entities": [],
            "blocks": [{"name": "oak_log", "pos": [2, 64, 0], "can_see": True},
                       {"name": "oak_log", "pos": [2, 64, 1], "can_see": True}],
            "daily_evidence": {}, "furnaces": [], "safe_stand_candidates": [], "placement_candidates": []}


def verified(**fields):
    return {"fresh": True, "verified": True, "dimension": "overworld", "source": "api_verified", **fields}


def apply_atomic(obs, decision):
    """A small mutable world fixture implementing actual material consumption."""
    name, args = decision.name, decision.args
    counts = obs["counts"]
    gained = {}
    if name in {"gather_block", "gather_resource"}:
        row = next(b for b in obs["blocks"] if b["pos"] == args["pos"])
        assert row["name"] == args["expected_name"]
        item = {"stone": "cobblestone", "iron_ore": "raw_iron", "coal_ore": "coal"}.get(row["name"], row["name"])
        counts[item] = counts.get(item, 0) + 1
        obs["blocks"].remove(row)
        gained = {item: 1}
    elif name == "craft_item":
        recipe, size = RECIPES[args["item"]]
        times = math.ceil(args["count"] / size)
        for item, count in recipe.items():
            remaining = count * times
            names = PLANK_NAMES if item == "planks" else (item,)
            for material in names:
                used = min(remaining, counts.get(material, 0))
                counts[material] = counts.get(material, 0) - used
                remaining -= used
            assert remaining == 0, (args, item, counts)
        counts[args["item"]] = counts.get(args["item"], 0) + times * size
        gained = {args["item"]: times * size}
    elif name == "place_item":
        assert counts[args["item"]] > 0
        counts[args["item"]] -= 1
        obs["blocks"].append({"name": args["item"], "pos": list(args["pos"]), "fresh": True})
    elif name == "clear_foliage":
        obs["blocks"] = [b for b in obs["blocks"] if b["pos"] != args["pos"]]
        for row in obs["blocks"]:
            if row.get("visibility", {}).get("blocker") == args["pos"]:
                row.update(can_see=True, visibility={"visible": True})
    elif name == "position_for_task":
        obs["status"]["pos"] = list(args["pos"])
    elif name == "collect_drops":
        row = next(e for e in obs["entities"] if e["id"] == args["entity_id"])
        item, amount = row["item"].rsplit("x", 1)
        assert item == args["expected_item"]
        counts[item] = counts.get(item, 0) + int(amount)
        obs["entities"].remove(row)
        gained = {item: int(amount)}
    else:
        raise AssertionError("Fixture has no implementation for " + name)
    return SkillResult(True, "World changed", {"verified": True, "executed": True, "gained": gained})


class AutonomousTaskTests(unittest.TestCase):
    def start(self, args=None, obs=None, **limits):
        self.obs = obs or world()
        self.controller = AutonomousTaskController({"autonomy": limits})
        self.controller.start(args or {"task": "gather", "resource": "logs", "count": 2}, self.obs)
        return self.controller

    def act(self):
        decision = self.controller.next_action(self.obs)
        self.assertIsNotNone(decision, self.controller.snapshot())
        result = apply_atomic(self.obs, decision)
        self.controller.record_result(decision, result, self.obs)
        return decision

    def test_bone_meal_crafts_from_held_bone_without_workstation(self):
        obs = world({"bone_meal": 2})
        controller = self.start({"task": "craft", "item": "bone_meal", "count": 3}, obs)
        obs["counts"]["bone"] = 1  # Newly observed inventory, not an entity or a hit receipt.
        self.assertEqual(self.act().name, "craft_item")
        self.assertEqual(obs["counts"], {"bone_meal": 5, "bone": 0})
        self.assertIsNone(controller.next_action(obs))
        self.assertEqual(controller.snapshot()["status"], "completed")

    def test_bone_meal_reports_remaining_bones_without_chasing_visible_drop(self):
        obs = world({"bone": 1})
        obs["entities"] = [{"id": 7, "name": "item", "item": "bonex4", "pos": [2.5, 64, .5]}]
        controller = self.start({"task": "craft", "item": "bone_meal", "count": 7}, obs)
        self.assertEqual(self.act().name, "craft_item")
        self.assertIsNone(controller.next_action(obs))
        snapshot = controller.snapshot()
        self.assertEqual(snapshot["status"], "needs_ai")
        self.assertIn("missing_material: bone x2", snapshot["reason"])
        self.assertEqual(snapshot["progress"], {"current": 3, "target": 7, "unit": "bone_meal"})
        self.assertIsNone(controller.state()["pending_digest"])

    def test_bone_meal_leaves_bone_block_untouched_when_recipe_is_ambiguous(self):
        obs = world({"bone": 1, "bone_block": 1})
        controller = self.start({"task": "craft", "item": "bone_meal", "count": 3}, obs)
        self.assertIsNone(controller.next_action(obs))
        self.assertIn("ambiguous_recipe", controller.snapshot()["reason"])
        self.assertEqual(obs["counts"], {"bone": 1, "bone_block": 1})

    def test_diamond_boots_use_four_diamonds_and_verify_output(self):
        obs = world({"diamond": 4})
        obs["blocks"].append({"name": "crafting_table", "pos": [1, 64, 0], "can_see": True})
        controller = self.start({"task": "craft", "item": "diamond_boots", "count": 1}, obs)
        decision = self.act()
        self.assertEqual((decision.name, decision.args), ("craft_item", {"item": "diamond_boots", "count": 1}))
        self.assertEqual(obs["counts"], {"diamond": 0, "diamond_boots": 1})
        self.assertIsNone(controller.next_action(obs))
        self.assertEqual(controller.snapshot()["status"], "completed")

    def test_invalid_arguments_and_unsupported_items_fail_without_mutating_active_goal(self):
        controller = self.start()
        before = controller.state()
        for args in ({"task": "dragon"}, {"task": "gather"}, {"task": "gather", "resource": []},
                     {"task": "craft", "item": "netherite_sword"}, {"task": "smelt", "item": "raw_iron"},
                     {"task": "food", "item": "bread"}, {"task": "food", "count": True},
                     {"task": "food", "radius": 65}, {"task": "food", "queue": []}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                controller.start(args, self.obs)
            self.assertEqual(controller.state(), before)

    def test_config_contract_accepts_zero_exploration_and_fractional_deadline(self):
        controller = self.start(max_explore_steps=0, max_seconds=.5)
        self.assertEqual(controller.limits["max_explore_steps"], 0)
        self.assertEqual(controller.limits["max_seconds"], .5)
        for settings in ({"max_seconds": 3601}, {"max_seconds": float("inf")}, {"max_steps": 2049}):
            with self.assertRaises(ValueError):
                AutonomousTaskController({"autonomy": settings})

    def test_incremental_gather_is_current_inventory_baseline_plus_requested_count(self):
        controller = self.start(obs=world({"oak_log": 5}))
        self.assertEqual(controller.snapshot()["progress"], {"current": 5, "target": 7, "unit": "logs"})
        self.act()
        self.assertEqual(controller.snapshot()["status"], "running")
        self.act()
        self.assertIsNone(controller.next_action(self.obs))
        self.assertEqual(controller.snapshot()["status"], "completed")
        self.assertEqual(self.obs["counts"]["oak_log"], 7)

    def test_acknowledgement_or_claimed_gained_does_not_complete_without_inventory(self):
        controller = self.start({"task": "gather", "resource": "logs", "count": 1}, max_no_progress_steps=2)
        for _ in range(2):
            decision = controller.next_action(self.obs)
            controller.record_result(decision, SkillResult(True, "claimed", {"verified": True, "gained": {"oak_log": 99}}), self.obs)
        self.assertIsNone(controller.next_action(self.obs))
        self.assertEqual(controller.snapshot()["status"], "needs_ai")
        self.assertEqual(controller.snapshot()["progress"]["current"], 0)

    def test_only_one_child_outstanding_and_no_action_queue_is_saved(self):
        controller = self.start()
        decision = controller.next_action(self.obs)
        self.assertIsNone(controller.next_action(self.obs))
        state = controller.state()
        self.assertEqual(len(state["pending_digest"]), 64)
        self.assertNotIn("expected_name", json.dumps(state))
        with self.assertRaises(ValueError):
            controller.record_result(Decision("wait", {}, "", ""), SkillResult(True), self.obs)
        controller.record_result(decision, apply_atomic(self.obs, decision), self.obs)
        self.assertIsNotNone(controller.next_action(self.obs))

    def test_known_path_failure_excludes_actual_target_and_selects_another_resource(self):
        controller = self.start()
        first = controller.next_action(self.obs)
        controller.record_result(first, SkillResult(False, "No path", {"target": first.args["pos"], "executed": False}, "path_not_found"), self.obs)
        second = controller.next_action(self.obs)
        self.assertEqual(second.name, "gather_block")
        self.assertNotEqual(second.args["pos"], first.args["pos"])
        self.assertEqual(controller.snapshot()["local_recoveries"], 1)

    def test_visible_leaf_is_cleared_then_resource_recomputed_from_fresh_blocks(self):
        obs = world(time_of_day=1000)
        obs["blocks"] = [{"name": "oak_log", "pos": [2, 64, 0], "can_see": False,
                          "visibility": {"blocker": [1, 64, 0]}},
                         {"name": "oak_leaves", "pos": [1, 64, 0], "fresh": True, "can_see": True}]
        self.start({"task": "gather", "resource": "logs", "count": 1}, obs)
        self.assertEqual(self.act().name, "clear_foliage")
        self.assertEqual(self.act().name, "gather_block")
        self.assertIsNone(self.controller.next_action(obs))
        self.assertEqual(self.controller.snapshot()["status"], "completed")

    def test_workstation_wood_prerequisite_uses_fresh_leaf_recovery(self):
        obs = world({"oak_planks": 3, "stick": 2})
        obs["blocks"] = [{"name": "birch_log", "pos": [2, 64, 0], "can_see": False,
                          "visibility": {"blocker": [1, 65, 0]}},
                         {"name": "birch_leaves", "pos": [1, 65, 0], "fresh": True, "can_see": True}]
        self.start({"task": "craft", "item": "wooden_pickaxe", "count": 1}, obs)
        self.assertEqual(self.act().name, "clear_foliage")
        self.assertEqual(self.act().args["expected_name"], "birch_log")
        for _ in range(16):
            decision = self.controller.next_action(obs)
            if decision is None:
                break
            self.controller.record_result(decision, apply_atomic(obs, decision), obs)
        self.assertEqual(self.controller.snapshot()["status"], "completed")
        self.assertEqual(obs["counts"].get("wooden_pickaxe", 0), 1)
        self.assertTrue(any(row["name"] == "crafting_table" for row in obs["blocks"]))

    def test_starter_board_uses_visible_alternative_instead_of_occluded_nearest_log(self):
        obs = world()
        obs["blocks"] = [{"name": "oak_log", "pos": [2, 64, 0], "can_see": False},
                         {"name": "birch_log", "pos": [3, 64, 1], "can_see": True}]
        self.start({"task": "starter_tools"}, obs)
        decision = self.controller.next_action(obs)
        self.assertEqual(decision.name, "gather_block")
        self.assertEqual(decision.args["expected_name"], "birch_log")

    def test_owned_stripped_and_bark_wood_can_supply_planks_without_more_gathering(self):
        for owned in ("stripped_birch_log", "birch_wood", "stripped_birch_wood"):
            for item in ("birch_planks", "wooden_pickaxe"):
                with self.subTest(owned=owned, target=item):
                    obs = world({owned: 1})
                    obs["blocks"] = []
                    self.start({"task": "craft", "item": item, "count": 1}, obs)
                    decision = self.controller.next_action(obs)
                    self.assertEqual(decision.name, "craft_item")
                    self.assertEqual(decision.args["item"], "birch_planks")

    def test_species_specific_planks_do_not_collect_unrelated_wood_drops(self):
        obs = world()
        obs["blocks"] = [{"name": "birch_log", "pos": [2, 64, 0], "can_see": True}]
        obs["entities"] = [{"id": 7, "name": "item", "item": "oak_logx8", "pos": [1.5, 64, .5]}]
        self.start({"task": "craft", "item": "birch_planks", "count": 4}, obs)
        decision = self.controller.next_action(obs)
        self.assertEqual(decision.name, "gather_block")
        self.assertEqual(decision.args["expected_name"], "birch_log")

    def bootstrap_world(self, counts=None):
        obs = world(counts, health=6, food=17)
        obs["footing_origin"] = list(obs["status"]["pos"])
        obs["footing"] = {"pos": [0, 63, 0], "name": "grass_block", "properties": {}, **verified()}
        for row in obs["blocks"]:
            row["can_dig"] = True
        context = {"food_recovery": {"needs_ai": True}, "cautious_bootstrap": True,
                   "survival_constraints": {"scenario": "cautious_bootstrap", "model_allowed": True}}
        return obs, context

    def test_cautious_bootstrap_requires_trusted_marker_and_fresh_matching_policy(self):
        for update, change in (({"cautious_bootstrap": False}, {}),
                ({"survival_constraints": {"scenario": "normal"}}, {}),
                ({"food_recovery": {}}, {}), ({}, {"health": 4}), ({}, {"food": 11}),
                ({}, {"is_day": False}), ({}, {"in_water": True}),
                ({}, {"nearest_hostile": {"name": "zombie", "dist": 10}})):
            with self.subTest(context=update, status=change):
                obs, context = self.bootstrap_world()
                obs["status"].update(change)
                context.update(update)
                self.start(obs=obs)
                self.assertIsNone(self.controller.next_action(obs, context))
                self.assertEqual(self.controller.snapshot()["status"], "suspended")
                self.assertIsNone(self.controller.state()["pending_digest"])

    def test_cautious_bootstrap_only_selects_close_visible_reachable_logs(self):
        obs, context = self.bootstrap_world()
        obs["blocks"] = [{"name": "oak_log", "pos": [2, 64, 0], "can_see": False, "can_dig": True,
                          "visibility": {"blocker": [1, 65, 0]}},
                         {"name": "oak_leaves", "pos": [1, 65, 0], "fresh": True, "can_see": True},
                         {"name": "birch_log", "pos": [0, 64, 2], "can_see": True, "can_dig": False},
                         {"name": "spruce_log", "pos": [4, 64, 0], "can_see": True, "can_dig": True},
                         {"name": "acacia_log", "pos": [-2, 64, 0], "can_see": True, "can_dig": True}]
        obs["entities"] = [{"id": 7, "name": "item", "item": "oak_logx8", "pos": [1.5, 64, .5]}]
        self.start(obs=obs)
        decision = self.controller.next_action(obs, context)
        self.assertEqual((decision.name, decision.args["expected_name"]), ("gather_block", "acacia_log"))

    def test_cautious_bootstrap_never_explores_or_clears_when_no_close_log(self):
        obs, context = self.bootstrap_world()
        obs["blocks"] = [{"name": "oak_log", "pos": [4, 64, 0], "can_see": True, "can_dig": True}]
        obs["local_geometry_origin"] = list(obs["status"]["pos"])
        obs["safe_stand_candidates"] = [{"pos": [1.5, 64, .5], "support": "grass_block"}]
        self.start(obs=obs)
        self.assertIsNone(self.controller.next_action(obs, context))
        self.assertEqual(self.controller.snapshot()["status"], "needs_ai")
        self.assertEqual(self.controller.snapshot()["steps"], 0)

    def test_cautious_bootstrap_rejects_broader_goal_even_with_trusted_context(self):
        for args in ({"task": "gather", "resource": "logs", "count": 9},
                     {"task": "gather", "resource": "cobblestone", "count": 1},
                     {"task": "craft", "item": "stone_pickaxe", "count": 1},
                     {"task": "starter_tools"}):
            with self.subTest(args=args):
                obs, context = self.bootstrap_world()
                self.start(args, obs)
                self.assertIsNone(self.controller.next_action(obs, context))
                self.assertEqual(self.controller.snapshot()["reason"], "cautious_bootstrap_goal_not_allowed")

    def test_cautious_bootstrap_workspace_uses_only_fresh_close_placement(self):
        obs, context = self.bootstrap_world({"oak_planks": 3, "stick": 2, "crafting_table": 1})
        obs["blocks"] = []
        obs["local_geometry_origin"] = list(obs["status"]["pos"])
        obs["placement_candidates"] = [{"pos": [0, 64, 1], "support": "grass_block"}]
        self.start({"task": "craft", "item": "wooden_pickaxe", "count": 1}, obs)
        first = self.controller.next_action(obs, context)
        self.assertEqual((first.name, first.args), ("place_item", {"item": "crafting_table", "pos": [0, 64, 1]}))
        self.controller.record_result(first, apply_atomic(obs, first), obs)
        second = self.controller.next_action(obs, context)
        self.assertEqual(second.name, "craft_item")
        self.controller.record_result(second, apply_atomic(obs, second), obs)
        self.assertTrue(self.controller.confirm_completion(obs, context))
        self.assertEqual(obs["counts"]["wooden_pickaxe"], 1)

    def test_cautious_bootstrap_refuses_unknown_footing_or_saturated_entities(self):
        for invalid in ("footing", "entities"):
            with self.subTest(invalid=invalid):
                obs, context = self.bootstrap_world()
                if invalid == "footing":
                    obs.pop("footing")
                else:
                    obs["entities"] = [{"id": i, "name": "item", "pos": [1, 64, 1]} for i in range(40)]
                self.start(obs=obs)
                self.assertIsNone(self.controller.next_action(obs, context))
                self.assertEqual(self.controller.snapshot()["status"], "suspended")

    def test_workstation_path_failure_switches_log_locally_without_repeating_target(self):
        obs = world({"oak_planks": 3, "stick": 2})
        self.start({"task": "craft", "item": "wooden_pickaxe", "count": 1}, obs)
        first = self.controller.next_action(obs)
        self.controller.record_result(first, SkillResult(False, "No path", {"executed": False}, "stalled"), obs)
        second = self.controller.next_action(obs)
        self.assertEqual(second.name, "gather_block")
        self.assertNotEqual(first.args["pos"], second.args["pos"])
        self.assertEqual(self.controller.snapshot()["ai_escalations"], 0)

    def test_nearest_type_terrain_summary_never_becomes_clearable_foliage(self):
        obs = world(pos=[4, 124, 6.6])
        obs["blocks"] = []
        obs["terrain"] = [{"name": "oak_leaves", "pos": [4, 123, 7], "count": 658}]
        self.start(obs=obs)
        self.assertIsNone(self.controller.next_action(obs))
        self.assertEqual(self.controller.snapshot()["status"], "needs_ai")

    def test_safe_needed_drops_precede_producing_more_material(self):
        self.start({"task": "gather", "resource": "logs", "count": 2})
        self.obs["entities"] = [{"id": 7, "name": "item", "item": "oak_logx2", "pos": [1.5, 64, .5]}]
        self.assertEqual(self.act().name, "collect_drops")
        self.assertIsNone(self.controller.next_action(self.obs))
        self.assertEqual(self.controller.snapshot()["status"], "completed")

    def test_craft_stone_pickaxe_from_empty_builds_prerequisites_one_atomic_child_at_a_time(self):
        obs = world()
        obs["blocks"] = ([{"name": "oak_log", "pos": [2, 64, z], "can_see": True} for z in range(4)]
                         + [{"name": "stone", "pos": [4, 64, z], "can_see": True} for z in range(4)])
        self.start({"task": "craft", "item": "stone_pickaxe", "count": 1}, obs)
        actions = []
        for _ in range(40):
            decision = self.controller.next_action(obs)
            if decision is None:
                break
            actions.append((decision.name, decision.args.get("item")))
            self.controller.record_result(decision, apply_atomic(obs, decision), obs)
        self.assertEqual(self.controller.snapshot()["status"], "completed", self.controller.snapshot())
        self.assertEqual(obs["counts"]["stone_pickaxe"], 1)
        self.assertIn(("craft_item", "wooden_pickaxe"), actions)
        self.assertIn(("place_item", "crafting_table"), actions)
        self.assertNotIn("gather_materials", [name for name, _ in actions])

    def test_smelting_start_wait_collect_uses_slots_and_actual_output_inventory(self):
        pos = [2, 64, 0]
        obs = world({"raw_iron": 2, "coal": 1})
        obs["blocks"] = [{"name": "furnace", "pos": pos}]
        obs["furnaces"] = [verified(pos=pos, name="furnace", input=None, fuel=None, output=None, burning=False)]
        self.start({"task": "smelt", "item": "iron_ingot", "count": 2}, obs, max_no_progress_steps=2)
        decision = self.controller.next_action(obs)
        self.assertEqual(decision.name, "smelt_start")
        obs["counts"].update(raw_iron=0, coal=0)
        obs["furnaces"][0].update(input={"name": "raw_iron", "count": 2}, burning=True)
        self.controller.record_result(decision, SkillResult(True, "Loaded only", {"verified": True, "stage": "started", "completed": False}), obs)
        self.assertEqual(self.controller.snapshot()["progress"]["current"], 0)
        for _ in range(4):
            decision = self.controller.next_action(obs)
            self.assertEqual(decision.name, "wait")
            self.controller.record_result(decision, SkillResult(True, "Waiting"), obs)
        obs["furnaces"][0].update(input=None, burning=False, output={"name": "iron_ingot", "count": 2})
        decision = self.controller.next_action(obs)
        self.assertEqual(decision.name, "smelt_collect")
        obs["counts"]["iron_ingot"] = 2
        obs["furnaces"][0]["output"] = None
        self.controller.record_result(decision, SkillResult(True, "Actual output", {"verified": True, "gained": 2}), obs)
        self.assertIsNone(self.controller.next_action(obs))
        self.assertEqual(self.controller.snapshot()["status"], "completed")
        self.assertEqual(self.controller.state()["jobs"], [])

    def test_saved_smelting_job_only_inspects_when_current_window_missing(self):
        obs = world({"raw_iron": 1, "coal": 1})
        pos = [2, 64, 0]
        obs["blocks"] = [{"name": "furnace", "pos": pos}]
        obs["furnaces"] = [verified(pos=pos, input=None, fuel=None, output=None, burning=False)]
        controller = self.start({"task": "smelt", "item": "iron_ingot", "count": 1}, obs)
        decision = controller.next_action(obs)
        obs["counts"] = {}
        obs["furnaces"] = []
        controller.record_result(decision, SkillResult(True, "Loaded", {"verified": True}), obs)
        restored = AutonomousTaskController(saved=controller.state())
        self.assertEqual(restored.next_action(obs).name, "inspect_container")

    def test_unknown_child_or_external_unknown_never_replans_even_after_inventory_changes(self):
        for error, data in (("request_timeout", {}), ("custom", {"action_state": "unknown"})):
            with self.subTest(error=error):
                controller = self.start()
                decision = controller.next_action(self.obs)
                apply_atomic(self.obs, decision)
                controller.record_result(decision, SkillResult(False, "Unknown", data, error), self.obs)
                self.assertEqual(controller.snapshot()["status"], "unknown")
                self.assertIsNone(controller.next_action(self.obs))
                with self.assertRaises(ValueError):
                    controller.start({"task": "food"}, self.obs)
        controller = self.start()
        controller.stop("Unknown emergency write", unknown=True)
        self.assertEqual(controller.snapshot()["status"], "unknown")
        self.assertIsNone(controller.next_action(self.obs))

    def test_pending_saved_child_is_unknown_not_replayed_on_restart(self):
        controller = self.start()
        controller.next_action(self.obs)
        restored = AutonomousTaskController(saved=controller.state())
        self.assertEqual(restored.snapshot()["status"], "unknown")
        self.assertIsNone(restored.next_action(self.obs))

    def test_lifecycle_and_dimension_change_stop_the_old_incremental_target(self):
        for change in ({"last_death": {"t": 12}}, {"alive": False}, {"dimension": "the_nether"}):
            with self.subTest(change=change):
                controller = self.start()
                self.obs["status"].update(change)
                self.assertIsNone(controller.next_action(self.obs))
                self.assertEqual(controller.snapshot()["status"], "needs_ai")
                self.assertEqual(controller.snapshot()["last_escalation_reason"], "life_or_dimension_changed")

    def test_partial_danger_and_night_suspend_then_fresh_day_replans(self):
        controller = self.start()
        original_id = controller.snapshot()["task_id"]
        partial = copy.deepcopy(self.obs)
        partial.update(observation_scope="safety", inventory_fresh=False, counts={})
        self.assertIsNone(controller.next_action(partial))
        self.assertEqual(controller.snapshot()["status"], "suspended")
        self.obs["status"]["is_day"] = False
        self.assertIsNone(controller.next_action(self.obs))
        self.obs["status"]["is_day"] = True
        self.assertIsNotNone(controller.next_action(self.obs))
        self.assertEqual(controller.snapshot()["task_id"], original_id)

    def test_only_fresh_same_level_supported_local_stance_can_be_exploration(self):
        obs = world()
        obs["blocks"] = []
        obs["local_geometry_origin"] = obs["status"]["pos"]
        obs["safe_stand_candidates"] = [{"pos": [2.5, 64, .5], "support": "stone"}]
        controller = self.start(obs=obs, max_explore_steps=1)
        self.assertEqual(self.act().name, "position_for_task")
        obs["local_geometry_origin"] = obs["status"]["pos"]
        obs["safe_stand_candidates"] = [{"pos": [3.5, 64, .5], "support": "stone"}]
        self.assertIsNone(controller.next_action(obs))
        self.assertEqual(controller.snapshot()["status"], "needs_ai")
        self.assertEqual(controller.state()["explore_steps"], 1)

    def test_material_held_does_not_prove_shelter_bed_storage_or_farm_complete(self):
        for task in ("shelter", "bed", "storage", "farm"):
            with self.subTest(task=task):
                obs = world({"oak_planks": 64, "white_bed": 1, "chest": 1, "wheat_seeds": 8})
                controller = self.start({"task": task}, obs)
                controller.next_action(obs)
                self.assertNotEqual(controller.snapshot()["status"], "completed")

    def test_fresh_structure_evidence_completes_shelter_without_replaying_build(self):
        obs = world()
        obs["daily_evidence"]["shelter"] = verified(pos=[0, 64, 0], shelter_verified=True)
        controller = self.start({"task": "shelter"}, obs)
        self.assertIsNone(controller.next_action(obs))
        self.assertEqual(controller.snapshot()["status"], "completed")

    def test_recover_drops_completes_only_verified_inventory_recovery_not_disappearance(self):
        obs = world()
        obs["entities"] = [{"id": 10, "name": "item", "item": "iron_ingotx3", "pos": [1.5, 64, .5]}]
        controller = self.start({"task": "recover_drops"}, obs)
        self.assertEqual(self.act().name, "collect_drops")
        self.assertIsNone(controller.next_action(obs))
        self.assertEqual(controller.snapshot()["status"], "completed")
        self.assertEqual(obs["counts"]["iron_ingot"], 3)

    def test_wall_clock_and_child_step_limits_end_in_needs_ai(self):
        with patch("core.autonomous_tasks.time.time", return_value=100):
            controller = self.start(max_seconds=.5)
        with patch("core.autonomous_tasks.time.time", return_value=101):
            self.assertIsNone(controller.next_action(self.obs))
        self.assertEqual(controller.snapshot()["last_escalation_reason"], "task_budget_exhausted")
        controller = self.start(max_steps=1)
        self.act()
        self.assertIsNone(controller.next_action(self.obs))
        self.assertEqual(controller.snapshot()["status"], "needs_ai")

    def test_last_allowed_child_can_complete_goal_without_requesting_another_write(self):
        controller = self.start({"task": "gather", "resource": "logs", "count": 1}, max_steps=1)
        self.act()
        self.assertIsNone(controller.next_action(self.obs))
        self.assertEqual(controller.snapshot()["status"], "completed")
        self.assertEqual(controller.snapshot()["steps"], 1)

    def test_confirm_completion_settles_last_child_without_next_action_or_pending_write(self):
        controller = self.start({"task": "gather", "resource": "logs", "count": 1}, max_steps=1)
        self.act()
        self.assertEqual(controller.snapshot()["status"], "running")
        with patch.object(controller, "next_action", side_effect=AssertionError("No new child allowed")), \
                patch.object(controller, "_gather", side_effect=AssertionError("No new gather allowed")):
            self.assertTrue(controller.confirm_completion(self.obs))
        self.assertEqual(controller.snapshot()["status"], "completed")
        self.assertEqual(controller.snapshot()["steps"], 1)
        self.assertIsNone(controller.state()["pending_digest"])

    def test_confirm_completion_rejects_unknown_partial_pending_and_changed_life(self):
        controller = self.start({"task": "gather", "resource": "logs", "count": 1})
        self.act()
        for patch_obs, patch_status, context in (({"fresh": False}, {}, None),
                ({"observation_scope": "safety"}, {}, None), ({"inventory_fresh": False}, {}, None),
                ({}, {"alive": False}, None), ({}, {"last_death": {"t": 9}}, None),
                ({}, {"dimension": "the_nether"}, None), ({}, {"busy": "dig"}, None),
                ({}, {}, {"pending": {"decision": "unresolved"}}),
                ({}, {}, {"last_result": {"data": {"action_state": "unknown"}}})):
            with self.subTest(obs=patch_obs, status=patch_status, context=context):
                obs = {**copy.deepcopy(self.obs), **patch_obs}
                obs["status"].update(patch_status)
                self.assertFalse(controller.confirm_completion(obs, context))
                self.assertEqual(controller.snapshot()["status"], "running")
        controller.stop("unknown safety action", unknown=True)
        self.assertFalse(controller.confirm_completion(self.obs))
        self.assertEqual(controller.snapshot()["status"], "unknown")

    def test_confirm_completion_requires_physical_goal_evidence_not_held_shelter_material(self):
        controller = self.start({"task": "shelter"}, world({"oak_planks": 64}))
        self.assertFalse(controller.confirm_completion(self.obs))
        self.obs["daily_evidence"]["shelter"] = verified(pos=[0, 64, 0], shelter_verified=True)
        self.assertTrue(controller.confirm_completion(self.obs))
        self.assertIsNone(controller.state()["pending_digest"])

    def test_food_task_builds_cooking_prerequisites_for_owned_raw_meat(self):
        obs = world({"beef": 2, "cobblestone": 8})
        obs["blocks"].append({"name": "crafting_table", "pos": [1, 64, 0]})
        controller = self.start({"task": "food", "count": 2}, obs)
        decision = controller.next_action(obs)
        self.assertEqual((decision.name, decision.args), ("craft_item", {"item": "furnace", "count": 1}))

    def test_food_task_prepares_a_weapon_before_hunting_observed_animals(self):
        obs = world({"cobblestone": 2, "stick": 1})
        obs["blocks"].append({"name": "crafting_table", "pos": [1, 64, 0]})
        obs["entities"] = [{"id": 7, "name": "cow", "type": "animal", "pos": [2, 64, 0]}]
        controller = self.start({"task": "food"}, obs)
        decision = controller.next_action(obs)
        self.assertEqual((decision.name, decision.args), ("craft_item", {"item": "stone_sword", "count": 1}))

    def test_occlusion_failure_can_clear_newly_observed_leaf_then_release_resource_cooldown(self):
        obs = world()
        obs["blocks"] = [{"name": "oak_log", "pos": [2, 64, 0]}]
        controller = self.start({"task": "gather", "resource": "logs", "count": 1}, obs)
        decision = controller.next_action(obs)
        controller.record_result(decision, SkillResult(False, "Blocked", {}, "target_occluded"), obs)
        obs["blocks"][0].update(can_see=False, visibility={"blocker": [1, 64, 0]})
        obs["blocks"].append({"name": "oak_leaves", "pos": [1, 64, 0], "fresh": True, "can_see": True})
        self.assertEqual(self.act().name, "clear_foliage")
        self.assertEqual(self.act().name, "gather_block")
        self.assertIsNone(controller.next_action(obs))
        self.assertEqual(controller.snapshot()["status"], "completed")

    def test_bed_and_storage_missing_planks_continue_from_held_logs(self):
        for task, extra in (("bed", {"white_wool": 3}), ("storage", {})):
            with self.subTest(task=task):
                obs = world({"oak_log": 2, **extra})
                obs["daily_evidence"] = {"base_site": verified(pos=[0, 64, 0]),
                    "shelter": verified(pos=[0, 64, 0], shelter_verified=True)}
                controller = self.start({"task": task}, obs)
                decision = controller.next_action(obs)
                self.assertEqual(decision.name, "craft_item")
                self.assertEqual(decision.args["item"], "oak_planks")

    def test_specific_fish_smelting_never_substitutes_unrelated_bread_or_unbound_fishing(self):
        obs = world({"wheat": 3, "coal": 1})
        obs["blocks"] = [{"name": "furnace", "pos": [2, 64, 0]},
                         {"name": "crafting_table", "pos": [1, 64, 0]}]
        controller = self.start({"task": "smelt", "item": "cooked_cod", "count": 1}, obs)
        self.assertIsNone(controller.next_action(obs))
        self.assertEqual(controller.snapshot()["status"], "needs_ai")

    def test_failed_animal_target_is_replaced_locally_without_repeating_the_same_id(self):
        obs = world({"stone_sword": 1})
        obs["entities"] = [{"id": i, "name": "cow", "type": "animal", "pos": [2, 64, z]}
                           for i, z in ((7, 0), (8, 1))]
        controller = self.start({"task": "food", "count": 1}, obs)
        first = controller.next_action(obs)
        self.assertEqual(first.name, "hunt_animal")
        controller.record_result(first, SkillResult(False, "Animal moved", {}, "out_of_range"), obs)
        second = controller.next_action(obs)
        self.assertEqual(second.name, "hunt_animal")
        self.assertNotEqual(first.args["entity_id"], second.args["entity_id"])

    def test_explicit_cancellation_clears_only_task_digest_and_does_not_replay(self):
        controller = self.start()
        controller.next_action(self.obs)
        controller.stop("Explicit fresh resume abandons old task", cancelled=True)
        self.assertEqual(controller.snapshot()["status"], "cancelled")
        self.assertIsNone(controller.state()["pending_digest"])
        self.assertIsNone(controller.next_action(self.obs))
        controller.start({"task": "food"}, self.obs)
        self.assertEqual(controller.snapshot()["status"], "running")

    def test_snapshot_is_bounded_and_copies_do_not_mutate_controller(self):
        controller = self.start()
        self.assertTrue(CRAFT_ITEMS and SMELT_ITEMS)
        summary = controller.snapshot()
        self.assertLess(len(json.dumps(summary).encode()), 4096)
        self.assertFalse({"baseline", "life", "pending_digest", "blocked"} & summary.keys())
        summary["args"]["count"] = 999
        self.assertEqual(controller.snapshot()["args"]["count"], 2)

    def test_unknown_goal_requires_explicit_complete_idle_reobservation_to_abandon(self):
        controller = self.start()
        decision = controller.next_action(self.obs)
        controller.record_result(decision, SkillResult(False, "Unknown hit", {"action_state": "unknown"}, "custom"), self.obs)
        evidence = controller.snapshot()["recent_results"]
        controller.stop("ordinary cancellation", cancelled=True)
        self.assertEqual(controller.snapshot()["status"], "unknown")
        for change in ({"fresh": False}, {"observation_scope": "safety"}, {"inventory_fresh": False}, {"counts": None}):
            candidate = {**self.obs, **change}
            self.assertFalse(controller.abandon_after_reobserve(candidate))
        for change in ({"ready": False}, {"alive": False}, {"busy": "dig"}):
            candidate = copy.deepcopy(self.obs)
            candidate["status"].update(change)
            self.assertFalse(controller.abandon_after_reobserve(candidate))
        self.assertEqual(controller.snapshot()["status"], "unknown")
        self.assertTrue(controller.abandon_after_reobserve(self.obs))
        self.assertEqual(controller.snapshot()["status"], "cancelled")
        self.assertEqual(controller.snapshot()["recent_results"], evidence)
        self.assertIsNone(controller.next_action(self.obs))
        controller.start({"task": "food"}, self.obs)
        self.assertEqual(controller.snapshot()["status"], "running")

    def test_explicit_reobservation_can_abandon_a_saved_pending_hash_without_replay(self):
        controller = self.start()
        controller.next_action(self.obs)
        restored = AutonomousTaskController(saved=controller.state())
        self.assertTrue(restored.abandon_after_reobserve(self.obs))
        self.assertIsNone(restored.state()["pending_digest"])
        self.assertEqual(restored.snapshot()["status"], "cancelled")

    def test_life_generation_persists_and_legacy_state_defaults_to_zero(self):
        controller = AutonomousTaskController()
        controller.start({"task": "gather", "resource": "logs", "count": 2}, world(), {"life_generation": 3})
        restored = AutonomousTaskController(saved=controller.state())
        self.assertEqual(restored.state()["life_generation"], 3)
        self.assertIsNotNone(restored.next_action(world(), {"life_generation": 3}))
        legacy = controller.state()
        legacy.pop("life_generation")
        restored = AutonomousTaskController(saved=legacy)
        self.assertEqual(restored.state()["life_generation"], 0)
        self.assertIsNone(restored.next_action(world(), {"life_generation": 1}))
        self.assertEqual(restored.snapshot()["status"], "needs_ai")

    def test_new_generation_neither_uses_old_baseline_nor_confirms_completion(self):
        for method in ("next_action", "confirm_completion"):
            with self.subTest(method=method):
                controller = self.start(obs=world({"oak_log": 10}))
                self.assertEqual(controller.snapshot()["progress"]["target"], 12)
                controller.interrupt("Dead; waiting to respawn")
                result = getattr(controller, method)(world({"oak_log": 12}), {"life_generation": 1})
                self.assertFalse(result)
                self.assertEqual(controller.snapshot()["status"], "needs_ai")
                self.assertEqual(controller.snapshot()["reason"], "life_or_dimension_changed")
                self.assertIsNone(controller.state()["pending_digest"])

    def test_generation_change_preserves_pending_and_unknown_result_priority(self):
        controller = self.start()
        decision = controller.next_action(self.obs)
        pending = controller.state()["pending_digest"]
        self.assertIsNone(controller.next_action(self.obs, {"life_generation": 1}))
        self.assertFalse(controller.confirm_completion(self.obs, {"life_generation": 1}))
        self.assertEqual(controller.state()["pending_digest"], pending)
        controller.record_result(decision, SkillResult(False, "Unknown after death", {
            "executed": True, "action_state": "unknown"}, "action_unconfirmed"),
            self.obs, context={"life_generation": 1})
        self.assertEqual(controller.snapshot()["status"], "unknown")
        self.assertIsNone(controller.next_action(self.obs, {"life_generation": 1}))
        self.assertFalse(controller.confirm_completion(self.obs, {"life_generation": 1}))
        self.assertEqual(controller.snapshot()["status"], "unknown")

    def test_known_child_result_in_new_generation_escalates_without_progress_credit(self):
        controller = self.start()
        decision = controller.next_action(self.obs)
        self.obs["counts"]["oak_log"] = 2
        controller.record_result(decision, SkillResult(True, "Acknowledged", {"executed": True}),
                                 self.obs, context={"life_generation": 1})
        self.assertEqual(controller.snapshot()["status"], "needs_ai")
        self.assertEqual(controller.snapshot()["progress"]["current"], 0)
        self.assertEqual(controller.snapshot()["recent_results"][-1]["name"], decision.name)

    def test_gather_diamond_ensures_second_iron_pickaxe_then_dispatches_deep_mine(self):
        obs = world({"iron_pickaxe": 1, "iron_ingot": 3, "stick": 2})
        obs["blocks"].append({"name": "crafting_table", "pos": [1, 64, 0], "can_see": True})
        controller = self.start({"task": "gather", "resource": "diamond", "count": 2}, obs=obs)
        ctx = {"deep_mining_enabled": True}
        first = controller.next_action(obs, ctx)
        self.assertEqual((first.name, first.args["item"]), ("craft_item", "iron_pickaxe"))
        obs["counts"]["iron_pickaxe"] = 2
        obs["counts"]["iron_ingot"] -= 3
        obs["counts"]["stick"] -= 2
        controller.record_result(first, SkillResult(True, "Crafted", {"executed": True}), obs, context=ctx)
        second = controller.next_action(obs, ctx)
        self.assertEqual(second.goal, "deep_mine")
        self.assertEqual(controller.state()["pending_deep_mine"]["resource"], "diamond")
        obs["counts"]["diamond"] = 2
        controller.record_result(
            second,
            SkillResult(True, "Mined 2 diamond", {"executed": True, "gained": {"diamond": 2}}),
            obs,
            context=ctx,
        )
        self.assertIsNone(controller.next_action(obs, ctx))
        self.assertEqual(controller.snapshot()["status"], "completed")

    def test_deep_mine_unknown_result_freezes_without_replay(self):
        obs = world({"iron_pickaxe": 2})
        controller = self.start({"task": "gather", "resource": "diamond", "count": 1}, obs=obs)
        ctx = {"deep_mining_enabled": True}
        decision = controller.next_action(obs, ctx)
        self.assertEqual(decision.goal, "deep_mine")
        controller.record_result(
            decision,
            SkillResult(False, "Uncertain", {"executed": True, "action_state": "unknown"}, "action_unconfirmed"),
            obs,
            context=ctx,
        )
        self.assertEqual(controller.snapshot()["status"], "unknown")
        self.assertIsNone(controller.next_action(obs, ctx))
        self.assertFalse(controller.confirm_completion(obs, ctx))

    def test_gather_cobblestone_falls_back_to_shallow_shaft_when_no_surface_stone_visible(self):
        obs = world({"wooden_pickaxe": 1})
        obs["blocks"] = [{"name": "stone", "pos": [2, 48, 0], "can_see": False}]
        controller = self.start({"task": "gather", "resource": "cobblestone", "count": 8}, obs=obs)
        ctx = {"deep_mining_enabled": True}
        decision = controller.next_action(obs, ctx)
        self.assertEqual(decision.goal, "deep_mine")
        self.assertEqual(controller.state()["pending_deep_mine"]["resource"], "cobblestone")

    def test_workspace_prefers_fresh_placement_candidates_over_blind_offsets(self):
        obs = world({"crafting_table": 1, "birch_planks": 4, "stick": 2})
        obs["placement_candidates"] = [{"pos": [1, 64, 1], "support": "grass_block"}]
        controller = self.start({"task": "craft", "item": "wooden_pickaxe", "count": 1}, obs=obs)
        decision = controller.next_action(obs)
        self.assertEqual((decision.name, decision.args["pos"]), ("place_item", [1, 64, 1]))

    def test_starter_tools_dispatches_shallow_cobblestone_shaft_instead_of_wandering(self):
        obs = world({"wooden_pickaxe": 1, "stick": 2, "birch_planks": 3})
        obs["daily_tasks"] = {
            "phase": "establish",
            "active_task_id": "starter_tools",
            "tasks": [{
                "id": "starter_tools",
                "status": "ready",
                "missing": ["stone_pickaxe", "stone_axe", "stone_sword"],
                "next_action": {"name": "move_to", "args": {"pos": [30, 64, 0], "range": 2}, "goal": "starter_tools"},
            }],
        }
        controller = self.start({"task": "starter_tools"}, obs=obs)
        decision = controller.next_action(obs, {"deep_mining_enabled": True})
        self.assertEqual(decision.goal, "deep_mine")
        self.assertEqual(controller.state()["pending_deep_mine"]["resource"], "cobblestone")

    def test_smelt_uses_immediate_inspect_container_snapshot_without_looping(self):
        obs = world({"raw_iron": 3, "coal": 2})
        obs["status"]["pos"] = [0.5, 64.0, 3.2]
        obs["blocks"].append({"name": "furnace", "pos": [0, 66, 0], "can_see": True})
        obs["furnaces"] = []
        controller = self.start({"task": "smelt", "item": "iron_ingot", "count": 1}, obs=obs)
        first = controller.next_action(obs)
        self.assertEqual((first.name, first.args["pos"]), ("inspect_container", [0, 66, 0]))
        controller.record_result(
            first,
            SkillResult(True, "inspect_container verified", {
                "executed": True,
                "verified": True,
                "pos": [0, 66, 0],
                "window": {
                    "window": "furnace",
                    "target": "furnace",
                    "sections": {"input": [], "fuel": [], "output": []},
                    "info": {"burning": False},
                },
            }),
            obs,
        )
        second = controller.next_action(obs)
        self.assertEqual(second.name, "smelt_start")
        self.assertEqual(second.args["pos"], [0, 66, 0])

    def test_gather_diamond_stashes_excess_cobblestone_when_inventory_is_crowded(self):
        obs = world({"iron_pickaxe": 2, "cobblestone": 576, "diorite": 35})
        obs["inventory"] = [{"name": "cobblestone", "count": 64, "slot": i} for i in range(9)] + [
            {"name": "diorite", "count": 35, "slot": 9 + i} for i in range(21)
        ]
        obs["blocks"].append({"name": "chest", "pos": [1, 64, 0], "can_see": True})
        controller = self.start({"task": "gather", "resource": "diamond", "count": 1}, obs=obs)
        decision = controller.next_action(obs, {"deep_mining_enabled": True})
        self.assertEqual(decision.name, "store_items")
        self.assertEqual(decision.args["item"], "diorite")

    def test_gather_logs_prefers_reachable_trunk_logs_over_high_canopy_logs(self):
        obs = world({})
        obs["blocks"] = [
            {"name": "oak_log", "pos": [2, 67, 0], "can_see": True},
            {"name": "oak_log", "pos": [3, 65, 0], "can_see": True},
        ]
        controller = self.start({"task": "gather", "resource": "logs", "count": 2}, obs=obs)
        decision = controller.next_action(obs)
        self.assertEqual(decision.name, "gather_block")
        self.assertEqual(decision.args["pos"], [3, 65, 0])

    def test_desert_dead_bush_yields_sticks_and_opportunistic_sleep_allows_night_work(self):
        from skills.survival import SurvivalSkills
        self.assertIn("sand", SurvivalSkills.SUPPORT)
        self.assertIn("sandstone", SurvivalSkills.SUPPORT)
        self.assertIn("dead_bush", SurvivalSkills.GATHERABLE)
        obs = world({})
        obs["status"].update(is_day=False, time_of_day=14000, thundering=True)
        obs["blocks"] = [
            {"name": "dead_bush", "pos": [2, 64, 0], "can_see": True, "can_dig": True},
            {"name": "crafting_table", "pos": [1, 64, 0], "can_see": True},
        ]
        # No logs or planks in desert: craft stick harvests nearby dead_bush directly.
        obs["counts"] = {}
        controller = AutonomousTaskController({"autonomy": {"enabled": True}, "sleep_policy": "opportunistic"})
        controller.start({"task": "craft", "item": "stick", "count": 2}, obs)
        decision = controller.next_action(obs)
        self.assertEqual((decision.name, decision.args["expected_name"]), ("gather_block", "dead_bush"))

    def test_iron_armor_crafts_and_equips_and_scout_completes_in_one_pass(self):
        obs = world({"iron_chestplate": 1, "iron_helmet": 1, "iron_leggings": 1, "iron_boots": 1, "shield": 1})
        controller = self.start({"task": "iron_armor"}, obs=obs)
        step = controller.next_action(obs)
        self.assertEqual((step.name, step.goal), ("equip_armor", "equip_loadout"))
        obs["armor"] = {
            "head": {"name": "iron_helmet", "count": 1},
            "chest": {"name": "iron_chestplate", "count": 1},
            "legs": {"name": "iron_leggings", "count": 1},
            "feet": {"name": "iron_boots", "count": 1},
        }
        obs["status"]["offhand"] = {"name": "shield", "count": 1}
        controller.record_result(step, SkillResult(True, "Equipped", {"executed": True}), obs)
        self.assertTrue(controller.confirm_completion(obs))
        self.assertEqual(controller.snapshot()["status"], "completed")

        scout_obs = world({})
        scout_ctrl = self.start({"task": "scout"}, obs=scout_obs)
        s_step = scout_ctrl.next_action(scout_obs)
        self.assertEqual((s_step.name, s_step.goal), ("move_to", "scout_surface"))
        scout_ctrl.record_result(s_step, SkillResult(True, "Scout done", {"aborted_empty": True}), scout_obs)
        self.assertTrue(scout_ctrl.confirm_completion(scout_obs))
        self.assertEqual(scout_ctrl.snapshot()["status"], "completed")

    def test_nether_portal_gathers_water_and_flint_and_steel_then_builds_portal(self):
        obs = world({"bucket": 1, "iron_ingot": 1, "flint": 1, "cobblestone": 16, "iron_pickaxe": 1})
        obs["blocks"] = [
            {"name": "water", "pos": [2, 63, 0], "can_see": True},
            {"name": "crafting_table", "pos": [1, 64, 0], "can_see": True},
        ]
        ctrl = self.start({"task": "nether_portal"}, obs=obs)
        step1 = ctrl.next_action(obs)
        self.assertEqual((step1.name, step1.args["expected_name"]), ("collect_liquid", "water"))
        obs["counts"].pop("bucket", None)
        obs["counts"]["water_bucket"] = 1
        ctrl.record_result(step1, SkillResult(True, "Water filled", {"executed": True}), obs)

        step2 = ctrl.next_action(obs)
        self.assertEqual((step2.name, step2.args["item"]), ("craft_item", "flint_and_steel"))
        obs["counts"]["flint_and_steel"] = 1
        ctrl.record_result(step2, SkillResult(True, "Crafted flint_and_steel", {"executed": True}), obs)

        step3 = ctrl.next_action(obs)
        self.assertEqual((step3.name, step3.goal), ("move_to", "nether_portal"))
        ctrl.record_result(step3, SkillResult(True, "Portal active", {"portal_active": True, "portal_skill": True}), obs)
        self.assertTrue(ctrl.confirm_completion(obs))
        self.assertEqual(ctrl.snapshot()["status"], "completed")

    def test_craft_missing_crafting_table_and_planks_delegates_to_log_gathering(self):
        obs = world({"cobblestone": 8, "stick": 2, "oak_planks": 3})
        obs["blocks"] = [{"name": "stone", "pos": [1, 63, 0], "can_see": True}]
        ctrl = self.start({"task": "craft", "item": "stone_pickaxe", "count": 1}, obs=obs)
        step = ctrl.next_action(obs, {"remote_log_target": [20, 64, 0]})
        self.assertIsNotNone(step)
        self.assertEqual((step.name, step.goal), ("move_to", "scout_logs"))

    def test_smelt_waits_for_residual_burning_furnace_instead_of_exploring(self):
        obs = world({"raw_iron": 3, "coal": 2})
        obs["blocks"].append({"name": "furnace", "pos": [1, 64, 0], "can_see": True})
        obs["furnaces"] = [{
            "pos": [1, 64, 0],
            "name": "furnace",
            "dimension": "overworld",
            "fresh": True,
            "verified": True,
            "source": "inspect_container",
            "input": {"name": None, "count": 0},
            "fuel": {"name": None, "count": 0},
            "output": {"name": None, "count": 0},
            "burning": True,
        }]
        ctrl = self.start({"task": "smelt", "item": "iron_ingot", "count": 3}, obs=obs)
        step = ctrl.next_action(obs)
        self.assertIsNotNone(step)
        self.assertEqual(step.name, "wait")

    def test_gather_diamond_skips_iron_shaft_loop_when_holding_one_iron_pickaxe_and_zero_iron(self):
        obs = world({"iron_pickaxe": 1, "bucket": 1})
        ctrl = self.start({"task": "gather", "resource": "diamond", "count": 3}, obs=obs)
        step = ctrl.next_action(obs, {"deep_mining_enabled": True})
        self.assertIsNotNone(step)
        self.assertEqual(step.goal, "deep_mine")
        self.assertEqual(ctrl.state()["pending_deep_mine"]["resource"], "diamond")

    def test_gather_diamond_crafts_third_iron_pickaxe_when_spare_ingots_held(self):
        obs = world({"iron_pickaxe": 2, "iron_ingot": 3, "stick": 2})
        obs["blocks"].append({"name": "crafting_table", "pos": [1, 64, 0], "can_see": True})
        ctrl = self.start({"task": "gather", "resource": "diamond", "count": 3}, obs=obs)
        step = ctrl.next_action(obs, {"deep_mining_enabled": True})
        self.assertIsNotNone(step)
        self.assertEqual((step.name, step.args["item"]), ("craft_item", "iron_pickaxe"))

    def test_smelt_places_extra_furnace_for_bulk_raw_iron_when_first_is_burning(self):
        obs = world({"raw_iron": 16, "coal": 3, "furnace": 1})
        obs["blocks"].append({"name": "furnace", "pos": [1, 64, 0], "can_see": True})
        obs["furnaces"] = [{
            "pos": [1, 64, 0], "name": "furnace", "dimension": "overworld",
            "fresh": True, "verified": True, "source": "inspect_container",
            "input": {"name": "raw_iron", "count": 8}, "fuel": {"name": "coal", "count": 1},
            "output": {"name": None, "count": 0}, "burning": True,
        }]
        obs["placement_candidates"] = [{"pos": [2, 64, 0], "support": "grass_block"}]
        ctrl = self.start({"task": "smelt", "item": "iron_ingot", "count": 24}, obs=obs)
        step = ctrl.next_action(obs, {"smelting_jobs": [{"pos": [1, 64, 0], "dimension": "overworld", "expected_output": "iron_ingot"}]})
        self.assertIsNotNone(step)
        self.assertEqual((step.name, step.args["item"], step.args["pos"]), ("place_item", "furnace", [2, 64, 0]))

    def test_raw_iron_mines_visible_surface_stone_for_spare_pickaxes_when_cobble_spent(self):
        obs = world({"stone_pickaxe": 1, "oak_log": 2})
        obs["blocks"] = [
            {"name": "stone", "pos": [2, 64, 0], "can_see": True, "can_dig": True},
            {"name": "crafting_table", "pos": [1, 64, 0], "can_see": True},
        ]
        ctrl = self.start({"task": "gather", "resource": "raw_iron", "count": 24}, obs=obs)
        step = ctrl.next_action(obs, {"deep_mining_enabled": True})
        self.assertIsNotNone(step)
        self.assertEqual((step.name, step.args["expected_name"]), ("gather_block", "stone"))

    def test_gather_logs_excludes_entire_route_failed_trunk_column_and_uses_remote_waypoint(self):
        obs = world({})
        obs["blocks"] = [
            {"name": "oak_log", "pos": [4, 65, 0], "can_see": True, "can_dig": True},
            {"name": "oak_log", "pos": [4, 66, 0], "can_see": True, "can_dig": True},
        ]
        ctrl = self.start({"task": "gather", "resource": "logs", "count": 4}, obs=obs)
        step1 = ctrl.next_action(obs)
        self.assertEqual((step1.name, step1.args["pos"]), ("gather_block", [4, 65, 0]))
        ctrl.record_result(step1, SkillResult(False, "No path", {"target": [4, 65, 0]}, "path_not_found"), obs)
        step2 = ctrl.next_action(obs, {"remote_log_target": [24.0, 64.0, 0.0]})
        self.assertIsNotNone(step2)
        self.assertEqual((step2.name, step2.goal), ("move_to", "scout_logs"))

    def test_smelt_places_extra_furnace_when_only_logs_held_as_fuel(self):
        obs = world({"raw_iron": 16, "oak_log": 3, "furnace": 1})
        obs["blocks"].append({"name": "furnace", "pos": [1, 64, 0], "can_see": True})
        obs["furnaces"] = [{
            "pos": [1, 64, 0], "name": "furnace", "dimension": "overworld",
            "fresh": True, "verified": True, "source": "inspect_container",
            "input": {"name": "raw_iron", "count": 8}, "fuel": {"name": "oak_planks", "count": 4},
            "output": {"name": None, "count": 0}, "burning": True,
        }]
        obs["placement_candidates"] = [{"pos": [2, 64, 0], "support": "grass_block"}]
        ctrl = self.start({"task": "smelt", "item": "iron_ingot", "count": 24}, obs=obs)
        step = ctrl.next_action(obs, {"smelting_jobs": [{"pos": [1, 64, 0], "dimension": "overworld", "expected_output": "iron_ingot"}]})
        self.assertIsNotNone(step)
        self.assertEqual((step.name, step.args["item"], step.args["pos"]), ("place_item", "furnace", [2, 64, 0]))

    def test_scout_does_not_escalate_cooling_down_when_origin_cell_blocked(self):
        obs = world({})
        ctrl = self.start({"task": "scout", "count": 8}, obs=obs)
        step = ctrl.next_action(obs, {"blocked_targets": {"0,64,0": 9999999999.0}})
        self.assertIsNotNone(step)
        self.assertEqual((step.name, step.goal), ("move_to", "scout_surface"))

    def test_craft_does_not_chase_remote_logs_when_existing_table_is_blocked(self):
        obs = world({"cobblestone": 8})
        obs["blocks"] = [{"name": "crafting_table", "pos": [2, 67, 0], "can_see": False}]
        ctrl = self.start({"task": "craft", "item": "furnace", "count": 1}, obs=obs)
        step = ctrl.next_action(obs, {"blocked_targets": {"2,67,0": 9999999999.0}, "remote_log_target": [24, 64, 0]})
        self.assertIsNone(step)
        self.assertEqual(ctrl.snapshot()["status"], "needs_ai")


if __name__ == "__main__":
    unittest.main()
