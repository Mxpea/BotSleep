"""Explicit, disposable live acceptance for the starter survival goal.

Separate steps: --prepare, --start, --run, --shutdown, each with
--runtime .survival-test-runtime. Never targets the user's .test-runtime world.
The fixture supplies world blocks, not inventory items. Success requires the
actual main runner to finish and the server-saved inventory to contain all four
target items. RCON is used only by this development harness.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import time
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.client import MinecraftAgentClient
from core.config import load_config

BOT = "SurvivalTestBot"
WORLD = "survival-smoke-world"
PORTS = (25586, 25596, 18921)
TARGETS = ("stone_pickaxe", "stone_axe", "stone_sword", "furnace")
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def runtime_path(value):
    path = Path(value).resolve()
    if path != (ROOT / ".survival-test-runtime").resolve():
        raise ValueError("Only this project's .survival-test-runtime is allowed")
    return path


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_properties(runtime):
    result = {}
    for line in (runtime / "leaves/server.properties").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            result[key] = value
    return result


class Rcon:
    def __init__(self, password):
        self.password = password

    @staticmethod
    def receive(sock):
        def exact(count):
            data = b""
            while len(data) < count:
                part = sock.recv(count - len(data))
                if not part:
                    raise OSError("RCON closed")
                data += part
            return data
        length = struct.unpack("<i", exact(4))[0]
        if not 10 <= length <= 1024 * 1024:
            raise OSError("Invalid RCON packet length")
        body = exact(length)
        return struct.unpack("<ii", body[:8]), body[8:-2].decode("utf-8", "replace")

    @staticmethod
    def send(sock, request_id, kind, text):
        body = struct.pack("<ii", request_id, kind) + text.encode() + b"\0\0"
        sock.sendall(struct.pack("<i", len(body)) + body)

    def command(self, text):
        with socket.create_connection(("127.0.0.1", 25596), timeout=8) as sock:
            self.send(sock, 1, 3, self.password)
            (request_id, _), _ = self.receive(sock)
            if request_id == -1:
                raise RuntimeError("Disposable RCON authentication failed")
            self.send(sock, 2, 2, text)
            return self.receive(sock)[1]

    def require(self, text):
        result = self.command(text)
        if "Incorrect argument" in result or "Unknown or incomplete" in result:
            raise RuntimeError(f"Fixture command rejected: {text}: {result}")
        return result


def validate(runtime):
    manifest = json.loads((runtime / "manifest.json").read_text(encoding="utf-8-sig"))
    if manifest.get("purpose") != "disposable_survival_acceptance" or manifest.get("destructive_tests_allowed") is not True:
        raise ValueError("Runtime is not explicitly marked disposable")
    if tuple(manifest.get(key) for key in ("minecraft_port", "rcon_port", "api_port")) != PORTS:
        raise ValueError("Disposable port manifest does not match")
    if manifest.get("username") != BOT or Path(manifest["server_directory"]).resolve() != runtime / "leaves":
        raise ValueError("Disposable identity/directory does not match")
    props = read_properties(runtime)
    for key, expected in {"server-ip": "127.0.0.1", "server-port": "25586", "rcon.port": "25596", "level-name": WORLD}.items():
        if props.get(key) != expected:
            raise ValueError("Server properties do not match the disposable fixture")
    api = json.loads((runtime / "api-config.json").read_text(encoding="utf-8"))
    if api["http"] != {"host": "127.0.0.1", "port": 18921} or api["minecraft"]["host"] != "127.0.0.1" or api["minecraft"]["port"] != 25586:
        raise ValueError("API does not target the disposable fixture")
    if api["auth"].get("allowedUsernames") != [BOT]:
        raise ValueError("API must allow only the disposable bot")
    return manifest, props, api


def verify_owners(runtime):
    record = json.loads((runtime / "processes.json").read_text(encoding="utf-8-sig"))
    if Path(record["server_dir"]).resolve() != runtime / "leaves":
        raise ValueError("Process directory does not match")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         "@(Get-NetTCPConnection -LocalPort 25586,25596,18921 | Select-Object LocalPort,OwningProcess -Unique) | ConvertTo-Json -Compress"],
        capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW,
    )
    rows = json.loads(result.stdout or "[]")
    rows = [rows] if isinstance(rows, dict) else rows
    expected = {25586: record["java_pid"], 25596: record["java_pid"], 18921: record["api_pid"]}
    if result.returncode != 0 or {row["LocalPort"]: row["OwningProcess"] for row in rows} != expected:
        raise ValueError("Ports do not belong to the recorded disposable processes")
    return record


def prepare(runtime):
    if runtime.exists():
        raise ValueError("Runtime already exists; refusing to overwrite")
    server = runtime / "leaves"
    server.mkdir(parents=True)
    source = ROOT.parent / "server"
    for name in ("leaves-1.21.11.jar", "cache", "libraries", "versions", "eula.txt"):
        if (source / name).is_dir():
            shutil.copytree(source / name, server / name)
        else:
            shutil.copy2(source / name, server / name)
    rcon_password, api_password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    props = {
        "server-ip": "127.0.0.1", "server-port": 25586, "enable-rcon": "true", "rcon.port": 25596,
        "rcon.password": rcon_password, "broadcast-rcon-to-ops": "false", "enable-query": "false",
        "online-mode": "false", "enforce-secure-profile": "false", "level-name": WORLD,
        "level-type": "minecraft:flat", "level-seed": 778899, "generate-structures": "false",
        "generator-settings": json.dumps({"biome": "minecraft:plains", "layers": [
            {"block": "minecraft:bedrock", "height": 1}, {"block": "minecraft:dirt", "height": 2},
            {"block": "minecraft:grass_block", "height": 1}], "structure_overrides": []}, separators=(",", ":")),
        "difficulty": "peaceful", "gamemode": "survival", "force-gamemode": "true", "spawn-protection": 0,
        "max-players": 2, "view-distance": 4, "simulation-distance": 4, "allow-flight": "true",
        "allow-nether": "false", "white-list": "false", "pause-when-empty-seconds": -1,
        "sync-chunk-writes": "true", "motd": "BotSleep disposable survival acceptance",
    }
    (server / "server.properties").write_text("\n".join(f"{key}={value}" for key, value in props.items()) + "\n", encoding="utf-8")
    (server / "leaves.yml").write_text("settings:\n  misc:\n    auto-update:\n      enable: false\n    bstats-privacy-mode: true\n", encoding="utf-8")
    dump(runtime / "api-config.json", {
        "http": {"host": "127.0.0.1", "port": 18921},
        "minecraft": {"host": "127.0.0.1", "port": 25586, "version": "1.21.11"},
        "auth": {"type": "offline", "password": api_password, "allowedUsernames": [BOT]},
        "readiness": {"type": "direct"},
    })
    config = {
        "mc_api": {"base_url": "http://127.0.0.1:18921/api/v1", "timeout_seconds": 15, "login_timeout_seconds": 40},
        "agent": {"name": BOT, "password": api_password, "mode": "survival"},
        "survival": {"world_id": "isolated-survival-smoke", "state_dir": "state", "scan_radius": 24,
                     "poll_interval_seconds": 0.2, "retry_seconds": 2, "max_steps": 80, "max_runtime_seconds": 900},
        "debug": {"enable_in_game_commands": False, "allowed_usernames": [],
                  "flight_recorder": {"enabled": True, "output_dir": "logs"}},
        "rcon": {"enabled": False},
    }
    (runtime / "survival.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    dump(runtime / "manifest.json", {"purpose": "disposable_survival_acceptance", "destructive_tests_allowed": True,
         "minecraft_port": 25586, "rcon_port": 25596, "api_port": 18921, "username": BOT,
         "server_directory": str(server), "world_name": WORLD, "copied_not_linked": True,
         "java_executable": r"D:\Java-env\jdk-24\bin\java.exe"})
    print("Prepared isolated survival runtime; no service started.", flush=True)


def start(runtime):
    manifest, _, _ = validate(runtime)
    for port in PORTS:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    env = dict(os.environ)
    for key in ("MC_AGENT_PORT", "MC_AGENT_CONFIG"):
        env.pop(key, None)
    record = {"server_dir": str(runtime / "leaves"), "purpose": manifest["purpose"]}
    with (runtime / "leaves.stdout.log").open("w") as out, (runtime / "leaves.stderr.log").open("w") as err:
        java = subprocess.Popen([manifest["java_executable"], "-Xms1G", "-Xmx2G", "-jar", "leaves-1.21.11.jar", "--nogui"],
                                cwd=runtime / "leaves", env=env, stdout=out, stderr=err, creationflags=NO_WINDOW)
    record["java_pid"] = java.pid
    dump(runtime / "processes.json", record)
    with (runtime / "api.stdout.log").open("w") as out, (runtime / "api.stderr.log").open("w") as err:
        api = subprocess.Popen([shutil.which("node"), str(ROOT.parent / "mc-agent-api/src/index.js"), str(runtime / "api-config.json")],
                               cwd=ROOT.parent / "mc-agent-api", env=env, stdout=out, stderr=err, creationflags=NO_WINDOW)
    record["api_pid"] = api.pid
    dump(runtime / "processes.json", record)
    print(json.dumps(record), flush=True)


def episodes(directory):
    result = []
    for path in sorted(Path(directory).rglob("episodes.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                result.append(json.loads(line))
            except ValueError:
                pass  # A concurrent final line may not have been flushed yet.
    return result


def saved_inventory(runtime):
    offline_id = uuid.UUID(bytes=hashlib.md5(("OfflinePlayer:" + BOT).encode()).digest(), version=3)
    file = runtime / "leaves" / WORLD / "playerdata" / f"{offline_id}.dat"
    code = """const fs=require('fs'),nbt=require('prismarine-nbt');
(async()=>{const d=nbt.simplify((await nbt.parse(fs.readFileSync(process.argv[1]))).parsed);
console.log(JSON.stringify({gamemode:d.playerGameType,inventory:d.Inventory||[],pos:d.Pos}));
})().catch(e=>{console.error(e.message);process.exitCode=1});"""
    result = subprocess.run([shutil.which("node"), "-e", code, str(file)], cwd=ROOT.parent / "mc-agent-api",
                            capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW)
    if result.returncode != 0:
        raise RuntimeError("Could not inspect the disposable bot's saved inventory")
    return json.loads(result.stdout)


def run(runtime):
    _, props, api = validate(runtime)
    process_record = verify_owners(runtime)
    rcon = Rcon(props["rcon.password"])
    if "There are 0 of" not in rcon.command("list"):
        raise RuntimeError("The disposable server must be empty before fixture setup")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "passed": False, "run_id": stamp,
              "targets": list(TARGETS), "inventory_grants": False, "fixture": {"oak_logs": 24, "stone": 24, "floor": "dirt"}}
    state_dir = runtime / "state" / stamp
    config = yaml.safe_load((runtime / "survival.yaml").read_text(encoding="utf-8"))
    config["survival"].update(state_dir=str(state_dir), world_id="survival-smoke-" + stamp)
    config["debug"]["flight_recorder"]["output_dir"] = str(runtime / "logs" / stamp)
    config_path = runtime / f"run-{stamp}.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    load_config(config_path, environ={})
    client = None
    process = None
    try:
        for command in (
            "difficulty peaceful", "weather clear", "gamerule minecraft:spawn_mobs false",
            "gamerule minecraft:advance_weather false", "gamerule minecraft:advance_time false",
            "gamerule minecraft:respawn_radius 0", "time set noon", "forceload add -20 -20 20 20",
            "fill -18 99 -18 18 99 18 dirt", "fill -18 100 -18 18 108 18 air",
            "fill 4 100 4 11 102 4 oak_log", "fill -11 100 4 -4 102 4 stone", "setworldspawn 0 100 0",
        ):
            rcon.require(command)
        client = MinecraftAgentClient(config["mc_api"]["base_url"], BOT, api["auth"]["password"])
        client.login(max_wait_seconds=40)
        rcon.require(f"gamemode survival {BOT}")
        rcon.require(f"tp {BOT} 0.5 100 0.5")
        rcon.require(f"clear {BOT}")
        time.sleep(0.4)
        report["initial_inventory"] = client.inventory()["result"]["items"]
        report["initial_inventory_rcon"] = rcon.command(f"data get entity {BOT} Inventory")
        if report["initial_inventory"]:
            raise RuntimeError("Initial inventory is not empty")
        client.logout()
        client = None
        env = dict(os.environ)
        for key in ("MC_AGENT_API", "MC_AGENT_USER", "MC_AGENT_PASSWORD", "BOTSLEEP_CONFIG"):
            env.pop(key, None)
        with (runtime / f"run-{stamp}.stdout.log").open("w", encoding="utf-8") as out, (runtime / f"run-{stamp}.stderr.log").open("w", encoding="utf-8") as err:
            process = subprocess.Popen([sys.executable, "-u", "-B", str(ROOT / "main.py"), "--config", str(config_path),
                                        "--mode", "survival", "--max-steps", "80"], cwd=ROOT, env=env,
                                       stdout=out, stderr=err, creationflags=NO_WINDOW)
        process_record["bot_pid"] = process.pid
        dump(runtime / "processes.json", process_record)
        start_time, seen, last_inventory = time.monotonic(), 0, 0.0
        samples = []
        while process.poll() is None:
            entries = episodes(state_dir)
            for entry in entries[seen:]:
                if entry.get("event") == "STEP_RESULT":
                    item = entry["data"]
                    print(json.dumps({"step": item.get("step"), "action": item["decision"]["name"],
                                      "args": item["decision"]["args"], "success": item["result"]["success"],
                                      "error_code": item["result"].get("error_code"), "paused": item.get("paused")}), flush=True)
                    if item.get("paused"):
                        report["stopped_on_pause"] = True
                        process.terminate()
            seen = len(entries)
            if time.monotonic() - last_inventory >= 2:
                samples.append({"elapsed_seconds": round(time.monotonic() - start_time, 2),
                                "inventory": rcon.command(f"data get entity {BOT} Inventory")})
                last_inventory = time.monotonic()
            if time.monotonic() - start_time > 960:
                report["timed_out"] = True
                process.terminate()
            time.sleep(0.2)
        process.wait(timeout=15)
        if BOT in rcon.command("list"):
            rcon.command(f"kick {BOT} Disposable acceptance finished")
        rcon.command("save-all flush")
        saved = saved_inventory(runtime)
        counts = {}
        for item in saved["inventory"]:
            name = item["id"].removeprefix("minecraft:")
            counts[name] = counts.get(name, 0) + item.get("count", item.get("Count", 0))
        actions = [entry["data"] for entry in episodes(state_dir) if entry.get("event") == "STEP_RESULT"]
        table_crafted = any(item["decision"]["name"] == "craft_item" and item["decision"]["args"].get("item") == "crafting_table" and item["result"]["success"] for item in actions)
        table_placed = any(item["decision"]["name"] == "place_item" and item["decision"]["args"].get("item") == "crafting_table" and item["result"]["success"] for item in actions)
        report.update(returncode=process.returncode, elapsed_seconds=round(time.monotonic() - start_time, 2),
                      saved_inventory=saved, final_counts=counts, inventory_samples=samples, actions=actions,
                      crafting_table_crafted=table_crafted, crafting_table_placed=table_placed,
                      logged_out=BOT not in rcon.command("list"))
        report["passed"] = bool(process.returncode == 0 and all(counts.get(name, 0) >= 1 for name in TARGETS)
                                and table_crafted and table_placed and report["logged_out"])
    except Exception as error:
        message = str(error)
        for secret in (props["rcon.password"], api["auth"]["password"]):
            message = message.replace(secret, "[redacted]")
        report["error"] = message
        print("Live survival failed: " + message, file=sys.stderr, flush=True)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=15)
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass
        try:
            if BOT in rcon.command("list"):
                rcon.command(f"kick {BOT} Disposable acceptance cleanup")
            report["cleanup_players"] = rcon.command("list")
        except Exception:
            report["cleanup_error"] = "RCON cleanup unavailable"
        dump(runtime / f"live_survival_report-{stamp}.json", report)
        dump(runtime / "live_survival_report.json", report)
    print(json.dumps({key: report.get(key) for key in ("passed", "returncode", "final_counts", "elapsed_seconds", "error")}), flush=True)
    return 0 if report["passed"] else 1


def shutdown(runtime):
    _, props, _ = validate(runtime)
    record = verify_owners(runtime)
    rcon = Rcon(props["rcon.password"])
    if BOT in rcon.command("list"):
        raise RuntimeError("Stop the disposable bot before shutdown")
    rcon.command("save-all flush")
    try:
        rcon.command("stop")
    except OSError:
        pass
    # Only the API PID already verified as this runtime's listener is stopped.
    subprocess.run(["taskkill", "/PID", str(record["api_pid"]), "/F"], capture_output=True, creationflags=NO_WINDOW, check=True)
    record["shutdown_requested_at"] = datetime.now(timezone.utc).isoformat()
    dump(runtime / "processes.json", record)
    print("Disposable Minecraft stop requested; disposable API stopped.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    for action in ("prepare", "start", "run", "shutdown"):
        group.add_argument("--" + action, action="store_true")
    args = parser.parse_args()
    runtime = runtime_path(args.runtime)
    if args.prepare:
        prepare(runtime)
    elif args.start:
        start(runtime)
    elif args.run:
        return run(runtime)
    else:
        shutdown(runtime)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
