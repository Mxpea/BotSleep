import copy
import threading
import unittest
from unittest.mock import patch

from core.client import APIError
from skills.shelter import ShelterSkills


class ShelterClient:
    def __init__(self):
        self.status = {"ready": True, "alive": True, "health": 20, "food": 20,
                       "gamemode": "survival", "dimension": "overworld", "on_ground": True, "pos": [0.5, 64, 0.5]}
        self.blocks = {(x, 63, z): {"name": "grass_block", "properties": {}}
                       for x in range(-3, 4) for z in range(-4, 4)}
        self.items = {"cobblestone": 100}
        self.entities = []
        self.actions = []
        self.reads = 0
        self.after_read = None
        self.after_place = None
        self.consume = True
        self.place_actual = True

    def _read(self):
        self.reads += 1
        if self.after_read:
            self.after_read(self)

    def get_status(self, **kwargs):
        self._read()
        return {"ok": True, "result": copy.deepcopy(self.status)}

    def get_block(self, pos, **kwargs):
        self._read()
        result = copy.deepcopy(self.blocks.get(tuple(pos), {"name": "air", "properties": {}}))
        if result is None:
            raise APIError("not_loaded", "Block is not loaded")
        result["pos"] = list(pos)
        return {"ok": True, "result": result}

    def get_entities(self, **kwargs):
        self._read()
        return copy.deepcopy(self.entities)

    def inventory(self, **kwargs):
        self._read()
        return {"ok": True, "result": {"items": [{"name": name, "count": n} for name, n in self.items.items()]}}

    def place_block(self, x, y, z, item=None):
        if self.blocks.get((x, y, z), {}).get("name", "air") != "air":
            raise AssertionError("Fixture forbids overwriting an existing block")
        neighbors = [(x + dx, y + dy, z + dz) for dx, dy, dz in
                     ((0, -1, 0), (0, 1, 0), (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1))]
        if not any(self.blocks.get(pos, {}).get("name") in ShelterSkills.MATERIALS | ShelterSkills.NATURAL_GROUND for pos in neighbors):
            raise AssertionError("Fixture requires a real adjacent placement reference")
        self.actions.append(("place", [x, y, z], item))
        if self.place_actual:
            self.blocks[(x, y, z)] = {"name": item, "properties": {}}
        if self.consume:
            self.items[item] -= 1
        if self.after_place:
            self.after_place(self)
        return {"ok": True, "result": {"placed": True}}

    def stop(self):
        self.actions.append(("stop",))
        return {"ok": True, "result": {}}


class ShelterTests(unittest.TestCase):
    def setUp(self):
        self.client = ShelterClient()
        self.skills = ShelterSkills(self.client)
        self.args = {"pos": [0, 64, 0], "material": "cobblestone"}

    def run_skill(self, name="build_shelter_step", args=None, cancel=None):
        return self.skills.execute(name, self.args if args is None else args, cancel)

    def fill_structure(self, *, door=True, facing="north", opened=False):
        walls, roof, _ = self.skills._blueprint(self.args["pos"])
        for pos in walls + roof:
            self.client.blocks[pos] = {"name": "cobblestone", "properties": {}}
        if door:
            for dy, half in ((0, "lower"), (1, "upper")):
                self.client.blocks[(0, 64 + dy, -2)] = {"name": "oak_door", "properties": {
                    "half": half, "facing": facing, "open": opened, "hinge": "left"}}

    def test_plan_is_read_only_and_fixed_geometry_is_not_completed(self):
        result = self.run_skill("plan_shelter")
        self.assertTrue(result.success, result)
        self.assertEqual(self.client.actions, [])
        self.assertEqual(result.data["blueprint"]["total_building_blocks"], 55)
        self.assertEqual(result.data["missing_blocks"], 55)
        self.assertFalse(result.data["shelter_verified"])
        self.assertFalse(result.data["blueprint"]["natural_ownership_verified"])
        self.assertEqual(result.data["lighting"]["state"], "unknown")
        self.assertLessEqual(result.data["read_calls"], 150)
        self.assertEqual(result.data["block_reads"], 103)

    def test_plan_rejects_unknown_unsafe_floor_and_existing_objects(self):
        for pos, block in (((0, 63, 0), {"name": "air"}),
                           ((0, 63, 0), {"name": "oak_leaves"}),
                           ((0, 63, 0), {"name": "water"}),
                           ((0, 63, 0), {"name": "grass_block", "properties": {"waterlogged": True}}),
                           ((0, 64, 0), {"name": "chest"}), ((1, 66, 1), {"name": "oak_log"})):
            with self.subTest(pos=pos, block=block):
                self.client = ShelterClient()
                self.skills = ShelterSkills(self.client)
                self.client.blocks[pos] = block
                result = self.run_skill("plan_shelter")
                self.assertFalse(result.success)
                self.assertEqual(self.client.actions, [])

    def test_missing_block_is_unknown_not_air(self):
        self.client.blocks[(0, 65, 0)] = None
        result = self.run_skill("plan_shelter")
        self.assertEqual(result.error_code, "not_loaded")
        self.assertEqual(self.client.actions, [])

    def test_exit_needs_safe_floor_and_two_clear_cells(self):
        for pos in ((0, 63, -3), (0, 64, -3), (0, 65, -1)):
            with self.subTest(pos=pos):
                client = ShelterClient()
                client.blocks[pos] = {"name": "air" if pos[1] == 63 else "stone"}
                result = ShelterSkills(client).execute("plan_shelter", self.args)
                self.assertFalse(result.success)
                self.assertEqual(client.actions, [])

    def test_one_build_step_has_one_real_block_and_one_item_decrement(self):
        self.client.status["pos"] = [-0.5, 64, -0.5]
        result = self.run_skill()
        self.assertTrue(result.success, result)
        self.assertEqual(len(self.client.actions), 1)
        self.assertEqual(result.data["used"], 1)
        self.assertEqual(result.data["missing_blocks"], 54)
        self.assertTrue(result.data["executed"])
        self.assertFalse(result.data["shelter_verified"])
        self.assertEqual(self.client.items["cobblestone"], 99)
        self.assertLessEqual(result.data["read_calls"], 150)

    def test_build_needs_position_returns_candidates_and_never_moves(self):
        self.client.status["pos"] = [6, 64, 0]
        result = self.run_skill()
        self.assertEqual(result.error_code, "needs_position")
        self.assertTrue(result.data["needs_position"])
        self.assertTrue(result.data["stand_candidates"])
        self.assertFalse(result.data["return_route_verified"])
        self.assertEqual(self.client.actions, [])

    def test_existing_blueprint_block_is_never_overwritten_or_removed(self):
        walls, _, _ = self.skills._blueprint(self.args["pos"])
        self.client.blocks[walls[0]] = {"name": "chest", "properties": {}}
        result = self.run_skill()
        self.assertEqual(result.error_code, "unsafe_site")
        self.assertEqual(self.client.blocks[walls[0]]["name"], "chest")
        self.assertEqual(self.client.actions, [])

    def test_build_fifty_five_steps_closes_roof_but_still_requires_real_door(self):
        built = 0
        for _ in range(120):
            result = self.run_skill()
            if result.error_code == "needs_position":
                self.client.status["pos"] = result.data["stand_candidates"][0]
                continue
            self.assertTrue(result.success, result)
            self.assertLessEqual(result.data["read_calls"], 150)
            built += int(result.data["executed"])
            if result.data["missing_blocks"] == 0:
                break
        self.assertEqual(built, 55)
        self.assertEqual(self.client.items["cobblestone"], 45)
        self.assertTrue(result.data["geometry"]["walls_complete"])
        self.assertTrue(result.data["geometry"]["roof_complete"])
        self.assertFalse(result.data["geometry"]["door_complete"])
        self.assertFalse(result.data["shelter_verified"])
        self.assertEqual(result.data["needs_door"]["pos"], [0, 64, -2])
        self.assertNotIn((0, 64, -2), self.client.blocks)
        self.assertNotIn((0, 65, -2), self.client.blocks)

    def test_inspect_requires_closed_paired_correctly_oriented_door(self):
        for facing, opened, expected in (("north", False, True), ("south", "false", True),
                                          ("east", False, False), ("north", True, False)):
            with self.subTest(facing=facing, opened=opened):
                self.fill_structure(facing=facing, opened=opened)
                result = self.run_skill("inspect_shelter", {"pos": [0, 64, 0]})
                self.assertTrue(result.success)
                self.assertEqual(result.data["shelter_verified"], expected)
                self.assertEqual(self.client.actions, [])
        self.client.blocks[(0, 65, -2)]["properties"]["hinge"] = "right"
        self.assertFalse(self.run_skill("inspect_shelter", {"pos": [0, 64, 0]}).data["shelter_verified"])

    def test_verified_geometry_does_not_claim_spawn_protection_or_light(self):
        self.fill_structure()
        self.client.blocks[(1, 65, 0)] = {"name": "wall_torch", "properties": {}}
        result = self.run_skill("inspect_shelter", {"pos": [0, 64, 0]})
        self.assertTrue(result.data["shelter_verified"])
        self.assertEqual(result.data["protected_from_spawns"], "unknown")
        self.assertEqual(result.data["lighting"], {"state": "unknown", "torch_positions": [[1, 65, 0]]})

    def test_open_valid_door_preserves_structure_but_not_closed_protection(self):
        self.fill_structure(opened=True)
        result = self.run_skill("inspect_shelter", {"pos": [0, 64, 0]})
        self.assertTrue(result.success)
        self.assertTrue(result.data["structure_complete"])
        self.assertTrue(result.data["geometry"]["structure_complete"])
        self.assertTrue(result.data["door"]["frame_verified"])
        self.assertIs(result.data["door"]["open"], True)
        self.assertFalse(result.data["door"]["complete"])
        self.assertFalse(result.data["shelter_verified"])
        self.assertFalse(result.data["geometry_complete"])

    def test_unknown_or_disagreeing_door_open_state_is_not_a_verified_frame(self):
        self.fill_structure(opened=True)
        self.client.blocks[(0, 65, -2)]["properties"]["open"] = False
        self.assertFalse(self.run_skill("inspect_shelter", {"pos": [0, 64, 0]}).data["door"]["frame_verified"])
        self.client.blocks[(0, 65, -2)]["properties"].pop("open")
        self.assertFalse(self.run_skill("inspect_shelter", {"pos": [0, 64, 0]}).data["structure_complete"])

    def test_bed_requires_two_observed_halves_inside(self):
        self.fill_structure()
        foot = {"name": "red_bed", "properties": {"part": "foot", "facing": "south", "occupied": False}}
        head = {"name": "red_bed", "properties": {"part": "head", "facing": "south", "occupied": False}}
        self.client.blocks[(-1, 64, 0)] = foot
        self.assertEqual(self.run_skill("inspect_shelter", {"pos": [0, 64, 0]}).data["beds"], [])
        self.client.blocks[(-1, 64, 1)] = head
        result = self.run_skill("inspect_shelter", {"pos": [0, 64, 0]})
        self.assertEqual(len(result.data["beds"]), 1)
        self.assertTrue(result.data["beds"][0]["verified"])

    def test_own_body_and_other_player_prevent_placement(self):
        for other in (False, True):
            with self.subTest(other=other):
                client = ShelterClient()
                client.status["pos"] = [-0.5, 64, -0.5] if other else [-1.8, 64, -1.8]
                if other:
                    client.entities = [{"type": "player", "name": "Visitor", "pos": [-1.8, 64, -1.8]}]
                result = ShelterSkills(client).execute("build_shelter_step", self.args)
                self.assertEqual(result.error_code, "body_occupied")
                self.assertEqual(client.actions, [])

    def test_place_success_requires_both_world_change_and_consumption(self):
        for change_world, consume in ((False, True), (True, False)):
            with self.subTest(change_world=change_world, consume=consume):
                client = ShelterClient()
                client.status["pos"] = [-0.5, 64, -0.5]
                client.place_actual, client.consume = change_world, consume
                result = ShelterSkills(client).execute("build_shelter_step", self.args)
                self.assertEqual(result.error_code, "place_unverified")
                self.assertTrue(result.data["executed"])
                self.assertEqual(sum(a[0] == "place" for a in client.actions), 1)

    def test_read_timeouts_cancellation_and_life_change_cannot_write(self):
        for mode in ("cancel", "death", "dimension", "damage", "timeout"):
            with self.subTest(mode=mode):
                client = ShelterClient()
                cancel = threading.Event()
                def change(c):
                    if c.reads != 30:
                        return
                    if mode == "cancel": cancel.set()
                    elif mode == "death": c.status["last_death"] = {"t": 456, "pos": [0, 64, 0]}
                    elif mode == "dimension": c.status["dimension"] = "the_nether"
                    elif mode == "damage": c.status["health"] = 19
                    else: raise APIError("request_timeout", "Timed out reading a block")
                client.after_read = change
                result = ShelterSkills(client).execute("build_shelter_step", self.args, cancel)
                self.assertFalse(result.success)
                self.assertEqual(client.actions, [])

    def test_death_during_last_prewrite_checks_is_detected(self):
        self.client.status["pos"] = [-0.5, 64, -0.5]
        def change(c):
            if c.reads == 112:
                c.status["last_death"] = {"t": 789, "pos": [0, 64, 0]}
        self.client.after_read = change
        result = self.run_skill()
        self.assertEqual(result.error_code, "life_changed")
        self.assertEqual(self.client.actions, [])

    def test_late_visibility_change_blocks_the_write(self):
        self.client.status["pos"] = [-0.5, 64, -0.5]
        original = self.client.get_entities
        def change(**kwargs):
            entities = original(**kwargs)
            self.client.blocks[(-1, 65, -1)] = {"name": "stone", "properties": {}}
            return entities
        self.client.get_entities = change
        result = self.run_skill()
        self.assertEqual(result.error_code, "site_changed")
        self.assertEqual(self.client.actions, [])

    def test_airborne_or_wrong_height_builder_does_not_place(self):
        self.client.status["pos"] = [-0.5, 64, -0.5]
        self.client.status["on_ground"] = False
        self.assertEqual(self.run_skill().error_code, "unsafe_state")
        self.client.status["on_ground"] = True
        self.client.status["pos"][1] = 65
        self.assertEqual(self.run_skill().error_code, "needs_position")
        self.assertEqual(self.client.actions, [])

    def test_read_budget_and_time_budget_are_bounded(self):
        with patch.object(ShelterSkills, "MAX_READS", 20):
            result = self.run_skill("plan_shelter")
        self.assertEqual(result.error_code, "shelter_budget_exhausted")
        self.assertEqual(result.data["read_calls"], 20)
        with patch.object(ShelterSkills, "READ_BUDGET_SECONDS", 0):
            result = self.run_skill("plan_shelter")
        self.assertEqual(result.error_code, "shelter_budget_exhausted")
        self.assertEqual(self.client.actions, [])

    def test_missing_material_bad_arguments_and_creative_never_place(self):
        self.client.status["pos"] = [-0.5, 64, -0.5]
        self.client.items.clear()
        self.assertEqual(self.run_skill().error_code, "missing_item")
        for material in (None, [], "sand", "lava_bucket"):
            result = self.run_skill(args={"pos": [0, 64, 0], "material": material})
            self.assertFalse(result.success)
        self.client.status["gamemode"] = "creative"
        self.assertEqual(self.run_skill("plan_shelter").error_code, "unsafe_state")
        self.assertEqual(self.client.actions, [])


if __name__ == "__main__":
    unittest.main()
