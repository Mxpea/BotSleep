"""Evidence-based daily goals; no I/O, model calls or saved-action replay.

Memory supplies places to revalidate, never current inventory or completion.
Optional ``daily_evidence`` rows are trusted, current collector evidence with
verified/fresh=True, matching dimension and a position. Furnace/container rows
use that same envelope; furnace rows have input/output {name, count}, container
rows have items [{name, count}]. Old snapshots must not be copied into them.
"""

import copy
import math
import re

from agent.survival_planner import Decision, LOG_NAMES, PLANK_NAMES, SAFE_FOOD, SurvivalPlanner
from skills.daily_survival import DailySurvivalSkills


DAILY_SKILLS = frozenset({
    "gather_block", "gather_resource", "craft_item", "place_item", "eat_food", "move_to",
    "retreat_from", "auto_sleep", "harvest_crop", "plant_crop", "till_soil", "hunt_animal",
    "equip_armor", "smelt_start", "smelt_collect", "inspect_container", "store_items",
    "take_items", "collect_drops", "place_bed", "place_door", "wait",
    "plan_shelter", "inspect_shelter", "build_shelter_step", "position_for_task",
    "gather_materials", "build_shelter_batch", "set_door", "gather_seeds", "clear_foliage",
})
TASK_IDS = ("recover_drops", "food_reserve", "wood_reserve", "starter_tools", "furnace",
            "fuel_reserve", "iron_tools", "base_site", "bed", "shelter", "storage",
            "food_production", "maintain")
COOKING = {"beef": "cooked_beef", "porkchop": "cooked_porkchop", "mutton": "cooked_mutton",
           "chicken": "cooked_chicken", "cod": "cooked_cod", "salmon": "cooked_salmon",
           "potato": "baked_potato", "raw_iron": "iron_ingot", "iron_ore": "iron_ingot",
           "deepslate_iron_ore": "iron_ingot", **{name: "charcoal" for name in LOG_NAMES}}
CROPS = {"wheat": (7, "wheat_seeds"), "carrots": (7, "carrot"),
         "potatoes": (7, "potato"), "beetroots": (3, "beetroot_seeds")}
TOOL_KINDS = ("pickaxe", "axe", "sword")
BUILDING_MATERIALS = ("oak_planks", "birch_planks", "spruce_planks", "cobblestone")


class DailySurvivalGoals:
    """Build a bounded task board, or choose one action for rules mode only."""

    def __init__(self, settings=None):
        self.settings = settings or {}
        self.rules = SurvivalPlanner(self.settings)
        daily = self.settings.get("daily", self.settings)
        self.targets = {name: self._limit(daily.get(name), default, maximum) for name, default, maximum in (
            ("food_reserve", 8, 64), ("wood_reserve", 8, 64), ("fuel_reserve", 8, 64),
            ("maintenance_interval_seconds", 30, 60))}

    @staticmethod
    def _limit(value, default, maximum):
        return max(1, min(maximum, int(value))) if type(value) in (int, float) and math.isfinite(value) else default

    @staticmethod
    def _pos(value):
        return SurvivalPlanner._pos(value)

    @staticmethod
    def _rows(value):
        return [row for row in value or [] if isinstance(row, dict)] if isinstance(value, list) else []

    @staticmethod
    def _drop_name(row):
        item = row.get("item")
        if isinstance(item, str) and len(item) <= 160:
            match = re.fullmatch(r"([a-z0-9_]+)x([1-9][0-9]*)", item)
            if match:
                return match.group(1)
        name = row.get("item_name") or (item.get("name") if isinstance(item, dict) else None)
        return name if isinstance(name, str) and re.fullmatch(r"[a-z0-9_]{1,128}", name) else None

    @staticmethod
    def _action(name, args, reason, goal):
        return {"name": name, "args": args, "reason": reason, "goal": goal}

    def _safe_rows(self, observation, names=None, entities=False, context=None):
        origin = self._pos(observation["status"].get("pos"))
        if origin is None:
            return []
        hostiles = self.rules._hostiles(observation, origin)
        blocked = self.rules._blocked(context or {})
        rows = self._rows(observation.get("entities" if entities else "blocks"))
        found = []
        for row in rows:
            pos = self._pos(row.get("pos"))
            if pos is None or math.dist(origin, pos) > 16 or (names is not None and row.get("name") not in names):
                continue
            if self.rules._key(pos) in blocked:
                continue
            if any(math.dist(pos, threat) <= self.rules.threat_radius for _, threat in hostiles):
                continue
            dangerous = [b for b in self._rows(observation.get("blocks")) + self._rows(observation.get("terrain"))
                         if self._pos(b.get("pos")) is not None and b.get("name") in {
                             "lava", "fire", "soul_fire", "magma_block", "cactus", "campfire", "soul_campfire", "powder_snow"}]
            if any(math.dist(pos, b["pos"]) <= 1.5 for b in dangerous):
                continue
            found.append(row)
        return sorted(found, key=lambda row: (math.dist(origin, row["pos"]), str(row.get("name")), row["pos"]))

    def _verified(self, row, dimension):
        return (isinstance(row, dict) and row.get("fresh") is True and row.get("verified") is True
                and row.get("dimension") == dimension and self._pos(row.get("pos")) is not None)

    @staticmethod
    def _burning(row):
        info = row.get("info") if isinstance(row.get("info"), dict) else {}
        return row.get("burning") is True or info.get("burning") is True

    def _place(self, item, observation, goal, context=None):
        origin = self._pos(observation["status"].get("pos"))
        for row in self._rows(observation.get("placement_candidates"))[:8]:
            pos = self._pos(row.get("pos"))
            base = (observation.get("daily_evidence") or {}).get("base_site")
            if goal in {"bed", "storage"} and self._verified(base, observation["status"].get("dimension")):
                if pos is None or pos[1] != base["pos"][1] or abs(pos[0] - base["pos"][0]) > 1 or abs(pos[2] - base["pos"][2]) > 1:
                    continue  # Reserve the blueprint boundary for walls and exits.
                if pos[0] == base["pos"][0] and pos[2] <= base["pos"][2]:
                    continue  # Preserve the north doorway and centre approach.
                if goal == "bed" and not (pos[2] == base["pos"][2] and abs(pos[0] - base["pos"][0]) == 1):
                    continue  # Side-middle foot and rear head leave the centre entry clear.
            if (pos is not None and origin is not None and math.dist(origin, pos) <= 4
                    and self.rules._key(pos) not in self.rules._blocked(context or {})):
                if goal == "bed":
                    stance = [pos[0] + .5, pos[1], pos[2] - .5]
                    if (tuple(math.floor(value) for value in origin) != (pos[0], pos[1], pos[2] - 1)
                            or DailySurvivalSkills.placement_facings(pos, origin) != ["south"]):
                        blocked = self.rules._blocked(context or {})
                        if (math.dist(origin, stance) > 3 or math.dist(origin, stance) <= .4
                                or self.rules._key(stance) in blocked
                                or self.rules._key([pos[0], pos[1], pos[2] - 1]) in blocked):
                            continue
                        stands = self._rows(observation.get("safe_stand_candidates"))[:8]
                        if not any(self._pos(row.get("pos")) == stance for row in stands):
                            continue
                        if DailySurvivalSkills.placement_facings(pos, stance) != ["south"]:
                            continue
                        return self._action("position_for_task", {"pos": stance},
                            "Use the freshly checked front-side stance; reobserve before placing the bed toward the rear", goal)
                name = "place_bed" if item.endswith("_bed") else "place_item"
                return self._action(name, {"item": item, "pos": [math.floor(v) for v in pos]},
                                    "Validate and place the held item in a current candidate cell", goal)
        return None

    def _gather(self, names, observation, context, goal, *, iron=False):
        decision = self.rules._gather(names, goal, observation, context)
        if decision.name == "gather_block":
            return self._action("gather_resource" if iron else decision.name, decision.args, decision.reason, goal)
        if decision.name == "move_to":
            return self._action(decision.name, decision.args, decision.reason, "explore")
        return None

    def _batch_ready(self, observation):
        status = observation["status"]
        daytime = status.get("time_of_day")
        hostile = status.get("nearest_hostile")
        return (status.get("is_day") is True and status.get("thundering") is False
                and self.rules._number(daytime) and 0 <= daytime < 11000
                and status.get("health", 0) > max(8, self.rules.low_health) and status.get("food", 0) > 6
                and status.get("on_ground") is True
                and not any(status.get(key) for key in ("in_water", "submerged", "in_lava", "on_fire", "in_vehicle", "is_sleeping"))
                and not any(distance <= 8 for distance, _ in self.rules._hostiles(observation, status["pos"]))
                and (hostile is None or isinstance(hostile, dict) and self.rules._number(hostile.get("dist")) and hostile["dist"] > 8))

    def _batch_hint(self, action, observation, counts, context, capabilities):
        """Only compress currently justified work; a batch is never completion evidence."""
        if not action or not self._batch_ready(observation):
            return action
        if action["name"] == "build_shelter_step" and "build_shelter_batch" in capabilities:
            missing = (observation.get("shelter") or {}).get("missing_blocks")
            maximum = min(8, counts.get(action["args"]["material"], 0), missing if type(missing) is int else 0)
            if maximum >= 2:
                return self._action("build_shelter_batch", {**action["args"], "max_blocks": maximum},
                    "Build a bounded batch with fresh verification after every block; the house still needs independent inspection", action["goal"])
            return action
        if action["name"] != "gather_block" or "gather_materials" not in capabilities:
            return action
        name, goal = action["args"].get("expected_name"), action["goal"]
        if name in LOG_NAMES:
            resource, names = "logs", set(LOG_NAMES)
            wood = sum(counts.get(key, 0) for key in LOG_NAMES) + sum(counts.get(key, 0) for key in PLANK_NAMES) // 4
            wanted = self.targets["wood_reserve"] - wood if goal == "wood_reserve" else 2
            if goal == "shelter":
                missing = (observation.get("shelter") or {}).get("missing_blocks")
                wanted = math.ceil(missing / 4) if type(missing) is int else 1
        elif name in {"stone", "cobblestone"}:
            resource, names = "cobblestone", {"stone", "cobblestone"}
            wanted = (8 if goal == "furnace" else 3) - counts.get("cobblestone", 0)
        else:
            return action
        if wanted < 2:
            return action
        origin = observation["status"]["pos"]
        blocks, blocked = self.rules._blocks(observation), self.rules._blocked(context)
        hostiles = self.rules._hostiles(observation, origin)
        current = {}
        for row in blocks:
            if row["name"] in names and math.dist(origin, row["pos"]) <= 8:
                current[self.rules._key(row["pos"])] = row
        candidates = sorted((row for row in current.values() if row.get("can_see") is True
            and self.rules._safe_target(row, origin, blocks, blocked, hostiles)),
            key=lambda row: (math.dist(origin, row["pos"]), row["name"], row["pos"]))
        count = min(8, wanted, len(candidates))
        if count < 2:
            return action
        radius = max(1, math.ceil(math.dist(origin, candidates[count - 1]["pos"])))
        # The batch rescans and chooses its own next target. Do not send it into
        # a radius containing a known blocked/occluded alternative it may select.
        if any(math.dist(origin, row["pos"]) <= radius and
               (row.get("can_see") is not True or not self.rules._safe_target(row, origin, blocks, blocked, hostiles))
               for row in current.values()
               if (math.floor(row["pos"][0]), math.floor(row["pos"][2])) != (math.floor(origin[0]), math.floor(origin[2]))):
            return action
        return self._action("gather_materials", {"resource": resource, "count": count, "radius": radius},
            "Gather a bounded current material deficit; every target and inventory gain must be revalidated", goal)

    def _foliage_hint(self, action, observation, context, capabilities):
        if (not action or action["name"] not in {"gather_block", "gather_resource"}
                or "clear_foliage" not in capabilities or not self._batch_ready(observation)
                or observation["status"]["health"] < 12 or observation["status"]["food"] < 8):
            return action
        target = next((row for row in self._rows(observation.get("blocks"))
                       if row.get("pos") == action["args"].get("pos") and row.get("can_see") is False), None)
        blocker = (target.get("visibility") or {}).get("blocker") if target else None
        origin = [math.floor(value) for value in observation["status"]["pos"]]
        for row in self._safe_rows(observation, DailySurvivalSkills.FOLIAGE, context=context):
            pos = row["pos"]
            if (pos == blocker and row.get("fresh") is True and row.get("can_see") is True
                    and max(abs(pos[0] - origin[0]), abs(pos[2] - origin[2])) == 1
                    and pos[1] in (origin[1], origin[1] + 1)):
                return self._action("clear_foliage", {"pos": pos, "expected_name": row["name"]},
                    "Clear the single freshly visible leaf blocking this resource, then reobserve before gathering", action["goal"])
        return action

    def _smelt(self, outputs, observation, counts, goal, context=None):
        dimension = observation["status"].get("dimension")
        origin = observation["status"]["pos"]
        hostiles = self.rules._hostiles(observation, origin)
        for row in self._rows(observation.get("furnaces"))[:8]:
            if (not self._verified(row, dimension) or math.dist(row["pos"], origin) > 16
                    or self.rules._key(row["pos"]) in self.rules._blocked(context or {})
                    or any(math.dist(row["pos"], threat) <= self.rules.threat_radius for _, threat in hostiles)):
                continue
            output = row.get("output") or {}
            if output.get("name") in outputs and type(output.get("count")) is int and output["count"] > 0:
                return self._action("smelt_collect", {"pos": row["pos"], "item": output["name"],
                    "count": min(16, output["count"])}, "Collect a currently observed furnace output", goal)
        for row in self._safe_rows(observation, {"furnace"}, context=context):
            # Opening the furnace is a separate verified step. Never refill a
            # remembered/start-only job without inspecting its current slots.
            checked = next((f for f in self._rows(observation.get("furnaces"))
                            if f.get("pos") == row["pos"] and self._verified(f, dimension)), None)
            pending_job = any(job.get("pos") == row["pos"] and job.get("dimension") == dimension
                              and job.get("expected_output") in outputs
                              for job in self._rows((context or {}).get("smelting_jobs")))
            if pending_job and checked is None:
                return self._action("inspect_container", {"pos": row["pos"]},
                                    "Reinspect a pending furnace job; the old start result is not output evidence", goal)
            if pending_job and checked is not None and ((checked.get("input") or {}).get("count", 0) or self._burning(checked)):
                return self._action("wait", {"seconds": 5}, "The inspected furnace is still processing; reobserve later", goal)
            inputs = [item for item, output in COOKING.items() if output in outputs and counts.get(item, 0) > 0]
            fuel = next((item for item in ("coal", "charcoal") if counts.get(item, 0) > 0), None)
            if fuel is None:
                fuel = next((item for item in PLANK_NAMES if counts.get(item, 0) > 0), None)
            if not inputs or fuel is None:
                continue
            if checked is None:
                return self._action("inspect_container", {"pos": row["pos"]}, "Inspect furnace slots before transferring inputs", goal)
            if any((checked.get(key) or {}).get("count", 0) for key in ("input", "fuel", "output")) or self._burning(checked):
                continue
            capacity = 8 if fuel in {"coal", "charcoal"} else 1.5
            count = min(16, counts[inputs[0]], math.floor(counts[fuel] * capacity))
            return self._action("smelt_start", {"pos": row["pos"], "input_item": inputs[0], "count": count,
                "fuel_item": fuel, "fuel_count": math.ceil(count / capacity)}, "Start smelting; completion still requires verified output", goal)
        return None

    def _food_action(self, observation, counts, context):
        stored = self._take_supplies(observation, counts, SAFE_FOOD, self.targets["food_reserve"], "food_reserve", context)
        if stored:
            return stored
        if counts.get("wheat", 0) >= 3:
            workspace = self.rules._workspace(observation, context)
            if workspace:
                return self._action(workspace.name, workspace.args, workspace.reason, "food_reserve") if workspace.name != "wait" else None
            return self._action("craft_item", {"item": "bread", "count": min(64, counts["wheat"] // 3)},
                                "Turn held wheat into a verified food reserve", "food_reserve")
        cooked = self._smelt(set(SAFE_FOOD), observation, counts, "food_reserve", context)
        if cooked:
            return cooked
        for row in self._safe_rows(observation, set(CROPS), context=context):
            age = (row.get("properties") or {}).get("age")
            if type(age) is int and age >= CROPS[row["name"]][0]:
                return self._action("harvest_crop", {"pos": row["pos"], "expected_name": row["name"]},
                                    "Harvest an observed mature crop and verify inventory gain", "food_reserve")
        # Raw meat needs cooking. Do not repeatedly hunt while unable to cook
        # the raw food already held, or attack an unobserved/non-whitelisted mob.
        if not any(counts.get(item, 0) for item, output in COOKING.items() if output in SAFE_FOOD):
            for row in self._safe_rows(observation, {"cow", "pig", "sheep", "chicken"}, entities=True, context=context):
                entity_id = row.get("id", row.get("entity_id"))
                if type(entity_id) is int and self._can_hunt(observation, counts, row):
                    return self._action("hunt_animal", {"entity_id": entity_id, "expected_name": row["name"]},
                                        "Acquire food from one observed passive animal", "food_reserve")
        return None

    def _can_hunt(self, observation, counts, row):
        status = observation["status"]
        return (status.get("health", 0) >= 12 and status.get("food", 0) >= 10
                and any(count and (name.endswith("_axe") or name.endswith("_sword")) for name, count in counts.items())
                and math.dist(status["pos"], row["pos"]) <= 8)

    def _containers(self, observation, context=None):
        dimension = observation["status"].get("dimension")
        blocked = self.rules._blocked(context or {})
        hostiles = self.rules._hostiles(observation, observation["status"]["pos"])
        return [row for row in self._rows(observation.get("containers"))[:8]
                if self._verified(row, dimension) and row.get("name") in {"chest", "barrel"}
                and isinstance(row.get("items"), list) and self.rules._key(row["pos"]) not in blocked
                and math.dist(row["pos"], observation["status"]["pos"]) <= 16
                and not any(math.dist(row["pos"], threat) <= self.rules.threat_radius for _, threat in hostiles)]

    def _take_supplies(self, observation, counts, names, target, goal, context):
        deficit = target - sum(counts.get(name, 0) for name in names)
        if deficit <= 0:
            return None
        for row in self._containers(observation, context):
            stored = self.rules._counts({"inventory": row["items"]})
            for item in sorted(stored):
                if item in names:
                    return self._action("take_items", {"pos": row["pos"], "item": item,
                        "count": min(64, deficit, stored[item])}, "Withdraw a currently verified needed reserve", goal)
        return None

    def _surplus(self, counts):
        reserves = {**{name: self.targets["wood_reserve"] for name in LOG_NAMES},
                    **{name: self.targets["wood_reserve"] * 4 for name in PLANK_NAMES},
                    "cobblestone": 32, "coal": self.targets["fuel_reserve"], "charcoal": self.targets["fuel_reserve"],
                    "stick": 16, "raw_iron": 16, "iron_ingot": 16,
                    "wheat_seeds": 16, "beetroot_seeds": 16}
        for item, count in sorted(counts.items()):
            if item in reserves and count > reserves[item]:
                return item, min(64, count - reserves[item])
        return None

    def _base_candidate(self, observation, counts, context):
        material = next((name for name in BUILDING_MATERIALS if counts.get(name, 0)), "oak_planks")
        blocked = self.rules._blocked(context)
        candidates = [observation["status"]["pos"]] + [row.get("pos") for row in self._rows(observation.get("safe_stand_candidates"))[:8]]
        seen = set()
        for raw in candidates:
            pos = self._pos(raw)
            if pos is None:
                continue
            pos = [math.floor(v) for v in pos]
            key = self.rules._key(pos)
            if key in blocked or key in seen:
                continue
            seen.add(key)
            return self._action("plan_shelter", {"pos": pos, "material": material},
                                "Survey a local candidate footprint; a standing cell does not prove a safe base", "base_site")
        return None

    def _shelter_action(self, observation, counts, base, context):
        if not self._verified(base, observation["status"].get("dimension")):
            return None
        pos = base["pos"]
        if self.rules._key(pos) in self.rules._blocked(context):
            return None
        shelter = observation.get("shelter")
        if (not isinstance(shelter, dict) or shelter.get("fresh") is not True
                or shelter.get("dimension") != observation["status"]["dimension"] or shelter.get("pos") != pos):
            return self._action("inspect_shelter", {"pos": pos}, "Reinspect the base before selecting one construction step", "shelter")
        geometry = shelter.get("geometry") or {}
        if (geometry.get("floor_verified") is not True or geometry.get("exit_verified") is not True
                or shelter.get("hazards") or shelter.get("occupied_blueprint_cells")):
            return None
        if shelter.get("needs_position") is True:
            for raw in shelter.get("stand_candidates") or []:
                target = self._pos(raw)
                if (target and 1 < math.dist(target, observation["status"]["pos"]) <= 16
                        and self.rules._key(target) not in self.rules._blocked(context)):
                    return self._action("move_to", {"pos": target, "range": .5},
                                        "Move to a currently checked construction standing cell, then reobserve", "shelter")
            return None
        if type(shelter.get("missing_blocks")) is int and shelter["missing_blocks"] > 0:
            material = next((name for name in BUILDING_MATERIALS if counts.get(name, 0)), None)
            if material:
                return self._action("build_shelter_step", {"pos": pos, "material": material},
                                    "Build at most one checked blueprint block", "shelter")
            log = next((name for name in ("oak_log", "birch_log", "spruce_log") if counts.get(name, 0)), None)
            if log:
                count = min(64, counts[log] * 4, math.ceil(shelter["missing_blocks"] / 4) * 4)
                return self._action("craft_item", {"item": log[:-4] + "_planks", "count": count},
                                    "Prepare available building material for the remaining blueprint", "shelter")
            return self._gather(("oak_log", "birch_log", "spruce_log"), observation, context, "shelter")
        door = shelter.get("needs_door")
        if isinstance(door, dict) and self._pos(door.get("pos")):
            held = next((name for name in counts if name.endswith("_door") and name != "iron_door" and counts[name]), None)
            if held:
                stand = self._pos(door.get("stand_pos"))
                if stand and math.dist(stand, observation["status"]["pos"]) > 1:
                    if self.rules._key(stand) in self.rules._blocked(context):
                        return None
                    return self._action("move_to", {"pos": stand, "range": .5}, "Approach the checked doorway standing cell", "shelter")
                return self._action("place_door", {"pos": door["pos"], "item": held},
                                    "Place and verify both wooden-door halves", "shelter")
            planks = next((name for name in PLANK_NAMES if counts.get(name, 0) >= 6), None)
            if planks:
                workspace = self.rules._workspace(observation, context)
                return (self._action(workspace.name, workspace.args, workspace.reason, "shelter") if workspace
                        else self._action("craft_item", {"item": planks[:-7] + "_door", "count": 1}, "Craft a wooden door for the checked opening", "shelter"))
            needed = self.rules._need_planks(6, observation, context)
            if needed:
                return self._action(needed.name, needed.args, needed.reason, "shelter")
        return self._action("inspect_shelter", {"pos": pos}, "Confirm the complete roof, walls, doorway and exit", "shelter")

    def build(self, observation, memory=None, context=None):
        """Return current facts, actionable prerequisites and explicit unmet goals."""
        observation = observation if isinstance(observation, dict) else {}
        context = context if isinstance(context, dict) else {}
        memory = memory if isinstance(memory, dict) else {}
        status = observation.get("status") or {}
        if not isinstance(status, dict):
            status = {}
        raw_counts = observation.get("counts")
        inventory_known = (isinstance(raw_counts, dict) and all(isinstance(name, str) and type(count) is int and count >= 0
                           for name, count in raw_counts.items()))
        if "counts" not in observation:
            inventory_known = isinstance(observation.get("inventory"), list) and all(
                isinstance(item, dict) and isinstance(item.get("name"), str) and type(item.get("count")) is int and item["count"] >= 0
                for item in observation["inventory"])
        fresh = (observation.get("fresh") is True and observation.get("stale") is not True
                 and status.get("ready") is True and status.get("alive") is True
                 and not status.get("busy") and inventory_known
                 and status.get("dimension") in {"overworld", "minecraft:overworld"}
                 and status.get("gamemode") == "survival" and self._pos(status.get("pos")) is not None
                 and self.rules._number(status.get("health")) and self.rules._number(status.get("food")))
        counts = self.rules._counts(observation) if fresh else {}
        dimension = status.get("dimension")
        evidence = observation.get("daily_evidence") or {}
        evidence = evidence if isinstance(evidence, dict) else {}
        caps = context.get("capabilities", DAILY_SKILLS)
        caps = set(caps) if isinstance(caps, (list, tuple, set, frozenset)) else set()
        tasks = []

        def task(task_id, priority, satisfied, missing=(), action=None, depends_on=(), proof=None, reason=None, waiting=False):
            if fresh:
                action = self._foliage_hint(action, observation, context, caps)
                action = self._batch_hint(action, observation, counts, context, caps)
            if action and action["name"] not in caps:
                reason, action = "unsupported_skill:" + action["name"], None
            state = "satisfied" if fresh and satisfied else "ready" if fresh and action else "waiting" if waiting else "blocked"
            if not fresh:
                state, reason, action = "waiting", "fresh_living_overworld_observation_required", None
            if state == "ready" and context.get("busy") is True and context.get("active_task_id") == task_id:
                state = "running"
            tasks.append({"id": task_id, "status": state, "priority": priority, "goal": task_id,
                          "depends_on": list(depends_on), "evidence": proof or {}, "missing": list(missing),
                          "blocked_reason": None if state in {"satisfied", "ready", "running"} else reason or "no_verified_action",
                          "next_action": None if state == "satisfied" else action})

        if not fresh:
            for task_id in TASK_IDS:
                task(task_id, 100 if task_id == "recover_drops" else 0, False)
            return {"phase": "observe", "active_task_id": None, "tasks": tasks, "maintenance_ready": False,
                    "needs": ["fresh_observation"], "revalidate_candidates": []}

        drops = None
        if context.get("death_count", 0) > 0:
            for row in self._safe_rows(observation, entities=True, context=context):
                item = self._drop_name(row)
                entity_id = row.get("id", row.get("entity_id"))
                if isinstance(item, str) and type(entity_id) is int:
                    if math.dist(status["pos"], row["pos"]) > 8:
                        continue
                    drops = self._action("collect_drops", {"entity_id": entity_id, "expected_item": item},
                                         "Collect one currently observed safe drop after death", "recover_drops")
                    break
        task("recover_drops", 100, context.get("death_count", 0) == 0, action=drops,
             reason="No currently visible safe drop; do not replay a route to the death location", waiting=True)

        food = sum(counts.get(name, 0) for name in SAFE_FOOD)
        task("food_reserve", 95, food >= self.targets["food_reserve"],
             missing=[{"food_items": max(0, self.targets["food_reserve"] - food)}],
             action=self._food_action(observation, counts, context), proof={"food_items": food},
             reason="No safe mature crop/passive animal or ready cooking input is currently available")

        wood = sum(counts.get(name, 0) for name in LOG_NAMES) + sum(counts.get(name, 0) for name in PLANK_NAMES) // 4
        task("wood_reserve", 65, wood >= self.targets["wood_reserve"],
             missing=[{"log_equivalents": max(0, self.targets["wood_reserve"] - wood)}],
             action=self._gather(LOG_NAMES, observation, context, "wood_reserve"), proof={"log_equivalents": wood})

        progress = self.rules.progress(observation)
        starter_missing = ["stone_" + kind for kind in TOOL_KINDS if not progress["stone_" + kind]]
        starter = self.rules.choose(observation, context)
        starter_action = self._action(starter.name, starter.args, starter.reason, "starter_tools") if starter.name not in {"wait", "goal_complete"} else None
        for missing in starter_missing:
            kind = missing.removeprefix("stone_")
            backup = self._take_supplies(observation, counts, {tier + "_" + kind for tier in ("stone", "iron", "diamond", "netherite")},
                                         1, "starter_tools", context)
            if backup:
                starter_action = backup
                break
        task("starter_tools", 85, not starter_missing, missing=starter_missing, action=starter_action,
             proof={name: progress[name] for name in ("stone_pickaxe", "stone_axe", "stone_sword")})

        furnaces = self._safe_rows(observation, {"furnace"})
        furnace_prerequisite = self.rules._craft_goal("furnace", observation, context)
        furnace_action = (self._place("furnace", observation, "furnace", context) if counts.get("furnace", 0)
                          else self._action(furnace_prerequisite.name, furnace_prerequisite.args,
                                            furnace_prerequisite.reason, "furnace")
                          if furnace_prerequisite.name not in {"wait", "goal_complete"} else None)
        task("furnace", 84, bool(furnaces), missing=["deployed_furnace"] if not furnaces else [], action=furnace_action,
             proof={"positions": [row["pos"] for row in furnaces[:4]]}, reason="Need eight cobblestone or a verified placement candidate")

        fuel = counts.get("coal", 0) + counts.get("charcoal", 0)
        fuel_action = self._take_supplies(observation, counts, {"coal", "charcoal"}, self.targets["fuel_reserve"], "fuel_reserve", context)
        fuel_action = fuel_action or self._smelt({"charcoal"}, observation, counts, "fuel_reserve", context)
        if fuel_action is None and progress["wooden_pickaxe"]:
            fuel_action = self._gather(("coal_ore", "deepslate_coal_ore"), observation, context, "fuel_reserve")
        task("fuel_reserve", 83, fuel >= self.targets["fuel_reserve"], missing=[{"coal_or_charcoal": max(0, self.targets["fuel_reserve"] - fuel)}],
             action=fuel_action, proof={"fuel_items": fuel}, reason="Fuel requires a usable pickaxe and visible coal or a later charcoal job")

        missing_iron = [kind for kind in TOOL_KINDS if not any(counts.get(tier + "_" + kind, 0) for tier in ("iron", "diamond", "netherite"))]
        iron_action = None
        if missing_iron:
            kind = missing_iron[0]
            ingots, sticks = (2, 1) if kind == "sword" else (3, 2)
            if counts.get("iron_ingot", 0) >= ingots and counts.get("stick", 0) >= sticks:
                workspace = self.rules._workspace(observation, context)
                iron_action = (self._action(workspace.name, workspace.args, workspace.reason, "iron_tools") if workspace
                               else self._action("craft_item", {"item": "iron_" + kind, "count": 1}, "Upgrade one tool using held iron", "iron_tools"))
            elif counts.get("iron_ingot", 0) >= ingots:
                planks = self.rules._need_planks(2, observation, context)
                iron_action = (self._action(planks.name, planks.args, planks.reason, "iron_tools") if planks
                               else self._action("craft_item", {"item": "stick", "count": 4}, "Prepare sticks for the iron tool", "iron_tools"))
            else:
                iron_action = self._smelt({"iron_ingot"}, observation, counts, "iron_tools", context)
                if iron_action is None and progress["stone_pickaxe"] and not counts.get("raw_iron", 0):
                    iron_action = self._gather(("iron_ore", "deepslate_iron_ore"), observation, context, "iron_tools", iron=True)
            backup = self._take_supplies(observation, counts, {tier + "_" + kind for tier in ("iron", "diamond", "netherite")},
                                         1, "iron_tools", context)
            iron_action = backup or self._take_supplies(observation, counts, {"iron_ingot"}, ingots, "iron_tools", context) or iron_action
        task("iron_tools", 55, not missing_iron, missing=["iron_" + kind for kind in missing_iron], action=iron_action,
             depends_on=("starter_tools", "furnace", "fuel_reserve"), reason="Need safe observed iron, fuel, smelting output and crafting materials")

        base = evidence.get("base_site")
        base_ok = self._verified(base, dimension)
        task("base_site", 75, base_ok, missing=[] if base_ok else ["fresh_base_site_survey"], proof=copy.deepcopy(base) if base_ok else {},
             action=self._base_candidate(observation, counts, context),
             reason="A single safe standing cell or remembered location does not verify a buildable base site")
        beds = self._safe_rows(observation, {row.get("name") for row in self._rows(observation.get("blocks")) if str(row.get("name", "")).endswith("_bed")})
        complete_beds = []
        offsets = {"north": (0, -1), "south": (0, 1), "west": (-1, 0), "east": (1, 0)}
        indexed = {tuple(row["pos"]): row for row in beds}
        for row in beds:
            props = row.get("properties") or {}
            if (props.get("part") != "foot" or not (props.get("occupied") is False or props.get("occupied") == "false")
                    or props.get("facing") not in offsets):
                continue
            dx, dz = offsets[props["facing"]]
            head = indexed.get((row["pos"][0] + dx, row["pos"][1], row["pos"][2] + dz))
            head_props = (head.get("properties") or {}) if head else {}
            if (head and head.get("name") == row["name"] and head_props.get("part") == "head"
                    and head_props.get("facing") == props["facing"]
                    and (head_props.get("occupied") is False or head_props.get("occupied") == "false")):
                complete_beds.append(row["pos"])
        held_bed = next((name for name, count in counts.items() if name.endswith("_bed") and count > 0), None)
        shelter = evidence.get("shelter")
        shelter_ok = self._verified(shelter, dimension) and shelter.get("shelter_verified") is True
        bed_action = self._place(held_bed, observation, "bed", context) if held_bed and base_ok and shelter_ok else None
        if not held_bed:
            wool = next((name for name, count in counts.items() if name.endswith("_wool") and count >= 3), None)
            if wool and sum(counts.get(name, 0) for name in PLANK_NAMES) >= 3:
                workspace = self.rules._workspace(observation, context)
                bed_action = (self._action(workspace.name, workspace.args, workspace.reason, "bed") if workspace
                              else self._action("craft_item", {"item": wool[:-5] + "_bed", "count": 1}, "Craft a bed from same-colour wool and planks", "bed"))
            elif wool is None:
                for row in self._safe_rows(observation, {"sheep"}, entities=True, context=context):
                    entity_id = row.get("id", row.get("entity_id"))
                    if type(entity_id) is int and self._can_hunt(observation, counts, row):
                        bed_action = self._action("hunt_animal", {"entity_id": entity_id, "expected_name": "sheep"},
                                                  "Acquire wool from one observed sheep; verify its actual drops", "bed")
                        break
        task("bed", 74, bool(complete_beds), missing=[] if complete_beds else ["complete_unoccupied_bed"], action=bed_action,
             depends_on=("base_site", "shelter"), proof={"positions": complete_beds[:4]}, reason="Need wool, planks and a safe two-block placement inside the completed shelter; held beds are not deployed beds")

        task("shelter", 73, shelter_ok, missing=[] if shelter_ok else ["fresh_roof_walls_exit_survey"],
             action=self._shelter_action(observation, counts, base, context),
             depends_on=("base_site", "wood_reserve"), proof=copy.deepcopy(shelter) if shelter_ok else {},
             reason="Shelter geometry must be freshly surveyed; a roof or remembered house alone is insufficient")

        storage_rows = self._containers(observation, context)
        storage = evidence.get("storage")
        storage_ok = bool(storage_rows) or self._verified(storage, dimension)
        chests = self._safe_rows(observation, {"chest", "barrel"}, context=context)
        storage_action = self._action("inspect_container", {"pos": chests[0]["pos"]}, "Verify the current storage contents and access", "storage") if chests else None
        if not chests and base_ok:
            storage_action = self._place("chest", observation, "storage", context) if counts.get("chest", 0) else (
                self._action("craft_item", {"item": "chest", "count": 1}, "Craft a chest from held planks", "storage")
                if sum(counts.get(name, 0) for name in PLANK_NAMES) >= 8 else None)
            if storage_action and storage_action["name"] == "craft_item":
                workspace = self.rules._workspace(observation, context)
                if workspace:
                    storage_action = self._action(workspace.name, workspace.args, workspace.reason, "storage")
        surplus = self._surplus(counts)
        if storage_rows and surplus:
            storage_action = self._action("store_items", {"pos": storage_rows[0]["pos"], "item": surplus[0], "count": surplus[1]},
                                          "Store excess materials while preserving working reserves", "storage")
        task("storage", 60, storage_ok and not (storage_rows and surplus), missing=[] if storage_ok else ["fresh_accessible_storage_snapshot"], action=storage_action,
             depends_on=("base_site",), proof={"positions": [row["pos"] for row in storage_rows[:4]]},
             reason="A placed or remembered chest still needs a current container inspection")

        production = evidence.get("food_production")
        production_ok = self._verified(production, dimension) and production.get("renewable") is True
        plant_action = None
        known_blocks = {tuple(row["pos"]): row for row in self._rows(observation.get("blocks")) + self._rows(observation.get("terrain"))
                        if self._pos(row.get("pos")) is not None}
        for row in self._safe_rows(observation, {"farmland"}, context=context):
            above = known_blocks.get((row["pos"][0], row["pos"][1] + 1, row["pos"][2]))
            if above is None or above.get("name") not in {"air", "cave_air", "void_air"}:
                continue
            seed = next((item for _, item in CROPS.values() if counts.get(item, 0) > 0), None)
            if seed:
                plant_action = self._action("plant_crop", {"pos": row["pos"], "item": seed}, "Validate and replant one existing farmland cell", "food_production")
                break
        if plant_action is None and base_ok and any(counts.get(seed, 0) for _, seed in CROPS.values()):
            hoe = any(count and name.endswith("_hoe") for name, count in counts.items())
            water = [row for row in known_blocks.values() if row.get("name") == "water" and (row.get("properties") or {}).get("level") == 0]
            for row in self._safe_rows(observation, {"dirt", "grass_block"}, context=context):
                above = known_blocks.get((row["pos"][0], row["pos"][1] + 1, row["pos"][2]))
                if (above is None or above.get("name") not in {"air", "cave_air", "void_air"}
                        or math.dist(row["pos"], base["pos"]) > 8
                        or not any(abs(w["pos"][0] - row["pos"][0]) <= 4 and abs(w["pos"][2] - row["pos"][2]) <= 4
                                   and w["pos"][1] in {row["pos"][1], row["pos"][1] + 1} for w in water)):
                    continue
                if hoe:
                    plant_action = self._action("till_soil", {"pos": row["pos"]}, "Prepare one observed irrigated soil cell", "food_production")
                elif counts.get("cobblestone", 0) >= 2 and counts.get("stick", 0) >= 2:
                    workspace = self.rules._workspace(observation, context)
                    plant_action = (self._action(workspace.name, workspace.args, workspace.reason, "food_production") if workspace
                                    else self._action("craft_item", {"item": "stone_hoe", "count": 1}, "Prepare a hoe for verified nearby soil", "food_production"))
                break
        if plant_action is None and base_ok and not any(counts.get(seed, 0) for _, seed in CROPS.values()):
            origin = status["pos"]
            blocks = self.rules._blocks(observation)
            blocked = self.rules._blocked(context)
            hostiles = self.rules._hostiles(observation, origin)
            for row in self._safe_rows(observation, {"short_grass", "tall_grass"}, context=context):
                if (row.get("fresh") is not True or row.get("can_see") is not True
                        or math.dist(origin, row["pos"]) > 4
                        or row["name"] == "tall_grass" and (row.get("properties") or {}).get("half") != "lower"
                        or not self.rules._safe_target(row, origin, blocks, blocked, hostiles)):
                    continue
                plant_action = self._action("gather_seeds", {"pos": row["pos"], "expected_name": row["name"]},
                    "Try one currently visible grass plant; removal alone is not seed acquisition and only inventory gain advances planting", "food_production")
                break
        task("food_production", 70, production_ok, missing=[] if production_ok else ["verified_renewable_food_source"], action=plant_action,
             depends_on=("base_site",), proof=copy.deepcopy(production) if production_ok else {},
             reason="Mature crops alone do not prove a renewable source; replanting and farm conditions need verification")

        required = {"food_reserve", "wood_reserve", "starter_tools", "furnace", "fuel_reserve", "iron_tools", "base_site", "bed", "shelter", "storage", "food_production"}
        maintenance_ready = all(row["status"] == "satisfied" for row in tasks if row["id"] in required)
        task("maintain", 10, False, missing=[row["id"] for row in tasks if row["id"] in required and row["status"] != "satisfied"],
             action=self._action("wait", {"seconds": self.targets["maintenance_interval_seconds"]},
                                 "Reobserve food, tools and the base; maintenance is continuous", "maintain") if maintenance_ready else None,
             depends_on=sorted(required), reason="Daily prerequisites remain unmet", waiting=maintenance_ready)
        ready = sorted((row for row in tasks if row["status"] in {"ready", "running"}), key=lambda row: (-row["priority"], TASK_IDS.index(row["id"])))
        active = ready[0]["id"] if ready else None
        candidates = [copy.deepcopy(row) for row in self._rows(memory.get("places"))
                      if row.get("dimension") == dimension and self._pos(row.get("pos")) is not None][:8]
        for row in candidates:
            row["requires_revalidation"] = True
        return {"phase": "maintain" if maintenance_ready else "establish", "active_task_id": active,
                "tasks": tasks, "maintenance_ready": maintenance_ready,
                "needs": [row["id"] for row in tasks if row["status"] not in {"satisfied"} and row["id"] != "maintain"],
                "revalidate_candidates": candidates}

    def choose(self, observation, memory=None, context=None):
        """Rules-mode convenience only; LLM mode receives the board as context."""
        safety = self.rules.choose(observation, context)
        if safety.goal in {"safety", "recover", "observe", "sleep"} or safety.name == "eat_food":
            return safety
        board = self.build(observation, memory, context)
        active = next((row for row in board["tasks"] if row["id"] == board["active_task_id"]), None)
        if active and active["next_action"]:
            action = active["next_action"]
            # Critically hungry bots may acquire food, but not start unrelated
            # resource work while the legacy safety planner asks for food.
            if safety.goal != "food" or active["id"] in {"food_reserve", "food_production"}:
                return Decision(action["name"], copy.deepcopy(action["args"]), action["reason"], action["goal"])
        return Decision("wait", {"seconds": 5}, "Daily goals need new evidence or an implemented safe action", "maintain")
