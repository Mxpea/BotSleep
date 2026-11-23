"""Pure hints for one visible leaf after a known, non-executed stance failure.

Failure coordinates are never authority to dig or move. The caller must supply
fresh matching block evidence, and the existing skills recheck every action.
"""

import copy
import math

from agent.survival_planner import Decision
from core.domain import parse_pos
from skills.daily_survival import DailySurvivalSkills


def _pos(value, *, block=False):
    parsed = parse_pos(value, integer=block)
    return list(parsed) if parsed is not None else None


def _adjacent_body_cell(origin, blocker):
    feet = [math.floor(v + .0001) for v in origin]
    return (max(abs(blocker[0] - feet[0]), abs(blocker[2] - feet[2])) == 1
            and blocker[1] in (feet[1], feet[1] + 1) and math.dist(origin, blocker) <= 3)


def position_foliage_hint(decision, result, observation):
    """Keep only bounded diagnostic evidence from a pre-write route refusal."""
    data = result.data if isinstance(result.data, dict) else {}
    status = observation.get("status", {}) if isinstance(observation, dict) else {}
    if (decision.name != "position_for_task" or result.success is not False
            or result.error_code != "unsafe_route" or data.get("executed") is not False
            or data.get("action_state") == "unknown" or data.get("block") not in DailySurvivalSkills.FOLIAGE):
        return None
    origin, blocker, target = _pos(data.get("origin")), _pos(data.get("blocked_cell"), block=True), _pos(decision.args.get("pos"))
    observed = _pos(status.get("pos"))
    if (origin is None or blocker is None or target is None or observed is None
            or math.dist(origin, observed) > .2 or not _adjacent_body_cell(origin, blocker)
            or status.get("dimension") not in {"overworld", "minecraft:overworld"}):
        return None
    return {"origin": origin, "target": target, "blocker": blocker, "name": data["block"],
            "dimension": status["dimension"], "life": copy.deepcopy(status.get("last_death"))}


def _current_hint(hint, observation):
    if not isinstance(hint, dict):
        return False
    status = observation.get("status", {})
    current, origin = _pos(status.get("pos")), _pos(hint.get("origin"))
    blocker, target = _pos(hint.get("blocker"), block=True), _pos(hint.get("target"))
    return (current is not None and origin is not None and blocker is not None and target is not None
            and hint.get("name") in DailySurvivalSkills.FOLIAGE
            and status.get("dimension") == hint.get("dimension")
            and status.get("last_death") == hint.get("life")
            and math.dist(current, origin) <= .2
            and tuple(map(math.floor, current)) == tuple(map(math.floor, origin))
            and _adjacent_body_cell(current, blocker))


def choose_route_foliage(recent_results, observation, context, daily, goal):
    """Propose one existing clear_foliage skill; no route or budget is extended."""
    status = observation.get("status", {})
    if (observation.get("fresh") is not True or observation.get("stale") is True
            or observation.get("observation_scope") == "safety" or observation.get("inventory_fresh") is False
            or context.get("pending") or "clear_foliage" not in context.get("capabilities", ())
            or status.get("ready") is not True or status.get("alive") is not True
            or status.get("gamemode") != "survival" or status.get("busy") not in (None, False)
            or not daily._batch_ready(observation) or status.get("health", 0) < 12 or status.get("food", 0) < 8):
        return None
    nearest = status.get("nearest_hostile")
    if nearest is not None and (not isinstance(nearest, dict) or not isinstance(nearest.get("name"), str)
            or not daily.rules._number(nearest.get("dist"))
            or nearest["dist"] <= daily.rules._threat_distance(nearest["name"])):
        return None
    rows = daily._safe_rows(observation, DailySurvivalSkills.FOLIAGE, context=context)
    handled = set()
    for recent in reversed(recent_results[-8:]):
        if not isinstance(recent, dict):
            continue
        if recent.get("name") == "clear_foliage" and _pos(recent.get("target"), block=True) is not None:
            handled.add(tuple(recent["target"]))  # Never repeat a previous clear from an old hint.
        hint = recent.get("route_foliage")
        if not _current_hint(hint, observation) or tuple(hint["blocker"]) in handled:
            continue
        for row in rows:
            props = row.get("properties") if isinstance(row.get("properties"), dict) else {}
            if (row.get("pos") == hint["blocker"] and row.get("name") == hint["name"]
                    and row.get("fresh") is True and row.get("can_see") is True and row.get("diggable") is True
                    and (row["name"] == "vine" or props.get("waterlogged") is False or props.get("waterlogged") == "false")):
                return Decision("clear_foliage", {"pos": list(hint["blocker"]), "expected_name": hint["name"]},
                    "Clear one freshly visible leaf from a known refused stance corridor; then reobserve the route", goal)
    return None


def confirmed_leaf_removal(decision, result, observation):
    """Return the position of an independently verified, completed foliage cut."""
    data = result.data if isinstance(result.data, dict) else {}
    if (decision.name != "clear_foliage" or result.success is not True
            or data.get("executed") is not True or data.get("verified") is not True
            or data.get("block_removed") is not True or data.get("remaining_block") not in DailySurvivalSkills.AIR
            or data.get("action_state") == "unknown" or data.get("pos") != decision.args.get("pos")
            or data.get("cleared") != decision.args.get("expected_name")
            or not isinstance(observation, dict) or observation.get("fresh") is not True):
        return None
    return data.get("pos")


def cleared_route_targets(recent_results, decision, result, observation):
    """Return only routes whose known blocking leaf was actually removed."""
    pos = confirmed_leaf_removal(decision, result, observation)
    if pos is None:
        return []
    return [list(row["route_foliage"]["target"]) for row in recent_results[-8:] if isinstance(row, dict)
            and _current_hint(row.get("route_foliage"), observation)
            and row["route_foliage"]["blocker"] == pos
            and row["route_foliage"]["name"] == decision.args.get("expected_name")]
