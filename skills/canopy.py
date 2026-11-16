"""Harness-only, single-step canopy descent with fresh landing evidence."""

import math
import time
from typing import Any, Dict

from core.canopy_geometry import plan_canopy_step
from .survival import SurvivalSkills, _Failure


class CanopyRecoverySkill(SurvivalSkills):
    """Cut at most one adjacent leaf, then execute one checked movement."""

    ARGUMENTS = {
        "descend_canopy": ({"pos", "clear_pos", "expected_name"},
                           {"pos", "clear_pos", "expected_name"}),
    }

    @staticmethod
    def _check(context):
        if context["cancel"] is not None and context["cancel"].is_set():
            evidence = context.get("retreat_evidence", {})
            if evidence.get("executed"):
                evidence.update(action_state="unknown", retreat_complete=False, landing_reason="cancelled",
                                landing_termination="interrupted")
                code = "action_unconfirmed" if evidence.get("move_sent") else "dig_unconfirmed"
                raise _Failure(code, "Canopy action cancelled after a write", evidence)
            raise _Failure("cancelled", "Canopy action cancelled")

    def _status(self, context):
        status = super()._status(context)
        self._overworld(status)
        food = self._number(status.get("food"), "food", 0, 20)
        health = status["health"]
        previous = context.setdefault("canopy_health", health)
        life = (status.get("dimension"), status.get("last_death"))
        if life != context.setdefault("canopy_life", life) or health < previous:
            raise _Failure("unsafe_state", "Life or health changed during canopy recovery")
        context["canopy_health"] = health
        if (health < 12 or food < 8 or status.get("gamemode") != "survival"
                or status.get("thundering") is not False
                or any(status.get(key) for key in ("in_water", "in_lava", "on_fire", "submerged"))
                or not context.get("canopy_landing") and status.get("on_ground") is not True):
            raise _Failure("unsafe_state", "Canopy recovery requires healthy, dry, stable survival state")
        nearest = status.get("nearest_hostile")
        if nearest is not None:
            if not isinstance(nearest, dict):
                raise _Failure("invalid_response", "Nearest hostile evidence is malformed")
            distance = self._number(nearest.get("dist"), "hostile distance", 0, 1000000)
            if distance <= 12:
                raise _Failure("unsafe", "A hostile is inside the canopy safety buffer")
        context.setdefault("canopy_origin", list(status["pos"]))
        return status

    def _observe(self, context, *, at_origin=False):
        self._check(context)
        entities = self.client.get_entities(radius=16, types=["hostile"],
                                            **self._read_options(context))
        self._check(context)
        if not isinstance(entities, list):
            raise _Failure("invalid_response", "Hostile query must return a list")
        status = self._status(context)
        if at_origin and math.dist(status["pos"], context["canopy_origin"]) > 1e-4:
            raise _Failure("unsafe_state", "Bot moved away from the original canopy position")
        for entity in entities:
            if not isinstance(entity, dict):
                raise _Failure("invalid_response", "Hostile query contains malformed evidence")
            if entity.get("type") not in (None, "hostile"):
                continue
            pos = entity.get("pos")
            if pos is not None:
                distance = math.dist(status["pos"], self._pos(pos))
            else:
                distance = self._number(entity.get("dist"), "hostile distance", 0, 1000000)
            if distance <= 12:
                raise _Failure("unsafe", "A fresh entity observation found a nearby hostile")
            if at_origin and context.get("canopy_target") is not None:
                if pos is None:
                    raise _Failure("invalid_response", "Hostile position is required to check target safety")
                if math.dist(self._pos(pos), context["canopy_target"]) <= 12:
                    raise _Failure("unsafe", "A hostile is inside the target landing safety buffer")
        return status

    def _read_canopy(self, key, context):
        try:
            return self._block(tuple(key), context)
        except Exception as error:
            if getattr(error, "code", None) == "not_loaded":
                return None
            raise

    def _plan(self, status, args, context, *, cleared=False):
        plan = plan_canopy_step(status, lambda key: self._read_canopy(key, context),
                                target=args["pos"])
        if plan is None:
            raise _Failure("no_plan", "No freshly verified canopy step matches this target")
        clear = None if cleared else args["clear_pos"]
        expected = None if cleared else args["expected_name"]
        if (list(plan["pos"]) != args["pos"]
                or plan["clear_pos"] != clear or plan["expected_name"] != expected
                or list(plan["origin"]) != context["canopy_origin"]):
            raise _Failure("target_changed", "Canopy plan no longer matches all three arguments")
        return plan

    def _terrain(self, plan, context, *, cleared=False):
        """Independently verify the one-step column, support and adjacent hazards."""
        origin = context["canopy_origin"]
        source = tuple(math.floor(value + 1e-4) for value in origin)
        target = self._pos(plan["pos"])
        cell = tuple(math.floor(value + 1e-4) for value in target)
        dx, dz = cell[0] - source[0], cell[2] - source[2]
        drop = source[1] - cell[1]
        if (abs(dx) + abs(dz) != 1 or drop not in (0, 1, 2) or plan["drop"] != drop
                or target != [cell[0] + .5, cell[1], cell[2] + .5]):
            raise _Failure("unsafe", "Canopy movement must be one centered adjacent step down at most two blocks")
        clear = plan["clear_pos"]
        if clear is not None and (clear != [cell[0], source[1] - 1, cell[2]]
                                  or drop == 0 or plan["expected_name"] not in self.RETREAT_LEAVES):
            raise _Failure("unsafe", "Only one adjacent leaf below the original feet may be cleared")
        support = self._read_canopy((cell[0], cell[1] - 1, cell[2]), context)
        if (support is None or not self._safe_block(support)
                or not (support["name"] in self.SUPPORT
                        or support["name"] in self.RETREAT_LEAVES
                        and (support.get("properties") or {}).get("waterlogged") in (False, "false"))):
            raise _Failure("unsafe", "Canopy target has no verified dry landing support")
        if not context.get("canopy_landing"):
            floor = self._read_canopy((source[0], source[1] - 1, source[2]), context)
            if (floor is None or floor["name"] not in self.RETREAT_LEAVES
                    or not self._safe_block(floor)
                    or (floor.get("properties") or {}).get("waterlogged") not in (False, "false")):
                raise _Failure("unsafe", "Original canopy support is no longer a dry leaf")
            for level in (source[1], source[1] + 1):
                body = self._read_canopy((source[0], level, source[2]), context)
                if body is None or not self._safe_block(body) or body["name"] not in self.RETREAT_PASSABLE:
                    raise _Failure("unsafe", "Original canopy body space changed")
        # Liquids beside the landing support can flow into the landing column.
        for level in range(cell[1] - 1, source[1] + 2):
            for ox, oz in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
                key = (cell[0] + ox, level, cell[2] + oz)
                block = self._read_canopy(key, context)
                if block is None or not self._safe_block(block):
                    raise _Failure("unsafe", "Canopy corridor has unknown terrain or adjacent hazards")
                if level >= cell[1] and ox == oz == 0 and block["name"] not in self.RETREAT_PASSABLE:
                    if (cleared or clear != list(key) or block["name"] != plan["expected_name"]
                            or block.get("diggable") is not True or block.get("can_see") is False
                            or (block.get("properties") or {}).get("waterlogged") not in (False, "false")):
                        raise _Failure("unsafe", "Canopy body corridor is obstructed")
        return support["name"]

    @staticmethod
    def _landed_at_target(point, target):
        feet = lambda pos: tuple(math.floor(value + 1e-4) for value in pos)
        return (feet(point) == feet(target) and abs(point[1] - target[1]) <= .05 + 1e-9
                and all(abs(point[axis] - target[axis]) <= .125 + 1e-9 for axis in (0, 2)))

    def _prepare_write(self, args, context, support, *, cleared=False):
        status = self._observe(context, at_origin=True)
        plan = self._plan(status, args, context, cleared=cleared)
        if self._terrain(plan, context, cleared=cleared) != support:
            raise _Failure("target_changed", "Landing support changed before a canopy write")
        # Geometry reads may take time: lifecycle, health and origin must still
        # match immediately before dispatch, including after those reads.
        status = self._status(context)
        if math.dist(status["pos"], context["canopy_origin"]) > 1e-4:
            raise _Failure("unsafe_state", "Bot moved while rechecking canopy terrain")
        self._check(context)
        return plan

    def _descend_canopy(self, args: Dict[str, Any], context: Dict[str, Any]) -> dict:
        args = dict(args)
        args["pos"] = self._pos(args["pos"])
        context["canopy_target"] = list(args["pos"])
        if args["clear_pos"] is not None:
            args["clear_pos"] = self._pos(args["clear_pos"], block=True)
            args["expected_name"] = self._item(args["expected_name"])
        elif args["expected_name"] is not None:
            raise _Failure("bad_args", "A pure move must have null clear_pos and expected_name")
        evidence = {"executed": False, "move_sent": False, "moved": 0,
                    "block_removed": False, "target": args["pos"], "retreat_complete": False,
                    "landing_reads": 0, "landing_reason": "not_started"}
        context["retreat_evidence"] = evidence
        try:
            status = self._observe(context, at_origin=True)
            plan = self._plan(status, args, context)
            support = self._terrain(plan, context)
            evidence.update(origin=list(context["canopy_origin"]), support=support,
                            route_end=plan["route_end"], block_reads=plan["block_reads"])
            if args["clear_pos"] is not None:
                plan = self._prepare_write(args, context, support)
                context["acted"] = True
                evidence.update(executed=True, action_state="unknown", landing_reason="dig_sent")
                result = self._result(self.client.dig(args["clear_pos"], collect=False))
                if result.get("interrupted_by"):
                    raise _Failure("dig_unconfirmed", "Backend interrupted canopy digging")
                self._check(context)
                block = self._read_canopy(tuple(args["clear_pos"]), context)
                if block is None or block["name"] not in self.AIR:
                    raise _Failure("dig_unconfirmed", "Cleared leaf was not freshly confirmed as air")
                evidence["block_removed"] = True
            status = self._observe(context, at_origin=True)
            plan = self._plan(status, args, context, cleared=True)
            if self._terrain(plan, context, cleared=True) != support:
                raise _Failure("target_changed", "Landing support changed before movement")
            plan = self._prepare_write(args, context, support, cleared=True)
            context["acted"] = True
            evidence.update(executed=True, move_sent=True, action_state="unknown", landing_reason="move_sent")
            result = self._result(self.client.move_to(*args["pos"], range=0, timeout_ms=5000))
            if result.get("interrupted_by"):
                raise _Failure("action_unconfirmed", "Backend interrupted canopy movement")
            context["canopy_landing"] = True
            started = time.monotonic()
            context["retreat_landing_deadline"] = started + 1.25
            try:
                while evidence["landing_reads"] < 6:
                    evidence["landing_reads"] += 1
                    after = self._observe(context)
                    point = after["pos"]
                    if (any(abs(point[axis] - args["pos"][axis]) > .125 + 1e-9 for axis in (0, 2))
                            or not args["pos"][1] - 1e-4 <= point[1] <= context["canopy_origin"][1] + .05 + 1e-9):
                        raise _Failure("action_unconfirmed", "Movement left the checked canopy landing column")
                    if after.get("on_ground") is True:
                        if not self._landed_at_target(point, args["pos"]):
                            raise _Failure("action_unconfirmed", "Bot did not land at the exact checked target")
                        if self._terrain(plan, context, cleared=True) != support:
                            raise _Failure("action_unconfirmed", "Landing support changed during movement")
                        if evidence["landing_reads"] >= 6:
                            raise _Failure("action_unconfirmed", "No status budget remains to confirm landing after terrain reads",
                                           {"landing_termination": "read_limit"})
                        evidence["landing_reads"] += 1
                        after = self._observe(context)
                        point = after["pos"]
                        if after.get("on_ground") is not True or not self._landed_at_target(point, args["pos"]):
                            raise _Failure("action_unconfirmed", "Landing changed during final terrain verification")
                        self._check(context)
                        if time.monotonic() >= context["retreat_landing_deadline"]:
                            raise _Failure("action_unconfirmed", "Canopy landing verification exceeded its deadline")
                        complete = support in self.SUPPORT
                        evidence.update(moved=round(math.dist(context["canopy_origin"], point), 3),
                                        pos=point, action_state="verified", landing_reason="confirmed",
                                        landing_termination="confirmed", retreat_complete=complete,
                                        evacuation_stage="ground" if complete else "relay")
                        return dict(evidence)
                    evidence["landing_reason"] = "not_grounded"
                    if evidence["landing_reads"] < 6:
                        delay = min(.2, context["retreat_landing_deadline"] - time.monotonic())
                        if delay <= 0:
                            break
                        if context["cancel"] is None:
                            time.sleep(delay)
                        else:
                            context["cancel"].wait(delay)
                raise _Failure("action_unconfirmed", "No precise grounded landing within six observations",
                               {"landing_termination": "read_limit"})
            finally:
                evidence["landing_elapsed"] = round(time.monotonic() - started, 3)
                context.pop("retreat_landing_deadline", None)
        except Exception as error:
            if evidence["executed"]:
                code = "action_unconfirmed" if evidence["move_sent"] else "dig_unconfirmed"
                evidence.update(action_state="unknown", retreat_complete=False,
                                interruption_code=getattr(error, "code", "internal_error"))
                if evidence["landing_reason"] != "not_grounded":
                    evidence["landing_reason"] = evidence["interruption_code"]
                evidence.setdefault("landing_termination", "interrupted")
                evidence.update(getattr(error, "data", {}))
                evidence["action_state"] = "unknown"
                raise _Failure(code, str(error), dict(evidence)) from error
            raise
