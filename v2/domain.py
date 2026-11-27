"""L0 Pure domain constants, geometry helpers, item/block classifications, and oriented structures."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

Coord3 = Tuple[int, int, int]
Pos3 = Tuple[float, float, float]


def block_coord(pos: Sequence[float]) -> Coord3:
    """Convert a float/int 3D position into integer block coordinates."""
    return (math.floor(float(pos[0])), math.floor(float(pos[1])), math.floor(float(pos[2])))


def dist3(a: Sequence[float], b: Sequence[float]) -> float:
    return math.dist((float(a[0]), float(a[1]), float(a[2])), (float(b[0]), float(b[1]), float(b[2])))


def dist2_xz(a: Sequence[float], b: Sequence[float]) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[2]) - float(b[2]))


AIR_BLOCKS: FrozenSet[str] = frozenset({"air", "cave_air", "void_air"})
LIQUID_BLOCKS: FrozenSet[str] = frozenset({"water", "lava", "flowing_water", "flowing_lava"})
WATER_BLOCKS: FrozenSet[str] = frozenset({"water", "flowing_water"})
LAVA_BLOCKS: FrozenSet[str] = frozenset({"lava", "flowing_lava"})
GRAVITY_BLOCKS: FrozenSet[str] = frozenset({"sand", "red_sand", "gravel", "anvil", "chipped_anvil", "damaged_anvil"})

PASS_THROUGH_BLOCKS: FrozenSet[str] = AIR_BLOCKS | frozenset({
    "short_grass", "tall_grass", "fern", "large_fern", "dead_bush", "leaf_litter",
    "vine", "glow_lichen", "hanging_roots", "moss_carpet", "pale_moss_carpet",
    "pale_hanging_moss", "eyeblossom", "open_eyeblossom", "closed_eyeblossom",
    "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet", "oxeye_daisy",
    "cornflower", "lily_of_the_valley", "orange_tulip", "pink_tulip", "red_tulip",
    "white_tulip", "sunflower", "lilac", "rose_bush", "peony", "torch", "wall_torch",
    "snow", "sugar_cane", "nether_portal", "fire", "soul_fire",
})

NON_SOLID_BLOCKS: FrozenSet[str] = PASS_THROUGH_BLOCKS | LIQUID_BLOCKS
HAZARD_BLOCKS: FrozenSet[str] = LAVA_BLOCKS | frozenset({
    "fire", "soul_fire", "campfire", "soul_campfire", "magma_block", "cactus", "sweet_berry_bush", "wither_rose",
})
SOFT_OCCLUDERS: FrozenSet[str] = frozenset({
    "short_grass", "tall_grass", "fern", "large_fern", "bush", "dead_bush", "snow",
    "dandelion", "poppy", "sugar_cane", "vine", "leaf_litter", "dirt", "grass_block",
    "oak_leaves", "birch_leaves", "spruce_leaves", "jungle_leaves", "acacia_leaves",
    "dark_oak_leaves", "mangrove_leaves", "cherry_leaves", "pale_oak_leaves", "azalea_leaves", "bamboo",
})

LOG_BLOCKS: FrozenSet[str] = frozenset({
    "oak_log", "birch_log", "spruce_log", "jungle_log", "acacia_log",
    "dark_oak_log", "mangrove_log", "cherry_log", "pale_oak_log",
})
LEAF_BLOCKS: FrozenSet[str] = frozenset({
    "oak_leaves", "birch_leaves", "spruce_leaves", "jungle_leaves", "acacia_leaves",
    "dark_oak_leaves", "mangrove_leaves", "cherry_leaves", "pale_oak_leaves",
    "azalea_leaves", "flowering_azalea_leaves",
})
PLANK_ITEMS: FrozenSet[str] = frozenset({
    "oak_planks", "birch_planks", "spruce_planks", "jungle_planks", "acacia_planks",
    "dark_oak_planks", "mangrove_planks", "cherry_planks", "pale_oak_planks",
    "bamboo_planks", "crimson_planks", "warped_planks",
})
SCAFFOLD_BLOCKS: Tuple[str, ...] = (
    "cobblestone", "cobbled_deepslate", "dirt", "netherrack", "oak_planks",
    "spruce_planks", "birch_planks", "stone", "deepslate", "andesite", "diorite", "granite",
)
FUEL_ITEMS: Tuple[str, ...] = (
    "coal", "charcoal", "coal_block", "blaze_rod",
    "oak_planks", "birch_planks", "spruce_planks", "jungle_planks", "acacia_planks", "dark_oak_planks",
    "oak_log", "birch_log", "spruce_log", "jungle_log", "stick",
)
FOOD_ITEMS: Dict[str, int] = {
    "cooked_beef": 8, "cooked_porkchop": 8, "golden_carrot": 6, "cooked_mutton": 6,
    "cooked_salmon": 6, "cooked_chicken": 6, "bread": 5, "baked_potato": 5,
    "cooked_cod": 5, "cooked_rabbit": 5, "apple": 4, "golden_apple": 4,
    "beef": 3, "porkchop": 3, "mutton": 2, "chicken": 2, "carrot": 3,
    "melon_slice": 2, "sweet_berries": 2, "rotten_flesh": 4,
}
JUNK_DROP_PRIORITY: Tuple[str, ...] = (
    "rotten_flesh", "poisonous_potato", "spider_eye", "wheat_seeds", "beetroot_seeds",
    "gravel", "diorite", "granite", "andesite", "tuff", "dirt", "sand",
    "oak_sapling", "birch_sapling", "spruce_sapling", "feather", "bone", "string",
)

HOSTILE_MOBS: FrozenSet[str] = frozenset({
    "zombie", "husk", "drowned", "zombie_villager", "skeleton", "stray", "bogged",
    "creeper", "spider", "cave_spider", "enderman", "witch", "pillager", "vindicator",
    "ravager", "phantom", "slime", "magma_cube", "blaze", "ghast", "wither_skeleton",
    "piglin_brute", "hoglin", "zoglin", "silverfish", "warden", "breeze",
})
RANGED_MOBS: FrozenSet[str] = frozenset({
    "skeleton", "stray", "bogged", "drowned", "pillager", "witch", "blaze", "ghast", "breeze",
})
PASSIVE_ANIMALS: FrozenSet[str] = frozenset({
    "cow", "pig", "sheep", "chicken", "rabbit", "mooshroom",
})
BED_BLOCKS: Tuple[str, ...] = (
    "white_bed", "red_bed", "blue_bed", "green_bed", "yellow_bed", "black_bed",
    "brown_bed", "cyan_bed", "gray_bed", "light_blue_bed", "light_gray_bed",
    "lime_bed", "magenta_bed", "orange_bed", "pink_bed", "purple_bed",
)
BOAT_ITEMS: Tuple[str, ...] = (
    "oak_boat", "spruce_boat", "birch_boat", "jungle_boat", "acacia_boat",
    "dark_oak_boat", "mangrove_boat", "cherry_boat", "pale_oak_boat", "bamboo_raft",
)

PICKAXE_TIERS: Tuple[str, ...] = (
    "netherite_pickaxe", "diamond_pickaxe", "iron_pickaxe", "stone_pickaxe", "golden_pickaxe", "wooden_pickaxe",
)
SWORD_TIERS: Tuple[str, ...] = (
    "netherite_sword", "diamond_sword", "iron_sword", "stone_sword", "golden_sword", "wooden_sword",
)
AXE_TIERS: Tuple[str, ...] = (
    "netherite_axe", "diamond_axe", "iron_axe", "stone_axe", "golden_axe", "wooden_axe",
)
SHOVEL_TIERS: Tuple[str, ...] = (
    "netherite_shovel", "diamond_shovel", "iron_shovel", "stone_shovel", "golden_shovel", "wooden_shovel",
)

# Upstream /inventory uses `armor.chest` while /equip uses `slot="torso"`.
ARMOR_EQUIP_SLOT_BY_INVENTORY_KEY: Dict[str, str] = {
    "head": "head",
    "chest": "torso",
    "legs": "legs",
    "feet": "feet",
}
ARMOR_RANK: Dict[str, int] = {
    "leather": 1, "golden": 2, "chainmail": 3, "iron": 4, "diamond": 5, "netherite": 6,
}


def pick_best_item(inventory_counts: Dict[str, int], ranked: Sequence[str]) -> Optional[str]:
    for name in ranked:
        if inventory_counts.get(name, 0) > 0:
            return name
    return None


def rotate_offset(dx: int, dy: int, dz: int, facing: str = "north") -> Coord3:
    """Rotate a canonical north-facing local offset to north/south/east/west."""
    if facing == "north":
        return (dx, dy, dz)
    if facing == "south":
        return (-dx, dy, -dz)
    if facing == "east":
        return (-dz, dy, dx)
    if facing == "west":
        return (dz, dy, -dx)
    raise ValueError(f"Unsupported facing: {facing}")


@dataclass(frozen=True)
class StructureBlock:
    offset: Coord3
    block: str
    role: str = "wall"


@dataclass(frozen=True)
class StructureSpec:
    """Four-orientation shelter blueprint ensuring door/bed/chest/home share one facing."""

    name: str
    origin: Coord3
    facing: str = "north"
    blocks: Tuple[StructureBlock, ...] = ()
    interior_offset: Coord3 = (0, 1, 0)
    door_offset: Coord3 = (0, 1, -2)
    bed_offset: Coord3 = (1, 1, 1)
    chest_offset: Coord3 = (-1, 1, 1)

    def to_world(self, offset: Coord3) -> Coord3:
        rx, ry, rz = rotate_offset(offset[0], offset[1], offset[2], self.facing)
        return (self.origin[0] + rx, self.origin[1] + ry, self.origin[2] + rz)

    def world_blocks(self) -> List[Tuple[Coord3, str, str]]:
        return [(self.to_world(sb.offset), sb.block, sb.role) for sb in self.blocks]

    def interior_pos(self) -> Coord3:
        return self.to_world(self.interior_offset)

    def door_pos(self) -> Coord3:
        return self.to_world(self.door_offset)

    def bed_pos(self) -> Coord3:
        return self.to_world(self.bed_offset)

    def chest_pos(self) -> Coord3:
        return self.to_world(self.chest_offset)

    def protected_coords(self) -> FrozenSet[Coord3]:
        coords = {self.to_world(sb.offset) for sb in self.blocks}
        coords.update({self.door_pos(), self.bed_pos(), self.chest_pos()})
        return frozenset(coords)


def compact_shelter_spec(origin: Coord3, facing: str = "north", material: str = "cobblestone") -> StructureSpec:
    """Standard 5x4x5 starter shelter with 4-orientation support."""
    blocks: List[StructureBlock] = []
    # Floor & roof & walls around x in [-2..2], z in [-2..2], y in [0..3]
    for dx in range(-2, 3):
        for dz in range(-2, 3):
            blocks.append(StructureBlock((dx, 0, dz), material, "floor"))
            blocks.append(StructureBlock((dx, 3, dz), material, "roof"))
    for dy in (1, 2):
        for dx in range(-2, 3):
            for dz in (-2, 2):
                if dx == 0 and dz == -2:
                    continue  # Doorway opening
                blocks.append(StructureBlock((dx, dy, dz), material, "wall"))
        for dz in range(-1, 2):
            for dx in (-2, 2):
                blocks.append(StructureBlock((dx, dy, dz), material, "wall"))
    return StructureSpec(
        name="compact_shelter",
        origin=origin,
        facing=facing,
        blocks=tuple(blocks),
        interior_offset=(0, 1, 0),
        door_offset=(0, 1, -2),
        bed_offset=(1, 1, 1),
        chest_offset=(-1, 1, 1),
    )


LOOT_WHITELIST_ITEMS: FrozenSet[str] = frozenset({
    "iron_ingot", "gold_ingot", "diamond", "emerald", "obsidian", "crying_obsidian",
    "flint_and_steel", "flint", "iron_nugget", "gold_nugget", "bucket", "water_bucket",
    "iron_pickaxe", "iron_sword", "iron_axe", "iron_shovel",
    "iron_helmet", "iron_chestplate", "iron_leggings", "iron_boots",
    "golden_helmet", "golden_chestplate", "golden_leggings", "golden_boots",
    "golden_apple", "enchanted_golden_apple", "bread", "apple",
    "cooked_beef", "cooked_porkchop", "cooked_mutton", "cooked_chicken", "baked_potato",
    "coal", "oak_log", "spruce_log", "birch_log", "saddle", "name_tag",
    "arrow", "ender_pearl", "blaze_rod", "fire_charge",
})

VALUABLE_STASH_ITEMS: FrozenSet[str] = frozenset({
    "diamond", "emerald", "iron_ingot", "gold_ingot", "raw_iron", "raw_gold",
    "obsidian", "flint_and_steel", "water_bucket", "bucket",
    "golden_apple", "enchanted_golden_apple", "blaze_rod", "ender_pearl",
    "diamond_pickaxe", "diamond_sword", "diamond_chestplate",
    "iron_pickaxe", "iron_sword", "iron_helmet", "iron_chestplate", "iron_leggings", "iron_boots",
})

DEEP_DARK_BLOCKS: FrozenSet[str] = frozenset({
    "sculk_sensor", "calibrated_sculk_sensor", "sculk_shrieker", "sculk_catalyst",
})

STONE_MOLD_ITEMS: Tuple[str, ...] = ("cobblestone", "cobbled_deepslate")

