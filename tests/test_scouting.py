"""Offline unit tests for 200-block forward low-stamina scouting skill (skills/scouting.py)."""

import copy
import math
import unittest

from core.client import APIError
from skills.scouting import SurfaceScoutSkill


class SimScoutWorld:
    """In-memory surface terrain for multi-hop forward scanning."""

    def __init__(self, *, pos=(0.5, 64.0, 0.5), food=20.0):
        self.status = {
            "ready": True,
            "alive": True,
            "busy": False,
            "dimension": "overworld",
            "health": 20.0,
            "food": float(food),
            "pos": list(pos),
        }
        self.world_blocks = []  # [{"name": ..., "pos": [x, y, z]}]
        self.entities = []
        self.food_drain_per_hop = 0.0
        self.blocked_moves = False
        self.moves = []
        self.find_calls = []

    def get_status(self, **kwargs):
        return {"ok": True, "result": copy.deepcopy(self.status)}

    def move_to(self, x, y=None, z=None, **kwargs):
        self.moves.append([float(x), float(self.status["pos"][1] if y is None else y), float(z)])
        if self.blocked_moves:
            raise APIError("path_not_found", "Water or cliff blocking forward path")
        self.status["pos"] = [float(x), float(self.status["pos"][1] if y is None else y), float(z)]
        if self.food_drain_per_hop > 0:
            self.status["food"] = max(0.0, self.status["food"] - self.food_drain_per_hop)
        return {"ok": True, "result": {"pos": list(self.status["pos"])}}

    def find_blocks(self, types, radius=48, limit=16, **kwargs):
        self.find_calls.append((list(self.status["pos"]), int(radius)))
        cur = self.status["pos"]
        allowed = set(types)
        matches = [
            copy.deepcopy(row)
            for row in self.world_blocks
            if row["name"] in allowed and math.dist(cur, row["pos"]) <= radius
        ]
        matches.sort(key=lambda r: math.dist(cur, r["pos"]))
        return matches[:limit]

    def get_entities(self, radius=48, types=None, **kwargs):
        cur = self.status["pos"]
        return [
            copy.deepcopy(ent)
            for ent in self.entities
            if math.dist(cur, ent["pos"]) <= radius
        ]


class SurfaceScoutSkillTests(unittest.TestCase):
    def test_forward_hops_reach_200_block_span_and_stop_cleanly_when_empty(self):
        world = SimScoutWorld(pos=(10.5, 64.0, 20.5))
        skill = SurfaceScoutSkill(world)
        res = skill.execute(scan_radius=48, hop_distance=55.0, max_hops=3)
        self.assertTrue(res.success, res)
        self.assertEqual(res.data["hops_completed"], 3)
        self.assertGreaterEqual(res.data["estimated_reach"], 190.0)
        self.assertTrue(res.data["aborted_empty"])
        self.assertEqual(res.data["early_stop_reason"], "completed_forward_span")
        # Spawn point stays at waypoints[0]; scout advances outward along one primary axis.
        self.assertEqual(res.data["waypoints"][0], [10.5, 64.0, 20.5])
        self.assertGreater(math.dist(res.data["origin"], res.data["final_pos"]), 150.0)

    def test_stops_immediately_upon_discovering_structure_or_oasis_log(self):
        # Origin chosen so heading is +x: 10+21 = 31 (31 % 4 == 3 -> (-1,0)), pick (0, 64, 0) -> (1, 0).
        world = SimScoutWorld(pos=(0.5, 64.0, 0.5))
        world.world_blocks = [{"name": "chest", "pos": [85, 64, 0]}]
        res = SurfaceScoutSkill(world).execute(scan_radius=48, hop_distance=55.0, max_hops=3)
        self.assertTrue(res.success, res)
        self.assertFalse(res.data["aborted_empty"])
        self.assertEqual(res.data["early_stop_reason"], "structure_or_village_found")
        self.assertEqual(res.data["hops_completed"], 1)  # Stopped after hop 1 at x=55.5
        self.assertEqual(res.data["found_structures"][0]["pos"], [85, 64, 0])

        desert = SimScoutWorld(pos=(0.5, 64.0, 0.5))
        desert.world_blocks = [
            {"name": "dead_bush", "pos": [4, 64, 0]},
            {"name": "acacia_log", "pos": [140, 65, 2]},
        ]
        res_wood = SurfaceScoutSkill(desert).execute(scan_radius=48, hop_distance=55.0, max_hops=3, prefer_wood=True)
        self.assertTrue(res_wood.success)
        self.assertEqual(res_wood.data["early_stop_reason"], "wood_found")
        self.assertEqual(res_wood.data["found_wood"][0]["name"], "acacia_log")
        self.assertEqual(res_wood.data["found_dead_bush"][0]["name"], "dead_bush")

    def test_aborts_decisively_when_stamina_drops_or_terrain_is_blocked(self):
        world = SimScoutWorld(pos=(0.5, 64.0, 0.5), food=20.0)
        world.food_drain_per_hop = 2.0
        res = SurfaceScoutSkill(world).execute(scan_radius=48, hop_distance=55.0, max_hops=3)
        self.assertTrue(res.success, res)
        self.assertEqual(res.data["early_stop_reason"], "stamina_conserved")
        self.assertEqual(res.data["hops_completed"], 1)

        blocked = SimScoutWorld(pos=(0.5, 64.0, 0.5))
        blocked.blocked_moves = True
        res_blk = SurfaceScoutSkill(blocked).execute(scan_radius=48, hop_distance=55.0, max_hops=3)
        self.assertTrue(res_blk.success, res_blk)
        self.assertEqual(res_blk.data["early_stop_reason"], "terrain_blocked_abort")
        self.assertEqual(res_blk.data["hops_completed"], 0)
        self.assertEqual(len(blocked.moves), 2)  # Tried primary + 1 rotated heading, then aborted

    def test_post_write_network_error_preserves_unknown_without_replay(self):
        world = SimScoutWorld(pos=(0.5, 64.0, 0.5))

        def disconnect(*args, **kwargs):
            raise APIError("request_timeout", "Lost connection while moving")

        world.move_to = disconnect
        res = SurfaceScoutSkill(world).execute(scan_radius=48, hop_distance=55.0, max_hops=3)
        self.assertFalse(res.success)
        self.assertEqual(res.error_code, "request_timeout")
        self.assertEqual(res.data["action_state"], "unknown")
        self.assertTrue(res.data["executed"])


if __name__ == "__main__":
    unittest.main()
