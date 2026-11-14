"""Real loopback HTTP tests for the read-only event dashboard; no game is used."""

import base64
import contextlib
import hashlib
import io
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from debug.monitor import EventMonitor, MonitorServer


def event(name="HEARTBEAT", data=None, level="INFO"):
    return {"timestamp": "2026-11-13T14:00:00Z", "level": level,
            "module": "test", "event": name, "data": data or {}}


class EventMonitorTests(unittest.TestCase):
    def test_cursor_ring_and_lost_cursor_are_bounded_and_explicit(self):
        monitor = EventMonitor(max_events=3)
        for step in range(5):
            monitor.publish(event(data={"steps": step}))
        snapshot = monitor.snapshot(1)
        self.assertEqual([e["seq"] for e in snapshot["events"]], [3, 4, 5])
        self.assertEqual(snapshot["last_seq"], 5)
        self.assertEqual(snapshot["dropped"], 2)
        self.assertTrue(snapshot["reset"])
        self.assertEqual(monitor.snapshot(4)["events"][0]["seq"], 5)
        self.assertEqual(monitor.snapshot(5)["events"], [])
        self.assertTrue(monitor.snapshot(99)["reset"])

    def test_default_ring_never_exceeds_500_records(self):
        monitor = EventMonitor()
        for i in range(550):
            monitor.publish(event(data={"steps": i}))
        self.assertEqual(len(monitor.snapshot()["events"]), 500)
        self.assertEqual(monitor.snapshot()["dropped"], 50)

    def test_memory_budget_handles_large_records_without_unbounded_retention(self):
        monitor = EventMonitor(max_bytes=4096)
        monitor.publish(event("LLM_RESPONSE", {"content": "x" * 10000}))
        payload = monitor.snapshot()
        self.assertLess(len(json.dumps(payload)), 4096)
        self.assertTrue(payload["events"][0]["data"]["_monitor_truncated"])

    def test_nested_credentials_and_echoed_values_are_redacted_but_usage_is_kept(self):
        monitor = EventMonitor()
        monitor.publish(event("LLM_RESPONSE", {
            "api_key": "model-secret-value", "rcon_password": "rcon-secret-value",
            "headers": {"Authorization": "Bearer very-private-auth"},
            "response": {"choices": [{"message": {"content": "model-secret-value / rcon-secret-value"}}],
                         "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}},
            "elapsed_ms": 1245,
        }))
        text = json.dumps(monitor.snapshot()) + json.dumps(monitor.state())
        for secret in ("model-secret-value", "rcon-secret-value", "very-private-auth"):
            self.assertNotIn(secret, text)
        self.assertEqual(monitor.state()["llm"]["usage"]["total_tokens"], 17)
        self.assertEqual(monitor.state()["llm"]["elapsed_ms"], 1245)

    def test_url_credentials_query_values_and_freeform_bearer_are_redacted(self):
        monitor = EventMonitor()
        monitor.publish(event("LLM_ERROR", {"message": "https://user:password123@example.test/path?api_key=querysecret&token=othertoken Bearer ABCDEFG12345 sk-example123456789"}))
        text = json.dumps(monitor.snapshot())
        for secret in ("password123", "querysecret", "othertoken", "ABCDEFG12345", "sk-example123456789"):
            self.assertNotIn(secret, text)
        self.assertIn("example.test/path", text)

    def test_state_tracks_agent_decision_model_and_result_without_requiring_llm(self):
        monitor = EventMonitor()
        self.assertFalse(monitor.state()["has_data"])
        monitor.publish(event(data={"username": "SleepTestBot", "planner": "deterministic", "steps": 3,
                                    "status": {"pos": [1, 64, 2], "health": 18, "food": 17}}))
        self.assertEqual(monitor.state()["run"]["username"], "SleepTestBot")
        self.assertEqual(monitor.state()["status"]["health"], 18)
        decision = {"name": "gather_block", "args": {"pos": [3, 64, 2]}, "reason": "Need wood", "goal": "wood"}
        monitor.publish(event("DECISION", {"decision": decision}))
        self.assertTrue(monitor.state()["run"]["busy"])
        monitor.publish(event("STEP_RESULT", {"decision": decision, "step": 4, "result": {"success": True, "message": "Verified gain"}}))
        self.assertFalse(monitor.state()["run"]["busy"])
        self.assertEqual(monitor.state()["last_result"]["message"], "Verified gain")
        self.assertEqual(monitor.state()["run"]["steps"], 4)
        monitor.publish(event("PAUSED", {"reason": "user"}))
        self.assertEqual(monitor.state()["run"]["state"], "paused")
        monitor.publish(event("RESUMED"))
        self.assertFalse(monitor.state()["run"]["paused"])

    def test_full_model_content_tool_calls_and_reasoning_are_preserved(self):
        monitor = EventMonitor()
        request = {"model": "test-model", "messages": [{"role": "user", "content": "观察后行动"}]}
        response = {"response": {"choices": [{"message": {"content": "模型正文" * 500,
                    "tool_calls": [{"id": "call-1", "function": {"name": "move_to", "arguments": "{\"x\":2}"}}],
                    "reasoning_content": "服务返回的附带说明"}}], "usage": {"total_tokens": 99}}, "elapsed_ms": 500}
        monitor.publish(event("LLM_REQUEST", request))
        self.assertEqual(monitor.state()["llm"]["state"], "requesting")
        monitor.publish(event("LLM_RESPONSE", response))
        self.assertEqual(monitor.state()["llm"]["request"], request)
        self.assertEqual(monitor.state()["llm"]["response"], response)

    def test_request_route_never_substitutes_for_actual_response_model(self):
        monitor = EventMonitor()
        monitor.publish(event(data={"model": "default", "planner": "llm"}))
        self.assertIsNone(monitor.state()["llm"]["actual_model"])
        monitor.publish(event("LLM_REQUEST", {"model": "default"}))
        self.assertEqual(monitor.state()["llm"]["request_model"], "default")
        monitor.publish(event("LLM_ERROR", {"code": "llm_http_error", "message": "HTTP 502"}))
        self.assertIsNone(monitor.state()["llm"]["actual_model"])
        monitor.publish(event("LLM_RESPONSE", {"response": {"model": "actual-backend-model", "choices": []}}))
        monitor.publish(event(data={"model": "default"}))
        self.assertEqual(monitor.state()["llm"]["actual_model"], "actual-backend-model")
        self.assertEqual(monitor.state()["llm"]["request_model"], "default")
        monitor.publish(event("LLM_REQUEST", {"model": "default"}))
        self.assertIsNone(monitor.state()["llm"]["actual_model"])
        monitor.publish(event("LLM_RESPONSE", {"model": "default", "response": {"choices": []}}))
        self.assertIsNone(monitor.state()["llm"]["actual_model"])

    def test_agent_phases_and_heartbeat_pause_reason_are_retained(self):
        monitor = EventMonitor()
        for phase in ("observing", "thinking", "rechecking", "acting", "waiting"):
            monitor.publish(event("AGENT_PHASE", {"phase": phase, "planner": "llm"}))
            self.assertEqual(monitor.state()["run"]["phase"], phase)
            self.assertEqual(monitor.state()["run"]["busy"], phase != "waiting")
        monitor.publish(event(data={"phase": "paused", "paused": True, "pause_reason": "llm_http_error"}))
        self.assertEqual(monitor.state()["run"]["state"], "paused")
        self.assertEqual(monitor.state()["run"]["pause_reason"], "llm_http_error")
        monitor.publish(event("RESUMED"))
        self.assertIsNone(monitor.state()["run"]["pause_reason"])

    def test_snapshots_are_copies_and_input_records_are_not_modified(self):
        monitor = EventMonitor()
        source = event(data={"status": {"health": 20}})
        monitor.publish(source)
        source["data"]["status"]["health"] = 0
        snapshot = monitor.snapshot()
        snapshot["events"][0]["data"]["status"]["health"] = -1
        self.assertEqual(monitor.state()["status"]["health"], 20)

    def test_concurrent_publish_has_unique_ordered_sequences(self):
        monitor = EventMonitor()
        threads = [threading.Thread(target=lambda: [monitor.publish(event()) for _ in range(25)]) for _ in range(4)]
        for worker in threads:
            worker.start()
        for worker in threads:
            worker.join()
        self.assertEqual([r["seq"] for r in monitor.snapshot()["events"]], list(range(1, 101)))


class MonitorHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        Path(self.directory.name, "config.yaml").write_text("password: never-read-this-secret", encoding="utf-8")
        self.server = MonitorServer(port=0, log_dir=self.directory.name).start()
        self.addCleanup(self.server.close)

    def fetch(self, path, *, headers=None, method="GET"):
        request = Request(self.server.address + path, headers=headers or {}, method=method)
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            return response.status, dict(response.headers), response.read().decode("utf-8")

    def test_real_http_cursor_and_state_include_only_published_data(self):
        self.server.publish(event(data={"username": "VisibleBot", "status": {"health": 15}}))
        code, headers, body = self.fetch("/api/events?since=0")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["events"][0]["data"]["username"], "VisibleBot")
        self.assertNotIn("never-read-this-secret", body)
        self.assertNotIn(self.directory.name, body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(json.loads(self.fetch("/api/events?since=1")[2])["events"], [])
        state = json.loads(self.fetch("/api/state")[2])
        self.assertEqual(state["status"]["health"], 15)
        self.assertEqual(state["run"]["username"], "VisibleBot")

    def test_refresh_can_retrieve_current_ring_and_summary(self):
        self.server.publish(event("PAUSED", {"reason": "manual"}))
        self.assertEqual(len(json.loads(self.fetch("/api/events")[2])["events"]), 1)
        self.assertEqual(json.loads(self.fetch("/api/state")[2])["run"]["state"], "paused")
        self.assertEqual(len(json.loads(self.fetch("/api/events?since=0")[2])["events"]), 1)

    def test_xss_record_remains_json_text_and_cannot_enter_static_html(self):
        malicious = '</script><img src=x onerror="alert(document.cookie)"><script>alert(1)</script>'
        self.server.publish(event("LLM_RESPONSE", {"content": malicious}))
        code, headers, body = self.fetch("/api/events")
        self.assertEqual(code, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(json.loads(body)["events"][0]["data"]["content"], malicious)
        _, page_headers, html = self.fetch("/")
        self.assertNotIn(malicious, html)
        self.assertNotIn("innerHTML", html)
        self.assertIn("textContent", html)
        self.assertIn("frame-ancestors 'none'", page_headers["Content-Security-Policy"])
        for script in re.findall(r"<script>(.*?)</script>", html, re.S):
            digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
            self.assertIn("'sha256-" + digest + "'", page_headers["Content-Security-Policy"])
        self.assertNotIn("script-src 'unsafe-inline'", page_headers["Content-Security-Policy"])

    def test_arbitrary_paths_traversal_and_config_are_not_served(self):
        for path in ("/config.yaml", "/../config.yaml", "/%2e%2e/config.yaml", "/api/config", "/monitor.py", "/logs/flight.jsonl"):
            with self.subTest(path=path):
                code, _, body = self.fetch(path)
                self.assertEqual(code, 404)
                self.assertNotIn("never-read-this-secret", body)
                self.assertNotIn(path, body)

    def test_request_url_secrets_are_never_reflected_or_logged(self):
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            code, _, body = self.fetch("/api/events?since=0&api_key=url-secret-value")
        self.assertEqual(code, 400)
        self.assertNotIn("url-secret-value", body)
        self.assertNotIn("url-secret-value", stream.getvalue())

    def test_nonlocal_host_cross_origin_and_writes_are_rejected(self):
        self.assertEqual(self.fetch("/api/state", headers={"Host": "untrusted.example"})[0], 403)
        self.assertEqual(self.fetch("/api/state", headers={"Origin": "https://untrusted.example"})[0], 403)
        self.assertEqual(self.fetch("/api/events", method="POST")[0], 405)
        self.assertEqual(self.server.snapshot()["events"], [])

    def test_invalid_cursors_do_not_reflect_input(self):
        for query in ("since=-1", "since=abc", "since=1&since=2", "since="):
            with self.subTest(query=query):
                self.assertEqual(self.fetch("/api/events?" + query)[0], 400)

    def test_binding_is_loopback_only_and_start_close_are_idempotent(self):
        for host in ("0.0.0.0", "192.168.1.1", "example.com"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                MonitorServer(host=host, port=0)
        self.assertIs(self.server.start(), self.server)
        self.server.close()
        self.server.close()


if __name__ == "__main__":
    unittest.main()
