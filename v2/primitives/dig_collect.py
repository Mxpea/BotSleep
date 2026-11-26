"""L3a Primitive 2: DigCollectPrimitive (logs, ores, gravel->flint, occluders, vein completion)."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from v2.bt.blackboard import Blackboard
from v2.domain import Coord3, HAZARD_BLOCKS, LIQUID_BLOCKS, NON_SOLID_BLOCKS, block_coord, dist3
from v2.gateway.world_proxy import ray_cells
from v2.primitives.base import ActionOutcome, PrimitiveContext
from v2.primitives.navigate import NavigatePrimitive

_PROTECTED_DROP_WALL_BLOCKS = frozenset({
    "bedrock", "obsidian", "crafting_table", "furnace", "chest",
    "blast_furnace", "smoker", "nether_portal",
})


class DigCollectPrimitive:
    CLOSE_APPROACH_MIN_DIST = 2.6
    CLOSE_APPROACH_MAX_DIST = 4.2
    MAX_CLOSE_APPROACH_ATTEMPTS = 2
    DROP_PICKUP_MIN_DIST = 1.5
    DROP_PICKUP_MAX_DIST = 6.0
    VEIN_CLUSTER_DIST = 2.5
    VEIN_ORIGIN_MAX_MOVE = 8.0

    def __init__(self, ctx: PrimitiveContext) -> None:
        self.ctx = ctx
        self._nav = NavigatePrimitive(ctx)
        self._close_approach_attempts: Dict[Tuple[str, Coord3], int] = {}
        self._vein_queue: List[Coord3] = []
        self._vein_types: Optional[Tuple[str, ...]] = None
        self._vein_origin: Optional[Tuple[str, Tuple[float, float, float]]] = None
        self._last_collect_dig: Optional[Tuple[str, Coord3, int]] = None
        self._ignored_drop_keys: Set[str] = set()

    def clear_cache(self) -> None:
        """Pure-memory reset of vein queue, close-approach attempts, and drop hints."""
        self._close_approach_attempts.clear()
        self._vein_queue.clear()
        self._vein_types = None
        self._vein_origin = None
        self._last_collect_dig = None
        self._ignored_drop_keys.clear()

    @staticmethod
    def _is_item_entity(ent: Dict[str, Any]) -> bool:
        return str(ent.get("name") or "") == "item" or str(ent.get("type") or "") == "item"

    def _extract_item_drops(self, bb: Blackboard) -> List[Tuple[float, Tuple[float, float, float], str]]:
        drops: List[Tuple[float, Tuple[float, float, float], str]] = []
        for ent in bb.entities:
            if not isinstance(ent, dict) or not self._is_item_entity(ent):
                continue
            raw_pos = ent.get("pos")
            if not isinstance(raw_pos, (list, tuple)) or len(raw_pos) != 3:
                continue
            epos = (float(raw_pos[0]), float(raw_pos[1]), float(raw_pos[2]))
            d = dist3(bb.pos, epos)
            if not (self.DROP_PICKUP_MIN_DIST < d <= self.DROP_PICKUP_MAX_DIST):
                continue
            eid = ent.get("id")
            dkey = f"{bb.dimension}:{eid if eid is not None else (round(epos[0], 1), round(epos[1], 1), round(epos[2], 1))}"
            if dkey in self._ignored_drop_keys:
                continue
            drops.append((d, epos, dkey))
        drops.sort(key=lambda row: row[0])
        return drops

    def _record_post_dig_drop_hint(self, bb: Blackboard, target: Coord3) -> None:
        self._last_collect_dig = (bb.dimension, target, bb.tick_index)
        drops = self._extract_item_drops(bb)
        if drops:
            _, dpos, dkey = drops[0]
            bb.set(
                "pending_drop_pickup",
                {"pos": list(dpos), "key": dkey, "dim": bb.dimension, "attempts": 0, "set_tick": bb.tick_index},
                writer="dig_collect",
            )

    def _can_dig_thin_drop_wall(self, bb: Blackboard, cell: Coord3) -> bool:
        fx, fy, fz = block_coord(bb.pos)
        if cell == (fx, fy - 1, fz):
            return False
        protected = set(bb.get("protected_cells") or ())
        if cell in protected or self.ctx.gateway.unreachable.is_blocked(bb.dimension, cell, "dig"):
            return False
        eye = (bb.pos[0], bb.pos[1] + 1.62, bb.pos[2])
        if dist3(eye, (cell[0] + 0.5, cell[1] + 0.5, cell[2] + 0.5)) > 4.4:
            return False
        cx, cy, cz = cell
        probe = [cell, (cx, cy + 1, cz), (cx, cy - 1, cz), (cx + 1, cy, cz), (cx - 1, cy, cz), (cx, cy, cz + 1), (cx, cy, cz - 1)]
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, probe, max_age=3.0)
        ent = fetched.get(cell)
        if ent is None or ent.name in (NON_SOLID_BLOCKS | HAZARD_BLOCKS | LIQUID_BLOCKS | _PROTECTED_DROP_WALL_BLOCKS):
            return False
        if ent.name.endswith("_bed"):
            return False
        return not any(
            (nb := fetched.get(n)) is not None and (nb.name in HAZARD_BLOCKS or nb.name in LIQUID_BLOCKS)
            for n in probe[1:]
        )

    def pickup_pending_drop(self, bb: Blackboard) -> Optional[ActionOutcome]:
        """Step toward a stranded post-dig item drop (or clear a single thin solid wall blocking reach)."""
        if bb.writes_this_tick >= 1:
            return None
        pending = bb.get("pending_drop_pickup")
        has_pending = isinstance(pending, dict) and pending.get("dim") == bb.dimension
        recent_dig = (
            self._last_collect_dig is not None
            and self._last_collect_dig[0] == bb.dimension
            and abs(bb.tick_index - self._last_collect_dig[2]) <= 3
        )
        if not has_pending and not recent_dig:
            return None

        drops = self._extract_item_drops(bb)
        if not drops:
            if has_pending:
                bb.set("pending_drop_pickup", None, writer="dig_collect")
            return None

        _, drop_pos, drop_key = drops[0]
        attempts = int(pending.get("attempts", 0)) if (has_pending and pending.get("key") == drop_key) else 0
        if attempts >= 4:
            bb.set("pending_drop_pickup", None, writer="dig_collect")
            if len(self._ignored_drop_keys) > 64:
                self._ignored_drop_keys.clear()
            self._ignored_drop_keys.add(drop_key)
            return None

        bb.set(
            "pending_drop_pickup",
            {"pos": list(drop_pos), "key": drop_key, "dim": bb.dimension, "attempts": attempts + 1, "set_tick": bb.tick_index},
            writer="dig_collect",
        )

        fx, fy, fz = block_coord(bb.pos)
        drop_cell = block_coord(drop_pos)
        ignore_cells = {(fx, fy, fz), (fx, fy + 1, fz), (fx, fy - 1, fz), drop_cell}
        mid_cells = [
            c
            for c in ray_cells((bb.pos[0], bb.pos[1] + 0.6, bb.pos[2]), (drop_pos[0], drop_pos[1] + 0.25, drop_pos[2]))
            if c not in ignore_cells
        ]
        if mid_cells:
            fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, mid_cells, max_age=3.0)
            solid_blockers = [
                c for c in mid_cells
                if (ent := fetched.get(c)) is not None and ent.name not in NON_SOLID_BLOCKS
            ]
            if 1 <= len(solid_blockers) <= 2 and len({(c[0], c[2]) for c in solid_blockers}) == 1:
                bcell = solid_blockers[0]
                if self._can_dig_thin_drop_wall(bb, bcell):
                    dig_out = self.ctx.execute_write(
                        bb,
                        "dig",
                        {"pos": list(bcell), "collect": False},
                        lambda _c=bcell: self.ctx.client.dig(list(_c), collect=False),
                        unknown_center=bcell,
                        record_unreachable_action="dig",
                    )
                    if dig_out.status == "OK":
                        self.ctx.gateway.write_through_dig(bb.dimension, bcell)
                        return ActionOutcome(
                            "PROGRESSED",
                            "dig",
                            reason="drop_wall_cleared",
                            details={"pos": list(bcell), "drop": list(drop_pos)},
                        )
                    return dig_out

        nav_out = self._nav.step_to(bb, drop_pos, tolerance=1.0)
        if nav_out.status == "OK":
            bb.set("pending_drop_pickup", None, writer="dig_collect")
            return nav_out
        if nav_out.status == "FAILED_LOCAL":
            bb.set("pending_drop_pickup", None, writer="dig_collect")
            if len(self._ignored_drop_keys) > 64:
                self._ignored_drop_keys.clear()
            self._ignored_drop_keys.add(drop_key)
            if bb.writes_this_tick >= 1:
                return ActionOutcome("PROGRESSED", "navigate", reason="drop_pickup_abandoned")
            return None
        return nav_out

    def dig_block(self, bb: Blackboard, pos: Coord3, *, collect: bool = True) -> ActionOutcome:
        """Approach if out of dig reach (with two-tier close-approach for collect=True), otherwise dig ONE block."""
        target = (int(pos[0]), int(pos[1]), int(pos[2]))
        if self.ctx.gateway.unreachable.is_blocked(bb.dimension, target, "dig"):
            return ActionOutcome("FAILED_LOCAL", "dig", reason="blacklisted_unreachable", details={"pos": list(target)})

        center = (target[0] + 0.5, target[1] + 0.5, target[2] + 0.5)
        eye = (bb.pos[0], bb.pos[1] + 1.62, bb.pos[2])
        eye_dist = dist3(eye, center)
        key = (bb.dimension, target)

        if eye_dist > 4.4:
            nav_out = self._nav.step_to(bb, center, tolerance=2.0)
            if nav_out.status == "FAILED_LOCAL":
                # Blacklist unreachable far target (e.g. sealed underground cave pocket) so next tick picks another!
                self.ctx.gateway.unreachable.record_failure(bb.dimension, target, "dig", nav_out.reason)
                self.ctx.gateway.unreachable.record_failure(bb.dimension, target, "dig", nav_out.reason)
                return nav_out
            if not (nav_out.status == "OK" and (nav_out.action == "none" or nav_out.reason == "reached")):
                return nav_out
        elif collect and self.CLOSE_APPROACH_MIN_DIST < eye_dist <= self.CLOSE_APPROACH_MAX_DIST:
            attempts = self._close_approach_attempts.get(key, 0)
            if attempts < self.MAX_CLOSE_APPROACH_ATTEMPTS:
                nav_out = self._nav.step_to(bb, center, tolerance=1.5)
                if nav_out.status == "OK" and (nav_out.action == "none" or nav_out.reason == "reached"):
                    self._close_approach_attempts[key] = self.MAX_CLOSE_APPROACH_ATTEMPTS
                elif nav_out.status == "FAILED_LOCAL":
                    # Never blacklist "dig" on close-approach failure: preserve through-wall digging!
                    self._close_approach_attempts[key] = self.MAX_CLOSE_APPROACH_ATTEMPTS
                    if bb.writes_this_tick >= 1:
                        return ActionOutcome("PROGRESSED", "navigate", reason="close_approach_fallback_dig", details={"pos": list(target)})
                elif nav_out.status == "UNKNOWN":
                    return nav_out
                else:
                    self._close_approach_attempts[key] = attempts + 1
                    return nav_out

        out = self.ctx.execute_write(
            bb,
            "dig",
            {"pos": list(target), "collect": collect},
            lambda: self.ctx.client.dig(list(target), collect=collect),
            unknown_center=target,
            record_unreachable_action="dig",
        )
        if out.status == "OK":
            if len(self._close_approach_attempts) > 64:
                self._close_approach_attempts.clear()
            self._close_approach_attempts.pop(key, None)
            self.ctx.gateway.write_through_dig(bb.dimension, target)
            if collect:
                self._record_post_dig_drop_hint(bb, target)
            return out
        return out

    def dig_nearest_of_types(
        self,
        bb: Blackboard,
        block_types: Sequence[str],
        *,
        radius: int = 24,
        collect: bool = True,
        max_dy: Optional[float] = None,
        candidate_filter: Optional[Callable[[Coord3], bool]] = None,
    ) -> ActionOutcome:
        """Pick up stranded drops, consume local vein cache, or scan exposed blocks and dig nearest (<= 1 write)."""
        if collect:
            drop_out = self.pickup_pending_drop(bb)
            if drop_out is not None and drop_out.status in {"OK", "PROGRESSED", "UNKNOWN"}:
                return drop_out
            if bb.writes_this_tick >= 1 and drop_out is not None:
                return drop_out

        py = bb.pos[1]
        fx, fy, fz = int(bb.pos[0] // 1), int(bb.pos[1] // 1), int(bb.pos[2] // 1)
        under_feet = (fx, fy - 1, fz)
        protected = set(bb.get("protected_cells") or ())
        types_key = tuple(block_types)
        types_set = set(types_key)

        if (
            self._vein_types != types_key
            or self._vein_origin is None
            or self._vein_origin[0] != bb.dimension
            or dist3(bb.pos, self._vein_origin[1]) > self.VEIN_ORIGIN_MAX_MOVE
        ):
            self._vein_queue.clear()

        def _is_candidate_valid(c: Coord3) -> bool:
            if c == under_feet or c in protected:
                return False
            if max_dy is not None:
                dy = c[1] - py
                if dy < -1.0 or dy > max_dy:
                    return False
            if self.ctx.gateway.unreachable.is_blocked(bb.dimension, c, "dig"):
                return False
            cached = self.ctx.gateway.peek_block(bb.dimension, c)
            if cached is not None and not cached.must_reverify and cached.name not in types_set:
                return False
            return True

        self._vein_queue = [c for c in self._vein_queue if _is_candidate_valid(c)]
        if self._vein_queue:
            self._vein_queue.sort(key=lambda c: dist3(bb.pos, (c[0] + 0.5, c[1] + 0.5, c[2] + 0.5)) + abs(c[1] - py) * 2.5)
            while self._vein_queue:
                target = self._vein_queue[0]
                if candidate_filter is not None and not candidate_filter(target):
                    self._vein_queue.pop(0)
                    continue
                out = self.dig_block(bb, target, collect=collect)
                if out.status in {"OK", "FAILED_LOCAL"} and target in self._vein_queue:
                    self._vein_queue.remove(target)
                if out.status in {"OK", "PROGRESSED", "UNKNOWN"} or bb.writes_this_tick >= 1:
                    return out

        rows = self.ctx.gateway.scan_blocks(bb.dimension, list(block_types), radius=radius, limit=64)
        candidates: List[Coord3] = []
        for row in rows:
            raw_p = row.get("pos")
            if isinstance(raw_p, list) and len(raw_p) == 3:
                c = (int(raw_p[0]), int(raw_p[1]), int(raw_p[2]))
                # Minecraft Rule #1: never dig directly under own feet into a 1x1 pit!
                if c == under_feet or c in protected:
                    continue
                if max_dy is not None:
                    # Never dig blocks >= 2 below feet through a solid floor (drops fall into sealed caves!)
                    dy = c[1] - py
                    if dy < -1.0 or dy > max_dy:
                        continue
                if not self.ctx.gateway.unreachable.is_blocked(bb.dimension, c, "dig"):
                    candidates.append(c)
        if not candidates:
            self._vein_queue.clear()
            return ActionOutcome("FAILED_LOCAL", "dig_collect", reason="no_exposed_block_found")
        # Strongly prefer same-elevation surface blocks over deep underground cave pockets!
        candidates.sort(key=lambda c: dist3(bb.pos, (c[0] + 0.5, c[1] + 0.5, c[2] + 0.5)) + abs(c[1] - py) * 2.5)
        seed: Optional[Coord3] = None
        for idx, cand in enumerate(candidates):
            if candidate_filter is None or candidate_filter(cand):
                seed = cand
                candidates = candidates[idx:]
                break
        if seed is None:
            self._vein_queue.clear()
            return ActionOutcome("FAILED_LOCAL", "dig_collect", reason="no_exposed_block_found")
        cluster: List[Coord3] = [seed]
        remaining = set(candidates[1:])
        frontier: List[Coord3] = [seed]
        while frontier and len(cluster) < 16:
            cur = frontier.pop(0)
            neighbors = [cand for cand in remaining if dist3(cur, cand) <= self.VEIN_CLUSTER_DIST]
            neighbors.sort(key=lambda c: dist3(bb.pos, (c[0] + 0.5, c[1] + 0.5, c[2] + 0.5)))
            for cand in neighbors:
                if len(cluster) >= 16:
                    break
                remaining.discard(cand)
                cluster.append(cand)
                frontier.append(cand)
        self._vein_queue = cluster
        self._vein_types = types_key
        self._vein_origin = (bb.dimension, bb.pos)
        out = self.dig_block(bb, seed, collect=collect)
        if out.status in {"OK", "FAILED_LOCAL"} and seed in self._vein_queue:
            self._vein_queue.remove(seed)
        return out
