"""Unified Cross-Reading Debug Workbench: merges MC server ground truth, BT state transitions,
LLM I/O / discards, API errors, and spatial/code heatmaps on millisecond UTC timestamps (`ts_ms`).
Automatically classifies anomalies into SYSTEMIC vs LOCAL_CLUSTER vs ONE_OFF.
"""

from __future__ import annotations

import argparse
import collections
from datetime import datetime, timezone
import html as _html
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.analyze_heatmap import diff_runs, load_run_heatmaps, normalize_fail  # noqa: E402

DEFAULT_MC_PLUGINS_RUNS = (
    REPO_ROOT.parent / "minecraft-server" / "plugins" / "BotSleepDebug" / "runs"
)

_ROCK_BLOCK_SUBSTRINGS = (
    "stone", "cobblestone", "deepslate", "andesite", "diorite", "granite",
    "tuff", "netherrack", "obsidian", "_ore", "blackstone", "basalt",
)


def _fmt_ts_ms(ts_ms: int) -> str:
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).astimezone()
        return dt.strftime("%H:%M:%S.") + f"{ts_ms % 1000:03d}"
    except Exception:
        return str(ts_ms)


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _is_bare_hand_rock(block_name: str, hand_name: str) -> bool:
    blk = str(block_name or "").lower()
    hand = str(hand_name or "").lower()
    if "pickaxe" in hand:
        return False
    return any(tok in blk for tok in _ROCK_BLOCK_SUBSTRINGS)


def load_server_events(server_log_path: Optional[Path]) -> Dict[str, Any]:
    """Parse MC server-side BotSleepDebug `server_events.jsonl` into per-player ground truth."""
    out: Dict[str, Any] = {
        "available": False,
        "path": str(server_log_path) if server_log_path else "",
        "players": {},
        "event_count": 0,
    }
    if server_log_path is None or not server_log_path.is_file():
        return out

    out["available"] = True
    players: Dict[str, Dict[str, Any]] = {}

    def ensure_p(name: str) -> Dict[str, Any]:
        rec = players.get(name)
        if rec is None:
            rec = {
                "player": name,
                "timeline": [],
                "breaks": 0,
                "places": 0,
                "bare_hand_rock_breaks": [],
                "damages": [],
                "deaths": [],
                "advancements": [],
                "kicks": [],
                "sample_stuck": 0,
                "blocks": {},
            }
            players[name] = rec
        return rec

    def ensure_blk(prec: Dict[str, Any], dim: str, pos: List[int]) -> Dict[str, Any]:
        k = f"{dim}:{pos[0]}:{pos[1]}:{pos[2]}"
        b = prec["blocks"].get(k)
        if b is None:
            b = {"k": k, "d": dim, "p": list(pos), "dw": 0, "st": 0, "dg": 0, "df": 0, "pl": 0, "pf": 0}
            prec["blocks"][k] = b
        return b

    last_sample_sig: Dict[str, Tuple[Any, ...]] = {}
    with server_log_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            out["event_count"] += 1
            player = str(ev.get("player") or "")
            if not player:
                continue
            prec = ensure_p(player)
            et = str(ev.get("event") or "")
            dim = str(ev.get("dim") or "overworld")
            ts_ms = _safe_int(ev.get("ts_ms"), _safe_int(_safe_float(ev.get("ts")) * 1000))
            d = ev.get("data") or {}

            keep_in_timeline = et in {
                "BLOCK_BREAK", "BLOCK_PLACE", "BUCKET_FILL", "BUCKET_EMPTY",
                "PLAYER_DAMAGE", "PLAYER_DEATH", "PLAYER_RESPAWN",
                "ADVANCEMENT", "INVENTORY_OPEN", "PLAYER_KICK", "PLAYER_QUIT",
            }
            if et == "BLOCK_BREAK":
                prec["breaks"] += 1
                pos = d.get("pos") or [0, 0, 0]
                if isinstance(pos, list) and len(pos) == 3:
                    b = ensure_blk(prec, dim, [_safe_int(pos[0]), _safe_int(pos[1]), _safe_int(pos[2])])
                    if d.get("cancelled"):
                        b["df"] += 1
                    else:
                        b["dg"] += 1
                if not d.get("cancelled") and _is_bare_hand_rock(d.get("block", ""), d.get("hand", "")):
                    prec["bare_hand_rock_breaks"].append({
                        "ts_ms": ts_ms, "dim": dim, "pos": pos, "block": d.get("block"), "hand": d.get("hand"),
                    })
            elif et == "BLOCK_PLACE":
                prec["places"] += 1
                pos = d.get("pos") or [0, 0, 0]
                if isinstance(pos, list) and len(pos) == 3:
                    b = ensure_blk(prec, dim, [_safe_int(pos[0]), _safe_int(pos[1]), _safe_int(pos[2])])
                    if d.get("cancelled"):
                        b["pf"] += 1
                    else:
                        b["pl"] += 1
            elif et == "PLAYER_DAMAGE":
                prec["damages"].append({"ts_ms": ts_ms, "cause": d.get("cause"), "attacker": d.get("attacker"), "final_damage": d.get("final_damage")})
            elif et == "PLAYER_DEATH":
                prec["deaths"].append({"ts_ms": ts_ms, "dim": dim, "pos": d.get("pos"), "message": d.get("message")})
            elif et == "ADVANCEMENT":
                prec["advancements"].append({"ts_ms": ts_ms, "key": d.get("key")})
            elif et == "PLAYER_KICK":
                prec["kicks"].append({"ts_ms": ts_ms, "reason": d.get("reason")})
            elif et == "PLAYER_SAMPLE":
                blk = d.get("block") or [0, 0, 0]
                if isinstance(blk, list) and len(blk) == 3:
                    b = ensure_blk(prec, dim, [_safe_int(blk[0]), _safe_int(blk[1]), _safe_int(blk[2])])
                    b["dw"] += 1
                    if d.get("same_cell"):
                        b["st"] += 1
                        prec["sample_stuck"] += 1
                sig = (dim, bool(d.get("same_cell")), str(d.get("hand") or ""), _safe_int(d.get("hostiles_12m")), bool(d.get("in_water")), bool(d.get("in_lava")))
                if last_sample_sig.get(player) != sig or d.get("same_cell"):
                    last_sample_sig[player] = sig
                    keep_in_timeline = True

            if keep_in_timeline:
                prec["timeline"].append({
                    "ts_ms": ts_ms,
                    "ts_fmt": _fmt_ts_ms(ts_ms),
                    "source": "mc_server",
                    "player": player,
                    "event": et,
                    "dim": dim,
                    "summary": _summarize_server_event(et, d),
                    "data": d,
                })

    out["players"] = players
    return out


def _summarize_server_event(et: str, d: Dict[str, Any]) -> str:
    if et == "BLOCK_BREAK":
        return f"break {d.get('block')} @ {d.get('pos')} (hand={d.get('hand')})"
    if et == "BLOCK_PLACE":
        return f"place {d.get('block')} @ {d.get('pos')}"
    if et == "PLAYER_DAMAGE":
        return f"dmg {d.get('cause')} ({d.get('final_damage')}) by {d.get('attacker') or '-'} -> hp={d.get('hp_after')}"
    if et == "PLAYER_DEATH":
        return f"DEATH: {d.get('message')}"
    if et == "ADVANCEMENT":
        return f"advancement: {d.get('key')}"
    if et == "PLAYER_SAMPLE":
        return f"sample block={d.get('block')} hand={d.get('hand')} hp={d.get('hp')} stuck={d.get('same_cell')} hostiles={d.get('hostiles_12m')}"
    if et == "INVENTORY_OPEN":
        return f"open {d.get('inv_type')} @ {d.get('pos')}"
    return f"{et}: {json.dumps(d, ensure_ascii=False)[:100]}"


def _summarize_client_event(et: str, d: Dict[str, Any]) -> str:
    if et == "STATE_TRANSITION":
        to = d.get("to") or {}
        return f"t={d.get('tick')} {to.get('branch')} | {to.get('task_chain') or '-'} [{to.get('goal_status')}] out={d.get('outcome')} fail={to.get('fail') or '-'}"
    if et == "LLM_IO":
        tcs = [tc.get("name") for tc in (d.get("tool_calls") or []) if isinstance(tc, dict)]
        return f"LLM_IO ({d.get('route')}/{d.get('confirmed_model')}) {d.get('latency_ms')}ms pt={d.get('prompt_tokens')} ct={d.get('completion_tokens')} -> {tcs}"
    if et == "LLM_RESPONSE":
        pr = d.get("plan_result") or {}
        return f"LLM_RESPONSE mounted={pr.get('mounted') or pr.get('action') or '-'}"
    if et == "LLM_DISCARDED":
        return f"LLM_DISCARDED job={d.get('job_id')} ver {d.get('submitted_ctx_ver')}->{d.get('current_ctx_ver')} reason={d.get('stop_reason')}"
    if et == "API_ERROR":
        return f"API_ERR {d.get('endpoint')} code={d.get('code')} target={d.get('target') or '-'}: {d.get('message')}"
    if et == "TICK_SUMMARY":
        return f"t={d.get('tick')} {d.get('branch')} | {d.get('goal') or '-'} out={d.get('out')} fail={d.get('fail') or '-'}"
    return f"{et}: {json.dumps(d, ensure_ascii=False)[:100]}"


def load_client_traces(run_dir: Path) -> Dict[str, Any]:
    """Load `v2_debug_trace_*.jsonl` (preferred) + `v2_flight_*.jsonl` signals per bot."""
    subdirs = sorted(
        [p for p in run_dir.iterdir() if p.is_dir() and re.match(r"bot_(\d+)$", p.name)],
        key=lambda p: int(p.name.split("_")[1]),
    )
    bot_dirs = [(p.name, p) for p in subdirs] if subdirs else [(run_dir.name, run_dir)]

    bots: Dict[str, Dict[str, Any]] = {}
    player_to_bot: Dict[str, str] = {}

    for bot_id, bdir in bot_dirs:
        trace_files = sorted(bdir.glob("v2_debug_trace_*.jsonl"))
        flight_files = sorted(bdir.glob("v2_flight_*.jsonl"))
        player_name = bot_id
        timeline: List[Dict[str, Any]] = []
        llm_http = 0
        llm_mounted = 0
        llm_discarded = 0
        no_pickaxe_hits = 0
        stalled_hits = 0
        descents = []
        was_above = True
        entry_xz = None
        seen_state: Optional[Tuple[str, str, str]] = None

        # Read flights first for metadata (player username, llm counts, descents, baseline states)
        for fp in flight_files:
            with fp.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    et = str(ev.get("event") or "")
                    d = ev.get("data") or {}
                    ts_ms = _safe_int(ev.get("ts_ms"), _safe_int(_safe_float(ev.get("ts")) * 1000))
                    if et == "STARTUP" and d.get("username"):
                        player_name = str(d["username"])
                    elif et == "LLM_HTTP":
                        llm_http += 1
                    elif et == "LLM_RESPONSE":
                        pr = d.get("plan_result") or {}
                        if pr.get("ok") and pr.get("mounted"):
                            llm_mounted += 1
                        if not trace_files:
                            timeline.append({
                                "ts_ms": ts_ms, "ts_fmt": _fmt_ts_ms(ts_ms), "source": "client_llm",
                                "player": player_name, "event": et, "summary": _summarize_client_event(et, d), "data": d,
                            })
                    elif et == "LLM_DISCARDED":
                        llm_discarded += 1
                        if not trace_files:
                            timeline.append({
                                "ts_ms": ts_ms, "ts_fmt": _fmt_ts_ms(ts_ms), "source": "client_llm",
                                "player": player_name, "event": et, "summary": _summarize_client_event(et, d), "data": d,
                            })
                    elif et == "TICK_SUMMARY":
                        pos = d.get("pos") or [0, 64, 0]
                        if isinstance(pos, list) and len(pos) >= 3:
                            x, y, z = _safe_float(pos[0]), _safe_float(pos[1]), _safe_float(pos[2])
                            if y >= 58:
                                was_above = True
                                entry_xz = (round(x), round(z))
                            elif was_above and y <= 48 and entry_xz is not None:
                                descents.append(entry_xz)
                                was_above = False
                        fail_str = str(d.get("fail") or "")
                        if "no_pickaxe" in fail_str:
                            no_pickaxe_hits += 1
                        if "stalled_no_displacement" in fail_str:
                            stalled_hits += 1
                        if not trace_files:
                            state_sig = (str(d.get("branch") or ""), str(d.get("goal") or ""), normalize_fail(fail_str))
                            if seen_state != state_sig:
                                seen_state = state_sig
                                timeline.append({
                                    "ts_ms": ts_ms, "ts_fmt": _fmt_ts_ms(ts_ms), "source": "client_bt",
                                    "player": player_name, "event": "TICK_SUMMARY", "summary": _summarize_client_event("TICK_SUMMARY", d), "data": d,
                                })
                    elif et == "API_ERROR" and not trace_files:
                        timeline.append({
                            "ts_ms": ts_ms, "ts_fmt": _fmt_ts_ms(ts_ms), "source": "client_api",
                            "player": player_name, "event": et, "summary": _summarize_client_event(et, d), "data": d,
                        })

        # Read rich debug traces if available
        for tp in trace_files:
            with tp.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    et = str(ev.get("event") or "")
                    d = ev.get("data") or {}
                    if ev.get("player"):
                        player_name = str(ev["player"])
                    elif et == "STARTUP" and d.get("username"):
                        player_name = str(d["username"])
                    ts_ms = _safe_int(ev.get("ts_ms"), _safe_int(_safe_float(ev.get("ts")) * 1000))
                    if et == "STATE_TRANSITION":
                        to = d.get("to") or {}
                        if "no_pickaxe" in str(to.get("fail") or ""):
                            no_pickaxe_hits += 1
                    src = "client_llm" if et.startswith("LLM_") else ("client_api" if et == "API_ERROR" else "client_bt")
                    timeline.append({
                        "ts_ms": ts_ms, "ts_fmt": _fmt_ts_ms(ts_ms), "source": src,
                        "player": player_name, "event": et, "summary": _summarize_client_event(et, d), "data": d,
                    })

        # Cluster shaft entries (>16m apart = distinct shaft sites)
        clusters: List[List[Any]] = []
        for ex, ez in descents:
            matched = False
            for c in clusters:
                if math.hypot(ex - c[0][0], ez - c[0][1]) <= 16.0:
                    c[1] += 1
                    matched = True
                    break
            if not matched:
                clusters.append([(ex, ez), 1])

        player_to_bot[player_name] = bot_id
        bots[bot_id] = {
            "bot_id": bot_id,
            "player": player_name,
            "has_debug_trace": bool(trace_files),
            "llm_http": llm_http,
            "llm_mounted": llm_mounted,
            "llm_discarded": llm_discarded,
            "no_pickaxe_hits": no_pickaxe_hits,
            "stalled_hits": stalled_hits,
            "descents": len(descents),
            "distinct_shaft_sites": len(clusters),
            "timeline": timeline,
        }

    return {"bots": bots, "player_to_bot": player_to_bot}


def classify_anomalies(
    heatmap: Dict[str, Any],
    client: Dict[str, Any],
    server: Dict[str, Any],
    baseline_heatmap: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Classify runtime issues into SYSTEMIC vs LOCAL_CLUSTER vs ONE_OFF using fused evidence."""
    anomalies: List[Dict[str, Any]] = []
    hm_bots = {b["bot_id"]: b for b in heatmap.get("bots") or ()}
    total_stuck = max(1, _safe_int(heatmap.get("total_stuck_ticks")))
    base_codes = (baseline_heatmap or {}).get("code_hotspots") or {}

    def _severity(affected_count: int, hits: int, stuck_share: float, persists: bool) -> str:
        if affected_count >= 2 or stuck_share >= 15.0 or (persists and hits >= 5):
            return "SYSTEMIC"
        if affected_count == 1 and hits >= 10:
            return "LOCAL_CLUSTER"
        return "ONE_OFF"

    # 1. Underground No-Pickaxe Lock
    np_bots = []
    np_hits = 0
    for bid, cb in client["bots"].items():
        if cb["no_pickaxe_hits"] > 0:
            np_bots.append(bid)
            np_hits += cb["no_pickaxe_hits"]
    if np_hits > 0:
        stuck_no_pick = sum(
            c["stuck_ticks"] for k, c in heatmap["code_hotspots"].items()
            if "no_pickaxe" in k or "stone_pickaxex1" in k
        )
        persists = any("no_pickaxe" in k for k in base_codes)
        sev = _severity(len(np_bots), np_hits, 100.0 * stuck_no_pick / total_stuck, persists)
        anomalies.append({
            "id": "UNDERGROUND_NO_PICK_LOCK",
            "title": "地下断镐无木软锁 / 禁止裸手退回路径锁死",
            "severity": sev,
            "affected_bots": sorted(np_bots),
            "hits": np_hits,
            "stuck_ticks": stuck_no_pick,
            "persisted_across_runs": persists,
            "evidence": f"{len(np_bots)} 台 Bot 共触发 {np_hits} 次 `no_pickaxe` 阻塞，相关分支累计原地卡死 {stuck_no_pick} Ticks。",
        })

    # 2. Duplicate Shaft Carving
    dup_bots = [bid for bid, cb in client["bots"].items() if cb["distinct_shaft_sites"] >= 2]
    total_sites = sum(cb["distinct_shaft_sites"] for cb in client["bots"].values())
    total_descents = sum(cb["descents"] for cb in client["bots"].values())
    if dup_bots:
        anomalies.append({
            "id": "DUPLICATE_SHAFT_CARVING",
            "title": "重复挖掘多条新矿道 / 竖井阶梯交叉",
            "severity": "SYSTEMIC" if len(dup_bots) >= 2 else "LOCAL_CLUSTER",
            "affected_bots": sorted(dup_bots),
            "hits": total_sites,
            "stuck_ticks": 0,
            "persisted_across_runs": bool(baseline_heatmap),
            "evidence": f"{len(dup_bots)} 台 Bot 开挖 >=2 处独立下探井口（全队 {total_descents} 次下探产生 {total_sites} 处点位）。",
        })

    # 3. LLM Preempt / Discard Storm
    storm_bots = []
    storm_wasted = 0
    for bid, cb in client["bots"].items():
        wasted = max(cb["llm_discarded"], cb["llm_http"] - cb["llm_mounted"])
        rate = wasted / max(1, cb["llm_http"])
        if cb["llm_http"] >= 20 and rate >= 0.50:
            storm_bots.append(bid)
            storm_wasted += wasted
    if storm_bots:
        anomalies.append({
            "id": "LLM_PREEMPT_DISCARD_STORM",
            "title": "异步规划高频被抢占/冷却丢弃（LLM 空转消耗）",
            "severity": "SYSTEMIC" if len(storm_bots) >= 2 else "LOCAL_CLUSTER",
            "affected_bots": sorted(storm_bots),
            "hits": storm_wasted,
            "stuck_ticks": 0,
            "persisted_across_runs": False,
            "evidence": f"{', '.join(sorted(storm_bots))} LLM 丢弃/未挂载率 >= 50%，合计无效请求 {storm_wasted} 次。",
        })

    # 4. Bare-Hand Rock Breaking (server ground truth OR slow dig>=6s)
    srv_bare_bots = []
    srv_bare_hits = 0
    for pname, prec in server["players"].items():
        if prec["bare_hand_rock_breaks"]:
            bid = client["player_to_bot"].get(pname, pname)
            srv_bare_bots.append(bid)
            srv_bare_hits += len(prec["bare_hand_rock_breaks"])
    dig_ge6s = _safe_int(heatmap.get("dig_ge6s"))
    if srv_bare_hits > 0 or dig_ge6s >= 50:
        hits = srv_bare_hits if srv_bare_hits > 0 else dig_ge6s
        src_note = f"服务端 `BLOCK_BREAK` 实测 {srv_bare_hits} 次无镐碎石" if srv_bare_hits > 0 else f"客户端 `dig >= 6s` 慢挖 {dig_ge6s} 次"
        anomalies.append({
            "id": "BARE_HAND_ROCK_BREAK",
            "title": "空手或错工具硬刨硬石/矿石",
            "severity": "SYSTEMIC" if (len(srv_bare_bots) >= 2 or dig_ge6s >= 100) else ("LOCAL_CLUSTER" if hits >= 10 else "ONE_OFF"),
            "affected_bots": sorted(srv_bare_bots) if srv_bare_bots else sorted(hm_bots.keys()),
            "hits": hits,
            "stuck_ticks": 0,
            "persisted_across_runs": _safe_int((baseline_heatmap or {}).get("dig_ge6s")) >= 100,
            "evidence": src_note,
        })

    # 5. Physics Stall / Movement Loop
    stall_bots = []
    stall_hits = 0
    for bid, cb in client["bots"].items():
        hb = hm_bots.get(bid) or {}
        stuck_pct = 100.0 * _safe_int(hb.get("total_stuck_ticks")) / max(1, _safe_int(hb.get("total_ticks")))
        if cb["stalled_hits"] >= 15 or stuck_pct >= 25.0:
            stall_bots.append(bid)
            stall_hits += max(cb["stalled_hits"], _safe_int(hb.get("total_stuck_ticks")))
    if stall_bots:
        anomalies.append({
            "id": "PHYSICS_STALL_OR_LOOP",
            "title": "物理位移卡死 / 寻路原地滞留",
            "severity": "SYSTEMIC" if len(stall_bots) >= 2 else "LOCAL_CLUSTER",
            "affected_bots": sorted(stall_bots),
            "hits": stall_hits,
            "stuck_ticks": heatmap.get("total_stuck_ticks", 0),
            "persisted_across_runs": _safe_float((baseline_heatmap or {}).get("stuck_pct")) >= 15.0,
            "evidence": f"{', '.join(sorted(stall_bots))} 出现高密度停滞/无位移报错（合计特征计数 {stall_hits}）。",
        })

    # 6. Server-side Combat Mortality (when server log available)
    if server["available"]:
        high_death_bots = [
            client["player_to_bot"].get(p, p)
            for p, prec in server["players"].items()
            if len(prec["deaths"]) >= 4
        ]
        total_deaths = sum(len(prec["deaths"]) for prec in server["players"].values())
        if total_deaths >= 12 or high_death_bots:
            anomalies.append({
                "id": "HIGH_COMBAT_MORTALITY",
                "title": "服务端高频战斗/环境死亡打断进度",
                "severity": "SYSTEMIC" if (total_deaths >= 12 or len(high_death_bots) >= 2) else "LOCAL_CLUSTER",
                "affected_bots": sorted(high_death_bots) or sorted(client["bots"].keys()),
                "hits": total_deaths,
                "stuck_ticks": 0,
                "persisted_across_runs": False,
                "evidence": f"服务端记录总死亡 {total_deaths} 次。",
            })

    rank = {"SYSTEMIC": 0, "LOCAL_CLUSTER": 1, "ONE_OFF": 2}
    anomalies.sort(key=lambda a: (rank.get(a["severity"], 9), -a["hits"], a["id"]))
    return anomalies


def _merge_aligned_timelines(client: Dict[str, Any], server: Dict[str, Any], *, max_events: int = 3500) -> Dict[str, List[Dict[str, Any]]]:
    merged: Dict[str, List[Dict[str, Any]]] = {}
    for bid, cb in client["bots"].items():
        events = list(cb["timeline"])
        pname = cb["player"]
        sp = server["players"].get(pname) or server["players"].get(bid)
        if sp is not None:
            events.extend(sp["timeline"])
        events.sort(key=lambda e: (e["ts_ms"], 0 if e["source"] == "mc_server" else 1))
        if len(events) > max_events:
            # Sample deterministically while preserving deaths, kicks, LLM_DISCARDED, API_ERROR
            important = [e for e in events if e["event"] in {"PLAYER_DEATH", "PLAYER_KICK", "ADVANCEMENT", "LLM_DISCARDED", "API_ERROR", "LLM_IO", "LLM_RESPONSE"}]
            rest = [e for e in events if e not in important]
            step = max(1, len(rest) // max(1, max_events - len(important)))
            events = sorted(important + rest[::step], key=lambda e: e["ts_ms"])[:max_events]
        merged[bid] = events

    # Include any server-only player not mapped to client bot dir
    for pname, sp in server["players"].items():
        bid = client["player_to_bot"].get(pname)
        if bid is None:
            merged[pname] = sorted(sp["timeline"], key=lambda e: e["ts_ms"])[:max_events]
    return merged


def render_workbench_markdown(
    heatmap: Dict[str, Any],
    client: Dict[str, Any],
    server: Dict[str, Any],
    anomalies: List[Dict[str, Any]],
    diff: Optional[Dict[str, Any]] = None,
) -> str:
    lines: List[str] = []
    lines.append(f"# BotSleep 全栈交叉调试工作台报告：`{heatmap['run_name']}`")
    lines.append("")
    lines.append(f"- 客户端数据源：`{', '.join(heatmap['sources'])}` | 总采样帧：`{heatmap['total_ticks']}` | 停滞率：**`{heatmap['stuck_pct']}%`** (`{heatmap['total_stuck_ticks']}`)")
    if server["available"]:
        lines.append(f"- 服务端真值日志：`{server['path']}`（已对齐事件 `{server['event_count']}` 条，追踪玩家 `{len(server['players'])}` 名）")
    else:
        lines.append("- 服务端真值日志：未挂载（本报告使用客户端时间线与热力图重建；后续开启 `BotSleepDebug.jar` 后自动双端对齐）")
    if diff is not None:
        sd = diff["summary_deltas"]
        lines.append(f"- 较基线 `{diff['baseline_name']}`：停滞率 `{sd['stuck_pct']:+.2f} pp`、`>=6s 慢挖 {sd['dig_ge6s']:+d}`、`place:no_support {sd['place_no_support']:+d}`")

    lines.append("")
    lines.append("## 一、 系统性问题 vs 偶发问题自动判别")
    lines.append("")
    if not anomalies:
        lines.append("_未检测到显著异常热点。_")
    else:
        lines.append("| 分类 | 问题模式 | 影响 Bot | 证据强度 |")
        lines.append("|---|---|---|---|")
        for a in anomalies:
            lines.append(f"| **{a['severity']}** | `{a['id']}` ({a['title']}) | `{','.join(a['affected_bots'])}` | {a['evidence']} |")

    lines.append("")
    lines.append("## 二、 逐 Bot 全栈观测汇总")
    lines.append("")
    lines.append("| Bot | 玩家 | DebugTrace | 下探/独立井口 | 停滞率 | `no_pickaxe` | `stalled` | LLM (调/挂/弃) | 服务端事件 |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|")
    hm_bots = {b["bot_id"]: b for b in heatmap["bots"]}
    for bid, cb in sorted(client["bots"].items(), key=lambda kv: kv[0]):
        hb = hm_bots.get(bid) or {}
        sp = server["players"].get(cb["player"])
        stuck_pct = round(100.0 * _safe_int(hb.get("total_stuck_ticks")) / max(1, _safe_int(hb.get("total_ticks"))), 1)
        srv_str = f"brk={sp['breaks']}, death={len(sp['deaths'])}" if sp else "-"
        lines.append(
            f"| `{bid}` | `{cb['player']}` | {'YES' if cb['has_debug_trace'] else 'fallback'} | "
            f"{cb['descents']} / {cb['distinct_shaft_sites']} | `{stuck_pct}%` | {cb['no_pickaxe_hits']} | {cb['stalled_hits']} | "
            f"{cb['llm_http']}/{cb['llm_mounted']}/{cb['llm_discarded']} | {srv_str} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_workbench_html(
    heatmap: Dict[str, Any],
    client: Dict[str, Any],
    server: Dict[str, Any],
    anomalies: List[Dict[str, Any]],
    diff: Optional[Dict[str, Any]] = None,
) -> str:
    merged_timelines = _merge_aligned_timelines(client, server)
    bots_hm = []
    for hb in heatmap["bots"]:
        bid = hb["bot_id"]
        pname = client["bots"].get(bid, {}).get("player", bid)
        # Fuse server block breaks/places if present
        merged_blocks = dict(hb["blocks"])
        sp = server["players"].get(pname)
        if sp:
            for k, sb in sp["blocks"].items():
                if k not in merged_blocks:
                    merged_blocks[k] = {
                        "dim": sb["d"], "pos": sb["p"], "dwell": sb["dw"], "stuck": sb["st"],
                        "dig_ok": sb["dg"], "dig_fail": sb["df"], "place_ok": sb["pl"], "place_fail": sb["pf"], "codes": {"mc_server:ground_truth": sb["dg"] + sb["pl"] + sb["st"]},
                    }
        blks_list = []
        for k, rec in list(merged_blocks.items())[:1400]:
            dig = _safe_int(rec.get("dig_ok")) + _safe_int(rec.get("dig_fail"))
            pl = _safe_int(rec.get("place_ok")) + _safe_int(rec.get("place_fail"))
            st = _safe_int(rec.get("stuck"))
            dw = _safe_int(rec.get("dwell"))
            blks_list.append({"k": k, "d": rec.get("dim", "overworld"), "p": rec.get("pos", [0, 64, 0]), "dw": dw, "st": st, "dg": dig, "pl": pl, "h": st * 3 + dw + dig * 2 + pl, "c": rec.get("codes", {})})
        bots_hm.append({"id": bid, "player": pname, "ticks": hb["total_ticks"], "stuck": hb["total_stuck_ticks"], "blocks": blks_list})

    codes_list = [
        {"key": c["key"], "br": c["branch"], "tc": c["task_chain"], "out": c["outcome"], "fail": c["fail"], "stuck": c["stuck_ticks"], "ticks": c["ticks"]}
        for c in sorted(heatmap["code_hotspots"].values(), key=lambda x: (-x["stuck_ticks"], -x["ticks"]))[:50]
    ]

    payload = {
        "run_name": heatmap["run_name"],
        "server_available": server["available"],
        "server_events": server["event_count"],
        "stuck_pct": heatmap["stuck_pct"],
        "total_ticks": heatmap["total_ticks"],
        "total_stuck": heatmap["total_stuck_ticks"],
        "dig_ge6s": heatmap["dig_ge6s"],
        "place_no_support": heatmap["place_no_support"],
        "anomalies": anomalies,
        "diff": diff,
        "timelines": merged_timelines,
        "bots_hm": bots_hm,
        "codes": codes_list,
    }
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    title = _html.escape(f"BotSleep Debug Workbench - {heatmap['run_name']}")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>{title}</title>
<style>
  :root {{ --bg:#0d1117; --panel:#161b22; --panel2:#1f2631; --border:#30363d; --text:#e6edf3; --muted:#8b949e; --accent:#58a6ff; --red:#f85149; --amber:#d29922; --green:#3fb950; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; background:var(--bg); color:var(--text); }}
  header {{ padding:14px 20px; border-bottom:1px solid var(--border); background:#11161d; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; }}
  .tabs {{ display:flex; gap:8px; padding:12px 20px 0; border-bottom:1px solid var(--border); background:#11161d; }}
  .tab {{ padding:8px 14px; border:1px solid transparent; border-bottom:none; border-radius:6px 6px 0 0; cursor:pointer; color:var(--muted); background:transparent; }}
  .tab.active {{ color:var(--text); background:var(--bg); border-color:var(--border); font-weight:600; }}
  .pane {{ display:none; padding:16px 20px; }}
  .pane.active {{ display:block; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:10px; margin-bottom:14px; }}
  .card {{ background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:10px 12px; }}
  .card .k {{ font-size:11px; color:var(--muted); }} .card .v {{ font-size:20px; font-weight:700; margin-top:4px; }}
  .anom {{ background:var(--panel); border:1px solid var(--border); border-left:4px solid var(--amber); border-radius:8px; padding:12px; margin-bottom:10px; }}
  .anom.SYSTEMIC {{ border-left-color:var(--red); }} .anom.LOCAL_CLUSTER {{ border-left-color:var(--amber); }} .anom.ONE_OFF {{ border-left-color:var(--green); }}
  .badge {{ display:inline-block; padding:2px 7px; border-radius:4px; font-size:11px; font-weight:700; margin-right:8px; }}
  .badge.SYSTEMIC {{ background:#491b1d; color:#ffa198; }} .badge.LOCAL_CLUSTER {{ background:#422c0c; color:#f2cc60; }} .badge.ONE_OFF {{ background:#153620; color:#7ee787; }}
  .src {{ display:inline-block; padding:1px 6px; border-radius:4px; font-size:11px; font-weight:600; }}
  .src.mc_server {{ background:#1f3b28; color:#7ee787; }} .src.client_bt {{ background:#1d324d; color:#79c0ff; }} .src.client_llm {{ background:#3b264d; color:#d2a8ff; }} .src.client_api {{ background:#4b1d1d; color:#ffa198; }}
  select, button, input {{ background:var(--panel2); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:6px 10px; font-size:12px; }}
  .tl-wrap {{ max-height:620px; overflow:auto; border:1px solid var(--border); border-radius:8px; background:var(--panel); }}
  table {{ width:100%; border-collapse:collapse; font-size:12px; }}
  th, td {{ padding:6px 8px; border-bottom:1px solid var(--border); text-align:left; vertical-align:top; }}
  th {{ position:sticky; top:0; background:var(--panel2); }}
  details pre {{ margin:6px 0 0; padding:8px; background:#0b0f14; border-radius:4px; overflow:auto; max-height:240px; font-size:11px; }}
  .hm-grid {{ display:grid; grid-template-columns:1.5fr 1fr; gap:12px; }}
  #wbCanvas {{ width:100%; height:460px; background:#080b10; border:1px solid var(--border); border-radius:8px; }}
</style>
</head>
<body>
<header>
  <div><b>BotSleep 全栈交叉调试工作台</b> <span id="hdrMeta" style="font-size:12px;color:var(--muted);"></span></div>
  <div style="font-size:12px;color:var(--muted);">服务端真值 × 状态机跃迁 × LLM I/O &amp; 丢弃 × 方块/代码热度</div>
</header>
<div class="tabs">
  <button class="tab active" data-tab="diag">1. 系统性 vs 偶发诊断</button>
  <button class="tab" data-tab="timeline">2. 毫秒时间戳交叉时间线</button>
  <button class="tab" data-tab="heatmap">3. 空间方块 &amp; 代码热度图</button>
</div>

<div id="pane-diag" class="pane active">
  <div class="cards" id="diagCards"></div>
  <div id="anomList"></div>
</div>

<div id="pane-timeline" class="pane">
  <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px;align-items:center;">
    <label>Bot: <select id="tlBot"></select></label>
    <label><input type="checkbox" class="tlSrc" value="mc_server" checked /> MC 服务端真值</label>
    <label><input type="checkbox" class="tlSrc" value="client_bt" checked /> 客户端 BT 状态机</label>
    <label><input type="checkbox" class="tlSrc" value="client_llm" checked /> LLM I/O &amp; 丢弃</label>
    <label><input type="checkbox" class="tlSrc" value="client_api" checked /> API 报错</label>
    <input id="tlSearch" placeholder="过滤关键字 (如 no_pickaxe / stone / stalled)..." style="min-width:240px;" />
  </div>
  <div class="tl-wrap"><table id="tlTable"><thead><tr><th>时间 (`ts_ms`)</th><th>来源</th><th>事件</th><th>摘要 &amp; 原始上下文</th></tr></thead><tbody></tbody></table></div>
</div>

<div id="pane-heatmap" class="pane">
  <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px;align-items:center;">
    <label>Bot: <select id="hmBot"></select></label>
    <label>视角: <select id="hmProj"><option value="xz">XZ 俯视</option><option value="xy">XY 矿道纵剖面</option><option value="zy">ZY 侧剖面</option></select></label>
    <label>指标: <select id="hmMetric"><option value="h">综合热度</option><option value="st">卡死停留</option><option value="dg">挖掘次数</option><option value="pl">放置次数</option></select></label>
    <button id="hmFit" type="button">居中 Fit</button>
  </div>
  <div class="hm-grid">
    <div><canvas id="wbCanvas" width="840" height="460"></canvas><div id="hmInsp" style="margin-top:8px;padding:8px;background:var(--panel);border-radius:6px;font-size:12px;">点击画布方块查看详情</div></div>
    <div style="max-height:520px;overflow:auto;background:var(--panel);border:1px solid var(--border);border-radius:8px;">
      <table id="codeTbl"><thead><tr><th>代码热点</th><th>Stuck</th><th>Ticks</th></tr></thead><tbody></tbody></table>
    </div>
  </div>
</div>

<script>
const W = {blob};
document.getElementById('hdrMeta').textContent = ` | Run=${{W.run_name}} | ServerPlugin=${{W.server_available ? ('ON (' + W.server_events + ' events)') : 'OFF (Client Fallback)'}}`;

document.querySelectorAll('.tab').forEach(btn => btn.onclick = () => {{
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.pane').forEach(x => x.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('pane-' + btn.dataset.tab).classList.add('active');
  if (btn.dataset.tab === 'heatmap') renderHm(true);
}});

function addCard(k, v) {{
  const d = document.createElement('div'); d.className = 'card';
  d.innerHTML = `<div class="k">${{k}}</div><div class="v">${{v}}</div>`;
  document.getElementById('diagCards').appendChild(d);
}}
addCard('Total Ticks', W.total_ticks);
addCard('Stuck Rate', `${{W.stuck_pct}}% (${{W.total_stuck}})`);
addCard('Dig >=6s Slow', W.dig_ge6s);
addCard('place:no_support', W.place_no_support);
addCard('Anomalies Found', W.anomalies.length);

const anomEl = document.getElementById('anomList');
W.anomalies.forEach(a => {{
  const div = document.createElement('div');
  div.className = 'anom ' + a.severity;
  div.innerHTML = `<div><span class="badge ${{a.severity}}">${{a.severity}}</span><b>${{a.title}}</b> <code>${{a.id}}</code></div>
    <div style="margin-top:6px;font-size:13px;">${{a.evidence}}</div>
    <div style="margin-top:6px;font-size:12px;color:var(--muted);">Affected Bots: <code>${{a.affected_bots.join(', ')}}</code> | <a href="#" style="color:var(--accent);" onclick="jumpBot('${{a.affected_bots[0] || ''}}');return false;">查看首个 Bot 交叉时间线 →</a></div>`;
  anomEl.appendChild(div);
}});

const botIds = Object.keys(W.timelines);
const tlBot = document.getElementById('tlBot');
const hmBot = document.getElementById('hmBot');
const optAll = new Option(`全部 (${{W.bots_hm.length}})`, '__ALL__');
hmBot.add(optAll);
botIds.forEach(b => tlBot.add(new Option(b, b)));
W.bots_hm.forEach(b => hmBot.add(new Option(`${{b.id}} (stuck=${{b.stuck}})`, b.id)));

function jumpBot(bid) {{
  if (bid && botIds.includes(bid)) tlBot.value = bid;
  document.querySelector('.tab[data-tab="timeline"]').click();
  renderTimeline();
}}

function renderTimeline() {{
  const bid = tlBot.value;
  const srcs = new Set(Array.from(document.querySelectorAll('.tlSrc:checked')).map(c => c.value));
  const q = document.getElementById('tlSearch').value.trim().toLowerCase();
  const tbody = document.querySelector('#tlTable tbody');
  tbody.innerHTML = '';
  const rows = (W.timelines[bid] || []).filter(e => srcs.has(e.source) && (!q || (e.summary + e.event + JSON.stringify(e.data)).toLowerCase().includes(q)));
  rows.slice(0, 600).forEach(e => {{
    const tr = document.createElement('tr');
    const rawJson = JSON.stringify(e.data, null, 2).replace(/</g, '&lt;');
    tr.innerHTML = `<td style="white-space:nowrap;">${{e.ts_fmt}}<br/><span style="color:var(--muted);font-size:10px;">${{e.ts_ms}}</span></td>
      <td><span class="src ${{e.source}}">${{e.source}}</span></td>
      <td><code>${{e.event}}</code></td>
      <td>${{e.summary}}<details><summary style="color:var(--muted);cursor:pointer;">JSON</summary><pre>${{rawJson}}</pre></details></td>`;
    tbody.appendChild(tr);
  }});
}}
tlBot.onchange = renderTimeline;
document.querySelectorAll('.tlSrc').forEach(c => c.onchange = renderTimeline);
document.getElementById('tlSearch').oninput = renderTimeline;
renderTimeline();

// Heatmap canvas
let hmCells = [], hmView = {{ cx:0, cy:0, scale:10 }};
function renderHm(fit) {{
  const bid = hmBot.value, proj = document.getElementById('hmProj').value, m = document.getElementById('hmMetric').value;
  const map = new Map(); let maxV = 1;
  for (const b of W.bots_hm) {{
    if (bid !== '__ALL__' && b.id !== bid) continue;
    for (const blk of b.blocks) {{
      const u = proj === 'zy' ? blk.p[2] : blk.p[0];
      const v = proj === 'xz' ? blk.p[2] : -blk.p[1];
      const k = u + ',' + v;
      let cell = map.get(k);
      if (!cell) {{ cell = {{ u, v, val:0, items:[] }}; map.set(k, cell); }}
      cell.val += (blk[m] || 0); cell.items.push({{ bot:b.id, blk }});
      if (cell.val > maxV) maxV = cell.val;
    }}
  }}
  hmCells = Array.from(map.values()).sort((a,b) => a.val - b.val);
  const cv = document.getElementById('wbCanvas');
  if (fit && hmCells.length) {{
    let u0=Infinity,u1=-Infinity,v0=Infinity,v1=-Infinity;
    hmCells.forEach(c => {{ u0=Math.min(u0,c.u); u1=Math.max(u1,c.u); v0=Math.min(v0,c.v); v1=Math.max(v1,c.v); }});
    hmView.cx = (u0+u1)/2; hmView.cy = (v0+v1)/2;
    hmView.scale = Math.max(2, Math.min(32, Math.min(cv.width/Math.max(12,u1-u0+4), cv.height/Math.max(12,v1-v0+4))*0.88));
  }}
  const ctx = cv.getContext('2d'); ctx.clearRect(0,0,cv.width,cv.height);
  const sz = Math.max(3, Math.ceil(hmView.scale * 0.9));
  hmCells.forEach(c => {{
    const sx = (c.u - hmView.cx)*hmView.scale + cv.width/2, sy = (c.v - hmView.cy)*hmView.scale + cv.height/2;
    const r = Math.log1p(c.val)/Math.log1p(maxV);
    ctx.fillStyle = r < 0.35 ? '#3b82f6' : (r < 0.7 ? '#f59e0b' : '#ef4444');
    ctx.fillRect(sx-sz/2, sy-sz/2, sz, sz);
  }});
}}
const cv = document.getElementById('wbCanvas');
cv.onclick = e => {{
  const rect = cv.getBoundingClientRect(), mx = (e.clientX-rect.left)*(cv.width/rect.width), my = (e.clientY-rect.top)*(cv.height/rect.height);
  let best = null, bd = Infinity;
  hmCells.forEach(c => {{
    const d = Math.hypot((c.u-hmView.cx)*hmView.scale+cv.width/2 - mx, (c.v-hmView.cy)*hmView.scale+cv.height/2 - my);
    if (d < Math.max(10, hmView.scale) && d < bd) {{ best = c; bd = d; }}
  }});
  if (best) document.getElementById('hmInsp').innerHTML = best.items.slice(0,4).map(x => `<b>${{x.bot}}</b> @ (${{x.blk.p.join(',')}}) dwell=${{x.blk.dw}} stuck=${{x.blk.st}} dig=${{x.blk.dg}}`).join('<br/>');
}};
hmBot.onchange = () => renderHm(true);
document.getElementById('hmProj').onchange = () => renderHm(true);
document.getElementById('hmMetric').onchange = () => renderHm(false);
document.getElementById('hmFit').onclick = () => renderHm(true);
const ctbody = document.querySelector('#codeTbl tbody');
W.codes.forEach(c => {{
  const tr = document.createElement('tr');
  tr.innerHTML = `<td><code>${{c.br}}</code> / <code>${{c.tc||'-'}}</code><br/><code>${{c.out||'-'}}</code> <code>${{c.fail||''}}</code></td><td><b>${{c.stuck}}</b></td><td>${{c.ticks}}</td>`;
  ctbody.appendChild(tr);
}});
</script>
</body>
</html>"""


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="BotSleep Full-Stack Cross-Reading Debug Workbench.")
    parser.add_argument("run_dir", help="Client run log directory (e.g. logs/v2_live9_r6b_90m)")
    parser.add_argument("--server-log", default="", help="Optional path to BotSleepDebug server_events.jsonl")
    parser.add_argument("--compare", default="", help="Optional baseline client run directory")
    parser.add_argument("--html-out", default="", help="Optional output HTML workbench path")
    parser.add_argument("--md-out", default="", help="Optional output Markdown report path")
    parser.add_argument("--top", type=int, default=20, help="Top hotspots to retain")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    heatmap = load_run_heatmaps(run_dir)
    baseline_hm = load_run_heatmaps(args.compare) if args.compare else None
    diff = diff_runs(heatmap, baseline_hm, top_n=args.top) if baseline_hm is not None else None

    server_path: Optional[Path] = Path(args.server_log) if args.server_log else None
    if server_path is None:
        auto_srv = DEFAULT_MC_PLUGINS_RUNS / run_dir.name / "server_events.jsonl"
        if auto_srv.is_file():
            server_path = auto_srv

    client = load_client_traces(run_dir)
    server = load_server_events(server_path)
    anomalies = classify_anomalies(heatmap, client, server, baseline_hm)

    md = render_workbench_markdown(heatmap, client, server, anomalies, diff=diff)
    if args.md_out:
        mp = Path(args.md_out)
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(md, encoding="utf-8")
    else:
        print(md)

    if args.html_out:
        hp = Path(args.html_out)
        hp.parent.mkdir(parents=True, exist_ok=True)
        hp.write_text(render_workbench_html(heatmap, client, server, anomalies, diff=diff), encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
