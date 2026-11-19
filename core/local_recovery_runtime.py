"""Bounded local escape/foraging state; saved evidence is never an action queue."""
import copy
import math
import time

from agent.survival_planner import Decision
from skills.survival import SurvivalSkills


class LocalRecoveryRuntime:
    def _recovery_state(self, key, observation):
        status = observation["status"]
        state = self.context.get(key)
        if (not isinstance(state, dict) or state.get("dimension") != status.get("dimension")
                or state.get("life") != status.get("last_death")
                or state.get("life_generation", 0) != self.context.get("life_generation", 0)):
            state = {"dimension": status.get("dimension"), "life": copy.deepcopy(status.get("last_death")),
                "life_generation": self.context.get("life_generation", 0),
                "origin": list(status["pos"]), "steps": 0,
                "visited": [[math.floor(value + .0001) for value in status["pos"]]] if key == "food_recovery" else [],
                "excluded_until": {}}
            self.context[key] = state
        now = time.time()
        state["excluded_until"] = {key: until for key, until in state.get("excluded_until", {}).items()
            if type(until) in (int, float) and math.isfinite(until) and until > now}
        return state

    def _prepare_local_recovery(self, observation):
        if not self.daily:
            return
        status = observation["status"]
        if self._safety_rules._pos(status.get("pos")) is None:
            return
        food = self._recovery_state("food_recovery", observation)
        canopy = self.context.get("canopy_recovery")
        if (isinstance(canopy, dict)
                and canopy.get("life_generation", 0) != self.context.get("life_generation", 0)):
            self.context.pop("canopy_recovery", None)
        water = self.context.get("water_recovery")
        if (isinstance(water, dict)
                and (water.get("life_generation", 0) != self.context.get("life_generation", 0)
                     or water.get("life") != status.get("last_death")
                     or (status.get("in_water") is False and status.get("submerged") is False
                         and status.get("on_ground") is True and observation.get("fresh") is True))):
            self.context.pop("water_recovery", None)
        # Older checkpoints marked night-time waiting as exhausted food search.
        # Only a bounded search or a current daylight evaluation can authorize
        # the limited model turn; a saved wait is not that evidence.
        if (food.get("needs_ai") is True and food["steps"] < 8
                and food.get("daylight_search_evaluated") is not True):
            food.update(needs_ai=False, reason="reobserve_local_food_options")
        footing = observation.get("footing")
        if isinstance(footing, dict) and observation.get("fresh") is True:
            current = {"origin": list(status["pos"]), "support": copy.deepcopy(footing)}
            if self.context.get("local_footing") != current:
                self.context["local_footing"] = current
                self._record("INFO", "LOCAL_FOOTING", current)
        retreat = self.context.get("retreat_recovery") or {}
        verified_footing = self.daily_policy._footing(observation)
        nearest = status.get("nearest_hostile") or {}
        nearby = (isinstance(nearest, dict) and self._safety_rules._number(nearest.get("dist"))
                  and nearest["dist"] <= self._safety_rules._threat_distance(nearest.get("name")))
        stable_full = (observation.get("fresh") is True and observation.get("stale") is not True
            and not self.daily_policy._partial(observation)
            and observation.get("inventory_fresh") is not False and observation.get("terrain_fresh") is not False
            and status.get("ready") is True and status.get("alive") is True and status.get("on_ground") is True
            and not status.get("busy") and not any(status.get(key) for key in
                ("in_water", "in_lava", "on_fire", "submerged", "in_vehicle", "is_sleeping")))
        if (stable_full and retreat.get("evacuating") is True and verified_footing is not None
                and len(observation.get("entities") or []) < 40 and not nearby
                and not self._safety_rules._dangerous_hostiles(observation, status["pos"])):
            # A departed enemy must not force an endless directional retreat.
            # Fresh footing decides whether terrain recovery still remains.
            support = verified_footing["name"]
            if support in SurvivalSkills.RETREAT_LEAVES | SurvivalSkills.RETREAT_TRUNKS:
                canopy = self._recovery_state("canopy_recovery", observation)
                canopy.update(evacuating=True, reason="threat_cleared_continue_terrain_recovery")
                mode = "canopy_escape"
            elif support in SurvivalSkills.SUPPORT:
                self.context.pop("canopy_recovery", None)
                mode = "solid_footing"
            else:
                mode = None
            if mode:
                self.context.pop("retreat_recovery", None)
                self.context.pop("combat_retreat", None)
                self._record("INFO", "RETREAT_THREAT_CLEARED", {"mode": mode, "pos": list(status["pos"]),
                    "support": support, "fresh": True})
        if (type(status.get("food")) in (int, float) and status["food"] >= 18
                or type(status.get("health")) in (int, float)
                and status["health"] > self.settings["low_health_threshold"]):
            food.update(steps=0, visited=[], excluded_until={}, needs_ai=False)
        if not self._task_active():
            self.context.pop("cautious_bootstrap", None)
            self.context.pop("cautious_bootstrap_basis", None)

    def _local_recovery_decision(self, decision, observation):
        if decision is None or not self.daily:
            return decision
        if decision.name == "canopy_escape":
            state = self._recovery_state("canopy_recovery", observation)
            origin = observation["status"]["pos"]
            previous = state.get("search_origin")
            unchanged = previous is not None and math.dist(origin, previous) < .2
            if state["steps"] >= 128:
                if not state.get("exhausted"):
                    state.update(exhausted=True, reason="canopy_step_budget_exhausted")
                    self._record("WARN", "CANOPY_RECOVERY_EXHAUSTED", {"recovery": copy.deepcopy(state)})
                    if self._task_active():
                        self.task_controller.stop("canopy_step_budget_exhausted")
                        self._save_task("TASK_NEEDS_AI")
                        self._finish_task_feedback(observation)
                self._paused = True
                self.pause_reason = "canopy_recovery_exhausted"
                return Decision("wait", {"seconds": 2},
                    "Canopy recovery budget exhausted; saved evidence needs a new strategy before more movement", "safety")
            if state.get("retry_after", 0) > time.time() and unchanged:
                return Decision("wait", {"seconds": 2},
                    "Canopy escape has no verified new step; preserve recovery evidence and reobserve",
                    "safety")
            state.pop("retry_after", None)
        if (self.context.get("survival_constraints") or {}).get("scenario") == "food_recovery_blocked":
            state = self._recovery_state("food_recovery", observation)
            status = observation["status"]
            if not status.get("is_day") or status.get("thundering") or status.get("time_of_day", 24000) >= 11000:
                if state["steps"] < 8 and not self._task_active():
                    state["needs_ai"] = False
                state["reason"] = "food_search_deferred_until_daylight"
                return decision
            first = state.get("needs_ai") is not True
            state.update(needs_ai=True, reason=decision.reason, daylight_search_evaluated=True)
            if first:
                self._record("WARN", "FOOD_RECOVERY_EXHAUSTED", {"recovery": copy.deepcopy(state)})
            if self._task_active() and self.context.get("cautious_bootstrap") is not True:
                self.task_controller.stop("local_food_recovery_exhausted")
                self._save_task("TASK_NEEDS_AI")
                self._finish_task_feedback(observation)
            # A fresh, tightly limited bootstrap permission may let the model
            # choose how to escape the empty-food/empty-tool deadlock. No goal
            # is invented by this fallback; dangerous production stays closed.
            if self.daily_policy.cautious_bootstrap(observation, self.context):
                self.context["survival_constraints"] = self.daily_policy.constraints(observation, context=self.context)
                return None
        return decision

    def _canopy_options(self, observation):
        state = self._recovery_state("canopy_recovery", observation)
        now = time.time()
        state["excluded_until"] = {k: v for k, v in state["excluded_until"].items() if v > now}
        excluded = []
        for key in state["excluded_until"]:
            try:
                point = [int(value) for value in key.split(",")]
            except (ValueError, AttributeError):
                continue
            if len(point) == 3:
                excluded.append(point)
        return {"purpose": "canopy_escape", "evacuating": state.get("evacuating", False),
                "evacuation_origin": state["origin"], "recent_targets": state["visited"][-8:],
                "excluded_targets": excluded[-8:], "threat_radius": self.settings["threat_radius"]}

    def _local_recovery_result(self, decision, result, observation, *, uncertain=False):
        if observation is None or not self.daily:
            return False
        if decision.name == "water_escape":
            state = self._recovery_state("water_recovery", observation)
            state["steps"] += 1
            state["reason"] = result.error_code or result.message
            if uncertain:
                state["unknown"] = True
                self._record("WARN", "LOCAL_RECOVERY_UNKNOWN", {"kind": "water_recovery", "recovery": copy.deepcopy(state)})
                return False
            state.pop("unknown", None)
            if result.success:
                self._record("INFO", "WATER_ESCAPE_COMPLETED", {"result": copy.deepcopy(result.data), "steps": state["steps"]})
                self.context.pop("water_recovery", None)
                return True
            if result.data.get("progress_made") is True:
                state["stalled_steps"] = 0
                state.pop("retry_after", None)
            else:
                stalled = int(state.get("stalled_steps", 0)) + 1
                state["stalled_steps"] = stalled
                if stalled >= 4:
                    state["retry_after"] = time.time() + 15
                    state["stalled_steps"] = 0
            if state["steps"] >= 24:
                state.update(exhausted=True, reason="water_escape_budget_exhausted")
                self._record("WARN", "WATER_ESCAPE_EXHAUSTED", {"recovery": copy.deepcopy(state)})
            return result.error_code not in {"unauthorized", "invalid_credentials", "death_limit", "dead",
                "internal", "internal_error", "cancelled", "life_changed", "dimension_changed"}
        canopy = decision.name == "canopy_escape"
        forage = decision.goal == "food_recovery" and decision.name == "position_for_task"
        if not canopy and not forage:
            return False
        key = "canopy_recovery" if canopy else "food_recovery"
        state = self._recovery_state(key, observation)
        state["steps"] += 1
        state["reason"] = result.error_code or result.message
        if uncertain:
            state["unknown"] = True
            self._record("WARN", "LOCAL_RECOVERY_UNKNOWN", {"kind": key, "recovery": copy.deepcopy(state)})
            return False
        state.pop("unknown", None)
        target = result.data.get("target") or decision.args.get("pos")
        if result.success:
            pos = self._safety_rules._pos(observation["status"].get("pos"))
            if pos and observation.get("fresh") is True:
                cell = [math.floor(value + .0001) for value in pos]
                if canopy and cell in state["visited"]:
                    cell_key = self._safety_rules._key(cell)
                    state["excluded_until"][cell_key] = time.time() + self.settings["target_cooldown_seconds"]
                    state["excluded_until"] = dict(list(state["excluded_until"].items())[-16:])
                state["visited"] = ([row for row in state["visited"] if row != cell] + [cell])[-32:]
            if canopy:
                state["stalled_unsafe"] = 0
                state["evacuating"] = result.data.get("evacuation_stage") != "ground"
                if not state["evacuating"]:
                    self._record("INFO", "CANOPY_ESCAPE_COMPLETED", {"result": copy.deepcopy(result.data), "steps": state["steps"]})
                    self.context.pop(key, None)
                    return True
            state.pop("retry_after", None)
        else:
            if self._safety_rules._pos(target):
                cell = self._safety_rules._key(target)
                state["excluded_until"][cell] = time.time() + self.settings["target_cooldown_seconds"]
                state["excluded_until"] = dict(list(state["excluded_until"].items())[-16:])
            previous_origin = state.get("search_origin")
            current_pos = observation["status"]["pos"]
            unchanged = previous_origin is not None and math.dist(current_pos, previous_origin) < 0.2
            state["search_origin"] = list(current_pos)
            state["retry_after"] = time.time() + 15
            if canopy and result.error_code == "unsafe":
                state["stalled_unsafe"] = int(state.get("stalled_unsafe", 0)) + 1 if unchanged else 1
                footing = self.daily_policy._footing(observation)
                if (state["stalled_unsafe"] >= 3 and isinstance(footing, dict)
                        and footing.get("name") in SurvivalSkills.RETREAT_TRUNKS):
                    state.update(evacuating=False, evacuation_stage="ground", reason="trunk_stalled_fallback")
                    self._record("INFO", "CANOPY_TRUNK_RELEASED", {"recovery": copy.deepcopy(state), "footing": copy.deepcopy(footing)})
                    self.context.pop(key, None)
                    return True
            elif canopy:
                state["stalled_unsafe"] = 0
        if forage and state["steps"] >= 8:
            state.update(needs_ai=True, reason="bounded_food_search_exhausted")
        self._record("INFO" if result.success else "WARN", "CANOPY_RECOVERY" if canopy else "FOOD_RECOVERY",
                     {"recovery": copy.deepcopy(state), "result": copy.deepcopy(result.data)})
        return result.error_code not in {"unauthorized", "invalid_credentials", "death_limit", "dead",
            "internal", "internal_error", "cancelled", "life_changed", "dimension_changed"}
