"""Fixed, no-demolition shelter construction from fresh bounded geometry.

``pos`` is the integer centre of the 5x5 footprint at floor/feet height.
The doorway is always on the north wall. Geometry verification is not a claim
of monster-proofing or adequate light: the official API exposes no light level.
"""

import copy
import math
import time

from core.block_geometry import trace_block_visibility
from . import SkillResult
from .survival import SurvivalSkills, _Failure


class ShelterSkills(SurvivalSkills):
    ARGUMENTS = {
        "plan_shelter": ({"pos", "material"}, {"pos", "material"}),
        "inspect_shelter": ({"pos"}, {"pos"}),
        "build_shelter_step": ({"pos", "material"}, {"pos", "material"}),
    }
    MATERIALS = SurvivalSkills.PLANKS | {"cobblestone", "stone", "dirt"}
    NATURAL_GROUND = frozenset({"grass_block", "dirt", "coarse_dirt", "rooted_dirt",
                                "stone", "andesite", "granite", "diorite", "deepslate", "moss_block"})
    DOORS = frozenset(wood + "_door" for wood in SurvivalSkills.WOODS)
    INTERIOR_PASSABLE = SurvivalSkills.AIR | {"torch", "wall_torch"}
    MAX_READS = 150
    # Checked between calls and used as each read's remaining HTTP timeout.
    # An already dispatched place keeps the client's bounded action timeout;
    # expiration after dispatch is a failure, never assumed placement success.
    READ_BUDGET_SECONDS = 45

    @staticmethod
    def _check(context):
        SurvivalSkills._check(context)
        if time.monotonic() >= context.get("shelter_deadline", math.inf):
            raise _Failure("shelter_budget_exhausted", "Shelter observation budget expired")

    @staticmethod
    def _read_options(context):
        ShelterSkills._check(context)
        return {"timeout_seconds": min(15, context["shelter_deadline"] - time.monotonic())}

    def _count_read(self, context, *, block=False):
        self._check(context)
        if context["read_calls"] >= self.MAX_READS:
            raise _Failure("shelter_budget_exhausted", "Shelter read limit reached")
        context["read_calls"] += 1
        if block:
            context["block_reads"] += 1

    def _status(self, context):
        self._count_read(context)
        status = super()._status(context)
        self._overworld(status)
        if status.get("gamemode") != "survival":
            raise _Failure("unsafe_state", "Shelter skills require survival mode")
        if any(status.get(k) for k in ("in_water", "in_lava", "on_fire", "submerged")):
            raise _Failure("unsafe_state", "Leave water, lava or fire before shelter work")
        marker = (status.get("dimension"), copy.deepcopy(status.get("last_death")))
        if "life_marker" in context and marker != context["life_marker"]:
            raise _Failure("life_changed", "Life changed during the shelter operation")
        if status["health"] < context.get("initial_health", status["health"]):
            raise _Failure("unsafe_state", "Damage occurred during the shelter operation")
        context.setdefault("life_marker", marker)
        context.setdefault("initial_health", status["health"])
        return status

    def _inventory(self, context):
        self._count_read(context)
        return super()._inventory(context)

    def _block(self, pos, context):
        self._count_read(context, block=True)
        block = super()._block(pos, context)
        if block.get("properties") is not None and not isinstance(block["properties"], dict):
            raise _Failure("invalid_response", "Shelter block properties are malformed")
        return block

    def _entities(self, context):
        self._count_read(context)
        entities = self.client.get_entities(radius=12, **self._read_options(context))
        self._check(context)
        if not isinstance(entities, list) or len(entities) > 256:
            raise _Failure("invalid_response", "Shelter entity observation is malformed or oversized")
        for row in entities:
            if not isinstance(row, dict):
                raise _Failure("invalid_response", "Shelter entity observation is malformed")
            self._pos(row.get("pos"))
        return entities

    @staticmethod
    def _blueprint(anchor):
        x, y, z = anchor
        doorway = (x, y, z - 2)
        walls = [(cx, y + dy, cz) for dy in (0, 1)
                 for cx in range(x - 2, x + 3) for cz in range(z - 2, z + 3)
                 if (abs(cx - x) == 2 or abs(cz - z) == 2) and (cx, cz) != (x, z - 2)]
        # An inside builder cannot see an upper corner after both adjacent
        # walls exist. Place each layer's corners before filling its edges.
        walls.sort(key=lambda p: (p[1], not (abs(p[0] - x) == 2 and abs(p[2] - z) == 2), p))
        roof = [(cx, y + 2, cz) for cx in range(x - 2, x + 3) for cz in range(z - 2, z + 3)]
        roof.sort(key=lambda cell: (min(cell[0] - x + 2, x + 2 - cell[0], cell[2] - z + 2, z + 2 - cell[2]),
                                   not (abs(cell[0] - x) == 2 and abs(cell[2] - z) == 2), cell))
        return walls, roof, doorway

    @staticmethod
    def _body_intersects(block_pos, feet):
        # Include rounding tolerance in the API's one-decimal entity coordinates.
        return (feet[0] - 0.36 < block_pos[0] + 1 and feet[0] + 0.36 > block_pos[0]
                and feet[2] - 0.36 < block_pos[2] + 1 and feet[2] + 0.36 > block_pos[2]
                and feet[1] - 0.05 < block_pos[1] + 1 and feet[1] + 1.85 > block_pos[1])

    @staticmethod
    def _false(value):
        return value is False or value == "false"

    def _door(self, anchor, cache):
        x, y, z = anchor
        lower_pos, upper_pos = (x, y, z - 2), (x, y + 1, z - 2)
        lower, upper = cache[lower_pos], cache[upper_pos]
        lp, up = lower.get("properties") or {}, upper.get("properties") or {}
        result = {"pos": list(lower_pos), "second_pos": list(upper_pos), "complete": False,
                  "frame_verified": False, "open": None,
                  "name": lower["name"], "facing": lp.get("facing"), "reason": "door_missing"}
        if lower["name"] in self.AIR and upper["name"] in self.AIR:
            return result
        if (lower["name"] not in self.DOORS or upper["name"] != lower["name"]
                or lp.get("half") != "lower" or up.get("half") != "upper"
                or lp.get("facing") != up.get("facing")
                or lp.get("hinge") not in {"left", "right"} or lp.get("hinge") != up.get("hinge")):
            result["reason"] = "door_unverified"
        elif lp.get("facing") not in {"north", "south"}:
            result["reason"] = "door_orientation"
        else:
            lower_open = False if self._false(lp.get("open")) else True if lp.get("open") is True or lp.get("open") == "true" else None
            upper_open = False if self._false(up.get("open")) else True if up.get("open") is True or up.get("open") == "true" else None
            if lower_open is None or upper_open is None or lower_open != upper_open:
                result["reason"] = "door_unverified"
            else:
                result.update(frame_verified=True, open=lower_open, complete=not lower_open,
                              reason="door_not_closed" if lower_open else None)
        return result

    def _scan(self, anchor, context):
        status = self._status(context)
        if math.dist(status["pos"], anchor) > 24:
            raise _Failure("out_of_range", "Shelter site is more than 24 blocks away")
        x, y, z = anchor
        cache = {}
        for cx in range(x - 2, x + 3):
            for cz in range(z - 2, z + 3):
                for cy in range(y - 1, y + 3):
                    cache[(cx, cy, cz)] = self._block([cx, cy, cz], context)
        # The sole exit needs a loaded floor and two clear body cells outside.
        for cy in range(y - 1, y + 2):
            cache[(x, cy, z - 3)] = self._block([x, cy, z - 3], context)
        final = self._status(context)
        if math.dist(status["pos"], final["pos"]) > 0.75:
            raise _Failure("observation_changed", "Bot moved while the shelter site was scanned")
        return cache, final

    def _beds(self, anchor, cache):
        x, y, z = anchor
        beds = []
        directions = {"north": (0, -1), "south": (0, 1), "west": (-1, 0), "east": (1, 0)}
        for cx in range(x - 1, x + 2):
            for cz in range(z - 1, z + 2):
                pos = (cx, y, cz)
                block = cache[pos]
                props = block.get("properties") or {}
                facing = props.get("facing")
                if not block["name"].endswith("_bed") or props.get("part") != "foot" or facing not in directions:
                    continue
                dx, dz = directions[facing]
                head_pos = (cx + dx, y, cz + dz)
                if not (abs(head_pos[0] - x) <= 1 and abs(head_pos[2] - z) <= 1):
                    continue
                head = cache[head_pos]
                hp = head.get("properties") or {}
                if head["name"] == block["name"] and hp.get("part") == "head" and hp.get("facing") == facing:
                    occupied = (props.get("occupied") in (True, "true") or hp.get("occupied") in (True, "true"))
                    known = all(p.get("occupied") in (True, False, "true", "false") for p in (props, hp))
                    beds.append({"foot_pos": list(pos), "head_pos": list(head_pos), "name": block["name"],
                                 "occupied": occupied if known else None, "verified": True})
        return beds

    def _evidence(self, anchor, material, cache, status):
        x, y, z = anchor
        walls, roof, doorway = self._blueprint(anchor)
        floor = [(cx, y - 1, cz) for cx in range(x - 2, x + 3) for cz in range(z - 2, z + 3)]
        floor_ok = all(cache[p]["name"] in self.NATURAL_GROUND and self._safe_block(cache[p]) for p in floor)
        hazards = [list(pos) for pos, block in cache.items() if not self._safe_block(block)]
        filled = lambda pos: cache[pos]["name"] in self.MATERIALS and self._safe_block(cache[pos])
        missing = [(p, "wall" if p in walls else "roof") for p in walls + roof if cache[p]["name"] in self.AIR]
        occupied = [list(p) for p in walls + roof if cache[p]["name"] not in self.AIR and not filled(p)]
        door = self._door(anchor, cache)
        corridor = [(x, cy, z - 1) for cy in (y, y + 1)] + [(x, cy, z - 3) for cy in (y, y + 1)]
        exit_floor = cache[(x, y - 1, z - 3)]
        exit_ok = (exit_floor["name"] in self.NATURAL_GROUND and self._safe_block(exit_floor)
                   and all(cache[p]["name"] in self.INTERIOR_PASSABLE and self._safe_block(cache[p]) for p in corridor)
                   and (door["frame_verified"] or all(cache[p]["name"] in self.AIR for p in (doorway, (x, y + 1, z - 2)))))
        candidates = []
        for cx in range(x - 1, x + 2):
            for cz in range(z - 1, z + 2):
                if all(cache[(cx, cy, cz)]["name"] in self.INTERIOR_PASSABLE for cy in (y, y + 1)):
                    candidates.append([cx + 0.5, y, cz + 0.5])
        # Only cells connected to the reserved inside doorway may be proposed.
        reachable = {(x, z - 1)} if [x + 0.5, y, z - 0.5] in candidates else set()
        cells = {(math.floor(p[0]), math.floor(p[2])) for p in candidates}
        while True:
            expanded = reachable | {cell for cell in cells if any((cell[0] + dx, cell[1] + dz) in reachable for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)))}
            if expanded == reachable:
                break
            reachable = expanded
        candidates = [p for p in candidates if (math.floor(p[0]), math.floor(p[2])) in reachable]
        candidates.sort(key=lambda p: math.dist(status["pos"], p))
        next_gap = None
        for pos, stage in missing:
            adjacent = [(pos[0] + dx, pos[1] + dy, pos[2] + dz) for dx, dy, dz in ((0, -1, 0), (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1), (0, 1, 0))]
            if any(p in cache and cache[p]["name"] in self.MATERIALS | self.NATURAL_GROUND and self._safe_block(cache[p]) for p in adjacent):
                next_gap = {"pos": list(pos), "item": material, "stage": stage}
                break
        if next_gap:
            target = next_gap["pos"]
            candidates = [p for p in candidates if math.dist(p, target) <= 3.5
                          and not self._body_intersects(target, p)
                          and trace_block_visibility(p, target, lambda _: None, cache=cache,
                                                     max_queries=0, max_distance=4.5)["visible"] is True]
            candidates.sort(key=lambda p: (math.dist(p, target), math.dist(status["pos"], p)))
        geometry = {"floor_verified": floor_ok, "walls_complete": all(filled(p) for p in walls),
                    "roof_complete": all(filled(p) for p in roof), "door_complete": door["complete"], "exit_verified": exit_ok}
        structure = (floor_ok and geometry["walls_complete"] and geometry["roof_complete"]
                     and door["frame_verified"] and exit_ok and not hazards)
        geometry["structure_complete"] = structure
        verified = all(geometry.values()) and not hazards
        inside = (x - 1 + 0.36 <= status["pos"][0] <= x + 2 - 0.36
                  and z - 1 + 0.36 <= status["pos"][2] <= z + 2 - 0.36
                  and y - 0.05 <= status["pos"][1] <= y + 0.7)
        return {"pos": list(anchor), "material": material, "geometry": geometry,
                "fresh": True, "dimension": status["dimension"], "geometry_complete": verified, "structure_complete": structure,
                "shelter_verified": verified, "inside": inside,
                "interior_pos": candidates[0] if candidates else None, "return_route_verified": False,
                "protected_from_spawns": "unknown", "lighting": {"state": "unknown", "torch_positions": [list(p) for p, b in cache.items() if b["name"] in {"torch", "wall_torch"}]},
                "beds": self._beds(anchor, cache), "door": door, "missing_blocks": len(missing),
                "occupied_blueprint_cells": occupied[:8], "hazards": hazards[:8], "next_gap": next_gap,
                "needs_door": None if door["complete"] else {"pos": list(doorway), "allowed_facing": ["north", "south"],
                    "stand_pos": [x + 0.5, y, z - 2.5], "reason": door["reason"]},
                "needs_position": False, "stand_candidates": candidates[:8], "executed": False}

    def _prepare(self, args, context):
        anchor = self._pos(args["pos"], block=True)
        material = args.get("material")
        if "material" in args and not isinstance(material, str):
            raise _Failure("bad_args", "Shelter material must be an item name")
        if material is not None and material not in self.MATERIALS:
            raise _Failure("unsupported_item", "Shelter material must be a supported full solid building block")
        cache, status = self._scan(anchor, context)
        evidence = self._evidence(anchor, material, cache, status)
        context["evidence"] = evidence
        return anchor, material, cache, status, evidence

    def _plan_shelter(self, args, context):
        anchor, material, cache, status, evidence = self._prepare(args, context)
        _, y, _ = anchor
        if not evidence["geometry"]["floor_verified"] or not evidence["geometry"]["exit_verified"] or evidence["hazards"]:
            raise _Failure("unsafe_site", "Shelter needs a flat natural-type solid floor and a clear supported exit")
        if any(block["name"] not in self.AIR for pos, block in cache.items() if pos[1] >= y):
            raise _Failure("site_occupied", "Planning requires three clear layers; existing blocks will not be removed")
        evidence["blueprint"] = {"size": [5, 3, 5], "wall_blocks": 30, "roof_blocks": 25,
                                 "total_building_blocks": 55, "order": ["lower_walls", "upper_walls", "roof_from_edges", "door"],
                                 "natural_ownership_verified": False}
        return evidence

    def _inspect_shelter(self, args, context):
        return self._prepare(args, context)[-1]

    def _build_shelter_step(self, args, context):
        anchor, material, cache, status, evidence = self._prepare(args, context)
        if (not evidence["geometry"]["floor_verified"] or not evidence["geometry"]["exit_verified"]
                or evidence["hazards"] or evidence["occupied_blueprint_cells"]):
            raise _Failure("unsafe_site", "Shelter site changed, is occupied, or has an unsafe exit")
        gap = evidence["next_gap"]
        if gap is None:
            if evidence["missing_blocks"]:
                raise _Failure("unsupported_placement", "No missing blueprint block has verified adjacent support")
            evidence["step_complete"] = True
            return evidence  # Door work belongs to the separately verified place_door skill.
        target = gap["pos"]
        if status.get("on_ground") is not True:
            raise _Failure("unsafe_state", "Building requires a verified grounded status")
        if abs(status["pos"][1] - anchor[1]) > 0.15:
            evidence["needs_position"] = True
            raise _Failure("needs_position", "Stand on the shelter floor level before building")
        visibility = trace_block_visibility(status["pos"], target, lambda p: self._block(p, context),
                                            cache=cache, max_queries=16, max_distance=4.5)
        evidence["visibility"] = visibility
        if math.dist(status["pos"], target) > 3.5 or visibility["visible"] is not True:
            evidence["needs_position"] = True
            if not evidence["stand_candidates"]:
                raise _Failure("no_build_stance", "No observed interior stand cell can reach the next blueprint block")
            raise _Failure("needs_position", "Move to a freshly checked stand candidate before the next building block")
        before, _ = self._inventory(context)
        if before.get(material, 0) < 1:
            raise _Failure("missing_item", "No shelter building material is in inventory")

        def validate(current):
            if self._body_intersects(target, current["pos"]):
                raise _Failure("body_occupied", "The next block intersects the bot's body")
            for entity in self._entities(context):
                if entity.get("type") in {"item", "orb", "experience_orb"}:
                    continue
                if self._body_intersects(target, entity["pos"]):
                    raise _Failure("body_occupied", "The next block intersects an observed entity")
            current_target = self._block(target, context)
            if current_target["name"] not in self.AIR or not self._safe_block(current_target):
                raise _Failure("site_changed", "The intended blueprint cell is no longer empty")
            adjacent = [[target[0] + dx, target[1] + dy, target[2] + dz] for dx, dy, dz in
                        ((0, -1, 0), (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1), (0, 1, 0))]
            supports = [p for p in adjacent if tuple(p) in cache and cache[tuple(p)]["name"] in self.MATERIALS | self.NATURAL_GROUND]
            if not supports:
                raise _Failure("site_changed", "Blueprint adjacency is no longer supported")
            support = self._block(supports[0], context)
            if support["name"] not in self.MATERIALS | self.NATURAL_GROUND or not self._safe_block(support):
                raise _Failure("site_changed", "Placement support changed after scanning")
            # Recheck the reserved passage just before the single mutation.
            for p in ((anchor[0], anchor[1], anchor[2] - 1), (anchor[0], anchor[1] + 1, anchor[2] - 1),
                      (anchor[0], anchor[1], anchor[2] - 3), (anchor[0], anchor[1] + 1, anchor[2] - 3)):
                block = self._block(list(p), context)
                if block["name"] not in self.INTERIOR_PASSABLE or not self._safe_block(block):
                    raise _Failure("site_changed", "The reserved entrance passage changed")
            final = self._status(context)
            if self._body_intersects(target, final["pos"]):
                raise _Failure("body_occupied", "Bot moved into the intended block before placement")
            if math.dist(final["pos"], target) > 3.5:
                raise _Failure("needs_position", "Bot moved out of reach before placement")
            if final.get("on_ground") is not True or abs(final["pos"][1] - anchor[1]) > 0.15:
                raise _Failure("unsafe_state", "Bot is no longer standing on the verified floor level")
            sight = trace_block_visibility(final["pos"], target, lambda p: self._block(p, context),
                                           max_queries=16, max_distance=4.5)
            evidence["prewrite_visibility"] = sight
            if sight["visible"] is not True:
                raise _Failure("site_changed", "Placement line of sight changed after scanning")
            latest = self._status(context)
            if (latest.get("on_ground") is not True or math.dist(latest["pos"], final["pos"]) > 0.25
                    or self._body_intersects(target, latest["pos"])):
                raise _Failure("observation_changed", "Bot moved while the final placement ray was checked")

        self._action(context, self.client.place_block, *target, item=material, _validate_status=validate)
        final_status = self._status(context)
        if final_status.get("on_ground") is not True:
            raise _Failure("unsafe_state", "Bot lost its grounded state during placement")
        placed = self._block(target, context)
        after, _ = self._inventory(context)
        used = before.get(material, 0) - after.get(material, 0)
        evidence.update(executed=True, placed_pos=target, placed=placed["name"], used=used)
        if placed["name"] != material or used != 1:
            raise _Failure("place_unverified", "One matching world block and exactly one consumed item were not both verified")
        cache[tuple(target)] = placed
        evidence.update(self._evidence(anchor, material, cache, final_status))
        evidence.update(executed=True, placed_pos=target, placed=material, used=used, step_complete=True)
        return evidence

    def execute(self, name, args, cancel_event=None, **options):
        context = {"cancel": cancel_event, "acted": False, "read_calls": 0, "block_reads": 0,
                   "shelter_deadline": time.monotonic() + self.READ_BUDGET_SECONDS}
        data = {"skill": name, "executed": False, "shelter_verified": False}
        try:
            if not isinstance(name, str) or name not in self.ARGUMENTS:
                raise _Failure("unknown_skill", "Unknown shelter skill")
            allowed, required = self.ARGUMENTS[name]
            if options or not isinstance(args, dict) or set(args) - allowed or required - set(args):
                raise _Failure("bad_args", "Missing or unsupported shelter arguments")
            status = self._status(context)
            context["dimension"] = status["dimension"]
            data["pos_before"] = status["pos"]
            data.update(getattr(self, "_" + name)(args, context))
            self._check(context)
            return SkillResult(True, "Shelter geometry observed; completion is reported separately", data)
        except Exception as error:
            data.update(context.get("evidence", {}))
            data.update(getattr(error, "data", {}))
            data["executed"] = context["acted"]
            code = getattr(error, "code", "internal_error")
            if context["acted"]:
                try:
                    self.client.stop()
                    data["cleanup"] = "stop_requested"
                except Exception as cleanup_error:
                    data["cleanup_error"] = getattr(cleanup_error, "code", "internal_error")
            return SkillResult(False, str(error), data, code)
        finally:
            data.update(read_calls=context["read_calls"], block_reads=context["block_reads"])
