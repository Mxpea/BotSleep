"""Unified 3D navigation step executor, reversible breadcrumb trail, autonomous obstacle recovery, and retreat."""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from core.cave_probe import _can_dig_step_cell
from core.domain import AIR, HAZARDS, parse_pos
from core.pathfinder_3d import Pathfinder3D
from core.structure_spec import (
    CAVE_DIGGABLE_BLOCKS,
    DEFAULT_REPLACEABLE,
    ORE_DROP_MAP,
    SCAFFOLD_BLOCKS,
)
from core.world_3d import VoxelWorld3D, norm_coord
from skills.action_workflow import (
    WorkflowContext,
    WorkflowStepError,
    aim_at,
    dig_block,
    escape_to_surface,
    pillar_down,
    pillar_up,
    place_block,
    scaffold_drop,
    stand_cell,
)


STEP_RECOVERABLE_ERRORS = {
    "stalled",
    "path_not_found",
    "timeout",
    "unreachable_stance",
    "dig_failed",
    "place_failed",
    "pillar_failed",
    "unsafe_dig_floor",
    "target_obstructed",
    "route_blocked",
}


class ReversibleTrail:
    """Reversible breadcrumb + placed-pillar history with loop trimming over flat segments."""

    def __init__(self, entry_cell: Sequence[int]) -> None:
        self.entry_cell: List[int] = [int(entry_cell[0]), int(entry_cell[1]), int(entry_cell[2])]
        self.breadcrumbs: List[List[int]] = [list(self.entry_cell)]
        self.pillar_undo: Dict[int, List[List[int]]] = {}

    def push_step(
        self,
        pos: Sequence[int],
        *,
        placed_pillar_cells: Optional[Sequence[Sequence[int]]] = None,
    ) -> None:
        cell = [int(pos[0]), int(pos[1]), int(pos[2])]
        if placed_pillar_cells:
            self.pillar_undo[len(self.breadcrumbs)] = [list(pc) for pc in placed_pillar_cells]
            self.breadcrumbs.append(cell)
            return
        if cell in self.breadcrumbs:
            loop_idx = self.breadcrumbs.index(cell)
            if not any(pk > loop_idx for pk in self.pillar_undo):
                self.breadcrumbs = self.breadcrumbs[: loop_idx + 1]
                return
        self.breadcrumbs.append(cell)

    def sync_pos(self, pos: Sequence[int]) -> None:
        cell = [int(pos[0]), int(pos[1]), int(pos[2])]
        if cell in self.breadcrumbs:
            loop_idx = self.breadcrumbs.index(cell)
            if not any(pk > loop_idx for pk in self.pillar_undo):
                self.breadcrumbs = self.breadcrumbs[: loop_idx + 1]
        elif not self.breadcrumbs or self.breadcrumbs[-1] != cell:
            self.breadcrumbs.append(cell)


class WorldNavigator:
    """Single canonical executor for composite 3D steps and autonomous obstacle recovery."""

    @staticmethod
    def sync_live_pos(ctx: WorkflowContext) -> Optional[List[int]]:
        try:
            st = ctx.guard.check_status(require_safe=False)
            bp = parse_pos(st.get("pos"))
            if bp is not None:
                return [math.floor(bp[0] + 0.0001), math.floor(bp[1] + 0.0001), math.floor(bp[2] + 0.0001)]
        except WorkflowStepError:
            pass
        return None

    @classmethod
    def execute_step(
        cls,
        ctx: WorkflowContext,
        world: VoxelWorld3D,
        cur_pos: Sequence[int],
        step: Mapping[str, Any],
        read_fn: Callable[[Sequence[int]], Dict[str, Any]],
        *,
        cobweb_tool: Optional[str] = None,
        allow_micro_dig: bool = False,
        allow_dig_headroom_on_pillar: bool = False,
        trail: Optional[ReversibleTrail] = None,
        tolerance: float = 0.85,
    ) -> Tuple[List[int], bool]:
        """Execute one walk/dig/pillar/bridge/drop step, handle minor local obstacles, and update VoxelWorld3D.

        Returns `(new_pos, skipped_cobweb_unpassable)`.
        """
        nxt = [int(step["to"][0]), int(step["to"][1]), int(step["to"][2])]
        mode = str(step.get("mode") or "walk")
        cur_cell = [int(cur_pos[0]), int(cur_pos[1]), int(cur_pos[2])]

        # Clear cobweb at target foot/head only with sword or shears
        for wy in (nxt[1], nxt[1] + 1):
            wname = str(world.read_block((nxt[0], wy, nxt[2]), read_fn).get("name") or "air")
            if wname == "cobweb":
                if not cobweb_tool:
                    return cur_cell, True
                ctx.guard.write(ctx.guard.client.equip, item=cobweb_tool, slot="hand")
                aim_at(ctx, [nxt[0], wy, nxt[2]], relative=False)
                dig_block(ctx, [nxt[0], wy, nxt[2]], relative=False, allowed_blocks={"cobweb"}, collect=False)
                world.invalidate([nxt[0], wy, nxt[2]], radius=1)

        dig_allowed = set(CAVE_DIGGABLE_BLOCKS) | set(ORE_DROP_MAP)
        placed_pillar_cells: Optional[List[List[int]]] = None

        try:
            if mode == "pillar":
                for dcell in step.get("dig_cells") or ():
                    dig_block(ctx, dcell, relative=False, allowed_blocks=dig_allowed, collect=False)
                    world.invalidate(dcell, radius=1)
                pres = pillar_up(
                    ctx,
                    steps=int(step.get("pillar_steps", 1)),
                    blocks=SCAFFOLD_BLOCKS,
                    allow_dig_headroom=allow_dig_headroom_on_pillar,
                )
                placed_list = [list(pc) for pc in (pres.get("placed_cells") or ())]
                for pc in placed_list:
                    world.placed_scaffolds.add(norm_coord(pc))
                    world.invalidate(pc, radius=1)
                if placed_list:
                    placed_pillar_cells = placed_list
            elif mode == "bridge":
                bridge_rep = set(AIR) | set(HAZARDS) | set(DEFAULT_REPLACEABLE)
                for bcell in step.get("bridge_cells") or ():
                    place_block(ctx, SCAFFOLD_BLOCKS, bcell, relative=False, replaceable=bridge_rep)
                    world.placed_scaffolds.add(norm_coord(bcell))
                    world.invalidate(bcell, radius=1)
                stand_cell(ctx, nxt, tolerance=tolerance)
            elif mode == "scaffold_drop":
                def _inv(p: Sequence[int]) -> None:
                    world.invalidate(p, radius=1)
                dres = scaffold_drop(
                    ctx,
                    nxt,
                    step.get("step_cells") or (),
                    blocks=SCAFFOLD_BLOCKS,
                    invalidate=_inv,
                )
                for pc in (dres.get("placed_cells") or ()):
                    world.placed_scaffolds.add(norm_coord(pc))
            else:
                if mode == "dig":
                    for dcell in step.get("dig_cells") or ():
                        dig_block(ctx, dcell, relative=False, allowed_blocks=dig_allowed, collect=False)
                        world.invalidate(dcell, radius=1)
                try:
                    stand_cell(ctx, nxt, tolerance=tolerance)
                except WorkflowStepError as walk_exc:
                    # Autonomous micro-recovery: if moving stalled/obstructed due to an overlooked head/step overhang
                    # and micro-dig is permitted, inspect (cur+2y) & (nxt+1y), clear a single soft rock block, and retry once.
                    if allow_micro_dig and walk_exc.code in {"stalled", "path_not_found", "unreachable_stance", "target_obstructed"}:
                        world.invalidate(cur_cell, radius=1)
                        world.invalidate(nxt, radius=1)
                        cleared_any = False
                        cand_obs = []
                        if nxt[1] > cur_cell[1]:
                            cand_obs.append((cur_cell[0], cur_cell[1] + 2, cur_cell[2]))
                        cand_obs.append((nxt[0], nxt[1] + 1, nxt[2]))
                        cand_obs.append((nxt[0], nxt[1], nxt[2]))
                        for obs in cand_obs:
                            bdata = world.read_block(obs, read_fn)
                            bname = str(bdata.get("name") or "air")
                            if bname not in AIR and _can_dig_step_cell(obs, lambda p: world.read_block(p, read_fn)):
                                try:
                                    dig_block(ctx, obs, relative=False, allowed_blocks=dig_allowed, collect=False)
                                    world.invalidate(obs, radius=1)
                                    cleared_any = True
                                    break
                                except WorkflowStepError:
                                    pass
                        if cleared_any:
                            stand_cell(ctx, nxt, tolerance=tolerance)
                        else:
                            raise
                    else:
                        raise
        except WorkflowStepError as step_exc:
            if step_exc.code in STEP_RECOVERABLE_ERRORS:
                world.record_blocked_edge(cur_cell, nxt)
                world.invalidate(cur_cell, radius=1)
                world.invalidate(nxt, radius=1)
                snap = cls.sync_live_pos(ctx)
                if snap is not None and trail is not None:
                    trail.sync_pos(snap)
            raise

        world.clear_blocked_edges_near(nxt, radius=0)
        world.record_stance(nxt)
        if trail is not None:
            trail.push_step(nxt, placed_pillar_cells=placed_pillar_cells)
        return nxt, False

    @classmethod
    def follow_path(
        cls,
        ctx: WorkflowContext,
        world: VoxelWorld3D,
        start_pos: Sequence[int],
        path: Sequence[Mapping[str, Any]],
        read_fn: Callable[[Sequence[int]], Dict[str, Any]],
        *,
        cobweb_tool: Optional[str] = None,
        allow_micro_dig: bool = False,
        allow_dig_headroom_on_pillar: bool = False,
        trail: Optional[ReversibleTrail] = None,
        tolerance: float = 0.9,
    ) -> Tuple[bool, List[int]]:
        """Execute a multi-step 3D path sequentially; returns `(all_done, last_pos)`."""
        cur = [int(start_pos[0]), int(start_pos[1]), int(start_pos[2])]
        for step in path:
            if [int(step["to"][0]), int(step["to"][1]), int(step["to"][2])] == cur and str(step.get("mode") or "walk") != "pillar":
                continue
            try:
                cur, skipped = cls.execute_step(
                    ctx,
                    world,
                    cur,
                    step,
                    read_fn,
                    cobweb_tool=cobweb_tool,
                    allow_micro_dig=allow_micro_dig,
                    allow_dig_headroom_on_pillar=allow_dig_headroom_on_pillar,
                    trail=trail,
                    tolerance=tolerance,
                )
                if skipped:
                    return False, cur
            except WorkflowStepError as exc:
                if exc.code in STEP_RECOVERABLE_ERRORS:
                    snap = cls.sync_live_pos(ctx)
                    return False, (snap if snap is not None else cur)
                raise
        return True, cur

    @classmethod
    def retreat_to_entry(
        cls,
        ctx: WorkflowContext,
        world: VoxelWorld3D,
        trail: ReversibleTrail,
        entry_cell: Sequence[int],
        read_fn: Callable[[Sequence[int]], Dict[str, Any]],
        *,
        allow_pillar: bool = True,
        allow_dig: bool = True,
        allow_bridge: bool = True,
        allow_drop: bool = True,
        allow_surface_escape: bool = True,
        max_ascend: int = 48,
    ) -> Tuple[bool, bool]:
        """Retreat safely back to entry_cell via direct move -> 3D A* -> reversible breadcrumbs -> surface escape."""
        breadcrumbs = trail.breadcrumbs
        pillar_undo = trail.pillar_undo
        entry = [int(entry_cell[0]), int(entry_cell[1]), int(entry_cell[2])]

        if len(breadcrumbs) <= 1:
            return True, False

        # 1. Direct fast pathfinder move if Baritone/client can reach entry directly
        try:
            stand_cell(ctx, entry, tolerance=1.25)
            return True, False
        except WorkflowStepError as exc:
            if exc.code not in {"stalled", "path_not_found", "timeout", "unreachable_stance", "route_blocked"}:
                raise

        # 2. Directed 3D A* over known VoxelWorld3D (fast multi-chunk traversal across large caves)
        live = cls.sync_live_pos(ctx) or list(breadcrumbs[-1])
        for _ in range(10):
            if abs(live[0] - entry[0]) <= 1 and abs(live[1] - entry[1]) <= 1 and abs(live[2] - entry[2]) <= 1:
                try:
                    stand_cell(ctx, entry, tolerance=1.25)
                    return True, False
                except WorkflowStepError:
                    return True, False
            path = Pathfinder3D.find_path(
                world,
                live,
                entry,
                read_fn,
                goal_tolerance=0,
                allow_pillar=allow_pillar,
                allow_dig=allow_dig,
                allow_bridge=allow_bridge,
                allow_drop=allow_drop,
                max_nodes=2400,
                cache_only=False,
            )
            if not path:
                break
            ok, live = cls.follow_path(
                ctx,
                world,
                live,
                path[:14],
                read_fn,
                allow_micro_dig=allow_dig,
                allow_dig_headroom_on_pillar=allow_dig,
                tolerance=0.9,
            )
            if not ok:
                live = cls.sync_live_pos(ctx) or live

        # 3. Reverse unwind along exact recorded breadcrumbs & placed pillars
        for idx in range(len(breadcrumbs) - 1, 0, -1):
            try:
                placed = pillar_undo.get(idx)
                if placed:
                    pillar_down(ctx, placed)
                    for pc in placed:
                        world.invalidate(pc, radius=1)
                    if idx == 1 or list(breadcrumbs[idx - 1]) != entry:
                        stand_cell(ctx, breadcrumbs[idx - 1], tolerance=0.9)
                else:
                    stand_cell(ctx, breadcrumbs[idx - 1], tolerance=0.9)
            except WorkflowStepError:
                live_err = cls.sync_live_pos(ctx) or list(breadcrumbs[idx])
                path_esc = Pathfinder3D.find_path(
                    world,
                    live_err,
                    entry,
                    read_fn,
                    goal_tolerance=1,
                    allow_pillar=allow_pillar,
                    allow_dig=allow_dig,
                    allow_bridge=allow_bridge,
                    allow_drop=allow_drop,
                    max_nodes=1800,
                )
                if path_esc:
                    ok, _ = cls.follow_path(
                        ctx,
                        world,
                        live_err,
                        path_esc,
                        read_fn,
                        allow_micro_dig=allow_dig,
                        allow_dig_headroom_on_pillar=allow_dig,
                        tolerance=0.95,
                    )
                    if ok:
                        return True, False
                if allow_surface_escape:
                    try:
                        esc = escape_to_surface(
                            ctx,
                            max_ascend=max_ascend,
                            blocks=SCAFFOLD_BLOCKS,
                            invalidate=lambda p: world.invalidate(p, radius=1),
                        )
                        return False, bool(esc.get("escaped"))
                    except WorkflowStepError:
                        return False, False
                return False, False

        live_end = cls.sync_live_pos(ctx)
        if live_end is not None and (
            abs(live_end[0] - entry[0]) > 1
            or abs(live_end[1] - entry[1]) > 1
            or abs(live_end[2] - entry[2]) > 1
        ):
            try:
                stand_cell(ctx, entry, tolerance=1.25)
            except WorkflowStepError:
                if allow_surface_escape:
                    try:
                        esc = escape_to_surface(
                            ctx,
                            max_ascend=max_ascend,
                            blocks=SCAFFOLD_BLOCKS,
                            invalidate=lambda p: world.invalidate(p, radius=1),
                        )
                        return False, bool(esc.get("escaped"))
                    except WorkflowStepError:
                        return False, False
                return False, False
        return True, False
