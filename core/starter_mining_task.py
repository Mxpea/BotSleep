"""Scoped entrance and return intent for a local cobblestone collection task."""

import copy
import json

from agent.survival_planner import Decision
from core.domain import block_key, cell_coords, parse_pos


DIRECTIONS = ("north", "south", "east", "west")
OFFSETS = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}


def rung_depth(site, point):
    if not isinstance(site, dict) or parse_pos(point) is None:
        return None
    entry = parse_pos(site.get("entry"), integer=True)
    direction = site.get("direction")
    if entry is None or not isinstance(direction, str) or direction not in OFFSETS:
        return None
    x, y, z = cell_coords(point)
    dx, dz = OFFSETS[direction]
    depth = entry[1] - y
    if 0 <= depth <= 8 and (x, z) == (entry[0] + dx * depth, entry[2] + dz * depth):
        return depth
    return None


def interrupted_mine(action, result, task):
    """Keep survey coordinates only when the unknown child belongs to that verified mine."""
    if not isinstance(action, dict) or action.get("name") != "step_starter_staircase":
        return None
    args = action.get("args") or {}
    detail = (result or {}).get("data") or {}
    mine = task.get("starter_mine") or {}
    site = mine.get("site")
    target = mine.get("cobblestone_target")
    start = detail.get("pos_before")
    if (not isinstance(site, dict) or site != {k: args.get(k) for k in ("entry", "direction")}
            or any(detail.get(k) != v for k, v in site.items())
            or type(target) is not int or not 1 <= target <= 4096
            or detail.get("executed") is not True or detail.get("action_state") != "unknown"
            or detail.get("operation") not in ("descend", "ascend", "dig") or rung_depth(site, start) is None):
        return None
    return {"site": site, "target": target, "goal": task["args"], "from": list(cell_coords(start)),
            "operation": detail["operation"]}


def recover_interrupted_mine(journal, old):
    """Migrate one older, matching readback from this account/world's bounded journal tail."""
    if (journal.state.get("scope") != journal.scope or old.get("name") != "step_starter_staircase"
            or old.get("generation") != journal.state["context"].get("life_generation")):
        return None
    try:
        with journal.events_path.open("rb") as stream:
            stream.seek(0, 2)
            start = max(0, stream.tell() - 8 * 1024 * 1024)
            stream.seek(start)
            if start:
                stream.readline()
            lines = reversed(stream.read().splitlines()[-5000:])
        abandoned = task = landmark = None
        bound_source = False
        for line in lines:
            row = json.loads(line)
            kind, data = row["event"], row["data"]
            if kind in {"DEATH_OBSERVED", "DIMENSION_CHANGED"}:
                return None
            if abandoned is None:
                if kind != "CONTINUOUS_INTENT_ABANDONED":
                    continue
                action = data.get("decision") or {}
                if (action.get("name") != old["name"] or action.get("args") != old["args"]
                        or data["generation"] != old["generation"] or list(cell_coords(data["pos"])) != old["observed"]["cell"]):
                    return None
                abandoned = data
            elif not task and kind == "TASK_CANCELLED":
                task = data["task_execution"]
                if task["task_id"] != abandoned["task_id"]:
                    return None
                landmark = interrupted_mine(abandoned["decision"], abandoned["prior_result"], task)
                if not landmark:
                    return None
            elif landmark and not bound_source and kind == "DECISION":
                status = data["status"]
                if (data["decision"] != abandoned["decision"] or status["dimension"] != old["observed"]["dimension"]
                        or status.get("last_death") != old["observed"]["life"]
                        or list(cell_coords(status["pos"])) != landmark["from"]):
                    return None
                bound_source = True
            elif bound_source and kind == "STEP_RESULT":
                result = data["result"]
                proof = result.get("data") or {}
                if (result.get("success") is True and not result.get("error_code") and proof.get("verified") is True
                        and data["decision"].get("name") in {"step_starter_staircase", "plan_starter_staircase"}
                        and all(proof.get(k) == v for k, v in landmark["site"].items())):
                    return landmark
                return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        return None


class StarterMiningTask:
    def __init__(self, saved=None):
        saved = {} if saved is None else saved
        if not isinstance(saved, dict):
            raise ValueError("Invalid starter mine state")
        self.site = copy.deepcopy(saved.get("site"))
        self.rejected = copy.deepcopy(saved.get("rejected", {}))
        self.target = saved.get("target", 0)
        self.returning = saved.get("returning", False)
        if (type(self.target) is not int or self.target < 0 or type(self.returning) is not bool
                or self.site is not None and (not isinstance(self.site, dict) or set(self.site) != {"entry", "direction"}
                    or parse_pos(self.site.get("entry"), integer=True) is None
                    or self.site.get("direction") not in DIRECTIONS or self.target < 1)
                or not isinstance(self.rejected, dict) or len(self.rejected) > 16
                or any(not isinstance(key, str) or len(key) > 64 or not isinstance(values, list)
                       or len(values) > 4 or any(v not in DIRECTIONS for v in values)
                       for key, values in self.rejected.items())):
            raise ValueError("Invalid starter mine evidence")

    def state(self):
        return {"site": copy.deepcopy(self.site), "rejected": copy.deepcopy(self.rejected),
                "target": self.target, "returning": self.returning}

    @staticmethod
    def _protection(obs, context):
        evidence = obs.get("daily_evidence") or {}
        for home in (evidence.get("base_site"), context.get("shelter_project"), context.get("base_project")):
            if isinstance(home, dict) and home.get("dimension") == obs["status"]["dimension"]:
                pos = parse_pos(home.get("pos"), integer=True)
                if pos is not None:
                    return {"protected_pos": pos}
        return {}

    def survey(self, obs, context, target):
        origin = obs["status"]["pos"]
        if obs.get("local_geometry_origin") != origin:
            return None
        excluded = self.rejected.get(block_key(cell_coords(origin)), [])
        if len(excluded) == len(DIRECTIONS):
            return None  # Ask the caller to find another freshly observed stance.
        self.target = target
        args = self._protection(obs, context)
        if excluded:
            args["excluded_directions"] = list(excluded)
        return Decision("plan_starter_staircase", args,
                        "No accessible surface stone; survey the current footing for a safe stair entrance", "starter_mining")

    def step(self, obs, context):
        args = {**self.site, **self._protection(obs, context), "from_cell": list(cell_coords(obs["status"]["pos"]))}
        if self.returning or obs["counts"].get("cobblestone", 0) >= self.target:
            self.returning = True
            args["returning"] = True
        reason = ("Return one checked stair at a time before resuming surface work" if self.returning
                  else "Reinspect the stair and clear or descend one increment toward the cobblestone target")
        return Decision("step_starter_staircase", args, reason, "starter_mining")

    def _reject(self, entry, directions):
        key = block_key(entry)
        self.rejected[key] = list(dict.fromkeys(self.rejected.get(key, []) + list(directions)))
        self.rejected = dict(list(self.rejected.items())[-16:])

    def observe_result(self, decision, result, obs):
        """Return an escalation reason only when further mine writes cannot be chosen safely."""
        data = result.data
        if decision.name == "plan_starter_staircase":
            if result.success:
                entry = parse_pos(data.get("entry"), integer=True)
                direction = data.get("direction")
                if (entry is None or direction not in DIRECTIONS or data.get("verified") is not True
                        or tuple(entry) != cell_coords(obs["status"]["pos"])):
                    return "staircase_survey_requires_new_site_evidence"
                self.site = {"entry": entry, "direction": direction}
                self.returning = False
            elif result.error_code in {"no_staircase_site", "staircase_blocked"} and data.get("executed") is False:
                self._reject(cell_coords(obs["status"]["pos"]), DIRECTIONS)
            return None
        if decision.name != "step_starter_staircase" or self.site is None:
            return None
        at_entry = cell_coords(obs["status"]["pos"]) == tuple(self.site["entry"])
        if result.success:
            if data.get("verified") is not True:
                return "staircase_step_requires_new_evidence"
            if decision.args.get("returning") and data.get("at_entry") is True and at_entry:
                self.site = None
                self.returning = False
        elif result.error_code in {"staircase_blocked", "missing_tool"} and data.get("executed") is False:
            self._reject(self.site["entry"], [self.site["direction"]])
            if at_entry:
                self.site = None
                self.returning = False
            elif self.returning:
                return "staircase_return_route_blocked"
            else:
                self.returning = True
        return None
