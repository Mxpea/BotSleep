"""Run N isolated BotSleep agents against a single mc-agent-api >= 1.3.13 endpoint.

mc-agent-api 1.3.13+ isolates each logged-in bot inside its own Node worker
process (roughly 220 MB per bot), removing the need for a row of separate
API ports (18901..18909). One shared API base_url serves all bots while
BotSleep keeps per-bot state_dir, world_id, session_file, logs, and optional
read-only monitor ports strictly isolated.
"""

import argparse
import copy
import math
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.client import MinecraftAgentClient
from core.rcon import RconManager

DEFAULT_SPREAD_GRID: Tuple[Tuple[int, int], ...] = (
    (0, 0),
    (2000, 0),
    (-2000, 0),
    (0, 2000),
    (0, -2000),
    (4000, 0),
    (2000, -2000),
    (-2000, 2000),
    (-2000, -2000),
)


def compute_min_horizontal_distance(positions: Mapping[str, Sequence[float]]) -> float:
    """Return minimum pairwise horizontal distance among logged bot coordinates."""
    names = list(positions)
    if len(names) < 2:
        return math.inf
    return min(
        math.hypot(float(positions[names[i]][0]) - float(positions[names[j]][0]),
                   float(positions[names[i]][2]) - float(positions[names[j]][2]))
        for i in range(len(names))
        for j in range(i + 1, len(names))
    )


def build_spread_grid(count: int, step: int = 2000) -> List[Tuple[int, int]]:
    """Return at least `count` horizontal anchors spaced across distinct map regions."""
    if count <= 0:
        raise ValueError("count must be positive")
    if step == 2000 and count <= len(DEFAULT_SPREAD_GRID):
        return list(DEFAULT_SPREAD_GRID[:count])
    anchors: List[Tuple[int, int]] = []
    ring = 0
    while len(anchors) < count:
        if ring == 0:
            anchors.append((0, 0))
        else:
            for dx in range(-ring, ring + 1):
                for dz in range(-ring, ring + 1):
                    if max(abs(dx), abs(dz)) == ring:
                        anchors.append((dx * step, dz * step))
                        if len(anchors) >= count:
                            return anchors
        ring += 1
    return anchors


def build_bot_config(
    base_cfg: Mapping[str, Any],
    *,
    index: int,
    run_id: str,
    api_base_url: str,
    runtime_root: Path,
    log_root: Path,
    bot_prefix: str = "SleepBot",
    monitor_base_port: int = 18930,
    max_runtime_seconds: Optional[int] = None,
    max_steps: Optional[int] = None,
) -> Tuple[Dict[str, Any], Path, Path]:
    """Build an isolated per-bot YAML payload sharing one single mc-agent-api URL."""
    if index < 1:
        raise ValueError("index must be >= 1")
    cfg = copy.deepcopy(dict(base_cfg))
    bot_name = f"{bot_prefix}{index}"
    state_dir = runtime_root / run_id / f"bot_{index}"
    log_dir = log_root / run_id / f"bot_{index}"
    sess_path = state_dir / f"session_{bot_name}.json"

    agent = cfg.setdefault("agent", {})
    agent["name"] = bot_name

    mc_api = cfg.setdefault("mc_api", {})
    mc_api["base_url"] = api_base_url.rstrip("/")
    mc_api["session_file"] = str(sess_path)

    survival = cfg.setdefault("survival", {})
    survival["state_dir"] = str(state_dir)
    survival["world_id"] = f"{run_id}_bot_{index}"
    if max_runtime_seconds is not None:
        survival["max_runtime_seconds"] = int(max_runtime_seconds)
    if max_steps is not None:
        survival["max_steps"] = int(max_steps)

    debug = cfg.setdefault("debug", {})
    recorder = debug.setdefault("flight_recorder", {})
    recorder["output_dir"] = str(log_dir)
    monitor = debug.setdefault("monitor", {})
    monitor["port"] = int(monitor_base_port) + index
    monitor["console"] = False

    return cfg, state_dir, log_dir


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch multiple BotSleep processes sharing one mc-agent-api port")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"), help="Template YAML config file")
    parser.add_argument("--count", type=int, default=9, help="Number of bots to spawn (default: 9)")
    parser.add_argument("--run-id", default=time.strftime("multi_%Y%m%d_%H%M%S"), help="Run identifier for state/logs")
    parser.add_argument("--api-url", default="http://127.0.0.1:18900/api/v1", help="Single shared mc-agent-api base URL")
    parser.add_argument("--bot-prefix", default="SleepBot", help="Username prefix (default: SleepBot)")
    parser.add_argument("--start-api", action="store_true", help="Also spawn and manage one mc-agent-api server process")
    parser.add_argument("--api-dir", default=str(ROOT.parent / "mc-agent-api"), help="Path to mc-agent-api directory")
    parser.add_argument("--api-config", default=None, help="Optional config JSON path for --start-api")
    parser.add_argument("--spread", action="store_true", help="Spread bots via RCON and clear inventory before launch")
    parser.add_argument("--min-dist", type=float, default=1500.0, help="Minimum horizontal spread distance check (blocks)")
    parser.add_argument("--login-delay", type=float, default=1.0, help="Delay between bot logins in seconds")
    parser.add_argument("--launch-delay", type=float, default=2.0, help="Delay between launching bot subprocesses")
    parser.add_argument("--max-runtime-seconds", type=int, default=None, help="Override survival.max_runtime_seconds")
    parser.add_argument("--max-steps", type=int, default=None, help="Override survival.max_steps")
    parser.add_argument("--keep-connected", action="store_true", help="Pass --keep-connected to each bot worker")
    parser.add_argument("--dry-run", action="store_true", help="Generate per-bot configs without connecting or running")
    return parser.parse_args(argv)


def _prepare_and_spread(
    bot_entries: Sequence[Tuple[int, Dict[str, Any], Path, Path]],
    anchors: Sequence[Tuple[int, int]],
    *,
    spread: bool,
    min_dist: float,
    login_delay: float,
) -> None:
    positions: Dict[str, List[float]] = {}
    rcon = None
    first_rcon = dict(bot_entries[0][1].get("rcon") or {}) if bot_entries else {}
    if spread:
        rcon = RconManager(
            str(first_rcon.get("host") or "127.0.0.1"),
            int(first_rcon.get("port") or 25575),
            str(first_rcon.get("password") or ""),
        )
    try:
        for (idx, cfg, _state_dir, _log_dir), (gx, gz) in zip(bot_entries, anchors):
            agent_cfg = cfg["agent"]
            api_cfg = cfg["mc_api"]
            client = MinecraftAgentClient(
                api_cfg["base_url"],
                username=agent_cfg["name"],
                password=agent_cfg.get("password", ""),
                timeout_seconds=float(api_cfg.get("timeout_seconds", 15)),
            )
            try:
                status = client.login(max_wait_seconds=35, retry_rate_limit=True, max_login_attempts=5)
                if spread and rcon is not None:
                    bot_name = agent_cfg["name"]
                    rcon.command(f"clear {bot_name}")
                    rcon.command(f"xp set {bot_name} 0 levels")
                    rcon.command(f"xp set {bot_name} 0 points")
                    rcon.command(f"effect clear {bot_name}")
                    candidates = [
                        (gx, gz),
                        (gx + 500, gz + 500),
                        (gx - 500, gz - 500),
                        (6000, 0),
                        (-6000, 0),
                        (0, 6000),
                        (0, -6000),
                        (6000, 6000),
                        (-6000, -6000),
                        (6000, -6000),
                        (-6000, 6000),
                    ]
                    st_after: Dict[str, Any] = {}
                    remaining_anchors = [a for a in anchors[idx:]]
                    for cx, cz in candidates:
                        if any(math.hypot(cx - p[0], cz - p[2]) < (min_dist + 300.0) for p in positions.values()):
                            continue
                        if (cx, cz) != (gx, gz) and any(math.hypot(cx - ax, cz - az) < (min_dist + 300.0) for ax, az in remaining_anchors):
                            continue
                        for mr in (32, 256):
                            resp = rcon.command(f"spreadplayers {cx} {cz} 5 {mr} false {bot_name}")
                            if "Spread" in resp:
                                time.sleep(0.8)
                                st_after = client.get_status(timeout_seconds=10).get("result", {})
                                pos = st_after.get("pos") or status.get("pos")
                                if pos and not st_after.get("in_water") and pos[1] >= 62.0:
                                    break
                        if st_after and not st_after.get("in_water") and (st_after.get("pos") or [0, 0, 0])[1] >= 62.0:
                            break
                    pos = st_after.get("pos") or status.get("pos")
                    if pos and len(pos) >= 3:
                        rcon.command(
                            f"spawnpoint {bot_name} {int(math.floor(float(pos[0])))} "
                            f"{int(math.ceil(float(pos[1])))} {int(math.floor(float(pos[2])))}"
                        )
                else:
                    pos = status.get("pos")
                client.save_session(api_cfg["session_file"])
                if pos:
                    positions[agent_cfg["name"]] = list(pos)
                print(f"[{idx}] {agent_cfg['name']} ready on {api_cfg['base_url']} pos={pos}", flush=True)
            finally:
                client.close()
            if login_delay > 0 and idx < len(bot_entries):
                time.sleep(login_delay)
    finally:
        if rcon is not None:
            rcon.disconnect()

    if spread and len(positions) >= 2:
        actual_min = compute_min_horizontal_distance(positions)
        print(f"Verified single-API multi-bot spacing; min_dist={actual_min:.1f}", flush=True)
        if actual_min < min_dist:
            raise SystemExit(f"Spacing check failed: {actual_min:.1f} < {min_dist:.1f}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    base_cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    if not isinstance(base_cfg, dict):
        raise SystemExit("Invalid base config YAML")

    runtime_root = ROOT / "runtime"
    log_root = ROOT / "logs"
    run_cfg_dir = runtime_root / args.run_id / "configs"
    run_cfg_dir.mkdir(parents=True, exist_ok=True)

    anchors = build_spread_grid(args.count)
    bot_entries: List[Tuple[int, Dict[str, Any], Path, Path, Path]] = []
    for idx in range(1, args.count + 1):
        cfg, state_dir, log_dir = build_bot_config(
            base_cfg,
            index=idx,
            run_id=args.run_id,
            api_base_url=args.api_url,
            runtime_root=runtime_root,
            log_root=log_root,
            bot_prefix=args.bot_prefix,
            max_runtime_seconds=args.max_runtime_seconds,
            max_steps=args.max_steps,
        )
        state_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = run_cfg_dir / f"bot_{idx}.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
        bot_entries.append((idx, cfg, state_dir, log_dir, cfg_path))

    if args.dry_run:
        print(f"Prepared {len(bot_entries)} single-API bot configs at {run_cfg_dir} -> {args.api_url}")
        return 0

    api_proc = None
    api_log = None
    bot_procs: List[Tuple[int, subprocess.Popen]] = []
    bot_logs = []
    try:
        if args.start_api:
            api_dir = Path(args.api_dir)
            cmd = ["node", "src/index.js"]
            if args.api_config:
                cmd.append(str(args.api_config))
            api_log = (log_root / args.run_id / "api.log").open("w", encoding="utf-8", buffering=1)
            api_proc = subprocess.Popen(cmd, cwd=str(api_dir), stdout=api_log, stderr=subprocess.STDOUT)
            print(f"Started single mc-agent-api pid={api_proc.pid} url={args.api_url}", flush=True)
            time.sleep(2.5)

        if args.spread or args.login_delay > 0:
            _prepare_and_spread(
                [(i, cfg, sd, ld) for i, cfg, sd, ld, _ in bot_entries],
                anchors,
                spread=args.spread,
                min_dist=args.min_dist,
                login_delay=args.login_delay,
            )

        for idx, _cfg, _sd, log_dir, cfg_path in bot_entries:
            fh = (log_dir / "stdout.log").open("w", encoding="utf-8", buffering=1)
            bot_logs.append(fh)
            cmd = [sys.executable, "-u", "main.py", "--config", str(cfg_path)]
            if args.keep_connected:
                cmd.append("--keep-connected")
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT)
            bot_procs.append((idx, proc))
            print(f"Started {args.bot_prefix}{idx} pid={proc.pid}", flush=True)
            if args.launch_delay > 0 and idx < len(bot_entries):
                time.sleep(args.launch_delay)

        results: Dict[int, int] = {}
        while len(results) < len(bot_procs):
            for idx, proc in bot_procs:
                if idx in results:
                    continue
                rc = proc.poll()
                if rc is not None:
                    results[idx] = rc
                    print(f"{args.bot_prefix}{idx} exited rc={rc}", flush=True)
            time.sleep(2.0)
        print("ALL_DONE", results, flush=True)
        return 0
    finally:
        for _idx, proc in bot_procs:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        if api_proc is not None and api_proc.poll() is None:
            try:
                api_proc.terminate()
            except Exception:
                pass
        for fh in bot_logs:
            try:
                fh.close()
            except Exception:
                pass
        if api_log is not None:
            try:
                api_log.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
