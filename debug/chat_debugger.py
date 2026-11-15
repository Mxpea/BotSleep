"""Allowlisted Minecraft chat controls for active tasks and skills."""

import re
from typing import Any, Callable, Dict, Optional


class ChatDebugger:
    def __init__(
        self,
        client,
        prefix="#",
        allowed_usernames=(),
        on_stop: Optional[Callable] = None,
        on_sleep: Optional[Callable] = None,
        on_resume: Optional[Callable] = None,
        on_summary: Optional[Callable] = None,
        radar_skill=None,
        memory=None,
        chop_skill=None,
        craft_skill=None,
    ):
        self.client = client
        self.prefix = prefix
        self.allowed_usernames = {name.casefold() for name in allowed_usernames}
        self.last_seq = 0
        self.on_stop = on_stop or client.stop
        self.on_sleep = on_sleep
        self.on_resume = on_resume
        self.on_summary = on_summary
        self.radar_skill = radar_skill
        self.memory = memory
        self.chop_skill = chop_skill
        self.craft_skill = craft_skill

        self.commands: Dict[str, Callable] = {
            "help": self._cmd_help,
            "status": self._cmd_status,
            "stop": self._cmd_stop,
            "inv": self._cmd_inv,
        }
        if on_sleep:
            self.commands["sleep"] = self._cmd_sleep
        if on_resume:
            self.commands["resume"] = self._cmd_resume
        if self.radar_skill:
            self.commands["radar"] = self._cmd_radar
        if self.chop_skill:
            self.commands["chop"] = self._cmd_chop
        if self.craft_skill:
            self.commands["craft"] = self._cmd_craft

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

    def _reply(self, text: str) -> None:
        # A rate-limited debug reply must not kill an active task.
        try:
            self.client.chat(text[:100])
        except Exception:
            pass

    def _handle_command(self, raw_cmd: str) -> None:
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

    def _cmd_help(self, args: str) -> None:
        self._reply("[BotSleep] " + ", ".join("#" + key for key in self.commands))

    def _cmd_status(self, args: str) -> None:
        if self.on_summary:
            self._reply("[BotSleep] " + self.on_summary())
            return
        status = self.client.get_status().get("result", {})
        pos = status.get("pos", [0, 0, 0])
        hp = status.get("health", 0)
        sleeping = status.get("is_sleeping", False)
        radar_stat = "ON" if (self.radar_skill and getattr(self.radar_skill, "is_active", False)) else "OFF"
        self._reply(f"[BotSleep] Pos: {pos} HP: {hp} Sleeping: {sleeping} Radar: {radar_stat}")

    def _cmd_stop(self, args: str) -> None:
        if self.radar_skill and getattr(self.radar_skill, "is_active", False):
            self.radar_skill.disable()
        self.on_stop()
        self._reply("[BotSleep] Stop requested; automation paused. Use " + ("#resume" if self.on_resume else "#sleep") + " to resume.")

    def _cmd_sleep(self, args: str) -> None:
        started = self.on_sleep() if self.on_sleep else False
        self._reply("[BotSleep] Sleep attempt started." if started else "[BotSleep] Sleep task is still active.")

    def _cmd_resume(self, args: str) -> None:
        resumed = self.on_resume() if self.on_resume else False
        self._reply("[BotSleep] Survival resumed; observing fresh state." if resumed else "[BotSleep] Task still active or run finished.")

    def _cmd_radar(self, args: str) -> None:
        if not self.radar_skill:
            self._reply("[Radar] Radar skill not initialized.")
            return
        sub = args.strip().lower()
        if sub == "on":
            res = self.radar_skill.enable()
            self._reply(f"[Radar] {res.message}")
        elif sub == "off":
            res = self.radar_skill.disable()
            self._reply(f"[Radar] {res.message}")
        elif sub == "scan":
            self._reply("[Radar] Executing manual radar sweep...")
            res = self.radar_skill.execute(force_sweep=True)
            new_pois = res.data.get("new_pois", 0)
            self._reply(f"[Radar] Sweep completed! Indexed {new_pois} strategic POIs.")
        elif sub == "poi":
            st = self.client.get_status().get("result", {})
            pos = st.get("pos", [0, 0, 0])
            if self.memory:
                pois = self.memory.query_pois(max_distance=32.0, from_pos=pos)
                if not pois:
                    self._reply("[Radar] No POIs recorded within 32 blocks.")
                else:
                    summary = ", ".join(f"{p['name']} ({p.get('current_dist')}m)" for p in pois[:4])
                    self._reply(f"[Radar POI] Nearest: {summary}")
            else:
                self._reply("[Radar] Memory engine not bound.")
        else:
            self._reply("[Radar] Usage: #radar [on|off|scan|poi]")

    def _cmd_chop(self, args: str) -> None:
        if not self.chop_skill:
            self._reply("[ChopTree] ChopTree skill not initialized.")
            return
        parts = args.split()
        target = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 5
        timeout = float(parts[1]) if len(parts) > 1 and parts[1].replace(".", "").isdigit() else 60.0

        self._reply(f"[ChopTree] Starting tree chopping (target: {target} logs, timeout: {timeout}s)...")
        res = self.chop_skill.execute(target_count=target, max_duration_seconds=timeout)
        self._reply(f"[ChopTree] {res.message}")

    def _cmd_craft(self, args: str) -> None:
        if not self.craft_skill:
            self._reply("[Craft] Craft skill not initialized.")
            return
        parts = args.split()
        if not parts:
            self._reply("[Craft] Usage: #craft <item> [count]")
            return

        item = parts[0].strip().lower()
        count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1

        self._reply(f"[Craft] Starting automated crafting pipeline for {count}x {item}...")
        res = self.craft_skill.execute(item=item, count=count)
        self._reply(f"[Craft] {res.message}")

    def _cmd_inv(self, args: str) -> None:
        counts = self.client.get_inventory_counts()
        if not counts:
            self._reply("[Inventory] Empty inventory.")
            return
        summary = ", ".join(f"{k}x{v}" for k, v in sorted(counts.items()))
        self._reply(f"[Inventory] Items: {summary}")
