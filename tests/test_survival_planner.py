"""Pure snapshot planning tests, including a finite resource-chain simulation."""

import copy
import math
import unittest
from types import SimpleNamespace

from agent.survival_planner import Decision, SurvivalPlanner


def block(name, x, y=64, z=0, **properties):
    return {"name": name, "pos": [x, y, z], "properties": properties}


def observation(counts=None, blocks=None, **status_changes):
    return {
        "fresh": True,
        "status": {"ready": True, "alive": True, "busy": None, "gamemode": "survival",
                   "dimension": "overworld", "health": 20, "food": 20, "pos": [0.5, 64, 0.5],
                   "is_day": True, "thundering": False, "on_ground": True,
                   "is_sleeping": False, **status_changes},
        "counts": counts or {}, "inventory": [], "entities": [],
        "blocks": blocks if blocks is not None else [block("oak_log", 3), block("stone", 5, 63)],
    }


class SurvivalPlannerTests(unittest.TestCase):
    def setUp(self):
        self.planner = SurvivalPlanner()

    def test_empty_inventory_starts_with_an_observed_log(self):
        decision = self.planner.choose(observation())
        self.assertIsInstance(decision, Decision)
        self.assertEqual(decision.name, "gather_block")
        self.assertEqual(decision.args, {"pos": [3, 64, 0], "expected_name": "oak_log"})

    def test_held_wood_is_converted_to_specific_planks_in_incremental_count(self):
        decision = self.planner.choose(observation({"spruce_log": 1}))
        self.assertEqual(decision.name, "craft_item")
        self.assertEqual(decision.args, {"item": "spruce_planks", "count": 4})

    def test_missing_sticks_are_crafted_as_a_batch(self):
        decision = self.planner.choose(observation({"oak_planks": 4}))
        self.assertEqual(decision.args, {"item": "stick", "count": 4})

    def test_workspace_is_crafted_when_tool_materials_are_ready(self):
        decision = self.planner.choose(observation({"oak_planks": 8, "stick": 2}))
        self.assertEqual(decision.args, {"item": "crafting_table", "count": 1})

    def test_held_workspace_is_placed_outside_player_column(self):
        decision = self.planner.choose(observation({"crafting_table": 1, "oak_planks": 3, "stick": 2}))
        self.assertEqual(decision.name, "place_item")
        self.assertEqual(decision.args, {"item": "crafting_table", "pos": [2, 64, 0]})

    def test_workspace_placement_skips_known_occupied_or_unsafe_cells(self):
        obs = observation({"crafting_table": 1, "oak_planks": 3, "stick": 2},
                          [block("stone", 2), block("lava", 0, 63, 2)])
        decision = self.planner.choose(obs)
        self.assertEqual(decision.name, "place_item")
        self.assertEqual(decision.args["pos"], [-2, 64, 0])

    def test_existing_distant_workspace_is_approached_before_crafting(self):
        decision = self.planner.choose(observation({"oak_planks": 3, "stick": 2}, [block("crafting_table", 10)]))
        self.assertEqual(decision.name, "move_to")
        self.assertEqual(decision.args, {"pos": [10, 64, 0], "range": 2})
        self.assertNotEqual(decision.goal, "explore")

    def test_wooden_pickaxe_requires_real_materials_and_workspace(self):
        obs = observation({"oak_planks": 3, "stick": 2}, [block("crafting_table", 2)])
        self.assertEqual(self.planner.choose(obs).args, {"item": "wooden_pickaxe", "count": 1})

    def test_stone_is_not_mined_without_a_pickaxe(self):
        decision = self.planner.choose(observation(blocks=[block("stone", 3)]))
        self.assertNotEqual(decision.name, "gather_block")
        self.assertEqual(decision.goal, "explore")

    def test_pickaxe_unlocks_cobblestone_gathering(self):
        decision = self.planner.choose(observation({"wooden_pickaxe": 1, "stick": 2}))
        self.assertEqual(decision.name, "gather_block")
        self.assertEqual(decision.args["expected_name"], "stone")

    def test_stone_tools_and_furnace_follow_inventory_dependencies(self):
        cases = [
            ({"wooden_pickaxe": 1, "cobblestone": 3, "stick": 2}, "stone_pickaxe"),
            ({"stone_pickaxe": 1, "cobblestone": 3, "stick": 2}, "stone_axe"),
            ({"stone_pickaxe": 1, "stone_axe": 1, "cobblestone": 2, "stick": 1}, "stone_sword"),
            ({"stone_pickaxe": 1, "stone_axe": 1, "stone_sword": 1, "cobblestone": 8}, "furnace"),
        ]
        for counts, expected in cases:
            with self.subTest(expected=expected):
                decision = self.planner.choose(observation(counts, [block("crafting_table", 2)]))
                self.assertEqual((decision.name, decision.args), ("craft_item", {"item": expected, "count": 1}))

    def test_complete_means_only_the_explicit_starter_goal(self):
        obs = observation({"stone_pickaxe": 1, "stone_axe": 1, "stone_sword": 1, "furnace": 1})
        decision = self.planner.choose(obs)
        self.assertEqual(decision.name, "goal_complete")
        self.assertIn("full survival is outside", decision.reason)
        self.assertTrue(self.planner.progress(obs)["starter_goal_complete"])

    def test_optional_torches_use_existing_materials_without_coal_expedition(self):
        counts = {"stone_pickaxe": 1, "stone_axe": 1, "stone_sword": 1, "furnace": 1, "coal": 1, "stick": 1}
        self.assertEqual(self.planner.choose(observation(counts)).args, {"item": "torch", "count": 4})
        counts["torch"] = 4
        self.assertEqual(self.planner.choose(observation(counts)).name, "goal_complete")
        counts.pop("torch")
        counts.pop("stick")
        self.assertEqual(self.planner.choose(observation(counts)).name, "goal_complete")

    def test_higher_tier_tools_count_but_gold_does_not_replace_stone(self):
        counts = {"iron_pickaxe": 1, "diamond_axe": 1, "netherite_sword": 1, "furnace": 1}
        self.assertEqual(self.planner.choose(observation(counts)).name, "goal_complete")
        self.assertFalse(self.planner.progress(observation({"golden_pickaxe": 1}))["stone_pickaxe"])

    def test_stale_dead_busy_and_wrong_modes_block_resource_work(self):
        cases = [observation(alive=False, health=0), observation(ready=False), observation(busy="dig"),
                 observation(dimension="the_nether"), observation(gamemode="creative"), observation(health=None)]
        stale = observation()
        stale["fresh"] = False
        cases.append(stale)
        for obs in cases:
            with self.subTest(status=obs.get("status")):
                self.assertEqual(self.planner.choose(obs).name, "wait")

    def test_hazards_and_unstable_footing_do_not_trigger_blind_gathering(self):
        for hazard in ("in_lava", "on_fire", "submerged"):
            with self.subTest(hazard=hazard):
                decision = self.planner.choose(observation(**{hazard: True}))
                self.assertEqual((decision.name, decision.goal), ("wait", "recover"))
        self.assertEqual(self.planner.choose(observation(on_ground=False)).name, "wait")

    def test_hostile_threat_is_prioritized_over_eating_or_crafting(self):
        obs = observation({"bread": 1}, food=10)
        obs["entities"] = [{"type": "hostile", "name": "zombie", "pos": [4, 64, 0]}]
        decision = self.planner.choose(obs)
        self.assertEqual(decision.name, "retreat_from")
        self.assertEqual(decision.args, {"pos": [4, 64, 0], "distance": 8})

    def test_nearest_hostile_without_position_is_not_ignored(self):
        self.assertEqual(self.planner.choose(observation(nearest_hostile={"dist": 3})).name, "wait")

    def test_low_health_uses_safe_food_or_waits(self):
        self.assertEqual(self.planner.choose(observation({"bread": 1}, health=5, food=15)).name, "eat_food")
        self.assertEqual(self.planner.choose(observation(health=5, food=15)).name, "wait")
        self.assertEqual(self.planner.choose(observation({"rotten_flesh": 5}, health=5, food=15)).name, "wait")

    def test_full_hunger_does_not_keep_trying_to_eat(self):
        self.assertEqual(self.planner.choose(observation({"bread": 1}, health=5, food=20)).name, "wait")

    def test_food_acquisition_is_not_invented_for_critical_hunger(self):
        decision = self.planner.choose(observation(food=5))
        self.assertEqual((decision.name, decision.goal), ("wait", "food"))

    def test_night_bed_uses_verified_skill_and_daytime_does_not(self):
        obs = observation(blocks=[block("red_bed", 3)], is_day=False)
        self.assertEqual(self.planner.choose(obs).name, "auto_sleep")
        obs["status"]["is_day"] = True
        self.assertNotEqual(self.planner.choose(obs).name, "auto_sleep")
        self.assertNotEqual(self.planner.choose(obs).name, "goal_complete")

    def test_night_without_free_bed_waits_instead_of_wandering(self):
        obs = observation(blocks=[block("red_bed", 3, occupied=True)], is_day=False)
        decision = self.planner.choose(obs)
        self.assertEqual((decision.name, decision.goal), ("wait", "sleep"))

    def test_same_column_floor_and_overhead_resources_are_excluded(self):
        obs = observation(blocks=[block("oak_log", 0, 63), block("oak_log", 0, 66), block("oak_log", 4)])
        self.assertEqual(self.planner.choose(obs).args["pos"], [4, 64, 0])

    def test_known_hazard_next_to_resource_is_excluded(self):
        obs = observation(blocks=[block("oak_log", 3), block("lava", 3, 64, 1), block("oak_log", 6)])
        self.assertEqual(self.planner.choose(obs).args["pos"], [6, 64, 0])

    def test_blocked_target_is_skipped_but_expired_failure_history_is_not(self):
        obs = observation(blocks=[block("oak_log", 3), block("oak_log", 6)])
        decision = self.planner.choose(obs, {"blocked_targets": ["3.0,64,0"]})
        self.assertEqual(decision.args["pos"], [6, 64, 0])
        decision = self.planner.choose(obs, {"blocked_targets": [], "failures": {"3,64,0": 99}})
        self.assertEqual(decision.args["pos"], [3, 64, 0])

    def test_resource_beside_far_hostile_is_not_selected(self):
        obs = observation(blocks=[block("oak_log", 12), block("oak_log", -5)])
        obs["entities"] = [{"type": "hostile", "pos": [14, 64, 0]}]
        self.assertEqual(self.planner.choose(obs).args["pos"], [-5, 64, 0])

    def test_resource_absence_causes_one_bounded_exploration_move(self):
        obs = observation(blocks=[])
        decision = self.planner.choose(obs, {"spawn": [0.5, 64, 0.5], "explore_index": 0})
        self.assertEqual((decision.name, decision.goal), ("move_to", "explore"))
        self.assertEqual(decision.args, {"pos": [16.5, 64, 0.5], "range": 2})

    def test_exploration_cooldown_uses_harness_floored_coordinate_keys(self):
        obs = observation(blocks=[])
        decision = self.planner.choose(obs, {"spawn": [0.5, 64, 0.5], "blocked_targets": ["16,64,0"]})
        self.assertEqual(decision.args["pos"], [0.5, 64, 16.5])

    def test_exploration_stops_at_budget_and_keeps_each_move_local(self):
        obs = observation(blocks=[])
        context = {"spawn": [0.5, 64, 0.5], "explore_index": 0}
        for index in range(8):
            context["explore_index"] = index
            decision = self.planner.choose(obs, context)
            self.assertEqual((decision.name, decision.goal), ("move_to", "explore"))
            self.assertLessEqual(math.dist(decision.args["pos"], obs["status"]["pos"]), 32.001)
            obs["status"]["pos"] = decision.args["pos"]
        context["explore_index"] = 8
        self.assertEqual(self.planner.choose(obs, context).name, "wait")

    def test_settings_support_dataclass_like_objects(self):
        planner = SurvivalPlanner(SimpleNamespace(explore_distance=8, max_explore_steps=1))
        self.assertEqual(planner.choose(observation(blocks=[])).args["pos"], [8.5, 64, 0.5])
        self.assertEqual(planner.choose(observation(blocks=[]), {"explore_index": 1}).name, "wait")

    def test_inventory_loss_replans_instead_of_trusting_historical_milestones(self):
        complete = observation({"stone_pickaxe": 1, "stone_axe": 1, "stone_sword": 1, "furnace": 1})
        historical = self.planner.progress(complete)
        recovered = observation()
        decision = self.planner.choose(recovered, {"milestones": historical, "last_result": {"success": True}})
        self.assertEqual(decision.name, "gather_block")
        self.assertFalse(self.planner.progress(recovered)["starter_goal_complete"])

    def test_explicit_empty_counts_override_old_inventory(self):
        obs = observation()
        obs["inventory"] = [{"name": "stone_pickaxe", "count": 1}]
        self.assertFalse(self.planner.progress(obs)["stone_pickaxe"])
        del obs["counts"]
        self.assertTrue(self.planner.progress(obs)["stone_pickaxe"])

    def test_decisions_are_deterministic_and_do_not_mutate_inputs(self):
        obs = observation()
        context = {"blocked_targets": [], "milestones": {}}
        snapshot = copy.deepcopy((obs, context))
        first = self.planner.choose(obs, context)
        self.assertEqual(first, self.planner.choose(obs, context))
        self.assertEqual((obs, context), snapshot)

    def test_complete_resource_chain_reaches_goal_without_free_materials(self):
        obs = observation(blocks=[block("oak_log", x) for x in (3, 4, 5)] +
                          [block("stone", x, 64, 3) for x in range(6, 22)])
        counts = obs["counts"]
        decisions = []

        def take(name, amount):
            self.assertGreaterEqual(counts.get(name, 0), amount, (name, counts, decisions[-1]))
            counts[name] -= amount

        costs = {"wooden_pickaxe": {"oak_planks": 3, "stick": 2},
                 "stone_pickaxe": {"cobblestone": 3, "stick": 2},
                 "stone_axe": {"cobblestone": 3, "stick": 2},
                 "stone_sword": {"cobblestone": 2, "stick": 1}, "furnace": {"cobblestone": 8}}
        for _ in range(80):
            decision = self.planner.choose(obs, {"spawn": [0.5, 64, 0.5]})
            decisions.append(decision)
            if decision.name == "goal_complete":
                break
            if decision.name == "gather_block":
                target = next(b for b in obs["blocks"] if b["pos"] == decision.args["pos"])
                obs["blocks"].remove(target)
                gained = "cobblestone" if target["name"] == "stone" else target["name"]
                counts[gained] = counts.get(gained, 0) + 1
                obs["status"]["pos"] = [target["pos"][0] - 1, target["pos"][1], target["pos"][2]]
            elif decision.name == "craft_item":
                item, count = decision.args["item"], decision.args["count"]
                if item == "oak_planks":
                    take("oak_log", count // 4)
                elif item == "stick":
                    take("oak_planks", count // 4 * 2)
                elif item == "crafting_table":
                    take("oak_planks", 4)
                else:
                    self.assertTrue(any(b["name"] == "crafting_table" for b in obs["blocks"]))
                    for ingredient, amount in costs[item].items():
                        take(ingredient, amount)
                counts[item] = counts.get(item, 0) + count
            elif decision.name == "place_item":
                take(decision.args["item"], 1)
                obs["blocks"].append({"name": decision.args["item"], "pos": decision.args["pos"], "properties": {}})
            elif decision.name == "move_to" and decision.goal != "explore":
                target = decision.args["pos"]
                obs["status"]["pos"] = [target[0] - 1, target[1], target[2]]
            else:
                self.fail(f"Unexpected action in stocked simulation: {decision}")
        self.assertEqual(decisions[-1].name, "goal_complete")
        self.assertTrue(self.planner.progress(obs)["starter_goal_complete"])
        self.assertLess(len(decisions), 60)


if __name__ == "__main__":
    unittest.main()
