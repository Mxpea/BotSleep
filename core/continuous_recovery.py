"""Read-only game-action reconciliation for continuous daily survival."""

import copy
import math
import time

from core.survival_state import ObservationCollector
from core.starter_mining_task import interrupted_mine
from skills import SkillResult


def _stance(observation):
    status = observation["status"]
    return {
        "dimension": status.get("dimension"), "life": status.get("last_death"),
        "cell": [math.floor(v) for v in status["pos"]], "counts": observation["counts"],
    }


class ContinuousRecovery:
    def _init_continuous_recovery(self):
        self._continuous_retry_at = 0.
        self._continuous_retries = 0
        self._continuous_gate_check = 0.
        self._operator_pause = self.context.get("operator_pause") if self.continuous else None
        if self.continuous:
            if self._operator_pause or self.pause_reason == "user_requested":
                self._operator_pause = self._operator_pause or "user_requested"
                self._paused, self.pause_reason = True, self._operator_pause
            elif self._paused:
                self._paused, self.pause_reason = False, None

    def _schedule_continuous_retry(self, code, retry_after=0):
        if self._operator_pause:
            self._paused, self.pause_reason = True, self._operator_pause
            return
        self._continuous_retries = min(6, self._continuous_retries + 1)
        delay = max(retry_after, min(60, max(1, self.settings["retry_seconds"]) * 2 ** (self._continuous_retries - 1)))
        self._continuous_retry_at = time.monotonic() + delay
        self._next_attempt = self._continuous_retry_at
        self._paused, self.pause_reason, self.phase = False, None, "waiting"
        self._record("WARN", "CONTINUOUS_RETRY", {"code": code, "retry_seconds": delay,
                     "pending": bool(self.journal.state.get("pending"))})

    def _reobserve_interrupted(self):
        """Abandon an interrupted intent without opening a GUI or replaying a write."""
        pending = copy.deepcopy(self.journal.state.get("pending"))
        # DailyObservationCollector also calls window_view. Only the base
        # inventory/block/entity/status reads are appropriate for this barrier.
        observation = ObservationCollector(self.client, self.settings["scan_radius"]).collect(self._cancel)
        status = observation["status"]
        if (not observation.get("fresh") or status.get("ready") is not True or status.get("alive") is not True
                or status.get("gamemode") != "survival" or status.get("dimension") not in {"overworld", "minecraft:overworld"}
                or "busy" not in status or status["busy"] not in (False, None)
                or self._cancel.is_set() or pending != self.journal.state.get("pending")):
            return SkillResult(False, "Waiting for a complete idle living observation", {"executed": False},
                               "recovery_observation_pending"), observation
        self.latest_status = status
        self._update_lifecycle(status)
        task = self._task_snapshot() or {}
        task_cancelled = False
        if task.get("status") == "unknown" or self.task_controller and self.task_controller.state().get("pending_digest"):
            if not self.task_controller.abandon_after_reobserve(observation):
                return SkillResult(False, "Interrupted task still needs a fresh observation", {"executed": False},
                                   "recovery_observation_pending"), observation
            task_cancelled = True
        elif pending and self._task_active():
            # Safety can interrupt between task substeps. Rebuild this goal
            # from current inventory instead of retaining a pre-respawn target.
            self.task_controller.stop("Old action abandoned after read-only observation", cancelled=True)
            task_cancelled = True
        if task_cancelled:
            self._save_task("TASK_CANCELLED")
            self._finish_task_feedback(observation)
        original = (pending or {}).get("decision")
        if isinstance(original, dict):
            self.context["last_abandoned_action"] = {"name": original.get("name"),
                "args": copy.deepcopy(original.get("args")), "observed": _stance(observation),
                "generation": self.context["life_generation"]}
            if original.get("name") == "step_starter_staircase" and self.task_controller:
                saved = self.task_controller.state()
                same_life = (saved["life_generation"] == self.context["life_generation"]
                    and saved["dimension"] == status["dimension"]
                    and saved["life"] == self.task_controller._life(status) and pending.get("task_id") == task.get("task_id"))
                old = self.context["last_abandoned_action"]
                old["mine_landmark"] = (interrupted_mine(original, self.journal.state.get("last_result"), task)
                                        if same_life else None)
            pos = (original.get("args") or {}).get("pos")
            if self._safety_rules._pos(pos) is not None:
                self.context.setdefault("blocked_until", {})[self._safety_rules._key(pos)] = (
                    time.time() + self.settings["target_cooldown_seconds"])
        canopy = self.context.get("canopy_recovery") or {}
        if canopy.get("unknown") and isinstance(original, dict) and original.get("name") in {"retreat_from", "canopy_escape"}:
            self.context.pop("canopy_recovery", None)
        self._task_resume_requested = False
        self._record("WARN", "CONTINUOUS_INTENT_ABANDONED", {"decision": original, "task_id": task.get("task_id"),
            "prior_result": self.journal.state.get("last_result"), "pos": status["pos"], "generation": self.context["life_generation"]})
        return SkillResult(True, "Current game state observed; plan again without crediting the previous action",
                           {"executed": False, "reobserved": True, "original_action_confirmed": False}), observation

    def _matches_abandoned_action(self, decision, observation):
        old = self.context.get("last_abandoned_action") or {}
        if not old:
            return False
        if old.get("generation") != self.context.get("life_generation"):
            return False
        if decision.name != old.get("name") or decision.args != old.get("args"):
            return False
        current = _stance(observation)
        if observation.get("inventory_fresh") is False or observation.get("observation_scope") == "safety":
            current["counts"] = old["observed"].get("counts")
        return old["observed"] == current
