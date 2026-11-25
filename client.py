"""Compatibility import shim for standalone scripts.

Forwards directly to v2 L0 MinecraftAgentClient without duplicating protocol code.
"""

from v2.kernel.client import APIError, MinecraftAgentClient

__all__ = ["APIError", "MinecraftAgentClient"]
