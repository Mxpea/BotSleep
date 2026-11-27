"""Offline tests for Block Spatial Heatmap, Code Execution Hotspots, target coords, and CLI visualizer."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts import analyze_heatmap
from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt.blackboard import Blackboard
from v2.kernel.client import MinecraftAgentClient
from v2.observability import FlightRecorder, HeatmapTracker, describe_task_chain, normalize_fail_reason
from v2.tasks.base import Task


class _DummyChildTask(Task):
    def is_finished(self, bb: Blackboard) -> bool:
        return False

    def on_tick(self, bb: Blackboard) -> str:
        return "RUNNING"


class _DummyParentTask(Task):
    def is_finished(self, bb: Blackboard) -> bool:
        return False

    def on_tick(self, bb: Blackboard) -> str:
        return self.run_child(bb, _DummyChildTask("mine_stone"))


class TestHeatmapAndCodeHotspots(unittest.TestCase):
    def test_heatmap_tracker_records_dwell_stuck_dig_place_and_code_cross_index(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fr = FlightRecorder(td, enabled=True)
            hm = HeatmapTracker(td, username="HeatmapBot", enabled=True, flush_every_ticks=2)
            fr.add_listener(hm.on_flight_event)

            parent = _DummyParentTask("acquire:stone_pickaxe")
            child = _DummyChildTask("return_to_surface")
            parent._active_child = child

            self.assertEqual(
                describe_task_chain(parent),
                "_DummyParentTask(acquire:stone_pickaxe)>_DummyChildTask(return_to_surface)",
            )
            self.assertEqual(
                normalize_fail_reason("acquire:stone_pickaxe@(2111, 31, -1881):nav:blocked"),
                "acquire:stone_pickaxe@(pos):nav:blocked",
            )

            bb = Blackboard()
            bb.status = {"pos": [10.4, 31.0, -5.8], "dimension": "overworld"}
            bb.active_branch = "ActiveGoalMount"
            bb.last_action_outcome = "nav:blocked:pathfinder_3d"
            bb.last_failure_reason = "acquire:stone_pickaxe@(10, 31, -6):surface_escape:no_pickaxe"

            # Tick 0: dwell=1, stuck=0
            hm.record_tick(0, bb, parent, 4.2)
            # Tick 1 at same cell (10, 31, -6): dwell=2, stuck=1
            bb.status = {"pos": [10.8, 31.2, -5.2], "dimension": "overworld"}
            hm.record_tick(1, bb, parent, 6.8)

            fr.record("INFO", "mc_api", "API_TIMING", {
                "endpoint": "dig", "method": "POST", "duration_ms": 6400, "ok": True, "target": [11, 31, -6],
            })
            fr.record("INFO", "mc_api", "API_TIMING", {
                "endpoint": "place", "method": "POST", "duration_ms": 120, "ok": False, "target": [12, 31, -6],
            })
            fr.record("WARN", "mc_api", "API_ERROR", {
                "endpoint": "place", "method": "POST", "code": "no_support", "message": "no support",
            })

            out_path = hm.flush()
            self.assertIsNotNone(out_path)
            assert out_path is not None
            data = json.loads(out_path.read_text(encoding="utf-8"))

            self.assertEqual(data["total_ticks"], 2)
            self.assertEqual(data["total_stuck_ticks"], 1)

            stand_blk = data["blocks"]["overworld:10:31:-6"]
            self.assertEqual(stand_blk["dwell"], 2)
            self.assertEqual(stand_blk["stuck"], 1)

            dig_blk = data["blocks"]["overworld:11:31:-6"]
            self.assertEqual(dig_blk["dig_ok"], 1)
            self.assertEqual(dig_blk["dig_ms"], 6400)
            self.assertTrue(len(dig_blk["codes"]) >= 1)

            place_blk = data["blocks"]["overworld:12:31:-6"]
            self.assertEqual(place_blk["place_fail"], 1)

            self.assertEqual(data["api_hotspots"]["dig"]["slow_ge6s"], 1)
            self.assertEqual(data["api_hotspots"]["place"]["err_codes"]["no_support"], 1)
            self.assertEqual(data["y_profile"]["31"], {"dwell": 2, "stuck": 1, "dig": 1})

            ckey = next(iter(data["code_hotspots"]))
            ch = data["code_hotspots"][ckey]
            self.assertEqual(ch["stuck_ticks"], 1)
            self.assertEqual(ch["fail"], "acquire:stone_pickaxe@(pos):surface_escape:no_pickaxe")
            self.assertIn("overworld:10:31:-6", ch["blocks"])
            self.assertIn("overworld:11:31:-6", ch["blocks"])

    def test_client_request_records_target_coords_on_spatial_writes(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-2, 63, -2, 6, 63, 2, "stone")
        sim.set_block(1, 64, 0, "dirt")
        sim.give("cobblestone", 8)

        events = []
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            fr = FlightRecorder(td, enabled=True)
            fr.add_listener(events.append)
            client = MinecraftAgentClient(srv.base_url, "TargetBot", "pass", recorder=fr)
            try:
                client.login()
                client.dig([1, 64, 0], collect=True)
                client.place_block("cobblestone", 1, 64, 0)
                client.move_to(2.7, 64.0, 0.2)
            finally:
                client.close()

        targets = {
            e["data"]["endpoint"]: e["data"].get("target")
            for e in events
            if e.get("event") == "API_TIMING" and e.get("data", {}).get("endpoint") in {"dig", "place", "move_to"}
        }
        self.assertEqual(targets.get("dig"), [1, 64, 0])
        self.assertEqual(targets.get("place"), [1, 64, 0])
        self.assertEqual(targets.get("move_to"), [2, 64, 0])

    def test_analyze_heatmap_cli_supports_native_json_legacy_jsonl_and_compare(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cur_dir = tmp / "run_cur" / "bot_1"
            base_dir = tmp / "run_base" / "bot_1"
            cur_dir.mkdir(parents=True)
            base_dir.mkdir(parents=True)

            # Native v2_heatmap JSON in cur_dir
            hm = HeatmapTracker(cur_dir, username="Bot1", enabled=True)
            bb = Blackboard()
            bb.status = {"pos": [5.0, 31.0, 5.0], "dimension": "overworld"}
            bb.active_branch = "ActiveGoalMount"
            bb.last_action_outcome = "nav:blocked"
            bb.last_failure_reason = "surface_escape:no_pickaxe"
            hm.record_tick(0, bb, None, 3.0)
            hm.record_tick(1, bb, None, 3.0)
            hm.flush()

            # Legacy v2_flight JSONL in base_dir
            legacy_lines = [
                {"ts": 1.0, "level": "INFO", "component": "v2.app", "event": "TICK_SUMMARY", "data": {
                    "tick": 0, "pos": [5.0, 31.0, 5.0], "dim": "overworld",
                    "branch": "ActiveGoalMount", "goal": "acquire:stone_pickaxex1", "out": "nav:blocked", "fail": "surface_escape:no_pickaxe",
                }},
                {"ts": 2.0, "level": "INFO", "component": "v2.app", "event": "TICK_SUMMARY", "data": {
                    "tick": 20, "pos": [5.0, 31.0, 5.0], "dim": "overworld",
                    "branch": "ActiveGoalMount", "goal": "acquire:stone_pickaxex1", "out": "nav:blocked", "fail": "surface_escape:no_pickaxe",
                }},
                {"ts": 3.0, "level": "INFO", "component": "mc_api", "event": "API_TIMING", "data": {
                    "endpoint": "dig", "duration_ms": 6500, "ok": True,
                }},
            ]
            (base_dir / "v2_flight_legacy.jsonl").write_text(
                "\n".join(json.dumps(x) for x in legacy_lines) + "\n", encoding="utf-8"
            )

            html_out = tmp / "out" / "compare.html"
            md_out = tmp / "out" / "compare.md"
            rc = analyze_heatmap.main([
                str(tmp / "run_cur"),
                "--compare", str(tmp / "run_base"),
                "--html-out", str(html_out),
                "--md-out", str(md_out),
                "--top", "10",
            ])
            self.assertEqual(rc, 0)
            html_txt = html_out.read_text(encoding="utf-8")
            md_txt = md_out.read_text(encoding="utf-8")
            self.assertIn("<canvas id=\"hmCanvas\"", html_txt)
            self.assertIn("run_cur", html_txt)
            self.assertIn("run_base", md_txt)
            self.assertIn("surface_escape:no_pickaxe", md_txt)


if __name__ == "__main__":
    unittest.main()
