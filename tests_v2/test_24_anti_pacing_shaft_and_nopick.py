"""Regression tests for R6b anti-pacing, shaft-mouth reuse, and bare-hand stone digging prevention."""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt import BTRuntime
from v2.bt.blackboard import Blackboard
from v2.gateway import MultiHopNavigator, VirtualWorldGateway
from v2.kernel import MinecraftAgentClient, PendingTransactionJournal, UnifiedWorldDB
from v2.primitives import PrimitiveContext
from v2.tasks import AcquireItemTask, MineResourceTask, ReturnToSurfaceTask, TaskCatalogue


class TestAntiPacingShaftAndNoPick(unittest.TestCase):
    def test_mine_resource_walks_to_surface_entry_mouth_before_reusing_deep_shaft(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(15.5, 64.0, 0.5))
        sim.fill(-4, 28, -4, 20, 63, 4, "stone")
        # Carve an existing open staircase entrance at x=0 -> buried shaft at x=14, y=30
        sim.fill(0, 63, 0, 14, 65, 0, "air")
        sim.set_block(0, 63, 0, "stone")
        sim.give("iron_pickaxe", 1)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "ShaftMouthBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "ShaftMouthBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                db.save_landmark("overworld", "surface_entry", "latest", (0, 64, 0))
                db.save_landmark("overworld", "mine_shaft", "latest", (14, 30, 0))

                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                task = MineResourceTask(PrimitiveContext(gw, journal), TaskCatalogue(), "raw_iron", 1, target_y=16)
                rt.mount_goal(task)

                rt.tick_once()
                # Even though bot spawned right above buried shaft (15.5, 64, 0.5), it must head to surface_entry (0, 64, 0) FIRST!
                self.assertEqual([ep for ep, _ in sim.write_calls if ep == "dig"], [])
                move_calls = [p for ep, p in sim.write_calls if ep == "move_to"]
                self.assertTrue(move_calls)
                self.assertLess(float(move_calls[0]["x"]), 5.0)
                self.assertFalse(task._entry_reached)
            finally:
                client.close()
                db.close()

    def test_acquire_surface_log_underground_mounts_return_to_surface_instead_of_pacing(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 42.0, 0.5))
        sim.fill(-6, 40, -6, 6, 63, 6, "stone")
        sim.fill(0, 42, 0, 1, 43, 0, "air")
        # Oak tree directly overhead on surface (y=64) inside scan radius=32
        for y in range(64, 68):
            sim.set_block(2, y, 0, "oak_log")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "UndergroundLogBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "UndergroundLogBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                task = AcquireItemTask(ctx, TaskCatalogue(), "oak_log", 2)
                bb = Blackboard()
                bb.begin_tick()
                bb.status = client.get_status()["result"]
                bb.inventory_counts = {}

                res = task.on_tick(bb)
                self.assertEqual(res, "RUNNING")
                self.assertIsInstance(task._active_child, ReturnToSurfaceTask)
            finally:
                client.close()
                db.close()

    def test_return_to_surface_persists_no_pickaxe_counter_across_remounts(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 40.0, 0.5))
        sim.fill(-3, 38, -3, 3, 45, 3, "stone")
        sim.set_block(0, 40, 0, "air")
        sim.set_block(0, 41, 0, "air")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "PersistNoPickBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "PersistNoPickBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                orig_dig = client.dig

                def _warn_dig(pos, collect=True):
                    res = orig_dig(pos, collect=collect)
                    res.setdefault("result", {})["warning"] = "no proper tool: block drops nothing"
                    return res

                with patch.object(client, "dig", side_effect=_warn_dig):
                    rt.mount_goal(ReturnToSurfaceTask(ctx))
                    for _ in range(3):
                        rt.tick_once()
                    self.assertEqual(rt.bb.goal_status, "NEEDS_AI")
                    self.assertEqual(rt.bb.last_failure_reason, "surface_escape:no_pickaxe")
                    digs_after_first = len([w for w in sim.write_calls if w[0] == "dig"])
                    self.assertEqual(digs_after_first, 2)

                    # Re-mounting a brand-new ReturnToSurfaceTask must immediately refuse bare-hand digging (0 extra digs)!
                    rt.mount_goal(ReturnToSurfaceTask(ctx))
                    rt.tick_once()
                    self.assertEqual(rt.bb.goal_status, "NEEDS_AI")
                    self.assertEqual(rt.bb.last_failure_reason, "surface_escape:no_pickaxe")
                    self.assertEqual(len([w for w in sim.write_calls if w[0] == "dig"]), digs_after_first)
            finally:
                client.close()
                db.close()

    def test_multihop_navigator_refuses_bare_hand_stone_wall_but_clears_leaves(self) -> None:
        # 1) Sealed stone wall with no pickaxe -> blocks without issuing a dig write
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-2, 63, -2, 6, 63, 2, "stone")
        for z in (-2, -1, 0, 1, 2):
            sim.set_block(2, 64, z, "stone")
            sim.set_block(2, 65, z, "stone")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "NavNoPickBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "NavNoPickBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                nav = MultiHopNavigator(gw, journal)
                st = client.get_status()["result"]
                gw.prefetch_blocks("overworld", [(2, 64, z) for z in range(-2, 3)] + [(2, 65, z) for z in range(-2, 3)])

                out = nav.step_toward(
                    (4.5, 64.0, 0.5),
                    st,
                    tolerance=1.0,
                    inventory_counts={},
                    prefer_3d=True,
                    avoid_hard_rock_without_pick=True,
                )
                self.assertEqual(out.status, "BLOCKED")
                self.assertEqual([w for w in sim.write_calls if w[0] == "dig"], [])

                # Leaves remain clearable even bare-handed!
                sim.set_block(2, 64, 0, "oak_leaves")
                sim.set_block(2, 65, 0, "oak_leaves")
                gw.invalidate_unknown_radius("overworld", (2, 64, 0), radius=2)
                out_leaf = nav.step_toward(
                    (4.5, 64.0, 0.5),
                    st,
                    tolerance=1.0,
                    inventory_counts={},
                    prefer_3d=True,
                    avoid_hard_rock_without_pick=True,
                )
                self.assertEqual(out_leaf.status, "PROGRESSED")
                self.assertEqual(out_leaf.action_taken, "dig")
            finally:
                client.close()
                db.close()

        # Underground (y=40), bare-hand stone guard triggers by default (avoid_hard_rock_without_pick=None)
        sim_ug = VirtualMcApiServer(spawn_pos=(0.5, 40.0, 0.5))
        sim_ug.fill(-2, 39, -2, 6, 39, 2, "stone")
        for z in (-2, -1, 0, 1, 2):
            sim_ug.set_block(2, 40, z, "stone")
            sim_ug.set_block(2, 41, z, "stone")
        with tempfile.TemporaryDirectory() as td_ug, VirtualMcHttpServer(sim_ug) as srv_ug:
            db_ug = UnifiedWorldDB.for_scope(td_ug, srv_ug.base_url, "NavNoPickUgBot", "w1")
            client_ug = MinecraftAgentClient(srv_ug.base_url, "NavNoPickUgBot", "pass")
            try:
                client_ug.login()
                journal_ug = PendingTransactionJournal(db_ug)
                gw_ug = VirtualWorldGateway(client_ug, db=db_ug)
                nav_ug = MultiHopNavigator(gw_ug, journal_ug)
                st_ug = client_ug.get_status()["result"]
                gw_ug.prefetch_blocks("overworld", [(2, 40, z) for z in range(-2, 3)] + [(2, 41, z) for z in range(-2, 3)])
                out_ug = nav_ug.step_toward((4.5, 40.0, 0.5), st_ug, tolerance=1.0, inventory_counts={}, prefer_3d=True)
                self.assertEqual(out_ug.status, "BLOCKED")
                self.assertEqual([w for w in sim_ug.write_calls if w[0] == "dig"], [])
            finally:
                client_ug.close()
                db_ug.close()


if __name__ == "__main__":
    unittest.main()
