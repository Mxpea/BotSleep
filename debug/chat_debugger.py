"""Allowlisted Minecraft chat controls for the active sleep task."""

import re
from typing import Callable, Optional


class ChatDebugger:
    def __init__(self, client, prefix="#", allowed_usernames=(),
                 on_stop: Optional[Callable] = None, on_sleep: Optional[Callable] = None,
                 on_resume: Optional[Callable] = None, on_summary: Optional[Callable] = None):
        self.client = client
        self.prefix = prefix
        self.allowed_usernames = {name.casefold() for name in allowed_usernames}
        self.last_seq = 0
        self.on_stop = on_stop or client.stop
        self.on_sleep = on_sleep
        self.on_resume = on_resume
        self.on_summary = on_summary
        self.commands = {"help": self._cmd_help, "status": self._cmd_status, "stop": self._cmd_stop}
        if on_sleep:
            self.commands["sleep"] = self._cmd_sleep
        if on_resume:
            self.commands["resume"] = self._cmd_resume

    def poll_and_dispatch(self) -> None:
        for event in self.client.get_events(since=self.last_seq):
            self.last_seq = max(self.last_seq, event.get("seq", 0))
            if event.get("type") != "chat":
                continue
            # API messagestr contains the displayed message, usually '<Player> text'.
            # Never execute anonymous/server text or arbitrary substrings containing '#'.
            match = re.fullmatch(r"<([A-Za-z0-9_]{3,16})>\s*(.*)", event.get("text", "").strip())
            if not match:
                continue
            sender, message = match.groups()
            if sender.casefold() == self.client.username.casefold():
                continue
            if sender.casefold() not in self.allowed_usernames or not message.startswith(self.prefix):
                continue
            self._handle_command(message[len(self.prefix):].strip())

    def _reply(self, text):
        # A rate-limited debug reply must not kill an active sleep task.
        try:
            self.client.chat(text[:100])
        except Exception:
            pass

    def _handle_command(self, raw_cmd):
        parts = raw_cmd.split(maxsplit=1)
        if not parts:
            return
        handler = self.commands.get(parts[0].lower())
        if not handler:
            self._reply("[BotSleep] Unknown command. Use #help.")
            return
        try:
            handler(parts[1] if len(parts) > 1 else "")
        except Exception as exc:
            self._reply(f"[BotSleep] Command failed: {getattr(exc, 'code', 'error')}")

    def _cmd_help(self, args):
        self._reply("[BotSleep] " + ", ".join("#" + key for key in self.commands))

    def _cmd_status(self, args):
        if self.on_summary:
            self._reply("[BotSleep] " + self.on_summary())
            return
        status = self.client.get_status()["result"]
        self._reply(f"[BotSleep] Pos: {status.get('pos')} HP: {status.get('health')} Sleeping: {status.get('is_sleeping')}")

    def _cmd_stop(self, args):
        self.on_stop()
        self._reply("[BotSleep] Stop requested; automation paused. Use " + ("#resume" if self.on_resume else "#sleep") + " to resume.")

    def _cmd_sleep(self, args):
        started = self.on_sleep()
        self._reply("[BotSleep] Sleep attempt started." if started else "[BotSleep] Sleep task is still active.")

    def _cmd_resume(self, args):
        resumed = self.on_resume()
        self._reply("[BotSleep] Survival resumed; observing fresh state." if resumed else "[BotSleep] Task still active or run finished.")
