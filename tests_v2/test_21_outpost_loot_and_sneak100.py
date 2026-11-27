"""Tests for L0 domain loot/outpost/deep-dark constants, EstablishOutpostTask, NetherPortalShelterTask, AvoidVibrationTask, and LootChestTask."""

from __future__ import annotations

import tempfile
import unittest

from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt import BTRuntime, Status
from v2.bt.blackboard import Blackboard
from v2.domain import (
    BED_BLOCKS,
    DEEP_DARK_BLOCKS,
    LOOT_WHITELIST_ITEMS,
    STONE_MOLD_ITEMS,
    VALUABLE_STASH_ITEMS,
)
from v2.gateway import VirtualWorldGateway
from v2.kernel import MinecraftAgentClient, PendingTransactionJournal, UnifiedWorldDB
from v2.primitives import PrimitiveContext
from v2.tasks.catalogue import TaskCatalogue
from v2.tasks.loot import AvoidVibrationTask, LootChestTask
from v2.tasks.outpost import EstablishOutpostTask, NetherPortalShelterTask


def _drive_task(rt: BTRuntime, task, *, max_ticks: int = 40):
    rt.mount_goal(task)
    last_status = Status.RUNNING
    for _ in range(max_ticks):
        last_status = rt.tick_once()
        assert rt.bb.writes_this_tick <= 1, f"Exceeded 1 write/tick: {rt.bb.writes_this_tick}"
        if rt.bb.goal_status in {"COMPLETED", "FAILED_LOCAL", "NEEDS_AI"}:
            break
    return last_status, rt.bb.goal_status


class TestOutpostLootAndSneak100(unittest.TestCase):
    def test_domain_constants_present(self) -> None:
        self.assertIn("diamond", LOOT_WHITELIST_ITEMS)
        self.assertIn("crying_obsidian", LOOT_WHITELIST_ITEMS)
        self.assertIn("fire_charge", LOOT_WHITELIST_ITEMS)
        self.assertNotIn("rotten_flesh", LOOT_WHITELIST_ITEMS)
        self.assertIn("diamond_pickaxe", VALUABLE_STASH_ITEMS)
        self.assertIn("raw_iron", VALUABLE_STASH_ITEMS)
        self.assertEqual(
            DEEP_DARK_BLOCKS,
            frozenset({"sculk_sensor", "calibrated_sculk_sensor", "sculk_shrieker", "sculk_catalyst"}),
        )
        self.assertEqual(STONE_MOLD_ITEMS, ("cobblestone", "cobbled_deepslate"))

    def test_overworld_outpost_places_chest_and_binds_bed_daytime(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5), dimension="overworld")
        sim.fill(-6, 63, -6, 6, 63, 6, "stone")
        sim.time_of_day = 1000  # daytime
        sim.give("chest", 1)
        sim.give("white_bed", 1)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "OutpostOW", "w1")
            client = MinecraftAgentClient(srv.base_url, "OutpostOW", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                task = EstablishOutpostTask(ctx, TaskCatalogue(), mode="establish")

                _st, goal_st = _drive_task(rt, task, max_ticks=20)
                self.assertEqual(goal_st, "COMPLETED")

                chest_pos = rt.bb.get("outpost_chest")
                bed_pos = rt.bb.get("outpost_bed")
                self.assertIsNotNone(chest_pos)
                self.assertIsNotNone(bed_pos)
                self.assertEqual(sim.block_name(*chest_pos), "chest")
                self.assertIn(sim.block_name(*bed_pos), BED_BLOCKS)
                self.assertIn(chest_pos, rt.bb.get("owned_chest_coords") or [])
                self.assertEqual(rt.bb.get("outpost_bed_bound"), bed_pos)
                self.assertTrue(any(ev.get("type") == "spawn_bound" for ev in sim.events))

                # stop(bb) must be pure-memory with zero writes
                writes_before = len(sim.write_calls)
                task.stop(rt.bb)
                self.assertEqual(len(sim.write_calls), writes_before)
            finally:
                client.close()
                db.close()

    def test_nether_outpost_hard_forbids_bed_and_saves_nether_chest(self) -> None:
        # Case A: bed-only inventory in the_nether -> FAILED_LOCAL("nether_bed_forbidden"), ZERO bed writes
        sim1 = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5), dimension="the_nether")
        sim1.fill(-5, 63, -5, 5, 63, 5, "netherrack")
        sim1.give("white_bed", 1)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim1) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "NetherBedRefuse", "w1")
            client = MinecraftAgentClient(srv.base_url, "NetherBedRefuse", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                task = EstablishOutpostTask(ctx, TaskCatalogue(), mode="establish")

                _st, goal_st = _drive_task(rt, task, max_ticks=10)
                self.assertEqual(goal_st, "FAILED_LOCAL")
                self.assertEqual(rt.bb.last_failure_reason, "nether_bed_forbidden")
                self.assertEqual(sim1.write_calls, [])

                # Explicit require_bed=True also hard-refuses with zero writes
                task2 = EstablishOutpostTask(ctx, TaskCatalogue(), mode="establish", require_bed=True)
                _st2, goal_st2 = _drive_task(rt, task2, max_ticks=5)
                self.assertEqual(goal_st2, "FAILED_LOCAL")
                self.assertEqual(rt.bb.last_failure_reason, "nether_bed_forbidden")
                self.assertEqual(sim1.write_calls, [])
            finally:
                client.close()
                db.close()

        # Case B: chest + bed in the_nether -> places ONLY chest, skips bed, zero bed writes
        sim2 = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5), dimension="the_nether")
        sim2.fill(-5, 63, -5, 5, 63, 5, "netherrack")
        sim2.give("chest", 1)
        sim2.give("white_bed", 1)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim2) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "NetherChestOnly", "w1")
            client = MinecraftAgentClient(srv.base_url, "NetherChestOnly", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                task = EstablishOutpostTask(ctx, TaskCatalogue(), mode="establish")

                _st, goal_st = _drive_task(rt, task, max_ticks=12)
                self.assertEqual(goal_st, "COMPLETED")
                nether_chest = rt.bb.get("nether_outpost_chest")
                self.assertIsNotNone(nether_chest)
                self.assertEqual(sim2.block_name(*nether_chest), "chest")
                self.assertIn(nether_chest, rt.bb.get("owned_chest_coords") or [])
                self.assertIsNone(rt.bb.get("outpost_bed"))
                bed_writes = [
                    (ep, p)
                    for ep, p in sim2.write_calls
                    if (ep == "place" and str(p.get("item", "")).endswith("_bed")) or ep == "interact"
                ]
                self.assertEqual(bed_writes, [])
            finally:
                client.close()
                db.close()

    def test_outpost_stash_retains_kit_and_withdraw_restores(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5), dimension="overworld")
        sim.fill(-5, 63, -5, 5, 63, 5, "stone")
        sim.set_block(2, 64, 0, "chest")
        sim.containers[(2, 64, 0)] = {"type": "chest", "items": {}}
        sim.give("stone_pickaxe", 1)
        sim.give("cobblestone", 40)
        sim.give("cooked_beef", 6)
        sim.give("diamond", 3)
        sim.give("iron_ingot", 5)
        sim.give("golden_apple", 2)
        sim.give("dirt", 8)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "StashBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "StashBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)

                stash_task = EstablishOutpostTask(ctx, TaskCatalogue(), mode="stash")
                _st, goal_st = _drive_task(rt, stash_task, max_ticks=20)
                self.assertEqual(goal_st, "COMPLETED")

                chest_items = sim.containers[(2, 64, 0)]["items"]
                self.assertEqual(chest_items.get("diamond"), 3)
                self.assertEqual(chest_items.get("iron_ingot"), 5)
                self.assertEqual(chest_items.get("golden_apple"), 2)
                self.assertEqual(sim.inventory_counts.get("stone_pickaxe"), 1)
                self.assertGreaterEqual(sim.inventory_counts.get("cobblestone", 0), 32)
                self.assertGreaterEqual(sim.inventory_counts.get("cooked_beef", 0), 4)
                self.assertEqual(sim.inventory_counts.get("dirt"), 8)
                self.assertNotIn("diamond", sim.inventory_counts)

                withdraw_task = EstablishOutpostTask(ctx, TaskCatalogue(), mode="withdraw")
                _st2, goal_st2 = _drive_task(rt, withdraw_task, max_ticks=20)
                self.assertEqual(goal_st2, "COMPLETED")
                self.assertEqual(sim.inventory_counts.get("diamond"), 3)
                self.assertEqual(sim.inventory_counts.get("iron_ingot"), 5)
                self.assertEqual(sim.inventory_counts.get("golden_apple"), 2)
                self.assertEqual(sim.containers[(2, 64, 0)]["items"], {})
            finally:
                client.close()
                db.close()

    def test_nether_portal_shelter_builds_shield_and_transit_chest(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 3.5), dimension="the_nether")
        sim.fill(-8, 63, -8, 8, 63, 8, "netherrack")
        # 4x5 portal along X: interior at x in [0, 1], y in [64..66], z=0
        for x in (0, 1):
            sim.set_block(x, 63, 0, "obsidian")
            sim.set_block(x, 67, 0, "obsidian")
        for y in (64, 65, 66):
            sim.set_block(-1, y, 0, "obsidian")
            sim.set_block(2, y, 0, "obsidian")
            for x in (0, 1):
                sim.set_block(x, y, 0, "nether_portal")
        sim.give("cobblestone", 16)
        sim.give("chest", 1)
        sim.give("white_bed", 1)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "PortalShelterBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "PortalShelterBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)

                task = NetherPortalShelterTask(ctx, TaskCatalogue())
                _st, goal_st = _drive_task(rt, task, max_ticks=30)
                self.assertEqual(goal_st, "COMPLETED")
                self.assertTrue(rt.bb.get("nether_portal_sheltered"))
                # Doorway on bot side (z=1) remains open
                self.assertEqual(sim.block_name(0, 64, 1), "air")
                self.assertEqual(sim.block_name(1, 64, 1), "air")
                # Back wall shielded with cobblestone
                self.assertEqual(sim.block_name(0, 64, -1), "cobblestone")
                self.assertEqual(sim.block_name(1, 64, -1), "cobblestone")
                # Transit chest placed & saved
                nether_chest = rt.bb.get("nether_outpost_chest")
                self.assertIsNotNone(nether_chest)
                self.assertEqual(sim.block_name(*nether_chest), "chest")
                # Zero bed writes
                bed_writes = [
                    (ep, p)
                    for ep, p in sim.write_calls
                    if (ep == "place" and str(p.get("item", "")).endswith("_bed")) or ep == "interact"
                ]
                self.assertEqual(bed_writes, [])

                # stop(bb) must be pure-memory with zero writes
                writes_before = len(sim.write_calls)
                task.stop(rt.bb)
                self.assertEqual(len(sim.write_calls), writes_before)
            finally:
                client.close()
                db.close()

    def test_avoid_vibration_sneaks_approaches_steps_and_resets_posture(self) -> None:
        # Missing sensor -> FAILED_LOCAL
        sim_empty = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim_empty.fill(-4, 63, -4, 4, 63, 4, "stone")
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim_empty) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "NoSensorBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "NoSensorBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                task = AvoidVibrationTask(ctx, radius=32)
                _st, goal_st = _drive_task(rt, task, max_ticks=10)
                self.assertEqual(goal_st, "FAILED_LOCAL")
                self.assertEqual(rt.bb.last_failure_reason, "avoid_vibration:no_sensor")
            finally:
                client.close()
                db.close()

        # Sensor 12m away -> posture(sneak=True), approach <= 8m, sneak steps, posture(sneak=False)
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-4, 63, -4, 16, 63, 4, "deepslate")
        sim.set_block(12, 64, 0, "sculk_sensor")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "SneakBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "SneakBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                task = AvoidVibrationTask(ctx, radius=32)
                rt.mount_goal(task)

                saw_sneak_move = False
                for _ in range(15):
                    rt.tick_once()
                    self.assertLessEqual(rt.bb.writes_this_tick, 1)
                    if sim.sneaking and sim.pos[0] > 0.5:
                        saw_sneak_move = True
                    if rt.bb.goal_status in {"COMPLETED", "FAILED_LOCAL"}:
                        break

                self.assertEqual(rt.bb.goal_status, "COMPLETED")
                self.assertTrue(saw_sneak_move)
                self.assertTrue(rt.bb.get("avoid_vibration_done"))
                self.assertFalse(sim.sneaking)
                posture_calls = [p for ep, p in sim.write_calls if ep == "posture"]
                self.assertGreaterEqual(len(posture_calls), 2)
                self.assertTrue(posture_calls[0]["sneak"])
                self.assertFalse(posture_calls[-1]["sneak"])
                control_calls = [p for ep, p in sim.write_calls if ep == "control"]
                self.assertTrue(any(c.get("sneak") is True for c in control_calls))

                # stop(bb) is pure-memory
                n_writes = len(sim.write_calls)
                task.stop(rt.bb)
                self.assertEqual(len(sim.write_calls), n_writes)
            finally:
                client.close()
                db.close()

    def test_loot_chest_whitelist_owned_exclusion_dedup_trap_and_trip_cap(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5), dimension="overworld")
        sim.fill(-12, 63, -12, 16, 63, 12, "deepslate")

        # 1) Own outpost chest at (2, 64, 0) -> MUST NEVER be looted
        sim.set_block(2, 64, 0, "chest")
        sim.containers[(2, 64, 0)] = {"type": "chest", "items": {"diamond": 9}}

        # 2) Trapped desert-temple chest at (4, 64, 0) with TNT below & pressure plate beside -> MUST be rejected
        sim.set_block(4, 64, 0, "chest")
        sim.set_block(4, 62, 0, "tnt")
        sim.set_block(4, 64, 1, "stone_pressure_plate")
        sim.containers[(4, 64, 0)] = {"type": "chest", "items": {"emerald": 5}}

        # 3) Safe Deep Dark barrel at (8, 64, 0) near a sculk_sensor -> MUST sneak, loot whitelist only, and reset sneak
        sim.set_block(9, 64, 1, "sculk_sensor")
        sim.set_block(8, 64, 0, "barrel")
        sim.containers[(8, 64, 0)] = {
            "type": "barrel",
            "items": {
                "diamond": 2,
                "iron_ingot": 4,
                "rotten_flesh": 6,
                "wheat_seeds": 12,
            },
        }

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "LootBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "LootBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.bb.set("outpost_chest", (2, 64, 0), writer="test")
                rt.bb.set("owned_chest_coords", [(2, 64, 0)], writer="test")

                task1 = LootChestTask(ctx, radius=32, max_high_risk_trips=2)
                _st, goal_st = _drive_task(rt, task1, max_ticks=25)
                self.assertEqual(goal_st, "COMPLETED")

                # Whitelisted items looted, junk left behind, own & trapped chests untouched
                self.assertEqual(sim.inventory_counts.get("diamond"), 2)
                self.assertEqual(sim.inventory_counts.get("iron_ingot"), 4)
                self.assertNotIn("rotten_flesh", sim.inventory_counts)
                self.assertNotIn("wheat_seeds", sim.inventory_counts)
                self.assertEqual(sim.containers[(8, 64, 0)]["items"], {"rotten_flesh": 6, "wheat_seeds": 12})
                self.assertEqual(sim.containers[(2, 64, 0)]["items"], {"diamond": 9})
                self.assertEqual(sim.containers[(4, 64, 0)]["items"], {"emerald": 5})

                # Deep Dark sneak engaged & cleanly reset
                self.assertFalse(sim.sneaking)
                posture_calls = [p for ep, p in sim.write_calls if ep == "posture"]
                self.assertTrue(any(p.get("sneak") is True for p in posture_calls))
                self.assertFalse(posture_calls[-1]["sneak"])

                # Merged window/act call took both whitelisted items in 1 call
                act_calls = [p for ep, p in sim.write_calls if ep == "window/act"]
                self.assertEqual(len(act_calls), 1)
                self.assertEqual(len(act_calls[0]["ops"]), 2)

                # Dedup: re-running within 600s skips (8,64,0), owns (2,64,0), rejects (4,64,0) -> no_chest
                task2 = LootChestTask(ctx, radius=32, max_high_risk_trips=2)
                _st2, goal_st2 = _drive_task(rt, task2, max_ticks=15)
                self.assertEqual(goal_st2, "FAILED_LOCAL")
                self.assertEqual(rt.bb.last_failure_reason, "loot_chest:no_chest")

                # Add another safe chest in same Deep Dark 64m sector (outside +/-3 of TNT trap; trip 2 succeeds, trip 3 hits cap=2!)
                sim.set_block(12, 64, 0, "chest")
                sim.containers[(12, 64, 0)] = {"type": "chest", "items": {"gold_ingot": 3}}
                task3 = LootChestTask(ctx, radius=32, max_high_risk_trips=2)
                _st3, goal_st3 = _drive_task(rt, task3, max_ticks=25)
                self.assertEqual(goal_st3, "COMPLETED")
                self.assertEqual(sim.inventory_counts.get("gold_ingot"), 3)

                sim.set_block(14, 64, 0, "chest")
                sim.containers[(14, 64, 0)] = {"type": "chest", "items": {"emerald": 1}}
                task4 = LootChestTask(ctx, radius=32, max_high_risk_trips=2)
                _st4, goal_st4 = _drive_task(rt, task4, max_ticks=15)
                self.assertEqual(goal_st4, "FAILED_LOCAL")
                self.assertEqual(rt.bb.last_failure_reason, "loot_chest:high_risk_trip_cap")
                self.assertFalse(sim.sneaking)

                # stop(bb) must be pure-memory with zero writes
                writes_before = len(sim.write_calls)
                task1.stop(rt.bb)
                task4.stop(rt.bb)
                self.assertEqual(len(sim.write_calls), writes_before)
            finally:
                client.close()
                db.close()


if __name__ == "__main__":
    unittest.main()
