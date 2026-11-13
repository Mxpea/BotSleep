"""RCON Test Playground

Constructs automated mock test scenarios in the live Minecraft server.
"""

import time
from core.rcon import RconManager


class RconPlayground:
    def __init__(self, rcon: RconManager, bot_name: str = "SleepBot"):
        self.rcon = rcon
        self.bot_name = bot_name

    def setup_mlg_fall_test(self, height: int = 25) -> None:
        """Gives water bucket and teleports bot into high altitude to test MLG water clutch."""
        print(f"[Playground] Setting up MLG fall test (height={height})...")
        self.rcon.give_item(self.bot_name, "water_bucket", 1)
        self.rcon.command(f"execute at {self.bot_name} run tp {self.bot_name} ~ ~{height} ~")

    def setup_night_sleep_test(self) -> None:
        """Places a bed near the bot and sets time to night."""
        print("[Playground] Setting up night sleep test...")
        self.rcon.set_time("midnight")
        self.rcon.command(f"execute at {self.bot_name} run setblock ~2 ~ ~ red_bed[facing=north,part=foot]")
        self.rcon.command(f"execute at {self.bot_name} run setblock ~2 ~ ~-1 red_bed[facing=north,part=head]")

    def setup_hostile_mob_test(self) -> None:
        """Spawns a zombie near the bot to test evade / pillar_up behavior."""
        print("[Playground] Spawning hostile mob test...")
        self.rcon.command(f"execute at {self.bot_name} run summon zombie ~4 ~ ~")
