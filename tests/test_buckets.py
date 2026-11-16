"""Bucket source changes, containment and unknown-action recovery, all offline."""

import threading
import unittest

from core.client import APIError
from core.block_geometry import _ray_cells
from core.survival_harness import _uncertain_result
from skills.buckets import BucketSkills
from test_daily_skills import DailyWorld, envelope


class LiquidWorld(DailyWorld):
    def __init__(self):
        super().__init__()
        # Stand at the inner edge of a full stone wall, not over the pit. The
        # official ray to the bottom support clears the wall only at this edge.
        self.status.update(pos=[1.1, 64, .5], is_day=True, thundering=False, time_of_day=1000)
        self.source = [0, 62, 0]
        self.block(self.source, "air")
        self.block([0, 63, 0], "air")
        self.entities = [{"id": 3, "name": "pig", "type": "animal", "pos": [.5, 62, .5]}]
        self.counts = {"lava_bucket": 1}
        self.after_write = None
        self.food_drop = None

    def interact(self, pos=None, item=None, **kwargs):
        self.calls.append(("interact", pos, item))
        if item == "lava_bucket":
            # Official session.js useBucket selects below + top face for an
            # empty target. Do not let a fake teleport liquid through a wall.
            eye = [self.status["pos"][0], self.status["pos"][1] + 1.62, self.status["pos"][2]]
            reference = (pos[0], pos[1] - 1, pos[2])
            aim = [pos[0] + .5, pos[1] - .01, pos[2] + .5]
            for cell in dict.fromkeys(_ray_cells(eye, aim)):
                if cell == reference:
                    break
                if self.get_block(list(cell))["result"]["name"] != "air":
                    raise AssertionError("Official placement ray hits a different block")
            self.counts["lava_bucket"] -= 1
            self.counts["bucket"] = self.counts.get("bucket", 0) + 1
            self.block(pos, "lava", properties={"level": 0})
        elif item == "bucket":
            liquid = self.blocks[tuple(pos)]["name"]
            self.counts["bucket"] -= 1
            self.counts[liquid + "_bucket"] = self.counts.get(liquid + "_bucket", 0) + 1
            self.block(pos, "air")
            if self.food_drop:
                self.counts[self.food_drop] = self.counts.get(self.food_drop, 0) + 1
        if self.after_write:
            self.after_write(item)
        return envelope({"target": list(pos), "changed": True})


class BucketTests(unittest.TestCase):
    def setUp(self):
        self.world = LiquidWorld()
        self.skills = BucketSkills(self.world)

    def hunt(self, event=None):
        return self.skills.execute("hunt_with_lava", {"entity_id": 3, "expected_name": "pig"}, event)

    def writes(self):
        return [row for row in self.world.calls if row[0] == "interact"]

    def test_lava_round_trip_and_actual_cooked_food_gain(self):
        self.world.food_drop = "cooked_porkchop"
        result = self.hunt()
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["gained"], {"cooked_porkchop": 1})
        self.assertTrue(result.data["source_recovered"])
        self.assertEqual([r[2] for r in self.writes()], ["lava_bucket", "bucket"])
        self.assertEqual(self.world.counts["lava_bucket"], 1)
        self.assertEqual(self.world.blocks[tuple(self.world.source)]["name"], "air")
        self.assertFalse(any(c[0] in {"move", "attack"} for c in self.world.calls))
        self.assertEqual(result.data["placement_reference"], [0, 61, 0])
        self.assertEqual(result.data["placement_face"], [0, 1, 0])
        self.assertEqual(result.data["placement_aim"], [.5, 61.99, .5])

    def test_pickup_visible_but_real_placement_ray_hits_pit_wall(self):
        self.world.status["pos"] = [1.5, 64, .5]
        result = self.hunt()
        self.assertEqual(result.error_code, "target_occluded", result)
        self.assertEqual(result.data["blocker"], [1, 63, 0])
        self.assertEqual(result.data["bucket_item"], "lava_bucket")
        self.assertFalse(result.data["executed"])
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.world.counts, {"lava_bucket": 1})

    def test_pickup_keeps_official_higher_liquid_surface_aim(self):
        self.world.status["pos"] = [1.5, 64, .5]
        self.world.counts = {"bucket": 1}
        self.world.block(self.world.source, "lava", properties={"level": 0})
        result = self.skills.execute("collect_liquid", {"pos": self.world.source, "expected_name": "lava"})
        self.assertTrue(result.success, result)
        self.assertEqual(len(self.writes()), 1)

    def test_placement_reference_must_remain_known_full_block(self):
        self.world.block([0, 61, 0], "stone_slab")
        result = self.hunt()
        self.assertEqual(result.error_code, "unsafe_lava_site", result)
        self.assertFalse(self.writes())

    def test_ignition_without_food_is_not_a_kill_and_does_not_immediately_repeat(self):
        result = self.hunt()
        self.assertEqual(result.error_code, "hunt_unverified", result)
        self.assertTrue(result.data["source_recovered"])
        self.assertFalse(result.data["killed_verified"])
        self.assertFalse(result.data["completed"])
        self.assertEqual(self.hunt().error_code, "hunt_cooldown")
        self.assertEqual(len(self.writes()), 2)

    def test_vegetation_buildings_unknown_blocks_and_water_reject_before_lava(self):
        for name in ("oak_planks", "oak_leaves", "short_grass", "water", "furnace", "unknown_mod_block"):
            with self.subTest(name=name):
                self.setUp()
                self.world.block([3, 65, 2], name)
                self.assertEqual(self.hunt().error_code, "unsafe_lava_site")
                self.assertFalse(self.writes())

    def test_open_ground_and_missing_containment_wall_are_not_lava_sites(self):
        self.world.block([1, 62, 0], "air")
        self.assertEqual(self.hunt().error_code, "unsafe_lava_site")
        self.assertFalse(self.writes())

    def test_other_animals_players_or_drops_exclude_lava(self):
        for name, kind in (("Player", "player"), ("cow", "animal"), ("item", "other")):
            with self.subTest(name=name):
                self.world.entities = self.world.entities[:1] + [{"id": 7, "name": name, "type": kind, "pos": [3, 64, 1]}]
                self.assertEqual(self.hunt().error_code, "unsafe_target")
                self.assertFalse(self.writes())

    def test_unknown_placement_never_sends_pickup_or_cleanup(self):
        for code in ("request_timeout", "not_connected", "invalid_json"):
            with self.subTest(code=code):
                self.setUp()
                def fail(item):
                    raise APIError(code, "result unavailable")
                self.world.after_write = fail
                result = self.hunt()
                self.assertTrue(_uncertain_result(result))
                self.assertTrue(result.data["lava_outstanding"])
                self.assertTrue(result.data["recovery_required"])
                self.assertEqual(len(self.writes()), 1)
                self.assertFalse(any(c[0] == "stop" for c in self.world.calls))

    def test_unknown_pickup_preserves_pending_source_diagnostic_without_third_write(self):
        def fail(item):
            if item == "bucket":
                raise APIError("request_timeout", "pickup result unavailable")
        self.world.after_write = fail
        result = self.hunt()
        self.assertTrue(_uncertain_result(result))
        self.assertTrue(result.data["recovery_required"])
        self.assertEqual(len(self.writes()), 2)

    def test_cancel_after_known_placement_recovers_exact_source_then_stops(self):
        event = threading.Event()
        self.world.after_write = lambda item: event.set() if item == "lava_bucket" else None
        result = self.hunt(event)
        self.assertEqual(result.error_code, "cancelled", result)
        self.assertTrue(result.data["source_recovered"])
        self.assertFalse(result.data["lava_outstanding"])
        self.assertEqual(len(self.writes()), 2)

    def test_changed_placement_location_never_guesses_a_pickup(self):
        def change(item):
            if item == "lava_bucket":
                self.world.block(self.world.source, "air")
        self.world.after_write = change
        result = self.hunt()
        self.assertTrue(_uncertain_result(result))
        self.assertEqual(len(self.writes()), 1)

    def test_collect_water_and_lava_sources_require_real_bucket_delta(self):
        for name in ("water", "lava"):
            with self.subTest(name=name):
                self.setUp()
                self.world.counts = {"bucket": 1}
                self.world.block(self.world.source, name, properties={"level": 0})
                result = self.skills.execute("collect_liquid", {"pos": self.world.source, "expected_name": name})
                self.assertTrue(result.success, result)
                self.assertEqual(result.data["gained"], {name + "_bucket": 1})
                self.assertEqual(len(self.writes()), 1)

    def test_flowing_or_unknown_source_level_is_refused(self):
        for props in ({}, {"level": 1}, {"level": 8}):
            with self.subTest(props=props):
                self.world.counts = {"bucket": 1}
                self.world.block(self.world.source, "lava", properties=props)
                result = self.skills.execute("collect_liquid", {"pos": self.world.source, "expected_name": "lava"})
                self.assertEqual(result.error_code, "not_liquid_source")
                self.assertFalse(self.writes())

    def test_acknowledgement_without_inventory_change_is_unknown(self):
        self.world.counts = {"bucket": 1}
        self.world.block(self.world.source, "water", properties={"level": 0})
        self.world.interact = lambda **kw: envelope({"target": self.world.source, "changed": True})
        result = self.skills.execute("collect_liquid", {"pos": self.world.source, "expected_name": "water"})
        self.assertTrue(_uncertain_result(result))

    def test_pickup_cancel_death_or_life_change_before_verification_is_unknown(self):
        for problem in ("cancel", "dead", "new_life"):
            with self.subTest(problem=problem):
                self.setUp()
                self.world.counts = {"bucket": 1}
                self.world.block(self.world.source, "water", properties={"level": 0})
                event = threading.Event()
                def change(item):
                    if problem == "cancel": event.set()
                    elif problem == "dead": self.world.status.update(alive=False, health=0)
                    else: self.world.status.update(last_death={"t": 1})
                self.world.after_write = change
                result = self.skills.execute("collect_liquid", {"pos": self.world.source, "expected_name": "water"}, event)
                self.assertEqual(result.error_code, "action_unconfirmed", result)
                self.assertTrue(_uncertain_result(result))
                self.assertEqual(result.data["cleanup_skipped"], "unknown_bucket")
                self.assertEqual(len(self.writes()), 1)

    def test_full_inventory_and_foreign_targets_do_not_write(self):
        original = self.world.inventory
        def full(**kw):
            response = original(**kw)
            response["result"]["empty_slots"] = 0
            return response
        self.world.inventory = full
        self.assertEqual(self.hunt().error_code, "inventory_full")
        self.assertFalse(self.writes())
        result = self.skills.execute("hunt_with_lava", {"entity_id": 3, "expected_name": "wolf"})
        self.assertEqual(result.error_code, "unsafe_target")

    def test_night_or_low_health_no_ignition(self):
        for changes in ({"is_day": False}, {"health": 15}, {"food": 9}, {"thundering": True}):
            with self.subTest(changes=changes):
                self.setUp()
                self.world.status.update(changes)
                self.assertEqual(self.hunt().error_code, "unsafe")
                self.assertFalse(self.writes())
