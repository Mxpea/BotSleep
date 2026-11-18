"""3D graybox voxel scene collector and serializer for the Gradio developer workbench."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
import zlib
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from core.world_3d import Coord3D, VoxelWorld3D, norm_coord

AIR_BLOCKS = frozenset({"air", "cave_air", "void_air", "unknown"})

ORE_BLOCKS = frozenset({
    "coal_ore", "deepslate_coal_ore",
    "iron_ore", "deepslate_iron_ore",
    "copper_ore", "deepslate_copper_ore",
    "gold_ore", "deepslate_gold_ore", "nether_gold_ore",
    "redstone_ore", "deepslate_redstone_ore",
    "lapis_ore", "deepslate_lapis_ore",
    "diamond_ore", "deepslate_diamond_ore",
    "emerald_ore", "deepslate_emerald_ore",
    "nether_quartz_ore", "ancient_debris",
})

HAZARD_BLOCKS = frozenset({
    "lava", "flowing_lava", "water", "flowing_water",
    "fire", "soul_fire", "magma_block", "cactus",
    "campfire", "soul_campfire", "powder_snow",
    "sweet_berry_bush", "tnt",
})

STATION_BLOCKS = frozenset({
    "crafting_table", "furnace", "blast_furnace", "smoker",
    "chest", "trapped_chest", "barrel", "ender_chest",
    "anvil", "chipped_anvil", "damaged_anvil",
    "enchanting_table", "brewing_stand", "smithing_table", "loom",
    "torch", "wall_torch", "soul_torch", "soul_wall_torch",
})

FOLIAGE_EXACT = frozenset({
    "vine", "short_grass", "tall_grass", "fern", "large_fern",
    "dead_bush", "seagrass", "tall_seagrass", "kelp", "kelp_plant",
    "sugar_cane", "bamboo",
})

# Distinct muted graybox tones for terrain, plus high-contrast developer accents.
BLOCK_COLORS: Dict[str, str] = {
    "stone": "#7c8694",
    "deepslate": "#525b68",
    "cobbled_deepslate": "#5a6370",
    "cobblestone": "#6e7784",
    "granite": "#8f756b",
    "diorite": "#a9b1ba",
    "andesite": "#7e858c",
    "tuff": "#666b66",
    "dirt": "#6f5d4b",
    "coarse_dirt": "#655444",
    "grass_block": "#68825a",
    "podzol": "#6a5842",
    "sand": "#b5aa89",
    "red_sand": "#a3704d",
    "sandstone": "#ada17f",
    "gravel": "#807a78",
    "clay": "#8994a2",
    "netherrack": "#78464a",
    "obsidian": "#352849",
    "bedrock": "#2e3138",
    "coal_ore": "#252930",
    "deepslate_coal_ore": "#1e2228",
    "iron_ore": "#e0a57c",
    "deepslate_iron_ore": "#d6986d",
    "copper_ore": "#d97b4c",
    "deepslate_copper_ore": "#cc6e40",
    "gold_ore": "#f5c842",
    "deepslate_gold_ore": "#eab932",
    "nether_gold_ore": "#f2be35",
    "redstone_ore": "#e03f4c",
    "deepslate_redstone_ore": "#cf323f",
    "lapis_ore": "#3d74e0",
    "deepslate_lapis_ore": "#3264cc",
    "diamond_ore": "#4ee4e8",
    "deepslate_diamond_ore": "#36d4dc",
    "emerald_ore": "#39d977",
    "deepslate_emerald_ore": "#2bc766",
    "ancient_debris": "#8c563f",
    "lava": "#ff5b24",
    "flowing_lava": "#ff6f3c",
    "water": "#3385ff",
    "flowing_water": "#4994ff",
    "fire": "#ff8f2b",
    "soul_fire": "#4cd6e8",
    "magma_block": "#cc4b28",
    "crafting_table": "#c89254",
    "furnace": "#8d959e",
    "blast_furnace": "#7c8591",
    "smoker": "#827467",
    "chest": "#d19f47",
    "trapped_chest": "#d18747",
    "barrel": "#9e774c",
    "torch": "#ffe066",
    "wall_torch": "#ffe066",
}


def classify_block(name: str) -> str:
    """Group a Minecraft block name into a graybox visualization layer."""
    norm = str(name or "").strip().lower()
    if not norm or norm in AIR_BLOCKS:
        return "air"
    if norm in ORE_BLOCKS or norm.endswith("_ore"):
        return "ore"
    if norm in HAZARD_BLOCKS:
        return "hazard"
    if (
        norm in STATION_BLOCKS
        or norm.endswith("_bed")
        or norm.endswith("_door")
        or norm.endswith("_shulker_box")
    ):
        return "station"
    if norm in FOLIAGE_EXACT or norm.endswith("_leaves") or norm.endswith("_sapling"):
        return "foliage"
    return "terrain"


def block_color(name: str, category: Optional[str] = None) -> str:
    """Return a muted graybox or accent hex color for a block."""
    norm = str(name or "").strip().lower()
    if norm in BLOCK_COLORS:
        return BLOCK_COLORS[norm]
    cat = category or classify_block(norm)
    if cat == "ore":
        return "#4ee4e8"
    if cat == "hazard":
        return "#ff5b24"
    if cat == "station":
        return "#e0ae5c"
    if cat == "foliage":
        return "#4d6b47"
    if norm.endswith("_log") or norm.endswith("_wood"):
        return "#7d6447"
    if norm.endswith("_planks") or norm.endswith("_slab") or norm.endswith("_stairs"):
        return "#9e8560"
    if "deepslate" in norm:
        return "#545d6a"
    return "#7b8491"


def _normalize_dimension(dimension: Optional[str]) -> str:
    dim = str(dimension or "overworld").strip()
    if dim.startswith("minecraft:"):
        return dim[len("minecraft:"):]
    return dim or "overworld"


def find_world_3d_db(
    *,
    endpoint: str,
    username: str,
    world_id: str,
    dimension: str = "overworld",
    base_dir: str | Path = "runtime/world_3d",
) -> Optional[Path]:
    """Locate an existing persisted VoxelWorld3D SQLite database without creating files."""
    candidates = []
    norm_dim = _normalize_dimension(dimension)
    for dim_variant in (dimension, norm_dim, f"minecraft:{norm_dim}"):
        if dim_variant and dim_variant not in candidates:
            candidates.append(dim_variant)
    for dim in candidates:
        scope_payload = json.dumps(
            {
                "endpoint": str(endpoint).rstrip("/"),
                "username": str(username),
                "world_id": str(world_id),
                "dimension": str(dim),
            },
            sort_keys=True,
        )
        scope_hash = hashlib.sha256(scope_payload.encode("utf-8")).hexdigest()[:24]
        candidate = Path(base_dir) / scope_hash / "world_3d.sqlite3"
        if candidate.is_file():
            return candidate
    return None


def read_world_3d_readonly(db_path: Path) -> Dict[str, Any]:
    """Read voxels and exploration topology from a VoxelWorld3D SQLite DB in read-only mode."""
    result: Dict[str, Any] = {
        "voxels": {},
        "visited_stances": [],
        "open_frontiers": [],
        "ore_registry": [],
        "blocked_edges": [],
        "placed_scaffolds": [],
    }
    if not db_path.is_file():
        return result
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=1.5)) as conn:
            for (blob,) in conn.execute("SELECT payload FROM chunks").fetchall():
                raw_map = json.loads(zlib.decompress(bytes(blob)).decode("utf-8"))
                for coord_str, bname in raw_map.items():
                    xs, ys, zs = coord_str.split(",")
                    result["voxels"][(int(xs), int(ys), int(zs))] = str(bname)
            blob_rows = {
                key: json.loads(zlib.decompress(bytes(payload)).decode("utf-8"))
                for key, payload in conn.execute("SELECT key, payload FROM state_blobs").fetchall()
            }
    except (sqlite3.Error, ValueError, zlib.error, OSError):
        return result

    result["visited_stances"] = [
        [norm_coord(pos), int(cnt)]
        for pos, cnt in blob_rows.get("visited_stances", [])
        if isinstance(pos, (list, tuple)) and len(pos) == 3
    ]
    result["open_frontiers"] = [
        dict(row) for row in blob_rows.get("open_frontiers", [])
        if isinstance(row, dict) and isinstance(row.get("pos"), (list, tuple))
    ]
    result["ore_registry"] = [
        dict(row) for row in blob_rows.get("ore_registry", [])
        if isinstance(row, dict) and isinstance(row.get("pos"), (list, tuple))
    ]
    result["blocked_edges"] = [
        [list(norm_coord(u)), list(norm_coord(v)), int(cnt)]
        for u, v, cnt in blob_rows.get("blocked_edges", [])
        if isinstance(u, (list, tuple)) and isinstance(v, (list, tuple))
    ]
    result["placed_scaffolds"] = [
        list(norm_coord(pos)) for pos in blob_rows.get("placed_scaffolds", [])
        if isinstance(pos, (list, tuple)) and len(pos) == 3
    ]
    return result


class Scene3DCollector:
    """Thread-safe aggregator for Three.js graybox scene data."""

    MAX_MANUAL_VOXELS = 4096

    def __init__(self, *, world_3d_base_dir: str | Path = "runtime/world_3d") -> None:
        self._lock = threading.RLock()
        self.world_3d_base_dir = Path(world_3d_base_dir)
        self._manual_voxels: Dict[Tuple[str, int, int, int], str] = {}
        self._manual_scan_meta: Optional[Dict[str, Any]] = None
        self._bot_trail: List[List[float]] = []

    def record_bot_pos(self, status: Optional[Mapping[str, Any]]) -> None:
        if not isinstance(status, Mapping):
            return
        pos = status.get("pos")
        if not isinstance(pos, (list, tuple)) or len(pos) != 3:
            return
        try:
            pt = [round(float(pos[0]), 2), round(float(pos[1]), 2), round(float(pos[2]), 2)]
        except (TypeError, ValueError):
            return
        if not all(math.isfinite(v) for v in pt):
            return
        with self._lock:
            if self._bot_trail:
                last = self._bot_trail[-1]
                if math.dist(last, pt) < 0.35:
                    return
            self._bot_trail.append(pt)
            if len(self._bot_trail) > 250:
                self._bot_trail = self._bot_trail[-250:]

    def scan_local_box(
        self,
        client: Any,
        *,
        radius_xz: int = 4,
        radius_y: int = 3,
        max_queries: int = 600,
    ) -> Dict[str, Any]:
        """Explicitly probe a bounded 3D voxel box around the bot via client.get_block."""
        rx = max(1, min(6, int(radius_xz)))
        ry = max(1, min(5, int(radius_y)))
        status = client.get_status()["result"]
        pos = status.get("pos")
        if not isinstance(pos, (list, tuple)) or len(pos) != 3:
            raise ValueError("Bot position unavailable for 3D scan")
        dim = _normalize_dimension(status.get("dimension"))
        cx, cy, cz = int(math.floor(pos[0])), int(math.floor(pos[1])), int(math.floor(pos[2]))

        queried = 0
        solid_count = 0
        not_loaded = 0
        errors = 0
        scanned_entries: Dict[Tuple[str, int, int, int], str] = {}

        # Sort cells from center outward so if max_queries caps the box, inner shells win.
        offsets: List[Tuple[int, int, int, float]] = []
        for dx in range(-rx, rx + 1):
            for dy in range(-ry, ry + 1):
                for dz in range(-rx, rx + 1):
                    offsets.append((dx, dy, dz, dx * dx + dy * dy + dz * dz))
        offsets.sort(key=lambda item: item[3])

        for dx, dy, dz, _ in offsets:
            if queried >= max_queries:
                break
            qx, qy, qz = cx + dx, cy + dy, cz + dz
            queried += 1
            try:
                resp = client.get_block([qx, qy, qz])
                block = resp.get("result") if isinstance(resp, dict) else None
                name = str((block or {}).get("name") or "air")
                scanned_entries[(dim, qx, qy, qz)] = name
                if classify_block(name) != "air":
                    solid_count += 1
            except Exception as exc:
                code = getattr(exc, "code", "")
                if code == "not_loaded":
                    not_loaded += 1
                else:
                    errors += 1

        with self._lock:
            self._manual_voxels.update(scanned_entries)
            while len(self._manual_voxels) > self.MAX_MANUAL_VOXELS:
                oldest = next(iter(self._manual_voxels))
                self._manual_voxels.pop(oldest, None)
            self._manual_scan_meta = {
                "center": [cx, cy, cz],
                "dimension": dim,
                "radius_xz": rx,
                "radius_y": ry,
                "queried": queried,
                "solid_count": solid_count,
                "not_loaded": not_loaded,
                "errors": errors,
                "scanned_at": time.time(),
            }
            return dict(self._manual_scan_meta)

    def clear_manual_scan(self) -> None:
        with self._lock:
            self._manual_voxels.clear()
            self._manual_scan_meta = None

    def build_scene(
        self,
        *,
        observation: Optional[Mapping[str, Any]] = None,
        status: Optional[Mapping[str, Any]] = None,
        world_3d: Optional[VoxelWorld3D] = None,
        scope: Optional[Mapping[str, Any]] = None,
        radius: int = 32,
        max_voxels: int = 3500,
    ) -> Dict[str, Any]:
        """Build a bounded, JSON-serializable 3D graybox scene payload."""
        obs = observation if isinstance(observation, Mapping) else {}
        bot_status = dict(status) if isinstance(status, Mapping) else dict(obs.get("status") or {})
        self.record_bot_pos(bot_status)

        raw_pos = bot_status.get("pos")
        if isinstance(raw_pos, (list, tuple)) and len(raw_pos) == 3:
            origin = [float(raw_pos[0]), float(raw_pos[1]), float(raw_pos[2])]
        else:
            origin = [0.0, 64.0, 0.0]
        dim = _normalize_dimension(bot_status.get("dimension"))

        voxel_map: Dict[Coord3D, Tuple[str, str]] = {}
        visited_stances: List[List[int]] = []
        frontiers: List[Dict[str, Any]] = []
        ores: List[Dict[str, Any]] = []
        blocked_edges: List[Dict[str, Any]] = []
        scaffolds: List[List[int]] = []
        world_source = None

        # 1. Load persisted or live VoxelWorld3D memory
        if world_3d is not None:
            world_source = "memory"
            for coord, bname in list(world_3d.voxels.items()):
                voxel_map[coord] = (str(bname), "world3d")
            visited_stances = [list(c) for c in list(world_3d.visited_stances.keys())[-250:]]
            frontiers = [
                {"pos": list(fn.pos), "op": fn.requires_op, "score": round(fn.last_score, 2), "fails": fn.fail_count}
                for fn in world_3d.open_frontiers.values()
            ]
            ores = [
                {"pos": list(on.pos), "name": on.name, "status": on.status}
                for on in world_3d.ore_registry.values()
            ]
            blocked_edges = [
                {"from": list(u), "to": list(v), "count": cnt}
                for (u, v), cnt in list(world_3d.blocked_edges.items())[:120]
            ]
            scaffolds = [list(c) for c in list(world_3d.placed_scaffolds)[:200]]
        elif isinstance(scope, Mapping) and scope.get("endpoint") and scope.get("username"):
            db_path = find_world_3d_db(
                endpoint=str(scope["endpoint"]),
                username=str(scope["username"]),
                world_id=str(scope.get("world_id") or "default"),
                dimension=str(bot_status.get("dimension") or "overworld"),
                base_dir=self.world_3d_base_dir,
            )
            if db_path is not None:
                world_source = "sqlite"
                wdata = read_world_3d_readonly(db_path)
                for coord, bname in wdata["voxels"].items():
                    voxel_map[coord] = (str(bname), "world3d")
                visited_stances = [list(pos) for pos, _ in wdata["visited_stances"][-250:]]
                frontiers = [
                    {
                        "pos": list(norm_coord(fn["pos"])),
                        "op": str(fn.get("requires_op", "walk")),
                        "score": round(float(fn.get("last_score", 0.0)), 2),
                        "fails": int(fn.get("fail_count", 0)),
                    }
                    for fn in wdata["open_frontiers"]
                ]
                ores = [
                    {
                        "pos": list(norm_coord(on["pos"])),
                        "name": str(on.get("name", "ore")),
                        "status": str(on.get("status", "pending")),
                    }
                    for on in wdata["ore_registry"]
                ]
                blocked_edges = [
                    {"from": u, "to": v, "count": cnt}
                    for u, v, cnt in wdata["blocked_edges"][:120]
                ]
                scaffolds = list(wdata["placed_scaffolds"][:200])

        # 2. Layer manual local 3D scan cache (fresher local reads)
        with self._lock:
            for (vdim, vx, vy, vz), bname in self._manual_voxels.items():
                if vdim == dim:
                    voxel_map[(vx, vy, vz)] = (bname, "scan3d")
            manual_meta = dict(self._manual_scan_meta) if self._manual_scan_meta else None
            bot_trail = list(self._bot_trail)

        # 3. Layer live ObservationCollector blocks & terrain (highest freshness)
        for source_key, source_tag in (("terrain", "terrain"), ("blocks", "obs")):
            for row in obs.get(source_key) or ():
                if not isinstance(row, Mapping):
                    continue
                pos = row.get("pos")
                name = row.get("name")
                if not isinstance(pos, (list, tuple)) or len(pos) != 3 or not isinstance(name, str):
                    continue
                try:
                    coord = (int(math.floor(float(pos[0]))), int(math.floor(float(pos[1]))), int(math.floor(float(pos[2]))))
                except (TypeError, ValueError):
                    continue
                voxel_map[coord] = (name, source_tag)

        # Filter non-air voxels within radius and prioritize important categories before distance
        ox, oy, oz = origin
        rad_sq = float(radius * radius)
        candidates: List[Tuple[int, float, int, int, int, str, str, str, str]] = []
        counts_by_category: Dict[str, int] = {"terrain": 0, "ore": 0, "hazard": 0, "station": 0, "foliage": 0}
        priority_map = {"ore": 0, "hazard": 0, "station": 1, "terrain": 2, "foliage": 3}

        y_min = int(math.floor(oy)) - 8
        y_max = int(math.floor(oy)) + 8

        for (vx, vy, vz), (bname, source_tag) in voxel_map.items():
            cat = classify_block(bname)
            if cat == "air":
                continue
            dx, dy, dz = vx + 0.5 - ox, vy + 0.5 - oy, vz + 0.5 - oz
            dist_sq = dx * dx + dy * dy + dz * dz
            if dist_sq > rad_sq:
                continue
            candidates.append((
                priority_map.get(cat, 4),
                dist_sq,
                vx, vy, vz,
                bname,
                cat,
                block_color(bname, cat),
                source_tag,
            ))

        candidates.sort(key=lambda item: (item[0], item[1]))
        selected = candidates[:max_voxels]

        voxels_out: List[Dict[str, Any]] = []
        for _, _, vx, vy, vz, bname, cat, color, source_tag in selected:
            counts_by_category[cat] = counts_by_category.get(cat, 0) + 1
            if vy < y_min:
                y_min = vy
            if vy > y_max:
                y_max = vy
            voxels_out.append({
                "x": vx,
                "y": vy,
                "z": vz,
                "name": bname,
                "cat": cat,
                "color": color,
                "src": source_tag,
            })

        entities_out: List[Dict[str, Any]] = []
        for ent in obs.get("entities") or ():
            if not isinstance(ent, Mapping):
                continue
            epos = ent.get("pos")
            if not isinstance(epos, (list, tuple)) or len(epos) != 3:
                continue
            entities_out.append({
                "id": ent.get("id"),
                "name": ent.get("name") or ent.get("type") or "entity",
                "type": ent.get("type") or "unknown",
                "pos": [round(float(epos[0]), 2), round(float(epos[1]), 2), round(float(epos[2]), 2)],
                "dist": ent.get("dist"),
            })

        stand_candidates = [
            row.get("pos") for row in (obs.get("safe_stand_candidates") or ())
            if isinstance(row, Mapping) and isinstance(row.get("pos"), (list, tuple)) and len(row["pos"]) == 3
        ]
        place_candidates = [
            row.get("pos") for row in (obs.get("placement_candidates") or ())
            if isinstance(row, Mapping) and isinstance(row.get("pos"), (list, tuple)) and len(row["pos"]) == 3
        ]

        return {
            "timestamp": time.time(),
            "bot": {
                "has_pos": isinstance(raw_pos, (list, tuple)) and len(raw_pos) == 3,
                "pos": [round(origin[0], 2), round(origin[1], 2), round(origin[2], 2)],
                "yaw": bot_status.get("yaw", 0.0),
                "pitch": bot_status.get("pitch", 0.0),
                "health": bot_status.get("health"),
                "food": bot_status.get("food"),
                "dimension": dim,
                "busy": bot_status.get("busy"),
                "is_day": bot_status.get("is_day"),
            },
            "y_bounds": {"min": y_min, "max": y_max},
            "voxels": voxels_out,
            "entities": entities_out,
            "trail": bot_trail,
            "visited_stances": visited_stances,
            "frontiers": frontiers[:200],
            "ores": ores[:200],
            "blocked_edges": blocked_edges,
            "scaffolds": scaffolds,
            "stand_candidates": stand_candidates,
            "place_candidates": place_candidates,
            "counts_by_category": counts_by_category,
            "total_cached": len(voxel_map),
            "world_source": world_source,
            "manual_scan": manual_meta,
        }
