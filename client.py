"""Compatibility import for the standalone sleep script.

Both entrypoints use the same protocol implementation and error handling.
"""

from core.client import APIError, MinecraftAgentClient

__all__ = ["APIError", "MinecraftAgentClient"]
