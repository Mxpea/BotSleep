"""Regression tests locking in all protocol, physics, safety, and operational fixes found during v2 review."""

from __future__ import annotations

import math
import tempfile
import unittest

from scripts import botctl
from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt import BTRuntime, Blackboard, Status
from v2.gateway import MultiHopNavigator, SurfaceSwimExecutor, VirtualWorldGateway
from v2.kernel import APIError, MinecraftAgentClient, PendingTransactionJournal, UnifiedWorldDB
from v2.planner import AsyncLLMReplanNode, DepsPlanner, HorizonSelector, IntentCompiler
from v2.primitives import ContainerPrimitive, PrimitiveContext
from v2.tasks import AcquireItemTask, MineResourceTask, SmeltItemTask, TaskCatalogue
from v2.tasks.competition import CompetitionManager


class TestV2ReviewRegressions(unittest.TestCase):
    def test_realtime_delayed_furnace_smelt_uses_upstream_to_from_schema_without_remining(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-6, 63, -6, 6, 63, 6, "stone")
        sim.set_block(2, 64, 0, "furnace")
        sim.smelt_ticks_required = 3  # Real Minecraft takes time; output is NOT instant!
        sim.give("raw_iron", 3)
        sim.give("coal", 1)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "SmeltBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "SmeltBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.mount_goal(SmeltItemTask(ctx, cat, "iron_ingot", 3, poll_interval_seconds=0.0))

                for _ in range(15):
                    rt.tick_once()
                    if sim.inventory_counts.get("iron_ingot", 0) >= 3:
                        break

                self.assertEqual(sim.inventory_counts.get("iron_ingot", 0), 3)
                # Confirm zero dig calls (never went off to re-mine raw_iron while furnace was burning!)
                dig_calls = [ep for ep, _ in sim.write_calls if ep == "dig"]
                self.assertEqual(dig_calls, [])
                # Confirm exact upstream `to`/`from` schema in window/act payloads
                w_acts = [pl for ep, pl in sim.write_calls if ep == "window/act"]
                self.assertTrue(any(op.get("to") == "input" for pl in w_acts for op in pl.get("ops", [])))
                self.assertTrue(any(op.get("from") == "output" for pl in w_acts for op in pl.get("ops", [])))

                # Chest transfer uses exact upstream `put/take` on `container` section
                sim.set_block(-2, 64, 0, "chest")
                cont = ContainerPrimitive(ctx)
                rt.tick_once()
                dep = cont.chest_transfer(rt.bb, (-2, 64, 0), op="deposit", item="iron_ingot", count=2)
                self.assertEqual(dep.status, "OK")
                self.assertEqual(sim.inventory_counts.get("iron_ingot", 0), 1)
                rt.tick_once()
                wdr = cont.chest_transfer(rt.bb, (-2, 64, 0), op="withdraw", item="iron_ingot", count=2)
                self.assertEqual(wdr.status, "OK")
                self.assertEqual(sim.inventory_counts.get("iron_ingot", 0), 3)
            finally:
                client.close()
                db.close()

    def test_surface_swim_uses_mineflayer_yaw_zero_faces_minus_z(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 63.0, 0.5))
        sim.fill(-4, 60, -14, 4, 61, 4, "stone")
        sim.fill(-4, 62, -14, 4, 63, 4, "water", {"level": "0"})
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "SwimNorthBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "SwimNorthBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                swimmer = SurfaceSwimExecutor(client, journal, pulse_ms=400)
                st = client.get_status()["result"]
                # Target due North (-Z)
                res = swimmer.swim_step((0.5, 64.0, -10.0), st)
                self.assertEqual(res.status, "SWAM")
                # Must move toward -Z (North), NOT +Z (South)!
                self.assertLess(sim.pos[2], 0.5)
                # Verify yaw sent was ~0.0 rad (mineflayer 0 = -Z)
                ctrl_payloads = [pl for ep, pl in sim.write_calls if ep == "control"]
                self.assertAlmostEqual(ctrl_payloads[-1]["yaw"], 0.0, places=2)
            finally:
                client.close()
                db.close()

    def test_explore_child_retains_state_and_exhausts_budget_without_infinite_east_loop(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5), loaded_radius=96)
        sim.fill(-60, 63, -60, 60, 63, 60, "grass_block")
        # Zero sugar_cane anywhere -> AcquireItemTask("sugar_cane") delegates to ExploreUntilTask
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "ExpBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "ExpBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                task = AcquireItemTask(ctx, cat, "sugar_cane", 1)
                rt.mount_goal(task)

                saw_needs_ai = False
                for _ in range(30):
                    rt.tick_once()
                    if rt.bb.goal_status == "NEEDS_AI":
                        saw_needs_ai = True
                        break
                self.assertTrue(saw_needs_ai, f"ExploreUntilTask never exhausted budget; goal={rt.bb.goal_status}, pos={sim.pos}")
                self.assertIn("explore_budget_exhausted", rt.bb.last_failure_reason)
            finally:
                client.close()
                db.close()

    def test_java_lava_water_physics_direct_water_replaces_lava_adjacent_casts_obsidian(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-4, 63, -4, 6, 63, 4, "stone")
        sim.set_block(2, 64, 0, "lava", {"level": "0"})
        sim.give("water_bucket", 2)
        # Pouring directly ONTO lava replaces it with water (Java Edition!)
        r1 = sim.handle("POST", "interact", {"pos": [2, 64, 0], "item": "water_bucket"})
        self.assertTrue(r1["ok"])
        self.assertEqual(sim.block_name(2, 64, 0), "water")
        # Pouring ADJACENT to lava casts obsidian!
        sim.set_block(2, 64, 0, "air")
        sim.set_block(2, 64, 0, "lava", {"level": "0"})
        r2 = sim.handle("POST", "interact", {"pos": [2, 64, 1], "item": "water_bucket"})
        self.assertTrue(r2["ok"])
        self.assertEqual(sim.block_name(2, 64, 0), "obsidian")

    def test_unknown_write_on_goal_tick_never_falls_through_to_replan_same_tick(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-4, 63, -4, 4, 63, 4, "stone")
        sim.set_block(2, 64, 0, "oak_log")
        sim.inject_fault("not_connected", "dig")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "NoFallBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "NoFallBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                compiler = IntentCompiler(ctx, cat)
                horizon = HorizonSelector(cat)
                planner = DepsPlanner(None, compiler, horizon)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                replan = AsyncLLMReplanNode(planner, rt.goal_mount)
                rt.selector.children.append(replan)

                rt.mount_goal(AcquireItemTask(ctx, cat, "oak_log", 1))
                rt.tick_once()
                # Must freeze on this exact tick and NOT call AsyncLLMReplanNode!
                self.assertTrue(journal.has_pending_unknown())
                self.assertTrue(rt.bb.get("pending_frozen"))
                self.assertEqual(rt.bb.active_branch, "ActiveGoalMount")
                self.assertIsNone(rt.bb.get("last_plan_result"))
            finally:
                client.close()
                db.close()

    def test_watchdog_aborts_stalled_goal_so_replan_takes_over(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-4, 63, -4, 4, 63, 4, "stone")
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "AbortBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "AbortBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)

                class _ForeverStuck:
                    name = "forever_stuck"
                    def __init__(self) -> None:
                        self.stopped = 0
                    def tick(self, bb: Blackboard) -> str:
                        bb.last_action_outcome = "blocked"
                        return "RUNNING"
                    def stop(self, bb: Blackboard) -> None:
                        self.stopped += 1

                g = _ForeverStuck()
                rt.mount_goal(g)
                for _ in range(8):
                    rt.tick_once()
                self.assertGreaterEqual(g.stopped, 1)
                self.assertIsNone(rt.goal_mount.goal)
            finally:
                client.close()
                db.close()

    def test_hard_safety_lava_douse_cooldown_and_no_barehanded_melee(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        # Deep lava pool (no solid block under feet!) + water_bucket in inventory
        sim.fill(-2, 62, -2, 2, 64, 2, "lava", {"level": "0"})
        sim.give("water_bucket", 1)
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "LavaBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "LavaBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)

                # Tick 1 tries water_bucket douse -> fails with no_support -> enters cooldown
                rt.tick_once()
                self.assertEqual(rt.bb.last_action_outcome, "safety_failed:no_support")
                # Tick 2 MUST fall back to control jump-escape instead of retrying interact forever!
                rt.tick_once()
                self.assertEqual(rt.bb.last_action_outcome, "safety_escape_lava")
            finally:
                client.close()
                db.close()

        # Bare-handed player near zombie must retreat, NOT punch bare-handed!
        sim2 = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim2.fill(-12, 63, -12, 12, 63, 12, "stone")
        sim2.add_entity(501, "zombie", (2.5, 64.0, 0.5), kind="hostile")
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim2) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "RetreatBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "RetreatBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.tick_once()
                self.assertEqual(rt.bb.last_action_outcome, "safety_retreat:zombie")
            finally:
                client.close()
                db.close()

    def test_tall_canopy_refuses_lethal_drop_dig(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 80.0, 0.5))
        sim.fill(-4, 63, -4, 4, 63, 4, "grass_block")
        # Floating leaf 16 blocks above ground with pure air below!
        sim.set_block(0, 79, 0, "oak_leaves")
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "TallBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "TallBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.tick_once()
                # Must NOT dig out the leaf under feet into a 16-block lethal fall!
                self.assertEqual(sim.block_name(0, 79, 0), "oak_leaves")
            finally:
                client.close()
                db.close()

    def test_mine_resource_uses_pickaxe_tier_clears_3high_staircase_and_tunnels_2high(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 18.0, 0.5))
        sim.fill(-2, 14, -2, 8, 22, 2, "stone")
        # Carve small starting room at (0, 18..19, 0)
        sim.set_block(0, 18, 0, "air")
        sim.set_block(0, 19, 0, "air")
        sim.give("iron_pickaxe", 1)  # Higher tier than stone_pickaxe!

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "MineBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "MineBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.mount_goal(MineResourceTask(ctx, cat, "raw_iron", 1, target_y=16))

                for _ in range(18):
                    rt.tick_once()
                # Should descend to y=16 AND advance along +X in a 2-high tunnel without crafting stone_pickaxe!
                self.assertEqual(sim.inventory_counts.get("stone_pickaxe", 0), 0)
                self.assertLessEqual(sim.pos[1], 16.0)
                self.assertGreaterEqual(sim.pos[0], 3.0)
            finally:
                client.close()
                db.close()

    def test_birch_only_spawn_crafts_wooden_pickaxe_without_oak_trees(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-12, 63, -12, 12, 63, 12, "grass_block")
        # Only birch trees exist — zero oak!
        for y in range(64, 68):
            sim.set_block(3, y, 0, "birch_log")
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "BirchBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "BirchBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.mount_goal(AcquireItemTask(ctx, cat, "wooden_pickaxe", 1))
                for _ in range(25):
                    rt.tick_once()
                    if sim.inventory_counts.get("wooden_pickaxe", 0) >= 1:
                        break
                self.assertEqual(sim.inventory_counts.get("wooden_pickaxe", 0), 1)
            finally:
                client.close()
                db.close()

    def test_sync_blackboard_survives_transient_not_ready_during_dimension_switch(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-4, 63, -4, 4, 63, 4, "stone")
        sim.inject_fault("not_ready", "get_entities")
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "DimBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "DimBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                # Tick 1 encounters transient not_ready -> returns RUNNING cleanly instead of crashing!
                st1 = rt.tick_once()
                self.assertEqual(st1, Status.RUNNING)
                self.assertEqual(rt.observe.consecutive_read_errors, 1)
                # Tick 2 recovers cleanly!
                rt.tick_once()
                self.assertEqual(rt.observe.consecutive_read_errors, 0)
            finally:
                client.close()
                db.close()

    def test_botctl_detects_v2_instance_lock_and_pending_tx_and_competition_persists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = {
                "agent": {"mode": "survival", "name": "OpsBot"},
                "mc_api": {"base_url": "http://127.0.0.1:18900/api/v1"},
                "survival": {"state_dir": td, "world_id": "w_ops", "objective": "daily"},
                "llm": {"enabled": False},
            }
            # Idle -> ready
            r0 = botctl.inspect_runtime(cfg)
            self.assertTrue(r0["ready_to_replace"])

            db = UnifiedWorldDB.for_scope(td, cfg["mc_api"]["base_url"], "OpsBot", "w_ops")
            journal = PendingTransactionJournal(db)
            try:
                tx_id = journal.begin_write("dig", {"pos": [1, 64, 1]})
                journal.resolve_write(tx_id, "unknown")
                with journal.instance_lock():
                    r1 = botctl.inspect_runtime(cfg)
                    self.assertFalse(r1["ready_to_replace"])
                    self.assertIn("instance_already_running", r1["block_reasons"])
                    self.assertIn("pending_game_action_requires_resume", r1["block_reasons"])
                    self.assertIsNotNone(r1["journal"]["v2_pending"])

                # Competition persistence & reset
                cat = TaskCatalogue()
                m1 = CompetitionManager(cat, db=db)
                self.assertTrue(m1.select_task("minecraft:story/mine_stone")["ok"])
                m2 = CompetitionManager(cat, db=db)
                self.assertEqual(m2.active_task_id, "minecraft:story/mine_stone")
                m3 = CompetitionManager(cat, db=db, reset=True)
                self.assertIsNone(m3.active_task_id)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
