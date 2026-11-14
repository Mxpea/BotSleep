"""Bounded, scoped factual memory; never an inventory, safety or replay authority.

All input is local structured evidence. This module has no API/model dependency.
SQLite transactions keep episodes, places and failure strategies consistent. The
caller must still obtain a fresh observation before acting on any recalled row.
"""

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit


class SurvivalMemory:
    VERSION = 1
    SOURCES = {"api_verified", "skill_verified", "lifecycle", "user_verified", "derived"}
    KINDS = ("episodes", "places", "strategies")
    MAX_ROW_BYTES = 4096
    MAX_TTL = 365 * 86400
    IDENTIFIER = re.compile(r"[A-Za-z0-9_:.\-/]{1,96}\Z")
    SECRET_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}|\bBearer\s+\S+", re.I)
    STRATEGIES = {"reobserve", "choose_other_target", "acquire_ingredients",
                  "verify_before_retry", "wait_for_safe_state"}
    PLACE_PRIORITY = {"home": 4, "shelter": 4, "bed": 3, "workstation": 3,
                      "storage": 3, "hazard": 2, "resource": 1}
    COUNT_KEYS = {"before_counts", "after_counts", "inventory_delta"}
    POSITION_KEYS = {"pos", "target", "pos_before", "pos_after"}
    FACT_KEYS = {"action", "success", "error_code", "target", "count_before",
                 "count_after", "gained", "consumed", "moved", "before_counts",
                 "after_counts", "inventory_delta", "executed", "block_removed",
                 "death_count", "from_dimension", "to_dimension", "reason_code",
                 "name", "can_see", "can_dig", "occupied", "pos_before", "pos_after"}

    def __init__(self, directory, *, endpoint, username, world_id, clock=time.time,
                 secrets=(), max_episodes=256, max_places=128, max_strategies=128):
        self._lock = threading.RLock()
        self._closed = False
        self._clock = clock
        self._secrets = {s for s in secrets if isinstance(s, str) and s}
        self._limits = dict(zip(self.KINDS, (max_episodes, max_places, max_strategies)))
        if any(type(n) is not int or not 1 <= n <= 4096 for n in self._limits.values()):
            raise ValueError("Memory capacity must be an integer between 1 and 4096")
        # Include ample B-tree/index/overflow overhead, then impose a hard SQLite
        # page ceiling as well as the much smaller retained record/row budgets.
        self.max_database_bytes = 262144 + sum(self._limits.values()) * self.MAX_ROW_BYTES * 4
        if not all(isinstance(v, str) and v.strip() for v in (endpoint, username, world_id)):
            raise ValueError("Memory scope requires endpoint, username and world_id")
        try:
            url = urlsplit(endpoint)
            if url.scheme not in {"http", "https"} or not url.hostname:
                raise ValueError()
            host = url.hostname.lower()
            if ":" in host:
                host = "[" + host + "]"
            if url.port:
                host += ":" + str(url.port)
            canonical = urlunsplit((url.scheme.lower(), host, url.path.rstrip("/"), "", ""))
        except ValueError:
            raise ValueError("Memory endpoint is invalid") from None
        if url.password:
            self._secrets.add(unquote(url.password))
        scope = {"endpoint": canonical, "username": username, "world_id": world_id}
        self.scope_id = hashlib.sha256(self._json(scope).encode()).hexdigest()
        self.directory = Path(directory) / self.scope_id[:32]
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "memory.sqlite3"
        try:
            with self._transaction() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, self.VERSION):
                    raise ValueError("Memory schema is incompatible; preserve the database")
                db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                existing = db.execute("SELECT value FROM meta WHERE key='scope'").fetchone()
                if existing and existing[0] != self.scope_id:
                    raise ValueError("Memory scope does not match; preserve the database")
                db.execute("INSERT OR IGNORE INTO meta VALUES ('scope', ?)", (self.scope_id,))
                db.execute("CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY, "
                           "kind TEXT NOT NULL, identity TEXT NOT NULL, dimension TEXT, "
                           "observed_at REAL NOT NULL, expires_at REAL NOT NULL, "
                           "confidence REAL NOT NULL, payload TEXT NOT NULL, UNIQUE(kind, identity))")
                db.execute("CREATE INDEX IF NOT EXISTS memory_lookup ON memories(kind, dimension, expires_at)")
                db.execute("PRAGMA user_version=1")
                self._prune(db, self._now())
        except sqlite3.DatabaseError:
            raise ValueError("Memory database is unreadable; preserve it before choosing a new directory") from None

    @staticmethod
    def _json(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @contextmanager
    def _transaction(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("Memory is closed")
            db = sqlite3.connect(self.path, timeout=5)
            try:
                db.execute("PRAGMA secure_delete=ON")
                db.execute("PRAGMA synchronous=FULL")
                page_size = db.execute("PRAGMA page_size").fetchone()[0]
                db.execute("PRAGMA max_page_count=" + str(max(64, self.max_database_bytes // page_size)))
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def _now(self):
        now = self._clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise ValueError("Memory clock must return a finite nonnegative timestamp")
        return float(now)

    def _redact(self, value):
        if isinstance(value, str):
            for secret in sorted(self._secrets, key=len, reverse=True):
                value = value.replace(secret, "redacted")
            return self.SECRET_PATTERN.sub("redacted", value)
        if isinstance(value, dict):
            # Schema keys are not prose. Short passwords must not corrupt them.
            return {k: ({self._redact(str(name)): count for name, count in v.items()}
                        if k in self.COUNT_KEYS and isinstance(v, dict) else self._redact(v))
                    for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact(v) for v in value]
        return value

    def _identifier(self, value):
        if not isinstance(value, str):
            return None
        value = self._redact(value)
        if "://" in value or not self.IDENTIFIER.fullmatch(value):
            return None
        return value

    @staticmethod
    def _pos(value):
        if (not isinstance(value, (list, tuple)) or len(value) != 3
                or any(type(x) not in (int, float) or not math.isfinite(x)
                       or abs(x) > 30000000 for x in value)):
            return None
        return list(value)

    def _facts(self, data):
        """Discard arbitrary prose, chat, prompts, credentials and model thoughts."""
        if not isinstance(data, dict):
            return {}
        result = {}
        for key in sorted(self.FACT_KEYS & data.keys()):
            value = data[key]
            if key in self.COUNT_KEYS and isinstance(value, dict):
                result[key] = {name: n for k, n in list(value.items())[:8]
                               if (name := self._identifier(k)) and type(n) is int and abs(n) <= 1000000}
            elif key in self.POSITION_KEYS:
                if (pos := self._pos(value)) is not None:
                    result[key] = pos
                elif key == "target" and (name := self._identifier(value)):
                    result[key] = name
            elif isinstance(value, bool):
                result[key] = value
            elif type(value) in (int, float) and math.isfinite(value) and abs(value) <= 1000000:
                result[key] = value
            elif (name := self._identifier(value)):
                result[key] = name
        return result

    def _write(self, db, kind, identity, dimension, payload, *, source, confidence, ttl_seconds, now):
        if source not in self.SOURCES:
            raise ValueError("Memory source is not supported")
        if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("Memory confidence must be finite and between zero and one")
        if (type(ttl_seconds) not in (int, float) or not math.isfinite(ttl_seconds)
                or not 0 < ttl_seconds <= self.MAX_TTL):
            raise ValueError("Memory TTL must be finite, positive and at most one year")
        payload = self._redact({**payload, "dimension": dimension, "source": source,
                                "observed_at": now, "expires_at": now + ttl_seconds,
                                "confidence": float(confidence), "requires_revalidation": True})
        encoded = self._json(payload)
        if len(encoded.encode("utf-8")) > self.MAX_ROW_BYTES:
            raise ValueError("Memory evidence exceeds the per-record byte budget")
        db.execute("INSERT INTO memories(kind,identity,dimension,observed_at,expires_at,confidence,payload) "
                   "VALUES (?,?,?,?,?,?,?) ON CONFLICT(kind,identity) DO UPDATE SET "
                   "dimension=excluded.dimension, observed_at=excluded.observed_at, "
                   "expires_at=excluded.expires_at, confidence=excluded.confidence, payload=excluded.payload",
                   (kind, identity, dimension, now, now + ttl_seconds, confidence, encoded))

    def _prune(self, db, now):
        db.execute("DELETE FROM memories WHERE expires_at<=?", (now,))
        for kind, limit in self._limits.items():
            if kind == "places":
                rows = db.execute("SELECT id,observed_at,payload FROM memories WHERE kind='places'").fetchall()
                rows.sort(key=lambda row: (self.PLACE_PRIORITY.get(json.loads(row[2])["category"], 2),
                                           row[1], row[0]), reverse=True)
                db.executemany("DELETE FROM memories WHERE id=?", [(row[0],) for row in rows[limit:]])
                continue
            db.execute("DELETE FROM memories WHERE kind=? AND id NOT IN "
                       "(SELECT id FROM memories WHERE kind=? ORDER BY observed_at DESC,id DESC LIMIT ?)",
                       (kind, kind, limit))

    @staticmethod
    def _identity(*values):
        return hashlib.sha256(SurvivalMemory._json(values).encode()).hexdigest()

    def add_secret(self, value):
        if not isinstance(value, str) or not value:
            return
        with self._lock:
            self._secrets.add(value)
            with self._transaction() as db:
                for row_id, dimension, encoded in db.execute("SELECT id,dimension,payload FROM memories").fetchall():
                    db.execute("UPDATE memories SET dimension=?,payload=? WHERE id=?",
                               (self._redact(dimension), self._json(self._redact(json.loads(encoded))), row_id))
                lifecycle = self._lifecycle(db)
                if lifecycle:
                    self._save_lifecycle(db, self._redact(lifecycle))

    def record_episode(self, event, *, dimension=None, pos=None, data=None,
                       source="skill_verified", confidence=1.0, ttl_seconds=7 * 86400):
        event = self._identifier(event)
        if not event:
            raise ValueError("Memory event must be a short identifier")
        dimension = self._identifier(dimension)
        now = self._now()
        with self._transaction() as db:
            self._episode(db, event, dimension, self._pos(pos), self._facts(data), source, confidence, ttl_seconds, now)
            self._prune(db, now)

    def _episode(self, db, event, dimension, pos, data, source, confidence, ttl, now):
        # SQLite's row id avoids timestamp collisions when one tick yields two facts.
        next_id = db.execute("SELECT COALESCE(MAX(id),0)+1 FROM memories").fetchone()[0]
        identity = self._identity(now, next_id, event)
        self._write(db, "episodes", identity, dimension,
                    {"event": event, "pos": pos, "data": data}, source=source,
                    confidence=confidence, ttl_seconds=ttl, now=now)

    def remember_place(self, category, name, pos, *, dimension, source="api_verified",
                       confidence=0.85, ttl_seconds=86400, metadata=None):
        category, name, dimension = map(self._identifier, (category, name, dimension))
        pos = self._pos(pos)
        if not all((category, name, dimension)) or pos is None:
            raise ValueError("Memory place requires valid category, name, dimension and position")
        now = self._now()
        with self._transaction() as db:
            self._place(db, category, name, pos, dimension, source, confidence, ttl_seconds, metadata, now)
            self._prune(db, now)

    def _place(self, db, category, name, pos, dimension, source, confidence, ttl, metadata, now):
        cell = [math.floor(v) for v in pos]
        identity = self._identity(dimension, category, name, cell)
        if source == "api_verified":
            for row_id, encoded in db.execute("SELECT id,payload FROM memories WHERE kind='places' AND dimension=?", (dimension,)).fetchall():
                previous = json.loads(encoded)
                if (previous["pos"] == cell and previous["name"] != name
                        and previous["category"] in {"bed", "workstation", "storage", "resource", "hazard"}):
                    db.execute("DELETE FROM memories WHERE id=?", (row_id,))
        self._write(db, "places", identity, dimension,
                    {"category": category, "name": name, "pos": cell, "metadata": self._facts(metadata)},
                    source=source, confidence=confidence, ttl_seconds=ttl, now=now)

    def remember_failure(self, action, error_code, *, dimension, target=None,
                         strategy="reobserve", source="skill_verified", confidence=1.0,
                         ttl_seconds=300):
        action, error_code, dimension = map(self._identifier, (action, error_code, dimension))
        target = self._pos(target) or self._identifier(target)
        if not all((action, error_code, dimension)) or strategy not in self.STRATEGIES:
            raise ValueError("Failure memory requires valid structured identifiers")
        now = self._now()
        with self._transaction() as db:
            self._failure(db, action, error_code, dimension, target, strategy, source, confidence, ttl_seconds, now)
            self._prune(db, now)

    def _failure(self, db, action, code, dimension, target, strategy, source, confidence, ttl, now):
        identity = self._identity(dimension, action, target, code)
        old = db.execute("SELECT payload,expires_at FROM memories WHERE kind='strategies' AND identity=?", (identity,)).fetchone()
        count = min(999, json.loads(old[0]).get("count", 0) + 1) if old and old[1] > now else 1
        self._write(db, "strategies", identity, dimension,
                    {"action": action, "target": target, "error_code": code, "strategy": strategy, "count": count},
                    source=source, confidence=confidence, ttl_seconds=ttl, now=now)

    @staticmethod
    def _lifecycle(db):
        row = db.execute("SELECT value FROM meta WHERE key='lifecycle'").fetchone()
        return json.loads(row[0]) if row else {}

    def _save_lifecycle(self, db, value):
        db.execute("INSERT INTO meta VALUES ('lifecycle',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (self._json(value),))

    def observe(self, observation):
        """Learn only present, verified rows. Missing/partial rows never erase facts."""
        if not isinstance(observation, dict) or observation.get("fresh") is False:
            return
        status = observation.get("status")
        if not isinstance(status, dict):
            return
        dimension = self._identifier(status.get("dimension"))
        if not dimension:
            return
        now = self._now()
        with self._transaction() as db:
            old = self._lifecycle(db)
            alive = status.get("alive") if isinstance(status.get("alive"), bool) else old.get("alive")
            death = status.get("last_death")
            marker = None
            if isinstance(death, dict) and type(death.get("t")) in (int, float) and math.isfinite(death["t"]):
                marker = self._identity(death["t"], self._pos(death.get("pos")))
            death_changed = bool(old and marker and marker != old.get("death_marker") and not old.get("death_pending"))
            died = bool(old and ((alive is False and old.get("alive") is True) or death_changed))
            changed_dimension = bool(old.get("dimension") and old["dimension"] != dimension)
            if died or changed_dimension:
                # Old routes/failed targets must not dictate actions in a new life or dimension.
                db.execute("DELETE FROM memories WHERE kind='strategies'")
                db.execute("DELETE FROM memories WHERE kind='places' AND json_extract(payload,'$.category') "
                           "IN ('resource','hazard','safe_stand','dropped_items')")
                for row_id, encoded in db.execute("SELECT id,payload FROM memories WHERE kind='places'").fetchall():
                    payload = json.loads(encoded)
                    payload["confidence"] *= 0.5
                    payload["invalidated_by"] = "death" if died else "dimension_change"
                    db.execute("UPDATE memories SET confidence=?,payload=? WHERE id=?",
                               (payload["confidence"], self._json(payload), row_id))
                self._episode(db, "death_observed" if died else "dimension_changed", dimension,
                              self._pos(status.get("pos")), {"from_dimension": old.get("dimension"), "to_dimension": dimension},
                              "lifecycle", 1.0, 7 * 86400, now)
            self._save_lifecycle(db, {"dimension": dimension, "alive": alive,
                                     "death_marker": marker or old.get("death_marker"),
                                     "death_pending": bool(died and alive is False) or bool(old.get("death_pending") and alive is not True)})
            if status.get("ready") is True and alive is True:
                rows = observation.get("blocks")
                if isinstance(rows, list):
                    for row in rows[:128]:
                        if not isinstance(row, dict):
                            continue
                        name, pos = self._identifier(row.get("name")), self._pos(row.get("pos"))
                        if not name or pos is None:
                            continue
                        category, ttl = self._place_type(name)
                        if category:
                            self._place(db, category, name, pos, dimension, "api_verified", 0.85, ttl, row, now)
            self._prune(db, now)

    @staticmethod
    def _place_type(name):
        plain = name.removeprefix("minecraft:")
        if plain.endswith("_bed"):
            return "bed", 86400
        if plain in {"crafting_table", "furnace", "smoker", "blast_furnace"}:
            return "workstation", 86400
        if plain in {"chest", "trapped_chest", "barrel"}:
            return "storage", 86400
        if plain in {"lava", "fire", "soul_fire", "magma_block", "cactus", "powder_snow"}:
            return "hazard", 300
        if plain.endswith(("_log", "_ore")) or plain in {"stone", "cobblestone", "wheat", "carrots", "potatoes", "water"}:
            return "resource", 900
        return None, 0

    @staticmethod
    def _mapping(value):
        return asdict(value) if is_dataclass(value) else value if isinstance(value, dict) else {}

    def record_result(self, decision, result, observation=None):
        """Save the supplied result, not a guessed success or remembered inventory."""
        dec, result = self._mapping(decision), self._mapping(result)
        action = self._identifier(dec.get("name"))
        if not action or not isinstance(result.get("success"), bool):
            return
        if action in {"wait", "observe"} and result["success"]:
            return  # Routine polling is not an episode and must not evict useful history.
        observation = observation if isinstance(observation, dict) else {}
        status = observation.get("status") if isinstance(observation.get("status"), dict) else {}
        dimension = self._identifier(status.get("dimension"))
        args = dec.get("args") if isinstance(dec.get("args"), dict) else {}
        detail = result.get("data") if isinstance(result.get("data"), dict) else {}
        code = self._identifier(result.get("error_code"))
        moved = detail.get("moved")
        no_motion = action == "move_to" and (detail.get("already_in_range") is True
                    or moved is False or (type(moved) in (int, float) and moved <= 0))
        if no_motion:
            code = code or "no_movement"
        target = self._pos(args.get("pos")) or self._identifier(args.get("item", args.get("name")))
        facts = self._facts({**detail, "action": action, "success": result["success"],
                             "error_code": code, "target": target})
        # Common skill counters are historical evidence, never current inventory.
        for key, dest in (("before", "count_before"), ("after", "count_after")):
            if type(detail.get(key)) is int:
                facts[dest] = detail[key]
        now = self._now()
        with self._transaction() as db:
            self._episode(db, "skill_result", dimension, self._pos(status.get("pos")), facts,
                          "skill_verified", 1.0, 7 * 86400, now)
            if dimension and (not result["success"] or no_motion) and code and not code.startswith("llm_") and code != "cancelled":
                strategy = ("verify_before_retry" if code in {"dig_unconfirmed", "request_timeout", "internal"}
                            else "acquire_ingredients" if code == "missing_ingredients"
                            else "wait_for_safe_state" if code in {"unsafe", "safety_changed", "dead"}
                            else "choose_other_target" if code in {"stalled", "out_of_reach", "no_progress", "no_movement"}
                            else "reobserve")
                self._failure(db, action, code, dimension, target, strategy, "skill_verified", 1.0, 300, now)
            elif dimension and result["success"] and action not in {"wait", "observe"} and not no_motion:
                for row_id, encoded in db.execute("SELECT id,payload FROM memories WHERE kind='strategies' AND dimension=?", (dimension,)).fetchall():
                    previous = json.loads(encoded)
                    if previous["action"] == action and previous["target"] == target:
                        db.execute("DELETE FROM memories WHERE id=?", (row_id,))
            self._prune(db, now)

    def recall(self, observation, *, limit=8, max_bytes=6000):
        """Return deterministic, dimension-scoped candidates within total JSON bytes.

        No observation/unknown dimension returns no facts, not a previous position.
        Limit is the total number of rows, across all three categories.
        The budget counts ordinary json.dumps(..., ensure_ascii=False) output,
        including the result envelope. Smaller budgets raise without revealing data.
        """
        if type(limit) is not int or not 0 <= limit <= 64:
            raise ValueError("Recall limit must be an integer between zero and 64")
        empty = {kind: [] for kind in self.KINDS}
        size = lambda value: len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if type(max_bytes) is not int or not size(empty) <= max_bytes <= 65536:
            raise ValueError("Recall byte budget cannot hold the envelope or exceeds 65536")
        observation = observation if isinstance(observation, dict) else {}
        status = observation.get("status") if isinstance(observation.get("status"), dict) else {}
        dimension, origin = self._identifier(status.get("dimension")), self._pos(status.get("pos"))
        if not dimension or observation.get("fresh") is False or not limit:
            return empty
        now = self._now()
        with self._transaction() as db:
            self._prune(db, now)
            rows = [(kind, self._redact(json.loads(payload))) for kind, payload in db.execute(
                "SELECT kind,payload FROM memories WHERE dimension=? AND expires_at>? "
                "ORDER BY observed_at DESC,id DESC", (dimension, now))]
        def rank(row):
            kind, payload = row
            distance = math.dist(origin, payload["pos"]) if origin is not None and self._pos(payload.get("pos")) is not None else 0
            priority = self.PLACE_PRIORITY.get(payload.get("category"), 2) if kind == "places" else 0
            return (self.KINDS.index(kind), -priority, -payload["confidence"], distance, -payload["observed_at"])
        # Interleave categories so a full episode list cannot starve nearby places.
        groups = {kind: [] for kind in self.KINDS}
        for kind, payload in sorted(rows, key=rank):
            groups[kind].append(payload)
        result = {kind: [] for kind in self.KINDS}
        for index in range(max((len(v) for v in groups.values()), default=0)):
            for kind in ("places", "strategies", "episodes"):
                if len(result[kind]) >= limit or index >= len(groups[kind]):
                    continue
                result[kind].append(groups[kind][index])
                if size(result) > max_bytes:
                    result[kind].pop()
                elif sum(map(len, result.values())) >= limit:
                    return result
        return result

    def close(self):
        """Connections are transaction-local; close only rejects subsequent calls."""
        with self._lock:
            self._closed = True
