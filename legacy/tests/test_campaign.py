"""Campaign coverage with independent fake game evidence and the normal harness."""

import copy
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.survival_planner import Decision
from core.campaign import Campaign
from core.daily_policy import DailySafetyPolicy
from core.structure_spec import transform_rel_pos
from skills import SkillResult
from skills.action_workflow import WorkflowGuard
from skills.campaign import CampaignSkills, eye_search_target
from test_autonomous_tasks import world
from test_daily_skills import envelope
from test_task_harness import TaskWorld


class CampaignWorld(TaskWorld):
    def __init__(self, dimension="overworld"):
        super().__init__()
        self.status.update(dimension=dimension, pos=[.5, 64, .5], offhand={"name": "shield"})
        self.dragon_completed = False
        self.travel = {}

    def find_blocks(self, types, **kwargs):
        return self.get_blocks(types=types, **{key: value for key, value in kwargs.items() if key in {"radius", "limit"}})

    def get_entities(self, radius=32, types=None, **kwargs):
        return [row for row in super().get_entities(**kwargs)
                if (types is None or row["name"] in types or row.get("type") in types)
                and math.dist(self.status["pos"], row["pos"]) <= radius]

    def place_block(self, x, y, z, *, item, **kwargs):
        return super().place_block(x, y, z, item=item)

    def dig(self, pos, *, collect=True, stop_on=None, timeout_ms=None):
        if self.blocks.get(tuple(pos), {}).get("name", "").endswith("_leaves"):
            self.calls.append(("dig", tuple(pos)))
            self.block(pos, "air")
            return envelope({"dug": True})
        return super().dig(pos, collect=collect)

    def move_to(self, x, y, z, **kwargs):
        self.calls.append(("campaign_move", (x, y, z)))
        travel = self.travel.get((math.floor(x), y, math.floor(z)))
        if travel:
            self.status.update(dimension=travel, pos=[20.5, 64, 30.5])
            return envelope({"pos": self.status["pos"]})
        return super().move_to(x, y, z, **kwargs)

    def advancements(self, **kwargs):
        return envelope({"ready": True, "advancements": [
            {"id": "minecraft:end/kill_dragon", "status": "completed" if self.dragon_completed else "not_started"}]})

    def interact(self, pos=None, item=None, **kwargs):
        if item == "ender_eye":
            self.calls.append(("fill", pos))
            self.counts[item] -= 1
            self.blocks[tuple(pos)]["properties"]["eye"] = True
            return envelope({"changed": True})
        return super().interact(pos=pos, item=item, **kwargs)


class CampaignTests(unittest.TestCase):
    def test_cornered_with_empty_hands_defends_against_fresh_close_zombie(self):
        client = CampaignWorld()
        client.status.update(health=4, nearest_hostile={"id": 7, "name": "zombie", "dist": 1.5})
        client.entities = [{"id": 7, "name": "zombie", "type": "hostile", "pos": [2, 64, .5]}]

        def attack(entity_id, **options):
            self.assertEqual((entity_id, options), (7, {"timeout_ms": 450, "max_hits": 1}))
            return envelope({"hits": 1, "killed": False})

        client.attack = attack
        campaign = Campaign({})
        observation = {"fresh": True, "status": client.status}
        wait = Decision("wait", {"seconds": 1}, "No retreat", "safety")
        decision = campaign.melee_recovery(wait, observation, {})
        self.assertEqual(decision.args["operation"], "defend_melee")
        result = CampaignSkills(client).execute(decision.args, campaign.state)
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["swings"], 1)
        campaign.record(decision, result)
        self.assertEqual(campaign.melee_recovery(wait, observation, {}).args["seconds"], .25)
        client.status["nearest_hostile"]["name"] = "creeper"
        self.assertEqual(Campaign({}).melee_recovery(wait, observation, {}).args["operation"], "escape_pressure")
        client.status["nearest_hostile"]["name"] = "zombie_villager"
        self.assertEqual(Campaign({}).melee_recovery(wait, observation, {}).args["mob"], "zombie_villager")

    def test_emergency_food_replaces_food_wait_with_observed_safe_raw_food(self):
        client = CampaignWorld()
        client.status.update(health=6, food=17)
        client.counts["mutton"] = 1
        campaign = Campaign({})
        observation = world(health=6, food=17)
        wait = DailySafetyPolicy({"low_health_threshold": 8}).choose(observation, None)
        action = campaign.food_recovery(wait, observation)
        # The low-health skill reads inventory again before making its decision.
        result = CampaignSkills(client).execute(action.args, campaign.state)
        self.assertTrue(result.success, result)
        self.assertEqual(client.status["food"], 20)
        self.assertEqual(result.data["ate"], "mutton")

    def test_campaign_surfaces_with_bounded_input_from_a_confirmed_submerged_pose(self):
        client = CampaignWorld()
        client.status.update(in_water=True, submerged=True, on_ground=False)
        def control(**options):
            self.assertEqual(options, {"jump": True, "ms": 750, "wait": True})
            client.status.update(submerged=False, pos=[.5, 65, .5])
            return envelope({})
        client.control = control
        campaign = Campaign({})
        action = campaign.water_recovery(client.status)
        self.assertEqual(action.args["operation"], "surface_water")
        result = CampaignSkills(client).execute(action.args, campaign.state)
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["reached_air"])

    def test_campaign_recovers_also_when_wading_on_the_bottom(self):
        status = world(on_ground=True, in_water=True, submerged=False)["status"]
        action = Campaign({}).water_recovery(status)
        self.assertEqual(action.args["operation"], "surface_water")

    def test_field_cover_places_its_checked_walls_and_roof_around_player(self):
        client = CampaignWorld()
        client.counts["dirt"] = 10
        client.status.update(time_of_day=11000)
        client.entities = [{"id": 4, "name": "zombie", "type": "hostile", "pos": [10.5, 64, .5]}]
        client.get_status()
        campaign = Campaign({"needs_field_base": True})
        runner = CampaignSkills(client)
        for _ in range(10):
            action = campaign.night_cover({**world(), "status": client.status, "counts": client.counts})
            result = runner.execute(action.args, campaign.state)
            self.assertTrue(result.success, result)
            campaign.record(action, result)
        self.assertEqual(client.blocks[0, 66, 0]["name"], "dirt")
        self.assertEqual(len(campaign.state["night_cover"]["placed"]), 10)
        self.assertEqual(client.status["pos"], [.5, 64, .5])
        client.entities.clear()
        client.get_status()
        self.assertEqual(campaign.night_cover({**world(), "status": client.status, "counts": client.counts})
                         .args["operation"], "open_cover")

    def test_quiet_night_does_not_start_a_temporary_enclosure(self):
        obs = world({"dirt": 24}, is_day=False, time_of_day=14000)
        self.assertIsNone(Campaign({"needs_field_base": True}).night_cover(obs))

    def test_completed_house_adds_a_rotated_five_by_three_porch(self):
        client = CampaignWorld()
        anchor, orientation = [0, 64, 0], "east"
        cell = lambda pos: transform_rel_pos(pos, anchor, orientation, integer=True)
        client.status.update(pos=transform_rel_pos((.5, 0, 3.5), anchor, orientation, integer=False))
        client.counts.update(oak_planks=17, cobblestone=2)
        for x in range(-2, 3):
            client.block(cell((x, 2, 2)), "oak_planks")
        shelter = {"pos": anchor, "orientation": orientation, "dimension": "overworld",
                   "fresh": True, "verified": True, "shelter_verified": True}
        campaign, skill = Campaign({}), CampaignSkills(client)
        for _ in range(36):
            obs = {**world(client.counts), "status": client.status,
                   "daily_evidence": {"shelter": shelter}}
            action = campaign.choose(obs)
            self.assertEqual(action.args.get("operation"), "house_porch")
            result = skill.execute(action.args, campaign.state)
            self.assertTrue(result.success, result)
            campaign.record(action, result)
            if result.data["porch"]["complete"]:
                break
        self.assertTrue(campaign.state["porch"]["complete"])
        self.assertEqual(client.counts["cobblestone"], 0)
        self.assertEqual(client.counts["oak_planks"], 0)
        for z in range(3, 6):
            for x in range(-2, 3):
                self.assertEqual(client.blocks[tuple(cell((x, 2, z)))]["name"], "oak_planks")
        self.assertNotIn(tuple(cell((0, 0, 3))), client.blocks)

    def test_failed_primary_retreat_can_try_a_checked_short_way_out_while_enemy_closes(self):
        client = CampaignWorld()
        client.entities = [{"id": 4, "name": "zombie", "type": "hostile", "pos": [-4, 64, .5]}]
        client.status["nearest_hostile"] = {"id": 4, "name": "zombie", "dist": 4.5}
        action = Campaign({}).melee_recovery(Decision("wait", {"seconds": 5}, "Blocked", "safety"),
            {"fresh": True, "status": client.status}, {"retry_after": time.time() + 5})
        self.assertEqual(action.args["operation"], "escape_pressure")
        result = CampaignSkills(client).execute(action.args, {})
        self.assertTrue(result.success, result)
        self.assertGreater(result.data["position"][0], 2)

    def test_ranged_pressure_does_not_hold_still_for_five_seconds(self):
        status = world(nearest_hostile={"id": 4, "name": "skeleton", "dist": 12})["status"]
        wait = Decision("wait", {"seconds": 5}, "Blocked", "safety")
        decision = Campaign({}).melee_recovery(wait, {"status": status, "fresh": True},
                                              {"retry_after": time.time() + 5})
        self.assertEqual(decision.args["operation"], "escape_pressure")

    def test_melee_escape_can_take_the_only_step_along_solid_cover(self):
        client = CampaignWorld()
        for x in range(-3, 4):
            for z in range(-3, 4):
                if (x, z) not in {(0, 0), (1, 0)}:
                    for y in (62, 63):
                        client.block((x, y, z), "air")
        for y in (64, 65):
            client.block((3, y, 0), "stone")
        client.entities = [{"id": 4, "name": "spider", "type": "hostile", "pos": [6.9, 64, .5]}]
        skill = CampaignSkills(client)
        action = {"operation": "escape_pressure", "attacker_id": 4}
        result = skill.execute(action, {})
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["position"], [1.5, 64, .5])
        client.status["pos"] = [.5, 64, .5]
        for y in (64, 65):
            client.block((3, y, 0), "air")
        result = skill.execute(action, {})
        self.assertEqual(result.error_code, "campaign_no_landing")

    def test_unseen_attack_can_use_a_checked_sidestep(self):
        client = CampaignWorld()
        skill = CampaignSkills(client)
        attempt = skill.execute({"operation": "escape_pressure"}, {})
        self.assertEqual(attempt.error_code, "campaign_no_threat")
        escaped = skill.execute({"operation": "escape_pressure", "unseen_attacker": True}, {})
        self.assertTrue(escaped.success, escaped)
        self.assertEqual(escaped.data["position"], [2.5, 64, .5])

    def test_approaching_creeper_gets_a_local_escape_before_the_first_hit(self):
        status = world(nearest_hostile={"id": 4, "name": "creeper", "dist": 4.4})["status"]
        retreat = Decision("retreat_from", {"pos": [4, 64, 0], "distance": 8}, "Blocked", "safety")
        result = Campaign({}).melee_recovery(retreat, {"status": status, "fresh": True}, {})
        self.assertEqual(result.args, {"operation": "escape_pressure", "attacker_id": 4, "imminent_explosion": True})

    def test_a_changed_threat_can_reuse_previously_traversed_safe_escape_cell(self):
        client = CampaignWorld()
        origin = client.status["pos"][:]
        enemy = {"id": 4, "name": "spider", "type": "hostile", "pos": [-4, 64, .5]}
        client.entities = [enemy]
        campaign = Campaign({})
        skill = CampaignSkills(client)
        action = campaign.action("recovery", "escape_pressure", "Escape")
        first = skill.execute(action.args, campaign.state)
        self.assertTrue(first.success, first)
        campaign.record(action, first)
        client.status["pos"] = origin[:]
        second = skill.execute(action.args, campaign.state)
        self.assertTrue(second.success, second)
        self.assertNotEqual(second.data["escape_step"], first.data["escape_step"])

        client.status["pos"] = origin[:]
        enemy.update(id=5, name="creeper")
        campaign.state["rejected_steps"] = [first.data["escape_step"]]
        third = skill.execute(action.args, campaign.state)
        self.assertTrue(third.success, third)
        self.assertEqual(third.data["escape_step"], first.data["escape_step"])

    def test_death_recovery_expires_or_finishes_when_no_matching_drops_exist(self):
        campaign = Campaign({})
        obs = world(last_death={"t": int(time.time() * 1000) - 5000, "dropped_items_at": [17, 64, 3]})
        action = campaign.death_recovery(obs)
        self.assertEqual(action.args["operation"], "recover_gear")
        campaign.record(action, SkillResult(True, "No drops", {"gear_search_done": True}))
        self.assertIsNone(campaign.death_recovery(obs))
        obs["status"]["last_death"]["t"] -= 240000
        self.assertIsNone(Campaign({}).death_recovery(obs))

    def test_interrupted_deep_mine_uses_its_own_life_entry_before_crafting(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "episodes.jsonl"
            scope = {"world_id": "campaign-world"}
            journal = SimpleNamespace(scope=scope, state={"scope": scope}, events_path=path)
            observation = world(pos=[6, 20, .5], last_death={"t": 123})
            previous = {**observation["status"], "pos": [.5, 64, .5]}
            event = {"event": "DECISION", "data": {"decision": {"goal": "deep_mine"}, "status": previous}}
            path.write_text(json.dumps(event) + "\n", encoding="utf-8")
            campaign = Campaign({"view": {"stage": "water"}})
            action = campaign.mine_recovery(observation, journal)
            self.assertEqual(action.args, {"operation": "exit_shaft", "anchor": [.5, 64, .5]})
            observation["status"]["last_death"] = {"t": 456}
            self.assertIsNone(campaign.mine_recovery(observation, journal))

    def test_recovery_of_a_stronger_tool_abandons_the_obsolete_wooden_goal(self):
        task = {"status": "running", "args": {"task": "craft", "item": "wooden_pickaxe"}}
        self.assertTrue(Campaign.obsolete_opening_task(task, world({"stone_pickaxe": 1})))
        self.assertFalse(Campaign.obsolete_opening_task(task, world({"oak_log": 1})))

    def test_exhausted_food_goal_checks_area_before_restarting_same_goal(self):
        campaign = Campaign({})
        obs = world({"stone_pickaxe": 1, "stone_sword": 1}, food=17)
        goal = campaign.choose(obs)
        previous = {"task_id": "food-1", "status": "needs_ai", "args": goal.args}
        step = campaign.recover_task(goal, previous)
        self.assertEqual(step.args["operation"], "seek_food")
        campaign.record(step, SkillResult(False, "Path blocked"))
        self.assertEqual(campaign.recover_task(goal, previous).args["operation"], "seek_food")
        campaign.record(step, SkillResult(True, "New patch", {"survey_step": [6, 64, 0]}))
        self.assertEqual(campaign.recover_task(goal, previous), goal)

    def test_wood_search_stays_active_through_approach_and_dig_until_pickup(self):
        campaign = Campaign({})
        goal = campaign.choose(world())
        previous = {"task_id": "wood-1", "status": "needs_ai", "args": goal.args}
        action = campaign.recover_task(goal, previous)
        self.assertEqual(action.args["operation"], "seek_ground_wood")
        for evidence in ({"survey_step": [1, 64, 0]}, {"wood_opened": True}):
            campaign.record(action, SkillResult(True, "Wood search progressed", evidence))
            self.assertEqual(campaign.recover_task(goal, previous), action)
        campaign.record(action, SkillResult(True, "Wood in inventory", {"wood_collected": True}))
        self.assertEqual(campaign.recover_task(goal, previous), goal)

    def test_wood_search_clears_one_visible_leaf_then_reobserves_the_root(self):
        client = CampaignWorld()
        client.block([3, 63, 0], "dirt")
        client.block([3, 64, 0], "oak_log")
        client.block([2, 64, 0], "oak_leaves", properties={"waterlogged": False})
        client.block([2, 65, 0], "oak_leaves", properties={"waterlogged": False})
        result = CampaignSkills(client).execute({"operation": "seek_ground_wood"}, {})
        self.assertTrue(result.success, result)
        self.assertIn(result.data["cleared_wood_sight"], ([2, 64, 0], [2, 65, 0]))
        self.assertEqual(client.blocks[3, 64, 0]["name"], "oak_log")

    def test_wood_search_follows_observed_dropped_log_beyond_local_pickup_reach(self):
        client = CampaignWorld()
        client.entities = [{"id": 9, "type": "other", "name": "item", "pos": [14, 64, .5], "item": "oak_logx1"}]
        result = CampaignSkills(client).execute({"operation": "seek_ground_wood"}, {})
        self.assertTrue(result.success, result)
        self.assertGreater(result.data["position"][0], 5)
        self.assertNotIn("wood_collected", result.data)

    def test_timber_sweeps_widen_only_after_rejecting_nearby_columns(self):
        client = CampaignWorld()
        client.block([8, 64, 0], "oak_log")
        client.block([30, 64, 0], "birch_log")
        skill = CampaignSkills(client)
        with patch.object(client, "find_blocks", wraps=client.find_blocks) as scan:
            nearby = skill.execute({"operation": "seek_ground_wood"}, {})
        self.assertTrue(nearby.success, nearby)
        self.assertEqual([call.kwargs["radius"] for call in scan.call_args_list], [12])

        client.status["pos"] = [.5, 64, .5]
        state = {"blocked_wood": [{"pos": [8, 64, 0], "until": time.time() + 30}],
                 "wood_search_steps": 22, "wood_waypoint": [-64, 64, 0]}
        with patch.object(client, "find_blocks", wraps=client.find_blocks) as scan:
            farther = skill.execute({"operation": "seek_ground_wood"}, state)
        self.assertTrue(farther.success, farther)
        self.assertEqual([call.kwargs["radius"] for call in scan.call_args_list], [12, 24, 48])
        self.assertEqual(farther.data["wood_scan_radius"], 48)
        self.assertGreater(farther.data["position"][0], 1)

    def test_wood_search_uses_only_the_supported_scan_radii_when_empty(self):
        client = CampaignWorld()
        with patch.object(client, "find_blocks", wraps=client.find_blocks) as scan:
            result = CampaignSkills(client).execute({"operation": "seek_ground_wood"}, {})
        self.assertTrue(result.success, result)
        self.assertEqual([call.kwargs["radius"] for call in scan.call_args_list], [12, 24, 48, 64])

    def test_directed_walk_rejects_a_grounded_landing_without_goal_progress(self):
        client = CampaignWorld()

        def sideways():
            client.status["pos"] = [.5, 64, 1.4]
            return envelope({})

        client.effects["move"] = sideways
        result = CampaignSkills(client)._walk(WorkflowGuard(client), client.status["pos"][:], {},
                                              toward=[1.5, 64, .5])
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "campaign_waypoint_missed")
        self.assertEqual(result.data["reject_pos"], [1, 64, 0])

    def test_shelter_recovery_checks_other_long_corridors_before_taking_one_short_step(self):
        client = CampaignWorld()
        client.block([2, 64, 0], "stone")
        campaign = Campaign({})
        goal = campaign.task("shelter", "shelter", "Build a verified place")
        previous = {"task_id": "shelter-1", "status": "needs_ai", "args": goal.args}
        action = campaign.recover_task(goal, previous)
        self.assertTrue(action.args["prefer_far"])
        result = CampaignSkills(client).execute(action.args, campaign.state)
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["position"], [.5, 64, 12.5])

    def test_fuel_recovery_mines_reachable_coal_instead_of_seeking_wood(self):
        client = CampaignWorld()
        client.counts["iron_pickaxe"] = 1
        client.block([2, 64, 0], "coal_ore")
        result = CampaignSkills(client).execute({"operation": "recover_fuel"}, {})
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["fuel_collected"])
        self.assertEqual(client.counts["coal"], 1)

    def test_uphill_timber_search_prefers_an_observed_cardinal_step(self):
        client = CampaignWorld()
        client.block([1, 64, 0], "stone")
        client.block([8, 70, 0], "oak_log")
        result = CampaignSkills(client).execute({"operation": "seek_ground_wood"}, {"rejected_steps": [[1, 65, 0]]})
        self.assertTrue(result.success, result)
        self.assertEqual(result.data.get("position"), [1.5, 65, .5])

    def test_remote_animals_avoid_restarting_local_search_and_raw_food_resumes_cooking(self):
        campaign = Campaign({})
        obs = world({"stone_pickaxe": 1, "stone_sword": 1}, food=17)
        step = campaign.action("supplies", "seek_food", "Test survey")
        campaign.record(step, SkillResult(True, "Found cattle", {"observed_supply": [0, 64, 36]}))
        self.assertEqual(campaign.choose(obs).args["operation"], "seek_food")
        obs["counts"]["beef"] = 2
        self.assertEqual(campaign.choose(obs).args["task"], "food")
        next_goal = campaign.choose(obs)
        old = {"task_id": "food", "status": "needs_ai", "args": next_goal.args}
        recovery = campaign.recover_task(next_goal, old, obs)
        self.assertEqual(recovery.args["operation"], "recover_fuel")
        obs["counts"]["oak_planks"] = 2
        self.assertEqual(campaign.recover_task(next_goal, old, obs).args["operation"], "recover_stance")

    def test_well_fed_stone_age_can_prepare_iron_before_building_food_reserve(self):
        campaign = Campaign({})
        self.assertEqual(campaign.choose(world({"stone_pickaxe": 1})).args["item"], "stone_sword")
        obs = world({"stone_pickaxe": 1, "stone_sword": 1})
        self.assertEqual(campaign.choose(obs).args["item"], "iron_pickaxe")
        obs["status"]["food"] = 17
        self.assertEqual(campaign.choose(obs).args["task"], "food")

    def test_missing_equipment_routes_through_normal_bounded_craft_goal(self):
        campaign = Campaign({})
        action = campaign.choose(world())
        self.assertEqual(action.name, "run_survival_task")
        self.assertEqual(action.args, {"task": "craft", "item": "wooden_pickaxe", "count": 1})

    def test_nether_resource_and_return_depend_on_actual_inventory_and_health(self):
        campaign = Campaign({})
        counts = {"iron_pickaxe": 1, "iron_sword": 1, "bread": 5}
        obs = world(counts, dimension="the_nether")
        self.assertEqual(campaign.choose(obs).args["mob"], "blaze")
        obs["counts"]["blaze_rod"] = 8
        self.assertEqual(campaign.choose(obs).args["mob"], "enderman")
        obs["counts"]["ender_pearl"] = 16
        self.assertEqual(campaign.choose(obs).args["operation"], "leave_nether")
        obs["counts"].pop("ender_pearl")
        obs["status"]["health"] = 6
        self.assertEqual(campaign.choose(obs).args["operation"], "leave_nether")

    def test_crossing_verifies_new_dimension_and_retains_the_return_anchor(self):
        client = CampaignWorld()
        client.block([2, 64, 0], "nether_portal")
        client.block([2, 65, 0], "nether_portal")
        client.travel[(2, 64, 0)] = "the_nether"
        runner = CampaignSkills(client)
        campaign = Campaign({})
        action = campaign.action("nether", "enter_nether", "Enter portal")
        result = runner.execute(action.args, campaign.state)
        self.assertTrue(result.success, result)
        campaign.record(action, result)
        self.assertEqual(campaign.state["nether_arrival"], [20.5, 64, 30.5])
        self.assertEqual(campaign.state["overworld_portal"], [2, 64, 0])

    def test_stronghold_fills_only_a_verified_ring_slot_and_checks_consumption(self):
        client = CampaignWorld()
        client.counts["ender_eye"] = 12
        runner = CampaignSkills(client)
        centre = [3, 64, 0]
        for pos, facing in runner._ring(centre):
            client.block(pos, "end_portal_frame", properties={"eye": False, "facing": facing})
        outcome = runner.execute({"operation": "stronghold"}, {})
        self.assertTrue(outcome.success, outcome)
        self.assertEqual(outcome.data["end_eyes_required"], 11)
        self.assertEqual(client.counts["ender_eye"], 11)
        self.assertEqual([row[0] for row in client.calls].count("fill"), 1)

    def test_old_dragon_advancement_cannot_be_credited_as_a_new_kill(self):
        client = CampaignWorld("the_end")
        client.dragon_completed = True
        runner = CampaignSkills(client)
        result = runner.execute({"operation": "dragon"}, {})
        self.assertTrue(result.data["dragon_baseline"])
        self.assertNotIn("dragon_defeated", result.data)

    def test_incomplete_portal_geometry_does_not_fill_frames(self):
        client = CampaignWorld()
        client.counts["ender_eye"] = 12
        runner = CampaignSkills(client)
        positions = list(runner._ring([3, 64, 0]))
        for index, (pos, facing) in enumerate(positions):
            client.block(pos, "end_portal_frame", properties={"eye": False, "facing": facing if index != 3 else "north"})
        result = runner.execute({"operation": "stronghold"}, {})
        self.assertNotIn("filled_frame", result.data)
        self.assertEqual(client.counts["ender_eye"], 12)
        self.assertFalse(any(row[0] == "fill" for row in client.calls))

    def test_unverified_eye_transfer_leaves_no_filled_slot_receipt(self):
        client = CampaignWorld()
        client.counts["ender_eye"] = 12
        runner = CampaignSkills(client)
        for pos, facing in runner._ring([3, 64, 0]):
            client.block(pos, "end_portal_frame", properties={"eye": False, "facing": facing})
        client.interact = lambda **kwargs: envelope({"changed": True})
        outcome = runner.execute({"operation": "stronghold"}, {})
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.data["action_state"], "unknown")
        self.assertNotIn("filled_frame", outcome.data)

    def test_bridge_places_only_one_verified_block_before_any_movement(self):
        client = CampaignWorld("the_end")
        client.block([0, 63, 0], "obsidian")
        client.block([9, 63, 0], "end_stone")
        client.block([1, 63, 0], "air")
        client.counts["cobblestone"] = 3
        result = CampaignSkills(client).execute({"operation": "dragon"}, {"dragon_baseline": False})
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["placed_bridge_block"], [1, 63, 0])
        self.assertEqual(client.counts["cobblestone"], 2)
        self.assertFalse(any(row[0] in {"campaign_move", "dig"} for row in client.calls))

    def test_new_dragon_evidence_and_exit_both_required_for_completion(self):
        client = CampaignWorld("the_end")
        client.dragon_completed = True
        client.block([1, 63, 0], "end_portal")
        client.travel[(1, 64, 0)] = "overworld"
        runner = CampaignSkills(client)
        campaign = Campaign({"dragon_seen": True, "dragon_baseline": False})
        actor = campaign.action("dragon", "dragon", "Check dragon advancement")
        result = runner.execute(actor.args, campaign.state)
        self.assertTrue(result.data["dragon_defeated"])
        campaign.record(actor, result)
        self.assertFalse(campaign.state.get("returned", False))
        actor = campaign.action("leave_end", "leave_end", "Exit the End")
        result = runner.execute(actor.args, campaign.state)
        self.assertTrue(result.success, result)
        campaign.record(actor, result)
        self.assertEqual(campaign.choose(world()).goal, "campaign")
        self.assertEqual(campaign.state["view"]["stage"], "completed")

    def test_eye_intersection_needs_separated_forward_rays(self):
        origin = [0, 64, 0]
        current = {"bearing_deg": 0, "x": 0, "z": 0, "sigma_deg": .1}
        aligned = {"bearing_deg": 0, "x": 0, "z": 50, "sigma_deg": .1}
        oblique = {"bearing_deg": 45, "x": 50, "z": 50, "sigma_deg": .1}
        self.assertEqual(eye_search_target(origin, current, [aligned]), [0, 64, 160])
        target = eye_search_target(origin, current, [oblique])
        self.assertAlmostEqual(target[2], 100)


class CampaignHarnessTests(unittest.TestCase):
    def test_default_cycle_persists_one_real_campaign_step_without_model(self):
        from core.config import DEFAULTS
        from core.survival_harness import SurvivalHarness
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as root:
            settings = copy.deepcopy(DEFAULTS["survival"])
            settings.update(objective="daily", intent="speedrun", campaign=True, world_id="campaign-test",
                            state_dir=root, poll_interval_seconds=.001)
            client = CampaignWorld("nether")
            client.counts = {"bread": 8, "iron_sword": 1, "iron_pickaxe": 1, "ender_eye": 16}
            client.block([2, 64, 0], "nether_portal")
            client.block([2, 65, 0], "nether_portal")
            client.travel[(2, 64, 0)] = "overworld"
            harness = SurvivalHarness(client, settings, DEFAULTS["sleep"])
            try:
                decision, result, observation = harness._cycle(None)
                self.assertEqual(decision.args.get("operation"), "leave_nether", (decision, result))
                self.assertTrue(result.success, result)
                self.assertEqual(observation["status"]["dimension"], "overworld")
                self.assertEqual(harness.snapshot()["campaign"]["stage"], "return_overworld")
                self.assertEqual(harness.journal.state["pending"]["decision"]["name"], "campaign_action")
            finally:
                harness.close()
