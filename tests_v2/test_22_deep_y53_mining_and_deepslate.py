"""Tests for deep Y=-53 mining readiness, Y=-54 lava safety, opportunistic deep ores, and cobbled_deepslate portal casting."""

from __future__ import annotations

import tempfile
import unittest

from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt import BTRuntime
from v2.bt.blackboard import Blackboard
from v2.gateway import VirtualWorldGateway
from v2.kernel import MinecraftAgentClient, PendingTransactionJournal, UnifiedWorldDB
from v2.primitives import PrimitiveContext
from v2.tasks import EnterNetherTask, MineResourceTask, TaskCatalogue
from v2.tasks.mining import deep_mining_ready


class TestDeepY53MiningAndDeepslate(unittest.TestCase):
    def test_deep_mining_ready_threshold_gates(self) -> None:
        bb = Blackboard()
        bb.status = {"pos": [0.5, 64.0, 0.5], "dimension": "overworld", "health": 14.0}
        bb.inventory_counts = {
            "iron_pickaxe": 1,
            "water_bucket": 1,
            "cobblestone": 6,
            "cobbled_deepslate": 10,
            "bread": 4,
        }
        self.assertTrue(deep_mining_ready(bb))

        # Diamond/netherite pickaxe + cooked meat + cobbled_deepslate alone -> True
        bb.inventory_counts = {
            "diamond_pickaxe": 1,
            "water_bucket": 1,
            "cobbled_deepslate": 16,
            "cooked_beef": 4,
        }
        self.assertTrue(deep_mining_ready(bb))

        # Missing iron+ pickaxe (only stone_pickaxe) -> False
        bb.inventory_counts = {
            "stone_pickaxe": 1,
            "water_bucket": 1,
            "cobbled_deepslate": 16,
            "bread": 4,
        }
        self.assertFalse(deep_mining_ready(bb))

        # Missing water_bucket -> False
        bb.inventory_counts = {
            "iron_pickaxe": 1,
            "bucket": 1,
            "cobbled_deepslate": 16,
            "bread": 4,
        }
        self.assertFalse(deep_mining_ready(bb))

        # Insufficient cobblestone + cobbled_deepslate (< 16) -> False
        bb.inventory_counts = {
            "iron_pickaxe": 1,
            "water_bucket": 1,
            "cobblestone": 7,
            "cobbled_deepslate": 8,
            "bread": 4,
        }
        self.assertFalse(deep_mining_ready(bb))

        # Insufficient edible food (rotten_flesh excluded) -> False
        bb.inventory_counts = {
            "iron_pickaxe": 1,
            "water_bucket": 1,
            "cobbled_deepslate": 16,
            "bread": 3,
            "rotten_flesh": 12,
        }
        self.assertFalse(deep_mining_ready(bb))

        # Low HP (< 14.0) -> False
        bb.inventory_counts = {
            "iron_pickaxe": 1,
            "water_bucket": 1,
            "cobbled_deepslate": 16,
            "bread": 4,
        }
        bb.status = {"pos": [0.5, 64.0, 0.5], "dimension": "overworld", "health": 13.5}
        self.assertFalse(deep_mining_ready(bb))

    def test_mine_resource_diamond_stops_at_y53_avoids_y54_lava_and_uses_deepslate_collect_cap(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, -51.0, 0.5))
        # Bedrock base at y=-55, lava ocean at y=-54 outside immediate descent column, deepslate at y=-53..-48
        sim.fill(-4, -55, -4, 8, -55, 4, "bedrock")
        sim.fill(-4, -54, -4, 8, -54, 4, "lava", {"level": "0"})
        sim.fill(-2, -54, -2, 6, -54, 2, "deepslate")
        sim.fill(-2, -53, -2, 6, -48, 2, "deepslate")
        sim.set_block(0, -51, 0, "air")
        sim.set_block(0, -50, 0, "air")
        # Diamond vein at y=-53 along +X tunnel
        sim.set_block(4, -53, 0, "deepslate_diamond_ore")
        sim.give("iron_pickaxe", 1)
        sim.give("cobbled_deepslate", 8)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "DeepDiamondBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "DeepDiamondBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()

                task = MineResourceTask(ctx, cat, "diamond", 1)
                self.assertEqual(task.target_y, -53)
                self.assertEqual(MineResourceTask(ctx, cat, "redstone", 1).target_y, -53)
                self.assertEqual(MineResourceTask(ctx, cat, "diamond", 1, target_y=-54).target_y, -54)

                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.mount_goal(task)

                min_y = sim.pos[1]
                for _ in range(40):
                    rt.tick_once()
                    self.assertLessEqual(rt.bb.writes_this_tick, 1)
                    min_y = min(min_y, sim.pos[1])
                    if rt.bb.goal_status == "COMPLETED":
                        break

                self.assertEqual(rt.bb.goal_status, "COMPLETED")
                self.assertGreaterEqual(sim.inventory_counts.get("diamond", 0), 1)
                self.assertGreaterEqual(min_y, -53.0)
                self.assertEqual(sim.pos[1], -53.0)
                # Clearing deepslate while holding cobbled_deepslate >= 8 used collect=False
                deepslate_digs = [
                    w for w in sim.write_calls
                    if w[0] == "dig" and (w[1]["x"], w[1]["y"], w[1]["z"]) != (4, -53, 0)
                ]
                self.assertTrue(deepslate_digs)
                self.assertTrue(all(w[1]["collect"] is False for w in deepslate_digs))
            finally:
                client.close()
                db.close()

    def test_enter_nether_deep_descent_falls_through_on_floor_and_uses_cobbled_deepslate(self) -> None:
        # 1) When all 4 stair directions hit lava/void at fy=-52, descent falls through to horizontal tunnel (collect=False with cobbled_deepslate>=16)
        sim_fall = VirtualMcApiServer(spawn_pos=(0.5, -52.0, 0.5))
        sim_fall.fill(-4, -53, -4, 4, -50, 4, "deepslate")
        sim_fall.set_block(0, -52, 0, "air")
        sim_fall.set_block(0, -51, 0, "air")
        # Put lava at y=-54 in all 4 stair floor_after cells, while y=-53 has solid deepslate floor!
        for dx, dz in ((1, 0), (0, 1), (-1, 0), (0, -1)):
            sim_fall.set_block(dx, -54, dz, "lava", {"level": "0"})
        sim_fall.give("iron_pickaxe", 1)
        sim_fall.give("water_bucket", 1)
        sim_fall.give("cobbled_deepslate", 16)
        sim_fall.give("cooked_beef", 4)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim_fall) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "FloorFallBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "FloorFallBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                task = EnterNetherTask(PrimitiveContext(gw, journal), TaskCatalogue())
                bb = Blackboard()
                bb.begin_tick()
                bb.status = client.get_status()["result"]
                bb.inventory_counts = dict(sim_fall.inventory_counts)

                self.assertEqual(task._effective_lava_target_y(bb), -53)
                self.assertEqual(EnterNetherTask(PrimitiveContext(gw, journal), TaskCatalogue(), lava_target_y=11)._effective_lava_target_y(bb), 11)
                res = task._step_underground_lava_descent(bb)
                self.assertEqual(res, "RUNNING")
                digs = [w for w in sim_fall.write_calls if w[0] == "dig"]
                self.assertEqual(len(digs), 1)
                self.assertFalse(digs[0][1]["collect"])
            finally:
                client.close()
                db.close()

        # 2) Full deep-kit descent from Y=20 past Y=11 down to Y=-53, mining adjacent diamond/gold ores and casting with cobbled_deepslate
        sim = VirtualMcApiServer(spawn_pos=(0.5, 20.0, 0.5), loaded_radius=150)
        # Solid deepslate staircase shaft from (0,20,0) down to (73,-53,0), then open cavern to lava pool at x=136..139, y=-54
        # At (72,-52,0) dist to (136,-54,0) is 64.03m (>64m); at (73,-53,0) dist is 63.01m (<=64m)!
        sim.fill(-3, -55, -3, 76, 24, 3, "deepslate")
        sim.set_block(0, 20, 0, "air")
        sim.set_block(0, 21, 0, "air")
        sim.fill(74, -55, -5, 144, -47, 5, "deepslate")
        sim.fill(74, -53, -4, 142, -48, 4, "air")
        sim.fill(136, -54, -1, 139, -54, 1, "lava", {"level": "0"})
        # Adjacent exposed deep ores along the shaft wall (within 4m during descent below y=48)
        sim.set_block(60, -40, 1, "deepslate_diamond_ore")
        sim.set_block(70, -50, 1, "deepslate_gold_ore")
        sim.give("iron_pickaxe", 1)
        sim.give("water_bucket", 1)
        sim.give("flint_and_steel", 1)
        sim.give("cobbled_deepslate", 32)
        sim.give("cooked_beef", 4)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "DeepNetherBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "DeepNetherBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                task = EnterNetherTask(ctx, cat)
                rt.mount_goal(task)

                saw_acquire_cobble = False
                min_y = sim.pos[1]
                for _ in range(520):
                    rt.tick_once()
                    self.assertLessEqual(rt.bb.writes_this_tick, 1)
                    min_y = min(min_y, sim.pos[1])
                    if task._active_child is not None and task._active_child.name.startswith("acquire:cobblestone"):
                        saw_acquire_cobble = True
                    if sim.dimension == "the_nether" or rt.bb.goal_status == "COMPLETED":
                        break

                self.assertFalse(saw_acquire_cobble, "Must use cobbled_deepslate without acquiring cobblestone")
                self.assertLessEqual(min_y, -53.0)
                self.assertGreaterEqual(sim.inventory_counts.get("diamond", 0), 1)
                self.assertEqual(sim.block_name(70, -50, 1), "air")
                placed_items = [w[1]["item"] for w in sim.write_calls if w[0] == "place"]
                self.assertIn("cobbled_deepslate", placed_items)
                self.assertEqual(sim.dimension, "the_nether")
            finally:
                client.close()
                db.close()


if __name__ == "__main__":
    unittest.main()
