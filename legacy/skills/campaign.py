"""Bounded local operations for the Java survival campaign."""

import math
import re
import time

from core.block_geometry import PASS_THROUGH, trace_block_visibility
from core.campaign import EMERGENCY_FOOD, EMERGENCY_MELEE, PORCH_POSTS, PORCH_ROOF, RANGED_THREATS, RECOVERY_GEAR, dimension
from core.client import APIError
from core.domain import LOG_NAMES, PLANK_NAMES, SAFE_FOOD, parse_pos
from core.structure_spec import transform_rel_pos
from integrations.ninjabrain.observations import EyeObservationCollector, ObservationError
from skills import SkillResult
from skills.action_workflow import WorkflowGuard, WorkflowStepError, WorkflowUncertainError
from skills.cave_explore import CaveExploreSkill
from skills.combat import clear_entity_ray
from skills.mining import MineShaftSkill
from skills.portal_speedrun import NetherPortalSpeedrunSkill
from skills.shelter import ShelterSkills
from skills.survival import SurvivalSkills


AIR = SurvivalSkills.AIR | PASS_THROUGH
SUPPORT = SurvivalSkills.SUPPORT | {
    "bedrock", "obsidian", "netherrack", "nether_bricks", "red_nether_bricks", "blackstone",
    "basalt", "smooth_basalt", "end_stone", "end_stone_bricks", "stone_bricks", "mossy_stone_bricks",
    "cracked_stone_bricks", "soul_soil", "soul_sand", "crimson_nylium", "warped_nylium",
}
DRAGON_ADVANCEMENT = "minecraft:end/kill_dragon"
HOSTS = {"blaze": "blaze_rod", "enderman": "ender_pearl", "spider": "string", "chicken": "feather"}
DIRECTIONS = ((1, 0), (0, 1), (-1, 0), (0, -1), (1, 1), (-1, 1), (-1, -1), (1, -1))


def eye_search_target(origin, sample, previous):
    """Intersect independent forward rays, or take another bounded leg."""
    angle = math.radians(sample["bearing_deg"])
    dx, dz = -math.sin(angle), math.cos(angle)
    for old in reversed(previous):
        theta = math.radians(old["bearing_deg"])
        ox, oz = -math.sin(theta), math.cos(theta)
        x, z = old["x"] - sample["x"], old["z"] - sample["z"]
        cross = dx * oz - dz * ox
        uncertainty = math.radians(max(3, 2 * (sample["sigma_deg"] + old["sigma_deg"])))
        if math.hypot(x, z) < 24 or abs(cross) <= math.sin(uncertainty):
            continue
        distance, old_distance = (x * oz - z * ox) / cross, (x * dz - z * dx) / cross
        if 8 <= distance <= 2500 and old_distance >= 0:
            distance = min(320, distance)
            return [sample["x"] + dx * distance, origin[1], sample["z"] + dz * distance]
    lateral = 48 if not previous else 0
    return [origin[0] + dx * 160 + dz * lateral, origin[1], origin[2] + dz * 160 - dx * lateral]


class CampaignSkills:
    def __init__(self, client, *, memory=None, projection=None):
        self.client = client
        self.memory = memory
        from skills.projection import ProjectionBuilder
        self.projection_builder = ProjectionBuilder(self, projection) if projection else None

    def execute(self, args, state, cancel_event=None):
        handlers = {
            "fill_water": self._fill_water, "flint": self._flint, "eat": self._eat, "equip_shield": self._equip_shield,
            "enter_nether": self._enter_nether, "leave_nether": self._leave_nether,
            "loot_mob": self._loot_mob, "stronghold": self._stronghold,
            "dragon": self._dragon, "leave_end": self._leave_end,
            "recover_stance": self._recover_stance, "seek_food": self._seek_food,
            "recover_gear": self._recover_gear,
            "surface_water": self._surface_water,
            "seek_ground_wood": self._seek_ground_wood,
            "recover_fuel": self._recover_fuel,
            "emergency_food": self._emergency_food,
            "defend_melee": self._defend_melee,
            "escape_pressure": self._escape_pressure,
            "exit_shaft": self._exit_shaft,
            "night_cover": self._night_cover, "open_cover": self._night_cover,
            "house_porch": self._house_porch,
            "build_projection": self._build_projection, "projection_relocate": self._projection_relocate,
        }
        operation = args["operation"]
        defense = operation in {"defend_melee", "escape_pressure"}
        guard = WorkflowGuard(self.client, cancel_event=cancel_event, timeout_seconds=4 if defense else 24,
                              max_reads=650 if operation == "build_projection" else 360, allow_damage=True,
                              critical_health=0 if defense or operation == "surface_water" else 5)
        try:
            status = guard.check_status()
            origin = parse_pos(status.get("pos"))
            requires_ground = operation not in {"surface_water", "defend_melee"}
            if (origin is None or status.get("alive") is not True or status.get("ready") is not True
                    or status.get("gamemode") != "survival" or status.get("busy")
                    or requires_ground and (status.get("on_ground") is not True or status.get("submerged"))):
                raise WorkflowStepError("campaign_unsafe_stance", "Wait for a ready, grounded survival stance")
            expected = {"enter_nether": "overworld", "leave_nether": "nether", "stronghold": "overworld",
                        "leave_end": "end", "dragon": "end", "flint": "overworld", "equip_shield": "overworld",
                        "seek_food": "overworld", "recover_gear": "overworld", "seek_ground_wood": "overworld",
                        "emergency_food": "overworld", "defend_melee": "overworld",
                        "recover_fuel": "overworld", "exit_shaft": "overworld", "night_cover": "overworld",
                        "open_cover": "overworld", "house_porch": "overworld", "build_projection": "overworld",
                        "projection_relocate": "overworld"}
            if operation in expected and dimension(status) != expected[operation]:
                raise WorkflowStepError("wrong_dimension", "The dimension changed before the campaign action")
            evidence = handlers[operation](guard, status, origin, args, state)
            if isinstance(evidence, SkillResult):
                return evidence
            return SkillResult(True, evidence.pop("message", "Campaign step verified"),
                               {"executed": bool(guard.writes), **evidence})
        except WorkflowUncertainError as exc:
            return SkillResult(False, str(exc), {**exc.data, "executed": bool(guard.writes),
                               "cleanup_skipped": "workflow_unknown"}, exc.code)
        except WorkflowStepError as exc:
            evidence = dict(exc.data)
            if exc.blocker_pos is not None:
                evidence["blocker_pos"] = exc.blocker_pos
            return SkillResult(False, str(exc), {**evidence, "executed": bool(guard.writes)}, exc.code)

    @staticmethod
    def _result(guard, envelope):
        if not isinstance(envelope, dict) or envelope.get("ok") is not True or not isinstance(envelope.get("result"), dict):
            guard.fail_uncertain("action_unconfirmed", "The campaign request has no verifiable response",
                                 {"action_state": "unknown" if guard.writes else "not_executed"})
        return envelope["result"]

    def _block(self, guard, pos):
        return self._result(guard, guard.read(self.client.get_block, list(pos), timeout_seconds=guard.remaining_timeout(2)))

    def _find(self, guard, names, radius=64, limit=32):
        rows = guard.read(self.client.find_blocks, list(names), radius=radius, limit=limit,
                          timeout_seconds=guard.remaining_timeout(8))
        return [row for row in rows if row.get("name") in names and parse_pos(row.get("pos"), integer=True) is not None]

    def _entities(self, guard, names, radius=40):
        return guard.read(self.client.get_entities, radius=radius, types=list(names),
                          timeout_seconds=guard.remaining_timeout(4))

    def _counts(self, guard):
        payload = self._result(guard, guard.read(self.client.inventory, timeout_seconds=guard.remaining_timeout(4)))
        counts = {}
        for row in payload.get("items", []):
            name, count = row.get("name"), row.get("count")
            if isinstance(name, str) and type(count) is int and count > 0:
                counts[name] = counts.get(name, 0) + count
        return counts

    def _stand(self, guard, cell, *, portal=None, cache=None):
        x, y, z = cell

        def read(pos):
            key = tuple(pos)
            if cache is None:
                return self._block(guard, key)
            if key not in cache:
                cache[key] = self._block(guard, key)
            return cache[key]

        try:
            floor = read((x, y - 1, z))
            if floor.get("name") not in SUPPORT or not SurvivalSkills._safe_block(floor):
                return False
            passable = AIR if portal is None else AIR | {portal}
            return all((row := read((x, y + dy, z))).get("name") in passable and SurvivalSkills._safe_block(row)
                       for dy in (0, 1))
        except WorkflowStepError as exc:
            if exc.code == "not_loaded":
                return False
            raise

    def _move(self, guard, origin, target):
        distance = math.dist(target, origin)
        if distance < .5:
            raise WorkflowStepError("campaign_no_landing", "The waypoint would not move the bot")
        response = self._result(guard, guard.write(self.client.move_to, *target, range=0 if distance <= 2 else .5, timeout_ms=7000,
                                                  stop_on=["hurt", "death"]))
        if response.get("interrupted_by"):
            raise WorkflowStepError("damage_taken", "A hazard interrupted travel")
        deadline = time.monotonic() + 1.5
        while True:
            after = guard.check_status()
            where = parse_pos(after.get("pos"))
            if where is not None and after.get("on_ground") is True and after.get("busy") in (None, False):
                if math.dist(where, target) <= 1.4 and math.dist(where, origin) >= .45:
                    return {"position": where}
                raise WorkflowStepError("campaign_waypoint_missed", "Observed a grounded landing away from the waypoint",
                                        data={"actual_pos": where})
            if time.monotonic() >= deadline:
                guard.fail_uncertain("action_unconfirmed", "The campaign movement still has no grounded readback")
            if guard.cancel_event:
                guard.cancel_event.wait(.12)
            else:
                time.sleep(.12)

    def _dry_approach(self, guard, origin, foot, cache=None):
        start = list(map(math.floor, origin))
        count = max(abs(foot[0] - start[0]), abs(foot[2] - start[2]))
        for i in range(1, count):
            cell = [round(a + (b - a) * i / count) for a, b in zip(start, foot)]
            if not self._stand(guard, cell, cache=cache):
                return False
        return True

    def _walk(self, guard, origin, state, *, toward=None, allow_detour=True, prefer_far=False):
        """Prefer a freshly observed ledge; rotate on scoped route failures."""
        x, y, z = map(math.floor, origin)
        index = state.get("search_index", 0)
        if toward is not None:
            dx, dz = toward[0] - x, toward[2] - z
            offsets = sorted(DIRECTIONS, key=lambda pair: -(pair[0] * dx + pair[1] * dz) / math.hypot(*pair))
        else:
            offsets = [DIRECTIONS[(index + i) % len(DIRECTIONS)] for i in range(len(DIRECTIONS))]
        elevations = (0, -1, 1, -2, 2)
        if toward is not None and abs(toward[1] - origin[1]) > 3:
            elevations = (1, 2, 0, -1, -2) if toward[1] > origin[1] else (-1, -2, 0, 1, 2)
        rejected = state.get("rejected_steps", [])
        cache = {}
        if toward is not None and toward[1] - origin[1] > 3 and self._block(guard, [x, y + 2, z]).get("name") in AIR:
            failed = [row["pos"] for row in state.get("failed_steps", []) if row["until"] > time.time()]
            for dx, dz in (offset for offset in offsets if offset in DIRECTIONS[:4]):
                foot = [x + dx, y + 1, z + dz]
                if foot not in failed and self._stand(guard, foot, cache=cache):
                    try:
                        move = self._move(guard, origin, [foot[0] + .5, foot[1], foot[2] + .5])
                        if move["position"][1] <= origin[1] + .5:
                            return SkillResult(False, "The checked climb did not gain height", {**move, "reject_pos": foot,
                                               "executed": True}, "campaign_waypoint_missed")
                        return {**move, "survey_step": foot}
                    except WorkflowStepError as exc:
                        if exc.code != "campaign_waypoint_missed":
                            raise
                        return SkillResult(False, str(exc), {**exc.data, "reject_pos": foot, "executed": True}, exc.code)
        if prefer_far:
            candidates = ((dx, dz, gap) for gap in (12, 6, 3, 1)
                          for dx, dz in offsets if gap <= 3 or (dx, dz) in DIRECTIONS[:4])
        else:
            distances = (12, 6, 3, 1) if toward is not None else (3, 1, 6, 12)
            candidates = ((dx, dz, gap) for dx, dz in offsets[:4] for gap in distances)
        for dx, dz, distance in candidates:
            for dy in elevations:
                foot = [x + distance * dx, y + dy, z + distance * dz]
                if toward is not None and math.hypot(foot[0] + .5 - toward[0], foot[2] + .5 - toward[2]) >= math.hypot(
                        origin[0] - toward[0], origin[2] - toward[2]) - .25:
                    continue
                if (foot in rejected or not self._stand(guard, foot, cache=cache)
                        or not self._dry_approach(guard, origin, foot, cache)):
                    continue
                try:
                    moved = self._move(guard, origin, [foot[0] + .5, foot[1], foot[2] + .5])
                except WorkflowStepError as exc:
                    if exc.code in {"stalled", "path_not_found", "no_path", "campaign_waypoint_missed"}:
                        return SkillResult(False, "Campaign waypoint was unreachable",
                                           {**exc.data, "reject_pos": foot, "executed": True}, exc.code)
                    raise
                if toward is not None:
                    actual = moved["position"]
                    horizontal_gain = (math.hypot(origin[0] - toward[0], origin[2] - toward[2])
                                       - math.hypot(actual[0] - toward[0], actual[2] - toward[2]))
                    if horizontal_gain < .2 and math.dist(origin, toward) - math.dist(actual, toward) < .45:
                        return SkillResult(False, "The landing did not make progress toward the route target",
                                           {**moved, "reject_pos": foot, "executed": True}, "campaign_waypoint_missed")
                return {**moved, "survey_step": foot}
        if toward is not None and allow_detour:
            return self._walk(guard, origin, state)
        raise WorkflowStepError("campaign_no_landing", "No verified local waypoint from this stance")

    def _fill_water(self, guard, status, origin, args, state):
        if dimension(status) != "overworld":
            raise WorkflowStepError("wrong_dimension", "Water is sourced in the overworld")
        sources = self._find(guard, {"water"}, limit=20)
        for row in sources:
            pos = row["pos"]
            if math.dist(origin, pos) > 3.5:
                continue
            block = self._block(guard, pos)
            if block.get("name") != "water" or str((block.get("properties") or {}).get("level")) != "0":
                continue
            if not trace_block_visibility(origin, pos, lambda p: self._block(guard, p))["visible"]:
                continue
            before = self._counts(guard)
            self._result(guard, guard.write(self.client.interact, pos=pos, item="bucket", timeout=6))
            after = self._counts(guard)
            if after.get("water_bucket", 0) <= before.get("water_bucket", 0):
                guard.fail_uncertain("action_unconfirmed", "Water pickup was not confirmed by inventory")
            return {"item": "water_bucket"}
        return self._walk(guard, origin, state, toward=sources[0]["pos"] if sources else None)

    def _recover_stance(self, guard, status, origin, args, state):
        return self._walk(guard, origin, state, prefer_far=args.get("prefer_far") is True)

    def _projection_relocate(self, guard, status, origin, args, state):
        return self._walk(guard, origin, state, prefer_far=True)

    def _build_projection(self, guard, status, origin, args, state):
        if self.projection_builder is None:
            raise WorkflowStepError("unsupported_projection", "No projection was selected in the configuration")
        return self.projection_builder.step(guard, status, origin, args, state.get("projection_project") or {})

    def _exit_shaft(self, guard, status, origin, args, state):
        anchor = parse_pos(args.get("anchor"))
        if anchor is None or not 6 <= anchor[1] - origin[1] <= 80 or math.dist(anchor, origin) > 96:
            raise WorkflowStepError("campaign_no_landing", "The recorded mine entrance is outside this route")
        if not status.get("is_day"):
            x, y, z = map(math.floor, origin)
            roof = self._block(guard, [x, y + 2, z])
            if roof["name"] in {"stone", "andesite", "diorite", "granite", "deepslate", "cobblestone", "dirt"} and self._stand(guard, [x, y, z]):
                if guard.cancel_event:
                    guard.cancel_event.wait(3)
                else:
                    time.sleep(3)
                guard.check_status()
                return {"sheltered_until_daylight": True,
                        "message": "Remain under the checked mine roof while watching for daylight"}
        guard.writes += 1  # The delegated miner may move or dig before returning evidence.
        exit_data = MineShaftSkill(self.client, memory=self.memory)._ensure_returned_to_surface(
            list(map(math.floor, anchor)), timeout_seconds=60, cancel_event=guard.cancel_event)
        if not exit_data.get("on_surface"):
            return SkillResult(False, "The mine exit needs another checked approach", exit_data, "campaign_no_landing")
        return {**exit_data, "executed": True, "message": "Reached the recorded surface level after the interrupted shaft"}

    def _night_cover(self, guard, status, origin, args, state):
        x, y, z = anchor = list(map(math.floor, origin))
        if (args["anchor"] != anchor or any(abs(origin[i] - anchor[i] - .5) > .18 for i in (0, 2))
                or not self._stand(guard, anchor)):
            raise WorkflowStepError("campaign_no_landing", "Cover needs a centered dry standing cell")
        cover = state.get("night_cover") or {}
        if cover.get("anchor") != anchor or cover.get("life") != status.get("last_death"):
            exit_offset = next(((dx, dz) for dx, dz in DIRECTIONS[:4]
                                if self._stand(guard, [x + dx, y, z + dz])
                                and self._stand(guard, [x + 2 * dx, y, z + 2 * dz])), None)
            if exit_offset is None or args["operation"] == "open_cover":
                raise WorkflowStepError("campaign_no_landing", "No verified exterior landing for an enclosure")
            cover = {"anchor": anchor, "life": status.get("last_death"), "exit": list(exit_offset), "placed": []}
        dx, dz = cover["exit"]
        doorway = [[x + dx, y + dy, z + dz] for dy in (0, 1)]
        if args["operation"] == "open_cover":
            for pos in reversed(doorway):
                if pos in cover["placed"] and self._block(guard, pos).get("name") == "dirt":
                    self._result(guard, guard.write(self.client.dig, pos, collect=False, stop_on=["hurt", "death"]))
                    if self._block(guard, pos).get("name") not in AIR:
                        guard.fail_uncertain("dig_unconfirmed", "The doorway opening has no block readback")
                    return {"cover": cover, "opened_piece": pos}
            return {"cover_opened": True, "message": "The temporary mine-side shelter has an open exit"}
        walls = [[x + sx, y + dy, z + sz] for dy in (0, 1) for sx, sz in DIRECTIONS[:4]]
        roof = [x, y + 2, z]
        roof_block = self._block(guard, roof)
        if roof_block.get("name") in AIR:
            walls += [[x + dx, y + 2, z + dz], roof]
        elif roof_block.get("name") not in SUPPORT or not SurvivalSkills._safe_block(roof_block):
            raise WorkflowStepError("campaign_no_landing", "The existing roof is not verified dry solid cover")
        for pos in walls:
            block = self._block(guard, pos)
            if block.get("name") in SUPPORT and SurvivalSkills._safe_block(block):
                continue
            if pos[1] == y:
                pad = [pos[0], y - 1, pos[2]]
                pad_block = self._block(guard, pad)
                if pad_block.get("name") in {"air", "short_grass", "leaf_litter"}:
                    # Anchor the wall to the side of the current verified floor
                    # on a slope. The planned exit already has natural footing.
                    pos, block = pad, pad_block
            if block.get("name") not in {"air", "short_grass", "leaf_litter"} or not self._counts(guard).get("dirt"):
                raise WorkflowStepError("campaign_no_landing", "The small enclosure needs clear wall cells and carried dirt")
            self._result(guard, guard.write(self.client.place_block, *pos, item="dirt", stop_on=["hurt", "death"]))
            if self._block(guard, pos).get("name") != "dirt":
                guard.fail_uncertain("action_unconfirmed", "The newly placed shelter block has no readback")
            return {"cover": {**cover, "placed": [*cover["placed"], pos]}, "placed_cover": pos}
        if guard.cancel_event:
            guard.cancel_event.wait(3)
        else:
            time.sleep(3)
        guard.check_status()
        return {"cover": cover, "message": "Watch the natural night from the checked walls and roof"}

    def _house_porch(self, guard, status, origin, args, state):
        anchor, orientation = args["anchor"], args["orientation"]
        site = [anchor, orientation]
        surveyed = (state.get("porch") or {}).get("site") == site

        def world(relative):
            return transform_rel_pos(relative, anchor, orientation, integer=True)

        deck = [world((x, 0, z)) for z in range(3, 6) for x in range(-2, 3)]
        stone = [world(rel) for rel in PORCH_POSTS if rel[1] == 0]
        wood = [world(rel) for rel in (*PORCH_POSTS, *PORCH_ROOF) if rel[1] > 0]
        cache = {}
        if not surveyed and not all(self._stand(guard, position, cache=cache) for position in deck):
            raise WorkflowStepError("porch_site_occupied", "The porch needs flat dry ground in front of the house")
        missing = []
        for cell in stone + wood:
            block = self._block(guard, cell)
            allowed = {"cobblestone"} if cell in stone else PLANK_NAMES
            if block.get("name") in allowed and SurvivalSkills._safe_block(block):
                continue
            if block.get("name") not in AIR | {"short_grass", "leaf_litter"} or not SurvivalSkills._safe_block(block):
                raise WorkflowStepError("porch_site_occupied", "An existing block occupies the porch shape")
            missing.append(cell)
        progress = {"site": site, "surveyed": True, "wood": sum(p in wood for p in missing),
                    "stone": sum(p in stone for p in missing), "complete": not missing}
        if not missing:
            return {"porch": progress, "message": "Verified the 5 by 3 covered porch and both stone-footed posts"}
        if not surveyed:
            return {"porch": progress, "message": "Surveyed a dry 5 by 3 porch in front of the house"}
        counts = self._counts(guard)
        target = missing[0]
        item = ("cobblestone" if target in stone else next(
            (name for name in sorted(PLANK_NAMES) if counts.get(name, 0)), None))
        if item is None or not counts.get(item, 0):
            return {"porch": progress, "message": "Refresh the stock needed to finish the porch"}

        def visible_from(point):
            return (math.dist(point, target) <= 3.65 and not ShelterSkills._body_intersects(target, point)
                    and trace_block_visibility(point, target, lambda p: self._block(guard, p))["visible"] is True)

        if not visible_from(origin):
            for cell in sorted(deck, key=lambda p: math.dist(p, origin)):
                stand = [cell[0] + .5, cell[1], cell[2] + .5]
                if (math.dist(stand, origin) <= 6 and visible_from(stand) and self._stand(guard, cell, cache=cache)
                        and self._dry_approach(guard, origin, cell, cache)):
                    return {**self._move(guard, origin, stand), "porch": progress}
            raise WorkflowStepError("porch_no_stance", "No checked stance can reach the next porch block")

        adjacent = ([target[0] + dx, target[1] + dy, target[2] + dz]
                    for dx, dy, dz in ((0, -1, 0), (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1)))
        if not any((block := self._block(guard, p)).get("name") in SUPPORT and SurvivalSkills._safe_block(block)
                   for p in adjacent):
            raise WorkflowStepError("porch_site_occupied", "The next porch block has no solid attachment")
        for entity in self._entities(guard, {"hostile", "player", "animal", "mob"}, radius=10):
            point = parse_pos(entity.get("pos"))
            if point is not None and ShelterSkills._body_intersects(target, point):
                raise WorkflowStepError("safety_changed", "An entity entered the planned porch block")
        current = guard.check_status()
        if current.get("on_ground") is not True or math.dist(current["pos"], origin) > .2:
            raise WorkflowStepError("safety_changed", "The porch building stance changed")
        self._result(guard, guard.write(self.client.place_block, *target, item=item, stop_on=["hurt", "death"]))
        if self._block(guard, target).get("name") != item or counts[item] - self._counts(guard).get(item, 0) != 1:
            guard.fail_uncertain("action_unconfirmed", "The porch block or its inventory consumption was not verified")
        progress["stone" if target in stone else "wood"] -= 1
        return {"porch": progress, "porch_block": target, "message": "Placed and verified a porch block"}

    def _defend_melee(self, guard, status, origin, args, state):
        mob = args.get("mob")
        if (mob not in EMERGENCY_MELEE or any(status.get(key) for key in
                ("in_water", "submerged", "in_lava", "on_fire", "in_vehicle", "is_sleeping"))):
            raise WorkflowStepError("unsafe_state", "This close attack needs a current melee threat and dry footing")
        enemies = self._entities(guard, {"hostile"}, radius=4)
        target = next((row for row in enemies if row.get("name") == mob and row.get("id") == args["entity_id"]
                       and row.get("type") == "hostile"), None)
        point = parse_pos(target.get("pos")) if target is not None else None
        if (point is None or math.dist(origin, point) > 2.65 or not clear_entity_ray(
                origin, point, lambda p: self._block(guard, p), max_queries=12)["visible"]):
            raise WorkflowStepError("campaign_no_contact", "The melee attacker moved or its body is obstructed")
        final = guard.check_status()
        nearest = final.get("nearest_hostile") or {}
        gap = nearest.get("dist")
        if (nearest.get("id") != target["id"] or type(gap) not in (int, float) or not 0 <= gap <= 2.65
                or any(final.get(key) for key in ("in_water", "submerged", "in_lava", "on_fire"))):
            raise WorkflowStepError("safety_changed", "The current melee threat or body state changed before the swing")
        if math.dist(final["pos"], origin) > .35:
            nearby = self._entities(guard, {"hostile"}, radius=4)
            moved = next((parse_pos(row.get("pos")) for row in nearby if row.get("id") == target["id"]), None)
            if (moved is None or math.dist(final["pos"], moved) > 2.65 or not clear_entity_ray(
                    final["pos"], moved, lambda p: self._block(guard, p), max_queries=12)["visible"]):
                raise WorkflowStepError("campaign_no_contact", "The moving attacker is no longer in clear melee reach")
        reply = self._result(guard, guard.write(self.client.attack, target["id"], timeout_ms=450, max_hits=1))
        if reply.get("hits", 0) < 1:
            raise WorkflowStepError("campaign_no_contact", "The one-swing response sent no hit")
        return {"defended_against": mob, "entity_id": target["id"], "swings": reply["hits"],
                "message": "Sent one close-range defensive swing, then observe before moving"}

    def _escape_pressure(self, guard, status, origin, args, state):
        attacker_id = args.get("attacker_id")
        names = {"hostile", "player"} if type(attacker_id) is int else {"hostile"}
        threats = [row for row in self._entities(guard, names, radius=18)
                   if row.get("type") == "hostile" or row.get("id") == attacker_id]
        points = [(row.get("name"), pos) for row in threats if (pos := parse_pos(row.get("pos"))) is not None]
        if len(threats) >= 40 or len(points) != len(threats) or (not points and args.get("unseen_attacker") is not True):
            raise WorkflowStepError("campaign_no_threat", "An observed hostile is required for local evasion")
        observed_attacker = next((row for row in threats if row.get("id") == attacker_id), None) or min(
            threats, key=lambda row: math.dist(row["pos"], origin), default={})
        primary_name, primary = observed_attacker.get("name"), parse_pos(observed_attacker.get("pos"))
        threat_id = observed_attacker.get("id")
        history = state.get("escape_steps") or {}
        recent = [row["pos"] for row in history.get("positions", []) if row["until"] > time.time()
                  and history.get("threat") == threat_id]
        distance_now = math.dist(origin, primary) if primary else 0
        x, y, z = map(math.floor, origin)
        index = state.get("search_index", 0) % len(DIRECTIONS)
        directions = DIRECTIONS[index:] + DIRECTIONS[:index] if primary is None else DIRECTIONS
        lengths = (2, 1) if primary is None else (3, 2, 1)
        options = [[x + dx * length, y + dy, z + dz * length]
                   for length in lengths for dy in (0, 1, -1) for dx, dz in directions]
        if primary is not None:
            options.sort(key=lambda cell: -math.dist([cell[0] + .5, cell[1], cell[2] + .5], primary))
        terrain = {}

        def block(pos):
            key = tuple(pos)
            if key not in terrain:
                terrain[key] = self._block(guard, pos)
            return terrain[key]

        def covered(threat, point):
            blocker = clear_entity_ray(threat, point, block, max_queries=16).get("blocker")
            if blocker is None:
                return False
            wall = block(blocker)
            return wall.get("name") in SUPPORT and SurvivalSkills._safe_block(wall)

        for foot in options:
            destination = [foot[0] + .5, foot[1], foot[2] + .5]
            gap = math.dist(destination, primary) if primary else 0
            if foot in recent or not self._stand(guard, foot, cache=terrain):
                continue
            ranged_cover = melee_cover = False
            if primary is not None and gap < distance_now + .35:
                if primary_name in RANGED_THREATS and gap >= max(6, distance_now - .25):
                    ranged_cover = covered(primary, destination)
                elif primary_name == "creeper" and gap >= max(3.6, distance_now):
                    melee_cover = covered(primary, destination)
                elif (primary_name in EMERGENCY_MELEE and gap >= max(5, distance_now - 1.1)
                      and math.dist(origin, destination) <= 1.85):
                    midpoint = [(a + b) / 2 for a, b in zip(origin, destination)]
                    melee_cover = all(covered(primary, point) for point in (origin, midpoint, destination))
                if not (ranged_cover or melee_cover):
                    continue
            delta = [destination[i] - origin[i] for i in range(3)]
            squared = sum(v * v for v in delta)
            for name, threat in points:
                t = max(0., min(1., sum((threat[i] - origin[i]) * delta[i] for i in range(3)) / squared))
                minimum = math.dist(threat, [origin[i] + t * delta[i] for i in range(3)])
                limit = 16 if name in RANGED_THREATS else 3.5
                if ranged_cover and threat == primary:
                    limit = max(6, distance_now - .35)
                if minimum + .1 < min(limit, math.dist(threat, origin)):
                    break
            else:
                if self._dry_approach(guard, origin, foot, cache=terrain):
                    if math.dist(guard.check_status()["pos"], origin) > .3:
                        raise WorkflowStepError("safety_changed", "The checked escape stance moved")
                    result = self._move(guard, origin, destination)
                    return {**result, "escape_step": foot, "escape_threat_id": threat_id,
                            "message": "Moved through a checked local escape corridor"}
        raise WorkflowStepError("campaign_no_landing", "No checked lateral exit avoids the observed hostiles")

    def _surface_water(self, guard, status, origin, args, state):
        if not status.get("in_water") and not status.get("submerged"):
            return {"reached_air": True}
        if not status.get("submerged"):
            x, y, z = map(math.floor, origin)
            for dx, dz in DIRECTIONS[:4]:
                for distance in (1, 2, 3, 4):
                    for dy in (0, 1, 2):
                        foot = [x + dx * distance, y + dy, z + dz * distance]
                        if self._stand(guard, foot):
                            return self._move(guard, origin, [foot[0] + .5, foot[1], foot[2] + .5])
        self._result(guard, guard.write(self.client.control, jump=True, ms=750, wait=True))
        after = guard.check_status()
        where = parse_pos(after.get("pos"))
        if where is None:
            guard.fail_uncertain("action_unconfirmed", "No position was observed after the swimming pulse")
        if not after.get("submerged") or where[1] > origin[1] + .1:
            return {"position": where, "reached_air": not after.get("submerged")}
        raise WorkflowStepError("campaign_swim_blocked", "Upward swimming was observed under a ceiling")
    def _seek_food(self, guard, status, origin, args, state):
        animals = self._entities(guard, {"pig", "cow", "sheep", "chicken"}, radius=96)
        positions = [pos for row in animals if (pos := parse_pos(row.get("pos"))) is not None]
        positions.sort(key=lambda pos: math.dist(pos, origin))
        target = next((pos for pos in positions[:16] if self._stand(guard, list(map(math.floor, pos)))), None)
        if target is not None and math.dist(target, origin) <= 2.8:
            return {"observed_supply": target, "food_in_reach": True}
        moved = self._walk(guard, origin, state, toward=target)
        if isinstance(moved, dict) and target is not None:
            moved["observed_supply"] = target
        return moved

    def _seek_ground_wood(self, guard, status, origin, args, state):
        counts = self._counts(guard)
        requested = args.get("log")
        log_names = (requested,) if requested in LOG_NAMES else LOG_NAMES
        if sum(counts.get(name, 0) for name in log_names) >= args.get("min_logs", 1):
            return {"wood_collected": True, "message": "Current inventory contains the recovered logs"}
        drop = self._pickup(guard, origin, log_names)
        if drop:
            return {**drop, "wood_collected": bool(drop.get("picked_up"))}
        if state.get("wood_search_steps", 0) >= 18:
            sapling = next((name for name in ("oak_sapling", "birch_sapling", "spruce_sapling")
                            if counts.get(name) and name.removesuffix("_sapling") + "_log" in log_names), None)
            if sapling:
                planted = self._plant_emergency_tree(guard, origin, sapling)
                if planted:
                    return planted
        skipped = [row["pos"] for row in state.get("blocked_wood", []) if row["until"] > time.time()]
        drops = [pos for row in self._entities(guard, {"item"}, radius=48)
                 if (row.get("item") or "").rpartition("x")[0] in log_names
                 and (pos := parse_pos(row.get("pos"))) is not None and list(map(math.floor, pos)) not in skipped]
        if drops:
            target = min(drops, key=lambda p: math.dist(origin, p))
            if math.dist(origin, target) <= 5:
                x, y, z = map(math.floor, target)
                for piece in ([x, y + 1, z], [x, y, z]):
                    if self._block(guard, piece).get("name") not in SurvivalSkills.RETREAT_LEAVES:
                        continue
                    sight = trace_block_visibility(origin, piece, lambda p: self._block(guard, p))
                    leaf = piece if sight["visible"] is True else sight.get("blocker")
                    cleared = self._open_wood_sight(guard, origin, leaf)
                    if cleared is not None:
                        return cleared
            return self._approach_wood(guard, origin, state, target)
        sources, upper_forest, scan_radius = self._scan_timber(guard, origin, skipped, log_names)
        for row in sources[:8]:
            pos = row["pos"]
            centre = [v + .5 for v in pos]
            distance = math.dist(centre, origin)
            if not 0.8 <= distance <= 5.5 or (pos[0], pos[2]) == (math.floor(origin[0]), math.floor(origin[2])):
                continue
            sight = trace_block_visibility(origin, pos, lambda p: self._block(guard, p))
            if sight["state"] == "occluded":
                cleared = self._open_wood_sight(guard, origin, sight.get("blocker"))
                if cleared is not None:
                    return cleared
            if sight["visible"] is not True or distance > 3.65:
                continue
            check = self._block(guard, pos)
            if check.get("name") != row["name"] or check.get("diggable") is not True:
                continue
            gathered = SurvivalSkills(self.client).execute("gather_block", {"pos": pos, "expected_name": row["name"]},
                                                          cancel_event=guard.cancel_event)
            if gathered.success:
                gathered.data["wood_collected"] = True
            return gathered
        for row in sources[:2]:
            pos = row["pos"]
            if math.dist(pos, origin) > 10:
                continue
            x, y, z = pos
            steps = [[x + dx * distance, y + dy, z + dz * distance]
                     for dx, dz in DIRECTIONS[:4] for distance in (1, 2) for dy in (0, 1, -1)]
            for foot in sorted(steps, key=lambda p: math.dist(p, origin))[:12]:
                target = [foot[0] + .5, foot[1], foot[2] + .5]
                if (not 1 <= math.dist(target, origin) <= 7 or foot in state.get("rejected_steps", [])
                        or not self._stand(guard, foot)
                        or not trace_block_visibility(target, pos, lambda p: self._block(guard, p))["visible"]
                        or not self._dry_approach(guard, origin, foot)):
                    continue
                try:
                    return {**self._move(guard, origin, target), "survey_step": foot}
                except WorkflowStepError as exc:
                    if exc.code != "campaign_waypoint_missed":
                        raise
                    return SkillResult(False, str(exc), {**exc.data, "reject_pos": foot, "executed": True}, exc.code)
        near_rejections = sum(math.hypot(p[0] - origin[0], p[2] - origin[2]) <= 16 for p in skipped)
        if sources and near_rejections < 4 and math.dist(sources[0]["pos"], origin) <= 6:
            return {"blocked_wood": sources[0]["pos"], "message": "No checked approach to this timber; survey another tree"}
        target = sources[0]["pos"] if sources else upper_forest
        if target is not None and near_rejections < 4:
            result = self._approach_wood(guard, origin, state, target)
            (result.data if isinstance(result, SkillResult) else result)["wood_scan_radius"] = scan_radius
            return result
        if state.get("wood_search_steps", 0) >= 18:
            waypoint = parse_pos(state.get("wood_waypoint"))
            if waypoint is None or math.dist(waypoint, origin) <= 7:
                dx, dz = DIRECTIONS[state.get("search_index", 0) % len(DIRECTIONS)]
                waypoint = [origin[0] + dx * 64, origin[1], origin[2] + dz * 64]
            try:
                result = self._walk(guard, origin, state, toward=waypoint, allow_detour=False)
            except WorkflowStepError as exc:
                if exc.code != "campaign_no_landing":
                    raise
                return SkillResult(False, "The forest survey heading is blocked",
                                   {"wood_waypoint_blocked": True}, exc.code)
            if isinstance(result, dict):
                result["wood_waypoint"] = waypoint
            return result
        return self._walk(guard, origin, state, prefer_far=near_rejections >= 4)

    def _scan_timber(self, guard, origin, skipped, log_names=LOG_NAMES):
        """Widen the loaded-block search only when the closer sweep has no usable trunk."""
        rejected_columns = {(pos[0], pos[2]) for pos in skipped}
        seen = set()
        upper_forest = []
        for radius in (12, 24, 48, 64):
            timber = []
            for row in self._find(guard, log_names, radius=radius, limit=64):
                x, y, z = key = tuple(row["pos"])
                if key in seen or (x, z) in rejected_columns:
                    continue
                seen.add(key)
                height = y - origin[1]
                horizontal_gap = math.hypot(x + .5 - origin[0], z + .5 - origin[2])
                if -2 <= height <= 12 and (height <= 4 or horizontal_gap >= 4):
                    upper_forest.append(row["pos"])
                if abs(height) > 4:
                    continue
                floor = self._block(guard, [x, y - 1, z])
                rooted = floor.get("name") in {"grass_block", "dirt", "podzol", "coarse_dirt", "rooted_dirt", "mud"}
                if rooted or -1.25 <= height <= 2.25:
                    timber.append((math.dist(origin, key) + (0 if rooted else 3), row))
            if timber:
                return [row for _, row in sorted(timber, key=lambda entry: entry[0])], None, radius
        return [], min(upper_forest, key=lambda pos: math.dist(pos, origin), default=None), radius

    def _approach_wood(self, guard, origin, state, target):
        try:
            return self._walk(guard, origin, state, toward=target, allow_detour=False)
        except WorkflowStepError as exc:
            if exc.code != "campaign_no_landing":
                raise
            return {"blocked_wood": list(map(math.floor, target)), "message": "No checked approach to the wood source"}

    def _plant_emergency_tree(self, guard, origin, sapling):
        x, y, z = map(math.floor, origin)
        for dx, dz in DIRECTIONS[:4]:
            pos = [x + dx * 2, y, z + dz * 2]
            if self._block(guard, [pos[0], y - 1, pos[2]]).get("name") not in {"grass_block", "dirt", "podzol"}:
                continue
            if any(self._block(guard, [pos[0], y + dy, pos[2]]).get("name") not in AIR for dy in range(7)):
                continue
            self._result(guard, guard.write(self.client.place_block, *pos, item=sapling, stop_on=["hurt", "death"]))
            if self._block(guard, pos).get("name") != sapling:
                guard.fail_uncertain("action_unconfirmed", "The sapling placement has no block readback")
            return {"planted_tree": pos, "message": "Planted a carried sapling on checked open soil"}
        return None

    def _recover_fuel(self, guard, status, origin, args, state):
        counts = self._counts(guard)
        if counts.get("coal") or counts.get("charcoal"):
            return {"fuel_collected": True, "message": "The furnace fuel is already in inventory"}
        own_column = (math.floor(origin[0]), math.floor(origin[2]))
        ore = self._find(guard, {"coal_ore", "deepslate_coal_ore"}, radius=5, limit=16)
        for row in ore:
            pos = row["pos"]
            if ((pos[0], pos[2]) == own_column or math.dist([v + .5 for v in pos], origin) > 3.6
                    or not trace_block_visibility(origin, pos, lambda p: self._block(guard, p))["visible"]):
                continue
            result = SurvivalSkills(self.client).execute("gather_block", {"pos": pos, "expected_name": row["name"]},
                                                        cancel_event=guard.cancel_event)
            if result.success:
                result.data["fuel_collected"] = True
            if result.success or result.data.get("executed") or result.error_code not in {"unsafe", "target_occluded"}:
                return result
        return self._seek_ground_wood(guard, status, origin, args, state)

    def _open_wood_sight(self, guard, origin, pos):
        if (pos is None or not 1.1 <= math.dist([v + .5 for v in pos], origin) <= 3.5
                or (pos[0], pos[2]) == (math.floor(origin[0]), math.floor(origin[2]))
                or not self._stand(guard, list(map(math.floor, origin)))):
            return None
        block = self._block(guard, pos)
        if (block.get("name") not in SurvivalSkills.RETREAT_LEAVES or block.get("diggable") is not True
                or not SurvivalSkills._safe_block(block)
                or not trace_block_visibility(origin, pos, lambda p: self._block(guard, p))["visible"]):
            return None
        self._result(guard, guard.write(self.client.dig, pos, collect=False, stop_on=["hurt", "death"], timeout_ms=2000))
        if self._block(guard, pos).get("name") not in AIR:
            guard.fail_uncertain("dig_unconfirmed", "The blocking leaf has no removal readback")
        return {"cleared_wood_sight": pos, "message": "Removed the verified leaf blocking rooted timber"}

    def _recover_gear(self, guard, status, origin, args, state):
        if (status.get("last_death") or {}).get("t") != args["life"]:
            raise WorkflowStepError("life_changed", "The recovery site belongs to the previous life")
        counts = self._counts(guard)
        sites = []
        for row in self._entities(guard, {"item"}, radius=80):
            item = (row.get("item") or "").rsplit("x", 1)[0]
            pos = parse_pos(row.get("pos"))
            if (item in RECOVERY_GEAR and not counts.get(item, 0) and pos is not None
                    and math.dist(pos, args["death"]) <= 40):
                priority = -1 if status["health"] < 16 and item in SAFE_FOOD else RECOVERY_GEAR.index(item)
                sites.append((priority, math.dist(pos, origin), item, pos))
        if not sites:
            death = args["death"]
            if math.dist(death, origin) > 16:
                if not status.get("is_day"):
                    return {"gear_deferred": True, "message": "Defer checking the distant death site until daylight"}
                return self._walk(guard, origin, state, toward=death)
            return {"gear_search_done": True, "message": "There are no matching loaded equipment drops"}
        _, distance, item, pos = min(sites)
        if distance <= 7:
            recovered = self._pickup(guard, origin, item)
            if recovered:
                return recovered
            if distance <= 2.8 and not self._entities(guard, {"hostile"}, radius=12):
                foot = list(map(math.floor, pos))
                x, y, z = foot
                if (self._block(guard, foot).get("name") == "water"
                        and self._block(guard, [x, y + 1, z]).get("name") in AIR
                        and self._block(guard, [x, y - 1, z]).get("name") in SUPPORT):
                    yaw = math.atan2(origin[0] - pos[0], origin[2] - pos[2])
                    self._result(guard, guard.write(self.client.control, forward=True, jump=True, yaw=yaw,
                                                    ms=min(750, max(250, round(distance * 260))), wait=True))
                    after = guard.check_status()
                    gained = max(0, self._counts(guard).get(item, 0) - counts.get(item, 0))
                    return {"position": after["pos"], "item": item, "picked_up": gained}
        return self._walk(guard, origin, state, toward=pos)
    def _flint(self, guard, status, origin, args, state):
        recovered = self._pickup(guard, origin, "flint")
        if recovered:
            return recovered
        gravel = self._find(guard, {"gravel"})
        for row in gravel:
            pos = row["pos"]
            if (not 1 <= math.dist(origin, pos) <= 3.8 or pos[1] != math.floor(origin[1])
                    or [pos[0], pos[2]] == [math.floor(origin[0]), math.floor(origin[2])]):
                continue
            if not self._stand(guard, [math.floor(v) for v in origin]):
                break
            if self._block(guard, pos).get("name") != "gravel" or not trace_block_visibility(
                    origin, pos, lambda p: self._block(guard, p))["visible"]:
                continue
            self._result(guard, guard.write(self.client.dig, pos, collect=False))
            if self._block(guard, pos).get("name") == "gravel":
                guard.fail_uncertain("action_unconfirmed", "Gravel removal could not be confirmed")
            return {"gravel_removed": pos}
        return self._walk(guard, origin, state, toward=gravel[0]["pos"] if gravel else None)

    def _eat(self, guard, status, origin, args, state):
        counts = self._counts(guard)
        allowed = EMERGENCY_FOOD if args["operation"] == "emergency_food" else SAFE_FOOD
        food = next((item for item in sorted(allowed) if counts.get(item)), None)
        if food is None:
            raise WorkflowStepError("missing_food", "No verified carried food is available")
        self._result(guard, guard.write(self.client.use_item, food))
        if guard.check_status()["food"] <= status["food"]:
            guard.fail_uncertain("action_unconfirmed", "Food consumption did not raise the observed food level")
        return {"ate": food}

    def _emergency_food(self, guard, status, origin, args, state):
        hostile = status.get("nearest_hostile") or {}
        if hostile.get("dist", 100) <= 10:
            raise WorkflowStepError("unsafe_state", "A hostile is too close for low-health forage")
        current = self._counts(guard)
        if any(current.get(item) for item in EMERGENCY_FOOD):
            return self._eat(guard, status, origin, args, state)
        picked = self._pickup(guard, origin, EMERGENCY_FOOD)
        if picked:
            return picked
        bushes = self._find(guard, {"sweet_berry_bush"}, radius=48)
        for row in bushes[:8]:
            pos = row["pos"]
            if not 1.5 <= math.dist(pos, origin) <= 3.2:
                continue
            block = self._block(guard, pos)
            age = str((block.get("properties") or {}).get("age"))
            if block.get("name") != "sweet_berry_bush" or age not in ("2", "3"):
                continue
            if not trace_block_visibility(origin, pos, lambda p: self._block(guard, p))["visible"]:
                continue
            self._result(guard, guard.write(self.client.interact, pos=pos, timeout=5))
            if str((self._block(guard, pos).get("properties") or {}).get("age")) == age:
                guard.fail_uncertain("action_unconfirmed", "The berry bush change was not independently observed")
            return {"harvested_berries": pos}
        if bushes:
            return self._walk(guard, origin, state, toward=bushes[0]["pos"])
        if status["health"] >= 7:
            animals = self._entities(guard, {"cow", "pig", "sheep"}, radius=80)
            candidates = [(math.dist(origin, pos), row, pos) for row in animals
                          if (pos := parse_pos(row.get("pos"))) is not None and type(row.get("id")) is int]
            candidates.sort(key=lambda entry: entry[0])
            for distance, row, pos in candidates[:12]:
                foot = list(map(math.floor, pos))
                if not self._stand(guard, foot):
                    continue
                if distance <= 1.9 and self._dry_approach(guard, origin, foot) and clear_entity_ray(
                        origin, pos, lambda p: self._block(guard, p), max_queries=12)["visible"]:
                    if self._entities(guard, {"hostile"}, radius=12):
                        raise WorkflowStepError("unsafe_state", "A hostile interrupted the emergency food approach")
                    reply = self._result(guard, guard.write(self.client.attack, row["id"], timeout_ms=1400, max_hits=4))
                    guard.check_status()
                    if reply.get("hits", 0) > 0:
                        return {"target": row["name"], "entity_id": row["id"], "emergency_swings": reply["hits"]}
                return self._walk(guard, origin, state, toward=pos)
        return self._walk(guard, origin, state)

    def _equip_shield(self, guard, status, origin, args, state):
        self._result(guard, guard.write(self.client.equip, "shield", slot="off-hand"))
        if (guard.check_status().get("offhand") or {}).get("name") != "shield":
            guard.fail_uncertain("action_unconfirmed", "Shield equip was not verified in the offhand")
        return {"equipped": "shield"}

    def _cross(self, guard, status, origin, pos, portal, destination, state):
        if self._block(guard, pos).get("name") != portal:
            raise WorkflowStepError("campaign_portal_changed", "The previously observed portal is absent")
        foot = [pos[0], pos[1] + (portal == "end_portal"), pos[2]]
        if portal == "nether_portal" and not self._stand(guard, foot, portal=portal):
            raise WorkflowStepError("campaign_portal_stance", "The gateway entrance has no verified footing and headroom")
        if portal == "end_portal" and self._block(guard, foot).get("name") not in AIR:
            raise WorkflowStepError("campaign_portal_stance", "The gateway's entry space is obstructed")
        if math.dist([foot[0] + .5, foot[1], foot[2] + .5], origin) > 7:
            return self._walk(guard, origin, state, toward=foot)
        # A dimension transition is expected: the generic same-life guard is
        # used until the write, then the new dimension gets its own readback.
        try:
            self._result(guard, guard.write(self.client.move_to, foot[0] + .5, foot[1], foot[2] + .5,
                                            range=0, timeout_ms=7000, stop_on=["hurt", "death"]))
        except WorkflowStepError as exc:
            if exc.code not in {"path_not_found", "stalled", "no_path", "interrupted"}:
                raise
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            after = self._result(guard, guard.read(self.client.get_status, timeout_seconds=guard.remaining_timeout(2)))
            if after.get("alive") is not True or after.get("last_death") != status.get("last_death"):
                raise WorkflowStepError("life_changed", "A death cannot count as a portal transition")
            if after.get("ready") and dimension(after) == destination and parse_pos(after.get("pos")) is not None:
                return {"transition": destination, "portal": pos, "arrival": after["pos"]}
            if dimension(after) not in {dimension(status), destination}:
                guard.fail_uncertain("action_unconfirmed", "Portal led to an unexpected dimension")
            if guard.cancel_event and guard.cancel_event.wait(.25):
                break
            if not guard.cancel_event:
                time.sleep(.25)
        after_pos = parse_pos(after.get("pos"))
        if (dimension(after) == dimension(status) and after_pos is not None and math.dist(after_pos, foot) > 2
                and self._block(guard, list(map(math.floor, after_pos))).get("name") != portal):
            raise WorkflowStepError("campaign_portal_unreached", "The bot is still outside the portal")
        guard.fail_uncertain("action_unconfirmed", "The expected dimension transition could not be verified")

    def _portal(self, guard, status, origin, state, *, portal, destination):
        rows = self._find(guard, {portal})
        if rows:
            for row in sorted(rows, key=lambda row: math.dist(row["pos"], origin))[:10]:
                try:
                    return self._cross(guard, status, origin, row["pos"], portal, destination, state)
                except WorkflowStepError as exc:
                    if exc.code != "campaign_portal_stance":
                        raise
            raise WorkflowStepError("campaign_portal_stance", "No observed portal cell has an accessible entrance")
        return None

    def _enter_nether(self, guard, status, origin, args, state):
        found = self._portal(guard, status, origin, state, portal="nether_portal", destination="nether")
        if found:
            return found
        lava = self._find(guard, {"lava"}, limit=12)
        if lava and any(math.dist(row["pos"], origin) < 16 for row in lava):
            return NetherPortalSpeedrunSkill(self.client).build_portal(cancel_event=guard.cancel_event, timeout_seconds=160)
        return self._walk(guard, origin, state, toward=lava[0]["pos"] if lava else state.get("overworld_portal"))

    def _leave_nether(self, guard, status, origin, args, state):
        found = self._portal(guard, status, origin, state, portal="nether_portal", destination="overworld")
        return found or self._walk(guard, origin, state, toward=state.get("nether_arrival"))

    def _pickup(self, guard, origin, items):
        allowed = {items} if isinstance(items, str) else set(items)
        for row in self._entities(guard, {"item"}, radius=7):
            pos = parse_pos(row.get("pos"))
            item, sep, count = (row.get("item") or "").rpartition("x")
            if pos is None or item not in allowed or not sep or not re.fullmatch(r"[1-9][0-9]*", count):
                continue
            foot = list(map(math.floor, pos))
            if not self._stand(guard, foot) or not self._dry_approach(guard, origin, foot):
                continue
            before = self._counts(guard).get(item, 0)
            target = [foot[0] + .5, foot[1], foot[2] + .5]
            if math.dist(target, origin) < .5:
                if guard.cancel_event:
                    guard.cancel_event.wait(.4)
                else:
                    time.sleep(.4)
                result = {}
            else:
                result = self._move(guard, origin, target)
            gained = self._counts(guard).get(item, 0) - before
            return {**result, "item": item, "picked_up": max(0, gained)}
        return None

    def _strike(self, guard, origin, row, *, ranged):
        pos = parse_pos(row.get("pos"))
        if type(row.get("id")) is not int or pos is None:
            raise WorkflowStepError("campaign_no_target", "No current entity id and position")
        if ranged:
            counts = self._counts(guard)
            weapon = next((item for item in ("bow", "crossbow") if counts.get(item)), None)
            if not weapon or not counts.get("arrow"):
                raise WorkflowStepError("missing_ammo", "A bow or crossbow and regular arrows are required")
            reply = guard.write(self.client.shoot, row["id"], weapon=weapon)
        else:
            if math.dist(origin, pos) > 2.8:
                raise WorkflowStepError("campaign_no_target", "Target is outside verified melee reach")
            reply = guard.write(self.client.attack, row["id"], timeout_ms=3000, max_hits=1)
        result = self._result(guard, reply)
        guard.check_status()
        return {"target": row["name"], "entity_id": row["id"], "attack": result}

    def _barter(self, guard, origin):
        counts = self._counts(guard)
        if not counts.get("gold_ingot"):
            return None
        inventory = self._result(guard, guard.read(self.client.inventory, timeout_seconds=guard.remaining_timeout(4)))
        armor = inventory.get("armor") or {}
        if not any((piece or {}).get("name", "").startswith("golden_") for piece in armor.values()):
            return None
        for row in self._entities(guard, {"piglin"}, radius=4):
            pos = parse_pos(row.get("pos"))
            if pos is None or math.dist(pos, origin) > 2.6 or row.get("name") != "piglin" or type(row.get("id")) is not int:
                continue
            self._result(guard, guard.write(self.client.interact, entity_id=row["id"], item="gold_ingot", timeout=5))
            if self._counts(guard).get("gold_ingot", 0) != counts["gold_ingot"] - 1:
                guard.fail_uncertain("action_unconfirmed", "The gold handoff was not independently verified")
            return {"bartered": True, "piglin": row["id"]}
        return None

    def _loot_mob(self, guard, status, origin, args, state):
        mob, item = args["mob"], args["item"]
        if HOSTS.get(mob) != item:
            raise WorkflowStepError("bad_args", "The mob and expected item do not match")
        if (mob == "blaze" and dimension(status) != "nether"
                or mob in {"chicken", "spider"} and dimension(status) != "overworld"):
            raise WorkflowStepError("wrong_dimension", "The selected resource mob belongs to another stage")
        collected = self._pickup(guard, origin, item)
        if collected:
            return collected
        if mob == "enderman" and dimension(status) == "nether":
            trade = self._barter(guard, origin)
            if trade is not None:
                return trade
        targets = self._entities(guard, {mob})
        for row in targets:
            pos = parse_pos(row.get("pos"))
            if pos is None or row["name"] != mob:
                continue
            distance = math.dist(pos, origin)
            if distance <= 2.8:
                return self._strike(guard, origin, row, ranged=False)
            if mob == "blaze" and distance <= 24:
                counts = self._counts(guard)
                if counts.get("arrow") and (counts.get("bow") or counts.get("crossbow")):
                    return self._strike(guard, origin, row, ranged=True)
            return self._walk(guard, origin, state, toward=pos)
        marker = ({"nether_bricks", "nether_brick_fence", "spawner"} if mob == "blaze" else
                  {"warped_nylium"} if mob == "enderman" and dimension(status) == "nether" else set())
        clues = self._find(guard, marker) if marker else []
        if (mob == "blaze" and state.get("spawner_waits", 0) < 3
                and any(row["name"] == "spawner" and math.dist(row["pos"], origin) <= 12 for row in clues)
                and any(row["name"] in {"nether_bricks", "nether_brick_fence"} for row in clues)):
            if guard.cancel_event:
                guard.cancel_event.wait(2)
            else:
                time.sleep(2)
            return {"awaiting_blaze": True, "message": "Reobserve the nearby fortress spawner after a bounded wait"}
        return self._walk(guard, origin, state, toward=clues[0]["pos"] if clues else None)

    @staticmethod
    def _ring(centre):
        x, y, z = centre
        for offset in (-1, 0, 1):
            yield (x + offset, y, z - 2), "south"
            yield (x + offset, y, z + 2), "north"
            yield (x - 2, y, z + offset), "east"
            yield (x + 2, y, z + offset), "west"

    def _portal_ring(self, guard, frames, origin):
        observed = {tuple(row["pos"]) for row in frames}
        # Derive candidates from geometrically possible centres, then re-read
        # the whole ring before selecting an eye slot.
        centres = {(x + dx, y, z + dz) for x, y, z in observed
                   for dx, dz in ((a, b) for a in range(-2, 3) for b in range(-2, 3)
                                  if sorted((abs(a), abs(b))) in ([0, 2], [1, 2]))}
        for centre in sorted(centres, key=lambda row: math.dist(row, origin)):
            ring = list(self._ring(centre))
            if sum(pos in observed for pos, _ in ring) < 9:
                continue
            missing = []
            for pos, inward in ring:
                row = self._block(guard, pos)
                props = row.get("properties") or {}
                eye = str(props.get("eye")).lower()
                if row.get("name") != "end_portal_frame" or props.get("facing") != inward or eye not in ("true", "false"):
                    break
                if eye == "false":
                    missing.append(list(pos))
            else:
                return list(centre), missing
        return None, []

    def _navigate(self, guard, target, *, dig=False, bridge=False):
        # Reuse the shared voxel navigator and the outer campaign cancel event.
        nav = CaveExploreSkill(self.client, memory=self.memory).navigate_with_ops(
            target, allow_pillar=False, allow_dig=dig, allow_bridge=bridge, allow_drop=False,
            allow_damage=False, max_steps=16, timeout_seconds=18, cancel_event=guard.cancel_event)
        return nav

    def _stronghold(self, guard, status, origin, args, state):
        active = self._portal(guard, status, origin, state, portal="end_portal", destination="end")
        if active:
            return active
        frames = self._find(guard, {"end_portal_frame"}, limit=48)
        centre, missing = self._portal_ring(guard, frames, origin)
        if centre is not None:
            if self._counts(guard).get("ender_eye", 0) < len(missing):
                return {"end_eyes_required": len(missing), "message": "The verified ring still needs more Eyes of Ender"}
            for pos in sorted(missing, key=lambda target: math.dist(target, origin)):
                if math.dist(pos, origin) > 3.25:
                    break
                if not trace_block_visibility(origin, pos, lambda p: self._block(guard, p))["visible"]:
                    continue
                before = self._counts(guard).get("ender_eye", 0)
                self._result(guard, guard.write(self.client.interact, pos=pos, item="ender_eye", timeout=6))
                verified = self._block(guard, pos)
                after = self._counts(guard)
                if (verified.get("name") != "end_portal_frame"
                        or str((verified.get("properties") or {}).get("eye")).lower() != "true"
                        or after.get("ender_eye", 0) != before - 1):
                    guard.fail_uncertain("action_unconfirmed", "Eye insertion did not match the observed portal slot")
                return {"end_eyes_required": len(missing) - 1, "filled_frame": pos}
            if missing:
                for pos in missing:
                    for dx, dz in DIRECTIONS:
                        foot = [pos[0] + dx * 2, pos[1], pos[2] + dz * 2]
                        if math.dist(foot, origin) > 1.6 and self._stand(guard, foot):
                            return self._navigate(guard, foot, dig=True)
            raise WorkflowStepError("campaign_portal_stance", "The confirmed portal ring has no verified approach")
        if frames:
            target = frames[0]["pos"]
            for dx, dz in DIRECTIONS:
                foot = [target[0] + dx * 2, target[1], target[2] + dz * 2]
                if math.dist(foot, origin) > 2 and self._stand(guard, foot):
                    return self._navigate(guard, foot, dig=True)
        waypoint = parse_pos(state.get("eye_waypoint"))
        if waypoint is not None and math.hypot(origin[0] - waypoint[0], origin[2] - waypoint[2]) > 10:
            return self._walk(guard, origin, state, toward=waypoint)
        if self._counts(guard).get("ender_eye", 0) <= 12:
            return {"end_eyes_required": 16, "clear_eye_waypoint": True,
                    "message": "Keep up to twelve eyes for the frame and replenish search eyes"}
        guard.check_status()
        guard.writes += 1  # The sampler owns one blocking use_item with concurrent readbacks.
        try:
            capture = EyeObservationCollector(self.client).capture()
        except (ObservationError, APIError) as exc:
            guard.fail_uncertain("action_unconfirmed", str(exc))
        measured = capture["throw"]
        return {"eye_waypoint": eye_search_target(origin, measured, state.get("eye_samples", [])),
                "eye_sample": measured}

    def _dragon_state(self, guard):
        report = self._result(guard, guard.read(self.client.advancements, status="all",
                                               timeout_seconds=guard.remaining_timeout(4)))
        if report.get("ready") is not True or not isinstance(report.get("advancements"), list):
            raise WorkflowStepError("campaign_advancements", "Server advancement evidence is unavailable")
        row = next((row for row in report["advancements"] if row.get("id") == DRAGON_ADVANCEMENT), None)
        return row.get("status") == "completed" if row is not None else None

    def _reach_end_land(self, guard, origin):
        own = list(map(math.floor, origin))
        floor = [own[0], own[1] - 1, own[2]]
        if self._block(guard, floor).get("name") == "end_stone":
            return {"end_land": True}
        land = self._find(guard, {"end_stone"}, radius=64, limit=48)
        for row in land:
            x, y, z = row["pos"]
            target = [x, y + 1, z]
            if not self._stand(guard, target):
                continue
            if math.dist(origin, target) <= 6:
                return self._navigate(guard, target, bridge=True)
            if abs(y - floor[1]) > 6 or not self._stand(guard, own):
                continue
            dx, dz = x - floor[0], z - floor[2]
            directions = sorted(DIRECTIONS[:4], key=lambda step: -(dx * step[0] + dz * step[1]))
            for sx, sz in directions[:2]:
                if sx * dx + sz * dz <= 0:
                    continue
                adjacent = [own[0] + sx, own[1], own[2] + sz]
                next_floor = [adjacent[0], adjacent[1] - 1, adjacent[2]]
                if self._stand(guard, adjacent):
                    return self._move(guard, origin, [adjacent[0] + .5, adjacent[1], adjacent[2] + .5])
                head = [adjacent[0], adjacent[1] + 1, adjacent[2]]
                if any(self._block(guard, pos).get("name") not in AIR for pos in (next_floor, adjacent, head)):
                    continue
                before = self._counts(guard).get("cobblestone", 0)
                if before == 0:
                    raise WorkflowStepError("campaign_bridge_stock", "No dry building blocks remain for the End crossing")
                self._result(guard, guard.write(self.client.place_block, *next_floor, item="cobblestone",
                                                stop_on=["hurt", "death"]))
                if (self._block(guard, next_floor).get("name") != "cobblestone"
                        or self._counts(guard).get("cobblestone", 0) != before - 1):
                    guard.fail_uncertain("action_unconfirmed", "The next End bridge block is unconfirmed")
                return {"placed_bridge_block": next_floor}
        raise WorkflowStepError("campaign_end_island", "No loaded dry route points toward the main End island")

    def _cage(self, guard, origin, crystal):
        """Remove one reachable bar immediately surrounding an observed crystal."""
        centre = parse_pos(crystal.get("pos"))
        if centre is None:
            return None
        x, y, z = map(math.floor, centre)
        for row in self._find(guard, {"iron_bars"}, radius=7, limit=16):
            pos = row["pos"]
            if (max(abs(pos[0] - x), abs(pos[2] - z)) > 2 or not y - 1 <= pos[1] <= y + 2
                    or not 1 < math.dist(pos, origin) <= 3.25
                    or (pos[0], pos[2]) == (math.floor(origin[0]), math.floor(origin[2]))):
                continue
            block = self._block(guard, pos)
            if block.get("name") != "iron_bars" or block.get("diggable") is not True:
                continue
            if not trace_block_visibility(origin, pos, lambda p: self._block(guard, p))["visible"]:
                continue
            # A bar removal never hits the crystal. The next shot still
            # requires the independent blast-distance check.
            self._result(guard, guard.write(self.client.dig, pos, collect=False))
            if self._block(guard, pos).get("name") not in AIR:
                guard.fail_uncertain("action_unconfirmed", "Crystal cage opening could not be verified")
            return {"removed_cage_bar": pos}
        return None

    def _dragon(self, guard, status, origin, args, state):
        advancement = self._dragon_state(guard)
        if advancement is True and state.get("dragon_baseline") is False and state.get("dragon_seen"):
            return {"dragon_defeated": True, "advancement": DRAGON_ADVANCEMENT}
        if "dragon_baseline" not in state and advancement is not None:
            return {"dragon_baseline": advancement}
        if not state.get("end_land"):
            return self._reach_end_land(guard, origin)
        clouds = self._entities(guard, {"area_effect_cloud"}, radius=7)
        if clouds:
            threat = parse_pos(clouds[0].get("pos"))
            if threat is not None:
                return self._walk(guard, origin, state, toward=[2 * origin[i] - threat[i] for i in range(3)])
        dragons = self._entities(guard, {"ender_dragon"}, radius=128)
        crystals = self._entities(guard, {"end_crystal"}, radius=64)
        if dragons and not state.get("dragon_seen"):
            return {"dragon_seen": True}
        if crystals:
            open_cage = self._cage(guard, origin, crystals[0])
            if open_cage is not None:
                return open_cage
            repeated = state.get("crystal_shots", {})
            crystal = next((row for row in crystals if (pos := parse_pos(row.get("pos"))) is not None
                            and 12 <= math.dist(pos, origin) <= 55
                            and (row["id"] != repeated.get("id") or repeated.get("count", 0) < 2)), None)
            if crystal:
                return self._strike(guard, origin, crystal, ranged=True)
            nearest = parse_pos(crystals[0].get("pos"))
            if nearest is not None:
                if math.dist(nearest, origin) < 12:
                    nearest = [2 * origin[i] - nearest[i] for i in range(3)]
                return self._walk(guard, origin, state, toward=nearest)
        if dragons:
            pos = parse_pos(dragons[0]["pos"])
            if pos is not None and math.dist(pos, origin) <= 50:
                return self._strike(guard, origin, dragons[0], ranged=math.dist(pos, origin) > 2.8)
        land = self._find(guard, {"end_stone"}, radius=64, limit=24)
        if not land:
            raise WorkflowStepError("campaign_end_island", "The main End island is not loaded")
        floor = land[0]["pos"]
        foot = [floor[0], floor[1] + 1, floor[2]]
        if math.dist(foot, origin) > 3 and self._stand(guard, foot):
            return self._navigate(guard, foot, bridge=True)
        return self._walk(guard, origin, state, toward=[0, origin[1], 0])

    def _leave_end(self, guard, status, origin, args, state):
        found = self._portal(guard, status, origin, state, portal="end_portal", destination="overworld")
        return found or self._walk(guard, origin, state, toward=[0, origin[1], 0])
