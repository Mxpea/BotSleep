"""Naturally grassed dirt remains structural evidence, never new site authority."""

import copy
import unittest

from core.daily_observation import DailyObservationCollector
from core.structure_spec import SHELTER_5X5_SPEC, inspect_structure, transform_rel_pos
from skills.shelter import ShelterSkills
from test_shelter import ShelterClient


class ShelterGrassWallTests(unittest.TestCase):
    ORIGIN = [0, 64, 0]

    def world(self):
        client = ShelterClient()
        client.status.update(busy=None, is_day=True, time_of_day=1000)
        client.items = {"dirt": 55, "oak_planks": 55}
        return client, ShelterSkills(client)

    def place_one(self, client, skills, material, orientation="north"):
        for _ in range(3):
            result = skills.execute("build_shelter_step", {
                "pos": self.ORIGIN, "material": material, "orientation": orientation})
            if result.error_code == "needs_position":
                client.status["pos"] = list(result.data["stand_candidates"][0])
                continue
            self.assertTrue(result.success, result)
            self.assertEqual(result.data["used"], 1)
            return result
        self.fail("Offline builder could not reach a supplied stand candidate")

    def home(self, client, orientation="north"):
        collector = DailyObservationCollector(client)
        collector.set_context({"base_project": {"pos": self.ORIGIN,
            "orientation": orientation, "dimension": "overworld", "material": "dirt"}})
        observation = {"status": copy.deepcopy(client.status), "daily_evidence": {}}
        collector._home(observation, None)
        return observation

    def test_twelve_real_placements_keep_base_and_resume_after_two_dirt_walls_grass(self):
        client, skills = self.world()
        self.assertTrue(skills.execute("plan_shelter", {
            "pos": self.ORIGIN, "material": "dirt", "orientation": "east"}).success)
        for index in range(12):
            self.place_one(client, skills, "dirt" if index < 7 else "oak_planks", "east")
        grown = [tuple(client.actions[index][1]) for index in (0, 6)]
        before = self.home(client, "east")
        self.assertEqual(before["shelter"]["missing_blocks"], 43)
        for pos in grown:
            client.blocks[pos] = {"name": "grass_block", "properties": {"snowy": False}}

        after = self.home(client, "east")
        self.assertTrue(after["daily_evidence"]["base_site"]["verified"])
        self.assertTrue(after["daily_evidence"]["base_site"]["fresh"])
        self.assertEqual(after["daily_evidence"]["base_site"]["orientation"], "east")
        self.assertEqual(after["shelter"]["missing_blocks"], 43)
        self.assertEqual(after["shelter"]["occupied_blueprint_cells"], [])
        self.assertFalse(after["shelter"]["shelter_verified"])
        self.assertEqual(len(client.actions), 12)  # Both observations are read-only.

        continued = self.place_one(client, skills, "oak_planks", "east")
        self.assertEqual(continued.data["missing_blocks"], 42)
        self.assertEqual(len(client.actions), 13)
        self.assertNotIn(tuple(continued.data["placed_pos"]), grown)
        self.assertTrue(all(client.blocks[pos]["name"] == "grass_block" for pos in grown))

    def test_all_rotations_accept_grassed_shell_but_still_require_a_closed_door(self):
        for orientation in ("north", "east", "south", "west"):
            with self.subTest(orientation=orientation):
                client, skills = self.world()
                walls, roof, doorway = skills._blueprint(self.ORIGIN, orientation)
                for pos in walls + roof:
                    client.blocks[pos] = {"name": "grass_block", "properties": {"snowy": False}}
                result = skills.execute("inspect_shelter", {"pos": self.ORIGIN, "orientation": orientation})
                self.assertTrue(result.success, result)
                self.assertEqual(result.data["missing_blocks"], 0)
                self.assertEqual(result.data["occupied_blueprint_cells"], [])
                self.assertTrue(result.data["geometry"]["walls_complete"])
                self.assertTrue(result.data["geometry"]["roof_complete"])
                self.assertFalse(result.data["shelter_verified"])
                for dy, half in ((0, "lower"), (1, "upper")):
                    client.blocks[(doorway[0], doorway[1] + dy, doorway[2])] = {
                        "name": "oak_door", "properties": {
                            "half": half, "facing": orientation, "hinge": "left", "open": False}}
                finished = skills.execute("inspect_shelter", {"pos": self.ORIGIN, "orientation": orientation})
                self.assertTrue(finished.data["shelter_verified"], finished)
                shared = inspect_structure(SHELTER_5X5_SPEC, self.ORIGIN, client.get_block, orientation=orientation)
                self.assertTrue(shared["complete"], shared)
                self.assertFalse(client.actions)

    def test_grass_is_not_an_authorized_inventory_building_material(self):
        client, skills = self.world()
        client.items["grass_block"] = 64
        for name in ("plan_shelter", "build_shelter_step"):
            result = skills.execute(name, {"pos": self.ORIGIN, "material": "grass_block"})
            self.assertEqual(result.error_code, "unsupported_item", result)
        self.assertNotIn("grass_block", skills.MATERIALS)
        self.assertFalse(client.actions)

    def test_natural_grass_in_any_above_ground_layer_does_not_authorize_a_new_site(self):
        client, skills = self.world()
        walls, roof, doorway = skills._blueprint(self.ORIGIN)
        for pos in (walls[0], roof[0], (0, 64, 0), doorway, (0, 64, -3)):
            with self.subTest(pos=pos):
                client.blocks[pos] = {"name": "grass_block", "properties": {"snowy": False}}
                result = skills.execute("plan_shelter", {"pos": self.ORIGIN, "material": "dirt"})
                self.assertFalse(result.success, result)
                self.assertIn(result.error_code, {"site_occupied", "unsafe_site"})
                self.assertFalse(client.actions)
                del client.blocks[pos]

    def test_grass_in_reserved_passage_or_door_never_counts_as_enclosure(self):
        for rel in ((0, 0, 1), (0, 0, 2), (0, 0, 3)):
            with self.subTest(rel=rel):
                client, skills = self.world()
                pos = tuple(transform_rel_pos(rel, self.ORIGIN, "north", integer=True))
                client.blocks[pos] = {"name": "grass_block", "properties": {"snowy": False}}
                result = skills.execute("inspect_shelter", {"pos": self.ORIGIN})
                self.assertTrue(result.success, result)
                self.assertFalse(result.data["geometry"]["exit_verified"])
                self.assertNotIn("base_site", self.home(client)["daily_evidence"])
                build = skills.execute("build_shelter_step", {"pos": self.ORIGIN, "material": "dirt"})
                self.assertEqual(build.error_code, "unsafe_site", build)
                self.assertFalse(client.actions)

    def test_unsafe_unknown_and_other_full_blocks_still_invalidate_base(self):
        for block in ({"name": "grass_block", "properties": {"waterlogged": True}},
                      {"name": "unknown", "properties": {}},
                      {"name": "moss_block", "properties": {}}, None):
            with self.subTest(block=block):
                client, skills = self.world()
                pos = skills._blueprint(self.ORIGIN)[0][0]
                client.blocks[pos] = block
                observed = self.home(client)
                self.assertNotIn("base_site", observed["daily_evidence"])
                if block is None:
                    self.assertEqual(observed["shelter_error"]["error_code"], "not_loaded")
                else:
                    self.assertIn(list(pos), observed["shelter"]["occupied_blueprint_cells"])
                self.assertFalse(client.actions)


if __name__ == "__main__":
    unittest.main()
