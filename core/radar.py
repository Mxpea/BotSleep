"""Radar Engine for 3D Block and Environment Perception

Scans surrounding blocks, terrain profiles, and strategic POIs for AI and sub-agents.
"""

import math
from typing import Dict, Any, List, Optional
from core.client import MinecraftAgentClient


class BlockRadar:
    """Multi-tier 3D block radar and terrain scanner."""

    # Strategic POI Category Definitions
    POI_DEFINITIONS = {
        "bed": [
            "white_bed", "orange_bed", "magenta_bed", "light_blue_bed", "yellow_bed",
            "lime_bed", "pink_bed", "gray_bed", "light_gray_bed", "cyan_bed",
            "purple_bed", "blue_bed", "brown_bed", "green_bed", "red_bed", "black_bed"
        ],
        "crafting": [
            "crafting_table", "furnace", "blast_furnace", "smoker", "anvil",
            "enchanting_table", "brewing_stand", "smithing_table", "loom"
        ],
        "container": [
            "chest", "trapped_chest", "barrel", "shulker_box", "ender_chest"
        ],
        "mineral": [
            "iron_ore", "deepslate_iron_ore", "coal_ore", "deepslate_coal_ore",
            "copper_ore", "deepslate_copper_ore", "gold_ore", "deepslate_gold_ore",
            "diamond_ore", "deepslate_diamond_ore", "ancient_debris"
        ],
        "liquid_and_hazard": [
            "water", "lava", "fire", "soul_fire", "campfire", "sweet_berry_bush", "tnt"
        ]
    }

    def __init__(self, client: MinecraftAgentClient):
        self.client = client

    def scan_census(self, radius: int = 16) -> Dict[str, Any]:
        """Performs a full-spectrum block census around the bot (counts & nearest coordinates)."""
        res = self.client.get_blocks(radius=radius)
        # Call mc-agent-api POST /find_blocks with no types to get global summary
        import requests
        url = f"{self.client.base_url}/find_blocks"
        try:
            resp = requests.post(
                url,
                headers=self.client.headers,
                json={"radius": radius},
                timeout=10,
            ).json()
            if resp.get("ok"):
                return resp.get("result", {})
        except Exception as e:
            return {"error": str(e), "radius": radius, "blocks": {}}
        return {"radius": radius, "blocks": {}}

    def scan_poi(
        self,
        radius: int = 24,
        categories: Optional[List[str]] = None,
        limit_per_category: int = 10,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Scans for strategic Points of Interest (Beds, Crafting, Containers, Minerals, Hazards)."""
        import requests
        url = f"{self.client.base_url}/find_blocks"
        target_categories = categories or list(self.POI_DEFINITIONS.keys())
        results: Dict[str, List[Dict[str, Any]]] = {}

        # Aggregate block names to scan
        types_to_scan = []
        cat_map = {}
        for cat in target_categories:
            block_names = self.POI_DEFINITIONS.get(cat, [])
            types_to_scan.extend(block_names)
            for b in block_names:
                cat_map[b] = cat
            results[cat] = []

        try:
            resp = requests.post(
                url,
                headers=self.client.headers,
                json={"types": types_to_scan, "radius": radius, "limit": 50},
                timeout=10,
            ).json()

            if resp.get("ok"):
                found_blocks = resp.get("result", {}).get("blocks", [])
                for fb in found_blocks:
                    cat = cat_map.get(fb.get("name"), "other")
                    if cat not in results:
                        results[cat] = []
                    if len(results[cat]) < limit_per_category:
                        results[cat].append(fb)

        except Exception as e:
            results["error"] = [{"message": str(e)}]

        return results

    def scan_terrain_profile(self, radius: int = 3) -> Dict[str, Any]:
        """Scans local terrain heightmap around the bot to detect cliffs, drops, and safe walk zones."""
        status = self.client.get_status().get("result", {})
        pos = status.get("pos", [0, 64, 0])
        bx, by, bz = round(pos[0]), round(pos[1]), round(pos[2])

        profile = {
            "origin": [bx, by, bz],
            "elevation_grid": {},
            "hazards_detected": [],
            "max_drop": 0,
            "max_climb": 0,
        }

        # Query surrounding 3x3 or 5x5 blocks
        blocks = self.client.get_blocks(radius=radius)
        block_lookup = {tuple(b["pos"]): b for b in blocks}

        for dx in range(-radius, radius + 1):
            for dz in range(-radius, radius + 1):
                key = f"{dx},{dz}"
                # Find surface block below or at player level
                found_surface = None
                for dy in range(2, -5, -1):
                    p = (bx + dx, by + dy, bz + dz)
                    b = block_lookup.get(p)
                    if b and b.get("name") not in ["air", "cave_air", "water"]:
                        found_surface = (by + dy, b.get("name"))
                        break

                if found_surface:
                    surf_y, b_name = found_surface
                    diff_y = surf_y - by
                    profile["elevation_grid"][key] = {"delta_y": diff_y, "block": b_name}
                    if diff_y < profile["max_drop"]:
                        profile["max_drop"] = diff_y
                    if diff_y > profile["max_climb"]:
                        profile["max_climb"] = diff_y
                    if diff_y <= -3:
                        profile["hazards_detected"].append({
                            "type": "cliff",
                            "rel_pos": [dx, diff_y, dz],
                            "depth": abs(diff_y),
                        })

        return profile

    def format_for_subagent(self, task_type: str, findings: Dict[str, Any]) -> Dict[str, Any]:
        """Tailors radar findings into a compact mission brief for sub-agents."""
        brief: Dict[str, Any] = {"task_type": task_type, "timestamp": None}

        if task_type == "sleep":
            beds = findings.get("bed", [])
            brief["available_beds"] = beds
            brief["can_sleep"] = len(beds) > 0
            brief["nearest_bed"] = beds[0] if beds else None

        elif task_type == "mining":
            brief["minerals"] = findings.get("mineral", [])
            brief["crafting"] = findings.get("crafting", [])

        elif task_type == "navigation":
            brief["hazards"] = findings.get("liquid_and_hazard", [])
            brief["terrain_profile"] = self.scan_terrain_profile(radius=3)

        return brief
