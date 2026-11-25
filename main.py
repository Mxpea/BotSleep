"""BotSleep main entrypoint (v2 L0-L4 reactive Behavior Tree + stateless Task architecture)."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from v2.app import run_v2_session
from v2.kernel.config import ConfigError, load_config


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="BotSleep v2 automatic sleep and survival runner")
    parser.add_argument("--config", default=os.environ.get("BOTSLEEP_CONFIG", str(Path(__file__).with_name("config.yaml"))))
    parser.add_argument("--username", default=None, help="Override the configured Minecraft username")
    parser.add_argument("--debug", action="store_true", help="Enable allowlisted in-game debug commands")
    parser.add_argument("--radar", action="store_true", help="Legacy compatibility flag (no-op in v2 unified gateway)")
    parser.add_argument("--mode", choices=("sleep", "survival"), help="Override agent.mode")
    parser.add_argument("--objective", choices=("starter", "daily", "competition"), help="Select starter, daily survival or competition scoring")
    parser.add_argument("--max-steps", type=int, help="Bound survival observation/action cycles")
    parser.add_argument("--once", action="store_true", help="Run one sleep attempt or survival macro-tick and exit")
    parser.add_argument("--keep-connected", action="store_true", help="Keep the Minecraft player logged in on exit and reuse cached session token")
    parser.add_argument("--new-competition", action="store_true", help="Start a fresh competition round")
    parser.add_argument("--resume", action="store_true", help="Explicitly resume survival from fresh observations; never replay unknown writes")
    parser.add_argument("--acknowledge-model-idle", action="store_true", help="Clear model transport uncertainty after confirming provider idle")
    parser.add_argument("--check-config", action="store_true", help="Validate configuration without connecting")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config, username=args.username)
        if args.mode:
            config["agent"]["mode"] = args.mode
        if args.objective:
            if config["agent"]["mode"] != "survival":
                raise ConfigError("--objective is only available in survival mode")
            config["survival"]["objective"] = args.objective
        if config["survival"]["objective"] == "competition":
            if config["agent"]["mode"] != "survival" or not config["llm"]["enabled"]:
                raise ConfigError("competition requires survival mode and llm.enabled=true")
            if args.debug or config["debug"]["enable_in_game_commands"]:
                raise ConfigError("competition disables in-game debug commands")
        if args.acknowledge_model_idle and (config["agent"]["mode"] != "survival" or not config["llm"]["enabled"]):
            raise ConfigError("--acknowledge-model-idle requires model survival mode")
        if args.new_competition and (args.resume or config["survival"]["objective"] != "competition"):
            raise ConfigError("--new-competition requires competition mode and cannot be combined with --resume")
        if args.resume and config["agent"]["mode"] != "survival":
            raise ConfigError("--resume is only available in survival mode")
        if args.max_steps is not None:
            if not 1 <= args.max_steps <= 10000:
                raise ConfigError("--max-steps must be between 1 and 10000")
            budget = (
                config["survival"]["competition"]
                if config["survival"]["objective"] == "competition"
                else config["survival"]
            )
            budget["max_steps"] = args.max_steps
        if args.once or args.max_steps is not None:
            config["survival"]["continuous"] = False
    except (ConfigError, OSError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    if args.check_config:
        print("Configuration valid.")
        return 0

    return run_v2_session(
        config,
        once=args.once,
        keep_connected=args.keep_connected,
        resume=args.resume,
        new_competition=args.new_competition,
        acknowledge_model_idle=args.acknowledge_model_idle,
        debug_commands=args.debug,
        max_ticks_override=args.max_steps,
    )


if __name__ == "__main__":
    raise SystemExit(main())
