"""In-game Chat Debugger Dispatcher

Listens to in-game chat messages starting with '#' and executes debug commands.
"""

from typing import Dict, Any, Callable
from core.client import MinecraftAgentClient


class ChatDebugger:
    def __init__(self, client: MinecraftAgentClient, prefix: str = "#"):
        self.client = client
        self.prefix = prefix
        self.last_seq = 0
        self.commands: Dict[str, Callable[[str], None]] = {}
        self._register_default_commands()

    def _register_default_commands(self) -> None:
        self.commands["help"] = self._cmd_help
        self.commands["status"] = self._cmd_status
        self.commands["stop"] = self._cmd_stop

    def poll_and_dispatch(self) -> None:
        """Polls new chat events and dispatches matching # commands."""
        events = self.client.get_events(since=self.last_seq)
        for ev in events:
            self.last_seq = max(self.last_seq, ev.get("seq", 0))
            if ev.get("type") == "chat":
                msg = ev.get("text", "").strip()
                if msg.startswith(self.prefix):
                    self._handle_command(msg[len(self.prefix):].strip())

    def _handle_command(self, raw_cmd: str) -> None:
        parts = raw_cmd.split(maxsplit=1)
        if not parts:
            return
        cmd_name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if cmd_name in self.commands:
            self.commands[cmd_name](args)
        else:
            self.client.chat(f"[BotSleep Debug] Unknown command: #{cmd_name}. Type #help for command list.")

    def _cmd_help(self, args: str) -> None:
        cmd_list = ", ".join(f"#{c}" for c in self.commands.keys())
        self.client.chat(f"[BotSleep Debug] Available commands: {cmd_list}")

    def _cmd_status(self, args: str) -> None:
        st = self.client.get_status().get("result", {})
        pos = st.get("pos", [0, 0, 0])
        hp = st.get("health", 0)
        day = st.get("is_day", True)
        self.client.chat(f"[BotSleep Debug] Pos: {pos}, HP: {hp}, Day: {day}")

    def _cmd_stop(self, args: str) -> None:
        self.client.chat("[BotSleep Debug] Stopping all active tasks...")
        # TODO: Send stop signal
