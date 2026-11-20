"""Rotatable 5x3x5 shelter skill backed by StructureSpec and ActionWorkflow.

Preserves BotSleep's conservative single-step daily contract (`plan_shelter`,
`inspect_shelter`, `build_shelter_step` with ``no-demolition`` guarantees and
per-call observation budgets) across all four cardinal orientations
(``north``, ``south``, ``east``, ``west``), while adding end-to-end
``ActionWorkflow`` site-finding, obstacle clearing, floor patching, shell
construction, and automatic door installation (`build_shelter` /
`find_shelter_sites`).
"""

import copy
import math
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from core.block_geometry import trace_block_visibility
from core.domain import AIR, parse_pos
from core.structure_spec import (
    DEFAULT_REPLACEABLE,
    SHELTER_5X5_SPEC,
    SHELTER_BUILDING_BLOCKS,
    SHELTER_STRUCTURE_BLOCKS,
    SHELTER_CLEARABLE_BLOCKS,
    SHELTER_GROUND_BLOCKS,
    WOODEN_DOOR_BLOCKS,
    evaluate_shelter_site,
    find_shelter_sites as find_shelter_sites_spec,
    inspect_structure,
    normalize_orientation,
    rotate_facing,
    transform_rel_pos,
)
from skills.action_workflow import (
    ActionStep,
    ActionWorkflowRunner,
    WorkflowContext,
    WorkflowGuard,
    WorkflowStepError,
    aim_at,
    dig_block,
    place_block,
    stand_at,
    wait_until,
)
from . import SkillResult
from .survival import SurvivalSkills, _Failure


class ShelterSkills(SurvivalSkills):
    ARGUMENTS = {
        "plan_shelter": ({"pos", "material", "orientation"}, {"pos", "material"}),
        "inspect_shelter": ({"pos", "orientation"}, {"pos"}),
        "build_shelter_step": ({"pos", "material", "orientation"}, {"pos", "material"}),
    }
    MATERIALS = SHELTER_BUILDING_BLOCKS
    STRUCTURE_BLOCKS = SHELTER_STRUCTURE_BLOCKS
    NATURAL_GROUND = SHELTER_GROUND_BLOCKS
    DOORS = WOODEN_DOOR_BLOCKS
    # Both have empty collision in 1.21.11 and are replaceable by API.place.
    # Do not reuse the broader workflow set: snow can have partial collision.
    REPLACEABLE_PLANTS = frozenset({"leaf_litter", "short_grass"})
    REPLACEABLE_SPACE = SurvivalSkills.AIR | REPLACEABLE_PLANTS
    INTERIOR_PASSABLE = REPLACEABLE_SPACE | {"torch", "wall_torch"}
    MAX_READS = 150
    # Checked between calls and used as each read's remaining HTTP timeout.
    # An already dispatched place keeps the client's bounded action timeout;
    # expiration after dispatch is a failure, never assumed placement success.
    READ_BUDGET_SECONDS = 45

    @staticmethod
    def _normalize_orientation(orientation: Any) -> str:
        if orientation is None:
            return "north"
        try:
            return normalize_orientation(orientation)
        except ValueError as exc:
            raise _Failure("bad_args", str(exc)) from None

    @staticmethod
    def _axis_facings(orientation: str) -> Tuple[str, str]:
        door_facing = rotate_facing("south", orientation)
        opposite = rotate_facing("north", orientation)
        return tuple(sorted((door_facing, opposite)))  # type: ignore[return-value]

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
    def _blueprint(anchor, orientation: str = "north"):
        norm_orient = normalize_orientation(orientation)
        int_anchor = [int(v) for v in anchor]
        walls: List[Tuple[int, int, int]] = []
        roof: List[Tuple[int, int, int]] = []
        for rule in SHELTER_5X5_SPEC.cells:
            wpos = tuple(transform_rel_pos(rule.rel_pos, int_anchor, norm_orient, integer=True))
            if rule.stage in {"lower_walls", "upper_walls"}:
                walls.append(wpos)  # type: ignore[arg-type]
            elif rule.stage == "roof":
                roof.append(wpos)  # type: ignore[arg-type]
        doorway = tuple(transform_rel_pos((0, 0, 2), int_anchor, norm_orient, integer=True))
        return walls, roof, doorway  # type: ignore[return-value]

    @staticmethod
    def _body_intersects(block_pos, feet):
        # Include rounding tolerance in the API's one-decimal entity coordinates.
        return (
            feet[0] - 0.36 < block_pos[0] + 1
            and feet[0] + 0.36 > block_pos[0]
            and feet[2] - 0.36 < block_pos[2] + 1
            and feet[2] + 0.36 > block_pos[2]
            and feet[1] - 0.05 < block_pos[1] + 1
            and feet[1] + 1.85 > block_pos[1]
        )

    @staticmethod
    def _false(value):
        return value is False or value == "false"

    def _door(self, anchor, cache, orientation: str = "north"):
        norm_orient = normalize_orientation(orientation)
        lower_pos = tuple(transform_rel_pos((0, 0, 2), anchor, norm_orient, integer=True))
        upper_pos = tuple(transform_rel_pos((0, 1, 2), anchor, norm_orient, integer=True))
        lower, upper = cache[lower_pos], cache[upper_pos]
        lp, up = lower.get("properties") or {}, upper.get("properties") or {}
        allowed_facings = set(self._axis_facings(norm_orient))
        result = {
            "pos": list(lower_pos),
            "second_pos": list(upper_pos),
            "complete": False,
            "frame_verified": False,
            "open": None,
            "name": lower["name"],
            "facing": lp.get("facing"),
            "reason": "door_missing",
        }
        if lower["name"] in self.REPLACEABLE_SPACE and upper["name"] in self.REPLACEABLE_SPACE:
            return result
        if (
            lower["name"] not in self.DOORS
            or upper["name"] != lower["name"]
            or lp.get("half") != "lower"
            or up.get("half") != "upper"
            or lp.get("facing") != up.get("facing")
            or lp.get("hinge") not in {"left", "right"}
            or lp.get("hinge") != up.get("hinge")
        ):
            result["reason"] = "door_unverified"
        elif lp.get("facing") not in allowed_facings:
            result["reason"] = "door_orientation"
        else:
            lower_open = (
                False
                if self._false(lp.get("open"))
                else True
                if lp.get("open") is True or lp.get("open") == "true"
                else None
            )
            upper_open = (
                False
                if self._false(up.get("open"))
                else True
                if up.get("open") is True or up.get("open") == "true"
                else None
            )
            if lower_open is None or upper_open is None or lower_open != upper_open:
                result["reason"] = "door_unverified"
            else:
                result.update(
                    frame_verified=True,
                    open=lower_open,
                    complete=not lower_open,
                    reason="door_not_closed" if lower_open else None,
                )
        return result

    def _scan(self, anchor, context, orientation: str = "north", *, reject_bad_floor=False):
        norm_orient = normalize_orientation(orientation)
        status = self._status(context)
        if math.dist(status["pos"], anchor) > 24:
            raise _Failure("out_of_range", "Shelter site is more than 24 blocks away")
        x, y, z = anchor
        cache = {}
        if reject_bad_floor:
            ground = self.NATURAL_GROUND | self.MATERIALS
            floor = [(x + dx, y - 1, z + dz) for dx, dz in ((0, 0), (-2, -2), (-2, 2), (2, -2), (2, 2))]
            floor.extend((cx, y - 1, cz) for cx in range(x - 2, x + 3) for cz in range(z - 2, z + 3)
                         if (cx, y - 1, cz) not in floor)
            for cell in floor:
                block = cache[cell] = self._block(list(cell), context)
                if block["name"] not in ground or not self._safe_block(block):
                    final = self._status(context)
                    if math.dist(status["pos"], final["pos"]) > 0.75:
                        raise _Failure("observation_changed", "Bot moved while the shelter site was scanned")
                    context["evidence"] = {"pos": list(anchor), "orientation": norm_orient,
                        "dimension": final["dimension"], "fresh": True, "executed": False,
                        "geometry": {"floor_verified": False}, "occupied_blueprint_cells": [],
                        "hazards": [] if self._safe_block(block) else [list(cell)], "blocked_cell": list(cell)}
                    raise _Failure("unsafe_site", "Shelter floor has a missing or unsafe support block")
        for cx in range(x - 2, x + 3):
            for cz in range(z - 2, z + 3):
                for cy in range(y - 1, y + 3):
                    cell = (cx, cy, cz)
                    if cell not in cache:
                        cache[cell] = self._block(list(cell), context)
        # The sole exit needs a loaded floor and two clear body cells outside (total = 103 block reads).
        for dy in (-1, 0, 1):
            exit_pos = tuple(transform_rel_pos((0, dy, 3), anchor, norm_orient, integer=True))
            cache[exit_pos] = self._block(list(exit_pos), context)
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
                    occupied = props.get("occupied") in (True, "true") or hp.get("occupied") in (True, "true")
                    known = all(p.get("occupied") in (True, False, "true", "false") for p in (props, hp))
                    beds.append(
                        {
                            "foot_pos": list(pos),
                            "head_pos": list(head_pos),
                            "name": block["name"],
                            "occupied": occupied if known else None,
                            "verified": True,
                        }
                    )
        return beds

    def _evidence(self, anchor, material, cache, status, orientation: str = "north"):
        norm_orient = normalize_orientation(orientation)
        x, y, z = anchor
        walls, roof, doorway = self._blueprint(anchor, orientation=norm_orient)
        wall_set = set(walls)
        floor = [(cx, y - 1, cz) for cx in range(x - 2, x + 3) for cz in range(z - 2, z + 3)]
        floor_blocks = self.NATURAL_GROUND | self.MATERIALS
        floor_ok = all(cache[p]["name"] in floor_blocks and self._safe_block(cache[p]) for p in floor)
        hazards = [list(pos) for pos, block in cache.items() if not self._safe_block(block)]
        filled = lambda pos: cache[pos]["name"] in self.STRUCTURE_BLOCKS and self._safe_block(cache[pos])
        missing = [(p, "wall" if p in wall_set else "roof") for p in walls + roof if cache[p]["name"] in self.REPLACEABLE_SPACE]
        occupied = [list(p) for p in walls + roof if cache[p]["name"] not in self.REPLACEABLE_SPACE and not filled(p)]
        door = self._door(anchor, cache, orientation=norm_orient)

        corridor = [
            tuple(transform_rel_pos(rel, anchor, norm_orient, integer=True))
            for rel in ((0, 0, 1), (0, 1, 1), (0, 0, 3), (0, 1, 3))
        ]
        exit_floor_pos = tuple(transform_rel_pos((0, -1, 3), anchor, norm_orient, integer=True))
        door_upper_pos = tuple(transform_rel_pos((0, 1, 2), anchor, norm_orient, integer=True))
        exit_floor = cache[exit_floor_pos]
        exit_ok = (
            exit_floor["name"] in floor_blocks
            and self._safe_block(exit_floor)
            and all(cache[p]["name"] in self.INTERIOR_PASSABLE and self._safe_block(cache[p]) for p in corridor)
            and (door["frame_verified"] or all(cache[p]["name"] in self.REPLACEABLE_SPACE for p in (doorway, door_upper_pos)))
        )

        candidates = []
        for cx in range(x - 1, x + 2):
            for cz in range(z - 1, z + 2):
                if all(cache[(cx, cy, cz)]["name"] in self.INTERIOR_PASSABLE for cy in (y, y + 1)):
                    candidates.append([cx + 0.5, y, cz + 0.5])

        inside_door_cell = transform_rel_pos((0, 0, 1), anchor, norm_orient, integer=True)
        inside_door_stand = [inside_door_cell[0] + 0.5, y, inside_door_cell[2] + 0.5]
        reachable = {(inside_door_cell[0], inside_door_cell[2])} if inside_door_stand in candidates else set()
        cells = {(math.floor(p[0]), math.floor(p[2])) for p in candidates}
        while True:
            expanded = reachable | {
                cell
                for cell in cells
                if any((cell[0] + dx, cell[1] + dz) in reachable for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)))
            }
            if expanded == reachable:
                break
            reachable = expanded
        candidates = [p for p in candidates if (math.floor(p[0]), math.floor(p[2])) in reachable]
        candidates.sort(key=lambda p: math.dist(status["pos"], p))

        next_gap = None
        for pos, stage in missing:
            adjacent = [
                (pos[0] + dx, pos[1] + dy, pos[2] + dz)
                for dx, dy, dz in ((0, -1, 0), (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1), (0, 1, 0))
            ]
            if any(
                p in cache and cache[p]["name"] in self.MATERIALS | self.NATURAL_GROUND and self._safe_block(cache[p])
                for p in adjacent
            ):
                next_gap = {"pos": list(pos), "item": material, "stage": stage}
                break
        if next_gap:
            target = next_gap["pos"]
            candidates = [
                p
                for p in candidates
                if math.dist(p, target) <= 3.5
                and not self._body_intersects(target, p)
                and trace_block_visibility(
                    p, target, lambda _: None, cache=cache, max_queries=0, max_distance=4.5
                )["visible"]
                is True
            ]
            candidates.sort(key=lambda p: (math.dist(p, target), math.dist(status["pos"], p)))

        current_visibility = (trace_block_visibility(
            status["pos"], next_gap["pos"], lambda _: None, cache=cache,
            max_queries=0, max_distance=4.5) if next_gap else None)
        needs_position = bool(next_gap and (
            abs(status["pos"][1] - y) > .15
            or math.dist(status["pos"], next_gap["pos"]) > 3.5
            or self._body_intersects(next_gap["pos"], status["pos"])
            or current_visibility["visible"] is not True))

        geometry = {
            "floor_verified": floor_ok,
            "walls_complete": all(filled(p) for p in walls),
            "roof_complete": all(filled(p) for p in roof),
            "door_complete": door["complete"],
            "exit_verified": exit_ok,
        }
        structure = (
            floor_ok
            and geometry["walls_complete"]
            and geometry["roof_complete"]
            and door["frame_verified"]
            and exit_ok
            and not hazards
        )
        geometry["structure_complete"] = structure
        verified = all(geometry.values()) and not hazards
        inside = (
            x - 1 + 0.36 <= status["pos"][0] <= x + 2 - 0.36
            and z - 1 + 0.36 <= status["pos"][2] <= z + 2 - 0.36
            and y - 0.05 <= status["pos"][1] <= y + 0.7
        )
        door_stand = transform_rel_pos(
            SHELTER_5X5_SPEC.anchors["doorway_outside"].rel_pos, anchor, norm_orient, integer=False
        )
        return {
            "pos": list(anchor),
            "orientation": norm_orient,
            "material": material,
            "geometry": geometry,
            "fresh": True,
            "dimension": status["dimension"],
            "geometry_complete": verified,
            "structure_complete": structure,
            "shelter_verified": verified,
            "inside": inside,
            "interior_pos": candidates[0] if candidates else None,
            "return_route_verified": False,
            "protected_from_spawns": "unknown",
            "lighting": {
                "state": "unknown",
                "torch_positions": [list(p) for p, b in cache.items() if b["name"] in {"torch", "wall_torch"}],
            },
            "beds": self._beds(anchor, cache),
            "door": door,
            "missing_blocks": len(missing),
            "occupied_blueprint_cells": occupied[:8],
            "replaceable_vegetation": [{"pos": list(p), "name": b["name"]}
                                       for p, b in cache.items() if b["name"] in self.REPLACEABLE_PLANTS][:8],
            "hazards": hazards[:8],
            "next_gap": next_gap,
            "needs_door": None
            if door["complete"]
            else {
                "pos": list(doorway),
                "allowed_facing": list(self._axis_facings(norm_orient)),
                "stand_pos": door_stand,
                "approach_pos": inside_door_stand if inside_door_stand in candidates else None,
                "reason": door["reason"],
            },
            "needs_position": needs_position,
            "visibility": current_visibility,
            "stand_candidates": candidates[:8],
            "executed": False,
        }

    def _prepare(self, args, context, *, reject_bad_floor=False):
        anchor = self._pos(args["pos"], block=True)
        orientation = self._normalize_orientation(args.get("orientation"))
        material = args.get("material")
        if "material" in args and not isinstance(material, str):
            raise _Failure("bad_args", "Shelter material must be an item name")
        if material is not None and material not in self.MATERIALS:
            raise _Failure("unsupported_item", "Shelter material must be a supported full solid building block")
        cache, status = self._scan(anchor, context, orientation=orientation, reject_bad_floor=reject_bad_floor)
        evidence = self._evidence(anchor, material, cache, status, orientation=orientation)
        context["evidence"] = evidence
        return anchor, orientation, material, cache, status, evidence

    def _plan_shelter(self, args, context):
        anchor, _, _, cache, _, evidence = self._prepare(args, context, reject_bad_floor=True)
        _, y, _ = anchor
        if not evidence["geometry"]["floor_verified"] or not evidence["geometry"]["exit_verified"] or evidence["hazards"]:
            raise _Failure("unsafe_site", "Shelter needs a flat natural-type solid floor and a clear supported exit")
        if any(block["name"] not in self.REPLACEABLE_SPACE for pos, block in cache.items() if pos[1] >= y):
            raise _Failure("site_occupied", "Planning requires clear layers or known replaceable ground cover; obstacles will not be removed")
        evidence["blueprint"] = {
            "size": [5, 3, 5],
            "wall_blocks": 30,
            "roof_blocks": 25,
            "total_building_blocks": 55,
            "orientation": evidence["orientation"],
            "order": ["lower_walls", "upper_walls", "roof_from_edges", "door"],
            "natural_ownership_verified": False,
        }
        return evidence

    def _inspect_shelter(self, args, context):
        return self._prepare(args, context)[-1]

    def _build_shelter_step(self, args, context):
        anchor, orientation, material, cache, status, evidence = self._prepare(args, context)
        if (
            not evidence["geometry"]["floor_verified"]
            or not evidence["geometry"]["exit_verified"]
            or evidence["hazards"]
            or evidence["occupied_blueprint_cells"]
        ):
            raise _Failure("unsafe_site", "Shelter site changed, is occupied, or has an unsafe exit")
        gap = evidence["next_gap"]
        if gap is None:
            if evidence["missing_blocks"]:
                raise _Failure("unsupported_placement", "No missing blueprint block has verified adjacent support")
            evidence["step_complete"] = True
            return evidence  # Door work belongs to place_door or build_shelter().
        target = gap["pos"]
        if status.get("on_ground") is not True:
            raise _Failure("unsafe_state", "Building requires a verified grounded status")
        if abs(status["pos"][1] - anchor[1]) > 0.15:
            evidence["needs_position"] = True
            raise _Failure("needs_position", "Stand on the shelter floor level before building")
        visibility = trace_block_visibility(
            status["pos"], target, lambda p: self._block(p, context), cache=cache, max_queries=16, max_distance=4.5
        )
        evidence["visibility"] = visibility
        if math.dist(status["pos"], target) > 3.5 or visibility["visible"] is not True:
            evidence["needs_position"] = True
            if not evidence["stand_candidates"]:
                raise _Failure("no_build_stance", "No observed interior stand cell can reach the next blueprint block")
            raise _Failure("needs_position", "Move to a freshly checked stand candidate before the next building block")
        before, _ = self._inventory(context)
        if before.get(material, 0) < 1:
            raise _Failure("missing_item", "No shelter building material is in inventory")

        corridor_cells = [
            transform_rel_pos(rel, anchor, orientation, integer=True)
            for rel in ((0, 0, 1), (0, 1, 1), (0, 0, 3), (0, 1, 3))
        ]

        def validate(current):
            if self._body_intersects(target, current["pos"]):
                raise _Failure("body_occupied", "The next block intersects the bot's body")
            for entity in self._entities(context):
                if entity.get("type") in {"item", "orb", "experience_orb"}:
                    continue
                if self._body_intersects(target, entity["pos"]):
                    raise _Failure("body_occupied", "The next block intersects an observed entity")
            current_target = self._block(target, context)
            if current_target["name"] not in self.REPLACEABLE_SPACE or not self._safe_block(current_target):
                raise _Failure("site_changed", "The intended blueprint cell is no longer clear or replaceable")
            adjacent = [
                [target[0] + dx, target[1] + dy, target[2] + dz]
                for dx, dy, dz in ((0, -1, 0), (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1), (0, 1, 0))
            ]
            supports = [
                p
                for p in adjacent
                if tuple(p) in cache and cache[tuple(p)]["name"] in self.MATERIALS | self.NATURAL_GROUND
            ]
            if not supports:
                raise _Failure("site_changed", "Blueprint adjacency is no longer supported")
            support = self._block(supports[0], context)
            if support["name"] not in self.MATERIALS | self.NATURAL_GROUND or not self._safe_block(support):
                raise _Failure("site_changed", "Placement support changed after scanning")
            # Recheck the reserved passage just before the single mutation.
            for passage_pos in corridor_cells:
                block = self._block(passage_pos, context)
                if block["name"] not in self.INTERIOR_PASSABLE or not self._safe_block(block):
                    raise _Failure("site_changed", "The reserved entrance passage changed")
            final = self._status(context)
            if self._body_intersects(target, final["pos"]):
                raise _Failure("body_occupied", "Bot moved into the intended block before placement")
            if math.dist(final["pos"], target) > 3.5:
                raise _Failure("needs_position", "Bot moved out of reach before placement")
            if final.get("on_ground") is not True or abs(final["pos"][1] - anchor[1]) > 0.15:
                raise _Failure("unsafe_state", "Bot is no longer standing on the verified floor level")
            sight = trace_block_visibility(
                final["pos"], target, lambda p: self._block(p, context), max_queries=16, max_distance=4.5
            )
            evidence["prewrite_visibility"] = sight
            if sight["visible"] is not True:
                raise _Failure("site_changed", "Placement line of sight changed after scanning")
            latest = self._status(context)
            if (
                latest.get("on_ground") is not True
                or math.dist(latest["pos"], final["pos"]) > 0.25
                or self._body_intersects(target, latest["pos"])
            ):
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
        evidence.update(self._evidence(anchor, material, cache, final_status, orientation=orientation))
        evidence.update(executed=True, placed_pos=target, placed=material, used=used, step_complete=True)
        return evidence

    def execute(self, name, args, cancel_event=None, **options):
        context = {
            "cancel": cancel_event,
            "acted": False,
            "read_calls": 0,
            "block_reads": 0,
            "shelter_deadline": time.monotonic() + self.READ_BUDGET_SECONDS,
        }
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
            error = self._disconnection_error(error, context)
            data.update(context.get("evidence", {}))
            data.update(getattr(error, "data", {}))
            data["executed"] = context["acted"]
            code = getattr(error, "code", "internal_error")
            if context.get("disconnected_unknown"):
                data.update(action_state="unknown", cleanup_skipped="disconnected_unknown")
                data["api_details"] = context["disconnection_details"]
            if context["acted"] and not context.get("disconnected_unknown"):
                try:
                    self.client.stop()
                    data["cleanup"] = "stop_requested"
                except Exception as cleanup_error:
                    data["cleanup_error"] = getattr(cleanup_error, "code", "internal_error")
            return SkillResult(False, str(error), data, code)
        finally:
            data.update(read_calls=context["read_calls"], block_reads=context["block_reads"])

    # ------------------------------------------------------------------
    # Unified StructureSpec + ActionWorkflow autonomous shelter pipeline
    # ------------------------------------------------------------------

    def inspect_shelter_structure(
        self,
        pos: Sequence[int],
        *,
        orientation: str = "north",
        read_block: Optional[Callable[[List[int]], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Inspect a 5x3x5 shelter via StructureSpec diff reporting."""
        norm_orient = normalize_orientation(orientation)
        reader = read_block or (lambda p: self.client.get_block(p[0], p[1], p[2]))
        return inspect_structure(SHELTER_5X5_SPEC, pos, reader, orientation=norm_orient)

    def find_shelter_sites(
        self,
        bot_pos: Optional[Sequence[float]] = None,
        *,
        radius: int = 8,
        orientations: Optional[Sequence[str]] = None,
        allow_clearing: bool = True,
        allow_floor_patch: bool = True,
        max_clear_cells: int = 24,
        max_floor_patches: int = 6,
        max_results: int = 5,
    ) -> List[Dict[str, Any]]:
        """Find candidate 5x3x5 shelter plots in loaded terrain around bot_pos."""
        if bot_pos is None:
            raw_status = self.client.get_status()
            st = raw_status.get("result") if isinstance(raw_status, dict) and "result" in raw_status else raw_status
            bot_pos = parse_pos(st.get("pos") if isinstance(st, dict) else None)
            if bot_pos is None:
                return []
        return find_shelter_sites_spec(
            bot_pos,
            lambda p: self.client.get_block(p[0], p[1], p[2]),
            radius=radius,
            orientations=orientations,
            allow_clearing=allow_clearing,
            allow_floor_patch=allow_floor_patch,
            max_clear_cells=max_clear_cells,
            max_floor_patches=max_floor_patches,
            max_results=max_results,
        )

    def build_shelter(
        self,
        origin: Optional[Sequence[int]] = None,
        *,
        material: str = "cobblestone",
        orientation: Optional[str] = None,
        door_item: Optional[str] = None,
        radius: int = 8,
        clear_obstacles: bool = True,
        patch_floor: bool = True,
        auto_door: bool = True,
        close_door: bool = True,
        finish_inside: bool = True,
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: float = 180.0,
        allow_damage: bool = False,
    ) -> SkillResult:
        """Locate (if omitted) and build a complete 5x3x5 shelter + door via ActionWorkflow."""
        if not isinstance(material, str) or material not in self.MATERIALS:
            return SkillResult(
                False,
                "Shelter material must be a supported full solid building block.",
                error_code="bad_args",
                data={"executed": False, "shelter_verified": False},
            )
        if door_item is not None and (not isinstance(door_item, str) or door_item not in self.DOORS):
            return SkillResult(
                False,
                "door_item must be a supported wooden door item.",
                error_code="bad_args",
                data={"executed": False, "shelter_verified": False},
            )

        site: Optional[Dict[str, Any]] = None
        if origin is None:
            orient_filter = [normalize_orientation(orientation)] if orientation is not None else None
            try:
                sites = self.find_shelter_sites(
                    radius=radius,
                    orientations=orient_filter,
                    allow_clearing=clear_obstacles,
                    allow_floor_patch=patch_floor,
                    max_results=3,
                )
            except Exception as exc:
                return SkillResult(
                    False,
                    f"Failed scanning nearby shelter plots: {exc}",
                    error_code=getattr(exc, "code", "network_error"),
                    data={"executed": False, "shelter_verified": False},
                )
            if not sites:
                return SkillResult(
                    False,
                    f"No suitable 5x3x5 shelter plot found within radius {radius}.",
                    error_code="no_shelter_site_found",
                    data={"executed": False, "shelter_verified": False},
                )
            site = sites[0]
            int_origin = list(site["origin"])
            norm_orient = site["orientation"]
        else:
            parsed = parse_pos(origin, integer=True)
            if parsed is None:
                return SkillResult(
                    False,
                    "Shelter origin must be three integer coordinates [x, y, z].",
                    error_code="bad_args",
                    data={"executed": False, "shelter_verified": False},
                )
            int_origin = list(parsed)
            if orientation is not None:
                norm_orient = normalize_orientation(orientation)
            else:
                # Score orientations at the requested origin so doorways prefer natural clear sides.
                try:
                    raw_status = self.client.get_status()
                    st = raw_status.get("result") if isinstance(raw_status, dict) and "result" in raw_status else raw_status
                    bpos = st.get("pos") if isinstance(st, dict) else None
                except Exception:
                    bpos = None
                ranked = []
                for cand_orient in ("north", "south", "east", "west"):
                    cand = evaluate_shelter_site(
                        int_origin,
                        cand_orient,
                        lambda p: self.client.get_block(p[0], p[1], p[2]),
                        bot_pos=bpos,
                        allow_clearing=clear_obstacles,
                        allow_floor_patch=patch_floor,
                    )
                    if cand is not None:
                        ranked.append(cand)
                if ranked:
                    ranked.sort(key=lambda s: (float(s["score"]), float(s["distance"])))
                    site = ranked[0]
                    norm_orient = site["orientation"]
                else:
                    norm_orient = "north"
            if site is None:
                site = evaluate_shelter_site(
                    int_origin,
                    norm_orient,
                    lambda p: self.client.get_block(p[0], p[1], p[2]),
                    allow_clearing=clear_obstacles,
                    allow_floor_patch=patch_floor,
                )

        if site is None:
            return SkillResult(
                False,
                f"Requested site {int_origin} ({norm_orient}) failed safety/floor/clearance validation.",
                error_code="unsafe_site",
                data={"executed": False, "shelter_verified": False, "origin": int_origin, "orientation": norm_orient},
            )

        guard = WorkflowGuard(
            self.client,
            cancel_event=cancel_event,
            timeout_seconds=timeout_seconds,
            max_reads=6000,
            allow_damage=allow_damage,
        )
        player_end = (
            list(site["interior_center"])
            if (finish_inside and auto_door)
            else list(site["doorway_outside"])
        )
        ctx = WorkflowContext(
            guard=guard,
            spec=SHELTER_5X5_SPEC,
            origin=int_origin,
            orientation=norm_orient,
            player_start=list(site["doorway_outside"]),
            player_end=player_end,
            extra={
                "material": material,
                "door_item": door_item,
                "auto_door": auto_door,
                "close_door": close_door,
                "finish_inside": finish_inside,
                "clear_obstacles": clear_obstacles,
                "patch_floor": patch_floor,
                "site": site,
                "start_tolerance": 2.2,
            },
        )
        steps = self._build_workflow_steps(auto_door=auto_door)
        return ActionWorkflowRunner().run(
            ctx,
            steps,
            precheck=self._workflow_precheck,
            verify_structure=self._verify_workflow_structure,
            ready_timeout=5.0,
        )

    def _build_workflow_steps(self, *, auto_door: bool) -> List[ActionStep]:
        move_branches = {
            "stalled": self._branch_recover_shelter_move,
            "path_not_found": self._branch_recover_shelter_move,
            "unreachable_stance": self._branch_recover_shelter_move,
            "timeout": self._branch_recover_shelter_move,
        }
        steps: List[ActionStep] = [
            ActionStep(
                step_id="prepare_shelter_site",
                description="清理选址范围内障碍物并补全底面与门外落脚点",
                stage="floor",
                action=self._wf_prepare_site,
                branches=dict(move_branches),
                max_branch_attempts=2,
            ),
            ActionStep(
                step_id="enter_interior",
                description="通过门洞进入室内 3x3 中心建造站位",
                stage="lower_walls",
                action=lambda ctx: stand_at(
                    ctx,
                    SHELTER_5X5_SPEC.anchors["interior_center"].rel_pos,
                    tolerance=0.7,
                ),
                branches=dict(move_branches),
                max_branch_attempts=2,
            ),
            ActionStep(
                step_id="build_shelter_shell",
                description="按四向结构规约建造全部墙体与屋顶（55格）",
                stage="roof",
                action=self._wf_build_shell,
                branches={
                    "target_obstructed": self._branch_clear_shell_obstacle,
                    "place_failed": self._branch_reposition_inside,
                    **move_branches,
                },
                max_branch_attempts=3,
            ),
        ]
        if auto_door:
            steps.append(
                ActionStep(
                    step_id="install_and_close_door",
                    description="安装木门并按要求进屋关门校准",
                    stage="door",
                    action=self._wf_install_door,
                    branches=dict(move_branches),
                    max_branch_attempts=2,
                )
            )
        return steps

    def _workflow_precheck(self, ctx: WorkflowContext) -> None:
        material = str(ctx.extra["material"])
        site = ctx.extra.get("site") or {}
        report = self.inspect_shelter_structure(
            ctx.origin, orientation=ctx.orientation, read_block=ctx.read_block
        )
        missing_shell = [
            m for m in report["missing"] if m["stage"] in {"lower_walls", "upper_walls", "roof"}
        ]
        bridge_needed = len(site.get("bridge_cells") or [])
        needed_blocks = len(missing_shell) + bridge_needed
        counts = ctx.inventory_counts()
        if counts.get(material, 0) < needed_blocks:
            raise WorkflowStepError(
                "missing_building_block",
                f"Need {needed_blocks} {material} (shell={len(missing_shell)}, floor={bridge_needed}), found {counts.get(material, 0)}.",
                blocker_type="missing_item",
                suggested_recovery="gather_building_material",
                data={"required": needed_blocks, "available": counts.get(material, 0)},
            )
        if ctx.extra.get("auto_door", True):
            door_cells = [m for m in report["missing"] if m["stage"] == "door"]
            if door_cells:
                wanted_door = ctx.extra.get("door_item")
                if wanted_door:
                    if counts.get(wanted_door, 0) < 1:
                        raise WorkflowStepError(
                            "missing_door",
                            f"Need wooden door item {wanted_door}.",
                            blocker_type="missing_item",
                            suggested_recovery="craft_or_equip_door",
                        )
                elif ctx.select_item(sorted(WOODEN_DOOR_BLOCKS)) is None:
                    raise WorkflowStepError(
                        "missing_door",
                        "Need at least 1 wooden door in inventory.",
                        blocker_type="missing_item",
                        suggested_recovery="craft_or_equip_door",
                    )

    def _verify_workflow_structure(self, ctx: WorkflowContext) -> Dict[str, Any]:
        stages = ("floor", "lower_walls", "upper_walls", "roof", "door") if ctx.extra.get("auto_door", True) else (
            "floor",
            "lower_walls",
            "upper_walls",
            "roof",
        )
        report = inspect_structure(
            SHELTER_5X5_SPEC,
            ctx.origin,
            ctx.read_block,
            orientation=ctx.orientation,
            stages=stages,
        )
        if ctx.extra.get("auto_door", True) and report.get("complete"):
            door_lower_rel = (0, 0, 2)
            door_upper_rel = (0, 1, 2)
            lower_pos = tuple(ctx.world_pos(door_lower_rel, integer=True))
            upper_pos = tuple(ctx.world_pos(door_upper_rel, integer=True))
            door = self._door(
                ctx.origin,
                {lower_pos: ctx.read_block(list(lower_pos)), upper_pos: ctx.read_block(list(upper_pos))},
                orientation=ctx.orientation,
            )
            report["door"] = door
            expected_closed = bool(ctx.extra.get("close_door", True))
            if not door["frame_verified"] or (expected_closed and not door["complete"]):
                report["complete"] = False
                report.setdefault("missing", []).append(
                    {
                        "stage": "door",
                        "world_pos": list(lower_pos),
                        "current": door.get("reason") or "door_unverified",
                        "expected": ["closed_paired_door" if expected_closed else "paired_door"],
                    }
                )
        return report

    def _wf_move_within_reach(
        self,
        ctx: WorkflowContext,
        target_world: Sequence[int],
        *,
        max_dist: float = 3.5,
        prefer_interior: bool = False,
    ) -> None:
        status = ctx.guard.check_status(require_safe=True)
        bpos = parse_pos(status.get("pos"))
        is_roof = int(target_world[1]) >= int(ctx.origin[1]) + 2
        tcenter_x = float(target_world[0]) + 0.5
        tcenter_z = float(target_world[2]) + 0.5
        horiz = math.hypot(bpos[0] - tcenter_x, bpos[2] - tcenter_z) if bpos is not None else 0.0
        if (
            bpos is not None
            and math.dist(bpos, target_world) <= max_dist
            and not self._body_intersects(target_world, bpos)
            and (not is_roof or not prefer_interior or horiz >= 0.85)
        ):
            return
        candidates: List[Tuple[float, float, float]] = []
        if prefer_interior:
            for dx in range(-1, 2):
                for dz in range(-1, 2):
                    wcell = ctx.world_pos((dx, 0, dz), integer=True)
                    feet_blk = str(ctx.read_block(wcell).get("name") or "air")
                    head_blk = str(ctx.read_block([wcell[0], wcell[1] + 1, wcell[2]]).get("name") or "air")
                    floor_blk = str(ctx.read_block([wcell[0], wcell[1] - 1, wcell[2]]).get("name") or "air")
                    cand_pt = (wcell[0] + 0.5, float(wcell[1]), wcell[2] + 0.5)
                    if (
                        feet_blk in AIR
                        and head_blk in AIR
                        and floor_blk in (SHELTER_GROUND_BLOCKS | self.MATERIALS)
                        and not self._body_intersects(target_world, cand_pt)
                        and math.dist(cand_pt, target_world) <= max_dist
                    ):
                        candidates.append(cand_pt)
        if not candidates:
            ext = ctx.world_pos(SHELTER_5X5_SPEC.anchors["doorway_outside"].rel_pos, integer=False)
            candidates = [(ext[0], ext[1], ext[2])]
        candidates.sort(
            key=lambda p: (
                1 if (is_roof and math.hypot(p[0] - tcenter_x, p[2] - tcenter_z) < 0.85) else 0,
                abs(math.dist(p, target_world) - 2.0),
                math.dist(p, bpos) if bpos is not None else 0.0,
            )
        )
        for cand in candidates[:4]:
            try:
                stand_at(ctx, cand, relative=False, tolerance=1.1)
                return
            except WorkflowStepError:
                continue
        stand_at(ctx, [target_world[0] + 0.5, ctx.origin[1], target_world[2] + 0.5], relative=False, tolerance=max_dist - 0.3)

    def _wf_prepare_site(self, ctx: WorkflowContext) -> Dict[str, Any]:
        site = ctx.extra.get("site") or {}
        material = str(ctx.extra["material"])
        dug = 0
        patched = 0

        if ctx.extra.get("clear_obstacles", True):
            clear_cells = list(site.get("clear_cells") or [])
            # Also clear replaceable ground cover (leaf_litter, tall/short grass, snow) in doorway/corridor/interior
            corridor_and_interior = [
                ctx.world_pos((dx, dy, dz), integer=True)
                for dy in (0, 1)
                for dx in range(-1, 2)
                for dz in range(-1, 2)
            ] + [
                ctx.world_pos((0, dy, rz), integer=True)
                for rz in (2, 3)
                for dy in (0, 1)
            ]
            seen_clear = {tuple(c) for c in clear_cells}
            for cpos in corridor_and_interior:
                ckey = tuple(cpos)
                if ckey not in seen_clear:
                    bname = str(ctx.read_block(cpos).get("name") or "air")
                    if bname not in AIR and bname in DEFAULT_REPLACEABLE:
                        clear_cells.append(cpos)
                        seen_clear.add(ckey)
            for cell in clear_cells:
                blk = ctx.read_block(cell)
                bname = str(blk.get("name") or "air")
                if bname in AIR:
                    continue
                if bname not in (SHELTER_CLEARABLE_BLOCKS | DEFAULT_REPLACEABLE):
                    raise WorkflowStepError(
                        "target_obstructed",
                        f"Unexpected non-clearable obstacle {bname} at {cell}",
                        blocker_type="obstructed",
                        blocker_pos=list(cell),
                        blocker_block=bname,
                    )
                self._wf_move_within_reach(ctx, cell, max_dist=3.8, prefer_interior=False)
                dig_block(ctx, cell, relative=False, allowed_blocks=SHELTER_CLEARABLE_BLOCKS | DEFAULT_REPLACEABLE, collect=False)
                dug += 1

        if ctx.extra.get("patch_floor", True):
            for fcell in list(site.get("bridge_cells") or []):
                blk = ctx.read_block(fcell)
                if str(blk.get("name") or "air") not in DEFAULT_REPLACEABLE:
                    continue
                self._wf_move_within_reach(ctx, fcell, max_dist=3.8, prefer_interior=False)
                place_block(ctx, material, fcell, relative=False, replaceable=DEFAULT_REPLACEABLE)
                patched += 1

        return {"dug_obstacles": dug, "patched_floor": patched}

    def _wf_build_shell(self, ctx: WorkflowContext) -> Dict[str, Any]:
        material = str(ctx.extra["material"])
        walls, roof, _ = self._blueprint(ctx.origin, orientation=ctx.orientation)
        placed_count = 0
        for target_tuple in walls + roof:
            target = list(target_tuple)
            cur = str(ctx.read_block(target).get("name") or "air")
            if cur in self.STRUCTURE_BLOCKS:
                continue
            if cur not in AIR:
                if cur in (SHELTER_CLEARABLE_BLOCKS | DEFAULT_REPLACEABLE):
                    self._wf_move_within_reach(ctx, target, max_dist=3.4, prefer_interior=True)
                    dig_block(ctx, target, relative=False, allowed_blocks=SHELTER_CLEARABLE_BLOCKS | DEFAULT_REPLACEABLE, collect=False)
                else:
                    raise WorkflowStepError(
                        "target_obstructed",
                        f"Blueprint cell {target} occupied by {cur}",
                        blocker_type="obstructed",
                        blocker_pos=target,
                        blocker_block=cur,
                    )
            self._wf_move_within_reach(ctx, target, max_dist=3.3, prefer_interior=True)
            place_block(ctx, material, target, relative=False, replaceable=DEFAULT_REPLACEABLE)
            placed_count += 1
        return {"placed_shell_blocks": placed_count}

    def _wf_install_door(self, ctx: WorkflowContext) -> Dict[str, Any]:
        door_lower = ctx.world_pos((0, 0, 2), integer=True)
        door_upper = ctx.world_pos((0, 1, 2), integer=True)
        lower_blk = ctx.read_block(door_lower)
        upper_blk = ctx.read_block(door_upper)
        door_state = self._door(
            ctx.origin,
            {tuple(door_lower): lower_blk, tuple(door_upper): upper_blk},
            orientation=ctx.orientation,
        )

        if not door_state["frame_verified"]:
            # Stand on the target finish side BEFORE placing the door because mineflayer-pathfinder
            # may treat an open/closed door block as impassable.
            place_anchor = (
                SHELTER_5X5_SPEC.anchors["interior_center"].rel_pos
                if ctx.extra.get("finish_inside", True)
                else SHELTER_5X5_SPEC.anchors["doorway_outside"].rel_pos
            )
            stand_at(ctx, place_anchor, tolerance=0.75)
            for dcell in (door_upper, door_lower):
                cur = str(ctx.read_block(dcell).get("name") or "air")
                if cur not in DEFAULT_REPLACEABLE:
                    dig_block(ctx, dcell, relative=False, allowed_blocks=SHELTER_CLEARABLE_BLOCKS | DEFAULT_REPLACEABLE | self.DOORS, collect=False)
            wanted_door = ctx.extra.get("door_item")
            door_choices = [wanted_door] if wanted_door else sorted(WOODEN_DOOR_BLOCKS)
            chosen = ctx.select_item(door_choices)
            if chosen is None:
                raise WorkflowStepError(
                    "missing_door",
                    "Missing wooden door item for doorway placement.",
                    blocker_type="missing_item",
                    suggested_recovery="craft_or_equip_door",
                )
            aim_at(ctx, door_lower, relative=False)
            ctx.guard.write(
                ctx.guard.client.place_block,
                item=chosen,
                x=door_lower[0],
                y=door_lower[1],
                z=door_lower[2],
            )
            ok = wait_until(
                ctx,
                lambda c: self._door(
                    c.origin,
                    {tuple(door_lower): c.read_block(door_lower), tuple(door_upper): c.read_block(door_upper)},
                    orientation=c.orientation,
                )["frame_verified"],
                timeout_seconds=2.0,
                poll_seconds=0.2,
                description="verify paired wooden door",
            )
            if not ok:
                raise WorkflowStepError(
                    "door_unverified",
                    f"Placed {chosen} at {door_lower}, but paired door frame did not verify.",
                    blocker_type="verification_failed",
                    blocker_pos=door_lower,
                )
            door_state = self._door(
                ctx.origin,
                {tuple(door_lower): ctx.read_block(door_lower), tuple(door_upper): ctx.read_block(door_upper)},
                orientation=ctx.orientation,
            )

        if ctx.extra.get("finish_inside", True):
            if door_state["open"] is False:
                ctx.guard.write(ctx.guard.client.interact, pos=door_lower)
                wait_until(
                    ctx,
                    lambda c: self._door(
                        c.origin,
                        {tuple(door_lower): c.read_block(door_lower), tuple(door_upper): c.read_block(door_upper)},
                        orientation=c.orientation,
                    )["open"]
                    is True,
                    timeout_seconds=1.5,
                    poll_seconds=0.2,
                )
            stand_at(ctx, SHELTER_5X5_SPEC.anchors["interior_center"].rel_pos, tolerance=0.7)
            door_state = self._door(
                ctx.origin,
                {tuple(door_lower): ctx.read_block(door_lower), tuple(door_upper): ctx.read_block(door_upper)},
                orientation=ctx.orientation,
            )

        if ctx.extra.get("close_door", True) and door_state["open"] is True:
            stand_at(
                ctx,
                SHELTER_5X5_SPEC.anchors["doorway_inside"].rel_pos
                if ctx.extra.get("finish_inside", True)
                else SHELTER_5X5_SPEC.anchors["doorway_outside"].rel_pos,
                tolerance=0.75,
            )
            ctx.guard.write(ctx.guard.client.interact, pos=door_lower)
            closed_ok = wait_until(
                ctx,
                lambda c: self._door(
                    c.origin,
                    {tuple(door_lower): c.read_block(door_lower), tuple(door_upper): c.read_block(door_upper)},
                    orientation=c.orientation,
                )["complete"],
                timeout_seconds=1.5,
                poll_seconds=0.2,
                description="verify door closed",
            )
            if not closed_ok:
                raise WorkflowStepError(
                    "door_not_closed",
                    "Door interaction did not confirm closed state.",
                    blocker_type="verification_failed",
                    blocker_pos=door_lower,
                )
        return {"door_pos": door_lower, "orientation": ctx.orientation}

    def _branch_recover_shelter_move(self, ctx: WorkflowContext, err: WorkflowStepError) -> bool:
        bpos = err.blocker_pos
        if bpos is not None:
            blk = str(ctx.read_block(bpos).get("name") or "air")
            if blk in SHELTER_CLEARABLE_BLOCKS:
                try:
                    dig_block(ctx, bpos, relative=False, allowed_blocks=SHELTER_CLEARABLE_BLOCKS, collect=False)
                    return True
                except WorkflowStepError:
                    return False
        return False

    def _branch_clear_shell_obstacle(self, ctx: WorkflowContext, err: WorkflowStepError) -> bool:
        if err.blocker_pos is not None and err.blocker_block in (SHELTER_CLEARABLE_BLOCKS | DEFAULT_REPLACEABLE):
            try:
                dig_block(ctx, err.blocker_pos, relative=False, allowed_blocks=SHELTER_CLEARABLE_BLOCKS | DEFAULT_REPLACEABLE, collect=False)
                return True
            except WorkflowStepError:
                return False
        return False

    def _branch_reposition_inside(self, ctx: WorkflowContext, err: WorkflowStepError) -> bool:
        bpos_cell = err.blocker_pos
        if bpos_cell is not None:
            time.sleep(0.25)
            cur = str(ctx.read_block(bpos_cell).get("name") or "air")
            if cur in self.STRUCTURE_BLOCKS:
                return True
        status = ctx.guard.check_status(require_safe=True)
        bot_p = parse_pos(status.get("pos")) or (0.0, 0.0, 0.0)
        candidates: List[Tuple[float, float, float]] = []
        for dx, dz in ((0, 1), (1, 0), (-1, 0), (0, -1), (1, 1), (-1, 1), (1, -1), (-1, -1), (0, 0)):
            wcell = ctx.world_pos((dx, 0, dz), integer=True)
            cand = (wcell[0] + 0.5, float(wcell[1]), wcell[2] + 0.5)
            if bpos_cell is not None:
                hdist = math.hypot(cand[0] - (bpos_cell[0] + 0.5), cand[2] - (bpos_cell[2] + 0.5))
                if not (0.85 <= hdist <= 2.6):
                    continue
            if math.hypot(cand[0] - bot_p[0], cand[2] - bot_p[2]) < 0.45:
                continue
            candidates.append(cand)
        for cand in candidates:
            try:
                stand_at(ctx, cand, relative=False, tolerance=0.75)
                return True
            except WorkflowStepError:
                continue
        try:
            stand_at(ctx, SHELTER_5X5_SPEC.anchors["interior_center"].rel_pos, tolerance=0.9)
            return True
        except WorkflowStepError:
            return False
