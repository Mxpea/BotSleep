"""LLM runtime acceptance using loopback HTTP and an in-memory Minecraft world.

These tests never contact a model provider or a Minecraft server. The actual
planner, harness and skills run; only the game client and model endpoint are fake.
"""

import copy
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen

import main as entrypoint
from core.config import DEFAULTS
from core.survival_harness import SurvivalHarness


def envelope(result):
    return {"ok": True, "result": copy.deepcopy(result)}


def tool_response(name, arguments, call_id="call_1", content="Choose one observed action."):
    return {"choices": [{"message": {
        "role": "assistant", "content": content,
        "tool_calls": [{"id": call_id, "type": "function", "function": {
            "name": name,
            "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
        }}],
    }, "finish_reason": "tool_calls"}]}


class FakeChatEndpoint:
    """Serve queued Chat Completions replies over a real ephemeral HTTP socket."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.before_response = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append({"path": self.path, "body": body,
                                       "authorization": self.headers.get("Authorization")})
                owner.entered.set()
                owner.release.wait(3)
                if owner.before_response:
                    owner.before_response()
                response = owner.responses.pop(0) if owner.responses else (500, {"error": "No queued test reply"})
                status, payload = response if isinstance(response, tuple) else (200, response)
                encoded = json.dumps(payload).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # The timeout scenario intentionally closes the client.

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class FakeMinecraftClient:
    """Minimal observable game semantics for real gathering/crafting skills."""

    def __init__(self):
        self.base_url = "http://fake-game.invalid/api/v1"
        self.username = "RuntimeTestBot"
        self.token = "fake-game-session-secret"
        self.status = {"ready": True, "alive": True, "health": 20, "food": 20,
                       "dimension": "overworld", "gamemode": "survival", "is_day": True, "thundering": False,
                       "pos": [0.5, 64, 0.5], "busy": None, "on_ground": True,
                       "is_sleeping": False}
        self.counts = {}
        self.blocks = {(2, 64, 0): "oak_log"}
        self.calls = []
        self.dig_error = None

    def login(self, **kwargs):
        self.calls.append(("login",))
        return copy.deepcopy(self.status)

    def logout(self):
        self.calls.append(("logout",))
        return envelope({"logged_out": True})

    def get_status(self):
        return envelope(self.status)

    def inventory(self):
        return envelope({"items": [{"name": name, "count": count}
                                   for name, count in self.counts.items() if count > 0]})

    def get_blocks(self, radius=16, types=None, limit=64):
        rows = []
        seen = set()
        for pos, name in sorted(self.blocks.items(), key=lambda block: math.dist(self.status["pos"], block[0])):
            if math.dist(self.status["pos"], pos) > radius or (types is not None and name not in types):
                continue
            if types is None and name in seen:
                continue  # Untyped terrain reports the nearest row per type.
            seen.add(name)
            rows.append({"name": name, "pos": list(pos), "diggable": True, "properties": {}})
        return rows[:limit]

    def get_entities(self, **kwargs):
        return []

    def get_block(self, pos):
        key = tuple(math.floor(value) for value in pos)
        return envelope({"name": self.blocks.get(key, "dirt" if key[1] < 64 else "air"),
                         "pos": list(key), "diggable": True, "properties": {}})

    def dig(self, pos, collect=True):
        self.calls.append(("dig", list(pos), collect))
        if self.dig_error:
            return {"ok": False, "error": {"code": self.dig_error, "message": "Fixture refuses this dig"}}
        name = self.blocks.pop(tuple(pos))
        self.counts[name] = self.counts.get(name, 0) + 1
        return envelope({"dug": name})

    def craft(self, item, count=1):
        self.calls.append(("craft", item, count))
        if item != "oak_planks" or self.counts.get("oak_log", 0) < 1:
            return {"ok": False, "error": {"code": "missing_materials", "message": "A log is required"}}
        self.counts["oak_log"] -= 1
        self.counts["oak_planks"] = self.counts.get("oak_planks", 0) + 4
        return envelope({"crafted": item})

    def move_to(self, x, y, z, **kwargs):
        self.calls.append(("move_to", [x, y, z]))
        self.status["pos"] = [x, y, z]
        return envelope({"pos": [x, y, z]})

    def stop(self):
        self.calls.append(("stop",))
        return envelope({"stopped": True})

    def writes(self):
        return [call for call in self.calls if call[0] not in {"login", "logout", "stop"}]


class LLMRuntimeTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = FakeMinecraftClient()
        self.scope = 0

    def endpoint(self, replies):
        endpoint = FakeChatEndpoint(replies)
        self.addCleanup(endpoint.close)
        return endpoint

    def settings(self, endpoint, **changes):
        return {"enabled": True, "base_url": endpoint.base_url, "api_key": "fake-model-key-sentinel",
                "model": "fake-chat-model", "temperature": 0, "max_tokens": 256,
                "timeout_seconds": 1, **changes}

    def survival_settings(self, **changes):
        self.scope += 1
        return {**copy.deepcopy(DEFAULTS["survival"]), "state_dir": str(Path(self.temp.name) / f"state_{self.scope}"),
                "poll_interval_seconds": 0.002, "retry_seconds": 0.002,
                "max_steps": 2, "max_runtime_seconds": 10, **changes}

    def harness(self, endpoint, llm_changes=None, recorder=None, **settings):
        from agent.llm_planner import LLMSurvivalPlanner
        survival = self.survival_settings(**settings)
        planner = LLMSurvivalPlanner(self.settings(endpoint, **(llm_changes or {})), survival, recorder=recorder)
        harness = SurvivalHarness(self.game, survival, DEFAULTS["sleep"], planner=planner, recorder=recorder)
        self.addCleanup(harness.close)
        return harness

    def finish(self, harness, automatic=False):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            result = harness.tick(automatic=automatic)
            if result is not None:
                return result
            time.sleep(0.002)
        self.fail("The fake-model worker did not complete before its deadline")

    @staticmethod
    def tool_results(request):
        return [message for message in request["body"]["messages"] if message.get("role") == "tool"]

    def run_main(self, endpoint, max_steps=2, once=False, **llm_changes):
        config = {"agent": {"name": self.game.username, "mode": "survival", "password": "fake-login-secret"},
                  "llm": self.settings(endpoint, **llm_changes),
                  "survival": self.survival_settings(max_steps=max_steps),
                  "debug": {"flight_recorder": {"enabled": True, "output_dir": str(Path(self.temp.name) / "logs")}}}
        path = Path(self.temp.name) / "config.yaml"
        path.write_text(json.dumps(config), encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch.object(entrypoint, "MinecraftAgentClient", return_value=self.game), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            arguments = ["--config", str(path), "--max-steps", str(max_steps)]
            exit_code = entrypoint.main(arguments + (["--once"] if once else []))
        return exit_code, stdout.getvalue() + stderr.getvalue()

    def test_main_gathers_then_sends_success_before_model_decides_to_craft(self):
        endpoint = self.endpoint([
            tool_response("gather_block", {"pos": [2, 64, 0], "expected_name": "oak_log"}, "gather_1"),
            tool_response("craft_item", {"item": "oak_planks", "count": 4}, "craft_2"),
        ])
        counts_at_response = []
        endpoint.before_response = lambda: counts_at_response.append(copy.deepcopy(self.game.counts))
        result, output = self.run_main(endpoint)
        self.assertEqual(result, 2, output)  # Action budget, not the full starter goal.
        self.assertEqual([call[0] for call in self.game.writes()], ["dig", "craft"])
        self.assertEqual(self.game.counts["oak_planks"], 4)
        self.assertEqual(len(endpoint.requests), 2)
        self.assertEqual(counts_at_response, [{}, {"oak_log": 1}])
        self.assertEqual(endpoint.requests[0]["path"], "/v1/chat/completions")
        self.assertEqual(endpoint.requests[0]["authorization"], "Bearer fake-model-key-sentinel")
        feedback = self.tool_results(endpoint.requests[1])
        self.assertEqual(feedback[-1]["tool_call_id"], "gather_1")
        self.assertIn('"success": true', feedback[-1]["content"].lower())
        self.assertIn("oak_log", feedback[-1]["content"])
        newest = json.loads(endpoint.requests[1]["body"]["messages"][-1]["content"])
        self.assertEqual(newest["observation"]["counts"], {"oak_log": 1})
        self.assertEqual(self.game.calls[-1], ("logout",))

    def test_failed_skill_result_is_sent_to_model_before_its_next_decision(self):
        endpoint = self.endpoint([
            tool_response("gather_block", {"pos": [2, 64, 0], "expected_name": "oak_log"}, "blocked_1"),
            tool_response("wait", {"seconds": 1}, "wait_2"),
        ])
        self.game.dig_error = "no_path"
        harness = self.harness(endpoint)
        harness.request_step()
        self.assertEqual(self.finish(harness).error_code, "no_path")
        harness.request_step()
        self.assertTrue(self.finish(harness).success)
        feedback = self.tool_results(endpoint.requests[1])[-1]
        self.assertEqual(feedback["tool_call_id"], "blocked_1")
        self.assertIn("no_path", feedback["content"])
        self.assertIn('"success": false', feedback["content"].lower())
        self.assertEqual(len(self.game.writes()), 1)

    def test_pause_while_model_is_in_flight_discards_its_late_action(self):
        endpoint = self.endpoint([tool_response("gather_block", {"pos": [2, 64, 0], "expected_name": "oak_log"})])
        endpoint.release.clear()
        harness = self.harness(endpoint)
        self.assertTrue(harness.request_step())
        self.assertTrue(endpoint.entered.wait(1))
        harness.pause("operator_pause")
        endpoint.release.set()
        result = self.finish(harness)
        self.assertFalse(result.success)
        self.assertIn(result.error_code, {"cancelled", "llm_cancelled"})
        self.assertTrue(harness.paused)
        self.assertEqual(self.game.writes(), [])
        self.assertIn(("stop",), self.game.calls)

    def test_malformed_or_unregistered_model_calls_cannot_reach_game_actions(self):
        multiple = tool_response("gather_block", {"pos": [2, 64, 0], "expected_name": "oak_log"}, "first")
        multiple["choices"][0]["message"]["tool_calls"].extend(
            tool_response("craft_item", {"item": "oak_planks"}, "second")["choices"][0]["message"]["tool_calls"])
        malicious = [
            tool_response("rcon", {"command": "op RuntimeTestBot"}),
            tool_response("gather_block", {"pos": [2, 64, 0], "expected_name": "oak_log", "ignore_safety": True}),
            tool_response("gather_block", '{"pos": [2, 64, 0], "expected_name":'),
            tool_response("move_to", {"pos": [1000000, 64, 0]}),
            multiple,
        ]
        for response in malicious:
            with self.subTest(tool=response["choices"][0]["message"]["tool_calls"][0]["function"]):
                endpoint = self.endpoint([response])
                harness = self.harness(endpoint, {"max_repair_attempts": 0})
                harness.request_step()
                result = self.finish(harness)
                self.assertFalse(result.success)
                self.assertTrue(result.error_code.startswith("llm_"), result)
                self.assertEqual(len(endpoint.requests), 1)
                self.assertEqual(self.game.writes(), [])
                harness.close()

    def test_fresh_safety_gate_blocks_action_after_status_changes_during_model_request(self):
        endpoint = self.endpoint([tool_response("gather_block", {"pos": [2, 64, 0], "expected_name": "oak_log"})])
        endpoint.before_response = lambda: self.game.status.update(gamemode="creative")
        harness = self.harness(endpoint)
        harness.request_step()
        result = self.finish(harness)
        self.assertEqual(result.error_code, "safety_changed", result)
        self.assertFalse(result.data["executed"])
        self.assertEqual(len(endpoint.requests), 1)
        self.assertEqual(self.game.writes(), [])
        self.assertEqual(self.game.counts, {})
        self.assertIsNone(harness.journal.state.get("pending"))

    def test_repair_and_history_trimming_keep_native_tool_result_pairs(self):
        endpoint = self.endpoint([
            tool_response("rcon", {"command": "give RuntimeTestBot diamond"}, "rejected_0"),
            *[tool_response("wait", {"seconds": 1}, f"wait_{index}") for index in range(1, 5)],
        ])
        harness = self.harness(endpoint, {"max_history_turns": 2, "max_repair_attempts": 1}, max_steps=8)
        for _ in range(4):
            harness.request_step()
            self.assertTrue(self.finish(harness).success)
        self.assertEqual(len(endpoint.requests), 5)
        repair = self.tool_results(endpoint.requests[1])[-1]
        self.assertEqual(repair["tool_call_id"], "rejected_0")
        self.assertFalse(json.loads(repair["content"])["executed"])
        self.assertEqual(len(self.tool_results(endpoint.requests[-1])), 2)
        for request in endpoint.requests:
            pending = set()
            for message in request["body"]["messages"]:
                if message["role"] == "assistant":
                    self.assertFalse(pending, "Previous assistant call lost its result")
                    pending.update(call["id"] for call in message.get("tool_calls", []))
                elif message["role"] == "tool":
                    self.assertIn(message["tool_call_id"], pending)
                    pending.remove(message["tool_call_id"])
            self.assertFalse(pending, "HTTP request contains an unpaired tool call")
        self.assertEqual(self.game.writes(), [])

    def test_main_auth_failure_is_visible_and_never_falls_back_to_rules(self):
        endpoint = self.endpoint([(401, {"error": {"message": "fake-model-key-sentinel must never appear"}})])
        result, output = self.run_main(endpoint, max_steps=1, once=True, max_repair_attempts=0)
        self.assertNotEqual(result, 0, output)
        self.assertIn("llm_http_error", output)
        self.assertNotIn("fake-model-key-sentinel", output)
        self.assertEqual(len(endpoint.requests), 1)
        self.assertEqual(self.game.writes(), [])
        self.assertEqual(self.game.calls[-1], ("logout",))

    def test_automatic_model_retry_recovers_after_seven_502_responses(self):
        from debug.logger import FlightRecorder
        records = []
        recorder = FlightRecorder(self.temp.name, enabled=False, sink=records.append)
        endpoint = self.endpoint([
            *[(502, {"error": {"message": "Temporary model service failure"}}) for _ in range(7)],
            tool_response("wait", {"seconds": 1}, "recovered_8"),
        ])
        harness = self.harness(endpoint, recorder=recorder, max_steps=16,
                               max_consecutive_failures=6, retry_seconds=5)
        for failure_number in range(1, 8):
            # Advance only the retry deadline. request_step()/resume() would
            # reset the failure count and conceal the regression under test.
            harness._next_attempt = 0
            result = self.finish(harness, automatic=True)
            self.assertEqual(result.error_code, "llm_http_error")
            self.assertEqual(result.data["http_status"], 502)
            self.assertEqual(harness.failures, failure_number)
            self.assertFalse(harness.paused)
            self.assertFalse(harness.finished)
            self.assertEqual(len(endpoint.requests), failure_number)
            self.assertIsNone(harness.tick(automatic=True))
            self.assertFalse(harness.busy, "Backoff must defer the next model request")
            self.assertEqual(self.game.writes(), [])

        retries = [record["data"] for record in records if record["event"] == "MODEL_RETRY_SCHEDULED"]
        self.assertEqual([event["retry_seconds"] for event in retries], [5, 10, 20, 40, 60, 60, 60])
        self.assertEqual([event["consecutive_failures"] for event in retries], list(range(1, 8)))
        self.assertTrue(all(event["http_status"] == 502 for event in retries))
        harness._next_attempt = 0
        result = self.finish(harness, automatic=True)
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["waiting"])
        self.assertEqual(harness.failures, 0)
        self.assertFalse(harness.paused)
        self.assertEqual(len(endpoint.requests), 8)
        self.assertEqual(self.game.writes(), [])

    def test_automatic_401_pauses_instead_of_retrying_or_using_rule_actions(self):
        endpoint = self.endpoint([
            (401, {"error": {"message": "Invalid fixture credentials"}}),
            tool_response("wait", {"seconds": 1}, "must_not_be_requested"),
        ])
        harness = self.harness(endpoint, max_steps=8)
        result = self.finish(harness, automatic=True)
        self.assertEqual(result.error_code, "llm_http_error")
        self.assertEqual(result.data["http_status"], 401)
        self.assertTrue(harness.paused)
        self.assertEqual(harness.pause_reason, "llm_http_error")
        harness._next_attempt = 0
        self.assertIsNone(harness.tick(automatic=True))
        self.assertFalse(harness.busy)
        self.assertEqual(len(endpoint.requests), 1)
        self.assertEqual(self.game.writes(), [])

    def test_main_model_timeout_is_visible_and_never_falls_back_to_rules(self):
        endpoint = self.endpoint([tool_response("gather_block", {"pos": [2, 64, 0], "expected_name": "oak_log"})])
        endpoint.release.clear()
        try:
            result, output = self.run_main(endpoint, max_steps=1, once=True,
                                           timeout_seconds=0.05, max_repair_attempts=0)
        finally:
            endpoint.release.set()
        self.assertNotEqual(result, 0, output)
        self.assertIn("llm_timeout", output)
        self.assertEqual(len(endpoint.requests), 1)
        self.assertEqual(self.game.writes(), [])
        self.assertEqual(self.game.calls[-1], ("logout",))

    def test_model_events_reach_http_monitor_and_disk_with_secrets_redacted(self):
        from debug.logger import FlightRecorder
        from debug.monitor import MonitorServer
        secret = "private-value-without-a-token-prefix"
        monitor = MonitorServer(port=0).start()
        self.addCleanup(monitor.close)
        recorder = FlightRecorder(str(Path(self.temp.name) / "events"),
                                  secrets=(secret, "fake-model-key-sentinel"), sink=monitor.publish)
        recorder.add_secret(self.game.token)
        endpoint = self.endpoint([tool_response(
            "gather_block", {"pos": [2, 64, 0], "expected_name": "oak_log"},
            content=f"Gather wood; diagnostic value {secret} and {self.game.token}.")])
        harness = self.harness(endpoint, recorder=recorder)
        harness.request_step()
        self.assertTrue(self.finish(harness).success)
        with urlopen(monitor.address + "/api/events", timeout=2) as response:
            snapshot = json.load(response)
        event_names = {record["event"] for record in snapshot["events"]}
        self.assertTrue({"LLM_REQUEST", "LLM_RESPONSE", "LLM_TOOL_RESULT", "STEP_RESULT"} <= event_names,
                        event_names)
        raw = json.dumps(snapshot) + Path(recorder.log_file).read_text(encoding="utf-8")
        for value in (secret, "fake-model-key-sentinel", self.game.token):
            self.assertNotIn(value, raw)
        self.assertNotIn("Bearer fake-", raw)


if __name__ == "__main__":
    unittest.main()
