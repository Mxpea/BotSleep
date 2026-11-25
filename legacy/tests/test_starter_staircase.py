"""Starter mine integration against the API contract: no grants or live server."""

import threading
import unittest

from core.client import APIError
from skills.starter_staircase import StarterStaircaseSkills
from test_daily_skills import DailyWorld, envelope


class StairWorld(DailyWorld):
    def __init__(self):
        super().__init__()
        self.status.update(is_day=True, speed=0, velocity=[0, 0, 0], last_death=None)
        self.counts = {"wooden_pickaxe": 1}
        self.drops = {}

    def get_block(self, pos, **kwargs):
        result = super().get_block(pos, **kwargs)
        if tuple(pos) not in self.blocks:
            result["result"]["name"] = "air" if pos[1] >= 64 else "dirt" if pos[1] >= 62 else "stone"
        return result

    def dig(self, pos, collect=True):
        assert collect is False
        self.calls.append(("dig", list(pos)))
        block = self.get_block(pos)["result"]["name"]
        self.block(pos, "air")
        self.drops[tuple(pos)] = "cobblestone" if block == "stone" else block
        return envelope({"dug": block})

    def move_to(self, x, y, z, **kwargs):
        result = super().move_to(x, y, z, **kwargs)
        for pos, item in list(self.drops.items()):
            if (pos[0] + .5, pos[2] + .5) == (x, z):
                self.counts[item] = self.counts.get(item, 0) + 1
                del self.drops[pos]
        return result

    def writes(self):
        return [call for call in self.calls if call[0] in ("dig", "move", "stop", "place")]


class StarterStaircaseTests(unittest.TestCase):
    def setUp(self):
        self.world = StairWorld()
        self.skills = StarterStaircaseSkills(self.world)
        self.args = {"entry": [0, 64, 0], "direction": "east"}

    def plan(self, **kw):
        return self.skills.execute("plan_starter_staircase", kw)

    def step(self, **kw):
        return self.skills.execute("step_starter_staircase", {**self.args, **kw})

    def load_north_stairs(self):
        # The second and third treads and their dry side walls from the live
        # north-facing mine. The API rounded x=4.3001, z=27.17777 to 4.3, 27.2.
        self.world = StairWorld()
        self.skills = StarterStaircaseSkills(self.world)
        self.args = {"entry": [4, 69, 29], "direction": "north"}
        self.world.status["pos"] = [4.3, 67, 27.2]
        for x, z, top, surface in ((3, 26, 67, "grass_block"), (3, 27, 67, "grass_block"),
                                   (4, 25, 68, "grass_block"), (4, 26, 65, "stone"),
                                   (4, 27, 66, "grass_block"), (4, 28, 67, "grass_block")):
            for y in range(64, top):
                self.world.block((x, y, z), "stone" if y <= 65 else "dirt")
            self.world.block((x, top, z), surface)
        self.world.block((3, 68, 27), "leaf_litter")
        self.world.block((3, 68, 26), "leaf_litter")

    def test_steps_from_observed_stair_lip_to_rounded_wall_then_digs_and_returns(self):
        self.load_north_stairs()
        def land():
            self.world.status["pos"] = [4.5, 66, 26.3]
            return envelope({})
        self.world.effects["move"] = land
        descend = self.step()
        self.assertTrue(descend.success, descend)
        self.assertEqual(descend.data["lower_tread"], [4, 65, 26])
        self.assertTrue(descend.data["rounded_wall_contact"])
        self.assertGreaterEqual(descend.data["landing_observation"]["status_samples"], 3)
        self.assertEqual(descend.data["depth"], 3)
        dig = self.step()
        self.assertTrue(dig.success, dig)
        self.assertEqual(dig.data["dug_pos"], [4, 68, 25])
        self.world.effects.clear()
        ascend = self.step(returning=True)
        self.assertTrue(ascend.success, ascend)
        self.assertEqual(ascend.data["depth"], 2)
        self.assertEqual([call[0] for call in self.world.writes()], ["move", "dig", "move"])

    def test_partial_stair_contact_rejects_protruding_head_pit_hazard_and_unknown(self):
        for coord, name, props in (([4, 68, 26], "stone", {}), ([4, 65, 26], "air", {}),
                                   ([4, 65, 26], "lava", {}), ([4, 65, 26], "unknown", {}),
                                   ([3, 67, 27], "gravel", {}), ([3, 67, 27], "oak_leaves", {}),
                                   ([3, 67, 27], "stone", {"waterlogged": True})):
            with self.subTest(coord=coord, name=name):
                self.load_north_stairs()
                self.world.block(coord, name, properties=props)
                result = self.step()
                self.assertFalse(result.success, result)
                self.assertEqual(result.data["blocked_cell"], coord)
                self.assertFalse(self.world.writes())

    def test_real_overlap_after_move_remains_unknown_without_a_second_write(self):
        self.load_north_stairs()
        def land():
            self.world.status["pos"] = [4.5, 66, 26.2]
            return envelope({})
        self.world.effects["move"] = land
        result = self.step()
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["landing_observation"]["blocked_cell"], [4, 66, 25])
        self.assertEqual([call[0] for call in self.world.writes()], ["move"])

    def test_rounded_wall_contact_waits_for_zero_speed_samples(self):
        self.load_north_stairs()
        original = self.world.get_status
        samples = []
        def land():
            self.world.status["pos"] = [4.5, 66, 26.3]
            samples.extend([.04, 0, 0, 0])
            return envelope({})
        def status(**kw):
            result = original(**kw)
            if samples:
                speed = samples.pop(0)
                result["result"].update(speed=speed, velocity=[0, 0, speed])
            return result
        self.world.effects["move"], self.world.get_status = land, status
        result = self.step()
        self.assertTrue(result.success, result)
        self.assertIn("stair_contact_motion", result.data["landing_observation"]["first"]["waiting_for"])
        self.assertEqual(result.data["landing_observation"]["status_samples"], 4)

    def test_motion_reset_at_descent_and_ascent_waits_for_real_physics_samples(self):
        read_status = self.world.get_status
        send_move = self.world.move_to
        resets = []

        def move(*args, **kw):
            resets.append(True)
            return send_move(*args, **kw)

        def status(**kw):
            result = read_status(**kw)
            if resets and resets[-1]:
                result["result"].update(speed=None, velocity=None)
                resets[-1] = False
            return result

        self.world.move_to, self.world.get_status = move, status
        for _ in range(5):
            result = self.step()
            self.assertTrue(result.success, result)
            if result.data["operation"] == "descend":
                break
        self.assertEqual(result.data["operation"], "descend")
        self.assertEqual(result.data["landing_observation"]["first"]["waiting_for"], ["motion_sample"])
        returned = self.step(returning=True)
        self.assertTrue(returned.success, returned)
        self.assertTrue(returned.data["at_entry"])
        self.assertEqual(returned.data["landing_observation"]["termination"], "verified")
        self.assertEqual(len([c for c in self.world.writes() if c[0] == "move"]), 2)

    def test_excavates_from_soil_to_three_collected_cobbles_and_climbs_back(self):
        plan = self.plan(excluded_directions=["north", "south", "west"])
        self.assertTrue(plan.success, plan)
        self.assertEqual(plan.data["site"], {"pos": [0, 64, 0], "orientation": "east"})
        self.assertEqual(self.world.writes(), [])
        actions = []
        for _ in range(48):
            before = len(self.world.writes())
            current = tuple(self.world.status["pos"])
            result = self.step()
            self.assertTrue(result.success, result)
            self.assertEqual(len(self.world.writes()), before + 1)
            actions.append(result.data["operation"])
            if result.data["operation"] == "dig":
                self.assertEqual(result.data["gained_cobblestone"], 0)
                self.assertNotEqual((result.data["dug_pos"][0] + .5, result.data["dug_pos"][2] + .5),
                                    (current[0], current[2]))
            if self.world.counts.get("cobblestone", 0) >= 3:
                break
        self.assertGreaterEqual(self.world.counts.get("cobblestone", 0), 3)
        self.assertIn("dig", actions)
        self.assertIn("descend", actions)
        self.assertLess(self.world.status["pos"][1], 64)
        del self.world.counts["wooden_pickaxe"]  # A broken tool must not trap the miner underground.
        for _ in range(self.skills.MAX_DEPTH + 1):
            result = self.step(returning=True)
            self.assertTrue(result.success, result)
            self.assertEqual(result.data["operation"], "ascend")
            if result.data["at_entry"]:
                break
        self.assertEqual(self.world.status["pos"], [.5, 64, .5])

    def test_project_cannot_cut_house_or_pass_through_its_exit_support(self):
        result = self.plan(protected_pos=[0, 64, 3])
        self.assertFalse(result.success)
        result = self.step(protected_pos=[4, 64, 0])
        self.assertFalse(result.success)
        self.assertEqual(self.world.writes(), [])

    def test_site_selection_changes_direction_when_first_stair_would_disturb_furniture(self):
        self.world.block([1, 63, 1], "chest")
        result = self.plan()
        self.assertTrue(result.success, result)
        self.assertIn(result.data["direction"], ("west", "north"))
        self.assertEqual(self.world.writes(), [])

    def test_read_only_survey_rejects_future_aquifer_before_starting_the_shaft(self):
        self.world.block([3, 61, 1], "water")
        result = self.plan(excluded_directions=["north", "south", "west"])
        self.assertEqual(result.error_code, "no_staircase_site")
        self.assertEqual(self.world.writes(), [])

    def test_checks_liquid_falling_blocks_waterlogging_and_empty_landing_before_writing(self):
        for pos, name, props in (([2, 63, 0], "lava", {}), ([1, 64, 0], "sand", {}),
                                  ([1, 64, 0], "gravel", {}), ([1, 63, 1], "stone", {"waterlogged": True}),
                                  ([1, 62, 0], "air", {}), ([1, 63, -1], "unknown", {})):
            with self.subTest(pos=pos, name=name):
                self.world.blocks.clear()
                self.world.block(pos, name, properties=props)
                self.assertFalse(self.step().success)
                self.assertFalse(self.world.writes())

    def test_rechecks_each_target_and_rejects_lava_appearing_before_dig(self):
        original = self.world.get_block
        hits = 0
        def read(pos, **options):
            nonlocal hits
            if pos == [2, 63, 0]:
                hits += 1
                if hits == 2:
                    self.world.block(pos, "water")
            return original(pos, **options)
        self.world.get_block = read
        result = self.step()
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "staircase_blocked")
        self.assertEqual(self.world.writes(), [])

    def test_dig_rejects_overhead_water_through_air_but_accepts_a_solid_barrier(self):
        self.world.block([1, 68, 0], "water")
        blocked = self.step()
        self.assertEqual(blocked.error_code, "staircase_blocked")
        self.assertEqual(blocked.data["blocked_cell"], [1, 68, 0])
        self.assertEqual(self.world.writes(), [])

        self.world.block([1, 67, 0], "stone")
        safe = self.step()
        self.assertTrue(safe.success, safe)
        self.assertEqual(self.world.writes(), [("dig", [1, 63, 0])])

    def test_hostile_broken_pick_and_source_footprint_prevent_writes(self):
        self.world.entities = [{"name": "skeleton", "type": "hostile", "id": 3, "pos": [16, 64, 0]}]
        self.assertFalse(self.step().success)
        self.world.entities = []
        self.world.status["pos"] = [.92, 64, .5]
        self.assertFalse(self.step().success)
        self.world.status["pos"] = [.5, 64, .5]
        self.world.inventory = lambda **kw: envelope({"items": [{"name": "wooden_pickaxe", "count": 1, "durability": [0, 59]}]})
        self.assertEqual(self.plan().error_code, "missing_tool")
        self.assertFalse(self.world.writes())

    def test_no_mining_from_wrong_stance_or_outside_depth_limit(self):
        self.assertFalse(self.step(entry=[1, 64, 0]).success)
        self.assertFalse(self.step(direction="below").success)
        self.assertFalse(self.step(entry=[-9, 73, 0]).success)
        self.assertFalse(self.world.writes())

    def test_unconfirmed_dig_never_retries_and_requires_independent_gain(self):
        original = self.world.dig
        def uncertain(pos, collect):
            original(pos, collect)
            raise APIError("dig_unconfirmed", "Server acknowledgement missing")
        self.world.dig = uncertain
        result = self.step()
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual(self.world.writes(), [("dig", [1, 63, 0])])

    def test_uncertain_acknowledgement_reconciled_once_when_block_and_inventory_confirm(self):
        original = self.world.dig
        def uncertain(pos, collect):
            original(pos, collect)
            self.world.counts["dirt"] = 1
            raise APIError("dig_unconfirmed", "Missing acknowledgement")
        self.world.dig = uncertain
        result = self.step()
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["block_removed"])
        self.assertEqual(result.data["observed_drop_gain"], 1)
        self.assertEqual(result.data["gained_cobblestone"], 0)
        self.assertEqual(self.world.writes(), [("dig", [1, 63, 0])])

    def test_cancel_and_changed_life_after_dig_stay_unknown_without_writes(self):
        for change in ("cancel", "death"):
            with self.subTest(change=change):
                self.setUp()
                token = threading.Event()
                original = self.world.dig
                def interrupted(pos, collect):
                    result = original(pos, collect)
                    if change == "cancel":
                        token.set()
                    else:
                        self.world.status["last_death"] = {"t": 123}
                    return result
                self.world.dig = interrupted
                result = self.skills.execute("step_starter_staircase", self.args, token)
                self.assertFalse(result.success)
                self.assertEqual(result.error_code, "action_unconfirmed")
                self.assertEqual(result.data["action_state"], "unknown")
                self.assertEqual(self.world.writes(), [("dig", [1, 63, 0])])
