"""Internal scope for continuing an existing intent past a soft policy hold."""

import math
from dataclasses import dataclass

from core.domain import LOG_NAMES, PLANK_NAMES, cell_coords, hostile_threat_radius, parse_pos


NIGHT = "night_requires_verified_sleep_or_shelter_route"
LOW_HEALTH = "low_health_food_only"
HAND_CRAFT = frozenset(PLANK_NAMES) | {"stick", "crafting_table", "torch"}


@dataclass(frozen=True)
class RelaxedWork:
    """One-step permission supplied by the harness, never saved or model-facing."""

    mode: str
    baseline: dict
    threat_radius: float

    def stable(self, status, *, require_idle=True):
        pos, origin = parse_pos(status.get("pos")), parse_pos(self.baseline.get("pos"))
        health, food = status.get("health"), status.get("food")
        nearest = status.get("nearest_hostile")
        if nearest is not None and (not isinstance(nearest, dict)
                or type(nearest.get("dist")) not in (int, float) or not math.isfinite(nearest["dist"])
                or nearest["dist"] <= hostile_threat_radius(nearest.get("name"), self.threat_radius)):
            return False
        return (status.get("ready") is True and status.get("alive") is True
                and status.get("gamemode") == "survival" and status.get("on_ground") is True
                and status.get("dimension") == self.baseline.get("dimension")
                and status.get("last_death") == self.baseline.get("last_death")
                and status.get("thundering") is False
                and (not require_idle or "busy" in status and status["busy"] in (None, False))
                and type(health) in (int, float) and math.isfinite(health) and health >= self.baseline["health"] > 4
                and type(food) in (int, float) and 6 < food <= 20
                and pos is not None and origin is not None and cell_coords(pos) == cell_coords(origin)
                and math.dist(pos, origin) <= .2
                and not any(status.get(k) for k in ("in_water", "submerged", "in_lava", "on_fire", "in_vehicle", "is_sleeping")))


def allows_atomic(name, args, mode):
    """Shape allowlist only. Callers still need fresh stance and target proof."""
    if mode not in (NIGHT, LOW_HEALTH) or not isinstance(args, dict):
        return False
    if name == "craft_item":
        return isinstance(args.get("item"), str) and args["item"] in HAND_CRAFT
    return name == "gather_block" and args.get("expected_name") in LOG_NAMES


def allows_goal(args, mode):
    if not isinstance(args, dict):
        return False
    if args.get("task") == "craft":
        return allows_atomic("craft_item", args, mode)
    return mode in (NIGHT, LOW_HEALTH) and args.get("task") == "gather" and args.get("resource") == "logs"
