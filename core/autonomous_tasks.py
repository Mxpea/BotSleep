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
from core.mining_targets import estimate_empty_main_slots
from core.production_recipes import CRAFT_ITEMS, RECIPES, ingredient_items, plan_craft, wood_inputs
from core.skill_registry import STARTER_MINING_SKILLS, internal_skills_for_objective
from core.route_foliage_recovery import choose_route_foliage, cleared_route_targets, confirmed_leaf_removal, position_foliage_hint
from core.shelter_project import active_project, same_site
from core.shelter_approach import avoids_shell, entrance_geometry, entrance_waypoint
from core.shelter_route_recovery import changed_shelter_routes, shelter_route_refusal
from core.shelter_return import MAX_RETURN_STEPS, choose_shelter_return
from core.starter_mining_task import StarterMiningTask, rung_depth
from core.survival_mainline import mainline_enabled
from core.structure_spec import SHELTER_5X5_SPEC, SHELTER_BUILDING_BLOCKS
from core.policy_relaxation import LOW_HEALTH, NIGHT, allows_goal
from skills.daily_survival import DailySurvivalSkills
from skills.equipment import ARMOR_SLOTS, SLOT_SUFFIX, armor_rank, read_worn_slot_item
from skills.hunting import HUNTING_TOOLS
from skills.positioning import PositioningSkills


TASKS = frozenset({"gather", "craft", "smelt", "food", "starter_tools", "iron_tools", "iron_armor", "scout",
                  "nether_portal", "shelter", "bed", "storage", "farm", "recover_drops", "maintenance"})
RESOURCES = frozenset({"logs", "cobblestone", "coal", "raw_iron", "diamond"})
SMELT_ITEMS = frozenset(DailySurvivalSkills.SMELTABLE.values())
STATES = frozenset({"idle", "running", "suspended", "completed", "needs_ai", "unknown", "cancelled"})
UNKNOWN = frozenset({"request_timeout", "network_error", "invalid_json", "invalid_response",
                     "dig_unconfirmed", "action_unconfirmed", "internal", "internal_error"})
PATH_FAILURES = frozenset({"stalled", "no_path", "path_not_found", "out_of_reach", "out_of_range",
                           "move_unverified", "target_occluded", "visibility_unknown", "unsafe_target"})
GATHER_NAVIGATION_FAILURES = frozenset({"stalled", "no_path", "path_not_found", "move_unverified"})
TASK_ROWS = {"food": "food_reserve", "farm": "food_production", "maintenance": "maintain"}
RESOURCE_NAMES = {"logs": frozenset(LOG_NAMES), "sand": frozenset({"sand"}),
                  "cobblestone": frozenset({"stone", "cobblestone"}),
                  "coal": frozenset({"coal_ore", "deepslate_coal_ore"}),
                  "raw_iron": DailySurvivalSkills.IRON,
                  "diamond": frozenset({"diamond_ore", "deepslate_diamond_ore"})}


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
        self._mine = StarterMiningTask()
        self._before_child = None
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
                      "shelter_return_steps": 0, "shelter_return_visited": [],
                      "preparation_site": None, "preparation_attempts": {},
                      "deep_mine_ores": [], "pending_deep_mine": None,
                      "scout_completed": False,
                      "portal_built": False, "portal_attempted": False,
                      "pending_digest": None, "baseline": 0, "sequence": 0, "life_generation": 0}
        if saved is not None:
            self._restore(saved)
            self._mine = StarterMiningTask(saved.get("starter_mine"))
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
            if not isinstance(args.get("resource"), str) or args["resource"] not in RESOURCES | {"sand", *LOG_NAMES} or "item" in args:
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
                    "survey_steps", "survey_since_move", "shelter_return_steps"):
            if type(self._data[key]) is not int or self._data[key] < 0:
                raise ValueError("Saved counters are invalid")
        for key, maximum in (("recent_results", 8), ("visited", 16), ("jobs", 8),
                             ("observed_drop_ids", 40), ("recovered_drop_ids", 40), ("rejected_sites", 128),
                             ("shelter_return_visited", 64), ("deep_mine_ores", 8)):
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
        self._mine = replacement._mine
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
                                 if args["task"] == "food" else 4
                                 if args["task"] == "iron_armor" else max(1, len(self._data["observed_drop_ids"]))
                                 if args["task"] == "recover_drops" else 1, "unit": self._unit()}
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
        return args.get("resource", args.get("item", "food_items" if args["task"] == "food"
                        else "pieces" if args["task"] == "iron_armor"
                        else "portal" if args["task"] == "nether_portal"
                        else "scan" if args["task"] == "scout" else "goal"))

    @staticmethod
    def _iron_armor_count(observation):
        counts = observation.get("counts") or {}
        worn_count = 0
        for slot in ARMOR_SLOTS:
            worn = read_worn_slot_item(observation, slot)
            suffix = SLOT_SUFFIX[slot]
            min_rank = armor_rank(f"iron_{suffix}", slot)
            worn_rank = armor_rank((worn or {}).get("name"), slot)
            if worn_rank >= min_rank or counts.get(f"iron_{suffix}", 0) > 0:
                worn_count += 1
        return worn_count

    def _quantity(self, observation):
        counts = observation["counts"]
        args = self._data["args"]
        if args["task"] == "gather":
            return sum(counts.get(n, 0) for n in LOG_NAMES) if args["resource"] == "logs" else counts.get(args["resource"], 0)
        if args["task"] == "nether_portal":
            return 1 if (self._data.get("portal_built") or any(
                b.get("name") == "nether_portal" for b in (observation.get("blocks") or []) if isinstance(b, dict)
            )) else 0
        if args["task"] in {"craft", "smelt"}:
            return counts.get(args["item"], 0)
        if args["task"] == "food":
            return sum(counts.get(n, 0) for n in SAFE_FOOD)
        if args["task"] == "iron_armor":
            return self._iron_armor_count(observation)
        if args["task"] == "scout":
            return int(bool(self._data.get("scout_completed")))
        if args["task"] == "recover_drops":
            return len(self._data["recovered_drop_ids"])
        return 0

    def _fingerprint(self, observation):
        status = observation["status"]
        rows = self.rules._blocks(observation)
        return _digest({"counts": observation["counts"], "cell": [math.floor(v) for v in status["pos"]],
            "blocks": sorted((tuple(row["pos"]), row["name"]) for row in rows)[:256],
            "facilities": observation.get("daily_evidence", {}), "furnaces": observation.get("furnaces", []),
            "armor": observation.get("armor", {}), "offhand": status.get("offhand"),
            "scout_completed": bool(self._data.get("scout_completed"))})

    def _child_metric(self, decision, observation):
        """Measure only the output or destination of a resource, craft or move."""
        name, args = decision.name, decision.args
        counts = observation["counts"]
        if name in {"craft_item", "take_items", "smelt_collect", "collect_drops", "store_items"}:
            item = args["expected_item"] if name == "collect_drops" else args["item"]
            direction = -1 if name == "store_items" else 1
            return direction * counts.get(item, 0)
        if name in {"gather_block", "gather_resource"}:
            block = args["expected_name"]
            if block == "dead_bush":
                return counts.get("stick", 0)
            for material, names in RESOURCE_NAMES.items():
                if block in names:
                    return counts.get(block if material == "logs" else material, 0)
        if name in {"move_to", "position_for_task"} and decision.goal != "scout_surface":
            # Positive change means the destination got closer. Quantization
            # avoids interpreting rounded position jitter as movement.
            return -round(math.dist(observation["status"]["pos"], args["pos"]), 1)
        return None

    def _context(self, context, observation=None):
        result = copy.deepcopy(context or {})
        now = time.time()
        if self._fresh(observation):
            self._bind_shelter_search(observation["status"], self._data["life_generation"])
        self._data["blocked"] = {k: v for k, v in self._data["blocked"].items() if _finite(v) and v > now}
        changed_routes = set()
        if (self._data["task"] == "shelter" and not self._unknown(context)
                and not self._generation_changed(context) and self._fresh(observation)):
            for target in changed_shelter_routes(self._data["recent_results"], observation):
                key = self.rules._key(target)
                if self._data["blocked_codes"].get(key) == "unsafe_route":
                    changed_routes.add(key)
        if observation is not None:
            for row in self.rules._blocks(observation):
                key = self.rules._key(row["pos"])
                if (row.get("can_see") is True and self._data["blocked_codes"].get(key) in {"target_occluded", "visibility_unknown"}):
                    self._data["blocked"].pop(key, None)
                if (row["name"] == "crafting_table" and self._data["blocked_codes"].get(key) == "move_unverified"
                        and self._fresh(observation) and observation["status"].get("on_ground") is True
                        and math.dist(row["pos"], observation["status"]["pos"]) <= 3.25):
                    # A narrower navigation radius failed, but fresh footing is now
                    # within the interaction range. The craft skill still verifies output.
                    self._data["blocked"].pop(key, None)
        self._data["blocked_codes"] = {k: v for k, v in self._data["blocked_codes"].items() if k in self._data["blocked"]}
        self._data["blocked_entities"] = {k: v for k, v in self._data["blocked_entities"].items() if _finite(v) and v > now}
        # Keep the original refused sweep cooling down. Only its projection
        # onto a different fresh origin changes; external risk blocks still win.
        result["blocked_targets"] = sorted(self.rules._blocked(result) | (set(self._data["blocked"]) - changed_routes))
        result["capabilities"] = list(set(result.get("capabilities", DAILY_SKILLS)) - {"gather_materials", "build_shelter_batch"})
        if self._data["task"] == "shelter":
            result["capabilities"] = sorted(set(result["capabilities"]) | (internal_skills_for_objective("daily") - STARTER_MINING_SKILLS))
        if self._data["task"] in {"gather", "starter_tools", "craft", "smelt", "iron_tools"} and self.settings.get("objective", "daily") == "daily":
            result["capabilities"] = sorted(set(result["capabilities"]) | STARTER_MINING_SKILLS)
        result["smelting_jobs"] = (self.daily._rows(result.get("smelting_jobs")) + copy.deepcopy(self._data["jobs"]))[-8:]
        result["explore_index"] = self._data["explore_steps"]
        self._prune_shelter_search()
        result["shelter_rejected_sites"] = list(dict.fromkeys(
            [*self._shelter_search["rejected"], *self._data["rejected_sites"]]))
        result["shelter_visited_stances"] = list(self._shelter_search["visited"])
        result["shelter_survey_reposition"] = self._data["survey_since_move"] >= (2 if self.settings.get("campaign") is True else 4)
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

    def _withdraw(self, names, needed, obs, context):
        if (context.get("_bounded_bootstrap") or needed <= 0
                or "take_items" not in context.get("capabilities", DAILY_SKILLS)):
            return None
        counts = obs["counts"]
        target = sum(counts.get(name, 0) for name in names) + needed
        return self._row_decision(self.daily._take_supplies(
            obs, counts, names, target, self._data["task"], context, radius=self._data["args"]["radius"]))

    def _stash_bulk_if_crowded(self, obs, counts, context):
        if self._data.get("stash_done"):
            return None
        inv = obs.get("inventory") or []
        if len(inv) < 26 or (estimate_empty_main_slots(inv) > 10 and counts.get("chest", 0) == 0):
            return None
        bulk_keep = (
            ("diorite", 0), ("granite", 0), ("andesite", 0), ("cobbled_deepslate", 0),
            ("tuff", 0), ("dirt", 0), ("gravel", 0), ("flint", 0), ("leaf_litter", 0),
            ("red_mushroom", 0), ("raw_copper", 0), ("cobblestone", 64),
        )
        if not any(counts.get(name, 0) > keep for name, keep in bulk_keep):
            return None
        chests = self.daily._safe_rows(obs, {"chest", "barrel"}, context=context)
        if not chests:
            return None
        chest_pos = chests[0]["pos"]
        for name, keep in bulk_keep:
            excess = counts.get(name, 0) - keep
            if excess > 0:
                return self._decision(
                    "store_items",
                    {"pos": list(chest_pos), "item": name, "count": min(64, int(excess))},
                    "Free inventory slots in nearby storage before deep mining",
                )
        return None

    def _deep_mine_decision(self, resource, obs, count=4):
        origin = obs["status"]["pos"]
        self._data["pending_deep_mine"] = {"resource": resource, "count": max(1, min(32, int(count)))}
        return Decision(
            "gather_resource",
            {"pos": [math.floor(origin[0]), math.floor(origin[1]), math.floor(origin[2])], "expected_name": "iron_ore"},
            f"Launch bounded surface-to-deep mining expedition for {resource} and return safely to surface",
            "deep_mine",
        )

    def _gather_dead_bush(self, obs, context):
        for row in self.daily._safe_rows(obs, {"dead_bush"}, context=context):
            if math.dist(obs["status"]["pos"], row["pos"]) <= self._data["args"]["radius"]:
                return self._decision("gather_block", {"pos": list(row["pos"]), "expected_name": "dead_bush"},
                                      "Harvest dead_bush in desert for sticks without requiring wood planks")
        return None

    def _equip_loadout_decision(self, obs, *, item_hint="iron_chestplate", slot_hint="torso",
                                prefer_offhand="shield", reason="Equip crafted armor and shield"):
        return Decision(
            "equip_armor",
            {"item": item_hint, "slot": slot_hint, "prefer_offhand": prefer_offhand},
            reason,
            "equip_loadout",
        )

    def _scout(self, obs, context):
        if self._data.get("scout_completed"):
            return None
        origin = obs["status"]["pos"]
        return Decision(
            "move_to",
            {"pos": list(origin), "range": 4, "scan_radius": self._data["args"].get("radius", 32)},
            "Scout forward ~200 blocks for structures, wood, or surface caves",
            "scout_surface",
        )

    def _iron_armor(self, obs, context):
        counts = obs["counts"]
        missing = []
        for slot in ("torso", "legs", "head", "feet"):
            piece = "iron_" + SLOT_SUFFIX[slot]
            worn = read_worn_slot_item(obs, slot)
            if armor_rank((worn or {}).get("name"), slot) >= armor_rank(piece, slot):
                continue
            if counts.get(piece, 0) > 0:
                return self._equip_loadout_decision(obs, item_hint=piece, slot_hint=slot,
                                                    reason=f"Equip crafted {piece} and best available gear")
            missing.append(piece)
        return self._craft(missing[0], 1, obs, context) if missing else None

    def _nether_portal(self, obs, context):
        counts = obs["counts"]
        origin = obs["status"]["pos"]
        if any(b.get("name") == "nether_portal" for b in (obs.get("blocks") or []) if isinstance(b, dict)):
            self._data["portal_built"] = True
            return None
        if counts.get("water_bucket", 0) < 1:
            if counts.get("bucket", 0) < 1:
                return self._craft("bucket", 1, obs, context)
            waters = [row for row in self.daily._safe_rows(obs, {"water"}, context=context)]
            waters.sort(key=lambda r: math.dist(origin, r["pos"]))
            if waters:
                target_w = waters[0]
                if math.dist(origin, target_w["pos"]) <= 4.5:
                    return self._decision("collect_liquid", {"pos": list(target_w["pos"]), "expected_name": "water"},
                                          "Fill bucket with water for Nether Portal speedrun casting")
                return Decision("move_to", {"pos": list(target_w["pos"]), "range": 3},
                                "Move within reach of water source to fill bucket", "nether_portal_water")
            return None
        if counts.get("flint_and_steel", 0) < 1:
            if counts.get("flint", 0) < 1:
                gravels = [row for row in self.daily._safe_rows(obs, {"gravel"}, context=context) if row.get("can_dig") is not False]
                gravels.sort(key=lambda r: math.dist(origin, r["pos"]))
                if gravels:
                    return self._decision("gather_block", {"pos": list(gravels[0]["pos"]), "expected_name": "gravel"},
                                          "Dig gravel for flint to craft flint_and_steel")
                if counts.get("gravel", 0) >= 1:
                    for cand in self.daily._rows(obs.get("placement_candidates"))[:6]:
                        cpos = self.rules._pos(cand.get("pos"))
                        if cpos is not None and 1.0 <= math.dist(origin, cpos) <= 4.0:
                            step = self._decision("place_item", {"item": "gravel", "pos": list(cpos)},
                                                  "Place carried gravel so it can be mined for flint")
                            if self.safety_policy.validate(step, obs, context) is None:
                                return step
            return self._craft("flint_and_steel", 1, obs, context)
        scaffold_total = sum(counts.get(name, 0) for name in ("cobblestone", "stone", "cobbled_deepslate", "dirt"))
        if scaffold_total < 10:
            return self._gather("cobblestone", obs, context)
        if not any(counts.get(t + "_pickaxe", 0) for t in ("wooden", "stone", "iron", "diamond", "netherite")):
            return self._craft("stone_pickaxe", 1, obs, context)
        if not self._data.get("portal_attempted"):
            return Decision("move_to", {"pos": list(origin), "range": 4},
                            "Locate lava pool or obsidian site and build/ignite Nether Portal", "nether_portal")
        return None

    def _gather(self, resource, obs, context, names=None, *, target_count=None):
        counts = obs["counts"]
        if resource in LOG_NAMES:
            wanted = self._data["progress"]["target"]
            return self._gather("logs", obs, context, {resource}, target_count=wanted)
        cautious = context.get("_bounded_bootstrap") is True
        if cautious and resource != "logs":
            return None
        origin = obs["status"]["pos"]
        failures = self._gather_navigation_hints(obs)
        needs_stance = any("blocker" not in row and tuple(map(math.floor, row["origin"])) == tuple(map(math.floor, origin))
                           and math.dist(row["origin"], origin) <= .5 for row in failures)
        items = set(names or LOG_NAMES) if resource == "logs" else {resource}
        if not cautious and not needs_stance:
            drop = self._drop(items, obs, context)
            if drop is not None:
                return drop
            needed = 1 if target_count is None else target_count - sum(counts.get(name, 0) for name in items)
            if self._data["task"] == "gather" and self._data["args"].get("resource") == resource:
                needed = self._data["progress"]["target"] - self._quantity(obs)
            withdrawal = self._withdraw(items, needed, obs, context)
            if withdrawal is not None:
                return withdrawal
        if resource not in {"logs", "sand"}:
            if resource == "diamond":
                required = "iron_pickaxe"
                minimum = ("iron", "diamond", "netherite")
            elif resource == "raw_iron":
                required = "stone_pickaxe"
                minimum = ("stone", "iron", "diamond", "netherite")
            else:
                required = "wooden_pickaxe"
                minimum = ("wooden", "stone", "iron", "diamond", "netherite")
            if not any(counts.get(tier + "_pickaxe", 0) for tier in minimum):
                return self._craft(required, 1, obs, context)
        if (resource == "cobblestone" and "plan_starter_staircase" in context["capabilities"]
                and obs.get("local_geometry_origin") == origin
                and (self.safety_policy._footing(obs) or {}).get("name") == "air"):
            blocked = self.rules._blocked(context)
            for row in self.daily._rows(obs.get("safe_stand_candidates"))[:8]:
                target = self.rules._pos(row.get("pos"))
                if (target is not None and row.get("support") in PositioningSkills.EDGE_GROUND
                        and self.rules._key(target) not in blocked
                        and PositioningSkills.contacts_adjacent_centre(origin, target)):
                    return Decision("position_for_task", {"pos": list(target)},
                                    "Centre on the natural ground already touching the player before mining", "starter_mining")
            return None
        blocks = self.rules._blocks(obs)
        blocked, hostiles = self.rules._blocked(context), self.rules._hostiles(obs, origin)
        blocked_occlusion = {key for key, code in self._data["blocked_codes"].items()
                             if code in {"target_occluded", "visibility_unknown"}}
        blocked_columns = {tuple(row["target"][i] for i in (0, 2)) for row in failures
                           if row["expected_name"] in LOG_NAMES and "blocker" not in row}
        route_failures = {"stalled", "no_path", "path_not_found", "out_of_reach", "out_of_range", "move_unverified", "timeout"}
        blocked_log_xz = set()
        if resource == "logs" or resource in LOG_NAMES:
            for bkey in blocked:
                if self._data["blocked_codes"].get(bkey) in route_failures:
                    parts = bkey.split(",")
                    if len(parts) == 3:
                        try:
                            blocked_log_xz.add((int(parts[0]), int(parts[2])))
                        except ValueError:
                            pass
        blocked_columns.update(blocked_log_xz)
        occluded_columns = {tuple(row["target"][i] for i in (0, 2)) for row in failures if "blocker" in row}
        ground = obs.get("ground_log_approach")
        if (resource == "logs" and not cautious and (mainline_enabled(self.settings) or self.settings.get("campaign") is True)
                and isinstance(ground, dict) and ground.get("verified") is True
                and ground.get("origin") == obs.get("local_geometry_origin") == origin
                and ground.get("ground") in PositioningSkills.EDGE_GROUND
                and (ground.get("visibility") or {}).get("visible") is True
                and ground.get("name") in (names or LOG_NAMES)):
            for row in blocks:
                if (row["name"] not in (names or LOG_NAMES) or row.get("can_see") is not False
                        or math.dist(origin, row["pos"]) > 4 or not self.rules._safe_target(
                            row, origin, blocks, blocked - blocked_occlusion, hostiles, observation=obs)):
                    continue
                prior = {"name": "gather_block", "args": {"pos": row["pos"], "expected_name": row["name"]},
                         "goal": self._data["task"]}
                clear = self.daily._foliage_hint(prior, obs, context, context["capabilities"])
                if clear["name"] == "clear_foliage":
                    return self._row_decision(clear)
            target = self.rules._pos(ground.get("target"))
            log = next((row for row in blocks if row["name"] == ground["name"] and row["pos"] == target), None)
            step = self.rules._pos(ground.get("next"))
            if (log is not None and self.rules._key(target) not in blocked and (target[0], target[2]) not in blocked_columns
                    and self.rules._safe_target(log, origin, blocks, blocked, hostiles, observation=obs)):
                if (step is None and ground.get("steps") == 0 and math.dist(origin, target) <= 4):
                    return self._decision("gather_block", {"pos": target, "expected_name": log["name"]},
                                          "Harvest the rooted trunk from the verified ground stance without approaching")
                if (step is not None and (target[0], target[2]) not in occluded_columns and self.rules._key(step) not in blocked
                        and step[1] == math.floor(origin[1]) and sum(abs(math.floor(step[i]) - math.floor(origin[i])) for i in (0, 2)) == 1
                        and not self.rules._dangerous_hostiles(obs, step)
                        and any(row.get("pos") == step and row.get("support") in PositioningSkills.EDGE_GROUND
                                for row in self.daily._rows(obs.get("safe_stand_candidates")))):
                    return self._decision("position_for_task", {"pos": step},
                                          "Move one level ground cell toward the checked low trunk, then reobserve")
        mineable = RESOURCE_NAMES[resource] if names is None else RESOURCE_NAMES[resource].intersection(names)
        candidates = [row for row in blocks if row["name"] in mineable
                      and math.dist(origin, row["pos"]) <= self._data["args"]["radius"]
                      and (resource != "cobblestone" or "plan_starter_staircase" not in context["capabilities"]
                           or obs.get("local_geometry_origin") != origin or row.get("can_see") is True
                           or math.dist(origin, row["pos"]) <= 4)
                      and (not needs_stance or math.dist(origin, row["pos"]) <= 4
                           and row.get("can_see") is True and row.get("can_dig") is True)
                      and (row["name"] not in LOG_NAMES or tuple(row["pos"][i] for i in (0, 2)) not in blocked_columns)
                      and (resource != "logs" or abs(float(row["pos"][1]) - float(origin[1])) <= 3.1)
                      and (not cautious or row.get("can_see") is True and row.get("can_dig") is True
                           and math.dist(origin, row["pos"]) <= 3)
                      and self.rules._safe_target(row, origin, blocks, blocked - blocked_occlusion, hostiles, observation=obs)]
        candidates.sort(key=lambda row: (
            0 if (resource == "logs" and math.dist(origin, row["pos"]) <= 4
                  and abs(row["pos"][1] - origin[1]) <= 2.1 and row.get("can_see") is True) else 1,
            0 if row.get("can_see") is True else 2 if row.get("can_see") is False else 1,
            math.dist(origin, row["pos"]),
            round(abs(float(row["pos"][1]) - float(origin[1])), 1) if resource == "logs" else 0.0,
            row["pos"],
        ))
        for row in candidates:
            action = {"name": "gather_resource" if resource in {"raw_iron", "diamond"} else "gather_block",
                      "args": {"pos": list(row["pos"]), "expected_name": row["name"]},
                      "reason": "Gather one fresh resource for the selected goal", "goal": self._data["task"]}
            alternative = self.daily._foliage_hint(action, obs, context, context["capabilities"])
            if alternative["name"] == "clear_foliage":
                return self._row_decision(alternative)
            if (row["name"] in LOG_NAMES and (row["pos"][0], row["pos"][2]) in occluded_columns
                    and not (math.dist(origin, row["pos"]) <= 4 and row.get("can_see") is True and row.get("can_dig") is True)):
                continue
            if row.get("can_see") is not False and self.rules._key(row["pos"]) not in blocked:
                return self._row_decision(alternative)
        if (resource == "cobblestone" and "plan_starter_staircase" in context["capabilities"] and not cautious
                and obs.get("local_geometry_origin") == origin):
            if target_count is None:
                target_count = (self._data["progress"]["target"] if self._data["task"] == "gather"
                                else counts.get("cobblestone", 0) + 3)
            survey = self._mine.survey(obs, context, target_count)
            if survey is not None:
                return survey
            reposition = self._explore(obs, context)
            if reposition is not None:
                return reposition
        if resource == "logs" and not cautious:
            remote_log = context.get("remote_log_target") if isinstance(context, dict) else None
            pos = self.rules._pos(remote_log)
            if (pos is not None and math.dist(origin, pos) > 4 and self.rules._key(pos) not in blocked
                    and (pos[0], pos[2]) not in blocked_columns | occluded_columns):
                return Decision("move_to", {"pos": list(pos), "range": 2}, "Approach distant observed trees outside local scan radius", "scout_logs")
        if resource in {"cobblestone", "coal", "raw_iron", "diamond"} and not cautious and isinstance(context, dict) and context.get("deep_mining_enabled") is True:
            stone_plus = sum(counts.get(t + "_pickaxe", 0) for t in ("stone", "iron", "diamond", "netherite"))
            iron_plus = sum(counts.get(t + "_pickaxe", 0) for t in ("iron", "diamond", "netherite"))
            if (self.settings.get("campaign") is True and resource == "raw_iron"
                    and iron_plus == 0 and stone_plus < 2):
                # A stone pick can wear out reaching the iron band. Gather the
                # spare's material before starting an underground expedition.
                return self._craft("stone_pickaxe", 1, obs, context)
            if (resource == "diamond" and counts.get("diamond_pickaxe", 0) == 0
                    and counts.get("netherite_pickaxe", 0) == 0):
                if iron_plus < 2 and (iron_plus == 0 or counts.get("iron_ingot", 0) + counts.get("raw_iron", 0) >= 3):
                    spare_iron = self._craft("iron_pickaxe", 1, obs, context)
                    if spare_iron is not None:
                        return spare_iron
                elif iron_plus < 3 and counts.get("iron_ingot", 0) >= 3:
                    spare_iron = self._craft("iron_pickaxe", 1, obs, context)
                    if spare_iron is not None:
                        return spare_iron
            if (resource in {"raw_iron", "coal"}
                    and stone_plus < (3 if resource == "raw_iron" else 2)
                    and (counts.get("cobblestone", 0) >= 3
                         or any(row["name"] in {"stone", "cobblestone"}
                                and math.dist(origin, row["pos"]) <= self._data["args"]["radius"]
                                for row in blocks))
                    and (counts.get("stick", 0) >= 2 or any(counts.get(n, 0) >= 1 for n in (*PLANK_NAMES, *LOG_NAMES)))):
                spare = self._craft("stone_pickaxe", 1, obs, context)
                if spare is not None:
                    return spare
            stash = self._stash_bulk_if_crowded(obs, counts, context)
            if stash is not None:
                return stash
            if resource not in self._data.get("deep_mine_ores", []):
                if target_count is not None:
                    needed = max(1, target_count - counts.get(resource, 0))
                elif self._data["task"] == "gather" and self._data["args"].get("resource") == resource:
                    needed = max(1, self._data["progress"]["target"] - self._quantity(obs))
                else:
                    needed = {"diamond": 3, "cobblestone": 12, "raw_iron": 12, "coal": 4}[resource]
                return self._deep_mine_decision(resource, obs, count=needed)
        return None

    def _gather_navigation_hints(self, obs):
        """Short-lived failed approaches are exclusions, never new route proof."""
        status, now = obs["status"], time.time()
        hints = []
        for recent in self._data["recent_results"][-8:]:
            hint = recent.get("gather_navigation") if isinstance(recent, dict) else None
            if (not isinstance(hint, dict) or not _finite(hint.get("expires_at"))
                    or not now < hint["expires_at"] <= now + 30
                    or hint.get("dimension") != status.get("dimension")
                    or hint.get("life") != self._life(status)
                    or self.rules._pos(hint.get("origin")) is None or self.rules._pos(hint.get("target")) is None
                    or not isinstance(hint.get("expected_name"), str)
                    or hint.get("expected_name") not in set().union(*RESOURCE_NAMES.values())):
                continue
            hints.append(hint)
        return hints

    def _remember_gather_navigation(self, recent, decision, result, observation):
        data = result.data if isinstance(result.data, dict) else {}
        target = self.rules._pos(decision.args.get("pos"))
        origin = self.rules._pos(data.get("pos_before"))
        visibility = data.get("visibility")
        blocker = (self.rules._pos(visibility.get("blocker")) if isinstance(visibility, dict)
                   and visibility.get("state") == "occluded" and visibility.get("visible") is False else None)
        approach_occluded = (result.error_code == "target_occluded" and decision.name == "gather_block"
                             and decision.args.get("expected_name") in LOG_NAMES and data.get("target") == target
                             and data.get("executed") is True and data.get("can_see") is False
                             and blocker is not None and all(v == math.floor(v) for v in blocker))
        if (decision.name not in {"gather_block", "gather_resource"} or result.success is not False
                or result.error_code not in GATHER_NAVIGATION_FAILURES and not approach_occluded
                or data.get("action_state") == "unknown" or data.get("block_removed") is True or data.get("gained")
                or target is None or origin is None or math.dist(origin, target) <= 4
                or not self._fresh(observation)):
            return
        status = observation["status"]
        if (status.get("busy") not in (None, False) or status.get("dimension") != self._data["dimension"]
                or self._life(status) != self._data["life"]):
            return
        recent["gather_navigation"] = {"origin": list(status["pos"]), "target": target,
            "expected_name": decision.args.get("expected_name"), "dimension": status["dimension"],
            "life": self._life(status), "expires_at": time.time() + 30}
        if approach_occluded:
            recent["gather_navigation"]["blocker"] = list(blocker)

    def _obtain(self, item, needed, obs, context, depth, *, output=None):
        counts = obs["counts"]
        accepted = ingredient_items(item, output)
        if item in LOG_NAMES:
            held = sum(counts.get(name, 0) for name in accepted)
            return self._gather("logs", obs, context, set(accepted), target_count=held + needed)
        if item in RESOURCE_NAMES and accepted == (item,):
            return self._gather(item, obs, context, target_count=counts.get(item, 0) + needed)
        if item == "planks":
            for wood, planks in zip(WOOD_SPECIES, PLANK_NAMES):
                if any(counts.get(name, 0) for name in wood_inputs(wood)):
                    return self._craft(planks, 4 * math.ceil(needed / 4), obs, context, depth + 1)
        if not context.get("_bounded_bootstrap"):
            names = set(accepted)
            found = self._drop(names, obs, context) or self._withdraw(names, needed, obs, context)
            if found is not None:
                return found
        if item == "planks":
            return self._gather("logs", obs, context)
        if item in CRAFT_ITEMS:
            return self._craft(item, needed, obs, context, depth + 1)
        if item == "iron_ingot":
            return self._smelt(item, obs, context, depth + 1)
        if item in RESOURCE_NAMES:
            return self._gather(item, obs, context, target_count=counts.get(item, 0) + needed)
        if item == "wheat":
            for row in self.daily._safe_rows(obs, {"wheat"}, context=context):
                age = (row.get("properties") or {}).get("age")
                if age in (7, "7"):
                    return self._decision("harvest_crop", {"pos": row["pos"], "expected_name": "wheat"}, "Harvest currently mature wheat")
        return None

    def _resource_child(self, decision, obs, context):
        """Board/workstation prerequisites use the same fresh resource recovery."""
        if decision and decision.name in {"gather_block", "gather_resource"}:
            expected = decision.args.get("expected_name")
            for resource, names in RESOURCE_NAMES.items():
                if expected in names:
                    return self._gather(resource, obs, context)
        # A table visible across the valley should not pull the builder away
        # from the doorway when it is already carrying its own workstation.
        base = (obs.get("daily_evidence") or {}).get("base_site")
        local_table_for_door = (decision and decision.name == "move_to" and self._data["task"] == "shelter"
            and obs["counts"].get("crafting_table", 0) > 0
            and self.daily._verified(base, obs["status"].get("dimension"))
            and math.dist(obs["status"]["pos"], base["pos"]) <= 5
            and any(row["name"] == "crafting_table" and row["pos"] == decision.args.get("pos")
                    and math.dist(obs["status"]["pos"], row["pos"]) > 3.5 for row in self.rules._blocks(obs)))
        if decision and (local_table_for_door or decision.name == "place_item" and decision.args.get("item") == "crafting_table"
                or decision.name == "wait" and decision.goal == "crafting_table" and obs["counts"].get("crafting_table", 0)):
            origin = obs["status"]["pos"]
            if not self._fresh(obs) or obs.get("local_geometry_origin") != origin:
                return None
            feet = [math.floor(v) for v in origin]
            blocked = self.rules._blocked(context)
            for row in self.daily._rows(obs.get("placement_candidates"))[:8]:
                pos = self.rules._pos(row.get("pos"))
                if (pos is None or any(v != math.floor(v) for v in pos)
                        or row.get("support") not in DailySurvivalSkills.SUPPORT
                        or math.dist(origin, pos) > 3 or pos in (feet, [feet[0], feet[1] + 1, feet[2]])
                        or self.rules._key(pos) in blocked or self.rules._dangerous_hostiles(obs, pos)
                        or self._workspace_cell_reserved(pos, obs)):
                    continue
                candidate = self._decision("place_item", {"item": "crafting_table", "pos": list(pos)},
                                           "Place the held workstation at one freshly verified close empty cell")
                if self.safety_policy.validate(candidate, obs, context) is None:
                    return candidate
            return None
        return decision

    def _workspace_cell_reserved(self, pos, obs):
        """A base is only an exclusion hint; placement still needs fresh geometry."""
        base = (obs.get("daily_evidence") or {}).get("base_site")
        if not self.daily._verified(base, obs["status"].get("dimension")):
            return False
        directions = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
        orientation = base.get("orientation", "north")
        direction = directions.get(orientation) if isinstance(orientation, str) else None
        if direction is None:
            return True
        x, y, z = (pos[i] - base["pos"][i] for i in range(3))
        if 0 <= y <= 2 and abs(x) <= 2 and abs(z) <= 2 and (max(abs(x), abs(z)) == 2 or y == 2):
            return True  # Do not occupy unfinished walls or roof.
        front, side = x * direction[0] + z * direction[1], x * direction[1] - z * direction[0]
        if 0 <= y <= 1 and side == 1 and -1 <= front <= 1:
            return True  # Preserve one complete bed side, including its front stance.
        return 0 <= y <= 1 and side == 0 and 0 <= front <= 3  # Centre, inner approach, door and exit.

    def _craft(self, item, needed, obs, context, depth=0):
        if depth > 8 or item not in RECIPES or needed <= 0:
            return None
        counts = obs["counts"]
        plan = plan_craft(item, needed, counts)
        if plan.missing:
            if item == "bone_meal":
                return None  # Craft from inventory without detouring for bones.
            ingredient, missing = plan.missing
            total_planks = sum(counts.get(n, 0) for n in PLANK_NAMES)
            if ((item == "stick" and not any(counts.get(n, 0) for n in LOG_NAMES))
                    or (ingredient == "stick" and total_planks < RECIPES[item][0].get("planks", 0) + 2)):
                bush = self._gather_dead_bush(obs, context)
                if bush is not None:
                    return bush
            return self._obtain(ingredient, missing, obs, context, depth, output=item)
        if plan.needs_table:
            workspace = self.rules._workspace(obs, context)
            if workspace:
                if workspace.goal == "explore":
                    if any(b["name"] == "crafting_table" for b in self.rules._blocks(obs)):
                        return None
                    return self._gather("logs", obs, context) or workspace
                return self._resource_child(workspace, obs, context)
        return self._decision("craft_item", {"item": item, "count": plan.request_count},
                              "Craft the batch supported by the current ingredients and verify inventory gain")

    @staticmethod
    def _window_to_furnace_row(pos, window, dimension):
        if not isinstance(window, dict) or window.get("window") != "furnace":
            return None
        sections = window.get("sections")
        info = window.get("info")
        if not isinstance(sections, dict) or not isinstance(info, dict) or type(info.get("burning")) is not bool:
            return None
        def slot(name):
            rows = sections.get(name)
            if not isinstance(rows, list) or len(rows) > 1:
                return None
            if not rows:
                return {"name": None, "count": 0}
            item = rows[0]
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or type(item.get("count")) is not int:
                return None
            return {"name": item["name"], "count": item["count"]}
        slots = {name: slot(name) for name in ("input", "fuel", "output")}
        if any(value is None for value in slots.values()):
            return None
        return {"pos": list(pos), "name": "furnace", "dimension": dimension, "fresh": True, "verified": True,
                "source": "inspect_container", **slots, "burning": info["burning"], "info": copy.deepcopy(info)}

    def _smelt(self, item, obs, context, depth=0):
        if depth > 8:
            return None
        if self.settings.get("campaign") is True:
            origin = obs["status"]["pos"]

            def local(row):
                pos = self.rules._pos(row.get("pos"))
                return (pos is not None and abs(pos[1] - origin[1]) <= 1.25
                        and math.dist(pos, origin) <= 3 and row.get("can_see") is not False)

            obs = {**obs,
                   **{key: [row for row in self.daily._rows(obs.get(key)) if row.get("name") != "furnace" or local(row)]
                      for key in ("blocks", "terrain")},
                   "furnaces": [row for row in self.daily._rows(obs.get("furnaces")) if local(row)]}
            context = {**context, "smelting_jobs": [row for row in context.get("smelting_jobs", []) if local(row)]}
        counts = obs["counts"]
        smelt_obs = obs
        inspected = self._data.get("inspected_furnace")
        if (isinstance(inspected, dict) and inspected.get("dimension") == obs["status"].get("dimension")
                and any(row.get("pos") == inspected.get("pos") for row in self.daily._safe_rows(obs, {"furnace"}, context=context))
                and not any(row.get("pos") == inspected.get("pos") and self.daily._verified(row, obs["status"].get("dimension"))
                            for row in self.daily._rows(obs.get("furnaces")))):
            smelt_obs = {**obs, "furnaces": [*list(obs.get("furnaces") or []), inspected]}
        current = self.daily._smelt({item}, smelt_obs, counts, self._data["task"], context)
        if current and current["name"] != "wait":
            return self._row_decision(current)
        if item == "iron_ingot" and counts.get("raw_iron", 0) >= 8:
            existing_furnaces = self.daily._safe_rows(obs, {"furnace"}, context=context)
            desired_furnaces = 3 if counts.get("raw_iron", 0) >= 16 else 2
            has_fuel = any(counts.get(f, 0) >= 1 for f in ("coal", "charcoal")) or (
                sum(counts.get(n, 0) for n in PLANK_NAMES)
                + 4 * sum(counts.get(n, 0) for n in LOG_NAMES)
            ) >= 6
            if (len(existing_furnaces) < desired_furnaces and has_fuel
                    and (counts.get("furnace", 0) >= 1 or counts.get("cobblestone", 0) >= 8)):
                if counts.get("furnace", 0) >= 1:
                    placed = self.daily._place("furnace", obs, self._data["task"], context)
                    if placed:
                        return self._row_decision(placed)
                else:
                    crafted = self._craft("furnace", 1, obs, context, depth + 1)
                    if crafted:
                        return crafted
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
            if item == "glass":
                return self._gather("sand", obs, context)
            animals = {"cooked_beef": "cow", "cooked_porkchop": "pig", "cooked_mutton": "sheep", "cooked_chicken": "chicken"}
            if item in animals:
                return self._hunt({animals[item]}, obs, context)
            if item == "baked_potato":
                for row in self.daily._safe_rows(obs, {"potatoes"}, context=context):
                    if (row.get("properties") or {}).get("age") in (7, "7"):
                        return self._decision("harvest_crop", {"pos": row["pos"], "expected_name": "potatoes"}, "Acquire potatoes for the selected cooked output")
            return self._drop(set(inputs), obs, context)
        if not any(counts.get(fuel, 0) for fuel in {"coal", "charcoal", *PLANK_NAMES}):
            return self._obtain("planks", 8 if counts.get("raw_iron", 0) >= 8 else 2, obs, context, depth + 1)
        dim = obs["status"].get("dimension")
        if any(self.daily._verified(row, dim) and self.daily._burning(row) for row in self.daily._rows(smelt_obs.get("furnaces"))):
            return self._decision("wait", {"seconds": 10}, "Wait for residual furnace burn to finish before loading next batch")
        return None

    def _hunt(self, names, obs, context):
        for row in self.daily._safe_rows(obs, names, entities=True, context=context):
            if type(row.get("id")) is not int or math.dist(row["pos"], obs["status"]["pos"]) > 8:
                continue
            if not any(obs["counts"].get(item) for item in HUNTING_TOOLS):
                return self._craft("stone_sword", 1, obs, context)
            if not self.daily._can_hunt(obs, obs["counts"], row):
                continue
            if math.dist(row["pos"], obs["status"]["pos"]) > 2.8:
                approach = self.daily._hunt_approach(obs, row, context, self._data["task"])
                if approach is not None:
                    return self._row_decision(approach)
                continue
            return self._decision("hunt_animal", {"entity_id": row["id"], "expected_name": row["name"]}, "Hunt only the species needed for this goal")
        return None

    def _recent_hunt_failures(self):
        return sum(
            1 for row in self._data.get("recent_results", [])[-4:]
            if isinstance(row, dict) and not row.get("success") and row.get("name") in {"hunt_animal", "move_to"}
        )

    def _food(self, obs, context):
        if self.settings.get("campaign") is True:
            for source, output in DailySurvivalSkills.SMELTABLE.items():
                if output in SAFE_FOOD and obs["counts"].get(source, 0):
                    return self._smelt(output, obs, context)
        status = obs.get("status") or {}
        if status.get("food", 0) >= 16 and status.get("health", 0) >= 16 and self._recent_hunt_failures() >= 2:
            return None
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
        if (self._data["task"] != "shelter" and not self._mine.rejected
                or obs.get("local_geometry_origin") != origin
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
        if not self._fresh(obs) or obs.get("local_geometry_origin") != origin:
            return None
        blocked = self.rules._blocked(context)
        hostiles = self.rules._hostiles(obs, origin)
        gather_recovery = any(tuple(map(math.floor, row["origin"])) == tuple(map(math.floor, origin))
                              and math.dist(row["origin"], origin) <= .5 for row in self._gather_navigation_hints(obs))
        candidates = []
        for row in self.daily._rows(obs.get("safe_stand_candidates"))[:8]:
            pos = self.rules._pos(row.get("pos"))
            if pos is None or row.get("support") not in DailySurvivalSkills.SUPPORT:
                continue
            key = self.rules._key(pos)
            distance = math.dist(origin, pos)
            enough_movement = (distance > .4 and tuple(map(math.floor, pos)) != tuple(map(math.floor, origin))
                               if self._data["task"] == "shelter" or gather_recovery or self._mine.rejected else distance >= 1)
            if (key in blocked or key in self._data["visited"] or len(self._mine.rejected.get(key, [])) == 4
                    or self._data["task"] == "shelter" and key in context.get("shelter_visited_stances", [])
                    or not enough_movement or distance > 3
                    or abs(pos[1] - origin[1]) > .15 and not self._local_shelter_step(origin, pos, obs)
                    or any(math.dist(pos, point) <= self.rules.threat_radius for _, point in hostiles)):
                continue
            candidates.append(pos)
        self._prune_shelter_search()
        rejected_centers = {tuple(row["pos"]) for row in self._shelter_search["rejected"].values()}
        if self._data["task"] == "shelter" and (self._data["survey_steps"] >= 4 or len(rejected_centers) >= 4):
            # Local height density is only a search hint, never flat-site or
            # route proof. Rank the already permitted candidates; no new move
            # coordinates or wider stair permissions are introduced here.
            neighbours_pool = tuple(candidates)
            def search_rank(pos):
                level = 0 if abs(pos[1] - origin[1]) <= .15 else 1 if pos[1] < origin[1] else 2
                neighbours = {tuple(other) for other in neighbours_pool
                              if other != pos and abs(other[1] - pos[1]) <= .15
                              and math.dist(pos, other) <= 1.5}
                return level, -len(neighbours)
            candidates.sort(key=search_rank)
        if candidates:
            return Decision("position_for_task", {"pos": list(candidates[0])}, "Inspect from one freshly checked local stance; route is revalidated by the skill", "explore")
        return None

    def _shelter_stockpile(self, obs, context):
        """Gather the bounded fresh house deficit before returning to build.

        These quantities are planning hints, never remembered site authority.
        Recompute from the current complete inspection and current inventory on
        every child; restart/life/safety/unknown gates remain in next_action.
        """
        base = (obs.get("daily_evidence") or {}).get("base_site")
        shelter = obs.get("shelter")
        dimension = obs["status"].get("dimension")
        if (not self._fresh(obs) or not self.daily._verified(base, dimension)
                or not isinstance(shelter, dict) or shelter.get("fresh") is not True
                or shelter.get("pos") != base["pos"] or shelter.get("dimension") != dimension
                or shelter.get("orientation", "north") != base.get("orientation", "north")
                or not isinstance(base.get("orientation", "north"), str)
                or base.get("orientation", "north") not in {"north", "east", "south", "west"}):
            return None, False
        geometry = shelter.get("geometry")
        missing = shelter.get("missing_blocks")
        maximum = sum(cell.stage in {"lower_walls", "upper_walls", "roof"} for cell in SHELTER_5X5_SPEC.cells)
        if (not isinstance(geometry, dict) or geometry.get("floor_verified") is not True or geometry.get("exit_verified") is not True
                or shelter.get("hazards") != [] or shelter.get("occupied_blueprint_cells") != []
                or type(missing) is not int or not 0 <= missing <= maximum):
            return None, False
        door = shelter.get("door") or {}
        if not isinstance(door, dict):
            return None, False
        return self._shelter_material_stockpile(obs, context, missing, door.get("frame_verified") is not True)

    def _shelter_material_stockpile(self, obs, context, missing, door_needed):
        """Compute inventory demand separately from authority to build at a site."""
        counts = obs["counts"]
        needs_door = door_needed and not any(counts.get(name, 0) for name in DailySurvivalSkills.DOORS)
        tables = self.daily._safe_rows(obs, {"crafting_table"}, context=context)
        table_planks = 4 if needs_door and not counts.get("crafting_table", 0) and not tables else 0
        wall_materials = sum(counts.get(name, 0) for name in SHELTER_BUILDING_BLOCKS - set(PLANK_NAMES))
        wall_demand = max(0, missing - wall_materials)
        plank_demand = wall_demand + (6 if needs_door else 0) + table_planks
        woods = [wood for wood in WOOD_SPECIES if wood + "_door" in DailySurvivalSkills.DOORS]
        wood_supply = {wood: counts.get(wood + "_planks", 0)
            + 4 * sum(counts.get(name, 0) for name in wood_inputs(wood)) for wood in woods}
        total_supply = sum(wood_supply.values())
        building_woods = {wood for wood in woods if wood + "_planks" in SHELTER_BUILDING_BLOCKS}
        wall_supply = sum(wood_supply[wood] for wood in building_woods)
        visible = {row["name"] for row in self.rules._blocks(obs)}
        # Reserve one craftable door species; mixed plank totals cannot meet it.
        door_woods = sorted(woods, key=lambda wood: (wood_supply[wood],
            bool(set(wood_inputs(wood)) & visible)), reverse=True)
        door_short = needs_door and wood_supply[door_woods[0]] < 6
        if not door_short and total_supply >= plank_demand and wall_supply >= wall_demand:
            return None, False
        if door_short:
            # A few held planks cannot lock the goal to an unavailable species.
            # Each alternative still goes through the real fresh resource filter.
            decision = next((candidate for wood in door_woods
                if (candidate := self._gather("logs", obs, context, set(wood_inputs(wood))))), None)
        else:
            names = {name for wood in building_woods for name in wood_inputs(wood)} if wall_supply < wall_demand else None
            decision = self._gather("logs", obs, context, names)
        if decision:
            decision = Decision(decision.name, decision.args,
                f"Stock the current shelter deficit before returning: {plank_demand} plank-equivalents needed, "
                f"{total_supply} held; door needs one six-plank species", decision.goal)
        # Do not return to place the last few blocks when more stock is needed.
        # The existing bounded local exploration handles unavailable resources.
        return decision, True

    def _board_action(self, task, obs, context):
        project = active_project(context.get("shelter_project"), scope=self._world_scope,
                                 dimension=obs["status"].get("dimension"))
        base = (obs.get("daily_evidence") or {}).get("base_site")
        if task == "shelter" and project and not (
                self.daily._verified(base, obs["status"].get("dimension")) and same_site(project, base)):
            # Historical counts can guide safe local resource work, never
            # fabricate a base_site or authorize placement or site preparation.
            stock, required = self._shelter_material_stockpile(obs, context,
                project["missing_blocks_hint"], project["door_needed_hint"])
            if required:
                return stock, False
            return self._return_to_project(obs, context, project), False
        if task == "shelter":
            stock, required = self._shelter_stockpile(obs, context)
            if required:
                return stock, False
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
            if task == "iron_tools":
                offhand = obs["status"].get("offhand") if isinstance(obs["status"].get("offhand"), dict) else {}
                if obs["counts"].get("shield", 0) > 0 and offhand.get("name") != "shield":
                    return self._equip_loadout_decision(obs, item_hint="iron_helmet", slot_hint="head",
                                                        reason="Equip shield to off-hand and best armor"), False
            return None, task != "maintenance"
        seen = set()
        def choose(entry):
            if not entry or entry["id"] in seen:
                return None
            seen.add(entry["id"])
            if entry.get("next_action"):
                action = entry["next_action"]
                # The board suggests a generic radial walk when it has no visible
                # ore. Leave the choice to the resource planner, which can assess
                # spare tools, exposed ore, storage and the bounded shaft.
                if (task == "iron_tools" and action["name"] == "move_to" and action.get("goal") == "explore"
                        and context.get("deep_mining_enabled") is True):
                    return None
                return self._row_decision(action)
            for required in entry.get("depends_on", []):
                dependency = rows.get(required)
                if dependency and dependency["status"] != "satisfied":
                    candidate = choose(dependency)
                    if candidate:
                        return candidate
            return None
        candidate = choose(row)
        counts = obs["counts"]
        if (task == "starter_tools" and "plan_starter_staircase" in context["capabilities"]
                and not self.rules._has_tool(obs["counts"], "pickaxe") and obs["counts"].get("wooden_pickaxe", 0)
                and not (candidate and candidate.name == "take_items")):
            return self._craft("stone_pickaxe", 1, obs, context), False
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
                    logs = sum(obs["counts"].get(name, 0) for name in wood_inputs(wood))
                    return planks >= 6, planks + logs * 4 >= 6, wood + "_log" in visible, planks
                woods = [wood for wood in WOOD_SPECIES if wood + "_door" in DailySurvivalSkills.DOORS]
                wood = max(woods, key=available)
                # _craft already uses the checked resource planner. Do not
                # normalize its species-specific gather into arbitrary logs.
                return self._craft(wood + "_door", 1, obs, context), False
        if (task == "starter_tools" and candidate is not None and candidate.name == "move_to"
                and any(counts.get(p + "_pickaxe", 0) for p in ("wooden", "stone", "iron", "diamond", "netherite"))
                and context.get("deep_mining_enabled") is True and context.get("_bounded_bootstrap") is not True):
            for tool in ("stone_pickaxe", "stone_axe", "stone_sword"):
                if tool in (row.get("missing") or []) and counts.get("cobblestone", 0) < RECIPES[tool][0]["cobblestone"]:
                    return self._craft(tool, 1, obs, context), False
        if candidate is None:
            entrance = entrance_geometry(obs.get("shelter")) if task == "shelter" else None
            if (entrance and project and self.daily._verified(base, obs["status"].get("dimension"))
                    and same_site(project, base)):
                # A temporarily unavailable work/door stance is not permission
                # to abandon a verified house and resume generic exploration.
                return self._return_to_project(obs, context, {**project, "pos": entrance["outside"]}), False
            if task == "bed" and not any(counts.get(bed, 0) for bed in DailySurvivalSkills.BEDS):
                wool = next((name for name, count in counts.items() if name.endswith("_wool") and count >= 3), None)
                candidate = self._craft(wool[:-5] + "_bed", 1, obs, context) if wool else self._hunt({"sheep"}, obs, context)
            elif task == "storage" and not counts.get("chest", 0):
                candidate = self._craft("chest", 1, obs, context)
            elif task == "farm" and any(counts.get(seed, 0) for seed in DailySurvivalSkills.SEEDS) and not any(
                    count and name.endswith("_hoe") for name, count in counts.items()):
                candidate = self._craft("stone_hoe", 1, obs, context)
            elif task == "iron_tools" and context.get("deep_mining_enabled") is True and context.get("_bounded_bootstrap") is not True:
                if (any(counts.get(t + "_pickaxe", 0) for t in ("stone", "iron", "diamond", "netherite"))
                        and counts.get("raw_iron", 0) == 0 and "raw_iron" not in self._data.get("deep_mine_ores", [])):
                    return self._gather("raw_iron", obs, context, target_count=4), False
                if (any(counts.get(t + "_pickaxe", 0) for t in ("wooden", "stone", "iron", "diamond", "netherite"))
                        and counts.get("coal", 0) + counts.get("charcoal", 0) == 0 and "coal" not in self._data.get("deep_mine_ores", [])):
                    return self._gather("coal", obs, context, target_count=4), False
        return self._resource_child(candidate, obs, context), False

    def _return_to_project(self, obs, context, project):
        if self._data["shelter_return_steps"] >= MAX_RETURN_STEPS:
            self._escalate("shelter_return_budget_exhausted")
            return None
        visited = self._data["shelter_return_visited"]
        if not visited:
            visited.append(list(obs["status"]["pos"]))
        entrance = entrance_geometry(obs.get("shelter"))
        if entrance and (obs.get("shelter") or {}).get("dimension") == obs["status"].get("dimension"):
            obs = {**obs, "safe_stand_candidates": [row for row in self.daily._rows(obs.get("safe_stand_candidates"))
                if avoids_shell(obs["status"]["pos"], row.get("pos"), entrance)]}
        move = choose_shelter_return(obs, project, {**context, "threat_radius": self.rules.threat_radius},
            visited=visited, allow_step=self._local_shelter_step)
        if move is None:
            self._escalate("shelter_project_requires_fresh_geometry_or_safe_return")
        return move

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
        entrance = entrance_geometry(shelter)
        if entrance and requested in stands:
            targets = [entrance_waypoint(origin, entrance)]
        project = active_project(context.get("shelter_project"), scope=self._world_scope,
                                 dimension=status.get("dimension"))
        if project and same_site(project, base) and min(math.dist(origin, p) for p in targets) > 3:
            target = min(targets, key=lambda p: math.dist(origin, p))
            return self._return_to_project(obs, context, {**project, "pos": list(target)})
        blocked = self.rules._blocked(context)
        def usable(pos):
            return (pos is not None and (abs(pos[1] - origin[1]) <= .05
                                        or self._local_shelter_step(origin, pos, obs))
                    and .4 < math.dist(origin, pos) <= 3
                    and self.rules._key(pos) not in blocked
                    and (entrance is None or avoids_shell(origin, pos, entrance))
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
            if entrance and project and same_site(project, base):
                target = min(targets, key=lambda p: math.dist(origin, p))
                return self._return_to_project(obs, context, {**project, "pos": list(target)})
            return None
        target = min(candidates, key=lambda p: (min(math.dist(p, t) for t in targets), math.dist(origin, p)))
        return self._decision("position_for_task", {"pos": list(target)},
                              "Take one freshly observed shelter work stance; recheck its corridor before moving")

    def _effective_max_seconds(self):
        base = float(self.limits["max_seconds"])
        if (self._data.get("task") == "gather"
                and (self._data.get("args") or {}).get("resource") == "diamond"
                and "max_seconds" not in (self.settings.get("autonomy") or {})):
            return max(base, 900.0)
        if (self._data.get("task") == "nether_portal"
                and "max_seconds" not in (self.settings.get("autonomy") or {})):
            return max(base, 600.0)
        return base

    def remaining_seconds(self):
        """Return the current goal's unused wall-clock budget for a delegated workflow."""
        if self._data["status"] not in {"running", "suspended"}:
            return 0.0
        return max(0.0, self._effective_max_seconds() - max(0.0, time.time() - self._data["started_at"]))

    def next_action(self, observation, context=None, *, relaxed_policy=None):
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
        exhausted = self._data["steps"] >= self.limits["max_steps"] or self.remaining_seconds() <= 0
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
        if relaxed_policy is not None and (not allows_goal(args, relaxed_policy)
                or not self.safety_policy.relaxation_stance(observation, context)):
            self.interrupt("local_safety_policy_required")
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
                or self.safety_policy._strict_night_block(observation, status) and relaxed_policy != NIGHT
                or not _finite(status.get("health")) or not _finite(status.get("food"))
                or status["health"] <= self.rules.low_health and not cautious and relaxed_policy != LOW_HEALTH
                or status["food"] <= 6
                or any(status.get(k) for k in ("in_water", "in_lava", "on_fire", "submerged", "is_sleeping", "in_vehicle"))
                or any(distance <= self.rules.threat_radius for distance, _ in self.rules._hostiles(observation, status["pos"]))
                or isinstance(status.get("nearest_hostile"), dict) and _finite(status["nearest_hostile"].get("dist"))
                   and status["nearest_hostile"]["dist"] <= self.rules.threat_radius):
            self.interrupt("local_safety_policy_required")
            return None
        local = self._context(context, observation)
        local["_bounded_bootstrap"] = cautious or relaxed_policy is not None
        obs = copy.deepcopy(observation)
        origin = obs["status"]["pos"]
        obs["blocks"] = [b for b in self.rules._blocks(obs) if math.dist(origin, b["pos"]) <= self._data["args"]["radius"]]
        if relaxed_policy is not None:
            obs["blocks"] = [b for b in obs["blocks"] if b["name"] not in LOG_NAMES or math.dist(origin, b["pos"]) <= 2.8]
        obs["entities"] = [row for row in self.daily._rows(obs.get("entities"))
                           if str(row.get("id")) not in self._data["blocked_entities"]]
        self.daily.targets["food_reserve"] = args["count"] if task == "food" else self.daily.targets["food_reserve"]
        if self._data["local_failures"] >= self.limits["max_local_failures"] or self._data["no_progress"] >= self.limits["max_no_progress_steps"]:
            self._escalate("bounded_local_recovery_exhausted")
            return None
        recovery = choose_route_foliage(self._data["recent_results"], obs, local, self.daily, task)
        if self._mine.site is not None:
            decision = self._mine.step(obs, local)
        elif recovery is not None:
            decision = recovery
        elif task == "gather":
            decision = self._gather(args["resource"], obs, local)
        elif task == "craft":
            item = args["item"]
            remaining = self._data["progress"]["target"] - self._quantity(obs)
            if item == "bone_meal" and obs["counts"].get("bone_block", 0):
                # The upstream craft endpoint takes an output, not a recipe ID.
                self._escalate("ambiguous_recipe: bone_block could be consumed; choose another goal")
                return None
            decision = self._craft(item, remaining, obs, local)
            if item == "bone_meal" and decision is None:
                missing = math.ceil(remaining / RECIPES[item][1])
                self._escalate(f"missing_material: bone x{missing}; use held bones only, choose another goal")
                return None
        elif task == "smelt":
            decision = self._smelt(args["item"], obs, local)
        elif task == "food":
            decision = self._food(obs, local)
        elif task == "recover_drops":
            names = {self.daily._drop_name(row) for row in self.daily._rows(obs.get("entities"))}
            decision = self._drop(names - {None}, obs, local)
        elif task == "scout":
            decision = self._scout(obs, local)
        elif task == "iron_armor":
            decision = self._iron_armor(obs, local)
        elif task == "nether_portal":
            decision = self._nether_portal(obs, local)
        else:
            decision, complete = self._board_action(task, obs, local)
            if complete:
                self._data["progress"]["current"] = 1
                self._set("completed", "Fresh task evidence verifies the selected goal")
                return None
        if self._data["status"] == "needs_ai":
            return None
        if decision and decision.goal not in {"deep_mine", "scout_logs", "scout_surface", "equip_loadout", "nether_portal"} and (
                decision.goal == "explore" and decision.name != "position_for_task"
                or decision.name == "move_to" and decision.args.get("pos") not in
                [b["pos"] for b in self.rules._blocks(obs)] + [row.get("pos") for row in self.daily._rows(obs.get("entities"))]):
            decision = self._shelter_stance(decision, obs, local)
            project = active_project(local.get("shelter_project"), scope=self._world_scope,
                                     dimension=status.get("dimension"))
            if decision is None and task == "shelter" and project:
                decision = self._return_to_project(obs, local, project)
                if decision is None:
                    return None
        if (relaxed_policy is not None and (decision is None
                or not self.safety_policy.allows_relaxed(decision, relaxed_policy, observation))):
            self.interrupt("no_stationary_step_for_relaxed_work")
            return None
        if decision is None and not (task == "food" and status.get("food", 0) >= 16 and status.get("health", 0) >= 16 and self._recent_hunt_failures() >= 2):
            if task == "shelter" and self.settings.get("campaign") is True and local["shelter_survey_reposition"]:
                self._escalate("shelter_needs_new_patch")
                return None
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
        delegated_move = decision.name == "move_to" and decision.goal in {"scout_surface", "nether_portal"}
        if (not delegated_move
                and decision.args.get("pos") is not None
                and self.rules._key(decision.args["pos"]) in self.rules._blocked(local)):
            self._escalate("selected_target_is_cooling_down")
            return None
        if (not delegated_move and decision.name == "move_to"
                and math.dist(origin, decision.args["pos"]) <= decision.args.get("range", 2) + .5):
            decision = self._explore(obs, local)
            if decision is None:
                self._escalate("no_progress_move_requires_new_strategy")
                return None
        self._before_child = self._child_metric(decision, observation)
        self._data.update(current_step=decision.name, pending_digest=_digest([decision.name, decision.args]),
                          fingerprint=self._fingerprint(observation) if self._before_child is None else None)
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
        elif task == "scout":
            complete = bool(self._data.get("scout_completed"))
            current = int(complete)
        elif task == "nether_portal":
            complete = bool(self._data.get("portal_built")) or any(
                b.get("name") == "nether_portal" for b in (observation.get("blocks") or []) if isinstance(b, dict)
            )
            current = int(complete)
        elif task == "iron_armor":
            current = self._iron_armor_count(observation)
            all_worn = all(
                armor_rank((read_worn_slot_item(observation, s) or {}).get("name"), s)
                >= armor_rank(f"iron_{SLOT_SUFFIX[s]}", s)
                for s in ARMOR_SLOTS
            )
            complete = all_worn
        elif task == "maintenance":
            return False
        else:
            board = self.daily.build(observation, (context or {}).get("memory"), context)
            key = TASK_ROWS.get(task, task)
            complete = any(row["id"] == key and row["status"] == "satisfied" for row in board["tasks"])
            if task == "iron_tools" and observation["counts"].get("shield", 0):
                offhand = observation["status"].get("offhand") or {}
                complete = complete and isinstance(offhand, dict) and offhand.get("name") == "shield"
            current = int(complete)
        if not complete or self._mine.site is not None:
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
        if decision.goal == "deep_mine":
            pending_dm = self._data.pop("pending_deep_mine", None)
            self._data["pending_deep_mine"] = None
            res_key = (pending_dm or {}).get("resource")
            if res_key and res_key not in self._data["deep_mine_ores"]:
                self._data["deep_mine_ores"] = (self._data["deep_mine_ores"] + [res_key])[-8:]
        elif decision.goal == "scout_surface" and result.success is True:
            self._data["scout_completed"] = True
        elif decision.goal == "nether_portal" and decision.name == "move_to":
            if result.success is True and (data.get("portal_active") is True or data.get("structure_verified") is True):
                self._data["portal_built"] = True
            elif data.get("portal_skill") is True:
                self._data["portal_attempted"] = True
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
        if decision.name in STARTER_MINING_SKILLS:
            if not self._fresh(observation) or observation["status"].get("dimension") != self._data["dimension"] or self._life(observation["status"]) != self._data["life"]:
                self._escalate("staircase_result_requires_fresh_same_life_observation")
                return
            issue = self._mine.observe_result(decision, result, observation)
            if issue:
                self._escalate(issue)
                return
            recent.update(operation=data.get("operation"), depth=data.get("depth"),
                          gained_cobblestone=data.get("gained_cobblestone", 0))
        if self._data["task"] == "shelter":
            refused = shelter_route_refusal(decision, result, observation)
            if refused is not None:
                recent["shelter_route_refusal"] = refused
        hint = position_foliage_hint(decision, result, observation)
        if hint is not None:
            recent["route_foliage"] = hint
        for target in cleared_route_targets(self._data["recent_results"], decision, result, observation):
            key = self.rules._key(target)
            if self._data["blocked_codes"].get(key) == "unsafe_route":
                self._data["blocked"].pop(key, None)
                self._data["blocked_codes"].pop(key, None)
                self._data["visited"] = [old for old in self._data["visited"] if old != key]
        cleared = confirmed_leaf_removal(decision, result, observation)
        if cleared is not None:
            for previous in self._data["recent_results"]:
                navigation = previous.get("gather_navigation") or {}
                if navigation.get("blocker") == cleared:
                    del previous["gather_navigation"]
        self._remember_gather_navigation(recent, decision, result, observation)
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
                if remaining and self.settings.get("campaign") is not True:
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
                tree = decision.args.get("expected_name") in LOG_NAMES
                cooldown = 90 if not tree and code in (PATH_FAILURES | {"timeout"}) else 30
                self._data["blocked"][self.rules._key(target)] = time.time() + cooldown
                self._data["blocked"] = dict(list(self._data["blocked"].items())[-16:])
                self._data["blocked_codes"][self.rules._key(target)] = code or "no_movement"
                self._data["blocked_codes"] = {k: v for k, v in self._data["blocked_codes"].items() if k in self._data["blocked"]}
            moved_animal = (decision.name == "hunt_animal" and code == "out_of_range" and data.get("executed") is False
                            and self.rules._pos(data.get("target_pos")) is not None)
            if type(decision.args.get("entity_id")) is int and not moved_animal:
                self._data["blocked_entities"][str(decision.args["entity_id"])] = time.time() + 30
                self._data["blocked_entities"] = dict(list(self._data["blocked_entities"].items())[-16:])
        if decision.goal in {"explore", "shelter_preparation_position"}:
            self._data["explore_steps"] += 1
            target = self.rules._pos(decision.args.get("pos"))
            if target:
                self._data["visited"] = (self._data["visited"] + [self.rules._key(target)])[-16:]
        if decision.goal == "shelter_return":
            self._data["shelter_return_steps"] += 1
            if (result.success is True and data.get("verified") is True and self._fresh(observation)
                    and math.dist(observation["status"]["pos"], decision.args["pos"]) <= .4):
                arrived = list(observation["status"]["pos"])
                if not self._data["shelter_return_visited"] or self._data["shelter_return_visited"][-1] != arrived:
                    self._data["shelter_return_visited"] = (self._data["shelter_return_visited"] + [arrived])[-64:]
        if decision.name == "inspect_container" and result.success and data.get("verified") is True:
            row = self._window_to_furnace_row(decision.args.get("pos"), data.get("window"), self._data["dimension"])
            if row is not None:
                self._data["inspected_furnace"] = row
            else:
                self._data.pop("inspected_furnace", None)
        else:
            self._data.pop("inspected_furnace", None)
        if decision.name == "store_items" and not result.success:
            self._data["stash_done"] = True
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
            if self._before_child is None:
                progress = self._fingerprint(observation) != self._data.get("fingerprint")
            else:
                progress = self._child_metric(decision, observation) > self._before_child
            recent["made_progress"] = progress
            furnaces = list(self.daily._rows(observation.get("furnaces")))
            inspected = self._data.get("inspected_furnace")
            if isinstance(inspected, dict) and not any(row.get("pos") == inspected.get("pos") for row in furnaces):
                furnaces.append(inspected)
            processing = decision.name == "wait" and any(self.daily._verified(row, self._data["dimension"])
                and (self.daily._burning(row) or (row.get("input") or {}).get("count", 0))
                for row in furnaces)
            self._data["no_progress"] = 0 if progress else self._data["no_progress"] + (0 if processing or negative_read else 1)
            if progress:
                if not negative_read:
                    self._data["local_failures"] = 0
                if decision.name == "position_for_task" and result.success is True:
                    self._data["survey_since_move"] = 0
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
        self._before_child = None
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
        if self._mine.target:
            result["starter_mine"] = {"site": copy.deepcopy(self._mine.site), "cobblestone_target": self._mine.target,
                                      "returning": self._mine.returning}
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
        self._data.update(pending_digest=None, current_step=None, drop_before=None, pending_deep_mine=None, fresh=True)
        self._set("cancelled", "Explicit fresh reobservation abandoned the old goal; unknown evidence is retained")
        return True

    def restore_interrupted_mine(self, obs, context, old):
        """Adopt an entrance into a new goal only at a freshly grounded rung of the same life."""
        anchor = old.get("mine_landmark")
        status = obs.get("status") or {}
        previous = old.get("observed")
        if (not isinstance(anchor, dict) or not isinstance(previous, dict)
                or type(anchor.get("target")) is not int or not 1 <= anchor["target"] <= 4096
                or anchor.get("operation") not in ("descend", "ascend", "dig")
                or self._mine.site is not None or self._data.get("pending_digest") or self._unknown(context)
                or self._data["status"] not in {"running", "suspended"} or not self._fresh(obs)
                or obs.get("local_geometry_origin") != status["pos"] or status.get("on_ground") is not True
                or "busy" not in status or status["busy"] not in (False, None) or status.get("gamemode") != "survival"
                or self._generation_changed(context) or old.get("generation") != self._data["life_generation"]
                or status.get("dimension") != self._data["dimension"] or status.get("dimension") != previous.get("dimension")
                or status.get("last_death") != previous.get("life") or self._data["args"] != anchor.get("goal")):
            return False
        site = anchor.get("site")
        depth = rung_depth(site, status["pos"])
        old_depth = rung_depth(site, anchor.get("from"))
        if (depth is None or old_depth is None or rung_depth(site, previous.get("cell")) is None
                or set(site) != {"entry", "direction"} or (anchor["operation"] == "ascend" and depth == old_depth)):
            return False
        self._mine.site = copy.deepcopy(site)
        self._mine.target = anchor["target"]
        # Continue below the old descent only if already on a deeper rung.
        # A stationary uncertain dig or descent can only lead to a checked return.
        self._mine.returning = not (anchor["operation"] == "descend" and depth > old_depth)
        return True

    def state(self):
        """Serializable bounded evidence checkpoint; pending hashes cannot execute."""
        self._prune_shelter_search()
        return {**copy.deepcopy(self._data), "starter_mine": self._mine.state(),
                "shelter_search_cache": {"version": 1, **copy.deepcopy(self._shelter_search)}}
