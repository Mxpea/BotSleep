"""Offline tests for VoxelWorld3D, Pathfinder3D, anchored bridging, and multi-branch cave exploration."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from core.cave_probe import flood_safe_cave_graph
from core.pathfinder_3d import Pathfinder3D
from core.structure_spec import MINE_SHAFT_SPEC, ORE_DROP_MAP
from core.world_3d import FrontierNode, OreNode, VoxelWorld3D
from skills.action_workflow import WorkflowContext, WorkflowGuard
from skills.cave_explore import CaveExploreSkill
from skills.ore_harvester import OreHarvester
from skills.world_navigator import WorldNavigator


class _Sim3DCaveClient:
    """Minimal stateful cave simulator for testing VoxelWorld3D + CaveExploreSkill."""

    def __init__(self, *, bot_pos=(0.5, 59.0, 0.5), default="stone"):
        self.blocks = {}
        self.default = default
        self.ground_drops = {}
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
            "pos": list(bot_pos),
            "held": {"name": "iron_pickaxe", "count": 1},
        }
        self.items = [
            {"name": "iron_pickaxe", "count": 1, "slot": 0},
            {"name": "torch", "count": 24, "slot": 1},
            {"name": "cobblestone", "count": 48, "slot": 2},
        ]
        self.actions = []

    def carve(self, xs, ys, zs, name="air"):
        for x in xs:
            for y in ys:
                for z in zs:
                    self.blocks[(x, y, z)] = {"name": name, "properties": {}}

    def set_block(self, x, y, z, name):
        self.blocks[(x, y, z)] = {"name": name, "properties": {}}

    def reader(self, pos):
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        return dict(self.blocks.get(key) or {"name": self.default, "properties": {}})

    def _add_item(self, name, count=1):
        for row in self.items:
            if row["name"] == name:
                row["count"] += count
                return
        used = {r.get("slot") for r in self.items if isinstance(r.get("slot"), int)}
        nxt = next((i for i in range(36) if i not in used), len(self.items))
        self.items.append({"name": name, "count": count, "slot": nxt})

    def _dec_item(self, name, count=1):
        for idx, row in enumerate(self.items):
            if row["name"] == name and row["count"] > 0:
                row["count"] -= count
                if row["count"] <= 0:
                    self.items.pop(idx)
                return True
        return False

    def _pickup_near(self, pos):
        px, py, pz = int(pos[0]), int(pos[1]), int(pos[2])
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    bag = self.ground_drops.pop((px + dx, py + dy, pz + dz), None)
                    if bag:
                        for item_name, cnt in bag.items():
                            self._add_item(item_name, cnt)

    def get_status(self, **kwargs):
        return {"ok": True, "result": copy.deepcopy(self.status)}

    def inventory(self, **kwargs):
        counts = {}
        for row in self.items:
            counts[row["name"]] = counts.get(row["name"], 0) + row["count"]
        return {"ok": True, "result": {"counts": counts, "items": copy.deepcopy(self.items)}}

    def get_inventory_counts(self):
        return self.inventory()["result"]["counts"]

    def get_entities(self, **kwargs):
        return []

    def get_events(self, **kwargs):
        return []

    def get_block(self, x, y=None, z=None, **kwargs):
        pos = x if y is None else (x, y, z)
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        blk = copy.deepcopy(self.blocks.get(key) or {"name": self.default, "properties": {}})
        blk["pos"] = list(key)
        return {"ok": True, "result": blk}

    def get_blocks(self, radius=16, types=None, limit=64, **kwargs):
        bx, by, bz = [int(self.status["pos"][i] // 1) for i in range(3)]
        type_set = set(types) if types else None
        out = []
        for (px, py, pz), blk in self.blocks.items():
            if max(abs(px - bx), abs(py - by), abs(pz - bz)) > int(radius):
                continue
            bname = blk.get("name", self.default)
            if type_set is not None and bname not in type_set:
                continue
            out.append({"name": bname, "pos": [px, py, pz], "dist": abs(px - bx) + abs(py - by) + abs(pz - bz)})
        return out[: int(limit)]

    def look_at(self, x, y, z, **kwargs):
        return {"ok": True, "result": {}}

    def equip(self, item, **kwargs):
        if self.get_inventory_counts().get(item, 0) <= 0:
            return {"ok": False, "error": {"code": "missing_item", "message": f"No {item}"}}
        self.status["held"] = {"name": item, "count": self.get_inventory_counts()[item]}
        return {"ok": True, "result": {"equipped": item}}

    def posture(self, sneak=False, shield=False, jump=False, **kwargs):
        self.status["jumping"] = bool(jump)
        if jump:
            bx, by, bz = [int(self.status["pos"][i] // 1) for i in range(3)]
            above2 = self.reader((bx, by + 2, bz))["name"]
            if above2 in {"air", "torch", "wall_torch"}:
                self.status["pos"][1] = float(by + 1.15)
        else:
            bx, by, bz = [int(self.status["pos"][i] // 1) for i in range(3)]
            below = self.reader((bx, by - 1, bz))["name"]
            if below in {"air", "torch", "wall_torch"} and self.status["pos"][1] > 0:
                self.status["pos"][1] = float(by - 1)
            else:
                self.status["pos"][1] = float(by)
        return {"ok": True, "result": {"sneaking": bool(sneak), "jumping": bool(jump)}}

    def move_to(self, x, y, z, **kwargs):
        ty = float(self.status["pos"][1]) if y is None else float(y)
        self.status["pos"] = [float(x), ty, float(z)]
        self._pickup_near((int(float(x) // 1), int(ty // 1), int(float(z) // 1)))
        self.actions.append(("move_to", [x, y, z]))
        return {"ok": True, "result": {"pos": list(self.status["pos"])}}

    def dig(self, x, y, z, collect=False, **kwargs):
        key = (int(x), int(y), int(z))
        bname = self.reader(key)["name"]
        if bname == "air":
            return {"ok": False, "error": {"code": "target_empty", "message": "Air"}}
        self.blocks[key] = {"name": "air", "properties": {}}
        drop = ORE_DROP_MAP.get(bname, "cobblestone" if bname == "stone" else None)
        if drop:
            if collect:
                self._add_item(drop, 1)
            else:
                dy_pos = key[1]
                while dy_pos > -10:
                    below = self.reader((key[0], dy_pos - 1, key[2]))["name"]
                    if below not in {"air", "torch", "wall_torch"}:
                        break
                    dy_pos -= 1
                bag = self.ground_drops.setdefault((key[0], dy_pos, key[2]), {})
                bag[drop] = bag.get(drop, 0) + 1
                bot_cell = (int(self.status["pos"][0] // 1), int(self.status["pos"][1] // 1), int(self.status["pos"][2] // 1))
                self._pickup_near(bot_cell)
        self.actions.append(("dig", list(key), bname, bool(collect)))
        return {"ok": True, "result": {"dug": bname}}

    def place_block(self, arg1=None, arg2=None, arg3=None, arg4=None, *, item=None, x=None, y=None, z=None, **kwargs):
        if isinstance(arg1, str) and item is None:
            item = arg1
            px, py, pz = (x if x is not None else arg2), (y if y is not None else arg3), (z if z is not None else arg4)
        else:
            px, py, pz = (x if x is not None else arg1), (y if y is not None else arg2), (z if z is not None else arg3)
        key = (int(px), int(py), int(pz))
        if not self._dec_item(item, 1):
            return {"ok": False, "error": {"code": "missing_item", "message": f"No {item}"}}
        placed_name = item
        if item == "torch":
            below = self.reader((key[0], key[1] - 1, key[2]))["name"]
            placed_name = "torch" if below not in {"air", "torch", "wall_torch"} else "wall_torch"
        self.blocks[key] = {"name": placed_name, "properties": {}}
        self.actions.append(("place", list(key), placed_name))
        return {"ok": True, "result": {"placed": placed_name}}


class VoxelWorld3DTests(unittest.TestCase):
    def test_voxel_world_3d_sqlite_roundtrip_pruning_and_blocked_edges(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "world_3d.sqlite3"
            world = VoxelWorld3D(db_path, scope="test:overworld")
            for dx in range(-4, 5):
                for dz in range(-4, 5):
                    world.set_block((dx, 58, dz), {"name": "stone", "properties": {}})
                    world.set_block((dx, 59, dz), {"name": "air", "properties": {}})
                    world.set_block((dx, 60, dz), {"name": "air", "properties": {}})
            world.open_frontiers[(1, 59, 0)] = FrontierNode(pos=(1, 59, 0), unseen_neighbors=3, clearance=2)
            world.open_frontiers[(4, 59, 0)] = FrontierNode(pos=(4, 59, 0), unseen_neighbors=4, clearance=2)
            world.record_stance((0, 59, 0), cover_radius=2)
            # (1, 59, 0) is within cover_radius=2 of (0, 59, 0) so it must be pruned; (4, 59, 0) remains.
            self.assertNotIn((1, 59, 0), world.open_frontiers)
            self.assertIn((4, 59, 0), world.open_frontiers)

            world.record_blocked_edge((0, 59, 0), (0, 59, 1))
            self.assertTrue(world.is_edge_blocked((0, 59, 0), (0, 59, 1)))
            world.ore_registry[(4, 60, 0)] = OreNode(pos=(4, 60, 0), name="iron_ore", status="pending")
            world.save()

            reloaded = VoxelWorld3D(db_path, scope="test:overworld", auto_load=True)
            self.assertEqual(reloaded.get_block_cached((4, 58, 0))["name"], "stone")
            self.assertEqual(reloaded.visited_stances.get((0, 59, 0)), 1)
            self.assertIn((4, 59, 0), reloaded.open_frontiers)
            self.assertNotIn((1, 59, 0), reloaded.open_frontiers)
            self.assertTrue(reloaded.is_edge_blocked((0, 59, 0), (0, 59, 1)))
            self.assertIn((4, 60, 0), reloaded.ore_registry)
            self.assertEqual(reloaded.ore_registry[(4, 60, 0)].name, "iron_ore")

    def test_pathfinder_3d_astar_avoids_blocked_edge_and_climbs_two_block_ledge(self):
        sim = _Sim3DCaveClient(bot_pos=(0.5, 59.0, 0.5))
        # Direct corridor x=0..4, z=0 and bypass loop via z=1..2
        sim.carve(range(-1, 6), (59, 60, 61, 62), range(-1, 4), "air")
        for x in range(-1, 6):
            for z in range(-1, 4):
                sim.set_block(x, 58, z, "stone")
        # 2-block high ledge at x=4..5: floor moves from y=58 to y=60, feet y=61
        for z in range(-1, 4):
            sim.set_block(4, 59, z, "stone")
            sim.set_block(4, 60, z, "stone")
            sim.set_block(5, 59, z, "stone")
            sim.set_block(5, 60, z, "stone")

        world = VoxelWorld3D()
        # Block the direct straight edge (0,59,0) -> (1,59,0) so 3D A* must detour via z=1
        world.record_blocked_edge((0, 59, 0), (1, 59, 0))

        path = Pathfinder3D.find_path(
            world,
            (0, 59, 0),
            (4, 61, 0),
            sim.reader,
            allow_pillar=True,
            max_pillar_height=4,
            allow_dig=False,
            allow_bridge=False,
        )
        self.assertGreater(len(path), 0)
        self.assertEqual(tuple(path[-1]["to"]), (4, 61, 0))
        edges = [(tuple(s["from"]), tuple(s["to"])) for s in path]
        self.assertNotIn(((0, 59, 0), (1, 59, 0)), edges)
        modes = [s["mode"] for s in path]
        self.assertIn("pillar", modes)

        # Also check 1-block step up uses walk cleanly without pillar
        sim_step = _Sim3DCaveClient(bot_pos=(0.5, 59.0, 0.5))
        sim_step.carve(range(-1, 4), (59, 60, 61, 62), (0,), "air")
        for x in range(-1, 4):
            sim_step.set_block(x, 58, 0, "stone")
        sim_step.set_block(2, 59, 0, "stone")
        sim_step.set_block(3, 59, 0, "stone")
        path_walk = Pathfinder3D.find_path(
            VoxelWorld3D(),
            (0, 59, 0),
            (3, 60, 0),
            sim_step.reader,
            allow_pillar=True,
            allow_dig=False,
            allow_bridge=False,
        )
        self.assertEqual([s["mode"] for s in path_walk], ["walk", "walk", "walk"])

    def test_anchored_bridge_vs_open_void_no_skybridge(self):
        # 1) 2-block lava gap with opposite stone platform at x=3..4 MUST allow anchored bridge
        sim_bridge = _Sim3DCaveClient(bot_pos=(0.5, 59.0, 0.5))
        sim_bridge.carve(range(-1, 6), (59, 60, 61), (0,), "air")
        sim_bridge.set_block(0, 58, 0, "stone")
        sim_bridge.set_block(1, 58, 0, "lava")
        sim_bridge.set_block(2, 58, 0, "lava")
        sim_bridge.set_block(3, 58, 0, "stone")
        sim_bridge.set_block(4, 58, 0, "stone")

        world_b = VoxelWorld3D()
        path_b = Pathfinder3D.find_path(
            world_b,
            (0, 59, 0),
            (4, 59, 0),
            sim_bridge.reader,
            allow_bridge=True,
            max_bridge_steps=4,
            allow_dig=False,
            allow_pillar=False,
        )
        self.assertGreater(len(path_b), 0)
        self.assertEqual(tuple(path_b[-1]["to"]), (4, 59, 0))
        self.assertIn("bridge", [s["mode"] for s in path_b])

        # 2) Open air void with NO opposite solid floor at x=1..7 MUST emit zero bridges or mid-air frontiers
        sim_void = _Sim3DCaveClient(bot_pos=(0.5, 59.0, 0.5))
        sim_void.carve(range(1, 9), range(45, 63), range(-2, 3), "air")
        sim_void.carve((0,), (59, 60, 61), (0,), "air")
        sim_void.set_block(0, 58, 0, "stone")

        graph = flood_safe_cave_graph(
            (0, 59, 0),
            sim_void.reader,
            max_nodes=80,
            max_radius=8,
            allow_bridge=True,
            max_bridge_steps=4,
        )
        bridge_nodes = [
            cell
            for cell, meta in (graph.get("edge_meta") or {}).items()
            if meta.get("mode") == "bridge"
        ]
        self.assertEqual(bridge_nodes, [])
        world_v = VoxelWorld3D()
        Pathfinder3D.plan_horizon(
            world_v,
            (0, 59, 0),
            sim_void.reader,
            max_nodes=80,
            max_radius=8,
            allow_bridge=True,
            max_bridge_steps=4,
        )
        self.assertEqual([fn for fn in world_v.open_frontiers.values() if fn.requires_op == "bridge"], [])
        for fn in world_v.open_frontiers.values():
            fx, fy, fz = fn.pos
            self.assertNotEqual(sim_void.reader((fx, fy - 1, fz))["name"], "air")

    def test_multi_branch_cavern_exhausts_both_arms_via_world_3d_frontiers(self):
        sim = _Sim3DCaveClient(bot_pos=(0.5, 59.0, 0.5))
        # Entry & junction around x=-1..3, z=-1..1
        sim.carve(range(-1, 4), (59, 60, 61), range(-1, 2), "air")
        for x in range(-1, 4):
            for z in range(-1, 2):
                sim.set_block(x, 58, z, "stone")

        # East branch x=4..12, z=0 ending at 2 iron_ore blocks
        sim.carve(range(4, 13), (59, 60, 61), (0,), "air")
        for x in range(4, 13):
            sim.set_block(x, 58, 0, "stone")
        sim.set_block(13, 59, 0, "iron_ore")
        sim.set_block(13, 60, 0, "iron_ore")

        # North branch z=-2..-12, x=2 ending at 1 diamond_ore block
        sim.carve((2,), (59, 60, 61), range(-12, -1), "air")
        for z in range(-12, -1):
            sim.set_block(2, 58, z, "stone")
        sim.set_block(2, 59, -13, "diamond_ore")

        world = VoxelWorld3D()
        skill = CaveExploreSkill(sim, world=world)
        res = skill.explore_cave(
            entry_pos=[0, 59, 0],
            origin_air=[1, 59, 0],
            targets={
                "iron": {"count": 2, "min_tool": "stone_pickaxe"},
                "diamond": {"count": 1, "min_tool": "iron_pickaxe"},
            },
            cave_policy={"explore_max_visited": 120, "explore_radius": 24, "torch_interval": 24},
            allow_pillar=False,
            allow_bridge=False,
        )
        self.assertTrue(res.success, msg=res.message)
        harvested = res.data.get("harvested") or {}
        self.assertGreaterEqual(harvested.get("raw_iron", 0), 2)
        self.assertGreaterEqual(harvested.get("diamond", 0), 1)
        self.assertIn("world_stats", res.data)
        self.assertGreater(res.data["world_stats"]["voxels"], 20)
        # Returned near entry after clearing both arms
        end_pos = sim.status["pos"]
        self.assertLessEqual(abs(end_pos[0] - 0.5) + abs(end_pos[2] - 0.5), 3.0)


class _StrictPickupSim(_Sim3DCaveClient):
    """Strict simulator requiring 2-high standable air and close proximity for pickup."""

    def _pickup_near(self, pos):
        px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
        for key in list(self.ground_drops):
            gx, gy, gz = key
            if abs(px - (gx + 0.5)) <= 0.72 and abs(py - gy) <= 0.65 and abs(pz - (gz + 0.5)) <= 0.72:
                bag = self.ground_drops.pop(key)
                for item_name, cnt in bag.items():
                    self._add_item(item_name, cnt)

    def dig(self, x, y, z, collect=False, **kwargs):
        res = super().dig(x, y, z, collect=collect, **kwargs)
        # Keep dropped item gravity-settled cleanly even if a supporting block underneath is dug
        if res.get("ok"):
            for dkey in list(self.ground_drops):
                dx, dy, dz = dkey
                ny = dy
                while ny > -10 and self.reader((dx, ny - 1, dz))["name"] in {"air", "torch", "wall_torch"}:
                    ny -= 1
                if ny != dy:
                    bag = self.ground_drops.pop(dkey)
                    dst = self.ground_drops.setdefault((dx, ny, dz), {})
                    for k, v in bag.items():
                        dst[k] = dst.get(k, 0) + v
            self._pickup_near(self.status["pos"])
        return res

    def move_to(self, x, y, z, **kwargs):
        tx, ty, tz = int(float(x) // 1), int(round(float(y) if y is not None else self.status["pos"][1])), int(float(z) // 1)
        cur_x, cur_y, cur_z = [int(self.status["pos"][i] // 1) for i in range(3)]
        if self.reader((tx, ty, tz))["name"] != "air" or self.reader((tx, ty + 1, tz))["name"] != "air":
            return {"ok": False, "error": {"code": "path_not_found", "message": "not 2-high standable"}}
        if self.reader((tx, ty - 1, tz))["name"] == "air":
            return {"ok": False, "error": {"code": "path_not_found", "message": "no solid floor"}}
        # Reject climbing >=2 blocks UP without pillar_up, multi-block vertical drop-hops, or corner hops through wall
        if (ty - cur_y >= 2) or (abs(ty - cur_y) >= 2 and abs(tx - cur_x) + abs(tz - cur_z) > 1):
            return {"ok": False, "error": {"code": "path_not_found", "message": "vertical jump blocked"}}
        if cur_x == 0 and cur_z == 0 and tx == 1 and tz == 1 and self.reader((1, ty, 0))["name"] != "air":
            return {"ok": False, "error": {"code": "unreachable_stance", "message": "corner blocked"}}
        self.status["pos"] = [float(x), float(ty), float(z)]
        self._pickup_near(self.status["pos"])
        self.actions.append(("move_to", [float(x), float(ty), float(z)]))
        return {"ok": True, "result": {"pos": list(self.status["pos"])}}


def _make_harvester_ctx(sim):
    guard = WorkflowGuard(sim, timeout_seconds=30.0, max_reads=5000, allow_damage=True)
    return WorkflowContext(guard=guard, spec=MINE_SHAFT_SPEC, origin=[0, 59, 0], orientation="south", extra={})


class DropCollectionAndStrideTests(unittest.TestCase):
    def test_collect_mined_drop_handles_one_high_pocket_digging_headroom(self):
        sim = _StrictPickupSim(bot_pos=(0.5, 59.0, 0.5))
        sim.carve((0, 1, 2), (59, 60, 61), (0,), "air")
        for x in (0, 1, 2, 3):
            sim.set_block(x, 58, 0, "stone")
        # 1-high pocket at x=3, y=59: (3,59,0) is air (just dug), (3,60,0) is stone ceiling, (3,61,0) is stone roof
        sim.set_block(3, 59, 0, "air")
        sim.set_block(3, 60, 0, "stone")
        sim.set_block(3, 61, 0, "stone")
        sim.ground_drops[(3, 59, 0)] = {"raw_iron": 1}

        world = VoxelWorld3D()
        ctx = _make_harvester_ctx(sim)
        before = sim.get_inventory_counts()
        after = OreHarvester.collect_mined_drop(
            ctx,
            world,
            (3, 59, 0),
            (0, 59, 0),
            "raw_iron",
            before,
            sim.reader,
        )
        self.assertIsNotNone(after)
        self.assertEqual(sim.get_inventory_counts().get("raw_iron", 0), before.get("raw_iron", 0) + 1)
        self.assertEqual(sim.reader((3, 60, 0))["name"], "air")

    def test_collect_mined_drop_handles_two_block_well_and_pillars_out(self):
        sim = _StrictPickupSim(bot_pos=(0.5, 59.0, 0.5))
        sim.carve((0, 1, 2, 3), (59, 60, 61), (0,), "air")
        for x in (0, 1, 3):
            sim.set_block(x, 58, 0, "stone")
        # 2-block deep 1x1 well at x=2: bottom floor at y=56, drop settles at (2, 57, 0)
        sim.carve((2,), (57, 58), (0,), "air")
        sim.set_block(2, 56, 0, "stone")
        sim.ground_drops[(2, 57, 0)] = {"raw_iron": 1}

        world = VoxelWorld3D()
        ctx = _make_harvester_ctx(sim)
        before = sim.get_inventory_counts()
        OreHarvester.collect_mined_drop(
            ctx,
            world,
            (2, 59, 0),
            (0, 59, 0),
            "raw_iron",
            before,
            sim.reader,
        )
        self.assertEqual(sim.get_inventory_counts().get("raw_iron", 0), before.get("raw_iron", 0) + 1)
        # Must pillar out of the 1x1 well and return cleanly to stance y=59
        self.assertAlmostEqual(sim.status["pos"][1], 59.0, delta=0.1)
        self.assertEqual(int(sim.status["pos"][0] // 1), 0)

    def test_collect_mined_drop_handles_corner_and_high_cliff_ledge(self):
        # 1) Corner drop blocked straight across (1, 59, 0), must detour via (0, 59, 1) -> (1, 59, 1)
        sim_corner = _StrictPickupSim(bot_pos=(0.5, 59.0, 0.5))
        for p in ((0, 0), (0, 1), (1, 1)):
            sim_corner.carve((p[0],), (59, 60, 61), (p[1],), "air")
            sim_corner.set_block(p[0], 58, p[1], "stone")
        sim_corner.set_block(1, 59, 0, "obsidian")
        sim_corner.set_block(1, 60, 0, "obsidian")
        sim_corner.ground_drops[(1, 59, 1)] = {"diamond": 1}

        world_c = VoxelWorld3D()
        ctx_c = _make_harvester_ctx(sim_corner)
        before_c = sim_corner.get_inventory_counts()
        OreHarvester.collect_mined_drop(
            ctx_c,
            world_c,
            (1, 59, 1),
            (0, 59, 0),
            "diamond",
            before_c,
            sim_corner.reader,
        )
        self.assertEqual(sim_corner.get_inventory_counts().get("diamond", 0), before_c.get("diamond", 0) + 1)

        # 2) High cliff ledge (dy=+2) with diggable floor drops item cleanly onto low ground
        sim_cliff = _StrictPickupSim(bot_pos=(0.5, 59.0, 0.5))
        sim_cliff.carve((0, 1, 2), (59, 60, 61, 62, 63), (0,), "air")
        for x in (0, 1, 2):
            sim_cliff.set_block(x, 58, 0, "stone")
        sim_cliff.set_block(2, 60, 0, "stone")
        sim_cliff.ground_drops[(2, 61, 0)] = {"raw_iron": 1}

        world_k = VoxelWorld3D()
        ctx_k = _make_harvester_ctx(sim_cliff)
        before_k = sim_cliff.get_inventory_counts()
        OreHarvester.collect_mined_drop(
            ctx_k,
            world_k,
            (2, 61, 0),
            (0, 59, 0),
            "raw_iron",
            before_k,
            sim_cliff.reader,
        )
        self.assertEqual(sim_cliff.get_inventory_counts().get("raw_iron", 0), before_k.get("raw_iron", 0) + 1)

    def test_world_navigator_follow_path_coalesces_walk_strides(self):
        sim = _Sim3DCaveClient(bot_pos=(0.5, 59.0, 0.5))
        sim.carve(range(0, 14), (59, 60, 61), (0,), "air")
        for x in range(0, 14):
            sim.set_block(x, 58, 0, "stone")
        world = VoxelWorld3D()
        ctx = _make_harvester_ctx(sim)
        path = Pathfinder3D.find_path(world, (0, 59, 0), (12, 59, 0), sim.reader, allow_dig=False, allow_pillar=False)
        self.assertEqual(len(path), 12)
        ok, final_pos = WorldNavigator.follow_path(ctx, world, (0, 59, 0), path, sim.reader)
        self.assertTrue(ok)
        self.assertEqual(final_pos, [12, 59, 0])
        move_calls = [a for a in sim.actions if a[0] == "move_to"]
        # 12 steps coalesced into 3 strides of 4 blocks each
        self.assertLessEqual(len(move_calls), 4)


if __name__ == "__main__":
    unittest.main()
