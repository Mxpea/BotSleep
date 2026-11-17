"""A single precise task stance over fresh, short geometry.

The sampled straight corridor is terrain evidence, not proof of the backend's
actual trajectory. The original API may early-return within half a block even
with range=0; only the independently observed final cell/centre can pass.
"""

import math
import time

from core.block_geometry import PASS_THROUGH
from .daily_survival import DailySurvivalSkills
from .survival import _Failure


class PositioningSkills(DailySurvivalSkills):
    ARGUMENTS = {"position_for_task": ({"pos"}, {"pos"})}
    MAX_READS = 96
    READ_SECONDS = 20
    HALF_WIDTH = .36  # Player .3, rounded position .05 and a small margin.

    @staticmethod
    def _cell(pos):
        return tuple(math.floor(value + .0001) for value in pos)

    @staticmethod
    def _check(context):
        DailySurvivalSkills._check(context)
        if not context.get("daily_verified") and time.monotonic() >= context.get("position_deadline", math.inf):
            executed = context.get("retreat_evidence", {}).get("executed")
            raise _Failure("action_unconfirmed" if executed else "position_budget_exhausted",
                           "Task positioning observation budget expired",
                           {"action_state": "unknown" if executed else "not_executed"})

    @staticmethod
    def _read_options(context):
        if "position_deadline" not in context:
            return DailySurvivalSkills._read_options(context)
        PositioningSkills._check(context)
        return {"timeout_seconds": min(5, context["position_deadline"] - time.monotonic())}

    def _count(self, context):
        self._check(context)
        if "position_reads" not in context:
            return
        if context["position_reads"] >= self.MAX_READS:
            executed = context.get("retreat_evidence", {}).get("executed")
            raise _Failure("action_unconfirmed" if executed else "position_budget_exhausted",
                           "Task positioning reached its bounded read limit",
                           {"action_state": "unknown" if executed else "not_executed"})
        context["position_reads"] += 1
        context.setdefault("retreat_evidence", {"executed": False})["read_calls"] = context["position_reads"]

    def _status(self, context):
        if "position_deadline" not in context:
            context.update(position_deadline=time.monotonic() + self.READ_SECONDS, position_reads=0)
        self._count(context)
        return super()._status(context)

    def _block(self, pos, context):
        self._count(context)
        return super()._block(pos, context)

    @classmethod
    def _columns(cls, origin, target):
        """Exact line/expanded-voxel intersection for the full swept width."""
        found = []
        for x in range(math.floor(min(origin[0], target[0]) - cls.HALF_WIDTH), math.floor(max(origin[0], target[0]) + cls.HALF_WIDTH) + 1):
            for z in range(math.floor(min(origin[2], target[2]) - cls.HALF_WIDTH), math.floor(max(origin[2], target[2]) + cls.HALF_WIDTH) + 1):
                lo, hi = 0., 1.
                for index, cell in ((0, x), (2, z)):
                    start, delta = origin[index], target[index] - origin[index]
                    lower, upper = cell - cls.HALF_WIDTH, cell + 1 + cls.HALF_WIDTH
                    if abs(delta) < 1e-10:
                        if not lower <= start <= upper:
                            lo, hi = 1., 0.
                            break
                    else:
                        a, b = sorted(((lower - start) / delta, (upper - start) / delta))
                        lo, hi = max(lo, a), min(hi, b)
                if lo <= hi + 1e-10:
                    found.append((x, z))
        return found

    def _corridor(self, origin, target, context):
        columns = self._columns(origin, target)
        y = math.floor(target[1] + .0001)
        for x, z in columns:
            for dy in (0, 1):
                body = self._block([x, y + dy, z], context)
                if body["name"] not in PASS_THROUGH or not self._safe_block(body):
                    raise _Failure("unsafe_route", "Task stance corridor contains blocked or uncertain body space",
                                   {"blocked_cell": [x, y + dy, z], "block": body["name"]})
            ground = self._block([x, y - 1, z], context)
            if ground["name"] not in self.SUPPORT or not self._safe_block(ground):
                raise _Failure("unsafe_route", "Task stance corridor lacks known solid non-leaf ground",
                               {"blocked_cell": [x, y - 1, z], "block": ground["name"]})
        return columns

    def _threats(self, origin, target, context):
        self._count(context)
        entities = self.client.get_entities(radius=12, **self._read_options(context))
        self._check(context)
        if not isinstance(entities, list) or len(entities) > 256:
            raise _Failure("invalid_response", "Positioning entity observation is invalid")
        delta = [target[i] - origin[i] for i in range(3)]
        norm = sum(value * value for value in delta)
        for entity in entities:
            if not isinstance(entity, dict):
                raise _Failure("invalid_response", "Positioning entity row is invalid")
            if entity.get("type") != "hostile":
                continue
            pos = self._pos(entity.get("pos"))
            t = max(0, min(1, sum((pos[i] - origin[i]) * delta[i] for i in range(3)) / norm)) if norm else 0
            nearest = [origin[i] + t * delta[i] for i in range(3)]
            if math.dist(pos, nearest) <= 8:
                raise _Failure("unsafe", "A hostile is too close to the task stance corridor")

    def _step_stable(self, status, before):
        self._safe_status(status)
        velocity = self._pos(status.get("velocity"))
        speed = self._number(status.get("speed"), "speed", 0, 1024)
        if ("busy" not in status or status["busy"] is not None or speed > .1
                or math.dist(velocity, [0, 0, 0]) > .1
                or any(status.get(k) for k in ("jumping", "held_controls"))
                or status.get("last_death") != before.get("last_death")
                or status["health"] < before["health"]
                or abs(status["pos"][1] - round(status["pos"][1])) > .05):
            raise _Failure("safety_changed", "A terrain step requires stable, idle, same-life footing")
        nearest = status.get("nearest_hostile")
        if nearest is not None:
            if (not isinstance(nearest, dict) or not isinstance(nearest.get("name"), str)
                    or type(nearest.get("dist")) not in (int, float)
                    or not math.isfinite(nearest["dist"]) or nearest["dist"] < 0):
                raise _Failure("invalid_response", "Terrain step hostile summary is incomplete")
            radius = 16 if nearest["name"] in {"skeleton", "stray", "bogged", "pillager"} else 8
            if nearest["dist"] <= radius:
                raise _Failure("unsafe", "A hostile is too close for a terrain step")

    def _step_geometry(self, origin, target, context, *, landing=False):
        """Observe full player width; plants never provide structural support."""
        source, destination = self._cell(origin), self._cell(target)
        support, body = set(), set()
        if landing:
            for x, z in self._columns(origin, origin):
                support.add((x, source[1] - 1, z))
                body.update((x, source[1] + dy, z) for dy in (0, 1))
        else:
            ascending = destination[1] > source[1]
            axis = 0 if source[0] != destination[0] else 2
            # Both actual source footprint and every column swept on the
            # cardinal transition need a real full-height terrace beneath.
            for x, z in self._columns(origin, target):
                coord = x if axis == 0 else z
                floor_y = destination[1] if coord == destination[axis] else source[1]
                support.add((x, floor_y - 1, z))
                # A vanilla one-block jump rises slightly above the new floor;
                # reserve its entire head envelope, not only endpoint headroom.
                ceiling = max(source[1], destination[1]) + (2 if ascending else 1)
                body.update((x, level, z) for level in range(floor_y, ceiling + 1))
            for x, z in self._columns(origin, origin):
                support.add((x, source[1] - 1, z))
                body.update((x, source[1] + dy, z) for dy in (0, 1))
        observed = {}
        for key in sorted(support | body):
            block = self._block(list(key), context)
            observed[key] = block
            allowed = self.SUPPORT - self.RETREAT_TRUNKS if key in support else PASS_THROUGH
            if (key in support and key in body or block["name"] not in allowed
                    or not self._safe_block(block)):
                raise _Failure("unsafe_route", "Terrain step lacks full solid support or complete body clearance",
                               {"blocked_cell": list(key), "block": block["name"]})
        return {"support": support, "body": body, "observed": observed}

    def _step_threats(self, origin, target, context, *, entities=None):
        if entities is None:
            self._count(context)
            entities = self.client.get_entities(radius=32, types=["hostile"], **self._read_options(context))
            self._check(context)
        if not isinstance(entities, list) or len(entities) >= 40:
            raise _Failure("invalid_response", "Terrain step needs a complete hostile scan")
        # Check the whole possible feet envelope, including the jump apex,
        # rather than assuming the backend follows a particular two-leg path.
        low = [min(origin[i], target[i]) for i in range(3)]
        high = [max(origin[i], target[i]) for i in range(3)]
        if target[1] > origin[1] + .5:
            high[1] += .3
        for entity in entities:
            if (not isinstance(entity, dict) or entity.get("type") != "hostile"
                    or not isinstance(entity.get("name"), str) or not entity["name"]
                    or type(entity.get("id")) is not int):
                raise _Failure("invalid_response", "Terrain step hostile row is incomplete")
            point = self._pos(entity.get("pos"))
            nearest = [max(low[i], min(high[i], point[i])) for i in range(3)]
            radius = 16 if entity["name"] in {"skeleton", "stray", "bogged", "pillager"} else 8
            if math.dist(point, nearest) <= radius:
                raise _Failure("unsafe", "A hostile is too close to the complete terrain step")
        return entities

    def _position_step(self, target, before, context):
        origin = list(before["pos"])
        source, destination = self._cell(origin), self._cell(target)
        if (abs(origin[1] - round(origin[1])) > .05
                or abs(destination[1] - source[1]) != 1
                or abs(destination[0] - source[0]) + abs(destination[2] - source[2]) != 1):
            raise _Failure("out_of_range", "Only one adjacent cardinal terrain step is supported")
        self._step_stable(before, before)
        evidence = context["retreat_evidence"]
        evidence.update(origin=origin, target=target, actual_path_verified=False,
                        terrain_step={"height_change": destination[1] - source[1], "cardinal": True},
                        read_budget=self.MAX_READS, observation_budget_seconds=self.READ_SECONDS)
        self._step_threats(origin, target, context)
        self._step_geometry(origin, target, context)

        def same_start(current):
            self._step_stable(current, before)
            if self._cell(current["pos"]) != source or math.dist(current["pos"], origin) > .1:
                raise _Failure("safety_changed", "Terrain step origin changed before movement")

        def validate(current):
            same_start(current)
            proof = self._step_geometry(current["pos"], target, context)
            threats = self._step_threats(current["pos"], target, context)
            final = self._status(context)
            same_start(final)
            if not set(self._columns(final["pos"], target)).issubset(self._columns(current["pos"], target)):
                raise _Failure("safety_changed", "Final player footprint left the checked terrain step")
            if not set(self._columns(final["pos"], final["pos"])).issubset(self._columns(current["pos"], current["pos"])):
                raise _Failure("safety_changed", "Final source footprint changed during terrain inspection")
            self._step_threats(final["pos"], target, context, entities=threats)
            evidence["step_geometry_cells"] = len(proof["observed"])

        context["navigation_write"] = True
        context["navigation_observe_only"] = True
        self._daily_action(context, self.client.move_to, *target, range=0, timeout_ms=8000, validate=validate)
        # Everything after the single write is observation. A missing or
        # interrupted landing never authorizes stop, replay, or a second move.
        deadline = min(context["position_deadline"], time.monotonic() + 1.5)
        context["position_deadline"] = deadline
        previous = None
        try:
            while time.monotonic() < deadline:
                current = self._status(context)
                if (current.get("last_death") != before.get("last_death") or current["health"] < before["health"]
                        or any(current.get(k) for k in ("in_water", "in_lava", "on_fire", "submerged", "held_controls"))):
                    raise _Failure("action_unconfirmed", "Terrain step was interrupted before landing")
                if current.get("on_ground") is True:
                    self._step_stable(current, before)
                    if self._cell(current["pos"]) != destination or math.dist(current["pos"], target) > .3:
                        raise _Failure("action_unconfirmed", "Terrain step did not reach its checked landing")
                    self._step_geometry(current["pos"], current["pos"], context, landing=True)
                    if previous is not None and math.dist(current["pos"], previous["pos"]) <= .05:
                        threats = self._step_threats(current["pos"], current["pos"], context)
                        final = self._status(context)
                        self._step_stable(final, before)
                        if (time.monotonic() > deadline or self._cell(final["pos"]) != destination
                                or math.dist(final["pos"], target) > .3
                                or math.dist(final["pos"], current["pos"]) > .05
                                or not set(self._columns(final["pos"], final["pos"])).issubset(
                                    self._columns(current["pos"], current["pos"]))):
                            raise _Failure("action_unconfirmed", "Terrain landing changed during its final observation")
                        self._step_threats(final["pos"], final["pos"], context, entities=threats)
                        context["navigation_verified"] = True
                        return self._verified(context, pos=final["pos"], moved=math.dist(origin, final["pos"]),
                                              already_positioned=False, landing_stable=True)
                    previous = current
                else:
                    previous = None
                delay = min(.1, max(0, deadline - time.monotonic()))
                if context.get("cancel") is not None:
                    context["cancel"].wait(delay)
                else:
                    time.sleep(delay)
                self._check(context)
            raise _Failure("action_unconfirmed", "Terrain step landing did not settle in time")
        except Exception as error:
            raise _Failure("action_unconfirmed", "Terrain step landing could not be fully verified",
                           {"action_state": "unknown", "recovery_error": getattr(error, "code", "internal_error")}) from None

    def _position_for_task(self, args, context):
        target = self._pos(args["pos"])
        if (abs(target[0] - (math.floor(target[0]) + .5)) > 1e-6
                or abs(target[2] - (math.floor(target[2]) + .5)) > 1e-6 or target[1] != math.floor(target[1])):
            raise _Failure("bad_args", "Task position must be an integer-height feet-cell centre")
        status = self._prepare(context)
        origin = list(status["pos"])
        if abs(origin[1] - target[1]) > .05:
            return self._position_step(target, status, context)
        if abs(origin[1] - target[1]) > .05 or math.dist(origin, target) > 3:
            raise _Failure("out_of_range", "Task positioning is restricted to the same level within three blocks")
        evidence = context["retreat_evidence"]
        evidence.update(origin=origin, target=target, actual_path_verified=False,
                        read_budget=self.MAX_READS, observation_budget_seconds=self.READ_SECONDS)
        self._threats(origin, target, context)
        self._corridor(origin, target, context)
        if self._cell(origin) == self._cell(target) and math.dist(origin, target) <= .4:
            return self._verified(context, pos=origin, already_positioned=True, moved=False)

        def same_start(current):
            self._safe_status(current)
            if (self._cell(current["pos"]) != self._cell(origin) or abs(current["pos"][1] - origin[1]) > .05
                    or math.dist(current["pos"], origin) > .2):
                raise _Failure("safety_changed", "Task stance origin changed while its corridor was checked")

        def validate(current):
            same_start(current)
            self._threats(current["pos"], target, context)
            columns = self._corridor(current["pos"], target, context)
            final = self._status(context)
            same_start(final)
            if not set(self._columns(final["pos"], target)).issubset(columns):
                raise _Failure("safety_changed", "The final player width extends outside the checked corridor")

        self._daily_action(context, self.client.move_to, *target, range=0, timeout_ms=8000, validate=validate)
        after = self._prepare(context)
        if self._cell(after["pos"]) != self._cell(target) or math.dist(after["pos"], target) > .4:
            raise _Failure("position_unverified", "Backend movement did not reach the requested task cell and centre",
                           {"pos": after["pos"], "centre_distance": math.dist(after["pos"], target)})
        self._corridor(after["pos"], after["pos"], context)
        return self._verified(context, pos=after["pos"], moved=math.dist(origin, after["pos"]), already_positioned=False)
