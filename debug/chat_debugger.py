"""In-game Chat Debugger Dispatcher

Listens to in-game chat messages starting with '#' and executes debug commands.
"""

from typing import Dict, Any, Callable, Optional
from core.client import MinecraftAgentClient
from core.memory import MemoryEngine
from skills.exploration import ContinuousRadarSkill
from skills.mining import ChopTreeSkill
from skills.crafting import CraftItemSkill


class ChatDebugger:
    def __init__(
        self,
        client: MinecraftAgentClient,
        prefix: str = "#",
        radar_skill: Optional[ContinuousRadarSkill] = None,
        memory: Optional[MemoryEngine] = None,
        chop_skill: Optional[ChopTreeSkill] = None,
        craft_skill: Optional[CraftItemSkill] = None,
    ):
        self.client = client
        self.prefix = prefix
        self.radar_skill = radar_skill or ContinuousRadarSkill(client)
        self.memory = memory or MemoryEngine()
        self.chop_skill = chop_skill or ChopTreeSkill(client, memory=self.memory)
        self.craft_skill = craft_skill or CraftItemSkill(client, memory=self.memory)
        self.last_seq = 0
        self.commands: Dict[str, Callable[[str], None]] = {}
        self._register_default_commands()

    def _register_default_commands(self) -> None:
        self.commands["help"] = self._cmd_help
        self.commands["status"] = self._cmd_status
        self.commands["stop"] = self._cmd_stop
        self.commands["radar"] = self._cmd_radar
        self.commands["chop"] = self._cmd_chop
        self.commands["craft"] = self._cmd_craft
        self.commands["inv"] = self._cmd_inv

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
        radar_stat = "ON" if self.radar_skill.is_active else "OFF"
        self.client.chat(f"[BotSleep Debug] Pos: {pos}, HP: {hp}, Day: {day}, Radar: {radar_stat}")

    def _cmd_stop(self, args: str) -> None:
        self.client.chat("[BotSleep Debug] Stopping all active tasks and continuous radar...")
        self.radar_skill.disable()

    def _cmd_radar(self, args: str) -> None:
        sub = args.strip().lower()
        if sub == "on":
            res = self.radar_skill.enable()
            self.client.chat(f"[Radar] {res.message}")
        elif sub == "off":
            res = self.radar_skill.disable()
            self.client.chat(f"[Radar] {res.message}")
        elif sub == "scan":
            self.client.chat("[Radar] Executing manual radar sweep...")
            res = self.radar_skill.execute(force_sweep=True)
            new_pois = res.data.get("new_pois", 0)
            self.client.chat(f"[Radar] Sweep completed! Indexed {new_pois} strategic POIs.")
        elif sub == "poi":
            st = self.client.get_status().get("result", {})
            pos = st.get("pos", [0, 0, 0])
            pois = self.memory.query_pois(max_distance=32.0, from_pos=pos)
            if not pois:
                self.client.chat("[Radar] No POIs recorded within 32 blocks.")
            else:
                summary = ", ".join(f"{p['name']} ({p.get('current_dist')}m)" for p in pois[:4])
                self.client.chat(f"[Radar POI] Nearest: {summary}")
        else:
            self.client.chat("[Radar] Usage: #radar [on|off|scan|poi]")

    def _cmd_chop(self, args: str) -> None:
        """Handles #chop [target_count] [max_seconds]."""
        parts = args.split()
        target = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 5
        timeout = float(parts[1]) if len(parts) > 1 and parts[1].replace(".", "").isdigit() else 60.0

        self.client.chat(f"[ChopTree] Starting tree chopping (target: {target} logs, timeout: {timeout}s)...")
        res = self.chop_skill.execute(target_count=target, max_duration_seconds=timeout)
        self.client.chat(f"[ChopTree] {res.message}")

    def _cmd_craft(self, args: str) -> None:
        """Handles #craft <item> [count]."""
        parts = args.split()
        if not parts:
            self.client.chat("[Craft] Usage: #craft <item> [count], e.g. #craft wooden_pickaxe")
            return

        item = parts[0].strip().lower()
        count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1

        self.client.chat(f"[Craft] Starting automated crafting pipeline for {count}x {item}...")
        res = self.craft_skill.execute(item=item, count=count)
        self.client.chat(f"[Craft] {res.message}")

    def _cmd_inv(self, args: str) -> None:
        """Handles #inv to inspect inventory items."""
        counts = self.client.get_inventory_counts()
        if not counts:
            self.client.chat("[Inventory] Empty inventory.")
            return

        summary = ", ".join(f"{k}x{v}" for k, v in sorted(counts.items()))
        self.client.chat(f"[Inventory] Items: {summary}")
