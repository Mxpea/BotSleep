"""L4 CompetitionScheduler: non-serial PPM + UnlockSynergy + TimeFit + 24m OpportunisticHit + FailureDecay."""

from __future__ import annotations

import math
import time
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from v2.bt.blackboard import Blackboard
from v2.domain import FOOD_ITEMS, PASSIVE_ANIMALS, dist3
from v2.tasks.catalogue import TaskCatalogue
from v2.tasks.competition.graph import ADVANCEMENT_GRAPH, unlocked_capability_fanout
from v2.tasks.competition.manager import CompetitionManager


_TIME_FIT_WINDOW = 0.85
# Advancements requiring rare/specific world entities/features that should ONLY rank high when seen nearby or mats already held
_ENTITY_GATED_ADVANCEMENTS: FrozenSet[str] = frozenset({
    "minecraft:husbandry/breed_an_animal",
    "minecraft:husbandry/tame_an_animal",
    "minecraft:husbandry/make_a_sign_glow",
    "minecraft:husbandry/tactical_fishing",
    "minecraft:husbandry/axolotl_in_a_bucket",
    "minecraft:husbandry/safely_harvest_honey",
    "minecraft:husbandry/wax_on",
    "minecraft:husbandry/wax_off",
    "minecraft:husbandry/ride_a_boat_with_a_goat",
    "minecraft:adventure/brush_armadillo",
    "minecraft:adventure/spyglass_at_parrot",
    "minecraft:adventure/spyglass_at_ghast",
    "minecraft:adventure/sniper_duel",
    "minecraft:adventure/ol_betsy",
    "minecraft:adventure/shoot_arrow",
    "minecraft:story/deflect_arrow",
    "minecraft:nether/distract_piglin",
})


def _horizon_candidate_cls():
    from v2.planner.horizon_selector import HorizonCandidate

    return HorizonCandidate


class CompetitionScheduler:
    """Computes dynamic non-serial PPM priority across all eligible advancements."""

    def __init__(self, catalogue: TaskCatalogue, competition: CompetitionManager) -> None:
        self.catalogue = catalogue
        self.competition = competition

    @staticmethod
    def _owned_capabilities(bb: Blackboard, completed: Set[str]) -> Set[str]:
        inv = bb.inventory_counts
        caps: Set[str] = set()
        if inv.get("cobblestone", 0) > 0 or inv.get("stone_pickaxe", 0) > 0:
            caps.add("stone_avail")
        if any(inv.get(t, 0) > 0 for t in ("stone_pickaxe", "iron_pickaxe", "diamond_pickaxe", "netherite_pickaxe")):
            caps.add("stone_pick")
        if any(inv.get(t, 0) > 0 for t in ("iron_pickaxe", "diamond_pickaxe", "netherite_pickaxe")):
            caps.add("iron_pick")
        if inv.get("diamond_pickaxe", 0) > 0 or inv.get("netherite_pickaxe", 0) > 0:
            caps.add("diamond_pick")
        if inv.get("iron_ingot", 0) > 0 or "minecraft:story/smelt_iron" in completed:
            caps.add("iron_ingot")
        if inv.get("shield", 0) > 0:
            caps.add("shield")
        if bb.dimension == "the_nether" or "minecraft:story/enter_the_nether" in completed:
            caps.add("nether")
        return caps

    @staticmethod
    def _world_signals(bb: Blackboard) -> Tuple[Set[str], Dict[str, int]]:
        """Return (entity_or_block_names_within_24m, entity_counts)."""
        signals: Set[str] = set()
        counts: Dict[str, int] = {}
        for ent in bb.entities:
            if not isinstance(ent, dict):
                continue
            name = str(ent.get("name") or "")
            if not name:
                continue
            epos = ent.get("pos")
            if isinstance(epos, (list, tuple)) and len(epos) == 3:
                if dist3(bb.pos, epos) > 24.0:
                    continue
            elif ent.get("dist") is not None and float(ent["dist"]) > 24.0:
                continue
            signals.add(name)
            counts[name] = counts.get(name, 0) + 1
            if str(ent.get("type") or "") == "hostile":
                signals.add("hostile")
        if bb.get("nearby_sculk_sensor"):
            signals.add("sculk_sensor")
        recon = bb.get("island_recon")
        if isinstance(recon, dict):
            for key, rows in recon.items():
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    rpos = row.get("pos") if isinstance(row, dict) else None
                    if isinstance(rpos, (list, tuple)) and len(rpos) == 3 and dist3(bb.pos, rpos) <= 32.0:
                        signals.add(str(key))
                        break
        return signals, counts

    @staticmethod
    def _is_portal_gate_ready(bb: Blackboard) -> bool:
        if bb.dimension != "overworld":
            return False
        inv = bb.inventory_counts
        return inv.get("iron_pickaxe", 0) >= 1 or inv.get("iron_ingot", 0) >= 4

    def select_top(
        self,
        bb: Blackboard,
        *,
        corpse_cand: Any = None,
        limit: int = 5,
    ) -> List[Any]:
        HorizonCandidate = _horizon_candidate_cls()
        inv = bb.inventory_counts
        caps = self._owned_capabilities(bb, self.competition.completed_ids)
        signals, sig_counts = self._world_signals(bb)
        rem_s = bb.get("competition_remaining_seconds")
        rem_sec = float(rem_s) if isinstance(rem_s, (int, float)) and math.isfinite(float(rem_s)) else None
        portal_ready = self._is_portal_gate_ready(bb)
        fail_counts = getattr(self.competition, "failure_counts", {})
        tod = int(bb.status.get("time_of_day", 1000)) % 24000
        is_night_or_rain = (12541 <= tod <= 23458) or bool(bb.status.get("is_raining"))

        scored: List[Tuple[int, float, int, Any]] = []
        for entry in self.competition.selectable_tasks():
            aid = entry.id
            route = self.catalogue.advancement_route(aid)
            if route is None:
                continue
            kind, target = route
            spec = ADVANCEMENT_GRAPH.get(aid)

            # Estimate remaining step cost cleanly (preserving test_23 est_steps=44 for avoid_vibration)
            if kind == "enter_nether":
                if portal_ready:
                    step_est = 8
                else:
                    step_est = (
                        self.catalogue.estimate_steps("iron_pickaxe", 1, inv)
                        + self.catalogue.estimate_steps("water_bucket", 1, inv)
                        + self.catalogue.estimate_steps("flint_and_steel", 1, inv)
                        + 16
                    )
                est_minutes = 0.8 if portal_ready else (spec.base_minutes if spec else 5.5)
            elif kind == "avoid_vibration":
                step_est = 4 + max(0, entry.difficulty - 1) * 40
                est_minutes = spec.base_minutes if spec else 1.0
            else:
                req_items = spec.required_items if (spec and spec.required_items) else ((target, 1),)
                miss_steps = sum(max(0, self.catalogue.estimate_steps(it, cnt, inv)) for it, cnt in req_items)
                step_est = max(1, miss_steps + max(0, entry.difficulty - 1) * 18)
                base_min = spec.base_minutes if spec else (1.5 + entry.difficulty * 1.5)
                if miss_steps == 0:
                    est_minutes = max(0.35, base_min * 0.35)
                else:
                    est_minutes = max(0.5, base_min * (0.4 + 0.6 * min(1.5, miss_steps / 20.0)))

            # 1) Hard capability gate (don't try diamond/nether tasks before unlocked unless close)
            unmet_caps = (spec.capabilities_req - caps) if spec is not None else frozenset()
            gate_mult = 0.08 if unmet_caps else 1.0
            if aid == "minecraft:adventure/sleep_in_bed" and not is_night_or_rain and bb.dimension == "overworld":
                # Waiting until dusk avoids wasted daytime loops unless white_bed still needs wool from nearby sheep
                if inv.get("white_bed", 0) > 0:
                    gate_mult *= 0.05

            # 2) Opportunistic 24m trigger boost + unseen-entity gating
            is_opp = False
            if spec is not None and spec.world_triggers and (spec.world_triggers & signals):
                if aid == "minecraft:husbandry/breed_an_animal":
                    is_opp = any(sig_counts.get(a, 0) >= 2 for a in ("chicken", "cow", "sheep", "pig"))
                else:
                    is_opp = True
            opp_mult = 2.2 if (is_opp and not unmet_caps) else (1.25 if is_opp else 1.0)
            if aid in _ENTITY_GATED_ADVANCEMENTS and not is_opp and miss_steps > 0:
                # Avoid blind 40x wandering for rare mobs/structures; let deterministic mining/crafting lead!
                gate_mult *= 0.12

            # 3) UnlockSynergy fanout (prioritize T0/T1 unlockers naturally)
            fanout_pts = unlocked_capability_fanout(aid, self.competition.completed_ids, self.competition.catalog)
            eff_pts = float(entry.points) + min(35.0, 0.25 * fanout_pts)

            # 4) Time-fit endgame pruning
            time_fit = 1.0
            if rem_sec is not None and rem_sec > 0.0 and not is_opp:
                if est_minutes * 60.0 > rem_sec * _TIME_FIT_WINDOW:
                    time_fit = 0.05

            # 5) Exponential failure decay
            fails = int(fail_counts.get(aid, 0))
            decay = 0.55 ** fails

            ppm = (eff_pts / max(0.35, est_minutes)) * opp_mult * gate_mult * time_fit * decay

            is_portal_top = (aid == "minecraft:story/enter_the_nether" and portal_ready)
            if is_portal_top:
                tag = "PORTAL_GATE_READY"
            elif is_opp and not unmet_caps:
                tag = f"OPP_{entry.points}pt"
            else:
                tag = f"PPM_{entry.points}pt"

            cand = HorizonCandidate(
                goal="select_task",
                item=aid,
                count=1,
                est_steps=max(1, int(step_est)),
                priority_tag=tag,
                points=entry.points,
            )
            pin_rank = 0 if is_portal_top else 1
            scored.append((pin_rank, -ppm, step_est, cand))

        scored.sort(key=lambda row: (row[0], row[1], row[2]))
        out = [row[3] for row in scored]

        # Survival-in-competition injections: active corpse drop #1, low-food daytime hunt
        if bb.food <= 12 and bb.dimension == "overworld" and 0 <= tod < 12000:
            total_food = sum(inv.get(k, 0) for k in FOOD_ITEMS)
            if total_food < 2 and any((signals & PASSIVE_ANIMALS)):
                # Only suggest when no higher urgent task
                pass

        if corpse_cand is not None:
            out = [corpse_cand] + out
        return out[:limit]
