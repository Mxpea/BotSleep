"""Portal Speedrun Telemetry & State Recorder for BotSleep.

Records the player state (position, health, hunger, on_fire, held item)
and block states (target block before/after, frame, interior, fluids)
at every step of the speedrun nether portal construction process.
Saves detailed JSON telemetry logs for debugging and analysis.
"""

import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("BotSleep.PortalTelemetry")


class PortalTelemetryRecorder:
    """Structured telemetry recorder for Minecraft speedrun portal execution."""

    def __init__(self, client, trial_id: Optional[str] = None):
        self.client = client
        self.trial_id = trial_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self.history: List[Dict[str, Any]] = []
        self.start_time = time.time()
        self.damage_events: List[Dict[str, Any]] = []
        self.last_health = 20.0
        self.last_on_fire = False

    def capture_player_state(self) -> Dict[str, Any]:
        """Captures complete player status from mc-agent-api."""
        try:
            st = self.client.get_status().get("result", {})
            pos = st.get("pos", [])
            health = st.get("health", 0.0)
            food = st.get("food", 20)
            on_fire = st.get("on_fire", False)
            held = st.get("held", {})
            held_name = held.get("name") if held else None
            held_count = held.get("count", 0) if held else 0

            # Detect damage or fire events
            if health < self.last_health:
                dmg = self.last_health - health
                self.damage_events.append({
                    "timestamp": time.time() - self.start_time,
                    "hp_lost": dmg,
                    "hp_now": health,
                    "pos": pos,
                    "on_fire": on_fire,
                })
                logger.warning(f"⚠️ [TELEMETRY DAMAGE ALERT] Bot took {dmg:.1f} damage! HP: {self.last_health} -> {health}, Fire={on_fire}")
            self.last_health = health
            self.last_on_fire = on_fire

            return {
                "pos": [round(c, 2) for c in pos] if pos else [],
                "health": health,
                "food": food,
                "on_fire": on_fire,
                "held_item": held_name,
                "held_count": held_count,
            }
        except Exception as e:
            logger.warning(f"Failed to capture player state: {e}")
            return {"error": str(e)}

    def capture_block_state(self, pos: List[int]) -> Dict[str, Any]:
        """Captures block state at the given coordinate."""
        try:
            b = self.client.get_block(pos[0], pos[1], pos[2]).get("result", {})
            return {
                "pos": pos,
                "name": b.get("name", "unknown"),
                "hardness": b.get("hardness"),
                "material": b.get("material"),
            }
        except Exception as e:
            return {"pos": pos, "name": "query_failed", "error": str(e)}

    def capture_portal_matrix(self, x0: int, y0: int, z0: int) -> Dict[str, Any]:
        """Captures complete 10-block frame and 6 interior portal blocks."""
        frame_coords = [
            # Bottom 2
            [x0 + 1, y0 - 1, z0],
            [x0 + 2, y0 - 1, z0],
            # Left pillar 2
            [x0 + 0, y0 + 1, z0],
            [x0 + 0, y0 + 2, z0],
            # Right pillar 2
            [x0 + 3, y0 + 1, z0],
            [x0 + 3, y0 + 2, z0],
            # Top beam 2
            [x0 + 1, y0 + 3, z0],
            [x0 + 2, y0 + 3, z0],
            # Ground corners / posts if any
            [x0 + 0, y0, z0],
            [x0 + 3, y0, z0],
        ]
        interior_coords = [
            [x0 + 1, y0 + dy, z0] for dy in [0, 1, 2]
        ] + [
            [x0 + 2, y0 + dy, z0] for dy in [0, 1, 2]
        ]

        frame_blocks = [self.capture_block_state(p) for p in frame_coords]
        interior_blocks = [self.capture_block_state(p) for p in interior_coords]

        obsidian_count = sum(1 for b in frame_blocks if b.get("name") == "obsidian")
        portal_count = sum(1 for b in interior_blocks if b.get("name") == "nether_portal")

        return {
            "obsidian_frame_count": obsidian_count,
            "nether_portal_count": portal_count,
            "frame": frame_blocks,
            "interior": interior_blocks,
        }

    def record_step(
        self,
        step_id: str,
        description: str,
        target_pos: Optional[List[int]] = None,
        anchor_ref: Optional[List[int]] = None,
        anchor_face: Optional[List[int]] = None,
        action_func: Optional[Callable[[], Any]] = None,
        portal_origin: Optional[Tuple[int, int, int]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Executes and records a step with comprehensive player and block states.
        
        Args:
            step_id: Unique identifier for the step (e.g. 'step1_place_water').
            description: Human readable description.
            target_pos: Block coordinate being interacted with or targeted.
            anchor_ref: Reference block for placement/interaction.
            anchor_face: Face normal for placement.
            action_func: Callable performing the actual API action.
            portal_origin: Tuple of (x0, y0, z0) if portal state check is desired.
            metadata: Additional contextual info.
        """
        step_start = time.time()
        elapsed_since_start = round(step_start - self.start_time, 2)

        # 1. Pre-action player and block states
        player_pre = self.capture_player_state()
        target_pre = self.capture_block_state(target_pos) if target_pos else None

        # 2. Execute action
        action_result = None
        action_error = None
        if action_func is not None:
            try:
                action_result = action_func()
            except Exception as e:
                action_error = str(e)
                logger.error(f"[TELEMETRY ERROR] Action in step '{step_id}' failed: {e}")

        step_duration = round(time.time() - step_start, 3)

        # 3. Post-action player and block states
        player_post = self.capture_player_state()
        target_post = self.capture_block_state(target_pos) if target_pos else None

        # 4. Optional portal matrix snapshot
        portal_snapshot = None
        if portal_origin:
            portal_snapshot = self.capture_portal_matrix(portal_origin[0], portal_origin[1], portal_origin[2])

        entry = {
            "step_id": step_id,
            "description": description,
            "elapsed_since_start_s": elapsed_since_start,
            "duration_s": step_duration,
            "target_pos": target_pos,
            "anchor_ref": anchor_ref,
            "anchor_face": anchor_face,
            "player_pre": player_pre,
            "player_post": player_post,
            "target_block_pre": target_pre,
            "target_block_post": target_post,
            "action_result": action_result,
            "action_error": action_error,
            "portal_snapshot": portal_snapshot,
            "metadata": metadata or {},
        }
        self.history.append(entry)

        # Format concise logger output
        pre_name = target_pre.get("name") if target_pre else "-"
        post_name = target_post.get("name") if target_post else "-"
        bot_pos_str = f"[{player_post.get('pos', [0,0,0])[0]:.1f}, {player_post.get('pos', [0,0,0])[1]:.1f}, {player_post.get('pos', [0,0,0])[2]:.1f}]" if player_post.get("pos") else "[]"
        held_str = f"{player_post.get('held_item')}x{player_post.get('held_count')}"
        hp_str = f"{player_post.get('health')}/20 HP"

        logger.info(
            f"📊 [{step_id}] {description} | "
            f"Bot: {bot_pos_str} {hp_str} (Held: {held_str}) | "
            f"Target {target_pos}: {pre_name} -> {post_name} ({step_duration:.2f}s)"
        )

        return action_result

    def dump_json(self, filepath: Optional[str] = None) -> str:
        """Dumps the telemetry history to a JSON file."""
        if not filepath:
            os.makedirs("logs/telemetry", exist_ok=True)
            filepath = os.path.abspath(f"logs/telemetry/trial_{self.trial_id}.json")
        else:
            os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)

        total_elapsed = round(time.time() - self.start_time, 2)
        summary = {
            "trial_id": self.trial_id,
            "total_elapsed_s": total_elapsed,
            "step_count": len(self.history),
            "damage_events": self.damage_events,
            "final_health": self.last_health,
            "final_on_fire": self.last_on_fire,
            "steps": self.history,
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

        logger.info(f"💾 Telemetry log successfully written to: {filepath}")
        return filepath
