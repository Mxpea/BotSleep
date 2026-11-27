"""Offline Spatial Block Heatmap + Code-Path Hotspot analyzer & interactive HTML visualizer.

Supports both native `v2_heatmap_*.json` outputs (per-tick + per-target-block resolution) and
historical `v2_flight_*.jsonl` reconstruction (e.g. R5 / R6b), plus `--compare <baseline_dir>`.
"""

from __future__ import annotations

import argparse
import html as _html
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

_POS_TAG_RE = re.compile(r"@\([^)]+\)")
_SPATIAL_ENDPOINTS = frozenset({"dig", "place", "move_to", "interact"})


def normalize_fail(fail: str) -> str:
    if not fail:
        return ""
    return _POS_TAG_RE.sub("@(pos)", str(fail).strip())


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _merge_counts(dst: Dict[str, int], src: Dict[str, int], limit: int = 16) -> None:
    for k, v in src.items():
        dst[k] = dst.get(k, 0) + _safe_int(v)
    if len(dst) > limit * 2:
        top = dict(sorted(dst.items(), key=lambda kv: (-kv[1], kv[0]))[:limit])
        dst.clear()
        dst.update(top)


def _trim_dict(d: Dict[str, int], limit: int) -> Dict[str, int]:
    return dict(sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:limit])


def _extract_llm_from_flights(flight_paths: List[Path]) -> Dict[str, int]:
    stats = {"llm_http": 0, "llm_mounted": 0, "llm_errors": 0, "tokens": 0}
    for fp in flight_paths:
        with fp.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                et = ev.get("event")
                d = ev.get("data") or {}
                if et == "LLM_HTTP":
                    stats["llm_http"] += 1
                    stats["tokens"] += _safe_int(d.get("prompt_tokens")) + _safe_int(d.get("completion_tokens"))
                elif et == "LLM_RESPONSE":
                    pr = d.get("plan_result") or {}
                    if pr.get("ok") and pr.get("mounted"):
                        stats["llm_mounted"] += 1
                elif et in {"LLM_ERROR", "MODEL_ERROR"}:
                    stats["llm_errors"] += 1
    return stats


def _reconstruct_from_flights(bot_name: str, flight_paths: List[Path]) -> Dict[str, Any]:
    blocks: Dict[str, Dict[str, Any]] = {}
    code_hotspots: Dict[str, Dict[str, Any]] = {}
    api_hotspots: Dict[str, Dict[str, Any]] = {}
    y_profile: Dict[str, Dict[str, int]] = {}
    total_ticks = 0
    total_stuck = 0
    last_cell: Optional[Tuple[str, int, int, int]] = None
    last_dim = "overworld"
    last_pos: Optional[List[int]] = None
    last_code_key = ""

    def ensure_blk(dim: str, pos: List[int]) -> Tuple[str, Dict[str, Any]]:
        k = f"{dim}:{pos[0]}:{pos[1]}:{pos[2]}"
        rec = blocks.get(k)
        if rec is None:
            rec = {
                "dim": dim, "pos": [pos[0], pos[1], pos[2]],
                "dwell": 0, "stuck": 0,
                "dig_ok": 0, "dig_fail": 0, "dig_ms": 0,
                "place_ok": 0, "place_fail": 0,
                "move_ok": 0, "move_fail": 0,
                "interact_ok": 0, "interact_fail": 0,
                "codes": {},
            }
            blocks[k] = rec
        return k, rec

    def ensure_y(y: int) -> Dict[str, int]:
        yk = str(y)
        rec = y_profile.get(yk)
        if rec is None:
            rec = {"dwell": 0, "stuck": 0, "dig": 0}
            y_profile[yk] = rec
        return rec

    for fp in flight_paths:
        with fp.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                et = ev.get("event")
                d = ev.get("data") or {}
                if et == "TICK_SUMMARY":
                    pos_raw = d.get("pos") or [0, 64, 0]
                    if not isinstance(pos_raw, list) or len(pos_raw) < 3:
                        continue
                    pos = [math.floor(_safe_float(pos_raw[0])), math.floor(_safe_float(pos_raw[1])), math.floor(_safe_float(pos_raw[2]))]
                    dim = str(d.get("dim") or "overworld")
                    cell = (dim, pos[0], pos[1], pos[2])
                    is_stuck = last_cell is not None and last_cell == cell
                    last_cell = cell
                    last_dim = dim
                    last_pos = pos
                    total_ticks += 1
                    if is_stuck:
                        total_stuck += 1

                    bkey, brec = ensure_blk(dim, pos)
                    brec["dwell"] += 1
                    if is_stuck:
                        brec["stuck"] += 1
                    yrec = ensure_y(pos[1])
                    yrec["dwell"] += 1
                    if is_stuck:
                        yrec["stuck"] += 1

                    branch = str(d.get("branch") or "idle")
                    goal = str(d.get("goal") or "")
                    out = str(d.get("out") or "")
                    fail = normalize_fail(str(d.get("fail") or ""))
                    ckey = f"{branch}|{goal}|{out}|{fail}"
                    last_code_key = ckey
                    crec = code_hotspots.get(ckey)
                    if crec is None:
                        crec = {
                            "branch": branch, "task_chain": goal, "outcome": out, "fail": fail,
                            "ticks": 0, "stuck_ticks": 0, "total_ms": 0.0, "blocks": {},
                        }
                        code_hotspots[ckey] = crec
                    crec["ticks"] += 1
                    if is_stuck:
                        crec["stuck_ticks"] += 1
                    brec["codes"][ckey] = brec["codes"].get(ckey, 0) + 1
                    crec["blocks"][bkey] = crec["blocks"].get(bkey, 0) + 1
                elif et in {"API_TIMING", "API_ERROR"}:
                    ep = str(d.get("endpoint") or "")
                    if not ep:
                        continue
                    api = api_hotspots.get(ep)
                    if api is None:
                        api = {"ok": 0, "err": 0, "total_ms": 0, "slow_ge6s": 0, "err_codes": {}}
                        api_hotspots[ep] = api
                    if et == "API_TIMING":
                        dur = _safe_int(d.get("duration_ms"))
                        ok = bool(d.get("ok"))
                        api["total_ms"] += max(0, dur)
                        api["ok" if ok else "err"] += 1
                        if dur >= 6000:
                            api["slow_ge6s"] += 1
                        target = d.get("target")
                        if ep in _SPATIAL_ENDPOINTS and isinstance(target, list) and len(target) == 3:
                            tpos = [_safe_int(target[0]), _safe_int(target[1]), _safe_int(target[2])]
                            bkey, brec = ensure_blk(last_dim, tpos)
                            if ep == "dig":
                                brec["dig_ok" if ok else "dig_fail"] += 1
                                brec["dig_ms"] += max(0, dur)
                                ensure_y(tpos[1])["dig"] += 1
                            elif ep == "place":
                                brec["place_ok" if ok else "place_fail"] += 1
                            elif ep == "move_to":
                                brec["move_ok" if ok else "move_fail"] += 1
                            elif ep == "interact":
                                brec["interact_ok" if ok else "interact_fail"] += 1
                            if last_code_key and last_code_key in code_hotspots:
                                brec["codes"][last_code_key] = brec["codes"].get(last_code_key, 0) + 1
                                code_hotspots[last_code_key]["blocks"][bkey] = code_hotspots[last_code_key]["blocks"].get(bkey, 0) + 1
                    else:
                        code = str(d.get("code") or "error")
                        api["err_codes"][code] = api["err_codes"].get(code, 0) + 1

    return {
        "schema_version": 1,
        "username": bot_name,
        "source": "flight_jsonl_fallback",
        "total_ticks": total_ticks,
        "total_stuck_ticks": total_stuck,
        "last_dim": last_dim,
        "last_pos": last_pos,
        "blocks": blocks,
        "code_hotspots": code_hotspots,
        "api_hotspots": api_hotspots,
        "y_profile": y_profile,
    }


def _discover_bot_dirs(run_dir: Path) -> List[Tuple[str, Path]]:
    subdirs = sorted(
        [p for p in run_dir.iterdir() if p.is_dir() and re.match(r"bot_(\d+)$", p.name)],
        key=lambda p: int(p.name.split("_")[1]),
    )
    if subdirs:
        return [(p.name, p) for p in subdirs]
    return [(run_dir.name, run_dir)]


def _merge_bot_snapshots(bot_id: str, bot_dir: Path) -> Optional[Dict[str, Any]]:
    hm_files = sorted(bot_dir.glob("v2_heatmap_*.json"))
    flight_files = sorted(bot_dir.glob("v2_flight_*.jsonl"))
    llm_stats = _extract_llm_from_flights(flight_files) if flight_files else {"llm_http": 0, "llm_mounted": 0, "llm_errors": 0, "tokens": 0}

    if hm_files:
        # Merge multiple sessions in same bot dir if restarted, usually just 1
        merged: Dict[str, Any] = {
            "bot_id": bot_id,
            "username": bot_id,
            "source": "heatmap_json",
            "total_ticks": 0,
            "total_stuck_ticks": 0,
            "blocks": {},
            "code_hotspots": {},
            "api_hotspots": {},
            "y_profile": {},
            "llm": llm_stats,
        }
        for hp in hm_files:
            data = json.loads(hp.read_text(encoding="utf-8", errors="replace"))
            if data.get("username"):
                merged["username"] = str(data["username"])
            merged["total_ticks"] += _safe_int(data.get("total_ticks"))
            merged["total_stuck_ticks"] += _safe_int(data.get("total_stuck_ticks"))
            for bkey, brec in (data.get("blocks") or {}).items():
                dst = merged["blocks"].get(bkey)
                if dst is None:
                    dst = {
                        "dim": brec.get("dim", "overworld"),
                        "pos": list(brec.get("pos") or [0, 64, 0]),
                        "dwell": 0, "stuck": 0,
                        "dig_ok": 0, "dig_fail": 0, "dig_ms": 0,
                        "place_ok": 0, "place_fail": 0,
                        "move_ok": 0, "move_fail": 0,
                        "interact_ok": 0, "interact_fail": 0,
                        "codes": {},
                    }
                    merged["blocks"][bkey] = dst
                for f in ("dwell", "stuck", "dig_ok", "dig_fail", "dig_ms", "place_ok", "place_fail", "move_ok", "move_fail", "interact_ok", "interact_fail"):
                    dst[f] += _safe_int(brec.get(f))
                _merge_counts(dst["codes"], brec.get("codes") or {}, limit=12)
            for ckey, crec in (data.get("code_hotspots") or {}).items():
                dst = merged["code_hotspots"].get(ckey)
                if dst is None:
                    dst = {
                        "branch": str(crec.get("branch") or ""),
                        "task_chain": str(crec.get("task_chain") or ""),
                        "outcome": str(crec.get("outcome") or ""),
                        "fail": str(crec.get("fail") or ""),
                        "ticks": 0, "stuck_ticks": 0, "total_ms": 0.0, "blocks": {},
                    }
                    merged["code_hotspots"][ckey] = dst
                dst["ticks"] += _safe_int(crec.get("ticks"))
                dst["stuck_ticks"] += _safe_int(crec.get("stuck_ticks"))
                dst["total_ms"] = round(dst["total_ms"] + _safe_float(crec.get("total_ms")), 3)
                _merge_counts(dst["blocks"], crec.get("blocks") or {}, limit=16)
            for ep, arec in (data.get("api_hotspots") or {}).items():
                dst = merged["api_hotspots"].get(ep)
                if dst is None:
                    dst = {"ok": 0, "err": 0, "total_ms": 0, "slow_ge6s": 0, "err_codes": {}}
                    merged["api_hotspots"][ep] = dst
                for f in ("ok", "err", "total_ms", "slow_ge6s"):
                    dst[f] += _safe_int(arec.get(f))
                _merge_counts(dst["err_codes"], arec.get("err_codes") or {}, limit=16)
            for yk, yrec in (data.get("y_profile") or {}).items():
                dst = merged["y_profile"].get(yk)
                if dst is None:
                    dst = {"dwell": 0, "stuck": 0, "dig": 0}
                    merged["y_profile"][yk] = dst
                for f in ("dwell", "stuck", "dig"):
                    dst[f] += _safe_int(yrec.get(f))
        return merged

    if flight_files:
        data = _reconstruct_from_flights(bot_id, flight_files)
        data["bot_id"] = bot_id
        data["llm"] = llm_stats
        return data
    return None


def load_run_heatmaps(run_dir: Path | str) -> Dict[str, Any]:
    """Load and aggregate per-bot and fleet-wide heatmaps from a run directory."""
    root = Path(run_dir)
    if not root.exists():
        raise FileNotFoundError(f"Run directory not found: {root}")

    bots: List[Dict[str, Any]] = []
    agg_code: Dict[str, Dict[str, Any]] = {}
    agg_api: Dict[str, Dict[str, Any]] = {}
    agg_y: Dict[str, Dict[str, int]] = {}
    total_ticks = 0
    total_stuck = 0
    total_llm_http = 0
    total_llm_mounted = 0
    total_tokens = 0
    sources: set[str] = set()

    for bot_id, bdir in _discover_bot_dirs(root):
        bot_data = _merge_bot_snapshots(bot_id, bdir)
        if bot_data is None:
            continue
        bots.append(bot_data)
        sources.add(str(bot_data.get("source") or "unknown"))
        total_ticks += bot_data["total_ticks"]
        total_stuck += bot_data["total_stuck_ticks"]
        total_llm_http += bot_data["llm"]["llm_http"]
        total_llm_mounted += bot_data["llm"]["llm_mounted"]
        total_tokens += bot_data["llm"]["tokens"]

        for ckey, crec in bot_data["code_hotspots"].items():
            dst = agg_code.get(ckey)
            if dst is None:
                dst = {
                    "key": ckey,
                    "branch": crec["branch"],
                    "task_chain": crec["task_chain"],
                    "outcome": crec["outcome"],
                    "fail": crec["fail"],
                    "ticks": 0,
                    "stuck_ticks": 0,
                    "total_ms": 0.0,
                    "bots": set(),
                    "blocks": {},
                }
                agg_code[ckey] = dst
            dst["ticks"] += crec["ticks"]
            dst["stuck_ticks"] += crec["stuck_ticks"]
            dst["total_ms"] = round(dst["total_ms"] + crec["total_ms"], 3)
            dst["bots"].add(bot_id)
            for bkey, cnt in (crec.get("blocks") or {}).items():
                tagged = f"{bot_id}|{bkey}"
                dst["blocks"][tagged] = dst["blocks"].get(tagged, 0) + _safe_int(cnt)

        for ep, arec in bot_data["api_hotspots"].items():
            dst = agg_api.get(ep)
            if dst is None:
                dst = {"ok": 0, "err": 0, "total_ms": 0, "slow_ge6s": 0, "err_codes": {}}
                agg_api[ep] = dst
            for f in ("ok", "err", "total_ms", "slow_ge6s"):
                dst[f] += arec[f]
            _merge_counts(dst["err_codes"], arec["err_codes"], limit=20)

        for yk, yrec in bot_data["y_profile"].items():
            dst = agg_y.get(yk)
            if dst is None:
                dst = {"dwell": 0, "stuck": 0, "dig": 0}
                agg_y[yk] = dst
            for f in ("dwell", "stuck", "dig"):
                dst[f] += yrec[f]

    for rec in agg_code.values():
        rec["bots"] = sorted(rec["bots"])
        rec["blocks"] = _trim_dict(rec["blocks"], 18)

    dig_api = agg_api.get("dig") or {}
    place_api = agg_api.get("place") or {}
    place_no_sup = (place_api.get("err_codes") or {}).get("no_support", 0)

    return {
        "run_name": root.name,
        "run_dir": str(root),
        "sources": sorted(sources),
        "bot_count": len(bots),
        "total_ticks": total_ticks,
        "total_stuck_ticks": total_stuck,
        "stuck_pct": round((100.0 * total_stuck / total_ticks), 2) if total_ticks else 0.0,
        "llm_http": total_llm_http,
        "llm_mounted": total_llm_mounted,
        "tokens": total_tokens,
        "dig_total": _safe_int(dig_api.get("ok")) + _safe_int(dig_api.get("err")),
        "dig_ge6s": _safe_int(dig_api.get("slow_ge6s")),
        "place_no_support": _safe_int(place_no_sup),
        "bots": bots,
        "code_hotspots": agg_code,
        "api_hotspots": agg_api,
        "y_profile": dict(sorted(agg_y.items(), key=lambda kv: int(kv[0]))),
    }


def diff_runs(current: Dict[str, Any], baseline: Dict[str, Any], *, top_n: int = 20) -> Dict[str, Any]:
    """Compare current run hotspots vs a baseline run."""
    cur_codes = current["code_hotspots"]
    base_codes = baseline["code_hotspots"]
    cur_total = max(1, current["total_ticks"])
    base_total = max(1, baseline["total_ticks"])

    all_keys = set(cur_codes.keys()) | set(base_codes.keys())
    code_diffs = []
    for k in all_keys:
        c = cur_codes.get(k)
        b = base_codes.get(k)
        c_ticks = c["ticks"] if c else 0
        b_ticks = b["ticks"] if b else 0
        c_stuck = c["stuck_ticks"] if c else 0
        b_stuck = b["stuck_ticks"] if b else 0
        c_pct = round(100.0 * c_ticks / cur_total, 2)
        b_pct = round(100.0 * b_ticks / base_total, 2)
        ref = c or b
        code_diffs.append({
            "key": k,
            "branch": ref["branch"],
            "task_chain": ref["task_chain"],
            "outcome": ref["outcome"],
            "fail": ref["fail"],
            "cur_ticks": c_ticks,
            "base_ticks": b_ticks,
            "delta_ticks": c_ticks - b_ticks,
            "cur_stuck": c_stuck,
            "base_stuck": b_stuck,
            "delta_stuck": c_stuck - b_stuck,
            "cur_pct": c_pct,
            "base_pct": b_pct,
            "delta_pct": round(c_pct - b_pct, 2),
        })
    code_diffs.sort(key=lambda r: (-(r["cur_stuck"] + abs(r["delta_stuck"])), -r["cur_ticks"], r["key"]))

    return {
        "baseline_name": baseline["run_name"],
        "summary_deltas": {
            "total_ticks": current["total_ticks"] - baseline["total_ticks"],
            "stuck_pct": round(current["stuck_pct"] - baseline["stuck_pct"], 2),
            "dig_ge6s": current["dig_ge6s"] - baseline["dig_ge6s"],
            "place_no_support": current["place_no_support"] - baseline["place_no_support"],
            "llm_http": current["llm_http"] - baseline["llm_http"],
        },
        "code_diffs": code_diffs[:top_n],
    }


def _top_stuck_blocks(run: Dict[str, Any], top_n: int = 20) -> List[Dict[str, Any]]:
    rows = []
    for bot in run["bots"]:
        bid = bot["bot_id"]
        for bkey, brec in bot["blocks"].items():
            score = brec["stuck"] * 3 + brec["dwell"] + (brec["dig_ok"] + brec["dig_fail"]) * 2 + (brec["place_ok"] + brec["place_fail"])
            if score <= 0:
                continue
            rows.append({
                "bot_id": bid,
                "username": bot["username"],
                "block_key": bkey,
                "dim": brec["dim"],
                "pos": brec["pos"],
                "dwell": brec["dwell"],
                "stuck": brec["stuck"],
                "dig": brec["dig_ok"] + brec["dig_fail"],
                "dig_fail": brec["dig_fail"],
                "place": brec["place_ok"] + brec["place_fail"],
                "top_codes": _trim_dict(brec["codes"], 5),
                "heat": score,
            })
    rows.sort(key=lambda r: (-r["stuck"], -r["heat"], r["bot_id"], r["block_key"]))
    return rows[:top_n]


def render_markdown(run: Dict[str, Any], *, baseline: Optional[Dict[str, Any]] = None, diff: Optional[Dict[str, Any]] = None, top_n: int = 20) -> str:
    lines: List[str] = []
    lines.append(f"# 方块空间热度图 & 代码热度报告：`{run['run_name']}`")
    lines.append("")
    lines.append(f"- 数据源：`{', '.join(run['sources'])}`（Bot 数：`{run['bot_count']}`，总采样帧/Ticks：`{run['total_ticks']}`，原地停滞：`{run['total_stuck_ticks']}` / **`{run['stuck_pct']}%`**）")
    lines.append(f"- 行为与开销：`dig={run['dig_total']}`（`>=6s` 慢挖 `{run['dig_ge6s']}`）、`place:no_support={run['place_no_support']}`、LLM `{run['llm_http']}` 次（有效挂载 `{run['llm_mounted']}`，Tokens `{run['tokens']:,}`）")

    if baseline is not None and diff is not None:
        sd = diff["summary_deltas"]
        lines.append("")
        lines.append(f"## 一、 对照基线 `{baseline['run_name']}` 总体变化")
        lines.append("")
        lines.append("| 指标 | 本轮 | 基线 | 变化 |")
        lines.append("|---|---:|---:|---:|")
        lines.append(f"| 原地停滞率 (`stuck%`) | `{run['stuck_pct']}%` (`{run['total_stuck_ticks']}/{run['total_ticks']}`) | `{baseline['stuck_pct']}%` (`{baseline['total_stuck_ticks']}/{baseline['total_ticks']}`) | **`{sd['stuck_pct']:+.2f} pp`** |")
        lines.append(f"| `>=6s` 慢挖次数 | `{run['dig_ge6s']}` | `{baseline['dig_ge6s']}` | **`{sd['dig_ge6s']:+d}`** |")
        lines.append(f"| `place:no_support` | `{run['place_no_support']}` | `{baseline['place_no_support']}` | **`{sd['place_no_support']:+d}`** |")
        lines.append(f"| LLM 调用 / 挂载 | `{run['llm_http']} / {run['llm_mounted']}` | `{baseline['llm_http']} / {baseline['llm_mounted']}` | `{sd['llm_http']:+d}` |")

    lines.append("")
    lines.append("## 二、 逐 Bot 空间停滞密度")
    lines.append("")
    lines.append("| Bot | 数据源 | Ticks | 原地停滞 | 停滞率 | LLM (调/挂) | 独立方块数 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    for b in run["bots"]:
        pct = round(100.0 * b["total_stuck_ticks"] / max(1, b["total_ticks"]), 1)
        lines.append(
            f"| `{b['bot_id']}` | `{b['source']}` | {b['total_ticks']} | {b['total_stuck_ticks']} | `{pct}%` | {b['llm']['llm_http']} / {b['llm']['llm_mounted']} | {len(b['blocks'])} |"
        )

    lines.append("")
    lines.append(f"## 三、 Top {top_n} 代码执行 / 卡死热点（按原地停滞排序）")
    lines.append("")
    sorted_codes = sorted(run["code_hotspots"].values(), key=lambda c: (-c["stuck_ticks"], -c["ticks"], c["key"]))[:top_n]
    base_codes = baseline["code_hotspots"] if baseline else {}
    header = "| # | 分支 (`branch`) | 任务链 (`task_chain`) | 结果 (`outcome`) | 失败归因 (`fail`) | Stuck | Ticks | 涉及 Bot |"
    sep = "|---:|---|---|---|---|---:|---:|---|"
    if baseline is not None:
        header += " 较基线 Stuck 变化 |"
        sep += "---:|"
    lines.append(header)
    lines.append(sep)
    for idx, c in enumerate(sorted_codes, 1):
        row = f"| {idx} | `{c['branch']}` | `{c['task_chain'] or '-'}` | `{c['outcome'] or '-'}` | `{c['fail'] or '-'}` | **{c['stuck_ticks']}** | {c['ticks']} | `{','.join(c['bots'])}` |"
        if baseline is not None:
            bc = base_codes.get(c["key"])
            d_stuck = c["stuck_ticks"] - (bc["stuck_ticks"] if bc else 0)
            row += f" `{d_stuck:+d}` |"
        lines.append(row)

    lines.append("")
    lines.append(f"## 四、 Top {top_n} 方块空间热点（卡死 / 高频操作定位）")
    lines.append("")
    top_blks = _top_stuck_blocks(run, top_n=top_n)
    lines.append("| # | Bot | 坐标 `(dim, x, y, z)` | Dwell | Stuck | Dig | 关联最高频代码路径 |")
    lines.append("|---:|---|---|---:|---:|---:|---|")
    for idx, row in enumerate(top_blks, 1):
        p = row["pos"]
        top_c = next(iter(row["top_codes"].items()), ("-", 0))
        lines.append(
            f"| {idx} | `{row['bot_id']}` | `{row['dim']} ({p[0]}, {p[1]}, {p[2]})` | {row['dwell']} | **{row['stuck']}** | {row['dig']} | `{top_c[0]}` ({top_c[1]}x) |"
        )
    lines.append("")
    return "\n".join(lines)


def _compact_run_for_html(run: Dict[str, Any], max_blocks_per_bot: int = 1200, max_codes: int = 60) -> Dict[str, Any]:
    bots_payload = []
    for b in run["bots"]:
        # Keep top blocks by heat so HTML stays responsive even for huge runs
        blks = []
        for bkey, rec in b["blocks"].items():
            dig = rec["dig_ok"] + rec["dig_fail"]
            place = rec["place_ok"] + rec["place_fail"]
            heat = rec["stuck"] * 3 + rec["dwell"] + dig * 2 + place
            blks.append({
                "k": bkey,
                "d": rec["dim"],
                "p": rec["pos"],
                "dw": rec["dwell"],
                "st": rec["stuck"],
                "dg": dig,
                "df": rec["dig_fail"],
                "pl": place,
                "pf": rec["place_fail"],
                "mv": rec["move_ok"] + rec["move_fail"],
                "h": heat,
                "c": _trim_dict(rec["codes"], 6),
            })
        blks.sort(key=lambda x: (-x["st"], -x["h"], x["k"]))
        if len(blks) > max_blocks_per_bot:
            blks = blks[:max_blocks_per_bot]
        bots_payload.append({
            "id": b["bot_id"],
            "name": b["username"],
            "src": b["source"],
            "ticks": b["total_ticks"],
            "stuck": b["total_stuck_ticks"],
            "llm": b["llm"],
            "y": b["y_profile"],
            "blocks": blks,
        })

    codes_sorted = sorted(run["code_hotspots"].values(), key=lambda c: (-c["stuck_ticks"], -c["ticks"], c["key"]))[:max_codes]
    codes_payload = [
        {
            "key": c["key"],
            "br": c["branch"],
            "tc": c["task_chain"],
            "out": c["outcome"],
            "fail": c["fail"],
            "ticks": c["ticks"],
            "stuck": c["stuck_ticks"],
            "ms": c["total_ms"],
            "bots": c["bots"],
            "blocks": c["blocks"],
        }
        for c in codes_sorted
    ]
    return {
        "run_name": run["run_name"],
        "sources": run["sources"],
        "total_ticks": run["total_ticks"],
        "total_stuck_ticks": run["total_stuck_ticks"],
        "stuck_pct": run["stuck_pct"],
        "dig_total": run["dig_total"],
        "dig_ge6s": run["dig_ge6s"],
        "place_no_support": run["place_no_support"],
        "llm_http": run["llm_http"],
        "llm_mounted": run["llm_mounted"],
        "tokens": run["tokens"],
        "bots": bots_payload,
        "codes": codes_payload,
        "y_profile": run["y_profile"],
    }


def render_html(run: Dict[str, Any], *, baseline: Optional[Dict[str, Any]] = None, diff: Optional[Dict[str, Any]] = None) -> str:
    payload = {
        "current": _compact_run_for_html(run),
        "baseline": _compact_run_for_html(baseline) if baseline is not None else None,
        "diff": diff,
    }
    json_blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    title = _html.escape(f"BotSleep Spatial & Code Heatmap - {run['run_name']}")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>{title}</title>
<style>
  :root {{
    --bg: #0f141c; --panel: #171e29; --panel2: #1e2736; --border: #2b374b;
    --text: #e6edf5; --muted: #8fa3bf; --accent: #38bdf8; --hot: #ef4444; --warn: #f59e0b; --ok: #22c55e;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); }}
  header {{ padding: 14px 20px; border-bottom: 1px solid var(--border); background: #121822; display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between; }}
  h1 {{ margin: 0; font-size: 18px; }}
  .sub {{ font-size: 12px; color: var(--muted); }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(175px, 1fr)); gap: 10px; padding: 14px 20px; }}
  .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }}
  .card .k {{ font-size: 11px; color: var(--muted); text-transform: uppercase; }}
  .card .v {{ font-size: 20px; font-weight: 700; margin-top: 4px; }}
  .card .d {{ font-size: 12px; margin-top: 3px; color: var(--accent); }}
  .controls {{ display: flex; flex-wrap: wrap; gap: 10px; padding: 0 20px 12px; align-items: center; }}
  select, button {{ background: var(--panel2); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; font-size: 13px; cursor: pointer; }}
  button.active {{ border-color: var(--accent); background: #1b3652; }}
  .grid {{ display: grid; grid-template-columns: 1.55fr 1fr; gap: 14px; padding: 0 20px 20px; }}
  @media (max-width: 1100px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  .box {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 12px; display: flex; flex-direction: column; min-height: 460px; }}
  .box h2 {{ margin: 0 0 8px; font-size: 14px; color: var(--accent); display: flex; justify-content: space-between; align-items: center; }}
  #hmCanvas {{ width: 100%; height: 480px; background: #0a0e14; border-radius: 6px; border: 1px solid var(--border); cursor: crosshair; }}
  .inspector {{ margin-top: 8px; padding: 8px 10px; background: var(--panel2); border-radius: 6px; font-size: 12px; line-height: 1.5; max-height: 170px; overflow-y: auto; }}
  .table-wrap {{ overflow: auto; max-height: 640px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th, td {{ padding: 6px 8px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: top; }}
  th {{ position: sticky; top: 0; background: var(--panel2); color: var(--muted); }}
  tr.clickable:hover {{ background: #222f42; cursor: pointer; }}
  tr.selected {{ background: #1d3c5c !important; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; color: #93c5fd; word-break: break-all; }}
  .pill {{ display: inline-block; padding: 1px 6px; border-radius: 4px; background: #263348; font-size: 11px; }}
  .y-bars {{ display: flex; align-items: flex-end; gap: 2px; height: 90px; padding: 6px 0; overflow-x: auto; }}
  .y-bar {{ min-width: 6px; background: var(--accent); opacity: 0.85; border-radius: 2px 2px 0 0; }}
</style>
</head>
<body>
<header>
  <div>
    <h1>BotSleep 方块空间热度 &amp; 代码执行热点图</h1>
    <div class="sub" id="runMeta"></div>
  </div>
  <div class="sub">滚轮缩放 / 拖拽平移 / 点击方块看代码路径 / 点击右侧代码行反查方块</div>
</header>

<div class="cards" id="summaryCards"></div>

<div class="controls">
  <label>Bot: <select id="botSelect"></select></label>
  <label>投影视角:
    <select id="projSelect">
      <option value="xz">XZ 俯视 (地表 / 井口分布)</option>
      <option value="xy">XY 纵剖面 (矿道台阶 / 竖井)</option>
      <option value="zy">ZY 侧剖面 (地下深度切片)</option>
    </select>
  </label>
  <label>热度指标:
    <select id="metricSelect">
      <option value="heat">综合热度 (stuck*3 + dwell + dig*2 + place)</option>
      <option value="stuck">卡死停留 (stuck)</option>
      <option value="dwell">经过 dwell</option>
      <option value="dig">挖掘 dig</option>
      <option value="place">放置 place</option>
    </select>
  </label>
  <button id="btnFit" type="button">自动居中适配 (Fit)</button>
  <button id="btnClearCode" type="button">清除代码过滤</button>
</div>

<div class="grid">
  <div class="box">
    <h2><span>空间方块热力画布</span><span class="sub" id="canvasStats"></span></h2>
    <canvas id="hmCanvas" width="900" height="520"></canvas>
    <div class="inspector" id="inspector">悬停或点击画布上的方块以查看坐标 `(dim, x, y, z)` 与对应触发代码路径。</div>
    <h2 style="margin-top:10px;"><span>Y 轴深度分布 (-64 .. 120)</span><span class="sub">蓝=停留 红=卡死</span></h2>
    <div class="y-bars" id="yBars"></div>
  </div>

  <div class="box">
    <h2><span>代码执行 &amp; 卡死热点表</span><span class="sub">点击行高亮对应方块</span></h2>
    <div class="table-wrap">
      <table id="codeTable">
        <thead><tr><th>#</th><th>Branch / Task Chain / Out / Fail</th><th>Stuck</th><th>Ticks</th><th id="deltaHdr" style="display:none;">较基线</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </div>
</div>

<script>
const DATA = {json_blob};
const cur = DATA.current;
const base = DATA.baseline;
const diff = DATA.diff;

document.getElementById('runMeta').textContent =
  `Run: ${{cur.run_name}} | Sources: ${{cur.sources.join(', ')}}` +
  (base ? ` | Baseline: ${{base.run_name}}` : '');

function addCard(k, v, d) {{
  const div = document.createElement('div');
  div.className = 'card';
  div.innerHTML = `<div class="k">${{k}}</div><div class="v">${{v}}</div>` + (d ? `<div class="d">${{d}}</div>` : '');
  document.getElementById('summaryCards').appendChild(div);
}}
addCard('Total Ticks', cur.total_ticks, base ? `Baseline: ${{base.total_ticks}}` : '');
addCard('Stuck Ticks (%)', `${{cur.total_stuck_ticks}} (${{cur.stuck_pct}}%)`, diff ? `Delta: ${{diff.summary_deltas.stuck_pct >= 0 ? '+' : ''}}${{diff.summary_deltas.stuck_pct}} pp` : '');
addCard('Dig >= 6s Slow', cur.dig_ge6s, diff ? `Delta: ${{diff.summary_deltas.dig_ge6s >= 0 ? '+' : ''}}${{diff.summary_deltas.dig_ge6s}}` : `Total Dig: ${{cur.dig_total}}`);
addCard('place:no_support', cur.place_no_support, diff ? `Delta: ${{diff.summary_deltas.place_no_support >= 0 ? '+' : ''}}${{diff.summary_deltas.place_no_support}}` : '');
addCard('LLM (Call / Mounted)', `${{cur.llm_http}} / ${{cur.llm_mounted}}`, `Tokens: ${{cur.tokens.toLocaleString()}}`);

const botSel = document.getElementById('botSelect');
const optAll = document.createElement('option');
optAll.value = '__ALL__';
optAll.textContent = `全部 Bot (${{cur.bots.length}} 台)`;
botSel.appendChild(optAll);
cur.bots.forEach(b => {{
  const o = document.createElement('option');
  o.value = b.id;
  const pct = b.ticks ? ((100 * b.stuck) / b.ticks).toFixed(1) : '0.0';
  o.textContent = `${{b.id}} (ticks=${{b.ticks}}, stuck=${{b.stuck}} / ${{pct}}%)`;
  botSel.appendChild(o);
}});
if (cur.bots.length === 1) botSel.value = cur.bots[0].id;

let selectedCodeKey = null;
let selectedBlock = null;
let cells2D = [];
let view = {{ cx: 0, cy: 0, scale: 12 }};
let dragging = false, dragStart = null;

function heatColor(ratio) {{
  const r = Math.max(0, Math.min(1, ratio));
  if (r < 0.25) return `rgb(${{Math.round(30 + r*4*25)}}, ${{Math.round(90 + r*4*90)}}, 220)`;
  if (r < 0.55) return `rgb(${{Math.round(40 + (r-0.25)/0.3*210)}}, 210, ${{Math.round(200 - (r-0.25)/0.3*150)}})`;
  return `rgb(239, ${{Math.round(190 - (r-0.55)/0.45*145)}}, 48)`;
}}

function metricVal(b, m) {{
  if (m === 'stuck') return b.st;
  if (m === 'dwell') return b.dw;
  if (m === 'dig') return b.dg;
  if (m === 'place') return b.pl;
  return b.h;
}}

function getActiveBlocks() {{
  const bid = botSel.value;
  const list = [];
  for (const bot of cur.bots) {{
    if (bid !== '__ALL__' && bot.id !== bid) continue;
    for (const blk of bot.blocks) {{
      if (selectedCodeKey && (!blk.c || !blk.c[selectedCodeKey])) continue;
      list.push({{ bot: bot.id, blk }});
    }}
  }}
  return list;
}}

function rebuildProjection(autoFit) {{
  const proj = document.getElementById('projSelect').value;
  const metric = document.getElementById('metricSelect').value;
  const active = getActiveBlocks();
  const map = new Map();
  let maxVal = 1;
  for (const {{ bot, blk }} of active) {{
    const p = blk.p;
    const u = (proj === 'zy') ? p[2] : p[0];
    const v = (proj === 'xz') ? p[2] : -p[1];
    const k = `${{u}},${{v}}`;
    const val = metricVal(blk, metric);
    let cell = map.get(k);
    if (!cell) {{
      cell = {{ u, v, val: 0, st: 0, dw: 0, dg: 0, pl: 0, items: [] }};
      map.set(k, cell);
    }}
    cell.val += val;
    cell.st += blk.st;
    cell.dw += blk.dw;
    cell.dg += blk.dg;
    cell.pl += blk.pl;
    cell.items.push({{ bot, blk }});
    if (cell.val > maxVal) maxVal = cell.val;
  }}
  cells2D = Array.from(map.values());
  cells2D.sort((a, b) => a.val - b.val);
  document.getElementById('canvasStats').textContent = `单元=${{cells2D.length}} | Max=${{maxVal}}` + (selectedCodeKey ? ' | 已按代码过滤' : '');

  if (autoFit && cells2D.length > 0) {{
    let minU = Infinity, maxU = -Infinity, minV = Infinity, maxV = -Infinity;
    for (const c of cells2D) {{
      if (c.u < minU) minU = c.u; if (c.u > maxU) maxU = c.u;
      if (c.v < minV) minV = c.v; if (c.v > maxV) maxV = c.v;
    }}
    const canvas = document.getElementById('hmCanvas');
    view.cx = (minU + maxU) / 2;
    view.cy = (minV + maxV) / 2;
    const spanU = Math.max(12, maxU - minU + 4);
    const spanV = Math.max(12, maxV - minV + 4);
    view.scale = Math.max(2, Math.min(36, Math.min(canvas.width / spanU, canvas.height / spanV) * 0.88));
  }}
  drawCanvas(maxVal);
  renderYBars();
}}

function drawCanvas(maxVal) {{
  const canvas = document.getElementById('hmCanvas');
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!cells2D.length) {{
    ctx.fillStyle = '#8fa3bf';
    ctx.font = '14px sans-serif';
    ctx.fillText('当前筛选无空间方块记录', 24, 40);
    return;
  }}
  let mv = maxVal || 1;
  if (!maxVal) for (const c of cells2D) if (c.val > mv) mv = c.val;
  const s = view.scale;
  const pxSize = Math.max(3, Math.ceil(s * 0.92));
  for (const c of cells2D) {{
    const sx = (c.u - view.cx) * s + canvas.width / 2;
    const sy = (c.v - view.cy) * s + canvas.height / 2;
    if (sx < -pxSize || sy < -pxSize || sx > canvas.width + pxSize || sy > canvas.height + pxSize) continue;
    const ratio = Math.log1p(c.val) / Math.log1p(mv);
    ctx.fillStyle = heatColor(ratio);
    ctx.fillRect(sx - pxSize / 2, sy - pxSize / 2, pxSize, pxSize);
    if (c.st > 0 && s >= 6) {{
      ctx.strokeStyle = '#ffffffaa';
      ctx.lineWidth = 1;
      ctx.strokeRect(sx - pxSize / 2, sy - pxSize / 2, pxSize, pxSize);
    }}
  }}
}}

function renderYBars() {{
  const bid = botSel.value;
  let yMap = cur.y_profile;
  if (bid !== '__ALL__') {{
    const b = cur.bots.find(x => x.id === bid);
    yMap = b ? b.y : {{}};
  }}
  const el = document.getElementById('yBars');
  el.innerHTML = '';
  const ys = Object.keys(yMap).map(Number).sort((a, b) => a - b);
  let maxD = 1;
  for (const y of ys) {{
    const d = (yMap[String(y)].dwell || 0) + (yMap[String(y)].dig || 0);
    if (d > maxD) maxD = d;
  }}
  for (const y of ys) {{
    const rec = yMap[String(y)];
    const tot = (rec.dwell || 0) + (rec.dig || 0);
    const bar = document.createElement('div');
    bar.className = 'y-bar';
    bar.style.height = Math.max(4, Math.round((tot / maxD) * 78)) + 'px';
    if ((rec.stuck || 0) > 0 && rec.stuck >= tot * 0.4) bar.style.background = '#ef4444';
    bar.title = `Y=${{y}} | dwell=${{rec.dwell||0}}, stuck=${{rec.stuck||0}}, dig=${{rec.dig||0}}`;
    el.appendChild(bar);
  }}
}}

function renderCodeTable() {{
  const tbody = document.querySelector('#codeTable tbody');
  tbody.innerHTML = '';
  const diffMap = new Map();
  if (diff && diff.code_diffs) {{
    document.getElementById('deltaHdr').style.display = '';
    for (const d of diff.code_diffs) diffMap.set(d.key, d);
  }}
  cur.codes.forEach((c, i) => {{
    const tr = document.createElement('tr');
    tr.className = 'clickable' + (selectedCodeKey === c.key ? ' selected' : '');
    const d = diffMap.get(c.key);
    const deltaCell = diff ? `<td>${{d ? ((d.delta_stuck >= 0 ? '+' : '') + d.delta_stuck) : '-'}}</td>` : '';
    tr.innerHTML = `<td>${{i + 1}}</td>
      <td><code>${{c.br}}</code> / <code>${{c.tc || '-'}}</code><br/><span class="pill">${{c.out || '-'}}</span> <code>${{c.fail || ''}}</code></td>
      <td><b>${{c.stuck}}</b></td><td>${{c.ticks}}</td>${{deltaCell}}`;
    tr.onclick = () => {{
      selectedCodeKey = (selectedCodeKey === c.key) ? null : c.key;
      renderCodeTable();
      rebuildProjection(true);
    }};
    tbody.appendChild(tr);
  }});
}}

const canvas = document.getElementById('hmCanvas');
canvas.addEventListener('wheel', e => {{
  e.preventDefault();
  const factor = e.deltaY < 0 ? 1.18 : 0.85;
  view.scale = Math.max(1.5, Math.min(48, view.scale * factor));
  drawCanvas();
}});
canvas.addEventListener('mousedown', e => {{ dragging = true; dragStart = {{ x: e.clientX, y: e.clientY, cx: view.cx, cy: view.cy }}; }});
window.addEventListener('mouseup', () => {{ dragging = false; }});
canvas.addEventListener('mousemove', e => {{
  if (dragging && dragStart) {{
    view.cx = dragStart.cx - (e.clientX - dragStart.x) / view.scale;
    view.cy = dragStart.cy - (e.clientY - dragStart.y) / view.scale;
    drawCanvas();
  }}
}});
canvas.addEventListener('click', e => {{
  const rect = canvas.getBoundingClientRect();
  const mx = (e.clientX - rect.left) * (canvas.width / rect.width);
  const my = (e.clientY - rect.top) * (canvas.height / rect.height);
  let best = null, bestDist = Infinity;
  for (const c of cells2D) {{
    const sx = (c.u - view.cx) * view.scale + canvas.width / 2;
    const sy = (c.v - view.cy) * view.scale + canvas.height / 2;
    const dist = Math.hypot(sx - mx, sy - my);
    if (dist < Math.max(10, view.scale * 0.8) && dist < bestDist) {{ best = c; bestDist = dist; }}
  }}
  if (!best) return;
  const items = best.items.slice(0, 6).map(({{ bot, blk }}) => {{
    const topCodes = Object.entries(blk.c || {{}}).slice(0, 4).map(([ck, cnt]) => `<li><code>${{ck}}</code> (${{cnt}}x)</li>`).join('');
    return `<div><b>${{bot}}</b> @ <code>${{blk.d}} (${{blk.p.join(', ')}})</code> | dwell=${{blk.dw}} stuck=<b>${{blk.st}}</b> dig=${{blk.dg}} place=${{blk.pl}}<ul>${{topCodes}}</ul></div>`;
  }}).join('');
  document.getElementById('inspector').innerHTML = items;
}});

botSel.onchange = () => rebuildProjection(true);
document.getElementById('projSelect').onchange = () => rebuildProjection(true);
document.getElementById('metricSelect').onchange = () => rebuildProjection(false);
document.getElementById('btnFit').onclick = () => rebuildProjection(true);
document.getElementById('btnClearCode').onclick = () => {{ selectedCodeKey = null; renderCodeTable(); rebuildProjection(true); }};

renderCodeTable();
rebuildProjection(true);
</script>
</body>
</html>"""


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze BotSleep v2 block spatial & code execution heatmaps.")
    parser.add_argument("run_dir", help="Run log directory (e.g. logs/v2_live9_r6b_90m or single bot output dir)")
    parser.add_argument("--compare", default="", help="Optional baseline run directory to diff against")
    parser.add_argument("--html-out", default="", help="Optional path to write standalone interactive HTML visualizer")
    parser.add_argument("--md-out", default="", help="Optional path to write Markdown summary (also prints if empty)")
    parser.add_argument("--top", type=int, default=20, help="Top N hotspots to include in tables")
    args = parser.parse_args(argv)

    run = load_run_heatmaps(args.run_dir)
    baseline = load_run_heatmaps(args.compare) if args.compare else None
    diff = diff_runs(run, baseline, top_n=args.top) if baseline is not None else None

    md = render_markdown(run, baseline=baseline, diff=diff, top_n=args.top)
    if args.md_out:
        md_path = Path(args.md_out)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md, encoding="utf-8")
    else:
        print(md)

    if args.html_out:
        html_path = Path(args.html_out)
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(render_html(run, baseline=baseline, diff=diff), encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
