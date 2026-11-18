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
from skills.daily_survival import DailySurvivalSkills
from skills.equipment import ARMOR_SLOTS, SLOT_SUFFIX, armor_rank, read_worn_slot_item


TASKS = frozenset({"gather", "craft", "smelt", "food", "starter_tools", "iron_tools", "iron_armor", "scout",
                  "shelter", "bed", "storage", "farm", "recover_drops", "maintenance"})
IRON_ARMOR_ORDER = ("iron_chestplate", "iron_leggings", "iron_helmet", "iron_boots")
RESOURCES = frozenset({"logs", "cobblestone", "coal", "raw_iron", "diamond"})
SMELT_ITEMS = frozenset(DailySurvivalSkills.SMELTABLE.values())
RECIPES = {
    "crafting_table": ({"planks": 4}, 1), "stick": ({"planks": 2}, 4),
    "furnace": ({"cobblestone": 8}, 1), "chest": ({"planks": 8}, 1),
    "torch": ({"coal": 1, "stick": 1}, 4), "bread": ({"wheat": 3}, 1),
    "shield": ({"planks": 6, "iron_ingot": 1}, 1), "bucket": ({"iron_ingot": 3}, 1),
    **{f"{tier}_{tool}": ({material: quantity, "stick": sticks}, 1)
       for tier, material in (("wooden", "planks"), ("stone", "cobblestone"), ("iron", "iron_ingot"), ("diamond", "diamond"))
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
                  "raw_iron": DailySurvivalSkills.IRON,
                  "diamond": frozenset({"diamond_ore", "deepslate_diamond_ore"})}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


class AutonomousTaskController:
    """A caller executes children and supplies their real results and observations."""

    def __init__(self, settings=None, saved=None):
        self.settings = copy.deepcopy(settings or {})
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
                      "deep_mine_ores": [], "pending_deep_mine": None,
                      "scout_completed": False, "loadout_equipped": False,
                      "pending_digest": None, "baseline": 0, "sequence": 0, "life_generation": 0}
        if saved is not None:
            self._restore(saved)

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
                or len(json.dumps(saved, allow_nan=False).encode()) > 32768):
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
                    "local_recoveries", "emergency_interruptions", "ai_escalations", "life_generation"):
            if type(self._data[key]) is not int or self._data[key] < 0:
                raise ValueError("Saved counters are invalid")
        for key, maximum in (("recent_results", 8), ("visited", 16), ("jobs", 8),
                             ("observed_drop_ids", 40), ("recovered_drop_ids", 40), ("deep_mine_ores", 8)):
            if not isinstance(self._data[key], list) or len(self._data[key]) > maximum:
                raise ValueError("Saved evidence exceeds its limit")
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
                                 if args["task"] == "food" else 4
                                 if args["task"] == "iron_armor" else max(1, len(self._data["observed_drop_ids"]))
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
        return args.get("resource", args.get("item", "food_items" if args["task"] == "food"
                        else "pieces" if args["task"] == "iron_armor"
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
            "scout_completed": bool(self._data.get("scout_completed")),
            "loadout_equipped": bool(self._data.get("loadout_equipped"))})

    def _context(self, context, observation=None):
        result = copy.deepcopy(context or {})
        now = time.time()
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
        result["smelting_jobs"] = (self.daily._rows(result.get("smelting_jobs")) + copy.deepcopy(self._data["jobs"]))[-8:]
        result["explore_index"] = self._data["explore_steps"]
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
        if not self._data.get("loadout_equipped"):
            for piece, slot in (("iron_chestplate", "torso"), ("iron_leggings", "legs"),
                                ("iron_helmet", "head"), ("iron_boots", "feet")):
                worn = read_worn_slot_item(obs, slot)
                if counts.get(piece, 0) > 0 and armor_rank((worn or {}).get("name"), slot) < armor_rank(piece, slot):
                    return self._equip_loadout_decision(obs, item_hint=piece, slot_hint=slot,
                                                        reason=f"Equip crafted {piece} and best available gear")
        for piece in IRON_ARMOR_ORDER:
            slot = "torso" if piece == "iron_chestplate" else "head" if piece == "iron_helmet" else "legs" if piece == "iron_leggings" else "feet"
            worn = read_worn_slot_item(obs, slot)
            if armor_rank((worn or {}).get("name"), slot) >= armor_rank(piece, slot):
                continue
            if counts.get(piece, 0) > 0:
                continue
            return self._craft(piece, 1, obs, context)
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
        origin = obs["status"]["pos"]
        blocks = self.rules._blocks(obs)
        blocked, hostiles = self.rules._blocked(context), self.rules._hostiles(obs, origin)
        blocked_occlusion = {key for key, code in self._data["blocked_codes"].items()
                             if code in {"target_occluded", "visibility_unknown"}}
        candidates = [row for row in blocks if row["name"] in (names or RESOURCE_NAMES[resource])
                      and math.dist(origin, row["pos"]) <= self._data["args"]["radius"]
                      and (resource != "logs" or abs(float(row["pos"][1]) - float(origin[1])) <= 3.1)
                      and (not cautious or row.get("can_see") is True and row.get("can_dig") is True
                           and math.dist(origin, row["pos"]) <= 3)
                      and self.rules._safe_target(row, origin, blocks, blocked - blocked_occlusion, hostiles, observation=obs)]
        candidates.sort(key=lambda row: (
            0 if (resource != "logs" or abs(float(row["pos"][1]) - float(origin[1])) <= 2.1) else 1,
            0 if row.get("can_see") is True else 2 if row.get("can_see") is False else 1,
            math.dist(origin, row["pos"]),
            row["pos"],
        ))
        for row in candidates:
            action = {"name": "gather_resource" if resource == "raw_iron" else "gather_block",
                      "args": {"pos": list(row["pos"]), "expected_name": row["name"]},
                      "reason": "Gather one fresh resource for the selected goal", "goal": self._data["task"]}
            alternative = self.daily._foliage_hint(action, obs, context, context["capabilities"])
            if alternative["name"] == "clear_foliage" or (row.get("can_see") is not False and self.rules._key(row["pos"]) not in blocked):
                return self._row_decision(alternative)
        if resource == "logs" and not cautious:
            remote_log = context.get("remote_log_target") if isinstance(context, dict) else None
            pos = self.rules._pos(remote_log)
            if pos is not None and math.dist(origin, pos) > 4:
                return Decision("move_to", {"pos": list(pos), "range": 2}, "Approach distant observed trees outside local scan radius", "scout_logs")
        if resource in {"cobblestone", "coal", "raw_iron", "diamond"} and not cautious and isinstance(context, dict) and context.get("deep_mining_enabled") is True:
            stone_plus = sum(counts.get(t + "_pickaxe", 0) for t in ("stone", "iron", "diamond", "netherite"))
            iron_plus = sum(counts.get(t + "_pickaxe", 0) for t in ("iron", "diamond", "netherite"))
            if (resource == "diamond" and counts.get("diamond_pickaxe", 0) == 0
                    and counts.get("netherite_pickaxe", 0) == 0 and iron_plus < 2):
                spare_iron = self._craft("iron_pickaxe", 1, obs, context)
                if spare_iron is not None:
                    return spare_iron
            if (resource == "raw_iron" and stone_plus < 3
                    and counts.get("cobblestone", 0) >= 3
                    and (counts.get("stick", 0) >= 2 or any(counts.get(n, 0) >= 1 for n in PLANK_NAMES))):
                spare = self._craft("stone_pickaxe", 1, obs, context)
                if spare is not None:
                    return spare
            stash = self._stash_bulk_if_crowded(obs, counts, context)
            if stash is not None:
                return stash
            if resource not in self._data.get("deep_mine_ores", []):
                needed = (max(1, self._data["progress"]["target"] - self._quantity(obs))
                          if self._data["task"] == "gather" and self._data["args"].get("resource") == resource
                          else 3 if resource == "diamond" else 12 if resource == "cobblestone" else 7 if resource == "raw_iron" else 4)
                return self._deep_mine_decision(resource, obs, count=needed)
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
        if item in {"cobblestone", "coal", "raw_iron", "diamond"}:
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
                total_planks = sum(counts.get(n, 0) for n in PLANK_NAMES)
                needed_planks = int(recipe.get("planks", 0))
                if ((item == "stick" and total_planks < amount and not any(counts.get(n, 0) for n in LOG_NAMES))
                        or (ingredient == "stick" and total_planks < needed_planks + 2)):
                    bush = self._gather_dead_bush(obs, context)
                    if bush is not None:
                        return bush
                return self._obtain(ingredient, amount - held, obs, context, depth)
        if item not in {"stick", "crafting_table", "torch", *PLANK_NAMES}:
            workspace = self.rules._workspace(obs, context)
            if workspace:
                return self._resource_child(workspace, obs, context) if workspace.name != "wait" else None
        return self._decision("craft_item", {"item": item, "count": min(64, needed, output)}, "Craft one available recipe and verify the new inventory")

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
        counts = obs["counts"]
        smelt_obs = obs
        inspected = self._data.get("inspected_furnace")
        if (isinstance(inspected, dict) and inspected.get("dimension") == obs["status"].get("dimension")
                and any(row.get("pos") == inspected.get("pos") for row in self.daily._safe_rows(obs, {"furnace"}, context=context))
                and not any(row.get("pos") == inspected.get("pos") and self.daily._verified(row, obs["status"].get("dimension"))
                            for row in self.daily._rows(obs.get("furnaces")))):
            smelt_obs = {**obs, "furnaces": [*list(obs.get("furnaces") or []), inspected]}
        current = self.daily._smelt({item}, smelt_obs, counts, self._data["task"], context)
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
            if (key in blocked or key in self._data["visited"] or not 1 <= math.dist(origin, pos) <= 3
                    or abs(pos[1] - origin[1]) > .15
                    or any(math.dist(pos, point) <= self.rules.threat_radius for _, point in hostiles)):
                continue
            return Decision("position_for_task", {"pos": list(pos)}, "Inspect from one freshly checked local stance; route is revalidated by the skill", "explore")
        return None

    def _board_action(self, task, obs, context):
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
            if task == "iron_tools" and not self._data.get("loadout_equipped"):
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
                return self._row_decision(entry["next_action"])
            for required in entry.get("depends_on", []):
                dependency = rows.get(required)
                if dependency and dependency["status"] != "satisfied":
                    candidate = choose(dependency)
                    if candidate:
                        return candidate
            return None
        candidate = choose(row)
        counts = obs["counts"]
        if (task == "starter_tools" and candidate is not None and candidate.name == "move_to"
                and any(t in (row.get("missing") or []) for t in ("stone_pickaxe", "stone_axe", "stone_sword"))
                and any(counts.get(p + "_pickaxe", 0) for p in ("wooden", "stone", "iron", "diamond", "netherite"))
                and counts.get("cobblestone", 0) < 8
                and context.get("deep_mining_enabled") is True and context.get("_bounded_bootstrap") is not True
                and "cobblestone" not in self._data.get("deep_mine_ores", [])):
            return self._deep_mine_decision("cobblestone", obs, count=12), False
        if candidate is None:
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
                    return self._deep_mine_decision("raw_iron", obs, count=4), False
                if (any(counts.get(t + "_pickaxe", 0) for t in ("wooden", "stone", "iron", "diamond", "netherite"))
                        and counts.get("coal", 0) + counts.get("charcoal", 0) == 0 and "coal" not in self._data.get("deep_mine_ores", [])):
                    return self._deep_mine_decision("coal", obs, count=4), False
        return self._resource_child(candidate, obs, context), False

    def _effective_max_seconds(self):
        base = float(self.limits["max_seconds"])
        if (self._data.get("task") == "gather"
                and (self._data.get("args") or {}).get("resource") == "diamond"
                and "max_seconds" not in (self.settings.get("autonomy") or {})):
            return max(base, 900.0)
        return base

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
                     or time.time() - self._data["started_at"] >= self._effective_max_seconds())
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
                or self.safety_policy._strict_night_block(observation, status)
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
        elif task == "scout":
            decision = self._scout(obs, local)
        elif task == "iron_armor":
            decision = self._iron_armor(obs, local)
        else:
            decision, complete = self._board_action(task, obs, local)
            if complete:
                self._data["progress"]["current"] = 1
                self._set("completed", "Fresh task evidence verifies the selected goal")
                return None
        if decision and decision.goal not in {"deep_mine", "scout_logs", "scout_surface", "equip_loadout"} and (
                decision.goal == "explore" or decision.name == "move_to" and decision.args.get("pos") not in
                [b["pos"] for b in self.rules._blocks(obs)] + [row.get("pos") for row in self.daily._rows(obs.get("entities"))]):
            decision = None  # Replace blind radial exploration with checked local geometry.
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
        if (decision.goal != "scout_surface" and decision.name == "move_to"
                and math.dist(origin, decision.args["pos"]) <= decision.args.get("range", 2) + .5):
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
        elif task == "scout":
            complete = bool(self._data.get("scout_completed"))
            current = int(complete)
        elif task == "iron_armor":
            current = self._iron_armor_count(observation)
            all_worn = all(
                armor_rank((read_worn_slot_item(observation, s) or {}).get("name"), s)
                >= armor_rank(f"iron_{SLOT_SUFFIX[s]}", s)
                for s in ARMOR_SLOTS
            )
            complete = current >= 4 and (bool(self._data.get("loadout_equipped")) or all_worn)
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
        if decision.goal == "deep_mine":
            pending_dm = self._data.pop("pending_deep_mine", None)
            self._data["pending_deep_mine"] = None
            res_key = (pending_dm or {}).get("resource")
            if res_key and res_key not in self._data["deep_mine_ores"]:
                self._data["deep_mine_ores"] = (self._data["deep_mine_ores"] + [res_key])[-8:]
        elif decision.goal == "scout_surface" and result.success is True:
            self._data["scout_completed"] = True
        elif decision.goal == "equip_loadout":
            self._data["loadout_equipped"] = True
        elif decision.name == "craft_item" and result.success is True:
            crafted = str(decision.args.get("item") or "")
            if crafted == "shield" or any(crafted.endswith("_" + suffix) for suffix in SLOT_SUFFIX.values()):
                self._data["loadout_equipped"] = False
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
        no_motion = decision.name == "move_to" and (data.get("already_in_range") is True or data.get("executed") is False)
        if result.success is not True or no_motion:
            self._data["local_failures"] += 1
            self._data["local_recoveries"] += 1
            target = self.rules._pos(data.get("target")) or self.rules._pos(decision.args.get("pos"))
            if target:
                self._data["blocked"][self.rules._key(target)] = time.time() + 30
                self._data["blocked"] = dict(list(self._data["blocked"].items())[-16:])
                self._data["blocked_codes"][self.rules._key(target)] = code or "no_movement"
                self._data["blocked_codes"] = {k: v for k, v in self._data["blocked_codes"].items() if k in self._data["blocked"]}
            if type(decision.args.get("entity_id")) is int:
                self._data["blocked_entities"][str(decision.args["entity_id"])] = time.time() + 30
                self._data["blocked_entities"] = dict(list(self._data["blocked_entities"].items())[-16:])
        if decision.goal == "explore":
            self._data["explore_steps"] += 1
            target = self.rules._pos(decision.args.get("pos"))
            if target:
                self._data["visited"] = (self._data["visited"] + [self.rules._key(target)])[-16:]
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
            current = self._fingerprint(observation)
            progress = current != self._data.get("fingerprint")
            furnaces = list(self.daily._rows(observation.get("furnaces")))
            inspected = self._data.get("inspected_furnace")
            if isinstance(inspected, dict) and not any(row.get("pos") == inspected.get("pos") for row in furnaces):
                furnaces.append(inspected)
            processing = decision.name == "wait" and any(self.daily._verified(row, self._data["dimension"])
                and (self.daily._burning(row) or (row.get("input") or {}).get("count", 0))
                for row in furnaces)
            self._data["no_progress"] = 0 if progress else self._data["no_progress"] + (0 if processing else 1)
            if progress:
                self._data["local_failures"] = 0
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
        return {key: copy.deepcopy(self._data[key]) for key in keys}

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

    def state(self):
        """Serializable bounded evidence checkpoint; pending hashes cannot execute."""
        return copy.deepcopy(self._data)
