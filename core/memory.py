"""Memory and Spatial Knowledge Base Engine for BotSleep

Persists and indexes all game logic, bot states, spatial landmarks, terrain grids,
and exploration episodes so AI and sub-agents can rapidly retrieve historical intelligence.
"""

import os
import json
import time
import math
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict


class MemoryEngine:
    """Persistent spatial memory and sub-agent knowledge retrieval engine."""

    def __init__(self, data_dir: str = "data/game_logic"):
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)

        self.state_file = os.path.join(self.data_dir, "bot_state.json")
        self.landmarks_file = os.path.join(self.data_dir, "landmarks.json")
        self.world_file = os.path.join(self.data_dir, "world_context.json")
        self.spatial_file = os.path.join(self.data_dir, "spatial_grid.json")
        self.episodes_file = os.path.join(self.data_dir, "episodic_memory.jsonl")

        self._init_files()

    def _init_files(self) -> None:
        """Initializes data storage files if they do not exist."""
        if not os.path.exists(self.landmarks_file):
            with open(self.landmarks_file, "w", encoding="utf-8") as f:
                json.dump({"spawn_point": None, "landmarks": []}, f, indent=2, ensure_ascii=False)

        if not os.path.exists(self.world_file):
            with open(self.world_file, "w", encoding="utf-8") as f:
                json.dump({"last_survey": None, "explored_biomes": [], "block_census": {}}, f, indent=2, ensure_ascii=False)

        if not os.path.exists(self.spatial_file):
            with open(self.spatial_file, "w", encoding="utf-8") as f:
                json.dump({"visited_waypoints": [], "explored_chunks": []}, f, indent=2, ensure_ascii=False)

    def save_bot_state(self, status: Dict[str, Any]) -> None:
        """Caches current bot position, vitals, orientation, and status."""
        state_data = {
            "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "username": status.get("username", "Unknown"),
            "pos": status.get("pos", [0, 0, 0]),
            "yaw": status.get("yaw", 0.0),
            "pitch": status.get("pitch", 0.0),
            "dimension": status.get("dimension", "overworld"),
            "gamemode": status.get("gamemode", "survival"),
            "health": status.get("health", 20),
            "food": status.get("food", 20),
            "saturation": status.get("saturation", 20),
            "xp_level": status.get("xp_level", 0),
            "held_item": status.get("held"),
            "offhand_item": status.get("offhand"),
            "armor_points": status.get("armor_points", 0),
            "is_sleeping": status.get("is_sleeping", False),
            "in_water": status.get("in_water", False),
            "in_lava": status.get("in_lava", False),
            "in_vehicle": status.get("in_vehicle"),
            "alive": status.get("alive", True),
            "ready": status.get("ready", True),
        }
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(state_data, f, indent=2, ensure_ascii=False)

    def record_landmark(
        self,
        name: str,
        category: str,
        pos: List[float],
        dimension: str = "overworld",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Records or updates a spatial landmark (e.g. bed, base, chest, mineral)."""
        data = {"spawn_point": None, "landmarks": []}
        if os.path.exists(self.landmarks_file):
            try:
                with open(self.landmarks_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass

        if category == "spawn":
            data["spawn_point"] = pos

        existing = False
        for lm in data.get("landmarks", []):
            if lm.get("category") == category and lm.get("pos") == pos:
                lm["metadata"] = metadata or {}
                lm["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                existing = True
                break

        if not existing:
            data["landmarks"].append({
                "name": name,
                "category": category,
                "pos": pos,
                "dimension": dimension,
                "metadata": metadata or {},
                "discovered_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            })

        with open(self.landmarks_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def record_waypoint(self, pos: List[float], dimension: str = "overworld") -> None:
        """Records an explored spatial waypoint to maintain coverage tracking."""
        try:
            with open(self.spatial_file, "r", encoding="utf-8") as f:
                grid = json.load(f)
        except Exception:
            grid = {"visited_waypoints": [], "explored_chunks": []}

        grid_pos = [round(pos[0], 1), round(pos[1], 1), round(pos[2], 1)]
        chunk_x = int(math.floor(pos[0] / 16))
        chunk_z = int(math.floor(pos[2] / 16))
        chunk_coord = [chunk_x, chunk_z]

        if chunk_coord not in grid.get("explored_chunks", []):
            grid.setdefault("explored_chunks", []).append(chunk_coord)

        grid.setdefault("visited_waypoints", []).append({
            "pos": grid_pos,
            "dimension": dimension,
            "t": time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        })

        if len(grid["visited_waypoints"]) > 1000:
            grid["visited_waypoints"] = grid["visited_waypoints"][-1000:]

        with open(self.spatial_file, "w", encoding="utf-8") as f:
            json.dump(grid, f, indent=2, ensure_ascii=False)

    def update_from_radar(self, poi_findings: Dict[str, List[Dict[str, Any]]], current_pos: List[float], dimension: str = "overworld") -> int:
        """Batch-updates spatial memory from radar POI discoveries."""
        count = 0
        for cat, items in poi_findings.items():
            if cat == "error":
                continue
            for item in items:
                name = item.get("name", "unknown")
                pos = item.get("pos", [0, 0, 0])
                dist = item.get("dist", 0.0)
                self.record_landmark(
                    name=name,
                    category=cat,
                    pos=pos,
                    dimension=dimension,
                    metadata={"dist_at_discovery": dist, "properties": item.get("properties")},
                )
                count += 1
        return count

    def query_pois(
        self,
        category: Optional[str] = None,
        max_distance: float = 64.0,
        from_pos: Optional[List[float]] = None,
    ) -> List[Dict[str, Any]]:
        """Queries stored landmarks sorted by proximity to from_pos."""
        if not os.path.exists(self.landmarks_file):
            return []

        try:
            with open(self.landmarks_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return []

        landmarks = data.get("landmarks", [])
        if category:
            landmarks = [lm for lm in landmarks if lm.get("category") == category]

        if from_pos:
            fx, fy, fz = from_pos
            for lm in landmarks:
                lx, ly, lz = lm["pos"]
                lm["current_dist"] = round(math.sqrt((lx - fx)**2 + (ly - fy)**2 + (lz - fz)**2), 1)
            landmarks = [lm for lm in landmarks if lm.get("current_dist", 999) <= max_distance]
            landmarks.sort(key=lambda lm: lm.get("current_dist", 999))

        return landmarks

    def get_subagent_context(self, subagent_type: str, current_pos: List[float]) -> Dict[str, Any]:
        """Assembles a high-density intelligence briefing for a delegated subagent."""
        context = {
            "subagent_type": subagent_type,
            "origin_pos": current_pos,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }

        if subagent_type in ["sleep_agent", "bed_seeker"]:
            context["nearest_beds"] = self.query_pois(category="bed", max_distance=64.0, from_pos=current_pos)
            context["safe_shelters"] = self.query_pois(category="shelter", max_distance=64.0, from_pos=current_pos)

        elif subagent_type in ["miner_agent", "gatherer"]:
            context["known_minerals"] = self.query_pois(category="mineral", max_distance=48.0, from_pos=current_pos)
            context["crafting_tables"] = self.query_pois(category="crafting", max_distance=32.0, from_pos=current_pos)
            context["chests"] = self.query_pois(category="container", max_distance=32.0, from_pos=current_pos)

        elif subagent_type in ["navigation_agent", "pathfinder"]:
            context["hazards"] = self.query_pois(category="liquid_and_hazard", max_distance=32.0, from_pos=current_pos)

        return context

    def append_episode(
        self,
        event_type: str,
        summary: str,
        pos: Optional[List[float]] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Appends an episodic memory event for AI recall."""
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "event_type": event_type,
            "summary": summary,
            "pos": pos or [0, 0, 0],
            "details": details or {},
        }
        with open(self.episodes_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
