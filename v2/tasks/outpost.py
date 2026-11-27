"""L3b Outpost & Nether Portal Shelter tasks: EstablishOutpostTask and NetherPortalShelterTask (<= 1 write/tick)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from v2.bt.blackboard import Blackboard
from v2.domain import (
    AIR_BLOCKS,
    BED_BLOCKS,
    Coord3,
    FOOD_ITEMS,
    HAZARD_BLOCKS,
    LIQUID_BLOCKS,
    NON_SOLID_BLOCKS,
    STONE_MOLD_ITEMS,
    VALUABLE_STASH_ITEMS,
    block_coord,
    dist3,
    pick_best_item,
)
from v2.primitives.base import PrimitiveContext
from v2.primitives.container import ContainerPrimitive
from v2.primitives.navigate import NavigatePrimitive
from v2.primitives.place_build import PlaceBuildPrimitive
from v2.tasks.acquire import AcquireItemTask
from v2.tasks.base import Task
from v2.tasks.catalogue import TaskCatalogue
from v2.tasks.surface import load_anchor_landmark, save_anchor_landmark


_VALID_OUTPOST_MODES = frozenset({"establish", "stash", "withdraw"})


def _record_owned_chest(db: Any, bb: Blackboard, pos: Coord3, *, writer: str) -> None:
    """Persist chest coord to owned_chest_coords and dimension outpost landmark."""
    coord: Coord3 = (int(pos[0]), int(pos[1]), int(pos[2]))
    owned = list(bb.get("owned_chest_coords") or [])
    normalized = [(int(c[0]), int(c[1]), int(c[2])) for c in owned if isinstance(c, (list, tuple)) and len(c) == 3]
    if coord not in normalized:
        normalized.append(coord)
    bb.set("owned_chest_coords", normalized, writer=writer)
    slot = "nether_outpost_chest" if bb.dimension == "the_nether" else "outpost_chest"
    save_anchor_landmark(db, bb, slot, slot, coord, writer=writer)


def _extract_container_counts(window: Dict[str, Any]) -> Dict[str, int]:
    sections = window.get("sections") if isinstance(window.get("sections"), dict) else {}
    rows = sections.get("container")
    counts: Dict[str, int] = {}
    if not isinstance(rows, list):
        return counts
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("name"), str):
            name = row["name"]
            counts[name] = counts.get(name, 0) + max(0, int(row.get("count", 0) or 0))
    return counts


class EstablishOutpostTask(Task):
    """Establish a safe Overworld/Nether outpost chest (+ Overworld bed spawn bind), or stash/withdraw valuables."""

    def __init__(
        self,
        ctx: PrimitiveContext,
        catalogue: TaskCatalogue,
        *,
        mode: str = "establish",
        require_bed: Optional[bool] = None,
    ) -> None:
        if mode not in _VALID_OUTPOST_MODES:
            raise ValueError(f"Unsupported outpost mode: {mode!r}")
        super().__init__(f"outpost:{mode}")
        self.ctx = ctx
        self.catalogue = catalogue
        self.mode = mode
        self.require_bed = require_bed
        self._nav = NavigatePrimitive(ctx)
        self._place = PlaceBuildPrimitive(ctx)
        self._container = ContainerPrimitive(ctx)

        self._chest_pos: Optional[Coord3] = None
        self._bed_pos: Optional[Coord3] = None
        self._bed_interacted: bool = False
        self._established: bool = False
        self._inspected: bool = False
        self._last_window: Optional[Dict[str, Any]] = None
        self._transfer_done: bool = False
        self._landmark_nav_exhausted: bool = False

    def stop(self, bb: Blackboard) -> None:
        """Pure-memory reset; zero network writes."""
        self._chest_pos = None
        self._bed_pos = None
        self._bed_interacted = False
        self._established = False
        self._inspected = False
        self._last_window = None
        self._transfer_done = False
        self._landmark_nav_exhausted = False
        super().stop(bb)

    def is_finished(self, bb: Blackboard) -> bool:
        if self.mode in {"stash", "withdraw"}:
            return self._transfer_done
        return self._established

    def _find_dry_spot(
        self,
        bb: Blackboard,
        *,
        near_pos: Optional[Coord3] = None,
        exclude: Set[Coord3] = frozenset(),
    ) -> Optional[Coord3]:
        """Find a dry, solid-supported air block near player or anchor."""
        if bool(bb.status.get("in_water") or bb.status.get("submerged")):
            return None
        fx, fy, fz = block_coord(bb.pos)
        ax, ay, az = near_pos if near_pos is not None else (fx, fy, fz)
        offsets = (
            (1, 0), (-1, 0), (0, 1), (0, -1),
            (2, 0), (-2, 0), (0, 2), (0, -2),
            (1, 1), (-1, 1), (1, -1), (-1, -1),
        )
        cands: List[Coord3] = []
        for oy in (0, 1, -1):
            for ox, oz in offsets:
                c = (ax + ox, ay + oy, az + oz)
                if c in exclude or c in {(fx, fy, fz), (fx, fy + 1, fz)}:
                    continue
                cands.append(c)
        probe: List[Coord3] = []
        for cx, cy, cz in cands:
            probe.extend([(cx, cy, cz), (cx, cy - 1, cz), (cx, cy + 1, cz)])
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, probe, max_age=4.0)
        for cx, cy, cz in cands:
            c = (cx, cy, cz)
            if self.ctx.gateway.unreachable.is_blocked(bb.dimension, c, "place"):
                continue
            ent = fetched.get(c)
            below = fetched.get((cx, cy - 1, cz))
            above = fetched.get((cx, cy + 1, cz))
            if (
                ent is not None and ent.name in AIR_BLOCKS
                and below is not None and below.name not in (NON_SOLID_BLOCKS | HAZARD_BLOCKS | LIQUID_BLOCKS)
                and (above is None or above.name in AIR_BLOCKS)
            ):
                return c
        return None

    def _chest_landmark_slot(self, bb: Blackboard) -> str:
        return "nether_outpost_chest" if bb.dimension == "the_nether" else "outpost_chest"

    def _find_nearby_chest(self, bb: Blackboard, *, radius: int = 16) -> Optional[Coord3]:
        if self._chest_pos is not None:
            ent = self.ctx.gateway.get_block(bb.dimension, self._chest_pos, allow_fetch=True)
            if ent is not None and ent.name == "chest":
                return self._chest_pos
            self._chest_pos = None

        rows = self.ctx.gateway.scan_blocks(bb.dimension, ["chest"], radius=radius, limit=8)
        if rows:
            rows.sort(key=lambda r: float(r.get("dist", 999.0)))
            pos = rows[0].get("pos")
            if isinstance(pos, list) and len(pos) == 3:
                coord: Coord3 = (int(pos[0]), int(pos[1]), int(pos[2]))
                self._chest_pos = coord
                _record_owned_chest(self.ctx.gateway.db, bb, coord, writer=self.name)
                return coord
        return None

    def _ensure_chest(self, bb: Blackboard) -> Tuple[Optional[Coord3], Optional[str]]:
        """Ensure an outpost chest exists within 16m; returns (chest_pos, pending_status)."""
        existing = self._find_nearby_chest(bb, radius=16)
        if existing is not None:
            _record_owned_chest(self.ctx.gateway.db, bb, existing, writer=self.name)
            return existing, None

        # Check saved landmark up to 24m if not yet scanned nearby
        slot = self._chest_landmark_slot(bb)
        if not self._landmark_nav_exhausted:
            anchor = load_anchor_landmark(self.ctx.gateway.db, bb, slot, slot, writer=self.name)
            if anchor is not None and dist3(bb.pos, (anchor[0] + 0.5, anchor[1] + 0.5, anchor[2] + 0.5)) <= 24.0:
                center = (anchor[0] + 0.5, anchor[1] + 0.5, anchor[2] + 0.5)
                if dist3(bb.pos, center) <= 12.0:
                    self._landmark_nav_exhausted = True
                else:
                    nav = self._nav.step_to(bb, center, tolerance=3.5)
                    if nav.status == "UNKNOWN":
                        return None, "UNKNOWN"
                    if nav.status in {"OK", "PROGRESSED"}:
                        return None, "RUNNING"
                    self._landmark_nav_exhausted = True
            else:
                self._landmark_nav_exhausted = True

        if self.mode == "withdraw":
            bb.last_failure_reason = "outpost:no_chest_to_withdraw"
            return None, "FAILED_LOCAL"

        # Nether bed-only guard: if no chest & no wood/planks to craft one while holding a bed, refuse immediately
        if bb.dimension == "the_nether" and bb.inventory_counts.get("chest", 0) < 1:
            has_bed = any(bb.inventory_counts.get(b, 0) > 0 for b in BED_BLOCKS)
            has_wood = any(
                cnt > 0 and (name.endswith("_planks") or name.endswith("_log"))
                for name, cnt in bb.inventory_counts.items()
            )
            if has_bed and not has_wood:
                bb.last_failure_reason = "nether_bed_forbidden"
                return None, "FAILED_LOCAL"

        if bb.inventory_counts.get("chest", 0) < 1:
            res = self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "chest", 1))
            return None, res

        spot = self._find_dry_spot(bb)
        if spot is not None:
            out = self._place.place_at(bb, "chest", spot)
        else:
            out = self._place.place_near_player(bb, "chest")
        if out.status == "UNKNOWN":
            return None, "UNKNOWN"
        if out.status == "OK":
            raw_pos = out.details.get("pos") if isinstance(out.details, dict) else None
            placed = (
                (int(raw_pos[0]), int(raw_pos[1]), int(raw_pos[2]))
                if isinstance(raw_pos, (list, tuple)) and len(raw_pos) == 3
                else spot
            )
            if placed is not None:
                self._chest_pos = placed
                _record_owned_chest(self.ctx.gateway.db, bb, placed, writer=self.name)
            return placed, "RUNNING"
        if out.status == "PROGRESSED":
            return None, "RUNNING"
        bb.last_failure_reason = f"outpost_place_chest:{out.reason}"
        return None, out.status

    def _ensure_bed_and_bind(self, bb: Blackboard) -> str:
        """Overworld-only: place carried bed if needed and interact to bind spawn (day or night)."""
        if bb.dimension == "the_nether":
            bb.last_failure_reason = "nether_bed_forbidden"
            return "FAILED_LOCAL"

        if self._bed_pos is None:
            rows = self.ctx.gateway.scan_blocks(bb.dimension, list(BED_BLOCKS), radius=16, limit=4)
            if rows:
                rows.sort(key=lambda r: float(r.get("dist", 999.0)))
                raw = rows[0].get("pos")
                if isinstance(raw, list) and len(raw) == 3:
                    self._bed_pos = (int(raw[0]), int(raw[1]), int(raw[2]))
            if self._bed_pos is None:
                anchor = load_anchor_landmark(self.ctx.gateway.db, bb, "outpost_bed", "outpost_bed", writer=self.name)
                if anchor is not None and dist3(bb.pos, (anchor[0] + 0.5, anchor[1] + 0.5, anchor[2] + 0.5)) <= 16.0:
                    ent = self.ctx.gateway.get_block(bb.dimension, anchor, allow_fetch=True)
                    if ent is not None and ent.name in BED_BLOCKS:
                        self._bed_pos = anchor

        if self._bed_pos is None:
            bed_item = pick_best_item(bb.inventory_counts, BED_BLOCKS)
            if bed_item is None:
                if self.require_bed:
                    bb.last_failure_reason = "outpost:no_bed"
                    return "FAILED_LOCAL"
                self._established = True
                return "COMPLETED"
            exclude = {self._chest_pos} if self._chest_pos is not None else frozenset()
            spot = self._find_dry_spot(bb, near_pos=self._chest_pos, exclude=exclude)
            if spot is not None:
                out = self._place.place_at(bb, bed_item, spot)
            else:
                out = self._place.place_near_player(bb, bed_item)
            if out.status == "UNKNOWN":
                return "UNKNOWN"
            if out.status == "OK":
                raw_pos = out.details.get("pos") if isinstance(out.details, dict) else None
                placed = (
                    (int(raw_pos[0]), int(raw_pos[1]), int(raw_pos[2]))
                    if isinstance(raw_pos, (list, tuple)) and len(raw_pos) == 3
                    else spot
                )
                if placed is not None:
                    self._bed_pos = placed
                    save_anchor_landmark(self.ctx.gateway.db, bb, "outpost_bed", "outpost_bed", placed, writer=self.name)
                return "RUNNING"
            if out.status == "PROGRESSED":
                return "RUNNING"
            bb.last_failure_reason = f"outpost_place_bed:{out.reason}"
            return out.status

        save_anchor_landmark(self.ctx.gateway.db, bb, "outpost_bed", "outpost_bed", self._bed_pos, writer=self.name)
        if self._bed_interacted or bb.get("outpost_bed_bound") == self._bed_pos:
            self._established = True
            return "COMPLETED"

        bed_center = (self._bed_pos[0] + 0.5, float(self._bed_pos[1]), self._bed_pos[2] + 0.5)
        if dist3(bb.pos, bed_center) > 3.2:
            nav = self._nav.step_to(bb, bed_center, tolerance=2.5)
            if nav.status == "UNKNOWN":
                return "UNKNOWN"
            if nav.status in {"OK", "PROGRESSED"}:
                return "RUNNING"
            bb.last_failure_reason = f"outpost_bed_approach:{nav.reason}"
            return nav.status

        bpos = self._bed_pos
        out = self.ctx.execute_write(
            bb,
            "interact",
            {"pos": list(bpos)},
            lambda _p=bpos: self.ctx.client.interact(pos=list(_p)),
            unknown_center=bpos,
            record_unreachable_action="interact",
        )
        if out.status == "UNKNOWN":
            return "UNKNOWN"
        if out.status == "OK":
            self._bed_interacted = True
            bb.set("outpost_bed_bound", bpos, writer=self.name)
            self._established = True
            return "COMPLETED"
        if out.status == "PROGRESSED":
            return "RUNNING"
        bb.last_failure_reason = f"outpost_bed_interact:{out.reason}"
        return out.status

    @staticmethod
    def _compute_stash_ops(counts: Dict[str, int]) -> List[Dict[str, Any]]:
        """Build put ops for VALUABLE_STASH_ITEMS while keeping 1 stone_pickaxe + 32 mold + 4 food."""
        # Count non-valuable edible food first so golden_apple is only held back if no other food meets the 4-item reserve.
        non_valuable_food = sum(
            max(0, int(cnt))
            for item, cnt in counts.items()
            if item in FOOD_ITEMS and item not in VALUABLE_STASH_ITEMS and cnt > 0
        )
        food_reserve_needed = max(0, 4 - non_valuable_food)

        keep: Dict[str, int] = {}
        if counts.get("stone_pickaxe", 0) > 0:
            keep["stone_pickaxe"] = 1
        mold_rem = 32
        for m in STONE_MOLD_ITEMS:
            have = max(0, int(counts.get(m, 0)))
            take_keep = min(have, mold_rem)
            if take_keep > 0:
                keep[m] = take_keep
                mold_rem -= take_keep

        ops: List[Dict[str, Any]] = []
        for item in sorted(counts):
            if item not in VALUABLE_STASH_ITEMS:
                continue
            have = max(0, int(counts[item]))
            if have <= 0:
                continue
            held = keep.get(item, 0)
            if item in FOOD_ITEMS and food_reserve_needed > 0:
                reserve = min(have - held, food_reserve_needed)
                held += reserve
                food_reserve_needed -= reserve
            deposit = have - held
            if deposit > 0:
                ops.append({"op": "put", "to": "container", "item": item, "count": deposit})
        return ops

    @staticmethod
    def _compute_withdraw_ops(container_counts: Dict[str, int]) -> List[Dict[str, Any]]:
        ops: List[Dict[str, Any]] = []
        for item in sorted(container_counts):
            if item not in VALUABLE_STASH_ITEMS:
                continue
            cnt = max(0, int(container_counts[item]))
            if cnt > 0:
                ops.append({"op": "take", "from": "container", "item": item, "count": cnt})
        return ops

    def _run_transfer_mode(self, bb: Blackboard, chest_pos: Coord3) -> str:
        if not self._inspected:
            view = self._container.inspect_window(bb, chest_pos)
            if view.status == "UNKNOWN":
                return "UNKNOWN"
            if view.status == "PROGRESSED":
                return "RUNNING"
            if view.status == "FAILED_LOCAL":
                bb.last_failure_reason = f"outpost_inspect:{view.reason}"
                return "FAILED_LOCAL"
            self._inspected = True
            self._last_window = view.details if isinstance(view.details, dict) else {}
            return "RUNNING"

        if self.mode == "stash":
            ops = self._compute_stash_ops(bb.inventory_counts)
        else:
            c_counts = _extract_container_counts(self._last_window or {})
            ops = self._compute_withdraw_ops(c_counts)

        if not ops:
            self._transfer_done = True
            return "COMPLETED"

        batch = ops[:8]
        out = self._container.window_ops(bb, batch, pos=chest_pos)
        if out.status == "UNKNOWN":
            return "UNKNOWN"
        if out.status == "PROGRESSED":
            return "RUNNING"
        if out.status == "FAILED_LOCAL":
            bb.last_failure_reason = f"outpost_{self.mode}:{out.reason}"
            return "FAILED_LOCAL"

        if isinstance(out.details, dict) and isinstance(out.details.get("window"), dict):
            self._last_window = out.details["window"]

        if len(ops) <= 8:
            self._transfer_done = True
            return "COMPLETED"
        return "RUNNING"

    def on_tick(self, bb: Blackboard) -> str:
        if bb.dimension == "the_nether" and self.require_bed is True:
            bb.last_failure_reason = "nether_bed_forbidden"
            return "FAILED_LOCAL"

        chest_pos, pending = self._ensure_chest(bb)
        if pending is not None:
            return pending
        assert chest_pos is not None

        if self.mode in {"stash", "withdraw"}:
            return self._run_transfer_mode(bb, chest_pos)

        if bb.dimension == "the_nether":
            self._established = True
            return "COMPLETED"
        return self._ensure_bed_and_bind(bb)


class NetherPortalShelterTask(Task):
    """In the_nether, shield nearby nether_portal perimeter with stone mold & place a transit chest (ZERO beds)."""

    def __init__(self, ctx: PrimitiveContext, catalogue: TaskCatalogue) -> None:
        super().__init__("nether_portal_shelter")
        self.ctx = ctx
        self.catalogue = catalogue
        self._place = PlaceBuildPrimitive(ctx)
        self._portal_cells: List[Coord3] = []
        self._shield_cells: List[Coord3] = []
        self._skipped_cells: Set[Coord3] = set()
        self._chest_pos: Optional[Coord3] = None

    def stop(self, bb: Blackboard) -> None:
        """Pure-memory reset; zero network writes."""
        self._portal_cells = []
        self._shield_cells = []
        self._skipped_cells = set()
        self._chest_pos = None
        super().stop(bb)

    def is_finished(self, bb: Blackboard) -> bool:
        return bool(bb.get("nether_portal_sheltered"))

    def _discover_portal_and_shield(self, bb: Blackboard) -> bool:
        if not self._portal_cells:
            rows = self.ctx.gateway.scan_blocks(bb.dimension, ["nether_portal"], radius=24, limit=12)
            cells: List[Coord3] = []
            for r in rows:
                p = r.get("pos")
                if isinstance(p, list) and len(p) == 3:
                    cells.append((int(p[0]), int(p[1]), int(p[2])))
            if not cells:
                return False
            self._portal_cells = cells

        if not self._shield_cells:
            xs = [c[0] for c in self._portal_cells]
            ys = [c[1] for c in self._portal_cells]
            zs = [c[2] for c in self._portal_cells]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            min_z, max_z = min(zs), max(zs)
            portal_set = set(self._portal_cells)

            candidates: List[Coord3] = []
            if (max_x - min_x) >= (max_z - min_z):
                # Portal lies in X-Y plane at fixed Z; keep bot-side doorway open, shield opposite Z + side pillars.
                pz = min_z
                back_z = pz - 1 if bb.pos[2] >= pz + 0.5 else pz + 1
                door_z = pz + 1 if back_z == pz - 1 else pz - 1
                for y in range(min_y, min(max_y + 1, min_y + 2)):
                    for x in range(min_x, max_x + 1):
                        candidates.append((x, y, back_z))
                    candidates.append((min_x - 1, y, door_z))
                    candidates.append((max_x + 1, y, door_z))
            else:
                # Portal lies in Z-Y plane at fixed X; keep bot-side doorway open, shield opposite X + side pillars.
                px = min_x
                back_x = px - 1 if bb.pos[0] >= px + 0.5 else px + 1
                door_x = px + 1 if back_x == px - 1 else px - 1
                for y in range(min_y, min(max_y + 1, min_y + 2)):
                    for z in range(min_z, max_z + 1):
                        candidates.append((back_x, y, z))
                    candidates.append((door_x, y, min_z - 1))
                    candidates.append((door_x, y, max_z + 1))

            self._shield_cells = sorted(
                [c for c in dict.fromkeys(candidates) if c not in portal_set],
                key=lambda c: (c[1], c[0], c[2]),
            )
        return True

    def _next_missing_shield(self, bb: Blackboard) -> Optional[Coord3]:
        pending = [c for c in self._shield_cells if c not in self._skipped_cells]
        if not pending:
            return None
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, pending, max_age=4.0)
        for c in pending:
            if self.ctx.gateway.unreachable.is_blocked(bb.dimension, c, "place"):
                self._skipped_cells.add(c)
                continue
            ent = fetched.get(c)
            if ent is None or ent.name in AIR_BLOCKS:
                return c
        return None

    def _pick_transit_chest_spot(self, bb: Blackboard) -> Optional[Coord3]:
        xs = [c[0] for c in self._portal_cells]
        ys = [c[1] for c in self._portal_cells]
        zs = [c[2] for c in self._portal_cells]
        min_x, max_x = min(xs), max(xs)
        min_y = min(ys)
        min_z, max_z = min(zs), max(zs)
        fx, fy, fz = block_coord(bb.pos)
        protected = set(self._portal_cells) | set(self._shield_cells) | {(fx, fy, fz), (fx, fy + 1, fz)}

        cands: List[Coord3] = []
        for ox in range(-3, 4):
            for oz in range(-3, 4):
                if abs(ox) + abs(oz) == 0:
                    continue
                for base_x, base_z in ((min_x, min_z), (fx, fz)):
                    c = (base_x + ox, min_y, base_z + oz)
                    if c in protected:
                        continue
                    # Keep portal doorway lanes clear
                    if (min_x <= c[0] <= max_x and abs(c[2] - min_z) <= 1) or (min_z <= c[2] <= max_z and abs(c[0] - min_x) <= 1):
                        continue
                    cands.append(c)
        cands = list(dict.fromkeys(cands))
        probe: List[Coord3] = []
        for c in cands:
            probe.extend([c, (c[0], c[1] - 1, c[2]), (c[0], c[1] + 1, c[2])])
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, probe, max_age=4.0)
        for c in cands:
            ent = fetched.get(c)
            below = fetched.get((c[0], c[1] - 1, c[2]))
            above = fetched.get((c[0], c[1] + 1, c[2]))
            if (
                ent is not None and ent.name in AIR_BLOCKS
                and below is not None and below.name not in (NON_SOLID_BLOCKS | HAZARD_BLOCKS | LIQUID_BLOCKS)
                and (above is None or above.name in AIR_BLOCKS)
            ):
                return c
        return None

    def on_tick(self, bb: Blackboard) -> str:
        if bb.dimension != "the_nether":
            bb.last_failure_reason = "nether_portal_shelter:not_in_nether"
            return "FAILED_LOCAL"

        if not self._discover_portal_and_shield(bb):
            bb.last_failure_reason = "nether_portal_shelter:no_portal"
            return "FAILED_LOCAL"

        # 1) Build compact blast-shield cover around portal perimeter
        missing_cell = self._next_missing_shield(bb)
        if missing_cell is not None:
            mat = pick_best_item(bb.inventory_counts, STONE_MOLD_ITEMS)
            if mat is None:
                return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "cobblestone", 8))
            out = self._place.place_at(bb, mat, missing_cell)
            if out.status == "UNKNOWN":
                return "UNKNOWN"
            if out.status in {"OK", "PROGRESSED"}:
                return "RUNNING"
            if out.reason in {"occupied", "no_support", "blocked_by_self"}:
                self._skipped_cells.add(missing_cell)
                return "RUNNING"
            bb.last_failure_reason = f"nether_portal_shelter:{out.reason}"
            return out.status

        # 2) Ensure 1 transit chest nearby (ZERO bed writes!)
        if self._chest_pos is None:
            chests = self.ctx.gateway.scan_blocks(bb.dimension, ["chest"], radius=12, limit=4)
            if chests:
                chests.sort(key=lambda r: float(r.get("dist", 999.0)))
                raw = chests[0].get("pos")
                if isinstance(raw, list) and len(raw) == 3:
                    self._chest_pos = (int(raw[0]), int(raw[1]), int(raw[2]))

        if self._chest_pos is None:
            if bb.inventory_counts.get("chest", 0) < 1:
                return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "chest", 1))
            spot = self._pick_transit_chest_spot(bb)
            if spot is not None:
                out = self._place.place_at(bb, "chest", spot)
            else:
                out = self._place.place_near_player(bb, "chest")
            if out.status == "UNKNOWN":
                return "UNKNOWN"
            if out.status == "PROGRESSED":
                return "RUNNING"
            if out.status != "OK":
                bb.last_failure_reason = f"nether_portal_shelter_chest:{out.reason}"
                return out.status
            raw_pos = out.details.get("pos") if isinstance(out.details, dict) else None
            placed = (
                (int(raw_pos[0]), int(raw_pos[1]), int(raw_pos[2]))
                if isinstance(raw_pos, (list, tuple)) and len(raw_pos) == 3
                else spot
            )
            if placed is not None:
                self._chest_pos = placed

        if self._chest_pos is not None:
            _record_owned_chest(self.ctx.gateway.db, bb, self._chest_pos, writer=self.name)
        bb.set("nether_portal_sheltered", True, writer=self.name)
        return "COMPLETED"
