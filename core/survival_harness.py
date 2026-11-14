"""Observe -> plan -> one verified action -> checkpoint, with bounded recovery."""

import copy
import json
import math
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import asdict

from agent.survival_planner import Decision, SurvivalPlanner
from core.client import APIError
from core.sleep_controller import SleepController
from core.survival_state import ObservationCollector, SurvivalJournal
from skills import SkillResult
from skills.sleep import AutoSleepSkill
from skills.survival import SurvivalSkills
from agent.brain import LLMError


UNCERTAIN = {"request_timeout", "network_error", "invalid_json", "invalid_response"}
FATAL = {"unauthorized", "invalid_credentials", "death_loop", "death_loop_cooldown"}


class SurvivalHarness:
    """One worker owns actions; the main thread can stop/pause and watch danger.

    Model decisions are optional; local safety and verified skills remain mandatory.
    Restarted pending actions require explicit resume and fresh observation.
    """

    def __init__(self, client, settings, sleep_settings, recorder=None, *, planner=None,
                 skills=None, collector=None, journal=None):
        self.client = client
        self.settings = settings
        self.sleep_settings = sleep_settings
        self.recorder = recorder
        self.planner = planner or SurvivalPlanner(settings)
        self.uses_llm = getattr(self.planner, "uses_llm", False) is True
        self._safety_rules = SurvivalPlanner(settings)
        self._model_feedback = False
        self.phase = "idle"
        self.latest_status = None
        self.skills = skills or SurvivalSkills(client)
        self.collector = collector or ObservationCollector(client, settings["scan_radius"], include_terrain=self.uses_llm)
        self.journal = journal or SurvivalJournal(settings["state_dir"], endpoint=client.base_url,
                                                username=client.username, world_id=settings["world_id"])
        self.context = copy.deepcopy(self.journal.state.get("context", {}))
        self.context.setdefault("failures", {})
        self.context.setdefault("blocked_until", {})
        self.context.setdefault("explore_index", 0)
        self.context["death_count"] = 0
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="survival")
        self._future = None
        self._cancel = threading.Event()
        self._paused = bool(self.journal.state.get("pending"))
        self._closed = False
        self._active_decision = None
        self._next_attempt = 0.0
        self._next_monitor = 0.0
        self._started = time.monotonic()
        self._saw_observation = False
        self._death_awaiting_marker = False
        self._budget_exhausted = False
        self.steps = 0
        self.failures = 0
        self.finished = False
        self.completed = False
        self.last_result = None
        self.last_observation = None
        self.pause_reason = "interrupted_action_requires_resume" if self._paused else None
        if self._paused:
            self._record("WARN", "RECOVERY_REQUIRED", {"reason": self.pause_reason})

    @property
    def busy(self):
        return self._future is not None

    @property
    def paused(self):
        return self._paused

    def _record(self, level, event, data):
        if self.recorder and callable(getattr(self.recorder, "redact", None)):
            data = self.recorder.redact(data)
        self.journal.record(event, data)
        if self.recorder:
            self.recorder.record(level, "survival", event, data)

    def _checkpoint(self, **changes):
        if self.recorder and callable(getattr(self.recorder, "redact", None)):
            changes = self.recorder.redact(changes)
        self.journal.update(**changes)

    def summary(self):
        state = "complete" if self.completed else "finished" if self.finished else "paused" if self.paused else "running"
        goal = self._active_decision.goal if self._active_decision else "observe"
        return f"Survival {state}; steps={self.steps}; goal={goal}" + (f"; {self.pause_reason}" if self.pause_reason else "")

    def snapshot(self, status=None):
        if status is not None:
            self.latest_status = status
        return {"status": self.latest_status or {}, "steps": self.steps, "busy": self.busy,
                "paused": self.paused, "pause_reason": self.pause_reason,
                "finished": self.finished, "completed": self.completed, "phase": self.phase,
                "planner": "llm" if self.uses_llm else "rules",
                "retry_in_seconds": max(0, round(self._next_attempt - time.monotonic(), 1)),
                "goal": self._active_decision.goal if self._active_decision else None,
                "decision": asdict(self._active_decision) if self._active_decision else None}

    def _phase(self, phase):
        self.phase = phase
        self._record("INFO", "AGENT_PHASE", {"phase": phase, "planner": "llm" if self.uses_llm else "rules"})

    def _mandatory_safety(self, observation):
        """Only safety decisions may override the model, never its resource plan."""
        decision = self._safety_rules.choose(observation, self.context)
        if decision.goal in {"safety", "recover", "observe", "food", "sleep"}:
            return decision
        return None

    @staticmethod
    def _life_marker(observation):
        status = observation["status"]
        return json.dumps({key: status.get(key) for key in ("last_death", "dimension", "alive")}, sort_keys=True)

    def _unsafe_destination(self, decision, observation):
        if decision.name not in {"move_to", "gather_block", "place_item"}:
            return None
        target = self._safety_rules._pos(decision.args.get("pos"))
        if target is None:
            return "A local target position is required"
        for entity in observation.get("entities", []):
            pos = self._safety_rules._pos(entity.get("pos"))
            if entity.get("type") == "hostile" and pos is not None and math.dist(pos, target) <= self.settings["threat_radius"]:
                return "A hostile is close to the proposed destination"
        for block in observation.get("blocks", []) + observation.get("terrain", []):
            pos = self._safety_rules._pos(block.get("pos"))
            if pos is not None and block.get("name") in SurvivalSkills.HAZARDS and math.dist(pos, target) <= 1.5:
                return "An observed environmental hazard is close to the proposed destination"
        return None

    def resume(self):
        if self._closed or self.busy or self.finished:
            return False
        self._paused = False
        self.pause_reason = None
        self.failures = 0
        self._next_attempt = 0
        # A previous intention is evidence, never an instruction to replay.
        self._record("INFO", "RESUMED", {"reobserve": True})
        return True

    def pause(self, reason="user_requested"):
        self._paused = True
        self.pause_reason = reason
        self._cancel.set()
        try:
            if self.busy and not self._future.done():
                self.client.stop()
        finally:
            self._record("WARN", "PAUSED", {"reason": reason})

    def request_step(self):
        if not self.resume():
            return False
        return self._start()

    def request_sleep(self):
        if not self.resume():
            return False
        return self._start(Decision("auto_sleep", {}, "Player requested sleep", "sleep"))

    def _start(self, manual=None):
        if self._closed or self.busy or self.paused or self.finished:
            return False
        if self.steps >= self.settings["max_steps"] or time.monotonic() - self._started >= self.settings["max_runtime_seconds"]:
            self.finished = True
            self.pause_reason = "budget_exhausted"
            self._record("WARN", "BUDGET_EXHAUSTED", {"steps": self.steps})
            return False
        self._cancel = threading.Event()
        self._model_feedback = False
        self._future = self._executor.submit(self._cycle, manual)
        return True

    @staticmethod
    def _target(decision):
        pos = decision.args.get("pos")
        if isinstance(pos, (list, tuple)) and len(pos) == 3:
            return ",".join(str(int(math.floor(value))) for value in pos)
        return decision.name + ":" + str(decision.args.get("item", ""))

    def _prepare_context(self, observation):
        now = time.time()
        self.context["blocked_until"] = {key: value for key, value in self.context.get("blocked_until", {}).items()
                                         if isinstance(value, (int, float)) and value > now}
        self.context["blocked_targets"] = list(self.context["blocked_until"])
        status = observation["status"]
        death = status.get("last_death")
        marker = json.dumps(death, sort_keys=True) if death else None
        new_marker = marker and marker != self.context.get("last_death")
        lost_life = status.get("alive") is False and self.context.get("was_alive") is True
        # The death marker can arrive one packet after alive=False. Count that
        # transition once, and do not count historical last_death on first login.
        new_death = (self._saw_observation and new_marker and not self._death_awaiting_marker) or lost_life
        if not self._saw_observation and status.get("alive") is False:
            new_death = True
        if new_death:
            self.context.update(failures={}, blocked_until={}, blocked_targets=[], explore_index=0,
                                spawn=None, death_count=self.context["death_count"] + 1)
            self._record("WARN", "DEATH_OBSERVED", {"last_death": death, "count": self.context["death_count"]})
            self._death_awaiting_marker = not bool(new_marker)
        if new_marker:
            self._death_awaiting_marker = False
        if marker:
            self.context["last_death"] = marker
        self._saw_observation = True
        self.context["was_alive"] = status.get("alive")
        if status.get("alive") and status.get("ready") and not self.context.get("spawn"):
            self.context["spawn"] = status.get("pos")
        self.context["milestones"] = self.planner.progress(observation)

    def _cycle(self, manual):
        decision = Decision("observe", {}, "Read current state", "observe")
        observation = None
        try:
            self._phase("observing")
            observation = self.collector.collect(self._cancel)
            self.latest_status = observation["status"]
            self._prepare_context(observation)
            self.context.update(remaining_steps=max(0, self.settings["max_steps"] - self.steps),
                                remaining_seconds=max(0, self.settings["max_runtime_seconds"] - (time.monotonic() - self._started)))
            if self._cancel.is_set():
                return decision, SkillResult(False, "Cancelled before action", error_code="cancelled"), observation
            if self.context["death_count"] >= self.settings["max_deaths"]:
                return decision, SkillResult(False, "Death limit reached", error_code="death_limit"), observation
            if manual is not None and (not observation["status"].get("ready")
                                       or not observation["status"].get("alive") or observation["status"].get("busy")):
                decision = Decision("wait", {"seconds": 1}, "Manual action needs an idle, living, ready bot", "observe")
                return decision, SkillResult(False, decision.reason, error_code="busy"), observation
            if manual is not None:
                decision = manual
            elif self.uses_llm:
                safety = self._mandatory_safety(observation)
                if safety is not None:
                    decision = safety
                    self._record("WARN", "SAFETY_OVERRIDE", {"decision": asdict(safety), "status": observation["status"]})
                else:
                    self._phase("thinking")
                    before_life = self._life_marker(observation)
                    decision = self.planner.choose(observation, self.context, cancel_event=self._cancel)
                    self._model_feedback = True
                    if self._cancel.is_set():
                        return decision, SkillResult(False, "Paused while the model was planning; no action sent", error_code="cancelled"), observation
                    # A slow model must not act on pre-death inventory or an old
                    # daytime/danger snapshot. Skills then recheck target details.
                    self._phase("rechecking")
                    observation = self.collector.collect(self._cancel)
                    self.latest_status = observation["status"]
                    self._prepare_context(observation)
                    if self._cancel.is_set():
                        return decision, SkillResult(False, "Cancelled before model action", error_code="cancelled"), observation
                    if self.context["death_count"] >= self.settings["max_deaths"]:
                        return decision, SkillResult(False, "Death limit reached while the model was planning",
                                                     {"executed": False}, "death_limit"), observation
                    if self._life_marker(observation) != before_life:
                        return decision, SkillResult(False, "Life or dimension changed during model planning; replan",
                                                     {"executed": False}, "safety_changed"), observation
                    unsafe_target = self._unsafe_destination(decision, observation)
                    if unsafe_target:
                        self._record("WARN", "SAFETY_OVERRIDE", {"model_decision": asdict(decision), "reason": unsafe_target})
                        return decision, SkillResult(False, unsafe_target, {"executed": False}, "safety_changed"), observation
                    safety = self._mandatory_safety(observation)
                    if safety is not None and (safety.name != decision.name or safety.args != decision.args):
                        self._record("WARN", "SAFETY_OVERRIDE", {"model_decision": asdict(decision), "decision": asdict(safety)})
                        return decision, SkillResult(False, "Fresh local safety check rejected the model action; replan",
                                                     {"executed": False, "safety_decision": asdict(safety)}, "safety_changed"), observation
            else:
                decision = self.planner.choose(observation, self.context)
            self._active_decision = decision
            self._record("INFO", "DECISION", {"decision": asdict(decision), "status": observation["status"],
                                               "counts": observation["counts"], "milestones": self.context["milestones"],
                                               "source": "model" if self._model_feedback else "manual" if manual else "safety" if self.uses_llm else "rules"})
            if decision.name == "wait":
                self._phase("waiting")
                return decision, SkillResult(True, decision.reason, {"waiting": True}), observation
            if decision.name == "goal_complete":
                if self.uses_llm and not self._safety_rules.progress(observation)["starter_goal_complete"]:
                    return decision, SkillResult(False, "Latest inventory does not verify goal completion", error_code="goal_unverified"), observation
                return decision, SkillResult(True, decision.reason, {"goal_complete": True}), observation
            self._checkpoint(context=self.context, pending={"decision": asdict(decision), "before_counts": observation["counts"]})
            self._phase("acting")
            if decision.name == "auto_sleep":
                options = {key: value for key, value in self.sleep_settings.items() if key in SleepController.SKILL_OPTIONS}
                result = AutoSleepSkill(self.client).execute(**options, cancel_event=self._cancel)
            else:
                result = self.skills.execute(decision.name, decision.args, cancel_event=self._cancel)
            if not isinstance(result, SkillResult):
                raise TypeError("Skill returned an invalid result")
            if self._model_feedback and not self._cancel.is_set():
                try:
                    observation = self.collector.collect(self._cancel)
                    self.latest_status = observation["status"]
                    self._prepare_context(observation)
                except APIError as error:
                    self._record("WARN", "POST_OBSERVATION_FAILED", {"code": error.code})
                    # Keep the skill's verified result, but mark the feedback
                    # observation stale; the next turn must observe anew.
                    observation = {**observation, "fresh": False}
            return decision, result, observation
        except LLMError as exc:
            return decision, SkillResult(False, str(exc), {"executed": False, **getattr(exc, "details", {})}, exc.code), observation
        except APIError as exc:
            # Reuse the existing session so upstream death-loop protection stays
            # in force. Never log in again to bypass its cooldown.
            if decision.name == "observe" and exc.code == "not_connected" and not self._cancel.is_set():
                try:
                    self.client.connect()
                    return decision, SkillResult(False, "Reconnect requested; reobserve before acting", error_code="reconnecting"), observation
                except APIError as reconnect_error:
                    exc = reconnect_error
            return decision, SkillResult(False, str(exc), error_code=exc.code), observation
        except Exception as exc:
            return decision, SkillResult(False, "Unexpected harness error; inspect the checkpoint",
                                         {"exception_type": type(exc).__name__}, error_code="internal"), observation

    def _monitor(self):
        if self._cancel.is_set() or time.monotonic() < self._next_monitor:
            return
        self._next_monitor = time.monotonic() + max(1, self.settings["poll_interval_seconds"])
        decision = self._active_decision
        if decision is None or decision.name in ("retreat_from", "eat_food", "auto_sleep"):
            return
        try:
            status = self.client.get_status()["result"]
            self.latest_status = status
        except APIError:
            self.pause("status_unavailable_during_action")
            return
        hostile = status.get("nearest_hostile") or {}
        danger = (not status.get("alive") or status.get("in_lava") or status.get("on_fire")
                  or status.get("submerged") or status.get("health", 20) <= self.settings["low_health_threshold"]
                  or (isinstance(hostile.get("dist"), (int, float)) and hostile["dist"] <= self.settings["threat_radius"]))
        if danger:
            self._cancel.set()
            self.client.stop()
            self._record("WARN", "SAFETY_INTERRUPT", {"status": status})

    def tick(self, automatic=True):
        if self._closed:
            return None
        if not self.finished and time.monotonic() - self._started >= self.settings["max_runtime_seconds"]:
            self._budget_exhausted = True
            if self._future is not None and not self._future.done() and not self._cancel.is_set():
                self._cancel.set()
                try:
                    self.client.stop()
                except APIError as exc:
                    self._record("WARN", "STOP_FAILED", {"code": exc.code})
        if self._future is not None:
            if not self._future.done():
                try:
                    self._monitor()
                except APIError as exc:
                    self._record("WARN", "STOP_FAILED", {"code": exc.code})
                return None
            try:
                decision, result, observation = self._future.result()
            except CancelledError:
                decision = Decision("observe", {}, "Cancelled", "observe")
                result, observation = SkillResult(False, "Cancelled", error_code="cancelled"), None
            self._future = None
            self._active_decision = None
            if self._model_feedback:
                try:
                    self.planner.observe_result(decision, result, observation or {})
                except LLMError as error:
                    self._paused = True
                    self.pause_reason = error.code
                    self._record("ERROR", "LLM_FEEDBACK_FAILED", {"code": error.code, "message": str(error)})
                finally:
                    self._model_feedback = False
            self.steps += 1
            self.last_result, self.last_observation = result, observation
            self.context["last_result"] = asdict(result)
            if decision.goal == "explore" and decision.name == "move_to":
                self.context["explore_index"] = self.context.get("explore_index", 0) + 1
            delay = self.settings["poll_interval_seconds"]
            if not result.success:
                self.failures += 1
                key = self._target(decision)
                counts = self.context.setdefault("failures", {})
                counts[key] = counts.get(key, 0) + 1
                if counts[key] >= self.settings["max_target_failures"]:
                    self.context.setdefault("blocked_until", {})[key] = time.time() + self.settings["target_cooldown_seconds"]
                delay = min(60, self.settings["retry_seconds"] * 2 ** min(self.failures - 1, 4))
                uncertain_action = result.error_code in UNCERTAIN and decision.name != "observe"
                http_status = result.data.get("http_status")
                transient_model_error = (result.error_code in {"llm_timeout", "llm_network_error"}
                    or (result.error_code == "llm_http_error" and isinstance(http_status, int)
                        and (http_status in {408, 425, 429} or 500 <= http_status < 600)))
                fatal_model_error = result.error_code == "llm_http_error" and not transient_model_error
                if (uncertain_action or fatal_model_error
                    or result.error_code in FATAL | {"death_limit", "internal", "internal_error", "llm_invalid_config", "llm_record_error", "llm_missing_feedback"}
                    or (not transient_model_error and self.failures >= self.settings["max_consecutive_failures"])):
                    self._paused = True
                    self.pause_reason = result.error_code or "repeated_failures"
                if transient_model_error:
                    self._record("WARN", "MODEL_RETRY_SCHEDULED", {"code": result.error_code, "http_status": http_status,
                                 "retry_seconds": delay, "consecutive_failures": self.failures})
                if uncertain_action:
                    try:
                        self.client.stop()
                    except APIError as exc:
                        self._record("WARN", "STOP_FAILED", {"code": exc.code})
            else:
                no_movement = (decision.name == "move_to"
                               and result.data.get("already_in_range") is True
                               and result.data.get("executed") is False)
                if no_movement:
                    self._record("INFO", "NO_MOVEMENT", {"decision": asdict(decision),
                                 "reason": "Target was already within range; previous failures remain relevant"})
                else:
                    self.failures = 0
                if decision.name == "wait":
                    delay = max(delay, min(60, decision.args.get("seconds", 5)))
                self.completed = decision.name == "goal_complete"
                self.finished = self.completed
            if self._budget_exhausted:
                self.finished = True
                self.pause_reason = "budget_exhausted"
            self._next_attempt = time.monotonic() + delay
            self.phase = "paused" if self.paused else "complete" if self.completed else "waiting"
            # Keep uncertain intentions for explicit recovery after a restart.
            keep_pending = ((result.error_code in UNCERTAIN | {"internal", "internal_error"} and decision.name != "observe")
                            or (decision.name in {"observe", "wait"} and (observation is None
                                or not observation["status"].get("ready") or observation["status"].get("busy"))))
            self._checkpoint(context=self.context, last_result=asdict(result),
                                pending=self.journal.state.get("pending") if keep_pending else None)
            self._record("INFO" if result.success else "WARN", "STEP_RESULT",
                         {"step": self.steps, "decision": asdict(decision), "result": asdict(result), "paused": self.paused})
            return result
        if self.finished:
            return None
        if self.steps >= self.settings["max_steps"] or self._budget_exhausted:
            self.finished = True
            self.pause_reason = "budget_exhausted"
            self._record("WARN", "BUDGET_EXHAUSTED", {"steps": self.steps})
        elif self.paused or not automatic:
            return None
        elif time.monotonic() >= self._next_attempt:
            self._start()
        return None

    def close(self):
        if self._closed:
            return
        try:
            self.pause("shutdown")
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)
            if self._future is not None:
                self.tick(automatic=False)
            self._closed = True
