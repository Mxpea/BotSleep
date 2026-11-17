"""Mining and Harvesting Skills: Tree Chopping, Staircase Mining, Excavation."""

import math
from functools import wraps
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import requests

from core.cave_probe import (
    FALLING_BLOCKS,
    PASSABLE_NON_AIR,
    classify_cavity,
    is_lava_like,
    is_passable,
    is_solid_floor,
    is_water_like,
    new_exposed_face_positions,
    ore_cell_hazard_free,
    probe_cavity,
)
from core.domain import AIR, parse_pos
from core.client import APIError
from core.memory import MemoryEngine
from core.mining_targets import (
    DEFAULT_MINING_TARGETS,
    best_usable_pickaxe,
    evaluate_home_trigger,
    evaluate_ore_block,
    parse_mining_targets,
    pickaxe_tier,
    recommended_mining_bands,
    unmet_spawnable_targets_at_y,
)
from core.pathfinder_3d import Pathfinder3D
from core.structure_spec import (
    CAVE_LIMITS,
    CellRule,
    DEFAULT_REPLACEABLE,
    MINE_BED_BLOCKS,
    MINE_CHEST_BLOCKS,
    MINE_DIGGABLE_BLOCKS,
    MINE_FLOOR_BLOCKS,
    MINE_HAZARD_BLOCKS,
    MINE_SHAFT_SPEC,
    MINE_TORCH_BLOCKS,
    ORE_DROP_MAP,
    SCAFFOLD_BLOCKS,
    SHELTER_PROTECTED_BLOCKS,
    StructureSpec,
    WOODEN_DOOR_BLOCKS,
    evaluate_mine_site,
    find_mine_sites as find_mine_sites_spec,
    inspect_structure,
    mine_shaft_spec,
    normalize_orientation,
    transform_rel_pos,
)
from skills.action_workflow import (
    ActionStep,
    ActionWorkflowRunner,
    PICKAXE_PRIORITY,
    WorkflowContext,
    WorkflowGuard,
    WorkflowStepError,
    WorkflowUncertainError,
    UNKNOWN_ERROR_CODES,
    aim_at,
    dig_block,
    escape_to_surface,
    place_block,
    stand_at,
    stand_cell,
)
from skills.cave_explore import CaveExploreSkill
from skills.world_navigator import WorldNavigator
from core.world_3d import VoxelWorld3D
from . import BaseSkill, SkillResult


def _unknown_result(result: SkillResult) -> bool:
    data = result.data if isinstance(result.data, dict) else {}
    return data.get("action_state") == "unknown" or result.error_code in UNKNOWN_ERROR_CODES


def _raise_if_unknown(result: SkillResult) -> None:
    """A new child workflow must never erase an earlier ambiguous write."""
    if _unknown_result(result):
        data = dict(result.data or {})
        data.setdefault("action_state", "unknown")
        data.setdefault("cleanup_skipped", "workflow_unknown")
        data.setdefault("executed", data["action_state"] == "unknown")
        raise WorkflowUncertainError(result.error_code or "action_unconfirmed", result.message, data)


def _uncertain_boundary(function):
    """Keep standalone mining entrypoints on the SkillResult contract."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except WorkflowUncertainError as exc:
            data = dict(exc.data)
            data.setdefault("executed", data.get("action_state") == "unknown")
            return SkillResult(False, str(exc), data=data, error_code=exc.code)
    return wrapped


def _journal_boundary(function):
    """Persist one non-replayable intent around the standalone mining run."""
    @wraps(function)
    def wrapped(self, journal, *args, **kwargs):
        if journal.has_pending_intent():
            return SkillResult(False,
                "Refusing mining while the checkpoint has an unresolved pending intention.",
                error_code="pending_intent_active",
                data={"executed": False, "structure_verified": False})
        intent = {"operation": "mine_shaft", "action_state": "pending"}
        journal.update(pending=intent)
        try:
            result = function(self, journal, *args, **kwargs)
        except Exception as exc:
            data = dict(getattr(exc, "data", {}) or {})
            data["action_state"] = "unknown"
            data.setdefault("cleanup_skipped", "workflow_unknown")
            data.setdefault("executed", True)
            code = getattr(exc, "code", "internal_error")
            if code == "not_connected":
                data.update(api_error_code=code, cleanup_skipped="disconnected_unknown")
                code = "action_unconfirmed"
            result = SkillResult(False, str(exc), data=data,
                                 error_code=code)
        if not isinstance(result, SkillResult):
            result = SkillResult(False, "Mining returned an invalid result",
                data={"executed": True, "action_state": "unknown", "cleanup_skipped": "workflow_unknown"},
                error_code="invalid_response")
        pending = {**intent, "action_state": "unknown"} if _unknown_result(result) else None
        journal.update(pending=pending, last_result={"success": result.success, "message": result.message,
            "error_code": result.error_code, "data": result.data})
        return result
    return wrapped


class ChopTreeSkill(BaseSkill):
    """Locates the nearest accessible tree, cleanly harvests all contiguous logs,

    and respects configurable target count and working time limits.
    """

    KNOWN_LOGS = [
        "oak_log",
        "birch_log",
        "spruce_log",
        "jungle_log",
        "acacia_log",
        "dark_oak_log",
        "mangrove_log",
        "cherry_log",
        "pale_oak_log",
    ]

    def __init__(self, client, memory: Optional[MemoryEngine] = None):
        super().__init__(client)
        self.memory = memory or MemoryEngine()

    def validate_preconditions(self, **kwargs) -> bool:
        """Verifies bot is connected and ready."""
        st = self.client.get_status().get("result", {})
        return bool(st and st.get("ready"))

    def execute(
        self,
        target_count: int = 5,
        max_duration_seconds: float = 60.0,
        search_radius: int = 32,
        clean_entire_tree: bool = True,
        log_types: Optional[List[str]] = None,
    ) -> SkillResult:
        """Finds the nearest tree and cleanly chops logs until target count or time limit is reached.

        Args:
            target_count: Target number of logs to harvest (default 5).
            max_duration_seconds: Time limit in seconds for the operation (default 60s).
            search_radius: Max distance in blocks to search for trees (default 32).
            clean_entire_tree: If True, fully harvests the current tree before moving to the next.
            log_types: Specific log types to chop (defaults to all known wood logs).

        Returns:
            SkillResult containing detailed completion status, logs collected, and timing.
        """
        if not self.validate_preconditions():
            return SkillResult(
                success=False,
                error_code="not_ready",
                message="Bot is not connected or not ready.",
            )

        start_time = time.time()
        harvested_total = 0
        trees_cleared = 0
        harvested_breakdown: Dict[str, int] = {}
        target_logs = log_types or self.KNOWN_LOGS
        unreachable_trees: Set[Tuple[int, int]] = set()

        while harvested_total < target_count:
            # Check elapsed time limit
            elapsed = time.time() - start_time
            if elapsed >= max_duration_seconds:
                break

            # 1. Search for nearest candidate logs
            url = f"{self.client.base_url}/find_blocks"
            try:
                resp = requests.post(
                    url,
                    headers=self.client.headers,
                    json={"types": target_logs, "radius": search_radius, "limit": 40},
                    timeout=10,
                ).json()
            except Exception as e:
                return SkillResult(
                    success=False,
                    error_code="api_error",
                    message=f"Failed to scan for trees: {e}",
                    data={"harvested_count": harvested_total},
                )

            found_logs = resp.get("result", {}).get("blocks", [])
            if not found_logs:
                break

            # 2. Group logs by tree trunks and filter out unreachable ones
            tree_groups = self._group_logs_into_trees(found_logs)
            candidate_trees = [
                tg for tg in tree_groups
                if (tg["trunk_x"], tg["trunk_z"]) not in unreachable_trees
            ]

            if not candidate_trees:
                # All detected trees are either unreachable or no trees left
                break

            # Pick the closest candidate tree
            active_tree = candidate_trees[0]
            tree_key = (active_tree["trunk_x"], active_tree["trunk_z"])
            tree_logs = active_tree["logs"]

            if not clean_entire_tree:
                remaining_needed = target_count - harvested_total
                tree_logs = tree_logs[:remaining_needed]

            # 3. Chop all logs of this tree (accessible order: bottom-to-top)
            chopped_in_this_tree = 0
            tree_failed = False

            for log_entry in tree_logs:
                # Check time limit
                if time.time() - start_time >= max_duration_seconds:
                    break
                if harvested_total >= target_count and not clean_entire_tree:
                    break

                lx, ly, lz = log_entry["pos"]
                block_name = log_entry["name"]

                dig_res = self.client.dig(lx, ly, lz, collect=True)
                if dig_res.get("ok"):
                    harvested_total += 1
                    chopped_in_this_tree += 1
                    harvested_breakdown[block_name] = harvested_breakdown.get(block_name, 0) + 1
                else:
                    err = dig_res.get("error", {}).get("code", "unknown")
                    # If stalled or unreachable, mark tree as unreachable and switch to next tree
                    if err in ["stalled", "path_not_found", "out_of_reach"]:
                        unreachable_trees.add(tree_key)
                        tree_failed = True
                        break

            if not tree_failed and chopped_in_this_tree > 0:
                trees_cleared += 1

        total_elapsed = round(time.time() - start_time, 1)
        target_reached = harvested_total >= target_count
        time_limit_reached = total_elapsed >= max_duration_seconds

        success = target_reached or (harvested_total > 0 and not time_limit_reached)

        summary_msg = (
            f"Chopped {harvested_total}/{target_count} logs from {trees_cleared} tree(s) "
            f"in {total_elapsed}s. Target reached: {target_reached}, Time limit reached: {time_limit_reached}."
        )

        # Update memory engine episode
        self.memory.append_episode(
            event_type="chop_tree",
            summary=summary_msg,
            details={
                "harvested_count": harvested_total,
                "target_count": target_count,
                "elapsed_seconds": total_elapsed,
                "trees_cleared": trees_cleared,
                "breakdown": harvested_breakdown,
            },
        )

        return SkillResult(
            success=success,
            message=summary_msg,
            data={
                "harvested_count": harvested_total,
                "target_count": target_count,
                "elapsed_seconds": total_elapsed,
                "target_reached": target_reached,
                "time_limit_reached": time_limit_reached,
                "trees_cleared": trees_cleared,
                "breakdown": harvested_breakdown,
            },
            error_code=None if target_reached else ("time_limit_exceeded" if time_limit_reached else "no_more_trees"),
        )

    def _group_logs_into_trees(self, found_logs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Clusters log blocks into distinct trees based on horizontal column proximity."""
        trees: List[Dict[str, Any]] = []

        for log in found_logs:
            lx, ly, lz = log["pos"]
            assigned = False
            for tree in trees:
                tx, tz = tree["trunk_x"], tree["trunk_z"]
                if abs(lx - tx) <= 1 and abs(lz - tz) <= 1:
                    tree["logs"].append(log)
                    tree["min_dist"] = min(tree["min_dist"], log.get("dist", 999))
                    assigned = True
                    break

            if not assigned:
                trees.append({
                    "trunk_x": lx,
                    "trunk_z": lz,
                    "min_dist": log.get("dist", 999),
                    "logs": [log],
                })

        # Sort logs within each tree from bottom (lowest Y) to top
        for tree in trees:
            tree["logs"].sort(key=lambda b: (b["pos"][1], b["dist"]))

        # Sort trees by closest distance to the bot
        trees.sort(key=lambda t: t["min_dist"])
        return trees


class MineShaftSkill(BaseSkill):
    """Build a 5-layer downward staircase mine with an open connected 4x3x2
    workstation platform (crafting_table, furnace, chest, bed anchors) and
    continuation mining branch via StructureSpec + ActionWorkflow.
    """

    FLUID_HAZARDS = frozenset({"water", "flowing_water", "lava", "flowing_lava"})
    FALLING_BLOCKS = frozenset({"sand", "red_sand", "gravel"})
    SCAFFOLD_PRIORITY: Tuple[str, ...] = tuple(SCAFFOLD_BLOCKS) + (
        "andesite",
        "diorite",
        "granite",
        "tuff",
        "deepslate",
    )

    def __init__(self, client, memory: Optional[MemoryEngine] = None):
        super().__init__(client)
        self.memory = memory or MemoryEngine()

    def validate_preconditions(self, **kwargs) -> bool:
        """Verify bot is connected and ready."""
        raw = self.client.get_status()
        st = raw.get("result") if isinstance(raw, dict) and "result" in raw else raw
        return bool(isinstance(st, dict) and st.get("ready"))

    def find_mine_sites(
        self,
        bot_pos: Optional[Sequence[float]] = None,
        *,
        radius: int = 10,
        orientations: Optional[Sequence[str]] = None,
        branch_length: int = 4,
        max_floor_patches: int = 6,
        min_solid_yield: int = 16,
        max_results: int = 5,
    ) -> List[Dict[str, Any]]:
        """Locate and rank safe candidate 5-layer staircase mine entry plots."""
        if bot_pos is None:
            raw_status = self.client.get_status()
            st = raw_status.get("result") if isinstance(raw_status, dict) and "result" in raw_status else raw_status
            bot_pos = parse_pos(st.get("pos") if isinstance(st, dict) else None)
            if bot_pos is None:
                return []
        return find_mine_sites_spec(
            bot_pos,
            lambda p: self.client.get_block(p[0], p[1], p[2]),
            radius=radius,
            orientations=orientations,
            branch_length=branch_length,
            max_floor_patches=max_floor_patches,
            min_solid_yield=min_solid_yield,
            max_results=max_results,
        )

    def inspect_mine_shaft(
        self,
        origin: Sequence[int],
        *,
        orientation: str = "south",
        branch_length: int = 4,
        include_workstations: bool = False,
        read_block: Optional[Callable[[List[int]], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Inspect an existing 5-layer mine + 4x3x2 platform + branch against StructureSpec."""
        norm_orient = normalize_orientation(orientation)
        spec = mine_shaft_spec(branch_length=branch_length, include_workstations=include_workstations)
        reader = read_block or (lambda p: self.client.get_block(p[0], p[1], p[2]))
        return inspect_structure(spec, origin, reader, orientation=norm_orient)

    def execute(
        self,
        origin: Optional[Sequence[int]] = None,
        *,
        orientation: Optional[str] = None,
        branch_length: int = 4,
        radius: int = 10,
        collect_drops: bool = False,
        return_to_surface: bool = False,
        require_pickaxe: bool = True,
        place_workstation: Optional[bool] = None,
        targets: Optional[Mapping[str, Any]] = None,
        cave_policy: Optional[Mapping[str, Any]] = None,
        home_thresholds: Optional[Mapping[str, Any]] = None,
        handle_caves: bool = True,
        target_y: Optional[int] = None,
        target_ys: Optional[Sequence[int]] = None,
        mining_mode: str = "single",
        rib_length: int = 12,
        rib_spacing: int = 4,
        max_trunk_steps: int = 40,
        prefer_caves_first: bool = False,
        initial_quota_progress: Optional[Mapping[str, int]] = None,
        resume_progress: Optional[Mapping[str, Any]] = None,
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: float = 210.0,
        allow_damage: bool = True,
    ) -> SkillResult:
        """Locate (if omitted), excavate 5-step stair/4x3x2 platform/branch (or deep target_y fishbone), and link CaveExploreSkill."""
        return self.build_mine_shaft(
            origin=origin,
            orientation=orientation,
            branch_length=branch_length,
            radius=radius,
            collect_drops=collect_drops,
            return_to_surface=return_to_surface,
            require_pickaxe=require_pickaxe,
            place_workstation=place_workstation,
            targets=targets,
            cave_policy=cave_policy,
            home_thresholds=home_thresholds,
            handle_caves=handle_caves,
            target_y=target_y,
            mining_mode=mining_mode,
            rib_length=rib_length,
            rib_spacing=rib_spacing,
            max_trunk_steps=max_trunk_steps,
            resume_progress=resume_progress,
            cancel_event=cancel_event,
            timeout_seconds=timeout_seconds,
            allow_damage=allow_damage,
        )

    @_uncertain_boundary
    def build_mine_shaft(
        self,
        origin: Optional[Sequence[int]] = None,
        *,
        orientation: Optional[str] = None,
        branch_length: int = 4,
        radius: int = 10,
        collect_drops: bool = False,
        return_to_surface: bool = False,
        require_pickaxe: bool = True,
        place_workstation: Optional[bool] = None,
        targets: Optional[Mapping[str, Any]] = None,
        cave_policy: Optional[Mapping[str, Any]] = None,
        home_thresholds: Optional[Mapping[str, Any]] = None,
        handle_caves: bool = True,
        target_y: Optional[int] = None,
        target_ys: Optional[Sequence[int]] = None,
        mining_mode: str = "single",
        rib_length: int = 12,
        rib_spacing: int = 4,
        max_trunk_steps: int = 40,
        prefer_caves_first: bool = False,
        initial_quota_progress: Optional[Mapping[str, int]] = None,
        resume_progress: Optional[Mapping[str, Any]] = None,
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: float = 210.0,
        allow_damage: bool = True,
    ) -> SkillResult:
        """Excavate 5-layer downward staircase + 4x3x2 platform + branch with cave detection and target mining."""
        try:
            branch_len = max(1, min(12, int(branch_length)))
        except (TypeError, ValueError):
            return SkillResult(
                False,
                "branch_length must be an integer between 1 and 12.",
                error_code="bad_args",
                data={"executed": False, "structure_verified": False},
            )

        resumed = dict(resume_progress) if isinstance(resume_progress, Mapping) else None
        if resumed and origin is None:
            origin = resumed.get("origin")
            if orientation is None:
                orientation = resumed.get("orientation")

        excavation_spec = mine_shaft_spec(branch_length=branch_len, include_workstations=False)
        site: Optional[Dict[str, Any]] = None
        if origin is None:
            orient_filter = [normalize_orientation(orientation)] if orientation is not None else None
            try:
                sites = self.find_mine_sites(
                    radius=radius,
                    orientations=orient_filter,
                    branch_length=branch_len,
                    max_results=8,
                )
            except Exception as exc:
                return SkillResult(
                    False,
                    f"Failed scanning nearby mine plots: {exc}",
                    error_code=getattr(exc, "code", "internal_error"),
                    data={"executed": False, "structure_verified": False},
                )
            if not sites:
                return SkillResult(
                    False,
                    f"No safe 5-layer mine plot found within radius {radius}.",
                    error_code="no_mine_site_found",
                    data={"executed": False, "structure_verified": False},
                )
            site = sites[0]
            approach_started = False
            try:
                raw_cur = self.client.get_status()
                st_cur = raw_cur.get("result") if isinstance(raw_cur, dict) and "result" in raw_cur else raw_cur
                pos_cur = parse_pos(st_cur.get("pos") if isinstance(st_cur, dict) else None)
                first_se = parse_pos(site.get("surface_entry"))
                if pos_cur is not None and first_se is not None and math.dist(pos_cur, first_se) > 1.5:
                    nav_pre = CaveExploreSkill(self.client, memory=self.memory)
                    for cand in sites[:6]:
                        se = parse_pos(cand.get("surface_entry"))
                        if se is None:
                            continue
                        approach_started = True
                        approach = nav_pre.navigate_with_ops(
                            [math.floor(se[0]), math.floor(se[1]), math.floor(se[2])],
                            allow_pillar=True,
                            allow_dig=True,
                            allow_bridge=True,
                            allow_drop=True,
                            max_steps=28,
                            timeout_seconds=12.0,
                        )
                        _raise_if_unknown(approach)
                        raw_chk = self.client.get_status()
                        st_chk = raw_chk.get("result") if isinstance(raw_chk, dict) and "result" in raw_chk else raw_chk
                        pos_chk = parse_pos(st_chk.get("pos") if isinstance(st_chk, dict) else None)
                        if pos_chk is not None and math.dist(pos_chk, se) <= 1.6:
                            site = cand
                            break
            except WorkflowUncertainError:
                raise
            except APIError as exc:
                if approach_started and (exc.code in UNKNOWN_ERROR_CODES | {"not_connected"}
                        or (isinstance(exc.details, dict) and exc.details.get("action_state") == "unknown")):
                    code = "action_unconfirmed" if exc.code == "not_connected" else exc.code
                    raise WorkflowUncertainError(code, str(exc), {
                        "executed": True, "action_state": "unknown", "api_error_code": exc.code,
                        "cleanup_skipped": "disconnected_unknown" if exc.code == "not_connected" else "workflow_unknown"}) from None
            except Exception:
                pass
            int_origin = list(site["origin"])
            norm_orient = site["orientation"]
        else:
            parsed = parse_pos(origin, integer=True)
            if parsed is None:
                return SkillResult(
                    False,
                    "Mine origin must be three integer coordinates [x, y, z].",
                    error_code="bad_args",
                    data={"executed": False, "structure_verified": False},
                )
            int_origin = list(parsed)
            if orientation is not None:
                norm_orient = normalize_orientation(orientation)
            else:
                ranked = []
                for cand_orient in ("south", "west", "north", "east"):
                    cand = evaluate_mine_site(
                        int_origin,
                        cand_orient,
                        lambda p: self.client.get_block(p[0], p[1], p[2]),
                        branch_length=branch_len,
                    )
                    if cand is not None:
                        ranked.append(cand)
                if ranked:
                    ranked.sort(key=lambda s: float(s["score"]))
                    site = ranked[0]
                    norm_orient = site["orientation"]
                else:
                    norm_orient = "south"
            if resumed is not None:
                # Resuming an existing mine shaft from checkpoint: verify core hub instead of unmined solid yield
                hub_ok = inspect_structure(
                    self._hub_only_spec(branch_len, include_workstations=bool(resumed.get("workstation_deployed"))),
                    int_origin,
                    lambda p: self.client.get_block(p[0], p[1], p[2]),
                    orientation=norm_orient,
                )
                if hub_ok.get("complete"):
                    site = {
                        "origin": int_origin,
                        "orientation": norm_orient,
                        "surface_entry": transform_rel_pos(
                            excavation_spec.anchors["surface_entry"].rel_pos,
                            int_origin,
                            norm_orient,
                            integer=False,
                        ),
                    }
            if site is None:
                site = evaluate_mine_site(
                    int_origin,
                    norm_orient,
                    lambda p: self.client.get_block(p[0], p[1], p[2]),
                    branch_length=branch_len,
                )

        if site is None:
            return SkillResult(
                False,
                f"Requested mine site {int_origin} ({norm_orient}) failed safety/yield validation.",
                error_code="unsafe_site",
                data={"executed": False, "structure_verified": False, "origin": int_origin, "orientation": norm_orient},
            )

        deep_or_fishbone = (
            target_y is not None
            or bool(target_ys)
            or str(mining_mode).lower() == "fishbone"
        )
        guard = WorkflowGuard(
            self.client,
            cancel_event=cancel_event,
            timeout_seconds=timeout_seconds,
            max_reads=120000 if deep_or_fishbone else 6000,
            allow_damage=allow_damage,
            critical_health=6.0,
        )
        start_anchor = "platform_center" if resumed is not None else "surface_entry"
        end_anchor = "surface_entry" if return_to_surface else "platform_center"
        policy = dict(CAVE_LIMITS)
        if cave_policy:
            policy.update(cave_policy)
        parsed_targets = parse_mining_targets(targets) if targets is not None else {}

        ctx = WorkflowContext(
            guard=guard,
            spec=excavation_spec,
            origin=int_origin,
            orientation=norm_orient,
            player_start=transform_rel_pos(excavation_spec.anchors[start_anchor].rel_pos, int_origin, norm_orient, integer=False),
            player_end=transform_rel_pos(excavation_spec.anchors[end_anchor].rel_pos, int_origin, norm_orient, integer=False),
            extra={
                "branch_length": branch_len,
                "collect_drops": False,  # Plan v2 enforces collect=False; floor pickup handles drops
                "return_to_surface": bool(return_to_surface),
                "require_pickaxe": bool(require_pickaxe),
                "place_workstation_mode": place_workstation,
                "deploy_workstation": bool(resumed.get("workstation_deployed")) if resumed else False,
                "workstation_report": {},
                "targets": parsed_targets,
                "raw_targets": dict(targets or {}) if isinstance(targets, Mapping) else {},
                "cave_policy": policy,
                "home_thresholds": dict(home_thresholds or {}) if isinstance(home_thresholds, Mapping) else None,
                "handle_caves": bool(handle_caves),
                "target_y": int(target_y) if target_y is not None else (resumed.get("target_y") if resumed else None),
                "target_ys": [int(v) for v in target_ys] if target_ys else None,
                "mining_mode": str(mining_mode or (resumed.get("mining_mode") if resumed else "single")).lower(),
                "rib_length": max(2, min(24, int(rib_length))),
                "rib_spacing": max(2, min(8, int(rib_spacing))),
                "max_trunk_steps": max(4, min(120, int(max_trunk_steps))),
                "prefer_caves_first": bool(prefer_caves_first),
                "deep_pos": list(resumed.get("deep_pos")) if resumed and resumed.get("deep_pos") else None,
                "deep_fwd": list(resumed.get("deep_fwd")) if resumed and resumed.get("deep_fwd") else None,
                "deep_trail": [list(p) for p in (resumed.get("deep_trail") or [])] if resumed else [],
                "trunk_done": int(resumed.get("trunk_done", 0)) if resumed else 0,
                "resume_progress": resumed,
                "block_cache": {},
                "world_3d": None,
                "caves": list(resumed.get("caves", [])) if resumed else [],
                "sealed_caves": list(resumed.get("sealed_caves", [])) if resumed else [],
                "ore_records": {
                    (int(r["pos"][0]), int(r["pos"][1]), int(r["pos"][2])): dict(r)
                    for r in (resumed.get("ores", []) if resumed else [])
                    if isinstance(r, Mapping) and isinstance(r.get("pos"), list) and len(r["pos"]) == 3
                },
                "torches_placed": int(resumed.get("torches_placed", 0)) if resumed else 0,
                "quota_base": dict(resumed.get("quota_progress", {})) if resumed else dict(initial_quota_progress or {}),
                "quota_progress": dict(resumed.get("quota_progress", {})) if resumed else dict(initial_quota_progress or {}),
                "heading_index": int(resumed.get("heading_index", 0)) if resumed else 0,
                "steps_completed": int(resumed.get("steps_completed", 0)) if resumed else 0,
                "unsafe_count": int(resumed.get("unsafe_count", 0)) if resumed else 0,
                "stop_reason": None,
                "home_reason": None,
                "branch_redirected_or_sealed": bool(
                    deep_or_fishbone or (resumed and (resumed.get("heading_index", 0) > 0 or resumed.get("sealed_caves")))
                ),
                "site": site,
                "start_tolerance": 1.6,
                "end_tolerance": 1.0,
                "before_inventory": {},
            },
        )
        steps = self._build_steps(
            return_to_surface=bool(return_to_surface),
            place_workstation=place_workstation,
            resumed=resumed is not None,
            deep_or_fishbone=deep_or_fishbone,
        )

        def verify_fn(c: WorkflowContext) -> Dict[str, Any]:
            include_ws = bool(c.extra.get("deploy_workstation"))
            stop_reason_now = str(c.extra.get("stop_reason") or "branch_done")
            if c.extra.get("branch_redirected_or_sealed") or stop_reason_now != "branch_done":
                spec_to_check = self._hub_only_spec(branch_len, include_workstations=include_ws)
            else:
                spec_to_check = mine_shaft_spec(branch_length=branch_len, include_workstations=include_ws)
            corridor_only = (
                not include_ws
                and (
                    c.extra.get("target_y") is not None
                    or c.extra.get("target_ys")
                    or str(c.extra.get("mining_mode") or "").lower() == "fishbone"
                )
            )
            if corridor_only:
                kept = tuple(
                    cell for cell in spec_to_check.cells
                    if cell.stage not in {"platform_clear", "platform_floor"} or int(cell.rel_pos[0]) == 0
                )
                spec_to_check = StructureSpec(
                    spec_id=spec_to_check.spec_id,
                    stages=spec_to_check.stages,
                    cells=kept,
                    anchors=spec_to_check.anchors,
                    clearance_rel=spec_to_check.clearance_rel,
                    site_spec=None,
                )
            return inspect_structure(spec_to_check, c.origin, c.read_block, orientation=c.orientation)

        result = ActionWorkflowRunner().run(
            ctx,
            steps,
            precheck=self._workflow_precheck,
            verify_structure=verify_fn,
            ready_timeout=5.0,
        )

        if _unknown_result(result):
            return result

        # Report net gained materials and rotated workstation anchors
        before_inv = dict(ctx.extra.get("before_inventory") or {})
        try:
            after_inv = ctx.inventory_counts()
        except Exception:
            after_inv = {}
        harvested = {
            k: after_inv[k] - before_inv.get(k, 0)
            for k in after_inv
            if after_inv[k] > before_inv.get(k, 0)
        }
        workstation_slots = {
            slot: transform_rel_pos(excavation_spec.anchors[slot].rel_pos, int_origin, norm_orient, integer=True)
            for slot in (
                "workstation_bed_foot",
                "workstation_bed_head",
                "workstation_crafting_table",
                "workstation_furnace",
                "workstation_chest_1",
                "workstation_chest_2",
                "workstation_torch",
            )
        }
        ores_out = sorted(ctx.extra["ore_records"].values(), key=lambda r: (r["pos"][1], r["pos"][0], r["pos"][2]))
        stop_reason = str(
            ctx.extra.get("stop_reason")
            or ("branch_done" if result.success else (result.error_code or "safety_interrupted"))
        )
        if not result.success and result.error_code == "cave_abandoned":
            stop_reason = "cave_abandoned"
        home_reason = ctx.extra.get("home_reason")
        progress_snapshot = {
            "origin": list(int_origin),
            "orientation": norm_orient,
            "branch_length": branch_len,
            "workstation_deployed": bool(ctx.extra.get("deploy_workstation")),
            "heading_index": int(ctx.extra.get("heading_index", 0)),
            "steps_completed": int(ctx.extra.get("steps_completed", 0)),
            "unsafe_count": int(ctx.extra.get("unsafe_count", 0)),
            "target_y": ctx.extra.get("target_y"),
            "mining_mode": ctx.extra.get("mining_mode"),
            "deep_pos": ctx.extra.get("deep_pos"),
            "deep_fwd": ctx.extra.get("deep_fwd"),
            "deep_trail": list(ctx.extra.get("deep_trail") or []),
            "trunk_done": int(ctx.extra.get("trunk_done", 0)),
            "caves": list(ctx.extra.get("caves") or []),
            "sealed_caves": list(ctx.extra.get("sealed_caves") or []),
            "ores": ores_out,
            "torches_placed": int(ctx.extra.get("torches_placed", 0)),
            "quota_progress": dict(ctx.extra.get("quota_progress") or {}),
            "raw_targets": dict(ctx.extra.get("raw_targets") or {}),
        }

        result.data["workstation_slots"] = workstation_slots
        result.data["workstation_deployed"] = bool(ctx.extra.get("deploy_workstation"))
        result.data["workstation"] = dict(ctx.extra.get("workstation_report") or {})
        result.data["harvested"] = harvested
        result.data["branch_length"] = branch_len
        result.data["caves"] = list(ctx.extra.get("caves") or [])
        result.data["ores"] = ores_out
        result.data["torches_placed"] = int(ctx.extra.get("torches_placed", 0))
        result.data["stop_reason"] = stop_reason
        result.data["home_reason"] = home_reason
        result.data["mining_progress"] = progress_snapshot

        if stop_reason == "return_home" and home_reason:
            result = SkillResult(
                True,
                f"Mining checkpoint pausing for rest/resupply ({home_reason}).",
                error_code="return_home_requested",
                data=dict(result.data),
            )

        if result.success:
            ws_tag = " + workstation(bed/respawn/table+furnace/double-chest/torch)" if result.data["workstation_deployed"] else ""
            self.memory.append_episode(
                event_type="mine_shaft",
                summary=f"Excavated 5-step staircase + 4x3x2 platform + {branch_len}-block branch{ws_tag} at {int_origin} ({norm_orient}) [{stop_reason}]",
                details={
                    "origin": int_origin,
                    "orientation": norm_orient,
                    "harvested": harvested,
                    "workstation_deployed": result.data["workstation_deployed"],
                    "stop_reason": stop_reason,
                    "caves": len(result.data["caves"]),
                },
            )
        return result

    @staticmethod
    def _hub_only_spec(branch_length: int, *, include_workstations: bool) -> StructureSpec:
        full = mine_shaft_spec(branch_length=branch_length, include_workstations=include_workstations)
        hub_stages = tuple(s for s in full.stages if s not in {"branch_floor", "branch_clear"})
        hub_cells = tuple(c for c in full.cells if c.stage in set(hub_stages))
        return StructureSpec(
            spec_id=full.spec_id,
            stages=hub_stages,
            cells=hub_cells,
            anchors=full.anchors,
            clearance_rel=full.clearance_rel,
            site_spec=None,
        )

    def _build_steps(
        self,
        *,
        return_to_surface: bool,
        place_workstation: Optional[bool],
        resumed: bool = False,
        deep_or_fishbone: bool = False,
    ) -> List[ActionStep]:
        move_branches = {
            "stalled": self._branch_recover_move,
            "path_not_found": self._branch_recover_move,
            "unreachable_stance": self._branch_recover_move,
            "timeout": self._branch_recover_move,
        }
        dig_branches = {
            "dig_failed": self._branch_retry_dig,
            **move_branches,
        }
        steps: List[ActionStep] = []
        if not resumed:
            steps.extend([
                ActionStep(
                    step_id="prepare_entry",
                    description="清理矿道地面入口并确认入口落脚地基稳固",
                    stage="stairs_clear",
                    action=self._wf_prepare_entry,
                    branches=dict(dig_branches),
                    max_branch_attempts=2,
                ),
                ActionStep(
                    step_id="excavate_stairs",
                    description="按 3 格净高与踏步地板校向下挖掘 5 层楼梯矿道",
                    stage="stairs_clear",
                    action=self._wf_excavate_stairs,
                    branches=dict(dig_branches),
                    max_branch_attempts=2,
                ),
                ActionStep(
                    step_id="excavate_platform",
                    description="沿矿道侧边以 4 格长边连通展开 4x3x2 工作站平台并修补地基",
                    stage="platform_clear",
                    action=self._wf_excavate_platform,
                    branches=dict(dig_branches),
                    max_branch_attempts=2,
                ),
            ])
        if place_workstation is not False and not resumed:
            steps.append(
                ActionStep(
                    step_id="deploy_workstation",
                    description="部署床(含右键保存重生点)、工作台+顶部熔炉、2宽大箱子与矿道墙火把",
                    stage="platform_clear",
                    action=self._wf_deploy_workstation,
                    branches=dict(move_branches),
                    max_branch_attempts=2,
                )
            )
        steps.append(
            ActionStep(
                step_id="excavate_branch",
                description="沿分支掘进（或深挖至目标层并执行间距3格鱼骨分支与洞穴联动）",
                stage="branch_clear",
                action=self._wf_deep_fishbone if deep_or_fishbone else self._wf_excavate_branch,
                branches=dict(dig_branches),
                max_branch_attempts=2,
            )
        )
        steps.append(
            ActionStep(
                step_id="settle_pose",
                description="回到 4x3x2 工作平台过道或沿楼梯返回地面入口",
                stage="branch_clear",
                action=lambda ctx: self._wf_settle_pose(ctx, return_to_surface=return_to_surface),
                branches=dict(move_branches),
                max_branch_attempts=2,
            )
        )
        return steps

    @staticmethod
    def _missing_workstation_items(counts: Dict[str, int]) -> List[str]:
        missing: List[str] = []
        if not any(counts.get(b, 0) >= 1 for b in MINE_BED_BLOCKS):
            missing.append("bed(x1)")
        if counts.get("crafting_table", 0) < 1:
            missing.append("crafting_table(x1)")
        if counts.get("furnace", 0) < 1:
            missing.append("furnace(x1)")
        chest_total = counts.get("chest", 0) + counts.get("trapped_chest", 0)
        if chest_total < 2:
            missing.append(f"chest(need 2, have {chest_total})")
        if counts.get("torch", 0) < 1:
            missing.append("torch(x1)")
        return missing

    def _workflow_precheck(self, ctx: WorkflowContext) -> None:
        counts = ctx.inventory_counts()
        ctx.extra["before_inventory"] = dict(counts)
        if ctx.extra.get("require_pickaxe", True) and ctx.select_item(PICKAXE_PRIORITY) is None:
            raise WorkflowStepError(
                "missing_pickaxe",
                "Need at least one usable pickaxe (wooden or better) to excavate stone staircase and chamber.",
                blocker_type="missing_item",
                suggested_recovery="craft_or_equip_pickaxe",
            )
        if ctx.extra.get("resume_progress") is not None:
            return  # Keep persisted deploy_workstation status when resuming checkpoint
        ws_mode = ctx.extra.get("place_workstation_mode")
        if ws_mode is False:
            ctx.extra["deploy_workstation"] = False
            return
        missing_ws = self._missing_workstation_items(counts)
        if ws_mode is True:
            if missing_ws:
                raise WorkflowStepError(
                    "missing_workstation_items",
                    f"Missing required workstation items: {', '.join(missing_ws)}",
                    blocker_type="missing_item",
                    suggested_recovery="craft_or_collect_workstation_items",
                )
            ctx.extra["deploy_workstation"] = True
        else:
            # place_workstation=None -> deploy automatically if inventory already has the workstation kit
            ctx.extra["deploy_workstation"] = not missing_ws

    def _check_cell_and_neighbors_safe(self, ctx: WorkflowContext, world: List[int]) -> str:
        """Read target block and verify it and its surrounding shell have no fluid or protected hazards."""
        protected = set(SHELTER_PROTECTED_BLOCKS) | set(WOODEN_DOOR_BLOCKS)
        cur_blk = self._cached_read(ctx, world)
        bname = str(cur_blk.get("name") or "unknown")
        props = cur_blk.get("properties") if isinstance(cur_blk.get("properties"), dict) else {}
        if bname in {"not_loaded", "unknown"}:
            raise WorkflowStepError("not_loaded", f"Block at {world} is unloaded.", blocker_type="unloaded", blocker_pos=world)
        if bname in MINE_HAZARD_BLOCKS or props.get("waterlogged") in (True, "true"):
            raise WorkflowStepError(
                "hazard_detected",
                f"Hazardous block {bname} at {world}",
                blocker_type="safety_interrupted",
                blocker_pos=world,
                blocker_block=bname,
            )
        if bname in protected or bname.endswith("_bed"):
            raise WorkflowStepError(
                "protected_block",
                f"Refusing to mine protected block {bname} at {world}",
                blocker_type="obstructed",
                blocker_pos=world,
                blocker_block=bname,
            )
        if bname == "bedrock":
            raise WorkflowStepError(
                "bedrock_blocked",
                f"Hit unbreakable bedrock at {world}",
                blocker_type="obstructed",
                blocker_pos=world,
                blocker_block=bname,
            )
        if bname not in (MINE_DIGGABLE_BLOCKS | DEFAULT_REPLACEABLE):
            raise WorkflowStepError(
                "unexpected_block",
                f"Unsupported block {bname} at {world}",
                blocker_type="obstructed",
                blocker_pos=world,
                blocker_block=bname,
            )

        for ndx, ndy, ndz in ((0, 1, 0), (0, -1, 0), (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1)):
            npos = [world[0] + ndx, world[1] + ndy, world[2] + ndz]
            nblk = self._cached_read(ctx, npos)
            nname = str(nblk.get("name") or "unknown")
            nprops = nblk.get("properties") if isinstance(nblk.get("properties"), dict) else {}
            if nname in self.FLUID_HAZARDS or nname == "lava" or nprops.get("waterlogged") in (True, "true"):
                raise WorkflowStepError(
                    "hazard_adjacent",
                    f"Adjacent liquid hazard {nname} at {npos} near {world}",
                    blocker_type="safety_interrupted",
                    blocker_pos=npos,
                    blocker_block=nname,
                )
            if ndy == 1 and nname in self.FALLING_BLOCKS:
                above2 = self._cached_read(ctx, [world[0], world[1] + 2, world[2]])
                if str(above2.get("name") or "air") in self.FLUID_HAZARDS:
                    raise WorkflowStepError(
                        "hazard_adjacent",
                        f"Liquid above falling {nname} at {[world[0], world[1] + 2, world[2]]}",
                        blocker_type="safety_interrupted",
                        blocker_pos=[world[0], world[1] + 2, world[2]],
                    )
        return bname

    @staticmethod
    def _cached_read(ctx: WorkflowContext, world: Sequence[int]) -> Dict[str, Any]:
        cache: Dict[Tuple[int, int, int], Dict[str, Any]] = ctx.extra.setdefault("block_cache", {})
        key = (int(world[0]), int(world[1]), int(world[2]))
        if key not in cache:
            cache[key] = ctx.read_block(key)
        return cache[key]

    @staticmethod
    def _invalidate_cache(ctx: WorkflowContext, world: Sequence[int]) -> None:
        cache: Dict[Tuple[int, int, int], Dict[str, Any]] = ctx.extra.setdefault("block_cache", {})
        px, py, pz = int(world[0]), int(world[1]), int(world[2])
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    cache.pop((px + dx, py + dy, pz + dz), None)

    SOFT_ROCK_TOOL_PRIORITY: Tuple[str, ...] = (
        "stone_pickaxe",
        "iron_pickaxe",
        "diamond_pickaxe",
        "netherite_pickaxe",
        "golden_pickaxe",
        "wooden_pickaxe",
    )
    SOFT_TERRAIN_BLOCKS: Set[str] = {
        "stone",
        "cobblestone",
        "andesite",
        "diorite",
        "granite",
        "dirt",
        "grass_block",
        "coarse_dirt",
        "gravel",
        "sand",
        "sandstone",
        "tuff",
        "netherrack",
    }

    def _dig_cell_safely(
        self,
        ctx: WorkflowContext,
        rel_pos: Tuple[int, int, int],
        *,
        collect: bool = False,
        allowed_extra: Optional[Set[str]] = None,
        relative: bool = True,
    ) -> int:
        """Safely dig one cell with neighbor hazard checks and falling-block settle recovery."""
        world = ctx.world_pos(rel_pos, integer=True) if relative else [int(rel_pos[0]), int(rel_pos[1]), int(rel_pos[2])]
        dug = 0
        allowed = set(MINE_DIGGABLE_BLOCKS | DEFAULT_REPLACEABLE | (allowed_extra or set()))
        for _attempt in range(5):
            bname = self._check_cell_and_neighbors_safe(ctx, world)
            if bname in AIR:
                break
            above_name = str(self._cached_read(ctx, [world[0], world[1] + 1, world[2]]).get("name") or "air")
            aim_at(ctx, world, relative=False)
            # Preserve iron/diamond pickaxe durability on soft upper stone when a stone pickaxe is available.
            tool_prio = self.SOFT_ROCK_TOOL_PRIORITY if bname in self.SOFT_TERRAIN_BLOCKS else None
            res = dig_block(ctx, world, relative=False, allowed_blocks=allowed, collect=False, tool_priority=tool_prio)
            self._invalidate_cache(ctx, world)
            if not res.get("skipped"):
                dug += 1
            if above_name in self.FALLING_BLOCKS:
                time.sleep(0.35)
                self._invalidate_cache(ctx, world)
                if str(self._cached_read(ctx, world).get("name") or "air") not in AIR:
                    continue
            break
        return dug

    def _ensure_floor_cell(self, ctx: WorkflowContext, rel_floor: Sequence[int], *, relative: bool = True) -> int:
        """Ensure a floor support cell is solid before the bot walks onto the cell above it."""
        world = ctx.world_pos(rel_floor, integer=True) if relative else [int(rel_floor[0]), int(rel_floor[1]), int(rel_floor[2])]
        blk = self._cached_read(ctx, world)
        name = str(blk.get("name") or "unknown")
        props = blk.get("properties") if isinstance(blk.get("properties"), dict) else {}
        if name in self.FLUID_HAZARDS or props.get("waterlogged") in (True, "true"):
            raise WorkflowStepError(
                "hazard_floor",
                f"Liquid hazard {name} at floor cell {world}",
                blocker_type="safety_interrupted",
                blocker_pos=world,
                blocker_block=name,
            )
        if name in MINE_FLOOR_BLOCKS:
            return 0
        if name in DEFAULT_REPLACEABLE:
            scaffold = ctx.select_item(self.SCAFFOLD_PRIORITY)
            if scaffold is None:
                raise WorkflowStepError(
                    "missing_floor_scaffold",
                    f"Hole at floor cell {world}, and inventory lacks cobblestone/dirt/stone to bridge it.",
                    blocker_type="missing_item",
                    blocker_pos=world,
                )
            aim_at(ctx, world, relative=False)
            place_block(ctx, scaffold, world, relative=False, replaceable=DEFAULT_REPLACEABLE)
            self._invalidate_cache(ctx, world)
            return 1
        raise WorkflowStepError(
            "unsafe_floor",
            f"Unsupported floor block {name} at {world}",
            blocker_type="safety_interrupted",
            blocker_pos=world,
            blocker_block=name,
        )

    def _step_to(
        self,
        ctx: WorkflowContext,
        rel_pos: Tuple[float, float, float],
        *,
        tolerance: float = 0.75,
        exact: bool = False,
    ) -> None:
        """Move cleanly within the already-excavated corridor/platform."""
        stand_at(ctx, rel_pos, relative=True, tolerance=tolerance, exact=exact)

    def _wf_prepare_entry(self, ctx: WorkflowContext) -> Dict[str, Any]:
        dug = 0
        # Ensure we are standing behind/level with surface_entry before clearing (0, 1, -1) / (0, 0, -1),
        # so an elevated bot on a slope cannot stand on (0, 0, -1) as its own floor cell.
        st_pos = parse_pos((ctx.guard.check_status(require_safe=False) or {}).get("pos"))
        bot_floor = (
            [int(math.floor(st_pos[0])), int(math.floor(st_pos[1])) - 1, int(math.floor(st_pos[2]))]
            if st_pos is not None
            else None
        )
        for rel in ((0, 1, -1), (0, 0, -1)):
            wpos = ctx.world_pos(rel, integer=True)
            bname = str(self._cached_read(ctx, wpos).get("name") or "air")
            if bname not in AIR:
                if bot_floor is not None and wpos == bot_floor:
                    self._step_to(ctx, (0.5, 0.0, -1.5), tolerance=0.75, exact=True)
                    bot_floor = None
                dug += self._dig_cell_safely(ctx, rel, collect=False)
        patched = self._ensure_floor_cell(ctx, (0, -1, -1))
        self._step_to(ctx, (0.5, 0.0, -0.5), tolerance=0.75, exact=True)
        return {"dug": dug, "patched": patched}

    def _wf_excavate_stairs(self, ctx: WorkflowContext) -> Dict[str, Any]:
        dug = 0
        patched = 0
        for step in range(1, 6):
            dz = step - 1
            foot_y = -step
            prev_rel = (0.5, 0.0, -0.5) if step == 1 else (0.5, float(-(step - 1)), float(dz - 1) + 0.5)
            self._step_to(ctx, prev_rel, tolerance=0.75, exact=True)

            # Dig top-down so headroom and view stay clear and no block falls onto bot head.
            for off in (2, 1):
                dug += self._dig_cell_safely(ctx, (0, foot_y + off, dz), collect=False)

            # Check/patch the step floor BEFORE allowing bot movement onto this step.
            patched += self._ensure_floor_cell(ctx, (0, foot_y - 1, dz))
            dug += self._dig_cell_safely(ctx, (0, foot_y, dz), collect=False)

            # Verify floor again (in case falling block shifted nearby) and advance onto step
            patched += self._ensure_floor_cell(ctx, (0, foot_y - 1, dz))
            self._step_to(ctx, (0.5, float(foot_y), float(dz) + 0.5), tolerance=0.75)

        return {"dug_stair_blocks": dug, "patched_stair_floors": patched}

    def _wf_excavate_platform(self, ctx: WorkflowContext) -> Dict[str, Any]:
        """Carve open 4x3x2 side-expanded workstation platform along dx=1..3, dz=5..8 with dx=0 open tunnel."""
        dug = 0
        patched = 0
        self._step_to(ctx, (0.5, -5.0, 4.5), tolerance=0.75)
        corridor_only = (
            not ctx.extra.get("deploy_workstation")
            and (
                ctx.extra.get("target_y") is not None
                or ctx.extra.get("target_ys")
                or str(ctx.extra.get("mining_mode") or "").lower() == "fishbone"
            )
        )
        dx_limit = 1 if corridor_only else 4

        for dz in range(5, 9):
            for dx in range(0, dx_limit):
                stand_x = 0.5 if dx <= 2 else 1.5
                stand_z = float(dz) + 0.5 if dz == 5 and dx >= 1 else float(dz - 1) + 0.5
                self._step_to(ctx, (stand_x, -5.0, stand_z), tolerance=0.8)
                dug += self._dig_cell_safely(ctx, (dx, -4, dz), collect=False)
                patched += self._ensure_floor_cell(ctx, (dx, -6, dz))
                dug += self._dig_cell_safely(ctx, (dx, -5, dz), collect=False)
                patched += self._ensure_floor_cell(ctx, (dx, -6, dz))
            self._step_to(ctx, (0.5, -5.0, float(dz) + 0.5), tolerance=0.75)

        return {"dug_platform_blocks": dug, "patched_platform_floors": patched}

    def _seal_opening(self, ctx: WorkflowContext, positions: Sequence[Sequence[int]]) -> None:
        """Seal an unsafe cave opening with cobblestone (or scaffold) and verify via get_block."""
        for w in positions:
            wpos = [int(w[0]), int(w[1]), int(w[2])]
            blk = self._cached_read(ctx, wpos)
            bname = str(blk.get("name") or "air")
            if bname not in AIR and bname not in DEFAULT_REPLACEABLE and not is_water_like(blk) and not is_lava_like(blk):
                continue
            scaffold = ctx.select_item(("cobblestone",) + self.SCAFFOLD_PRIORITY)
            if scaffold is None:
                raise WorkflowStepError("missing_seal_block", f"Lack cobblestone to seal cave opening at {wpos}", blocker_type="missing_item", blocker_pos=wpos)
            aim_at(ctx, wpos, relative=False)
            place_block(ctx, scaffold, wpos, relative=False, replaceable=set(DEFAULT_REPLACEABLE) | set(MINE_HAZARD_BLOCKS))
            self._invalidate_cache(ctx, wpos)

    def _sync_quota_progress(self, ctx: WorkflowContext, cave_skill: CaveExploreSkill) -> List[Dict[str, Any]]:
        """Update cumulative harvested quota counts from net inventory gain across resumed cycles."""
        quota_base = ctx.extra.get("quota_base") or {}
        quota_progress = ctx.extra["quota_progress"]
        before_inv = ctx.extra.get("before_inventory") or {}
        _, inv_items = cave_skill._read_inventory(ctx)
        cur_counts = {str(r["name"]): int(r["count"]) for r in inv_items if isinstance(r, dict) and r.get("name")}
        for k in set(cur_counts) | set(before_inv):
            delta = max(0, cur_counts.get(k, 0) - before_inv.get(k, 0))
            total_k = int(quota_base.get(k, 0)) + delta
            if total_k > quota_progress.get(k, 0):
                quota_progress[k] = total_k
        return inv_items

    def _scan_branch_slice_ores(
        self,
        ctx: WorkflowContext,
        cave_skill: CaveExploreSkill,
        next_foot_w: Sequence[int],
        forward_w: Sequence[int],
        *,
        skip_cells: Set[Tuple[int, int, int]],
    ) -> int:
        """Scan the 8 newly exposed faces of one 1x2 slice and mine target veins safely."""
        targets = ctx.extra.get("targets") or {}
        ore_records = ctx.extra["ore_records"]
        dug = 0

        ore_faces: List[Tuple[Tuple[int, int, int], str]] = []
        for face_pos in new_exposed_face_positions(next_foot_w, forward_w):
            fkey = (int(face_pos[0]), int(face_pos[1]), int(face_pos[2]))
            if fkey in skip_cells:
                continue
            bname = str(self._cached_read(ctx, fkey).get("name") or "air")
            if bname in ORE_DROP_MAP:
                ore_faces.append((fkey, bname))
        if not ore_faces:
            return 0

        inv_items = self._sync_quota_progress(ctx, cave_skill)
        quota_progress = ctx.extra["quota_progress"]

        pick_name = best_usable_pickaxe(inv_items)
        tool_tier = pickaxe_tier(pick_name)
        read_fn = lambda p: self._cached_read(ctx, p)
        invalidate = lambda p: self._invalidate_cache(ctx, p)
        max_vein = int(ctx.extra.get("cave_policy", {}).get("max_vein_blocks", 16))
        max_dist = float(ctx.extra.get("cave_policy", {}).get("eye_scan_radius", 4.5))

        for fkey, bname in ore_faces:
            if not targets:
                ore_records.setdefault(fkey, {"pos": list(fkey), "name": bname, "status": "seen"})
                continue
            action, tk, _ = evaluate_ore_block(bname, targets, harvested_counts=quota_progress, best_tool_tier=tool_tier)
            if action != "mine":
                if not ore_cell_hazard_free(fkey, read_fn):
                    action = "skipped_hazard"
                prev = ore_records.get(fkey)
                if prev is None or prev.get("status") not in {"mined", "skipped_hazard"}:
                    ore_records[fkey] = {"pos": list(fkey), "name": bname, "status": action}
                continue
            if not ore_cell_hazard_free(fkey, read_fn):
                ore_records[fkey] = {"pos": list(fkey), "name": bname, "status": "skipped_hazard"}
                continue
            dug += cave_skill._mine_vein(
                ctx,
                next_foot_w,
                list(fkey),
                tk,
                targets,
                dict(quota_progress),
                read_fn,
                invalidate,
                ore_records,
                max_dist=max_dist,
                max_vein=max_vein,
            )
            self._sync_quota_progress(ctx, cave_skill)
        return dug

    def _wf_excavate_branch(self, ctx: WorkflowContext) -> Dict[str, Any]:
        """Carve 1x2 mining branch with 8-face target ore scan, cave probe/link/seal, heading retry, and home checkpoints."""
        headings: Tuple[Tuple[Tuple[int, int, int], Tuple[int, int, int]], ...] = (
            ((0, -5, 8), (0, 0, 1)),   # Primary straight continuation
            ((0, -5, 5), (-1, 0, 0)),  # 90° left off tunnel wall at dz=5
            ((0, -5, 8), (-1, 0, 0)),  # 90° left shifted 3 blocks along tunnel (dz=8)
            ((2, -5, 8), (0, 0, 1)),   # Parallel forward shifted +2 clear of workstation chest
        )
        branch_len = int(ctx.extra.get("branch_length", 4))
        handle_caves = bool(ctx.extra.get("handle_caves", True))
        policy = dict(ctx.extra.get("cave_policy") or CAVE_LIMITS)
        max_unsafe = int(policy.get("max_unsafe_retries", 3))
        targets = ctx.extra.get("targets") or {}
        before_inv = ctx.extra.get("before_inventory") or {}

        cave_skill = CaveExploreSkill(self.client, memory=self.memory)
        read_fn = lambda p: self._cached_read(ctx, p)
        invalidate = lambda p: self._invalidate_cache(ctx, p)

        dug = 0
        patched = 0
        heading_idx = int(ctx.extra.get("heading_index", 0)) % len(headings)
        start_step = int(ctx.extra.get("steps_completed", 0)) + 1
        unsafe_count = int(ctx.extra.get("unsafe_count", 0))
        opened_caves: Set[Tuple[int, int, int]] = set()

        while unsafe_count < max_unsafe:
            j_rel, fwd_rel = headings[heading_idx % len(headings)]
            j_world = ctx.world_pos(j_rel, integer=True)
            fwd_world = ctx.world_vec(fwd_rel)
            vx, vz = fwd_world[0], fwd_world[2]
            breadcrumbs: List[List[int]] = [list(j_world)]
            stand_cell(ctx, j_world, tolerance=0.85)
            consecutive_floor_patches = 0
            completed_this_heading = start_step >= branch_len and start_step > 1

            for step in range(start_step, branch_len + 1):
                prev_foot_w = [j_world[0] + (step - 1) * vx, j_world[1], j_world[2] + (step - 1) * vz]
                next_foot_w = [j_world[0] + step * vx, j_world[1], j_world[2] + step * vz]
                next_head_w = [next_foot_w[0], next_foot_w[1] + 1, next_foot_w[2]]
                next_floor_w = [next_foot_w[0], next_foot_w[1] - 1, next_foot_w[2]]

                # Walk smoothly along already-carved tiles up to prev_foot_w if resuming
                if tuple(prev_foot_w) != tuple(breadcrumbs[-1]):
                    stand_cell(ctx, prev_foot_w, tolerance=0.85)
                    breadcrumbs.append(list(prev_foot_w))

                # Inspect head/foot/floor before carving step
                head_blk = self._cached_read(ctx, next_head_w)
                foot_blk = self._cached_read(ctx, next_foot_w)
                floor_blk = self._cached_read(ctx, next_floor_w)
                head_name = str(head_blk.get("name") or "unknown")
                foot_name = str(foot_blk.get("name") or "unknown")
                floor_name = str(floor_blk.get("name") or "unknown")

                # Check direct liquid/spawner ahead or open cavity breach when handle_caves=True
                direct_hazard = (
                    is_lava_like(head_blk) or is_lava_like(foot_blk) or is_lava_like(floor_blk)
                    or is_water_like(head_blk) or is_water_like(foot_blk) or is_water_like(floor_blk)
                    or head_name == "spawner" or foot_name == "spawner"
                )
                cavity_air = None
                if not direct_hazard:
                    if is_passable(head_blk, allow_cobweb=True):
                        cavity_air = next_head_w
                    elif is_passable(foot_blk, allow_cobweb=True):
                        cavity_air = next_foot_w
                    elif (not is_solid_floor(floor_blk)) and consecutive_floor_patches >= 1:
                        cavity_air = next_floor_w

                if handle_caves and (direct_hazard or (cavity_air is not None and tuple(cavity_air) not in opened_caves)):
                    probe_seed = next_head_w if is_passable(head_blk, allow_cobweb=True) or direct_hazard else (cavity_air or next_foot_w)
                    back_guard = (prev_foot_w, [prev_foot_w[0], prev_foot_w[1] + 1, prev_foot_w[2]])
                    report = probe_cavity(
                        probe_seed,
                        prev_foot_w,
                        read_fn,
                        limits=policy,
                        block_cache=ctx.extra["block_cache"],
                        blocked_backtracks=back_guard,
                    )
                    if direct_hazard:
                        if is_lava_like(head_blk) or is_lava_like(foot_blk) or is_lava_like(floor_blk):
                            report["lava_found"] = True
                        elif head_name == "spawner" or foot_name == "spawner":
                            report["spawner_found"] = True
                        else:
                            report["water_count"] = max(int(report.get("water_count", 0)), int(policy.get("max_water_blocks", 3)))
                    _, inv_items = cave_skill._read_inventory(ctx)
                    torch_cnt = sum(int(r.get("count", 0)) for r in inv_items if r.get("name") == "torch")
                    cls = classify_cavity(report, torch_count=torch_cnt, limits=policy)

                    if cls["decision"] == "small_bubble" and consecutive_floor_patches < 1:
                        # Small air pocket -> patch floor at most 1 consecutive block and proceed
                        pass
                    elif cls["decision"] == "enter":
                        opened_caves.add(tuple(probe_seed))
                        for vc in report.get("visited_cells") or ():
                            opened_caves.add((int(vc[0]), int(vc[1]), int(vc[2])))
                        cave_res = cave_skill.explore_cave(
                            entry_pos=prev_foot_w,
                            origin_air=probe_seed,
                            targets=ctx.extra.get("raw_targets") or None,
                            cave_policy=policy,
                            home_thresholds=ctx.extra.get("home_thresholds"),
                            harvested_totals=ctx.extra["quota_progress"],
                            existing_report=report,
                            existing_classification=cls,
                            block_cache=ctx.extra["block_cache"],
                            world=ctx.extra.get("world_3d"),
                            blocked_backtracks=breadcrumbs,
                            guard=ctx.guard,
                        )
                        _raise_if_unknown(cave_res)
                        ctx.extra["caves"].extend(cave_res.data.get("caves") or [])
                        ctx.extra["torches_placed"] = int(ctx.extra.get("torches_placed", 0)) + int(cave_res.data.get("torches_placed", 0))
                        for rec in cave_res.data.get("ores") or []:
                            rk = (int(rec["pos"][0]), int(rec["pos"][1]), int(rec["pos"][2]))
                            ctx.extra["ore_records"][rk] = rec
                        if cave_res.data.get("junction_unsafe"):
                            # Treat junction breach as unsafe, seal & retreat to platform
                            cls = {"decision": "unsafe", "kind": "tall_cave", "reason": "Junction became unsafe during exploration"}
                        elif cave_res.data.get("stop_reason") == "return_home":
                            for bpos in reversed(breadcrumbs[:-1]):
                                stand_cell(ctx, bpos, tolerance=0.9)
                            ctx.extra["stop_reason"] = "return_home"
                            ctx.extra["home_reason"] = cave_res.data.get("home_reason")
                            return {"dug_branch_blocks": dug, "patched_branch_floors": patched, "stop_reason": "return_home"}
                        elif cave_res.data.get("stop_reason") == "safety_interrupted":
                            for bpos in reversed(breadcrumbs[:-1]):
                                stand_cell(ctx, bpos, tolerance=0.9)
                            raise WorkflowStepError("safety_interrupted", cave_res.message, blocker_type="safety_interrupted")

                    if cls["decision"] == "unsafe":
                        unsafe_count += 1
                        ctx.extra["unsafe_count"] = unsafe_count
                        ctx.extra["branch_redirected_or_sealed"] = True
                        cave_rec = {
                            "pos": list(probe_seed),
                            "decision": "unsafe",
                            "kind": cls["kind"],
                            "height": int(report.get("height", 0)),
                            "drop": int(report.get("drop", 0)),
                        }
                        ctx.extra["caves"].append(cave_rec)
                        ctx.extra["sealed_caves"].append({
                            **cave_rec,
                            "water": int(report.get("water_count", 0)),
                        })
                        self.memory.append_episode(
                            event_type="cave_sealed",
                            summary=f"Sealed unsafe {cls['kind']} at {probe_seed}",
                            details=cave_rec,
                        )
                        self._seal_opening(ctx, (next_foot_w, next_head_w))
                        for bpos in reversed(breadcrumbs[:-1]):
                            stand_cell(ctx, bpos, tolerance=0.9)
                        self._step_to(ctx, (1.5, -5.0, 6.5), tolerance=0.85)
                        if unsafe_count >= max_unsafe:
                            ctx.extra["stop_reason"] = "cave_abandoned"
                            raise WorkflowStepError(
                                "cave_abandoned",
                                f"Abandoned branch mining after {unsafe_count} unsafe cave openings.",
                                blocker_type="safety_interrupted",
                                blocker_pos=probe_seed,
                                suggested_recovery="relocate_mine_shaft",
                            )
                        heading_idx = (heading_idx + 1) % len(headings)
                        ctx.extra["heading_index"] = heading_idx
                        start_step = 1
                        ctx.extra["steps_completed"] = 0
                        break  # Restart inner loop on alternate branch heading

                # Record ore if tunnel centerline block itself is an ore
                for tc in (next_head_w, next_foot_w):
                    tb = str(self._cached_read(ctx, tc).get("name") or "air")
                    if tb in ORE_DROP_MAP:
                        tk_key = (tc[0], tc[1], tc[2])
                        ctx.extra["ore_records"][tk_key] = {"pos": list(tc), "name": tb, "status": "mined"}
                        drop_item = ORE_DROP_MAP[tb]
                        ctx.extra["quota_progress"][drop_item] = ctx.extra["quota_progress"].get(drop_item, 0) + 1

                # Carve top head cell first (collect=False), validate floor (max 1 consecutive patch), carve foot cell
                # Skip passable non-air blocks (wall_torch, torch, rails) so CaveExplore lights stay intact.
                if not is_passable(self._cached_read(ctx, next_head_w), allow_cobweb=False):
                    dug += self._dig_cell_safely(ctx, next_head_w, collect=False, relative=False)

                floor_now = str(self._cached_read(ctx, next_floor_w).get("name") or "unknown")
                if floor_now in DEFAULT_REPLACEABLE:
                    if consecutive_floor_patches >= 1:
                        # Second consecutive missing floor -> treat as deep drop hazard & seal
                        unsafe_count += 1
                        ctx.extra["unsafe_count"] = unsafe_count
                        ctx.extra["branch_redirected_or_sealed"] = True
                        self._seal_opening(ctx, (next_foot_w, next_head_w))
                        for bpos in reversed(breadcrumbs[:-1]):
                            stand_cell(ctx, bpos, tolerance=0.9)
                        self._step_to(ctx, (1.5, -5.0, 6.5), tolerance=0.85)
                        if unsafe_count >= max_unsafe:
                            ctx.extra["stop_reason"] = "cave_abandoned"
                            raise WorkflowStepError("cave_abandoned", "Repeated unsupported branch drop.", blocker_type="safety_interrupted")
                        heading_idx = (heading_idx + 1) % len(headings)
                        ctx.extra["heading_index"] = heading_idx
                        start_step = 1
                        ctx.extra["steps_completed"] = 0
                        break
                    patched += self._ensure_floor_cell(ctx, next_floor_w, relative=False)
                    consecutive_floor_patches += 1
                else:
                    patched += self._ensure_floor_cell(ctx, next_floor_w, relative=False)
                    consecutive_floor_patches = 0

                if not is_passable(self._cached_read(ctx, next_foot_w), allow_cobweb=False):
                    dug += self._dig_cell_safely(ctx, next_foot_w, collect=False, relative=False)
                patched += self._ensure_floor_cell(ctx, next_floor_w, relative=False)

                # Scan the 8 newly exposed slice faces against mining targets before moving on
                corridor_set = {(prev_foot_w[0], prev_foot_w[1], prev_foot_w[2]), (next_foot_w[0], next_foot_w[1], next_foot_w[2]), (next_head_w[0], next_head_w[1], next_head_w[2])}
                dug += self._scan_branch_slice_ores(ctx, cave_skill, next_foot_w, fwd_world, skip_cells=corridor_set)
                patched += self._ensure_floor_cell(ctx, next_floor_w, relative=False)

                # Advance bot cleanly onto next_foot_w (auto-picks up walkable floor drops)
                stand_cell(ctx, next_foot_w, tolerance=0.85)
                breadcrumbs.append(list(next_foot_w))
                ctx.extra["steps_completed"] = step

                # Checkpoint evaluation every 4 branch blocks
                if step % 4 == 0:
                    cur_status = ctx.guard.check_status(require_safe=True)
                    _, cur_items = cave_skill._read_inventory(ctx)
                    home_eval = evaluate_home_trigger(
                        cur_status,
                        cur_items,
                        remaining_seconds=ctx.guard.remaining_timeout(999.0),
                        thresholds=ctx.extra.get("home_thresholds"),
                    )
                    if home_eval["trigger"]:
                        for bpos in reversed(breadcrumbs[:-1]):
                            stand_cell(ctx, bpos, tolerance=0.9)
                        ctx.extra["stop_reason"] = "return_home"
                        ctx.extra["home_reason"] = home_eval["home_reason"]
                        return {"dug_branch_blocks": dug, "patched_branch_floors": patched, "stop_reason": "return_home"}
                    if home_eval.get("wrap_up"):
                        break
            else:
                completed_this_heading = True

            if completed_this_heading:
                break

        return {"dug_branch_blocks": dug, "patched_branch_floors": patched, "heading_index": heading_idx}

    def _all_targets_met(self, ctx: WorkflowContext, cave_skill: CaveExploreSkill) -> bool:
        targets = ctx.extra.get("targets") or {}
        if not targets:
            return False
        self._sync_quota_progress(ctx, cave_skill)
        quota_progress = ctx.extra["quota_progress"]
        for spec in targets.values():
            drop = spec["drop_item"]
            # Count raw ore AND smelted ingots across resumed cycles
            have = quota_progress.get(drop, 0)
            if drop == "raw_iron":
                have += quota_progress.get("iron_ingot", 0)
            elif drop == "raw_gold":
                have += quota_progress.get("gold_ingot", 0)
            if have < int(spec["count"]):
                return False
        return True

    @staticmethod
    def _compress_trail(trail: Sequence[Sequence[int]], stride: int = 4) -> List[List[int]]:
        """Downsample a breadcrumb trail while preserving every turn/elevation-change corner."""
        if not trail:
            return []
        if len(trail) <= 2:
            return [list(p) for p in trail]
        out: List[List[int]] = [list(trail[0])]
        step_n = max(1, int(stride))
        for i in range(1, len(trail) - 1):
            prev_d = (trail[i][0] - trail[i - 1][0], trail[i][1] - trail[i - 1][1], trail[i][2] - trail[i - 1][2])
            next_d = (trail[i + 1][0] - trail[i][0], trail[i + 1][1] - trail[i][1], trail[i + 1][2] - trail[i][2])
            if prev_d != next_d or (i % step_n == 0):
                pt = list(trail[i])
                if pt != out[-1]:
                    out.append(pt)
        last = list(trail[-1])
        if out[-1] != last:
            out.append(last)
        return out

    def _retreat_downsampled(
        self,
        ctx: WorkflowContext,
        trail: Sequence[Sequence[int]],
        step_stride: int = 2,
        *,
        reverse: bool = True,
    ) -> None:
        if len(trail) <= 1:
            return
        seq = list(reversed(trail[:-1])) if reverse else list(trail[1:])
        stride = max(1, int(step_stride))
        indices = list(range(0, len(seq), stride))
        if indices[-1] != len(seq) - 1:
            indices.append(len(seq) - 1)
        goal = [int(seq[-1][0]), int(seq[-1][1]), int(seq[-1][2])]
        prev_idx = -1
        consec_stalls = 0
        for idx in indices:
            if ctx.guard.remaining_timeout(999.0) < 1.5:
                break
            try:
                stand_cell(ctx, seq[idx], tolerance=1.15)
                consec_stalls = 0
            except WorkflowStepError as exc:
                if exc.code not in {"path_not_found", "stalled", "timeout", "unreachable_stance"}:
                    raise
                step_ok = False
                for sub_idx in range(prev_idx + 1, idx + 1):
                    if ctx.guard.remaining_timeout(999.0) < 1.5:
                        break
                    try:
                        stand_cell(ctx, seq[sub_idx], tolerance=1.35)
                        step_ok = True
                        consec_stalls = 0
                    except WorkflowStepError as sub_exc:
                        if sub_exc.code not in {"path_not_found", "stalled", "timeout", "unreachable_stance"}:
                            raise
                        if sub_idx == len(seq) - 1:
                            try:
                                stand_cell(ctx, goal, tolerance=1.8)
                                step_ok = True
                            except WorkflowStepError:
                                pass
                        break
                if not step_ok:
                    consec_stalls += 1
                    if consec_stalls >= 2:
                        break
            prev_idx = idx

        # Verify actual arrival near trail endpoint; fall back to 3D A* and emergency surface escape if trapped
        live = WorldNavigator.sync_live_pos(ctx)
        if live is not None and (
            abs(live[0] - goal[0]) > 3 or abs(live[1] - goal[1]) > 2 or abs(live[2] - goal[2]) > 3
        ):
            world_3d = ctx.extra.get("world_3d") if isinstance(ctx.extra, dict) else None
            if world_3d is None:
                world_3d = VoxelWorld3D(block_cache=ctx.extra.get("block_cache") if isinstance(ctx.extra, dict) else None)
            read_fn = lambda p: self._cached_read(ctx, p)
            try:
                path_back = Pathfinder3D.find_path(
                    world_3d,
                    live,
                    goal,
                    read_fn,
                    goal_tolerance=1,
                    allow_pillar=True,
                    allow_dig=True,
                    allow_bridge=True,
                    allow_drop=True,
                    max_nodes=2400,
                )
                if path_back:
                    WorldNavigator.follow_path(
                        ctx,
                        world_3d,
                        live,
                        path_back,
                        read_fn,
                        allow_micro_dig=True,
                        allow_dig_headroom_on_pillar=True,
                        tolerance=1.0,
                    )
            except WorkflowStepError:
                pass
            live_after = WorldNavigator.sync_live_pos(ctx)
            if reverse and live_after is not None and live_after[1] < goal[1] - 4:
                try:
                    escape_to_surface(
                        ctx,
                        max_ascend=160,
                        blocks=SCAFFOLD_BLOCKS,
                        invalidate=lambda p: self._invalidate_cache(ctx, p),
                    )
                except WorkflowStepError:
                    pass

    def _carve_1x2_step_or_handle_cave(
        self,
        ctx: WorkflowContext,
        cave_skill: CaveExploreSkill,
        prev_foot_w: List[int],
        fwd_vec: Tuple[int, int, int],
        opened_caves: Set[Tuple[int, int, int]],
        consecutive_floor_patches: int,
        breadcrumbs: List[List[int]],
        *,
        stand_after: bool = True,
    ) -> Tuple[str, int, int, int]:
        """Carve one 1x2 horizontal step (or probe/link/seal cave). Returns (outcome, dug, patched, new_consec_patch).

        outcome in {'ok', 'sealed_unsafe', 'return_home', 'quota_met', 'wrap_up'}.
        """
        policy = dict(ctx.extra.get("cave_policy") or CAVE_LIMITS)
        handle_caves = bool(ctx.extra.get("handle_caves", True))
        vx, vz = fwd_vec[0], fwd_vec[2]
        next_foot_w = [prev_foot_w[0] + vx, prev_foot_w[1], prev_foot_w[2] + vz]
        next_head_w = [next_foot_w[0], next_foot_w[1] + 1, next_foot_w[2]]
        next_floor_w = [next_foot_w[0], next_foot_w[1] - 1, next_foot_w[2]]
        read_fn = lambda p: self._cached_read(ctx, p)
        dug = 0
        patched = 0

        head_blk = self._cached_read(ctx, next_head_w)
        foot_blk = self._cached_read(ctx, next_foot_w)
        floor_blk = self._cached_read(ctx, next_floor_w)
        head_name = str(head_blk.get("name") or "unknown")
        foot_name = str(foot_blk.get("name") or "unknown")
        if "bedrock" in {head_name, foot_name, str(floor_blk.get("name") or "")}:
            return ("sealed_unsafe", dug, patched, consecutive_floor_patches)

        direct_hazard = (
            is_lava_like(head_blk) or is_lava_like(foot_blk) or is_lava_like(floor_blk)
            or is_water_like(head_blk) or is_water_like(foot_blk) or is_water_like(floor_blk)
            or head_name == "spawner" or foot_name == "spawner"
        )
        cavity_air = None
        if not direct_hazard:
            if is_passable(head_blk, allow_cobweb=True):
                cavity_air = next_head_w
            elif is_passable(foot_blk, allow_cobweb=True):
                cavity_air = next_foot_w
            elif (not is_solid_floor(floor_blk)) and consecutive_floor_patches >= 1:
                cavity_air = next_floor_w

        if handle_caves and (direct_hazard or (cavity_air is not None and tuple(cavity_air) not in opened_caves)):
            probe_seed = next_head_w if is_passable(head_blk, allow_cobweb=True) or direct_hazard else (cavity_air or next_foot_w)
            back_guard = (prev_foot_w, [prev_foot_w[0], prev_foot_w[1] + 1, prev_foot_w[2]])
            report = probe_cavity(
                probe_seed,
                prev_foot_w,
                read_fn,
                limits=policy,
                block_cache=ctx.extra["block_cache"],
                blocked_backtracks=back_guard,
            )
            if direct_hazard:
                if is_lava_like(head_blk) or is_lava_like(foot_blk) or is_lava_like(floor_blk):
                    report["lava_found"] = True
                elif head_name == "spawner" or foot_name == "spawner":
                    report["spawner_found"] = True
                else:
                    report["water_count"] = max(int(report.get("water_count", 0)), int(policy.get("max_water_blocks", 3)))
            _, inv_items = cave_skill._read_inventory(ctx)
            torch_cnt = sum(int(r.get("count", 0)) for r in inv_items if r.get("name") == "torch")
            cls = classify_cavity(report, torch_count=torch_cnt, limits=policy)
            allow_subgraph = bool(ctx.extra.get("prefer_caves_first") or policy.get("allow_tall_cave_subgraph"))

            if cls["decision"] == "enter" or (
                allow_subgraph
                and cls.get("kind") in {"tall_cave", "deep_drop", "lava", "water_cave"}
                and not report.get("spawner_found")
            ):
                opened_caves.add(tuple(probe_seed))
                for vc in report.get("visited_cells") or ():
                    opened_caves.add((int(vc[0]), int(vc[1]), int(vc[2])))
                cave_res = cave_skill.explore_cave(
                    entry_pos=prev_foot_w,
                    origin_air=probe_seed,
                    targets=ctx.extra.get("raw_targets") or None,
                    cave_policy=policy,
                    home_thresholds=ctx.extra.get("home_thresholds"),
                    harvested_totals=dict(ctx.extra["quota_progress"]),
                    existing_report=report,
                    existing_classification=None if allow_subgraph and cls["decision"] != "enter" else cls,
                    block_cache=ctx.extra["block_cache"],
                    world=ctx.extra.get("world_3d"),
                    blocked_backtracks=breadcrumbs[-3:],
                    guard=ctx.guard,
                    allow_damage=ctx.guard.allow_damage,
                    prefer_flood_planner=True,
                    allow_tall_cave_subgraph=allow_subgraph,
                    allow_pillar=allow_subgraph,
                    allow_micro_dig=allow_subgraph,
                    allow_bridge=allow_subgraph,
                    allow_drop=allow_subgraph,
                )
                _raise_if_unknown(cave_res)
                self._sync_quota_progress(ctx, cave_skill)
                if isinstance(cave_res.data.get("classification"), dict):
                    cls = dict(cave_res.data["classification"])
                ctx.extra["caves"].extend(cave_res.data.get("caves") or [])
                ctx.extra["torches_placed"] = int(ctx.extra.get("torches_placed", 0)) + int(cave_res.data.get("torches_placed", 0))
                for rec in cave_res.data.get("ores") or []:
                    rk = (int(rec["pos"][0]), int(rec["pos"][1]), int(rec["pos"][2]))
                    ctx.extra["ore_records"][rk] = rec
                if cave_res.data.get("junction_unsafe"):
                    cls = {"decision": "unsafe", "kind": "tall_cave", "reason": "Junction became unsafe"}
                elif cave_res.data.get("stop_reason") == "return_home":
                    ctx.extra["home_reason"] = cave_res.data.get("home_reason")
                    return ("return_home", dug, patched, consecutive_floor_patches)
                elif self._all_targets_met(ctx, cave_skill):
                    return ("quota_met", dug, patched, consecutive_floor_patches)

            if cls["decision"] == "unsafe":
                ctx.extra["branch_redirected_or_sealed"] = True
                cave_rec = {
                    "pos": list(probe_seed),
                    "decision": "unsafe",
                    "kind": cls["kind"],
                    "height": int(report.get("height", 0)),
                    "drop": int(report.get("drop", 0)),
                }
                ctx.extra["caves"].append(cave_rec)
                ctx.extra["sealed_caves"].append({**cave_rec, "water": int(report.get("water_count", 0))})
                try:
                    self._seal_opening(ctx, (next_foot_w, next_head_w))
                except WorkflowStepError:
                    pass
                return ("sealed_unsafe", dug, patched, consecutive_floor_patches)

        for tc in (next_head_w, next_foot_w):
            tb = str(self._cached_read(ctx, tc).get("name") or "air")
            if tb in ORE_DROP_MAP:
                ctx.extra["ore_records"][(tc[0], tc[1], tc[2])] = {"pos": list(tc), "name": tb, "status": "mined"}
                di = ORE_DROP_MAP[tb]
                ctx.extra["quota_progress"][di] = ctx.extra["quota_progress"].get(di, 0) + 1

        try:
            if not is_passable(self._cached_read(ctx, next_head_w), allow_cobweb=False):
                dug += self._dig_cell_safely(ctx, next_head_w, collect=False, relative=False)

            floor_now = str(self._cached_read(ctx, next_floor_w).get("name") or "unknown")
            new_consec = consecutive_floor_patches
            if floor_now in DEFAULT_REPLACEABLE:
                if new_consec >= 1:
                    try:
                        self._seal_opening(ctx, (next_foot_w, next_head_w))
                    except WorkflowStepError:
                        pass
                    return ("sealed_unsafe", dug, patched, new_consec)
                patched += self._ensure_floor_cell(ctx, next_floor_w, relative=False)
                new_consec += 1
            else:
                patched += self._ensure_floor_cell(ctx, next_floor_w, relative=False)
                new_consec = 0

            if not is_passable(self._cached_read(ctx, next_foot_w), allow_cobweb=False):
                dug += self._dig_cell_safely(ctx, next_foot_w, collect=False, relative=False)
            patched += self._ensure_floor_cell(ctx, next_floor_w, relative=False)

            corridor_set = {(prev_foot_w[0], prev_foot_w[1], prev_foot_w[2]), (next_foot_w[0], next_foot_w[1], next_foot_w[2]), (next_head_w[0], next_head_w[1], next_head_w[2])}
            dug += self._scan_branch_slice_ores(ctx, cave_skill, next_foot_w, fwd_vec, skip_cells=corridor_set)
            patched += self._ensure_floor_cell(ctx, next_floor_w, relative=False)
        except WorkflowStepError as exc:
            if exc.code not in {"hazard_adjacent", "hazard_detected", "hazard_floor", "bedrock_blocked", "unsafe_floor", "unexpected_block"}:
                raise
            blk_kind = "lava" if "lava" in str(exc.blocker_block or exc) else ("water_cave" if "water" in str(exc.blocker_block or exc) else exc.code)
            rec = {"pos": list(exc.blocker_pos or next_foot_w), "decision": "unsafe", "kind": blk_kind, "height": 2, "drop": 0}
            ctx.extra["caves"].append(rec)
            ctx.extra["sealed_caves"].append(rec)
            try:
                self._seal_opening(ctx, (next_foot_w, next_head_w))
            except WorkflowStepError:
                pass
            return ("sealed_unsafe", dug, patched, consecutive_floor_patches)

        if stand_after:
            stand_cell(ctx, next_foot_w, tolerance=0.85)
        if self._all_targets_met(ctx, cave_skill):
            if not stand_after:
                stand_cell(ctx, next_foot_w, tolerance=0.85)
            return ("quota_met", dug, patched, new_consec)
        return ("ok", dug, patched, new_consec)

    def _wf_deep_fishbone(self, ctx: WorkflowContext) -> Dict[str, Any]:
        """Descend safely across elevation bands (target_ys / target_y) and run fishbone branches with guaranteed surface/hub return."""
        if ctx.extra.get("deep_retreat_done"):
            return {
                "dug_branch_blocks": 0,
                "patched_branch_floors": 0,
                "stop_reason": ctx.extra.get("stop_reason") or "return_home",
            }
        cave_skill = CaveExploreSkill(self.client, memory=self.memory)
        target_y = ctx.extra.get("target_y")
        target_ys_raw = ctx.extra.get("target_ys")
        if isinstance(target_ys_raw, (list, tuple)) and target_ys_raw:
            band_schedule: List[Optional[int]] = [int(v) for v in target_ys_raw]
        elif target_y is not None:
            band_schedule = [int(target_y)]
        else:
            band_schedule = [None]

        rib_len = int(ctx.extra.get("rib_length", 12))
        rib_spacing = int(ctx.extra.get("rib_spacing", 4))
        max_trunk = int(ctx.extra.get("max_trunk_steps", 40))

        cur_foot = list(ctx.extra.get("deep_pos") or ctx.world_pos((0, -5, 8), integer=True))
        raw_fwd = ctx.extra.get("deep_fwd") or ctx.world_vec((0, 0, 1))
        fwd = (int(raw_fwd[0]), 0, int(raw_fwd[2]))
        opened_caves: Set[Tuple[int, int, int]] = set()
        saved_trail = [list(p) for p in (ctx.extra.get("deep_trail") or []) if isinstance(p, list) and len(p) == 3]
        trail: List[List[int]] = saved_trail if saved_trail else [ctx.world_pos((0, -5, 8), integer=True)]
        if saved_trail and len(saved_trail) > 1:
            self._retreat_downsampled(ctx, saved_trail, step_stride=1, reverse=False)
        elif tuple(cur_foot) != tuple(trail[0]):
            stand_cell(ctx, cur_foot, tolerance=1.0)
            trail.append(list(cur_foot))
        else:
            stand_cell(ctx, cur_foot, tolerance=0.85)

        dug = 0
        patched = 0
        consec_floor = 0
        timed_out_pause = False
        targets_map = ctx.extra.get("targets") or {}

        for band_idx, band_y in enumerate(band_schedule):
            has_more_bands = band_idx < len(band_schedule) - 1

            # Phase 1: Safe descending staircase down to band_y along continuous trail
            stair_timed_out = False
            if band_y is not None and cur_foot[1] > int(band_y):
                turns_left = 24
                step_counter = 0
                visited_turns: Set[Tuple[Tuple[int, int, int], Tuple[int, int, int]]] = set()
                while cur_foot[1] > int(band_y) and turns_left > 0:
                    stair_reserve = min(95.0, max(25.0, 0.45 * len(trail)))
                    if ctx.guard.remaining_timeout(999.0) < stair_reserve:
                        ctx.extra["deep_pos"] = list(cur_foot)
                        ctx.extra["deep_fwd"] = list(fwd)
                        stair_timed_out = True
                        break
                    nxt = [cur_foot[0] + fwd[0], cur_foot[1] - 1, cur_foot[2] + fwd[2]]
                    try:
                        for off in (2, 1, 0):
                            dug += self._dig_cell_safely(ctx, (nxt[0], nxt[1] + off, nxt[2]), collect=False, relative=False)
                        floor_w = (nxt[0], nxt[1] - 1, nxt[2])
                        floor_name = str(self._cached_read(ctx, floor_w).get("name") or "unknown")
                        if floor_name in DEFAULT_REPLACEABLE:
                            if consec_floor >= 2:
                                raise WorkflowStepError("unsafe_floor", "Repeated gap under stair", blocker_pos=list(floor_w))
                            consec_floor += 1
                        else:
                            consec_floor = 0
                        p_step = self._ensure_floor_cell(ctx, floor_w, relative=False)
                        patched += p_step
                        ore_step = self._scan_branch_slice_ores(
                            ctx,
                            cave_skill,
                            nxt,
                            fwd,
                            skip_cells={(cur_foot[0], cur_foot[1], cur_foot[2]), (nxt[0], nxt[1], nxt[2]), (nxt[0], nxt[1] + 1, nxt[2])},
                        )
                        dug += ore_step
                        if (step_counter % 2 == 1) or p_step > 0 or ore_step > 0 or nxt[1] <= int(band_y):
                            stand_cell(ctx, nxt, tolerance=0.95)
                        cur_foot = list(nxt)
                        trail.append(list(cur_foot))
                        step_counter += 1
                        if step_counter % 8 == 0:
                            if CaveExploreSkill._place_near_torch(
                                ctx,
                                trail[-2],
                                lambda p: self._cached_read(ctx, p),
                                lambda p: self._invalidate_cache(ctx, p),
                                forbid_cell=cur_foot,
                            ):
                                ctx.extra["torches_placed"] = int(ctx.extra.get("torches_placed", 0)) + 1
                    except WorkflowStepError as exc:
                        if exc.code in {"damage_taken", "unsafe_state", "life_changed", "missing_pickaxe"}:
                            ctx.extra["deep_retreat_done"] = True
                            self._retreat_downsampled(ctx, trail)
                            raise
                        try:
                            self._seal_opening(ctx, ([nxt[0], cur_foot[1], nxt[2]], [nxt[0], cur_foot[1] + 1, nxt[2]]))
                        except WorkflowStepError:
                            pass
                        visited_turns.add((tuple(cur_foot), tuple(fwd)))
                        turns_left -= 1
                        recent_cells = {(int(p[0]), int(p[2])) for p in trail[-10:-1]}
                        turn_cands = [
                            (-fwd[2], 0, fwd[0]),
                            (fwd[2], 0, -fwd[0]),
                            tuple(fwd),
                            (-fwd[0], 0, -fwd[2]),
                        ]
                        ordered = [
                            c for c in turn_cands
                            if (tuple(cur_foot), c) not in visited_turns
                            and (cur_foot[0] + c[0], cur_foot[2] + c[2]) not in recent_cells
                        ]
                        if not ordered:
                            ordered = [
                                c for c in turn_cands
                                if (cur_foot[0] + c[0], cur_foot[2] + c[2]) not in recent_cells
                            ] or [turn_cands[0]]

                        def _rock_heading_score(cand_dir: Tuple[int, int, int]) -> int:
                            cx = cur_foot[0] + cand_dir[0]
                            cy = cur_foot[1]
                            cz = cur_foot[2] + cand_dir[2]
                            s = 0
                            for dy in (0, -1, 1):
                                bname = str(self._cached_read(ctx, (cx, cy + dy, cz)).get("name") or "unknown")
                                if bname in MINE_HAZARD_BLOCKS:
                                    s -= 10
                                elif bname not in DEFAULT_REPLACEABLE and bname != "unknown":
                                    s += 2 if dy <= 0 else 1
                            return s

                        ordered.sort(key=lambda c: -_rock_heading_score(c))
                        moved_any = False
                        for cand in ordered:
                            visited_turns.add((tuple(cur_foot), cand))
                            moved_pad = 0
                            # Jog flat (dy=0) across underwater rivers / cave seams until floor+subfloor ahead is hazard-free
                            for _pad in range(8):
                                p_foot = [cur_foot[0] + cand[0], cur_foot[1], cur_foot[2] + cand[2]]
                                p_head2 = [p_foot[0], p_foot[1] + 2, p_foot[2]]
                                p_head = [p_foot[0], p_foot[1] + 1, p_foot[2]]
                                p_floor = [p_foot[0], p_foot[1] - 1, p_foot[2]]
                                try:
                                    if _pad == 0:
                                        dug += self._dig_cell_safely(ctx, tuple(p_head2), collect=False, relative=False)
                                    dug += self._dig_cell_safely(ctx, tuple(p_head), collect=False, relative=False)
                                    p_patch = self._ensure_floor_cell(ctx, p_floor, relative=False)
                                    patched += p_patch
                                    dug += self._dig_cell_safely(ctx, tuple(p_foot), collect=False, relative=False)
                                    if _pad % 2 == 1 or p_patch > 0 or _pad == 7:
                                        stand_cell(ctx, p_foot, tolerance=0.95)
                                    cur_foot = list(p_foot)
                                    trail.append(list(cur_foot))
                                    moved_pad += 1
                                    if _pad >= 1:
                                        df_x, df_y, df_z = cur_foot[0] + cand[0], cur_foot[1] - 1, cur_foot[2] + cand[2]
                                        df_name = str(self._cached_read(ctx, (df_x, df_y - 1, df_z)).get("name") or "unknown")
                                        sub_name = str(self._cached_read(ctx, (df_x, df_y - 2, df_z)).get("name") or "unknown")
                                        if (
                                            df_name not in (DEFAULT_REPLACEABLE | MINE_HAZARD_BLOCKS)
                                            and sub_name not in MINE_HAZARD_BLOCKS
                                        ):
                                            break
                                except WorkflowStepError:
                                    break
                            if moved_pad > 0:
                                fwd = cand
                                moved_any = True
                                break
                        if not moved_any:
                            fwd = ordered[0]
                        stand_cell(ctx, cur_foot, tolerance=0.95)
                        continue

                    if self._all_targets_met(ctx, cave_skill):
                        ctx.extra["deep_retreat_done"] = True
                        ctx.extra["stop_reason"] = "target_reached"
                        self._retreat_downsampled(ctx, trail)
                        return {"dug_branch_blocks": dug, "patched_branch_floors": patched, "stop_reason": "target_reached"}

                    if step_counter % 8 == 0:
                        cur_status = ctx.guard.check_status(require_safe=True)
                        cur_counts, cur_items = cave_skill._read_inventory(ctx)
                        home_eval = evaluate_home_trigger(
                            cur_status,
                            cur_items,
                            remaining_seconds=ctx.guard.remaining_timeout(999.0),
                            thresholds=ctx.extra.get("home_thresholds"),
                        )
                        if home_eval["home_reason"] == "inventory_full" and callable(getattr(ctx.guard.client, "drop", None)):
                            try:
                                ctx.guard.write(
                                    ctx.guard.client.look_at,
                                    float(cur_foot[0]) + 0.5 - float(fwd[0]) * 2.0,
                                    float(cur_foot[1]) + 1.5,
                                    float(cur_foot[2]) + 0.5 - float(fwd[2]) * 2.0,
                                )
                            except Exception:
                                pass
                            clutter_keep = (
                                ("granite", 0), ("diorite", 0), ("andesite", 0), ("cobbled_deepslate", 0),
                                ("tuff", 0), ("gravel", 0), ("flint", 0), ("dirt", 0), ("cobblestone", 64),
                            )
                            for cname, keep in clutter_keep:
                                if cname == "dirt" and cur_counts.get("cobblestone", 0) < 32:
                                    continue
                                excess = int(cur_counts.get(cname, 0)) - int(keep)
                                if excess > 0:
                                    try:
                                        ctx.guard.write(ctx.guard.client.drop, cname, excess)
                                    except Exception:
                                        break
                            _, cur_items = cave_skill._read_inventory(ctx)
                            home_eval = evaluate_home_trigger(
                                cur_status,
                                cur_items,
                                remaining_seconds=ctx.guard.remaining_timeout(999.0),
                                thresholds=ctx.extra.get("home_thresholds"),
                            )
                        if home_eval["trigger"]:
                            ctx.extra["deep_pos"] = list(cur_foot)
                            ctx.extra["deep_fwd"] = list(fwd)
                            ctx.extra["deep_trail"] = self._compress_trail(trail)
                            ctx.extra["deep_retreat_done"] = True
                            ctx.extra["stop_reason"] = "return_home"
                            ctx.extra["home_reason"] = home_eval["home_reason"]
                            self._retreat_downsampled(ctx, trail)
                            return {"dug_branch_blocks": dug, "patched_branch_floors": patched, "stop_reason": "return_home"}

                stand_cell(ctx, cur_foot, tolerance=0.95)
                if cur_foot[1] > int(band_y) + 2:
                    ctx.extra["deep_pos"] = list(cur_foot)
                    ctx.extra["deep_fwd"] = list(fwd)
                    ctx.extra["deep_trail"] = self._compress_trail(trail)
                    ctx.extra["deep_retreat_done"] = True
                    self._retreat_downsampled(ctx, trail)
                    if stair_timed_out or ctx.guard.remaining_timeout(999.0) < 95.0:
                        ctx.extra["stop_reason"] = "return_home"
                        ctx.extra["home_reason"] = "timeout_checkpoint"
                        return {"dug_branch_blocks": dug, "patched_branch_floors": patched, "stop_reason": "return_home"}
                    ctx.extra["stop_reason"] = "cave_abandoned"
                    raise WorkflowStepError("cave_abandoned", f"Could not descend safely to Y={band_y} (stopped at Y={cur_foot[1]}).", blocker_type="safety_interrupted")

            # Save staircase junction anchor for this band so multi-band shafts can descend further cleanly
            stair_base_len = len(trail)
            stair_base_pos = list(cur_foot)
            stair_base_fwd = tuple(fwd)

            # Phase 2: 3-spaced alternating left/right Fishbone branches at band_y
            start_trunk = int(ctx.extra.get("trunk_done", 0)) + 1 if band_idx == 0 else 1
            band_finished_early = False
            if has_more_bands and targets_map and band_y is not None and int(band_y) >= 20:
                self._sync_quota_progress(ctx, cave_skill)
                if not any(
                    b.get("reason") == "shallow_coal_iron"
                    for b in recommended_mining_bands(targets_map, ctx.extra["quota_progress"])
                ):
                    band_finished_early = True
            for trunk_step in ([] if band_finished_early else range(start_trunk, max_trunk + 1)):
                if targets_map:
                    self._sync_quota_progress(ctx, cave_skill)
                    if not unmet_spawnable_targets_at_y(targets_map, ctx.extra["quota_progress"], cur_foot[1]):
                        if has_more_bands:
                            band_finished_early = True
                            break
                        ctx.extra["stop_reason"] = "band_complete"
                        self._retreat_downsampled(ctx, trail)
                        return {"dug_branch_blocks": dug, "patched_branch_floors": patched, "stop_reason": "band_complete"}
                retreat_reserve = min(95.0, max(25.0, 0.45 * len(trail)))
                if ctx.guard.remaining_timeout(999.0) < retreat_reserve:
                    timed_out_pause = True
                    break
                outcome, d1, p1, consec_floor = self._carve_1x2_step_or_handle_cave(
                    ctx,
                    cave_skill,
                    cur_foot,
                    fwd,
                    opened_caves,
                    consec_floor,
                    trail,
                    stand_after=(trunk_step % 2 == 0 or trunk_step % rib_spacing == 0 or trunk_step == max_trunk),
                )
                dug += d1
                patched += p1
                if outcome == "sealed_unsafe":
                    fwd = (-fwd[2], 0, fwd[0])
                    continue
                if outcome == "return_home":
                    ctx.extra["deep_pos"] = list(cur_foot)
                    ctx.extra["deep_fwd"] = list(fwd)
                    ctx.extra["deep_trail"] = self._compress_trail(trail)
                    ctx.extra["stop_reason"] = "return_home"
                    self._retreat_downsampled(ctx, trail)
                    return {"dug_branch_blocks": dug, "patched_branch_floors": patched, "stop_reason": "return_home"}
                cur_foot = [cur_foot[0] + fwd[0], cur_foot[1], cur_foot[2] + fwd[2]]
                trail.append(list(cur_foot))
                ctx.extra["trunk_done"] = trunk_step
                ctx.extra["deep_pos"] = list(cur_foot)
                ctx.extra["deep_fwd"] = list(fwd)
                ctx.extra["deep_trail"] = self._compress_trail(trail)
                if outcome == "quota_met":
                    ctx.extra["stop_reason"] = "target_reached"
                    self._retreat_downsampled(ctx, trail)
                    return {"dug_branch_blocks": dug, "patched_branch_floors": patched, "stop_reason": "target_reached"}

                if trunk_step % 8 == 0:
                    if CaveExploreSkill._place_near_torch(
                        ctx,
                        trail[-2],
                        lambda p: self._cached_read(ctx, p),
                        lambda p: self._invalidate_cache(ctx, p),
                        forbid_cell=cur_foot,
                    ):
                        ctx.extra["torches_placed"] = int(ctx.extra.get("torches_placed", 0)) + 1

                # Spawn alternating Left and Right 1x2 ribs every rib_spacing (stride=4 -> 3-block pillar!)
                if trunk_step % rib_spacing == 0:
                    left_vec = (fwd[2], 0, -fwd[0])
                    right_vec = (-fwd[2], 0, fwd[0])
                    for side_vec in (left_vec, right_vec):
                        rib_cur = list(cur_foot)
                        rib_trail = [list(cur_foot)]
                        rib_consec = 0
                        for rstep in range(1, rib_len + 1):
                            rib_reserve = min(95.0, max(25.0, 0.45 * (len(trail) + len(rib_trail))))
                            if ctx.guard.remaining_timeout(999.0) < rib_reserve:
                                timed_out_pause = True
                                break
                            ro, d2, p2, rib_consec = self._carve_1x2_step_or_handle_cave(
                                ctx,
                                cave_skill,
                                rib_cur,
                                side_vec,
                                opened_caves,
                                rib_consec,
                                rib_trail,
                                stand_after=(rstep % 2 == 0 or rstep == rib_len),
                            )
                            dug += d2
                            patched += p2
                            if ro == "ok":
                                rib_cur = [rib_cur[0] + side_vec[0], rib_cur[1], rib_cur[2] + side_vec[2]]
                                rib_trail.append(list(rib_cur))
                                if rstep == 8:
                                    if CaveExploreSkill._place_near_torch(
                                        ctx,
                                        rib_trail[-2],
                                        lambda p: self._cached_read(ctx, p),
                                        lambda p: self._invalidate_cache(ctx, p),
                                        forbid_cell=rib_cur,
                                    ):
                                        ctx.extra["torches_placed"] = int(ctx.extra.get("torches_placed", 0)) + 1
                                continue
                            if ro == "quota_met":
                                rib_cur = [rib_cur[0] + side_vec[0], rib_cur[1], rib_cur[2] + side_vec[2]]
                                rib_trail.append(list(rib_cur))
                                self._retreat_downsampled(ctx, rib_trail)
                                self._retreat_downsampled(ctx, trail)
                                ctx.extra["stop_reason"] = "target_reached"
                                return {"dug_branch_blocks": dug, "patched_branch_floors": patched, "stop_reason": "target_reached"}
                            if ro == "return_home":
                                ctx.extra["deep_pos"] = list(cur_foot)
                                ctx.extra["deep_fwd"] = list(fwd)
                                ctx.extra["deep_trail"] = self._compress_trail(trail)
                                self._retreat_downsampled(ctx, rib_trail)
                                self._retreat_downsampled(ctx, trail)
                                ctx.extra["stop_reason"] = "return_home"
                                return {"dug_branch_blocks": dug, "patched_branch_floors": patched, "stop_reason": "return_home"}
                            break
                        self._retreat_downsampled(ctx, rib_trail)
                        if timed_out_pause:
                            break
                    if timed_out_pause:
                        break

                if trunk_step % 4 == 0:
                    cur_status = ctx.guard.check_status(require_safe=True)
                    _, cur_items = cave_skill._read_inventory(ctx)
                    home_eval = evaluate_home_trigger(
                        cur_status,
                        cur_items,
                        remaining_seconds=ctx.guard.remaining_timeout(999.0),
                        thresholds=ctx.extra.get("home_thresholds"),
                    )
                    if home_eval["trigger"]:
                        ctx.extra["deep_pos"] = list(cur_foot)
                        ctx.extra["deep_fwd"] = list(fwd)
                        ctx.extra["deep_trail"] = self._compress_trail(trail)
                        ctx.extra["stop_reason"] = "return_home"
                        ctx.extra["home_reason"] = home_eval["home_reason"]
                        self._retreat_downsampled(ctx, trail)
                        return {"dug_branch_blocks": dug, "patched_branch_floors": patched, "stop_reason": "return_home"}

            if timed_out_pause:
                break
            if has_more_bands:
                if len(trail) > stair_base_len:
                    self._retreat_downsampled(ctx, trail[stair_base_len - 1 :])
                    trail = trail[:stair_base_len]
                cur_foot = list(stair_base_pos)
                fwd = stair_base_fwd
                ctx.extra["trunk_done"] = 0
                if band_finished_early:
                    continue

        self._retreat_downsampled(ctx, trail)
        if self._all_targets_met(ctx, cave_skill):
            ctx.extra["stop_reason"] = "target_reached"
        elif timed_out_pause:
            ctx.extra["deep_pos"] = list(cur_foot)
            ctx.extra["deep_fwd"] = list(fwd)
            ctx.extra["deep_trail"] = self._compress_trail(trail)
            ctx.extra["stop_reason"] = "return_home"
            ctx.extra["home_reason"] = "timeout_checkpoint"
            return {"dug_branch_blocks": dug, "patched_branch_floors": patched, "stop_reason": "return_home"}
        return {"dug_branch_blocks": dug, "patched_branch_floors": patched, "trunk_done": ctx.extra.get("trunk_done", 0)}

    def _wf_deploy_workstation(self, ctx: WorkflowContext) -> Dict[str, Any]:
        """Deploy bed (+right-click spawn), wall torch by tunnel, crafting_table + top furnace, and 2-wide double chest."""
        if not ctx.extra.get("deploy_workstation"):
            return {"skipped": True, "reason": "workstation_disabled_or_items_not_carried"}

        bed_items = tuple(sorted(MINE_BED_BLOCKS))
        # 1. Place bed at (2, -5, 5) foot -> (3, -5, 5) head, standing west at (0.5, -5.0, 5.5) looking +right.
        self._step_to(ctx, (0.5, -5.0, 5.5), tolerance=0.75)
        aim_at(ctx, (2, -5, 5))
        bed_place = place_block(ctx, bed_items, (2, -5, 5))

        bed_foot_w = ctx.world_pos((2, -5, 5), integer=True)
        bed_head_w = ctx.world_pos((3, -5, 5), integer=True)
        foot_blk = ctx.read_block(bed_foot_w)
        head_blk = ctx.read_block(bed_head_w)
        if str(foot_blk.get("name") or "") not in MINE_BED_BLOCKS or str(head_blk.get("name") or "") not in MINE_BED_BLOCKS:
            raise WorkflowStepError(
                "bed_pair_incomplete",
                f"Expected bed halves at {bed_foot_w} and {bed_head_w}, got {foot_blk.get('name')} and {head_blk.get('name')}",
                blocker_type="verification_failed",
                blocker_pos=bed_head_w,
            )

        # Right-click bed head to bind respawn point (and wake cleanly if it's night).
        self._step_to(ctx, (1.5, -5.0, 5.5), tolerance=0.75)
        target_bed_pos = bed_head_w
        foot_props = foot_blk.get("properties") if isinstance(foot_blk.get("properties"), dict) else {}
        if foot_props.get("part") == "head":
            target_bed_pos = bed_foot_w
        respawn_res = ctx.guard.write(ctx.guard.client.interact, pos=target_bed_pos)
        respawn_payload = (
            respawn_res.get("result")
            if isinstance(respawn_res, dict) and isinstance(respawn_res.get("result"), dict)
            else respawn_res
        )
        woke_up = False
        if isinstance(respawn_payload, dict) and respawn_payload.get("slept") is True:
            ctx.guard.write(ctx.guard.client.wake)
            woke_up = True

        # 2. Place wall torch at (1, -4, 5) next to bed and flush against tunnel wall (1, -4, 4).
        self._step_to(ctx, (1.5, -5.0, 6.5), tolerance=0.75)
        torch_wall_w = ctx.world_pos((1, -4, 4), integer=True)
        if str(ctx.read_block(torch_wall_w).get("name") or "air") in AIR:
            place_block(ctx, self.SCAFFOLD_PRIORITY, (1, -4, 4))
        torch_place = place_block(ctx, tuple(MINE_TORCH_BLOCKS), (1, -4, 5))

        # 3. Place crafting_table at (3, -5, 6) and stack furnace directly on top at (3, -4, 6).
        self._step_to(ctx, (1.5, -5.0, 6.5), tolerance=0.75)
        table_place = place_block(ctx, ("crafting_table",), (3, -5, 6))
        furnace_place = place_block(ctx, ("furnace", "lit_furnace"), (3, -4, 6))

        # 4. Place 1 double chest (2 blocks wide: (3, -5, 7) and (3, -5, 8)).
        self._step_to(ctx, (1.5, -5.0, 7.5), tolerance=0.75)
        chest1_place = place_block(ctx, tuple(MINE_CHEST_BLOCKS), (3, -5, 7))
        chest1_w = ctx.world_pos((3, -5, 7), integer=True)
        chest2_w = ctx.world_pos((3, -5, 8), integer=True)
        floor2_w = ctx.world_pos((3, -6, 8), integer=True)

        chest2_name = str(ctx.read_block(chest2_w).get("name") or "air")
        chest2_place: Dict[str, Any] = {"skipped": True, "world_pos": chest2_w, "block": chest2_name}
        if chest2_name not in MINE_CHEST_BLOCKS:
            self._step_to(ctx, (2.5, -5.0, 8.5), tolerance=0.75)
            chosen_chest = ctx.select_item(sorted(MINE_CHEST_BLOCKS))
            if chosen_chest is not None:
                try:
                    ctx.guard.write(ctx.guard.client.look_at, floor2_w[0] + 0.5, floor2_w[1] + 1.0, floor2_w[2] + 0.5)
                    ctx.guard.write(ctx.guard.client.interact, pos=floor2_w, item=chosen_chest)
                except WorkflowStepError:
                    pass
            chest2_name = str(ctx.read_block(chest2_w).get("name") or "air")
            if chest2_name not in MINE_CHEST_BLOCKS:
                self._step_to(ctx, (1.5, -5.0, 8.5), tolerance=0.75)
                chest2_place = place_block(ctx, tuple(MINE_CHEST_BLOCKS), (3, -5, 8))
            else:
                chest2_place = {"skipped": False, "world_pos": chest2_w, "item": chosen_chest, "block": chest2_name}

        c1_blk = ctx.read_block(chest1_w)
        c2_blk = ctx.read_block(chest2_w)
        c1_props = c1_blk.get("properties") if isinstance(c1_blk.get("properties"), dict) else {}
        c2_props = c2_blk.get("properties") if isinstance(c2_blk.get("properties"), dict) else {}
        double_merged = (
            str(c1_props.get("type") or "") in {"left", "right"}
            and str(c2_props.get("type") or "") in {"left", "right"}
        )

        report = {
            "bed": bed_place,
            "bed_respawn_saved": True,
            "bed_interact_result": respawn_res,
            "bed_woke_up": woke_up,
            "torch": torch_place,
            "crafting_table": table_place,
            "furnace": furnace_place,
            "chest_1": chest1_place,
            "chest_2": chest2_place,
            "double_chest_merged": double_merged,
            "chest_types": [c1_props.get("type"), c2_props.get("type")],
        }
        ctx.extra["workstation_report"] = report
        return report

    def _wf_settle_pose(self, ctx: WorkflowContext, *, return_to_surface: bool) -> Dict[str, Any]:
        if return_to_surface:
            live = WorldNavigator.sync_live_pos(ctx)
            if live is not None and live[1] >= int(ctx.origin[1]) - 1:
                try:
                    self._step_to(ctx, (0.5, 0.0, -0.5), tolerance=1.6)
                except WorkflowStepError:
                    pass
                return {"settled": "surface_entry"}
            try:
                # Walk smoothly back up the 5 stairs so upward pathing is explicitly verified.
                for rel in ((0.5, -5.0, 4.5), (0.5, -3.0, 2.5), (0.5, -1.0, 0.5), (0.5, 0.0, -0.5)):
                    self._step_to(ctx, rel, tolerance=0.9)
            except WorkflowStepError:
                escape_to_surface(
                    ctx,
                    max_ascend=160,
                    blocks=SCAFFOLD_BLOCKS,
                    invalidate=lambda p: self._invalidate_cache(ctx, p),
                )
            return {"settled": "surface_entry"}
        self._step_to(ctx, (1.5, -5.0, 6.5), tolerance=0.85)
        return {"settled": "platform_center"}

    def _branch_retry_dig(self, ctx: WorkflowContext, err: WorkflowStepError) -> bool:
        bpos = err.blocker_pos
        if bpos is None:
            return False
        cur = str(ctx.read_block(bpos).get("name") or "air")
        if cur in AIR:
            return True
        pick = ctx.select_item(PICKAXE_PRIORITY)
        if pick is not None:
            try:
                ctx.guard.write(ctx.guard.client.equip, item=pick, slot="hand")
                return True
            except WorkflowStepError:
                return False
        return False

    def _branch_recover_move(self, ctx: WorkflowContext, err: WorkflowStepError) -> bool:
        bpos = err.blocker_pos
        if bpos is not None:
            cur = str(ctx.read_block(bpos).get("name") or "air")
            if cur in (MINE_DIGGABLE_BLOCKS | DEFAULT_REPLACEABLE) and cur not in AIR:
                try:
                    self._check_cell_and_neighbors_safe(ctx, list(bpos))
                    dig_block(ctx, list(bpos), relative=False, allowed_blocks=MINE_DIGGABLE_BLOCKS | DEFAULT_REPLACEABLE, collect=False)
                    self._invalidate_cache(ctx, bpos)
                    return True
                except WorkflowStepError:
                    return False
        return False

    def rest_at_workstation(
        self,
        workstation_slots: Mapping[str, Sequence[int]],
        *,
        targets: Optional[Mapping[str, Any]] = None,
        cobblestone_reserve: int = 32,
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        """Execute bounded rest/resupply cycle at the verified underground workstation.

        Reuses DailySurvivalSkills (store_items, smelt_start, eat_food, auto_sleep)
        and CraftItemSkill without duplicating pathfinding logic.
        """
        from skills.container import ContainerSkill
        from skills.crafting import CraftItemSkill
        from skills.daily_survival import DailySurvivalSkills
        from core.mining_targets import DEFAULT_EDIBLE_FOODS

        container = ContainerSkill(self.client)
        daily = DailySurvivalSkills(self.client)
        parsed_targets = parse_mining_targets(targets) if targets is not None else {}
        protected_drops = {spec["drop_item"] for spec in parsed_targets.values()}
        chest_pos = workstation_slots.get("workstation_chest_1")
        furnace_pos = workstation_slots.get("workstation_furnace")
        actions_taken: List[str] = []

        def read_counts() -> Dict[str, int]:
            raw = self.client.inventory()
            res = raw.get("result") if isinstance(raw, dict) else {}
            items = res.get("items") if isinstance(res, dict) else []
            out: Dict[str, int] = {}
            for row in items:
                if isinstance(row, dict) and isinstance(row.get("name"), str):
                    out[row["name"]] = out.get(row["name"], 0) + int(row.get("count", 0))
            return out

        def safe_daily(action_name: str, args: Dict[str, Any]):
            try:
                result = daily.execute(action_name, args, cancel_event=cancel_event)
                _raise_if_unknown(result)
                return result
            except WorkflowUncertainError:
                raise
            except Exception:
                return SkillResult(False, f"{action_name} unavailable in current client", error_code="unsupported")

        counts = read_counts()

        # 1. Batch-store excess cobblestone and non-target clutter into the double chest via ContainerSkill
        if chest_pos is not None:
            clutter_order = ("cobblestone", "cobbled_deepslate", "dirt", "andesite", "diorite", "granite", "tuff", "gravel")
            batch_put: Dict[str, int] = {}
            for cname in clutter_order:
                if cname in protected_drops:
                    continue
                have = counts.get(cname, 0)
                keep = cobblestone_reserve if cname == "cobblestone" else 0
                to_store = max(0, have - keep)
                if to_store > 0:
                    batch_put[cname] = to_store
            if batch_put:
                try:
                    cr = container.deposit(list(chest_pos), batch_put, ensure_all=False, cancel_event=cancel_event)
                    _raise_if_unknown(cr)
                    if cr.success and (cr.data.get("deposited") or {}):
                        for k, v in (cr.data.get("deposited") or {}).items():
                            actions_taken.append(f"stored:{k}x{v}")
                        counts = read_counts()
                except WorkflowUncertainError:
                    raise
                except Exception:
                    pass

        # 2. Smelt raw_iron / raw_gold via ContainerSkill (supports >16 items & auto-collects existing outputs)
        if furnace_pos is not None:
            raw_smelt = next((k for k in ("raw_iron", "raw_gold") if counts.get(k, 0) > 0), None)
            if raw_smelt:
                try:
                    sr = container.smelt(
                        list(furnace_pos),
                        raw_smelt,
                        counts[raw_smelt],
                        wait_complete=False,
                        cancel_event=cancel_event,
                    )
                    _raise_if_unknown(sr)
                    if sr.success:
                        actions_taken.append(f"smelt_start:{raw_smelt}x{sr.data.get('loaded_input', counts[raw_smelt])}")
                        counts = read_counts()
                except WorkflowUncertainError:
                    raise
                except Exception:
                    pass

        # 3. Eat food if hungry
        raw_st = self.client.get_status()
        st = raw_st.get("result") if isinstance(raw_st, dict) else {}
        if isinstance(st, dict) and float(st.get("food", 20)) < 16:
            edible = next((f for f in DEFAULT_EDIBLE_FOODS if counts.get(f, 0) > 0), None)
            if edible:
                r = safe_daily("eat_food", {"item": edible})
                if r.success:
                    actions_taken.append(f"ate:{edible}")

        # 4. Sleep at workstation bed if night/storm
        if isinstance(st, dict) and (st.get("is_day") is False or st.get("thundering") is True):
            r = safe_daily("auto_sleep", {})
            if r.success:
                actions_taken.append("slept")

        # 5. Restock torches or stone pickaxe via CraftItemSkill when materials exist
        counts = read_counts()
        if counts.get("torch", 0) < 8 and counts.get("coal", 0) >= 1 and counts.get("stick", 0) >= 1:
            r = CraftItemSkill(self.client).execute("torch", count=1)
            _raise_if_unknown(r)
            if r.success:
                actions_taken.append("crafted:torch")
                counts = read_counts()
        if best_usable_pickaxe([{"name": k, "count": v} for k, v in counts.items()]) is None:
            if counts.get("cobblestone", 0) >= 3 and counts.get("stick", 0) >= 2:
                r = CraftItemSkill(self.client).execute("stone_pickaxe", count=1)
                _raise_if_unknown(r)
                if r.success:
                    actions_taken.append("crafted:stone_pickaxe")

        return {"actions": actions_taken, "inventory_after": read_counts()}

    @_journal_boundary
    @_uncertain_boundary
    def run_with_journal(
        self,
        journal,
        *,
        origin: Optional[Sequence[int]] = None,
        orientation: Optional[str] = None,
        branch_length: int = 4,
        targets: Optional[Mapping[str, Any]] = None,
        cave_policy: Optional[Mapping[str, Any]] = None,
        home_thresholds: Optional[Mapping[str, Any]] = None,
        place_workstation: Optional[bool] = None,
        target_y: Optional[int] = None,
        mining_mode: str = "single",
        rib_length: int = 12,
        rib_spacing: int = 4,
        max_trunk_steps: int = 40,
        prefer_caves_first: bool = False,
        cave_search_radius: int = 22,
        timeout_seconds: float = 210.0,
        cancel_event: Optional[threading.Event] = None,
    ) -> SkillResult:
        """Run or resume MineShaftSkill using SurvivalJournal checkpoints while enforcing pending non-replay."""
        # The outer journal boundary already refused old pending state and
        # installed this run's intent before any game I/O.
        resume_data = journal.get_mining_progress()
        if resume_data is not None:
            saved_origin = resume_data.get("origin")
            saved_orient = resume_data.get("orientation", "south")
            saved_len = int(resume_data.get("branch_length", branch_length))
            hub_report = inspect_structure(
                self._hub_only_spec(saved_len, include_workstations=bool(resume_data.get("workstation_deployed"))),
                saved_origin,
                lambda p: self.client.get_block(p[0], p[1], p[2]),
                orientation=saved_orient,
            )
            if not hub_report.get("complete"):
                resume_data = None

        cave_phase_summary: Optional[Dict[str, Any]] = None
        seeded_quota: Optional[Dict[str, int]] = None
        t_start = time.monotonic()
        if prefer_caves_first and resume_data is None and targets:
            cave_skill = CaveExploreSkill(self.client, memory=self.memory)
            cave_res = cave_skill.discover_and_explore_caves(
                origin=origin,
                search_radius=cave_search_radius,
                max_caves=3,
                targets=targets,
                cave_policy=cave_policy,
                home_thresholds=home_thresholds,
                cancel_event=cancel_event,
                timeout_seconds=min(float(timeout_seconds) * 0.55, 220.0),
            )
            if _unknown_result(cave_res):
                return cave_res
            cave_phase_summary = dict(cave_res.data or {})
            seeded_quota = dict(cave_res.data.get("harvested") or {})
            if cave_res.data.get("stop_reason") == "target_reached":
                res_out = SkillResult(
                    True,
                    cave_res.message,
                    data={**cave_phase_summary, "workstation_deployed": False, "cave_first_phase": cave_phase_summary},
                )
                return res_out
            if cave_res.error_code == "return_home_requested":
                return cave_res

        rem_timeout = max(40.0, float(timeout_seconds) - (time.monotonic() - t_start))
        res = self.build_mine_shaft(
            origin=origin,
            orientation=orientation,
            branch_length=branch_length,
            place_workstation=place_workstation,
            targets=targets,
            cave_policy=cave_policy,
            home_thresholds=home_thresholds,
            target_y=target_y,
            mining_mode=mining_mode,
            rib_length=rib_length,
            rib_spacing=rib_spacing,
            max_trunk_steps=max_trunk_steps,
            prefer_caves_first=prefer_caves_first,
            initial_quota_progress=seeded_quota,
            timeout_seconds=rem_timeout,
            resume_progress=resume_data,
            cancel_event=cancel_event,
        )
        if _unknown_result(res):
            return res
        if cave_phase_summary is not None:
            res.data["cave_first_phase"] = cave_phase_summary
        progress = res.data.get("mining_progress")
        if isinstance(progress, dict):
            journal.save_mining_progress(progress)

        if res.error_code == "return_home_requested" and res.data.get("workstation_deployed"):
            rest_summary = self.rest_at_workstation(
                res.data.get("workstation_slots") or {},
                targets=targets,
                cancel_event=cancel_event,
            )
            res.data["rest_summary"] = rest_summary
        return res

    def _ensure_returned_to_surface(
        self,
        start_surface: Sequence[int],
        *,
        retreat_trail: Optional[Sequence[Sequence[int]]] = None,
        timeout_seconds: float = 240.0,
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        """Verify live Y is back at surface level; otherwise follow retreat_trail, 3D-navigate, or pillar-escape up."""
        sx, sy, sz = int(start_surface[0]), int(start_surface[1]), int(start_surface[2])
        raw = self.client.get_status()
        st = raw.get("result") if isinstance(raw, dict) and "result" in raw else raw
        pos = parse_pos(st.get("pos") if isinstance(st, dict) else None)
        if pos is None:
            return {"on_surface": False, "reason": "invalid_pos"}
        if pos[1] >= float(sy) - 1.25:
            return {"on_surface": True, "pos": list(pos), "escaped": False}

        # 1. If we have a known dry staircase trail from this expedition, walk straight back up with a fresh guard
        if retreat_trail and len(retreat_trail) > 1:
            trail_guard = WorkflowGuard(
                self.client,
                cancel_event=cancel_event,
                timeout_seconds=min(60.0, max(20.0, float(timeout_seconds) * 0.6)),
                max_reads=15000,
                allow_damage=True,
                critical_health=1.0,
            )
            trail_ctx = WorkflowContext(
                guard=trail_guard,
                spec=MINE_SHAFT_SPEC,
                origin=[sx, sy, sz],
                orientation="south",
                extra={"block_cache": {}},
            )
            try:
                self._retreat_downsampled(trail_ctx, retreat_trail, step_stride=2, reverse=True)
            except WorkflowUncertainError:
                raise
            except Exception:
                pass
            raw_t = self.client.get_status()
            st_t = raw_t.get("result") if isinstance(raw_t, dict) and "result" in raw_t else raw_t
            pos_t = parse_pos(st_t.get("pos") if isinstance(st_t, dict) else None)
            if pos_t is not None and pos_t[1] >= float(sy) - 2.0:
                return {"on_surface": True, "pos": list(pos_t), "escaped": False, "trail_retreat": True}

        cave_nav = CaveExploreSkill(self.client, memory=self.memory)
        nav = cave_nav.navigate_with_ops(
            [sx, sy, sz],
            allow_pillar=True,
            allow_dig=True,
            allow_bridge=True,
            allow_drop=True,
            max_steps=96,
            timeout_seconds=min(60.0, max(20.0, float(timeout_seconds) * 0.55)),
        )
        _raise_if_unknown(nav)
        raw2 = self.client.get_status()
        st2 = raw2.get("result") if isinstance(raw2, dict) and "result" in raw2 else raw2
        pos2 = parse_pos(st2.get("pos") if isinstance(st2, dict) else None)
        if pos2 is not None and pos2[1] >= float(sy) - 2.0:
            return {"on_surface": True, "pos": list(pos2), "escaped": False, "nav": nav.success}

        guard = WorkflowGuard(
            self.client,
            cancel_event=cancel_event,
            timeout_seconds=max(20.0, float(timeout_seconds)),
            max_reads=25000,
            allow_damage=True,
            critical_health=1.0,
        )
        ctx = WorkflowContext(guard=guard, spec=MINE_SHAFT_SPEC, origin=[sx, sy, sz], orientation="south", extra={})
        esc: Dict[str, Any] = {"escaped": False, "pos": list(pos2 or pos)}
        try:
            esc = escape_to_surface(ctx, max_ascend=160, blocks=SCAFFOLD_BLOCKS)
        except WorkflowStepError:
            esc = {"escaped": False, "pos": list(pos2 or pos)}
        if not esc.get("escaped"):
            ep = parse_pos(esc.get("pos")) or pos2
            if ep is not None and ep[1] < float(sy) - 2.0:
                for dx, dz in ((2, 0), (-2, 0), (0, 2), (0, -2)):
                    try:
                        lateral = cave_nav.navigate_with_ops(
                            [int(ep[0]) + dx, int(ep[1]), int(ep[2]) + dz],
                            allow_pillar=True,
                            allow_dig=True,
                            allow_bridge=True,
                            allow_drop=True,
                            max_steps=8,
                            timeout_seconds=10.0,
                        )
                        _raise_if_unknown(lateral)
                        esc = escape_to_surface(ctx, max_ascend=160, blocks=SCAFFOLD_BLOCKS)
                    except WorkflowUncertainError:
                        raise
                    except Exception:
                        continue
                    if esc.get("escaped"):
                        break
        if not esc.get("escaped"):
            try:
                raw_fin = self.client.get_status()
                st_fin = raw_fin.get("result") if isinstance(raw_fin, dict) and "result" in raw_fin else raw_fin
                pos_fin = parse_pos(st_fin.get("pos") if isinstance(st_fin, dict) else None)
                if pos_fin is not None:
                    esc["pos"] = list(pos_fin)
            except Exception:
                pass
        return {"on_surface": bool(esc.get("escaped")), "escaped": bool(esc.get("escaped")), "pos": esc.get("pos")}

    @_uncertain_boundary
    def mine_targets_from_surface(
        self,
        origin: Optional[Sequence[int]] = None,
        *,
        targets: Optional[Mapping[str, Any]] = None,
        cave_policy: Optional[Mapping[str, Any]] = None,
        home_thresholds: Optional[Mapping[str, Any]] = None,
        prefer_caves_first: bool = True,
        cave_search_radius: int = 28,
        max_caves: int = 3,
        place_workstation: Optional[bool] = None,
        rib_length: int = 12,
        rib_spacing: int = 4,
        max_trunk_steps: int = 32,
        band_timeout_seconds: float = 260.0,
        total_timeout_seconds: float = 720.0,
        cancel_event: Optional[threading.Event] = None,
        allow_damage: bool = True,
    ) -> SkillResult:
        """Start from the surface, explore shallow caves first, then mine stratified Y-bands (coal+iron vs diamond+iron) and guarantee surface return."""
        parsed_targets = parse_mining_targets(targets if targets is not None else DEFAULT_MINING_TARGETS)
        raw_st = self.client.get_status()
        st0 = raw_st.get("result") if isinstance(raw_st, dict) and "result" in raw_st else raw_st
        bp0 = parse_pos(st0.get("pos") if isinstance(st0, dict) else None)
        if origin is not None:
            p0 = parse_pos(origin, integer=True)
            if p0 is None:
                return SkillResult(False, "origin must be 3 integers", error_code="bad_args")
            start_surface = list(p0)
        elif bp0 is not None:
            start_surface = [math.floor(bp0[0] + 0.0001), math.floor(bp0[1] + 0.0001), math.floor(bp0[2] + 0.0001)]
        else:
            return SkillResult(False, "Cannot resolve surface start position", error_code="invalid_state")

        before_counts = dict(self.client.get_inventory_counts() or {})
        t_start = time.monotonic()

        def _net_counts() -> Dict[str, int]:
            now_c = dict(self.client.get_inventory_counts() or {})
            out: Dict[str, int] = {}
            for k in set(now_c) | set(before_counts):
                diff = max(0, int(now_c.get(k, 0)) - int(before_counts.get(k, 0)))
                if diff > 0:
                    out[k] = diff
            return out

        phase_reports: List[Dict[str, Any]] = []
        stop_reason = "bands_exhausted"

        if prefer_caves_first and parsed_targets:
            cave_t = min(120.0, float(band_timeout_seconds) * 0.5, max(45.0, float(total_timeout_seconds) * 0.25))
            cave_skill = CaveExploreSkill(self.client, memory=self.memory)
            cave_res = cave_skill.discover_and_explore_caves(
                origin=start_surface,
                search_radius=cave_search_radius,
                max_caves=max_caves,
                targets=targets,
                cave_policy=cave_policy,
                home_thresholds=home_thresholds,
                cancel_event=cancel_event,
                timeout_seconds=cave_t,
                allow_damage=allow_damage,
            )
            if _unknown_result(cave_res):
                return cave_res
            surface_chk = self._ensure_returned_to_surface(start_surface, cancel_event=cancel_event)
            net_now = _net_counts()
            phase_reports.append({
                "phase": "surface_caves",
                "success": cave_res.success,
                "error_code": cave_res.error_code,
                "message": cave_res.message,
                "stop_reason": (cave_res.data or {}).get("stop_reason"),
                "harvested": dict(net_now),
                "surface_return": surface_chk,
            })
            if (cave_res.data or {}).get("stop_reason") == "target_reached" or (
                parsed_targets and all(
                    (net_now.get(spec["drop_item"], 0) + (net_now.get("iron_ingot", 0) if spec["drop_item"] == "raw_iron" else 0))
                    >= int(spec["count"])
                    for spec in parsed_targets.values()
                )
            ):
                stop_reason = "target_reached"
                return SkillResult(
                    True,
                    f"Completed all targets from surface caves: harvested={net_now}",
                    data={"harvested": net_now, "stop_reason": stop_reason, "phases": phase_reports, "surface_return": surface_chk},
                )

        # Continuous single-shaft stratified schedule (coal@Y=28 -> diamond@Y=-54 -> peak iron@Y=16)
        net_now = _net_counts()
        bands = recommended_mining_bands(parsed_targets, net_now)
        if not bands:
            stop_reason = "target_reached"
        else:
            schedule = [int(b["target_y"]) for b in bands]
            rem_total = max(90.0, float(total_timeout_seconds) - (time.monotonic() - t_start) - 10.0)
            self._ensure_returned_to_surface(start_surface, cancel_event=cancel_event)
            res = self.build_mine_shaft(
                radius=20,
                branch_length=4,
                place_workstation=place_workstation,
                return_to_surface=True,
                targets=targets,
                cave_policy=cave_policy,
                home_thresholds=home_thresholds,
                target_ys=schedule,
                mining_mode="fishbone",
                rib_length=rib_length,
                rib_spacing=rib_spacing,
                max_trunk_steps=max_trunk_steps,
                initial_quota_progress=net_now,
                timeout_seconds=min(float(band_timeout_seconds), rem_total),
                cancel_event=cancel_event,
                allow_damage=allow_damage,
            )
            if _unknown_result(res):
                return res
            retreat_trail = ((res.data or {}).get("mining_progress") or {}).get("deep_trail") or []
            surface_chk = self._ensure_returned_to_surface(
                start_surface,
                retreat_trail=retreat_trail,
                cancel_event=cancel_event,
            )
            net_after = _net_counts()
            phase_reports.append({
                "phase": f"stratified_shaft_{schedule}",
                "bands": bands,
                "success": res.success,
                "error_code": res.error_code,
                "message": res.message,
                "stop_reason": (res.data or {}).get("stop_reason"),
                "harvested": dict(net_after),
                "surface_return": surface_chk,
            })
            if not recommended_mining_bands(parsed_targets, net_after) or (res.data or {}).get("stop_reason") == "target_reached":
                stop_reason = "target_reached"
            elif res.error_code == "return_home_requested":
                stop_reason = "return_home"
            elif (res.data or {}).get("stop_reason") in {"band_complete", "branch_done"}:
                stop_reason = str((res.data or {}).get("stop_reason"))
            elif time.monotonic() - t_start >= float(total_timeout_seconds) - 10.0:
                stop_reason = "timeout"

        final_surface = self._ensure_returned_to_surface(
            start_surface,
            retreat_trail=retreat_trail if bands else None,
            cancel_event=cancel_event,
        )
        final_net = _net_counts()
        target_drop_items = {spec["drop_item"] for spec in parsed_targets.values()} | {"iron_ingot", "gold_ingot"}
        target_ore_gained = sum(int(final_net.get(k, 0)) for k in target_drop_items)
        shaft_ok = (not bands) or bool(phase_reports and phase_reports[-1].get("success"))
        done_ok = stop_reason == "target_reached" or bool(
            final_surface.get("on_surface") and target_ore_gained > 0 and shaft_ok
        )
        return SkillResult(
            done_ok,
            f"Surface stratified mining finished (stop={stop_reason}, on_surface={final_surface.get('on_surface')}, harvested={final_net})",
            error_code=None if done_ok else stop_reason,
            data={
                "harvested": final_net,
                "stop_reason": stop_reason,
                "phases": phase_reports,
                "surface_return": final_surface,
            },
        )
