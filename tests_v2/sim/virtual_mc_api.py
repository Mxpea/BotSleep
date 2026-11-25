"""Pure-memory 3D simulator matching upstream mc-agent-api semantics and quirks.

Key upstream behaviors reproduced faithfully:
- GET /status uses `pos: [x, y, z]` (1-decimal floats), never `position` dict.
- POST /find_blocks skips air/cave_air/void_air and non-exposed blocks;
  returns summary dict when `types` is omitted and block list when `types` is given.
- POST /interact with empty `bucket` raycasts to `(tx+0.5, ty+0.85, tz+0.5)` and fails
  with `target_occluded` if any intermediate cell (including grass/dirt lip) blocks it.
- POST /interact with full bucket aims at `(tx+0.5, ty-0.01, tz+0.5)` and casts obsidian
  when water meets stationary lava.
- POST /move_to has NO 32-block artificial error and ignores `allow_partial`; it pathfinds
  with `canDig=False` and `scafoldingBlocks=[]`, failing with `path_not_found` across walls/chasms.
- Supports both direct client-compatible method calls and a real loopback HTTP server
  (`VirtualMcHttpServer`) for testing `requests.Session` + `AsyncReadPool` over HTTP.
"""

from __future__ import annotations

from collections import deque
import http.server
import itertools
import json
import math
import socketserver
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qs, urlsplit


AIR_BLOCKS = frozenset({"air", "cave_air", "void_air"})
LIQUID_BLOCKS = frozenset({"water", "lava"})
NON_SOLID_BLOCKS = AIR_BLOCKS | LIQUID_BLOCKS | frozenset({
    "short_grass", "tall_grass", "fern", "dandelion", "poppy", "nether_portal",
    "fire", "torch", "wall_torch", "snow", "vine",
})
TRANSPARENT_FOR_EXPOSURE = NON_SOLID_BLOCKS | frozenset({
    "oak_leaves", "birch_leaves", "spruce_leaves", "glass", "oak_door",
})
GRAVITY_BLOCKS = frozenset({"sand", "red_sand", "gravel"})

DROP_MAP: Dict[str, str] = {
    "stone": "cobblestone",
    "deepslate": "cobbled_deepslate",
    "iron_ore": "raw_iron",
    "deepslate_iron_ore": "raw_iron",
    "coal_ore": "coal",
    "deepslate_coal_ore": "coal",
    "diamond_ore": "diamond",
    "deepslate_diamond_ore": "diamond",
    "gold_ore": "raw_gold",
    "copper_ore": "raw_copper",
    "grass_block": "dirt",
}

FOOD_RESTORE: Dict[str, Tuple[int, float]] = {
    "bread": (5, 6.0),
    "cooked_beef": (8, 12.8),
    "cooked_porkchop": (8, 12.8),
    "cooked_mutton": (6, 9.6),
    "cooked_chicken": (6, 7.2),
    "apple": (4, 2.4),
    "baked_potato": (5, 6.0),
    "sweet_berries": (2, 0.4),
    "beef": (3, 1.8),
    "porkchop": (3, 1.8),
}

# Minimal built-in recipes for simulator crafting (also augmented dynamically when needed).
SIM_RECIPES: Dict[str, Tuple[bool, int, Dict[str, int]]] = {
    # item: (needs_table, output_count, {ingredient: count})
    "oak_planks": (False, 4, {"oak_log": 1}),
    "birch_planks": (False, 4, {"birch_log": 1}),
    "spruce_planks": (False, 4, {"spruce_log": 1}),
    "stick": (False, 4, {"oak_planks": 2}),
    "crafting_table": (False, 1, {"oak_planks": 4}),
    "torch": (False, 4, {"coal": 1, "stick": 1}),
    "wooden_pickaxe": (True, 1, {"oak_planks": 3, "stick": 2}),
    "wooden_axe": (True, 1, {"oak_planks": 3, "stick": 2}),
    "wooden_sword": (True, 1, {"oak_planks": 2, "stick": 1}),
    "stone_pickaxe": (True, 1, {"cobblestone": 3, "stick": 2}),
    "stone_axe": (True, 1, {"cobblestone": 3, "stick": 2}),
    "stone_sword": (True, 1, {"cobblestone": 2, "stick": 1}),
    "stone_shovel": (True, 1, {"cobblestone": 1, "stick": 2}),
    "furnace": (True, 1, {"cobblestone": 8}),
    "chest": (True, 1, {"oak_planks": 8}),
    "iron_pickaxe": (True, 1, {"iron_ingot": 3, "stick": 2}),
    "iron_axe": (True, 1, {"iron_ingot": 3, "stick": 2}),
    "iron_sword": (True, 1, {"iron_ingot": 2, "stick": 1}),
    "bucket": (True, 1, {"iron_ingot": 3}),
    "shield": (True, 1, {"oak_planks": 6, "iron_ingot": 1}),
    "flint_and_steel": (False, 1, {"iron_ingot": 1, "flint": 1}),
    "oak_door": (True, 3, {"oak_planks": 6}),
    "white_bed": (True, 1, {"white_wool": 3, "oak_planks": 3}),
    "bread": (True, 1, {"wheat": 3}),
}


def sim_ray_cells(start: Sequence[float], end: Sequence[float]) -> List[Tuple[int, int, int]]:
    """Supercover DDA matching core/block_geometry._ray_cells."""
    cell = [math.floor(v) for v in start]
    final = tuple(math.floor(v) for v in end)
    out = [tuple(cell)]
    delta = [end[i] - start[i] for i in range(3)]
    step = [1 if v > 0 else -1 if v < 0 else 0 for v in delta]
    interval = [abs(1.0 / v) if v else math.inf for v in delta]
    crossing = [
        ((cell[i] + (1 if step[i] > 0 else 0) - start[i]) / delta[i]) if step[i] else math.inf
        for i in range(3)
    ]
    for _ in range(256):
        if tuple(cell) == final:
            break
        nearest = min(crossing)
        if nearest > 1.0 + 1e-10:
            break
        axes = [i for i in range(3) if abs(crossing[i] - nearest) <= 1e-10]
        for length in range(1, len(axes) + 1):
            for subset in itertools.combinations(axes, length):
                touched = cell.copy()
                for axis in subset:
                    touched[axis] += step[axis]
                out.append(tuple(touched))
        for axis in axes:
            cell[axis] += step[axis]
            crossing[axis] += interval[axis]
    return out


class SimAPIError(RuntimeError):
    """Lightweight error mirroring mc-agent-api error envelopes."""

    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(f"{code}: {message}")


class VirtualMcApiServer:
    """In-memory 3D Minecraft world + mc-agent-api protocol engine."""

    def __init__(
        self,
        *,
        spawn_pos: Tuple[float, float, float] = (0.5, 64.0, 0.5),
        dimension: str = "overworld",
        loaded_radius: int = 96,
    ) -> None:
        self._lock = threading.RLock()
        self.blocks: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
        self.containers: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
        self.pos: List[float] = [float(spawn_pos[0]), float(spawn_pos[1]), float(spawn_pos[2])]
        self.yaw: float = 0.0
        self.pitch: float = 0.0
        self.dimension: str = dimension
        self.loaded_radius: int = loaded_radius
        self.alive: bool = True
        self.ready: bool = True
        self.health: float = 20.0
        self.food: int = 20
        self.saturation: float = 5.0
        self.oxygen: int = 20
        self.armor_points: int = 0
        self.on_fire: bool = False
        self.is_sleeping: bool = False
        self.sneaking: bool = False
        self.jumping: bool = False
        self.blocking: bool = False
        self.in_vehicle: Optional[Dict[str, Any]] = None
        self.time_of_day: int = 1000
        self.inventory_counts: Dict[str, int] = {}
        self.held_item: Optional[str] = None
        self.offhand_item: Optional[str] = None
        self.armor: Dict[str, Optional[str]] = {"head": None, "chest": None, "legs": None, "feet": None}
        self.entities: List[Dict[str, Any]] = []
        self.advancements_state: Dict[str, str] = {}
        self.events: List[Dict[str, Any]] = []
        self._event_seq: int = 0
        self._gravel_digs: int = 0

        # Telemetry & fault injection
        self.read_calls: List[Tuple[str, Any]] = []
        self.write_calls: List[Tuple[str, Any]] = []
        self.fault_queue: deque[Tuple[str, str]] = deque()  # (endpoint_or_*, error_code)
        self.token: str = "sim-token-123"
        self.logged_in: bool = True

    # ---- World Setup Helpers ----

    def set_block(
        self,
        x: int,
        y: int,
        z: int,
        name: str,
        properties: Optional[Dict[str, Any]] = None,
    ) -> None:
        key = (int(x), int(y), int(z))
        if name in AIR_BLOCKS:
            self.blocks.pop(key, None)
            return
        props = dict(properties or {})
        if name in LIQUID_BLOCKS and "level" not in props:
            props["level"] = "0"
        self.blocks[key] = {"name": name, "properties": props}

    def fill(
        self,
        x1: int,
        y1: int,
        z1: int,
        x2: int,
        y2: int,
        z2: int,
        name: str,
        properties: Optional[Dict[str, Any]] = None,
    ) -> None:
        for x in range(min(x1, x2), max(x1, x2) + 1):
            for y in range(min(y1, y2), max(y1, y2) + 1):
                for z in range(min(z1, z2), max(z1, z2) + 1):
                    self.set_block(x, y, z, name, properties)

    def block_name(self, x: int, y: int, z: int) -> str:
        rec = self.blocks.get((int(x), int(y), int(z)))
        return rec["name"] if rec else "air"

    def block_props(self, x: int, y: int, z: int) -> Dict[str, Any]:
        rec = self.blocks.get((int(x), int(y), int(z)))
        return dict(rec["properties"]) if rec else {}

    def give(self, item: str, count: int = 1) -> None:
        if count <= 0:
            return
        self.inventory_counts[item] = self.inventory_counts.get(item, 0) + int(count)

    def consume(self, item: str, count: int = 1) -> bool:
        have = self.inventory_counts.get(item, 0)
        if have < count:
            return False
        new_count = have - count
        if new_count <= 0:
            self.inventory_counts.pop(item, None)
            if self.held_item == item:
                self.held_item = None
            if self.offhand_item == item:
                self.offhand_item = None
        else:
            self.inventory_counts[item] = new_count
        return True

    def add_entity(
        self,
        entity_id: int,
        name: str,
        pos: Sequence[float],
        *,
        kind: str = "hostile",
        health: float = 20.0,
    ) -> None:
        self.entities.append({
            "id": int(entity_id),
            "name": name,
            "type": kind,
            "pos": [float(pos[0]), float(pos[1]), float(pos[2])],
            "health": float(health),
        })

    def push_event(self, event_type: str, **fields: Any) -> Dict[str, Any]:
        self._event_seq += 1
        evt = {"seq": self._event_seq, "t": time.time(), "type": event_type, **fields}
        self.events.append(evt)
        return evt

    def inject_fault(self, error_code: str, endpoint: str = "*") -> None:
        self.fault_queue.append((endpoint, error_code))

    def _consume_fault(self, endpoint: str) -> Optional[str]:
        if not self.fault_queue:
            return None
        target, code = self.fault_queue[0]
        if target == "*" or target == endpoint:
            self.fault_queue.popleft()
            return code
        return None

    # ---- Internal Physics / Exposure Helpers ----

    def _is_exposed(self, x: int, y: int, z: int) -> bool:
        for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
            if self.block_name(x + dx, y + dy, z + dz) in TRANSPARENT_FOR_EXPOSURE:
                return True
        return False

    def _is_passable(self, x: int, y: int, z: int) -> bool:
        return self.block_name(x, y, z) in NON_SOLID_BLOCKS and self.block_name(x, y, z) != "lava"

    def _is_solid(self, x: int, y: int, z: int) -> bool:
        return self.block_name(x, y, z) not in NON_SOLID_BLOCKS

    def _can_stand_at(self, x: int, y: int, z: int) -> bool:
        below = self.block_name(x, y - 1, z)
        if below not in NON_SOLID_BLOCKS:
            return self._is_passable(x, y, z) and self._is_passable(x, y + 1, z)
        return False

    def _feet_cell(self) -> Tuple[int, int, int]:
        return (math.floor(self.pos[0]), math.floor(self.pos[1]), math.floor(self.pos[2]))

    def _update_environmental_flags(self) -> Tuple[bool, bool, bool, bool]:
        fx, fy, fz = self._feet_cell()
        feet_b = self.block_name(fx, fy, fz)
        head_b = self.block_name(fx, fy + 1, fz)
        below_b = self.block_name(fx, fy - 1, fz)
        in_water = feet_b == "water" or head_b == "water"
        submerged = head_b == "water"
        in_lava = feet_b == "lava" or head_b == "lava"
        on_ground = below_b not in NON_SOLID_BLOCKS
        if feet_b == "nether_portal" or head_b == "nether_portal":
            if self.dimension == "overworld":
                self.dimension = "the_nether"
                self.advancements_state["minecraft:story/enter_the_nether"] = "completed"
        return on_ground, in_water, submerged, in_lava

    def _apply_gravity(self, x: int, y: int, z: int) -> None:
        # Check cells directly above (x, y, z) for falling sand/gravel.
        cy = y + 1
        while self.block_name(x, cy, z) in GRAVITY_BLOCKS:
            name = self.block_name(x, cy, z)
            props = self.block_props(x, cy, z)
            self.set_block(x, cy, z, "air")
            ny = cy - 1
            while ny > -64 and self.block_name(x, ny - 1, z) in NON_SOLID_BLOCKS:
                ny -= 1
            self.set_block(x, ny, z, name, props)
            cy += 1

    def _check_and_ignite_portal(self, x: int, y: int, z: int) -> bool:
        """Detect a valid 4x5 obsidian portal frame along X or Z containing (x, y, z)."""
        # Try both orientations: width along X (dx=1, dz=0) or along Z (dx=0, dz=1)
        for dx, dz in ((1, 0), (0, 1)):
            for shift_w in (0, -1):
                for shift_h in (0, -1, -2):
                    bx = x + shift_w * dx
                    by = y + shift_h
                    bz = z + shift_w * dz
                    # Interior is w in [0, 1], h in [0, 1, 2]; obsidian frame is w in {-1, 2} or h in {-1, 3}
                    ok = True
                    for h in range(0, 3):
                        for w in (0, 1):
                            ix, iy, iz = bx + w * dx, by + h, bz + w * dz
                            if self.block_name(ix, iy, iz) not in (AIR_BLOCKS | {"fire", "nether_portal"}):
                                ok = False
                                break
                        if not ok:
                            break
                    if not ok:
                        continue
                    # Bottom and top obsidian
                    for w in (0, 1):
                        if self.block_name(bx + w * dx, by - 1, bz + w * dz) != "obsidian":
                            ok = False
                        if self.block_name(bx + w * dx, by + 3, bz + w * dz) != "obsidian":
                            ok = False
                    # Left and right obsidian pillars
                    for h in range(0, 3):
                        if self.block_name(bx - dx, by + h, bz - dz) != "obsidian":
                            ok = False
                        if self.block_name(bx + 2 * dx, by + h, bz + 2 * dz) != "obsidian":
                            ok = False
                    if ok:
                        for h in range(0, 3):
                            for w in (0, 1):
                                self.set_block(bx + w * dx, by + h, bz + w * dz, "nether_portal")
                        return True
        return False

    def _pathfind(
        self,
        goal: Tuple[int, Optional[int], int],
        goal_range: float = 1.0,
        max_expansions: int = 8000,
    ) -> Optional[List[Tuple[int, int, int]]]:
        """BFS/A* pathfinder simulating mineflayer-pathfinder with canDig=False, scafoldingBlocks=[]."""
        start = self._feet_cell()
        gx, gy, gz = goal

        def reached(cell: Tuple[int, int, int]) -> bool:
            eff_range = max(goal_range, 0.25)
            if gy is None:
                return math.hypot(cell[0] - gx, cell[2] - gz) <= eff_range
            return math.dist(cell, (gx, gy, gz)) <= eff_range

        if reached(start):
            return [start]

        queue = deque([start])
        parents: Dict[Tuple[int, int, int], Optional[Tuple[int, int, int]]] = {start: None}
        expansions = 0
        while queue and expansions < max_expansions:
            cur = queue.popleft()
            expansions += 1
            cx, cy, cz = cur
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, nz = cx + dx, cz + dz
                # Try same level, step up 1, or drop up to 3
                candidates: List[int] = []
                if self._can_stand_at(nx, cy, nz):
                    candidates.append(cy)
                elif self._can_stand_at(nx, cy + 1, nz) and self._is_passable(cx, cy + 2, cz):
                    candidates.append(cy + 1)
                else:
                    for drop in (1, 2, 3):
                        if self._can_stand_at(nx, cy - drop, nz) and self._is_passable(nx, cy, nz):
                            candidates.append(cy - drop)
                            break
                for ny in candidates:
                    nxt = (nx, ny, nz)
                    if nxt in parents:
                        continue
                    if math.hypot(nx - start[0], nz - start[2]) > self.loaded_radius:
                        continue
                    parents[nxt] = cur
                    if reached(nxt):
                        path = [nxt]
                        p = cur
                        while p is not None:
                            path.append(p)
                            p = parents[p]
                        path.reverse()
                        return path
                    queue.append(nxt)
        return None

    # ---- Upstream mc-agent-api Dispatch ----

    def handle(self, method: str, endpoint: str, payload: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Process one mc-agent-api request and return exact HTTP JSON envelope."""
        payload = payload or {}
        params = params or {}
        ep = endpoint.lstrip("/")

        with self._lock:
            if ep == "auth/login":
                self.logged_in = True
                return {"ok": True, "token": self.token}
            if ep == "auth/logout":
                self.logged_in = False
                return {"ok": True}

            fault = self._consume_fault(ep)
            if fault is not None:
                return {"ok": False, "ms": 1, "error": {"code": fault, "message": f"injected fault {fault}"}}

            readonly_eps = {
                "status", "inventory", "find_blocks", "get_entities",
                "events", "advancements", "get_block", "stop", "connect", "disconnect",
            }
            if ep in readonly_eps:
                self.read_calls.append((ep, dict(payload or params)))
            else:
                self.write_calls.append((ep, dict(payload)))

            try:
                res = self._dispatch_inner(ep, payload, params)
                return {"ok": True, "ms": 1, "result": res}
            except SimAPIError as err:
                return {"ok": False, "ms": 1, "error": {"code": err.code, "message": err.message, **err.details}}

    def _dispatch_inner(self, ep: str, payload: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        if ep == "status":
            on_ground, in_water, submerged, in_lava = self._update_environmental_flags()
            nearest_hostile = None
            best_d = 999.0
            for ent in self.entities:
                if ent.get("type") == "hostile":
                    d = math.dist(self.pos, ent["pos"])
                    if d <= 24.0 and d < best_d:
                        best_d = d
                        nearest_hostile = {"id": ent["id"], "name": ent["name"], "dist": round(d, 2)}
            res: Dict[str, Any] = {
                "pos": [round(self.pos[0], 1), round(self.pos[1], 1), round(self.pos[2], 1)],
                "speed": 0.0,
                "velocity": [0.0, 0.0, 0.0],
                "yaw": round(self.yaw, 3),
                "on_ground": on_ground,
                "alive": self.alive,
                "ready": self.ready,
                "busy": None,
                "dimension": self.dimension,
                "health": self.health,
                "food": self.food,
                "saturation": self.saturation,
                "oxygen": self.oxygen,
                "armor_points": self.armor_points,
                "on_fire": self.on_fire,
                "in_water": in_water,
                "submerged": submerged,
                "in_lava": in_lava,
                "is_sleeping": self.is_sleeping,
                "in_vehicle": self.in_vehicle,
                "sneaking": self.sneaking,
                "jumping": self.jumping,
                "blocking": self.blocking,
                "held": {"name": self.held_item, "count": self.inventory_counts.get(self.held_item, 1)} if self.held_item else None,
                "offhand": {"name": self.offhand_item, "count": self.inventory_counts.get(self.offhand_item, 1)} if self.offhand_item else None,
                "time_of_day": self.time_of_day,
            }
            if nearest_hostile is not None:
                res["nearest_hostile"] = nearest_hostile
            return res

        if ep == "inventory":
            items = [
                {"name": name, "count": cnt, "slot": idx}
                for idx, (name, cnt) in enumerate(sorted(self.inventory_counts.items()))
                if cnt > 0
            ]
            empty_slots = max(0, 36 - len(items))
            return {
                "items": items,
                "armor": {k: ({"name": v, "count": 1} if v else None) for k, v in self.armor.items()},
                "offhand": {"name": self.offhand_item, "count": self.inventory_counts.get(self.offhand_item, 1)} if self.offhand_item else None,
                "held": {"name": self.held_item, "count": self.inventory_counts.get(self.held_item, 1)} if self.held_item else None,
                "empty_slots": empty_slots,
            }

        if ep == "get_block":
            raw_pos = payload.get("pos")
            if not isinstance(raw_pos, list) or len(raw_pos) != 3:
                raise SimAPIError("bad_args", "pos must be [x,y,z]")
            bx, by, bz = int(math.floor(raw_pos[0])), int(math.floor(raw_pos[1])), int(math.floor(raw_pos[2]))
            if math.hypot(bx - self.pos[0], bz - self.pos[2]) > self.loaded_radius:
                raise SimAPIError("not_loaded", "chunk not loaded")
            name = self.block_name(bx, by, bz)
            props = self.block_props(bx, by, bz)
            return {
                "name": name,
                "pos": [bx, by, bz],
                "properties": props,
                "diggable": name not in AIR_BLOCKS | LIQUID_BLOCKS | {"bedrock"},
                "hardness": 0.0 if name in AIR_BLOCKS else 1.5,
                "harvest_tool_ok": True,
            }

        if ep == "find_blocks":
            radius = int(payload.get("radius", 16))
            limit = int(payload.get("limit", 64))
            types = payload.get("types")
            type_set = set(types) if isinstance(types, list) and types else None
            px, py, pz = self.pos
            found: List[Dict[str, Any]] = []
            for (bx, by, bz), rec in self.blocks.items():
                name = rec["name"]
                if name in AIR_BLOCKS:
                    continue
                if type_set is not None and name not in type_set:
                    continue
                d = math.dist((px, py, pz), (bx + 0.5, by + 0.5, bz + 0.5))
                if d > radius:
                    continue
                if not self._is_exposed(bx, by, bz):
                    continue
                found.append({
                    "name": name,
                    "pos": [bx, by, bz],
                    "dist": round(d, 2),
                    "properties": dict(rec["properties"]),
                })
            found.sort(key=lambda item: item["dist"])
            if type_set is not None:
                return {"total": len(found), "blocks": found[:limit]}
            summary: Dict[str, Dict[str, Any]] = {}
            for row in found:
                nm = row["name"]
                if nm not in summary:
                    summary[nm] = {"count": 1, "nearest": row["pos"], "dist": row["dist"]}
                else:
                    summary[nm]["count"] += 1
            return {"radius": radius, "scanned": len(found), "blocks": summary}

        if ep == "get_entities":
            radius = float(payload.get("radius", 16))
            types = set(payload["types"]) if isinstance(payload.get("types"), list) else None
            out = []
            for ent in self.entities:
                d = math.dist(self.pos, ent["pos"])
                if d <= radius and (types is None or ent["name"] in types or ent.get("type") in types):
                    out.append({**ent, "dist": round(d, 2)})
            out.sort(key=lambda e: e["dist"])
            return {"entities": out}

        if ep == "events":
            since = int(params.get("since", 0))
            limit = int(params.get("limit", 50))
            matching = [e for e in self.events if e["seq"] > since]
            page = matching[:limit]
            return {
                "since": since,
                "last_seq": self._event_seq,
                "total": len(matching),
                "has_more": len(matching) > limit,
                "events": page,
            }

        if ep == "advancements":
            status_filter = payload.get("status", "all")
            rows = []
            for adv_id, st in sorted(self.advancements_state.items()):
                if status_filter == "all" or st == status_filter:
                    rows.append({"id": adv_id, "status": st})
            return {"ready": True, "advancements": rows}

        if ep == "move_to":
            if "entity_id" in payload:
                ent = next((e for e in self.entities if e["id"] == payload["entity_id"]), None)
                if ent is None:
                    raise SimAPIError("not_found", "entity not found")
                gx, gy, gz = int(math.floor(ent["pos"][0])), int(math.floor(ent["pos"][1])), int(math.floor(ent["pos"][2]))
            else:
                gx = int(math.floor(payload["x"]))
                gy = int(math.floor(payload["y"])) if "y" in payload and payload["y"] is not None else None
                gz = int(math.floor(payload["z"]))
            goal_range = float(payload.get("range", 1.0))
            path = self._pathfind((gx, gy, gz), goal_range=goal_range)
            if path is None:
                raise SimAPIError("path_not_found", "no path with canDig=false")
            dest = path[-1]
            self.pos = [dest[0] + 0.5, float(dest[1]), dest[2] + 0.5]
            self._update_environmental_flags()
            return {"arrived": True, "pos": [round(c, 1) for c in self.pos]}

        if ep == "dig":
            bx, by, bz = int(math.floor(payload["x"])), int(math.floor(payload["y"])), int(math.floor(payload["z"]))
            name = self.block_name(bx, by, bz)
            if name in AIR_BLOCKS:
                raise SimAPIError("air", "block is already air")
            if name in LIQUID_BLOCKS or name == "bedrock":
                raise SimAPIError("not_diggable", f"cannot dig {name}")
            if math.dist(self.pos, (bx + 0.5, by + 0.5, bz + 0.5)) > 4.8:
                # Auto-approach if reachable (matching upstream mineflayer dig behavior)
                path = self._pathfind((bx, by, bz), goal_range=3.5)
                if path is None:
                    raise SimAPIError("out_of_range", "target block unreachable for dig")
                dest = path[-1]
                self.pos = [dest[0] + 0.5, float(dest[1]), dest[2] + 0.5]
            self.set_block(bx, by, bz, "air")
            self._apply_gravity(bx, by, bz)
            drop = DROP_MAP.get(name, name)
            if name == "gravel":
                self._gravel_digs += 1
                drop = "flint" if self._gravel_digs % 2 == 0 else "gravel"
            if name in {"short_grass", "tall_grass", "oak_leaves", "birch_leaves", "spruce_leaves"}:
                drop = ""
            if payload.get("collect", True) and drop:
                self.give(drop, 1)
                self.push_event("pickup", item=drop, count=1)
            return {"dug": name, "pos": [bx, by, bz], "dropped": drop or None}

        if ep == "place":
            item = str(payload["item"])
            bx, by, bz = int(math.floor(payload["x"])), int(math.floor(payload["y"])), int(math.floor(payload["z"]))
            if self.inventory_counts.get(item, 0) < 1:
                raise SimAPIError("missing_item", f"no {item} in inventory")
            if math.dist(self.pos, (bx + 0.5, by + 0.5, bz + 0.5)) > 5.0:
                raise SimAPIError("out_of_range", "place target too far")
            if self.block_name(bx, by, bz) not in NON_SOLID_BLOCKS:
                raise SimAPIError("occupied", "target cell already occupied")
            self.consume(item, 1)
            self.set_block(bx, by, bz, item)
            if item == "furnace":
                self.containers[(bx, by, bz)] = {"type": "furnace", "input": None, "fuel": None, "output": None}
            elif item == "chest":
                self.containers[(bx, by, bz)] = {"type": "chest", "items": {}}
            return {"placed": item, "pos": [bx, by, bz]}

        if ep == "interact":
            raw_pos = payload.get("pos")
            item = payload.get("item")
            if raw_pos is None:
                raise SimAPIError("bad_args", "interact requires pos")
            tx, ty, tz = int(math.floor(raw_pos[0])), int(math.floor(raw_pos[1])), int(math.floor(raw_pos[2]))
            eye = (self.pos[0], self.pos[1] + 1.62, self.pos[2])

            if item == "bucket":
                if self.inventory_counts.get("bucket", 0) < 1:
                    raise SimAPIError("missing_item", "no empty bucket")
                blk = self.block_name(tx, ty, tz)
                props = self.block_props(tx, ty, tz)
                if blk not in LIQUID_BLOCKS or str(props.get("level", "0")) != "0":
                    raise SimAPIError("not_liquid_source", f"block {blk} is not a liquid source")
                # Exact upstream useBucket constraints: feet dist <= 3.1, eye-to-(y+0.85) <= 4.5, clear ray!
                aim = (tx + 0.5, ty + 0.85, tz + 0.5)
                if math.dist(self.pos, (tx + 0.5, ty + 0.5, tz + 0.5)) > 3.2 or math.dist(eye, aim) > 4.5:
                    raise SimAPIError("out_of_range", "liquid source out of bucket reach")
                for cell in sim_ray_cells(eye, aim):
                    if cell == (tx, ty, tz):
                        break
                    cname = self.block_name(*cell)
                    if cname not in AIR_BLOCKS:
                        raise SimAPIError("target_occluded", f"ray to {aim} blocked by {cname} at {cell}", {"blocker": list(cell), "blocker_name": cname})
                self.consume("bucket", 1)
                self.give(f"{blk}_bucket", 1)
                # Real Minecraft 2x2 infinite water source rule: reforms if >= 2 horizontal water sources neighbor it
                if blk == "water":
                    water_adj = sum(
                        1 for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1))
                        if self.block_name(tx + dx, ty, tz + dz) == "water"
                        and str(self.block_props(tx + dx, ty, tz + dz).get("level", "0")) == "0"
                    )
                    if water_adj < 2:
                        self.set_block(tx, ty, tz, "air")
                else:
                    self.set_block(tx, ty, tz, "air")
                return {"filled": f"{blk}_bucket", "pos": [tx, ty, tz]}

            if item in {"water_bucket", "lava_bucket"}:
                if self.inventory_counts.get(item, 0) < 1:
                    raise SimAPIError("missing_item", f"no {item}")
                aim = (tx + 0.5, ty - 0.01, tz + 0.5)
                if math.dist(eye, aim) > 4.8:
                    raise SimAPIError("out_of_range", "pour target out of reach")
                if not self._is_solid(tx, ty - 1, tz):
                    raise SimAPIError("no_support", "pour requires solid support below")
                for cell in sim_ray_cells(eye, aim):
                    if cell in {(tx, ty, tz), (tx, ty - 1, tz)}:
                        break
                    cname = self.block_name(*cell)
                    if cname not in AIR_BLOCKS:
                        raise SimAPIError("target_occluded", f"pour ray blocked by {cname} at {cell}", {"blocker": list(cell)})
                liquid = "water" if item == "water_bucket" else "lava"
                self.consume(item, 1)
                self.give("bucket", 1)
                existing = self.block_name(tx, ty, tz)
                if liquid == "water" and existing == "lava":
                    self.set_block(tx, ty, tz, "obsidian")
                else:
                    self.set_block(tx, ty, tz, liquid, {"level": "0"})
                    # Water next to stationary lava casts obsidian!
                    if liquid == "water":
                        for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                            nx, ny, nz = tx + dx, ty + dy, tz + dz
                            if self.block_name(nx, ny, nz) == "lava" and str(self.block_props(nx, ny, nz).get("level", "0")) == "0":
                                self.set_block(nx, ny, nz, "obsidian")
                    elif liquid == "lava":
                        for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, 0, 1), (0, 0, -1)):
                            if self.block_name(tx + dx, ty + dy, tz + dz) == "water":
                                self.set_block(tx, ty, tz, "obsidian")
                                break
                return {"poured": liquid, "pos": [tx, ty, tz]}

            if item == "flint_and_steel":
                if self.inventory_counts.get("flint_and_steel", 0) < 1:
                    raise SimAPIError("missing_item", "no flint_and_steel")
                if math.dist(self.pos, (tx + 0.5, ty + 0.5, tz + 0.5)) > 4.8:
                    raise SimAPIError("out_of_range", "ignition target too far")
                # Allow clicking either bottom obsidian (ignites cell above) or air cell inside frame
                ignite_cell = (tx, ty + 1, tz) if self.block_name(tx, ty, tz) == "obsidian" else (tx, ty, tz)
                lit = self._check_and_ignite_portal(*ignite_cell)
                if not lit:
                    self.set_block(*ignite_cell, "fire")
                return {"ignited": True, "portal": lit, "pos": list(ignite_cell)}

            # Bed sleep / wake
            blk = self.block_name(tx, ty, tz)
            if blk.endswith("_bed"):
                if self.is_sleeping:
                    self.is_sleeping = False
                    self.push_event("wake")
                    return {"sleeping": False}
                if 12541 <= (self.time_of_day % 24000) <= 23458:
                    self.is_sleeping = True
                    self.push_event("sleep")
                    return {"sleeping": True}
                raise SimAPIError("cannot_sleep", "you can only sleep at night")

            return {"interacted": blk, "pos": [tx, ty, tz]}

        if ep == "craft":
            item = str(payload["item"])
            count = max(1, int(payload.get("count", 1)))
            if item not in SIM_RECIPES:
                raise SimAPIError("no_recipe", f"unknown recipe {item}")
            needs_table, out_per, ingredients = SIM_RECIPES[item]
            if needs_table:
                has_table = any(
                    rec["name"] == "crafting_table" and math.dist(self.pos, (bx + 0.5, by + 0.5, bz + 0.5)) <= 5.0
                    for (bx, by, bz), rec in self.blocks.items()
                )
                if not has_table:
                    raise SimAPIError("no_crafting_table", "requires nearby crafting_table")
            batches = math.ceil(count / out_per)
            # Planks wildcard: accept any *_planks for oak_planks in craft
            for ing, req_per in ingredients.items():
                req = req_per * batches
                if self.inventory_counts.get(ing, 0) < req:
                    raise SimAPIError("missing_ingredients", f"need {req} {ing}")
            for ing, req_per in ingredients.items():
                self.consume(ing, req_per * batches)
            self.give(item, out_per * batches)
            return {"crafted": item, "count": out_per * batches}

        if ep == "window/view":
            pos = tuple(int(v) for v in payload["target"]["pos"])
            c = self.containers.get(pos)
            if c is None:
                blk = self.block_name(*pos)
                if blk == "furnace":
                    c = {"type": "furnace", "input": None, "fuel": None, "output": None}
                    self.containers[pos] = c
                elif blk == "chest":
                    c = {"type": "chest", "items": {}}
                    self.containers[pos] = c
                else:
                    raise SimAPIError("not_container", f"{blk} is not a container")
            return {"container": dict(c), "pos": list(pos)}

        if ep == "window/act":
            pos = tuple(int(v) for v in payload["target"]["pos"])
            ops = payload.get("ops", [])
            c = self.containers.setdefault(pos, {"type": self.block_name(*pos), "input": None, "fuel": None, "output": None, "items": {}})
            for op in ops:
                action = op.get("op") or op.get("action")
                item = op.get("item")
                cnt = int(op.get("count", 1))
                slot = op.get("slot", "")
                if c.get("type") == "furnace":
                    if action == "put" and slot == "input":
                        if not self.consume(item, cnt):
                            raise SimAPIError("missing_item", f"missing {item}")
                        cur = c.get("input") or {"name": item, "count": 0}
                        cur["count"] += cnt
                        c["input"] = cur
                    elif action == "put" and slot == "fuel":
                        if not self.consume(item, cnt):
                            raise SimAPIError("missing_item", f"missing {item}")
                        cur = c.get("fuel") or {"name": item, "count": 0}
                        cur["count"] += cnt
                        c["fuel"] = cur
                    # Auto-complete smelting immediately when input + fuel present
                    if c.get("input") and c.get("fuel"):
                        in_name = c["input"]["name"]
                        in_cnt = c["input"]["count"]
                        smelt_out = {"raw_iron": "iron_ingot", "raw_gold": "gold_ingot", "cobblestone": "stone", "sand": "glass", "oak_log": "charcoal"}.get(in_name, in_name)
                        out = c.get("output") or {"name": smelt_out, "count": 0}
                        out["count"] += in_cnt
                        c["output"] = out
                        c["input"] = None
                    if action == "take" and slot == "output":
                        out = c.get("output")
                        if out and out["count"] > 0:
                            take_n = min(cnt, out["count"])
                            self.give(out["name"], take_n)
                            out["count"] -= take_n
                            if out["count"] <= 0:
                                c["output"] = None
                else:
                    store = c.setdefault("items", {})
                    if action == "deposit":
                        if not self.consume(item, cnt):
                            raise SimAPIError("missing_item", f"missing {item}")
                        store[item] = store.get(item, 0) + cnt
                    elif action == "withdraw":
                        avail = store.get(item, 0)
                        if avail < cnt:
                            raise SimAPIError("missing_item", f"chest missing {item}")
                        store[item] = avail - cnt
                        if store[item] <= 0:
                            store.pop(item, None)
                        self.give(item, cnt)
            return {"ok": True, "container": dict(c)}

        if ep == "equip":
            item = str(payload["item"])
            slot = str(payload.get("slot", "hand"))
            if self.inventory_counts.get(item, 0) < 1:
                raise SimAPIError("missing_item", f"no {item} to equip")
            if slot == "hand":
                self.held_item = item
            elif slot == "off-hand":
                self.offhand_item = item
            elif slot == "torso":
                self.armor["chest"] = item
            elif slot in self.armor:
                self.armor[slot] = item
            return {"equipped": item, "slot": slot}

        if ep == "use_item":
            item = str(payload["item"])
            if self.inventory_counts.get(item, 0) < 1:
                raise SimAPIError("missing_item", f"no {item}")
            if item in FOOD_RESTORE:
                food_gain, sat_gain = FOOD_RESTORE[item]
                self.consume(item, 1)
                self.food = min(20, self.food + food_gain)
                self.saturation = min(20.0, self.saturation + sat_gain)
                return {"ate": item, "food": self.food}
            if item == "ender_eye":
                self.consume(item, 1)
                return {"used": "ender_eye"}
            return {"used": item}

        if ep == "posture":
            self.sneaking = bool(payload.get("sneak", False))
            self.blocking = bool(payload.get("shield", False))
            self.jumping = bool(payload.get("jump", False))
            return {"sneaking": self.sneaking, "blocking": self.blocking, "jumping": self.jumping}

        if ep == "look":
            tx, ty, tz = float(payload["x"]), float(payload["y"]), float(payload["z"])
            dx, dz = tx - self.pos[0], tz - self.pos[2]
            self.yaw = math.atan2(-dx, dz)
            return {"yaw": round(self.yaw, 3)}

        if ep == "control":
            ms = int(payload.get("ms", 100))
            yaw = float(payload["yaw"]) if payload.get("yaw") is not None else self.yaw
            self.yaw = yaw
            step_dist = min(1.5, (ms / 1000.0) * 4.3)
            if payload.get("forward"):
                dx = -math.sin(yaw) * step_dist
                dz = math.cos(yaw) * step_dist
                nx, nz = self.pos[0] + dx, self.pos[2] + dz
                ny = self.pos[1]
                # Buoyancy if swimming with jump
                fx, fy, fz = math.floor(nx), math.floor(ny), math.floor(nz)
                if payload.get("jump") and (self.block_name(fx, fy, fz) == "water" or self.block_name(fx, fy + 1, fz) == "water"):
                    ny = float(fy + 1) if self.block_name(fx, fy + 1, fz) in (AIR_BLOCKS | {"water"}) else ny
                    self.oxygen = 20
                if self._is_passable(math.floor(nx), math.floor(ny), math.floor(nz)):
                    self.pos = [nx, ny, nz]
                elif payload.get("jump") and self._can_stand_at(math.floor(nx), math.floor(ny) + 1, math.floor(nz)):
                    self.pos = [nx, float(math.floor(ny) + 1), nz]
            elif payload.get("jump"):
                fx, fy, fz = self._feet_cell()
                if self._is_passable(fx, fy + 2, fz):
                    self.pos[1] = float(fy + 1)
            self._update_environmental_flags()
            return {"pos": [round(c, 1) for c in self.pos]}

        if ep == "attack":
            eid = int(payload["entity_id"])
            ent = next((e for e in self.entities if e["id"] == eid), None)
            if ent is None:
                raise SimAPIError("not_found", "entity not found")
            if math.dist(self.pos, ent["pos"]) > 4.5:
                raise SimAPIError("out_of_range", "entity too far")
            ent["health"] -= 10.0
            killed = ent["health"] <= 0
            if killed:
                self.entities = [e for e in self.entities if e["id"] != eid]
                if ent["name"] in {"cow", "pig", "sheep", "chicken"}:
                    drop = {"cow": "beef", "pig": "porkchop", "sheep": "white_wool", "chicken": "chicken"}[ent["name"]]
                    self.give(drop, 2)
            return {"hits": 1, "killed": killed}

        if ep == "shoot":
            eid = int(payload["entity_id"])
            ent = next((e for e in self.entities if e["id"] == eid), None)
            if ent is None:
                raise SimAPIError("not_found", "entity not found")
            ent["health"] -= 8.0
            killed = ent["health"] <= 0
            if killed:
                self.entities = [e for e in self.entities if e["id"] != eid]
            return {"fired": True, "killed": killed}

        if ep == "drop":
            item = str(payload["item"])
            cnt = int(payload.get("count", self.inventory_counts.get(item, 0)))
            if not self.consume(item, cnt):
                raise SimAPIError("missing_item", f"cannot drop {cnt} {item}")
            return {"dropped": item, "count": cnt}

        if ep == "dismount":
            self.in_vehicle = None
            return {"dismounted": True}

        if ep == "chat":
            self.push_event("chat", message=str(payload.get("message", "")))
            return {"sent": True}

        if ep in {"stop", "connect", "disconnect"}:
            return {"ok": True}

        raise SimAPIError("unknown_api", f"unknown endpoint {ep}")


class VirtualMcHttpServer:
    """Real loopback HTTP server wrapping VirtualMcApiServer for L0 client integration tests."""

    def __init__(self, sim: Optional[VirtualMcApiServer] = None) -> None:
        self.sim = sim or VirtualMcApiServer()
        self._server: Optional[socketserver.TCPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.base_url: str = ""

    def start(self) -> str:
        sim = self.sim

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def _serve(self, method: str) -> None:
                parsed = urlsplit(self.path)
                ep = parsed.path
                if ep.startswith("/api/v1/"):
                    ep = ep[len("/api/v1/"):]
                params = {k: v[0] for k, v in parse_qs(parsed.query).items() if v}
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length > 0 else b""
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                body = sim.handle(method, ep, payload=payload, params=params)
                encoded = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:
                self._serve("GET")

            def do_POST(self) -> None:
                self._serve("POST")

        class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._server = ThreadingServer(("127.0.0.1", 0), Handler)
        port = self._server.server_address[1]
        self.base_url = f"http://127.0.0.1:{port}/api/v1"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self) -> "VirtualMcHttpServer":
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()
