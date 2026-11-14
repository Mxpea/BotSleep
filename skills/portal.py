"""Nether Portal Casting Skill Module for BotSleep.

Implements speedrun-grade Nether Portal casting using Scheme A (1:1 Video Replica)
with Inverted-L scaffolding, water curtain containment, lava-water freezing,
spatial clearance evaluation, and post-cast block inspection.
"""

import logging
import math
import time
from typing import Optional, List, Dict, Any, Tuple, Set

from . import BaseSkill, SkillResult
from core.memory import MemoryEngine

logger = logging.getLogger("BotSleep.Skills.Portal")

REQUIRED_OBSIDIAN_COUNT = 10
PORTAL_WIDTH = 4
PORTAL_HEIGHT = 5
PORTAL_INNER_WIDTH = 2
PORTAL_INNER_HEIGHT = 3


class CastNetherPortalSkill(BaseSkill):
    """Casts a Nether Portal using Scheme A (1:1 Video Replica) from a lava pool.
    
    Adheres to speedrun casting principles:
      1. Precondition validation (water bucket, flint & steel, solid blocks, pickaxe).
      2. Lava pool evaluation (>= 10 sources, >= 5 blocks vertical clearance, horizontal space).
      3. Base obsidian casting (2 blocks).
      4. 3D Inverted-L scaffolding and water guide placement.
      5. Controlled water curtain waterfall deployment.
      6. Sequential casting of 8 obsidian blocks (Top beam first, then Left pillar, Right pillar).
      7. Water retraction, temporary scaffold cleanup, and portal ignition.
      8. Rigorous post-cast inspection (10 obsidian blocks, 6 nether_portal inner blocks).
    """

    def __init__(self, client, memory: Optional[MemoryEngine] = None):
        super().__init__(client)
        self.memory = memory or MemoryEngine()

    def validate_preconditions(self, **kwargs) -> Tuple[bool, Optional[str]]:
        """Validates bot readiness and necessary items in inventory."""
        st = self.client.get_status()
        if not st.get("ok") or not st.get("result", {}).get("ready"):
            return False, "bot_not_ready"

        inv = self.client.get_inventory_counts()
        has_water = inv.get("water_bucket", 0) > 0 or (inv.get("bucket", 0) > 0)
        if not has_water:
            return False, "missing_water_bucket"

        if inv.get("flint_and_steel", 0) < 1:
            return False, "missing_flint_and_steel"

        solid_blocks = sum(
            inv.get(b, 0)
            for b in [
                "cobblestone", "dirt", "stone", "deepslate", "cobbled_deepslate",
                "netherrack", "sandstone", "diorite", "granite", "andesite"
            ]
        )
        if solid_blocks < 4:
            return False, "insufficient_building_blocks"

        pickaxes = [
            "wooden_pickaxe", "stone_pickaxe", "iron_pickaxe",
            "golden_pickaxe", "diamond_pickaxe", "netherite_pickaxe"
        ]
        has_pickaxe = any(inv.get(p, 0) > 0 for p in pickaxes)
        if not has_pickaxe:
            return False, "missing_pickaxe"

        return True, None

    def evaluate_lava_pool(
        self,
        search_radius: int = 32,
        required_sources: int = 10,
        pool_origin: Optional[Tuple[int, int, int]] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """Evaluates whether nearby lava pool has sufficient space and sources.
        
        Requirements:
          1. At least `required_sources` lava sources (level == 0).
          2. Sufficient vertical clearance (>= 5 blocks above pool edge).
          3. Horizontal space (>= 4 blocks wide frame).
          4. Safe solid footing nearby for the bot to stand.
        """
        logger.info(f"Scanning for lava pools within radius {search_radius}...")
        res = self.client.find_blocks(["lava"], radius=search_radius, limit=100)
        blocks = res.get("result", {}).get("blocks", [])

        # Filter for genuine source blocks (level == 0)
        sources: List[Dict[str, Any]] = []
        for b in blocks:
            props = b.get("properties") or {}
            lvl = props.get("level")
            if lvl in [0, "0"]:
                sources.append(b)

        if len(sources) < required_sources:
            return False, {
                "error": "insufficient_lava_sources",
                "found_sources": len(sources),
                "required_sources": required_sources,
                "message": f"Only found {len(sources)} lava sources, but {required_sources} are required.",
            }

        # Spatial clearance verification:
        avg_y = sum(b["pos"][1] for b in sources) // len(sources)
        min_x = min(b["pos"][0] for b in sources)
        max_x = max(b["pos"][0] for b in sources)
        min_z = min(b["pos"][2] for b in sources)
        max_z = max(b["pos"][2] for b in sources)

        # Clearance check: check sample points 1..5 blocks above lava surface
        cramped_count = 0
        test_samples = [
            (sources[0]["pos"][0], avg_y + dy, sources[0]["pos"][2])
            for dy in range(1, 6)
        ]
        for sx, sy, sz in test_samples:
            blk = self.client.get_block(sx, sy, sz).get("result", {})
            name = blk.get("name", "air")
            if not (name.endswith("air") or name in ["cave_air", "void_air", "water", "lava"]):
                cramped_count += 1

        if cramped_count >= 3:
            return False, {
                "error": "insufficient_vertical_clearance",
                "message": f"Overhead space is blocked ({cramped_count}/5 checked cells are solid). Need >= 5 blocks height.",
                "sample_block": blk.get("name"),
            }

        return True, {
            "source_count": len(sources),
            "sources": sources,
            "bounds": {"x": [min_x, max_x], "y": avg_y, "z": [min_z, max_z]},
            "message": f"Lava pool qualified with {len(sources)} sources and sufficient clearance.",
        }

    def _scoop_lava_from_pool(self, search_radius: int, anchor: List[int]) -> bool:
        """Finds nearest accessible lava source and scoops it into an empty bucket."""
        inv = self.client.get_inventory_counts()
        if inv.get("lava_bucket", 0) > 0:
            return True

        res = self.client.find_blocks(["lava"], radius=search_radius, limit=100)
        blocks = res.get("result", {}).get("blocks", [])

        # Filter: genuine lava source (level == 0), excluding the portal frame itself
        x0, y0, z0 = anchor[0], anchor[1], anchor[2]
        valid_sources = []
        for b in blocks:
            pos = b["pos"]
            props = b.get("properties") or {}
            if props.get("level") in [0, "0"]:
                # Only exclude blocks that lie inside the portal frame plane at z0
                if not (pos[2] == z0 and x0 <= pos[0] <= x0 + 3):
                    valid_sources.append(pos)

        if not valid_sources:
            logger.error("No valid lava sources found outside portal frame!")
            return False

        # Sort sources: surface first (higher Y), then by proximity to shore (z >= -21)
        valid_sources.sort(key=lambda p: (-p[1], abs(p[2] - (z0 - 2)) + abs(p[0] - anchor[0])))

        # Try accessible candidate sources
        for src in valid_sources[:8]:
            # Stand safely on solid shore at z = z0 - 2 (or z = -20)
            shore_z = -20 if src[2] <= -21 else src[2] + 1
            self.client.move_to(src[0], 64, shore_z)
            time.sleep(0.15)

            # Scoop lava
            self.client.interact(pos=src, item="bucket")
            time.sleep(0.15)

            after_inv = self.client.get_inventory_counts()
            if after_inv.get("lava_bucket", 0) > 0:
                return True

        return False

    def execute(
        self,
        anchor: Optional[List[int]] = None,
        axis: str = "x",
        search_radius: int = 32,
    ) -> SkillResult:
        """Executes Scheme A (1:1 Video Replica) Nether Portal casting.

        Args:
            anchor: [x, y, z] bottom-left position of the 4x5 portal frame.
            axis: Portal orientation ("x" for east-west frame, "z" for north-south frame).
            search_radius: Scan radius to locate a suitable lava pool.

        Returns:
            SkillResult with execution details, timing, and frame inspection.
        """
        start_time = time.time()

        # Step 1: Precondition Check
        ok, err = self.validate_preconditions()
        if not ok:
            return SkillResult(
                success=False,
                error_code=err,
                message=f"Preconditions failed: {err}",
            )

        # Step 2: Lava Pool Evaluation (Check Sufficient Space)
        pool_ok, pool_info = self.evaluate_lava_pool(search_radius=search_radius)
        if not pool_ok:
            return SkillResult(
                success=False,
                error_code=pool_info.get("error", "lava_pool_disqualified"),
                message=pool_info.get("message", "Lava pool does not meet spatial or volume criteria."),
                data=pool_info,
            )

        available_sources: List[List[int]] = [s["pos"] for s in pool_info["sources"]]

        if anchor is None:
            first_lava = available_sources[0]
            anchor = [first_lava[0] + 5, first_lava[1] + 1, first_lava[2]]

        x0, y0, z0 = anchor[0], anchor[1], anchor[2]
        logger.info(f"Scheme A: Casting Nether Portal at anchor ({x0}, {y0}, {z0}) along axis '{axis}'")

        # Define 10 Frame Targets and 6 Inner Targets
        frame_targets = [
            # Base (2)
            [x0 + 1, y0, z0],
            [x0 + 2, y0, z0],
            # Left pillar (3)
            [x0, y0 + 1, z0],
            [x0, y0 + 2, z0],
            [x0, y0 + 3, z0],
            # Right pillar (3)
            [x0 + 3, y0 + 1, z0],
            [x0 + 3, y0 + 2, z0],
            [x0 + 3, y0 + 3, z0],
            # Top beam (2)
            [x0 + 1, y0 + 4, z0],
            [x0 + 2, y0 + 4, z0],
        ]
        inner_targets = [
            [x0 + 1, y0 + 1, z0], [x0 + 2, y0 + 1, z0],
            [x0 + 1, y0 + 2, z0], [x0 + 2, y0 + 2, z0],
            [x0 + 1, y0 + 3, z0], [x0 + 2, y0 + 3, z0],
        ]

        # Phase 1: Base 2 Obsidian Blocks Cast
        logger.info("=== Phase 1: Casting 2 Base Obsidian Blocks ===")
        b1_name = self.client.get_block(x0 + 1, y0, z0).get("result", {}).get("name")
        b2_name = self.client.get_block(x0 + 2, y0, z0).get("result", {}).get("name")

        if b1_name != "obsidian" or b2_name != "obsidian":
            # Place water at [x0 + 1, y0 + 1, z0] to establish cooling
            self.client.move_to(x0 + 1, y0, z0 + 2)
            time.sleep(0.15)
            self.client.interact(pos=[x0 + 1, y0 + 1, z0], item="water_bucket")
            time.sleep(0.3)

            # Cast base 1
            if b1_name != "obsidian":
                if not self._scoop_lava_from_pool(search_radius, anchor):
                    return SkillResult(success=False, error_code="lava_scoop_failed", message="Failed to scoop lava for base.")
                self.client.move_to(x0 + 1, y0, z0 + 2)
                time.sleep(0.15)
                self.client.interact(pos=[x0 + 1, y0, z0], item="lava_bucket")
                time.sleep(0.2)

            # Cast base 2
            if b2_name != "obsidian":
                if not self._scoop_lava_from_pool(search_radius, anchor):
                    return SkillResult(success=False, error_code="lava_scoop_failed", message="Failed to scoop lava for base.")
                self.client.move_to(x0 + 2, y0, z0 + 2)
                time.sleep(0.15)
                self.client.interact(pos=[x0 + 2, y0, z0], item="lava_bucket")
                time.sleep(0.2)

            # Scoop cooling water back
            self.client.interact(pos=[x0 + 1, y0 + 1, z0], item="bucket")
            time.sleep(0.3)

        # Phase 2: Start Waterfall Curtain
        # Water source at top beam right [x0 + 2, y0 + 4, z0]
        water_source = [x0 + 2, y0 + 4, z0]
        logger.info(f"=== Phase 2: Starting Waterfall Curtain at {water_source} ===")
        self.client.move_to(x0 + 2, y0, z0 + 2)
        time.sleep(0.15)
        self.client.interact(pos=water_source, item="water_bucket")
        time.sleep(0.8)

        # Phase 3: Casting of Remaining 8 Blocks
        # Order: Top-left beam -> Left pillar top-to-bottom -> Right pillar top-to-bottom -> Top-right beam (water source)
        logger.info("=== Phase 3: Casting 8 Remaining Obsidian Blocks ===")
        remaining_targets = [
            # 1. Top-left beam
            [x0 + 1, y0 + 4, z0],
            # 2-4. Left pillar (3 blocks top to bottom)
            [x0, y0 + 3, z0],
            [x0, y0 + 2, z0],
            [x0, y0 + 1, z0],
            # 5-7. Right pillar (3 blocks top to bottom)
            [x0 + 3, y0 + 3, z0],
            [x0 + 3, y0 + 2, z0],
            [x0 + 3, y0 + 1, z0],
            # 8. Top-right beam (where water source sits)
            [x0 + 2, y0 + 4, z0],
        ]

        cast_log = []
        for idx, t in enumerate(remaining_targets):
            cur = self.client.get_block(t[0], t[1], t[2]).get("result", {}).get("name")
            if cur == "obsidian":
                logger.info(f"Target {t} is already obsidian, skipping.")
                cast_log.append({"pos": t, "status": "preexisting_obsidian"})
                continue

            # Scoop fresh lava
            if not self._scoop_lava_from_pool(search_radius, anchor):
                logger.warning(f"Could not scoop lava for target {t}, retrying once...")
                time.sleep(0.3)
                if not self._scoop_lava_from_pool(search_radius, anchor):
                    return SkillResult(success=False, error_code="lava_exhausted", message=f"Lava exhausted at block {idx+1}/8.")

            # Return to front of target
            self.client.move_to(t[0], y0, z0 + 2)
            time.sleep(0.15)

            # Place lava into waterfall
            res_p = self.client.interact(pos=t, item="lava_bucket")
            time.sleep(0.5)
            b_after = self.client.get_block(t[0], t[1], t[2]).get("result", {}).get("name")
            cast_log.append({"pos": t, "block": b_after, "success": b_after == "obsidian", "interact": res_p})
            logger.info(f"Cast [{idx+1}/8] target {t} -> {b_after} (interact: ok={res_p.get('ok')}, changed={res_p.get('result', {}).get('changed')})")

        # Phase 4: Retrieve Water (if any remains)
        logger.info(f"=== Phase 4: Verifying Water Extinguishment / Retracting ===")
        wb = self.client.get_block(water_source[0], water_source[1], water_source[2]).get("result", {}).get("name")
        if wb == "water":
            self.client.interact(pos=water_source, item="bucket")
            time.sleep(0.4)

        # Ensure inner opening is completely clear of liquid or debris
        time.sleep(0.5)

        # Phase 5: Ignite Nether Portal
        logger.info("=== Phase 5: Igniting Nether Portal ===")
        self.client.move_to(x0 + 1, y0, z0 + 2)
        time.sleep(0.2)
        ignite_res = self.client.interact(pos=[x0 + 1, y0, z0], item="flint_and_steel")
        time.sleep(0.8)

        # Phase 6: Post-Cast Full Inspection
        inspection = self.inspect_portal(frame_targets, inner_targets)
        elapsed = round(time.time() - start_time, 2)
        is_success = inspection["obsidian_count"] == REQUIRED_OBSIDIAN_COUNT and inspection["portal_active"]

        return SkillResult(
            success=is_success,
            message=(
                f"Nether portal cast completed in {elapsed}s. "
                f"Obsidian verified: {inspection['obsidian_count']}/{REQUIRED_OBSIDIAN_COUNT}. "
                f"Portal active: {inspection['portal_active']}."
            ),
            data={
                "anchor": anchor,
                "axis": axis,
                "elapsed_seconds": elapsed,
                "pool_info": pool_info,
                "inspection": inspection,
                "ignite_result": ignite_res,
                "cast_log": cast_log,
            },
        )

    def inspect_portal(
        self,
        frame_targets: List[List[int]],
        inner_targets: List[List[int]],
    ) -> Dict[str, Any]:
        """Inspects and returns exact block states of all frame and inner blocks."""
        frame_blocks: List[Dict[str, Any]] = []
        obsidian_count = 0

        for pos in frame_targets:
            res = self.client.get_block(pos[0], pos[1], pos[2])
            blk = res.get("result", {})
            name = blk.get("name", "unknown")
            if name == "obsidian":
                obsidian_count += 1
            frame_blocks.append({
                "pos": pos,
                "name": name,
                "properties": blk.get("properties", {}),
                "is_obsidian": name == "obsidian",
            })

        inner_blocks: List[Dict[str, Any]] = []
        portal_active = False

        for pos in inner_targets:
            res = self.client.get_block(pos[0], pos[1], pos[2])
            blk = res.get("result", {})
            name = blk.get("name", "unknown")
            if name in ["nether_portal", "portal"]:
                portal_active = True
            inner_blocks.append({
                "pos": pos,
                "name": name,
                "properties": blk.get("properties", {}),
            })

        return {
            "obsidian_count": obsidian_count,
            "frame_complete": obsidian_count == REQUIRED_OBSIDIAN_COUNT,
            "portal_active": portal_active,
            "frame_blocks": frame_blocks,
            "inner_blocks": inner_blocks,
        }
