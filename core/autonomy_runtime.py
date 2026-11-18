"""Harness integration for goal-level tasks and one deferred native tool result.

The controller chooses from fresh observations; this layer never stores a queue
of game writes. Each child uses the ordinary harness journal and skill checks.
"""

import copy
import math
from dataclasses import asdict
from pathlib import Path

from agent.brain import LLMError
from agent.survival_planner import Decision, LOG_NAMES, PLANK_NAMES
from skills import SkillResult


class AutonomyRuntime:
    def _init_autonomy(self):
        self.task_controller = None
        self._task_model_decision = None
        self._task_child = False
        self._task_safety = False
        self._task_resume_requested = False
        self._task_event_signature = None
        if self.daily and self.settings.get("autonomy", {}).get("enabled", True):
            from core.autonomous_tasks import AutonomousTaskController
            self.task_controller = AutonomousTaskController(
                self.settings, saved=self.context.get("autonomous_task"))
            if self.task_controller.snapshot().get("status") == "unknown":
                self._paused = True
                self.pause_reason = "task_outcome_requires_resume"
        self.context.setdefault("autonomy_stats", {"model_planning_rounds": 0, "model_calls_avoided": 0})

    def _task_snapshot(self):
        if self.task_controller is None:
            return None
        return {**self.task_controller.snapshot(), **self.context.get("autonomy_stats", {})}

    def _task_active(self):
        return (self.task_controller is not None
                and self.task_controller.snapshot().get("status") in {"running", "suspended"})

    def _save_task(self, event=None):
        if self.task_controller is None:
            return
        self.context["autonomous_task"] = self.task_controller.state()
        self.context["task_execution"] = self._task_snapshot()
        self._checkpoint(context=self.context)
        if event:
            self._record("WARN" if event in {"TASK_NEEDS_AI", "TASK_UNKNOWN"} else "INFO",
                         event, {"task_execution": self._task_snapshot()})

    def _task_start(self, decision, observation):
        if self.task_controller is None:
            return SkillResult(False, "Goal execution is disabled", {"executed": False}, "unsupported_skill")
        if self._task_active() or self.journal.state.get("pending"):
            return SkillResult(False, "Resolve the existing task or action before accepting another goal",
                               {"executed": False}, "task_conflict")
        try:
            cautious = (self.context.get("survival_constraints") or {}).get("scenario") == "cautious_bootstrap"
            self.context["cautious_bootstrap"] = cautious
            if cautious:
                self.context["cautious_bootstrap_basis"] = copy.deepcopy(observation["status"])
            self.task_controller.start(decision.args, observation, self.context)
        except ValueError as error:
            return SkillResult(False, str(error), {"executed": False}, "bad_args")
        if self._model_feedback:
            # Keep the original tool_call_id open until the goal ends. Children
            # are not fabricated native tool calls and never trigger the model.
            self._task_model_decision = copy.deepcopy(decision)
            self._model_feedback = False
        self._save_task("TASK_STARTED")
        return SkillResult(True, "Goal accepted; local substeps will run without model calls", {
            "accepted": True, "completed": False, "executed": False,
            "task_id": self._task_snapshot().get("task_id")})

    def _remote_log_waypoint(self, observation):
        blocks = observation.get("blocks") or []
        if any(row.get("name") in LOG_NAMES for row in blocks if isinstance(row, dict)):
            return None
        status = observation.get("status") if isinstance(observation, dict) else {}
        origin = status.get("pos") if isinstance(status, dict) else None
        if not isinstance(origin, (list, tuple)) or len(origin) != 3:
            return None
        try:
            rows = self.client.get_blocks(radius=128, types=sorted(LOG_NAMES), limit=8)
        except Exception:
            return None
        if not isinstance(rows, list) or not rows:
            return None
        best = min(
            (r.get("pos") for r in rows if isinstance(r, dict) and isinstance(r.get("pos"), list) and len(r["pos"]) == 3),
            key=lambda p: math.dist(origin, p),
            default=None,
        )
        if best is None:
            return None
        dist = math.dist(origin, best)
        if dist <= 24.0:
            return None
        if dist <= 26.0:
            return [float(best[0]), float(origin[1]), float(best[2])]
        scale = 24.0 / dist
        return [
            round(float(origin[0]) + (float(best[0]) - float(origin[0])) * scale, 1),
            float(origin[1]),
            round(float(origin[2]) + (float(best[2]) - float(origin[2])) * scale, 1),
        ]

    _MINING_CLUTTER_KEEP = (
        ("granite", 0), ("diorite", 0), ("andesite", 0), ("cobbled_deepslate", 0),
        ("tuff", 0), ("gravel", 8), ("flint", 4), ("leaf_litter", 0),
        ("red_mushroom", 0), ("brown_mushroom", 0), ("rotten_flesh", 0),
        ("raw_copper", 0), ("sand", 0), ("dirt", 0), ("cobblestone", 64),
    )

    def _discard_mining_clutter(self, status_pos=None):
        drop_fn = getattr(self.client, "drop", None)
        if not callable(drop_fn):
            return
        try:
            inv_raw = self.client.inventory()
            items = (inv_raw.get("result") if isinstance(inv_raw, dict) and isinstance(inv_raw.get("result"), dict) else inv_raw).get("items", [])
        except Exception:
            return
        counts = {}
        for row in items:
            if isinstance(row, dict) and isinstance(row.get("name"), str) and isinstance(row.get("count"), int):
                counts[row["name"]] = counts.get(row["name"], 0) + int(row["count"])
        from core.mining_targets import estimate_empty_main_slots
        if len(items) < 22 and estimate_empty_main_slots(items) > 12:
            return
        # Turn slightly away from digging face if look_at is available so tossed clutter lands clear.
        look_fn = getattr(self.client, "look_at", None)
        if callable(look_fn) and isinstance(status_pos, (list, tuple)) and len(status_pos) >= 3:
            try:
                look_fn(float(status_pos[0]) - 3.0, float(status_pos[1]) + 1.5, float(status_pos[2]) - 3.0)
            except Exception:
                pass
        for name, keep in self._MINING_CLUTTER_KEEP:
            if name == "dirt" and counts.get("cobblestone", 0) < 32:
                continue
            excess = int(counts.get(name, 0)) - int(keep)
            if excess > 0:
                try:
                    drop_fn(name, excess)
                except Exception:
                    break

    def _run_task_deep_mine(self, decision, observation):
        from core.memory import MemoryEngine
        from skills.mining import MineShaftSkill

        spec = (self.task_controller.state().get("pending_deep_mine") if self.task_controller else None) or {}
        resource = str(spec.get("resource") or "raw_iron")
        count = max(1, min(32, int(spec.get("count", 4))))
        if resource == "diamond":
            targets = {"diamond": {"count": count, "min_tool": "iron_pickaxe"}}
        elif resource == "coal":
            targets = {"coal": {"count": count, "min_tool": "wooden_pickaxe"}}
        else:
            targets = {
                "iron": {"count": max(count, 12), "min_tool": "stone_pickaxe"},
            }
        # Never abort a resource expedition solely because the player lacks torches or scaffold blocks yet.
        deep_home_thresholds = {
            "min_torches": 0,
            "min_blocks": 0,
            "min_empty_slots": 0,
        }
        status_pos = (observation.get("status") or {}).get("pos") if isinstance(observation, dict) else None
        self._discard_mining_clutter(status_pos=status_pos)

        remaining = 1200.0 if resource == "diamond" else 660.0 if resource == "raw_iron" else 540.0
        if hasattr(self, "_remaining_budget"):
            try:
                remaining = max(60.0, float(self._remaining_budget().get("remaining_seconds", remaining)) - 30.0)
            except Exception:
                pass
        total_t = min(1140.0 if resource == "diamond" else 640.0 if resource == "raw_iron" else 540.0, max(90.0, remaining))
        band_t = min(960.0 if resource == "diamond" else 560.0 if resource == "raw_iron" else 460.0, max(75.0, total_t * 0.85))

        state_root = Path(str(self.settings.get("state_dir") or "runtime/survival"))
        memory = MemoryEngine(str(state_root / "deep_mine_memory"))
        miner = MineShaftSkill(self.client, memory=memory)
        if resource == "cobblestone":
            pos = ((observation.get("status") or {}).get("pos") if isinstance(observation, dict) else None) or [0, 64, 0]
            start_surface = [int(math.floor(float(pos[0]))), int(math.floor(float(pos[1]))), int(math.floor(float(pos[2])))]
            # Reach real stone beneath topsoil hills while staying shallow enough for wooden pickaxes and quick return.
            target_y = max(36, start_surface[1] - 11)
            counts_fn = getattr(self.client, "get_inventory_counts", None)
            before_c = dict((counts_fn() if callable(counts_fn) else getattr(self.client, "counts", None)) or {})
            res = miner.build_mine_shaft(
                origin=None,
                radius=24,
                branch_length=4,
                place_workstation=False,
                return_to_surface=True,
                target_y=target_y,
                mining_mode="single",
                rib_length=3,
                max_trunk_steps=6,
                home_thresholds=deep_home_thresholds,
                cancel_event=getattr(self, "_cancel", None),
                timeout_seconds=210.0,
                allow_damage=True,
            )
            data = dict(res.data or {}) if isinstance(res.data, dict) else {}
            data.setdefault("executed", True)
            if data.get("action_state") == "unknown" or res.error_code == "action_unconfirmed":
                return SkillResult(False, str(res.message), data, res.error_code)
            retreat_trail = ((data.get("mining_progress") or {}).get("deep_trail")) or []
            surface_anchor = data.get("surface_entry") or data.get("origin") or start_surface
            surface_chk = miner._ensure_returned_to_surface(
                surface_anchor,
                retreat_trail=retreat_trail,
                cancel_event=getattr(self, "_cancel", None),
            )
            data["surface_return"] = surface_chk
            after_c = dict((counts_fn() if callable(counts_fn) else getattr(self.client, "counts", None)) or {})
            gained_cobble = max(0, int(after_c.get("cobblestone", 0)) - int(before_c.get("cobblestone", 0)))
            if gained_cobble > 0:
                data.setdefault("gained", {})["cobblestone"] = gained_cobble
            ok = bool(gained_cobble > 0 and surface_chk.get("on_surface", True))
            err = res.error_code if not ok else None
            if not ok and err is None and gained_cobble <= 0:
                err = "no_cobblestone_found"
            return SkillResult(ok, str(res.message), data, err)

        res = miner.mine_targets_from_surface(
            targets=targets,
            cave_policy={"explore_timeout_seconds": 35.0},
            home_thresholds=deep_home_thresholds,
            prefer_caves_first=(resource == "coal"),
            cave_search_radius=20,
            max_caves=1,
            place_workstation=False,
            rib_length=12 if resource in {"diamond", "raw_iron"} else 6,
            rib_spacing=3 if resource in {"diamond", "raw_iron"} else 4,
            max_trunk_steps=36 if resource == "diamond" else 28 if resource == "raw_iron" else 10,
            band_timeout_seconds=band_t,
            total_timeout_seconds=total_t,
            cancel_event=getattr(self, "_cancel", None),
            allow_damage=True,
        )
        data = dict(res.data or {}) if isinstance(res.data, dict) else {}
        data.setdefault("executed", True)
        return SkillResult(bool(res.success), str(res.message), data, res.error_code)

    def _run_task_scout(self, decision, observation):
        from core.memory import MemoryEngine
        from skills.scouting import SurfaceScoutSkill

        state_root = Path(str(self.settings.get("state_dir") or "runtime/survival"))
        memory = MemoryEngine(str(state_root / "scout_memory"))
        counts = (observation.get("counts") or {}) if isinstance(observation, dict) else {}
        prefer_wood = sum(counts.get(n, 0) for n in (*LOG_NAMES, *PLANK_NAMES)) == 0
        raw_r = decision.args.get("scan_radius", 48) if isinstance(decision.args, dict) else 48
        scan_r = max(48, min(64, int(raw_r))) if isinstance(raw_r, int) and raw_r >= 32 else 48
        res = SurfaceScoutSkill(self.client, memory=memory).execute(
            scan_radius=scan_r,
            hop_distance=55.0,
            max_hops=3,
            prefer_wood=prefer_wood,
            cancel_event=getattr(self, "_cancel", None),
        )
        data = dict(res.data or {}) if isinstance(res.data, dict) else {}
        if res.success:
            self.context["last_scout_report"] = copy.deepcopy(data)
        return SkillResult(bool(res.success), str(res.message), data, res.error_code)

    def _run_task_equip_loadout(self, decision, observation):
        from skills.equipment import EquipmentSkill

        prefer_offhand = (decision.args.get("prefer_offhand", "shield") if isinstance(decision.args, dict) else "shield")
        res = EquipmentSkill(self.client).execute(
            "auto_equip_best",
            prefer_offhand=prefer_offhand,
            cancel_event=getattr(self, "_cancel", None),
        )
        data = dict(res.data or {}) if isinstance(res.data, dict) else {}
        return SkillResult(bool(res.success), str(res.message), data, res.error_code)

    def _run_task_nether_portal(self, decision, observation):
        from skills.portal_speedrun import NetherPortalSpeedrunSkill

        skill = NetherPortalSpeedrunSkill(self.client)
        res = skill.build_portal(
            timeout_seconds=210.0,
            allow_damage=True,
            cancel_event=getattr(self, "_cancel", None),
        )
        data = dict(res.data or {}) if isinstance(res.data, dict) else {}
        if not res.success and res.error_code == "no_portal_site_found":
            # Scan wider radius (up to 64 blocks) for lava pools, approach once, and retry cleanly.
            find_fn = getattr(self.client, "find_blocks", None) or getattr(self.client, "get_blocks", None)
            origin = ((observation.get("status") or {}).get("pos") if isinstance(observation, dict) else None)
            if callable(find_fn) and isinstance(origin, (list, tuple)) and len(origin) == 3:
                try:
                    rows = find_fn(types=["lava"], radius=64, limit=32) or []
                    pts = [
                        row.get("pos")
                        for row in rows
                        if isinstance(row, dict) and isinstance(row.get("pos"), (list, tuple)) and len(row.get("pos")) == 3
                    ]
                    if pts:
                        nearest = min(pts, key=lambda p: math.dist(origin, p))
                        if math.dist(origin, nearest) > 12.0:
                            from skills.survival import SurvivalSkills
                            mv = SurvivalSkills(self.client).execute(
                                "move_to",
                                {"pos": [int(nearest[0]), int(nearest[1]) + 1, int(nearest[2])], "range": 5},
                                cancel_event=getattr(self, "_cancel", None),
                            )
                            if (isinstance(mv.data, dict) and mv.data.get("action_state") == "unknown") or mv.error_code == "action_unconfirmed":
                                return mv
                            res = skill.build_portal(
                                timeout_seconds=180.0,
                                allow_damage=True,
                                cancel_event=getattr(self, "_cancel", None),
                            )
                            data = dict(res.data or {}) if isinstance(res.data, dict) else {}
                except Exception:
                    pass
        data["portal_skill"] = True
        data.setdefault("executed", bool(res.success or data.get("writes", 0) > 0))
        if res.success:
            data.setdefault("portal_active", True)
        return SkillResult(bool(res.success), str(res.message), data, res.error_code)

    def _task_next(self, observation):
        if not self._task_active():
            return None
        task_ctx = dict(self.context)
        task_ctx["deep_mining_enabled"] = not bool(self.settings.get("disable_deep_mining", False))
        remote_log = self._remote_log_waypoint(observation)
        if remote_log is not None:
            task_ctx["remote_log_target"] = remote_log
        decision = self.task_controller.next_action(observation, task_ctx)
        if decision is None:
            status = self._task_snapshot().get("status")
            event = {"completed": "TASK_COMPLETED", "needs_ai": "TASK_NEEDS_AI",
                     "unknown": "TASK_UNKNOWN"}.get(status, "TASK_PROGRESS")
            self._save_task(event)
            self._finish_task_feedback(observation)
            return Decision("wait", {"seconds": .25},
                            "Goal boundary reached; next planning uses the recorded task report", "autonomous_task")
        self._task_child = True
        stats = self.context["autonomy_stats"]
        stats["model_calls_avoided"] = stats.get("model_calls_avoided", 0) + 1
        self._save_task("TASK_PROGRESS")
        return decision

    def _task_interrupt(self, reason):
        if not self._task_active():
            return
        self._task_safety = True
        before = self.task_controller.snapshot()
        self.task_controller.interrupt(reason)
        # Repeated night waits need not publish an identical interruption.
        after = self.task_controller.snapshot()
        self._save_task("TASK_SUSPENDED" if before.get("status") != after.get("status")
                        or before.get("reason") != after.get("reason") else None)

    def _task_result(self, decision, result, observation, *, uncertain=False, fatal=False):
        """Return whether a known task failure uses bounded task recovery."""
        child, self._task_child = self._task_child, False
        safety, self._task_safety = self._task_safety, False
        if self.task_controller is None:
            return False
        if child:
            self.task_controller.record_result(decision, result, observation, context=self.context)
            if not uncertain and not fatal:
                self.task_controller.confirm_completion(observation, self.context)
        if uncertain and (child or safety or self._task_active()):
            self.task_controller.stop(result.error_code or "unknown_action", unknown=True)
            result.data.setdefault("cleanup_skipped", "task_unknown")
        elif fatal and (child or self._task_active()):
            self.task_controller.stop(result.error_code or "fatal_action")
        if child or safety or uncertain or fatal:
            status = self.task_controller.snapshot().get("status")
            event = {"completed": "TASK_COMPLETED", "needs_ai": "TASK_NEEDS_AI",
                     "unknown": "TASK_UNKNOWN", "suspended": "TASK_SUSPENDED"}.get(status, "TASK_PROGRESS")
            self._save_task(event)
        self._finish_task_feedback(observation)
        return bool(child and not uncertain and not fatal and self.task_controller.snapshot().get("status") in {
            "running", "suspended", "completed", "needs_ai"})

    def _finish_task_feedback(self, observation=None, *, paused=False):
        if self._task_model_decision is None or self.task_controller is None:
            return
        snapshot = self._task_snapshot()
        status = snapshot.get("status")
        if status not in {"completed", "needs_ai", "unknown", "cancelled"} and not paused:
            return
        data = {"task_execution": snapshot, "completed": status == "completed",
                "executed": bool(snapshot.get("steps", 0)), "partial": status != "completed"}
        if snapshot.get("task") == "scout" and isinstance(self.context.get("last_scout_report"), dict):
            data["scout_report"] = copy.deepcopy(self.context["last_scout_report"])
        code = None if status == "completed" else "action_unconfirmed" if status == "unknown" else "task_paused" if paused else "task_needs_ai"
        if status == "unknown":
            data.update(action_state="unknown", cleanup_skipped="task_unknown")
        result = SkillResult(status == "completed", snapshot.get("reason") or
                             ("Goal verified from current observations" if status == "completed"
                              else "Local goal execution needs review"), data, code)
        decision, self._task_model_decision = self._task_model_decision, None
        try:
            self.planner.observe_result(decision, result, observation or {})
        except LLMError as error:
            self._paused = True
            self.pause_reason = error.code
            self._record("ERROR", "LLM_FEEDBACK_FAILED", {"code": error.code, "message": str(error)})
        self._record("INFO", "TASK_FEEDBACK", {"decision": asdict(decision), "result": asdict(result)})

    def _task_reconcile(self, observation):
        """An explicit resume abandons an uncertain child, never repeats it."""
        if not self._task_resume_requested or self.task_controller is None:
            return None
        if observation.get("fresh") is not True or observation.get("observation_scope") == "safety":
            return None
        status = observation.get("status", {})
        if status.get("ready") is not True or status.get("alive") is not True or status.get("busy"):
            return None
        state = self._task_snapshot().get("status")
        if state == "unknown" or self.journal.state.get("pending") and state in {"running", "suspended"}:
            if not self.task_controller.abandon_after_reobserve(observation):
                return None
            self._task_resume_requested = False
            self._save_task("TASK_NEEDS_AI")
            self._finish_task_feedback(observation)
            return SkillResult(True, "Fresh state observed; interrupted goal requires a new plan", {
                "executed": False, "reobserved": True, "task_abandoned": True})
        self._task_resume_requested = False
        return None

    def _task_end_at_boundary(self, reason):
        """Called only with no in-flight child; unknown evidence stays unknown."""
        if self._task_active():
            complete = (reason == "global_budget_exhausted"
                        and self.task_controller.confirm_completion(self.last_observation, self.context))
            if not complete:
                self.task_controller.stop(reason)
            self._save_task("TASK_COMPLETED" if complete else "TASK_NEEDS_AI")
        self._finish_task_feedback(self.last_observation)

    def _local_task_food(self, observation):
        """Choose a currently proven emergency food action without a model."""
        if not self._task_active() or not self.daily_policy:
            return None
        constraints = self.daily_policy.constraints(observation, context=self.context)
        if constraints.get("scenario") not in {"low_health", "critical_hunger"}:
            return None
        options = self.daily_policy._food_options(observation,
            low_health=constraints["scenario"] == "low_health", task_board=self.daily_board)
        for decision in options:
            if self.daily_policy.validate(decision, observation, context=self.context) is None:
                return decision
        return None
