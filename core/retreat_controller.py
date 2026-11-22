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
from core.domain import finite_number, parse_pos


RETREAT_REPLAN = {"unsafe", "path_not_found", "no_path", "move_unverified", "retreat_unverified"}
RETREAT_WAIT_SECONDS = 5
RETREAT_COOLDOWN_WAIT_SECONDS = 2
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

    @staticmethod
    def changed(observation: Dict[str, Any], threat: Any, recovery: Dict[str, Any]) -> bool:
        """Return True if bot position, threat position, or observed blocking cells changed."""
        basis = recovery.get("search_basis") or {}
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
        if self.changed(observation, threat, recovery):
            recovery.pop("retry_after", None)
            recovery.pop("cooldown_cycles", None)
            recovery.pop("no_route_exhausted", None)
            return decision
        retry_after = recovery.get("retry_after", 0)
        if retry_after > time.time():
            return Decision(
                "wait",
                {"seconds": RETREAT_COOLDOWN_WAIT_SECONDS},
                "No verified retreat route; observe again before the bounded search cooldown expires",
                "safety",
            )
        if retry_after > 0:
            recovery["cooldown_cycles"] = int(recovery.get("cooldown_cycles", 0)) + 1
            if recovery["cooldown_cycles"] >= 2 and recovery.get("evacuating") is not True:
                recovery["no_route_exhausted"] = True
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
                "retry_seconds": RETREAT_WAIT_SECONDS,
                "search_retry_after": recovery.get("retry_after"),
            },
        )
        return True
