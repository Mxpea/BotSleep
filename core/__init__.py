"""Core exports, loaded lazily so RCON remains an optional dependency."""

from importlib import import_module

_EXPORTS = {
    "APIError": (".client", "APIError"),
    "MinecraftAgentClient": (".client", "MinecraftAgentClient"),
    "RconManager": (".rcon", "RconManager"),
    "TelemetryManager": (".telemetry", "TelemetryManager"),
    "ReflexDaemon": (".telemetry", "ReflexDaemon"),
    "MemoryEngine": (".memory", "MemoryEngine"),
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = _EXPORTS[name]
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
