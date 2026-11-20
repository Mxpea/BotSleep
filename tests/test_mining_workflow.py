"""Offline geometry, site-selection, safety, and workflow tests for MineShaftSkill."""

import copy
import unittest
from unittest.mock import Mock, patch

from core.structure_spec import (
    DEFAULT_REPLACEABLE,
    MINE_SHAFT_SPEC,
    ORE_DROP_MAP,
    inspect_structure,
    mine_shaft_spec,
    transform_rel_pos,
)
from skills.mining import MineShaftSkill


class WorkflowMineWorld:
    """In-memory underground terrain world for testing 5-step staircase + 4x3x2 platform mining."""

    DROPS = {
        **ORE_DROP_MAP,
        "stone": "cobblestone",
        "andesite": "andesite",
        "diorite": "diorite",
        "granite": "granite",
        "deepslate": "cobbled_deepslate",
        "dirt": "dirt",
        "grass_block": "dirt",
        "gravel": "gravel",
    }

    def __init__(self, *, bot_pos=(0.5, 64.0, -2.5), surface_y=64):
        self.blocks = {}
        self.surface_y = surface_y
        # Fill natural terrain cube: stone below y=(surface_y - 2), dirt at surface_y-2..surface_y-1, air above
        for x in range(-18, 19):
            for z in range(-18, 19):
                for y in range(surface_y - 10, surface_y):
                    name = "grass_block" if y == surface_y - 1 else ("dirt" if y == surface_y - 2 else "stone")
                    self.blocks[(x, y, z)] = {"name": name, "properties": {}}

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
            "held": {"name": "stone_pickaxe", "count": 1},
        }
        self.items = {"stone_pickaxe": 1, "cobblestone": 4}
        self.extra_inventory_entries = []
        self.actions = []
        self.dig_collect_flags = []
        self.events = []
        self.fall_columns = set()

    def get_status(self, **kwargs):
        return {"ok": True, "result": copy.deepcopy(self.status)}

    def inventory(self, **kwargs):
        items = [{"name": k, "count": v} for k, v in self.items.items() if v > 0]
        items.extend(copy.deepcopy(self.extra_inventory_entries))
        return {
            "ok": True,
            "result": {
                "counts": dict(self.items),
                "items": items,
            },
        }

    def get_inventory_counts(self):
        return dict(self.items)

    def get_entities(self, **kwargs):
        return []

    def get_events(self, **kwargs):
        return list(self.events)

    def get_block(self, x, y=None, z=None, **kwargs):
        pos = x if y is None else (x, y, z)
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        blk = copy.deepcopy(self.blocks.get(key, {"name": "air", "properties": {}}))
        blk["pos"] = list(key)
        return {"ok": True, "result": blk}

    def move_to(self, x, y, z, range=1.0, **kwargs):
        self.actions.append(("move_to", [x, y, z, range]))
        target_y = float(self.status["pos"][1]) if y is None else float(y)
        self.status["pos"] = [float(x), target_y, float(z)]
        return {"ok": True, "result": {"pos": list(self.status["pos"])}}

    def look_at(self, x, y, z, **kwargs):
        return {"ok": True, "result": {}}

    def equip(self, item, **kwargs):
        if self.items.get(item, 0) <= 0:
            return {"ok": False, "error": {"code": "missing_item", "message": f"No {item}"}}
        self.status["held"] = {"name": item, "count": self.items[item]}
        return {"ok": True, "result": {"equipped": item}}

    def posture(self, sneak=False, **kwargs):
        self.status["sneak"] = bool(sneak)
        return {"ok": True, "result": {"sneak": bool(sneak)}}

    def sneak(self, active=True, **kwargs):
        return self.posture(sneak=active, **kwargs)

    def dig(self, x, y, z, collect=False, **kwargs):
        self.dig_collect_flags.append(bool(collect))
        key = (int(x), int(y), int(z))
        blk = self.blocks.get(key, {"name": "air"})
        name = blk.get("name", "air")
        if name == "air":
            return {"ok": False, "error": {"code": "target_empty", "message": "Air"}}
        self.actions.append(("dig", list(key), name))
        self.blocks.pop(key, None)
        drop = self.DROPS.get(name, name)
        if name not in {"short_grass", "tall_grass", "leaf_litter"}:
            self.items[drop] = self.items.get(drop, 0) + 1
        # Simulate sand/gravel falling from 1 block above
        above_key = (key[0], key[1] + 1, key[2])
        above = self.blocks.get(above_key)
        if above and above.get("name") in {"sand", "red_sand", "gravel"}:
            self.blocks[key] = self.blocks.pop(above_key)
        return {"ok": True, "result": {"dug": name}}

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
        cur = self.blocks.get(key, {"name": "air"}).get("name")
        if cur not in DEFAULT_REPLACEABLE:
            return {"ok": False, "error": {"code": "target_obstructed", "message": "Occupied"}}
        self.items[item] -= 1
        if str(item).endswith("_bed"):
            bx, bz = float(self.status["pos"][0]), float(self.status["pos"][2])
            dx = (key[0] + 0.5) - bx
            dz = (key[2] + 0.5) - bz
            if abs(dx) >= abs(dz):
                step = (1, 0) if dx >= 0 else (-1, 0)
            else:
                step = (0, 1) if dz >= 0 else (0, -1)
            head_key = (key[0] + step[0], key[1], key[2] + step[1])
            head_cur = self.blocks.get(head_key, {"name": "air"}).get("name")
            if head_cur not in DEFAULT_REPLACEABLE:
                self.items[item] += 1
                return {"ok": False, "error": {"code": "target_obstructed", "message": "Bed head occupied"}}
            self.blocks[key] = {"name": item, "properties": {"part": "foot"}}
            self.blocks[head_key] = {"name": item, "properties": {"part": "head"}}
            self.actions.append(("place_bed", [list(key), list(head_key)], item))
            return {"ok": True, "result": {"placed": item, "pos": list(key)}}
        if item == "torch":
            below = self.blocks.get((key[0], key[1] - 1, key[2]), {"name": "air"}).get("name")
            placed_name = "torch" if below not in DEFAULT_REPLACEABLE else "wall_torch"
            self.blocks[key] = {"name": placed_name, "properties": {}}
            self.actions.append(("place", list(key), placed_name))
            return {"ok": True, "result": {"placed": placed_name, "pos": list(key)}}
        props = {"type": "single"} if item == "chest" else {}
        self.blocks[key] = {"name": item, "properties": props}
        self.actions.append(("place", list(key), item))
        return {"ok": True, "result": {"placed": item, "pos": list(key)}}

    def interact(self, pos=None, *, item=None, **kwargs):
        if pos is None:
            return {"ok": False, "error": {"code": "bad_args", "message": "pos required"}}
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        blk = self.blocks.get(key, {"name": "air", "properties": {}})
        name = blk.get("name", "air")
        if item is None and str(name).endswith("_bed"):
            self.actions.append(("interact_bed", list(key), name))
            if getattr(self, "nighttime", False):
                self.status["sleeping"] = True
                return {"ok": True, "result": {"slept": True}}
            return {"ok": True, "result": {"slept": False, "interacted": True, "daytime": True}}
        if item == "chest" and name not in DEFAULT_REPLACEABLE:
            above_key = (key[0], key[1] + 1, key[2])
            if self.items.get("chest", 0) <= 0:
                return {"ok": False, "error": {"code": "missing_item", "message": "No chest"}}
            if self.blocks.get(above_key, {"name": "air"}).get("name") not in DEFAULT_REPLACEABLE:
                return {"ok": False, "error": {"code": "target_obstructed", "message": "Above occupied"}}
            self.items["chest"] -= 1
            ctype = "single"
            for ndx, ndz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nkey = (above_key[0] + ndx, above_key[1], above_key[2] + ndz)
                nblk = self.blocks.get(nkey)
                if nblk and nblk.get("name") == "chest":
                    nblk.setdefault("properties", {})["type"] = "right"
                    ctype = "left"
                    break
            self.blocks[above_key] = {"name": "chest", "properties": {"type": ctype}}
            self.actions.append(("interact_place_chest", list(above_key), ctype))
            return {"ok": True, "result": {"block": name, "above": {"before": "air", "after": "chest"}, "changed": True}}
        self.actions.append(("interact", list(key), item))
        return {"ok": True, "result": {"interacted": True}}

    def wake(self):
        self.status["sleeping"] = False
        self.actions.append(("wake",))
        return {"ok": True, "result": {"awake": True}}

    def stop(self):
        return {"ok": True, "result": {}}


class MiningWorkflowTests(unittest.TestCase):
    def test_excavation_rejects_open_channel_below_liquid_before_writing(self):
        from skills.action_workflow import WorkflowContext, WorkflowGuard, WorkflowStepError

        world = WorkflowMineWorld()
        world.blocks[(1, 68, -3)] = {"name": "lava"}
        skill = MineShaftSkill(world, memory=Mock())
        context = WorkflowContext(
            guard=WorkflowGuard(world, timeout_seconds=30),
            spec=MineShaftSkill._hub_only_spec(4, include_workstations=False),
            origin=[0, 64, 0], orientation="south",
        )
        with self.assertRaises(WorkflowStepError) as caught:
            skill._dig_cell_safely(context, [1, 63, -3], relative=False)
        self.assertEqual(caught.exception.code, "hazard_adjacent")
        self.assertEqual(list(caught.exception.blocker_pos), [1, 68, -3])
        self.assertEqual(world.dig_collect_flags, [])

    def setUp(self):
        # Rest/resupply creates a nested container skill; isolate its legacy memory too.
        container_memory = patch("skills.container.MemoryEngine", return_value=Mock())
        container_memory.start()
        self.addCleanup(container_memory.stop)

    def test_mine_shaft_spec_structure_and_four_orientations(self):
        spec = mine_shaft_spec(branch_length=4)
        self.assertEqual(spec.spec_id, "mine_shaft_5x4x3x2")
        self.assertEqual(
            spec.stages,
            ("stairs_floor", "stairs_clear", "platform_floor", "platform_clear", "branch_floor", "branch_clear"),
        )
        stairs = [c for c in spec.cells if c.stage == "stairs_clear"]
        plat = [c for c in spec.cells if c.stage == "platform_clear"]
        branch = [c for c in spec.cells if c.stage == "branch_clear"]
        # 5 steps x 3 tall = 15; open 4-long along tunnel (dz=5..8, dx=0..3, dy=-5..-4) = 32; 1x2x4 branch (dz=9..12) = 8
        self.assertEqual(len(stairs), 15)
        self.assertEqual(len(plat), 32)
        self.assertEqual(len(branch), 8)

        # Long edge (4 blocks: dz=5..8) borders tunnel dx=0, and all workstation slots stay out of dx=0
        plat_set = {c.rel_pos for c in plat}
        station_cells = []
        for slot in (
            "workstation_bed_foot",
            "workstation_bed_head",
            "workstation_crafting_table",
            "workstation_furnace",
            "workstation_chest_1",
            "workstation_chest_2",
            "workstation_torch",
        ):
            anchor = spec.anchors[slot].rel_pos
            cell = (int(anchor[0] // 1), int(anchor[1] // 1), int(anchor[2] // 1))
            self.assertIn(cell, plat_set)
            self.assertNotEqual(cell[0], 0)
            self.assertIn(cell[2], (5, 6, 7, 8))
            station_cells.append(cell)
        self.assertEqual(len(set(station_cells)), 7)

        ws_spec = mine_shaft_spec(branch_length=4, include_workstations=True)
        self.assertEqual(ws_spec.stages[-1], "workstation")
        self.assertEqual(len([c for c in ws_spec.cells if c.stage == "platform_clear"]), 25)
        self.assertEqual(len([c for c in ws_spec.cells if c.stage == "workstation"]), 7)

        # Rotate anchors across all 4 cardinal directions
        entries = {
            orient: tuple(transform_rel_pos((0, 0, -1), [10, 64, 10], orient, integer=True))
            for orient in ("south", "west", "north", "east")
        }
        self.assertEqual(entries["south"], (10, 64, 9))
        self.assertEqual(entries["north"], (10, 64, 11))
        self.assertEqual(entries["east"], (9, 64, 10))
        self.assertEqual(entries["west"], (11, 64, 10))

    def test_find_mine_sites_rejects_lava_water_and_chests_and_prefers_stone(self):
        world = WorkflowMineWorld(bot_pos=(0.5, 64.0, 0.5))
        # Put lava right under origin [0, 64, 0] area and chest nearby
        world.blocks[(0, 59, 4)] = {"name": "lava", "properties": {}}
        world.blocks[(1, 64, 0)] = {"name": "chest", "properties": {}}
        sites = MineShaftSkill(world, memory=Mock()).find_mine_sites(radius=9, max_results=3)
        self.assertTrue(sites)
        best = sites[0]
        # Must not pick a site touching (0, 59, 4) lava or (1, 64, 0) chest
        excavated = {
            tuple(transform_rel_pos(rel, best["origin"], best["orientation"], integer=True))
            for rel in MINE_SHAFT_SPEC.clearance_rel
        }
        self.assertNotIn((0, 59, 4), excavated)
        self.assertNotIn((1, 64, 0), excavated)

    def test_build_mine_shaft_excavates_5_stairs_4x3x2_platform_branch_patches_floor_and_returns(self):
        world = WorkflowMineWorld(bot_pos=(0.5, 64.0, -2.5))
        # Seed coal and iron ore along stairs & branch plus a 1-block floor cavity under platform in 'south'
        world.blocks[(0, 59, 9)] = {"name": "coal_ore", "properties": {}}
        world.blocks[(0, 60, 10)] = {"name": "iron_ore", "properties": {}}
        world.blocks[(1, 64, -1)] = {"name": "short_grass", "properties": {}}
        # Remove one floor block inside the 4x3x2 platform bottom (dx=2, dy=-6, dz=6 -> (2, 58, 6))
        world.blocks.pop((2, 58, 6), None)

        res = MineShaftSkill(world, memory=Mock()).build_mine_shaft(
            origin=[0, 64, 0],
            orientation="south",
            branch_length=4,
            return_to_surface=True,
        )
        self.assertTrue(res.success, res)
        self.assertFalse(res.data["workstation_deployed"])
        self.assertTrue(res.data["structure_verified"])
        self.assertTrue(res.data["structure_report"]["complete"])
        # Floor hole at (2, 58, 6) must have been patched cleanly
        self.assertNotEqual(world.blocks.get((2, 58, 6), {"name": "air"})["name"], "air")
        # Ores & cobblestone harvested cleanly
        self.assertGreater(res.data["harvested"].get("cobblestone", 0), 20)
        self.assertEqual(res.data["harvested"].get("coal"), 1)
        self.assertEqual(res.data["harvested"].get("raw_iron"), 1)
        # Workstation anchors returned in rotated world coordinates (4-long edge along tunnel)
        self.assertEqual(res.data["workstation_slots"]["workstation_bed_foot"], [2, 59, 5])
        self.assertEqual(res.data["workstation_slots"]["workstation_bed_head"], [3, 59, 5])
        self.assertEqual(res.data["workstation_slots"]["workstation_crafting_table"], [3, 59, 6])
        self.assertEqual(res.data["workstation_slots"]["workstation_furnace"], [3, 60, 6])
        self.assertEqual(res.data["workstation_slots"]["workstation_chest_1"], [3, 59, 7])
        self.assertEqual(res.data["workstation_slots"]["workstation_chest_2"], [3, 59, 8])
        self.assertEqual(res.data["workstation_slots"]["workstation_torch"], [1, 60, 5])
        # Returned cleanly to surface entry (y=64)
        self.assertAlmostEqual(world.status["pos"][1], 64.0, delta=0.5)

        # Direct inspect_mine_shaft also verifies 100% completion across all 6 stages
        report = MineShaftSkill(world, memory=Mock()).inspect_mine_shaft([0, 64, 0], orientation="south", branch_length=4)
        self.assertTrue(report["complete"])

    def test_build_mine_shaft_deploys_full_workstation_kit_and_saves_bed_respawn(self):
        # 1. Missing items rejected cleanly when place_workstation=True
        world_missing = WorkflowMineWorld(bot_pos=(0.5, 64.0, -2.5))
        r_missing = MineShaftSkill(world_missing, memory=Mock()).build_mine_shaft(
            origin=[0, 64, 0],
            orientation="south",
            place_workstation=True,
        )
        self.assertFalse(r_missing.success)
        self.assertEqual(r_missing.error_code, "missing_workstation_items")
        self.assertFalse(r_missing.data["executed"])
        self.assertEqual(world_missing.actions, [])

        # 2. Full kit carried: bed, crafting_table, furnace, chest x2, torch (test nighttime sleep -> wake recovery!)
        world = WorkflowMineWorld(bot_pos=(0.5, 64.0, -2.5))
        world.nighttime = True
        world.items.update({
            "white_bed": 1,
            "crafting_table": 1,
            "furnace": 1,
            "chest": 2,
            "torch": 1,
        })
        res = MineShaftSkill(world, memory=Mock()).build_mine_shaft(
            origin=[0, 64, 0],
            orientation="south",
            branch_length=4,
            place_workstation=True,
        )
        self.assertTrue(res.success, res)
        self.assertTrue(res.data["workstation_deployed"])
        self.assertTrue(res.data["structure_verified"])
        ws = res.data["workstation"]
        self.assertTrue(ws["bed_respawn_saved"])
        self.assertTrue(ws["bed_woke_up"])
        self.assertTrue(ws["double_chest_merged"])

        # Exact placed blocks in world
        self.assertEqual(world.blocks[(2, 59, 5)]["name"], "white_bed")
        self.assertEqual(world.blocks[(2, 59, 5)]["properties"]["part"], "foot")
        self.assertEqual(world.blocks[(3, 59, 5)]["name"], "white_bed")
        self.assertEqual(world.blocks[(3, 59, 5)]["properties"]["part"], "head")
        self.assertEqual(world.blocks[(3, 59, 6)]["name"], "crafting_table")
        self.assertEqual(world.blocks[(3, 60, 6)]["name"], "furnace")
        self.assertEqual(world.blocks[(3, 59, 7)]["name"], "chest")
        self.assertEqual(world.blocks[(3, 59, 8)]["name"], "chest")
        self.assertIn(world.blocks[(3, 59, 7)]["properties"]["type"], {"left", "right"})
        self.assertIn(world.blocks[(3, 59, 8)]["properties"]["type"], {"left", "right"})
        self.assertNotIn((3, 60, 7), world.blocks)
        self.assertNotIn((3, 60, 8), world.blocks)
        self.assertEqual(world.blocks[(1, 60, 5)]["name"], "wall_torch")
        self.assertFalse(world.status.get("sleeping", False))

        # Direct inspect_mine_shaft(include_workstations=True) verifies 100% including 'workstation' stage
        report = MineShaftSkill(world, memory=Mock()).inspect_mine_shaft(
            [0, 64, 0],
            orientation="south",
            branch_length=4,
            include_workstations=True,
        )
        self.assertTrue(report["complete"], report)

    def test_build_mine_shaft_auto_selects_east_and_checks_pickaxe_and_gravel(self):
        # 1. Missing pickaxe fails deterministically before any writes
        world_no_pick = WorkflowMineWorld(bot_pos=(0.5, 64.0, -2.5))
        world_no_pick.items.pop("stone_pickaxe")
        world_no_pick.status["held"] = {}
        r_fail = MineShaftSkill(world_no_pick, memory=Mock()).build_mine_shaft(origin=[0, 64, 0], orientation="east")
        self.assertEqual(r_fail.error_code, "missing_pickaxe")
        self.assertFalse(r_fail.data["executed"])
        self.assertEqual(world_no_pick.actions, [])

        # 2. East orientation with falling gravel above branch succeeds via settle loop
        world = WorkflowMineWorld(bot_pos=(-1.5, 64.0, 0.5))
        world.blocks[(9, 60, 0)] = {"name": "gravel", "properties": {}}
        world.blocks[(9, 61, 0)] = {"name": "gravel", "properties": {}}
        res = MineShaftSkill(world, memory=Mock()).build_mine_shaft(
            origin=[0, 64, 0],
            orientation="east",
            branch_length=3,
        )
        self.assertTrue(res.success, res)
        self.assertTrue(res.data["structure_verified"])
        # Confirm gravity-fallen gravel inside (9, 60, 0) was cleared cleanly to air
        self.assertEqual(world.blocks.get((9, 60, 0), {"name": "air"})["name"], "air")
        self.assertAlmostEqual(world.status["pos"][1], 59.0, delta=0.5)
        self.assertTrue(all(f is False for f in world.dig_collect_flags))

    def test_unsafe_caves_seal_rotate_headings_and_abandon_on_third(self):
        world = WorkflowMineWorld(bot_pos=(0.5, 64.0, -2.5))
        world.items["cobblestone"] = 32
        world.items["torch"] = 12
        # 1) Heading 0 (dz=9+): air tunnel from dz=10 to a deep water pool at dz=14 (>= 3 water blocks, beyond initial site check)
        for dz in range(10, 15):
            for dy in (59, 60):
                world.blocks.pop((0, dy, dz), None)
        for dx in (-1, 0, 1):
            world.blocks.pop((dx, 60, 14), None)
            world.blocks[(dx, 59, 14)] = {"name": "water", "properties": {}}

        # 2) Heading 1 ((0,-5,5) -> -x): tall cave height=10 at x=-2
        for x in (-2, -3):
            for y in range(59, 69):
                world.blocks.pop((x, y, 5), None)

        # 3) Heading 2 ((0,-5,8) -> -x): deep drop (drop=5 >= 4, height=7 < 10) at x=-2
        for y in range(54, 61):
            world.blocks.pop((-2, y, 8), None)
        world.blocks[(-2, 53, 8)] = {"name": "stone", "properties": {}}

        res = MineShaftSkill(world, memory=Mock()).build_mine_shaft(
            origin=[0, 64, 0],
            orientation="south",
            branch_length=4,
        )
        self.assertFalse(res.success)
        self.assertEqual(res.error_code, "cave_abandoned")
        self.assertEqual(res.data["stop_reason"], "cave_abandoned")
        self.assertEqual(len(res.data["caves"]), 3)
        kinds = [c["kind"] for c in res.data["caves"]]
        self.assertEqual(kinds, ["water_cave", "tall_cave", "deep_drop"])
        # Confirm all 3 breaches were sealed with cobblestone
        self.assertEqual(world.blocks[(0, 59, 10)]["name"], "cobblestone")
        self.assertEqual(world.blocks[(-2, 59, 5)]["name"], "cobblestone")
        self.assertEqual(world.blocks[(-2, 59, 8)]["name"], "cobblestone")
        self.assertTrue(all(f is False for f in world.dig_collect_flags))

    def test_branch_links_cave_explore_and_targets_only_mined(self):
        world = WorkflowMineWorld(bot_pos=(0.5, 64.0, -2.5))
        world.items["torch"] = 24
        world.items["cobblestone"] = 24
        # Breach a low safe side cave along branch at dz=10 extending +x (x=1..4, y=59..62, z=10)
        for dx in (0, 1, 2, 3, 4):
            for dy in (59, 60, 61):
                world.blocks.pop((dx, dy, 10), None)
        # Put iron_ore in cave wall + copper_ore (unlisted) + diamond_ore (tool too low) + wall ore at branch step 1 (dz=9)
        world.blocks[(1, 60, 9)] = {"name": "iron_ore", "properties": {}}
        world.blocks[(-1, 60, 9)] = {"name": "copper_ore", "properties": {}}
        world.blocks[(2, 60, 11)] = {"name": "deepslate_iron_ore", "properties": {}}
        world.blocks[(3, 59, 11)] = {"name": "diamond_ore", "properties": {}}

        res = MineShaftSkill(world, memory=Mock()).build_mine_shaft(
            origin=[0, 64, 0],
            orientation="south",
            branch_length=4,
            targets={"iron": {"count": 10, "min_tool": "stone_pickaxe"}, "diamond": {"count": 2, "min_tool": "iron_pickaxe"}},
        )
        self.assertTrue(res.success, res)
        self.assertEqual(res.data["stop_reason"], "branch_done")
        self.assertTrue(res.data["caves"] and res.data["caves"][0]["decision"] == "entered")
        self.assertEqual(res.data["caves"][0]["kind"], "small")
        self.assertGreaterEqual(res.data["torches_placed"], 1)
        by_pos = {tuple(o["pos"]): o["status"] for o in res.data["ores"]}
        self.assertEqual(by_pos[(1, 60, 9)], "mined")
        self.assertEqual(by_pos[(-1, 60, 9)], "seen")
        self.assertEqual(by_pos[(2, 60, 11)], "mined")
        self.assertEqual(by_pos[(3, 59, 11)], "skipped_tool")
        self.assertTrue(all(f is False for f in world.dig_collect_flags))

    def test_checkpoint_pause_on_full_bag_refuses_pending_and_resumes_when_clear(self):
        import tempfile
        from core.survival_state import SurvivalJournal

        world = WorkflowMineWorld(bot_pos=(0.5, 64.0, -2.5))
        world.items.update({
            "white_bed": 1,
            "crafting_table": 1,
            "furnace": 1,
            "chest": 2,
            "torch": 8,
            "cobblestone": 32,
        })
        # Fill 31 slots so empty slots == 3 after workstation placement & 4 steps -> triggers return_home_requested
        world.extra_inventory_entries = [{"name": f"junk_{i}", "count": 1, "slot": 10 + i} for i in range(30)]

        with tempfile.TemporaryDirectory() as tmp:
            journal = SurvivalJournal(tmp, endpoint="http://127.0.0.1:18900/api/v1", username="SleepBot", world_id="test_world")
            skill = MineShaftSkill(world, memory=Mock())

            first = skill.run_with_journal(
                journal,
                origin=[0, 64, 0],
                orientation="south",
                branch_length=6,
                place_workstation=True,
                targets={"iron": {"count": 10, "min_tool": "stone_pickaxe"}},
            )
            self.assertTrue(first.success, first)
            self.assertEqual(first.error_code, "return_home_requested")
            self.assertEqual(first.data["stop_reason"], "return_home")
            self.assertEqual(first.data["home_reason"], "inventory_full")
            self.assertIsNotNone(journal.get_mining_progress())
            self.assertEqual(journal.get_mining_progress()["steps_completed"], 4)

            # While a pending intention exists, auto-resume MUST refuse
            journal.update(pending={"action": "place_block", "ts": 123})
            blocked = skill.run_with_journal(journal, branch_length=6)
            self.assertFalse(blocked.success)
            self.assertEqual(blocked.error_code, "pending_intent_active")

            # Clear pending intention and free inventory slots -> resumes from step 4 to completion
            journal.update(pending=None)
            world.extra_inventory_entries.clear()
            resumed = skill.run_with_journal(journal, branch_length=6)
            self.assertTrue(resumed.success, resumed)
            self.assertEqual(resumed.data["stop_reason"], "branch_done")
            self.assertEqual(journal.get_mining_progress()["steps_completed"], 6)

    def test_deep_target_y_fishbone_reaches_30_iron_quota_and_stops(self):
        world = WorkflowMineWorld(bot_pos=(0.5, 64.0, -2.5))
        # Extend solid stone down to y=46 so stair descent from 59 -> 52 stays solid
        for x in range(-14, 15):
            for z in range(-4, 30):
                for y in range(48, 55):
                    world.blocks[(x, y, z)] = {"name": "stone", "properties": {}}
        world.items.update({"iron_pickaxe": 2, "torch": 24, "cobblestone": 32})
        # Seed 34 exposed/adjacent iron ores along fishbone ribs at y=52..53 (trunk at z=15..25)
        planted = 0
        for z in (19, 23):
            for x in range(-6, 7):
                if x == 0:
                    continue
                for y in (52, 53):
                    if planted < 34:
                        world.blocks[(x, y, z)] = {"name": "iron_ore", "properties": {}}
                        planted += 1

        res = MineShaftSkill(world, memory=Mock()).build_mine_shaft(
            origin=[0, 64, 0],
            orientation="south",
            target_y=52,
            mining_mode="fishbone",
            rib_length=6,
            rib_spacing=4,
            max_trunk_steps=16,
            targets={"iron": {"count": 30, "min_tool": "stone_pickaxe"}},
        )
        self.assertTrue(res.success, res)
        self.assertEqual(res.data["stop_reason"], "target_reached")
        self.assertGreaterEqual(res.data["harvested"].get("raw_iron", 0), 30)
        self.assertTrue(all(f is False for f in world.dig_collect_flags))

    def test_deep_band_exits_band_complete_when_only_shallow_coal_unmet_and_returns_to_surface(self):
        world = WorkflowMineWorld(bot_pos=(0.5, 10.0, -2.5), surface_y=10)
        world.items.update({"iron_pickaxe": 1, "cobblestone": 64, "torch": 16})
        for x in range(-18, 19):
            for z in range(-10, 32):
                for y in range(-8, 8):
                    world.blocks[(x, y, z)] = {"name": "stone", "properties": {}}
        res = MineShaftSkill(world, memory=Mock()).build_mine_shaft(
            origin=[0, 10, 0],
            orientation="south",
            target_y=-2,
            mining_mode="fishbone",
            rib_length=6,
            rib_spacing=4,
            max_trunk_steps=16,
            return_to_surface=True,
            initial_quota_progress={"diamond": 1},
            targets={
                "diamond": {"count": 1, "min_tool": "iron_pickaxe"},
                "coal": {"count": 4, "min_tool": "wooden_pickaxe"},
            },
        )
        self.assertTrue(res.success, res)
        self.assertEqual(res.data["stop_reason"], "band_complete")
        self.assertAlmostEqual(world.status["pos"][1], 10.0, delta=1.0)

    def test_deep_retreat_done_prevents_re_descent_on_branch_retry(self):
        from skills.action_workflow import WorkflowContext, WorkflowGuard

        world = WorkflowMineWorld(bot_pos=(0.5, 5.0, 8.5), surface_y=10)
        guard = WorkflowGuard(world, timeout_seconds=30.0)
        ctx = WorkflowContext(
            guard=guard,
            spec=MineShaftSkill._hub_only_spec(4, include_workstations=False),
            origin=[0, 10, 0],
            orientation="south",
            extra={
                "deep_retreat_done": True,
                "stop_reason": "return_home",
                "deep_pos": [0, -40, 20],
                "deep_trail": [[0, 5, 8], [0, -40, 20]],
            },
        )
        skill = MineShaftSkill(world, memory=Mock())
        out = skill._wf_deep_fishbone(ctx)
        self.assertEqual(out["stop_reason"], "return_home")
        self.assertAlmostEqual(world.status["pos"][1], 5.0, delta=0.5)

    def test_prepare_entry_moves_bot_off_origin_edge_before_step_1_floor_dig(self):
        # Bot starts inside cell (0, 64, 0) but within 0.7 blocks of (0.5, 64.0, -0.5)
        world = WorkflowMineWorld(bot_pos=(0.5, 64.0, 0.15))
        res = MineShaftSkill(world, memory=Mock()).build_mine_shaft(
            origin=[0, 64, 0],
            orientation="south",
            branch_length=4,
            place_workstation=False,
        )
        self.assertTrue(res.success, res)

    def test_prepare_entry_moves_elevated_bot_off_surface_entry_cell(self):
        # Bot starts 1 block higher on top of (0, 64, -1) with solid stone at (0, 64, -1)
        world = WorkflowMineWorld(bot_pos=(0.5, 65.0, -0.5))
        world.blocks[(0, 64, -1)] = {"name": "stone", "properties": {}}
        res = MineShaftSkill(world, memory=Mock()).build_mine_shaft(
            origin=[0, 64, 0],
            orientation="south",
            branch_length=4,
            place_workstation=False,
        )
        self.assertTrue(res.success, res)

    def test_retreat_downsampled_breaks_early_after_consecutive_stalls(self):
        from skills.action_workflow import WorkflowContext, WorkflowGuard

        world = WorkflowMineWorld(bot_pos=(0.5, 10.0, 0.5), surface_y=64)
        stalled_targets = []

        def fake_move(x, y, z, **kwargs):
            stalled_targets.append((x, y, z))
            return {"ok": False, "error": {"code": "stalled", "message": "stuck"}}

        world.move_to = fake_move
        guard = WorkflowGuard(world, timeout_seconds=30.0)
        ctx = WorkflowContext(
            guard=guard,
            spec=MineShaftSkill._hub_only_spec(4, include_workstations=False),
            origin=[0, 64, 0],
            orientation="south",
            extra={"world_3d": None},
        )
        trail = [[0, y, 0] for y in range(64, 9, -1)]
        with patch("skills.mining.Pathfinder3D.find_path", return_value=[]):
            MineShaftSkill(world, memory=Mock())._retreat_downsampled(ctx, trail)
        # Should abort after 2 consecutive stalled segments instead of iterating all ~28 segments.
        self.assertLessEqual(len(stalled_targets), 12)

    def test_deep_fishbone_backtracks_up_staircase_and_jogs_sideways_past_water_pocket(self):
        world = WorkflowMineWorld(bot_pos=(0.5, 64.0, 0.5), surface_y=64)
        world.inventory_counts = {"iron_pickaxe": 2, "cobblestone": 64, "torch": 16}
        # Block descent around south hub exit at Y=57..56 with water pocket across straight & neighbors
        for x in range(-2, 3):
            for z in range(11, 18):
                world.blocks[(x, 56, z)] = {"name": "water", "diggable": False}
                world.blocks[(x, 55, z)] = {"name": "water", "diggable": False}
        res = MineShaftSkill(world, memory=Mock()).build_mine_shaft(
            origin=[0, 64, 0],
            orientation="south",
            target_y=52,
            place_workstation=False,
            return_to_surface=True,
            max_trunk_steps=2,
            rib_length=2,
            timeout_seconds=45.0,
        )
        # Bypass should descend past the water pocket at Y=56..55 without getting stuck spinning at one coordinate.
        self.assertTrue(res.success, res)
        self.assertLessEqual(res.data["mining_progress"]["deep_pos"][1], 53)

    def test_uncertain_boundary_and_discover_caves_convert_workflow_step_error_to_skill_result(self):
        from skills import SkillResult
        from skills.action_workflow import WorkflowStepError
        from skills.cave_explore import CaveExploreSkill

        world = WorkflowMineWorld(bot_pos=(0.5, 64.0, 0.5), surface_y=64)
        world.inventory_counts = {"stone_pickaxe": 1, "cobblestone": 16}

        with patch(
            "skills.ore_harvester.OreHarvester.read_inventory",
            side_effect=WorkflowStepError("timeout", "Workflow time budget exceeded", blocker_type="safety_interrupted"),
        ):
            cave_res = CaveExploreSkill(world, memory=Mock()).discover_and_explore_caves(origin=[0, 64, 0])
        self.assertIsInstance(cave_res, SkillResult)
        self.assertFalse(cave_res.success)
        self.assertEqual(cave_res.error_code, "timeout")

        with patch(
            "skills.mining.CaveExploreSkill.discover_and_explore_caves",
            side_effect=WorkflowStepError("timeout", "Workflow time budget exceeded", blocker_type="safety_interrupted"),
        ):
            mine_res = MineShaftSkill(world, memory=Mock()).mine_targets_from_surface(
                origin=[0, 64, 0],
                targets={"iron": {"count": 1, "min_tool": "stone_pickaxe"}},
                prefer_caves_first=True,
                place_workstation=False,
            )
        self.assertIsInstance(mine_res, SkillResult)
        self.assertFalse(mine_res.success)
        self.assertEqual(mine_res.error_code, "timeout")
        self.assertFalse(mine_res.data.get("executed"))

    def test_mine_targets_from_surface_falls_through_to_shaft_when_cave_phase_times_out(self):
        from skills import SkillResult

        world = WorkflowMineWorld(bot_pos=(0.5, 64.0, 0.5), surface_y=64)
        world.inventory_counts = {"stone_pickaxe": 1, "cobblestone": 16}
        shaft_calls = []

        def fake_build(miner_self, *args, **kwargs):
            shaft_calls.append(kwargs.get("target_ys"))
            return SkillResult(True, "Shaft done", data={"stop_reason": "target_reached", "mining_progress": {"deep_trail": []}})

        with patch(
            "skills.mining.CaveExploreSkill.discover_and_explore_caves",
            return_value=SkillResult(False, "Cave-first expedition interrupted (timeout)", error_code="timeout", data={"stop_reason": "timeout", "harvested": {}}),
        ), patch("skills.mining.MineShaftSkill.build_mine_shaft", new=fake_build):
            res = MineShaftSkill(world, memory=Mock()).mine_targets_from_surface(
                origin=[0, 64, 0],
                targets={"iron": {"count": 4, "min_tool": "stone_pickaxe"}},
                prefer_caves_first=True,
                place_workstation=False,
            )
        self.assertEqual(len(shaft_calls), 1)
        self.assertEqual(res.data["phases"][0]["error_code"], "timeout")
        self.assertEqual(res.data["phases"][1]["stop_reason"], "target_reached")


if __name__ == "__main__":
    unittest.main()
