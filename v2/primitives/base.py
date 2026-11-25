"""L3a Primitive base contract: ActionOutcome, PrimitiveContext, and write-ahead execution wrapper."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Sequence

from v2.bt.blackboard import Blackboard
from v2.domain import Coord3, block_coord
from v2.gateway.multihop_navigator import MultiHopNavigator
from v2.gateway.world_proxy import VirtualWorldGateway
from v2.kernel.client import APIError
from v2.kernel.journal import PendingTransactionJournal


@dataclass(frozen=True)
class ActionOutcome:
    """Result of executing AT MOST ONE atomic primitive write."""

    status: str  # "OK" | "PROGRESSED" | "FAILED_LOCAL" | "UNKNOWN" | "NEEDS_AI"
    action: str
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    verified: bool = False


class PrimitiveContext:
    """Shared L0/L1 dependencies passed to all 8 orthogonal primitives."""

    def __init__(
        self,
        gateway: VirtualWorldGateway,
        journal: PendingTransactionJournal,
        navigator: Optional[MultiHopNavigator] = None,
    ) -> None:
        self.gateway = gateway
        self.client = gateway.client
        self.journal = journal
        self.navigator = navigator or MultiHopNavigator(gateway, journal)

    def execute_write(
        self,
        bb: Blackboard,
        endpoint: str,
        payload: Dict[str, Any],
        call: Callable[[], Dict[str, Any]],
        *,
        unknown_center: Optional[Coord3] = None,
    ) -> ActionOutcome:
        """Execute ONE write endpoint guarded by PendingTransactionJournal."""
        tx_id = self.journal.begin_write(endpoint, payload, dimension=bb.dimension, pos=bb.pos)
        bb.writes_this_tick += 1
        try:
            env = call()
        except APIError as err:
            if err.details.get("action_state") == "unknown":
                self.journal.resolve_write(tx_id, "unknown", details={"code": err.code, "message": err.message})
                self.gateway.invalidate_unknown_radius(bb.dimension, unknown_center or block_coord(bb.pos))
                bb.last_action_outcome = f"{endpoint}:unknown"
                return ActionOutcome("UNKNOWN", endpoint, reason=err.code, details=err.details)
            self.journal.resolve_write(tx_id, "failed_known", details={"code": err.code, "message": err.message})
            bb.last_action_outcome = f"{endpoint}:failed:{err.code}"
            return ActionOutcome("FAILED_LOCAL", endpoint, reason=err.code, details=err.details)

        self.journal.resolve_write(tx_id, "confirmed")
        bb.last_action_outcome = f"{endpoint}:ok"
        return ActionOutcome("OK", endpoint, details=env.get("result", {}) if isinstance(env, dict) else {}, verified=True)
