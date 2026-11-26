"""Recover a dropped eye after sampling has finished."""

from dataclasses import dataclass
import math
import re
import time
from typing import Any, Dict, Optional, Sequence, Tuple

from v2.domain import HAZARD_BLOCKS, LIQUID_BLOCKS, NON_SOLID_BLOCKS
from v2.kernel.client import APIError

_HOSTILE_THREAT_RADIUS = {
    "creeper": 10.0,
    "skeleton": 12.0,
    "stray": 12.0,
    "bogged": 12.0,
    "pillager": 12.0,
    "witch": 10.0,
}


def _parse_pos(value: Any) -> Optional[list[float]]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        coords = [float(value[0]), float(value[1]), float(value[2])]
    except (TypeError, ValueError):
        return None
    return coords if all(math.isfinite(c) for c in coords) else None


def _hostile_threat_radius(name: Any, default: float = 8.0) -> float:
    clean = str(name or "").removeprefix("minecraft:").lower()
    return _HOSTILE_THREAT_RADIUS.get(clean, float(default))


@dataclass
class _PickupResult:
    success: bool
    error_code: Optional[str] = None
    message: str = ""
    data: Dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.data is None:
            self.data = {}


class _Failure(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _EyePickup:
    PICKUP_DISTANCE = 20

    def __init__(self, client: Any) -> None:
        self.client = client

    @staticmethod
    def _is_safe_support(block: Dict[str, Any]) -> bool:
        name = str(block.get("name") or "").removeprefix("minecraft:")
        return bool(name) and name not in (NON_SOLID_BLOCKS | HAZARD_BLOCKS | LIQUID_BLOCKS)

    @staticmethod
    def _is_safe_air(block: Dict[str, Any]) -> bool:
        name = str(block.get("name") or "").removeprefix("minecraft:")
        return name in NON_SOLID_BLOCKS and name not in HAZARD_BLOCKS and name not in LIQUID_BLOCKS

    def _corridor(self, origin: Sequence[float], destination: Sequence[float]) -> None:
        steps = max(1, math.ceil(math.dist(origin, destination) * 2))
        seen = set()
        for step in range(steps + 1):
            frac = step / steps
            px = math.floor(origin[0] + (destination[0] - origin[0]) * frac)
            py = math.floor(origin[1] + (destination[1] - origin[1]) * frac)
            pz = math.floor(origin[2] + (destination[2] - origin[2]) * frac)
            if (px, py, pz) in seen:
                continue
            seen.add((px, py, pz))
            for cy, check_fn in ((py - 1, self._is_safe_support), (py, self._is_safe_air), (py + 1, self._is_safe_air)):
                resp = self.client.get_block([px, cy, pz], timeout_seconds=1.5)
                blk = resp.get("result") if isinstance(resp, dict) else None
                if not isinstance(blk, dict) or not check_fn(blk):
                    raise _Failure("unsafe_route", "Eye recovery corridor blocked or hazardous")

    def _pickup_route(self, origin: Sequence[float], destination: Sequence[float]) -> None:
        if abs(origin[1] - destination[1]) > 0.05:
            raise _Failure("unsafe_route", "Eye recovery needs a same-level path")
        self._corridor(origin, destination)
        length = math.dist(origin, destination)
        rows = self.client.get_entities(radius=math.ceil(length) + 16, types=["hostile"], timeout_seconds=2.0)
        if not isinstance(rows, list) or len(rows) >= 40:
            raise _Failure("unsafe_route", "Eye recovery needs a complete hostile scan")
        delta = [end - start for start, end in zip(origin, destination)]
        norm = length**2
        for row in rows:
            if not isinstance(row, dict) or (pos := _parse_pos(row.get("pos"))) is None:
                raise _Failure("unsafe_route", "Eye recovery has malformed hostile evidence")
            projection = sum((point - start) * axis for point, start, axis in zip(pos, origin, delta))
            fraction = min(1.0, max(0.0, projection / norm)) if norm else 0.0
            nearest = [start + fraction * axis for start, axis in zip(origin, delta)]
            if math.dist(pos, nearest) <= _hostile_threat_radius(row.get("name"), 8.0):
                raise _Failure("unsafe_route", "A hostile is close to the eye recovery route")

    def collect(self, origin: Sequence[float], drop_pos: Sequence[float], entity_id: int) -> _PickupResult:
        try:
            self._pickup_route(origin, drop_pos)
            self.client.move_to(x=float(drop_pos[0]), y=float(drop_pos[1]), z=float(drop_pos[2]), range=0.8, timeout_ms=8000)
            self.client.move_to(x=float(origin[0]), y=float(origin[1]), z=float(origin[2]), range=0.8, timeout_ms=8000)
            return _PickupResult(True, data={"entity_id": entity_id, "drop_pos": list(drop_pos)})
        except _Failure as exc:
            return _PickupResult(False, error_code=exc.code, message=exc.message)
        except APIError as err:
            return _PickupResult(False, error_code=err.code, message=err.message)


def eye_drops(rows):
    """Normalize only identified dropped eye stacks."""
    return {row["id"]: pos for row in rows if isinstance(row, dict)
            and type(row.get("id")) is int and row["id"] >= 0 and row.get("name") == "item"
            and isinstance(row.get("item"), str) and re.fullmatch(r"ender_eyex[1-9][0-9]*", row["item"])
            and (pos := _parse_pos(row.get("pos"))) is not None}


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
    return response.get("ok") is True and isinstance(support, dict) and _EyePickup._is_safe_support(support)


def recover_eye(client, status, origin, flight_id, last_position, existing_drops, eye_names):
    """Find a newly observed drop near the end of one identified flight."""
    position = _parse_pos(last_position)
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
            position = _parse_pos(flying[0].get("pos"))
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
                    current_pos = _parse_pos(current.get("pos"))
                    if (current_pos is None or "busy" not in current or current["busy"] not in (False, None)
                            or current.get("dimension") != origin["dimension"]
                            or current.get("last_death") != origin.get("last_death")
                            or math.dist(current_pos, origin["pos"]) > .25):
                        return {"status": "state_changed"}
                    result = _EyePickup(client).collect(origin["pos"], point, entity_id)
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
