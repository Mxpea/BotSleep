"""Verified single-column edge descent; no game or model requests."""

import unittest

from core.client import APIError
from skills.survival import SurvivalSkills
from test_survival_skills import World


class EdgeDropRetreatTests(unittest.TestCase):
    def world(self, center="leaf_litter", continuation=True):
        world = World()
        world.ground = "air"
        world.status.update(pos=[8.6, 114, 10], food=17, last_death=None)
        world.set_block([8, 113, 9], "grass_block")
        world.set_block([8, 113, 10], center)
        world.set_block([8, 112, 10], "grass_block")
        if continuation:
            for z in (11, 12, 13):
                world.set_block([8, 112, z], "grass_block")
        return world

    def retreat(self, world, recovery=None):
        return SurvivalSkills(world).execute("retreat_from", {"pos": [8.3, 114, 7.8]},
                                              recovery_context=recovery)

    def test_observed_litter_edge_moves_down_without_returning_toward_skeleton(self):
        for center in ("air", "cave_air", "leaf_litter"):
            with self.subTest(center=center):
                world = self.world(center)
                result = self.retreat(world)
                self.assertTrue(result.success, result)
                self.assertEqual(result.data["target"], [8.5, 113, 10.5])
                self.assertEqual(result.data["edge_drop"], {
                    "source_contact": [8, 113, 9], "target": [8.5, 113, 10.5],
                    "drop_height": 1, "landing_verified": True})
                self.assertFalse(result.data["retreat_complete"])
                self.assertEqual([row[0] for row in world.writes()], ["move_to"])
                self.assertEqual(world.writes()[0][2]["range"], 0)
                self.assertLessEqual(result.data["terrain_search"]["block_reads"], 256)

    def test_only_verified_drop_can_execute_with_unknown_surroundings(self):
        world = self.world(continuation=False)
        original = world.get_block
        known = {(8, 113, 9), (8, 113, 10), (8, 112, 10),
                 (8, 114, 9), (8, 115, 9), (8, 114, 10), (8, 115, 10)}

        def read(pos, **kwargs):
            if tuple(pos) not in known:
                raise APIError("not_loaded", "Outside the observed edge")
            return original(pos, **kwargs)

        world.get_block = read
        result = self.retreat(world)
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["terrain_search"]["termination"], "verified_edge_drop")
        self.assertFalse(result.data["ground_route_verified"])
        self.assertFalse(result.data["retreat_complete"])
        self.assertIsNone(result.data["final_target"])
        self.assertEqual(len(world.writes()), 1)
        self.assertLessEqual(result.data["terrain_search"]["block_reads"], 256)

    def test_direct_drop_does_not_expand_canopy_exploration_authority(self):
        world = self.world(continuation=False)
        world.set_block([8, 113, 9], "oak_leaves", properties={"waterlogged": False})
        # Both drop endpoints are outside 16 blocks, but the upper turn enters
        # the shooter's circle. Canopy exploration must retain its old entry.
        world.entities = [{"id": 2, "type": "hostile", "name": "skeleton", "pos": [8.5, 115, 26.43]}]
        result = SurvivalSkills(world).execute("canopy_escape", {})
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["target"], [8.5, 114, 9.5])
        self.assertNotIn("edge_drop", result.data)

    def test_requires_current_neighbor_support_and_does_not_relabel_lower_ground(self):
        for name in ("air", "leaf_litter", "water", "lava", "unknown_block"):
            with self.subTest(name=name):
                world = self.world()
                world.set_block([8, 113, 9], name)
                result = self.retreat(world)
                self.assertEqual(result.error_code, "unsafe")
                self.assertFalse(world.writes())

    def test_does_not_expand_to_other_decorations_or_solid_centers(self):
        for name in ("short_grass", "pink_petals", "moss_carpet", "snow", "water", "lava"):
            with self.subTest(name=name):
                world = self.world(name)
                result = self.retreat(world)
                self.assertEqual(result.error_code, "unsafe")
                self.assertFalse(world.writes())

    def test_lower_floor_must_be_known_solid_and_dry(self):
        for name, properties in (("air", {}), ("leaf_litter", {}), ("oak_leaves", {"waterlogged": False}),
                                 ("water", {}), ("lava", {}), ("unknown_block", {}),
                                 ("grass_block", {"waterlogged": True})):
            with self.subTest(name=name):
                world = self.world()
                world.set_block([8, 112, 10], name, properties=properties)
                result = self.retreat(world)
                self.assertEqual(result.error_code, "unsafe")
                self.assertFalse(world.writes())

    def test_unknown_source_lower_floor_or_headspace_refuses_drop(self):
        for missing in ([8, 113, 9], [8, 112, 10], [8, 115, 10]):
            with self.subTest(missing=missing):
                world = self.world()
                original = world.get_block

                def read(pos, **kwargs):
                    if pos == missing:
                        raise APIError("not_loaded", "Unknown drop geometry")
                    return original(pos, **kwargs)

                world.get_block = read
                self.assertEqual(self.retreat(world).error_code, "unsafe")
                self.assertFalse(world.writes())

    def test_excluded_drop_two_block_drop_and_corner_departure_are_refused(self):
        world = self.world()
        self.assertEqual(self.retreat(world, {"excluded_targets": [[8, 113, 10]]}).error_code, "unsafe")
        self.assertFalse(world.writes())
        world = self.world()
        world.set_block([8, 112, 10], "air")
        world.set_block([8, 111, 10], "grass_block")
        self.assertEqual(self.retreat(world).error_code, "unsafe")
        self.assertFalse(world.writes())
        world = self.world()
        world.status["pos"][0] = 8.8
        self.assertEqual(self.retreat(world).error_code, "unsafe")
        self.assertFalse(world.writes())

    def test_upper_sweep_cannot_pass_through_head_obstruction(self):
        for pos in ([8, 115, 9], [8, 115, 10]):
            with self.subTest(pos=pos):
                world = self.world()
                world.set_block(pos, "oak_leaves")
                self.assertEqual(self.retreat(world).error_code, "unsafe")
                self.assertFalse(world.writes())

    def test_upper_leg_cannot_approach_enemy_hidden_by_safer_low_endpoint(self):
        world = self.world()
        world.entities = [{"id": 2, "type": "hostile", "name": "zombie", "pos": [9.1, 114, 10.5]}]
        result = self.retreat(world)
        self.assertEqual(result.error_code, "unsafe")
        self.assertEqual(result.data["origin_support"]["entry_rejections"]["drop_threat_proximity"], 1)
        self.assertFalse(world.writes())

    def test_fresh_hostile_scan_rejects_changed_upper_leg_threat(self):
        world = self.world()
        original = world.get_entities
        reads = 0

        def entities(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 2:
                world.entities = [{"id": 2, "type": "hostile", "name": "zombie", "pos": [9.1, 114, 10.5]}]
            return original(*args, **kwargs)

        world.get_entities = entities
        result = self.retreat(world)
        self.assertEqual(result.error_code, "unsafe")
        self.assertIn("hostile changed", result.message)
        self.assertFalse(world.writes())

    def test_source_lower_floor_and_sweep_are_reobserved_before_move(self):
        for changed in ([8, 113, 9], [8, 112, 10], [8, 115, 10]):
            for replacement in ("air", "lava", None):
                if changed == [8, 115, 10] and replacement == "air":
                    continue
                with self.subTest(changed=changed, replacement=replacement):
                    world = self.world()
                    original = world.get_block
                    reads = 0

                    def read(pos, **kwargs):
                        nonlocal reads
                        if pos == changed:
                            reads += 1
                            if reads > 1:
                                if replacement is None:
                                    raise APIError("not_loaded", "Support became unknown")
                                world.set_block(pos, replacement)
                        return original(pos, **kwargs)

                    world.get_block = read
                    result = self.retreat(world)
                    self.assertEqual(result.error_code, "unsafe")
                    self.assertFalse(world.writes())

    def test_changed_source_contact_height_grounding_or_health_prevents_write(self):
        for change in ({"pos": [8.6, 114, 10.3]}, {"pos": [8.6, 114.1, 10]},
                       {"on_ground": False}, {"health": 19}):
            with self.subTest(change=change):
                world = self.world()
                original = world.get_status
                reads = 0

                def status(**kwargs):
                    nonlocal reads
                    reads += 1
                    if reads == 4:
                        world.status.update(change)
                    return original(**kwargs)

                world.get_status = status
                result = self.retreat(world)
                self.assertIn(result.error_code, {"unsafe", "safety_changed"})
                self.assertFalse(world.writes())

    def test_wrong_landing_and_hurt_during_fall_do_not_trigger_more_actions(self):
        for changes in ({"pos": [8.5, 114, 10.5], "on_ground": True},
                        {"pos": [8.5, 113.5, 10.5], "on_ground": False, "health": 19}):
            with self.subTest(changes=changes):
                world = self.world()

                def move(pos, kwargs):
                    world.status.update(changes)
                    return {"ok": True, "result": {"pos": world.status["pos"]}}

                world.move_effect = move
                result = self.retreat(world)
                self.assertIn(result.error_code, {"retreat_unverified", "action_unconfirmed"})
                self.assertFalse(result.data.get("edge_drop", {}).get("landing_verified"))
                self.assertEqual([row[0] for row in world.writes()], ["move_to"])


if __name__ == "__main__":
    unittest.main()
