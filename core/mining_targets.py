"""Pure target parsing, ore-to-drop mapping, tool-tier checks, and home-trigger evaluation."""

import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from core.structure_spec import ORE_DROP_MAP, ORE_MIN_TOOL, SCAFFOLD_BLOCKS, TOOL_MAX_DURABILITY

PICKAXE_TIERS: Dict[str, int] = {
    "wooden_pickaxe": 1,
    "golden_pickaxe": 1,
    "stone_pickaxe": 2,
    "iron_pickaxe": 3,
    "diamond_pickaxe": 4,
    "netherite_pickaxe": 5,
}

TARGET_ALIASES: Dict[str, str] = {
    "iron": "iron",
    "raw_iron": "iron",
    "iron_ingot": "iron",
    "iron_ore": "iron",
    "deepslate_iron_ore": "iron",
    "coal": "coal",
    "coal_ore": "coal",
    "deepslate_coal_ore": "coal",
    "copper": "copper",
    "raw_copper": "copper",
    "copper_ingot": "copper",
    "copper_ore": "copper",
    "deepslate_copper_ore": "copper",
    "gold": "gold",
    "raw_gold": "gold",
    "gold_ingot": "gold",
    "gold_ore": "gold",
    "deepslate_gold_ore": "gold",
    "nether_gold_ore": "gold",
    "redstone": "redstone",
    "redstone_ore": "redstone",
    "deepslate_redstone_ore": "redstone",
    "lapis": "lapis",
    "lapis_lazuli": "lapis",
    "lapis_ore": "lapis",
    "deepslate_lapis_ore": "lapis",
    "diamond": "diamond",
    "diamond_ore": "diamond",
    "deepslate_diamond_ore": "diamond",
    "emerald": "emerald",
    "emerald_ore": "emerald",
    "deepslate_emerald_ore": "emerald",
    "quartz": "quartz",
    "nether_quartz_ore": "quartz",
}

TARGET_DROP_ITEM: Dict[str, str] = {
    "iron": "raw_iron",
    "coal": "coal",
    "copper": "raw_copper",
    "gold": "raw_gold",
    "redstone": "redstone",
    "lapis": "lapis_lazuli",
    "diamond": "diamond",
    "emerald": "emerald",
    "quartz": "quartz",
}

DEFAULT_MINING_TARGETS: Dict[str, Dict[str, Any]] = {
    "iron": {"count": 24, "min_tool": "stone_pickaxe"},
    "coal": {"count": 16, "min_tool": "wooden_pickaxe"},
}

DEFAULT_HOME_THRESHOLDS: Dict[str, Any] = {
    "min_empty_slots": 3,
    "min_health": 12.0,
    "min_food": 10.0,
    "min_torches": 4,
    "min_blocks": 16,
    "min_pickaxe_durability_ratio": 0.10,
    "min_remaining_seconds": 30.0,
}

DEFAULT_PLACEABLE_BLOCKS: Tuple[str, ...] = tuple(SCAFFOLD_BLOCKS) + (
    "stone",
    "andesite",
    "diorite",
    "granite",
    "tuff",
    "deepslate",
)

DEFAULT_EDIBLE_FOODS: Tuple[str, ...] = (
    "cooked_beef",
    "cooked_porkchop",
    "cooked_mutton",
    "cooked_chicken",
    "cooked_salmon",
    "cooked_cod",
    "baked_potato",
    "bread",
    "apple",
    "carrot",
    "melon_slice",
    "sweet_berries",
    "golden_carrot",
    "beef",
    "porkchop",
    "chicken",
    "mutton",
)


def pickaxe_tier(tool_name: Optional[str]) -> int:
    """Return integer harvest tier for a pickaxe name, or 0 if unknown/non-pickaxe."""
    if not isinstance(tool_name, str):
        return 0
    return PICKAXE_TIERS.get(tool_name.strip().lower(), 0)


def pickaxe_durability_ratio(item_entry: Mapping[str, Any]) -> Optional[float]:
    """Compute remaining durability fraction [0.0, 1.0] if durability_used is exposed."""
    name = str(item_entry.get("name") or "")
    if pickaxe_tier(name) <= 0:
        return None
    used = item_entry.get("durability_used")
    if not isinstance(used, (int, float)) or isinstance(used, bool) or not math.isfinite(used):
        return None
    prefix = name.split("_", 1)[0]
    max_dur = TOOL_MAX_DURABILITY.get(prefix)
    if not max_dur or max_dur <= 0:
        return None
    remaining = max(0.0, float(max_dur) - float(used))
    return remaining / float(max_dur)


def ore_target_key(block_name: str) -> Optional[str]:
    """Map an ore block name (e.g. deepslate_iron_ore) to canonical target key ('iron')."""
    if not isinstance(block_name, str):
        return None
    norm = block_name.strip().lower()
    if norm in TARGET_ALIASES:
        return TARGET_ALIASES[norm]
    drop = ORE_DROP_MAP.get(norm)
    if drop:
        return TARGET_ALIASES.get(drop, drop)
    return None


def parse_mining_targets(raw_targets: Optional[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Normalize configured mining.targets mapping into canonical per-target specs."""
    if not raw_targets:
        return {}
    parsed: Dict[str, Dict[str, Any]] = {}
    for raw_key, raw_spec in raw_targets.items():
        if not isinstance(raw_key, str):
            continue
        key = TARGET_ALIASES.get(raw_key.strip().lower(), raw_key.strip().lower())
        drop_item = TARGET_DROP_ITEM.get(key)
        if drop_item is None:
            drop_item = ORE_DROP_MAP.get(raw_key.strip().lower(), key)
        ore_blocks = tuple(sorted(ore for ore, drop in ORE_DROP_MAP.items() if drop == drop_item))
        if isinstance(raw_spec, int) and not isinstance(raw_spec, bool):
            count = max(0, int(raw_spec))
            min_tool = min(
                (ORE_MIN_TOOL.get(b, "wooden_pickaxe") for b in ore_blocks),
                key=pickaxe_tier,
                default="wooden_pickaxe",
            )
        elif isinstance(raw_spec, Mapping):
            raw_count = raw_spec.get("count", 1)
            count = max(0, int(raw_count)) if isinstance(raw_count, (int, float)) and not isinstance(raw_count, bool) else 1
            default_tool = min(
                (ORE_MIN_TOOL.get(b, "wooden_pickaxe") for b in ore_blocks),
                key=pickaxe_tier,
                default="wooden_pickaxe",
            )
            min_tool = str(raw_spec.get("min_tool") or default_tool).strip().lower()
        else:
            continue
        if count <= 0:
            continue
        parsed[key] = {
            "target_key": key,
            "count": count,
            "min_tool": min_tool,
            "min_tier": pickaxe_tier(min_tool),
            "drop_item": drop_item,
            "ore_blocks": ore_blocks,
        }
    return parsed


def best_usable_pickaxe(
    inventory_items: Sequence[Mapping[str, Any]],
    *,
    min_durability_ratio: float = 0.0,
) -> Optional[str]:
    """Return the highest-tier usable pickaxe name from inventory items."""
    best_name: Optional[str] = None
    best_rank = (-1, -1.0)
    for entry in inventory_items:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name") or "")
        count = entry.get("count", 1)
        if not isinstance(count, (int, float)) or isinstance(count, bool) or count <= 0:
            continue
        tier = pickaxe_tier(name)
        if tier <= 0:
            continue
        ratio = pickaxe_durability_ratio(entry)
        if ratio is not None and ratio < float(min_durability_ratio):
            continue
        rank = (tier, 1.0 if ratio is None else ratio)
        if rank > best_rank:
            best_rank = rank
            best_name = name
    return best_name


def evaluate_ore_block(
    block_name: str,
    targets: Mapping[str, Mapping[str, Any]],
    *,
    harvested_counts: Mapping[str, int],
    best_tool_tier: int,
) -> Tuple[str, Optional[str], Optional[str]]:
    """Classify an exposed ore block against targets, quota, and tool tier.

    Returns (action_or_status, target_key, drop_item) where action_or_status is one of:
    'not_ore', 'mine', 'skipped_quota', 'skipped_tool', 'seen'.
    """
    norm = str(block_name or "").strip().lower()
    if norm not in ORE_DROP_MAP:
        return ("not_ore", None, None)
    drop_item = ORE_DROP_MAP[norm]
    key = ore_target_key(norm)
    if key is None or key not in targets:
        return ("seen", key, drop_item)
    spec = targets[key]
    current = int(harvested_counts.get(drop_item, 0))
    if current >= int(spec.get("count", 0)):
        return ("skipped_quota", key, drop_item)
    block_req = pickaxe_tier(ORE_MIN_TOOL.get(norm, "wooden_pickaxe"))
    config_req = int(spec.get("min_tier", pickaxe_tier(spec.get("min_tool"))))
    required_tier = max(block_req, config_req)
    if int(best_tool_tier) < required_tier:
        return ("skipped_tool", key, drop_item)
    return ("mine", key, drop_item)


def estimate_empty_main_slots(inventory_items: Sequence[Mapping[str, Any]], *, total_main_slots: int = 36) -> int:
    """Estimate free main-inventory slots without double-counting stacked entries."""
    used_slots = set()
    counted_entries = 0
    for entry in inventory_items:
        if not isinstance(entry, Mapping):
            continue
        count = entry.get("count", 0)
        if not isinstance(count, (int, float)) or isinstance(count, bool) or count <= 0:
            continue
        slot = entry.get("slot")
        if isinstance(slot, int) and not isinstance(slot, bool):
            # Ignore armor/offhand slots outside main inventory (9..44 in Mineflayer window)
            if 9 <= slot <= 44 or slot < 36:
                used_slots.add(slot)
                continue
        counted_entries += 1
    used = len(used_slots) + counted_entries
    return max(0, int(total_main_slots) - used)


def evaluate_home_trigger(
    status: Mapping[str, Any],
    inventory_items: Sequence[Mapping[str, Any]],
    *,
    remaining_seconds: Optional[float] = None,
    thresholds: Optional[Mapping[str, Any]] = None,
    edible_foods: Iterable[str] = DEFAULT_EDIBLE_FOODS,
) -> Dict[str, Any]:
    """Evaluate Plan v2 checkpoint rest/wrap-up rules."""
    th = dict(DEFAULT_HOME_THRESHOLDS)
    if thresholds:
        for k, v in thresholds.items():
            if k in th and v is not None:
                th[k] = v

    if remaining_seconds is not None and float(remaining_seconds) < float(th["min_remaining_seconds"]):
        return {
            "trigger": False,
            "wrap_up": True,
            "home_reason": None,
            "remaining_seconds": float(remaining_seconds),
        }

    counts: Dict[str, int] = {}
    for entry in inventory_items:
        if isinstance(entry, Mapping) and isinstance(entry.get("name"), str):
            c = entry.get("count", 0)
            if isinstance(c, (int, float)) and not isinstance(c, bool) and c > 0:
                counts[entry["name"]] = counts.get(entry["name"], 0) + int(c)

    empty_slots = estimate_empty_main_slots(inventory_items)
    health = status.get("health")
    food = status.get("food")
    has_edible = any(counts.get(f, 0) > 0 for f in edible_foods)
    torch_count = counts.get("torch", 0)
    block_count = sum(counts.get(b, 0) for b in DEFAULT_PLACEABLE_BLOCKS)
    usable_pick = best_usable_pickaxe(
        inventory_items,
        min_durability_ratio=float(th["min_pickaxe_durability_ratio"]),
    )

    reason: Optional[str] = None
    if isinstance(health, (int, float)) and not isinstance(health, bool) and float(health) < float(th["min_health"]):
        reason = "low_health"
    elif (
        isinstance(food, (int, float))
        and not isinstance(food, bool)
        and float(food) < float(th["min_food"])
        and not has_edible
    ):
        reason = "low_food"
    elif usable_pick is None:
        reason = "no_pickaxe"
    elif empty_slots <= int(th["min_empty_slots"]):
        reason = "inventory_full"
    elif torch_count < int(th["min_torches"]):
        reason = "low_torch"
    elif block_count < int(th["min_blocks"]):
        reason = "low_blocks"

    return {
        "trigger": reason is not None,
        "wrap_up": False,
        "home_reason": reason,
        "empty_slots": empty_slots,
        "torch_count": torch_count,
        "block_count": block_count,
        "usable_pickaxe": usable_pick,
    }
