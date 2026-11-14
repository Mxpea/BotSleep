"""Synchronous mc-agent-api client with explicit protocol errors.

Actions are sent once. A transport timeout does not prove that the server stopped
the action; callers must inspect status or stop it before deciding to retry.
"""

import math
import time
from typing import Any, Dict, List, Optional

import requests


class APIError(RuntimeError):
    """HTTP, transport, or mc-agent-api business failure."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(f"{code}: {message}")


class MinecraftAgentClient:
    """Return API envelopes, except the documented list convenience methods."""

    ACTION_MARGIN_SECONDS = 5.0
    PATHFINDING_SECONDS = 60.0

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:18900/api/v1",
        username: str = "SleepBot",
        password: str = "",
        timeout_seconds: float = 15,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout_seconds = self._positive(timeout_seconds, "timeout_seconds")
        self.token: Optional[str] = None

    @staticmethod
    def _positive(value: float, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise APIError("bad_args", f"{name} must be a positive finite number")
        return float(value)

    @property
    def headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
        authenticated: bool = True,
    ) -> Dict[str, Any]:
        timeout = self.timeout_seconds if timeout_seconds is None else self._positive(timeout_seconds, "timeout_seconds")
        kwargs: Dict[str, Any] = {"headers": self.headers if authenticated else {}, "timeout": timeout}
        if payload is not None:
            kwargs["json"] = payload
        if params is not None:
            kwargs["params"] = params
        try:
            response = requests.request(method, f"{self.base_url}/{endpoint}", **kwargs)
        except requests.Timeout:
            raise APIError("request_timeout", f"{endpoint} request timed out; server action state is unknown", details={"action_state": "unknown"}) from None
        except requests.RequestException:
            # requests exception text may contain URLs or supplied credentials.
            raise APIError("network_error", f"{endpoint} request failed") from None

        status_code = response.status_code
        try:
            data = response.json()
        except ValueError:
            raise APIError("invalid_json", f"{endpoint} returned invalid JSON", status_code) from None
        if not isinstance(data, dict):
            raise APIError("invalid_response", f"{endpoint} returned a non-object response", status_code)
        if not 200 <= status_code < 300 or data.get("ok") is False:
            error = data.get("error")
            error = error if isinstance(error, dict) else {}
            code = str(error.get("code") or ("http_error" if not 200 <= status_code < 300 else "api_error"))
            message = str(error.get("message") or f"{endpoint} failed (HTTP {status_code})")
            for secret in (self.password, self.token):
                if secret:
                    message = message.replace(secret, "[redacted]")
            details = {key: value for key, value in error.items() if key not in {"code", "message"}}
            raise APIError(code, message, status_code, details)
        if data.get("ok") is not True:
            raise APIError("invalid_response", f"{endpoint} response has no boolean ok=true", status_code)
        return data

    @staticmethod
    def _result(envelope: Dict[str, Any]) -> Dict[str, Any]:
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise APIError("invalid_response", "API result must be an object")
        return result

    @staticmethod
    def _list(result: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
        values = result.get(key)
        if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
            raise APIError("invalid_response", f"API {key} must be a list of objects")
        return values

    def _action_timeout(self, deadline_seconds: float) -> float:
        return max(self.timeout_seconds, deadline_seconds + self.ACTION_MARGIN_SECONDS)

    def login(self, max_wait_seconds: float = 15) -> Dict[str, Any]:
        """Log in and return a ready status; clean up the session on failure."""
        max_wait_seconds = self._positive(max_wait_seconds, "max_wait_seconds")
        if self.token:
            self.logout()
        try:
            data = self._request("POST", "auth/login", payload={"username": self.username, "password": self.password}, authenticated=False)
            token = data.get("token")
            if not isinstance(token, str) or not token:
                raise APIError("invalid_response", "Login response has no token")
            self.token = token
            deadline = time.monotonic() + max_wait_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise APIError("ready_timeout", "Timed out waiting for bot readiness")
                try:
                    status = self._result(self.get_status(timeout_seconds=min(self.timeout_seconds, remaining)))
                except APIError as error:
                    # Login returns before the mineflayer entity has spawned.
                    # In 1.21.11, entity/saturation fields can also be absent after
                    # login but before spawn. Match only this observed read error;
                    # every other internal/auth/transport failure stays visible.
                    initializing_numbers = (
                        error.code == "internal"
                        and error.status_code == 200
                        and error.message == "Cannot read properties of undefined (reading 'toFixed')"
                    )
                    if error.code not in {"not_connected", "not_ready", "wrong_server"} and not initializing_numbers:
                        raise
                else:
                    if status.get("ready") is True:
                        return status
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(0.5, remaining))
        except BaseException:
            if self.token:
                try:
                    self.logout()
                except Exception:
                    # Preserve the original failure, while clearing local auth.
                    self.token = None
            raise

    def logout(self) -> Dict[str, Any]:
        """Revoke the session; clear the local token even if the request fails."""
        if not self.token:
            return {"ok": True}
        try:
            return self._request("POST", "auth/logout")
        finally:
            self.token = None

    def get_status(self, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        return self._request("GET", "status", timeout_seconds=timeout_seconds)

    def get_events_page(self, since: int = 0, limit: int = 50) -> Dict[str, Any]:
        """Return the envelope, including last_seq, has_more and total."""
        response = self._request("GET", "events", params={"since": since, "limit": limit})
        self._list(self._result(response), "events")
        return response

    def get_events(self, since: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
        return self._result(self.get_events_page(since=since, limit=limit))["events"]

    def chat(self, message: str) -> Dict[str, Any]:
        return self._request("POST", "chat", payload={"message": message})

    def move_to(self, x: float, y: float, z: float, timeout_ms: int = 15000, range: float = 1) -> Dict[str, Any]:
        """Pathfind within range (default one block), with a server deadline."""
        deadline = self._positive(timeout_ms, "timeout_ms") / 1000
        return self._request("POST", "move_to", payload={"x": x, "y": y, "z": z, "range": range, "timeout": timeout_ms}, timeout_seconds=self._action_timeout(deadline))

    def look_at(self, x: float, y: float, z: float) -> Dict[str, Any]:
        return self._request("POST", "look", payload={"x": x, "y": y, "z": z})

    def posture(self, sneak: bool = False, shield: bool = False, jump: bool = False) -> Dict[str, Any]:
        return self._request("POST", "posture", payload={"sneak": sneak, "shield": shield, "jump": jump})

    def get_blocks(self, radius: int = 16, types: Optional[List[str]] = None, limit: int = 64) -> List[Dict[str, Any]]:
        """Typed queries return blocks; untyped queries return one row per type.

        Summary rows describe the nearest exposed block of each name and carry
        its count. They are not a complete list of every block in the radius.
        """
        payload: Dict[str, Any] = {"radius": radius, "limit": limit}
        if types is not None:
            payload["types"] = types
        result = self._result(self._request("POST", "find_blocks", payload=payload))
        if types:
            return self._list(result, "blocks")
        blocks = result.get("blocks")
        if not isinstance(blocks, dict) or any(not isinstance(value, dict) for value in blocks.values()):
            raise APIError("invalid_response", "Untyped block query must return a summary object")
        return [{"name": name, "pos": info.get("nearest"), "dist": info.get("dist"), "count": info.get("count")} for name, info in blocks.items()]

    def get_entities(self, radius: int = 16, types: Optional[List[str]] = None, *,
                     timeout_seconds: Optional[float] = None) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {"radius": radius}
        if types is not None:
            payload["types"] = types
        return self._list(self._result(self._request("POST", "get_entities", payload=payload,
                                                     timeout_seconds=timeout_seconds)), "entities")

    def get_block(self, pos: List[float], *, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        return self._request("POST", "get_block", payload={"pos": pos}, timeout_seconds=timeout_seconds)

    def inventory(self, *, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        return self._request("GET", "inventory", timeout_seconds=timeout_seconds)

    def get_inventory(self) -> Dict[str, Any]:
        return self.inventory()

    def equip(self, item: str, slot: str = "hand") -> Dict[str, Any]:
        return self._request("POST", "equip", payload={"item": item, "slot": slot})

    def stop(self, *, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        """Request an immediate stop; this does not guarantee task cancellation."""
        return self._request("POST", "stop", timeout_seconds=timeout_seconds)

    def connect(self) -> Dict[str, Any]:
        """Request reconnection once; readiness must be checked separately."""
        return self._request("POST", "connect")

    def find_blocks(self, types: List[str], radius: int = 16, limit: int = 64, *,
                    properties: Optional[Dict[str, Any]] = None,
                    mature: Optional[bool] = None) -> List[Dict[str, Any]]:
        """Return typed rows with the upstream crop/property filters."""
        payload: Dict[str, Any] = {"types": types, "radius": radius, "limit": limit}
        if properties is not None:
            payload["properties"] = properties
        if mature is not None:
            payload["mature"] = mature
        return self._list(self._result(self._request("POST", "find_blocks", payload=payload)), "blocks")

    def window_view(self, pos: List[int], *, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        """Open, inspect and close a GUI; this may pathfind and takes the action lock."""
        budget = self._action_timeout(65) if timeout_seconds is None else self._positive(timeout_seconds, "timeout_seconds")
        return self._request("POST", "window/view", payload={"target": {"pos": pos}}, timeout_seconds=budget)

    def window_act(self, pos: List[int], ops: List[Dict[str, Any]], *,
                   timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        """Send one bounded operation batch. Completed operations are not rolled back."""
        if not isinstance(ops, list) or not 1 <= len(ops) <= 8:
            raise APIError("bad_args", "window_act requires between one and eight operations")
        budget = self._action_timeout(115) if timeout_seconds is None else self._positive(timeout_seconds, "timeout_seconds")
        return self._request("POST", "window/act", payload={"target": {"pos": pos}, "ops": ops}, timeout_seconds=budget)

    def attack(self, entity_id: int, *, timeout_ms: int = 6000, max_hits: int = 6) -> Dict[str, Any]:
        """Bound passive-animal hunting and stop on damage, overriding upstream combat defaults."""
        deadline = self._positive(timeout_ms, "timeout_ms") / 1000
        return self._request("POST", "attack", payload={"entity_id": entity_id, "timeout": timeout_ms,
                             "max_hits": max_hits, "stop_on": ["hurt", "death"]},
                             timeout_seconds=self._action_timeout(deadline + 35))

    def dig(self, pos: List[float], collect: bool = True) -> Dict[str, Any]:
        """Allow 60s approach, 30s digging and up to four 8s pickup paths."""
        if not isinstance(pos, (list, tuple)) or len(pos) != 3:
            raise APIError("bad_args", "dig requires three coordinates")
        return self._request(
            "POST", "dig", payload={"x": pos[0], "y": pos[1], "z": pos[2], "collect": collect},
            timeout_seconds=self._action_timeout(130),
        )

    def craft(self, item: str, count: int = 1) -> Dict[str, Any]:
        """Request an inventory increment, allowing the backend's manual rounds.

        The backend has no overall crafting deadline. The 420s HTTP budget covers
        its normal fixed waits for 64 manual rounds plus approach, but a timeout
        still leaves completion unknown and must not trigger an automatic retry.
        """
        return self._request(
            "POST", "craft", payload={"item": item, "count": count},
            timeout_seconds=self._action_timeout(415),
        )

    def place_block(self, x: float, y: float, z: float, item: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"x": x, "y": y, "z": z}
        if item is not None:
            payload["item"] = item
        return self._request("POST", "place", payload=payload, timeout_seconds=self._action_timeout(self.PATHFINDING_SECONDS))

    def interact(self, pos: Optional[List[float]] = None, entity_id: Optional[int] = None, item: Optional[str] = None, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        """Interact with a target, allowing the backend's 60-second pathfinding.

        An explicit timeout_seconds bounds this HTTP wait only, not the backend
        action. On timeout, inspect/stop the bot before starting another action.
        """
        payload: Dict[str, Any] = {}
        if pos is not None:
            payload["pos"] = pos
        if entity_id is not None:
            payload["entity_id"] = entity_id
        if item is not None:
            payload["item"] = item
        timeout = self._action_timeout(self.PATHFINDING_SECONDS) if timeout_seconds is None else self._positive(timeout_seconds, "timeout_seconds")
        return self._request("POST", "interact", payload=payload, timeout_seconds=timeout)

    def use_item(self, item: str, ms: Optional[int] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"item": item}
        if ms is not None:
            payload["ms"] = ms
        return self._request("POST", "use_item", payload=payload, timeout_seconds=self._action_timeout(self.PATHFINDING_SECONDS))

    def find_nearest_block(self, name_contains: str = "_bed", radius: int = 16) -> Optional[Dict[str, Any]]:
        """Compatibility helper over the nearest-per-type block summary."""
        matching = [block for block in self.get_blocks(radius=radius) if name_contains in block["name"]]
        return min(matching, key=lambda block: block["dist"]) if matching else None

    def sleep_in_bed(self, bed_pos: List[float]) -> Dict[str, Any]:
        """Approach within two blocks, then request bed interaction."""
        self.move_to(*bed_pos, range=2)
        return self.interact(pos=bed_pos)

    def wake(self, bed_pos: List[float]) -> Dict[str, Any]:
        """Interact again with the occupied bed to request waking."""
        return self.interact(pos=bed_pos)
