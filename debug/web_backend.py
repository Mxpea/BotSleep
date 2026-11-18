"""Runtime host, worker stack sampler, guarded controls, and config tuner for the Gradio web console."""

from __future__ import annotations

import copy
import json
import linecache
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple

import yaml

from core.client import APIError, MinecraftAgentClient
from core.config import ConfigError, DEFAULTS, load_config, validate_supplied_config
from core.model_gate import ModelGateError, ModelRequestGate
from core.radar import BlockRadar
from core.skill_registry import SKILL_REGISTRY, resolve_skill_runner
from core.sleep_controller import SleepController
from core.survival_state import SurvivalInstanceError, SurvivalJournal
from debug.logger import FlightRecorder
from debug.monitor import EventMonitor, MonitorServer
from debug.scene_3d import Scene3DCollector
from skills.sleep import AutoSleepSkill

REPO_ROOT = Path(__file__).resolve().parent.parent

STRUCTURAL_PATHS: Tuple[Tuple[str, ...], ...] = (
    ("mc_api", "base_url"),
    ("agent", "name"),
    ("agent", "mode"),
    ("survival", "objective"),
    ("survival", "intent"),
    ("survival", "operator_goal"),
    ("survival", "world_id"),
    ("survival", "state_dir"),
    ("llm", "enabled"),
    ("llm", "base_url"),
    ("llm", "tls_version"),
)


def _deep_get(mapping: Mapping[str, Any], path: Sequence[str], default: Any = None) -> Any:
    cur: Any = mapping
    for key in path:
        if not isinstance(cur, Mapping) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _deep_set(mapping: Dict[str, Any], path: Sequence[str], value: Any) -> None:
    cur = mapping
    for key in path[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[path[-1]] = value


def _deep_merge_updates(base: Dict[str, Any], updates: Mapping[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, val in updates.items():
        if isinstance(val, Mapping) and isinstance(out.get(key), dict):
            out[key] = _deep_merge_updates(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def sample_thread_frames(
    thread_specs: List[Tuple[str, Optional[int]]],
    *,
    repo_root: Path = REPO_ROOT,
    max_frames: int = 16,
) -> List[Dict[str, Any]]:
    """Capture live Python stack frames for tracked worker threads without blocking them."""
    frames_by_tid = sys._current_frames()
    root_resolved = repo_root.resolve()
    snapshots: List[Dict[str, Any]] = []

    for label, tid in thread_specs:
        if tid is None:
            continue
        frame = frames_by_tid.get(tid)
        if frame is None:
            continue
        raw_frames: List[Dict[str, Any]] = []
        cur = frame
        while cur is not None and len(raw_frames) < 64:
            code = cur.f_code
            filename = code.co_filename
            lineno = cur.f_lineno
            func_name = code.co_name
            fpath = Path(filename)
            in_repo = False
            display_path = filename
            try:
                rel = fpath.resolve().relative_to(root_resolved)
                in_repo = True
                display_path = rel.as_posix()
            except Exception:
                in_repo = False
            src_line = linecache.getline(filename, lineno).strip()
            raw_frames.append({
                "file": display_path,
                "line": lineno,
                "function": func_name,
                "code": src_line[:180],
                "in_repo": in_repo,
            })
            cur = cur.f_back

        # raw_frames goes innermost -> outermost. Show innermost repo frame first and bounded tail.
        raw_frames.reverse()
        trimmed = raw_frames[-max_frames:]
        repo_frames = [item for item in trimmed if item["in_repo"]]
        current_repo = repo_frames[-1] if repo_frames else (trimmed[-1] if trimmed else None)
        snapshots.append({
            "thread": label,
            "tid": tid,
            "current": current_repo,
            "frames": trimmed,
        })
    return snapshots


class WebConsoleRuntime:
    """Manages BotSleep lifecycle, telemetry projection, manual controls, and live config tuning."""

    def __init__(
        self,
        config_path: str | Path = "config.yaml",
        *,
        username: Optional[str] = None,
        config_overrides: Optional[Mapping[str, Any]] = None,
        client_factory: Optional[Callable[[Dict[str, Any]], Any]] = None,
        harness_factory: Optional[Callable[..., Any]] = None,
        planner_factory: Optional[Callable[..., Any]] = None,
        world_3d_base_dir: str | Path = "runtime/world_3d",
    ) -> None:
        self.config_path = Path(config_path).expanduser().resolve()
        self.username_override = username
        self.config_overrides = copy.deepcopy(dict(config_overrides)) if config_overrides else None
        self._client_factory = client_factory
        self._harness_factory = harness_factory
        self._planner_factory = planner_factory

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._tick_thread: Optional[threading.Thread] = None

        self.config: Dict[str, Any] = self._load_effective_config()
        self.mode: str = "stopped"  # stopped | host | readonly_attach | disconnected
        self.connect_error: Optional[str] = None
        self.restart_required: bool = False
        self.last_action_notice: str = ""

        self.event_monitor = EventMonitor(max_events=500)
        self.monitor_server: Optional[MonitorServer] = None
        self.recorder: Optional[FlightRecorder] = None
        self.client: Any = None
        self.controller: Any = None
        self.planner: Any = None
        self.journal_guard: Optional[SurvivalJournal] = None
        self._instance_lock_cm: Any = None
        self._keep_connected: bool = False
        self._session_file: Optional[str] = None
        self._login_attempted: bool = False

        self.scene_collector = Scene3DCollector(world_3d_base_dir=world_3d_base_dir)
        self._manual_observation: Optional[Dict[str, Any]] = None
        self._latest_radar: Dict[str, Any] = {}
        self._chat_history: List[Dict[str, Any]] = []
        self._chat_seq: int = 0
        self._last_chat_poll: float = 0.0
        self._last_heartbeat_at: float = 0.0
        self._readonly_last_event_seq: int = 0
        self._readonly_episode_offset: int = 0
        self._readonly_monitor_state: Optional[Dict[str, Any]] = None

        self._tracked_phase: str = "idle"
        self._tracked_phase_since: float = time.monotonic()
        self._last_applied_structural: Tuple[Any, ...] = self._structural_signature(self.config)

    def _load_effective_config(self) -> Dict[str, Any]:
        cfg = load_config(self.config_path, username=self.username_override)
        if self.config_overrides:
            cfg = _deep_merge_updates(cfg, self.config_overrides)
        return cfg

    @staticmethod
    def _structural_signature(cfg: Mapping[str, Any]) -> Tuple[Any, ...]:
        return tuple(_deep_get(cfg, path) for path in STRUCTURAL_PATHS)

    def _init_telemetry(self) -> None:
        log_cfg = self.config["debug"]["flight_recorder"]
        mon_cfg = self.config["debug"]["monitor"]
        if mon_cfg["enabled"] and self.monitor_server is None:
            try:
                self.monitor_server = MonitorServer(host=mon_cfg["host"], port=mon_cfg["port"]).start()
                self.event_monitor = self.monitor_server.events
            except OSError as exc:
                self.last_action_notice = f"只读监控端口 {mon_cfg['port']} 占用，控制台将使用内置事件总线 ({exc})"
                self.monitor_server = None
        secrets = (self.config["agent"]["password"], self.config["llm"]["api_key"])
        self.recorder = FlightRecorder(
            log_cfg["output_dir"],
            enabled=log_cfg["enabled"],
            secrets=secrets,
            sink=self.event_monitor.publish,
            console=mon_cfg["console"],
        )

    def _build_client(self, cfg: Mapping[str, Any]) -> Any:
        if self._client_factory is not None:
            return self._client_factory(dict(cfg))
        return MinecraftAgentClient(
            base_url=cfg["mc_api"]["base_url"],
            username=cfg["agent"]["name"],
            password=cfg["agent"]["password"],
            timeout_seconds=cfg["mc_api"]["timeout_seconds"],
        )

    def _build_planner(self, cfg: Mapping[str, Any]) -> Any:
        if not cfg["llm"]["enabled"]:
            return None
        if self._planner_factory is not None:
            return self._planner_factory(cfg["llm"], cfg["survival"], recorder=self.recorder)
        from agent.llm_planner import LLMSurvivalPlanner
        return LLMSurvivalPlanner(cfg["llm"], cfg["survival"], recorder=self.recorder)

    def _build_controller(self, cfg: Mapping[str, Any], client: Any, planner: Any) -> Any:
        if cfg["agent"]["mode"] == "survival":
            if self._harness_factory is not None:
                return self._harness_factory(
                    client, cfg["survival"], cfg["sleep"], recorder=self.recorder, planner=planner
                )
            from core.survival_harness import SurvivalHarness
            return SurvivalHarness(
                client, cfg["survival"], cfg["sleep"], recorder=self.recorder, planner=planner
            )
        return SleepController(client, cfg["sleep"], recorder=self.recorder)

    def start(
        self,
        *,
        auto_connect: bool = True,
        start_paused: bool = False,
        resume: bool = False,
        acknowledge_model_idle: bool = False,
    ) -> Dict[str, Any]:
        """Initialize runtime in host mode, or fall back to readonly_attach if scope is locked."""
        with self._lock:
            self.config = self._load_effective_config()
            self._last_applied_structural = self._structural_signature(self.config)
            self.restart_required = False
            self._init_telemetry()
            self.connect_error = None

            survival = self.config["agent"]["mode"] == "survival"
            if survival:
                self.journal_guard = SurvivalJournal(
                    self.config["survival"]["state_dir"],
                    endpoint=self.config["mc_api"]["base_url"],
                    username=self.config["agent"]["name"],
                    world_id=self.config["survival"]["world_id"],
                )
                inst = self.journal_guard.instance_status()
                if inst.get("locked"):
                    self.mode = "readonly_attach"
                    self.last_action_notice = (
                        f"检测到同作用域实例已在运行 (PID={inst.get('holder_pid')})，已进入只读附加观察模式"
                    )
                    self._ensure_tick_thread()
                    return self.status_report()
            else:
                self.journal_guard = None

            if not auto_connect:
                self.mode = "disconnected"
                self.last_action_notice = "控制台已就绪（未连接游戏，点击「启动/加入游戏」即可登录）"
                return self.status_report()

            if survival and self.journal_guard is not None:
                try:
                    self._instance_lock_cm = self.journal_guard.instance_lock()
                    self._instance_lock_cm.__enter__()
                except SurvivalInstanceError as exc:
                    self._instance_lock_cm = None
                    self.mode = "readonly_attach"
                    self.last_action_notice = f"实例锁已被占用 ({exc})，已自动降级为只读观察模式"
                    self._ensure_tick_thread()
                    return self.status_report()

            ok, err = self._connect_and_attach_runner(
                start_paused=start_paused,
                resume=resume,
                acknowledge_model_idle=acknowledge_model_idle,
            )
            if not ok:
                self._release_instance_lock()
                self.mode = "disconnected"
                self.connect_error = err
                self.last_action_notice = f"未连接到游戏：{err}"
            else:
                self.mode = "host"
                self._ensure_tick_thread()
            return self.status_report()

    def _release_instance_lock(self) -> None:
        if self._instance_lock_cm is not None:
            try:
                self._instance_lock_cm.__exit__(None, None, None)
            except Exception:
                pass
            self._instance_lock_cm = None

    def _connect_and_attach_runner(
        self,
        *,
        start_paused: bool = False,
        resume: bool = False,
        acknowledge_model_idle: bool = False,
    ) -> Tuple[bool, Optional[str]]:
        cfg = self.config
        survival = cfg["agent"]["mode"] == "survival"
        self._keep_connected = bool(cfg["mc_api"].get("keep_session_on_exit", False))
        cfg_sf = str(cfg["mc_api"].get("session_file") or "").strip()
        if cfg_sf:
            self._session_file = cfg_sf
        elif self._keep_connected:
            self._session_file = str(Path(cfg["survival"]["state_dir"]) / f"session_{cfg['agent']['name']}.json")
        else:
            self._session_file = None

        client = self._build_client(cfg)
        self.client = client
        self._login_attempted = True
        try:
            attach_fn = getattr(client, "attach_or_login", None)
            if self._session_file and callable(attach_fn):
                status = attach_fn(
                    session_file=self._session_file,
                    max_wait_seconds=cfg["mc_api"]["login_timeout_seconds"],
                )
            else:
                status = client.login(max_wait_seconds=cfg["mc_api"]["login_timeout_seconds"])
        except Exception as exc:
            self._login_attempted = False
            self.client = None
            err_code = getattr(exc, "code", None)
            if err_code in {"network_error", "request_timeout"}:
                return False, (
                    f"无法连接 mc-agent-api ({cfg['mc_api']['base_url']})：{exc}。"
                    "请确认已启动 mc-agent-api（仅启动 MC 服务端 25565 无法登录）"
                )
            return False, str(exc)

        if self.recorder:
            if getattr(client, "token", None):
                self.recorder.add_secret(client.token)
            self.recorder.record(
                "INFO",
                "main",
                "STARTUP",
                {
                    "username": client.username,
                    "mode": cfg["agent"]["mode"],
                    "planner": "llm" if survival and cfg["llm"]["enabled"] else "rules",
                    "objective": cfg["survival"]["objective"] if survival else None,
                    "model": cfg["llm"]["model"] if cfg["llm"]["enabled"] else None,
                    "web_console": True,
                },
            )
            self.recorder.record("INFO", "main", "CONNECTED", {"status": status})

        self.planner = self._build_planner(cfg) if survival else None
        if acknowledge_model_idle and self.planner is not None:
            gate = getattr(getattr(self.planner, "brain", None), "request_gate", None)
            if gate is not None:
                gate.acknowledge_provider_idle()
        self.controller = self._build_controller(cfg, client, self.planner)
        if resume and survival and self.controller is not None and not start_paused:
            self.controller.resume()
        if start_paused and self.controller is not None:
            if survival:
                self.controller.pause("web_console_start_paused")
            else:
                self.controller.pause()
        self.scene_collector.record_bot_pos(status)
        self.connect_error = None
        self.restart_required = False
        self._last_applied_structural = self._structural_signature(cfg)
        self._chat_seq = 0
        return True, None

    def connect_or_restart(
        self,
        *,
        start_paused: bool = False,
        mode: Optional[str] = None,
        objective: Optional[str] = None,
        username: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Connect/join game (if disconnected) or restart controller/planner."""
        with self._lock:
            if self.mode == "readonly_attach":
                return False, "当前处于只读附加模式（外部进程正在运行同作用域 Bot），无法接管或重启"
            prev_scope = (
                self.config["mc_api"]["base_url"],
                self.config["agent"]["name"],
                self.config["survival"]["world_id"],
                self.config["agent"]["mode"],
            )
            candidate_cfg = copy.deepcopy(self.config)
            if mode in {"sleep", "survival"}:
                candidate_cfg["agent"]["mode"] = mode
            if objective in {"starter", "daily", "competition"}:
                candidate_cfg["survival"]["objective"] = objective
            if isinstance(username, str) and username.strip():
                candidate_cfg["agent"]["name"] = username.strip()
                self.username_override = username.strip()
            try:
                validated = validate_supplied_config(
                    candidate_cfg,
                    self.config_path.parent,
                    environ={},
                    username=candidate_cfg["agent"]["name"],
                )
            except ConfigError as exc:
                return False, f"启动参数无效：{exc}"

            self._teardown_runner(logout=not self._keep_connected)
            new_scope = (
                validated["mc_api"]["base_url"],
                validated["agent"]["name"],
                validated["survival"]["world_id"],
                validated["agent"]["mode"],
            )
            if new_scope != prev_scope or validated["agent"]["mode"] != "survival":
                self._release_instance_lock()
            self.config = validated

            if self.config["agent"]["mode"] == "survival":
                self.journal_guard = SurvivalJournal(
                    self.config["survival"]["state_dir"],
                    endpoint=self.config["mc_api"]["base_url"],
                    username=self.config["agent"]["name"],
                    world_id=self.config["survival"]["world_id"],
                )
                if self._instance_lock_cm is None:
                    inst = self.journal_guard.instance_status()
                    if inst.get("locked"):
                        self.mode = "readonly_attach"
                        return False, f"实例锁已被 PID={inst.get('holder_pid')} 占用，已切换只读观察"
                    self._instance_lock_cm = self.journal_guard.instance_lock()
                    self._instance_lock_cm.__enter__()
            else:
                self.journal_guard = None

            ok, err = self._connect_and_attach_runner(start_paused=start_paused)
            if not ok:
                self._release_instance_lock()
                self.mode = "disconnected"
                self.connect_error = err
                return False, f"加入游戏失败：{err}"
            self.mode = "host"
            self._ensure_tick_thread()
            state_txt = "（已暂停，等待手动/恢复指令）" if start_paused else "（AI 运行中）"
            self.last_action_notice = f"Bot {self.client.username} 已加入游戏 {state_txt}"
            return True, self.last_action_notice

    def disconnect_from_game(self, *, force_logout: bool = True) -> Tuple[bool, str]:
        """Gracefully pause, detach runner, logout from mc-agent-api, and release scope lock."""
        with self._lock:
            if self.mode == "readonly_attach":
                return False, "当前处于只读附加模式，不能断开外部运行中的 Bot"
            if self.mode == "disconnected" and self.client is None:
                return False, "当前未连接到游戏"
            self._teardown_runner(logout=force_logout or (not self._keep_connected))
            self._release_instance_lock()
            self.mode = "disconnected"
            self.connect_error = None
            self.restart_required = False
            if self.recorder:
                self.recorder.record("INFO", "web_console", "WEB_DISCONNECTED", {"logout": force_logout})
            self.last_action_notice = "Bot 已断开并登出游戏，可重新配置后点击「启动/加入游戏」"
            return True, self.last_action_notice

    def _ensure_tick_thread(self) -> None:
        if self._tick_thread is not None and self._tick_thread.is_alive():
            return
        self._stop_event.clear()
        self._tick_thread = threading.Thread(target=self._tick_loop, daemon=True, name="BotSleepWebLoop")
        self._tick_thread.start()

    def _tick_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self.mode == "readonly_attach":
                    self._poll_readonly_attach()
                    self._stop_event.wait(0.8)
                    continue
                if self.mode != "host" or self.controller is None:
                    self._stop_event.wait(0.4)
                    continue

                survival = self.config["agent"]["mode"] == "survival"
                poll_interval = float(
                    self.config["survival" if survival else "sleep"]["poll_interval_seconds"]
                )
                tick_interval = min(0.1, poll_interval) if survival else max(0.1, min(0.5, poll_interval))

                with self._lock:
                    controller = self.controller
                    client = self.client
                    cfg = self.config

                if controller is not None:
                    result = controller.tick(automatic=True)
                    if result is not None and self.recorder:
                        self.recorder.record(
                            "INFO" if result.success else "WARN",
                            "web_console",
                            "TICK_SUMMARY",
                            {"success": result.success, "message": result.message, "error_code": result.error_code},
                        )
                    cur_phase = getattr(controller, "phase", "idle")
                    if cur_phase != self._tracked_phase:
                        self._tracked_phase = cur_phase
                        self._tracked_phase_since = time.monotonic()

                now = time.monotonic()
                hb_interval = float(cfg["debug"]["monitor"].get("heartbeat_seconds", 2))
                if client is not None and now - self._last_heartbeat_at >= hb_interval:
                    self._last_heartbeat_at = now
                    try:
                        live_status = client.get_status(timeout_seconds=2.0)["result"]
                        self.scene_collector.record_bot_pos(live_status)
                        if controller is not None:
                            if survival:
                                hb = controller.snapshot(live_status)
                            else:
                                hb = {"status": live_status, "busy": controller.busy}
                            hb.update(
                                username=client.username,
                                mode=cfg["agent"]["mode"],
                                model=cfg["llm"]["model"] if cfg["llm"]["enabled"] else None,
                            )
                            if self.recorder:
                                self.recorder.record("INFO", "main", "HEARTBEAT", hb)
                    except APIError as exc:
                        if self.recorder:
                            self.recorder.record("WARN", "main", "HEARTBEAT_FAILED", {"code": exc.code})
                    except Exception:
                        pass

                if client is not None and now - self._last_chat_poll >= 1.2:
                    self._last_chat_poll = now
                    self._poll_chat_once(client)

                self._stop_event.wait(tick_interval)
            except Exception as exc:
                if self.recorder:
                    self.recorder.record("ERROR", "web_console", "LOOP_ERROR", {"error": str(exc)})
                self._stop_event.wait(1.0)

    def _poll_readonly_attach(self) -> None:
        # 1. Try local monitor HTTP if enabled
        mon_cfg = self.config["debug"]["monitor"]
        mon_url = f"http://{mon_cfg['host']}:{mon_cfg['port']}"
        try:
            with urllib.request.urlopen(f"{mon_url}/api/state", timeout=0.6) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, dict) and data.get("has_data"):
                    self._readonly_monitor_state = data
                    self.scene_collector.record_bot_pos(data.get("status"))
            with urllib.request.urlopen(
                f"{mon_url}/api/events?since={self._readonly_last_event_seq}", timeout=0.6
            ) as resp:
                ev_data = json.loads(resp.read().decode("utf-8"))
                for row in ev_data.get("events") or ():
                    if isinstance(row, dict):
                        self._readonly_last_event_seq = max(self._readonly_last_event_seq, int(row.get("seq", 0)))
                        self.event_monitor.publish(row)
        except (urllib.error.URLError, OSError, ValueError):
            pass

        # 2. Also tail journal checkpoint/episodes from disk
        if self.journal_guard is not None:
            try:
                cp = self.journal_guard.path
                if cp.is_file():
                    loaded = json.loads(cp.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict) and loaded.get("version") == 1:
                        self.journal_guard.state = loaded
                ep = self.journal_guard.events_path
                if ep.is_file():
                    size = ep.stat().st_size
                    if size < self._readonly_episode_offset:
                        self._readonly_episode_offset = 0
                    if size > self._readonly_episode_offset:
                        with ep.open("r", encoding="utf-8") as fh:
                            fh.seek(self._readonly_episode_offset)
                            for line in fh:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    rec = json.loads(line)
                                    self.event_monitor.publish({
                                        "timestamp": rec.get("timestamp"),
                                        "level": "INFO",
                                        "module": "journal",
                                        "event": rec.get("event", "EPISODE"),
                                        "data": rec.get("data", {}),
                                    })
                                except ValueError:
                                    pass
                            self._readonly_episode_offset = fh.tell()
            except Exception:
                pass

    def _poll_chat_once(self, client: Any) -> None:
        try:
            events = client.get_events(since=self._chat_seq, limit=40)
        except Exception:
            return
        if not isinstance(events, list):
            return
        with self._lock:
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                seq = ev.get("seq")
                if isinstance(seq, int):
                    self._chat_seq = max(self._chat_seq, seq)
                ev_type = ev.get("type")
                text = ev.get("text")
                if ev_type in {"chat", "system", "death", "spawn"} and isinstance(text, str) and text.strip():
                    self._chat_history.append({
                        "seq": seq,
                        "type": ev_type,
                        "text": text.strip(),
                        "timestamp": ev.get("timestamp") or time.strftime("%H:%M:%S"),
                    })
            if len(self._chat_history) > 200:
                self._chat_history = self._chat_history[-200:]

    def _guard_manual_write(self, *, force_override: bool = False) -> Tuple[bool, str]:
        if self.mode == "readonly_attach":
            return False, "只读附加模式：同作用域已有运行中的 BotSleep 实例，禁止发起写入控制"
        if self.mode != "host" or self.client is None:
            return False, "Bot 尚未连接，请先连接 mc-agent-api"
        if self.config["agent"]["mode"] == "survival" and self.controller is not None:
            if not self.controller.paused and not force_override:
                return False, "安全拦截：请先点击「暂停 AI」后再手动操作（或勾选「开发者强制接管」）"
            pending = self.controller.journal.state.get("pending")
            if pending is not None and not force_override:
                return False, "安全拦截：存在未核验的 pending 游戏动作意图，已禁止手动写入（调试需覆盖请勾选「开发者强制接管」）"
            gate = self._model_gate_snapshot()
            if gate.get("state") == "uncertain" and not force_override:
                return False, "安全拦截：模型请求门禁处于 uncertain 状态，已拦截手动技能（调试需覆盖请勾选「开发者强制接管」）"
            if self.controller.busy and not force_override:
                return False, "安全拦截：动作线程正在执行中，请等待完成或先点击「急停」（或勾选强制接管）"
        if force_override and self.recorder:
            self.recorder.record("WARN", "web_console", "WEB_FORCE_OVERRIDE", {"mode": self.config["agent"]["mode"]})
        return True, ""

    def _model_gate_snapshot(self) -> Dict[str, Any]:
        llm_cfg = self.config.get("llm", {})
        if not llm_cfg.get("enabled"):
            return {"enabled": False, "state": "idle"}
        planner_gate = getattr(getattr(self.planner, "brain", None), "request_gate", None)
        if planner_gate is not None:
            try:
                return {"enabled": True, **planner_gate.snapshot()}
            except ModelGateError as exc:
                return {"enabled": True, "state": "corrupt", "error": str(exc)}
        try:
            gate = ModelRequestGate(llm_cfg.get("base_url", ""), llm_cfg.get("api_key", ""))
            return {"enabled": True, **gate.snapshot()}
        except ModelGateError as exc:
            return {"enabled": True, "state": "corrupt", "error": str(exc)}

    # --- Lifecycle & emergency actions ---

    def pause_ai(self) -> Tuple[bool, str]:
        if self.mode != "host" or self.controller is None:
            return False, "当前非宿主运行模式，无法暂停"
        if self.config["agent"]["mode"] == "survival":
            self.controller.pause("web_console_pause")
        else:
            self.controller.pause()
        self.last_action_notice = "AI 已请求暂停"
        return True, self.last_action_notice

    def resume_ai(self) -> Tuple[bool, str]:
        if self.mode != "host" or self.controller is None:
            return False, "当前非宿主运行模式，无法恢复"
        if self.config["agent"]["mode"] == "survival":
            ok = self.controller.resume()
            msg = "AI 已恢复（将先重新观察新鲜状态）" if ok else "无法恢复：可能正在执行动作或已到达运行上限"
        else:
            ok = self.controller.request_sleep()
            msg = "已触发自动睡眠检查" if ok else "当前已有任务在运行或已关闭"
        self.last_action_notice = msg
        return ok, msg

    def step_once(self) -> Tuple[bool, str]:
        if self.mode != "host" or self.controller is None:
            return False, "当前非宿主运行模式"
        if self.config["agent"]["mode"] == "survival":
            ok = self.controller.request_step()
            msg = "已触发单步观察与执行" if ok else "单步请求被拒绝（线程忙或已到达上限）"
        else:
            ok = self.controller.request_sleep()
            msg = "已触发一次睡眠尝试" if ok else "睡眠任务仍在运行"
        self.last_action_notice = msg
        return ok, msg

    def emergency_stop(self) -> Tuple[bool, str]:
        if self.mode == "readonly_attach":
            return False, "只读附加模式不能对外部 Bot 进程发送急停"
        if self.client is None:
            return False, "Bot 未连接"
        if self.controller is not None:
            try:
                if self.config["agent"]["mode"] == "survival":
                    self.controller.pause("web_console_emergency_stop")
                else:
                    self.controller.pause()
            except Exception:
                pass
        try:
            self.client.stop(timeout_seconds=3.0)
        except Exception as exc:
            if self.recorder:
                self.recorder.record("WARN", "web_console", "WEB_EMERGENCY_STOP_ERROR", {"error": str(exc)})
            return False, f"已暂停 AI，但调用 API /stop 返回异常：{exc}"
        if self.recorder:
            self.recorder.record("WARN", "web_console", "WEB_EMERGENCY_STOP", {})
        self.last_action_notice = "已发送急停并暂停 AI"
        return True, self.last_action_notice

    def acknowledge_model_idle(self) -> Tuple[bool, str]:
        if self.mode == "readonly_attach":
            return False, "只读附加模式禁止解除外部进程模型门禁"
        gate = getattr(getattr(self.planner, "brain", None), "request_gate", None)
        if gate is None:
            llm_cfg = self.config.get("llm", {})
            if not llm_cfg.get("enabled"):
                return False, "当前未启用 LLM"
            gate = ModelRequestGate(llm_cfg.get("base_url", ""), llm_cfg.get("api_key", ""))
        gate.acknowledge_provider_idle()
        if self.controller is not None and hasattr(self.controller, "_model_blocked"):
            self.controller._model_blocked = None
        if self.recorder:
            self.recorder.record("WARN", "web_console", "WEB_ACK_MODEL_IDLE", {})
        return True, "已确认远端模型空闲并重置本地门禁（请确保服务商端确实没有在途请求）"

    # --- Chat & manual controls ---

    def send_chat(self, message: str) -> Tuple[bool, str]:
        if self.mode == "readonly_attach":
            return False, "只读附加模式禁止发送聊天"
        if self.client is None:
            return False, "Bot 未连接"
        text = str(message or "").strip()
        if not text:
            return False, "请输入要发送的聊天内容"
        try:
            self.client.chat(text[:256])
            with self._lock:
                self._chat_history.append({
                    "seq": None,
                    "type": "outgoing",
                    "text": f"[{self.client.username}] {text[:256]}",
                    "timestamp": time.strftime("%H:%M:%S"),
                })
                if len(self._chat_history) > 200:
                    self._chat_history = self._chat_history[-200:]
            if self.recorder:
                self.recorder.record("INFO", "web_console", "WEB_CHAT_SENT", {"length": len(text[:256])})
            return True, f"已发送：{text[:256]}"
        except Exception as exc:
            return False, f"发送聊天失败：{exc}"

    def manual_move_step(
        self,
        direction: str,
        *,
        duration_ms: int = 400,
        sprint: bool = False,
        sneak: bool = False,
        jump: bool = False,
        force_override: bool = False,
    ) -> Tuple[bool, str]:
        ok, err = self._guard_manual_write(force_override=force_override)
        if not ok:
            return False, err
        flags = {
            "forward": direction == "forward",
            "back": direction == "back",
            "left": direction == "left",
            "right": direction == "right",
            "jump": jump or direction == "jump",
            "sneak": sneak,
            "sprint": sprint,
        }
        ms = max(50, min(3000, int(duration_ms)))
        try:
            res = self.client.control(**flags, ms=ms, wait=True)
            if self.recorder:
                self.recorder.record("INFO", "web_console", "WEB_MANUAL_MOVE", {"direction": direction, "ms": ms})
            self._refresh_manual_status()
            return True, f"步进执行完成 ({direction}, {ms}ms): {json.dumps(res.get('result', {}), ensure_ascii=False)[:180]}"
        except Exception as exc:
            return False, f"步进移动失败：{exc}"

    def manual_move_to(
        self,
        x: float,
        y: Optional[float],
        z: float,
        *,
        range_blocks: float = 1.0,
        force_override: bool = False,
    ) -> Tuple[bool, str]:
        ok, err = self._guard_manual_write(force_override=force_override)
        if not ok:
            return False, err
        try:
            res = self.client.move_to(float(x), float(y) if y is not None else None, float(z), range=float(range_blocks))
            if self.recorder:
                self.recorder.record("INFO", "web_console", "WEB_MANUAL_MOVE_TO", {"x": x, "y": y, "z": z})
            self._refresh_manual_status()
            return True, f"寻路移动结果：{json.dumps(res.get('result', {}), ensure_ascii=False)[:220]}"
        except Exception as exc:
            return False, f"寻路移动失败：{exc}"

    def manual_look_at(
        self,
        x: float,
        y: float,
        z: float,
        *,
        force_override: bool = False,
    ) -> Tuple[bool, str]:
        ok, err = self._guard_manual_write(force_override=force_override)
        if not ok:
            return False, err
        try:
            self.client.look_at(float(x), float(y), float(z))
            self._refresh_manual_status()
            return True, f"已转向 ({x}, {y}, {z})"
        except Exception as exc:
            return False, f"转向失败：{exc}"

    def manual_3d_scan(self, *, radius_xz: int = 4, radius_y: int = 3) -> Tuple[bool, str]:
        if self.mode == "readonly_attach":
            return False, "只读附加模式不会向外部实例的 mc-agent-api 发起采样请求"
        if self.client is None:
            return False, "Bot 未连接，无法执行局部 3D 采样"
        try:
            meta = self.scene_collector.scan_local_box(
                self.client,
                radius_xz=int(radius_xz),
                radius_y=int(radius_y),
            )
            self._refresh_manual_status()
            if self.recorder:
                self.recorder.record("INFO", "web_console", "WEB_3D_SCAN", meta)
            return True, (
                f"局部 3D 采样完成：中心 {meta['center']}，查询 {meta['queried']} 格，"
                f"实心方块 {meta['solid_count']}，未加载 {meta['not_loaded']}"
            )
        except Exception as exc:
            return False, f"局部 3D 采样失败：{exc}"

    def manual_radar_scan(self, kind: str = "poi", radius: int = 24) -> Tuple[bool, Dict[str, Any], str]:
        if self.mode == "readonly_attach" or self.client is None:
            return False, {}, "需要已连接的宿主模式才能执行主动雷达扫描"
        radar = BlockRadar(self.client)
        rad = max(2, min(48, int(radius)))
        try:
            if kind == "census":
                data = radar.scan_census(radius=rad)
            elif kind == "terrain":
                data = radar.scan_terrain_profile(radius=min(6, max(2, rad // 4)))
            else:
                data = radar.scan_poi(radius=rad)
            with self._lock:
                self._latest_radar = {"kind": kind, "radius": rad, "captured_at": time.time(), "data": data}
            self._refresh_manual_status()
            return True, data, f"雷达扫描 ({kind}, r={rad}) 完成"
        except Exception as exc:
            return False, {"error": str(exc)}, f"雷达扫描失败：{exc}"

    def manual_run_skill(
        self,
        skill_name: str,
        args_json: str = "{}",
        *,
        force_override: bool = False,
    ) -> Tuple[bool, str]:
        ok, err = self._guard_manual_write(force_override=force_override)
        if not ok:
            return False, err
        name = str(skill_name or "").strip()
        spec = SKILL_REGISTRY.get(name)
        if spec is None:
            return False, f"未知技能：{name}"
        try:
            args = json.loads(args_json or "{}")
        except ValueError as exc:
            return False, f"参数 JSON 解析失败：{exc}"
        if not isinstance(args, dict):
            return False, "技能参数必须是 JSON 对象"
        if name == "wait":
            secs = args.get("seconds", 1)
            try:
                secs_f = max(0.05, min(5.0, float(secs)))
            except (TypeError, ValueError):
                return False, "wait 的 seconds 参数必须是数字"
            time.sleep(secs_f)
            return True, f"[wait] 已等待 {secs_f:.2f}s"
        if spec.runner_slot in {"meta", "autonomy", "competition"}:
            return False, f"技能 {name} 属于 Harness 内部状态机控制工具，不能作为独立底层技能直接触发"
        if self.config["agent"]["mode"] != "survival" or self.controller is None:
            if name == "auto_sleep":
                res = AutoSleepSkill(self.client).execute(
                    **{k: v for k, v in self.config["sleep"].items() if k in SleepController.SKILL_OPTIONS}
                )
                return res.success, f"{res.message} | data={json.dumps(res.data, ensure_ascii=False)}"
            return False, "当前非生存模式，仅支持 auto_sleep 与 wait"
        try:
            if name == "auto_sleep":
                res = AutoSleepSkill(self.client).execute(
                    **{k: v for k, v in self.config["sleep"].items() if k in SleepController.SKILL_OPTIONS}
                )
            else:
                runner = resolve_skill_runner(self.controller, name)
                res = runner.execute(name, args)
            if self.recorder:
                self.recorder.record(
                    "INFO" if res.success else "WARN",
                    "web_console",
                    "WEB_MANUAL_SKILL",
                    {"skill": name, "args": args, "result": asdict(res)},
                )
            self._refresh_manual_status()
            return res.success, f"[{name}] {res.message} (code={res.error_code})"
        except Exception as exc:
            return False, f"技能执行异常：{exc}"

    def _refresh_manual_status(self) -> None:
        if self.client is None:
            return
        try:
            st = self.client.get_status(timeout_seconds=2.0)["result"]
            self.scene_collector.record_bot_pos(st)
            if self.controller is not None and hasattr(self.controller, "latest_status"):
                self.controller.latest_status = st
            obs = dict(self._manual_observation or {})
            obs["status"] = st
            try:
                inv = self.client.inventory(timeout_seconds=2.0)["result"].get("items", [])
                counts: Dict[str, int] = {}
                for item in inv:
                    if isinstance(item, dict) and isinstance(item.get("name"), str):
                        counts[item["name"]] = counts.get(item["name"], 0) + int(item.get("count", 1))
                obs["inventory"] = inv
                obs["counts"] = counts
            except Exception:
                pass
            try:
                obs["entities"] = self.client.get_entities(radius=min(24, int(self.config["survival"]["scan_radius"])), timeout_seconds=2.0)
            except Exception:
                pass
            self._manual_observation = obs
        except Exception:
            pass

    # --- Settings inspection, hot-reload & config.yaml persistence ---

    def get_editable_settings(self) -> Dict[str, Any]:
        """Return non-secret settings safe for rendering in Gradio inputs."""
        cfg = copy.deepcopy(self.config)
        return {
            "mc_api": {
                "base_url": cfg["mc_api"]["base_url"],
                "timeout_seconds": cfg["mc_api"]["timeout_seconds"],
                "login_timeout_seconds": cfg["mc_api"]["login_timeout_seconds"],
                "keep_session_on_exit": cfg["mc_api"]["keep_session_on_exit"],
            },
            "agent": {
                "name": cfg["agent"]["name"],
                "mode": cfg["agent"]["mode"],
                "has_password": bool(cfg["agent"]["password"]),
            },
            "survival": copy.deepcopy(cfg["survival"]),
            "llm": {
                "enabled": cfg["llm"]["enabled"],
                "base_url": cfg["llm"]["base_url"],
                "has_api_key": bool(cfg["llm"]["api_key"]),
                "model": cfg["llm"]["model"],
                "temperature": cfg["llm"]["temperature"],
                "max_tokens": cfg["llm"]["max_tokens"],
                "timeout_seconds": cfg["llm"]["timeout_seconds"],
                "max_history_turns": cfg["llm"]["max_history_turns"],
                "max_repair_attempts": cfg["llm"]["max_repair_attempts"],
                "tls_version": cfg["llm"]["tls_version"],
            },
            "sleep": copy.deepcopy(cfg["sleep"]),
            "mining": copy.deepcopy(cfg["mining"]),
        }

    def update_and_save_settings(self, updates: Mapping[str, Any]) -> Tuple[bool, str]:
        """Validate updates, save atomically to config.yaml preserving secrets, and hot-apply."""
        with self._lock:
            if self.config_path.is_file():
                try:
                    raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8-sig"))
                    raw_cfg = raw if isinstance(raw, dict) else {}
                except Exception as exc:
                    return False, f"读取原配置文件失败：{exc}"
            else:
                raw_cfg = {}

            cleaned_updates = copy.deepcopy(dict(updates))
            # Never erase existing secrets unless a new non-empty value was explicitly provided.
            if isinstance(cleaned_updates.get("agent"), dict) and not cleaned_updates["agent"].get("password"):
                cleaned_updates["agent"].pop("password", None)
            if isinstance(cleaned_updates.get("llm"), dict) and not cleaned_updates["llm"].get("api_key"):
                cleaned_updates["llm"].pop("api_key", None)

            candidate_raw = _deep_merge_updates(raw_cfg, cleaned_updates)
            try:
                validated = validate_supplied_config(
                    candidate_raw,
                    self.config_path.parent,
                    username=self.username_override,
                )
            except ConfigError as exc:
                return False, f"配置校验失败，未保存：{exc}"

            # Atomic YAML write-back
            tmp_path = self.config_path.with_name(f".{self.config_path.name}.{os.getpid()}.tmp")
            try:
                yaml_text = yaml.safe_dump(candidate_raw, allow_unicode=True, sort_keys=False)
                tmp_path.write_text(yaml_text, encoding="utf-8")
                os.replace(tmp_path, self.config_path)
            except OSError as exc:
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                return False, f"写入 {self.config_path.name} 失败：{exc}"

            self.config = validated
            self._apply_hot_config(validated)
            if self.recorder:
                self.recorder.record("INFO", "web_console", "CONFIG_SAVED_AND_APPLIED", {
                    "restart_required": self.restart_required,
                })
            if self.restart_required:
                return True, (
                    f"已校验并保存至 {self.config_path.name}；基础参数已热生效。"
                    "检测到结构性参数（模式/目标/账号/LLM开关等）变化，请点击「热重启 Runner」使其完全生效。"
                )
            return True, f"已校验、热生效并保存至 {self.config_path.name}"

    def _apply_hot_config(self, cfg: Dict[str, Any]) -> None:
        self.restart_required = (
            self.mode == "host" and self._structural_signature(cfg) != self._last_applied_structural
        )
        controller = self.controller
        if controller is None:
            return
        if self.config["agent"]["mode"] == "survival" and hasattr(controller, "settings"):
            controller.settings.update(copy.deepcopy(cfg["survival"]))
            if hasattr(controller, "sleep_settings"):
                controller.sleep_settings.update(copy.deepcopy(cfg["sleep"]))
            if getattr(controller, "collector", None) is not None:
                controller.collector.radius = cfg["survival"]["scan_radius"]
            if getattr(controller, "_safety_rules", None) is not None:
                self._sync_survival_planner(controller._safety_rules, cfg["survival"])
            if getattr(controller, "daily_policy", None) is not None:
                self._sync_daily_policy(controller.daily_policy, cfg["survival"])
            if getattr(controller, "daily_goals", None) is not None:
                self._sync_daily_goals(controller.daily_goals, cfg["survival"])
            if getattr(controller, "task_controller", None) is not None:
                self._sync_task_controller(controller.task_controller, cfg["survival"])
            if getattr(controller, "planner", None) is not None and getattr(controller.planner, "uses_llm", False):
                self._sync_llm_planner(controller.planner, cfg["llm"], cfg["survival"])
        elif hasattr(controller, "settings"):
            controller.settings.update(copy.deepcopy(cfg["sleep"]))

    @staticmethod
    def _sync_survival_planner(planner: Any, survival_cfg: Mapping[str, Any]) -> None:
        planner.settings = dict(survival_cfg)
        planner.explore_distance = planner._setting("explore_distance", 16, 1, 32)
        planner.max_explore_steps = int(planner._setting("max_explore_steps", 8, 0, 1000))
        planner.low_health = planner._setting("low_health_threshold", 8, 1, 20)
        planner.food_threshold = planner._setting("food_threshold", 16, 1, 20)
        planner.threat_radius = planner._setting("threat_radius", 8, 1, 32)
        planner.max_target_failures = int(planner._setting("max_target_failures", 2, 1, 1000))

    @classmethod
    def _sync_daily_policy(cls, policy: Any, survival_cfg: Mapping[str, Any]) -> None:
        cls._sync_survival_planner(policy.rules, survival_cfg)
        policy.low_health = policy.rules.low_health
        policy.threat_radius = policy.rules.threat_radius
        policy.food_threshold = policy.rules.food_threshold
        raw_policy = survival_cfg.get("sleep_policy", "strict")
        policy.sleep_policy = raw_policy if raw_policy in {"opportunistic", "strict"} else "strict"

    @classmethod
    def _sync_daily_goals(cls, goals: Any, survival_cfg: Mapping[str, Any]) -> None:
        goals.settings = dict(survival_cfg)
        cls._sync_survival_planner(goals.rules, survival_cfg)
        daily = survival_cfg.get("daily", {})
        goals.targets = {
            name: goals._limit(daily.get(name), default, maximum)
            for name, default, maximum in (
                ("food_reserve", 8, 64),
                ("wood_reserve", 8, 64),
                ("fuel_reserve", 8, 64),
                ("maintenance_interval_seconds", 30, 60),
            )
        }

    @classmethod
    def _sync_task_controller(cls, task_ctrl: Any, survival_cfg: Mapping[str, Any]) -> None:
        task_ctrl.settings = copy.deepcopy(dict(survival_cfg))
        options = task_ctrl.settings.get("autonomy", {})
        task_ctrl.enabled = options.get("enabled", True) is True
        for key in ("max_steps", "max_seconds", "max_local_failures", "max_no_progress_steps", "max_explore_steps"):
            if key in options:
                task_ctrl.limits[key] = options[key]
        cls._sync_daily_goals(task_ctrl.daily, survival_cfg)
        cls._sync_daily_policy(task_ctrl.safety_policy, survival_cfg)

    @classmethod
    def _sync_llm_planner(cls, planner: Any, llm_cfg: Mapping[str, Any], survival_cfg: Mapping[str, Any]) -> None:
        planner.settings = dict(llm_cfg)
        cls._sync_survival_planner(planner.rules, survival_cfg)
        planner.max_history_turns = planner._limit(llm_cfg, "max_history_turns", 8, 1, 100)
        planner.max_repair_attempts = planner._limit(llm_cfg, "max_repair_attempts", 1, 0, 3)
        brain = getattr(planner, "brain", None)
        if brain is not None:
            brain.model = llm_cfg.get("model", brain.model)
            brain.temperature = llm_cfg.get("temperature", brain.temperature)
            brain.max_tokens = llm_cfg.get("max_tokens", brain.max_tokens)
            brain.timeout_seconds = llm_cfg.get("timeout_seconds", brain.timeout_seconds)

    # --- Telemetry, 3D scene & stack snapshot queries ---

    def _active_observation(self) -> Optional[Dict[str, Any]]:
        ctrl = self.controller
        if ctrl is not None and getattr(ctrl, "last_observation", None) is not None:
            return ctrl.last_observation
        return self._manual_observation

    def get_scene_3d(self, *, radius: int = 28) -> Dict[str, Any]:
        obs = self._active_observation()
        ctrl = self.controller
        status = getattr(ctrl, "latest_status", None) if ctrl is not None else None
        if status is None and isinstance(self._readonly_monitor_state, dict):
            status = self._readonly_monitor_state.get("status")
        scope = {
            "endpoint": self.config["mc_api"]["base_url"],
            "username": self.config["agent"]["name"],
            "world_id": self.config["survival"]["world_id"],
        }
        return self.scene_collector.build_scene(
            observation=obs,
            status=status,
            scope=scope,
            radius=max(8, min(64, int(radius))),
        )

    def sample_runtime_code(self) -> Dict[str, Any]:
        """Return live worker Python stack frames, active skill, model call & task substep."""
        ctrl = self.controller
        specs: List[Tuple[str, Optional[int]]] = []
        if self._tick_thread is not None and self._tick_thread.is_alive():
            specs.append(("WebLoop", self._tick_thread.ident))

        def _pool_tids(executor: Any) -> List[int]:
            threads = getattr(executor, "_threads", None)
            if not threads:
                return []
            return [t.ident for t in list(threads) if t.is_alive() and t.ident is not None]

        if ctrl is not None:
            for idx, tid in enumerate(_pool_tids(getattr(ctrl, "_executor", None))):
                specs.append((f"ActionWorker#{idx + 1}", tid))
            worker = getattr(ctrl, "_model_worker", None)
            if worker is not None:
                for idx, tid in enumerate(_pool_tids(getattr(worker, "_executor", None))):
                    specs.append((f"ModelWorker#{idx + 1}", tid))

        threads = sample_thread_frames(specs)
        active_decision = None
        if ctrl is not None and getattr(ctrl, "_active_decision", None) is not None:
            active_decision = asdict(ctrl._active_decision)

        return {
            "mode": self.mode,
            "phase": getattr(ctrl, "phase", "idle") if ctrl is not None else "idle",
            "phase_elapsed_s": round(time.monotonic() - self._tracked_phase_since, 2),
            "busy": bool(getattr(ctrl, "busy", False)) if ctrl is not None else False,
            "paused": bool(getattr(ctrl, "paused", False)) if ctrl is not None else False,
            "pause_reason": getattr(ctrl, "pause_reason", None) if ctrl is not None else None,
            "active_decision": active_decision,
            "task_execution": ctrl._task_snapshot() if ctrl is not None and hasattr(ctrl, "_task_snapshot") else None,
            "model_pending": bool(
                ctrl is not None and getattr(ctrl, "_model_worker", None) and ctrl._model_worker.pending
            ),
            "model_gate": self._model_gate_snapshot(),
            "threads": threads,
        }

    def status_report(self) -> Dict[str, Any]:
        """Build complete structured telemetry snapshot for the Gradio workbench."""
        mon_state = self.event_monitor.state()
        if self.mode == "readonly_attach" and isinstance(self._readonly_monitor_state, dict):
            # Merge richer remote monitor state if available while keeping local events
            base = copy.deepcopy(self._readonly_monitor_state)
            base["events_count"] = mon_state.get("last_seq", 0)
            mon_state = base

        ctrl = self.controller
        obs = self._active_observation() or {}
        if ctrl is not None and self.config["agent"]["mode"] == "survival":
            ctrl_snap = ctrl.snapshot()
        elif ctrl is not None:
            ctrl_snap = {"busy": ctrl.busy, "paused": getattr(ctrl, "_paused", False)}
        else:
            ctrl_snap = {}

        journal_pending = None
        journal_context = {}
        journal_last_result = None
        if self.journal_guard is not None:
            journal_pending = copy.deepcopy(self.journal_guard.state.get("pending"))
            journal_context = copy.deepcopy(self.journal_guard.state.get("context") or {})
            journal_last_result = copy.deepcopy(self.journal_guard.state.get("last_result"))
        if ctrl is not None and hasattr(ctrl, "journal"):
            journal_pending = copy.deepcopy(ctrl.journal.state.get("pending"))
            journal_context = copy.deepcopy(ctrl.journal.state.get("context") or {})
            journal_last_result = copy.deepcopy(ctrl.journal.state.get("last_result"))

        boards = {
            "daily_tasks": ctrl_snap.get("daily_tasks") or journal_context.get("daily_tasks") or (mon_state.get("daily") or {}).get("board"),
            "competition": ctrl_snap.get("competition") or (mon_state.get("competition") or {}).get("board"),
            "playbook": ctrl_snap.get("playbook") or journal_context.get("playbook") or mon_state.get("playbook"),
            "task_execution": ctrl_snap.get("task_execution") or journal_context.get("task_execution") or mon_state.get("task_execution"),
            "memory_summary": ctrl_snap.get("memory_summary") or (mon_state.get("daily") or {}).get("memory_counts"),
            "last_result": journal_last_result or mon_state.get("last_result"),
            "decision": ctrl_snap.get("decision") or mon_state.get("decision"),
        }

        return {
            "host_mode": self.mode,
            "connect_error": self.connect_error,
            "restart_required": self.restart_required,
            "notice": self.last_action_notice,
            "config_path": str(self.config_path),
            "scope": {
                "username": self.config["agent"]["name"],
                "mode": self.config["agent"]["mode"],
                "objective": self.config["survival"]["objective"],
                "world_id": self.config["survival"]["world_id"],
                "endpoint": self.config["mc_api"]["base_url"],
            },
            "monitor": mon_state,
            "controller": ctrl_snap,
            "pending": journal_pending,
            "model_gate": self._model_gate_snapshot(),
            "boards": boards,
            "observation": {
                "status": obs.get("status") or ctrl_snap.get("status") or mon_state.get("status") or {},
                "counts": obs.get("counts") or {},
                "inventory": obs.get("inventory") or [],
                "blocks": obs.get("blocks") or [],
                "terrain": obs.get("terrain") or [],
                "entities": obs.get("entities") or [],
                "safe_stand_candidates": obs.get("safe_stand_candidates") or [],
                "placement_candidates": obs.get("placement_candidates") or [],
                "observed_at": obs.get("observed_at"),
                "observation_scope": obs.get("observation_scope"),
            },
            "radar": copy.deepcopy(self._latest_radar),
            "chat": list(self._chat_history[-60:]),
            "runtime_code": self.sample_runtime_code(),
        }

    def get_events(
        self,
        *,
        since: int = 0,
        level: str = "ALL",
        module: str = "ALL",
        search: str = "",
        limit: int = 80,
    ) -> List[Dict[str, Any]]:
        snap = self.event_monitor.snapshot(since=max(0, int(since)))
        records = snap.get("events", [])
        lvl = str(level or "ALL").upper()
        mod = str(module or "ALL").lower()
        kw = str(search or "").strip().lower()
        filtered: List[Dict[str, Any]] = []
        for rec in records:
            if lvl != "ALL" and str(rec.get("level", "")).upper() != lvl:
                continue
            if mod != "all" and str(rec.get("module", "")).lower() != mod:
                continue
            if kw:
                hay = json.dumps(rec, ensure_ascii=False).lower()
                if kw not in hay:
                    continue
            filtered.append(rec)
        return filtered[-max(1, min(300, int(limit))):]

    def _teardown_runner(self, *, logout: bool) -> None:
        if self.controller is not None:
            try:
                self.controller.close()
            except Exception:
                pass
            self.controller = None
        if self.client is not None and self._login_attempted:
            if not logout:
                save_fn = getattr(self.client, "save_session", None)
                if self._session_file and callable(save_fn):
                    try:
                        save_fn(self._session_file)
                    except Exception:
                        pass
            else:
                try:
                    self.client.logout()
                except Exception:
                    pass
        self.client = None
        self.planner = None
        self._login_attempted = False

    def close(self) -> None:
        self._stop_event.set()
        if self._tick_thread is not None:
            self._tick_thread.join(timeout=2.5)
            self._tick_thread = None
        with self._lock:
            self._teardown_runner(logout=not self._keep_connected)
            if self._instance_lock_cm is not None:
                try:
                    self._instance_lock_cm.__exit__(None, None, None)
                except Exception:
                    pass
                self._instance_lock_cm = None
            if self.recorder:
                self.recorder.record("INFO", "main", "SHUTDOWN", {"exit_code": 0, "web_console": True})
            if self.monitor_server is not None:
                self.monitor_server.close()
                self.monitor_server = None
            self.mode = "stopped"
