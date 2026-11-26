"""Baseline regression scenarios reproducing real v30/v31 live failures."""

from __future__ import annotations

from tests_v2.sim.virtual_mc_api import VirtualMcApiServer


def _build_flat_ground(sim: VirtualMcApiServer, x1: int, x2: int, z1: int, z2: int, y: int = 63) -> None:
    sim.fill(x1, y - 2, z1, x2, y - 1, z2, "stone")
    sim.fill(x1, y, z1, x2, y, z2, "grass_block")


def scenario_v31_64m_lava_portal() -> VirtualMcApiServer:
    """Reproduce v31 bot_3: player ready for portal, lava pool 54m away across a small ridge."""
    sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5), loaded_radius=96)
    _build_flat_ground(sim, -8, 66, -12, 12, y=63)
    # 1-block low wall / foliage obstacle at x=25 to test 3D pathing / micro-clear
    for z in range(-12, 13):
        sim.set_block(25, 64, z, "oak_leaves")
        sim.set_block(25, 65, z, "oak_leaves")
    # Lava pool at x=54..57, z=-1..2, y=63 (sunk 1 block in ground)
    sim.fill(54, 62, -1, 57, 62, 2, "stone")
    sim.fill(54, 63, -1, 57, 63, 2, "lava", {"level": "0"})
    # Water source near lava pool
    sim.set_block(51, 63, 3, "water", {"level": "0"})
    sim.give("iron_pickaxe", 1)
    sim.give("water_bucket", 1)
    sim.give("flint_and_steel", 1)
    sim.give("cobblestone", 32)
    sim.give("bread", 8)
    return sim


def scenario_v31_sloped_water_bank() -> VirtualMcApiServer:
    """Reproduce v31 bot_1/bot_6: recessed water source occluded by grass/dirt bank lip on y+0.85 ray."""
    sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5), loaded_radius=48)
    _build_flat_ground(sim, -8, 12, -8, 8, y=63)
    # Recessed water at (4, 62, 0), bank lip at (3, 63, 0) plus short_grass at (3, 64, 0)
    sim.set_block(4, 63, 0, "air")
    sim.set_block(4, 62, 0, "water", {"level": "0"})
    sim.set_block(4, 61, 0, "stone")
    # Place occluder on bank lip (3, 64, 0) blocking y+0.85 ray from (1.5, 64, 0.5)
    sim.set_block(3, 64, 0, "short_grass")
    sim.give("bucket", 1)
    sim.give("stone_shovel", 1)
    return sim


def scenario_v30_deep_pit_and_canopy() -> tuple[VirtualMcApiServer, VirtualMcApiServer]:
    """Reproduce v30 deep shaft pit at y=43 and high tree canopy at y=78 with unreachable zombie at y=64."""
    # 1) Deep pit
    pit = VirtualMcApiServer(spawn_pos=(0.5, 43.0, 0.5), loaded_radius=48)
    pit.fill(-6, 40, -6, 6, 63, 6, "stone")
    # 1x1 vertical shaft up to y=64
    pit.fill(0, 43, 0, 0, 64, 0, "air")
    pit.give("cobblestone", 48)
    pit.give("stone_pickaxe", 1)

    # 2) High canopy with ground zombie
    canopy = VirtualMcApiServer(spawn_pos=(0.5, 78.0, 0.5), loaded_radius=48)
    _build_flat_ground(canopy, -10, 10, -10, 10, y=63)
    # Tree trunk + canopy platform at y=77
    canopy.fill(0, 64, 0, 0, 76, 0, "oak_log")
    canopy.fill(-2, 77, -2, 2, 77, 2, "oak_leaves")
    canopy.add_entity(101, "zombie", (1.5, 64.0, 1.5), kind="hostile", health=20.0)
    return pit, canopy


def scenario_full_nether_speedrun() -> VirtualMcApiServer:
    """Empty-handed spawn with trees, stone, coal, iron ore, gravel, water, and 54m lava pool."""
    sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5), loaded_radius=96)
    _build_flat_ground(sim, -16, 66, -16, 16, y=63)
    # Trees nearby
    for tx, tz in ((4, 2), (6, -3), (-4, 3)):
        for ty in range(64, 69):
            sim.set_block(tx, ty, tz, "oak_log")
    # Exposed stone, coal, iron ore, gravel on surface hillside (x=-6..-3, z=-6..-3)
    for idx in range(6):
        sim.set_block(-5, 64, -3 - idx, "stone")
        sim.set_block(-6, 64, -3 - idx, "coal_ore")
        sim.set_block(-7, 64, -3 - idx, "iron_ore")
    for idx in range(4):
        sim.set_block(-4, 64, -3 - idx, "gravel")
    # Surface water source
    sim.set_block(2, 63, -5, "water", {"level": "0"})
    # Surface lava pool 54m away
    sim.fill(54, 62, -1, 57, 62, 2, "stone")
    sim.fill(54, 63, -1, 57, 63, 2, "lava", {"level": "0"})
    return sim
