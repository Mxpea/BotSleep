"""Bounded local-first planning. It returns one intent; the existing harness owns all I/O."""

import copy
import hashlib
import json
import time

from agent.survival_planner import Decision, SurvivalPlanner
from core.local_recovery import canopy_clearances, canopy_descent
from core.local_tasks import LocalTaskPlanner
from skills.daily_survival import DailySurvivalSkills


class LocalExecutor:
    """Plan fresh work until bounded exhaustion, then retain a compact takeover report.

    Persist only evidence/signatures and budgets, never a replayable action queue.
    The caller passes its scoped context and records every verified result.
    """

    def __init__(self, settings: dict, clock=time.time):
        self.settings = settings
        self.limits = settings.get("local_execution", {})
        self.enabled = self.limits.get("enabled", False) is True
        self.clock = clock
        self.tasks = LocalTaskPlanner(settings)

    @staticmethod
    def _digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()

    def _basis(self, observation, context, board):
        status = observation.get("status") or {}
        return self._digest({"life": [status.get("dimension"), status.get("last_death")],
            "pos": status.get("pos"), "counts": observation.get("counts", {}),
            "blocks": [[row.get("pos"), row.get("name"), row.get("can_see")] for row in observation.get("blocks", [])[:64]
                       if isinstance(row, dict)], "goal": (board or {}).get("active_goal_id"),
            "phase": (board or {}).get("phase"),
            "completion": ((board or {}).get("active_goal") or {}).get("completion"),
            "assessment": [(board or {}).get("assessment_complete"), (board or {}).get("assessment_fresh"),
                ((board or {}).get("advancements") or {}).get("ready"),
                ((board or {}).get("advancements") or {}).get("stale")],
            "canopy_step": {key: (observation.get("canopy_step") or {}).get(key)
                for key in ("origin", "pos", "clear_pos", "support", "drop")},
            "stations": {key: [{field: row.get(field) for field in
                ("pos", "dimension", "fresh", "verified", "input", "fuel", "output", "items", "burning")}
                for row in observation.get(key, [])[:8] if isinstance(row, dict)]
                for key in ("furnaces", "containers")}, "canopy": (context.get("retreat_recovery") or {}).get("canopy_blocked")})

    def choose(self, observation: dict, context: dict, *, competition=None, daily_board=None):
        if not self.enabled:
            return None
        state = context.get("local_execution")
        if isinstance(state, dict) and state.get("phase") == "blocked_unknown":
            return None
        basis = self._basis(observation, context, competition)
        status = observation.get("status") or {}
        scope = self._digest([status.get("dimension"), status.get("last_death"),
            (competition or {}).get("phase"), (competition or {}).get("active_goal_id"),
            "canopy" if (context.get("retreat_recovery") or {}).get("canopy_blocked") else "task"])
        state = context.get("local_execution")
        if not isinstance(state, dict) or state.get("version") != 1 or state.get("scope") != scope:
            state = {"version": 1, "scope": scope, "started_at": self.clock(), "steps": 0,
                     "failures": {}, "failed_targets": [], "recent": [], "yield_basis": None, "phase": "running", "reason": None}
            context["local_execution"] = state
        if state.get("phase") == "blocked_unknown":
            return None
        if state.get("phase") == "model_takeover":
            if state.get("yield_basis") == basis:
                return None
            state.update(started_at=self.clock(), steps=0, failures={}, failed_targets=[], yield_basis=None, phase="running", reason=None)
        if (state["steps"] >= self.limits.get("max_steps", 64)
                or self.clock() - state["started_at"] >= self.limits.get("max_seconds", 180)):
            self.yield_to_model(context, basis, "local_budget_exhausted")
            return None
        recovery = context.get("retreat_recovery") or {}
        candidates = []
        if recovery.get("canopy_blocked") is True:
            descent = canopy_descent(observation, context, self.settings)
            candidates = [Decision("force_attempt", {"action": row.name, **row.args}, row.reason, "safety")
                          for row in canopy_clearances(observation, context, self.settings)]
            if descent is not None:
                candidates.insert(0, descent)
        else:
            planning = context
            if state.get("failed_targets"):
                planning = copy.deepcopy(context)
                planning["blocked_targets"] = list(dict.fromkeys(
                    list(context.get("blocked_targets") or []) + state["failed_targets"]))
            decision = self.tasks.choose(observation, planning, competition=competition, daily_board=daily_board)
            if decision is None:
                decision = self._processing_wait(observation, context, competition)
            if decision is not None:
                candidates = [decision]
        for decision in candidates:
            signature = self._digest([decision.name, decision.args])
            if state["failures"].get(signature, 0) < 1:
                state.update(phase="running", reason=None, before_basis=basis, last_signature=signature)
                return decision
        self.yield_to_model(context, basis, "no_verified_local_recovery" if recovery.get("canopy_blocked") else "no_verified_local_step")
        return None

    def _processing_wait(self, observation, context, competition):
        """Known, freshly inspected jobs are scheduling work, not model decisions."""
        if (observation.get("fresh") is not True or observation.get("stale") is True
                or observation.get("observation_scope", "full") != "full"):
            return None
        if competition and (competition.get("phase") != "active" or
                (competition.get("active_goal_id") or "").split("/")[-1] not in
                {"smelt_iron", "iron_tools", "obtain_armor"}):
            return None
        dimension = (observation.get("status") or {}).get("dimension")
        for job in context.get("smelting_jobs", [])[:8]:
            for furnace in observation.get("furnaces", [])[:8]:
                source = furnace.get("input") or {}
                if (job.get("dimension") == furnace.get("dimension") == dimension
                        and furnace.get("pos") == job.get("pos")
                        and furnace.get("fresh") is True and furnace.get("verified") is True
                        and furnace.get("burning") is True
                        and type(source.get("count")) is int and source["count"] > 0
                        and job.get("expected_output") is not None
                        and DailySurvivalSkills.SMELTABLE.get(source.get("name")) == job["expected_output"]
                        and not (furnace.get("output") or {}).get("count", 0)):
                    return Decision("wait", {"seconds": 5},
                        "Verified furnace is processing the known job; reobserve within the local budget", "local_processing")
        return None

    def yield_to_model(self, context, basis, reason):
        state = context["local_execution"]
        state.update(phase="model_takeover", yield_basis=basis, reason=reason)

    def reject(self, context, reason):
        state = context.get("local_execution") or {}
        if state and state.get("phase") != "blocked_unknown":
            self.yield_to_model(context, state.get("before_basis"), reason)

    def record(self, decision, result, observation, context, *, competition=None):
        state = context.get("local_execution") or {}
        if not state:
            return
        state["steps"] += 1
        changed = (observation is not None and observation.get("fresh") is True
                   and observation.get("stale") is not True
                   and self._basis(observation, context, competition) != state.get("before_basis"))
        processing = decision.name == "wait" and decision.goal == "local_processing" and result.success
        if not result.success or not changed and not processing:
            signature = state.get("last_signature")
            if signature:
                state["failures"][signature] = state["failures"].get(signature, 0) + 1
                state["failures"] = dict(list(state["failures"].items())[-16:])
            pos = decision.args.get("pos")
            if isinstance(pos, (list, tuple)) and len(pos) == 3:
                key = SurvivalPlanner._key(pos)
                state["failed_targets"] = list(dict.fromkeys(state.get("failed_targets", []) + [key]))[-16:]
        state["recent"] = (state["recent"] + [{"action": decision.args.get("action", decision.name),
            "success": result.success, "error_code": result.error_code, "progress": bool(changed)}])[-6:]
        if result.error_code in {"request_timeout", "network_error", "invalid_json", "invalid_response", "dig_unconfirmed", "action_unconfirmed", "internal", "internal_error"} or result.data.get("action_state") == "unknown":
            state.update(phase="blocked_unknown", reason="uncertain_action")

    @staticmethod
    def summary(context):
        state = context.get("local_execution") or {}
        return {key: copy.deepcopy(state.get(key)) for key in ("phase", "reason", "steps", "recent")}
