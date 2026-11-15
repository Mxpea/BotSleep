"""Exploration Skills: Continuous Radar Sweep and Spatial Mapping."""

import threading
import time
import math
from typing import Optional, List, Dict, Any
from . import BaseSkill, SkillResult
from core.radar import BlockRadar
from core.memory import MemoryEngine


class ContinuousRadarSkill(BaseSkill):
    """Toggleable background autonomous radar that scans and maps the world as the bot moves."""

    def __init__(
        self,
        client,
        memory: Optional[MemoryEngine] = None,
        radar: Optional[BlockRadar] = None,
        min_move_distance: float = 6.0,
        scan_radius: int = 24,
    ):
        super().__init__(client)
        self.memory = memory or MemoryEngine()
        self.radar = radar or BlockRadar(client)
        self.min_move_distance = min_move_distance
        self.scan_radius = scan_radius

        self._active = False
        self._thread: Optional[threading.Thread] = None
        self._last_scan_pos: Optional[List[float]] = None
        self._lock = threading.Lock()

    @property
    def is_active(self) -> bool:
        return self._active

    def enable(self) -> SkillResult:
        """Enables the continuous radar exploration skill."""
        with self._lock:
            if self._active:
                return SkillResult(success=True, message="Continuous radar is already running.")
            self._active = True
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="ContinuousRadar")
            self._thread.start()
            return SkillResult(success=True, message="Continuous exploration radar activated.")

    def disable(self) -> SkillResult:
        """Disables the continuous radar exploration skill."""
        with self._lock:
            if not self._active:
                return SkillResult(success=True, message="Continuous radar is already stopped.")
            self._active = False
            return SkillResult(success=True, message="Continuous exploration radar deactivated.")

    def toggle(self) -> SkillResult:
        """Toggles the continuous radar skill state."""
        return self.disable() if self._active else self.enable()

    def validate_preconditions(self, **kwargs) -> bool:
        return self.client is not None

    def execute(self, force_sweep: bool = True, **kwargs) -> SkillResult:
        """Executes a single immediate radar sweep and stores intelligence into memory."""
        status = self.client.get_status().get("result", {})
        pos = status.get("pos", [0, 64, 0])
        dim = status.get("dimension", "overworld")

        # 1. Scan POIs
        findings = self.radar.scan_poi(radius=self.scan_radius)

        # 2. Update memory engine
        new_count = self.memory.update_from_radar(findings, current_pos=pos, dimension=dim)
        self.memory.record_waypoint(pos=pos, dimension=dim)
        self._last_scan_pos = pos

        summary = f"Radar sweep complete at {pos}. Found {new_count} POIs."
        self.memory.append_episode(
            event_type="radar_sweep",
            summary=summary,
            pos=pos,
            details={"findings_count": new_count},
        )

        return SkillResult(success=True, message=summary, data={"new_pois": new_count, "pos": pos})

    def _run_loop(self) -> None:
        """Background continuous monitoring loop."""
        while self._active:
            try:
                st = self.client.get_status().get("result", {})
                if st and st.get("ready"):
                    cur_pos = st.get("pos", [0, 64, 0])
                    should_sweep = False

                    if self._last_scan_pos is None:
                        should_sweep = True
                    else:
                        dx = cur_pos[0] - self._last_scan_pos[0]
                        dy = cur_pos[1] - self._last_scan_pos[1]
                        dz = cur_pos[2] - self._last_scan_pos[2]
                        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
                        if dist >= self.min_move_distance:
                            should_sweep = True

                    if should_sweep:
                        self.execute(force_sweep=True)

                time.sleep(1.0)
            except Exception:
                time.sleep(2.0)
