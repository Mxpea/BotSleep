"""Current daily-production evidence, with bounded sequential world/window reads."""

import copy
import math
import time

from core.block_geometry import PASS_THROUGH, trace_block_visibility
from core.client import APIError
from core.night_home_route import inspect_exterior
from core.structure_spec import normalize_orientation, transform_rel_pos
from core.shelter_project import active_project
from core.survival_state import ObservationCollector
from skills.survival import SurvivalSkills
from skills.positioning import PositioningSkills
from skills.combat import combat_options


class DailyObservationCollector(ObservationCollector):
    FURNACES = {"furnace", "smoker", "blast_furnace"}
    CONTAINERS = {"chest", "barrel"}
    SEED_PLANTS = {"short_grass", "tall_grass"}
    def __init__(self, client, radius=24, *, low_health_threshold=8, mainline=False, campaign=False):
        super().__init__(client, radius, include_terrain=True, terrain_summary=not campaign, combat_mode="daily")
        self.context = {}
        self.low_health_threshold = low_health_threshold
        self.mainline = mainline
        self.campaign = campaign

    def set_context(self, context):
        self.context = copy.deepcopy(context) if isinstance(context, dict) else {}

    @staticmethod
    def _result(response):
        if not isinstance(response, dict) or response.get("ok") is not True or not isinstance(response.get("result"), dict):
            raise APIError("invalid_response", "Daily observation response is malformed")
        return response["result"]

    @classmethod
    def _block_pos(cls, value):
        pos = cls._position({"pos": value})
        if any(v != math.floor(v) for v in pos):
            raise APIError("invalid_response", "Daily block coordinates must be integers")
        return [int(v) for v in pos]

    def _read(self, pos, cancel_event, *, read_block=None):
        pos = self._block_pos(pos)
        self._check_cancel(cancel_event)
        read_block = read_block or self.client.get_block
        value = self._result(read_block(pos, timeout_seconds=2))
        self._check_cancel(cancel_event)
        if (not isinstance(value.get("name"), str) or not value["name"]
                or value.get("properties") is not None and not isinstance(value["properties"], dict)
                or "pos" in value and self._block_pos(value["pos"]) != pos):
            raise APIError("invalid_response", "Daily block evidence is malformed")
        return copy.deepcopy(value)

    @staticmethod
    def _slot(rows):
        if not isinstance(rows, list) or len(rows) > 1:
            raise APIError("invalid_response", "Furnace slot evidence is incomplete or oversized")
        if not rows:
            return {"name": None, "count": 0}
        item = rows[0]
        if (not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]
                or type(item.get("count")) is not int or item["count"] <= 0):
            raise APIError("invalid_response", "Window slot evidence is malformed")
        return {"name": item["name"], "count": item["count"]}

    def _rows(self, observation, names):
        unique = {}
        for row in observation.get("blocks", []):
            if not isinstance(row, dict) or row.get("name") not in names:
                continue
            pos = self._block_pos(row.get("pos"))
            unique.setdefault(tuple(pos), {**row, "pos": pos})
        return sorted(unique.values(), key=lambda row: math.dist(observation["status"]["pos"], row["pos"]))

    def _replace_block(self, observation, pos, block):
        row = {**copy.deepcopy(block), "pos": list(pos), "fresh": True, "source": "api_verified"}
        observation["blocks"] = [old for old in observation["blocks"]
                                 if not isinstance(old.get("pos"), (list, tuple)) or list(old["pos"]) != list(pos)] + [row]

    def _windows(self, observation, cancel_event):
        original = observation["status"]
        origin, dimension = original["pos"], original["dimension"]
        observation.update(furnaces=[], containers=[])
        if original.get("on_ground") is not True:
            return
        rows = [row for row in self._rows(observation, self.FURNACES | self.CONTAINERS)
                if math.dist(origin, row["pos"]) <= 3][:2]
        for row in rows:
            # window/view can approach before opening. Stay strictly inside its
            # 3-block radius and recheck immediately before this sequential read.
            status = self._status(original, cancel_event, grounded=True)
            if math.dist(status["pos"], row["pos"]) > 3:
                continue
            block = self._read(row["pos"], cancel_event)
            self._replace_block(observation, row["pos"], block)
            if block["name"] != row["name"]:
                continue
            try:
                window = self._result(self.client.window_view(row["pos"], timeout_seconds=5))
            except APIError as error:
                # No inventory transfer was requested. An unavailable snapshot
                # stays unavailable; never turn it into an empty furnace/chest.
                raise APIError("window_observation_uncertain", "Window inspection did not yield a confirmed snapshot",
                               details={"cause": error.code}) from None
            self._check_cancel(cancel_event)
            self._status(original, cancel_event, grounded=True)
            if self._read(row["pos"], cancel_event)["name"] != row["name"]:
                raise APIError("observation_changed", "Window station changed during inspection")
            sections = window.get("sections")
            if not isinstance(sections, dict) or window.get("target") != row["name"]:
                raise APIError("invalid_response", "Window does not match its observed station")
            common = {"pos": list(row["pos"]), "name": row["name"], "dimension": dimension,
                      "fresh": True, "verified": True, "source": "api_verified"}
            if row["name"] in self.FURNACES:
                info = window.get("info")
                if not isinstance(info, dict) or type(info.get("burning")) is not bool:
                    raise APIError("invalid_response", "Furnace burning state is unknown")
                observation["furnaces"].append({**common, **{key: self._slot(sections.get(key))
                    for key in ("input", "fuel", "output")}, "burning": info["burning"], "info": copy.deepcopy(info)})
            else:
                items = sections.get("container")
                if not isinstance(items, list) or len(items) > 256 or any(not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]
                        or type(item.get("count")) is not int or item["count"] <= 0 for item in items):
                    raise APIError("invalid_response", "Container inventory is malformed")
                observation["containers"].append({**common, "items": [{"name": item["name"], "count": item["count"]} for item in items]})
        if observation["containers"]:
            observation["daily_evidence"]["storage"] = copy.deepcopy(observation["containers"][0])

    def _farm(self, observation, cancel_event):
        crops = {"wheat": 7, "carrots": 7, "potatoes": 7, "beetroots": 3}
        rows = self._rows(observation, crops)[:8]
        verified = []
        for row in rows:
            block = self._read(row["pos"], cancel_event)
            self._replace_block(observation, row["pos"], block)
            age = (block.get("properties") or {}).get("age")
            try:
                valid_age = (type(age) is int or isinstance(age, str) and age.isascii() and age.isdigit()) and 0 <= int(age) <= crops[row["name"]]
            except (ValueError, TypeError):
                valid_age = False
            if block["name"] != row["name"] or not valid_age or not SurvivalSkills._safe_block(block):
                continue
            block.setdefault("properties", {})["age"] = int(age)
            self._replace_block(observation, row["pos"], block)
            x, y, z = row["pos"]
            soil_pos = [x, y - 1, z]
            soil = self._read(soil_pos, cancel_event)
            self._replace_block(observation, soil_pos, soil)
            if soil["name"] == "farmland" and SurvivalSkills._safe_block(soil):
                verified.append({"pos": row["pos"], "name": row["name"], "age": int(age),
                                 "farmland_pos": soil_pos, "farmland_verified": True})
        if len(verified) >= 4:
            observation["daily_evidence"]["food_production"] = {
                "fresh": True, "verified": True, "renewable": True, "dimension": observation["status"]["dimension"],
                "pos": verified[0]["pos"], "source": "api_verified", "growing_crops": verified,
                "verification_scope": "current_replantable_crop_patch", "lighting": "unknown",
                "water_source": "unknown", "sustained_yield_guaranteed": False}

    def _seed_plants(self, observation, cancel_event):
        """Keep grass out of the soil quota; visibility has its own small read budget."""
        self._check_cancel(cancel_event)
        rows = self.client.get_blocks(radius=min(self.radius, 8), types=sorted(self.SEED_PLANTS), limit=8)
        self._check_cancel(cancel_event)
        if not isinstance(rows, list):
            raise APIError("invalid_response", "Seed-plant search is malformed")
        cache = {}
        for row in rows[:8]:
            if not isinstance(row, dict) or row.get("name") not in self.SEED_PLANTS:
                raise APIError("invalid_response", "Seed-plant search contains an unexpected block")
            pos = self._block_pos(row.get("pos"))
            if len(cache) >= 48:
                break
            key = tuple(pos)
            if key not in cache:
                cache[key] = self._read(pos, cancel_event)
            current = copy.deepcopy(cache[key])
            current["can_see"] = None
            if current["name"] in self.SEED_PLANTS and SurvivalSkills._safe_block(current):
                sight = trace_block_visibility(observation["status"]["pos"], pos,
                    lambda point: self._read(point, cancel_event), cache=cache,
                    max_queries=min(12, 48 - len(cache)), max_distance=8)
                current["can_see"] = sight["visible"]
                current["visibility"] = sight
            self._replace_block(observation, pos, current)

    def _local_foliage(self, observation, cancel_event, *, read_block=None):
        """Observe neighbouring body cells; this does not authorize removing them."""
        from skills.daily_survival import DailySurvivalSkills
        names = DailySurvivalSkills.FOLIAGE
        origin = observation["status"]["pos"]
        x, y, z = self._cell(origin)
        cache, targets = {}, []

        def read(pos):
            try:
                return self._read(pos, cancel_event, read_block=read_block)
            except APIError as error:
                if error.code != "not_loaded":
                    raise
                return None

        # Cardinal neighbours come first because the clearing skill only accepts
        # them. Diagonals remain diagnostic context, never implied action targets.
        offsets = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))
        for dx, dz in offsets:
            for dy in (0, 1):
                pos = [x + dx, y + dy, z + dz]
                current = read(pos)
                cache[tuple(pos)] = current
                old_foliage = any(row.get("name") in names and row.get("pos") == pos for row in observation["blocks"])
                if current is None:
                    if old_foliage:
                        observation["blocks"] = [row for row in observation["blocks"] if row.get("pos") != pos]
                elif current["name"] in names:
                    targets.append((pos, current))
                elif old_foliage:
                    self._replace_block(observation, pos, current)
        for pos, current in targets:
            current = copy.deepcopy(current)
            props = current.get("properties") or {}
            known_dry = current["name"] == "vine" or props.get("waterlogged") is False or props.get("waterlogged") == "false"
            sight = {"state": "unknown", "visible": None, "reason": "foliage_state_unverified"}
            if known_dry and SurvivalSkills._safe_block(current):
                sight = trace_block_visibility(origin, pos, read, cache=cache,
                    max_queries=min(8, 32 - len(cache)), max_distance=4)
            current.update(can_see=sight["visible"], visibility=sight)
            self._replace_block(observation, pos, current)

    def _food_neighborhoods(self, observation, cancel_event):
        """Expose at most two freshly checked immediate-food neighbourhoods."""
        status = observation["status"]
        health, food, day_time = (status.get(key) for key in ("health", "food", "time_of_day"))
        if (any(type(value) not in (int, float) or not math.isfinite(value) for value in (health, food, day_time))
                or not 0 < health <= self.low_health_threshold or not 0 <= food < 18 or not 0 <= day_time < 11000
                or status.get("is_day") is not True or status.get("thundering") is not False
                or status.get("on_ground") is not True):
            return
        from skills.daily_survival import DailySurvivalSkills
        candidates = {}
        for row in self._rows(observation, {"carrots", "potatoes", "beetroots"}):
            age = (row.get("properties") or {}).get("age")
            maximum = 3 if row["name"] == "beetroots" else 7
            if row.get("fresh") is True and type(age) is int and age == maximum:
                candidates[tuple(row["pos"])] = row["name"]
        for category in ("containers", "furnaces"):
            for row in observation.get(category, []):
                if (row.get("fresh") is not True or row.get("verified") is not True
                        or row.get("dimension") != status["dimension"]):
                    continue
                items = row.get("items", []) if category == "containers" else [row.get("output") or {}]
                if any(item.get("name") in DailySurvivalSkills.FOODS
                       and type(item.get("count")) is int and item["count"] > 0 for item in items):
                    candidates[tuple(self._block_pos(row["pos"]))] = row["name"]
        nearby = sorted((pos for pos in candidates if math.dist(status["pos"], pos) <= 2),
                        key=lambda pos: math.dist(status["pos"], pos))[:2]
        for pos in nearby:
            current = self._read(pos, cancel_event)
            if current["name"] != candidates[pos]:
                raise APIError("observation_changed", "Immediate food target changed during inspection")
            if current["name"] in {"carrots", "potatoes", "beetroots"}:
                age = (current.get("properties") or {}).get("age")
                maximum = 3 if current["name"] == "beetroots" else 7
                if not ((type(age) is int and age == maximum) or age == str(maximum)):
                    raise APIError("observation_changed", "Immediate food crop is no longer confirmed mature")
                current["properties"]["age"] = maximum
            self._replace_block(observation, pos, current)
            x, y, z = pos
            for dx, dy, dz in ((-1, 0, 0), (1, 0, 0), (0, -1, 0), (0, 1, 0), (0, 0, -1), (0, 0, 1)):
                cell = [x + dx, y + dy, z + dz]
                self._replace_block(observation, cell, self._read(cell, cancel_event))

    def _resource_visibility(self, observation, cancel_event, *, read_block=None):
        status = observation["status"]
        task = self.context.get("task_execution") or {}
        active = task.get("status") in {"running", "suspended", "needs_ai"}
        wooden_pickaxe_goal = task.get("task") == "craft" and (task.get("args") or {}).get("item") == "wooden_pickaxe"
        wood_first = (self.mainline and not self.campaign and (not active or wooden_pickaxe_goal)
                      and not self.context.get("blocked_targets")
                      and status.get("dimension") in {"overworld", "minecraft:overworld"}
                      and status.get("on_ground") is True
                      and not any(count and name.endswith("_pickaxe") for name, count in observation["counts"].items()))
        reachable_logs = super()._resource_visibility(observation, cancel_event, read_block=read_block, wood_first=wood_first)
        health, food = status.get("health"), status.get("food")
        if (reachable_logs and wooden_pickaxe_goal and task.get("status") == "running"
                and type(health) in (int, float) and health > self.low_health_threshold
                and type(food) in (int, float) and food >= 18
                and status.get("is_day") is True and status.get("thundering") is False):
            observation["defer_seed_survey"] = True

    def _ground_log(self, obs, cancel_event):
        """Inspect a rooted trunk and a short level approach ending in actual hand reach."""
        if not self.mainline:
            return
        status, origin = obs["status"], obs["status"]["pos"]
        x, y, z = self._cell(origin)
        if (obs.get("local_geometry_origin") != origin or abs(origin[1] - y) > .05
                or status.get("is_day") is not True or status.get("thundering") is not False
                or status.get("time_of_day", 24000) >= 11000 or status.get("on_ground") is not True
                or status.get("health", 0) <= self.low_health_threshold or status.get("food", 0) <= 6):
            return
        rows = self._rows(obs, SurvivalSkills.LOGS)
        roots = [row for row in rows if row["pos"][1] == y and math.dist(origin, row["pos"]) <= 8][:3]
        if not roots:
            return
        cache = {}
        deadline = time.monotonic() + 2

        def read(point):
            key = tuple(point)
            if key not in cache:
                if len(cache) >= 80 or time.monotonic() >= deadline:
                    raise APIError("survey_budget", "Ground log survey reached its read budget")
                cache[key] = self._read(list(key), cancel_event)
            return cache[key]

        def clear(col):
            cx, cz = col
            support = read((cx, y - 1, cz))
            if support["name"] not in PositioningSkills.EDGE_GROUND or not SurvivalSkills._safe_block(support):
                return False
            for level in (y, y + 1):
                body = read((cx, level, cz))
                if body["name"] not in PASS_THROUGH or not SurvivalSkills._safe_block(body):
                    return False
            return True

        try:
            if not all(clear(col) for col in PositioningSkills._columns(origin, origin)):
                return
            for row in roots:
                target = row["pos"]
                tx, _, tz = target
                base, log = read((tx, y - 1, tz)), read(target)
                if (base["name"] not in PositioningSkills.EDGE_GROUND or not SurvivalSkills._safe_block(base)
                        or log["name"] != row["name"] or log.get("diggable") is not True):
                    continue
                # Only level ground within six cardinal steps, first trying the
                # nearest reachable stance. No proposed waypoint is replayed.
                stances = sorted((abs(cx-x) + abs(cz-z), cx, cz)
                    for cx in range(tx-4, tx+4) for cz in range(tz-4, tz+4)
                    if (cx, cz) != (tx, tz) and abs(cx-x) + abs(cz-z) <= 6
                    and math.dist([cx+.5, y, cz+.5], target) <= 4)
                for steps, cx, cz in stances:
                    stance = [cx+.5, y, cz+.5] if steps else origin
                    if (math.dist(stance, target) > 4 or not clear((cx, cz))
                            or not steps and log.get("can_dig", log.get("harvest_tool_ok")) is not True):
                        continue
                    for axes in ((0, 1), (1, 0)):
                        p, path = [x, z], []
                        for axis in axes:
                            goal = (cx, cz)[axis]
                            while p[axis] != goal:
                                p[axis] += 1 if goal > p[axis] else -1
                                path.append(tuple(p))
                        first = [path[0][0]+.5, y, path[0][1]+.5] if path else origin
                        if (not all(clear(col) for col in PositioningSkills._columns(origin, first))
                                or not all(clear(col) for col in path)):
                            continue
                        sight = trace_block_visibility(stance, target, read, max_queries=20, max_distance=6)
                        if sight["visible"] is not True:
                            break  # The other axis order ends at the same viewing angle.
                        obs["ground_log_approach"] = {"origin": list(origin), "target": target,
                            "name": row["name"], "stance": list(stance), "next": first if path else None,
                            "ground": base["name"], "steps": steps, "visibility": sight, "verified": True}
                        return
        except APIError as error:
            if error.code not in {"not_loaded", "survey_budget", "request_timeout"}:
                raise

    def _home(self, observation, cancel_event):
        project = active_project(self.context.get("shelter_project")) or self.context.get("base_project")
        status = observation["status"]
        if not isinstance(project, dict) or project.get("dimension") != status["dimension"]:
            return
        try:
            pos = self._block_pos(project.get("pos"))
        except APIError:
            return
        if math.dist(status["pos"], pos) > 16:
            return
        orientation = project.get("orientation")
        norm_orient = normalize_orientation(orientation) if isinstance(orientation, str) else "north"
        inspect_args = {"pos": pos}
        if isinstance(orientation, str) and norm_orient != "north":
            inspect_args["orientation"] = norm_orient
        from skills.shelter import ShelterSkills
        result = ShelterSkills(self.client).execute("inspect_shelter", inspect_args, cancel_event=cancel_event)
        self._check_cancel(cancel_event)
        if not result.success:
            observation["shelter_error"] = {"error_code": result.error_code, "fresh": False}
            return
        data = copy.deepcopy(result.data)
        if (data.get("fresh") is not True or data.get("dimension") != status["dimension"]
                or data.get("pos") != pos or type(data.get("inside")) is not bool
                or not isinstance(data.get("geometry"), dict)):
            raise APIError("invalid_response", "Shelter inspection is stale, mismatched or incomplete")
        self._status(status, cancel_event)
        data["source"] = "api_verified"
        gap = data.get("next_gap")
        if isinstance(gap, dict):
            target = self._block_pos(gap.get("pos"))
            sight = trace_block_visibility(status["pos"], target, lambda p: self._read(p, cancel_event), max_queries=16, max_distance=4.5)
            data["visibility"] = sight
            data["needs_position"] = (status.get("on_ground") is not True or abs(status["pos"][1] - pos[1]) > .15
                                      or math.dist(status["pos"], target) > 3.5 or sight["visible"] is not True
                                      or ShelterSkills._body_intersects(target, status["pos"]))
        observation["shelter"] = data
        geometry = data.get("geometry") or {}
        if (geometry.get("floor_verified") is True and geometry.get("exit_verified") is True
                and data.get("hazards") == [] and data.get("occupied_blueprint_cells") == []):
            observation["daily_evidence"]["base_site"] = {"pos": pos, "dimension": status["dimension"],
                "orientation": norm_orient, "verified": True, "fresh": True, "source": "api_verified"}
        required = ("floor_verified", "walls_complete", "roof_complete", "exit_verified")
        door = data.get("door") or {}
        if (data.get("structure_complete") is True and all(geometry.get(key) is True for key in required)
                and door.get("frame_verified") is True and type(door.get("open")) is bool):
            # Preserve the skill's AABB-based inside result, not floor rounding.
            observation["daily_evidence"]["shelter"] = {**data, "verified": True,
                "geometry_complete": data.get("shelter_verified") is True, "structure_complete": True}
            route = self._return_route(status, pos, cancel_event, orientation=norm_orient) if not data["inside"] or door["open"] else None
            if route:
                observation["daily_evidence"]["shelter"]["return_route"] = route
            if not data["inside"]:
                outside = inspect_exterior(self, observation, data, cancel_event)
                if outside:
                    observation["daily_evidence"]["shelter"]["exterior_route"] = outside

    def _return_route(self, status, center, cancel_event, *, orientation="north"):
        origin, center = self._position(status), self._block_pos(center)
        norm_orient = normalize_orientation(orientation)
        start = self._cell(origin)
        if (status.get("on_ground") is not True or abs(origin[1] - start[1]) > .1
                or start[1] != center[1] or abs(start[0] - center[0]) + abs(start[2] - center[2]) > 4):
            return None
        cache = {}
        blocked_door = None
        checked_door = None
        def read(pos):
            key = tuple(pos)
            if key not in cache:
                if len(cache) >= 36:
                    raise APIError("observation_changed", "Return-route observation budget exhausted")
                cache[key] = self._read(pos, cancel_event)
            return cache[key]
        def clear_body(pos):
            nonlocal blocked_door, checked_door
            block = read(pos)
            if not SurvivalSkills._safe_block(block):
                return False
            if block["name"] in self.PASSABLE:
                return True
            from skills.shelter import ShelterSkills
            lower = tuple(transform_rel_pos((0, 0, 2), center, norm_orient, integer=True))
            upper = tuple(transform_rel_pos((0, 1, 2), center, norm_orient, integer=True))
            if block["name"] not in ShelterSkills.DOORS or tuple(pos) not in (lower, upper):
                return False
            if checked_door is None:
                halves = {lower: read(lower), upper: read(upper)}
                if not all(SurvivalSkills._safe_block(value) for value in halves.values()):
                    return False
                checked_door = ShelterSkills(self.client)._door(center, halves, orientation=norm_orient)
            if not checked_door["frame_verified"] or type(checked_door["open"]) is not bool:
                return False
            if checked_door["open"] is False:
                blocked_door = {"pos": list(lower), "verified": True, "open": False}
            return True
        def valid_cell(cell):
            ground = read([cell[0], cell[1] - 1, cell[2]])
            return (ground["name"] in SurvivalSkills.SUPPORT and SurvivalSkills._safe_block(ground)
                    and clear_body(cell) and clear_body([cell[0], cell[1] + 1, cell[2]]))
        # The starting support and the rounded body's adjacent voxels matter;
        # validating only destination cells would authorize leaving a bad start.
        if not valid_cell(start):
            return None
        for x in range(math.floor(origin[0] - .36), math.floor(origin[0] + .36) + 1):
            for z in range(math.floor(origin[2] - .36), math.floor(origin[2] + .36) + 1):
                if not valid_cell((x, start[1], z)):
                    return None
        path, current = [], list(start)
        for axis in (0, 2):
            while current[axis] != center[axis]:
                current = current.copy()
                current[axis] += 1 if center[axis] > current[axis] else -1
                if not valid_cell(current):
                    return None
                path.append(list(current))
        if not path or blocked_door and blocked_door["pos"] not in path:
            return None
        result = {"fresh": True, "verified": blocked_door is None, "dimension": status["dimension"],
                "source": "api_verified", "origin": list(origin), "path": path,
                "start_verified": True, "body_and_support_verified": blocked_door is None,
                "all_other_body_and_support_verified": True, "actual_path_verified": False}
        if blocked_door:
            result["blocked_door"] = blocked_door
        return result

    def collect(self, cancel_event=None, *, inspect_windows=True):
        read_geometry = self._geometry_reader(cancel_event)
        observation = super().collect(cancel_event, _read_geometry_block=read_geometry)
        status = observation["status"]
        observation["daily_evidence"] = {}
        if status.get("ready") is not True or status.get("alive") is not True or status.get("busy"):
            return observation
        if "busy" not in status or status["busy"] is not None and status["busy"] is not False:
            raise APIError("observation_changed", "Backend idle state is unknown")
        self._collect_footing(observation, cancel_event)
        resources = [("iron_ore", "deepslate_iron_ore", "diamond_ore", "deepslate_diamond_ore",
                      "dead_bush", "chest", "barrel", "smoker", "blast_furnace",
                      "obsidian", "crying_obsidian", "gravel", "nether_portal")]
        if self.campaign:
            resources.append(("sand",))
        if not self.campaign:
            resources.append(("wheat", "carrots", "potatoes", "beetroots", "farmland",
                              "hay_block", "sweet_berry_bush", "grass_block", "dirt"))
        for types in resources:
            self._check_cancel(cancel_event)
            rows = self.client.get_blocks(radius=self.radius, types=list(types), limit=48)
            self._check_cancel(cancel_event)
            if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("name"), str) for row in rows):
                raise APIError("invalid_response", "Daily resource rows are malformed")
            for row in rows:
                self._block_pos(row.get("pos"))
            observation["blocks"].extend(rows)
        if inspect_windows:
            self._windows(observation, cancel_event)
        if not self.campaign:
            self._farm(observation, cancel_event)
            if not observation.pop("defer_seed_survey", False):
                self._seed_plants(observation, cancel_event)
        self._food_neighborhoods(observation, cancel_event)
        self._local_foliage(observation, cancel_event, read_block=read_geometry)
        self._ground_log(observation, cancel_event)
        self._home(observation, cancel_event)
        observation["status"] = self._status(status, cancel_event)
        observation["combat"] = combat_options(observation)
        return observation
