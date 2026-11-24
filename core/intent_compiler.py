"""Deterministic model intent compiler and local-workstation resolution helpers.

Bridges loose small-model tool outputs and strict local skill contracts:
1. Canonicalizes singular/plural names, ingredient->output smelting requests,
   and field swaps (`item` vs `resource`) before schema validation.
2. Filters prompt advice (`playbook.next_actions`) so the model is never told to
   call tools hidden from the active turn's `tools` catalog.
3. Enforces L2 `blocked_task_signatures` cooldowns and speedrun smelting focus.
4. Decides when crafting/smelting should build a local workstation instead of
   trekking across steep elevation gaps or unsafe cave routes to a stale one.
"""

import copy
import math
from typing import Any, Optional

from core.domain import LOG_NAMES, PLANK_NAMES, extract_blocks, inventory_counts, parse_pos


# Deterministic plural / colloquial aliases emitted by small LLMs.
_ITEM_ALIASES: dict[str, str] = {
    "sticks": "stick",
    "torches": "torch",
    "cobble": "cobblestone",
    "cobblestones": "cobblestone",
    "buckets": "bucket",
    "shields": "shield",
    "furnaces": "furnace",
    "crafting_tables": "crafting_table",
    "beds": "white_bed",
    "iron_ingots": "iron_ingot",
    "charcoals": "charcoal",
    "apples": "apple",
    "carrots": "carrot",
    "potatoes": "potato",
    "breads": "bread",
    "wooden_pick": "wooden_pickaxe",
    "stone_pick": "stone_pickaxe",
    "iron_pick": "iron_pickaxe",
    "diamond_pick": "diamond_pickaxe",
    "flint_steel": "flint_and_steel",
}

_GATHER_ALIASES: dict[str, str] = {
    "log": "logs",
    "oak_log": "logs",
    "birch_log": "logs",
    "spruce_log": "logs",
    "jungle_log": "logs",
    "acacia_log": "logs",
    "dark_oak_log": "logs",
    "mangrove_log": "logs",
    "cherry_log": "logs",
    "wood": "logs",
    "iron": "raw_iron",
    "iron_ore": "raw_iron",
    "deepslate_iron_ore": "raw_iron",
    "cobble": "cobblestone",
    "stone": "cobblestone",
    "coal_ore": "coal",
    "diamond_ore": "diamond",
}

_SMELT_INPUT_TO_OUTPUT: dict[str, str] = {
    "raw_iron": "iron_ingot",
    "iron_ore": "iron_ingot",
    "deepslate_iron_ore": "iron_ingot",
    "iron": "iron_ingot",
    "log": "charcoal",
    "logs": "charcoal",
    **{name: "charcoal" for name in LOG_NAMES},
}


def resolve_plank_species(counts: dict[str, int]) -> str:
    """Pick the best matching plank item name based on currently held logs or planks."""
    for plank in PLANK_NAMES:
        if counts.get(plank, 0) > 0:
            return plank
    for log_name in LOG_NAMES:
        if counts.get(log_name, 0) > 0 and log_name.endswith("_log"):
            candidate = log_name[:-4] + "_planks"
            if candidate in PLANK_NAMES:
                return candidate
    return "oak_planks"


class IntentCompiler:
    """Compile and normalize LLM tool calls and context before schema/policy checks."""

    def __init__(self, *, intent: str = "survival", smelt_items: Optional[frozenset[str]] = None) -> None:
        self.intent = str(intent or "survival")
        self.smelt_items = smelt_items or frozenset()

    def compile_tool_call(
        self,
        name: str,
        raw_args: dict[str, Any],
        observation: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], Optional[tuple[str, str]]]:
        """Normalize raw model tool arguments and return `(args, error_tuple_or_None)`.

        Args:
            name: Native function name chosen by the LLM.
            raw_args: Parsed JSON object supplied for the function call.
            observation: Current observation dict (used for counts / plank species resolution).
            context: Active harness context (used for L2 `blocked_task_signatures` checks).

        Returns:
            `(compiled_args, error)`: when `error` is not None it is `(error_code, message)`.
        """
        args = copy.deepcopy(raw_args) if isinstance(raw_args, dict) else {}
        counts = inventory_counts(observation) if isinstance(observation, dict) else {}

        if name == "craft_item" and isinstance(args.get("item"), str):
            item = args["item"].strip().lower()
            if item in {"plank", "planks"}:
                args["item"] = resolve_plank_species(counts)
            elif item in _ITEM_ALIASES:
                args["item"] = _ITEM_ALIASES[item]
            return args, None

        if name != "run_survival_task":
            return args, None

        task = args.get("task")
        # Normalize legacy board key `wood_reserve` into bounded gather goal.
        if task == "wood_reserve" and args.get("resource", "logs") == "logs" and "item" not in args:
            args = {**args, "task": "gather", "resource": "logs"}
            task = "gather"

        # Normalize `gather(item=...)` into `gather(resource=...)`.
        if task == "gather" and "resource" not in args and isinstance(args.get("item"), str):
            raw_res = args.pop("item").strip().lower()
            args["resource"] = _GATHER_ALIASES.get(raw_res, raw_res)
        elif task == "gather" and isinstance(args.get("resource"), str):
            raw_res = args["resource"].strip().lower()
            args["resource"] = _GATHER_ALIASES.get(raw_res, raw_res)

        # Normalize `craft` singulars and plank species.
        if task == "craft":
            if "item" not in args and isinstance(args.get("resource"), str):
                args["item"] = args.pop("resource")
            if isinstance(args.get("item"), str):
                item = args["item"].strip().lower()
                if item in {"plank", "planks"}:
                    args["item"] = resolve_plank_species(counts)
                elif item in _ITEM_ALIASES:
                    args["item"] = _ITEM_ALIASES[item]
                else:
                    args["item"] = item

        # Normalize `smelt` ingredients (`raw_iron` -> `iron_ingot`) and check speedrun focus.
        if task == "smelt":
            if "item" not in args and isinstance(args.get("resource"), str):
                args["item"] = args.pop("resource")
            if isinstance(args.get("item"), str):
                item = args["item"].strip().lower()
                item = _SMELT_INPUT_TO_OUTPUT.get(item, _ITEM_ALIASES.get(item, item))
                args["item"] = item
                if self.intent == "speedrun" and item in {"copper_ingot", "gold_ingot"}:
                    return args, (
                        "llm_invalid_arguments",
                        f"Speedrun mode skips smelting '{item}'. Call smelt(item='iron_ingot') for bucket/flint_and_steel/iron_pickaxe, smelt(item='charcoal'), or cooked meat.",
                    )
                if self.smelt_items and item not in self.smelt_items:
                    return args, (
                        "llm_invalid_arguments",
                        f"smelt does not produce '{item}'. Valid smelt items include: iron_ingot, charcoal, cooked_beef, cooked_porkchop, cooked_mutton, cooked_chicken, baked_potato.",
                    )

        # Enforce L2 repeated-signature cooldown before wasting another turn.
        blocked_sigs = context.get("blocked_task_signatures") if isinstance(context, dict) else None
        if isinstance(blocked_sigs, dict) and isinstance(task, str):
            target = str(args.get("item") or args.get("resource") or "")
            sig = f"run_survival_task:{task}:{target}"
            if sig in blocked_sigs:
                return args, (
                    "llm_repeated_failure",
                    f"Goal '{sig}' recently failed repeatedly without inventory change; pick a different prerequisite, gather/craft target, or repair action.",
                )

        return args, None

    @staticmethod
    def filter_prompt_context(
        prompt_context: dict[str, Any],
        allowed_names: set[str],
    ) -> None:
        """Align `playbook` and `action_hints` in `prompt_context` with visible `tools`."""
        pb = prompt_context.get("playbook")
        if isinstance(pb, dict):
            next_actions = pb.get("next_actions")
            if isinstance(next_actions, list):
                filtered = [
                    row for row in next_actions
                    if isinstance(row, dict) and row.get("name") in allowed_names
                ]
                if len(filtered) != len(next_actions):
                    pb_copy = dict(pb)
                    pb_copy["next_actions"] = filtered
                    pb_copy["bindings"] = sorted({row["name"] for row in filtered})
                    prompt_context["playbook"] = pb_copy


def should_prefer_local_workstation(
    station_name: str,
    observation: dict[str, Any],
    context: Optional[dict[str, Any]] = None,
    *,
    max_horiz: float = 16.0,
    max_dy: float = 4.0,
) -> bool:
    """Return True when building/placing a local workstation beats trekking to a distant/high one.

    For example, if the bot is deep underground (`y=16` or `y=69`) while its only
    seen crafting_table is 11+ blocks vertically overhead on the surface, walking back
    to the surface fails with `unsafe_route`/`path_not_found`. Whenever no station is
    safely reachable nearby AND the bot holds the ingredients (or item) to make one,
    crafting/placing locally avoids getting stranded.
    """
    if station_name not in {"crafting_table", "furnace"}:
        return False
    status = observation.get("status") or {}
    origin = parse_pos(status.get("pos"))
    if origin is None:
        return False
    counts = inventory_counts(observation)
    blocked = set((context or {}).get("blocked_targets") or [])

    def _key(p: list[float]) -> str:
        return f"{int(math.floor(p[0]))},{int(math.floor(p[1]))},{int(math.floor(p[2]))}"

    for row in extract_blocks(observation):
        if row.get("name") != station_name:
            continue
        pos = parse_pos(row.get("pos"))
        if pos is None or _key(pos) in blocked:
            continue
        horiz = math.hypot(pos[0] - origin[0], pos[2] - origin[2])
        dy = abs(pos[1] - origin[1])
        if horiz <= max_horiz and dy <= max_dy:
            return False  # A safe nearby workstation exists on this level.

    if station_name == "crafting_table":
        if counts.get("crafting_table", 0) >= 1:
            return True
        plank_total = sum(counts.get(n, 0) for n in PLANK_NAMES)
        log_total = sum(counts.get(n, 0) for n in LOG_NAMES)
        return plank_total >= 4 or log_total >= 1

    if station_name == "furnace":
        return counts.get("furnace", 0) >= 1 or counts.get("cobblestone", 0) >= 8

    return False
