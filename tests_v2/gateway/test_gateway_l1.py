"""Phase 2 tests: VirtualWorldGateway, exact bucket LOS, Pathfinder3D, MultiHopNavigator, SurfaceSwim."""

from __future__ import annotations

import tempfile
import unittest

from tests_v2.sim.scenarios import (
    scenario_v31_64m_lava_portal,
    scenario_v31_sloped_water_bank,
)
from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.gateway import (
    BlockUnreachableTracker,
    MultiHopNavigator,
    Pathfinder3D,
    SurfaceSwimExecutor,
    VirtualWorldGateway,
)
from v2.kernel import MinecraftAgentClient, PendingTransactionJournal, UnifiedWorldDB


class TestGatewayL1(unittest.TestCase):
    def test_multihop_navigates_54m_across_leaf_wall_to_lava_pool(self) -> None:
        sim = scenario_v31_64m_lava_portal()
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "Bot3", "w1")
            client = MinecraftAgentClient(srv.base_url, "Bot3", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                nav = MultiHopNavigator(gw, journal)

                reached = False
                for _ in range(25):
                    st = client.get_status()["result"]
                    gw.ingest_status_stance(st)
                    out = nav.step_toward((53.0, 64.0, 0.5), st, tolerance=2.5, inventory_counts=sim.inventory_counts)
                    self.assertNotEqual(out.status, "UNKNOWN")
                    if out.status == "REACHED":
                        reached = True
                        break
                self.assertTrue(reached, f"Failed to reach 54m lava pool; final pos={sim.pos}")
                self.assertGreaterEqual(sim.pos[0], 50.5)
            finally:
                client.close()
                db.close()

    def test_sloped_water_bank_exact_y085_los_and_occluder_detection(self) -> None:
        sim = scenario_v31_sloped_water_bank()
        # Block all other sides around water (4, 62, 0) so the ONLY stance near (3, 64, 0) is occluded by short_grass
        for dx, dz in ((1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1), (2, 0), (0, 2), (0, -2)):
            sim.set_block(4 + dx, 64, dz, "stone")
            sim.set_block(4 + dx, 65, dz, "stone")
        sim.set_block(2, 64, 0, "stone")
        sim.set_block(2, 65, 0, "stone")

        with VirtualMcHttpServer(sim) as srv:
            client = MinecraftAgentClient(srv.base_url, "Bot1", "pass")
            try:
                client.login()
                gw = VirtualWorldGateway(client)
                plan = gw.plan_liquid_interaction("overworld", (4, 62, 0), sim.pos, mode="pickup")
                self.assertEqual(plan.status, "CLEAR_OCCLUDER")
                self.assertEqual(plan.occluder_pos, (3, 64, 0))
                self.assertEqual(plan.occluder_name, "short_grass")

                # Clear occluder and re-plan -> READY!
                client.dig(3, 64, 0)
                gw.write_through_dig("overworld", (3, 64, 0))
                plan2 = gw.plan_liquid_interaction("overworld", (4, 62, 0), sim.pos, mode="pickup")
                self.assertEqual(plan2.status, "READY")
                self.assertEqual(plan2.stand_pos, (3, 64, 0))
            finally:
                client.close()

    def test_voxel_cache_hit_ratio_and_unknown_invalidation(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-4, 63, -4, 4, 63, 4, "stone")
        with VirtualMcHttpServer(sim) as srv:
            client = MinecraftAgentClient(srv.base_url, "BotC", "pass")
            try:
                client.login()
                gw = VirtualWorldGateway(client)
                coords = [(x, 63, 0) for x in range(-3, 4)]
                gw.prefetch_blocks("overworld", coords)
                reads_after_first = len(sim.read_calls)

                # Second pass hits cache -> 0 new HTTP calls!
                for c in coords:
                    e = gw.get_block("overworld", c)
                    self.assertIsNotNone(e)
                    self.assertEqual(e.name, "stone")
                self.assertEqual(len(sim.read_calls), reads_after_first)
                self.assertGreaterEqual(gw.cache_hit_ratio, 0.5)

                # Unknown invalidation forces re-read on affected 3x3x3 cell
                gw.invalidate_unknown_radius("overworld", (0, 63, 0), radius=1)
                gw.get_block("overworld", (0, 63, 0))
                self.assertEqual(len(sim.read_calls), reads_after_first + 1)
            finally:
                client.close()

    def test_pathfinder_3d_respects_avoid_block_breaking(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-2, 63, -3, 6, 63, 3, "stone")
        # Protected wall at x=2, z=-1..1; open detour at z=2
        for z in (-1, 0, 1):
            sim.set_block(2, 64, z, "cobblestone")
            sim.set_block(2, 65, z, "cobblestone")
        protected = {(2, 64, z) for z in (-1, 0, 1)} | {(2, 65, z) for z in (-1, 0, 1)}

        with VirtualMcHttpServer(sim) as srv:
            client = MinecraftAgentClient(srv.base_url, "BotP", "pass")
            try:
                client.login()
                gw = VirtualWorldGateway(client)
                pf = Pathfinder3D(gw)
                steps = pf.plan(
                    "overworld", (0, 64, 0), (4, 64, 0),
                    tolerance=1.0,
                    avoid_block_breaking=lambda c: c in protected,
                )
                self.assertIsNotNone(steps)
                for s in steps:
                    self.assertFalse(s.action == "dig" and s.target in protected)
            finally:
                client.close()

    def test_surface_swim_executor_oxygen_guard_and_buoyancy(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 62.0, 0.5))
        sim.fill(-2, 60, -2, 6, 60, 2, "stone")
        sim.fill(-2, 61, -2, 6, 63, 2, "water", {"level": "0"})
        sim.oxygen = 6  # low oxygen while submerged

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "BotS", "w1")
            client = MinecraftAgentClient(srv.base_url, "BotS", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                swim = SurfaceSwimExecutor(client, journal)
                st = client.get_status()["result"]
                self.assertTrue(st["submerged"])
                res = swim.swim_step((5.0, 64.0, 0.5), st)
                self.assertEqual(res.status, "SURFACED")
                self.assertGreater(sim.pos[1], 62.0)
            finally:
                client.close()
                db.close()

    def test_unreachable_tracker_ttl_and_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = UnifiedWorldDB.for_scope(td, "http://127.0.0.1:18900/api/v1", "BotU", "w1")
            try:
                t1 = BlockUnreachableTracker(db=db, base_ttl_seconds=60.0)
                t1.record_failure("overworld", (5, 64, 5), "dig", "occluded", now=1000.0)
                self.assertFalse(t1.is_blocked("overworld", (5, 64, 5), "dig", now=1001.0))
                t1.record_failure("overworld", (5, 64, 5), "dig", "occluded", now=1002.0)
                self.assertTrue(t1.is_blocked("overworld", (5, 64, 5), "dig", now=1003.0))
                self.assertFalse(t1.is_blocked("overworld", (5, 64, 5), "dig", now=1070.0))

                # Reload from DB
                t2 = BlockUnreachableTracker(db=db, base_ttl_seconds=60.0)
                t2.load_dimension("overworld", now=1010.0)
                self.assertTrue(t2.is_blocked("overworld", (5, 64, 5), "dig", now=1010.0))
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
