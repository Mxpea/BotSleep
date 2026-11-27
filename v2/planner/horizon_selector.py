"""L4 HorizonSelector: step-cost goal ranker with PORTAL_GATE_READY speedrun prioritization."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Dict, List, Optional

from v2.bt.blackboard import Blackboard
from v2.domain import FOOD_ITEMS, PASSIVE_ANIMALS, PLANK_ITEMS, dist3
from v2.tasks.catalogue import TaskCatalogue
from v2.tasks.competition.manager import CompetitionManager


_COOKABLE_FOOD_PAIRS = (
    ("beef", "cooked_beef"),
    ("porkchop", "cooked_porkchop"),
    ("mutton", "cooked_mutton"),
    ("chicken", "cooked_chicken"),
    ("potato", "baked_potato"),
)
_COOKED_FOOD_ITEMS = frozenset({
    "cooked_beef", "cooked_porkchop", "cooked_mutton", "cooked_chicken",
    "baked_potato", "cooked_salmon", "cooked_cod", "cooked_rabbit",
    "bread", "golden_carrot",
})


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

    @staticmethod
    def _has_item_or_equipped(bb: Blackboard, item: str) -> bool:
        if bb.inventory_counts.get(item, 0) > 0:
            return True
        armor = bb.inventory.get("armor") if isinstance(bb.inventory.get("armor"), dict) else {}
        for piece in armor.values():
            if isinstance(piece, dict) and piece.get("name") == item:
                return True
        offhand = bb.inventory.get("offhand")
        return isinstance(offhand, dict) and offhand.get("name") == item

    @staticmethod
    def _active_corpse_candidate(bb: Blackboard) -> Optional[HorizonCandidate]:
        drop = bb.get("active_corpse_drop")
        if (
            isinstance(drop, dict)
            and isinstance(drop.get("pos"), (list, tuple))
            and len(drop["pos"]) == 3
            and float(drop.get("loaded_elapsed", 0.0) or 0.0) < 300.0
        ):
            return HorizonCandidate("recover_corpse", "death_drop", 1, 1, "CORPSE_RECOVERY")
        return None

    @staticmethod
    def _daytime_food_hunt_candidate(
        bb: Blackboard,
        corpse_cand: Optional[HorizonCandidate],
    ) -> Optional[HorizonCandidate]:
        if corpse_cand is not None or bb.dimension != "overworld":
            return None
        raw_tod = bb.status.get("time_of_day")
        tod = (int(raw_tod) if raw_tod is not None else 1000) % 24000
        if not (0 <= tod < 12000):
            return None
        inv = bb.inventory_counts
        total_food = sum(inv.get(k, 0) for k in FOOD_ITEMS)
        if not (total_food < 4 or (bb.food <= 14 and total_food < 2)):
            return None
        for ent in bb.entities:
            if not isinstance(ent, dict) or str(ent.get("name") or "") not in PASSIVE_ANIMALS:
                continue
            epos = ent.get("pos")
            if isinstance(epos, (list, tuple)) and len(epos) == 3:
                if dist3(bb.pos, epos) <= 16.0:
                    return HorizonCandidate("hunt_food", "food", 4, 4, "FOOD_HUNT")
            elif ent.get("dist") is not None and float(ent["dist"]) <= 16.0:
                return HorizonCandidate("hunt_food", "food", 4, 4, "FOOD_HUNT")
        return None

    @staticmethod
    def _cook_food_candidate(
        bb: Blackboard,
        corpse_cand: Optional[HorizonCandidate],
    ) -> Optional[HorizonCandidate]:
        if corpse_cand is not None:
            return None
        inv = bb.inventory_counts
        cooked_have = sum(inv.get(k, 0) for k in _COOKED_FOOD_ITEMS)
        if cooked_have >= 4:
            return None
        has_fuel = (
            inv.get("coal", 0) > 0
            or inv.get("charcoal", 0) > 0
            or sum(inv.get(p, 0) for p in PLANK_ITEMS) >= 4
        )
        can_furnace = (
            inv.get("furnace", 0) > 0
            or bb.get("last_furnace_anchor") is not None
            or inv.get("cobblestone", 0) >= 8
        )
        if not (has_fuel and can_furnace):
            return None
        best_raw: Optional[tuple] = None
        for raw_name, cooked_name in _COOKABLE_FOOD_PAIRS:
            raw_cnt = inv.get(raw_name, 0)
            min_raw = 1 if bb.food <= 14 else 2
            if raw_cnt >= min_raw and (best_raw is None or raw_cnt > best_raw[0]):
                best_raw = (raw_cnt, cooked_name)
        if best_raw is None:
            return None
        raw_cnt, cooked_name = best_raw
        target_cnt = inv.get(cooked_name, 0) + min(raw_cnt, 8)
        est = 3 if (bb.food <= 14 and cooked_have == 0) else 5
        return HorizonCandidate("acquire", cooked_name, target_cnt, est, "COOK_FOOD")

    def select_top(self, bb: Blackboard, *, limit: int = 5) -> List[HorizonCandidate]:
        inv = bb.inventory_counts
        candidates: List[HorizonCandidate] = []
        corpse_cand = self._active_corpse_candidate(bb)

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
            for entry in self.competition.selectable_tasks():
                route = self.catalogue.advancement_route(entry.id)
                if route is None:
                    continue
                kind, target = route
                if kind == "enter_nether":
                    if self.is_portal_gate_ready(bb):
                        est = 8
                    else:
                        est = (
                            self.catalogue.estimate_steps("iron_pickaxe", 1, inv)
                            + self.catalogue.estimate_steps("water_bucket", 1, inv)
                            + self.catalogue.estimate_steps("flint_and_steel", 1, inv)
                            + 16
                        )
                elif kind == "avoid_vibration":
                    est = 4
                else:
                    est = max(1, self.catalogue.estimate_steps(target, 1, inv))
                eff_est = est + max(0, entry.difficulty - 1) * 40 + indirect_penalties.get(entry.id, 0)
                if not has_iron_tool and target in {"diamond", "diamond_chestplate", "obsidian", "enchanting_table", "diamond_hoe"}:
                    eff_est += 400
                if not has_stone_tool and target in {"iron_ingot", "iron_pickaxe", "iron_chestplate", "shield", "bucket", "water_bucket", "lava_bucket"}:
                    eff_est += 80
                tag = "PORTAL_GATE_READY" if (entry.id == "minecraft:story/enter_the_nether" and self.is_portal_gate_ready(bb)) else f"comp_{entry.points}pt"
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
            if corpse_cand is not None:
                candidates = [corpse_cand] + candidates
            return candidates[:limit]

        # Survival / speedrun progression ladder
        if corpse_cand is not None:
            candidates.append(corpse_cand)

        portal_ready = self.is_portal_gate_ready(bb)
        if portal_ready:
            # Pin EnterNetherTask at #1 with compact fast-track cost so small models don't drift into slow loops!
            est_portal = min(
                8,
                self.catalogue.estimate_steps("iron_pickaxe", 1, inv)
                + self.catalogue.estimate_steps("water_bucket", 1, inv)
                + self.catalogue.estimate_steps("flint_and_steel", 1, inv)
                + 4,
            )
            candidates.append(HorizonCandidate("enter_nether", "the_nether", 1, max(1, est_portal), "PORTAL_GATE_READY"))

        food_hunt = self._daytime_food_hunt_candidate(bb, corpse_cand)
        if food_hunt is not None:
            candidates.append(food_hunt)
        cook_cand = self._cook_food_candidate(bb, corpse_cand)
        if cook_cand is not None:
            candidates.append(cook_cand)

        if bb.get("nearby_sculk_sensor") and not bb.get("avoid_vibration_done"):
            candidates.append(HorizonCandidate("avoid_vibration", "sculk_sensor", 1, 2, "SNEAK_100"))
        if bb.get("nearby_loot_chest") and corpse_cand is None:
            candidates.append(HorizonCandidate("loot_chest", "chest", 1, 3, "LOOT_CHEST"))
        if bb.dimension == "the_nether" and not bb.get("nether_portal_sheltered") and corpse_cand is None:
            candidates.append(HorizonCandidate("setup_outpost", "nether_shelter", 1, 3, "NETHER_OUTPOST"))

        has_iron_pick = inv.get("iron_pickaxe", 0) > 0 or inv.get("diamond_pickaxe", 0) > 0
        has_stone_pick = has_iron_pick or inv.get("stone_pickaxe", 0) > 0
        has_melee_weapon = any(k.endswith(("_sword", "_axe")) and v > 0 for k, v in inv.items())
        iron_ingots = inv.get("iron_ingot", 0)
        has_water_bucket = self._has_item_or_equipped(bb, "water_bucket")
        has_iron_chest = self._has_item_or_equipped(bb, "iron_chestplate") or self._has_item_or_equipped(bb, "diamond_chestplate")
        milestones = [
            ("acquire", "wooden_pickaxe", 1, "tier1"),
            ("acquire", "stone_pickaxe", 1, "tier2"),
            ("acquire", "furnace", 1, "smelt_prep"),
            ("acquire", "stone_sword", 1, "defense_early"),
            ("acquire", "iron_pickaxe", 1, "tier3"),
            ("acquire", "water_bucket", 1, "portal_kit"),
            ("acquire", "flint_and_steel", 1, "portal_kit"),
            ("acquire", "shield", 1, "defense"),
            ("acquire", "iron_chestplate", 1, "armor_iron"),
            ("acquire", "iron_leggings", 1, "armor_iron"),
            ("acquire", "iron_helmet", 1, "armor_iron"),
            ("acquire", "iron_boots", 1, "armor_iron"),
            ("acquire", "golden_boots", 1, "nether_gold"),
            ("mine", "diamond", 3, "tier4_diamond"),
            ("acquire", "diamond_pickaxe", 1, "tier4_diamond"),
        ]
        for goal, item, cnt, tag in milestones:
            if item == "wooden_pickaxe" and (has_stone_pick or portal_ready):
                continue
            if item == "stone_pickaxe" and (has_iron_pick or portal_ready):
                continue
            if item == "furnace" and portal_ready:
                continue
            if item == "stone_sword" and has_melee_weapon:
                continue
            if item == "water_bucket" and not (has_iron_pick or iron_ingots >= 6 or inv.get("bucket", 0) >= 1):
                continue
            if item in {"flint_and_steel", "shield"} and not ((has_iron_pick and has_water_bucket) or iron_ingots >= 7):
                continue
            if item == "iron_chestplate" and not (has_iron_pick and (has_water_bucket or iron_ingots >= 8)):
                continue
            if item in {"iron_leggings", "iron_helmet", "iron_boots"} and not has_iron_chest:
                continue
            if item == "golden_boots" and not (has_iron_pick and (inv.get("gold_ingot", 0) >= 4 or inv.get("raw_gold", 0) >= 4)):
                continue
            deep_diamond_ready = (
                has_iron_pick
                and has_water_bucket
                and (inv.get("cobblestone", 0) + inv.get("cobbled_deepslate", 0) >= 16)
                and bb.health >= 14.0
            )
            if item == "diamond" and (
                not ((has_iron_pick and (has_iron_chest or bb.pos[1] <= 16.0)) or deep_diamond_ready)
                or inv.get("diamond_pickaxe", 0) > 0
            ):
                continue
            if item == "diamond_pickaxe" and (inv.get("diamond", 0) < 3 or inv.get("diamond_pickaxe", 0) > 0):
                continue
            already_have = self._has_item_or_equipped(bb, item) if cnt == 1 else False
            if not already_have and inv.get(item, 0) < cnt:
                est = self.catalogue.estimate_steps(item, cnt, inv)
                if item == "stone_sword":
                    if has_stone_pick:
                        est = max(1, min(est, 6))
                    else:
                        est = max(est, self.catalogue.estimate_steps("stone_pickaxe", 1, inv) + 1)
                candidates.append(HorizonCandidate(goal, item, cnt, est, tag))

        if bb.dimension == "the_nether":
            candidates.append(HorizonCandidate("explore", "nether_fortress", 1, 15, "nether_explore"))

        # Pin CORPSE_RECOVERY #1, then PORTAL_GATE_READY; sort remaining by fewest steps, demoting cooled-down failures
        corpse_pinned = [c for c in candidates if c.priority_tag == "CORPSE_RECOVERY"]
        portal_pinned = [c for c in candidates if c.priority_tag == "PORTAL_GATE_READY"]
        rest = [c for c in candidates if c.priority_tag not in {"CORPSE_RECOVERY", "PORTAL_GATE_READY"}]
        rest.sort(key=lambda c: c.est_steps)

        raw_cd = bb.get("failed_goal_cooldowns")
        cooldowns = raw_cd if isinstance(raw_cd, dict) else {}
        if cooldowns:
            now = time.monotonic()

            def _is_cooled(cand: HorizonCandidate) -> bool:
                for k in (f"{cand.goal}:{cand.item}x{cand.count}", f"{cand.goal}:{cand.item}", cand.goal):
                    exp = cooldowns.get(k)
                    if exp is not None and float(exp) > now:
                        return True
                return False

            fresh = [c for c in rest if not _is_cooled(c)]
            cooled = [c for c in rest if _is_cooled(c)]
            if cooled and not any(c.goal == "explore" for c in fresh):
                fresh.append(HorizonCandidate("explore", "surface", 1, 3, "COOLDOWN_EXPLORE"))
            rest = fresh + cooled

        return (corpse_pinned + portal_pinned + rest)[:limit]

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
