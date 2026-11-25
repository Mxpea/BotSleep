"""Phase 4 tests: 886-recipe TaskCatalogue, full empty-handed -> Nether simulation, preemption self-heal, competition."""

from __future__ import annotations

import tempfile
import unittest

from tests_v2.sim.scenarios import scenario_full_nether_speedrun, scenario_v31_sloped_water_bank
from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt import BTRuntime
from v2.gateway import VirtualWorldGateway
from v2.kernel import MinecraftAgentClient, PendingTransactionJournal, UnifiedWorldDB
from v2.primitives import PrimitiveContext, UtilityPrimitive
from v2.tasks import AcquireItemTask, EnterNetherTask, TaskCatalogue
from v2.tasks.competition import CompetitionManager, scoring_catalog


class TestTasksL3(unittest.TestCase):
    def test_catalogue_compiles_886_recipes_and_dag_solvability(self) -> None:
        cat = TaskCatalogue()
        self.assertGreaterEqual(len(cat.recipes), 886)
        for item in ("wooden_pickaxe", "stone_pickaxe", "furnace", "iron_pickaxe", "water_bucket", "flint_and_steel", "shield"):
            self.assertTrue(cat.can_produce(item), f"Expected {item} to be producible")
        # Step estimator decreases as prerequisites are acquired
        empty_cost = cat.estimate_steps("iron_pickaxe", 1, {})
        ingot_cost = cat.estimate_steps("iron_pickaxe", 1, {"iron_ingot": 3, "stick": 2, "crafting_table": 1})
        self.assertGreater(empty_cost, ingot_cost)
        self.assertEqual(ingot_cost, 1)

    def test_end_to_end_empty_handed_spawn_to_the_nether(self) -> None:
        sim = scenario_full_nether_speedrun()
        # Add extra iron_ore & lava sources so full 10-obsidian portal cast completes cleanly
        for i in range(6):
            sim.set_block(-7, 65, -3 - i, "iron_ore")
            sim.set_block(-5, 65, -3 - i, "stone")
        # Expand lava pool to 12 sources at x=54..57, z=-1..1
        sim.fill(54, 63, -1, 57, 63, 2, "lava", {"level": "0"})
        # Nearby water pond near lava pool for easy obsidian quenching
        sim.fill(50, 63, -3, 52, 63, -2, "water", {"level": "0"})

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "SpeedBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "SpeedBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                task = EnterNetherTask(ctx, cat)
                rt.mount_goal(task)

                for _ in range(260):
                    rt.tick_once()
                    # Single-write macro-tick invariant!
                    self.assertLessEqual(rt.bb.writes_this_tick, 1)
                    if sim.dimension == "the_nether":
                        break

                self.assertEqual(
                    sim.dimension,
                    "the_nether",
                    f"Did not reach the_nether; pos={sim.pos}, inv={sim.inventory_counts}, last={rt.bb.last_action_outcome}",
                )
                self.assertEqual(sim.advancements_state.get("minecraft:story/enter_the_nether"), "completed")
                # Verify we never spammed duplicate crafting tables
                tables = [pos for pos, rec in sim.blocks.items() if rec["name"] == "crafting_table"]
                self.assertLessEqual(len(tables), 1)
            finally:
                client.close()
                db.close()

    def test_sloped_water_bank_acquire_clears_occluder_and_fills_bucket(self) -> None:
        sim = scenario_v31_sloped_water_bank()
        for dx, dz in ((1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1), (2, 0), (0, 2), (0, -2)):
            sim.set_block(4 + dx, 64, dz, "stone")
            sim.set_block(4 + dx, 65, dz, "stone")
        sim.set_block(2, 64, 0, "stone")
        sim.set_block(2, 65, 0, "stone")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "BucketBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "BucketBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.mount_goal(AcquireItemTask(ctx, cat, "water_bucket", 1))

                for _ in range(15):
                    rt.tick_once()
                    if sim.inventory_counts.get("water_bucket", 0) >= 1:
                        break
                self.assertEqual(sim.inventory_counts.get("water_bucket", 0), 1)
            finally:
                client.close()
                db.close()

    def test_mid_craft_preemption_by_skeleton_self_heals_cleanly(self) -> None:
        sim = scenario_full_nether_speedrun()
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "HealBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "HealBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.mount_goal(AcquireItemTask(ctx, cat, "stone_pickaxe", 1))

                for tick in range(45):
                    if tick == 4:
                        sim.add_entity(999, "skeleton", (sim.pos[0] + 4.0, sim.pos[1], sim.pos[2]), kind="hostile")
                    elif tick == 6:
                        sim.entities.clear()
                    rt.tick_once()
                    if sim.inventory_counts.get("stone_pickaxe", 0) >= 1:
                        break
                self.assertGreaterEqual(sim.inventory_counts.get("stone_pickaxe", 0), 1)
            finally:
                client.close()
                db.close()

    def test_armor_chest_to_torso_mapping_and_shield_offhand(self) -> None:
        sim = VirtualMcApiServer()
        sim.give("iron_chestplate", 1)
        sim.give("shield", 1)
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "ArmBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "ArmBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                util = UtilityPrimitive(ctx)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.tick_once()
                util.equip_best_armor_or_shield(rt.bb)
                rt.tick_once()
                util.equip_best_armor_or_shield(rt.bb)
                self.assertEqual(sim.armor["chest"], "iron_chestplate")
                self.assertEqual(sim.offhand_item, "shield")
            finally:
                client.close()
                db.close()

    def test_competition_manager_enforces_45_unconditional_overrides_and_server_truth(self) -> None:
        base_cat = scoring_catalog()
        self.assertEqual(len(base_cat), 45)
        self.assertNotIn("minecraft:story/enter_the_end", base_cat)

        with_override = scoring_catalog({"minecraft:story/enter_the_end": True})
        self.assertEqual(len(with_override), 46)
        self.assertIn("minecraft:story/enter_the_end", with_override)

        # Overriding an unconditional or unknown ID raises ValueError
        with self.assertRaises(ValueError):
            scoring_catalog({"minecraft:story/mine_stone": True})

        sim = VirtualMcApiServer()
        with VirtualMcHttpServer(sim) as srv:
            client = MinecraftAgentClient(srv.base_url, "CompBot", "pass")
            try:
                client.login()
                cat = TaskCatalogue()
                mgr = CompetitionManager(cat)
                # Conditional rejected
                self.assertFalse(mgr.select_task("minecraft:story/enter_the_end")["ok"])
                # Unconditional accepted
                sel = mgr.select_task("minecraft:story/mine_stone")
                self.assertTrue(sel["ok"])
                # finish_task refuses before server confirms advancement!
                fin1 = mgr.finish_task(client)
                self.assertFalse(fin1["ok"])
                self.assertEqual(mgr.total_score, 0)
                # Server grants advancement -> finish_task confirms and awards points!
                sim.advancements_state["minecraft:story/mine_stone"] = "completed"
                fin2 = mgr.finish_task(client)
                self.assertTrue(fin2["ok"])
                self.assertTrue(fin2["verified"])
                self.assertGreater(mgr.total_score, 0)
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
