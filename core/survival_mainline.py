"""Ordered overworld milestones for a survival world without a dragon objective."""

import math

from agent.survival_planner import Decision
from core.domain import LOG_NAMES, PLANK_NAMES, SAFE_FOOD, parse_pos


CHAPTERS = (
    ("wooden_pickaxe", "采木、工作台与木镐"),
    ("stone_pickaxe", "取得三块圆石并制作石镐"),
    ("first_food", "准备第一批口粮"),
    ("shelter", "建成可关闭的住所"),
    ("starter_tools", "补齐石斧和石剑"),
    ("furnace", "制作并放置熔炉"),
    ("fuel_reserve", "储备煤或木炭"),
    ("torches", "制作第一批火把"),
    ("bed", "取得羊毛并在住所安床"),
    ("storage", "建立储物点"),
    ("iron_tools", "熔铁、制作盾牌、水桶和铁工具"),
    ("food_production", "建立可再生食物来源"),
    ("food_reserve", "补足日常口粮"),
    ("wood_reserve", "补足维修木材"),
    ("maintenance", "定期检查补给、工具与基地"),
)


def _task(name, **args):
    return {"task": name, **args}


def mainline_enabled(settings):
    options = settings.get("autonomy", {})
    return (settings.get("objective") == "daily" and settings.get("intent", "survival") == "survival"
            and options.get("enabled", True) and options.get("mainline") is True)


def choose_chapter(observation, board, settings, known_sites):
    """Return one bounded goal. Historical sites only avoid duplicate facilities."""
    status = observation["status"]
    origin = parse_pos(status.get("pos"))
    counts = observation["counts"]
    dim = status.get("dimension")
    rows = {row["id"]: row for row in board.get("tasks", [])}
    limits = settings.get("daily", {})
    food_limit = limits.get("food_reserve", 8)
    fuel_limit = limits.get("fuel_reserve", 8)
    wood_limit = limits.get("wood_reserve", 8)
    picks = ("wooden", "stone", "iron", "diamond", "netherite")
    pick_tier = max((i for i, tier in enumerate(picks) if counts.get(tier + "_pickaxe", 0)), default=-1)
    food = sum(counts.get(item, 0) for item in SAFE_FOOD)
    fuel = counts.get("coal", 0) + counts.get("charcoal", 0)
    wood = sum(counts.get(item, 0) for item in LOG_NAMES) + sum(counts.get(item, 0) for item in PLANK_NAMES) // 4
    # A verified permanent installation can be outside the current scan. This
    # memory can skip rebuilding it but never supplies coordinates for a write.
    facilities = {"shelter", "furnace", "bed", "storage", "food_production"}
    done = {key for key, row in rows.items() if row.get("status") == "satisfied"}
    for key in facilities:
        row = rows.get(key, {})
        proof = row.get("evidence") or {}
        positions = proof.get("positions") or []
        place = parse_pos(proof.get("pos") or (positions[0] if positions else None))
        if key in done and place is not None:
            known_sites[key] = {"pos": place, "dimension": dim}
        elif key not in done:
            remembered = known_sites.get(key) or {}
            pos = parse_pos(remembered.get("pos"))
            if pos is not None and origin is not None and remembered.get("dimension") == dim and math.dist(pos, origin) > 16:
                done.add(key)

    objectives = {
        "wooden_pickaxe": (pick_tier >= 0, _task("craft", item="wooden_pickaxe", count=1)),
        "stone_pickaxe": (pick_tier >= 1, _task("craft", item="stone_pickaxe", count=1)),
        "first_food": (food >= min(4, food_limit), _task("food", count=min(4, food_limit))),
        "shelter": ("shelter" in done, _task("shelter")),
        "starter_tools": ("starter_tools" in done, _task("starter_tools")),
        # The smelting controller deploys an owned furnace, checks its actual
        # contents and waits for a real output before closing the job.
        "furnace": ("furnace" in done, _task("smelt", item="charcoal", count=1)
                    if counts.get("furnace", 0) else _task("craft", item="furnace", count=1)),
        "fuel_reserve": (fuel >= fuel_limit, _task("smelt", item="charcoal", count=max(1, fuel_limit - fuel))),
        "torches": (counts.get("torch", 0) >= 4, _task("craft", item="torch", count=4)),
        "bed": ("bed" in done, _task("bed")),
        "storage": ("storage" in done, _task("storage")),
        "iron_tools": ("iron_tools" in done, _task("iron_tools")),
        "food_production": ("food_production" in done, _task("farm")),
        "food_reserve": (food >= food_limit, _task("food", count=food_limit)),
        "wood_reserve": (wood >= wood_limit, _task("gather", resource="logs", count=max(1, wood_limit - wood))),
        "maintenance": (False, _task("maintenance")),
    }
    for index, (stage, label) in enumerate(CHAPTERS, 1):
        satisfied, goal = objectives[stage]
        if satisfied:
            continue
        view = {"stage": stage, "label": label, "chapter": index, "chapters": len(CHAPTERS),
                "goal": goal, "fresh": True, "profile": "overworld_survival"}
        return view, Decision("run_survival_task", goal, label, "mainline")


def same_goal(args, target):
    return (isinstance(args, dict) and isinstance(target, dict)
            and all(args.get(k) == target.get(k) for k in ("task", "item", "resource")))
