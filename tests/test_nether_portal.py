"""BotSleepTest: End-to-end and unit testing for Nether Portal Casting Skill."""

import json
import logging
import sys
import time
import unittest
from pathlib import Path
from typing import Dict, Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.client import MinecraftAgentClient
from core.rcon import RconManager
from skills.portal import CastNetherPortalSkill, REQUIRED_OBSIDIAN_COUNT

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("BotSleepTest.Portal")


class BotSleepTest(unittest.TestCase):
    """Test suite for Nether Portal Casting and Space Validation in BotSleep."""

    @classmethod
    def setUpClass(cls):
        cls.rcon = RconManager(host="127.0.0.1", port=25575, password="agent-rcon-pass")
        cls.client = MinecraftAgentClient()

    def test_01_precondition_missing_items(self):
        """Validates that skill fails gracefully when essential items are missing."""
        skill = CastNetherPortalSkill(self.client)
        ok, err = skill.validate_preconditions()
        self.assertFalse(ok)
        self.assertIn(err, ["bot_not_ready", "missing_water_bucket", "missing_flint_and_steel"])

    def test_02_lava_pool_insufficient_space_or_sources(self):
        """Validates that skill rejects lava pools with < 10 sources or blocked clearance."""
        self.client.login()
        try:
            skill = CastNetherPortalSkill(self.client)
            # Evaluate with requirement of 9999 sources (guaranteed insufficient)
            ok, info = skill.evaluate_lava_pool(search_radius=16, required_sources=9999)
            self.assertFalse(ok)
            self.assertEqual(info.get("error"), "insufficient_lava_sources")
            logger.info("Successfully rejected insufficient lava pool as expected.")
        finally:
            self.client.logout()

    def test_03_end_to_end_portal_casting_and_frame_inspection(self):
        """Live End-to-End Test: Prepares a qualified lava pool, casts portal, and verifies frame status."""
        logger.info("=== [BotSleepTest] Step 1: Setting up live test arena with RCON ===")
        with self.rcon as _:
            # 1. Clean flat arena: ground at 64, air above
            self.rcon.command("fill -42 64 -30 -20 72 -10 air")
            self.rcon.command("fill -42 63 -30 -20 63 -10 stone")
            self.rcon.command("fill -42 64 -30 -20 64 -10 stone")
            # 2. Surface lava pool: along shore at z=-25..-21, x=-38..-27 (120+ sources)
            self.rcon.command("fill -38 63 -25 -27 64 -21 lava")
            # 3. Portal build area anchor: (-26, 64, -18)
            # Support under portal frame base at 63
            self.rcon.command("fill -26 63 -18 -23 63 -18 stone")
            # Backboard at Z = -19 (y=64..68)
            self.rcon.command("fill -26 64 -19 -23 68 -19 cobblestone")
            # Side containment stops at x=-27 and x=-22
            self.rcon.command("fill -27 64 -19 -27 68 -18 cobblestone")
            self.rcon.command("fill -22 64 -19 -22 68 -18 cobblestone")
            # Base corners at 64
            self.rcon.command("setblock -26 64 -18 cobblestone")
            self.rcon.command("setblock -23 64 -18 cobblestone")
            # Front scaffolding (Inverted L at z=-17)
            self.rcon.command("fill -25 65 -17 -25 68 -17 cobblestone")
            self.rcon.command("setblock -24 68 -17 cobblestone")
            self.rcon.command("setblock -24 64 -17 cobblestone")
            self.rcon.command("setblock -23 64 -17 cobblestone")
            # Base blocks at y=64: pre-cast base obsidian for clean start
            self.rcon.command("setblock -25 64 -18 obsidian")
            self.rcon.command("setblock -24 64 -18 obsidian")
            # Ensure middle portal opening is 100% empty air
            self.rcon.command("fill -25 65 -18 -24 67 -18 air")

        logger.info("=== [BotSleepTest] Step 2: Logging in SleepBot and provisioning gear ===")
        self.client.login()
        time.sleep(0.5)

        with self.rcon as _:
            self.rcon.command("clear SleepBot")
            self.rcon.command("effect give SleepBot fire_resistance 9999 0 true")
            self.rcon.command("effect give SleepBot instant_health 1 255 true")
            self.rcon.command("tp SleepBot -25 64 -16")
            self.rcon.command("give SleepBot water_bucket 1")
            self.rcon.command("give SleepBot bucket 3")
            self.rcon.command("give SleepBot flint_and_steel 1")
            self.rcon.command("give SleepBot cobblestone 32")
            self.rcon.command("give SleepBot diamond_pickaxe 1")

        time.sleep(1)
        inv = self.client.get_inventory_counts()
        logger.info(f"SleepBot inventory prepared: {inv}")
        self.assertGreaterEqual(inv.get("water_bucket", 0), 1)
        self.assertGreaterEqual(inv.get("flint_and_steel", 0), 1)

        logger.info("=== [BotSleepTest] Step 3: Executing Nether Portal Casting Skill ===")
        skill = CastNetherPortalSkill(self.client)

        # Precondition check
        ok, err = skill.validate_preconditions()
        self.assertTrue(ok, f"Preconditions should be met, got error: {err}")

        # Execute casting with explicit anchor at arena boundary (-26, 64, -18)
        anchor = [-26, 64, -18]
        result = skill.execute(anchor=anchor, axis="x", search_radius=32)

        logger.info(f"Skill Execution Result: Success={result.success}, Message={result.message}")
        logger.info(f"Data payload:\n{json.dumps(result.data, indent=2)}")

        logger.info("=== [BotSleepTest] Step 4: Rigorous Frame and State Verification ===")
        self.assertTrue(result.success, f"Portal casting should succeed! Result: {result.message}")

        inspection = result.data.get("inspection", {})
        obsidian_count = inspection.get("obsidian_count", 0)
        frame_complete = inspection.get("frame_complete", False)
        portal_active = inspection.get("portal_active", False)

        # 1. Assert exactly 10 obsidian blocks in frame
        self.assertEqual(
            obsidian_count,
            REQUIRED_OBSIDIAN_COUNT,
            f"Expected {REQUIRED_OBSIDIAN_COUNT} obsidian blocks, verified {obsidian_count}",
        )
        self.assertTrue(frame_complete, "Portal frame should be complete.")

        # 2. Check each frame block individually via get_block
        for blk_info in inspection.get("frame_blocks", []):
            pos = blk_info["pos"]
            blk = self.client.get_block(pos[0], pos[1], pos[2]).get("result", {})
            self.assertEqual(
                blk.get("name"),
                "obsidian",
                f"Block at {pos} must be obsidian, got {blk.get('name')}",
            )

        # 3. Assert portal activation
        self.assertTrue(
            portal_active,
            "Portal should be activated with purple nether_portal blocks in inner opening.",
        )

        logger.info("[BotSleepTest PASSED] All 10 obsidian frame blocks and portal activation verified!")
        self.client.logout()


def main():
    suite = unittest.TestLoader().loadTestsFromTestCase(BotSleepTest)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        exit(1)


if __name__ == "__main__":
    main()
