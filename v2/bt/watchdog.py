"""L2 LoopWatchdog: detects cross-step oscillation, exact repeats, and Bounding-Sphere physical stalls."""

from __future__ import annotations

from collections import deque
import math
from typing import Deque, Optional, Tuple

from v2.bt.blackboard import Blackboard
from v2.bt.kernel import Node, Status
from v2.domain import dist2_xz, dist3


class LoopWatchdogNode(Node):
    """Tracks exact state signatures AND a Bounding-Sphere progress invariant to trigger 4-level unstuck."""

    def __init__(
        self,
        *,
        window: int = 12,
        max_repeats: int = 6,
        sphere_window: int = 12,
        sphere_radius: float = 1.6,
        escalation_ticks: int = 4,
    ) -> None:
        super().__init__("LoopWatchdog")
        self.window = max(4, int(window))
        self.max_repeats = max(3, int(max_repeats))
        self.sphere_window = max(4, int(sphere_window))
        self.sphere_radius = max(0.5, float(sphere_radius))
        self.escalation_ticks = max(2, int(escalation_ticks))
        self._signatures: Deque[Tuple[int, int, int, str, str, Tuple[Tuple[str, int], ...]]] = deque(maxlen=self.window)
        self._sphere_positions: Deque[Tuple[float, float, float]] = deque(maxlen=self.sphere_window)
        self._pace_positions: Deque[Tuple[float, float, float]] = deque(maxlen=24)
        self._last_inv_sig: Optional[Tuple[Tuple[str, int], ...]] = None
        self._ticks_since_unstuck: int = 0
        self.stalled: bool = False
        self.stall_signature: Optional[str] = None

    @staticmethod
    def _bounding_sphere_radius(points: Deque[Tuple[float, float, float]]) -> float:
        n = len(points)
        if n == 0:
            return 0.0
        cx = sum(p[0] for p in points) / n
        cy = sum(p[1] for p in points) / n
        cz = sum(p[2] for p in points) / n
        center = (cx, cy, cz)
        return max(dist3(p, center) for p in points)

    def update(self, bb: Blackboard) -> Status:
        px, py, pz = bb.pos
        cur_pos = (px, py, pz)
        inv_sig = tuple(sorted((k, int(v)) for k, v in bb.inventory_counts.items() if v > 0))
        sig = (math.floor(px), math.floor(py), math.floor(pz), bb.active_goal_name, bb.last_action_outcome, inv_sig)
        self._signatures.append(sig)

        # Reset physical stall state whenever inventory changes or player escapes >= 3.5m from unstuck origin
        unstuck_level = int(bb.get("unstuck_level", 0) or 0)
        unstuck_origin = bb.get("unstuck_origin")
        if self._last_inv_sig is not None and inv_sig != self._last_inv_sig:
            self._sphere_positions.clear()
            self._pace_positions.clear()
            self._ticks_since_unstuck = 0
            if unstuck_level > 0:
                bb.set("unstuck_level", 0, writer=self.name)
                bb.set("unstuck_needed", False, writer=self.name)
                bb.set("unstuck_origin", None, writer=self.name)
                unstuck_level = 0
        elif unstuck_origin is not None and dist3(cur_pos, unstuck_origin) >= 3.5:
            self._sphere_positions.clear()
            self._ticks_since_unstuck = 0
            bb.set("unstuck_level", 0, writer=self.name)
            bb.set("unstuck_needed", False, writer=self.name)
            bb.set("unstuck_origin", None, writer=self.name)
            unstuck_level = 0
        self._last_inv_sig = inv_sig

        # Bounding-Sphere progress invariant (ignores idle, sleeping, frozen, furnace wait, or active safety preemption;
        # stays active when in_water/submerged even between goals so bobbing still escalates PhysicalUnstuckBranch)
        in_water = bool(bb.status.get("in_water") or bb.status.get("submerged"))
        skip_sphere = (
            bb.paused
            or bool(bb.get("pending_frozen"))
            or bool(bb.status.get("is_sleeping", False))
            or bb.last_action_outcome in {"smelt_waiting_furnace", "nether_portal_waiting"}
            or bb.active_branch in {"HardSafetyBranch", "PendingUnknownGuard"}
            or (not bb.active_goal_name and unstuck_level == 0 and not in_water)
        )
        sphere_stalled = False
        if not skip_sphere:
            self._sphere_positions.append(cur_pos)
            self._pace_positions.append(cur_pos)
            if unstuck_level > 0 and not bb.get("unstuck_needed"):
                self._ticks_since_unstuck += 1
                if self._ticks_since_unstuck >= self.escalation_ticks:
                    origin = unstuck_origin or cur_pos
                    if dist3(cur_pos, origin) < self.sphere_radius * 1.5:
                        next_level = min(4, unstuck_level + 1)
                        bb.set("unstuck_level", next_level, writer=self.name)
                        bb.set("unstuck_needed", True, writer=self.name)
                        self._ticks_since_unstuck = 0
                        sphere_stalled = True
            elif len(self._sphere_positions) >= self.sphere_window:
                radius = self._bounding_sphere_radius(self._sphere_positions)
                if radius < self.sphere_radius:
                    next_level = min(4, unstuck_level + 1)
                    bb.set("unstuck_level", next_level, writer=self.name)
                    bb.set("unstuck_needed", True, writer=self.name)
                    if unstuck_origin is None:
                        bb.set("unstuck_origin", cur_pos, writer=self.name)
                    self._sphere_positions.clear()
                    self._ticks_since_unstuck = 0
                    sphere_stalled = True
                    self.stalled = True
                    goal_label = bb.active_goal_name or ("water" if in_water else "idle")
                    self.stall_signature = f"bounding_sphere@{goal_label}(r={radius:.2f}m,L{next_level})"
                    bb.set("bounding_sphere_stalled", True, writer=self.name)
                    bb.set("watchdog_reason", self.stall_signature, writer=self.name)

        if not sphere_stalled:
            bb.set("bounding_sphere_stalled", False, writer=self.name)

        # A<->B pacing oscillation check (wide spatial back-and-forth without net progress)
        if not skip_sphere and bb.active_goal_name and len(self._pace_positions) >= 16:
            pts = list(self._pace_positions)
            total_path = 0.0
            reversals = 0
            prev_dx, prev_dz = 0.0, 0.0
            xs = [p[0] for p in pts]
            zs = [p[2] for p in pts]
            for idx in range(1, len(pts)):
                dx = pts[idx][0] - pts[idx - 1][0]
                dz = pts[idx][2] - pts[idx - 1][2]
                step_xz = math.hypot(dx, dz)
                if step_xz < 12.0:
                    total_path += step_xz
                if step_xz >= 0.8:
                    if math.hypot(prev_dx, prev_dz) >= 0.8:
                        dot = dx * prev_dx + dz * prev_dz
                        if dot < -0.25 * step_xz * math.hypot(prev_dx, prev_dz):
                            reversals += 1
                    prev_dx, prev_dz = dx, dz
            net_disp = dist2_xz(pts[0], pts[-1])
            span_xz = math.hypot(max(xs) - min(xs), max(zs) - min(zs))
            if total_path >= 18.0 and span_xz >= 3.0 and net_disp <= 5.0 and reversals >= 3:
                self._pace_positions.clear()
                self.stalled = True
                self.stall_signature = (
                    f"pacing_oscillation@{bb.active_goal_name}(path={total_path:.1f}m,net={net_disp:.1f}m,rev={reversals})"
                )
                bb.set("watchdog_stalled", True, writer=self.name)
                bb.set("watchdog_reason", self.stall_signature, writer=self.name)
                return Status.SUCCESS

        # Count exact occurrences of current signature in recent window
        if bb.active_goal_name and bb.last_action_outcome not in {"smelt_waiting_furnace", "nether_portal_waiting"}:
            repeats = sum(1 for s in self._signatures if s == sig)
            if repeats >= self.max_repeats:
                self.stalled = True
                self.stall_signature = f"{bb.active_goal_name}@{sig[:3]}:{bb.last_action_outcome}"
                bb.set("watchdog_stalled", True, writer=self.name)
                bb.set("watchdog_reason", self.stall_signature, writer=self.name)
                return Status.SUCCESS

        self.stalled = sphere_stalled
        if not sphere_stalled:
            self.stall_signature = None
        bb.set("watchdog_stalled", False, writer=self.name)
        return Status.SUCCESS

    def reset(self) -> None:
        self._signatures.clear()
        self._sphere_positions.clear()
        self._pace_positions.clear()
        self._last_inv_sig = None
        self._ticks_since_unstuck = 0
        self.stalled = False
        self.stall_signature = None
