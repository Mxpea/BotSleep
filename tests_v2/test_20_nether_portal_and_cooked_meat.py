"""R6 tests: water-stall cooldown & overhang dig, land-first portal sweep, ruined/unlit portal reuse, collect=False lava descent, and raw meat batch cooking."""

from __future__ import annotations

import math
import tempfile
import unittest

from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt import BTRuntime, Status
from v2.bt.blackboard import Blackboard
from v2.gateway import VirtualWorldGateway
from v2.kernel import MinecraftAgentClient, PendingTransactionJournal, UnifiedWorldDB
from v2.planner import HorizonSelector
from v2.primitives import PrimitiveContext
from v2.tasks import EnterNetherTask, SmeltItemTask, TaskCatalogue


class TestNetherPortalAndCookedMeat(unittest.TestCase):
    def test_local_recovery_water_stall_digs_overhang_and_respects_cooldown(self) -> None:
        # Phase 1: trapped in water with solid bank floor (1,61,0) and rock overhang at (1,63,0) -> must dig overhang
        sim = VirtualMcApiServer(spawn_pos=(0.5, 61.0, 0.5))
        sim.fill(-3, 59, -3, 3, 60, 3, "stone")
        sim.fill(-1, 61, -1, 1, 62, 1, "water", {"level": "0"})
        sim.set_block(1, 61, 0, "stone")
        sim.set_block(1, 62, 0, "air")
        sim.set_block(1, 63, 0, "stone")
        sim.give("stone_pickaxe", 1)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "WaterOverhangBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "WaterOverhangBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                bb = rt.bb
                rec = rt.local_recovery

                for _ in range(4):
                    sim.pos = [0.5, 61.0, 0.5]
                    bb.begin_tick()
                    bb.status = {
                        "pos": [0.5, 61.0, 0.5],
                        "dimension": "overworld",
                        "in_water": True,
                        "submerged": True,
                        "oxygen": 14,
                        "health": 20.0,
                        "food": 20,
                    }
                    bb.inventory_counts = {"stone_pickaxe": 1}
                    st = rec.update(bb)
                    if bb.last_action_outcome == "recovery_swim_dig_bank":
                        break
                    self.assertEqual(st, Status.RUNNING)

                self.assertEqual(bb.last_action_outcome, "recovery_swim_dig_bank")
                self.assertEqual(sim.block_name(1, 63, 0), "air")
            finally:
                client.close()
                db.close()

        # Phase 2: open-water stall yields to PhysicalUnstuckBranch & cooldown stops instant re-preemption while oxygen>=8
        sim2 = VirtualMcApiServer(spawn_pos=(0.5, 61.0, 0.5))
        sim2.fill(-3, 59, -3, 3, 60, 3, "stone")
        sim2.fill(-2, 61, -2, 2, 62, 2, "water", {"level": "0"})

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim2) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "WaterCooldownBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "WaterCooldownBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                bb = rt.bb
                rec = rt.local_recovery

                st = Status.RUNNING
                for _ in range(5):
                    sim2.pos = [0.9, 61.0, 0.5]
                    bb.begin_tick()
                    bb.status = {
                        "pos": [0.9, 61.0, 0.5],
                        "dimension": "overworld",
                        "in_water": True,
                        "submerged": True,
                        "oxygen": 14,
                        "health": 20.0,
                        "food": 20,
                    }
                    bb.inventory_counts = {}
                    st = rec.update(bb)
                    if st == Status.FAILURE:
                        break
                self.assertEqual(st, Status.FAILURE)
                self.assertEqual(bb.last_action_outcome, "recovery_swim_yield")

                # While cooldown active and oxygen >= 8, submerged=True still yields instead of looping forever
                bb.begin_tick()
                bb.status = {
                    "pos": [0.9, 61.0, 0.5],
                    "dimension": "overworld",
                    "in_water": True,
                    "submerged": True,
                    "oxygen": 14,
                    "health": 20.0,
                    "food": 20,
                }
                self.assertEqual(rec.update(bb), Status.FAILURE)

                # Once oxygen drops critically low (< 8), water rescue MUST immediately preempt regardless of cooldown
                bb.begin_tick()
                bb.status = {
                    "pos": [0.9, 61.0, 0.5],
                    "dimension": "overworld",
                    "in_water": True,
                    "submerged": True,
                    "oxygen": 6,
                    "health": 20.0,
                    "food": 20,
                }
                self.assertEqual(rec.update(bb), Status.RUNNING)
                self.assertTrue(bb.last_action_outcome.startswith("recovery_swim:"))
            finally:
                client.close()
                db.close()

    def test_enter_nether_land_first_surface_sweep_skips_water_and_pivots(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5), loaded_radius=96)
        sim.fill(-30, 63, -30, 30, 63, 30, "stone")
        # Open ocean along +X (dir 0) and +Z (dir 1), dry stone along -X (dir 2)
        sim.fill(5, 62, -6, 26, 63, 6, "water", {"level": "0"})
        sim.fill(-6, 62, 5, 6, 63, 26, "water", {"level": "0"})

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "LandPortalSweepBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "LandPortalSweepBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                task = EnterNetherTask(ctx, TaskCatalogue())
                bb = Blackboard()
                bb.begin_tick()
                bb.status = {"pos": [0.5, 64.0, 0.5], "dimension": "overworld", "in_water": False}

                wp = task._pick_next_surface_sector(bb)
                self.assertIsNotNone(wp)
                assert wp is not None
                # Must skip water sectors (+X, +Z) and choose dry -X direction!
                self.assertLess(wp[0], 0.0)
                excluded = bb.get("excluded_sectors") or {}
                self.assertIn((math.floor(52.5 / 16.0), math.floor(0.5 / 16.0)), excluded)
                self.assertIn((math.floor(0.5 / 16.0), math.floor(52.5 / 16.0)), excluded)

                # If already in water during active sector travel, _search_lava_pool must exclude and pivot!
                task._current_sector_target = wp
                bb.status = {"pos": [0.5, 64.0, 0.5], "dimension": "overworld", "in_water": True}
                res = task._search_lava_pool(bb)
                self.assertEqual(res, "RUNNING")
                self.assertIsNone(task._current_sector_target)
                self.assertGreaterEqual(task._surface_sectors_visited, 1)
            finally:
                client.close()
                db.close()

    def test_enter_nether_complete_unlit_obsidian_portal_ignites_without_lava_or_bucket(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(3.5, 64.0, 0.5))
        sim.fill(-4, 63, -4, 6, 63, 6, "stone")
        # Z-aligned complete 4x5 unlit portal at anchor=(0, 64, 0)
        anchor = (0, 64, 0)
        for cell in EnterNetherTask._frame_cells(anchor, axis="z"):
            sim.set_block(*cell, "obsidian")
        # Doorway / interior clutter block to verify clearing + ignition without lava/bucket/iron_pickaxe
        sim.set_block(0, 64, 0, "cobblestone")
        sim.give("flint_and_steel", 1)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "UnlitPortalBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "UnlitPortalBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                task = EnterNetherTask(ctx, TaskCatalogue())
                rt.mount_goal(task)

                for _ in range(12):
                    rt.tick_once()
                    if sim.block_name(0, 64, 0) == "nether_portal" or sim.dimension == "the_nether":
                        break

                self.assertTrue(task._portal_complete_unlit)
                self.assertEqual(task._portal_axis, "z")
                self.assertEqual(task._portal_anchor, anchor)
                self.assertTrue(
                    sim.block_name(0, 64, 0) == "nether_portal" or sim.dimension == "the_nether"
                )
            finally:
                client.close()
                db.close()

    def test_enter_nether_partial_ruined_portal_reused_and_crying_obsidian_rejected(self) -> None:
        # Case A: Crying obsidian inside candidate frame MUST be rejected cleanly
        sim_cry = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 4.5))
        sim_cry.fill(-4, 63, -4, 6, 63, 6, "stone")
        anchor = (0, 64, 0)
        frame = EnterNetherTask._frame_cells(anchor, axis="x")
        for cell in frame[:6]:
            sim_cry.set_block(*cell, "obsidian")
        sim_cry.set_block(*frame[6], "crying_obsidian")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim_cry) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "CryObsBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "CryObsBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                task = EnterNetherTask(PrimitiveContext(gw, journal), TaskCatalogue())
                bb = Blackboard()
                bb.begin_tick()
                bb.status = {"pos": [0.5, 64.0, 4.5], "dimension": "overworld"}
                obs_anchor, _, obs_count = task._scan_existing_obsidian_portal(bb, radius=32)
                self.assertIsNone(obs_anchor)
                self.assertEqual(obs_count, 0)
            finally:
                client.close()
                db.close()

        # Case B: Valid 6-obsidian ruined portal + nearby lava -> reuses ruined portal anchor directly!
        sim_ruin = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 4.5))
        sim_ruin.fill(-8, 63, -8, 14, 63, 10, "stone")
        for cell in frame[:6]:
            sim_ruin.set_block(*cell, "obsidian")
        sim_ruin.set_block(10, 63, 0, "lava", {"level": "0"})
        sim_ruin.give("iron_pickaxe", 1)
        sim_ruin.give("water_bucket", 1)
        sim_ruin.give("flint_and_steel", 1)
        sim_ruin.give("cobblestone", 32)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim_ruin) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "RuinPortalBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "RuinPortalBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                task = EnterNetherTask(PrimitiveContext(gw, journal), TaskCatalogue())
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.mount_goal(task)
                rt.tick_once()
                self.assertEqual(task._portal_anchor, anchor)
                self.assertEqual(task._portal_axis, "x")
                self.assertFalse(task._portal_complete_unlit)
            finally:
                client.close()
                db.close()

    def test_enter_nether_lava_descent_uses_collect_false_when_cobble_stocked(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 11.0, 0.5))
        sim.fill(-3, 9, -3, 3, 14, 3, "stone")
        sim.set_block(0, 11, 0, "air")
        sim.set_block(0, 12, 0, "air")
        sim.give("iron_pickaxe", 1)
        sim.give("cobblestone", 16)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "LavaDigBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "LavaDigBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                task = EnterNetherTask(PrimitiveContext(gw, journal), TaskCatalogue())
                bb = Blackboard()
                bb.begin_tick()
                bb.status = client.get_status()["result"]
                bb.inventory_counts = dict(sim.inventory_counts)

                res = task._step_underground_lava_descent(bb)
                self.assertEqual(res, "RUNNING")
                dig_calls = [w for w in sim.write_calls if w[0] == "dig"]
                self.assertEqual(len(dig_calls), 1)
                self.assertFalse(dig_calls[0][1]["collect"])
            finally:
                client.close()
                db.close()

    def test_cooked_meat_horizon_candidate_and_batch_smelt(self) -> None:
        hs = HorizonSelector(TaskCatalogue(), objective="daily")
        bb = Blackboard()
        bb.status = {"dimension": "overworld", "pos": [0.5, 64.0, 0.5], "food": 12}
        bb.inventory_counts = {"beef": 4, "coal": 1, "furnace": 1}
        top = hs.select_top(bb, limit=5)
        cook = [c for c in top if c.priority_tag == "COOK_FOOD"]
        self.assertEqual(len(cook), 1)
        self.assertEqual(cook[0].goal, "acquire")
        self.assertEqual(cook[0].item, "cooked_beef")
        self.assertEqual(cook[0].count, 4)

        # Suppress when >= 4 cooked food already held or when CORPSE_RECOVERY active
        bb.inventory_counts["cooked_beef"] = 4
        self.assertIsNone(HorizonSelector._cook_food_candidate(bb, None))
        bb.inventory_counts["cooked_beef"] = 0
        bb.set("active_corpse_drop", {"pos": [10.0, 64.0, 0.0], "loaded_elapsed": 5.0}, writer="test")
        top_corpse = hs.select_top(bb, limit=5)
        self.assertEqual(top_corpse[0].priority_tag, "CORPSE_RECOVERY")
        self.assertNotIn("COOK_FOOD", [c.priority_tag for c in top_corpse])

        # Batch smelt 4 raw beef on 1 coal cleanly
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-3, 63, -3, 6, 63, 3, "stone")
        sim.set_block(2, 64, 0, "furnace")
        sim.smelt_ticks_required = 1
        sim.give("beef", 4)
        sim.give("coal", 1)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "CookBeefBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "CookBeefBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.mount_goal(SmeltItemTask(ctx, TaskCatalogue(), "cooked_beef", 4, poll_interval_seconds=0.0))

                for _ in range(15):
                    rt.tick_once()
                    if rt.bb.goal_status == "COMPLETED":
                        break

                self.assertEqual(rt.bb.goal_status, "COMPLETED")
                self.assertEqual(sim.inventory_counts.get("cooked_beef", 0), 4)
                self.assertEqual(sim.inventory_counts.get("beef", 0), 0)

                # With 0 beef left and cooked_beef>0, SmeltItemTask completes early instead of recursing on beef
                rt.mount_goal(SmeltItemTask(ctx, TaskCatalogue(), "cooked_beef", 6, poll_interval_seconds=0.0))
                rt.tick_once()
                rt.tick_once()
                self.assertEqual(rt.bb.goal_status, "COMPLETED")

                # With 0 beef AND 0 cooked_beef left, SmeltItemTask fails cleanly as no_raw_food:beef
                sim.consume("cooked_beef", 4)
                rt.mount_goal(SmeltItemTask(ctx, TaskCatalogue(), "cooked_beef", 4, poll_interval_seconds=0.0))
                rt.tick_once()
                rt.tick_once()
                self.assertEqual(rt.bb.goal_status, "FAILED_LOCAL")
                self.assertEqual(rt.bb.last_failure_reason, "no_raw_food:beef")
            finally:
                client.close()
                db.close()


if __name__ == "__main__":
    unittest.main()
