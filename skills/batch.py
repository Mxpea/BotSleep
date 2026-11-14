"""Small serial batches of existing verified actions; no model or write replay.

Targets are freshly scanned inside the original requested radius. The 120 second
budget is checked between calls; an already dispatched child HTTP operation still
has its existing bounded timeout. Search radius is not a promise about the actual
pathfinder trajectory. A batch checkpoint is never a resumable command list.
"""

import copy
import math
import time

from core.client import APIError
from . import SkillResult
from .survival import SurvivalSkills, _Failure


class _CheckedClient:
    """Apply batch life/safety checks to every status read in the child skill."""

    WRITES = frozenset({"move_to", "dig", "place_block", "interact", "craft", "equip", "use_item", "attack", "window_act"})

    def __init__(self, client, check_status):
        self.client = client
        self.check_status = check_status
        self.writes = 0

    def get_status(self, **kwargs):
        response = self.client.get_status(**kwargs)
        self.check_status(SurvivalSkills._result(response))
        return response

    def __getattr__(self, name):
        operation = getattr(self.client, name)
        if name not in self.WRITES:
            return operation
        def send(*args, **kwargs):
            self.writes += 1
            return operation(*args, **kwargs)
        return send


class BatchSkills:
    ARGUMENTS = {
        "gather_materials": ({"resource", "count", "radius"}, {"resource", "count"}),
        "build_shelter_batch": ({"pos", "material", "max_blocks"}, {"pos", "material"}),
    }
    RESOURCES = {
        "logs": (SurvivalSkills.LOGS, SurvivalSkills.LOGS),
        "stone": ({"stone"}, {"stone", "cobblestone"}),
        "cobblestone": ({"stone", "cobblestone"}, {"cobblestone"}),
        "coal": ({"coal_ore", "deepslate_coal_ore"}, {"coal"}),
    }
    UNKNOWN = frozenset({"request_timeout", "network_error", "invalid_json", "invalid_response",
                         "dig_unconfirmed", "action_unconfirmed", "internal", "internal_error"})
    UNCONFIRMED_AFTER_WRITE = frozenset({"cancelled", "interrupted", "dead", "dimension_changed",
                                       "life_changed", "unsafe_state", "unsafe", "batch_budget_exhausted"})
    MAX_SECONDS = 120

    def __init__(self, client, gather_skills=None, shelter_skills=None):
        self.client = client
        self.gather_skills = gather_skills
        self.shelter_skills = shelter_skills

    @staticmethod
    def _integer(value, name, maximum=8):
        if type(value) is not int or not 1 <= value <= maximum:
            raise _Failure("bad_args", f"{name} must be an integer from 1 to {maximum}")
        return value

    @staticmethod
    def _check(context):
        event = context["cancel"]
        if event is not None and event.is_set():
            raise _Failure("cancelled", "Batch cancelled; completed children will not be replayed")
        if time.monotonic() >= context["deadline"]:
            raise _Failure("batch_budget_exhausted", "Batch budget expired between operations")

    def _guard(self, status, context):
        self._check(context)
        if not isinstance(status, dict):
            raise _Failure("invalid_response", "Batch needs an actual status object")
        health = status.get("health")
        if status.get("alive") is False or type(health) in (int, float) and health <= 0:
            raise _Failure("dead", "Bot died during the batch")
        if status.get("alive") is not True or status.get("ready") is not True:
            raise _Failure("not_ready", "Batch requires a ready living bot")
        if status.get("busy"):
            raise _Failure("busy", "Another backend action is running")
        if status.get("dimension") not in {"overworld", "minecraft:overworld"} or status.get("gamemode") != "survival":
            raise _Failure("unsafe_state", "Batch work is restricted to overworld survival")
        try:
            health = SurvivalSkills._number(status.get("health"), "health", 0, 1024)
            food = SurvivalSkills._number(status.get("food"), "food", 0, 20)
            pos = SurvivalSkills._pos(status.get("pos"))
        except _Failure:
            raise _Failure("invalid_response", "Batch status has invalid life, food or coordinates") from None
        death = status.get("last_death")
        marker = (status["dimension"], death.get("t") if isinstance(death, dict) else None)
        if "life" in context and marker != context["life"]:
            raise _Failure("life_changed", "Life changed; do not continue the previous batch")
        context.setdefault("life", marker)
        context.setdefault("initial_health", health)
        context.setdefault("origin", list(pos))
        if health <= 8 or health < context["initial_health"] or food <= 6:
            raise _Failure("unsafe", "Damage, low health or critical hunger stops batch work")
        if (status.get("on_ground") is not True or status.get("is_sleeping") or status.get("in_vehicle")
                or any(status.get(key) for key in ("in_water", "submerged", "in_lava", "on_fire"))):
            raise _Failure("unsafe_state", "Batch work requires stable safe ground")
        daytime = status.get("time_of_day")
        if (status.get("is_day") is not True or status.get("thundering") is not False
                or not isinstance(daytime, (int, float)) or isinstance(daytime, bool)
                or not math.isfinite(daytime) or not 0 <= daytime < 11000):
            raise _Failure("unsafe", "Batch work ends before dusk, night or a storm")
        hostile = status.get("nearest_hostile")
        if isinstance(hostile, dict):
            distance = hostile.get("dist")
            if type(distance) not in (int, float) or not math.isfinite(distance) or distance <= 8:
                raise _Failure("unsafe", "A nearby or uncertain hostile stops batch work")
        return status

    def _status(self, context):
        return SurvivalSkills._result(context["client"].get_status(timeout_seconds=min(15, max(.01, context["deadline"] - time.monotonic()))))

    def _inventory(self, context):
        self._check(context)
        response = self.client.inventory(timeout_seconds=min(15, max(.01, context["deadline"] - time.monotonic())))
        result = SurvivalSkills._result(response)
        self._check(context)
        rows = result.get("items")
        if not isinstance(rows, list):
            raise _Failure("invalid_response", "Batch inventory is missing its item list")
        counts = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str) or type(row.get("count")) is not int or row["count"] < 0:
                raise _Failure("invalid_response", "Batch inventory has invalid items")
            counts[row["name"]] = counts.get(row["name"], 0) + row["count"]
        return counts

    def _entities(self, context, origin):
        self._check(context)
        rows = self.client.get_entities(radius=16, timeout_seconds=min(15, max(.01, context["deadline"] - time.monotonic())))
        self._check(context)
        if not isinstance(rows, list) or len(rows) > 256:
            raise _Failure("invalid_response", "Batch entity observation is invalid")
        hostiles = []
        for row in rows:
            if not isinstance(row, dict):
                raise _Failure("invalid_response", "Batch entity row is invalid")
            if row.get("type") != "hostile":
                continue
            pos = SurvivalSkills._pos(row.get("pos"))
            if math.dist(origin, pos) <= 8:
                raise _Failure("unsafe", "A freshly observed hostile is close to the bot")
            hostiles.append(pos)
        return hostiles

    @staticmethod
    def _gained(before, after, names):
        return {name: after.get(name, 0) - before.get(name, 0) for name in sorted(names) if after.get(name, 0) > before.get(name, 0)}

    def _child(self, context, skill, name, args):
        self._check(context)
        context["in_child"] = True
        context["child_start_writes"] = context["client"].writes
        result = skill.execute(name, args, cancel_event=context["cancel"])
        context["in_child"] = False
        if not isinstance(result, SkillResult) or not isinstance(result.data, dict):
            raise _Failure("invalid_response", "The child skill returned an invalid result")
        context["data"]["steps"].append({"skill": name, "args": copy.deepcopy(args), "success": result.success,
                                          "error_code": result.error_code, "data": copy.deepcopy(result.data)})
        if not result.success:
            code = result.error_code or "child_failed"
            if ((context["client"].writes > context["child_start_writes"] or result.data.get("executed"))
                    and code in self.UNCONFIRMED_AFTER_WRITE):
                code = "action_unconfirmed"
            details = {"child_error": result.error_code, "child_result": result.data}
            if code in self.UNKNOWN or result.data.get("action_state") == "unknown":
                details["action_state"] = "unknown"
                code = code if code in self.UNKNOWN else "action_unconfirmed"
            raise _Failure(code, "Batch stopped at the first failed child; it will not be replayed", details)
        return result

    def _gather_materials(self, args, context):
        resource = args.get("resource")
        if not isinstance(resource, str) or resource not in self.RESOURCES:
            raise _Failure("bad_args", "resource must be logs, stone, cobblestone or coal")
        requested = self._integer(args["count"], "count")
        radius = self._integer(args.get("radius", 8), "radius")
        names, expected = self.RESOURCES[resource]
        data = context["data"]
        data.update(resource=resource, requested=requested, radius=radius, gained={}, gained_count=0)
        initial = self._status(context)
        before = self._inventory(context)
        data["origin"] = list(initial["pos"])
        visited = set()
        skill = self.gather_skills or SurvivalSkills(context["client"])
        for _ in range(requested):
            status = self._status(context)
            hostiles = self._entities(context, status["pos"])
            rows = self.client.get_blocks(radius=radius, types=sorted(names), limit=64)
            self._check(context)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise _Failure("invalid_response", "Fresh material candidates are invalid")
            candidates = []
            feet = [math.floor(value) for value in status["pos"]]
            for row in rows:
                if row.get("name") not in names:
                    continue
                pos = SurvivalSkills._pos(row.get("pos"), block=True)
                if (tuple(pos) in visited or math.dist(pos, initial["pos"]) > radius or math.dist(pos, status["pos"]) > radius
                        or (pos[0] == feet[0] and pos[2] == feet[2])
                        or any(math.dist(pos, hostile) <= 8 for hostile in hostiles)
                        or (row.get("properties") or {}).get("waterlogged") in (True, "true")
                        or row.get("can_see") is False):
                    continue
                candidates.append((math.dist(pos, status["pos"]), row["name"], pos))
            if not candidates:
                raise _Failure("partial_completion" if data["completed_blocks"] else "no_safe_candidate",
                               "No fresh safe material target remains inside the original batch radius")
            _, name, pos = min(candidates)
            visited.add(tuple(pos))
            data["target"] = list(pos)
            self._status(context)  # Reject a life/hazard change during the scan.
            self._child(context, skill, "gather_block", {"pos": pos, "expected_name": name})
            data["completed_blocks"] += 1
            # A child success alone does not count toward a material goal.
            self._status(context)
            after = self._inventory(context)
            gained = self._gained(before, after, expected)
            total = sum(gained.values())
            if total <= data["gained_count"]:
                raise _Failure("batch_unverified", "Verified block removal did not add the requested material")
            data.update(gained=gained, gained_count=total)
            if total >= requested:
                data.update(completed=True, partial=False, action_state="verified")
                return
        raise _Failure("partial_completion", "The bounded child limit ended before the requested material gain")

    def _build_shelter_batch(self, args, context):
        from .shelter import ShelterSkills
        pos = SurvivalSkills._pos(args["pos"], block=True)
        material = SurvivalSkills._item(args["material"])
        maximum = self._integer(args.get("max_blocks", 8), "max_blocks")
        if material not in ShelterSkills.MATERIALS:
            raise _Failure("unsupported_item", "Batch building only supports the shelter material whitelist")
        data = context["data"]
        data.update(pos=pos, material=material, max_blocks=maximum, used=0, shelter_verified=False)
        self._status(context)
        before = self._inventory(context)
        skill = self.shelter_skills or ShelterSkills(context["client"])
        for _ in range(maximum):
            status = self._status(context)
            self._entities(context, status["pos"])
            result = self._child(context, skill, "build_shelter_step", {"pos": pos, "material": material})
            data["shelter_verified"] = result.data.get("shelter_verified") is True
            data["needs_door"] = result.data.get("needs_door")
            if not result.data.get("executed"):
                data.update(completed=True, partial=False, shell_complete=not bool(result.data.get("missing_blocks")), action_state="verified")
                return
            self._status(context)
            after = self._inventory(context)
            used = before.get(material, 0) - after.get(material, 0)
            if used != data["used"] + 1 or result.data.get("used") != 1:
                raise _Failure("batch_unverified", "Building did not independently consume exactly one material per verified step")
            data.update(used=used, completed_blocks=data["completed_blocks"] + 1)
        data.update(completed=True, partial=False, action_state="verified")

    def execute(self, name, args, cancel_event=None):
        data = {"skill": name, "executed": False, "completed": False, "partial": False,
                "completed_blocks": 0, "steps": [], "budget_seconds": self.MAX_SECONDS}
        context = {"cancel": cancel_event, "deadline": time.monotonic() + self.MAX_SECONDS, "data": data}
        proxy = _CheckedClient(self.client, lambda status: self._guard(status, context))
        context["client"] = proxy
        try:
            if not isinstance(name, str) or name not in self.ARGUMENTS:
                raise _Failure("unknown_skill", "Unknown batch skill")
            allowed, required = self.ARGUMENTS[name]
            if not isinstance(args, dict) or set(args) - allowed or required - set(args):
                raise _Failure("bad_args", "Missing or unsupported batch arguments")
            self._check(context)
            getattr(self, "_" + name)(args, context)
            return SkillResult(True, "Bounded batch verified; this does not establish the whole daily goal", data)
        except Exception as error:
            code = getattr(error, "code", "internal_error")
            data.update(getattr(error, "data", {}))
            if isinstance(getattr(error, "details", None), dict):
                data["api_details"] = error.details
            if context.get("in_child") and proxy.writes > context.get("child_start_writes", proxy.writes) and code in self.UNCONFIRMED_AFTER_WRITE:
                code = "action_unconfirmed"
            if code in self.UNKNOWN and (proxy.writes or data.get("action_state") == "unknown"):
                data["action_state"] = "unknown"
            data["partial"] = data["completed_blocks"] > 0
            data["stop_reason"] = code
            return SkillResult(False, str(error), data, code)
        finally:
            data["executed"] = bool(proxy.writes or any(step["data"].get("executed") for step in data["steps"]))
