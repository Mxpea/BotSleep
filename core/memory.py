"""Memory and Cache Engine for BotSleep

Persists and recalls game logic data, bot state, landmarks, world context,
and episodic memories so the AI Agent can recall past context and decisions.
"""

import os
import json
import time
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict


@dataclass
class Landmark:
    name: str
    category: str  # 'bed', 'chest', 'shelter', 'spawn', 'death', 'mine'
    pos: List[float]
    dimension: str = "overworld"
    metadata: Dict[str, Any] = None
    discovered_at: str = ""


class MemoryEngine:
    def __init__(self, data_dir: str = "data/game_logic"):
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)

        self.state_file = os.path.join(self.data_dir, "bot_state.json")
        self.landmarks_file = os.path.join(self.data_dir, "landmarks.json")
        self.world_file = os.path.join(self.data_dir, "world_context.json")
        self.episodes_file = os.path.join(self.data_dir, "episodic_memory.jsonl")

        self._init_files()

    def _init_files(self) -> None:
        """Initializes data files if they do not exist."""
        if not os.path.exists(self.landmarks_file):
            with open(self.landmarks_file, "w", encoding="utf-8") as f:
                json.dump({"spawn_point": None, "landmarks": []}, f, indent=2, ensure_ascii=False)

        if not os.path.exists(self.world_file):
            with open(self.world_file, "w", encoding="utf-8") as f:
                json.dump({"last_survey": None, "explored_biomes": []}, f, indent=2, ensure_ascii=False)

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
        """Records or updates a spatial landmark (e.g. bed, base, chest)."""
        data = {"spawn_point": None, "landmarks": []}
        if os.path.exists(self.landmarks_file):
            try:
                with open(self.landmarks_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass

        if category == "spawn":
            data["spawn_point"] = pos

        # Update if exists, or append
        existing = False
        for lm in data.get("landmarks", []):
            if lm.get("name") == name and lm.get("category") == category:
                lm["pos"] = pos
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

    def save_world_context(self, context: Dict[str, Any]) -> None:
        """Saves environmental context (day/night, weather, surrounding map)."""
        with open(self.world_file, "w", encoding="utf-8") as f:
            json.dump(context, f, indent=2, ensure_ascii=False)

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

    def capture_full_snapshot(self, client) -> Dict[str, Any]:
        """Captures all observable live game state into persistent cache."""
        st_res = client.get_status()
        status = st_res.get("result", {})
        if not status:
            return {"ok": False, "error": "Could not fetch status"}

        pos = status.get("pos", [0, 0, 0])

        # 1. Save Bot State
        self.save_bot_state(status)

        # 2. Query surrounding blocks
        blocks = client.get_blocks(radius=8)
        beds = [b for b in blocks if "_bed" in b.get("name", "")]
        for bed in beds:
            self.record_landmark(
                name=bed["name"],
                category="bed",
                pos=bed["pos"],
                dimension=status.get("dimension", "overworld"),
                metadata={"dist": bed.get("dist")},
            )

        # 3. Query visible entities
        entities = client.get_entities(radius=16)

        # 4. Save World Context
        world_data = {
            "last_survey_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "time_of_day": status.get("time_of_day", 0),
            "is_day": status.get("is_day", True),
            "raining": status.get("raining", False),
            "thundering": status.get("thundering", False),
            "biome": status.get("biome", ""),
            "dimension": status.get("dimension", "overworld"),
            "surrounding_blocks_count": len(blocks),
            "visible_entities_count": len(entities),
            "entities_sample": entities[:10],
        }
        self.save_world_context(world_data)

        # 5. Append Episode
        self.append_episode(
            event_type="snapshot",
            summary=f"Bot snapshot recorded at {pos}. Weather: is_day={status.get('is_day')}, HP={status.get('health')}",
            pos=pos,
            details={"blocks_count": len(blocks), "entities_count": len(entities)},
        )

        return {
            "ok": True,
            "pos": pos,
            "blocks_count": len(blocks),
            "entities_count": len(entities),
            "beds_discovered": len(beds),
        }

    def get_memory_prompt_summary(self) -> str:
        """Formats recalled memory into a concise string for LLM system prompt."""
        if not os.path.exists(self.state_file):
            return "No previous memory recorded."

        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                st = json.load(f)
            with open(self.landmarks_file, "r", encoding="utf-8") as f:
                lm = json.load(f)
            with open(self.world_file, "r", encoding="utf-8") as f:
                wc = json.load(f)

            summary = [
                f"【Bot 历史记忆状态】",
                f"- 上次记录时间: {st.get('last_updated')}",
                f"- 记录位置: {st.get('pos')} (维度: {st.get('dimension')})",
                f"- 生命值: {st.get('health')}/20, 饥饿度: {st.get('food')}/20",
                f"- 环境: {'白天' if wc.get('is_day') else '黑夜'}, 雷暴: {wc.get('thundering')}",
            ]

            beds = [x for x in lm.get("landmarks", []) if x.get("category") == "bed"]
            if beds:
                summary.append(f"- 已记忆床位数量: {len(beds)}，最近床坐标: {beds[0].get('pos')}")
            else:
                summary.append("- 附近暂未发现已记忆的床位")

            return "\n".join(summary)
        except Exception as e:
            return f"Error loading memory: {e}"
