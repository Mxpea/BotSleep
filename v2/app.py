"""BotSleep v2 Composition Root: assembles L0-L4 cleanly for sleep / starter / daily / competition modes."""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Tuple

from v2.bt import BTRuntime, Status
from v2.gateway import VirtualWorldGateway
from v2.kernel import (
    APIError,
    MinecraftAgentClient,
    ModelRequestGate,
    PendingTransactionJournal,
    UnifiedWorldDB,
)
from v2.observability import ChatDebugger, DebugStateTracer, FlightRecorder, HeatmapTracker, MonitorServer
from v2.planner import (
    AsyncLLMReplanNode,
    DepsPlanner,
    HorizonSelector,
    IntentCompiler,
    OpenAIBrain,
    SerialModelWorker,
)
from v2.primitives import PrimitiveContext
from v2.tasks import TaskCatalogue
from v2.tasks.competition import CompetitionManager


_RECON_ENTITIES = frozenset({
    "villager", "wandering_trader", "sheep", "cow", "chicken", "pig", "wolf", "cat",
    "horse", "donkey", "cod", "salmon", "tropical_fish", "pufferfish", "glow_squid",
    "skeleton", "stray", "zombie", "spider", "piglin", "armadillo", "goat", "parrot", "axolotl",
})
_STATIC_LANDMARK_ENTITIES = frozenset({"villager", "wandering_trader", "glow_squid"})


def update_island_recon(bb: Any, gateway: VirtualWorldGateway, *, now: Optional[float] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Zero-HTTP passive landmark recorder tracking nearby useful mobs & cached features in bb['island_recon']."""
    ts = time.time() if now is None else float(now)
    recon: Dict[str, List[Dict[str, Any]]] = dict(bb.get("island_recon") or {})
    dim = bb.dimension
    for ent in bb.entities:
        if not isinstance(ent, dict):
            continue
        name = str(ent.get("name") or "")
        if name not in _RECON_ENTITIES:
            continue
        epos = ent.get("pos")
        if not isinstance(epos, (list, tuple)) or len(epos) != 3:
            continue
        p = [round(float(epos[0]), 1), round(float(epos[1]), 1), round(float(epos[2]), 1)]
        rows = list(recon.get(name) or [])
        deduped = False
        for row in rows:
            rp = row.get("pos")
            if isinstance(rp, list) and len(rp) == 3:
                if abs(rp[0] - p[0]) + abs(rp[1] - p[1]) + abs(rp[2] - p[2]) <= 8.0 and row.get("dim") == dim:
                    row["pos"] = p
                    row["ts"] = ts
                    deduped = True
                    break
        if not deduped:
            rows.append({"dim": dim, "pos": p, "ts": ts})
            if len(rows) > 4:
                rows = rows[-4:]
            if name in _STATIC_LANDMARK_ENTITIES and gateway.db is not None:
                gateway.db.save_landmark(dim, name, f"{name}_{int(p[0])}_{int(p[1])}_{int(p[2])}", (int(p[0]), int(p[1]), int(p[2])))
        recon[name] = rows
    bb.set("island_recon", recon, writer="island_recon")
    return recon


class CompetitionSupervisor:
    """Tracks per-task progress timeouts and emits structured competition telemetry."""

    def __init__(self, comp_mgr: CompetitionManager, *, start_mono: float, recorder: Optional[FlightRecorder] = None) -> None:
        self.comp_mgr = comp_mgr
        self.start_mono = float(start_mono)
        self.recorder = recorder
        self.active_id: Optional[str] = comp_mgr.active_task_id
        self.started_mono: float = float(start_mono)
        self.last_progress_mono: float = float(start_mono)
        self.last_inv_sig: Tuple[Tuple[str, int], ...] = ()
        self.last_completed: set[str] = set(comp_mgr.completed_ids)

    def step(self, rt: BTRuntime, client: MinecraftAgentClient, *, now_mono: Optional[float] = None) -> Optional[str]:
        now = time.monotonic() if now_mono is None else float(now_mono)
        bb = rt.bb
        cur_id = self.comp_mgr.active_task_id
        if cur_id != self.active_id:
            self.active_id = cur_id
            self.started_mono = now
            self.last_progress_mono = now
            if cur_id is not None and self.recorder:
                self.recorder.record("INFO", "v2.competition", "COMPETITION_SELECT", {
                    "task_id": cur_id,
                    "elapsed_s": round(now - self.start_mono, 1),
                    "score": self.comp_mgr.total_score,
                })

        inv_sig = tuple(sorted((k, int(v)) for k, v in bb.inventory_counts.items() if v > 0))
        prev_map = dict(self.last_inv_sig)
        if any(cnt > prev_map.get(k, 0) for k, cnt in inv_sig) or self.comp_mgr.completed_ids != self.last_completed:
            self.last_progress_mono = now
        self.last_inv_sig = inv_sig

        if self.active_id is not None and not bb.get("pending_frozen"):
            timeout_s = self.comp_mgr.get_timeout_seconds(self.active_id)
            total_elapsed = now - self.started_mono
            idle_elapsed = now - self.last_progress_mono
            if total_elapsed > timeout_s * 1.45 or idle_elapsed > timeout_s:
                timed_out_id = self.active_id
                res = self.comp_mgr.force_exit_task("task_timeout_stoploss")
                rt.goal_mount.mount(None, bb)
                bb.last_failure_reason = f"comp_timeout:{timed_out_id}"
                self.active_id = None
                if self.recorder:
                    self.recorder.record("WARN", "v2.competition", "COMPETITION_TIMEOUT", {
                        "task_id": timed_out_id,
                        "idle_s": round(idle_elapsed, 1),
                        "total_s": round(total_elapsed, 1),
                        "defer_seconds": res.get("defer_seconds"),
                    })
                return f"timeout:{timed_out_id}"
        return None

    def flush_score_delta(self, *, now_mono: Optional[float] = None) -> Optional[Tuple[str, ...]]:
        now = time.monotonic() if now_mono is None else float(now_mono)
        newly = tuple(sorted(self.comp_mgr.completed_ids - self.last_completed))
        if not newly:
            return None
        self.last_completed = set(self.comp_mgr.completed_ids)
        self.last_progress_mono = now
        if self.recorder:
            self.recorder.record("INFO", "v2.competition", "COMPETITION_SCORE", {
                **self.comp_mgr.snapshot(),
                "newly_completed": list(newly),
                "elapsed_s": round(now - self.start_mono, 1),
            })
        return newly


def _build_brain(config: Dict[str, Any], recorder: Optional[FlightRecorder], *, scope_id: str) -> Optional[OpenAIBrain]:
    llm = config["llm"]
    if not llm.get("enabled"):
        return None
    primary_gate = ModelRequestGate(
        llm["base_url"],
        llm["api_key"],
        wait_seconds=llm["gate_wait_seconds"],
        recorder=recorder,
        global_concurrency=True,
        auto_recover_uncertain=llm["recover_uncertain_transport"],
    )
    overflow_brain = None
    ov = llm.get("overflow") or {}
    if ov.get("enabled"):
        ov_gate = ModelRequestGate(
            ov["base_url"],
            ov["api_key"],
            wait_seconds=ov["timeout_seconds"],
            recorder=recorder,
            global_concurrency=False,
            auto_recover_uncertain=True,
            scope_id=scope_id,
        )
        overflow_brain = OpenAIBrain(
            ov["base_url"],
            ov["api_key"],
            ov["model"],
            timeout_seconds=ov["timeout_seconds"],
            temperature=llm["temperature"],
            max_tokens=llm["max_tokens"],
            tls_version=ov["tls_version"],
            gate=ov_gate,
            recorder=recorder,
        )
    return OpenAIBrain(
        llm["base_url"],
        llm["api_key"],
        llm["model"],
        timeout_seconds=llm["timeout_seconds"],
        temperature=llm["temperature"],
        max_tokens=llm["max_tokens"],
        tls_version=llm["tls_version"],
        gate=primary_gate,
        overflow_brain=overflow_brain,
        primary_wait_seconds=ov.get("primary_wait_seconds", 1.5),
        recorder=recorder,
    )


def run_v2_session(
    config: Dict[str, Any],
    *,
    once: bool = False,
    keep_connected: bool = False,
    resume: bool = False,
    new_competition: bool = False,
    acknowledge_model_idle: bool = False,
    debug_commands: bool = False,
    max_ticks_override: Optional[int] = None,
) -> int:
    """Execute a complete BotSleep v2 runtime session across all modes (sleep/starter/daily/competition)."""
    agent_cfg = config["agent"]
    api_cfg = config["mc_api"]
    surv_cfg = config["survival"]
    sleep_cfg = config["sleep"]
    dbg_cfg = config["debug"]

    mode = agent_cfg["mode"]
    objective = surv_cfg["objective"]
    keep_session = bool(keep_connected or api_cfg.get("keep_session_on_exit", False))
    session_file = str(api_cfg.get("session_file") or "").strip()
    if not session_file and keep_session:
        session_file = str(Path(surv_cfg["state_dir"]) / f"session_{agent_cfg['name']}.json")

    monitor: Optional[MonitorServer] = None
    recorder: Optional[FlightRecorder] = None
    heatmap: Optional[HeatmapTracker] = None
    debug_tracer: Optional[DebugStateTracer] = None
    brain: Optional[OpenAIBrain] = None
    db: Optional[UnifiedWorldDB] = None
    client: Optional[MinecraftAgentClient] = None
    login_attempted = False

    try:
        mon_cfg = dbg_cfg["monitor"]
        if mon_cfg["enabled"]:
            monitor = MonitorServer(host=mon_cfg["host"], port=mon_cfg["port"]).start()
            print(f"[v2] Live monitor: {monitor.address}")

        fr_cfg = dbg_cfg["flight_recorder"]
        overflow_key = str((config["llm"].get("overflow") or {}).get("api_key") or "")
        if fr_cfg["enabled"] or monitor is not None or mon_cfg["console"]:
            recorder = FlightRecorder(
                fr_cfg["output_dir"],
                enabled=fr_cfg["enabled"],
                secrets=tuple(s for s in (agent_cfg["password"], config["llm"]["api_key"], overflow_key) if s),
                sink=monitor.publish if monitor else None,
                console=mon_cfg["console"],
            )
            if fr_cfg["enabled"]:
                heatmap = HeatmapTracker(
                    fr_cfg["output_dir"],
                    username=str(agent_cfg["name"]),
                    enabled=True,
                )
                recorder.add_listener(heatmap.on_flight_event)
                debug_tracer = DebugStateTracer(
                    fr_cfg["output_dir"],
                    username=str(agent_cfg["name"]),
                    enabled=True,
                )
                recorder.add_listener(debug_tracer.on_flight_event)

        db = UnifiedWorldDB.for_scope(surv_cfg["state_dir"], api_cfg["base_url"], agent_cfg["name"], surv_cfg["world_id"])
        journal = PendingTransactionJournal(db)
        client = MinecraftAgentClient(
            base_url=api_cfg["base_url"],
            username=agent_cfg["name"],
            password=agent_cfg["password"],
            timeout_seconds=api_cfg["timeout_seconds"],
            recorder=recorder,
        )

        with journal.instance_lock():
            if resume and journal.has_pending_unknown():
                # Explicit --resume still refuses to replay an unknown write; only logs clearly!
                print("[v2] Pending unknown transaction active; PendingUnknownGuard will hold writes safely.")

            brain = _build_brain(config, recorder, scope_id=db.scope_id)
            if brain is not None and acknowledge_model_idle:
                brain.gate.acknowledge_provider_idle()

            if recorder:
                recorder.record("INFO", "v2.app", "STARTUP", {
                    "username": client.username,
                    "mode": mode,
                    "objective": objective if mode == "survival" else None,
                    "llm_enabled": brain is not None,
                    "model": config["llm"]["model"] if brain is not None else None,
                })

            login_attempted = True
            if session_file:
                status = client.attach_or_login(session_file=session_file, max_wait_seconds=api_cfg["login_timeout_seconds"])
            else:
                status = client.login(max_wait_seconds=api_cfg["login_timeout_seconds"])
            if recorder:
                recorder.add_secret(client.token)
                recorder.record("INFO", "v2.app", "CONNECTED", {"pos": status.get("pos"), "dim": status.get("dimension")})
            print(f"[v2] Bot '{client.username}' ready at {status.get('pos')} ({status.get('dimension')}).")

            gateway = VirtualWorldGateway(client, db=db)
            ctx = PrimitiveContext(gateway, journal)
            catalogue = TaskCatalogue()
            comp_mgr: Optional[CompetitionManager] = None
            if mode == "survival" and objective == "competition":
                comp_mgr = CompetitionManager(
                    catalogue,
                    eligibility_overrides=surv_cfg["competition"]["eligibility_overrides"],
                    db=db,
                    defer_seconds=surv_cfg["competition"]["defer_seconds"],
                    reset=new_competition,
                )
                comp_mgr.sync_server_advancements(client)

            rt = BTRuntime(
                gateway,
                journal,
                mode=mode,
                threat_radius=surv_cfg["threat_radius"],
                food_threshold=surv_cfg["food_threshold"],
                enable_auto_sleep=not surv_cfg.get("disable_sleep", False),
                sleep_radius=sleep_cfg["radius"],
            )

            replan_node: Optional[AsyncLLMReplanNode] = None
            if mode == "survival":
                compiler = IntentCompiler(ctx, catalogue, objective=objective, competition=comp_mgr)
                horizon = HorizonSelector(catalogue, objective=objective, intent=surv_cfg["intent"], competition=comp_mgr)
                planner = DepsPlanner(brain, compiler, horizon)
                replan_node = AsyncLLMReplanNode(planner, rt.goal_mount, worker=SerialModelWorker())
                rt.selector.children.append(replan_node)

            chat_dbg = None
            if (debug_commands or dbg_cfg["enable_in_game_commands"]) and objective != "competition":
                chat_dbg = ChatDebugger(
                    prefix=dbg_cfg["command_prefix"],
                    allowed_usernames=dbg_cfg["allowed_usernames"],
                )

            max_ticks = 1 if once else (
                max_ticks_override
                if max_ticks_override is not None
                else (surv_cfg["competition"]["max_steps"] if objective == "competition" else surv_cfg["max_steps"])
            )
            poll_s = float(sleep_cfg["poll_interval_seconds"] if mode == "sleep" else surv_cfg["poll_interval_seconds"])
            deadline = time.monotonic() + float(
                sleep_cfg["timeout_seconds"] if mode == "sleep"
                else (surv_cfg["competition"]["max_seconds"] if objective == "competition" else surv_cfg["max_runtime_seconds"])
            )

            last_logged_resp = None
            last_logged_err: Any = None
            comp_sup = (
                CompetitionSupervisor(comp_mgr, start_mono=time.monotonic(), recorder=recorder)
                if comp_mgr is not None
                else None
            )
            last_dim: Optional[str] = status.get("dimension")
            reconnect_attempts = 0
            transient_tick_errors = 0

            for tick_idx in range(max_ticks):
                now_loop = time.monotonic()
                if now_loop >= deadline or rt.bb.stop_requested:
                    break
                rt.bb.set("competition_remaining_seconds", max(0.0, deadline - now_loop), writer="app")
                tick_t0 = time.perf_counter()
                try:
                    tick_status = rt.tick_once()
                    transient_tick_errors = 0
                except APIError as err:
                    if (
                        err.code in {"not_ready", "wrong_server", "dead", "backend_unavailable", "request_timeout"}
                        and err.details.get("action_state") != "unknown"
                        and not once
                        and transient_tick_errors < 20
                        and (time.monotonic() + 3.0) < deadline
                    ):
                        transient_tick_errors += 1
                        if recorder:
                            recorder.record("WARN", "v2.app", "TRANSIENT_TICK_RETRY", {
                                "code": err.code,
                                "message": err.message,
                                "attempt": transient_tick_errors,
                            })
                        time.sleep(1.5)
                        continue
                    if (
                        err.code == "not_connected"
                        and not once
                        and reconnect_attempts < 5
                        and (time.monotonic() + 10.0) < deadline
                    ):
                        reconnect_attempts += 1
                        time.sleep(4.0)
                        client.login(max_wait_seconds=api_cfg["login_timeout_seconds"])
                        rt.observe.consecutive_read_errors = 0
                        transient_tick_errors = 0
                        if recorder:
                            recorder.record("WARN", "v2.app", "RECONNECTED", {"attempt": reconnect_attempts})
                        continue
                    raise
                tick_elapsed_ms = (time.perf_counter() - tick_t0) * 1000.0
                if heatmap is not None:
                    heatmap.record_tick(
                        tick_idx,
                        rt.bb,
                        rt.goal_mount.goal,
                        tick_elapsed_ms,
                    )
                if debug_tracer is not None:
                    debug_tracer.record_tick(
                        tick_idx,
                        rt.bb,
                        rt.goal_mount.goal,
                        tick_elapsed_ms,
                    )
                if chat_dbg is not None:
                    chat_dbg.process_events(rt.bb)

                if tick_idx % 10 == 0:
                    update_island_recon(rt.bb, gateway)

                if comp_mgr is not None and comp_sup is not None:
                    comp_sup.step(rt, client)
                    if rt.bb.goal_status in {"COMPLETED", "NEEDS_AI", "FAILED_LOCAL"} or tick_idx % 20 == 19:
                        was_active = comp_mgr.active_task_id
                        try:
                            comp_mgr.sync_server_advancements(client)
                        except APIError:
                            pass
                        if was_active is not None and comp_mgr.active_task_id is None:
                            # Server already confirmed the active advancement -> immediately unmount & pivot!
                            rt.goal_mount.mount(None, rt.bb)
                        elif rt.bb.goal_status in {"COMPLETED", "NEEDS_AI", "FAILED_LOCAL"} and comp_mgr.active_task_id is not None:
                            # Task exited without earning its server advancement -> defer with exponential backoff so bot pivots cleanly!
                            comp_mgr.force_exit_task(f"goal_{rt.bb.goal_status.lower()}:{str(rt.bb.last_failure_reason)[:48]}")
                        newly = comp_sup.flush_score_delta()
                        if newly:
                            print(
                                f"[v2] [{client.username}] score={comp_mgr.total_score} +{list(newly)} done={len(comp_mgr.completed_ids)} active={comp_mgr.active_task_id}",
                                flush=True,
                            )

                if replan_node is not None:
                    resp = replan_node.last_response
                    if resp is not None and resp is not last_logged_resp:
                        last_logged_resp = resp
                        plan_res = rt.bb.get("last_plan_result")
                        if recorder:
                            recorder.record("INFO", "v2.llm", "LLM_RESPONSE", {
                                "route": resp.route,
                                "requested_model": resp.requested_model,
                                "confirmed_model": resp.confirmed_model,
                                "latency_ms": resp.latency_ms,
                                "prompt_tokens": resp.prompt_tokens,
                                "completion_tokens": resp.completion_tokens,
                                "tool_calls": [tc.name for tc in resp.tool_calls],
                                "plan_result": plan_res,
                            })
                        print(
                            f"[v2] [{client.username}] LLM({resp.route}/{resp.confirmed_model}) {resp.latency_ms}ms "
                            f"pt={resp.prompt_tokens} ct={resp.completion_tokens} tools={[tc.name for tc in resp.tool_calls]}",
                            flush=True,
                        )
                    cur_err = rt.bb.get("last_llm_error")
                    if cur_err and cur_err != last_logged_err:
                        last_logged_err = cur_err
                        if recorder:
                            recorder.record("WARN", "v2.llm", "LLM_ERROR", {"error": str(cur_err)})
                        print(f"[v2] [{client.username}] LLM_ERROR: {cur_err}", flush=True)

                if tick_idx % 20 == 0 or rt.bb.dimension != last_dim or rt.bb.goal_status == "COMPLETED":
                    last_dim = rt.bb.dimension
                    summary = {
                        "tick": tick_idx,
                        "pos": [round(float(v), 1) for v in rt.bb.pos],
                        "dim": rt.bb.dimension,
                        "hp": round(float(rt.bb.health), 1),
                        "food": int(rt.bb.food),
                        "branch": rt.bb.active_branch,
                        "goal": rt.bb.active_goal_name,
                        "goal_status": rt.bb.goal_status,
                        "out": rt.bb.last_action_outcome,
                        "fail": rt.bb.last_failure_reason,
                        "cache_hit_ratio": round(gateway.cache_hit_ratio, 3),
                        "primary_calls": brain.primary_calls if brain else 0,
                        "overflow_calls": brain.overflow_calls if brain else 0,
                        "prompt_tokens": brain.total_prompt_tokens if brain else 0,
                        "completion_tokens": brain.total_completion_tokens if brain else 0,
                        "comp_score": comp_mgr.total_score if comp_mgr else 0,
                    }
                    if recorder:
                        recorder.record("INFO", "v2.app", "TICK_SUMMARY", summary)
                    print(
                        f"[v2] [{client.username}] t={tick_idx} pos={summary['pos']} dim={summary['dim']} hp={summary['hp']} "
                        f"goal={summary['goal']}:{summary['goal_status']} out={summary['out']} score={summary['comp_score']}",
                        flush=True,
                    )

                if monitor is not None:
                    monitor.update_snapshot(
                        bt=rt.bb.snapshot(),
                        gateway={
                            "cache_hit_ratio": round(gateway.cache_hit_ratio, 3),
                            "cache_hits": gateway.cache_hits,
                            "cache_misses": gateway.cache_misses,
                        },
                        llm={
                            "confirmed_model": (
                                replan_node.last_response.confirmed_model
                                if (replan_node and replan_node.last_response)
                                else "未确认"
                            ),
                            "primary_calls": brain.primary_calls if brain else 0,
                            "overflow_calls": brain.overflow_calls if brain else 0,
                            "prompt_tokens": brain.total_prompt_tokens if brain else 0,
                            "completion_tokens": brain.total_completion_tokens if brain else 0,
                            "ratio": round(brain.prompt_completion_ratio, 2) if brain else 0.0,
                        },
                        pending_frozen=bool(rt.bb.get("pending_frozen")),
                        competition=comp_mgr.snapshot() if comp_mgr else None,
                    )
                if once:
                    break
                if mode == "sleep" and rt.bb.status.get("is_sleeping"):
                    break
                if comp_mgr is not None and comp_mgr.ended:
                    break
                if poll_s > 0 and tick_status == Status.RUNNING:
                    if rt.bb.get("fast_tick_once"):
                        time.sleep(0.18)
                    elif rt.bb.writes_this_tick > 0 and rt.bb.last_action_outcome not in {
                        "smelt_waiting_furnace",
                        "nether_portal_waiting",
                    } and not str(rt.bb.last_action_outcome).endswith(":not_ready"):
                        time.sleep(min(poll_s, 0.25))
                    else:
                        time.sleep(min(poll_s, 1.0))

        return 0
    except APIError as err:
        print(f"[v2] API error ({err.code}): {err.message}")
        return 1
    finally:
        if client is not None:
            if login_attempted and client.token and not keep_session:
                try:
                    client.logout()
                except Exception:
                    pass
            client.close()
        if brain is not None:
            brain.close()
        if monitor is not None:
            monitor.stop()
        if heatmap is not None:
            heatmap.flush()
        if db is not None:
            db.close()
