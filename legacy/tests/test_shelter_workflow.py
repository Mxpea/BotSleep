"""Offline workflow, rotation, and site-finding tests for refactored ShelterSkills."""

import copy
import unittest

from agent.daily_survival import DailySurvivalGoals
from agent.survival_planner import Decision
from core.client import APIError
from core.daily_observation import DailyObservationCollector
from core.daily_policy import DailySafetyPolicy
from core.structure_spec import (
    DEFAULT_REPLACEABLE,
    SHELTER_5X5_SPEC,
    evaluate_shelter_site,
    find_shelter_sites,
    transform_rel_pos,
)
from skills.batch import BatchSkills
from skills.shelter import ShelterSkills


class WorkflowShelterWorld:
    """In-memory Minecraft world mock supporting movement, digging, placement, and doors."""

    def __init__(self, *, bot_pos=(0.5, 64.0, -4.5)):
        self.status = {
            "ready": True,
            "alive": True,
            "busy": False,
            "health": 20,
            "food": 20,
            "gamemode": "survival",
            "dimension": "overworld",
            "on_ground": True,
            "in_water": False,
            "in_lava": False,
            "on_fire": False,
            "submerged": False,
            "pos": list(bot_pos),
            "yaw": 0.0,
            "pitch": 0.0,
        }
        self.blocks = {
            (x, 63, z): {"name": "grass_block", "properties": {}}
            for x in range(-14, 15)
            for z in range(-14, 15)
        }
        self.items = {"cobblestone": 128, "oak_door": 2, "iron_pickaxe": 1, "wooden_shovel": 1}
        self.entities = []
        self.actions = []
        self.place_failures = 0

    def get_status(self, **kwargs):
        return {"ok": True, "result": copy.deepcopy(self.status)}

    def inventory(self, **kwargs):
        return {
            "ok": True,
            "result": {
                "items": [
                    {"name": name, "count": count, "slot": idx}
                    for idx, (name, count) in enumerate(self.items.items())
                    if count > 0
                ]
            },
        }

    def get_entities(self, **kwargs):
        return copy.deepcopy(self.entities)

    def get_block(self, x, y=None, z=None, **kwargs):
        pos = x if y is None else (x, y, z)
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        blk = copy.deepcopy(self.blocks.get(key, {"name": "air", "properties": {}}))
        blk["pos"] = list(key)
        return {"ok": True, "result": blk}

    def get_blocks(self, **kwargs):
        return []

    def move_to(self, x, y, z, range=1.0, **kwargs):
        self.actions.append(("move_to", [x, y, z, range]))
        target_y = float(self.status["pos"][1]) if y is None else float(y)
        self.status["pos"] = [float(x), target_y, float(z)]
        return {"ok": True, "result": {"pos": list(self.status["pos"])}}

    def look_at(self, x, y, z, **kwargs):
        self.actions.append(("look_at", [x, y, z]))
        return {"ok": True, "result": {}}

    def equip(self, item, **kwargs):
        if self.items.get(item, 0) <= 0:
            return {"ok": False, "error": {"code": "missing_item", "message": f"No {item}"}}
        self.actions.append(("equip", item))
        return {"ok": True, "result": {"equipped": item}}

    def sneak(self, active=True, **kwargs):
        self.actions.append(("sneak", bool(active)))
        return {"ok": True, "result": {}}

    def dig(self, x, y, z, collect=False, **kwargs):
        key = (int(x), int(y), int(z))
        blk = self.blocks.get(key, {"name": "air"})
        if blk.get("name") == "air":
            return {"ok": False, "error": {"code": "target_empty", "message": "Air"}}
        self.actions.append(("dig", list(key), blk["name"]))
        self.blocks.pop(key, None)
        return {"ok": True, "result": {"dug": True}}

    def place_block(self, arg1=None, arg2=None, arg3=None, arg4=None, *, item=None, x=None, y=None, z=None, **kwargs):
        if isinstance(arg1, str) and item is None:
            item = arg1
            px, py, pz = (x if x is not None else arg2), (y if y is not None else arg3), (z if z is not None else arg4)
        else:
            px = x if x is not None else arg1
            py = y if y is not None else arg2
            pz = z if z is not None else arg3
            if item is None and isinstance(arg4, str):
                item = arg4
        key = (int(px), int(py), int(pz))
        if self.items.get(item, 0) <= 0:
            return {"ok": False, "error": {"code": "missing_item", "message": f"No {item}"}}
        if self.place_failures > 0:
            self.place_failures -= 1
            return {"ok": True, "result": {"placed": False}}
        cur_name = self.blocks.get(key, {"name": "air"}).get("name")
        if cur_name not in DEFAULT_REPLACEABLE:
            return {"ok": False, "error": {"code": "target_obstructed", "message": "Occupied"}}
        self.items[item] -= 1
        self.actions.append(("place", list(key), item))
        if item.endswith("_door"):
            # Determine facing from bot -> door vector along cardinal axis
            dx = (key[0] + 0.5) - float(self.status["pos"][0])
            dz = (key[2] + 0.5) - float(self.status["pos"][2])
            if abs(dx) >= abs(dz):
                facing = "east" if dx >= 0 else "west"
            else:
                facing = "south" if dz >= 0 else "north"
            self.blocks[key] = {
                "name": item,
                "properties": {"half": "lower", "facing": facing, "open": False, "hinge": "left"},
            }
            self.blocks[(key[0], key[1] + 1, key[2])] = {
                "name": item,
                "properties": {"half": "upper", "facing": facing, "open": False, "hinge": "left"},
            }
        else:
            self.blocks[key] = {"name": item, "properties": {}}
        return {"ok": True, "result": {"placed": True}}

    def interact(self, pos, **kwargs):
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        blk = self.blocks.get(key)
        if not blk or not str(blk.get("name", "")).endswith("_door"):
            return {"ok": False, "error": {"code": "cannot_interact", "message": "Not a door"}}
        new_open = not bool((blk.get("properties") or {}).get("open"))
        for cy in (key[1], key[1] + 1):
            if (key[0], cy, key[2]) in self.blocks:
                self.blocks[(key[0], cy, key[2])]["properties"]["open"] = new_open
        self.actions.append(("interact", list(key), new_open))
        return {"ok": True, "result": {"open": new_open}}

    def stop(self):
        self.actions.append(("stop",))
        return {"ok": True, "result": {}}


class ShelterWorkflowTests(unittest.TestCase):
    def test_four_orientations_rotate_door_stand_and_daily_policy(self):
        expected_door = {
            "north": (0, 64, -2),
            "south": (0, 64, 2),
            "east": (2, 64, 0),
            "west": (-2, 64, 0),
        }
        expected_stand = {
            "north": [0.5, 64.0, -2.5],
            "south": [0.5, 64.0, 3.5],
            "east": [3.5, 64.0, 0.5],
            "west": [-2.5, 64.0, 0.5],
        }
        for orient, door_pos in expected_door.items():
            with self.subTest(orientation=orient):
                world = WorkflowShelterWorld(bot_pos=expected_stand[orient])
                walls, roof, doorway = ShelterSkills._blueprint([0, 64, 0], orientation=orient)
                self.assertEqual(len(walls), 30)
                self.assertEqual(len(roof), 25)
                self.assertEqual(doorway, door_pos)
                res = ShelterSkills(world).execute(
                    "inspect_shelter", {"pos": [0, 64, 0], "orientation": orient}
                )
                self.assertTrue(res.success, res)
                self.assertEqual(res.data["orientation"], orient)
                self.assertEqual(res.data["needs_door"]["pos"], list(door_pos))
                self.assertEqual(res.data["needs_door"]["stand_pos"], expected_stand[orient])
                self.assertIn(orient, res.data["needs_door"]["allowed_facing"])

        # Daily policy accepts east-west door facings when orientation is 'east'
        policy = DailySafetyPolicy({"low_health_threshold": 8, "food_threshold": 16, "threat_radius": 8})
        baseline = Decision("wait", {"seconds": 5}, "Baseline idle", "shelter")
        obs = {
            "fresh": True,
            "counts": {},
            "entities": [],
            "terrain": [],
            "status": {
                "ready": True,
                "alive": True,
                "dimension": "overworld",
                "pos": [3.5, 64.0, 0.5],
                "on_ground": True,
                "health": 20,
                "food": 20,
                "is_day": False,
                "time_of_day": 13500,
                "thundering": False,
                "gamemode": "survival",
                "busy": None,
                "in_water": False,
                "in_lava": False,
                "on_fire": False,
            },
            "blocks": [],
            "daily_evidence": {
                "shelter": {
                    "fresh": True,
                    "verified": True,
                    "dimension": "overworld",
                    "pos": [0, 64, 0],
                    "orientation": "east",
                    "inside": False,
                    "structure_complete": True,
                    "geometry_complete": True,
                    "shelter_verified": False,
                    "geometry": {"structure_complete": True},
                    "door": {
                        "pos": [2, 64, 0],
                        "name": "oak_door",
                        "frame_verified": True,
                        "open": False,
                        "facing": "west",
                    },
                    "return_route": {
                        "fresh": True,
                        "dimension": "overworld",
                        "verified": False,
                        "all_other_body_and_support_verified": True,
                        "origin": [3.5, 64.0, 0.5],
                        "path": [[2, 64, 0], [1, 64, 0], [0, 64, 0]],
                        "blocked_door": {"pos": [2, 64, 0], "verified": True, "open": False},
                    },
                }
            },
        }
        decision = policy.choose(obs, baseline)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.name, "set_door")
        self.assertTrue(decision.args["open"])

        # End-to-end build_shelter succeeds cleanly in all four cardinal orientations
        for orient, door_pos in expected_door.items():
            with self.subTest(end_to_end_orientation=orient):
                w = WorkflowShelterWorld(bot_pos=expected_stand[orient])
                res = ShelterSkills(w).build_shelter(origin=[0, 64, 0], material="cobblestone", orientation=orient)
                self.assertTrue(res.success, res)
                self.assertTrue(res.data["structure_verified"])
                self.assertEqual(w.blocks[door_pos]["name"], "oak_door")
                self.assertFalse(w.blocks[door_pos]["properties"]["open"])

    def test_find_shelter_sites_prefers_flat_over_bumpy_and_rejects_chests_and_lava(self):
        world = WorkflowShelterWorld(bot_pos=(0.5, 64.0, 0.5))
        # Put chest right next to origin so candidate around [0, 64, 0] is protected/rejected
        world.blocks[(0, 64, 0)] = {"name": "chest", "properties": {}}
        # Put lava hazard on -x side
        world.blocks[(-8, 64, 0)] = {"name": "lava", "properties": {}}
        sites = ShelterSkills(world).find_shelter_sites(radius=7, max_results=3)
        self.assertTrue(sites)
        best = sites[0]
        # Must not pick a site whose clearance touches (0, 64, 0) chest or (-8, 64, 0) lava
        best_clear = {
            tuple(transform_rel_pos(rel, best["origin"], best["orientation"], integer=True))
            for rel in SHELTER_5X5_SPEC.clearance_rel
        }
        self.assertNotIn((0, 64, 0), best_clear)
        self.assertNotIn((-8, 64, 0), best_clear)

    def test_build_shelter_workflow_clears_obstacles_patches_floor_builds_55_and_installs_door(self):
        world = WorkflowShelterWorld(bot_pos=(0.5, 64.0, -7.5))
        # Add rough terrain on plot [0, 64, 0] ('east'): grass, grass_block hump, stone, leaves, and a 1-block shallow dip
        world.blocks[(2, 64, -2)] = {"name": "grass_block", "properties": {}}
        world.blocks[(2, 65, -2)] = {"name": "short_grass", "properties": {}}
        world.blocks[(0, 64, 0)] = {"name": "stone", "properties": {}}
        world.blocks[(1, 66, 1)] = {"name": "oak_leaves", "properties": {}}
        world.blocks.pop((-1, 63, -1), None)
        world.blocks[(-1, 62, -1)] = {"name": "stone", "properties": {}}

        skills = ShelterSkills(world)
        result = skills.build_shelter(
            origin=[0, 64, 0],
            material="cobblestone",
            orientation="east",
            clear_obstacles=True,
            patch_floor=True,
            auto_door=True,
            close_door=True,
            finish_inside=True,
        )
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["structure_verified"])
        self.assertTrue(result.data["structure_report"]["complete"])
        self.assertTrue(result.data["structure_report"]["door"]["complete"])
        # Verify patched floor, east door closed, and bot settled inside [0.5, 64, 0.5]
        self.assertEqual(world.blocks[(-1, 63, -1)]["name"], "cobblestone")
        self.assertEqual(world.blocks[(2, 64, 0)]["name"], "oak_door")
        self.assertFalse(world.blocks[(2, 64, 0)]["properties"]["open"])
        self.assertGreater(len([a for a in world.actions if a[0] == "dig"]), 0)
        # 55 shell blocks + 1 patched floor block = 56 cobblestone consumed
        self.assertEqual(world.items["cobblestone"], 128 - 56)
        self.assertEqual(world.items["oak_door"], 1)

        # Inspect via DailyObservationCollector using same east orientation
        collector = DailyObservationCollector(world)
        collector.set_context({"base_project": {"pos": [0, 64, 0], "dimension": "overworld", "orientation": "east"}})
        obs = collector.collect()
        self.assertIn("shelter", obs["daily_evidence"])
        self.assertTrue(obs["daily_evidence"]["shelter"]["shelter_verified"])
        self.assertTrue(obs["daily_evidence"]["shelter"]["inside"])

        # DailySurvivalGoals forwards non-north orientation cleanly when resuming an incomplete shelter
        goals = DailySurvivalGoals()
        obs_incomplete = copy.deepcopy(obs)
        obs_incomplete.pop("shelter", None)
        obs_incomplete["daily_evidence"].pop("shelter", None)
        action = goals._shelter_action(
            obs_incomplete,
            {"cobblestone": 10},
            {"pos": [0, 64, 0], "dimension": "overworld", "orientation": "east", "verified": True, "fresh": True},
            {},
        )
        self.assertIsNotNone(action)
        self.assertEqual(action["args"].get("orientation"), "east")

    def test_build_shelter_auto_finds_site_when_origin_omitted_and_handles_missing_items(self):
        world = WorkflowShelterWorld(bot_pos=(0.5, 64.0, -4.5))
        skills = ShelterSkills(world)
        res = skills.build_shelter(material="cobblestone", radius=6)
        self.assertTrue(res.success, res)
        self.assertTrue(res.data["structure_verified"])

        # Missing door triggers deterministic precheck failure without writes
        world_no_door = WorkflowShelterWorld(bot_pos=(0.5, 64.0, -4.5))
        world_no_door.items.pop("oak_door")
        res_no_door = ShelterSkills(world_no_door).build_shelter(origin=[0, 64, 0], material="cobblestone")
        self.assertEqual(res_no_door.error_code, "missing_door")
        self.assertFalse(res_no_door.data["executed"])
        self.assertEqual(world_no_door.actions, [])

    def test_build_shelter_uncertain_write_stops_immediately_without_retry(self):
        world = WorkflowShelterWorld(bot_pos=(0.5, 64.0, -4.5))

        def timeout_place(*args, **kwargs):
            raise APIError("request_timeout", "Lost connection during place")

        world.place_block = timeout_place
        res = ShelterSkills(world).build_shelter(origin=[0, 64, 0], material="cobblestone", orientation="north")
        self.assertFalse(res.success)
        self.assertEqual(res.error_code, "request_timeout")
        self.assertEqual(res.data["action_state"], "unknown")

    def test_batch_skills_supports_orientation_parameter(self):
        world = WorkflowShelterWorld(bot_pos=(0.5, 64.0, 0.5))
        world.status.update({"is_day": True, "thundering": False, "time_of_day": 5000})
        res = BatchSkills(world).execute(
            "build_shelter_batch",
            {"pos": [0, 64, 0], "material": "cobblestone", "orientation": "south", "max_blocks": 2},
        )
        self.assertTrue(res.success, res)
        self.assertEqual(res.data["orientation"], "south")
        self.assertEqual(res.data["completed_blocks"], 2)


if __name__ == "__main__":
    unittest.main()
