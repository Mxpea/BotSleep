"""L3b TaskCatalogue: unified 886-recipe + mc_tree + smelting + gathering + competition DAG compiler."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class RecipeEntry:
    item: str
    ingredients: Dict[str, int]
    output_count: int
    needs_table: bool


@dataclass(frozen=True)
class SmeltEntry:
    output: str
    input_item: str
    default_fuel: str = "coal"


@dataclass(frozen=True)
class GatherEntry:
    item: str
    block_types: Tuple[str, ...]
    min_pickaxe: Optional[str] = None  # wooden_pickaxe | stone_pickaxe | iron_pickaxe
    preferred_y: Optional[int] = None


SMELT_TABLE: Dict[str, SmeltEntry] = {
    "iron_ingot": SmeltEntry("iron_ingot", "raw_iron"),
    "gold_ingot": SmeltEntry("gold_ingot", "raw_gold"),
    "copper_ingot": SmeltEntry("copper_ingot", "raw_copper"),
    "stone": SmeltEntry("stone", "cobblestone"),
    "glass": SmeltEntry("glass", "sand"),
    "charcoal": SmeltEntry("charcoal", "oak_log"),
    "cooked_beef": SmeltEntry("cooked_beef", "beef"),
    "cooked_porkchop": SmeltEntry("cooked_porkchop", "porkchop"),
    "cooked_mutton": SmeltEntry("cooked_mutton", "mutton"),
    "cooked_chicken": SmeltEntry("cooked_chicken", "chicken"),
    "baked_potato": SmeltEntry("baked_potato", "potato"),
    "brick": SmeltEntry("brick", "clay_ball"),
    "nether_brick": SmeltEntry("nether_brick", "netherrack"),
}

GATHER_TABLE: Dict[str, GatherEntry] = {
    "oak_log": GatherEntry("oak_log", ("oak_log",)),
    "birch_log": GatherEntry("birch_log", ("birch_log",)),
    "spruce_log": GatherEntry("spruce_log", ("spruce_log",)),
    "jungle_log": GatherEntry("jungle_log", ("jungle_log",)),
    "acacia_log": GatherEntry("acacia_log", ("acacia_log",)),
    "dark_oak_log": GatherEntry("dark_oak_log", ("dark_oak_log",)),
    "cobblestone": GatherEntry("cobblestone", ("stone", "cobblestone"), min_pickaxe="wooden_pickaxe"),
    "coal": GatherEntry("coal", ("coal_ore", "deepslate_coal_ore"), min_pickaxe="wooden_pickaxe", preferred_y=48),
    "raw_iron": GatherEntry("raw_iron", ("iron_ore", "deepslate_iron_ore"), min_pickaxe="stone_pickaxe", preferred_y=16),
    "raw_gold": GatherEntry("raw_gold", ("gold_ore", "deepslate_gold_ore"), min_pickaxe="iron_pickaxe", preferred_y=-16),
    "raw_copper": GatherEntry("raw_copper", ("copper_ore", "deepslate_copper_ore"), min_pickaxe="stone_pickaxe", preferred_y=48),
    "diamond": GatherEntry("diamond", ("diamond_ore", "deepslate_diamond_ore"), min_pickaxe="iron_pickaxe", preferred_y=-54),
    "redstone": GatherEntry("redstone", ("redstone_ore", "deepslate_redstone_ore"), min_pickaxe="iron_pickaxe", preferred_y=-54),
    "lapis_lazuli": GatherEntry("lapis_lazuli", ("lapis_ore", "deepslate_lapis_ore"), min_pickaxe="stone_pickaxe", preferred_y=0),
    "flint": GatherEntry("flint", ("gravel",)),
    "gravel": GatherEntry("gravel", ("gravel",)),
    "sand": GatherEntry("sand", ("sand", "red_sand")),
    "dirt": GatherEntry("dirt", ("dirt", "grass_block")),
    "obsidian": GatherEntry("obsidian", ("obsidian",), min_pickaxe="diamond_pickaxe"),
    "netherrack": GatherEntry("netherrack", ("netherrack",), min_pickaxe="wooden_pickaxe"),
    "sugar_cane": GatherEntry("sugar_cane", ("sugar_cane",)),
    "clay_ball": GatherEntry("clay_ball", ("clay",)),
    "hay_block": GatherEntry("hay_block", ("hay_block",)),
}

# Maps competition advancement IDs to concrete item/goal routes solvable by v2 tasks!
ADVANCEMENT_GOAL_ROUTES: Dict[str, Tuple[str, str]] = {
    # (kind, target) where kind in {"acquire", "enter_nether", "shelter", "equip", "hunt", "smelt"}
    "minecraft:story/mine_stone": ("acquire", "cobblestone"),
    "minecraft:story/upgrade_tools": ("acquire", "stone_pickaxe"),
    "minecraft:story/smelt_iron": ("acquire", "iron_ingot"),
    "minecraft:story/iron_tools": ("acquire", "iron_pickaxe"),
    "minecraft:story/obtain_armor": ("acquire", "iron_chestplate"),
    "minecraft:story/deflect_arrow": ("acquire", "shield"),
    "minecraft:story/lava_bucket": ("acquire", "lava_bucket"),
    "minecraft:story/form_obsidian": ("acquire", "obsidian"),
    "minecraft:story/enter_the_nether": ("enter_nether", "the_nether"),
    "minecraft:story/mine_diamond": ("acquire", "diamond"),
    "minecraft:story/shiny_gear": ("acquire", "diamond_chestplate"),
    "minecraft:story/enchant_item": ("acquire", "enchanting_table"),
    "minecraft:adventure/sleep_in_bed": ("acquire", "white_bed"),
    "minecraft:adventure/shoot_arrow": ("acquire", "bow"),
    "minecraft:adventure/ol_betsy": ("acquire", "crossbow"),
    "minecraft:adventure/spyglass_at_parrot": ("acquire", "spyglass"),
    "minecraft:adventure/lightning_rod_with_villager_no_fire": ("acquire", "lightning_rod"),
    "minecraft:adventure/brush_armadillo": ("acquire", "brush"),
    "minecraft:adventure/crafters_crafting_crafters": ("acquire", "crafter"),
    "minecraft:husbandry/plant_seed": ("acquire", "wheat_seeds"),
    "minecraft:husbandry/make_a_sign_glow": ("acquire", "oak_sign"),
    "minecraft:husbandry/balanced_diet": ("acquire", "bread"),
    "minecraft:husbandry/obtain_netherite_hoe": ("acquire", "diamond_hoe"),
    "minecraft:husbandry/axolotl_in_a_bucket": ("acquire", "water_bucket"),
    "minecraft:husbandry/tactical_fishing": ("acquire", "water_bucket"),
    "minecraft:husbandry/wax_on": ("acquire", "copper_block"),
    "minecraft:nether/obtain_crying_obsidian": ("acquire", "obsidian"),
    "minecraft:nether/distract_piglin": ("acquire", "gold_ingot"),
    "minecraft:nether/use_lodestone": ("acquire", "compass"),
    "minecraft:nether/brew_potion": ("acquire", "glass_bottle"),
    "minecraft:nether/ride_strider": ("acquire", "fishing_rod"),
    "minecraft:nether/get_wither_skull": ("acquire", "iron_sword"),
    "minecraft:nether/obtain_blaze_rod": ("acquire", "iron_sword"),
    "minecraft:nether/charge_respawn_anchor": ("acquire", "obsidian"),
    "minecraft:adventure/kill_a_mob": ("acquire", "stone_sword"),
    "minecraft:adventure/trade": ("acquire", "emerald"),
    "minecraft:adventure/sniper_duel": ("acquire", "bow"),
    "minecraft:adventure/honey_block_slide": ("acquire", "glass_bottle"),
    "minecraft:story/follow_ender_eye": ("acquire", "ender_eye"),
    "minecraft:story/enter_the_end": ("enter_nether", "the_nether"),
}


class TaskCatalogue:
    """Unified DAG catalogue compiling data/recipes.json (886) + mc_tree_crafting.json + smelt + gather."""

    def __init__(self, *, repo_root: Optional[Path] = None) -> None:
        self.repo_root = repo_root or _REPO_ROOT
        self.recipes: Dict[str, List[RecipeEntry]] = {}
        self.smelting: Dict[str, SmeltEntry] = dict(SMELT_TABLE)
        self.gathering: Dict[str, GatherEntry] = dict(GATHER_TABLE)
        self._load_recipes()

    def _load_recipes(self) -> None:
        recipes_path = self.repo_root / "data" / "recipes.json"
        if recipes_path.is_file():
            raw = json.loads(recipes_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for item_name, variants in raw.items():
                    if not isinstance(variants, list):
                        continue
                    parsed: List[RecipeEntry] = []
                    for v in variants:
                        if not isinstance(v, dict):
                            continue
                        res_info = v.get("result") or {}
                        out_cnt = int(res_info.get("count", 1))
                        grid_size = int(v.get("grid_size", 3))
                        needs_table = grid_size >= 3
                        counts: Dict[str, int] = {}
                        if isinstance(v.get("shape"), list):
                            for row in v["shape"]:
                                if isinstance(row, list):
                                    for cell in row:
                                        if isinstance(cell, str) and cell:
                                            counts[cell] = counts.get(cell, 0) + 1
                        elif isinstance(v.get("ingredients"), list):
                            for ing in v["ingredients"]:
                                if isinstance(ing, str) and ing:
                                    counts[ing] = counts.get(ing, 0) + 1
                        if counts:
                            parsed.append(RecipeEntry(item_name, counts, max(1, out_cnt), needs_table))
                    if parsed:
                        self.recipes[item_name] = parsed

        # Merge data/mc_tree_crafting.json for any additional entries or simpler canonical variants
        tree_path = self.repo_root / "data" / "mc_tree_crafting.json"
        if tree_path.is_file():
            raw_tree = json.loads(tree_path.read_text(encoding="utf-8"))
            if isinstance(raw_tree, dict):
                for item_name, spec in raw_tree.items():
                    if not isinstance(spec, list) or len(spec) < 3 or not isinstance(spec[0], dict):
                        continue
                    ing = {str(k): int(v) for k, v in spec[0].items() if int(v) > 0}
                    entry = RecipeEntry(item_name, ing, max(1, int(spec[1])), bool(spec[2]))
                    if item_name not in self.recipes:
                        self.recipes[item_name] = [entry]

    def get_best_recipe(self, item: str, inventory_counts: Optional[Dict[str, int]] = None) -> Optional[RecipeEntry]:
        """Select the recipe variant with the lowest inventory-aware cost (preferring owned logs/planks & oak)."""
        variants = self.recipes.get(item)
        if not variants:
            return None
        inv = inventory_counts or {}

        def _has_precursor(ing: str) -> bool:
            if inv.get(ing, 0) > 0:
                return True
            if ing.endswith("_planks"):
                log_name = ing.replace("_planks", "_log")
                if inv.get(log_name, 0) > 0:
                    return True
            return False

        def score(rec: RecipeEntry) -> Tuple[int, int, int, int, int]:
            unproducible = sum(
                0 if (inv.get(ing, 0) > 0 or self.can_produce(ing)) else 1
                for ing in rec.ingredients
            )
            missing_direct = sum(max(0, req - inv.get(ing, 0)) for ing, req in rec.ingredients.items())
            missing_precursor = sum(0 if _has_precursor(ing) else 1 for ing in rec.ingredients)
            non_basic_penalty = sum(
                1 for ing in rec.ingredients
                if any(
                    w in ing
                    for w in (
                        "blackstone", "deepslate", "birch", "spruce", "crimson", "warped",
                        "bamboo", "cherry", "mangrove", "pale_oak", "dark_oak", "acacia", "jungle",
                    )
                )
            )
            return (unproducible, missing_precursor, missing_direct, non_basic_penalty, len(rec.ingredients))

        return min(variants, key=score)

    def can_produce(self, item: str, *, _seen: Optional[set[str]] = None) -> bool:
        """Check whether an item is producible through gathering, smelting, fluids, or recipes."""
        if item in {"water_bucket", "lava_bucket"}:
            return self.can_produce("bucket", _seen=_seen)
        if item in self.gathering:
            return True
        seen = set() if _seen is None else set(_seen)
        if item in seen:
            return False
        seen.add(item)
        if item in self.smelting:
            return self.can_produce(self.smelting[item].input_item, _seen=seen)
        variants = self.recipes.get(item)
        if not variants:
            return False
        for rec in variants:
            if all(self.can_produce(ing, _seen=seen) for ing in rec.ingredients):
                return True
        return False

    def advancement_route(self, adv_id: str) -> Optional[Tuple[str, str]]:
        return ADVANCEMENT_GOAL_ROUTES.get(adv_id)

    def estimate_steps(self, item: str, count: int, inventory_counts: Dict[str, int], *, _depth: int = 0) -> int:
        """Estimate remaining primitive steps for HorizonSelector cost ordering."""
        if _depth > 8:
            return 50
        have = inventory_counts.get(item, 0)
        need = max(0, count - have)
        if need == 0:
            return 0
        if item in {"water_bucket", "lava_bucket"}:
            return self.estimate_steps("bucket", need, inventory_counts, _depth=_depth + 1) + 3
        if item in self.gathering:
            g = self.gathering[item]
            tool_cost = 0
            if g.min_pickaxe and inventory_counts.get(g.min_pickaxe, 0) < 1:
                tool_cost = self.estimate_steps(g.min_pickaxe, 1, inventory_counts, _depth=_depth + 1)
            return tool_cost + need * 2
        if item in self.smelting:
            sm = self.smelting[item]
            return (
                self.estimate_steps("furnace", 1, inventory_counts, _depth=_depth + 1)
                + self.estimate_steps(sm.input_item, need, inventory_counts, _depth=_depth + 1)
                + 2
            )
        rec = self.get_best_recipe(item, inventory_counts)
        if rec is None:
            return 40
        batches = (need + rec.output_count - 1) // rec.output_count
        cost = 1
        if rec.needs_table and inventory_counts.get("crafting_table", 0) < 1:
            cost += 2
        for ing, qty in rec.ingredients.items():
            cost += self.estimate_steps(ing, qty * batches, inventory_counts, _depth=_depth + 1)
        return cost
