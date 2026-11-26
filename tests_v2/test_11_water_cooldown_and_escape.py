"""Regression tests for water escape, shoreline recovery, watchdog water bobbing, and LLM water/same-pos cooldowns."""

from __future__ import annotations

import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt import BTRuntime, Status
from v2.bt.blackboard import Blackboard
from v2.bt.branches.local_recovery import LocalRecoveryBranch
from v2.bt.goal_mount import ActiveGoalMount
from v2.bt.watchdog import LoopWatchdogNode
from v2.gateway import MultiHopNavigator, SurfaceSwimExecutor, VirtualWorldGateway
from v2.kernel import MinecraftAgentClient, PendingTransactionJournal, UnifiedWorldDB
from v2.planner.deps_planner import AsyncLLMReplanNode
from v2.planner.llm_client import LLMResponse
from v2.planner.model_worker import WorkerResult


class _ImmediateStubWorker:
    def __init__(self) -> None:
        self.is_busy = False
        self.submitted = 0
        self.pending: list = []

    def submit(self, job_id, ctx_ver, fn) -> bool:
        self.submitted += 1
        try:
            self.pending.append(WorkerResult(job_id, ctx_ver, True, value=fn()))
        except Exception as exc:
            self.pending.append(WorkerResult(job_id, ctx_ver, False, error=exc))
        return True

    def poll(self, expected_context_version=None):
        return self.pending.pop(0) if self.pending else None


class TestWaterCooldownAndEscape(unittest.TestCase):
    def test_multihop_water_zero_displacement_rotates_yaw_and_returns_blocked(self) -> None:
        # Reproduce [12338.3, 62, -6175.3] enclosed 1x1 water pit with tall stone walls -> zero displacement
        sim = VirtualMcApiServer(spawn_pos=(12338.3, 62.0, -6175.3))
        sim.fill(12336, 60, -6177, 12340, 65, -6173, "stone")
        sim.set_block(12338, 61, -6176, "water", {"level": "0"})
        sim.set_block(12338, 62, -6176, "water", {"level": "0"})
        sim.set_block(12338, 63, -6176, "air")
        sim.set_block(12338, 64, -6176, "air")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "WaterLoopBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "WaterLoopBot", "pw")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                nav = MultiHopNavigator(gw, journal)
                target = (12350.5, 64.0, -6175.5)

                outcomes = []
                for _ in range(6):
                    st = client.get_status()["result"]
                    out = nav.step_toward(target, st, tolerance=2.0, inventory_counts={})
                    outcomes.append(out)
                    if out.status == "BLOCKED":
                        break

                self.assertEqual(outcomes[-1].status, "BLOCKED")
                self.assertEqual(outcomes[-1].action_taken, "control")
                self.assertEqual(outcomes[-1].reason, "water_stalled_no_displacement")
                self.assertTrue(gw.unreachable.is_blocked("overworld", (12350, 64, -6176), "navigate"))

                # SurfaceSwimExecutor must rotate yaw laterally when stagnant instead of hammering one bearing
                ctrl_yaws = [pl["yaw"] for ep, pl in sim.write_calls if ep == "control"]
                self.assertGreaterEqual(len(set(ctrl_yaws)), 2)
            finally:
                client.close()
                db.close()

    def test_multihop_water_shoreline_recovery_digs_soft_bank_or_places_scaffold(self) -> None:
        # Case A: 1-block dirt bank blocks +X escape from water
        sim = VirtualMcApiServer(spawn_pos=(0.5, 62.0, 0.5))
        sim.fill(-2, 60, -2, 3, 64, 2, "stone")
        sim.fill(0, 61, 0, 0, 62, 0, "water", {"level": "0"})
        sim.set_block(0, 63, 0, "air")
        sim.set_block(0, 64, 0, "air")
        sim.set_block(1, 62, 0, "dirt")
        sim.set_block(1, 63, 0, "air")
        sim.set_block(1, 64, 0, "air")
        sim.set_block(1, 61, 0, "stone")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "ShoreDigBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "ShoreDigBot", "pw")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                nav = MultiHopNavigator(gw, journal)
                reasons = []
                for _ in range(5):
                    st = client.get_status()["result"]
                    out = nav.step_toward((3.5, 62.0, 0.5), st, tolerance=1.0, inventory_counts={})
                    reasons.append(out.reason)
                self.assertIn("water_shore_clear", reasons)
                self.assertEqual(sim.block_name(1, 62, 0), "air")
            finally:
                client.close()
                db.close()

        # Case B: Scaffold placement when stuck in 2x1 water pool with stone walls and dry headroom above neighbor water
        sim2 = VirtualMcApiServer(spawn_pos=(0.5, 62.0, 0.5))
        sim2.fill(-2, 60, -2, 3, 64, 2, "stone")
        sim2.fill(0, 61, 0, 1, 62, 0, "water", {"level": "0"})
        sim2.fill(0, 63, 0, 1, 64, 0, "air")
        sim2.give("cobblestone", 4)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim2) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "ShoreScaffoldBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "ShoreScaffoldBot", "pw")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                nav = MultiHopNavigator(gw, journal)
                reasons = []
                for _ in range(4):
                    st = client.get_status()["result"]
                    out = nav.step_toward((4.5, 63.0, 0.5), st, tolerance=1.0, inventory_counts=dict(sim2.inventory_counts))
                    reasons.append(out.reason)
                self.assertIn("water_shore_scaffold", reasons)
                self.assertEqual(sim2.block_name(1, 62, 0), "cobblestone")
            finally:
                client.close()
                db.close()

    def test_local_recovery_prefetches_shore_tracks_dry_streak_and_yields_lock(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 62.0, 0.5))
        sim.fill(-3, 60, -3, 3, 64, 3, "stone")
        sim.fill(0, 61, 0, 0, 62, 0, "water", {"level": "0"})
        sim.fill(0, 63, 0, 0, 64, 0, "air")
        # Dry shore at x=2, y=62, z=0 (radius 2, NOT yet in voxel cache!)
        sim.set_block(1, 61, 0, "water", {"level": "0"})
        sim.set_block(1, 62, 0, "water", {"level": "0"})
        sim.set_block(1, 63, 0, "air")
        sim.set_block(1, 64, 0, "air")
        sim.set_block(2, 61, 0, "stone")
        sim.set_block(2, 62, 0, "air")
        sim.set_block(2, 63, 0, "air")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "RecShoreBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "RecShoreBot", "pw")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rec = LocalRecoveryBranch(gw, journal, enable_auto_sleep=False)
                bb = Blackboard()
                bb.status = client.get_status()["result"]
                target = rec._pick_swim_target(bb)
                self.assertEqual(target, (2.5, 62.5, 0.5))

                # Now trap in enclosed 1x1 stone water pit -> must yield with unstuck_needed & water_replan_lock_until
                sim.fill(-2, 60, -2, 2, 64, 2, "stone")
                sim.fill(0, 61, 0, 0, 62, 0, "water", {"level": "0"})
                sim.fill(0, 63, 0, 0, 64, 0, "air")
                statuses = []
                for _ in range(5):
                    bb.begin_tick()
                    bb.status = client.get_status()["result"]
                    statuses.append(rec.update(bb))
                    self.assertEqual(bb.get("dry_footing_streak"), 0)

                self.assertIn(Status.FAILURE, statuses)
                self.assertEqual(bb.last_action_outcome, "recovery_swim_yield")
                self.assertTrue(bool(bb.get("unstuck_needed")))
                self.assertGreater(float(bb.get("water_replan_lock_until", 0.0)), time.monotonic())

                # Dry ground increments dry_footing_streak
                sim.pos = [10.5, 64.0, 10.5]
                sim.fill(9, 63, 9, 11, 63, 11, "stone")
                for expected_streak in (1, 2, 3):
                    bb.begin_tick()
                    bb.status = client.get_status()["result"]
                    rec.update(bb)
                    self.assertEqual(bb.get("dry_footing_streak"), expected_streak)
            finally:
                client.close()
                db.close()

    def test_watchdog_detects_water_bobbing_between_goals_and_escalates(self) -> None:
        wd = LoopWatchdogNode(sphere_window=5, sphere_radius=1.6, escalation_ticks=3)
        bb = Blackboard()
        bb.active_goal_name = ""  # Between goals!
        for tick in range(5):
            bb.begin_tick()
            bb.status = {
                "pos": [12338.3, 62.0 + (0.2 if tick % 2 else 0.0), -6175.3],
                "dimension": "overworld",
                "in_water": True,
                "submerged": False,
            }
            wd.update(bb)
        self.assertTrue(bool(bb.get("bounding_sphere_stalled")))
        self.assertTrue(bool(bb.get("unstuck_needed")))
        self.assertEqual(bb.get("unstuck_level"), 1)

    def test_async_replan_water_lock_prevents_llm_spam_and_same_pos_cooldown_escalates(self) -> None:
        # Part 1: Full BT runtime bobbing in water pit -> 0 LLM calls while bobbing in water!
        sim = VirtualMcApiServer(spawn_pos=(0.5, 62.0, 0.5))
        sim.fill(-2, 60, -2, 2, 64, 2, "stone")
        sim.fill(0, 61, 0, 0, 62, 0, "water", {"level": "0"})
        sim.fill(0, 63, 0, 0, 64, 0, "air")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "NoSpamBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "NoSpamBot", "pw")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                planner = MagicMock()
                planner.brain = MagicMock()
                planner.build_request_payload.return_value = ([{"role": "user", "content": "hi"}], [])
                planner.brain.chat.return_value = LLMResponse("m", "m", "wait", [], 10, 5, "primary", 10)
                planner.apply_llm_response.return_value = (None, {"ok": True, "action": "wait"}, planner.brain.chat.return_value)

                worker = _ImmediateStubWorker()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                replan = AsyncLLMReplanNode(planner, rt.goal_mount, worker=worker)
                rt.selector.children.append(replan)

                for _ in range(18):
                    rt.tick_once()
                self.assertEqual(worker.submitted, 0, "LLM must not be spammed while bobbing in water")
            finally:
                client.close()
                db.close()

        # Part 2: Water lock releases only after dry_footing_streak >= 2, and same-pos throttle escalates 4s->20s
        planner2 = MagicMock()
        planner2.brain = MagicMock()
        planner2.build_request_payload.return_value = ([{"role": "user", "content": "hi"}], [])
        resp = LLMResponse("m", "m", "ok", [], 10, 5, "primary", 10)
        planner2.brain.chat.return_value = resp
        planner2.apply_llm_response.return_value = (None, {"ok": True, "action": "wait"}, resp)

        worker2 = _ImmediateStubWorker()
        replan2 = AsyncLLMReplanNode(planner2, ActiveGoalMount(), worker=worker2)
        bb = Blackboard()

        fake_now = [100.0]
        with patch("v2.planner.deps_planner.time.monotonic", side_effect=lambda: fake_now[0]):
            # Locked after water exit until dry_footing_streak >= 2
            bb.status = {"pos": [5.5, 64.0, 5.5], "in_water": False, "submerged": False, "on_ground": True}
            bb.set("water_replan_lock_until", 115.0, writer="test")
            bb.set("dry_footing_streak", 1, writer="test")
            replan2.update(bb)
            self.assertEqual(worker2.submitted, 0)
            self.assertEqual(bb.last_action_outcome, "llm_wait_dry_footing")

            # dry_footing_streak == 2 unlocks -> 1st replan fires & completes
            bb.set("dry_footing_streak", 2, writer="test")
            replan2.update(bb)  # submit 1
            replan2.update(bb)  # collect 1
            self.assertEqual(worker2.submitted, 1)

            # 2nd replan at same position & inventory fires, then arms 4s cooldown
            replan2.update(bb)  # submit 2
            replan2.update(bb)  # collect 2 -> sets 4s cooldown
            self.assertEqual(worker2.submitted, 2)

            replan2.update(bb)
            self.assertEqual(worker2.submitted, 2)
            self.assertEqual(bb.last_action_outcome, "llm_same_pos_cooldown")

            # After 3.9s still throttled; after 4.1s fires 3rd replan -> arms 8s cooldown
            fake_now[0] += 3.9
            replan2.update(bb)
            self.assertEqual(worker2.submitted, 2)

            fake_now[0] += 0.2
            replan2.update(bb)  # submit 3
            replan2.update(bb)  # collect 3 -> sets 8s cooldown
            self.assertEqual(worker2.submitted, 3)

            fake_now[0] += 7.5
            replan2.update(bb)
            self.assertEqual(worker2.submitted, 3)
            self.assertEqual(bb.last_action_outcome, "llm_same_pos_cooldown")

            # Moving > 2.5m clears same-pos cooldown immediately
            bb.status = {"pos": [10.0, 64.0, 5.5], "in_water": False, "submerged": False, "on_ground": True}
            replan2.update(bb)
            self.assertEqual(worker2.submitted, 4)


if __name__ == "__main__":
    unittest.main()
