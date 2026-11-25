"""L1 SurfaceSwimExecutor: bounded single-pulse surface swim with oxygen guard."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Dict, Sequence

from v2.kernel.client import APIError, MinecraftAgentClient
from v2.kernel.journal import PendingTransactionJournal


@dataclass(frozen=True)
class SwimStepResult:
    status: str  # "SWAM" | "SURFACED" | "UNKNOWN" | "FAILED"
    reason: str = ""
    details: Dict[str, Any] | None = None


class SurfaceSwimExecutor:
    """Executes ONE bounded buoyancy/swim control pulse per macro-tick."""

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

    def swim_step(
        self,
        target_pos: Sequence[float],
        status: Dict[str, Any],
    ) -> SwimStepResult:
        """Issue one bounded jump+forward swim pulse (or vertical surface-only if low oxygen)."""
        pos = status.get("pos") or [0.0, 64.0, 0.0]
        dim = str(status.get("dimension") or "overworld")
        oxygen = int(status.get("oxygen", 20))
        submerged = bool(status.get("submerged", False))

        dx = float(target_pos[0]) - float(pos[0])
        dz = float(target_pos[2]) - float(pos[2])
        yaw = math.atan2(-dx, dz)

        # Low oxygen or deeply submerged -> pure vertical buoyancy first
        forward = not (submerged and oxygen <= 10)
        payload = {"forward": forward, "jump": True, "yaw": round(yaw, 3), "ms": self.pulse_ms}
        tx_id = self.journal.begin_write("control", payload, dimension=dim, pos=pos)
        try:
            res = self.client.control(forward=forward, jump=True, yaw=yaw, ms=self.pulse_ms, wait=True)
        except APIError as err:
            if err.details.get("action_state") == "unknown":
                self.journal.resolve_write(tx_id, "unknown", details={"code": err.code, "message": err.message})
                return SwimStepResult("UNKNOWN", reason=err.code)
            self.journal.resolve_write(tx_id, "failed_known", details={"code": err.code})
            return SwimStepResult("FAILED", reason=err.code)

        self.journal.resolve_write(tx_id, "confirmed")
        return SwimStepResult("SURFACED" if not forward else "SWAM", details=res.get("result", {}))
