"""Collect coordinate-only eye trajectories; never trust an arbitrary new entity."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import math
import time

from .calculator import CalculationError, EyeThrow, _finite

EYE_NAMES = frozenset(("eye_of_ender", "ender_eye", "eye_of_ender_signal"))


class ObservationError(CalculationError):
    """A throw cannot be converted into a defensible horizontal ray."""

    def __init__(self, message: str, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def fit_trajectory(positions, sigma_deg=None) -> tuple:
    """Fit a directed horizontal line to one identified eye's coordinate samples.

    Precision is limited by packet timing and the API's 0.1-block rounding. The
    default sigma is a conservative geometric estimate, not a real calibration.
    """
    unique = []
    for position in positions:
        if len(position) != 3:
            raise ObservationError("each eye position must contain x, y, z")
        point = tuple(_finite(value, "eye position") for value in position)
        if not unique or (point[0], point[2]) != (unique[-1][0], unique[-1][2]):
            unique.append(point)
    if len(unique) < 3:
        raise ObservationError("need at least three distinct horizontal eye positions")
    mx = sum(p[0] for p in unique) / len(unique)
    mz = sum(p[2] for p in unique) / len(unique)
    xx = sum((p[0] - mx) ** 2 for p in unique)
    zz = sum((p[2] - mz) ** 2 for p in unique)
    xz = sum((p[0] - mx) * (p[2] - mz) for p in unique)
    angle = 0.5 * math.atan2(2 * xz, xx - zz)
    dx, dz = math.cos(angle), math.sin(angle)
    if dx * (unique[-1][0] - unique[0][0]) + dz * (unique[-1][2] - unique[0][2]) < 0:
        dx, dz = -dx, -dz
    along = [(p[0] - mx) * dx + (p[2] - mz) * dz for p in unique]
    span = max(along) - min(along)
    residual = max(abs((p[0] - mx) * dz - (p[2] - mz) * dx) for p in unique)
    if span < 3:
        raise ObservationError("eye horizontal travel is under 3 blocks; direction is unreliable or the stronghold is very close")
    if residual > 0.2 or any(b < a - 0.2 for a, b in zip(along, along[1:])):
        raise ObservationError("eye trajectory is not a straight forward flight")
    estimate = max(0.05, math.degrees(math.atan2(0.1 + residual, span)))
    sigma = _finite(sigma_deg, "sigma_deg") if sigma_deg is not None else estimate
    # Use a point on the fitted trajectory, not the player position: hand/spawn
    # offsets must not rotate the measured flight direction.
    origin_x, origin_z = mx + min(along) * dx, mz + min(along) * dz
    throw = EyeThrow(origin_x, origin_z, math.degrees(math.atan2(-dx, dz)), sigma)
    return throw, {"unique_samples": len(unique), "span_blocks": span, "max_residual_blocks": residual,
                   "sigma_source": "explicit" if sigma_deg is not None else "uncalibrated_coordinate_bound"}


class EyeObservationCollector:
    """Throw one eye and sample its entity while the blocking use-item call runs.

    Calling capture consumes an eye. It does not move the bot, equip via RCON,
    navigate to a stronghold, or rely on use_item's potentially misidentified eye.
    """

    def __init__(self, client, poll_seconds: float = 0.1, duration_seconds: float = 3.5):
        if not 0.05 <= poll_seconds <= 1 or not 1 <= duration_seconds <= 5:
            raise ValueError("poll must be 0.05..1 seconds and capture duration 1..5 seconds")
        self.client = client
        self.poll_seconds = poll_seconds
        self.duration_seconds = duration_seconds

    def _status(self):
        response = self.client.get_status()
        if response.get("ok") is not True or not isinstance(response.get("result"), dict):
            raise ObservationError("status request failed")
        return response["result"]

    def _eyes(self):
        return [entity for entity in self.client.get_entities(radius=64, types=sorted(EYE_NAMES))
                if entity.get("name", "").removeprefix("minecraft:") in EYE_NAMES]

    def capture(self, sigma_deg=None) -> dict:
        if sigma_deg is not None:
            EyeThrow(0, 0, 0, sigma_deg)  # Validate before consuming an eye.
        before = self._status()
        if not before.get("ready") or before.get("alive") is not True or before.get("dimension") not in ("overworld", "minecraft:overworld"):
            raise ObservationError("bot must be ready and alive in the overworld")
        if before.get("busy") or before.get("in_vehicle") or before.get("jumping"):
            raise ObservationError("bot must be idle, stationary, and outside a vehicle")
        if any(before.get(flag) for flag in ("in_lava", "on_fire", "submerged")):
            raise ObservationError("bot is not in a suitable state for an eye measurement")
        inventory = self.client.get_inventory()
        items = list(inventory.get("items", [])) + [inventory.get("offhand") or {}]
        if not any(item.get("name") == "ender_eye" and item.get("count", 0) > 0 for item in items):
            raise ObservationError("inventory has no ender_eye; no item was used")
        existing = {entity["id"] for entity in self._eyes()}
        positions = {}
        diagnostics = {"player_before": before["pos"], "dimension": before["dimension"], "samples": []}
        failure = None
        # The endpoint is blocking, so observations must run concurrently with it.
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.client.use_item, "ender_eye")
            started = time.monotonic()
            try:
                while time.monotonic() - started < self.duration_seconds:
                    for entity in self._eyes():
                        entity_id = entity.get("id")
                        if entity_id is None or entity_id in existing:
                            continue
                        position = entity.get("pos")
                        if not isinstance(position, list) or len(position) != 3:
                            raise ObservationError("eye entity has no usable pos field")
                        positions.setdefault(entity_id, []).append(position)
                        diagnostics["samples"].append({"elapsed_s": round(time.monotonic() - started, 3),
                                                       "entity_id": entity_id, "pos": position})
                    if future.done() and future.exception() is not None:
                        future.result()
                    time.sleep(self.poll_seconds)
            except Exception as error:
                failure = error
            try:
                diagnostics["use_item"] = future.result(timeout=12)
            except Exception as error:
                failure = failure or error
        try:
            if failure:
                raise ObservationError(f"capture API call failed: {failure}") from failure
            if diagnostics["use_item"].get("ok") is not True or not diagnostics["use_item"].get("result", {}).get("thrown"):
                raise ObservationError("API did not confirm that an eye was thrown")
            if len(positions) != 1:
                raise ObservationError("expected exactly one new identified eye entity; observed " + str(len(positions)))
            after = self._status()
            diagnostics["player_after"] = after.get("pos")
            if not after.get("alive") or not after.get("ready") or after.get("dimension") != before["dimension"]:
                raise ObservationError("bot died, disconnected, or changed dimension during capture")
            if math.dist(before["pos"], after["pos"]) > 0.25:
                raise ObservationError("bot moved during capture; stay still and repeat from a stable position")
            entity_id, samples = next(iter(positions.items()))
            throw, quality = fit_trajectory(samples, sigma_deg)
            return {"throw": asdict(throw), "entity_id": entity_id, "quality": quality, "diagnostics": diagnostics}
        except (ValueError, ObservationError) as error:
            raise ObservationError(str(error), diagnostics) from error
