"""BotSleepTest for Nether Portal Speedrun (Step-by-Step Verification)."""

import json
import logging
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.client import MinecraftAgentClient
from core.rcon import RconManager
from skills.portal_speedrun import NetherPortalSpeedrunSkill

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("BotSleepTest.SpeedrunPortal")


class BotSleepTest(unittest.TestCase):
    """Test suite for Nether Portal Speedrun casting using BotSleepTest."""

    @classmethod
    def setUpClass(cls):
        cls.rcon = RconManager(host="127.0.0.1", port=25575, password="agent-rcon-pass")
        cls.client = MinecraftAgentClient()

    def setUp(self):
        """Prepare world state and bot before each test."""
        self.client.login()
        time.sleep(0.5)

    def tearDown(self):
        """Clean up bot session."""
        self.client.logout()

    def test_step_1_place_stone_water_and_move_to_red_sand(self):
        """Step 1 Test: 放置石头，检查状态，放置水，检查水/岩浆/黑曜石状态，换位置到红砂.
        
        Runs in dedicated isolated CI test arena (X=-58) to protect all user exhibition structures.
        """
        logger.info("=== [BotSleepTest] Step 1 Setup: Resetting Isolated CI Arena at X=-58 ===")
        with self.rcon as _:
            # 1. Clear air first in isolated test volume
            self.rcon.command("fill -62 62 -20 -55 67 -16 air")
            self.rcon.command("fill -62 62 -20 -55 62 -16 stone")
            
            # 2. Reset ground & lava pool edge
            self.rcon.command("setblock -58 65 -19 sand")
            self.rcon.command("setblock -58 64 -19 stone")
            self.rcon.command("setblock -59 64 -19 stone")
            
            # Lava pool edge at z=-18
            self.rcon.command("setblock -60 64 -18 lava")
            self.rcon.command("setblock -59 64 -18 lava")
            self.rcon.command("setblock -58 64 -18 lava")
            self.rcon.command("setblock -57 64 -18 lava")
            
            # Deep pool at z=-18 y=63
            self.rcon.command("setblock -60 63 -18 lava")
            self.rcon.command("setblock -59 63 -18 lava")
            self.rcon.command("setblock -58 63 -18 lava")
            self.rcon.command("setblock -57 63 -18 lava")
            
            # Lava at z=-17
            self.rcon.command("setblock -60 64 -17 lava")
            self.rcon.command("setblock -59 64 -17 lava")
            self.rcon.command("setblock -58 64 -17 lava")
            self.rcon.command("setblock -57 64 -17 lava")
            self.rcon.command("setblock -59 65 -17 red_sand")
            
            # Gear and position SleepBot at sand
            self.rcon.command("clear SleepBot")
            self.rcon.command("effect give SleepBot fire_resistance 9999 0 true")
            self.rcon.command("effect give SleepBot instant_health 1 255 true")
            self.rcon.command("tp SleepBot -58 65 -19")
            self.rcon.command("give SleepBot cobblestone 16")
            self.rcon.command("give SleepBot water_bucket 1")
            self.rcon.command("give SleepBot bucket 1")

        time.sleep(0.5)

        # Precondition verification
        inv = self.client.get_inventory_counts()
        logger.info(f"SleepBot inventory ready: {inv}")
        self.assertGreaterEqual(inv.get("cobblestone", 0), 1)
        self.assertGreaterEqual(inv.get("water_bucket", 0), 1)

        # Execute Step 1 Skill
        skill = NetherPortalSpeedrunSkill(self.client)
        start_sand = [-58, 65, -19]
        stone_pos = [-58, 64, -18]
        water_pos = [-59, 64, -18]
        red_sand = [-59, 65, -17]

        logger.info("=== [BotSleepTest] Executing Step 1 Skill ===")
        res = skill.execute_step_1(
            start_sand_pos=start_sand,
            stone_pos=stone_pos,
            water_pos=water_pos,
            red_sand_pos=red_sand,
        )

        logger.info(f"Step 1 Result: Success={res.success}, Message={res.message}")
        logger.info(f"Data: {json.dumps(res.data, indent=2)}")

        # Assertions for Step 1
        self.assertTrue(res.success, f"Step 1 must succeed: {res.message}")

        # 1. Stone verified
        b_stone = self.client.get_block(stone_pos[0], stone_pos[1], stone_pos[2]).get("result", {})
        self.assertIn(b_stone.get("name"), ["cobblestone", "stone"], "Stone must be placed at stone_pos")

        # 2. Water / Obsidian verified
        b_water = self.client.get_block(water_pos[0], water_pos[1], water_pos[2]).get("result", {})
        self.assertIn(b_water.get("name"), ["water", "obsidian"], "Water spot must have water or formed obsidian")

        # 3. Formed obsidian on edge
        b_obs = self.client.get_block(-60, 64, -18).get("result", {})
        self.assertEqual(b_obs.get("name"), "obsidian", "Adjacent lava at [-60, 64, -18] must freeze into obsidian")

        # 4. Bot reached red_sand position
        final_pos = self.client.get_status().get("result", {}).get("pos", [])
        dist_to_red_sand = ((final_pos[0] - red_sand[0])**2 + (final_pos[2] - red_sand[2])**2)**0.5
        self.assertLess(dist_to_red_sand, 1.5, f"Bot must be standing near red_sand marker: {final_pos}")

        logger.info("[BotSleepTest PASSED] Step 1 verified successfully!")

    def test_step_2_dig_stone_and_cast_bottom_obsidian(self):
        """Step 2 Test: 拆掉石头，检查底部方块-挖掘非黑曜石方块替换，放置石油，放置水.
        
        Runs in dedicated isolated CI test arena (X=-58) to protect all user exhibition structures.
        """
        logger.info("=== [BotSleepTest] Step 2 Setup: Resetting Isolated CI Arena at X=-58 ===")
        with self.rcon as _:
            # 1. Clear air first in isolated test volume
            self.rcon.command("fill -62 62 -20 -55 67 -16 air")
            self.rcon.command("fill -62 62 -20 -55 62 -16 stone")
            
            # 2. Reset ground & shore
            self.rcon.command("setblock -58 65 -19 sand")
            self.rcon.command("setblock -58 64 -19 stone")
            self.rcon.command("setblock -59 64 -19 stone")
            self.rcon.command("setblock -59 65 -17 red_sand")
            
            # 3. Z=-18 portal plane:
            # Outer posts at Y=64 (from Step 1)
            self.rcon.command("setblock -60 64 -18 obsidian")
            self.rcon.command("setblock -57 64 -18 obsidian")
            
            # Base slots at Y=64:
            # -58 64 -18 is temporary cobblestone placed in Step 1
            self.rcon.command("setblock -58 64 -18 cobblestone")
            # -59 64 -18 is cavity with water flow
            self.rcon.command("setblock -59 64 -18 air")
            
            # 4. Subsurface at Y=63 (Z=-18):
            # Bottom slots in lava pool awaiting conversion
            self.rcon.command("setblock -60 63 -18 lava")
            self.rcon.command("setblock -59 63 -18 lava")
            self.rcon.command("setblock -58 63 -18 lava")
            self.rcon.command("setblock -57 63 -18 lava")
            
            # Pool boundary at Z=-17
            self.rcon.command("setblock -60 64 -17 lava")
            self.rcon.command("setblock -58 64 -17 lava")
            self.rcon.command("setblock -57 64 -17 lava")
            
            # Water source to sustain water curtain into base
            self.rcon.command("setblock -59 65 -19 water")
            
            # Gear and position SleepBot at red_sand
            self.rcon.command("clear SleepBot")
            self.rcon.command("effect give SleepBot fire_resistance 9999 0 true")
            self.rcon.command("effect give SleepBot instant_health 1 255 true")
            self.rcon.command("tp SleepBot -59 65 -17")
            self.rcon.command("give SleepBot diamond_pickaxe 1")
            self.rcon.command("give SleepBot lava_bucket 2")
            self.rcon.command("give SleepBot water_bucket 1")
            self.rcon.command("give SleepBot bucket 1")

        time.sleep(0.5)

        # Inventory check
        inv = self.client.get_inventory_counts()
        logger.info(f"SleepBot inventory ready for Step 2: {inv}")
        self.assertGreaterEqual(inv.get("diamond_pickaxe", 0), 1)

        # Execute Step 2 Skill
        skill = NetherPortalSpeedrunSkill(self.client)
        stone_pos = [-58, 64, -18]
        base_positions = [[-59, 63, -18], [-58, 63, -18]]
        red_sand = [-59, 65, -17]
        water_placement = [-59, 65, -19]

        logger.info("=== [BotSleepTest] Executing Step 2 Skill ===")
        res = skill.execute_step_2(
            stone_pos=stone_pos,
            base_positions=base_positions,
            red_sand_pos=red_sand,
            water_placement_pos=water_placement,
        )

        logger.info(f"Step 2 Result: Success={res.success}, Message={res.message}")
        logger.info(f"Data: {json.dumps(res.data, indent=2)}")

        # Assertions for Step 2
        self.assertTrue(res.success, f"Step 2 must succeed: {res.message}")

        # 1. Temporary stone dug
        b_stone_after = self.client.get_block(stone_pos[0], stone_pos[1], stone_pos[2]).get("result", {})
        self.assertNotIn(b_stone_after.get("name"), ["cobblestone", "stone"], "Temporary stone must be mined away")

        # 2. Both bottom frame positions at Y=63 converted to obsidian
        b_base_1 = self.client.get_block(base_positions[0][0], base_positions[0][1], base_positions[0][2]).get("result", {})
        b_base_2 = self.client.get_block(base_positions[1][0], base_positions[1][1], base_positions[1][2]).get("result", {})
        self.assertEqual(b_base_1.get("name"), "obsidian", f"Base 1 at {base_positions[0]} must be obsidian")
        self.assertEqual(b_base_2.get("name"), "obsidian", f"Base 2 at {base_positions[1]} must be obsidian")

        # 3. Outer posts at Y=64 remain obsidian
        b_post_l = self.client.get_block(-60, 64, -18).get("result", {})
        b_post_r = self.client.get_block(-57, 64, -18).get("result", {})
        self.assertEqual(b_post_l.get("name"), "obsidian", "Left post at [-60, 64, -18] must be obsidian")
        self.assertEqual(b_post_r.get("name"), "obsidian", "Right post at [-57, 64, -18] must be obsidian")

        logger.info("[BotSleepTest PASSED] Step 2 verified successfully!")


def main():
    suite = unittest.TestLoader().loadTestsFromTestCase(BotSleepTest)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        exit(1)


if __name__ == "__main__":
    main()
