"""Short, reversible starter mine for collecting the first cobblestone.

Surveying proposes a straight descending staircase from the current feet cell.
Each later invocation reads the world again and performs at most one dig or
one cardinal move. Inventory changes, including pickup while descending, are
measured separately from making a passage.
"""

import math
import time

from core.block_geometry import PASS_THROUGH, trace_block_visibility
from core.client import APIError
from core.structure_spec import MINE_HAZARD_BLOCKS, SHELTER_PROTECTED_BLOCKS
from .positioning import PositioningSkills
from .survival import _Failure


class StarterStaircaseSkills(PositioningSkills):
    ARGUMENTS = {
        "plan_starter_staircase": ({"protected_pos", "excluded_directions"}, set()),
        "step_starter_staircase": ({"entry", "direction", "returning", "protected_pos", "from_cell"}, {"entry", "direction"}),
    }
    DIRECTIONS = {"north": (0, -1), "east": (1, 0), "south": (0, 1), "west": (-1, 0)}
    MAX_DEPTH = 8
    MAX_READS = 480
    NATURAL = frozenset({"grass_block", "dirt", "coarse_dirt", "rooted_dirt", "moss_block",
                         "stone", "andesite", "diorite", "granite", "deepslate"})
    FALLING = frozenset({"sand", "red_sand", "gravel", "suspicious_sand", "suspicious_gravel",
                         "anvil", "chipped_anvil", "damaged_anvil", "dragon_egg", "pointed_dripstone"})

    def execute(self, name, args, cancel_event=None, *, recovery_context=None, cautious=False):
        result = super().execute(name, args, cancel_event, recovery_context=recovery_context, cautious=cautious)
        if not result.success and result.data.get("executed") is True:
            # Even an unusual read failure or changed footing after sending
            # the action is distinct from a known, pre-write rejected site.
            result.data.update(action_state="unknown", verification_error=result.error_code)
            result.error_code = "action_unconfirmed"
        return result

    @staticmethod
    def _blocked(message, pos=None, name=None):
        raise _Failure("staircase_blocked", message, {"blocked_cell": list(pos) if pos else None, "block": name})

    def _reader(self, context):
        cache = {}

        def read(pos):
            key = tuple(pos)
            if key not in cache:
                row = self._block(list(key), context)
                if row["name"] in {"unknown", "not_loaded", "void_air"}:
                    self._blocked("Staircase geometry is incomplete", key, row["name"])
                cache[key] = row
            return cache[key]

        return read

    def _pick(self, context):
        counts, items = self._inventory(context)
        for item in items:
            if item["count"] > 0 and item["name"] in self.PICKAXES:
                durability = item.get("durability")
                if durability is None or (isinstance(durability, (list, tuple)) and durability
                                          and type(durability[0]) is int and durability[0] > 0):
                    return counts
        raise _Failure("missing_tool", "The starter staircase needs a usable pickaxe")

    def _same_stance(self, status, initial):
        self._step_stable(status, initial)
        near_edge = len(self._columns(initial["pos"], initial["pos"])) > 1
        if (self._cell(status["pos"]) != self._cell(initial["pos"])
                or math.dist(status["pos"], initial["pos"]) > (.0001 if near_edge else .1)):
            raise _Failure("safety_changed", "The staircase stance changed during inspection")

    def _step_stable(self, status, before, *, allow_motion=False):
        pending = super()._step_stable(status, before, allow_motion=allow_motion)
        if (not pending and len(self._columns(status["pos"], status["pos"])) > 1
                and (status["speed"] != 0 or any(status["velocity"]))):
            if not allow_motion:
                raise _Failure("safety_changed", "The stair wall contact is still moving")
            pending.append("stair_contact_motion")
        return pending

    def _home(self, args):
        pos = args.get("protected_pos")
        return self._pos(pos, block=True) if pos is not None else None

    def _guard_home(self, cell, home):
        if home and max(abs(cell[0] - home[0]), abs(cell[2] - home[2])) <= 3:
            self._blocked("This column belongs to the shelter or its exit", cell)

    def _hazard(self, row):
        return (not self._safe_block(row) or row["name"] in MINE_HAZARD_BLOCKS | self.FALLING
                or row["name"].endswith("_concrete_powder"))

    @classmethod
    def _at_wall_face(cls, pos, column, target=None):
        # The server rounds x/z to 0.1. A stationary grounded body can touch a
        # dry full cube at the reported .3 half-width. Positive nominal overlap
        # on both axes is never clearance, including at a diagonal corner.
        for axis, cell in zip((0, 2), column):
            gap = max(cell - pos[axis], pos[axis] - cell - 1)
            if .3 - 1e-9 <= gap <= cls.HALF_WIDTH + 1e-9:
                if target is None:
                    return True
                # A new corridor may only *leave* this face, never move into it.
                if (pos[axis] <= cell and target[axis] < pos[axis] - 1e-9
                        or pos[axis] >= cell + 1 and target[axis] > pos[axis] + 1e-9):
                    return True
        return False

    def _in_step_landing(self, pos, target):
        # The mine checks the entire observed stance and repeatedly confirms
        # the rounded position. At least .45 of each horizontal body dimension
        # must still rest on the centre tread across rounding uncertainty.
        cell = self._cell(pos)
        return (cell == self._cell(target) and abs(target[1] - pos[1]) <= .05
                and min(self.contact_overlap(pos, cell)) >= .45 - 1e-9)

    def _mine_stance(self, pos, read, context):
        source = self._cell(pos)
        x, y, z = source
        columns = self._columns(pos, pos)
        direction = context.get("mine_direction")
        lower_tread = None
        if len(columns) > 1 and min(self.contact_overlap(pos, source)) < .45 - 1e-9:
            self._blocked("Too little of the player's body rests on the centre stair", source)
        for cx, cz in columns:
            floor = (cx, y - 1, cz)
            ground = read(floor)
            if ground["name"] not in self.NATURAL or self._hazard(ground):
                # Only the next cell of this exact staircase can already be
                # excavated one block below the stance. No diagonal, open pit,
                # arbitrary ledge or other unsupported floor is accepted.
                if (not direction or (cx - x, cz - z) != self.DIRECTIONS[direction]
                        or ground["name"] not in self.AIR or self._hazard(ground)):
                    self._blocked("The mine entrance and stairs require solid natural ground", floor, ground["name"])
                lower = (cx, y - 2, cz)
                tread = read(lower)
                if tread["name"] not in self.NATURAL or self._hazard(tread):
                    self._blocked("The lower stair lip has no checked dry tread", lower, tread["name"])
                context["retreat_evidence"]["lower_tread"] = list(lower)
                lower_tread = lower
            for by in range(y, y + 3):
                cell = (cx, by, cz)
                block = read(cell)
                if block["name"] in PASS_THROUGH and not self._hazard(block):
                    continue
                if (direction and self._at_wall_face(pos, (cx, cz))
                        and block["name"] in self.NATURAL and not self._hazard(block)):
                    context["retreat_evidence"]["rounded_wall_contact"] = True
                    continue
                self._blocked("The staircase must retain enough clearance to climb back", cell, block["name"])
        return lower_tread

    def _step_geometry(self, origin, target, context, *, landing=False):
        if "mine_direction" not in context:
            return super()._step_geometry(origin, target, context, landing=landing)
        read = self._reader(context)
        lower_tread = self._mine_stance(origin, read, context)
        source, dest = self._cell(origin), self._cell(target)
        start_columns = self._columns(origin, origin)
        observed = {}
        if not landing:
            axis = 0 if source[0] != dest[0] else 2
            ceiling = max(source[1], dest[1]) + (2 if dest[1] > source[1] else 1)
            for x, z in self._columns(origin, target):
                level = dest[1] if (x, z)[axis // 2] == dest[axis] else source[1]
                floor = (x, level - 1, z)
                ground = observed[floor] = read(floor)
                if ground["name"] not in self.NATURAL or self._hazard(ground):
                    # The checked lower lip may already be under the trailing
                    # edge on an ascent. The corridor must be leaving that cell.
                    if ((x, level - 2, z) != lower_tread
                            or (x, z) not in start_columns or ground["name"] not in self.AIR
                            or self._hazard(ground) or (x, z) in self._columns(target, target)):
                        self._blocked("The stair transition has no verified tread", floor, ground["name"])
                for by in range(level, ceiling + 1):
                    cell = (x, by, z)
                    block = observed[cell] = read(cell)
                    if block["name"] in PASS_THROUGH and not self._hazard(block):
                        continue
                    if ((x, z) in start_columns and self._at_wall_face(origin, (x, z), target)
                            and block["name"] in self.NATURAL and not self._hazard(block)):
                        continue
                    self._blocked("A block obstructs the stair transition", cell, block["name"])
        return {"observed": observed, "rounded_origin": len(start_columns) > 1}

    def _rung(self, entry, direction, depth, home, read):
        dx, dz = self.DIRECTIONS[direction]
        x, y, z = entry[0] + dx * depth, entry[1] - depth, entry[2] + dz * depth
        self._guard_home((x, y, z), home)
        floor = (x, y - 1, z)
        if read(floor)["name"] not in self.NATURAL or self._hazard(read(floor)):
            self._blocked("The next stair has no solid natural landing", floor, read(floor)["name"])
        solid = []
        # A cardinal descent needs an extra head cell; its reverse jump needs
        # one more. Carve from the top so each next face can actually be seen.
        for by in range(y + 3, y - 1, -1):
            p = (x, by, z)
            b = read(p)
            if self._hazard(b) or b["name"] not in PASS_THROUGH | self.NATURAL:
                self._blocked("The next stair intersects a hazard or a protected block", p, b["name"])
            if b["name"] not in PASS_THROUGH:
                solid.append((p, b["name"]))
        return [x + .5, y, z + .5], solid

    def _dig_neighbors(self, target, read):
        for dx, dy, dz in ((0, 1, 0), (0, -1, 0), (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1)):
            p = (target[0] + dx, target[1] + dy, target[2] + dz)
            neighbor = read(p)
            name = neighbor["name"]
            if (self._hazard(neighbor) or name in SHELTER_PROTECTED_BLOCKS
                    or name.endswith(("_bed", "_door", "_planks"))):
                self._blocked("The dig could disturb a hazard, furniture or a building", p, name)

    def _dig_site(self, origin, target, expected, home, read):
        self._guard_home(target, home)
        if ((target[0], target[2]) in self._columns(origin, origin)
                and not self._at_wall_face(origin, (target[0], target[2]))):
            self._blocked("Cannot dig in the player's own footprint", target)
        if math.dist(origin, target) > 4:
            self._blocked("The dig would require unchecked backend navigation", target)
        block = read(target)
        if (block["name"] != expected or block.get("diggable") is False or block.get("can_see") is False
                or block.get("can_dig", block.get("harvest_tool_ok")) is False):
            self._blocked("The target changed or cannot be reached", target, block["name"])
        self._dig_neighbors(target, read)
        if trace_block_visibility(origin, list(target), read, max_queries=16, max_distance=4.5)["visible"] is not True:
            self._blocked("No clear sight line to the next stair face", target, expected)

    def _plan_starter_staircase(self, args, context):
        initial = self._prepare(context)
        self._step_stable(initial, initial)
        self._pick(context)
        read = self._reader(context)
        entry = list(self._cell(initial["pos"]))
        home = self._home(args)
        try:
            self._mine_stance(initial["pos"], read, context)
            self._guard_home(entry, home)
        except _Failure as err:
            if err.code != "staircase_blocked":
                raise
            raise _Failure("no_staircase_site", str(err), {**err.data, "entry": entry, "executed": False}) from None
        excluded = args.get("excluded_directions", [])
        if (not isinstance(excluded, list) or len(excluded) > 4
                or any(not isinstance(d, str) or d not in self.DIRECTIONS for d in excluded)):
            raise _Failure("bad_args", "Invalid excluded staircase directions")
        self._step_threats(initial["pos"], initial["pos"], context)
        candidates = []
        for direction in self.DIRECTIONS:
            if direction in excluded:
                continue
            stones = digs = 0
            try:
                for depth in range(1, self.MAX_DEPTH + 1):
                    target, solids = self._rung(entry, direction, depth, home, read)
                    if depth == 1:
                        if (math.floor(target[0]), math.floor(target[2])) in self._columns(initial["pos"], initial["pos"]):
                            self._blocked("Landing overlaps the source footprint")
                        if solids:
                            self._dig_site(initial["pos"], *solids[0], home, read)
                    stones += sum(name == "stone" for _, name in solids)
                    digs += len(solids)
                    if stones >= 3:
                        candidates.append((digs, depth, direction, stones))
                        break
            except _Failure as err:
                if err.code != "staircase_blocked":
                    raise
            except APIError as err:
                if err.code != "not_loaded":
                    raise
        for digs, depth, direction, stones in sorted(candidates):
            try:
                for rung in range(1, depth + 1):
                    _, solids = self._rung(entry, direction, rung, home, read)
                    for pos, _ in solids:
                        self._dig_neighbors(pos, read)
            except (_Failure, APIError) as err:
                if err.code not in {"staircase_blocked", "not_loaded"}:
                    raise
                continue
            self._same_stance(self._prepare(context), initial)
            return self._verified(context, operation="survey", entry=entry, direction=direction,
                                  site={"pos": entry, "orientation": direction}, depth=0, at_entry=True,
                                  estimated_digs=digs, surveyed_depth=depth, surveyed_stone=stones, gained_cobblestone=0)
        raise _Failure("no_staircase_site", "No checked staircase from this stance reaches three stone blocks",
                       {"entry": entry, "executed": False})

    def _step_starter_staircase(self, args, context):
        entry, direction = self._pos(args["entry"], block=True), args["direction"]
        returning = args.get("returning", False)
        if not isinstance(direction, str) or direction not in self.DIRECTIONS or type(returning) is not bool:
            raise _Failure("bad_args", "Invalid staircase direction or return flag")
        home = self._home(args)
        initial = self._prepare(context)
        self._step_stable(initial, initial)
        read = self._reader(context)
        x, y, z = self._cell(initial["pos"])
        if "from_cell" in args and tuple(self._pos(args["from_cell"], block=True)) != (x, y, z):
            self._blocked("The bot left the independently observed starting rung", (x, y, z))
        depth = entry[1] - y
        dx, dz = self.DIRECTIONS[direction]
        if not 0 <= depth <= self.MAX_DEPTH or (x, z) != (entry[0] + depth * dx, entry[2] + depth * dz):
            self._blocked("The bot is no longer on the saved staircase", (x, y, z))
        context["mine_direction"] = direction
        self._mine_stance(initial["pos"], read, context)
        if not returning:
            self._guard_home((x, y, z), home)
        context["retreat_evidence"].update(entry=entry, direction=direction, depth=depth, at_entry=depth == 0,
                                           gained_cobblestone=0)
        if returning and depth == 0:
            return self._verified(context, operation="at_entry")
        if not returning and depth == self.MAX_DEPTH:
            self._blocked("The starter staircase reached its depth limit", (x, y, z))
        before = self._inventory(context)[0] if returning else self._pick(context)
        next_depth = depth - 1 if returning else depth + 1
        if returning:
            target = [entry[0] + dx * next_depth + .5, entry[1] - next_depth, entry[2] + dz * next_depth + .5]
            solids = []
        else:
            target, solids = self._rung(entry, direction, next_depth, home, read)
        self._step_threats(initial["pos"], target, context)
        if solids:
            pos, name = solids[0]
            self._dig_site(initial["pos"], pos, name, home, read)
            evidence = context["retreat_evidence"]
            evidence.update(operation="dig", dug_pos=list(pos), expected_name=name)

            def validate(current):
                self._same_stance(current, initial)
                fresh = self._reader(context)
                self._mine_stance(current["pos"], fresh, context)
                _, current_solids = self._rung(entry, direction, next_depth, home, fresh)
                if not current_solids or current_solids[0] != (pos, name):
                    self._blocked("The next stair face changed before digging", pos)
                self._dig_site(current["pos"], pos, name, home, fresh)
                self._step_threats(current["pos"], target, context)
                self._same_stance(self._prepare(context), initial)

            # Disable collection walks by the dig backend. Progress to the
            # landing itself picks up drops without leaving the verified stairs.
            context["navigation_observe_only"] = True
            try:
                response = self._daily_action(context, self.client.dig, list(pos), collect=False, validate=validate)
                if response.get("dug") != name:
                    raise _Failure("action_unconfirmed", "The server did not acknowledge the expected stair material")
            except APIError as err:
                if err.code != "dig_unconfirmed":
                    raise
                evidence["backend_error"] = err.code
            return self._confirm_cut(initial, pos, name, before, context)

        context["retreat_evidence"]["operation"] = "ascend" if returning else "descend"
        self._position_step(target, initial, context)
        after, _ = self._inventory(context)
        return self._verified(context, depth=next_depth, at_entry=next_depth == 0,
                              gained_cobblestone=max(0, after.get("cobblestone", 0) - before.get("cobblestone", 0)))

    def _confirm_cut(self, initial, pos, name, before, context):
        evidence = context["retreat_evidence"]
        uncertain = evidence.get("backend_error") == "dig_unconfirmed"
        expected_drop = {"stone": "cobblestone", "grass_block": "dirt"}.get(name, name)
        for attempt in range(3 if uncertain else 1):
            current = self._prepare(context)
            self._same_stance(current, initial)
            read = self._reader(context)
            self._mine_stance(current["pos"], read, context)
            block = read(pos)
            after, _ = self._inventory(context)
            gain = after.get(expected_drop, 0) - before.get(expected_drop, 0)
            if block["name"] in self.AIR and (not uncertain or gain > 0):
                return self._verified(context, block_removed=True, observed_drop_gain=max(0, gain),
                    gained_cobblestone=max(0, after.get("cobblestone", 0) - before.get("cobblestone", 0)))
            if uncertain and attempt < 2:
                if context["cancel"] is None:
                    time.sleep(.15)
                else:
                    context["cancel"].wait(.15)
                self._check(context)
        raise _Failure("action_unconfirmed", "The staircase dig could not be confirmed by fresh world evidence",
                       {"action_state": "unknown", "remaining": block["name"]})
