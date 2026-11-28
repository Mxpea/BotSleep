"""L1 SurfaceSwimExecutor: bounded single-pulse surface swim with oxygen guard."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Dict, Optional, Sequence, Tuple

from v2.kernel.client import APIError, MinecraftAgentClient
from v2.kernel.journal import PendingTransactionJournal


_LATERAL_YAW_OFFSETS: Tuple[float, ...] = (
    math.pi / 3.0,
    -math.pi / 3.0,
    math.pi / 2.0,
    -math.pi / 2.0,
    2.0 * math.pi / 3.0,
    -2.0 * math.pi / 3.0,
    math.pi,
)


@dataclass(frozen=True)
class SwimStepResult:
    status: str  # "SWAM" | "SURFACED" | "UNKNOWN" | "FAILED"
    reason: str = ""
    details: Dict[str, Any] | None = None


class SurfaceSwimExecutor:
    """Executes ONE bounded buoyancy/swim control pulse per macro-tick with lateral escape on stall."""

    def __init__(
        self,
        client: MinecraftAgentClient,
        journal: PendingTransactionJournal,
        *,
        pulse_ms: int = 450,
    ) -> None:
        self.client = client
        self.journal = journal
        self.pulse_ms = int(pulse_ms)
        self._last_pos: Optional[Tuple[float, float, float]] = None
        self._stagnant_pulses: int = 0

    def reset(self) -> None:
        """Reset stagnant pulse tracking once dry footing or new target is reached."""
        self._last_pos = None
        self._stagnant_pulses = 0

    def swim_step(
        self,
        target_pos: Sequence[float],
        status: Dict[str, Any],
        *,
        yaw_offset_rad: Optional[float] = None,
    ) -> SwimStepResult:
        """Issue one bounded jump+forward swim pulse (or vertical surface-only if low oxygen)."""
        pos = status.get("pos") or [0.0, 64.0, 0.0]
        cur_p = (float(pos[0]), float(pos[1]), float(pos[2]))
        dim = str(status.get("dimension") or "overworld")
        oxygen = int(status.get("oxygen", 20))
        submerged = bool(status.get("submerged", False))

        if self._last_pos is not None:
            if math.dist(cur_p, self._last_pos) < 0.25:
                self._stagnant_pulses += 1
            else:
                self._stagnant_pulses = 0
        self._last_pos = cur_p

        tx = math.floor(float(target_pos[0])) + 0.5
        tz = math.floor(float(target_pos[2])) + 0.5
        dx = tx - cur_p[0]
        dz = tz - cur_p[2]
        # Mineflayer yaw convention (session.js:896,2992): 0 faces -Z, pi/2 faces -X
        yaw = math.atan2(-dx, -dz)

        # Low oxygen or deeply submerged -> pure vertical buoyancy first
        forward = not (submerged and oxygen <= 10)
        lateral_deflected = False
        if yaw_offset_rad is not None:
            yaw = math.atan2(math.sin(yaw + yaw_offset_rad), math.cos(yaw + yaw_offset_rad))
            lateral_deflected = abs(yaw_offset_rad) > 1e-6
        elif forward and self._stagnant_pulses >= 1:
            offset = _LATERAL_YAW_OFFSETS[(self._stagnant_pulses - 1) % len(_LATERAL_YAW_OFFSETS)]
            yaw = math.atan2(math.sin(yaw + offset), math.cos(yaw + offset))
            lateral_deflected = True

        payload = {"forward": forward, "jump": True, "sprint": False, "yaw": round(yaw, 3), "ms": self.pulse_ms, "stop_on": []}
        tx_id = self.journal.begin_write("control", payload, dimension=dim, pos=pos)
        try:
            res = self.client.control(
                forward=forward, jump=True, sprint=False, yaw=yaw, ms=self.pulse_ms, wait=True, stop_on=[]
            )
        except APIError as err:
            if err.details.get("action_state") == "unknown":
                self.journal.resolve_write(tx_id, "unknown", details={"code": err.code, "message": err.message})
                return SwimStepResult("UNKNOWN", reason=err.code)
            self.journal.resolve_write(tx_id, "failed_known", details={"code": err.code})
            return SwimStepResult("FAILED", reason=err.code)

        self.journal.resolve_write(tx_id, "confirmed")
        details = dict(res.get("result", {}) or {})
        details["yaw"] = round(yaw, 3)
        details["lateral_deflected"] = lateral_deflected
        return SwimStepResult("SURFACED" if not forward else "SWAM", details=details)
