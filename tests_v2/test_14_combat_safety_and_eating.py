"""Tests for combat safety: attack stop_on, weapon equip, safe emergency eating, 3-condition walled exemption, Pathfinder3D retreat, night burrow <=10 HP, and stone_sword milestone."""

from __future__ import annotations

import math
import tempfile
import unittest

from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt import BTRuntime, Blackboard, Status
from v2.gateway import VirtualWorldGateway
from v2.kernel import MinecraftAgentClient, PendingTransactionJournal, UnifiedWorldDB
from v2.planner import HorizonSelector
from v2.tasks import TaskCatalogue


class _CountingGoal:
    def __init__(self) -> None:
        self.name = "counting_goal"
        self.ticks = 0

    def tick(self, bb: Blackboard) -> str:
        self.ticks += 1
        bb.last_action_outcome = f"goal_tick_{self.ticks}"
        return "RUNNING"

    def stop(self, bb: Blackboard) -> None:
        pass


class TestCombatSafetyAndEating(unittest.TestCase):
    def test_attack_defaults_stop_on_death_and_allows_override(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-4, 63, -4, 4, 63, 4, "stone")
        sim.add_entity(10, "zombie", (2.0, 64.0, 0.5), kind="hostile", health=30.0)
        with VirtualMcHttpServer(sim) as srv:
            client = MinecraftAgentClient(srv.base_url, "AttackStopBot", "pass")
            try:
                client.login()
                client.attack(10, timeout_ms=2500, max_hits=2)
                client.attack(10, timeout_ms=2500, max_hits=1, stop_on=["hurt", "death"])
                attack_calls = [pl for ep, pl in sim.write_calls if ep == "attack"]
                self.assertEqual(len(attack_calls), 2)
                self.assertEqual(attack_calls[0]["stop_on"], ["death"])
                self.assertEqual(attack_calls[1]["stop_on"], ["hurt", "death"])
            finally:
                client.close()

    def test_equips_highest_damage_weapon_before_striking_excluding_tools(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-8, 63, -8, 8, 63, 8, "stone")
        sim.give("diamond_pickaxe", 1)
        sim.give("iron_shovel", 1)
        sim.give("wooden_sword", 1)
        sim.give("stone_sword", 1)
        sim.give("iron_axe", 1)
        sim.add_entity(20, "zombie", (2.5, 64.0, 0.5), kind="hostile", health=30.0)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "WeaponBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "WeaponBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)

                # Tick 1: equips highest-damage weapon (iron_axe: 9 dmg > stone_sword: 5, ignoring diamond_pickaxe/iron_shovel)
                rt.tick_once()
                self.assertEqual(rt.bb.last_action_outcome, "safety_equip:iron_axe")
                self.assertEqual(sim.held_item, "iron_axe")
                self.assertEqual(rt.bb.writes_this_tick, 1)

                # Tick 2: iron_axe is now held -> strikes directly without redundant equip!
                rt.tick_once()
                self.assertEqual(rt.bb.last_action_outcome, "safety_strike:zombie")
                equip_calls = [pl for ep, pl in sim.write_calls if ep == "equip"]
                self.assertEqual(len(equip_calls), 1)
                self.assertEqual(equip_calls[0], {"item": "iron_axe", "slot": "hand"})
            finally:
                client.close()
                db.close()

    def test_emergency_eating_respects_food_limit_burst_damage_and_safe_window(self) -> None:
        # Case 1: food < 18, low health, safe distance (>= 5.5m) -> eats before retreating
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-12, 63, -12, 12, 63, 12, "stone")
        sim.health = 9.0
        sim.food = 12
        sim.give("cooked_beef", 2)
        sim.add_entity(30, "zombie", (6.5, 64.0, 0.5), kind="hostile")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "EatSafeBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "EatSafeBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False, threat_radius=8.0)

                rt.tick_once()
                self.assertEqual(rt.bb.last_action_outcome, "safety_eat:cooked_beef")
                self.assertEqual(sim.food, 20)

                # Next tick: food == 20 with normal food ONLY -> must NOT eat normal food (retreats instead)!
                rt.tick_once()
                self.assertEqual(rt.bb.last_action_outcome, "safety_retreat:zombie")
                use_calls = [pl for ep, pl in sim.write_calls if ep == "use_item"]
                self.assertEqual(len(use_calls), 1)
            finally:
                client.close()
                db.close()

        # Case 2: Clear LOS enemy < 5.5m away -> NEVER freeze eating; instead defend/retreat!
        sim_close = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim_close.fill(-12, 63, -12, 12, 63, 12, "stone")
        sim_close.health = 9.0
        sim_close.food = 10
        sim_close.give("cooked_beef", 2)
        sim_close.add_entity(31, "zombie", (4.0, 64.0, 0.5), kind="hostile")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim_close) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "NoFreezeEatBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "NoFreezeEatBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False, threat_radius=8.0)

                rt.tick_once()
                self.assertNotEqual(rt.bb.last_action_outcome, "safety_eat:cooked_beef")
                self.assertEqual(rt.bb.last_action_outcome, "safety_retreat:zombie")
                self.assertEqual([ep for ep, _ in sim_close.write_calls if ep == "use_item"], [])
            finally:
                client.close()
                db.close()

        # Case 3: < 5.5m wall-occluded + recent burst damage with golden_apple at food=20 -> eats golden_apple!
        sim_wall = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim_wall.fill(-12, 63, -12, 12, 63, 12, "stone")
        sim_wall.fill(2, 64, -2, 2, 66, 2, "stone")  # Solid wall between player and zombie
        sim_wall.health = 18.0
        sim_wall.food = 20
        sim_wall.give("golden_apple", 1)
        sim_wall.give("bread", 2)
        sim_wall.add_entity(32, "zombie", (4.0, 64.0, 0.5), kind="hostile")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim_wall) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "GappleBurstBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "GappleBurstBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False, threat_radius=8.0)

                # Tick 1 establishes baseline health=18.0
                rt.tick_once()
                # Burst damage drops HP from 18 -> 12 (> 10 HP, but -6 burst!)
                sim_wall.health = 12.0
                rt.tick_once()
                self.assertEqual(rt.bb.last_action_outcome, "safety_eat:golden_apple")
            finally:
                client.close()
                db.close()

    def test_walled_threat_3_condition_exemption_and_hurt_reset(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-4, 63, -4, 8, 63, 4, "stone")
        # Enclose player in a stone room so retreat has path_not_found, with skeleton behind wall at x=5.5 (dist=5 > 3.5)
        for y in (64, 65):
            for x in range(-1, 3):
                sim.set_block(x, y, -1, "stone")
                sim.set_block(x, y, 1, "stone")
            sim.set_block(-1, y, 0, "stone")
            sim.set_block(2, y, 0, "stone")
        sim.add_entity(40, "skeleton", (5.5, 64.0, 0.5), kind="hostile")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "WalledBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "WalledBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False, threat_radius=8.0)
                goal = _CountingGoal()
                rt.mount_goal(goal)

                # First 2 ticks fail retreat -> 3-condition exemption kicks in for 15s and goal runs!
                for _ in range(5):
                    rt.tick_once()
                self.assertGreaterEqual(goal.ticks, 2)
                self.assertIn(40, rt.hard_safety._exempt_threats)

                # Hurt event clears temporary exemptions immediately!
                sim.push_event("hurt", source="skeleton", damage=3.0)
                rt.tick_once()
                self.assertEqual(rt.hard_safety._exempt_threats, {})
                self.assertEqual(rt.bb.active_branch, "HardSafetyBranch")
            finally:
                client.close()
                db.close()

    def test_dead_end_clear_los_never_exempts_strikes_or_burrows(self) -> None:
        # Case A: Dead-end + clear LOS + weapon + dist <= 3.5 -> NEVER exempts, strikes immediately!
        sim_a = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim_a.fill(-2, 60, -2, 6, 63, 2, "stone")
        # Enclosed dead-end chamber from x=0..3, z=0 with zombie inside at (3.0, 64, 0.5) (clear LOS, dist=2.5)
        for y in (64, 65):
            for x in range(-1, 5):
                sim_a.set_block(x, y, -1, "stone")
                sim_a.set_block(x, y, 1, "stone")
            sim_a.set_block(-1, y, 0, "stone")
            sim_a.set_block(4, y, 0, "stone")
        sim_a.give("iron_sword", 1)
        sim_a.held_item = "iron_sword"
        sim_a.health = 10.0  # low HP, yet dead-end with clear LOS -> must melee knockback, NEVER exempt!
        sim_a.add_entity(50, "zombie", (3.0, 64.0, 0.5), kind="hostile", health=30.0)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim_a) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "DeadEndStrikeBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "DeadEndStrikeBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False, threat_radius=8.0)

                rt.tick_once()
                self.assertEqual(rt.bb.last_action_outcome, "safety_strike:zombie")
                self.assertEqual(rt.hard_safety._exempt_threats, {})
            finally:
                client.close()
                db.close()

        # Case B: Dead-end + clear LOS + dist > 3.5 + no weapon -> digs/caps emergency sealed burrow!
        sim_b = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim_b.fill(-2, 60, -2, 7, 63, 2, "stone")
        for y in (64, 65):
            for x in range(-1, 7):
                sim_b.set_block(x, y, -1, "stone")
                sim_b.set_block(x, y, 1, "stone")
            sim_b.set_block(-1, y, 0, "stone")
            sim_b.set_block(6, y, 0, "stone")
        sim_b.give("cobblestone", 4)
        sim_b.health = 10.0
        sim_b.add_entity(51, "skeleton", (5.0, 64.0, 0.5), kind="hostile")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim_b) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "DeadEndBurrowBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "DeadEndBurrowBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False, threat_radius=8.0)

                for _ in range(5):
                    rt.tick_once()
                    if rt.bb.get("in_sealed_burrow"):
                        break
                self.assertTrue(rt.bb.get("in_sealed_burrow"))
                self.assertEqual(rt.hard_safety._exempt_threats, {})
            finally:
                client.close()
                db.close()

    def test_terrain_aware_pathfinder_retreat_and_close_knockback(self) -> None:
        # 2-block wall behind player along x=3 except opening at z=4; water along z <= -2 -> Pathfinder3D routes around!
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-12, 63, -12, 14, 63, 14, "stone")
        sim.fill(-12, 63, -12, 14, 63, -3, "water", {"level": "0"})
        # Wall directly along naive +X retreat vector at x=3, z=-2..3 (open at z >= 4)
        sim.fill(3, 64, -2, 3, 65, 3, "stone")
        sim.add_entity(60, "zombie", (-3.5, 64.0, 0.5), kind="hostile")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "TerrainRetreatBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "TerrainRetreatBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False, threat_radius=8.0)

                rt.tick_once()
                self.assertEqual(rt.bb.last_action_outcome, "safety_retreat:zombie")
                self.assertGreater(sim.pos[2], 2.0)  # deflected onto dry +Z away from water & wall
                self.assertNotEqual(sim.block_name(math.floor(sim.pos[0]), 63, math.floor(sim.pos[2])), "water")

                # Close-range (<3.0m) non-creeper with weapon while low HP (<12) -> knockback strike!
                sim.health = 9.0
                sim.give("stone_sword", 1)
                sim.held_item = "stone_sword"
                sim.entities[0]["pos"] = [sim.pos[0] - 2.0, 64.0, sim.pos[2]]
                rt.tick_once()
                self.assertEqual(rt.bb.last_action_outcome, "safety_strike:zombie")
            finally:
                client.close()
                db.close()

    def test_night_burrow_relaxed_to_hp_10_and_requires_scaffold(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-10, 60, -10, 10, 63, 10, "stone")
        sim.time_of_day = 15000
        sim.health = 10.0  # Relaxed from <= 8.0 to <= 10.0!
        sim.give("cobblestone", 1)
        sim.add_entity(70, "skeleton", (5.5, 64.0, 0.5), kind="hostile")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "RelaxedBurrowBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "RelaxedBurrowBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                for _ in range(5):
                    rt.tick_once()
                    if rt.bb.get("in_sealed_burrow"):
                        break
                self.assertTrue(rt.bb.get("in_sealed_burrow"))
            finally:
                client.close()
                db.close()

        # Without scaffold blocks, night burrow MUST NOT dig down!
        sim_no_scaf = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim_no_scaf.fill(-10, 60, -10, 10, 63, 10, "stone")
        sim_no_scaf.time_of_day = 15000
        sim_no_scaf.health = 9.0
        sim_no_scaf.add_entity(71, "skeleton", (5.5, 64.0, 0.5), kind="hostile")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim_no_scaf) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "NoScafBurrowBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "NoScafBurrowBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.tick_once()
                self.assertFalse(rt.bb.get("in_sealed_burrow"))
                self.assertEqual(rt.bb.last_action_outcome, "safety_retreat:skeleton")
            finally:
                client.close()
                db.close()

    def test_horizon_selector_stone_sword_milestone_and_portal_pinning(self) -> None:
        cat = TaskCatalogue()
        hs = HorizonSelector(cat, objective="daily")
        bb = Blackboard()
        bb.status = {"dimension": "overworld", "pos": [0.5, 64.0, 0.5]}

        # No _sword/_axe -> stone_sword defense_early candidate appears
        bb.inventory_counts = {"stone_pickaxe": 1, "furnace": 1, "cobblestone": 4, "oak_planks": 4}
        top = hs.select_top(bb, limit=6)
        sword_cands = [c for c in top if c.item == "stone_sword"]
        self.assertEqual(len(sword_cands), 1)
        self.assertEqual(sword_cands[0].priority_tag, "defense_early")

        # Once holding an axe or sword -> stone_sword omitted
        bb.inventory_counts["wooden_axe"] = 1
        top_with_axe = hs.select_top(bb, limit=6)
        self.assertNotIn("stone_sword", [c.item for c in top_with_axe])

        # Iron ready -> PORTAL_GATE_READY stays #1 even without weapon
        bb.inventory_counts = {"iron_pickaxe": 1, "oak_planks": 8}
        top_portal = hs.select_top(bb, limit=5)
        self.assertEqual(top_portal[0].goal, "enter_nether")
        self.assertEqual(top_portal[0].priority_tag, "PORTAL_GATE_READY")
        self.assertIn("stone_sword", [c.item for c in top_portal])


if __name__ == "__main__":
    unittest.main()
