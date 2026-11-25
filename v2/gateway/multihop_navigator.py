"""L1 MultiHopNavigator: abolishes 32m local limit, slices >22m relays, falls back to 3D micro-dig/pillar/bridge.

Enforces the Single-Write Macro-Tick contract: each call to `step_toward()` executes AT MOST ONE
write action wrapped in `PendingTransactionJournal`, then immediately returns control to the caller/BT.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from v2.domain import Coord3, SCAFFOLD_BLOCKS, block_coord, dist2_xz, dist3, pick_best_item
from v2.gateway.pathfinder_3d import NavStep, Pathfinder3D
from v2.gateway.surface_swim import SurfaceSwimExecutor
from v2.gateway.world_proxy import VirtualWorldGateway
from v2.kernel.client import APIError, MinecraftAgentClient
from v2.kernel.journal import PendingTransactionJournal


@dataclass(frozen=True)
class NavOutcome:
    status: str  # "REACHED" | "PROGRESSED" | "BLOCKED" | "UNKNOWN" | "NEEDS_SUBTASK"
    action_taken: str = "none"
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


class MultiHopNavigator:
    """Two-tier navigator: 18-22m relay slicing + Pathfinder3D micro-dig/bridge/pillar + SurfaceSwim."""

    RELAY_THRESHOLD = 22.0
    RELAY_STEP = 20.0
    MIN_PROGRESS_METERS = 0.35

    def __init__(
        self,
        gateway: VirtualWorldGateway,
        journal: PendingTransactionJournal,
        *,
        pathfinder: Optional[Pathfinder3D] = None,
        swimmer: Optional[SurfaceSwimExecutor] = None,
    ) -> None:
        self.gateway = gateway
        self.client: MinecraftAgentClient = gateway.client
        self.journal = journal
        self.pathfinder = pathfinder or Pathfinder3D(gateway)
        self.swimmer = swimmer or SurfaceSwimExecutor(self.client, journal)
        self._stall_counts: Dict[Tuple[str, Coord3], int] = {}
        self._last_pos_for_target: Dict[Tuple[str, Coord3], Tuple[float, float, float]] = {}

    def reset_target_state(self, dimension: str, target: Coord3) -> None:
        self._stall_counts.pop((dimension, target), None)
        self._last_pos_for_target.pop((dimension, target), None)

    def _run_write(
        self,
        endpoint: str,
        payload: Dict[str, Any],
        dimension: str,
        pos: Sequence[float],
        fn: Callable[[], Dict[str, Any]],
        *,
        unknown_center: Optional[Coord3] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[NavOutcome]]:
        tx_id = self.journal.begin_write(endpoint, payload, dimension=dimension, pos=pos)
        try:
            res = fn()
        except APIError as err:
            if err.details.get("action_state") == "unknown":
                self.journal.resolve_write(tx_id, "unknown", details={"code": err.code, "message": err.message})
                self.gateway.invalidate_unknown_radius(dimension, unknown_center or block_coord(pos))
                return None, NavOutcome("UNKNOWN", action_taken=endpoint, reason=err.code)
            self.journal.resolve_write(tx_id, "failed_known", details={"code": err.code})
            return None, NavOutcome("BLOCKED", action_taken=endpoint, reason=err.code, details=err.details)
        self.journal.resolve_write(tx_id, "confirmed")
        return res, None

    def step_toward(
        self,
        target_pos: Sequence[float],
        status: Dict[str, Any],
        *,
        tolerance: float = 2.0,
        inventory_counts: Optional[Dict[str, int]] = None,
        avoid_block_breaking: Optional[Callable[[Coord3], bool]] = None,
        prefer_3d: bool = False,
        xz_only: bool = False,
    ) -> NavOutcome:
        """Perform AT MOST ONE bounded navigation/clearing/bridging/pillaring write step toward target_pos."""
        pos = status.get("pos") or [0.0, 64.0, 0.0]
        cur_p = (float(pos[0]), float(pos[1]), float(pos[2]))
        dim = str(status.get("dimension") or "overworld")
        tgt = (float(target_pos[0]), float(target_pos[1]), float(target_pos[2]))
        tgt_cell = block_coord(tgt)
        inv = inventory_counts or {}

        distance = dist2_xz(cur_p, tgt) if xz_only else dist3(cur_p, tgt)
        if distance <= tolerance:
            self.reset_target_state(dim, tgt_cell)
            return NavOutcome("REACHED", action_taken="none", reason="within_tolerance")

        # 1) Surface swim if currently in water/submerged
        if status.get("in_water") or status.get("submerged"):
            swim_res = self.swimmer.swim_step(tgt, status)
            if swim_res.status == "UNKNOWN":
                return NavOutcome("UNKNOWN", action_taken="control", reason=swim_res.reason)
            if swim_res.status == "FAILED":
                return NavOutcome("BLOCKED", action_taken="control", reason=swim_res.reason)
            return NavOutcome("PROGRESSED", action_taken="control", reason=swim_res.status.lower())

        key = (dim, tgt_cell)
        prev_pos = self._last_pos_for_target.get(key)
        stalled = prev_pos is not None and dist3(prev_pos, cur_p) < self.MIN_PROGRESS_METERS
        if stalled:
            self._stall_counts[key] = self._stall_counts.get(key, 0) + 1
        elif prev_pos is not None:
            self._stall_counts[key] = 0
        self._last_pos_for_target[key] = cur_p

        use_3d = prefer_3d or self._stall_counts.get(key, 0) >= 1

        # 2) Direct or relay move_to (no 32m hard cap!)
        if not use_3d:
            if distance > self.RELAY_THRESHOLD:
                ratio = self.RELAY_STEP / max(1e-6, dist2_xz(cur_p, tgt))
                rx = cur_p[0] + (tgt[0] - cur_p[0]) * min(1.0, ratio)
                rz = cur_p[2] + (tgt[2] - cur_p[2]) * min(1.0, ratio)
                payload = {"x": round(rx, 1), "z": round(rz, 1), "range": 2.0, "timeout_ms": 8000}
                res, err_out = self._run_write(
                    "move_to", payload, dim, cur_p,
                    lambda: self.client.move_to(x=payload["x"], z=payload["z"], range=2.0, timeout_ms=8000),
                )
            else:
                ry = None if xz_only else tgt[1]
                payload = {"x": round(tgt[0], 1), "y": ry, "z": round(tgt[2], 1), "range": tolerance, "timeout_ms": 10000}
                res, err_out = self._run_write(
                    "move_to", payload, dim, cur_p,
                    lambda: self.client.move_to(x=payload["x"], y=ry, z=payload["z"], range=tolerance, timeout_ms=10000),
                )

            if err_out is None:
                return NavOutcome("PROGRESSED", action_taken="move_to", details=res.get("result", {}) if res else {})
            if err_out.status == "UNKNOWN":
                return err_out
            # move_to failed (e.g. path_not_found across leaves/wall/pit) -> mark stall and fall through to 3D on NEXT tick or immediately?
            # Strict Single-Write Macro-Tick: move_to already used 1 write attempt; switch to 3D for next tick!
            self._stall_counts[key] = self._stall_counts.get(key, 0) + 1
            return NavOutcome("PROGRESSED", action_taken="move_to_fallback_3d", reason=err_out.reason)

        # 3) Local 3D A* step (micro-dig / pillar-up / bridge / detour walk)
        scaffold = pick_best_item(inv, SCAFFOLD_BLOCKS)
        start_cell = block_coord(cur_p)
        # If target is far, aim Pathfinder3D at an intermediate 12m sub-goal
        sub_goal = tgt_cell
        if dist3(start_cell, tgt_cell) > 14.0:
            ratio = 12.0 / max(1e-6, dist3(start_cell, tgt_cell))
            sub_goal = (
                int(round(start_cell[0] + (tgt_cell[0] - start_cell[0]) * ratio)),
                int(round(start_cell[1] + (tgt_cell[1] - start_cell[1]) * ratio)),
                int(round(start_cell[2] + (tgt_cell[2] - start_cell[2]) * ratio)),
            )

        steps = self.pathfinder.plan(
            dim,
            start_cell,
            sub_goal,
            tolerance=min(tolerance, 2.0),
            has_scaffold=scaffold is not None,
            avoid_block_breaking=avoid_block_breaking,
        )
        if not steps:
            return NavOutcome("BLOCKED", action_taken="pathfinder_3d", reason="no_3d_path")

        step: NavStep = steps[0]
        # If an upcoming dig step is already within reach (<= 3.8m), execute the dig directly
        # instead of wasting a tick on a tiny walk adjustment.
        for upcoming in steps[:4]:
            if upcoming.action == "dig" and dist3(cur_p, (upcoming.target[0] + 0.5, upcoming.target[1] + 0.5, upcoming.target[2] + 0.5)) <= 3.8:
                step = upcoming
                break
            if upcoming.action != "walk":
                break

        if step.action == "dig":
            res, err_out = self._run_write(
                "dig", {"pos": list(step.target)}, dim, cur_p,
                lambda: self.client.dig(list(step.target), collect=True),
                unknown_center=step.target,
            )
            if err_out is not None:
                if err_out.status == "BLOCKED":
                    self.gateway.unreachable.record_failure(dim, step.target, "dig", err_out.reason)
                return err_out
            self.gateway.write_through_dig(dim, step.target)
            # Keep 3D active if a 2nd block (e.g. lower half of 1x2 wall) still needs digging
            if step.extra is None:
                self._stall_counts[key] = 0
            return NavOutcome("PROGRESSED", action_taken="dig", reason="cleared_3d_obstacle", details={"pos": list(step.target)})

        if step.action == "bridge" and scaffold is not None:
            res, err_out = self._run_write(
                "place", {"item": scaffold, "pos": list(step.target)}, dim, cur_p,
                lambda: self.client.place_block(scaffold, step.target[0], step.target[1], step.target[2]),
                unknown_center=step.target,
            )
            if err_out is not None:
                return err_out
            self.gateway.write_through_place(dim, step.target, scaffold)
            self._stall_counts[key] = 0
            return NavOutcome("PROGRESSED", action_taken="place", reason="bridged_gap", details={"pos": list(step.target)})

        if step.action == "pillar_up" and scaffold is not None:
            res, err_out = self._run_write(
                "place", {"item": scaffold, "pos": list(step.target)}, dim, cur_p,
                lambda: self.client.place_block(scaffold, step.target[0], step.target[1], step.target[2]),
                unknown_center=step.target,
            )
            if err_out is not None:
                return err_out
            self.gateway.write_through_place(dim, step.target, scaffold)
            self._stall_counts[key] = 0
            return NavOutcome("PROGRESSED", action_taken="place", reason="pillared_up", details={"pos": list(step.target)})

        # Default: walk step (coalesce consecutive plain walk steps up to 6 blocks, exact range=0.4)
        walk_end = step.stand_after
        has_following_non_walk = False
        for s in steps[1:6]:
            if s.action != "walk":
                has_following_non_walk = True
                break
            walk_end = s.stand_after
        res, err_out = self._run_write(
            "move_to", {"x": walk_end[0], "y": walk_end[1], "z": walk_end[2], "range": 0.4}, dim, cur_p,
            lambda: self.client.move_to(x=walk_end[0], y=walk_end[1], z=walk_end[2], range=0.4, timeout_ms=8000),
        )
        if err_out is not None:
            if err_out.status == "BLOCKED":
                self.gateway.unreachable.record_blocked_edge(dim, start_cell, step.stand_after)
            return err_out
        if not has_following_non_walk:
            self._stall_counts[key] = 0
        return NavOutcome("PROGRESSED", action_taken="move_to", reason="3d_walk_step", details={"to": list(walk_end)})
