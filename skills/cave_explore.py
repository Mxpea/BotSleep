"""Independent bounded cave/mineshaft exploration and targeted ore mining skill."""

from collections import deque
import math
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from core.cave_probe import (
    FALLING_BLOCKS,
    classify_cavity,
    flood_safe_cave_graph,
    is_lava_like,
    is_passable,
    is_solid_floor,
    is_water_like,
    lateral_air_clearance,
    ore_cell_hazard_free,
    probe_cavity,
    reachable_exposed_ores,
    reconstruct_flood_path,
    walkable_neighbors,
)
from core.domain import AIR, HAZARDS, parse_pos
from core.memory import MemoryEngine
from core.mining_targets import (
    DEFAULT_EDIBLE_FOODS,
    DEFAULT_MINING_TARGETS,
    best_usable_pickaxe,
    evaluate_home_trigger,
    evaluate_ore_block,
    parse_mining_targets,
    pickaxe_tier,
)
from core.structure_spec import (
    CAVE_DIGGABLE_BLOCKS,
    CAVE_LIMITS,
    DEFAULT_REPLACEABLE,
    MINE_DIGGABLE_BLOCKS,
    MINE_SHAFT_SPEC,
    MINE_TORCH_BLOCKS,
    ORE_DROP_MAP,
    SCAFFOLD_BLOCKS,
)
from skills.action_workflow import (
    PICKAXE_PRIORITY,
    WorkflowContext,
    WorkflowGuard,
    WorkflowStepError,
    aim_at,
    consume,
    dig_block,
    escape_to_surface,
    pillar_down,
    pillar_up,
    place_block,
    place_torch,
    scaffold_drop,
    stand_cell,
)
from . import BaseSkill, SkillResult

SWORD_OR_SHEARS: Tuple[str, ...] = (
    "shears",
    "netherite_sword",
    "diamond_sword",
    "iron_sword",
    "stone_sword",
    "wooden_sword",
    "golden_sword",
)


class CaveExploreSkill(BaseSkill):
    """Safely explore bounded caves or abandoned mineshafts, light paths, mine target ores, and retreat along verified breadcrumbs."""

    def __init__(self, client, memory: Optional[MemoryEngine] = None):
        super().__init__(client)
        self.memory = memory or MemoryEngine()

    def validate_preconditions(self, **kwargs) -> bool:
        """Verify bot is connected and ready."""
        raw = self.client.get_status()
        st = raw.get("result") if isinstance(raw, dict) and "result" in raw else raw
        return bool(isinstance(st, dict) and st.get("ready"))

    def execute(
        self,
        entry_pos: Optional[Sequence[int]] = None,
        *,
        origin_air: Optional[Sequence[int]] = None,
        targets: Optional[Mapping[str, Any]] = None,
        cave_policy: Optional[Mapping[str, Any]] = None,
        home_thresholds: Optional[Mapping[str, Any]] = None,
        harvested_totals: Optional[Mapping[str, int]] = None,
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: Optional[float] = None,
        allow_damage: bool = True,
        critical_health: float = 6.0,
    ) -> SkillResult:
        """Standalone cave exploration entrypoint around entry_pos (defaults to current bot cell)."""
        return self.explore_cave(
            entry_pos=entry_pos,
            origin_air=origin_air,
            targets=targets,
            cave_policy=cave_policy,
            home_thresholds=home_thresholds,
            harvested_totals=harvested_totals,
            cancel_event=cancel_event,
            timeout_seconds=timeout_seconds,
            allow_damage=allow_damage,
            critical_health=critical_health,
            allow_tall_cave_subgraph=True,
            allow_pillar=True,
            allow_micro_dig=True,
            allow_bridge=True,
            allow_drop=True,
            allow_surface_escape=True,
        )

    def explore_cave(
        self,
        entry_pos: Optional[Sequence[int]] = None,
        *,
        origin_air: Optional[Sequence[int]] = None,
        targets: Optional[Mapping[str, Any]] = None,
        cave_policy: Optional[Mapping[str, Any]] = None,
        home_thresholds: Optional[Mapping[str, Any]] = None,
        harvested_totals: Optional[Mapping[str, int]] = None,
        existing_report: Optional[Mapping[str, Any]] = None,
        existing_classification: Optional[Mapping[str, Any]] = None,
        block_cache: Optional[Dict[Tuple[int, int, int], Dict[str, Any]]] = None,
        blocked_backtracks: Optional[Sequence[Sequence[int]]] = None,
        covered_cells: Optional[Set[Tuple[int, int, int]]] = None,
        guard: Optional[WorkflowGuard] = None,
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: Optional[float] = None,
        allow_damage: bool = True,
        critical_health: float = 6.0,
        prefer_flood_planner: bool = True,
        allow_tall_cave_subgraph: bool = False,
        allow_pillar: bool = False,
        allow_micro_dig: bool = False,
        allow_bridge: bool = False,
        allow_drop: bool = False,
        allow_surface_escape: bool = False,
    ) -> SkillResult:
        """Probe (if needed) and explore one cave/mineshaft from entry_pos, returning along breadcrumbs."""
        policy = dict(CAVE_LIMITS)
        if cave_policy:
            policy.update(cave_policy)
        timeout = float(timeout_seconds if timeout_seconds is not None else policy["explore_timeout_seconds"])

        own_guard = guard is None
        active_guard = guard or WorkflowGuard(
            self.client,
            cancel_event=cancel_event,
            timeout_seconds=timeout,
            max_reads=150000,
            allow_damage=allow_damage,
            critical_health=critical_health,
        )
        cache: Dict[Tuple[int, int, int], Dict[str, Any]] = block_cache if block_cache is not None else {}

        ctx = WorkflowContext(
            guard=active_guard,
            spec=MINE_SHAFT_SPEC,
            origin=[0, 0, 0],
            orientation="south",
            extra={},
        )

        def cached_read(pos: Sequence[int]) -> Dict[str, Any]:
            key = (int(pos[0]), int(pos[1]), int(pos[2]))
            if key not in cache:
                cache[key] = ctx.read_block(key)
            return cache[key]

        def invalidate(pos: Sequence[int]) -> None:
            px, py, pz = int(pos[0]), int(pos[1]), int(pos[2])
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        cache.pop((px + dx, py + dy, pz + dz), None)

        # Resolve entry_pos
        try:
            status = active_guard.check_status(require_safe=True)
        except WorkflowStepError as exc:
            return SkillResult(
                False,
                str(exc),
                error_code=exc.code,
                data={"caves": [], "ores": [], "harvested": {}, "torches_placed": 0, "stop_reason": "safety_interrupted"},
            )

        if entry_pos is None:
            bot_p = parse_pos(status.get("pos"))
            if bot_p is None:
                return SkillResult(False, "Invalid bot position", error_code="invalid_state")
            entry_cell = [int(math.floor(bot_p[0])), int(math.floor(bot_p[1])), int(math.floor(bot_p[2]))]
        else:
            parsed_entry = parse_pos(entry_pos, integer=True)
            if parsed_entry is None:
                return SkillResult(False, "entry_pos must be 3 integers", error_code="bad_args")
            entry_cell = list(parsed_entry)

        # Determine initial inventory snapshot (and hand-craft torches if materials are already on hand)
        before_counts, inv_items = self._read_inventory(ctx)
        if before_counts.get("torch", 0) < 4:
            if self._try_craft_torches(ctx, before_counts):
                before_counts, inv_items = self._read_inventory(ctx)
        torch_count = before_counts.get("torch", 0)
        parsed_targets = parse_mining_targets(targets if targets is not None else DEFAULT_MINING_TARGETS)

        # Resolve origin_air & classification if standalone call
        report = dict(existing_report) if existing_report else None
        classification = dict(existing_classification) if existing_classification else None
        if report is None:
            air_seed = parse_pos(origin_air, integer=True) if origin_air is not None else self._find_adjacent_air(entry_cell, cached_read)
            if air_seed is None:
                return SkillResult(
                    False,
                    f"No open cavity found adjacent to {entry_cell}.",
                    error_code="cave_route_blocked",
                    data={"caves": [], "ores": [], "harvested": {}, "torches_placed": 0, "stop_reason": "cave_route_blocked"},
                )
            report = probe_cavity(air_seed, entry_cell, cached_read, limits=policy, block_cache=cache)
        if classification is None:
            classification = classify_cavity(report, torch_count=torch_count, limits=policy)

        if (
            allow_tall_cave_subgraph
            and classification["decision"] != "enter"
            and classification.get("kind") in {"tall_cave", "deep_drop", "lava", "water_cave"}
            and not report.get("spawner_found")
            and (classification.get("kind") != "lava" or allow_bridge)
        ):
            sub_graph = flood_safe_cave_graph(
                entry_cell,
                cached_read,
                max_nodes=max(120, int(policy["explore_max_visited"])),
                max_radius=int(policy["explore_radius"]),
                allow_pillar=allow_pillar,
                allow_dig=allow_micro_dig,
                allow_bridge=allow_bridge,
                allow_drop=allow_drop,
                targets=parsed_targets,
                harvested_counts=harvested_totals or {},
                best_tool_tier=pickaxe_tier(best_usable_pickaxe(inv_items)),
                eye_radius=float(policy["eye_scan_radius"]),
                block_cache=cache,
            )
            if len(sub_graph.get("nodes") or []) >= 3 or sub_graph.get("ore_targets"):
                sub_kind = "tall_subgraph" if classification.get("kind") in {"tall_cave", "deep_drop"} else "hazard_subgraph"
                classification = {
                    "decision": "enter",
                    "kind": sub_kind,
                    "reason": f"Flood planner verified {len(sub_graph['nodes'])} safe subgraph nodes",
                }

        cave_entry = {
            "pos": list(report["origin"]),
            "decision": "entered" if classification["decision"] == "enter" else "unsafe",
            "kind": str(classification["kind"]),
            "height": int(report.get("height", 0)),
            "drop": int(report.get("drop", 0)),
        }

        if classification["decision"] != "enter":
            err = "missing_torch" if classification["kind"] == "missing_torch" else "cave_route_blocked"
            return SkillResult(
                False,
                str(classification.get("reason") or "Cavity rejected as unsafe"),
                error_code=err,
                data={
                    "caves": [cave_entry],
                    "ores": [],
                    "harvested": {},
                    "torches_placed": 0,
                    "stop_reason": "cave_route_blocked",
                    "classification": classification,
                    "report": report,
                },
            )

        # Enter & explore inside bounded limits
        running_harvested: Dict[str, int] = dict(harvested_totals or {})
        ore_records: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
        torches_placed = 0
        stop_reason = "cave_done"
        home_reason: Optional[str] = None
        junction_unsafe = False

        explore_deadline = time.monotonic() + float(policy["explore_timeout_seconds"])
        explore_radius = int(policy["explore_radius"])
        max_visited = int(policy["explore_max_visited"])
        torch_interval = int(policy["torch_interval"])
        max_vein = int(policy["max_vein_blocks"])
        eye_radius = float(policy["eye_scan_radius"])

        breadcrumbs: List[List[int]] = [list(entry_cell)]
        pillar_undo: Dict[int, List[List[int]]] = {}
        visited: Set[Tuple[int, int, int]] = {tuple(entry_cell)}
        covered: Set[Tuple[int, int, int]] = covered_cells if covered_cells is not None else set()
        for bpos in (blocked_backtracks or ()):
            bp_t = (int(bpos[0]), int(bpos[1]), int(bpos[2]))
            visited.add(bp_t)
            covered.add(bp_t)
        cur_pos = list(entry_cell)

        def _mark_covered(center_pos: Sequence[int]) -> None:
            mx, my, mz = int(center_pos[0]), int(center_pos[1]), int(center_pos[2])
            for dx in (-2, -1, 0, 1, 2):
                for dy in (-1, 0, 1):
                    for dz in (-2, -1, 0, 1, 2):
                        if abs(dx) + abs(dz) <= 3:
                            covered.add((mx + dx, my + dy, mz + dz))

        _mark_covered(entry_cell)

        def _refresh_ore_hints(center_pos: Sequence[int]) -> List[List[int]]:
            fb = getattr(ctx.guard.client, "find_blocks", None) or getattr(ctx.guard.client, "get_blocks", None)
            if not callable(fb) or not parsed_targets:
                return []
            want_blocks: List[str] = []
            for spec in parsed_targets.values():
                want_blocks.extend(spec.get("ore_blocks") or ())
            if not want_blocks:
                return []
            try:
                rows = fb(types=want_blocks, radius=min(32, explore_radius + 4), limit=20)
                return [r["pos"] for r in (rows or []) if isinstance(r, dict) and isinstance(r.get("pos"), list) and len(r["pos"]) == 3]
            except Exception:
                return []

        ore_hints: List[List[int]] = _refresh_ore_hints(entry_cell)
        hint_refresh_step = 0

        # Step 1: Light the junction with an entry torch if available (every 24 blocks thereafter)
        torch_spacing = max(4, int(round(torch_interval * 0.75)))
        placed_torch_cells: List[Tuple[int, int, int]] = []
        if torch_count > 0 and self._place_near_torch(ctx, entry_cell, cached_read, invalidate, min_spacing=min(6, torch_spacing)):
            torches_placed += 1
            placed_torch_cells.append((int(entry_cell[0]), int(entry_cell[1]), int(entry_cell[2])))

        steps_since_torch = 0
        prev_dir: Optional[Tuple[int, int]] = None
        planned_queue: List[Dict[str, Any]] = []
        last_graph_at_visited = -1
        consecutive_step_failures = 0

        try:
            while (
                len(visited) < max_visited
                and time.monotonic() < explore_deadline
                and active_guard.read_calls < max(500, active_guard.max_reads - 1500)
            ):
                if not own_guard:
                    active_guard.check_budget()
                cur_status = active_guard.check_status(require_safe=True)
                cur_counts, cur_items = self._read_inventory(ctx)

                # Heal minor chip damage/hunger in-place with food instead of abandoning the cave
                if self._try_eat_food(ctx, cur_status, cur_counts):
                    cur_status = active_guard.check_status(require_safe=True)
                    cur_counts, cur_items = self._read_inventory(ctx)

                # Stop early only when genuinely unable to continue (bag full, no pickaxe, or critical low health)
                home_eval = evaluate_home_trigger(
                    cur_status,
                    cur_items,
                    remaining_seconds=min(active_guard.remaining_timeout(999.0), explore_deadline - time.monotonic()),
                    thresholds=home_thresholds,
                )
                if home_eval["trigger"] and home_eval["home_reason"] in {"inventory_full", "low_health", "no_pickaxe"}:
                    stop_reason = "return_home"
                    home_reason = home_eval["home_reason"]
                    break
                if home_eval.get("wrap_up"):
                    break

                # Sync persistent quota progress from actual inventory delta + previous mined count
                base_totals = harvested_totals or {}
                for k in set(base_totals) | set(cur_counts) | set(before_counts):
                    inv_progress = int(base_totals.get(k, 0)) + max(0, cur_counts.get(k, 0) - before_counts.get(k, 0))
                    if inv_progress > running_harvested.get(k, 0):
                        running_harvested[k] = inv_progress
                t_dig, _ = self._mine_reachable_ores(
                    ctx,
                    cur_pos,
                    cur_items,
                    parsed_targets,
                    running_harvested,
                    cached_read,
                    invalidate,
                    ore_records,
                    max_dist=eye_radius,
                    max_vein=max_vein,
                )
                if t_dig:
                    # Re-check safety and resync live cell after mining/drop collection
                    active_guard.check_status(require_safe=True)
                    snap_dig = self._sync_live_pos(ctx)
                    if snap_dig is not None:
                        if snap_dig != cur_pos:
                            if list(snap_dig) in breadcrumbs:
                                loop_idx = breadcrumbs.index(list(snap_dig))
                                if not any(pk > loop_idx for pk in pillar_undo):
                                    breadcrumbs = breadcrumbs[: loop_idx + 1]
                            else:
                                breadcrumbs.append(list(snap_dig))
                        cur_pos = snap_dig
                        visited.add(tuple(cur_pos))
                        _mark_covered(cur_pos)

                has_web_tool = any(cur_counts.get(w, 0) > 0 for w in SWORD_OR_SHEARS)
                raw_neighbors = walkable_neighbors(
                    cur_pos,
                    cached_read,
                    allow_cobweb=has_web_tool,
                    block_cache=cache,
                )
                unvisited = [
                    n for n in raw_neighbors
                    if tuple(n) not in visited
                    and abs(n[0] - entry_cell[0]) <= explore_radius
                    and abs(n[2] - entry_cell[2]) <= explore_radius
                ]
                uncovered_adj = [n for n in unvisited if tuple(n) not in covered]
                for group in (uncovered_adj, unvisited):
                    group.sort(
                        key=lambda n: (
                            -lateral_air_clearance(n, cached_read, block_cache=cache),
                            0 if (prev_dir is not None and (n[0] - cur_pos[0], n[2] - cur_pos[2]) == prev_dir) else 1,
                            abs(n[1] - cur_pos[1]),
                        )
                    )

                planned_step: Optional[Dict[str, Any]] = None
                while planned_queue:
                    cand_step = planned_queue.pop(0)
                    if list(cand_step.get("to") or []) != cur_pos or str(cand_step.get("mode") or "walk") != "walk":
                        planned_step = cand_step
                        break
                    visited.add(tuple(cur_pos))
                    _mark_covered(cur_pos)

                if planned_step is None:
                    hint_refresh_step += 1
                    if hint_refresh_step % 4 == 0:
                        ore_hints = _refresh_ore_hints(cur_pos)
                        if ore_hints:
                            ore_hints.sort(
                                key=lambda hp: abs(hp[0] - cur_pos[0]) + abs(hp[1] - cur_pos[1]) + abs(hp[2] - cur_pos[2])
                            )
                    use_graph = prefer_flood_planner and (len(visited) != last_graph_at_visited)
                    if use_graph:
                        last_graph_at_visited = len(visited)
                        has_scaffold = sum(cur_counts.get(s, 0) for s in SCAFFOLD_BLOCKS) >= 4
                        protected: Set[Tuple[int, int, int]] = set()
                        for plist in pillar_undo.values():
                            for pc in plist:
                                px, py, pz = int(pc[0]), int(pc[1]), int(pc[2])
                                protected.add((px, py, pz))
                                protected.add((px, py - 1, pz))

                        def _run_graph_scan(node_budget: int) -> Dict[str, Any]:
                            return flood_safe_cave_graph(
                                cur_pos,
                                cached_read,
                                max_nodes=node_budget,
                                max_radius=max(explore_radius, abs(cur_pos[0] - entry_cell[0]) + explore_radius, abs(cur_pos[2] - entry_cell[2]) + explore_radius),
                                allow_pillar=bool(allow_pillar and has_scaffold),
                                max_pillar_height=4,
                                allow_dig=allow_micro_dig,
                                max_dig_steps=3,
                                allow_bridge=bool(allow_bridge and has_scaffold),
                                max_bridge_steps=4,
                                allow_drop=allow_drop,
                                max_drop_steps=5,
                                allow_cobweb=has_web_tool,
                                targets=parsed_targets,
                                harvested_counts=running_harvested,
                                best_tool_tier=pickaxe_tier(best_usable_pickaxe(cur_items)),
                                eye_radius=eye_radius,
                                visited_cells=visited,
                                covered_cells=covered,
                                goal_hints=ore_hints,
                                protected_cells=protected,
                                anchor_pos=entry_cell,
                                anchor_radius=explore_radius,
                                block_cache=cache,
                            )

                        def _pick_graph_plan(graph_obj: Dict[str, Any], max_q: int) -> Optional[Dict[str, Any]]:
                            for row in graph_obj.get("ore_targets") or ():
                                path = row.get("path") or []
                                if not path:
                                    continue
                                if all(
                                    abs(s["to"][0] - entry_cell[0]) <= explore_radius and abs(s["to"][2] - entry_cell[2]) <= explore_radius
                                    for s in path
                                ):
                                    planned_queue.extend(path[1:max_q])
                                    return path[0]
                            for frow in graph_obj.get("frontiers") or ():
                                path = frow.get("path") or []
                                if not path:
                                    continue
                                if all(
                                    abs(s["to"][0] - entry_cell[0]) <= explore_radius and abs(s["to"][2] - entry_cell[2]) <= explore_radius
                                    for s in path
                                ):
                                    planned_queue.extend(path[1:max_q])
                                    return path[0]
                            return None

                        remaining_reads = max(0, int(active_guard.max_reads) - int(active_guard.read_calls) - 1000)
                        read_cap_nodes = max(120, remaining_reads // 12)
                        local_budget = min(700, max(180, int(max_visited) * 2), read_cap_nodes)
                        graph = _run_graph_scan(local_budget)
                        planned_step = _pick_graph_plan(graph, 20)
                        # Exhaustive escalation: when local scan finds no frontiers (dead-end/corner),
                        # flood up to 4500 cached 3D nodes across the entire connected cavern when read budget allows.
                        if planned_step is None and not uncovered_adj and remaining_reads >= 12000:
                            esc_cap = max(local_budget, remaining_reads // 10)
                            global_budget = min(4500, max(1600, int(max_visited) * 8), esc_cap)
                            if global_budget > local_budget and len(graph.get("nodes") or []) >= local_budget - 5:
                                graph_full = _run_graph_scan(global_budget)
                                planned_step = _pick_graph_plan(graph_full, 48)
                        if planned_step is None and uncovered_adj:
                            planned_step = {"mode": "walk", "to": uncovered_adj[0]}
                    elif uncovered_adj:
                        planned_step = {"mode": "walk", "to": uncovered_adj[0]}
                    elif not prefer_flood_planner and unvisited:
                        planned_step = {"mode": "walk", "to": unvisited[0]}

                if planned_step is None:
                    # If flood planner already ran at this state and found no in-bounds ores/frontiers,
                    # don't blindly walk backwards 1-by-1 across an empty room; finish cleanly to fast retreat.
                    top_idx = len(breadcrumbs) - 1
                    if (not prefer_flood_planner or last_graph_at_visited == len(visited)) and (
                        not pillar_undo or top_idx not in pillar_undo
                    ):
                        break
                    if len(breadcrumbs) <= 1:
                        break
                    placed_here = pillar_undo.pop(top_idx, None)
                    try:
                        if placed_here:
                            pillar_down(ctx, placed_here)
                            for pc in placed_here:
                                invalidate(pc)
                        else:
                            stand_cell(ctx, breadcrumbs[-2], tolerance=0.9)
                    except WorkflowStepError as bt_err:
                        if bt_err.code not in {"stalled", "path_not_found", "timeout", "unreachable_stance", "route_blocked"}:
                            raise
                        snap = self._sync_live_pos(ctx)
                        if snap is not None:
                            cur_pos = snap
                        invalidate(cur_pos)
                        if not self._recover_step_with_graph(
                            ctx,
                            cur_pos,
                            breadcrumbs[-2],
                            cached_read,
                            invalidate,
                            cache,
                            allow_pillar=allow_pillar,
                            allow_dig=allow_micro_dig,
                            allow_bridge=allow_bridge,
                            allow_drop=allow_drop,
                        ):
                            break
                    breadcrumbs.pop()
                    cur_pos = self._sync_live_pos(ctx) or list(breadcrumbs[-1])
                    prev_dir = None
                    continue

                nxt = list(planned_step["to"])
                mode = str(planned_step.get("mode") or "walk")
                if nxt == cur_pos and mode != "pillar":
                    visited.add(tuple(nxt))
                    _mark_covered(nxt)
                    continue
                step_dir = (nxt[0] - cur_pos[0], nxt[2] - cur_pos[2])

                # Clear cobweb at target foot/head only with sword or shears
                for wy in (nxt[1], nxt[1] + 1):
                    wname = str(cached_read((nxt[0], wy, nxt[2])).get("name") or "air")
                    if wname == "cobweb":
                        tool = next((w for w in SWORD_OR_SHEARS if cur_counts.get(w, 0) > 0), None)
                        if tool is None:
                            nxt = None
                            break
                        ctx.guard.write(ctx.guard.client.equip, item=tool, slot="hand")
                        aim_at(ctx, [nxt[0], wy, nxt[2]], relative=False)
                        dig_block(ctx, [nxt[0], wy, nxt[2]], relative=False, allowed_blocks={"cobweb"}, collect=False)
                        invalidate([nxt[0], wy, nxt[2]])
                if nxt is None:
                    visited.add(tuple(planned_step["to"]))
                    continue

                old_pos = list(cur_pos)
                try:
                    if mode == "pillar":
                        for dcell in planned_step.get("dig_cells") or ():
                            dig_block(ctx, dcell, relative=False, allowed_blocks=set(CAVE_DIGGABLE_BLOCKS) | set(ORE_DROP_MAP), collect=False)
                            invalidate(dcell)
                        pres = pillar_up(ctx, steps=int(planned_step.get("pillar_steps", 1)), blocks=SCAFFOLD_BLOCKS, allow_dig_headroom=allow_micro_dig)
                        for pc in pres.get("placed_cells") or ():
                            invalidate(pc)
                        if pres.get("placed_cells"):
                            pillar_undo[len(breadcrumbs)] = list(pres["placed_cells"])
                    elif mode == "bridge":
                        bridge_rep = set(AIR) | set(HAZARDS) | set(DEFAULT_REPLACEABLE)
                        for bcell in planned_step.get("bridge_cells") or ():
                            place_block(ctx, SCAFFOLD_BLOCKS, bcell, relative=False, replaceable=bridge_rep)
                            invalidate(bcell)
                        stand_cell(ctx, nxt, tolerance=0.85)
                    elif mode == "scaffold_drop":
                        scaffold_drop(
                            ctx,
                            nxt,
                            planned_step.get("step_cells") or (),
                            blocks=SCAFFOLD_BLOCKS,
                            invalidate=invalidate,
                        )
                    else:
                        if mode == "dig":
                            for dcell in planned_step.get("dig_cells") or ():
                                dig_block(ctx, dcell, relative=False, allowed_blocks=set(CAVE_DIGGABLE_BLOCKS) | set(ORE_DROP_MAP), collect=False)
                                invalidate(dcell)
                        stand_cell(ctx, nxt, tolerance=0.85)
                except WorkflowStepError as step_exc:
                    if step_exc.code in {
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
                    }:
                        visited.add(tuple(nxt))
                        planned_queue.clear()
                        consecutive_step_failures += 1
                        invalidate(nxt)
                        snap = self._sync_live_pos(ctx)
                        if snap is not None:
                            cur_pos = snap
                            invalidate(cur_pos)
                            if list(cur_pos) in breadcrumbs:
                                loop_idx = breadcrumbs.index(list(cur_pos))
                                if not any(pk > loop_idx for pk in pillar_undo):
                                    breadcrumbs = breadcrumbs[: loop_idx + 1]
                            elif not breadcrumbs or breadcrumbs[-1] != cur_pos:
                                breadcrumbs.append(list(cur_pos))
                        if consecutive_step_failures >= 6:
                            break
                        continue
                    raise

                consecutive_step_failures = 0
                if list(nxt) in breadcrumbs:
                    loop_idx = breadcrumbs.index(list(nxt))
                    if not any(pk > loop_idx for pk in pillar_undo):
                        breadcrumbs = breadcrumbs[: loop_idx + 1]
                    else:
                        breadcrumbs.append(list(nxt))
                else:
                    breadcrumbs.append(list(nxt))
                visited.add(tuple(nxt))
                _mark_covered(nxt)
                cur_pos = list(nxt)
                steps_since_torch += 1
                if step_dir != (0, 0):
                    prev_dir = step_dir

                if mode != "pillar" and steps_since_torch >= torch_interval:
                    too_close = any(
                        math.hypot(old_pos[0] - tx, old_pos[2] - tz) < torch_spacing and abs(old_pos[1] - ty) <= 4
                        for tx, ty, tz in placed_torch_cells
                    )
                    if not too_close and cur_counts.get("torch", 0) > 0 and self._place_near_torch(
                        ctx,
                        old_pos,
                        cached_read,
                        invalidate,
                        forbid_cell=cur_pos,
                        min_spacing=min(6, torch_spacing),
                    ):
                        torches_placed += 1
                        steps_since_torch = 0
                        placed_torch_cells.append((int(old_pos[0]), int(old_pos[1]), int(old_pos[2])))

        except WorkflowStepError as exc:
            stop_reason = "safety_interrupted" if exc.code in {"damage_taken", "unsafe_state", "hazard_detected", "hazard_adjacent", "life_changed"} else "cave_route_blocked"

        # Always backtrack along verified breadcrumbs; if blocked, replan with cached flood graph or pillar to surface.
        escaped_surface = False
        try:
            retreat_ok = self._retreat_breadcrumbs(
                ctx,
                breadcrumbs,
                entry_cell,
                pillar_undo=pillar_undo,
                invalidate=invalidate,
                read_fn=cached_read,
                block_cache=cache,
                allow_pillar=allow_pillar,
                allow_dig=allow_micro_dig,
                allow_bridge=allow_bridge,
                allow_drop=allow_drop,
            )
        except WorkflowStepError:
            retreat_ok = False
        if not retreat_ok:
            if allow_surface_escape:
                try:
                    esc = escape_to_surface(ctx, max_ascend=48, blocks=SCAFFOLD_BLOCKS, invalidate=invalidate)
                    escaped_surface = bool(esc.get("escaped"))
                except WorkflowStepError:
                    escaped_surface = False
            if not escaped_surface:
                stop_reason = "safety_interrupted"

        # Verify whether entry_cell itself remained hazard-free
        invalidate(entry_cell)
        for check_y in (entry_cell[1], entry_cell[1] + 1):
            for dx, dy, dz in ((0, 0, 0), (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                blk = cached_read((entry_cell[0] + dx, check_y + dy, entry_cell[2] + dz))
                if is_lava_like(blk) or is_water_like(blk) or str(blk.get("name") or "") == "spawner":
                    junction_unsafe = True
                    break

        after_counts, after_items = self._read_inventory(ctx)
        harvested = {
            k: after_counts[k] - before_counts.get(k, 0)
            for k in after_counts
            if after_counts[k] > before_counts.get(k, 0)
        }

        if stop_reason == "cave_done":
            try:
                end_status = active_guard.check_status(require_safe=True)
                home_eval = evaluate_home_trigger(
                    end_status,
                    after_items,
                    remaining_seconds=active_guard.remaining_timeout(999.0),
                    thresholds=home_thresholds,
                )
                urgent_reasons = {"inventory_full", "no_pickaxe", "low_health", "low_food"}
                if int(policy.get("min_enter_torches", 0)) > 0:
                    urgent_reasons.add("low_torch")
                if home_eval["trigger"] and home_eval["home_reason"] in urgent_reasons:
                    stop_reason = "return_home"
                    home_reason = home_eval["home_reason"]
            except WorkflowStepError:
                stop_reason = "safety_interrupted"

        ores_out = sorted(ore_records.values(), key=lambda r: (r["pos"][1], r["pos"][0], r["pos"][2]))
        success = stop_reason in {"cave_done", "return_home"} or escaped_surface
        return SkillResult(
            success,
            f"Explored {cave_entry['kind']} ({len(visited)} cells, {torches_placed} torches, stop={stop_reason})",
            error_code=None if success else stop_reason,
            data={
                "caves": [cave_entry],
                "ores": ores_out,
                "harvested": harvested,
                "torches_placed": torches_placed,
                "stop_reason": stop_reason,
                "home_reason": home_reason,
                "junction_unsafe": junction_unsafe,
                "escaped_to_surface": escaped_surface,
                "visited_count": len(visited),
                "report": report,
                "classification": classification,
            },
        )

    @staticmethod
    def _sync_live_pos(ctx: WorkflowContext) -> Optional[List[int]]:
        try:
            st = ctx.guard.check_status(require_safe=False)
            bp = parse_pos(st.get("pos"))
            if bp is not None:
                return [math.floor(bp[0] + 0.0001), math.floor(bp[1] + 0.0001), math.floor(bp[2] + 0.0001)]
        except WorkflowStepError:
            pass
        return None

    @staticmethod
    def _try_craft_torches(ctx: WorkflowContext, counts: Mapping[str, int]) -> bool:
        """Auto-craft torches in 2x2 inventory when coal/charcoal and sticks/wood exist."""
        craft_fn = getattr(ctx.guard.client, "craft", None)
        if not callable(craft_fn):
            return False
        if not (counts.get("coal", 0) > 0 or counts.get("charcoal", 0) > 0):
            return False
        has_sticks = counts.get("stick", 0) > 0
        if not has_sticks:
            wood_planks = next((k for k in counts if k.endswith("_planks") and counts[k] >= 2), None)
            if wood_planks is None:
                return False
            try:
                ctx.guard.write(craft_fn, "stick", 4)
            except WorkflowStepError:
                return False
        try:
            ctx.guard.write(craft_fn, "torch", 4)
            return True
        except WorkflowStepError:
            return False

    @staticmethod
    def _try_eat_food(ctx: WorkflowContext, status: Mapping[str, Any], counts: Mapping[str, int]) -> bool:
        """Eat edible food inside the cave when hurt or hungry instead of running away."""
        health = status.get("health")
        food = status.get("food")
        need_heal = isinstance(health, (int, float)) and not isinstance(health, bool) and float(health) < 17.0
        can_eat_food = isinstance(food, (int, float)) and not isinstance(food, bool) and float(food) < 20.0
        hungry = isinstance(food, (int, float)) and not isinstance(food, bool) and float(food) < 14.0
        if not ((need_heal and can_eat_food) or hungry):
            return False
        food_item = next((f for f in DEFAULT_EDIBLE_FOODS if counts.get(f, 0) > 0), None)
        if food_item is None or not callable(getattr(ctx.guard.client, "use_item", None)):
            return False
        try:
            consume(ctx, food_item)
            return True
        except WorkflowStepError:
            return False

    @staticmethod
    def _find_adjacent_air(entry_cell: Sequence[int], read_fn: Callable[[Sequence[int]], Dict[str, Any]]) -> Optional[List[int]]:
        ex, ey, ez = int(entry_cell[0]), int(entry_cell[1]), int(entry_cell[2])
        for dx, dz in ((0, 1), (1, 0), (-1, 0), (0, -1)):
            for dy in (0, 1):
                cand = [ex + dx, ey + dy, ez + dz]
                if str(read_fn(cand).get("name") or "unknown") in AIR:
                    return cand
        return None

    @staticmethod
    def _read_inventory(ctx: WorkflowContext) -> Tuple[Dict[str, int], List[Dict[str, Any]]]:
        inv_fn = getattr(ctx.guard.client, "inventory", None)
        if not callable(inv_fn):
            counts = ctx.inventory_counts()
            return counts, [{"name": k, "count": v} for k, v in counts.items()]
        env = ctx.guard.read(inv_fn, timeout_seconds=ctx.guard.remaining_timeout(10.0))
        res = env.get("result") if isinstance(env, dict) and isinstance(env.get("result"), dict) else {}
        items = res.get("items") if isinstance(res.get("items"), list) else []
        counts: Dict[str, int] = {}
        for row in items:
            if isinstance(row, dict) and isinstance(row.get("name"), str):
                c = row.get("count", 0)
                if isinstance(c, int) and not isinstance(c, bool) and c > 0:
                    counts[row["name"]] = counts.get(row["name"], 0) + c
        return counts, items

    @staticmethod
    def _place_near_torch(
        ctx: WorkflowContext,
        anchor_pos: Sequence[int],
        read_fn: Callable[[Sequence[int]], Dict[str, Any]],
        invalidate: Callable[[Sequence[int]], None],
        *,
        forbid_cell: Optional[Sequence[int]] = None,
        min_spacing: int = 8,
    ) -> bool:
        if ctx.select_item(["torch"]) is None:
            return False
        ax, ay, az = int(anchor_pos[0]), int(anchor_pos[1]), int(anchor_pos[2])
        forbid = (int(forbid_cell[0]), int(forbid_cell[1]), int(forbid_cell[2])) if forbid_cell else (ax, ay, az)
        # Skip placing an extra torch if another torch is already within spacing window
        r_chk = max(2, int(min_spacing))
        for dx in range(-r_chk, r_chk + 1):
            for dy in (-2, -1, 0, 1, 2):
                for dz in range(-r_chk, r_chk + 1):
                    if abs(dx) + abs(dz) <= r_chk and str(read_fn((ax + dx, ay + dy, az + dz)).get("name") or "") in MINE_TORCH_BLOCKS:
                        return False
        # Prefer eye-level wall spot adjacent to solid rock, then floor spot not occupied by bot
        candidates: List[Tuple[int, int, int]] = [
            (ax, ay + 1, az),
            (ax + 1, ay + 1, az),
            (ax - 1, ay + 1, az),
            (ax, ay + 1, az + 1),
            (ax, ay + 1, az - 1),
            (ax, ay, az),
        ]
        for cx, cy, cz in candidates:
            if (cx, cy, cz) in {(forbid[0], forbid[1], forbid[2]), (forbid[0], forbid[1] + 1, forbid[2])}:
                continue
            cur = str(read_fn((cx, cy, cz)).get("name") or "unknown")
            if cur in MINE_TORCH_BLOCKS:
                return False  # Already lit nearby
            if cur not in AIR:
                continue
            has_support = any(
                is_solid_floor(read_fn((cx + dx, cy + dy, cz + dz)))
                for dx, dy, dz in ((0, -1, 0), (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1))
            )
            if not has_support:
                continue
            try:
                res = place_torch(ctx, (cx, cy, cz), relative=False)
                invalidate((cx, cy, cz))
                return not bool(res.get("skipped"))
            except WorkflowStepError:
                continue
        return False

    def _mine_reachable_ores(
        self,
        ctx: WorkflowContext,
        stance_pos: Sequence[int],
        inv_items: Sequence[Dict[str, Any]],
        targets: Mapping[str, Mapping[str, Any]],
        harvested_totals: Dict[str, int],
        read_fn: Callable[[Sequence[int]], Dict[str, Any]],
        invalidate: Callable[[Sequence[int]], None],
        ore_records: Dict[Tuple[int, int, int], Dict[str, Any]],
        *,
        max_dist: float,
        max_vein: int,
    ) -> Tuple[int, int]:
        pick_name = best_usable_pickaxe(inv_items)
        tool_tier = pickaxe_tier(pick_name)
        found = reachable_exposed_ores(
            stance_pos,
            read_fn,
            targets,
            harvested_counts=harvested_totals,
            best_tool_tier=tool_tier,
            max_dist=max_dist,
        )
        dug_total = 0
        for row in found:
            key = (row["pos"][0], row["pos"][1], row["pos"][2])
            if row["status"] != "mined":
                prev = ore_records.get(key)
                if prev is None or prev.get("status") not in {"mined", "skipped_hazard"}:
                    ore_records[key] = {"pos": list(row["pos"]), "name": row["name"], "status": row["status"]}
                continue
            vein_dug = self._mine_vein(
                ctx,
                stance_pos,
                row["pos"],
                row["target_key"],
                targets,
                harvested_totals,
                read_fn,
                invalidate,
                ore_records,
                max_dist=max_dist,
                max_vein=max_vein,
            )
            dug_total += vein_dug
        return dug_total, len(ore_records)

    def _mine_vein(
        self,
        ctx: WorkflowContext,
        stance_pos: Sequence[int],
        seed_pos: Sequence[int],
        target_key: str,
        targets: Mapping[str, Mapping[str, Any]],
        harvested_totals: Dict[str, int],
        read_fn: Callable[[Sequence[int]], Dict[str, Any]],
        invalidate: Callable[[Sequence[int]], None],
        ore_records: Dict[Tuple[int, int, int], Dict[str, Any]],
        *,
        max_dist: float,
        max_vein: int,
    ) -> int:
        queue: deque = deque([list(seed_pos)])
        seen_vein: Set[Tuple[int, int, int]] = {tuple(seed_pos)}
        dug = 0
        sx, sy, sz = int(stance_pos[0]), int(stance_pos[1]), int(stance_pos[2])
        eye = (sx + 0.5, sy + 1.62, sz + 0.5)

        while queue and dug < int(max_vein):
            cur = queue.popleft()
            ckey = (cur[0], cur[1], cur[2])
            if ckey == (sx, sy - 1, sz):
                continue
            blk = read_fn(ckey)
            bname = str(blk.get("name") or "air")
            if bname not in ORE_DROP_MAP:
                continue

            _, cur_items = self._read_inventory(ctx)
            pick_name = best_usable_pickaxe(cur_items)
            tool_tier = pickaxe_tier(pick_name)
            action, tk, drop_item = evaluate_ore_block(
                bname,
                targets,
                harvested_counts=harvested_totals,
                best_tool_tier=tool_tier,
            )
            if tk != target_key:
                continue
            if action != "mine":
                if not ore_cell_hazard_free(ckey, read_fn):
                    action = "skipped_hazard"
                prev = ore_records.get(ckey)
                if prev is None or prev.get("status") not in {"mined", "skipped_hazard"}:
                    ore_records[ckey] = {"pos": list(cur), "name": bname, "status": action}
                continue

            dist = math.sqrt((cur[0] + 0.5 - eye[0]) ** 2 + (cur[1] + 0.5 - eye[1]) ** 2 + (cur[2] + 0.5 - eye[2]) ** 2)
            if dist > float(max_dist):
                ore_records[ckey] = {"pos": list(cur), "name": bname, "status": "unreachable"}
                continue

            if not ore_cell_hazard_free(ckey, read_fn):
                ore_records[ckey] = {"pos": list(cur), "name": bname, "status": "skipped_hazard"}
                continue

            # Guard gravel/sand settling hazard before breaking
            above_name = str(read_fn((cur[0], cur[1] + 1, cur[2])).get("name") or "air")
            if above_name in FALLING_BLOCKS:
                above2 = read_fn((cur[0], cur[1] + 2, cur[2]))
                if is_lava_like(above2) or is_water_like(above2):
                    ore_records[ckey] = {"pos": list(cur), "name": bname, "status": "skipped_hazard"}
                    continue

            if pick_name:
                ctx.guard.write(ctx.guard.client.equip, item=pick_name, slot="hand")
            aim_at(ctx, cur, relative=False)
            before_one, _ = self._read_inventory(ctx)
            res = dig_block(ctx, cur, relative=False, allowed_blocks=set(ORE_DROP_MAP) | MINE_DIGGABLE_BLOCKS, collect=False)
            invalidate(cur)
            if above_name in FALLING_BLOCKS:
                time.sleep(0.35)
                invalidate(cur)

            if not res.get("skipped"):
                dug += 1
                ore_records[ckey] = {"pos": list(cur), "name": bname, "status": "mined"}
                time.sleep(0.22 if cur[1] > sy else 0.12)
                after_one, _ = self._read_inventory(ctx)
                direct_gained = (
                    max(0, after_one.get(drop_item, 0) - before_one.get(drop_item, 0))
                    if drop_item
                    else 0
                )
                if direct_gained == 0:
                    after_mid = self._collect_mined_drop(ctx, cur, stance_pos, drop_item, before_one, read_fn, invalidate)
                    if after_mid and drop_item and after_mid.get(drop_item, 0) > after_one.get(drop_item, 0):
                        after_one = after_mid
                    else:
                        after_retry, _ = self._read_inventory(ctx)
                        if drop_item and after_retry.get(drop_item, 0) > after_one.get(drop_item, 0):
                            after_one = after_retry
                if drop_item:
                    gained = max(0, after_one.get(drop_item, 0) - before_one.get(drop_item, 0))
                    if gained > 0:
                        harvested_totals[drop_item] = harvested_totals.get(drop_item, 0) + gained

                for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                    nkey = (cur[0] + dx, cur[1] + dy, cur[2] + dz)
                    if nkey not in seen_vein:
                        seen_vein.add(nkey)
                        nblk = read_fn(nkey)
                        if str(nblk.get("name") or "") in ORE_DROP_MAP:
                            queue.append(list(nkey))
        return dug

    def _collect_mined_drop(
        self,
        ctx: WorkflowContext,
        ore_pos: Sequence[int],
        stance_pos: Sequence[int],
        drop_item: Optional[str],
        before_counts: Mapping[str, int],
        read_fn: Callable[[Sequence[int]], Dict[str, Any]],
        invalidate: Callable[[Sequence[int]], None],
    ) -> Optional[Dict[str, int]]:
        """Move onto the actual landing floor/entity position of a dug ore drop and return safely."""
        ox, oy, oz = int(ore_pos[0]), int(ore_pos[1]), int(ore_pos[2])
        sx, sy, sz = int(stance_pos[0]), int(stance_pos[1]), int(stance_pos[2])

        cand_spots: List[List[int]] = []
        seen_spots: Set[Tuple[int, int, int]] = {(sx, sy, sz)}

        def _add_if_standable(cx: int, cy: int, cz: int) -> None:
            key = (cx, cy, cz)
            if key in seen_spots or abs(cx - sx) + abs(cz - sz) > 6 or abs(cy - sy) > 2:
                return
            seen_spots.add(key)
            if cy > sy and not is_passable(read_fn((sx, sy + 2, sz))):
                return
            if (
                is_solid_floor(read_fn((cx, cy - 1, cz)))
                and is_passable(read_fn((cx, cy, cz)))
                and is_passable(read_fn((cx, cy + 1, cz)))
                and ore_cell_hazard_free((cx, cy, cz), read_fn)
            ):
                cand_spots.append([cx, cy, cz])

        # 1. Live item entity hint if available from mc-agent-api
        ent_fn = getattr(ctx.guard.client, "get_entities", None)
        if callable(ent_fn):
            try:
                raw_ents = ent_fn(radius=8)
                ent_list = raw_ents.get("result", {}).get("entities", []) if isinstance(raw_ents, dict) else (raw_ents or [])
                for ent in ent_list if isinstance(ent_list, list) else ():
                    if not isinstance(ent, dict):
                        continue
                    etype = str(ent.get("type") or ent.get("kind") or "").lower()
                    ename = str(ent.get("name") or "").lower()
                    epos = parse_pos(ent.get("pos"))
                    if epos is None:
                        continue
                    if ("item" in etype or (drop_item and drop_item in ename)) and (
                        math.hypot(epos[0] - (ox + 0.5), epos[2] - (oz + 0.5)) <= 4.5
                        and abs(epos[1] - oy) <= 3.5
                    ):
                        ex, ey, ez = math.floor(epos[0]), math.floor(epos[1]), math.floor(epos[2])
                        for dy in (0, -1, 1):
                            _add_if_standable(ex, ey + dy, ez)
            except Exception:
                pass

        # 2. Scan physically downward beneath ore_pos for the actual resting floor
        land_y: Optional[int] = None
        for y_try in range(min(oy + 1, sy + 2), max(oy - 4, sy - 4) - 1, -1):
            if (
                is_solid_floor(read_fn((ox, y_try - 1, oz)))
                and is_passable(read_fn((ox, y_try, oz)))
                and is_passable(read_fn((ox, y_try + 1, oz)))
            ):
                land_y = y_try
                _add_if_standable(ox, y_try, oz)
                break

        # 3. Same-level / lowered adjacent floor cells sorted closest to the landing spot
        ref_y = land_y if land_y is not None else min(oy, sy)
        nearby: List[Tuple[float, Tuple[int, int, int]]] = []
        for dx, dz in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
            for y_try in (ref_y, sy, sy - 1, oy, oy - 1):
                if abs(y_try - sy) <= 2:
                    dist_drop = math.hypot(dx, dz) + 0.8 * abs(y_try - ref_y) + 0.25 * abs(y_try - sy)
                    nearby.append((dist_drop, (ox + dx, y_try, oz + dz)))
        nearby.sort(key=lambda it: it[0])
        for _, cell in nearby:
            _add_if_standable(cell[0], cell[1], cell[2])

        best_counts: Optional[Dict[str, int]] = None
        for spot in cand_spots[:2]:
            try:
                stand_cell(ctx, spot, tolerance=0.95)
            except WorkflowStepError:
                cur_chk, _ = self._read_inventory(ctx)
                if best_counts is None or (drop_item and cur_chk.get(drop_item, 0) > best_counts.get(drop_item, 0)):
                    best_counts = cur_chk
                if drop_item and cur_chk.get(drop_item, 0) > before_counts.get(drop_item, 0):
                    break
                continue
            time.sleep(0.15)
            after_mid, _ = self._read_inventory(ctx)
            if best_counts is None or (drop_item and after_mid.get(drop_item, 0) > best_counts.get(drop_item, 0)):
                best_counts = after_mid
            try:
                stand_cell(ctx, stance_pos, tolerance=0.95)
            except WorkflowStepError:
                self._recover_step_with_graph(
                    ctx,
                    spot,
                    stance_pos,
                    read_fn,
                    invalidate,
                    allow_pillar=True,
                    allow_dig=False,
                    allow_bridge=False,
                    allow_drop=True,
                )
            if drop_item and after_mid.get(drop_item, 0) > before_counts.get(drop_item, 0):
                break
        return best_counts

    @classmethod
    def _recover_step_with_graph(
        cls,
        ctx: WorkflowContext,
        start_pos: Sequence[int],
        goal_pos: Sequence[int],
        read_fn: Callable[[Sequence[int]], Dict[str, Any]],
        invalidate: Callable[[Sequence[int]], None],
        block_cache: Optional[Dict[Tuple[int, int, int], Dict[str, Any]]] = None,
        *,
        allow_pillar: bool = True,
        allow_dig: bool = True,
        allow_bridge: bool = True,
        allow_drop: bool = True,
    ) -> bool:
        """Plan a multi-op rescue path from start_pos toward goal_pos using cached blocks."""
        tx, ty, tz = int(goal_pos[0]), int(goal_pos[1]), int(goal_pos[2])
        cur = [int(start_pos[0]), int(start_pos[1]), int(start_pos[2])]
        for _ in range(10):
            if abs(cur[0] - tx) <= 1 and abs(cur[1] - ty) <= 1 and abs(cur[2] - tz) <= 1:
                try:
                    stand_cell(ctx, [tx, ty, tz], tolerance=1.2)
                    return True
                except WorkflowStepError:
                    return True
            radius = max(12, min(28, max(abs(cur[0] - tx), abs(cur[2] - tz)) + 6))
            graph = flood_safe_cave_graph(
                cur,
                read_fn,
                max_nodes=260,
                max_radius=radius,
                allow_pillar=allow_pillar,
                max_pillar_height=4,
                allow_dig=allow_dig,
                max_dig_steps=3,
                allow_bridge=allow_bridge,
                max_bridge_steps=4,
                allow_drop=allow_drop,
                max_drop_steps=5,
                block_cache=block_cache,
            )
            nodes = graph.get("nodes") or []
            if not nodes:
                return False
            best_node = min(nodes, key=lambda n: abs(n[0] - tx) + 1.5 * abs(n[1] - ty) + abs(n[2] - tz))
            if tuple(best_node) == tuple(cur):
                return False
            path = reconstruct_flood_path(graph.get("parents") or {}, graph.get("edge_meta") or {}, best_node)
            if not path:
                return False
            for step in path[:12]:
                mode = str(step.get("mode") or "walk")
                try:
                    if mode == "pillar":
                        for dcell in step.get("dig_cells") or ():
                            dig_block(ctx, dcell, relative=False, allowed_blocks=set(CAVE_DIGGABLE_BLOCKS) | set(ORE_DROP_MAP), collect=False)
                            invalidate(dcell)
                        pres = pillar_up(ctx, steps=1, blocks=SCAFFOLD_BLOCKS, allow_dig_headroom=allow_dig)
                        for pc in pres.get("placed_cells") or ():
                            invalidate(pc)
                    elif mode == "bridge":
                        bridge_rep = set(AIR) | set(HAZARDS) | set(DEFAULT_REPLACEABLE)
                        for bcell in step.get("bridge_cells") or ():
                            place_block(ctx, SCAFFOLD_BLOCKS, bcell, relative=False, replaceable=bridge_rep)
                            invalidate(bcell)
                        stand_cell(ctx, step["to"], tolerance=0.9)
                    elif mode == "scaffold_drop":
                        scaffold_drop(ctx, step["to"], step.get("step_cells") or (), blocks=SCAFFOLD_BLOCKS, invalidate=invalidate)
                    else:
                        if mode == "dig":
                            for dcell in step.get("dig_cells") or ():
                                dig_block(ctx, dcell, relative=False, allowed_blocks=set(CAVE_DIGGABLE_BLOCKS) | set(ORE_DROP_MAP), collect=False)
                                invalidate(dcell)
                        stand_cell(ctx, step["to"], tolerance=0.9)
                    cur = list(step["to"])
                except WorkflowStepError:
                    snap = cls._sync_live_pos(ctx)
                    if snap is not None:
                        cur = snap
                    return False
        return abs(cur[0] - tx) <= 1 and abs(cur[1] - ty) <= 1 and abs(cur[2] - tz) <= 1

    @classmethod
    def _retreat_breadcrumbs(
        cls,
        ctx: WorkflowContext,
        breadcrumbs: Sequence[Sequence[int]],
        entry_cell: Sequence[int],
        *,
        pillar_undo: Optional[Mapping[int, Sequence[Sequence[int]]]] = None,
        invalidate: Optional[Callable[[Sequence[int]], None]] = None,
        read_fn: Optional[Callable[[Sequence[int]], Dict[str, Any]]] = None,
        block_cache: Optional[Dict[Tuple[int, int, int], Dict[str, Any]]] = None,
        allow_pillar: bool = False,
        allow_dig: bool = False,
        allow_bridge: bool = False,
        allow_drop: bool = False,
    ) -> bool:
        if len(breadcrumbs) <= 1:
            return True
        inv = invalidate or (lambda _pos: None)
        try:
            stand_cell(ctx, entry_cell, tolerance=1.25)
            return True
        except WorkflowStepError as exc:
            if exc.code not in {"stalled", "path_not_found", "timeout", "unreachable_stance", "route_blocked"}:
                raise
        if read_fn is not None and len(breadcrumbs) > 4:
            live = cls._sync_live_pos(ctx) or list(breadcrumbs[-1])
            if cls._recover_step_with_graph(
                ctx,
                live,
                entry_cell,
                read_fn,
                inv,
                block_cache,
                allow_pillar=allow_pillar,
                allow_dig=allow_dig,
                allow_bridge=allow_bridge,
                allow_drop=allow_drop,
            ):
                return True
        for idx in range(len(breadcrumbs) - 1, 0, -1):
            try:
                placed = pillar_undo.get(idx) if pillar_undo else None
                if placed:
                    pillar_down(ctx, placed)
                    for pc in placed:
                        inv(pc)
                    if idx == 1 or list(breadcrumbs[idx - 1]) != list(entry_cell):
                        stand_cell(ctx, breadcrumbs[idx - 1], tolerance=0.9)
                else:
                    stand_cell(ctx, breadcrumbs[idx - 1], tolerance=0.9)
            except WorkflowStepError:
                if read_fn is not None:
                    live = cls._sync_live_pos(ctx) or list(breadcrumbs[idx])
                    if cls._recover_step_with_graph(
                        ctx,
                        live,
                        entry_cell,
                        read_fn,
                        inv,
                        block_cache,
                        allow_pillar=allow_pillar,
                        allow_dig=allow_dig,
                        allow_bridge=allow_bridge,
                        allow_drop=allow_drop,
                    ):
                        return True
                return False
        live_end = cls._sync_live_pos(ctx)
        if live_end is not None and (
            abs(live_end[0] - int(entry_cell[0])) > 1
            or abs(live_end[1] - int(entry_cell[1])) > 1
            or abs(live_end[2] - int(entry_cell[2])) > 1
        ):
            try:
                stand_cell(ctx, entry_cell, tolerance=1.25)
            except WorkflowStepError:
                return False
        return True

    def navigate_with_ops(
        self,
        target_pos: Sequence[int],
        *,
        ctx: Optional[WorkflowContext] = None,
        allow_pillar: bool = True,
        allow_dig: bool = True,
        allow_bridge: bool = True,
        allow_drop: bool = True,
        allow_damage: bool = True,
        critical_health: float = 6.0,
        max_steps: int = 36,
        timeout_seconds: float = 45.0,
        block_cache: Optional[Dict[Tuple[int, int, int], Dict[str, Any]]] = None,
    ) -> SkillResult:
        """Navigate to target_pos using fast move_to, falling back to flood-planned pillar/bridge/drop/micro-dig on stall."""
        tx, ty, tz = int(target_pos[0]), int(target_pos[1]), int(target_pos[2])
        own_guard = ctx is None
        if ctx is None:
            guard = WorkflowGuard(
                self.client,
                timeout_seconds=timeout_seconds,
                max_reads=4000,
                allow_damage=allow_damage,
                critical_health=critical_health,
            )
            ctx = WorkflowContext(guard=guard, spec=MINE_SHAFT_SPEC, origin=[0, 0, 0], orientation="south", extra={})
        cache = block_cache if block_cache is not None else {}

        def cached_read(p: Sequence[int]) -> Dict[str, Any]:
            k = (int(p[0]), int(p[1]), int(p[2]))
            if k not in cache:
                cache[k] = ctx.read_block(k)
            return cache[k]

        def invalidate(p: Sequence[int]) -> None:
            px, py, pz = int(p[0]), int(p[1]), int(p[2])
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        cache.pop((px + dx, py + dy, pz + dz), None)

        try:
            stand_cell(ctx, [tx, ty, tz], tolerance=1.5)
            return SkillResult(True, f"Reached {[tx, ty, tz]} directly.", data={"pos": [tx, ty, tz], "steps": 0, "used_ops": False})
        except WorkflowStepError as exc:
            if exc.code not in {"stalled", "path_not_found", "unreachable_stance", "timeout"}:
                return SkillResult(False, str(exc), error_code=exc.code)

        # Fallback: local flood-fill planner with pillar-up and micro-dig
        steps_done = 0
        used_ops = False
        try:
            while steps_done < int(max_steps):
                st = ctx.guard.check_status(require_safe=True)
                bp = parse_pos(st.get("pos"))
                if bp is None:
                    break
                cur = [math.floor(bp[0] + 0.0001), math.floor(bp[1] + 0.0001), math.floor(bp[2] + 0.0001)]
                if abs(cur[0] - tx) <= 1 and abs(cur[2] - tz) <= 1 and abs(cur[1] - ty) <= 1:
                    return SkillResult(True, f"Reached {[tx, ty, tz]} via flood-ops navigation.", data={"pos": cur, "steps": steps_done, "used_ops": used_ops})

                radius = max(10, min(28, max(abs(cur[0] - tx), abs(cur[2] - tz)) + 6))
                graph = flood_safe_cave_graph(
                    cur,
                    cached_read,
                    max_nodes=420,
                    max_radius=radius,
                    allow_pillar=allow_pillar,
                    max_pillar_height=4,
                    allow_dig=allow_dig,
                    max_dig_steps=3,
                    allow_bridge=allow_bridge,
                    max_bridge_steps=4,
                    allow_drop=allow_drop,
                    max_drop_steps=5,
                    block_cache=cache,
                )
                parents = graph.get("parents") or {}
                edge_meta = graph.get("edge_meta") or {}
                nodes = graph.get("nodes") or []

                def _goal_dist(p: Sequence[int]) -> float:
                    return abs(p[0] - tx) + 1.5 * abs(p[1] - ty) + abs(p[2] - tz)

                best_node = min(nodes, key=_goal_dist) if nodes else cur
                cur_dist = _goal_dist(cur)
                best_dist = _goal_dist(best_node)
                if tuple(best_node) == tuple(cur):
                    break
                if best_dist > 3.5 and (len(nodes) < 400 or cur_dist - best_dist < 5.0):
                    break

                # Walk back along parents to reconstruct ordered steps toward best_node
                path = reconstruct_flood_path(parents, edge_meta, best_node)
                if not path:
                    break
                for step in path[:6]:
                    mode = str(step.get("mode") or "walk")
                    if mode == "pillar":
                        used_ops = True
                        for dcell in step.get("dig_cells") or ():
                            dig_block(ctx, dcell, relative=False, allowed_blocks=set(CAVE_DIGGABLE_BLOCKS) | set(ORE_DROP_MAP), collect=False)
                            invalidate(dcell)
                        pillar_up(ctx, steps=1, blocks=SCAFFOLD_BLOCKS, allow_dig_headroom=allow_dig)
                        invalidate(step["to"])
                    elif mode == "bridge":
                        used_ops = True
                        bridge_rep = set(AIR) | set(HAZARDS) | set(DEFAULT_REPLACEABLE)
                        for bcell in step.get("bridge_cells") or ():
                            place_block(ctx, SCAFFOLD_BLOCKS, bcell, relative=False, replaceable=bridge_rep)
                            invalidate(bcell)
                        stand_cell(ctx, step["to"], tolerance=0.9)
                    elif mode == "scaffold_drop":
                        used_ops = True
                        scaffold_drop(ctx, step["to"], step.get("step_cells") or (), blocks=SCAFFOLD_BLOCKS, invalidate=invalidate)
                    else:
                        if mode == "dig":
                            used_ops = True
                            for dcell in step.get("dig_cells") or ():
                                dig_block(ctx, dcell, relative=False, allowed_blocks=set(CAVE_DIGGABLE_BLOCKS) | set(ORE_DROP_MAP), collect=False)
                                invalidate(dcell)
                        stand_cell(ctx, step["to"], tolerance=0.9)
                    steps_done += 1
                    if abs(step["to"][0] - tx) <= 1 and abs(step["to"][2] - tz) <= 1 and abs(step["to"][1] - ty) <= 1:
                        return SkillResult(True, f"Reached {[tx, ty, tz]} via flood-ops navigation.", data={"pos": step["to"], "steps": steps_done, "used_ops": True})
            return SkillResult(False, f"Could not reach {[tx, ty, tz]} with flood-ops navigation.", error_code="route_blocked")
        except WorkflowStepError as exc:
            return SkillResult(False, str(exc), error_code=exc.code)
        finally:
            if own_guard:
                ctx.guard.restore_posture()

    def find_cave_openings(
        self,
        origin: Optional[Sequence[int]] = None,
        *,
        search_radius: int = 22,
        max_candidates: int = 4,
        block_cache: Optional[Dict[Tuple[int, int, int], Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Discover nearby natural cave openings, stone overhangs, or shallow exposed ore pockets."""
        raw_st = self.client.get_status()
        st = raw_st.get("result") if isinstance(raw_st, dict) and "result" in raw_st else raw_st
        bp = parse_pos(origin if origin is not None else (st.get("pos") if isinstance(st, dict) else None))
        if bp is None:
            return []
        ox, oy, oz = int(math.floor(bp[0])), int(math.floor(bp[1])), int(math.floor(bp[2]))
        cache: Dict[Tuple[int, int, int], Dict[str, Any]] = block_cache if block_cache is not None else {}

        def read_blk(pos: Sequence[int]) -> Dict[str, Any]:
            k = (int(pos[0]), int(pos[1]), int(pos[2]))
            if k not in cache:
                raw = self.client.get_block(k[0], k[1], k[2])
                cache[k] = raw.get("result") if isinstance(raw, dict) and "result" in raw else raw
            return cache[k]

        candidates: List[Dict[str, Any]] = []
        # Step 1: check find_blocks for exposed underground stone/iron_ore hints first if available
        seed_coords: List[Tuple[int, int, int]] = []
        try:
            hits = self.client.get_blocks(radius=min(32, int(search_radius)), types=["iron_ore", "deepslate_iron_ore", "coal_ore"], limit=12)
            for h in hits or []:
                p = h.get("pos")
                if isinstance(p, list) and len(p) == 3:
                    for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        seed_coords.append((int(p[0]) + dx, int(p[1]), int(p[2]) + dz))
        except Exception:
            pass

        # Step 2: coarse 4x4 grid scan for roofed air cavities below/near surface around origin
        step = 4
        for dx in range(-int(search_radius), int(search_radius) + 1, step):
            for dz in range(-int(search_radius), int(search_radius) + 1, step):
                if dx == 0 and dz == 0:
                    continue
                for dy in range(-14, 10, 3):
                    seed_coords.append((ox + dx, oy + dy, oz + dz))

        seen_clusters: List[Tuple[int, int, int]] = []
        for sx, sy, sz in seed_coords:
            if any(abs(sx - cx) + abs(sz - cz) < 10 and abs(sy - cy) < 8 for cx, cy, cz in seen_clusters):
                continue
            foot = read_blk((sx, sy, sz))
            head = read_blk((sx, sy + 1, sz))
            floor = read_blk((sx, sy - 1, sz))
            ceil = read_blk((sx, sy + 2, sz))
            if not (is_passable(foot) and is_passable(head) and is_solid_floor(floor)):
                continue
            # Require a natural rock/mineral ceiling within y+2..y+7 (reject open sky and forest leaf canopies)
            rock_roof_blocks = {
                "stone", "deepslate", "andesite", "diorite", "granite", "tuff",
                "sandstone", "red_sandstone", "terracotta", "calcite", "cobblestone",
                "dripstone_block", "netherrack",
            } | set(ORE_DROP_MAP)
            has_rock_roof = any(
                str(read_blk((sx, sy + ky, sz)).get("name") or "air") in rock_roof_blocks
                for ky in range(2, 8)
            )
            if not has_rock_roof:
                continue
            rep = probe_cavity([sx, sy, sz], [sx, sy, sz], read_blk, limits={"max_probe_cells": 90, "horizontal_radius": 8, "vertical_span": 6}, block_cache=cache)
            if rep.get("lava_found") or rep.get("spawner_found") or int(rep.get("water_count", 0)) >= 3:
                continue
            if int(rep.get("air_count", 0)) < 10:
                continue
            graph = flood_safe_cave_graph([sx, sy, sz], read_blk, max_nodes=80, max_radius=10, block_cache=cache)
            if len(graph.get("nodes") or []) < 3:
                continue
            seen_clusters.append((sx, sy, sz))
            dist = abs(sx - ox) + abs(sy - oy) + abs(sz - oz)
            candidates.append(
                {
                    "entry_pos": [sx, sy, sz],
                    "air_count": int(rep.get("air_count", 0)),
                    "safe_nodes": len(graph.get("nodes") or []),
                    "distance": dist,
                    "report": rep,
                }
            )
            if len(candidates) >= int(max_candidates) * 2:
                break

        candidates.sort(key=lambda c: (-c["safe_nodes"], c["distance"]))
        return candidates[: int(max_candidates)]

    def discover_and_explore_caves(
        self,
        origin: Optional[Sequence[int]] = None,
        *,
        search_radius: int = 22,
        max_caves: int = 4,
        targets: Optional[Mapping[str, Any]] = None,
        cave_policy: Optional[Mapping[str, Any]] = None,
        home_thresholds: Optional[Mapping[str, Any]] = None,
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: float = 360.0,
        allow_damage: bool = True,
        critical_health: float = 6.0,
    ) -> SkillResult:
        """Cave-first expedition: locate nearby natural caves, traverse steep/lava terrain with pillar/bridge/drop/dig, and sweep ores."""
        parsed_targets = parse_mining_targets(targets if targets is not None else DEFAULT_MINING_TARGETS)
        policy = dict(CAVE_LIMITS)
        if cave_policy:
            policy.update(cave_policy)
        guard = WorkflowGuard(
            self.client,
            cancel_event=cancel_event,
            timeout_seconds=timeout_seconds,
            max_reads=200000,
            allow_damage=allow_damage,
            critical_health=critical_health,
        )
        ctx = WorkflowContext(guard=guard, spec=MINE_SHAFT_SPEC, origin=[0, 0, 0], orientation="south", extra={})
        cache: Dict[Tuple[int, int, int], Dict[str, Any]] = {}

        before_counts, _ = self._read_inventory(ctx)
        if before_counts.get("torch", 0) < 4:
            if self._try_craft_torches(ctx, before_counts):
                before_counts, _ = self._read_inventory(ctx)
        openings = self.find_cave_openings(origin=origin, search_radius=search_radius, max_candidates=max_caves, block_cache=cache)
        caves_explored: List[Dict[str, Any]] = []
        ores_all: List[Dict[str, Any]] = []
        torches_total = 0
        stop_reason = "no_caves_found" if not openings else "caves_exhausted"
        home_reason: Optional[str] = None
        shared_covered: Set[Tuple[int, int, int]] = set()

        for op in openings:
            if guard.remaining_timeout(999.0) < 25.0:
                break
            entry = op["entry_pos"]
            if (int(entry[0]), int(entry[1]), int(entry[2])) in shared_covered:
                continue
            nav = self.navigate_with_ops(entry, ctx=ctx, allow_pillar=True, allow_dig=True, allow_bridge=True, allow_drop=True, max_steps=32, block_cache=cache)
            if not nav.success:
                continue
            cur_counts, _ = self._read_inventory(ctx)
            running = {
                k: max(0, cur_counts.get(k, 0) - before_counts.get(k, 0))
                for k in cur_counts
            }
            ex = self.explore_cave(
                entry_pos=nav.data.get("pos", entry),
                origin_air=entry,
                targets=targets,
                cave_policy=policy,
                home_thresholds=home_thresholds,
                harvested_totals=running,
                block_cache=cache,
                covered_cells=shared_covered,
                guard=guard,
                prefer_flood_planner=True,
                allow_tall_cave_subgraph=True,
                allow_pillar=True,
                allow_micro_dig=True,
                allow_bridge=True,
                allow_drop=True,
                allow_surface_escape=True,
            )
            caves_explored.extend(ex.data.get("caves") or [])
            ores_all.extend(ex.data.get("ores") or [])
            torches_total += int(ex.data.get("torches_placed", 0))

            after_counts, _ = self._read_inventory(ctx)
            net_counts = {k: max(0, after_counts.get(k, 0) - before_counts.get(k, 0)) for k in after_counts}
            quotas_done = bool(parsed_targets) and all(
                net_counts.get(spec["drop_item"], 0) >= int(spec["count"])
                for spec in parsed_targets.values()
            )
            if quotas_done:
                stop_reason = "target_reached"
                break
            if ex.data.get("stop_reason") == "return_home" and ex.data.get("home_reason") in {
                "inventory_full",
                "no_pickaxe",
                "low_health",
                "low_food",
            }:
                stop_reason = "return_home"
                home_reason = ex.data.get("home_reason")
                break

        after_counts, _ = self._read_inventory(ctx)
        harvested = {k: max(0, after_counts.get(k, 0) - before_counts.get(k, 0)) for k in after_counts if after_counts.get(k, 0) > before_counts.get(k, 0)}
        return SkillResult(
            stop_reason == "target_reached" or bool(caves_explored),
            f"Cave-first expedition visited {len(caves_explored)} cave(s), stop={stop_reason}, harvested={harvested}",
            error_code="return_home_requested" if stop_reason == "return_home" else None,
            data={
                "caves": caves_explored,
                "ores": ores_all,
                "harvested": harvested,
                "torches_placed": torches_total,
                "stop_reason": stop_reason,
                "home_reason": home_reason,
                "openings_found": len(openings),
            },
        )
