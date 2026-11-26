"""Phase 5 tests: BM25 skills_kb, 6.6KB payload budget across 60 turns, HorizonSelector gate, async non-blocking worker."""

from __future__ import annotations

import http.server
import json
import socketserver
import tempfile
import threading
import time
import unittest

from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt import BTRuntime, Blackboard
from v2.gateway import VirtualWorldGateway
from v2.kernel import MinecraftAgentClient, ModelRequestGate, PendingTransactionJournal, UnifiedWorldDB
from v2.planner import (
    AsyncLLMReplanNode,
    BM25SkillRetriever,
    DepsExplainer,
    DepsPlanner,
    HorizonSelector,
    IntentCompiler,
    LLMResponse,
    LLMToolCall,
    OpenAIBrain,
    SemanticCompactor,
    SerialModelWorker,
)
from v2.primitives import PrimitiveContext
from v2.tasks import TaskCatalogue
from v2.tasks.competition import COMPETITION_TOOL_NAMES, CompetitionManager


class _FakeSlowBrain:
    def __init__(self, delay_s: float = 0.25) -> None:
        self.delay_s = delay_s
        self.calls = 0

    def chat(self, messages, tools=None) -> LLMResponse:
        self.calls += 1
        time.sleep(self.delay_s)
        return LLMResponse(
            requested_model="fake-35b",
            confirmed_model="fake-35b",
            content="",
            tool_calls=[LLMToolCall("tc_1", "run_goal", {"goal": "acquire", "item": "wooden_pickaxe", "count": 1})],
            prompt_tokens=600,
            completion_tokens=40,
        )


class TestPlannerL4(unittest.TestCase):
    def test_bm25_loads_15_skill_cards_and_retrieves_relevant_under_budget(self) -> None:
        ret = BM25SkillRetriever()
        self.assertGreaterEqual(len(ret.cards), 15)

        portal_cards = ret.retrieve("lava pool obsidian portal", tags=["portal_ready", "nether"], top_k=3, max_bytes=1500)
        self.assertTrue(any(c.id == "nether_portal_speedrun" for c in portal_cards))

        fluid_cards = ret.retrieve("target_occluded bucket water", tags=["bucket", "target_occluded"], top_k=3, max_bytes=1500)
        self.assertTrue(any(c.id == "bucket_and_fluid_los" for c in fluid_cards))

        ctx_txt = ret.format_context("lava portal", tags=["portal_ready"], top_k=3, max_bytes=1500)
        self.assertLessEqual(len(ctx_txt.encode("utf-8")), 1500)

    def test_payload_stays_under_6600_bytes_across_60_turns_with_valid_tool_pairing(self) -> None:
        sim = VirtualMcApiServer()
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "PlanBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "PlanBot", "pass")
            try:
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                comp = SemanticCompactor(keep_pairs=2)
                compiler = IntentCompiler(ctx, cat, objective="daily")
                horizon = HorizonSelector(cat, objective="daily")
                planner = DepsPlanner(None, compiler, horizon, compactor=comp)

                bb = Blackboard()
                bb.status = {"pos": [12.3, 64.0, -45.6], "dimension": "overworld", "health": 18.0, "food": 16, "oxygen": 20}
                bb.inventory_counts = {"iron_pickaxe": 1, "cobblestone": 32, "oak_planks": 12, "coal": 8}

                for turn in range(60):
                    bb.last_failure_reason = f"sample_error_{turn}" if turn % 3 == 0 else ""
                    msgs, tools = planner.build_request_payload(bb, needs_ai=(turn % 3 == 0))
                    raw = json.dumps({"messages": msgs, "tools": tools}, ensure_ascii=False).encode("utf-8")
                    self.assertLessEqual(len(raw), 6600, f"Turn {turn} exceeded 6600B: {len(raw)}B")

                    # Verify strict (assistant.tool_calls -> tool.tool_call_id) pairing!
                    i = 0
                    while i < len(msgs):
                        m = msgs[i]
                        if m["role"] == "assistant" and m.get("tool_calls"):
                            self.assertLess(i + 1, len(msgs), "Orphan assistant tool_call")
                            nxt = msgs[i + 1]
                            self.assertEqual(nxt["role"], "tool")
                            self.assertEqual(nxt["tool_call_id"], m["tool_calls"][0]["id"])
                            i += 2
                        else:
                            self.assertNotEqual(m["role"], "tool", "Orphan tool result without preceding assistant tool_call")
                            i += 1

                    comp.record_tool_turn(
                        call_id=f"call_{turn}",
                        tool_name="run_goal",
                        arguments={"goal": "acquire", "item": "iron_ingot", "count": 4},
                        result_summary={"ok": True, "turn": turn, "note": "x" * 180},
                    )
                self.assertEqual(comp.card.turns_compacted, 58)
            finally:
                client.close()
                db.close()

    def test_horizon_selector_pins_portal_gate_when_iron_ready(self) -> None:
        cat = TaskCatalogue()
        hs = HorizonSelector(cat, objective="daily")
        bb = Blackboard()
        bb.status = {"dimension": "overworld", "pos": [0, 64, 0]}
        bb.inventory_counts = {"iron_pickaxe": 1, "oak_planks": 8}
        top = hs.select_top(bb, limit=5)
        self.assertEqual(top[0].goal, "enter_nether")
        self.assertEqual(top[0].priority_tag, "PORTAL_GATE_READY")

    def test_intent_compiler_dynamic_schemas_and_competition_5_tools(self) -> None:
        sim = VirtualMcApiServer()
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "ToolBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "ToolBot", "pass")
            try:
                ctx = PrimitiveContext(VirtualWorldGateway(client, db=db), PendingTransactionJournal(db))
                cat = TaskCatalogue()
                daily_c = IntentCompiler(ctx, cat, objective="daily")
                norm_names = [t["function"]["name"] for t in daily_c.tool_schemas(needs_ai=False)]
                self.assertEqual(norm_names, ["run_goal", "wait"])
                repair_names = [t["function"]["name"] for t in daily_c.tool_schemas(needs_ai=True)]
                self.assertIn("repair_step", repair_names)

                comp_mgr = CompetitionManager(cat)
                comp_c = IntentCompiler(ctx, cat, objective="competition", competition=comp_mgr)
                comp_names = tuple(t["function"]["name"] for t in comp_c.tool_schemas())
                self.assertEqual(comp_names, COMPETITION_TOOL_NAMES)
            finally:
                client.close()
                db.close()

    def test_async_replan_never_blocks_bt_and_discards_stale_proposal_on_preemption(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-10, 63, -10, 10, 63, 10, "stone")
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "AsyncBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "AsyncBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                slow_brain = _FakeSlowBrain(delay_s=0.25)
                planner = DepsPlanner(slow_brain, IntentCompiler(ctx, cat), HorizonSelector(cat))
                worker = SerialModelWorker()
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                replan = AsyncLLMReplanNode(planner, rt.goal_mount, worker=worker)
                rt.selector.children.append(replan)

                # Tick 1 submits async job and returns IMMEDIATELY (< 0.1s!)
                t0 = time.perf_counter()
                rt.tick_once()
                elapsed = time.perf_counter() - t0
                self.assertLess(elapsed, 0.15)
                self.assertTrue(worker.is_busy)

                # Skeleton spawns while LLM is thinking -> HardSafetyBranch preempts immediately and calls replan.stop()!
                sim.add_entity(555, "skeleton", (4.5, 64.0, 0.5), kind="hostile")
                rt.tick_once()
                self.assertEqual(rt.bb.active_branch, "HardSafetyBranch")

                # Wait for slow LLM to finish -> its stale proposal MUST be discarded!
                worker.join(timeout=2.0)
                sim.entities.clear()
                rt.tick_once()
                # Because stale proposal was discarded, goal_mount is STILL empty and a fresh job is submitted
                self.assertIsNone(rt.goal_mount.goal)
                worker.join(timeout=2.0)
                # Next tick consumes fresh valid proposal and mounts goal!
                rt.tick_once()
                self.assertIsNotNone(rt.goal_mount.goal)
                self.assertEqual(rt.goal_mount.goal.name, "acquire:wooden_pickaxex1")
            finally:
                client.close()
                db.close()

    def test_openai_brain_confirmed_model_defaults_to_unconfirmed_when_missing(self) -> None:
        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                _ = self.rfile.read(length)
                # Intentionally omit top-level "model" field!
                body = json.dumps({
                    "choices": [{"message": {"content": "ok", "tool_calls": []}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10},
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        with tempfile.TemporaryDirectory() as td:
            gate = ModelRequestGate(f"http://127.0.0.1:{port}/v1", "k", directory=td)
            brain = OpenAIBrain(f"http://127.0.0.1:{port}/v1", "k", "req-model", gate=gate)
            try:
                resp = brain.chat([{"role": "user", "content": "hi"}])
                self.assertEqual(resp.requested_model, "req-model")
                self.assertEqual(resp.confirmed_model, "未确认")
                self.assertEqual(brain.prompt_completion_ratio, 10.0)
            finally:
                brain.close()
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
