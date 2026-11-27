"""Tests for two-tier close-approach mining, stranded drop pickup, vein queue, collect=False rock-clearing, water plug, and shaft/surface memory."""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt import BTRuntime
from v2.bt.blackboard import Blackboard
from v2.gateway import VirtualWorldGateway
from v2.kernel import MinecraftAgentClient, PendingTransactionJournal, UnifiedWorldDB
from v2.primitives import ActionOutcome, DigCollectPrimitive, PrimitiveContext
from v2.tasks import AcquireItemTask, MineResourceTask, ReturnToSurfaceTask, TaskCatalogue


def _sync_bb(bb: Blackboard, sim: VirtualMcApiServer) -> None:
    bb.begin_tick()
    bb.status = {
        "pos": list(sim.pos),
        "dimension": sim.dimension,
        "alive": sim.alive,
        "health": sim.health,
        "food": sim.food,
        "oxygen": sim.oxygen,
        "on_ground": True,
        "in_water": False,
        "submerged": False,
        "in_lava": False,
    }
    bb.inventory_counts = dict(sim.inventory_counts)
    bb.entities = list(sim.entities)


class TestMiningMemoryPlugAndDrops(unittest.TestCase):
    def test_two_tier_close_approach_and_through_wall_fallback(self) -> None:
        # 1) Open path within 2.6 < eye_dist <= 4.2 -> close-approaches first, then digs!
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-4, 63, -4, 6, 63, 4, "stone")
        sim.set_block(3, 64, 0, "oak_log")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "CloseBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "CloseBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                dig = DigCollectPrimitive(ctx)
                bb = Blackboard()

                _sync_bb(bb, sim)
                out1 = dig.dig_block(bb, (3, 64, 0), collect=True)
                self.assertEqual(out1.status, "PROGRESSED")
                self.assertEqual(out1.action, "navigate")
                self.assertLessEqual(bb.writes_this_tick, 1)
                self.assertEqual(sim.block_name(3, 64, 0), "oak_log")

                _sync_bb(bb, sim)
                out2 = dig.dig_block(bb, (3, 64, 0), collect=True)
                self.assertEqual(out2.status, "OK")
                self.assertEqual(out2.action, "dig")
                self.assertLessEqual(bb.writes_this_tick, 1)
                self.assertEqual(sim.block_name(3, 64, 0), "air")
            finally:
                client.close()
                db.close()

        # 2) Sealed wall between bot and target at eye_dist ~ 3.2 -> approach fails WITHOUT blacklisting "dig", then digs directly!
        sim2 = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim2.fill(-3, 62, -3, 5, 66, 3, "stone")
        sim2.fill(0, 64, 0, 0, 65, 0, "air")
        sim2.set_block(3, 64, 0, "iron_ore")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim2) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "WallDigBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "WallDigBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                dig = DigCollectPrimitive(ctx)
                bb = Blackboard()

                for _ in range(5):
                    _sync_bb(bb, sim2)
                    res = dig.dig_block(bb, (3, 64, 0), collect=True)
                    self.assertLessEqual(bb.writes_this_tick, 1)
                    self.assertFalse(gw.unreachable.is_blocked("overworld", (3, 64, 0), "dig"))
                    if res.status == "OK":
                        break
                self.assertEqual(sim2.block_name(3, 64, 0), "air")
                self.assertEqual(sim2.inventory_counts.get("raw_iron", 0), 1)
            finally:
                client.close()
                db.close()

    def test_post_dig_stranded_drop_pickup_wall_clear_and_single_write_invariant(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-2, 63, -2, 5, 63, 2, "stone")
        # Thin 1-block wall at x=1, y=64..65 and ore at x=3, y=64
        sim.set_block(1, 64, 0, "stone")
        sim.set_block(1, 65, 0, "stone")
        sim.set_block(3, 64, 0, "iron_ore")
        sim.give("iron_pickaxe", 1)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "DropBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "DropBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                dig = DigCollectPrimitive(ctx)
                bb = Blackboard()

                # Mock failed close-approach so dig happens immediately through thin wall, then simulate stranded drop at (3.5, 64.0, 0.5)
                _sync_bb(bb, sim)
                with patch.object(dig._nav, "step_to", return_value=ActionOutcome("FAILED_LOCAL", "navigate", reason="no_3d_path")):
                    out = dig.dig_block(bb, (3, 64, 0), collect=True)
                self.assertEqual(out.status, "OK")
                self.assertEqual(bb.writes_this_tick, 1)
                self.assertEqual(sim.block_name(3, 64, 0), "air")

                # Next tick: server reports stranded item entity at x=3.5 (dist=3.0m > 1.5m) behind wall x=1
                sim.add_entity(501, "item", (3.5, 64.0, 0.5), kind="object")
                _sync_bb(bb, sim)
                out_wall = dig.dig_nearest_of_types(bb, ("iron_ore",), radius=16, collect=True)
                self.assertEqual(out_wall.status, "PROGRESSED")
                self.assertEqual(out_wall.reason, "drop_wall_cleared")
                self.assertEqual(bb.writes_this_tick, 1)
                self.assertEqual(sim.block_name(1, 64, 0), "air")

                # Next tick: wall opening allows step_to toward the stranded drop
                _sync_bb(bb, sim)
                out_step = dig.dig_nearest_of_types(bb, ("iron_ore",), radius=16, collect=True)
                self.assertIn(out_step.status, {"OK", "PROGRESSED"})
                self.assertEqual(out_step.action, "navigate")
                self.assertLessEqual(bb.writes_this_tick, 1)
                self.assertGreater(sim.pos[0], 1.5)
            finally:
                client.close()
                db.close()

    def test_vein_queue_cuts_scan_blocks_and_clears_on_type_or_move(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-4, 63, -4, 16, 63, 4, "stone")
        # 3 adjacent oak logs in a vein/trunk within 2.5m
        sim.set_block(2, 64, 0, "oak_log")
        sim.set_block(2, 65, 0, "oak_log")
        sim.set_block(2, 66, 0, "oak_log")
        # Another block type + distant log > 8m away
        sim.set_block(3, 64, 1, "iron_ore")
        sim.set_block(13, 64, 0, "oak_log")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "VeinBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "VeinBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                dig = DigCollectPrimitive(ctx)
                bb = Blackboard()

                # Dig all 3 logs in the vein; scan_blocks must fire ONLY ONCE!
                for _ in range(6):
                    _sync_bb(bb, sim)
                    out = dig.dig_nearest_of_types(bb, ("oak_log",), radius=16, collect=True)
                    self.assertLessEqual(bb.writes_this_tick, 1)
                    if all(sim.block_name(2, y, 0) == "air" for y in (64, 65, 66)):
                        break
                    self.assertIn(out.status, {"OK", "PROGRESSED"})

                find_calls = [args for ep, args in sim.read_calls if ep == "find_blocks"]
                self.assertEqual(len(find_calls), 1)
                self.assertTrue(all(sim.block_name(2, y, 0) == "air" for y in (64, 65, 66)))

                # Put an extra log into vein queue, then change block_types -> clears queue & triggers fresh scan
                dig._vein_queue = [(13, 64, 0)]
                dig._vein_types = ("oak_log",)
                dig._vein_origin = ("overworld", tuple(sim.pos))
                _sync_bb(bb, sim)
                dig.dig_nearest_of_types(bb, ("iron_ore",), radius=16, collect=True)
                self.assertEqual(len([1 for ep, _ in sim.read_calls if ep == "find_blocks"]), 2)

                # Populate queue at origin (0.5, 64, 0.5) then move > 8.0m -> clears queue & triggers fresh scan
                dig._vein_queue = [(2, 64, 0)]
                dig._vein_types = ("oak_log",)
                dig._vein_origin = ("overworld", (0.5, 64.0, 0.5))
                sim.pos = [11.0, 64.0, 0.5]
                _sync_bb(bb, sim)
                dig.dig_nearest_of_types(bb, ("oak_log",), radius=16, collect=True)
                self.assertEqual(len([1 for ep, _ in sim.read_calls if ep == "find_blocks"]), 3)
            finally:
                client.close()
                db.close()

    def test_mining_collect_false_rock_clearing_and_water_plug_with_fallback(self) -> None:
        # 1) cobble >= 16 -> stone clearing uses collect=False, while target ore keeps collect=True!
        sim = VirtualMcApiServer(spawn_pos=(0.5, 18.0, 0.5))
        sim.fill(-2, 14, -2, 6, 22, 2, "stone")
        sim.set_block(0, 18, 0, "air")
        sim.set_block(0, 19, 0, "air")
        sim.set_block(1, 17, 0, "iron_ore")
        sim.give("iron_pickaxe", 1)
        sim.give("cobblestone", 20)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "FastRockBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "FastRockBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.mount_goal(MineResourceTask(ctx, cat, "raw_iron", 1, target_y=16))

                for _ in range(5):
                    rt.tick_once()
                    self.assertLessEqual(rt.bb.writes_this_tick, 1)

                dig_calls = [pl for ep, pl in sim.write_calls if ep == "dig"]
                stone_digs = [p for p in dig_calls if (p["x"], p["y"], p["z"]) != (1, 17, 0)]
                ore_digs = [p for p in dig_calls if (p["x"], p["y"], p["z"]) == (1, 17, 0)]
                self.assertTrue(stone_digs and all(p["collect"] is False for p in stone_digs), dig_calls)
                self.assertTrue(ore_digs and all(p["collect"] is True for p in ore_digs), dig_calls)
            finally:
                client.close()
                db.close()

        # 2) Fluid risk split + fast water plug -> conservative fallback
        sim2 = VirtualMcApiServer(spawn_pos=(0.5, 16.0, 0.5))
        sim2.fill(-3, 14, -3, 5, 20, 3, "stone")
        sim2.set_block(0, 16, 0, "air")
        sim2.set_block(0, 17, 0, "air")
        # Front tunnel head cell at +X is a 1-block water pocket; +Z has lava
        sim2.set_block(1, 17, 0, "water", {"level": "0"})
        sim2.set_block(0, 17, 1, "lava", {"level": "0"})
        # Safety split probe cells (hidden/outside exposed ore scan until opened, or checked directly)
        sim2.set_block(4, 16, 0, "iron_ore")
        sim2.set_block(4, 16, 1, "water", {"level": "0"})
        sim2.set_block(4, 17, 1, "air")
        sim2.set_block(4, 16, -2, "iron_ore")
        sim2.set_block(4, 17, -2, "water", {"level": "0"})
        sim2.give("iron_pickaxe", 1)
        sim2.give("cobblestone", 24)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim2) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "PlugBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "PlugBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                task = MineResourceTask(ctx, cat, "raw_iron", 1, target_y=16)
                bb = Blackboard()
                _sync_bb(bb, sim2)

                # Fluid risk split checks: shallow floor water allowed for ore, overhead water & lava hard-blocked
                self.assertTrue(task._is_safe_to_dig(bb, (4, 16, 0), allow_shallow_floor_water=True))
                self.assertFalse(task._is_safe_to_dig(bb, (4, 16, -2), allow_shallow_floor_water=True))
                self.assertFalse(task._is_safe_to_dig(bb, (0, 16, 1), allow_shallow_floor_water=True))

                # Hide (4, 16, 0) behind stone while testing front tunnel water-plugging at (1, 17, 0);
                # note (4, 16, -2) remains exposed under overhead water and MUST be skipped by Step 2!
                sim2.set_block(4, 16, 0, "stone")
                _sync_bb(bb, sim2)
                res = task.on_tick(bb)
                self.assertEqual(res, "RUNNING")
                self.assertEqual(sim2.block_name(1, 17, 0), "cobblestone")

                # If persistent water remains behind the plug (e.g. (2, 17, 0) is water), it falls back to rotating direction!
                sim2.set_block(2, 17, 0, "water", {"level": "0"})
                gw.invalidate_unknown_radius("overworld", (1, 17, 0), radius=1)
                _sync_bb(bb, sim2)
                prev_dir = task._dir_idx
                task.on_tick(bb)
                self.assertNotEqual(task._dir_idx, prev_dir)
            finally:
                client.close()
                db.close()

    def test_mine_shaft_and_surface_entry_memory_fast_return_and_staircase_fallback(self) -> None:
        # 1) MineResourceTask saves surface_entry above y=58, mine_shaft below y=58, and reuses mine_shaft when above y>=58
        sim = VirtualMcApiServer(spawn_pos=(0.5, 62.0, 0.5))
        sim.fill(-4, 54, -4, 12, 61, 4, "stone")
        sim.give("iron_pickaxe", 1)
        sim.give("cobblestone", 20)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "MemBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "MemBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                task = MineResourceTask(ctx, cat, "raw_iron", 1, target_y=55)
                rt.mount_goal(task)

                for _ in range(30):
                    rt.tick_once()
                    if sim.pos[1] < 57.0:
                        break

                self.assertIsNotNone(rt.bb.get("last_surface_anchor"))
                self.assertIsNotNone(rt.bb.get("last_mine_anchor"))
                surf_lms = db.list_landmarks("overworld", kind="surface_entry")
                mine_lms = db.list_landmarks("overworld", kind="mine_shaft")
                self.assertTrue(surf_lms)
                self.assertTrue(mine_lms)

                # Stop is pure-memory (zero HTTP calls)
                reads_before = len(sim.read_calls)
                writes_before = len(sim.write_calls)
                task.stop(rt.bb)
                self.assertEqual(len(sim.read_calls), reads_before)
                self.assertEqual(len(sim.write_calls), writes_before)

                # ReturnToSurfaceTask navigates back along remembered surface_entry FIRST!
                ret = ReturnToSurfaceTask(ctx, target_y=62)
                rt.mount_goal(ret)
                ret_writes_before = len(sim.write_calls)
                for _ in range(18):
                    rt.tick_once()
                    self.assertLessEqual(rt.bb.writes_this_tick, 1)
                    if ret.is_finished(rt.bb):
                        break
                self.assertTrue(ret.is_finished(rt.bb))
                ret_writes = sim.write_calls[ret_writes_before:]
                self.assertTrue(any(ep == "move_to" for ep, _ in ret_writes), ret_writes)
                self.assertFalse(any(ep == "dig" for ep, _ in ret_writes), ret_writes)

                # Re-running MineResourceTask above y>=58 with remembered mine_shaft within 48m navigates toward it first!
                sim.pos = [0.5, 62.0, 0.5]
                task2 = MineResourceTask(ctx, cat, "raw_iron", 1, target_y=55)
                rt.mount_goal(task2)
                writes_before2 = len(sim.write_calls)
                rt.tick_once()
                new_writes = sim.write_calls[writes_before2:]
                self.assertTrue(any(ep == "move_to" for ep, _ in new_writes), new_writes)
            finally:
                client.close()
                db.close()

        # 2) ReturnToSurfaceTask bounded 6-tick fast-path fallback to 3-high staircase (+ water plug)
        sim2 = VirtualMcApiServer(spawn_pos=(0.5, 50.0, 0.5))
        sim2.fill(-3, 49, -3, 18, 63, 3, "stone")
        sim2.set_block(0, 50, 0, "air")
        sim2.set_block(0, 51, 0, "air")
        sim2.set_block(1, 52, 0, "water", {"level": "0"})
        sim2.give("cobblestone", 16)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim2) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "FallbackSurfBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "FallbackSurfBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                bb = Blackboard()
                # Remembered surface_entry is completely walled off 15m away -> must exhaust within 6 ticks and fall back to staircase!
                db.save_landmark("overworld", "surface_entry", "latest", (15, 62, 0))
                ret = ReturnToSurfaceTask(ctx, target_y=62)

                for _ in range(10):
                    _sync_bb(bb, sim2)
                    ret.on_tick(bb)
                    self.assertLessEqual(bb.writes_this_tick, 1)
                    if ret._surface_route_exhausted:
                        break
                self.assertTrue(ret._surface_route_exhausted)

                # Staircase fallback plugs water at (1, 52, 0) first, then climbs upward!
                for _ in range(4):
                    _sync_bb(bb, sim2)
                    ret.on_tick(bb)
                    self.assertLessEqual(bb.writes_this_tick, 1)
                self.assertEqual(sim2.block_name(1, 52, 0), "air")
            finally:
                client.close()
                db.close()

    def test_acquire_item_task_uses_vein_queue_and_pure_memory_stop(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-4, 63, -4, 4, 63, 4, "grass_block")
        for y in range(64, 67):
            sim.set_block(2, y, 0, "oak_log")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "AcqBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "AcqBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                task = AcquireItemTask(ctx, cat, "oak_log", 3)
                rt.mount_goal(task)

                for _ in range(10):
                    rt.tick_once()
                    self.assertLessEqual(rt.bb.writes_this_tick, 1)
                    if sim.inventory_counts.get("oak_log", 0) >= 3:
                        break

                self.assertGreaterEqual(sim.inventory_counts.get("oak_log", 0), 3)
                find_calls = [1 for ep, _ in sim.read_calls if ep == "find_blocks"]
                self.assertEqual(len(find_calls), 1)

                reads_before = len(sim.read_calls)
                task.stop(rt.bb)
                self.assertEqual(len(sim.read_calls), reads_before)
            finally:
                client.close()
                db.close()


if __name__ == "__main__":
    unittest.main()
