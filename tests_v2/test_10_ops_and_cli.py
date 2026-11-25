"""Phase 6 cutover verification: ops CLIs, multi-bot runner, ninjabrain integration, and AST legacy-import guard."""

from __future__ import annotations

import ast
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import yaml

from integrations.ninjabrain import __main__ as ninjabrain_cli
from integrations.ninjabrain import recovery as eye_recovery
from scripts import botctl, multi_bot_runner
from v2.kernel.journal import PendingTransactionJournal
from v2.kernel.storage import UnifiedWorldDB

REPO_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_TOP_MODULES = frozenset({"core", "agent", "skills", "debug"})


def _write_temp_config(directory: Path, example_path: Path) -> Path:
    cfg = yaml.safe_load(example_path.read_text(encoding="utf-8"))
    cfg["survival"]["state_dir"] = str(directory / "state")
    cfg["survival"]["world_id"] = "test_world"
    cfg["llm"]["enabled"] = False
    out_path = directory / "config.yaml"
    out_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return out_path


class TestOpsAndCliCutover(unittest.TestCase):
    def test_zero_legacy_imports_outside_legacy_dir(self) -> None:
        targets = [
            REPO_ROOT / "main.py",
            REPO_ROOT / "bot_sleep.py",
            REPO_ROOT / "client.py",
        ]
        for folder in ("v2", "scripts", "integrations"):
            targets.extend(sorted((REPO_ROOT / folder).rglob("*.py")))

        violations = []
        for py_file in targets:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        if top in FORBIDDEN_TOP_MODULES:
                            violations.append(f"{py_file.relative_to(REPO_ROOT)}:{node.lineno} import {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    top = node.module.split(".")[0]
                    if top in FORBIDDEN_TOP_MODULES:
                        violations.append(f"{py_file.relative_to(REPO_ROOT)}:{node.lineno} from {node.module}")
                    elif node.module.startswith("legacy.") and py_file.relative_to(REPO_ROOT).as_posix() != "scripts/botctl.py":
                        violations.append(f"{py_file.relative_to(REPO_ROOT)}:{node.lineno} from {node.module}")
        self.assertEqual(violations, [], f"Unexpected legacy imports found: {violations}")

    def test_botctl_status_and_check_ready_to_replace(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg_path = _write_temp_config(tmp, REPO_ROOT / "config.example.yaml")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = botctl.main(["--config", str(cfg_path), "--gate-dir", str(tmp / "gate"), "check-ready-to-replace"])
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertTrue(payload["ready_to_replace"])
            self.assertEqual(payload["block_reasons"], [])

            # Inject an unresolved unknown transaction -> check-ready-to-replace must block cleanly with exit code 1
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
            db = UnifiedWorldDB.for_scope(
                cfg["survival"]["state_dir"],
                cfg["mc_api"]["base_url"],
                cfg["agent"]["name"],
                cfg["survival"]["world_id"],
            )
            try:
                journal = PendingTransactionJournal(db)
                tx_id = journal.begin_write("place", {"item": "cobblestone"}, dimension="overworld", pos=(0.5, 64.0, 0.5))
                journal.resolve_write(tx_id, "unknown", details={"code": "timeout"})
            finally:
                db.close()

            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                rc2 = botctl.main(["--config", str(cfg_path), "--gate-dir", str(tmp / "gate"), "check-ready-to-replace"])
            self.assertEqual(rc2, 1)
            payload2 = json.loads(buf2.getvalue())
            self.assertFalse(payload2["ready_to_replace"])
            self.assertIn("pending_game_action_requires_resume", payload2["block_reasons"])

    def test_multi_bot_runner_and_ninjabrain_decoupled_helpers(self) -> None:
        self.assertTrue(hasattr(ninjabrain_cli, "main"))
        anchors = multi_bot_runner.build_spread_grid(9)
        self.assertEqual(len(anchors), 9)
        self.assertGreaterEqual(
            multi_bot_runner.compute_min_horizontal_distance({"b1": [anchors[0][0], 64, anchors[0][1]], "b2": [anchors[1][0], 64, anchors[1][1]]}),
            2000.0,
        )
        base_cfg = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as td:
            bot_cfg, state_dir, _ = multi_bot_runner.build_bot_config(
                base_cfg,
                index=2,
                run_id="r1",
                api_base_url="http://127.0.0.1:18900/api/v1",
                runtime_root=Path(td) / "rt",
                log_root=Path(td) / "logs",
            )
            self.assertEqual(bot_cfg["agent"]["name"], "SleepBot2")
            self.assertEqual(bot_cfg["survival"]["world_id"], "r1_bot_2")
            self.assertTrue(str(state_dir).endswith("bot_2"))

        drops = eye_recovery.eye_drops([
            {"id": 7, "name": "item", "item": "ender_eyex1", "pos": [1.5, 64.0, -2.5]},
            {"id": 8, "name": "zombie", "pos": [2.0, 64.0, 2.0]},
        ])
        self.assertEqual(drops, {7: [1.5, 64.0, -2.5]})
        res = eye_recovery.recover_eye(None, None, {"pos": [0.0, 64.0, 0.0]}, 1, None, {}, {"eye_of_ender"})
        self.assertEqual(res, {"status": "flight_position_unknown"})
