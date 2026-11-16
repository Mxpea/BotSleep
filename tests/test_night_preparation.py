"""Night preparation policy contracts: supplied evidence only, no game/model I/O."""

import copy
import unittest
from unittest.mock import patch

from core.daily_policy import DailySafetyPolicy, NIGHT_PREPARATION_ACTIONS
from skills.daily_survival import DailySurvivalSkills
from test_daily_policy import action, observed


class NightPreparationTests(unittest.TestCase):
    def setUp(self):
        self.policy = DailySafetyPolicy({"threat_radius": 8})
        self.baseline = action("gather_block", pos=[2, 64, 0], expected_name="oak_log")
        for target in ("builtins.open", "socket.create_connection", "requests.sessions.Session.request"):
            self.enterContext(patch(target, side_effect=AssertionError("Pure policy must not perform I/O")))
        self.enterContext(patch.object(DailySurvivalSkills, "execute",
                                      side_effect=AssertionError("Policy must not execute skills")))

    def night(self, **status):
        obs = observed(**{"is_day": False, "time_of_day": 14000, **status})
        obs.update(observation_scope="full", inventory_fresh=True, terrain_fresh=True,
                   counts={"oak_log": 2})
        return obs

    def log(self, obs, **fields):
        row = {"name": "oak_log", "pos": [2, 64, 0], "can_see": True, "can_dig": True, **fields}
        obs["blocks"].append(row)
        return row

    def shelter(self, obs, *, inside=False):
        row = {"pos": [3, 64, 0], "dimension": "overworld", "fresh": True,
               "verified": True, "geometry_complete": True, "inside": inside,
               "return_route": {"fresh": True, "verified": True, "dimension": "overworld",
                                "origin": list(obs["status"]["pos"]),
                                "path": [[1, 64, 0], [2, 64, 0], [3, 64, 0]]}}
        obs["daily_evidence"] = {"shelter": row}
        return row

    def assert_released(self, obs, policy=None):
        policy = policy or self.policy
        before = copy.deepcopy(obs)
        self.assertIsNone(policy.choose(obs, self.baseline), "Only the model may choose preparation")
        constraints = policy.constraints(obs)
        self.assertEqual(constraints["scenario"], "night_preparation")
        self.assertIs(constraints["model_allowed"], True)
        self.assertEqual(constraints["max_action_distance"], 3)
        self.assertEqual(obs, before, "Pure policy must not mutate evidence")
        return constraints

    def test_dusk_and_night_release_model_without_executing_baseline_gather(self):
        expected = {"gather_block", "craft_item", "place_item", "place_bed",
                    "place_door", "plan_shelter", "inspect_shelter", "build_shelter_step",
                    "position_for_task", "wait"}
        self.assertEqual(set(NIGHT_PREPARATION_ACTIONS), expected)
        for status in ({"is_day": True, "time_of_day": 11000},
                       {"is_day": False, "time_of_day": 14000},
                       {"is_day": False, "time_of_day": 23000},
                       {"health": 20, "food": 12}, {"health": 21}):
            with self.subTest(status=status):
                obs = self.night(**status)
                self.assertEqual(set(self.assert_released(obs)["allowed_actions"]), expected)

    def test_release_needs_materials_visible_nearby_logs_or_current_safe_stand(self):
        for source in ("materials", "log", "stand", "none", "unrelated", "hidden", "far"):
            obs = self.night()
            obs["counts"] = {"oak_planks": 4} if source == "materials" else {}
            if source == "unrelated": obs["counts"] = {"diamond": 1, "oak_log": 0}
            if source in {"log", "hidden", "far"}:
                self.log(obs, can_see=source != "hidden", pos=[5, 64, 0] if source == "far" else [2, 64, 0])
            if source == "stand":
                obs["safe_stand_candidates"] = [{"pos": [1.5, 64, .5], "support": "dirt"}]
            with self.subTest(source=source):
                if source in {"materials", "log", "stand"}:
                    self.assert_released(obs)
                else:
                    self.assertFalse(self.policy.constraints(obs)["model_allowed"])
                    self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")

    def test_unsafe_lifecycle_health_hunger_weather_and_partial_observations_wait(self):
        cases = [{"health": 19}, {"health": 5}, {"food": 11}, {"food": 4},
                 {"ready": False}, {"alive": False}, {"busy": "dig"},
                 {"dimension": "the_nether"}, {"gamemode": "creative"},
                 {"on_ground": False}, {"in_water": True}, {"on_fire": True},
                 {"in_lava": True}, {"submerged": True}, {"in_vehicle": True}, {"thundering": True}]
        for status in cases:
            obs = self.night(**status)
            with self.subTest(status=status):
                self.assertFalse(self.policy.constraints(obs)["model_allowed"])
                self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
                self.assertIsNotNone(self.policy.validate(action("craft_item", item="stick", count=1), obs))
        for field, value in (("fresh", False), ("stale", True), ("inventory_fresh", False),
                             ("terrain_fresh", False), ("observation_scope", "safety")):
            obs = self.night()
            obs[field] = value
            with self.subTest(field=field):
                self.assertFalse(self.policy.constraints(obs)["model_allowed"])
                self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")

    def test_threat_buffer_is_at_least_twelve_and_respects_larger_configured_radius(self):
        for radius in (8, 16):
            policy = DailySafetyPolicy({"threat_radius": radius})
            buffer = max(radius, 12)
            for distance in (buffer, buffer + .01):
                for source in ("entities", "nearest_hostile"):
                    obs = self.night()
                    if source == "entities":
                        obs["entities"] = [{"name": "zombie", "type": "hostile",
                                            "pos": [.5 + distance, 64, .5]}]
                    else:
                        obs["status"]["nearest_hostile"] = {"name": "zombie", "dist": distance}
                    with self.subTest(radius=radius, distance=distance, source=source):
                        if distance > buffer:
                            self.assert_released(obs, policy)
                        else:
                            self.assertFalse(policy.constraints(obs)["model_allowed"])
                            self.assertIsNotNone(policy.choose(obs, self.baseline))
                            self.assertIsNotNone(policy.validate(action("craft_item", item="stick"), obs))

    def test_legal_local_single_steps_validate_without_executing_skills(self):
        obs = self.night()
        obs["counts"].update(oak_planks=8, white_bed=1, oak_door=1, crafting_table=1,
                             cobblestone=8, stick=4, coal=1, white_wool=3)
        self.log(obs)
        obs["blocks"].append({"name": "oak_leaves", "pos": [1, 64, 0], "can_see": True})
        obs["safe_stand_candidates"] = [{"pos": [1.5, 64, .5], "support": "dirt"}]
        decisions = [self.baseline,
                     action("place_item", pos=[1, 64, 1], item="crafting_table"),
                     action("place_bed", pos=[1, 64, 1], item="white_bed"),
                     action("place_door", pos=[1, 64, 1], item="oak_door"),
                     action("position_for_task", pos=[1.5, 64, .5]), action("wait", seconds=1)]
        decisions += [action(name, pos=[1, 64, 1], material="oak_planks")
                      for name in ("plan_shelter", "inspect_shelter", "build_shelter_step")]
        decisions += [action("craft_item", item=item, count=count) for item, count in
                      (("oak_planks", 4), ("stick", 4), ("crafting_table", 1),
                       ("wooden_pickaxe", 1), ("stone_pickaxe", 1), ("white_bed", 1),
                       ("oak_door", 3), ("torch", 4), ("furnace", 1))]
        before = copy.deepcopy(obs)
        for decision in decisions:
            with self.subTest(name=decision.name, args=decision.args):
                self.assertIsNone(self.policy.validate(decision, obs))
        self.assertEqual(obs, before)

    def test_batches_travel_hunting_and_late_or_excessive_production_are_rejected(self):
        obs = self.night()
        decisions = [action("clear_foliage", pos=[1, 64, 0], expected_name="oak_leaves"),
                     action("gather_materials", resource="logs", count=2),
                     action("build_shelter_batch", pos=[1, 64, 1], material="oak_planks", max_blocks=2),
                     action("hunt_animal", entity_id=1, expected_name="cow"),
                     action("move_to", pos=[1.5, 64, .5]),
                     action("smelt_start", pos=[1, 64, 0], item="iron_ore", count=1)]
        decisions += [action("craft_item", item=item, count=1) for item in
                      ("iron_pickaxe", "diamond_pickaxe", "iron_chestplate", "bucket", "shield")]
        decisions += [action("craft_item", item="oak_planks", count=count)
                      for count in (0, -1, 64, True, 1.5, "4")]
        for decision in decisions:
            with self.subTest(name=decision.name, args=decision.args):
                self.assertEqual(self.policy.validate(decision, obs), "night_preparation_single_step_only")

    def test_gather_requires_matching_visible_diggable_log_in_three_block_safe_reach(self):
        for problem in ("none", "missing", "mismatch", "hidden", "unknown_visibility",
                        "undiggable", "far", "hazard", "waterlogged", "stone", "coal"):
            obs = self.night()
            row = self.log(obs)
            decision = self.baseline
            if problem == "missing": obs["blocks"] = []
            if problem == "mismatch": row["name"] = "birch_log"
            if problem == "hidden": row["can_see"] = False
            if problem == "unknown_visibility": row.pop("can_see")
            if problem == "undiggable": row["can_dig"] = False
            if problem == "far":
                row["pos"] = [4, 64, 0]
                decision = action("gather_block", pos=row["pos"], expected_name="oak_log")
            if problem == "hazard": obs["terrain"] = [{"name": "lava", "pos": [3, 64, 0]}]
            if problem == "waterlogged": row["properties"] = {"waterlogged": True}
            if problem in {"stone", "coal"}:
                row["name"] = "stone" if problem == "stone" else "coal_ore"
                decision = action("gather_block", pos=row["pos"], expected_name=row["name"])
            with self.subTest(problem=problem):
                if problem == "none":
                    self.assertIsNone(self.policy.validate(decision, obs))
                    row.pop("can_dig")  # Unknown diggability is allowed; explicit False is not.
                    self.assertIsNone(self.policy.validate(decision, obs))
                else:
                    self.assertIsNotNone(self.policy.validate(decision, obs))

    def test_positioning_requires_fresh_listed_same_level_stand_within_three_blocks(self):
        for problem in ("none", "unlisted", "far", "different_level", "stale", "hazard"):
            obs = self.night()
            target = [1.5, 64, .5]
            if problem == "far": target = [4.5, 64, .5]
            if problem == "different_level": target = [1.5, 65, .5]
            obs["safe_stand_candidates"] = [{"pos": target, "support": "dirt"}]
            if problem == "unlisted": obs["safe_stand_candidates"] = []
            if problem == "stale": obs["terrain_fresh"] = False
            if problem == "hazard": obs["terrain"] = [{"name": "fire", "pos": [1, 64, 0]}]
            with self.subTest(problem=problem):
                error = self.policy.validate(action("position_for_task", pos=target), obs)
                if problem == "none": self.assertIsNone(error)
                else: self.assertIsNotNone(error)

    def test_changed_conditions_invalidate_a_previously_approved_model_step(self):
        original = self.night()
        self.log(original)
        self.assert_released(original)
        self.assertIsNone(self.policy.validate(self.baseline, original))
        for change in ("health", "hunger", "storm", "inventory", "terrain", "position", "block", "enemy", "bed"):
            obs = copy.deepcopy(original)
            if change == "health": obs["status"]["health"] = 19
            if change == "hunger": obs["status"]["food"] = 11
            if change == "storm": obs["status"]["thundering"] = True
            if change == "inventory": obs["inventory_fresh"] = False
            if change == "terrain": obs["terrain_fresh"] = False
            if change == "position": obs["status"]["pos"] = [10.5, 64, .5]
            if change == "block": obs["blocks"][0]["name"] = "air"
            if change == "enemy": obs["status"]["nearest_hostile"] = {"dist": 10, "name": "zombie"}
            if change == "bed": obs["blocks"].append({"name": "white_bed", "pos": [1, 64, 1]})
            with self.subTest(change=change):
                self.assertIsNotNone(self.policy.validate(self.baseline, obs))
                self.assertIsNone(self.policy.validate(action("wait", seconds=1), obs))

    def test_free_bed_verified_home_and_known_inside_keep_existing_priority(self):
        obs = self.night()
        obs["blocks"] = [{"name": "white_bed", "pos": [1, 64, 0], "properties": {"occupied": False}}]
        sleep = self.policy.choose(obs, self.baseline)
        self.assertEqual(sleep.name, "auto_sleep")
        self.assertIsNone(self.policy.validate(sleep, obs))
        self.assertFalse(self.policy.constraints(obs)["model_allowed"])
        for occupied in (True, "true"):
            obs["blocks"][0]["properties"]["occupied"] = occupied
            with self.subTest(occupied=occupied):
                self.assert_released(obs)
        for inside in (False, True):
            obs = self.night()
            self.shelter(obs, inside=inside)
            with self.subTest(inside=inside):
                chosen = self.policy.choose(obs, self.baseline)
                self.assertEqual(chosen.name, "wait" if inside else "move_to")
                self.assertIsNone(self.policy.validate(chosen, obs))
                self.assertFalse(self.policy.constraints(obs)["model_allowed"])
                self.assertIsNotNone(self.policy.validate(action("craft_item", item="stick"), obs))

    def test_competition_metadata_tools_release_empty_handed_turn_without_side_effects(self):
        policy = DailySafetyPolicy({"objective": "competition", "threat_radius": 8})
        obs = self.night()
        obs["counts"] = {}
        before = copy.deepcopy(obs)
        constraints = self.assert_released(obs, policy)
        metadata = {"select_task", "update_progress", "finish_task", "force_exit_task", "end_competition"}
        self.assertEqual(set(policy.task_tools), metadata)
        self.assertEqual(set(constraints["allowed_actions"]), set(NIGHT_PREPARATION_ACTIONS) | metadata)
        for name in metadata:
            with self.subTest(name=name):
                self.assertIsNone(policy.validate(action(name, task_id="test", reason="Local metadata"), obs))
        self.assertEqual(obs, before)
        obs["status"]["food"] = 11
        self.assertFalse(policy.constraints(obs)["model_allowed"])
        self.assertIsNotNone(policy.validate(action("select_task", task_id="test"), obs))

    def test_day_before_dusk_retains_normal_planning_without_night_action_restrictions(self):
        for time_of_day in (5000, 10999):
            obs = self.night(is_day=True, time_of_day=time_of_day)
            obs["counts"] = {}
            with self.subTest(time_of_day=time_of_day):
                constraints = self.policy.constraints(obs)
                self.assertEqual(constraints["scenario"], "normal")
                self.assertTrue(constraints["model_allowed"])
                self.assertIsNone(constraints["allowed_actions"])
                self.assertIsNone(self.policy.choose(obs, self.baseline))
                for decision in (action("gather_materials", resource="logs", count=2),
                                 action("move_to", pos=[8, 64, 0]),
                                 action("craft_item", item="iron_pickaxe", count=1)):
                    self.assertIsNone(self.policy.validate(decision, obs))


if __name__ == "__main__":
    unittest.main()
