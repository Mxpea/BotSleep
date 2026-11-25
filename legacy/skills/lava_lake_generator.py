"""Natural & Randomized Lava Lake Generator for BotSleep.

Generates realistic surface lava lakes in Minecraft via RCON with randomized
dimensions, organic shoreline noise, varied shoreline materials, trees,
and approach pathways from 20 blocks away.
"""

import math
import random
import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger("BotSleep.LavaLakeGenerator")


class LavaLakeGenerator:
    """Procedural generator for natural and randomized Minecraft lava lakes."""

    def __init__(self, rcon):
        self.rcon = rcon

    def safe_fill(self, x1: int, y1: int, z1: int, x2: int, y2: int, z2: int, block: str):
        """Fills a volume in Minecraft, automatically chunking to respect 32,768 block limit."""
        xa, xb = min(x1, x2), max(x1, x2)
        ya, yb = min(y1, y2), max(y1, y2)
        za, zb = min(z1, z2), max(z1, z2)
        vol = (xb - xa + 1) * (yb - ya + 1) * (zb - za + 1)
        if vol <= 30000:
            self.rcon.command(f"fill {xa} {ya} {za} {xb} {yb} {zb} {block}")
        else:
            dx = xb - xa + 1
            dy = yb - ya + 1
            dz = zb - za + 1
            if dy >= dx and dy >= dz:
                mid_y = (ya + yb) // 2
                self.safe_fill(xa, ya, za, xb, mid_y, zb, block)
                self.safe_fill(xa, mid_y + 1, za, xb, yb, zb, block)
            elif dx >= dz:
                mid_x = (xa + xb) // 2
                self.safe_fill(xa, ya, za, mid_x, yb, zb, block)
                self.safe_fill(mid_x + 1, ya, za, xb, yb, zb, block)
            else:
                mid_z = (za + zb) // 2
                self.safe_fill(xa, ya, za, xb, yb, mid_z, block)
                self.safe_fill(xa, ya, mid_z + 1, xb, yb, zb, block)

    def generate_random_lake(
        self,
        center_x: int,
        center_y: int,
        center_z: int,
        seed: Optional[int] = None,
        create_trees: bool = True,
        num_trees: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Generates a randomized natural lava lake and surrounding terrain."""
        rng = random.Random(seed) if seed is not None else random.Random()
        
        rx = rng.randint(6, 9)
        rz = rng.randint(5, 7)
        tree_count = num_trees if num_trees is not None else rng.randint(1, 3)

        return self.generate_natural_lake(
            center_x=center_x,
            center_y=center_y,
            center_z=center_z,
            rx=rx,
            rz=rz,
            create_tree=create_trees,
            num_trees=tree_count,
            narrow_shore=True,
            rng=rng,
        )

    def generate_natural_lake(
        self,
        center_x: int,
        center_y: int,
        center_z: int,
        rx: int = 7,
        rz: int = 5,
        create_tree: bool = True,
        num_trees: int = 1,
        narrow_shore: bool = True,
        rng: Optional[random.Random] = None,
    ) -> Dict[str, Any]:
        """Carves a natural lava lake and surrounding organic environment."""
        if rng is None:
            rng = random.Random()

        cx, cy, cz = center_x, center_y, center_z
        logger.info(f"Generating natural lava lake at [{cx}, {cy}, {cz}] (rx={rx}, rz={rz}, trees={num_trees})...")

        # Casting baseline coordinates
        casting_baseline = {
            "x0": cx - 1,
            "y0": cy,
            "z0": cz - rz + 3,
        }
        x0, y0, z0 = casting_baseline["x0"], casting_baseline["y0"], casting_baseline["z0"]

        approach_shore = [cx + 1, cy, z0 - 3]
        start_20_blocks = [cx + 1, cy, approach_shore[2] - 20]

        # 1. Clear wide airspace across the entire operating footprint (up to Y+32)
        min_x = min(cx - rx - 12, start_20_blocks[0] - 8)
        max_x = max(cx + rx + 12, start_20_blocks[0] + 8)
        min_z = min(cz - rz - 12, start_20_blocks[2] - 6)
        max_z = max(cz + rz + 12, approach_shore[2] + 8)

        self.safe_fill(min_x, cy + 1, min_z, max_x, cy + 32, max_z, "air")

        # 2. Level surrounding base ground (Y=cy-2 stone, Y=cy grass_block)
        self.safe_fill(min_x, cy - 2, min_z, max_x, cy - 1, max_z, "stone")
        self.safe_fill(min_x, cy, min_z, max_x, cy, max_z, "grass_block")

        # Shoreline materials palette
        shore_palette = ["gravel", "stone", "coarse_dirt", "dirt", "andesite", "sand", "granite"]

        # 3. Carve organic lava basin with noise
        lava_count = 0
        for dx in range(-rx - 2, rx + 3):
            for dz in range(-rz - 2, rz + 3):
                noise = 0.12 * math.sin(dx * 1.3) * math.cos(dz * 1.5) + 0.08 * math.sin((dx + dz) * 0.9)
                dist_sq = (dx / rx) ** 2 + (dz / rz) ** 2 + noise
                
                is_protrusion = False
                if narrow_shore and dz <= -rz + 2:
                    if dx < -1 or dx > 3:
                        is_protrusion = True

                if dist_sq <= 0.85 and not is_protrusion:
                    self.rcon.command(f"setblock {cx+dx} {cy-2} {cz+dz} stone")
                    self.rcon.command(f"setblock {cx+dx} {cy-1} {cz+dz} lava")
                    self.rcon.command(f"setblock {cx+dx} {cy} {cz+dz} lava")
                    lava_count += 1
                elif dist_sq <= 1.25:
                    mat = rng.choice(shore_palette)
                    self.rcon.command(f"setblock {cx+dx} {cy} {cz+dz} {mat}")

        # Guarantee deep lava at portal baseline and surrounding casting pool:
        for px in range(x0 - 1, x0 + 5):
            for pz in range(z0, z0 + 4):
                self.rcon.command(f"setblock {px} {y0-2} {pz} stone")
                self.rcon.command(f"setblock {px} {y0-1} {pz} lava")
                self.rcon.command(f"setblock {px} {y0} {pz} lava")

        # 4. Safe Runner Approach Shore & Wide Pier:
        # Pave wide cobblestone platform at [x0-3 .. x0+5], [z0-4 .. z0-1] at Y=y0
        for px in range(x0 - 3, x0 + 6):
            for pz in range(z0 - 4, z0):
                self.rcon.command(f"setblock {px} {y0-2} {pz} stone")
                self.rcon.command(f"setblock {px} {y0-1} {pz} stone")
                self.rcon.command(f"setblock {px} {y0} {pz} cobblestone")

        # 5. Clear portal volume airspace (x0-4 .. x0+6, y0+1 .. y0+9, z0-5 .. z0+5)
        self.rcon.command(f"fill {x0-4} {y0+1} {z0-5} {x0+6} {y0+9} {z0+5} air")

        # 6. Pave solid 20-block runway with deep foundation from start_20_blocks to approach_shore
        pave_min_x = min(start_20_blocks[0], x0) - 3
        pave_max_x = max(start_20_blocks[0], x0 + 3) + 3
        pave_min_z = start_20_blocks[2] - 4
        pave_max_z = approach_shore[2] + 1

        self.rcon.command(f"fill {pave_min_x} {cy-2} {pave_min_z} {pave_max_x} {cy-1} {pave_max_z} stone")
        self.rcon.command(f"fill {pave_min_x} {cy} {pave_min_z} {pave_max_x} {cy} grass_block")
        self.rcon.command(f"fill {pave_min_x} {cy+1} {pave_min_z} {pave_max_x} {cy+6} {pave_max_z} air")

        # 7. Plant randomized natural oak trees on the outer shore (safe distance from path and portal)
        tree_positions = []
        if create_tree and num_trees > 0:
            candidate_zones = [
                (cx - rx - 3, cz),           # Far West shore
                (cx + rx + 3, cz),           # Far East shore
                (cx - rx - 2, cz + rz - 1),  # Southwest shore
                (cx + rx + 2, cz + rz - 1),  # Southeast shore
                (cx, cz + rz + 2),          # South far shore
            ]
            rng.shuffle(candidate_zones)
            for i in range(min(num_trees, len(candidate_zones))):
                tx, tz = candidate_zones[i]
                tree_positions.append([tx, cy, tz])
                self._plant_oak_tree(tx, cy, tz)

        logger.info(
            f"Randomized lava lake created with {lava_count} surface lava blocks.\n"
            f"  Approach shore: {approach_shore}\n"
            f"  Start 20 blocks away: {start_20_blocks}\n"
            f"  Casting baseline: {casting_baseline}\n"
            f"  Trees planted: {len(tree_positions)} at {tree_positions}"
        )

        return {
            "center": [cx, cy, cz],
            "surface_y": cy,
            "rx": rx,
            "rz": rz,
            "lava_count": lava_count,
            "tree_positions": tree_positions,
            "approach_shore": approach_shore,
            "start_20_blocks": start_20_blocks,
            "casting_baseline": casting_baseline,
        }

    def _plant_oak_tree(self, tx: int, ty: int, tz: int):
        """Plants a standard natural oak tree."""
        self.rcon.command(f"fill {tx-2} {ty+1} {tz-2} {tx+2} {ty+7} {tz+2} air")
        for dy in range(5):
            self.rcon.command(f"setblock {tx} {ty+dy} {tz} oak_log")
        for ldx in range(-2, 3):
            for ldz in range(-2, 3):
                for ldy in range(2, 4):
                    if abs(ldx) + abs(ldz) <= 3:
                        self.rcon.command(f"setblock {tx+ldx} {ty+ldy+1} {tz+ldz} oak_leaves keep")
        self.rcon.command(f"setblock {tx} {ty+5} {tz} oak_leaves")
