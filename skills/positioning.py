"""A single precise task stance over fresh, short, same-level geometry.

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

    def _position_for_task(self, args, context):
        target = self._pos(args["pos"])
        if (abs(target[0] - (math.floor(target[0]) + .5)) > 1e-6
                or abs(target[2] - (math.floor(target[2]) + .5)) > 1e-6 or target[1] != math.floor(target[1])):
            raise _Failure("bad_args", "Task position must be an integer-height feet-cell centre")
        status = self._prepare(context)
        origin = list(status["pos"])
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
