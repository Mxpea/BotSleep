"""BotSleep v2 Composition Root: assembles L0-L4 cleanly for sleep / starter / daily / competition modes."""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Dict, Optional

from v2.bt import BTRuntime, Status
from v2.gateway import VirtualWorldGateway
from v2.kernel import (
    APIError,
    MinecraftAgentClient,
    ModelRequestGate,
    PendingTransactionJournal,
    UnifiedWorldDB,
)
from v2.observability import ChatDebugger, FlightRecorder, MonitorServer
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
            last_comp_key: Optional[tuple] = comp_mgr.snapshot() and (comp_mgr.total_score, len(comp_mgr.completed_ids)) if comp_mgr is not None else None
            last_dim: Optional[str] = status.get("dimension")

            for tick_idx in range(max_ticks):
                if time.monotonic() >= deadline or rt.bb.stop_requested:
                    break
                tick_status = rt.tick_once()
                if chat_dbg is not None:
                    chat_dbg.process_events(rt.bb)

                if comp_mgr is not None and (rt.bb.goal_status == "COMPLETED" or tick_idx % 20 == 19):
                    try:
                        comp_mgr.sync_server_advancements(client)
                    except APIError:
                        pass
                    if rt.bb.goal_status == "COMPLETED" and comp_mgr.active_task_id is not None:
                        # Subgoal completed on client without earning its server advancement (e.g. shield craft before block): defer cleanly so bot advances!
                        comp_mgr.force_exit_task("subgoal_completed_without_server_advancement")
                    comp_key = (comp_mgr.total_score, len(comp_mgr.completed_ids))
                    if comp_key != last_comp_key:
                        last_comp_key = comp_key
                        if recorder:
                            recorder.record("INFO", "v2.competition", "COMPETITION_SCORE", comp_mgr.snapshot())
                        print(
                            f"[v2] [{client.username}] score={comp_mgr.total_score} done={len(comp_mgr.completed_ids)} active={comp_mgr.active_task_id}",
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
        if db is not None:
            db.close()
