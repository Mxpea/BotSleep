"""Part A regression tests: hysteresis + shield, hostile repulsion, Bounding-Sphere 4-level unstuck, failure state mutation."""

from __future__ import annotations

import tempfile
import unittest

from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt import BTRuntime, Blackboard, Status
from v2.gateway import MultiHopNavigator, Pathfinder3D, VirtualWorldGateway
from v2.kernel import MinecraftAgentClient, PendingTransactionJournal, UnifiedWorldDB
from v2.primitives import CombatPrimitive, ContainerPrimitive, DigCollectPrimitive, PlaceBuildPrimitive, PrimitiveContext


class TestAntiDeadlockAndHysteresis(unittest.TestCase):
    def test_hysteresis_enter_exit_radii_and_shield_raise_lower(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-20, 63, -20, 20, 63, 20, "stone")
        sim.give("shield", 1)
        # Skeleton starts at 10m (outside enter_radius=8m, inside exit_radius=12m) -> must NOT enter yet
        sim.add_entity(101, "skeleton", (10.5, 64.0, 0.5), kind="hostile")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "HystBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "HystBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False, threat_radius=8.0)
                rt.hard_safety.threat_dwell_seconds = 0.0  # test pure radius hysteresis deterministically

                # Distance 10m > enter_radius(8m) -> HardSafetyBranch stays inactive, still publishes repulsion coord!
                rt.tick_once()
                self.assertNotEqual(rt.bb.active_branch, "HardSafetyBranch")
                self.assertEqual(len(rt.bb.get("active_threat_positions") or []), 1)

                # Skeleton closes to 6m (<= enter_radius=8m) -> enters evasion and raises shield!
                sim.entities[0]["pos"] = [6.5, 64.0, 0.5]
                rt.tick_once()
                self.assertEqual(rt.bb.active_branch, "HardSafetyBranch")
                self.assertEqual(rt.bb.last_action_outcome, "safety_shield_raise:skeleton")
                self.assertTrue(sim.blocking)
                self.assertTrue(rt.bb.get("threat_hysteresis_active"))

                # Skeleton backs off to 10m (between enter=8m and exit=12m) -> MUST stay in hysteresis evasion!
                sim.entities[0]["pos"] = [sim.pos[0] + 10.0, 64.0, sim.pos[2]]
                rt.tick_once()
                self.assertEqual(rt.bb.active_branch, "HardSafetyBranch")
                self.assertTrue(rt.bb.last_action_outcome.startswith("safety_retreat"))

                # Skeleton moves past exit_radius (18m) -> exits hysteresis and lowers shield!
                sim.entities[0]["pos"] = [sim.pos[0] + 18.0, 64.0, sim.pos[2]]
                rt.tick_once()
                self.assertEqual(rt.bb.last_action_outcome, "safety_shield_lower")
                self.assertFalse(sim.blocking)
                self.assertFalse(rt.bb.get("threat_hysteresis_active"))
            finally:
                client.close()
                db.close()

    def test_pathfinder_and_navigator_deflect_around_active_hostile(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-4, 63, -12, 16, 63, 12, "stone")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "RepulseBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "RepulseBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                pf = Pathfinder3D(gw)
                nav = MultiHopNavigator(gw, journal, pathfinder=pf)

                # Threat sitting directly on the straight line (5, 64, 0) between (0,64,0) and (10,64,0)
                threat = [(5.5, 64.0, 0.5)]
                steps = pf.plan("overworld", (0, 64, 0), (10, 64, 0), tolerance=1.0, threat_positions=threat)
                self.assertIsNotNone(steps)
                # At least one step must bow laterally away from z=0 to reduce threat proximity cost!
                max_abs_z = max(abs(s.stand_after[2]) for s in (steps or []))
                self.assertGreaterEqual(max_abs_z, 2)

                # Direct navigator waypoint must also deflect laterally when straight path crosses threat!
                wx, wz, deflected = nav._deflect_waypoint_from_threats((0.5, 64.0, 0.5), 14.5, 0.5, threat)
                self.assertTrue(deflected)
                self.assertGreaterEqual(abs(wz - 0.5), 8.0)
            finally:
                client.close()
                db.close()

    def test_bounding_sphere_catches_ab_oscillation_and_escalates_l1_to_l4(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-50, 63, -50, 50, 63, 50, "stone")
        # Place a soft leaf block next to player for L2 to clear
        sim.set_block(0, 65, 1, "oak_leaves")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "UnstuckBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "UnstuckBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                # Tune window small for fast deterministic test
                rt.watchdog.sphere_window = 6
                rt.watchdog.escalation_ticks = 2

                class _OscillatingGoal:
                    name = "ab_oscillate"
                    def __init__(self) -> None:
                        self.toggle = False
                    def tick(self, bb: Blackboard) -> str:
                        self.toggle = not self.toggle
                        # Ping-pong between two adjacent spots (0.5 and 1.2) -> never repeats exact sig 6x in a row,
                        # yet stays strictly inside bounding sphere radius < 1.6m!
                        sim.pos[0] = 1.2 if self.toggle else 0.5
                        bb.last_action_outcome = "ping" if self.toggle else "pong"
                        return "RUNNING"
                    def stop(self, bb: Blackboard) -> None:
                        pass

                rt.mount_goal(_OscillatingGoal())
                seen_outcomes = []
                for _ in range(24):
                    rt.tick_once()
                    seen_outcomes.append(rt.bb.last_action_outcome)
                    # Keep sim.pos pinned near origin until L4 triggers so we observe full L1 -> L2 -> L4 ladder
                    if rt.bb.last_action_outcome.startswith("unstuck_l1"):
                        sim.pos[0] = 0.5
                        sim.pos[2] = 0.5
                    if rt.bb.last_action_outcome.startswith("unstuck_l4"):
                        break

                self.assertTrue(any(o == "unstuck_l1_control_pulse" for o in seen_outcomes), seen_outcomes)
                self.assertTrue(any(o == "unstuck_l2_soft_clear" for o in seen_outcomes), seen_outcomes)
                self.assertEqual(sim.block_name(0, 65, 1), "air")  # L2 actually dug the soft leaf!
                self.assertTrue(any(o.startswith("unstuck_l4_relocate") for o in seen_outcomes), seen_outcomes)
                # L4 must have recorded excluded sector on Blackboard!
                self.assertTrue(bool(rt.bb.get("excluded_sectors")))
            finally:
                client.close()
                db.close()

    def test_l3_tall_canopy_lateral_to_trunk_descent_without_lethal_drop(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 80.0, 0.5))
        sim.fill(-6, 63, -6, 6, 63, 6, "grass_block")
        # Canopy leaf platform at y=79 from x=0..2 with a tree trunk column at x=2, y=64..79!
        sim.set_block(0, 79, 0, "jungle_leaves")
        sim.set_block(1, 79, 0, "jungle_leaves")
        for y in range(64, 80):
            sim.set_block(2, y, 0, "jungle_log")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "CanopyL3Bot", "w1")
            client = MinecraftAgentClient(srv.base_url, "CanopyL3Bot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)

                # Tick 1: perched on tall leaf (16m above ground) -> must NOT dig (0,79,0); must step laterally toward trunk!
                rt.tick_once()
                self.assertEqual(sim.block_name(0, 79, 0), "jungle_leaves")
                self.assertEqual(rt.bb.last_action_outcome, "unstuck_l3_canopy_lateral_to_trunk")
                self.assertGreater(sim.pos[0], 0.9)
            finally:
                client.close()
                db.close()

    def test_primitive_failures_mutate_blackboard_and_unreachable_costs(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-4, 63, -4, 4, 63, 4, "stone")
        sim.set_block(2, 64, 0, "stone")  # Occupied cell
        sim.give("cobblestone", 4)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "FailMutBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "FailMutBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                place = PlaceBuildPrimitive(ctx)
                bb = Blackboard()
                bb.status = client.get_status()["result"]
                bb.inventory_counts = {"cobblestone": 4}

                # Place into occupied cell -> fails and mutates BB + unreachable + invalidates stale cache!
                gw.put_voxel("overworld", (2, 64, 0), "air", source="EXACT")
                out = place.place_at(bb, "cobblestone", (2, 64, 0))
                self.assertEqual(out.status, "FAILED_LOCAL")
                self.assertIn("place:", bb.last_failure_reason)
                self.assertTrue(bool(bb.get("recent_primitive_failures")))
                self.assertTrue(gw.peek_block("overworld", (2, 64, 0)).must_reverify)
            finally:
                client.close()
                db.close()

    def test_night_sleep_scan_is_typed_and_throttled_not_untyped_spam(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-6, 63, -6, 6, 63, 6, "stone")
        sim.time_of_day = 15000  # night, NO beds nearby

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "NightBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "NightBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=True)

                for _ in range(6):
                    rt.tick_once()

                find_calls = [args for ep, args in sim.read_calls if ep == "find_blocks"]
                # Must have scanned AT MOST ONCE across 6 night ticks, and ALWAYS with explicit typed bed list!
                self.assertEqual(len(find_calls), 1)
                self.assertTrue(bool(find_calls[0].get("types")))
                self.assertIn("white_bed", find_calls[0]["types"])
            finally:
                client.close()
                db.close()


if __name__ == "__main__":
    unittest.main()
