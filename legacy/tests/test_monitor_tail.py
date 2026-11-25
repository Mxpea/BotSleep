"""Offline tests for the bounded, read-only flight log sidecar."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from debug.monitor import MonitorServer
from debug.monitor_tail import (
    CHUNK_BYTES, MAX_LINE_BYTES, POLL_SECONDS, TAIL_BYTES,
    FlightLogTail, LogTailEvents, main,
)


STAMP = "2026-11-13T00:00:00Z"


def line(event="AGENT_STATE", data=None, session="one", stamp=STAMP):
    return (json.dumps({"event": event, "data": data or {}, "timestamp": stamp,
                        "session_id": session}, ensure_ascii=False) + "\n").encode("utf-8")


class FlightLogTailTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "flight_20261113.jsonl"
        self.publish = Mock()
        self.tail = FlightLogTail(self.publish, log_dir=self.root)

    def records(self, name=None):
        records = [call.args[0] for call in self.publish.call_args_list]
        return [record for record in records if record["event"] == name] if name else records

    def append(self, data, path=None):
        with (path or self.path).open("ab") as stream:
            stream.write(data)

    def test_startup_reads_only_bounded_tail_of_large_file(self):
        # A sparse 32 MiB prefix must never be scanned or parsed.
        with self.path.open("wb") as stream:
            stream.seek(32 * 1024 * 1024)
            stream.write(b"\n" + line("STARTUP") + line(data={"steps": 7}))
        reads = []
        original_open = Path.open

        class CountedReader:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def read(self, size=-1):
                self_test.assertGreaterEqual(size, 0)
                value = self.stream.read(size)
                reads.append(len(value))
                return value

        self_test = self
        with patch.object(Path, "open", lambda path, *a, **kw: CountedReader(original_open(path, *a, **kw))):
            self.tail.poll()
        self.assertLessEqual(sum(reads), TAIL_BYTES + 65)
        self.assertLessEqual(max(reads), CHUNK_BYTES)
        self.assertEqual(self.records("AGENT_STATE")[0]["data"]["steps"], 7)
        self.assertEqual(self.tail.stats["window_skips"], 1)

    def test_tail_cut_in_middle_of_line_discards_prefix(self):
        payload = line(data={"padding": "x" * 300}) + line(data={"steps": 9})
        self.path.write_bytes(payload)
        tail = FlightLogTail(self.publish, file=self.path, tail_bytes=len(line(data={"steps": 9})) + 20)
        tail.poll()
        self.assertEqual([r["data"] for r in self.records("AGENT_STATE")], [{"steps": 9}])
        self.assertEqual(tail.stats["malformed"], 0)

    def test_tail_exact_line_boundary_keeps_first_record(self):
        recent = line(data={"steps": 2})
        self.path.write_bytes(line(data={"steps": 1}) + recent)
        FlightLogTail(self.publish, file=self.path, tail_bytes=len(recent)).poll()
        self.assertEqual(self.records("AGENT_STATE")[0]["data"], {"steps": 2})

    def test_partial_utf8_and_json_survive_multiple_polls(self):
        payload = line(data={"message": "中文"})
        cut = payload.index("中".encode("utf-8")) + 1
        self.path.write_bytes(payload[:cut])
        self.tail.poll()
        self.append(payload[cut:-1])
        self.tail.poll()
        self.assertEqual(self.records("AGENT_STATE"), [])
        self.append(b"\n")
        self.tail.poll()
        self.tail.poll()
        self.assertEqual(len(self.records("AGENT_STATE")), 1)
        self.assertEqual(self.records("AGENT_STATE")[0]["data"]["message"], "中文")

    def test_malformed_shapes_utf8_and_json_are_counted_without_raw_secrets(self):
        self.path.write_bytes(b'{"password":"private-secret"\n\xff\n[]\n'
                              b'{"event": 12}\n{"event":"BAD","data":[]}\n'
                              + line(data={"steps": 1}))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.tail.poll()
        self.assertEqual(self.tail.stats["malformed"], 5)
        self.assertEqual(len(self.records("AGENT_STATE")), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("private-secret", json.dumps(self.records()))
        self.assertNotIn(str(self.root), json.dumps(self.records()))

    def test_malformed_nested_state_shapes_cannot_interrupt_poll(self):
        monitor = LogTailEvents()
        tail = FlightLogTail(monitor.publish, file=self.path)
        self.path.write_bytes(line("STEP_RESULT", {"result": "secret-result"})
                              + line("AGENT_PHASE", {"phase": []})
                              + line("DECISION", {"decision": {"name": []}}) + line())
        tail.poll()
        self.assertEqual(tail.stats["malformed"], 3)
        self.assertEqual(tail.stats["published"], 1)
        self.assertNotIn("secret-result", json.dumps(monitor.snapshot()))

    def test_interleaved_old_session_cannot_pollute_latest_startup(self):
        monitor = LogTailEvents()
        tail = FlightLogTail(monitor.publish, file=self.path)
        self.path.write_bytes(line("STARTUP", session="new")
                              + line("COMPETITION_TASKS", {"board": {"score": 100}}, session="old"))
        tail.poll()
        self.append(line("COMPETITION_TASKS", {"board": {"score": 200}}, session="old"))
        tail.poll()
        self.assertIsNone(monitor.state()["competition"]["board"])
        self.append(line("COMPETITION_TASKS", {"board": {"score": 1}}, session="new"))
        tail.poll()
        self.assertEqual(monitor.state()["competition"]["board"], {"score": 1})

    def test_oversized_complete_and_unterminated_lines_have_bounded_storage(self):
        self.path.write_bytes(b"x" * (MAX_LINE_BYTES + 1))
        self.tail.poll()
        self.assertEqual(self.tail.stats["oversized"], 1)
        self.assertLessEqual(len(self.tail._partial), MAX_LINE_BYTES)
        self.append(b"y" * (MAX_LINE_BYTES + 1))
        self.tail.poll()
        self.assertEqual(self.tail.stats["oversized"], 1)
        self.append(b"\n" + b"z" * (MAX_LINE_BYTES + 1) + b"\n" + line())
        self.tail.poll()
        self.assertEqual(self.tail.stats["oversized"], 2)
        self.assertEqual(len(self.records("AGENT_STATE")), 1)

    def test_append_is_incremental_without_duplicates(self):
        self.path.write_bytes(line(data={"steps": 1}))
        self.tail.poll()
        self.tail.poll()
        self.append(line(data={"steps": 2}))
        self.tail.poll()
        self.assertEqual([r["data"]["steps"] for r in self.records("AGENT_STATE")], [1, 2])

    def test_truncate_discards_partial_and_resets_session_state(self):
        monitor = LogTailEvents()
        tail = FlightLogTail(monitor.publish, file=self.path)
        self.path.write_bytes(line("COMPETITION_TASKS", {"board": {"score": 100}}) + b'{"partial":')
        tail.poll()
        self.path.write_bytes(line(data={"steps": 2}, session="two"))
        tail.poll()
        self.assertIsNone(monitor.state()["competition"]["board"])
        self.assertEqual(monitor.state()["run"]["steps"], 2)
        self.assertFalse(tail._partial)

    def test_truncate_and_regrow_beyond_old_offset_is_detected(self):
        self.path.write_bytes(line(data={"steps": 1}))
        self.tail.poll()
        self.path.write_bytes(line(data={"steps": 2, "padding": "x" * 200}))
        self.tail.poll()
        self.assertEqual([r["data"]["steps"] for r in self.records("AGENT_STATE")], [1, 2])
        self.assertTrue(any(r["data"]["reason"] == "file_truncated" for r in self.records("LOG_SOURCE")))

    def test_newest_startup_excludes_previous_run_scores(self):
        self.path.write_bytes(line("STARTUP", session="old")
                              + line("COMPETITION_TASKS", {"board": {"score": 100}}, session="old")
                              + line("STARTUP", session="new") + line(data={"steps": 3}, session="new"))
        self.tail.poll()
        self.assertEqual([r["event"] for r in self.records() if r["event"] != "LOG_SOURCE"],
                         ["STARTUP", "AGENT_STATE"])
        self.assertEqual(self.records("STARTUP")[0]["session_id"], "new")

    def test_appended_startup_clears_boards_heartbeat_and_model_state(self):
        monitor = LogTailEvents()
        tail = FlightLogTail(monitor.publish, file=self.path)
        self.path.write_bytes(line("HEARTBEAT") + line("COMPETITION_TASKS", {"board": {"score": 100}})
                              + line("LLM_REQUEST", {"model": "old"}))
        tail.poll()
        cursor = monitor.snapshot()["last_seq"]
        self.append(line("STARTUP", session="two"))
        tail.poll()
        state = monitor.state()
        self.assertIsNone(state["competition"]["board"])
        self.assertIsNone(state["heartbeat_at"])
        self.assertIsNone(state["llm"]["request"])
        self.assertGreater(monitor.snapshot()["last_seq"], cursor)
        self.assertFalse(monitor.snapshot(cursor)["reset"])

    def test_rotation_different_session_clears_previous_score(self):
        monitor = LogTailEvents()
        tail = FlightLogTail(monitor.publish, log_dir=self.root)
        self.path.write_bytes(line("COMPETITION_TASKS", {"board": {"score": 100}}))
        tail.poll()
        next_path = self.root / "flight_20261114.jsonl"
        next_path.write_bytes(line(data={"steps": 1}, session="two"))
        os.utime(next_path, (self.path.stat().st_mtime + 10,) * 2)
        tail.poll()
        self.assertEqual(tail.path, next_path)
        self.assertIsNone(monitor.state()["competition"]["board"])

    def test_midnight_rotation_same_session_preserves_its_board(self):
        monitor = LogTailEvents()
        tail = FlightLogTail(monitor.publish, log_dir=self.root)
        self.path.write_bytes(line("COMPETITION_TASKS", {"board": {"score": 10}}))
        tail.poll()
        next_path = self.root / "flight_20261114.jsonl"
        next_path.write_bytes(line(data={"steps": 2}))
        os.utime(next_path, (self.path.stat().st_mtime + 10,) * 2)
        tail.poll()
        self.assertEqual(monitor.state()["competition"]["board"], {"score": 10})

    def test_pinned_file_replacement_and_directory_filter(self):
        self.path.write_bytes(line(data={"steps": 1}))
        (self.root / "other.jsonl").write_bytes(line(data={"steps": 99}))
        self.tail.poll()
        pinned = FlightLogTail(self.publish, file=self.path)
        pinned.poll()
        replacement = self.root / "replacement"
        replacement.write_bytes(line(data={"steps": 2}))
        replacement.replace(self.path)
        pinned.poll()
        self.assertEqual([r["data"]["steps"] for r in self.records("AGENT_STATE")], [1, 1, 2])

    def test_missing_directory_and_disappearing_file_recover_without_paths(self):
        tail = FlightLogTail(self.publish, log_dir=self.root / "missing")
        tail.poll()
        self.assertEqual(self.records("LOG_SOURCE")[-1]["data"]["reason"], "waiting_for_log")
        pinned = FlightLogTail(self.publish, file=self.path)
        pinned.poll()
        self.assertEqual(pinned.stats["io_errors"], 1)
        self.path.write_bytes(line())
        pinned.poll()
        self.assertEqual(len(self.records("AGENT_STATE")), 1)
        self.assertNotIn(str(self.root), json.dumps(self.records()))

    def test_source_age_and_heartbeat_never_imply_current_agent_health(self):
        monitor = LogTailEvents()
        now = 1794528000.0  # 2026-11-13T00:00:00Z
        tail = FlightLogTail(monitor.publish, file=self.path, clock=lambda: now + 3600)
        self.path.write_bytes(line("STARTUP") + line(data={"phase": "acting"}))
        tail.poll()
        state = monitor.state()
        self.assertIsNone(state["heartbeat_at"])
        self.assertEqual(state["source"]["mode"], "log_tail")
        self.assertEqual(state["source"]["file"], self.path.name)
        self.assertEqual(state["source"]["last_event_at"], STAMP)
        self.assertEqual(state["source"]["data_age_seconds"], 3600)
        self.assertEqual(state["run"]["source"]["health"], "unconfirmed")
        self.assertNotEqual(state["run"]["state"], "running")
        self.append(line("HEARTBEAT"))
        tail.poll()
        self.assertEqual(monitor.state()["heartbeat_at"], STAMP)
        self.assertIs(monitor.state()["status_fresh"], False)
        tail.poll()
        self.assertEqual(monitor.state()["heartbeat_at"], STAMP)

    def test_invalid_or_missing_timestamp_cannot_fabricate_heartbeat(self):
        monitor = LogTailEvents()
        self.path.write_bytes(line("HEARTBEAT", stamp=None) + line("HEARTBEAT", stamp="bad")
                              + line("HEARTBEAT", stamp="2026-11-13T00:00:00"))
        tail = FlightLogTail(monitor.publish, file=self.path)
        tail.poll()
        self.assertIsNone(monitor.state()["heartbeat_at"])
        self.assertEqual(tail.stats["malformed"], 3)


class MonitorTailEntrypointTests(unittest.TestCase):
    def test_cli_reuses_monitor_without_starting_network_or_loading_config(self):
        with tempfile.TemporaryDirectory() as directory:
            server = Mock(spec=MonitorServer)
            with patch("debug.monitor_tail.MonitorServer", return_value=server) as factory, \
                    patch("debug.monitor_tail.time.sleep", side_effect=KeyboardInterrupt) as sleep, \
                    patch("socket.socket", side_effect=AssertionError("unexpected network")):
                self.assertEqual(main(["--log-dir", directory, "--host", "127.0.0.1", "--port", "18930"]), 0)
            factory.assert_called_once_with(host="127.0.0.1", port=18930)
            server.start.assert_called_once_with()
            server.close.assert_called_once_with()
            self.assertIsInstance(server.events, LogTailEvents)
            sleep.assert_called_once_with(POLL_SECONDS)
            self.assertTrue(all(call.args[0]["event"] == "LOG_SOURCE" for call in server.publish.call_args_list))

    def test_explicit_file_and_localhost_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chosen.jsonl"
            path.write_bytes(line())
            server = Mock(spec=MonitorServer)
            with patch("debug.monitor_tail.MonitorServer", return_value=server) as factory, \
                    patch("debug.monitor_tail.time.sleep", side_effect=KeyboardInterrupt):
                main(["--file", str(path), "--host", "localhost", "--port", "18931"])
            factory.assert_called_once_with(host="127.0.0.1", port=18931)
            self.assertTrue(any(call.args[0]["event"] == "AGENT_STATE" for call in server.publish.call_args_list))

    def test_non_loopback_and_invalid_ports_rejected_before_server_creation(self):
        for args in (["--host", "0.0.0.0"], ["--host", "example.com"], ["--host", "192.168.1.2"],
                     ["--port", "-1"], ["--port", "65536"], ["--file", "x", "--log-dir", "y"]):
            with self.subTest(args=args), patch("debug.monitor_tail.MonitorServer") as factory, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(args)
            factory.assert_not_called()

    def test_fresh_import_and_cli_do_not_import_config_game_or_model(self):
        # A fresh interpreter guards against imports hidden by this test process.
        code = r'''
import importlib.abc
import sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'core', 'agent', 'skills', 'requests', 'yaml', 'main'}:
            raise AssertionError('Forbidden dependency: ' + fullname)
sys.meta_path.insert(0, Guard())
import debug.monitor_tail as module
from unittest.mock import Mock, patch
with patch.object(module, 'MonitorServer', return_value=Mock()), patch.object(module.time, 'sleep', side_effect=KeyboardInterrupt), patch('socket.socket', side_effect=AssertionError('network')):
    module.main(['--log-dir', sys.argv[1]])
'''
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-B", "-c", code, directory],
                                    capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
