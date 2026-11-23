"""Read native damage events and retain a brief, same-life combat signal."""

import math
import threading
import time

from core.domain import RANGED_HOSTILES
from core.client import APIError


COMBAT_SOURCES = frozenset({"mob_attack", "mob_attack_no_aggro", "player_attack", "arrow", "trident", "sting",
                            "fireball", "unattributed_fireball", "sonic_boom", "wither_skull", "dragon_breath",
                            "player_explosion", "explosion", "mace_smash", "thorns"})
REACTION_SECONDS = 4


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


class CombatReaction:
    def __init__(self):
        self._lock = threading.Lock()
        self._token = None
        self._cursor = 0
        self._next_read = 0.0
        self._event = None
        self._life = None
        self._environment_hit = 0

    def observe(self, client, status, baseline=None):
        if status.get("alive") is not True or status.get("ready") is not True:
            return None
        token = getattr(client, "token", None)
        death = status.get("last_death") or {}
        raw_marker = death.get("t", 0) if isinstance(death, dict) else 0
        marker = raw_marker if _finite(raw_marker) else 0
        life = (status.get("dimension"), marker)
        now = time.time()
        with self._lock:
            if self._token != token:
                self._token, self._cursor, self._next_read = token, 0, 0.0
                self._event = None
            if self._life != life:
                self._life, self._event = life, None
                self._environment_hit = 0
            fetch = getattr(client, "get_events_page", None)
            if callable(fetch) and time.monotonic() >= self._next_read:
                self._next_read = time.monotonic() + .35
                try:
                    envelope = fetch(since=self._cursor, limit=500 if self._cursor == 0 else 120, timeout_seconds=.7)
                    now = time.time()
                    page = envelope.get("result") if isinstance(envelope, dict) else None
                    events = page.get("events") if isinstance(page, dict) else None
                    if isinstance(page, dict) and type(page.get("last_seq")) is int and page["last_seq"] < self._cursor:
                        self._cursor, self._event = 0, None
                    for event in events if isinstance(events, list) else ():
                        if not isinstance(event, dict):
                            continue
                        sequence, stamp = event.get("seq"), event.get("t")
                        if type(sequence) is not int or sequence <= self._cursor:
                            continue
                        self._cursor = sequence
                        if event.get("type") in {"death", "spawn"}:
                            self._event = None
                        if (event.get("type") != "hurt" or not _finite(stamp) or stamp < marker
                                or not 0 <= now - stamp / 1000 <= REACTION_SECONDS):
                            continue
                        source = event.get("source")
                        if isinstance(source, str) and source in COMBAT_SOURCES:
                            attacker = event.get("source_entity") or event.get("likely_attacker")
                            self._event = {"t": stamp, "source": source,
                                           "attacker": attacker if isinstance(attacker, dict) else {}}
                        elif isinstance(source, str) and not source.startswith("#"):
                            self._environment_hit = stamp
                except APIError:
                    pass  # A missing event page leaves the health/hostile fallback available.
            if self._event and now - self._event["t"] / 1000 <= REACTION_SECONDS:
                return dict(self._event)
            if now - self._environment_hit / 1000 <= REACTION_SECONDS:
                return None
            first = baseline or {}
            attacker = status.get("nearest_hostile") or {}
            gap = attacker.get("dist")
            if (_finite(gap) and gap <= (16 if attacker.get("name") in RANGED_HOSTILES else 8)
                    and first.get("dimension") == life[0] and first.get("last_death") == status.get("last_death")
                    and _finite(first.get("health")) and _finite(status.get("health"))
                    and status["health"] < first["health"]):
                self._event = {"t": int(now * 1000), "source": "health_drop_near_hostile", "attacker": attacker}
                return dict(self._event)
            return None
