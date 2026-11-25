"""Offline simulator tests for NetherPortalSpeedrunSkill.build_portal."""

import threading
import unittest

from core.client import APIError
from core.structure_spec import CARDINAL_ORIENTATIONS, NETHER_PORTAL_SPEC, transform_rel_pos
from skills.portal_speedrun import NetherPortalSpeedrunSkill


class _SimulatedPortalWorldClient:
    """In-memory Minecraft world mock supporting 4-way portal casting & ignition."""

    def __init__(self, origin, orientation, *, items=None):
        self.origin = list(origin)
        self.orientation = orientation
        self.pos = transform_rel_pos(
            NETHER_PORTAL_SPEC.anchors["player_start"].rel_pos,
            self.origin,
            self.orientation,
            integer=False,
        )
        self.health = 20.0
        self.held = None
        self.blocks = {}
        self.items = dict(
            items
            or {
                "cobblestone": 16,
                "water_bucket": 1,
                "lava_bucket": 6,
                "bucket": 1,
                "flint_and_steel": 1,
                "diamond_pickaxe": 1,
            }
        )
        self.spill_once = False
        self.ignite_fail_once = False
        self.timeout_on_interact_step = None
        self._interact_count = 0
        self._water_flows = {}

        # Pre-seed base, shore, and front walkway by default (tests can override)
        for dx in range(-1, 5):
            for dz in (-2, -1):
                self.set_rel_block((dx, 0, dz), "stone")
        for dx in (1, 2):
            self.set_rel_block((dx, -2, 0), "stone")
            self.set_rel_block((dx, 0, 1), "obsidian")
        for rel in ((1, -1, 0), (2, -1, 0), (0, 0, 0), (3, 0, 0)):
            self.set_rel_block(rel, "obsidian")

    def find_blocks(self, types, radius=16, limit=64, **kwargs):
        wanted = set(types or ())
        bx, by, bz = [float(v) for v in self.pos]
        r2 = float(radius) ** 2
        hits = []
        for (x, y, z), blk in self.blocks.items():
            if blk.get("name") not in wanted:
                continue
            d2 = (x - bx) ** 2 + (y - by) ** 2 + (z - bz) ** 2
            if d2 <= r2:
                hits.append({"name": blk["name"], "pos": [x, y, z], "dist": round(d2 ** 0.5, 2)})
        hits.sort(key=lambda h: h["dist"])
        return hits[: int(limit)]

    def wpos(self, rel):
        return tuple(transform_rel_pos(rel, self.origin, self.orientation, integer=True))

    def set_rel_block(self, rel, name):
        wp = self.wpos(rel)
        self.blocks[wp] = {"name": name}
        if name == "water":
            self._propagate_water_from(wp)

    def _propagate_water_from(self, wp):
        flows = []
        below = (wp[0], wp[1] - 1, wp[2])
        if self.blocks.get(below, {"name": "air"})["name"] in {"air", "lava"}:
            seeds = [wp]
            for d in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1)):
                adj = (wp[0] + d[0], wp[1], wp[2] + d[2])
                if self.blocks.get(adj, {"name": "air"})["name"] == "air":
                    self.blocks[adj] = {"name": "flowing_water"}
                    flows.append(adj)
                    seeds.append(adj)
            for sx, sy, sz in seeds:
                for dy in (-1, -2):
                    c = (sx, sy + dy, sz)
                    if self.blocks.get(c, {"name": "air"})["name"] != "air":
                        break
                    self.blocks[c] = {"name": "flowing_water"}
                    flows.append(c)
                    under = (c[0], c[1] - 1, c[2])
                    if self.blocks.get(under, {"name": "air"})["name"] not in {"air", "flowing_water", "water"}:
                        for d in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1)):
                            side = (c[0] + d[0], c[1], c[2] + d[2])
                            if self.blocks.get(side, {"name": "air"})["name"] == "air":
                                self.blocks[side] = {"name": "flowing_water"}
                                flows.append(side)
                                drop = (side[0], side[1] - 1, side[2])
                                if self.blocks.get(drop, {"name": "air"})["name"] == "air":
                                    self.blocks[drop] = {"name": "flowing_water"}
                                    flows.append(drop)
        self._water_flows[wp] = flows

    def get_rel_block(self, rel):
        return self.blocks.get(self.wpos(rel), {"name": "air"})["name"]

    def get_status(self, timeout_seconds=None):
        return {
            "ok": True,
            "result": {
                "pos": list(self.pos),
                "health": self.health,
                "alive": True,
                "ready": True,
                "dimension": "overworld",
                "last_death": None,
                "in_lava": False,
                "on_fire": False,
                "held": {"name": self.held} if self.held else {},
            },
        }

    def inventory(self, timeout_seconds=None):
        return {
            "ok": True,
            "result": {"items": [{"name": k, "count": v} for k, v in self.items.items() if v > 0]},
        }

    def get_inventory_counts(self):
        return {k: v for k, v in self.items.items() if v > 0}

    def get_block(self, x, y, z):
        return {"ok": True, "result": dict(self.blocks.get((int(x), int(y), int(z)), {"name": "air"}))}

    def move_to(self, x, y=None, z=None, range=1.0):
        self.pos = [float(x), self.pos[1] if y is None else float(y), float(z)]
        return {"ok": True, "result": {"moved": True}}

    def look_at(self, x, y, z):
        return {"ok": True, "result": {"looked": [x, y, z]}}

    def posture(self, sneak=False, shield=False, jump=False):
        return {"ok": True, "result": {"sneak": sneak}}

    def equip(self, item, slot="hand"):
        if self.items.get(item, 0) <= 0:
            raise APIError("missing_item", f"Cannot equip missing item {item}")
        self.held = item
        return {"ok": True, "result": {"equipped": item}}

    def place_block(self, item=None, x=0, y=0, z=0):
        if self.items.get(item, 0) <= 0:
            raise APIError("missing_item", f"No {item}")
        self.items[item] -= 1
        self.blocks[(int(x), int(y), int(z))] = {"name": item}
        return {"ok": True, "result": {"placed": item}}

    def dig(self, x, y, z, collect=False, stop_on=None):
        self.blocks[(int(x), int(y), int(z))] = {"name": "air"}
        return {"ok": True, "result": {"dug": True}}

    def interact(self, pos=None, entity_id=None, item=None, ref=None, face=None, stop_on=None):
        self._interact_count += 1
        if self.timeout_on_interact_step == self._interact_count:
            raise APIError("request_timeout", "interact timeout", details={"action_state": "unknown"})

        wp = (int(pos[0]), int(pos[1]), int(pos[2]))
        if item == "water_bucket":
            self.items["water_bucket"] -= 1
            self.items["bucket"] = self.items.get("bucket", 0) + 1
            self.blocks[wp] = {"name": "water", "properties": {"level": 0}}
            if self.spill_once and wp == self.wpos((2, 3, -1)):
                self.spill_once = False
                self.blocks[(wp[0], wp[1] + 1, wp[2])] = {"name": "water", "properties": {"level": 0}}
            self._propagate_water_from(wp)
            # Freeze orthogonal lava neighbors of source (real contact only)
            for d in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                nb = (wp[0] + d[0], wp[1] + d[1], wp[2] + d[2])
                if self.blocks.get(nb, {}).get("name") == "lava":
                    self.blocks[nb] = {"name": "obsidian"}
        elif item == "bucket":
            self.items["bucket"] -= 1
            if self.blocks.get(wp, {}).get("name") == "lava":
                self.items["lava_bucket"] = self.items.get("lava_bucket", 0) + 1
            else:
                self.items["water_bucket"] = self.items.get("water_bucket", 0) + 1
            self.blocks[wp] = {"name": "air"}
            for fc in self._water_flows.pop(wp, []):
                if self.blocks.get(fc, {}).get("name") == "flowing_water":
                    self.blocks[fc] = {"name": "air"}
            # Contained doorway dig/flow step freezes adjacent corners when water flows to (2,0,0)
            if wp == self.wpos((1, 0, 0)):
                for rel in ((3, 0, 0), (2, -1, 0), (2, 0, 1)):
                    if self.get_rel_block(rel) == "lava":
                        self.set_rel_block(rel, "obsidian")
        elif item == "lava_bucket":
            self.items["lava_bucket"] -= 1
            self.items["bucket"] = self.items.get("bucket", 0) + 1
            # Real Minecraft converts placed lava directly to obsidian if any orthogonal neighbor is water.
            has_adj_water = any(
                self.blocks.get((wp[0] + d[0], wp[1] + d[1], wp[2] + d[2]), {}).get("name") in {"water", "flowing_water"}
                for d in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
            )
            if has_adj_water:
                self.blocks[wp] = {"name": "obsidian"}
            else:
                self.blocks[wp] = {"name": "lava"}
        elif item == "flint_and_steel":
            if self.ignite_fail_once:
                self.ignite_fail_once = False
                return {"ok": True, "result": {"ignited": False}}
            # Activate 6 interior nether_portal blocks if any 4x5 obsidian frame around clicked base matches
            fx, fy, fz = wp
            for ax, az in ((1, 0), (0, 1), (-1, 0), (0, -1)):
                activated = False
                for shift in (1, 2):
                    ox, oy, oz = fx - shift * ax, fy + 1, fz - shift * az
                    frame_cells = (
                        (1, -1), (2, -1), (0, 0), (3, 0), (0, 1), (3, 1), (0, 2), (3, 2), (1, 3), (2, 3),
                    )
                    frame_ok = all(
                        self.blocks.get((ox + d * ax, oy + dy, oz + d * az), {}).get("name") == "obsidian"
                        for d, dy in frame_cells
                    )
                    air_ok = all(
                        self.blocks.get((ox + d * ax, oy + dy, oz + d * az), {}).get("name") in (None, "air", "cave_air")
                        for d in (1, 2)
                        for dy in (0, 1, 2)
                    )
                    if frame_ok and air_ok:
                        for d in (1, 2):
                            for dy in (0, 1, 2):
                                self.blocks[(ox + d * ax, oy + dy, oz + d * az)] = {"name": "nether_portal"}
                        activated = True
                        break
                if activated:
                    break
        return {"ok": True, "result": {"interacted": True}}


class PortalWorkflowTests(unittest.TestCase):
    def test_build_portal_succeeds_across_all_four_orientations(self):
        origin = [20, 64, -10]
        for orientation in CARDINAL_ORIENTATIONS:
            with self.subTest(orientation=orientation):
                client = _SimulatedPortalWorldClient(origin, orientation)
                skill = NetherPortalSpeedrunSkill(client)
                res = skill.build_portal(origin, orientation=orientation)
                self.assertTrue(res.success, f"{orientation} failed: {res.message}")
                self.assertTrue(res.data["structure_verified"])
                self.assertEqual(res.data["orientation"], orientation)
                report = skill.inspect_portal(origin, orientation=orientation, include_scaffold=False)
                self.assertTrue(report["frame_complete"])
                self.assertTrue(report["portal_active"])
                self.assertEqual(client.get_rel_block((2, 3, -1)), "air")

    def test_idempotent_resume_skips_completed_base_and_scaffold(self):
        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(
            origin,
            "east",
            items={"lava_bucket": 4, "bucket": 1, "flint_and_steel": 1, "cobblestone": 1, "diamond_pickaxe": 1},
        )
        # Pre-seed scaffold, water curtain, and top beam
        for rule in NETHER_PORTAL_SPEC.cells:
            if rule.stage == "scaffold":
                client.set_rel_block(rule.rel_pos, "cobblestone")
        client.set_rel_block((2, 3, -1), "water")
        client.set_rel_block((2, 3, 0), "obsidian")
        client.set_rel_block((1, 3, 0), "obsidian")

        skill = NetherPortalSpeedrunSkill(client)
        res = skill.build_portal(origin, orientation="east")
        self.assertTrue(res.success, res.message)
        self.assertTrue(res.data["structure_verified"])
        self.assertEqual(client.items["lava_bucket"], 0)

    def test_local_branches_handle_base_stone_spill_puddle_and_ignition_retry(self):
        origin = [5, 64, 5]
        client = _SimulatedPortalWorldClient(
            origin,
            "north",
            items={
                "cobblestone": 8,
                "water_bucket": 1,
                "lava_bucket": 8,  # 6 for frame + 2 for base stone replacement
                "bucket": 1,
                "flint_and_steel": 1,
                "diamond_pickaxe": 1,
            },
        )
        # Non-obsidian base blocks at (1,-1,0) and (2,-1,0)
        client.set_rel_block((1, -1, 0), "stone")
        client.set_rel_block((2, -1, 0), "cobblestone")
        # Trigger Y+1 spill during water curtain placement and 1 missed ignition
        client.spill_once = True
        client.ignite_fail_once = True

        skill = NetherPortalSpeedrunSkill(client)
        res = skill.build_portal(origin, orientation="north")
        self.assertTrue(res.success, res.message)
        self.assertTrue(res.data["structure_verified"])
        self.assertEqual(client.get_rel_block((1, -1, 0)), "obsidian")
        self.assertEqual(client.get_rel_block((2, -1, 0)), "obsidian")
        self.assertEqual(client.get_rel_block((2, 4, -1)), "air")

    def test_missing_lava_buckets_returns_structured_ai_diagnostic(self):
        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(
            origin,
            "south",
            items={"cobblestone": 10, "water_bucket": 1, "lava_bucket": 2, "flint_and_steel": 1},
        )
        skill = NetherPortalSpeedrunSkill(client)
        res = skill.build_portal(origin, orientation="south")
        self.assertFalse(res.success)
        self.assertEqual(res.error_code, "missing_lava_buckets")
        self.assertFalse(res.data["structure_verified"])
        self.assertEqual(res.data["blocker_type"], "missing_item")
        self.assertEqual(res.data["suggested_recovery"], "gather_lava_buckets")
        self.assertEqual(client._interact_count, 0)

    def test_timeout_during_interact_halts_with_unknown_action_state(self):
        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(origin, "west")
        client.timeout_on_interact_step = 1
        skill = NetherPortalSpeedrunSkill(client)
        res = skill.build_portal(origin, orientation="west")
        self.assertFalse(res.success)
        self.assertEqual(res.error_code, "request_timeout")
        self.assertEqual(res.data["action_state"], "unknown")
        self.assertEqual(res.data["blocker_type"], "uncertain_write")

    def test_cancel_event_stops_cleanly(self):
        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        cancel = threading.Event()
        cancel.set()
        skill = NetherPortalSpeedrunSkill(client)
        res = skill.build_portal(origin, orientation="south", cancel_event=cancel)
        self.assertFalse(res.success)
        self.assertEqual(res.error_code, "cancelled")
        self.assertEqual(res.data["blocker_type"], "safety_interrupted")

    def test_build_portal_from_clean_lava_pool_with_zero_prefilled_lava_buckets(self):
        origin = [10, 64, 10]
        client = _SimulatedPortalWorldClient(
            origin,
            "west",
            items={
                "cobblestone": 16,
                "water_bucket": 1,
                "bucket": 1,
                "flint_and_steel": 1,
                "diamond_pickaxe": 1,
            },
        )
        # Replace pre-seeded obsidian base with a complete untouched 4x4x2 sunken lava pool
        for dx in range(4):
            for dz in range(4):
                for dy in (-1, 0):
                    client.set_rel_block((dx, dy, dz), "lava")

        skill = NetherPortalSpeedrunSkill(client)
        res = skill.build_portal(origin, orientation="west")
        self.assertTrue(res.success, f"west zero-lava failed: {res.message}")
        self.assertTrue(res.data["structure_verified"])
        # Confirm open molten lava remains in the pool (did not freeze the whole pool)
        remaining_lava = sum(
            1
            for dx in range(4)
            for dz in range(2, 4)
            if client.get_rel_block((dx, 0, dz)) == "lava"
        )
        self.assertGreaterEqual(remaining_lava, 1)

    def test_auto_find_site_and_build_on_shallow_deepslate_lava_pool(self):
        origin = [-370, -55, 62]
        client = _SimulatedPortalWorldClient(
            origin,
            "east",
            items={
                "cobblestone": 20,
                "water_bucket": 1,
                "bucket": 2,
                "flint_and_steel": 1,
                "diamond_pickaxe": 1,
            },
        )
        # Clear pre-seeded obsidian base and craft a 1-block-deep 4x4 natural deepslate lava pool
        for dx in range(-2, 6):
            for dz in range(-3, 5):
                client.set_rel_block((dx, -2, dz), "deepslate")
                client.set_rel_block((dx, -1, dz), "deepslate")
                is_pool = 0 <= dx <= 3 and 0 <= dz <= 3
                client.set_rel_block((dx, 0, dz), "lava" if is_pool else "deepslate")
        # Bot starts nearby on the east shore edge, NOT at any hardcoded origin
        client.pos = [origin[0] - 1.5, float(origin[1] + 1), origin[2] - 1.5]

        skill = NetherPortalSpeedrunSkill(client)
        site = skill.find_portal_site()
        self.assertIsNotNone(site)
        res = skill.build_portal()  # origin=None auto-detects nearby lava pool edge!
        self.assertTrue(res.success, f"auto site shallow pool failed: {res.message}")
        self.assertTrue(res.data["structure_verified"])
        self.assertEqual(res.data["orientation"], "east")

    def test_site_finder_finds_distant_lava_pool_via_seed_blocks_and_walks_over(self):
        origin = [40, 64, 10]
        client = _SimulatedPortalWorldClient(
            origin,
            "south",
            items={
                "cobblestone": 24,
                "water_bucket": 1,
                "bucket": 2,
                "flint_and_steel": 1,
                "diamond_pickaxe": 1,
            },
        )
        # Replace pre-seeded portal obsidian with a full 4x4 lava pool at origin
        for dx in range(-2, 6):
            for dz in range(-3, 5):
                client.set_rel_block((dx, -2, dz), "stone")
                is_pool = 0 <= dx <= 3 and 0 <= dz <= 3
                client.set_rel_block((dx, -1, dz), "lava" if is_pool else "stone")
                client.set_rel_block((dx, 0, dz), "lava" if is_pool else "stone")
        # Start 12 blocks away on north/south shore; find_blocks -> StructureSpec site finder walks over and builds
        client.pos = [42.5, 65.0, -3.5]

        skill = NetherPortalSpeedrunSkill(client)
        res = skill.build_portal()
        self.assertTrue(res.success, f"distant site walkover failed: {res.message}")
        self.assertTrue(res.data["structure_verified"])
        self.assertEqual(res.data["origin"][1], 64)
        self.assertTrue(39 <= res.data["origin"][0] <= 44 and 9 <= res.data["origin"][2] <= 14)

    def test_clears_overhead_cave_ceiling_blocks_before_building(self):
        origin = [12, -55, 24]
        client = _SimulatedPortalWorldClient(
            origin,
            "west",
            items={
                "cobblestone": 20,
                "water_bucket": 1,
                "bucket": 2,
                "flint_and_steel": 1,
                "diamond_pickaxe": 1,
            },
        )
        for dx in range(-2, 6):
            for dz in range(-3, 5):
                client.set_rel_block((dx, -2, dz), "deepslate")
                client.set_rel_block((dx, -1, dz), "deepslate")
                is_pool = 0 <= dx <= 3 and 0 <= dz <= 3
                client.set_rel_block((dx, 0, dz), "lava" if is_pool else "deepslate")
        # Low cave ceiling hanging down across structure and temp_stand headroom
        for rel in ((1, 2, 0), (2, 3, 0), (3, 3, 0), (2, 3, -1), (1, 2, 1), (1, 3, 1)):
            client.set_rel_block(rel, "tuff")
        client.set_rel_block((2, 4, -1), "gravel")
        client.pos = list(transform_rel_pos((2.5, 1.0, -1.5), origin, "west", integer=False))

        skill = NetherPortalSpeedrunSkill(client)
        res = skill.build_portal(origin, orientation="west")
        self.assertTrue(res.success, f"overhead cave ceiling test failed: {res.message}")
        self.assertTrue(res.data["structure_verified"])
        self.assertEqual(client.get_rel_block((1, 2, 1)), "air")
        self.assertEqual(client.get_rel_block((1, 3, 1)), "air")

    def test_narrow_lava_pool_bridges_shore_and_front_walkway(self):
        origin = [-15, 64, 30]
        client = _SimulatedPortalWorldClient(
            origin,
            "north",
            items={
                "cobblestone": 28,
                "water_bucket": 1,
                "bucket": 2,
                "flint_and_steel": 1,
                "diamond_pickaxe": 1,
            },
        )
        for dx in range(-2, 6):
            for dz in range(-3, 5):
                client.set_rel_block((dx, -2, dz), "stone")
                client.set_rel_block((dx, -1, dz), "stone")
                client.set_rel_block((dx, 0, dz), "stone")
        # Narrow lava channel: only dz=0 (plus side lava at dx=3..4, dz=0..2) instead of full 4x4 sheet,
        # and notch out shore cells (2,0,-1), (3,0,-1), (2,0,-2) with lava + open air at front walkway (1..2,0,1).
        for dx in range(0, 5):
            client.set_rel_block((dx, 0, 0), "lava")
        for dz in (0, 1, 2, 3):
            for dx in (0, 3, 4):
                if not (dx == 0 and dz > 0):
                    client.set_rel_block((dx, 0, dz), "lava")
        for gap in ((2, 0, -1), (3, 0, -1), (2, 0, -2)):
            client.set_rel_block(gap, "lava")
        for air_front in ((1, 0, 1), (2, 0, 1), (0, 0, 1)):
            client.set_rel_block(air_front, "air")
        # Bot starts on the solid west shore bank (0, 1, -2)
        client.pos = list(transform_rel_pos((0.5, 1.0, -1.5), origin, "north", integer=False))

        skill = NetherPortalSpeedrunSkill(client)
        res = skill.build_portal(origin, orientation="north")
        self.assertTrue(res.success, f"narrow pool bridging test failed: {res.message}")
        self.assertTrue(res.data["structure_verified"])
        for gap in ((2, 0, -1), (3, 0, -1), (2, 0, -2)):
            self.assertNotIn(client.get_rel_block(gap), {"air", "lava"})
        for wk in ((1, 0, 1), (2, 0, 1)):
            self.assertNotIn(client.get_rel_block(wk), {"air", "lava"})

    def test_evaluate_site_avoids_blocked_staging_anchor_with_solid_headroom(self):
        from core.structure_spec import NETHER_PORTAL_SPEC, evaluate_structure_site

        origin = [-551, -55, 225]
        client = _SimulatedPortalWorldClient(origin, "south")
        for dx in range(-1, 5):
            for dz in range(-2, 4):
                client.set_rel_block((dx, -1, dz), "deepslate")
                client.set_rel_block((dx, 0, dz), "lava" if 0 <= dz <= 3 else "deepslate")
        # Default (2, 0, -2) anchor is buried under deepslate at (2, 1, -2); (2, 0, -1) is open air.
        client.set_rel_block((2, 1, -2), "deepslate")
        client.set_rel_block((2, 2, -2), "deepslate")

        site = evaluate_structure_site(
            NETHER_PORTAL_SPEC,
            origin,
            "south",
            lambda pos: client.get_block(pos[0], pos[1], pos[2]),
            bot_pos=transform_rel_pos((2.5, 1.0, -0.5), origin, "south", integer=False),
        )
        self.assertIsNotNone(site)
        blocked_staging = list(transform_rel_pos((2.5, 1.0, -1.5), origin, "south", integer=False))
        self.assertNotEqual(site["staging_pos"], blocked_staging)

    def test_stand_at_rejects_vertical_y_offset_on_same_xz(self):
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import WorkflowContext, WorkflowGuard, WorkflowStepError, stand_at

        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        # Bot is floating/stuck 3 blocks above target on the same XZ (e.g. swimming up water curtain).
        target = list(transform_rel_pos((1.5, 1.0, -1.5), origin, "south", integer=False))
        client.pos = [target[0], target[1] + 3.0, target[2]]
        # Keep move_to pinned at +3.0 Y so reposition also fails vertical tolerance.
        client.move_to = lambda x, y, z, range=0.8, **kw: {"ok": True, "pos": client.pos}

        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="south",
            guard=WorkflowGuard(client),
        )
        with self.assertRaises(WorkflowStepError) as cm:
            stand_at(ctx, "shore_approach_1")
        self.assertEqual(cm.exception.code, "unreachable_stance")

    def test_refill_lava_phase2_scoops_deep_lava_from_upper_shore(self):
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import WorkflowContext, WorkflowGuard

        origin = [0, -55, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        for dx in range(-2, 6):
            for dz in range(-4, 5):
                client.set_rel_block((dx, -1, dz), "deepslate")
                client.set_rel_block((dx, 0, dz), "deepslate" if dz >= 0 else "air")
        # Clear headroom above shore and approach cells
        for dz in range(-4, 2):
            for dx in range(-1, 5):
                client.set_rel_block((dx, 1, dz), "air")
                client.set_rel_block((dx, 2, dz), "air")
        # No Phase-1 lava source directly reachable from temp_stand; instead a deep source sits
        # 1 block lower at (4, -1, 1) under flowing lava at (4, 0, 1), adjacent to shore (3, 0, 1).
        client.set_rel_block((4, -1, 1), "lava")
        client.set_rel_block((4, 0, 1), "flowing_lava")
        client.set_rel_block((4, 1, 1), "air")
        client.set_rel_block((3, 0, 1), "deepslate")
        client.items = {"bucket": 2, "cobblestone": 64}
        client.pos = list(transform_rel_pos((1.5, 2.0, 0.5), origin, "south", integer=False))

        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="south",
            guard=WorkflowGuard(client),
        )
        skill = NetherPortalSpeedrunSkill(client)
        ok = skill._refill_lava_from_pool(ctx, return_stance="cast_center", max_refills=1, force_phase2=True)
        self.assertTrue(ok)
        self.assertGreaterEqual(client.items.get("lava_bucket", 0), 1)

    def test_pour_lava_into_base_cell_accepts_mineflayer_stop_radius(self):
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import WorkflowContext, WorkflowGuard

        origin = [0, -55, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        for dx in range(-1, 5):
            for dz in range(-2, 2):
                client.set_rel_block((dx, -2, dz), "deepslate")
                client.set_rel_block((dx, -1, dz), "air" if (dx, dz) == (1, 0) else "deepslate")
                client.set_rel_block((dx, 0, dz), "deepslate" if dz < 0 else "air")
        client.items = {"lava_bucket": 1, "bucket": 1}
        # Simulate Mineflayer stopping ~0.47m away from (1.5, 1.0, -0.78) at z = -0.35 (dist=0.43 <= 0.65)
        stopped_pos = list(transform_rel_pos((1.5, 1.0, -0.35), origin, "south", integer=False))
        client.pos = list(stopped_pos)
        client.move_to = lambda x, y, z, range=0.8, **kw: {"ok": True, "pos": list(stopped_pos)}

        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="south",
            guard=WorkflowGuard(client),
        )
        skill = NetherPortalSpeedrunSkill(client)
        skill._pour_lava_into_base_cell(ctx, (1, -1, 0))
        self.assertIn(client.get_rel_block((1, -1, 0)), {"lava", "obsidian"})

    def test_scoop_top_water_scoops_from_scaffold_when_stand_at_raises_within_reach(self):
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import WorkflowContext, WorkflowGuard

        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        client.set_rel_block((2, 3, -1), "water")
        client.items = {"bucket": 1}
        # Simulate bot stranded atop scaffold (1,1,-1) at y+2.0 within reach of (2,3,-1)
        scaffold_pos = list(transform_rel_pos((1.5, 2.0, -0.3), origin, "south", integer=False))
        client.pos = list(scaffold_pos)
        client.move_to = lambda x, y, z, range=0.8, **kw: {"ok": False, "error_code": "path_not_found", "message": "blocked"}

        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="south",
            guard=WorkflowGuard(client),
        )
        skill = NetherPortalSpeedrunSkill(client)
        res = skill._step_scoop_top_water(ctx)
        self.assertTrue(res.get("scooped"))
        self.assertNotEqual(client.get_rel_block((2, 3, -1)), "water")

    def test_ensure_base_falls_back_when_shore_west_blocked_by_stone(self):
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import WorkflowContext, WorkflowGuard

        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        for dx in range(-1, 5):
            for dz in range(-3, 2):
                client.set_rel_block((dx, -2, dz), "stone")
                client.set_rel_block((dx, -1, dz), "stone")
                client.set_rel_block((dx, 0, dz), "stone" if dz != 0 else "air")
        for dx in (0, 1, 2, 3):
            client.set_rel_block((dx, 0, 0), "lava")
        # Natural stone wall pinches shore_west (0.55, 1.0, -1.5) while (1.4, 1.0, -0.55) stays open.
        client.set_rel_block((1, 1, -2), "stone")
        client.set_rel_block((0, 1, -1), "stone")
        client.items = {"cobblestone": 64, "water_bucket": 1, "lava_bucket": 1, "bucket": 1, "diamond_pickaxe": 1}
        start_pos = list(transform_rel_pos((1.5, 1.0, -1.5), origin, "south", integer=False))
        client.pos = list(start_pos)
        orig_move_to = client.move_to
        west_target = transform_rel_pos((0.55, 1.0, -1.5), origin, "south", integer=False)

        def _pinched_move_to(x, y, z, range=0.8, **kw):
            if abs(float(x) - west_target[0]) < 0.2 and abs(float(z) - west_target[2]) < 0.2:
                if client.get_rel_block((1, 1, -2)) == "stone":
                    return {"ok": False, "error_code": "path_not_found", "message": "pinched"}
            return orig_move_to(x, y, z, range=range, **kw)

        client.move_to = _pinched_move_to
        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="south",
            guard=WorkflowGuard(client),
        )
        skill = NetherPortalSpeedrunSkill(client)
        res = skill._step_ensure_base(ctx)
        self.assertTrue(res.get("base_obsidian"))
        self.assertEqual(client.get_rel_block((1, 1, -2)), "air")
        self.assertEqual(client.get_rel_block((3, 0, 0)), "obsidian")
        self.assertEqual(client.get_rel_block((1, -1, 0)), "obsidian")

    def test_clear_overhead_steps_to_inner_edge_west_for_upper_corner_reach(self):
        import math
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import WorkflowContext, WorkflowGuard, clear_overhead_cells

        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(origin, "west")
        for dx in range(-1, 5):
            for dz in (-2, -1):
                client.set_rel_block((dx, 0, dz), "stone")
        client.set_rel_block((1, 4, 1), "stone")
        client.items = {"diamond_pickaxe": 1}
        client.pos = list(transform_rel_pos((1.5, 1.0, -1.5), origin, "west", integer=False))
        corner = transform_rel_pos((1, 4, 1), origin, "west", integer=True)
        dig_dists = []
        orig_dig = client.dig

        def _checked_dig(x, y, z, **kw):
            bx, by, bz = client.pos
            d = math.sqrt((bx - float(corner[0])) ** 2 + (by - float(corner[1])) ** 2 + (bz - float(corner[2])) ** 2)
            dig_dists.append(d)
            if d > 3.88:
                return {"ok": False, "error_code": "path_not_found", "message": "unreachable corner"}
            return orig_dig(x, y, z, **kw)

        client.dig = _checked_dig
        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="west",
            guard=WorkflowGuard(client),
        )
        clear_overhead_cells(ctx, [(1, 4, 1)])
        self.assertEqual(client.get_rel_block((1, 4, 1)), "air")
        self.assertTrue(dig_dists and all(d <= 3.88 for d in dig_dists), f"Expected feet_to_corner <= 3.88, got {dig_dists}")

    def test_cast_obsidian_recovers_cast_center_by_pausing_curtain_when_stand_at_fails(self):
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import WorkflowContext, WorkflowGuard

        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        for rel in ((1, 1, -2), (0, 1, -1), (1, 1, -1), (3, 1, -1), (3, 2, -1), (3, 3, -1), (3, 3, 0), (2, 3, 0)):
            client.set_rel_block(rel, "cobblestone")
        client.set_rel_block((2, 3, 0), "obsidian")
        client.set_rel_block((1, 1, 1), "cobblestone")
        client.set_rel_block((2, 3, -1), "water")
        client.set_rel_block((1, 2, -1), "flowing_water")
        client.items = {"lava_bucket": 1, "bucket": 1, "cobblestone": 16}
        client.pos = list(transform_rel_pos((2.45, 1.0, -1.5), origin, "south", integer=False))
        cast_center_w = transform_rel_pos((1.55, 2.0, 1.25), origin, "south", integer=False)
        orig_move_to = client.move_to

        def _flooded_move_to(x, y, z, range=0.8, **kw):
            if (
                client.get_rel_block((2, 3, -1)) == "water"
                and abs(float(x) - cast_center_w[0]) < 0.4
                and abs(float(z) - cast_center_w[2]) < 0.4
                and float(y) > origin[1] + 1.5
            ):
                return {"ok": False, "error_code": "path_not_found", "message": "blocked by water curtain"}
            return orig_move_to(x, y, z, range=range, **kw)

        client.move_to = _flooded_move_to
        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="south",
            guard=WorkflowGuard(client),
        )
        ctx.extra["temp_stand_pos"] = (1, 1, 1)
        skill = NetherPortalSpeedrunSkill(client)
        spec = {
            "num": 6,
            "desc": "top_beam_left",
            "pos": (1, 3, 0),
            "ref": (2, 3, 0),
            "face": (-1, 0, 0),
            "stance": "cast_center",
        }
        res = skill._step_cast_obsidian(ctx, spec)
        self.assertEqual(res.get("block"), "obsidian")
        self.assertEqual(client.get_rel_block((1, 3, 0)), "obsidian")
        self.assertEqual(client.get_rel_block((2, 3, -1)), "water")

    def test_evaluate_site_rejects_floor_hazard_magma_block(self):
        from core.structure_spec import NETHER_PORTAL_SPEC, evaluate_structure_site

        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        for dx in range(-1, 5):
            for dz in range(-2, 4):
                client.set_rel_block((dx, -1, dz), "stone")
                client.set_rel_block((dx, 0, dz), "lava" if 0 <= dz <= 3 else "stone")
        client.set_rel_block((3, 0, -1), "magma_block")
        site = evaluate_structure_site(
            NETHER_PORTAL_SPEC,
            origin,
            "south",
            lambda pos: client.get_block(pos[0], pos[1], pos[2]),
            bot_pos=transform_rel_pos((2.5, 1.0, -1.5), origin, "south", integer=False),
        )
        self.assertIsNone(site)

    def test_approach_site_branch_digs_blocking_stone_wall(self):
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import ActionStep, ActionWorkflowRunner, WorkflowContext, WorkflowGuard

        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        client.set_rel_block((2, 1, -2), "stone")
        client.set_rel_block((2, 2, -2), "stone")
        client.items = {"diamond_pickaxe": 1}
        client.pos = [2.5, 65.0, -3.5]
        target = [2.5, 65.0, -1.5]
        orig_move_to = client.move_to

        def _walled_move_to(x, y, z, range=0.8, **kw):
            if client.get_rel_block((2, 2, -2)) == "stone":
                raise APIError("stalled", "wall blocking path")
            return orig_move_to(x, y, z, range=range, **kw)

        client.move_to = _walled_move_to
        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="south",
            guard=WorkflowGuard(client),
            extra={"approach_target": target, "start_tolerance": 1.2},
        )
        skill = NetherPortalSpeedrunSkill(client)
        step = ActionStep(
            step_id="move_to_start",
            description="approach",
            stage="base",
            action=skill._step_approach_site,
            branches={"stalled": skill._branch_approach_site, "unreachable_stance": skill._branch_approach_site},
            max_branch_attempts=2,
        )
        res = ActionWorkflowRunner().run(
            ctx,
            [step],
            verify_structure=lambda _c: {"complete": True, "present_count": 1, "total_count": 1},
        )
        self.assertTrue(res.success, res.message)
        self.assertEqual(client.get_rel_block((2, 2, -2)), "air")
        self.assertIn("move_to_start", res.data["completed_steps"])

    def test_refill_lava_phase3_finds_distant_lava_via_find_blocks(self):
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import WorkflowContext, WorkflowGuard

        origin = [0, -55, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        for dx in range(-2, 11):
            for dz in range(-3, 9):
                client.set_rel_block((dx, -1, dz), "deepslate")
                client.set_rel_block((dx, 0, dz), "deepslate" if (dx, dz) != (8, 5) else "lava")
        client.set_rel_block((8, 1, 5), "air")
        client.items = {"bucket": 2, "cobblestone": 16}
        client.pos = list(transform_rel_pos((2.5, 1.0, -1.5), origin, "south", integer=False))
        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="south",
            guard=WorkflowGuard(client),
        )
        skill = NetherPortalSpeedrunSkill(client)
        ok = skill._refill_lava_from_pool(ctx, return_stance="player_start", max_refills=1)
        self.assertTrue(ok)
        self.assertEqual(client.items.get("lava_bucket", 0), 1)
        self.assertEqual(client.get_rel_block((8, 0, 5)), "air")

    def test_bridge_cells_skips_when_falling_sand_already_filled_cell(self):
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import WorkflowContext, WorkflowGuard, bridge_cells

        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        for dx in range(0, 4):
            client.set_rel_block((dx, 0, -2), "sandstone")
            client.set_rel_block((dx, 0, -1), "air")
        client.items = {"cobblestone": 16}
        client.pos = list(transform_rel_pos((0.5, 1.0, -1.5), origin, "south", integer=False))
        orig_move_to = client.move_to

        def _drop_sand_on_move(x, y, z, range=0.8, **kw):
            # Simulate sand falling into (2, 0, -1) while walking to bridge it
            client.set_rel_block((2, 0, -1), "sand")
            return orig_move_to(x, y, z, range=range, **kw)

        client.move_to = _drop_sand_on_move
        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="south",
            guard=WorkflowGuard(client),
        )
        res = bridge_cells(ctx, [(2, 0, -1)])
        self.assertEqual(client.get_rel_block((2, 0, -1)), "sand")
        self.assertTrue(any(row.get("already_solid") for row in res))

    def test_refill_lava_skips_cannot_interact_when_cell_froze_to_obsidian(self):
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import WorkflowContext, WorkflowGuard

        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        for rel in ((1, 0, 2), (2, 0, 2)):
            client.set_rel_block(rel, "lava")
        client.items = {"bucket": 1}
        client.pos = list(transform_rel_pos((1.5, 2.0, 1.25), origin, "south", integer=False))
        first_cell = client.wpos((1, 0, 2))
        orig_interact = client.interact

        def _freeze_first_interact(pos=None, item=None, **kw):
            if item == "bucket" and tuple(pos) == first_cell:
                client.blocks[first_cell] = {"name": "obsidian"}
                raise APIError("cannot_interact", "an empty bucket needs pos to be the liquid block itself (got obsidian)")
            return orig_interact(pos=pos, item=item, **kw)

        client.interact = _freeze_first_interact
        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="south",
            guard=WorkflowGuard(client),
        )
        skill = NetherPortalSpeedrunSkill(client)
        ok = skill._refill_lava_from_pool(ctx, return_stance="player_start", max_refills=1)
        self.assertTrue(ok)
        self.assertEqual(client.items.get("lava_bucket", 0), 1)
        self.assertEqual(client.get_rel_block((2, 0, 2)), "air")

    def test_stand_pour_stance_recovers_after_spilled_water_raises_unreachable_stance(self):
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import WorkflowContext, WorkflowGuard

        origin = [0, 65, 0]
        client = _SimulatedPortalWorldClient(origin, "east")
        client.set_rel_block((0, 1, -1), "water")
        client.items = {"bucket": 1}
        client.pos = list(transform_rel_pos((0.5, 1.9, -0.78), origin, "east", integer=False))
        orig_move_to = client.move_to

        def _bob_until_scooped(x, y, z, range=0.8, **kw):
            if client.get_rel_block((0, 1, -1)) == "water":
                return {"ok": True, "pos": list(client.pos)}
            return orig_move_to(x, y, z, range=range, **kw)

        client.move_to = _bob_until_scooped
        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="east",
            guard=WorkflowGuard(client),
        )
        skill = NetherPortalSpeedrunSkill(client)
        skill._stand_pour_stance(ctx, (0, 0, 0), (0.5, 1.0, -0.78))
        self.assertEqual(client.get_rel_block((0, 1, -1)), "air")

    def test_ensure_base_avoids_solid_dy1_pour_and_recovers_dy2_water(self):
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import WorkflowContext, WorkflowGuard

        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        for dx in range(-1, 5):
            client.set_rel_block((dx, 0, -1), "stone")
            client.set_rel_block((dx, 0, -2), "stone")
        # Natural stone wall at (0..3, 1, -1) prevents low water-pour landing at dy=1
        for cx in (0, 1, 2, 3):
            client.set_rel_block((cx, 1, -1), "stone")
        client.set_rel_block((0, 0, 0), "lava")
        client.set_rel_block((1, -1, 0), "obsidian")
        client.set_rel_block((2, -1, 0), "obsidian")
        client.set_rel_block((3, 0, 0), "obsidian")
        client.set_rel_block((1, 0, 1), "cobblestone")
        client.set_rel_block((2, 0, 1), "cobblestone")
        client.items = {"water_bucket": 1, "diamond_pickaxe": 1, "cobblestone": 16}
        client.pos = list(transform_rel_pos((0.5, 1.0, -1.5), origin, "south", integer=False))
        orig_interact = client.interact

        def _pour_dy2_freeze(pos=None, item=None, face=None, **kw):
            res = orig_interact(pos=pos, item=item, face=face, **kw)
            if item == "water_bucket" and tuple(pos) == client.wpos((0, 1, -1)):
                # Landing spilled water at (0, 2, -1) freezes (0, 0, 0) lava into obsidian
                client.set_rel_block((0, 0, 0), "obsidian")
            return res

        client.interact = _pour_dy2_freeze
        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="south",
            guard=WorkflowGuard(client),
        )
        skill = NetherPortalSpeedrunSkill(client)
        res = skill._step_ensure_base(ctx)
        self.assertTrue(res.get("base_obsidian"))
        self.assertEqual(client.get_rel_block((0, 0, 0)), "obsidian")
        self.assertEqual(client.get_rel_block((0, 2, -1)), "air")
        self.assertEqual(client.items.get("water_bucket", 0), 1)

    def test_clear_overhead_steps_off_feet_floor_before_digging(self):
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import WorkflowContext, WorkflowGuard, clear_overhead_cells

        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        for dx in range(0, 4):
            client.set_rel_block((dx, 0, -2), "stone")
        # Bot is standing on (2, 1, -2), which is also in the overhead clear list
        client.set_rel_block((2, 1, -2), "stone")
        client.items = {"diamond_pickaxe": 1}
        client.pos = list(transform_rel_pos((2.5, 2.0, -1.5), origin, "south", integer=False))
        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="south",
            guard=WorkflowGuard(client),
        )
        res = clear_overhead_cells(ctx, [(2, 1, -2)])
        self.assertTrue(len(res) >= 1)
        self.assertEqual(client.get_rel_block((2, 1, -2)), "air")

    def test_bridge_cells_tolerates_falling_gravel_fill(self):
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import WorkflowContext, WorkflowGuard, bridge_cells

        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        client.set_rel_block((2, 0, -1), "stone")
        client.set_rel_block((2, 0, 0), "air")
        client.items = {"cobblestone": 8}
        client.pos = list(transform_rel_pos((2.5, 1.0, -1.5), origin, "south", integer=False))
        orig_place = client.place_block

        def _gravel_collapses_into_target(item=None, x=0, y=0, z=0, **kw):
            if (int(x), int(y), int(z)) == client.wpos((2, 0, 0)):
                client.set_rel_block((2, 0, 0), "gravel")
                return {"ok": True, "result": {"placed": False}}
            return orig_place(item=item, x=x, y=y, z=z, **kw)

        client.place_block = _gravel_collapses_into_target
        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="south",
            guard=WorkflowGuard(client),
        )
        placed = bridge_cells(ctx, [(2, 0, 0)], solid_blocks=["cobblestone"])
        self.assertEqual(len(placed), 1)
        self.assertTrue(placed[0].get("already_solid"))
        self.assertEqual(placed[0].get("block"), "gravel")

    def test_branch_approach_site_tunnels_forward_and_digs_step_down(self):
        import math
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import WorkflowContext, WorkflowGuard, WorkflowStepError

        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        # Thick wall along +Z between z=-5 (start_world) and z=-2 (shore)
        target_w = list(transform_rel_pos((2.5, 1.0, -1.5), origin, "south", integer=False))
        for dz in range(-6, 0):
            client.set_rel_block((2, 0, dz), "stone")
        for dz in (-4, -3):
            client.set_rel_block((2, 1, dz), "deepslate")
            client.set_rel_block((2, 2, dz), "deepslate")
        client.items = {"diamond_pickaxe": 1}
        client.pos = list(transform_rel_pos((2.5, 1.0, -4.5), origin, "south", integer=False))
        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="south",
            extra={"approach_target": target_w, "start_tolerance": 2.4},
            guard=WorkflowGuard(client),
        )
        skill = NetherPortalSpeedrunSkill(client)
        err = WorkflowStepError("unreachable_stance", "blocked by cave wall")
        ok = skill._branch_approach_site(ctx, err)
        self.assertTrue(ok)
        # Should both clear ahead blocks AND step forward into the gap
        self.assertGreater(client.pos[2], origin[2] - 4.0)

        # Now test perched directly above target: should dig adjacent step-down dirt mound
        client.pos = [target_w[0], target_w[1] + 2.0, target_w[2]]
        bx = math.floor(client.pos[0])
        by = math.floor(client.pos[1])
        bz = math.floor(client.pos[2])
        client.blocks[(bx, by - 1, bz)] = {"name": "dirt"}
        client.blocks[(bx + 1, by - 1, bz)] = {"name": "dirt"}
        client.blocks[(bx + 1, by, bz)] = {"name": "dirt"}
        ok2 = skill._branch_approach_site(ctx, WorkflowStepError("unreachable_stance", "perched above"))
        self.assertTrue(ok2)
        self.assertEqual(client.blocks.get((bx, by - 1, bz), {}).get("name"), "dirt")
        self.assertEqual(client.blocks.get((bx + 1, by, bz), {}).get("name"), "air")

    def test_branch_recover_movement_handles_stalled_and_adjacent_blocker(self):
        import math
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import WorkflowContext, WorkflowGuard, WorkflowStepError

        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        client.items = {"diamond_pickaxe": 1}
        client.pos = list(transform_rel_pos((1.5, 1.0, -1.5), origin, "south", integer=False))
        bx, by, bz = math.floor(client.pos[0]), math.floor(client.pos[1]), math.floor(client.pos[2])
        client.blocks[(bx + 1, by + 1, bz)] = {"name": "stone"}
        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="south",
            guard=WorkflowGuard(client),
        )
        skill = NetherPortalSpeedrunSkill(client)
        self.assertTrue(skill._branch_recover_movement(ctx, WorkflowStepError("path_not_found", "blocked")))
        self.assertEqual(client.blocks.get((bx + 1, by + 1, bz), {}).get("name"), "air")
        # Even with no diggable neighbors, transient stalled returns True once
        self.assertTrue(skill._branch_recover_movement(ctx, WorkflowStepError("stalled", "stalled")))

    def test_refill_lava_phase3_sorts_closest_first_and_stops_early(self):
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import WorkflowContext, WorkflowGuard

        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        client.items = {"bucket": 1}
        client.pos = [0.5, 65.0, -1.5]
        # Far unreachable lava blocks (no solid neighbor floor) listed first, plus 1 nearby reachable lava block last
        far_hits = [{"pos": [15, 78, i]} for i in range(12)]
        near_pos = [1, 64, 2]
        for row in far_hits:
            client.blocks[tuple(row["pos"])] = {"name": "lava", "properties": {"level": 0}}
        client.blocks[tuple(near_pos)] = {"name": "lava", "properties": {"level": 0}}
        client.blocks[(1, 64, 1)] = {"name": "stone"}
        client.find_blocks = lambda *a, **kw: list(far_hits) + [{"pos": list(near_pos)}]
        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="south",
            guard=WorkflowGuard(client, max_reads=120),
        )
        skill = NetherPortalSpeedrunSkill(client)
        filled = skill._refill_lava_phase3(ctx, max_fills=1)
        self.assertEqual(filled, 1)
        self.assertEqual(client.items.get("lava_bucket", 0), 1)
        self.assertLess(ctx.guard.read_calls, 120)

    def test_find_structure_sites_prefers_same_side_shore_over_cross_lava_pool(self):
        from core.structure_spec import NETHER_PORTAL_SPEC, find_structure_sites

        blocks = {}
        # Build 6-wide lava lake at x=0..3, z=0..4 (south pool edge is z=5..6, north pool edge is z=-2..-1)
        for x in range(-1, 5):
            for z in range(-2, 7):
                blocks[(x, 62, z)] = {"name": "stone"}
                blocks[(x, 63, z)] = {"name": "stone"}
                if 0 <= z <= 4:
                    blocks[(x, 64, z)] = {"name": "lava", "properties": {"level": 0}}
                else:
                    blocks[(x, 64, z)] = {"name": "stone"}
        bot_pos = [1.5, 65.0, 5.5]
        seed_positions = [[x, 64, z] for x in range(0, 4) for z in range(0, 5)]
        sites = find_structure_sites(
            NETHER_PORTAL_SPEC,
            bot_pos,
            lambda pos: blocks.get((int(pos[0]), int(pos[1]), int(pos[2])), {"name": "air"}),
            seed_positions=seed_positions,
            inventory_counts={"cobblestone": 64, "bucket": 2, "lava_bucket": 1, "water_bucket": 1},
            max_results=5,
        )
        self.assertTrue(sites)
        # Top ranked site must be on south shore (orientation north, staging z >= 5) rather than across the lava lake
        self.assertEqual(sites[0]["orientation"], "north")
        self.assertGreaterEqual(sites[0]["staging_pos"][2], 4.0)

    def test_pour_lava_into_base_cell_uses_corner_or_inner_edge_with_solid_wall(self):
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import WorkflowContext, WorkflowGuard

        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        for dx in range(-1, 5):
            for dz in range(-2, 2):
                client.set_rel_block((dx, -2, dz), "stone")
                client.set_rel_block((dx, -1, dz), "air" if (dx, dz) == (1, 0) else "stone")
                client.set_rel_block((dx, 0, dz), "stone" if dz < 0 else "air")
        client.set_rel_block((2, 0, 0), "cobblestone")
        client.items = {"lava_bucket": 1, "bucket": 1}
        client.pos = list(transform_rel_pos((1.5, 1.0, -1.5), origin, "south", integer=False))
        stand_history = []
        orig_move_to = client.move_to

        def _rec_move(x, y, z, **kw):
            stand_history.append((x, y, z))
            return orig_move_to(x, y, z, **kw)

        client.move_to = _rec_move
        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="south",
            guard=WorkflowGuard(client),
        )
        skill = NetherPortalSpeedrunSkill(client)
        skill._pour_lava_into_base_cell(ctx, (1, -1, 0))
        self.assertEqual(client.get_rel_block((1, -1, 0)), "lava")
        # Should step onto solid corner (2.5, 65.0, 0.5) or edge z=-0.32, never pour from z=-0.78
        self.assertTrue(any(abs(pos[2] - (origin[2] + 0.5)) < 0.25 or pos[2] > origin[2] - 0.45 for pos in stand_history))

    def test_bridge_cells_falls_back_to_within_reach_stance_when_lava_peninsula_unpathable(self):
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import WorkflowContext, WorkflowGuard, bridge_cells

        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        client.set_rel_block((3, 0, -2), "stone")
        client.set_rel_block((3, 0, -1), "stone")
        client.set_rel_block((2, 0, -1), "lava")
        client.items = {"cobblestone": 8}
        # Bot is 2.02m away from (3.5, 65, -0.5) on (3.2, 66, -2.5), well within 3.85m placement reach of (2, 0, -1)
        fixed_pos = list(transform_rel_pos((3.2, 2.0, -2.5), origin, "south", integer=False))
        client.pos = list(fixed_pos)
        client.move_to = lambda x, y, z, **kw: {"ok": False, "error": {"code": "path_not_found", "message": "peninsula blocked"}}
        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="south",
            guard=WorkflowGuard(client),
        )
        placed = bridge_cells(ctx, [(2, 0, -1)], solid_blocks=["cobblestone"])
        self.assertEqual(len(placed), 1)
        self.assertEqual(client.get_rel_block((2, 0, -1)), "cobblestone")

    def test_setup_temp_stand_fills_pit_and_cast_obsidian_recovers_via_staging(self):
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import WorkflowContext, WorkflowGuard

        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        client.set_rel_block((0, -1, 1), "deepslate")
        client.set_rel_block((0, 0, 1), "air")
        client.set_rel_block((1, 0, 1), "cobblestone")
        client.set_rel_block((2, 0, 1), "cobblestone")
        client.set_rel_block((2, 3, -1), "water")
        client.set_rel_block((2, 3, 0), "obsidian")
        client.items = {"cobblestone": 16, "lava_bucket": 1, "bucket": 1}
        client.pos = list(transform_rel_pos((2.5, 1.0, -0.5), origin, "south", integer=False))
        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="south",
            guard=WorkflowGuard(client),
        )
        skill = NetherPortalSpeedrunSkill(client)
        stand_w = list(transform_rel_pos((1, 1, 1), origin, "south", integer=True))
        ctx.extra["temp_stand_pos"] = stand_w
        skill._step_setup_temp_stand(ctx, stand_w)
        self.assertEqual(client.get_rel_block((0, 0, 1)), "cobblestone")
        self.assertEqual(client.get_rel_block((1, 1, 1)), "cobblestone")

        # Now place bot trapped/perched off stand and fail first direct move to cast_center when curtain is air
        client.set_rel_block((2, 3, -1), "water")
        client.pos = list(transform_rel_pos((0.4, 0.0, 1.5), origin, "south", integer=False))
        client.set_rel_block((1, 1, -1), "cobblestone")
        client.set_rel_block((3, 3, -1), "cobblestone")
        orig_move_to = client.move_to
        cast_center_w = transform_rel_pos((1.55, 2.0, 1.25), origin, "south", integer=False)

        def _fail_until_staged(x, y, z, range=0.8, **kw):
            if (
                abs(float(x) - cast_center_w[0]) < 0.3
                and abs(float(z) - cast_center_w[2]) < 0.3
                and client.pos[1] < origin[1] + 0.5
            ):
                return {"ok": False, "error_code": "path_not_found", "message": "trapped below"}
            return orig_move_to(x, y, z, range=range, **kw)

        client.move_to = _fail_until_staged
        spec = {
            "num": 6,
            "desc": "top_beam_left",
            "pos": (1, 3, 0),
            "ref": (2, 3, 0),
            "face": (-1, 0, 0),
            "stance": "cast_center",
        }
        res = skill._step_cast_obsidian(ctx, spec)
        self.assertEqual(res.get("block"), "obsidian")

    def test_pour_lava_into_base_cell_never_uses_occluded_corner_ref_or_corner_stance(self):
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import WorkflowContext, WorkflowGuard

        origin = [0, 64, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        for dx in range(-1, 5):
            for dz in range(-2, 2):
                client.set_rel_block((dx, -2, dz), "stone")
                client.set_rel_block((dx, -1, dz), "air" if (dx, dz) == (1, 0) else "stone")
                client.set_rel_block((dx, 0, dz), "stone" if dz < 0 else "air")
        # Solid blocks on corner (2, 0, 0) and back wall (1, -1, 1) is air (forces pit floor or other ref)
        client.set_rel_block((2, 0, 0), "cobblestone")
        client.set_rel_block((1, -1, 1), "air")
        client.items = {"lava_bucket": 1, "bucket": 1}
        client.pos = list(transform_rel_pos((1.5, 1.0, -1.5), origin, "south", integer=False))
        interact_calls = []
        orig_interact = client.interact

        def _rec_interact(**kw):
            interact_calls.append(dict(kw))
            return orig_interact(**kw)

        client.interact = _rec_interact
        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="south",
            guard=WorkflowGuard(client),
        )
        skill = NetherPortalSpeedrunSkill(client)
        skill._pour_lava_into_base_cell(ctx, (1, -1, 0))
        self.assertEqual(client.get_rel_block((1, -1, 0)), "lava")
        # Bot should stand on dry front inner edge z ~ -0.22 (in [-0.39, 0.0)), never atop corner (2, 0, 0) at z >= 0
        self.assertLess(client.pos[2], origin[2])
        self.assertGreaterEqual(client.pos[2], origin[2] - 0.39)
        self.assertTrue(len(interact_calls) == 1 and interact_calls[0].get("pos") == [1, 63, 0])

    def test_refill_lava_skips_flowing_and_water_above_and_recovers_crust_lava(self):
        from core.structure_spec import NETHER_PORTAL_SPEC
        from skills.action_workflow import WorkflowContext, WorkflowGuard

        origin = [0, -55, 0]
        client = _SimulatedPortalWorldClient(origin, "south")
        for dx in range(-2, 6):
            for dz in range(-2, 5):
                client.set_rel_block((dx, -2, dz), "deepslate")
                client.set_rel_block((dx, -1, dz), "deepslate")
                client.set_rel_block((dx, 0, dz), "deepslate")
                client.set_rel_block((dx, 1, dz), "air")
                client.set_rel_block((dx, 2, dz), "air")
        # Flowing lava (level=4) and water-covered lava should NOT be scooped; crusted source under cobbles CAN be dug.
        client.blocks[client.wpos((4, -1, 1))] = {"name": "lava", "properties": {"level": 4}}
        client.set_rel_block((4, 0, 1), "water")
        client.set_rel_block((3, -1, 2), "lava")
        client.set_rel_block((3, 0, 2), "cobblestone")
        client.items = {"bucket": 2, "iron_pickaxe": 1, "cobblestone": 32}
        client.pos = list(transform_rel_pos((1.5, 1.0, -0.5), origin, "south", integer=False))
        crust_w = list(transform_rel_pos((3, -1, 2), origin, "south", integer=True))
        flowing_w = list(transform_rel_pos((4, -1, 1), origin, "south", integer=True))
        client.find_blocks = lambda types=None, radius=18, limit=48: [
            {"pos": flowing_w, "name": "lava"},  # omitted properties like mc-agent-api
            {"pos": crust_w, "name": "lava"},
        ]

        ctx = WorkflowContext(
            spec=NETHER_PORTAL_SPEC,
            origin=origin,
            orientation="south",
            guard=WorkflowGuard(client),
        )
        skill = NetherPortalSpeedrunSkill(client)
        ok = skill._refill_lava_from_pool(ctx, return_stance="shore_approach_2", max_refills=1, force_phase2=True)
        self.assertTrue(ok)
        self.assertGreaterEqual(client.items.get("lava_bucket", 0), 1)
        self.assertEqual(client.get_rel_block((3, 0, 2)), "air")
        self.assertEqual(client.get_rel_block((4, 0, 1)), "water")

    def test_evaluate_structure_site_rejects_shore_waterfall(self):
        from core.structure_spec import NETHER_PORTAL_SPEC, evaluate_structure_site

        origin = [0, 96, 0]
        client = _SimulatedPortalWorldClient(origin, "north")
        for dx in range(4):
            for dy in (-1, 0):
                for dz in range(0, 3):
                    if dy == -1 or dz >= 1 or dx in (1, 2):
                        client.set_rel_block((dx, dy, dz), "lava")
            for dz in (-2, -1):
                client.set_rel_block((dx, -1, dz), "stone")
                client.set_rel_block((dx, 0, dz), "stone")
        # Shore waterfall flooding z=-2..-1 at dy=1..2
        client.set_rel_block((0, 1, -2), "water")
        client.set_rel_block((0, 2, -2), "water")
        client.set_rel_block((0, 2, -1), "water")

        site = evaluate_structure_site(
            NETHER_PORTAL_SPEC,
            origin,
            "north",
            lambda pos: client.get_block(pos[0], pos[1], pos[2]),
            allow_overhead_dig=True,
        )
        self.assertIsNone(site)

    def test_evaluate_structure_site_rejects_overhead_aquifer_behind_stone(self):
        from core.structure_spec import NETHER_PORTAL_SPEC, evaluate_structure_site

        origin = [0, 54, 0]
        client = _SimulatedPortalWorldClient(origin, "west")
        for dx in range(4):
            for dy in (-1, 0):
                for dz in range(0, 3):
                    if dy == -1 or dz >= 1 or dx in (1, 2):
                        client.set_rel_block((dx, dy, dz), "lava")
            for dz in (-2, -1):
                client.set_rel_block((dx, -1, dz), "stone")
                client.set_rel_block((dx, 0, dz), "stone")
        # Stone wall at (0, 2, -2) dams water right behind it at (-1, 2, -2)
        client.set_rel_block((0, 2, -2), "stone")
        client.set_rel_block((-1, 2, -2), "water")

        site = evaluate_structure_site(
            NETHER_PORTAL_SPEC,
            origin,
            "west",
            lambda pos: client.get_block(pos[0], pos[1], pos[2]),
            allow_overhead_dig=True,
        )
        self.assertIsNone(site)

    def test_pour_lava_into_base_cell_uses_goal_near_xz_when_behind_max_back(self):
        from skills.action_workflow import WorkflowContext, WorkflowGuard

        class _GoNearClampClient(_SimulatedPortalWorldClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.move_calls = []

            def move_to(self, x, y=None, z=None, timeout_ms=15000, range=1):
                self.move_calls.append((x, y, z, range))
                if y is not None:
                    dx = float(x) - float(self.pos[0])
                    dy = float(y) - float(self.pos[1])
                    dz = float(z) - float(self.pos[2])
                    if (dx * dx + dy * dy + dz * dz) ** 0.5 <= float(range) + 0.5:
                        return {"pos": list(self.pos)}
                self.pos = [float(x), self.pos[1] if y is None else float(y), float(z)]
                return {"pos": list(self.pos)}

        origin = [24, 64, 24]
        client = _GoNearClampClient(origin, "south")
        client.set_rel_block((1, -1, 0), "air")
        client.set_rel_block((0, -1, 0), "air")
        client.set_rel_block((2, -1, 0), "air")
        client.set_rel_block((1, -1, 1), "air")
        client.set_rel_block((1, -2, 0), "stone")
        client.pos = list(transform_rel_pos((1.5, 1.0, -0.58), origin, "south", integer=False))

        ctx = WorkflowContext(guard=WorkflowGuard(client), spec=NETHER_PORTAL_SPEC, origin=origin, orientation="south")
        NetherPortalSpeedrunSkill(client)._pour_lava_into_base_cell(ctx, (1, -1, 0))

        self.assertTrue(any(call[1] is None and float(call[3]) <= 0.1 for call in client.move_calls))
        self.assertEqual(client.blocks[client.wpos((1, -1, 0))]["name"], "lava")

    def test_pour_lava_into_base_cell_refuses_to_clip_shore_when_stuck_behind_max_back(self):
        from skills.action_workflow import WorkflowContext, WorkflowGuard, WorkflowStepError

        class _FrozenClampClient(_SimulatedPortalWorldClient):
            def move_to(self, x, y=None, z=None, timeout_ms=15000, range=1):
                return {"pos": list(self.pos)}

        origin = [24, 64, 24]
        client = _FrozenClampClient(origin, "south")
        client.set_rel_block((1, -1, 0), "air")
        client.set_rel_block((0, -1, 0), "stone")
        client.set_rel_block((2, -1, 0), "air")
        client.set_rel_block((1, -1, 1), "air")
        client.set_rel_block((1, -2, 0), "stone")
        client.pos = list(transform_rel_pos((1.5, 1.0, -0.58), origin, "south", integer=False))

        ctx = WorkflowContext(guard=WorkflowGuard(client), spec=NETHER_PORTAL_SPEC, origin=origin, orientation="south")
        with self.assertRaises(WorkflowStepError) as cm:
            NetherPortalSpeedrunSkill(client)._pour_lava_into_base_cell(ctx, (1, -1, 0))

        self.assertEqual(cm.exception.code, "unreachable_stance")
        self.assertEqual(client._interact_count, 0)
        self.assertEqual(client.blocks[client.wpos((1, -1, 0))]["name"], "air")

    def test_prepare_site_clears_natural_shore_blocks_and_safe_shoulders_without_breaching_water(self):
        from core.structure_spec import find_structure_sites

        origin = [32, 64, 32]
        client = _SimulatedPortalWorldClient(origin, "south")
        # Natural rock on (0,1,-1)/(1,1,-1) and dry side shoulders (-1,1,-1)/(4,1,-1),
        # plus (-1,1,-2) retaining contained water at (-2,1,-2).
        client.set_rel_block((0, 0, 0), "lava")
        client.set_rel_block((0, 1, -1), "andesite")
        client.set_rel_block((1, 1, -1), "stone")
        client.set_rel_block((-1, 1, -1), "stone")
        client.set_rel_block((4, 1, -1), "stone")
        client.set_rel_block((-2, 1, -1), "stone")
        client.set_rel_block((-1, 1, -2), "stone")
        client.set_rel_block((-1, 2, -2), "stone")
        client.set_rel_block((-2, 2, -2), "stone")
        client.set_rel_block((-3, 1, -2), "stone")
        client.set_rel_block((-2, 1, -3), "stone")
        client.set_rel_block((-2, 0, -2), "stone")
        client.set_rel_block((-2, 1, -2), "water")

        skill = NetherPortalSpeedrunSkill(client)
        result = skill.build_portal(origin=origin, orientation="south")
        self.assertTrue(result.success, result.message)
        self.assertEqual(client.blocks[client.wpos((-1, 1, -1))]["name"], "air")
        self.assertEqual(client.blocks[client.wpos((4, 1, -1))]["name"], "air")
        # Retaining block next to water must NOT be dug.
        self.assertEqual(client.blocks[client.wpos((-1, 1, -2))]["name"], "stone")

        # Also verify find_structure_sites discovers shore origin when seeds are 2 rows out in pool.
        client2 = _SimulatedPortalWorldClient(origin, "south")
        for dx in range(4):
            for dz in (0, 1, 2):
                client2.set_rel_block((dx, 0, dz), "lava")
        deep_seeds = [list(client2.wpos((1, 0, 2))), list(client2.wpos((2, 0, 2)))]
        sites = find_structure_sites(
            NETHER_PORTAL_SPEC,
            list(client2.pos),
            lambda pos: client2.get_block(pos[0], pos[1], pos[2]),
            seed_positions=deep_seeds,
            orientations=("south",),
        )
        self.assertTrue(any(s["origin"] == origin and s["orientation"] == "south" for s in sites))


if __name__ == "__main__":
    unittest.main()
