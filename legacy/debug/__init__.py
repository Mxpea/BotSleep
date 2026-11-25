"""Debug exports; importing logging or chat tools does not require RCON."""

from importlib import import_module

_EXPORTS = {
    "FlightRecorder": (".logger", "FlightRecorder"),
    "ChatDebugger": (".chat_debugger", "ChatDebugger"),
    "RconPlayground": (".playground", "RconPlayground"),
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = _EXPORTS[name]
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
