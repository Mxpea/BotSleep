"""Read the competition draft catalog without observing or controlling Minecraft."""

import json
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any


DEFAULT_RULES_PATH = Path(__file__).resolve().parent.parent / "data" / "competition" / "rules_v2.json"
DIFFICULTY_POINTS = {"1": 5, "2": 15, "3": 30, "4": 60, "5": 100}
_CATEGORIES = {"story", "nether", "end", "adventure", "husbandry"}


def _fail(message: str) -> None:
    raise ValueError(f"Invalid competition rules: {message}")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be nonempty text")
    return value


def _date(value: Any, label: str) -> None:
    if not isinstance(value, str):
        _fail(f"{label} must be an ISO date")
    try:
        if date.fromisoformat(value).isoformat() != value:
            _fail(f"{label} must be an ISO date")
    except ValueError:
        _fail(f"{label} must be an ISO date")


def _object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _constant(value: str) -> None:
    _fail(f"non-finite JSON number {value}")


def _validate(catalog: Any) -> None:
    if not isinstance(catalog, dict):
        _fail("catalog must be an object")
    if type(catalog.get("schema_version")) is not int or catalog["schema_version"] != 1:
        _fail("schema_version must be 1")
    _text(catalog.get("version"), "version")
    if catalog.get("status") not in ("draft", "final"):
        _fail("status must be draft or final")
    _date(catalog.get("date"), "date")
    if catalog.get("date_kind") != "catalog_snapshot":
        _fail("date_kind must be catalog_snapshot")
    source = catalog.get("source")
    if not isinstance(source, dict):
        _fail("source must be an object")
    document = _text(source.get("document"), "source.document")
    if "\\" in document or ":" in document or document.startswith("/") or ".." in document.split("/"):
        _fail("source.document must be a portable relative path")
    _text(source.get("title"), "source.title")
    if source.get("version") != catalog["version"] or source.get("status") != catalog["status"]:
        _fail("source version/status must match catalog")
    if source.get("date") is not None:
        _date(source["date"], "source.date")
    else:
        _text(source.get("date_note"), "source.date_note")
    digest = source.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        _fail("source.sha256 must be a lowercase SHA256 digest")
    levels = catalog.get("difficulty_points")
    if not isinstance(levels, dict) or levels != DIFFICULTY_POINTS or any(type(v) is not int for v in levels.values()):
        _fail("difficulty_points must map 1..5 to 5/15/30/60/100")
    entries = catalog.get("entries")
    if not isinstance(entries, list) or not entries:
        _fail("entries must be a nonempty list")
    seen = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            _fail(f"entry {index} must be an object")
        ident = _text(entry.get("id"), f"entry {index} id")
        if ident in seen:
            _fail(f"duplicate advancement ID {ident}")
        seen.add(ident)
        category = entry.get("category")
        if not isinstance(category, str) or category not in _CATEGORIES:
            _fail(f"{ident}: invalid category")
        if not re.fullmatch(rf"minecraft:{category}/[a-z0-9_]+", ident):
            _fail(f"{ident}: invalid advancement ID")
        _text(entry.get("title"), f"{ident} title")
        condition = _text(entry.get("condition"), f"{ident} condition")
        is_root = entry.get("is_root")
        if type(is_root) is not bool or is_root != (ident == f"minecraft:{category}/root"):
            _fail(f"{ident}: invalid is_root")
        difficulty, points = entry.get("difficulty"), entry.get("points")
        if type(difficulty) is not int or type(points) is not int:
            _fail(f"{ident}: difficulty and points must be integers")
        classification = entry.get("classification")
        if classification not in ("unconditional", "conditional", "excluded"):
            _fail(f"{ident}: invalid classification")
        if is_root:
            if (difficulty, points, entry.get("source_points"), classification) != (0, 0, "0", "excluded"):
                _fail(f"{ident}: roots must be excluded with difficulty/points 0")
            continue
        if str(difficulty) not in DIFFICULTY_POINTS:
            _fail(f"{ident}: difficulty must be 1..5")
        if classification == "excluded":
            if points != 0 or entry.get("source_points") != "—" or not condition.startswith("不计分"):
                _fail(f"{ident}: excluded entries must preserve the exclusion and have points 0")
        else:
            if points != DIFFICULTY_POINTS[str(difficulty)] or entry.get("source_points") != str(points):
                _fail(f"{ident}: points do not match difficulty or original points")
            if classification == "unconditional" and condition != "计分":
                _fail(f"{ident}: unconditional condition must be 计分")
            if classification == "conditional" and not (condition.startswith("岛上有") and condition.endswith("时计分")):
                _fail(f"{ident}: conditional condition must preserve 岛上有…时计分")


def load_rules(path: str | Path | None = None) -> dict:
    """Return a fresh validated catalog; malformed or unreadable rules raise ValueError."""
    try:
        rules_path = DEFAULT_RULES_PATH if path is None else Path(path)
        catalog = json.loads(rules_path.read_text(encoding="utf-8-sig"), object_pairs_hook=_object, parse_constant=_constant)
        json.dumps(catalog, allow_nan=False)
    except (OSError, UnicodeError, TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"Cannot load competition rules: {exc}") from exc
    _validate(catalog)
    return catalog


def scoring_catalog(overrides: Mapping[str, bool] | None = None) -> list[dict]:
    """Return all rows with eligibility; only explicit True enables pending rules.

    Overrides map full advancement IDs to booleans. False disables a rule;
    True on an excluded rule (including a root) raises ValueError. Unknown IDs
    and non-boolean values also raise ValueError. No observations are accepted.
    Eligibility describes scoring permission, not completion or tool capability.
    """
    catalog = load_rules()
    entries = catalog["entries"]
    by_id = {entry["id"]: entry for entry in entries}
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, Mapping):
        raise ValueError("overrides must map advancement IDs to booleans")
    for ident, enabled in overrides.items():
        if not isinstance(ident, str) or ident not in by_id:
            raise ValueError(f"Unknown advancement override: {ident!r}")
        if type(enabled) is not bool:
            raise ValueError(f"Override for {ident} must be a boolean")
        if enabled and by_id[ident]["classification"] == "excluded":
            raise ValueError(f"Cannot enable excluded advancement: {ident}")
    rows = []
    for entry in entries:
        classification = entry["classification"]
        enabled = overrides.get(entry["id"])
        if classification == "excluded" or enabled is False:
            eligibility = "excluded"
        elif classification == "conditional" and enabled is not True:
            eligibility = "pending"
        else:
            eligibility = "scored"
        rows.append({**entry, "eligibility": eligibility})
    return rows
