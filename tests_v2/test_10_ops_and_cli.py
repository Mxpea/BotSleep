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

    def test_rcon_manager_disconnect_and_overflow_stats_redaction(self) -> None:
        from unittest.mock import patch
        from v2.observability.logger import FlightRecorder
        from v2.kernel.model_gate import ModelGateError
        from v2.planner.llm_client import LLMResponse, OpenAIBrain

        rm = multi_bot_runner.RconManager("127.0.0.1", 25575, "pw")
        rm.disconnect()

        with tempfile.TemporaryDirectory() as td:
            fr = FlightRecorder(td, enabled=True, secrets=("prim-secret", "ov-secret-xyz"))
            fr.record("INFO", "test", "EVT", {"msg": "leaked ov-secret-xyz and prim-secret"})
            content = next(Path(td).glob("*.jsonl")).read_text(encoding="utf-8")
            self.assertNotIn("ov-secret-xyz", content)
            self.assertNotIn("prim-secret", content)

        primary = OpenAIBrain("http://127.0.0.1:9", "k1", "m1")
        overflow = OpenAIBrain("http://127.0.0.1:9", "k2", "m2")
        primary.overflow_brain = overflow
        try:
            ov_resp = LLMResponse("m2", "m2", "", [], prompt_tokens=120, completion_tokens=15, route="overflow", latency_ms=20)
            with patch.object(primary, "_call_single", side_effect=ModelGateError("llm_busy", "busy")), \
                 patch.object(overflow, "_call_single", return_value=ov_resp):
                got = primary.chat([{"role": "user", "content": "hi"}])
            self.assertEqual(got.route, "overflow")
            self.assertEqual(primary.primary_calls, 0)
            self.assertEqual(primary.overflow_calls, 1)
            self.assertEqual(primary.total_prompt_tokens, 120)
            self.assertEqual(primary.total_completion_tokens, 15)
        finally:
            primary.close()

    def test_async_replan_main_thread_compile_and_error_backoff(self) -> None:
        from unittest.mock import MagicMock
        from v2.bt.blackboard import Blackboard
        from v2.bt.goal_mount import ActiveGoalMount
        from v2.planner.deps_planner import AsyncLLMReplanNode
        from v2.planner.model_worker import WorkerResult

        class _StubWorker:
            def __init__(self) -> None:
                self.is_busy = False
                self.submitted = 0
                self.pending: list = []

            def submit(self, job_id, ctx_ver, fn) -> bool:
                self.submitted += 1
                try:
                    self.pending.append(WorkerResult(job_id, ctx_ver, True, value=fn()))
                except Exception as exc:
                    self.pending.append(WorkerResult(job_id, ctx_ver, False, error=exc))
                return True

            def poll(self, expected_context_version=None):
                return self.pending.pop(0) if self.pending else None

        planner = MagicMock()
        planner.brain = MagicMock()
        planner.build_request_payload.return_value = ([{"role": "user", "content": "hi"}], [])
        planner.brain.chat.side_effect = RuntimeError("rate_limited")

        worker = _StubWorker()
        replan = AsyncLLMReplanNode(planner, ActiveGoalMount(), worker=worker)
        bb = Blackboard()
        bb.begin_tick()
        # Tick 1 submits once (worker executes immediately and stores error result)
        replan.update(bb)
        self.assertEqual(worker.submitted, 1)
        # Tick 2 collects error & arms exponential backoff
        replan.update(bb)
        self.assertEqual(bb.get("last_llm_error"), "rate_limited")
        self.assertEqual(replan._error_streak, 1)
        # Tick 3 skips immediate resubmission during backoff window
        replan.update(bb)
        self.assertEqual(worker.submitted, 1)
        self.assertEqual(bb.last_action_outcome, "llm_error_backoff")

        # Verify competition snapshot formatting in DepsPlanner never raises KeyError
        from unittest.mock import patch
        from v2.planner.deps_planner import DepsPlanner
        from v2.planner.horizon_selector import HorizonSelector
        from v2.planner.intent_compiler import IntentCompiler
        from v2.tasks.catalogue import TaskCatalogue
        from v2.tasks.competition import CompetitionManager

        cat = TaskCatalogue()
        comp = CompetitionManager(cat)
        comp_compiler = IntentCompiler(MagicMock(), cat, objective="competition", competition=comp)
        comp_horizon = HorizonSelector(cat, objective="competition", competition=comp)
        dp = DepsPlanner(None, comp_compiler, comp_horizon)
        msgs, _tools = dp.build_request_payload(bb)
        content = msgs[-1]["content"]
        self.assertIn("comp_score=0", content)
        self.assertIn('select_task(task_id="minecraft:story/mine_stone")', content)
        self.assertNotIn("x1)", content.split("CANDIDATES:")[1].split("PROGRESS_CARD")[0])

        # Repeated update_progress while idle must prompt caller to invoke select_task/force_exit
        comp.select_task("minecraft:story/mine_stone")
        bb.active_goal_name = ""
        _, r1 = comp_compiler.compile_tool_call("update_progress", {"note": "first note"}, bb)
        self.assertTrue(r1["ok"])
        bb.set("last_plan_result", r1, writer="test")
        _, r2 = comp_compiler.compile_tool_call("update_progress", {"note": "second note"}, bb)
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["error"], "update_progress_is_note_only_call_select_task_or_force_exit")

    def test_failed_pillar_jump_blacklists_edge_instead_of_looping(self) -> None:
        from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
        from v2.bt.blackboard import Blackboard
        from v2.gateway import VirtualWorldGateway
        from v2.gateway.multihop_navigator import MultiHopNavigator
        from v2.kernel import MinecraftAgentClient, PendingTransactionJournal, UnifiedWorldDB

        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-2, 63, -2, 2, 63, 2, "stone")
        sim.give("cobblestone", 8)
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "PillarBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "PillarBot", "pw")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                nav = MultiHopNavigator(gw, journal)
                nav._pending_pillar = ("overworld", (0, 64, 0), "cobblestone")
                # Player landed back on ground at y=64 without rising above p_target
                out = nav.step_toward((5.5, 64.0, 0.5), {"pos": [0.5, 64.0, 0.5], "dimension": "overworld", "on_ground": True}, inventory_counts={"cobblestone": 8})
                self.assertIsNone(nav._pending_pillar)
                self.assertTrue(gw.unreachable.is_edge_blocked("overworld", (0, 64, 0), (0, 65, 0)))
                self.assertIn(out.status, {"PROGRESSED", "BLOCKED"})
            finally:
                client.close()
                db.close()
