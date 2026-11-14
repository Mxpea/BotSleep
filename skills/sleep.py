"""Verified bed discovery, sleeping and waking through the public game API."""

import math
import time
from typing import Any, Dict, List, Optional

from . import BaseSkill, SkillResult


class _SleepFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class AutoSleepSkill(BaseSkill):
    """Sleep in the overworld and verify that the night or storm has ended."""

    BED_TYPES = [f"{color}_bed" for color in (
        "white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray",
        "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black",
    )]
    FACING = {"north": (0, -1), "south": (0, 1), "west": (-1, 0), "east": (1, 0)}
    PATH_ERRORS = {"path_not_found", "stalled", "out_of_reach", "drive_timeout"}

    @staticmethod
    def _result(response: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(response, dict) or response.get("ok") is not True:
            error = response.get("error", {}) if isinstance(response, dict) else {}
            raise _SleepFailure(error.get("code", "invalid_response"), error.get("message", "API request failed"))
        result = response.get("result")
        if not isinstance(result, dict):
            raise _SleepFailure("invalid_response", "API response has no result object")
        if result.get("interrupted_by"):
            raise _SleepFailure("interrupted", f"Action interrupted by {result['interrupted_by']}")
        return result

    @staticmethod
    def _code(error: Exception) -> str:
        code = getattr(error, "code", "internal_error")
        if code in {"not_connected", "network_error", "connection_error", "invalid_token", "unauthorized"}:
            return "disconnected"
        return "not_ready" if code == "wrong_server" else code

    def _status(self) -> Dict[str, Any]:
        status = self._result(self.client.get_status())
        if status.get("alive") is False or status.get("health", 1) <= 0:
            raise _SleepFailure("dead", "Bot is dead")
        if status.get("ready") is not True or status.get("alive") is not True:
            raise _SleepFailure("not_ready", "Bot is not ready and alive")
        if status.get("dimension") not in {"overworld", "minecraft:overworld"}:
            raise _SleepFailure("unsafe_dimension", "Sleeping is allowed only in the overworld")
        for key in ("is_day", "thundering", "is_sleeping"):
            if not isinstance(status.get(key), bool):
                raise _SleepFailure("invalid_response", f"Missing boolean status field: {key}")
        return status

    @staticmethod
    def _sleep_time(status: Dict[str, Any]) -> bool:
        return not status["is_day"] or status["thundering"]

    def validate_preconditions(self, **kwargs) -> bool:
        """Read-only convenience check; execute returns detailed failure reasons."""
        try:
            status = self._status()
            return status["is_sleeping"] or self._sleep_time(status)
        except Exception:
            return False

    @staticmethod
    def _position(value: Any) -> Optional[List[float]]:
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            return None
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in value):
            return None
        return list(value)

    def _beds(self, blocks: List[Dict[str, Any]], origin: List[float]) -> List[Dict[str, Any]]:
        """Group head and foot blocks into one bed, including occupancy of either half."""
        beds = {}
        for block in blocks:
            if not isinstance(block, dict) or block.get("name") not in self.BED_TYPES:
                continue
            pos = self._position(block.get("pos"))
            if pos is None:
                continue
            properties = block.get("properties") or {}
            key_pos = list(pos)
            if properties.get("part") == "foot" and properties.get("facing") in self.FACING:
                dx, dz = self.FACING[properties["facing"]]
                key_pos[0] += dx
                key_pos[2] += dz
            key = (block["name"], *key_pos)
            occupied = properties.get("occupied") in (True, "true")
            if key not in beds:
                beds[key] = {"pos": pos, "name": block["name"], "occupied": occupied}
            else:
                beds[key]["occupied"] |= occupied
                if properties.get("part") == "head":
                    beds[key]["pos"] = pos
        return sorted(beds.values(), key=lambda b: math.dist(b["pos"], origin))

    def _threats(self, status: Dict[str, Any], bed_pos: List[float], distance: float) -> List[Dict[str, Any]]:
        entities = self.client.get_entities(radius=min(64, distance + 3), types=["hostile"])
        if not isinstance(entities, list):
            raise _SleepFailure("invalid_response", "Entity query did not return a list")
        threats = []
        origin = self._position(status.get("pos"))
        for entity in entities:
            if not isinstance(entity, dict) or entity.get("type") != "hostile":
                continue
            pos = self._position(entity.get("pos"))
            if pos is not None and (math.dist(pos, bed_pos) <= distance or (origin and math.dist(pos, origin) <= distance)):
                threats.append({"id": entity.get("id"), "name": entity.get("name"), "pos": pos})
        return threats

    def execute(
        self, radius: int = 16, threat_check: bool = True, *,
        timeout_seconds: float = 600, move_timeout_seconds: float = 15,
        confirm_timeout_seconds: float = 5, threat_distance: float = 8,
        poll_interval_seconds: float = 0.5, cancel_event=None,
    ) -> SkillResult:
        """Success requires verified sleep and wake evidence, not a successful click.

        Cancellation interrupts polling waits. Requests already in flight remain bounded
        by their HTTP timeout; the controller can call stop concurrently. Cleanup never
        blindly clicks a bed, since the same interaction can also put the bot to sleep.
        """
        started = time.monotonic()
        data: Dict[str, Any] = {"bed_pos": None, "state": "preconditions", "attempts": [], "evidence": []}
        owns_action = False

        def check() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise _SleepFailure("cancelled", "Sleep task cancelled")
            if time.monotonic() >= deadline:
                raise _SleepFailure("timeout", "Sleep task exceeded its deadline")

        def pause(until: float) -> None:
            check()
            delay = max(0, min(poll_interval_seconds, until - time.monotonic(), deadline - time.monotonic()))
            if cancel_event is None:
                time.sleep(delay)
            else:
                cancel_event.wait(delay)
            check()

        def status() -> Dict[str, Any]:
            check()
            value = self._status()
            check()
            return value

        def evidence(kind: str, value: Dict[str, Any]) -> None:
            data["evidence"].append({"kind": kind, "elapsed_seconds": round(time.monotonic() - started, 3), **value})

        def success(method: str, value: Dict[str, Any]) -> SkillResult:
            data["state"] = "completed"
            data["verification"] = method
            data["final_status"] = {k: value[k] for k in ("is_day", "thundering", "is_sleeping", "dimension")}
            data["elapsed_seconds"] = round(time.monotonic() - started, 3)
            return SkillResult(True, "Sleep verified; bot is awake after the night or storm", data)

        def wait_for_wake(value: Dict[str, Any]) -> SkillResult:
            nonlocal owns_action
            owns_action = True
            data["state"] = "sleeping"
            evidence("sleep_observed", {"is_sleeping": True})
            wake_deadline = None
            while True:
                if not value["is_sleeping"]:
                    if not self._sleep_time(value):
                        evidence("wake_observed", {"is_day": True, "thundering": False})
                        return success("observed_sleep_then_wake", value)
                    # Wake and world-time packets arrive independently. A stale night
                    # snapshot immediately after waking does not prove interruption.
                    if wake_deadline is None:
                        wake_deadline = min(deadline, time.monotonic() + confirm_timeout_seconds)
                        data["state"] = "confirming_wake"
                        evidence("wake_pending_time_confirmation", {
                            "is_day": value["is_day"], "thundering": value["thundering"],
                        })
                    if time.monotonic() >= wake_deadline:
                        raise _SleepFailure("sleep_interrupted", "Bot remained awake before the night or storm ended")
                    pause(wake_deadline)
                else:
                    if wake_deadline is not None:
                        evidence("sleep_resumed", {"is_sleeping": True})
                        data["state"] = "sleeping"
                        wake_deadline = None
                    pause(deadline)
                value = status()

        try:
            if not isinstance(radius, int) or isinstance(radius, bool) or not 1 <= radius <= 64:
                raise _SleepFailure("bad_args", "radius must be an integer between 1 and 64")
            if not isinstance(threat_check, bool):
                raise _SleepFailure("bad_args", "threat_check must be boolean")
            for name, value in (("timeout_seconds", timeout_seconds), ("move_timeout_seconds", move_timeout_seconds),
                                ("confirm_timeout_seconds", confirm_timeout_seconds), ("threat_distance", threat_distance),
                                ("poll_interval_seconds", poll_interval_seconds)):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise _SleepFailure("bad_args", f"{name} must be a finite positive number")
            if threat_distance > 61:
                raise _SleepFailure("bad_args", "threat_distance must not exceed 61")
            deadline = started + timeout_seconds
            initial = status()
            if initial["is_sleeping"]:
                return wait_for_wake(initial)
            if not self._sleep_time(initial):
                raise _SleepFailure("daytime", "It is daytime and not thundering")
            origin = self._position(initial.get("pos"))
            if origin is None:
                raise _SleepFailure("invalid_response", "Status has no valid position")
            data["state"] = "searching"
            blocks = self.client.get_blocks(radius=radius, types=self.BED_TYPES, limit=128)
            check()
            if not isinstance(blocks, list):
                raise _SleepFailure("invalid_response", "Bed query did not return a block list")
            beds = self._beds(blocks, origin)
            if not beds:
                raise _SleepFailure("no_bed_nearby", f"No beds found within {radius} blocks")

            last_code = "bed_occupied"
            for bed in beds:
                check()
                data["bed_pos"] = bed["pos"]
                attempt = {"bed_pos": bed["pos"], "name": bed["name"]}
                data["attempts"].append(attempt)
                if bed["occupied"]:
                    attempt["error_code"] = "bed_occupied"
                    continue
                before = status()
                if before["is_sleeping"]:
                    return wait_for_wake(before)
                if not self._sleep_time(before):
                    raise _SleepFailure("daytime", "Sleep time ended before the bot entered a bed")
                if threat_check:
                    threats = self._threats(before, before["pos"], threat_distance)
                    check()
                    if threats:
                        data["threats"] = threats
                        raise _SleepFailure("monsters_nearby", "Hostile entities are close to the bot; no retreat was attempted")
                data["state"] = "approaching"
                owns_action = True
                try:
                    budget = min(move_timeout_seconds, deadline - time.monotonic())
                    self._result(self.client.move_to(*bed["pos"], timeout_ms=max(1, int(budget * 1000)), range=2))
                except Exception as error:
                    check()
                    code = self._code(error)
                    if code in self.PATH_ERRORS or code == "timeout":
                        attempt["error_code"] = last_code = "path_unreachable"
                        self.client.stop()
                        continue
                    raise
                before = status()
                if before["is_sleeping"]:
                    return wait_for_wake(before)
                if not self._sleep_time(before):
                    raise _SleepFailure("daytime", "Sleep time ended while approaching the bed")
                reached = self._position(before.get("pos"))
                if reached is None or math.dist(reached, bed["pos"]) > 3.5:
                    attempt["error_code"] = last_code = "path_unreachable"
                    self.client.stop()
                    continue
                current = self._result(self.client.get_block(bed["pos"]))
                check()
                if current.get("name") not in self.BED_TYPES:
                    attempt["error_code"] = last_code = "bed_unavailable"
                    continue
                if (current.get("properties") or {}).get("occupied") in (True, "true"):
                    attempt["error_code"] = last_code = "bed_occupied"
                    continue
                if threat_check:
                    threats = self._threats(before, bed["pos"], threat_distance)
                    check()
                    if threats:
                        data["threats"] = threats
                        raise _SleepFailure("monsters_nearby", "Hostile entities are close to the bed; no retreat was attempted")
                before = status()  # Recheck dimension and life immediately before clicking a bed.
                if before["is_sleeping"]:
                    return wait_for_wake(before)
                if not self._sleep_time(before):
                    raise _SleepFailure("daytime", "Sleep time ended before interacting with the bed")
                reached = self._position(before.get("pos"))
                if reached is None or math.dist(reached, bed["pos"]) > 3.5:
                    attempt["error_code"] = last_code = "path_unreachable"
                    continue
                data["state"] = "confirming"
                try:
                    reply = self._result(self.client.interact(pos=bed["pos"], timeout_seconds=min(confirm_timeout_seconds, deadline - time.monotonic())))
                except Exception as error:
                    check()
                    code = self._code(error)
                    if code in {"cannot_sleep", "bed_occupied", "no_such_block", "out_of_reach"}:
                        attempt["error_code"] = last_code = "sleep_rejected"
                        continue
                    raise
                check()
                reported_slept = reply.get("slept") is True
                evidence("interaction", {"bed_pos": bed["pos"], "reported_slept": reported_slept})
                confirmation_deadline = min(deadline, time.monotonic() + confirm_timeout_seconds)
                while True:
                    current_status = status()
                    if current_status["is_sleeping"]:
                        attempt["sleep_observed"] = True
                        return wait_for_wake(current_status)
                    if reported_slept and self._sleep_time(before) and not self._sleep_time(current_status):
                        evidence("instant_sleep_transition", {"before_is_day": before["is_day"], "before_thundering": before["thundering"], "after_is_day": current_status["is_day"], "after_thundering": current_status["thundering"]})
                        return success("reported_sleep_and_time_transition", current_status)
                    if not self._sleep_time(current_status):
                        raise _SleepFailure("sleep_unverified", "Daylight arrived without evidence that this bot slept")
                    if time.monotonic() >= confirmation_deadline:
                        attempt["error_code"] = last_code = "sleep_unverified"
                        break
                    pause(confirmation_deadline)
            raise _SleepFailure(last_code, "No candidate bed produced verified sleep")
        except Exception as error:
            code = self._code(error)
            data["state"] = "cancelled" if code == "cancelled" else "failed"
            data["elapsed_seconds"] = round(time.monotonic() - started, 3)
            if owns_action:
                try:
                    self.client.stop()
                    data["cleanup"] = "stop_requested; bed was not clicked"
                except Exception as cleanup_error:
                    data["cleanup_error"] = self._code(cleanup_error)
            return SkillResult(False, str(error), data, code)
