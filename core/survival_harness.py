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
from core.local_execution import LocalExecutor
from core.local_recovery import canopy_descent
from core.sleep_controller import SleepController
from core.survival_state import ObservationCollector, SurvivalJournal
from skills import SkillResult
from skills.sleep import AutoSleepSkill
from skills.survival import SurvivalSkills
from agent.brain import LLMError


# The API can restore its original block cache when no server acknowledgement
# arrives. An unchanged cached block does not prove the dig never happened.
UNCERTAIN = {"request_timeout", "network_error", "invalid_json", "invalid_response", "dig_unconfirmed", "action_unconfirmed"}
FATAL = {"unauthorized", "invalid_credentials", "death_loop", "death_loop_cooldown"}
RETREAT_REPLAN = {"unsafe", "path_not_found", "no_path", "move_unverified", "retreat_unverified"}
RETREAT_WAIT_SECONDS = 5
RETREAT_SEARCH_COOLDOWN = 30


def _uncertain_result(result):
    return (result.error_code in UNCERTAIN | {"internal", "internal_error"}
            or isinstance(result.data, dict) and result.data.get("action_state") == "unknown")


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
        self.local_executor = LocalExecutor(settings)
        from skills.canopy import CanopyRecoverySkill
        self.canopy_skills = CanopyRecoverySkill(client)
        self._local_step = False
        self.competition_mode = settings.get("objective", "starter") == "competition"
        self.competition = None
        self._competition_board_signature = None
        self._competition_advancements = None
        self._competition_advancements_fetched_at = None
        self._competition_advancements_fetched_monotonic = None
        self._competition_next_advancement = 0.0
        self.daily = settings.get("objective", "starter") in {"daily", "competition"}
        if self.competition_mode and not self.uses_llm:
            raise ValueError("competition requires a model planner; no rule fallback")
        self.daily_goals = None
        self.daily_policy = None
        self.memory = None
        self.shelter_skills = None
        self.batch_skills = None
        self.door_skills = None
        self.positioning_skills = None
        self.daily_board = None
        self._daily_board_signature = None
        self._model_blocked = None
        self._model_worker = None
        self._safety_baseline = None
        if self.uses_llm:
            from core.model_worker import SerialModelWorker
            self._model_worker = SerialModelWorker(self.planner)
        self.phase = "idle"
        self.latest_status = None
        self.skills = skills or SurvivalSkills(client)
        self.collector = collector or ObservationCollector(client, settings["scan_radius"], include_terrain=self.uses_llm)
        if self.daily:
            from agent.daily_survival import DailySurvivalGoals
            from core.daily_observation import DailyObservationCollector
            from core.daily_policy import DailySafetyPolicy
            from skills.daily_survival import DailySurvivalSkills
            from skills.shelter import ShelterSkills
            from skills.batch import BatchSkills
            from skills.doors import DoorSkills
            from skills.positioning import PositioningSkills
            self.daily_goals = DailySurvivalGoals(settings)
            self.daily_policy = DailySafetyPolicy(settings, allow_controls=self.uses_llm)
            self.skills = skills or DailySurvivalSkills(client)
            self.collector = collector or DailyObservationCollector(client, settings["scan_radius"],
                low_health_threshold=settings["low_health_threshold"])
            self.shelter_skills = ShelterSkills(client)
            self.batch_skills = BatchSkills(client, gather_skills=self.skills, shelter_skills=self.shelter_skills)
            self.door_skills = DoorSkills(client)
            self.positioning_skills = PositioningSkills(client)
        self.journal = journal or SurvivalJournal(settings["state_dir"], endpoint=client.base_url,
                                                username=client.username, world_id=settings["world_id"])
        self.context = copy.deepcopy(self.journal.state.get("context", {}))
        self.context.setdefault("failures", {})
        self.context.setdefault("blocked_until", {})
        self.context.setdefault("explore_index", 0)
        self.context["death_count"] = 0
        if self.competition_mode:
            from core.competition_tasks import CompetitionTasks
            self.competition = CompetitionTasks(settings.get("competition", {}), saved=self.context.get("competition"))
        if self.daily:
            from core.survival_memory import SurvivalMemory
            self.memory = SurvivalMemory(settings["state_dir"] + "/memory", endpoint=client.base_url,
                username=client.username, world_id=settings["world_id"],
                secrets=(getattr(client, "password", ""), getattr(client, "token", ""), getattr(self.planner, "_secret", "")))
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="survival")
        self._future = None
        self._cancel = threading.Event()
        self._paused = bool(self.journal.state.get("pending"))
        self._closed = False
        self._active_decision = None
        self._next_attempt = 0.0
        self._next_monitor = 0.0
        self._next_idle_monitor = 0.0
        self._started = time.monotonic()
        self._saw_observation = False
        self._death_awaiting_marker = False
        self._budget_exhausted = False
        self.steps = 0
        self.failures = 0
        terminal_competition = bool(self.competition and self.competition.snapshot().get("phase") == "completed")
        self.finished = terminal_competition
        self.completed = terminal_competition
        if terminal_competition:
            self.phase = "complete"
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

    @property
    def next_poll_seconds(self) -> float:
        """Wake quickly for running/ready local work; retain deliberate backoff."""
        interval = self.settings["poll_interval_seconds"]
        if self.busy:
            return min(interval, .05)
        if self.paused or self.finished:
            return interval
        delay = max(0, self._next_attempt - time.monotonic())
        local = self.context.get("local_execution") or {}
        if self.local_executor.enabled and local.get("phase") == "running":
            return min(interval, max(.05, delay))
        return interval

    def _record(self, level, event, data):
        if self.recorder and callable(getattr(self.recorder, "redact", None)):
            data = self.recorder.redact(data)
        self.journal.record(event, data)
        if self.recorder:
            self.recorder.record(level, "survival", event, data)

    def _checkpoint(self, **changes):
        if self.competition:
            self.context["competition"] = self.competition.state()
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
                "objective": "competition" if self.competition_mode else "daily" if self.daily else "starter",
                "competition": self.competition.snapshot() if self.competition else None,
                "model_blocked": self._model_blocked,
                "model_pending": bool(self._model_worker and self._model_worker.pending),
                "pending": copy.deepcopy(self.journal.state.get("pending")),
                "local_execution": self.local_executor.summary(self.context),
                "daily_tasks": self.daily_board,
                "memory_summary": {key: len(value) for key, value in self.context.get("memory", {}).items() if isinstance(value, list)},
                "retry_in_seconds": max(0, round(self._next_attempt - time.monotonic(), 1)),
                "goal": self._active_decision.goal if self._active_decision else None,
                "decision": asdict(self._active_decision) if self._active_decision else None}

    def _phase(self, phase):
        self.phase = phase
        self._record("INFO", "AGENT_PHASE", {"phase": phase, "planner": "llm" if self.uses_llm else "rules"})

    def _mandatory_safety(self, observation):
        """Only safety decisions may override the model, never its resource plan."""
        if (self.local_executor.enabled and not self.journal.state.get("pending")
                and not self._model_blocked and canopy_descent(observation, self.context, self.settings) is not None):
            return None  # Fresh quiet-canopy recovery is independent of an old threat coordinate.
        decision = self._safety_rules.choose(observation, self.context)
        decision = self._retreat_decision(observation, decision)
        if self.daily_policy:
            decision = self.daily_policy.choose(observation, decision, self.daily_board)
            if decision is None:
                return None
            recovery = self.context.get("retreat_recovery") or {}
            if (self.uses_llm and decision.name == "wait" and recovery.get("canopy_blocked") is True
                    and not self.journal.state.get("pending") and not self._model_blocked
                    and self.daily_policy._force_environment_error(observation) is None):
                return None  # Let the model report the blockage or explicitly clear one checked leaf.
        if decision.goal in {"safety", "recover", "observe", "food", "sleep"}:
            return decision
        return None

    def _retreat_state(self, observation, threat):
        """Persist recovery evidence, never an executable route or old pending action."""
        status = observation["status"]
        recovery = self.context.get("retreat_recovery")
        if not isinstance(recovery, dict) or recovery.get("dimension") != status.get("dimension"):
            recovery = {"dimension": status.get("dimension"), "evacuating": False,
                        "evacuation_origin": list(status["pos"]), "excluded_until": {}}
            self.context["retreat_recovery"] = recovery
        if recovery.get("evacuating") is not True:
            recovery["evacuation_origin"] = list(status["pos"])
        recovery["threat_pos"] = list(threat)
        now = time.time()
        recovery["excluded_until"] = {key: expiry for key, expiry in recovery.get("excluded_until", {}).items()
            if self._safety_rules._number(expiry) and expiry > now}
        return recovery

    def _retreat_changed(self, observation, threat, recovery):
        basis = recovery.get("search_basis") or {}
        origin = self._safety_rules._pos(observation["status"].get("pos"))
        old_origin = self._safety_rules._pos(basis.get("pos"))
        old_threat = self._safety_rules._pos(basis.get("threat_pos"))
        if origin and old_origin and (math.dist(origin, old_origin) >= 1
                or any(math.floor(a) != math.floor(b) for a, b in zip(origin, old_origin))):
            return True
        if old_threat and math.dist(threat, old_threat) >= 2:
            return True
        # Only compare cells actually observed on both occasions. A missing
        # resource row is not proof that an obstruction has disappeared.
        blocks = {tuple(row["pos"]): row for row in observation.get("blocks", []) + observation.get("terrain", [])
                  if isinstance(row, dict) and self._safety_rules._pos(row.get("pos")) is not None}
        for row in recovery.get("blocked_cells", []):
            current = blocks.get(tuple(row["pos"]))
            if current is not None and current.get("name") != row.get("name"):
                return True
        return False

    def _retreat_decision(self, observation, decision):
        recovery = self.context.get("retreat_recovery") or {}
        if decision.name != "retreat_from":
            if recovery.get("evacuating") is not True:
                return decision
            # Lifecycle, immediate hazards, stable footing, food and low health
            # retain priority. Night alone must not abandon a canopy relay.
            if decision.goal in {"observe", "recover", "food", "safety"} or observation["status"].get("is_sleeping"):
                return decision
            threat = self._safety_rules._pos(recovery.get("threat_pos"))
            if threat is None:
                return Decision("wait", {"seconds": RETREAT_WAIT_SECONDS}, "Evacuation needs a verified threat position", "safety")
            decision = Decision("retreat_from", {"pos": threat, "distance": 8},
                                "Continue the canopy evacuation until safe ground is verified", "safety")
        threat = self._safety_rules._pos(decision.args.get("pos"))
        if threat is None:
            return decision  # Skill parameter validation still owns malformed input.
        recovery = self._retreat_state(observation, threat)
        if recovery.get("retry_after", 0) > time.time() and not self._retreat_changed(observation, threat, recovery):
            return Decision("wait", {"seconds": RETREAT_WAIT_SECONDS},
                            "No verified retreat route; observe again before the bounded search cooldown expires", "safety")
        recovery.pop("retry_after", None)
        return decision

    def _retreat_options(self, observation, decision):
        recovery = self._retreat_state(observation, decision.args["pos"])
        excluded = []
        for key in recovery["excluded_until"]:
            try:
                pos = [int(value) for value in key.split(",")]
            except (ValueError, AttributeError):
                continue
            if len(pos) == 3:
                excluded.append(pos)
        return {"excluded_targets": excluded[-8:], "evacuating": recovery.get("evacuating") is True,
                "evacuation_origin": recovery.get("evacuation_origin")}

    def _retreat_result(self, decision, result, observation):
        """Return whether a known route failure uses recovery rather than global failure limits."""
        if decision.name == "descend_canopy" and observation is not None:
            recovery = self.context.get("retreat_recovery") or {}
            if result.success:
                if result.data.get("retreat_complete") is True:
                    self.context.pop("retreat_recovery", None)
                    self._record("INFO", "CANOPY_LANDED", {"pos": observation["status"]["pos"]})
                else:
                    recovery.update(evacuating=True, canopy_blocked=True)
                    recovery.pop("retry_after", None)
            return False
        if decision.name != "retreat_from" or observation is None:
            return False
        recovery = self.context.get("retreat_recovery")
        if not isinstance(recovery, dict):
            return False
        if result.success:
            if result.data.get("retreat_complete") is False and result.data.get("evacuation_stage") == "relay":
                recovery["evacuating"] = True
                recovery.pop("retry_after", None)
                self._record("INFO", "EVACUATION_RELAY", {"target": result.data.get("target")})
            elif result.data.get("retreat_complete") is True and result.data.get("evacuation_stage") == "ground":
                self.context.pop("retreat_recovery", None)
            return False
        # Unknown writes must never become permission to try a different route.
        if result.error_code in UNCERTAIN | FATAL | {"internal", "internal_error", "cancelled"}:
            return False
        if result.error_code not in RETREAT_REPLAN and result.data.get("candidate_failed") is not True:
            return False
        support = result.data.get("origin_support") or {}
        if isinstance(support, dict) and support.get("center_name") in SurvivalSkills.RETREAT_LEAVES:
            recovery.update(evacuating=True, canopy_blocked=True)
        target = self._safety_rules._pos(result.data.get("target"))
        if target is not None:
            key = ",".join(str(math.floor(value)) for value in target)
            excluded = recovery.setdefault("excluded_until", {})
            excluded.pop(key, None)
            excluded[key] = time.time() + self.settings["target_cooldown_seconds"]
            recovery["excluded_until"] = dict(list(excluded.items())[-8:])
        else:
            recovery["retry_after"] = time.time() + RETREAT_SEARCH_COOLDOWN
        recovery["search_basis"] = {"pos": list(observation["status"]["pos"]), "threat_pos": list(decision.args["pos"])}
        recovery["blocked_cells"] = [copy.deepcopy(row) for row in result.data.get("blocked_cells", [])
                                     if isinstance(row, dict) and self._safety_rules._pos(row.get("pos")) is not None][:4]
        self._record("WARN", "RETREAT_REPLAN_SCHEDULED", {"code": result.error_code, "target": target,
                     "retry_seconds": RETREAT_WAIT_SECONDS, "search_retry_after": recovery.get("retry_after")})
        return True

    @staticmethod
    def _life_marker(observation):
        status = observation["status"]
        return json.dumps({key: status.get(key) for key in ("last_death", "dimension", "alive")}, sort_keys=True)

    def _unsafe_destination(self, decision, observation):
        target_actions = {"move_to", "gather_block", "place_item", "descend_canopy"}
        if self.daily:
            target_actions |= {"gather_resource", "harvest_crop", "gather_seeds", "clear_foliage", "plant_crop", "till_soil", "place_bed", "place_door",
                               "smelt_start", "smelt_collect", "inspect_container", "store_items", "take_items",
                               "hunt_animal", "collect_drops", "build_shelter_step", "build_shelter_batch", "set_door", "position_for_task"}
        if decision.name not in target_actions:
            return None
        if "entity_id" in decision.args:
            entity = next((row for row in observation.get("entities", []) if row.get("id") == decision.args["entity_id"]), None)
            target = self._safety_rules._pos((entity or {}).get("pos"))
        else:
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
        gate = getattr(getattr(self.planner, "brain", None), "request_gate", None)
        if self._model_blocked and gate and gate.snapshot().get("state") == "idle":
            self._model_blocked = None
        # A previous intention is evidence, never an instruction to replay.
        self._record("INFO", "RESUMED", {"reobserve": True})
        return True

    def pause(self, reason="user_requested"):
        self._paused = True
        self.pause_reason = reason
        self._cancel.set()
        if self._model_worker:
            self._model_worker.cancel()
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
        step_limit = (self.settings.get("competition", {}).get("max_steps", 100000)
                      if self.competition_mode else self.settings["max_steps"])
        time_expired = (self.competition.snapshot().get("remaining_seconds", 0) <= 0
                        if self.competition is not None else
                        time.monotonic() - self._started >= self.settings["max_runtime_seconds"])
        if self.steps >= step_limit or time_expired:
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
        self._update_lifecycle(observation["status"])
        if observation.get("observation_scope") == "safety":
            # Empty fields mean unobserved, not an empty backpack or lost goals.
            self.context.setdefault("milestones", {})
            if isinstance(self.daily_board, dict):
                self.daily_board = {**self.daily_board, "fresh": False, "observation_scope": "safety"}
            if isinstance(self.context.get("daily_tasks"), dict):
                self.context["daily_tasks"].update(fresh=False, observation_scope="safety")
            return
        self.context["milestones"] = self.planner.progress(observation)
        if self.memory:
            self.memory.observe(observation)
            recalled = self.memory.recall(observation, limit=6, max_bytes=2500)
            self.context["memory"] = recalled
            self.daily_board = self.daily_goals.build(observation, recalled, self.context)
            self.daily_board.update(fresh=True, observation_scope="full")
            self.context["daily_tasks"] = {key: self.daily_board.get(key) for key in ("phase", "active_task_id", "maintenance_ready")}
            self.context["daily_tasks"].update(fresh=True, observation_scope="full")
            self.context["daily_tasks"]["tasks"] = [{key: row.get(key) for key in ("id", "status", "priority", "missing", "blocked_reason")}
                                                     for row in self.daily_board.get("tasks", [])]
            self.context["action_hints"] = [row["next_action"] for row in self.daily_board.get("tasks", []) if row.get("next_action")][:3]
            self.context["survival_constraints"] = self.daily_policy.constraints(observation)
            signature = json.dumps(self.context["daily_tasks"], sort_keys=True)
            if signature != self._daily_board_signature:
                self._record("INFO", "DAILY_TASKS", {"board": self.daily_board,
                    "memory_counts": {key: len(value) for key, value in recalled.items() if isinstance(value, list)}})
                self._daily_board_signature = signature
        self._competition_context(observation)

    def _competition_context(self, observation):
        """Refresh evidence outside the model; retain unknowns instead of fabricated progress."""
        if not self.competition:
            return
        if observation.get("observation_scope") == "safety" or observation.get("fresh") is False:
            self.context["competition"] = self.competition.state()
            return
        now = time.monotonic()
        if now >= self._competition_next_advancement:
            interval = self.settings.get("competition", {}).get("advancement_poll_seconds", 5)
            self._competition_next_advancement = now + interval
            try:
                self._competition_advancements = self.client.advancements(
                    status="all", include_recipes=False, include_criteria=False, timeout_seconds=3)
                self._competition_advancements_fetched_at = time.time()
                self._competition_advancements_fetched_monotonic = time.monotonic()
            except APIError as error:
                self._competition_advancements = None
                self._competition_advancements_fetched_at = None
                self._competition_advancements_fetched_monotonic = None
                self._record("WARN", "COMPETITION_EVIDENCE_UNAVAILABLE", {"code": error.code})
        fetch_mono = self._competition_advancements_fetched_monotonic
        cache_now = time.monotonic()
        cache_fresh = fetch_mono is not None and 0 <= cache_now - fetch_mono <= 5
        advancements = self._competition_advancements if cache_fresh else None
        fetched_at = self._competition_advancements_fetched_at if cache_fresh else None
        tools = getattr(self.planner, "tool_definitions", [])
        capabilities = [entry["function"]["name"] for entry in tools]
        board = self.competition.update(observation, advancements, capabilities=capabilities,
                                        advancements_fetched_at=fetched_at)
        self.context["competition"] = self.competition.state()
        signature = json.dumps(board, sort_keys=True, ensure_ascii=True)
        if signature != self._competition_board_signature:
            self._record("INFO", "COMPETITION_TASKS", {"board": board})
            self._competition_board_signature = signature

    @staticmethod
    def _execution_decision(decision):
        if decision.name != "force_attempt":
            return decision
        return Decision(decision.args["action"], {key: value for key, value in decision.args.items() if key != "action"},
                        decision.reason, decision.goal)

    def _report_blocked(self, decision, observation):
        if decision.name != "report_blocked":
            return None
        if self.journal.state.get("pending") or self._model_blocked or self._cancel.is_set():
            return SkillResult(False, "Recovery reporting cannot clear an unsettled action or model slot",
                               {"executed": False}, "action_unconfirmed")
        board = self.competition.snapshot() if self.competition else {}
        status = observation.get("status", {})
        basis = {"goal_id": board.get("active_goal_id"), "pos": status.get("pos"),
                 "dimension": status.get("dimension"), "last_death": status.get("last_death"),
                 "counts": observation.get("counts", {}),
                 "blocks": [{key: row.get(key) for key in ("name", "pos", "can_see")}
                            for row in observation.get("blocks", [])[:16] if isinstance(row, dict)]}
        previous = self.context.get("last_blockage") or {}
        attempts = previous.get("attempts", 0) + 1 if previous.get("basis") == basis else 1
        self.context["last_blockage"] = {"basis": copy.deepcopy(basis), "attempts": attempts,
            "reason": decision.reason[:240], "resolution": decision.args.get("resolution", "replan")}
        exit_task = decision.args.get("resolution") == "exit_task" or attempts >= 3
        data = {"executed": False, "replan_required": True, "attempts": attempts,
                "task_exited": False, "active_goal_id": board.get("active_goal_id")}
        if self.competition and board.get("active_goal_id"):
            if exit_task:
                result = self.competition.force_exit("blocked: " + decision.reason[:140])
                data.update(result.get("data", {}), task_exited=result["success"])
            else:
                self.competition.record_action("replan", {}, {"success": True, "data": {"replan_attempted": True}})
        data["phase"] = self.competition.snapshot()["phase"] if self.competition else "daily_reassessment"
        self._checkpoint(context=self.context)
        if self.competition:
            self._record("INFO", "COMPETITION_TASKS", {"board": self.competition.snapshot()})
        self._record("WARN", "BLOCKAGE_REPORTED", {"reason": decision.reason[:240], **data})
        return SkillResult(True, "Blockage recorded; choose a different plan or a supported forced attempt. Unknown locks and failure history are unchanged", data)

    def _competition_tool(self, decision, observation):
        """Handle metadata-only task tools at an idle, fresh execution boundary."""
        if not self.competition:
            return None
        names = {"select_task", "update_progress", "finish_task", "force_exit_task", "end_competition"}
        if decision.name not in names:
            canopy_clear = (decision.name == "clear_foliage"
                and self._active_decision is not None and self._active_decision.name == "force_attempt"
                and (self.context.get("retreat_recovery") or {}).get("canopy_blocked") is True)
            canopy_clear = canopy_clear or (decision.name == "descend_canopy" and self._local_step
                and (self.context.get("retreat_recovery") or {}).get("canopy_blocked") is True)
            if not canopy_clear and self.competition.snapshot().get("active_goal_id") is None and decision.name not in {
                "wait", "retreat_from", "eat_food", "auto_sleep"}:
                return SkillResult(False, "Select a scored goal before production actions", {"executed": False}, "task_selection_required")
            return None
        status = observation.get("status", {})
        if status.get("busy") or not status.get("ready") or not status.get("alive") or self._cancel.is_set():
            return SkillResult(False, "Task transition requires an idle fresh living observation", {"executed": False}, "task_transition_unsafe")
        if self.journal.state.get("pending"):
            return SkillResult(False, "Uncertain game intention must be reconciled before task transitions", {"executed": False}, "action_unconfirmed")
        if decision.name == "select_task":
            response = self.competition.select(decision.args["goal_id"])
        elif decision.name == "update_progress":
            response = self.competition.update_progress(decision.args["summary"])
        elif decision.name == "finish_task":
            response = self.competition.finish(decision.args["reason"])
        elif decision.name == "force_exit_task":
            response = self.competition.force_exit(decision.args["reason"])
        else:
            response = self.competition.end_competition(decision.args["reason"])
        self.context["competition"] = self.competition.state()
        self._checkpoint(context=self.context)
        self._record("INFO", "COMPETITION_TASKS", {"board": self.competition.snapshot()})
        return SkillResult(response["success"], response.get("message", ""), response.get("data", {}), response.get("error_code"))

    def _observe(self, *, safety_only=False):
        if callable(getattr(self.collector, "set_context", None)):
            self.collector.set_context(self.context)
        # Optional collector capability preserves custom collector injection.
        if callable(getattr(type(self.collector), "collect_safety", None)):
            recovery = self.context.get("retreat_recovery") or {}
            short = self.collector.collect_safety(self._cancel,
                force=safety_only or recovery.get("evacuating") is True,
                threat_radius=self._safety_rules.threat_radius)
            if short is not None:
                status = short["status"]
                hostiles = self._safety_rules._hostiles(short, status.get("pos"))
                nearby = status.get("nearest_hostile") or {}
                close = (bool(hostiles and hostiles[0][0] <= self._safety_rules.threat_radius)
                    or isinstance(nearby, dict) and self._safety_rules._number(nearby.get("dist"))
                    and nearby["dist"] <= self._safety_rules.threat_radius)
                needs_inventory = (self._safety_rules._number(status.get("health"))
                    and self._safety_rules._number(status.get("food"))
                    and status.get("on_ground") is True and not status.get("is_sleeping")
                    and not status.get("in_vehicle")
                    and not any(status.get(key) for key in ("in_water", "in_lava", "on_fire", "submerged"))
                    and status.get("dimension") in {"overworld", "minecraft:overworld"}
                    and status.get("gamemode") == "survival" and status["food"] < 20
                    and (status["health"] <= self._safety_rules.low_health
                         or status["food"] <= self._safety_rules.food_threshold))
                # A blocked canopy needs a full inventory/terrain turn before
                # the model may report or clear a single obstacle. Safety-only
                # snapshots must never authorize physical preparation.
                needs_canopy = (self.uses_llm and recovery.get("canopy_blocked") is True
                    and self._safety_rules._number(status.get("health"))
                    and status["health"] > self.settings["low_health_threshold"]
                    and self._safety_rules._number(status.get("food")) and status["food"] > 6
                    and status.get("on_ground") is True and not status.get("thundering")
                    and not any(status.get(key) for key in ("in_water", "in_lava", "on_fire", "submerged", "in_vehicle"))
                    and not any(distance <= max(12, self.settings["threat_radius"]) for distance, _ in hostiles)
                    and not (isinstance(nearby, dict) and self._safety_rules._number(nearby.get("dist"))
                             and nearby["dist"] <= max(12, self.settings["threat_radius"])))
                if safety_only or close or not (needs_inventory or needs_canopy) or not status.get("ready") or not status.get("alive") or status.get("busy"):
                    return short
        return self.collector.collect(self._cancel)

    def _local_decision(self, observation):
        """Prefer a fresh deterministic step before asking the model; never on unknown state."""
        if (not self.local_executor.enabled or self.journal.state.get("pending")
                or self._model_blocked or self._model_worker and self._model_worker.pending):
            return None
        if (self.context.get("local_execution") or {}).get("phase") == "blocked_unknown":
            return None
        gate = getattr(getattr(self.planner, "brain", None), "request_gate", None)
        if gate is not None and callable(getattr(gate, "snapshot", None)):
            slot = gate.snapshot()
            if isinstance(slot, dict) and slot.get("state") != "idle":
                return None
        if observation.get("fresh") is not True or observation.get("observation_scope") == "safety":
            return None
        board = self.competition.model_view() if self.competition else None
        decision = self.local_executor.choose(observation, self.context, competition=board, daily_board=self.daily_board)
        if decision is None:
            return None
        execution = self._execution_decision(decision)
        blocked = self._unsafe_destination(execution, observation)
        if self.daily_policy:
            if decision.name == "descend_canopy":
                blocked = blocked or self.daily_policy._force_environment_error(observation)
            else:
                validation = self.daily_policy.validate_force if decision.name == "force_attempt" else self.daily_policy.validate
                blocked = blocked or validation(execution, observation)
        if blocked:
            self.local_executor.reject(self.context, blocked)
            self._record("WARN", "LOCAL_EXECUTION_YIELDED", {"reason": blocked, "summary": self.local_executor.summary(self.context)})
            return None
        self._local_step = True
        self._record("INFO", "LOCAL_EXECUTION_STEP", {"decision": asdict(decision), "summary": self.local_executor.summary(self.context)})
        return decision

    def _model_decision(self, observation):
        worker = self._model_worker
        if worker.pending:
            if not worker.done:
                return None  # A cancelled remote request must finish before any new one.
            decision = worker.take(observation)
            if decision is not None:
                return decision
        self._safety_baseline = copy.deepcopy(observation["status"])
        model_context = self.context
        if self.competition:
            model_context = copy.deepcopy(self.context)
            # Competition goals are the primary board. Do not duplicate the
            # entire daily-production catalogue or its unrelated top-3 hints.
            model_context.pop("daily_tasks", None)
            model_context.pop("action_hints", None)
            model_context["competition"] = self.competition.model_view()
        recovery = self.context.get("retreat_recovery") or {}
        if recovery.get("canopy_blocked") is True:
            if model_context is self.context:
                model_context = copy.deepcopy(self.context)
            model_context["recovery"] = {"kind": "canopy", "goal": "Reach freshly verified solid ground before resource work",
                "blocked_cells": copy.deepcopy(recovery.get("blocked_cells", [])[:4]),
                "instruction": "Report the blockage, or force_attempt one verified adjacent leaf clearance when no explicit danger exists. Do not mine support or jump blindly; the harness rechecks descent."}
        if self.local_executor.enabled:
            if model_context is self.context:
                model_context = copy.deepcopy(self.context)
            model_context["local_execution"] = self.local_executor.summary(self.context)
            model_context["blocked_targets"] = list(dict.fromkeys(
                list(model_context.get("blocked_targets") or [])
                + (self.context.get("local_execution") or {}).get("failed_targets", [])))
            self._record("WARN", "MODEL_TAKEOVER", {"summary": model_context["local_execution"]})
        worker.start(observation, model_context)
        while not worker.done:
            if self._cancel.wait(.05):
                worker.cancel()
                code = "llm_cancelled" if self.paused else "llm_preempted"
                raise LLMError(code, "Local safety or pause superseded model planning; response will be drained")
        return worker.take(observation)

    def _daily_result(self, decision, result, observation):
        if not self.daily:
            return
        if observation and observation.get("observation_scope") == "safety":
            return  # Do not persist partial counts as authoritative inventory.
        if self.memory:
            self.memory.record_result(decision, result, observation)
        status = (observation or {}).get("status") or {}
        data = result.data
        if decision.name == "plan_shelter" and result.success and (data.get("geometry") or {}).get("floor_verified"):
            self.context["base_project"] = {"pos": decision.args["pos"], "material": decision.args["material"],
                                            "dimension": status.get("dimension"), "source": "skill_verified"}
            if self.memory and status.get("dimension"):
                self.memory.remember_place("home", "shelter_site", decision.args["pos"], dimension=status["dimension"], source="skill_verified")
        if decision.name == "smelt_start" and (result.success or result.error_code == "partial_completion"):
            from skills.daily_survival import DailySurvivalSkills
            jobs = [row for row in self.context.get("smelting_jobs", []) if row.get("pos") != decision.args["pos"]][-7:]
            jobs.append({"pos": decision.args["pos"], "dimension": status.get("dimension"),
                         "expected_output": DailySurvivalSkills.SMELTABLE.get(decision.args["input_item"]),
                         "stage": "started", "requires_revalidation": True})
            self.context["smelting_jobs"] = jobs
        if decision.name == "smelt_collect" and result.success and observation:
            empty = any(row.get("pos") == decision.args["pos"] and row.get("fresh") is True
                        and (row.get("input") or {}).get("count") == 0 and (row.get("output") or {}).get("count") == 0
                        for row in observation.get("furnaces", []))
            if empty:
                self.context["smelting_jobs"] = [row for row in self.context.get("smelting_jobs", []) if row.get("pos") != decision.args["pos"]]

    def _update_lifecycle(self, status):
        """Status-only reads update life markers without inventing an empty inventory."""
        dimension = status.get("dimension")
        old_dimension = self.context.get("last_dimension")
        if dimension and old_dimension and dimension != old_dimension:
            self.context.update(failures={}, blocked_until={}, blocked_targets=[], explore_index=0, spawn=None)
            self.context.pop("retreat_recovery", None)
            self._record("WARN", "DIMENSION_CHANGED", {"from": old_dimension, "to": dimension})
        if dimension:
            self.context["last_dimension"] = dimension
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
            self.context.pop("retreat_recovery", None)
            self._record("WARN", "DEATH_OBSERVED", {"last_death": death, "count": self.context["death_count"]})
            self._death_awaiting_marker = not bool(new_marker)
        if new_marker:
            self._death_awaiting_marker = False
        if marker:
            self.context["last_death"] = marker
        self._saw_observation = True
        if isinstance(status.get("alive"), bool):
            self.context["was_alive"] = status["alive"]
        if status.get("alive") and status.get("ready") and not self.context.get("spawn"):
            self.context["spawn"] = status.get("pos")

    def _cycle(self, manual):
        decision = Decision("observe", {}, "Read current state", "observe")
        observation = None
        self._local_step = False
        try:
            self._phase("observing")
            observation = self._observe()
            self.latest_status = observation["status"]
            self._prepare_context(observation)
            step_limit = (self.settings.get("competition", {}).get("max_steps", 100000)
                          if self.competition_mode else self.settings["max_steps"])
            remaining = (self.competition.snapshot().get("remaining_seconds", 0) if self.competition else
                         self.settings["max_runtime_seconds"] - (time.monotonic() - self._started))
            self.context.update(remaining_steps=max(0, step_limit - self.steps),
                                remaining_seconds=max(0, remaining))
            if self._cancel.is_set():
                return decision, SkillResult(False, "Cancelled before action", error_code="cancelled"), observation
            if self.context["death_count"] >= self.settings["max_deaths"]:
                return decision, SkillResult(False, "Death limit reached", error_code="death_limit"), observation
            if manual is not None and (not observation["status"].get("ready")
                                       or not observation["status"].get("alive") or observation["status"].get("busy")):
                decision = Decision("wait", {"seconds": 1}, "Manual action needs an idle, living, ready bot", "observe")
                return decision, SkillResult(False, decision.reason, error_code="busy"), observation
            if observation.get("observation_scope") == "safety":
                if self._model_worker and self._model_worker.pending:
                    self._model_worker.cancel()
                decision = self._mandatory_safety(observation)
                if decision is None or decision.name not in {"retreat_from", "wait"}:
                    decision = Decision("wait", {"seconds": 1}, "Short safety observation cannot authorize production", "observe")
                self._record("WARN", "SAFETY_OVERRIDE", {"decision": asdict(decision), "status": observation["status"],
                    "observation_scope": "safety"})
            elif manual is not None:
                decision = manual
            elif self.uses_llm:
                safety = self._mandatory_safety(observation)
                if safety is not None:
                    if self._model_worker and self._model_worker.pending:
                        self._model_worker.cancel()
                    decision = safety
                    self._record("WARN", "SAFETY_OVERRIDE", {"decision": asdict(safety), "status": observation["status"]})
                else:
                    local = self._local_decision(observation)
                    if local is not None:
                        decision = local
                    else:
                        if self._model_blocked:
                            decision = Decision("wait", {"seconds": 5}, "Model request outcome is uncertain; local safety remains active", "observe")
                            return decision, SkillResult(True, decision.reason, {"waiting": True, "model_blocked": self._model_blocked}), observation
                        self._phase("thinking")
                        before_life = self._life_marker(observation)
                        decision = self._model_decision(observation)
                        if decision is None:
                            decision = Decision("wait", {"seconds": 1}, "Previous model request is draining; local safety remains active", "observe")
                            if self._cancel.is_set():
                                return decision, SkillResult(False, "Planning cancelled; no action sent", {"executed": False}, "cancelled"), observation
                            return decision, SkillResult(True, decision.reason, {"waiting": True, "model_pending": True}), observation
                        self._model_feedback = True
                        if self._cancel.is_set():
                            return decision, SkillResult(False, "Paused while the model was planning; no action sent", error_code="cancelled"), observation
                        # A slow model must not act on pre-death inventory or an old
                        # daytime/danger snapshot. Skills then recheck target details.
                        self._phase("rechecking")
                        observation = self._observe()
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
                        execution = self._execution_decision(decision)
                        unsafe_target = self._unsafe_destination(execution, observation)
                        if self.daily_policy:
                            validation = self.daily_policy.validate_force if decision.name == "force_attempt" else self.daily_policy.validate
                            unsafe_target = unsafe_target or validation(execution, observation)
                        if decision.name == "force_attempt" and self.journal.state.get("pending"):
                            unsafe_target = "action_unconfirmed"
                        if unsafe_target:
                            self._record("WARN", "SAFETY_OVERRIDE", {"model_decision": asdict(decision), "reason": unsafe_target})
                            return decision, SkillResult(False, unsafe_target, {"executed": False}, "safety_changed"), observation
                        canopy_control = (decision.name in {"force_attempt", "report_blocked"}
                            and (self.context.get("retreat_recovery") or {}).get("canopy_blocked") is True
                            and self.daily_policy is not None
                            and self.daily_policy._force_environment_error(observation) is None)
                        safety = None if canopy_control else self._mandatory_safety(observation)
                        if safety is not None and (safety.name != decision.name or safety.args != decision.args):
                            self._record("WARN", "SAFETY_OVERRIDE", {"model_decision": asdict(decision), "decision": asdict(safety)})
                            return decision, SkillResult(False, "Fresh local safety check rejected the model action; replan",
                                                         {"executed": False, "safety_decision": asdict(safety)}, "safety_changed"), observation
            else:
                decision = self.planner.choose(observation, self.context)
                decision = self._retreat_decision(observation, decision)
                if self.daily:
                    safety = self.daily_policy.choose(observation, decision, self.daily_board)
                    decision = safety or self.daily_goals.choose(observation, self.context.get("memory"), self.context)
                    if decision is not None and not safety and self.context.get("survival_constraints", {}).get("scenario") == "night_preparation":
                        blocked = self.daily_policy.validate(decision, observation)
                        if blocked:
                            decision = Decision("wait", {"seconds": 5}, blocked, "safety")
                    if decision is None:
                        decision = Decision("wait", {"seconds": 5}, "No verified daily action is currently available", "observe")
            self._active_decision = decision
            self._safety_baseline = copy.deepcopy(observation["status"])
            self._record("INFO", "DECISION", {"decision": asdict(decision), "status": observation["status"],
                                               "counts": observation["counts"], "milestones": self.context["milestones"],
                                               "source": "local" if self._local_step else "model" if self._model_feedback else "manual" if manual else "safety" if self.uses_llm else "rules"})
            execution = self._execution_decision(decision)
            reported = self._report_blocked(decision, observation)
            if reported is not None:
                return decision, reported, observation
            task_result = self._competition_tool(execution, observation)
            if task_result is not None:
                return decision, task_result, observation
            if decision.name == "wait":
                self._phase("waiting")
                return decision, SkillResult(True, decision.reason, {"waiting": True}), observation
            if decision.name == "goal_complete":
                if self.daily:
                    return decision, SkillResult(False, "Daily survival continues with maintenance; there is no final goal_complete", error_code="goal_unverified"), observation
                if self.uses_llm and not self._safety_rules.progress(observation)["starter_goal_complete"]:
                    return decision, SkillResult(False, "Latest inventory does not verify goal completion", error_code="goal_unverified"), observation
                return decision, SkillResult(True, decision.reason, {"goal_complete": True}), observation
            self._checkpoint(context=self.context, pending={"decision": asdict(decision), "before_counts": observation["counts"],
                "before_counts_fresh": observation.get("inventory_fresh", True)})
            self._phase("acting")
            if execution.name == "auto_sleep":
                options = {key: value for key, value in self.sleep_settings.items() if key in SleepController.SKILL_OPTIONS}
                result = AutoSleepSkill(self.client).execute(**options, cancel_event=self._cancel)
            else:
                options = {"recovery_context": self._retreat_options(observation, execution)} if execution.name == "retreat_from" else {}
                runner = self.shelter_skills if self.daily and execution.name in {"plan_shelter", "inspect_shelter", "build_shelter_step"} else self.skills
                if execution.name == "descend_canopy":
                    runner = self.canopy_skills
                elif self.daily and execution.name in {"gather_materials", "build_shelter_batch"}:
                    runner = self.batch_skills
                elif self.daily and execution.name == "set_door":
                    runner = self.door_skills
                elif self.daily and execution.name == "position_for_task":
                    runner = self.positioning_skills
                if decision.name == "force_attempt":
                    options.update(force_advisory=True, force_threat_radius=max(12, self.settings["threat_radius"]))
                result = runner.execute(execution.name, execution.args, cancel_event=self._cancel, **options)
                if decision.name == "force_attempt" and isinstance(result, SkillResult):
                    result.data.update(forced_attempt=True, requested_action=execution.name)
                    if result.success and execution.name == "clear_foliage" and result.data.get("block_removed") is True:
                        recovery = self.context.get("retreat_recovery") or {}
                        if recovery.get("canopy_blocked") is True:
                            recovery.pop("retry_after", None)
                            self._record("INFO", "CANOPY_PREPARATION", {"cleared": execution.args.get("pos"), "reobserve_before_descent": True})
                    self._record("INFO" if result.success else "WARN", "FORCED_ATTEMPT_RESULT",
                                 {"action": execution.name, "reason": decision.reason[:240], "result": asdict(result)})
            if not isinstance(result, SkillResult):
                raise TypeError("Skill returned an invalid result")
            if (self._local_step or self._model_feedback or self.daily) and not self._cancel.is_set() and not _uncertain_result(result):
                try:
                    observation = self._observe(safety_only=decision.name == "retreat_from")
                    self.latest_status = observation["status"]
                    self._prepare_context(observation)
                except APIError as error:
                    self._record("WARN", "POST_OBSERVATION_FAILED", {"code": error.code})
                    # Keep the skill's verified result, but mark the feedback
                    # observation stale; the next turn must observe anew.
                    observation = {**observation, "fresh": False}
            elif _uncertain_result(result):
                observation = {**observation, "fresh": False}
            return decision, result, observation
        except LLMError as exc:
            if exc.code == "llm_preempted":
                decision = Decision("wait", {"seconds": 0.25}, str(exc), "safety")
                return decision, SkillResult(True, str(exc), {"executed": False, "model_pending": True}), observation
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
        planning = self.phase == "thinking" and self._model_worker is not None and self._model_worker.pending
        if (decision is None and not planning) or (decision is not None and decision.name in ("retreat_from", "eat_food", "auto_sleep")):
            return
        try:
            status = self.client.get_status()["result"]
            self.latest_status = status
        except APIError:
            self.pause("status_unavailable_during_action")
            return
        hostile = status.get("nearest_hostile") or {}
        # A locally permitted low-health food turn must not be cancelled just
        # because its initial health is low. Any new deterioration still wins.
        baseline = self._safety_baseline or {}
        constraints = self.context.get("survival_constraints") or {}
        food_turn = False
        if self.daily and constraints.get("scenario") == "low_health" and constraints.get("model_allowed") is True:
            from core.daily_policy import FOOD_ACTIONS
            food_turn = (planning or decision is not None and decision.name in FOOD_ACTIONS) and (
                status.get("dimension") == baseline.get("dimension")
                and status.get("last_death") == baseline.get("last_death")
                and status.get("health", 0) >= baseline.get("health", 20)
                and status.get("food", 0) >= baseline.get("food", 20)
                and status.get("pos") == baseline.get("pos")
                and status.get("on_ground") is True and status.get("is_day") is True
                and not status.get("thundering") and status.get("time_of_day", 24000) < 11000)
        night_preparation_danger = False
        if self.daily and (constraints.get("scenario") in {"night_preparation", "advisory_blocked"}
                           or decision is not None and decision.name == "force_attempt"):
            health = status.get("health")
            food = status.get("food")
            night_preparation_danger = (
                not self._safety_rules._number(health) or health < baseline.get("health", 20)
                or not self._safety_rules._number(food)
                or food < (7 if constraints.get("scenario") == "advisory_blocked" or decision is not None and decision.name == "force_attempt" else 12)
                or status.get("thundering")
                or (self._safety_rules._number(hostile.get("dist"))
                    and hostile["dist"] <= max(12, self.settings["threat_radius"])))
        danger = (night_preparation_danger or not status.get("alive") or status.get("in_lava") or status.get("on_fire")
                  or status.get("submerged") or status.get("in_water")
                  or status.get("health", 20) <= self.settings["low_health_threshold"] and not food_turn
                  or (isinstance(hostile.get("dist"), (int, float)) and hostile["dist"] <= self.settings["threat_radius"]))
        if danger:
            self._cancel.set()
            if planning:
                self._model_worker.cancel()
            else:
                self.client.stop()
            self._record("WARN", "SAFETY_INTERRUPT", {"status": status, "model_pending": planning})

    def _monitor_idle(self):
        """Read-only lifecycle checks remain active while paused or backing off."""
        now = time.monotonic()
        if now < self._next_idle_monitor:
            return
        self._next_idle_monitor = now + max(RETREAT_WAIT_SECONDS, self.settings["poll_interval_seconds"])
        try:
            status = self.client.get_status(timeout_seconds=2.0)["result"]
        except APIError as error:
            self._record("WARN", "IDLE_STATUS_UNAVAILABLE", {"code": error.code})
            return
        self.latest_status = status
        before = (self.context.get("death_count"), self.context.get("last_dimension"))
        self._update_lifecycle(status)
        hostile = status.get("nearest_hostile") or {}
        danger = (status.get("alive") is False or any(status.get(key) for key in ("in_lava", "on_fire", "submerged"))
                  or (self._safety_rules._number(status.get("health")) and status["health"] <= self.settings["low_health_threshold"])
                  or (isinstance(hostile, dict) and self._safety_rules._number(hostile.get("dist"))
                      and hostile["dist"] <= self.settings["threat_radius"]))
        changed = before != (self.context.get("death_count"), self.context.get("last_dimension"))
        if not self.paused and (danger or changed):
            self._next_attempt = min(self._next_attempt, now)
        if not self.paused and self.context["death_count"] >= self.settings["max_deaths"]:
            self._paused = True
            self.pause_reason = "death_limit"
        # Preserve pending and pause reason, including manual/unknown-result pauses.
        self._checkpoint(context=self.context)

    def tick(self, automatic=True):
        if self._closed:
            return None
        time_expired = (self.competition.snapshot().get("remaining_seconds", 0) <= 0
                        if self.competition is not None else
                        time.monotonic() - self._started >= self.settings["max_runtime_seconds"])
        if not self.finished and time_expired:
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
            self._daily_result(self._execution_decision(decision), result, observation)
            if self._local_step:
                board = self.competition.model_view() if self.competition else None
                self.local_executor.record(decision, result, observation, self.context, competition=board)
                self._record("INFO", "LOCAL_EXECUTION_RESULT", {"decision": asdict(decision), "result": asdict(result),
                             "summary": self.local_executor.summary(self.context)})
            if self.competition and decision.name not in {"select_task", "update_progress", "finish_task", "force_exit_task", "end_competition"}:
                self.competition.record_action(decision.name, decision.args, asdict(result), observation)
                self.context["competition"] = self.competition.state()
                self._record("INFO", "COMPETITION_TASKS", {"board": self.competition.snapshot()})
            if decision.goal == "explore" and decision.name == "move_to":
                self.context["explore_index"] = self.context.get("explore_index", 0) + 1
            delay = self.settings["poll_interval_seconds"]
            retreat_replan = self._retreat_result(decision, result, observation)
            if not result.success:
                if retreat_replan:
                    delay = RETREAT_WAIT_SECONDS
                else:
                    self.failures += 1
                    key = self._target(decision) if decision.name != "retreat_from" else "retreat_from"
                    counts = self.context.setdefault("failures", {})
                    counts[key] = counts.get(key, 0) + 1
                    if counts[key] >= self.settings["max_target_failures"]:
                        self.context.setdefault("blocked_until", {})[key] = time.time() + self.settings["target_cooldown_seconds"]
                    delay = min(60, self.settings["retry_seconds"] * 2 ** min(self.failures - 1, 4))
                uncertain_action = _uncertain_result(result) and decision.name != "observe"
                http_status = result.data.get("http_status")
                transient_model_error = (result.error_code in {"llm_timeout", "llm_network_error", "llm_busy"}
                    or (result.error_code == "llm_http_error" and isinstance(http_status, int)
                        and (http_status in {408, 425, 429} or 500 <= http_status < 600)))
                fatal_model_error = result.error_code == "llm_http_error" and not transient_model_error
                model_uncertain = result.error_code in {"llm_request_uncertain", "llm_gate_corrupt"}
                if self.daily and model_uncertain:
                    self._model_blocked = result.error_code
                    self._record("WARN", "MODEL_BLOCKED_SAFETY_ACTIVE", {"code": result.error_code})
                if (uncertain_action or fatal_model_error
                    or result.error_code in FATAL | {"death_limit", "internal", "internal_error", "llm_invalid_config", "llm_record_error", "llm_missing_feedback", "llm_request_uncertain", "llm_gate_corrupt"}
                    or (not retreat_replan and not transient_model_error and self.failures >= self.settings["max_consecutive_failures"])):
                    self._paused = True
                    self.pause_reason = result.error_code or "repeated_failures"
                    if self.daily and model_uncertain and not uncertain_action:
                        self._paused = False
                        self.pause_reason = None
                if transient_model_error:
                    self._record("WARN", "MODEL_RETRY_SCHEDULED", {"code": result.error_code, "http_status": http_status,
                                 "retry_seconds": delay, "consecutive_failures": self.failures})
                # Dig recovery already made a bounded stop attempt, including
                # when that attempt failed. Do not block the main tick with a
                # second default-timeout request; the unknown result stays paused.
                dig_cleanup_attempted = result.error_code == "dig_unconfirmed" and (
                    result.data.get("cleanup") == "stop_requested" or "cleanup_error" in result.data)
                if uncertain_action and not dig_cleanup_attempted:
                    try:
                        options = {"timeout_seconds": 2.0} if result.error_code == "dig_unconfirmed" else {}
                        self.client.stop(**options)
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
                elif (decision.name == "retreat_from" and result.data.get("retreat_complete") is False
                      and result.data.get("evacuation_stage") == "relay"):
                    delay = 0  # Next tick reobserves; never replay the saved route.
                terminal_competition = bool(
                    self.competition and self.competition.snapshot().get("phase") == "completed")
                self.completed = decision.name == "goal_complete" or terminal_competition
                self.finished = self.completed
            if self._budget_exhausted:
                self.finished = True
                self.pause_reason = "budget_exhausted"
            if self._local_step and decision.name != "wait" and result.success and not self.paused and not self.finished:
                delay = 0  # Continue local verified steps without a model-round or retry delay.
            self._next_attempt = time.monotonic() + delay
            self.phase = "paused" if self.paused else "complete" if self.completed else "waiting"
            # Keep uncertain intentions for explicit recovery after a restart.
            keep_pending = ((_uncertain_result(result) and decision.name != "observe")
                            or (decision.name in {"observe", "wait"} and (observation is None
                                or not observation["status"].get("ready") or observation["status"].get("busy"))))
            self._checkpoint(context=self.context, last_result=asdict(result),
                                pending=self.journal.state.get("pending") if keep_pending else None)
            self._record("INFO" if result.success else "WARN", "STEP_RESULT",
                         {"step": self.steps, "decision": asdict(decision), "result": asdict(result), "paused": self.paused})
            return result
        if self.finished:
            return None
        step_limit = (self.settings.get("competition", {}).get("max_steps", 100000)
                      if self.competition_mode else self.settings["max_steps"])
        if self.steps >= step_limit or self._budget_exhausted:
            self.finished = True
            self.pause_reason = "budget_exhausted"
            self._record("WARN", "BUDGET_EXHAUSTED", {"steps": self.steps})
        else:
            if self.paused or time.monotonic() < self._next_attempt:
                self._monitor_idle()
            if not self.paused and automatic and time.monotonic() >= self._next_attempt:
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
            if self._model_worker:
                self._model_worker.close(self.last_observation)
            if self.memory:
                self.memory.close()
            self._closed = True
