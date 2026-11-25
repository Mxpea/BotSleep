"""A known refused construction sweep belongs to its actual starting cell."""

import copy
import math

from core.domain import HAZARDS, block_key, cell_coords, parse_pos
from core.shelter_project import same_site


def _current_site(observation):
    if not isinstance(observation, dict):
        return None
    status = observation.get("status") or {}
    base = (observation.get("daily_evidence") or {}).get("base_site")
    shelter = observation.get("shelter")
    if (observation.get("fresh") is not True or observation.get("stale") is True
            or observation.get("observation_scope") == "safety" or observation.get("inventory_fresh") is False
            or not isinstance(base, dict) or base.get("fresh") is not True or base.get("verified") is not True
            or base.get("dimension") != status.get("dimension") or parse_pos(base.get("pos"), integer=True) is None
            or base.get("orientation", "north") not in ("north", "east", "south", "west")
            or not isinstance(shelter, dict) or shelter.get("fresh") is not True
            or not same_site({**base, "orientation": base.get("orientation", "north")}, shelter)
            or shelter.get("hazards") != [] or shelter.get("occupied_blueprint_cells") != []
            or status.get("ready") is not True or status.get("alive") is not True
            or status.get("busy") not in (None, False) or parse_pos(status.get("pos")) is None):
        return None
    geometry = shelter.get("geometry") or {}
    if (not isinstance(geometry, dict) or geometry.get("floor_verified") is not True
            or geometry.get("exit_verified") is not True):
        return None
    return {"pos": list(base["pos"]), "orientation": base.get("orientation", "north"),
            "dimension": base["dimension"]}


def shelter_route_refusal(decision, result, observation):
    """Keep only a known prewrite route refusal, not a failed movement or hazard."""
    site = _current_site(observation)
    data = result.data if isinstance(result.data, dict) else {}
    origin, target = parse_pos(data.get("origin")), parse_pos(decision.args.get("pos"))
    if (site is None or decision.name != "position_for_task" or decision.goal not in {"shelter", "shelter_return"}
            or result.success is not False or result.error_code != "unsafe_route"
            or data.get("executed") is not False or data.get("action_state") == "unknown"
            or origin is None or target is None or not .4 < math.dist(origin, target) <= 3
            or parse_pos(data.get("blocked_cell"), integer=True) is None
            or not isinstance(data.get("block"), str) or data["block"] in HAZARDS | {"unknown"}
            or math.dist(origin, observation["status"]["pos"]) > .2):
        return None
    return {"origin": origin, "target": target, "site": site,
            "life": copy.deepcopy(observation["status"].get("last_death"))}


def changed_shelter_routes(recent_results, observation):
    """Reconsider only the newest refused route to a target after a real cell change.

    This yields suggestions to remove controller-owned route cooldowns only.
    The caller preserves all external risk blocks and every movement rechecks.
    """
    site = _current_site(observation)
    if site is None:
        return []
    current = observation["status"]["pos"]
    seen, targets = set(), []
    for row in reversed(recent_results[-8:]):
        if not isinstance(row, dict):
            continue
        target = parse_pos(row.get("target"))
        if target is None or block_key(target) in seen:
            continue
        seen.add(block_key(target))
        hint = row.get("shelter_route_refusal")
        if (row.get("name") != "position_for_task" or row.get("success") is not False
                or row.get("executed") is not False or row.get("error_code") != "unsafe_route"
                or not isinstance(hint, dict) or hint.get("site") != site or hint.get("target") != target
                or hint.get("life") != observation["status"].get("last_death")):
            continue
        origin = parse_pos(hint.get("origin"))
        if origin is not None and cell_coords(origin) != cell_coords(current) and math.dist(origin, current) > .5:
            targets.append(target)
    return targets
