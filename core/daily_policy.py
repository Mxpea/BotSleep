"""Pure daily safety policy: inspect supplied evidence, never perform I/O.

Food emergencies can release a narrowly restricted model turn. This is not a
fallback resource planner. Shelter routes are current terrain evidence, not a
promise that the backend will follow that path or that a shelter is monster-proof.
"""

import math
import re
import time
from collections.abc import Mapping

from agent.survival_planner import Decision, SurvivalPlanner
from core.domain import FOOD_POINTS, LOG_NAMES, PLANK_NAMES, RANGED_HOSTILES, cell_coords, extract_blocks, parse_pos
from core.local_recovery_runtime import TREE_SUPPORTS, has_canopy_support, verified_footing
from core.policy_relaxation import LOW_HEALTH, NIGHT, allows_atomic, allows_goal
from skills.daily_survival import DailySurvivalSkills
from skills.combat import combat_options


CROP_OUTPUT = {"wheat": (7, "wheat"), "carrots": (7, "carrot"), "potatoes": (7, "potato"), "beetroots": (3, "beetroot")}
FOOD_ACTIONS = frozenset({"harvest_crop", "take_items", "inspect_container", "craft_item", "smelt_collect", "collect_drops", "eat_food", "wait"})
HAZARDS = DailySurvivalSkills.HAZARDS
BOOTSTRAP_ITEMS = frozenset(PLANK_NAMES) | {"stick", "crafting_table", "wooden_pickaxe", "wooden_sword", "wooden_axe"}


class DailySafetyPolicy:
    """Choose local emergency actions and validate restricted food/model turns."""

    def __init__(self, settings=None):
        settings = settings or {}
        if isinstance(settings, Mapping) and isinstance(settings.get("survival"), Mapping):
            settings = settings["survival"]
        self.rules = SurvivalPlanner(settings)
        self.low_health = self.rules.low_health
        self.threat_radius = self.rules.threat_radius
        self.food_threshold = self.rules.food_threshold
        raw_policy = settings.get("sleep_policy", "strict") if isinstance(settings, Mapping) else "strict"
        self.sleep_policy = raw_policy if raw_policy in {"opportunistic", "strict"} else "strict"

    @staticmethod
    def _rows(value):
        return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []

    @staticmethod
    def _wait(reason, goal="safety", seconds=5):
        return Decision("wait", {"seconds": seconds, "reason": reason}, reason, goal)

    @staticmethod
    def _point(value):
        return parse_pos(value)

    @staticmethod
    def _cell(point):
        return cell_coords(point)

    @staticmethod
    def _partial(observation):
        return (observation.get("scope") in {"safety", "partial"}
                or observation.get("observation_scope") == "safety" or observation.get("partial") is True)

    @staticmethod
    def _foods():
        # Never authorize an eat action that the active daily skill rejects.
        return {name: points for name, points in FOOD_POINTS.items() if name in DailySurvivalSkills.FOODS}

    def _basics(self, observation, context=None):
        if (not isinstance(observation, dict) or observation.get("fresh") is not True
                or observation.get("stale") is True or not isinstance(observation.get("status"), dict)):
            return "observation_required", self._wait("A fresh complete observation is required", "observe")
        status = observation["status"]
        health = status.get("health")
        if status.get("alive") is False or self.rules._number(health) and health <= 0:
            return "dead", self._wait("Wait for respawn and fresh inventory", "recover")
        if status.get("alive") is not True or status.get("ready") is not True:
            return "not_ready", self._wait("The bot is not ready and alive", "observe")
        if status.get("busy"):
            return "busy", self._wait("The backend action is still active", "observe", 1)
        if status.get("dimension") in {"the_nether", "minecraft:the_nether"}:
            return "unsafe_dimension", self._wait("Bot is safely in the Nether; hold position without wandering into hazards", "observe", 10)
        if status.get("dimension") not in {"overworld", "minecraft:overworld"}:
            return "unsafe_dimension", self._wait("Daily survival is limited to the overworld")
        if status.get("gamemode") != "survival":
            return "unsafe_mode", self._wait("Daily survival requires survival mode")
        if (self._point(status.get("pos")) is None or not self.rules._number(health) or not 0 < health <= 1024
                or not self.rules._number(status.get("food")) or not 0 <= status["food"] <= 20
                or type(status.get("is_day")) is not bool or type(status.get("thundering")) is not bool
                or not self.rules._number(status.get("time_of_day")) or not 0 <= status["time_of_day"] < 24000):
            return "observation_required", self._wait("Position, life, food and daytime data must be valid", "observe")
        if status.get("in_lava") or status.get("on_fire"):
            return "environment_hazard", self._wait("No environmental rescue route is verified", "recover", 1)
        if status.get("submerged") or status.get("in_water"):
            water_state = (context or {}).get("water_recovery") or {}
            cooled = bool(water_state.get("exhausted")) or (
                type(water_state.get("retry_after")) in (int, float) and water_state["retry_after"] > time.time()
            )
            if status.get("in_water") and not cooled:
                return "water_escape", Decision(
                    "water_escape",
                    {},
                    "Exit water onto freshly checked dry ground or swim upward for air",
                    "safety",
                )
            return "environment_hazard", self._wait("No environmental rescue route is verified", "recover", 1)
        if status.get("in_vehicle"):
            return "vehicle", self._wait("Leave the vehicle before daily tasks", "recover")
        if status.get("on_ground") is not True and not status.get("is_sleeping"):
            return "unstable", self._wait("Wait for stable footing", "recover", 1)
        hostiles = self.rules._dangerous_hostiles(observation, status["pos"])
        if hostiles:
            combat = combat_options(observation)
            ranged = any(row.get("name") in RANGED_HOSTILES for _, _, row in hostiles)
            if combat.get("allowed") is True and (not ranged or combat.get("shield") is True):
                target = combat["target"]
                return "combat", Decision("fight_hostile", {"entity_id": target["id"], "expected_name": target["name"]},
                    "Take one locally verified defensive step, then observe again", "safety")
            return "near_hostile", Decision("retreat_from", {"pos": hostiles[0][1], "distance": 8},
                "A hostile is within its danger radius; withdraw through freshly checked terrain instead of waiting exposed", "safety")
        nearby = status.get("nearest_hostile")
        if isinstance(nearby, dict) and self.rules._number(nearby.get("dist")) and nearby["dist"] <= self.rules._threat_distance(nearby.get("name")):
            return "hostile_position_unknown", self._wait("Nearby hostile has no fresh position for a safe retreat")
        if status.get("is_sleeping"):
            return "sleeping", Decision("auto_sleep", {}, "Observe the existing sleep through the verified sleep skill", "sleep")
        return "normal", None

    def _meal(self, observation):
        status = observation["status"]
        counts = self.rules._counts(observation)
        foods = self._foods()
        available = [name for name in foods if counts.get(name, 0) > 0]
        if status["food"] >= 20 or not available or (status["health"] > self.low_health and status["food"] > self.food_threshold):
            return None
        deficit = 20 - status["food"]
        fitting = [name for name in available if foods[name] <= deficit]
        item = min(fitting, key=lambda name: (-foods[name], name)) if fitting else min(available, key=lambda name: (foods[name], name))
        return Decision("eat_food", {"item": item}, "Eat an existing safe food before further work", "food")

    def _night(self, status):
        return not status["is_day"] or status["thundering"] or status["time_of_day"] >= 11000

    def _strict_night_block(self, observation, status):
        """Gate on night/thunder strictly unless sleep_policy='opportunistic' allows working."""
        if self.sleep_policy != "opportunistic":
            return self._night(status)
        if not status["is_day"] or status["time_of_day"] >= 11000:
            shelter = self._shelter(observation)
            # Opportunistic policy only sleeps or waits in shelter when a nearby verified bed, short shelter route, or verified inside stance exists.
            return bool(self._bed(observation) or self._return_home(observation)
                        or (isinstance(shelter, dict) and shelter.get("inside") is True))
        return False

    _footing = staticmethod(verified_footing)
    _canopy = staticmethod(has_canopy_support)

    def _canopy_decision(self):
        return Decision("canopy_escape", {}, "Current support remains part of the canopy evacuation; take one checked escape step and observe again", "safety")

    def _food_search(self, observation, context=None):
        """Suggest one short checked ground move, never a remembered route.

        The harness owns the eight-step budget and prunes expired exclusions.
        position_for_task still verifies the entire corridor before any write.
        """
        status, context = observation["status"], context or {}
        recovery = context.get("food_recovery") or {}
        steps = recovery.get("steps", 0)
        if (type(steps) is not int or not 0 <= steps < 8 or recovery.get("needs_ai") is True
                or recovery.get("cooling_down") is True or self._night(status)
                or len(self._rows(observation.get("entities"))) >= 40):
            return None
        footing = self._footing(observation)
        if (footing is None or footing.get("name") not in DailySurvivalSkills.SUPPORT
                or footing.get("name") in TREE_SUPPORTS):
            return None
        origin = status["pos"]
        source = self._point(observation.get("local_geometry_origin"))
        if source is None or self._cell(source) != self._cell(origin) or math.dist(source, origin) > .2:
            return None
        visited = set()
        for value in (recovery.get("visited") or [])[-8:]:
            pos = self._point(value)
            if pos is not None:
                visited.add(self._cell(pos))
        excluded = set(recovery.get("excluded_until") or {}) | self.rules._blocked(context)
        candidates = []
        for row in self._rows(observation.get("safe_stand_candidates"))[:8]:
            pos = self._point(row.get("pos"))
            if (pos is None or row.get("support") not in DailySurvivalSkills.SUPPORT
                    or row.get("support") in TREE_SUPPORTS
                    or abs(pos[1] - origin[1]) > .1 or self._cell(pos) == self._cell(origin)
                    or self._cell(pos) in visited or self.rules._key(pos) in excluded
                    or not self._near_safe(pos, observation, 3)):
                continue
            candidates.append(pos)
        if not candidates:
            return None
        target = min(candidates, key=lambda pos: (math.dist(origin, pos), pos))
        return Decision("position_for_task", {"pos": target},
                        "Food below 18 cannot regenerate health; inspect one fresh ground position within the bounded food search", "food_recovery")

    def _bootstrap_ready(self, observation, context=None):
        status, context = observation["status"], context or {}
        recovery = context.get("food_recovery") or {}
        footing = self._footing(observation)
        return (recovery.get("needs_ai") is True and 4 < status["health"] <= self.low_health
                and 12 <= status["food"] < 18 and not self._night(status)
                and footing is not None and footing.get("name") in DailySurvivalSkills.SUPPORT
                and footing.get("name") not in TREE_SUPPORTS
                and len(self._rows(observation.get("entities"))) < 40
                and not any(distance <= max(16, self.threat_radius)
                            for distance, _ in self.rules._hostiles(observation, status["pos"]))
                and not (isinstance(status.get("nearest_hostile"), dict)
                         and self.rules._number(status["nearest_hostile"].get("dist"))
                         and status["nearest_hostile"]["dist"] <= max(16, self.threat_radius)))

    def cautious_bootstrap(self, observation, context=None):
        """Shared pure gate for goal admission and each limited atomic child."""
        _, forced = self._basics(observation, context)
        return forced is None and self._meal(observation) is None and self._bootstrap_ready(observation, context)

    def _bootstrap_valid(self, decision, observation, context=None):
        if not self._bootstrap_ready(observation, context):
            return False
        name, args = decision.name, decision.args
        count = args.get("count", 8 if name == "run_survival_task" else 1)
        if type(count) is not int or not 1 <= count <= 8:
            return False
        if name == "run_survival_task":
            return ((args.get("task") == "gather" and args.get("resource") == "logs" and "item" not in args)
                    or (args.get("task") == "craft" and args.get("item") in BOOTSTRAP_ITEMS and "resource" not in args))
        # This bit is supplied by the harness for a previously accepted limited
        # goal. A model cannot acquire atomic-production authority from its args.
        if (context or {}).get("cautious_bootstrap") is not True:
            return False
        if name == "craft_item":
            item = args.get("item")
            if item not in BOOTSTRAP_ITEMS:
                return False
            if item in {"wooden_pickaxe", "wooden_sword", "wooden_axe"}:
                return any(row["name"] == "crafting_table" and math.dist(row["pos"], observation["status"]["pos"]) <= 3
                           for row in self._blocks(observation))
            return True
        pos = self._point(args.get("pos"))
        origin = observation["status"]["pos"]
        if pos is None or not self._near_safe(pos, observation, 3):
            return False
        if name == "gather_block":
            if args.get("expected_name") not in LOG_NAMES or (self._cell(pos)[0], self._cell(pos)[2]) == (self._cell(origin)[0], self._cell(origin)[2]):
                return False
            return any(row.get("name") == args["expected_name"] and self._point(row.get("pos")) == pos
                       and row.get("can_see") is True and row.get("can_dig") is True
                       for row in self._rows(observation.get("blocks")))
        if name == "place_item":
            return (args.get("item") == "crafting_table"
                    and any(self._point(row.get("pos")) == pos for row in self._rows(observation.get("placement_candidates")))
                    and self._point(observation.get("local_geometry_origin")) is not None
                    and math.dist(observation["local_geometry_origin"], origin) <= .2)
        return False

    @staticmethod
    def _drop_item(entity):
        if entity.get("type") != "item" or entity.get("name") != "item" or type(entity.get("id")) is not int:
            return None
        raw = entity.get("item")
        match = re.fullmatch(r"([a-z][a-z0-9_]*)x[1-9][0-9]*", raw) if isinstance(raw, str) else None
        return match.group(1) if match else None

    def _blocks(self, observation):
        return extract_blocks(observation, include_terrain=True)

    def _near_safe(self, pos, observation, radius, *, complete=False):
        status = observation["status"]
        pos = self._point(pos)
        if pos is None or math.dist(status["pos"], pos) > radius:
            return False
        blocks = self._blocks(observation)
        for row in blocks:
            waterlogged = (row.get("properties") or {}).get("waterlogged") in (True, "true")
            if (row["name"] in HAZARDS or waterlogged) and math.dist(row["pos"], pos) <= 1.5:
                return False
        if self.rules._dangerous_hostiles(observation, pos):
            return False
        if complete:
            # The low-health exception needs a complete local six-neighbour
            # observation, rather than interpreting missing hazard rows as air.
            by_pos = {self._cell(row["pos"]): row for row in blocks}
            x, y, z = self._cell(pos)
            cells = [(x, y, z), (x - 1, y, z), (x + 1, y, z), (x, y - 1, z),
                     (x, y + 1, z), (x, y, z - 1), (x, y, z + 1)]
            if any(cell not in by_pos for cell in cells):
                return False
        return True

    @staticmethod
    def _fresh_row(row, dimension):
        return isinstance(row, dict) and row.get("fresh") is True and row.get("verified") is True and row.get("dimension") == dimension

    def _food_valid(self, decision, observation, *, low_health=False):
        name, args = decision.name, decision.args
        radius = 2 if low_health else 4
        status = observation["status"]
        counts, foods = self.rules._counts(observation), self._foods()
        if name == "wait":
            return True
        if name == "eat_food":
            return args.get("item") in foods and counts.get(args.get("item"), 0) > 0 and status["food"] < 20
        if self._partial(observation):
            return False
        if name not in FOOD_ACTIONS:
            return False
        if name == "collect_drops":
            entity = next((row for row in self._rows(observation.get("entities"))
                           if type(args.get("entity_id")) is int and row.get("id") == args["entity_id"]), None)
            if entity is None or self._drop_item(entity) not in foods or self._drop_item(entity) != args.get("expected_item"):
                return False
            pos = self._point(entity.get("pos"))
            if pos is None or not self._near_safe(pos, observation, min(radius, 2), complete=True):
                return False
            start, end = self._cell(status["pos"]), self._cell(pos)
            if start[1] != end[1] or abs(start[0] - end[0]) + abs(start[2] - end[2]) > 1:
                return False
            blocks = {self._cell(row["pos"]): row for row in self._blocks(observation)}
            for x, y, z in {start, end}:
                floor = blocks.get((x, y - 1, z), {})
                if floor.get("name") not in DailySurvivalSkills.SUPPORT or not DailySurvivalSkills._safe_block(floor):
                    return False
                for dy in (0, 1):
                    body = blocks.get((x, y + dy, z), {})
                    if body.get("name") not in DailySurvivalSkills.RETREAT_PASSABLE or not DailySurvivalSkills._safe_block(body):
                        return False
            return True
        if name == "craft_item":
            count = args.get("count", 1)
            return (args.get("item") == "bread" and type(count) is int and 1 <= count <= 64
                    and counts.get("wheat", 0) >= count * 3
                    and any(row["name"] == "crafting_table" and self._near_safe(row["pos"], observation, radius, complete=low_health)
                            for row in self._blocks(observation)))
        pos = self._point(args.get("pos"))
        if pos is None or not self._near_safe(pos, observation, radius, complete=low_health):
            return False
        blocks = [row for row in self._blocks(observation) if self._cell(row["pos"]) == self._cell(pos)]
        if name == "harvest_crop":
            crop = args.get("expected_name")
            if crop not in CROP_OUTPUT or low_health and CROP_OUTPUT[crop][1] not in foods:
                return False
            for row in blocks:
                age = (row.get("properties") or {}).get("age")
                if row["name"] == crop and (row.get("mature") is True or type(age) is int and age >= CROP_OUTPUT[crop][0]):
                    return row.get("can_see") is not False
            return False
        if name == "inspect_container":
            # An empty inspection cannot feed a low-health bot; do not turn its
            # emergency into unbounded container prospecting.
            return not low_health and any(row["name"] in {"chest", "barrel", "furnace", "smoker", "blast_furnace"} for row in blocks)
        item, count = args.get("item"), args.get("count")
        if item not in foods or type(count) is not int or not 1 <= count <= 64:
            return False
        rows = self._rows(observation.get("containers" if name == "take_items" else "furnaces"))
        for row in rows:
            row_pos = self._point(row.get("pos"))
            if not self._fresh_row(row, status["dimension"]) or row_pos is None or self._cell(row_pos) != self._cell(pos):
                continue
            if name == "take_items":
                return row.get("name") in {"chest", "barrel"} and self.rules._counts({"inventory": row.get("items")}).get(item, 0) >= count
            output = row.get("output") or {}
            return output.get("name") == item and type(output.get("count")) is int and output["count"] >= count
        return False

    def _food_options(self, observation, *, low_health=False, task_board=None):
        candidates = []
        if isinstance(task_board, dict):
            for row in self._rows(task_board.get("tasks")):
                action = row.get("next_action")
                if isinstance(action, dict) and isinstance(action.get("name"), str) and isinstance(action.get("args"), dict):
                    candidates.append(Decision(action["name"], action["args"], "Current food candidate", "food"))
        for row in self._blocks(observation):
            if row["name"] in CROP_OUTPUT:
                candidates.append(Decision("harvest_crop", {"pos": row["pos"], "expected_name": row["name"]}, "Current crop", "food"))
            if row["name"] in {"chest", "barrel", "furnace", "smoker", "blast_furnace"}:
                candidates.append(Decision("inspect_container", {"pos": row["pos"]}, "Current nearby container", "food"))
        if self.rules._counts(observation).get("wheat", 0) >= 3:
            candidates.append(Decision("craft_item", {"item": "bread", "count": 1}, "Held wheat", "food"))
        for row in self._rows(observation.get("containers")):
            for item in self.rules._counts({"inventory": row.get("items")}):
                candidates.append(Decision("take_items", {"pos": row.get("pos"), "item": item, "count": 1}, "Current stored food", "food"))
        for row in self._rows(observation.get("furnaces")):
            output = row.get("output") or {}
            candidates.append(Decision("smelt_collect", {"pos": row.get("pos"), "item": output.get("name"), "count": 1}, "Current cooked output", "food"))
        for row in self._rows(observation.get("entities")):
            item = self._drop_item(row)
            if item in self._foods():
                candidates.append(Decision("collect_drops", {"entity_id": row["id"], "expected_item": item}, "Current nearby food drop", "food"))
        return [decision for decision in candidates if self._food_valid(decision, observation, low_health=low_health)]

    def _shelter(self, observation):
        evidence = observation.get("daily_evidence")
        row = evidence.get("shelter") if isinstance(evidence, dict) else None
        status = observation["status"]
        if not self._fresh_row(row, status["dimension"]) or self._point(row.get("pos")) is None:
            return None
        geometry = row.get("geometry") if isinstance(row.get("geometry"), dict) else {}
        structure = row.get("structure_complete", geometry.get("structure_complete", row.get("geometry_complete")))
        if structure is not True:
            return None
        return row

    def _door(self, shelter, observation):
        door = shelter.get("door")
        if not isinstance(door, dict):
            return None
        orientation = str(shelter.get("orientation") or "north").lower()
        allowed_facings = {"east", "west"} if orientation in {"east", "west"} else {"north", "south"}
        pos = self._point(door.get("pos"))
        if (pos is None or any(value != math.floor(value) for value in pos)
                or door.get("name") not in DailySurvivalSkills.DOORS
                or door.get("frame_verified") is not True or type(door.get("open")) is not bool
                or door.get("facing") not in allowed_facings):
            return None
        return door

    def _return_home(self, observation):
        shelter = self._shelter(observation)
        if shelter is None:
            return None
        door = self._door(shelter, observation)
        if door is None and ("door" in shelter or shelter.get("geometry_complete") is not True):
            return None
        if shelter.get("inside") is True:
            if door is None or not door["open"]:
                return None
            if self._near_safe(door["pos"], observation, 3):
                return Decision("set_door", {"pos": door["pos"], "open": False},
                                "Close the currently verified shelter door, then reobserve the interior", "safety")
            # A far interior corner is not interaction range. Only a freshly
            # checked short interior route may reposition before closing.
        elif shelter.get("inside") is not False:
            return None
        route = shelter.get("return_route")
        status = observation["status"]
        if (not isinstance(route, dict) or route.get("fresh") is not True
                or route.get("dimension") != status["dimension"]):
            return None
        blocked_door = route.get("blocked_door")
        door_blocked = (route.get("verified") is False and isinstance(blocked_door, dict)
                        and route.get("all_other_body_and_support_verified") is True
                        and blocked_door.get("verified") is True and blocked_door.get("open") is False
                        and door is not None and door["open"] is False
                        and blocked_door.get("pos") == door["pos"]
                        and self._near_safe(door["pos"], observation, 3))
        if route.get("verified") is not True and not door_blocked:
            return None
        # A closed known door contradicts an otherwise clear walking route.
        if not door_blocked and isinstance(shelter.get("door"), dict) and shelter["door"].get("open") is not True:
            return None
        origin = self._point(route.get("origin"))
        path = route.get("path")
        if (origin is None or math.dist(origin, status["pos"]) > .2 or self._cell(origin) != self._cell(status["pos"])
                or not isinstance(path, list) or not 1 <= len(path) <= 4):
            return None
        previous = self._cell(origin)
        seen = {previous}
        for raw in path:
            point = self._point(raw)
            if point is None or any(value != math.floor(value) for value in point):
                return None
            cell = self._cell(point)
            if cell in seen or cell[1] != previous[1] or abs(cell[0] - previous[0]) + abs(cell[2] - previous[2]) != 1:
                return None
            if not self._near_safe(point, observation, 5):
                return None
            seen.add(cell)
            previous = cell
        center = self._cell(shelter["pos"])
        if previous[1] != center[1] or abs(previous[0] - center[0]) > 1 or abs(previous[2] - center[2]) > 1:
            return None
        if door_blocked:
            if self._cell(door["pos"]) not in {self._cell(point) for point in path}:
                return None
            return Decision("set_door", {"pos": door["pos"], "open": True},
                            "Open the sole verified obstruction on the short shelter route; reobserve before moving", "safety")
        target = [previous[0] + .5, previous[1], previous[2] + .5]
        if math.dist(status["pos"], target) <= 1.25:
            return None
        return Decision("move_to", {"pos": target, "range": .5},
                        "Return along fresh short terrain evidence; actual path and arrival require re-observation", "safety")

    def _bed(self, observation):
        return any(row["name"].endswith("_bed") and self._near_safe(row["pos"], observation, 16)
                   and (row.get("properties") or {}).get("occupied") not in (True, "true") for row in self._blocks(observation))

    def constraints(self, observation, context=None):
        scenario, forced = self._basics(observation, context)
        allowed, radius, model = None, 32, forced is None
        reason = forced.reason if forced else "Normal bounded daily planning remains subject to local skill verification"
        if forced is not None:
            allowed, radius = sorted({"wait", forced.name}), 0
        else:
            status = observation["status"]
            if self._meal(observation):
                scenario, allowed, radius, model = "food_available", ["eat_food", "wait"], 0, False
                reason = "Existing safe food has priority"
            elif (status["health"] <= self.low_health and status["food"] < 18
                  and (not self._night(status) or self.sleep_policy == "opportunistic")
                  and self._food_options(observation, low_health=True)):
                scenario, allowed, radius, model = "low_health", sorted(FOOD_ACTIONS - {"inspect_container"}), 2, False
                reason = "Perform one directly verified nearby food action locally, then observe inventory"
            elif self._canopy(observation, context):
                scenario, allowed, radius, model = "canopy_escape", ["canopy_escape", "wait"], 1, False
                reason = "Canopy support requires one local verified escape step before production or food search"
            elif status["health"] <= self.low_health:
                scenario, allowed, radius, model = "low_health", ["wait"], 0, False
                reason = ("Food is below 18: natural regeneration cannot restore health; seek only verified nearby food"
                          if status["food"] < 18 else "Food permits natural regeneration; observe recovery before production")
                if self._bootstrap_ready(observation, context):
                    scenario, allowed, radius, model = "cautious_bootstrap", ["run_survival_task", "wait"], 3, True
                    reason = "Food search is exhausted; only a bounded nearby log or wooden-tool goal may be proposed"
                elif status["food"] < 18:
                    search = self._food_search(observation, context)
                    scenario = "food_recovery" if search else "food_recovery_blocked"
                    allowed, radius = (["position_for_task", "wait"], 3) if search else (["wait"], 0)
                    reason = search.reason if search else "Food recovery is blocked; food below 18 cannot regenerate health and no fresh safe search step remains"
                    if self._partial(observation):
                        scenario, reason = "food_observation_required", "A complete fresh food and ground observation is required; partial safety data cannot prove search exhaustion"
            elif self._strict_night_block(observation, status):
                scenario, allowed, radius, model = "shelter_time", ["wait", "auto_sleep", "move_to", "set_door"], 4, False
                reason = "Dusk/night permits verified sleep or separately verified door and short shelter-route steps"
            elif status["food"] <= 6:
                scenario, allowed, radius, model = "critical_hunger", sorted(FOOD_ACTIONS), 4, True
                reason = "Only nearby observed food acquisition is allowed; no hunting, mining, building or exploration"
        result = {"scenario": scenario, "model_allowed": model, "allowed_actions": allowed,
                "max_action_distance": radius, "reason": reason,
                "route_evidence_is_actual_trajectory": False, "shelter_spawn_safety": "unknown"}
        if scenario == "cautious_bootstrap":
            result.update(allowed_task_resources=["logs"], allowed_craft_items=sorted(BOOTSTRAP_ITEMS), max_task_count=8)
        return result

    def choose(self, observation, baseline_decision, task_board=None, context=None):
        scenario, forced = self._basics(observation, context)
        if forced is not None:
            # Preserve the harness's bounded retreat search cooldown instead of
            # recreating the same unsafe route every observation.
            if scenario == "near_hostile" and isinstance(baseline_decision, Decision) and baseline_decision.goal == "safety" and baseline_decision.name in {"wait", "retreat_from"}:
                return baseline_decision
            return forced
        if isinstance(baseline_decision, Decision) and baseline_decision.name in {"retreat_from", "canopy_escape"}:
            return baseline_decision  # Continue an already verified canopy relay.
        if isinstance(baseline_decision, Decision) and baseline_decision.name == "wait" and baseline_decision.goal == "safety":
            return baseline_decision
        meal = self._meal(observation)
        if meal:
            return meal
        status = observation["status"]
        if status["health"] <= self.low_health and status["food"] < 18 and (not self._night(status) or self.sleep_policy == "opportunistic"):
            foods = self._food_options(observation, low_health=True, task_board=task_board)
            if foods:
                return foods[0]
        if self._canopy(observation, context):
            return self._canopy_decision()
        if status["health"] <= self.low_health:
            if self._partial(observation) and status["food"] < 18:
                return self._wait("Collect a complete fresh food and ground observation before resuming bounded recovery", "observe")
            if self._bootstrap_ready(observation, context):
                return None
            if status["food"] < 18:
                search = self._food_search(observation, context)
                if search:
                    return search
            return self._wait("Food recovery is blocked: food is below 18 and no currently verified safe food is available; waiting cannot regenerate health"
                              if status["food"] < 18 else "Observe natural regeneration without hunting or gathering", "recover")
        if self._strict_night_block(observation, status):
            home = self._return_home(observation)
            if home:
                return home
            if (not status["is_day"] or status["thundering"]) and self._bed(observation):
                return Decision("auto_sleep", {}, "Use a currently observed free bed through the verified sleep skill", "sleep")
            shelter = self._shelter(observation)
            return self._wait("Remain in the freshly verified shelter geometry; spawn protection is not proven" if shelter and shelter.get("inside") is True
                              else "Dusk/night has no fresh verified short shelter route or usable bed", "sleep")
        # Critical hunger intentionally releases only the constrained food turn.
        # A baseline resource action is never executed here as model fallback.
        return None

    def validate(self, decision, observation, context=None):
        if not isinstance(decision, Decision) or not isinstance(decision.args, dict):
            return "invalid_decision"
        if decision.name == "wait":
            return None
        scenario, forced = self._basics(observation, context)
        if forced is not None:
            if forced.name == "fight_hostile" and decision.name == forced.name and decision.args == forced.args:
                return None
            if forced.name == "retreat_from" and decision.name == forced.name and self._point(decision.args.get("pos")) == forced.args["pos"]:
                return None
            if forced.name in {"auto_sleep", "water_escape"} and decision.name == forced.name and (forced.name != "water_escape" or not decision.args):
                return None
            return scenario
        status = observation["status"]
        if decision.name == "eat_food":
            return None if self._food_valid(decision, observation) else "food_not_available_or_unsafe"
        if self._meal(observation):
            return "existing_food_has_priority"
        if (status["health"] <= self.low_health and status["food"] < 18
                and (not self._night(status) or self.sleep_policy == "opportunistic")
                and self._food_valid(decision, observation, low_health=True)):
            return None
        if self._canopy(observation, context):
            return None if decision.name == "canopy_escape" and not decision.args else "canopy_escape_required"
        if decision.name == "canopy_escape":
            return "canopy_support_unverified"
        if status["health"] <= self.low_health:
            if (status["food"] < 18 and (not self._night(status) or self.sleep_policy == "opportunistic")
                    and self._food_valid(decision, observation, low_health=True)):
                return None
            search = self._food_search(observation, context) if status["food"] < 18 else None
            if search and decision.name == search.name and decision.args == search.args:
                return None
            if self._bootstrap_valid(decision, observation, context):
                return None
            return "low_health_food_only"
        if self._strict_night_block(observation, status):
            home = self._return_home(observation)
            if home is not None:
                return None if decision.name == home.name and decision.args == home.args else "night_requires_verified_sleep_or_shelter_route"
            if decision.name == "auto_sleep" and (not status["is_day"] or status["thundering"]) and self._bed(observation):
                return None
            return "night_requires_verified_sleep_or_shelter_route"
        if status["food"] <= 6 and not self._food_valid(decision, observation):
            return "critical_hunger_nearby_food_only"
        if decision.name == "fight_hostile":
            combat = combat_options(observation, decision.args.get("entity_id"), decision.args.get("expected_name"))
            return None if combat.get("allowed") is True else combat.get("reason", "combat_not_verified")
        return None

    def soft_hold(self, safety_decision, observation, context=None):
        """Identify a relaxation candidate only when local safety would just wait."""
        if not isinstance(safety_decision, Decision) or safety_decision.name != "wait":
            return None
        if not self.relaxation_stance(observation, context):
            return None
        status = observation["status"]
        if safety_decision.goal == "sleep" and self._strict_night_block(observation, status):
            return NIGHT if status["health"] > self.low_health else None
        if (safety_decision.goal == "recover" and status["health"] <= self.low_health
                and status["food"] < 18 and not self._night(status)
                and not self._food_options(observation, low_health=True)
                and self._food_search(observation, context) is None):
            return LOW_HEALTH
        return None

    def relaxation_stance(self, observation, context=None):
        """Full observation is a prerequisite, not an execution authorization."""
        context = context or {}
        if (not isinstance(observation, dict) or self._partial(observation) or context.get("pending")
                or context.get("cautious_bootstrap") or (context.get("retreat_recovery") or {}).get("evacuating")
                or context.get("combat_retreat") or observation.get("inventory_fresh") is False
                or observation.get("terrain_fresh") is False or self._basics(observation, context)[1] is not None):
            return False
        status = observation["status"]
        footing = self._footing(observation)
        entities = observation.get("entities")
        return (status.get("on_ground") is True and status.get("thundering") is False
                and "busy" in status and status["busy"] in (None, False)
                and status["health"] > 4 and status["food"] > 6 and self._meal(observation) is None
                and footing is not None and footing["name"] in DailySurvivalSkills.SUPPORT - TREE_SUPPORTS
                and isinstance(entities, list) and len(entities) < 40)

    def allows_relaxed(self, decision, mode, observation):
        """Match the original intent, without authorizing an implicit approach."""
        if decision.name == "run_survival_task":
            return allows_goal(decision.args, mode)
        if not allows_atomic(decision.name, decision.args, mode):
            return False
        if decision.name == "craft_item":
            return True
        target = self._point(decision.args.get("pos"))
        origin = observation["status"]["pos"]
        return (target is not None and math.dist(origin, target) <= 2.8
                and self._cell(target)[0::2] != self._cell(origin)[0::2]
                and any(row["pos"] == target and row["name"] == decision.args["expected_name"]
                        and row.get("can_see") is True and row.get("can_dig", row.get("harvest_tool_ok")) is True
                        for row in self.rules._blocks(observation)))
