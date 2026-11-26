"""Tests for chunk-load-aware corpse tracking, RecoverDeathDropTask, HuntFoodTask, and cornered weaponless knockback."""

from __future__ import annotations

import tempfile
import time
import unittest

from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt import BTRuntime, Blackboard
from v2.gateway import VirtualWorldGateway
from v2.kernel import MinecraftAgentClient, PendingTransactionJournal, UnifiedWorldDB
from v2.primitives.base import PrimitiveContext
from v2.tasks.catalogue import TaskCatalogue
from v2.tasks.corpse import RecoverDeathDropTask
from v2.tasks.food import HuntFoodTask
from v2.tasks.surface import save_anchor_landmark


class TestCorpseRecoveryAndCombat(unittest.TestCase):
    def test_observe_node_filters_early_and_lava_deaths_and_tracks_loaded_timer(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.give("wooden_pickaxe", 1)
        sim.give("stone_pickaxe", 1)
        sim.give("cobblestone", 12)
        sim.give("raw_iron", 3)
        sim.give("iron_ingot", 1)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "CorpseTrackBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "CorpseTrackBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)

                # Tick 1 caches last_alive_inv (wood/stone + below-threshold iron)
                rt.tick_once()
                self.assertIsNotNone(rt.bb.get("last_alive_inv"))

                # Early wood/stone death -> MUST NOT activate active_corpse_drop
                sim.inventory_counts.clear()
                sim.push_event("death", pos=[5.0, 64.0, 5.0], dropped_items_at=[5.0, 64.0, 5.0], cause="Zombie")
                rt.tick_once()
                self.assertIsNone(rt.bb.get("active_corpse_drop"))
                self.assertEqual(len(rt.bb.death_positions), 1)

                # Respawn with iron_pickaxe + offhand shield, then die in lava -> skipped!
                sim.give("iron_pickaxe", 1)
                sim.offhand_item = "shield"
                rt.tick_once()
                sim.inventory_counts.clear()
                sim.offhand_item = None
                sim.push_event(
                    "death",
                    pos=[10.0, 12.0, 10.0],
                    dropped_items_at=[10.0, 12.0, 10.0],
                    cause="Player tried to swim in lava",
                )
                rt.tick_once()
                self.assertIsNone(rt.bb.get("active_corpse_drop"))

                # Respawn with iron_sword -> tick alive -> die normally -> activates active_corpse_drop!
                sim.give("iron_sword", 1)
                rt.tick_once()
                sim.inventory_counts.clear()
                sim.push_event(
                    "death",
                    pos=[20.0, 64.0, 20.0],
                    dropped_items_at=[20.0, 64.0, 20.0],
                    cause="Slain by Skeleton",
                )
                rt.tick_once()
                drop = rt.bb.get("active_corpse_drop")
                self.assertIsInstance(drop, dict)
                self.assertEqual(drop["dim"], "overworld")
                self.assertEqual(drop["pos"], [20.0, 64.0, 20.0])
                self.assertIn("iron_sword", drop["key_items"])
                self.assertEqual(drop["loaded_elapsed"], 0.0)

                # Same-dim <= 160m: timer advances by min(5.0, delta)
                sim.pos = [30.0, 64.0, 30.0]
                drop["last_tick_ts"] = time.time() - 3.0
                rt.tick_once()
                self.assertGreaterEqual(drop["loaded_elapsed"], 2.9)
                self.assertLessEqual(drop["loaded_elapsed"], 5.0)
                before_pause = drop["loaded_elapsed"]

                # Outside 160m: timer MUST PAUSE
                sim.pos = [300.0, 64.0, 300.0]
                drop["last_tick_ts"] = time.time() - 4.0
                rt.tick_once()
                self.assertAlmostEqual(drop["loaded_elapsed"], before_pause, places=5)

                # Other dimension: timer MUST PAUSE
                sim.dimension = "the_nether"
                sim.pos = [20.0, 64.0, 20.0]
                drop["last_tick_ts"] = time.time() - 4.0
                rt.tick_once()
                self.assertAlmostEqual(drop["loaded_elapsed"], before_pause, places=5)

                # Re-enter overworld <= 160m and cross 300s -> cleared!
                sim.dimension = "overworld"
                sim.pos = [25.0, 64.0, 25.0]
                drop["loaded_elapsed"] = 298.0
                drop["last_tick_ts"] = time.time() - 3.0
                rt.tick_once()
                self.assertIsNone(rt.bb.get("active_corpse_drop"))
            finally:
                client.close()
                db.close()

    def test_recover_death_drop_loaded_rush_and_surface_anchor_and_item_pickup(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5), loaded_radius=192)
        sim.fill(-4, 63, -4, 40, 63, 12, "stone")
        # Underground cave floor at y=49 below surface_entry shaft at x=12, z=0
        sim.fill(8, 49, -4, 30, 49, 4, "stone")
        # Staircase opening from (12,64,0) down to (20,50,0) (actually walkable step-down or flat in sim)
        sim.fill(10, 63, -2, 22, 63, 2, "air")
        for step in range(15):
            sim.set_block(10 + step, 63 - step, 0, "stone")
            sim.fill(10 + step, 64 - step, -1, 10 + step, 67 - step, 1, "air")
        # Item entity at drop site
        sim.add_entity(901, "item", [24.0, 50.0, 0.5], kind="object")
        sim.entities[-1]["item"] = "iron_pickaxex1"

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "LoadedCorpseBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "LoadedCorpseBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()

                # Save surface_entry landmark at (10, 64, 0) (<= 48m from bot and closer to drop at (24, 50, 0))
                save_anchor_landmark(db, rt.bb, "last_surface_anchor", "surface_entry", (10, 64, 0))
                rt.bb.set(
                    "active_corpse_drop",
                    {
                        "id": "d1",
                        "dim": "overworld",
                        "pos": [24.0, 50.0, 0.5],
                        "created_at": time.time(),
                        "loaded_elapsed": 5.0,
                        "last_tick_ts": time.time(),
                        "key_items": ["iron_pickaxe"],
                        "cause": "Skeleton",
                    },
                    writer="test",
                )
                task = RecoverDeathDropTask(ctx, cat, loaded_radius=160.0)
                rt.mount_goal(task)

                # Tick 1 (no weapon/pickaxe, <= 160m): MUST skip prep and rush via surface_entry!
                rt.tick_once()
                craft_or_dig = [ep for ep, _ in sim.write_calls if ep in ("craft", "dig")]
                self.assertEqual(craft_or_dig, [])
                self.assertIsInstance(rt.bb.get("active_corpse_drop"), dict)

                # Simulate arrival near drop with item entity still present -> steps onto item, does NOT clear yet!
                sim.pos = [21.0, 50.0, 0.5]
                rt.tick_once()
                self.assertIsInstance(rt.bb.get("active_corpse_drop"), dict)
                self.assertEqual(rt.bb.goal_status, "RUNNING")

                # Item picked up (entities now empty) and within 6m -> clears active_corpse_drop & COMPLETED!
                sim.entities.clear()
                sim.pos = [24.0, 50.0, 0.5]
                rt.tick_once()
                self.assertIsNone(rt.bb.get("active_corpse_drop"))
                self.assertEqual(rt.bb.goal_status, "COMPLETED")
                # Pure-memory stop makes zero HTTP writes
                writes_before = len(sim.write_calls)
                task.stop(rt.bb)
                self.assertEqual(len(sim.write_calls), writes_before)
            finally:
                client.close()
                db.close()

    def test_recover_death_drop_unloaded_prep_and_never_clears_on_nav_fail(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-6, 63, -6, 6, 63, 6, "stone")
        # Crafting table + materials for stone_pickaxe & stone_sword
        sim.set_block(2, 64, 0, "crafting_table")
        sim.give("cobblestone", 5)
        sim.give("stick", 3)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "UnloadedCorpseBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "UnloadedCorpseBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()

                # Corpse is 220m away (> 160m unloaded) -> Mode B crafts stone_pickaxe first
                rt.bb.set(
                    "active_corpse_drop",
                    {
                        "id": "d2",
                        "dim": "overworld",
                        "pos": [220.5, 64.0, 0.5],
                        "created_at": time.time(),
                        "loaded_elapsed": 0.0,
                        "last_tick_ts": time.time(),
                        "key_items": ["iron_pickaxe"],
                        "cause": "Zombie",
                    },
                    writer="test",
                )
                task = RecoverDeathDropTask(ctx, cat, loaded_radius=160.0)
                rt.mount_goal(task)

                rt.tick_once()
                self.assertGreaterEqual(sim.inventory_counts.get("stone_pickaxe", 0), 1)

                # Now bot enters loaded_radius (<= 160m) BEFORE crafting stone_sword -> MUST abort prep & rush!
                sim.pos = [100.5, 64.0, 0.5]
                sim.fill(95, 63, -4, 130, 63, 4, "stone")
                rt.tick_once()
                self.assertEqual(sim.inventory_counts.get("stone_sword", 0), 0)
                self.assertIsNone(task._active_child)

                # Inject repeated move_to path_not_found failures -> NEVER clears active_corpse_drop!
                for _ in range(7):
                    sim.inject_fault("path_not_found", "move_to")
                    rt.tick_once()
                self.assertEqual(rt.bb.goal_status, "NEEDS_AI")
                self.assertIsInstance(rt.bb.get("active_corpse_drop"), dict)
            finally:
                client.close()
                db.close()

    def test_hunt_food_task_hunts_collects_and_respects_daytime_and_budget(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-12, 63, -12, 12, 63, 12, "stone")
        sim.time_of_day = 2000
        sim.add_entity(101, "cow", [2.5, 64.0, 0.5], kind="passive", health=10.0)
        sim.add_entity(102, "pig", [6.5, 64.0, 0.5], kind="passive", health=10.0)
        sim.add_entity(103, "item", [1.5, 64.0, 0.5], kind="object")
        sim.entities[-1]["item"] = "beefx2"

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "FoodHunterBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "FoodHunterBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                ctx = PrimitiveContext(gw, journal)

                task = HuntFoodTask(ctx, min_food_items=4, max_steps=16)
                rt.mount_goal(task)

                # Tick 1 prioritizes nearby meat item drop within 8m
                rt.tick_once()
                self.assertEqual(rt.bb.goal_status, "RUNNING")
                # Simulate item picked up (2 beef)
                sim.entities = [e for e in sim.entities if e["id"] != 103]
                sim.give("beef", 2)

                # Tick 2 strikes cow (<= 3.0m) -> sim gives 2 beef -> total 4 >= min_food_items!
                rt.tick_once()
                rt.tick_once()
                self.assertEqual(rt.bb.goal_status, "COMPLETED")
                self.assertGreaterEqual(sim.inventory_counts.get("beef", 0), 4)

                # Nighttime check -> FAILED_LOCAL
                sim.inventory_counts.clear()
                sim.time_of_day = 15000
                night_task = HuntFoodTask(ctx, min_food_items=4, max_steps=16)
                rt.mount_goal(night_task)
                rt.tick_once()
                self.assertEqual(rt.bb.goal_status, "FAILED_LOCAL")

                # Pure-memory stop makes zero HTTP writes
                writes_before = len(sim.write_calls)
                night_task.stop(rt.bb)
                self.assertEqual(len(sim.write_calls), writes_before)
            finally:
                client.close()
                db.close()

    def test_hard_safety_cornered_weaponless_knockback_strike(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-2, 60, -2, 6, 63, 2, "stone")
        # Enclosed dead-end chamber with zombie right in front at d=2.0 (<= 2.5m), NO weapon!
        for y in (64, 65):
            for x in range(-1, 5):
                sim.set_block(x, y, -1, "stone")
                sim.set_block(x, y, 1, "stone")
            sim.set_block(-1, y, 0, "stone")
            sim.set_block(4, y, 0, "stone")
        sim.give("stone_pickaxe", 1)
        sim.held_item = "stone_pickaxe"
        sim.health = 12.0
        sim.add_entity(201, "zombie", (2.5, 64.0, 0.5), kind="hostile", health=20.0)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "CorneredBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "CorneredBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False, threat_radius=8.0)

                rt.tick_once()
                self.assertEqual(rt.bb.last_action_outcome, "safety_strike:zombie")
                attack_calls = [pl for ep, pl in sim.write_calls if ep == "attack"]
                self.assertEqual(len(attack_calls), 1)
                self.assertEqual(attack_calls[0]["max_hits"], 1)
            finally:
                client.close()
                db.close()


if __name__ == "__main__":
    unittest.main()
