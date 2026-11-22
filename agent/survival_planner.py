"""Deterministic, one-action planning for an initial stone-tool workstation.

The planner never calls a client or trusts historical milestones as current items.
Its bounded scope excludes food acquisition, smelting, shelter and combat.
"""

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from core.domain import (
    AIR,
    HAZARDS,
    LOG_NAMES,
    PLANK_NAMES,
    SAFE_FOOD,
    STONE_TIERS,
    WOOD_SPECIES,
    block_key,
    blocked_targets,
    extract_blocks,
    extract_hostiles,
    finite_number,
    has_tool_tier,
    hostile_threat_radius,
    inventory_counts,
    parse_pos,
)

RECIPES = {
    "wooden_pickaxe": {"planks": 3, "stick": 2},
    "stone_pickaxe": {"cobblestone": 3, "stick": 2},
    "stone_axe": {"cobblestone": 3, "stick": 2},
    "stone_sword": {"cobblestone": 2, "stick": 1},
    "furnace": {"cobblestone": 8},
}


@dataclass(frozen=True)
class Decision:
    name: str
    args: Dict[str, Any]
    reason: str
    goal: str


class SurvivalPlanner:
    """Choose one conservative action from a fresh observation without mutation."""

    def __init__(self, settings=None):
        self.settings = settings or {}
        self.explore_distance = self._setting("explore_distance", 16, 1, 32)
        self.max_explore_steps = int(self._setting("max_explore_steps", 8, 0, 1000))
        self.low_health = self._setting("low_health_threshold", 8, 1, 20)
        self.food_threshold = self._setting("food_threshold", 16, 1, 20)
        self.threat_radius = self._setting("threat_radius", 8, 1, 32)
        self.max_target_failures = int(self._setting("max_target_failures", 2, 1, 1000))

    def _setting(self, name, default, minimum, maximum):
        value = self.settings.get(name, default) if isinstance(self.settings, Mapping) else getattr(self.settings, name, default)
        if not self._number(value):
            return default
        return min(maximum, max(minimum, value))

    @staticmethod
    def _number(value):
        return finite_number(value)

    @classmethod
    def _pos(cls, value):
        return parse_pos(value)

    @staticmethod
    def _key(pos):
        # Harness cooldowns identify block cells, including fractional walk targets.
        return block_key(pos)

    @classmethod
    def _counts(cls, observation):
        return inventory_counts(observation)

    @classmethod
    def _blocks(cls, observation):
        return extract_blocks(observation)

    @staticmethod
    def _has_tool(counts, kind, *, wooden=False):
        return has_tool_tier(counts, kind, wooden=wooden)

    def progress(self, observation: dict) -> dict:
        """Return current capabilities/items, not persisted completion claims."""
        counts = self._counts(observation)
        blocks = self._blocks(observation)
        origin = self._pos((observation.get("status") or {}).get("pos"))

        def station(name):
            return counts.get(name, 0) > 0 or any(b["name"] == name and origin and math.dist(b["pos"], origin) <= 32 for b in blocks)

        milestones = {
            "wood": any(counts.get(n, 0) > 0 for n in LOG_NAMES),
            "planks": any(counts.get(n, 0) > 0 for n in PLANK_NAMES),
            "sticks": counts.get("stick", 0) > 0,
            "crafting_table": bool(station("crafting_table")),
            "wooden_pickaxe": self._has_tool(counts, "pickaxe", wooden=True),
            "cobblestone": counts.get("cobblestone", 0) > 0,
            "stone_pickaxe": self._has_tool(counts, "pickaxe"),
            "stone_axe": self._has_tool(counts, "axe"),
            "stone_sword": self._has_tool(counts, "sword"),
            "furnace": bool(station("furnace")),
            "torches": counts.get("torch", 0) > 0,
        }
        milestones["starter_goal_complete"] = all(milestones[k] for k in ("stone_pickaxe", "stone_axe", "stone_sword", "furnace"))
        return milestones

    @staticmethod
    def _wait(reason, goal="safety", seconds=2):
        return Decision("wait", {"seconds": seconds, "reason": reason}, reason, goal)

    def _blocked(self, context):
        # Only active cooldowns are authoritative. Historical failure counters may
        # remain after a cooldown expires and must not permanently blacklist a site.
        return blocked_targets(context)

    def _hostiles(self, observation, origin):
        return extract_hostiles(observation, origin)

    def _threat_distance(self, name):
        """Ranged enemies remain dangerous beyond the melee avoidance radius."""
        return hostile_threat_radius(name, self.threat_radius)

    def _dangerous_hostiles(self, observation, origin):
        found = []
        for entity in observation.get("entities") or []:
            if not isinstance(entity, dict) or entity.get("type") != "hostile":
                continue
            pos = self._pos(entity.get("pos"))
            if pos is not None:
                distance = math.dist(pos, origin)
                if distance <= self._threat_distance(entity.get("name")):
                    found.append((distance, pos, entity))
        return sorted(found, key=lambda value: (value[0], value[1]))

    def _safe_target(self, block, origin, blocks, blocked, hostiles, observation=None):
        pos = block["pos"]
        if self._key(pos) in blocked or math.dist(pos, origin) > 32:
            return False
        if math.floor(pos[0]) == math.floor(origin[0]) and math.floor(pos[2]) == math.floor(origin[2]):
            return False  # Never mine the player's floor or overhead column.
        if not -1 <= pos[1] - math.floor(origin[1]) <= 3:
            return False
        if (block.get("properties") or {}).get("waterlogged") in (True, "true"):
            return False
        if any(math.dist(pos, b["pos"]) <= 1.01 and b["name"] in HAZARDS for b in blocks):
            return False
        if observation is not None and self._dangerous_hostiles(observation, pos):
            return False
        return not any(math.dist(pos, hostile_pos) <= self.threat_radius for _, hostile_pos in hostiles)

    def _gather(self, names, goal, observation, context):
        origin = observation["status"]["pos"]
        blocks = self._blocks(observation)
        blocked = self._blocked(context)
        hostiles = self._hostiles(observation, origin)
        candidates = [b for b in blocks if b["name"] in names
                      and self._safe_target(b, origin, blocks, blocked, hostiles, observation=observation)]
        if candidates:
            target = min(candidates, key=lambda b: (math.dist(b["pos"], origin), b["name"], b["pos"]))
            return Decision("gather_block", {"pos": list(target["pos"]), "expected_name": target["name"]}, f"Gather {target['name']} for {goal}", goal)
        return self._explore(observation, context, f"No safe reachable {goal} resource is visible")

    def _explore(self, observation, context, reason):
        raw_index = context.get("explore_index", 0)
        index = max(0, int(raw_index)) if self._number(raw_index) else 0
        if index >= self.max_explore_steps:
            return self._wait(f"Exploration limit reached: {reason}", "explore", 5)
        origin = observation["status"]["pos"]
        spawn = self._pos(context.get("spawn")) or list(origin)
        directions = ((1, 0), (0, 1), (-1, 0), (0, -1), (1, 1), (-1, 1), (-1, -1), (1, -1))
        blocked = self._blocked(context)
        blocks = self._blocks(observation)
        for step in range(index, self.max_explore_steps):
            dx, dz = directions[step % len(directions)]
            radius = self.explore_distance * (1 + step // len(directions))
            norm = math.hypot(dx, dz)
            target = [spawn[0] + radius * dx / norm, origin[1], spawn[2] + radius * dz / norm]
            distance = math.dist(origin, target)
            if distance <= 2:
                continue
            if distance > 32:
                ratio = self.explore_distance / distance
                target = [origin[i] + (target[i] - origin[i]) * ratio for i in range(3)]
            target = [round(v, 3) for v in target]
            if self._key(target) in blocked or any(b["name"] in HAZARDS and math.dist(b["pos"], target) <= 2 for b in blocks):
                continue
            if self._dangerous_hostiles(observation, target):
                continue
            return Decision("move_to", {"pos": target, "range": 2}, reason + "; inspect again after this bounded move", "explore")
        return self._wait(f"No unblocked exploration target: {reason}", "explore", 5)

    def _need_planks(self, count, observation, context):
        counts = self._counts(observation)
        total = sum(counts.get(n, 0) for n in PLANK_NAMES)
        if total >= count:
            return None
        for wood in WOOD_SPECIES:
            available = sum(counts.get(n, 0) for n in (f"{wood}_log", f"{wood}_wood", f"stripped_{wood}_log", f"stripped_{wood}_wood"))
            if available:
                amount = min(64, available * 4, math.ceil((count - total) / 4) * 4)
                return Decision("craft_item", {"item": f"{wood}_planks", "count": amount}, "Convert held wood into the missing planks", "planks")
        return self._gather(LOG_NAMES, "wood", observation, context)

    def _workspace(self, observation, context):
        counts = self._counts(observation)
        origin = observation["status"]["pos"]
        blocks = self._blocks(observation)
        blocked = self._blocked(context)
        tables = [b for b in blocks if b["name"] == "crafting_table"
                  and math.dist(b["pos"], origin) <= 32 and self._key(b["pos"]) not in blocked
                  and not self._dangerous_hostiles(observation, b["pos"])
                  and not any(other["name"] in HAZARDS and math.dist(b["pos"], other["pos"]) <= 1.01 for other in blocks)]
        if tables:
            table = min(tables, key=lambda b: (math.dist(b["pos"], origin), b["pos"]))
            if math.dist(table["pos"], origin) > 3.5:
                return Decision("move_to", {"pos": list(table["pos"]), "range": 2}, "Approach the existing crafting table", "crafting_table")
            return None
        if counts.get("crafting_table", 0) > 0:
            for row in observation.get("placement_candidates") or []:
                pos = self._pos(row.get("pos")) if isinstance(row, dict) else None
                if pos is None or len(pos) != 3 or math.dist(pos, origin) > 3.5:
                    continue
                target = [math.floor(pos[0]), math.floor(pos[1]), math.floor(pos[2])]
                if self._key(target) in blocked or self._dangerous_hostiles(observation, target):
                    continue
                return Decision("place_item", {"item": "crafting_table", "pos": target}, "Propose an adjacent workspace; the skill must verify air and solid support", "crafting_table")
            base = [math.floor(v) for v in origin]
            by_pos = {self._key(b["pos"]): b for b in blocks}
            for dx, dz in ((2, 0), (0, 2), (-2, 0), (0, -2), (1, 1), (-1, 1), (-1, -1), (1, -1)):
                target = [base[0] + dx, base[1], base[2] + dz]
                at = by_pos.get(self._key(target))
                below = by_pos.get(self._key([target[0], target[1] - 1, target[2]]))
                if self._key(target) in blocked or (at and at["name"] not in AIR) or (below and below["name"] in AIR | HAZARDS):
                    continue
                if any(b["name"] in HAZARDS and math.dist(b["pos"], target) <= 1.01 for b in blocks):
                    continue
                if self._dangerous_hostiles(observation, target):
                    continue
                return Decision("place_item", {"item": "crafting_table", "pos": target}, "Propose an adjacent workspace; the skill must verify air and solid support", "crafting_table")
            return self._wait("No unblocked workspace placement candidate", "crafting_table")
        missing = self._need_planks(4, observation, context)
        return missing or Decision("craft_item", {"item": "crafting_table", "count": 1}, "Create the workstation needed for tools", "crafting_table")

    def _craft_goal(self, item, observation, context):
        counts = self._counts(observation)
        recipe = RECIPES[item]
        if recipe.get("planks", 0):
            missing = self._need_planks(recipe["planks"], observation, context)
            if missing:
                return missing
        if counts.get("stick", 0) < recipe.get("stick", 0):
            missing = self._need_planks(2, observation, context)
            return missing or Decision("craft_item", {"item": "stick", "count": 4}, "Craft a batch of sticks for the missing tool", "sticks")
        if counts.get("cobblestone", 0) < recipe.get("cobblestone", 0):
            if not self._has_tool(counts, "pickaxe", wooden=True):
                return self._craft_goal("wooden_pickaxe", observation, context)
            return self._gather(("stone", "cobblestone"), "cobblestone", observation, context)
        workspace = self._workspace(observation, context)
        return workspace or Decision("craft_item", {"item": item, "count": 1}, f"Craft the missing {item}", item)

    def choose(self, observation: dict, context: Optional[dict] = None) -> Decision:
        context = context or {}
        if not isinstance(observation, dict) or observation.get("fresh") is False or observation.get("stale") is True:
            return self._wait("A fresh observation is required", "observe")
        status = observation.get("status")
        if not isinstance(status, dict):
            return self._wait("No status snapshot is available", "observe")
        if status.get("alive") is False or (self._number(status.get("health")) and status["health"] <= 0):
            return self._wait("Bot is dead; wait for respawn and fresh inventory", "recover")
        if status.get("ready") is not True or status.get("alive") is not True:
            return self._wait("Bot is not ready and alive", "observe")
        if status.get("busy"):
            return self._wait("An action is still running; do not overlap actions", "observe", 0.5)
        if status.get("dimension") not in {"overworld", "minecraft:overworld"}:
            return self._wait("Starter survival is restricted to the overworld")
        if status.get("gamemode") != "survival":
            return self._wait("Starter survival requires survival game mode")
        origin = self._pos(status.get("pos"))
        if origin is None or not all(self._number(status.get(k)) for k in ("health", "food")):
            return self._wait("Status is missing valid position, health or hunger", "observe")
        if not isinstance(status.get("is_day"), bool) or not isinstance(status.get("thundering"), bool):
            return self._wait("Status is missing day or storm information", "observe")
        counts = self._counts(observation)
        blocks = self._blocks(observation)
        hostiles = self._hostiles(observation, origin)
        if any(status.get(k) for k in ("in_lava", "on_fire", "submerged")):
            return self._wait("Immediate environmental hazard; no safe recovery route is verified", "recover", 0.5)
        if status.get("on_ground") is False and not status.get("is_sleeping"):
            return self._wait("Wait for stable footing before starting a resource action", "recover", 0.5)
        dangers = self._dangerous_hostiles(observation, origin)
        if dangers:
            return Decision("retreat_from", {"pos": dangers[0][1], "distance": 8}, "A hostile is within its danger radius; the skill must verify a safe retreat corridor", "safety")
        nearby = status.get("nearest_hostile") or {}
        if isinstance(nearby, dict) and self._number(nearby.get("dist")) and nearby["dist"] <= self._threat_distance(nearby.get("name")):
            return self._wait("A nearby hostile has no observed position for safe retreat")
        foods = [name for name in SAFE_FOOD if counts.get(name, 0) > 0]
        if status["food"] < 20 and foods and (status["health"] <= self.low_health or status["food"] <= self.food_threshold):
            deficit = 20 - status["food"]
            fitting = [name for name in foods if SAFE_FOOD[name] <= deficit]
            food = min(fitting, key=lambda name: (-SAFE_FOOD[name], name)) if fitting else min(foods, key=lambda name: (SAFE_FOOD[name], name))
            return Decision("eat_food", {"item": food}, "Use existing safe food before resource work", "food")
        if status["health"] <= self.low_health:
            return self._wait("Health is low; food is below 18 so natural regeneration cannot restore health; verified food recovery is required"
                              if status["food"] < 18 else "Health is low; observe natural regeneration before gathering", "recover")
        if status["food"] <= 6 and not foods:
            return self._wait("Food is critically low; food acquisition is not implemented", "food", 5)
        if status.get("is_sleeping"):
            return Decision("auto_sleep", {}, "Observe the current sleep until waking is verified", "sleep")
        if not status["is_day"] or status["thundering"]:
            beds = [b for b in blocks if b["name"].endswith("_bed") and math.dist(b["pos"], origin) <= 16 and (b.get("properties") or {}).get("occupied") not in (True, "true")]
            if beds:
                return Decision("auto_sleep", {}, "A bed is visible during night or a storm; use verified sleep", "sleep")
            return self._wait("Night or storm without a known free bed; shelter is not implemented", "sleep", 5)
        progress = self.progress(observation)
        if progress["stone_pickaxe"] and counts.get("torch", 0) == 0 and counts.get("stick", 0) > 0 and (counts.get("coal", 0) > 0 or counts.get("charcoal", 0) > 0):
            return Decision("craft_item", {"item": "torch", "count": 4}, "Use available fuel and a stick for optional lighting", "torches")
        if progress["starter_goal_complete"]:
            return Decision("goal_complete", {}, "Initial stone tools and furnace are available; full survival is outside this goal", "starter_goal")
        if not self._has_tool(counts, "pickaxe", wooden=True):
            return self._craft_goal("wooden_pickaxe", observation, context)
        for item in ("stone_pickaxe", "stone_axe", "stone_sword", "furnace"):
            if not progress[item]:
                return self._craft_goal(item, observation, context)
        return self._wait("Observe again before advancing the starter goal", "observe")
