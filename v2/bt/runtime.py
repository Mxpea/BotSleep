"""L2 BTRuntime: unified reactive tick driver assembling sleep / starter / daily / competition trees."""

from __future__ import annotations

import time
from typing import List, Optional

from v2.bt.blackboard import Blackboard
from v2.bt.branches.hard_safety import HardSafetyBranch, PendingUnknownGuard
from v2.bt.branches.local_recovery import LocalRecoveryBranch
from v2.bt.goal_mount import ActiveGoalMount, GoalTaskProtocol
from v2.bt.kernel import Node, PrioritySelector, Sequence, Status
from v2.bt.observe_node import SyncBlackboardNode
from v2.bt.watchdog import LoopWatchdogNode
from v2.gateway.world_proxy import VirtualWorldGateway
from v2.kernel.journal import PendingTransactionJournal


class BTRuntime:
    """Executes the root Behavior Tree: [SyncBlackboard -> Watchdog -> PrioritySelector]."""

    def __init__(
        self,
        gateway: VirtualWorldGateway,
        journal: PendingTransactionJournal,
        *,
        mode: str = "survival",
        replan_node: Optional[Node] = None,
        threat_radius: float = 8.0,
        food_threshold: int = 14,
        enable_auto_sleep: bool = True,
        sleep_radius: int = 16,
    ) -> None:
        self.gateway = gateway
        self.journal = journal
        self.mode = mode
        self.bb = Blackboard()

        self.observe = SyncBlackboardNode(gateway)
        self.watchdog = LoopWatchdogNode()
        self.pending_guard = PendingUnknownGuard(journal)
        self.hard_safety = HardSafetyBranch(gateway, journal, threat_radius=threat_radius)
        self.local_recovery = LocalRecoveryBranch(
            gateway,
            journal,
            food_threshold=food_threshold,
            enable_auto_sleep=enable_auto_sleep,
            sleep_radius=sleep_radius,
        )
        self.goal_mount = ActiveGoalMount()
        self.replan_node = replan_node

        branches: List[Node] = [self.pending_guard, self.hard_safety, self.local_recovery]
        if mode != "sleep":
            branches.append(self.goal_mount)
            if replan_node is not None:
                branches.append(replan_node)

        self.selector = PrioritySelector("RootPriority", branches)
        self.root = Sequence("RootMacroTick", [self.observe, self.watchdog, self.selector])

    def mount_goal(self, goal: Optional[GoalTaskProtocol]) -> None:
        self.goal_mount.mount(goal, self.bb)

    def tick_once(self) -> Status:
        self.bb.begin_tick()
        return self.root.tick(self.bb)

    def run_ticks(self, max_ticks: int, *, poll_interval: float = 0.0) -> Status:
        last = Status.RUNNING
        for _ in range(max_ticks):
            if self.bb.stop_requested:
                break
            last = self.tick_once()
            if poll_interval > 0:
                time.sleep(poll_interval)
        return last
