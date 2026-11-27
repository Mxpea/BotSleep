"""R5 tests: unreachable exponent clamp, in-water place guard, adaptive collectDrops stall circuit-breaker, surface no-pickaxe guard, SmeltItemTask material/fuel gating, opportunistic underground mining, and mid/late HorizonSelector ladder."""

from __future__ import annotations

import itertools
import tempfile
import unittest
from unittest.mock import patch

from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt import BTRuntime
from v2.bt.blackboard import Blackboard
from v2.gateway import BlockUnreachableTracker, VirtualWorldGateway
from v2.kernel import MinecraftAgentClient, PendingTransactionJournal, UnifiedWorldDB
from v2.planner import HorizonSelector
from v2.primitives import DigCollectPrimitive, PlaceBuildPrimitive, PrimitiveContext
from v2.tasks import MineResourceTask, ReturnToSurfaceTask, SmeltItemTask, TaskCatalogue


class TestFastDigSmeltAndMidgame(unittest.TestCase):
    def test_unreachable_tracker_clamps_huge_count_without_overflow(self) -> None:
        tr = BlockUnreachableTracker(base_ttl_seconds=60.0, max_ttl_seconds=600.0)
        for _ in range(1500):
            cnt = tr.record_failure("overworld", (10, 69, -4), "move_to", "canopy_stall", now=1000.0)
        self.assertEqual(cnt, 1500)
        self.assertTrue(tr.is_blocked("overworld", (10, 69, -4), "move_to", now=1599.0))
        self.assertFalse(tr.is_blocked("overworld", (10, 69, -4), "move_to", now=1601.0))

    def test_place_near_player_rejects_in_water_without_http_write(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 62.0, 0.5))
        sim.fill(-3, 60, -3, 3, 62, 3, "water", {"level": "0"})
        sim.give("crafting_table", 1)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "WaterPlaceBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "WaterPlaceBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                place = PlaceBuildPrimitive(PrimitiveContext(gw, journal))
                bb = Blackboard()
                bb.begin_tick()
                bb.status = {"pos": [0.5, 62.0, 0.5], "dimension": "overworld", "in_water": True, "submerged": False}
                bb.inventory_counts = {"crafting_table": 1}

                out = place.place_near_player(bb, "crafting_table")
                self.assertEqual(out.status, "FAILED_LOCAL")
                self.assertEqual(out.reason, "in_water_no_support")
                self.assertEqual(bb.writes_this_tick, 0)
                self.assertEqual([w for w in sim.write_calls if w[0] == "place"], [])
            finally:
                client.close()
                db.close()

    def test_adaptive_collect_circuit_breaker_trips_after_slow_overhead(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-4, 63, -4, 6, 63, 4, "stone")
        sim.set_block(1, 64, 0, "iron_ore")
        sim.set_block(2, 64, 0, "iron_ore")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "CollectBreakBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "CollectBreakBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                dig = DigCollectPrimitive(PrimitiveContext(gw, journal))
                bb = Blackboard()

                # First dig: simulate normal collect=True (0 overhead) -> stays collect=True
                bb.begin_tick()
                bb.status = {"pos": [0.5, 64.0, 0.5], "dimension": "overworld", "in_water": False}
                out1 = dig.dig_block(bb, (1, 64, 0), collect=True)
                self.assertEqual(out1.status, "OK")
                self.assertTrue(sim.write_calls[-1][1]["collect"])

                # Simulate an 8s upstream collectDrops stall on dig #2 via monotonic clock delta
                sim.set_block(1, 64, 1, "iron_ore")
                ticks = itertools.chain([100.0, 100.0, 106.5, 106.5], itertools.repeat(107.0))
                with patch("v2.primitives.dig_collect.time.monotonic", side_effect=lambda: next(ticks)):
                    bb.begin_tick()
                    out2 = dig.dig_block(bb, (1, 64, 1), collect=True)
                    self.assertEqual(out2.status, "OK")
                    self.assertTrue(sim.write_calls[-1][1]["collect"])

                    # Dig #3 within skip window MUST flip upstream collect=False!
                    bb.begin_tick()
                    out3 = dig.dig_block(bb, (2, 64, 0), collect=True)
                    self.assertEqual(out3.status, "OK")
                    self.assertFalse(sim.write_calls[-1][1]["collect"])
            finally:
                client.close()
                db.close()

    def test_return_to_surface_refuses_bare_hand_stone_dig(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 40.0, 0.5))
        sim.fill(-3, 38, -3, 3, 45, 3, "stone")
        sim.set_block(0, 40, 0, "air")
        sim.set_block(0, 41, 0, "air")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "NoPickSurfaceBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "NoPickSurfaceBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                task = ReturnToSurfaceTask(ctx)
                rt.mount_goal(task)
                # After 2 bare-hand stone digs reporting 'no proper tool', task must escalate cleanly to NEEDS_AI
                orig_dig = client.dig
                def _warn_dig(pos, collect=True):
                    res = orig_dig(pos, collect=collect)
                    res.setdefault("result", {})["warning"] = "no proper tool: block drops nothing"
                    return res
                with patch.object(client, "dig", side_effect=_warn_dig):
                    rt.tick_once()
                    rt.tick_once()
                    rt.tick_once()
                self.assertEqual(rt.bb.goal_status, "NEEDS_AI")
                self.assertEqual(rt.bb.last_failure_reason, "surface_escape:no_pickaxe")
                self.assertEqual(len([w for w in sim.write_calls if w[0] == "dig"]), 2)
            finally:
                client.close()
                db.close()

    def test_smelt_item_skips_window_view_until_raw_materials_ready(self) -> None:
        # Furnace is 10m away (> 5m), raw_iron < missing (only 1 ore in world) -> 0 window/view before mining fails/completes!
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-4, 63, -4, 14, 63, 4, "stone")
        sim.set_block(10, 64, 0, "furnace")
        sim.give("coal", 1)
        sim.give("stone_pickaxe", 1)
        sim.set_block(2, 64, 0, "iron_ore")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "GateSmeltBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "GateSmeltBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.mount_goal(SmeltItemTask(ctx, TaskCatalogue(), "iron_ingot", 3, poll_interval_seconds=0.0))

                for _ in range(8):
                    rt.tick_once()
                    if sim.inventory_counts.get("raw_iron", 0) >= 1:
                        break

                view_calls = [ep for ep, _ in sim.read_calls if ep == "window/view"]
                self.assertEqual(view_calls, [])
                self.assertEqual(sim.inventory_counts.get("raw_iron", 0), 1)
            finally:
                client.close()
                db.close()

        # Once raw_iron >= missing (3) and fuel ready, even with < 6 ore it smelts immediately without going back underground!
        sim_ready = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim_ready.fill(-4, 63, -4, 14, 63, 4, "stone")
        sim_ready.set_block(10, 64, 0, "furnace")
        sim_ready.smelt_ticks_required = 1
        sim_ready.give("raw_iron", 3)
        sim_ready.give("coal", 1)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim_ready) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "ReadySmeltBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "ReadySmeltBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.mount_goal(SmeltItemTask(ctx, TaskCatalogue(), "iron_ingot", 3, poll_interval_seconds=0.0))

                for _ in range(15):
                    rt.tick_once()
                    if rt.bb.goal_status == "COMPLETED":
                        break

                self.assertEqual(rt.bb.goal_status, "COMPLETED")
                self.assertEqual(sim_ready.inventory_counts.get("iron_ingot", 0), 3)
                self.assertEqual([ep for ep, _ in sim_ready.write_calls if ep == "dig"], [])
            finally:
                client.close()
                db.close()

    def test_mine_resource_opportunistically_harvests_diamond_gold_and_coal(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 16.0, 0.5))
        sim.fill(-3, 14, -3, 3, 19, 3, "stone")
        sim.set_block(0, 16, 0, "air")
        sim.set_block(0, 17, 0, "air")
        sim.set_block(2, 16, 0, "deepslate_diamond_ore")
        sim.give("iron_pickaxe", 1)
        sim.give("coal", 4)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "OppMineBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "OppMineBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.mount_goal(MineResourceTask(ctx, TaskCatalogue(), "raw_iron", 4, target_y=16))

                for _ in range(4):
                    rt.tick_once()
                    if sim.inventory_counts.get("diamond", 0) >= 1:
                        break

                self.assertEqual(sim.inventory_counts.get("diamond", 0), 1)
            finally:
                client.close()
                db.close()

    def test_horizon_selector_mid_late_armor_gold_diamond_and_equipped_skip(self) -> None:
        hs = HorizonSelector(TaskCatalogue(), objective="daily")
        bb = Blackboard()
        bb.status = {"dimension": "overworld", "pos": [0.5, 16.0, 0.5], "time_of_day": 1000, "food": 20}
        bb.inventory_counts = {"iron_pickaxe": 1, "water_bucket": 1, "flint_and_steel": 1, "shield": 1, "stone_sword": 1, "gold_ingot": 4}
        items = [c.item for c in hs.select_top(bb, limit=8)]
        self.assertIn("iron_chestplate", items)
        self.assertIn("golden_boots", items)
        self.assertIn("diamond", items)

        # Already equipped iron_chestplate should be skipped and legs/helmet/boots unlocked
        bb.inventory = {"armor": {"chest": {"name": "iron_chestplate"}}}
        items_after = [c.item for c in hs.select_top(bb, limit=8)]
        self.assertNotIn("iron_chestplate", items_after)
        self.assertTrue(any(p in items_after for p in ("iron_helmet", "iron_leggings", "iron_boots")))


if __name__ == "__main__":
    unittest.main()
