"""Independent bounded cave/mineshaft exploration backed by VoxelWorld3D, Pathfinder3D, WorldNavigator, and OreHarvester."""

import math
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from core.cave_probe import (
    classify_cavity,
    has_exposed_air_face,
    is_lava_like,
    is_passable,
    is_solid_floor,
    lateral_air_clearance,
    ore_cell_hazard_free,
    probe_cavity,
    walkable_neighbors,
)
from core.domain import AIR, parse_pos
from core.memory import MemoryEngine
from core.mining_targets import (
    DEFAULT_EDIBLE_FOODS,
    DEFAULT_MINING_TARGETS,
    best_usable_pickaxe,
    evaluate_home_trigger,
    parse_mining_targets,
    pickaxe_tier,
)
from core.pathfinder_3d import Pathfinder3D
from core.structure_spec import (
    CAVE_LIMITS,
    MINE_SHAFT_SPEC,
    MINE_TORCH_BLOCKS,
    ORE_DROP_MAP,
    SCAFFOLD_BLOCKS,
)
from core.world_3d import OreNode, VoxelWorld3D, norm_coord
from skills.action_workflow import (
    WorkflowContext,
    WorkflowGuard,
    WorkflowStepError,
    consume,
    place_torch,
    stand_cell,
)
from skills.ore_harvester import OreHarvester
from skills.world_navigator import (
    STEP_RECOVERABLE_ERRORS,
    ReversibleTrail,
    WorldNavigator,
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
    """Safely explore caves/mineshafts with persistent 3D world memory, A* frontier routing, and physical drop pickup."""

    def __init__(
        self,
        client,
        memory: Optional[MemoryEngine] = None,
        world: Optional[VoxelWorld3D] = None,
    ):
        super().__init__(client)
        self.memory = memory or MemoryEngine()
        self.default_world = world

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
        world: Optional[VoxelWorld3D] = None,
        persistent_world: bool = False,
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
            world=world,
            persistent_world=persistent_world,
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

    def _resolve_world(
        self,
        *,
        world: Optional[VoxelWorld3D],
        block_cache: Optional[Dict[Tuple[int, int, int], Dict[str, Any]]],
        persistent_world: bool,
    ) -> VoxelWorld3D:
        if world is not None:
            return world
        if self.default_world is not None:
            return self.default_world
        if persistent_world:
            w = VoxelWorld3D.for_runtime(
                endpoint=str(getattr(self.client, "base_url", "http://127.0.0.1:18900/api/v1")),
                username=str(getattr(self.client, "username", "SleepBot")),
            )
            if block_cache:
                for k, v in block_cache.items():
                    if isinstance(v, dict) and k not in w.voxels:
                        w.set_block(k, v)
                w.block_cache = block_cache
            return w
        return VoxelWorld3D(block_cache=block_cache)

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
        world: Optional[VoxelWorld3D] = None,
        persistent_world: bool = False,
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
        stop_on_target: Optional[bool] = None,
    ) -> SkillResult:
        """Probe (if needed) and explore one cave/mineshaft via VoxelWorld3D + Pathfinder3D until exhausted."""
        policy = dict(CAVE_LIMITS)
        if cave_policy:
            policy.update(cave_policy)
        timeout = float(timeout_seconds if timeout_seconds is not None else policy["explore_timeout_seconds"])

        active_guard = guard or WorkflowGuard(
            self.client,
            cancel_event=cancel_event,
            timeout_seconds=timeout,
            max_reads=150000,
            allow_damage=allow_damage,
            critical_health=critical_health,
        )
        active_world = self._resolve_world(
            world=world,
            block_cache=block_cache,
            persistent_world=persistent_world,
        )
        if covered_cells is not None:
            active_world.scanned_cells.update(covered_cells)
        for bpos in (blocked_backtracks or ()):
            bp_t = norm_coord(bpos)
            active_world.visited_stances.setdefault(bp_t, 1)
            active_world.scanned_cells.add(bp_t)

        ctx = WorkflowContext(
            guard=active_guard,
            spec=MINE_SHAFT_SPEC,
            origin=[0, 0, 0],
            orientation="south",
            extra={},
        )

        def cached_read(pos: Sequence[int]) -> Dict[str, Any]:
            return active_world.read_block(pos, lambda p: ctx.read_block(p))

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

        before_counts, inv_items = OreHarvester.read_inventory(ctx)
        if before_counts.get("torch", 0) < 4:
            if self._try_craft_torches(ctx, before_counts):
                before_counts, inv_items = OreHarvester.read_inventory(ctx)
        torch_count = before_counts.get("torch", 0)
        parsed_targets = parse_mining_targets(targets if targets is not None else DEFAULT_MINING_TARGETS)

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
            report = probe_cavity(air_seed, entry_cell, cached_read, limits=policy, block_cache=active_world.block_cache)
        if classification is None:
            classification = classify_cavity(report, torch_count=torch_count, limits=policy)

        if (
            allow_tall_cave_subgraph
            and classification["decision"] != "enter"
            and classification.get("kind") in {"tall_cave", "deep_drop", "lava", "water_cave"}
            and not report.get("spawner_found")
            and (classification.get("kind") != "lava" or allow_bridge)
        ):
            sub_graph = Pathfinder3D.plan_horizon(
                active_world,
                entry_cell,
                cached_read,
                max_nodes=max(120, int(policy["explore_max_visited"])),
                max_radius=int(policy["explore_radius"]),
                anchor_pos=entry_cell,
                anchor_radius=int(policy["explore_radius"]),
                allow_pillar=allow_pillar,
                allow_dig=allow_micro_dig,
                allow_bridge=allow_bridge,
                allow_drop=allow_drop,
                targets=parsed_targets,
                harvested_counts=harvested_totals or {},
                best_tool_tier=pickaxe_tier(best_usable_pickaxe(inv_items)),
                eye_radius=float(policy["eye_scan_radius"]),
            )
            if len(sub_graph.get("nodes") or []) >= 3 or sub_graph.get("ore_targets"):
                sub_kind = "tall_subgraph" if classification.get("kind") in {"tall_cave", "deep_drop"} else "hazard_subgraph"
                classification = {
                    "decision": "enter",
                    "kind": sub_kind,
                    "reason": f"3D graph verified {len(sub_graph['nodes'])} safe subgraph nodes",
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

        trail = ReversibleTrail(entry_cell)
        dive_visited: Set[Tuple[int, int, int]] = {tuple(entry_cell)}
        cur_pos = list(entry_cell)
        active_world.record_stance(entry_cell)

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
                rows = fb(types=want_blocks, radius=min(64, explore_radius + 8), limit=128)
                candidates = [
                    r
                    for r in (rows or [])
                    if isinstance(r, dict) and isinstance(r.get("pos"), list) and len(r["pos"]) == 3
                ]
                candidates.sort(
                    key=lambda r: (
                        0 if abs(int(r["pos"][1]) - int(center_pos[1])) <= 22 else 1,
                        abs(int(r["pos"][0]) - int(center_pos[0]))
                        + abs(int(r["pos"][1]) - int(center_pos[1]))
                        + abs(int(r["pos"][2]) - int(center_pos[2])),
                    )
                )
                if explore_radius >= 24:
                    reg_checked = 0
                    for row in candidates:
                        hp = row["pos"]
                        if (
                            abs(int(hp[0]) - entry_cell[0]) > explore_radius
                            or abs(int(hp[2]) - entry_cell[2]) > explore_radius
                            or abs(int(hp[1]) - int(center_pos[1])) > 24
                        ):
                            continue
                        pos_t = (int(hp[0]), int(hp[1]), int(hp[2]))
                        reg_checked += 1
                        if pos_t not in active_world.ore_registry:
                            if has_exposed_air_face(
                                pos_t,
                                cached_read,
                                block_cache=active_world.block_cache,
                            ) and ore_cell_hazard_free(
                                pos_t,
                                cached_read,
                                block_cache=active_world.block_cache,
                            ):
                                bname = str(row.get("name") or cached_read(pos_t).get("name") or "ore")
                                active_world.ore_registry[pos_t] = OreNode(
                                    pos=pos_t,
                                    name=bname,
                                    status="pending",
                                )
                        if reg_checked >= 12:
                            break
                return [list(r["pos"]) for r in candidates]
            except Exception:
                return []

        ore_hints: List[List[int]] = _refresh_ore_hints(entry_cell)
        hint_refresh_step = 0

        torch_spacing = max(4, int(round(torch_interval * 0.75)))
        placed_torch_cells: List[Tuple[int, int, int]] = []
        if torch_count > 0 and self._place_near_torch(
            ctx,
            entry_cell,
            cached_read,
            lambda p: active_world.invalidate(p, radius=1),
            min_spacing=min(6, torch_spacing),
        ):
            torches_placed += 1
            placed_torch_cells.append((int(entry_cell[0]), int(entry_cell[1]), int(entry_cell[2])))

        steps_since_torch = 0
        prev_dir: Optional[Tuple[int, int]] = None
        planned_queue: List[Dict[str, Any]] = []
        last_graph_at_visited = -1
        consecutive_step_failures = 0

        try:
            while (
                len(dive_visited) < max_visited
                and time.monotonic() < explore_deadline
                and active_guard.read_calls < max(500, active_guard.max_reads - 1500)
            ):
                if guard is not None:
                    active_guard.check_budget()
                cur_status = active_guard.check_status(require_safe=True)
                cur_counts, cur_items = OreHarvester.read_inventory(ctx)

                if self._try_eat_food(ctx, cur_status, cur_counts):
                    cur_status = active_guard.check_status(require_safe=True)
                    cur_counts, cur_items = OreHarvester.read_inventory(ctx)

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

                base_totals = harvested_totals or {}
                for k in set(base_totals) | set(cur_counts) | set(before_counts):
                    inv_progress = int(base_totals.get(k, 0)) + max(0, cur_counts.get(k, 0) - before_counts.get(k, 0))
                    if inv_progress > running_harvested.get(k, 0):
                        running_harvested[k] = inv_progress

                t_dig, _ = OreHarvester.harvest_reachable_at(
                    ctx,
                    active_world,
                    cur_pos,
                    cur_items,
                    parsed_targets,
                    running_harvested,
                    cached_read,
                    ore_records,
                    max_dist=eye_radius,
                    max_vein=max_vein,
                )
                if t_dig:
                    active_guard.check_status(require_safe=True)
                    snap_dig = WorldNavigator.sync_live_pos(ctx)
                    if snap_dig is not None:
                        if snap_dig != cur_pos:
                            trail.sync_pos(snap_dig)
                        cur_pos = snap_dig
                        dive_visited.add(tuple(cur_pos))
                        active_world.record_stance(cur_pos)
                    cur_counts, cur_items = OreHarvester.read_inventory(ctx)
                    for k in set(base_totals) | set(cur_counts) | set(before_counts):
                        inv_progress = int(base_totals.get(k, 0)) + max(0, cur_counts.get(k, 0) - before_counts.get(k, 0))
                        if inv_progress > running_harvested.get(k, 0):
                            running_harvested[k] = inv_progress
                    if (
                        (
                            bool(stop_on_target)
                            if stop_on_target is not None
                            else (
                                explore_radius >= 24
                                or max((int(s["count"]) for s in parsed_targets.values()), default=0) >= 8
                            )
                        )
                        and parsed_targets
                        and all(
                            running_harvested.get(spec["drop_item"], 0) >= int(spec["count"])
                            for spec in parsed_targets.values()
                        )
                    ):
                        stop_reason = "target_reached"
                        break

                has_web_tool = any(cur_counts.get(w, 0) > 0 for w in SWORD_OR_SHEARS)
                raw_neighbors = walkable_neighbors(
                    cur_pos,
                    cached_read,
                    allow_cobweb=has_web_tool,
                    block_cache=active_world.block_cache,
                )
                unvisited_adj = [
                    n for n in raw_neighbors
                    if tuple(n) not in dive_visited
                    and not active_world.is_edge_blocked(cur_pos, n)
                    and abs(n[0] - entry_cell[0]) <= explore_radius
                    and abs(n[2] - entry_cell[2]) <= explore_radius
                ]
                uncovered_adj = [n for n in unvisited_adj if tuple(n) not in active_world.scanned_cells]
                for group in (uncovered_adj, unvisited_adj):
                    group.sort(
                        key=lambda n: (
                            -lateral_air_clearance(n, cached_read, block_cache=active_world.block_cache),
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
                    dive_visited.add(tuple(cur_pos))
                    active_world.record_stance(cur_pos)

                if planned_step is None:
                    hint_refresh_step += 1
                    if hint_refresh_step % 4 == 0:
                        ore_hints = _refresh_ore_hints(cur_pos)
                    use_graph = prefer_flood_planner and (len(dive_visited) != last_graph_at_visited)
                    if use_graph:
                        last_graph_at_visited = len(dive_visited)
                        has_scaffold = sum(cur_counts.get(s, 0) for s in SCAFFOLD_BLOCKS) >= 4
                        tool_tier = pickaxe_tier(best_usable_pickaxe(cur_items))
                        if active_guard.max_reads >= 20000:
                            active_world.ingest_around(
                                cur_pos,
                                cached_read,
                                radius=3,
                                targets=parsed_targets,
                                harvested_counts=running_harvested,
                                best_tool_tier=tool_tier,
                            )

                        remaining_reads = max(0, int(active_guard.max_reads) - int(active_guard.read_calls) - 1000)
                        read_cap_nodes = max(120, remaining_reads // 12)
                        local_budget = min(560, max(180, max_visited * 2), read_cap_nodes)
                        graph = Pathfinder3D.plan_horizon(
                            active_world,
                            cur_pos,
                            cached_read,
                            max_nodes=local_budget,
                            max_radius=max(explore_radius, abs(cur_pos[0] - entry_cell[0]) + explore_radius, abs(cur_pos[2] - entry_cell[2]) + explore_radius),
                            anchor_pos=entry_cell,
                            anchor_radius=explore_radius,
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
                            best_tool_tier=tool_tier,
                            eye_radius=eye_radius,
                            goal_hints=ore_hints,
                        )

                        def _pick_graph_ores(graph_obj: Mapping[str, Any], max_q: int) -> Optional[Dict[str, Any]]:
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
                            return None

                        def _pick_graph_frontiers(graph_obj: Mapping[str, Any], max_q: int) -> Optional[Dict[str, Any]]:
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

                        def _pick_graph_plan(graph_obj: Mapping[str, Any], max_q: int) -> Optional[Dict[str, Any]]:
                            return _pick_graph_ores(graph_obj, max_q) or _pick_graph_frontiers(graph_obj, max_q)

                        planned_step = _pick_graph_ores(graph, 20)

                        # Active target-driven 3D routing: when local horizon sees no ore_targets, route via
                        # VoxelWorld3D.ore_registry + 3D A* before falling back to blind frontier wandering.
                        if (
                            planned_step is None
                            and explore_radius >= 24
                            and not (graph.get("ore_targets") or ())
                            and active_guard.max_reads >= 20000
                        ):
                            for pending_ore in active_world.query_pending_ores(
                                cur_pos,
                                parsed_targets,
                                best_tool_tier=tool_tier,
                                harvested_counts=running_harvested,
                                entry_pos=entry_cell,
                                max_radius=explore_radius,
                            )[:3]:
                                v_res = Pathfinder3D.find_ore_vantage_path(
                                    active_world,
                                    cur_pos,
                                    pending_ore.pos,
                                    cached_read,
                                    eye_radius=eye_radius,
                                    anchor_pos=entry_cell,
                                    anchor_radius=explore_radius,
                                    allow_pillar=bool(allow_pillar and has_scaffold),
                                    allow_dig=allow_micro_dig,
                                    allow_bridge=bool(allow_bridge and has_scaffold),
                                    allow_drop=allow_drop,
                                    allow_cobweb=has_web_tool,
                                    max_nodes=2400,
                                )
                                if v_res and v_res[1]:
                                    planned_step = v_res[1][0]
                                    planned_queue[:] = v_res[1][1:36]
                                    break
                                pending_ore.fail_count += 1
                                if pending_ore.fail_count >= 2:
                                    pending_ore.status = "unreachable"

                        if planned_step is None:
                            planned_step = _pick_graph_frontiers(graph, 20)

                        # Persistent 3D Memory A* Frontier/Ore Jump: route across explored caverns to open remote branches
                        if planned_step is None and not uncovered_adj:
                            for pending_ore in active_world.query_pending_ores(
                                cur_pos,
                                parsed_targets,
                                best_tool_tier=tool_tier,
                                harvested_counts=running_harvested,
                                entry_pos=entry_cell,
                                max_radius=explore_radius,
                            )[:4]:
                                v_res = Pathfinder3D.find_ore_vantage_path(
                                    active_world,
                                    cur_pos,
                                    pending_ore.pos,
                                    cached_read,
                                    eye_radius=eye_radius,
                                    anchor_pos=entry_cell,
                                    anchor_radius=explore_radius,
                                    allow_pillar=bool(allow_pillar and has_scaffold),
                                    allow_dig=allow_micro_dig,
                                    allow_bridge=bool(allow_bridge and has_scaffold),
                                    allow_drop=allow_drop,
                                    allow_cobweb=has_web_tool,
                                    max_nodes=1800,
                                )
                                if v_res and v_res[1]:
                                    planned_step = v_res[1][0]
                                    planned_queue.extend(v_res[1][1:36])
                                    break
                                pending_ore.fail_count += 1
                                if pending_ore.fail_count >= 2:
                                    pending_ore.status = "unreachable"

                        if planned_step is None and not uncovered_adj:
                            for fnode in active_world.select_best_frontiers(
                                cur_pos,
                                entry_pos=entry_cell,
                                max_radius=explore_radius,
                                goal_hints=ore_hints,
                                limit=10,
                            ):
                                fpath = Pathfinder3D.find_path(
                                    active_world,
                                    cur_pos,
                                    fnode.pos,
                                    cached_read,
                                    goal_tolerance=0,
                                    allow_pillar=bool(allow_pillar and has_scaffold),
                                    allow_dig=allow_micro_dig,
                                    allow_bridge=bool(allow_bridge and has_scaffold),
                                    allow_drop=allow_drop,
                                    allow_cobweb=has_web_tool,
                                    anchor_pos=entry_cell,
                                    anchor_radius=explore_radius,
                                    max_nodes=2400,
                                    cache_only=(remaining_reads < 6000),
                                )
                                if fpath:
                                    planned_step = fpath[0]
                                    planned_queue.extend(fpath[1:40])
                                    break
                                active_world.mark_frontier_failed(fnode.pos)

                        if planned_step is None and not uncovered_adj and remaining_reads >= 12000:
                            esc_cap = max(local_budget, remaining_reads // 10)
                            global_budget = min(3600, max(1400, max_visited * 6), esc_cap)
                            if global_budget > local_budget and len(graph.get("nodes") or []) >= local_budget - 5:
                                graph_full = Pathfinder3D.plan_horizon(
                                    active_world,
                                    cur_pos,
                                    cached_read,
                                    max_nodes=global_budget,
                                    max_radius=max(explore_radius, abs(cur_pos[0] - entry_cell[0]) + explore_radius, abs(cur_pos[2] - entry_cell[2]) + explore_radius),
                                    anchor_pos=entry_cell,
                                    anchor_radius=explore_radius,
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
                                    best_tool_tier=tool_tier,
                                    eye_radius=eye_radius,
                                    goal_hints=ore_hints,
                                )
                                planned_step = _pick_graph_plan(graph_full, 48)

                        if planned_step is None and uncovered_adj:
                            planned_step = {"mode": "walk", "to": uncovered_adj[0]}
                    elif uncovered_adj:
                        planned_step = {"mode": "walk", "to": uncovered_adj[0]}
                    elif not prefer_flood_planner and unvisited_adj:
                        planned_step = {"mode": "walk", "to": unvisited_adj[0]}

                if planned_step is None:
                    break

                nxt = list(planned_step["to"])
                mode = str(planned_step.get("mode") or "walk")
                if nxt == cur_pos and mode != "pillar":
                    dive_visited.add(tuple(nxt))
                    active_world.record_stance(nxt)
                    continue

                old_pos = list(cur_pos)
                step_dir = (nxt[0] - old_pos[0], nxt[2] - old_pos[2])
                cobweb_tool = next((w for w in SWORD_OR_SHEARS if cur_counts.get(w, 0) > 0), None)
                try:
                    new_pos, skipped_web = WorldNavigator.execute_step(
                        ctx,
                        active_world,
                        cur_pos,
                        planned_step,
                        cached_read,
                        cobweb_tool=cobweb_tool,
                        allow_micro_dig=allow_micro_dig,
                        allow_dig_headroom_on_pillar=allow_micro_dig,
                        trail=trail,
                        tolerance=0.85,
                    )
                    if skipped_web:
                        dive_visited.add(tuple(nxt))
                        active_world.scanned_cells.add(tuple(nxt))
                        planned_queue.clear()
                        continue
                except WorkflowStepError as step_exc:
                    if step_exc.code in STEP_RECOVERABLE_ERRORS:
                        dive_visited.add(tuple(nxt))
                        planned_queue.clear()
                        consecutive_step_failures += 1
                        snap = WorldNavigator.sync_live_pos(ctx)
                        if snap is not None:
                            cur_pos = snap
                            dive_visited.add(tuple(cur_pos))
                            active_world.record_stance(cur_pos)
                        if consecutive_step_failures >= 6:
                            break
                        continue
                    raise

                consecutive_step_failures = 0
                dive_visited.add(tuple(new_pos))
                cur_pos = list(new_pos)
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
                        lambda p: active_world.invalidate(p, radius=1),
                        forbid_cell=cur_pos,
                        min_spacing=min(6, torch_spacing),
                    ):
                        torches_placed += 1
                        steps_since_torch = 0
                        placed_torch_cells.append((int(old_pos[0]), int(old_pos[1]), int(old_pos[2])))

        except WorkflowStepError as exc:
            stop_reason = (
                "safety_interrupted"
                if exc.code in {"damage_taken", "unsafe_state", "hazard_detected", "hazard_adjacent", "life_changed"}
                else "cave_route_blocked"
            )

        retreat_ok = False
        escaped_surface = False
        try:
            retreat_ok, escaped_surface = WorldNavigator.retreat_to_entry(
                ctx,
                active_world,
                trail,
                entry_cell,
                cached_read,
                allow_pillar=allow_pillar,
                allow_dig=allow_micro_dig,
                allow_bridge=allow_bridge,
                allow_drop=allow_drop,
                allow_surface_escape=allow_surface_escape,
            )
        except WorkflowStepError:
            retreat_ok = False
        if not retreat_ok and not escaped_surface:
            stop_reason = "safety_interrupted"

        active_world.invalidate(entry_cell, radius=1)
        for check_y in (entry_cell[1], entry_cell[1] + 1):
            for dx, dy, dz in ((0, 0, 0), (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                blk = cached_read((entry_cell[0] + dx, check_y + dy, entry_cell[2] + dz))
                if is_lava_like(blk) or str(blk.get("name") or "") == "spawner":
                    junction_unsafe = True
                    break

        after_counts, after_items = OreHarvester.read_inventory(ctx)
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

        if covered_cells is not None:
            covered_cells.update(active_world.scanned_cells)
        if active_world.db_path is not None:
            active_world.save()

        ores_out = sorted(ore_records.values(), key=lambda r: (r["pos"][1], r["pos"][0], r["pos"][2]))
        success = stop_reason in {"cave_done", "return_home", "target_reached"} or escaped_surface
        return SkillResult(
            success,
            f"Explored {cave_entry['kind']} ({len(dive_visited)} cells, {torches_placed} torches, stop={stop_reason})",
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
                "visited_count": len(dive_visited),
                "world_stats": active_world.stats(),
                "report": report,
                "classification": classification,
            },
        )

    @staticmethod
    def _sync_live_pos(ctx: WorkflowContext) -> Optional[List[int]]:
        return WorldNavigator.sync_live_pos(ctx)

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
        """Eat edible food inside the cave when hurt or hungry instead of abandoning exploration."""
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
        return OreHarvester.read_inventory(ctx)

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
        shared_cache = ctx.extra.get("block_cache") if isinstance(ctx.extra, dict) else None
        world = self._resolve_world(world=ctx.extra.get("world_3d") if isinstance(ctx.extra, dict) else None, block_cache=shared_cache, persistent_world=False)
        return OreHarvester.mine_vein(
            ctx,
            world,
            stance_pos,
            seed_pos,
            target_key,
            targets,
            harvested_totals,
            read_fn,
            ore_records,
            invalidate=invalidate,
            max_dist=max_dist,
            max_vein=max_vein,
        )

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
        r_chk = max(2, int(min_spacing))
        for dx in range(-r_chk, r_chk + 1):
            for dy in (-2, -1, 0, 1, 2):
                for dz in range(-r_chk, r_chk + 1):
                    if abs(dx) + abs(dz) <= r_chk and str(read_fn((ax + dx, ay + dy, az + dz)).get("name") or "") in MINE_TORCH_BLOCKS:
                        return False
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
                return False
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

    def navigate_with_ops(
        self,
        target_pos: Sequence[int],
        *,
        ctx: Optional[WorkflowContext] = None,
        world: Optional[VoxelWorld3D] = None,
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
        """Navigate to target_pos using direct move_to, falling back to 3D A* with compound terrain ops."""
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
        active_world = self._resolve_world(world=world, block_cache=block_cache, persistent_world=False)

        def cached_read(p: Sequence[int]) -> Dict[str, Any]:
            return active_world.read_block(p, lambda k: ctx.read_block(k))

        try:
            stand_cell(ctx, [tx, ty, tz], tolerance=1.5)
            return SkillResult(True, f"Reached {[tx, ty, tz]} directly.", data={"pos": [tx, ty, tz], "steps": 0, "used_ops": False})
        except WorkflowStepError as exc:
            if exc.code not in {"stalled", "path_not_found", "unreachable_stance", "timeout"}:
                return SkillResult(False, str(exc), error_code=exc.code)

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
                    return SkillResult(True, f"Reached {[tx, ty, tz]} via 3D A* navigation.", data={"pos": cur, "steps": steps_done, "used_ops": used_ops})

                radius = max(10, min(28, max(abs(cur[0] - tx), abs(cur[2] - tz)) + 6))
                path = Pathfinder3D.find_path(
                    active_world,
                    cur,
                    [tx, ty, tz],
                    cached_read,
                    goal_tolerance=1,
                    allow_pillar=allow_pillar,
                    max_pillar_height=4,
                    allow_dig=allow_dig,
                    max_dig_steps=3,
                    allow_bridge=allow_bridge,
                    max_bridge_steps=4,
                    allow_drop=allow_drop,
                    max_drop_steps=5,
                    anchor_pos=cur,
                    anchor_radius=radius,
                    max_nodes=420,
                )
                if not path:
                    break
                for step in path[:8]:
                    mode = str(step.get("mode") or "walk")
                    if mode in {"pillar", "bridge", "scaffold_drop", "dig"}:
                        used_ops = True
                    cur, _ = WorldNavigator.execute_step(
                        ctx,
                        active_world,
                        cur,
                        step,
                        cached_read,
                        allow_micro_dig=allow_dig,
                        allow_dig_headroom_on_pillar=allow_dig,
                        tolerance=0.9,
                    )
                    steps_done += 1
                    if abs(cur[0] - tx) <= 1 and abs(cur[2] - tz) <= 1 and abs(cur[1] - ty) <= 1:
                        return SkillResult(True, f"Reached {[tx, ty, tz]} via 3D A* navigation.", data={"pos": cur, "steps": steps_done, "used_ops": True})
            return SkillResult(False, f"Could not reach {[tx, ty, tz]} with 3D A* navigation.", error_code="route_blocked")
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
        world: Optional[VoxelWorld3D] = None,
    ) -> List[Dict[str, Any]]:
        """Discover nearby natural rock-roofed cave openings."""
        raw_st = self.client.get_status()
        st = raw_st.get("result") if isinstance(raw_st, dict) and "result" in raw_st else raw_st
        bp = parse_pos(origin if origin is not None else (st.get("pos") if isinstance(st, dict) else None))
        if bp is None:
            return []
        ox, oy, oz = int(math.floor(bp[0])), int(math.floor(bp[1])), int(math.floor(bp[2]))
        active_world = self._resolve_world(world=world, block_cache=block_cache, persistent_world=False)

        def raw_read(k: Tuple[int, int, int]) -> Dict[str, Any]:
            raw = self.client.get_block(k[0], k[1], k[2])
            if isinstance(raw, dict):
                blk = raw.get("result") if isinstance(raw.get("result"), dict) else raw
                if isinstance(blk, dict) and blk.get("name"):
                    return {"name": str(blk["name"]), "properties": dict(blk.get("properties") or {})}
            return {"name": "unknown", "properties": {}}

        def read_blk(pos: Sequence[int]) -> Dict[str, Any]:
            return active_world.read_block(pos, raw_read)

        candidates: List[Dict[str, Any]] = []
        seed_coords: List[Tuple[int, int, int]] = []
        try:
            fb = getattr(self.client, "find_blocks", None) or getattr(self.client, "get_blocks", None)
            if callable(fb):
                ores = fb(types=list(ORE_DROP_MAP), radius=min(32, int(search_radius)), limit=24) or []
                for row in ores:
                    p = row.get("pos") if isinstance(row, dict) else None
                    if not isinstance(p, (list, tuple)) or len(p) != 3:
                        continue
                    for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        seed_coords.append((int(p[0]) + dx, int(p[1]), int(p[2]) + dz))
        except Exception:
            pass

        step = 4
        for dx in range(-int(search_radius), int(search_radius) + 1, step):
            for dz in range(-int(search_radius), int(search_radius) + 1, step):
                if dx == 0 and dz == 0:
                    continue
                for dy in range(-14, 10, 3):
                    seed_coords.append((ox + dx, oy + dy, oz + dz))

        rock_roof_blocks = {
            "stone", "deepslate", "andesite", "diorite", "granite", "tuff",
            "sandstone", "red_sandstone", "terracotta", "calcite", "cobblestone",
            "dripstone_block", "netherrack",
        } | set(ORE_DROP_MAP)

        seen_clusters: List[Tuple[int, int, int]] = []
        for sx, sy, sz in seed_coords:
            if any(abs(sx - cx) + abs(sz - cz) < 10 and abs(sy - cy) < 8 for cx, cy, cz in seen_clusters):
                continue
            foot = read_blk((sx, sy, sz))
            head = read_blk((sx, sy + 1, sz))
            floor = read_blk((sx, sy - 1, sz))
            if not (is_passable(foot) and is_passable(head) and is_solid_floor(floor)):
                continue
            if not any(str(read_blk((sx, sy + ky, sz)).get("name") or "air") in rock_roof_blocks for ky in range(2, 8)):
                continue
            rep = probe_cavity([sx, sy, sz], [sx, sy, sz], read_blk, limits={"max_probe_cells": 90, "horizontal_radius": 8, "vertical_span": 6}, block_cache=active_world.block_cache)
            if rep.get("lava_found") or rep.get("spawner_found") or int(rep.get("water_count", 0)) >= 3:
                continue
            if int(rep.get("air_count", 0)) < 10:
                continue
            graph = Pathfinder3D.plan_horizon(active_world, [sx, sy, sz], read_blk, max_nodes=80, max_radius=10)
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
        world: Optional[VoxelWorld3D] = None,
        persistent_world: bool = False,
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: float = 360.0,
        allow_damage: bool = True,
        critical_health: float = 6.0,
    ) -> SkillResult:
        """Cave-first expedition: locate nearby natural caves, traverse steep/lava terrain, and sweep ores."""
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
        active_world = self._resolve_world(world=world, block_cache=None, persistent_world=persistent_world)

        before_counts, _ = OreHarvester.read_inventory(ctx)
        if before_counts.get("torch", 0) < 4:
            if self._try_craft_torches(ctx, before_counts):
                before_counts, _ = OreHarvester.read_inventory(ctx)
        openings = self.find_cave_openings(origin=origin, search_radius=search_radius, max_candidates=max_caves, world=active_world)
        caves_explored: List[Dict[str, Any]] = []
        ores_all: List[Dict[str, Any]] = []
        torches_total = 0
        stop_reason = "no_caves_found" if not openings else "caves_exhausted"
        home_reason: Optional[str] = None

        for op in openings:
            if guard.remaining_timeout(999.0) < 25.0:
                break
            entry = op["entry_pos"]
            if norm_coord(entry) in active_world.scanned_cells:
                continue
            nav = self.navigate_with_ops(entry, ctx=ctx, world=active_world, allow_pillar=True, allow_dig=True, allow_bridge=True, allow_drop=True, max_steps=32)
            if not nav.success:
                continue
            cur_counts, _ = OreHarvester.read_inventory(ctx)
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
                world=active_world,
                guard=guard,
                prefer_flood_planner=True,
                allow_tall_cave_subgraph=True,
                allow_pillar=True,
                allow_micro_dig=True,
                allow_bridge=True,
                allow_drop=True,
                allow_surface_escape=True,
                stop_on_target=True,
            )
            caves_explored.extend(ex.data.get("caves") or [])
            ores_all.extend(ex.data.get("ores") or [])
            torches_total += int(ex.data.get("torches_placed", 0))

            after_counts, _ = OreHarvester.read_inventory(ctx)
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

        if active_world.db_path is not None:
            active_world.save()

        after_counts, _ = OreHarvester.read_inventory(ctx)
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
                "world_stats": active_world.stats(),
            },
        )
