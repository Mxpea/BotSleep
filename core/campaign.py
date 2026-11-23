"""Local Java survival progression. Choices are derived from inventory and verified transitions."""

import copy
import json
import math
import time

from agent.survival_planner import Decision
from core.build_projection import wood_material
from core.domain import LOG_NAMES, PLANK_NAMES, SAFE_FOOD, extract_blocks, parse_pos
from core.production_recipes import CRAFT_ITEMS, wood_inputs
from core.survival_mainline import same_goal
from skills.hunting import HUNTING_TOOLS


EYE_RESERVE = 16
RETURN_HEALTH = 9
NETHER_RATIONS = 6
VALUABLE_DROP_WINDOW = 1200
RAW_TRAVEL_FOOD = frozenset({"beef", "porkchop", "mutton", "chicken", "rabbit", "cod", "salmon", "potato"})
EMERGENCY_FOOD = frozenset(SAFE_FOOD) | {"beef", "porkchop", "mutton"}
RECOVERY_GEAR = ("diamond_pickaxe", "iron_pickaxe", "stone_pickaxe", "wooden_pickaxe",
                 "iron_sword", "stone_sword", "shield", "raw_iron", "iron_ingot", "cobblestone",
                 "cooked_beef", "cooked_mutton", "cooked_porkchop", "bread")
EMERGENCY_MELEE = frozenset({"zombie", "zombie_villager", "husk", "spider", "cave_spider"})
RANGED_THREATS = frozenset({"skeleton", "stray", "bogged", "pillager"})
PICKAXES = ("wooden_pickaxe", "stone_pickaxe", "iron_pickaxe", "diamond_pickaxe", "netherite_pickaxe")
IRON_TOOLS = (PICKAXES[2:], ("iron_sword", "diamond_sword", "netherite_sword"))
DEFENSIVE_WEAPONS = frozenset(name for name in HUNTING_TOOLS if name.endswith(("_sword", "_axe")))
PORCH_POSTS = tuple((x, y, 5) for y in (0, 1) for x in (-2, 2))
PORCH_ROOF = ((-2, 2, 5), (2, 2, 5)) + tuple(
    (x, 2, z) for z in range(3, 6) for x in range(-2, 3) if z < 5 or abs(x) < 2)


def dimension(status):
    return str(status.get("dimension", "")).removeprefix("minecraft:").removeprefix("the_")


def scoped_episode_tail(journal):
    if journal.state.get("scope") != journal.scope:
        return
    try:
        with journal.events_path.open("rb") as stream:
            stream.seek(0, 2)
            start = max(0, stream.tell() - 2 * 1024 * 1024)
            stream.seek(start)
            if start:
                stream.readline()
            lines = stream.read().splitlines()
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row
    except OSError:
        return


def recent_mine_entry(journal, status):
    """Recover an already-issued deep-mine entry from the same scoped life."""
    origin = parse_pos(status.get("pos"))
    if origin is None:
        return None
    for row in scoped_episode_tail(journal):
        if row.get("event") in {"DEATH_OBSERVED", "DIMENSION_CHANGED"}:
            return None
        data = row.get("data") or {}
        if row.get("event") != "DECISION" or (data.get("decision") or {}).get("goal") != "deep_mine":
            continue
        before = data.get("status") or {}
        anchor = parse_pos(before.get("pos"))
        if (anchor is None or before.get("last_death") != status.get("last_death")
                or dimension(before) != dimension(status) or anchor[1] - origin[1] < 8 or math.dist(anchor, origin) > 96):
            return None
        return anchor
    return None


def recent_valuable_death(journal):
    site = None
    now = time.time()
    for row in scoped_episode_tail(journal):
        data = row.get("data") or {}
        if row.get("event") == "DIMENSION_CHANGED":
            break
        if row.get("event") == "DEATH_OBSERVED":
            site = data.get("last_death") or {}
            marker = site.get("t")
            if type(marker) is not int or not 0 <= now - marker / 1000 < VALUABLE_DROP_WINDOW:
                break
        if site and row.get("event") == "DECISION":
            status = data.get("status") or {}
            point, origin = parse_pos(site.get("dropped_items_at")), parse_pos(status.get("pos"))
            if (point is not None and origin is not None and dimension(status) == "overworld"
                    and math.dist(origin, point) <= 16 and any((data.get("counts") or {}).get(item)
                         for item in ("iron_pickaxe", "iron_sword", "diamond_pickaxe", "raw_iron", "iron_ingot"))):
                return site
    return None


class Campaign:
    def __init__(self, state, projection=None):
        self.state = state
        self.projection = projection

    def _decision(self, stage, name, args, reason):
        previous = self.state.get("view", {})
        last = previous.get("last_result") if previous.get("stage") == stage else None
        self.state["view"] = {"stage": stage, "reason": reason, "last_result": last}
        project = self.state.get("projection_project") or {}
        if self.projection and project:
            self.state["view"]["projection"] = {"preset": self.projection.name,
                **{key: project.get(key) for key in ("stage", "scan", "confirmed", "total", "remaining", "complete", "wood_species")}}
        return Decision(name, args, reason, "campaign")

    def action(self, stage, operation, reason, **args):
        return self._decision(stage, "campaign_action", {"operation": operation, **args}, reason)

    def _porch_action(self, status, counts, shelter):
        anchor = parse_pos(shelter.get("pos"), integer=True)
        origin, orientation = parse_pos(status.get("pos")), shelter.get("orientation")
        if (anchor is None or origin is None or orientation not in ("north", "east", "south", "west")
                or math.dist(origin, anchor) > 9 or not status.get("is_day") or status.get("time_of_day", 0) >= 10000):
            return None
        site = [anchor, orientation]
        progress = self.state.get("porch") or {}
        if progress.get("site") != site:
            progress = {}
        if progress.get("complete") or progress.get("blocked"):
            return None
        if progress.get("surveyed"):
            wood = progress["wood"]
            shortage = wood - sum(counts.get(item, 0) for item in PLANK_NAMES)
            if shortage > 0:
                species = max(sorted(PLANK_NAMES), key=lambda item: 4 * sum(
                    counts.get(source, 0) for source in wood_inputs(item[:-7])) + counts.get(item, 0))
                if not any(counts.get(source) for source in wood_inputs(species[:-7])) and not counts.get(species):
                    species = "oak_planks"
                return self.task("shelter", "craft", "Prepare wooden porch posts and a canopy", item=species, count=shortage)
            stone = progress["stone"] - counts.get("cobblestone", 0)
            if stone > 0:
                return self.task("shelter", "gather", "Prepare porch foundations", resource="cobblestone", count=stone)
        return self.action("shelter", "house_porch", "Extend the house with a roofed timber porch",
                           anchor=anchor, orientation=orientation)

    def task(self, stage, task, reason, **args):
        return self._decision(stage, "run_survival_task", {"task": task, **args}, reason)

    def wait(self, stage, reason, seconds=4):
        return self._decision(stage, "wait", {"seconds": seconds}, reason)

    def recover_task(self, decision, task, observation=None):
        """Leave a locally exhausted patch before starting the same goal again."""
        task_id = (task or {}).get("task_id")
        if (decision.name != "run_survival_task" or decision.goal != "campaign"
                or (task or {}).get("status") != "needs_ai" or not task_id
                or task_id == self.state.get("last_recovered_task")
                or not same_goal(task.get("args"), decision.args)):
            return decision
        operation = ("seek_food" if decision.args["task"] == "food" else "seek_ground_wood"
                     if decision.args.get("item") == "wooden_pickaxe" else "recover_stance")
        counts = (observation or {}).get("counts", {})
        if self.projection and self.state.get("view", {}).get("stage") == "projection":
            material = decision.args.get("item") or decision.args.get("resource")
            timber = wood_material(material)
            if material == "logs" or timber is not None:
                options = {}
                if timber is not None:
                    log = timber[0] + "_log"
                    options = {"log": log, "min_logs": counts.get(log, 0) + 1}
                return self.action("projection", "seek_ground_wood", "Find a new stand of timber for the projection",
                                   task_id=task_id, **options)
        if operation == "seek_food" and any(counts.get(item) for item in RAW_TRAVEL_FOOD):
            fuels = (*LOG_NAMES, *PLANK_NAMES, "coal", "charcoal")
            operation = "recover_stance" if any(counts.get(item) for item in fuels) else "recover_fuel"
        if decision.args["task"] == "shelter":
            return self.action(self.state["view"]["stage"], operation, "Scout farther ground after exhausting local shelter sites",
                               task_id=task_id, prefer_far=True)
        return self.action(self.state["view"]["stage"], operation,
                           "The previous local goal exhausted its search; find a different approach", task_id=task_id)

    def death_recovery(self, observation, journal=None):
        status, counts = observation["status"], observation["counts"]
        death = status.get("last_death") or {}
        life = death.get("t")
        if (not observation.get("fresh") or status.get("on_ground") is not True or status.get("health", 0) < 7
                or type(life) is not int or self._iron_ready(status, counts)):
            return None
        if journal is not None and self.state.get("death_history_life") != life:
            self.state["death_history_life"] = life
            self.state["prior_gear_site"] = recent_valuable_death(journal)
        historic = self.state.get("prior_gear_site") or {}
        if type(historic.get("t")) is int and 0 <= time.time() - historic["t"] / 1000 < VALUABLE_DROP_WINDOW:
            death = historic
            if not self.state.get("field_base_ready"):
                self.state["needs_field_base"] = True
        elif status["health"] < 16 or counts.get("stone_pickaxe", 0) and counts.get("stone_sword", 0):
            return None
        if status["health"] < 16 and (not status.get("is_day") or status.get("food", 0) < 16
                                     or (status.get("nearest_hostile") or {}).get("dist", 100) <= 12):
            return None
        marker = death.get("t")
        point, origin = parse_pos(death.get("dropped_items_at")), parse_pos(status.get("pos"))
        if (point is None or origin is None or type(marker) is not int
                or not 0 <= time.time() - marker / 1000 < (VALUABLE_DROP_WINDOW if death is historic else 235)
                or math.dist(point, origin) > (192 if death is historic else 96)):
            return None
        repair = self.state.get("death_gear") or {}
        if repair.get("marker") != marker or repair.get("progress_aware") is not True:
            repair = self.state["death_gear"] = {"marker": marker, "attempts": 0, "progress_aware": True}
        if repair["attempts"] >= 12 or repair.get("retry_after", 0) > time.time():
            return None
        return self.action("recovery", "recover_gear", "Look for current equipment drops before rebuilding",
                           death=point, marker=marker, life=life)

    def water_recovery(self, status):
        if (status.get("ready") is True and status.get("alive") is True and not status.get("busy")
                and (status.get("submerged") or status.get("in_water"))
                and not status.get("in_vehicle")
                and not status.get("in_lava") and not status.get("on_fire")):
            return self.action("recovery", "surface_water", "Rise to air and look for a checked bank")
        return None

    def mine_recovery(self, observation, journal):
        status = observation["status"]
        origin = parse_pos(status.get("pos"))
        if (not observation.get("fresh") or origin is None or status.get("on_ground") is not True
                or status.get("busy") or status.get("health", 0) < 10 or dimension(status) != "overworld"
                or (self.state.get("view") or {}).get("stage") not in {"water", "iron_gear", "supplies", "recovery"}):
            return None
        if self.state.get("mine_exit_retry", 0) > time.time():
            return None
        anchor = recent_mine_entry(journal, status)
        if anchor is None:
            self.state["mine_exit_retry"] = time.time() + 10
            return None
        return self.action("recovery", "exit_shaft", "Return to the recorded mine entrance before surface production",
                           anchor=anchor)

    def night_cover(self, observation):
        status, counts = observation["status"], observation["counts"]
        origin = parse_pos(status.get("pos"))
        if (not self.state.get("needs_field_base") or self.state.get("field_base_ready") or origin is None
                or not observation.get("fresh") or status.get("on_ground") is not True or status.get("health", 0) < 10
                or status.get("food", 0) < 14 or dimension(status) != "overworld"
                or self.state.get("cover_retry_after", 0) > time.time()):
            return None
        cell = list(map(math.floor, origin))
        existing = self.state.get("night_cover") or {}
        nearest = (status.get("nearest_hostile") or {}).get("dist")
        threat_near = type(nearest) in (int, float) and 0 <= nearest <= 12
        night = 10000 <= status.get("time_of_day", 0) < 22000
        if existing.get("anchor") == cell and existing.get("life") == status.get("last_death"):
            operation = "night_cover" if night and threat_near else "open_cover"
            return self.action("shelter", operation, "Recheck the small field shelter before the next step", anchor=cell)
        if night and threat_near and counts.get("dirt", 0) >= 10:
            return self.action("shelter", "night_cover", "Build a small checked enclosure while a hostile is nearby", anchor=cell)
        return None

    def food_recovery(self, safety, observation):
        status = observation["status"]
        food_wait = safety is None and status.get("health", 20) <= 8 or (
            safety is not None and safety.name == "wait" and safety.goal == "food_recovery")
        if (food_wait and status.get("on_ground") is True and status.get("health", 0) >= 5
                and status.get("food", 20) < 18 and not any(status.get(flag) for flag in ("in_water", "on_fire", "in_lava"))):
            return self.action("recovery", "emergency_food", "Look for edible drops or ripe berries so health can regenerate")
        return safety

    def melee_recovery(self, safety, observation, retreat, *, under_attack=False):
        status = observation["status"]
        enemy = status.get("nearest_hostile") or {}
        gap = enemy.get("dist")
        danger_radius = 16 if enemy.get("name") in RANGED_THREATS else 8
        if (safety is None or safety.name not in {"retreat_from", "wait"}
                or not observation.get("fresh") or dimension(status) != "overworld"
                or status.get("ready") is not True or status.get("alive") is not True or status.get("busy")
                or any(status.get(key) for key in ("in_water", "submerged", "in_lava", "on_fire", "in_vehicle", "is_sleeping"))
                or type(gap) not in (int, float) or not 0 <= gap <= danger_radius):
            return safety
        if (enemy.get("name") == "creeper" and type(enemy.get("id")) is int and gap <= 5
                and status.get("on_ground") is True and self.state.get("escape_retry_after", 0) <= time.time()):
            return self.action("recovery", "escape_pressure", "Leave the approaching creeper before it can explode",
                               attacker_id=enemy["id"], imminent_explosion=True)
        if (enemy.get("name") in EMERGENCY_MELEE and type(enemy.get("id")) is int and gap <= 2.65
                and (gap <= 1.6 or under_attack or (retreat or {}).get("search_basis"))
                and self.state.get("melee_retry_after", 0) <= time.time()):
            return self.action("recovery", "defend_melee", "Push back the close melee attacker, then recheck escape",
                               entity_id=enemy["id"], mob=enemy["name"])
        if (safety.name == "wait" and gap >= 2.65 and status.get("on_ground") is True
                and (retreat or {}).get("retry_after", 0) > time.time()
                and self.state.get("escape_retry_after", 0) <= time.time()):
            return self.action("recovery", "escape_pressure", "Check a short lateral exit while the attacker closes in")
        if safety.name == "wait" and safety.args.get("seconds", 0) > .25:
            return Decision("wait", {"seconds": .25}, "Recheck the advancing hostile and retreat corridor now", "safety")
        return safety

    @staticmethod
    def obsolete_opening_task(task, observation):
        args = task.get("args") or {}
        item = args.get("item")
        if task.get("status") not in {"running", "suspended"} or observation.get("fresh") is not True:
            return False
        counts = observation["counts"]
        if args.get("task") == "food":
            return sum(counts.get(name, 0) for name in SAFE_FOOD) >= Campaign._food_target(observation["status"], counts)
        if args.get("task") != "craft":
            return False
        if item == "stone_sword":
            return any(counts.get(tool) for tool in DEFENSIVE_WEAPONS)
        return item in PICKAXES[:2] and any(counts.get(pick) for pick in PICKAXES[PICKAXES.index(item) + 1:])

    @staticmethod
    def _iron_ready(status, counts):
        return (all(any(counts.get(item) for item in tools) for tools in IRON_TOOLS)
                and (status.get("offhand") or {}).get("name") == "shield")

    @staticmethod
    def _food_target(status, counts):
        if Campaign._iron_ready(status, counts) and counts.get("water_bucket") and counts.get("cobblestone", 0) >= 32:
            return NETHER_RATIONS
        return int(status["health"] < 15 or status["food"] < 18)

    def record(self, decision, result):
        data = result.data
        view = self.state.get("view", {})
        self.state["view"] = {**view, "last_result": result.error_code or result.message}
        operation = decision.args["operation"]
        if operation == "build_projection":
            if isinstance(data.get("projection"), dict):
                self.state["projection_project"] = data["projection"]
                if data["projection"].get("complete"):
                    self.state["field_base_ready"] = True
                self.state.get("projection_retry", {}).pop("until", None)
            elif result.error_code in {"projection_site_blocked", "projection_no_stance", "projection_no_support"}:
                project = self.state.get("projection_project") or {}
                if project.get("confirmed", 0) == 0:
                    self.state.pop("projection_project", None)
                    self.state["projection_relocate"] = True
                else:
                    self.state["projection_retry"] = {"until": time.time() + 20}
        if operation == "projection_relocate" and result.success and data.get("position") is not None:
            self.state.pop("projection_relocate", None)
        if decision.args["operation"] == "house_porch":
            if data.get("porch") is not None:
                self.state["porch"] = data["porch"]
            elif result.error_code in {"porch_site_occupied", "porch_no_stance"}:
                self.state["porch"] = {"site": [decision.args["anchor"], decision.args["orientation"]], "blocked": True}
        if decision.args["operation"] == "defend_melee":
            self.state["melee_retry_after"] = time.time() + .75
        if decision.args["operation"] == "escape_pressure":
            now = time.time()
            self.state["escape_retry_after"] = now + (.2 if decision.args.get("imminent_explosion") else 1)
            if data.get("escape_step") is not None:
                previous = self.state.get("escape_steps") or {}
                same_threat = previous.get("threat") == data.get("escape_threat_id")
                recent = [row for row in previous.get("positions", []) if row["until"] > now] if same_threat else []
                self.state["escape_steps"] = {"threat": data.get("escape_threat_id"),
                    "positions": (recent + [{"pos": data["escape_step"], "until": now + 4}])[-4:]}
        if decision.args["operation"] in {"seek_ground_wood", "recover_fuel"}:
            if data.get("wood_collected") or data.get("fuel_collected"):
                self.state["wood_search_steps"] = 0
                self.state.pop("wood_waypoint", None)
            elif "cleared_wood_sight" not in data:
                self.state["wood_search_steps"] = self.state.get("wood_search_steps", 0) + 1
            waypoint = parse_pos(data.get("wood_waypoint"))
            if waypoint is not None:
                self.state["wood_waypoint"] = waypoint
            if data.get("wood_waypoint_blocked"):
                self.state.pop("wood_waypoint", None)
        if decision.args["operation"] == "exit_shaft" and not data.get("sheltered_until_daylight"):
            self.state["mine_exit_retry"] = time.time() + 2
        if decision.args["operation"] in {"night_cover", "open_cover"}:
            if data.get("cover") is not None:
                self.state["night_cover"] = data["cover"]
            if data.get("cover_opened"):
                self.state.pop("night_cover", None)
            if not result.success:
                self.state["cover_retry_after"] = time.time() + 8
        if (root := parse_pos(data.get("blocked_wood"), integer=True)) is not None:
            now = time.time()
            previous = [row for row in self.state.get("blocked_wood", []) if row["until"] > now and row["pos"] != root]
            self.state["blocked_wood"] = (previous + [{"pos": root, "until": now + 90}])[-40:]
        if decision.args["operation"] == "recover_gear":
            repair = self.state.get("death_gear")
            if repair is not None:
                point = parse_pos(data.get("position"))
                if data.get("gear_deferred"):
                    repair["retry_after"] = time.time() + 20
                elif data.get("gear_search_done"):
                    repair["attempts"] = 12
                elif data.get("picked_up"):
                    repair["attempts"] = 0
                elif point is not None and math.dist(point, decision.args["death"]) + 2 < repair.get("distance", float("inf")):
                    repair.update(distance=math.dist(point, decision.args["death"]), attempts=0)
                else:
                    repair["attempts"] += 1
        if not result.success:
            point = data.get("reject_pos")
            if point is not None:
                self.state["rejected_steps"] = (self.state.get("rejected_steps", []) + [point])[-20:]
                until = time.time() + 20
                self.state["failed_steps"] = (self.state.get("failed_steps", []) + [{"pos": point, "until": until}])[-12:]
            self.state["search_index"] = (self.state.get("search_index", 0) + 1) % 8
            return
        if "survey_step" in data:
            self.state["rejected_steps"] = (self.state.get("rejected_steps", []) + [data["survey_step"]])[-20:]
            self.state.pop("spawner_waits", None)
        task_recovered = (data.get("wood_collected") is True or data.get("fuel_collected") is True
                          if decision.args["operation"] in {"seek_ground_wood", "recover_fuel"}
                          else "survey_step" in data or data.get("food_in_reach") is True)
        if decision.args.get("task_id") and task_recovered:
            self.state["last_recovered_task"] = decision.args["task_id"]
        if decision.args["operation"] == "seek_food":
            found = parse_pos(data.get("observed_supply"))
            if found is None:
                self.state.pop("food_focus", None)
            else:
                self.state["food_focus"] = {"pos": found, "expires_at": time.time() + 40}
        if data.get("awaiting_blaze"):
            self.state["spawner_waits"] = self.state.get("spawner_waits", 0) + 1
        if data.get("bartered"):
            self.state["trade_until"] = time.time() + 7
        if data.get("target") == "end_crystal":
            target = data["entity_id"]
            previous = self.state.get("crystal_shots", {})
            self.state["crystal_shots"] = {"id": target, "count": previous.get("count", 0) + 1
                                           if previous.get("id") == target else 1}
        elif "position" in data:
            self.state.pop("crystal_shots", None)
        for key in ("eye_waypoint", "end_eyes_required", "dragon_baseline", "dragon_seen", "dragon_defeated", "end_land"):
            if key in data:
                self.state[key] = copy.deepcopy(data[key])
        if "eye_sample" in data:
            self.state["eye_samples"] = (self.state.get("eye_samples", []) + [data["eye_sample"]])[-8:]
        if data.get("clear_eye_waypoint"):
            self.state.pop("eye_waypoint", None)
        if data.get("portal_active") is True or data.get("transition") == "nether":
            self.state["portal_seen"] = True
        transition = data.get("transition")
        if transition:
            self.state["rejected_steps"] = []
            self.state.pop("failed_steps", None)
            self.state.pop("trade_until", None)
        if transition == "end":
            self.state.pop("end_land", None)
        if transition == "nether":
            self.state["nether_arrival"] = list(data["arrival"])
            self.state["overworld_portal"] = list(data["portal"])
        if decision.args["operation"] == "leave_end" and transition == "overworld" and self.state.get("dragon_defeated"):
            self.state["returned"] = True

    def choose(self, observation):
        status, counts = observation["status"], observation["counts"]
        if (observation.get("fresh") is not True or observation.get("inventory_fresh", True) is not True
                or not status.get("ready") or not status.get("alive") or status.get("busy")):
            return self.wait("observe", "Wait for a complete ready observation")
        world = dimension(status)
        food = sum(counts.get(item, 0) for item in SAFE_FOOD)
        if world == "overworld" and self.state.get("returned"):
            return self.wait("completed", "Server confirmed the dragon advancement and the return to the overworld", 30)
        if world != "overworld":
            if status["food"] <= 16 and food:
                return self.action("resupply", "eat", "Eat from carried food before continuing")
            if world == "nether":
                return self._nether(status, counts, food)
            if world == "end":
                operation = "leave_end" if self.state.get("dragon_defeated") else "dragon"
                return self.action(operation, operation, "Check crystals, dragon and the exit against the current world")
            return self.wait("observe", "The current dimension is outside the campaign")
        construction = self.projection_action(observation)
        if construction is not None:
            return construction
        shelter = (observation.get("daily_evidence") or {}).get("shelter") or {}
        if (shelter.get("fresh") is True and shelter.get("verified") is True
                and shelter.get("shelter_verified") is True and dimension(shelter) == "overworld"):
            self.state["field_base_ready"] = True
            porch = self._porch_action(status, counts, shelter) if self.projection is None else None
            if porch is not None:
                return porch
        return self._overworld(status, counts, food)

    def projection_action(self, observation):
        if self.projection is None or observation.get("fresh") is not True:
            return None
        status, counts = observation["status"], observation["counts"]
        origin = parse_pos(status.get("pos"))
        if (origin is None or origin[1] < 48 or dimension(status) != "overworld"
                or status.get("on_ground") is not True or status.get("ready") is not True
                or status.get("alive") is not True or status.get("busy")
                or not status.get("is_day") or status.get("time_of_day", 24000) >= 10500
                or not any(counts.get(item) for item in PICKAXES[1:])
                or not any(counts.get(item) for item in DEFENSIVE_WEAPONS)
                or (self.state.get("projection_retry") or {}).get("until", 0) > time.time()):
            return None
        project = self.state.get("projection_project") or {}
        key = project.get("key") or []
        if len(key) != 4 or key[0] != self.projection.digest or key[3] != status.get("dimension"):
            project, key = {}, []
        if project.get("complete"):
            return None
        if self.state.get("projection_relocate"):
            return self.action("projection", "projection_relocate", "Find a more open site for the tracked projection")
        surveyed = project.get("scan") == len(self.projection.blocks)
        wood = project.get("wood_species")
        hint = self.projection.preferred_wood(counts, extract_blocks(observation)) if wood is None else None
        if surveyed and self.projection.uses_wood and "wood_species" in project and wood is None and hint is None:
            return self.task("projection", "gather", "Find timber before fixing the projection's wood species",
                             resource="logs", count=1)
        stock = project.get("stage_materials") or {} if surveyed and (wood or not self.projection.uses_wood) else {}
        for item, required in stock.items():
            missing = min(64, required - counts.get(item, 0))
            if missing <= 0:
                continue
            if item in CRAFT_ITEMS:
                return self.task("projection", "craft", f"Prepare {item} for the next projected layer", item=item, count=missing)
            if item == "glass":
                return self.task("projection", "smelt", "Smelt glass for the projected windows", item=item, count=missing)
            if item in LOG_NAMES or item in {"sand", "cobblestone"}:
                return self.task("projection", "gather", f"Collect {item} for the next projected layer",
                                 resource=item, count=missing)
            return self.wait("projection", f"Waiting for {missing} of {item}: this item has no automated supply recipe")
        anchor, orientation = key[1:3] if key else (list(map(math.floor, origin)), "north")
        return self.action("projection", "build_projection", "Survey and build the selected projection",
                           anchor=anchor, orientation=orientation, wood_hint=hint)

    def _nether(self, status, counts, food):
        eyes = counts.get("ender_eye", 0)
        powder = counts.get("blaze_powder", 0) + counts.get("blaze_rod", 0) * 2
        pearls = counts.get("ender_pearl", 0)
        if (status["health"] < RETURN_HEALTH or food < 3
                or eyes + min(powder, pearls) >= EYE_RESERVE
                or not any(counts.get(tier + "_sword") for tier in ("iron", "diamond", "netherite"))
                or not any(counts.get(tier + "_pickaxe") for tier in ("iron", "diamond", "netherite"))):
            return self.action("return_overworld", "leave_nether", "Return through the observed Nether portal")
        if powder + eyes < EYE_RESERVE:
            return self.action("blaze_rods", "loot_mob", "Locate a Nether fortress and collect blaze rods",
                               mob="blaze", item="blaze_rod")
        if time.time() < self.state.get("trade_until", 0):
            return self.wait("ender_pearls", "Wait for the verified gold trade to produce drops", 3)
        return self.action("ender_pearls", "loot_mob", "Collect ender pearls from endermen in the Nether",
                           mob="enderman", item="ender_pearl")

    def _overworld(self, status, counts, food):
        eyes = counts.get("ender_eye", 0)
        powder = counts.get("blaze_powder", 0)
        rods = counts.get("blaze_rod", 0)
        pearls = counts.get("ender_pearl", 0)
        for tiers in (PICKAXES, PICKAXES[1:]):
            if not any(counts.get(item) for item in tiers):
                return self.task("opening", "craft", "Make the next reachable pickaxe", item=tiers[0], count=1)
        if not any(counts.get(item) for item in DEFENSIVE_WEAPONS):
            return self.task("supplies", "craft", "Prepare a sword for close defense before descending", item="stone_sword", count=1)
        if (self.projection is None and self.state.get("needs_field_base") and not self.state.get("field_base_ready")
                and status.get("is_day") and status.get("time_of_day", 24000) < 10000):
            return self.task("shelter", "shelter", "Build a verified place to protect the equipment from another nighttime loss")
        food_target = self._food_target(status, counts)
        if food < food_target:
            seen = self.state.get("food_focus") or {}
            source, origin = parse_pos(seen.get("pos")), parse_pos(status.get("pos"))
            if (source is not None and origin is not None and 8 < math.dist(source, origin) <= 96
                    and time.time() < seen.get("expires_at", 0)
                    and not any(counts.get(item) for item in RAW_TRAVEL_FOOD)):
                return self.action("supplies", "seek_food", "Rescan and approach the observed animals before local hunting")
            return self.task("supplies", "food", "Stock travel food before leaving the overworld", count=food_target)
        if (status.get("offhand") or {}).get("name") != "shield":
            if counts.get("shield"):
                return self.action("iron_gear", "equip_shield", "Verify an equipped offhand shield")
            if counts.get("iron_ingot"):
                return self.task("iron_gear", "craft", "Use an available iron ingot for a defensive shield", item="shield", count=1)
        for equivalents in (*IRON_TOOLS, ("shield",)):
            item = equivalents[0]
            if not any(counts.get(name) for name in equivalents) and (status.get("offhand") or {}).get("name") != item:
                return self.task("iron_gear", "craft", "Make the equipment needed for the next dimension", item=item, count=1)
        if (status.get("offhand") or {}).get("name") != "shield":
            return self.action("iron_gear", "equip_shield", "Verify an equipped offhand shield")
        if not counts.get("water_bucket"):
            if not counts.get("bucket"):
                return self.task("water", "craft", "Make an empty bucket", item="bucket", count=1)
            return self.action("water", "fill_water", "Find and verify a water source")
        if counts.get("cobblestone", 0) < 32:
            return self.task("supplies", "gather", "Stock dry blocks for terrain and the arrival island",
                             resource="cobblestone", count=32 - counts.get("cobblestone", 0))
        eye_target = self.state.get("end_eyes_required")
        if type(eye_target) is not int or not 0 <= eye_target <= EYE_RESERVE:
            eye_target = 12 if self.state.get("eye_waypoint") else EYE_RESERVE
        if eyes < eye_target:
            deficit = eye_target - eyes
            if powder and pearls:
                return self.task("eyes", "craft", "Combine observed blaze powder and pearls", item="ender_eye",
                                 count=min(deficit, powder, pearls))
            if rods and pearls:
                return self.task("eyes", "craft", "Convert blaze rods to powder", item="blaze_powder",
                                 count=min(deficit, pearls, rods * 2))
            if not self.state.get("portal_seen") and not counts.get("flint_and_steel"):
                if not counts.get("flint"):
                    return self.action("nether_portal", "flint", "Collect flint from verified gravel")
                return self.task("nether_portal", "craft", "Make the portal igniter", item="flint_and_steel", count=1)
            return self.action("nether_portal", "enter_nether", "Find, build or enter the Nether portal")
        # Equip the End expedition in the overworld. Preserve previously checked
        # portal work when Eyes are consumed filling the actual frame.
        if not counts.get("bow") and not counts.get("crossbow"):
            if counts.get("string", 0) < 3:
                return self.action("end_equipment", "loot_mob", "Get string for a bow", mob="spider", item="string")
            return self.task("end_equipment", "craft", "Craft a bow", item="bow", count=1)
        if counts.get("arrow", 0) < 40:
            if not counts.get("flint"):
                return self.action("end_equipment", "flint", "Collect flint for arrows")
            if not counts.get("feather"):
                return self.action("end_equipment", "loot_mob", "Collect feathers from chickens", mob="chicken", item="feather")
            return self.task("end_equipment", "craft", "Craft a batch of arrows", item="arrow", count=4)
        return self.action("stronghold", "stronghold", "Verify the portal room or follow fresh Eye of Ender observations")
