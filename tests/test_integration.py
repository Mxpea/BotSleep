"""Real HTTP transport against a deterministic fake API, never a game server."""

import contextlib
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from main import main


class FakeAPI:
    def __init__(self, mode="sleep"):
        self.mode = mode
        self.calls = []
        self.interacted = False
        self.sleep_polls = 0
        self.position = [0, 64, 0]
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.dispatch()

            def do_POST(self):
                self.dispatch()

            def dispatch(self):
                path = urlsplit(self.path).path.removeprefix("/api/v1/")
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}
                owner.calls.append((self.command, path, body))
                status = 200
                payload = {"ok": True, "result": {}}
                if path == "auth/login":
                    if owner.mode == "denied":
                        status = 401
                        payload = {"ok": False, "error": {"code": "invalid_credentials", "message": "denied"}}
                    else:
                        payload = {"ok": True, "token": "fake-session", "bot_username": "TestBot"}
                elif self.headers.get("Authorization") != "Bearer fake-session":
                    status = 401
                    payload = {"ok": False, "error": {"code": "unauthorized"}}
                elif path == "status":
                    if owner.interacted:
                        owner.sleep_polls += 1
                    awake = owner.sleep_polls >= 2
                    payload["result"] = {
                        "username": "TestBot", "ready": True, "alive": True, "health": 20,
                        "food": 20, "dimension": "overworld", "pos": owner.position,
                        "busy": None, "is_day": awake, "time_of_day": 0 if awake else 18000,
                        "is_sleeping": owner.interacted and not awake, "thundering": False,
                    }
                elif path == "find_blocks":
                    payload["result"] = {"blocks": [] if owner.mode == "no_bed" else [
                        {"name": "red_bed", "pos": [6, 64, 0], "dist": 6,
                         "properties": {"part": "head", "occupied": False, "facing": "north"}},
                        {"name": "red_bed", "pos": [6, 64, 1], "dist": 6.1,
                         "properties": {"part": "foot", "occupied": False, "facing": "north"}},
                    ]}
                elif path == "get_entities":
                    payload["result"] = {"entities": []}
                elif path == "get_block":
                    payload["result"] = {"name": "red_bed", "pos": [body.get("x", 6), 64, body.get("z", 0)],
                                         "properties": {"part": "head", "occupied": False, "facing": "north"}}
                elif path == "move_to":
                    owner.position = [body["x"], body["y"], body["z"]]
                    payload["result"] = {"pos": owner.position}
                elif path == "interact":
                    owner.interacted = True
                    payload["result"] = {"slept": True}
                elif path == "events":
                    payload["result"] = {"events": [], "last_seq": 0, "has_more": False}
                elif path in ("stop", "auth/logout", "chat"):
                    payload["result"] = {"stopped": path == "stop"}
                else:
                    status = 404
                    payload = {"ok": False, "error": {"code": "unknown_api"}}
                encoded = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class EntryPointIntegrationTests(unittest.TestCase):
    def run_scenario(self, mode):
        with FakeAPI(mode) as api, tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text(json.dumps({
                "agent": {"name": "TestBot", "password": "fake-password"},
                "mc_api": {"base_url": f"http://127.0.0.1:{api.server.server_port}/api/v1"},
                "sleep": {"poll_interval_seconds": 0.01, "confirm_timeout_seconds": 0.5,
                          "timeout_seconds": 3, "move_timeout_seconds": 1},
            }), encoding="utf-8")
            output = io.StringIO()
            with patch.dict("os.environ", {}, clear=True), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                code = main(["--config", str(config), "--once"])
            records = []
            for path in (root / "logs").glob("*.jsonl"):
                records.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
            self.assertNotIn("fake-password", output.getvalue())
            self.assertNotIn("fake-session", output.getvalue())
            return code, api.calls, records

    def test_config_client_skill_main_sleep_and_logout(self):
        code, calls, records = self.run_scenario("sleep")
        self.assertEqual(code, 0)
        paths = [path for _, path, _ in calls]
        self.assertIn("find_blocks", paths)
        self.assertIn("get_entities", paths)
        self.assertIn("interact", paths)
        self.assertEqual(paths[-1], "auth/logout")
        find = next(body for _, path, body in calls if path == "find_blocks")
        self.assertIn("red_bed", find["types"])
        move = next(body for _, path, body in calls if path == "move_to")
        self.assertIn("timeout", move)
        self.assertNotIn("timeout_ms", move)
        result = next(record["data"] for record in records if record["event"] == "SLEEP_RESULT")
        self.assertTrue(result["success"])

    def test_no_bed_fails_honestly_and_logs_out(self):
        code, calls, records = self.run_scenario("no_bed")
        self.assertEqual(code, 1)
        self.assertNotIn("interact", [path for _, path, _ in calls])
        self.assertEqual(calls[-1][1], "auth/logout")
        result = next(record["data"] for record in records if record["event"] == "SLEEP_RESULT")
        self.assertEqual(result["error_code"], "no_bed_nearby")

    def test_login_failure_returns_nonzero_without_actions(self):
        code, calls, _ = self.run_scenario("denied")
        self.assertEqual(code, 1)
        self.assertEqual([path for _, path, _ in calls], ["auth/login"])

    def test_missing_config_returns_nonzero_without_connecting(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(io.StringIO()):
            with patch("main.MinecraftAgentClient") as client:
                self.assertEqual(main(["--config", str(Path(directory) / "missing.yaml")]), 1)
                client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
