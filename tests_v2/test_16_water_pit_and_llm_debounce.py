"""Regression tests for 2-deep flowing-water pit escape, land-first exploration, and LLM wet/submit debounce."""

from __future__ import annotations

import math
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt import Status
from v2.bt.blackboard import Blackboard
from v2.bt.branches.local_recovery import LocalRecoveryBranch
from v2.bt.goal_mount import ActiveGoalMount
from v2.gateway import VirtualWorldGateway
from v2.kernel import MinecraftAgentClient, PendingTransactionJournal, UnifiedWorldDB
from v2.planner.deps_planner import AsyncLLMReplanNode
from v2.planner.llm_client import LLMResponse
from v2.planner.model_worker import WorkerResult
from v2.primitives.base import PrimitiveContext
from v2.tasks.explore import ExploreUntilTask


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
        res = self.pending.pop(0) if self.pending else None
        if res is None:
            return None
        if expected_context_version is not None and res.context_version != expected_context_version:
            return None
        return res


class TestWaterPitAndLlmDebounce(unittest.TestCase):
    def test_local_recovery_2deep_flowing_water_pit_plug_and_stone_bank_dig(self) -> None:
        # Part A: 2-deep flowing water pit (Minecraft water with level != 0) -> plugs adjacent water cell
        sim = VirtualMcApiServer(spawn_pos=(0.5, 61.0, 0.5))
        sim.fill(-2, 60, -2, 3, 63, 2, "stone")
        sim.set_block(0, 61, 0, "water", {"level": "1"})
        sim.set_block(0, 62, 0, "water", {"level": "8"})
        sim.set_block(0, 63, 0, "air")
        sim.set_block(1, 61, 0, "water", {"level": "2"})
        sim.set_block(1, 62, 0, "air")
        sim.set_block(1, 63, 0, "air")
        sim.give("cobblestone", 4)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "PlugPitBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "PlugPitBot", "pw")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rec = LocalRecoveryBranch(gw, journal, enable_auto_sleep=False)
                bb = Blackboard()

                outcomes = []
                for _ in range(5):
                    bb.begin_tick()
                    bb.status = client.get_status()["result"]
                    bb.status["in_water"] = True
                    bb.inventory = client.inventory()["result"]
                    bb.inventory_counts = client.get_inventory_counts()
                    rec.update(bb)
                    outcomes.append(bb.last_action_outcome)
                    self.assertGreater(float(bb.get("last_wet_at", 0.0)), 0.0)
                    # Keep bot pinned at pit bottom to simulate downward flowing current
                    sim.pos = [0.5, 61.0, 0.5]

                self.assertIn("recovery_swim_plug_water", outcomes)
                self.assertEqual(sim.block_name(1, 61, 0), "cobblestone")
            finally:
                client.close()
                db.close()

        # Part B: 1x1 water pit with blocked_by_self at feet records unreachable failure,
        # and holding stone_pickaxe carves 1 safe stone bank step while skipping lava-adjacent stone!
        sim2 = VirtualMcApiServer(spawn_pos=(0.5, 61.0, 0.5))
        sim2.fill(-2, 60, -2, 2, 62, 2, "stone")
        sim2.fill(-2, 63, -2, 2, 64, 2, "air")
        sim2.set_block(0, 61, 0, "water", {"level": "1"})
        sim2.set_block(0, 62, 0, "water", {"level": "8"})
        # Put lava next to +X stone bank (1, 62, 0) so +X must be skipped in favor of another safe stone side!
        sim2.set_block(2, 62, 0, "lava", {"level": "0"})
        sim2.give("cobblestone", 2)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim2) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "StonePitBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "StonePitBot", "pw")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rec = LocalRecoveryBranch(gw, journal, enable_auto_sleep=False)
                bb = Blackboard()

                # Without pickaxe: tries feet (0, 61, 0) -> blocked_by_self -> records failure (2x -> blocked)
                failures = 0
                for _ in range(6):
                    bb.begin_tick()
                    bb.status = client.get_status()["result"]
                    bb.status["in_water"] = True
                    bb.inventory = client.inventory()["result"]
                    bb.inventory_counts = client.get_inventory_counts()
                    st = rec.update(bb)
                    sim2.pos = [0.5, 61.0, 0.5]
                    if st == Status.FAILURE and bb.last_action_outcome == "recovery_failed:blocked_by_self":
                        failures += 1
                        if failures >= 2:
                            break
                self.assertEqual(failures, 2)
                self.assertTrue(gw.unreachable.is_blocked("overworld", (0, 61, 0), "place"))

                # Give stone_pickaxe: stalled_in_water carves safe stone bank step (NOT lava-adjacent (1, 62, 0)!)
                sim2.give("stone_pickaxe", 1)
                rec._swim_consec = 2
                rec._swim_anchor = (0.5, 61.0, 0.5)
                bb.begin_tick()
                bb.status = client.get_status()["result"]
                bb.status["in_water"] = True
                bb.inventory = client.inventory()["result"]
                bb.inventory_counts = client.get_inventory_counts()
                st2 = rec.update(bb)
                self.assertEqual(st2, Status.RUNNING)
                self.assertEqual(bb.last_action_outcome, "recovery_swim_dig_bank")
                self.assertEqual(sim2.block_name(1, 62, 0), "stone")
                carved = any(
                    sim2.block_name(dx, 62, dz) == "air"
                    for dx, dz in ((-1, 0), (0, 1), (0, -1))
                )
                self.assertTrue(carved, "Pickaxe bank dig must carve a safe stone climb-out step")
            finally:
                client.close()
                db.close()

    def test_explore_until_avoids_water_east_prefers_dry_land_and_rotates_cursor(self) -> None:
        # Part A: Shore at (0.5, 64.0, 0.5), +X east is ocean water, +Z south is dry grass_block
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5), loaded_radius=96)
        sim.fill(-30, 63, -30, 30, 63, 30, "grass_block")
        # Make +X (east) and NE/SE water-dominated at 8m and 16m
        sim.fill(4, 63, -30, 30, 63, 30, "water", {"level": "0"})

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "LandExploreBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "LandExploreBot", "pw")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                bb = Blackboard()
                bb.status = client.get_status()["result"]

                exp1 = ExploreUntilTask(ctx, lambda _b, _g: False, name="find_trees")
                out = exp1.on_tick(bb)
                self.assertEqual(out, "RUNNING")
                # East (+X) sector around (20.5, 0.5) must be excluded with ~60s TTL
                excluded = bb.get("excluded_sectors") or {}
                east_sec = (math.floor(20.5 / 16.0), math.floor(0.5 / 16.0))
                self.assertIn(east_sec, excluded)
                # Chosen waypoint must NOT go east into ocean (x <= 0.5!)
                self.assertIsNotNone(exp1._active_wp)
                assert exp1._active_wp is not None
                self.assertLessEqual(exp1._active_wp[0], 0.5)
                cursor_after_first = int(bb.get("explore_sector_cursor", 0))
                self.assertGreater(cursor_after_first, 0)

                # Restarting ExploreUntilTask resumes from explore_sector_cursor instead of resetting to 0
                exp2 = ExploreUntilTask(ctx, lambda _b, _g: False, name="find_trees_2")
                exp2.on_tick(bb)
                self.assertGreaterEqual(exp2._sector_idx, cursor_after_first)
            finally:
                client.close()
                db.close()

        # Part B: Mocked/empty gateway returns {} -> falls back cleanly without error
        mock_gw = MagicMock()
        mock_gw.prefetch_blocks.return_value = {}
        mock_ctx = PrimitiveContext(mock_gw, MagicMock())
        exp_mock = ExploreUntilTask(mock_ctx, lambda _b, _g: False, prefer_surface=False)
        exp_mock._nav = MagicMock()
        exp_mock._nav.step_to.return_value = MagicMock(status="PROGRESSED")
        bb_mock = Blackboard()
        bb_mock.status = {"pos": [0.5, 64.0, 0.5], "dimension": "overworld"}
        self.assertEqual(exp_mock.on_tick(bb_mock), "RUNNING")
        self.assertEqual(bb_mock.get("excluded_sectors"), None)

    def test_async_replan_wet_hysteresis_submit_cooldown_and_preempt_preserve(self) -> None:
        planner = MagicMock()
        planner.brain = MagicMock()
        planner.build_request_payload.return_value = ([{"role": "user", "content": "hi"}], [])
        resp = LLMResponse("m", "m", "ok", [], 10, 5, "primary", 10)
        planner.brain.chat.return_value = resp
        planner.apply_llm_response.return_value = (None, {"ok": True, "action": "wait"}, resp)

        worker = _ImmediateStubWorker()
        replan = AsyncLLMReplanNode(planner, ActiveGoalMount(), worker=worker)
        bb = Blackboard()

        fake_now = [200.0]
        with patch("v2.planner.deps_planner.time.monotonic", side_effect=lambda: fake_now[0]):
            # 1) Wet-flicker hysteresis: last_wet_at within 10s & dry_footing_streak < 3 blocks LLM!
            bb.status = {"pos": [1.5, 64.0, 1.5], "in_water": False, "submerged": False, "on_ground": True}
            bb.set("last_wet_at", 198.0, writer="LocalRecoveryBranch")
            bb.set("dry_footing_streak", 2, writer="LocalRecoveryBranch")
            replan.update(bb)
            self.assertEqual(worker.submitted, 0)
            self.assertEqual(bb.last_action_outcome, "llm_wait_dry_footing")

            # dry_footing_streak == 3 unlocks immediately
            bb.set("dry_footing_streak", 3, writer="LocalRecoveryBranch")
            replan.update(bb)  # submit 1 (succeeds & sets _last_submit_pos)
            self.assertEqual(worker.submitted, 1)

            # 2) Preempted-future snowball check: even if stop() discards future BEFORE poll/harvest,
            # submit-side (pos, inv) exponential cooldown 4s -> 8s -> 16s -> 30s still throttles!
            replan.stop(bb)  # direct stop increments context_version -> pending result discarded
            replan.update(bb)  # polls & discards stale result
            replan.update(bb)  # submit 2 at same pos/inv -> allowed, arms 4s submit cooldown
            self.assertEqual(worker.submitted, 2)
            replan.stop(bb)
            replan.update(bb)  # discards stale 2

            replan.update(bb)  # immediate tick at t=200 -> blocked by 4s submit cooldown!
            self.assertEqual(worker.submitted, 2)
            self.assertEqual(bb.last_action_outcome, "llm_same_pos_cooldown")

            fake_now[0] += 4.1  # t=204.1 -> submit 3 allowed, arms 8s submit cooldown
            replan.update(bb)
            self.assertEqual(worker.submitted, 3)
            replan.stop(bb)
            replan.update(bb)  # discards stale 3

            fake_now[0] += 7.9  # t=212.0 -> still inside 8s cooldown
            replan.update(bb)
            self.assertEqual(worker.submitted, 3)
            self.assertEqual(bb.last_action_outcome, "llm_same_pos_cooldown")

            fake_now[0] += 0.2  # t=212.2 -> submit 4 allowed, arms 16s submit cooldown
            replan.update(bb)
            self.assertEqual(worker.submitted, 4)
            replan.stop(bb)
            replan.update(bb)

            fake_now[0] += 15.0  # t=227.2 -> still inside 16s cooldown
            replan.update(bb)
            self.assertEqual(worker.submitted, 4)

            fake_now[0] += 1.5  # t=228.7 -> submit 5 allowed, arms 30s max cooldown
            replan.update(bb)
            self.assertEqual(worker.submitted, 5)
            replan.stop(bb)
            replan.update(bb)

            fake_now[0] += 29.0  # t=257.7 -> still inside 30s cooldown
            replan.update(bb)
            self.assertEqual(worker.submitted, 5)

            # Moving > 2.5m clears submit cooldown immediately
            bb.status = {"pos": [6.0, 64.0, 1.5], "in_water": False, "submerged": False, "on_ground": True}
            replan.update(bb)
            self.assertEqual(worker.submitted, 6)

            # 3) Benign recovery preemption with unchanged inventory preserves context_version!
            ver_before = replan._context_version
            bb.last_action_outcome = "recovery_swim:swam"
            replan.stop(bb)
            self.assertEqual(replan._context_version, ver_before)
            replan.update(bb)  # harvests preserved response!
            self.assertEqual(replan.last_response, resp)

            # HardSafety preemption bumps context_version & discards stale future
            fake_now[0] += 35.0
            bb.status = {"pos": [12.0, 64.0, 1.5], "in_water": False, "submerged": False, "on_ground": True}
            replan.update(bb)  # submit 7
            ver_mid = replan._context_version
            bb.last_action_outcome = "safety_retreat:zombie"
            replan.stop(bb)
            self.assertEqual(replan._context_version, ver_mid + 1)
            replan.last_response = None
            replan.update(bb)
            self.assertIsNone(replan.last_response)


if __name__ == "__main__":
    unittest.main()
