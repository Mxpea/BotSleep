"""Bounded goal execution from fresh observations, with no model or client I/O.

State stores goals, evidence and failure exclusions, never executable queues.
Each next_action requires a new caller-owned observation and returns one child.
"""

import copy
import hashlib
import json
import math
import time
from datetime import datetime, timezone

from agent.daily_survival import DAILY_SKILLS, DailySurvivalGoals
from agent.survival_planner import Decision, LOG_NAMES, PLANK_NAMES, SAFE_FOOD, WOOD_SPECIES
from core.daily_policy import DailySafetyPolicy
from core.skill_registry import internal_skills_for_objective
from skills.daily_survival import DailySurvivalSkills


TASKS = frozenset({"gather", "craft", "smelt", "food", "starter_tools", "iron_tools",
                  "shelter", "bed", "storage", "farm", "recover_drops", "maintenance"})
RESOURCES = frozenset({"logs", "cobblestone", "coal", "raw_iron"})
SMELT_ITEMS = frozenset(DailySurvivalSkills.SMELTABLE.values())
RECIPES = {
    "crafting_table": ({"planks": 4}, 1), "stick": ({"planks": 2}, 4),
    "furnace": ({"cobblestone": 8}, 1), "chest": ({"planks": 8}, 1),
    "torch": ({"coal": 1, "stick": 1}, 4), "bread": ({"wheat": 3}, 1),
    "shield": ({"planks": 6, "iron_ingot": 1}, 1), "bucket": ({"iron_ingot": 3}, 1),
    **{f"{tier}_{tool}": ({material: quantity, "stick": sticks}, 1)
       for tier, material in (("wooden", "planks"), ("stone", "cobblestone"), ("iron", "iron_ingot"))
       for tool, quantity, sticks in (("pickaxe", 3, 2), ("axe", 3, 2), ("sword", 2, 1), ("hoe", 2, 2))},
    **{f"iron_{armor}": ({"iron_ingot": count}, 1)
       for armor, count in (("helmet", 5), ("chestplate", 8), ("leggings", 7), ("boots", 4))},
    **{f"{wood}_planks": ({f"{wood}_log": 1}, 4) for wood in WOOD_SPECIES},
    **{f"{wood}_door": ({f"{wood}_planks": 6}, 3) for wood in WOOD_SPECIES},
    **{bed: ({bed[:-4] + "_wool": 3, "planks": 3}, 1) for bed in DailySurvivalSkills.BEDS},
}
CRAFT_ITEMS = frozenset(RECIPES)
STATES = frozenset({"idle", "running", "suspended", "completed", "needs_ai", "unknown", "cancelled"})
UNKNOWN = frozenset({"request_timeout", "network_error", "invalid_json", "invalid_response",
                     "dig_unconfirmed", "action_unconfirmed", "internal", "internal_error"})
PATH_FAILURES = frozenset({"stalled", "no_path", "path_not_found", "out_of_reach", "out_of_range",
                           "move_unverified", "target_occluded", "visibility_unknown", "unsafe_target"})
TASK_ROWS = {"food": "food_reserve", "farm": "food_production", "maintenance": "maintain"}
RESOURCE_NAMES = {"logs": frozenset(LOG_NAMES), "cobblestone": frozenset({"stone", "cobblestone"}),
                  "coal": frozenset({"coal_ore", "deepslate_coal_ore"}),
                  "raw_iron": DailySurvivalSkills.IRON}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


class AutonomousTaskController:
    """A caller executes children and supplies their real results and observations."""

    SHELTER_SEARCH_TTL = 1800

    def __init__(self, settings=None, saved=None, *, scope=None):
        self.settings = copy.deepcopy(settings or {})
        # Scoped negative hints only, never restored as fresh safety evidence.
        self._world_scope = copy.deepcopy(scope or {"world_id": self.settings.get("world_id")})
        self._shelter_search = {"scope": None, "rejected": {}, "visited": {}}
        options = self.settings.get("autonomy", {})
        self.enabled = options.get("enabled", True) is True
        self.limits = {}
        for key, default, maximum in (("max_steps", 128, 2048), ("max_seconds", 600, 3600),
                ("max_local_failures", 4, 32), ("max_no_progress_steps", 8, 128), ("max_explore_steps", 8, 64)):
            value = options.get(key, default)
            valid = (_finite(value) and 0 < value <= maximum) if key == "max_seconds" else (
                type(value) is int and (0 if key == "max_explore_steps" else 1) <= value <= maximum)
            if not valid:
                raise ValueError(f"Invalid bounded autonomy.{key}")
            self.limits[key] = value
        self.daily = DailySurvivalGoals(self.settings)
        self.rules = self.daily.rules
        self.safety_policy = DailySafetyPolicy(self.settings)
        self._data = {"version": 1, "task_id": None, "task": None, "status": "idle", "args": {},
                      "progress": {"current": 0, "target": 0, "unit": "items"}, "current_step": None,
                      "local_recoveries": 0, "emergency_interruptions": 0, "ai_escalations": 0,
                      "last_escalation_reason": None, "reason": "No goal selected", "fresh": False,
                      "updated_at": None, "steps": 0, "max_steps": self.limits["max_steps"],
                      "recent_results": [], "blocked": {}, "blocked_codes": {}, "visited": [], "jobs": [],
                      "blocked_entities": {}, "observed_drop_ids": [], "recovered_drop_ids": [],
                      "drop_before": None,
                      "local_failures": 0, "no_progress": 0, "explore_steps": 0,
                      "survey_steps": 0, "survey_since_move": 0, "rejected_sites": [],
                      "preparation_site": None, "preparation_attempts": {},
                      "pending_digest": None, "baseline": 0, "sequence": 0, "life_generation": 0}
        if saved is not None:
            self._restore(saved)
            self._restore_shelter_search(saved.get("shelter_search_cache"))

    @staticmethod
    def _args(args):
        if not isinstance(args, dict) or set(args) - {"task", "resource", "item", "count", "radius"}:
            raise ValueError("Unsupported task arguments")
        task = args.get("task")
        if not isinstance(task, str) or task not in TASKS:
            raise ValueError("Unsupported autonomous task")
        value = {"task": task, "count": args.get("count", 8), "radius": args.get("radius", 32)}
        for key, low, high in (("count", 1, 64), ("radius", 4, 64)):
            if type(value[key]) is not int or not low <= value[key] <= high:
                raise ValueError(f"{key} must be an integer from {low} to {high}")
        if task == "gather":
            if not isinstance(args.get("resource"), str) or args["resource"] not in RESOURCES or "item" in args:
                raise ValueError("gather requires a supported resource and no item")
            value["resource"] = args["resource"]
        elif task in {"craft", "smelt"}:
            allowed = CRAFT_ITEMS if task == "craft" else SMELT_ITEMS
            if not isinstance(args.get("item"), str) or args["item"] not in allowed or "resource" in args:
                raise ValueError(f"{task} requires a supported output item and no resource")
            value["item"] = args["item"]
        elif "item" in args or "resource" in args:
            raise ValueError("Only gather/craft/smelt accept resource or item")
        return value

    def _restore(self, saved):
        if (not isinstance(saved, dict) or saved.get("version") != 1
                or saved.get("status") not in STATES
                or len(json.dumps(saved, allow_nan=False).encode()) > 65536):
            raise ValueError("Invalid autonomous task state")
        if saved.get("task") is None:
            if saved.get("status") != "idle":
                raise ValueError("Saved active task has no goal")
            return
        args = self._args(saved.get("args"))
        if args["task"] != saved["task"] or not _finite(saved.get("started_at")):
            raise ValueError("Saved task identity or clock is invalid")
        self._data.update({key: copy.deepcopy(saved[key]) for key in self._data if key in saved})
        for key in ("started_at", "dimension", "life", "fingerprint", "origin"):
            self._data[key] = copy.deepcopy(saved.get(key))
        self._data["args"] = args
        for key in ("steps", "local_failures", "no_progress", "explore_steps", "baseline", "sequence",
                    "local_recoveries", "emergency_interruptions", "ai_escalations", "life_generation",
                    "survey_steps", "survey_since_move"):
            if type(self._data[key]) is not int or self._data[key] < 0:
                raise ValueError("Saved counters are invalid")
        for key, maximum in (("recent_results", 8), ("visited", 16), ("jobs", 8),
                             ("observed_drop_ids", 40), ("recovered_drop_ids", 40), ("rejected_sites", 128)):
            if not isinstance(self._data[key], list) or len(self._data[key]) > maximum:
                raise ValueError("Saved evidence exceeds its limit")
        if not all(isinstance(key, str) and len(key) <= 100 for key in self._data["rejected_sites"]):
            raise ValueError("Saved survey exclusions are invalid")
        attempts = self._data["preparation_attempts"]
        if (not isinstance(attempts, dict) or len(attempts) > 32
                or any(not isinstance(k, str) or len(k) > 100 or type(v) is not int or not 0 <= v <= 8
                       for k, v in attempts.items())):
            raise ValueError("Saved site preparation budget is invalid")
        intent = self._data["preparation_site"]
        if intent is not None and (not isinstance(intent, dict) or self.rules._pos(intent.get("pos")) is None
                or intent.get("orientation") not in {"north", "east", "south", "west"}
                or intent.get("stage") not in {"plan", "prepare", "survey", "position"}
                or not isinstance(intent.get("clear_cells", []), list) or len(intent.get("clear_cells", [])) > 8
                or any(self.rules._pos(p) is None for p in intent.get("clear_cells", []))
                or not isinstance(intent.get("plan_orientations", []), list)
                or len(intent.get("plan_orientations", [])) > 4
                or any(o not in {"north", "east", "south", "west"} for o in intent.get("plan_orientations", []))):
            raise ValueError("Saved site preparation intent is invalid")
        if not isinstance(self._data["blocked"], dict) or len(self._data["blocked"]) > 16:
            raise ValueError("Saved exclusions exceed their limit")
        if not isinstance(self._data["blocked_entities"], dict) or len(self._data["blocked_entities"]) > 16:
            raise ValueError("Saved entity exclusions exceed their limit")
        if not isinstance(self._data["blocked_codes"], dict) or len(self._data["blocked_codes"]) > 16:
            raise ValueError("Saved failure reasons exceed their limit")
        self._data["fresh"] = False
        if self._data.get("pending_digest"):
            self._set("unknown", "A saved child has no verified result; never replay it")
        elif self._data["status"] == "running":
            self._set("suspended", "Restored goal requires a fresh observation")

    @staticmethod
    def _life(status):
        death = status.get("last_death")
        return death.get("t") if isinstance(death, dict) else None

    def _generation_changed(self, context):
        # The API may omit last_death across a real death and respawn. The
        # harness generation binds inventory baselines to the observed life.
        return (isinstance(context, dict)
                and context.get("life_generation", 0) != self._data["life_generation"])

    def _fresh(self, obs):
        status = obs.get("status") if isinstance(obs, dict) else None
        counts = obs.get("counts") if isinstance(obs, dict) else None
        return (isinstance(status, dict) and obs.get("fresh") is True and obs.get("stale") is not True
                and obs.get("observation_scope") != "safety" and obs.get("inventory_fresh") is not False
                and isinstance(counts, dict) and all(isinstance(k, str) and type(v) is int and v >= 0 for k, v in counts.items())
                and status.get("ready") is True and status.get("alive") is True
                and self.rules._pos(status.get("pos")) is not None)

    def _set(self, status, reason):
        self._data.update(status=status, reason=str(reason)[:240],
                          updated_at=datetime.fromtimestamp(time.time(), timezone.utc).isoformat())

    def _escalate(self, reason):
        if self._data["status"] != "needs_ai":
            self._data["ai_escalations"] += 1
        self._data["last_escalation_reason"] = str(reason)[:160]
        self._set("needs_ai", reason)

    def _bind_shelter_search(self, status, generation):
        identity = _digest([self._world_scope, status.get("dimension"), self._life(status), generation])
        if self._shelter_search["scope"] != identity:
            self._shelter_search = {"scope": identity, "rejected": {}, "visited": {}}
        self._prune_shelter_search()

    def _restore_shelter_search(self, saved):
        """Optional checkpoint extension: reject malformed/foreign hints only."""
        if not isinstance(saved, dict) or saved.get("version") != 1:
            return
        expected = _digest([self._world_scope, self._data.get("dimension"),
                            self._data.get("life"), self._data["life_generation"]])
        if saved.get("scope") != expected:
            return
        now = time.time()
        restored = {"scope": expected, "rejected": {}, "visited": {}}
        for kind, maximum in (("rejected", 128), ("visited", 32)):
            rows = saved.get(kind)
            if not isinstance(rows, dict) or len(rows) > maximum:
                return
            for key, row in rows.items():
                if not isinstance(row, dict):
                    return
                pos, stamp, expiry = self.rules._pos(row.get("pos")), row.get("observed_at"), row.get("expires_at")
                if (pos is None or not _finite(stamp) or not _finite(expiry)
                        or stamp > now + 2 or not 0 < expiry - stamp <= self.SHELTER_SEARCH_TTL
                        or row.get("negative_hint") is not True):
                    return
                if kind == "rejected":
                    if (row.get("orientation") not in {"north", "east", "south", "west"}
                            or row.get("error_code") not in {"unsafe_site", "site_occupied"}
                            or any(v != math.floor(v) for v in pos)
                            or key != self.rules._key(pos) + "|" + row["orientation"]):
                        return
                elif key != self.rules._key(pos):
                    return
                if expiry > now:
                    restored[kind][key] = copy.deepcopy(row)
        self._shelter_search = restored

    def _prune_shelter_search(self):
        now = time.time()
        for kind in ("rejected", "visited"):
            self._shelter_search[kind] = {key: row for key, row in self._shelter_search[kind].items()
                                           if row["expires_at"] > now}

    def _remember_shelter_search(self, kind, key, **fields):
        self._prune_shelter_search()
        rows = self._shelter_search[kind]
        rows.pop(key, None)
        now = time.time()
        rows[key] = {**fields, "negative_hint": True, "observed_at": now,
                     "expires_at": now + self.SHELTER_SEARCH_TTL}
        self._shelter_search[kind] = dict(list(rows.items())[-(128 if kind == "rejected" else 32):])

    def _shelter_search_report(self):
        self._prune_shelter_search()
        rejected = list(self._shelter_search["rejected"].values())
        visits = list(self._shelter_search["visited"].values())
        centers = list(dict.fromkeys(tuple(row["pos"]) for row in rejected))
        points = centers + [tuple(row["pos"]) for row in visits]
        return {"negative_hints_only": True, "safety_verified": False, "ttl_seconds": self.SHELTER_SEARCH_TTL,
                "rejected_center_count": len(centers), "rejected_orientation_count": len(rejected),
                "recent_rejected_centers": [list(pos) for pos in centers[-16:]],
                "recent_rejections": [{key: row[key] for key in ("pos", "orientation", "error_code")}
                                      for row in rejected[-8:]],
                "visited_stances": [row["pos"] for row in visits[-16:]],
                "searched_bounds": ({"min": [min(p[i] for p in points) for i in range(3)],
                                     "max": [max(p[i] for p in points) for i in range(3)]} if points else None),
                "guidance": "Recent rejected footprints and visited stances are avoidance hints, not safety proof. "
                            "Choose a different observed direction or search region instead of returning to these centers; "
                            "all movement and any new site still require fresh validation."}

    def start(self, args, observation, context=None):
        """Select a bounded goal; no action is executed or cached by this call."""
        args = self._args(args)
        if not self.enabled:
            raise ValueError("Autonomous tasks are disabled")
        if self._data["status"] == "unknown" or self._data.get("pending_digest") or self._unknown(context):
            raise ValueError("Resolve the unknown child outcome before selecting another goal")
        if not self._fresh(observation):
            raise ValueError("Starting a goal requires a fresh complete living observation")
        status = observation["status"]
        if status.get("dimension") not in {"overworld", "minecraft:overworld"} or status.get("gamemode") != "survival":
            raise ValueError("Autonomous production requires overworld survival")
        generation = (context or {}).get("life_generation", 0)
        if type(generation) is not int or generation < 0:
            raise ValueError("Invalid observed life generation")
        self._bind_shelter_search(status, generation)
        sequence = self._data["sequence"] + 1
        replacement = AutonomousTaskController(self.settings)
        self._data = replacement._data
        self._data.update(task_id=f"task-{int(time.time() * 1000)}-{sequence}", task=args["task"], args=args,
                          sequence=sequence, started_at=time.time(), dimension=status["dimension"],
                          life=self._life(status), life_generation=generation,
                          origin=list(status["pos"]), fresh=True)
        self.daily = replacement.daily
        if args["task"] == "recover_drops":
            self._data["observed_drop_ids"] = [row["id"] for row in self.daily._rows(observation.get("entities"))
                if type(row.get("id")) is int and self.daily._drop_name(row)][:40]
        current = self._quantity(observation)
        incremental = args["task"] in {"gather", "craft", "smelt"}
        self._data["baseline"] = current if incremental else 0
        self._data["progress"] = {"current": current, "target": current + args["count"] if incremental else args["count"]
                                 if args["task"] == "food" else max(1, len(self._data["observed_drop_ids"]))
                                 if args["task"] == "recover_drops" else 1, "unit": self._unit()}
        self._data["fingerprint"] = self._fingerprint(observation)
        self._set("running", "Goal selected; every child requires fresh evidence")
        return self.snapshot()

    @staticmethod
    def _unknown(context):
        context = context if isinstance(context, dict) else {}
        last = context.get("last_result") if isinstance(context.get("last_result"), dict) else {}
        data = last.get("data") if isinstance(last.get("data"), dict) else {}
        return bool(context.get("pending") or context.get("action_state") == "unknown"
                    or data.get("action_state") == "unknown" or last.get("error_code") in UNKNOWN)

    def _unit(self):
        args = self._data["args"]
        return args.get("resource", args.get("item", "food_items" if args["task"] == "food" else "goal"))

    def _quantity(self, observation):
        counts = observation["counts"]
        args = self._data["args"]
        if args["task"] == "gather":
            return sum(counts.get(n, 0) for n in LOG_NAMES) if args["resource"] == "logs" else counts.get(args["resource"], 0)
        if args["task"] in {"craft", "smelt"}:
            return counts.get(args["item"], 0)
        if args["task"] == "food":
            return sum(counts.get(n, 0) for n in SAFE_FOOD)
        if args["task"] == "recover_drops":
            return len(self._data["recovered_drop_ids"])
        return 0

    def _fingerprint(self, observation):
        status = observation["status"]
        rows = self.rules._blocks(observation)
        return _digest({"counts": observation["counts"], "cell": [math.floor(v) for v in status["pos"]],
            "blocks": sorted((tuple(row["pos"]), row["name"]) for row in rows)[:256],
            "facilities": observation.get("daily_evidence", {}), "furnaces": observation.get("furnaces", [])})

    def _context(self, context, observation=None):
        result = copy.deepcopy(context or {})
        now = time.time()
        if self._fresh(observation):
            self._bind_shelter_search(observation["status"], self._data["life_generation"])
        self._data["blocked"] = {k: v for k, v in self._data["blocked"].items() if _finite(v) and v > now}
        if observation is not None:
            for row in self.rules._blocks(observation):
                key = self.rules._key(row["pos"])
                if (row.get("can_see") is True and self._data["blocked_codes"].get(key) in {"target_occluded", "visibility_unknown"}):
                    self._data["blocked"].pop(key, None)
        self._data["blocked_codes"] = {k: v for k, v in self._data["blocked_codes"].items() if k in self._data["blocked"]}
        self._data["blocked_entities"] = {k: v for k, v in self._data["blocked_entities"].items() if _finite(v) and v > now}
        result["blocked_targets"] = sorted(self.rules._blocked(result) | set(self._data["blocked"]))
        result["capabilities"] = list(set(result.get("capabilities", DAILY_SKILLS)) - {"gather_materials", "build_shelter_batch"})
        if self._data["task"] == "shelter":
            result["capabilities"] = sorted(set(result["capabilities"]) | internal_skills_for_objective("daily"))
        result["smelting_jobs"] = (self.daily._rows(result.get("smelting_jobs")) + copy.deepcopy(self._data["jobs"]))[-8:]
        result["explore_index"] = self._data["explore_steps"]
        self._prune_shelter_search()
        result["shelter_rejected_sites"] = list(dict.fromkeys(
            [*self._shelter_search["rejected"], *self._data["rejected_sites"]]))
        result["shelter_visited_stances"] = list(self._shelter_search["visited"])
        result["shelter_survey_reposition"] = self._data["survey_since_move"] >= 4
        result["shelter_survey_exhausted"] = self._data["survey_steps"] >= 32
        return result

    def _decision(self, name, args, reason):
        return Decision(name, copy.deepcopy(args), reason, self._data["task"])

    def _row_decision(self, row):
        return self._decision(row["name"], row["args"], row.get("reason", "Continue the selected goal")) if row else None

    def _drop(self, names, obs, context):
        for row in self.daily._safe_rows(obs, entities=True, context=context):
            item = self.daily._drop_name(row)
            if item in names and type(row.get("id")) is int and math.dist(obs["status"]["pos"], row["pos"]) <= 8:
                return self._decision("collect_drops", {"entity_id": row["id"], "expected_item": item}, "Collect freshly observed needed drops before producing more")
        return None

    def _gather(self, resource, obs, context, names=None):
        counts = obs["counts"]
        cautious = context.get("_bounded_bootstrap") is True
        if cautious and resource != "logs":
            return None
        drops = None if cautious else self._drop(set(names or LOG_NAMES) if resource == "logs" else {resource}, obs, context)
        if drops:
            return drops
        if resource != "logs":
            required = "stone_pickaxe" if resource == "raw_iron" else "wooden_pickaxe"
            minimum = ("stone", "iron", "diamond", "netherite") if resource == "raw_iron" else ("wooden", "stone", "iron", "diamond", "netherite")
            if not any(counts.get(tier + "_pickaxe", 0) for tier in minimum):
                return self._craft(required, 1, obs, context)
        origin = obs["status"]["pos"]
        blocks = self.rules._blocks(obs)
        blocked, hostiles = self.rules._blocked(context), self.rules._hostiles(obs, origin)
        blocked_occlusion = {key for key, code in self._data["blocked_codes"].items()
                             if code in {"target_occluded", "visibility_unknown"}}
        candidates = [row for row in blocks if row["name"] in (names or RESOURCE_NAMES[resource])
                      and math.dist(origin, row["pos"]) <= self._data["args"]["radius"]
                      and (not cautious or row.get("can_see") is True and row.get("can_dig") is True
                           and math.dist(origin, row["pos"]) <= 3)
                      and self.rules._safe_target(row, origin, blocks, blocked - blocked_occlusion, hostiles, observation=obs)]
        candidates.sort(key=lambda row: (0 if row.get("can_see") is True else 2 if row.get("can_see") is False else 1,
                                          math.dist(origin, row["pos"]), row["pos"]))
        for row in candidates:
            action = {"name": "gather_resource" if resource == "raw_iron" else "gather_block",
                      "args": {"pos": list(row["pos"]), "expected_name": row["name"]},
                      "reason": "Gather one fresh resource for the selected goal", "goal": self._data["task"]}
            alternative = self.daily._foliage_hint(action, obs, context, context["capabilities"])
            if alternative["name"] == "clear_foliage" or (row.get("can_see") is not False and self.rules._key(row["pos"]) not in blocked):
                return self._row_decision(alternative)
        return None

    def _obtain(self, item, needed, obs, context, depth):
        counts = obs["counts"]
        if item == "planks":
            for wood, planks in zip(WOOD_SPECIES, PLANK_NAMES):
                available = sum(counts.get(name, 0) for name in self._wood_inputs(wood))
                if available:
                    return self._decision("craft_item", {"item": planks, "count": min(64, available * 4, 4 * math.ceil(needed / 4))}, "Convert held wood into required planks")
            return self._gather("logs", obs, context)
        if item in CRAFT_ITEMS:
            return self._craft(item, needed, obs, context, depth + 1)
        if item == "iron_ingot":
            return self._smelt(item, obs, context, depth + 1)
        if item in {"cobblestone", "coal", "raw_iron"}:
            return self._gather(item, obs, context)
        if item in LOG_NAMES:
            return self._gather("logs", obs, context, {item})
        if item == "wheat":
            for row in self.daily._safe_rows(obs, {"wheat"}, context=context):
                age = (row.get("properties") or {}).get("age")
                if age in (7, "7"):
                    return self._decision("harvest_crop", {"pos": row["pos"], "expected_name": "wheat"}, "Harvest currently mature wheat")
        return self._drop({item}, obs, context)

    @staticmethod
    def _wood_inputs(wood):
        return (f"{wood}_log", f"{wood}_wood", f"stripped_{wood}_log", f"stripped_{wood}_wood")

    def _resource_child(self, decision, obs, context):
        """Board/workstation prerequisites use the same fresh resource recovery."""
        if decision and decision.name in {"gather_block", "gather_resource"}:
            expected = decision.args.get("expected_name")
            for resource, names in RESOURCE_NAMES.items():
                if expected in names:
                    return self._gather(resource, obs, context)
        if (context.get("_bounded_bootstrap") is True and decision
                and decision.name == "place_item" and decision.args.get("item") == "crafting_table"):
            for row in self.daily._rows(obs.get("placement_candidates"))[:8]:
                pos = self.rules._pos(row.get("pos"))
                if pos is None or math.dist(obs["status"]["pos"], pos) > 3:
                    continue
                candidate = self._decision("place_item", {"item": "crafting_table", "pos": list(pos)},
                                           "Place the held workstation at one freshly verified close empty cell")
                if self.safety_policy.validate(candidate, obs, context) is None:
                    return candidate
            return None
        return decision

    def _craft(self, item, needed, obs, context, depth=0):
        if depth > 8 or item not in RECIPES:
            return None
        counts = obs["counts"]
        recipe, output = RECIPES[item]
        # One recipe at a time keeps prerequisite acquisition bounded. The
        # external count still means the required inventory increase.
        for ingredient, amount in recipe.items():
            held = sum(counts.get(n, 0) for n in PLANK_NAMES) if ingredient == "planks" else counts.get(ingredient, 0)
            if item in PLANK_NAMES:
                held = sum(counts.get(name, 0) for name in self._wood_inputs(item[:-7]))
            if item == "torch" and ingredient == "coal":
                held += counts.get("charcoal", 0)
            if held < amount:
                return self._obtain(ingredient, amount - held, obs, context, depth)
        if item not in {"stick", "crafting_table", "torch", *PLANK_NAMES}:
            workspace = self.rules._workspace(obs, context)
            if workspace:
                return self._resource_child(workspace, obs, context) if workspace.name != "wait" else None
        return self._decision("craft_item", {"item": item, "count": min(64, needed, output)}, "Craft one available recipe and verify the new inventory")

    def _smelt(self, item, obs, context, depth=0):
        if depth > 8:
            return None
        counts = obs["counts"]
        current = self.daily._smelt({item}, obs, counts, self._data["task"], context)
        if current:
            return self._row_decision(current)
        # A job is a place to inspect, never permission to transfer again.
        for job in context.get("smelting_jobs", []):
            if (job.get("expected_output") == item and job.get("dimension") == obs["status"].get("dimension")
                    and self.rules._pos(job.get("pos")) and self.rules._key(job["pos"]) not in self.rules._blocked(context)):
                return self._decision("inspect_container", {"pos": job["pos"]}, "Inspect the current pending furnace job")
        if not self.daily._safe_rows(obs, {"furnace"}, context=context):
            if counts.get("furnace", 0):
                return self._row_decision(self.daily._place("furnace", obs, self._data["task"], context))
            return self._craft("furnace", 1, obs, context, depth + 1)
        inputs = [name for name, output in DailySurvivalSkills.SMELTABLE.items() if output == item]
        if not any(counts.get(name, 0) for name in inputs):
            if item == "iron_ingot":
                return self._gather("raw_iron", obs, context)
            if item == "charcoal":
                return self._gather("logs", obs, context)
            animals = {"cooked_beef": "cow", "cooked_porkchop": "pig", "cooked_mutton": "sheep", "cooked_chicken": "chicken"}
            if item in animals:
                return self._hunt({animals[item]}, obs, context)
            if item == "baked_potato":
                for row in self.daily._safe_rows(obs, {"potatoes"}, context=context):
                    if (row.get("properties") or {}).get("age") in (7, "7"):
                        return self._decision("harvest_crop", {"pos": row["pos"], "expected_name": "potatoes"}, "Acquire potatoes for the selected cooked output")
            return self._drop(set(inputs), obs, context)
        if not any(counts.get(fuel, 0) for fuel in {"coal", "charcoal", *PLANK_NAMES}):
            return self._obtain("planks", 2, obs, context, depth + 1)
        return None

    def _hunt(self, names, obs, context):
        for row in self.daily._safe_rows(obs, names, entities=True, context=context):
            if type(row.get("id")) is not int or math.dist(row["pos"], obs["status"]["pos"]) > 8:
                continue
            if not any(count and (name.endswith("_sword") or name.endswith("_axe")) for name, count in obs["counts"].items()):
                return self._craft("stone_sword", 1, obs, context)
            if not self.daily._can_hunt(obs, obs["counts"], row):
                continue
            if math.dist(row["pos"], obs["status"]["pos"]) > 2.8:
                return self._decision("move_to", {"pos": row["pos"], "range": 2}, "Approach the observed passive animal once and reobserve")
            return self._decision("hunt_animal", {"entity_id": row["id"], "expected_name": row["name"]}, "Hunt only the species needed for this goal")
        return None

    def _food(self, obs, context):
        decision = self.daily._food_action(obs, obs["counts"], context)
        if decision:
            return self._row_decision(decision)
        for source, output in DailySurvivalSkills.SMELTABLE.items():
            if output in SAFE_FOOD and obs["counts"].get(source, 0):
                return self._smelt(output, obs, context)
        return self._hunt(set(DailySurvivalSkills.ANIMALS), obs, context)

    def _local_shelter_step(self, origin, pos, obs):
        """Authorize only a freshly observed adjacent cardinal stair candidate.

        This is a planning filter, not route proof. PositioningSkills checks
        the transition body, headroom, support and final position again.
        """
        if (self._data["task"] != "shelter" or obs.get("local_geometry_origin") != origin
                or abs(origin[1] - round(origin[1])) > .05
                or pos[1] != math.floor(pos[1])
                or abs(pos[1] - round(origin[1])) != 1
                or any(abs(pos[i] - math.floor(pos[i]) - .5) > 1e-6 for i in (0, 2))
                or sum(abs(math.floor(pos[i]) - math.floor(origin[i])) for i in (0, 2)) != 1):
            return False
        return any(row.get("pos") == list(pos) and row.get("support") in DailySurvivalSkills.SUPPORT
                   for row in self.daily._rows(obs.get("safe_stand_candidates"))[:8])

    def _explore(self, obs, context):
        if context.get("_bounded_bootstrap") is True:
            return None
        if self._data["explore_steps"] >= self.limits["max_explore_steps"]:
            return None
        origin = obs["status"]["pos"]
        if obs.get("local_geometry_origin") != origin:
            return None
        blocked = self.rules._blocked(context)
        hostiles = self.rules._hostiles(obs, origin)
        for row in self.daily._rows(obs.get("safe_stand_candidates"))[:8]:
            pos = self.rules._pos(row.get("pos"))
            if pos is None or row.get("support") not in DailySurvivalSkills.SUPPORT:
                continue
            key = self.rules._key(pos)
            if (key in blocked or key in self._data["visited"]
                    or self._data["task"] == "shelter" and key in context.get("shelter_visited_stances", [])
                    or not 1 <= math.dist(origin, pos) <= 3
                    or abs(pos[1] - origin[1]) > .15 and not self._local_shelter_step(origin, pos, obs)
                    or any(math.dist(pos, point) <= self.rules.threat_radius for _, point in hostiles)):
                continue
            return Decision("position_for_task", {"pos": list(pos)}, "Inspect from one freshly checked local stance; route is revalidated by the skill", "explore")
        return None

    def _board_action(self, task, obs, context):
        if task == "shelter" and not self.daily._verified(
                (obs.get("daily_evidence") or {}).get("base_site"), obs["status"].get("dimension")):
            intent = self._data["preparation_site"]
            if (intent and intent["stage"] == "plan"
                    and (self._data["preparation_attempts"].get(self.rules._key(intent["pos"]), 0) >= 8
                         or self._data["survey_steps"] >= 32)):
                self._data["preparation_site"] = intent = None
            if intent:
                args = {"pos": list(intent["pos"]), "orientation": intent["orientation"]}
                if intent["stage"] == "position":
                    move = self._preparation_stance(obs, context, intent.get("clear_cells", []))
                    if move:
                        return move, False
                    self._data["preparation_site"] = None
                else:
                    name = {"plan": "plan_shelter_preparation", "prepare": "prepare_shelter_step",
                            "survey": "plan_shelter"}[intent["stage"]]
                    if name == "plan_shelter":
                        args["material"] = "oak_planks"
                    return self._decision(name, args, "Revalidate the small natural site preparation; only a new shelter survey can authorize a base"), False
            # A failed read-only survey calls for another checked site or a
            # bounded local exploration step, not unrelated tool production.
            return self._row_decision(self.daily._base_candidate(obs, obs["counts"], context)), False
        board = self.daily.build(obs, context.get("memory"), context)
        rows = {row["id"]: row for row in board["tasks"]}
        key = TASK_ROWS.get(task, task)
        if task == "maintenance":
            if board["maintenance_ready"]:
                return self._decision("wait", {"seconds": 5}, "Maintenance conditions currently hold; inspect again"), False
            key = board.get("active_task_id")
        row = rows.get(key)
        if row is None:
            return None, False
        if row["status"] == "satisfied":
            return None, task != "maintenance"
        seen = set()
        def choose(entry):
            if not entry or entry["id"] in seen:
                return None
            seen.add(entry["id"])
            if entry.get("next_action"):
                return self._row_decision(entry["next_action"])
            for required in entry.get("depends_on", []):
                dependency = rows.get(required)
                if dependency and dependency["status"] != "satisfied":
                    candidate = choose(dependency)
                    if candidate:
                        return candidate
            return None
        candidate = choose(row)
        if task == "shelter" and candidate and candidate.name == "inspect_shelter":
            shelter = obs.get("shelter") or {}
            base = (obs.get("daily_evidence") or {}).get("base_site") or {}
            door = shelter.get("needs_door") or {}
            geometry = shelter.get("geometry") or {}
            # Door recipes need six planks of ONE species. A mixed-plank
            # total can satisfy the generic board prerequisite without being
            # craftable, causing repeated inspection instead of material work.
            if (self.daily._verified(base, obs["status"].get("dimension"))
                    and shelter.get("fresh") is True and shelter.get("pos") == base.get("pos")
                    and shelter.get("dimension") == obs["status"].get("dimension")
                    and shelter.get("orientation", "north") == base.get("orientation", "north")
                    and geometry.get("floor_verified") is True and geometry.get("exit_verified") is True
                    and not shelter.get("hazards") and not shelter.get("occupied_blueprint_cells")
                    and shelter.get("missing_blocks") == 0 and door.get("reason") == "door_missing"
                    and not any(obs["counts"].get(name, 0) for name in DailySurvivalSkills.DOORS)):
                visible = {block["name"] for block in self.rules._blocks(obs)}
                def available(wood):
                    planks = obs["counts"].get(wood + "_planks", 0)
                    logs = sum(obs["counts"].get(name, 0) for name in self._wood_inputs(wood))
                    return planks >= 6, planks + logs * 4 >= 6, wood + "_log" in visible, planks
                woods = [wood for wood in WOOD_SPECIES if wood + "_door" in DailySurvivalSkills.DOORS]
                wood = max(woods, key=available)
                # _craft already uses the checked resource planner. Do not
                # normalize its species-specific gather into arbitrary logs.
                return self._craft(wood + "_door", 1, obs, context), False
        if candidate is None:
            counts = obs["counts"]
            if task == "bed" and not any(counts.get(bed, 0) for bed in DailySurvivalSkills.BEDS):
                wool = next((name for name, count in counts.items() if name.endswith("_wool") and count >= 3), None)
                candidate = self._craft(wool[:-5] + "_bed", 1, obs, context) if wool else self._hunt({"sheep"}, obs, context)
            elif task == "storage" and not counts.get("chest", 0):
                candidate = self._craft("chest", 1, obs, context)
            elif task == "farm" and any(counts.get(seed, 0) for seed in DailySurvivalSkills.SEEDS) and not any(
                    count and name.endswith("_hoe") for name, count in counts.items()):
                candidate = self._craft("stone_hoe", 1, obs, context)
        return self._resource_child(candidate, obs, context), False

    def _preparation_stance(self, obs, context, targets):
        origin = obs["status"]["pos"]
        if (not targets or obs.get("local_geometry_origin") != origin
                or self._data["explore_steps"] >= self.limits["max_explore_steps"]):
            return None
        blocked = self.rules._blocked(context)
        before = min(math.dist(origin, p) for p in targets)
        for row in self.daily._rows(obs.get("safe_stand_candidates"))[:8]:
            pos = self.rules._pos(row.get("pos"))
            if (pos is None or row.get("support") not in DailySurvivalSkills.SUPPORT
                    or not .4 < math.dist(origin, pos) <= 3
                    or abs(pos[1] - origin[1]) > .05 and not self._local_shelter_step(origin, pos, obs)
                    or self.rules._key(pos) in blocked or self.rules._key(pos) in self._data["visited"]
                    or self.rules._dangerous_hostiles(obs, pos)
                    or min(math.dist(pos, p) for p in targets) >= before - .25):
                continue
            return Decision("position_for_task", {"pos": list(pos)},
                            "Take one freshly checked closer preparation stance, then independently replan the site",
                            "shelter_preparation_position")
        return None

    def _shelter_stance(self, decision, obs, context):
        """Turn a fresh construction stance into one bounded, rechecked move."""
        shelter = obs.get("shelter") or {}
        base = (obs.get("daily_evidence") or {}).get("base_site") or {}
        status = obs["status"]
        if (not self.daily._verified(base, status.get("dimension"))
                or shelter.get("fresh") is not True
                or shelter.get("dimension") != status.get("dimension")
                or shelter.get("pos") != base.get("pos")
                or shelter.get("orientation", "north") != base.get("orientation", "north")
                or shelter.get("hazards") or shelter.get("occupied_blueprint_cells")):
            return None
        geometry = shelter.get("geometry") or {}
        if geometry.get("floor_verified") is not True or geometry.get("exit_verified") is not True:
            return None
        requested = self.rules._pos(decision.args.get("pos"))
        stands = [self.rules._pos(pos) for pos in shelter.get("stand_candidates", [])]
        door_stand = self.rules._pos((shelter.get("needs_door") or {}).get("stand_pos"))
        door_approach = self.rules._pos((shelter.get("needs_door") or {}).get("approach_pos"))
        if requested == door_stand and requested is not None:
            targets = [requested]
        elif (requested is not None and requested == door_approach and shelter.get("inside") is True
              and requested in stands):
            targets = [requested]
        elif shelter.get("needs_position") is True and requested is not None and requested in stands:
            targets = [p for p in stands if p is not None]
        else:
            return None
        origin = status["pos"]
        blocked = self.rules._blocked(context)
        def usable(pos):
            return (pos is not None and (abs(pos[1] - origin[1]) <= .05
                                        or self._local_shelter_step(origin, pos, obs))
                    and .4 < math.dist(origin, pos) <= 3
                    and self.rules._key(pos) not in blocked
                    and not self.rules._dangerous_hostiles(obs, pos))
        candidates = [p for p in targets if usable(p)]
        if not candidates and obs.get("local_geometry_origin") == origin:
            # Only one observed short approach step; no saved route or blind
            # move_to. PositioningSkills rechecks the whole body corridor.
            before = min(math.dist(origin, p) for p in targets)
            for row in self.daily._rows(obs.get("safe_stand_candidates"))[:8]:
                pos = self.rules._pos(row.get("pos"))
                if (row.get("support") in DailySurvivalSkills.SUPPORT and usable(pos)
                        and min(math.dist(pos, p) for p in targets) < before - .25):
                    candidates.append(pos)
        if not candidates:
            return None
        target = min(candidates, key=lambda p: (min(math.dist(p, t) for t in targets), math.dist(origin, p)))
        return self._decision("position_for_task", {"pos": list(target)},
                              "Take one freshly observed shelter work stance; recheck its corridor before moving")

    def next_action(self, observation, context=None):
        """Replan exactly one atomic child. None means inspect snapshot status."""
        if self._data["status"] not in {"running", "suspended"}:
            return None
        if self._unknown(context):
            self._set("unknown", "A write outcome is unresolved; no local replacement action is allowed")
            return None
        if self._data.get("pending_digest"):
            return None  # The caller must report the selected child's outcome.
        if self._generation_changed(context):
            self._escalate("life_or_dimension_changed")
            return None
        exhausted = (self._data["steps"] >= self.limits["max_steps"]
                     or time.time() - self._data["started_at"] >= self.limits["max_seconds"])
        self._data["fresh"] = self._fresh(observation)
        status = observation.get("status", {}) if isinstance(observation, dict) else {}
        if (status.get("alive") is False or status.get("dimension") not in (None, self._data["dimension"])
                or status.get("alive") is True and self._life(status) != self._data["life"]):
            self._escalate("life_or_dimension_changed")
            return None
        if not self._data["fresh"]:
            self._escalate("task_budget_exhausted") if exhausted else self.interrupt("fresh_complete_observation_required")
            return None
        task, args = self._data["task"], self._data["args"]
        self._data["progress"]["current"] = self._quantity(observation)
        if self.confirm_completion(observation, context):
            return None
        if exhausted:
            self._escalate("task_budget_exhausted")
            return None
        cautious = (isinstance(context, dict) and context.get("cautious_bootstrap") is True
                    and isinstance(context.get("survival_constraints"), dict)
                    and context["survival_constraints"].get("scenario") == "cautious_bootstrap"
                    and self.safety_policy.cautious_bootstrap(observation, context))
        if cautious and self.safety_policy.validate(
                Decision("run_survival_task", args, "Revalidate the accepted limited goal", task), observation, context):
            self._escalate("cautious_bootstrap_goal_not_allowed")
            return None
        if (status.get("busy") or status.get("gamemode") != "survival" or status.get("on_ground") is not True
                or status.get("is_day") is not True or status.get("thundering")
                or not _finite(status.get("health")) or not _finite(status.get("food"))
                or status["health"] <= self.rules.low_health and not cautious or status["food"] <= 6
                or any(status.get(k) for k in ("in_water", "in_lava", "on_fire", "submerged", "is_sleeping", "in_vehicle"))
                or any(distance <= self.rules.threat_radius for distance, _ in self.rules._hostiles(observation, status["pos"]))
                or isinstance(status.get("nearest_hostile"), dict) and _finite(status["nearest_hostile"].get("dist"))
                   and status["nearest_hostile"]["dist"] <= self.rules.threat_radius):
            self.interrupt("local_safety_policy_required")
            return None
        local = self._context(context, observation)
        local["_bounded_bootstrap"] = cautious
        obs = copy.deepcopy(observation)
        origin = obs["status"]["pos"]
        obs["blocks"] = [b for b in self.rules._blocks(obs) if math.dist(origin, b["pos"]) <= self._data["args"]["radius"]]
        obs["entities"] = [row for row in self.daily._rows(obs.get("entities"))
                           if str(row.get("id")) not in self._data["blocked_entities"]]
        self.daily.targets["food_reserve"] = args["count"] if task == "food" else self.daily.targets["food_reserve"]
        if self._data["local_failures"] >= self.limits["max_local_failures"] or self._data["no_progress"] >= self.limits["max_no_progress_steps"]:
            self._escalate("bounded_local_recovery_exhausted")
            return None
        if task == "gather":
            decision = self._gather(args["resource"], obs, local)
        elif task == "craft":
            decision = self._craft(args["item"], self._data["progress"]["target"] - self._quantity(obs), obs, local)
        elif task == "smelt":
            decision = self._smelt(args["item"], obs, local)
        elif task == "food":
            decision = self._food(obs, local)
        elif task == "recover_drops":
            names = {self.daily._drop_name(row) for row in self.daily._rows(obs.get("entities"))}
            decision = self._drop(names - {None}, obs, local)
        else:
            decision, complete = self._board_action(task, obs, local)
            if complete:
                self._data["progress"]["current"] = 1
                self._set("completed", "Fresh task evidence verifies the selected goal")
                return None
        if decision and decision.goal == "explore":
            decision = None
        if decision and decision.name == "move_to" and decision.args.get("pos") not in (
                [b["pos"] for b in self.rules._blocks(obs)] + [row.get("pos") for row in self.daily._rows(obs.get("entities"))]):
            decision = self._shelter_stance(decision, obs, local)
        if decision is None:
            decision = self._explore(obs, local)
        if decision is None:
            self._escalate("no_safe_local_action_or_unavailable_prerequisite")
            return None
        if cautious and self.safety_policy.validate(decision, observation, local):
            self._escalate("cautious_bootstrap_child_not_allowed")
            return None
        if decision.name not in local["capabilities"] or decision.name in {"gather_materials", "build_shelter_batch", "goal_complete"}:
            self._escalate("unsupported_atomic_child")
            return None
        if decision.args.get("pos") is not None and self.rules._key(decision.args["pos"]) in self.rules._blocked(local):
            self._escalate("selected_target_is_cooling_down")
            return None
        if decision.name == "move_to" and math.dist(origin, decision.args["pos"]) <= decision.args.get("range", 2) + .5:
            decision = self._explore(obs, local)
            if decision is None:
                self._escalate("no_progress_move_requires_new_strategy")
                return None
        self._data.update(current_step=decision.name, pending_digest=_digest([decision.name, decision.args]),
                          fingerprint=self._fingerprint(observation))
        if decision.name == "collect_drops":
            item = decision.args["expected_item"]
            self._data["drop_before"] = {"item": item, "count": observation["counts"].get(item, 0),
                                         "id": decision.args["entity_id"]}
        self._set("running", decision.reason)
        return decision

    def confirm_completion(self, observation, context=None):
        """Settle a known child from existing evidence without choosing another.

        This check intentionally precedes execution-budget termination: the last
        permitted child can satisfy the goal, but cannot authorize another write.
        """
        if (self._data["status"] not in {"running", "suspended"} or self._data.get("pending_digest")
                or self._unknown(context)):
            return False
        if self._generation_changed(context):
            self._escalate("life_or_dimension_changed")
            return False
        if not self._fresh(observation):
            return False
        status = observation["status"]
        if (status.get("dimension") != self._data["dimension"] or self._life(status) != self._data["life"]
                or status.get("busy") not in (None, False) or status.get("gamemode") != "survival"
                or not _finite(status.get("health")) or status["health"] <= 0):
            return False
        task = self._data["task"]
        if task in {"gather", "craft", "smelt", "food", "recover_drops"}:
            current = self._quantity(observation)
            complete = current >= self._data["progress"]["target"]
        elif task == "maintenance":
            return False
        else:
            board = self.daily.build(observation, (context or {}).get("memory"), context)
            key = TASK_ROWS.get(task, task)
            complete = any(row["id"] == key and row["status"] == "satisfied" for row in board["tasks"])
            current = int(complete)
        if not complete:
            return False
        self._data["progress"]["current"] = current
        self._data["fresh"] = True
        self._set("completed", "Fresh current evidence verifies the selected goal")
        return True

    def record_result(self, decision, result, observation=None, context=None):
        """Consume a real child result; acknowledgements never complete a goal."""
        if (not isinstance(decision, Decision) or self._data.get("pending_digest") != _digest([decision.name, decision.args])):
            raise ValueError("Result does not match the outstanding child")
        data = result.data if isinstance(result.data, dict) else {}
        self._data["steps"] += 1
        self._data["pending_digest"] = None
        self._data["current_step"] = None
        code = result.error_code
        recent = {"name": decision.name, "success": result.success is True, "error_code": str(code)[:80] if code else None,
                  "executed": data.get("executed") is True,
                  "target": self.rules._pos(data.get("target")) or self.rules._pos(decision.args.get("pos"))}
        self._data["recent_results"] = (self._data["recent_results"] + [recent])[-8:]
        if code in UNKNOWN or data.get("action_state") == "unknown":
            self._set("unknown", "Child outcome is unknown; preserve the journal and do not replay")
            return
        if self._generation_changed(context):
            self._escalate("life_or_dimension_changed")
            return
        if code in {"dead", "lifecycle_changed", "life_changed", "dimension_changed"}:
            self._escalate("life_or_dimension_changed")
            return
        intent = self._data["preparation_site"]
        preparation_rejected = (intent is not None and decision.name == "plan_shelter_preparation"
            and result.success is False and code == "unsafe_site" and data.get("executed") is False
            and data.get("fresh") is True and data.get("preparation_rejected") is True
            and data.get("dimension") == self._data["dimension"] and data.get("pos") == intent["pos"]
            and data.get("orientation") == intent["orientation"])
        if preparation_rejected:
            self._data["survey_steps"] += 1
            self._data["survey_since_move"] += 1
        preparation_evidence = (intent is not None and data.get("fresh") is True
            and data.get("dimension") == self._data["dimension"] and data.get("pos") == intent["pos"]
            and data.get("orientation") == intent["orientation"]
            and data.get("preparation_plan_verified") is True
            and isinstance(data.get("clear_cells"), list) and len(data["clear_cells"]) <= 8
            and all(self.rules._pos(p) is not None for p in data["clear_cells"])
            and type(data.get("clear_remaining")) is int and 0 <= data["clear_remaining"] <= 8)
        if decision.name in {"plan_shelter_preparation", "prepare_shelter_step"} and intent is not None:
            key = self.rules._key(intent["pos"])
            if decision.name == "plan_shelter_preparation":
                attempts = self._data["preparation_attempts"]
                attempts[key] = min(8, attempts.get(key, 0) + 1)
                self._data["preparation_attempts"] = dict(list(attempts.items())[-32:])
                intent["plan_orientations"] = list(dict.fromkeys(
                    [*intent.get("plan_orientations", []), intent["orientation"]]))
            if (decision.name == "plan_shelter_preparation" and result.success is True
                    and data.get("executed") is False and preparation_evidence):
                intent.update(stage="prepare" if data["clear_remaining"] else "survey",
                              clear_cells=copy.deepcopy(data["clear_cells"]))
            elif (decision.name == "prepare_shelter_step" and result.success is True and preparation_evidence
                    and data.get("executed") is True and data.get("verified") is True
                    and data.get("block_removed") is True and self.rules._pos(data.get("cleared_pos")) is not None
                    and data.get("cleared_name") in {"dirt", "grass_block"}):
                # Only a verified actual terrain change invalidates the old
                # rejection. It still supplies no base/construction authority.
                prefix = key + "|"
                self._data["rejected_sites"] = [p for p in self._data["rejected_sites"] if not p.startswith(prefix)]
                self._shelter_search["rejected"] = {p: row for p, row in self._shelter_search["rejected"].items()
                                                       if not p.startswith(prefix)}
                intent.update(stage="survey", clear_cells=[])
            elif (decision.name == "prepare_shelter_step" and result.success is True and preparation_evidence
                    and data.get("executed") is False and data["clear_remaining"] == 0):
                intent.update(stage="survey", clear_cells=[])
            elif code == "needs_position" and data.get("executed") is False and preparation_evidence:
                intent.update(stage="position", clear_cells=copy.deepcopy(data["clear_cells"]))
            elif preparation_rejected:
                remaining = [o for o in ("north", "east", "south", "west") if o not in intent["plan_orientations"]]
                if remaining:
                    intent.update(stage="plan", orientation=remaining[0], clear_cells=[])
                else:
                    self._data["preparation_site"] = None
            else:
                self._data["preparation_site"] = None
        elif decision.goal == "shelter_preparation_position" and intent is not None:
            if result.success is True and data.get("verified") is True:
                intent["stage"] = "plan"
            else:
                self._data["preparation_site"] = None
        elif decision.name == "plan_shelter" and result.success is True:
            self._data["preparation_site"] = None
        no_motion = decision.name == "move_to" and (data.get("already_in_range") is True or data.get("executed") is False)
        survey_key = self.rules._key(decision.args.get("pos")) + "|" + decision.args.get("orientation", "north") if decision.name == "plan_shelter" else None
        survey_rejected = (decision.name == "plan_shelter" and result.success is False
            and code in {"unsafe_site", "site_occupied"} and data.get("executed") is False
            and data.get("fresh") is True and data.get("dimension") == self._data["dimension"]
            and data.get("pos") == decision.args.get("pos")
            and data.get("orientation") == decision.args.get("orientation", "north")
            and isinstance(data.get("geometry"), dict)
            and all(type(data["geometry"].get(key)) is bool for key in ("floor_verified", "exit_verified"))
            and isinstance(data.get("occupied_blueprint_cells"), list) and isinstance(data.get("hazards"), list)
            and survey_key not in self._data["rejected_sites"])
        if survey_rejected:
            self._data["survey_steps"] += 1
            self._data["survey_since_move"] += 1
            occupied = data.get("occupied_blueprint_cells") or []
            orientations = (("north", "east", "south", "west")
                if data["geometry"].get("floor_verified") is False or len(occupied) > 2
                else (decision.args.get("orientation", "north"),))
            # Conservative exclusion of a shared square footprint, not a
            # claim that any uninspected orientation has been validated.
            for orientation in orientations:
                key = self.rules._key(decision.args["pos"]) + "|" + orientation
                if key not in self._data["rejected_sites"]:
                    self._data["rejected_sites"].append(key)
                self._remember_shelter_search("rejected", key, pos=list(decision.args["pos"]),
                                              orientation=orientation, error_code=code)
            anchor_key = self.rules._key(decision.args["pos"])
            # A bounded list in a rejected survey is only a reason to request
            # the independent complete read plan, never permission to dig.
            if (self._data["task"] == "shelter" and data["geometry"]["floor_verified"] is True
                    and data["hazards"] == []
                    and (1 <= len(occupied) <= 8 or code == "site_occupied" and not occupied)
                    and self._data["preparation_attempts"].get(anchor_key, 0) < 8):
                self._data["preparation_site"] = {"pos": list(decision.args["pos"]),
                    "orientation": decision.args.get("orientation", "north"), "stage": "plan", "clear_cells": [],
                    "plan_orientations": []}
            elif self._data["preparation_site"] and self._data["preparation_site"]["pos"] == decision.args["pos"]:
                self._data["preparation_site"] = None
        negative_read = survey_rejected or preparation_rejected
        if result.success is not True or no_motion:
            self._data["local_failures"] += int(not negative_read)
            self._data["local_recoveries"] += 1
            target = self.rules._pos(data.get("target")) or self.rules._pos(decision.args.get("pos"))
            stance_request = (decision.name in {"build_shelter_step", "build_shelter_batch"}
                              and code == "needs_position" and data.get("executed") is False)
            stance_request |= (decision.name == "prepare_shelter_step" and code == "needs_position"
                               and data.get("executed") is False and preparation_evidence)
            if target and not stance_request and not negative_read:
                self._data["blocked"][self.rules._key(target)] = time.time() + 30
                self._data["blocked"] = dict(list(self._data["blocked"].items())[-16:])
                self._data["blocked_codes"][self.rules._key(target)] = code or "no_movement"
                self._data["blocked_codes"] = {k: v for k, v in self._data["blocked_codes"].items() if k in self._data["blocked"]}
            if type(decision.args.get("entity_id")) is int:
                self._data["blocked_entities"][str(decision.args["entity_id"])] = time.time() + 30
                self._data["blocked_entities"] = dict(list(self._data["blocked_entities"].items())[-16:])
        if decision.goal in {"explore", "shelter_preparation_position"}:
            self._data["explore_steps"] += 1
            target = self.rules._pos(decision.args.get("pos"))
            if target:
                self._data["visited"] = (self._data["visited"] + [self.rules._key(target)])[-16:]
        if result.success and decision.name == "smelt_start" and data.get("verified") is True:
            output = DailySurvivalSkills.SMELTABLE.get(decision.args.get("input_item"))
            if output:
                self._data["jobs"] = (self._data["jobs"] + [{"pos": decision.args["pos"],
                    "dimension": self._data["dimension"], "expected_output": output, "stage": "started"}])[-8:]
        if result.success and decision.name == "smelt_collect" and data.get("verified") is True:
            self._data["jobs"] = [job for job in self._data["jobs"] if job["pos"] != decision.args["pos"]]
        if self._fresh(observation):
            status = observation["status"]
            if status.get("dimension") != self._data["dimension"] or self._life(status) != self._data["life"]:
                self._escalate("life_or_dimension_changed")
                return
            if (self._data["task"] == "shelter" and decision.goal in {"explore", "shelter_preparation_position"}
                    and decision.name == "position_for_task" and result.success is True
                    and data.get("verified") is True
                    and math.dist(status["pos"], decision.args["pos"]) <= .4):
                self._remember_shelter_search("visited", self.rules._key(status["pos"]),
                                              pos=list(status["pos"]))
            current = self._fingerprint(observation)
            progress = current != self._data.get("fingerprint")
            processing = decision.name == "wait" and any(self.daily._verified(row, self._data["dimension"])
                and (self.daily._burning(row) or (row.get("input") or {}).get("count", 0))
                for row in self.daily._rows(observation.get("furnaces")))
            self._data["no_progress"] = 0 if progress else self._data["no_progress"] + (0 if processing or negative_read else 1)
            if progress:
                if not negative_read:
                    self._data["local_failures"] = 0
                if decision.name == "position_for_task" and result.success is True:
                    self._data["survey_since_move"] = 0
            self._data["fingerprint"] = current
            drop = self._data.get("drop_before")
            if (self._data["task"] == "recover_drops" and decision.name == "collect_drops" and result.success
                    and data.get("verified") is True and drop and drop["id"] in self._data["observed_drop_ids"]
                    and observation["counts"].get(drop["item"], 0) > drop["count"]
                    and drop["id"] not in self._data["recovered_drop_ids"]):
                self._data["recovered_drop_ids"].append(drop["id"])
            self._data["progress"]["current"] = self._quantity(observation)
            self._data["fresh"] = True
        else:
            self._data["fresh"] = False
            self._data["no_progress"] += 1
        self._data["drop_before"] = None
        if code == "cancelled":
            self.interrupt("cancelled_before_known_write")
        else:
            self._set("running", "Reobserve and replan the next child")

    def interrupt(self, reason):
        """Retain the goal while the caller runs safety or handles manual pause."""
        if self._data["status"] not in {"running", "suspended"}:
            return
        if self._data["status"] == "running":
            self._data["emergency_interruptions"] += 1
        self._data["fresh"] = False
        self._set("suspended", reason)

    def snapshot(self):
        """Compact public progress; no baseline, lifecycle token or queued action."""
        keys = ("task_id", "task", "status", "progress", "current_step", "local_recoveries",
                "emergency_interruptions", "ai_escalations", "last_escalation_reason", "reason", "fresh",
                "updated_at", "steps", "max_steps", "recent_results", "args")
        result = {key: copy.deepcopy(self._data[key]) for key in keys}
        if self._data["task"] == "shelter":
            result["shelter_search"] = self._shelter_search_report()
        return result

    def stop(self, reason, *, unknown=False, cancelled=False):
        """Freeze a goal after an external exception, unknown safety write or stop."""
        if self._data["status"] == "unknown" and not unknown:
            return  # Cancellation is not evidence that the write never happened.
        self._data["fresh"] = False
        if unknown:
            self._set("unknown", reason)
        elif cancelled:
            self._data.update(pending_digest=None, current_step=None, drop_before=None)
            self._set("cancelled", reason)
        else:
            self._escalate(reason)

    def abandon_after_reobserve(self, observation):
        """Explicit caller-authorized recovery discards the goal, never the evidence.

        The harness may call this only after a manual resume and a fresh idle
        observation. It must separately reconcile its game-action journal.
        """
        if (not self._fresh(observation) or observation["status"].get("busy") not in (None, False)
                or not (self._data["status"] == "unknown" or self._data.get("pending_digest"))):
            return False
        self._data.update(pending_digest=None, current_step=None, drop_before=None, fresh=True)
        self._set("cancelled", "Explicit fresh reobservation abandoned the old goal; unknown evidence is retained")
        return True

    def state(self):
        """Serializable bounded evidence checkpoint; pending hashes cannot execute."""
        self._prune_shelter_search()
        return {**copy.deepcopy(self._data),
                "shelter_search_cache": {"version": 1, **copy.deepcopy(self._shelter_search)}}
