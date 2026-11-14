"""Load and validate BotSleep configuration without exposing secret values."""

import copy
import math
import os
import re
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
    },
    "agent": {"name": "SleepBot", "password": "", "mode": "sleep"},
    "survival": {
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
