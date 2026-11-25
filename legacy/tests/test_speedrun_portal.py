"""BotSleepTest for Nether Portal Speedrun (Step-by-Step Verification)."""

import json
import logging
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.client import MinecraftAgentClient
from skills.portal_speedrun import NetherPortalSpeedrunSkill

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("BotSleepTest.SpeedrunPortal")


@unittest.skipUnless(os.environ.get("BOTSLEEP_RUN_LIVE_TESTS") == "1",
                     "Live RCON arena test; set BOTSLEEP_RUN_LIVE_TESTS=1 only for a disposable test world")
class BotSleepTest(unittest.TestCase):
    """Test suite for Nether Portal Speedrun casting using BotSleepTest."""

    @classmethod
    def setUpClass(cls):
        from core.rcon import RconManager
        # Arena commands below intentionally address the fixture account SleepBot.
        if os.environ.get("MC_AGENT_USER", "SleepBot") != "SleepBot":
            raise ValueError("This fixed arena fixture requires MC_AGENT_USER=SleepBot")
        cls.rcon = RconManager(host=os.environ.get("MC_RCON_HOST", "127.0.0.1"),
                               port=int(os.environ.get("MC_RCON_PORT", "25575")),
                               password=os.environ["MC_RCON_PASSWORD"])
        cls.client = MinecraftAgentClient(base_url=os.environ["MC_AGENT_API"], username="SleepBot",
                                           password=os.environ["MC_AGENT_PASSWORD"])

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
            
            # Gear and position SleepBot at red_sand in PURE SURVIVAL (0 fire resistance)
            self.rcon.command("clear SleepBot")
            self.rcon.command("effect clear SleepBot")
            self.rcon.command("effect give SleepBot instant_health 1 255 true")
            self.rcon.command("gamemode survival SleepBot")
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

    def test_step_3_casting_water_scoop_and_ignition(self):
        """Step 3 Test: 浇筑顺序 (空 2 1 空 / 6 4 / 5 3 / 下面已完成), 挪掉水, 等水流干净, 点燃.
        
        Runs in dedicated isolated CI test arena (X=-60) to protect all user exhibition structures.
        Pure survival: 20 HP, 0 fire resistance effects, zero damage taken.
        """
        logger.info("=== [BotSleepTest] Step 3 Setup: Preparing Scaffolding & Water Curtain at X=-60 ===")
        x0, y0, z0 = -60, 64, -18
        with self.rcon as _:
            # 1. Solid ground and pool floor in CI test volume
            self.rcon.command(f"fill {x0-4} 62 {z0-4} {x0+7} 70 {z0+3} air")
            self.rcon.command(f"fill {x0-4} 62 {z0-4} {x0+7} 62 {z0+3} stone")
            self.rcon.command(f"fill {x0-4} 63 {z0-4} {x0+7} 64 {z0+3} stone")
            
            # 2. Pool floor at Y=63 (Z=Z0)
            self.rcon.command(f"setblock {x0+0} 63 {z0} lava")
            self.rcon.command(f"setblock {x0+1} 63 {z0} obsidian")
            self.rcon.command(f"setblock {x0+2} 63 {z0} obsidian")
            self.rcon.command(f"setblock {x0+3} 63 {z0} lava")
            self.rcon.command(f"fill {x0} 63 {z0+1} {x0+3} 64 {z0+2} lava")
            
            # 3. Ground level at Y=64 (Z=Z0)
            self.rcon.command(f"setblock {x0+0} 64 {z0} obsidian")
            self.rcon.command(f"setblock {x0+1} 64 {z0} air")
            self.rcon.command(f"setblock {x0+2} 64 {z0} air")
            self.rcon.command(f"setblock {x0+3} 64 {z0} obsidian")
            
            # Front obsidian dam at Y=64, Z=Z0+1
            self.rcon.command(f"setblock {x0+0} 64 {z0+1} lava")
            self.rcon.command(f"setblock {x0+1} 64 {z0+1} obsidian")
            self.rcon.command(f"setblock {x0+2} 64 {z0+1} obsidian")
            self.rcon.command(f"setblock {x0+3} 64 {z0+1} lava")
            
            # Temporary standing block at Y=65 (red sand / temp cobblestone)
            rcon_stand_pos = [x0 + 1, 65, z0 + 1]
            self.rcon.command(f"setblock {rcon_stand_pos[0]} {rcon_stand_pos[1]} {rcon_stand_pos[2]} red_sand")
            
            # 4. 7 Scaffolding Cobblestones
            self.rcon.command(f"setblock {x0+1} 65 {z0-2} cobblestone") # Backboard
            self.rcon.command(f"setblock {x0+0} 65 {z0-1} cobblestone") # Left wall far
            self.rcon.command(f"setblock {x0+1} 65 {z0-1} cobblestone") # Left wall near
            self.rcon.command(f"setblock {x0+3} 65 {z0-1} cobblestone") # Pillar layer 1
            self.rcon.command(f"setblock {x0+3} 66 {z0-1} cobblestone") # Pillar layer 2
            self.rcon.command(f"setblock {x0+3} 67 {z0-1} cobblestone") # Pillar layer 3
            self.rcon.command(f"setblock {x0+3} 67 {z0+0} cobblestone") # Arm forward
            
            # Water source at [X0+2, 67, Z0-1]
            self.rcon.command(f"setblock {x0+2} 67 {z0-1} water")
            
            # Gear SleepBot in pure SURVIVAL mode (20 HP, 0 fire resistance)
            self.rcon.command("clear SleepBot")
            self.rcon.command("effect clear SleepBot")
            self.rcon.command("gamemode survival SleepBot")
            self.rcon.command(f"tp SleepBot {x0+1.6} 66.0 {z0+1.5} 0 0")
            self.rcon.command("effect give SleepBot instant_health 1 10 true")
            self.rcon.command("give SleepBot lava_bucket 6")
            self.rcon.command("give SleepBot bucket 1")
            self.rcon.command("give SleepBot diamond_pickaxe 1")
            self.rcon.command("give SleepBot flint_and_steel 1")

        time.sleep(0.8)

        # Inventory & Health Check
        inv = self.client.get_inventory_counts()
        st = self.client.get_status().get("result", {})
        logger.info(f"SleepBot ready for Step 3: Pos={st.get('pos')}, HP={st.get('health')}, Inv={inv}")
        self.assertEqual(st.get("health"), 20, "Bot must be at 20 HP")
        self.assertEqual(len(st.get("effects", [])), 0, "Bot must have 0 potion effects (no fire resistance)")
        self.assertGreaterEqual(inv.get("lava_bucket", 0), 6)
        self.assertGreaterEqual(inv.get("bucket", 0), 1)
        self.assertGreaterEqual(inv.get("flint_and_steel", 0), 1)

        # Execute Step 3 Skill
        skill = NetherPortalSpeedrunSkill(self.client)
        logger.info("=== [BotSleepTest] Executing Step 3 Skill ===")
        res = skill.execute_step_3(x0=x0, y0=y0, z0=z0, temp_stand_pos=rcon_stand_pos)
        
        logger.info(f"Step 3 Result: Success={res.success}, Message={res.message}")
        logger.info(f"Data: {json.dumps(res.data, indent=2)}")

        # Assertions for Step 3
        self.assertTrue(res.success, f"Step 3 must succeed: {res.message}")

        # 1. Verify 10 obsidian blocks in portal frame
        # Bottom 2 at Y=63
        self.assertEqual(self.client.get_block(x0 + 1, 63, z0).get("result", {}).get("name"), "obsidian")
        self.assertEqual(self.client.get_block(x0 + 2, 63, z0).get("result", {}).get("name"), "obsidian")
        # Posts at Y=64, 65, 66
        for y in [64, 65, 66]:
            self.assertEqual(self.client.get_block(x0 + 0, y, z0).get("result", {}).get("name"), "obsidian", f"Left post at Y={y} must be obsidian")
            self.assertEqual(self.client.get_block(x0 + 3, y, z0).get("result", {}).get("name"), "obsidian", f"Right post at Y={y} must be obsidian")
        # Top beam at Y=67
        self.assertEqual(self.client.get_block(x0 + 1, 67, z0).get("result", {}).get("name"), "obsidian")
        self.assertEqual(self.client.get_block(x0 + 2, 67, z0).get("result", {}).get("name"), "obsidian")

        # 2. Verify all 6 portal blocks formed
        for dx in [1, 2]:
            for y in [64, 65, 66]:
                pb = self.client.get_block(x0 + dx, y, z0).get("result", {}).get("name")
                self.assertEqual(pb, "nether_portal", f"Portal block at [{x0+dx}, {y}, {z0}] must be nether_portal")

        # 3. Verify temporary marker removed
        b_temp = self.client.get_block(rcon_stand_pos[0], rcon_stand_pos[1], rcon_stand_pos[2]).get("result", {}).get("name")
        self.assertIn(b_temp, ["air", "cave_air"], "Temporary standing marker must be mined away")

        # 4. Verify pure survival status (20 HP, not on fire)
        st_final = self.client.get_status().get("result", {})
        self.assertEqual(st_final.get("health"), 20, "Bot must take 0 damage throughout Step 3")
        self.assertFalse(st_final.get("on_fire"), "Bot must not catch fire")

        logger.info("[BotSleepTest PASSED] Step 3 verified successfully!")


def main():
    suite = unittest.TestLoader().loadTestsFromTestCase(BotSleepTest)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        exit(1)


if __name__ == "__main__":
    main()
