"""Two-tier loop and watchdog supervisor for BotSleep survival loops.

Architecture:
- Tier 1 (L1 Execution Watchdog): Watches a single active API command or multi-step
  workflow (`deep_mine`, `scout`, `nether_portal`, etc.) and aborts via `stop()` if
  execution stalls without activity/position/step progress beyond `action_watchdog_seconds`.
- Tier 2 (L2 Livelock & No-Progress Supervisor): Watches cross-step behavioral cycles
  where each step returns quickly (<1s) yet the agent makes zero real progress:
  1. `llm_cancelled` / safety preemption thrash shield (cooldown on non-hit noise).
  2. Continuous passive `wait` livelock alerts (`>= 6s` stationary waiting).
  3. Identical failing action signature (`bad_args` / repeated failure) cooldowns.
  4. Nighttime repeated-death hotspot suspension for surface `recover_drops`.
"""

from collections import deque
import json
import math
from typing import Any, Callable, Optional

from core.domain import finite_number


class LoopSupervisor:
    """Unified L1 execution watchdog and L2 cross-step livelock breaker."""

    def __init__(
        self,
        *,
        action_watchdog_seconds: float = 0.0,
        record_fn: Optional[Callable[[str, str, dict[str, Any]], None]] = None,
    ) -> None:
        """Initialize L1 timing state and L2 rolling livelock trackers."""
        self.action_watchdog_seconds = float(action_watchdog_seconds or 0.0)
        self._record_fn = record_fn

        # L1 single-action watchdog state (exposed via properties on SurvivalHarness).
        self.watchdog_started_at: float = 0.0
        self.watchdog_last_progress_at: float = 0.0
        self.watchdog_last_activity_at: float = 0.0
        self.watchdog_last_activity_seq: Optional[int] = None
        self.watchdog_last_write_at: float = 0.0
        self.watchdog_last_pos: Optional[list[float]] = None
        self.watchdog_last_task_steps: Optional[int] = None
        self.watchdog_decision_id: Optional[int] = None
        self.watchdog_triggered: bool = False
        self.watchdog_reason: Optional[str] = None

        # L2 preemption thrash state (`llm_cancelled` / `llm_preempted` burst detection).
        self._cancel_timestamps: deque[float] = deque(maxlen=16)
        self._preempt_shield_until: float = 0.0

        # L2 passive wait streak state.
        self._passive_wait_seconds: float = 0.0
        self._passive_wait_count: int = 0
        self._passive_wait_pos: Optional[tuple[int, int, int]] = None

        # L2 bad-args / repeated identical failure cooldown map: signature -> (count, expires_at, hint)
        self._signature_failures: dict[str, tuple[int, float, str]] = {}
        self._last_counts_digest: Optional[str] = None

    def _emit(self, level: str, event: str, data: dict[str, Any]) -> None:
        if callable(self._record_fn):
            self._record_fn(level, event, data)

    # ------------------------------------------------------------------
    # Tier 1: Single-Action & Multi-Step Workflow Execution Watchdog
    # ------------------------------------------------------------------

    @staticmethod
    def is_workflow_decision(decision: Any, *, task_child: bool = False) -> bool:
        """Return True if `decision` represents a multi-step long-running workflow."""
        if not task_child or decision is None:
            return False
        return bool(
            decision.goal in {"deep_mine", "scout_surface", "equip_loadout", "nether_portal_water", "nether_portal_gravel"}
            or (decision.goal == "nether_portal" and decision.name == "move_to")
        )

    def reset_for_decision(self, now: float, decision: Any) -> None:
        """Reset L1 watchdog markers when dispatching a new concrete decision."""
        self.watchdog_started_at = now
        self.watchdog_last_progress_at = now
        self.watchdog_last_activity_at = now
        self.watchdog_decision_id = id(decision)
        self.watchdog_triggered = False
        self.watchdog_reason = None

    def begin_cycle_watchdog(self, now: float, *, client: Any, task_controller: Any = None) -> None:
        """Initialize L1 watchdog baselines at the start of an async survival tick."""
        self.watchdog_started_at = now
        self.watchdog_last_progress_at = now
        self.watchdog_last_activity_at = now
        raw_seq = getattr(client, "activity_seq", None)
        self.watchdog_last_activity_seq = raw_seq if type(raw_seq) is int else None
        raw_write = getattr(client, "last_write_at", 0.0)
        self.watchdog_last_write_at = (
            float(raw_write) if isinstance(raw_write, (int, float)) and not isinstance(raw_write, bool) else 0.0
        )
        self.watchdog_last_pos = None
        self.watchdog_last_task_steps = task_controller.snapshot().get("steps") if task_controller else None
        self.watchdog_decision_id = None
        self.watchdog_triggered = False
        self.watchdog_reason = None

    def clear_trigger(self) -> None:
        """Clear active watchdog trigger flags at the end of a step cycle."""
        self.watchdog_decision_id = None
        self.watchdog_triggered = False
        self.watchdog_reason = None

    def check_action_watchdog(
        self,
        now: float,
        decision: Any,
        status: Optional[dict[str, Any]],
        *,
        client: Any,
        task_controller: Any = None,
        task_child: bool = False,
        cancelled: bool = False,
        reading_pending: bool = False,
        cancel_callback: Optional[Callable[[], None]] = None,
        latest_status: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Check whether the active action or workflow stalled past the L1 watchdog budget."""
        watchdog_s = self.action_watchdog_seconds
        if (
            watchdog_s <= 0
            or decision is None
            or self.watchdog_triggered
            or cancelled
            or reading_pending
            or decision.name in {"wait", "observe", "goal_complete"}
        ):
            return False

        # First tick observing this decision object initializes progress baselines.
        if self.watchdog_decision_id != id(decision):
            self.watchdog_decision_id = id(decision)
            self.watchdog_started_at = now
            self.watchdog_last_progress_at = now
            self.watchdog_last_activity_at = now
            raw_seq = getattr(client, "activity_seq", None)
            self.watchdog_last_activity_seq = raw_seq if type(raw_seq) is int else None
            raw_write = getattr(client, "last_write_at", 0.0)
            self.watchdog_last_write_at = (
                float(raw_write) if isinstance(raw_write, (int, float)) and not isinstance(raw_write, bool) else 0.0
            )
            self.watchdog_last_task_steps = task_controller.snapshot().get("steps") if task_controller else None
            pos = status.get("pos") if isinstance(status, dict) else None
            self.watchdog_last_pos = (
                [float(v) for v in pos] if isinstance(pos, (list, tuple)) and len(pos) == 3 else None
            )
            return False

        workflow = self.is_workflow_decision(decision, task_child=task_child)
        if workflow:
            # Multi-step workflows (cave scans / shaft mining / scouting) advance via client
            # activity sequences, write timestamps, controller step increments, or 0.45m movement.
            raw_seq = getattr(client, "activity_seq", None)
            act_seq = raw_seq if type(raw_seq) is int else None
            if act_seq is not None and act_seq != self.watchdog_last_activity_seq:
                self.watchdog_last_activity_seq = act_seq
                self.watchdog_last_activity_at = now

            raw_write = getattr(client, "last_write_at", 0.0)
            write_at = (
                float(raw_write) if isinstance(raw_write, (int, float)) and not isinstance(raw_write, bool) else 0.0
            )
            if write_at > self.watchdog_last_write_at:
                self.watchdog_last_write_at = write_at
                self.watchdog_last_progress_at = now

            task_steps = task_controller.snapshot().get("steps") if task_controller else None
            if task_steps is not None and task_steps != self.watchdog_last_task_steps:
                self.watchdog_last_task_steps = task_steps
                self.watchdog_last_progress_at = now

            pos = status.get("pos") if isinstance(status, dict) else None
            if isinstance(pos, (list, tuple)) and len(pos) == 3:
                cur_pos = [float(v) for v in pos]
                if self.watchdog_last_pos is None or math.dist(cur_pos, self.watchdog_last_pos) >= 0.45:
                    self.watchdog_last_pos = cur_pos
                    self.watchdog_last_progress_at = now

            if act_seq is not None and now - self.watchdog_last_activity_at >= watchdog_s:
                elapsed = now - self.watchdog_last_activity_at
            else:
                stall_limit = max(watchdog_s * 3.0, 90.0) if act_seq is not None else watchdog_s
                elapsed = now - self.watchdog_last_progress_at
                if elapsed < stall_limit:
                    return False
        else:
            elapsed = now - self.watchdog_started_at

        if elapsed < watchdog_s:
            return False

        self.watchdog_triggered = True
        self.watchdog_reason = f"{decision.name} timed out after {int(watchdog_s)}s watchdog"
        if callable(cancel_callback):
            cancel_callback()
        self._emit(
            "WARN",
            "ACTION_WATCHDOG_TIMEOUT",
            {
                "decision": decision.name,
                "goal": decision.goal,
                "watchdog_seconds": watchdog_s,
                "elapsed": round(elapsed, 2),
                "workflow": workflow,
                "pos": (status or latest_status or {}).get("pos"),
            },
        )
        try:
            client.stop(timeout_seconds=2.0)
        except Exception as exc:
            self._emit("WARN", "STOP_FAILED", {"code": getattr(exc, "code", type(exc).__name__)})
        return True

    # ------------------------------------------------------------------
    # Tier 2: Cross-Step Livelock & No-Progress Detection
    # ------------------------------------------------------------------

    @staticmethod
    def action_signature(decision: Any) -> Optional[str]:
        """Build a stable canonical signature for task/tool failure deduplication."""
        if decision is None:
            return None
        args = decision.args if isinstance(getattr(decision, "args", None), dict) else {}
        if decision.name == "run_survival_task":
            task = str(args.get("task") or "")
            target = str(args.get("item") or args.get("resource") or "")
            return f"run_survival_task:{task}:{target}"
        if decision.name in {"craft_item", "smelt_start", "place_item"}:
            return f"{decision.name}:{args.get('item') or args.get('input_item') or ''}"
        if decision.name in {"plan_shelter", "build_shelter_step"}:
            return f"{decision.name}"
        return None

    def sync_inventory_counts(self, counts: Any) -> None:
        """Clear bad-args/repeated-failure cooldowns whenever real inventory counts change."""
        if not isinstance(counts, dict):
            return
        digest = json.dumps(counts, sort_keys=True, ensure_ascii=True)
        if self._last_counts_digest is not None and digest != self._last_counts_digest:
            self._signature_failures.clear()
        self._last_counts_digest = digest

    def record_step_outcome(
        self,
        now_mono: float,
        now_wall: float,
        decision: Any,
        result: Any,
        observation: Optional[dict[str, Any]],
        context: dict[str, Any],
    ) -> None:
        """Update L2 thrash, wait-streak, and repeated failure trackers after each step."""
        code = getattr(result, "error_code", None)
        res_data = getattr(result, "data", None)
        preempt_code = res_data.get("preempt_code") if isinstance(res_data, dict) else None
        effective_cancel = code if code in {"llm_cancelled", "llm_preempted"} else preempt_code
        success = bool(getattr(result, "success", False))

        # 1. Detect rapid `llm_cancelled` / `llm_preempted` burst thrash (`>=3` in `15s`).
        if effective_cancel in {"llm_cancelled", "llm_preempted"}:
            self._cancel_timestamps.append(now_mono)
            while self._cancel_timestamps and now_mono - self._cancel_timestamps[0] > 15.0:
                self._cancel_timestamps.popleft()
            if len(self._cancel_timestamps) >= 3 and now_mono >= self._preempt_shield_until:
                self._preempt_shield_until = now_mono + 10.0
                self._emit(
                    "WARN",
                    "LOOP_PREEMPT_SHIELD_ACTIVE",
                    {"burst_count": len(self._cancel_timestamps), "shield_seconds": 10.0, "code": effective_cancel},
                )
        elif success and decision is not None and decision.name not in {"wait", "observe"}:
            # Successful forward action clears prior cancel burst memory.
            self._cancel_timestamps.clear()

        # 2. Track consecutive stationary passive waits to expose L2 livelock alerts.
        status = (observation or {}).get("status") if isinstance(observation, dict) else None
        pos = status.get("pos") if isinstance(status, dict) else None
        cell = (
            (math.floor(pos[0]), math.floor(pos[1]), math.floor(pos[2]))
            if isinstance(pos, (list, tuple)) and len(pos) == 3 and all(finite_number(v) for v in pos)
            else None
        )
        is_passive_wait = (
            decision is not None
            and decision.name == "wait"
            and decision.goal in {"observe", "safety"}
            and status is not None
            and status.get("ready") is True
            and status.get("alive") is True
            and not status.get("busy")
            and not (isinstance(getattr(result, "data", None), dict) and result.data.get("model_blocked"))
        )
        if is_passive_wait:
            seconds = float((decision.args or {}).get("seconds") or 1.0)
            if self._passive_wait_pos is not None and cell is not None and self._passive_wait_pos != cell:
                self._passive_wait_seconds = seconds
                self._passive_wait_count = 1
            else:
                self._passive_wait_seconds += seconds
                self._passive_wait_count += 1
            self._passive_wait_pos = cell
        else:
            self._passive_wait_seconds = 0.0
            self._passive_wait_count = 0
            self._passive_wait_pos = cell

        if self._passive_wait_seconds >= 6.0:
            context["loop_supervisor_alert"] = {
                "type": "stationary_wait_livelock",
                "wait_seconds": round(self._passive_wait_seconds, 1),
                "wait_count": self._passive_wait_count,
                "hint": "Break wait loop: escape, dig/burrow, or move/scout away from current hazard.",
            }
        else:
            context.pop("loop_supervisor_alert", None)

        # 3. Track identical tool/task `bad_args` & `llm_repeated_failure` streaks.
        sig = self.action_signature(decision)
        if sig is not None:
            is_placement_or_shelter = sig.startswith(("place_item:", "plan_shelter", "build_shelter_step"))
            tracked_errors = (
                {"bad_args", "llm_invalid_arguments", "llm_repeated_failure", "unsafe", "unsafe_site", "safety_changed", "place_unverified", "no_place_candidate"}
                if is_placement_or_shelter
                else {"bad_args", "llm_invalid_arguments", "llm_repeated_failure"}
            )
            if not success and code in tracked_errors:
                prev_cnt, _, _ = self._signature_failures.get(sig, (0, 0.0, ""))
                new_cnt = prev_cnt + 1
                cooldown_until = now_wall + 120.0 if new_cnt >= 2 else 0.0
                hint = str(getattr(result, "message", "") or code)
                self._signature_failures[sig] = (new_cnt, cooldown_until, hint)
                if new_cnt == 2:
                    self._emit("WARN", "LOOP_ACTION_COOLDOWN", {"signature": sig, "reason": code, "cooldown_seconds": 120})
            elif success:
                self._signature_failures.pop(sig, None)

        # Export active L2 cooldowns cleanly into context for planner/compiler introspection.
        active_blocked = {
            k: round(exp - now_wall, 1)
            for k, (_, exp, _) in self._signature_failures.items()
            if exp > now_wall
        }
        if active_blocked:
            context["blocked_task_signatures"] = active_blocked
        else:
            context.pop("blocked_task_signatures", None)

    def preemption_shield_active(self, now_mono: float) -> bool:
        """Return True while L2 temporarily shields non-hit planning/actions from jitter cancels."""
        return now_mono < self._preempt_shield_until

    def is_signature_blocked(self, decision: Any, now_wall: float) -> Optional[str]:
        """Return block reason if `decision`'s normalized signature is in L2 cooldown."""
        sig = self.action_signature(decision)
        if sig is None:
            return None
        row = self._signature_failures.get(sig)
        if row and row[1] > now_wall:
            return row[2] or "Repeated identical tool argument failure is in L2 cooldown"
        return None

    @staticmethod
    def should_suspend_drop_recovery(observation: dict[str, Any], context: Optional[dict[str, Any]]) -> bool:
        """Suspend nighttime surface `recover_drops` when dying repeatedly at a hotspot.

        Prevents unarmed night-death spirals (e.g., dying 28 times running back into a mob pile).
        Does not suppress daytime drop recovery or first-time recovery when armored.
        """
        if not isinstance(observation, dict) or not isinstance(context, dict):
            return False
        status = observation.get("status") or {}
        if status.get("is_day") is True:
            return False
        armor = status.get("armor_points", 0) or 0
        recent_cluster = int(context.get("recent_spawn_deaths") or 0)
        death_count = int(context.get("death_count") or 0)
        return bool(
            context.get("spawn_camp_alert") is True
            or recent_cluster >= 2
            or (death_count >= 2 and armor <= 0)
        )
