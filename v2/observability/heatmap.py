"""L4/Observability HeatmapTracker: passive spatial block & code execution hotspot tracker."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from v2.bt.blackboard import Blackboard
from v2.domain import Coord3, block_coord

_SPATIAL_ENDPOINTS = frozenset({"dig", "place", "move_to", "interact"})
_POS_TAG_RE = re.compile(r"@\([^)]+\)")


def normalize_fail_reason(fail: str) -> str:
    """Normalize volatile coordinates like '@(12, 34, -56)' in failure strings so identical causes aggregate."""
    if not fail:
        return ""
    return _POS_TAG_RE.sub("@(pos)", str(fail).strip())


def describe_task_chain(goal_obj: Any, *, max_depth: int = 6) -> str:
    """Walk Task._active_child links into a compact readable call-chain string without side effects."""
    parts: List[str] = []
    cur = goal_obj
    seen: set[int] = set()
    while cur is not None and len(parts) < max_depth:
        oid = id(cur)
        if oid in seen:
            break
        seen.add(oid)
        cls_name = type(cur).__name__
        t_name = str(getattr(cur, "name", "") or "")
        parts.append(f"{cls_name}({t_name})" if t_name else cls_name)
        cur = getattr(cur, "_active_child", None)
    return ">".join(parts)


def _top_k_counts(counts: Dict[str, int], limit: int) -> Dict[str, int]:
    if limit <= 0 or len(counts) <= limit:
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return dict(top)


class HeatmapTracker:
    """Passive, thread-safe per-block spatial heatmap + code-path hotspot aggregator."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        output_dir: Path | str,
        *,
        username: str = "",
        enabled: bool = True,
        flush_every_ticks: int = 60,
        max_code_hits_per_block: int = 8,
        max_blocks_per_code: int = 12,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.username = str(username or "")
        self.enabled = bool(enabled)
        self.flush_every_ticks = max(0, int(flush_every_ticks))
        self.max_code_hits_per_block = max(1, int(max_code_hits_per_block))
        self.max_blocks_per_code = max(1, int(max_blocks_per_code))

        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self.output_path: Optional[Path] = None
        if self.enabled:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            self.output_path = self.output_dir / f"v2_heatmap_{stamp}.json"

        now = round(time.time(), 3)
        self._started_ts: float = now
        self._updated_ts: float = now
        self._total_ticks: int = 0
        self._total_stuck_ticks: int = 0
        self._last_cell: Optional[Tuple[str, int, int, int]] = None
        self._last_dim: str = "overworld"
        self._last_pos: Optional[List[int]] = None
        self._current_code_key: str = ""

        self._blocks: Dict[str, Dict[str, Any]] = {}
        self._code_hotspots: Dict[str, Dict[str, Any]] = {}
        self._api_hotspots: Dict[str, Dict[str, Any]] = {}
        self._y_profile: Dict[str, Dict[str, int]] = {}

    @staticmethod
    def _block_key(dim: str, cell: Coord3) -> str:
        return f"{dim}:{int(cell[0])}:{int(cell[1])}:{int(cell[2])}"

    def _ensure_block_locked(self, dim: str, cell: Coord3) -> Tuple[str, Dict[str, Any]]:
        key = self._block_key(dim, cell)
        rec = self._blocks.get(key)
        if rec is None:
            rec = {
                "dim": dim,
                "pos": [int(cell[0]), int(cell[1]), int(cell[2])],
                "dwell": 0,
                "stuck": 0,
                "dig_ok": 0,
                "dig_fail": 0,
                "dig_ms": 0,
                "place_ok": 0,
                "place_fail": 0,
                "move_ok": 0,
                "move_fail": 0,
                "interact_ok": 0,
                "interact_fail": 0,
                "codes": {},
            }
            self._blocks[key] = rec
        return key, rec

    def _ensure_y_locked(self, y: int) -> Dict[str, int]:
        yk = str(int(y))
        rec = self._y_profile.get(yk)
        if rec is None:
            rec = {"dwell": 0, "stuck": 0, "dig": 0}
            self._y_profile[yk] = rec
        return rec

    def _ensure_code_locked(
        self, branch: str, task_chain: str, outcome: str, fail_norm: str
    ) -> Tuple[str, Dict[str, Any]]:
        code_key = f"{branch}|{task_chain}|{outcome}|{fail_norm}"
        rec = self._code_hotspots.get(code_key)
        if rec is None:
            rec = {
                "branch": branch,
                "task_chain": task_chain,
                "outcome": outcome,
                "fail": fail_norm,
                "ticks": 0,
                "stuck_ticks": 0,
                "total_ms": 0.0,
                "blocks": {},
            }
            self._code_hotspots[code_key] = rec
        return code_key, rec

    def record_tick(self, tick_idx: int, bb: Blackboard, goal_obj: Any, elapsed_ms: float) -> None:
        """Record dwell, stuck state, active BT/Task chain, and Y profile for one BT tick."""
        _ = tick_idx
        dim = bb.dimension
        cell = block_coord(bb.pos)
        branch = str(bb.active_branch or "idle")
        task_chain = describe_task_chain(goal_obj)
        outcome = str(bb.last_action_outcome or "")
        fail_norm = normalize_fail_reason(bb.last_failure_reason)

        should_flush = False
        with self._lock:
            is_stuck = self._last_cell is not None and self._last_cell == (dim, cell[0], cell[1], cell[2])
            self._last_cell = (dim, cell[0], cell[1], cell[2])
            self._last_dim = dim
            self._last_pos = [cell[0], cell[1], cell[2]]
            self._updated_ts = round(time.time(), 3)
            self._total_ticks += 1
            if is_stuck:
                self._total_stuck_ticks += 1

            bkey, brec = self._ensure_block_locked(dim, cell)
            brec["dwell"] += 1
            if is_stuck:
                brec["stuck"] += 1

            yrec = self._ensure_y_locked(cell[1])
            yrec["dwell"] += 1
            if is_stuck:
                yrec["stuck"] += 1

            ckey, crec = self._ensure_code_locked(branch, task_chain, outcome, fail_norm)
            self._current_code_key = ckey
            crec["ticks"] += 1
            if is_stuck:
                crec["stuck_ticks"] += 1
            crec["total_ms"] = round(float(crec["total_ms"]) + max(0.0, float(elapsed_ms)), 3)

            brec["codes"][ckey] = brec["codes"].get(ckey, 0) + 1
            crec["blocks"][bkey] = crec["blocks"].get(bkey, 0) + 1

            if self.flush_every_ticks > 0 and self._total_ticks % self.flush_every_ticks == 0:
                should_flush = True

        if should_flush and self.enabled:
            self.flush()

    def on_flight_event(self, entry: Dict[str, Any]) -> None:
        """Passive FlightRecorder listener attributing spatial API timings & errors to blocks."""
        if not isinstance(entry, dict):
            return
        etype = entry.get("event")
        if etype not in {"API_TIMING", "API_ERROR"}:
            return
        data = entry.get("data")
        if not isinstance(data, dict):
            return
        endpoint = str(data.get("endpoint") or "")
        if not endpoint:
            return

        with self._lock:
            self._updated_ts = round(time.time(), 3)
            api = self._api_hotspots.get(endpoint)
            if api is None:
                api = {"ok": 0, "err": 0, "total_ms": 0, "slow_ge6s": 0, "err_codes": {}}
                self._api_hotspots[endpoint] = api

            if etype == "API_TIMING":
                dur = int(data.get("duration_ms") or 0)
                ok = bool(data.get("ok"))
                api["total_ms"] += max(0, dur)
                if ok:
                    api["ok"] += 1
                else:
                    api["err"] += 1
                if dur >= 6000:
                    api["slow_ge6s"] += 1
                if endpoint in _SPATIAL_ENDPOINTS:
                    target = data.get("target")
                    if isinstance(target, (list, tuple)) and len(target) == 3:
                        tcell: Coord3 = (int(target[0]), int(target[1]), int(target[2]))
                        bkey, brec = self._ensure_block_locked(self._last_dim, tcell)
                        if endpoint == "dig":
                            brec["dig_ok" if ok else "dig_fail"] += 1
                            brec["dig_ms"] += max(0, dur)
                            self._ensure_y_locked(tcell[1])["dig"] += 1
                        elif endpoint == "place":
                            brec["place_ok" if ok else "place_fail"] += 1
                        elif endpoint == "move_to":
                            brec["move_ok" if ok else "move_fail"] += 1
                        elif endpoint == "interact":
                            brec["interact_ok" if ok else "interact_fail"] += 1

                        if self._current_code_key:
                            brec["codes"][self._current_code_key] = brec["codes"].get(self._current_code_key, 0) + 1
                            crec = self._code_hotspots.get(self._current_code_key)
                            if crec is not None:
                                crec["blocks"][bkey] = crec["blocks"].get(bkey, 0) + 1
            else:
                code = str(data.get("code") or "error")
                api["err_codes"][code] = api["err_codes"].get(code, 0) + 1

    def snapshot(self) -> Dict[str, Any]:
        """Return a trimmed JSON-serializable snapshot of all heatmaps."""
        with self._lock:
            blocks_out = {}
            for k, rec in self._blocks.items():
                copy_rec = dict(rec)
                copy_rec["codes"] = _top_k_counts(rec["codes"], self.max_code_hits_per_block)
                blocks_out[k] = copy_rec

            code_out = {}
            for k, rec in self._code_hotspots.items():
                copy_rec = dict(rec)
                copy_rec["blocks"] = _top_k_counts(rec["blocks"], self.max_blocks_per_code)
                code_out[k] = copy_rec

            api_out = {
                k: {
                    "ok": v["ok"],
                    "err": v["err"],
                    "total_ms": v["total_ms"],
                    "slow_ge6s": v["slow_ge6s"],
                    "err_codes": dict(sorted(v["err_codes"].items(), key=lambda kv: (-kv[1], kv[0]))),
                }
                for k, v in self._api_hotspots.items()
            }
            y_out = {k: dict(v) for k, v in sorted(self._y_profile.items(), key=lambda kv: int(kv[0]))}
            return {
                "schema_version": self.SCHEMA_VERSION,
                "username": self.username,
                "started_ts": self._started_ts,
                "updated_ts": self._updated_ts,
                "total_ticks": self._total_ticks,
                "total_stuck_ticks": self._total_stuck_ticks,
                "last_dim": self._last_dim,
                "last_pos": list(self._last_pos) if self._last_pos is not None else None,
                "blocks": blocks_out,
                "code_hotspots": code_out,
                "api_hotspots": api_out,
                "y_profile": y_out,
            }

    def flush(self) -> Optional[Path]:
        """Atomically persist the current heatmap snapshot to disk."""
        if not self.enabled or self.output_path is None:
            return None
        payload = self.snapshot()
        tmp_path = self.output_path.with_suffix(".json.tmp")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(encoded, encoding="utf-8")
            os.replace(tmp_path, self.output_path)
        return self.output_path
