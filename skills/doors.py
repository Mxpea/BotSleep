"""Explicit, verified door state changes; never blindly toggle or approach."""

import math

from core.block_geometry import trace_block_visibility
from .daily_survival import DailySurvivalSkills
from .survival import _Failure


class DoorSkills(DailySurvivalSkills):
    ARGUMENTS = {"set_door": ({"pos", "open"}, {"pos", "open"})}

    @staticmethod
    def _boolean(value):
        if type(value) is bool:
            return value
        if value in ("true", "false"):
            return value == "true"
        return None

    def _door_state(self, pos, context):
        lower = self._block(pos, context)
        upper = self._block([pos[0], pos[1] + 1, pos[2]], context)
        lp, up = lower.get("properties") or {}, upper.get("properties") or {}
        opened = self._boolean(lp.get("open"))
        if (lower["name"] not in self.DOORS or upper["name"] != lower["name"]
                or lp.get("half") != "lower" or up.get("half") != "upper"
                or lp.get("facing") not in {"north", "south", "east", "west"}
                or lp.get("facing") != up.get("facing") or lp.get("hinge") not in {"left", "right"}
                or lp.get("hinge") != up.get("hinge") or opened is None
                or self._boolean(up.get("open")) is not opened
                or not self._safe_block(lower) or not self._safe_block(upper)):
            raise _Failure("door_unverified", "A matching, known wooden door frame is required")
        return {"pos": list(pos), "name": lower["name"], "facing": lp["facing"], "hinge": lp["hinge"], "open": opened}

    def _set_door(self, args, context):
        pos = self._pos(args["pos"], block=True)
        wanted = args["open"]
        if type(wanted) is not bool:
            raise _Failure("bad_args", "Door open must be a boolean")
        context["retreat_evidence"] = {"executed": False, "pos": pos}
        status = self._status(context)
        death = status.get("last_death")
        context["daily_life"] = death.get("t") if isinstance(death, dict) else None

        def safe(source, target):
            self._overworld(source)
            if (source.get("gamemode") != "survival" or source.get("on_ground") is not True
                    or any(source.get(key) for key in ("in_water", "submerged", "in_lava", "on_fire"))):
                raise _Failure("unsafe", "Door operation requires stable dry survival footing")
            if math.dist(source["pos"], target) > 3:
                raise _Failure("needs_position", "Move to a checked nearby door stance first; this skill never approaches")
            hostile = source.get("nearest_hostile") or {}
            if wanted and isinstance(hostile.get("dist"), (int, float)) and hostile["dist"] <= 8:
                raise _Failure("unsafe", "Do not open a door toward a nearby hostile")

        safe(status, pos)
        before = self._door_state(pos, context)
        if before["open"] is wanted:
            return self._verified(context, door=before, already_in_state=True)
        interaction = None
        for target in (pos, [pos[0], pos[1] + 1, pos[2]]):
            if math.dist(status["pos"], target) > 3:
                continue
            visibility = trace_block_visibility(status["pos"], target,
                lambda point: self._block(point, context), max_queries=20)
            if visibility.get("visible") is True:
                interaction = target
                break
        if interaction is None:
            raise _Failure("occluded", "No verified interaction ray reaches the observed door")
        current = self._door_state(pos, context)
        if current != before:
            raise _Failure("target_changed", "Door state changed before interaction; reobserve")
        origin_cell = tuple(math.floor(value + .0001) for value in status["pos"])
        def guard(latest):
            safe(latest, interaction)
            if tuple(math.floor(value + .0001) for value in latest["pos"]) != origin_cell:
                raise _Failure("safety_changed", "Door stance changed before interaction")
        self._action(context, self.client.interact, pos=interaction, timeout_seconds=5, _validate_status=guard)
        self._status(context)
        try:
            after = self._door_state(pos, context)
        except _Failure:
            raise _Failure("action_unconfirmed", "Door frame changed after interaction; do not toggle again",
                           {"action_state": "unknown"}) from None
        if after["open"] is not wanted or any(after[key] != before[key] for key in ("name", "facing", "hinge")):
            raise _Failure("action_unconfirmed", "Requested door state was not confirmed after interaction",
                           {"door": after, "action_state": "unknown"})
        return self._verified(context, door=after, interaction_pos=interaction, changed=True)
