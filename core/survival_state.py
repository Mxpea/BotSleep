"""Fresh, bounded observations and atomic checkpoints for one bot/world scope."""

import copy
import hashlib
import json
import math
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from core.client import APIError
from core.block_geometry import trace_block_visibility
from core.domain import BED_NAMES, LOG_NAMES, hostile_threat_radius
from skills.combat import HOSTILES, MELEE_RANGE, combat_options
from skills.survival import SurvivalSkills


LOGS = list(LOG_NAMES)
BEDS = list(BED_NAMES)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ObservationCollector:
    """Never substitute a stale snapshot when a required read fails."""

    # Twelve selected columns cover nearby exits without a full 5x5 voxel scan.
    LOCAL_COLUMNS = ((1, 0), (0, 1), (-1, 0), (0, -1),
                     (1, 1), (1, -1), (-1, 1), (-1, -1),
                     (2, 0), (0, 2), (-2, 0), (0, -2))
    PASSABLE = SurvivalSkills.AIR | {"short_grass", "leaf_litter"}

    def __init__(self, client, radius=24, include_terrain=False, *, include_footing=False, combat_mode=None):
        self.client = client
        self.radius = radius
        self.include_terrain = include_terrain
        self.include_footing = include_footing
        self.combat_mode = combat_mode

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

    def _collect_footing(self, observation, cancel_event):
        origin = self._position(observation["status"])
        x, y, z = self._cell(origin)
        pos = [x, y - 1, z]
        self._check_cancel(cancel_event)
        reply = self.client.get_block(pos, timeout_seconds=2)
        self._check_cancel(cancel_event)
        block = reply.get("result") if isinstance(reply, dict) and reply.get("ok") is True else None
        if (not isinstance(block, dict) or not isinstance(block.get("name"), str) or not block["name"]
                or block.get("properties") is not None and not isinstance(block["properties"], dict)
                or "pos" in block and self._position(block) != pos):
            raise APIError("invalid_response", "Footing block evidence is malformed")
        observation["footing_origin"] = origin
        observation["footing"] = {"pos": pos, "name": block["name"],
            "properties": copy.deepcopy(block.get("properties") or {}),
            "fresh": True, "verified": True, "source": "api_verified"}

    def _status(self, original, cancel_event, *, grounded=False, timeout_seconds=2):
        self._check_cancel(cancel_event)
        reply = self.client.get_status(timeout_seconds=timeout_seconds)
        self._check_cancel(cancel_event)
        status = reply.get("result") if isinstance(reply, dict) and reply.get("ok") is True else None
        if not isinstance(status, dict):
            raise APIError("invalid_response", "Status response is malformed")
        if (status.get("alive") is not True or status.get("ready") is not True
                or status.get("dimension") != original.get("dimension")
                or status.get("last_death") != original.get("last_death")
                or "busy" not in status or (status["busy"] is not None and status["busy"] is not False)
                or grounded and status.get("on_ground") is not True):
            raise APIError("observation_changed", "Stance check requires the same idle living bot")
        pos, origin = self._position(status), self._position(original)
        if self._cell(pos) != self._cell(origin) or math.dist(pos, origin) > .2:
            raise APIError("observation_changed", "Bot moved during the stance check")
        return status

    def collect_safety(self, cancel_event=None, *, force: bool = False,
                       threat_radius: float = 8) -> dict | None:
        """Read bounded threat evidence for local safety decisions.

        Eligible encounters may include a fresh inventory for combat only.
        None indicates that a full observation can proceed.
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

        def near_summary(status):
            row = status.get("nearest_hostile")
            gap = row.get("dist") if isinstance(row, dict) else None
            limit = hostile_threat_radius(row.get("name") if isinstance(row, dict) else None, threat_radius)
            return type(gap) in (int, float) and math.isfinite(gap) and 0 <= gap <= limit

        near = near_summary(first)
        # The nearest enemy can be a harmlessly distant melee mob while a
        # slightly farther skeleton can still shoot. One bounded entity read
        # resolves that ambiguity, rather than starting a full terrain scan.
        possible_ranged = (type(distance) in (int, float) and math.isfinite(distance)
                           and 0 <= distance <= max(threat_radius, 16))
        idle = first["ready"] and first["alive"] and (first["busy"] is None or first["busy"] is False)
        if not force and not near and not possible_ranged and idle:
            return None
        observation = {"status": first, "inventory": [], "counts": {}, "blocks": [], "entities": [],
                       "fresh": True, "observed_at": utc_now(), "observation_scope": "safety",
                       "inventory_fresh": False, "terrain_fresh": False}
        if not idle:
            return observation  # Local wait only; never overlap a backend action.
        entities = self.client.get_entities(radius=min(64, max(18, threat_radius + 2)), **options())
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
        dangerous = any(entity["type"] == "hostile" and math.dist(endpoint, entity["pos"]) <=
                        hostile_threat_radius(entity.get("name"), threat_radius) for entity in entities)
        if not force and not near_summary(final) and not dangerous:
            return None  # A distant melee-only scan must not starve full observations.
        observation.update(status=final, entities=entities, duration_seconds=round(time.monotonic() - started, 3))
        return self._collect_combat_inventory(observation, cancel_event, threat_radius)

    def _collect_combat_inventory(self, observation, cancel_event, threat_radius):
        status = observation["status"]
        if self.combat_mode is None or status.get("on_ground") is not True:
            return observation
        targets = [row for row in observation["entities"] if row["type"] == "hostile" and row.get("name") in HOSTILES]
        if self.combat_mode == "starter":
            targets = [row for row in targets if row["name"] == "zombie"
                       and 1.5 <= math.dist(row["pos"], status["pos"]) <= MELEE_RANGE]
        if not targets:
            return observation
        deadline = time.monotonic() + 1.5

        def remaining():
            self._check_cancel(cancel_event)
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise APIError("request_timeout", "Combat inventory observation budget exhausted")
            return seconds

        try:
            reply = self.client.inventory(timeout_seconds=remaining())
            inventory = reply.get("result") if isinstance(reply, dict) and reply.get("ok") is True else None
            items = inventory.get("items") if isinstance(inventory, dict) else None
            if (not isinstance(items, list) or len(items) > 256
                    or any(not isinstance(row, dict) or not isinstance(row.get("name"), str)
                           or type(row.get("count")) is not int or row["count"] < 0 for row in items)):
                raise APIError("invalid_response", "Combat inventory evidence is malformed")
            final = self._status(status, cancel_event, grounded=True, timeout_seconds=remaining())
            remaining()
        except APIError as error:
            if self.combat_mode != "starter" or error.code not in {
                    "request_timeout", "network_error", "invalid_json", "invalid_response"}:
                raise
            # The retreat skill will recheck status and terrain before any write.
            observation["combat"] = {"allowed": False, "reason": "combat_observation_unavailable",
                                     "error_code": error.code}
            return observation
        counts = {}
        for row in items:
            counts[row["name"]] = counts.get(row["name"], 0) + row["count"]
        observation.update(status=final, inventory=copy.deepcopy(items), counts=counts, inventory_fresh=True)
        if isinstance(inventory.get("armor"), dict):
            observation["armor"] = copy.deepcopy(inventory["armor"])
        observation["combat"] = combat_options(observation, starter_defense=self.combat_mode == "starter",
                                                threat_radius=threat_radius)
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
        if self.include_footing and status.get("on_ground") is True and "busy" in status:
            self._collect_footing(observation, cancel_event)
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


class SurvivalInstanceError(RuntimeError):
    """Raised when another active runner already owns the same bot/world scope."""

    def __init__(self, code: str, message: str, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


_JOURNAL_LOCKS = {}
_INSTANCE_LOCKS = {}
_JOURNAL_REGISTRY_LOCK = threading.Lock()


class SurvivalJournal:
    """Store intentions before writes; never replay a pending intention on load."""

    def __init__(self, directory, *, endpoint, username, world_id):
        scope = {"endpoint": endpoint.rstrip("/"), "username": username, "world_id": world_id}
        self.scope = scope
        digest = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()[:16]
        self.directory = Path(directory) / digest
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "checkpoint.json"
        self.events_path = self.directory / "episodes.jsonl"
        self.lock_path = self.directory / "checkpoint.lock"
        self.instance_lock_path = self.directory / "instance.lock"
        self.instance_meta_path = self.directory / "instance.json"
        with _JOURNAL_REGISTRY_LOCK:
            self._lock = _JOURNAL_LOCKS.setdefault(str(self.lock_path.resolve()), threading.RLock())
            self._instance_thread_lock = _INSTANCE_LOCKS.setdefault(str(self.instance_lock_path.resolve()), threading.Lock())
        self._lock_depth = 0
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

    @staticmethod
    def _os_lock(handle):
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _os_unlock(handle):
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _file_lock(self, wait_seconds=5.0):
        deadline = time.monotonic() + wait_seconds
        while not self._lock.acquire(timeout=0.05):
            if time.monotonic() >= deadline:
                raise SurvivalInstanceError("journal_lock_busy", "Timed out waiting for survival checkpoint lock")
        handle = None
        acquired_os = False
        try:
            self._lock_depth += 1
            if self._lock_depth == 1:
                handle = self.lock_path.open("a+b")
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                while True:
                    try:
                        self._os_lock(handle)
                        acquired_os = True
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise SurvivalInstanceError("journal_lock_busy", "Another process is holding the survival checkpoint lock") from None
                        time.sleep(0.02)
            yield
        finally:
            self._lock_depth -= 1
            if acquired_os and handle is not None:
                try:
                    self._os_unlock(handle)
                except OSError:
                    pass
            if handle is not None:
                handle.close()
            self._lock.release()

    def _write_json_atomic(self, target_path: Path, payload: dict) -> None:
        temporary = target_path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target_path)
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def _read_instance_meta(self) -> dict:
        if not self.instance_meta_path.exists():
            return {}
        try:
            data = json.loads(self.instance_meta_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @contextmanager
    def instance_lock(self):
        """Hold an OS-level exclusive runner lock for this bot/world scope."""
        if not self._instance_thread_lock.acquire(blocking=False):
            meta = self._read_instance_meta()
            raise SurvivalInstanceError(
                "instance_already_running",
                f"Another BotSleep runner already owns survival scope {self.scope['username']}@{self.scope['world_id']}",
                {"scope": copy.deepcopy(self.scope), "holder_pid": meta.get("pid")},
            )
        handle = None
        acquired_os = False
        try:
            handle = self.instance_lock_path.open("a+b")
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            try:
                self._os_lock(handle)
                acquired_os = True
            except OSError:
                meta = self._read_instance_meta()
                holder_pid = meta.get("pid")
                suffix = f" (pid={holder_pid})" if holder_pid is not None else ""
                raise SurvivalInstanceError(
                    "instance_already_running",
                    f"Another BotSleep process{suffix} already owns survival scope {self.scope['username']}@{self.scope['world_id']}",
                    {"scope": copy.deepcopy(self.scope), "holder_pid": holder_pid},
                ) from None
            self._write_json_atomic(self.instance_meta_path, {
                "state": "running",
                "pid": os.getpid(),
                "started_at": utc_now(),
                "scope": self.scope,
            })
            yield self
        finally:
            if acquired_os:
                try:
                    self._write_json_atomic(self.instance_meta_path, {
                        "state": "idle",
                        "pid": os.getpid(),
                        "stopped_at": utc_now(),
                        "scope": self.scope,
                    })
                except OSError:
                    pass
                try:
                    self._os_unlock(handle)
                except OSError:
                    pass
            if handle is not None:
                handle.close()
            self._instance_thread_lock.release()

    def instance_status(self) -> dict:
        """Probe whether an active runner currently holds the scope instance lock."""
        meta = self._read_instance_meta()
        if not self._instance_thread_lock.acquire(blocking=False):
            return {"locked": True, "holder_pid": meta.get("pid", os.getpid()),
                    "started_at": meta.get("started_at"), "scope": copy.deepcopy(self.scope)}
        handle = None
        locked = False
        try:
            if self.instance_lock_path.exists():
                handle = self.instance_lock_path.open("a+b")
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                try:
                    self._os_lock(handle)
                    self._os_unlock(handle)
                except OSError:
                    locked = True
        finally:
            if handle is not None:
                handle.close()
            self._instance_thread_lock.release()
        return {
            "locked": locked,
            "holder_pid": meta.get("pid") if locked else None,
            "started_at": meta.get("started_at") if locked else None,
            "stopped_at": None if locked else meta.get("stopped_at"),
            "scope": copy.deepcopy(self.scope),
        }

    def update(self, **changes):
        with self._file_lock():
            self.state.update(changes, updated_at=utc_now())
            self._write_json_atomic(self.path, self.state)

    def has_pending_intent(self) -> bool:
        """Return True iff an unverified pending game-action intent is recorded."""
        return isinstance(self.state.get("pending"), dict)

    def get_mining_progress(self) -> Optional[Dict[str, Any]]:
        """Return a deep copy of persisted mining_progress context if present."""
        raw = self.state.get("context", {}).get("mining_progress")
        return copy.deepcopy(raw) if isinstance(raw, dict) else None

    def save_mining_progress(self, progress: Dict[str, Any]) -> None:
        """Atomically persist mining_progress into checkpoint context."""
        if not isinstance(progress, dict):
            raise ValueError("mining_progress must be a dictionary")
        with self._file_lock():
            ctx = self.state.setdefault("context", {})
            ctx["mining_progress"] = copy.deepcopy(progress)
            self.state["updated_at"] = utc_now()
            self._write_json_atomic(self.path, self.state)

    def can_resume_mining(self) -> bool:
        """Return True only when mining_progress exists and no pending intent blocks resume."""
        return (not self.has_pending_intent()) and (self.get_mining_progress() is not None)

    def record(self, event, data):
        with self._file_lock(), self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"timestamp": utc_now(), "event": event, "data": data},
                                    ensure_ascii=False, allow_nan=False) + "\n")
