"""Pure single-step planning before a model request; caller owns execution/budgets.

None means local work is exhausted, never that a task succeeded. Inputs and
planner state are not mutated. Recovery and serial harness integration are external.
"""

import copy
import math
from typing import Optional

from agent.daily_survival import CROPS, DailySurvivalGoals
from agent.survival_planner import AIR, LOG_NAMES, PLANK_NAMES, Decision, SurvivalPlanner
from agent.tools import tools_for_objective
from core.competition_tasks import _MAPPED


class LocalTaskPlanner:
    """Choose one registered production action from supplied snapshots only."""

    def __init__(self, settings=None):
        settings = settings or {}
        self.settings = settings.get("survival", settings)
        self.rules = SurvivalPlanner(self.settings)
        self.daily = DailySurvivalGoals(self.settings)
        self.schemas = {
            objective: {tool["function"]["name"]: tool["function"]["parameters"]
                        for tool in tools_for_objective(objective)}
            for objective in ("starter", "daily", "competition")
        }

    def choose(self, observation: dict, context: dict, competition=None,
               daily_board=None) -> Optional[Decision]:
        """Return a copied Decision, or None for the caller's model handoff.

        competition is CompetitionTasks.model_view(), not the full catalog.
        daily_board is an optional fresh DailySurvivalGoals.build() result.
        """
        context = context or {}
        if not self._ready(observation):
            return None
        objective = ("competition" if competition is not None else
                     "daily" if daily_board is not None else self.settings.get("objective", "starter"))
        if objective not in self.schemas:
            return None
        baseline = self.rules.choose(observation, context)
        if (baseline.goal in {"observe", "recover", "safety", "food"}
                or baseline.name in {"eat_food", "retreat_from"}):
            return None  # Safety/recovery belongs to the caller.
        night = not observation["status"]["is_day"] or observation["status"]["thundering"]
        limits = context.get("survival_constraints") or {}
        if night and baseline.name != "auto_sleep" and limits.get("scenario") != "night_preparation":
            return None
        if night and limits.get("scenario") == "night_preparation":
            status = observation["status"]
            nearby = status.get("nearest_hostile")
            if (status["health"] != 20 or status["food"] < 12 or status["thundering"]
                    or any(distance < 12 for distance, _ in self.rules._hostiles(observation, status["pos"]))
                    or nearby is not None and (not isinstance(nearby, dict)
                        or not self.rules._number(nearby.get("dist")) or nearby["dist"] < 12)):
                return None
        if objective == "competition":
            decision = self._competition(observation, context, competition)
        elif objective == "daily":
            decision = (self._board_action(daily_board) if daily_board is not None else
                        self.daily.choose(observation, context.get("memory"), context))
        else:
            decision = baseline
        return self._permitted(decision, observation, context, objective)

    def _ready(self, observation):
        if (not isinstance(observation, dict) or observation.get("fresh") is not True
                or observation.get("stale") is True
                or observation.get("observation_scope", "full") != "full"):
            return False
        counts = observation.get("counts")
        if "counts" in observation:
            if not isinstance(counts, dict) or any(not isinstance(name, str) or type(count) is not int
                    or count < 0 for name, count in counts.items()):
                return False
        else:
            inventory = observation.get("inventory")
            if not isinstance(inventory, list) or any(not isinstance(item, dict)
                    or not isinstance(item.get("name"), str) or type(item.get("count")) is not int
                    or item["count"] < 0 for item in inventory):
                return False
        status = observation.get("status")
        return (isinstance(status, dict) and status.get("ready") is True
                and status.get("alive") is True and not status.get("busy")
                and status.get("on_ground") is True
                and status.get("dimension") in {"overworld", "minecraft:overworld"}
                and status.get("gamemode") == "survival"
                and self.rules._pos(status.get("pos")) is not None
                and all(self.rules._number(status.get(key)) for key in ("health", "food"))
                and status["health"] > 0
                and all(type(status.get(key)) is bool for key in ("is_day", "thundering"))
                and not any(status.get(key) for key in
                            ("in_lava", "on_fire", "submerged", "in_water", "in_vehicle")))

    @staticmethod
    def _decision(action):
        if not isinstance(action, dict) or not isinstance(action.get("args"), dict):
            return None
        return Decision(action.get("name"), copy.deepcopy(action["args"]),
                        action.get("reason", "Current task's observed next step"), action.get("goal", "local_task"))

    def _board_action(self, board):
        if (not isinstance(board, dict) or board.get("fresh") is False
                or board.get("observation_scope", "full") != "full"):
            return None
        row = next((row for row in board.get("tasks", []) if isinstance(row, dict)
                    and row.get("id") == board.get("active_task_id")
                    and row.get("status") in {"ready", "running"}), None)
        return self._decision(row.get("next_action")) if row else None

    def _competition(self, observation, context, board):
        if not isinstance(board, dict) or board.get("stale") is not False:
            return None
        if self.rules._number(board.get("remaining_seconds")) and board["remaining_seconds"] <= 0:
            return None
        if board.get("phase") == "selection":
            selectable = board.get("selectable_goal_ids", [])
            candidates = [row for row in board.get("tasks", []) if isinstance(row, dict)
                          and row.get("id") in _MAPPED and row["id"] in selectable
                          and row.get("selectable") is not False
                          and row.get("eligibility", "scored") == "scored"
                          and row.get("availability") in {"ready", "needs_prerequisites"}]
            if not candidates:
                return None
            # Stable min: ready first, then the runtime's original catalog order.
            row = min(candidates, key=lambda row: row["availability"] != "ready")
            return Decision("select_task", {"goal_id": row["id"]},
                            "Select a currently unlocked scored task, preferring ready tasks", row["id"])
        active = board.get("active_goal")
        if (board.get("phase") != "active" or not isinstance(active, dict)
                or active.get("id") not in _MAPPED
                or active.get("id") != board.get("active_goal_id")
                or active.get("eligibility") != "scored"):
            return None
        goal = active["id"]
        if active.get("completion") == "completed":
            advancement = board.get("advancements") or {}
            if (board.get("assessment_complete") is True and board.get("assessment_fresh") is not False
                    and advancement.get("ready") is True
                    and advancement.get("stale") is False):
                return Decision("finish_task", {"reason": "Fresh server advancement confirms the active task completed"},
                                "Finish only after fresh server completion and assessment", goal)
            return None
        counts = self.rules._counts(observation)
        path = goal.split("/")[-1]
        if path == "mine_stone":
            if counts.get("cobblestone", 0):
                return None  # Inventory never substitutes for server completion.
            if not self.rules._has_tool(counts, "pickaxe", wooden=True):
                return self.rules._craft_goal("wooden_pickaxe", observation, context)
            return self.rules._gather(("stone", "cobblestone"), goal, observation, context)
        if path == "upgrade_tools":
            if self.rules._has_tool(counts, "pickaxe"):
                return None
            return self.rules._craft_goal("stone_pickaxe", observation, context)
        if path in {"smelt_iron", "iron_tools", "obtain_armor"}:
            return self._iron(path, observation, context, counts)
        if path == "sleep_in_bed":
            return self._sleep(observation, context)
        if path == "plant_seed":
            return self._plant(observation, context, counts)
        return None

    def _iron(self, path, observation, context, counts):
        required = 3 if path == "iron_tools" else 4
        if path != "smelt_iron" and counts.get("iron_ingot", 0) >= required:
            item = "iron_pickaxe" if path == "iron_tools" else "iron_boots"
            if counts.get(item, 0):
                return None
            if path == "iron_tools" and counts.get("stick", 0) < 2:
                return self.rules._need_planks(2, observation, context) or Decision(
                    "craft_item", {"item": "stick", "count": 4}, "Prepare sticks for an iron pickaxe", path)
            return self.rules._workspace(observation, context) or Decision(
                "craft_item", {"item": item, "count": 1}, "Craft using currently held iron", path)
        if path == "smelt_iron" and counts.get("iron_ingot", 0):
            return None
        action = self._decision(self.daily._smelt({"iron_ingot"}, observation, counts, path, context))
        if action:
            if action.name == "smelt_start":
                furnace = next((row for row in observation.get("furnaces", [])
                                if row.get("pos") == action.args["pos"]), {})
                if not self._empty_furnace(furnace):
                    return Decision("inspect_container", {"pos": action.args["pos"]},
                                    "Read all three furnace slots and burning state before loading", path)
            return action
        furnaces = self.daily._safe_rows(observation, {"furnace"}, context=context)
        # Processing is not a reason to refill or start unrelated work.
        for row in observation.get("furnaces", []):
            if self.daily._verified(row, observation["status"]["dimension"]) and (
                    self.daily._burning(row) or (row.get("input") or {}).get("count", 0)):
                return None
        raw = any(counts.get(item, 0) for item in ("raw_iron", "iron_ore", "deepslate_iron_ore"))
        if not raw:
            if not self.rules._has_tool(counts, "pickaxe"):
                return self.rules._craft_goal("stone_pickaxe", observation, context)
            return self._decision(self.daily._gather(
                ("iron_ore", "deepslate_iron_ore"), observation, context, path, iron=True))
        if not furnaces:
            if counts.get("furnace", 0):
                return self._decision(self.daily._place("furnace", observation, path, context))
            return self.rules._craft_goal("furnace", observation, context)
        if not any(counts.get(item, 0) for item in ("coal", "charcoal") + PLANK_NAMES):
            for name in LOG_NAMES:
                if counts.get(name, 0):
                    return self.rules._need_planks(2, observation, context)
            if self.rules._has_tool(counts, "pickaxe", wooden=True):
                return self._decision(self.daily._gather(
                    ("coal_ore", "deepslate_coal_ore"), observation, context, path))
        return None

    @staticmethod
    def _empty_furnace(row):
        return (type(row.get("burning")) is bool and row["burning"] is False
                and all(isinstance(row.get(key), dict) and type(row[key].get("count")) is int
                        and row[key]["count"] == 0 for key in ("input", "fuel", "output")))

    def _sleep(self, observation, context):
        status = observation["status"]
        if status["is_day"] and not status["thundering"]:
            return None
        beds = self.daily._safe_rows(observation, {row["name"] for row in self.rules._blocks(observation)
                                                  if row["name"].endswith("_bed")}, context=context)
        offsets = {"north": (0, -1), "south": (0, 1), "west": (-1, 0), "east": (1, 0)}
        for foot in beds:
            props = foot.get("properties") or {}
            if (props.get("part") != "foot" or props.get("occupied") not in (False, "false")
                    or props.get("facing") not in offsets):
                continue
            dx, dz = offsets[props["facing"]]
            head_pos = [foot["pos"][0] + dx, foot["pos"][1], foot["pos"][2] + dz]
            for head in beds:
                hp = head.get("properties") or {}
                if (head["name"] == foot["name"] and head["pos"] == head_pos
                        and hp.get("part") == "head" and hp.get("facing") == props["facing"]
                        and hp.get("occupied") in (False, "false")):
                    return Decision("auto_sleep", {}, "Use an observed complete free bed in the sleep window", "sleep")
        return None

    def _plant(self, observation, context, counts):
        seed = next((seed for _, seed in CROPS.values() if counts.get(seed, 0)), None)
        blocks = self.rules._blocks(observation) + self.rules._blocks({"blocks": observation.get("terrain", [])})
        if seed:
            for row in self.daily._safe_rows(observation, {"farmland"}, context=context):
                above = [row["pos"][0], row["pos"][1] + 1, row["pos"][2]]
                if any(block["pos"] == above and block["name"] in AIR for block in blocks):
                    return Decision("plant_crop", {"pos": list(row["pos"]), "item": seed},
                                    "Plant existing seed on observed farmland with clear crop space", "food_production")
        return None

    def _permitted(self, decision, observation, context, objective):
        if not isinstance(decision, Decision) or not isinstance(decision.name, str):
            return None
        name = decision.name
        if (name in {"wait", "update_progress", "end_competition", "retreat_from", "eat_food"}
                or name.startswith(("force", "report"))):
            return None
        if name == "goal_complete" and (objective != "starter"
                or not self.rules.progress(observation)["starter_goal_complete"]):
            return None
        if name == "move_to" and decision.goal != "crafting_table":
            return None  # No guessed resource sites or exploration waypoints.
        limits = context.get("survival_constraints") or {}
        allowed = limits.get("allowed_actions")
        if allowed is not None and (not isinstance(allowed, (list, tuple, set)) or name not in allowed):
            if name == "gather_materials" and decision.args.get("resource") == "logs":
                return self._nearby_log(observation, context, objective)
            return None
        night = not observation["status"]["is_day"] or observation["status"]["thundering"]
        if night and (name == "gather_materials" or "batch" in name or name in {
                "gather_resource", "hunt_animal", "clear_foliage", "gather_seeds"}):
            return self._nearby_log(observation, context, objective) if name == "gather_materials" and decision.args.get("resource") == "logs" else None
        capabilities = context.get("capabilities")
        if (capabilities is not None and name not in {"select_task", "finish_task", "goal_complete"}
                and name not in capabilities):
            return None
        schema = self.schemas[objective].get(name)
        if schema is None or not self._matches(decision.args, schema):
            return None
        pos = decision.args.get("pos")
        if pos is not None:
            radius = limits.get("max_action_distance", 32)
            if not self.rules._number(radius) or radius < 0:
                return None
            if night and limits.get("scenario") == "night_preparation":
                radius = min(radius, 3)
            if (math.dist(pos, observation["status"]["pos"]) > radius
                    or self.rules._key(pos) in self.rules._blocked(context)):
                return None
        if name == "smelt_start":
            furnace = next((row for row in observation.get("furnaces", [])
                            if row.get("pos") == pos and self.daily._verified(
                                row, observation["status"]["dimension"])), None)
            if furnace is None or not self._empty_furnace(furnace):
                return None
        if name in {"gather_block", "gather_resource"}:
            blocks = self.rules._blocks(observation)
            row = next((row for row in blocks if row["pos"] == pos
                        and row["name"] == decision.args.get("expected_name")), None)
            if row is None or not self.rules._safe_target(row, observation["status"]["pos"], blocks,
                    self.rules._blocked(context), self.rules._hostiles(observation, observation["status"]["pos"])):
                return None
            if night and (row["name"] not in LOG_NAMES or row.get("fresh") is not True
                          or row.get("can_see") is not True or row.get("can_dig") is False):
                return None
        return Decision(name, copy.deepcopy(decision.args), decision.reason, decision.goal)

    def _nearby_log(self, observation, context, objective):
        for row in self.daily._safe_rows(observation, set(LOG_NAMES), context=context):
            if math.dist(row["pos"], observation["status"]["pos"]) > 3:
                continue
            decision = Decision("gather_block", {"pos": list(row["pos"]), "expected_name": row["name"]},
                                "Replace a log batch with one currently observed nearby log", "wood_reserve")
            result = self._permitted(decision, observation, context, objective)
            if result:
                return result
        return None

    @classmethod
    def _matches(cls, value, schema):
        """Validate generated arguments against the actual registered tool schema."""
        kind = schema.get("type")
        if kind == "object":
            properties = schema.get("properties", {})
            return (isinstance(value, dict) and not set(value) - set(properties)
                    and all(key in value for key in schema.get("required", []))
                    and all(cls._matches(item, properties[key]) for key, item in value.items()))
        if kind == "array":
            return (isinstance(value, list) and schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", math.inf)
                    and all(cls._matches(item, schema.get("items", {})) for item in value))
        if kind in {"integer", "number"}:
            if not SurvivalPlanner._number(value) or kind == "integer" and type(value) is not int:
                return False
            if not schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf):
                return False
        if kind == "string" and (not isinstance(value, str)
                or not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", math.inf)):
            return False
        if kind == "boolean" and type(value) is not bool:
            return False
        return "enum" not in schema or value in schema["enum"]
