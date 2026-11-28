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
from v2.gateway import SurfaceSwimExecutor, VirtualWorldGateway
from v2.kernel import APIError, MinecraftAgentClient, PendingTransactionJournal, UnifiedWorldDB
from v2.kernel.config import DEFAULTS
from v2.primitives import PrimitiveContext
from v2.tasks import AcquireItemTask, TaskCatalogue


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


if __name__ == "__main__":
    unittest.main()
