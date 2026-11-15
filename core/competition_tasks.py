"""Pure task lifecycle; caller owns I/O, world scope, journal and locks.

Pass fresh collector observations and the unmodified /advancements envelope to
update. Missing visible-tree IDs remain unknown. state() is a small checkpoint;
snapshot() is a detached model board. Nonterminal restores need fresh assessment.
No networking, skill execution, compact, handoff or lock recovery occurs here.
Injected catalog rows must already resolve overrides in competition_rules.
clock is a callable returning epoch seconds (time.time by default).
"""

import copy
import hashlib
import json
import math
import time
import threading
from functools import wraps
from typing import Any, Callable


SCHEMA_VERSION = 2
RULES_VERSION = "v2-draft"
REPEAT_THRESHOLD = 3
SUMMARY_LIMIT = 240
REASON_LIMIT = 160
RECENT_LIMIT = 8
_CHECKS = ("resources", "capabilities", "advancement")
_ELIGIBILITY = {"scored": "scored", "pending": "conditional_pending", "excluded": "excluded"}
_KNOWN_SKILLS = frozenset({"gather_block", "gather_resource", "craft_item", "place_item",
                           "smelt_start", "smelt_collect", "equip_armor", "auto_sleep", "plant_crop",
                           "eat_food", "place_bed", "gather_seeds", "harvest_crop", "till_soil",
                           "hunt_animal", "wait"})
_MAPPED = frozenset({"minecraft:story/mine_stone", "minecraft:story/upgrade_tools",
                     "minecraft:story/smelt_iron", "minecraft:story/iron_tools",
                     "minecraft:story/obtain_armor", "minecraft:adventure/sleep_in_bed",
                     "minecraft:husbandry/plant_seed"})


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _text(value: Any, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def _checks() -> dict:
    return {key: {"checked": False, "status": "unknown"} for key in _CHECKS}


def _synchronized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


class CompetitionTasks:
    """Manage one task without executing game skills.

    Lifecycle failures return recoverable result dicts. Bad constructor settings
    or checkpoints raise ValueError. Returned objects are deep copies.
    capabilities is None (unknown), or an iterable of registered skill names.
    update_progress changes display text only, never completion or checks.
    """

    def __init__(self, settings: dict | None = None, saved: dict | None = None,
                 catalog: list[dict] | None = None, clock: Callable | None = None):
        self._lock = threading.RLock()
        self._assessed_at = None
        self._fetched_at = None
        if settings is None:
            settings = {}
        if not isinstance(settings, dict):
            raise ValueError("Competition settings must be a dict")
        self.settings = copy.deepcopy(settings)
        for key, default in (("defer_seconds", 300), ("max_seconds", 28800)):
            value = self.settings.get(key, default)
            if not _number(value) or key == "max_seconds" and value == 0:
                raise ValueError(f"Invalid {key}")
            self.settings[key] = value
        overrides = self.settings.get("eligibility_overrides", {})
        if not isinstance(overrides, dict) or any(not isinstance(k, str) or type(v) is not bool
                                                  for k, v in overrides.items()):
            raise ValueError("eligibility_overrides must map IDs to booleans")
        if catalog is None:
            from core.competition_rules import scoring_catalog
            catalog = scoring_catalog(overrides=overrides)
        elif overrides:
            raise ValueError("Injected catalog must already resolve overrides in the rules registry")
        self._catalog = self._validate_catalog(catalog)
        self._by_id = {row["id"]: row for row in self._catalog}
        self._clock = clock if clock is not None else time.time
        if not callable(self._clock):
            raise ValueError("clock must be callable")
        now = self._now()
        self._state = {
            "version": SCHEMA_VERSION, "phase": "selection", "active_goal_id": None,
            "started_at": now, "active_since": None, "confirmed": [], "deferred": {},
            "summary": "", "last_exit": None, "end_reason": None,
            "repeat": {"signature": None, "count": 0, "strikes": 0, "replan_attempted": False}, "recent_actions": [],
        }
        if saved is not None:
            self._state = self._validate_saved(saved)
        self._observation = None
        self._capabilities = None
        self._completion = {}
        self._server_statuses = {}
        self._explicit_status_ids = set()
        self._api = {"ready": False, "stale": True, "error_code": "not_observed"}
        self._assessment = _checks()
        self._plans = {}
        self._evidence = None

    @staticmethod
    def _validate_catalog(catalog: Any) -> list[dict]:
        if not isinstance(catalog, list):
            raise ValueError("Catalog must be a list of resolved scoring rows")
        rows, seen = [], set()
        for row in catalog:
            if not isinstance(row, dict):
                raise ValueError("Invalid catalog row")
            ident = row.get("id")
            if (not isinstance(ident, str) or not ident.strip() or len(ident) > 200
                    or ident in seen or not isinstance(row.get("eligibility"), str)
                    or row["eligibility"] not in _ELIGIBILITY
                    or not isinstance(row.get("title"), str) or not row["title"].strip()
                    or not isinstance(row.get("condition"), str)
                    or type(row.get("difficulty")) is not int or not 0 <= row["difficulty"] <= 5
                    or type(row.get("points")) is not int or row["points"] < 0):
                raise ValueError("Invalid or duplicate catalog row")
            seen.add(ident)
            try:
                json.dumps(row, allow_nan=False)
            except (TypeError, ValueError, RecursionError):
                raise ValueError("Catalog rows must contain serializable values") from None
            rows.append(copy.deepcopy(row))
        return rows

    def _validate_saved(self, saved: Any) -> dict:
        def fail():
            raise ValueError("Invalid competition checkpoint schema or version")

        if not isinstance(saved, dict):
            fail()
        # Migrate checkpoints from before explicit session termination existed.
        if (type(saved.get("version")) is int and saved["version"] == 1
                and set(saved) == set(self._state) - {"end_reason"}
                and saved.get("phase") in ("selection", "active")):
            saved = {**saved, "version": SCHEMA_VERSION, "end_reason": None}
        if set(saved) != set(self._state):
            fail()
        if type(saved["version"]) is not int or saved["version"] != SCHEMA_VERSION:
            fail()
        if saved["phase"] not in ("selection", "active", "completed"):
            fail()
        goal = saved["active_goal_id"]
        if saved["phase"] == "active":
            if not isinstance(goal, str) or goal not in self._by_id or not _number(saved["active_since"]):
                fail()
        elif goal is not None or saved["active_since"] is not None:
            fail()
        end_reason = saved["end_reason"]
        if saved["phase"] == "completed":
            if (not isinstance(end_reason, str) or not end_reason.strip()
                    or len(end_reason) > REASON_LIMIT):
                fail()
        elif end_reason is not None:
            fail()
        if not _number(saved["started_at"]):
            fail()
        if saved["active_since"] is not None and saved["active_since"] < saved["started_at"]:
            fail()
        confirmed, deferred = saved["confirmed"], saved["deferred"]
        if (not isinstance(confirmed, list) or any(not isinstance(x, str) or x not in self._by_id for x in confirmed)
                or len(set(confirmed)) != len(confirmed) or not isinstance(deferred, dict)
                or any(not isinstance(k, str) or k not in self._by_id or not _number(v)
                       for k, v in deferred.items())):
            fail()
        if not isinstance(saved["summary"], str) or len(saved["summary"]) > SUMMARY_LIMIT:
            fail()
        last = saved["last_exit"]
        if last is not None:
            if (not isinstance(last, dict) or set(last) != {"goal_id", "reason", "outcome", "at"}
                    or not isinstance(last["goal_id"], str) or last["goal_id"] not in self._by_id
                    or not isinstance(last["reason"], str) or len(last["reason"]) > REASON_LIMIT
                    or last["outcome"] not in ("completed", "finished_without_completion", "forced_exit")
                    or not _number(last["at"])):
                fail()
        repeat = saved["repeat"]
        if (not isinstance(repeat, dict) or set(repeat) != {"signature", "count", "strikes", "replan_attempted"}
                or not self._signature(repeat["signature"], optional=True)
                or type(repeat["count"]) is not int or not 0 <= repeat["count"] < REPEAT_THRESHOLD
                or type(repeat["strikes"]) is not int or not 0 <= repeat["strikes"] <= 1
                or type(repeat["replan_attempted"]) is not bool
                or repeat["replan_attempted"] and repeat["strikes"] == 0
                or repeat["signature"] is None and repeat["count"] != 0):
            fail()
        recent = saved["recent_actions"]
        if not isinstance(recent, list) or len(recent) > RECENT_LIMIT:
            fail()
        for action in recent:
            if (not isinstance(action, dict)
                    or set(action) != {"name", "success", "error_code", "outcome_unknown"}
                    or not isinstance(action["name"], str) or len(action["name"]) > 64
                    or action["success"] is not None and type(action["success"]) is not bool
                    or not isinstance(action["error_code"], str) or len(action["error_code"]) > 64
                    or type(action["outcome_unknown"]) is not bool):
                fail()
        try:
            json.dumps(saved, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            fail()
        return copy.deepcopy(saved)

    @staticmethod
    def _signature(value: Any, optional: bool = False) -> bool:
        return optional and value is None or (isinstance(value, str) and len(value) == 64
                                             and all(c in "0123456789abcdef" for c in value))

    def _now(self) -> float:
        value = self._clock()
        if not _number(value):
            raise ValueError("clock must return finite nonnegative seconds")
        return float(value)

    @_synchronized
    def state(self) -> dict:
        """Return a detached checkpoint; caller stores it in its scoped journal."""
        return copy.deepcopy(self._state)

    @staticmethod
    def _read_advancements(envelope: Any) -> tuple[dict, dict]:
        """Check the entire envelope before accepting any row, never infer absence.

        Upstream summary counts the pre-status-filter visible tree. Explicit
        truncation, malformed rows or inconsistent summary fails closed.
        """
        unavailable = {"ready": False, "stale": True, "error_code": "advancements_unavailable"}
        if (not isinstance(envelope, dict) or envelope.get("ok") is not True
                or envelope.get("error") is not None):
            return {}, unavailable
        result = envelope.get("result")
        if not isinstance(result, dict) or result.get("ready") is not True:
            return {}, unavailable
        invalid = {"ready": False, "stale": True, "error_code": "invalid_advancements"}
        for source in (envelope, result):
            if any(key in source for key in ("pagination", "paging", "page_info", "page", "page_size", "limit")):
                return {}, invalid
            if source.get("ok", True) is not True or source.get("success", True) is not True:
                return {}, invalid
            for key in ("stale", "partial", "truncated", "has_more", "has_next_page"):
                if key in source and source[key] is not False:
                    return {}, invalid
            for key in ("complete", "page_complete"):
                if key in source and source[key] is not True:
                    return {}, invalid
            for key in ("next", "next_cursor", "next_page"):
                if source.get(key) is not None:
                    return {}, invalid
            if "offset" in source and (type(source["offset"]) is not int or source["offset"] != 0):
                return {}, invalid
        rows = result.get("advancements")
        if not isinstance(rows, list):
            return {}, invalid
        summary = result.get("summary")
        if (not isinstance(summary, dict) or type(summary.get("total")) is not int
                or summary["total"] != len(rows)
                or any(type(summary.get(k)) is not int or summary[k] < 0
                       for k in ("completed", "in_progress", "not_started"))
                or sum(summary[k] for k in ("completed", "in_progress", "not_started")) != len(rows)):
            return {}, invalid
        if (type(result.get("packets_received")) is not int or result["packets_received"] < 1
                or type(result.get("updated_at")) not in (int, float)
                or not math.isfinite(result["updated_at"]) or result["updated_at"] <= 0):
            return {}, invalid
        completion = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
                return {}, invalid
            ident = row["id"]
            if ident in completion:
                return {}, invalid
            status = row.get("status")
            if "status" in row and status not in ("completed", "in_progress", "not_started"):
                return {}, invalid
            for key in ("done", "completed"):
                if key in row and type(row[key]) is not bool:
                    return {}, invalid
            yes = status == "completed" or row.get("done") is True or row.get("completed") is True
            no = status in ("in_progress", "not_started") or row.get("done") is False or row.get("completed") is False
            if yes and no:
                return {}, invalid
            completion[ident] = "completed" if yes else "not_completed" if no else "unknown"
        names = ("completed", "in_progress", "not_started")
        for key in names:
            if sum(row.get("status") == key for row in rows) != summary[key]:
                return {}, invalid
        for key in ("total", "total_count"):
            if key in result and (type(result[key]) is not int or result[key] != len(rows)):
                return {}, invalid
        return completion, {"ready": True, "stale": False, "error_code": None,
                            "updated_at": result["updated_at"]}

    @staticmethod
    def _resources(observation: Any) -> tuple[bool, dict, set]:
        if not isinstance(observation, dict):
            return False, {}, set()
        status = observation.get("status")
        if (observation.get("fresh") is not True or observation.get("stale", False) is not False
                or not isinstance(status, dict) or status.get("ready") is not True
                or status.get("alive") is not True or status.get("busy") not in (None, False)
                or not isinstance(observation.get("blocks"), list)):
            return False, {}, set()
        if observation.get("counts") is None and not isinstance(observation.get("inventory"), list):
            return False, {}, set()
        counts = observation.get("counts")
        if counts is None:
            items = observation.get("inventory")
            if not isinstance(items, list):
                return False, {}, set()
            counts = {}
            for item in items:
                if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                        or type(item.get("count")) is not int or item["count"] < 0):
                    return False, {}, set()
                name = item["name"].removeprefix("minecraft:")
                counts[name] = counts.get(name, 0) + item["count"]
        if not isinstance(counts, dict) or any(not isinstance(k, str) or not k
                                              or type(v) is not int or v < 0 for k, v in counts.items()):
            return False, {}, set()
        counts = {k.removeprefix("minecraft:"): v for k, v in counts.items()}
        # Only nearby, located blocks count as currently observed facilities.
        blocks, origin = set(), status.get("pos")
        if (isinstance(origin, (list, tuple)) and len(origin) == 3
                and all(type(x) in (int, float) and math.isfinite(x) for x in origin)):
            for block in observation.get("blocks", []) if isinstance(observation.get("blocks", []), list) else []:
                if not isinstance(block, dict) or not isinstance(block.get("name"), str):
                    continue
                pos = block.get("pos")
                if (isinstance(pos, (list, tuple)) and len(pos) == 3
                        and all(type(x) in (int, float) and math.isfinite(x) for x in pos)
                        and math.dist(origin, pos) <= 32):
                    blocks.add(block["name"].removeprefix("minecraft:"))
        return True, counts, blocks

    def _plan(self, ident: str, known: bool, counts: dict, blocks: set) -> dict:
        plan = {"availability": "unknown", "stage": "prerequisites", "reason": "requires_planning",
                "missing": [], "next_steps": [], "required_skills": []}
        if ident not in _MAPPED:
            # Unsupported is about this limited planner, not the Minecraft API.
            plan.update(availability="unsupported", reason="hostile_combat_unavailable"
                        if ident == "minecraft:adventure/kill_a_mob" else "requires_planning")
            return plan
        if not known:
            plan["reason"] = "fresh_resources_required"
            return plan
        path = ident.split("/")[-1]
        has = lambda item, n=1: counts.get(item, 0) >= n
        station = lambda item: has(item) or item in blocks
        picks = ("wooden", "stone", "iron", "golden", "diamond", "netherite")
        pick = any(has(f"{tier}_pickaxe") for tier in picks)
        stone_pick = any(has(f"{tier}_pickaxe") for tier in ("stone", "iron", "diamond", "netherite"))
        planks = sum(n for name, n in counts.items() if name.endswith("_planks"))
        logs = sum(n for name, n in counts.items() if name.endswith(("_log", "_wood", "_stem", "_hyphae")))
        visible_logs = any(name.endswith(("_log", "_stem")) for name in blocks)
        stone = bool(blocks & {"stone", "cobblestone", "deepslate"})
        ore = bool(blocks & {"iron_ore", "deepslate_iron_ore"})
        fuel = any(has(item) for item in ("coal", "charcoal")) or planks > 0 or logs > 0
        raw = any(has(item) for item in ("raw_iron", "iron_ore", "deepslate_iron_ore"))
        missing, steps, required = [], [], set()

        def need(condition, label):
            if not condition:
                missing.append(label)

        def step(label, *skills):
            if label not in steps and len(steps) < 5:
                steps.append(label)
            required.update(skill for skill in skills if skill)

        def wood_work():
            if not station("crafting_table"):
                if planks >= 4 or logs:
                    step("Craft a crafting table from current wood", "craft_item", "place_item")
                elif visible_logs:
                    step("Gather observed logs, then craft planks and a crafting table", "gather_block", "craft_item", "place_item")
                else:
                    step("Observe a local wood source")
            if not has("stick", 2):
                step("Prepare two sticks from planks", "craft_item")

        ready = False
        if path == "mine_stone":
            ready = pick and stone
            if not ready:
                need(pick, "wooden_or_better_pickaxe")
                need(stone or has("cobblestone"), "observed_stone")
                if not pick:
                    wood_work()
                    step("Craft a wooden pickaxe: 3 planks, 2 sticks, crafting table", "craft_item")
                if not stone:
                    step("Observe reachable stone")
            if not has("cobblestone"):
                required.add("gather_block")
                if ready:
                    step("Mine observed stone with the current pickaxe", "gather_block")
        elif path == "upgrade_tools":
            # Classic advancement fires on crafting the first stone tool.
            ready = has("cobblestone", 3) and has("stick", 2) and station("crafting_table")
            need(has("cobblestone", 3), "cobblestone:3")
            need(has("stick", 2), "stick:2")
            need(station("crafting_table"), "crafting_table")
            wood_work()
            if not has("cobblestone", 3):
                if not pick:
                    step("Craft a wooden pickaxe before gathering stone", "craft_item")
                step("Gather 3 cobblestone from observed stone", "gather_block")
            step("Craft a stone pickaxe", "craft_item")
        elif path == "smelt_iron":
            ready = raw and fuel and station("furnace")
            need(raw, "raw_iron:1")
            need(fuel, "fuel:1")
            need(station("furnace"), "furnace")
            if not raw:
                if not stone_pick:
                    step("Prepare a stone pickaxe: 3 cobblestone, 2 sticks, crafting table", "craft_item")
                step("Gather currently observed iron ore" if ore else "Observe local iron ore",
                     "gather_resource" if ore else "")
            if not station("furnace"):
                need(has("cobblestone", 8), "cobblestone:8_for_furnace")
                wood_work()
                if not has("cobblestone", 8):
                    step("Gather 8 cobblestone for a furnace", "gather_block")
                step("Craft and place a furnace", "craft_item", "place_item")
            if not fuel:
                step("Prepare coal, charcoal or wood fuel", "gather_block")
            step("Load furnace, wait and verify collected iron ingot", "smelt_start", "smelt_collect")
        elif path == "iron_tools":
            ready = has("iron_ingot", 3) and has("stick", 2) and station("crafting_table")
            need(has("iron_ingot", 3), "iron_ingot:3")
            need(has("stick", 2), "stick:2")
            need(station("crafting_table"), "crafting_table")
            if not stone_pick:
                step("Craft a stone pickaxe first: 3 cobblestone, 2 sticks, crafting table", "craft_item")
            if not has("iron_ingot", 3):
                step("Prepare and collect 3 iron ingots using a fueled furnace", "smelt_start", "smelt_collect")
            wood_work()
            step("Craft an iron pickaxe to trigger Isn't It Iron Pick", "craft_item")
        elif path == "obtain_armor":
            ready = has("iron_ingot", 4) and station("crafting_table")
            need(has("iron_ingot", 4), "iron_ingot:4_for_boots")
            need(station("crafting_table"), "crafting_table")
            if not stone_pick:
                step("Craft a stone pickaxe first: 3 cobblestone, 2 sticks, crafting table", "craft_item")
            if not has("iron_ingot", 4):
                step("Prepare and collect 4 iron ingots using a fueled furnace", "smelt_start", "smelt_collect")
            if not station("crafting_table"):
                wood_work()
            step("Craft iron boots to trigger Suit Up", "craft_item")
            # Suit Up needs acquiring armor, not a mandatory equip action.
        elif path == "sleep_in_bed":
            status = self._observation.get("status", {}) if isinstance(self._observation, dict) else {}
            visible_bed = any(name.endswith("_bed") for name in blocks)
            owned_bed = any(n > 0 and name.endswith("_bed") for name, n in counts.items())
            sleep_time = status.get("is_day") is False or status.get("thundering") is True
            overworld = status.get("dimension") in ("overworld", "minecraft:overworld")
            if "is_day" not in status or "thundering" not in status:
                plan.update(availability="unknown", reason="sleep_window_unobserved")
                return plan
            ready = visible_bed and sleep_time and overworld
            need(visible_bed, "observed_bed")
            need(sleep_time, "night_or_thunderstorm")
            need(overworld, "overworld")
            required.add("auto_sleep")
            if not visible_bed:
                required.update(("place_bed", "craft_item"))
                if not owned_bed:
                    need(any(n >= 3 and name.endswith("_wool") for name, n in counts.items()), "same_color_wool:3")
                    need(planks >= 3, "planks:3")
                    need(station("crafting_table"), "crafting_table")
                    sheep = any(isinstance(e, dict) and e.get("name") == "sheep"
                                for e in (self._observation or {}).get("entities", [])
                                if isinstance((self._observation or {}).get("entities", []), list))
                    if not any(n >= 3 and name.endswith("_wool") for name, n in counts.items()):
                        step("Gather 3 wool of one color from observed sheep" if sheep else "Observe a local wool source",
                             *(["hunt_animal"] if sheep else []))
                    if not station("crafting_table"):
                        wood_work()
                    step("Craft a bed: 3 matching wool, 3 planks, crafting table", "craft_item")
                step("Place the current bed on checked two-block ground", "place_bed")
            if not sleep_time:
                required.add("wait")
                step("Wait and reobserve until night or thunderstorm", "wait")
            if not overworld:
                plan.update(reason="unsafe_sleep_dimension", availability="unsupported")
                return plan
            step("Use auto_sleep and refresh server advancement", "auto_sleep")
        elif path == "plant_seed":
            seed = any(has(item) for item in ("wheat_seeds", "beetroot_seeds", "carrot", "potato"))
            farmland = "farmland" in blocks
            ready = seed and farmland
            need(seed, "supported_seed:1")
            need(farmland, "observed_farmland")
            required.add("plant_crop")
            if not seed:
                if blocks & {"short_grass", "tall_grass"}:
                    required.add("gather_seeds")
                    step("Try observed grass for wheat seeds; verify inventory gain", "gather_seeds")
                elif blocks & {"wheat", "carrots", "potatoes", "beetroots"}:
                    required.add("harvest_crop")
                    step("Harvest only an observed mature crop for planting stock", "harvest_crop")
                else:
                    step("Observe local grass or mature crops for planting stock")
            if not farmland:
                required.add("till_soil")
                hoe = any(n > 0 and name.endswith("_hoe") for name, n in counts.items())
                need(hoe, "hoe")
                need("water" in blocks, "hydration_water")
                need(bool(blocks & {"dirt", "grass_block"}), "clear_dirt_or_grass")
                if not hoe:
                    if not station("crafting_table"):
                        wood_work()
                    step("Craft a hoe from 2 planks or cobblestone and 2 sticks", "craft_item")
                step("Observe clear soil with nearby water; till it with a hoe", "till_soil")
            step("Plant current seed on freshly checked farmland", "plant_crop")
        plan.update(availability="ready" if ready else "needs_prerequisites",
                    stage="early" if ready else "prerequisites", reason="resources_ready" if ready else "resource_gap",
                    missing=missing, next_steps=steps, required_skills=sorted(required))
        if self._capabilities is None:
            plan.update(availability="unknown", reason="capabilities_unknown")
        elif not required <= self._capabilities:
            plan.update(availability="unsupported", reason="missing_registered_skills")
        return plan

    def _refresh_resources(self, observation: Any) -> bool:
        known, counts, blocks = self._resources(observation)
        if known:
            current = (counts, blocks)
            if self._evidence is not None:
                old_counts, old_blocks = self._evidence
                progressed = (any(n > old_counts.get(k, 0) for k, n in counts.items())
                              or bool(blocks - old_blocks))
                if progressed:
                    self._reset_repeat()
            self._evidence = copy.deepcopy(current)
        self._observation = copy.deepcopy(observation) if isinstance(observation, dict) else None
        self._plans = {row["id"]: self._plan(row["id"], known, counts, blocks) for row in self._catalog}
        return known

    @_synchronized
    def update(self, observation: dict, advancements: dict | None = None,
               capabilities: Any = None, advancements_fetched_at: float | None = None) -> dict:
        """Assess one fresh full observation and server envelope; return full board.

        None/invalid/stale advancements marks the controller stale. A cached
        envelope must carry its original fetch time; updated_at is packet time,
        not fetch time. Missing IDs always stay unknown. Confirmations survive outages,
        but stale evidence cannot finish. Explicit server negatives revoke them.
        """
        if isinstance(capabilities, (list, tuple, set, frozenset)) and all(isinstance(x, str) for x in capabilities):
            self._capabilities = set(capabilities) & _KNOWN_SKILLS
        elif isinstance(capabilities, dict) and all(isinstance(k, str) and type(v) is bool for k, v in capabilities.items()):
            self._capabilities = {k for k, v in capabilities.items() if v} & _KNOWN_SKILLS
        elif capabilities is None:
            # The manager advertises only its locally defined registered subset.
            self._capabilities = set(_KNOWN_SKILLS)
        else:
            self._capabilities = None
        known = self._refresh_resources(observation)
        self._completion, self._api = self._read_advancements(advancements)
        observed_at = self._now()
        fetched_at = observed_at if advancements_fetched_at is None else advancements_fetched_at
        if (self._api["ready"] and (not _number(fetched_at) or fetched_at > observed_at
                                    or observed_at - fetched_at > 5)):
            self._completion = {}
            self._api = {"ready": False, "stale": True, "error_code": "advancements_stale"}
        self._explicit_status_ids = set()
        self._server_statuses = {}
        if self._api["ready"]:
            rows = advancements["result"]["advancements"]
            self._server_statuses = {row["id"]: row["status"] for row in rows
                                     if row.get("status") in ("completed", "in_progress", "not_started")}
            self._explicit_status_ids = set(self._server_statuses)
        self._assessed_at = observed_at
        self._fetched_at = fetched_at if self._api["ready"] else None
        if not known:
            self._api = {"ready": False, "stale": True, "error_code": "fresh_observation_required"}
            self._completion = {}
            self._server_statuses = {}
            self._explicit_status_ids = set()
            self._fetched_at = None
        previous = set(self._state["confirmed"])
        confirmed = set(previous)
        for ident, completion in self._completion.items():
            if ident not in self._by_id:
                continue
            if completion == "completed":
                confirmed.add(ident)
            elif completion == "not_completed":
                confirmed.discard(ident)
        self._state["confirmed"] = sorted(confirmed)
        if confirmed - previous:
            self._reset_repeat()
        goal = self._state["active_goal_id"]
        if goal:
            plan = self._plans[goal]
            self._assessment = {
                "resources": {"checked": known, "status": "ready" if plan["availability"] == "ready" else "blocked" if known else "unknown"},
                "capabilities": {"checked": self._capabilities is not None, "status": "unknown" if self._capabilities is None else "blocked" if plan["availability"] == "unsupported" else "ready"},
                "advancement": {"checked": self._api["ready"], "status": self._completion.get(goal, "unknown")},
            }
            if self._remaining(self._now()) == 0:
                self._return_selection("time_budget_exhausted", "forced_exit")
        # Expiry removes only elapsed defers; it never discards the checkpoint.
        now = self._now()
        self._state["deferred"] = {k: v for k, v in self._state["deferred"].items() if v > now}
        return self.snapshot()

    def _remaining(self, now: float) -> float:
        return max(0.0, self.settings["max_seconds"] - max(0.0, now - self._state["started_at"]))

    @staticmethod
    def _is_root(row: dict) -> bool:
        return row.get("is_root") is True or row["id"].endswith("/root")

    def _catalog_assessment(self, now: float) -> dict:
        goals = [row["id"] for row in self._catalog if not self._is_root(row)]
        explicit = {"completed", "not_completed"}
        checked_ids = [ident for ident in goals if ident in self._explicit_status_ids
                       and self._completion.get(ident) in explicit]
        unknown_ids = [ident for ident in goals if ident not in self._explicit_status_ids
                       or self._completion.get(ident) not in explicit]
        fresh = (self._api["ready"] and self._fetched_at is not None
                 and 0 <= now - self._fetched_at <= 5
                 and self._assessed_at is not None and 0 <= now - self._assessed_at <= 5)
        # A successful visible-tree read assesses absent goals as unknown too.
        # Only an unavailable or expired assessment leaves goals unchecked.
        return {"checked": len(goals) if fresh else 0, "total": len(goals),
                "known_count": len(checked_ids), "complete": bool(fresh),
                "unknown_ids": unknown_ids, "fetched_at": self._fetched_at}

    def _assessment_ready(self, now: float) -> bool:
        catalog = self._catalog_assessment(now)
        return (self._state["phase"] == "active" and self._api["ready"]
                and self._fetched_at is not None and 0 <= now - self._fetched_at <= 5
                and self._assessed_at is not None and 0 <= now - self._assessed_at <= 5
                and all(check["checked"] for check in self._assessment.values())
                and catalog["complete"])

    @_synchronized
    def snapshot(self) -> dict:
        """Return a serializable, consistent all-catalog board without mutations."""
        now = self._now()
        confirmed = set(self._state["confirmed"])
        tasks, scored, pending, excluded = [], [], [], []
        for source in self._catalog:
            row = copy.deepcopy(source)
            ident = row["id"]
            plan = self._plans.get(ident, {"availability": "unknown", "stage": "prerequisites",
                "reason": "not_observed", "missing": [], "next_steps": [], "required_skills": []})
            row.update(copy.deepcopy(plan))
            row["scoring_status"] = _ELIGIBILITY[row["eligibility"]]
            row["condition"] = _text(row["condition"], 96)
            row["title"] = _text(row["title"], 96)
            row["completion"] = self._completion.get(ident, "unknown")
            row["assessment"] = self._server_statuses.get(ident, "unknown")
            row["confirmed"] = ident in confirmed
            row["deferred_until"] = self._state["deferred"].get(ident)
            row["selectable"] = (self._state["phase"] == "selection" and self._remaining(now) > 0
                and row["eligibility"] == "scored" and ident not in confirmed
                and row["availability"] in ("ready", "needs_prerequisites")
                and (row["deferred_until"] is None or row["deferred_until"] <= now))
            tasks.append(row)
            if ident in confirmed:
                {"scored": scored, "pending": pending, "excluded": excluded}[row["eligibility"]].append(row)
        score = {"raw": sum(row["points"] for row in scored), "normalized": None,
                 "completed_count": len(scored), "confirmed_count": len(scored),
                 "by_difficulty": {str(level): sum(row["difficulty"] == level for row in scored) for level in range(1, 6)},
                 "conditional_pending": {"confirmed_count": len(pending), "provisional_points": sum(row["points"] for row in pending)},
                 "excluded": {"confirmed_count": len(excluded)}}
        score["difficulty_counts"] = copy.deepcopy(score["by_difficulty"])
        fresh = (self._fetched_at is not None and 0 <= now - self._fetched_at <= 5
                 and self._assessed_at is not None and 0 <= now - self._assessed_at <= 5)
        catalog_assessment = self._catalog_assessment(now)
        active_row = next((row for row in tasks if row["id"] == self._state["active_goal_id"]), None)
        fetched_utc = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._fetched_at))
                       if self._fetched_at is not None else None)
        return {"rules_version": RULES_VERSION, "updated_at": fetched_utc,
                "stale": not self._api["ready"] or not fresh,
                "phase": self._state["phase"], "active_goal_id": self._state["active_goal_id"],
                "availability": active_row["availability"] if active_row else "unknown",
                "assessment": active_row["completion"] if active_row else "unknown",
                "progress_card": {"goal_id": self._state["active_goal_id"], "summary": self._state["summary"],
                    "checks": copy.deepcopy(self._assessment), "assessment_complete": self._assessment_ready(now),
                    "replan_required": self._state["repeat"]["strikes"] > 0,
                    "replan_attempted": self._state["repeat"]["replan_attempted"],
                    "recent_actions": copy.deepcopy(self._state["recent_actions"])},
                "score": score, "tasks": tasks, "remaining_seconds": self._remaining(now),
                "started_at": self._state["started_at"],
                "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._state["started_at"])),
                "budget_exhausted": self._remaining(now) == 0,
                "assessment_complete": self._assessment_ready(now),
                "catalog_assessment": catalog_assessment,
                "advancements": {**self._api, "stale": self._api["stale"] or not fresh},
                "replan_required": self._state["repeat"]["strikes"] > 0,
                "all_deferred": bool(tasks) and all(not row["selectable"] for row in tasks)
                    and any((row["deferred_until"] or 0) > now for row in tasks),
                "last_exit": copy.deepcopy(self._state["last_exit"]),
                "end_reason": self._state["end_reason"]}

    @staticmethod
    def _result(success: bool, message: str, data: dict | None = None,
                error_code: str | None = None) -> dict:
        return {"success": success, "message": message, "data": copy.deepcopy(data or {}), "error_code": error_code}

    @_synchronized
    def select(self, goal_id: str) -> dict:
        """Select an eligible ready/prerequisite task, then require assessment."""
        if self._state["phase"] == "completed":
            return self._result(False, "Competition session is completed", error_code="competition_completed")
        if self._state["phase"] != "selection":
            return self._result(False, "Finish or force-exit the current task first", error_code="task_active")
        if not isinstance(goal_id, str) or goal_id not in self._by_id:
            return self._result(False, "Unknown catalog goal", error_code="unknown_goal")
        if self._remaining(self._now()) == 0:
            return self._result(False, "Competition time budget exhausted; observation remains available", error_code="time_budget_exhausted")
        row = next(row for row in self.snapshot()["tasks"] if row["id"] == goal_id)
        if not row["selectable"]:
            code = ("ineligible_goal" if row["eligibility"] != "scored" else "already_completed" if row["confirmed"]
                    else "goal_deferred" if (row["deferred_until"] or 0) > self._now() else "goal_unavailable")
            return self._result(False, "Goal is not currently selectable", {"goal_id": goal_id, "availability": row["availability"]}, code)
        self._state.update(phase="active", active_goal_id=goal_id, active_since=self._now(), summary="")
        self._state["recent_actions"] = []
        self._assessment = _checks()
        self._reset_repeat()
        return self._result(True, "Task selected; refresh all preset checks before finishing", {"goal_id": goal_id, "phase": "active"})

    @_synchronized
    def update_progress(self, summary: str) -> dict:
        """Replace the bounded progress card; text is never evidence of progress."""
        if self._state["phase"] != "active":
            return self._result(False, "No active task", error_code="no_active_task")
        if not isinstance(summary, str) or not summary.strip():
            return self._result(False, "Progress summary must be nonempty text", error_code="invalid_summary")
        self._state["summary"] = _text(summary, SUMMARY_LIMIT)
        return self._result(True, "Progress card updated", {"progress_card": self.snapshot()["progress_card"]})

    def _return_selection(self, reason: str, outcome: str) -> dict:
        goal, now = self._state["active_goal_id"], self._now()
        if outcome != "completed":
            self._state["deferred"][goal] = now + self.settings["defer_seconds"]
        self._state["last_exit"] = {"goal_id": goal, "reason": _text(reason, REASON_LIMIT), "outcome": outcome, "at": now}
        self._state.update(phase="selection", active_goal_id=None, active_since=None, summary="")
        self._assessment = _checks()
        self._reset_repeat()
        return {"goal_id": goal, "phase": "selection", "returned_selection": True,
                "completed": outcome == "completed", "outcome": outcome,
                "deferred_until": self._state["deferred"].get(goal)}

    @_synchronized
    def finish(self, reason: str) -> dict:
        """Return to selection after fresh ready assessment, without inventing a win."""
        if self._state["phase"] != "active":
            return self._result(False, "No active task", error_code="no_active_task")
        if not isinstance(reason, str) or not reason.strip():
            return self._result(False, "A finish reason is required", error_code="invalid_reason")
        if self._remaining(self._now()) == 0:
            data = self._return_selection("time_budget_exhausted", "forced_exit")
            return self._result(False, "Budget exhausted; task force-exited", data, "time_budget_exhausted")
        now = self._now()
        if not self._assessment_ready(now):
            return self._result(False, "Refresh observation and assess every non-root catalog goal (unknown is allowed)",
                {"catalog_assessment": self._catalog_assessment(now), "checks": copy.deepcopy(self._assessment)},
                "assessment_not_ready")
        goal = self._state["active_goal_id"]
        completed = self._completion.get(goal) == "completed" and goal in self._state["confirmed"]
        data = self._return_selection(reason, "completed" if completed else "finished_without_completion")
        data["assessment_complete"] = True
        return self._result(True, "Server confirmed completion" if completed else "Assessment finished without confirmed completion; no points awarded", data)

    @_synchronized
    def end_competition(self, reason: str) -> dict:
        """End from selection after a fresh catalog assessment; unknown earns no points.

        Missing visible-tree IDs are assessed as unknown. Ending the session does
        not imply advancement completion and never changes confirmed scores.
        """
        if self._state["phase"] == "active":
            return self._result(False, "Finish or force-exit the active task first", error_code="task_active")
        if self._state["phase"] == "completed":
            return self._result(False, "Competition session is already completed", error_code="competition_completed")
        if not isinstance(reason, str) or not reason.strip():
            return self._result(False, "A competition end reason is required", error_code="invalid_reason")
        assessment = self._catalog_assessment(self._now())
        if not assessment["complete"]:
            return self._result(False, "Refresh observation and assess the catalog before ending",
                                {"catalog_assessment": assessment}, "assessment_not_ready")
        end_reason = _text(reason, REASON_LIMIT)
        self._state.update(phase="completed", active_goal_id=None, active_since=None,
                           summary="", end_reason=end_reason)
        return self._result(True, "Competition session completed",
                            {"phase": "completed", "completed_session": True,
                             "reason": end_reason, "assessment_complete": True,
                             "catalog_assessment": assessment})

    @_synchronized
    def force_exit(self, reason: str) -> dict:
        """Return to selection and persist defer; never clear external action locks."""
        if self._state["phase"] != "active":
            return self._result(False, "No active task", error_code="no_active_task")
        if not isinstance(reason, str) or not reason.strip():
            return self._result(False, "An exit reason is required", error_code="invalid_reason")
        return self._result(True, "Task force-exited and deferred; pending side effects remain the caller's responsibility",
                            self._return_selection(reason, "forced_exit"))

    def _reset_repeat(self) -> None:
        self._state["repeat"] = {"signature": None, "count": 0, "strikes": 0, "replan_attempted": False}

    @_synchronized
    def record_action(self, name: str, args: dict, result: dict,
                      observation: dict | None = None) -> dict:
        """Keep bounded status summaries and detect repeated no-progress actions.

        Canonical hashes detect identical name/args/results without persisting
        args, result payloads, messages or reasoning. Every third identical
        attempt requests replan. A second threshold force-exits only after the
        caller explicitly records record_action("replan", {}, {"success": True,
        "data": {"replan_attempted": True}}). No compact is performed or claimed.
        Text summaries and success=True do not reset the detector.
        """
        if self._state["phase"] != "active":
            return self._result(False, "No active task", error_code="no_active_task")
        if not isinstance(name, str) or not name.strip() or not isinstance(args, dict) or not isinstance(result, dict):
            return self._result(False, "Action requires a name and dict args/result", error_code="invalid_action")
        try:
            signature = _digest([name, args, result])
        except (ValueError, TypeError, RecursionError):
            return self._result(False, "Action args/result must be finite JSON data", error_code="invalid_action")
        if observation is not None:
            self._refresh_resources(observation)
        if self._remaining(self._now()) == 0:
            data = self._return_selection("time_budget_exhausted", "forced_exit")
            return self._result(False, "Budget exhausted; task force-exited", data, "time_budget_exhausted")
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        if name == "replan" and result.get("success") is True and data.get("replan_attempted") is True:
            if self._state["repeat"]["strikes"] > 0:
                self._state["repeat"]["replan_attempted"] = True
            return self._result(True, "Explicit replan attempt recorded", {
                "replan_required": self._state["repeat"]["strikes"] > 0,
                "replan_attempted": self._state["repeat"]["replan_attempted"], "forced_exit": False})
        unknown = (result.get("outcome_unknown") is True or data.get("outcome_unknown") is True
                   or result.get("error_code") in ("outcome_unknown", "action_outcome_unknown"))
        self._state["recent_actions"].append({"name": _text(name, 64),
            "success": result.get("success") if type(result.get("success")) is bool else None,
            "error_code": _text(result.get("error_code"), 64), "outcome_unknown": unknown})
        self._state["recent_actions"] = self._state["recent_actions"][-RECENT_LIMIT:]
        repeat = self._state["repeat"]
        repeat["count"] = repeat["count"] + 1 if repeat["signature"] == signature else 1
        repeat["signature"] = signature
        forced = (repeat["count"] >= REPEAT_THRESHOLD and repeat["strikes"] > 0
                  and repeat["replan_attempted"])
        if forced:
            exit_data = self._return_selection("repeated_actions_without_progress", "forced_exit")
            return self._result(True, "Repeated identical actions without progress; task force-exited",
                                {**exit_data, "replan_required": True, "forced_exit": True})
        if repeat["count"] >= REPEAT_THRESHOLD:
            repeat.update(count=0, strikes=1)
        return self._result(True, "Action status recorded", {"replan_required": repeat["strikes"] > 0,
                            "forced_exit": False, "repeat_count": repeat["count"], "outcome_unknown": unknown})
