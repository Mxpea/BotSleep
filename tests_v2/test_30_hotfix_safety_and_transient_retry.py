"""Regression tests for hotfix: bounded HardSafety retreat probes and transient tick APIError retry."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.app import run_v2_session
from v2.bt import BTRuntime
from v2.bt.blackboard import Blackboard
from v2.bt.branches.local_recovery import LocalRecoveryBranch
from v2.gateway import MultiHopNavigator, SurfaceSwimExecutor, VirtualWorldGateway
from v2.kernel import APIError, MinecraftAgentClient, PendingTransactionJournal, UnifiedWorldDB
from v2.kernel.config import DEFAULTS
from v2.planner.deps_planner import DepsPlanner
from v2.planner.horizon_selector import HorizonSelector
from v2.planner.intent_compiler import IntentCompiler
from v2.planner.llm_client import LLMResponse, LLMToolCall
from v2.primitives import PrimitiveContext
from v2.tasks import AcquireItemTask, MineResourceTask, SmeltItemTask, TaskCatalogue
from v2.tasks.competition import CompetitionManager


class TestHotfixSafetyAndTransientRetry(unittest.TestCase):
    def test_hard_safety_retreat_bounded_probes_and_lateral_deflection(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-12, 63, -14, 0, 63, 14, "stone")
        sim.fill(1, 63, -14, 12, 63, 14, "water", {"level": "0"})
        sim.add_entity(101, "zombie", (-2.5, 64.0, 0.5), kind="hostile")

        get_block_calls = 0
        orig_handle = sim.handle

        def counting_handle(method: str, ep: str, payload=None, params=None):
            nonlocal get_block_calls
            if ep == "get_block":
                get_block_calls += 1
            return orig_handle(method, ep, payload=payload, params=params)

        sim.handle = counting_handle  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "FastRetreatBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "FastRetreatBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False, threat_radius=8.0)

                rt.tick_once()
                self.assertEqual(rt.bb.active_branch, "HardSafetyBranch")
                self.assertEqual(rt.bb.last_action_outcome, "safety_retreat:zombie")
                self.assertLessEqual(sim.pos[0], 0.5)
                self.assertGreaterEqual(abs(sim.pos[2] - 0.5), 4.0)
                self.assertLessEqual(get_block_calls, 75)
                move_calls = [pl for ep, pl in sim.write_calls if ep == "move_to"]
                self.assertTrue(move_calls)
                self.assertEqual(move_calls[-1].get("stop_on"), [])

                swim = SurfaceSwimExecutor(client, journal, pulse_ms=300)
                st_swim = {"pos": [0.5, 62.0, 0.5], "dimension": "overworld", "oxygen": 20, "submerged": False}
                swim.swim_step((2.5, 62.0, 0.5), st_swim)
                ctrl_calls = [pl for ep, pl in sim.write_calls if ep == "control"]
                self.assertTrue(ctrl_calls)
                self.assertEqual(ctrl_calls[-1].get("stop_on"), [])
            finally:
                client.close()
                db.close()

    def test_walled_threat_exempts_after_single_fail_for_45s_and_resets_on_hurt(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-2, 60, -2, 6, 63, 2, "stone")
        for y in (64, 65):
            for x in range(-1, 3):
                sim.set_block(x, y, -1, "stone")
                sim.set_block(x, y, 1, "stone")
            sim.set_block(-1, y, 0, "stone")
            sim.set_block(2, y, 0, "stone")
        sim.add_entity(41, "creeper", (5.5, 64.0, 0.5), kind="hostile")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "Wall1FailBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "Wall1FailBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False, threat_radius=8.0)

                now_before = time.monotonic()
                rt.tick_once()
                self.assertIn(41, rt.hard_safety._exempt_threats)
                self.assertGreater(rt.hard_safety._exempt_threats[41], now_before + 30.0)

                sim.push_event("hurt", source="creeper", damage=2.0)
                rt.tick_once()
                self.assertEqual(rt.hard_safety._exempt_threats, {})
            finally:
                client.close()
                db.close()

    def test_acquire_ore_skips_water_lava_adjacent_and_falls_back_on_stall(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 40.0, 0.5))
        sim.fill(-4, 39, -4, 6, 39, 4, "stone")
        # Nearest iron_ore at (2, 41, 0) touches water; second at (3, 41, 0) touches lava; dry iron_ore at (4, 41, 0)
        sim.set_block(2, 41, 0, "iron_ore")
        sim.set_block(2, 41, 1, "water", {"level": "0"})
        sim.set_block(3, 41, 0, "iron_ore")
        sim.set_block(3, 41, -1, "lava", {"level": "0"})
        sim.set_block(4, 41, 0, "iron_ore")
        sim.give("stone_pickaxe", 1)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "DryOreBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "DryOreBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.mount_goal(AcquireItemTask(ctx, cat, "raw_iron", 1))

                for _ in range(6):
                    rt.tick_once()
                    if sim.inventory_counts.get("raw_iron", 0) >= 1:
                        break
                self.assertGreaterEqual(sim.inventory_counts.get("raw_iron", 0), 1)
                self.assertEqual(sim.block_name(2, 41, 0), "iron_ore")
                self.assertEqual(sim.block_name(3, 41, 0), "iron_ore")
                self.assertNotEqual(sim.block_name(4, 41, 0), "iron_ore")
            finally:
                client.close()
                db.close()

        # Verify stalled_no_displacement escalates to MineResourceTask instead of failing immediately
        sim2 = VirtualMcApiServer(spawn_pos=(0.5, 40.0, 0.5))
        sim2.fill(-4, 39, -4, 14, 39, 4, "stone")
        sim2.set_block(10, 41, 0, "iron_ore")
        sim2.give("stone_pickaxe", 1)
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim2) as srv2:
            db2 = UnifiedWorldDB.for_scope(td, srv2.base_url, "StallMineBot", "w1")
            client2 = MinecraftAgentClient(srv2.base_url, "StallMineBot", "pass")
            try:
                client2.login()
                journal2 = PendingTransactionJournal(db2)
                gw2 = VirtualWorldGateway(client2, db=db2)
                ctx2 = PrimitiveContext(gw2, journal2)
                cat2 = TaskCatalogue()
                task = AcquireItemTask(ctx2, cat2, "raw_iron", 1)
                rt2 = BTRuntime(gw2, journal2, enable_auto_sleep=False)
                rt2.mount_goal(task)
                with mock.patch.object(
                    task._dig,
                    "dig_nearest_of_types",
                    return_value=mock.Mock(status="FAILED_LOCAL", reason="stalled_no_displacement"),
                ):
                    rt2.tick_once()
                    self.assertEqual(rt2.bb.goal_status, "RUNNING")
                    rt2.tick_once()
                    self.assertIsNotNone(task._active_child)
            finally:
                client2.close()
                db2.close()

    def test_run_v2_session_retries_transient_not_ready_and_never_swallows_unknown(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-6, 63, -6, 6, 63, 6, "stone")

        orig_handle = sim.handle
        injected = {"count": 0}

        def flaky_handle(method: str, ep: str, payload=None, params=None):
            if ep == "get_block" and injected["count"] == 0:
                injected["count"] += 1
                return {
                    "ok": False,
                    "error": {"code": "not_ready", "message": "world is still loading; retry in a moment"},
                }
            return orig_handle(method, ep, payload=payload, params=params)

        sim.handle = flaky_handle  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            cfg = copy.deepcopy(DEFAULTS)
            cfg["mc_api"]["base_url"] = srv.base_url
            cfg["agent"]["name"] = "RetryBot"
            cfg["agent"]["password"] = "pass"
            cfg["agent"]["mode"] = "survival"
            cfg["survival"]["objective"] = "starter"
            cfg["survival"]["world_id"] = "retry_w1"
            cfg["survival"]["state_dir"] = str(Path(td) / "runtime")
            cfg["survival"]["poll_interval_seconds"] = 0.05
            cfg["debug"]["flight_recorder"]["enabled"] = True
            cfg["debug"]["flight_recorder"]["output_dir"] = str(Path(td) / "logs")
            cfg["debug"]["monitor"]["enabled"] = False
            cfg["llm"]["enabled"] = False

            with mock.patch("v2.app.time.sleep"):
                rc = run_v2_session(cfg, once=False, max_ticks_override=2)
            self.assertEqual(rc, 0)
            self.assertEqual(injected["count"], 1)

            flight_files = list((Path(td) / "logs").glob("v2_flight_*.jsonl"))
            self.assertTrue(flight_files)
            events = [json.loads(ln).get("event") for ln in flight_files[0].read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertIn("TRANSIENT_TICK_RETRY", events)

        # Verify action_state == "unknown" is NEVER swallowed as a transient retry
        sim_unk = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim_unk.fill(-6, 63, -6, 6, 63, 6, "stone")
        with tempfile.TemporaryDirectory() as td2, VirtualMcHttpServer(sim_unk) as srv2:
            cfg2 = copy.deepcopy(DEFAULTS)
            cfg2["mc_api"]["base_url"] = srv2.base_url
            cfg2["agent"]["name"] = "UnknownBot"
            cfg2["agent"]["password"] = "pass"
            cfg2["agent"]["mode"] = "survival"
            cfg2["survival"]["objective"] = "starter"
            cfg2["survival"]["world_id"] = "unk_w1"
            cfg2["survival"]["state_dir"] = str(Path(td2) / "runtime")
            cfg2["debug"]["flight_recorder"]["enabled"] = False
            cfg2["debug"]["monitor"]["enabled"] = False
            cfg2["llm"]["enabled"] = False

            with mock.patch("v2.bt.runtime.BTRuntime.tick_once") as tick_mock:
                tick_mock.side_effect = APIError(
                    "request_timeout",
                    "write timed out",
                    details={"action_state": "unknown"},
                )
                rc_unk = run_v2_session(cfg2, once=False, max_ticks_override=2)
            self.assertEqual(rc_unk, 1)
            self.assertEqual(tick_mock.call_count, 1)

    def test_mine_resource_conservative_enclosed_avoids_cave_protects_tread_and_places_torch(self) -> None:
        # 1) Enclosed tunnel prefers solid rock (+Z/-Z) over open cave (+X, even with ore inside) and wet adjacent ore
        sim = VirtualMcApiServer(spawn_pos=(0.5, 16.0, 0.5))
        sim.fill(-4, 14, -4, 12, 20, 4, "stone")
        sim.set_block(0, 16, 0, "air")
        sim.set_block(0, 17, 0, "air")
        # Open natural cave ahead in +X with exposed iron_ore beyond CLOSE_ORE_RADIUS (x=10)
        sim.fill(2, 15, -2, 10, 19, 2, "air")
        sim.set_block(10, 16, 0, "iron_ore")
        # Close ore touching water at (1, 16, 0) must be skipped (allow_shallow_floor_water=False)
        sim.set_block(1, 16, 0, "iron_ore")
        sim.set_block(1, 16, 1, "water", {"level": "0"})
        sim.give("iron_pickaxe", 1)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "ConservativeMineBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "ConservativeMineBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                task = MineResourceTask(ctx, cat, "raw_iron", 1, target_y=16)
                rt.mount_goal(task)

                rt.tick_once()
                digs = [(p["x"], p["y"], p["z"]) for ep, p in sim.write_calls if ep == "dig"]
                self.assertEqual(len(digs), 1)
                self.assertNotEqual(digs[0], (1, 16, 0))
                self.assertNotEqual(digs[0][0], 1)

                # 2) Stepping forward/down records previous underfoot cell as protected return tread
                bb = Blackboard()
                bb.begin_tick()
                bb.status = {"pos": [0.5, 20.0, 0.5], "dimension": "overworld"}
                task._record_step_and_protect(0, 20, 0)
                bb.status = {"pos": [1.5, 19.0, 0.5], "dimension": "overworld"}
                task._record_step_and_protect(1, 19, 0)
                self.assertIn((0, 19, 0), task._protected_treads)
                self.assertIn((1, 18, 0), task._protected_treads)
                sim.set_block(0, 19, 0, "stone")
                sim.set_block(0, 20, 0, "air")
                gw.invalidate_unknown_radius("overworld", (0, 19, 0), radius=1)
                self.assertFalse(task._is_safe_to_dig(bb, (0, 19, 0)))
            finally:
                client.close()
                db.close()

        # 3) Conditional torch placement behind player after TORCH_INTERVAL_STEPS underground
        sim_t = VirtualMcApiServer(spawn_pos=(1.5, 16.0, 0.5))
        sim_t.fill(-3, 14, -3, 5, 19, 3, "stone")
        sim_t.set_block(0, 16, 0, "air")
        sim_t.set_block(0, 17, 0, "air")
        sim_t.set_block(1, 16, 0, "air")
        sim_t.set_block(1, 17, 0, "air")
        sim_t.give("iron_pickaxe", 1)
        sim_t.give("torch", 4)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim_t) as srv_t:
            db_t = UnifiedWorldDB.for_scope(td, srv_t.base_url, "TorchBot", "w1")
            client_t = MinecraftAgentClient(srv_t.base_url, "TorchBot", "pass")
            try:
                client_t.login()
                journal_t = PendingTransactionJournal(db_t)
                gw_t = VirtualWorldGateway(client_t, db=db_t)
                ctx_t = PrimitiveContext(gw_t, journal_t)
                cat_t = TaskCatalogue()
                task_t = MineResourceTask(ctx_t, cat_t, "raw_iron", 1, target_y=16)
                task_t._last_pos_cell = (1, 16, 0)
                task_t._last_step_cell = (0, 16, 0)
                task_t._steps_since_torch = MineResourceTask.TORCH_INTERVAL_STEPS
                rt_t = BTRuntime(gw_t, journal_t, enable_auto_sleep=False)
                rt_t.mount_goal(task_t)

                rt_t.tick_once()
                self.assertEqual(sim_t.block_name(0, 16, 0), "torch")
                self.assertEqual(task_t._steps_since_torch, 0)
            finally:
                client_t.close()
                db_t.close()

    def test_competition_idle_update_progress_and_empty_tool_calls_auto_select_top_candidate(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-4, 63, -4, 4, 63, 4, "grass_block")
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "IdleFallbackBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "IdleFallbackBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()

                # 1) Calling update_progress when idle auto-selects top competition candidate
                comp1 = CompetitionManager(cat)
                compiler1 = IntentCompiler(ctx, cat, objective="competition", competition=comp1)
                horizon1 = HorizonSelector(cat, objective="competition", competition=comp1)
                planner1 = DepsPlanner(None, compiler1, horizon1)
                bb = Blackboard()
                bb.status = {"dimension": "overworld", "pos": [0.5, 64.0, 0.5], "health": 20.0, "food": 20, "oxygen": 20}
                bb.inventory_counts = {}

                resp_note = LLMResponse(
                    requested_model="default",
                    confirmed_model="qwen3.6-35b-a3b",
                    content="",
                    tool_calls=[LLMToolCall("c1", "update_progress", {"note": "need wood first"})],
                )
                task1, res1, _ = planner1.apply_llm_response(resp_note, bb)
                self.assertIsNotNone(task1)
                self.assertIsNotNone(comp1.active_task_id)
                self.assertEqual(res1.get("auto_selected_from"), "idle_update_progress")

                # 2) Empty tool_calls when idle also auto-selects top competition candidate
                comp2 = CompetitionManager(cat)
                compiler2 = IntentCompiler(ctx, cat, objective="competition", competition=comp2)
                horizon2 = HorizonSelector(cat, objective="competition", competition=comp2)
                planner2 = DepsPlanner(None, compiler2, horizon2)
                resp_empty = LLMResponse(
                    requested_model="default",
                    confirmed_model="qwen3.6-35b-a3b",
                    content="thinking about wood",
                    tool_calls=[],
                )
                task2, res2, _ = planner2.apply_llm_response(resp_empty, bb)
                self.assertIsNotNone(task2)
                self.assertIsNotNone(comp2.active_task_id)
                self.assertEqual(res2.get("auto_selected_from"), "empty_tool_calls")

                # 3) Never bypasses PendingUnknown freeze
                comp3 = CompetitionManager(cat)
                compiler3 = IntentCompiler(ctx, cat, objective="competition", competition=comp3)
                horizon3 = HorizonSelector(cat, objective="competition", competition=comp3)
                planner3 = DepsPlanner(None, compiler3, horizon3)
                bb.set("pending_frozen", True, writer="test")
                t_frozen1, _, _ = planner3.apply_llm_response(resp_note, bb)
                t_frozen2, _, _ = planner3.apply_llm_response(resp_empty, bb)
                self.assertIsNone(t_frozen1)
                self.assertIsNone(t_frozen2)
                self.assertIsNone(comp3.active_task_id)
            finally:
                client.close()
                db.close()

    def test_water_recovery_never_digs_submerged_riverbed_and_holds_steady_shore_heading(self) -> None:
        # 1) Submerged bot beside dirt riverbed under water must surface, never dig underwater dirt
        sim = VirtualMcApiServer(spawn_pos=(0.5, 59.0, 0.5))
        sim.fill(-4, 58, -4, 4, 62, 4, "water", {"level": "0"})
        sim.set_block(1, 59, 0, "dirt")
        sim.set_block(1, 60, 0, "dirt")
        sim.set_block(1, 61, 0, "water", {"level": "0"})

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "SubmergedNoDigBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "SubmergedNoDigBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rec = LocalRecoveryBranch(gw, journal, enable_auto_sleep=False)
                bb = Blackboard()
                bb.begin_tick()
                bb.status = {
                    "dimension": "overworld",
                    "pos": [0.5, 59.0, 0.5],
                    "health": 20.0,
                    "food": 20,
                    "oxygen": 12,
                    "in_water": True,
                    "submerged": True,
                }
                rec._swim_consec = 2
                rec._swim_anchor = (0.5, 59.0, 0.5)
                self.assertIsNone(rec._try_dig_swim_ceiling(bb))
                rec.update(bb)
                self.assertEqual([ep for ep, _ in sim.write_calls if ep == "dig"], [])
                self.assertEqual([ep for ep, _ in sim.write_calls if ep == "control"], ["control"])

                # 2) Surfaced bot next to underwater dirt (with water above) skips it in both recovery & navigator
                bb.begin_tick()
                bb.status = {
                    "dimension": "overworld",
                    "pos": [0.5, 61.0, 0.5],
                    "health": 20.0,
                    "food": 20,
                    "oxygen": 20,
                    "in_water": True,
                    "submerged": False,
                }
                sim.pos = [0.5, 61.0, 0.5]
                sim.set_block(1, 62, 0, "water", {"level": "0"})
                sim.set_block(1, 61, 0, "dirt")
                gw.invalidate_unknown_radius("overworld", (1, 61, 0), radius=2)
                self.assertIsNone(rec._try_dig_swim_ceiling(bb))
                nav = MultiHopNavigator(gw, journal)
                self.assertIsNone(
                    nav._try_water_shoreline_recovery(
                        "overworld", (0.5, 61.0, 0.5), (4.5, 63.0, 0.5), bb.status, {}, None, stalls=2
                    )
                )

                # 3) Wide open water keeps a steady open-water heading across ticks instead of spinning 45 deg/tick
                bb.status["pos"] = [0.5, 62.0, 0.5]
                rec._open_water_bearing_idx = 0
                rec._open_water_bearing_steps = 0
                t1 = rec._pick_swim_target(bb)
                t2 = rec._pick_swim_target(bb)
                t3 = rec._pick_swim_target(bb)
                self.assertEqual(t1, t2)
                self.assertEqual(t2, t3)
            finally:
                client.close()
                db.close()

    def test_smelt_item_task_waits_for_slow_furnace_without_watchdog_abort(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-3, 63, -3, 3, 63, 3, "stone")
        sim.set_block(1, 64, 0, "furnace")
        sim.smelt_ticks_required = 10  # Exceeds LoopWatchdog.max_repeats=6!
        sim.give("raw_iron", 2)
        sim.give("coal", 1)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "SlowSmeltBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "SlowSmeltBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                self.assertGreaterEqual(SmeltItemTask(ctx, cat, "iron_ingot", 2).poll_interval_seconds, 3.0)

                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                rt.mount_goal(SmeltItemTask(ctx, cat, "iron_ingot", 2, poll_interval_seconds=0.0))

                for _ in range(20):
                    rt.tick_once()
                    self.assertFalse(rt.watchdog.stalled, rt.watchdog.stall_signature)
                    if sim.inventory_counts.get("iron_ingot", 0) >= 2:
                        break
                self.assertEqual(sim.inventory_counts.get("iron_ingot", 0), 2)
            finally:
                client.close()
                db.close()


if __name__ == "__main__":
    unittest.main()
