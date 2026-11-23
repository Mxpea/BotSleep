"""Shared domain constants, pure coordinate/observation helpers, and typed contracts.

This module is pure and performs no network, disk, or clock I/O. Planner, policy,
observation, and harness layers import these primitives so coordinate parsing,
block-key formatting, inventory counting, and item catalogs stay consistent.
"""

import math
from collections.abc import Iterable, Mapping
from typing import Any, Dict, List, Optional, Set, Tuple, TypedDict


WOOD_SPECIES: Tuple[str, ...] = (
    "oak", "spruce", "birch", "jungle", "acacia", "dark_oak", "mangrove", "cherry", "pale_oak"
)
LOG_NAMES: Tuple[str, ...] = tuple(f"{wood}_log" for wood in WOOD_SPECIES)
PLANK_NAMES: Tuple[str, ...] = tuple(f"{wood}_planks" for wood in WOOD_SPECIES)
BED_COLORS: Tuple[str, ...] = (
    "white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray",
    "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black"
)
BED_NAMES: Tuple[str, ...] = tuple(f"{color}_bed" for color in BED_COLORS)

SAFE_FOOD: Dict[str, int] = {
    "cooked_beef": 8, "cooked_porkchop": 8, "cooked_mutton": 6, "cooked_chicken": 6,
    "cooked_salmon": 6, "cooked_cod": 5, "bread": 5, "baked_potato": 5,
    "golden_carrot": 6, "apple": 4, "carrot": 3, "beetroot": 1,
    "melon_slice": 2, "sweet_berries": 2, "glow_berries": 2, "dried_kelp": 1,
}
FOOD_POINTS: Dict[str, int] = {
    **SAFE_FOOD,
    "cooked_rabbit": 5, "cookie": 2, "pumpkin_pie": 8,
    "mushroom_stew": 6, "rabbit_stew": 10, "beetroot_soup": 6,
    "beef": 3, "porkchop": 3, "rabbit": 3, "mutton": 2, "cod": 2, "salmon": 2, "potato": 1,
}
HAZARDS: Set[str] = {
    "lava", "water", "fire", "soul_fire", "magma_block",
    "cactus", "campfire", "soul_campfire", "powder_snow",
}
SCOUT_MIN_DISTANCE = 16
SCOUT_MAX_DISTANCE = 256
SCOUT_DEFAULT_DISTANCE = 165

FOOD_HEADROOM_BLOCKS = frozenset({"dirt", "coarse_dirt", "rooted_dirt"})


def food_headroom_ready(status: Mapping[str, Any]) -> bool:
    """The state window for clearing one adjacent head block during food recovery."""
    health, food, game_time = (status.get(key) for key in ("health", "food", "time_of_day"))
    return (type(health) in (int, float) and 0 < health <= 4
            and type(food) in (int, float) and 12 <= food < 18
            and type(game_time) in (int, float) and 0 <= game_time < 11000
            and status.get("is_day") is True and status.get("thundering") is False)


AIR: Set[str] = {"air", "cave_air", "void_air"}
STONE_TIERS: Tuple[str, ...] = ("stone", "iron", "diamond", "netherite")
TOOL_KINDS: Tuple[str, ...] = ("pickaxe", "axe", "sword")


RANGED_HOSTILES = frozenset({"skeleton", "stray", "bogged", "pillager"})


def hostile_threat_radius(name: str | None, base_radius: float) -> float:
    """Extend the local danger radius for mobs with ranged attacks."""
    return max(base_radius, 16) if isinstance(name, str) and name in RANGED_HOSTILES else base_radius


class BotStatusDict(TypedDict, total=False):
    pos: List[float]
    health: float
    food: float
    alive: bool
    ready: bool
    busy: bool
    dimension: str
    gamemode: str
    is_day: bool
    thundering: bool
    time_of_day: int
    on_ground: bool
    is_sleeping: bool
    in_lava: bool
    on_fire: bool
    submerged: bool
    in_water: bool
    in_vehicle: bool
    nearest_hostile: Dict[str, Any]


class BlockRowDict(TypedDict, total=False):
    name: str
    pos: List[float]
    properties: Dict[str, Any]
    can_see: bool
    can_dig: bool


class EntityRowDict(TypedDict, total=False):
    id: int
    name: str
    type: str
    pos: List[float]
    dist: float
    item: str
    count: int


class ObservationDict(TypedDict, total=False):
    observed_at: str
    fresh: bool
    stale: bool
    status: BotStatusDict
    inventory: List[Dict[str, Any]]
    counts: Dict[str, int]
    blocks: List[BlockRowDict]
    terrain: List[BlockRowDict]
    entities: List[EntityRowDict]
    daily_evidence: Dict[str, Any]
    shelter_candidates: List[Dict[str, Any]]


class HarnessContextDict(TypedDict, total=False):
    spawn: List[float]
    failures: Dict[str, int]
    blocked_until: Dict[str, float]
    blocked_targets: List[str]
    explore_index: int
    death_count: int
    retreat: Dict[str, Any]
    competition: Dict[str, Any]


def finite_number(value: Any) -> bool:
    """Return True iff value is a finite int or float (excluding bool)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def parse_pos(value: Any, *, integer: bool = False) -> Optional[List[float]]:
    """Validate and copy a 3D coordinate list/tuple, or return None."""
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    if integer:
        if all(isinstance(v, int) and not isinstance(v, bool) for v in value):
            return [int(v) for v in value]
        return None
    if all(finite_number(v) for v in value):
        return list(value)
    return None


def cell_coords(point: Iterable[float]) -> Tuple[int, int, int]:
    """Convert a 3D point into integer voxel cell coordinates with epsilon guard."""
    vals = tuple(math.floor(value + 0.0001) for value in point)
    return (vals[0], vals[1], vals[2])


def block_key(pos: Iterable[float]) -> str:
    """Format a 3D position as a canonical 'x,y,z' integer block cell key."""
    return ",".join(str(math.floor(v)) for v in pos)


def inventory_counts(observation: Mapping[str, Any]) -> Dict[str, int]:
    """Extract positive integer item counts from observation['counts'] or observation['inventory']."""
    if not isinstance(observation, Mapping):
        return {}
    supplied = observation.get("counts")
    if isinstance(supplied, dict):
        return {
            name: int(count)
            for name, count in supplied.items()
            if isinstance(name, str) and finite_number(count) and count > 0
        }
    counts: Dict[str, int] = {}
    for item in observation.get("inventory") or []:
        if (isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and finite_number(item.get("count"))
                and item["count"] > 0):
            counts[item["name"]] = counts.get(item["name"], 0) + int(item["count"])
    return counts


def extract_blocks(observation: Mapping[str, Any], *, include_terrain: bool = False) -> List[Dict[str, Any]]:
    """Return validated block dictionaries from observation['blocks'] (and optionally 'terrain')."""
    if not isinstance(observation, Mapping):
        return []
    raw_blocks = observation.get("blocks")
    rows: List[Dict[str, Any]] = [b for b in raw_blocks if isinstance(b, dict)] if isinstance(raw_blocks, list) else []
    if include_terrain:
        raw_terrain = observation.get("terrain")
        if isinstance(raw_terrain, list):
            rows.extend(b for b in raw_terrain if isinstance(b, dict))
    return [
        b for b in rows
        if isinstance(b.get("name"), str) and parse_pos(b.get("pos")) is not None
    ]


def extract_hostiles(observation: Mapping[str, Any], origin: Any) -> List[Tuple[float, List[float]]]:
    """Return distance-sorted (dist, pos) pairs for hostile entities in observation."""
    origin_list = parse_pos(origin)
    if not isinstance(observation, Mapping) or origin_list is None:
        return []
    found: List[Tuple[float, List[float]]] = []
    for entity in observation.get("entities") or []:
        if not isinstance(entity, dict) or entity.get("type") != "hostile":
            continue
        pos = parse_pos(entity.get("pos"))
        if pos is not None:
            found.append((math.dist(pos, origin_list), pos))
    return sorted(found, key=lambda value: (value[0], value[1]))


def blocked_targets(context: Mapping[str, Any]) -> Set[str]:
    """Normalize active blocked target strings from harness context into cell keys."""
    if not isinstance(context, Mapping):
        return set()
    blocked: Set[str] = set()
    for key in context.get("blocked_targets") or []:
        if isinstance(key, str):
            try:
                pos = [float(v) for v in key.split(",")]
                blocked.add(block_key(pos) if parse_pos(pos) else key)
            except ValueError:
                blocked.add(key)
    return blocked


def has_tool_tier(counts: Mapping[str, int], kind: str, *, wooden: bool = False) -> bool:
    """Return True if counts includes a tool of the given kind at stone+ (or wooden+) tier."""
    tiers = (("wooden",) + STONE_TIERS) if wooden else STONE_TIERS
    return any(counts.get(f"{tier}_{kind}", 0) > 0 for tier in tiers)
