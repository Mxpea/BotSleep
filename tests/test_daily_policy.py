"""Pure policy regressions: no model, HTTP client, or game is used."""

import copy
import unittest
from unittest.mock import patch

from agent.survival_planner import Decision
from core.daily_policy import DailySafetyPolicy, FOOD_POINTS
from skills.daily_survival import DailySurvivalSkills


def observed(**status):
    return {"fresh": True, "status": {"alive": True, "ready": True, "health": 20, "food": 20,
            "gamemode": "survival", "dimension": "overworld", "pos": [.5, 64, .5],
            "on_ground": True, "is_day": True, "time_of_day": 5000, "thundering": False,
            "busy": None, **status}, "counts": {}, "blocks": [], "entities": [], "terrain": []}


def action(name, **args):
    return Decision(name, args, "Test action", "food" if name in {"eat_food", "harvest_crop"} else "test")


class DailyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = DailySafetyPolicy({"low_health_threshold": 8, "food_threshold": 16, "threat_radius": 8})
        self.baseline = Decision("wait", {"seconds": 5}, "Food acquisition not implemented", "food")

    def crop(self, observation, pos=None, name="carrots", mature=True, complete=False):
        pos = pos or [2, 64, 0]
        observation["blocks"].append({"name": name, "pos": pos, "mature": mature})
        if complete:
            x, y, z = pos
            for dx, dy, dz in ((-1, 0, 0), (1, 0, 0), (0, -1, 0), (0, 1, 0), (0, 0, -1), (0, 0, 1)):
                observation["blocks"].append({"name": "farmland" if dy == -1 else "air", "pos": [x + dx, y + dy, z + dz]})

    def shelter(self, observation, *, inside=False, path=None):
        observation["daily_evidence"] = {"shelter": {"pos": [3, 64, 0], "dimension": "overworld",
            "fresh": True, "verified": True, "geometry_complete": True, "inside": inside,
            "return_route": {"fresh": True, "verified": True, "dimension": "overworld",
                             "origin": list(observation["status"]["pos"]), "path": path or [[1, 64, 0], [2, 64, 0], [3, 64, 0]]}}}
        return observation["daily_evidence"]["shelter"]

    def gated_shelter(self, observation, *, opened=False, inside=False):
        observation["status"]["pos"] = [.5, 64, .5] if inside else [.5, 64, -2.5]
        row = self.shelter(observation, inside=inside, path=[[0, 64, -2], [0, 64, -1], [0, 64, 0]])
        row.update(pos=[0, 64, 0], geometry_complete=not opened, shelter_verified=not opened,
                   structure_complete=True, geometry={"structure_complete": True},
                   door={"pos": [0, 64, -2], "name": "oak_door", "facing": "north",
                         "frame_verified": True, "open": opened, "complete": not opened})
        row["return_route"]["verified"] = opened
        if not opened:
            row["return_route"]["blocked_door"] = {"pos": [0, 64, -2], "verified": True, "open": False}
            row["return_route"]["all_other_body_and_support_verified"] = True
        return row

    def test_critical_hunger_releases_only_local_food_planning(self):
        obs = observed(food=4)
        self.crop(obs)
        self.assertIsNone(self.policy.choose(obs, self.baseline))
        limits = self.policy.constraints(obs)
        self.assertEqual(limits["scenario"], "critical_hunger")
        self.assertTrue(limits["model_allowed"])
        self.assertEqual(limits["max_action_distance"], 4)
        self.assertIsNone(self.policy.validate(action("harvest_crop", pos=[2, 64, 0], expected_name="carrots"), obs))
        for name, args in (("hunt_animal", {"entity_id": 1, "expected_name": "cow"}),
                           ("gather_block", {"pos": [2, 64, 0], "expected_name": "oak_log"}),
                           ("gather_resource", {"pos": [2, 64, 0], "expected_name": "iron_ore"}),
                           ("move_to", {"pos": [2, 64, 0], "range": 1}),
                           ("smelt_start", {"pos": [2, 64, 0]}), ("place_bed", {"pos": [2, 64, 0]})):
            with self.subTest(name=name):
                self.assertEqual(self.policy.validate(action(name, **args), obs), "critical_hunger_nearby_food_only")

    def test_food_crop_must_be_current_mature_nearby_and_not_dangerous(self):
        for modification in ("far", "immature", "missing", "occluded", "hazard", "target_hostile"):
            obs = observed(food=3)
            self.crop(obs)
            pos = [2, 64, 0]
            if modification == "far":
                pos = [6, 64, 0]
                obs["blocks"][0]["pos"] = pos
            elif modification == "immature":
                obs["blocks"][0]["mature"] = False
            elif modification == "missing":
                obs["blocks"] = []
            elif modification == "occluded":
                obs["blocks"][0]["can_see"] = False
            elif modification == "hazard":
                obs["terrain"] = [{"name": "lava", "pos": [2, 63, 0]}]
            else:
                obs["entities"] = [{"type": "hostile", "name": "skeleton", "pos": [9, 64, 0]}]
            with self.subTest(modification=modification):
                self.assertIsNotNone(self.policy.validate(action("harvest_crop", pos=pos, expected_name="carrots"), obs))

    def test_fresh_current_container_food_is_allowed_but_old_or_nonfood_is_not(self):
        obs = observed(food=5)
        obs["containers"] = [{"name": "chest", "pos": [2, 64, 0], "fresh": True, "verified": True,
                              "dimension": "overworld", "items": [{"name": "bread", "count": 2}]}]
        take = action("take_items", pos=[2, 64, 0], item="bread", count=2)
        self.assertIsNone(self.policy.validate(take, obs))
        self.assertIsNotNone(self.policy.validate(action("take_items", pos=[2, 64, 0], item="coal", count=1), obs))
        obs["containers"][0]["fresh"] = False
        self.assertIsNotNone(self.policy.validate(take, obs))

    def test_nearby_inspection_is_allowed_without_claiming_food_exists(self):
        obs = observed(food=5)
        inspect = action("inspect_container", pos=[2, 64, 0])
        self.assertIsNotNone(self.policy.validate(inspect, obs))
        obs["blocks"] = [{"name": "chest", "pos": [2, 64, 0]}]
        self.assertIsNone(self.policy.validate(inspect, obs))

    def test_bread_needs_held_wheat_and_nearby_observed_table(self):
        obs = observed(food=5)
        obs["counts"] = {"wheat": 6}
        bread = action("craft_item", item="bread", count=2)
        self.assertIsNotNone(self.policy.validate(bread, obs))
        obs["blocks"] = [{"name": "crafting_table", "pos": [2, 64, 0]}]
        self.assertIsNone(self.policy.validate(bread, obs))
        self.assertIsNotNone(self.policy.validate(action("craft_item", item="stone_sword", count=1), obs))
        self.assertIsNotNone(self.policy.validate(action("craft_item", item="bread", count=3), obs))

    def test_smelting_collection_needs_actual_food_output(self):
        obs = observed(food=5)
        obs["furnaces"] = [{"pos": [2, 64, 0], "fresh": True, "verified": True, "dimension": "overworld",
                            "output": {"name": "cooked_beef", "count": 1}}]
        self.assertIsNone(self.policy.validate(action("smelt_collect", pos=[2, 64, 0], item="cooked_beef", count=1), obs))
        obs["furnaces"][0]["output"] = {"name": "iron_ingot", "count": 3}
        self.assertIsNotNone(self.policy.validate(action("smelt_collect", pos=[2, 64, 0], item="iron_ingot", count=1), obs))

    def test_existing_food_is_local_override_even_without_model(self):
        obs = observed(food=5)
        obs["counts"] = {"bread": 1}
        choice = self.policy.choose(obs, self.baseline)
        self.assertEqual(choice.name, "eat_food")
        self.assertEqual(choice.args, {"item": "bread"})
        self.assertFalse(self.policy.constraints(obs)["model_allowed"])
        self.assertEqual(self.policy.validate(action("harvest_crop", pos=[2, 64, 0], expected_name="carrots"), obs), "existing_food_has_priority")

    def test_approved_raw_foods_are_edible_only_when_registered(self):
        raw = {"beef": 3, "porkchop": 3, "rabbit": 3, "mutton": 2, "cod": 2, "salmon": 2, "potato": 1}
        with patch.object(DailySurvivalSkills, "FOODS", DailySurvivalSkills.FOODS | set(raw)):
            for item, points in raw.items():
                obs = observed(food=5)
                obs["counts"] = {item: 1}
                self.assertEqual(self.policy.choose(obs, self.baseline).args, {"item": item})
                self.assertEqual(FOOD_POINTS[item], points)
        for item in ("chicken", "rotten_flesh", "poisonous_potato", "spider_eye", "pufferfish", "chorus_fruit"):
            obs = observed(food=5)
            obs["counts"] = {item: 1}
            self.assertIsNotNone(self.policy.validate(action("eat_food", item=item), obs))

    def test_low_health_eats_or_waits_and_never_hunts(self):
        obs = observed(health=5, food=4)
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        self.assertFalse(self.policy.constraints(obs)["model_allowed"])
        self.assertEqual(self.policy.validate(action("hunt_animal", entity_id=1, expected_name="cow"), obs), "low_health_food_only")
        obs["counts"] = {"bread": 1}
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "eat_food")

    def test_low_health_exception_needs_complete_close_immediate_food_evidence(self):
        obs = observed(health=6, food=4)
        self.crop(obs, pos=[1, 64, 0])
        decision = action("harvest_crop", pos=[1, 64, 0], expected_name="carrots")
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        self.assertIsNotNone(self.policy.validate(decision, obs))
        obs["blocks"] = []
        self.crop(obs, pos=[1, 64, 0], complete=True)
        self.assertIsNone(self.policy.choose(obs, self.baseline))
        self.assertIsNone(self.policy.validate(decision, obs))
        self.assertEqual(self.policy.constraints(obs)["max_action_distance"], 2)
        obs["terrain"] = [{"name": "fire", "pos": [1, 65, 0]}]
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")

    def test_low_health_does_not_harvest_wheat_or_prospect_unknown_chests(self):
        obs = observed(health=6, food=4)
        self.crop(obs, pos=[1, 64, 0], name="wheat", complete=True)
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        self.assertIsNotNone(self.policy.validate(action("harvest_crop", pos=[1, 64, 0], expected_name="wheat"), obs))
        self.assertIsNotNone(self.policy.validate(action("inspect_container", pos=[1, 64, 0]), obs))

    def test_low_health_with_regeneration_food_waits_instead_of_collecting(self):
        obs = observed(health=6, food=20)
        self.crop(obs, pos=[1, 64, 0], complete=True)
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        self.assertFalse(self.policy.constraints(obs)["model_allowed"])

    def test_stale_lifecycle_mode_hazard_and_airborne_never_release_food_plan(self):
        cases = [({"alive": False}, "dead"), ({"ready": False}, "not_ready"), ({"busy": "dig"}, "busy"),
                 ({"dimension": "the_nether"}, "unsafe_dimension"), ({"gamemode": "creative"}, "unsafe_mode"),
                 ({"in_lava": True}, "environment_hazard"), ({"on_fire": True}, "environment_hazard"),
                 ({"submerged": True}, "environment_hazard"), ({"on_ground": False}, "unstable"),
                 ({"time_of_day": float("nan")}, "observation_required")]
        for fields, scenario in cases:
            obs = observed(food=4, **fields)
            self.crop(obs)
            with self.subTest(scenario=scenario, fields=fields):
                self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
                self.assertEqual(self.policy.constraints(obs)["scenario"], scenario)
                self.assertIsNotNone(self.policy.validate(action("harvest_crop", pos=[2, 64, 0], expected_name="carrots"), obs))
        self.assertEqual(self.policy.choose({"fresh": False}, self.baseline).name, "wait")

    def test_hostile_retreat_and_harness_cooldown_take_priority(self):
        obs = observed(food=4, health=4)
        obs["counts"] = {"bread": 1}
        obs["entities"] = [{"type": "hostile", "name": "zombie", "pos": [4, 64, 0]}]
        retreat = self.policy.choose(obs, self.baseline)
        self.assertEqual(retreat.name, "retreat_from")
        self.assertIsNone(self.policy.validate(retreat, obs))
        cooldown = Decision("wait", {"seconds": 5}, "No route; preserve cooldown", "safety")
        self.assertIs(self.policy.choose(obs, cooldown), cooldown)
        obs["entities"] = []
        obs["status"]["nearest_hostile"] = {"dist": 3, "name": "zombie"}
        self.assertEqual(self.policy.constraints(obs)["scenario"], "hostile_position_unknown")
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")

    def test_baseline_relay_and_safety_wait_are_not_replaced_by_resources(self):
        obs = observed()
        relay = Decision("retreat_from", {"pos": [-5, 64, 0], "distance": 8}, "Continue relay", "safety")
        self.assertIs(self.policy.choose(obs, relay), relay)
        cooldown = Decision("wait", {"seconds": 5}, "Continue bounded cooldown", "safety")
        self.assertIs(self.policy.choose(obs, cooldown), cooldown)
        resource = action("gather_block", pos=[2, 64, 0], expected_name="oak_log")
        self.assertIsNone(self.policy.choose(obs, resource))

    def test_night_bed_uses_local_sleep_without_model(self):
        obs = observed(is_day=False, time_of_day=14000)
        obs["blocks"] = [{"name": "white_bed", "pos": [2, 64, 0], "properties": {"occupied": False}}]
        decision = self.policy.choose(obs, self.baseline)
        self.assertEqual(decision.name, "auto_sleep")
        self.assertIsNone(self.policy.validate(decision, obs))
        obs["blocks"][0]["properties"]["occupied"] = True
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")

    def test_dusk_returns_along_fresh_short_shelter_route_to_its_center(self):
        obs = observed(time_of_day=11000)
        self.shelter(obs)
        decision = self.policy.choose(obs, self.baseline)
        self.assertEqual(decision.name, "move_to")
        self.assertEqual(decision.args, {"pos": [3.5, 64, .5], "range": .5})
        self.assertIn("terrain evidence", decision.reason)
        self.assertIsNone(self.policy.validate(decision, obs))
        changed = action("move_to", pos=[4.5, 64, .5], range=.5)
        self.assertIsNotNone(self.policy.validate(changed, obs))

    def test_return_home_opens_reobserves_moves_reobserves_then_closes(self):
        obs = observed(time_of_day=11000)
        shelter = self.gated_shelter(obs)
        before = copy.deepcopy(obs)
        opened = self.policy.choose(obs, self.baseline)
        self.assertEqual(opened.name, "set_door")
        self.assertEqual(opened.args, {"pos": [0, 64, -2], "open": True})
        self.assertIsNone(self.policy.validate(opened, obs))
        self.assertIsNotNone(self.policy.validate(action("move_to", pos=[.5, 64, .5], range=.5), obs))
        self.assertEqual(obs, before)
        shelter["door"].update(open=True, complete=False)
        shelter.update(geometry_complete=False, shelter_verified=False)
        # A door update alone does not make the old blocked route traversable.
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        shelter["return_route"].update(verified=True)
        shelter["return_route"].pop("blocked_door")
        moved = self.policy.choose(obs, self.baseline)
        self.assertEqual(moved.name, "move_to")
        self.assertEqual(moved.args, {"pos": [.5, 64, .5], "range": .5})
        self.assertIsNone(self.policy.validate(moved, obs))
        self.assertIsNotNone(self.policy.validate(opened, obs))
        # Only current inside evidence allows the separate close decision.
        shelter["inside"] = True
        obs["status"]["pos"] = [.5, 64, .5]
        closed = self.policy.choose(obs, self.baseline)
        self.assertEqual(closed.name, "set_door")
        self.assertEqual(closed.args, {"pos": [0, 64, -2], "open": False})
        self.assertIsNone(self.policy.validate(closed, obs))
        shelter["door"].update(open=False, complete=True)
        shelter.update(geometry_complete=True, shelter_verified=True)
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        self.assertIsNotNone(self.policy.validate(closed, obs))

    def test_close_current_open_door_precedes_sleep_and_never_toggles_closed_door(self):
        obs = observed(is_day=False, time_of_day=14000)
        shelter = self.gated_shelter(obs, opened=True, inside=True)
        obs["blocks"] = [{"name": "white_bed", "pos": [1, 64, 0], "properties": {"occupied": False}}]
        self.assertEqual(self.policy.choose(obs, self.baseline).args, {"pos": [0, 64, -2], "open": False})
        self.assertIsNotNone(self.policy.validate(action("auto_sleep"), obs))
        shelter["door"].update(open=False, complete=True)
        shelter.update(geometry_complete=True, shelter_verified=True)
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "auto_sleep")

    def test_inside_far_corner_requires_fresh_route_before_repositioning_to_close(self):
        obs = observed(time_of_day=11000)
        shelter = self.gated_shelter(obs, opened=True, inside=True)
        obs["status"]["pos"] = [1.5, 64, 1.5]
        shelter.pop("return_route")
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        shelter["return_route"] = {"fresh": True, "verified": True, "dimension": "overworld",
                                   "origin": [1.5, 64, 1.5], "path": [[0, 64, 1], [0, 64, 0]]}
        decision = self.policy.choose(obs, self.baseline)
        self.assertEqual(decision.name, "move_to")
        self.assertEqual(decision.args, {"pos": [.5, 64, .5], "range": .5})
        self.assertIsNone(self.policy.validate(decision, obs))
        obs["status"]["pos"] = [.5, 64, .5]
        self.assertEqual(self.policy.choose(obs, self.baseline).args, {"pos": [0, 64, -2], "open": False})

    def test_door_opening_never_bypasses_hostile_or_harness_retreat_cooldown(self):
        obs = observed(time_of_day=11000)
        self.gated_shelter(obs)
        obs["entities"] = [{"type": "hostile", "name": "zombie", "pos": [3, 64, -2]}]
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "retreat_from")
        self.assertEqual(self.policy.validate(action("set_door", pos=[0, 64, -2], open=True), obs), "near_hostile")
        cooldown = Decision("wait", {"seconds": 5}, "Retreat route cooldown", "safety")
        self.assertIs(self.policy.choose(obs, cooldown), cooldown)

    def test_door_steps_require_complete_current_structure_and_actual_wooden_frame(self):
        for problem in ("stale", "dimension", "structure", "frame", "open_unknown", "fractional_pos",
                        "iron_door", "wrong_facing", "missing_door", "unknown_inside"):
            obs = observed(time_of_day=11000)
            shelter = self.gated_shelter(obs)
            if problem == "stale": shelter["fresh"] = False
            if problem == "dimension": shelter["dimension"] = "the_nether"
            if problem == "structure": shelter["structure_complete"] = False
            if problem == "frame": shelter["door"]["frame_verified"] = False
            if problem == "open_unknown": shelter["door"]["open"] = "false"
            if problem == "fractional_pos": shelter["door"]["pos"] = [0.5, 64, -2]
            if problem == "iron_door": shelter["door"]["name"] = "iron_door"
            if problem == "wrong_facing": shelter["door"]["facing"] = "east"
            if problem == "missing_door": shelter.pop("door")
            if problem == "unknown_inside": shelter.pop("inside")
            with self.subTest(problem=problem):
                self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")

    def test_opening_requires_sole_verified_door_obstruction_on_fresh_short_route(self):
        for problem in ("blocked_unverified", "blocked_open", "other_cells_unknown", "different_door", "outside_path",
                        "origin_changed", "route_stale", "route_wrong_dimension", "hazard", "too_far"):
            obs = observed(time_of_day=11000)
            shelter = self.gated_shelter(obs)
            route = shelter["return_route"]
            if problem == "blocked_unverified": route["blocked_door"]["verified"] = False
            if problem == "blocked_open": route["blocked_door"]["open"] = True
            if problem == "other_cells_unknown": route.pop("all_other_body_and_support_verified")
            if problem == "different_door": route["blocked_door"]["pos"] = [1, 64, -2]
            if problem == "outside_path":
                route["path"] = [[1, 64, -3], [1, 64, -2], [1, 64, -1], [1, 64, 0]]
            if problem == "origin_changed": route["origin"] = [.5, 64, -1.5]
            if problem == "route_stale": route["fresh"] = False
            if problem == "route_wrong_dimension": route["dimension"] = "the_nether"
            if problem == "hazard": obs["blocks"] = [{"name": "lava", "pos": [0, 63, -2]}]
            if problem == "too_far":
                obs["status"]["pos"] = [.9, 64, -4.9]
                route["origin"] = list(obs["status"]["pos"])
                route["path"] = [[0, 64, z] for z in (-4, -3, -2, -1)]
            with self.subTest(problem=problem):
                self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")

    def test_open_door_route_still_needs_current_origin_and_no_contradiction(self):
        obs = observed(time_of_day=11000)
        shelter = self.gated_shelter(obs, opened=True)
        shelter.pop("structure_complete")  # Accept the collector's nested geometry field too.
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "move_to")
        shelter["door"]["open"] = False
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        shelter["door"]["open"] = True
        obs["status"]["pos"] = [1.5, 64, -2.5]
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")

    def test_daytime_does_not_start_automatic_home_door_actions(self):
        obs = observed()
        self.gated_shelter(obs)
        self.assertIsNone(self.policy.choose(obs, self.baseline))

    def test_memory_or_geometry_without_route_never_authorizes_return(self):
        obs = observed(time_of_day=11000)
        obs["memory"] = {"shelter": {"pos": [3, 64, 0], "verified": True}}
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        shelter = self.shelter(obs)
        shelter.pop("return_route")
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        self.assertFalse(self.policy.constraints(obs)["model_allowed"])

    def test_route_rejects_stale_changed_origin_dimension_nonadjacent_or_height(self):
        for problem in ("stale", "origin", "dimension", "gap", "height", "too_long", "outside", "hazard"):
            obs = observed(time_of_day=11000)
            shelter = self.shelter(obs)
            route = shelter["return_route"]
            if problem == "stale": route["fresh"] = False
            if problem == "origin": route["origin"] = [1.5, 64, .5]
            if problem == "dimension": route["dimension"] = "the_nether"
            if problem == "gap": route["path"] = [[3, 64, 0]]
            if problem == "height": route["path"] = [[1, 65, 0], [2, 65, 0]]
            if problem == "too_long": route["path"] = [[x, 64, 0] for x in range(1, 6)]
            if problem == "outside": shelter["pos"] = [7, 64, 0]
            if problem == "hazard": obs["blocks"] = [{"name": "lava", "pos": [2, 63, 0]}]
            with self.subTest(problem=problem):
                self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")

    def test_inside_or_very_near_shelter_waits_without_false_move_progress(self):
        obs = observed(time_of_day=11000)
        self.shelter(obs, inside=True)
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        obs = observed(time_of_day=11000, pos=[1.5, 64, .5])
        self.shelter(obs, path=[[2, 64, 0]])
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")

    def test_dusk_does_not_release_critical_food_hunting_or_resource_work(self):
        obs = observed(food=3, time_of_day=11000)
        self.crop(obs)
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        self.assertEqual(self.policy.validate(action("harvest_crop", pos=[2, 64, 0], expected_name="carrots"), obs),
                         "night_requires_verified_sleep_or_shelter_route")

    def test_pure_policy_does_not_mutate_observation_or_use_network(self):
        obs = observed(food=4)
        self.crop(obs)
        before = copy.deepcopy(obs)
        with patch("requests.request", side_effect=AssertionError("Policy must not use HTTP")):
            self.policy.choose(obs, self.baseline)
            limits = self.policy.constraints(obs)
            self.policy.validate(action("harvest_crop", pos=[2, 64, 0], expected_name="carrots"), obs)
        self.assertEqual(obs, before)
        self.assertEqual(limits, self.policy.constraints(obs))
        self.assertFalse(limits["route_evidence_is_actual_trajectory"])
        self.assertEqual(limits["shelter_spawn_safety"], "unknown")


if __name__ == "__main__":
    unittest.main()
