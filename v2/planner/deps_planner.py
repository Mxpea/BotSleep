"""L4 DepsPlanner + AsyncLLMReplanNode: <= 6.6KB cognitive loop plugged non-blockingly into L2 BT."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

from v2.bt.blackboard import Blackboard
from v2.bt.goal_mount import ActiveGoalMount
from v2.bt.kernel import Node, Status
from v2.domain import dist3
from v2.planner.compactor import SemanticCompactor
from v2.planner.deps_explainer import DepsExplainer
from v2.planner.horizon_selector import HorizonSelector
from v2.planner.intent_compiler import IntentCompiler
from v2.planner.llm_client import LLMResponse, OpenAIBrain
from v2.planner.model_worker import SerialModelWorker
from v2.planner.skill_retriever import BM25SkillRetriever


BASE_SYSTEM_PROMPT = (
    "You are BotSleep v2 cognitive planner. Choose ONE tool call per turn.\n"
    "Rules: never invent tools; respect PendingUnknown freeze (call wait only); "
    "prioritize PORTAL_GATE_READY EnterNether when iron_pickaxe or 4 iron_ingot ready; "
    "in competition mode score only via server /advancements."
)


class DepsPlanner:
    """Assembles <= 6,600-byte DEPS prompt payloads and compiles model decisions into L3 Tasks."""

    MAX_REQUEST_BYTES = 6600

    def __init__(
        self,
        brain: Optional[OpenAIBrain],
        compiler: IntentCompiler,
        horizon: HorizonSelector,
        *,
        retriever: Optional[BM25SkillRetriever] = None,
        explainer: Optional[DepsExplainer] = None,
        compactor: Optional[SemanticCompactor] = None,
    ) -> None:
        self.brain = brain
        self.compiler = compiler
        self.horizon = horizon
        self.retriever = retriever or BM25SkillRetriever()
        self.explainer = explainer or DepsExplainer()
        self.compactor = compactor or SemanticCompactor(keep_pairs=2)
        self._last_opp_scan_at: float = 0.0
        self._last_opp_scan_pos: Optional[Tuple[float, float, float]] = None
        self._last_opp_scan_dim: Optional[str] = None

    @staticmethod
    def _as_coord3(raw: Any) -> Optional[Tuple[int, int, int]]:
        if isinstance(raw, (list, tuple)) and len(raw) == 3:
            return (int(raw[0]), int(raw[1]), int(raw[2]))
        return None

    def _refresh_opportunity_signals(self, bb: Blackboard) -> None:
        """Throttled single-query sensor/loot scanner populating nearby_sculk_sensor and nearby_loot_chest."""
        if bb.get("pending_frozen"):
            return
        ctx = getattr(self.compiler, "ctx", None)
        gw = getattr(ctx, "gateway", None) if ctx is not None else None
        if gw is None or not hasattr(gw, "scan_blocks"):
            return

        now = time.monotonic()
        cur_pos = bb.pos
        if (
            self._last_opp_scan_pos is not None
            and bb.dimension == self._last_opp_scan_dim
            and (now - self._last_opp_scan_at) < 10.0
            and dist3(cur_pos, self._last_opp_scan_pos) < 12.0
        ):
            return

        try:
            rows = gw.scan_blocks(
                bb.dimension,
                ["sculk_sensor", "calibrated_sculk_sensor", "chest", "barrel"],
                radius=24,
                limit=12,
            )
        except Exception:
            return
        if not isinstance(rows, list):
            return

        self._last_opp_scan_at = now
        self._last_opp_scan_pos = cur_pos
        self._last_opp_scan_dim = bb.dimension

        owned: Set[Tuple[int, int, int]] = set()
        for slot in ("outpost_chest", "nether_outpost_chest"):
            c = self._as_coord3(bb.get(slot))
            if c is not None:
                owned.add(c)
        for raw in bb.get("owned_chest_coords") or ():
            c = self._as_coord3(raw)
            if c is not None:
                owned.add(c)
        db = getattr(gw, "db", None)
        if db is not None and hasattr(db, "list_landmarks"):
            for kind in ("outpost_chest", "nether_outpost_chest", "chest"):
                lm_rows = db.list_landmarks(bb.dimension, kind=kind)
                if isinstance(lm_rows, list):
                    for lm in lm_rows:
                        c = self._as_coord3(lm.get("pos") if isinstance(lm, dict) else None)
                        if c is not None:
                            owned.add(c)

        looted: Set[Tuple[int, int, int]] = set()
        now_wall = time.time()
        raw_looted = bb.get("looted_chests")
        if isinstance(raw_looted, dict):
            for key, ts in raw_looted.items():
                if not isinstance(ts, (int, float)):
                    continue
                if isinstance(key, tuple) and len(key) == 2 and isinstance(key[0], str):
                    if key[0] != bb.dimension:
                        continue
                    c = self._as_coord3(key[1])
                else:
                    c = self._as_coord3(key)
                if c is not None and (now_wall - float(ts)) < 600.0:
                    looted.add(c)
        elif isinstance(raw_looted, (list, set, tuple)):
            for item in raw_looted:
                c = self._as_coord3(item)
                if c is not None:
                    looted.add(c)

        has_sculk = False
        has_loot = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "")
            if name in {"sculk_sensor", "calibrated_sculk_sensor"}:
                has_sculk = True
            elif name in {"chest", "barrel"}:
                coord = self._as_coord3(row.get("pos"))
                if coord is not None and coord not in owned and coord not in looted:
                    has_loot = True

        bb.set("nearby_sculk_sensor", has_sculk, writer="deps_planner")
        bb.set("nearby_loot_chest", has_loot, writer="deps_planner")

    def _state_tags(self, bb: Blackboard) -> List[str]:
        tags: List[str] = [self.compiler.objective]
        inv = bb.inventory_counts
        if bb.get("pending_frozen"):
            tags.extend(["unknown", "pending", "frozen"])
        if self.horizon.is_portal_gate_ready(bb):
            tags.extend(["portal_ready", "nether", "lava", "speedrun"])
        elif inv.get("stone_pickaxe", 0) > 0:
            tags.extend(["iron", "smelt", "mining"])
        elif inv.get("wooden_pickaxe", 0) > 0:
            tags.extend(["stone", "furnace"])
        else:
            tags.extend(["opening", "wood"])
        if bb.last_failure_reason:
            tags.append(bb.last_failure_reason.split(":")[0])
        return tags

    def _compact_world_snapshot(self, bb: Blackboard) -> str:
        top_inv = sorted(bb.inventory_counts.items(), key=lambda kv: -kv[1])[:12]
        inv_str = ", ".join(f"{k}:{v}" for k, v in top_inv) or "empty"
        pos = [round(v, 1) for v in bb.pos]
        comp_str = ""
        if self.compiler.objective == "competition" and self.compiler.competition is not None:
            snap = self.compiler.competition.snapshot()
            comp_str = f" comp_score={snap['total_score']} active={snap['active_task_id']} comp_done={snap['completed_count']}"
        return (
            f"STATE: dim={bb.dimension} pos={pos} hp={bb.health:.1f} food={bb.food} ox={bb.oxygen}"
            f"{comp_str} inv=[{inv_str}]"
        )

    def build_request_payload(
        self,
        bb: Blackboard,
        *,
        needs_ai: bool = False,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Build OpenAI (messages, tools) strictly bounded to <= 6,600 serialized UTF-8 bytes."""
        self._refresh_opportunity_signals(bb)
        tools = self.compiler.tool_schemas(needs_ai=needs_ai)
        tools_bytes = len(json.dumps(tools, ensure_ascii=False).encode("utf-8"))

        tags = self._state_tags(bb)
        query = f"{bb.dimension} {bb.last_failure_reason} {' '.join(tags)}"
        kb_budget = max(400, min(1400, self.MAX_REQUEST_BYTES - tools_bytes - 2800))
        kb_text = self.retriever.format_context(query, tags=tags, top_k=3, max_bytes=kb_budget)

        sys_text = BASE_SYSTEM_PROMPT
        if kb_text:
            sys_text = f"{BASE_SYSTEM_PROMPT}\n\n{kb_text}"

        snapshot = self._compact_world_snapshot(bb)
        horizon_text = self.horizon.format_top(bb, limit=5)
        card_text = self.compactor.card.to_compact_text(max_bytes=420)
        diag_text = self.explainer.explain(bb) if (needs_ai or bb.last_failure_reason) else ""

        user_parts = [snapshot, horizon_text, card_text]
        if diag_text:
            user_parts.append(diag_text)
        user_text = "\n".join(user_parts)

        messages: List[Dict[str, Any]] = [{"role": "system", "content": sys_text}]
        messages.extend(self.compactor.history_messages())
        messages.append({"role": "user", "content": user_text})

        # Hard byte-budget enforcement (never breaks tool_call/tool_result pairing!)
        def _payload_bytes(msgs: List[Dict[str, Any]]) -> int:
            return len(json.dumps({"messages": msgs, "tools": tools}, ensure_ascii=False).encode("utf-8"))

        if _payload_bytes(messages) > self.MAX_REQUEST_BYTES:
            # First trim system KB context while preserving exact history pairs
            excess = _payload_bytes(messages) - self.MAX_REQUEST_BYTES + 64
            sys_enc = messages[0]["content"].encode("utf-8")
            keep_sys = max(len(BASE_SYSTEM_PROMPT.encode("utf-8")), len(sys_enc) - excess)
            messages[0]["content"] = sys_enc[:keep_sys].decode("utf-8", errors="ignore")

        if _payload_bytes(messages) > self.MAX_REQUEST_BYTES and len(messages) > 2:
            # Drop oldest complete (assistant, tool) pair atomically if still over budget
            messages = [messages[0], messages[-1]]

        return messages, tools

    def apply_llm_response(self, resp: LLMResponse, bb: Blackboard) -> Tuple[Optional[Any], Dict[str, Any], LLMResponse]:
        if not resp.tool_calls:
            return None, {"ok": True, "action": "wait", "content": resp.content[:120]}, resp

        tc = resp.tool_calls[0]
        task, res = self.compiler.compile_tool_call(tc.name, tc.arguments, bb)
        self.compactor.record_tool_turn(
            call_id=tc.id,
            tool_name=tc.name,
            arguments=tc.arguments,
            result_summary=res,
            succeeded=bool(res.get("ok")),
        )
        return task, res, resp

    def plan_once(self, bb: Blackboard, *, needs_ai: bool = False) -> Tuple[Optional[Any], Dict[str, Any], Optional[LLMResponse]]:
        """Execute one planning step: LLM if enabled, else deterministic HorizonSelector fallback."""
        self._refresh_opportunity_signals(bb)
        if self.brain is None:
            top = self.horizon.select_top(bb, limit=1)
            if not top:
                return None, {"ok": True, "action": "wait"}, None
            cand = top[0]
            if cand.goal == "select_task":
                task, res = self.compiler.compile_tool_call("select_task", {"task_id": cand.item}, bb)
            else:
                task, res = self.compiler.compile_tool_call("run_goal", {"goal": cand.goal, "item": cand.item, "count": cand.count}, bb)
            return task, res, None

        messages, tools = self.build_request_payload(bb, needs_ai=needs_ai)
        resp = self.brain.chat(messages, tools=tools)
        return self.apply_llm_response(resp, bb)


class AsyncLLMReplanNode(Node):
    """Non-blocking L2 BT leaf that polls SerialModelWorker and mounts compiled L3 Tasks."""

    def __init__(
        self,
        planner: DepsPlanner,
        goal_mount: ActiveGoalMount,
        worker: Optional[SerialModelWorker] = None,
    ) -> None:
        super().__init__("AsyncLLMReplanNode")
        self.planner = planner
        self.goal_mount = goal_mount
        self.worker = worker or SerialModelWorker()
        self._context_version: int = 0
        self.last_response: Optional[LLMResponse] = None
        self._error_streak: int = 0
        self._next_retry_at: float = 0.0
        self._last_replan_pos: Optional[Tuple[float, float, float]] = None
        self._last_replan_inv: Optional[Tuple[Tuple[str, int], ...]] = None
        self._same_pos_replan_streak: int = 0
        self._same_pos_cooldown_until: float = 0.0
        self._last_submit_pos: Optional[Tuple[float, float, float]] = None
        self._last_submit_inv: Optional[Tuple[Tuple[str, int], ...]] = None
        self._submit_same_pos_streak: int = 0
        self._submit_cooldown_until: float = 0.0
        self._in_flight_inv: Optional[Tuple[Tuple[str, int], ...]] = None
        self._last_stop_reason: str = ""
        self._last_discarded_info: Optional[Dict[str, Any]] = None

    def bump_context_version(self) -> None:
        self._context_version += 1

    @staticmethod
    def _inventory_signature(bb: Blackboard) -> Tuple[Tuple[str, int], ...]:
        return tuple(sorted((k, int(v)) for k, v in bb.inventory_counts.items() if v > 0))

    def _record_completed_replan(self, bb: Blackboard) -> None:
        cur_pos = bb.pos
        inv_sig = self._inventory_signature(bb)
        if (
            self._last_replan_pos is not None
            and dist3(cur_pos, self._last_replan_pos) <= 2.5
            and inv_sig == self._last_replan_inv
        ):
            self._same_pos_replan_streak += 1
            cooldown = min(20.0, 4.0 * float(2 ** (self._same_pos_replan_streak - 1)))
            self._same_pos_cooldown_until = time.monotonic() + cooldown
        else:
            self._same_pos_replan_streak = 0
            self._same_pos_cooldown_until = 0.0
        self._last_replan_pos = cur_pos
        self._last_replan_inv = inv_sig

    def update(self, bb: Blackboard) -> Status:
        if bb.get("pending_frozen"):
            return Status.RUNNING

        # 1) Check if an async model result is ready (compile & record on main BT thread!)
        ready = self.worker.poll(expected_context_version=self._context_version)
        discarded = self.worker.pop_discarded() if hasattr(self.worker, "pop_discarded") else None
        if discarded is not None:
            info = {
                "job_id": discarded.job_id,
                "submitted_ctx_ver": discarded.context_version,
                "current_ctx_ver": self._context_version,
                "stop_reason": self._last_stop_reason,
                "active_branch": bb.active_branch,
                "outcome": bb.last_action_outcome,
                "ok": discarded.ok,
                "ts_ms": int(time.time() * 1000),
            }
            bb.set("last_llm_discarded", info, writer=self.name)
            self._last_discarded_info = info
            rec = getattr(self.planner.brain, "recorder", None)
            if rec is not None:
                rec.record("WARN", "v2.llm", "LLM_DISCARDED", info)
        if ready is not None:
            self._in_flight_inv = None
            if ready.ok and isinstance(ready.value, LLMResponse):
                task, res, llm_resp = self.planner.apply_llm_response(ready.value, bb)
                self.last_response = llm_resp
                self._error_streak = 0
                self._next_retry_at = 0.0
                self._record_completed_replan(bb)
                bb.set("last_plan_result", res, writer=self.name)
                if task is not None:
                    self.goal_mount.mount(task, bb)
                    bb.last_failure_reason = ""
                    return Status.SUCCESS
            elif ready.ok and isinstance(ready.value, tuple):
                task, res, llm_resp = ready.value
                self.last_response = llm_resp
                self._error_streak = 0
                self._next_retry_at = 0.0
                self._record_completed_replan(bb)
                bb.set("last_plan_result", res, writer=self.name)
                if task is not None:
                    self.goal_mount.mount(task, bb)
                    bb.last_failure_reason = ""
                    return Status.SUCCESS
            elif ready.error is not None:
                self._error_streak += 1
                self._next_retry_at = time.monotonic() + min(30.0, float(2 ** min(5, self._error_streak)))
                bb.set("last_llm_error", str(ready.error), writer=self.name)
            return Status.RUNNING

        # 2) If worker is still thinking, return RUNNING without blocking BT safety ticks!
        if self.worker.is_busy:
            bb.last_action_outcome = "llm_thinking_async"
            return Status.RUNNING

        # 2.5) Water cooldown lock & wet-flicker hysteresis: never spam LLM replans until dry footing stabilizes
        now = time.monotonic()
        in_water = bool(bb.status.get("in_water") or bb.status.get("submerged"))
        water_lock_until = float(bb.get("water_replan_lock_until", 0.0) or 0.0)
        dry_footing_streak = int(bb.get("dry_footing_streak", 0) or 0)
        last_wet_at = float(bb.get("last_wet_at", 0.0) or 0.0)
        if (
            in_water
            or (now < water_lock_until and dry_footing_streak < 2)
            or (last_wet_at > 0.0 and (now - last_wet_at) < 10.0 and dry_footing_streak < 3)
        ):
            bb.last_action_outcome = "llm_wait_dry_footing"
            return Status.RUNNING

        # 3) Submit a new background planning job (or run deterministic HorizonSelector instantaneously)
        needs_ai = bb.goal_status in {"NEEDS_AI", "FAILED_LOCAL"} or bool(bb.get("watchdog_stalled"))
        if self.planner.brain is None:
            task, res, _ = self.planner.plan_once(bb, needs_ai=needs_ai)
            bb.set("last_plan_result", res, writer=self.name)
            if task is not None:
                self.goal_mount.mount(task, bb)
                bb.last_failure_reason = ""
                return Status.SUCCESS
            return Status.RUNNING

        if now < self._next_retry_at:
            bb.last_action_outcome = "llm_error_backoff"
            return Status.RUNNING

        cur_pos = bb.pos
        inv_sig = self._inventory_signature(bb)
        if self._last_replan_pos is not None and (
            dist3(cur_pos, self._last_replan_pos) > 2.5 or inv_sig != self._last_replan_inv
        ):
            self._same_pos_replan_streak = 0
            self._same_pos_cooldown_until = 0.0
        if self._last_submit_pos is not None and (
            dist3(cur_pos, self._last_submit_pos) > 2.5 or inv_sig != self._last_submit_inv
        ):
            self._submit_same_pos_streak = 0
            self._submit_cooldown_until = 0.0
        if now < self._same_pos_cooldown_until or now < self._submit_cooldown_until:
            bb.last_action_outcome = "llm_same_pos_cooldown"
            return Status.RUNNING

        messages, tools = self.planner.build_request_payload(bb, needs_ai=needs_ai)
        brain = self.planner.brain
        job_id = uuid.uuid4().hex[:8]
        ctx_ver = self._context_version
        if self.worker.submit(job_id, ctx_ver, lambda m=messages, t=tools, b=brain: b.chat(m, tools=t)):
            if (
                self._last_submit_pos is not None
                and dist3(cur_pos, self._last_submit_pos) <= 2.5
                and inv_sig == self._last_submit_inv
            ):
                self._submit_same_pos_streak += 1
                submit_cooldown = min(30.0, 4.0 * float(2 ** (self._submit_same_pos_streak - 1)))
                self._submit_cooldown_until = now + submit_cooldown
            else:
                self._submit_same_pos_streak = 0
                self._submit_cooldown_until = 0.0
            self._last_submit_pos = cur_pos
            self._last_submit_inv = inv_sig
            self._in_flight_inv = inv_sig
        bb.last_action_outcome = "llm_submitted_async"
        return Status.RUNNING

    def stop(self, bb: Blackboard) -> None:
        """Pure-memory stop: preserve in-flight future on brief benign recovery/unstuck when inventory unchanged."""
        inv_unchanged = self._in_flight_inv is not None and self._inventory_signature(bb) == self._in_flight_inv
        benign_preempt = (
            not bb.get("pending_frozen")
            and bb.active_branch != "HardSafetyBranch"
            and not bb.last_action_outcome.startswith("safety_")
            and bb.last_action_outcome.startswith(("recovery_", "unstuck_"))
        )
        if not (inv_unchanged and benign_preempt):
            self._context_version += 1
            self._last_stop_reason = (
                f"preempted_by:{bb.active_branch}|out:{bb.last_action_outcome}|frozen:{bool(bb.get('pending_frozen'))}"
            )
            self._in_flight_inv = None
        super().stop(bb)
