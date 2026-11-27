"""R4 tests for portable crafting_table/furnace pickup, furnace landmark reuse, batch iron smelting, iron order lock, corpse pin, daytime hunt, and compact IntentCompiler schemas."""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt import BTRuntime, Blackboard
from v2.gateway import VirtualWorldGateway
from v2.kernel import MinecraftAgentClient, PendingTransactionJournal, UnifiedWorldDB
from v2.planner import HorizonSelector, IntentCompiler
from v2.primitives import PrimitiveContext
from v2.tasks import AcquireItemTask, SmeltItemTask, TaskCatalogue
from v2.tasks.competition import CompetitionManager


class TestPortableStationsAndIronOrder(unittest.TestCase):
    def test_intent_compiler_schemas_under_1200_bytes_and_lazy_compiles_new_goals(self) -> None:
        cat = TaskCatalogue()
        ctx = MagicMock()

        daily_c = IntentCompiler(ctx, cat, objective="daily")
        for needs_ai in (False, True):
            tools = daily_c.tool_schemas(needs_ai=needs_ai)
            payload_bytes = len(json.dumps(tools, ensure_ascii=False).encode("utf-8"))
            self.assertLessEqual(payload_bytes, 1200, f"needs_ai={needs_ai} exceeded 1200B: {payload_bytes}")
            run_goal = next(t for t in tools if t["function"]["name"] == "run_goal")
            enum = run_goal["function"]["parameters"]["properties"]["goal"]["enum"]
            self.assertIn("recover_corpse", enum)
            self.assertIn("hunt_food", enum)

        comp_c = IntentCompiler(ctx, cat, objective="competition", competition=CompetitionManager(cat))
        comp_bytes = len(json.dumps(comp_c.tool_schemas(), ensure_ascii=False).encode("utf-8"))
        self.assertLessEqual(comp_bytes, 1200)

        # Lazy import compilation check using temporary stub modules (corpse/food live on parallel branch)
        corpse_mod = types.ModuleType("v2.tasks.corpse")
        food_mod = types.ModuleType("v2.tasks.food")

        class _StubRecoverDeathDropTask:
            def __init__(self, c, cat_arg) -> None:
                self.ctx = c
                self.catalogue = cat_arg

        class _StubHuntFoodTask:
            def __init__(self, c) -> None:
                self.ctx = c

        corpse_mod.RecoverDeathDropTask = _StubRecoverDeathDropTask  # type: ignore[attr-defined]
        food_mod.HuntFoodTask = _StubHuntFoodTask  # type: ignore[attr-defined]

        bb = Blackboard()
        with patch.dict(sys.modules, {"v2.tasks.corpse": corpse_mod, "v2.tasks.food": food_mod}):
            t_corpse, r_corpse = daily_c.compile_tool_call("run_goal", {"goal": "recover_corpse"}, bb)
            self.assertTrue(r_corpse["ok"])
            self.assertEqual(r_corpse["mounted"], "recover_corpse")
            self.assertIsInstance(t_corpse, _StubRecoverDeathDropTask)
            self.assertIs(t_corpse.ctx, ctx)
            self.assertIs(t_corpse.catalogue, cat)

            t_food, r_food = daily_c.compile_tool_call("run_goal", {"goal": "hunt_food"}, bb)
            self.assertTrue(r_food["ok"])
            self.assertEqual(r_food["mounted"], "hunt_food")
            self.assertIsInstance(t_food, _StubHuntFoodTask)
            self.assertIs(t_food.ctx, ctx)

    def test_horizon_selector_corpse_pin_daytime_hunt_early_sword_and_iron_order(self) -> None:
        cat = TaskCatalogue()
        hs = HorizonSelector(cat, objective="daily")

        # 1) Active corpse drop pins CORPSE_RECOVERY #1 even ahead of PORTAL_GATE_READY (survival & competition)
        bb = Blackboard()
        bb.status = {"dimension": "overworld", "pos": [0.5, 64.0, 0.5], "time_of_day": 2000, "food": 20}
        bb.inventory_counts = {"iron_pickaxe": 1, "oak_planks": 8}
        bb.set("active_corpse_drop", {"id": "c1", "dim": "overworld", "pos": [12.0, 64.0, -4.0], "loaded_elapsed": 15.0}, writer="test")

        top_surv = hs.select_top(bb, limit=5)
        self.assertEqual(top_surv[0].goal, "recover_corpse")
        self.assertEqual(top_surv[0].priority_tag, "CORPSE_RECOVERY")
        self.assertEqual(top_surv[1].priority_tag, "PORTAL_GATE_READY")

        comp = CompetitionManager(cat)
        hs_comp = HorizonSelector(cat, objective="competition", competition=comp)
        top_comp = hs_comp.select_top(bb, limit=5)
        self.assertEqual(top_comp[0].goal, "recover_corpse")
        self.assertEqual(top_comp[0].priority_tag, "CORPSE_RECOVERY")

        # Expired corpse (>= 300s) does NOT pin
        bb.set("active_corpse_drop", {"id": "c1", "dim": "overworld", "pos": [12.0, 64.0, -4.0], "loaded_elapsed": 300.0}, writer="test")
        top_expired = hs.select_top(bb, limit=5)
        self.assertEqual(top_expired[0].priority_tag, "PORTAL_GATE_READY")

        # 2) Daytime animal food hunt emitted when food < 4 and passive animal within 16m (overworld daytime only)
        bb_hunt = Blackboard()
        bb_hunt.status = {"dimension": "overworld", "pos": [0.5, 64.0, 0.5], "time_of_day": 3000, "food": 12}
        bb_hunt.inventory_counts = {"stone_pickaxe": 1, "stone_sword": 1}
        bb_hunt.entities = [{"id": 77, "name": "cow", "type": "passive", "pos": [6.5, 64.0, 0.5]}]
        top_hunt = hs.select_top(bb_hunt, limit=5)
        self.assertEqual(top_hunt[0].goal, "hunt_food")
        self.assertEqual(top_hunt[0].priority_tag, "FOOD_HUNT")
        self.assertEqual(top_hunt[0].est_steps, 4)

        # Nighttime suppresses FOOD_HUNT
        bb_hunt.status["time_of_day"] = 15000
        self.assertNotIn("hunt_food", [c.goal for c in hs.select_top(bb_hunt, limit=5)])

        # 3) Early stone_sword right after stone_pickaxe
        bb_early = Blackboard()
        bb_early.status = {"dimension": "overworld", "pos": [0.5, 64.0, 0.5], "time_of_day": 1000, "food": 20}
        bb_early.inventory_counts = {"wooden_pickaxe": 1}
        early_items = [c.item for c in hs.select_top(bb_early, limit=5)]
        self.assertIn("stone_pickaxe", early_items)
        self.assertIn("stone_sword", early_items)
        self.assertLess(early_items.index("stone_pickaxe"), early_items.index("stone_sword"))

        bb_early.inventory_counts = {"stone_pickaxe": 1}
        armed_items = [c.item for c in hs.select_top(bb_early, limit=5)]
        self.assertEqual(armed_items[0], "stone_sword")

        # 4) Strict iron spending order: 1-2 iron ingots must NOT unlock flint_and_steel / shield / water_bucket before iron_pickaxe!
        bb_iron = Blackboard()
        bb_iron.status = {"dimension": "overworld", "pos": [0.5, 64.0, 0.5], "time_of_day": 1000, "food": 20}
        bb_iron.inventory_counts = {"stone_pickaxe": 1, "stone_sword": 1, "iron_ingot": 2, "flint": 1, "oak_planks": 6}
        iron_items = [c.item for c in hs.select_top(bb_iron, limit=5)]
        self.assertIn("iron_pickaxe", iron_items)
        self.assertNotIn("water_bucket", iron_items)
        self.assertNotIn("flint_and_steel", iron_items)
        self.assertNotIn("shield", iron_items)

        # Once iron_pickaxe is crafted, water_bucket unlocks while flint_and_steel/shield still wait for water_bucket (or 7 ingots)
        bb_iron.inventory_counts = {"iron_pickaxe": 1, "stone_sword": 1, "iron_ingot": 2, "flint": 1, "oak_planks": 6}
        post_pick_items = [c.item for c in hs.select_top(bb_iron, limit=5)]
        self.assertIn("water_bucket", post_pick_items)
        self.assertNotIn("flint_and_steel", post_pick_items)
        self.assertNotIn("shield", post_pick_items)

        # Once both iron_pickaxe and water_bucket exist, flint_and_steel & shield unlock
        bb_iron.inventory_counts = {"iron_pickaxe": 1, "water_bucket": 1, "stone_sword": 1, "iron_ingot": 1, "flint": 1, "oak_planks": 6}
        unlocked_items = [c.item for c in hs.select_top(bb_iron, limit=5)]
        self.assertIn("flint_and_steel", unlocked_items)
        self.assertIn("shield", unlocked_items)

    def test_smelt_item_batch_loads_8_raw_iron_reuses_landmark_and_picks_up_empty_furnace(self) -> None:
        # Part A: Batch-load 8 raw_iron on 1 coal + pick up self-placed empty furnace after completion
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-6, 63, -6, 6, 63, 6, "stone")
        sim.smelt_ticks_required = 2
        sim.give("furnace", 1)
        sim.give("raw_iron", 8)
        sim.give("coal", 1)
        sim.give("stone_pickaxe", 1)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "BatchSmeltBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "BatchSmeltBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                task = SmeltItemTask(ctx, cat, "iron_ingot", 3, poll_interval_seconds=0.0)
                rt.mount_goal(task)

                for _ in range(20):
                    rt.tick_once()
                    self.assertLessEqual(rt.bb.writes_this_tick, 1)
                    if rt.bb.goal_status == "COMPLETED":
                        break

                self.assertEqual(rt.bb.goal_status, "COMPLETED")
                self.assertEqual(sim.inventory_counts.get("iron_ingot", 0), 8)
                self.assertEqual(sim.inventory_counts.get("furnace", 0), 1)
                self.assertFalse(any(rec["name"] == "furnace" for rec in sim.blocks.values()))

                w_acts = [pl for ep, pl in sim.write_calls if ep == "window/act"]
                load_ops = [op for pl in w_acts for op in pl.get("ops", []) if op.get("op") == "put"]
                self.assertTrue(any(op.get("to") == "input" and op.get("count") == 8 for op in load_ops), load_ops)
                self.assertTrue(any(op.get("to") == "fuel" and op.get("count") == 1 for op in load_ops), load_ops)

                reads_before = len(sim.read_calls)
                writes_before = len(sim.write_calls)
                task.stop(rt.bb)
                self.assertEqual(len(sim.read_calls), reads_before)
                self.assertEqual(len(sim.write_calls), writes_before)
            finally:
                client.close()
                db.close()

        # Part B: Batch-mine up to 6 raw_iron when raw_iron < need_raw_more and count >= 3
        sim_mine = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim_mine.fill(-6, 63, -6, 8, 63, 6, "stone")
        sim_mine.set_block(2, 64, 0, "furnace")
        for idx in range(6):
            sim_mine.set_block(3 + (idx // 3), 64 + (idx % 3), 2, "iron_ore")
        sim_mine.give("stone_pickaxe", 1)
        sim_mine.give("coal", 1)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim_mine) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "BatchMineBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "BatchMineBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.mount_goal(SmeltItemTask(ctx, cat, "iron_ingot", 3, poll_interval_seconds=0.0))

                for _ in range(35):
                    rt.tick_once()
                    if rt.bb.goal_status == "COMPLETED":
                        break

                self.assertEqual(rt.bb.goal_status, "COMPLETED")
                self.assertEqual(sim_mine.inventory_counts.get("iron_ingot", 0), 6)
            finally:
                client.close()
                db.close()

        # Part C: Furnace landmark reuse within 24m avoids crafting/placing a duplicate furnace
        sim_lm = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim_lm.fill(-4, 63, -4, 24, 63, 4, "stone")
        sim_lm.set_block(20, 64, 0, "furnace")  # outside 16m scan radius, inside 24m landmark radius
        sim_lm.give("raw_iron", 3)
        sim_lm.give("coal", 1)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim_lm) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "LandmarkBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "LandmarkBot", "pass")
            try:
                client.login()
                db.save_landmark("overworld", "furnace", "latest", (20, 64, 0))
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.mount_goal(SmeltItemTask(ctx, cat, "iron_ingot", 3, poll_interval_seconds=0.0))

                for _ in range(20):
                    rt.tick_once()
                    if rt.bb.goal_status == "COMPLETED":
                        break

                self.assertEqual(rt.bb.goal_status, "COMPLETED")
                self.assertEqual(sim_lm.inventory_counts.get("iron_ingot", 0), 3)
                # Never crafted or placed a second furnace!
                self.assertFalse(any(ep in {"craft", "place"} for ep, _ in sim_lm.write_calls))
                furnace_blocks = [p for p, r in sim_lm.blocks.items() if r["name"] == "furnace"]
                self.assertEqual(furnace_blocks, [(20, 64, 0)])
            finally:
                client.close()
                db.close()

    def test_acquire_item_picks_up_self_placed_crafting_table_only(self) -> None:
        # Case A: Self-placed table is picked back up after top-level craft completes
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-4, 63, -4, 4, 63, 4, "stone")
        sim.give("crafting_table", 1)
        sim.give("cobblestone", 3)
        sim.give("stick", 2)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "TablePickupBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "TablePickupBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                task = AcquireItemTask(ctx, cat, "stone_pickaxe", 1)
                rt.mount_goal(task)

                for _ in range(10):
                    rt.tick_once()
                    self.assertLessEqual(rt.bb.writes_this_tick, 1)
                    if rt.bb.goal_status == "COMPLETED":
                        break

                self.assertEqual(rt.bb.goal_status, "COMPLETED")
                self.assertEqual(sim.inventory_counts.get("stone_pickaxe", 0), 1)
                self.assertEqual(sim.inventory_counts.get("crafting_table", 0), 1)
                self.assertFalse(any(r["name"] == "crafting_table" for r in sim.blocks.values()))

                reads_before = len(sim.read_calls)
                writes_before = len(sim.write_calls)
                task.stop(rt.bb)
                self.assertEqual(len(sim.read_calls), reads_before)
                self.assertEqual(len(sim.write_calls), writes_before)
            finally:
                client.close()
                db.close()

        # Case B: Pre-existing crafting_table in world is NOT picked up
        sim_pre = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim_pre.fill(-4, 63, -4, 4, 63, 4, "stone")
        sim_pre.set_block(2, 64, 0, "crafting_table")
        sim_pre.give("cobblestone", 3)
        sim_pre.give("stick", 2)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim_pre) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "ReuseTableBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "ReuseTableBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.mount_goal(AcquireItemTask(ctx, cat, "stone_pickaxe", 1))

                for _ in range(10):
                    rt.tick_once()
                    if rt.bb.goal_status == "COMPLETED":
                        break

                self.assertEqual(rt.bb.goal_status, "COMPLETED")
                self.assertEqual(sim_pre.inventory_counts.get("stone_pickaxe", 0), 1)
                self.assertEqual(sim_pre.inventory_counts.get("crafting_table", 0), 0)
                self.assertEqual(sim_pre.block_name(2, 64, 0), "crafting_table")
            finally:
                client.close()
                db.close()


if __name__ == "__main__":
    unittest.main()
