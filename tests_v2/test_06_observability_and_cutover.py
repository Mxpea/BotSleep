"""Phase 6 tests: v2 observability, monitor HTTP snapshot, chat debugger, main.py & shims cutover."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import requests
import yaml

import bot_sleep
import client as root_client
import main as root_main
from tests_v2.sim.scenarios import scenario_full_nether_speedrun
from tests_v2.sim.virtual_mc_api import VirtualMcHttpServer
from v2.bt import Blackboard
from v2.kernel import MinecraftAgentClient
from v2.observability import ChatDebugger, FlightRecorder, MonitorServer, read_flight_events


class TestObservabilityAndCutover(unittest.TestCase):
    def test_root_shims_forward_cleanly_to_v2(self) -> None:
        self.assertIs(bot_sleep.main, root_main.main)
        self.assertIs(root_client.MinecraftAgentClient, MinecraftAgentClient)

    def test_main_check_config_with_example_yaml(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        rc = root_main.main(["--config", str(repo_root / "config.example.yaml"), "--check-config"])
        self.assertEqual(rc, 0)

    def test_flight_recorder_redacts_secrets_and_tailer_reads_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fr = FlightRecorder(td, enabled=True, secrets=("super-secret-pass", "sk-token-999"))
            fr.record("INFO", "test", "AUTH", {"msg": "login with super-secret-pass and sk-token-999"})
            self.assertIsNotNone(fr.log_path)
            events = read_flight_events(fr.log_path)
            self.assertEqual(len(events), 1)
            msg = events[0]["data"]["msg"]
            self.assertNotIn("super-secret-pass", msg)
            self.assertNotIn("sk-token-999", msg)
            self.assertIn("[REDACTED]", msg)

    def test_monitor_server_exposes_readonly_snapshot(self) -> None:
        mon = MonitorServer(host="127.0.0.1", port=0).start()
        try:
            mon.update_snapshot(
                bt={"active_branch": "ActiveGoalMount", "active_goal": "enter_nether"},
                gateway={"cache_hit_ratio": 0.875, "cache_hits": 70, "cache_misses": 10},
                llm={"confirmed_model": "qwen3.6-35b-a3b", "prompt_tokens": 2000, "completion_tokens": 100, "ratio": 20.0},
            )
            r = requests.get(f"{mon.address}/api/snapshot", timeout=5)
            self.assertEqual(r.status_code, 200)
            snap = r.json()
            self.assertEqual(snap["engine"], "v2")
            self.assertEqual(snap["bt"]["active_branch"], "ActiveGoalMount")
            self.assertEqual(snap["gateway"]["cache_hit_ratio"], 0.875)
            self.assertEqual(snap["llm"]["ratio"], 20.0)
        finally:
            mon.stop()

    def test_chat_debugger_handles_allowlisted_stop_and_resume(self) -> None:
        bb = Blackboard()
        bb.recent_events = [
            {"type": "chat", "username": "Stranger", "message": "#stop"},
            {"type": "chat", "username": "Admin", "message": "#stop"},
        ]
        dbg = ChatDebugger(allowed_usernames=["Admin"])
        handled = dbg.process_events(bb)
        self.assertEqual(handled, ["stop"])
        self.assertTrue(bb.paused)

        bb.recent_events = [{"type": "chat", "username": "Admin", "message": "#resume"}]
        handled2 = dbg.process_events(bb)
        self.assertEqual(handled2, ["resume"])
        self.assertFalse(bb.paused)

    def test_main_end_to_end_survival_once_against_virtual_http_server(self) -> None:
        sim = scenario_full_nether_speedrun()
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            cfg_path = Path(td) / "test_cfg.yaml"
            cfg_path.write_text(
                yaml.safe_dump({
                    "mc_api": {"base_url": srv.base_url, "timeout_seconds": 5, "login_timeout_seconds": 5},
                    "agent": {"name": "CutoverBot", "password": "pass", "mode": "survival"},
                    "survival": {"objective": "daily", "state_dir": str(Path(td) / "state"), "poll_interval_seconds": 0.05},
                    "debug": {"flight_recorder": {"enabled": True, "output_dir": str(Path(td) / "logs")}},
                }),
                encoding="utf-8",
            )
            rc = root_main.main(["--config", str(cfg_path), "--max-steps", "5"])
            self.assertEqual(rc, 0)
            # Bot actually progressed in survival (mined oak_log / crafted planks!)
            self.assertGreater(len(sim.write_calls), 0)


if __name__ == "__main__":
    unittest.main()
