"""Phase 3 tests: reactive preemption, pure-memory stop(), pending freeze, canopy exemption, auto-sleep."""

from __future__ import annotations

import tempfile
import unittest

from tests_v2.sim.scenarios import scenario_v30_deep_pit_and_canopy
from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt import BTRuntime, Blackboard, Status
from v2.gateway import VirtualWorldGateway
from v2.kernel import APIError, MinecraftAgentClient, PendingTransactionJournal, UnifiedWorldDB


class _CountingGoal:
    def __init__(self, target_steps: int = 3) -> None:
        self.name = "counting_goal"
        self.steps = 0
        self.target_steps = target_steps
        self.stop_calls = 0

    def tick(self, bb: Blackboard) -> str:
        self.steps += 1
        bb.last_action_outcome = f"step_{self.steps}"
        if self.steps >= self.target_steps:
            return "COMPLETED"
        return "RUNNING"

    def stop(self, bb: Blackboard) -> None:
        # Pure memory only!
        self.stop_calls += 1


class _UnknownDigGoal:
    def __init__(self, gw: VirtualWorldGateway, journal: PendingTransactionJournal) -> None:
        self.name = "unknown_dig_goal"
        self.gw = gw
        self.journal = journal
        self.stop_calls = 0

    def tick(self, bb: Blackboard) -> str:
        tx_id = self.journal.begin_write("dig", {"pos": [2, 64, 0]}, dimension=bb.dimension, pos=bb.pos)
        try:
            self.gw.client.dig(2, 64, 0)
        except APIError as err:
            if err.details.get("action_state") == "unknown":
                self.journal.resolve_write(tx_id, "unknown", details={"code": err.code})
                return "RUNNING"
            self.journal.resolve_write(tx_id, "failed_known")
            return "FAILED_LOCAL"
        self.journal.resolve_write(tx_id, "confirmed")
        return "COMPLETED"

    def stop(self, bb: Blackboard) -> None:
        self.stop_calls += 1


class TestBehaviorTreeL2(unittest.TestCase):
    def test_preemption_by_skeleton_calls_pure_memory_stop_and_resumes(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-12, 63, -12, 12, 63, 12, "stone")
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "BotBT", "w1")
            client = MinecraftAgentClient(srv.base_url, "BotBT", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                goal = _CountingGoal(target_steps=3)
                rt.mount_goal(goal)

                # Tick 1: goal runs step 1
                rt.tick_once()
                self.assertEqual(goal.steps, 1)
                self.assertEqual(rt.bb.active_branch, "ActiveGoalMount")

                # Skeleton appears at distance 5 -> HardSafetyBranch preempts, calls goal.stop() with 0 extra writes from stop()
                sim.add_entity(201, "skeleton", (5.5, 64.0, 0.5), kind="hostile")
                rt.tick_once()
                self.assertEqual(rt.bb.active_branch, "HardSafetyBranch")
                self.assertEqual(goal.stop_calls, 1)
                self.assertEqual(goal.steps, 1)  # did not execute during threat

                # Clear skeleton -> goal resumes cleanly and completes
                sim.entities.clear()
                rt.tick_once()
                rt.tick_once()
                self.assertEqual(goal.steps, 3)
                self.assertEqual(rt.bb.goal_status, "COMPLETED")
            finally:
                client.close()
                db.close()

    def test_unknown_write_freezes_tree_with_zero_subsequent_writes(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-4, 63, -4, 4, 63, 4, "stone")
        sim.set_block(2, 64, 0, "oak_log")
        sim.inject_fault("not_connected", "dig")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "BotFreeze", "w1")
            client = MinecraftAgentClient(srv.base_url, "BotFreeze", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                goal = _UnknownDigGoal(gw, journal)
                rt.mount_goal(goal)

                # Tick 1 triggers the unknown dig write
                rt.tick_once()
                self.assertTrue(journal.has_pending_unknown())
                writes_after_unknown = len(sim.write_calls)

                # Even if a skeleton spawns right next to us, PendingUnknownGuard MUST win with ZERO writes!
                sim.add_entity(301, "skeleton", (2.5, 64.0, 0.5), kind="hostile")
                for _ in range(5):
                    rt.tick_once()
                    self.assertEqual(rt.bb.active_branch, "PendingUnknownGuard")
                    self.assertTrue(rt.bb.get("pending_frozen"))

                self.assertEqual(len(sim.write_calls), writes_after_unknown)
                self.assertGreaterEqual(goal.stop_calls, 1)
            finally:
                client.close()
                db.close()

    def test_canopy_ground_zombie_exempted_so_recovery_descends(self) -> None:
        _, canopy = scenario_v30_deep_pit_and_canopy()
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(canopy) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "BotCanopy", "w1")
            client = MinecraftAgentClient(srv.base_url, "BotCanopy", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)

                # Player at y=78 standing on oak_leaves; ground zombie at y=64 (dy=14 >= 3)
                rt.tick_once()
                # HardSafetyBranch MUST NOT preempt; LocalRecoveryBranch breaks leaf to descend
                self.assertEqual(rt.bb.active_branch, "LocalRecoveryBranch")
                self.assertEqual(rt.bb.last_action_outcome, "recovery_canopy_descend")
            finally:
                client.close()
                db.close()

    def test_sleep_mode_approaches_and_sleeps_in_bed_at_night(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-6, 63, -6, 8, 63, 6, "stone")
        sim.set_block(5, 64, 0, "white_bed")
        sim.time_of_day = 14000  # night

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "BotSleep", "w1")
            client = MinecraftAgentClient(srv.base_url, "BotSleep", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, mode="sleep", enable_auto_sleep=True)

                rt.tick_once()  # approach bed
                rt.tick_once()  # interact bed -> sleep
                self.assertTrue(sim.is_sleeping)
            finally:
                client.close()
                db.close()

    def test_watchdog_flags_repeated_stall_signature(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-4, 63, -4, 4, 63, 4, "stone")
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "BotWD", "w1")
            client = MinecraftAgentClient(srv.base_url, "BotWD", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)

                class _StuckGoal:
                    name = "stuck_goal"
                    def tick(self, bb: Blackboard) -> str:
                        bb.last_action_outcome = "blocked"
                        return "RUNNING"
                    def stop(self, bb: Blackboard) -> None:
                        pass

                rt.mount_goal(_StuckGoal())
                for _ in range(7):
                    rt.tick_once()
                self.assertTrue(rt.bb.get("watchdog_stalled"))
            finally:
                client.close()
                db.close()


if __name__ == "__main__":
    unittest.main()
