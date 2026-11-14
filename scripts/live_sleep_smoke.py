"""Explicit live acceptance test for the disposable .test-runtime environment.

Start the isolated Leaves and API services first, then run:
    python scripts/live_sleep_smoke.py --runtime .test-runtime

This is NOT a unit test. It modifies the disposable world through RCON, launches
main.py --once for two sleep cases and one missing-bed case, and writes a credential-free report. It refuses any other
runtime path, ports, world name, or bot identity. It does not start/stop services;
the caller should shut down its recorded server/API processes after the test.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config


class Rcon:
    def __init__(self, password):
        self.password = password

    @staticmethod
    def _receive(sock):
        def exact(count):
            data = b""
            while len(data) < count:
                part = sock.recv(count - len(data))
                if not part:
                    raise OSError("RCON closed the connection")
                data += part
            return data

        length = struct.unpack("<i", exact(4))[0]
        if not 10 <= length <= 1024 * 1024:
            raise OSError("Invalid RCON response length")
        payload = exact(length)
        return struct.unpack("<ii", payload[:8]), payload[8:-2].decode("utf-8", "replace")

    @staticmethod
    def _send(sock, request_id, kind, text):
        payload = struct.pack("<ii", request_id, kind) + text.encode() + b"\x00\x00"
        sock.sendall(struct.pack("<i", len(payload)) + payload)

    def command(self, command):
        with socket.create_connection(("127.0.0.1", 25595), timeout=5) as sock:
            self._send(sock, 1, 3, self.password)
            (request_id, _), _ = self._receive(sock)
            if request_id == -1:
                raise RuntimeError("Isolated RCON authentication failed")
            self._send(sock, 2, 2, command)
            (_, _), text = self._receive(sock)
            return text

    def require(self, command):
        text = self.command(command)
        if any(fragment in text for fragment in ("Incorrect argument", "Unknown or incomplete", "Failed to")):
            # Commands are generated here and never contain credentials.
            raise RuntimeError(f"RCON setup failed: {command}: {text}")
        return text


def validate_runtime(path):
    runtime = Path(path).resolve()
    if runtime != (ROOT / ".test-runtime").resolve():
        raise ValueError("Only this project's .test-runtime directory is allowed")
    manifest = json.loads((runtime / "manifest.json").read_text(encoding="utf-8-sig"))
    if (manifest.get("minecraft_port"), manifest.get("rcon_port"), manifest.get("api_port")) != (25585, 25595, 18920):
        raise ValueError("Isolated port manifest does not match")
    if Path(manifest.get("server_directory", "")).resolve() != runtime / "leaves":
        raise ValueError("Isolated server directory does not match")
    props = {}
    for line in (runtime / "leaves/server.properties").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            props[key] = value
    expected = {"server-ip": "127.0.0.1", "server-port": "25585", "rcon.port": "25595",
                "enable-rcon": "true", "level-name": "botsleep-test-world"}
    if any(props.get(key) != value for key, value in expected.items()):
        raise ValueError("Server properties are not the disposable local test world")
    api = json.loads((runtime / "api-config.json").read_text(encoding="utf-8"))
    if api.get("http", {}).get("host") != "127.0.0.1" or api["http"].get("port") != 18920:
        raise ValueError("API listener is not the isolated local test service")
    if api.get("minecraft", {}).get("host") != "127.0.0.1" or api["minecraft"].get("port") != 25585:
        raise ValueError("API Minecraft target is not the isolated local test service")
    if api.get("auth", {}).get("allowedUsernames") != ["SleepTestBot"]:
        raise ValueError("API must allow only the disposable bot identity")
    config = load_config(runtime / "botsleep.yaml", environ={})
    if config["agent"]["name"] != "SleepTestBot" or config["mc_api"]["base_url"] != "http://127.0.0.1:18920/api/v1":
        raise ValueError("Bot configuration does not target the disposable server")
    if config["agent"]["password"] != api["auth"].get("password"):
        raise ValueError("Disposable bot and API credentials do not match")
    if not Path(config["debug"]["flight_recorder"]["output_dir"]).is_relative_to(runtime):
        raise ValueError("Flight recorder must stay inside the disposable runtime")
    processes = json.loads((runtime / "processes.json").read_text(encoding="utf-8-sig"))
    if Path(processes.get("server_dir", "")).resolve() != runtime / "leaves":
        raise ValueError("Recorded process directory is not the disposable server")
    if os.name != "nt":
        raise ValueError("This prepared runtime uses Windows process ownership verification")
    inspected = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         "@(Get-NetTCPConnection -LocalPort 25585,25595,18920 | Select-Object LocalPort,OwningProcess -Unique) | ConvertTo-Json -Compress"],
        capture_output=True, text=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if inspected.returncode != 0:
        raise ValueError("Could not verify disposable listener process ownership")
    listeners = json.loads(inspected.stdout or "[]")
    if isinstance(listeners, dict):
        listeners = [listeners]
    expected_owners = {25585: processes.get("java_pid"), 25595: processes.get("java_pid"), 18920: processes.get("api_pid")}
    if {item["LocalPort"]: item["OwningProcess"] for item in listeners} != expected_owners:
        raise ValueError("Listening ports do not belong to the recorded disposable processes")
    return runtime, config, Rcon(props["rcon.password"]), [props["rcon.password"], config["agent"]["password"]]


def redact(text, secrets):
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def records(log_dir):
    entries = []
    for path in sorted(Path(log_dir).glob("flight_*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
    return entries


def time_of_day(rcon):
    response = rcon.command("time query daytime")
    match = re.search(r"(\d+)\s*$", response)
    if not match:
        raise RuntimeError("Could not read isolated server daytime")
    return int(match.group(1))


def run_case(runtime, config, rcon, secrets, *, with_bed, name):
    if with_bed:
        rcon.require("setblock 3 100 0 red_bed[facing=east,part=foot]")
        rcon.require("setblock 4 100 0 red_bed[facing=east,part=head]")
    else:
        rcon.command("setblock 3 100 0 air")
        rcon.command("setblock 4 100 0 air")
    rcon.require("time set midnight")
    before = time_of_day(rcon)
    log_dir = config["debug"]["flight_recorder"]["output_dir"]
    start_records = len(records(log_dir))
    env = dict(os.environ)
    for key in ("MC_AGENT_API", "MC_AGENT_USER", "MC_AGENT_PASSWORD", "BOTSLEEP_CONFIG"):
        env.pop(key, None)
    command = [sys.executable, str(ROOT / "main.py"), "--config", str(runtime / "botsleep.yaml"), "--once"]
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace",
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    sleeping_samples = []
    expired = False
    try:
        while process.poll() is None:
            if time.monotonic() - started > 100:
                expired = True
                process.terminate()
                break
            response = rcon.command("data get entity SleepTestBot SleepTimer")
            match = re.search(r"entity data:\s*(\d+)s?\s*$", response)
            # The server retains SleepTimer values >= 100 briefly after waking.
            if match and 0 < int(match.group(1)) < 100:
                sleeping_samples.append({"elapsed_seconds": round(time.monotonic() - started, 3), "sleep_timer": int(match.group(1))})
            time.sleep(0.15)
        stdout, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    stdout, stderr = redact(stdout, secrets), redact(stderr, secrets)
    (runtime / f"{name}.stdout.log").write_text(stdout, encoding="utf-8")
    (runtime / f"{name}.stderr.log").write_text(stderr, encoding="utf-8")
    after = time_of_day(rcon)
    added = records(log_dir)[start_records:]
    results = [entry.get("data", {}) for entry in added if entry.get("event") == "SLEEP_RESULT"]
    result = results[-1] if results else None
    players = rcon.command("list")
    logged_out = "SleepTestBot" not in players
    if with_bed:
        passed = (not expired and process.returncode == 0 and result and result.get("success") is True
                  and after < 12000 and logged_out and bool(sleeping_samples)
                  and bool((result.get("data") or {}).get("verification")))
    else:
        passed = (not expired and process.returncode != 0 and result and result.get("success") is False
                  and result.get("error_code") == "no_bed_nearby" and logged_out)
    report = {"name": name, "passed": bool(passed), "returncode": process.returncode, "timed_out": expired,
              "elapsed_seconds": round(time.monotonic() - started, 3), "daytime_before": before,
              "daytime_after": after, "sleep_observed_by_rcon": bool(sleeping_samples),
              "sleep_samples": sleeping_samples[:20], "logged_out": logged_out,
              "skill_result": result, "stdout": stdout, "stderr": stderr}
    print(json.dumps({key: report[key] for key in ("name", "passed", "returncode", "daytime_before", "daytime_after", "sleep_observed_by_rcon", "logged_out")}), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, help="Explicit path to this project's disposable .test-runtime")
    args = parser.parse_args()
    runtime, config, rcon, secrets = validate_runtime(args.runtime)
    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "runtime": str(runtime), "cases": [], "passed": False}
    try:
        players = rcon.command("list")
        if not re.search(r"There are 0 of", players):
            raise RuntimeError("The disposable server must have no players before setup")
        for command in (
            "difficulty peaceful", "weather clear", "gamerule minecraft:spawn_mobs false",
            "gamerule minecraft:advance_weather false", "gamerule minecraft:advance_time true",
            "gamerule minecraft:respawn_radius 0", "gamerule minecraft:players_sleeping_percentage 100",
            "forceload add -16 -16 16 16", "fill -16 99 -16 16 99 16 stone",
            "fill -16 100 -16 16 110 16 air", "setworldspawn 0 100 0",
        ):
            rcon.require(command)
        report["cases"].append(run_case(runtime, config, rcon, secrets, with_bed=True, name="with_bed_1"))
        report["cases"].append(run_case(runtime, config, rcon, secrets, with_bed=True, name="with_bed_2"))
        report["cases"].append(run_case(runtime, config, rcon, secrets, with_bed=False, name="without_bed"))
        report["passed"] = all(case["passed"] for case in report["cases"])
    except Exception as error:
        report["error"] = redact(str(error), secrets)
        print("Live smoke failed: " + report["error"], file=sys.stderr)
    finally:
        try:
            if "SleepTestBot" in rcon.command("list"):
                rcon.command("kick SleepTestBot Live test cleanup")
            report["cleanup_players"] = rcon.command("list")
        except Exception as error:
            report["cleanup_error"] = redact(str(error), secrets)
            report["passed"] = False
        encoded = redact(json.dumps(report, ensure_ascii=False, indent=2), secrets)
        (runtime / "live_sleep_report.json").write_text(encoded + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
