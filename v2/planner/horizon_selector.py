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

        # Competition mode: rank directly-achievable scoring tasks cleanly in natural progression order!
        if self.objective == "competition" and self.competition is not None:
            has_stone_tool = inv.get("stone_pickaxe", 0) > 0 or inv.get("iron_pickaxe", 0) > 0 or inv.get("diamond_pickaxe", 0) > 0
            has_iron_tool = inv.get("iron_pickaxe", 0) > 0 or inv.get("diamond_pickaxe", 0) > 0
            # Tasks whose current AcquireItem route does NOT by itself trigger the server advancement
            indirect_penalties = {
                "minecraft:husbandry/make_a_sign_glow": 600,
                "minecraft:husbandry/tactical_fishing": 600,
                "minecraft:adventure/sniper_duel": 500,
                "minecraft:adventure/ol_betsy": 500,
                "minecraft:adventure/shoot_arrow": 400,
                "minecraft:adventure/kill_a_mob": 350,
                "minecraft:story/deflect_arrow": 350,
                "minecraft:nether/ride_strider": 800,
                "minecraft:nether/charge_respawn_anchor": 800,
                "minecraft:nether/obtain_crying_obsidian": 800,
                "minecraft:nether/distract_piglin": 700,
                "minecraft:story/enchant_item": 600,
                "minecraft:adventure/crafters_crafting_crafters": 600,
            }
            for entry in self.competition.selectable_tasks()[:24]:
                route = self.catalogue.advancement_route(entry.id)
                if route is None:
                    continue
                kind, target = route
                if kind == "enter_nether":
                    if self.is_portal_gate_ready(bb):
                        est = 12
                    else:
                        est = (
                            self.catalogue.estimate_steps("iron_pickaxe", 1, inv)
                            + self.catalogue.estimate_steps("water_bucket", 1, inv)
                            + self.catalogue.estimate_steps("flint_and_steel", 1, inv)
                            + 16
                        )
                else:
                    est = max(1, self.catalogue.estimate_steps(target, 1, inv))
                eff_est = est + max(0, entry.difficulty - 1) * 40 + indirect_penalties.get(entry.id, 0)
                if not has_iron_tool and target in {"diamond", "diamond_chestplate", "obsidian", "enchanting_table", "diamond_hoe"}:
                    eff_est += 400
                if not has_stone_tool and target in {"iron_ingot", "iron_pickaxe", "iron_chestplate", "shield", "bucket", "water_bucket", "lava_bucket"}:
                    eff_est += 80
                tag = "PORTAL_GATE_READY" if (kind == "enter_nether" and self.is_portal_gate_ready(bb)) else f"comp_{entry.points}pt"
                candidates.append(HorizonCandidate(
                    goal="select_task",
                    item=entry.id,
                    count=1,
                    est_steps=max(1, eff_est),
                    priority_tag=tag,
                    points=entry.points,
                ))
            candidates.sort(key=lambda c: (
                0 if c.priority_tag == "PORTAL_GATE_READY" else 1,
                -(c.points / max(1, c.est_steps)),
                c.est_steps,
            ))
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
            if c.goal == "select_task":
                lines.append(f'{i}. select_task(task_id="{c.item}") est={c.est_steps} [{c.priority_tag}{extra}]')
            else:
                lines.append(f"{i}. {c.goal}({c.item}x{c.count}) est={c.est_steps} [{c.priority_tag}{extra}]")
        return "\n".join(lines)
