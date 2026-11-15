"""Minecraft Recipe Manager and Crafting Planner

Provides lookup, 2x2 vs 3x3 dimension classification, ingredient resolution,
and hierarchical crafting prerequisite resolution.
"""

import json
import logging
import os
from typing import Dict, Any, List, Optional, Tuple, Set

logger = logging.getLogger("BotSleep.Recipes")

LOG_VARIANTS = {
    "oak_log", "spruce_log", "birch_log", "jungle_log", "acacia_log",
    "dark_oak_log", "mangrove_log", "cherry_log", "pale_oak_log",
    "crimson_stem", "warped_stem"
}

PLANK_VARIANTS = {
    "oak_planks", "spruce_planks", "birch_planks", "jungle_planks", "acacia_planks",
    "dark_oak_planks", "mangrove_planks", "cherry_planks", "pale_oak_planks",
    "crimson_planks", "warped_planks", "bamboo_planks"
}

LOG_TO_PLANK = {
    "oak_log": "oak_planks",
    "spruce_log": "spruce_planks",
    "birch_log": "birch_planks",
    "jungle_log": "jungle_planks",
    "acacia_log": "acacia_planks",
    "dark_oak_log": "dark_oak_planks",
    "mangrove_log": "mangrove_planks",
    "cherry_log": "cherry_planks",
    "pale_oak_log": "pale_oak_planks",
    "crimson_stem": "crimson_planks",
    "warped_stem": "warped_planks",
}


class RecipeBook:
    """Manages recipes loaded from recipes.json and calculates crafting requirements."""

    def __init__(self, recipe_file: Optional[str] = None):
        if not recipe_file:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            recipe_file = os.path.join(base_dir, "data", "recipes.json")
        self.recipe_file = recipe_file
        self.recipes: Dict[str, List[Dict[str, Any]]] = {}
        self.load()

    def load(self) -> None:
        """Loads recipes from the local JSON registry."""
        if not os.path.exists(self.recipe_file):
            logger.warning(f"Recipe file not found at {self.recipe_file}. Empty recipe book.")
            return

        try:
            with open(self.recipe_file, "r", encoding="utf-8") as f:
                self.recipes = json.load(f)
            logger.info(f"Loaded {len(self.recipes)} items from {self.recipe_file}")
        except Exception as e:
            logger.error(f"Failed to load recipes from {self.recipe_file}: {e}")

    def has_recipe(self, item_name: str) -> bool:
        """Checks if a recipe exists for the item."""
        return item_name in self.recipes and len(self.recipes[item_name]) > 0

    def get_recipes(self, item_name: str) -> List[Dict[str, Any]]:
        """Returns all recipe variants for this item."""
        return self.recipes.get(item_name, [])

    def is_3x3(self, item_name: str) -> bool:
        """Determines whether crafting this item strictly requires a 3x3 crafting table grid."""
        variants = self.get_recipes(item_name)
        if not variants:
            # Common 3x3 defaults fallback
            return item_name.endswith(("_pickaxe", "_axe", "_sword", "_shovel", "_hoe", "_helmet", "_chestplate", "_leggings", "_boots", "_door")) or item_name in {"chest", "furnace", "shield", "bow", "crossbow", "boat"}

        # If any recipe variant fits within 2x2, it does NOT strictly need a 3x3 table
        has_2x2 = any(v.get("grid_size", 3) <= 2 for v in variants)
        return not has_2x2

    def can_craft_in_inventory(self, item_name: str) -> bool:
        """Checks if the item can be crafted in the 2x2 player inventory."""
        return not self.is_3x3(item_name)

    def resolve_wood_type(self, inventory: Dict[str, int]) -> Tuple[str, str]:
        """Detects available log and plank types in inventory.
        
        Returns (best_log, best_plank).
        """
        # Check existing planks
        for plank in PLANK_VARIANTS:
            if inventory.get(plank, 0) > 0:
                log = plank.replace("_planks", "_log")
                return log, plank

        # Check existing logs
        for log in LOG_VARIANTS:
            if inventory.get(log, 0) > 0:
                plank = LOG_TO_PLANK.get(log, "oak_planks")
                return log, plank

        return "oak_log", "oak_planks"

    def get_recipe_ingredients(self, item_name: str, preferred_plank: str = "oak_planks") -> Tuple[int, Dict[str, int]]:
        """Calculates standard ingredients required for 1 craft execution.
        
        Returns (output_count, {ingredient_name: count_needed}).
        """
        variants = self.get_recipes(item_name)
        if not variants:
            # Fallback heuristics for common items
            if item_name == "wooden_pickaxe":
                return 1, {preferred_plank: 3, "stick": 2}
            elif item_name == "wooden_axe":
                return 1, {preferred_plank: 3, "stick": 2}
            elif item_name == "wooden_sword":
                return 1, {preferred_plank: 2, "stick": 1}
            elif item_name == "crafting_table":
                return 1, {preferred_plank: 4}
            elif item_name == "stick":
                return 4, {preferred_plank: 2}
            elif item_name.endswith("_planks"):
                log_name = item_name.replace("_planks", "_log")
                return 4, {log_name: 1}
            return 1, {}

        # Prefer variant strictly matching preferred_plank
        chosen = variants[0]
        for v in variants:
            if "shape" in v:
                cells = [c for row in v["shape"] for c in row if c]
                if preferred_plank in cells:
                    chosen = v
                    break
            elif "ingredients" in v:
                if preferred_plank in v["ingredients"]:
                    chosen = v
                    break

        out_count = chosen.get("result", {}).get("count", 1)
        needed: Dict[str, int] = {}

        if "shape" in chosen:
            for row in chosen["shape"]:
                for cell in row:
                    if cell:
                        # Normalize any plank variant to preferred_plank if appropriate
                        norm_cell = preferred_plank if cell in PLANK_VARIANTS else cell
                        needed[norm_cell] = needed.get(norm_cell, 0) + 1
        elif "ingredients" in chosen:
            for ing in chosen["ingredients"]:
                if ing:
                    norm_ing = preferred_plank if ing in PLANK_VARIANTS else ing
                    needed[norm_ing] = needed.get(norm_ing, 0) + 1

        return out_count, needed

    def plan_recipe_chain(
        self,
        target_item: str,
        target_count: int,
        inventory: Dict[str, int],
        has_nearby_table: bool = False,
    ) -> List[Dict[str, Any]]:
        """Builds a sequence of crafting and world interaction steps to fulfill target_item.
        
        Returns a list of action steps:
          - {"type": "craft", "item": str, "count": int, "grid": "2x2" | "3x3"}
          - {"type": "place_table"}
          - {"type": "retrieve_table"}
        """
        steps: List[Dict[str, Any]] = []
        inv = dict(inventory)

        best_log, best_plank = self.resolve_wood_type(inv)
        is_3x3 = self.is_3x3(target_item)

        # Calculate wood and plank budget
        # Check table requirement
        needs_table_placement = is_3x3 and not has_nearby_table
        table_in_inv = inv.get("crafting_table", 0) > 0
        needs_table_craft = needs_table_placement and not table_in_inv

        # Get direct recipe requirements
        out_batch, direct_reqs = self.get_recipe_ingredients(target_item, preferred_plank=best_plank)
        craft_rounds = (target_count + out_batch - 1) // out_batch

        # Total plank requirement calculation
        direct_planks_needed = sum(
            count * craft_rounds
            for ing, count in direct_reqs.items()
            if ing == best_plank or ing in PLANK_VARIANTS
        )

        sticks_needed = sum(
            count * craft_rounds
            for ing, count in direct_reqs.items()
            if ing == "stick"
        )
        stick_deficit = max(0, sticks_needed - inv.get("stick", 0))
        stick_craft_rounds = (stick_deficit + 3) // 4 if stick_deficit > 0 else 0
        planks_for_sticks = stick_craft_rounds * 2

        planks_for_table = 4 if needs_table_craft else 0

        total_planks_required = direct_planks_needed + planks_for_sticks + planks_for_table
        current_planks_avail = sum(inv.get(p, 0) for p in PLANK_VARIANTS)
        planks_deficit = max(0, total_planks_required - current_planks_avail)

        if planks_deficit > 0:
            logs_needed = (planks_deficit + 3) // 4
            current_logs_avail = sum(inv.get(l, 0) for l in LOG_VARIANTS)
            if current_logs_avail < logs_needed:
                raise ValueError(
                    f"Insufficient wood logs to craft {target_item}. "
                    f"Need {logs_needed} logs for {planks_deficit} more planks, but only have {current_logs_avail} logs."
                )
            # Add batch plank craft step (each log yields 4 planks)
            steps.append({"type": "craft", "item": best_plank, "count": logs_needed * 4, "grid": "2x2"})
            inv[best_log] = inv.get(best_log, 0) - logs_needed
            inv[best_plank] = inv.get(best_plank, 0) + (logs_needed * 4)

        # Craft crafting table if needed
        if needs_table_craft:
            steps.append({"type": "craft", "item": "crafting_table", "count": 1, "grid": "2x2"})
            inv[best_plank] = inv.get(best_plank, 0) - 4
            inv["crafting_table"] = inv.get("crafting_table", 0) + 1

        # Craft sticks if needed (each batch of 2 planks yields 4 sticks)
        if stick_craft_rounds > 0:
            steps.append({"type": "craft", "item": "stick", "count": stick_craft_rounds * 4, "grid": "2x2"})
            inv[best_plank] = inv.get(best_plank, 0) - planks_for_sticks
            inv["stick"] = inv.get("stick", 0) + (stick_craft_rounds * 4)

        # Check other non-wood direct ingredients (e.g. cobblestone, iron_ingot)
        for ing, per_craft in direct_reqs.items():
            if ing in PLANK_VARIANTS or ing == "stick" or ing == best_plank:
                continue
            req_cnt = per_craft * craft_rounds
            if inv.get(ing, 0) < req_cnt:
                raise ValueError(f"Missing ingredient '{ing}': need {req_cnt}, have {inv.get(ing, 0)}")

        # Place crafting table if 3x3
        if needs_table_placement:
            steps.append({"type": "place_table"})

        # Craft target item
        grid_mode = "3x3" if is_3x3 else "2x2"
        steps.append({"type": "craft", "item": target_item, "count": target_count, "grid": grid_mode})

        # Retrieve crafting table
        if needs_table_placement:
            steps.append({"type": "retrieve_table"})

        return steps
