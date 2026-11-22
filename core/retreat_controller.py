"""Bounded canopy/hostile retreat recovery state machine for SurvivalHarness.

Persists only recovery evidence (evacuation origin, threat position, bounded
excluded target cooldowns, and last search basis), never an unverified path or
interrupted pending action.
"""

import copy
import math
import time
from typing import Any, Callable, Dict, Optional

from agent.survival_planner import Decision
from core.domain import RANGED_HOSTILES
from core.domain import finite_number, parse_pos


RETREAT_REPLAN = {"unsafe", "path_not_found", "no_path", "move_unverified", "retreat_unverified", "retreat_interrupted"}
RETREAT_WAIT_SECONDS = 5
RETREAT_SEARCH_COOLDOWN = 30


class RetreatController:
    """Manage retreat recovery state, route exclusion cooldowns, and canopy relay stages."""

    def __init__(self, context: Dict[str, Any], settings: Dict[str, Any],
                 record_fn: Optional[Callable[[str, str, Dict[str, Any]], None]] = None):
        self.context = context
        self.settings = settings
        self._record = record_fn or (lambda _level, _event, _data: None)

    def state(self, observation: Dict[str, Any], threat: Any) -> Dict[str, Any]:
        """Persist recovery evidence, never an executable route or old pending action."""
        status = observation["status"]
        recovery = self.context.get("retreat_recovery")
        if not isinstance(recovery, dict) or recovery.get("dimension") != status.get("dimension"):
            recovery = {
                "dimension": status.get("dimension"),
                "evacuating": False,
                "evacuation_origin": list(status["pos"]),
                "excluded_until": {},
            }
            self.context["retreat_recovery"] = recovery
        if recovery.get("evacuating") is not True:
            recovery["evacuation_origin"] = list(status["pos"])
        recovery["threat_pos"] = list(threat)
        now = time.time()
        recovery["excluded_until"] = {
            key: expiry
            for key, expiry in recovery.get("excluded_until", {}).items()
            if finite_number(expiry) and expiry > now
        }
        return recovery

    def nearby_threats(self, observation: Dict[str, Any]) -> list:
        """Retain only bounded, observed danger evidence; absence never clears a route."""
        status = observation.get("status", {})
        origin = parse_pos(status.get("pos"))
        rows = {}

        def add(entity, gap):
            if not isinstance(entity, dict) or not finite_number(gap) or gap < 0:
                return
            name = entity.get("name") if isinstance(entity.get("name"), str) else None
            radius = self.settings.get("threat_radius", 8)
            limit = max(radius, 16) if name in RANGED_HOSTILES else radius
            if gap > limit:
                return
            entity_id = entity.get("id")
            if type(entity_id) not in (int, str):
                entity_id = None
            key = (entity_id, name)
            if key not in rows or gap < rows[key]["distance"]:
                rows[key] = {"id": entity_id, "name": name, "distance": gap}

        if origin is not None:
            for row in observation.get("entities", [])[:64]:
                point = parse_pos(row.get("pos")) if isinstance(row, dict) else None
                if point is not None and row.get("type") == "hostile":
                    add(row, math.dist(origin, point))
        nearest = status.get("nearest_hostile")
        if isinstance(nearest, dict):
            add(nearest, nearest.get("dist"))
        return list(rows.values())

    def wait_seconds(self, observation: Optional[Dict[str, Any]]) -> int:
        """Observe exposed shooters promptly without repeating the terrain search."""
        return 1 if observation and any(row["name"] in RANGED_HOSTILES
                                       for row in self.nearby_threats(observation)) else RETREAT_WAIT_SECONDS

    def changed(self, observation: Dict[str, Any], threat: Any, recovery: Dict[str, Any]) -> bool:
        """Invalidate a failed search when fresh harm, enemies or geometry change its basis."""
        basis = recovery.get("search_basis") or {}
        health, old_health = observation["status"].get("health"), basis.get("health")
        if finite_number(health) and finite_number(old_health) and health < old_health:
            return True
        old_rows = basis.get("nearby_threats")
        if isinstance(old_rows, list):
            previous = {(row.get("id"), row.get("name")): row.get("distance")
                        for row in old_rows if isinstance(row, dict)}
            for row in self.nearby_threats(observation):
                old_gap = previous.get((row["id"], row["name"]))
                # A new enemy or a two-block approach invalidates the old
                # failed search; sub-block coordinate noise does not.
                if old_gap is None or finite_number(old_gap) and row["distance"] <= old_gap - 2:
                    return True
        origin = parse_pos(observation["status"].get("pos"))
        old_origin = parse_pos(basis.get("pos"))
        old_threat = parse_pos(basis.get("threat_pos"))
        if origin and old_origin and (
            math.dist(origin, old_origin) >= 1
            or any(math.floor(a) != math.floor(b) for a, b in zip(origin, old_origin))
        ):
            return True
        if old_threat and math.dist(threat, old_threat) >= 2:
            return True
        # Only compare cells actually observed on both occasions. A missing
        # resource row is not proof that an obstruction has disappeared.
        blocks = {
            tuple(row["pos"]): row
            for row in observation.get("blocks", []) + observation.get("terrain", [])
            if isinstance(row, dict) and parse_pos(row.get("pos")) is not None
        }
        for row in recovery.get("blocked_cells", []):
            current = blocks.get(tuple(row["pos"]))
            if current is not None and current.get("name") != row.get("name"):
                return True
        return False

    def choose_decision(self, observation: Dict[str, Any], decision: Decision) -> Decision:
        """Continue active canopy evacuation or wait during search cooldown when appropriate."""
        recovery = self.context.get("retreat_recovery") or {}
        if decision.name != "retreat_from":
            if recovery.get("evacuating") is not True:
                return decision
            # Lifecycle, immediate hazards, stable footing, food and low health
            # retain priority. Night alone must not abandon a canopy relay.
            if decision.goal in {"observe", "recover", "food", "food_recovery", "safety"} or observation["status"].get("is_sleeping"):
                return decision
            threat = parse_pos(recovery.get("threat_pos"))
            if threat is None:
                return Decision("wait", {"seconds": RETREAT_WAIT_SECONDS}, "Evacuation needs a verified threat position", "safety")
            decision = Decision(
                "retreat_from",
                {"pos": threat, "distance": 8},
                "Continue the canopy evacuation until safe ground is verified",
                "safety",
            )
        threat = parse_pos(decision.args.get("pos"))
        if threat is None:
            return decision  # Skill parameter validation still owns malformed input.
        recovery = self.state(observation, threat)
        if recovery.get("retry_after", 0) > time.time() and not self.changed(observation, threat, recovery):
            return Decision(
                "wait",
                {"seconds": self.wait_seconds(observation)},
                "No verified retreat route; observe again before the bounded search cooldown expires",
                "safety",
            )
        recovery.pop("retry_after", None)
        return decision

    def options(self, observation: Dict[str, Any], decision: Decision) -> Dict[str, Any]:
        """Build recovery_context kwargs for the retreat_from skill."""
        recovery = self.state(observation, decision.args["pos"])
        excluded = []
        for key in recovery["excluded_until"]:
            try:
                pos = [int(value) for value in key.split(",")]
            except (ValueError, AttributeError):
                continue
            if len(pos) == 3:
                excluded.append(pos)
        return {
            "excluded_targets": excluded[-8:],
            "evacuating": recovery.get("evacuating") is True,
            "evacuation_origin": recovery.get("evacuation_origin"),
        }

    def handle_result(self, decision: Decision, result: Any, observation: Optional[Dict[str, Any]],
                      *, uncertain_codes: set, fatal_codes: set) -> bool:
        """Return whether a known route failure uses recovery rather than global failure limits."""
        if decision.name != "retreat_from" or observation is None:
            return False
        recovery = self.context.get("retreat_recovery")
        if not isinstance(recovery, dict):
            return False
        # A transport/custom result can retain an otherwise known error code.
        # Explicit uncertainty must still prevent route mutation or replay.
        if result.data.get("action_state") == "unknown":
            return False
        if result.success:
            if result.data.get("retreat_complete") is False and result.data.get("evacuation_stage") == "relay":
                recovery["evacuating"] = True
                recovery.pop("retry_after", None)
                self._record("INFO", "EVACUATION_RELAY", {"target": result.data.get("target")})
            elif result.data.get("retreat_complete") is True and result.data.get("evacuation_stage") == "ground":
                self.context.pop("retreat_recovery", None)
                self.context.pop("combat_retreat", None)
            return False
        # Unknown writes must never become permission to try a different route.
        if result.error_code in uncertain_codes | fatal_codes | {"internal", "internal_error", "cancelled"}:
            return False
        if result.error_code not in RETREAT_REPLAN and result.data.get("candidate_failed") is not True:
            return False
        target = parse_pos(result.data.get("target"))
        if target is not None:
            key = ",".join(str(math.floor(value)) for value in target)
            excluded = recovery.setdefault("excluded_until", {})
            excluded.pop(key, None)
            excluded[key] = time.time() + self.settings["target_cooldown_seconds"]
            recovery["excluded_until"] = dict(list(excluded.items())[-8:])
        else:
            recovery["retry_after"] = time.time() + RETREAT_SEARCH_COOLDOWN
        recovery["search_basis"] = {
            "pos": list(observation["status"]["pos"]),
            "threat_pos": list(decision.args["pos"]),
            "health": observation["status"].get("health"),
            "nearby_threats": self.nearby_threats(observation),
        }
        recovery["blocked_cells"] = [
            copy.deepcopy(row)
            for row in result.data.get("blocked_cells", [])
            if isinstance(row, dict) and parse_pos(row.get("pos")) is not None
        ][:4]
        self._record(
            "WARN",
            "RETREAT_REPLAN_SCHEDULED",
            {
                "code": result.error_code,
                "target": target,
                "retry_seconds": self.wait_seconds(observation),
                "search_retry_after": recovery.get("retry_after"),
            },
        )
        return True
