"""Survival construction from a surveyed projection, using the normal action lock."""

from collections import Counter
import math

from core.block_geometry import trace_block_visibility
from core.build_projection import wood_material
from core.domain import WOOD_SPECIES, parse_pos
from skills import SkillResult
from skills.action_workflow import WorkflowStepError
from skills.shelter import ShelterSkills
from skills.survival import SurvivalSkills


SIDES = ((0, -1, 0), (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1), (0, 1, 0))
SURVEY_BATCH = 80
PLACE_BATCH = 5


def _adjacent(pos, offset):
    return tuple(a + b for a, b in zip(pos, offset))


def projection_report(projected, done):
    """Count physical blocks still missing, plus the material for the next stage."""
    remaining = [block for index, block in enumerate(projected) if index not in done]
    stage = remaining[0].stage if remaining else None
    return {"confirmed": len(projected) - len(remaining), "total": len(projected), "stage": stage,
            "remaining": dict(Counter(block.item for block in remaining)),
            "stage_materials": dict(Counter(block.item for block in remaining if block.stage == stage))}


class ProjectionBuilder:
    def __init__(self, driver, blueprint):
        self.driver = driver
        self.blueprint = blueprint

    @staticmethod
    def _same(block, read):
        current = read(block.pos)
        if current.get("name") != block.item:
            return False
        if not block.item.endswith("_door"):
            return True
        upper = read(_adjacent(block.pos, (0, 1, 0)))
        return (current.get("properties", {}).get("half") == "lower"
                and upper.get("name") == block.item and upper.get("properties", {}).get("half") == "upper")

    @staticmethod
    def _vacant(row):
        return row.get("name") in ShelterSkills.REPLACEABLE_SPACE and SurvivalSkills._safe_block(row)

    @staticmethod
    def _support(row):
        return row.get("name") in SurvivalSkills.SUPPORT | {"glass"} and SurvivalSkills._safe_block(row)

    def step(self, guard, status, origin, args, saved):
        anchor, rotation = args["anchor"], args["orientation"]
        key = [self.blueprint.digest, anchor, rotation, status["dimension"]]
        prior = saved if saved.get("key") == key else {}
        if math.dist(origin, anchor) > 21:
            moved = self.driver._walk(guard, origin, {}, toward=anchor, allow_detour=False)
            return moved if isinstance(moved, SkillResult) else {**moved, "message": "Return to the projected building site"}
        uses_wood = self.blueprint.uses_wood
        if uses_wood and "wood_species" not in prior:
            prior = {}  # Survey legacy placements before trusting their stock or material.
        wood = prior.get("wood_species")
        hint = args.get("wood_hint")
        if hint not in WOOD_SPECIES:
            hint = None
        projected = self.blueprint.at(anchor, rotation, wood)
        checked = prior.get("scan", 0)
        done = set(prior.get("placed", []))
        resume = {"key": key, "placed": sorted(done), "scan": checked}
        cache = {}

        def read(pos):
            pos = tuple(pos)
            if pos not in cache:
                cache[pos] = self.driver._block(guard, pos)
            return cache[pos]

        def report(message):
            resolved = tuple(block.with_wood(wood) for block in projected) if wood else projected
            summary = projection_report(resolved, done)
            if uses_wood and wood is None:
                for field in ("remaining", "stage_materials"):
                    summary[field] = {item: count for item, count in summary[field].items()
                                      if wood_material(item) is None}
            return {"projection": {**resume, **summary, "placed": sorted(done), "wood_species": wood},
                    "message": message}

        if checked < len(projected):
            floor = min(block.pos[1] for block in projected)
            end = min(checked + SURVEY_BATCH, len(projected))
            for i in range(checked, end):
                block = projected[i]
                expected = wood_material(block.item)
                if expected is not None and wood is None:
                    observed = wood_material(read(block.pos).get("name"))
                    if observed is not None and observed[1] == expected[1]:
                        candidate = block.with_wood(observed[0])
                        if self._same(candidate, read):
                            wood = observed[0]
                block = block.with_wood(wood)
                if self._same(block, read):
                    done.add(i)
                    continue
                if (not self._vacant(read(block.pos))
                        or block.item.endswith("_door") and not self._vacant(read(_adjacent(block.pos, (0, 1, 0))))
                        or block.pos[1] == floor and not self._support(read(_adjacent(block.pos, SIDES[0])))):
                    raise WorkflowStepError("projection_site_blocked", "The projection meets terrain, an existing block, or missing floor support",
                                            blocker_pos=list(block.pos))
            resume["scan"] = end
            if end == len(projected) and uses_wood and wood is None:
                wood = hint
            return report("Read another part of the projection against the current world")

        if uses_wood and wood is None:
            wood = hint
            return report("Selected the timber for the whole projection" if wood else "Look for suitable timber")

        if len(done) == len(projected):
            start = prior.get("verify", 0)
            end = min(start + SURVEY_BATCH, len(projected))
            changed = {i for i in range(start, end) if not self._same(projected[i], read)}
            done.difference_update(changed)
            resume["verify"] = 0 if changed else end
            resume["complete"] = not changed and end == len(projected)
            return report("Verified the completed projection" if resume["complete"]
                          else "Verify the actual completed layers")

        counts = self.driver._counts(guard)
        stage = next(block.stage for i, block in enumerate(projected) if i not in done)
        layer = min(block.pos[1] for i, block in enumerate(projected) if i not in done and block.stage == stage)
        work = [(i, block) for i, block in enumerate(projected)
                if i not in done and block.stage == stage and block.pos[1] == layer]
        work.sort(key=lambda row: math.dist(row[1].pos, origin))
        ready = []
        current = status

        def reachable(pos, cell):
            eye = [pos[0], pos[1] + 1.62, pos[2]]
            center = [v + .5 for v in cell]
            return (math.dist(pos, cell) <= 3.95 and math.dist(eye, center) <= 4.8
                    and not ShelterSkills._body_intersects(cell, pos)
                    and trace_block_visibility(pos, cell, read, max_queries=20)["visible"] is True)

        for i, block in work:
            if self._same(block, read):
                done.add(i)
                continue
            if not self._vacant(read(block.pos)):
                raise WorkflowStepError("projection_site_blocked", "An existing block changed in the construction area",
                                        blocker_pos=list(block.pos))
            if (not counts.get(block.item, 0) or not any(
                    self._support(read(_adjacent(block.pos, direction))) for direction in SIDES)):
                continue
            ready.append(block)
            if not reachable(current["pos"], block.pos):
                continue
            if any(ShelterSkills._body_intersects(block.pos, row["pos"])
                   for row in self.driver._entities(guard, {"player", "hostile", "animal", "mob"}, radius=8)
                   if parse_pos(row.get("pos")) is not None):
                continue
            guard.write(self.driver.client.place_block, *block.pos, item=block.item, stop_on=["hurt", "death"])
            cache.pop(block.pos, None)
            cache.pop(_adjacent(block.pos, (0, 1, 0)), None)
            fresh = self.driver._counts(guard)
            if not self._same(block, read) or counts[block.item] - fresh.get(block.item, 0) != 1:
                guard.fail_uncertain("action_unconfirmed", "The projected placement or inventory decrement was not verified")
            done.add(i)
            counts = fresh
            current = guard.check_status()
            if len(done) - len(resume["placed"]) >= PLACE_BATCH or math.dist(current["pos"], origin) > .35:
                break
        if len(done) > len(resume["placed"]):
            return report("Verified consecutive projected blocks from one stance")
        if not ready:
            if not any(counts.get(block.item, 0) for _, block in work):
                return report("The next layer needs more stock")
            raise WorkflowStepError("projection_no_support", "None of the available blocks can attach to this layer yet",
                                    blocker_pos=list(work[0][1].pos))

        # Only stand on observed support. Failed waypoints are temporary and scoped to this layer.
        rejected = [tuple(pos) for pos in prior.get("rejected", [])] if prior.get("layer") == [stage, layer] else []
        candidates = set()
        for block in ready[:8]:
            x, y, z = block.pos
            candidates.update((x + dx, y + dy, z + dz) for dx in range(-3, 4) for dz in range(-3, 4)
                              for dy in (-3, -2, -1, 0, 1, 2) if dx * dx + dz * dz <= 10)
        for point in sorted(candidates, key=lambda p: (math.dist(origin, p), p))[:140]:
            foot = [point[0] + .5, point[1], point[2] + .5]
            if (point in rejected or math.dist(foot, origin) < .6 or not self.driver._stand(guard, point, cache=cache)
                    or not any(reachable(foot, block.pos) for block in ready[:8])):
                continue
            resume.update(layer=[stage, layer], rejected=[list(p) for p in rejected])
            try:
                return {**self.driver._move(guard, origin, foot), **report("Reach the next group of projected blocks")}
            except WorkflowStepError as error:
                if error.code not in {"stalled", "path_not_found", "no_path", "campaign_waypoint_missed"}:
                    raise
                resume["rejected"] = [list(p) for p in (*rejected, point)]
                return report("Try another observed stance for this building layer")
        raise WorkflowStepError("projection_no_stance", "No reachable supported stance for this projection layer")
