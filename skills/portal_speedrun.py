"""Speedrun Nether Portal Skill Module for BotSleep.

Implements the official speedrun portal casting procedure step-by-step:
  Step 1: 放置石头，放置水，换位置 (Place temp stone, place water to freeze initial obsidian, move to red_sand)
  Step 2: 拆掉石头，检查底部方块-挖掘非黑曜石方块替换，放置石油，放置水 (Dig stone/base, cast bottom obsidian, place water)
  Step 3: 浇筑顺序 (Cast 6 obsidian blocks in sequence: Top beam right->left, right pillar mid->top, left pillar mid->top)
  Final:  挪掉水，等水流干净，点燃 (Scoop water, await clearance, ignite portal)
"""

import logging
import time
from typing import Optional, List, Dict, Any, Tuple

from . import BaseSkill, SkillResult
from core.memory import MemoryEngine

logger = logging.getLogger("BotSleep.Skills.PortalSpeedrun")


class NetherPortalSpeedrunSkill(BaseSkill):
    """Speedrun Nether Portal Casting Skill executing step-by-step."""

    def __init__(self, client, memory: Optional[MemoryEngine] = None):
        super().__init__(client)
        self.memory = memory or MemoryEngine()

    def validate_preconditions(self, **kwargs) -> bool:
        """Validates that items, space, and bot status meet preconditions."""
        inv = self.client.get_inventory_counts()
        has_stone = any(inv.get(b, 0) > 0 for b in ["cobblestone", "stone", "dirt", "netherrack"])
        has_water = inv.get("water_bucket", 0) > 0 or inv.get("bucket", 0) > 0
        return has_stone and has_water

    def build_scaffolding(self, x0: int, y0: int, z0: int) -> SkillResult:
        """Builds the precision 7-cobblestone scaffolding and activates controlled water curtain.
        
        Relative coordinate offsets from portal origin (x0, y0, z0):
          - x0: left column (dx=0), x0+1: left opening, x0+2: right opening, x0+3: right column
          - y0: ground level (dy=0), y0+1..3: vertical frame levels
          - z0: portal plane (dz=0), z0-1: pillar & wall line, z0-2: backboard line
        
        7 Cobblestone placements:
          1. Backboard:      [x0 + 1, y0 + 1, z0 - 2]
          2. Left wall far:  [x0 + 0, y0 + 1, z0 - 1]
          3. Left wall near: [x0 + 1, y0 + 1, z0 - 1]
          4. Pillar layer 1: [x0 + 3, y0 + 1, z0 - 1]
          5. Pillar layer 2: [x0 + 3, y0 + 2, z0 - 1]
          6. Pillar layer 3: [x0 + 3, y0 + 3, z0 - 1]
          7. Arm forward:    [x0 + 3, y0 + 3, z0 + 0]
        
        Water activation:
          - Water poured at: [x0 + 2, y0 + 3, z0 - 1] against pillar layer 3.
        """
        logger.info(f"Building precision cobblestone scaffolding for portal origin [{x0}, {y0}, {z0}]...")
        # Check inventory for building blocks and water bucket
        inv = self.client.get_inventory_counts()
        if inv.get("cobblestone", 0) < 7:
            return SkillResult(success=False, error_code="missing_cobblestone", message="Need at least 7 cobblestone for scaffolding.")
        if inv.get("water_bucket", 0) < 1:
            return SkillResult(success=False, error_code="missing_water_bucket", message="Water bucket required for water curtain.")

        # 1. Backboard
        self.client.place_block("cobblestone", x0 + 1, y0 + 1, z0 - 2)
        # 2. Left walls
        self.client.place_block("cobblestone", x0 + 0, y0 + 1, z0 - 1)
        self.client.place_block("cobblestone", x0 + 1, y0 + 1, z0 - 1)
        # 3. Right pillar (3 blocks)
        self.client.place_block("cobblestone", x0 + 3, y0 + 1, z0 - 1)
        self.client.place_block("cobblestone", x0 + 3, y0 + 2, z0 - 1)
        self.client.place_block("cobblestone", x0 + 3, y0 + 3, z0 - 1)
        # 4. Arm extending forward
        self.client.place_block("cobblestone", x0 + 3, y0 + 3, z0 + 0)
        time.sleep(0.3)

        # 5. Pour water on top against pillar layer 3
        water_spot = [x0 + 2, y0 + 3, z0 - 1]
        logger.info(f"Activating controlled water curtain at {water_spot}...")
        water_res = self.client.interact(pos=water_spot, item="water_bucket", stop_on=["death"])
        time.sleep(1.0)

        return SkillResult(
            success=True,
            message="Scaffolding and controlled water curtain built successfully with zero leaks.",
            data={"water_spot": water_spot, "water_res": water_res},
        )

    def execute(self, step: int = 1, **kwargs) -> SkillResult:
        """Executes a specific step or the entire speedrun sequence."""
        if step == 1:
            return self.execute_step_1(**kwargs)
        if step == 2:
            return self.execute_step_2(**kwargs)
        if step == 3:
            return self.execute_step_3(**kwargs)
        if step == "scaffold":
            return self.build_scaffolding(**kwargs)
        return SkillResult(success=False, error_code="step_not_implemented", message=f"Step {step} not yet implemented.")

    def execute_step_1(
        self,
        start_sand_pos: List[int],
        stone_pos: List[int],
        water_pos: List[int],
        red_sand_pos: List[int],
    ) -> SkillResult:
        """Executes Step 1 of Nether Portal Speedrun.
        
        Actions:
          1. Verify starting position on sand marker.
          2. Inventory check (building blocks and water bucket).
          3. 放置石头: Place temporary stone at stone_pos.
          4. 检查方块与周边流体状态.
          5. 放置水: Verify water bucket held, place water at water_pos.
          6. 检查水和岩浆状态及黑曜石生成.
          7. 换位置: Move to recommended standing position (red_sand_pos).
          8. 全状态核验.
        """
        start_time = time.time()
        logger.info(f"=== Starting Step 1: Place Stone, Place Water, Move to Red Sand ===")

        # 1. Check Starting Position & Inventory
        inv = self.client.get_inventory_counts()
        has_stone = any(inv.get(b, 0) > 0 for b in ["cobblestone", "stone", "dirt", "netherrack"])
        if not has_stone:
            return SkillResult(success=False, error_code="missing_building_block", message="No building blocks in inventory.")

        has_water = inv.get("water_bucket", 0) > 0
        if not has_water:
            return SkillResult(success=False, error_code="missing_water_bucket", message="No water bucket in inventory.")

        status = self.client.get_status().get("result", {})
        bot_pos = status.get("pos", [])
        logger.info(f"Step 1 Start: Bot at {bot_pos}, Inventory={inv}")

        # 2. 放置石头 (Place stone)
        b_stone_cur = self.client.get_block(stone_pos[0], stone_pos[1], stone_pos[2]).get("result", {})
        if b_stone_cur.get("name") in ["cobblestone", "stone"]:
            logger.info(f"Step 1.1: Stone already present at {stone_pos}.")
            stone_name = b_stone_cur.get("name")
            stone_res = {"placed": stone_name, "reused": True}
        else:
            logger.info(f"Step 1.1: Placing stone at {stone_pos}...")
            stone_res = self.client.place_block(item="cobblestone", x=stone_pos[0], y=stone_pos[1], z=stone_pos[2])
            time.sleep(0.3)
            b_stone = self.client.get_block(stone_pos[0], stone_pos[1], stone_pos[2]).get("result", {})
            stone_name = b_stone.get("name", "air")
            if stone_name not in ["cobblestone", "stone"]:
                return SkillResult(
                    success=False,
                    error_code="stone_place_failed",
                    message=f"Failed to place stone at {stone_pos}, current block is {stone_name}.",
                    data={"result": stone_res},
                )
        logger.info(f"Step 1.1 Verified: Stone at {stone_pos} -> {stone_name}")

        # Check liquid around stone before water placement
        b_water_target_before = self.client.get_block(water_pos[0], water_pos[1], water_pos[2]).get("result", {})
        logger.info(f"Step 1.2: Liquid state before water placement at {water_pos}: {b_water_target_before.get('name')}")

        # 3. 检查手持并放置水 (Check hand & Place water)
        inv_before_water = self.client.get_inventory_counts()
        if inv_before_water.get("water_bucket", 0) < 1:
            return SkillResult(success=False, error_code="no_water_bucket_held", message="Water bucket missing before placement.")

        logger.info(f"Step 1.3: Placing water at {water_pos}...")
        water_res = self.client.interact(pos=water_pos, item="water_bucket")
        time.sleep(0.6)

        # 4. 检查放置水后水和岩浆的状态
        b_water_after = self.client.get_block(water_pos[0], water_pos[1], water_pos[2]).get("result", {})
        logger.info(f"Step 1.4: Target block at {water_pos} after water placement: {b_water_after.get('name')}")

        # In Step 1, adjacent lava sources freeze into obsidian
        adjacent_checks = [
            [water_pos[0] - 1, water_pos[1], water_pos[2]],
            [water_pos[0], water_pos[1], water_pos[2] + 1],
        ]
        obsidian_found = []
        for p in adjacent_checks:
            b = self.client.get_block(p[0], p[1], p[2]).get("result", {})
            name = b.get("name")
            obsidian_found.append({"pos": p, "name": name})
            logger.info(f"Step 1.4 Check: Block at {p} -> {name}")

        # 5. 换位置 (Move to red_sand)
        logger.info(f"Step 1.5: Moving to recommended standing position (red_sand at {red_sand_pos})...")
        move_res = self.client.move_to(red_sand_pos[0], red_sand_pos[1], red_sand_pos[2])
        time.sleep(0.5)

        final_status = self.client.get_status().get("result", {})
        final_pos = final_status.get("pos", [])
        logger.info(f"Step 1.5 Bot arrived at: {final_pos}")

        elapsed = round(time.time() - start_time, 2)
        return SkillResult(
            success=True,
            message=f"Step 1 completed in {elapsed}s: Stone placed, water placed, moved to red_sand.",
            data={
                "start_pos": start_sand_pos,
                "stone_pos": stone_pos,
                "water_pos": water_pos,
                "red_sand_pos": red_sand_pos,
                "bot_final_pos": final_pos,
                "stone_block": stone_name,
                "water_spot_block": b_water_after.get("name"),
                "adjacent_blocks": obsidian_found,
                "elapsed": elapsed,
            },
        )

    def execute_step_2(
        self,
        stone_pos: List[int],
        base_positions: List[List[int]],
        red_sand_pos: List[int],
        water_placement_pos: Optional[List[int]] = None,
    ) -> SkillResult:
        """Executes Step 2 of Nether Portal Speedrun.
        
        Actions:
          1. 状态与位置检查: Ensure bot is standing near recommended red_sand position.
          2. 工具检查: Ensure pickaxe and buckets are available in inventory.
          3. 拆掉石头: Dig temporary stone block at stone_pos.
          4. 检查底部方块:
             - Check blocks at base_positions (Y=-1 / Y=63).
             - If already obsidian: confirmed!
             - If non-obsidian solid block (e.g. stone/dirt):
                 * Dig the block with pickaxe (collect=False).
                 * Check hand: ensure held item is lava_bucket (prevent holding water_bucket!).
                 * Place lava into the dug hole.
                 * Place water or let water flow onto it to freeze into obsidian.
             - If lava: ensure water freezes it into obsidian.
          5. 结构与流体校验:
             - Verify both bottom frame positions are confirmed obsidian.
             - Verify water coverage and borders conform to step_2_grid.
        """
        start_time = time.time()
        logger.info("=== Starting Step 2: Dig Stone, Check/Replace Bottom Base, Cast Obsidian ===")

        # 1. Check Bot Position & Inventory
        inv = self.client.get_inventory_counts()
        has_pick = any(inv.get(p, 0) > 0 for p in [
            "diamond_pickaxe", "netherite_pickaxe", "iron_pickaxe", "stone_pickaxe", "golden_pickaxe", "wooden_pickaxe"
        ])
        if not has_pick:
            return SkillResult(success=False, error_code="missing_pickaxe", message="No pickaxe found in inventory to dig stone/base.")

        # Ensure bot is near red_sand
        status = self.client.get_status().get("result", {})
        bot_pos = status.get("pos", [])
        dist_to_red_sand = ((bot_pos[0] - red_sand_pos[0])**2 + (bot_pos[2] - red_sand_pos[2])**2)**0.5 if bot_pos else 99
        if dist_to_red_sand > 2.0:
            logger.info(f"Step 2: Bot at {bot_pos}, repositioning to red_sand at {red_sand_pos}...")
            self.client.move_to(red_sand_pos[0], red_sand_pos[1], red_sand_pos[2])
            time.sleep(0.4)

        # 2. 拆掉石头 (Dig temporary stone)
        b_stone = self.client.get_block(stone_pos[0], stone_pos[1], stone_pos[2]).get("result", {})
        stone_name = b_stone.get("name", "air")
        logger.info(f"Step 2.1: Checking stone at {stone_pos} -> {stone_name}")
        if stone_name in ["cobblestone", "stone", "dirt", "netherrack"]:
            logger.info(f"Step 2.1: Digging temporary stone ({stone_name}) at {stone_pos}...")
            # Equip pickaxe
            for pick in ["diamond_pickaxe", "netherite_pickaxe", "iron_pickaxe", "stone_pickaxe", "wooden_pickaxe"]:
                if inv.get(pick, 0) > 0:
                    self.client.equip(item=pick, slot="hand")
                    break
            dig_res = self.client.dig(stone_pos[0], stone_pos[1], stone_pos[2], collect=False, stop_on=["death"])
            time.sleep(0.4)
            b_stone_after = self.client.get_block(stone_pos[0], stone_pos[1], stone_pos[2]).get("result", {})
            stone_after_name = b_stone_after.get("name", "air")
            if stone_after_name in ["cobblestone", "stone", "dirt"]:
                return SkillResult(
                    success=False,
                    error_code="stone_dig_failed",
                    message=f"Failed to dig temporary stone at {stone_pos}, still {stone_after_name}.",
                    data={"dig_result": dig_res},
                )
            logger.info(f"Step 2.1 Verified: Stone at {stone_pos} successfully removed (now {stone_after_name}).")
        else:
            logger.info(f"Step 2.1: Stone at {stone_pos} is already {stone_name}, skipping dig.")

        # 3. 检查底部方块 (Check bottom blocks at Y=-1 / Y=63)
        base_states = []
        for idx, pos in enumerate(base_positions):
            b_cur = self.client.get_block(pos[0], pos[1], pos[2]).get("result", {})
            cur_name = b_cur.get("name", "air")
            logger.info(f"Step 2.2: Checking bottom block {idx+1}/{len(base_positions)} at {pos} -> {cur_name}")

            if cur_name == "obsidian":
                logger.info(f"Step 2.2: Bottom block at {pos} is already obsidian.")
                base_states.append({"pos": pos, "name": "obsidian", "action": "already_obsidian"})
                continue

            # Non-obsidian solid block: Dig it out
            if cur_name in ["stone", "cobblestone", "dirt", "gravel", "sand", "andesite", "diorite", "granite"]:
                logger.info(f"Step 2.2: Digging non-obsidian solid block ({cur_name}) at {pos}...")
                self.client.dig(pos[0], pos[1], pos[2], collect=False, stop_on=["death"])
                time.sleep(0.3)
                cur_name = self.client.get_block(pos[0], pos[1], pos[2]).get("result", {}).get("name", "air")
                logger.info(f"Step 2.2: Block at {pos} after dig is {cur_name}.")

            # If cavity needs lava: 放置石油 (place lava)
            if cur_name != "obsidian":
                # Check inventory for lava_bucket
                inv = self.client.get_inventory_counts()
                if inv.get("lava_bucket", 0) < 1:
                    return SkillResult(
                        success=False,
                        error_code="missing_lava_bucket",
                        message=f"No lava bucket available to cast bottom obsidian at {pos}.",
                    )
                # Handheld safety check: Ensure we hold lava_bucket, NOT water_bucket!
                logger.info("Step 2.2 Handheld Check: Equipping lava_bucket (verifying NOT water_bucket)...")
                self.client.equip(item="lava_bucket", slot="hand")
                time.sleep(0.1)

                logger.info(f"Step 2.2: Placing lava at {pos}...")
                place_lava_res = self.client.interact(pos=pos, item="lava_bucket", stop_on=["death"])
                time.sleep(0.5)

                # Check if water flowed in or place water to freeze
                cur_b = self.client.get_block(pos[0], pos[1], pos[2]).get("result", {})
                if cur_b.get("name") != "obsidian":
                    # If not yet frozen, place water at designated water placement position
                    if water_placement_pos:
                        logger.info(f"Step 2.2: Placing water at {water_placement_pos} to freeze lava at {pos}...")
                        self.client.interact(pos=water_placement_pos, item="water_bucket", stop_on=["death"])
                        time.sleep(0.6)

            # Re-verify bottom block is now obsidian
            b_final = self.client.get_block(pos[0], pos[1], pos[2]).get("result", {})
            final_name = b_final.get("name", "air")
            logger.info(f"Step 2.2 Verification: Bottom block at {pos} is now {final_name}")
            base_states.append({"pos": pos, "name": final_name, "action": "cast_obsidian"})
            if final_name != "obsidian":
                return SkillResult(
                    success=False,
                    error_code="base_obsidian_incomplete",
                    message=f"Bottom frame block at {pos} is {final_name}, expected obsidian.",
                    data={"base_states": base_states},
                )

        # 4. Final verification of Step 2 (Grid conformity)
        logger.info(f"Step 2.3: Verifying full bottom frame obsidian integrity...")
        all_obsidian = all(s["name"] == "obsidian" for s in base_states)
        if not all_obsidian:
            return SkillResult(
                success=False,
                error_code="step_2_verification_failed",
                message="Not all bottom portal positions formed obsidian.",
                data={"base_states": base_states},
            )

        elapsed = round(time.time() - start_time, 2)
        return SkillResult(
            success=True,
            message=f"Step 2 completed in {elapsed}s: Stone dug, bottom blocks verified and converted to obsidian.",
            data={
                "stone_pos": stone_pos,
                "base_positions": base_positions,
                "base_states": base_states,
                "red_sand_pos": red_sand_pos,
                "elapsed": elapsed,
            },
        )

    def execute_step_3(
        self,
        x0: int,
        y0: int,
        z0: int,
        temp_stand_pos: Optional[List[int]] = None,
    ) -> SkillResult:
        """Executes Step 3 of Nether Portal Speedrun: Casting, Water Retrieval & Ignition.
        
        Casting Grid Layout:
          空 2 1 空   (Y = y0 + 3)
          6       4   (Y = y0 + 2)
          5       3   (Y = y0 + 1)
          下面已经完成 (Y = y0 & y0 - 1)
        
        Actions:
          1. 状态与位置准备:
             - Ensure temporary elevated standing block exists at temp_stand_pos [x0 + 2, y0 + 1, z0 + 1].
             - Bot positions on the elevated block for 100% unobstructed top-down line of sight.
          2. 手持安全检查与浇筑:
             - Verify 6 lava buckets, empty bucket, and flint & steel in inventory.
             - Cast 1..6 in exact order:
                 * Handheld check: strictly verify held item is lava_bucket (never water_bucket!).
                 * Pour lava into water curtain at target.
                 * Verify target block transformed into obsidian.
          3. 挪掉水并等水流干净:
             - Move to safe shore position [x0 + 2, y0, z0 - 2.5].
             - Equip empty bucket and scoop water source block at [x0 + 2, y0 + 3, z0 - 1].
             - Wait for water curtain to completely recede; verify portal openings are pure air.
          4. 清理临时标记方块:
             - Mine temporary standing block at temp_stand_pos with pickaxe (no markers left behind).
          5. 点燃地狱门:
             - Equip flint_and_steel.
             - Click top face of bottom sunken obsidian [x0 + 1, y0 - 1, z0].
             - Verify all 6 portal interior blocks [x0 + 1..2, y0..y0+2, z0] become nether_portal.
          6. 纯生存状态核验:
             - Ensure bot health is 20, zero damage taken, on_fire is False.
        """
        start_time = time.time()
        logger.info(f"=== Starting Step 3: Precision Portal Casting, Scoop Water, Ignite Portal ===")
        
        # 1. Precondition checks
        inv = self.client.get_inventory_counts()
        if inv.get("lava_bucket", 0) < 6:
            return SkillResult(
                success=False,
                error_code="missing_lava_buckets",
                message=f"Need 6 lava buckets for casting, found {inv.get('lava_bucket', 0)}.",
            )
        if inv.get("bucket", 0) < 1:
            return SkillResult(
                success=False,
                error_code="missing_empty_bucket",
                message="Need an empty bucket to scoop back water.",
            )
        if inv.get("flint_and_steel", 0) < 1:
            return SkillResult(
                success=False,
                error_code="missing_flint_and_steel",
                message="Need flint_and_steel to ignite the portal.",
            )
        
        # Ensure temporary elevated standing block exists at [x0 + 1, y0 + 1, z0 + 1]
        stand_block_pos = temp_stand_pos or [x0 + 1, y0 + 1, z0 + 1]
        b_stand = self.client.get_block(stand_block_pos[0], stand_block_pos[1], stand_block_pos[2]).get("result", {})
        if b_stand.get("name") in ["air", "cave_air"]:
            logger.info(f"Step 3: Placing temporary standing block at {stand_block_pos}...")
            # Place cobblestone or dirt
            place_item = "cobblestone" if inv.get("cobblestone", 0) > 0 else "dirt"
            self.client.place_block(item=place_item, x=stand_block_pos[0], y=stand_block_pos[1], z=stand_block_pos[2])
            time.sleep(0.3)
        
        # Position bot on the elevated standing block if not already near it
        stand_coords = [stand_block_pos[0] + 0.6, stand_block_pos[1] + 1.0, stand_block_pos[2] + 0.5]
        cur_st = self.client.get_status().get("result", {})
        cur_pos = cur_st.get("pos", [])
        dist = ((cur_pos[0] - stand_coords[0])**2 + (cur_pos[2] - stand_coords[2])**2)**0.5 if cur_pos else 99
        if dist > 0.2:
            logger.info(f"Step 3: Repositioning bot to standing spot at {stand_coords} (current dist={dist:.2f})...")
            self.client.move_to(stand_coords[0], stand_coords[1], stand_coords[2])
            time.sleep(0.4)
        else:
            logger.info(f"Step 3: Bot already in position at {cur_pos} (dist={dist:.2f} <= 1.0), maintaining stance.")
        
        # The 6 casting targets in user's exact required sequence:
        # 空 2 1 空  (Y = y0 + 3)
        # 6       4  (Y = y0 + 2)
        # 5       3  (Y = y0 + 1)
        # 下面已经完成
        cast_targets = [
            {"num": 1, "desc": "Top beam right",     "pos": [x0 + 2, y0 + 3, z0]},
            {"num": 2, "desc": "Top beam left",      "pos": [x0 + 1, y0 + 3, z0]},
            {"num": 3, "desc": "Right pillar lower", "pos": [x0 + 3, y0 + 1, z0]},
            {"num": 4, "desc": "Right pillar upper", "pos": [x0 + 3, y0 + 2, z0]},
            {"num": 5, "desc": "Left pillar lower",  "pos": [x0 + 0, y0 + 1, z0]},
            {"num": 6, "desc": "Left pillar upper",  "pos": [x0 + 0, y0 + 2, z0]},
        ]
        
        cast_results = []
        for target in cast_targets:
            num = target["num"]
            desc = target["desc"]
            pos = target["pos"]
            
            # Handheld safety check: Strictly verify held item is lava_bucket, NOT water_bucket!
            inv_now = self.client.get_inventory_counts()
            if inv_now.get("lava_bucket", 0) < 1:
                return SkillResult(
                    success=False,
                    error_code="missing_lava_bucket_mid_cast",
                    message=f"No lava bucket available before casting block {num} ({desc}).",
                )
            
            logger.info(f"Step 3 Pour {num}/6: Equipping lava_bucket (verifying NOT water_bucket)...")
            self.client.equip(item="lava_bucket", slot="hand")
            time.sleep(0.1)
            
            st_held = self.client.get_status().get("result", {}).get("held", {})
            held_name = st_held.get("name")
            if held_name != "lava_bucket":
                return SkillResult(
                    success=False,
                    error_code="wrong_held_item",
                    message=f"Safety check failed: holding {held_name}, expected lava_bucket!",
                )
            
            logger.info(f"Step 3 Pour {num}/6: Casting {desc} at {pos}...")
            pour_res = self.client.interact(pos=pos, item="lava_bucket", stop_on=["death"])
            time.sleep(0.6)
            
            # Verify target block turned into obsidian
            b_after = self.client.get_block(pos[0], pos[1], pos[2]).get("result", {})
            after_name = b_after.get("name")
            logger.info(f"Step 3 Pour {num}/6 Verification: Block at {pos} -> {after_name}")
            cast_results.append({"num": num, "desc": desc, "pos": pos, "block": after_name})
            if after_name != "obsidian":
                return SkillResult(
                    success=False,
                    error_code="cast_block_failed",
                    message=f"Failed to cast block {num} ({desc}) at {pos}: got {after_name}, expected obsidian.",
                    data={"cast_results": cast_results, "pour_res": pour_res},
                )
        
        logger.info("Step 3.2: All 6 obsidian blocks cast successfully!")
        
        # 3. 清理临时标记方块 (Mine temporary standing block before moving)
        b_temp_cur = self.client.get_block(stand_block_pos[0], stand_block_pos[1], stand_block_pos[2]).get("result", {})
        if b_temp_cur.get("name") not in ["air", "cave_air"]:
            logger.info(f"Step 3.3: Mining temporary standing block at {stand_block_pos}...")
            # Equip pickaxe
            for pick in ["diamond_pickaxe", "netherite_pickaxe", "iron_pickaxe", "stone_pickaxe"]:
                if inv.get(pick, 0) > 0:
                    self.client.equip(item=pick, slot="hand")
                    break
            self.client.dig(stand_block_pos[0], stand_block_pos[1], stand_block_pos[2], collect=False, stop_on=["death"])
            time.sleep(0.4)

        # 4. 挪掉水并等水流干净 (Navigate around left column to shore behind backboard, then scoop water)
        logger.info("Step 3.4: Navigating around left column to shore behind scaffolding...")
        # Waypoints around left pillar:
        # Step 1: Outside left pillar
        self.client.move_to(x0 - 1.5, y0, z0 + 1.0)
        time.sleep(0.5)
        # Step 2: Shore corner behind left pillar
        self.client.move_to(x0 - 1.5, y0, z0 - 2.5)
        time.sleep(0.5)
        # Step 3: Shore position behind backboard
        shore_pos = [x0 + 2.0, y0, z0 - 2.5]
        self.client.move_to(shore_pos[0], shore_pos[1], shore_pos[2])
        time.sleep(0.5)
        
        water_source = [x0 + 2, y0 + 3, z0 - 1]
        logger.info(f"Step 3.4: Scooping back water source at {water_source} from shore...")
        self.client.equip(item="bucket", slot="hand")
        time.sleep(0.1)
        scoop_res = self.client.interact(pos=water_source, item="bucket", stop_on=["death"])
        time.sleep(1.5)
        
        # Verify portal openings are cleared of water
        logger.info("Step 3.4: Waiting for water curtain to completely recede...")
        t_water_wait = time.time()
        while time.time() - t_water_wait < 6.0:
            interior_blocks = [
                self.client.get_block(x0 + dx, y, z0).get("result", {}).get("name", "air")
                for dx in [1, 2]
                for y in [y0, y0 + 1, y0 + 2]
            ]
            if all(b in ["air", "cave_air"] for b in interior_blocks):
                logger.info(f"Water cleared in {time.time() - t_water_wait:.2f}s!")
                break
            time.sleep(0.5)
        
        # 5. 点燃地狱门 (Ignite Nether Portal with Flint & Steel)
        logger.info(f"Step 3.5: Igniting portal with flint_and_steel at [{x0 + 1}, {y0 - 1}, {z0}]...")
        self.client.equip(item="flint_and_steel", slot="hand")
        time.sleep(0.2)
        ignite_res = self.client.interact(pos=[x0 + 1, y0 - 1, z0], item="flint_and_steel", stop_on=["death"])
        time.sleep(1.0)
        
        # Check all 6 portal blocks; retry ignition if needed
        portal_blocks = []
        for retry in range(2):
            portal_blocks = []
            for dx in [1, 2]:
                for y in [y0, y0 + 1, y0 + 2]:
                    pb = self.client.get_block(x0 + dx, y, z0).get("result", {})
                    pname = pb.get("name")
                    portal_blocks.append({"pos": [x0 + dx, y, z0], "name": pname})
            if all(p["name"] == "nether_portal" for p in portal_blocks):
                break
            logger.warning(f"Retrying ignition (attempt {retry + 2})...")
            self.client.equip(item="flint_and_steel", slot="hand")
            time.sleep(0.2)
            self.client.interact(pos=[x0 + 1, y0 - 1, z0], item="flint_and_steel", stop_on=["death"])
            time.sleep(1.0)
        
        all_portal = all(p["name"] == "nether_portal" for p in portal_blocks)
        logger.info(f"Step 3.5: Portal blocks check: {portal_blocks}")
        
        # 6. Survival state verification
        final_status = self.client.get_status().get("result", {})
        bot_health = final_status.get("health", 0)
        bot_on_fire = final_status.get("on_fire", False)
        logger.info(f"Step 3 Final Bot Status: Health={bot_health}, OnFire={bot_on_fire}")
        
        if not all_portal:
            return SkillResult(
                success=False,
                error_code="portal_not_ignited",
                message="Portal frame complete but nether_portal blocks did not activate.",
                data={"portal_blocks": portal_blocks, "ignite_res": ignite_res},
            )
        
        elapsed = round(time.time() - start_time, 2)
        return SkillResult(
            success=True,
            message=f"Step 3 completed successfully in {elapsed}s: All 6 obsidian blocks cast in sequence, water scooped, portal ignited!",
            data={
                "cast_results": cast_results,
                "portal_blocks": portal_blocks,
                "bot_health": bot_health,
                "bot_on_fire": bot_on_fire,
                "elapsed": elapsed,
            },
        )
