"""Harness integration for goal-level tasks and one deferred native tool result.

The controller chooses from fresh observations; this layer never stores a queue
of game writes. Each child uses the ordinary harness journal and skill checks.
"""

import copy
import time
from dataclasses import asdict

from agent.brain import LLMError
from agent.survival_planner import Decision
from core.model_gate import ModelGateError
from core.survival_mainline import choose_chapter, mainline_enabled, same_goal
from core.starter_mining_task import recover_interrupted_mine
from skills import SkillResult


class AutonomyRuntime:
    def _init_autonomy(self):
        self.task_controller = None
        self._task_model_decision = None
        self._task_child = False
        self._task_safety = False
        self._task_resume_requested = False
        self._task_event_signature = None
        self._mainline_decision = None
        self._mainline_repair_id = None
        self._mainline_repair_after = 0
        self.mainline_enabled = mainline_enabled(self.settings)
        if not self.mainline_enabled:
            self.context.pop("mainline", None)
        if self.daily and self.settings.get("autonomy", {}).get("enabled", True):
            from core.autonomous_tasks import AutonomousTaskController
            self.task_controller = AutonomousTaskController(
                self.settings, saved=self.context.get("autonomous_task"), scope=self.journal.scope)
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

    def _update_mainline(self, observation):
        self._mainline_decision = None
        if not self.mainline_enabled or not self.daily_board:
            return
        status = observation.get("status") or {}
        if (not self.task_controller._fresh(observation) or status.get("gamemode") != "survival"
                or status.get("dimension") not in ("overworld", "minecraft:overworld")):
            if self.context.get("mainline"):
                self.context["mainline"]["fresh"] = False
            return
        old = self.context.get("mainline") or {}
        view, self._mainline_decision = choose_chapter(observation, self.daily_board, self.settings,
                                                       self.context.setdefault("mainline_sites", {}))
        if old.get("stage") != view["stage"]:
            self._mainline_repair_id = None
            self._mainline_repair_after = 0
            self._record("INFO", "MAINLINE_STAGE", {"mainline": view})
        self.context["mainline"] = view

    def _mainline_action(self, observation):
        if not self.mainline_enabled:
            return None
        goal = self._mainline_decision
        if goal is None or self.journal.state.get("pending"):
            return Decision("wait", {"seconds": 2}, "Reobserve before choosing a mainline goal", "observe")
        if self.daily_policy.validate(goal, observation, context=self.context):
            return Decision("wait", {"seconds": 2}, "Mainline production requires a fresh safe opening", "observe")
        task = self._task_snapshot() or {}
        if (goal.name == "run_survival_task" and task.get("status") == "needs_ai"
                and same_goal(task.get("args"), goal.args)):
            now = time.monotonic()
            if self._mainline_repair_id != task["task_id"]:
                if now < self._mainline_repair_after:
                    return Decision("wait", {"seconds": min(5, self._mainline_repair_after - now)},
                                    "Local recovery exhausted; recheck before another repair request", "observe")
                self._mainline_repair_id = task["task_id"]
                self._mainline_repair_after = now + 30
                if self.uses_llm and not self._model_blocked:
                    return None  # One serial, stage-scoped repair turn.
        return goal

    def _local_opening(self, observation):
        """Start one observed tool goal through the same controller used by native calls."""
        if (self.task_controller is None or self.settings.get("objective") != "daily"
                or self.settings.get("autonomy", {}).get("local_opening") is not True
                or self.settings.get("operator_goal") or self.settings.get("intent", "survival") != "survival"
                or self._task_snapshot().get("status") != "idle"
                or self.journal.state.get("pending") or self._model_blocked
                or self._model_worker is not None and self._model_worker.pending):
            return None
        gate = getattr(getattr(self.planner, "brain", None), "request_gate", None)
        if gate is not None:
            try:
                if gate.snapshot()["state"] != "idle":
                    return None
            except ModelGateError:
                return None
        if (observation.get("fresh") is not True or observation.get("stale") is True
                or observation.get("observation_scope") == "safety" or observation.get("inventory_fresh") is False):
            return None
        row = next((row for row in (self.daily_board or {}).get("tasks", []) if row["id"] == "starter_tools"), None)
        action = (row or {}).get("next_action")
        if not action or action["name"] not in {"craft_item", "place_item", "gather_block", "gather_materials",
                                                 "clear_foliage", "take_items"}:
            return None
        decision = Decision("run_survival_task", {"task": "starter_tools"},
                            "Current observations support starting the basic tool goal", "autonomous_task")
        return decision if self.daily_policy.validate(decision, observation, context=self.context) is None else None

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
        if self.mainline_enabled:
            self.context["mainline_task_id"] = self._task_snapshot()["task_id"]
            view = self.context.get("mainline") or {}
            self.context["mainline_task_stage"] = (view.get("stage")
                if same_goal(decision.args, view.get("goal")) else None)
        if self._model_feedback:
            # Keep the original tool_call_id open until the goal ends. Children
            # are not fabricated native tool calls and never trigger the model.
            self._task_model_decision = copy.deepcopy(decision)
            self._model_feedback = False
        self._save_task("TASK_STARTED")
        return SkillResult(True, "Goal accepted; local substeps will run without model calls", {
            "accepted": True, "completed": False, "executed": False,
            "task_id": self._task_snapshot().get("task_id")})

    def _task_next(self, observation):
        if not self._task_active():
            return None
        if self.mainline_enabled and not self.journal.state.get("pending"):
            task = self._task_snapshot()
            view = self.context.get("mainline") or {}
            previous_stage = self.context.get("mainline_task_stage")
            chapter_changed = previous_stage is not None and previous_stage != view.get("stage")
            if (view.get("fresh") and (self.context.get("mainline_task_id") != task["task_id"] or chapter_changed)
                    and not (task.get("starter_mine") or {}).get("site")):
                if not same_goal(task.get("args"), view.get("goal")):
                    self.task_controller.stop("Fresh evidence selects another chapter; replan at this confirmed boundary",
                                              cancelled=True)
                    self._save_task("TASK_CANCELLED")
                    self._finish_task_feedback(observation)
                    return Decision("wait", {"seconds": .25}, "Continue from the current mainline chapter", "mainline")
                self.context["mainline_task_id"] = task["task_id"]
                self.context["mainline_task_stage"] = view["stage"]
        recovered = None
        if self.continuous and not self.journal.state.get("pending"):
            old = self.context.get("last_abandoned_action") or {}
            if old.get("name") == "step_starter_staircase" and old.get("generation") == self.context["life_generation"]:
                if "mine_landmark" not in old:
                    old["mine_landmark"] = recover_interrupted_mine(self.journal, old)
                if old["mine_landmark"] and self.task_controller.restore_interrupted_mine(observation, self.context, old):
                    recovered = old
        decision = self.task_controller.next_action(observation, self.context)
        if recovered and self._task_snapshot()["status"] in {"running", "suspended", "completed"}:
            self._record("INFO", "STARTER_MINE_LANDMARK_RECOVERED", {"pos": observation["status"]["pos"],
                         "site": recovered["mine_landmark"]["site"], "original_action_confirmed": False})
            recovered["mine_landmark"] = None
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
