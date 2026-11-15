"""Offline unit tests for scripts/botctl.py operator inspection CLI."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import yaml

from core.model_gate import ModelRequestGate
from core.survival_state import SurvivalJournal
from scripts import botctl


class BotCtlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config_path = self.root / "config.yaml"
        self.state_dir = self.root / "survival_state"
        self.gate_dir = self.root / "model_gate"

    def write_config(self, *, llm_enabled=True):
        data = {
            "agent": {"name": "OpsBot", "mode": "survival"},
            "mc_api": {"base_url": "http://127.0.0.1:18900/api/v1"},
            "survival": {
                "objective": "daily",
                "world_id": "ops_world",
                "state_dir": str(self.state_dir),
            },
            "llm": {
                "enabled": llm_enabled,
                "base_url": "https://llm.example.invalid/v1",
                "api_key": "ops-test-key",
                "model": "default",
            },
        }
        self.config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return str(self.config_path)

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = botctl.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_idle_runtime_is_ready_to_replace(self):
        cfg = self.write_config()
        code, out, err = self.run_cli("--config", cfg, "--gate-dir", str(self.gate_dir), "check-ready-to-replace")
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertTrue(report["ready_to_replace"])
        self.assertEqual(report["block_reasons"], [])
        self.assertFalse(report["instance"]["locked"])
        self.assertIsNone(report["journal"]["pending"])
        self.assertEqual(report["model_gate"]["state"], "idle")

    def test_pending_action_and_instance_lock_block_replacement(self):
        cfg = self.write_config(llm_enabled=False)
        journal = SurvivalJournal(
            str(self.state_dir),
            endpoint="http://127.0.0.1:18900/api/v1",
            username="OpsBot",
            world_id="ops_world",
        )
        journal.update(pending={"decision": {"name": "gather_block"}})

        with journal.instance_lock():
            code, out, _ = self.run_cli("--config", cfg, "check-ready-to-replace")
            self.assertEqual(code, 1)
            report = json.loads(out)
            self.assertFalse(report["ready_to_replace"])
            self.assertIn("instance_already_running", report["block_reasons"])
            self.assertIn("pending_game_action_requires_resume", report["block_reasons"])

        # After releasing instance lock, pending action alone still blocks replacement.
        code_after, out_after, _ = self.run_cli("--config", cfg, "check-ready-to-replace")
        self.assertEqual(code_after, 1)
        report_after = json.loads(out_after)
        self.assertEqual(report_after["block_reasons"], ["pending_game_action_requires_resume"])

    def test_uncertain_model_gate_blocks_replacement_until_acknowledged(self):
        cfg = self.write_config(llm_enabled=True)
        gate = ModelRequestGate(
            "https://llm.example.invalid/v1",
            "ops-test-key",
            directory=str(self.gate_dir),
        )
        with self.assertRaises(RuntimeError):
            with gate.request():
                raise RuntimeError("read_timeout")

        code, out, _ = self.run_cli("--config", cfg, "--gate-dir", str(self.gate_dir), "check-ready-to-replace")
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertFalse(report["ready_to_replace"])
        self.assertIn("model_gate_uncertain_requires_acknowledge_model_idle", report["block_reasons"])

        gate.acknowledge_provider_idle()
        code_ok, out_ok, _ = self.run_cli("--config", cfg, "--gate-dir", str(self.gate_dir), "check-ready-to-replace")
        self.assertEqual(code_ok, 0)
        self.assertTrue(json.loads(out_ok)["ready_to_replace"])


if __name__ == "__main__":
    unittest.main()
