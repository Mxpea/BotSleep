"""Command-line calculator and explicit, opt-in real-eye capture."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

from .calculator import (CalculationError, EyeThrow, StrongholdCalculator, _check_jar,
                         _java_tool, JAR_PATH, install_engine)
from .observations import EyeObservationCollector, ObservationError


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as handle:
        temporary = Path(handle.name)
        json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _measurements(document):
    if not isinstance(document, dict):
        raise ValueError("input must be a JSON object with throws or observations")
    if "observations" in document:
        values = [observation["throw"] for observation in document["observations"]]
    else:
        values = document.get("throws", [])
    if not isinstance(values, list):
        raise ValueError("throws must be a JSON array")
    return [EyeThrow(**value) for value in values]


def _capture(args):
    # Validate runtime and session before any login or item consumption.
    _check_jar(JAR_PATH)
    _java_tool("java")
    _java_tool("javac")
    if args.sigma is not None:
        EyeThrow(0, 0, 0, args.sigma)
    session_path = Path(args.session)
    session = _read_json(session_path) if session_path.exists() else {
        "schema_version": 1, "world": args.world, "base_url": args.base.rstrip("/"),
        "observations": [], "attempts": [],
    }
    if session.get("world") != args.world or session.get("base_url") != args.base.rstrip("/"):
        raise ValueError("session world/API differs; use a new session file for a different world")
    if session.get("schema_version") != 1 or not isinstance(session.get("observations"), list) or not isinstance(session.get("attempts"), list):
        raise ValueError("invalid capture session schema")
    if len(_measurements(session)) >= 32:
        raise ValueError("session already contains the maximum of 32 observations")
    username = args.username or os.environ.get("MC_AGENT_USER")
    password = os.environ.get("MC_AGENT_PASSWORD")
    if args.api_config:
        config = _read_json(args.api_config)
        auth = config.get("auth", {})
        names = auth.get("allowedUsernames", [])
        if not username and len(names) == 1:
            username = names[0]
        password = password or auth.get("password")
    if not username or not password:
        raise ValueError("set MC_AGENT_USER/MC_AGENT_PASSWORD, or pass --api-config with a single allowed username")
    from core.client import MinecraftAgentClient
    client = MinecraftAgentClient(base_url=args.base, username=username, password=password)
    attempt = {"time": datetime.now(timezone.utc).isoformat(), "ok": False}
    try:
        client.login(max_wait_seconds=30)
        try:
            observation = EyeObservationCollector(client).capture(sigma_deg=args.sigma)
            # Do not merge repeated throws into falsely independent evidence.
            current = EyeThrow(**observation["throw"])
            previous = _measurements(session)
            if any(math_distance(current, old) < 1 for old in previous):
                raise ObservationError("new observation is too close to an earlier throw; move sideways before another measurement", observation)
            session["observations"].append(observation)
            attempt["ok"] = True
        except (RuntimeError, ValueError, OSError) as error:
            attempt["error"] = str(error)
            attempt["diagnostics"] = getattr(error, "diagnostics", {})
            raise
        finally:
            session["attempts"].append(attempt)
            # Preserve raw evidence even if fitting or the later calculation fails.
            _write_json(session_path, session)
    finally:
        if client.token:
            client.logout()
    result = StrongholdCalculator().calculate(_measurements(session), top_k=args.top)
    return result


def math_distance(first, second):
    return ((first.x - second.x) ** 2 + (first.z - second.z) ** 2) ** 0.5


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ninjabrain Bot calculation core; no GUI or visual aiming")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="download and checksum the pinned official calculator JAR")
    calculate = commands.add_parser("calculate", help="calculate offline from saved throws or a capture session")
    calculate.add_argument("--input", required=True)
    calculate.add_argument("--output")
    calculate.add_argument("--top", type=int, default=5)
    calculate.add_argument("--no-advanced", action="store_true", help="disable upstream nearest-stronghold conditioning")
    capture = commands.add_parser("capture", help="THROWS ONE EYE; logs in, samples trajectory, saves session, logs out")
    capture.add_argument("--session", required=True)
    capture.add_argument("--world", required=True, help="label for this world; never mix separate worlds in one session")
    capture.add_argument("--base", default="http://127.0.0.1:18900/api/v1")
    capture.add_argument("--username")
    capture.add_argument("--api-config", help="explicit local API config path; credentials are never written to output")
    capture.add_argument("--sigma", type=float, help="angular standard deviation in degrees; otherwise estimate from coordinate precision")
    capture.add_argument("--top", type=int, default=5)
    capture.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        if args.command == "setup":
            print(json.dumps({"jar": str(install_engine()), "verified": True}))
            return 0
        if args.output:
            source = args.session if args.command == "capture" else args.input
            if Path(args.output).resolve() == Path(source).resolve():
                raise ValueError("output must not overwrite the source observations or capture session")
        if args.command == "capture":
            if not 1 <= args.top <= 100:
                raise ValueError("--top must be between 1 and 100")
            result = _capture(args)
        else:
            result = StrongholdCalculator().calculate(_measurements(_read_json(args.input)), args.top, not args.no_advanced)
        if args.output:
            _write_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (RuntimeError, ValueError, TypeError, KeyError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
