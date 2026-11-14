"""Pure daily safety policy: inspect supplied evidence, never perform I/O.

Food emergencies can release a narrowly restricted model turn. This is not a
fallback resource planner. Shelter routes are current terrain evidence, not a
promise that the backend will follow that path or that a shelter is monster-proof.
"""

import math
from collections.abc import Mapping

from agent.survival_planner import Decision, SurvivalPlanner
from skills.daily_survival import DailySurvivalSkills


FOOD_POINTS = {
    "cooked_beef": 8, "cooked_porkchop": 8, "cooked_mutton": 6, "cooked_chicken": 6,
    "cooked_rabbit": 5, "cooked_salmon": 6, "cooked_cod": 5, "bread": 5, "baked_potato": 5,
    "golden_carrot": 6, "apple": 4, "carrot": 3, "beetroot": 1, "melon_slice": 2,
    "sweet_berries": 2, "glow_berries": 2, "dried_kelp": 1, "cookie": 2, "pumpkin_pie": 8,
    "mushroom_stew": 6, "rabbit_stew": 10, "beetroot_soup": 6,
    "beef": 3, "porkchop": 3, "rabbit": 3, "mutton": 2, "cod": 2, "salmon": 2, "potato": 1,
}
CROP_OUTPUT = {"wheat": (7, "wheat"), "carrots": (7, "carrot"), "potatoes": (7, "potato"), "beetroots": (3, "beetroot")}
FOOD_ACTIONS = frozenset({"harvest_crop", "take_items", "inspect_container", "craft_item", "smelt_collect", "eat_food", "wait"})
HAZARDS = DailySurvivalSkills.HAZARDS


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

    @staticmethod
    def _rows(value):
        return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []

    @staticmethod
    def _wait(reason, goal="safety", seconds=5):
        return Decision("wait", {"seconds": seconds, "reason": reason}, reason, goal)

    @staticmethod
    def _point(value):
        return SurvivalPlanner._pos(value)

    @staticmethod
    def _cell(point):
        return tuple(math.floor(value + .0001) for value in point)

    @staticmethod
    def _foods():
        # Never authorize an eat action that the active daily skill rejects.
        return {name: points for name, points in FOOD_POINTS.items() if name in DailySurvivalSkills.FOODS}

    def _basics(self, observation):
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
        if status.get("dimension") not in {"overworld", "minecraft:overworld"}:
            return "unsafe_dimension", self._wait("Daily survival is limited to the overworld")
        if status.get("gamemode") != "survival":
            return "unsafe_mode", self._wait("Daily survival requires survival mode")
        if (self._point(status.get("pos")) is None or not self.rules._number(health) or not 0 < health <= 1024
                or not self.rules._number(status.get("food")) or not 0 <= status["food"] <= 20
                or type(status.get("is_day")) is not bool or type(status.get("thundering")) is not bool
                or not self.rules._number(status.get("time_of_day")) or not 0 <= status["time_of_day"] < 24000):
            return "observation_required", self._wait("Position, life, food and daytime data must be valid", "observe")
        if any(status.get(key) for key in ("in_lava", "on_fire", "submerged", "in_water")):
            return "environment_hazard", self._wait("No environmental rescue route is verified", "recover", 1)
        if status.get("in_vehicle"):
            return "vehicle", self._wait("Leave the vehicle before daily tasks", "recover")
        if status.get("on_ground") is not True and not status.get("is_sleeping"):
            return "unstable", self._wait("Wait for stable footing", "recover", 1)
        hostiles = self.rules._hostiles(observation, status["pos"])
        if hostiles and hostiles[0][0] <= self.threat_radius:
            return "near_hostile", Decision("retreat_from", {"pos": hostiles[0][1], "distance": 8},
                "A hostile is nearby; the retreat skill must verify terrain", "safety")
        nearby = status.get("nearest_hostile")
        if isinstance(nearby, dict) and self.rules._number(nearby.get("dist")) and nearby["dist"] <= self.threat_radius:
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

    def _blocks(self, observation):
        return [row for row in self._rows(observation.get("blocks")) + self._rows(observation.get("terrain"))
                if isinstance(row.get("name"), str) and self._point(row.get("pos")) is not None]

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
        if any(math.dist(pos, threat) <= self.threat_radius for _, threat in self.rules._hostiles(observation, status["pos"])):
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
        if name not in FOOD_ACTIONS:
            return False
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
        pos = self._point(door.get("pos"))
        if (pos is None or any(value != math.floor(value) for value in pos)
                or door.get("name") not in DailySurvivalSkills.DOORS
                or door.get("frame_verified") is not True or type(door.get("open")) is not bool
                or door.get("facing") not in {"north", "south"}):
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

    def constraints(self, observation):
        scenario, forced = self._basics(observation)
        allowed, radius, model = None, 32, forced is None
        reason = forced.reason if forced else "Normal bounded daily planning remains subject to local skill verification"
        if forced is not None:
            allowed, radius = sorted({"wait", forced.name}), 0
        else:
            status = observation["status"]
            if self._meal(observation):
                scenario, allowed, radius, model = "food_available", ["eat_food", "wait"], 0, False
                reason = "Existing safe food has priority"
            elif status["health"] <= self.low_health:
                can_feed = status["food"] < 18 and bool(self._food_options(observation, low_health=True)) and not self._night(status)
                scenario, allowed, radius, model = "low_health", sorted(FOOD_ACTIONS - {"inspect_container"}), 2, can_feed
                reason = "Eat or regenerate; only directly verified nearby food can justify a restricted model turn"
            elif self._night(status):
                scenario, allowed, radius, model = "shelter_time", ["wait", "auto_sleep", "move_to", "set_door"], 4, False
                reason = "Dusk/night permits verified sleep or separately verified door and short shelter-route steps"
            elif status["food"] <= 6:
                scenario, allowed, radius, model = "critical_hunger", sorted(FOOD_ACTIONS), 4, True
                reason = "Only nearby observed food acquisition is allowed; no hunting, mining, building or exploration"
        return {"scenario": scenario, "model_allowed": model, "allowed_actions": allowed,
                "max_action_distance": radius, "reason": reason,
                "route_evidence_is_actual_trajectory": False, "shelter_spawn_safety": "unknown"}

    def choose(self, observation, baseline_decision, task_board=None):
        scenario, forced = self._basics(observation)
        if forced is not None:
            # Preserve the harness's bounded retreat search cooldown instead of
            # recreating the same unsafe route every observation.
            if scenario == "near_hostile" and isinstance(baseline_decision, Decision) and baseline_decision.goal == "safety" and baseline_decision.name in {"wait", "retreat_from"}:
                return baseline_decision
            return forced
        if isinstance(baseline_decision, Decision) and baseline_decision.name == "retreat_from":
            return baseline_decision  # Continue an already verified canopy relay.
        if isinstance(baseline_decision, Decision) and baseline_decision.name == "wait" and baseline_decision.goal == "safety":
            return baseline_decision
        meal = self._meal(observation)
        if meal:
            return meal
        status = observation["status"]
        if status["health"] <= self.low_health:
            if status["food"] < 18 and not self._night(status) and self._food_options(observation, low_health=True, task_board=task_board):
                return None
            return self._wait("Health is low; eat available food or wait for regeneration without hunting", "recover")
        if self._night(status):
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

    def validate(self, decision, observation):
        if not isinstance(decision, Decision) or not isinstance(decision.args, dict):
            return "invalid_decision"
        if decision.name == "wait":
            return None
        scenario, forced = self._basics(observation)
        if forced is not None:
            if forced.name == "retreat_from" and decision.name == forced.name and self._point(decision.args.get("pos")) == forced.args["pos"]:
                return None
            if forced.name == "auto_sleep" and decision.name == "auto_sleep":
                return None
            return scenario
        status = observation["status"]
        if decision.name == "eat_food":
            return None if self._food_valid(decision, observation) else "food_not_available_or_unsafe"
        if self._meal(observation):
            return "existing_food_has_priority"
        if status["health"] <= self.low_health:
            if status["food"] < 18 and not self._night(status) and self._food_valid(decision, observation, low_health=True):
                return None
            return "low_health_food_only"
        if self._night(status):
            home = self._return_home(observation)
            if home is not None:
                return None if decision.name == home.name and decision.args == home.args else "night_requires_verified_sleep_or_shelter_route"
            if decision.name == "auto_sleep" and (not status["is_day"] or status["thundering"]) and self._bed(observation):
                return None
            return "night_requires_verified_sleep_or_shelter_route"
        if status["food"] <= 6 and not self._food_valid(decision, observation):
            return "critical_hunger_nearby_food_only"
        return None
