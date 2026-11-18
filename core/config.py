"""Load and validate BotSleep configuration without exposing secret values."""

import copy
import math
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union
from urllib.parse import urlsplit

import yaml


class ConfigError(ValueError):
    """A configuration file or override cannot be used safely."""


DEFAULTS: Dict[str, Any] = {
    "mc_api": {
        "base_url": "http://127.0.0.1:18900/api/v1",
        "timeout_seconds": 15,
        "login_timeout_seconds": 30,
        "keep_session_on_exit": False,
        "session_file": "",
    },
    "agent": {"name": "SleepBot", "password": "", "mode": "sleep"},
    "survival": {
        "objective": "starter",
        "intent": "survival",
        "operator_goal": "",
        "autonomy": {"enabled": True, "local_opening": False, "max_steps": 128, "max_seconds": 600,
                     "max_local_failures": 4, "max_no_progress_steps": 8, "max_explore_steps": 8},
        "competition": {"eligibility_overrides": {}, "defer_seconds": 300,
                        "advancement_poll_seconds": 5, "max_seconds": 28800, "max_steps": 100000},
        "daily": {"food_reserve": 8, "wood_reserve": 8, "fuel_reserve": 8, "maintenance_interval_seconds": 30},
        "world_id": "default",
        "state_dir": "runtime/survival",
        "scan_radius": 24,
        "poll_interval_seconds": 1,
        "retry_seconds": 5,
        "max_steps": 256,
        "max_runtime_seconds": 3600,
        "max_target_failures": 2,
        "target_cooldown_seconds": 300,
        "max_consecutive_failures": 6,
        "max_deaths": 3,
        "explore_distance": 16,
        "max_explore_steps": 8,
        "low_health_threshold": 8,
        "food_threshold": 16,
        "threat_radius": 8,
    },
    "sleep": {
        "enabled": True,
        "radius": 16,
        "threat_check": True,
        "threat_distance": 8,
        "poll_interval_seconds": 0.5,
        "retry_seconds": 15,
        "timeout_seconds": 600,
        "move_timeout_seconds": 15,
        "confirm_timeout_seconds": 5,
    },
    "debug": {
        "enable_in_game_commands": False,
        "command_prefix": "#",
        "allowed_usernames": [],
        "flight_recorder": {"enabled": True, "output_dir": "logs"},
        "monitor": {"enabled": False, "host": "127.0.0.1", "port": 18930,
                    "heartbeat_seconds": 2, "console": False},
    },
    "llm": {"enabled": False, "base_url": "http://127.0.0.1:8000/v1", "api_key": "",
            "model": "qwen3.6-35b-a3b", "temperature": 0.3, "max_tokens": 2048,
            "timeout_seconds": 60, "max_history_turns": 10, "max_repair_attempts": 1,
            "tls_version": "auto"},
    "mining": {
        "targets": {
            "iron": {"count": 24, "min_tool": "stone_pickaxe"},
            "coal": {"count": 16, "min_tool": "wooden_pickaxe"},
        },
        "cave_policy": {
            "flood_max_cells": 160,
            "horizontal_radius": 12,
            "vertical_span": 10,
            "max_height": 10,
            "max_drop": 4,
            "max_water_blocks": 3,
            "small_bubble_max_air": 4,
            "min_mineshaft_markers": 3,
            "min_enter_torches": 0,
            "explore_radius": 48,
            "explore_max_visited": 600,
            "explore_timeout_seconds": 360.0,
            "torch_interval": 24,
            "max_vein_blocks": 16,
            "max_unsafe_retries": 3,
            "eye_scan_radius": 4.5,
        },
        "home_thresholds": {
            "min_empty_slots": 3,
            "min_health": 12.0,
            "min_food": 10.0,
            "min_torches": 4,
            "min_blocks": 16,
            "min_pickaxe_durability_ratio": 0.10,
            "min_remaining_seconds": 30.0,
        },
    },
    "rcon": {"enabled": False},
}

_USERNAME = re.compile(r"[A-Za-z0-9_]{3,16}\Z")


def _mapping(value: Any, field: str) -> Dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ConfigError(f"{field} must be a mapping with string keys")
    return value


def _merge(defaults: Dict[str, Any], supplied: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(defaults)
    for key, value in supplied.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _string(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ConfigError(f"{field} must be {'a' if allow_empty else 'a non-empty'} string")
    return value


def _boolean(value: Any, field: str) -> None:
    if type(value) is not bool:
        raise ConfigError(f"{field} must be a boolean")


def _positive(value: Any, field: str) -> None:
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise ConfigError(f"{field} must be a finite positive number")


def _username(value: Any, field: str) -> None:
    if not isinstance(value, str) or not _USERNAME.fullmatch(value):
        raise ConfigError(f"{field} must contain 3-16 ASCII letters, digits or underscores")


def _validate(config: Dict[str, Any], config_dir: Path) -> None:
    for section in DEFAULTS:
        _mapping(config[section], section)

    api = config["mc_api"]
    base_url = _string(api["base_url"], "mc_api.base_url").strip().rstrip("/")
    try:
        url = urlsplit(base_url)
        valid_url = (
            url.scheme in ("http", "https")
            and bool(url.hostname)
            and not any(char.isspace() for char in base_url)
            and url.username is None
            and url.password is None
            and not url.query
            and not url.fragment
            and (url.port is None or 1 <= url.port <= 65535)
        )
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ConfigError("mc_api.base_url must be an HTTP(S) URL without credentials, query or fragment")
    api["base_url"] = base_url
    for key in ("timeout_seconds", "login_timeout_seconds"):
        _positive(api[key], f"mc_api.{key}")
    _boolean(api["keep_session_on_exit"], "mc_api.keep_session_on_exit")
    session_file = _string(api["session_file"], "mc_api.session_file", allow_empty=True)
    if session_file:
        try:
            if "\x00" in session_file:
                raise ValueError()
            sf_path = Path(session_file).expanduser()
            api["session_file"] = str((sf_path if sf_path.is_absolute() else config_dir / sf_path).resolve())
        except (OSError, ValueError, RuntimeError):
            raise ConfigError("mc_api.session_file is not a usable path") from None

    agent = config["agent"]
    _username(agent["name"], "agent.name")
    _string(agent["password"], "agent.password", allow_empty=True)
    if agent["mode"] not in ("sleep", "survival"):
        raise ConfigError("agent.mode must be sleep or survival")
    # Validate legacy fields when present without enabling their old behavior.
    if "reflex_enabled" in agent:
        _boolean(agent["reflex_enabled"], "agent.reflex_enabled")
    for key in ("reflex_frequency_hz", "threat_distance_threshold"):
        if key in agent:
            _positive(agent[key], f"agent.{key}")

    sleep = config["sleep"]
    if set(sleep) - set(DEFAULTS["sleep"]):
        raise ConfigError("sleep contains unsupported options")
    for key in ("enabled", "threat_check"):
        _boolean(sleep[key], f"sleep.{key}")
    if type(sleep["radius"]) is not int or not 1 <= sleep["radius"] <= 64:
        raise ConfigError("sleep.radius must be an integer between 1 and 64")
    for key in (
        "threat_distance", "poll_interval_seconds", "retry_seconds",
        "timeout_seconds", "move_timeout_seconds", "confirm_timeout_seconds",
    ):
        _positive(sleep[key], f"sleep.{key}")
    if sleep["threat_distance"] > 61:
        raise ConfigError("sleep.threat_distance must not exceed 61")

    survival = config["survival"]
    if set(survival) - set(DEFAULTS["survival"]):
        raise ConfigError("survival contains unsupported options")
    if survival["objective"] not in {"starter", "daily", "competition"}:
        raise ConfigError("survival.objective must be starter, daily or competition")
    if not isinstance(survival["intent"], str) or survival["intent"] not in ("survival", "speedrun"):
        raise ConfigError("survival.intent must be survival or speedrun")
    goal = survival["operator_goal"]
    if (not isinstance(goal, str) or len(goal) > 1000 or goal and not goal.strip()
            or isinstance(goal, str) and any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in goal)):
        raise ConfigError("survival.operator_goal must be empty or up to 1000 visible characters without control characters")
    autonomy = _mapping(survival["autonomy"], "survival.autonomy")
    if set(autonomy) - set(DEFAULTS["survival"]["autonomy"]):
        raise ConfigError("survival.autonomy contains unsupported options")
    _boolean(autonomy["enabled"], "survival.autonomy.enabled")
    _boolean(autonomy["local_opening"], "survival.autonomy.local_opening")
    for key, minimum, maximum in (("max_steps", 1, 2048), ("max_local_failures", 1, 32),
                                 ("max_no_progress_steps", 1, 128), ("max_explore_steps", 0, 64)):
        if type(autonomy[key]) is not int or not minimum <= autonomy[key] <= maximum:
            raise ConfigError(f"survival.autonomy.{key} must be an integer between {minimum} and {maximum}")
    _positive(autonomy["max_seconds"], "survival.autonomy.max_seconds")
    if autonomy["max_seconds"] > 3600:
        raise ConfigError("survival.autonomy.max_seconds must not exceed 3600")
    competition = _mapping(survival["competition"], "survival.competition")
    if set(competition) - set(DEFAULTS["survival"]["competition"]):
        raise ConfigError("survival.competition contains unsupported options")
    for key, maximum in (("defer_seconds", 3600), ("advancement_poll_seconds", 60), ("max_seconds", 28800), ("max_steps", 1000000)):
        _positive(competition[key], f"survival.competition.{key}")
        if competition[key] > maximum:
            raise ConfigError(f"survival.competition.{key} must not exceed {maximum}")
    if type(competition["max_steps"]) is not int or competition["max_steps"] < 1:
        raise ConfigError("survival.competition.max_steps must be a positive integer")
    overrides = _mapping(competition["eligibility_overrides"], "survival.competition.eligibility_overrides")
    if any(not key.startswith("minecraft:") or type(value) is not bool for key, value in overrides.items()):
        raise ConfigError("Eligibility overrides require Minecraft advancement IDs and boolean values")
    if overrides:
        from core.competition_rules import scoring_catalog
        try:
            scoring_catalog(overrides)
        except ValueError:
            raise ConfigError("Invalid competition scoring override") from None
    daily = _mapping(survival["daily"], "survival.daily")
    if set(daily) - set(DEFAULTS["survival"]["daily"]):
        raise ConfigError("survival.daily contains unsupported options")
    for key, maximum in (("food_reserve", 64), ("wood_reserve", 64), ("fuel_reserve", 64), ("maintenance_interval_seconds", 60)):
        if type(daily[key]) is not int or not 1 <= daily[key] <= maximum:
            raise ConfigError(f"survival.daily.{key} must be an integer between 1 and {maximum}")
    world_id = _string(survival["world_id"], "survival.world_id")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", world_id):
        raise ConfigError("survival.world_id must contain 1-64 letters, digits, underscores or hyphens")
    for key in ("max_steps", "max_target_failures", "max_consecutive_failures", "max_deaths", "max_explore_steps"):
        if type(survival[key]) is not int or not 1 <= survival[key] <= 10000:
            raise ConfigError(f"survival.{key} must be an integer between 1 and 10000")
    if type(survival["scan_radius"]) is not int or not 4 <= survival["scan_radius"] <= 64:
        raise ConfigError("survival.scan_radius must be an integer between 4 and 64")
    for key in ("poll_interval_seconds", "retry_seconds", "max_runtime_seconds", "target_cooldown_seconds"):
        _positive(survival[key], f"survival.{key}")
    for key, maximum in (("low_health_threshold", 20), ("food_threshold", 20), ("threat_radius", 24), ("explore_distance", 32)):
        _positive(survival[key], f"survival.{key}")
        if survival[key] > maximum:
            raise ConfigError(f"survival.{key} must not exceed {maximum}")
    state_dir = _string(survival["state_dir"], "survival.state_dir")
    try:
        if "\x00" in state_dir:
            raise ValueError()
        state_path = Path(state_dir).expanduser()
        survival["state_dir"] = str((state_path if state_path.is_absolute() else config_dir / state_path).resolve())
    except (OSError, ValueError, RuntimeError):
        raise ConfigError("survival.state_dir is not a usable path") from None

    debug = config["debug"]
    _boolean(debug["enable_in_game_commands"], "debug.enable_in_game_commands")
    prefix = _string(debug["command_prefix"], "debug.command_prefix")
    if any(char.isspace() for char in prefix):
        raise ConfigError("debug.command_prefix must not contain whitespace")
    allowed = debug["allowed_usernames"]
    if not isinstance(allowed, list):
        raise ConfigError("debug.allowed_usernames must be a list")
    for name in allowed:
        _username(name, "debug.allowed_usernames entry")

    monitor = _mapping(debug["monitor"], "debug.monitor")
    if set(monitor) - set(DEFAULTS["debug"]["monitor"]):
        raise ConfigError("debug.monitor contains unsupported options")
    for key in ("enabled", "console"):
        _boolean(monitor[key], f"debug.monitor.{key}")
    if monitor["host"] not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigError("debug.monitor.host must be a loopback address")
    if type(monitor["port"]) is not int or not 1 <= monitor["port"] <= 65535:
        raise ConfigError("debug.monitor.port must be an integer between 1 and 65535")
    _positive(monitor["heartbeat_seconds"], "debug.monitor.heartbeat_seconds")

    recorder = _mapping(debug["flight_recorder"], "debug.flight_recorder")
    _boolean(recorder["enabled"], "debug.flight_recorder.enabled")
    output = _string(recorder["output_dir"], "debug.flight_recorder.output_dir")
    if "\x00" in output:
        raise ConfigError("debug.flight_recorder.output_dir contains an invalid path character")
    try:
        output_path = Path(output).expanduser()
        if not output_path.is_absolute():
            output_path = config_dir / output_path
        recorder["output_dir"] = str(output_path.resolve())
    except (OSError, ValueError, RuntimeError):
        raise ConfigError("debug.flight_recorder.output_dir is not a usable path") from None

    _boolean(config["rcon"]["enabled"], "rcon.enabled")
    llm = config["llm"]
    if set(llm) - set(DEFAULTS["llm"]):
        raise ConfigError("llm contains unsupported options")
    _boolean(llm["enabled"], "llm.enabled")
    _string(llm["api_key"], "llm.api_key", allow_empty=True)
    _string(llm["model"], "llm.model", allow_empty=not llm["enabled"])
    llm_url = _string(llm["base_url"], "llm.base_url", allow_empty=not llm["enabled"]).strip().rstrip("/")
    try:
        parsed = urlsplit(llm_url)
        valid = (not llm_url and not llm["enabled"]) or (parsed.scheme in ("http", "https") and bool(parsed.hostname)
                 and not any(c.isspace() for c in llm_url) and parsed.username is None
                 and parsed.password is None and not parsed.query and not parsed.fragment
                 and (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        valid = False
    if not valid:
        raise ConfigError("llm.base_url must be an HTTP(S) URL without credentials, query or fragment")
    llm["base_url"] = llm_url
    _positive(llm["timeout_seconds"], "llm.timeout_seconds")
    if type(llm["temperature"]) not in (int, float) or not 0 <= llm["temperature"] <= 2 or not math.isfinite(llm["temperature"]):
        raise ConfigError("llm.temperature must be a finite number between 0 and 2")
    for key, low, high in (("max_tokens", 128, 32768), ("max_history_turns", 1, 64), ("max_repair_attempts", 0, 2)):
        if type(llm[key]) is not int or not low <= llm[key] <= high:
            raise ConfigError(f"llm.{key} must be an integer between {low} and {high}")
    if llm["tls_version"] not in ("auto", "1.2"):
        raise ConfigError("llm.tls_version must be auto or the string 1.2")

    mining = _mapping(config["mining"], "mining")
    if set(mining) - set(DEFAULTS["mining"]):
        raise ConfigError("mining contains unsupported options")
    targets = _mapping(mining["targets"], "mining.targets")
    for ore_key, spec in targets.items():
        if not isinstance(spec, dict) and (type(spec) is not int or spec < 1):
            raise ConfigError(f"mining.targets.{ore_key} must be a mapping or positive integer")
        if isinstance(spec, dict):
            cnt = spec.get("count", 1)
            if type(cnt) is not int or cnt < 1:
                raise ConfigError(f"mining.targets.{ore_key}.count must be a positive integer")
            if "min_tool" in spec:
                _string(spec["min_tool"], f"mining.targets.{ore_key}.min_tool")
    cave_policy = _mapping(mining["cave_policy"], "mining.cave_policy")
    if set(cave_policy) - set(DEFAULTS["mining"]["cave_policy"]):
        raise ConfigError("mining.cave_policy contains unsupported options")
    for key in cave_policy:
        if key == "min_enter_torches":
            val = cave_policy[key]
            if type(val) is not int or val < 0:
                raise ConfigError("mining.cave_policy.min_enter_torches must be a non-negative integer")
        else:
            _positive(cave_policy[key], f"mining.cave_policy.{key}")
    home_thresholds = _mapping(mining["home_thresholds"], "mining.home_thresholds")
    if set(home_thresholds) - set(DEFAULTS["mining"]["home_thresholds"]):
        raise ConfigError("mining.home_thresholds contains unsupported options")
    for key in home_thresholds:
        val = home_thresholds[key]
        if type(val) not in (int, float) or not math.isfinite(val) or val < 0:
            raise ConfigError(f"mining.home_thresholds.{key} must be a non-negative finite number")
        if key == "min_pickaxe_durability_ratio" and val > 1:
            raise ConfigError("mining.home_thresholds.min_pickaxe_durability_ratio must be between 0 and 1")


def load_config(
    path: Union[str, os.PathLike],
    *,
    environ: Optional[Mapping[str, str]] = None,
    username: Optional[str] = None,
) -> Dict[str, Any]:
    """Load YAML with CLI > environment > YAML > default precedence.

    ``path`` must exist. An empty YAML document uses defaults. The caller chooses
    the path (including any ``BOTSLEEP_CONFIG`` override). Relative log paths are
    anchored to the configuration directory, and loading never creates files.
    """
    try:
        config_path = Path(path).expanduser().resolve()
        with config_path.open("r", encoding="utf-8-sig") as stream:
            supplied = yaml.safe_load(stream)
    except FileNotFoundError:
        raise ConfigError("Configuration file does not exist; copy config.example.yaml or provide --config") from None
    except (yaml.YAMLError, UnicodeError):
        # YAML parser errors include source snippets, which may contain secrets.
        raise ConfigError("Configuration file is not valid YAML") from None
    except (OSError, ValueError, TypeError, RuntimeError):
        raise ConfigError("Configuration file could not be read") from None

    supplied = {} if supplied is None else _mapping(supplied, "configuration")
    config = _merge(DEFAULTS, supplied)
    # Reject malformed sections before applying overrides, so overrides cannot
    # silently replace an invalid YAML structure or fail with a TypeError.
    for section in DEFAULTS:
        _mapping(config[section], section)

    environment = os.environ if environ is None else environ
    for variable, section, key in (
        ("MC_AGENT_API", "mc_api", "base_url"),
        ("MC_AGENT_USER", "agent", "name"),
        ("MC_AGENT_PASSWORD", "agent", "password"),
        ("BOTSLEEP_LLM_API_KEY", "llm", "api_key"),
        ("BOTSLEEP_LLM_BASE_URL", "llm", "base_url"),
        ("BOTSLEEP_LLM_MODEL", "llm", "model"),
    ):
        if variable in environment:
            config[section][key] = environment[variable]
    if username is not None:
        config["agent"]["name"] = username

    _validate(config, config_path.parent)
    return config
