"""Observe -> plan -> one verified action -> checkpoint, with bounded recovery."""

import copy
import json
import math
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import asdict

from agent.survival_planner import Decision, RANGED_HOSTILES, SurvivalPlanner
from core.campaign import Campaign, dimension as campaign_dimension
from core.client import APIError, connection_retry_delay
from core.combat_reaction import CombatReaction
from core.continuous_recovery import ContinuousRecovery
from core.model_gate import ModelGateError
from core.config import continuous_daily
from core.foliage_reconciliation import confirm_foliage, foliage_intent, pending_foliage, resolved_result
from core.retreat_controller import (
    RETREAT_REPLAN,
    RETREAT_SEARCH_COOLDOWN,
    RETREAT_WAIT_SECONDS,
    RetreatController,
)
from core.skill_registry import STARTER_MINING_SKILLS, destination_checked_skills, resolve_skill_runner
from core.sleep_controller import SleepController
from core.survival_state import ObservationCollector, SurvivalJournal
from core.shelter_project import active_project, completion_observed, project_anchor, recover_project, same_site, verified_build_anchor
from core.survival_mainline import same_goal
from skills import SkillResult
from skills.sleep import AutoSleepSkill
from skills.survival import SurvivalSkills
from agent.brain import LLMError
from core.autonomy_runtime import AutonomyRuntime
from core.local_recovery_runtime import LocalRecoveryRuntime


# The API can restore its original block cache when no server acknowledgement
# arrives. An unchanged cached block does not prove the dig never happened.
UNCERTAIN = {"request_timeout", "network_error", "invalid_json", "invalid_response", "dig_unconfirmed", "action_unconfirmed"}
FATAL = {"unauthorized", "invalid_credentials", "death_loop", "death_loop_cooldown"}


def _uncertain_result(result):
    return (result.error_code in UNCERTAIN | {"internal", "internal_error"}
            or isinstance(result.data, dict) and result.data.get("action_state") == "unknown")


class SurvivalHarness(AutonomyRuntime, LocalRecoveryRuntime, ContinuousRecovery):
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
        self._automatic_cycle = False
        self.competition_mode = settings.get("objective", "starter") == "competition"
        self.competition = None
        self._competition_board_signature = None
        self._competition_advancements = None
        self._competition_advancements_fetched_at = None
        self._competition_advancements_fetched_monotonic = None
        self._competition_next_advancement = 0.0
        self.daily = settings.get("objective", "starter") in {"daily", "competition"}
        self.continuous = continuous_daily(settings)
        if self.competition_mode and not self.uses_llm:
            raise ValueError("competition requires a model planner; no rule fallback")
        self.daily_goals = None
        self.daily_policy = None
        self.memory = None
        self.shelter_skills = None
        self.shelter_preparation_skills = None
        self.starter_staircase_skills = None
        self.batch_skills = None
        self.door_skills = None
        self.positioning_skills = None
        self.combat_skills = None
        self.bucket_skills = None
        self.daily_board = None
        self.playbook = None
        self._daily_board_signature = None
        self._model_blocked = None
        self._model_worker = None
        self._safety_baseline = None
        self._combat_reaction = CombatReaction()
        self._last_reaction_hit = None
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
            from core.survival_mainline import mainline_enabled
            from core.daily_policy import DailySafetyPolicy
            from skills.daily_survival import DailySurvivalSkills
            from skills.shelter import ShelterSkills
            from skills.shelter_preparation import ShelterPreparationSkills
            from skills.starter_staircase import StarterStaircaseSkills
            from skills.batch import BatchSkills
            from skills.doors import DoorSkills
            from skills.positioning import PositioningSkills
            from skills.combat import CombatSkills
            from skills.buckets import BucketSkills
            self.daily_goals = DailySurvivalGoals(settings)
            self.daily_policy = DailySafetyPolicy(settings)
            self.skills = skills or DailySurvivalSkills(client, allow_herd_hunting=settings.get("campaign") is True)
            self.collector = collector or DailyObservationCollector(client, settings["scan_radius"],
                low_health_threshold=settings["low_health_threshold"],
                campaign=settings.get("campaign") is True and settings.get("intent") == "speedrun",
                mainline=mainline_enabled(settings) or (settings.get("campaign") is True and settings.get("intent") == "speedrun"))
            self.shelter_skills = ShelterSkills(client)
            self.shelter_preparation_skills = ShelterPreparationSkills(client)
            self.starter_staircase_skills = StarterStaircaseSkills(client)
            self.batch_skills = BatchSkills(client, gather_skills=self.skills, shelter_skills=self.shelter_skills)
            self.door_skills = DoorSkills(client)
            self.positioning_skills = PositioningSkills(client)
            self.combat_skills = CombatSkills(client)
            self.bucket_skills = BucketSkills(client)
            from core.survival_playbook import SurvivalPlaybook
            from agent.daily_survival import DAILY_SKILLS
            self.playbook = SurvivalPlaybook(available_tools=DAILY_SKILLS, goal=settings.get("intent", "survival"))
        self.journal = journal or SurvivalJournal(settings["state_dir"], endpoint=client.base_url,
                                                username=client.username, world_id=settings["world_id"])
        self.context = copy.deepcopy(self.journal.state.get("context", {}))
        self.campaign = None
        if self.daily and settings.get("campaign") is True and settings.get("intent") == "speedrun":
            from core.build_projection import load_projection
            from core.memory import MemoryEngine
            from skills.campaign import CampaignSkills
            preset = settings.get("projection")
            projection = load_projection(preset) if preset else None
            self.campaign = Campaign(self.context.setdefault("campaign", {}), projection=projection)
            self.campaign_skills = CampaignSkills(client, memory=MemoryEngine(str(self.journal.directory / "campaign")),
                                                  projection=projection)
        self.context.setdefault("failures", {})
        self.context.setdefault("blocked_until", {})
        self.context.setdefault("explore_index", 0)
        # The API may omit last_death even after respawning. Keep our observed
        # life generation across restarts independently of the per-run budget.
        self.context.setdefault("life_generation", self.context.get("death_count", 0))
        self.context["death_count"] = 0
        self._retreat = RetreatController(self.context, self.settings, self._record)
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
        self._init_autonomy()
        self._pending_leaf_readback = pending_foliage(self.journal) if self._paused else None
        self._foliage_respawn_checked = None
        if self._pending_leaf_readback is not None and (self.journal.state.get("pending") or {}).get("foliage_manually_paused"):
            self.pause_reason = "user_requested"
        elif self._pending_leaf_readback is not None:
            # The worker may observe this particular leaf. Its next cycle cannot
            # enter planning or send a write before the old outcome is resolved.
            self._paused, self.pause_reason = False, None
        self._init_continuous_recovery()
        if self._paused:
            self._record("WARN", "RECOVERY_REQUIRED", {"reason": self.pause_reason})
        if self.daily and self.context.get("shelter_project_migration") != 1:
            recovered = (recover_project(self.journal)
                         if project_anchor(self.context.get("shelter_project"), scope=self.journal.scope) is None else None)
            if recovered is not None:
                self.context["shelter_project"] = recovered
                if recovered.get("complete") is not True:
                    self.context["base_project"] = {key: recovered[key] for key in ("pos", "material", "dimension", "orientation")}
                self._record("INFO", "SHELTER_PROJECT_RECOVERED", {"project": recovered,
                    "note": "Historical verified placement recovered as intent; current geometry remains required"})
            self.context["shelter_project_migration"] = 1
            self._checkpoint(context=self.context)

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
                "continuous": self.continuous, "death_count": self.context["death_count"],
                "planner": "llm" if self.uses_llm else "rules",
                "objective": "competition" if self.competition_mode else "daily" if self.daily else "starter",
                "competition": self.competition.snapshot() if self.competition else None,
                "model_blocked": self._model_blocked,
                "model_pending": bool(self._model_worker and self._model_worker.pending),
                "pending": copy.deepcopy(self.journal.state.get("pending")),
                "daily_tasks": self.daily_board,
                "playbook": copy.deepcopy(self.context.get("playbook")),
                "mainline": copy.deepcopy(self.context.get("mainline")),
                "campaign": copy.deepcopy(self.campaign.state.get("view")) if self.campaign else None,
                "combat": copy.deepcopy((self.last_observation or {}).get("combat")),
                "task_execution": self._task_snapshot(),
                "memory_summary": {key: len(value) for key, value in self.context.get("memory", {}).items() if isinstance(value, list)},
                "retry_in_seconds": max(0, round(self._next_attempt - time.monotonic(), 1)),
                "goal": self._active_decision.goal if self._active_decision else None,
                "decision": asdict(self._active_decision) if self._active_decision else None}

    def _phase(self, phase):
        self.phase = phase
        self._record("INFO", "AGENT_PHASE", {"phase": phase, "planner": "llm" if self.uses_llm else "rules"})

    def _mandatory_safety(self, observation):
        """Only safety decisions may override the model, never its resource plan."""
        decision = self._safety_rules.choose(observation, self.context)
        decision = self._retreat_decision(observation, decision)
        if self.daily_policy:
            decision = self.daily_policy.choose(observation, decision, self.daily_board, context=self.context)
            decision = self._local_recovery_decision(decision, observation)
            recovery = self.context.get("combat_retreat")
            if isinstance(recovery, dict):
                status = observation["status"]
                target = next((row for row in observation.get("entities", [])
                               if row.get("id") == recovery.get("entity_id") and row.get("name") == recovery.get("name")), {})
                pos = self._safety_rules._pos(target.get("pos"))
                if (status.get("dimension") != recovery.get("dimension")
                        or status.get("last_death") != recovery.get("last_death") or pos is None
                        or math.dist(status["pos"], pos) > self._safety_rules._threat_distance(target.get("name"))):
                    self.context.pop("combat_retreat", None)
                elif decision is None or decision.name in {"fight_hostile", "retreat_from"}:
                    return self._retreat_decision(observation, Decision("retreat_from", {"pos": pos, "distance": 8},
                        "The previous combat step requires withdrawal before another attack", "safety"))
            if decision is None:
                return None
            if decision.name == "fight_hostile" and self.context.get("combat_blocked_until", {}).get(str(decision.args["entity_id"]), 0) > time.time():
                target = next((row for row in observation.get("entities", [])
                               if row.get("id") == decision.args["entity_id"]), {})
                pos = self._safety_rules._pos(target.get("pos"))
                if pos is not None:
                    return self._retreat_decision(observation, Decision("retreat_from", {"pos": pos, "distance": 8},
                        "The last combat step was not verified; withdraw instead of repeating it", "safety"))
        if decision.goal in {"safety", "recover", "observe", "food", "food_recovery", "sleep"}:
            return decision
        return None

    def _combat_priority(self, observation, hit):
        if hit is None or not observation.get("fresh"):
            return None
        status = observation["status"]
        wait = Decision("wait", {"seconds": .15}, "Recheck the attacker and footing after taking damage", "combat_reaction")
        if status.get("busy") or not status.get("ready") or not status.get("alive"):
            return wait
        if self.campaign is not None:
            water = self.campaign.water_recovery(status)
            if water is not None:
                return water
        if any(status.get(key) for key in ("in_water", "submerged", "in_lava", "on_fire", "is_sleeping", "in_vehicle")):
            return self._mandatory_safety(observation) or wait
        retreat = self.context.get("retreat_recovery") or {}
        retry_after = retreat.get("retry_after", 0)
        failed_from = self._safety_rules._pos((retreat.get("search_basis") or {}).get("pos"))
        position = self._safety_rules._pos(status.get("pos"))
        failed_long_route = (retry_after > time.time() and failed_from is not None and position is not None
                             and math.dist(position, failed_from) <= 1.5)
        defense = self._mandatory_safety(observation)
        if failed_long_route:
            retreat["retry_after"] = retry_after
        if defense is not None and defense.name == "fight_hostile":
            return defense
        reported = hit.get("attacker") or {}
        target = next((row for row in observation.get("entities", [])
                       if type(reported.get("id")) is int and row.get("id") == reported["id"]
                       and row.get("type") in {"hostile", "player"}), None)
        if self.campaign is not None:
            reaction = self.campaign.melee_recovery(defense or wait, observation, retreat, under_attack=True)
            if reaction.name == "campaign_action" and (reaction.args.get("operation") == "defend_melee"
                                                       or reaction.args.get("imminent_explosion")):
                return reaction
            nearest = status.get("nearest_hostile") or {}
            if (status.get("on_ground") is True and (target is not None or nearest)
                    and (failed_long_route or campaign_dimension(status) != "overworld"
                         or defense is None or defense.name == "wait"
                         or (target or nearest).get("name") in RANGED_HOSTILES)):
                if self.campaign.state.get("escape_retry_after", 0) <= time.time():
                    return self.campaign.action("recovery", "escape_pressure", "Break line of sight or leave the attacker",
                                                attacker_id=(target or {}).get("id"))
                return wait
            if (status.get("on_ground") is True and target is None and not nearest
                    and hit["t"] != self._last_reaction_hit):
                return self.campaign.action("recovery", "escape_pressure", "Take a checked sidestep from an unseen attack",
                                            attacker_id=reported.get("id"), unseen_attacker=True)
        if status.get("on_ground") is True and campaign_dimension(status) == "overworld":
            if defense is not None and defense.name == "retreat_from":
                return defense
            if target is not None and (point := self._safety_rules._pos(target.get("pos"))) is not None:
                retreat = self._retreat_decision(observation, Decision("retreat_from", {"pos": point, "distance": 8},
                    "Withdraw from the currently observed attacker", "safety"))
                if retreat.name == "retreat_from":
                    return retreat
        return wait

    def _retreat_state(self, observation, threat):
        """Persist recovery evidence, never an executable route or old pending action."""
        return self._retreat.state(observation, threat)

    def _retreat_changed(self, observation, threat, recovery):
        return self._retreat.changed(observation, threat, recovery)

    def _retreat_decision(self, observation, decision):
        return self._retreat.choose_decision(observation, decision)

    def _retreat_options(self, observation, decision):
        return self._retreat.options(observation, decision)

    def _retreat_result(self, decision, result, observation):
        """Return whether a known route failure uses recovery rather than global failure limits."""
        return self._retreat.handle_result(
            decision, result, observation, uncertain_codes=UNCERTAIN, fatal_codes=FATAL
        )

    @staticmethod
    def _life_marker(observation):
        status = observation["status"]
        return json.dumps({key: status.get(key) for key in ("last_death", "dimension", "alive")}, sort_keys=True)

    def _unsafe_destination(self, decision, observation):
        target_actions = destination_checked_skills(daily=self.daily)
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
            if (entity.get("type") == "hostile" and pos is not None
                    and math.dist(pos, target) <= self._safety_rules._threat_distance(entity.get("name"))):
                return "A hostile is close to the proposed destination"
        for block in observation.get("blocks", []) + observation.get("terrain", []):
            pos = self._safety_rules._pos(block.get("pos"))
            if (decision.name == "collect_liquid"
                    and block.get("name") == decision.args.get("expected_name")):
                continue  # The bucket skill verifies the source and safe stance.
            if pos is not None and block.get("name") in SurvivalSkills.HAZARDS and math.dist(pos, target) <= 1.5:
                return "An observed environmental hazard is close to the proposed destination"
        return None

    def resume(self):
        if self._closed or self.busy or self.finished:
            return False
        if self.continuous:
            self._operator_pause = None
            self._continuous_retry_at = 0.
            if self.context.pop("operator_pause", None) is not None:
                self._checkpoint(context=self.context)
        if self._pending_leaf_readback is not None:
            self._pending_leaf_readback = pending_foliage(self.journal) or {"invalid": True}
            pending = self.journal.state.get("pending") or {}
            if pending.get("foliage_manually_paused"):
                self._checkpoint(pending={key: value for key, value in pending.items() if key != "foliage_manually_paused"})
        pending_read = self.continuous and (self.journal.state.get("pending")
                                            or (self._task_snapshot() or {}).get("status") == "unknown")
        if self._pending_leaf_readback is None and not pending_read and not self._request_canopy_resume():
            self._paused = True
            self.pause_reason = self.pause_reason or "canopy_resume_requires_reconciliation"
            return False
        self._paused = False
        self.pause_reason = None
        self.failures = 0
        self._next_attempt = 0
        self._task_resume_requested = True
        gate = getattr(getattr(self.planner, "brain", None), "request_gate", None)
        if self._model_blocked and gate:
            try:
                if gate.snapshot().get("state") == "idle":
                    self._model_blocked = None
            except ModelGateError as error:
                self._record("WARN", "MODEL_GATE_UNAVAILABLE", {"code": error.code})
        # A previous intention is evidence, never an instruction to replay.
        self._record("INFO", "RESUMED", {"reobserve": True})
        return True

    def pause(self, reason="user_requested"):
        if self.continuous and reason != "status_unavailable_during_action":
            self._operator_pause = reason
            if reason != "shutdown":
                self.context["operator_pause"] = reason
                self._checkpoint(context=self.context)
        self._paused = True
        self.pause_reason = reason
        self._cancel.set()
        pending = self.journal.state.get("pending")
        if reason == "user_requested" and self._pending_leaf_readback is not None and pending:
            self._checkpoint(pending={**pending, "foliage_manually_paused": True})
        self._task_interrupt(reason)
        if self._model_worker:
            self._model_worker.cancel()
        try:
            if self.busy and not self._future.done() and self._pending_leaf_readback is None:
                self.client.stop()
        finally:
            self._record("WARN", "PAUSED", {"reason": reason})
            if not self.busy:
                self._finish_task_feedback(self.last_observation, paused=True)

    def request_step(self):
        if not self.resume():
            return False
        return self._start()

    def request_sleep(self):
        if not self.resume():
            return False
        return self._start(Decision("auto_sleep", {}, "Player requested sleep", "sleep"))

    def _remaining_budget(self):
        """Use the enforced objective budget for both scheduling and model context."""
        if self.continuous:
            return {"remaining_steps": None, "remaining_seconds": None}
        if self.competition is not None:
            step_limit = self.settings.get("competition", {}).get("max_steps", 100000)
            remaining_seconds = self.competition.snapshot().get("remaining_seconds", 0)
        else:
            step_limit = self.settings["max_steps"]
            remaining_seconds = self.settings["max_runtime_seconds"] - (time.monotonic() - self._started)
        return {"remaining_steps": max(0, step_limit - self.steps),
                "remaining_seconds": max(0, remaining_seconds)}

    def _death_limit_reached(self):
        return not self.continuous and self.context["death_count"] >= self.settings["max_deaths"]

    def _start(self, manual=None):
        if self._closed or self.busy or self.paused or self.finished:
            return False
        budget = self._remaining_budget()
        if budget["remaining_steps"] == 0 or budget["remaining_seconds"] == 0:
            self._task_end_at_boundary("global_budget_exhausted")
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
        refused = self.context.get("foliage_no_retry")
        if refused:
            status = observation["status"]
            pos = self._safety_rules._pos(status.get("pos"))
            if (pos is not None and (refused.get("superseded") or tuple(map(math.floor, pos)) == tuple(map(math.floor, refused["origin"])))
                    and status.get("dimension") == refused["dimension"] and status.get("last_death") == refused["life"]
                    and self.context.get("life_generation") == refused["generation"]):
                self.context["blocked_targets"].append(self._safety_rules._key(refused["pos"]))
            else:
                self.context.pop("foliage_no_retry", None)
        self._prepare_local_recovery(observation)
        if self.task_controller is not None:
            if self._task_snapshot().get("status") == "idle":
                self.context.pop("task_execution", None)
            else:
                self.context["task_execution"] = self._task_snapshot()
        if observation.get("observation_scope") == "safety":
            # Empty fields mean unobserved, not an empty backpack or lost goals.
            self.context.setdefault("milestones", {})
            if isinstance(self.daily_board, dict):
                self.daily_board = {**self.daily_board, "fresh": False, "observation_scope": "safety"}
            if isinstance(self.context.get("daily_tasks"), dict):
                self.context["daily_tasks"].update(fresh=False, observation_scope="safety")
            if isinstance(self.context.get("playbook"), dict):
                self.context["playbook"].update(fresh=False, observation_scope="safety")
            self._update_mainline(observation)
            if self.daily_policy:
                self.context["survival_constraints"] = self.daily_policy.constraints(observation, context=self.context)
            return
        self._observe_shelter_project(observation)
        self.context["milestones"] = self.planner.progress(observation)
        if self.playbook:
            self.context["playbook"] = self.playbook.build(observation,
                {**self.context, "pending": self.journal.state.get("pending")})
            self.context["playbook"].update(fresh=True, observation_scope="full")
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
            self.context["survival_constraints"] = self.daily_policy.constraints(observation, context=self.context)
            signature = json.dumps(self.context["daily_tasks"], sort_keys=True)
            if signature != self._daily_board_signature:
                self._record("INFO", "DAILY_TASKS", {"board": self.daily_board,
                    "memory_counts": {key: len(value) for key, value in recalled.items() if isinstance(value, list)}})
                self._daily_board_signature = signature
        self._update_mainline(observation)
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

    def _competition_tool(self, decision, observation):
        """Handle metadata-only task tools at an idle, fresh execution boundary."""
        if not self.competition:
            return None
        names = {"select_task", "update_progress", "finish_task", "force_exit_task", "end_competition"}
        if decision.name not in names:
            if self.competition.snapshot().get("active_goal_id") is None and decision.name not in {
                "wait", "retreat_from", "canopy_escape", "eat_food", "auto_sleep", "fight_hostile"}:
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
            self.collector.set_context({**self.context, "shelter_project": project_anchor(
                self.context.get("shelter_project"), scope=self.journal.scope)})
        # Optional collector capability preserves custom collector injection.
        if callable(getattr(type(self.collector), "collect_safety", None)):
            recovery = self.context.get("retreat_recovery") or {}
            short = self.collector.collect_safety(self._cancel,
                force=safety_only or recovery.get("evacuating") is True,
                threat_radius=self._safety_rules.threat_radius)
            if short is not None and (not self.campaign or campaign_dimension(short["status"]) == "overworld"):
                status = short["status"]
                hostiles = self._safety_rules._dangerous_hostiles(short, status.get("pos"))
                nearby = status.get("nearest_hostile") or {}
                close = (bool(hostiles)
                    or isinstance(nearby, dict) and self._safety_rules._number(nearby.get("dist"))
                    and nearby["dist"] <= self._safety_rules._threat_distance(nearby.get("name")))
                needs_inventory = (self._safety_rules._number(status.get("health"))
                    and self._safety_rules._number(status.get("food"))
                    and status.get("on_ground") is True and not status.get("is_sleeping")
                    and not status.get("in_vehicle")
                    and not any(status.get(key) for key in ("in_water", "in_lava", "on_fire", "submerged"))
                    and status.get("dimension") in {"overworld", "minecraft:overworld"}
                    and status.get("gamemode") == "survival" and status["food"] < 20
                    and (status["health"] <= self._safety_rules.low_health
                         or status["food"] <= self._safety_rules.food_threshold))
                # Without a nearby enemy, existing food/low-health priorities
                # still need a real inventory rather than a partial empty list.
                quiet_evacuation = recovery.get("evacuating") is True and not close
                if (safety_only or close or not needs_inventory and not quiet_evacuation
                        or not status.get("ready") or not status.get("alive") or status.get("busy")):
                    if (self.campaign is not None and close and status.get("on_ground") is True
                            and (status.get("offhand") or {}).get("name") == "shield"
                            and status.get("health", 0) >= 14):
                        try:
                            items = self.client.inventory(timeout_seconds=.8)["result"].get("items")
                            after = self.client.get_status(timeout_seconds=.8)["result"]
                            if (isinstance(items, list) and self._life_marker({"status": after}) == self._life_marker(short)
                                    and after.get("ready") and after.get("alive") and not after.get("busy")
                                    and math.dist(after["pos"], status["pos"]) <= .3):
                                # The combat skill still rereads the target, equipment and terrain before a write.
                                short = {**short, "status": after, "inventory": items, "inventory_fresh": True}
                        except APIError:
                            pass
                    return short
        return self.collector.collect(self._cancel)

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
        project = self._shelter_project_lock(observation["status"].get("dimension"))
        if project:
            model_context = copy.deepcopy(model_context)
            # task_execution is already included in the model's bounded context;
            # retain the anchor as intent, never as fresh construction evidence.
            model_context["task_execution"] = {**(model_context.get("task_execution") or self._task_snapshot())}
            model_context["task_execution"]["shelter_recovery_constraint"] = {
                **{key: project[key] for key in ("pos", "orientation", "dimension")},
                "requires_revalidation": True,
                "reason": "Continue this already started shelter. These are its blueprint centre and orientation; "
                          "a wall block or standing cell is not a different building centre. "
                          "Reobserve the original site before construction; old geometry never authorizes a write."}
        stats = self.context.get("autonomy_stats", {})
        stats["model_planning_rounds"] = stats.get("model_planning_rounds", 0) + 1
        worker.start(observation, model_context)
        while not worker.done:
            if self._cancel.wait(.05):
                worker.cancel()
                code = "llm_cancelled" if self.paused else "llm_preempted"
                raise LLMError(code, "Local safety or pause superseded model planning; response will be drained")
        return worker.take(observation)

    def _shelter_project_lock(self, dimension):
        task = self._task_snapshot() or {}
        if (not self.daily or self.competition_mode
                or not (self._task_child or self._automatic_cycle)
                or task.get("task") != "shelter"
                or task.get("status") not in {"running", "suspended", "needs_ai"}):
            return None
        return active_project(self.context.get("shelter_project"), scope=self.journal.scope, dimension=dimension)

    def _observe_shelter_project(self, observation):
        project = active_project(self.context.get("shelter_project"), scope=self.journal.scope)
        if (project and observation.get("fresh") is True and observation.get("observation_scope") != "safety"
                and observation.get("status", {}).get("dimension") == project["dimension"]
                and completion_observed(project, observation.get("shelter"))):
            project["complete"] = True
            self.context["shelter_project"] = project
            self.context["base_project"] = {key: project[key] for key in ("pos", "material", "dimension", "orientation")}
            self._record("INFO", "SHELTER_PROJECT_COMPLETED", {"project": project,
                "shelter": observation["shelter"],
                "note": "Fresh closed-door complete geometry releases construction selection lock"})
            self._checkpoint(context=self.context)

    def _daily_result(self, decision, result, observation):
        if not self.daily:
            return
        if observation and observation.get("observation_scope") == "safety":
            if decision.name == "fight_hostile" and self.memory:
                self.memory.record_result(decision, result, observation)
            return  # Do not persist partial counts as authoritative inventory.
        if self.memory:
            self.memory.record_result(decision, result, observation)
        status = (observation or {}).get("status") or {}
        data = result.data
        project = project_anchor(self.context.get("shelter_project"), scope=self.journal.scope)
        built = verified_build_anchor(decision, result, self.journal.scope)
        if built and (project is None or same_site(project, built) or not self._shelter_project_lock(status.get("dimension"))):
            self.context["shelter_project"] = project = built
            self.context["base_project"] = {key: built[key] for key in ("pos", "material", "dimension", "orientation")}
            self._checkpoint(context=self.context)
        elif (project and decision.name == "inspect_shelter" and result.success is True
                and not _uncertain_result(result) and data.get("fresh") is True and same_site(project, data)
                and type(data.get("missing_blocks")) is int and 0 <= data["missing_blocks"] <= 55):
            project.update(missing_blocks_hint=data["missing_blocks"],
                           door_needed_hint=(data.get("door") or {}).get("frame_verified") is not True)
            self.context["shelter_project"] = project
        shelter_orientation = decision.args.get("orientation", "north")
        if (decision.name == "plan_shelter" and result.success and not _uncertain_result(result)
                and (not self._shelter_project_lock(status.get("dimension"))
                     or same_site(project, {**decision.args, "dimension": status.get("dimension")}))
                and (data.get("geometry") or {}).get("floor_verified") is True
                and isinstance(shelter_orientation, str) and shelter_orientation in {"north", "south", "east", "west"}
                and data.get("orientation", "north") == shelter_orientation):
            if project and not same_site(project, {**decision.args, "dimension": status.get("dimension")}):
                self.context.pop("shelter_project", None)
            self.context["base_project"] = {"pos": decision.args["pos"], "material": decision.args["material"],
                                            "dimension": status.get("dimension"), "source": "skill_verified",
                                            "orientation": shelter_orientation}
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
            self.context.pop("combat_retreat", None)
            self.context.pop("combat_blocked_until", None)
            for key in ("canopy_recovery", "food_recovery", "local_footing", "cautious_bootstrap", "cautious_bootstrap_basis",
                        "last_scout_report"):
                self.context.pop(key, None)
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
        if (not self._saw_observation and status.get("alive") is False
                and self.context.get("was_alive") is not False):
            new_death = True
        if new_death:
            self.context.update(failures={}, blocked_until={}, blocked_targets=[], explore_index=0,
                                spawn=None, death_count=self.context["death_count"] + 1)
            self.context.pop("retreat_recovery", None)
            self.context.pop("combat_retreat", None)
            self.context.pop("combat_blocked_until", None)
            self.context["life_generation"] += 1
            for key in ("canopy_recovery", "food_recovery", "local_footing", "cautious_bootstrap", "cautious_bootstrap_basis",
                        "last_scout_report"):
                self.context.pop(key, None)
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
        self._task_child = False
        self._task_safety = False
        # Retain the decision source through result persistence: model feedback
        # is consumed before _daily_result, and an exhausted task has no child.
        # Explicit manual decisions and completed/replaced goals may change site.
        self._automatic_cycle = manual is None
        try:
            self._phase("observing")
            if self._pending_leaf_readback is not None:
                if self.continuous and self._continuous_retries:
                    decision = Decision("observe", {}, "Recheck the connection before observing the old foliage dig",
                                        "foliage_readback")
                    self.client.get_status(timeout_seconds=2)
                    refreshed = pending_foliage(self.journal)
                    if refreshed is not None:
                        self._pending_leaf_readback = refreshed
                return self._reobserve_pending_foliage()
            if self.continuous and (self.journal.state.get("pending") or (self._task_snapshot() or {}).get("status") == "unknown"):
                decision = Decision("observe", {}, "Reobserve an interrupted game action before replanning", "continuous_readback")
                result, observation = self._reobserve_interrupted()
                return decision, result, observation
            if self.continuous and self._model_blocked and time.monotonic() >= self._continuous_gate_check:
                self._continuous_gate_check = time.monotonic() + 5
                gate = getattr(getattr(self.planner, "brain", None), "request_gate", None)
                try:
                    if gate and gate.snapshot().get("state") == "idle":
                        self._model_blocked = None
                except ModelGateError:
                    pass  # Game observation and local survival continue.
            observation = self._observe()
            self.latest_status = observation["status"]
            hit = self._combat_reaction.observe(self.client, observation["status"], self._safety_baseline)
            self._prepare_context(observation)
            self.context.update(self._remaining_budget())
            if self._cancel.is_set():
                return decision, SkillResult(False, "Cancelled before action", error_code="cancelled"), observation
            if self._death_limit_reached():
                return decision, SkillResult(False, "Death limit reached", error_code="death_limit"), observation
            reconciled = self._task_reconcile(observation)
            if reconciled is not None:
                return decision, reconciled, observation
            if self.task_controller and self._task_snapshot().get("status") == "unknown":
                self._paused = True
                self.pause_reason = "task_outcome_requires_resume"
                return decision, SkillResult(False,
                    "Unknown task needs explicit resume with a fresh complete idle observation",
                    {"executed": False, "action_state": "unknown", "cleanup_skipped": "task_unknown"},
                    "action_unconfirmed"), observation
            if manual is not None and (not observation["status"].get("ready")
                                       or not observation["status"].get("alive") or observation["status"].get("busy")):
                decision = Decision("wait", {"seconds": 1}, "Manual action needs an idle, living, ready bot", "observe")
                return decision, SkillResult(False, decision.reason, error_code="busy"), observation
            urgent = self._combat_priority(observation, hit)
            if urgent is None:
                release = self._release_defense(observation)
                if release is not None:
                    decision, result = release
                    return decision, result, {**observation, "fresh": False} if _uncertain_result(result) else observation
            if urgent is not None:
                if self._model_worker and self._model_worker.pending:
                    self._model_worker.cancel()
                decision = urgent
                self._last_reaction_hit = hit["t"]
                manual = None  # No queued manual production is admitted during a confirmed attack.
                self._record("WARN", "COMBAT_PREEMPT", {"hit": hit, "decision": asdict(decision), "source": "rules"})
            elif observation.get("observation_scope") == "safety":
                if self._model_worker and self._model_worker.pending:
                    self._model_worker.cancel()
                water = self.campaign.water_recovery(observation["status"]) if self.campaign is not None else None
                decision = water or self._mandatory_safety(observation)
                if water is None and self.campaign is not None:
                    decision = self.campaign.melee_recovery(decision, observation, self.context.get("retreat_recovery"))
                    decision = self.campaign.food_recovery(decision, observation)
                if (water is None and (decision is None or decision.name not in {"retreat_from", "wait", "fight_hostile"})
                        and not (decision and decision.name == "campaign_action"
                                 and decision.args.get("operation") in {"emergency_food", "defend_melee", "escape_pressure"})):
                    decision = Decision("wait", {"seconds": 1}, "Short safety observation cannot authorize production", "observe")
                self._record("WARN", "SAFETY_OVERRIDE", {"decision": asdict(decision), "status": observation["status"],
                    "observation_scope": "safety"})
            elif manual is not None:
                decision = manual
            elif self.campaign is not None:
                overworld = campaign_dimension(observation["status"]) == "overworld"
                decision = self.campaign.water_recovery(observation["status"])
                if overworld and decision is None:
                    decision = self.campaign.melee_recovery(self._mandatory_safety(observation), observation,
                                                            self.context.get("retreat_recovery"))
                    if decision is None or (decision.name == "wait" and decision.goal == "food_recovery"):
                        decision = self.campaign.death_recovery(observation, self.journal) or decision
                    decision = self.campaign.food_recovery(decision, observation)
                    decision = decision or self.campaign.mine_recovery(observation, self.journal)
                    decision = decision or self.campaign.night_cover(observation)
                    if decision is None and self._task_active():
                        goal = self.campaign.projection_action(observation)
                        task = self._task_snapshot()
                        if goal is not None and not same_goal(task.get("args"), goal.args):
                            self.task_controller.stop("The selected projection supplies the next local goal", cancelled=True)
                            self._save_task("TASK_CANCELLED")
                        elif self.campaign.obsolete_opening_task(task, observation):
                            self.task_controller.stop("Current equipment already meets this opening prerequisite", cancelled=True)
                            self._save_task("TASK_CANCELLED")
                        else:
                            decision = self._task_next(observation)
                if decision is None:
                    decision = self.campaign.recover_task(self.campaign.choose(observation), self._task_snapshot(), observation)
                if decision.name == "campaign_action" and self.continuous and self._matches_abandoned_action(decision, observation):
                    if decision.args["operation"] in {"defend_melee", "escape_pressure", "surface_water"}:
                        decision = self._mandatory_safety(observation) or Decision("wait", {"seconds": .25},
                            "Reobserve without replaying the unconfirmed emergency action", "safety")
                    else:
                        decision = self.campaign.action(self.campaign.state["view"]["stage"], "recover_stance",
                            "Recheck a different safe stance before planning this uncertain stage again")
            elif self.uses_llm:
                safety = self._mandatory_safety(observation) or self._local_task_food(observation)
                if safety is not None:
                    if self._model_worker and self._model_worker.pending:
                        self._model_worker.cancel()
                    decision = safety
                    self._record("WARN", "SAFETY_OVERRIDE", {"decision": asdict(safety), "status": observation["status"]})
                elif self._task_active():
                    decision = self._task_next(observation)
                elif (opening := (self._mainline_action(observation) if self.mainline_enabled
                                  else self._local_opening(observation))) is not None:
                    decision = opening
                else:
                    if self._model_blocked:
                        decision = Decision("wait", {"seconds": 5}, "Model request outcome is uncertain; local safety remains active", "observe")
                        return decision, SkillResult(True, decision.reason, {"waiting": True, "model_blocked": self._model_blocked}), observation
                    self._phase("thinking")
                    before_life = self._life_marker(observation)
                    before_chapter = (self.context.get("mainline") or {}).get("stage")
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
                    if self._death_limit_reached():
                        return decision, SkillResult(False, "Death limit reached while the model was planning",
                                                     {"executed": False}, "death_limit"), observation
                    if self._life_marker(observation) != before_life:
                        return decision, SkillResult(False, "Life or dimension changed during model planning; replan",
                                                     {"executed": False}, "safety_changed"), observation
                    hit = self._combat_reaction.observe(self.client, observation["status"], self._safety_baseline)
                    if self._combat_priority(observation, hit) is not None or decision.name in {"fight_hostile", "retreat_from"}:
                        return decision, SkillResult(False, "Combat actions are selected by the local response",
                                                     {"executed": False}, "safety_changed"), observation
                    if self.mainline_enabled and (self.context.get("mainline") or {}).get("stage") != before_chapter:
                        return decision, SkillResult(False, "Mainline chapter changed while the model was planning",
                                                     {"executed": False}, "safety_changed"), observation
                    unsafe_target = self._unsafe_destination(decision, observation)
                    if self.daily_policy:
                        unsafe_target = unsafe_target or self.daily_policy.validate(decision, observation, context=self.context)
                    if unsafe_target:
                        self._record("WARN", "SAFETY_OVERRIDE", {"model_decision": asdict(decision), "reason": unsafe_target})
                        return decision, SkillResult(False, unsafe_target, {"executed": False}, "safety_changed"), observation
                    safety = self._mandatory_safety(observation)
                    if safety is not None and (safety.name != decision.name or safety.args != decision.args):
                        self._record("WARN", "SAFETY_OVERRIDE", {"model_decision": asdict(decision), "decision": asdict(safety)})
                        return decision, SkillResult(False, "Fresh local safety check rejected the model action; replan",
                                                     {"executed": False, "safety_decision": asdict(safety)}, "safety_changed"), observation
            else:
                if self.daily:
                    safety = self._mandatory_safety(observation) or self._local_task_food(observation)
                    decision = safety or self._task_next(observation) or (self._mainline_action(observation)
                        if self.mainline_enabled else self.daily_goals.choose(observation, self.context.get("memory"), self.context))
                    if decision is None:
                        decision = Decision("wait", {"seconds": 5}, "No verified daily action is currently available", "observe")
                else:
                    decision = self.planner.choose(observation, self.context)
                    decision = self._retreat_decision(observation, decision)
            if (self._task_active() and not self._task_child and decision.name != "run_survival_task"
                    and (urgent is not None or decision.goal in {"safety", "recover", "observe", "food", "food_recovery", "sleep"})):
                self._task_interrupt(decision.reason)
            if self.continuous and self._matches_abandoned_action(decision, observation):
                return decision, SkillResult(False, "Previous action remains unconfirmed at this stance; choose a new action",
                                             {"executed": False}, "previous_action_unconfirmed"), observation
            if decision.name in STARTER_MINING_SKILLS and not self._task_child:
                return decision, SkillResult(False, "Starter mining requires an active procedural goal",
                                             {"executed": False}, "unsupported_skill"), observation
            if decision.name == "campaign_action" and (not self.campaign or manual is not None or self._model_feedback):
                return decision, SkillResult(False, "Campaign actions are internal to the enabled local route",
                                             {"executed": False}, "unsupported_skill"), observation
            if self._task_child:
                rejected = self._unsafe_destination(decision, observation) or self.daily_policy.validate(decision, observation, context=self.context)
                if rejected:
                    return decision, SkillResult(False, rejected, {"executed": False}, "safety_changed"), observation
            project = self._shelter_project_lock(observation["status"].get("dimension"))
            if (project and decision.name in {"plan_shelter", "plan_shelter_preparation", "prepare_shelter_step",
                                             "build_shelter_step", "build_shelter_batch"}
                    and not same_site(project, {**decision.args, "dimension": observation["status"].get("dimension")})):
                return decision, SkillResult(False,
                    "Continue the already started shelter at its original blueprint centre and orientation; "
                    "a wall or standing cell cannot replace the site. Reobserve that site before building",
                    {"executed": False, "project": project, "requires_revalidation": True}, "shelter_project_active"), observation
            self._active_decision = decision
            self._safety_baseline = copy.deepcopy(observation["status"])
            self._record("INFO", "DECISION", {"decision": asdict(decision), "status": observation["status"],
                                               "counts": observation["counts"], "milestones": self.context["milestones"],
                                               "source": "task" if self._task_child else "model" if self._model_feedback else "manual" if manual else "mainline" if decision.goal == "mainline" else "safety" if self.uses_llm else "rules"})
            task_result = self._competition_tool(decision, observation)
            if task_result is not None:
                return decision, task_result, observation
            if decision.name == "run_survival_task":
                return decision, self._task_start(decision, observation), observation
            if decision.name == "wait":
                self._phase("waiting")
                return decision, SkillResult(True, decision.reason, {"waiting": True}), observation
            if decision.name == "goal_complete":
                if self.daily:
                    return decision, SkillResult(False, "Daily survival continues with maintenance; there is no final goal_complete", error_code="goal_unverified"), observation
                if self.uses_llm and not self._safety_rules.progress(observation)["starter_goal_complete"]:
                    return decision, SkillResult(False, "Latest inventory does not verify goal completion", error_code="goal_unverified"), observation
                return decision, SkillResult(True, decision.reason, {"goal_complete": True}), observation
            refused = self.context.get("foliage_no_retry")
            if refused and decision.name == "clear_foliage" and decision.args.get("pos") == refused["pos"]:
                return decision, SkillResult(False, "Previously unconfirmed leaf still present; choose a different route",
                    {"executed": False}, "foliage_still_present"), observation
            self._checkpoint(context=self.context, pending={"decision": asdict(decision), "before_counts": observation["counts"],
                "before_counts_fresh": observation.get("inventory_fresh", True),
                "task_id": self._task_snapshot().get("task_id") if self._task_child else None})
            self._phase("acting")
            if decision.name == "auto_sleep":
                options = {key: value for key, value in self.sleep_settings.items() if key in SleepController.SKILL_OPTIONS}
                result = AutoSleepSkill(self.client).execute(**options, cancel_event=self._cancel)
            elif decision.name == "campaign_action":
                result = self.campaign_skills.execute(decision.args, self.campaign.state, cancel_event=self._cancel)
                self.campaign.record(decision, result)
                self._record("INFO" if result.success else "WARN", "CAMPAIGN_STEP",
                             {"campaign": self.campaign.state["view"], "result": asdict(result)})
            else:
                options = {"recovery_context": self._retreat_options(observation, decision)} if decision.name == "retreat_from" else {}
                if decision.name == "retreat_from" and self.campaign is not None:
                    options["recovery_context"]["campaign_ground_escape"] = True
                if decision.name == "canopy_escape":
                    options["recovery_context"] = self._canopy_options(observation)
                if refused and decision.name in ("canopy_escape", "retreat_from"):
                    x, _, z = refused["pos"]
                    y = math.floor(refused["origin"][1])
                    excluded = options["recovery_context"].get("excluded_targets", [])
                    options["recovery_context"]["excluded_targets"] = (excluded + [[x, y-d, z] for d in (0, 1, 2)])[-8:]
                if self._task_child and self.context.get("cautious_bootstrap") is True:
                    options["cautious"] = True
                if self._task_child and decision.goal in {"deep_mine", "scout_surface", "equip_loadout"}:
                    result = self._run_task_workflow(decision, observation)
                else:
                    runner = resolve_skill_runner(self, decision.name)
                    result = runner.execute(decision.name, decision.args, cancel_event=self._cancel, **options)
            if not isinstance(result, SkillResult):
                raise TypeError("Skill returned an invalid result")
            if (decision.name in ("canopy_escape", "clear_foliage") and _uncertain_result(result)
                    and not self._cancel.is_set()):
                intent = foliage_intent(asdict(decision), asdict(result), observation["status"],
                    self.context.get("life_generation"), observation.get("footing"))
                if intent is not None:
                    pending = self.journal.state.get("pending") or {}
                    if pending.get("decision") == asdict(decision):
                        intent["scope"] = self.journal.scope
                        self._checkpoint(pending={**pending, "foliage_readback": intent, "foliage_result": asdict(result)})
                        result.data["cleanup_skipped"] = "foliage_readback"
                        proof, reason = confirm_foliage(self.client, intent, self.context["life_generation"],
                                                       self._cancel, self._safety_rules.threat_radius)
                        if proof is not None:
                            result = resolved_result(result, proof)
                            self._record_foliage_readback(intent, proof)
                        else:
                            self._pending_leaf_readback = intent
                            result.data["readback_reason"] = reason
                            self._record("WARN", "FOLIAGE_RECHECK_PAUSED", {"pos": intent["pos"], "reason": reason})
            verified_survey = (self.campaign is not None and self._task_child
                and decision.name in {"plan_shelter", "plan_shelter_preparation"}
                and result.data.get("executed") is False and result.data.get("fresh") is True)
            if ((self._model_feedback or self.daily) and not verified_survey
                    and not self._cancel.is_set() and not _uncertain_result(result)):
                try:
                    observation = self._observe(safety_only=decision.name in {"retreat_from", "fight_hostile"})
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
            if (decision.name == "observe" and not self._cancel.is_set()
                    and (exc.code == "not_connected" or self.continuous and exc.code in {"unauthorized", "invalid_token"})):
                try:
                    if self.continuous and exc.code != "not_connected":
                        self.client.connect_continuous()
                    else:
                        self.client.connect()
                    return decision, SkillResult(False, "Reconnect requested; reobserve before acting", error_code="reconnecting"), observation
                except APIError as reconnect_error:
                    exc = reconnect_error
            details = {}
            if self.continuous:
                details["retry_after_s"] = connection_retry_delay(exc, 0)
            return decision, SkillResult(False, str(exc), details, exc.code), observation
        except Exception as exc:
            return decision, SkillResult(False, "Unexpected harness error; inspect the checkpoint",
                                         {"exception_type": type(exc).__name__}, error_code="internal"), observation

    def _record_foliage_readback(self, intent, proof):
        if not proof["removed"] and proof.get("resolution") != "superseded_life":
            self.context["foliage_no_retry"] = {key: copy.deepcopy(intent[key])
                for key in ("origin", "pos", "dimension", "life", "generation")}
            if proof.get("resolution") == "superseded_stance":
                self.context["foliage_no_retry"]["superseded"] = True
        self._record("INFO", "FOLIAGE_INTENT_SUPERSEDED" if proof.get("resolution") in ("superseded_stance","superseded_life")
                     else "FOLIAGE_RECONCILED", proof)

    def _reobserve_pending_foliage(self):
        intent = self._pending_leaf_readback
        decision = Decision("observe", {}, "Check the pending foliage dig without sending another action", "foliage_readback")
        pending = copy.deepcopy(self.journal.state.get("pending"))
        observation = None
        try:
            if intent.get("invalid") or not pending or pending.get("decision") != intent["decision"]:
                raise ValueError("pending_identity_changed")
            if self.task_controller and pending.get("task_id") not in (None, self._task_snapshot().get("task_id")):
                raise ValueError("pending_task_changed")
            proof, reason = confirm_foliage(self.client, intent, self.context.get("life_generation"),
                                            self._cancel, self._safety_rules.threat_radius)
            if proof is None:
                raise ValueError(reason)
            # Only a complete same-life snapshot may release an interrupted goal.
            # Nothing in the original move, dig or route is replayed.
            from core.daily_observation import DailyObservationCollector
            if isinstance(self.collector, DailyObservationCollector):
                observation = self.collector.collect(self._cancel, inspect_windows=False)
            else:
                observation = self.collector.collect(self._cancel)
            status = observation.get("status") or {}
            pos = self._safety_rules._pos(status.get("pos"))
            if (observation.get("fresh") is not True or observation.get("stale") is True
                    or observation.get("observation_scope") == "safety" or observation.get("inventory_fresh") is False
                    or not isinstance(observation.get("inventory"), list) or not isinstance(observation.get("counts"), dict)
                    or status.get("ready") is not True or status.get("alive") is not True or status.get("busy")
                    or status.get("dimension") != intent["dimension"]
                    or status.get("last_death") != proof.get("current_death_marker", intent["life"])
                    or status.get("gamemode") != "survival" or status.get("on_ground") is not True
                    or ("health" in proof and status.get("health") != proof["health"])
                    or pos is None or math.dist(pos, proof.get("new_stance", intent["origin"])) > .2 or self._cancel.is_set()
                    or self.journal.state.get("pending") != pending):
                raise ValueError("fresh_snapshot_changed")
            self.latest_status = status
            self._prepare_context(observation)
            expected_generation = (intent.get("completed_life") or {}).get("new_generation", intent["generation"])
            if self.context.get("life_generation") != expected_generation:
                raise ValueError("life_generation_changed")
            if self.task_controller and self._task_snapshot().get("status") == "unknown":
                if not self.task_controller.abandon_after_reobserve(observation):
                    raise ValueError("interrupted_task_not_reconciled")
                self._save_task("TASK_NEEDS_AI")
            if intent["decision"]["name"] == "canopy_escape":
                canopy = self.context.get("canopy_recovery") or {}
                canopy.pop("unknown", None)
                canopy["reason"] = "foliage_reobserved_replan"
                if (proof.get("resolution") in ("superseded_stance", "superseded_life") and all(row["name"] not in
                        SurvivalSkills.RETREAT_LEAVES | SurvivalSkills.RETREAT_TRUNKS for row in proof["supports"])):
                    self.context.pop("canopy_recovery", None)
            self._task_resume_requested = False
            self._pending_leaf_readback = None
            self._record_foliage_readback(intent, proof)
            return decision, SkillResult(True, "Foliage state reobserved; plan again from current terrain",
                                        {"executed": False, "reobserved": True, "readback": proof}), observation
        except (APIError, ValueError, TypeError, KeyError, AttributeError) as error:
            reason = getattr(error, "code", None) or str(error)[:120]
            self._record("WARN", "FOLIAGE_RECHECK_PAUSED", {"pos": intent.get("pos"), "reason": reason})
            return decision, SkillResult(False, "Foliage outcome still needs confirmation",
                {"executed": False, "action_state": "unknown", "readback_reason": reason}, "action_unconfirmed"), observation

    def _release_defense(self, observation):
        """Journal shield release separately; never attach it to a model proposal."""
        if (not self.daily or getattr(self.combat_skills, "defense_active", False) is not True
                or self.journal.state.get("pending") or self._cancel.is_set()
                or observation.get("fresh") is not True or observation.get("observation_scope") == "safety"):
            return None
        status = observation["status"]
        if status.get("ready") is not True or status.get("alive") is not True or status.get("busy"):
            return None
        threats = self._safety_rules._hostiles(observation, status["pos"])
        nearest = status.get("nearest_hostile") or {}
        if (any(distance <= 12 for distance, _ in threats)
                or isinstance(nearest, dict) and self._safety_rules._number(nearest.get("dist")) and nearest["dist"] <= 12):
            return None
        safety = self._mandatory_safety(observation)
        if safety is not None and safety.name in {"fight_hostile", "retreat_from"}:
            return None
        decision = Decision("release_defense", {}, "Release this skill's raised shield before ordinary work", "safety")
        if self.continuous and self._matches_abandoned_action(decision, observation):
            return None
        self._active_decision = decision
        self._safety_baseline = copy.deepcopy(status)
        self._checkpoint(context=self.context, pending={"decision": asdict(decision), "before_counts": observation["counts"],
            "before_counts_fresh": observation.get("inventory_fresh", True)})
        self._record("INFO", "DECISION", {"decision": asdict(decision), "status": status,
            "counts": observation["counts"], "source": "safety"})
        self._phase("acting")
        try:
            result = self.combat_skills.release_defense(cancel_event=self._cancel)
            if not isinstance(result, SkillResult):
                raise TypeError("Defense release returned an invalid result")
        except Exception as error:
            # The journaled release may already have reached the API. Return
            # its own decision so an outer observe error cannot clear pending.
            result = SkillResult(False, "Defense release outcome was not confirmed", {
                "executed": True, "action_state": "unknown", "cleanup_skipped": "combat_unknown",
                "exception_type": type(error).__name__}, "action_unconfirmed")
        return decision, result

    def _monitor(self):
        if self._cancel.is_set():
            return
        now = time.monotonic()
        decision = self._active_decision
        planning = self.phase == "thinking" and self._model_worker is not None and self._model_worker.pending
        defending = (decision is not None and (decision.name in {"retreat_from", "canopy_escape", "fight_hostile"}
                    or decision.name == "campaign_action" and decision.args.get("operation") in
                    {"surface_water", "defend_melee", "escape_pressure"}))
        if ((decision is None and not planning)
                or defending and decision.name != "fight_hostile"):
            return
        if not defending:
            last_status = self.latest_status or self._safety_baseline or {}
            hit = self._combat_reaction.observe(self.client, last_status)
            if hit is not None:
                self._interrupt_for_safety(last_status, planning, hit)
                return
        if now < self._next_monitor:
            return
        self._next_monitor = now + max(1, self.settings["poll_interval_seconds"])
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
        attack = self._combat_reaction.observe(self.client, status, baseline)
        if decision is not None and decision.name in {"eat_food", "auto_sleep"} and not attack:
            return
        constraints = self.context.get("survival_constraints") or {}
        food_turn = False
        if self.daily and constraints.get("scenario") == "low_health":
            from core.daily_policy import FOOD_ACTIONS
            food_turn = ((planning and constraints.get("model_allowed") is True)
                or decision is not None and decision.name in FOOD_ACTIONS) and (
                status.get("dimension") == baseline.get("dimension")
                and status.get("last_death") == baseline.get("last_death")
                and status.get("health", 0) >= baseline.get("health", 20)
                and status.get("food", 0) >= baseline.get("food", 20)
                and status.get("pos") == baseline.get("pos")
                and status.get("on_ground") is True and status.get("is_day") is True
                and not status.get("thundering") and status.get("time_of_day", 24000) < 11000)
        local_recovery_turn = (constraints.get("scenario") in {"food_recovery", "cautious_bootstrap"}
            and (planning and constraints.get("model_allowed") is True or decision is not None
                 and (decision.goal == "food_recovery" or self.context.get("cautious_bootstrap") is True))
            and status.get("dimension") == baseline.get("dimension")
            and status.get("last_death") == baseline.get("last_death")
            and status.get("health", 0) >= baseline.get("health", 20)
            and status.get("food", 0) >= 12 and status.get("on_ground") is True)
        combat_action = decision is not None and decision.name == "fight_hostile"
        campaign_encounter = (decision is not None and decision.name == "campaign_action"
                              and (decision.args.get("operation") == "loot_mob" and hostile.get("name") == decision.args.get("mob")
                                   or decision.args.get("operation") == "dragon" and hostile.get("name") == "ender_dragon"))
        campaign_food = (decision is not None and decision.name == "campaign_action"
                         and decision.args.get("operation") in {"emergency_food", "recover_gear"}
                         and status.get("health", 0) >= baseline.get("health", 20))
        combat_changed = combat_action and (
            status.get("dimension") != baseline.get("dimension")
            or status.get("last_death") != baseline.get("last_death")
            or status.get("health", 0) < baseline.get("health", 20))
        surface_expedition = self._task_child and decision is not None and decision.goal in {"deep_mine", "scout_surface"}
        hit_preempts = attack is not None and not defending
        danger = (hit_preempts or not status.get("alive") or status.get("in_lava") or status.get("on_fire")
                  or status.get("submerged") or (status.get("in_water") and not surface_expedition)
                  or status.get("health", 20) <= self.settings["low_health_threshold"] and not (food_turn or local_recovery_turn or campaign_food)
                  or combat_changed
                  or (not (combat_action or campaign_encounter) and isinstance(hostile.get("dist"), (int, float))
                      and hostile["dist"] <= self._safety_rules._threat_distance(hostile.get("name"))))
        if danger:
            self._interrupt_for_safety(status, planning, attack if hit_preempts else None)

    def _interrupt_for_safety(self, status, planning, hit):
        self._cancel.set()
        self._record("WARN", "SAFETY_INTERRUPT", {"status": status, "model_pending": planning, "combat_hit": hit})
        if planning:
            self._model_worker.cancel()
        else:
            self.client.stop()

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
        if not self.paused:
            self._combat_reaction.observe(self.client, status, self.latest_status)
        self.latest_status = status
        before = (self.context.get("death_count"), self.context.get("last_dimension"))
        self._update_lifecycle(status)
        hostile = status.get("nearest_hostile") or {}
        danger = (status.get("alive") is False or any(status.get(key) for key in ("in_lava", "on_fire", "submerged"))
                  or (self._safety_rules._number(status.get("health")) and status["health"] <= self.settings["low_health_threshold"])
                  or (isinstance(hostile, dict) and self._safety_rules._number(hostile.get("dist"))
                      and hostile["dist"] <= self._safety_rules._threat_distance(hostile.get("name"))))
        changed = before != (self.context.get("death_count"), self.context.get("last_dimension"))
        if not self.paused and (danger or changed):
            self._next_attempt = min(self._next_attempt, now)
        if not self.paused and self._death_limit_reached():
            self._paused = True
            self.pause_reason = "death_limit"
            self._phase("paused")
            self._task_end_at_boundary("death_limit")
        # Preserve pending and pause reason, including manual/unknown-result pauses.
        self._checkpoint(context=self.context)
        return status

    def _recheck_leaf_after_respawn(self, status):
        """Authorize one observation per evidenced respawn, after an inconclusive foliage read."""
        intent = self._pending_leaf_readback or {}
        generation = self.context.get("life_generation")
        if (self.pause_reason != "action_unconfirmed" or not status or type(intent.get("generation")) is not int
                or type(generation) is not int or generation <= intent["generation"]
                or self._foliage_respawn_checked == generation or self._death_limit_reached()
                or status.get("alive") is not True or status.get("ready") is not True or status.get("on_ground") is not True
                or status.get("busy") not in (None, False) or status.get("gamemode") != "survival"
                or status.get("dimension") != intent.get("dimension") or not status.get("last_death")
                or not self._safety_rules._number(status.get("speed")) or status["speed"] > .1):
            return
        self._foliage_respawn_checked = generation
        candidate = pending_foliage(self.journal) or {}
        transition = candidate.get("completed_life") or {}
        if transition.get("new_generation") != generation or transition.get("last_death") != status["last_death"]:
            return
        self._pending_leaf_readback = candidate
        self._paused, self.pause_reason, self._next_attempt = False, None, 0
        self._record("INFO", "FOLIAGE_RESPAWN_RECHECK", {"generation": generation, "pos": intent["pos"]})

    def tick(self, automatic=True):
        if self._closed:
            return None
        budget = self._remaining_budget()
        if not self.finished and budget["remaining_seconds"] == 0:
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
            if decision.goal in {"foliage_readback", "continuous_readback"} and not result.success:
                # Keep the exact original intent/result even across repeated
                # restarts and explicit attempts to refresh a failed readback.
                if self.continuous:
                    self._schedule_continuous_retry(result.data.get("readback_reason") or result.error_code,
                                                    result.data.get("retry_after_s", 0))
                elif not self._paused:
                    self._paused, self.pause_reason = True, "action_unconfirmed"
                if self._paused:
                    self.phase = "paused"
                self.last_result = result
                return result
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
            task_replan = self._task_result(decision, result, observation, uncertain=_uncertain_result(result),
                fatal=result.error_code in FATAL | {"death_limit", "internal", "internal_error"})
            if self.paused:
                self._finish_task_feedback(observation, paused=True)
            if (decision.name == "fight_hostile" and result.data.get("retreat_required") is True
                    and not _uncertain_result(result)):
                status = (observation or {}).get("status", {})
                self.context["combat_retreat"] = {"entity_id": decision.args["entity_id"],
                    "name": decision.args["expected_name"], "dimension": status.get("dimension"),
                    "last_death": copy.deepcopy(status.get("last_death"))}
            if decision.name == "fight_hostile" and not result.success and not _uncertain_result(result):
                blocked = {key: until for key, until in self.context.get("combat_blocked_until", {}).items()
                           if type(until) in (int, float) and until > time.time()}
                blocked[str(decision.args["entity_id"])] = time.time() + 30
                self.context["combat_blocked_until"] = dict(list(blocked.items())[-8:])
            self._daily_result(decision, result, observation)
            if self.competition and decision.name not in {"select_task", "update_progress", "finish_task", "force_exit_task", "end_competition"}:
                self.competition.record_action(decision.name, decision.args, asdict(result), observation)
                self.context["competition"] = self.competition.state()
                self._record("INFO", "COMPETITION_TASKS", {"board": self.competition.snapshot()})
            if decision.goal == "explore" and decision.name == "move_to":
                self.context["explore_index"] = self.context.get("explore_index", 0) + 1
            delay = self.settings["poll_interval_seconds"]
            retreat_replan = self._retreat_result(decision, result, observation)
            local_replan = self._local_recovery_result(decision, result, observation, uncertain=_uncertain_result(result))
            combat_replan = (decision.name == "fight_hostile" and not result.success
                and result.data.get("retreat_required") is True and not _uncertain_result(result)
                and result.error_code not in FATAL | {"death_limit", "cancelled", "bad_args"})
            campaign_replan = (decision.name == "campaign_action" and not result.success and not _uncertain_result(result)
                               and result.error_code not in FATAL | {"internal", "internal_error", "cancelled", "bad_args"})
            if not result.success:
                if combat_replan:
                    # The bounded combat step is finished and explicitly asks
                    # for withdrawal. Reobserve before retreating; ordinary
                    # failure backoff must not leave the bot beside the threat.
                    delay = 0
                    self._record("WARN", "COMBAT_RECOVERY_SCHEDULED", {
                        "code": result.error_code, "entity_id": decision.args["entity_id"], "retry_seconds": delay})
                elif task_replan:
                    delay = 0
                elif campaign_replan:
                    delay = .25 if decision.args.get("imminent_explosion") else self.settings["retry_seconds"]
                elif retreat_replan:
                    delay = self._retreat.wait_seconds(observation)
                elif local_replan:
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
                    or (not retreat_replan and not local_replan and not combat_replan and not task_replan and not campaign_replan and not transient_model_error
                        and self.failures >= self.settings["max_consecutive_failures"])):
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
                # A disconnected worker may already have executed the write.
                # Preserve the pending result without sending more actions.
                cleanup_prohibited = result.data.get("cleanup_skipped") in {
                    "disconnected_unknown", "combat_unknown", "unknown_bucket", "task_unknown", "navigation_unknown", "placement_unknown", "foliage_readback", "workflow_unknown"}
                if uncertain_action and not dig_cleanup_attempted and not cleanup_prohibited:
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
                    if decision.goal == "combat_reaction":
                        delay = min(delay, decision.args["seconds"])
                    elif (decision.goal == "safety" and
                            (self.context.get("retreat_recovery") or {}).get("retry_after", 0) > time.time()):
                        delay = self._retreat.wait_seconds(observation)
                    else:
                        delay = max(delay, min(60, decision.args.get("seconds", 5)))
                elif (decision.name in {"retreat_from", "canopy_escape"} and result.data.get("retreat_complete") is False
                      and result.data.get("evacuation_stage") == "relay"):
                    delay = 0  # Next tick reobserves; never replay the saved route.
                elif decision.goal == "foliage_readback":
                    delay = 0  # Fresh safety policy must own the first action after reconciling a leaf.
                elif decision.name == "fight_hostile":
                    delay = 0  # One bounded defensive step; the next cycle rechecks.
                elif task_replan:
                    delay = min(delay, .25)
                terminal_competition = bool(
                    self.competition and self.competition.snapshot().get("phase") == "completed")
                self.completed = decision.name == "goal_complete" or terminal_competition
                self.finished = self.completed
            if self._budget_exhausted:
                self._task_end_at_boundary("global_budget_exhausted")
                self.finished = True
                self.pause_reason = "budget_exhausted"
            self._next_attempt = time.monotonic() + delay
            if self.continuous:
                if self._operator_pause:
                    self._paused, self.pause_reason = True, self._operator_pause
                elif self._paused or result.data.get("retry_after_s", 0) > 0:
                    self._schedule_continuous_retry(self.pause_reason or result.error_code, result.data.get("retry_after_s", 0))
                elif result.success:
                    self._continuous_retries = 0
            self.phase = "paused" if self.paused else "complete" if self.completed else "waiting"
            # Keep uncertain intentions for explicit recovery after a restart.
            keep_pending = ((_uncertain_result(result) and decision.name != "observe")
                            or (self.task_controller and self._task_snapshot().get("status") == "unknown")
                            or (decision.name in {"observe", "wait"} and (observation is None
                                or not observation["status"].get("ready") or observation["status"].get("busy"))))
            self._checkpoint(context=self.context, last_result=asdict(result),
                                pending=self.journal.state.get("pending") if keep_pending else None)
            self._record("INFO" if result.success else "WARN", "STEP_RESULT",
                         {"step": self.steps, "decision": asdict(decision), "result": asdict(result),
                          "paused": self.paused, "phase": self.phase, "pause_reason": self.pause_reason})
            return result
        if self.finished:
            return None
        if budget["remaining_steps"] == 0 or self._budget_exhausted:
            self._task_end_at_boundary("global_budget_exhausted")
            self.finished = True
            self.pause_reason = "budget_exhausted"
            self._record("WARN", "BUDGET_EXHAUSTED", {"steps": self.steps})
        else:
            if self.paused or time.monotonic() < self._next_attempt:
                heartbeat = self._monitor_idle()
                if self.paused and automatic:
                    self._recheck_leaf_after_respawn(heartbeat)
                elif automatic and time.monotonic() >= self._continuous_retry_at:
                    hit = self._combat_reaction.observe(self.client, self.latest_status or {})
                    if hit and hit["t"] != self._last_reaction_hit:
                        self._next_attempt = 0.0
            if (not self.paused and automatic and time.monotonic() >= self._next_attempt
                    and (not self.continuous or time.monotonic() >= self._continuous_retry_at)):
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
