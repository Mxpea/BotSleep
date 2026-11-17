"""Pure declarative structure blueprints and world-state diff inspection.

This module performs zero network, disk, or clock I/O. It models standard
structures in local relative coordinates, rotates them cleanly across all four
cardinal orientations (north, south, east, west), and inspects live block
readers to report completion, missing cells, and blocking obstacles.
"""

from dataclasses import dataclass, field
import math
from typing import Any, Callable, Collection, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from core.domain import AIR, HAZARDS, parse_pos


CARDINAL_ORIENTATIONS: Tuple[str, ...] = ("south", "west", "north", "east")
ORIENTATION_ALIASES: Dict[str, str] = {
    "south": "south",
    "west": "west",
    "north": "north",
    "east": "east",
    "x": "south",
    "z": "east",
}
_FACING_STEPS: Dict[str, int] = {"south": 0, "west": 1, "north": 2, "east": 3}

DEFAULT_REPLACEABLE: FrozenSet[str] = frozenset(AIR | {"short_grass", "tall_grass", "snow", "leaf_litter"})
SCAFFOLD_BLOCKS: FrozenSet[str] = frozenset({"cobblestone", "stone", "dirt", "netherrack", "cobbled_deepslate"})
WOODEN_DOOR_BLOCKS: FrozenSet[str] = frozenset({
    "oak_door",
    "spruce_door",
    "birch_door",
    "jungle_door",
    "acacia_door",
    "dark_oak_door",
    "mangrove_door",
    "cherry_door",
    "bamboo_door",
    "crimson_door",
    "warped_door",
})
SHELTER_PLANK_BLOCKS: FrozenSet[str] = frozenset({
    "oak_planks",
    "spruce_planks",
    "birch_planks",
    "jungle_planks",
    "acacia_planks",
    "dark_oak_planks",
    "mangrove_planks",
    "cherry_planks",
    "bamboo_planks",
})
SHELTER_BUILDING_BLOCKS: FrozenSet[str] = frozenset(SHELTER_PLANK_BLOCKS | {"cobblestone", "stone", "dirt"})
SHELTER_GROUND_BLOCKS: FrozenSet[str] = frozenset({
    "grass_block",
    "dirt",
    "coarse_dirt",
    "rooted_dirt",
    "stone",
    "andesite",
    "granite",
    "diorite",
    "deepslate",
    "cobbled_deepslate",
    "tuff",
    "moss_block",
    "sandstone",
    "red_sandstone",
    "cobblestone",
})
SHELTER_PROTECTED_BLOCKS: FrozenSet[str] = frozenset({
    "chest",
    "trapped_chest",
    "barrel",
    "furnace",
    "blast_furnace",
    "smoker",
    "crafting_table",
    "enchanting_table",
    "anvil",
    "spawner",
})
CAVE_DIGGABLE_BLOCKS: FrozenSet[str] = frozenset({
    "stone",
    "cobblestone",
    "deepslate",
    "cobbled_deepslate",
    "tuff",
    "andesite",
    "diorite",
    "granite",
    "calcite",
    "smooth_basalt",
    "dripstone_block",
    "pointed_dripstone",
    "gravel",
    "dirt",
    "coarse_dirt",
    "grass_block",
    "sand",
    "red_sand",
    "sandstone",
    "red_sandstone",
    "netherrack",
    "basalt",
    "blackstone",
    "sculk",
    "moss_block",
    "oak_leaves",
    "birch_leaves",
    "spruce_leaves",
})
SHELTER_CLEARABLE_BLOCKS: FrozenSet[str] = frozenset(
    CAVE_DIGGABLE_BLOCKS
    | {
        "jungle_leaves",
        "acacia_leaves",
        "dark_oak_leaves",
        "mangrove_leaves",
        "cherry_leaves",
        "azalea_leaves",
        "flowering_azalea_leaves",
        "snow_block",
        "podzol",
        "mycelium",
        "mud",
        "clay",
        "oak_log",
        "birch_log",
        "spruce_log",
        "jungle_log",
        "acacia_log",
        "dark_oak_log",
        "jungle_log",
        "vine",
        "bamboo",
        "bamboo_sapling",
        "sugar_cane",
        "fern",
        "large_fern",
        "dead_bush",
        "dandelion",
        "poppy",
        "cornflower",
        "oxeye_daisy",
        "azure_bluet",
        "pink_petals",
        "wildflowers",
        "short_dry_grass",
        "tall_dry_grass",
        "brown_mushroom",
        "red_mushroom",
    }
)
MINE_ORE_BLOCKS: FrozenSet[str] = frozenset({
    "coal_ore",
    "deepslate_coal_ore",
    "iron_ore",
    "deepslate_iron_ore",
    "copper_ore",
    "deepslate_copper_ore",
    "gold_ore",
    "deepslate_gold_ore",
    "redstone_ore",
    "deepslate_redstone_ore",
    "lapis_ore",
    "deepslate_lapis_ore",
    "diamond_ore",
    "deepslate_diamond_ore",
    "emerald_ore",
    "deepslate_emerald_ore",
    "raw_iron_block",
    "raw_copper_block",
})
MINE_STONE_BLOCKS: FrozenSet[str] = frozenset({
    "stone",
    "cobblestone",
    "mossy_cobblestone",
    "andesite",
    "diorite",
    "granite",
    "deepslate",
    "cobbled_deepslate",
    "tuff",
    "calcite",
    "smooth_basalt",
    "basalt",
    "blackstone",
    "dripstone_block",
    "pointed_dripstone",
    "sandstone",
    "red_sandstone",
    "terracotta",
})
MINE_DIGGABLE_BLOCKS: FrozenSet[str] = frozenset(
    SHELTER_CLEARABLE_BLOCKS | MINE_ORE_BLOCKS | MINE_STONE_BLOCKS
)
MINE_FLOOR_BLOCKS: FrozenSet[str] = frozenset(
    SHELTER_GROUND_BLOCKS | SHELTER_BUILDING_BLOCKS | MINE_STONE_BLOCKS | MINE_ORE_BLOCKS | {"bedrock"}
)
MINE_HAZARD_BLOCKS: FrozenSet[str] = frozenset(
    set(HAZARDS)
    | {
        "water",
        "flowing_water",
        "lava",
        "flowing_lava",
        "magma_block",
        "powder_snow",
        "cactus",
        "sweet_berry_bush",
        "spawner",
        "trial_spawner",
    }
)


@dataclass(frozen=True)
class StructureSiteSpec:
    """Declarative plot-qualification and bridging rules for finding a build site."""

    seed_blocks: Tuple[str, ...] = ()
    platform_rel: Tuple[Tuple[int, int, int], ...] = ()
    resource_pool_rel: Tuple[Tuple[int, int, int], ...] = ()
    support_floor_rel: Tuple[Tuple[int, int, int], ...] = ()
    resource_block: str = ""
    min_resource_blocks: int = 0
    max_bridge_cells: int = 10
    max_overhead_digs: int = 28
    approach_anchor: str = "player_start"


@dataclass(frozen=True)
class CellRule:
    """Expectation for one block cell inside a StructureSpec."""

    rel_pos: Tuple[int, int, int]
    expected_blocks: FrozenSet[str]
    stage: str
    priority: int = 0
    properties: Optional[Dict[str, Any]] = None
    replaceable: FrozenSet[str] = DEFAULT_REPLACEABLE
    cleanup_after_build: bool = False
    label: str = ""


@dataclass(frozen=True)
class AnchorPoint:
    """Named stance or waypoint relative to the structure origin."""

    name: str
    rel_pos: Tuple[float, float, float]
    tolerance: float = 0.8
    exact_cell: bool = False
    description: str = ""


@dataclass(frozen=True)
class StructureSpec:
    """Reusable declarative blueprint with staged cells and semantic anchors."""

    spec_id: str
    stages: Tuple[str, ...]
    cells: Tuple[CellRule, ...]
    anchors: Dict[str, AnchorPoint] = field(default_factory=dict)
    clearance_rel: Tuple[Tuple[int, int, int], ...] = ()
    site_spec: Optional[StructureSiteSpec] = None


def normalize_orientation(orientation: str) -> str:
    """Normalize a cardinal orientation or axis alias ('x'/'z') to 'north'|'south'|'east'|'west'."""
    if not isinstance(orientation, str):
        raise ValueError(f"Unsupported orientation: {orientation!r}")
    normalized = ORIENTATION_ALIASES.get(orientation.strip().lower())
    if normalized is None:
        raise ValueError(f"Unsupported orientation: {orientation!r}")
    return normalized


def _rotate_xz(dx: float, dz: float, orientation: str) -> Tuple[float, float]:
    """Rotate horizontal (dx, dz) clockwise from 'south' (0 deg) in XZ plane."""
    orient = normalize_orientation(orientation)
    if orient == "south":
        return dx, dz
    if orient == "west":
        return -dz, dx
    if orient == "north":
        return -dx, -dz
    return dz, -dx  # east


def transform_rel_vec(vec: Sequence[float], orientation: str = "south") -> List[int]:
    """Rotate a 3D unit/direction vector (e.g. block face) without translating by origin."""
    parsed = parse_pos(vec)
    if parsed is None:
        raise ValueError(f"Invalid 3D vector: {vec!r}")
    rx, rz = _rotate_xz(parsed[0], parsed[2], orientation)
    return [int(round(rx)), int(round(parsed[1])), int(round(rz))]


def transform_rel_pos(
    rel_pos: Sequence[float],
    origin: Sequence[int],
    orientation: str = "south",
    *,
    integer: Optional[bool] = None,
) -> List[float]:
    """Map local coordinates into world coordinates around an integer block origin.

    Integer block coordinates rotate around (0, 0) cell corners. Continuous
    floating-point stances rotate around (0.5, 0.5) — the center of the origin
    voxel — so any stance remains inside the exact rotated block cell.
    """
    parsed_origin = parse_pos(origin, integer=True)
    if parsed_origin is None:
        raise ValueError(f"Structure origin must be 3 integers, got {origin!r}")
    parsed_rel = parse_pos(rel_pos)
    if parsed_rel is None:
        raise ValueError(f"Invalid relative position: {rel_pos!r}")

    is_int = (
        integer
        if integer is not None
        else all(isinstance(v, int) and not isinstance(v, bool) for v in rel_pos)
    )
    if is_int:
        rx, rz = _rotate_xz(int(parsed_rel[0]), int(parsed_rel[2]), orientation)
        return [
            int(parsed_origin[0] + rx),
            int(parsed_origin[1] + int(parsed_rel[1])),
            int(parsed_origin[2] + rz),
        ]

    rx, rz = _rotate_xz(parsed_rel[0] - 0.5, parsed_rel[2] - 0.5, orientation)
    return [
        round(parsed_origin[0] + rx + 0.5, 4),
        round(parsed_origin[1] + parsed_rel[1], 4),
        round(parsed_origin[2] + rz + 0.5, 4),
    ]


def rotate_facing(facing: str, orientation: str = "south") -> str:
    """Rotate a cardinal blockstate facing string ('north'/'south'/'east'/'west')."""
    base = _FACING_STEPS.get(str(facing).lower())
    delta = _FACING_STEPS[normalize_orientation(orientation)]
    if base is None:
        raise ValueError(f"Unsupported facing value: {facing!r}")
    return CARDINAL_ORIENTATIONS[(base + delta) % 4]


def _safe_read_block(
    read_block: Callable[[List[int]], Dict[str, Any]],
    pos: Sequence[int],
) -> Dict[str, Any]:
    """Read a block dict, downgrading `not_loaded` errors into sentinel blocks."""
    try:
        raw = read_block([int(pos[0]), int(pos[1]), int(pos[2])])
    except Exception as exc:
        if getattr(exc, "code", None) == "not_loaded":
            return {"name": "not_loaded", "pos": [int(pos[0]), int(pos[1]), int(pos[2])]}
        raise
    blk = raw.get("result") if isinstance(raw, dict) and "result" in raw else raw
    return blk if isinstance(blk, dict) else {"name": "unknown"}


def _block_matches(block: Dict[str, Any], rule: CellRule, orientation: str) -> bool:
    name = block.get("name")
    if name not in rule.expected_blocks:
        return False
    if not rule.properties:
        return True
    props = block.get("properties")
    if not isinstance(props, dict):
        return False
    for key, expected in rule.properties.items():
        actual = props.get(key)
        target = rotate_facing(expected, orientation) if key == "facing" and isinstance(expected, str) else expected
        if actual != target:
            return False
    return True


def inspect_structure(
    spec: StructureSpec,
    origin: Sequence[int],
    read_block: Callable[[List[int]], Dict[str, Any]],
    *,
    orientation: str = "south",
    stages: Optional[Collection[str]] = None,
    stage_expected_overrides: Optional[Dict[str, FrozenSet[str]]] = None,
) -> Dict[str, Any]:
    """Scan structure cells via read_block and return a structured diff report."""
    norm_orientation = normalize_orientation(orientation)
    active_stages = set(stages) if stages is not None else set(spec.stages)
    overrides = stage_expected_overrides or {}

    selected_cells = sorted(
        (cell for cell in spec.cells if cell.stage in active_stages),
        key=lambda c: (spec.stages.index(c.stage) if c.stage in spec.stages else 999, c.priority, c.rel_pos),
    )

    stage_totals: Dict[str, int] = {s: 0 for s in spec.stages if s in active_stages}
    stage_present: Dict[str, int] = {s: 0 for s in spec.stages if s in active_stages}
    missing: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    hazards: List[Dict[str, Any]] = []
    cell_states: List[Dict[str, Any]] = []

    for rule in selected_cells:
        world_pos = transform_rel_pos(rule.rel_pos, origin, norm_orientation, integer=True)
        raw = read_block(world_pos)
        block = raw.get("result") if isinstance(raw, dict) and "result" in raw else raw
        block = block if isinstance(block, dict) else {}
        block_name = str(block.get("name") or "unknown")

        effective_rule = (
            CellRule(
                rel_pos=rule.rel_pos,
                expected_blocks=overrides[rule.stage],
                stage=rule.stage,
                priority=rule.priority,
                properties=rule.properties,
                replaceable=rule.replaceable,
                cleanup_after_build=rule.cleanup_after_build,
                label=rule.label,
            )
            if rule.stage in overrides
            else rule
        )

        stage_totals[rule.stage] = stage_totals.get(rule.stage, 0) + 1
        satisfied = _block_matches(block, effective_rule, norm_orientation)
        if satisfied:
            stage_present[rule.stage] = stage_present.get(rule.stage, 0) + 1
        else:
            entry = {
                "stage": rule.stage,
                "priority": rule.priority,
                "label": rule.label,
                "rel_pos": list(rule.rel_pos),
                "world_pos": world_pos,
                "current": block_name,
                "expected": sorted(effective_rule.expected_blocks),
            }
            missing.append(entry)
            if block_name in HAZARDS:
                hazards.append(entry)
            elif block_name not in effective_rule.replaceable:
                conflicts.append(entry)

        cell_states.append({
            "stage": rule.stage,
            "label": rule.label,
            "rel_pos": list(rule.rel_pos),
            "world_pos": world_pos,
            "current": block_name,
            "satisfied": satisfied,
        })

    stage_status = {
        stage: {
            "present": stage_present.get(stage, 0),
            "total": stage_totals.get(stage, 0),
            "complete": stage_present.get(stage, 0) == stage_totals.get(stage, 0),
        }
        for stage in spec.stages
        if stage in active_stages
    }

    anchors_world = {
        name: transform_rel_pos(anchor.rel_pos, origin, norm_orientation, integer=False)
        for name, anchor in spec.anchors.items()
    }

    total_count = len(selected_cells)
    present_count = sum(stage_present.values())
    return {
        "spec_id": spec.spec_id,
        "origin": list(parse_pos(origin, integer=True) or origin),
        "orientation": norm_orientation,
        "complete": present_count == total_count and total_count > 0,
        "present_count": present_count,
        "total_count": total_count,
        "stage_status": stage_status,
        "missing": missing,
        "conflicts": conflicts,
        "hazards": hazards,
        "anchors_world": anchors_world,
        "cells": cell_states,
    }


def nether_portal_spec() -> StructureSpec:
    """Return the canonical 4x5 speedrun Nether Portal StructureSpec.

    Local frame (orientation='south', matching axis='x'):
    - origin [0, 0, 0]: left-bottom corner at lava surface / ground Y.
    - +x (0..3): portal width left -> right.
    - +y (-1..3): vertical (base trench dy=-1, opening dy=0..2, top beam dy=3).
    - -z (-1..-2): back/shore side with 7-cobblestone scaffolding & water curtain.
    - +z (+1..+2): front/lava-pool side with casting & ignition stances.
    """
    obs = frozenset({"obsidian"})
    portal = frozenset({"nether_portal"})

    cells = (
        # Stage 1: Base & bottom corners (4 obsidian)
        CellRule((1, -1, 0), obs, "base", priority=1, label="base_left"),
        CellRule((2, -1, 0), obs, "base", priority=2, label="base_right"),
        CellRule((0, 0, 0), obs, "base", priority=3, label="corner_left"),
        CellRule((3, 0, 0), obs, "base", priority=4, label="corner_right"),
        # Stage 2: 7-block directional scaffolding (back & right pillar)
        CellRule((1, 1, -2), SCAFFOLD_BLOCKS, "scaffold", priority=1, label="scaffold_backboard"),
        CellRule((0, 1, -1), SCAFFOLD_BLOCKS, "scaffold", priority=2, label="scaffold_left_wall_far"),
        CellRule((1, 1, -1), SCAFFOLD_BLOCKS, "scaffold", priority=3, label="scaffold_left_wall_near"),
        CellRule((3, 1, -1), SCAFFOLD_BLOCKS, "scaffold", priority=4, label="scaffold_pillar_1"),
        CellRule((3, 2, -1), SCAFFOLD_BLOCKS, "scaffold", priority=5, label="scaffold_pillar_2"),
        CellRule((3, 3, -1), SCAFFOLD_BLOCKS, "scaffold", priority=6, label="scaffold_pillar_3"),
        CellRule((3, 3, 0), SCAFFOLD_BLOCKS, "scaffold", priority=7, label="scaffold_arm_forward"),
        # Stage 3: Remaining 6 obsidian blocks in exact speedrun cast order
        CellRule((2, 3, 0), obs, "frame", priority=1, label="top_beam_right"),
        CellRule((1, 3, 0), obs, "frame", priority=2, label="top_beam_left"),
        CellRule((3, 1, 0), obs, "frame", priority=3, label="right_pillar_lower"),
        CellRule((3, 2, 0), obs, "frame", priority=4, label="right_pillar_upper"),
        CellRule((0, 1, 0), obs, "frame", priority=5, label="left_pillar_lower"),
        CellRule((0, 2, 0), obs, "frame", priority=6, label="left_pillar_upper"),
        # Stage 4: 6 interior portal blocks
        CellRule((1, 0, 0), portal, "interior", priority=1, label="interior_1_0"),
        CellRule((2, 0, 0), portal, "interior", priority=2, label="interior_2_0"),
        CellRule((1, 1, 0), portal, "interior", priority=3, label="interior_1_1"),
        CellRule((2, 1, 0), portal, "interior", priority=4, label="interior_2_1"),
        CellRule((1, 2, 0), portal, "interior", priority=5, label="interior_1_2"),
        CellRule((2, 2, 0), portal, "interior", priority=6, label="interior_2_2"),
    )

    anchors = {
        "player_start": AnchorPoint("player_start", (2.45, 1.0, -1.5), tolerance=1.2, description="Shore start stance behind pool edge"),
        "shore_west": AnchorPoint("shore_west", (0.55, 1.0, -1.5), tolerance=0.7, description="Shore west stance for Y=64 doorway water placement"),
        "cast_center": AnchorPoint("cast_center", (1.55, 2.0, 1.25), tolerance=0.55, description="Elevated center cast stance on temp stand"),
        "cast_right": AnchorPoint("cast_right", (1.65, 2.0, 1.25), tolerance=0.55, description="Elevated right cast stance on temp stand"),
        "cast_left": AnchorPoint("cast_left", (1.45, 2.0, 1.25), tolerance=0.55, description="Elevated left cast stance on temp stand"),
        "shore_approach_1": AnchorPoint("shore_approach_1", (2.5, 1.0, 1.5), tolerance=0.55, description="Front-right dam staging stance"),
        "shore_approach_2": AnchorPoint("shore_approach_2", (2.5, 1.0, -0.5), tolerance=0.55, description="Pass-through behind portal opening"),
        "shore_scoop": AnchorPoint("shore_scoop", (2.45, 1.0, -1.5), tolerance=1.0, description="Behind-scaffold water scoop stance"),
        "player_end": AnchorPoint("player_end", (1.6, 1.0, 1.25), tolerance=0.8, description="Front safe ignition/exit stance"),
    }

    clearance_cells: List[Tuple[int, int, int]] = []
    seen_clearance = set()
    for rel in (
        [(dx, dy, 0) for dy in (1, 2, 3) for dx in range(4)]
        + [(dx, dy, -1) for dy in (1, 2, 3) for dx in range(4)]
        + [(dx, dy, -2) for dy in (1, 2, 3) for dx in range(4)]
        + [(dx, dy, 1) for dy in (1, 2, 3, 4) for dx in (1, 2)]
        + [(dx, dy, 1) for dy in (1, 2, 3) for dx in (0, 3)]
        + [(2, 4, -1), (1, 4, 0), (2, 4, 0)]
    ):
        if rel not in seen_clearance:
            seen_clearance.add(rel)
            clearance_cells.append(rel)

    site_spec = StructureSiteSpec(
        seed_blocks=("lava",),
        platform_rel=tuple((dx, 0, -1) for dx in range(4)) + tuple((dx, 0, -2) for dx in range(3)),
        resource_pool_rel=tuple(
            (dx, 0, dz)
            for dz in (0, 1, 2, 3)
            for dx in range(-1, 5)
            if not (dz == 0 and dx in (0, 3))
        ),
        support_floor_rel=((1, -2, 0), (2, -2, 0)),
        resource_block="lava",
        max_bridge_cells=7,
        max_overhead_digs=28,
        approach_anchor="player_start",
    )

    return StructureSpec(
        spec_id="nether_portal_4x5",
        stages=("base", "scaffold", "frame", "interior"),
        cells=cells,
        anchors=anchors,
        clearance_rel=tuple(clearance_cells),
        site_spec=site_spec,
    )


def _block_name(raw: Any) -> str:
    block = raw.get("result") if isinstance(raw, dict) and "result" in raw else raw
    if not isinstance(block, dict):
        return "unknown"
    return str(block.get("name") or "unknown")


def _block_props(raw: Any) -> Dict[str, Any]:
    block = raw.get("result") if isinstance(raw, dict) and "result" in raw else raw
    if not isinstance(block, dict):
        return {}
    props = block.get("properties")
    return props if isinstance(props, dict) else {}


def _plan_bridge_order(
    platform_rel: Sequence[Tuple[int, int, int]],
    solid_set: Collection[Tuple[int, int, int]],
    bridge_set: Collection[Tuple[int, int, int]],
    read_rel: Callable[[Tuple[int, int, int]], Dict[str, Any]],
    non_solid: Collection[str],
) -> Optional[Tuple[List[Tuple[int, int, int]], Optional[Tuple[int, int, int]]]]:
    """Order bridgeable platform cells by BFS from adjacent walkable solid blocks."""
    if len(solid_set) < 2:
        return None
    if not bridge_set:
        preferred = (2, 0, -2) if (2, 0, -2) in solid_set else (next(iter(solid_set), None))
        return [], preferred

    reached = set(solid_set)
    anchors: List[Tuple[int, int, int]] = list(solid_set)
    bridge_remaining = set(bridge_set)
    # Check 1-block external solid neighbors of platform cells if needed as bridge starters.
    for bx, by, bz in tuple(bridge_remaining):
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nb = (bx + dx, by, bz + dz)
            if nb in reached or nb in bridge_remaining:
                continue
            nb_name = _block_name(read_rel(nb))
            if nb_name in non_solid or nb_name in {"lava", "flowing_lava"}:
                continue
            head1 = _block_name(read_rel((nb[0], nb[1] + 1, nb[2])))
            if head1 in {"lava", "flowing_lava", "not_loaded", "unknown"}:
                continue
            reached.add(nb)
            anchors.append(nb)

    if not reached:
        return None

    ordered: List[Tuple[int, int, int]] = []
    progress = True
    while bridge_remaining and progress:
        progress = False
        for cell in sorted(bridge_remaining, key=lambda c: (c[2], c[0], c[1])):
            cx, cy, cz = cell
            if any((cx + dx, cy, cz + dz) in reached for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                ordered.append(cell)
                reached.add(cell)
                bridge_remaining.remove(cell)
                progress = True
    if bridge_remaining:
        return None

    preferred = (2, 0, -2) if (2, 0, -2) in solid_set else anchors[0]
    return ordered, preferred


def evaluate_structure_site(
    spec: StructureSpec,
    origin: Sequence[int],
    orientation: str,
    read_block: Callable[[List[int]], Dict[str, Any]],
    *,
    bot_pos: Optional[Sequence[float]] = None,
    diggable_blocks: FrozenSet[str] = CAVE_DIGGABLE_BLOCKS,
    inventory_counts: Optional[Dict[str, int]] = None,
    allow_overhead_dig: bool = True,
    allow_bridging: bool = True,
) -> Optional[Dict[str, Any]]:
    """Evaluate whether a candidate (origin, orientation) satisfies spec.site_spec."""
    site_spec = spec.site_spec
    if site_spec is None:
        return None

    norm_orient = normalize_orientation(orientation)
    int_origin = [int(v) for v in origin]
    non_solid = set(DEFAULT_REPLACEABLE) | set(HAZARDS) | {"not_loaded", "unknown"}

    def read_rel(rel_pos: Tuple[int, int, int]) -> Dict[str, Any]:
        return read_block(transform_rel_pos(rel_pos, int_origin, norm_orient, integer=True))

    # 1. Fast reject on required under-pool floor support & survival floor hazards.
    for sup_rel in site_spec.support_floor_rel:
        sup_name = _block_name(read_rel(sup_rel))
        if sup_name in non_solid:
            return None

    floor_hazards = {"magma_block", "cactus", "sweet_berry_bush", "campfire", "soul_campfire", "powder_snow"}
    for hdx in range(-1, 5):
        for hdz in (-2, -1, 0, 1):
            for hdy in (0, 1):
                if _block_name(read_rel((hdx, hdy, hdz))) in floor_hazards:
                    return None
    # Reject exposed waist/head-height lava directly adjacent to the working shore perimeter.
    for hdx, hdz in (
        (-1, -1), (-1, -2), (4, -1), (4, -2),
        (0, -3), (1, -3), (2, -3), (3, -3),
    ):
        for hdy in (1, 2):
            if _block_name(read_rel((hdx, hdy, hdz))) in {"lava", "flowing_lava"}:
                return None

    # 2. Evaluate working shore / platform cells and detect bridgeable gaps.
    solid_platform: List[Tuple[int, int, int]] = []
    bridge_raw: List[Tuple[int, int, int]] = []
    bridgeable_set = set(DEFAULT_REPLACEABLE) | {"lava", "flowing_lava", "water", "flowing_water"}
    for plat_rel in site_spec.platform_rel:
        pname = _block_name(read_rel(plat_rel))
        if pname in {"not_loaded", "unknown"}:
            return None
        if pname not in non_solid:
            solid_platform.append(plat_rel)
        elif allow_bridging and pname in bridgeable_set:
            bridge_raw.append(plat_rel)
        else:
            return None

    if len(bridge_raw) > site_spec.max_bridge_cells:
        return None
    bridge_plan = _plan_bridge_order(site_spec.platform_rel, solid_platform, bridge_raw, read_rel, non_solid)
    if bridge_plan is None:
        return None
    ordered_bridge, anchor_rel = bridge_plan

    # 3. Validate structure frame & base cells (non-obsidian must be clearable/lava/air).
    already_obsidian = 0
    frame_non_obsidian = 0
    dry_base_non_obsidian = 0
    deep_base_lava = 0
    for rule in spec.cells:
        if rule.stage not in {"base", "frame"}:
            continue
        raw = read_rel(rule.rel_pos)
        bname = _block_name(raw)
        if bname in {"not_loaded", "unknown"}:
            return None
        if bname == "obsidian":
            already_obsidian += 1
            continue
        if bname in {"lava", "flowing_lava"} or bname in AIR or bname in diggable_blocks:
            if rule.stage == "frame":
                frame_non_obsidian += 1
            elif bname not in {"lava", "flowing_lava"}:
                dry_base_non_obsidian += 1
            elif rule.rel_pos in ((1, -1, 0), (2, -1, 0)):
                deep_base_lava += 1
            continue
        return None

    # 4. Validate overhead clearance (allow clearable cave blocks up to max_overhead_digs).
    frame_rel_set = {c.rel_pos for c in spec.cells if c.stage in {"base", "frame"}}
    scaffold_rel_set = {c.rel_pos for c in spec.cells if c.stage == "scaffold"}
    overhead_cells: List[Tuple[int, int, int]] = []
    platform_head_digs = 0
    blocked_rel_set: set[Tuple[int, int, int]] = set()
    for cl_rel in spec.clearance_rel:
        cname = _block_name(read_rel(cl_rel))
        if cname in {"not_loaded", "unknown"}:
            return None
        if cl_rel[1] >= 1 and cname in {"lava", "flowing_lava"}:
            return None
        if (
            cl_rel[1] >= 1
            and cname in {"water", "flowing_water"}
            and (cl_rel[2] <= -1 or cl_rel[1] >= 2)
            and not (frame_non_obsidian < 10 and cl_rel == (2, 3, -1))
        ):
            return None
        if cname in AIR or cname in {"water", "flowing_water"} or cname in DEFAULT_REPLACEABLE or cname == "nether_portal":
            continue
        if cl_rel in frame_rel_set and cname == "obsidian":
            continue
        if cl_rel in scaffold_rel_set and cname in SCAFFOLD_BLOCKS:
            continue
        if allow_overhead_dig and cname in diggable_blocks:
            overhead_cells.append(cl_rel)
            blocked_rel_set.add(cl_rel)
            if cl_rel[2] <= -1 and cl_rel[1] in (1, 2):
                platform_head_digs += 1
            continue
        return None
    if len(overhead_cells) > site_spec.max_overhead_digs:
        return None
    open_plat_cells = [
        (px, py, pz)
        for (px, py, pz) in solid_platform
        if (px, 1, pz) not in blocked_rel_set and (px, 2, pz) not in blocked_rel_set
    ]
    if solid_platform and not open_plat_cells:
        return None
    # Ensure the chosen starting anchor actually has open 2-block headroom before digging,
    # preferring a connected open shoreline closest to the bot rather than an isolated pillar.
    open_plat_set = set(open_plat_cells)
    solid_plat_set = set(solid_platform)
    # Require at least one solid dry block in front of the doorway (0..2, 0, -1) so we
    # never try to bridge the entire working ledge over open lava in deep caves.
    if not any((dx, 0, -1) in solid_plat_set for dx in (0, 1, 2)):
        return None
    # Reject sites where digging an overhead block directly breaches a sealed water/lava pocket.
    hazard_liquid = {"water", "flowing_water"}
    overhead_lava_liquid = {"water", "flowing_water", "lava", "flowing_lava"}
    for ox, oy, oz in overhead_cells:
        for nx, ny, nz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
            nbr = (ox + nx, oy + ny, oz + nz)
            if nbr in blocked_rel_set:
                continue
            nname = _block_name(read_rel(nbr))
            if ny >= 0 and nbr[1] >= 1 and nname in overhead_lava_liquid:
                return None
            if nname in hazard_liquid:
                return None
    if anchor_rel is not None and anchor_rel not in open_plat_set:
        def _anchor_rank(p: Tuple[int, int, int]) -> Tuple[int, float, int]:
            adj_solid = sum(
                1
                for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1))
                if (p[0] + dx, p[1], p[2] + dz) in solid_plat_set
            )
            d = 0.0
            if bot_pos is not None:
                wp = transform_rel_pos((p[0] + 0.5, 1.0, p[2] + 0.5), int_origin, norm_orient, integer=False)
                d = ((wp[0] - float(bot_pos[0])) ** 2 + (wp[2] - float(bot_pos[2])) ** 2) ** 0.5 + abs(wp[1] - float(bot_pos[1])) * 1.5
            return (0 if adj_solid > 0 else 1, round(d, 3), abs(p[0] - 2) + abs(p[2] + 1))

        anchor_rel = min(open_plat_cells, key=_anchor_rank)

    # 5. Count pool resources (e.g. molten lava source blocks).
    resource_sources = 0
    walkway_lava_or_solid = 0
    if site_spec.resource_block:
        for res_rel in site_spec.resource_pool_rel:
            raw = read_rel(res_rel)
            rname = _block_name(raw)
            props = _block_props(raw)
            if rname == site_spec.resource_block and props.get("level") in (None, 0):
                resource_sources += 1
        for wk_rel in ((1, 0, 1), (2, 0, 1)):
            wname = _block_name(read_rel(wk_rel))
            if wname not in non_solid or wname in {"lava", "flowing_lava"}:
                walkway_lava_or_solid += 1

    counts = inventory_counts or {}
    lava_in_inv = int(counts.get("lava_bucket", 0))
    lava_needed = frame_non_obsidian + dry_base_non_obsidian
    if site_spec.resource_block == "lava":
        if lava_in_inv + resource_sources < max(lava_needed, site_spec.min_resource_blocks):
            return None
        if resource_sources < 1 and lava_in_inv < (frame_non_obsidian + dry_base_non_obsidian):
            return None

    scaffold_have = sum(int(counts.get(item, 0)) for item in SCAFFOLD_BLOCKS) if counts else 64
    if counts and scaffold_have < (len(ordered_bridge) + 9):
        return None

    anchor_spec = spec.anchors.get(site_spec.approach_anchor)
    approach_rel = anchor_spec.rel_pos if anchor_spec is not None else (2.5, 1.0, -1.5)
    approach_world = transform_rel_pos(approach_rel, int_origin, norm_orient, integer=False)
    staging_world = (
        transform_rel_pos((anchor_rel[0] + 0.5, 1.0, anchor_rel[2] + 0.5), int_origin, norm_orient, integer=False)
        if anchor_rel is not None
        else list(approach_world)
    )

    dist = 0.0
    cross_lava_penalty = 0.0
    if bot_pos is not None:
        dist = round(
            ((staging_world[0] - float(bot_pos[0])) ** 2 + (staging_world[2] - float(bot_pos[2])) ** 2) ** 0.5
            + abs(staging_world[1] - float(bot_pos[1])) * 1.5,
            3,
        )
        if dist > 2.5:
            min_y = int(round(min(float(bot_pos[1]), float(staging_world[1]))))
            for frac in (0.25, 0.5, 0.75):
                mx = int(round(float(bot_pos[0]) + (staging_world[0] - float(bot_pos[0])) * frac))
                mz = int(round(float(bot_pos[2]) + (staging_world[2] - float(bot_pos[2])) * frac))
                if any(
                    _block_name(read_block([mx, my, mz])) in {"lava", "flowing_lava"}
                    for my in (min_y - 1, min_y)
                ):
                    cross_lava_penalty = 25.0
                    break

    min_safe_lava = lava_needed + (2 if frame_non_obsidian > 0 else 0)
    low_lava_penalty = 25.0 if (site_spec.resource_block == "lava" and lava_in_inv + resource_sources < min_safe_lava) else 0.0
    elev_gap_penalty = (
        20.0
        if (bot_pos is not None and abs(staging_world[1] - float(bot_pos[1])) >= 2.5)
        else 0.0
    )
    score = (
        len(ordered_bridge) * 5.0
        + len(overhead_cells) * 2.5
        + platform_head_digs * 3.0
        + dry_base_non_obsidian * 8.0
        + deep_base_lava * 4.0
        + low_lava_penalty
        + cross_lava_penalty
        + elev_gap_penalty
        - already_obsidian * 4.0
        - min(resource_sources, 16) * 0.4
        - walkway_lava_or_solid * 0.5
        + dist * 0.8
    )
    return {
        "spec_id": spec.spec_id,
        "origin": int_origin,
        "orientation": norm_orient,
        "player_start": approach_world,
        "staging_pos": staging_world,
        "pool_lava_sources": resource_sources,
        "bridge_cells": [list(c) for c in ordered_bridge],
        "overhead_cells": [list(c) for c in overhead_cells],
        "already_obsidian": already_obsidian,
        "distance": dist,
        "score": round(score, 3),
    }


def find_structure_sites(
    spec: StructureSpec,
    bot_pos: Sequence[float],
    read_block: Callable[[List[int]], Dict[str, Any]],
    *,
    seed_positions: Optional[Sequence[Sequence[int]]] = None,
    radius: int = 16,
    orientations: Optional[Sequence[str]] = None,
    inventory_counts: Optional[Dict[str, int]] = None,
    block_cache: Optional[Dict[Tuple[int, int, int], Dict[str, Any]]] = None,
    max_results: int = 5,
) -> List[Dict[str, Any]]:
    """Find and rank candidate build sites matching a Standard StructureSpec."""
    bx, by, bz = int(round(float(bot_pos[0]))), int(round(float(bot_pos[1]))), int(round(float(bot_pos[2])))
    orient_list = tuple(normalize_orientation(o) for o in (orientations or CARDINAL_ORIENTATIONS))
    cache: Dict[Tuple[int, int, int], Dict[str, Any]] = block_cache if block_cache is not None else {}

    def cached_read(pos: List[int]) -> Dict[str, Any]:
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        if key not in cache:
            cache[key] = _safe_read_block(read_block, key)
        return cache[key]

    candidates: Dict[Tuple[int, int, int, str], Tuple[int, int, int]] = {}
    if seed_positions:
        seed_rel_anchors = (
            (1, 0, 0),
            (2, 0, 0),
            (0, 0, 0),
            (3, 0, 0),
            (1, 0, 1),
            (2, 0, 1),
            (1, 0, 2),
            (2, 0, 2),
            (0, 0, 1),
            (3, 0, 1),
        )
        for raw_seed in seed_positions:
            sx, sy, sz = int(raw_seed[0]), int(raw_seed[1]), int(raw_seed[2])
            for orient in orient_list:
                for rel_dx, rel_dy, rel_dz in seed_rel_anchors:
                    off_x, off_y, off_z = transform_rel_pos((rel_dx, rel_dy, rel_dz), (0, 0, 0), orient, integer=True)
                    ox, oy, oz = sx - off_x, sy - off_y, sz - off_z
                    for y_shift in (0, -1):
                        key = (ox, oy + y_shift, oz, orient)
                        candidates.setdefault(key, (ox, oy + y_shift, oz))
    else:
        scan_r = min(max(4, int(radius)), 8)
        for dy in (-1, -2, 0):
            y0 = by + dy
            for ox in range(bx - scan_r, bx + scan_r + 1):
                for oz in range(bz - scan_r, bz + scan_r + 1):
                    for orient in orient_list:
                        key = (ox, y0, oz, orient)
                        candidates.setdefault(key, (ox, y0, oz))

    matches: List[Dict[str, Any]] = []
    ordered_candidates = sorted(
        candidates.items(),
        key=lambda item: (item[1][0] - bx) ** 2 + (item[1][2] - bz) ** 2 + abs(item[1][1] - (by - 1)) * 4,
    )
    max_eval = 360 if seed_positions else len(ordered_candidates)
    best_dist_sq: Optional[int] = None
    for idx, ((_, _, _, orient), origin_tuple) in enumerate(ordered_candidates):
        if idx >= max_eval and matches:
            break
        cand_dist_sq = (origin_tuple[0] - bx) ** 2 + (origin_tuple[2] - bz) ** 2
        if (
            best_dist_sq is not None
            and len(matches) >= max(12, int(max_results) * 3)
            and len({s["orientation"] for s in matches}) >= min(4, len(orient_list))
            and cand_dist_sq > best_dist_sq + 196
        ):
            break
        site = evaluate_structure_site(
            spec,
            origin_tuple,
            orient,
            cached_read,
            bot_pos=bot_pos,
            inventory_counts=inventory_counts,
        )
        if site is not None:
            matches.append(site)
            if best_dist_sq is None or cand_dist_sq < best_dist_sq:
                best_dist_sq = cand_dist_sq

    matches.sort(key=lambda s: (float(s["score"]), float(s["distance"])))
    return matches[: max(1, int(max_results))]



def find_structure_site(
    spec: StructureSpec,
    bot_pos: Sequence[float],
    read_block: Callable[[List[int]], Dict[str, Any]],
    **kwargs: Any,
) -> Optional[Dict[str, Any]]:
    """Return the highest-ranked build site for a Standard StructureSpec, or None."""
    sites = find_structure_sites(spec, bot_pos, read_block, max_results=1, **kwargs)
    return sites[0] if sites else None


NETHER_PORTAL_SPEC: StructureSpec = nether_portal_spec()


def shelter_spec() -> StructureSpec:
    """Return the canonical 5x3x5 shelter StructureSpec.

    Canonical local frame (orientation='south'):
    - origin [0, 0, 0]: integer center of the 5x5 floor at interior feet level (Y).
    - walls perimeter: dx in -2..2, dz in -2..2 (|dx|==2 or |dz|==2), dy in 0..1,
      leaving doorway open at (0, 0, +2) and (0, 1, +2).
    - roof: 5x5 at dy=2, ordered outer perimeter first (corners before edges),
      then inner 3x3 back-to-front toward the doorway so every block attaches to an
      exposed neighbor face without self-trapping.
    - When rotated to orientation='north', doorway maps to (0, 0, -2) and exterior
      exit stand maps to (0.5, 0.0, -2.5), matching the legacy fixed shelter layout.
    """
    cells: List[CellRule] = []
    # Floor (25 footprint cells + doorway exterior exit step)
    floor_idx = 0
    for dz in range(-2, 3):
        for dx in range(-2, 3):
            floor_idx += 1
            cells.append(
                CellRule(
                    (dx, -1, dz),
                    SHELTER_GROUND_BLOCKS | SHELTER_BUILDING_BLOCKS,
                    "floor",
                    priority=floor_idx,
                    label=f"floor_{dx}_{dz}",
                )
            )
    cells.append(
        CellRule(
            (0, -1, 3),
            SHELTER_GROUND_BLOCKS | SHELTER_BUILDING_BLOCKS,
            "floor",
            priority=floor_idx + 1,
            label="exit_floor",
        )
    )

    # Lower walls (dy=0) and upper walls (dy=1): corners before edges so interior builder keeps sightlines.
    wall_xz = [
        (dx, dz)
        for dx in range(-2, 3)
        for dz in range(-2, 3)
        if (abs(dx) == 2 or abs(dz) == 2) and (dx, dz) != (0, 2)
    ]
    wall_xz.sort(key=lambda p: (not (abs(p[0]) == 2 and abs(p[1]) == 2), -p[0], -p[1]))
    for stage_name, dy in (("lower_walls", 0), ("upper_walls", 1)):
        for idx, (dx, dz) in enumerate(wall_xz, 1):
            cells.append(
                CellRule(
                    (dx, dy, dz),
                    SHELTER_BUILDING_BLOCKS,
                    stage_name,
                    priority=idx,
                    label=f"{stage_name}_{dx}_{dz}",
                )
            )

    # Roof (dy=2): outer ring corners -> outer ring edges -> inner 3x3 back-to-front toward doorway (+z).
    roof_xz = [(dx, dz) for dx in range(-2, 3) for dz in range(-2, 3)]
    roof_xz.sort(
        key=lambda p: (
            0 if (abs(p[0]) == 2 or abs(p[1]) == 2) else 1,
            p[1] if (abs(p[0]) < 2 and abs(p[1]) < 2) else 0,
            not (abs(p[0]) == 2 and abs(p[1]) == 2),
            0 if abs(p[0]) == 1 else 1,
            p[0],
            p[1],
        )
    )
    for idx, (dx, dz) in enumerate(roof_xz, 1):
        cells.append(
            CellRule(
                (dx, 2, dz),
                SHELTER_BUILDING_BLOCKS,
                "roof",
                priority=idx,
                label=f"roof_{dx}_{dz}",
            )
        )

    # Door (two halves at canonical south wall (0, 0..1, +2))
    cells.append(
        CellRule(
            (0, 0, 2),
            WOODEN_DOOR_BLOCKS,
            "door",
            priority=1,
            properties={"half": "lower"},
            label="door_lower",
        )
    )
    cells.append(
        CellRule(
            (0, 1, 2),
            WOODEN_DOOR_BLOCKS,
            "door",
            priority=2,
            properties={"half": "upper"},
            label="door_upper",
        )
    )

    anchors = {
        "interior_center": AnchorPoint("interior_center", (0.5, 0.0, 0.5), tolerance=0.6, description="Center of interior 3x3 floor"),
        "doorway_inside": AnchorPoint("doorway_inside", (0.5, 0.0, 1.5), tolerance=0.6, description="Inside doorway cell"),
        "doorway_outside": AnchorPoint("doorway_outside", (0.5, 0.0, 3.5), tolerance=0.75, description="Outside exit cell facing doorway"),
    }

    clearance_cells: List[Tuple[int, int, int]] = []
    for dy in (0, 1, 2):
        for dx in range(-2, 3):
            for dz in range(-2, 3):
                clearance_cells.append((dx, dy, dz))
    clearance_cells.extend([(0, 0, 3), (0, 1, 3)])

    return StructureSpec(
        spec_id="shelter_5x5x3",
        stages=("floor", "lower_walls", "upper_walls", "roof", "door"),
        cells=tuple(cells),
        anchors=anchors,
        clearance_rel=tuple(clearance_cells),
        site_spec=None,
    )


SHELTER_5X5_SPEC: StructureSpec = shelter_spec()


def evaluate_shelter_site(
    origin: Sequence[int],
    orientation: str,
    read_block: Callable[[List[int]], Dict[str, Any]],
    *,
    bot_pos: Optional[Sequence[float]] = None,
    building_blocks: FrozenSet[str] = SHELTER_BUILDING_BLOCKS,
    already_built_blocks: FrozenSet[str] = frozenset(SHELTER_PLANK_BLOCKS | {"cobblestone"}),
    ground_blocks: FrozenSet[str] = SHELTER_GROUND_BLOCKS,
    clearable_blocks: FrozenSet[str] = SHELTER_CLEARABLE_BLOCKS,
    allow_clearing: bool = True,
    allow_floor_patch: bool = True,
    max_clear_cells: int = 24,
    max_floor_patches: int = 6,
) -> Optional[Dict[str, Any]]:
    """Evaluate whether a natural terrain plot can host SHELTER_5X5_SPEC."""
    norm_orient = normalize_orientation(orientation)
    int_origin = parse_pos(origin, integer=True)
    if int_origin is None:
        return None

    solid_ground = set(ground_blocks) | set(building_blocks)
    replaceable_or_air = set(DEFAULT_REPLACEABLE)
    disallowed_hazards = set(HAZARDS) | {
        "water",
        "flowing_water",
        "lava",
        "flowing_lava",
        "magma_block",
        "cactus",
        "sweet_berry_bush",
        "campfire",
        "soul_campfire",
        "powder_snow",
    }
    protected = set(SHELTER_PROTECTED_BLOCKS) | set(WOODEN_DOOR_BLOCKS)

    cache: Dict[Tuple[int, int, int], Dict[str, Any]] = {}

    def read_rel_block(rel: Tuple[int, int, int]) -> Tuple[List[int], Dict[str, Any]]:
        wpos = transform_rel_pos(rel, int_origin, norm_orient, integer=True)
        key = (wpos[0], wpos[1], wpos[2])
        if key not in cache:
            cache[key] = _safe_read_block(read_block, wpos)
        return wpos, cache[key]

    def is_safe_blk(blk: Dict[str, Any]) -> bool:
        props = blk.get("properties") if isinstance(blk.get("properties"), dict) else {}
        if props.get("waterlogged") in (True, "true"):
            return False
        name = str(blk.get("name") or "unknown")
        return name not in {"not_loaded", "unknown"} and name not in disallowed_hazards

    # 1. Inspect floor (5x5 at dy=-1 + exit step (0, -1, 3))
    floor_cells = [(dx, -1, dz) for dx in range(-2, 3) for dz in range(-2, 3)] + [(0, -1, 3)]
    bridge_cells: List[List[int]] = []
    floor_solid_set = set()
    for rel in floor_cells:
        wpos, blk = read_rel_block(rel)
        name = str(blk.get("name") or "unknown")
        if name in {"not_loaded", "unknown"} or name in disallowed_hazards or name in protected or name.endswith("_bed"):
            return None
        props = blk.get("properties") if isinstance(blk.get("properties"), dict) else {}
        if props.get("waterlogged") in (True, "true"):
            return None
        if name in solid_ground:
            floor_solid_set.add(rel)
            continue
        if allow_floor_patch and name in replaceable_or_air:
            _, below = read_rel_block((rel[0], -2, rel[2]))
            bname = str(below.get("name") or "unknown")
            if bname in {"not_loaded", "unknown", "lava", "flowing_lava"}:
                return None
            # Require solid ground either directly below (shallow 1-deep dip) or leave for neighbor BFS check
            bridge_cells.append(wpos)
            continue
        return None

    if len(bridge_cells) > max_floor_patches or len(floor_solid_set) < 12:
        return None

    # Ensure every bridgeable hole connects horizontally or vertically to existing solid floor
    if bridge_cells:
        reached = set(floor_solid_set)
        bridge_rels = {
            rel for rel in floor_cells if rel not in floor_solid_set
        }
        for br_rel in tuple(bridge_rels):
            _, below = read_rel_block((br_rel[0], -2, br_rel[2]))
            if str(below.get("name") or "air") in solid_ground and is_safe_blk(below):
                reached.add(br_rel)
                bridge_rels.discard(br_rel)
        progressed = True
        while bridge_rels and progressed:
            progressed = False
            for br_rel in sorted(bridge_rels):
                if any((br_rel[0] + dx, br_rel[1], br_rel[2] + dz) in reached for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                    reached.add(br_rel)
                    bridge_rels.remove(br_rel)
                    progressed = True
        if bridge_rels:
            return None

    # 2. Inspect walls, roof, interior, doorway, exit corridor (dy in 0..2)
    wall_roof_rels = {
        c.rel_pos for c in SHELTER_5X5_SPEC.cells if c.stage in {"lower_walls", "upper_walls", "roof"}
    }
    door_rels = {(0, 0, 2), (0, 1, 2)}
    clear_candidates: List[Tuple[Tuple[int, int, int], List[int]]] = []
    already_built = 0
    for rel in SHELTER_5X5_SPEC.clearance_rel:
        wpos, blk = read_rel_block(rel)
        name = str(blk.get("name") or "unknown")
        if name in {"not_loaded", "unknown"} or name in disallowed_hazards or name in protected or name.endswith("_bed"):
            return None
        if not is_safe_blk(blk):
            return None
        if name in replaceable_or_air:
            continue
        if rel in wall_roof_rels and name in already_built_blocks:
            already_built += 1
            continue
        if rel in door_rels and name in WOODEN_DOOR_BLOCKS:
            continue
        if allow_clearing and name in clearable_blocks:
            # Ensure no adjacent water/lava/falling-liquid above before clearing
            for ndx, ndy, ndz in ((0, 1, 0), (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1)):
                _, nblk = read_rel_block((rel[0] + ndx, rel[1] + ndy, rel[2] + ndz))
                nname = str(nblk.get("name") or "unknown")
                if nname in {"not_loaded", "unknown", "water", "flowing_water", "lava", "flowing_lava"}:
                    return None
                if ndy == 1 and nname in {"sand", "red_sand", "gravel"}:
                    _, above2 = read_rel_block((rel[0], rel[1] + 2, rel[2]))
                    if str(above2.get("name") or "air") in {"water", "flowing_water", "lava", "flowing_lava", "not_loaded", "unknown"}:
                        return None
            clear_candidates.append((rel, wpos))
            continue
        return None

    if len(clear_candidates) > max_clear_cells:
        return None

    # Sort clearing order top-down (-world_y) so upper obstructions come off before lower ones
    clear_candidates.sort(key=lambda item: (-item[1][1], item[0][2], item[0][0]))
    clear_cells = [wpos for _, wpos in clear_candidates]

    doorway_outside = transform_rel_pos(
        SHELTER_5X5_SPEC.anchors["doorway_outside"].rel_pos, int_origin, norm_orient, integer=False
    )
    interior_center = transform_rel_pos(
        SHELTER_5X5_SPEC.anchors["interior_center"].rel_pos, int_origin, norm_orient, integer=False
    )
    doorway_world = transform_rel_pos((0, 0, 2), int_origin, norm_orient, integer=True)

    dist = 0.0
    elev_diff = 0.0
    if bot_pos is not None:
        dist = round(
            ((doorway_outside[0] - float(bot_pos[0])) ** 2 + (doorway_outside[2] - float(bot_pos[2])) ** 2) ** 0.5,
            3,
        )
        elev_diff = abs(float(int_origin[1]) - float(bot_pos[1]))

    score = (
        len(bridge_cells) * 4.5
        + len(clear_cells) * 0.75
        + elev_diff * 2.5
        + dist * 0.35
        - already_built * 0.4
    )
    return {
        "spec_id": SHELTER_5X5_SPEC.spec_id,
        "origin": list(int_origin),
        "orientation": norm_orient,
        "doorway_world": doorway_world,
        "doorway_outside": doorway_outside,
        "interior_center": interior_center,
        "bridge_cells": bridge_cells,
        "clear_cells": clear_cells,
        "already_built": already_built,
        "distance": dist,
        "score": round(score, 3),
    }


def find_shelter_sites(
    bot_pos: Sequence[float],
    read_block: Callable[[List[int]], Dict[str, Any]],
    *,
    radius: int = 12,
    orientations: Optional[Sequence[str]] = None,
    allow_clearing: bool = True,
    allow_floor_patch: bool = True,
    max_clear_cells: int = 24,
    max_floor_patches: int = 6,
    block_cache: Optional[Dict[Tuple[int, int, int], Dict[str, Any]]] = None,
    max_results: int = 5,
) -> List[Dict[str, Any]]:
    """Scan loaded nearby ground around bot_pos and rank buildable shelter plots."""
    bx = int(math.floor(float(bot_pos[0])))
    by = int(round(float(bot_pos[1])))
    bz = int(math.floor(float(bot_pos[2])))
    scan_r = min(max(3, int(radius)), 14)
    orient_list = tuple(normalize_orientation(o) for o in (orientations or CARDINAL_ORIENTATIONS))
    cache: Dict[Tuple[int, int, int], Dict[str, Any]] = block_cache if block_cache is not None else {}

    def cached_read(pos: List[int]) -> Dict[str, Any]:
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        if key not in cache:
            cache[key] = _safe_read_block(read_block, key)
        return cache[key]

    # Sample candidate (x, y, z) columns outward from bot position, checking ground height dynamically.
    ring_offsets = sorted(
        ((dx, dz) for dx in range(-scan_r, scan_r + 1) for dz in range(-scan_r, scan_r + 1) if dx * dx + dz * dz <= scan_r * scan_r),
        key=lambda d: (d[0] * d[0] + d[1] * d[1], abs(d[0]) + abs(d[1])),
    )
    matches: List[Dict[str, Any]] = []
    seen_origins = set()
    best_zero_work_dist: Optional[float] = None
    for dx, dz in ring_offsets:
        if len(matches) >= max(10, int(max_results) * 3) and best_zero_work_dist is not None:
            if (dx * dx + dz * dz) ** 0.5 > best_zero_work_dist + 4.0:
                break
        cx, cz = bx + dx, bz + dz
        # Find local surface feet Y near bot Y
        candidate_y: Optional[int] = None
        for dy in (0, -1, 1, -2, 2, -3):
            y_test = by + dy
            below_name = _block_name(cached_read([cx, y_test - 1, cz]))
            feet_name = _block_name(cached_read([cx, y_test, cz]))
            if (below_name in SHELTER_GROUND_BLOCKS or below_name in SHELTER_BUILDING_BLOCKS) and (
                feet_name in DEFAULT_REPLACEABLE or (allow_clearing and feet_name in SHELTER_CLEARABLE_BLOCKS)
            ):
                candidate_y = y_test
                break
        if candidate_y is None:
            continue
        origin_key = (cx, candidate_y, cz)
        if origin_key in seen_origins:
            continue
        seen_origins.add(origin_key)
        for orient in orient_list:
            site = evaluate_shelter_site(
                origin_key,
                orient,
                cached_read,
                bot_pos=bot_pos,
                allow_clearing=allow_clearing,
                allow_floor_patch=allow_floor_patch,
                max_clear_cells=max_clear_cells,
                max_floor_patches=max_floor_patches,
            )
            if site is not None:
                matches.append(site)
                if not site["bridge_cells"] and not site["clear_cells"]:
                    d = float(site["distance"])
                    if best_zero_work_dist is None or d < best_zero_work_dist:
                        best_zero_work_dist = d
                if len(matches) >= max(12, int(max_results) * 4) and best_zero_work_dist is not None:
                    break

    matches.sort(key=lambda s: (float(s["score"]), float(s["distance"])))
    return matches[: max(1, int(max_results))]



# ---------------------------------------------------------------------------
# Standard 5-layer Staircase Mine + Connected 4x3x2 Workstation Platform + Branch
# ---------------------------------------------------------------------------

MINE_BED_BLOCKS: FrozenSet[str] = frozenset({
    f"{c}_bed"
    for c in (
        "white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray",
        "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black",
    )
})
MINE_CHEST_BLOCKS: FrozenSet[str] = frozenset({"chest", "trapped_chest"})
MINE_TORCH_BLOCKS: FrozenSet[str] = frozenset({"torch", "wall_torch"})


def mine_shaft_spec(
    branch_length: int = 4,
    *,
    include_workstations: bool = False,
) -> StructureSpec:
    """Return canonical StructureSpec (south = +z forward, +x right) for a 5-step
    downward staircase leading into a connected 4x3x2 workstation platform whose
    4-block long side (dz=5..8) sits open and flush along the dx=0 mine tunnel,
    plus a continuation branch tunnel along dx=0, dz=9..8+branch_len.
    """
    branch_len = max(1, min(12, int(branch_length)))
    clear_expected: FrozenSet[str] = frozenset(AIR | MINE_TORCH_BLOCKS)
    workstation_rules: Dict[Tuple[int, int, int], Tuple[FrozenSet[str], str, int]] = {
        (2, -5, 5): (MINE_BED_BLOCKS, "workstation_bed_foot", 1),
        (3, -5, 5): (MINE_BED_BLOCKS, "workstation_bed_head", 2),
        (3, -5, 6): (frozenset({"crafting_table"}), "workstation_crafting_table", 3),
        (3, -4, 6): (frozenset({"furnace", "lit_furnace"}), "workstation_furnace", 4),
        (3, -5, 7): (MINE_CHEST_BLOCKS, "workstation_chest_1", 5),
        (3, -5, 8): (MINE_CHEST_BLOCKS, "workstation_chest_2", 6),
        (1, -4, 5): (MINE_TORCH_BLOCKS, "workstation_torch", 7),
    }
    cells: List[CellRule] = []

    # 1. 5-layer staircase (dz=0..4, foot_y=-1..-5): 3 vertical headroom blocks per step
    stair_floor_idx = 0
    stair_clear_idx = 0
    for step in range(1, 6):
        dz = step - 1
        foot_y = -step
        stair_floor_idx += 1
        cells.append(
            CellRule(
                (0, foot_y - 1, dz),
                MINE_FLOOR_BLOCKS,
                "stairs_floor",
                priority=stair_floor_idx,
                label=f"stair_floor_{step}",
            )
        )
        for off in (2, 1, 0):
            stair_clear_idx += 1
            cells.append(
                CellRule(
                    (0, foot_y + off, dz),
                    clear_expected,
                    "stairs_clear",
                    priority=stair_clear_idx,
                    label=f"stair_clear_{step}_{off}",
                )
            )

    # 2. Tunnel corridor (dx=0, dz=5..8) + open connected 4x3x2 platform (dx=1..3, dz=5..8)
    # The 4-block edge (dz=5..8) borders the mine tunnel cleanly with zero dividing wall.
    plat_floor_idx = 0
    plat_clear_idx = 0
    for dz in range(5, 9):
        for dx in range(0, 4):
            plat_floor_idx += 1
            cells.append(
                CellRule(
                    (dx, -6, dz),
                    MINE_FLOOR_BLOCKS,
                    "platform_floor",
                    priority=plat_floor_idx,
                    label=f"platform_floor_{dx}_{dz}",
                )
            )
            for dy in (-4, -5):
                if include_workstations and (dx, dy, dz) in workstation_rules:
                    continue
                plat_clear_idx += 1
                cells.append(
                    CellRule(
                        (dx, dy, dz),
                        clear_expected,
                        "platform_clear",
                        priority=plat_clear_idx,
                        label=f"platform_clear_{dx}_{dy}_{dz}",
                    )
                )

    # 3. Continuation 1x2 mining branch heading along dx=0, dz=9..(8 + branch_len)
    br_floor_idx = 0
    br_clear_idx = 0
    for step in range(1, branch_len + 1):
        dz = 8 + step
        br_floor_idx += 1
        cells.append(
            CellRule(
                (0, -6, dz),
                MINE_FLOOR_BLOCKS,
                "branch_floor",
                priority=br_floor_idx,
                label=f"branch_floor_{step}",
            )
        )
        for dy in (-4, -5):
            br_clear_idx += 1
            cells.append(
                CellRule(
                    (0, dy, dz),
                    clear_expected,
                    "branch_clear",
                    priority=br_clear_idx,
                    label=f"branch_clear_{step}_{dy}",
                )
            )

    if include_workstations:
        for rel, (expected, label, prio) in sorted(workstation_rules.items(), key=lambda kv: kv[1][2]):
            cells.append(
                CellRule(
                    rel,
                    expected,
                    "workstation",
                    priority=prio,
                    label=label,
                )
            )

    anchors = {
        "surface_entry": AnchorPoint("surface_entry", (0.5, 0.0, -0.5), tolerance=0.75, description="Surface entry before step 1"),
        "stair_top": AnchorPoint("stair_top", (0.5, -1.0, 0.5), tolerance=0.7, description="Top step of 5-layer staircase"),
        "shaft_bottom": AnchorPoint("shaft_bottom", (0.5, -5.0, 4.5), tolerance=0.7, description="Bottom step joining open 4x3x2 platform"),
        "platform_center": AnchorPoint("platform_center", (1.5, -5.0, 6.5), tolerance=0.75, description="Open floor aisle inside 4x3x2 platform"),
        "branch_entry": AnchorPoint("branch_entry", (0.5, -5.0, 8.5), tolerance=0.7, description="Main tunnel threshold into branch"),
        "branch_heading": AnchorPoint("branch_heading", (0.5, -5.0, float(8 + branch_len) + 0.5), tolerance=0.8, description="End of mining branch heading"),
        "workstation_bed_foot": AnchorPoint("workstation_bed_foot", (2.5, -5.0, 5.5), tolerance=0.5, description="Bed foot at dz=5 facing +right into wall"),
        "workstation_bed_head": AnchorPoint("workstation_bed_head", (3.5, -5.0, 5.5), tolerance=0.5, description="Bed head against far wall at dz=5"),
        "workstation_crafting_table": AnchorPoint("workstation_crafting_table", (3.5, -5.0, 6.5), tolerance=0.5, description="Crafting table beside bed at dz=6"),
        "workstation_furnace": AnchorPoint("workstation_furnace", (3.5, -4.0, 6.5), tolerance=0.5, description="Furnace stacked directly on crafting_table"),
        "workstation_chest_1": AnchorPoint("workstation_chest_1", (3.5, -5.0, 7.5), tolerance=0.5, description="Double chest primary half at dz=7"),
        "workstation_chest_2": AnchorPoint("workstation_chest_2", (3.5, -5.0, 8.5), tolerance=0.5, description="Double chest second half at dz=8"),
        "workstation_torch": AnchorPoint("workstation_torch", (1.5, -4.0, 5.5), tolerance=0.5, description="Wall torch beside bed near mine tunnel wall"),
    }

    clearance = [(0, 0, -1), (0, 1, -1)] + [
        c.rel_pos for c in cells if c.stage in {"stairs_clear", "platform_clear", "branch_clear"}
    ]
    stages = ("stairs_floor", "stairs_clear", "platform_floor", "platform_clear", "branch_floor", "branch_clear")
    if include_workstations:
        stages = stages + ("workstation",)
    return StructureSpec(
        spec_id="mine_shaft_5x4x3x2",
        stages=stages,
        cells=tuple(cells),
        anchors=anchors,
        clearance_rel=tuple(clearance),
        site_spec=None,
    )


MINE_SHAFT_SPEC: StructureSpec = mine_shaft_spec(branch_length=4)


def evaluate_mine_site(
    origin: Sequence[int],
    orientation: str,
    read_block: Callable[[List[int]], Dict[str, Any]],
    *,
    bot_pos: Optional[Sequence[float]] = None,
    branch_length: int = 4,
    max_floor_patches: int = 6,
    min_solid_yield: int = 16,
) -> Optional[Dict[str, Any]]:
    """Evaluate a surface entry + 5-step stair + 4x3x2 platform + branch mine plot."""
    norm_orient = normalize_orientation(orientation)
    int_origin = parse_pos(origin, integer=True)
    if int_origin is None:
        return None
    spec = mine_shaft_spec(branch_length=branch_length)

    cache: Dict[Tuple[int, int, int], Dict[str, Any]] = {}

    def read_rel(rel: Tuple[int, int, int]) -> Tuple[List[int], Dict[str, Any]]:
        wpos = transform_rel_pos(rel, int_origin, norm_orient, integer=True)
        key = (wpos[0], wpos[1], wpos[2])
        if key not in cache:
            cache[key] = _safe_read_block(read_block, wpos)
        return wpos, cache[key]

    protected = set(SHELTER_PROTECTED_BLOCKS) | set(WOODEN_DOOR_BLOCKS)

    def safe_non_hazard(blk: Dict[str, Any]) -> bool:
        props = blk.get("properties") if isinstance(blk.get("properties"), dict) else {}
        if props.get("waterlogged") in (True, "true"):
            return False
        name = str(blk.get("name") or "unknown")
        return (
            name not in {"not_loaded", "unknown"}
            and name not in MINE_HAZARD_BLOCKS
            and name not in protected
            and not name.endswith("_bed")
        )

    # 1. Surface entry must be walkable and clearable
    _, entry_floor = read_rel((0, -1, -1))
    if not safe_non_hazard(entry_floor) or str(entry_floor.get("name") or "") not in MINE_FLOOR_BLOCKS:
        return None
    for erel in ((0, 0, -1), (0, 1, -1)):
        _, eblk = read_rel(erel)
        ename = str(eblk.get("name") or "unknown")
        if not safe_non_hazard(eblk):
            return None
        if ename not in DEFAULT_REPLACEABLE and ename not in SHELTER_CLEARABLE_BLOCKS:
            return None

    # 2. Check all floors (stairs, 4x3x2 platform, branch)
    floor_rules = [c for c in spec.cells if c.stage.endswith("_floor")]
    floor_patches: List[List[int]] = []
    stair_patches = 0
    for rule in floor_rules:
        wpos, blk = read_rel(rule.rel_pos)
        name = str(blk.get("name") or "unknown")
        if not safe_non_hazard(blk):
            return None
        if name in MINE_FLOOR_BLOCKS:
            continue
        if name in DEFAULT_REPLACEABLE:
            # Verify at least one orthogonal neighbor is already solid so place_block has support
            rx, ry, rz = rule.rel_pos
            has_solid_anchor = any(
                str(read_rel((rx + sx, ry + sy, rz + sz))[1].get("name") or "unknown") in MINE_FLOOR_BLOCKS
                for sx, sy, sz in ((0, -1, 0), (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1), (0, 1, 0))
            )
            if not has_solid_anchor:
                return None
            if rule.stage == "stairs_floor":
                stair_patches += 1
                if stair_patches > 1:
                    return None
            floor_patches.append(wpos)
            continue
        return None
    if len(floor_patches) > max_floor_patches:
        return None

    # 3. Check all excavated clear cells & adjacent fluid/spawner shell
    clear_rules = [c for c in spec.cells if c.stage.endswith("_clear")]
    clear_set = {c.rel_pos for c in clear_rules}
    solid_yield = 0
    stone_yield = 0
    ore_yield = 0
    for rule in clear_rules:
        _, blk = read_rel(rule.rel_pos)
        name = str(blk.get("name") or "unknown")
        if not safe_non_hazard(blk):
            return None
        if name in DEFAULT_REPLACEABLE:
            pass
        elif name in MINE_DIGGABLE_BLOCKS:
            solid_yield += 1
            if name in MINE_STONE_BLOCKS:
                stone_yield += 1
            if name in MINE_ORE_BLOCKS:
                ore_yield += 1
        else:
            return None

        # Reject if an adjacent cell to any excavated block is fluid, lava, falling-above-liquid, or unloaded
        rx, ry, rz = rule.rel_pos
        for ndx, ndy, ndz in ((0, 1, 0), (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1)):
            nrel = (rx + ndx, ry + ndy, rz + ndz)
            if nrel in clear_set:
                continue
            _, nblk = read_rel(nrel)
            if not safe_non_hazard(nblk):
                return None

    if solid_yield < min_solid_yield:
        return None

    entry_pos = transform_rel_pos(spec.anchors["surface_entry"].rel_pos, int_origin, norm_orient, integer=False)
    dist = 0.0
    elev_diff = 0.0
    if bot_pos is not None:
        dist = round(((entry_pos[0] - float(bot_pos[0])) ** 2 + (entry_pos[2] - float(bot_pos[2])) ** 2) ** 0.5, 3)
        elev_diff = abs(float(int_origin[1]) - float(bot_pos[1]))

    score = (
        len(floor_patches) * 4.0
        + elev_diff * 1.5
        + dist * 0.45
        - stone_yield * 0.25
        - ore_yield * 0.8
    )
    workstation_slots = {
        slot: transform_rel_pos(spec.anchors[slot].rel_pos, int_origin, norm_orient, integer=True)
        for slot in (
            "workstation_bed_foot",
            "workstation_bed_head",
            "workstation_crafting_table",
            "workstation_furnace",
            "workstation_chest_1",
            "workstation_chest_2",
            "workstation_torch",
        )
    }
    return {
        "spec_id": spec.spec_id,
        "origin": list(int_origin),
        "orientation": norm_orient,
        "branch_length": max(1, min(12, int(branch_length))),
        "surface_entry": entry_pos,
        "shaft_bottom": transform_rel_pos(spec.anchors["shaft_bottom"].rel_pos, int_origin, norm_orient, integer=False),
        "platform_center": transform_rel_pos(spec.anchors["platform_center"].rel_pos, int_origin, norm_orient, integer=False),
        "branch_heading": transform_rel_pos(spec.anchors["branch_heading"].rel_pos, int_origin, norm_orient, integer=False),
        "workstation_slots": workstation_slots,
        "floor_patches": floor_patches,
        "solid_yield": solid_yield,
        "stone_yield": stone_yield,
        "ore_yield": ore_yield,
        "distance": dist,
        "score": round(score, 3),
    }


def find_mine_sites(
    bot_pos: Sequence[float],
    read_block: Callable[[List[int]], Dict[str, Any]],
    *,
    radius: int = 10,
    orientations: Optional[Sequence[str]] = None,
    branch_length: int = 4,
    max_floor_patches: int = 6,
    min_solid_yield: int = 16,
    block_cache: Optional[Dict[Tuple[int, int, int], Dict[str, Any]]] = None,
    max_results: int = 5,
) -> List[Dict[str, Any]]:
    """Scan loaded terrain around bot_pos and rank safe 5-layer staircase mine entry sites."""
    bx = int(math.floor(float(bot_pos[0])))
    by = int(round(float(bot_pos[1])))
    bz = int(math.floor(float(bot_pos[2])))
    scan_r = min(max(3, int(radius)), 12)
    orient_list = tuple(normalize_orientation(o) for o in (orientations or CARDINAL_ORIENTATIONS))
    cache: Dict[Tuple[int, int, int], Dict[str, Any]] = block_cache if block_cache is not None else {}

    def cached_read(pos: List[int]) -> Dict[str, Any]:
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        if key not in cache:
            cache[key] = _safe_read_block(read_block, key)
        return cache[key]

    ring_offsets = sorted(
        ((dx, dz) for dx in range(-scan_r, scan_r + 1) for dz in range(-scan_r, scan_r + 1) if dx * dx + dz * dz <= scan_r * scan_r),
        key=lambda d: (d[0] * d[0] + d[1] * d[1], abs(d[0]) + abs(d[1])),
    )
    matches: List[Dict[str, Any]] = []
    seen_origins = set()
    for dx, dz in ring_offsets:
        if len(matches) >= max(8, int(max_results) * 3):
            break
        cx, cz = bx + dx, bz + dz
        candidate_y: Optional[int] = None
        for dy in (0, -1, 1, -2, 2):
            y_test = by + dy
            below_name = _block_name(cached_read([cx, y_test - 1, cz]))
            feet_name = _block_name(cached_read([cx, y_test, cz]))
            if below_name in MINE_FLOOR_BLOCKS and (
                feet_name in DEFAULT_REPLACEABLE or feet_name in SHELTER_CLEARABLE_BLOCKS
            ):
                candidate_y = y_test
                break
        if candidate_y is None:
            continue
        origin_key = (cx, candidate_y, cz)
        if origin_key in seen_origins:
            continue
        seen_origins.add(origin_key)
        for orient in orient_list:
            site = evaluate_mine_site(
                origin_key,
                orient,
                cached_read,
                bot_pos=bot_pos,
                branch_length=branch_length,
                max_floor_patches=max_floor_patches,
                min_solid_yield=min_solid_yield,
            )
            if site is not None:
                matches.append(site)

    matches.sort(key=lambda s: (float(s["score"]), float(s["distance"])))
    return matches[: max(1, int(max_results))]


# ---------------------------------------------------------------------------
# Cave Exploration & Targeted Ore Mining Constants (Plan v2)
# ---------------------------------------------------------------------------

WATER_LIKE_BLOCKS: FrozenSet[str] = frozenset({
    "water",
    "flowing_water",
    "bubble_column",
    "kelp",
    "kelp_plant",
    "seagrass",
    "tall_seagrass",
})

LAVA_LIKE_BLOCKS: FrozenSet[str] = frozenset({
    "lava",
    "flowing_lava",
})

MINESHAFT_MARKER_BLOCKS: FrozenSet[str] = frozenset({
    "oak_planks",
    "dark_oak_planks",
    "oak_fence",
    "dark_oak_fence",
    "rail",
    "powered_rail",
    "detector_rail",
    "activator_rail",
})

CAVE_LIMITS: Dict[str, Any] = {
    "flood_max_cells": 160,
    "horizontal_radius": 12,
    "vertical_span": 10,
    "max_height": 10,
    "max_drop": 4,
    "max_water_blocks": 3,
    "small_bubble_max_air": 4,
    "min_mineshaft_markers": 3,
    "min_enter_torches": 0,
    "explore_radius": 48,
    "explore_max_visited": 600,
    "explore_timeout_seconds": 360.0,
    "torch_interval": 24,
    "max_vein_blocks": 16,
    "max_unsafe_retries": 3,
    "eye_scan_radius": 4.5,
}

ORE_DROP_MAP: Dict[str, str] = {
    "coal_ore": "coal",
    "deepslate_coal_ore": "coal",
    "iron_ore": "raw_iron",
    "deepslate_iron_ore": "raw_iron",
    "copper_ore": "raw_copper",
    "deepslate_copper_ore": "raw_copper",
    "gold_ore": "raw_gold",
    "deepslate_gold_ore": "raw_gold",
    "nether_gold_ore": "raw_gold",
    "redstone_ore": "redstone",
    "deepslate_redstone_ore": "redstone",
    "lapis_ore": "lapis_lazuli",
    "deepslate_lapis_ore": "lapis_lazuli",
    "diamond_ore": "diamond",
    "deepslate_diamond_ore": "diamond",
    "emerald_ore": "emerald",
    "deepslate_emerald_ore": "emerald",
    "nether_quartz_ore": "quartz",
}

ORE_MIN_TOOL: Dict[str, str] = {
    "coal_ore": "wooden_pickaxe",
    "deepslate_coal_ore": "wooden_pickaxe",
    "nether_quartz_ore": "wooden_pickaxe",
    "copper_ore": "stone_pickaxe",
    "deepslate_copper_ore": "stone_pickaxe",
    "iron_ore": "stone_pickaxe",
    "deepslate_iron_ore": "stone_pickaxe",
    "lapis_ore": "stone_pickaxe",
    "deepslate_lapis_ore": "stone_pickaxe",
    "gold_ore": "iron_pickaxe",
    "deepslate_gold_ore": "iron_pickaxe",
    "redstone_ore": "iron_pickaxe",
    "deepslate_redstone_ore": "iron_pickaxe",
    "diamond_ore": "iron_pickaxe",
    "deepslate_diamond_ore": "iron_pickaxe",
    "emerald_ore": "iron_pickaxe",
    "deepslate_emerald_ore": "iron_pickaxe",
}

TOOL_MAX_DURABILITY: Dict[str, int] = {
    "wooden": 59,
    "stone": 131,
    "iron": 250,
    "golden": 32,
    "diamond": 1561,
    "netherite": 2031,
}
