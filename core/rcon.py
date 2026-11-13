"""RCON Manager for Minecraft Server Administration and Testing Harness."""

from typing import Optional
from mcrcon import MCRcon


class RconManager:
    def __init__(self, host: str = "127.0.0.1", port: int = 25575, password: str = "agent-rcon-pass"):
        self.host = host
        self.port = port
        self.password = password
        self._rcon: Optional[MCRcon] = None

    def connect(self) -> None:
        """Connects to server RCON."""
        if not self._rcon:
            self._rcon = MCRcon(self.host, self.password, port=self.port)
            self._rcon.connect()

    def disconnect(self) -> None:
        """Disconnects RCON session."""
        if self._rcon:
            try:
                self._rcon.disconnect()
            finally:
                self._rcon = None

    def command(self, cmd: str) -> str:
        """Executes a command and returns the response string."""
        self.connect()
        clean_cmd = cmd.lstrip("/")
        return self._rcon.command(clean_cmd)

    def set_time(self, time_val: str) -> str:
        """Sets game time (e.g. 'day', 'night', 'midnight', '18000')."""
        return self.command(f"time set {time_val}")

    def give_item(self, player: str, item: str, count: int = 1) -> str:
        """Gives item to player."""
        return self.command(f"give {player} {item} {count}")

    def teleport(self, player: str, x: float, y: float, z: float) -> str:
        """Teleports player to coordinates."""
        return self.command(f"tp {player} {x} {y} {z}")
