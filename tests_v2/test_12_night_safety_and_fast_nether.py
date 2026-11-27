"""Tests for night safety (dry retreat deflection, low-HP burrow), fast nether gate, underground water escape, and bounded lava search."""

from __future__ import annotations

import math
import tempfile
import unittest

from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt import BTRuntime, Blackboard, Status
from v2.gateway import VirtualWorldGateway
from v2.kernel import MinecraftAgentClient, PendingTransactionJournal, UnifiedWorldDB
from v2.planner import HorizonSelector
from v2.primitives import PrimitiveContext
from v2.tasks import AcquireItemTask, EnterNetherTask, ReturnToSurfaceTask, TaskCatalogue, is_underground
from v2.tasks.competition import CompetitionManager


class TestNightSafetyAndFastNether(unittest.TestCase):
    def test_hard_safety_retreat_deflects_laterally_away_from_water_and_lava(self) -> None:
        # Platform dry on x <= 0 (z in [-14, 14]), water across x in [1, 12]
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-12, 63, -14, 0, 63, 14, "stone")
        sim.fill(1, 63, -14, 12, 63, 14, "water", {"level": "0"})
        # Zombie west at (-2.5, 64, 0.5) -> naive retreat vector is +X straight into water!
        sim.add_entity(101, "zombie", (-2.5, 64.0, 0.5), kind="hostile")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "DryRetreatBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "DryRetreatBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False, threat_radius=8.0)

                rt.tick_once()
                self.assertEqual(rt.bb.active_branch, "HardSafetyBranch")
                self.assertEqual(rt.bb.last_action_outcome, "safety_retreat:zombie")
                # Must have deflected laterally along Z on dry stone (x <= 0.5), NOT stepped into water (x >= 1)!
                self.assertLessEqual(sim.pos[0], 0.5)
                self.assertGreaterEqual(abs(sim.pos[2] - 0.5), 4.0)
                self.assertNotEqual(sim.block_name(math.floor(sim.pos[0]), 63, math.floor(sim.pos[2])), "water")
            finally:
                client.close()
                db.close()

        # Repeat with lava directly behind bot at x >= 1
        sim_lava = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim_lava.fill(-12, 63, -14, 0, 63, 14, "stone")
        sim_lava.fill(1, 63, -14, 12, 63, 14, "lava", {"level": "0"})
        sim_lava.add_entity(102, "skeleton", (-2.5, 64.0, 0.5), kind="hostile")
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim_lava) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "DryLavaBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "DryLavaBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False, threat_radius=8.0)
                rt.tick_once()
                self.assertEqual(rt.bb.last_action_outcome, "safety_retreat:skeleton")
                self.assertLessEqual(sim_lava.pos[0], 0.5)
                self.assertGreaterEqual(abs(sim_lava.pos[2] - 0.5), 4.0)
            finally:
                client.close()
                db.close()

    def test_night_low_hp_emergency_burrow_shelter_and_exit_rules(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        # Thick 4-layer solid ground y=60..63 so digging 2 blocks down leaves safe floor y=61 and y=60
        sim.fill(-10, 60, -10, 10, 63, 10, "stone")
        sim.time_of_day = 15000  # night
        sim.health = 7.0  # <= 8.0 low HP
        sim.food = 12
        sim.give("cobblestone", 4)
        sim.give("bread", 4)
        sim.add_entity(201, "skeleton", (5.5, 64.0, 0.5), kind="hostile")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "BurrowBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "BurrowBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)

                for _ in range(5):
                    rt.tick_once()
                    self.assertLessEqual(rt.bb.writes_this_tick, 1)
                    if rt.bb.get("in_sealed_burrow"):
                        break

                self.assertTrue(rt.bb.get("in_sealed_burrow"))
                self.assertEqual(sim.pos[1], 62.0)  # dug down 2 blocks (63, 62)
                self.assertEqual(sim.block_name(0, 64, 0), "cobblestone")  # capped overhead!

                # While night and health < 14.0, burrow stays sealed and HardSafety yields to LocalRecovery (eat)
                rt.tick_once()
                self.assertTrue(rt.bb.get("in_sealed_burrow"))
                self.assertEqual(rt.bb.active_branch, "LocalRecoveryBranch")
                self.assertEqual(rt.bb.last_action_outcome, "recovery_eat:bread")

                # If health >= 14.0 while skeleton is STILL within threat_radius, stay sealed!
                sim.health = 16.0
                rt.tick_once()
                self.assertTrue(rt.bb.get("in_sealed_burrow"))

                # Once threats move away (safe) and health >= 14.0, clear in_sealed_burrow!
                sim.entities.clear()
                rt.tick_once()
                self.assertFalse(rt.bb.get("in_sealed_burrow"))

                # Re-verify dawn clearing: if sealed at low HP and dawn arrives, clear in_sealed_burrow!
                rt.bb.set("in_sealed_burrow", True, writer="test")
                sim.health = 7.0
                sim.time_of_day = 1000  # day
                rt.tick_once()
                self.assertFalse(rt.bb.get("in_sealed_burrow"))
            finally:
                client.close()
                db.close()

    def test_night_burrow_refuses_lava_below_and_respects_pending_unknown(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-10, 60, -10, 10, 63, 10, "stone")
        # Lava right below burrow target (0, 61, 0) -> MUST refuse to dig down!
        sim.set_block(0, 61, 0, "lava", {"level": "0"})
        sim.time_of_day = 15000
        sim.health = 6.0
        sim.give("cobblestone", 4)
        sim.add_entity(202, "skeleton", (5.5, 64.0, 0.5), kind="hostile")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "SafeBurrowBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "SafeBurrowBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)

                rt.tick_once()
                self.assertFalse(rt.bb.get("in_sealed_burrow"))
                self.assertEqual(sim.pos[1], 64.0)
                self.assertEqual(rt.bb.last_action_outcome, "safety_retreat:skeleton")

                # PendingUnknownGuard still freezes before HardSafetyBranch
                tx_id = journal.begin_write("dig", {"pos": [0, 63, 0]}, dimension="overworld", pos=sim.pos)
                journal.resolve_write(tx_id, "unknown", details={"code": "timeout"})
                writes_before = len(sim.write_calls)
                rt.tick_once()
                self.assertEqual(rt.bb.active_branch, "PendingUnknownGuard")
                self.assertEqual(len(sim.write_calls), writes_before)
            finally:
                client.close()
                db.close()

    def test_horizon_selector_fast_tracks_portal_gate_and_suppresses_obsolete_loops(self) -> None:
        cat = TaskCatalogue()
        hs_daily = HorizonSelector(cat, objective="daily")
        bb = Blackboard()
        bb.status = {"dimension": "overworld", "pos": [0.5, 64.0, 0.5]}
        bb.inventory_counts = {"iron_ingot": 4, "oak_planks": 8}
        top = hs_daily.select_top(bb, limit=5)
        self.assertEqual(top[0].goal, "enter_nether")
        self.assertEqual(top[0].priority_tag, "PORTAL_GATE_READY")
        items = [c.item for c in top]
        self.assertNotIn("wooden_pickaxe", items)
        self.assertNotIn("stone_pickaxe", items)
        self.assertNotIn("furnace", items)

        # Competition mode also fast-tracks enter_the_nether #1 even with conditional overrides expanding pool > 24
        comp = CompetitionManager(cat, eligibility_overrides={"minecraft:story/enter_the_end": True})
        hs_comp = HorizonSelector(cat, objective="competition", competition=comp)
        top_comp = hs_comp.select_top(bb, limit=5)
        self.assertEqual(top_comp[0].item, "minecraft:story/enter_the_nether")
        self.assertEqual(top_comp[0].priority_tag, "PORTAL_GATE_READY")

    def test_acquire_water_bucket_underground_returns_to_surface_and_flint_uses_carried_gravel(self) -> None:
        # 1) Underground with bucket and distant cave water pocket -> must trigger ReturnToSurfaceTask first!
        sim = VirtualMcApiServer(spawn_pos=(0.5, 42.0, 0.5))
        sim.fill(-6, 40, -6, 6, 48, 6, "stone")
        sim.fill(0, 42, 0, 2, 43, 0, "air")
        # Distant exposed cave water 16m away (should NOT wander caves for it!)
        sim.fill(14, 41, 0, 18, 43, 0, "air")
        sim.set_block(17, 41, 0, "water", {"level": "0"})
        sim.give("bucket", 1)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "CaveWaterBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "CaveWaterBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                bb = Blackboard()
                bb.begin_tick()
                bb.status = client.get_status()["result"]
                bb.inventory_counts = dict(sim.inventory_counts)

                self.assertTrue(is_underground(bb, gw))
                task = AcquireItemTask(ctx, cat, "water_bucket", 1)
                res = task.on_tick(bb)
                self.assertEqual(res, "RUNNING")
                self.assertIsInstance(task._active_child, ReturnToSurfaceTask)
            finally:
                client.close()
                db.close()

        # 2) Carried gravel place->dig loop for flint runs BEFORE distant natural gravel!
        sim2 = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim2.fill(-6, 63, -6, 26, 63, 6, "stone")
        # Distant natural gravel at x=22 (within radius=32)
        sim2.set_block(22, 64, 0, "gravel")
        sim2.give("gravel", 2)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim2) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "FlintBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "FlintBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.mount_goal(AcquireItemTask(ctx, cat, "flint", 1))

                for _ in range(8):
                    rt.tick_once()
                    if sim2.inventory_counts.get("flint", 0) >= 1:
                        break
                self.assertGreaterEqual(sim2.inventory_counts.get("flint", 0), 1)
                # Must NOT have walked 22m to distant gravel!
                self.assertLess(sim2.pos[0], 6.0)
                self.assertEqual(sim2.block_name(22, 64, 0), "gravel")
            finally:
                client.close()
                db.close()

    def test_enter_nether_bounded_lava_search_excludes_visited_sectors_and_descends_to_y11(self) -> None:
        # Case A: Surface has zero lava; deep shaft around 4th wide sector (-Z) leads down to y=11 where (0, 10, -115) lava becomes visible!
        sim_deep = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5), loaded_radius=140)
        sim_deep.fill(-60, 63, -60, 60, 63, 60, "stone")
        # Solid descent column around (0, 8..63, -52) so staircase spirals down safely without hitting void
        sim_deep.fill(-6, 8, -58, 6, 63, -46, "stone")
        # Exposed underground lava cavern at (0, 10, -121): >64m until descending near y<=15
        sim_deep.fill(-2, 9, -123, 2, 12, -119, "stone")
        sim_deep.fill(-1, 10, -122, 1, 12, -120, "air")
        sim_deep.set_block(0, 10, -121, "lava", {"level": "0"})
        sim_deep.give("iron_pickaxe", 1)
        sim_deep.give("water_bucket", 1)
        sim_deep.give("flint_and_steel", 1)
        sim_deep.give("cobblestone", 16)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim_deep) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "DeepLavaBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "DeepLavaBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                task = EnterNetherTask(ctx, cat)
                rt.mount_goal(task)

                found_lava = False
                for _ in range(280):
                    rt.tick_once()
                    if task._portal_anchor is not None:
                        found_lava = True
                        break
                self.assertTrue(bool(rt.bb.get("excluded_sectors")))
                self.assertGreaterEqual(len(rt.bb.get("excluded_sectors") or {}), 4)
                self.assertTrue(found_lava, f"Failed to find y=10 lava; pos={sim_deep.pos}, anchor={task._portal_anchor}")
                self.assertLessEqual(sim_deep.pos[1], 24.0)
            finally:
                client.close()
                db.close()

        # Case B: Completely lava-free flat world -> must bound exploration, exclude sectors, and exit cleanly with NEEDS_AI
        sim_empty = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5), loaded_radius=128)
        sim_empty.fill(-120, 63, -120, 120, 63, 120, "stone")
        sim_empty.give("iron_pickaxe", 1)
        sim_empty.give("water_bucket", 1)
        sim_empty.give("flint_and_steel", 1)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim_empty) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "NoLavaBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "NoLavaBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                task = EnterNetherTask(ctx, cat)
                rt.mount_goal(task)

                for _ in range(80):
                    rt.tick_once()
                    if rt.bb.goal_status == "NEEDS_AI":
                        break
                self.assertEqual(rt.bb.goal_status, "NEEDS_AI")
                self.assertEqual(rt.bb.last_failure_reason, "no_lava_pool_found")
                self.assertGreaterEqual(len(rt.bb.get("excluded_sectors") or {}), 8)
            finally:
                client.close()
                db.close()


if __name__ == "__main__":
    unittest.main()
