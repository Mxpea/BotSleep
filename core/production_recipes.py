"""Inventory-based recipe planning for bounded autonomous production."""

from dataclasses import dataclass
from math import ceil
import json
from pathlib import Path

from core.domain import PLANK_NAMES, WOOD_SPECIES
from skills.daily_survival import DailySurvivalSkills


RECIPES = {
    "crafting_table": ({"planks": 4}, 1), "stick": ({"planks": 2}, 4),
    "furnace": ({"cobblestone": 8}, 1), "chest": ({"planks": 8}, 1),
    "torch": ({"coal": 1, "stick": 1}, 4), "bread": ({"wheat": 3}, 1),
    "bone_meal": ({"bone": 1}, 3), "diamond_boots": ({"diamond": 4}, 1),
    "shield": ({"planks": 6, "iron_ingot": 1}, 1), "bucket": ({"iron_ingot": 3}, 1),
    "flint_and_steel": ({"iron_ingot": 1, "flint": 1}, 1),
    "blaze_powder": ({"blaze_rod": 1}, 2),
    "ender_eye": ({"blaze_powder": 1, "ender_pearl": 1}, 1),
    "bow": ({"stick": 3, "string": 3}, 1), "arrow": ({"flint": 1, "stick": 1, "feather": 1}, 4),
    **{f"{tier}_{tool}": ({material: quantity, "stick": sticks}, 1)
       for tier, material in (("wooden", "planks"), ("stone", "cobblestone"), ("iron", "iron_ingot"), ("diamond", "diamond"))
       for tool, quantity, sticks in (("pickaxe", 3, 2), ("axe", 3, 2), ("sword", 2, 1), ("hoe", 2, 2))},
    **{f"iron_{armor}": ({"iron_ingot": count}, 1)
       for armor, count in (("helmet", 5), ("chestplate", 8), ("leggings", 7), ("boots", 4))},
    **{f"{wood}_planks": ({f"{wood}_log": 1}, 4) for wood in WOOD_SPECIES},
    **{f"{wood}_door": ({f"{wood}_planks": 6}, 3) for wood in WOOD_SPECIES},
    **{bed: ({bed[:-4] + "_wool": 3, "planks": 3}, 1) for bed in DailySurvivalSkills.BEDS},
}

HAND_RECIPES = frozenset({"stick", "torch", "crafting_table", "flint_and_steel", "blaze_powder", "ender_eye", "bone_meal", *PLANK_NAMES})
# Keep established acquisition choices (e.g. smelt iron instead of unpacking blocks).
_catalog = json.loads((Path(__file__).resolve().parents[1] / "data" / "mc_tree_crafting.json").read_text(encoding="utf-8"))
for _item, (_materials, _yield, _table) in _catalog.items():
    if _item not in RECIPES:
        RECIPES[_item] = (_materials, _yield)
        if not _table:
            HAND_RECIPES |= {_item}
CRAFT_ITEMS = frozenset(RECIPES)
MAX_PLANNED_BATCHES = 4


@dataclass(frozen=True)
class CraftPlan:
    needs_table: bool
    request_count: int = 0
    deficits: tuple[tuple[str, int], ...] = ()


def wood_inputs(wood):
    return (f"{wood}_log", f"{wood}_wood", f"stripped_{wood}_log", f"stripped_{wood}_wood")


def ingredient_items(ingredient, output):
    if ingredient == "planks":
        return PLANK_NAMES
    if output in PLANK_NAMES:
        return wood_inputs(output[:-7])
    if output == "torch" and ingredient == "coal":
        return ("coal", "charcoal")
    return (ingredient,)


def plan_craft(item, needed, counts):
    """Plan a small window of ingredients and execute only what is observed in hand."""
    recipe, output = RECIPES[item]
    needs_table = item not in HAND_RECIPES
    batches = min(MAX_PLANNED_BATCHES, ceil(needed / output))
    available = {ingredient: sum(counts.get(name, 0) for name in ingredient_items(ingredient, item))
                 for ingredient in recipe}
    ready = min(available[ingredient] // quantity for ingredient, quantity in recipe.items())
    if ready > 0:
        # Table calls can open a window for every recipe. Bound one such call
        # to a single recipe before status, inventory and threats are checked again.
        size = min(batches, ready, 1 if needs_table else MAX_PLANNED_BATCHES)
        return CraftPlan(needs_table, min(needed, size * output))
    return CraftPlan(needs_table, deficits=tuple(
        (name, batches * quantity - available[name]) for name, quantity in recipe.items()
        if available[name] < quantity))
