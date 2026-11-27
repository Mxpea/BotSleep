"""L3b CompetitionManager: exact 5 competition lifecycle tools + server /advancements scoring truth."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping, Optional

from v2.kernel.client import MinecraftAgentClient
from v2.kernel.storage import UnifiedWorldDB
from v2.primitives.base import PrimitiveContext
from v2.tasks.acquire import AcquireItemTask
from v2.tasks.base import Task
from v2.tasks.catalogue import TaskCatalogue
from v2.tasks.competition.rules import CompetitionEntry, scoring_catalog
from v2.tasks.nether_portal import EnterNetherTask


COMPETITION_TOOL_NAMES = (
    "select_task",
    "update_progress",
    "finish_task",
    "force_exit_task",
    "end_competition",
)


class CompetitionManager:
    """Manages scoring eligibility, server /advancements truth, and TaskCatalogue route binding."""

    def __init__(
        self,
        catalogue: TaskCatalogue,
        *,
        eligibility_overrides: Optional[Mapping[str, bool]] = None,
        db: Optional[UnifiedWorldDB] = None,
        defer_seconds: float = 300.0,
        reset: bool = False,
    ) -> None:
        self.catalogue = catalogue
        self.catalog: Dict[str, CompetitionEntry] = scoring_catalog(eligibility_overrides)
        self.db = db
        self.defer_seconds = float(defer_seconds)
        self.completed_ids: set[str] = set()
        self.active_task_id: Optional[str] = None
        self.progress_note: str = ""
        self.deferred_until: Dict[str, float] = {}
        self.ended: bool = False
        self.end_reason: str = ""
        if reset:
            self._persist()
        else:
            self._load_persisted()

    def _load_persisted(self) -> None:
        if self.db is None:
            return
        saved = self.db.load_checkpoint("competition")
        if not isinstance(saved, dict):
            return
        for aid in saved.get("completed_ids") or []:
            if isinstance(aid, str) and aid in self.catalog:
                self.completed_ids.add(aid)
        active = saved.get("active_task_id")
        if isinstance(active, str) and active in self.catalog and active not in self.completed_ids:
            self.active_task_id = active
        self.progress_note = str(saved.get("progress_note") or "")
        raw_def = saved.get("deferred_until")
        if isinstance(raw_def, dict):
            self.deferred_until = {str(k): float(v) for k, v in raw_def.items() if isinstance(v, (int, float))}
        self.ended = bool(saved.get("ended", False))
        self.end_reason = str(saved.get("end_reason") or "")

    def sync_server_advancements(self, client: MinecraftAgentClient) -> Dict[str, Any]:
        """Server `/advancements` (`status == 'completed'`) is the ONLY scoring truth."""
        env = client.advancements(status="completed")
        rows = env.get("result", {}).get("advancements", [])
        for row in rows:
            if row.get("status") == "completed" and isinstance(row.get("id"), str):
                adv_id = row["id"]
                if adv_id in self.catalog:
                    self.completed_ids.add(adv_id)
        if self.active_task_id in self.completed_ids:
            self.active_task_id = None
        self._persist()
        return self.snapshot()

    def is_executable(self, adv_id: str) -> bool:
        return adv_id in self.catalog and self.catalogue.advancement_route(adv_id) is not None

    def build_task(self, ctx: PrimitiveContext, adv_id: str) -> Optional[Task]:
        if adv_id not in self.catalog:
            return None
        route = self.catalogue.advancement_route(adv_id)
        if route is None:
            return None
        kind, target = route
        if kind == "enter_nether":
            return EnterNetherTask(ctx, self.catalogue)
        if kind == "avoid_vibration":
            from v2.tasks.loot import AvoidVibrationTask

            return AvoidVibrationTask(ctx)
        if kind == "acquire":
            return AcquireItemTask(ctx, self.catalogue, target, 1)
        return None

    # ---- Exact 5 Competition Lifecycle Tools ----

    def select_task(self, task_id: str) -> Dict[str, Any]:
        if self.ended:
            return {"ok": False, "error": "competition_ended"}
        if task_id not in self.catalog:
            return {"ok": False, "error": "ineligible_or_unknown_task"}
        if task_id in self.completed_ids:
            return {"ok": False, "error": "already_completed"}
        if not self.is_executable(task_id):
            return {"ok": False, "error": "no_executable_route"}
        self.active_task_id = task_id
        self.progress_note = "selected"
        self._persist()
        return {"ok": True, "active_task_id": task_id, "points": self.catalog[task_id].points}

    def update_progress(self, note: str) -> Dict[str, Any]:
        if self.active_task_id is None:
            return {"ok": False, "error": "no_active_task"}
        self.progress_note = str(note)[:200]
        self._persist()
        return {"ok": True, "active_task_id": self.active_task_id, "note": self.progress_note}

    def finish_task(self, client: MinecraftAgentClient) -> Dict[str, Any]:
        """Verify completion exclusively against fresh server `/advancements`."""
        target = self.active_task_id
        if target is None:
            return {"ok": False, "error": "no_active_task"}
        self.sync_server_advancements(client)
        if target in self.completed_ids:
            self.active_task_id = None
            self._persist()
            return {"ok": True, "verified": True, "task_id": target, "score": self.total_score}
        return {"ok": False, "verified": False, "error": "advancement_not_confirmed_by_server", "task_id": target}

    def force_exit_task(self, reason: str = "deferred") -> Dict[str, Any]:
        target = self.active_task_id
        if target is None:
            return {"ok": False, "error": "no_active_task"}
        self.deferred_until[target] = time.time() + self.defer_seconds
        self.active_task_id = None
        self.progress_note = reason
        self._persist()
        return {"ok": True, "exited": target, "reason": reason}

    def end_competition(self, reason: str = "completed") -> Dict[str, Any]:
        self.ended = True
        self.end_reason = reason
        self.active_task_id = None
        self._persist()
        return {"ok": True, "ended": True, "score": self.total_score, "reason": reason}

    @property
    def total_score(self) -> int:
        return sum(self.catalog[aid].points for aid in self.completed_ids if aid in self.catalog)

    def selectable_tasks(self, *, now: Optional[float] = None) -> List[CompetitionEntry]:
        ts = time.time() if now is None else now
        out = [
            e for aid, e in self.catalog.items()
            if aid not in self.completed_ids
            and self.is_executable(aid)
            and self.deferred_until.get(aid, 0.0) <= ts
        ]
        out.sort(key=lambda e: (-e.points, e.difficulty, e.id))
        return out

    def snapshot(self) -> Dict[str, Any]:
        return {
            "active_task_id": self.active_task_id,
            "completed_count": len(self.completed_ids),
            "total_score": self.total_score,
            "eligible_count": len(self.catalog),
            "executable_count": sum(1 for aid in self.catalog if self.is_executable(aid)),
            "ended": self.ended,
            "end_reason": self.end_reason,
        }

    def _persist(self) -> None:
        if self.db is not None:
            self.db.save_checkpoint("competition", {
                "completed_ids": sorted(self.completed_ids),
                "active_task_id": self.active_task_id,
                "progress_note": self.progress_note,
                "deferred_until": self.deferred_until,
                "ended": self.ended,
                "end_reason": self.end_reason,
            })
