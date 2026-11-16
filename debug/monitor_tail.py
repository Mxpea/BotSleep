"""Read-only flight-log sidecar; no configuration, game or model dependencies.

Run: python -B -m debug.monitor_tail --log-dir logs --host 127.0.0.1 --port 18931
Only the newest flight_*.jsonl is followed. --file pins an operator-selected file.
"""

import argparse
import ipaddress
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from debug.monitor import EventMonitor, MonitorServer


TAIL_BYTES = 4 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
POLL_SECONDS = 0.5
CHUNK_BYTES = 64 * 1024


class LogTailEvents(EventMonitor):
    """Keep the existing HTTP protocol, with explicitly historical agent state."""

    def _update_state(self, record):
        if record["event"] == "LOG_SOURCE" and record["data"].get("session_reset"):
            # Retain the event ring and monotonic HTTP cursor, reset only aggregates.
            self._state = EventMonitor().state()
        super()._update_state(record)
        if record["event"] == "LOG_SOURCE":
            self._state["source"] = record["data"]
        self._state["run"]["source"] = {"mode": "log_tail", "health": "unconfirmed"}
        self._state["run"]["state"] = "log_tail · Agent 健康未确认"


class FlightLogTail:
    """Bounded binary JSONL reader. poll() only reads local files and publishes."""

    def __init__(self, publish: Callable[[dict], object], *, log_dir=None, file=None,
                 tail_bytes: int = TAIL_BYTES, max_line_bytes: int = MAX_LINE_BYTES,
                 clock: Callable[[], float] = time.time):
        if (log_dir is None) == (file is None):
            raise ValueError("Select exactly one log directory or file")
        if tail_bytes < 1 or max_line_bytes < 1:
            raise ValueError("Byte limits must be positive")
        self.publish = publish
        self.log_dir = Path(log_dir) if log_dir is not None else None
        self.file = Path(file) if file is not None else None
        self.tail_bytes = tail_bytes
        self.max_line_bytes = max_line_bytes
        self.clock = clock
        self.path = None
        self.identity = None
        self.offset = 0
        self._anchor = b""
        self._partial = bytearray()
        self._discarding = False
        self._session = None
        self._startup_session = None
        self._last_timestamp = None
        self._file_mtime = None
        self.stats = {"malformed": 0, "oversized": 0, "io_errors": 0,
                      "window_skips": 0, "published": 0}

    def _count(self, key):
        self.stats[key] = min(self.stats[key] + 1, 2 ** 63 - 1)

    def _select(self):
        if self.file is not None:
            return self.file
        newest = None
        newest_key = None
        for path in self.log_dir.glob("flight_*.jsonl"):
            try:
                stat = path.stat()
                if not path.is_file():
                    continue
            except OSError:
                continue
            key = (stat.st_mtime_ns, path.name)
            if newest_key is None or key > newest_key:
                newest, newest_key = path, key
        return newest

    def _decode(self, raw):
        try:
            record = json.loads(raw.decode("utf-8"))
            if (not isinstance(record, dict) or not isinstance(record.get("event"), str)
                    or not isinstance(record.get("data", {}), dict)):
                raise ValueError("Invalid event shape")
            data = record.get("data", {})
            event = record["event"].upper()
            if event in {"STEP_RESULT", "SLEEP_RESULT"} and not isinstance(data.get("result", data), dict):
                raise ValueError("Invalid result shape")
            if event == "AGENT_PHASE" and not isinstance(data.get("phase", "idle"), str):
                raise ValueError("Invalid phase shape")
            decision = data.get("decision")
            if isinstance(decision, dict) and not isinstance(decision.get("name", "auto_sleep"), str):
                raise ValueError("Invalid decision shape")
            stamp = record.get("timestamp")
            if not isinstance(stamp, str) or datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo is None:
                raise ValueError("Missing or invalid timestamp")
            return record
        except (UnicodeError, ValueError, RecursionError):
            self._count("malformed")
            return None

    def _feed(self, chunk):
        records = []
        parts = chunk.split(b"\n")
        for index, part in enumerate(parts):
            complete = index < len(parts) - 1
            if not self._discarding:
                if len(self._partial) + len(part) > self.max_line_bytes:
                    self._partial.clear()
                    self._discarding = True
                    self._count("oversized")
                else:
                    self._partial.extend(part)
            if complete:
                if not self._discarding and self._partial:
                    record = self._decode(self._partial)
                    if record is not None:
                        records.append(record)
                self._partial.clear()
                self._discarding = False
        return records

    def _source(self, *, reset=False, reason=None):
        now = self.clock()
        age = None
        if self._last_timestamp is not None:
            try:
                stamp = datetime.fromisoformat(self._last_timestamp.replace("Z", "+00:00"))
                if stamp.tzinfo is not None:
                    age = max(0.0, now - stamp.timestamp())
                    if not math.isfinite(age):
                        age = None
            except (ValueError, TypeError, OverflowError):
                pass
        data = {"mode": "log_tail", "file": self.path.name if self.path else None,
                "health": "unconfirmed",
                "last_event_at": self._last_timestamp, "data_age_seconds": age,
                "file_age_seconds": max(0.0, now - self._file_mtime)
                if self._file_mtime is not None else None,
                "session_reset": reset, "reason": reason,
                "stats": dict(self.stats)}
        # Never include operator paths or exception text in HTTP-visible events.
        self.publish({"timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                      "level": "INFO", "module": "monitor_tail", "event": "LOG_SOURCE",
                      "data": data})

    def poll(self) -> None:
        """Read at most one tail window; retain incomplete bytes across calls."""
        reset_reason = None
        records = []
        try:
            path = self._select()
            if path is None:
                self._source(reason="waiting_for_log")
                return
            with path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                changed = path != self.path or identity != self.identity
                truncated = stat.st_size < self.offset
                if not changed and not truncated and self._anchor:
                    stream.seek(self.offset - len(self._anchor))
                    truncated = stream.read(len(self._anchor)) != self._anchor
                if changed or truncated:
                    reset_reason = "file_changed" if changed else "file_truncated"
                    self.path, self.identity = path, identity
                    self.offset = max(0, stat.st_size - self.tail_bytes)
                    self._partial.clear()
                    self._discarding = False
                    if self.offset:
                        self._count("window_skips")
                        stream.seek(self.offset - 1)
                        self._discarding = stream.read(1) != b"\n"
                stream.seek(self.offset)
                remaining = self.tail_bytes
                while remaining:
                    chunk = stream.read(min(CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    self.offset += len(chunk)
                    remaining -= len(chunk)
                    records.extend(self._feed(chunk))
                stream.seek(max(0, self.offset - 64))
                self._anchor = stream.read(min(64, self.offset))
                self._file_mtime = stat.st_mtime
        except OSError:
            self._count("io_errors")
            self._source(reason="log_unavailable")
            return

        # A flight file can contain many runs. Prefer only its last STARTUP.
        startups = [i for i, record in enumerate(records)
                    if record["event"].upper() == "STARTUP"]
        if startups:
            records = records[startups[-1]:]
            reset_reason = "startup"
        first_session = records[0].get("session_id") if records else None
        if reset_reason == "file_changed" and first_session and first_session == self._session:
            reset_reason = None  # Midnight rotation of the same run.
        if reset_reason:
            self._session = None
            self._startup_session = None
            self._last_timestamp = None
            self._source(reset=True, reason=reset_reason)
        for record in records:
            session = record.get("session_id")
            if isinstance(session, str) and session:
                if self._startup_session is not None and session != self._startup_session:
                    continue  # Interleaved events from an earlier run cannot add scores.
                if self._session is not None and session != self._session:
                    self._last_timestamp = None
                    self._source(reset=True, reason="session_changed")
                self._session = session
                if record["event"].upper() == "STARTUP":
                    self._startup_session = session
            self._last_timestamp = record["timestamp"]
            self.publish(record)
            self._count("published")
        self._source()


def _loopback(value: str) -> str:
    value = "127.0.0.1" if value == "localhost" else value
    try:
        if ipaddress.ip_address(value).is_loopback:
            return value
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("host must be a loopback IP address")


def main(argv=None) -> int:
    """Serve the existing read-only dashboard; never load application config."""
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--log-dir", type=Path, default=None)
    source.add_argument("--file", type=Path)
    parser.add_argument("--host", type=_loopback, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18931)
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    server = MonitorServer(host=args.host, port=args.port)
    server.events = LogTailEvents()
    tail = FlightLogTail(server.publish, file=args.file) if args.file else FlightLogTail(
        server.publish, log_dir=args.log_dir or Path("logs"))
    try:
        server.start()
        while True:
            tail.poll()
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        return 0
    finally:
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())
