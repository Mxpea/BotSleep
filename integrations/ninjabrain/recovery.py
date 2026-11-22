"""Recover a dropped eye after sampling has finished."""

import math
import re
import time

from core.client import APIError
from core.domain import hostile_threat_radius, parse_pos
from skills.positioning import PositioningSkills
from skills.survival import _Failure


class _EyePickup(PositioningSkills):
    ARGUMENTS = {"collect_drops": ({"entity_id", "expected_item"}, {"entity_id", "expected_item"})}
    PICKUP_DISTANCE = 20
    MAX_READS = 192

    def _pickup_route(self, origin, destination, context):
        if abs(origin[1] - destination[1]) > .05:
            raise _Failure("unsafe_route", "Eye recovery needs a same-level path")
        self._corridor(origin, destination, context)
        length = math.dist(origin, destination)
        rows = self.client.get_entities(radius=math.ceil(length) + 16, types=["hostile"],
                                        **self._read_options(context))
        self._check(context)
        if not isinstance(rows, list) or len(rows) >= 40:
            raise _Failure("unsafe_route", "Eye recovery needs a complete hostile scan")
        delta = [end - start for start, end in zip(origin, destination)]
        norm = length**2
        for row in rows:
            if not isinstance(row, dict) or (pos := parse_pos(row.get("pos"))) is None:
                raise _Failure("unsafe_route", "Eye recovery has malformed hostile evidence")
            projection = sum((point - start) * axis for point, start, axis in zip(pos, origin, delta))
            fraction = min(1, max(0, projection / norm)) if norm else 0
            nearest = [start + fraction * axis for start, axis in zip(origin, delta)]
            if math.dist(pos, nearest) <= hostile_threat_radius(row.get("name"), 8):
                raise _Failure("unsafe_route", "A hostile is close to the eye recovery route")


def eye_drops(rows):
    """Normalize only identified dropped eye stacks."""
    return {row["id"]: pos for row in rows if isinstance(row, dict)
            and type(row.get("id")) is int and row["id"] >= 0 and row.get("name") == "item"
            and isinstance(row.get("item"), str) and re.fullmatch(r"ender_eyex[1-9][0-9]*", row["item"])
            and (pos := parse_pos(row.get("pos"))) is not None}


def _landed(client, point, origin, timeout):
    x, y, z = map(math.floor, point)
    if abs(y - origin[1]) > .05 or point[1] - y > .25:
        return False
    try:
        response = client.get_block([x, y - 1, z], timeout_seconds=timeout)
    except APIError as error:
        if error.code == "not_loaded":
            return False
        raise
    support = response.get("result")
    return (response.get("ok") is True and isinstance(support, dict)
            and support.get("name") in _EyePickup.SUPPORT and _EyePickup._safe_block(support))


def recover_eye(client, status, origin, flight_id, last_position, existing_drops, eye_names):
    """Find a newly observed drop near the end of one identified flight."""
    position = parse_pos(last_position)
    if position is None:
        return {"status": "flight_position_unknown"}
    deadline = time.monotonic() + 6
    previous = None
    flight_ended = False
    saw_drop = False
    while (remaining := deadline - time.monotonic()) > 0:
        rows = client.get_entities(radius=24, types=sorted(eye_names | {"item"}), timeout_seconds=min(1.5, remaining))
        if len(rows) >= 40:
            return {"status": "scan_saturated"}
        flying = [row for row in rows if row.get("name", "").removeprefix("minecraft:") in eye_names]
        if any(row.get("id") != flight_id for row in flying):
            return {"status": "ambiguous_flight"}
        if flying:
            position = parse_pos(flying[0].get("pos"))
            if position is None:
                return {"status": "flight_position_unknown"}
            previous = None
        else:
            flight_ended = True
            candidates = [(entity_id, point) for entity_id, point in eye_drops(rows).items()
                          if entity_id not in existing_drops and math.dist(origin["pos"], point) <= _EyePickup.PICKUP_DISTANCE
                          and math.hypot(point[0] - position[0], point[2] - position[2]) <= 3
                          and -1 <= position[1] - point[1] <= 12]
            if len(candidates) > 1:
                return {"status": "ambiguous_drop"}
            if candidates:
                saw_drop = True
                entity_id, point = candidates[0]
                # The item may still be falling. Two close same-cell reads
                # avoid dispatching a write toward a transient position.
                remaining = deadline - time.monotonic()
                if (previous and previous[0] == entity_id and math.dist(previous[1], point) <= .2
                        and [math.floor(v) for v in previous[1]] == [math.floor(v) for v in point]
                        and remaining > 0 and _landed(client, point, origin["pos"], min(1.5, remaining))):
                    current = status()
                    current_pos = parse_pos(current.get("pos"))
                    if (current_pos is None or "busy" not in current or current["busy"] not in (False, None)
                            or current.get("dimension") != origin["dimension"]
                            or current.get("last_death") != origin.get("last_death")
                            or math.dist(current_pos, origin["pos"]) > .25):
                        return {"status": "state_changed"}
                    result = _EyePickup(client).execute("collect_drops", {"entity_id": entity_id, "expected_item": "ender_eye"})
                    return {"status": "recovered" if result.success else "pickup_failed", "entity_id": entity_id,
                            "drop_pos": point, "error_code": result.error_code, "message": result.message,
                            "evidence": result.data}
                previous = (entity_id, point)
            else:
                previous = None
        time.sleep(min(.2, max(0, deadline - time.monotonic())))
    if saw_drop:
        return {"status": "no_verified_landing"}
    return {"status": "drop_unobserved" if flight_ended else "flight_not_finished"}
