"""Unified safety and preemption arbiter for observation, cycle gating, and background monitoring.

This module provides a single source of truth (`SafetyArbiter`) shared across:
1. `_observe()` fast-path (`collect_safety` vs full world scan upgrade)
2. `_cycle()` short safety observation resolution (eliminating endless `wait(1)` stalls)
3. `_monitor()` background preemption checks (enforcing baseline symmetry so allowed
   planning/recovery turns are never immediately cancelled by the same initial conditions)
"""

from dataclasses import dataclass
from typing import Any, Optional

from core.campaign import dimension as campaign_dimension
from core.domain import finite_number


# Actions that genuinely resolve or hold an active emergency inside a short safety observation.
ACTIVE_SAFETY_ACTIONS = frozenset({
    "retreat_from",
    "water_escape",
    "emergency_burrow",
    "exit_burrow",
    "clear_burial",
    "fight_hostile",
    "wait",
})


@dataclass(frozen=True)
class ThreatAssessment:
    """Structured safety evaluation result produced by `SafetyArbiter`."""

    danger: bool
    reasons: tuple[str, ...]
    hostile_limit: float
    shallow_water_allowed: bool
    low_health_allowed: bool
    hit: Optional[dict[str, Any]] = None


class SafetyArbiter:
    """Centralizes threat distance, water/health exemptions, and monitor preemption logic."""

    def __init__(
        self,
        safety_rules: Any,
        settings: dict[str, Any],
        *,
        daily: bool = False,
        competition_mode: bool = False,
        llm_override_on_block: bool = False,
        daily_policy: Any = None,
    ) -> None:
        """Initialize the arbiter with shared rule thresholds and mode flags."""
        self.safety_rules = safety_rules
        self.settings = settings
        self.daily = bool(daily)
        self.competition_mode = bool(competition_mode)
        self.llm_override_on_block = bool(llm_override_on_block)
        self.daily_policy = daily_policy

    def hostile_threat_limit(
        self,
        status: dict[str, Any],
        hostile: dict[str, Any],
        decision: Any = None,
        *,
        planning: bool = False,
    ) -> float:
        """Return the unified hostile proximity radius for the active mode/decision."""
        raw_limit = self.safety_rules._threat_distance(hostile.get("name"))
        health = status.get("health", 20)
        if decision is not None and getattr(decision, "goal", None) == "deep_mine" and health >= 16:
            return min(5.0, raw_limit)
        if self.llm_override_on_block and health >= 14:
            # Keep planning and execution aligned when LLM override mode tolerates distant mobs.
            return min(4.5, raw_limit)
        return raw_limit

    def should_use_short_safety(
        self,
        short: dict[str, Any],
        recovery_state: dict[str, Any],
        *,
        safety_only: bool = False,
        safety_wait_streak: int = 0,
    ) -> tuple[bool, bool]:
        """Decide whether a short `collect_safety()` snapshot is sufficient for this tick.

        Returns:
            `(use_short, close)`:
            - `use_short`: True to return the short safety snapshot directly; False to upgrade
              into a full observation (`collect()`).
            - `close`: True if a close hostile threat was observed in the short scan.
        """
        status = short.get("status") or {}
        hostiles = self.safety_rules._dangerous_hostiles(short, status.get("pos"))
        nearby = status.get("nearest_hostile") or {}
        close = bool(
            hostiles
            or (
                isinstance(nearby, dict)
                and self.safety_rules._number(nearby.get("dist"))
                and nearby["dist"] <= self.safety_rules._threat_distance(nearby.get("name"))
            )
        )
        # If the previous >=2 consecutive short safety ticks ended in passive wait(1)
        # without an unready/busy state or head burial, force a full observation so
        # the planner/task runner is never starved by a distant or occluded hostile.
        if (
            not safety_only
            and safety_wait_streak >= 2
            and not short.get("buried_head")
            and status.get("ready") is True
            and status.get("alive") is True
            and not status.get("busy")
        ):
            return False, close

        needs_inventory = bool(
            self.safety_rules._number(status.get("health"))
            and self.safety_rules._number(status.get("food"))
            and status.get("on_ground") is True
            and not status.get("is_sleeping")
            and not status.get("in_vehicle")
            and not any(status.get(key) for key in ("in_water", "in_lava", "on_fire", "submerged"))
            and status.get("dimension") in {"overworld", "minecraft:overworld"}
            and status.get("gamemode") == "survival"
            and status["food"] < 20
            and (
                status["health"] <= self.safety_rules.low_health
                or status["food"] <= self.safety_rules.food_threshold
            )
        )
        quiet_evacuation = recovery_state.get("evacuating") is True and not close
        use_short = bool(
            safety_only
            or short.get("buried_head")
            or close
            or (not needs_inventory and not quiet_evacuation)
            or not status.get("ready")
            or not status.get("alive")
            or status.get("busy")
        )
        return use_short, close

    @staticmethod
    def is_passive_safety_wait(decision: Any) -> bool:
        """Return True when the short-safety tick merely stalled on a no-op production wait."""
        if decision is None or getattr(decision, "name", None) != "wait":
            return False
        reason = str(getattr(decision, "reason", "") or "")
        return (
            "Short safety observation cannot authorize production" in reason
            or "No verified retreat route" in reason
            or "Nearby hostile has no fresh position" in reason
        )

    @staticmethod
    def is_admitted_short_decision(decision: Any) -> bool:
        """Check whether a local safety decision can execute directly from a short snapshot."""
        if decision is None:
            return False
        if decision.name in ACTIVE_SAFETY_ACTIONS:
            return True
        if decision.name == "campaign_action" and (decision.args or {}).get("operation") in {
            "emergency_food",
            "defend_melee",
            "escape_pressure",
        }:
            return True
        return False

    def evaluate_monitor(
        self,
        status: dict[str, Any],
        baseline: dict[str, Any],
        *,
        decision: Any = None,
        planning: bool = False,
        defending: bool = False,
        attack: Optional[dict[str, Any]] = None,
        constraints: Optional[dict[str, Any]] = None,
        active_relaxed_work: Any = None,
        task_child: bool = False,
        context: Optional[dict[str, Any]] = None,
    ) -> ThreatAssessment:
        """Evaluate whether a live action or pending model request must be interrupted.

        Invariant:
            Conditions already accepted when entering a turn (`baseline`) must not trigger
            self-cancellation 50ms later unless an actual deterioration occurred (new hit,
            health drop, fatal hazard, broken relaxed permit, or hostile closing in).
        """
        hostile = status.get("nearest_hostile") or {}
        if not isinstance(hostile, dict):
            hostile = {}
        hostile_limit = self.hostile_threat_limit(status, hostile, decision, planning=planning)

        # Eating or sleeping turns only preempt on a confirmed incoming attack.
        if decision is not None and decision.name in {"eat_food", "auto_sleep"} and not attack:
            return ThreatAssessment(
                danger=False,
                reasons=(),
                hostile_limit=hostile_limit,
                shallow_water_allowed=False,
                low_health_allowed=True,
                hit=None,
            )

        constraints = constraints or {}
        context = context or {}

        food_turn = False
        if self.daily and constraints.get("scenario") == "low_health":
            from core.daily_policy import FOOD_ACTIONS

            food_turn = (
                (planning and constraints.get("model_allowed") is True)
                or (decision is not None and decision.name in FOOD_ACTIONS)
            ) and (
                status.get("dimension") == baseline.get("dimension")
                and status.get("last_death") == baseline.get("last_death")
                and status.get("health", 0) >= baseline.get("health", 20)
                and status.get("food", 0) >= baseline.get("food", 20)
                and status.get("pos") == baseline.get("pos")
                and status.get("on_ground") is True
                and status.get("is_day") is True
                and not status.get("thundering")
                and status.get("time_of_day", 24000) < 11000
            )

        forage_jump = False
        if decision is not None and decision.name == "position_for_task" and decision.goal == "food_recovery":
            start = self.safety_rules._pos(baseline.get("pos"))
            target = self.safety_rules._pos(decision.args.get("pos"))
            current = self.safety_rules._pos(status.get("pos"))
            forage_jump = bool(
                start
                and target
                and current
                and abs(start[1] - target[1]) == 1
                and all(min(start[i], target[i]) - 0.3 <= current[i] <= max(start[i], target[i]) + 0.3 for i in (0, 2))
                and min(start[1], target[1]) - 0.05 <= current[1] <= max(start[1], target[1]) + 1.3
            )

        local_recovery_turn = bool(
            constraints.get("scenario") in {"food_recovery", "cautious_bootstrap"}
            and (
                (planning and constraints.get("model_allowed") is True)
                or (
                    decision is not None
                    and (decision.goal == "food_recovery" or context.get("cautious_bootstrap") is True)
                )
            )
            and status.get("dimension") == baseline.get("dimension")
            and status.get("last_death") == baseline.get("last_death")
            and status.get("health", 0) >= baseline.get("health", 20)
            and status.get("food", 0) >= 12
            and (status.get("on_ground") is True or forage_jump)
        )

        combat_action = decision is not None and decision.name == "fight_hostile"
        campaign_encounter = bool(
            decision is not None
            and decision.name == "campaign_action"
            and (
                (decision.args.get("operation") == "loot_mob" and hostile.get("name") == decision.args.get("mob"))
                or (decision.args.get("operation") == "dragon" and hostile.get("name") == "ender_dragon")
            )
        )
        campaign_food = bool(
            decision is not None
            and decision.name == "campaign_action"
            and decision.args.get("operation") in {"emergency_food", "recover_gear"}
            and status.get("health", 0) >= baseline.get("health", 20)
        )

        permit = active_relaxed_work if decision is not None else None
        relaxed_stable = permit is not None and permit.stable(status, require_idle=False)
        advisory_stable = bool(
            self.daily_policy is not None
            and (self.daily_policy.advisory or self.competition_mode)
            and constraints.get("scenario") == "risk_advisory"
            and finite_number(status.get("health"))
            and finite_number(baseline.get("health"))
            and status["health"] >= baseline["health"]
            and status.get("dimension") == baseline.get("dimension")
            and status.get("last_death") == baseline.get("last_death")
        )

        combat_changed = bool(
            combat_action
            and (
                status.get("dimension") != baseline.get("dimension")
                or status.get("last_death") != baseline.get("last_death")
                or status.get("health", 0) < baseline.get("health", 20)
            )
        )

        surface_expedition = bool(
            task_child and decision is not None and decision.goal in {"deep_mine", "scout_surface"}
        )
        shallow_water_allowed = bool(
            surface_expedition
            or (
                self.llm_override_on_block
                and (planning or (decision is not None and decision.name not in {"water_escape", "auto_sleep"}))
            )
        )

        # Symmetry fix: `llm_override_on_block` must exempt non-dropping health BOTH while an
        # LLM decision executes AND while `planning=True` (when `decision is None`), stopping
        # the millisecond self-cancellation death spiral seen on low-health bots without food.
        low_health_override = bool(
            self.llm_override_on_block
            and (
                planning
                or (decision is not None and decision.name not in {"water_escape", "auto_sleep"})
            )
            and status.get("dimension") == baseline.get("dimension")
            and status.get("last_death") == baseline.get("last_death")
            and finite_number(baseline.get("health"))
            and finite_number(status.get("health"))
            and status["health"] >= baseline["health"]
        )
        low_health_allowed = bool(
            food_turn
            or local_recovery_turn
            or relaxed_stable
            or advisory_stable
            or campaign_food
            or low_health_override
        )

        hit_preempts = attack is not None and not defending
        submerged_danger = bool(
            status.get("submerged")
            and (
                not shallow_water_allowed
                or (finite_number(status.get("oxygen")) and float(status.get("oxygen", 20)) <= 12)
            )
        )

        sealed_burrow = False
        if self.daily_policy is not None and callable(getattr(self.daily_policy, "_in_active_night_burrow", None)):
            sealed_burrow = bool(self.daily_policy._in_active_night_burrow({"status": status}, context))
        elif isinstance(context.get("night_burrow"), dict):
            burrow = context["night_burrow"]
            bpos = burrow.get("pos") or burrow.get("burrow_feet")
            pos_now = status.get("pos")
            sealed_burrow = bool(
                burrow.get("generation") == context.get("life_generation")
                and isinstance(bpos, (list, tuple))
                and len(bpos) == 3
                and isinstance(pos_now, (list, tuple))
                and len(pos_now) == 3
                and abs(pos_now[0] - bpos[0]) <= 2.5
                and abs(pos_now[2] - bpos[2]) <= 2.5
                and pos_now[1] <= bpos[1] + 1.5
            )
        burrow_safe = bool(
            sealed_burrow
            and not hit_preempts
            and not status.get("in_lava")
            and not status.get("on_fire")
            and not status.get("in_water")
            and finite_number(baseline.get("health"))
            and finite_number(status.get("health"))
            and status["health"] >= baseline["health"]
            and (not finite_number(status.get("oxygen")) or float(status.get("oxygen", 20)) >= 16)
        )

        reasons: list[str] = []
        if hit_preempts:
            reasons.append("hit_preempt")
        if not status.get("alive"):
            reasons.append("not_alive")
        if status.get("in_lava"):
            reasons.append("in_lava")
        if status.get("on_fire"):
            reasons.append("on_fire")
        if submerged_danger:
            reasons.append("submerged_danger")
        if status.get("in_water") and not shallow_water_allowed:
            reasons.append("in_water")
        if (
            finite_number(status.get("health"))
            and status["health"] <= self.settings["low_health_threshold"]
            and not low_health_allowed
        ):
            reasons.append("low_health_deteriorated")
        if permit is not None and not relaxed_stable:
            reasons.append("relaxed_permit_broken")
        if combat_changed:
            reasons.append("combat_changed")
        if (
            not (combat_action or campaign_encounter or burrow_safe)
            and finite_number(hostile.get("dist"))
            and hostile["dist"] <= hostile_limit
        ):
            reasons.append("hostile_within_limit")

        return ThreatAssessment(
            danger=bool(reasons),
            reasons=tuple(reasons),
            hostile_limit=hostile_limit,
            shallow_water_allowed=shallow_water_allowed,
            low_health_allowed=low_health_allowed,
            hit=attack if hit_preempts else None,
        )
