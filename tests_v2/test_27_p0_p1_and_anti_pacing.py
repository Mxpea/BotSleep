"""Offline regression tests for P0/P1 & Anti-Pacing Remediation Package."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import debug_workbench
from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt.blackboard import Blackboard
from v2.bt.branches.hard_safety import HardSafetyBranch
from v2.bt.goal_mount import ActiveGoalMount
from v2.bt.kernel import Node, PrioritySelector, Status
from v2.bt.observe_node import SyncBlackboardNode
from v2.bt.watchdog import LoopWatchdogNode
from v2.domain import RANGED_MOBS
from v2.gateway.multihop_navigator import MultiHopNavigator
from v2.gateway.world_proxy import VirtualWorldGateway
from v2.kernel.client import MinecraftAgentClient
from v2.kernel.journal import PendingTransactionJournal
from v2.kernel.storage import UnifiedWorldDB
from v2.planner.deps_planner import AsyncLLMReplanNode
from v2.planner.horizon_selector import HorizonSelector
from v2.planner.llm_client import LLMResponse, OpenAIBrain
from v2.planner.model_worker import SerialModelWorker, WorkerResult
from v2.primitives import PrimitiveContext
from v2.tasks.acquire import AcquireItemTask
from v2.tasks.base import Task
from v2.tasks.catalogue import TaskCatalogue
from v2.tasks.surface import ReturnToSurfaceTask


class _NeedsAITask(Task):
    def is_finished(self, bb: Blackboard) -> bool:
        return False

    def on_tick(self, bb: Blackboard) -> str:
        return "NEEDS_AI"


class _SpyStopNode(Node):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.saw_active_branch_in_stop = ""

    def update(self, bb: Blackboard) -> Status:
        return Status.RUNNING

    def stop(self, bb: Blackboard) -> None:
        self.saw_active_branch_in_stop = bb.active_branch
        super().stop(bb)


class _ToggleNode(Node):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.ret = Status.FAILURE

    def update(self, bb: Blackboard) -> Status:
        return self.ret


class TestP0P1AndAntiPacing(unittest.TestCase):
    def test_acquire_item_gathers_ingredients_before_checking_nearby_table_preventing_pingpong(self) -> None:
        # Bot at (30.5, 64, 0.5) > 16m away from table, NO iron_ingot, HAS bucket/pick ingredients missing.
        # Crafting iron_pickaxe must gather missing iron_ingot BEFORE caring about nearby crafting_table!
        sim = VirtualMcApiServer(spawn_pos=(30.5, 64.0, 0.5))
        sim.fill(-2, 63, -2, 34, 63, 2, "stone")
        sim.set_block(0, 64, 0, "crafting_table")
        sim.give("stick", 2)
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "PingPongBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "PingPongBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                bb = Blackboard()
                bb.status = client.get_status()["result"]
                bb.inventory_counts = client.get_inventory_counts()

                task = AcquireItemTask(ctx, cat, "iron_pickaxe", 1)
                with patch.object(task._place, "place_near_player", side_effect=AssertionError("Must not check/place table before ingredients!")):
                    res = task.tick(bb)
                self.assertEqual(res, "RUNNING")
                # Must descend into iron_ingot -> SmeltItemTask (not crafting_table!)
                self.assertIsNotNone(task._active_child)
                self.assertIn("iron_ingot", task._active_child.name)
            finally:
                client.close()
                db.close()

    def test_multihop_navigator_detects_ab_pacing_and_blocks_y58_bare_hand_stone(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-2, 63, -2, 16, 63, 2, "stone")
        for z in (-2, -1, 0, 1, 2):
            sim.set_block(2, 64, z, "stone")
            sim.set_block(2, 65, z, "stone")
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "NavGuardBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "NavGuardBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                nav = MultiHopNavigator(gw, journal)
                st = client.get_status()["result"]
                gw.prefetch_blocks("overworld", [(2, 64, z) for z in range(-2, 3)] + [(2, 65, z) for z in range(-2, 3)])

                # 1) Surface y=64 defaults to blocking bare-hand stone wall now (no more y=58..61 leakage!)
                out = nav.step_toward((4.5, 64.0, 0.5), st, tolerance=1.0, inventory_counts={}, prefer_3d=True)
                self.assertEqual(out.status, "BLOCKED")
                self.assertEqual([w for w in sim.write_calls if w[0] == "dig"], [])

                # 2) Simulated A<->B ping-pong (>0.35m step motion without getting closer to target 15.5,64,0.5)
                #    must still increment stall count and return BLOCKED on 6th non-progressing step!
                nav.reset_target_state("overworld", (15, 64, 0))
                with patch.object(nav, "_run_write", return_value=({"result": {}}, None)):
                    statuses = []
                    for step_i in range(8):
                        x_osc = 1.0 if step_i % 2 == 0 else 4.0
                        st_osc = {"pos": [x_osc, 64.0, 0.5], "dimension": "overworld"}
                        res = nav.step_toward((15.5, 64.0, 0.5), st_osc, tolerance=1.0, inventory_counts={"stone_pickaxe": 1})
                        statuses.append((res.status, res.reason))
                self.assertEqual(statuses[-1], ("BLOCKED", "stalled_no_displacement"))
            finally:
                client.close()
                db.close()

    def test_watchdog_detects_ab_pacing_oscillation(self) -> None:
        wd = LoopWatchdogNode(window=12, max_repeats=8, sphere_window=20, sphere_radius=1.2)
        bb = Blackboard()
        bb.active_goal_name = "acquire:iron_pickaxex1"
        bb.inventory_counts = {"stone_pickaxe": 1}
        # Oscillate between x=0.5 and x=8.5 repeatedly (wide span, high total path, small net displacement)
        pts = [(0.5, 64.0, 0.5), (4.5, 64.0, 0.5), (8.5, 64.0, 0.5), (4.5, 64.0, 0.5)]
        stalled = False
        for i in range(18):
            x, y, z = pts[i % len(pts)]
            bb.status = {"pos": [x, y, z], "dimension": "overworld"}
            wd.update(bb)
            if bb.get("watchdog_stalled"):
                stalled = True
                break
        self.assertTrue(stalled)
        self.assertIn("pacing_oscillation", str(bb.get("watchdog_reason")))

    def test_active_goal_mount_unmounts_on_needs_ai_and_selector_sets_branch_before_stop(self) -> None:
        # 1) ActiveGoalMount unmounts goal on NEEDS_AI so next tick cannot re-tick and preempt AsyncLLMReplanNode
        gm = ActiveGoalMount(_NeedsAITask("acquire:stone_pickaxex1"))
        bb = Blackboard()
        bb.status = {"pos": [0.5, 64.0, 0.5], "dimension": "overworld"}
        st = gm.update(bb)
        self.assertEqual(st, Status.FAILURE)
        self.assertIsNone(gm.goal)
        self.assertEqual(bb.active_goal_name, "")
        self.assertEqual(bb.get("last_failed_goal_name"), "acquire:stone_pickaxex1")

        # 2) PrioritySelector updates bb.active_branch BEFORE calling prev.stop(bb)
        preemptor = _ToggleNode("HardSafetyBranch")
        victim = _SpyStopNode("AsyncLLMReplanNode")
        sel = PrioritySelector("Root", [preemptor, victim])
        sel.update(bb)  # victim becomes active
        preemptor.ret = Status.RUNNING
        sel.update(bb)  # preemptor preempts victim
        self.assertEqual(victim.saw_active_branch_in_stop, "HardSafetyBranch")

        # 3) HorizonSelector demotes cooled-down repeated failures
        hs = HorizonSelector(TaskCatalogue(), objective="daily")
        bb2 = Blackboard()
        bb2.status = {"pos": [0.5, 64.0, 0.5], "dimension": "overworld", "time_of_day": 6000}
        bb2.inventory_counts = {}
        import time as _t
        bb2.set("failed_goal_cooldowns", {"acquire:wooden_pickaxex1": _t.monotonic() + 60.0}, writer="test")
        top = hs.select_top(bb2, limit=3)
        self.assertGreaterEqual(len(top), 1)
        self.assertNotEqual(f"{top[0].goal}:{top[0].item}x{top[0].count}", "acquire:wooden_pickaxex1")

    def test_surface_return_prefers_soft_direction_without_pick_and_prechecks_place_support(self) -> None:
        # 1) Soft-direction preference: South (+Z) is blocked by stone, East (+X) is dirt -> must clear dirt, NOT dig stone!
        sim = VirtualMcApiServer(spawn_pos=(0.5, 40.0, 0.5))
        sim.fill(-2, 39, -2, 3, 39, 3, "stone")
        sim.fill(-2, 43, -2, 3, 46, 3, "stone")
        # South (+Z): stone
        sim.set_block(0, 42, 0, "air")
        sim.set_block(0, 41, 1, "stone")
        sim.set_block(0, 42, 1, "stone")
        # East (+X): dirt
        sim.set_block(1, 41, 0, "dirt")
        sim.set_block(1, 42, 0, "air")
        sim.set_block(1, 40, 0, "stone")
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "SurfSoftBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "SurfSoftBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                bb = Blackboard()
                bb.status = client.get_status()["result"]
                bb.inventory_counts = {"cobblestone": 4}
                bb.set("surface_no_pick_hard_digs", 2, writer="test")  # even if hard-dig budget hit!
                task = ReturnToSurfaceTask(ctx)
                res = task.tick(bb)
                self.assertEqual(res, "RUNNING")
                digs = [w for w in sim.write_calls if w[0] == "dig"]
                self.assertTrue(any(w[1]["x"] == 1 and w[1]["y"] == 41 for w in digs))
            finally:
                client.close()
                db.close()

        # 2) Missing orthogonal support around floor_cell skips place write (zero place:no_support calls!)
        sim2 = VirtualMcApiServer(spawn_pos=(0.5, 40.0, 0.5))
        sim2.fill(-2, 39, -2, 2, 39, 2, "stone")
        sim2.fill(-2, 44, -2, 2, 46, 2, "stone")
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            sim2.set_block(dx, 40, dz, "air")
            sim2.set_block(dx, 41, dz, "air")
            sim2.set_block(dx, 42, dz, "air")
            sim2.set_block(dx, 39, dz, "air")
            sim2.set_block(dx * 2, 40, dz * 2, "air")
            sim2.set_block(dx, 40, dz + (1 if dx != 0 else 0), "air")
            sim2.set_block(dx, 40, dz - (1 if dx != 0 else 0), "air")
            sim2.set_block(dx + (1 if dz != 0 else 0), 40, dz, "air")
            sim2.set_block(dx - (1 if dz != 0 else 0), 40, dz, "air")
        sim2.give("cobblestone", 4)
        with tempfile.TemporaryDirectory() as td2, VirtualMcHttpServer(sim2) as srv2:
            db2 = UnifiedWorldDB.for_scope(td2, srv2.base_url, "NoSupBot", "w1")
            client2 = MinecraftAgentClient(srv2.base_url, "NoSupBot", "pass")
            try:
                client2.login()
                journal2 = PendingTransactionJournal(db2)
                gw2 = VirtualWorldGateway(client2, db=db2)
                ctx2 = PrimitiveContext(gw2, journal2)
                bb2 = Blackboard()
                bb2.status = client2.get_status()["result"]
                bb2.inventory_counts = client2.get_inventory_counts()
                task2 = ReturnToSurfaceTask(ctx2)
                res2 = task2.tick(bb2)
                self.assertEqual(res2, "NEEDS_AI")
                self.assertEqual([w for w in sim2.write_calls if w[0] == "place"], [])
            finally:
                client2.close()
                db2.close()

    def test_client_extract_target_coord_omits_2d_move_to_without_y(self) -> None:
        self.assertIsNone(MinecraftAgentClient._extract_target_coord("move_to", {"x": 12.5, "z": -9.2}))
        self.assertIsNone(MinecraftAgentClient._extract_target_coord("move_to", {"x": 12.5, "y": None, "z": -9.2}))
        self.assertEqual(MinecraftAgentClient._extract_target_coord("move_to", {"x": 12.5, "y": 64.0, "z": -9.2}), [12, 64, -10])
        self.assertEqual(MinecraftAgentClient._extract_target_coord("dig", {"x": 1, "y": 40, "z": 2}), [1, 40, 2])

    def test_observe_node_respawn_settle_guard_and_corpse_repeat_death_fuse(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(10.5, 64.0, 10.5))
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "RespawnBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "RespawnBot", "pass")
            try:
                client.login()
                gw = VirtualWorldGateway(client, db=db)
                obs = SyncBlackboardNode(gw)
                bb = Blackboard()
                bb.set("last_alive_inv", {"counts": {"iron_pickaxe": 1}, "armor": [], "offhand": ""}, writer="test")

                # Tick 1: first death at [12, 60, 12] -> active_corpse_drop created
                with patch.object(
                    client,
                    "observe_tick_bundle",
                    return_value={
                        "status": {"pos": [12.5, 64.0, 12.5], "dimension": "overworld", "health": 20.0, "food": 20, "on_ground": True},
                        "inventory": {"items": []},
                        "entities": [],
                        "events": [{"type": "death", "dropped_items_at": [12.0, 60.0, 12.0], "cause": "drowned"}],
                        "last_event_seq": 1,
                        "has_more_events": False,
                    },
                ):
                    self.assertEqual(obs.update(bb), Status.SUCCESS)
                self.assertIsInstance(bb.get("active_corpse_drop"), dict)

                # Tick 2: floating in unloaded chunk right after respawn (on_ground=False) -> pause BT writes!
                with patch.object(
                    client,
                    "observe_tick_bundle",
                    return_value={
                        "status": {"pos": [120.5, 64.0, 120.5], "dimension": "overworld", "health": 20.0, "food": 20, "on_ground": False},
                        "inventory": {"items": []},
                        "entities": [],
                        "events": [],
                        "last_event_seq": 1,
                        "has_more_events": False,
                    },
                ):
                    self.assertEqual(obs.update(bb), Status.RUNNING)
                    self.assertEqual(bb.last_action_outcome, "respawn_world_loading")

                # Tick 3: 2nd death within 24m of previous death -> fuse clears active_corpse_drop!
                with patch.object(
                    client,
                    "observe_tick_bundle",
                    return_value={
                        "status": {"pos": [14.5, 60.0, 14.5], "dimension": "overworld", "health": 20.0, "food": 20, "on_ground": True},
                        "inventory": {"items": []},
                        "entities": [],
                        "events": [{"type": "death", "dropped_items_at": [14.0, 60.0, 14.0], "cause": "drowned"}],
                        "last_event_seq": 2,
                        "has_more_events": False,
                    },
                ):
                    obs.update(bb)
                self.assertIsNone(bb.get("active_corpse_drop"))
            finally:
                client.close()
                db.close()

    def test_drowned_in_ranged_mobs_and_hard_safety_swims_in_water(self) -> None:
        self.assertIn("drowned", RANGED_MOBS)
        sim = VirtualMcApiServer(spawn_pos=(0.5, 62.0, 0.5))
        sim.fill(-2, 60, -2, 8, 62, 8, "water")
        sim.fill(-2, 59, -2, 8, 59, 8, "sand")
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "SwimSafetyBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "SwimSafetyBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                hs = HardSafetyBranch(gw, journal, threat_radius=8.0, threat_dwell_seconds=0.0)
                bb = Blackboard()
                bb.status = {"pos": [0.5, 62.0, 0.5], "dimension": "overworld", "health": 20.0, "food": 20, "in_water": True, "submerged": False, "oxygen": 20, "time_of_day": 6000}
                bb.entities = [{"id": 77, "name": "drowned", "type": "hostile", "pos": [4.5, 58.5, 0.5]}]
                with patch.object(hs._swimmer, "swim_step", return_value=type("R", (), {"status": "PROGRESSED", "reason": "swam"})()) as mock_swim:
                    st = hs.update(bb)
                self.assertEqual(st, Status.RUNNING)
                self.assertTrue(mock_swim.called)
                self.assertIn("safety_swim_retreat:drowned", bb.last_action_outcome)
            finally:
                client.close()
                db.close()

    def test_debug_workbench_detects_ab_pacing_episodes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            run_dir = tmp / "run_pace"
            bdir = run_dir / "bot_1"
            bdir.mkdir(parents=True)
            (bdir / "v2_flight_1.jsonl").write_text(
                json.dumps({"ts_ms": 1700000000000, "event": "STARTUP", "data": {"username": "SleepBot1"}}) + "\n"
                + json.dumps({"ts_ms": 1700000001000, "event": "TICK_SUMMARY", "data": {"tick": 1, "pos": [0.5, 64.0, 0.5], "dim": "overworld", "branch": "ActiveGoalMount", "goal": "acquire:iron_pickaxex1", "out": "nav:progressed:move_to"}}) + "\n",
                encoding="utf-8",
            )
            srv_log = tmp / "server_events.jsonl"
            # Generate 50 server PLAYER_SAMPLEs oscillating back and forth between x=0.5 and x=8.5 over 25s
            pts = [0.5, 3.0, 6.0, 8.5, 6.0, 3.0]
            srv_lines = []
            for idx in range(50):
                x = pts[idx % len(pts)]
                srv_lines.append({
                    "ts_ms": 1700000000000 + idx * 500,
                    "tick": idx * 10,
                    "source": "mc_server",
                    "event": "PLAYER_SAMPLE",
                    "player": "SleepBot1",
                    "dim": "overworld",
                    "data": {"pos": [x, 64.0, 0.5], "block": [int(x), 64, 0], "hand": "air", "same_cell": False},
                })
            srv_log.write_text("\n".join(json.dumps(r) for r in srv_lines) + "\n", encoding="utf-8")

            md_out = tmp / "pace.md"
            rc = debug_workbench.main([str(run_dir), "--server-log", str(srv_log), "--md-out", str(md_out)])
            self.assertEqual(rc, 0)
            md_txt = md_out.read_text(encoding="utf-8")
            self.assertIn("PACING_OSCILLATION", md_txt)
            self.assertIn("LOCAL_CLUSTER", md_txt)


if __name__ == "__main__":
    unittest.main()
