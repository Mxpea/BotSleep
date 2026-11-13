"""Minecraft Agent API Python Client

Client wrapper for interacting with mc-agent-api (Mineflayer HTTP REST API).
"""

import time
import requests
from typing import Optional, Dict, Any, List


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
        """Login and wait until the bot is ready in the server."""
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

        # Wait for bot to be ready
        start_time = time.time()
        while time.time() - start_time < max_wait_seconds:
            st = self.get_status()
            if st.get("ok") and st.get("result", {}).get("ready"):
                return st["result"]
            time.sleep(0.5)

        raise TimeoutError("Timed out waiting for bot to be ready in the server.")

    def logout(self) -> Dict[str, Any]:
        """Logout and disconnect bot."""
        if not self.token:
            return {"ok": True}
        url = f"{self.base_url}/auth/logout"
        resp = requests.post(url, headers=self.headers, timeout=5)
        self.token = None
        return resp.json()

    def get_status(self) -> Dict[str, Any]:
        """Fetch bot status (position, health, day/night, sleeping, etc.)."""
        url = f"{self.base_url}/status"
        return requests.get(url, headers=self.headers, timeout=5).json()

    def chat(self, message: str) -> Dict[str, Any]:
        """Send a chat message to the server."""
        url = f"{self.base_url}/chat"
        return requests.post(url, headers=self.headers, json={"message": message}, timeout=5).json()

    def move_to(self, x: float, y: float, z: float, timeout_ms: int = 15000) -> Dict[str, Any]:
        """Move the bot to coordinates (x, y, z)."""
        url = f"{self.base_url}/move_to"
        return requests.post(
            url,
            headers=self.headers,
            json={"x": x, "y": y, "z": z, "timeout_ms": timeout_ms},
            timeout=timeout_ms / 1000 + 5,
        ).json()

    def look_at(self, x: float, y: float, z: float) -> Dict[str, Any]:
        """Look at the specified coordinates."""
        url = f"{self.base_url}/look_at"
        return requests.post(url, headers=self.headers, json={"x": x, "y": y, "z": z}, timeout=5).json()

    def get_blocks(self, radius: int = 8) -> List[Dict[str, Any]]:
        """Retrieve non-air blocks around the bot."""
        url = f"{self.base_url}/blocks"
        res = requests.get(url, headers=self.headers, params={"radius": radius}, timeout=5).json()
        return res.get("result", {}).get("blocks", [])

    def find_nearest_block(self, name_contains: str = "_bed", radius: int = 16) -> Optional[Dict[str, Any]]:
        """Find the nearest block whose name contains `name_contains`."""
        blocks = self.get_blocks(radius=radius)
        matching = [b for b in blocks if name_contains in b.get("name", "")]
        if not matching:
            return None
        matching.sort(key=lambda b: b.get("dist", 99999))
        return matching[0]

    def interact(
        self,
        pos: Optional[List[float]] = None,
        entity_id: Optional[int] = None,
        item: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Right-click interact with a block or entity."""
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

    def sleep_in_bed(self, bed_pos: List[float]) -> Dict[str, Any]:
        """Interact with a bed to sleep."""
        return self.interact(pos=bed_pos)

    def wake(self, bed_pos: List[float]) -> Dict[str, Any]:
        """Click on the bed again while sleeping to wake up."""
        return self.interact(pos=bed_pos)
