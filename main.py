"""Configured sleep, starter and daily survival runners for mc-agent-api."""

import argparse
import os
import sys
import time
from pathlib import Path

from core.client import APIError, MinecraftAgentClient, connection_retry_delay
from core.config import ConfigError, continuous_daily, load_config
from core.sleep_controller import SleepController
from debug.chat_debugger import ChatDebugger
from debug.logger import FlightRecorder


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="BotSleep automatic sleep and survival runner")
    parser.add_argument("--config", default=os.environ.get("BOTSLEEP_CONFIG", str(Path(__file__).with_name("config.yaml"))))
    parser.add_argument("--username", default=None, help="Override the configured Minecraft username")
    parser.add_argument("--debug", action="store_true", help="Enable allowlisted in-game debug commands")
    parser.add_argument("--radar", action="store_true", help="Start continuous exploration radar on launch")
    parser.add_argument("--mode", choices=("sleep", "survival"), help="Override agent.mode")
    parser.add_argument("--objective", choices=("starter", "daily", "competition"), help="Select starter, daily survival or model-driven competition scoring")
    parser.add_argument("--max-steps", type=int, help="Bound survival observation/action cycles")
    parser.add_argument("--once", action="store_true", help="Run one sleep attempt or survival decision and exit")
    parser.add_argument("--keep-connected", action="store_true", help="Keep the Minecraft player logged in on exit and reuse cached session token across runs")
    parser.add_argument("--new-competition", action="store_true", help="Archive a completed or expired competition and start a fresh round")
    parser.add_argument("--resume", action="store_true", help="Explicitly resume survival from fresh observations; never replay a saved action")
    parser.add_argument("--acknowledge-model-idle", action="store_true", help="Clear model transport uncertainty ONLY after confirming the provider has no running request")
    parser.add_argument("--check-config", action="store_true", help="Validate configuration without connecting")
    return parser.parse_args(argv)


def _connect_continuously(client, session_file, recorder):
    delay = 1
    while True:
        try:
            return client.connect_continuous(session_file=session_file)
        except APIError as error:
            if (error.code not in {"not_connected", "not_ready", "wrong_server", "ready_timeout", "network_error",
                                   "request_timeout", "death_loop", "death_loop_cooldown", "rate_limited"}
                    and error.status_code not in (429, 500, 502, 503, 504)):
                raise
            seconds = connection_retry_delay(error, delay)
            if recorder:
                recorder.record("WARN", "main", "CONNECTION_RETRY", {
                    "phase": "reconnecting", "code": error.code, "retry_in_seconds": seconds})
            time.sleep(seconds)
            delay = min(30, delay * 2)


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
            budget = (config["survival"]["competition"]
                      if config["survival"]["objective"] == "competition" else config["survival"])
            budget["max_steps"] = args.max_steps
        if args.once or args.max_steps is not None:
            config["survival"]["continuous"] = False
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
    instance_lock = None
    login_attempted = False
    keep_connected = False
    session_file = None
    exit_code = 0
    survival = config["agent"]["mode"] == "survival"
    continuous = survival and continuous_daily(config["survival"])
    try:
        keep_connected = bool(
            getattr(args, "keep_connected", False)
            or config["mc_api"].get("keep_session_on_exit", False)
        )
        cfg_session_file = str(config["mc_api"].get("session_file") or "").strip()
        if cfg_session_file:
            session_file = cfg_session_file
        elif keep_connected:
            session_file = str(Path(config["survival"]["state_dir"]) / f"session_{config['agent']['name']}.json")
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
            recorder=recorder,
        )
        if survival:
            from core.survival_state import SurvivalJournal
            journal_guard = SurvivalJournal(
                config["survival"]["state_dir"],
                endpoint=client.base_url,
                username=client.username,
                world_id=config["survival"]["world_id"],
            )
            instance_lock = journal_guard.instance_lock()
            instance_lock.__enter__()
            if config["survival"]["objective"] == "competition":
                journal_guard.prepare_competition_round(config["survival"]["competition"], new=args.new_competition)
        if recorder:
            recorder.record("INFO", "main", "STARTUP", {"username": client.username, "mode": config["agent"]["mode"],
                            "planner": "llm" if survival and config["llm"]["enabled"] else "rules",
                            "objective": config["survival"]["objective"] if survival else None,
                            "continuous": continuous,
                            "model": config["llm"]["model"] if config["llm"]["enabled"] else None,
                            "monitor_url": monitor.address if monitor else None})
        login_attempted = True
        attach_fn = getattr(client, "attach_or_login", None)
        if continuous:
            status = _connect_continuously(client, session_file, recorder)
        elif session_file and callable(attach_fn):
            status = attach_fn(session_file=session_file, max_wait_seconds=config["mc_api"]["login_timeout_seconds"])
        else:
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
                if args.acknowledge_model_idle:
                    planner.brain.request_gate.acknowledge_provider_idle()
                print(f"Model planner: {config['llm']['model']}")
            controller = SurvivalHarness(client, config["survival"], config["sleep"], recorder=recorder, planner=planner)
            if args.resume:
                controller.resume()
            print(controller.summary())
            if args.once and controller.paused and not args.resume:
                exit_code = 1
                print("Survival recovery is paused; use --resume to reobserve and replan before a single step.", file=sys.stderr)
                return exit_code
        else:
            controller = SleepController(client, config["sleep"], recorder=recorder)
        debug_config = config["debug"]
        debugger = None
        if args.debug or debug_config["enable_in_game_commands"]:
            radar_skill = None
            memory = None
            if getattr(args, "radar", False):
                from core.memory import MemoryEngine
                from core.radar import BlockRadar
                from skills.exploration import ContinuousRadarSkill
                memory = MemoryEngine()
                radar = BlockRadar(client)
                radar_skill = ContinuousRadarSkill(client, memory=memory, radar=radar)
                radar_skill.enable()
                print("[Radar] Continuous exploration radar enabled.")
            debugger = ChatDebugger(
                client,
                prefix=debug_config["command_prefix"],
                allowed_usernames=debug_config["allowed_usernames"],
                on_stop=controller.pause,
                on_sleep=controller.request_sleep,
                on_resume=controller.resume if survival else None,
                on_summary=controller.summary if survival else None,
                radar_skill=radar_skill,
                memory=memory,
            )
            if not debug_config["allowed_usernames"]:
                print("Debug enabled; add player names to debug.allowed_usernames to accept commands.")

        if args.once:
            if survival:
                controller.request_step()
            else:
                controller.request_sleep()
        poll_interval = config["survival" if survival else "sleep"]["poll_interval_seconds"]
        # Survival owns action/observation backoff. Poll completed futures promptly
        # without imposing the configured observation interval a second time.
        tick_interval = min(.1, poll_interval) if survival else poll_interval
        next_debug_poll = 0
        next_heartbeat = 0
        while True:
            if debugger and (not survival or time.monotonic() >= next_debug_poll):
                try:
                    debugger.poll_and_dispatch()
                except APIError as exc:
                    if recorder:
                        recorder.record("WARN", "debug", "DEBUG_POLL_FAILED", {"code": exc.code})
                finally:
                    next_debug_poll = time.monotonic() + poll_interval
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
            time.sleep(tick_interval)
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
        if client and login_attempted:
            if keep_connected:
                save_fn = getattr(client, "save_session", None)
                if session_file and callable(save_fn):
                    try:
                        save_fn(session_file)
                    except Exception as exc:
                        print(f"Session save warning: {exc}", file=sys.stderr)
            else:
                if client.token and callable(getattr(client, "disconnect", None)):
                    try:
                        client.disconnect()
                    except Exception as exc:
                        exit_code = 1
                        print(f"Game disconnect failed: {exc}", file=sys.stderr)
                try:
                    client.logout()
                except Exception as exc:
                    exit_code = 1
                    print(f"Logout failed: {exc}", file=sys.stderr)
        if instance_lock is not None:
            try:
                instance_lock.__exit__(None, None, None)
            except Exception as exc:
                exit_code = 1
                print(f"Instance lock release failed: {exc}", file=sys.stderr)
        if recorder:
            recorder.record("INFO", "main", "SHUTDOWN", {"exit_code": exit_code})
        if monitor:
            monitor.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
