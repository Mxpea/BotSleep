"""Regression tests for single-pass water A*, swim center-alignment, 5x3x5 shore BFS, and LocalRecovery armor auto-equip."""

from __future__ import annotations

import math
import tempfile
import unittest

from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt import Blackboard, Status
from v2.bt.branches.local_recovery import LocalRecoveryBranch
from v2.gateway import MultiHopNavigator, Pathfinder3D, SurfaceSwimExecutor, VirtualWorldGateway
from v2.kernel import MinecraftAgentClient, PendingTransactionJournal, UnifiedWorldDB


class TestWaterAStarAndRecovery(unittest.TestCase):
    def test_pathfinder_submerged_vertical_ascent_then_climb_shore(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 61.0, 0.5))
        sim.fill(-2, 60, -2, 3, 60, 2, "stone")
        # 2-deep water shaft at (0, 61..62, 0) surrounded by solid stone walls except shore step at (1, 63, 0)
        sim.fill(-2, 61, -2, 3, 64, 2, "stone")
        sim.set_block(0, 61, 0, "water", {"level": "0"})
        sim.set_block(0, 62, 0, "water", {"level": "0"})
        sim.set_block(0, 63, 0, "air")
        sim.set_block(0, 64, 0, "air")
        sim.set_block(1, 63, 0, "air")
        sim.set_block(1, 64, 0, "air")

        with VirtualMcHttpServer(sim) as srv:
            client = MinecraftAgentClient(srv.base_url, "SubBot", "pw")
            try:
                client.login()
                gw = VirtualWorldGateway(client)
                pf = Pathfinder3D(gw)
                steps = pf.plan("overworld", (0, 61, 0), (1, 63, 0), tolerance=0.5)
                self.assertIsNotNone(steps)
                assert steps is not None
                self.assertEqual(len(steps), 2)
                self.assertEqual(steps[0].action, "swim")
                self.assertEqual(steps[0].stand_after, (0, 62, 0))
                self.assertEqual(steps[1].action, "climb_shore")
                self.assertEqual(steps[1].stand_after, (1, 63, 0))

                # Soft dirt bank at (1, 63, 0) triggers micro-dig bank step from surface water (0, 62, 0)
                sim.set_block(1, 63, 0, "dirt")
                gw2 = VirtualWorldGateway(client)
                pf2 = Pathfinder3D(gw2)
                steps2 = pf2.plan("overworld", (0, 62, 0), (1, 63, 0), tolerance=0.5)
                self.assertIsNotNone(steps2)
                assert steps2 is not None
                self.assertEqual(steps2[0].action, "dig")
                self.assertEqual(steps2[0].target, (1, 63, 0))
            finally:
                client.close()

    def test_pathfinder_wide_river_corridor_xz_4_and_dry_puddle_avoidance(self) -> None:
        # 4-block wide river: x in 0..3 is water (y=61..62), shore bank at x=4 (y=62 stone, y=63..64 air)
        sim = VirtualMcApiServer(spawn_pos=(0.5, 62.0, 0.5))
        sim.fill(-2, 60, -5, 6, 60, 5, "stone")
        sim.fill(-2, 61, -5, -1, 62, 5, "stone")
        sim.fill(0, 61, -5, 3, 62, 5, "water", {"level": "0"})
        sim.fill(4, 61, -5, 6, 62, 5, "stone")

        with VirtualMcHttpServer(sim) as srv:
            client = MinecraftAgentClient(srv.base_url, "RiverBot", "pw")
            try:
                client.login()
                gw = VirtualWorldGateway(client)
                pf = Pathfinder3D(gw)
                # Cold cache start in water -> corridor_xz=4 prefetches across the wide river
                steps = pf.plan("overworld", (0, 62, 0), (4, 63, 0), tolerance=0.5)
                self.assertIsNotNone(steps)
                assert steps is not None
                self.assertEqual([s.action for s in steps], ["swim", "swim", "swim", "climb_shore"])
                self.assertEqual(steps[-1].stand_after, (4, 63, 0))

                # Dry bot at (-1, 63, 0) aiming at (-1, 63, 2) or across a 1-block water puddle at (0, 63, 0):
                # Put dry bridge/path around puddle at z=1..2 and water at (0, 63, 0)
                sim2 = VirtualMcApiServer(spawn_pos=(-1.5, 64.0, 0.5))
                sim2.fill(-2, 63, -2, 3, 63, 3, "stone")
                sim2.set_block(0, 64, 0, "water", {"level": "0"})
                with VirtualMcHttpServer(sim2) as srv2:
                    client2 = MinecraftAgentClient(srv2.base_url, "DryBot", "pw")
                    try:
                        client2.login()
                        gw2 = VirtualWorldGateway(client2)
                        pf2 = Pathfinder3D(gw2)
                        dry_steps = pf2.plan("overworld", (-1, 64, 0), (1, 64, 0), tolerance=0.5)
                        self.assertIsNotNone(dry_steps)
                        assert dry_steps is not None
                        self.assertTrue(all(s.action == "walk" for s in dry_steps))
                        self.assertNotIn((0, 64, 0), [s.stand_after for s in dry_steps])
                    finally:
                        client2.close()
            finally:
                client.close()

    def test_multihop_routes_water_to_swimmer_not_move_to(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 62.0, 0.5))
        sim.fill(-2, 60, -2, 5, 60, 2, "stone")
        sim.fill(-2, 61, -2, 2, 62, 2, "water", {"level": "0"})
        sim.fill(3, 61, -2, 5, 62, 2, "stone")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "NavSwimBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "NavSwimBot", "pw")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                nav = MultiHopNavigator(gw, journal)
                target = (3.5, 63.0, 0.5)  # dist2_xz=3.0 <= 10.0 -> Pathfinder3D + swimmer

                reached = False
                wet_actions = []
                for _ in range(10):
                    st = client.get_status()["result"]
                    gw.ingest_status_stance(st)
                    is_wet = bool(st.get("in_water") or st.get("submerged"))
                    out = nav.step_toward(target, st, tolerance=0.9, inventory_counts={})
                    if is_wet and out.status == "PROGRESSED":
                        wet_actions.append(out.action_taken)
                    if out.status == "REACHED":
                        reached = True
                        break
                self.assertTrue(reached, f"Failed to exit water via 3D A* swim/climb_shore; final pos={sim.pos}")
                self.assertGreaterEqual(len(wet_actions), 1)
                self.assertTrue(all(a == "control" for a in wet_actions))
                control_calls = [pl for ep, pl in sim.write_calls if ep == "control"]
                self.assertGreaterEqual(len(control_calls), 1)
                self.assertTrue(all(pl.get("sprint") is False for pl in control_calls))

                # Direct climb_shore step from water edge (2.5, 62.0, 0.5) onto bank (3, 63, 0) uses swimmer, not move_to
                sim.write_calls.clear()
                sim.pos = [2.5, 62.0, 0.5]
                st_edge = client.get_status()["result"]
                gw.ingest_status_stance(st_edge)
                out_edge = nav.step_toward((3.5, 63.0, 0.5), st_edge, tolerance=0.5, inventory_counts={})
                self.assertEqual(out_edge.status, "PROGRESSED")
                self.assertEqual(out_edge.action_taken, "control")
                self.assertEqual(out_edge.reason, "3d_climb_shore")
                self.assertEqual([ep for ep, _ in sim.write_calls if ep == "move_to"], [])
            finally:
                client.close()
                db.close()

    def test_surface_swim_center_alignment_and_sprint_false(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.2, 62.0, 0.2))
        sim.fill(-2, 60, -2, 4, 60, 4, "stone")
        sim.fill(-2, 61, -2, 4, 62, 4, "water", {"level": "0"})

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "AlignSwimBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "AlignSwimBot", "pw")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                swim = SurfaceSwimExecutor(client, journal, pulse_ms=400)
                st = client.get_status()["result"]
                # Unaligned target (1.9, 62.0, 0.9) must align to (1.5, 62.0, 0.5)
                res = swim.swim_step((1.9, 62.0, 0.9), st)
                self.assertEqual(res.status, "SWAM")
                ctrl_payloads = [pl for ep, pl in sim.write_calls if ep == "control"]
                self.assertFalse(ctrl_payloads[-1]["sprint"])
                expected_yaw = math.atan2(-(1.5 - 0.2), -(0.5 - 0.2))
                self.assertAlmostEqual(ctrl_payloads[-1]["yaw"], expected_yaw, places=2)

                # Stagnant pulse still rotates yaw laterally
                sim.pos = [0.2, 62.0, 0.2]
                st2 = client.get_status()["result"]
                res2 = swim.swim_step((1.9, 62.0, 0.9), st2)
                self.assertTrue(res2.details and res2.details.get("lateral_deflected"))
            finally:
                client.close()
                db.close()

    def test_local_recovery_5x3x5_bfs_routes_around_wall_to_dry_shore(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 62.0, 0.5))
        sim.fill(-3, 60, -3, 3, 64, 3, "stone")
        # Water at (0, 62, 0), (0, 62, 1), (1, 62, 1) with air headroom
        for wx, wz in ((0, 0), (0, 1), (1, 1)):
            sim.set_block(wx, 61, wz, "water", {"level": "0"})
            sim.set_block(wx, 62, wz, "water", {"level": "0"})
            sim.set_block(wx, 63, wz, "air")
            sim.set_block(wx, 64, wz, "air")
        # Closer shore (2, 62, 0) is walled off behind stone at (1, 62..63, 0)!
        sim.set_block(2, 61, 0, "stone")
        sim.set_block(2, 62, 0, "air")
        sim.set_block(2, 63, 0, "air")
        # Reachable shore via BFS around (0,62,1)->(1,62,1)->(1,62,2)
        sim.set_block(1, 61, 2, "stone")
        sim.set_block(1, 62, 2, "air")
        sim.set_block(1, 63, 2, "air")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "BfsShoreBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "BfsShoreBot", "pw")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rec = LocalRecoveryBranch(gw, journal, enable_auto_sleep=False)
                bb = Blackboard()
                bb.status = client.get_status()["result"]
                target = rec._pick_swim_target(bb)
                self.assertEqual(target, (1.5, 62.5, 2.5))
            finally:
                client.close()
                db.close()

    def test_local_recovery_auto_equips_best_armor_and_shield(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-2, 63, -2, 2, 63, 2, "stone")
        sim.armor["head"] = "leather_helmet"
        sim.give("diamond_helmet", 1)
        sim.give("golden_chestplate", 1)
        sim.give("iron_chestplate", 1)
        sim.give("shield", 1)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "AutoEquipBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "AutoEquipBot", "pw")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rec = LocalRecoveryBranch(gw, journal, enable_auto_sleep=False)
                bb = Blackboard()

                for _ in range(3):
                    bb.begin_tick()
                    bb.status = client.get_status()["result"]
                    bb.inventory = client.inventory()["result"]
                    bb.inventory_counts = client.get_inventory_counts()
                    st = rec.update(bb)
                    self.assertEqual(st, Status.RUNNING)
                    self.assertTrue(bb.last_action_outcome.startswith("recovery_equip:"))

                self.assertEqual(sim.armor["head"], "diamond_helmet")
                self.assertEqual(sim.armor["chest"], "iron_chestplate")
                self.assertEqual(sim.offhand_item, "shield")

                # Next tick with optimal gear equipped should fall through (FAILURE) with 0 new equips
                writes_before = len(sim.write_calls)
                bb.begin_tick()
                bb.status = client.get_status()["result"]
                bb.inventory = client.inventory()["result"]
                bb.inventory_counts = client.get_inventory_counts()
                self.assertEqual(rec.update(bb), Status.FAILURE)
                self.assertEqual(len(sim.write_calls), writes_before)

                # While submerged with low oxygen, surfacing takes priority over equipping
                sim.armor["feet"] = None
                sim.give("iron_boots", 1)
                sim.fill(-1, 61, -1, 1, 62, 1, "water", {"level": "0"})
                sim.pos = [0.5, 61.0, 0.5]
                sim.oxygen = 6
                bb.begin_tick()
                bb.status = client.get_status()["result"]
                bb.inventory = client.inventory()["result"]
                bb.inventory_counts = client.get_inventory_counts()
                self.assertEqual(rec.update(bb), Status.RUNNING)
                self.assertTrue(bb.last_action_outcome.startswith("recovery_swim:"))
            finally:
                client.close()
                db.close()


if __name__ == "__main__":
    unittest.main()
