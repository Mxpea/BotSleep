"""Safe, guarded procedural action workflow engine for Standard Structures.

Provides semantic action primitives (stand_at, aim_at, place_block, dig_block,
use_on, consume, wait_until), bounded local `if` recovery branches, strict
posture restoration, and structured AI handoff diagnostics while honoring
BotSleep's safety cancellation and uncertain-write stop-loss contracts.
"""

from dataclasses import dataclass, field
import inspect
import math
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from core.client import APIError
from core.domain import AIR, HAZARDS, parse_pos
from core.structure_spec import (
    CAVE_DIGGABLE_BLOCKS,
    DEFAULT_REPLACEABLE,
    MINE_DIGGABLE_BLOCKS,
    SCAFFOLD_BLOCKS,
    StructureSpec,
    inspect_structure,
    normalize_orientation,
    transform_rel_pos,
    transform_rel_vec,
)
from skills import SkillResult


UNKNOWN_ERROR_CODES = frozenset({
    "request_timeout",
    "network_error",
    "invalid_json",
    "invalid_response",
    "dig_unconfirmed",
    "action_unconfirmed",
    "internal",
    "internal_error",
})

PICKAXE_PRIORITY: Tuple[str, ...] = (
    "netherite_pickaxe",
    "diamond_pickaxe",
    "iron_pickaxe",
    "stone_pickaxe",
    "golden_pickaxe",
    "wooden_pickaxe",
)


class WorkflowUncertainError(Exception):
    """Raised on an ambiguous write/transport failure; never retry or branch."""

    def __init__(self, code: str, message: str, data: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.data = dict(data or {})
        self.data.setdefault("action_state", "unknown")


class WorkflowStepError(Exception):
    """Raised on a deterministic, non-ambiguous workflow failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        blocker_type: str = "verification_failed",
        blocker_pos: Optional[List[int]] = None,
        blocker_block: Optional[str] = None,
        suggested_recovery: str = "",
        data: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.code = code
        self.blocker_type = blocker_type
        self.blocker_pos = list(blocker_pos) if blocker_pos is not None else None
        self.blocker_block = blocker_block
        self.suggested_recovery = suggested_recovery
        self.data = dict(data or {})


class WorkflowGuard:
    """Per-step cancellation, budget, life-continuity, and posture guard."""

    def __init__(
        self,
        client: Any,
        *,
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: float = 180.0,
        max_reads: int = 1200,
        allow_damage: bool = False,
        damage_tolerance: float = 0.0,
        critical_health: float = 6.0,
    ):
        self.client = client
        self.cancel_event = cancel_event
        self.deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        self.max_reads = max(10, int(max_reads))
        self.allow_damage = allow_damage
        self.damage_tolerance = max(0.0, float(damage_tolerance))
        self.critical_health = max(0.0, float(critical_health))
        self.read_calls = 0
        self.writes = 0
        self.life_marker: Optional[Tuple[Any, Any]] = None
        self.initial_health: Optional[float] = None
        self.last_health: Optional[float] = None
        self.sneaking = False
        self.jumping = False
        self.uncertain_error: Optional[WorkflowUncertainError] = None

    def fail_uncertain(self, code: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
        details = dict(data or {})
        details.setdefault("action_state", "unknown" if self.writes else "not_executed")
        if details["action_state"] == "unknown":
            details.setdefault("cleanup_skipped", "disconnected_unknown" if code == "not_connected" else "workflow_unknown")
        if code == "not_connected" and self.writes:
            details["api_error_code"] = code
            code = "action_unconfirmed"
        self.uncertain_error = WorkflowUncertainError(code, message, details)
        raise self.uncertain_error

    @property
    def move_stop_on(self) -> Optional[List[str]]:
        """Override mc-agent-api default ['hurt', 'death'] when minor chip damage is tolerated."""
        if self.allow_damage or self.damage_tolerance > 0:
            return ["death"]
        return None

    def optional_stop_on_kwargs(self, fn: Any) -> Dict[str, Any]:
        """Return {'stop_on': [...]} only if fn accepts stop_on and guard tolerates chip damage."""
        stop_on = self.move_stop_on
        if stop_on is None:
            return {}
        try:
            sig = inspect.signature(fn)
            if "stop_on" in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                return {"stop_on": list(stop_on)}
        except (TypeError, ValueError):
            pass
        return {}

    def check_budget(self) -> None:
        if self.uncertain_error is not None:
            raise self.uncertain_error
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise WorkflowStepError(
                "cancelled",
                "Workflow cancelled by safety monitor or user",
                blocker_type="safety_interrupted",
                suggested_recovery="reobserve_and_assess_safety",
            )
        if time.monotonic() >= self.deadline:
            raise WorkflowStepError(
                "workflow_budget_exhausted",
                "Workflow time budget expired",
                blocker_type="safety_interrupted",
                suggested_recovery="inspect_progress_and_resume",
            )

    def remaining_timeout(self, cap: float = 15.0) -> float:
        return min(cap, max(0.05, self.deadline - time.monotonic()))

    def read(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        self.check_budget()
        if self.read_calls >= self.max_reads:
            raise WorkflowStepError(
                "workflow_budget_exhausted",
                "Workflow block/status read limit reached",
                blocker_type="safety_interrupted",
                suggested_recovery="inspect_progress_and_resume",
            )
        self.read_calls += 1
        try:
            result = fn(*args, **kwargs)
        except APIError as exc:
            if (exc.code in UNKNOWN_ERROR_CODES or (exc.code == "not_connected" and self.writes)
                    or (isinstance(exc.details, dict) and exc.details.get("action_state") == "unknown")):
                self.fail_uncertain(
                    exc.code,
                    str(exc),
                    {"action_state": "unknown" if self.writes else "not_executed", "api_details": exc.details},
                )
            raise WorkflowStepError(
                exc.code,
                str(exc),
                blocker_type="verification_failed",
                data={"api_details": exc.details},
            ) from None
        self.check_budget()
        return result

    def write(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        self.check_budget()
        self.writes += 1
        try:
            response = fn(*args, **kwargs)
        except APIError as exc:
            if (exc.code in UNKNOWN_ERROR_CODES or exc.code == "not_connected"
                    or (isinstance(exc.details, dict) and exc.details.get("action_state") == "unknown")):
                self.fail_uncertain(
                    exc.code,
                    str(exc),
                    {"action_state": "unknown", "api_details": exc.details},
                )
            raise WorkflowStepError(
                exc.code,
                str(exc),
                blocker_type="verification_failed",
                data={"api_details": exc.details},
            ) from None
        if isinstance(response, dict) and response.get("ok") is False:
            err = response.get("error") if isinstance(response.get("error"), dict) else {}
            code = str(err.get("code") or "api_error")
            msg = str(err.get("message") or "Write action rejected by API")
            details = err.get("details") or {}
            if (code in UNKNOWN_ERROR_CODES or code == "not_connected"
                    or (isinstance(details, dict) and details.get("action_state") == "unknown")):
                self.fail_uncertain(code, msg, {"action_state": "unknown", "api_details": details})
            raise WorkflowStepError(code, msg, blocker_type="verification_failed")
        return response

    def check_status(self, *, require_safe: bool = True) -> Dict[str, Any]:
        envelope = self.read(self.client.get_status, timeout_seconds=self.remaining_timeout(5.0))
        status = envelope.get("result") if isinstance(envelope, dict) else None
        if not isinstance(status, dict):
            self.fail_uncertain("invalid_response", "Status response missing result object")
        if status.get("alive") is False:
            raise WorkflowStepError(
                "dead",
                "Bot died during workflow execution",
                blocker_type="safety_interrupted",
                suggested_recovery="recover_after_respawn",
            )
        if status.get("ready") is False:
            raise WorkflowStepError(
                "not_ready",
                "Bot is not ready",
                blocker_type="safety_interrupted",
                suggested_recovery="wait_for_ready",
            )

        marker = (status.get("dimension"), status.get("last_death"))
        if self.life_marker is None:
            self.life_marker = marker
        elif marker != self.life_marker:
            raise WorkflowStepError(
                "life_changed",
                "Dimension or death state changed during workflow",
                blocker_type="safety_interrupted",
                suggested_recovery="reobserve_new_state",
            )

        health = status.get("health")
        if isinstance(health, (int, float)) and not isinstance(health, bool) and math.isfinite(health):
            cur_health = float(health)
            self.last_health = cur_health
            if self.initial_health is None:
                self.initial_health = cur_health
            elif self.allow_damage:
                if cur_health <= self.critical_health:
                    raise WorkflowStepError(
                        "damage_taken",
                        f"Health dropped critically to {health} <= {self.critical_health}",
                        blocker_type="safety_interrupted",
                        suggested_recovery="retreat_and_heal",
                    )
            elif self.damage_tolerance > 0:
                if cur_health <= self.critical_health or (self.initial_health - cur_health) > self.damage_tolerance:
                    raise WorkflowStepError(
                        "damage_taken",
                        f"Health dropped from {self.initial_health} to {health}",
                        blocker_type="safety_interrupted",
                        suggested_recovery="retreat_and_heal",
                    )
            elif cur_health < self.initial_health:
                raise WorkflowStepError(
                    "damage_taken",
                    f"Health dropped from {self.initial_health} to {health}",
                    blocker_type="safety_interrupted",
                    suggested_recovery="retreat_and_heal",
                )

        if require_safe:
            if status.get("in_lava") or status.get("on_fire"):
                raise WorkflowStepError(
                    "unsafe_state",
                    "Bot is in lava or on fire",
                    blocker_type="safety_interrupted",
                    suggested_recovery="retreat_from_hazard",
                )
        return status

    def set_sneak(self, enabled: bool) -> None:
        if self.sneaking == enabled:
            return
        self.write(self.client.posture, sneak=enabled, jump=self.jumping)
        self.sneaking = enabled

    def set_jump(self, enabled: bool) -> None:
        if self.jumping == enabled:
            return
        self.write(self.client.posture, sneak=self.sneaking, jump=enabled)
        self.jumping = enabled

    def restore_posture(self) -> None:
        if self.uncertain_error is not None:
            return
        if not self.sneaking and not self.jumping:
            return
        try:
            self.client.posture(sneak=False, jump=False)
        except Exception:
            pass
        finally:
            self.sneaking = False
            self.jumping = False


@dataclass
class WorkflowContext:
    """Shared execution state across all stages of a Standard Action workflow."""

    guard: WorkflowGuard
    spec: StructureSpec
    origin: List[int]
    orientation: str
    player_start: Optional[List[float]] = None
    player_end: Optional[List[float]] = None
    completed_steps: List[str] = field(default_factory=list)
    completed_stages: List[str] = field(default_factory=list)
    step_logs: List[Dict[str, Any]] = field(default_factory=list)
    structure_verified: bool = False
    end_move_error: Optional[Dict[str, Any]] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def world_pos(self, rel_pos: Sequence[float], *, integer: Optional[bool] = None) -> List[float]:
        return transform_rel_pos(rel_pos, self.origin, self.orientation, integer=integer)

    def world_vec(self, rel_vec: Sequence[float]) -> List[int]:
        return transform_rel_vec(rel_vec, self.orientation)

    def anchor_world(self, anchor_name: str) -> List[float]:
        anchor = self.spec.anchors[anchor_name]
        return self.world_pos(anchor.rel_pos, integer=False)

    def read_block(self, world_pos: Sequence[int]) -> Dict[str, Any]:
        raw = self.guard.read(self.client_get_block, list(world_pos))
        block = raw.get("result") if isinstance(raw, dict) and "result" in raw else raw
        if not isinstance(block, dict):
            self.guard.fail_uncertain("invalid_response", f"Invalid block response at {world_pos}")
        return block

    def client_get_block(self, world_pos: Sequence[int]) -> Dict[str, Any]:
        return self.guard.client.get_block(int(world_pos[0]), int(world_pos[1]), int(world_pos[2]))

    def read_rel_block(self, rel_pos: Sequence[int]) -> Dict[str, Any]:
        return self.read_block(self.world_pos(rel_pos, integer=True))

    def inventory_counts(self) -> Dict[str, int]:
        inv_fn = getattr(self.guard.client, "inventory", None)
        if callable(inv_fn):
            envelope = self.guard.read(inv_fn, timeout_seconds=self.guard.remaining_timeout(10.0))
            result = envelope.get("result") if isinstance(envelope, dict) else None
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                self.guard.fail_uncertain("invalid_response", "Inventory response missing items list")
            counts: Dict[str, int] = {}
            for row in result["items"]:
                if isinstance(row, dict) and isinstance(row.get("name"), str):
                    cnt = row.get("count", 0)
                    if isinstance(cnt, int) and cnt > 0:
                        counts[row["name"]] = counts.get(row["name"], 0) + cnt
            return counts
        counts = self.guard.read(self.guard.client.get_inventory_counts)
        return dict(counts or {})

    def select_item(self, candidates: Union[str, Iterable[str]]) -> Optional[str]:
        allowed = [candidates] if isinstance(candidates, str) else list(candidates)
        counts = self.inventory_counts()
        for name in allowed:
            if counts.get(name, 0) > 0:
                return name
        return None


@dataclass(frozen=True)
class ActionStep:
    """One declarative step inside an ActionWorkflow."""

    step_id: str
    description: str
    action: Callable[[WorkflowContext], Any]
    stage: Optional[str] = None
    skip_if: Optional[Callable[[WorkflowContext], bool]] = None
    branches: Dict[str, Callable[[WorkflowContext, WorkflowStepError], bool]] = field(default_factory=dict)
    max_branch_attempts: int = 2


# ---------------------------------------------------------------------------
# Semantic action primitives (all guarded & state-restoring)
# ---------------------------------------------------------------------------

def wait_until(
    ctx: WorkflowContext,
    predicate: Callable[[WorkflowContext], bool],
    *,
    timeout_seconds: float = 5.0,
    poll_seconds: float = 0.25,
    description: str = "condition",
) -> bool:
    """Poll predicate until True or timeout; always checks cancellation/budget."""
    deadline = min(ctx.guard.deadline, time.monotonic() + max(0.05, float(timeout_seconds)))
    while True:
        ctx.guard.check_status(require_safe=True)
        if predicate(ctx):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(poll_seconds, remaining))


def stand_at(
    ctx: WorkflowContext,
    target: Union[str, Sequence[float]],
    *,
    relative: bool = True,
    tolerance: Optional[float] = None,
    exact: bool = False,
    range_param: float = 0.8,
) -> Dict[str, Any]:
    """Move to a named anchor or coordinate and verify actual arrival distance."""
    if isinstance(target, str):
        anchor = ctx.spec.anchors[target]
        world_target = ctx.world_pos(anchor.rel_pos, integer=False)
        eff_tol = anchor.tolerance if tolerance is None else float(tolerance)
        exact = exact or anchor.exact_cell
    else:
        world_target = ctx.world_pos(target, integer=False) if relative else list(target)
        eff_tol = 0.8 if tolerance is None else float(tolerance)

    status = ctx.guard.check_status(require_safe=True)
    pos = parse_pos(status.get("pos"))
    if pos is None:
        ctx.guard.fail_uncertain("invalid_response", "Status missing valid pos")

    y_tol = 1.3 if eff_tol >= 1.5 else 0.85

    def _dist(cur: List[float]) -> float:
        return math.hypot(cur[0] - world_target[0], cur[2] - world_target[2])

    def _y_ok(cur: List[float]) -> bool:
        return abs(cur[1] - world_target[1]) <= y_tol

    def _in_cell(cur: List[float]) -> bool:
        return (
            math.floor(cur[0] + 0.0001) == math.floor(world_target[0] + 0.0001)
            and math.floor(cur[2] + 0.0001) == math.floor(world_target[2] + 0.0001)
        )

    cur_dist = _dist(pos)
    if cur_dist <= eff_tol and _y_ok(pos) and (not exact or _in_cell(pos)):
        return {"skipped": True, "pos": pos, "target": world_target, "dist": round(cur_dist, 3)}

    init_range = round(max(0.0, min(float(range_param), float(eff_tol)) - 0.5), 3)
    stop_kw = ctx.guard.optional_stop_on_kwargs(ctx.guard.client.move_to)
    ctx.guard.write(
        ctx.guard.client.move_to,
        world_target[0],
        world_target[1],
        world_target[2],
        range=init_range,
        **stop_kw,
    )
    after = ctx.guard.check_status(require_safe=True)
    after_pos = parse_pos(after.get("pos")) or pos
    after_dist = _dist(after_pos)

    # Bounded micro-reposition branch if pathfinder stopped slightly short.
    if after_dist > eff_tol or not _y_ok(after_pos) or (exact and not _in_cell(after_pos)):
        if after_dist <= max(2.0, eff_tol * 2.5):
            ctx.guard.write(
                ctx.guard.client.move_to,
                world_target[0],
                world_target[1],
                world_target[2],
                range=0.0,
                **stop_kw,
            )
            after = ctx.guard.check_status(require_safe=True)
            after_pos = parse_pos(after.get("pos")) or after_pos
            after_dist = _dist(after_pos)

    if after_dist > eff_tol or not _y_ok(after_pos) or (exact and not _in_cell(after_pos)):
        raise WorkflowStepError(
            "unreachable_stance",
            f"Failed to reach stance {world_target} (arrived at {after_pos}, dist={after_dist:.2f} > {eff_tol}, dy={abs(after_pos[1] - world_target[1]):.2f})",
            blocker_type="unreachable",
            blocker_pos=[int(math.floor(world_target[0])), int(math.floor(world_target[1])), int(math.floor(world_target[2]))],
            suggested_recovery=f"clear_path_or_reposition_near {world_target}",
        )
    return {"skipped": False, "pos": after_pos, "target": world_target, "dist": round(after_dist, 3)}


def stand_cell(
    ctx: WorkflowContext,
    cell_pos: Sequence[int],
    *,
    tolerance: float = 0.85,
    range_param: float = 0.25,
) -> Dict[str, Any]:
    """Stand on the center (+0.5 XZ) of an integer world block coordinate."""
    return stand_at(
        ctx,
        [int(cell_pos[0]) + 0.5, float(int(cell_pos[1])), int(cell_pos[2]) + 0.5],
        relative=False,
        tolerance=tolerance,
        range_param=range_param,
    )


def aim_at(ctx: WorkflowContext, rel_pos: Sequence[float], *, relative: bool = True) -> Dict[str, Any]:
    """Look at the center of a relative or world block coordinate."""
    world = ctx.world_pos(rel_pos, integer=False) if relative else list(rel_pos)
    return ctx.guard.write(ctx.guard.client.look_at, world[0] + 0.5, world[1] + 0.5, world[2] + 0.5)


def place_block(
    ctx: WorkflowContext,
    items: Union[str, Iterable[str]],
    rel_pos: Sequence[int],
    *,
    relative: bool = True,
    sneak: bool = False,
    replaceable: Iterable[str] = DEFAULT_REPLACEABLE,
    verify_timeout: float = 1.5,
) -> Dict[str, Any]:
    """Idempotently place a block from an item/tag set and verify placement."""
    allowed = {items} if isinstance(items, str) else set(items)
    replaceable_set = set(replaceable)
    world = [int(v) for v in (ctx.world_pos(rel_pos, integer=True) if relative else rel_pos)]

    current = str(ctx.read_block(world).get("name") or "air")
    if current in allowed:
        return {"skipped": True, "world_pos": world, "block": current}
    if current not in replaceable_set:
        raise WorkflowStepError(
            "target_obstructed",
            f"Cannot place at {world}: occupied by {current}",
            blocker_type="obstructed",
            blocker_pos=world,
            blocker_block=current,
            suggested_recovery=f"dig_or_clear_obstacle_at {world}",
        )

    chosen = ctx.select_item(items if not isinstance(items, str) else [items])
    if chosen is None:
        raise WorkflowStepError(
            "missing_building_block",
            f"Inventory lacks any of {sorted(allowed)}",
            blocker_type="missing_item",
            suggested_recovery=f"acquire_item {sorted(allowed)}",
        )

    try:
        if sneak:
            ctx.guard.set_sneak(True)
        stop_kw = ctx.guard.optional_stop_on_kwargs(ctx.guard.client.place_block)
        ctx.guard.write(ctx.guard.client.place_block, item=chosen, x=world[0], y=world[1], z=world[2], **stop_kw)
    finally:
        if sneak:
            ctx.guard.restore_posture()

    verified = wait_until(
        ctx,
        lambda c: str(c.read_block(world).get("name") or "air") in allowed,
        timeout_seconds=verify_timeout,
        poll_seconds=0.2,
        description=f"place {chosen} at {world}",
    )
    after = str(ctx.read_block(world).get("name") or "air")
    if not verified:
        raise WorkflowStepError(
            "place_failed",
            f"Placed {chosen} at {world}, but observed {after}",
            blocker_type="verification_failed",
            blocker_pos=world,
            blocker_block=after,
            suggested_recovery=f"reposition_and_retry_place_at {world}",
        )
    return {"skipped": False, "world_pos": world, "item": chosen, "block": after}


def place_torch(
    ctx: WorkflowContext,
    rel_pos: Sequence[int],
    *,
    relative: bool = True,
    verify_timeout: float = 1.5,
) -> Dict[str, Any]:
    """Place a torch at rel_pos and accept either standing torch or wall_torch on verification."""
    torch_names = ("torch", "wall_torch")
    world = [int(v) for v in (ctx.world_pos(rel_pos, integer=True) if relative else rel_pos)]
    current = str(ctx.read_block(world).get("name") or "air")
    if current in torch_names:
        return {"skipped": True, "world_pos": world, "block": current}
    if ctx.select_item(["torch"]) is None:
        raise WorkflowStepError(
            "missing_torch",
            "Inventory lacks 'torch' to light tunnel or cave.",
            blocker_type="missing_item",
            suggested_recovery="craft_or_collect_torch",
        )
    ctx.guard.write(ctx.guard.client.place_block, item="torch", x=world[0], y=world[1], z=world[2])
    verified = wait_until(
        ctx,
        lambda c: str(c.read_block(world).get("name") or "air") in torch_names,
        timeout_seconds=verify_timeout,
        poll_seconds=0.2,
        description=f"place torch at {world}",
    )
    after = str(ctx.read_block(world).get("name") or "air")
    if not verified:
        raise WorkflowStepError(
            "place_failed",
            f"Placed torch at {world}, but observed {after}",
            blocker_type="verification_failed",
            blocker_pos=world,
            blocker_block=after,
            suggested_recovery=f"reposition_and_retry_torch_at {world}",
        )
    return {"skipped": False, "world_pos": world, "item": "torch", "block": after}


def dig_block(
    ctx: WorkflowContext,
    rel_pos: Sequence[int],
    *,
    relative: bool = True,
    allowed_blocks: Optional[Iterable[str]] = None,
    collect: bool = False,
    verify_timeout: float = 1.5,
    tool_priority: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Safely equip an appropriate pickaxe, dig a target block, and verify removal."""
    world = [int(v) for v in (ctx.world_pos(rel_pos, integer=True) if relative else rel_pos)]
    status = ctx.guard.check_status(require_safe=True)
    bot_pos = parse_pos(status.get("pos"))
    if bot_pos is not None:
        feet_floor = (math.floor(bot_pos[0] + 0.0001), math.floor(bot_pos[1] + 0.0001) - 1, math.floor(bot_pos[2] + 0.0001))
        if tuple(world) == feet_floor:
            raise WorkflowStepError(
                "unsafe_dig_floor",
                f"Refusing to dig bot's supporting floor cell {world}",
                blocker_type="safety_interrupted",
                blocker_pos=world,
                suggested_recovery="move_off_block_before_digging",
            )

    current = str(ctx.read_block(world).get("name") or "air")
    if current in AIR:
        return {"skipped": True, "world_pos": world, "block": current}

    if allowed_blocks is not None and current not in set(allowed_blocks):
        raise WorkflowStepError(
            "unexpected_block",
            f"Block at {world} is {current}, outside allowed dig list",
            blocker_type="obstructed",
            blocker_pos=world,
            blocker_block=current,
            suggested_recovery=f"inspect_and_handle_block_at {world}",
        )

    pickaxe = ctx.select_item(tool_priority or PICKAXE_PRIORITY)
    if pickaxe is not None:
        ctx.guard.write(ctx.guard.client.equip, item=pickaxe, slot="hand")

    ctx.guard.write(ctx.guard.client.dig, world[0], world[1], world[2], collect=collect, stop_on=["death"])
    cleared = wait_until(
        ctx,
        lambda c: str(c.read_block(world).get("name") or "air") != current,
        timeout_seconds=verify_timeout,
        poll_seconds=0.2,
        description=f"dig {current} at {world}",
    )
    after = str(ctx.read_block(world).get("name") or "air")
    if not cleared:
        raise WorkflowStepError(
            "dig_failed",
            f"Failed to dig {current} at {world}, still {after}",
            blocker_type="verification_failed",
            blocker_pos=world,
            blocker_block=after,
            suggested_recovery=f"verify_pickaxe_and_reach_for {world}",
        )
    return {"skipped": False, "world_pos": world, "dug": current, "after": after}


def pillar_up(
    ctx: WorkflowContext,
    steps: int = 1,
    *,
    blocks: Iterable[str] = SCAFFOLD_BLOCKS,
    allow_dig_headroom: bool = True,
    allowed_headroom_blocks: Optional[Iterable[str]] = None,
    max_attempts_per_step: int = 4,
) -> Dict[str, Any]:
    """Jump-pillar vertically in place using posture(jump=True) + verified place_block.

    Safely validates overhead headroom before rising, keeps jump posture strictly reset
    via finally, and returns the exact world coordinates placed so callers can pillar_down.
    """
    n_steps = max(0, int(steps))
    if n_steps == 0:
        return {"placed": 0, "dug_headroom": 0, "placed_cells": []}
    if n_steps > 8:
        raise WorkflowStepError(
            "pillar_too_high",
            f"Refusing unbounded pillar height {n_steps} > 8",
            blocker_type="safety_interrupted",
        )

    allowed_blocks = tuple(blocks)
    head_allowed = (
        set(allowed_headroom_blocks)
        if allowed_headroom_blocks is not None
        else (
            set(CAVE_DIGGABLE_BLOCKS)
            | set(MINE_DIGGABLE_BLOCKS)
            | {"torch", "wall_torch", "soul_torch", "soul_wall_torch", "cobweb", "sculk_vein"}
        )
    )
    placed_cells: List[List[int]] = []
    dug_headroom = 0

    for _ in range(n_steps):
        st = ctx.guard.check_status(require_safe=True)
        bp = parse_pos(st.get("pos"))
        if bp is None:
            raise WorkflowStepError("invalid_state", "Bot position unavailable before pillar_up")
        cx, cy, cz = math.floor(bp[0] + 0.0001), math.floor(bp[1] + 0.0001), math.floor(bp[2] + 0.0001)

        # Ensure overhead headroom at cy+2 is hazard-free (dig solid obstacle if allowed)
        head2 = (cx, cy + 2, cz)
        head2_blk = ctx.read_block(head2)
        head2_name = str(head2_blk.get("name") or "air")
        if head2_name in HAZARDS:
            raise WorkflowStepError(
                "hazard_detected",
                f"Liquid/hazard {head2_name} directly overhead at {list(head2)}",
                blocker_type="safety_interrupted",
                blocker_pos=list(head2),
                blocker_block=head2_name,
            )
        if head2_name not in DEFAULT_REPLACEABLE:
            if not allow_dig_headroom:
                raise WorkflowStepError(
                    "pillar_headroom_blocked",
                    f"Blocked overhead by {head2_name} at {list(head2)}",
                    blocker_type="obstructed",
                    blocker_pos=list(head2),
                    blocker_block=head2_name,
                )
            above_top = str(ctx.read_block((cx, cy + 3, cz)).get("name") or "air")
            if above_top in HAZARDS:
                raise WorkflowStepError(
                    "hazard_detected",
                    f"Hazard {above_top} above ceiling at {[cx, cy + 3, cz]}",
                    blocker_type="safety_interrupted",
                    blocker_pos=[cx, cy + 3, cz],
                )
            dig_block(ctx, head2, relative=False, allowed_blocks=head_allowed, collect=False)
            dug_headroom += 1

        chosen = ctx.select_item(allowed_blocks)
        if chosen is None:
            raise WorkflowStepError(
                "missing_building_block",
                f"Inventory lacks scaffold block for pillar_up ({list(allowed_blocks)[:4]})",
                blocker_type="missing_item",
            )

        def _clear_self_occupied() -> bool:
            cleared = False
            for sy in (cy + 1, cy):
                bname = str(ctx.read_block((cx, sy, cz)).get("name") or "air")
                if bname not in DEFAULT_REPLACEABLE and bname not in HAZARDS and bname in head_allowed:
                    ctx.guard.set_jump(False)
                    pk = ctx.select_item(PICKAXE_PRIORITY)
                    if pk is not None:
                        ctx.guard.write(ctx.guard.client.equip, item=pk, slot="hand")
                    ctx.guard.write(ctx.guard.client.dig, cx, sy, cz, collect=False, stop_on=["death"])
                    wait_until(
                        ctx,
                        lambda c: str(c.read_block((cx, sy, cz)).get("name") or "air") in DEFAULT_REPLACEABLE,
                        timeout_seconds=1.5,
                        poll_seconds=0.12,
                        description=f"clear occupied {[cx, sy, cz]}",
                    )
                    cleared = True
            if cleared:
                ctx.guard.write(ctx.guard.client.equip, item=chosen, slot="hand")
                ctx.guard.write(ctx.guard.client.look_at, cx + 0.5, cy - 0.5, cz + 0.5)
                ctx.guard.set_jump(True)
            return cleared

        _clear_self_occupied()
        stand_cell(ctx, (cx, cy, cz), tolerance=0.45)
        ctx.guard.write(ctx.guard.client.equip, item=chosen, slot="hand")
        ctx.guard.write(ctx.guard.client.look_at, cx + 0.5, cy - 0.5, cz + 0.5)

        step_placed = False
        last_exc: Optional[WorkflowStepError] = None
        stop_kw = ctx.guard.optional_stop_on_kwargs(ctx.guard.client.place_block)
        ctx.guard.set_jump(True)
        try:
            for _attempt in range(max(1, int(max_attempts_per_step))):
                try:
                    ctx.guard.write(ctx.guard.client.place_block, item=chosen, x=cx, y=cy, z=cz, **stop_kw)
                    step_placed = True
                    break
                except WorkflowStepError as exc:
                    last_exc = exc
                    if exc.code not in {"blocked_by_self", "place_failed", "occupied"}:
                        raise
                    if exc.code == "occupied":
                        _clear_self_occupied()
                    time.sleep(0.15)
        finally:
            ctx.guard.set_jump(False)

        if not step_placed:
            raise last_exc or WorkflowStepError("pillar_failed", f"Failed placing pillar block at {[cx, cy, cz]}")

        verified = wait_until(
            ctx,
            lambda c: str(c.read_block((cx, cy, cz)).get("name") or "air") not in DEFAULT_REPLACEABLE,
            timeout_seconds=1.5,
            poll_seconds=0.15,
            description=f"pillar block at {[cx, cy, cz]}",
        )
        if not verified:
            raise WorkflowStepError("place_failed", f"Pillar placement at {[cx, cy, cz]} not verified", blocker_pos=[cx, cy, cz])

        # Wait briefly to settle cleanly on top of the placed block
        settled = wait_until(
            ctx,
            lambda c: (parse_pos(c.guard.check_status(require_safe=True).get("pos")) or [0, 0, 0])[1] >= cy + 0.85,
            timeout_seconds=1.2,
            poll_seconds=0.12,
            description=f"settle atop pillar {[cx, cy + 1, cz]}",
        )
        if not settled:
            stand_cell(ctx, (cx, cy + 1, cz), tolerance=0.75)
        placed_cells.append([cx, cy, cz])

    return {"placed": len(placed_cells), "dug_headroom": dug_headroom, "placed_cells": placed_cells}


def pillar_down(
    ctx: WorkflowContext,
    pillar_cells: Sequence[Sequence[int]],
    *,
    allowed_blocks: Optional[Iterable[str]] = None,
) -> int:
    """Safely descend by breaking own pillar blocks top-down after checking solid non-liquid landing."""
    dug = 0
    for cell in reversed(list(pillar_cells)):
        cx, cy, cz = int(cell[0]), int(cell[1]), int(cell[2])
        stand_cell(ctx, (cx, cy + 1, cz), tolerance=0.65)
        landing_safe = False
        first_below_name = str(ctx.read_block((cx, cy - 1, cz)).get("name") or "unknown")
        for depth in (1, 2):
            b = ctx.read_block((cx, cy - depth, cz))
            bn = str(b.get("name") or "unknown")
            if bn in HAZARDS:
                break
            if bn not in DEFAULT_REPLACEABLE:
                landing_safe = True
                break
        if not landing_safe:
            raise WorkflowStepError(
                "unsafe_pillar_descent",
                f"Refusing to break pillar block at {[cx, cy, cz]}: unsafe below ({first_below_name})",
                blocker_type="safety_interrupted",
                blocker_pos=[cx, cy - 1, cz],
                blocker_block=first_below_name,
            )
        # Step temporarily or break directly via client.dig (dig_block guards feet_floor == world, so use guarded dig)
        cur_name = str(ctx.read_block((cx, cy, cz)).get("name") or "air")
        if cur_name in AIR:
            continue
        if allowed_blocks is not None and cur_name not in set(allowed_blocks):
            continue
        pickaxe = ctx.select_item(PICKAXE_PRIORITY)
        if pickaxe is not None:
            ctx.guard.write(ctx.guard.client.equip, item=pickaxe, slot="hand")
        ctx.guard.write(ctx.guard.client.dig, cx, cy, cz, collect=False, stop_on=["death"])
        wait_until(
            ctx,
            lambda c: str(c.read_block((cx, cy, cz)).get("name") or "air") in AIR,
            timeout_seconds=1.5,
            poll_seconds=0.15,
            description=f"pillar_down at {[cx, cy, cz]}",
        )
        wait_until(
            ctx,
            lambda c: abs(((parse_pos(c.guard.check_status(require_safe=True).get("pos")) or [0, 0, 0])[1]) - float(cy)) <= 0.35,
            timeout_seconds=1.0,
            poll_seconds=0.12,
            description=f"land on {[cx, cy, cz]}",
        )
        dug += 1
    return dug


def scaffold_drop(
    ctx: WorkflowContext,
    target_cell: Sequence[int],
    step_cells: Sequence[Sequence[int]] = (),
    *,
    blocks: Iterable[str] = SCAFFOLD_BLOCKS,
    invalidate: Optional[Callable[[Sequence[int]], None]] = None,
) -> Dict[str, Any]:
    """Place cushioning/stepping blocks on descent so each vertical fall is <= 3 blocks (zero fall damage)."""
    tx, ty, tz = int(target_cell[0]), int(target_cell[1]), int(target_cell[2])
    placed: List[List[int]] = []
    allowed = tuple(blocks)
    replaceable = set(AIR) | set(DEFAULT_REPLACEABLE)
    for sc in step_cells:
        sx, sy, sz = int(sc[0]), int(sc[1]), int(sc[2])
        cur = str(ctx.read_block((sx, sy, sz)).get("name") or "air")
        if cur in replaceable and ctx.select_item(allowed) is not None:
            try:
                res = place_block(ctx, allowed, (sx, sy, sz), relative=False, replaceable=replaceable)
                if invalidate is not None:
                    invalidate((sx, sy, sz))
                if not res.get("skipped"):
                    placed.append([sx, sy, sz])
                stand_cell(ctx, (sx, sy + 1, sz), tolerance=0.9)
                continue
            except WorkflowStepError:
                pass
        # If step block could not be placed/stepped on, continue carefully to target landing
    stand_cell(ctx, (tx, ty, tz), tolerance=0.9)
    return {"placed_steps": placed, "landed": [tx, ty, tz]}


def escape_to_surface(
    ctx: WorkflowContext,
    *,
    max_ascend: int = 160,
    blocks: Iterable[str] = SCAFFOLD_BLOCKS,
    invalidate: Optional[Callable[[Sequence[int]], None]] = None,
) -> Dict[str, Any]:
    """Emergency pillar-and-dig upward shaft out of a trapped underground cave to open surface sky."""
    ascended = 0
    allowed_dig = set(CAVE_DIGGABLE_BLOCKS) | set(MINE_DIGGABLE_BLOCKS) | {
        "grass_block", "dirt", "coarse_dirt", "rooted_dirt", "mud", "clay",
        "sand", "red_sand", "gravel", "moss_block", "dripstone_block",
        "deepslate", "cobbled_deepslate", "tuff", "calcite",
        "torch", "wall_torch", "soul_torch", "soul_wall_torch", "cobweb", "sculk_vein",
        "oak_log", "birch_log", "spruce_log", "jungle_log", "acacia_log", "dark_oak_log", "mangrove_log", "cherry_log",
        "oak_leaves", "birch_leaves", "spruce_leaves", "jungle_leaves", "acacia_leaves", "dark_oak_leaves",
        "azalea_leaves", "flowering_azalea_leaves", "cherry_leaves", "mangrove_leaves",
        "snow_block", "packed_ice",
    }
    replaceable = set(AIR) | set(DEFAULT_REPLACEABLE)
    visited_shifts: set[Tuple[int, int, int]] = set()

    def _col_open_sky(col_x: int, base_y: int, col_z: int) -> bool:
        for dy in range(1, 22):
            bn = str(ctx.read_block((col_x, base_y + dy, col_z)).get("name") or "air")
            if bn not in replaceable:
                return False
        return True

    def _is_on_surface(cx: int, cy: int, cz: int) -> bool:
        if not _col_open_sky(cx, cy, cz):
            return False
        # Confirm we have climbed out of any pit/shaft: at least 2 adjacent horizontal columns also open to sky
        open_sides = 0
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            if _col_open_sky(cx + dx, cy, cz + dz):
                open_sides += 1
        return open_sides >= 2

    for _ in range(max(1, int(max_ascend))):
        st = ctx.guard.check_status(require_safe=True)
        bp = parse_pos(st.get("pos"))
        if bp is None:
            break
        cx, cy, cz = math.floor(bp[0] + 0.0001), math.floor(bp[1] + 0.0001), math.floor(bp[2] + 0.0001)
        if _is_on_surface(cx, cy, cz):
            return {"escaped": True, "ascended": ascended, "pos": [cx, cy, cz]}

        # Clear falling gravel/sand inside current column if buried
        for sy in (cy + 1, cy):
            sb = str(ctx.read_block((cx, sy, cz)).get("name") or "air")
            if sb not in replaceable and sb not in HAZARDS and sb in allowed_dig:
                try:
                    pk = ctx.select_item(PICKAXE_PRIORITY)
                    if pk is not None:
                        ctx.guard.write(ctx.guard.client.equip, item=pk, slot="hand")
                    ctx.guard.write(ctx.guard.client.dig, cx, sy, cz, collect=False, stop_on=["death"])
                    if invalidate is not None:
                        invalidate((cx, sy, cz))
                except WorkflowStepError:
                    pass

        # If an adjacent walkable column already opens straight upward (natural chimney or previous shaft), step to it once
        moved_chimney = False
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, nz = cx + dx, cz + dz
            for dy in (0, 1, -1):
                ny = cy + dy
                if (nx, ny, nz) in visited_shifts:
                    continue
                fl = str(ctx.read_block((nx, ny - 1, nz)).get("name") or "air")
                f1 = str(ctx.read_block((nx, ny, nz)).get("name") or "air")
                f2 = str(ctx.read_block((nx, ny + 1, nz)).get("name") or "air")
                if fl in (replaceable | set(HAZARDS)) or f1 not in replaceable or f2 not in replaceable:
                    continue
                if all(str(ctx.read_block((nx, ny + ky, nz)).get("name") or "air") in replaceable for ky in range(2, 6)):
                    # Prefer only if current column has solid ceiling at cy+2..cy+4
                    if any(str(ctx.read_block((cx, cy + ky, cz)).get("name") or "air") not in replaceable for ky in (2, 3, 4)):
                        try:
                            visited_shifts.add((cx, cy, cz))
                            visited_shifts.add((nx, ny, nz))
                            stand_cell(ctx, (nx, ny, nz), tolerance=0.35)
                            moved_chimney = True
                            break
                        except WorkflowStepError:
                            pass
            if moved_chimney:
                break
        if moved_chimney:
            continue

        # Check overhead block (cy+2) and above-ceiling liquid hazard (cy+3)
        head2 = (cx, cy + 2, cz)
        above_top_name = str(ctx.read_block((cx, cy + 3, cz)).get("name") or "air")
        head2_name = str(ctx.read_block(head2).get("name") or "air")
        if head2_name in HAZARDS or above_top_name in HAZARDS or (head2_name not in replaceable and head2_name not in allowed_dig):
            # Shift sideways onto an adjacent walkable tile with clear ceiling
            shifted = False
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, nz = cx + dx, cz + dz
                for dy in (0, 1, -1):
                    ny = cy + dy
                    if (nx, ny, nz) in visited_shifts:
                        continue
                    fl = str(ctx.read_block((nx, ny - 1, nz)).get("name") or "air")
                    f1 = str(ctx.read_block((nx, ny, nz)).get("name") or "air")
                    f2 = str(ctx.read_block((nx, ny + 1, nz)).get("name") or "air")
                    top3 = str(ctx.read_block((nx, ny + 3, nz)).get("name") or "air")
                    if fl not in (replaceable | set(HAZARDS)) and f1 in replaceable and f2 in replaceable and top3 not in HAZARDS:
                        try:
                            visited_shifts.add((cx, cy, cz))
                            visited_shifts.add((nx, ny, nz))
                            stand_cell(ctx, (nx, ny, nz), tolerance=0.35)
                            shifted = True
                            break
                        except WorkflowStepError:
                            pass
                if shifted:
                    break
            if not shifted:
                break
            continue

        if head2_name not in replaceable:
            dig_block(ctx, head2, relative=False, allowed_blocks=allowed_dig, collect=True)
            if invalidate is not None:
                invalidate(head2)
            # Wait briefly if sand/gravel might fall above
            if head2_name in {"sand", "red_sand", "gravel"}:
                time.sleep(0.35)
                continue

        # Ensure we have a scaffold block (dig side wall with collect=True if out of blocks)
        if ctx.select_item(blocks) is None:
            mined_extra = False
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                for sy in (cy + 1, cy):
                    wpos = (cx + dx, sy, cz + dz)
                    wname = str(ctx.read_block(wpos).get("name") or "air")
                    if wname in allowed_dig:
                        above_w = str(ctx.read_block((wpos[0], wpos[1] + 1, wpos[2])).get("name") or "air")
                        if above_w not in HAZARDS:
                            try:
                                dig_block(ctx, wpos, relative=False, allowed_blocks=allowed_dig, collect=True)
                                if invalidate is not None:
                                    invalidate(wpos)
                                mined_extra = True
                                break
                            except WorkflowStepError:
                                pass
                if mined_extra:
                    break

        pres = None
        try:
            pres = pillar_up(
                ctx,
                steps=1,
                blocks=blocks,
                allow_dig_headroom=True,
                allowed_headroom_blocks=allowed_dig,
            )
        except WorkflowStepError as exc:
            if exc.code not in {"occupied", "place_failed", "blocked_by_self", "dig_failed", "hazard_detected"}:
                raise
            shifted = False
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, nz = cx + dx, cz + dz
                for dy in (0, 1, -1):
                    ny = cy + dy
                    if (nx, ny, nz) in visited_shifts:
                        continue
                    fl = str(ctx.read_block((nx, ny - 1, nz)).get("name") or "air")
                    f1 = str(ctx.read_block((nx, ny, nz)).get("name") or "air")
                    f2 = str(ctx.read_block((nx, ny + 1, nz)).get("name") or "air")
                    top3 = str(ctx.read_block((nx, ny + 3, nz)).get("name") or "air")
                    if fl not in (replaceable | set(HAZARDS)) and f1 in replaceable and f2 in replaceable and top3 not in HAZARDS:
                        try:
                            visited_shifts.add((cx, cy, cz))
                            visited_shifts.add((nx, ny, nz))
                            stand_cell(ctx, (nx, ny, nz), tolerance=0.35)
                            shifted = True
                            break
                        except WorkflowStepError:
                            pass
                if shifted:
                    break
            if not shifted:
                break
            continue
        for pc in pres.get("placed_cells") or ():
            if invalidate is not None:
                invalidate(pc)
        ascended += int(pres.get("placed", 0))

    st_end = ctx.guard.check_status(require_safe=True)
    bp_end = parse_pos(st_end.get("pos")) or [0, 0, 0]
    ec = [math.floor(bp_end[0] + 0.0001), math.floor(bp_end[1] + 0.0001), math.floor(bp_end[2] + 0.0001)]
    return {"escaped": _is_on_surface(ec[0], ec[1], ec[2]), "ascended": ascended, "pos": ec}


def use_on(
    ctx: WorkflowContext,
    item: str,
    rel_pos: Sequence[int],
    *,
    relative: bool = True,
    ref_rel: Optional[Sequence[int]] = None,
    face_rel: Optional[Sequence[int]] = None,
    verify_held: bool = True,
    stop_on: Sequence[str] = ("death",),
) -> Dict[str, Any]:
    """Equip item, optionally verify held slot, and interact against rotated pos/ref/face."""
    if ctx.select_item(item) is None:
        raise WorkflowStepError(
            f"missing_{item}",
            f"Inventory lacks required item '{item}'",
            blocker_type="missing_item",
            suggested_recovery=f"acquire_{item}",
        )

    ctx.guard.write(ctx.guard.client.equip, item=item, slot="hand")
    if verify_held:
        status = ctx.guard.check_status(require_safe=True)
        held = status.get("held") if isinstance(status.get("held"), dict) else {}
        held_name = held.get("name")
        if held_name is not None and held_name != item:
            raise WorkflowStepError(
                "wrong_held_item",
                f"Expected held item '{item}', got '{held_name}'",
                blocker_type="verification_failed",
                suggested_recovery=f"re_equip_{item}",
            )

    world = [int(v) for v in (ctx.world_pos(rel_pos, integer=True) if relative else rel_pos)]
    ref = [int(v) for v in (ctx.world_pos(ref_rel, integer=True) if (ref_rel is not None and relative) else ref_rel)] if ref_rel is not None else None
    face = ctx.world_vec(face_rel) if (face_rel is not None and relative) else (list(face_rel) if face_rel is not None else None)

    kwargs: Dict[str, Any] = {"pos": world, "item": item, "stop_on": list(stop_on)}
    if ref is not None:
        kwargs["ref"] = ref
    if face is not None:
        kwargs["face"] = face

    res = ctx.guard.write(ctx.guard.client.interact, **kwargs)
    return {"world_pos": world, "ref": ref, "face": face, "item": item, "response": res}


def consume(ctx: WorkflowContext, item: str, *, ms: Optional[int] = None) -> Dict[str, Any]:
    """Consume an inventory item (e.g. food) via use_item."""
    if ctx.select_item(item) is None:
        raise WorkflowStepError(
            f"missing_{item}",
            f"Inventory lacks consumable '{item}'",
            blocker_type="missing_item",
            suggested_recovery=f"acquire_{item}",
        )
    res = ctx.guard.write(ctx.guard.client.use_item, item=item, ms=ms)
    return {"item": item, "response": res}


# ---------------------------------------------------------------------------
# Standard Action Workflow Runner
# ---------------------------------------------------------------------------

def clear_overhead_cells(
    ctx: WorkflowContext,
    cells_rel: Sequence[Tuple[int, int, int]],
    *,
    allowed_blocks: Iterable[str] = CAVE_DIGGABLE_BLOCKS,
    protected_rel: Iterable[Tuple[int, int, int]] = (),
    stand_y: float = 1.0,
) -> List[Dict[str, Any]]:
    """Dig obstructing overhead rock/soft cells bottom-up, handling falling gravel/sand."""
    allowed = set(allowed_blocks)
    protected = {tuple(int(v) for v in c) for c in protected_rel}
    cleared: List[Dict[str, Any]] = []
    non_blocking = set(AIR) | {"water", "flowing_water", "nether_portal"} | set(DEFAULT_REPLACEABLE)
    ordered = sorted({tuple(int(v) for v in c) for c in cells_rel}, key=lambda c: (c[1], c[2], c[0]))
    for rel in ordered:
        if rel in protected:
            continue
        cur = str(ctx.read_rel_block(rel).get("name") or "air")
        if cur in non_blocking or cur not in allowed:
            continue

        status = ctx.guard.check_status(require_safe=True)
        cur_pos = status.get("pos") if isinstance(status.get("pos"), (list, tuple)) else None
        corner_w = ctx.world_pos(rel, integer=True)
        target_w = ctx.world_pos((rel[0] + 0.5, rel[1] + 0.5, rel[2] + 0.5), integer=False)
        if cur_pos is not None:
            # mc-agent-api dig() calls goNear(p, 3.5), skipping pathfinder only when feet->corner <= 4.0.
            feet_dist = math.sqrt(sum((float(corner_w[i]) - float(cur_pos[i])) ** 2 for i in range(3)))
            eye_pos = (float(cur_pos[0]), float(cur_pos[1]) + 1.62, float(cur_pos[2]))
            eye_dist = math.sqrt(sum((target_w[i] - eye_pos[i]) ** 2 for i in range(3)))
            if feet_dist > 3.85 or eye_dist > 4.1:
                stepped = False
                cx_clamp = min(max(rel[0], 0), 3)
                seen_cands: set[Tuple[int, int, int]] = set()
                cands: List[Tuple[int, int, int]] = []
                for cand in (
                    (rel[0], 0, -1),
                    (cx_clamp, 0, -1),
                    (2, 0, -1),
                    (1, 0, -1),
                    (0, 0, -1),
                    (3, 0, -1),
                    (rel[0], 0, -2),
                    (cx_clamp, 0, -2),
                    (2, 0, -2),
                    (1, 0, -2),
                ):
                    if cand not in seen_cands:
                        seen_cands.add(cand)
                        cands.append(cand)
                for cand in cands:
                    if str(ctx.read_rel_block(cand).get("name") or "air") in (non_blocking | set(HAZARDS)):
                        continue
                    h1 = str(ctx.read_rel_block((cand[0], 1, cand[2])).get("name") or "air")
                    h2 = str(ctx.read_rel_block((cand[0], 2, cand[2])).get("name") or "air")
                    if h1 in non_blocking and h2 in non_blocking:
                        cand_feet_pos = (cand[0] + 0.5, stand_y, cand[2] + 0.5)
                        cand_feet_w = ctx.world_pos(cand_feet_pos, integer=False)
                        cand_eye_w = ctx.world_pos((cand[0] + 0.5, stand_y + 1.62, cand[2] + 0.5), integer=False)
                        cand_fdist = math.sqrt(sum((float(corner_w[i]) - cand_feet_w[i]) ** 2 for i in range(3)))
                        cand_edist = math.sqrt(sum((target_w[i] - cand_eye_w[i]) ** 2 for i in range(3)))
                        cand_tol = 0.45
                        if cand_fdist > 3.88 and cand[2] == -1:
                            edge_pos = (cand[0] + 0.5, stand_y, -0.30)
                            edge_feet_w = ctx.world_pos(edge_pos, integer=False)
                            edge_eye_w = ctx.world_pos((cand[0] + 0.5, stand_y + 1.62, -0.30), integer=False)
                            edge_fdist = math.sqrt(sum((float(corner_w[i]) - edge_feet_w[i]) ** 2 for i in range(3)))
                            edge_edist = math.sqrt(sum((target_w[i] - edge_eye_w[i]) ** 2 for i in range(3)))
                            if edge_fdist <= 3.88 and edge_edist <= 4.1:
                                cand_feet_pos = edge_pos
                                cand_fdist = edge_fdist
                                cand_edist = edge_edist
                                cand_tol = 0.25
                        if cand_fdist <= 3.88 and cand_edist <= 4.1:
                            try:
                                stand_at(ctx, cand_feet_pos, tolerance=cand_tol)
                                stepped = True
                                break
                            except WorkflowStepError:
                                pass
                if not stepped and rel[1] <= 2:
                    try:
                        ctx.guard.write(
                            ctx.guard.client.move_to,
                            target_w[0],
                            target_w[1],
                            target_w[2],
                            range=3.0,
                        )
                    except WorkflowStepError:
                        pass

        def _step_off_rel(target_rel: Tuple[int, int, int]) -> bool:
            target_w = tuple(int(v) for v in ctx.world_pos(target_rel, integer=True))
            st_cur = ctx.guard.check_status(require_safe=True)
            pos_cur = parse_pos(st_cur.get("pos"))
            if pos_cur is None:
                return False
            cur_floor = (
                math.floor(pos_cur[0] + 0.0001),
                math.floor(pos_cur[1] + 0.0001) - 1,
                math.floor(pos_cur[2] + 0.0001),
            )
            if cur_floor != target_w:
                return True
            step_cands: List[Tuple[int, int, int]] = [
                (cx, 0, cz)
                for cz in (-1, -2)
                for cx in (2, 1, 0, 3)
            ]
            for ox, oz in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, 1), (1, -1), (-1, -1)):
                cand_w = (target_w[0] + ox, target_w[1], target_w[2] + oz)
                if cand_w == target_w:
                    continue
                fl_name = str(ctx.read_block(cand_w).get("name") or "air")
                if fl_name in (non_blocking | set(HAZARDS)):
                    continue
                h1 = str(ctx.read_block((cand_w[0], cand_w[1] + 1, cand_w[2])).get("name") or "air")
                h2 = str(ctx.read_block((cand_w[0], cand_w[1] + 2, cand_w[2])).get("name") or "air")
                if h1 in non_blocking and h2 in non_blocking:
                    try:
                        stand_at(
                            ctx,
                            [cand_w[0] + 0.5, float(cand_w[1] + 1), cand_w[2] + 0.5],
                            relative=False,
                            tolerance=0.55,
                        )
                        return True
                    except WorkflowStepError:
                        pass
            for srel in step_cands:
                sw = tuple(int(v) for v in ctx.world_pos(srel, integer=True))
                if sw == target_w:
                    continue
                fl_name = str(ctx.read_rel_block(srel).get("name") or "air")
                if fl_name in (non_blocking | set(HAZARDS)):
                    continue
                h1 = str(ctx.read_rel_block((srel[0], srel[1] + 1, srel[2])).get("name") or "air")
                h2 = str(ctx.read_rel_block((srel[0], srel[1] + 2, srel[2])).get("name") or "air")
                if h1 in non_blocking and h2 in non_blocking:
                    try:
                        stand_at(ctx, (srel[0] + 0.5, float(srel[1] + 1), srel[2] + 0.5), tolerance=0.55)
                        return True
                    except WorkflowStepError:
                        pass
            return False

        for _ in range(10):
            cur = str(ctx.read_rel_block(rel).get("name") or "air")
            if cur in non_blocking:
                break
            if cur not in allowed:
                raise WorkflowStepError(
                    "overhead_unbreakable",
                    f"Overhead cell {ctx.world_pos(rel, integer=True)} blocked by {cur}",
                    blocker_type="blocked_by_block",
                    blocker_pos=ctx.world_pos(rel, integer=True),
                    blocker_block=cur,
                    suggested_recovery="clear_or_relocate_site",
                )
            _step_off_rel(rel)
            aim_at(ctx, rel)
            try:
                res = dig_block(ctx, rel, allowed_blocks=allowed, collect=False)
            except WorkflowStepError as err:
                if err.code == "unsafe_dig_floor" and _step_off_rel(rel):
                    aim_at(ctx, rel)
                    res = dig_block(ctx, rel, allowed_blocks=allowed, collect=False)
                else:
                    raise
            cleared.append(res)
            if cur in {"sand", "red_sand", "gravel"}:
                time.sleep(0.5)
    return cleared


def bridge_cells(
    ctx: WorkflowContext,
    targets_rel: Sequence[Tuple[int, int, int]],
    *,
    solid_blocks: Iterable[str] = SCAFFOLD_BLOCKS,
    clearance_diggable: Iterable[str] = CAVE_DIGGABLE_BLOCKS,
    extra_solid_rel: Iterable[Tuple[int, int, int]] = (),
) -> List[Dict[str, Any]]:
    """Step-by-step safe BFS bridging across air or lava gaps using scaffold blocks."""
    allowed_dig = set(clearance_diggable)
    non_solid = set(AIR) | set(HAZARDS) | set(DEFAULT_REPLACEABLE) | {"not_loaded", "unknown"}
    replaceable = set(AIR) | set(HAZARDS) | set(DEFAULT_REPLACEABLE)
    remaining = [tuple(int(v) for v in c) for c in targets_rel]
    remaining = [
        c for c in remaining
        if str(ctx.read_rel_block(c).get("name") or "air") in non_solid
    ]
    if not remaining:
        return []

    solid_known = {tuple(int(v) for v in c) for c in extra_solid_rel}
    for cx, cy, cz in tuple(remaining):
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nb = (cx + dx, cy, cz + dz)
            if nb in remaining or nb in solid_known:
                continue
            nb_name = str(ctx.read_rel_block(nb).get("name") or "air")
            if nb_name not in non_solid:
                solid_known.add(nb)

    placed: List[Dict[str, Any]] = []
    last_stand_err: Optional[WorkflowStepError] = None
    while remaining:
        progressed = False
        cur_pos = parse_pos(ctx.guard.check_status(require_safe=False).get("pos"))
        for target in list(remaining):
            tx, ty, tz = target
            open_cands: List[Tuple[Tuple[int, int, int], bool]] = []
            fallback_cands: List[Tuple[Tuple[int, int, int], bool]] = []
            for dx, dz in ((0, -1), (1, 0), (-1, 0), (0, 1)):
                cand = (tx + dx, ty, tz + dz)
                if cand not in solid_known:
                    continue
                head1 = str(ctx.read_rel_block((cand[0], cand[1] + 1, cand[2])).get("name") or "air")
                head2 = str(ctx.read_rel_block((cand[0], cand[1] + 2, cand[2])).get("name") or "air")
                if head1 in {"lava", "flowing_lava", "water", "flowing_water"}:
                    continue
                if head1 in (set(AIR) | set(DEFAULT_REPLACEABLE)) and head2 in (set(AIR) | set(DEFAULT_REPLACEABLE)):
                    open_cands.append((cand, False))
                else:
                    fallback_cands.append((cand, True))

            def _cand_sort_key(item: Tuple[Tuple[int, int, int], bool]) -> Tuple[int, float]:
                cand, is_fb = item
                if cur_pos is None:
                    return (1 if is_fb else 0, 0.0)
                w = ctx.world_pos((cand[0] + 0.5, float(cand[1] + 1), cand[2] + 0.5), integer=False)
                d = math.hypot(w[0] - cur_pos[0], w[2] - cur_pos[2]) + abs(w[1] - cur_pos[1]) * 1.5
                return (1 if is_fb else 0, d)

            ordered_cands = sorted(open_cands + fallback_cands, key=_cand_sort_key)
            if not ordered_cands:
                continue

            bridged_this = False
            for chosen, needs_dig in ordered_cands:
                if needs_dig:
                    for hy in (1, 2):
                        hcell = (chosen[0], chosen[1] + hy, chosen[2])
                        hname = str(ctx.read_rel_block(hcell).get("name") or "air")
                        if hname not in (set(AIR) | set(DEFAULT_REPLACEABLE) | {"water", "flowing_water"}) and hname in allowed_dig:
                            try:
                                dig_block(ctx, hcell, allowed_blocks=allowed_dig, collect=False)
                            except WorkflowStepError:
                                pass
                try:
                    stand_at(ctx, (chosen[0] + 0.5, float(chosen[1] + 1), chosen[2] + 0.5), tolerance=0.55)
                except WorkflowStepError as err:
                    # Fallback: accept adjacent shore/ledge within placement reach (avoid narrow lava peninsulas).
                    if err.code in {"path_not_found", "stalled", "unreachable_stance", "timeout"}:
                        target_w = ctx.world_pos((target[0] + 0.5, float(target[1]) + 0.5, target[2] + 0.5), integer=False)
                        latest_pos = parse_pos(ctx.guard.check_status(require_safe=False).get("pos"))
                        reach_tol = 2.45 if (
                            latest_pos is not None
                            and math.sqrt(sum((latest_pos[i] - target_w[i]) ** 2 for i in range(3))) <= 3.85
                        ) else 1.75
                        try:
                            stand_at(ctx, (chosen[0] + 0.5, float(chosen[1] + 1), chosen[2] + 0.5), tolerance=reach_tol)
                        except WorkflowStepError as err2:
                            last_stand_err = err2
                            continue
                    else:
                        last_stand_err = err
                        continue
                cur_target = str(ctx.read_rel_block(target).get("name") or "air")
                if cur_target not in non_solid:
                    placed.append({"pos": ctx.world_pos(target, integer=True), "block": cur_target, "already_solid": True})
                    solid_known.add(target)
                    remaining.remove(target)
                    progressed = True
                    bridged_this = True
                    break
                aim_at(ctx, target)
                try:
                    res = place_block(ctx, solid_blocks, target, replaceable=replaceable)
                except WorkflowStepError as err:
                    if err.code == "place_failed" and err.blocker_block in {"sand", "red_sand", "gravel"}:
                        time.sleep(0.4)
                        settled = str(ctx.read_rel_block(target).get("name") or "air")
                        if settled not in non_solid:
                            placed.append({"pos": ctx.world_pos(target, integer=True), "block": settled, "already_solid": True})
                            solid_known.add(target)
                            remaining.remove(target)
                            progressed = True
                            bridged_this = True
                            break
                    raise
                placed.append(res)
                solid_known.add(target)
                remaining.remove(target)
                progressed = True
                bridged_this = True
                break
            if bridged_this:
                break

        if not progressed:
            if last_stand_err is not None:
                raise last_stand_err
            first_world = ctx.world_pos(remaining[0], integer=True)
            raise WorkflowStepError(
                "bridge_no_anchor",
                f"Cannot bridge to {first_world}: no adjacent solid support block found.",
                blocker_type="blocked_by_block",
                blocker_pos=first_world,
                suggested_recovery="inspect_terrain_and_bridge_path",
            )
    return placed


class ActionWorkflowRunner:
    """Executes standard structure actions: start -> steps (+if branches) -> inspect -> end."""

    def run(
        self,
        ctx: WorkflowContext,
        steps: Sequence[ActionStep],
        *,
        precheck: Optional[Callable[[WorkflowContext], None]] = None,
        verify_structure: Optional[Callable[[WorkflowContext], Dict[str, Any]]] = None,
        ready_timeout: float = 0.0,
    ) -> SkillResult:
        started_at = time.monotonic()
        failed_step: Optional[str] = None

        try:
            if ready_timeout > 0:
                deadline = min(ctx.guard.deadline, time.monotonic() + float(ready_timeout))
                while True:
                    try:
                        ctx.guard.check_status(require_safe=True)
                        break
                    except WorkflowStepError as ready_err:
                        if ready_err.code != "not_ready" or time.monotonic() >= deadline:
                            raise
                        time.sleep(min(0.3, max(0.05, deadline - time.monotonic())))
            else:
                ctx.guard.check_status(require_safe=True)
            if precheck is not None:
                precheck(ctx)

            if ctx.player_start is not None:
                failed_step = "move_to_start"
                start_tol = float(ctx.extra.get("start_tolerance", 1.2))
                start_res = stand_at(ctx, ctx.player_start, relative=False, tolerance=start_tol)
                ctx.step_logs.append({"step_id": "move_to_start", "status": "ok", "detail": start_res})

            for step in steps:
                failed_step = step.step_id
                ctx.guard.check_status(require_safe=True)
                if step.skip_if is not None and step.skip_if(ctx):
                    ctx.completed_steps.append(step.step_id)
                    ctx.step_logs.append({"step_id": step.step_id, "status": "skipped"})
                    continue

                attempts = 0
                while True:
                    try:
                        detail = step.action(ctx)
                        ctx.completed_steps.append(step.step_id)
                        if step.stage and step.stage not in ctx.completed_stages:
                            ctx.completed_stages.append(step.stage)
                        ctx.step_logs.append({"step_id": step.step_id, "status": "ok", "detail": detail})
                        break
                    except WorkflowUncertainError:
                        raise
                    except WorkflowStepError as err:
                        branch = step.branches.get(err.code) or step.branches.get("*")
                        if branch is None or attempts >= step.max_branch_attempts:
                            raise
                        attempts += 1
                        recovered = branch(ctx, err)
                        ctx.step_logs.append({
                            "step_id": step.step_id,
                            "status": "branch_recovered" if recovered else "branch_aborted",
                            "error_code": err.code,
                            "attempt": attempts,
                        })
                        if not recovered:
                            raise
                        if step.skip_if is not None and step.skip_if(ctx):
                            ctx.completed_steps.append(step.step_id)
                            if step.stage and step.stage not in ctx.completed_stages:
                                ctx.completed_stages.append(step.stage)
                            break

            failed_step = "verify_structure"
            report = (
                verify_structure(ctx)
                if verify_structure is not None
                else inspect_structure(ctx.spec, ctx.origin, ctx.read_block, orientation=ctx.orientation)
            )
            ctx.extra["structure_report"] = report
            if not report.get("complete"):
                missing = report.get("missing") or []
                first_missing = missing[0] if missing else {}
                raise WorkflowStepError(
                    "structure_incomplete",
                    f"Structure '{ctx.spec.spec_id}' verification incomplete ({report.get('present_count')}/{report.get('total_count')})",
                    blocker_type="verification_failed",
                    blocker_pos=first_missing.get("world_pos"),
                    blocker_block=first_missing.get("current"),
                    suggested_recovery="inspect_missing_cells_and_resume",
                    data={"missing_cells": missing[:8]},
                )
            ctx.structure_verified = True

            if ctx.player_end is not None:
                failed_step = "move_to_end"
                try:
                    end_res = stand_at(ctx, ctx.player_end, relative=False, tolerance=1.0)
                    ctx.step_logs.append({"step_id": "move_to_end", "status": "ok", "detail": end_res})
                except WorkflowStepError as err:
                    # Decouple end-position move from already-verified structure completion.
                    ctx.end_move_error = {
                        "code": err.code,
                        "message": str(err),
                        "blocker_pos": err.blocker_pos,
                    }
                    return SkillResult(
                        success=False,
                        error_code="end_move_failed",
                        message=f"Structure '{ctx.spec.spec_id}' verified complete, but failed moving to exit stance: {err}",
                        data=self._diagnostic_payload(
                            ctx,
                            started_at=started_at,
                            failed_step="move_to_end",
                            blocker_type="unreachable",
                            blocker_pos=err.blocker_pos,
                            blocker_block=err.blocker_block,
                            suggested_recovery="navigate_manually_or_unblock_exit_stance",
                        ),
                    )

            elapsed = round(time.monotonic() - started_at, 2)
            return SkillResult(
                success=True,
                message=f"Workflow '{ctx.spec.spec_id}' completed and verified in {elapsed}s.",
                data=self._diagnostic_payload(ctx, started_at=started_at),
            )

        except WorkflowUncertainError as err:
            ctx.guard.uncertain_error = err
            return SkillResult(
                success=False,
                error_code=err.code,
                message=str(err),
                data=self._diagnostic_payload(
                    ctx,
                    started_at=started_at,
                    failed_step=failed_step,
                    blocker_type="uncertain_write",
                    suggested_recovery="pause_and_resume_with_fresh_observation",
                    extra={"action_state": "unknown", "cleanup_skipped": "workflow_unknown", **err.data},
                ),
            )
        except WorkflowStepError as err:
            return SkillResult(
                success=False,
                error_code=err.code,
                message=str(err),
                data=self._diagnostic_payload(
                    ctx,
                    started_at=started_at,
                    failed_step=failed_step,
                    blocker_type=err.blocker_type,
                    blocker_pos=err.blocker_pos,
                    blocker_block=err.blocker_block,
                    suggested_recovery=err.suggested_recovery,
                    extra=err.data,
                ),
            )
        finally:
            ctx.guard.restore_posture()

    @staticmethod
    def _diagnostic_payload(
        ctx: WorkflowContext,
        *,
        started_at: float,
        failed_step: Optional[str] = None,
        blocker_type: Optional[str] = None,
        blocker_pos: Optional[List[int]] = None,
        blocker_block: Optional[str] = None,
        suggested_recovery: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        report = ctx.extra.get("structure_report") or {}
        payload: Dict[str, Any] = {
            "spec_id": ctx.spec.spec_id,
            "origin": list(ctx.origin),
            "orientation": ctx.orientation,
            "structure_verified": ctx.structure_verified,
            "completed_steps": list(ctx.completed_steps),
            "completed_stages": list(ctx.completed_stages),
            "writes": ctx.guard.writes,
            "read_calls": ctx.guard.read_calls,
            "executed": ctx.guard.writes > 0,
            "elapsed": round(time.monotonic() - started_at, 2),
            "missing_cells": (report.get("missing") or [])[:8],
            "structure_report": report,
            "end_move_error": ctx.end_move_error,
            "step_logs": ctx.step_logs,
        }
        if failed_step is not None:
            payload["failed_step"] = failed_step
        if blocker_type is not None:
            payload["blocker_type"] = blocker_type
        if blocker_pos is not None:
            payload["blocker_pos"] = blocker_pos
        if blocker_block is not None:
            payload["blocker_block"] = blocker_block
        if suggested_recovery:
            payload["suggested_recovery"] = suggested_recovery
        if extra:
            payload.update(extra)
        return payload
