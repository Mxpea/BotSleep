"""Known replaceable cover stays distinct from support and completed geometry."""

import copy
import unittest

from core.client import APIError
from skills.daily_survival import DailySurvivalSkills
from skills.shelter import ShelterSkills
from test_daily_skills import DailyWorld, envelope
from test_shelter import ShelterClient


class VegetationClient(ShelterClient):
    """Model only the two vanilla covers accepted by the original place API."""
    def place_block(self, x, y, z, item=None):
        key = (x, y, z)
        original = copy.deepcopy(self.blocks.get(key, {"name": "air"}))
        if item.endswith("_door"):
            for cell in (key, (x, y + 1, z)):
                assert self.blocks.get(cell, {"name": "air"})["name"] in {"air", "leaf_litter", "short_grass"}
            assert self.blocks[(x, y - 1, z)]["name"] in {"grass_block", "cobblestone"}
            self.actions.append(("place", list(key), item))
            if self.place_actual:
                for dy, half in ((0, "lower"), (1, "upper")):
                    self.blocks[(x, y + dy, z)] = {"name": item, "properties": {
                        "half": half, "facing": "north", "hinge": "left", "open": False}}
            if self.consume:
                self.items[item] -= 1
            if self.after_place:
                self.after_place(self)
            return envelope({"placed": item})
        if original["name"] in {"leaf_litter", "short_grass"}:
            self.blocks.pop(key, None)
        result = super().place_block(x, y, z, item=item)
        if not self.place_actual:
            self.blocks[key] = original
        return result


class ShelterVegetationTests(unittest.TestCase):
    def setUp(self):
        self.client = VegetationClient()
        self.skills = ShelterSkills(self.client)
        self.args = {"pos": [0, 64, 0], "material": "cobblestone"}

    def cover(self):
        for x in range(-3, 4):
            for z in range(-4, 4):
                self.client.blocks[(x, 64, z)] = {"name": "leaf_litter" if (x + z) % 2 else "short_grass",
                                                  "properties": {}}

    def test_plan_accepts_cover_but_keeps_all_missing_blocks_and_missing_door(self):
        self.cover()
        result = self.skills.execute("plan_shelter", self.args)
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["geometry"]["floor_verified"])
        self.assertTrue(result.data["geometry"]["exit_verified"])
        self.assertEqual(result.data["occupied_blueprint_cells"], [])
        self.assertEqual(result.data["missing_blocks"], 55)
        self.assertEqual(result.data["door"]["reason"], "door_missing")
        self.assertFalse(result.data["shelter_verified"])
        self.assertTrue(result.data["replaceable_vegetation"])
        self.assertEqual(self.client.actions, [])
        self.assertEqual(result.data["block_reads"], 103)

    def test_bad_floor_corner_rejects_before_scanning_walls_and_roof(self):
        self.client.blocks[(-2, 63, -2)] = {"name": "air"}
        result = self.skills.execute("plan_shelter", self.args)
        self.assertEqual(result.error_code, "unsafe_site")
        self.assertIs(result.data["geometry"]["floor_verified"], False)
        self.assertTrue(result.data["fresh"])
        self.assertEqual(result.data["block_reads"], 2)
        self.assertEqual(self.client.actions, [])

    def test_full_covered_site_builds_55_blocks_and_requires_actual_closed_door(self):
        self.cover()
        self.client.items["oak_door"] = 1
        for _ in range(120):
            result = self.skills.execute("build_shelter_step", self.args)
            if result.error_code == "needs_position":
                self.client.status["pos"] = result.data["stand_candidates"][0]
                continue
            self.assertTrue(result.success, result)
            self.assertLessEqual(result.data["read_calls"], 150)
            if result.data["missing_blocks"] == 0:
                break
        self.assertEqual(len(self.client.actions), 55)
        self.assertEqual(self.client.items["cobblestone"], 45)
        self.assertFalse(result.data["shelter_verified"])
        self.assertEqual(self.client.blocks[(0, 64, -2)]["name"], "short_grass")
        self.client.status["pos"] = [.5, 64, -3.5]
        door = DailySurvivalSkills(self.client).execute("place_door", {"pos": [0, 64, -2], "item": "oak_door"})
        self.assertTrue(door.success, door)
        self.assertEqual(len(self.client.actions), 56)
        self.assertEqual(self.client.items["oak_door"], 0)
        final = self.skills.execute("inspect_shelter", {"pos": [0, 64, 0]})
        self.assertTrue(final.data["shelter_verified"], final)
        self.assertFalse(final.data["door"]["open"])
        self.assertEqual(self.client.blocks[(0, 64, 0)]["name"], "short_grass")
        self.assertEqual(self.client.blocks[(0, 63, 0)]["name"], "grass_block")
        self.assertTrue(all(row[0] == "place" for row in self.client.actions))

    def test_unknown_colliding_and_other_empty_blocks_remain_obstacles(self):
        walls, _, _ = self.skills._blueprint([0, 64, 0])
        for name in ("chest", "oak_log", "oak_leaves", "snow", "moss_carpet", "vine", "torch", "unknown_plant"):
            with self.subTest(name=name):
                self.client.blocks[walls[0]] = {"name": name, "properties": {}}
                result = self.skills.execute("plan_shelter", self.args)
                self.assertFalse(result.success, result)
                self.assertEqual(self.client.actions, [])

    def test_cover_is_never_support_and_waterlogged_cover_is_unsafe(self):
        for pos, properties in (((0, 63, 0), {}), ((0, 64, -2), {"waterlogged": True})):
            with self.subTest(pos=pos):
                client = VegetationClient()
                client.blocks[pos] = {"name": "leaf_litter", "properties": properties}
                result = ShelterSkills(client).execute("plan_shelter", self.args)
                self.assertEqual(result.error_code, "unsafe_site")
                self.assertFalse(client.actions)

    def test_prewrite_target_or_corridor_change_does_not_overwrite_new_obstacle(self):
        for where in ("target", "corridor"):
            with self.subTest(where=where):
                client = VegetationClient()
                skills = ShelterSkills(client)
                walls, _, _ = skills._blueprint([0, 64, 0])
                target = walls[0]
                client.blocks[target] = {"name": "leaf_litter", "properties": {}}
                client.status["pos"] = [-.5, 64, -.5]
                inventory = client.inventory

                def changed(**kwargs):
                    result = inventory(**kwargs)
                    client.blocks[target if where == "target" else (0, 64, -1)] = {"name": "chest", "properties": {}}
                    return result

                client.inventory = changed
                result = skills.execute("build_shelter_step", self.args)
                self.assertEqual(result.error_code, "site_changed", result)
                self.assertFalse(client.actions)

    def test_placement_acknowledgement_does_not_prove_replacement_or_consumption(self):
        for actual, consume in ((False, True), (True, False)):
            with self.subTest(actual=actual, consume=consume):
                client = VegetationClient()
                skills = ShelterSkills(client)
                target = skills._blueprint([0, 64, 0])[0][0]
                client.blocks[target] = {"name": "leaf_litter", "properties": {}}
                client.status["pos"] = [-.5, 64, -.5]
                client.place_actual, client.consume = actual, consume
                result = skills.execute("build_shelter_step", self.args)
                self.assertEqual(result.error_code, "place_unverified", result)
                self.assertEqual(len([a for a in client.actions if a[0] == "place"]), 1)

    def test_disconnect_after_replacing_plant_keeps_unknown_without_cleanup_or_replay(self):
        self.cover()
        self.client.status["pos"] = [-.5, 64, -.5]
        place = self.client.place_block

        def lost(*args, **kwargs):
            place(*args, **kwargs)
            raise APIError("not_connected", "Placement acknowledgement lost")

        self.client.place_block = lost
        result = self.skills.execute("build_shelter_step", self.args)
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual([a[0] for a in self.client.actions], ["place"])


class DoorVegetationTests(unittest.TestCase):
    def world(self):
        world = DailyWorld()
        world.counts["oak_door"] = 1
        world.block([2, 64, 0], "leaf_litter")
        return world

    def place(self, world):
        return DailySurvivalSkills(world).execute("place_door", {"pos": [2, 64, 0], "item": "oak_door"})

    def test_replaceable_doorway_still_requires_closed_matching_halves_and_one_item(self):
        for fault in ("open", "half", "hinge", "facing", "item"):
            with self.subTest(fault=fault):
                world = self.world()

                def place():
                    for dy, half in ((0, "lower"), (1, "upper")):
                        world.block([2, 64 + dy, 0], "oak_door", properties={
                            "half": half, "facing": "south", "hinge": "left", "open": False})
                    world.counts["oak_door"] = 1 if fault == "item" else 0
                    props = world.blocks[(2, 65, 0)]["properties"]
                    if fault == "open": props["open"] = True
                    elif fault == "half": props["half"] = "lower"
                    elif fault == "hinge": props["hinge"] = "right"
                    elif fault == "facing": props["facing"] = "north"
                    return envelope({"placed": "oak_door"})

                world.effects["place"] = place
                result = self.place(world)
                self.assertEqual(result.error_code, "place_unverified", result)
                self.assertEqual(len([row for row in world.calls if row[0] == "place"]), 1)

    def test_doorway_hazard_unknown_and_real_obstacle_never_become_air(self):
        for name, props in (("water", {}), ("snow", {"layers": "1"}), ("chest", {}),
                            ("unknown_plant", {}), ("leaf_litter", {"waterlogged": True})):
            with self.subTest(name=name):
                world = self.world()
                world.block([2, 64, 0], name, properties=props)
                self.assertEqual(self.place(world).error_code, "unsafe")
                self.assertFalse(any(row[0] == "place" for row in world.calls))

    def test_door_write_unknown_remains_unknown_without_a_second_place(self):
        world = self.world()

        def lost():
            raise APIError("not_connected", "Door acknowledgement lost")

        world.effects["place"] = lost
        result = self.place(world)
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual([row[0] for row in world.calls if row[0] in {"place", "stop", "move"}], ["place"])


if __name__ == "__main__":
    unittest.main()
