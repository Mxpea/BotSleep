"""Daily task-board evidence and prerequisite tests; no client or model."""

import copy
import unittest

from agent.daily_survival import DAILY_SKILLS, TASK_IDS, DailySurvivalGoals


def block(name, pos, **properties):
    return {"name": name, "pos": pos, "properties": properties}


def observation(counts=None, blocks=None, **status):
    return {"fresh": True, "status": {"ready": True, "alive": True, "busy": None,
        "gamemode": "survival", "dimension": "overworld", "pos": [.5, 64, .5],
        "health": 20, "food": 20, "is_day": True, "thundering": False, "on_ground": True, **status},
        "counts": counts or {}, "inventory": [], "entities": [],
        "blocks": blocks if blocks is not None else [block("oak_log", [3, 64, 0]), block("stone", [5, 63, 0])],
        "placement_candidates": [{"pos": [2, 64, 2], "support": "dirt"}]}


def verified(pos=None, **extra):
    return {"fresh": True, "verified": True, "dimension": "overworld", "pos": pos or [3, 64, 3], **extra}


def ready_world():
    obs = observation({"bread": 8, "oak_log": 8, "coal": 8,
        "iron_pickaxe": 1, "iron_axe": 1, "iron_sword": 1}, [
            block("furnace", [2, 64, 0]), block("chest", [2, 64, 2]),
            block("white_bed", [3, 64, 3], part="foot", facing="south", occupied=False),
            block("white_bed", [3, 64, 4], part="head", facing="south", occupied=False)])
    obs["daily_evidence"] = {"base_site": verified(), "shelter": verified(shelter_verified=True),
                             "food_production": verified(renewable=True)}
    obs["containers"] = [verified([2, 64, 2], name="chest", items=[])]
    return obs


class DailyGoalsTests(unittest.TestCase):
    def setUp(self):
        self.goals = DailySurvivalGoals()

    def task(self, obs, task_id, **kwargs):
        return next(t for t in self.goals.build(obs, **kwargs)["tasks"] if t["id"] == task_id)

    def test_fixed_bounded_schema_and_empty_inventory_prerequisites(self):
        board = self.goals.build(observation())
        self.assertEqual([row["id"] for row in board["tasks"]], list(TASK_IDS))
        self.assertEqual(board["active_task_id"], "starter_tools")
        choice = self.goals.choose(observation())
        self.assertEqual((choice.name, choice.args["expected_name"]), ("gather_block", "oak_log"))
        self.assertFalse(board["maintenance_ready"])
        for row in board["tasks"]:
            self.assertEqual(set(row), {"id", "status", "priority", "goal", "depends_on", "evidence", "missing", "blocked_reason", "next_action"})

    def test_missing_advanced_capability_is_explicitly_blocked(self):
        obs = observation()
        obs["blocks"].append(block("wheat", [2, 64, 1], age=7))
        task = self.task(obs, "food_reserve", context={"capabilities": ["gather_block", "craft_item"]})
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["blocked_reason"], "unsupported_skill:harvest_crop")
        self.assertIsNone(task["next_action"])

    def test_missing_stale_dead_or_wrong_world_cannot_satisfy_goals(self):
        for change in ({"fresh": False}, {"stale": True}, {"fresh": None}):
            obs = ready_world()
            obs.update(change)
            board = self.goals.build(obs)
            self.assertFalse(board["maintenance_ready"])
            self.assertTrue(all(t["status"] == "waiting" and t["next_action"] is None for t in board["tasks"]))
        for status in ({"alive": False}, {"ready": False}, {"dimension": "the_nether"}, {"gamemode": "creative"}):
            obs = ready_world()
            obs["status"].update(status)
            self.assertFalse(self.goals.build(obs)["maintenance_ready"])

    def test_memories_are_candidates_never_current_facilities_or_items(self):
        memories = {"places": [verified([5, 64, 5], category="bed", name="white_bed")],
                    "episodes": [{"summary": "All food and iron tools obtained"}]}
        board = self.goals.build(observation(), memories)
        self.assertFalse(board["maintenance_ready"])
        self.assertEqual(self.task(observation(), "bed", memory=memories)["status"], "blocked")
        self.assertTrue(board["revalidate_candidates"][0]["requires_revalidation"])

    def test_other_dimension_and_excess_memories_are_filtered(self):
        places = [verified([i, 64, 5]) for i in range(12)]
        places.insert(0, verified([1, 64, 5], dimension="the_nether"))
        board = self.goals.build(observation(), {"places": places})
        self.assertEqual(len(board["revalidate_candidates"]), 8)
        self.assertTrue(all(p["dimension"] == "overworld" for p in board["revalidate_candidates"]))

    def test_food_uses_only_mature_safe_crops_and_skips_cooldowns(self):
        obs = observation(blocks=[block("wheat", [2, 64, 0], age=6),
                                  block("carrots", [3, 64, 0], age=7), block("potatoes", [4, 64, 0], age=7)])
        task = self.task(obs, "food_reserve", context={"blocked_targets": ["3,64,0"]})
        self.assertEqual(task["next_action"]["args"], {"pos": [4, 64, 0], "expected_name": "potatoes"})
        obs["entities"] = [{"type": "hostile", "name": "zombie", "pos": [3, 64, 0]}]
        self.assertIsNone(self.task(obs, "food_reserve")["next_action"])

    def test_bread_needs_a_workbench_and_uses_incremental_amount(self):
        obs = observation({"wheat": 9})
        first = self.task(obs, "food_reserve")["next_action"]
        self.assertNotEqual(first["args"].get("item"), "bread")
        obs["blocks"].append(block("crafting_table", [2, 64, 0]))
        action = self.task(obs, "food_reserve")["next_action"]
        self.assertEqual(action["args"], {"item": "bread", "count": 3})

    def test_hunting_requires_usable_weapon_health_food_and_near_animal(self):
        obs = observation()
        obs["entities"] = [{"id": 7, "name": "cow", "type": "passive", "pos": [3, 64, 0]}]
        self.assertIsNone(self.task(obs, "food_reserve")["next_action"])
        obs["counts"]["stone_axe"] = 1
        self.assertEqual(self.task(obs, "food_reserve")["next_action"]["name"], "hunt_animal")
        for status in ({"health": 11}, {"food": 9}):
            changed = copy.deepcopy(obs)
            changed["status"].update(status)
            self.assertIsNone(self.task(changed, "food_reserve")["next_action"])
        obs["entities"][0]["pos"] = [9, 64, 0]
        self.assertIsNone(self.task(obs, "food_reserve")["next_action"])

    def test_raw_food_waits_for_cooking_instead_of_more_hunting(self):
        obs = observation({"beef": 2, "stone_axe": 1})
        obs["entities"] = [{"id": 7, "name": "cow", "pos": [3, 64, 0]}]
        self.assertIsNone(self.task(obs, "food_reserve")["next_action"])

    def test_smelt_starts_only_after_current_empty_window_and_never_counts_as_food(self):
        obs = observation({"beef": 4, "coal": 1}, [block("furnace", [2, 64, 0])])
        self.assertEqual(self.task(obs, "food_reserve")["next_action"]["name"], "inspect_container")
        obs["furnaces"] = [verified([2, 64, 0], input={}, output={}, fuel={}, burning=False)]
        action = self.task(obs, "food_reserve")["next_action"]
        self.assertEqual(action["name"], "smelt_start")
        self.assertEqual(action["args"]["count"], 4)
        self.assertEqual(action["args"]["fuel_count"], 1)
        task = self.task(obs, "food_reserve", context={"last_result": {"success": True, "data": {"stage": "started", "completed": False}}})
        self.assertNotEqual(task["status"], "satisfied")

    def test_pending_job_reinspects_without_replaying_input_transfer(self):
        obs = observation({}, [block("furnace", [2, 64, 0])])
        context = {"smelting_jobs": [{"pos": [2, 64, 0], "dimension": "overworld", "expected_output": "iron_ingot"}]}
        action = self.task(obs, "iron_tools", context=context)["next_action"]
        self.assertEqual(action["name"], "inspect_container")
        obs["furnaces"] = [verified([2, 64, 0], input={"name": "raw_iron", "count": 3}, output={}, burning=True)]
        self.assertEqual(self.task(obs, "iron_tools", context=context)["next_action"]["name"], "wait")
        obs["furnaces"][0].update(input={}, output={"name": "iron_ingot", "count": 3}, burning=False)
        self.assertEqual(self.task(obs, "iron_tools", context=context)["next_action"]["name"], "smelt_collect")

    def test_busy_or_missing_inventory_is_unknown_not_empty(self):
        for mode in ("busy", "missing", "invalid"):
            obs = ready_world()
            if mode == "busy":
                obs["status"]["busy"] = "craft"
            elif mode == "missing":
                obs.pop("counts")
                obs.pop("inventory")
            else:
                obs["counts"]["iron_pickaxe"] = True
            board = self.goals.build(obs)
            self.assertFalse(board["maintenance_ready"])
            self.assertTrue(all(row["status"] == "waiting" for row in board["tasks"]))

    def test_busy_furnace_input_fuel_or_burning_are_not_refilled(self):
        for busy in ({"input": {"name": "beef", "count": 1}}, {"fuel": {"name": "coal", "count": 1}}, {"burning": True}):
            obs = observation({"beef": 4, "coal": 1}, [block("furnace", [2, 64, 0])])
            obs["furnaces"] = [verified([2, 64, 0], **busy)]
            action = self.task(obs, "food_reserve")["next_action"]
            self.assertIsNone(action)

    def test_only_current_output_can_be_collected_and_raw_iron_does_not_complete_tools(self):
        obs = observation({"raw_iron": 10}, [block("furnace", [2, 64, 0])])
        obs["furnaces"] = [verified([2, 64, 0], output={"name": "iron_ingot", "count": 3})]
        task = self.task(obs, "iron_tools")
        self.assertEqual(task["next_action"]["name"], "smelt_collect")
        self.assertEqual(task["next_action"]["args"]["count"], 3)
        self.assertNotEqual(task["status"], "satisfied")
        obs["furnaces"][0]["fresh"] = False
        self.assertIsNone(self.task(obs, "iron_tools")["next_action"])

    def test_charcoal_can_start_with_logs_and_plank_fuel(self):
        obs = observation({"oak_log": 3, "oak_planks": 2}, [block("furnace", [2, 64, 0])])
        obs["furnaces"] = [verified([2, 64, 0], input={}, fuel={}, output={}, burning=False)]
        action = self.task(obs, "fuel_reserve")["next_action"]
        self.assertEqual(action["name"], "smelt_start")
        self.assertEqual(action["args"], {"pos": [2, 64, 0], "input_item": "oak_log", "count": 3, "fuel_item": "oak_planks", "fuel_count": 2})

    def test_furnace_prerequisites_continue_after_stone_tools(self):
        obs = observation({"stone_pickaxe": 1, "stone_axe": 1, "stone_sword": 1})
        action = self.task(obs, "furnace")["next_action"]
        self.assertEqual(action["name"], "gather_block")
        self.assertEqual(action["args"]["expected_name"], "stone")
        obs["counts"]["furnace"] = 1
        task = self.task(obs, "furnace")
        self.assertNotEqual(task["status"], "satisfied")
        self.assertEqual(task["next_action"]["name"], "place_item")

    def test_iron_ore_requires_stone_pickaxe_and_uses_new_skill(self):
        obs = observation({"wooden_pickaxe": 1}, [block("iron_ore", [3, 64, 0])])
        self.assertIsNone(self.task(obs, "iron_tools")["next_action"])
        obs["counts"]["stone_pickaxe"] = 1
        action = self.task(obs, "iron_tools")["next_action"]
        self.assertEqual(action["name"], "gather_resource")
        self.assertEqual(action["args"]["expected_name"], "iron_ore")

    def test_held_iron_without_sticks_plans_sticks_instead_of_more_ore(self):
        obs = observation({"iron_ingot": 3, "oak_planks": 2})
        self.assertEqual(self.task(obs, "iron_tools")["next_action"]["args"], {"item": "stick", "count": 4})

    def test_bed_requires_matching_two_unoccupied_halves(self):
        obs = ready_world()
        self.assertEqual(self.task(obs, "bed")["status"], "satisfied")
        for mode in ("head_missing", "head_occupied", "wrong_facing", "unknown_occupied"):
            changed = copy.deepcopy(obs)
            if mode == "head_missing":
                changed["blocks"].pop()
            elif mode == "head_occupied":
                changed["blocks"][-1]["properties"]["occupied"] = True
            elif mode == "wrong_facing":
                changed["blocks"][-1]["properties"]["facing"] = "north"
            else:
                changed["blocks"][-1]["properties"].pop("occupied")
            self.assertNotEqual(self.task(changed, "bed")["status"], "satisfied", mode)

    def test_held_bed_and_box_are_not_deployed_and_need_verified_base(self):
        obs = observation({"white_bed": 1, "chest": 1})
        for name in ("bed", "storage"):
            self.assertIsNone(self.task(obs, name)["next_action"])
        obs["daily_evidence"] = {"base_site": verified()}
        self.assertIsNone(self.task(obs, "bed")["next_action"])
        obs["daily_evidence"]["shelter"] = verified(shelter_verified=True)
        obs["status"]["pos"] = [2.5, 64, 2.5]
        obs["placement_candidates"] = [{"pos": [2, 64, 3]}]
        self.assertEqual(self.task(obs, "bed")["next_action"]["name"], "place_bed")
        self.assertEqual(self.task(obs, "storage")["next_action"]["name"], "place_item")

    def test_deployed_chest_needs_current_contents_not_just_a_block(self):
        obs = observation(blocks=[block("chest", [2, 64, 0])])
        task = self.task(obs, "storage")
        self.assertNotEqual(task["status"], "satisfied")
        self.assertEqual(task["next_action"]["name"], "inspect_container")
        obs["containers"] = [verified([2, 64, 0], name="chest", items=[])]
        self.assertEqual(self.task(obs, "storage")["status"], "satisfied")

    def test_food_and_fuel_can_be_withdrawn_only_from_current_safe_storage(self):
        obs = observation()
        obs["containers"] = [verified([2, 64, 2], name="chest", items=[
            {"name": "bread", "count": 3}, {"name": "coal", "count": 20}])]
        food = self.task(obs, "food_reserve")["next_action"]
        self.assertEqual(food["name"], "take_items")
        self.assertEqual(food["args"]["count"], 3)
        self.assertEqual(self.task(obs, "fuel_reserve")["next_action"]["args"]["count"], 8)
        obs["containers"][0]["fresh"] = False
        self.assertIsNone(self.task(obs, "food_reserve")["next_action"])

    def test_surplus_storage_preserves_working_materials_and_never_stores_tools(self):
        obs = ready_world()
        obs["counts"]["cobblestone"] = 64
        task = self.task(obs, "storage")
        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["next_action"]["args"], {"pos": [2, 64, 2], "item": "cobblestone", "count": 32})
        obs["counts"]["cobblestone"] = 32
        self.assertEqual(self.task(obs, "storage")["status"], "satisfied")

    def test_current_stored_backup_tools_can_replace_death_losses_before_mining(self):
        obs = observation()
        obs["containers"] = [verified([2, 64, 2], name="chest", items=[{"name": "iron_pickaxe", "count": 1}])]
        for goal in ("starter_tools", "iron_tools"):
            action = self.task(obs, goal)["next_action"]
            self.assertEqual(action["name"], "take_items")
            self.assertEqual(action["args"]["item"], "iron_pickaxe")

    def test_tilling_needs_known_source_water_air_base_and_hoe(self):
        obs = observation({"stone_hoe": 1, "wheat_seeds": 2}, [block("grass_block", [2, 63, 0]),
            block("water", [4, 63, 0], level=0), block("air", [2, 64, 0])])
        obs["daily_evidence"] = {"base_site": verified([2, 64, 2])}
        self.assertEqual(self.task(obs, "food_production")["next_action"], {
            "name": "till_soil", "args": {"pos": [2, 63, 0]},
            "reason": "Prepare one observed irrigated soil cell", "goal": "food_production"})
        obs["blocks"][1]["properties"]["level"] = 1
        self.assertIsNone(self.task(obs, "food_production")["next_action"])

    def test_farm_hoe_recipe_has_crafting_table_prerequisites(self):
        obs = observation({"cobblestone": 2, "stick": 2, "wheat_seeds": 1}, [
            block("grass_block", [2, 63, 0]), block("water", [4, 63, 0], level=0),
            block("air", [2, 64, 0]), block("crafting_table", [2, 64, 2])])
        obs["daily_evidence"] = {"base_site": verified([2, 64, 2])}
        self.assertEqual(self.task(obs, "food_production")["next_action"]["args"], {"item": "stone_hoe", "count": 1})

    def test_bed_wool_acquisition_reuses_passive_hunting_guards(self):
        obs = observation({"stone_axe": 1})
        obs["entities"] = [{"id": 2, "name": "sheep", "pos": [3, 64, 0]}]
        self.assertEqual(self.task(obs, "bed")["next_action"]["name"], "hunt_animal")
        obs["status"]["health"] = 11
        self.assertIsNone(self.task(obs, "bed")["next_action"])

    def test_completed_starter_tools_never_finish_the_daily_harness(self):
        obs = observation({"stone_pickaxe": 1, "stone_axe": 1, "stone_sword": 1, "furnace": 1})
        self.assertNotEqual(self.goals.choose(obs).name, "goal_complete")
        self.assertFalse(self.goals.build(obs)["maintenance_ready"])

    def test_critical_hunger_can_harvest_food_but_not_resume_resource_work(self):
        obs = observation(blocks=[block("carrots", [2, 64, 0], age=7)], food=4)
        self.assertEqual(self.goals.choose(obs).name, "harvest_crop")
        obs["blocks"] = [block("oak_log", [2, 64, 0])]
        self.assertEqual(self.goals.choose(obs).name, "wait")

    def test_sparse_farm_and_shelter_claims_cannot_satisfy_maintenance(self):
        obs = ready_world()
        obs["daily_evidence"].pop("shelter")
        self.assertFalse(self.goals.build(obs)["maintenance_ready"])
        obs = ready_world()
        obs["daily_evidence"]["food_production"].pop("renewable")
        self.assertFalse(self.goals.build(obs)["maintenance_ready"])

    def shelter_world(self, **changes):
        obs = observation({"oak_planks": 8})
        obs["daily_evidence"] = {"base_site": verified([0, 64, 0])}
        obs["shelter"] = verified([0, 64, 0], geometry={"floor_verified": True, "exit_verified": True},
                                  missing_blocks=55, next_gap={"pos": [2, 64, 2]}, **changes)
        return obs

    def test_local_base_candidate_is_only_a_plan_and_skips_failed_sites(self):
        obs = observation()
        obs["safe_stand_candidates"] = [{"pos": [2.5, 64, .5]}]
        task = self.task(obs, "base_site", context={"blocked_targets": ["0,64,0"]})
        self.assertNotEqual(task["status"], "satisfied")
        self.assertEqual(task["next_action"]["name"], "plan_shelter")
        self.assertEqual(task["next_action"]["args"]["pos"], [2, 64, 0])

    def test_building_requires_current_shelter_snapshot(self):
        obs = self.shelter_world()
        self.assertEqual(self.task(obs, "shelter")["next_action"]["name"], "build_shelter_step")
        obs["shelter"]["fresh"] = False
        self.assertEqual(self.task(obs, "shelter")["next_action"]["name"], "inspect_shelter")

    def test_changed_exit_or_occupied_blueprint_blocks_construction(self):
        obs = self.shelter_world()
        obs["shelter"]["geometry"]["exit_verified"] = False
        self.assertIsNone(self.task(obs, "shelter")["next_action"])
        obs["shelter"]["geometry"]["exit_verified"] = True
        obs["shelter"]["occupied_blueprint_cells"] = [{"pos": [2, 64, 2], "name": "chest"}]
        self.assertIsNone(self.task(obs, "shelter")["next_action"])

    def test_construction_moves_only_to_a_new_checked_stand_cell(self):
        obs = self.shelter_world(needs_position=True, stand_candidates=[[.5, 64, .5], [2.5, 64, .5]])
        action = self.task(obs, "shelter")["next_action"]
        self.assertEqual(action["name"], "move_to")
        self.assertEqual(action["args"], {"pos": [2.5, 64, .5], "range": .5})
        self.assertIsNone(self.task(obs, "shelter", context={"blocked_targets": ["2,64,0"]})["next_action"])

    def test_missing_building_material_plans_bounded_plank_conversion(self):
        obs = self.shelter_world()
        obs["counts"] = {"birch_log": 2}
        self.assertEqual(self.task(obs, "shelter")["next_action"]["args"], {"item": "birch_planks", "count": 8})

    def test_complete_walls_and_roof_need_the_separately_verified_door(self):
        obs = self.shelter_world()
        obs["shelter"].update(missing_blocks=0, needs_door={"pos": [0, 64, -2], "allowed_facing": ["north", "south"]})
        obs["counts"]["oak_door"] = 1
        self.assertEqual(self.task(obs, "shelter")["next_action"], {
            "name": "place_door", "args": {"pos": [0, 64, -2], "item": "oak_door"},
            "reason": "Place and verify both wooden-door halves", "goal": "shelter"})
        self.assertNotEqual(self.task(obs, "shelter")["status"], "satisfied")
        obs["shelter"]["needs_door"] = None
        self.assertEqual(self.task(obs, "shelter")["next_action"]["name"], "inspect_shelter")

    def test_inspection_success_is_not_shelter_completion(self):
        obs = self.shelter_world()
        obs["daily_evidence"]["shelter"] = verified([0, 64, 0], shelter_verified=False)
        self.assertNotEqual(self.task(obs, "shelter")["status"], "satisfied")
        obs["daily_evidence"]["shelter"]["shelter_verified"] = True
        self.assertEqual(self.task(obs, "shelter")["status"], "satisfied")

    def test_bed_and_storage_candidates_must_not_fill_future_walls(self):
        obs = self.shelter_world()
        obs["counts"].update(white_bed=1, chest=1)
        obs["daily_evidence"]["shelter"] = verified([0, 64, 0], shelter_verified=True)
        self.assertIsNone(self.task(obs, "bed")["next_action"])
        self.assertIsNone(self.task(obs, "storage")["next_action"])
        obs["placement_candidates"] = [{"pos": [1, 64, 1]}]
        self.assertIsNone(self.task(obs, "bed")["next_action"])  # A rear foot would point its head into the wall.
        obs["placement_candidates"] = [{"pos": [1, 64, 0]}]
        obs["status"]["pos"] = [1.5, 64, -.5]
        self.assertEqual(self.task(obs, "bed")["next_action"]["name"], "place_bed")
        for pos in ([0, 64, -1], [0, 64, 0]):
            obs["placement_candidates"] = [{"pos": pos}]
            self.assertIsNone(self.task(obs, "storage")["next_action"])
            self.assertIsNone(self.task(obs, "bed")["next_action"])

    def test_bed_uses_precise_fresh_front_side_stance_before_placing_rearward(self):
        for side in (-1, 1):
            with self.subTest(side=side):
                obs = self.shelter_world()
                obs["status"]["pos"] = [.5, 64, .5]
                obs["counts"]["white_bed"] = 1
                obs["daily_evidence"]["shelter"] = verified([0, 64, 0], shelter_verified=True)
                obs["placement_candidates"] = [{"pos": [side, 64, 0]}]
                self.assertIsNone(self.task(obs, "bed")["next_action"])
                obs["safe_stand_candidates"] = [{"pos": [side + .5, 64, -.5], "support": "stone"}]
                action = self.task(obs, "bed")["next_action"]
                self.assertEqual(action["name"], "position_for_task")
                self.assertEqual(action["args"], {"pos": [side + .5, 64, -.5]})
                obs["status"]["pos"] = action["args"]["pos"]
                placed = self.task(obs, "bed")["next_action"]
                self.assertEqual(placed["name"], "place_bed")
                self.assertEqual(placed["args"], {"item": "white_bed", "pos": [side, 64, 0]})
                self.assertNotEqual(self.task(obs, "bed")["status"], "satisfied")

    def test_bed_does_not_invent_a_stance_or_repeat_blocked_precise_position(self):
        obs = self.shelter_world()
        obs["status"]["pos"] = [.5, 64, .5]
        obs["counts"]["white_bed"] = 1
        obs["daily_evidence"]["shelter"] = verified([0, 64, 0], shelter_verified=True)
        obs["placement_candidates"] = [{"pos": [-1, 64, 0]}]
        obs["safe_stand_candidates"] = [{"pos": [-.5, 64, -.5], "support": "stone"}]
        for blocked in ("-1,64,0", "-1,64,-1", "-0.5,64,-0.5"):
            with self.subTest(blocked=blocked):
                self.assertIsNone(self.task(obs, "bed", context={"blocked_targets": [blocked]})["next_action"])
        obs["safe_stand_candidates"] = [{"pos": [1.5, 64, -.5], "support": "stone"}]
        self.assertIsNone(self.task(obs, "bed")["next_action"])

    def test_replant_requires_observed_air_above_existing_farmland(self):
        obs = observation({"wheat_seeds": 2}, [block("farmland", [2, 63, 0])])
        self.assertIsNone(self.task(obs, "food_production")["next_action"])
        obs["terrain"] = [block("air", [2, 64, 0])]
        self.assertEqual(self.task(obs, "food_production")["next_action"]["name"], "plant_crop")
        obs["terrain"][0]["name"] = "wheat"
        self.assertIsNone(self.task(obs, "food_production")["next_action"])

    def test_no_seeds_can_try_one_fresh_visible_grass_after_verified_base(self):
        grass = {**block("short_grass", [2, 64, 0]), "fresh": True, "can_see": True}
        obs = observation(blocks=[grass])
        self.assertIsNone(self.task(obs, "food_production")["next_action"])
        obs["daily_evidence"] = {"base_site": verified([0, 64, 0])}
        task = self.task(obs, "food_production")
        self.assertEqual(task["next_action"]["name"], "gather_seeds")
        self.assertEqual(task["next_action"]["args"], {"pos": [2, 64, 0], "expected_name": "short_grass"})
        self.assertNotEqual(task["status"], "satisfied")
        task = self.task(obs, "food_production", context={"last_result": {"success": True, "data": {"gained": 0, "completed": False}}})
        self.assertNotEqual(task["status"], "satisfied")
        obs["blocks"] = [block("air", [2, 64, 0])]
        self.assertIsNone(self.task(obs, "food_production")["next_action"])
        obs["counts"]["wheat_seeds"] = 1
        obs["blocks"].append(block("farmland", [2, 63, 0]))
        self.assertEqual(self.task(obs, "food_production")["next_action"]["name"], "plant_crop")

    def test_seed_attempt_rejects_stale_occluded_unsafe_or_upper_half_plants(self):
        for problem in ("stale", "unknown_sight", "occluded", "upper_half", "far", "blocked", "hostile", "waterlogged", "already_seeded"):
            grass = {**block("tall_grass", [2, 64, 0], half="lower"), "fresh": True, "can_see": True}
            obs = observation(blocks=[grass])
            obs["daily_evidence"] = {"base_site": verified([0, 64, 0])}
            context = {}
            if problem == "stale": grass["fresh"] = False
            if problem == "unknown_sight": grass.pop("can_see")
            if problem == "occluded": grass["can_see"] = False
            if problem == "upper_half": grass["properties"]["half"] = "upper"
            if problem == "far": grass["pos"] = [8, 64, 0]
            if problem == "blocked": context["blocked_targets"] = ["2,64,0"]
            if problem == "hostile": obs["entities"] = [{"type": "hostile", "name": "zombie", "pos": [3, 64, 0]}]
            if problem == "waterlogged": grass["properties"]["waterlogged"] = True
            if problem == "already_seeded": obs["counts"]["carrot"] = 1
            with self.subTest(problem=problem):
                self.assertIsNone(self.task(obs, "food_production", context=context)["next_action"])
        obs = observation(blocks=[{**block("tall_grass", [2, 64, 0], half="lower"), "fresh": True, "can_see": True}])
        obs["daily_evidence"] = {"base_site": verified([0, 64, 0])}
        self.assertEqual(self.task(obs, "food_production")["next_action"]["args"]["expected_name"], "tall_grass")

    def test_death_recovery_only_picks_visible_safe_drops_and_never_replays_death_pos(self):
        obs = observation(last_death={"pos": [999, 60, 999]})
        ctx = {"death_count": 1}
        self.assertIsNone(self.task(obs, "recover_drops", context=ctx)["next_action"])
        obs["entities"] = [{"id": 3, "type": "item", "pos": [2, 64, 0], "item": {"name": "stone_pickaxe"}}]
        action = self.task(obs, "recover_drops", context=ctx)["next_action"]
        self.assertEqual(action["args"], {"entity_id": 3, "expected_item": "stone_pickaxe"})
        obs["entities"].append({"type": "hostile", "name": "zombie", "pos": [3, 64, 0]})
        self.assertIsNone(self.task(obs, "recover_drops", context=ctx)["next_action"])

    def test_death_recovery_parses_official_item_string_and_not_entity_name(self):
        obs = observation()
        obs["entities"] = [{"id": 21, "type": "item", "name": "item", "pos": [2, 64, 0], "item": "iron_ingotx3"}]
        action = self.task(obs, "recover_drops", context={"death_count": 1})["next_action"]
        self.assertEqual(action["args"], {"entity_id": 21, "expected_item": "iron_ingot"})
        for invalid in (None, "iron_ingot", "iron_ingotx0", "iron_ingotx-3", "iron_ingotx3.0", "iron_ingotx3 ", "IRON_INGOTx3"):
            obs["entities"][0]["item"] = invalid
            with self.subTest(item=invalid):
                self.assertIsNone(self.task(obs, "recover_drops", context={"death_count": 1})["next_action"])

    def test_visible_log_batch_is_bounded_by_current_deficit_and_known_targets(self):
        obs = observation(blocks=[{**block("oak_log", [x, 64, z]), "can_see": True}
                                  for x in range(2, 6) for z in range(3)], time_of_day=5000)
        batch = self.task(obs, "wood_reserve")["next_action"]
        self.assertEqual(batch["name"], "gather_materials")
        self.assertEqual(batch["args"]["resource"], "logs")
        self.assertEqual(batch["args"]["count"], 8)
        self.assertLessEqual(batch["args"]["radius"], 8)
        obs["counts"]["oak_log"] = 6
        self.assertEqual(self.task(obs, "wood_reserve")["next_action"]["args"]["count"], 2)
        obs["counts"]["oak_log"] = 7
        self.assertEqual(self.task(obs, "wood_reserve")["next_action"]["name"], "gather_block")

    def test_batch_hints_do_not_hide_occluded_cooldown_or_unknown_nearby_targets(self):
        obs = observation(blocks=[{**block("oak_log", [x, 64, 0]), "can_see": True} for x in (2, 3, 4)], time_of_day=5000)
        for reason in ("occluded", "unknown", "blocked", "capability"):
            changed, context = copy.deepcopy(obs), {}
            if reason == "occluded": changed["blocks"][0]["can_see"] = False
            if reason == "unknown": changed["blocks"][0].pop("can_see")
            if reason == "blocked": context["blocked_targets"] = ["2,64,0"]
            if reason == "capability": context["capabilities"] = list(DAILY_SKILLS - {"gather_materials"})
            with self.subTest(reason=reason):
                self.assertEqual(self.task(changed, "wood_reserve", context=context)["next_action"]["name"], "gather_block")

    def test_stone_batch_respects_furnace_material_deficit(self):
        obs = observation({"wooden_pickaxe": 1, "cobblestone": 6},
            [{**block("stone", [x, 64, 0]), "can_see": True} for x in (2, 3, 4)], time_of_day=5000)
        action = self.task(obs, "furnace")["next_action"]
        self.assertEqual(action["name"], "gather_materials")
        self.assertEqual(action["args"]["resource"], "cobblestone")
        self.assertEqual(action["args"]["count"], 2)
        obs["counts"]["cobblestone"] = 7
        self.assertEqual(self.task(obs, "furnace")["next_action"]["name"], "gather_block")

    def test_shelter_batch_only_caps_current_material_and_does_not_satisfy_structure(self):
        obs = self.shelter_world()
        obs["status"]["time_of_day"] = 5000
        obs["counts"] = {"cobblestone": 20}
        action = self.task(obs, "shelter")["next_action"]
        self.assertEqual(action["name"], "build_shelter_batch")
        self.assertEqual(action["args"]["max_blocks"], 8)
        self.assertNotEqual(self.task(obs, "shelter")["status"], "satisfied")
        obs["counts"]["cobblestone"] = 2
        self.assertEqual(self.task(obs, "shelter")["next_action"]["args"]["max_blocks"], 2)
        obs["status"]["time_of_day"] = 11000
        self.assertEqual(self.task(obs, "shelter")["next_action"]["name"], "build_shelter_step")

    def test_batch_hints_require_daytime_healthy_fed_safe_ground(self):
        obs = observation(blocks=[{**block("oak_log", [x, 64, 0]), "can_see": True} for x in (2, 3)], time_of_day=5000)
        for status in ({"health": 8}, {"food": 6}, {"time_of_day": 11000}, {"time_of_day": None},
                       {"thundering": True}, {"on_ground": False}, {"on_fire": True},
                       {"nearest_hostile": {"dist": 3}}):
            changed = copy.deepcopy(obs)
            changed["status"].update(status)
            with self.subTest(status=status):
                self.assertEqual(self.task(changed, "wood_reserve")["next_action"]["name"], "gather_block")

    def test_maintenance_is_continuous_and_regresses_when_inventory_changes(self):
        obs = ready_world()
        board = self.goals.build(obs)
        self.assertTrue(board["maintenance_ready"])
        self.assertEqual(board["phase"], "maintain")
        self.assertEqual(self.goals.choose(obs).name, "wait")
        self.assertNotEqual(self.goals.choose(obs).name, "goal_complete")
        obs["counts"]["bread"] = 1
        self.assertFalse(self.goals.build(obs)["maintenance_ready"])
        obs["counts"].pop("iron_pickaxe")
        self.assertNotEqual(self.task(obs, "iron_tools")["status"], "satisfied")

    def test_life_hazard_pause_and_sleep_priorities_are_preserved(self):
        for change in ({"alive": False}, {"on_fire": True}, {"on_ground": False}, {"health": 4}):
            obs = ready_world()
            obs["status"].update(change)
            self.assertEqual(self.goals.choose(obs).name, "wait")
        obs = ready_world()
        obs["status"]["is_day"] = False
        self.assertEqual(self.goals.choose(obs).name, "auto_sleep")

    def test_build_is_pure_and_does_not_mutate_context_or_memory(self):
        obs, mem, ctx = ready_world(), {"places": [verified()]}, {"milestones": {"starter_goal_complete": True}}
        original = copy.deepcopy((obs, mem, ctx))
        self.goals.build(obs, mem, ctx)
        self.assertEqual((obs, mem, ctx), original)

    def test_configured_reserves_are_clamped_and_invalid_values_ignored(self):
        planner = DailySurvivalGoals({"daily": {"food_reserve": float("nan"), "wood_reserve": -3, "fuel_reserve": 10000}})
        self.assertEqual(planner.targets, {"food_reserve": 8, "wood_reserve": 1, "fuel_reserve": 64, "maintenance_interval_seconds": 30})


if __name__ == "__main__":
    unittest.main()
