"""L3b Competition rules loader faithfully enforcing 45 unconditional + 54 conditional overrides."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Dict, List, Mapping, Optional


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


@dataclass(frozen=True)
class CompetitionEntry:
    id: str
    title: str
    category: str
    difficulty: int
    points: int
    classification: str  # "unconditional" | "conditional" | "excluded"
    condition: str = ""
    is_root: bool = False


def load_competition_rules(path: Optional[Path] = None) -> List[CompetitionEntry]:
    rules_path = path or (_REPO_ROOT / "data" / "competition" / "rules_v2.json")
    raw = json.loads(rules_path.read_text(encoding="utf-8"))
    out: List[CompetitionEntry] = []
    for row in raw.get("entries", []):
        out.append(CompetitionEntry(
            id=str(row["id"]),
            title=str(row.get("title", row["id"])),
            category=str(row.get("category", "")),
            difficulty=int(row.get("difficulty", 1)),
            points=int(row.get("points", 0)),
            classification=str(row.get("classification", "excluded")),
            condition=str(row.get("condition", "")),
            is_root=bool(row.get("is_root", False)),
        ))
    return out


def scoring_catalog(
    eligibility_overrides: Optional[Mapping[str, bool]] = None,
    *,
    path: Optional[Path] = None,
) -> Dict[str, CompetitionEntry]:
    """Return ONLY currently eligible scoring advancements.

    - All 45 `unconditional` entries are eligible.
    - `conditional` entries (54) are ONLY eligible when explicitly set to True in `eligibility_overrides`.
    - `excluded` entries (26) are NEVER eligible; overriding an unknown or non-conditional ID raises ValueError.
    """
    entries = load_competition_rules(path=path)
    by_id = {e.id: e for e in entries}
    overrides = dict(eligibility_overrides or {})

    for adv_id, enabled in overrides.items():
        if type(enabled) is not bool or adv_id not in by_id or by_id[adv_id].classification != "conditional":
            raise ValueError(f"Invalid conditional eligibility override: {adv_id}")

    eligible: Dict[str, CompetitionEntry] = {}
    for e in entries:
        if e.classification == "unconditional":
            eligible[e.id] = e
        elif e.classification == "conditional" and overrides.get(e.id) is True:
            eligible[e.id] = e
    return eligible
