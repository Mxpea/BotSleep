"""Pig/cow/sheep/chicken hunts never use the backend's chase-and-pickup macro."""

import copy
import math
import threading
import unittest
from unittest.mock import patch

from core.client import APIError
from core.survival_harness import _uncertain_result
from skills.daily_survival import DailySurvivalSkills
from test_daily_skills import DailyWorld, envelope


class HuntingTests(unittest.TestCase):
    def setUp(self):
        self.world = DailyWorld()
        self.world.counts = {"stone_sword": 1}
        self.world.entities = [{"id": 3, "name": "cow", "type": "animal", "pos": [2.5, 64, .5], "health": 10}]
        self.skills = DailySurvivalSkills(self.world)
        self.rest = patch("skills.hunting._rest", return_value=None)
        self.rest.start()
        self.addCleanup(self.rest.stop)

    def act(self, name="cow", cancel=None):
        return self.skills.execute("hunt_animal", {"entity_id": 3, "expected_name": name}, cancel)

    def test_all_four_animals_accept_actual_raw_or_cooked_drops(self):
        for name, raw, cooked in (("cow", "beef", "cooked_beef"), ("pig", "porkchop", "cooked_porkchop"),
                                  ("sheep", "mutton", "cooked_mutton"), ("chicken", "chicken", "cooked_chicken")):
            for drop in (raw, cooked):
                with self.subTest(name=name, drop=drop):
                    self.world.entities = [{"id": 3, "name": name, "type": "animal", "pos": [2.5, 64, .5]}]
                    def effect():
                        self.world.entities = []
                        self.world.counts[drop] = self.world.counts.get(drop, 0) + 1
                    self.world.effects["attack"] = effect
                    result = self.act(name)
                    self.assertTrue(result.success, result)
                    self.assertEqual(result.data["gained"], {drop: 1})
                    self.assertFalse(result.data["automatic_chase"])
                    self.assertFalse(any(c[0] in {"attack", "move"} for c in self.world.calls))

    def test_fleeing_animal_requires_a_new_stance_without_chase(self):
        def effect():
            self.world.entities[0]["pos"] = [6, 64, .5]
        self.world.effects["attack"] = effect
        result = self.act()
        self.assertEqual(result.error_code, "out_of_range")
        self.assertEqual(sum(c[0] == "control" for c in self.world.calls), 1)
        self.assertFalse(any(c[0] in {"attack", "move"} for c in self.world.calls))

    def test_no_drop_despawn_is_not_kill_or_harvest(self):
        self.world.effects["attack"] = lambda: self.world.entities.clear()
        result = self.act()
        self.assertEqual(result.error_code, "hunt_unverified")
        self.assertTrue(result.data["target_missing"])
        self.assertFalse(result.data["killed_verified"])

    def test_players_pets_nearby_animals_and_items_prevent_sweep_or_wrong_target(self):
        for name, kind in (("Player", "player"), ("wolf", "animal"), ("sheep", "animal"), ("item", "other")):
            with self.subTest(name=name):
                self.world.entities = self.world.entities[:1] + [{"id": 9, "name": name, "type": kind, "pos": [2.4, 64, .6]}]
                result = self.act()
                self.assertEqual(result.error_code, "unsafe_target")
                self.assertFalse(any(c[0] == "control" for c in self.world.calls))

    def test_wall_and_missing_floor_prevent_attack(self):
        for pos in ([1, 65, 0], [0, 63, 0]):
            with self.subTest(pos=pos):
                self.world.blocks.clear()
                self.world.block(pos, "air" if pos[1] == 63 else "stone")
                self.assertFalse(self.act().success)
                self.assertFalse(any(c[0] == "control" for c in self.world.calls))

    def test_damage_stops_further_swings(self):
        self.world.effects["attack"] = lambda: self.world.status.update(health=19)
        result = self.act()
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["verification_error"], "unsafe")
        self.assertEqual(sum(c[0] == "control" for c in self.world.calls), 1)

    def test_wrong_ray_target_is_not_success(self):
        self.world.control = lambda **kw: envelope({"hits": [{"entity": {"id": 99}}]})
        self.assertEqual(self.act().error_code, "action_unconfirmed")

    def test_cancel_after_real_hit_keeps_unknown_without_another_action(self):
        event = threading.Event()
        self.world.effects["attack"] = event.set
        result = self.act(cancel=event)
        self.assertTrue(_uncertain_result(result))
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertFalse(any(c[0] == "stop" for c in self.world.calls))
        self.assertEqual(sum(c[0] == "control" for c in self.world.calls), 1)

    def test_write_disconnect_is_unknown_without_more_swings_or_stop(self):
        def fail():
            raise APIError("not_connected", "worker ended")
        self.world.effects["attack"] = fail
        result = self.act()
        self.assertTrue(_uncertain_result(result))
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(sum(c[0] == "control" for c in self.world.calls), 1)
        self.assertFalse(any(c[0] == "stop" for c in self.world.calls))

    def test_cancelled_before_hunt_has_no_writes(self):
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(self.act(cancel=cancel).error_code, "cancelled")
        self.assertFalse(any(c[0] in {"equip", "control", "attack", "move"} for c in self.world.calls))

    def test_fresh_changed_identity_never_attacked(self):
        self.world.entities[0]["name"] = "pig"
        self.assertEqual(self.act().error_code, "target_changed")
        self.assertFalse(any(c[0] == "control" for c in self.world.calls))

    def test_final_validation_side_step_updates_swing_yaw_and_pitch(self):
        original = self.world.get_entities
        target_scans = [0]
        def moving_target(**kwargs):
            if kwargs.get("radius") == 16:
                target_scans[0] += 1
                if target_scans[0] == 4:
                    self.world.entities[0]["pos"] = [2.5, 64, 1.5]
            return original(**kwargs)
        self.world.get_entities = moving_target
        result = self.act()
        self.assertTrue(result.success, result)
        self.assertEqual(target_scans[0], 4)
        writes = [c[1] for c in self.world.calls if c[0] == "control"]
        self.assertEqual(len(writes), 1)
        self.assertAlmostEqual(writes[0]["yaw"], math.atan2(-2, -1))
        self.assertAlmostEqual(writes[0]["pitch"], math.atan2(-.72, math.sqrt(5)))
        self.assertNotAlmostEqual(writes[0]["yaw"], -math.pi / 2)

    def test_final_validation_uses_latest_player_position_for_aim(self):
        original = self.world.get_status
        status_reads = [0]
        def shifted_status(**kwargs):
            status_reads[0] += 1
            if status_reads[0] == 5:
                self.world.status["pos"] = [.5, 64, .7]
            return original(**kwargs)
        self.world.get_status = shifted_status
        result = self.act()
        self.assertTrue(result.success, result)
        control = next(c[1] for c in self.world.calls if c[0] == "control")
        self.assertAlmostEqual(control["yaw"], math.atan2(-2, .2))
        self.assertAlmostEqual(control["pitch"], math.atan2(-.72, math.hypot(2, .2)))

    def test_latest_full_scan_updates_the_validated_ray_and_swing_together(self):
        original = self.world.get_entities
        scans = [0]
        def moving_target(**kwargs):
            if kwargs.get("radius") == 8:
                scans[0] += 1
                if scans[0] == 4:
                    self.world.entities[0]["pos"] = [2.5, 64, 1.5]
            return original(**kwargs)
        self.world.get_entities = moving_target
        result = self.act()
        self.assertTrue(result.success, result)
        control = next(c[1] for c in self.world.calls if c[0] == "control")
        self.assertAlmostEqual(control["yaw"], math.atan2(-2, -1))
        self.assertAlmostEqual(control["pitch"], math.atan2(-.72, math.sqrt(5)))

    def test_latest_scan_cannot_lose_change_duplicate_or_overcrowd_the_target(self):
        original = self.world.get_entities
        initial = copy.deepcopy(self.world.entities[0])
        cases = (([], "target_changed"),
                 ([{**initial, "name": "pig"}], "target_changed"),
                 ([{**initial, "type": "player"}], "target_changed"),
                 ([{**initial, "pos": [6, 64, .5]}], "out_of_range"),
                 ([initial, initial], "invalid_response"),
                 ([initial] + [{"id": i + 100, "name": "cow", "type": "animal", "pos": [7, 64, 7]}
                              for i in range(39)], "invalid_response"))
        for changed, code in cases:
            with self.subTest(code=code, target=changed[:1], size=len(changed)):
                self.world.entities = [copy.deepcopy(initial)]
                self.world.calls.clear()
                scans = [0]
                def changing_scan(**kwargs):
                    if kwargs.get("radius") == 8:
                        scans[0] += 1
                        if scans[0] == 4:
                            self.world.entities = copy.deepcopy(changed)
                    return original(**kwargs)
                self.world.get_entities = changing_scan
                result = self.act()
                self.assertEqual(scans[0], 4)
                self.assertEqual(result.error_code, code)
                self.assertFalse(any(c[0] == "control" for c in self.world.calls))

    def test_latest_target_position_requires_its_own_clear_ray(self):
        original = self.world.get_entities
        scans = [0]
        def side_step_behind_wall(**kwargs):
            if kwargs.get("radius") == 8:
                scans[0] += 1
                if scans[0] == 4:
                    self.world.entities[0]["pos"] = [2.5, 64, 1.5]
                    self.world.block([2, 65, 1], "stone")
            return original(**kwargs)
        self.world.get_entities = side_step_behind_wall
        result = self.act()
        self.assertEqual(result.error_code, "target_occluded")
        self.assertFalse(any(c[0] == "control" for c in self.world.calls))

    def test_all_hunt_io_receives_remaining_budget_and_short_timeout(self):
        clock = [100.0]
        requests = []
        for name in ("get_status", "inventory", "get_block", "get_entities", "equip", "control"):
            original = getattr(self.world, name)
            def record(*args, _name=name, _original=original, **kwargs):
                requests.append((_name, clock[0], dict(kwargs)))
                clock[0] += .01
                return _original(*args, **kwargs)
            setattr(self.world, name, record)
        with patch("skills.hunting.time.monotonic", side_effect=lambda: clock[0]):
            result = self.act()
        self.assertTrue(result.success, result)
        # The shared dispatcher has one ordinary readiness read before entering
        # this helper. Every IO inside the hunt uses its single eight-second clock.
        self.assertEqual(requests[0][0], "get_status")
        deadline = requests[1][1] + 8
        for name, started, kwargs in requests[1:]:
            with self.subTest(name=name, started=started):
                self.assertGreater(kwargs["timeout_seconds"], 0)
                self.assertLessEqual(kwargs["timeout_seconds"], min(1.5, deadline - started))

    def test_slow_initial_inventory_does_not_equip_or_attack(self):
        clock = [0.0]
        original = self.world.inventory
        def slow_inventory(**kwargs):
            result = original(**kwargs)
            clock[0] = 9
            return result
        self.world.inventory = slow_inventory
        with patch("skills.hunting.time.monotonic", side_effect=lambda: clock[0]):
            result = self.act()
        self.assertEqual(result.error_code, "operation_budget_exhausted")
        self.assertFalse(any(c[0] in {"equip", "control", "stop"} for c in self.world.calls))

    def test_late_final_entity_scan_cannot_dispatch_a_swing(self):
        clock = [0.0]
        scans = [0]
        original = self.world.get_entities
        def slow_last_scan(**kwargs):
            result = original(**kwargs)
            if kwargs.get("radius") == 8:
                scans[0] += 1
                if scans[0] == 4:
                    clock[0] = 9
            return result
        self.world.get_entities = slow_last_scan
        with patch("skills.hunting.time.monotonic", side_effect=lambda: clock[0]):
            result = self.act()
        self.assertEqual(scans[0], 4)
        self.assertEqual(result.error_code, "operation_budget_exhausted")
        self.assertEqual(sum(c[0] == "equip" for c in self.world.calls), 1)
        self.assertFalse(any(c[0] in {"control", "stop"} for c in self.world.calls))

    def test_deadline_between_final_validation_and_dispatch_never_attacks(self):
        clock = [0.0]
        original = self.skills._daily_action
        def delayed_dispatch(context, operation, *args, validate=None, **kwargs):
            def guard(status):
                validate(status)
                if operation.__name__ == "swing":
                    clock[0] = 9
            return original(context, operation, *args, validate=guard, **kwargs)
        self.skills._daily_action = delayed_dispatch
        with patch("skills.hunting.time.monotonic", side_effect=lambda: clock[0]):
            result = self.act()
        self.assertEqual(result.error_code, "operation_budget_exhausted")
        self.assertFalse(any(c[0] in {"control", "stop"} for c in self.world.calls))

    def test_slow_post_hit_inventory_is_unknown_even_if_food_was_gained(self):
        clock = [0.0]
        original = self.world.inventory
        def slow_verification(**kwargs):
            result = original(**kwargs)
            if any(c[0] == "control" for c in self.world.calls):
                clock[0] = 9
            return result
        self.world.inventory = slow_verification
        with patch("skills.hunting.time.monotonic", side_effect=lambda: clock[0]):
            result = self.act()
        self.assertEqual(self.world.counts["beef"], 1)
        self.assertTrue(_uncertain_result(result))
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["verification_error"], "operation_budget_exhausted")
        self.assertEqual(sum(c[0] == "control" for c in self.world.calls), 1)
        self.assertFalse(any(c[0] == "stop" for c in self.world.calls))

    def test_remaining_budget_cannot_shorten_the_weapon_cooldown(self):
        clock = [0.0]
        original = self.world.equip
        def slow_equip(*args, **kwargs):
            result = original(*args, **kwargs)
            clock[0] = 7.8
            return result
        self.world.equip = slow_equip
        with patch("skills.hunting.time.monotonic", side_effect=lambda: clock[0]):
            result = self.act()
        self.assertEqual(result.error_code, "operation_budget_exhausted")
        self.assertFalse(any(c[0] in {"control", "stop"} for c in self.world.calls))

    def test_equipment_timeout_remains_unknown_and_does_not_swing_or_stop(self):
        def uncertain_equip(*args, **kwargs):
            self.world.calls.append(("equip", args, kwargs))
            raise APIError("request_timeout", "equip response lost")
        self.world.equip = uncertain_equip
        result = self.act()
        self.assertTrue(_uncertain_result(result))
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["verification_error"], "request_timeout")
        self.assertFalse(any(c[0] in {"control", "stop"} for c in self.world.calls))
