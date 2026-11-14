"""Configured sleep and early-survival runners for mc-agent-api."""

import argparse
import os
import sys
import time
from pathlib import Path

from core.client import APIError, MinecraftAgentClient
from core.config import ConfigError, load_config
from core.sleep_controller import SleepController
from debug.chat_debugger import ChatDebugger
from debug.logger import FlightRecorder


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="BotSleep automatic sleep and starter survival runner")
    parser.add_argument("--config", default=os.environ.get("BOTSLEEP_CONFIG", str(Path(__file__).with_name("config.yaml"))))
    parser.add_argument("--username", default=None, help="Override the configured Minecraft username")
    parser.add_argument("--debug", action="store_true", help="Enable allowlisted in-game debug commands")
    parser.add_argument("--mode", choices=("sleep", "survival"), help="Override agent.mode")
    parser.add_argument("--max-steps", type=int, help="Bound survival observation/action cycles")
    parser.add_argument("--once", action="store_true", help="Run one sleep attempt or survival decision and exit")
    parser.add_argument("--check-config", action="store_true", help="Validate configuration without connecting")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config, username=args.username)
        if args.mode:
            config["agent"]["mode"] = args.mode
        if args.max_steps is not None:
            if not 1 <= args.max_steps <= 10000:
                raise ConfigError("--max-steps must be between 1 and 10000")
            config["survival"]["max_steps"] = args.max_steps
    except (ConfigError, OSError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    if args.check_config:
        print("Configuration valid.")
        return 0

    recorder = None
    monitor = None
    client = None
    controller = None
    exit_code = 0
    survival = config["agent"]["mode"] == "survival"
    try:
        log_config = config["debug"]["flight_recorder"]
        monitor_config = config["debug"]["monitor"]
        if monitor_config["enabled"]:
            from debug.monitor import MonitorServer
            monitor = MonitorServer(host=monitor_config["host"], port=monitor_config["port"]).start()
            print(f"Live monitor: {monitor.address}")
        if log_config["enabled"] or monitor or monitor_config["console"]:
            recorder = FlightRecorder(log_config["output_dir"], enabled=log_config["enabled"],
                                      secrets=(config["agent"]["password"], config["llm"]["api_key"]),
                                      sink=monitor.publish if monitor else None, console=monitor_config["console"])
        client = MinecraftAgentClient(
            base_url=config["mc_api"]["base_url"],
            username=config["agent"]["name"],
            password=config["agent"]["password"],
            timeout_seconds=config["mc_api"]["timeout_seconds"],
        )
        if recorder:
            recorder.record("INFO", "main", "STARTUP", {"username": client.username, "mode": config["agent"]["mode"],
                            "planner": "llm" if survival and config["llm"]["enabled"] else "rules",
                            "model": config["llm"]["model"] if config["llm"]["enabled"] else None,
                            "monitor_url": monitor.address if monitor else None})
        status = client.login(max_wait_seconds=config["mc_api"]["login_timeout_seconds"])
        if recorder:
            recorder.add_secret(client.token)
            recorder.record("INFO", "main", "CONNECTED", {"status": status})
        print(f"Bot '{client.username}' ready at {status.get('pos')} ({status.get('dimension')}).")
        if survival:
            from core.survival_harness import SurvivalHarness
            planner = None
            if config["llm"]["enabled"]:
                from agent.llm_planner import LLMSurvivalPlanner
                planner = LLMSurvivalPlanner(config["llm"], config["survival"], recorder=recorder)
                print(f"Model planner: {config['llm']['model']}")
            controller = SurvivalHarness(client, config["survival"], config["sleep"], recorder=recorder, planner=planner)
            print(controller.summary())
        else:
            controller = SleepController(client, config["sleep"], recorder=recorder)
        debug_config = config["debug"]
        debugger = None
        if args.debug or debug_config["enable_in_game_commands"]:
            debugger = ChatDebugger(
                client,
                prefix=debug_config["command_prefix"],
                allowed_usernames=debug_config["allowed_usernames"],
                on_stop=controller.pause,
                on_sleep=controller.request_sleep,
                on_resume=controller.resume if survival else None,
                on_summary=controller.summary if survival else None,
            )
            if not debug_config["allowed_usernames"]:
                print("Debug enabled; add player names to debug.allowed_usernames to accept commands.")

        if args.once:
            if survival:
                controller.request_step()
            else:
                controller.request_sleep()
        next_heartbeat = 0
        while True:
            if debugger:
                try:
                    debugger.poll_and_dispatch()
                except APIError as exc:
                    if recorder:
                        recorder.record("WARN", "debug", "DEBUG_POLL_FAILED", {"code": exc.code})
            result = controller.tick(automatic=not args.once)
            if monitor and time.monotonic() >= next_heartbeat:
                next_heartbeat = time.monotonic() + monitor_config["heartbeat_seconds"]
                try:
                    live_status = client.get_status()["result"]
                    heartbeat = controller.snapshot(live_status) if survival else {"status": live_status, "busy": controller.busy}
                    heartbeat.update(username=client.username,
                                     model=config["llm"]["model"] if config["llm"]["enabled"] else None)
                    recorder.record("INFO", "main", "HEARTBEAT", heartbeat)
                except APIError as exc:
                    recorder.record("WARN", "main", "HEARTBEAT_FAILED", {"code": exc.code})
            if result is not None:
                print(f"{'Survival' if survival else 'Sleep'} {'OK' if result.success else 'FAILED'}: {result.message}"
                      + (f" [{result.error_code}]" if result.error_code else ""))
                if args.once:
                    exit_code = 0 if result.success else 1
                    break
            if survival and controller.finished:
                exit_code = 0 if controller.completed else 2
                print(controller.summary())
                break
            time.sleep(config["survival" if survival else "sleep"]["poll_interval_seconds"])
    except KeyboardInterrupt:
        print("Stopping BotSleep...")
    except Exception as exc:
        exit_code = 1
        print(f"BotSleep failed: {exc}", file=sys.stderr)
        if recorder:
            recorder.record("ERROR", "main", "CRASH", {"error": str(exc)})
    finally:
        # Cleanup steps are independent: a failed stop must not skip logout.
        if controller:
            try:
                controller.close()
            except Exception as exc:
                exit_code = 1
                print(f"Runner cleanup failed: {exc}", file=sys.stderr)
        if client:
            try:
                client.logout()
            except Exception as exc:
                exit_code = 1
                print(f"Logout failed: {exc}", file=sys.stderr)
        if recorder:
            recorder.record("INFO", "main", "SHUTDOWN", {"exit_code": exit_code})
        if monitor:
            monitor.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
