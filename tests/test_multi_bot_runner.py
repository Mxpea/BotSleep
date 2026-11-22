import tempfile
import unittest
from pathlib import Path

import yaml

from scripts import multi_bot_runner


class MultiBotRunnerTests(unittest.TestCase):
    def test_build_bot_config_shares_single_api_port_with_isolated_state_and_monitors(self):
        base_cfg = {
            "agent": {"name": "SleepBot", "password": "pw", "mode": "survival"},
            "mc_api": {"base_url": "http://127.0.0.1:18999/api/v1"},
            "survival": {"objective": "daily", "state_dir": "runtime/survival", "world_id": "default"},
            "debug": {"flight_recorder": {"output_dir": "logs"}, "monitor": {"port": 18930, "console": True}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api_urls = set()
            monitor_ports = set()
            world_ids = set()
            state_dirs = set()
            for idx in range(1, 10):
                cfg, state_dir, log_dir = multi_bot_runner.build_bot_config(
                    base_cfg,
                    index=idx,
                    run_id="run_test",
                    api_base_url="http://127.0.0.1:18900/api/v1",
                    runtime_root=root / "runtime",
                    log_root=root / "logs",
                    max_runtime_seconds=2400,
                    max_steps=768,
                )
                self.assertEqual(cfg["agent"]["name"], f"SleepBot{idx}")
                api_urls.add(cfg["mc_api"]["base_url"])
                monitor_ports.add(cfg["debug"]["monitor"]["port"])
                world_ids.add(cfg["survival"]["world_id"])
                state_dirs.add(str(state_dir))
                self.assertFalse(cfg["debug"]["monitor"]["console"])
                self.assertTrue(str(log_dir).endswith(f"bot_{idx}"))
            self.assertEqual(api_urls, {"http://127.0.0.1:18900/api/v1"})
            self.assertEqual(len(monitor_ports), 9)
            self.assertEqual(len(world_ids), 9)
            self.assertEqual(len(state_dirs), 9)

    def test_spread_grid_and_min_distance(self):
        anchors = multi_bot_runner.build_spread_grid(9)
        self.assertEqual(len(anchors), 9)
        positions = {f"Bot{i}": [float(x), 64.0, float(z)] for i, (x, z) in enumerate(anchors, 1)}
        self.assertGreaterEqual(multi_bot_runner.compute_min_horizontal_distance(positions), 2000.0)

    def test_dry_run_writes_configs_for_single_api_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            tpl = Path(tmp) / "tpl.yaml"
            tpl.write_text(yaml.safe_dump({
                "agent": {"name": "SleepBot", "mode": "survival"},
                "mc_api": {"base_url": "http://127.0.0.1:18900/api/v1"},
                "survival": {"objective": "daily"},
                "debug": {"flight_recorder": {}, "monitor": {}},
            }), encoding="utf-8")
            rc = multi_bot_runner.main([
                "--config", str(tpl),
                "--count", "3",
                "--run-id", "unit_dry_test",
                "--api-url", "http://127.0.0.1:18900/api/v1",
                "--dry-run",
            ])
            self.assertEqual(rc, 0)
            cfg_dir = multi_bot_runner.ROOT / "runtime" / "unit_dry_test" / "configs"
            try:
                written = [yaml.safe_load((cfg_dir / f"bot_{i}.yaml").read_text(encoding="utf-8")) for i in (1, 2, 3)]
                self.assertEqual({c["mc_api"]["base_url"] for c in written}, {"http://127.0.0.1:18900/api/v1"})
            finally:
                import shutil
                shutil.rmtree(multi_bot_runner.ROOT / "runtime" / "unit_dry_test", ignore_errors=True)
                shutil.rmtree(multi_bot_runner.ROOT / "logs" / "unit_dry_test", ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
