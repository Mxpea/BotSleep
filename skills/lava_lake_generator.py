"""Natural Lava Lake Generator for BotSleep.

Generates realistic surface lava lakes in Minecraft via RCON.
Features:
  - Organic basin carving with noise and irregular shoreline.
  - Realistic shoreline materials: gravel, stone, coarse_dirt, grass_block.
  - Configurable obstacles: nearby oak tree, narrow inlet/protrusion requiring bridging.
  - Generates approach path and starting point 20 blocks away.
"""

import math
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("BotSleep.LavaLakeGenerator")


class LavaLakeGenerator:
    """Procedural generator for natural Minecraft lava lakes."""

    def __init__(self, rcon):
        self.rcon = rcon

    def generate_natural_lake(
        self,
        center_x: int,
        center_y: int,
        center_z: int,
        rx: int = 7,
        rz: int = 5,
        create_tree: bool = True,
        narrow_shore: bool = True,
    ) -> Dict[str, Any]:
        """Carves a natural lava lake and surrounding environment.

        Args:
            center_x: Center X coordinate.
            center_y: Ground/surface Y level (e.g. 64).
            center_z: Center Z coordinate.
            rx: Semi-major radius in X.
            rz: Semi-minor radius in Z.
            create_tree: Whether to plant an oak tree on the shore.
            narrow_shore: Whether the north shore has an irregular protrusion/inlet
                          where initial lava width is narrow (< 4 blocks), requiring
                          the bot to place bridging blocks out into the lake.

        Returns:
            Dictionary with lake metadata, approach coordinates, and casting baseline.
        """
        cx, cy, cz = center_x, center_y, center_z
        logger.info(f"Generating natural lava lake at [{cx}, {cy}, {cz}] (rx={rx}, rz={rz})...")

        # 1. Clear airspace above lake and immediate surround (up to Y+9)
        self.rcon.command(f"fill {cx-rx-6} {cy+1} {cz-rz-6} {cx+rx+6} {cy+9} {cz-rz+6} air")

        # 2. Level surrounding base ground (Y=cy-2 stone, Y=cy grass)
        self.rcon.command(f"fill {cx-rx-6} {cy-2} {cz-rz-6} {cx+rx+6} {cy-1} {cz-rz+6} stone")
        self.rcon.command(f"fill {cx-rx-6} {cy} {cz-rz-6} {cx+rx+6} {cy} grass_block")

        # 3. Carve organic lava basin
        lava_count = 0
        for dx in range(-rx, rx + 1):
            for dz in range(-rz, rz + 1):
                dist_sq = (dx / rx) ** 2 + (dz / rz) ** 2
                
                # If narrow_shore is True: create a shore protrusion at north side
                # leaving only a narrow 2-block inlet at the edge (dx in [0, 1], dz == -rz + 1)
                # so a runner cannot immediately build on the natural shore without bridging!
                is_protrusion = False
                if narrow_shore and dz <= -rz + 2:
                    # Protrude ground into the lake on left (dx < 0) and right (dx > 2)
                    if dx < 0 or dx > 2:
                        is_protrusion = True

                if dist_sq <= 0.85 and not is_protrusion:
                    # Deep lava: basin floor at cy-2, lava sources at cy-1 and cy
                    self.rcon.command(f"setblock {cx+dx} {cy-2} {cz+dz} stone")
                    self.rcon.command(f"setblock {cx+dx} {cy-1} {cz+dz} lava")
                    self.rcon.command(f"setblock {cx+dx} {cy} {cz+dz} lava")
                    lava_count += 1
                elif dist_sq <= 1.15:
                    # Natural irregular rim: mix of gravel, stone, coarse_dirt, dirt
                    h = (dx * 7 + dz * 13) % 4
                    rim_mat = ["gravel", "stone", "coarse_dirt", "dirt"][h]
                    self.rcon.command(f"setblock {cx+dx} {cy} {cz+dz} {rim_mat}")

        # 4. Plant a natural oak tree on the northwest shore
        tree_pos = None
        if create_tree:
            tx = cx - rx - 1
            tz = cz - 2
            tree_pos = [tx, cy, tz]
            logger.info(f"Planting oak tree on shore at {tree_pos}...")
            # Trunk (5 logs tall)
            for dy in range(5):
                self.rcon.command(f"setblock {tx} {cy+dy} {tz} oak_log")
            # Leaves canopy
            for ldx in range(-2, 3):
                for ldz in range(-2, 3):
                    for ldy in range(2, 4):
                        if abs(ldx) + abs(ldz) <= 3:
                            self.rcon.command(f"setblock {tx+ldx} {cy+ldy+1} {tz+ldz} oak_leaves keep")
            self.rcon.command(f"setblock {tx} {cy+5} {tz} oak_leaves")

        # 5. Calculate approach point and 20-block starting point
        # Approach shore is at north edge of lake:
        approach_shore = [cx + 1, cy, cz - rz]
        # Start point exactly 20 blocks north of approach shore:
        start_20_blocks = [cx + 1, cy, approach_shore[2] - 20]

        # Ensure the 20-block path is cleared and paved with grass_block
        self.rcon.command(
            f"fill {start_20_blocks[0]-2} {cy} {start_20_blocks[2]-2} {approach_shore[0]+2} {cy} {approach_shore[2]} grass_block"
        )
        self.rcon.command(
            f"fill {start_20_blocks[0]-2} {cy+1} {start_20_blocks[2]-2} {approach_shore[0]+2} {cy+4} {approach_shore[2]} air"
        )

        # Baseline coordinates for portal casting in deep lava:
        # At north side, the open deep lava starts at z0 = cz - rz + 3 = cz - 2
        # Left post x0 = cx - 1, portal opening = [cx, cx+1], right post = cx + 2
        casting_baseline = {
            "x0": cx - 1,
            "y0": cy,
            "z0": cz - rz + 3,
        }

        # Bridging plan: from approach shore [cx+1, cy, cz-rz] towards deep lava
        # Bot walks onto shore, places bridging blocks out to reach the baseline!
        bridge_blocks = [
            [cx + 1, cy, cz - rz + 1],
            [cx + 1, cy, cz - rz + 2],
        ]

        logger.info(
            f"Lava lake created with {lava_count} surface lava blocks.\n"
            f"  Approach shore: {approach_shore}\n"
            f"  Start 20 blocks away: {start_20_blocks}\n"
            f"  Casting baseline: {casting_baseline}\n"
            f"  Bridging required: {len(bridge_blocks)} blocks {bridge_blocks}"
        )

        return {
            "center": [cx, cy, cz],
            "surface_y": cy,
            "lava_count": lava_count,
            "tree_pos": tree_pos,
            "approach_shore": approach_shore,
            "start_20_blocks": start_20_blocks,
            "casting_baseline": casting_baseline,
            "bridge_blocks": bridge_blocks,
        }
