"""Offline regression tests for R8 deadlock, underground no-pickaxe escape, no_support precheck, and pacing cooldown fixes."""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt.blackboard import Blackboard
from v2.bt.branches.hard_safety import HardSafetyBranch
from v2.bt.branches.unstuck import PhysicalUnstuckBranch
from v2.bt.goal_mount import ActiveGoalMount
from v2.bt.kernel import Status
from v2.domain import NON_SOLID_BLOCKS
from v2.gateway.multihop_navigator import MultiHopNavigator
from v2.gateway.pathfinder_3d import NavStep
from v2.gateway.world_proxy import VirtualWorldGateway
from v2.kernel.client import MinecraftAgentClient
from v2.kernel.journal import PendingTransactionJournal
from v2.kernel.storage import UnifiedWorldDB
from v2.planner.deps_planner import AsyncLLMReplanNode, DepsPlanner
from v2.planner.horizon_selector import HorizonSelector
from v2.planner.intent_compiler import IntentCompiler
from v2.planner.model_worker import SerialModelWorker
from v2.primitives import PrimitiveContext
from v2.primitives.place_build import PlaceBuildPrimitive
from v2.tasks.acquire import AcquireItemTask
from v2.tasks.base import Task
from v2.tasks.catalogue import TaskCatalogue
from v2.tasks.surface import ReturnToSurfaceTask, save_anchor_landmark


class _DummyGoalTask(Task):
    def is_finished(self, bb: Blackboard) -> bool:
        return False

    def on_tick(self, bb: Blackboard) -> str:
        return "RUNNING"


class TestR8DeadlockAndSupportFixes(unittest.TestCase):
    def test_hard_safety_swim_retreat_yields_after_consecutive_stalls(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 62.0, 0.5))
        sim.fill(-2, 60, -2, 8, 62, 8, "water")
        sim.fill(-2, 59, -2, 8, 59, 8, "sand")
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "SwimYieldBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "SwimYieldBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                hs = HardSafetyBranch(gw, journal, threat_radius=8.0, threat_dwell_seconds=0.0)
                bb = Blackboard()
                bb.status = {
                    "pos": [0.5, 62.0, 0.5],
                    "dimension": "overworld",
                    "health": 20.0,
                    "food": 20,
                    "in_water": True,
                    "submerged": False,
                    "oxygen": 20,
                    "time_of_day": 6000,
                }
                bb.entities = [{"id": 77, "name": "drowned", "type": "hostile", "pos": [6.5, 58.5, 0.5]}]

                with patch.object(hs._swimmer, "swim_step", return_value=type("R", (), {"status": "PROGRESSED", "reason": "swam"})()):
                    bb.begin_tick()
                    self.assertEqual(hs.update(bb), Status.RUNNING)
                    self.assertEqual(bb.last_action_outcome, "safety_swim_retreat:drowned")
                    self.assertEqual(bb.writes_this_tick, 1)

                    bb.begin_tick()
                    self.assertEqual(hs.update(bb), Status.RUNNING)
                    self.assertEqual(bb.last_action_outcome, "safety_swim_retreat:drowned")

                    # Third tick after >= 2 stalled swim steps must yield to shore recovery!
                    bb.begin_tick()
                    st3 = hs.update(bb)
                    self.assertEqual(st3, Status.FAILURE)
                    self.assertEqual(bb.last_action_outcome, "safety_swim_retreat_yield:drowned")
                    self.assertIn(77, hs._exempt_threats)
            finally:
                client.close()
                db.close()

    def test_surface_escape_no_pickaxe_cools_both_pickaxes_and_unstuck_breaks_out_underground_only(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 30.0, 0.5))
        sim.fill(-3, 28, -3, 3, 36, 3, "stone")
        sim.set_block(0, 30, 0, "air")
        sim.set_block(0, 31, 0, "air")
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "NoPickUnstuckBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "NoPickUnstuckBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                cat = TaskCatalogue()
                planner = DepsPlanner(None, IntentCompiler(PrimitiveContext(gw, journal), cat), HorizonSelector(cat, objective="daily"))
                gm = ActiveGoalMount()
                worker = SerialModelWorker()
                replan = AsyncLLMReplanNode(planner, gm, worker=worker)

                # 1) surface_escape:no_pickaxe cools BOTH stone_pickaxex1 and wooden_pickaxex1 and triggers L3+ unstuck
                bb = Blackboard()
                bb.status = {"pos": [0.5, 30.0, 0.5], "dimension": "overworld", "health": 20.0, "food": 20}
                bb.inventory_counts = {}
                bb.goal_status = "NEEDS_AI"
                bb.last_failure_reason = "surface_escape:no_pickaxe"
                bb.set("last_failed_goal_name", "acquire:stone_pickaxex1", writer="ActiveGoalMount")
                replan.update(bb)
                cds = bb.get("failed_goal_cooldowns") or {}
                self.assertIn("acquire:stone_pickaxex1", cds)
                self.assertIn("acquire:wooden_pickaxex1", cds)
                self.assertTrue(bb.get("unstuck_needed"))
                self.assertGreaterEqual(int(bb.get("unstuck_level", 0) or 0), 3)

                # 2) PhysicalUnstuckBranch L3 breaks out of underground stone when sealed & no_pickaxe (fy < 58)
                gw.prefetch_blocks(
                    "overworld",
                    [(x, y, z) for x in range(-2, 3) for y in range(29, 34) for z in range(-2, 3)],
                )
                bb.last_failure_reason = "surface_escape:no_pickaxe"
                bb.set("unstuck_needed", True, writer="test")
                bb.set("unstuck_level", 3, writer="test")
                unstuck = PhysicalUnstuckBranch(gw, journal)
                st = unstuck.update(bb)
                self.assertEqual(st, Status.RUNNING)
                self.assertTrue(any(w[0] == "dig" for w in sim.write_calls))
            finally:
                client.close()
                db.close()

        # 3) On surface (fy >= 58), even with surface_escape:no_pickaxe, L3 still refuses bare-hand stone walls!
        sim_surf = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim_surf.fill(-2, 63, -2, 6, 63, 6, "stone")
        for x in (-2, 2):
            for z in range(-2, 3):
                sim_surf.set_block(x, 64, z, "stone")
                sim_surf.set_block(x, 65, z, "stone")
        for z in (-2, 2):
            for x in range(-2, 3):
                sim_surf.set_block(x, 64, z, "stone")
                sim_surf.set_block(x, 65, z, "stone")
        sim_surf.fill(-1, 66, -1, 1, 66, 1, "stone")
        with tempfile.TemporaryDirectory() as td2, VirtualMcHttpServer(sim_surf) as srv2:
            db2 = UnifiedWorldDB.for_scope(td2, srv2.base_url, "SurfGuardBot", "w1")
            client2 = MinecraftAgentClient(srv2.base_url, "SurfGuardBot", "pass")
            try:
                client2.login()
                journal2 = PendingTransactionJournal(db2)
                gw2 = VirtualWorldGateway(client2, db=db2)
                unstuck2 = PhysicalUnstuckBranch(gw2, journal2)
                bb2 = Blackboard()
                bb2.status = {"pos": [0.5, 64.0, 0.5], "dimension": "overworld", "health": 20.0, "food": 20}
                bb2.inventory_counts = {}
                bb2.last_failure_reason = "surface_escape:no_pickaxe"
                bb2.set("unstuck_needed", True, writer="test")
                bb2.set("unstuck_level", 3, writer="test")
                unstuck2.update(bb2)
                self.assertEqual([w for w in sim_surf.write_calls if w[0] == "dig"], [])
            finally:
                client2.close()
                db2.close()

    def test_return_to_surface_uses_anchor_up_to_160m_and_resets_no_pick_counter_on_climb(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 10.0, 0.5))
        sim.fill(-2, 9, -2, 100, 9, 2, "stone")
        sim.fill(-2, 13, -2, 6, 15, 6, "stone")
        sim.fill(-2, 44, -2, 2, 45, 2, "stone")
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "LongAnchorBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "LongAnchorBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                bb = Blackboard()
                bb.status = client.get_status()["result"]
                bb.inventory_counts = {"stone_pickaxe": 1}
                save_anchor_landmark(db, bb, "last_surface_anchor", "surface_entry", (90, 62, 0))

                task = ReturnToSurfaceTask(ctx)
                res = task.tick(bb)
                self.assertEqual(res, "RUNNING")
                self.assertFalse(task._surface_route_exhausted)
                self.assertTrue(any(w[0] == "move_to" for w in sim.write_calls))

                # +1 Y climb alone must NOT reset surface_no_pick_hard_digs; escaping >= 12m or gaining a pickaxe resets it
                sim.fill(-2, 40, -2, 2, 45, 2, "stone")
                sim.set_block(0, 41, 0, "air")
                sim.set_block(0, 42, 0, "air")
                bb.set("surface_no_pick_hard_digs", 2, writer="test")
                bb.set("surface_no_pick_origin", (0.5, 40.0, 0.5), writer="test")
                bb.status = {"pos": [0.5, 41.0, 0.5], "dimension": "overworld"}
                bb.inventory_counts = {}
                task._surface_route_exhausted = True
                res_climb = task.tick(bb)
                self.assertEqual(res_climb, "NEEDS_AI")
                self.assertEqual(int(bb.get("surface_no_pick_hard_digs", 0) or 0), 2)

                bb.status = {"pos": [15.5, 41.0, 0.5], "dimension": "overworld"}
                sim.fill(13, 39, -2, 18, 39, 2, "stone")
                sim.fill(13, 44, -2, 18, 45, 2, "stone")
                task.tick(bb)
                self.assertEqual(int(bb.get("surface_no_pick_hard_digs", 0) or 0), 0)
                self.assertIsNone(bb.get("surface_no_pick_origin"))
            finally:
                client.close()
                db.close()

    def test_place_no_support_precheck_and_aquatic_non_solid_blocks(self) -> None:
        for aquatic in ("kelp", "kelp_plant", "seagrass", "tall_seagrass", "bubble_column"):
            self.assertIn(aquatic, NON_SOLID_BLOCKS)

        sim = VirtualMcApiServer(spawn_pos=(0.5, 62.0, 0.5))
        # Floating cell (2, 62, 0) surrounded by air/water/seagrass on all 6 sides -> must NEVER send HTTP place!
        sim.fill(-1, 61, -1, 4, 61, 1, "water")
        sim.set_block(2, 61, 0, "seagrass")
        sim.set_block(2, 62, 0, "air")
        sim.give("cobblestone", 4)
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "NoSupportPrecheckBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "NoSupportPrecheckBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                place = PlaceBuildPrimitive(ctx)
                bb = Blackboard()
                bb.status = {"pos": [0.5, 62.0, 0.5], "dimension": "overworld"}
                bb.inventory_counts = {"cobblestone": 4}

                out = place.place_at(bb, "cobblestone", (2, 62, 0))
                self.assertEqual(out.status, "FAILED_LOCAL")
                self.assertEqual(out.reason, "no_support_precheck")
                self.assertEqual([w for w in sim.write_calls if w[0] == "place"], [])

                nav = MultiHopNavigator(gw, journal)
                bridge_step = NavStep(action="bridge", target=(2, 62, 0), stand_after=(2, 63, 0))
                with patch.object(nav.pathfinder, "plan", return_value=[bridge_step]):
                    nav_out = nav.step_toward((3.5, 63.0, 0.5), bb.status, inventory_counts={"cobblestone": 4}, prefer_3d=True)
                self.assertEqual(nav_out.status, "BLOCKED")
                self.assertEqual(nav_out.reason, "no_support_precheck")
                self.assertEqual([w for w in sim.write_calls if w[0] == "place"], [])
            finally:
                client.close()
                db.close()

    def test_watchdog_pacing_abort_feeds_goal_cooldown(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "PacingCoolBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "PacingCoolBot", "pass")
            try:
                client.login()
                gw = VirtualWorldGateway(client, db=db)
                journal = PendingTransactionJournal(db)
                cat = TaskCatalogue()
                gm = ActiveGoalMount(_DummyGoalTask("explore:surface"))
                bb = Blackboard()
                bb.status = {"pos": [0.5, 64.0, 0.5], "dimension": "overworld"}
                bb.set("watchdog_stalled", True, writer="LoopWatchdog")
                bb.set("watchdog_reason", "pacing_oscillation@explore:surface(path=20m,net=1m,rev=4)", writer="LoopWatchdog")

                st = gm.update(bb)
                self.assertEqual(st, Status.FAILURE)
                self.assertEqual(bb.get("last_failed_goal_name"), "explore:surface")

                replan = AsyncLLMReplanNode(
                    DepsPlanner(None, IntentCompiler(PrimitiveContext(gw, journal), cat), HorizonSelector(cat, objective="daily")),
                    gm,
                    worker=SerialModelWorker(),
                )
                replan.update(bb)
                cds = bb.get("failed_goal_cooldowns") or {}
                self.assertIn("explore:surface", cds)
            finally:
                client.close()
                db.close()

    def test_underground_acquire_log_checks_local_mineshaft_wood_first(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 30.0, 0.5))
        sim.fill(-2, 29, -2, 6, 36, 2, "stone")
        sim.fill(-1, 30, -1, 3, 32, 1, "air")
        sim.set_block(2, 30, 0, "oak_log")
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "MineshaftWoodBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "MineshaftWoodBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                bb = Blackboard()
                bb.status = client.get_status()["result"]
                bb.inventory_counts = {}
                acq = AcquireItemTask(ctx, TaskCatalogue(), "oak_log", 1)
                res = acq.tick(bb)
                self.assertEqual(res, "RUNNING")
                self.assertIsNone(acq._active_child)
                self.assertTrue(any(w[0] == "dig" and w[1]["x"] == 2 for w in sim.write_calls))
            finally:
                client.close()
                db.close()

    def test_hard_safety_burrow_settle_aborts_after_3_stalled_ticks(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(47.0, 39.0, 10.5))
        sim.fill(45, 35, 9, 49, 36, 12, "stone")
        sim.set_block(47, 38, 10, "air")
        sim.set_block(47, 37, 10, "air")
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "BurrowLipBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "BurrowLipBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                hs = HardSafetyBranch(gw, journal)
                bb = Blackboard()
                bb.status = {"pos": [47.0, 39.0, 10.5], "dimension": "overworld", "health": 5.0, "food": 16}
                bb.inventory_counts = {"cobblestone": 4}
                hs._burrow_origin = (47, 39, 10)
                hs._burrow_deep = False

                self.assertEqual(hs._step_emergency_burrow(bb), Status.RUNNING)
                self.assertEqual(bb.last_action_outcome, "safety_burrow_settle")
                self.assertEqual(hs._step_emergency_burrow(bb), Status.RUNNING)
                self.assertEqual(bb.last_action_outcome, "safety_burrow_settle")
                # 3rd consecutive tick hovering above target_y aborts burrow cleanly & applies cooldown
                self.assertIsNone(hs._step_emergency_burrow(bb))
                self.assertIsNone(hs._burrow_origin)
                self.assertGreater(hs._burrow_cooldown_until, 0.0)
            finally:
                client.close()
                db.close()

    def test_unstuck_l3_caps_bare_hand_digs_at_two(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 30.0, 0.5))
        sim.fill(-3, 28, -3, 3, 36, 3, "stone")
        sim.set_block(0, 30, 0, "air")
        sim.set_block(0, 31, 0, "air")
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "CapL3Bot", "w1")
            client = MinecraftAgentClient(srv.base_url, "CapL3Bot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                gw.prefetch_blocks(
                    "overworld",
                    [(x, y, z) for x in range(-2, 3) for y in range(29, 34) for z in range(-2, 3)],
                )
                unstuck = PhysicalUnstuckBranch(gw, journal)
                bb = Blackboard()
                bb.status = {"pos": [0.5, 30.0, 0.5], "dimension": "overworld", "health": 20.0, "food": 20}
                bb.inventory_counts = {}
                bb.last_failure_reason = "surface_escape:no_pickaxe"

                # Two emergency bare-hand L3 digs succeed, 3rd at same location refuses hard rock dig
                for _ in range(3):
                    bb.set("unstuck_needed", True, writer="test")
                    bb.set("unstuck_level", 3, writer="test")
                    unstuck.update(bb)
                dig_calls = [w for w in sim.write_calls if w[0] == "dig"]
                self.assertLessEqual(len(dig_calls), 2)
                self.assertEqual(unstuck._l3_no_pick_digs, 2)
            finally:
                client.close()
                db.close()


if __name__ == "__main__":
    unittest.main()
