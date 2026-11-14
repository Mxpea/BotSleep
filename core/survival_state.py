"""Fresh, bounded observations and atomic checkpoints for one bot/world scope."""

import hashlib
import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from core.client import APIError
from core.block_geometry import trace_block_visibility
from skills.survival import SurvivalSkills


LOGS = [f"{wood}_log" for wood in (
    "oak", "spruce", "birch", "jungle", "acacia", "dark_oak", "mangrove", "cherry", "pale_oak")]
BEDS = [f"{color}_bed" for color in (
    "white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray",
    "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black")]


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ObservationCollector:
    """Never substitute a stale snapshot when a required read fails."""

    # Twelve selected columns cover nearby exits without a full 5x5 voxel scan.
    LOCAL_COLUMNS = ((1, 0), (0, 1), (-1, 0), (0, -1),
                     (1, 1), (1, -1), (-1, 1), (-1, -1),
                     (2, 0), (0, 2), (-2, 0), (0, -2))
    PASSABLE = SurvivalSkills.AIR | {"short_grass", "leaf_litter"}

    def __init__(self, client, radius=24, include_terrain=False):
        self.client = client
        self.radius = radius
        self.include_terrain = include_terrain

    @staticmethod
    def _check_cancel(cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise APIError("cancelled", "Observation cancelled; discard the partial snapshot")

    @staticmethod
    def _position(status):
        pos = status.get("pos")
        if (not isinstance(pos, (list, tuple)) or len(pos) != 3
                or any(type(value) not in (int, float) or not -30000000 <= value <= 30000000
                       or not math.isfinite(value) for value in pos)):
            raise APIError("invalid_response", "Status position must contain three finite coordinates")
        return list(pos)

    @staticmethod
    def _cell(pos):
        # Ignore tiny floating-point jitter at exact block boundaries.
        return tuple(math.floor(value + 0.0001) for value in pos)

    def collect_safety(self, cancel_event=None, *, force=False, threat_radius=8):
        """A bounded status/entities snapshot, never fresh inventory or terrain.

        Return None after the status probe when no emergency requires this path.
        The caller must restrict partial snapshots to local retreat/wait actions.
        """
        started = time.monotonic()
        deadline = started + 2.0

        def options():
            self._check_cancel(cancel_event)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise APIError("observation_changed", "Short safety observation budget exhausted")
            return {"timeout_seconds": remaining}

        def read_status():
            response = self.client.get_status(**options())
            self._check_cancel(cancel_event)
            status = response.get("result") if isinstance(response, dict) else None
            if (not isinstance(status, dict) or type(status.get("ready")) is not bool
                    or type(status.get("alive")) is not bool or "busy" not in status
                    or not isinstance(status.get("dimension"), str)):
                raise APIError("invalid_response", "Short safety status lacks lifecycle or busy evidence")
            if status["ready"] and status["alive"]:
                self._position(status)
            return status

        first = read_status()
        nearby = first.get("nearest_hostile") or {}
        distance = nearby.get("dist") if isinstance(nearby, dict) else None
        near = type(distance) in (int, float) and math.isfinite(distance) and 0 <= distance <= threat_radius
        idle = first["ready"] and first["alive"] and (first["busy"] is None or first["busy"] is False)
        if not force and not near and idle:
            return None
        observation = {"status": first, "inventory": [], "counts": {}, "blocks": [], "entities": [],
                       "fresh": True, "observed_at": utc_now(), "observation_scope": "safety",
                       "inventory_fresh": False, "terrain_fresh": False}
        if not idle:
            return observation  # Local wait only; never overlap a backend action.
        entities = self.client.get_entities(radius=min(64, max(8, threat_radius + 2)), **options())
        self._check_cancel(cancel_event)
        if not isinstance(entities, list) or len(entities) > 64:
            raise APIError("invalid_response", "Short safety entity list is invalid")
        for entity in entities:
            if not isinstance(entity, dict) or not isinstance(entity.get("type"), str):
                raise APIError("invalid_response", "Short safety entity is invalid")
            self._position(entity)
        final = read_status()
        if (any(final.get(key) != first.get(key) for key in ("last_death", "dimension", "alive", "ready", "busy"))
                or final.get("alive") is not True or final.get("ready") is not True
                or not (final.get("busy") is None or final.get("busy") is False)):
            raise APIError("observation_changed", "Lifecycle or backend activity changed during short safety observation")
        origin, endpoint = self._position(first), self._position(final)
        if self._cell(origin) != self._cell(endpoint) or abs(origin[1] - endpoint[1]) >= .5:
            raise APIError("observation_changed", "Position changed during short safety observation")
        options()  # Enforce the same total budget after the final response.
        observation.update(status=final, entities=entities, duration_seconds=round(time.monotonic() - started, 3))
        return observation

    def _local_geometry(self, status, cancel_event):
        """Sample local support/body cells, without promising a traversable path.

        One collection caches at most five blocks per selected column (60 reads
        total). Flat ground usually fills both eight-row lists after 24 reads.
        Nothing is cached across observations or accepted from an unloaded cell.
        """
        origin = self._position(status)
        x, y, z = self._cell(origin)
        cache = {}
        standing, placement = [], []

        def block_at(pos):
            self._check_cancel(cancel_event)
            key = tuple(pos)
            if key not in cache:
                try:
                    block = self.client.get_block(list(pos))["result"]
                except APIError as error:
                    if error.code != "not_loaded":
                        raise
                    block = None
                self._check_cancel(cancel_event)
                if block is not None and (not isinstance(block, dict)
                        or not isinstance(block.get("name"), str)
                        or (block.get("properties") is not None and not isinstance(block["properties"], dict))):
                    raise APIError("invalid_response", "Local geometry block is malformed")
                cache[key] = block
            return cache[key]

        def safe(block):
            return block is not None and SurvivalSkills._safe_block(block)

        offsets = sorted(self.LOCAL_COLUMNS, key=lambda offset:
                         (x + offset[0] + 0.5 - origin[0]) ** 2
                         + (z + offset[1] + 0.5 - origin[2]) ** 2)
        for dx, dz in offsets:
            if len(standing) >= 8 and len(placement) >= 8:
                break
            # Current column is absent, including feet/head cells at other levels.
            for dy in (0, -1, 1):
                candidate = [x + dx, y + dy, z + dz]
                below = block_at([candidate[0], candidate[1] - 1, candidate[2]])
                if below is None:
                    break
                if below["name"] not in SurvivalSkills.SUPPORT or not safe(below):
                    continue
                feet = block_at(candidate)
                if not safe(feet) or feet["name"] not in self.PASSABLE:
                    continue
                head = block_at([candidate[0], candidate[1] + 1, candidate[2]])
                can_stand = safe(head) and head["name"] in self.PASSABLE
                can_place = feet["name"] in SurvivalSkills.AIR
                if can_stand and len(standing) < 8:
                    standing.append({"pos": [candidate[0] + 0.5, candidate[1], candidate[2] + 0.5],
                                     "support": below["name"]})
                if can_place and len(placement) < 8:
                    placement.append({"pos": candidate, "support": below["name"]})
                if can_stand and can_place:
                    break
        return {"local_geometry_origin": origin, "safe_stand_candidates": standing,
                "placement_candidates": placement}

    def _resource_visibility(self, observation, cancel_event):
        """Enrich a few nearby resources using only the original get_block API.

        Unknown shapes block a ray conservatively. No cached cell survives a
        collection, and at most 96 additional block reads serve all candidates.
        """
        origin = self._position(observation["status"])
        cache = {}
        groups = {"logs": 0, "minerals": 0}

        def block_at(pos):
            self._check_cancel(cancel_event)
            block = self.client.get_block(pos)["result"]
            self._check_cancel(cancel_event)
            if not isinstance(block, dict) or not isinstance(block.get("name"), str):
                raise APIError("invalid_response", "Resource visibility block is malformed")
            return block

        for row in observation["blocks"]:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                raise APIError("invalid_response", "Resource candidate is malformed")
            self._position({"pos": row.get("pos")})
        rows = sorted(observation["blocks"], key=lambda row: math.dist(origin, row["pos"]))
        for row in rows:
            group = "logs" if row["name"] in LOGS else "minerals"
            if row["name"] not in SurvivalSkills.GATHERABLE or groups[group] >= 8:
                continue
            if math.dist(origin, row["pos"]) > 8 or len(cache) >= 96:
                continue
            self._check_cancel(cancel_event)
            groups[group] += 1
            key = tuple(row["pos"])
            if key not in cache:
                try:
                    cache[key] = block_at(row["pos"])
                except APIError as error:
                    if error.code == "not_loaded":
                        cache[key] = None
                        continue
                    raise
            target = cache[key]
            if not isinstance(target, dict) or target.get("name") != row["name"]:
                continue
            reachable = target.get("can_dig", target.get("harvest_tool_ok"))
            if isinstance(reachable, bool):
                row["can_dig"] = reachable
            if len(cache) >= 96:
                continue
            visibility = trace_block_visibility(origin, row["pos"], block_at, cache=cache,
                                                max_queries=min(24, 96 - len(cache)))
            if isinstance(visibility.get("visible"), bool):
                row["can_see"] = visibility["visible"]
            row["visibility"] = visibility

    def collect(self, cancel_event=None):
        self._check_cancel(cancel_event)
        started = time.monotonic()
        status = self.client.get_status()["result"]
        self._check_cancel(cancel_event)
        if not isinstance(status, dict):
            raise APIError("invalid_response", "Status must be an object")
        observation = {"status": status, "inventory": [], "counts": {}, "blocks": [],
                       "entities": [], "fresh": True, "observed_at": utc_now()}
        if not status.get("ready") or not status.get("alive") or status.get("busy"):
            return observation
        origin = self._position(status)
        items = self.client.inventory()["result"].get("items")
        self._check_cancel(cancel_event)
        if not isinstance(items, list):
            raise APIError("invalid_response", "Inventory items must be a list")
        counts = {}
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or type(item.get("count")) is not int or item["count"] < 0:
                raise APIError("invalid_response", "Inventory item is malformed")
            counts[item["name"]] = counts.get(item["name"], 0) + item["count"]
        observation.update(inventory=items, counts=counts)
        # Separate resource classes so abundant stone cannot hide every bed/log.
        for names in (LOGS + BEDS + ["crafting_table", "furnace"],
                      ["stone", "cobblestone", "coal_ore", "deepslate_coal_ore"]):
            self._check_cancel(cancel_event)
            observation["blocks"].extend(self.client.get_blocks(radius=self.radius, types=names, limit=64))
        self._check_cancel(cancel_event)
        if self.include_terrain:
            # A model needs ground/foliage context to choose a workspace instead
            # of repeatedly proposing unsupported cells while standing in a tree.
            # Untyped rows describe only the nearest exposed block of each type.
            observation["terrain"] = self.client.get_blocks(radius=min(self.radius, 16), limit=64)
            self._check_cancel(cancel_event)
            observation["blocks"].extend(self.client.get_blocks(radius=self.radius, types=[
                "lava", "water", "fire", "soul_fire", "magma_block", "cactus",
                "campfire", "soul_campfire", "powder_snow", "sweet_berry_bush"], limit=32))
            observation.update(self._local_geometry(status, cancel_event))
            self._resource_visibility(observation, cancel_event)
        self._check_cancel(cancel_event)
        observation["entities"] = self.client.get_entities(radius=self.radius)
        self._check_cancel(cancel_event)
        # State may change while the scans are in flight; planning uses this last
        # status and each skill independently rechecks before any mutation.
        final_status = self.client.get_status()["result"]
        self._check_cancel(cancel_event)
        if not isinstance(final_status, dict):
            raise APIError("invalid_response", "Final status must be an object")
        if (final_status.get("last_death") != status.get("last_death")
                or final_status.get("dimension") != status.get("dimension")
                or final_status.get("alive") is not True or final_status.get("ready") is not True):
            # A respawn can complete during scanning. Never pair pre-death tools
            # with a post-respawn status and accidentally declare success.
            raise APIError("observation_changed", "Life or dimension changed during observation; discard and observe again")
        final_pos = self._position(final_status)
        if self._cell(final_pos) != self._cell(origin) or abs(final_pos[1] - origin[1]) >= 0.5:
            raise APIError("observation_changed", "Bot moved across a block or changed height during observation; observe again")
        observation["status"] = final_status
        observation["duration_seconds"] = round(time.monotonic() - started, 3)
        return observation


class SurvivalJournal:
    """Store intentions before writes; never replay a pending intention on load."""

    def __init__(self, directory, *, endpoint, username, world_id):
        scope = {"endpoint": endpoint.rstrip("/"), "username": username, "world_id": world_id}
        digest = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()[:16]
        self.directory = Path(directory) / digest
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "checkpoint.json"
        self.events_path = self.directory / "episodes.jsonl"
        self._lock = threading.RLock()
        self.state = {"version": 1, "scope": scope, "context": {}, "pending": None, "last_result": None}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if (not isinstance(loaded, dict) or loaded.get("version") != 1
                        or loaded.get("scope") != scope or not isinstance(loaded.get("context"), dict)
                        or (loaded.get("pending") is not None and not isinstance(loaded["pending"], dict))):
                    raise ValueError()
            except (ValueError, OSError):
                raise ValueError("Survival checkpoint is unreadable or incompatible; preserve it before choosing a new state_dir") from None
            self.state = loaded

    def update(self, **changes):
        with self._lock:
            self.state.update(changes, updated_at=utc_now())
            temporary = self.path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(self.state, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)

    def record(self, event, data):
        with self._lock, self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"timestamp": utc_now(), "event": event, "data": data},
                                    ensure_ascii=False, allow_nan=False) + "\n")
