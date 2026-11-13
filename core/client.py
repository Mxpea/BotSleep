"""Minecraft Agent API Client

Wraps mc-agent-api HTTP REST endpoints for Bot control.
"""

import time
import requests
from typing import Optional, Dict, Any, List


class MinecraftAPIError(RuntimeError):
    """An API call failed, including business errors returned with HTTP 200."""

    def __init__(self, payload):
        self.payload = payload
        error = payload.get("error") or {}
        if not isinstance(error, dict):
            error = {"code": "invalid_response", "message": "invalid API error envelope"}
        self.code = error.get("code", "invalid_response")
        super().__init__(f"{self.code}: {error.get('message', 'API call failed')}")


class MinecraftAgentClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:18900/api/v1",
        username: str = "SleepBot",
        password: str = "agent-secret-pass",
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.token: Optional[str] = None

    @property
    def headers(self) -> Dict[str, str]:
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    def login(self, max_wait_seconds: int = 15) -> Dict[str, Any]:
        """Exchanges credentials for Bearer Token and waits for bot readiness."""
        url = f"{self.base_url}/auth/login"
        resp = requests.post(
            url,
            json={"username": self.username, "password": self.password},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Login failed: {data}")

        self.token = data["token"]

        start_time = time.time()
        while time.time() - start_time < max_wait_seconds:
            st = self.get_status()
            if st.get("ok") and st.get("result", {}).get("ready"):
                return st["result"]
            time.sleep(0.5)

        raise TimeoutError("Timed out waiting for bot to be ready in the server.")

    def logout(self) -> Dict[str, Any]:
        """Logs out and disconnects bot."""
        if not self.token:
            return {"ok": True}
        url = f"{self.base_url}/auth/logout"
        resp = requests.post(url, headers=self.headers, timeout=5)
        self.token = None
        return resp.json()

    def get_status(self) -> Dict[str, Any]:
        """Fetches current player status, position, inventory, and environment."""
        url = f"{self.base_url}/status"
        return requests.get(url, headers=self.headers, timeout=5).json()

    def get_events(self, since: int = 0) -> List[Dict[str, Any]]:
        """Retrieves recent event stream since sequence number `since`."""
        url = f"{self.base_url}/events"
        res = requests.get(url, headers=self.headers, params={"since": since}, timeout=5).json()
        return res.get("result", {}).get("events", [])

    def chat(self, message: str) -> Dict[str, Any]:
        """Broadcasts a chat message into the game server."""
        url = f"{self.base_url}/chat"
        return requests.post(url, headers=self.headers, json={"message": message}, timeout=5).json()

    def move_to(self, x: float, y: float, z: float, timeout_ms: int = 15000) -> Dict[str, Any]:
        """Instructs the bot to pathfind to target coordinates."""
        url = f"{self.base_url}/move_to"
        return requests.post(
            url,
            headers=self.headers,
            json={"x": x, "y": y, "z": z, "timeout_ms": timeout_ms},
            timeout=timeout_ms / 1000 + 5,
        ).json()

    def look_at(self, x: float, y: float, z: float) -> Dict[str, Any]:
        """Turns the bot's gaze towards target coordinates."""
        url = f"{self.base_url}/look_at"
        return requests.post(url, headers=self.headers, json={"x": x, "y": y, "z": z}, timeout=5).json()

    def posture(self, sneak: bool = False, shield: bool = False, jump: bool = False) -> Dict[str, Any]:
        """Updates persistent posture states."""
        url = f"{self.base_url}/posture"
        return requests.post(
            url,
            headers=self.headers,
            json={"sneak": sneak, "shield": shield, "jump": jump},
            timeout=5,
        ).json()

    def get_blocks(self, radius: int = 8) -> List[Dict[str, Any]]:
        """Queries non-air blocks within radius."""
        url = f"{self.base_url}/blocks"
        res = requests.get(url, headers=self.headers, params={"radius": radius}, timeout=5).json()
        return res.get("result", {}).get("blocks", [])

    def _checked_request(self, method: str, path: str, payload=None, timeout: float = 10) -> Dict[str, Any]:
        """Validate both HTTP status and the API's business success flag."""
        response = requests.request(
            method, f"{self.base_url}{path}", headers=self.headers, json=payload, timeout=timeout,
        )
        try:
            data = response.json()
        except ValueError as error:
            raise MinecraftAPIError({"error": {"code": "invalid_response", "message": "API did not return JSON"}}) from error
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise MinecraftAPIError(data if isinstance(data, dict) else {"error": {"code": "invalid_response"}})
        response.raise_for_status()
        if not isinstance(data.get("result"), dict):
            raise MinecraftAPIError({"error": {"code": "invalid_response", "message": "API result object missing"}})
        return data

    def get_entities(self, radius: int = 16, types: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Query entities through the current API, optionally filtering by name."""
        payload = {"radius": radius}
        if types is not None:
            payload["types"] = types
        result = self._checked_request("POST", "/get_entities", payload, timeout=5).get("result", {})
        entities = result.get("entities")
        if not isinstance(entities, list):
            raise MinecraftAPIError({"error": {"code": "invalid_response", "message": "entities list missing"}})
        return entities

    def get_inventory(self) -> Dict[str, Any]:
        """Read inventory before consuming an item."""
        return self._checked_request("GET", "/inventory", timeout=5)["result"]

    def place_block(self, x: float, y: float, z: float, item: Optional[str] = None) -> Dict[str, Any]:
        """Places a block at the target coordinate."""
        url = f"{self.base_url}/place"
        payload = {"x": x, "y": y, "z": z}
        if item:
            payload["item"] = item
        return requests.post(url, headers=self.headers, json=payload, timeout=10).json()

    def interact(
        self,
        pos: Optional[List[float]] = None,
        entity_id: Optional[int] = None,
        item: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Interacts (right-clicks) with a block or entity."""
        url = f"{self.base_url}/interact"
        payload = {}
        if pos is not None:
            payload["x"] = pos[0]
            payload["y"] = pos[1]
            payload["z"] = pos[2]
        if entity_id is not None:
            payload["entity_id"] = entity_id
        if item is not None:
            payload["item"] = item
        return requests.post(url, headers=self.headers, json=payload, timeout=10).json()

    def use_item(self, item: str, ms: Optional[int] = None) -> Dict[str, Any]:
        """Uses/consumes an item (e.g. food, bow, potion)."""
        payload = {"item": item}
        if ms is not None:
            payload["ms"] = ms
        return self._checked_request("POST", "/use_item", payload, timeout=10)
