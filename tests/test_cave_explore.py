"""Offline tests for standalone CaveExploreSkill (skills/cave_explore.py)."""

import copy
import unittest

from core.structure_spec import ORE_DROP_MAP
from skills.cave_explore import CaveExploreSkill


class CaveSimWorld:
    """In-memory underground cave/mineshaft simulator enforcing collect=False drop pickup on walkable tiles."""

    def __init__(self, *, bot_pos=(0.5, 59.0, 0.5), default="stone"):
        self.blocks = {}
        self.default = default
        self.ground_drops = {}  # (x, y, z) -> {item: count}
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
        self.items = [
            {"name": "stone_pickaxe", "count": 1, "slot": 0},
            {"name": "torch", "count": 16, "slot": 1},
            {"name": "cobblestone", "count": 32, "slot": 2},
        ]
        self.actions = []
        self.dig_collect_flags = []

    def carve(self, xs, ys, zs, name="air", props=None):
        for x in xs:
            for y in ys:
                for z in zs:
                    self.blocks[(x, y, z)] = {"name": name, "properties": dict(props or {})}

    def set_block(self, x, y, z, name, props=None):
        self.blocks[(x, y, z)] = {"name": name, "properties": dict(props or {})}

    def _pickup_near(self, pos):
        px, py, pz = int(pos[0]), int(pos[1]), int(pos[2])
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    key = (px + dx, py + dy, pz + dz)
                    bag = self.ground_drops.pop(key, None)
                    if bag:
                        for item_name, cnt in bag.items():
                            self._add_item(item_name, cnt)

    def _add_item(self, name, count=1):
        for row in self.items:
            if row["name"] == name:
                row["count"] += count
                return
        used = {r.get("slot") for r in self.items if isinstance(r.get("slot"), int)}
        next_slot = next((i for i in range(36) if i not in used), len(self.items))
        self.items.append({"name": name, "count": count, "slot": next_slot})

    def _dec_item(self, name, count=1):
        for idx, row in enumerate(self.items):
            if row["name"] == name and row["count"] > 0:
                row["count"] -= count
                if row["count"] <= 0:
                    self.items.pop(idx)
                return True
        return False

    def get_status(self, **kwargs):
        return {"ok": True, "result": copy.deepcopy(self.status)}

    def inventory(self, **kwargs):
        counts = {}
        for r in self.items:
            counts[r["name"]] = counts.get(r["name"], 0) + r["count"]
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
        blk = copy.deepcopy(self.blocks.get(key, {"name": self.default, "properties": {}}))
        blk["pos"] = list(key)
        return {"ok": True, "result": blk}

    def move_to(self, x, y, z, range=1.0, **kwargs):
        self.actions.append(("move_to", [x, y, z]))
        ty = float(self.status["pos"][1]) if y is None else float(y)
        self.status["pos"] = [float(x), ty, float(z)]
        self._pickup_near((int(x // 1), int(ty // 1), int(z // 1)))
        return {"ok": True, "result": {"pos": list(self.status["pos"])}}

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
            above2 = self.blocks.get((bx, by + 2, bz), {"name": self.default}).get("name")
            if above2 in {"air", "torch", "wall_torch"}:
                self.status["pos"][1] = float(by + 1.15)
        else:
            bx, by, bz = [int(self.status["pos"][i] // 1) for i in range(3)]
            below = self.blocks.get((bx, by - 1, bz), {"name": self.default}).get("name")
            if below in {"air", "torch", "wall_torch"} and self.status["pos"][1] > 0:
                self.status["pos"][1] = float(by - 1)
            else:
                self.status["pos"][1] = float(by)
        return {"ok": True, "result": {"sneaking": bool(sneak), "jumping": bool(jump)}}

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

    def dig(self, x, y, z, collect=False, **kwargs):
        self.dig_collect_flags.append(bool(collect))
        key = (int(x), int(y), int(z))
        blk = self.blocks.get(key, {"name": self.default, "properties": {}})
        name = blk.get("name", self.default)
        if name == "air":
            return {"ok": False, "error": {"code": "target_empty", "message": "Air"}}
        self.blocks[key] = {"name": "air", "properties": {}}
        self.actions.append(("dig", list(key), name, bool(collect)))
        drop = ORE_DROP_MAP.get(name, "cobblestone" if name == "stone" else None)
        if drop:
            if collect:
                self._add_item(drop, 1)
            else:
                # Simulate gravity settling drop down through air onto the solid floor below
                dy_pos = key[1]
                while dy_pos > -10:
                    below_name = self.blocks.get((key[0], dy_pos - 1, key[2]), {"name": self.default}).get("name")
                    if below_name not in {"air", "torch", "wall_torch"}:
                        break
                    dy_pos -= 1
                drop_key = (key[0], dy_pos, key[2])
                bag = self.ground_drops.setdefault(drop_key, {})
                bag[drop] = bag.get(drop, 0) + 1
                bot_cell = (int(self.status["pos"][0] // 1), int(self.status["pos"][1] // 1), int(self.status["pos"][2] // 1))
                self._pickup_near(bot_cell)
        return {"ok": True, "result": {"dug": name}}

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
            below = self.blocks.get((key[0], key[1] - 1, key[2]), {"name": self.default}).get("name")
            placed_name = "torch" if below not in {"air", "torch", "wall_torch"} else "wall_torch"
        self.blocks[key] = {"name": placed_name, "properties": {}}
        self.actions.append(("place", list(key), placed_name))
        return {"ok": True, "result": {"placed": placed_name, "pos": list(key)}}

    def stop(self):
        return {"ok": True, "result": {}}


class CaveExploreTests(unittest.TestCase):
    def test_mineshaft_clear_cobweb_only_with_sword_avoids_minecart_and_mines_target(self):
        w = CaveSimWorld(bot_pos=(0.5, 59.0, 0.5))
        # Entry junction + straight mineshaft corridor along +z (z=1..8)
        w.carve([0], [59, 60], range(0, 9), "air")
        w.set_block(0, 58, 1, "oak_planks")
        w.set_block(0, 58, 2, "oak_planks")
        w.set_block(0, 59, 2, "rail")
        w.set_block(1, 59, 2, "oak_fence")
        w.set_block(0, 59, 3, "cobweb")  # blocks corridor beyond z=2 unless sword/shears present
        w.set_block(0, 59, 8, "chest_minecart")  # must not be broken or entered
        w.set_block(-1, 60, 7, "iron_ore")  # >4.5 blocks from z=2, reachable only after clearing cobweb
        w.set_block(1, 60, 7, "copper_ore")  # unlisted -> seen

        # Without sword or shears -> stops before cobweb (never clears it or mines z=7 ore)
        res_no_sword = CaveExploreSkill(w).execute(
            entry_pos=[0, 59, 0],
            origin_air=[0, 59, 1],
            targets={"iron": {"count": 4, "min_tool": "stone_pickaxe"}},
        )
        self.assertTrue(res_no_sword.success, res_no_sword)
        self.assertEqual(w.blocks[(0, 59, 3)]["name"], "cobweb")
        self.assertEqual(w.blocks[(-1, 60, 7)]["name"], "iron_ore")
        self.assertEqual(res_no_sword.data["caves"][0]["kind"], "mineshaft")
        self.assertGreaterEqual(res_no_sword.data["torches_placed"], 1)

        # Give iron_sword and re-explore -> clears cobweb, collects iron, avoids chest_minecart, returns to [0, 59, 0]
        w._add_item("iron_sword", 1)
        res = CaveExploreSkill(w).execute(
            entry_pos=[0, 59, 0],
            origin_air=[0, 59, 1],
            targets={"iron": {"count": 4, "min_tool": "stone_pickaxe"}},
        )
        self.assertTrue(res.success, res)
        self.assertEqual(w.blocks[(0, 59, 3)]["name"], "air")
        self.assertEqual(w.blocks[(0, 59, 8)]["name"], "chest_minecart")
        self.assertEqual(w.blocks[(-1, 60, 7)]["name"], "air")
        ores_by_pos = {tuple(o["pos"]): o["status"] for o in res.data["ores"]}
        self.assertEqual(ores_by_pos[(-1, 60, 7)], "mined")
        self.assertEqual(ores_by_pos[(1, 60, 7)], "seen")
        self.assertAllDigsCollectFalse(w.dig_collect_flags)
        self.assertAlmostEqual(w.status["pos"][0], 0.5, delta=0.5)
        self.assertAlmostEqual(w.status["pos"][2], 0.5, delta=0.5)

    def test_wide_low_cave_enforces_radius_torch_cadence_tool_quota_and_hazard_rules(self):
        w = CaveSimWorld(bot_pos=(0.5, 59.0, 0.5))
        # Flat wide cavern: z=1..14, x=-2..10, y=59..64 (height=6 < 10, > 160 air blocks)
        w.carve([0], [59, 60], [0], "air")
        w.carve(range(-2, 11), range(59, 65), range(1, 15), "air")
        # Iron target quota=2 (seed 3 accessible iron ores along x=-3 wall so 3rd becomes skipped_quota)
        w.set_block(1, 60, 0, "stone")
        w.set_block(-3, 60, 2, "iron_ore")
        w.set_block(-3, 60, 3, "deepslate_iron_ore")
        w.set_block(-3, 60, 4, "iron_ore")
        # Diamond needs iron_pickaxe -> skipped_tool with stone_pickaxe
        w.set_block(-3, 59, 5, "diamond_ore")
        # Exposed iron in x=-3 wall adjacent to hidden lava behind it at (-4, 60, 1) -> skipped_hazard
        w.set_block(-3, 60, 1, "iron_ore")
        w.set_block(-4, 60, 1, "lava")

        res = CaveExploreSkill(w).explore_cave(
            entry_pos=[0, 59, 0],
            origin_air=[0, 59, 1],
            targets={
                "iron": {"count": 2, "min_tool": "stone_pickaxe"},
                "diamond": {"count": 2, "min_tool": "iron_pickaxe"},
            },
            cave_policy={"explore_max_visited": 25, "explore_radius": 12},
        )
        self.assertTrue(res.success, res)
        self.assertEqual(res.data["caves"][0]["kind"], "small")
        self.assertLessEqual(res.data["visited_count"], 25)
        self.assertGreaterEqual(res.data["torches_placed"], 1)
        statuses = {tuple(o["pos"]): o["status"] for o in res.data["ores"]}
        mined_count = sum(1 for s in statuses.values() if s == "mined")
        self.assertEqual(mined_count, 2)
        self.assertIn("skipped_quota", statuses.values())
        self.assertEqual(statuses.get((-3, 59, 5)), "skipped_tool")
        self.assertEqual(statuses.get((-3, 60, 1)), "skipped_hazard")
        self.assertAllDigsCollectFalse(w.dig_collect_flags)

    def test_missing_torch_rejected_and_home_trigger_stops_cleanly(self):
        # 1a. Default policy enters safe cave even with 0 torches (never refuses cave for lack of torches)
        w_no_torch = CaveSimWorld(bot_pos=(0.5, 59.0, 0.5))
        w_no_torch._dec_item("torch", 16)
        w_no_torch.carve([0], [59, 60], [0], "air")
        w_no_torch.carve(range(-1, 3), range(59, 63), range(1, 6), "air")
        w_no_torch.set_block(2, 60, 2, "iron_ore")
        r_enter = CaveExploreSkill(w_no_torch).explore_cave(
            entry_pos=[0, 59, 0],
            origin_air=[0, 59, 1],
            targets={"iron": {"count": 1, "min_tool": "stone_pickaxe"}},
        )
        self.assertTrue(r_enter.success, r_enter)
        self.assertGreaterEqual(r_enter.data["harvested"].get("raw_iron", 0), 1)

        # 1b. Explicit min_enter_torches=1 still enforces missing_torch gate when requested
        w_gated = CaveSimWorld(bot_pos=(0.5, 59.0, 0.5))
        w_gated._dec_item("torch", 16)
        w_gated.carve([0], [59, 60], [0], "air")
        w_gated.carve(range(-1, 3), range(59, 63), range(1, 6), "air")
        r_nt = CaveExploreSkill(w_gated).explore_cave(
            entry_pos=[0, 59, 0],
            origin_air=[0, 59, 1],
            cave_policy={"min_enter_torches": 1},
        )
        self.assertFalse(r_nt.success)
        self.assertEqual(r_nt.error_code, "missing_torch")

        # 2. Low health triggers return_home and safe breadcrumb return
        w_low = CaveSimWorld(bot_pos=(0.5, 59.0, 0.5))
        w_low.carve([0], [59, 60], [0], "air")
        w_low.carve(range(-1, 3), range(59, 62), range(1, 8), "air")
        w_low.status["health"] = 10
        r_home = CaveExploreSkill(w_low).explore_cave(entry_pos=[0, 59, 0], origin_air=[0, 59, 1])
        self.assertTrue(r_home.success)
        self.assertEqual(r_home.data["stop_reason"], "return_home")
        self.assertEqual(r_home.data["home_reason"], "low_health")

    def test_minor_damage_and_lava_bridge_and_surface_escape(self):
        # Losing 1-2 HP + encountering lava inside a cave must NOT abort exploration;
        # bridges lava with stone, respects 24-block torch cadence, and finishes ores cleanly.
        class _ChipDamageWorld(CaveSimWorld):
            def __init__(self, **kw):
                super().__init__(**kw)
                self._moves = 0

            def move_to(self, x, y, z, range=1.0, **kwargs):
                self._moves += 1
                if self._moves == 2:
                    self.status["health"] = 18  # 2 HP chip damage
                return super().move_to(x, y, z, range=range, **kwargs)

        w = _ChipDamageWorld(bot_pos=(0.5, 59.0, 0.5))
        w.carve([0], [59, 60], [0], "air")
        w.carve(range(-1, 2), range(59, 63), range(1, 8), "air")
        for x in (-1, 0, 1):
            w.set_block(x, 58, 3, "lava")  # lava gap across tunnel at z=3
        w.set_block(0, 60, 8, "iron_ore")  # ore past the lava bridge (need to bridge z=3!)
        res = CaveExploreSkill(w).execute(
            entry_pos=[0, 59, 0],
            origin_air=[0, 59, 1],
            targets={"iron": {"count": 1, "min_tool": "stone_pickaxe"}},
            cave_policy={"explore_max_visited": 40, "explore_radius": 12},
        )
        self.assertTrue(res.success, res)
        self.assertEqual(res.data["caves"][0]["kind"], "hazard_subgraph")
        self.assertGreaterEqual(res.data["harvested"].get("raw_iron", 0), 1)
        # Never spams torches along a short <24-block tunnel and never circles unnecessarily
        self.assertLessEqual(res.data["torches_placed"], 1)
        self.assertLessEqual(res.data["visited_count"], 15)

        # Trapped return path triggers pillar-up surface escape instead of stalling forever underground
        class _TrappedBackWorld(CaveSimWorld):
            def move_to(self, x, y, z, range=1.0, **kwargs):
                # Block returning to z <= 1 once past z >= 2
                if float(self.status["pos"][2]) >= 2.0 and float(z) < 2.0:
                    return {"ok": False, "error": {"code": "stalled", "message": "collapsed behind"}}
                return super().move_to(x, y, z, range=range, **kwargs)

        w_esc = _TrappedBackWorld(bot_pos=(0.5, 59.0, 0.5))
        w_esc.carve([0], [59, 60], range(0, 4), "air")
        w_esc.carve(range(-1, 2), range(62, 85), range(2, 5), "air")  # realistic surface sky above 1-block roof y=61
        w_esc.set_block(0, 60, 4, "iron_ore")
        r_esc = CaveExploreSkill(w_esc).explore_cave(
            entry_pos=[0, 59, 0],
            origin_air=[0, 59, 1],
            targets={"iron": {"count": 1, "min_tool": "stone_pickaxe"}},
            allow_pillar=True,
            allow_micro_dig=True,
            allow_surface_escape=True,
        )
        self.assertTrue(r_esc.success, r_esc)
        self.assertTrue(r_esc.data["escaped_to_surface"])
        self.assertGreaterEqual(w_esc.status["pos"][1], 61.0)

    def test_tall_cave_subgraph_uses_pillar_up_and_pillar_down_to_mine_high_ores(self):
        w = CaveSimWorld(bot_pos=(0.5, 59.0, 0.5))
        # Tall cavern: height=12 (y=59..70), safe ground y=59 plus a 2-block high shelf at x=2, y=61 with iron_ore at (3, 66..67, 1) (>4.5 from floor y=59)
        w.carve([0], [59, 60], [0], "air")
        w.carve(range(-1, 5), range(59, 71), range(1, 5), "air")
        w.set_block(2, 59, 1, "stone")
        w.set_block(2, 60, 1, "stone")
        w.set_block(3, 66, 1, "iron_ore")
        w.set_block(3, 67, 1, "iron_ore")
        # Keep drop reachable cleanly from elevated shelf [2, 61, 1] -> [3, 61, 1] above solid (3, 60, 1)
        w.set_block(3, 60, 1, "stone")
        res = CaveExploreSkill(w).explore_cave(
            entry_pos=[0, 59, 0],
            origin_air=[0, 59, 1],
            targets={"iron": {"count": 2, "min_tool": "stone_pickaxe"}},
            cave_policy={"explore_max_visited": 30, "explore_radius": 8},
            allow_tall_cave_subgraph=True,
            allow_pillar=True,
            allow_micro_dig=True,
        )
        self.assertTrue(res.success, res)
        self.assertEqual(res.data["caves"][0]["kind"], "tall_subgraph")
        mined = [o for o in res.data["ores"] if o["status"] == "mined"]
        self.assertEqual(len(mined), 2)
        self.assertGreaterEqual(res.data["harvested"].get("raw_iron", 0), 2)
        # Pillar blocks placed to climb up should be cleanly unwound back to entry_pos [0, 59, 0]
        self.assertAlmostEqual(w.status["pos"][1], 59.0, delta=0.5)
        self.assertAllDigsCollectFalse(w.dig_collect_flags)

    def test_discover_and_explore_caves_recovers_from_slope_stall_and_reaches_quota(self):
        class _SlopeStallWorld(CaveSimWorld):
            def move_to(self, x, y, z, range=1.0, **kwargs):
                # Reject long direct Baritone hops to simulate steep slope stall, allow 1..2 block local steps
                bx, by, bz = self.status["pos"]
                if abs(float(x) - bx) + abs(float(y) - by) + abs(float(z) - bz) > 2.5:
                    return {"ok": False, "error": {"code": "stalled", "message": "slope stalled"}}
                return super().move_to(x, y, z, range=range, **kwargs)

        w = _SlopeStallWorld(bot_pos=(0.5, 65.0, 0.5))
        # Surface floor y=64 from x=0..4, with 1-block ledge y=65 at x=4 leading into a natural stone cavern at x=4..10
        w.carve(range(0, 12), range(65, 70), range(-2, 4), "air")
        for x in range(0, 4):
            w.set_block(x, 64, 0, "stone")
        # 2-block rise at x=4 -> shelf at y=66 (requires pillar_up at x=3,65,0 -> 67 -> step onto x=4,67,0 or micro-dig!)
        w.set_block(3, 65, 0, "stone")  # ledge step +1 (reachable walk dy=1)
        w.set_block(4, 65, 0, "stone")
        w.set_block(4, 66, 0, "stone")  # dy=+2 cliff before cave mouth at [5, 67, 0]
        w.set_block(4, 66, 1, "stone")
        w.set_block(4, 66, -1, "stone")
        w.carve(range(5, 11), range(67, 71), range(-1, 2), "air")
        for cx in range(5, 11):
            for cz in (-1, 0, 1):
                w.set_block(cx, 66, cz, "stone")
            w.set_block(cx, 71, 0, "stone")  # stone roof over cave
        w.set_block(8, 67, 1, "iron_ore")
        w.set_block(8, 68, 1, "iron_ore")
        w.set_block(9, 67, 1, "iron_ore")

        res = CaveExploreSkill(w).discover_and_explore_caves(
            origin=[0, 65, 0],
            search_radius=12,
            max_caves=2,
            targets={"iron": {"count": 3, "min_tool": "stone_pickaxe"}},
        )
        self.assertTrue(res.success, res)
        self.assertEqual(res.data["stop_reason"], "target_reached")
        self.assertGreaterEqual(res.data["harvested"].get("raw_iron", 0), 3)

    def test_wide_chamber_avoids_circling_and_shortcuts_retreat(self):
        w = CaveSimWorld(bot_pos=(0.5, 59.0, 0.5))
        # Wide open 9x9 cave room at y=59 (entry [0, 59, 0], far ore at [7, 59, 7]) plus 1-block shelf at [1, 60, 0]
        w.carve(range(-1, 9), range(59, 63), range(-1, 9), "air")
        for x in range(-1, 9):
            for z in range(-1, 9):
                w.set_block(x, 58, z, "stone")
        w.set_block(7, 59, 7, "iron_ore")

        res = CaveExploreSkill(w).explore_cave(
            entry_pos=[0, 59, 0],
            origin_air=[0, 59, 1],
            targets={"iron": {"count": 1, "min_tool": "stone_pickaxe"}},
            cave_policy={"explore_max_visited": 30, "explore_radius": 12},
            prefer_flood_planner=True,
            allow_pillar=True,
            allow_micro_dig=True,
        )
        self.assertTrue(res.success, res)
        self.assertGreaterEqual(res.data["harvested"].get("raw_iron", 0), 1)
        moves = [tuple(int(v // 1) for v in a[1]) for a in w.actions if a[0] == "move_to"]
        # Sweeps broad floor area (>=20 unique cells) and shortcuts directly back to entry [0, 59, 0] (no 1-by-1 reverse spiral)
        self.assertEqual(moves[-1], (0, 59, 0))
        self.assertGreaterEqual(len(set(moves)), 20, f"Bot trapped in tight circle: moves={moves}")
        self.assertGreater(abs(moves[-2][0] - 0) + abs(moves[-2][2] - 0), 3, f"Expected direct shortcut retreat, got {moves[-4:]}")

    def test_navigate_with_ops_fails_fast_on_unconnected_pocket(self):
        class _DisconnectWorld(CaveSimWorld):
            def move_to(self, x, y, z, range=1.0, **kwargs):
                self.actions.append(("move_to", [x, y, z]))
                return {"ok": False, "error": {"code": "path_not_found", "message": "No direct path"}}

        w = _DisconnectWorld(bot_pos=(0.5, 59.0, 0.5))
        # Closed 5x5 chamber at y=59; target [0, 48, 8] is an isolated lower air pocket with zero connecting path
        w.carve(range(-2, 3), range(59, 62), range(-2, 3), "air")
        for x in range(-2, 3):
            for z in range(-2, 3):
                w.set_block(x, 58, z, "stone")
        w.carve(range(-1, 2), range(48, 51), range(7, 10), "air")

        res = CaveExploreSkill(w).navigate_with_ops([0, 48, 8], max_steps=16, timeout_seconds=15.0)
        self.assertFalse(res.success)
        # Only the initial direct move_to probe should happen; zero wasted fallback steps inside already-bounded room
        self.assertEqual(len([a for a in w.actions if a[0] == "move_to"]), 1)

    def test_mine_vein_picks_up_lower_floor_ore_drops(self):
        w = CaveSimWorld(bot_pos=(0.5, 60.0, 0.5))
        # Upper shelf y=60 at z=0, step down z=1 (floor y=59), pit z=2 with exposed iron_ore at (0, 58, 2) (dy=-2 below stance y=60)
        w.carve([-1, 0, 1], range(59, 64), range(0, 4), "air")
        w.set_block(0, 59, 0, "stone")
        w.set_block(0, 58, 1, "stone")
        w.set_block(0, 57, 2, "stone")
        w.set_block(0, 58, 2, "iron_ore")

        res = CaveExploreSkill(w).explore_cave(
            entry_pos=[0, 60, 0],
            origin_air=[0, 60, 1],
            targets={"iron": {"count": 1, "min_tool": "stone_pickaxe"}},
            cave_policy={"explore_max_visited": 12, "explore_radius": 6},
        )
        self.assertTrue(res.success, res)
        self.assertEqual(w.blocks[(0, 58, 2)]["name"], "air")
        self.assertGreaterEqual(res.data["harvested"].get("raw_iron", 0), 1)
        self.assertAllDigsCollectFalse(w.dig_collect_flags)

    def assertAllDigsCollectFalse(self, flags):
        self.assertTrue(all(f is False for f in flags), f"Expected all dig collect=False, got {flags}")


if __name__ == "__main__":
    unittest.main()
