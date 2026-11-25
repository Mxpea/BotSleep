"""L4 HorizonSelector: step-cost goal ranker with PORTAL_GATE_READY speedrun prioritization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from v2.bt.blackboard import Blackboard
from v2.tasks.catalogue import TaskCatalogue
from v2.tasks.competition.manager import CompetitionManager


@dataclass(frozen=True)
class HorizonCandidate:
    goal: str
    item: str
    count: int
    est_steps: int
    priority_tag: str
    points: int = 0


class HorizonSelector:
    """Ranks top-5 feasible next goals using TaskCatalogue step estimates + speedrun portal gate."""

    def __init__(
        self,
        catalogue: TaskCatalogue,
        *,
        objective: str = "daily",
        intent: str = "survival",
        competition: Optional[CompetitionManager] = None,
    ) -> None:
        self.catalogue = catalogue
        self.objective = objective
        self.intent = intent
        self.competition = competition

    def is_portal_gate_ready(self, bb: Blackboard) -> bool:
        """Speedrun gate: once iron_pickaxe >= 1 or iron_ingot >= 4 in overworld, prioritize EnterNetherTask."""
        if bb.dimension != "overworld":
            return False
        inv = bb.inventory_counts
        return inv.get("iron_pickaxe", 0) >= 1 or inv.get("iron_ingot", 0) >= 4

    def select_top(self, bb: Blackboard, *, limit: int = 5) -> List[HorizonCandidate]:
        inv = bb.inventory_counts
        candidates: List[HorizonCandidate] = []

        # Competition mode: rank selectable scoring tasks by points / est_steps!
        if self.objective == "competition" and self.competition is not None:
            for entry in self.competition.selectable_tasks()[:12]:
                route = self.catalogue.advancement_route(entry.id)
                if route is None:
                    continue
                kind, target = route
                est = 12 if kind == "enter_nether" else max(1, self.catalogue.estimate_steps(target, 1, inv))
                candidates.append(HorizonCandidate(
                    goal="select_task",
                    item=entry.id,
                    count=1,
                    est_steps=est,
                    priority_tag=f"comp_{entry.points}pt",
                    points=entry.points,
                ))
            candidates.sort(key=lambda c: (-(c.points / max(1, c.est_steps)), c.est_steps))
            return candidates[:limit]

        # Survival / speedrun progression ladder
        if self.is_portal_gate_ready(bb):
            # Pin EnterNetherTask at #1 so small models don't drift into redundant wood/axe loops!
            est_portal = (
                self.catalogue.estimate_steps("iron_pickaxe", 1, inv)
                + self.catalogue.estimate_steps("water_bucket", 1, inv)
                + self.catalogue.estimate_steps("flint_and_steel", 1, inv)
                + 8
            )
            candidates.append(HorizonCandidate("enter_nether", "the_nether", 1, est_portal, "PORTAL_GATE_READY"))

        milestones = [
            ("acquire", "wooden_pickaxe", 1, "tier1"),
            ("acquire", "stone_pickaxe", 1, "tier2"),
            ("acquire", "furnace", 1, "smelt_prep"),
            ("acquire", "iron_pickaxe", 1, "tier3"),
            ("acquire", "water_bucket", 1, "portal_kit"),
            ("acquire", "flint_and_steel", 1, "portal_kit"),
            ("acquire", "shield", 1, "defense"),
        ]
        for goal, item, cnt, tag in milestones:
            if inv.get(item, 0) < cnt:
                est = self.catalogue.estimate_steps(item, cnt, inv)
                candidates.append(HorizonCandidate(goal, item, cnt, est, tag))

        if bb.dimension == "the_nether":
            candidates.append(HorizonCandidate("explore", "nether_fortress", 1, 15, "nether_explore"))

        # Keep PORTAL_GATE_READY pinned #1 if present; sort remaining by fewest steps
        pinned = [c for c in candidates if c.priority_tag == "PORTAL_GATE_READY"]
        rest = [c for c in candidates if c.priority_tag != "PORTAL_GATE_READY"]
        rest.sort(key=lambda c: c.est_steps)
        return (pinned + rest)[:limit]

    def format_top(self, bb: Blackboard, *, limit: int = 5) -> str:
        top = self.select_top(bb, limit=limit)
        if not top:
            return "CANDIDATES: (none)"
        lines = ["CANDIDATES:"]
        for i, c in enumerate(top, 1):
            extra = f" pts={c.points}" if c.points else ""
            lines.append(f"{i}. {c.goal}({c.item}x{c.count}) est={c.est_steps} [{c.priority_tag}{extra}]")
        return "\n".join(lines)
