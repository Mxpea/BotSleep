"""Offline tests for Full-Stack Debug Suite: ts_ms, LLM_IO scrubbing, LLM_DISCARDED, DebugStateTracer, and Workbench."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import debug_workbench
from v2.bt.blackboard import Blackboard
from v2.bt.goal_mount import ActiveGoalMount
from v2.observability import DebugStateTracer, FlightRecorder
from v2.planner.deps_planner import AsyncLLMReplanNode
from v2.planner.llm_client import LLMResponse, OpenAIBrain
from v2.planner.model_worker import SerialModelWorker, WorkerResult
from v2.tasks.base import Task


class _DummyGoal(Task):
    def is_finished(self, bb: Blackboard) -> bool:
        return False

    def on_tick(self, bb: Blackboard) -> str:
        return "RUNNING"


class _FakeHttpResp:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakePlanner:
    def __init__(self, brain: OpenAIBrain) -> None:
        self.brain = brain

    def build_request_payload(self, bb: Blackboard, *, needs_ai: bool = False):
        return ([{"role": "user", "content": "status"}], [])


class TestDebugSuiteAndWorkbench(unittest.TestCase):
    def test_llm_io_secret_redaction_and_ts_ms_alignment(self) -> None:
        secret = "sk-supersecretkey999"
        with tempfile.TemporaryDirectory() as td:
            fr = FlightRecorder(td, enabled=True, secrets=[secret])
            dt = DebugStateTracer(td, username="BotSecret", enabled=True)
            fr.add_listener(dt.on_flight_event)

            brain = OpenAIBrain("http://localhost:9999/v1", secret, "test-model", recorder=fr)
            try:
                fake_json = {
                    "model": "test-model-v1",
                    "usage": {"prompt_tokens": 100, "completion_tokens": 12},
                    "choices": [{
                        "message": {
                            "content": f"Leak test {secret}",
                            "tool_calls": [{
                                "id": "tc1",
                                "function": {"name": "run_goal", "arguments": '{"goal":"mine","item":"raw_iron"}'},
                            }],
                        }
                    }],
                }
                with patch.object(brain._session, "post", return_value=_FakeHttpResp(fake_json)):
                    resp = brain.chat(
                        [{"role": "user", "content": f"state token={secret}"}],
                        tools=[{"type": "function", "function": {"name": "run_goal"}}],
                    )
                self.assertEqual(resp.confirmed_model, "test-model-v1")
            finally:
                brain.close()

            assert fr.log_path is not None
            raw_flight = fr.log_path.read_text(encoding="utf-8")
            self.assertNotIn(secret, raw_flight)
            entries = [json.loads(ln) for ln in raw_flight.splitlines() if ln.strip()]
            llm_io = [e for e in entries if e["event"] == "LLM_IO"]
            self.assertEqual(len(llm_io), 1)
            self.assertIsInstance(llm_io[0]["ts_ms"], int)
            self.assertIn("[REDACTED]", llm_io[0]["data"]["response_content"])
            self.assertEqual(llm_io[0]["data"]["tool_names"], ["run_goal"])

            assert dt.trace_path is not None
            trace_lines = [json.loads(ln) for ln in dt.trace_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertTrue(any(e["event"] == "LLM_IO" for e in trace_lines))

    def test_worker_discard_and_replan_node_emits_llm_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fr = FlightRecorder(td, enabled=True)
            brain = OpenAIBrain("http://localhost:9999/v1", "sk-abc", "m", recorder=fr)
            try:
                worker = SerialModelWorker()
                gm = ActiveGoalMount()
                node = AsyncLLMReplanNode(_FakePlanner(brain), gm, worker=worker)  # type: ignore[arg-type]

                bb = Blackboard()
                bb.status = {"pos": [1.0, 64.0, 1.0], "dimension": "overworld"}
                bb.active_branch = "HardSafetyBranch"
                bb.last_action_outcome = "safety_retreat:zombie"

                # Simulate completed stale worker job at context_version=0 after preemption bumped ctx_ver -> 1
                worker._completed = WorkerResult(
                    job_id="stale123",
                    context_version=0,
                    ok=True,
                    value=LLMResponse("m", "m", "", []),
                    finished_at=time.time(),
                )
                node.stop(bb)  # bumps _context_version to 1 and records _last_stop_reason
                with patch.object(worker, "submit", return_value=False):
                    node.update(bb)

                discarded = bb.get("last_llm_discarded")
                self.assertIsInstance(discarded, dict)
                self.assertEqual(discarded["job_id"], "stale123")
                self.assertEqual(discarded["submitted_ctx_ver"], 0)
                self.assertEqual(discarded["current_ctx_ver"], 1)
                self.assertIn("HardSafetyBranch", discarded["stop_reason"])

                assert fr.log_path is not None
                ev_names = [json.loads(l)["event"] for l in fr.log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
                self.assertIn("LLM_DISCARDED", ev_names)
            finally:
                brain.close()

    def test_debug_state_tracer_emits_transitions_only_on_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dt = DebugStateTracer(td, username="BotT", enabled=True)
            bb = Blackboard()
            bb.status = {"pos": [4.2, 64.0, -3.5], "dimension": "overworld", "health": 20.0, "food": 20}
            bb.inventory_counts = {"stone_pickaxe": 1, "oak_log": 4}
            bb.active_branch = "ActiveGoalMount"
            bb.goal_status = "RUNNING"
            g = _DummyGoal("acquire:iron")

            self.assertIsNotNone(dt.record_tick(0, bb, g, 2.1))
            # Unchanged state on tick 1 -> suppressed
            self.assertIsNone(dt.record_tick(1, bb, g, 1.8))
            # Branch change + failure on tick 2 -> emitted
            bb.active_branch = "PhysicalUnstuckBranch"
            bb.last_failure_reason = "surface_escape:no_pickaxe"
            e2 = dt.record_tick(2, bb, g, 3.0)
            self.assertIsNotNone(e2)
            assert e2 is not None
            self.assertEqual(e2["data"]["from"]["branch"], "ActiveGoalMount")
            self.assertEqual(e2["data"]["to"]["branch"], "PhysicalUnstuckBranch")

    def test_debug_workbench_merges_server_client_and_classifies_systemic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            run_dir = tmp / "run_r7"
            for idx in (1, 2):
                bdir = run_dir / f"bot_{idx}"
                bdir.mkdir(parents=True)
                trace_lines = [
                    {"ts_ms": 1700000001000 + idx, "event": "STARTUP", "player": f"SleepBot{idx}", "data": {"username": f"SleepBot{idx}"}},
                    {"ts_ms": 1700000002000 + idx, "event": "STATE_TRANSITION", "player": f"SleepBot{idx}", "data": {
                        "tick": 100, "dim": "overworld", "pos": [10.0, 31.0, 10.0], "block": [10, 31, 10],
                        "from": {}, "to": {"branch": "ActiveGoalMount", "task_chain": "Acquire(stone_pickaxe)", "goal_status": "RUNNING", "fail": "surface_escape:no_pickaxe"},
                        "outcome": "nav:blocked",
                    }},
                    {"ts_ms": 1700000003000 + idx, "event": "LLM_DISCARDED", "player": f"SleepBot{idx}", "data": {
                        "job_id": f"j{idx}", "submitted_ctx_ver": 0, "current_ctx_ver": 1, "stop_reason": "preempted_by:HardSafetyBranch",
                    }},
                ]
                (bdir / "v2_debug_trace_test.jsonl").write_text("\n".join(json.dumps(x) for x in trace_lines) + "\n", encoding="utf-8")
                flight_lines = [
                    {"ts_ms": 1700000001000 + idx, "event": "STARTUP", "data": {"username": f"SleepBot{idx}"}},
                    {"ts_ms": 1700000002000 + idx, "event": "TICK_SUMMARY", "data": {
                        "tick": 100, "pos": [10.0, 31.0, 10.0], "dim": "overworld",
                        "branch": "ActiveGoalMount", "goal": "acquire:stone_pickaxex1", "out": "nav:blocked", "fail": "surface_escape:no_pickaxe",
                    }},
                    {"ts_ms": 1700000002500 + idx, "event": "TICK_SUMMARY", "data": {
                        "tick": 120, "pos": [10.0, 31.0, 10.0], "dim": "overworld",
                        "branch": "ActiveGoalMount", "goal": "acquire:stone_pickaxex1", "out": "nav:blocked", "fail": "surface_escape:no_pickaxe",
                    }},
                ]
                (bdir / "v2_flight_test.jsonl").write_text("\n".join(json.dumps(x) for x in flight_lines) + "\n", encoding="utf-8")

            srv_log = tmp / "server_events.jsonl"
            srv_lines = [
                {"ts_ms": 1700000001500, "tick": 20, "source": "mc_server", "event": "BLOCK_BREAK", "player": "SleepBot1", "dim": "overworld", "data": {"pos": [11, 31, 10], "block": "stone", "hand": "air", "hand_dmg": 0, "cancelled": False}},
                {"ts_ms": 1700000001600, "tick": 20, "source": "mc_server", "event": "BLOCK_BREAK", "player": "SleepBot2", "dim": "overworld", "data": {"pos": [12, 31, 10], "block": "deepslate", "hand": "air", "hand_dmg": 0, "cancelled": False}},
                {"ts_ms": 1700000004000, "tick": 30, "source": "mc_server", "event": "PLAYER_DEATH", "player": "SleepBot1", "dim": "overworld", "data": {"pos": [10.0, 31.0, 10.0], "message": "SleepBot1 was slain by Zombie"}},
            ]
            srv_log.write_text("\n".join(json.dumps(x) for x in srv_lines) + "\n", encoding="utf-8")

            html_out = tmp / "wb.html"
            md_out = tmp / "wb.md"
            rc = debug_workbench.main([
                str(run_dir),
                "--server-log", str(srv_log),
                "--html-out", str(html_out),
                "--md-out", str(md_out),
            ])
            self.assertEqual(rc, 0)
            md_txt = md_out.read_text(encoding="utf-8")
            html_txt = html_out.read_text(encoding="utf-8")
            self.assertIn("SYSTEMIC", md_txt)
            self.assertIn("UNDERGROUND_NO_PICK_LOCK", md_txt)
            self.assertIn("BARE_HAND_ROCK_BREAK", md_txt)
            self.assertIn("mc_server", html_txt)
            self.assertIn("LLM_DISCARDED", html_txt)


if __name__ == "__main__":
    unittest.main()
