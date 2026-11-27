"""L3b 6 compact reusable competition advancement plugins (single write/tick, pure-memory stop)."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from v2.bt.blackboard import Blackboard
from v2.domain import AIR_BLOCKS, BED_BLOCKS, Coord3, NON_SOLID_BLOCKS, block_coord, dist3, pick_best_item
from v2.primitives.base import ActionOutcome, PrimitiveContext
from v2.primitives.combat import CombatPrimitive
from v2.primitives.dig_collect import DigCollectPrimitive
from v2.primitives.navigate import NavigatePrimitive
from v2.primitives.place_build import PlaceBuildPrimitive
from v2.tasks.acquire import AcquireItemTask
from v2.tasks.base import Task
from v2.tasks.catalogue import TaskCatalogue


def _as_coord3(raw: Any) -> Optional[Coord3]:
    if isinstance(raw, (list, tuple)) and len(raw) == 3:
        return (int(math.floor(float(raw[0]))), int(math.floor(float(raw[1]))), int(math.floor(float(raw[2]))))
    return None


class _AdvPluginBase(Task):
    """Shared helper for acquiring missing items, approaching targets, and bounding retries."""

    def __init__(self, ctx: PrimitiveContext, catalogue: TaskCatalogue, target: str, *, max_steps: int = 40) -> None:
        super().__init__(f"adv:{target}")
        self.ctx = ctx
        self.catalogue = catalogue
        self.target = target
        self.max_steps = int(max_steps)
        self._nav = NavigatePrimitive(ctx)
        self._place = PlaceBuildPrimitive(ctx)
        self._dig = DigCollectPrimitive(ctx)
        self._combat = CombatPrimitive(ctx)
        self._steps: int = 0
        self._placed_pos: Optional[Coord3] = None
        self._completed: bool = False

    def stop(self, bb: Blackboard) -> None:
        self._steps = 0
        self._placed_pos = None
        self._completed = False
        super().stop(bb)

    def is_finished(self, bb: Blackboard) -> bool:
        return self._completed

    def on_tick(self, bb: Blackboard) -> str:
        res = self.step(bb)
        if res == "COMPLETED":
            self._completed = True
        return res

    def step(self, bb: Blackboard) -> str:
        return "NEEDS_AI"

    def _ensure_items(self, bb: Blackboard, reqs: Sequence[Tuple[str, int]]) -> Optional[str]:
        for item, need in reqs:
            if bb.inventory_counts.get(item, 0) < need:
                return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, item, need))
        return None

    def _ensure_placed_block(self, bb: Blackboard, block_name: str, *, radius: int = 12) -> Tuple[Optional[Coord3], Optional[str]]:
        if self._placed_pos is not None:
            ent = self.ctx.gateway.get_block(bb.dimension, self._placed_pos, allow_fetch=False)
            if ent is not None and ent.name == block_name:
                return self._placed_pos, None
        rows = self.ctx.gateway.scan_blocks(bb.dimension, [block_name], radius=radius, limit=4)
        if rows:
            c = _as_coord3(rows[0].get("pos"))
            if c is not None:
                self._placed_pos = c
                return c, None
        if bb.inventory_counts.get(block_name, 0) < 1:
            return None, self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, block_name, 1))
        out = self._place.place_near_player(bb, block_name)
        if out.status == "UNKNOWN":
            return None, "UNKNOWN"
        if out.status == "OK":
            c = _as_coord3((out.details or {}).get("pos"))
            if c is not None:
                self._placed_pos = c
            return c, "RUNNING"
        return None, "RUNNING"

    def _interact_block_step(self, bb: Blackboard, pos: Coord3, item: Optional[str] = None) -> ActionOutcome:
        center = (pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5)
        if dist3(bb.pos, center) > 4.2:
            nav = self._nav.step_to(bb, center, tolerance=3.0)
            return ActionOutcome("PROGRESSED", "navigate", reason=nav.reason) if nav.status == "OK" else nav
        return self.ctx.execute_write(
            bb,
            "interact",
            {"pos": list(pos), **({"item": item} if item else {})},
            lambda: self.ctx.client.interact(pos=list(pos), item=item),
        )

    def _interact_entity_step(self, bb: Blackboard, eid: int, epos: Sequence[float], item: Optional[str] = None) -> ActionOutcome:
        if dist3(bb.pos, epos) > 3.5:
            nav = self._nav.step_to(bb, epos, tolerance=2.6)
            return ActionOutcome("PROGRESSED", "navigate", reason=nav.reason) if nav.status == "OK" else nav
        return self.ctx.execute_write(
            bb,
            "interact",
            {"entity_id": eid, **({"item": item} if item else {})},
            lambda: self.ctx.client.interact(entity_id=eid, item=item),
        )

    def _place_at_checked(self, bb: Blackboard, item: str, coord: Coord3) -> ActionOutcome:
        out = self._place.place_at(bb, item, coord)
        if out.status == "OK" and out.action != "place":
            return ActionOutcome("PROGRESSED", out.action, reason=out.reason, details=out.details)
        return out


class InteractBlockAdvTask(_AdvPluginBase):
    """Plugin 1: sleep_in_bed, plant_seed, make_a_sign_glow, use_lodestone, wax_on/off, honey, bookshelf, anchor."""

    def __init__(self, ctx: PrimitiveContext, catalogue: TaskCatalogue, target: str) -> None:
        super().__init__(ctx, catalogue, target, max_steps=36)
        self._farmland_pos: Optional[Coord3] = None
        self._charges: int = 0

    def stop(self, bb: Blackboard) -> None:
        self._farmland_pos = None
        self._charges = 0
        super().stop(bb)

    def _step_plant_seed(self, bb: Blackboard) -> str:
        seed = next((s for s in ("wheat_seeds", "carrot", "potato", "beetroot_seeds") if bb.inventory_counts.get(s, 0) >= 1), None)
        if seed is None:
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "wheat_seeds", 1))
        if self._farmland_pos is None:
            farm = self.ctx.gateway.scan_blocks(bb.dimension, ["farmland"], radius=16, limit=4)
            if farm:
                self._farmland_pos = _as_coord3(farm[0].get("pos"))
        if self._farmland_pos is not None:
            above = self.ctx.gateway.get_block(bb.dimension, (self._farmland_pos[0], self._farmland_pos[1] + 1, self._farmland_pos[2]), allow_fetch=True)
            if above is not None and above.name in AIR_BLOCKS:
                out = self._interact_block_step(bb, self._farmland_pos, item=seed)
                if out.status == "UNKNOWN":
                    return "UNKNOWN"
                if out.status == "OK":
                    return "COMPLETED"
                return out.status
            self._farmland_pos = None

        hoe = next((h for h in ("diamond_hoe", "iron_hoe", "stone_hoe", "wooden_hoe") if bb.inventory_counts.get(h, 0) >= 1), None)
        if hoe is None:
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "wooden_hoe", 1))
        soils = self.ctx.gateway.scan_blocks(bb.dimension, ["dirt", "grass_block"], radius=16, limit=12)
        for row in soils:
            c = _as_coord3(row.get("pos"))
            if c is None:
                continue
            above = self.ctx.gateway.get_block(bb.dimension, (c[0], c[1] + 1, c[2]), allow_fetch=True)
            if above is not None and above.name in AIR_BLOCKS:
                out = self._interact_block_step(bb, c, item=hoe)
                if out.status == "UNKNOWN":
                    return "UNKNOWN"
                if out.status == "OK":
                    self.ctx.gateway.write_through_place(bb.dimension, c, "farmland")
                    self._farmland_pos = c
                    return "RUNNING"
                return "RUNNING"
        bb.last_failure_reason = "plant_seed:no_soil"
        return "NEEDS_AI"

    def step(self, bb: Blackboard) -> str:
        self._steps += 1
        if self._steps > self.max_steps:
            bb.last_failure_reason = f"adv_timeout:{self.target}"
            return "NEEDS_AI"

        if self.target == "plant_seed":
            return self._step_plant_seed(bb)

        if self.target == "sleep_in_bed":
            bed_item = pick_best_item(bb.inventory_counts, BED_BLOCKS)
            placed_beds = self.ctx.gateway.scan_blocks(bb.dimension, list(BED_BLOCKS), radius=12, limit=2)
            if bed_item is None and not placed_beds:
                return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "white_bed", 1))
            if bb.dimension != "overworld":
                bb.last_failure_reason = "sleep_in_bed:not_overworld"
                return "NEEDS_AI"
            tod = int(bb.status.get("time_of_day", 1000)) % 24000
            if not (12541 <= tod <= 23458 or bool(bb.status.get("is_raining"))):
                # Bed crafted/secured; defer cleanly until dusk so bot never wastes daytime looping on bed!
                bb.last_failure_reason = "sleep_in_bed:wait_night"
                return "NEEDS_AI"
            pos = _as_coord3(placed_beds[0].get("pos")) if placed_beds else None
            if pos is None:
                pos, st = self._ensure_placed_block(bb, bed_item or "white_bed")
                if st is not None or pos is None:
                    return st or "RUNNING"
            out = self._interact_block_step(bb, pos)
            if out.status == "UNKNOWN":
                return "UNKNOWN"
            return "COMPLETED" if out.status == "OK" else "RUNNING"

        if self.target == "make_a_sign_glow":
            child = self._ensure_items(bb, (("glow_ink_sac", 1),))
            if child:
                return child
            sign_item = next((k for k, v in bb.inventory_counts.items() if k.endswith("_sign") and not k.endswith("_hanging_sign") and v > 0), "oak_sign")
            pos, st = self._ensure_placed_block(bb, sign_item)
            if st is not None or pos is None:
                return st or "RUNNING"
            out = self._interact_block_step(bb, pos, item="glow_ink_sac")
            return "UNKNOWN" if out.status == "UNKNOWN" else ("COMPLETED" if out.status == "OK" else "RUNNING")

        if self.target == "use_lodestone":
            child = self._ensure_items(bb, (("compass", 1),))
            if child:
                return child
            pos, st = self._ensure_placed_block(bb, "lodestone")
            if st is not None or pos is None:
                return st or "RUNNING"
            out = self._interact_block_step(bb, pos, item="compass")
            return "UNKNOWN" if out.status == "UNKNOWN" else ("COMPLETED" if out.status == "OK" else "RUNNING")

        if self.target == "wax_on":
            child = self._ensure_items(bb, (("honeycomb", 1),))
            if child:
                return child
            pos, st = self._ensure_placed_block(bb, "copper_block")
            if st is not None or pos is None:
                return st or "RUNNING"
            out = self._interact_block_step(bb, pos, item="honeycomb")
            return "UNKNOWN" if out.status == "UNKNOWN" else ("COMPLETED" if out.status == "OK" else "RUNNING")

        if self.target == "wax_off":
            axe = next((a for a in ("diamond_axe", "iron_axe", "stone_axe", "wooden_axe") if bb.inventory_counts.get(a, 0) > 0), None)
            if axe is None:
                return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "stone_axe", 1))
            rows = self.ctx.gateway.scan_blocks(bb.dimension, ["waxed_copper_block", "waxed_cut_copper"], radius=16, limit=4)
            if not rows:
                bb.last_failure_reason = "wax_off:no_waxed_copper"
                return "NEEDS_AI"
            c = _as_coord3(rows[0].get("pos"))
            if c is None:
                return "NEEDS_AI"
            out = self._interact_block_step(bb, c, item=axe)
            return "UNKNOWN" if out.status == "UNKNOWN" else ("COMPLETED" if out.status == "OK" else "RUNNING")

        if self.target == "safely_harvest_honey":
            child = self._ensure_items(bb, (("campfire", 1), ("glass_bottle", 1)))
            if child:
                return child
            nests = self.ctx.gateway.scan_blocks(bb.dimension, ["bee_nest", "beehive"], radius=24, limit=4)
            if not nests:
                bb.last_failure_reason = "honey:no_nest"
                return "NEEDS_AI"
            c = _as_coord3(nests[0].get("pos"))
            if c is None:
                return "NEEDS_AI"
            camp = self.ctx.gateway.get_block(bb.dimension, (c[0], c[1] - 1, c[2]), allow_fetch=True)
            if camp is None or camp.name != "campfire":
                out = self._place.place_at(bb, "campfire", (c[0], c[1] - 1, c[2]))
                return "UNKNOWN" if out.status == "UNKNOWN" else "RUNNING"
            out = self._interact_block_step(bb, c, item="glass_bottle")
            return "UNKNOWN" if out.status == "UNKNOWN" else ("COMPLETED" if out.status == "OK" else "RUNNING")

        if self.target == "charge_respawn_anchor":
            child = self._ensure_items(bb, (("glowstone", 1),))
            if child:
                return child
            pos, st = self._ensure_placed_block(bb, "respawn_anchor")
            if st is not None or pos is None:
                return st or "RUNNING"
            out = self._interact_block_step(bb, pos, item="glowstone")
            return "UNKNOWN" if out.status == "UNKNOWN" else ("COMPLETED" if out.status == "OK" else "RUNNING")

        if self.target == "read_power_of_chiseled_bookshelf":
            child = self._ensure_items(bb, (("book", 1), ("comparator", 1)))
            if child:
                return child
            pos, st = self._ensure_placed_block(bb, "chiseled_bookshelf")
            if st is not None or pos is None:
                return st or "RUNNING"
            if bb.inventory_counts.get("book", 0) > 0:
                out = self._interact_block_step(bb, pos, item="book")
                return "UNKNOWN" if out.status == "UNKNOWN" else "RUNNING"
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                adj = (pos[0] + dx, pos[1], pos[2] + dz)
                ent = self.ctx.gateway.get_block(bb.dimension, adj, allow_fetch=True)
                if ent is not None and ent.name in AIR_BLOCKS:
                    out = self._place.place_at(bb, "comparator", adj)
                    return "UNKNOWN" if out.status == "UNKNOWN" else ("COMPLETED" if out.status == "OK" else "RUNNING")
            return "NEEDS_AI"

        bb.last_failure_reason = f"unknown_interact_block:{self.target}"
        return "NEEDS_AI"


class InteractEntityAdvTask(_AdvPluginBase):
    """Plugin 2: breed_an_animal, tame_an_animal, tactical_fishing, axolotl_in_a_bucket, distract_piglin, brush_armadillo, ride_a_boat_with_a_goat."""

    _BREED_SPECIES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
        ("chicken", ("wheat_seeds", "melon_seeds", "pumpkin_seeds", "beetroot_seeds")),
        ("cow", ("wheat",)),
        ("sheep", ("wheat",)),
        ("pig", ("carrot", "potato", "beetroot")),
    )

    def __init__(self, ctx: PrimitiveContext, catalogue: TaskCatalogue, target: str) -> None:
        super().__init__(ctx, catalogue, target, max_steps=34)
        self._fed_ids: Set[int] = set()
        self._tries: int = 0

    def stop(self, bb: Blackboard) -> None:
        self._fed_ids.clear()
        self._tries = 0
        super().stop(bb)

    def _step_breed(self, bb: Blackboard) -> str:
        # Pick species where >= 2 entities exist nearby; fallback to chicken+wheat_seeds
        chosen_species: Optional[str] = None
        chosen_food: str = "wheat_seeds"
        matched: List[dict] = []
        for sp, foods in self._BREED_SPECIES:
            rows = [e for e in bb.entities if str(e.get("name") or "") == sp and isinstance(e.get("pos"), list)]
            if len(rows) >= 2:
                chosen_species = sp
                owned_food = next((f for f in foods if bb.inventory_counts.get(f, 0) > 0), foods[0])
                chosen_food = owned_food
                matched = sorted(rows, key=lambda e: dist3(bb.pos, e["pos"]))
                break
        if chosen_species is None:
            bb.last_failure_reason = "breed:need_two_matching_animals"
            return "NEEDS_AI"
        need_food = max(0, 2 - len(self._fed_ids))
        if bb.inventory_counts.get(chosen_food, 0) < need_food:
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, chosen_food, max(2, need_food)))
        for ent in matched:
            eid = int(ent.get("id", -1))
            if eid in self._fed_ids:
                continue
            out = self._interact_entity_step(bb, eid, ent["pos"], item=chosen_food)
            if out.status == "UNKNOWN":
                return "UNKNOWN"
            if out.status == "OK":
                self._fed_ids.add(eid)
                if len(self._fed_ids) >= 2:
                    return "COMPLETED"
            return "RUNNING"
        return "COMPLETED" if len(self._fed_ids) >= 2 else "NEEDS_AI"

    def _step_tame(self, bb: Blackboard) -> str:
        targets = [
            e for e in bb.entities
            if str(e.get("name") or "") in {"wolf", "cat", "horse", "donkey"} and isinstance(e.get("pos"), list)
        ]
        if not targets:
            bb.last_failure_reason = "tame:no_tameable_animal"
            return "NEEDS_AI"
        targets.sort(key=lambda e: dist3(bb.pos, e["pos"]))
        ent = targets[0]
        ename = str(ent["name"])
        eid = int(ent.get("id", -1))
        if ename == "wolf":
            item: Optional[str] = "bone"
            if bb.inventory_counts.get("bone", 0) < 1:
                return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "bone", 2))
        elif ename == "cat":
            item = next((f for f in ("cod", "salmon") if bb.inventory_counts.get(f, 0) > 0), None)
            if item is None:
                bb.last_failure_reason = "tame:need_fish_for_cat"
                return "NEEDS_AI"
        else:
            item = None
        if bool(bb.status.get("in_vehicle")):
            out = self.ctx.execute_write(bb, "dismount", {}, lambda: self.ctx.client.dismount())
            return "UNKNOWN" if out.status == "UNKNOWN" else "COMPLETED"
        out = self._interact_entity_step(bb, eid, ent["pos"], item=item)
        if out.status == "UNKNOWN":
            return "UNKNOWN"
        self._tries += 1
        if out.status == "OK" and (ename not in {"wolf", "cat"} or self._tries >= 3 or bb.inventory_counts.get(item or "", 0) == 0):
            return "COMPLETED"
        if self._tries >= 6:
            return "COMPLETED"
        return "RUNNING"

    def step(self, bb: Blackboard) -> str:
        self._steps += 1
        if self._steps > self.max_steps:
            bb.last_failure_reason = f"adv_timeout:{self.target}"
            return "NEEDS_AI"

        if self.target == "breed_an_animal":
            return self._step_breed(bb)
        if self.target == "tame_an_animal":
            return self._step_tame(bb)
        if self.target in {"tactical_fishing", "axolotl_in_a_bucket"}:
            child = self._ensure_items(bb, (("water_bucket", 1),))
            if child:
                return child
            wanted = {"axolotl"} if self.target == "axolotl_in_a_bucket" else {"cod", "salmon", "tropical_fish", "pufferfish"}
            cands = [e for e in bb.entities if str(e.get("name") or "") in wanted and isinstance(e.get("pos"), list)]
            if not cands:
                bb.last_failure_reason = f"{self.target}:no_entity"
                return "NEEDS_AI"
            cands.sort(key=lambda e: dist3(bb.pos, e["pos"]))
            out = self._interact_entity_step(bb, int(cands[0]["id"]), cands[0]["pos"], item="water_bucket")
            return "UNKNOWN" if out.status == "UNKNOWN" else ("COMPLETED" if out.status == "OK" else "RUNNING")
        if self.target == "distract_piglin":
            child = self._ensure_items(bb, (("gold_ingot", 1),))
            if child:
                return child
            cands = [e for e in bb.entities if str(e.get("name") or "") == "piglin" and isinstance(e.get("pos"), list)]
            if not cands:
                bb.last_failure_reason = "distract_piglin:no_piglin"
                return "NEEDS_AI"
            cands.sort(key=lambda e: dist3(bb.pos, e["pos"]))
            if dist3(bb.pos, cands[0]["pos"]) > 6.0:
                nav = self._nav.step_to(bb, cands[0]["pos"], tolerance=5.0)
                return "UNKNOWN" if nav.status == "UNKNOWN" else "RUNNING"
            out = self.ctx.execute_write(bb, "drop", {"item": "gold_ingot", "count": 1}, lambda: self.ctx.client.drop("gold_ingot", 1))
            return "UNKNOWN" if out.status == "UNKNOWN" else ("COMPLETED" if out.status == "OK" else "RUNNING")
        if self.target == "brush_armadillo":
            child = self._ensure_items(bb, (("brush", 1),))
            if child:
                return child
            cands = [e for e in bb.entities if str(e.get("name") or "") == "armadillo" and isinstance(e.get("pos"), list)]
            if not cands:
                return "NEEDS_AI"
            out = self._interact_entity_step(bb, int(cands[0]["id"]), cands[0]["pos"], item="brush")
            return "UNKNOWN" if out.status == "UNKNOWN" else ("COMPLETED" if out.status == "OK" else "RUNNING")
        if self.target == "ride_a_boat_with_a_goat":
            child = self._ensure_items(bb, (("oak_boat", 1),))
            if child:
                return child
            goats = [e for e in bb.entities if str(e.get("name") or "") == "goat" and isinstance(e.get("pos"), list)]
            if not goats:
                return "NEEDS_AI"
            boats = [e for e in bb.entities if "boat" in str(e.get("name") or "") and isinstance(e.get("pos"), list)]
            if not boats:
                out = self.ctx.execute_write(bb, "use_item", {"item": "oak_boat"}, lambda: self.ctx.client.use_item("oak_boat"))
                return "UNKNOWN" if out.status == "UNKNOWN" else "RUNNING"
            if bool(bb.status.get("in_vehicle")):
                out = self.ctx.execute_write(bb, "dismount", {}, lambda: self.ctx.client.dismount())
                return "UNKNOWN" if out.status == "UNKNOWN" else "COMPLETED"
            out = self._interact_entity_step(bb, int(boats[0]["id"]), boats[0]["pos"])
            return "UNKNOWN" if out.status == "UNKNOWN" else "RUNNING"
        return "NEEDS_AI"


class UseItemAndFishAdvTask(_AdvPluginBase):
    """Plugin 3: fishy_business, spyglass_at_parrot, spyglass_at_ghast."""

    def __init__(self, ctx: PrimitiveContext, catalogue: TaskCatalogue, target: str) -> None:
        super().__init__(ctx, catalogue, target, max_steps=26)
        self._casts: int = 0

    def stop(self, bb: Blackboard) -> None:
        self._casts = 0
        super().stop(bb)

    def step(self, bb: Blackboard) -> str:
        self._steps += 1
        if self._steps > self.max_steps:
            bb.last_failure_reason = f"adv_timeout:{self.target}"
            return "NEEDS_AI"

        if self.target == "fishy_business":
            child = self._ensure_items(bb, (("fishing_rod", 1),))
            if child:
                return child
            waters = self.ctx.gateway.scan_blocks(bb.dimension, ["water"], radius=24, limit=8)
            if not waters:
                bb.last_failure_reason = "fishy_business:no_water"
                return "NEEDS_AI"
            wpos = _as_coord3(waters[0].get("pos"))
            if wpos is None:
                return "NEEDS_AI"
            shore: Optional[Coord3] = None
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                for dy in (1, 0):
                    cand = (wpos[0] + dx, wpos[1] + dy, wpos[2] + dz)
                    below = self.ctx.gateway.get_block(bb.dimension, (cand[0], cand[1] - 1, cand[2]), allow_fetch=True)
                    feet = self.ctx.gateway.get_block(bb.dimension, cand, allow_fetch=True)
                    if below is not None and below.name not in NON_SOLID_BLOCKS and feet is not None and feet.name in AIR_BLOCKS:
                        shore = cand
                        break
                if shore is not None:
                    break
            stand_tgt = (shore[0] + 0.5, float(shore[1]), shore[2] + 0.5) if shore else (wpos[0] + 0.5, float(wpos[1] + 1), wpos[2] + 1.5)
            if dist3(bb.pos, stand_tgt) > 3.2:
                nav = self._nav.step_to(bb, stand_tgt, tolerance=2.2)
                return "UNKNOWN" if nav.status == "UNKNOWN" else "RUNNING"
            out = self.ctx.execute_write(
                bb,
                "use_item",
                {"item": "fishing_rod", "target": {"pos": list(wpos)}},
                lambda _w=wpos: self.ctx.client.use_item("fishing_rod", target={"pos": list(_w)}, timeout_ms=25000),
            )
            if out.status == "UNKNOWN":
                return "UNKNOWN"
            self._casts += 1
            if out.status == "OK" or self._casts >= 2:
                return "COMPLETED"
            return "RUNNING"

        # spyglass_at_parrot / spyglass_at_ghast
        child = self._ensure_items(bb, (("spyglass", 1),))
        if child:
            return child
        want = "parrot" if self.target == "spyglass_at_parrot" else "ghast"
        cands = [e for e in bb.entities if str(e.get("name") or "") == want and isinstance(e.get("pos"), list)]
        if not cands:
            bb.last_failure_reason = f"{self.target}:no_{want}"
            return "NEEDS_AI"
        cands.sort(key=lambda e: dist3(bb.pos, e["pos"]))
        eid = int(cands[0]["id"])
        out = self.ctx.execute_write(
            bb,
            "use_item",
            {"item": "spyglass", "target": {"entity_id": eid}},
            lambda _id=eid: self.ctx.client.use_item("spyglass", ms=900, target={"entity_id": _id}),
        )
        return "UNKNOWN" if out.status == "UNKNOWN" else ("COMPLETED" if out.status == "OK" else "RUNNING")


class CombatAndRangedAdvTask(_AdvPluginBase):
    """Plugin 4: kill_a_mob, deflect_arrow, shoot_arrow, ol_betsy, bullseye, sniper_duel."""

    def __init__(self, ctx: PrimitiveContext, catalogue: TaskCatalogue, target: str) -> None:
        super().__init__(ctx, catalogue, target, max_steps=32)
        self._shots: int = 0
        self._guard_ticks: int = 0

    def stop(self, bb: Blackboard) -> None:
        self._shots = 0
        self._guard_ticks = 0
        super().stop(bb)

    def step(self, bb: Blackboard) -> str:
        self._steps += 1
        if self._steps > self.max_steps:
            bb.last_failure_reason = f"adv_timeout:{self.target}"
            return "NEEDS_AI"

        if self.target == "kill_a_mob":
            if not any(k.endswith(("_sword", "_axe")) and v > 0 for k, v in bb.inventory_counts.items()):
                return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "stone_sword", 1))
            if bb.health < 8.0:
                bb.last_failure_reason = "kill_a_mob:low_hp"
                return "NEEDS_AI"
            hostiles = [
                e for e in bb.entities
                if str(e.get("type") or "") == "hostile" and isinstance(e.get("pos"), list)
                and str(e.get("name") or "") not in {"creeper", "warden"}
            ]
            if not hostiles:
                bb.last_failure_reason = "kill_a_mob:no_hostile"
                return "NEEDS_AI"
            hostiles.sort(key=lambda e: dist3(bb.pos, e["pos"]))
            target_ent = hostiles[0]
            out = self._combat.melee_step(bb, int(target_ent["id"]), target_ent["pos"], max_hits=4)
            if out.status == "UNKNOWN":
                return "UNKNOWN"
            if out.status == "OK" and bool((out.details or {}).get("killed")):
                return "COMPLETED"
            return "RUNNING"

        if self.target == "deflect_arrow":
            child = self._ensure_items(bb, (("shield", 1),))
            if child:
                return child
            off = bb.inventory.get("offhand")
            if not (isinstance(off, dict) and off.get("name") == "shield"):
                out = self.ctx.execute_write(bb, "equip", {"item": "shield", "slot": "off-hand"}, lambda: self.ctx.client.equip("shield", slot="off-hand"))
                return "UNKNOWN" if out.status == "UNKNOWN" else "RUNNING"
            shooters = [e for e in bb.entities if str(e.get("name") or "") in {"skeleton", "stray", "pillager"} and isinstance(e.get("pos"), list)]
            if not shooters:
                bb.last_failure_reason = "deflect_arrow:no_skeleton"
                return "NEEDS_AI"
            shooters.sort(key=lambda e: dist3(bb.pos, e["pos"]))
            spos = shooters[0]["pos"]
            if dist3(bb.pos, spos) > 12.0:
                nav = self._nav.step_to(bb, spos, tolerance=8.0)
                return "UNKNOWN" if nav.status == "UNKNOWN" else "RUNNING"
            if not bool(bb.status.get("blocking")):
                out = self._combat.shield_posture(bb, active=True)
                return "UNKNOWN" if out.status == "UNKNOWN" else "RUNNING"
            self._guard_ticks += 1
            if self._guard_ticks >= 4:
                out = self._combat.shield_posture(bb, active=False)
                return "UNKNOWN" if out.status == "UNKNOWN" else "COMPLETED"
            out = self.ctx.execute_write(bb, "look", {"pos": list(spos)}, lambda: self.ctx.client.look_at(spos[0], spos[1] + 1.0, spos[2]))
            return "UNKNOWN" if out.status == "UNKNOWN" else "RUNNING"

        weapon = "crossbow" if self.target == "ol_betsy" else "bow"
        min_arrows = 4 if self.target == "bullseye" else (6 if self.target == "sniper_duel" else 1)
        child = self._ensure_items(bb, ((weapon, 1), ("arrow", min_arrows)))
        if child:
            return child

        if self.target == "bullseye":
            pos, st = self._ensure_placed_block(bb, "target")
            if st is not None or pos is None:
                return st or "RUNNING"
            center = [pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5]
            d = dist3(bb.pos, center)
            if d < 30.5:
                dx = bb.pos[0] - center[0]
                dz = bb.pos[2] - center[2]
                norm = math.hypot(dx, dz) or 1.0
                retreat = (center[0] + (dx / norm) * 32.0, bb.pos[1], center[2] + (dz / norm) * 32.0)
                nav = self._nav.step_to(bb, retreat, tolerance=2.0, xz_only=True)
                return "UNKNOWN" if nav.status == "UNKNOWN" else "RUNNING"
            out = self._combat.shoot_step(bb, pos=center, weapon="bow")
            if out.status == "UNKNOWN":
                return "UNKNOWN"
            self._shots += 1
            return "COMPLETED" if self._shots >= 3 else "RUNNING"

        wanted_names = {"skeleton"} if self.target == "sniper_duel" else {"zombie", "skeleton", "spider", "cow", "sheep", "pig", "chicken"}
        targets = [e for e in bb.entities if str(e.get("name") or "") in wanted_names and isinstance(e.get("pos"), list)]
        if not targets:
            bb.last_failure_reason = f"{self.target}:no_target"
            return "NEEDS_AI"
        targets.sort(key=lambda e: dist3(bb.pos, e["pos"]))
        tgt = targets[0]
        if self.target == "sniper_duel" and dist3(bb.pos, tgt["pos"]) < 50.5:
            dx = bb.pos[0] - float(tgt["pos"][0])
            dz = bb.pos[2] - float(tgt["pos"][2])
            norm = math.hypot(dx, dz) or 1.0
            far = (float(tgt["pos"][0]) + (dx / norm) * 53.0, bb.pos[1], float(tgt["pos"][2]) + (dz / norm) * 53.0)
            nav = self._nav.step_to(bb, far, tolerance=2.5, xz_only=True)
            return "UNKNOWN" if nav.status == "UNKNOWN" else "RUNNING"
        out = self._combat.shoot_step(bb, int(tgt["id"]), weapon=weapon)
        if out.status == "UNKNOWN":
            return "UNKNOWN"
        self._shots += 1
        return "COMPLETED" if (out.status == "OK" or self._shots >= 2) else "RUNNING"


class WindowActionAdvTask(_AdvPluginBase):
    """Plugin 5: trade (villager/wandering_trader) and enchant_item (enchanting_table)."""

    def __init__(self, ctx: PrimitiveContext, catalogue: TaskCatalogue, target: str) -> None:
        super().__init__(ctx, catalogue, target, max_steps=30)
        self._trade_pick: Optional[Tuple[int, int]] = None

    def stop(self, bb: Blackboard) -> None:
        self._trade_pick = None
        super().stop(bb)

    def _step_trade(self, bb: Blackboard) -> str:
        if self._trade_pick is not None:
            eid, chosen_idx = self._trade_pick
            self._trade_pick = None
            act_out = self.ctx.execute_write(
                bb,
                "window/act",
                {"entity_id": eid, "ops": [{"op": "select", "option": chosen_idx}]},
                lambda _id=eid, _i=chosen_idx: self.ctx.client.window_act(entity_id=_id, ops=[{"op": "select", "option": _i}]),
            )
            return "UNKNOWN" if act_out.status == "UNKNOWN" else ("COMPLETED" if act_out.status == "OK" else "NEEDS_AI")

        traders = [e for e in bb.entities if str(e.get("name") or "") in {"villager", "wandering_trader"} and isinstance(e.get("pos"), list)]
        if not traders:
            bb.last_failure_reason = "trade:no_villager"
            return "NEEDS_AI"
        traders.sort(key=lambda e: dist3(bb.pos, e["pos"]))
        ent = traders[0]
        eid = int(ent["id"])
        if dist3(bb.pos, ent["pos"]) > 3.5:
            nav = self._nav.step_to(bb, ent["pos"], tolerance=2.5)
            return "UNKNOWN" if nav.status == "UNKNOWN" else "RUNNING"
        view_out = self.ctx.execute_write(bb, "window/view", {"entity_id": eid}, lambda: self.ctx.client.window_view(entity_id=eid))
        if view_out.status == "UNKNOWN":
            return "UNKNOWN"
        if view_out.status != "OK":
            bb.last_failure_reason = f"trade:view_{view_out.reason}"
            return "NEEDS_AI"
        win = view_out.details or {}
        options = win.get("options") or win.get("offers") or win.get("trades") or []
        chosen_idx: Optional[int] = None
        for idx, off in enumerate(options):
            if not isinstance(off, dict) or off.get("disabled") or off.get("available") is False:
                continue
            give = off.get("give") or off.get("input1") or off.get("cost")
            reqs = give if isinstance(give, list) else ([give] if isinstance(give, dict) else [])
            if not reqs:
                continue
            can_pay = True
            for req in reqs:
                if not isinstance(req, dict):
                    continue
                req_name = str(req.get("name") or "")
                req_cnt = int(req.get("count", 1) or 1)
                if not req_name or bb.inventory_counts.get(req_name, 0) < req_cnt:
                    can_pay = False
                    break
            if can_pay:
                chosen_idx = int(off.get("index", idx))
                break
        if chosen_idx is None:
            bb.last_failure_reason = "trade:no_affordable_offer"
            return "NEEDS_AI"
        self._trade_pick = (eid, chosen_idx)
        return "RUNNING"

    def step(self, bb: Blackboard) -> str:
        self._steps += 1
        if self._steps > self.max_steps:
            bb.last_failure_reason = f"adv_timeout:{self.target}"
            return "NEEDS_AI"
        if self.target == "trade":
            return self._step_trade(bb)
        if self.target == "enchant_item":
            child = self._ensure_items(bb, (("lapis_lazuli", 1),))
            if child:
                return child
            gear = next(
                (it for it in ("diamond_pickaxe", "iron_pickaxe", "iron_sword", "iron_boots", "stone_pickaxe", "stone_sword") if bb.inventory_counts.get(it, 0) > 0),
                None,
            )
            if gear is None:
                return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "stone_pickaxe", 1))
            pos, st = self._ensure_placed_block(bb, "enchanting_table")
            if st is not None or pos is None:
                return st or "RUNNING"
            if dist3(bb.pos, (pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5)) > 4.0:
                nav = self._nav.step_to(bb, (pos[0] + 0.5, float(pos[1]), pos[2] + 0.5), tolerance=3.0)
                return "UNKNOWN" if nav.status == "UNKNOWN" else "RUNNING"
            ops = [
                {"op": "put", "item": gear, "count": 1, "to": "item"},
                {"op": "put", "item": "lapis_lazuli", "count": 1, "to": "lapis"},
                {"op": "select", "option": 0},
                {"op": "take", "from": "item"},
            ]
            out = self.ctx.execute_write(
                bb,
                "window/act",
                {"pos": list(pos), "ops": ops},
                lambda _p=pos, _o=ops: self.ctx.client.window_act(pos=list(_p), ops=_o),
            )
            return "UNKNOWN" if out.status == "UNKNOWN" else ("COMPLETED" if out.status == "OK" else "NEEDS_AI")
        return "NEEDS_AI"


class StructureBuildAdvTask(_AdvPluginBase):
    """Plugin 6: summon_iron_golem and crafters_crafting_crafters."""

    def __init__(self, ctx: PrimitiveContext, catalogue: TaskCatalogue, target: str) -> None:
        super().__init__(ctx, catalogue, target, max_steps=44)
        self._part_idx: int = 0
        self._origin: Optional[Coord3] = None

    def stop(self, bb: Blackboard) -> None:
        self._part_idx = 0
        self._origin = None
        super().stop(bb)

    def _step_iron_golem(self, bb: Blackboard) -> str:
        if self._origin is None:
            child = self._ensure_items(bb, (("iron_block", 4),))
            if child:
                return child
        if bb.inventory_counts.get("carved_pumpkin", 0) < 1 and self._part_idx < 4:
            if bb.inventory_counts.get("pumpkin", 0) > 0 and bb.inventory_counts.get("shears", 0) > 0:
                pos, st = self._ensure_placed_block(bb, "pumpkin")
                if st is not None or pos is None:
                    return st or "RUNNING"
                ent_p = self.ctx.gateway.get_block(bb.dimension, pos, allow_fetch=True)
                if ent_p is not None and ent_p.name == "carved_pumpkin":
                    out2 = self._dig.dig_nearest_of_types(bb, ("carved_pumpkin",), radius=8, collect=True)
                    return "UNKNOWN" if out2.status == "UNKNOWN" else "RUNNING"
                out = self._interact_block_step(bb, pos, item="shears")
                if out.status == "UNKNOWN":
                    return "UNKNOWN"
                if out.status == "OK":
                    self.ctx.gateway.write_through_place(bb.dimension, pos, "carved_pumpkin")
                return "RUNNING"
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "pumpkin", 1))
        if self._origin is None:
            bx, by, bz = block_coord(bb.pos)
            # Find clear 3-wide spot with solid floor beneath base (checking oy in 0, -1, 1)
            best_orig: Optional[Coord3] = None
            for oy_off in (0, -1, 1):
                for ox_off, oz_off in ((2, 0), (-2, 0), (0, 2), (0, -2), (3, 0), (0, 3)):
                    cx, cy, cz = bx + ox_off, by + oy_off, bz + oz_off
                    floor = self.ctx.gateway.get_block(bb.dimension, (cx, cy - 1, cz), allow_fetch=True)
                    base = self.ctx.gateway.get_block(bb.dimension, (cx, cy, cz), allow_fetch=True)
                    if floor is not None and floor.name not in NON_SOLID_BLOCKS and base is not None and base.name in AIR_BLOCKS:
                        best_orig = (cx, cy, cz)
                        break
                if best_orig is not None:
                    break
            if best_orig is None:
                bb.last_failure_reason = "iron_golem:no_build_site"
                return "NEEDS_AI"
            self._origin = best_orig
        ox, oy, oz = self._origin
        blueprint = [
            ((ox, oy, oz), "iron_block"),
            ((ox, oy + 1, oz), "iron_block"),
            ((ox - 1, oy + 1, oz), "iron_block"),
            ((ox + 1, oy + 1, oz), "iron_block"),
            ((ox, oy + 2, oz), "carved_pumpkin"),
        ]
        while self._part_idx < len(blueprint):
            coord, item = blueprint[self._part_idx]
            ent = self.ctx.gateway.get_block(bb.dimension, coord, allow_fetch=True)
            if ent is not None and ent.name == item:
                self._part_idx += 1
                continue
            rem_need = sum(1 for _, nm in blueprint[self._part_idx:] if nm == item)
            if bb.inventory_counts.get(item, 0) < rem_need and item == "iron_block":
                return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, item, rem_need))
            out = self._place_at_checked(bb, item, coord)
            if out.status == "UNKNOWN":
                return "UNKNOWN"
            if out.status == "OK":
                self._part_idx += 1
                return "COMPLETED" if self._part_idx >= len(blueprint) else "RUNNING"
            if out.status == "FAILED_LOCAL":
                if self._part_idx == 0:
                    self._origin = None
                    return "RUNNING"
                return "NEEDS_AI"
            return "RUNNING"
        return "COMPLETED"

    def step(self, bb: Blackboard) -> str:
        self._steps += 1
        if self._steps > self.max_steps:
            bb.last_failure_reason = f"adv_timeout:{self.target}"
            return "NEEDS_AI"
        if self.target == "summon_iron_golem":
            return self._step_iron_golem(bb)
        if self.target == "crafters_crafting_crafters":
            if self._placed_pos is None:
                reqs = (("crafter", 1), ("lever", 1), ("iron_ingot", 5), ("crafting_table", 1), ("redstone", 2), ("dropper", 1))
                child = self._ensure_items(bb, reqs)
                if child:
                    return child
            cpos, st = self._ensure_placed_block(bb, "crafter")
            if st is not None or cpos is None:
                return st or "RUNNING"
            lpos = (cpos[0] + 1, cpos[1], cpos[2])
            lent = self.ctx.gateway.get_block(bb.dimension, lpos, allow_fetch=True)
            if lent is None or lent.name != "lever":
                out = self._place_at_checked(bb, "lever", lpos)
                return "UNKNOWN" if out.status == "UNKNOWN" else "RUNNING"
            if self._part_idx == 0:
                ops = [
                    {"op": "put", "item": "iron_ingot", "count": 5, "to": "container"},
                    {"op": "put", "item": "crafting_table", "count": 1, "to": "container"},
                    {"op": "put", "item": "redstone", "count": 2, "to": "container"},
                    {"op": "put", "item": "dropper", "count": 1, "to": "container"},
                ]
                out = self.ctx.execute_write(bb, "window/act", {"pos": list(cpos), "ops": ops}, lambda: self.ctx.client.window_act(pos=list(cpos), ops=ops))
                if out.status == "UNKNOWN":
                    return "UNKNOWN"
                self._part_idx = 1
                return "RUNNING"
            out = self._interact_block_step(bb, lpos)
            return "UNKNOWN" if out.status == "UNKNOWN" else ("COMPLETED" if out.status == "OK" else "RUNNING")
        return "NEEDS_AI"
