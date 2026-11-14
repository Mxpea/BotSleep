"""Survival skill state verification and wrapper tests; never contact a game."""

import copy
import json
import math
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.block_geometry import trace_block_visibility
from core.client import APIError, MinecraftAgentClient
from skills.survival import SurvivalSkills


def envelope(result):
    return {"ok": True, "result": copy.deepcopy(result)}


class World:
    def __init__(self):
        self.status = {"ready": True, "alive": True, "health": 20, "food": 10, "dimension": "overworld",
                       "pos": [0.5, 64, 0.5], "busy": None, "on_ground": True, "is_sleeping": False}
        self.counts = {}
        self.blocks = {}
        self.entities = []
        self.calls = []
        self.ground = "stone"
        self.move_effect = None
        self.dig_effect = None
        self.craft_effect = None
        self.place_effect = None
        self.eat_effect = None

    def get_status(self, *, timeout_seconds=None):
        self.calls.append(("status",))
        return envelope(self.status)

    def inventory(self, *, timeout_seconds=None):
        self.calls.append(("inventory",))
        return envelope({"items": [{"name": name, "count": count, "durability": [20, 100]}
                                   for name, count in self.counts.items() if count > 0]})

    def get_block(self, pos, *, timeout_seconds=None):
        key = tuple(math.floor(value) for value in pos)
        self.calls.append(("get_block", key))
        block = self.blocks.get(key, {"name": self.ground if key[1] < 64 else "air"})
        return envelope({"pos": list(key), "diggable": True, "properties": {}, **block})

    def set_block(self, pos, name, **extra):
        self.blocks[tuple(pos)] = {"name": name, **extra}

    def get_entities(self, radius=16, types=None, *, timeout_seconds=None):
        self.calls.append(("get_entities", radius, types))
        return copy.deepcopy(self.entities)

    def move_to(self, x, y, z, **kwargs):
        self.calls.append(("move_to", [x, y, z], kwargs))
        if self.move_effect:
            return self.move_effect([x, y, z], kwargs)
        self.status["pos"] = [x, y, z]
        return envelope({"pos": self.status["pos"]})

    def dig(self, pos, collect=True):
        self.calls.append(("dig", pos, collect))
        if self.dig_effect:
            return self.dig_effect(pos)
        name = self.blocks[tuple(pos)]["name"]
        drop = {"stone": "cobblestone", "coal_ore": "coal", "deepslate_coal_ore": "coal"}.get(name, name)
        self.set_block(pos, "air")
        self.counts[drop] = self.counts.get(drop, 0) + 1
        return envelope({"dug": name, "gained": {drop: 1}})

    def craft(self, item, count=1):
        self.calls.append(("craft", item, count))
        if self.craft_effect:
            return self.craft_effect(item, count)
        self.counts[item] = self.counts.get(item, 0) + count
        return envelope({"crafted": item, "gained": count})

    def place_block(self, x, y, z, item=None):
        self.calls.append(("place", item, [x, y, z]))
        if self.place_effect:
            return self.place_effect(item, [x, y, z])
        self.set_block([x, y, z], item)
        self.counts[item] -= 1
        return envelope({"placed": item})

    def use_item(self, item):
        self.calls.append(("use_item", item))
        if self.eat_effect:
            return self.eat_effect(item)
        self.counts[item] -= 1
        self.status["food"] = min(20, self.status["food"] + 5)
        return envelope({"used": {"name": item, "count": 1}})

    def stop(self, *, timeout_seconds=None):
        self.calls.append(("stop",))
        return envelope({"stopped": True})

    def writes(self):
        return [call for call in self.calls if call[0] in {"move_to", "dig", "craft", "place", "use_item", "stop"}]


class DigRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.skills = SurvivalSkills(self.world)
        self.target = [2, 64, 0]
        self.world.set_block(self.target, "oak_log", harvest_tool_ok=True, can_see=True)
        self.diagnostics = {"pos": [0.5, 64, 0.5], "eye_dist": 2.3, "on_ground": True, "ping_ms": 80}
        self.world.dig_effect = self.unconfirmed
        self.now = 100.0
        clock_patch = patch("skills.survival.time.monotonic", side_effect=lambda: self.now)
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)
        sleep_patch = patch("skills.survival.time.sleep", side_effect=self.advance)
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def advance(self, seconds):
        self.now += seconds

    def unconfirmed(self, pos):
        raise APIError("dig_unconfirmed", "Server did not confirm the dig", 200, self.diagnostics)

    def gather(self, cancel=None):
        return self.skills.execute("gather_block", {"pos": self.target, "expected_name": "oak_log"}, cancel)

    def assert_single_dig_and_stop(self):
        self.assertEqual([call[0] for call in self.world.writes()], ["dig", "stop"])

    def test_late_server_confirmation_and_inventory_gain_recover_success(self):
        def arrive(seconds):
            self.advance(seconds)
            self.world.set_block(self.target, "air")
            self.world.counts["oak_log"] = 1

        self.sleep.side_effect = arrive
        result = self.gather()
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["recovered_from"], "dig_unconfirmed")
        self.assertEqual(result.data["api_details"], self.diagnostics)
        self.assertEqual(result.data["gained"], {"oak_log": 1})
        self.assertEqual(result.data["recovery_attempts"], 2)
        self.assertEqual(result.data["action_state"], "verified")
        self.assert_single_dig_and_stop()
        stop_index = self.world.calls.index(("stop",))
        self.assertEqual(self.world.calls[stop_index + 1][0], "status")

    def test_inventory_may_arrive_on_last_observation_after_early_removal(self):
        def dig(pos):
            self.world.set_block(pos, "air")
            self.unconfirmed(pos)

        def arrive(seconds):
            self.advance(seconds)
            if self.sleep.call_count == 2:
                self.world.counts["oak_log"] = 1

        self.world.dig_effect = dig
        self.sleep.side_effect = arrive
        result = self.gather()
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["recovery_attempts"], 3)
        self.assert_single_dig_and_stop()

    def test_restored_original_block_remains_unknown_even_with_inventory_gain(self):
        def dig(pos):
            self.world.counts["oak_log"] = 1
            self.unconfirmed(pos)

        self.world.dig_effect = dig
        result = self.gather()
        self.assertEqual(result.error_code, "dig_unconfirmed")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertIsNone(result.data["block_removed"])
        self.assertEqual(result.data["recovery_error"], "unconfirmed_block_state")
        self.assertEqual(result.data["recovery_attempts"], 3)
        self.assertEqual(result.data["api_details"], self.diagnostics)
        self.assertEqual(self.sleep.call_count, 2)
        self.assert_single_dig_and_stop()

    def test_confirmed_removal_without_pickup_reports_drops_after_three_rounds(self):
        def dig(pos):
            self.world.set_block(pos, "air")
            self.unconfirmed(pos)

        self.world.dig_effect = dig
        self.world.entities = [{"id": 5, "name": "item", "type": "object", "item": "oak_logx1", "pos": [2.5, 64.2, 0.5]}]
        self.world.get_entities = Mock(wraps=self.world.get_entities)
        result = self.gather()
        self.assertEqual(result.error_code, "gather_unverified")
        self.assertTrue(result.data["block_removed"])
        self.assertEqual(result.data["action_state"], "block_removed")
        self.assertEqual(result.data["recovery_attempts"], 3)
        self.assertEqual(result.data["visible_drops"][0], {"id": 5, "name": "oak_log", "count": 1, "pos": [2.5, 64.2, 0.5]})
        self.assertIn("do not dig", result.data["recovery_hint"])
        self.assertGreater(self.world.get_entities.call_args.kwargs["timeout_seconds"], 0)
        self.assertLessEqual(self.world.get_entities.call_args.kwargs["timeout_seconds"], 1.21)
        self.assert_single_dig_and_stop()

    def test_failed_essential_recovery_read_preserves_original_error_and_phase(self):
        for method, phase in (("get_status", "status"), ("inventory", "inventory"), ("get_block", "block")):
            with self.subTest(method=method):
                world = World()
                world.set_block(self.target, "oak_log", can_see=True)
                world.dig_effect = self.unconfirmed
                original = getattr(world, method)

                def fail_after_stop(*args, **kwargs):
                    if ("stop",) in world.calls:
                        raise APIError("network_error", "Recovery secret must not leak")
                    return original(*args, **kwargs)

                setattr(world, method, fail_after_stop)
                result = SurvivalSkills(world).execute("gather_block", {"pos": self.target, "expected_name": "oak_log"})
                self.assertEqual(result.error_code, "dig_unconfirmed")
                self.assertEqual(result.data["action_state"], "unknown")
                self.assertEqual(result.data["recovery_stage"], phase)
                self.assertEqual(result.data["recovery_error"], "network_error")
                self.assertEqual(result.data["api_details"], self.diagnostics)
                self.assertNotIn("Recovery secret", str(result))
                self.assertEqual([call[0] for call in world.writes()], ["dig", "stop"])

    def test_stop_failure_does_not_start_observations_or_retry_stop_or_dig(self):
        def stop(**kwargs):
            self.world.calls.append(("stop",))
            raise APIError("request_timeout", "Stop result unknown")

        self.world.stop = stop
        result = self.gather()
        self.assertEqual(result.error_code, "dig_unconfirmed")
        self.assertEqual(result.data["recovery_stage"], "stop")
        self.assertEqual(result.data["cleanup_error"], "request_timeout")
        self.assertEqual(result.data["recovery_attempts"], 0)
        self.assertEqual(self.world.calls[-1], ("stop",))
        self.assert_single_dig_and_stop()

    def test_stop_without_connected_bot_is_not_confirmation(self):
        def stop(**kwargs):
            self.world.calls.append(("stop",))
            return envelope({"stopped": False})

        self.world.stop = stop
        result = self.gather()
        self.assertEqual(result.error_code, "dig_unconfirmed")
        self.assertEqual(result.data["recovery_error"], "stop_unverified")
        self.assert_single_dig_and_stop()

    def test_cancellation_received_with_error_still_stops_and_keeps_unknown(self):
        cancel = threading.Event()

        def dig(pos):
            cancel.set()
            self.unconfirmed(pos)

        self.world.dig_effect = dig
        result = self.gather(cancel)
        self.assertEqual(result.error_code, "dig_unconfirmed")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual(result.data["recovery_error"], "cancelled")
        self.assertEqual(result.data["cleanup"], "stop_requested")
        self.assertNotIn("cleanup_error", result.data)
        self.assertEqual(self.world.calls[-1], ("stop",))
        self.assert_single_dig_and_stop()

    def test_cancellation_interrupts_recovery_wait_without_another_observation(self):
        cancel = threading.Event()
        with patch.object(cancel, "wait", side_effect=lambda _: cancel.set()) as wait:
            result = self.gather(cancel)
        self.assertEqual(result.error_code, "dig_unconfirmed")
        self.assertEqual(result.data["recovery_stage"], "wait")
        self.assertEqual(result.data["recovery_error"], "cancelled")
        self.assertEqual(result.data["recovery_attempts"], 1)
        wait.assert_called_once()
        self.assert_single_dig_and_stop()

    def test_cancellation_during_result_read_preserves_unknown_despite_apparent_success(self):
        cancel = threading.Event()
        original = self.world.get_block

        def dig(pos):
            self.world.set_block(pos, "air")
            self.world.counts["oak_log"] = 1
            self.unconfirmed(pos)

        def get_block(pos, **kwargs):
            response = original(pos, **kwargs)
            if ("stop",) in self.world.calls:
                cancel.set()
            return response

        self.world.dig_effect = dig
        self.world.get_block = get_block
        result = self.gather(cancel)
        self.assertEqual(result.error_code, "dig_unconfirmed")
        self.assertEqual(result.data["recovery_stage"], "block")
        self.assertEqual(result.data["recovery_error"], "cancelled")
        self.assert_single_dig_and_stop()

    def test_remaining_budget_limits_every_recovery_request_and_stops_new_reads(self):
        budgets = []
        for method in ("stop", "get_status", "inventory", "get_block"):
            original = getattr(self.world, method)

            def bounded(*args, _method=method, _original=original, **kwargs):
                if "timeout_seconds" in kwargs:
                    budget = kwargs["timeout_seconds"]
                    budgets.append(budget)
                    self.assertGreater(budget, 0)
                    self.assertLessEqual(budget, 102.0 - self.now + 1e-9)
                    if _method == "stop":
                        self.advance(1.7)
                return _original(*args, **kwargs)

            setattr(self.world, method, bounded)
        result = self.gather()
        self.assertEqual(result.error_code, "dig_unconfirmed")
        self.assertEqual(result.data["recovery_error"], "recovery_budget_exhausted")
        self.assertEqual(budgets[0], 2.0)
        self.assertTrue(all(budget <= 0.301 for budget in budgets[1:]))
        self.assertAlmostEqual(self.now, 102.0)
        self.assert_single_dig_and_stop()

    def test_death_during_recovery_cannot_turn_other_inventory_into_gather_success(self):
        def dig(pos):
            self.world.set_block(pos, "air")
            self.world.counts["oak_log"] = 1
            self.world.status["last_death"] = {"t": 123}
            self.unconfirmed(pos)

        self.world.dig_effect = dig
        result = self.gather()
        self.assertEqual(result.error_code, "dig_unconfirmed")
        self.assertEqual(result.data["recovery_error"], "death_changed")
        self.assert_single_dig_and_stop()

    def test_drop_diagnostics_are_skipped_when_final_observation_uses_budget(self):
        def dig(pos):
            self.world.set_block(pos, "air")
            self.unconfirmed(pos)

        original = self.world.get_status
        recovery_status_reads = 0

        def status(**kwargs):
            nonlocal recovery_status_reads
            response = original(**kwargs)
            if "timeout_seconds" in kwargs:
                recovery_status_reads += 1
                if recovery_status_reads == 6:
                    self.now = 102.0
            return response

        self.world.dig_effect = dig
        self.world.get_status = status
        self.world.get_entities = Mock(side_effect=AssertionError("No remaining query budget"))
        result = self.gather()
        self.assertEqual(result.error_code, "gather_unverified")
        self.assertTrue(result.data["block_removed"])
        self.assertEqual(result.data["drop_query_error"], "recovery_budget_exhausted")
        self.world.get_entities.assert_not_called()
        self.assert_single_dig_and_stop()


class TerrainRetreatTests(unittest.TestCase):
    @staticmethod
    def terrain(nodes):
        world = World()
        world.ground = "air"
        world.status["pos"] = [nodes[0][0] + 0.5, nodes[0][1], nodes[0][2] + 0.5]
        for x, y, z, support in nodes:
            world.set_block([x, y - 1, z], support, properties={"waterlogged": False})
        return world

    def stairs(self):
        return self.terrain([(0, 64, 0, "stone"), (1, 65, 0, "dirt"), (2, 66, 0, "dirt")])

    def canopy(self):
        return self.terrain([(0, 66, 0, "birch_leaves"), (1, 65, 0, "oak_leaves"), (2, 64, 0, "dirt")])

    @staticmethod
    def retreat(world):
        return SurvivalSkills(world).execute("retreat_from", {"pos": [-3, world.status["pos"][1], 0.5]})

    def test_one_block_stairs_use_one_target_move_with_bounded_candidate_path(self):
        world = self.stairs()
        result = self.retreat(world)
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["planned_path"], [[0.5, 64, 0.5], [1.5, 65, 0.5], [2.5, 66, 0.5]])
        self.assertEqual(result.data["target"], [1.5, 65, 0.5])
        self.assertEqual(result.data["final_target"], [2.5, 66, 0.5])
        self.assertEqual(result.data["evacuation_stage"], "relay")
        self.assertFalse(result.data["retreat_complete"])
        self.assertTrue(result.data["terrain_search"]["used"])
        self.assertFalse(result.data["path_following_verified"])
        self.assertEqual([call[0] for call in world.writes()], ["move_to"])
        self.assertEqual(world.writes()[0][2]["range"], 0)
        self.assertLessEqual(result.data["terrain_search"]["block_reads"], 256)
        self.assertEqual(result.data["terrain_search"]["block_reads"], len([call for call in world.calls if call[0] == "get_block"]))

    def test_canopy_path_can_use_verified_leaves_but_finishes_on_dirt(self):
        world = self.canopy()
        origin = copy.deepcopy(world.status["pos"])
        result = self.retreat(world)
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["target"], [1.5, 65, 0.5])
        self.assertEqual(result.data["final_target"], [2.5, 64, 0.5])
        self.assertEqual([point[1] for point in result.data["planned_path"]], [66, 65, 64])
        result = SurvivalSkills(world).execute("retreat_from", {"pos": [-3, 66, 0.5]},
                                              recovery_context={"evacuating": True, "evacuation_origin": origin})
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["retreat_complete"])
        self.assertEqual(result.data["evacuation_stage"], "ground")
        self.assertEqual(world.blocks[(2, 63, 0)]["name"], "dirt")
        self.assertNotIn("birch_leaves", SurvivalSkills.SUPPORT)
        self.assertNotIn("oak_leaves", SurvivalSkills.SUPPORT)
        self.assertEqual([call[0] for call in world.writes()], ["move_to", "move_to"])

    def test_leaf_support_requires_known_type_and_explicit_dry_state(self):
        for name, properties in (("birch_leaves", {}), ("birch_leaves", {"waterlogged": True}),
                                 ("birch_leaves", {"waterlogged": 0}),
                                 ("unknown_leaves", {"waterlogged": False})):
            with self.subTest(name=name, properties=properties):
                world = self.canopy()
                world.set_block([0, 65, 0], name, properties=properties)
                result = self.retreat(world)
                self.assertEqual(result.error_code, "unsafe")
                self.assertFalse(world.writes())

    def test_jump_source_ceiling_and_descent_swept_headroom_are_checked(self):
        for factory, obstacle in ((self.stairs, [0, 66, 0]), (self.canopy, [1, 67, 0])):
            with self.subTest(obstacle=obstacle):
                world = factory()
                world.set_block(obstacle, "stone")
                result = self.retreat(world)
                self.assertEqual(result.error_code, "unsafe")
                self.assertIn({"pos": obstacle, "name": "stone", "reason": "body_blocked"}, result.data["blocked_cells"])
                self.assertFalse(world.writes())

    def test_void_liquid_unknown_support_and_three_block_drop_do_not_form_paths(self):
        for support in ("air", "lava", "unknown_support"):
            with self.subTest(support=support):
                world = self.canopy()
                world.set_block([1, 64, 0], support)
                self.assertEqual(self.retreat(world).error_code, "unsafe")
                self.assertFalse(world.writes())
        world = self.terrain([(0, 67, 0, "birch_leaves"), (1, 64, 0, "dirt"), (2, 64, 0, "dirt")])
        self.assertEqual(self.retreat(world).error_code, "unsafe")
        self.assertFalse(world.writes())

    def test_leaf_fields_exhaust_node_or_read_budget_without_moving(self):
        for stepped in (False, True):
            with self.subTest(stepped=stepped):
                world = World()

                def read(pos, **kwargs):
                    x, y, z = pos
                    world.calls.append(("get_block", tuple(pos)))
                    height = 63 + ((x + z) % 2 if stepped else 0)
                    name = "air" if y > height else "birch_leaves" if y == height else "stone"
                    return envelope({"name": name, "properties": {"waterlogged": False}, "diggable": True})

                world.get_block = read
                result = self.retreat(world)
                self.assertEqual(result.error_code, "unsafe")
                stats = result.data["terrain_search"]
                self.assertIn(stats["termination"], {"node_budget", "read_budget"})
                self.assertLessEqual(stats["nodes"], 64)
                self.assertLessEqual(stats["block_reads"], 248)
                self.assertEqual(stats["block_reads"], len([call for call in world.calls if call[0] == "get_block"]))
                if stepped:
                    self.assertEqual(stats["block_reads"], 248)
                self.assertLessEqual(len(result.data["blocked_cells"]), 4)
                self.assertFalse(world.writes())

    def test_position_change_across_cell_or_half_block_height_rejects_before_move(self):
        for updated in ([1.05, 64, 0.5], [0.5, 64.5, 0.5]):
            with self.subTest(updated=updated):
                world = self.stairs()
                original = world.get_status

                def status(**kwargs):
                    if any(call[0] == "get_block" for call in world.calls):
                        world.status["pos"] = updated
                    return original(**kwargs)

                world.get_status = status
                result = self.retreat(world)
                self.assertEqual(result.error_code, "unsafe")
                self.assertIn("position changed", result.message)
                self.assertFalse(world.writes())

    def test_last_action_status_rechecks_start_after_terrain_queries(self):
        for update in ({"pos": [0.5, 64, 1.5]}, {"pos": [0.5, 64.5, 0.5]}, {"on_ground": False}):
            with self.subTest(update=update):
                world = self.stairs()
                original = world.get_status
                reads = 0

                def status(**kwargs):
                    nonlocal reads
                    reads += 1
                    if reads == 4:  # _action's read, after destination rechecks.
                        world.status.update(update)
                    return original(**kwargs)

                world.get_status = status
                result = self.retreat(world)
                self.assertEqual(result.error_code, "unsafe")
                self.assertEqual(reads, 4)
                self.assertEqual(result.data["target"], [1.5, 65, 0.5])
                self.assertFalse(result.data["executed"])
                self.assertFalse(world.writes())

    def test_destination_is_reloaded_before_move_and_after_arrival(self):
        for after_move in (False, True):
            with self.subTest(after_move=after_move):
                world = self.stairs()
                if after_move:
                    def move(pos, kwargs):
                        world.status["pos"] = pos
                        world.set_block([1, 64, 0], "lava")
                        return envelope({"pos": pos})
                    world.move_effect = move
                else:
                    original = world.get_status
                    def status(**kwargs):
                        if any(call[0] == "get_block" for call in world.calls):
                            world.set_block([1, 64, 0], "lava")
                        return original(**kwargs)
                    world.get_status = status
                result = self.retreat(world)
                self.assertEqual(result.error_code, "retreat_unverified" if after_move else "unsafe")
                self.assertIn({"pos": [1, 64, 0], "name": "lava", "reason": "hazard"}, result.data["blocked_cells"])
                self.assertEqual([call[0] for call in world.writes()], ["move_to", "stop"] if after_move else [])

    def test_actual_arrival_must_match_the_step_even_when_backend_claims_the_goal(self):
        for actual in ([0.5, 64, 0.5], [2.0, 66, 0.5]):
            with self.subTest(actual=actual):
                world = self.stairs()

                def move(pos, kwargs):
                    world.status["pos"] = actual
                    return envelope({"pos": pos})

                world.move_effect = move
                result = self.retreat(world)
                self.assertEqual(result.error_code, "retreat_unverified")
                self.assertEqual([call[0] for call in world.writes()], ["move_to", "stop"])

    def test_path_requiring_a_step_towards_threat_is_rejected(self):
        paths = [
            [(0, 64, 0, "birch_leaves"), (-1, 64, 0, "birch_leaves"),
             (-1, 64, 1, "birch_leaves"), (-1, 64, 2, "birch_leaves"),
             (0, 64, 2, "birch_leaves"), (1, 64, 2, "dirt"), (2, 64, 2, "dirt")],
            # This route first retreats and climbs, then its western step gets
            # closer than its parent while still staying farther than origin.
            [(0, 64, 0, "birch_leaves"), (1, 65, 0, "birch_leaves"),
             (1, 66, 1, "birch_leaves"), (0, 66, 1, "birch_leaves"),
             (0, 66, 2, "birch_leaves"), (0, 66, 3, "birch_leaves"),
             (0, 66, 4, "birch_leaves"), (0, 66, 5, "dirt")],
        ]
        for nodes in paths:
            with self.subTest(nodes=nodes):
                world = self.terrain(nodes)
                result = self.retreat(world)
                self.assertEqual(result.error_code, "unsafe")
                self.assertFalse(world.writes())

    def test_two_block_descent_checks_the_entire_swept_head_column(self):
        for obstruction_y in (66, 67):
            with self.subTest(obstruction_y=obstruction_y):
                world = self.terrain([(0, 66, 0, "birch_leaves"), (1, 64, 0, "dirt"), (2, 64, 0, "dirt")])
                world.set_block([1, obstruction_y, 0], "stone")
                self.assertEqual(self.retreat(world).error_code, "unsafe")
                self.assertFalse(world.writes())

    def test_descent_cannot_enter_near_threat_height_without_horizontal_clearance(self):
        world = self.terrain([(0, 66, 0, "birch_leaves"), (1, 64, 0, "dirt"),
                              (2, 64, 0, "dirt"), (3, 64, 0, "dirt"), (4, 64, 0, "dirt")])
        result = SurvivalSkills(world).execute("retreat_from", {"pos": [0.5, 63, 2.5]})
        self.assertEqual(result.error_code, "unsafe")
        self.assertGreater(result.data["terrain_search"]["height_guard_rejections"], 0)
        self.assertFalse(world.writes())

    def test_unstable_start_does_not_combine_consecutive_drops(self):
        world = self.canopy()
        world.status["on_ground"] = False
        result = self.retreat(world)
        self.assertEqual(result.error_code, "unsafe")
        self.assertEqual(result.data["terrain_search"]["termination"], "not_grounded")
        self.assertFalse(result.data["executed"])
        self.assertFalse(world.writes())

    def test_failed_waypoint_preserves_target_and_distinguishes_unknown_transport(self):
        for code in ("stalled", "network_error"):
            with self.subTest(code=code):
                world = self.stairs()

                def move(*_):
                    raise APIError(code, "offline path failure", details={"last_path_status": "noPath"})

                world.move_effect = move
                result = self.retreat(world)
                self.assertEqual(result.error_code, code)
                self.assertEqual(result.data["target"], [1.5, 65, 0.5])
                self.assertEqual(result.data["planned_path"][-1], [2.5, 66, 0.5])
                self.assertTrue(result.data["executed"])
                self.assertEqual(result.data["candidate_failed"], code == "stalled")
                self.assertEqual(result.data["api_details"]["last_path_status"], "noPath")


class EdgeRetreatTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.world.ground = "air"
        self.world.status["pos"] = [2.2, 64, 0.5]
        self.world.set_block([1, 63, 0], "birch_leaves", properties={"waterlogged": False})
        self.world.set_block([0, 63, 0], "dirt")
        self.world.set_block([-1, 63, 0], "dirt")

    def retreat(self, threat=None):
        return SurvivalSkills(self.world).execute("retreat_from", {"pos": threat or [6, 64, 0.5]})

    def test_guaranteed_aabb_contact_enters_support_only_with_a_complete_ground_route(self):
        result = self.retreat()
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["edge_start"])
        self.assertEqual(result.data["origin_support"]["center_name"], "air")
        self.assertEqual(result.data["origin_support"]["contacts"][0]["pos"], [1, 63, 0])
        self.assertGreater(result.data["origin_support"]["contacts"][0]["minimum_overlap"][0], 0.05)
        self.assertEqual(result.data["target"], [1.5, 64, 0.5])
        self.assertEqual(result.data["planned_path"][0], [2.2, 64, 0.5])
        self.assertEqual(result.data["final_target"], [-0.5, 64, 0.5])
        self.assertFalse(result.data["retreat_complete"])
        self.assertEqual([call[0] for call in self.world.writes()], ["move_to"])
        self.assertEqual(self.world.writes()[0][2]["range"], 0)
        self.assertLessEqual(result.data["terrain_search"]["block_reads"], 256)

    def test_nominal_or_nearby_contact_without_guaranteed_overlap_is_refused(self):
        self.world.status["pos"] = [2.3, 64, 0.5]
        # At nominal x=2.3 the box barely touches x=1; at possible x=2.35
        # from the API rounding interval it does not. Nearby z=-1 is unrelated.
        self.world.set_block([2, 63, -1], "birch_leaves", properties={"waterlogged": False})
        result = self.retreat()
        self.assertEqual(result.error_code, "unsafe")
        self.assertFalse(result.data["edge_start"])
        self.assertEqual(result.data["origin_support"]["contacts"], [])
        self.assertFalse(self.world.writes())

    def test_centering_checks_full_swept_width_not_only_both_center_columns(self):
        self.world.status["pos"] = [2.2, 64, 0.7]
        self.world.set_block([1, 64, 1], "oak_leaves")
        result = self.retreat()
        self.assertEqual(result.error_code, "unsafe")
        self.assertTrue(result.data["edge_start"])
        self.assertEqual(result.data["origin_support"]["entry_rejections"]["swept_body_blocked"], 1)
        self.assertIn({"pos": [1, 64, 1], "name": "oak_leaves", "reason": "body_blocked"}, result.data["blocked_cells"])
        self.assertFalse(self.world.writes())

    def test_contact_is_not_permission_to_approach_threat_or_walk_an_unproven_canopy(self):
        result = self.retreat([0, 64, 0.5])
        self.assertEqual(result.error_code, "unsafe")
        self.assertEqual(result.data["origin_support"]["entry_rejections"]["threat_proximity"], 1)
        self.assertFalse(self.world.writes())
        self.world.set_block([0, 63, 0], "air")
        self.world.set_block([-1, 63, 0], "air")
        result = self.retreat()
        self.assertEqual(result.error_code, "unsafe")
        self.assertEqual(result.data["origin_support"]["state"], "edge_entry_verified")
        self.assertIsNone(result.data["target"])
        self.assertFalse(self.world.writes())

    def test_waterlogged_contact_or_unknown_center_support_remains_unsafe(self):
        self.world.set_block([1, 63, 0], "birch_leaves", properties={"waterlogged": True})
        self.assertEqual(self.retreat().error_code, "unsafe")
        self.world.set_block([1, 63, 0], "birch_leaves", properties={"waterlogged": False})
        original = self.world.get_block

        def read(pos, **kwargs):
            if pos == [2, 63, 0]:
                raise APIError("not_loaded", "Unknown center support")
            return original(pos, **kwargs)

        self.world.get_block = read
        result = self.retreat()
        self.assertEqual(result.error_code, "unsafe")
        self.assertEqual(result.data["origin_support"]["state"], "unverified")
        self.assertFalse(self.world.writes())

    def test_last_status_must_preserve_contact_and_the_checked_swept_footprint(self):
        for updated in ([2.3, 64, 0.5], [2.2, 64, 0.8]):
            with self.subTest(updated=updated):
                self.world.status["pos"] = [2.2, 64, 0.5]
                self.world.calls.clear()
                reads = 0
                original = World.get_status.__get__(self.world)

                def status(**kwargs):
                    nonlocal reads
                    reads += 1
                    if reads == 4:
                        self.world.status["pos"] = updated
                    return original(**kwargs)

                self.world.get_status = status
                result = self.retreat()
                self.assertEqual(result.error_code, "unsafe")
                self.assertIn("Edge support contact changed", result.message)
                self.assertFalse(result.data["executed"])
                self.assertFalse(self.world.writes())


class ObservedCanopyRetreatTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).parent / "fixtures" / "retreat_canopy_steps.json"
        self.fixture = json.loads(path.read_text(encoding="utf-8"))
        self.world = World()
        self.world.status["pos"] = list(self.fixture["origin"])
        self.queries = []

        def read(pos, **kwargs):
            key = ",".join(str(math.floor(value)) for value in pos)
            self.queries.append(key)
            block = self.fixture["blocks"].get(key)
            if block is None:
                raise APIError("not_loaded", "Outside the observed fixture")
            return envelope({"pos": pos, **block})

        self.world.get_block = read
        self.skills = SurvivalSkills(self.world)

    def step(self, context=None):
        return self.skills.execute("retreat_from", {"pos": self.fixture["threat"], "distance": 8},
                                   recovery_context=context or {"evacuation_origin": self.fixture["origin"]})

    def test_observed_canopy_reaches_grass_in_four_verified_single_steps(self):
        previous = self.fixture["origin"]
        for index, target in enumerate(self.fixture["expected_waypoints"]):
            with self.subTest(index=index):
                calls_before = len(self.world.writes())
                result = self.step({"evacuating": index > 0, "evacuation_origin": self.fixture["origin"]})
                self.assertTrue(result.success, result)
                self.assertEqual(result.data["target"], target)
                self.assertEqual(len(self.world.writes()) - calls_before, 1)
                self.assertEqual(self.world.writes()[-1][2]["range"], 0)
                self.assertEqual(abs(math.floor(target[0]) - math.floor(previous[0])) +
                                 abs(math.floor(target[2]) - math.floor(previous[2])), 1)
                self.assertTrue(-2 <= target[1] - previous[1] <= 1)
                self.assertLessEqual(result.data["terrain_search"]["block_reads"], 256)
                self.assertEqual(result.data["retreat_complete"], index == 3)
                self.assertEqual(result.data["evacuation_stage"], "ground" if index == 3 else "relay")
                previous = target
        support = ",".join(str(value) for value in (9, 109, -6))
        self.assertEqual(self.fixture["blocks"][support]["name"], "grass_block")
        # A successful ground exit must not need the old impossible 3D +2 reward.
        self.assertLess(result.data["distance_after"], result.data["distance_before"] + 2)
        self.assertEqual(len(set(self.queries)), len(self.fixture["blocks"]))

    def test_world_changes_after_relay_force_a_new_plan_instead_of_replaying_path(self):
        first = self.step()
        self.assertTrue(first.success, first)
        self.fixture["blocks"]["9,109,-6"] = {"name": "lava", "properties": {}}
        second = self.step({"evacuating": True, "evacuation_origin": self.fixture["origin"]})
        self.assertEqual(second.error_code, "unsafe")
        self.assertIsNone(second.data["target"])
        self.assertFalse(second.data["executed"])
        self.assertEqual(len(self.world.writes()), 1)

    def test_excluded_failed_waypoint_is_never_selected_again(self):
        blocked = [9, 114, -9]
        result = self.step({"excluded_targets": [blocked], "evacuating": False})
        if result.success:
            self.assertNotEqual([math.floor(v) for v in result.data["target"]], blocked)
            self.assertNotIn(blocked, [[math.floor(v) for v in point] for point in result.data["planned_path"]])
        else:
            self.assertEqual(result.error_code, "unsafe")
            self.assertFalse(self.world.writes())

    def test_recovery_context_is_internal_bounded_and_validated(self):
        for context in ({"excluded_targets": [[0, 1, 2]] * 9}, {"excluded_targets": [[1.5, 2, 3]]},
                        {"evacuating": "yes"}, {"evacuation_origin": [0, float("nan"), 0]}, {"unknown": True}):
            with self.subTest(context=context):
                self.assertEqual(self.step(context).error_code, "bad_args")
                self.assertFalse(self.world.writes())


class SurvivalTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.skills = SurvivalSkills(self.world)
        self.target = [2, 64, 0]

    def gather(self, name="oak_log"):
        self.world.set_block(self.target, name)
        return self.skills.execute("gather_block", {"pos": self.target, "expected_name": name})

    def test_log_gather_requires_removed_block_and_real_inventory_gain(self):
        result = self.gather()
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["gained"], {"oak_log": 1})
        self.assertEqual([call[0] for call in self.world.writes()], ["dig"])

    def test_stone_gather_requires_a_pickaxe_and_checks_actual_drop(self):
        result = self.gather("stone")
        self.assertEqual(result.error_code, "missing_tool")
        self.assertFalse(self.world.writes())
        self.world.counts["wooden_pickaxe"] = 1
        result = self.gather("stone")
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["gained"], {"cobblestone": 1})

    def test_approach_accounts_for_goal_near_grid_and_actual_feet_position(self):
        # GoalNear checks integer path nodes, but feet are at the cell center.
        # A radius-3 endpoint on this side is sqrt(3.5**2 + 0.5**2) away.
        target = [-4, 64, 4]
        for action in ("gather_block", "place_item"):
            with self.subTest(action=action):
                world = World()
                world.status["pos"] = [4.4, 64, 3.5]
                world.counts = {"wooden_pickaxe": 1, "crafting_table": 1}

                def move_to(pos, kwargs):
                    node = [math.floor(pos[0]) + kwargs["range"], math.floor(pos[1]), math.floor(pos[2])]
                    world.status["pos"] = [node[0] + 0.5, node[1], node[2] + 0.5]
                    return envelope({"pos": world.status["pos"]})

                world.move_effect = move_to
                if action == "gather_block":
                    world.set_block(target, "stone")
                    args = {"pos": target, "expected_name": "stone"}
                else:
                    args = {"pos": target, "item": "crafting_table"}
                result = SurvivalSkills(world).execute(action, args)
                self.assertTrue(result.success, result)
                self.assertLessEqual(math.dist(world.status["pos"], target), 3.5)
                self.assertEqual([call[2]["range"] for call in world.calls if call[0] == "move_to"], [2])

    def test_approach_still_refuses_dig_when_actual_position_is_too_far(self):
        self.target = [-4, 64, 4]
        self.world.status["pos"] = [4.4, 64, 3.5]
        self.world.counts["wooden_pickaxe"] = 1

        def move_to(pos, kwargs):
            self.world.status["pos"] = [pos[0] + 3.5, pos[1], pos[2] + 0.5]
            return envelope({"pos": self.world.status["pos"]})

        self.world.move_effect = move_to
        result = self.gather("stone")
        self.assertEqual(result.error_code, "out_of_reach")
        self.assertEqual([call[0] for call in self.world.writes()], ["move_to", "stop"])

    def test_reachable_high_log_does_not_force_a_climb_before_digging(self):
        self.world.status["pos"] = [4.3, 109, -5.5]
        target = [6, 112, -4]
        self.world.set_block(target, "oak_log", can_dig=True, can_see=True)
        self.world.move_effect = lambda *_: self.fail("Reachable log must not force pathfinding")
        result = self.skills.execute("gather_block", {"pos": target, "expected_name": "oak_log"})
        self.assertTrue(result.success, result)
        self.assertEqual([call[0] for call in self.world.writes()], ["dig"])
        self.assertEqual(result.data["gained"], {"oak_log": 1})

    def test_occluded_high_log_does_not_use_reach_to_bypass_approach(self):
        self.world.status["pos"] = [4.3, 109, -5.5]
        target = [6, 112, -4]
        self.world.set_block(target, "oak_log", can_dig=True, can_see=False)
        result = self.skills.execute("gather_block", {"pos": target, "expected_name": "oak_log"})
        self.assertEqual(result.error_code, "target_occluded")
        self.assertFalse(result.data["executed"])
        self.assertFalse(self.world.writes())

    def test_failed_approach_keeps_api_path_diagnostics_for_model_feedback(self):
        target = [8, 64, 0]
        self.world.set_block(target, "oak_log", can_dig=False, can_see=True)
        def blocked(*_):
            raise APIError("path_not_found", "No supported route", details={"last_path_status": "noPath"})
        self.world.move_effect = blocked
        result = self.skills.execute("gather_block", {"pos": target, "expected_name": "oak_log"})
        self.assertEqual(result.error_code, "path_not_found")
        self.assertEqual(result.data["api_details"]["last_path_status"], "noPath")
        self.assertEqual([call[0] for call in self.world.writes()], ["move_to", "stop"])

    def test_original_api_reach_hint_needs_clear_voxel_sight_before_dig(self):
        self.world.status["pos"] = [4.3, 109, -5.5]
        target = [6, 112, -4]
        self.world.set_block(target, "oak_log", harvest_tool_ok=True)
        self.world.move_effect = lambda *_: self.fail("Visible reachable log must not force climbing")
        result = self.skills.execute("gather_block", {"pos": target, "expected_name": "oak_log"})
        self.assertTrue(result.success, result)
        self.assertEqual([call[0] for call in self.world.writes()], ["dig"])

    def test_original_api_reach_hint_cannot_dig_through_a_dirt_wall(self):
        self.world.status["pos"] = [4.3, 109, -5.5]
        target = [6, 112, -4]
        self.world.set_block(target, "oak_log", harvest_tool_ok=True)
        for y in range(109, 114):
            for z in range(-7, -2):
                self.world.set_block([5, y, z], "dirt")
        result = self.skills.execute("gather_block", {"pos": target, "expected_name": "oak_log"})
        self.assertEqual(result.error_code, "target_occluded")
        self.assertEqual(result.data["visibility"]["blocker"][0], 5)
        self.assertFalse(self.world.writes())

    def test_far_target_is_approached_before_testing_fresh_sight(self):
        target = [8, 64, 0]
        self.world.set_block(target, "oak_log", harvest_tool_ok=False)

        def approach(pos, kwargs):
            self.world.status["pos"] = [6.5, 64, 0.5]
            return envelope({"pos": self.world.status["pos"]})

        self.world.move_effect = approach
        with patch("skills.survival.trace_block_visibility", wraps=trace_block_visibility) as trace:
            result = self.skills.execute("gather_block", {"pos": target, "expected_name": "oak_log"})
        self.assertTrue(result.success, result)
        self.assertEqual(trace.call_args.args[0], [6.5, 64, 0.5])
        self.assertEqual([call[0] for call in self.world.writes()], ["move_to", "dig"])

    def test_unknown_voxel_sight_never_authorizes_dig(self):
        with patch("skills.survival.trace_block_visibility", return_value={
                "state": "unknown", "visible": None, "reason": "query_budget_exhausted", "queries": 32}):
            result = self.gather()
        self.assertEqual(result.error_code, "visibility_unknown")
        self.assertIsNone(result.data["can_see"])
        self.assertFalse(self.world.writes())

    def test_cancel_during_voxel_reads_never_authorizes_dig(self):
        cancel = threading.Event()
        self.world.set_block(self.target, "oak_log", harvest_tool_ok=True)
        original = self.world.get_block

        def read(pos):
            if pos == [0, 65, 0]:  # First ray cell is outside the target guard.
                cancel.set()
            return original(pos)

        self.world.get_block = read
        result = self.skills.execute("gather_block", {"pos": self.target, "expected_name": "oak_log"}, cancel)
        self.assertEqual(result.error_code, "cancelled")
        self.assertFalse(self.world.writes())

    def test_dig_without_pickup_preserves_collection_failure_evidence(self):
        def partial(pos):
            self.world.set_block(pos, "air")
            return envelope({"dug": "oak_log", "collection": {
                "completed": False, "error": {"code": "stalled"}}})
        self.world.dig_effect = partial
        result = self.gather()
        self.assertEqual(result.error_code, "gather_unverified")
        self.assertTrue(result.data["block_removed"])
        self.assertEqual(result.data["collection"]["error"]["code"], "stalled")

    def test_removed_block_without_inventory_reports_at_most_eight_matching_drops(self):
        def dig(pos):
            self.world.set_block(pos, "air")
            return envelope({"dug": "oak_log", "gained": {"oak_log": 99}})
        self.world.dig_effect = dig
        self.world.entities = [
            {"id": index, "name": "item", "type": "object", "item": "oak_logx2",
             "pos": [2 + index / 10, 64.2, 0.5]} for index in reversed(range(10))]
        self.world.entities += [
            {"name": "zombie", "item": "oak_logx2", "pos": self.target},
            {"name": "item", "item": "dirtx1", "pos": self.target},
            {"name": "item", "item": "oak_logx1", "pos": [float("nan"), 64, 0]},
            {"name": "item", "item": "oak_logx1", "pos": [20, 64, 0]},
            {"name": "item", "item": "oak_logx0", "pos": self.target}]
        result = self.gather()
        self.assertEqual(result.error_code, "gather_unverified")
        self.assertTrue(result.data["block_removed"])
        self.assertEqual(result.data["gained"], {})
        drops = result.data["visible_drops"]
        self.assertEqual([drop["id"] for drop in drops], list(range(8)))
        self.assertTrue(all(drop["name"] == "oak_log" and drop["count"] == 2 for drop in drops))
        self.assertEqual([call for call in self.world.calls if call[0] == "get_entities"], [("get_entities", 16, ["item"])])
        self.assertEqual([call[0] for call in self.world.writes()], ["dig", "stop"])

    def test_failed_drop_query_preserves_primary_gather_evidence_without_error_text(self):
        def dig(pos):
            self.world.set_block(pos, "air")
            return envelope({"dug": "oak_log"})
        self.world.dig_effect = dig
        self.world.get_entities = Mock(side_effect=APIError("network_error", "secret diagnostic must not leak"))
        result = self.gather()
        self.assertEqual(result.error_code, "gather_unverified")
        self.assertTrue(result.data["block_removed"])
        self.assertEqual(result.data["drop_query_error"], "network_error")
        self.assertNotIn("secret diagnostic", str(result))
        self.world.get_entities.assert_called_once_with(radius=16, types=["item"])
        self.assertEqual([call[0] for call in self.world.writes()], ["dig", "stop"])

    def test_drop_query_is_not_needed_for_verified_inventory_or_unchanged_block(self):
        self.world.get_entities = Mock(side_effect=AssertionError("Unnecessary drop query"))
        self.assertTrue(self.gather().success)
        self.world.dig_effect = lambda _: envelope({"gained": {"oak_log": 1}})
        self.assertEqual(self.gather().error_code, "gather_unverified")
        self.world.get_entities.assert_not_called()

    def test_copper_pickaxe_supported_by_current_minecraft_data_is_usable(self):
        self.world.counts["copper_pickaxe"] = 1
        result = self.gather("coal_ore")
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["gained"], {"coal": 1})

    def test_gained_response_without_actual_inventory_change_is_not_success(self):
        def dig(pos):
            self.world.set_block(pos, "air")
            return envelope({"gained": {"oak_log": 9}})
        self.world.dig_effect = dig
        result = self.gather()
        self.assertEqual(result.error_code, "gather_unverified")
        self.assertEqual(self.world.writes()[-1][0], "stop")

    def test_inventory_gain_without_block_removal_is_not_success(self):
        def dig(pos):
            self.world.counts["oak_log"] = 1
            return envelope({"gained": {"oak_log": 1}})
        self.world.dig_effect = dig
        self.assertEqual(self.gather().error_code, "gather_unverified")

    def test_unsupported_and_hazardous_blocks_are_never_dug(self):
        for name in ("water", "lava", "fire", "diamond_ore", "chest", "wheat"):
            with self.subTest(name=name):
                self.world.calls.clear()
                self.assertEqual(self.gather(name).error_code, "unsupported_block")
                self.assertFalse(self.world.writes())

    def test_gather_rejects_dangerous_dimensions_and_status(self):
        for update in ({"dimension": "the_nether"}, {"dimension": "the_end"}, {"in_water": True}, {"on_fire": True}):
            with self.subTest(update=update):
                original = copy.deepcopy(self.world.status)
                self.world.status.update(update)
                self.world.calls.clear()
                self.assertFalse(self.gather().success)
                self.assertFalse(self.world.writes())
                self.world.status = original

    def test_gather_rejects_same_vertical_column(self):
        for pos in ([0, 63, 0], [0, 66, 0]):
            self.world.set_block(pos, "oak_log")
            result = self.skills.execute("gather_block", {"pos": pos, "expected_name": "oak_log"})
            self.assertEqual(result.error_code, "unsafe")
        self.assertFalse(self.world.writes())

    def test_gather_rejects_adjacent_water_and_falling_sand(self):
        for block in ({"name": "water"}, {"name": "sand"}, {"name": "oak_log", "properties": {"waterlogged": True}}):
            with self.subTest(block=block):
                self.world.blocks[(2, 65, 0)] = block
                result = self.gather()
                self.assertEqual(result.error_code, "unsafe")
                self.assertFalse(self.world.writes())

    def test_gather_expected_name_prevents_stale_target(self):
        self.world.set_block(self.target, "chest")
        result = self.skills.execute("gather_block", {"pos": self.target, "expected_name": "oak_log"})
        self.assertEqual(result.error_code, "target_changed")
        self.assertFalse(self.world.writes())

    def test_unknown_dig_completion_is_not_retried(self):
        def timeout(pos):
            raise APIError("request_timeout", "completion unknown")
        self.world.dig_effect = timeout
        result = self.gather()
        self.assertEqual(result.error_code, "request_timeout")
        self.assertEqual([call[0] for call in self.world.writes()], ["dig", "stop"])

    def test_busy_rejection_does_not_stop_someone_elses_action(self):
        def busy(pos):
            raise APIError("busy", "another action started")
        self.world.dig_effect = busy
        self.assertEqual(self.gather().error_code, "busy")
        self.assertEqual([call[0] for call in self.world.writes()], ["dig"])

    def test_invalid_observation_after_action_is_an_unknown_outcome(self):
        def craft(item, count):
            self.world.counts[item] = count
            self.world.status["pos"] = None
            return envelope({"gained": count})
        self.world.craft_effect = craft
        result = self.skills.execute("craft_item", {"item": "stick"})
        self.assertEqual(result.error_code, "invalid_response")
        self.assertEqual([call[0] for call in self.world.writes()], ["craft", "stop"])

    def test_craft_uses_increment_not_preexisting_inventory(self):
        self.world.counts["stick"] = 10
        result = self.skills.execute("craft_item", {"item": "stick", "count": 4})
        self.assertTrue(result.success, result)
        self.assertEqual((result.data["before"], result.data["after"], result.data["gained"]), (10, 14, 4))

    def test_partial_craft_has_explicit_partial_result(self):
        def craft(item, count):
            self.world.counts[item] = 2
            return envelope({"gained": count})
        self.world.craft_effect = craft
        result = self.skills.execute("craft_item", {"item": "stick", "count": 4})
        self.assertFalse(result.success)
        self.assertEqual((result.error_code, result.data["gained"]), ("partial_completion", 2))

    def test_server_failure_after_partial_craft_reports_inventory_gain(self):
        def craft(item, count):
            self.world.counts[item] = 2
            raise APIError("missing_ingredients", "ran out")
        self.world.craft_effect = craft
        result = self.skills.execute("craft_item", {"item": "stick", "count": 4})
        self.assertEqual(result.error_code, "partial_completion")
        self.assertEqual(result.data["cause"], "missing_ingredients")

    def test_unreadable_craft_response_stays_unknown_despite_inventory_side_effect(self):
        for code in ("invalid_json", "invalid_response"):
            with self.subTest(code=code):
                world = World()

                def craft(item, count):
                    world.counts[item] = 2
                    raise APIError(code, "Craft action outcome is unknown")

                world.craft_effect = craft
                result = SurvivalSkills(world).execute("craft_item", {"item": "stick", "count": 4})
                self.assertEqual(result.error_code, code)
                self.assertNotIn("gained", result.data)
                self.assertEqual([call[0] for call in world.writes()], ["craft", "stop"])
                self.assertEqual(len([call for call in world.calls if call[0] == "inventory"]), 1)

    def test_no_crafted_inventory_is_not_success(self):
        self.world.craft_effect = lambda item, count: envelope({"gained": count})
        self.assertEqual(self.skills.execute("craft_item", {"item": "stick"}).error_code, "craft_unverified")

    def test_place_checks_world_and_consumption(self):
        self.world.counts["crafting_table"] = 1
        result = self.skills.execute("place_item", {"item": "crafting_table", "pos": self.target})
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["used"], 1)

    def test_place_rejects_occupied_unsupported_or_self_cell_without_moving(self):
        self.world.counts["dirt"] = 3
        for pos, block in (([10, 64, 0], "chest"), ([10, 63, 0], "lava"), ([0, 64, 0], "air")):
            with self.subTest(pos=pos):
                self.world.blocks.clear()
                self.world.set_block(pos, block)
                target = [10, 64, 0] if pos[0] == 10 else pos
                result = self.skills.execute("place_item", {"item": "dirt", "pos": target})
                self.assertEqual(result.error_code, "unsafe")
                self.assertFalse(self.world.writes())

    def test_place_does_not_trust_success_without_consumption(self):
        self.world.counts["dirt"] = 1
        def place(item, pos):
            self.world.set_block(pos, item)
            return envelope({"placed": item})
        self.world.place_effect = place
        self.assertEqual(self.skills.execute("place_item", {"item": "dirt", "pos": self.target}).error_code, "place_unverified")

    def test_dangerous_place_items_are_rejected(self):
        for item in ("tnt", "lava_bucket", "red_bed", "respawn_anchor"):
            result = self.skills.execute("place_item", {"item": item, "pos": self.target})
            self.assertEqual(result.error_code, "unsupported_item")
        self.assertFalse(self.world.writes())

    def test_eat_requires_food_gain_and_item_consumption(self):
        self.world.counts["bread"] = 1
        result = self.skills.execute("eat_food", {"item": "bread"})
        self.assertTrue(result.success, result)
        self.assertEqual((result.data["food_before"], result.data["food_after"]), (10, 15))

    def test_unsafe_food_full_food_and_no_food_are_rejected(self):
        for item in ("rotten_flesh", "pufferfish", "chorus_fruit", "suspicious_stew", "poisonous_potato"):
            self.assertEqual(self.skills.execute("eat_food", {"item": item}).error_code, "unsafe_food")
        self.assertEqual(self.skills.execute("eat_food", {"item": "bread"}).error_code, "missing_item")
        self.world.status["food"] = 20
        self.assertEqual(self.skills.execute("eat_food", {"item": "bread"}).error_code, "not_hungry")
        self.assertFalse(self.world.writes())

    def test_food_consumption_without_food_gain_is_not_success(self):
        self.world.counts["bread"] = 1
        def eat(item):
            self.world.counts[item] -= 1
            return envelope({"food": 20})
        self.world.eat_effect = eat
        self.assertEqual(self.skills.execute("eat_food", {"item": "bread"}).error_code, "eat_unverified")

    def test_movement_is_verified_from_actual_position(self):
        result = self.skills.execute("move_to", {"pos": [5, 64, 0]})
        self.assertTrue(result.success, result)
        self.world.status["pos"] = [0.5, 64, 0.5]
        self.world.move_effect = lambda pos, kwargs: envelope({"pos": pos})
        result = self.skills.execute("move_to", {"pos": [5, 64, 0]})
        self.assertEqual(result.error_code, "move_unverified")

    def test_already_near_goal_reports_no_movement_instead_of_recovery(self):
        self.world.status["pos"] = [4.3, 109, -5.5]
        result = self.skills.execute("move_to", {"pos": [4, 108, -6], "range": 2})
        self.assertTrue(result.success)
        self.assertTrue(result.data["already_in_range"])
        self.assertFalse(result.data["executed"])
        self.assertEqual(result.data["moved"], 0)
        self.assertIn("no movement", result.message)
        self.assertFalse(self.world.writes())

    def test_movement_partial_interruption_and_dimension_change_fail(self):
        self.world.move_effect = lambda pos, kwargs: envelope({"interrupted_by": "hurt", "pos": pos})
        self.assertEqual(self.skills.execute("move_to", {"pos": [5, 64, 0]}).error_code, "interrupted")
        def change_dimension(pos, kwargs):
            self.world.status.update(pos=pos, dimension="the_nether")
            return envelope({"pos": pos})
        self.world.move_effect = change_dimension
        self.assertEqual(self.skills.execute("move_to", {"pos": [5, 64, 0]}).error_code, "dimension_changed")

    def test_retreat_checks_safe_corridor_and_increases_threat_distance(self):
        result = self.skills.execute("retreat_from", {"pos": [-3, 64, 0.5]})
        self.assertTrue(result.success, result)
        self.assertGreater(result.data["distance_after"], result.data["distance_before"])
        self.assertEqual([call[0] for call in self.world.writes()], ["move_to"])

    def test_retreat_allows_short_grass_and_leaf_litter_in_body_cells(self):
        for plant in ("short_grass", "leaf_litter"):
            with self.subTest(plant=plant):
                world = World()
                world.set_block([0, 64, 0], plant)
                world.set_block([0, 65, 0], plant)
                result = SurvivalSkills(world).execute("retreat_from", {"pos": [-3, 64, 0.5]})
                self.assertTrue(result.success, result)
                self.assertEqual(result.data["pos"][1], 64)
                self.assertGreater(result.data["distance_after"], result.data["distance_before"])
                self.assertEqual([call[0] for call in world.writes()], ["move_to"])

    def test_retreat_still_rejects_liquid_waterlogged_plants_and_unknown_shapes(self):
        cases = (("water", {}, "hazard"), ("lava", {}, "hazard"),
                 ("leaf_litter", {"properties": {"waterlogged": True}}, "hazard"),
                 ("unknown_shape", {}, "body_blocked"), ("oak_leaves", {}, "body_blocked"))
        for name, properties, reason in cases:
            with self.subTest(name=name):
                world = World()
                world.set_block([0, 64, 0], name, **properties)
                result = SurvivalSkills(world).execute("retreat_from", {"pos": [-3, 64, 0.5]})
                self.assertEqual(result.error_code, "unsafe")
                self.assertIn({"pos": [0, 64, 0], "name": name, "reason": reason}, result.data["blocked_cells"])
                self.assertFalse(world.writes())

    def test_retreat_blocking_diagnostics_are_unique_and_capped_without_extra_reads(self):
        self.world.ground = "air"
        self.world.set_block([0, 63, 0], "stone")
        threat = [3, 64, 0.5]
        result = self.skills.execute("retreat_from", {"pos": threat})
        self.assertEqual(result.error_code, "unsafe")
        blocked = result.data["blocked_cells"]
        self.assertEqual(len(blocked), 4)
        self.assertEqual(len({tuple(cell["pos"]) for cell in blocked}), 4)
        self.assertTrue(all(cell["name"] == "air" and cell["reason"] == "unsupported_ground" for cell in blocked))
        self.assertEqual(result.data["origin"], [0.5, 64, 0.5])
        self.assertEqual(result.data["threat_pos"], threat)
        # Start support/body (3) and three allowed columns at four heights (12).
        self.assertEqual(len([call for call in self.world.calls if call[0] == "get_block"]), 15)
        self.assertFalse(self.world.writes())

    def test_retreat_unloaded_cells_stay_unknown_and_do_not_authorize_movement(self):
        self.world.get_block = Mock(side_effect=APIError("not_loaded", "chunk unavailable"))
        result = self.skills.execute("retreat_from", {"pos": [3, 64, 0.5]})
        self.assertEqual(result.error_code, "unsafe")
        self.assertTrue(result.data["blocked_cells"])
        self.assertLessEqual(len(result.data["blocked_cells"]), 4)
        self.assertTrue(all(cell["name"] is None and cell["reason"] == "not_loaded" for cell in result.data["blocked_cells"]))
        self.assertFalse(self.world.writes())

    def test_retreat_refuses_void_liquid_and_no_direction(self):
        for ground in ("air", "lava", "sand"):
            with self.subTest(ground=ground):
                self.world.ground = ground
                result = self.skills.execute("retreat_from", {"pos": [-3, 64, 0.5]})
                self.assertEqual(result.error_code, "unsafe")
                self.assertFalse(self.world.writes())
        result = self.skills.execute("retreat_from", {"pos": self.world.status["pos"]})
        self.assertEqual(result.error_code, "unsafe")

    def test_cancellation_before_start_makes_no_api_calls(self):
        cancel = threading.Event()
        cancel.set()
        result = self.skills.execute("craft_item", {"item": "stick"}, cancel_event=cancel)
        self.assertEqual(result.error_code, "cancelled")
        self.assertFalse(self.world.calls)

    def test_cancellation_after_action_does_not_claim_success(self):
        cancel = threading.Event()
        def craft(item, count):
            cancel.set()
            self.world.counts[item] = count
            return envelope({"gained": count})
        self.world.craft_effect = craft
        result = self.skills.execute("craft_item", {"item": "stick"}, cancel_event=cancel)
        self.assertEqual(result.error_code, "cancelled")
        self.assertEqual([call[0] for call in self.world.writes()], ["craft", "stop"])

    def test_dead_not_ready_and_busy_never_start_actions(self):
        for update, code in (({"alive": False}, "dead"), ({"ready": False}, "not_ready"), ({"busy": "dig"}, "busy")):
            old = copy.deepcopy(self.world.status)
            self.world.status.update(update)
            result = self.skills.execute("craft_item", {"item": "stick"})
            self.assertEqual(result.error_code, code)
            self.assertFalse(self.world.writes())
            self.world.status = old

    def test_invalid_arguments_do_not_mutate_world(self):
        for name, args in (("unknown", {}), ("move_to", {"pos": [float("nan"), 64, 0]}),
                           ("craft_item", {"item": "stick", "count": True}),
                           ("craft_item", {"item": "../secret"}),
                           ("gather_block", {"pos": [1.5, 64, 0], "expected_name": "stone"}),
                           ("retreat_from", {"pos": [1, 64, 0], "distance": 99}),
                           ("move_to", {"pos": [1, 64, 0], "unknown": 1})):
            with self.subTest(name=name, args=args):
                self.assertFalse(self.skills.execute(name, args).success)
                self.assertFalse(self.world.writes())


class SurvivalWrapperTests(unittest.TestCase):
    @patch("core.client.requests.request")
    def test_dig_and_craft_wait_cover_fixed_backend_phases(self, request):
        request.return_value = Mock(status_code=200)
        request.return_value.json.return_value = envelope({})
        client = MinecraftAgentClient()
        client.dig([1, 64, 2])
        self.assertEqual(request.call_args.kwargs["json"], {"x": 1, "y": 64, "z": 2, "collect": True})
        self.assertGreaterEqual(request.call_args.kwargs["timeout"], 135)
        client.craft("stick", 4)
        self.assertEqual(request.call_args.kwargs["json"], {"item": "stick", "count": 4})
        self.assertGreaterEqual(request.call_args.kwargs["timeout"], 420)
        client.connect()
        self.assertTrue(request.call_args.args[1].endswith("/connect"))


if __name__ == "__main__":
    unittest.main()
