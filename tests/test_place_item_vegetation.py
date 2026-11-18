"""Basic placement on two verified replaceable plants, with no live I/O."""

import copy
import json
import threading
import unittest
from pathlib import Path

from core.client import APIError
from core.survival_state import ObservationCollector
from skills.survival import SurvivalSkills, _Failure
from test_survival_skills import World, envelope


class PlaceVegetationTests(unittest.TestCase):
    TARGET = [2, 64, 0]

    def world(self, name="leaf_litter", support="grass_block"):
        world = World()
        world.counts["crafting_table"] = 1
        world.set_block(self.TARGET, name)
        world.set_block([2, 63, 0], support)
        return world

    def execute(self, world, **kwargs):
        return SurvivalSkills(world).execute(
            "place_item", {"item": "crafting_table", "pos": self.TARGET}, **kwargs)

    def test_verified_plants_are_replaced_by_one_verified_table(self):
        for name in ("leaf_litter", "short_grass", "air"):
            with self.subTest(name=name):
                world = self.world(name)
                result = self.execute(world)
                self.assertTrue(result.success, result)
                self.assertEqual(result.data["placed"], "crafting_table")
                self.assertEqual(result.data["used"], 1)
                self.assertEqual([row[0] for row in world.writes()], ["place"])

    def test_other_obstacles_plants_liquid_and_unknown_are_not_replaceable(self):
        for name in ("grass_block", "chest", "oak_leaves", "snow", "tall_grass",
                     "poppy", "water", "sweet_berry_bush", "unknown"):
            with self.subTest(name=name):
                world = self.world(name)
                result = self.execute(world)
                self.assertEqual(result.error_code, "unsafe", result)
                self.assertFalse(world.writes())

    def test_plants_do_not_become_support_and_waterlogged_cells_stay_unsafe(self):
        for name in ("leaf_litter", "short_grass", "air", "oak_leaves", "snow", "water", "unknown"):
            with self.subTest(support=name):
                world = self.world(support=name)
                self.assertEqual(self.execute(world).error_code, "unsafe")
                self.assertFalse(world.writes())
        for pos, name in ((self.TARGET, "leaf_litter"), ([2, 63, 0], "grass_block")):
            world = self.world()
            world.set_block(pos, name, properties={"waterlogged": "true"})
            self.assertEqual(self.execute(world).error_code, "unsafe")
            self.assertFalse(world.writes())

    def test_fresh_final_guard_rejects_new_obstacle_or_removed_support(self):
        for changed_pos, changed_name in ((self.TARGET, "chest"), ([2, 63, 0], "leaf_litter")):
            with self.subTest(changed_pos=changed_pos):
                world = self.world()
                original = world.get_block
                reads = 0

                def get_block(pos, **kwargs):
                    nonlocal reads
                    if pos == self.TARGET:
                        reads += 1
                        if reads == 3:
                            world.set_block(changed_pos, changed_name)
                    return original(pos, **kwargs)

                world.get_block = get_block
                result = self.execute(world)
                self.assertEqual(result.error_code, "unsafe", result)
                self.assertFalse(world.writes())
                self.assertNotEqual(result.data.get("action_state"), "unknown")

    def test_self_cell_is_never_replaced(self):
        world = self.world()
        world.status["pos"] = [2.5, 64, .5]
        result = self.execute(world)
        self.assertEqual(result.error_code, "unsafe")
        self.assertFalse(world.writes())

    def test_ack_without_world_change_or_consumption_remains_known_failure(self):
        for change_block, consume in ((False, True), (True, False), (False, False)):
            with self.subTest(change_block=change_block, consume=consume):
                world = self.world()

                def place(item, pos):
                    if change_block:
                        world.set_block(pos, item)
                    if consume:
                        world.counts[item] -= 1
                    return envelope({"placed": item})

                world.place_effect = place
                result = self.execute(world)
                self.assertEqual(result.error_code, "place_unverified", result)
                self.assertNotEqual(result.data.get("action_state"), "unknown")
                self.assertEqual(sum(row[0] == "place" for row in world.writes()), 1)

    def test_unknown_write_reply_has_one_place_and_no_cleanup_or_verification(self):
        for code in ("request_timeout", "network_error", "not_connected", "invalid_json", "invalid_response"):
            with self.subTest(code=code):
                world = self.world()

                def place(item, pos):
                    world.set_block(pos, item)
                    world.counts[item] -= 1
                    raise APIError(code, "unknown placement response")

                world.place_effect = place
                result = self.execute(world)
                self.assertFalse(result.success)
                self.assertEqual(result.data.get("action_state"), "unknown", result)
                self.assertIn(result.data.get("cleanup_skipped"), ("placement_unknown", "disconnected_unknown"))
                self.assertTrue(result.data["executed"])
                self.assertEqual([row[0] for row in world.writes()], ["place"])
                self.assertEqual(world.calls[-1][0], "place")

    def test_malformed_and_interrupted_success_replies_remain_unknown(self):
        for response in (None, {"ok": True, "result": []}, envelope({"interrupted_by": "hurt"})):
            with self.subTest(response=response):
                world = self.world()
                world.place_effect = lambda item, pos: response
                result = self.execute(world)
                self.assertFalse(result.success)
                self.assertEqual(result.data.get("action_state"), "unknown", result)
                self.assertEqual(result.data.get("cleanup_skipped"), "placement_unknown")
                self.assertEqual([row[0] for row in world.writes()], ["place"])

    def test_explicit_unknown_evidence_overrides_nonstandard_error_code(self):
        for error in (APIError("place_failed", "unknown write", 200, {"action_state": "unknown"}),
                      _Failure("place_failed", "unknown write", {"action_state": "unknown"})):
            with self.subTest(error=type(error).__name__):
                world = self.world()

                def place(item, pos):
                    raise error

                world.place_effect = place
                result = self.execute(world)
                self.assertEqual(result.error_code, "place_failed")
                self.assertEqual(result.data.get("action_state"), "unknown")
                self.assertEqual(result.data.get("cleanup_skipped"), "placement_unknown")
                self.assertEqual([row[0] for row in world.writes()], ["place"])

    def test_post_write_read_failures_do_not_add_actions(self):
        for operation in ("get_status", "get_block", "inventory"):
            for code in ("network_error", "not_loaded", "invalid_response"):
                with self.subTest(operation=operation, code=code):
                    world = self.world()
                    original = getattr(world, operation)

                    def read(*args, **kwargs):
                        if any(row[0] == "place" for row in world.calls):
                            raise APIError(code, "post-write evidence unavailable")
                        return original(*args, **kwargs)

                    setattr(world, operation, read)
                    result = self.execute(world)
                    self.assertEqual(result.data.get("action_state"), "unknown", result)
                    self.assertEqual([row[0] for row in world.writes()], ["place"])

    def test_prewrite_failure_is_not_unknown_write(self):
        world = self.world()
        original = world.get_block
        reads = 0

        def get_block(pos, **kwargs):
            nonlocal reads
            if pos == self.TARGET:
                reads += 1
                if reads == 3:
                    raise APIError("request_timeout", "final prewrite read failed")
            return original(pos, **kwargs)

        world.get_block = get_block
        result = self.execute(world)
        self.assertEqual(result.error_code, "request_timeout")
        self.assertFalse(result.data["executed"])
        self.assertNotEqual(result.data.get("action_state"), "unknown")
        self.assertFalse(world.writes())

    def test_cancel_after_place_is_unknown_without_stop(self):
        world = self.world()
        cancel = threading.Event()

        def place(item, pos):
            cancel.set()
            return envelope({"placed": item})

        world.place_effect = place
        result = self.execute(world, cancel_event=cancel)
        self.assertEqual(result.error_code, "action_unconfirmed", result)
        self.assertEqual(result.data.get("action_state"), "unknown")
        self.assertEqual([row[0] for row in world.writes()], ["place"])


class CapturedTableGeometryTests(unittest.TestCase):
    """Actual scene subset; missing terrain is never replaced by synthetic air."""

    def setUp(self):
        self.scene = json.loads((Path(__file__).parent / "fixtures" / "live_table_vegetation.json").read_text(encoding="utf-8"))

    def world(self):
        world = World()
        world.status = copy.deepcopy(self.scene["status"])
        world.counts["crafting_table"] = 1
        world.blocks = {tuple(map(int, key.split(","))): copy.deepcopy(row)
                        for key, row in self.scene["blocks"].items()}

        def get_block(pos, **kwargs):
            key = tuple(pos)
            world.calls.append(("get_block", key))
            if key not in world.blocks:
                raise APIError("not_loaded", "Coordinate absent from captured scene")
            return envelope(world.blocks[key])

        world.get_block = get_block
        return world

    def test_collector_preserves_actual_replaceable_target_and_rejects_litter_support(self):
        world = self.world()
        result = ObservationCollector(world)._local_geometry(world.status, None)
        candidates = {tuple(row["pos"]): row for row in result["placement_candidates"]}
        target = tuple(self.scene["expected_replaceable"])
        self.assertEqual(candidates[target]["support"], "grass_block")
        for pos in self.scene["expected_no_support"]:
            self.assertNotIn(tuple(pos), candidates)
        reads = [row[1] for row in world.calls if row[0] == "get_block"]
        self.assertLessEqual(len(reads), 60)
        self.assertEqual(len(reads), len(set(reads)))
        self.assertFalse(world.writes())

    def test_actual_target_and_support_pairs_match_executor_classification(self):
        for pos in [self.scene["expected_replaceable"], *self.scene["expected_no_support"]]:
            with self.subTest(pos=pos):
                world = self.world()
                result = SurvivalSkills(world).execute("place_item", {"item": "crafting_table", "pos": pos})
                expected = pos == self.scene["expected_replaceable"]
                self.assertEqual(result.success, expected, result)
                self.assertEqual([row[0] for row in world.writes()], ["place"] if expected else [])
                if not expected:
                    self.assertEqual(result.error_code, "unsafe")

    def test_unknown_target_or_support_is_never_a_candidate(self):
        target = self.scene["expected_replaceable"]
        for key in (tuple(target), (target[0], target[1] - 1, target[2])):
            with self.subTest(missing=key):
                world = self.world()
                world.blocks.pop(key)
                result = ObservationCollector(world)._local_geometry(world.status, None)
                self.assertNotIn(target, [row["pos"] for row in result["placement_candidates"]])
                action = SurvivalSkills(world).execute("place_item", {"item": "crafting_table", "pos": target})
                self.assertEqual(action.error_code, "not_loaded")
                self.assertFalse(action.data["executed"])
                self.assertFalse(world.writes())


if __name__ == "__main__":
    unittest.main()
