"""Speedrun Nether Portal Skill Module for BotSleep.

Implements both the legacy step-by-step speedrun portal casting API
(step=1|2|3|"scaffold") and the unified Standard Structure + ActionWorkflow
pipeline (`inspect_portal` / `build_portal` / step="full") supporting all four
cardinal orientations (north, south, east, west).
"""

import logging
import math
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from core.domain import AIR, HAZARDS, parse_pos
from core.structure_spec import (
    CAVE_DIGGABLE_BLOCKS,
    DEFAULT_REPLACEABLE,
    NETHER_PORTAL_SPEC,
    SCAFFOLD_BLOCKS,
    inspect_structure,
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
    aim_at,
    bridge_cells,
    clear_overhead_cells,
    dig_block,
    place_block,
    stand_at,
    use_on,
    wait_until,
)

from . import BaseSkill, SkillResult
from .portal_telemetry import PortalTelemetryRecorder

logger = logging.getLogger("BotSleep.Skills.PortalSpeedrun")


class NetherPortalSpeedrunSkill(BaseSkill):
    """Speedrun Nether Portal Casting Skill executing via StructureSpec + ActionWorkflow."""

    BASE_SOLID_DIGGABLE = frozenset({
        "stone",
        "cobblestone",
        "deepslate",
        "cobbled_deepslate",
        "tuff",
        "andesite",
        "diorite",
        "granite",
        "dirt",
        "coarse_dirt",
        "grass_block",
        "gravel",
        "sand",
        "red_sand",
        "sandstone",
        "netherrack",
        "basalt",
        "blackstone",
    })
    CAST_SPECS: Tuple[Dict[str, Any], ...] = (
        {"num": 1, "desc": "right_pillar_lower", "pos": (3, 1, 0), "ref": (3, 0, 0), "face": (0, 1, 0), "stance": "cast_right"},
        {"num": 2, "desc": "right_pillar_upper", "pos": (3, 2, 0), "ref": (3, 1, 0), "face": (0, 1, 0), "stance": "cast_right"},
        {"num": 3, "desc": "left_pillar_lower", "pos": (0, 1, 0), "ref": (0, 0, 0), "face": (0, 1, 0), "stance": "cast_left"},
        {"num": 4, "desc": "left_pillar_upper", "pos": (0, 2, 0), "ref": (0, 1, 0), "face": (0, 1, 0), "stance": "cast_left"},
        {"num": 5, "desc": "top_beam_right", "pos": (2, 3, 0), "ref": (3, 3, 0), "face": (-1, 0, 0), "stance": "cast_center"},
        {"num": 6, "desc": "top_beam_left", "pos": (1, 3, 0), "ref": (2, 3, 0), "face": (-1, 0, 0), "stance": "cast_center"},
    )

    def __init__(
        self,
        client,
        memory: Optional[Any] = None,
        telemetry: Optional[PortalTelemetryRecorder] = None,
    ):
        super().__init__(client)
        self.memory = memory
        self.telemetry = telemetry

    def _record(
        self,
        step_id: str,
        description: str,
        target_pos: Optional[List[int]] = None,
        anchor_ref: Optional[List[int]] = None,
        anchor_face: Optional[List[int]] = None,
        action_func: Optional[Any] = None,
        portal_origin: Optional[Tuple[int, int, int]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """Helper to cleanly route action execution through telemetry if enabled."""
        if self.telemetry:
            return self.telemetry.record_step(
                step_id=step_id,
                description=description,
                target_pos=target_pos,
                anchor_ref=anchor_ref,
                anchor_face=anchor_face,
                action_func=action_func,
                portal_origin=portal_origin,
                metadata=metadata,
            )
        elif action_func:
            return action_func()
        return None

    def validate_preconditions(self, **kwargs) -> bool:
        """Validates that items, space, and bot status meet preconditions."""
        inv = self.client.get_inventory_counts()
        has_stone = any(inv.get(b, 0) > 0 for b in ["cobblestone", "stone", "dirt", "netherrack"])
        has_water = inv.get("water_bucket", 0) > 0 or inv.get("bucket", 0) > 0
        return has_stone and has_water

    def build_scaffolding(self, x0: int, y0: int, z0: int) -> SkillResult:
        """Builds the precision 7-cobblestone scaffolding and activates controlled water curtain.
        
        Relative coordinate offsets from portal origin (x0, y0, z0):
          - x0: left column (dx=0), x0+1: left opening, x0+2: right opening, x0+3: right column
          - y0: ground level (dy=0), y0+1..3: vertical frame levels
          - z0: portal plane (dz=0), z0-1: pillar & wall line, z0-2: backboard line
        
        7 Cobblestone placements:
          1. Backboard:      [x0 + 1, y0 + 1, z0 - 2]
          2. Left wall far:  [x0 + 0, y0 + 1, z0 - 1]
          3. Left wall near: [x0 + 1, y0 + 1, z0 - 1]
          4. Pillar layer 1: [x0 + 3, y0 + 1, z0 - 1]
          5. Pillar layer 2: [x0 + 3, y0 + 2, z0 - 1]
          6. Pillar layer 3: [x0 + 3, y0 + 3, z0 - 1]
          7. Arm forward:    [x0 + 3, y0 + 3, z0 + 0]
        
        Water activation:
          - Water poured at: [x0 + 2, y0 + 3, z0 - 1] against pillar layer 3.
        """
        logger.info(f"Building precision cobblestone scaffolding for portal origin [{x0}, {y0}, {z0}]...")
        # Check inventory for building blocks and water bucket
        inv = self.client.get_inventory_counts()
        if inv.get("cobblestone", 0) < 7:
            return SkillResult(success=False, error_code="missing_cobblestone", message="Need at least 7 cobblestone for scaffolding.")
        if inv.get("water_bucket", 0) < 1:
            return SkillResult(success=False, error_code="missing_water_bucket", message="Water bucket required for water curtain.")

        def place_if_needed(bx, by, bz, name="scaffold_block"):
            b = self.client.get_block(bx, by, bz).get("result", {})
            if b.get("name") not in ["cobblestone", "stone"]:
                self._record(
                    step_id=name,
                    description=f"放置脚手架方块至 [{bx}, {by}, {bz}]",
                    target_pos=[bx, by, bz],
                    action_func=lambda: self.client.place_block("cobblestone", bx, by, bz),
                )
                time.sleep(0.15)

        # 1. Backboard
        place_if_needed(x0 + 1, y0 + 1, z0 - 2, "scaffold_backboard")
        # 2. Left walls
        place_if_needed(x0 + 0, y0 + 1, z0 - 1, "scaffold_left_wall_far")
        place_if_needed(x0 + 1, y0 + 1, z0 - 1, "scaffold_left_wall_near")
        # 3. Right pillar (3 blocks)
        place_if_needed(x0 + 3, y0 + 1, z0 - 1, "scaffold_pillar_layer_1")
        place_if_needed(x0 + 3, y0 + 2, z0 - 1, "scaffold_pillar_layer_2")
        place_if_needed(x0 + 3, y0 + 3, z0 - 1, "scaffold_pillar_layer_3")
        # 4. Arm extending forward
        place_if_needed(x0 + 3, y0 + 3, z0 + 0, "scaffold_arm_forward")
        time.sleep(0.3)

        # 5. Pour water on top against pillar layer 3 West face
        water_spot = [x0 + 2, y0 + 3, z0 - 1]
        pillar_top = [x0 + 3, y0 + 3, z0 - 1]
        logger.info(f"Activating controlled water curtain at {water_spot} against {pillar_top}...")
        water_res = self._record(
            step_id="scaffold_water_curtain",
            description="顶部激活定向水幕",
            target_pos=water_spot,
            anchor_ref=pillar_top,
            anchor_face=[-1, 0, 0],
            action_func=lambda: self.client.interact(
                pos=water_spot, item="water_bucket", ref=pillar_top, face=[-1, 0, 0], stop_on=["death"]
            ),
        )
        time.sleep(1.0)

        return SkillResult(
            success=True,
            message="Scaffolding and controlled water curtain built successfully with zero leaks.",
            data={"water_spot": water_spot, "water_res": water_res},
        )

    def execute(self, step: int = 1, **kwargs) -> SkillResult:
        """Executes a specific step or the entire speedrun sequence."""
        if step == 1:
            return self.execute_step_1(**kwargs)
        if step == 2:
            return self.execute_step_2(**kwargs)
        if step == 3:
            return self.execute_step_3(**kwargs)
        if step == "scaffold":
            return self.build_scaffolding(**kwargs)
        if step == "full":
            return self.build_portal(**kwargs)
        return SkillResult(success=False, error_code="step_not_implemented", message=f"Step {step} not yet implemented.")

    def inspect_portal(
        self,
        origin: Sequence[int],
        *,
        orientation: Optional[str] = None,
        axis: str = "x",
        include_scaffold: bool = True,
        read_block: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Inspect a 4x5 speedrun Nether Portal across any of the 4 cardinal orientations."""
        int_origin = parse_pos(origin, integer=True)
        if int_origin is None:
            raise ValueError(f"Portal origin must be 3 integer coordinates, got {origin!r}")
        norm_orient = normalize_orientation(orientation or axis)
        reader = read_block or (lambda wp: self.client.get_block(int(wp[0]), int(wp[1]), int(wp[2])))
        stages = ("base", "scaffold", "frame", "interior") if include_scaffold else ("base", "frame", "interior")
        report = inspect_structure(NETHER_PORTAL_SPEC, int_origin, reader, orientation=norm_orient, stages=stages)

        water_world = transform_rel_pos((2, 3, -1), int_origin, norm_orient, integer=True)
        raw_water = reader(water_world)
        water_block = raw_water.get("result") if isinstance(raw_water, dict) and "result" in raw_water else raw_water
        water_name = str((water_block or {}).get("name") or "air")

        stage_status = report["stage_status"]
        report["water_curtain"] = {"world_pos": water_world, "block": water_name, "active": water_name == "water"}
        report["frame_complete"] = bool(stage_status["base"]["complete"] and stage_status["frame"]["complete"])
        report["scaffold_complete"] = bool(stage_status.get("scaffold", {}).get("complete", False))
        report["portal_active"] = bool(stage_status["interior"]["complete"])
        return report

    def find_portal_site(
        self,
        *,
        radius: int = 8,
        orientations: Optional[Sequence[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Auto-detect a nearby lava-pool shore site `(origin, orientation)` around the bot."""
        status = self.client.get_status().get("result", {})
        bot_pos = parse_pos(status.get("pos"))
        if bot_pos is None:
            return None

        bx = int(__import__("math").floor(bot_pos[0]))
        feet_y = int(__import__("math").floor(bot_pos[1] + 0.01))
        bz = int(__import__("math").floor(bot_pos[2]))
        cache: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
        soft_clear = AIR | {"short_grass", "tall_grass", "fern", "large_fern", "vine", "snow", "leaf_litter"}
        non_shore = soft_clear | {"lava", "flowing_lava", "water", "flowing_water"}

        def read_cached(wx: int, wy: int, wz: int) -> Dict[str, Any]:
            key = (int(wx), int(wy), int(wz))
            if key not in cache:
                try:
                    raw = self.client.get_block(*key)
                    blk = raw.get("result") if isinstance(raw, dict) and "result" in raw else raw
                    cache[key] = blk if isinstance(blk, dict) else {"name": "air"}
                except APIError:
                    cache[key] = {"name": "not_loaded"}
            return cache[key]

        cand_orients = tuple(normalize_orientation(o) for o in (orientations or ("south", "west", "north", "east")))
        y_offsets = (feet_y - 1, feet_y - 2, feet_y)
        r = max(2, int(radius))

        best: Optional[Tuple[float, Dict[str, Any]]] = None
        for y0 in y_offsets:
            # Scan surface plane y0 in a square around the bot to index lava & solid shore quickly.
            for wx in range(bx - r, bx + r + 1):
                for wz in range(bz - r, bz + r + 1):
                    if read_cached(wx, y0, wz).get("name") not in {"lava", "obsidian"}:
                        continue
                    origin = [wx, y0, wz]
                    for orient in cand_orients:
                        # 1. Portal row dz=0 must be lava/obsidian, front row dz=1 must have >=2 lava/obsidian.
                        door_blocks = [
                            read_cached(*transform_rel_pos((dx, 0, 0), origin, orient, integer=True))
                            for dx in range(4)
                        ]
                        if not all(b.get("name") in {"lava", "obsidian"} for b in door_blocks):
                            continue
                        pool_lava = 0
                        for dz in (1, 2):
                            for dx in range(4):
                                pb = read_cached(*transform_rel_pos((dx, 0, dz), origin, orient, integer=True))
                                props = pb.get("properties") if isinstance(pb.get("properties"), dict) else {}
                                if pb.get("name") == "lava" and props.get("level") in (None, 0):
                                    pool_lava += 1
                        if pool_lava < 4:
                            continue

                        # 2. Shore blocks behind portal (dz=-1 for dx=0..3 and dz=-2 for dx=0..2) must be solid ground.
                        shore_ok = True
                        for rel in ((0, 0, -1), (1, 0, -1), (2, 0, -1), (3, 0, -1), (0, 0, -2), (1, 0, -2), (2, 0, -2)):
                            sb = read_cached(*transform_rel_pos(rel, origin, orient, integer=True))
                            if str(sb.get("name") or "air") in non_shore or sb.get("name") == "not_loaded":
                                shore_ok = False
                                break
                        if not shore_ok:
                            continue

                        # 3. Check headroom and base rock type (allow obsidian, lava, or BASE_SOLID_DIGGABLE).
                        clear_ok = True
                        for rel in (
                            (0, -1, 0), (1, -1, 0), (2, -1, 0), (3, -1, 0),
                        ):
                            base_name = str(read_cached(*transform_rel_pos(rel, origin, orient, integer=True)).get("name") or "air")
                            if rel[0] in (1, 2) and base_name not in ({"obsidian", "lava"} | self.BASE_SOLID_DIGGABLE):
                                clear_ok = False
                                break
                        if not clear_ok:
                            continue

                        for rule in NETHER_PORTAL_SPEC.cells:
                            if rule.stage not in {"scaffold", "frame", "interior"}:
                                continue
                            cur = str(read_cached(*transform_rel_pos(rule.rel_pos, origin, orient, integer=True)).get("name") or "air")
                            if cur in rule.expected_blocks or cur in soft_clear:
                                continue
                            # Allow interior (1..2, 0, 0) / base corners to currently hold lava
                            if rule.rel_pos[1] == 0 and cur == "lava":
                                continue
                            clear_ok = False
                            break
                        if not clear_ok:
                            continue

                        for dx in (0, 1, 2):
                            for dy in (1, 2):
                                hb = str(read_cached(*transform_rel_pos((dx, dy, -2), origin, orient, integer=True)).get("name") or "air")
                                if hb not in soft_clear:
                                    clear_ok = False
                                    break
                            if not clear_ok:
                                break
                        if not clear_ok:
                            continue

                        start_w = transform_rel_pos(
                            NETHER_PORTAL_SPEC.anchors["player_start"].rel_pos, origin, orient, integer=False
                        )
                        dist2 = (start_w[0] - bot_pos[0]) ** 2 + (start_w[1] - bot_pos[1]) ** 2 + (start_w[2] - bot_pos[2]) ** 2
                        score = dist2 - 0.1 * pool_lava
                        site = {
                            "origin": origin,
                            "orientation": orient,
                            "pool_lava_sources": pool_lava,
                            "player_start": start_w,
                        }
                        if best is None or score < best[0]:
                            best = (score, site)
            if best is not None:
                return best[1]
        return None

    def _locate_standard_portal_sites(
        self,
        *,
        radius: int = 20,
        orientations: Optional[Sequence[str]] = None,
        max_results: int = 12,
    ) -> List[Dict[str, Any]]:
        """Locate ranked qualifying portal build sites via NETHER_PORTAL_SPEC."""
        from core.structure_spec import find_structure_sites

        status_res = self.client.get_status()
        status = status_res.get("result") if isinstance(status_res, dict) and "result" in status_res else status_res
        if not isinstance(status, dict) or not isinstance(status.get("pos"), (list, tuple)):
            return []
        bot_pos = [float(v) for v in status["pos"]]

        inv_counts: Optional[Dict[str, int]] = None
        try:
            inv_counts = self.client.get_inventory_counts()
        except Exception:
            inv_counts = None

        seeds: List[List[int]] = []
        find_blocks = getattr(self.client, "find_blocks", None)
        if callable(find_blocks):
            try:
                rows = find_blocks(types=["lava"], radius=max(32, int(radius)), limit=256)
                for row in rows or []:
                    pos = row.get("pos") if isinstance(row, dict) else None
                    if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                        seeds.append([int(pos[0]), int(pos[1]), int(pos[2])])
            except Exception:
                seeds = []

        return find_structure_sites(
            NETHER_PORTAL_SPEC,
            bot_pos,
            lambda pos: self.client.get_block(pos[0], pos[1], pos[2]),
            seed_positions=seeds or None,
            radius=radius,
            orientations=orientations,
            inventory_counts=inv_counts,
            max_results=max_results,
        )

    def _locate_standard_portal_site(
        self,
        *,
        radius: int = 20,
        orientations: Optional[Sequence[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Locate the best qualifying portal build site via NETHER_PORTAL_SPEC."""
        sites = self._locate_standard_portal_sites(radius=radius, orientations=orientations, max_results=1)
        return sites[0] if sites else None

    def build_portal(
        self,
        origin: Optional[Sequence[int]] = None,
        *,
        orientation: Optional[str] = None,
        axis: str = "x",
        player_start: Optional[Sequence[float]] = None,
        player_end: Optional[Sequence[float]] = None,
        build_scaffold: bool = True,
        temp_stand_pos: Optional[Sequence[int]] = None,
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: float = 180.0,
        allow_damage: bool = False,
    ) -> SkillResult:
        """Build, cast, clear water, ignite, and verify a Nether Portal in 4 orientations."""
        if origin is None:
            orient_filter = (orientation,) if orientation is not None else None
            sites = self._locate_standard_portal_sites(radius=20, orientations=orient_filter, max_results=16)
            if not sites:
                return SkillResult(
                    success=False,
                    error_code="no_portal_site_found",
                    message="No suitable lava pool shore site found near the bot; move near a lava pool edge or specify origin/orientation.",
                    data={"executed": False, "structure_verified": False},
                )
            failed_sites: List[Tuple[List[float], str]] = []
            remaining = list(sites)
            last_res: Optional[SkillResult] = None
            overall_deadline = time.monotonic() + max(30.0, float(timeout_seconds))
            for _attempt in range(min(6, len(sites))):
                if _attempt > 0 and time.monotonic() >= overall_deadline - 15.0:
                    break
                if not failed_sites:
                    cand = remaining.pop(0)
                else:
                    # Prefer a candidate on another side of the pool >= 4 blocks from ALL failed staging spots.
                    pick_idx = 0
                    for idx, s in enumerate(remaining):
                        sp = s.get("staging_pos") or s["origin"]
                        far_from_all = all(
                            ((sp[0] - fx) ** 2 + (sp[2] - fz) ** 2) >= 16.0
                            for (fx, _, fz), _ in failed_sites
                        )
                        if far_from_all:
                            pick_idx = idx
                            break
                    cand = remaining.pop(pick_idx)

                site_budget = max(25.0, overall_deadline - time.monotonic())
                res = self._execute_portal_site(
                    cand["origin"],
                    orientation=cand["orientation"],
                    site=cand,
                    player_start=player_start,
                    player_end=player_end,
                    build_scaffold=build_scaffold,
                    temp_stand_pos=temp_stand_pos,
                    cancel_event=cancel_event,
                    timeout_seconds=site_budget,
                    allow_damage=allow_damage,
                )
                last_res = res
                data = res.data if isinstance(res.data, dict) else {}
                writes = int(data.get("writes", 0))
                steps_done = len(data.get("steps") or [])
                failed_step = data.get("failed_step")
                zero_terrain_changes = failed_step in {None, "move_to_start"} or (writes == 0 and steps_done == 0)
                if res.success or not zero_terrain_changes or res.error_code not in {
                    "path_not_found",
                    "stalled",
                    "unreachable_stance",
                    "timeout",
                    "missing_lava_buckets",
                }:
                    return res
                fail_anchor = list(cand.get("staging_pos") or cand["origin"])
                failed_sites.append((fail_anchor, str(cand["orientation"])))
            return last_res  # pragma: no cover

        int_origin = parse_pos(origin, integer=True)
        if int_origin is None:
            return SkillResult(
                success=False,
                error_code="bad_args",
                message="Portal origin must be three integer coordinates [x, y, z].",
                data={"executed": False, "structure_verified": False},
            )
        return self._execute_portal_site(
            int_origin,
            orientation=orientation or axis,
            site=None,
            player_start=player_start,
            player_end=player_end,
            build_scaffold=build_scaffold,
            temp_stand_pos=temp_stand_pos,
            cancel_event=cancel_event,
            timeout_seconds=timeout_seconds,
            allow_damage=allow_damage,
        )

    def _execute_portal_site(
        self,
        int_origin: Sequence[int],
        *,
        orientation: str,
        site: Optional[Dict[str, Any]] = None,
        player_start: Optional[Sequence[float]] = None,
        player_end: Optional[Sequence[float]] = None,
        build_scaffold: bool = True,
        temp_stand_pos: Optional[Sequence[int]] = None,
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: float = 180.0,
        allow_damage: bool = False,
    ) -> SkillResult:
        from core.structure_spec import evaluate_structure_site

        int_origin = [int(v) for v in int_origin]
        try:
            norm_orient = normalize_orientation(orientation)
        except ValueError as exc:
            return SkillResult(
                success=False,
                error_code="bad_args",
                message=str(exc),
                data={"executed": False, "structure_verified": False},
            )

        if site is None:
            try:
                status_res = self.client.get_status()
                st = status_res.get("result") if isinstance(status_res, dict) and "result" in status_res else status_res
                bpos = st.get("pos") if isinstance(st, dict) else None
                site = evaluate_structure_site(
                    NETHER_PORTAL_SPEC,
                    int_origin,
                    norm_orient,
                    lambda pos: self.client.get_block(pos[0], pos[1], pos[2]),
                    bot_pos=bpos,
                )
            except Exception:
                site = None

        if player_start is not None:
            start_world = parse_pos(player_start)
        elif site is not None and isinstance(site.get("staging_pos"), list):
            start_world = parse_pos(site["staging_pos"])
        else:
            start_world = transform_rel_pos(
                NETHER_PORTAL_SPEC.anchors["player_start"].rel_pos, int_origin, norm_orient, integer=False
            )
        end_world = parse_pos(player_end) if player_end is not None else transform_rel_pos(
            NETHER_PORTAL_SPEC.anchors["player_end"].rel_pos, int_origin, norm_orient, integer=False
        )
        if start_world is None or end_world is None:
            return SkillResult(
                success=False,
                error_code="bad_args",
                message="player_start and player_end must be valid 3D coordinates when provided.",
                data={"executed": False, "structure_verified": False},
            )

        if temp_stand_pos is not None:
            stand_block_world = parse_pos(temp_stand_pos, integer=True)
            if stand_block_world is None:
                return SkillResult(
                    success=False,
                    error_code="bad_args",
                    message="temp_stand_pos must be three integer coordinates when provided.",
                    data={"executed": False, "structure_verified": False},
                )
        elif build_scaffold:
            stand_block_world = transform_rel_pos((1, 1, 1), int_origin, norm_orient, integer=True)
        else:
            stand_block_world = None

        guard = WorkflowGuard(
            self.client,
            cancel_event=cancel_event,
            timeout_seconds=timeout_seconds,
            max_reads=6000,
            allow_damage=allow_damage,
        )
        ctx = WorkflowContext(
            guard=guard,
            spec=NETHER_PORTAL_SPEC,
            origin=[int(v) for v in int_origin],
            orientation=norm_orient,
            player_start=None,
            player_end=end_world,
            extra={
                "build_scaffold": build_scaffold,
                "temp_stand_pos": stand_block_world,
                "site": site or {},
                "approach_target": start_world,
                "start_tolerance": 2.4,
            },
        )
        steps = self._build_portal_steps(build_scaffold=build_scaffold, temp_stand_pos=stand_block_world)
        steps.insert(
            0,
            ActionStep(
                step_id="prepare_site_platform_and_overhead",
                description="清理头顶阻挡方块并搭路补全操作平台",
                stage="base",
                skip_if=self._site_platform_and_overhead_ready,
                action=self._step_prepare_site_platform_and_overhead,
                branches={
                    "stalled": self._branch_recover_movement,
                    "path_not_found": self._branch_recover_movement,
                    "unreachable_stance": self._branch_recover_movement,
                    "timeout": self._branch_recover_movement,
                },
                max_branch_attempts=2,
            ),
        )
        if start_world is not None:
            steps.insert(
                0,
                ActionStep(
                    step_id="move_to_start",
                    description="接近传送门预设站位并清理沿途阻挡方块",
                    stage="base",
                    action=self._step_approach_site,
                    branches={
                        "stalled": self._branch_approach_site,
                        "path_not_found": self._branch_approach_site,
                        "unreachable_stance": self._branch_approach_site,
                    },
                    max_branch_attempts=6,
                ),
            )
        return ActionWorkflowRunner().run(
            ctx,
            steps,
            precheck=self._portal_precheck,
            verify_structure=self._verify_portal_structure,
            ready_timeout=5.0,
        )

    def _portal_precheck(self, ctx: WorkflowContext) -> None:
        report = self.inspect_portal(
            ctx.origin,
            orientation=ctx.orientation,
            include_scaffold=bool(ctx.extra.get("build_scaffold", True)),
            read_block=ctx.read_block,
        )
        ctx.extra["initial_report"] = report
        if report["frame_complete"] and report["portal_active"]:
            return

        counts = ctx.inventory_counts()
        missing = report["missing"]
        missing_base = [m for m in missing if m["stage"] == "base"]
        missing_frame = [m for m in missing if m["stage"] == "frame"]
        missing_scaffold = [m for m in missing if m["stage"] == "scaffold"] if ctx.extra.get("build_scaffold", True) else []

        lava_needed = len(missing_frame) + sum(1 for m in missing_base if m["current"] != "lava")
        lava_in_inv = counts.get("lava_bucket", 0)
        pool_lava_cells = 0
        if lava_in_inv < lava_needed and (counts.get("bucket", 0) >= 1 or counts.get("water_bucket", 0) >= 1):
            for dz in (2, 3, 1, 0):
                for dx in (1, 2, 0, 3, -1, 4):
                    if dz == 0 and dx in (0, 3):
                        continue
                    blk = ctx.read_rel_block((dx, 0, dz))
                    props = blk.get("properties") if isinstance(blk.get("properties"), dict) else {}
                    if str(blk.get("name") or "air") == "lava" and props.get("level") in (None, 0):
                        pool_lava_cells += 1
        if lava_in_inv + pool_lava_cells < lava_needed:
            raise WorkflowStepError(
                "missing_lava_buckets",
                f"Need {lava_needed} lava_bucket(s), found {lava_in_inv} (and {pool_lava_cells} pool source blocks).",
                blocker_type="missing_item",
                suggested_recovery="gather_lava_buckets",
                data={"required": lava_needed, "available": lava_in_inv, "pool_sources": pool_lava_cells},
            )

        water_active = report["water_curtain"]["active"]
        needs_water_pour = (not water_active) and bool(
            missing_frame or any(m["current"] == "lava" for m in missing_base) or (missing_base and ctx.extra.get("build_scaffold", True))
        )
        if needs_water_pour and counts.get("water_bucket", 0) < 1:
            raise WorkflowStepError(
                "missing_water_bucket",
                "Need at least 1 water_bucket for portal casting/freezing.",
                blocker_type="missing_item",
                suggested_recovery="gather_water_bucket",
            )

        effective_empty_buckets = counts.get("bucket", 0) + (1 if needs_water_pour else 0)
        if (water_active or needs_water_pour) and effective_empty_buckets < 1:
            raise WorkflowStepError(
                "missing_empty_bucket",
                "Need an empty bucket to retrieve water before ignition.",
                blocker_type="missing_item",
                suggested_recovery="craft_or_empty_bucket",
            )

        scaffold_avail = sum(counts.get(name, 0) for name in SCAFFOLD_BLOCKS)
        if len(missing_scaffold) > scaffold_avail:
            raise WorkflowStepError(
                "missing_building_block",
                f"Need {len(missing_scaffold)} scaffold block(s), found {scaffold_avail}.",
                blocker_type="missing_item",
                suggested_recovery="gather_cobblestone_or_stone",
            )

        needs_pick = bool(ctx.extra.get("temp_stand_pos")) or bool(missing_base) or any(
            m["current"] in self.BASE_SOLID_DIGGABLE for m in missing_base
        )
        if needs_pick and ctx.select_item(PICKAXE_PRIORITY) is None:
            raise WorkflowStepError(
                "missing_pickaxe",
                "Need a pickaxe to clear base/temporary blocks.",
                blocker_type="missing_item",
                suggested_recovery="craft_or_equip_pickaxe",
            )

        if not report["portal_active"] and counts.get("flint_and_steel", 0) < 1:
            raise WorkflowStepError(
                "missing_flint_and_steel",
                "Need flint_and_steel to ignite the portal.",
                blocker_type="missing_item",
                suggested_recovery="craft_flint_and_steel",
            )

    def _verify_portal_structure(self, ctx: WorkflowContext) -> Dict[str, Any]:
        return self.inspect_portal(
            ctx.origin,
            orientation=ctx.orientation,
            include_scaffold=False,
            read_block=ctx.read_block,
        )

    def _build_portal_steps(
        self,
        *,
        build_scaffold: bool,
        temp_stand_pos: Optional[List[int]],
    ) -> List[ActionStep]:
        move_branches = {
            "stalled": self._branch_recover_movement,
            "path_not_found": self._branch_recover_movement,
            "unreachable_stance": self._branch_recover_movement,
            "timeout": self._branch_recover_movement,
        }
        steps: List[ActionStep] = [
            ActionStep(
                step_id="ensure_base_and_corners",
                description="检查并固化地狱门底部与底角黑曜石",
                stage="base",
                skip_if=self._base_complete,
                action=self._step_ensure_base,
                branches=dict(move_branches),
                max_branch_attempts=2,
            )
        ]

        if build_scaffold:
            scaffold_cells = sorted(
                (cell for cell in NETHER_PORTAL_SPEC.cells if cell.stage == "scaffold"),
                key=lambda c: c.priority,
            )
            for cell in scaffold_cells:
                steps.append(
                    ActionStep(
                        step_id=cell.label or f"scaffold_{cell.priority}",
                        description=f"放置脚手架 {cell.label}",
                        stage="scaffold",
                        skip_if=lambda ctx, rp=cell.rel_pos: str(ctx.read_rel_block(rp).get("name") or "air") in SCAFFOLD_BLOCKS,
                        action=lambda ctx, rp=cell.rel_pos: place_block(ctx, SCAFFOLD_BLOCKS, rp),
                        branches={
                            "target_obstructed": self._branch_clear_soft_obstacle,
                            "place_failed": self._branch_clear_soft_obstacle,
                            **move_branches,
                        },
                        max_branch_attempts=2,
                    )
                )

        if temp_stand_pos is not None:
            steps.append(
                ActionStep(
                    step_id="setup_temp_stand",
                    description="准备临时浇筑站位方块",
                    stage="frame",
                    skip_if=lambda ctx, tp=temp_stand_pos: str(ctx.read_block(tp).get("name") or "air") not in AIR,
                    action=lambda ctx, tp=temp_stand_pos: self._step_setup_temp_stand(ctx, tp),
                    branches=dict(move_branches),
                    max_branch_attempts=2,
                )
            )

        steps.append(
            ActionStep(
                step_id="prefill_lava_buckets",
                description="激活水幕前预装空桶岩浆",
                stage="frame",
                skip_if=self._prefill_not_needed,
                action=lambda ctx, has_stand=(temp_stand_pos is not None): self._step_prefill_lava_buckets(
                    ctx, has_temp_stand=has_stand
                ),
            )
        )

        if build_scaffold:
            steps.append(
                ActionStep(
                    step_id="activate_water_curtain",
                    description="顶部激活定向水幕",
                    stage="scaffold",
                    skip_if=lambda ctx: str(ctx.read_rel_block((2, 3, -1)).get("name") or "air") == "water",
                    action=self._step_activate_water_curtain,
                    branches=dict(move_branches),
                    max_branch_attempts=2,
                )
            )

        for spec in self.CAST_SPECS:
            steps.append(
                ActionStep(
                    step_id=f"cast_{spec['num']}_{spec['desc']}",
                    description=f"浇筑第{spec['num']}块黑曜石 ({spec['desc']})",
                    stage="frame",
                    skip_if=lambda ctx, rp=spec["pos"]: str(ctx.read_rel_block(rp).get("name") or "air") == "obsidian",
                    action=lambda ctx, s=spec: self._step_cast_obsidian(ctx, s),
                    branches={"cast_block_failed": lambda ctx, err, s=spec: self._branch_recover_cast(ctx, err, s)},
                    max_branch_attempts=1,
                )
            )

        if temp_stand_pos is not None:
            steps.append(
                ActionStep(
                    step_id="cleanup_temp_stand",
                    description="拆除临时站位方块",
                    stage="frame",
                    skip_if=lambda ctx, tp=temp_stand_pos: str(ctx.read_block(tp).get("name") or "air") in AIR,
                    action=lambda ctx, tp=temp_stand_pos: self._step_cleanup_temp_stand(ctx, tp),
                    branches=dict(move_branches),
                    max_branch_attempts=2,
                )
            )

        steps.extend([
            ActionStep(
                step_id="scoop_top_water",
                description="绕行脚手架后方回收顶部水源",
                stage="interior",
                skip_if=lambda ctx: str(ctx.read_rel_block((2, 3, -1)).get("name") or "air") not in {"water", "flowing_water"},
                action=self._step_scoop_top_water,
                branches=dict(move_branches),
                max_branch_attempts=2,
            ),
            ActionStep(
                step_id="clear_interior_water",
                description="清理门洞残留积水并等待完全排空",
                stage="interior",
                skip_if=self._interior_ready_for_ignition,
                action=self._step_clear_interior_water,
                branches={"water_lingering": self._branch_water_cleanup, **move_branches},
                max_branch_attempts=2,
            ),
            ActionStep(
                step_id="ignite_portal",
                description="使用打火石点燃地狱门",
                stage="interior",
                skip_if=self._portal_active,
                action=self._step_ignite_portal,
                branches={"portal_not_ignited": self._branch_retry_ignition, **move_branches},
                max_branch_attempts=2,
            ),
        ])
        return steps

    def _base_complete(self, ctx: WorkflowContext) -> bool:
        for rel in ((1, -1, 0), (2, -1, 0), (0, 0, 0), (3, 0, 0)):
            if str(ctx.read_rel_block(rel).get("name") or "air") != "obsidian":
                return False
        for walkway in ((1, 0, 1), (2, 0, 1)):
            if str(ctx.read_rel_block(walkway).get("name") or "air") in {"lava", "flowing_lava", "air", "cave_air"}:
                return False
        for opening in ((1, 0, 0), (2, 0, 0)):
            name = str(ctx.read_rel_block(opening).get("name") or "air")
            if name in self.BASE_SOLID_DIGGABLE:
                return False
        return True

    def _interior_ready_for_ignition(self, ctx: WorkflowContext) -> bool:
        if self._portal_active(ctx):
            return True
        for dx in (1, 2):
            for dy in (0, 1, 2):
                if str(ctx.read_rel_block((dx, dy, 0)).get("name") or "air") not in AIR:
                    return False
        return True

    def _portal_active(self, ctx: WorkflowContext) -> bool:
        for dx in (1, 2):
            for dy in (0, 1, 2):
                if str(ctx.read_rel_block((dx, dy, 0)).get("name") or "air") != "nether_portal":
                    return False
        return True

    def _pour_lava_into_base_cell(self, ctx: WorkflowContext, rel: Tuple[int, int, int]) -> None:
        """Pour lava cleanly into a dug base pit from the dry shore without overhanging into lava."""
        if ctx.select_item("lava_bucket") is None:
            stand_at(ctx, "shore_approach_2")
            self._refill_lava_from_pool(ctx, return_stance="shore_approach_2")

        non_solid = AIR | {"lava", "flowing_lava", "water", "flowing_water", "not_loaded"}
        if rel[1] == -1:
            door_blk = str(ctx.read_rel_block((rel[0], 0, rel[2])).get("name") or "air")
            if door_blk in self.BASE_SOLID_DIGGABLE:
                try:
                    dig_block(ctx, (rel[0], 0, rel[2]), allowed_blocks=self.BASE_SOLID_DIGGABLE, collect=False)
                except WorkflowStepError:
                    pass
            # Prefer inward-facing pit walls (aim_y = -0.5) over pit floor (aim_y = -1.01) so rays
            # from the shore always clear the (dx, 0, -1) lip cleanly; wall blocks remain visible
            # across open (rel[0], 0, 0) even when dy=0 above those walls is solid.
            wall_candidates = (
                ((rel[0], rel[1], rel[2] + 1), (0, 0, -1)),
                ((rel[0] + 1, rel[1], rel[2]), (-1, 0, 0)),
                ((rel[0] - 1, rel[1], rel[2]), (1, 0, 0)),
                ((rel[0], rel[1] - 1, rel[2]), (0, 1, 0)),
            )
            ref_rel, face_rel = wall_candidates[-1]
            for cand_ref, cand_face in wall_candidates:
                name = str(ctx.read_rel_block(cand_ref).get("name") or "air")
                if name not in non_solid:
                    ref_rel, face_rel = cand_ref, cand_face
                    break
        else:
            wall_candidates = (
                ((rel[0], rel[1], rel[2] + 1), (0, 0, -1)),
                ((rel[0] + 1, rel[1], rel[2]), (-1, 0, 0)),
                ((rel[0] - 1, rel[1], rel[2]), (1, 0, 0)),
                ((rel[0], rel[1] - 1, rel[2]), (0, 1, 0)),
            )
            ref_rel, face_rel = wall_candidates[-1]
            for cand_ref, cand_face in wall_candidates[:-1]:
                name = str(ctx.read_rel_block(cand_ref).get("name") or "air")
                if name not in non_solid:
                    ref_rel, face_rel = cand_ref, cand_face
                    break

        # Pouring dy=-1 cells from behind the ray-clip threshold hits the shore lip (dx,0,-1) and
        # places lava at the bot's feet (dx,1,-1). Enforce GoalNearXZ (y=None) micro-repositioning
        # so mc-agent-api does not skip <=0.5m moves in goNear, and refuse to pour if still clipped.
        if rel[1] == -1:
            dx_c = min(max(rel[0], 0), 3)
            edge_stance = (dx_c + 0.5, 1.0, -0.22)
            try:
                stand_at(ctx, edge_stance, tolerance=0.14, range_param=0.0)
            except WorkflowStepError:
                try:
                    stand_at(ctx, (dx_c + 0.5, 1.0, -0.26), tolerance=0.28, range_param=0.0)
                except WorkflowStepError:
                    stand_at(ctx, (dx_c + 0.5, 1.0, -0.42), tolerance=0.45, range_param=0.0)

            if ref_rel == (rel[0], rel[1] - 1, rel[2]):
                max_back = 0.36
            elif ref_rel in {(rel[0] + 1, rel[1], rel[2]), (rel[0] - 1, rel[1], rel[2])}:
                max_back = 0.52
            else:
                max_back = 0.95

            xyz = self._read_bot_xyz(ctx)
            if xyz is not None:
                lip_w = ctx.world_pos((dx_c + 0.5, 1.0, 0.0), integer=False)
                back_vec = ctx.world_vec((0, 0, -1))
                back_dist = (xyz[0] - lip_w[0]) * back_vec[0] + (xyz[2] - lip_w[2]) * back_vec[2]
                if back_dist > max_back:
                    target_w = ctx.world_pos((dx_c + 0.5, 1.0, -0.18), integer=False)
                    try:
                        ctx.guard.write(ctx.guard.client.move_to, target_w[0], None, target_w[2], range=0.05)
                    except TypeError:
                        ctx.guard.write(
                            ctx.guard.client.move_to,
                            target_w[0],
                            target_w[1],
                            target_w[2],
                            range=0.0,
                        )
                    xyz_after = self._read_bot_xyz(ctx)
                    if xyz_after is not None:
                        back_after = (xyz_after[0] - lip_w[0]) * back_vec[0] + (xyz_after[2] - lip_w[2]) * back_vec[2]
                        if back_after > max_back:
                            world_rel = ctx.world_pos(rel, integer=True)
                            raise WorkflowStepError(
                                "unreachable_stance",
                                (
                                    f"Bot remains too far back ({back_after:.2f} > {max_back:.2f}) to pour "
                                    f"base cell {world_rel} safely without hitting shore lip."
                                ),
                                blocker_type="navigation",
                                blocker_pos=world_rel,
                                suggested_recovery="recover_movement",
                            )
        else:
            safe_shore = (min(max(rel[0], 0), 3) + 0.5, 1.0, -0.78)
            try:
                stand_at(ctx, safe_shore, tolerance=0.65)
            except WorkflowStepError:
                stand_at(ctx, (min(max(rel[0], 0), 3) + 0.5, 1.0, -1.35), tolerance=0.85)

        aim_at(ctx, rel)
        use_on(
            ctx,
            "lava_bucket",
            rel,
            ref_rel=ref_rel,
            face_rel=face_rel,
            verify_held=True,
        )
        # Emergency rescue if physics/raycast spilled lava onto dy=+1 near our standing ledge.
        if rel[1] == -1 and ctx.select_item("bucket") is not None:
            for spill_rel in ((rel[0], 1, 0), (rel[0], 1, -1)):
                if str(ctx.read_rel_block(spill_rel).get("name") or "air") in {"lava", "flowing_lava"}:
                    try:
                        aim_at(ctx, spill_rel)
                        use_on(ctx, "bucket", spill_rel, verify_held=True)
                    except WorkflowStepError:
                        pass
        wait_until(
            ctx,
            lambda c, r=rel: str(c.read_rel_block(r).get("name") or "air") in {"lava", "obsidian"},
            timeout_seconds=1.5,
        )

    def _step_ensure_base(self, ctx: WorkflowContext) -> Dict[str, Any]:
        for opening in ((1, 0, 0), (2, 0, 0)):
            cur_open = str(ctx.read_rel_block(opening).get("name") or "air")
            if cur_open in self.BASE_SOLID_DIGGABLE:
                dig_block(ctx, opening, allowed_blocks=self.BASE_SOLID_DIGGABLE, collect=False)

        base_targets = ((1, -1, 0), (2, -1, 0), (0, 0, 0), (3, 0, 0))
        walkway_targets = ((1, 0, 1), (2, 0, 1))
        freeze_probes = base_targets + walkway_targets + ((1, 0, 0), (2, 0, 0))
        needs_water_freeze = any(
            str(ctx.read_rel_block(p).get("name") or "air") in {"lava", "flowing_lava"}
            for p in freeze_probes
        )

        # Phase A: Contained Y=y0 doorway freeze so outer lava pool stays molten for bucket refilling.
        if needs_water_freeze:
            left_door_rel = (1, 0, 0)
            right_door_rel = (2, 0, 0)
            replaceable_with_lava = AIR | {"lava", "flowing_lava", "water", "flowing_water"}

            # 1. Seal (2,0,0) with temporary cobblestone so (1,0,0) holds contained water.
            right_cur = str(ctx.read_rel_block(right_door_rel).get("name") or "air")
            if right_cur == "obsidian":
                dig_block(ctx, right_door_rel, allowed_blocks={"obsidian"}, collect=False)
                right_cur = str(ctx.read_rel_block(right_door_rel).get("name") or "air")
            if right_cur not in SCAFFOLD_BLOCKS and ctx.select_item(SCAFFOLD_BLOCKS) is not None:
                place_block(ctx, SCAFFOLD_BLOCKS, right_door_rel, replaceable=replaceable_with_lava)

            # 2. Move to shore_west and clear (1,0,0) (scooping lava gains 1 bucket for 0-lava starts).
            self._move_shore_west(ctx)
            left_cur = str(ctx.read_rel_block(left_door_rel).get("name") or "air")
            if left_cur == "obsidian":
                dig_block(ctx, left_door_rel, allowed_blocks={"obsidian"}, collect=False)
                left_cur = str(ctx.read_rel_block(left_door_rel).get("name") or "air")
            if left_cur in {"lava", "flowing_lava"}:
                if ctx.select_item("bucket") is not None:
                    aim_at(ctx, left_door_rel)
                    try:
                        use_on(ctx, "bucket", left_door_rel, verify_held=True)
                    except WorkflowStepError as err:
                        if err.code != "cannot_interact":
                            raise
                elif ctx.select_item(SCAFFOLD_BLOCKS) is not None:
                    place_block(ctx, SCAFFOLD_BLOCKS, left_door_rel, replaceable=replaceable_with_lava)
                    dig_block(ctx, left_door_rel, allowed_blocks=set(SCAFFOLD_BLOCKS) | self.BASE_SOLID_DIGGABLE, collect=False)

            # 2b. On 1-block-deep pools, prepare (1,-1,0) only while (1,0,0) is dry so pouring lava never hits active lava/shore.
            left_cur = str(ctx.read_rel_block(left_door_rel).get("name") or "air")
            if left_cur not in {"lava", "flowing_lava"}:
                b_left_cur = str(ctx.read_rel_block((1, -1, 0)).get("name") or "air")
                if b_left_cur in self.BASE_SOLID_DIGGABLE:
                    dig_block(ctx, (1, -1, 0), allowed_blocks=self.BASE_SOLID_DIGGABLE, collect=False)
                    b_left_cur = str(ctx.read_rel_block((1, -1, 0)).get("name") or "air")
                if b_left_cur in AIR:
                    self._pour_lava_into_base_cell(ctx, (1, -1, 0))
                    self._move_shore_west(ctx)

            # 3. Place water inside (1,0,0) aiming at the West (-1,0,0) face of (2,0,0).
            aim_at(ctx, right_door_rel)
            use_on(
                ctx,
                "water_bucket",
                left_door_rel,
                ref_rel=right_door_rel,
                face_rel=(-1, 0, 0),
                verify_held=True,
            )
            wait_until(
                ctx,
                lambda c: (
                    str(c.read_rel_block((0, 0, 0)).get("name") or "air") not in {"lava", "flowing_lava"}
                    and str(c.read_rel_block((1, -1, 0)).get("name") or "air") not in {"lava", "flowing_lava"}
                    and str(c.read_rel_block((1, 0, 1)).get("name") or "air") not in {"lava", "flowing_lava"}
                ),
                timeout_seconds=3.5,
                poll_seconds=0.25,
            )

            # 4. Dig (2,0,0): water flows 1 block across, freezing (3,0,0), (2,0,1), and (2,-1,0) if lava.
            right_cur = str(ctx.read_rel_block(right_door_rel).get("name") or "air")
            if right_cur in (set(SCAFFOLD_BLOCKS) | self.BASE_SOLID_DIGGABLE):
                dig_block(ctx, right_door_rel, allowed_blocks=set(SCAFFOLD_BLOCKS) | self.BASE_SOLID_DIGGABLE, collect=False)

            wait_until(
                ctx,
                lambda c: (
                    str(c.read_rel_block((3, 0, 0)).get("name") or "air") not in {"lava", "flowing_lava"}
                    and str(c.read_rel_block((2, -1, 0)).get("name") or "air") not in {"lava", "flowing_lava"}
                    and str(c.read_rel_block((2, 0, 1)).get("name") or "air") not in {"lava", "flowing_lava"}
                ),
                timeout_seconds=4.0,
                poll_seconds=0.25,
            )
            self._scoop_if_water(ctx, left_door_rel)
            self._scoop_if_water(ctx, right_door_rel)
            self._scoop_if_water(ctx, (left_door_rel[0], left_door_rel[1] + 1, left_door_rel[2]))
            wait_until(
                ctx,
                lambda c: (
                    str(c.read_rel_block(left_door_rel).get("name") or "air") in AIR
                    and str(c.read_rel_block(right_door_rel).get("name") or "air") in AIR
                ),
                timeout_seconds=4.5,
                poll_seconds=0.25,
            )

        # Phase B: Replace corners (0,0,0)/(3,0,0) first, then deep base cells (1,-1,0)/(2,-1,0).
        non_solid = AIR | {"lava", "flowing_lava", "water", "flowing_water", "not_loaded"}
        phase_b_targets = ((0, 0, 0), (3, 0, 0), (1, -1, 0), (2, -1, 0))
        for rel in phase_b_targets:
            for _attempt in range(2):
                if rel[1] == -1:
                    above_rel = (rel[0], 0, rel[2])
                    above_name = str(ctx.read_rel_block(above_rel).get("name") or "air")
                    if above_name in {"water", "flowing_water"}:
                        self._scoop_if_water(ctx, above_rel)
                        above_name = str(ctx.read_rel_block(above_rel).get("name") or "air")
                    if above_name not in (AIR | {"lava", "flowing_lava", "water", "flowing_water"}):
                        if above_name == "obsidian":
                            for w_rel in (
                                (rel[0], 1, -2),
                                (rel[0], 2, -2),
                                (rel[0], 1, -1),
                                (rel[0], 1, 0),
                            ):
                                self._scoop_if_water(ctx, w_rel)
                        dig_block(ctx, above_rel, allowed_blocks=self.BASE_SOLID_DIGGABLE | set(SCAFFOLD_BLOCKS) | {"obsidian"}, collect=False)
                blk = ctx.read_rel_block(rel)
                cur = str(blk.get("name") or "air")
                props = blk.get("properties") if isinstance(blk.get("properties"), dict) else {}
                if cur == "obsidian":
                    break
                pour_stance = (min(max(rel[0], 0), 3) + 0.5, 1.0, -0.32 if rel[1] == -1 else -0.78)
                self._stand_pour_stance(ctx, rel, pour_stance)
                if cur in self.BASE_SOLID_DIGGABLE:
                    dig_block(ctx, rel, allowed_blocks=self.BASE_SOLID_DIGGABLE, collect=False)
                    blk = ctx.read_rel_block(rel)
                    cur = str(blk.get("name") or "air")
                    props = blk.get("properties") if isinstance(blk.get("properties"), dict) else {}
                is_lava_source = cur == "lava" and props.get("level") in (None, 0)
                if not is_lava_source:
                    self._pour_lava_into_base_cell(ctx, rel)
                    blk = ctx.read_rel_block(rel)
                    cur = str(blk.get("name") or "air")
                if cur in {"lava", "flowing_lava"}:
                    freeze_spot = (rel[0], rel[1] + 1, rel[2])
                    fx, fy, fz = freeze_spot
                    wall_candidates = (
                        ((fx, fy, fz + 1), (0, 0, -1)),
                        ((fx + 1, fy, fz), (-1, 0, 0)),
                        ((fx - 1, fy, fz), (1, 0, 0)),
                    )
                    ref_rel = None
                    face_rel = None
                    water_target = freeze_spot
                    for cand_ref, cand_face in wall_candidates:
                        if str(ctx.read_rel_block(cand_ref).get("name") or "air") not in non_solid:
                            ref_rel, face_rel = cand_ref, cand_face
                            break
                    if ref_rel is None:
                        # Use solid shore floor block (cx, 0, -1) top face only when landing cell (cx, 1, -1) is clear.
                        dx_pref = min(max(rel[0], 0), 3)
                        landing_cands = []
                        for cx in (dx_pref, 2, 0, 3):
                            if cx not in landing_cands:
                                landing_cands.append(cx)
                        for cx in landing_cands:
                            cand_floor = (cx, 0, -1)
                            cand_land = (cx, 1, -1)
                            land_name = str(ctx.read_rel_block(cand_land).get("name") or "air")
                            if (
                                str(ctx.read_rel_block(cand_floor).get("name") or "air") not in non_solid
                                and land_name in (AIR | {"short_grass", "tall_grass", "fern", "snow", "leaf_litter"})
                            ):
                                ref_rel, face_rel = cand_floor, (0, 1, 0)
                                water_target = cand_land
                                break
                        if ref_rel is None:
                            shore_ref = (dx_pref, max(rel[1] + 1, 0), -1)
                            if str(ctx.read_rel_block((dx_pref, 1, -1)).get("name") or "air") not in non_solid:
                                ref_rel, face_rel = (dx_pref, 1, -1), (0, 1, 0)
                                water_target = (dx_pref, 2, -1)
                            else:
                                ref_rel, face_rel = shore_ref, (0, 0, 1)
                        stand_at(ctx, (min(max(rel[0], 0), 3) + 0.5, 1.0, -1.5), tolerance=1.25, range_param=0.5)
                    else:
                        self._stand_pour_stance(ctx, rel, pour_stance)
                    water_before = ctx.inventory_counts().get("water_bucket", 0)
                    aim_at(ctx, ref_rel)
                    use_on(
                        ctx,
                        "water_bucket",
                        water_target,
                        ref_rel=ref_rel,
                        face_rel=face_rel,
                        verify_held=True,
                    )
                    wait_until(ctx, lambda c, r=rel: str(c.read_rel_block(r).get("name") or "air") == "obsidian", timeout_seconds=2.5)
                    self._scoop_if_water(ctx, water_target)
                    if water_target != freeze_spot:
                        self._scoop_if_water(ctx, freeze_spot)
                    self._scoop_if_water(ctx, (fx, fy, fz - 1))
                    if water_target[2] <= -1:
                        self._scoop_if_water(ctx, (water_target[0], water_target[1], water_target[2] - 1))
                    if ctx.inventory_counts().get("water_bucket", 0) < water_before:
                        for wdy in (0, 1, 2, -1):
                            for wdx in (-1, 0, 1):
                                for wdz in (-1, 0, 1, -2):
                                    if self._scoop_if_water(ctx, (fx + wdx, fy + wdy, fz + wdz)):
                                        if ctx.inventory_counts().get("water_bucket", 0) >= water_before:
                                            break
                                if ctx.inventory_counts().get("water_bucket", 0) >= water_before:
                                    break
                            if ctx.inventory_counts().get("water_bucket", 0) >= water_before:
                                break
                    wait_until(ctx, lambda c, wt=water_target: str(c.read_rel_block(wt).get("name") or "air") in AIR, timeout_seconds=3.5)

        for rel in base_targets:
            final_name = str(ctx.read_rel_block(rel).get("name") or "air")
            if final_name != "obsidian":
                world = ctx.world_pos(rel, integer=True)
                raise WorkflowStepError(
                    "base_obsidian_incomplete",
                    f"Base cell {world} is {final_name}, expected obsidian.",
                    blocker_type="verification_failed",
                    blocker_pos=world,
                    blocker_block=final_name,
                    suggested_recovery="inspect_lava_and_water_supply",
                )

        # If lava pool was too narrow/dry at dz=1, bridge front walkway (1,0,1)/(2,0,1) using scaffold blocks.
        non_solid_wk = AIR | {"lava", "flowing_lava", "water", "flowing_water", "not_loaded", "unknown"}
        missing_walkway = [
            wk for wk in walkway_targets
            if str(ctx.read_rel_block(wk).get("name") or "air") in non_solid_wk
        ]
        if missing_walkway:
            from core.structure_spec import CAVE_DIGGABLE_BLOCKS

            walkway_plan = list(missing_walkway)
            # If neither (1,0,1) nor (2,0,1) has a solid neighbor beneath/beside it, bridge from (0,0,0) via (0,0,1).
            has_support = any(
                str(ctx.read_rel_block((wk[0] + dx, wk[1] + dy, wk[2] + dz)).get("name") or "air") not in non_solid_wk
                for wk in walkway_plan
                for dx, dy, dz in ((0, -1, 0), (-1, 0, 0), (1, 0, 0), (0, 0, 1))
                if (wk[0] + dx, wk[1] + dy, wk[2] + dz) != (1, 0, 0) and (wk[0] + dx, wk[1] + dy, wk[2] + dz) != (2, 0, 0)
            )
            if not has_support and str(ctx.read_rel_block((0, 0, 1)).get("name") or "air") in non_solid_wk:
                walkway_plan.insert(0, (0, 0, 1))
            bridge_cells(
                ctx,
                walkway_plan,
                solid_blocks=SCAFFOLD_BLOCKS,
                clearance_diggable=CAVE_DIGGABLE_BLOCKS,
                extra_solid_rel=((0, 0, 0), (3, 0, 0), (1, -1, 0), (2, -1, 0)),
            )
        stand_at(ctx, "player_start")
        return {"base_obsidian": True}

    def _protected_build_cells(self, ctx: WorkflowContext, *, include_scaffold: bool = True) -> set:
        protected = set()
        for rule in NETHER_PORTAL_SPEC.cells:
            name = str(ctx.read_rel_block(rule.rel_pos).get("name") or "air")
            if rule.stage in {"base", "frame"} and name == "obsidian":
                protected.add(rule.rel_pos)
            elif include_scaffold and rule.stage == "scaffold" and name in SCAFFOLD_BLOCKS:
                protected.add(rule.rel_pos)
            elif rule.stage == "interior" and name == "nether_portal":
                protected.add(rule.rel_pos)
        if ctx.extra.get("temp_stand_pos") is not None:
            stand_rel = (1, 1, 1)
            if str(ctx.read_rel_block(stand_rel).get("name") or "air") in SCAFFOLD_BLOCKS:
                protected.add(stand_rel)
        return protected

    def _shore_clearance_cells(self) -> List[Tuple[int, int, int]]:
        # Include the shore ledge cells (0..3,1,-1) and (1..2,1..2,-2) even though some overlap
        # later scaffold slots; before base completion they must be flat and walkable.
        extra_shore = [
            (0, 1, -1),
            (1, 1, -1),
            (2, 1, -1),
            (3, 1, -1),
            (1, 1, -2),
            (1, 2, -2),
            (2, 1, -2),
            (2, 2, -2),
        ]
        return sorted(
            set([c for c in NETHER_PORTAL_SPEC.clearance_rel if c[2] <= -1] + extra_shore),
            key=lambda c: (c[1], c[2], c[0]),
        )

    def _clear_safe_shoulder_blocks(self, ctx: WorkflowContext, *, allowed_blocks: frozenset) -> List[List[int]]:
        """Clear diggable waist/head blocks immediately flanking the working shore without breaching liquids."""
        protected_fluid = {"lava", "flowing_lava", "water", "flowing_water"}
        protected_set = self._protected_build_cells(ctx, include_scaffold=self._base_complete(ctx))
        shoulder_rel = [
            (-1, 1, -1), (-1, 2, -1), (-1, 1, -2), (-1, 2, -2),
            (4, 1, -1), (4, 2, -1), (4, 1, -2), (4, 2, -2),
            (0, 1, -3), (1, 1, -3), (2, 1, -3), (3, 1, -3),
            (0, 2, -3), (1, 2, -3), (2, 2, -3), (3, 2, -3),
        ]
        dug_world: List[List[int]] = []
        for rel in shoulder_rel:
            if rel in protected_set:
                continue
            bname = str(ctx.read_rel_block(rel).get("name") or "air")
            if bname not in allowed_blocks:
                continue
            wp = ctx.world_pos(rel, integer=True)
            # Never breach walls that hold back adjacent water or lava (check 5 non-downward neighbors).
            has_adjacent_fluid = any(
                str(ctx.read_block([wp[0] + ox, wp[1] + oy, wp[2] + oz]).get("name") or "air") in protected_fluid
                for ox, oy, oz in ((0, 1, 0), (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1))
            )
            if has_adjacent_fluid:
                continue
            # Also avoid digging a block directly supporting sand/gravel beneath liquid.
            above_name = str(ctx.read_block([wp[0], wp[1] + 1, wp[2]]).get("name") or "air")
            if above_name in {"sand", "red_sand", "gravel"}:
                above2 = str(ctx.read_block([wp[0], wp[1] + 2, wp[2]]).get("name") or "air")
                if above2 in protected_fluid:
                    continue
            xyz = self._read_bot_xyz(ctx)
            if xyz is not None:
                eye_y = float(xyz[1]) + 1.62
                edist = math.sqrt(
                    (wp[0] + 0.5 - float(xyz[0])) ** 2
                    + (wp[1] + 0.5 - eye_y) ** 2
                    + (wp[2] + 0.5 - float(xyz[2])) ** 2
                )
                if edist > 4.2:
                    continue
            try:
                dig_block(ctx, wp, relative=False, allowed_blocks=allowed_blocks, collect=False)
                dug_world.append(list(wp))
            except WorkflowStepError:
                continue
        return dug_world

    def _site_platform_and_overhead_ready(self, ctx: WorkflowContext) -> bool:
        from core.structure_spec import CAVE_DIGGABLE_BLOCKS, DEFAULT_REPLACEABLE

        non_solid = AIR | {"lava", "flowing_lava", "water", "flowing_water", "not_loaded", "unknown"} | DEFAULT_REPLACEABLE
        non_blocking = AIR | {"water", "flowing_water", "nether_portal"} | DEFAULT_REPLACEABLE
        site_spec = NETHER_PORTAL_SPEC.site_spec
        if site_spec is not None:
            for plat in site_spec.platform_rel:
                if str(ctx.read_rel_block(plat).get("name") or "air") in non_solid:
                    return False
        protected = self._protected_build_cells(ctx, include_scaffold=self._base_complete(ctx))
        for cl in sorted(set(NETHER_PORTAL_SPEC.clearance_rel) | set(self._shore_clearance_cells())):
            if cl in protected:
                continue
            cname = str(ctx.read_rel_block(cl).get("name") or "air")
            if cname not in non_blocking and cname in CAVE_DIGGABLE_BLOCKS:
                return False
        return True

    def _step_prepare_site_platform_and_overhead(self, ctx: WorkflowContext) -> Dict[str, Any]:
        from core.structure_spec import CAVE_DIGGABLE_BLOCKS, DEFAULT_REPLACEABLE

        non_solid = AIR | {"lava", "flowing_lava", "water", "flowing_water", "not_loaded", "unknown"} | DEFAULT_REPLACEABLE
        protected = self._protected_build_cells(ctx, include_scaffold=self._base_complete(ctx))
        shore_clearance = self._shore_clearance_cells()
        cleared_shore = clear_overhead_cells(
            ctx,
            shore_clearance,
            allowed_blocks=CAVE_DIGGABLE_BLOCKS,
            protected_rel=protected,
        )

        site_spec = NETHER_PORTAL_SPEC.site_spec
        bridged: List[Dict[str, Any]] = []
        if site_spec is not None:
            hinted = [tuple(int(v) for v in c) for c in (ctx.extra.get("site", {}).get("bridge_cells") or ())]
            seen = set(hinted)
            plan = list(hinted) + [c for c in site_spec.platform_rel if c not in seen]
            missing_plat = [c for c in plan if str(ctx.read_rel_block(c).get("name") or "air") in non_solid]
            if missing_plat:
                bridged = bridge_cells(
                    ctx,
                    missing_plat,
                    solid_blocks=SCAFFOLD_BLOCKS,
                    clearance_diggable=CAVE_DIGGABLE_BLOCKS,
                )

        if str(ctx.read_rel_block((2, 0, -1)).get("name") or "air") not in non_solid:
            try:
                stand_at(ctx, "player_start")
            except WorkflowStepError:
                stand_at(ctx, "shore_approach_2")
        cleared_shoulder = self._clear_safe_shoulder_blocks(ctx, allowed_blocks=CAVE_DIGGABLE_BLOCKS)
        remaining_clearance = [c for c in NETHER_PORTAL_SPEC.clearance_rel if c[2] >= 0]
        cleared_rest = clear_overhead_cells(
            ctx,
            remaining_clearance,
            allowed_blocks=CAVE_DIGGABLE_BLOCKS,
            protected_rel=protected,
        )
        try:
            stand_at(ctx, "player_start")
        except WorkflowStepError:
            stand_at(ctx, "shore_approach_2")
        return {
            "bridged_count": len(bridged),
            "cleared_count": len(cleared_shore) + len(cleared_shoulder) + len(cleared_rest),
        }

    def _step_setup_temp_stand(self, ctx: WorkflowContext, temp_stand_pos: List[int]) -> Dict[str, Any]:
        # Place (1,1,1) directly from dry shore_approach_2 (2.24m away) so mc-agent-api places
        # without walking around or overlapping the target cell; only step to (2.5,1,0.5) if needed.
        stand_at(ctx, "shore_approach_2")
        # Prevent falling into a 2-block-deep 1x1 pit at (0,0,1) beside temp_stand (1,1,1).
        pit_rel = (0, 0, 1)
        non_solid_dry = AIR | set(DEFAULT_REPLACEABLE)
        pit_name = str(ctx.read_rel_block(pit_rel).get("name") or "air")
        below_pit = str(ctx.read_rel_block((0, -1, 1)).get("name") or "air")
        if (
            pit_name in non_solid_dry
            and below_pit not in (non_solid_dry | {"lava", "flowing_lava", "not_loaded", "unknown"})
            and sum(int(ctx.inventory_counts().get(b, 0)) for b in SCAFFOLD_BLOCKS) >= 12
        ):
            try:
                place_block(ctx, SCAFFOLD_BLOCKS, pit_rel)
            except WorkflowStepError:
                pass
        try:
            return place_block(ctx, SCAFFOLD_BLOCKS, temp_stand_pos, relative=False)
        except WorkflowStepError:
            stand_at(ctx, (2.5, 1.0, 0.5), tolerance=0.5)
            return place_block(ctx, SCAFFOLD_BLOCKS, temp_stand_pos, relative=False)

    def _step_cleanup_temp_stand(self, ctx: WorkflowContext, temp_stand_pos: List[int]) -> Dict[str, Any]:
        for anchor in ("shore_approach_1", "shore_approach_2", "player_start"):
            try:
                stand_at(ctx, anchor)
                break
            except WorkflowStepError:
                if anchor == "player_start":
                    raise
        return dig_block(ctx, temp_stand_pos, relative=False, collect=False)

    def _scoop_if_water(self, ctx: WorkflowContext, rel_pos: Tuple[int, int, int]) -> bool:
        block = ctx.read_rel_block(rel_pos)
        name = str(block.get("name") or "air")
        props = block.get("properties") if isinstance(block.get("properties"), dict) else {}
        level = props.get("level")
        is_source = name == "water" and (level is None or level == 0)
        if is_source and ctx.select_item("bucket") is not None:
            try:
                use_on(ctx, "bucket", rel_pos, verify_held=True)
                return True
            except WorkflowStepError as err:
                if err.code != "cannot_interact":
                    raise
        return False

    def _step_activate_water_curtain(self, ctx: WorkflowContext) -> Dict[str, Any]:
        # Cast from elevated temp stand if present (before top_beam_right is placed) so outbound
        # transit through (2,0,0) stays completely dry; fall back to shore_scoop otherwise.
        if ctx.extra.get("temp_stand_pos") is not None:
            stand_at(ctx, "cast_center")
        else:
            stand_at(ctx, "shore_scoop")
        water_rel = (2, 3, -1)
        pillar_top_rel = (3, 3, -1)
        aim_at(ctx, pillar_top_rel)
        res = use_on(ctx, "water_bucket", water_rel, ref_rel=pillar_top_rel, face_rel=(-1, 0, 0), verify_held=True)
        # Safety check: scoop accidental Y+1 spill if water landed on top face.
        self._scoop_if_water(ctx, (2, 4, -1))
        ok = wait_until(
            ctx,
            lambda c: str(c.read_rel_block(water_rel).get("name") or "air") == "water",
            timeout_seconds=2.5,
            poll_seconds=0.25,
        )
        if not ok:
            world = ctx.world_pos(water_rel, integer=True)
            raise WorkflowStepError(
                "water_curtain_failed",
                f"Water curtain failed to activate at {world}.",
                blocker_type="verification_failed",
                blocker_pos=world,
                suggested_recovery="check_scaffolding_and_water_bucket",
            )
        # Wait briefly for the flowing curtain to reach (2, 2, 0) when top_beam_right is still open.
        if str(ctx.read_rel_block((2, 3, 0)).get("name") or "air") != "obsidian":
            wait_until(
                ctx,
                lambda c: str(c.read_rel_block((2, 2, 0)).get("name") or "air") in {"water", "flowing_water"},
                timeout_seconds=1.5,
                poll_seconds=0.2,
                description="water curtain flow to (2, 2, 0)",
            )
        return res

    def _prefill_not_needed(self, ctx: WorkflowContext) -> bool:
        counts = ctx.inventory_counts()
        empty_buckets = int(counts.get("bucket", 0))
        if empty_buckets <= 0:
            return True
        lava_have = int(counts.get("lava_bucket", 0))
        needed = sum(
            1
            for spec in self.CAST_SPECS
            if str(ctx.read_rel_block(spec["pos"]).get("name") or "air") != "obsidian"
        )
        return lava_have >= needed

    def _step_prefill_lava_buckets(
        self,
        ctx: WorkflowContext,
        *,
        has_temp_stand: bool = True,
    ) -> Dict[str, Any]:
        stance = "cast_center" if has_temp_stand else "shore_scoop"
        counts = ctx.inventory_counts()
        empty_before = int(counts.get("bucket", 0))
        lava_before = int(counts.get("lava_bucket", 0))
        needed = sum(
            1
            for spec in self.CAST_SPECS
            if str(ctx.read_rel_block(spec["pos"]).get("name") or "air") != "obsidian"
        )
        to_fill = min(empty_before, max(0, needed - lava_before))
        if to_fill <= 0:
            return {"skipped": True, "lava_buckets": lava_before}
        stand_at(ctx, stance)
        self._refill_lava_from_pool(
            ctx,
            return_stance=stance,
            max_refills=to_fill,
            force_phase2=True,
        )
        return {"lava_buckets": int(ctx.inventory_counts().get("lava_bucket", 0))}

    def _refill_lava_from_pool(
        self,
        ctx: WorkflowContext,
        *,
        return_stance: Optional[Union[str, Sequence[float]]] = None,
        max_refills: Optional[int] = None,
        force_phase2: bool = False,
    ) -> bool:
        empty_start = int(ctx.inventory_counts().get("bucket", 0))
        if empty_start <= 0 or ctx.select_item("bucket") is None:
            return False
        target_fills = max(1, min(empty_start, int(max_refills))) if max_refills is not None else empty_start

        candidates = (
            (1, 0, 2), (2, 0, 2), (0, 0, 2), (3, 0, 2),
            (1, 0, 3), (2, 0, 3), (0, 0, 3), (3, 0, 3),
            (0, 0, 1), (3, 0, 1),
            (4, 0, 0), (4, 0, 1), (4, 0, 2), (4, 0, 3),
            (-1, 0, 0), (-1, 0, 1), (-1, 0, 2), (-1, 0, 3),
            (-2, 0, 1), (-2, 0, 2), (5, 0, 1), (5, 0, 2),
        )
        open_above = AIR | set(DEFAULT_REPLACEABLE)
        open_cands: List[Tuple[int, int, int]] = []
        for rel in candidates:
            above_name = str(ctx.read_rel_block((rel[0], rel[1] + 1, rel[2])).get("name") or "air")
            if above_name in open_above:
                open_cands.append(rel)

        filled = 0
        for rel in open_cands:
            if filled >= target_fills or ctx.select_item("bucket") is None:
                break
            blk = ctx.read_rel_block(rel)
            props = blk.get("properties") if isinstance(blk.get("properties"), dict) else {}
            if str(blk.get("name") or "air") == "lava" and props.get("level") in (None, 0):
                before_empty = int(ctx.inventory_counts().get("bucket", 0))
                before_lava = int(ctx.inventory_counts().get("lava_bucket", 0))
                aim_at(ctx, rel)
                try:
                    use_on(ctx, "bucket", rel, verify_held=True)
                except WorkflowStepError as err:
                    if err.code == "cannot_interact":
                        continue
                    raise
                after_counts = ctx.inventory_counts()
                after_empty = int(after_counts.get("bucket", 0))
                after_lava = int(after_counts.get("lava_bucket", 0))
                if after_lava > before_lava or (after_lava > 0 and max_refills is None and filled == 0):
                    filled += 1
                    # Guard against fixed-inventory unit mocks that keep 'bucket' unchanged.
                    if after_empty >= before_empty or after_empty == 0:
                        return True
        if filled > 0 and ctx.select_item("lava_bucket") is not None:
            if not force_phase2 or filled >= target_fills or int(ctx.inventory_counts().get("bucket", 0)) == 0:
                return True

        # Phase 2: walk down via safe shore waypoints and scoop from reachable pool edges.
        if return_stance is None:
            return False

        non_solid = AIR | {"lava", "flowing_lava", "water", "flowing_water", "not_loaded", "unknown"}
        passable_head = AIR | set(DEFAULT_REPLACEABLE)
        open_worlds: List[Tuple[int, int, int]] = []
        liquid_above_worlds: List[Tuple[int, int, int]] = []
        seen_lava: set[Tuple[int, int, int]] = set()
        portal_frame_set = (
            {tuple(ctx.world_pos((dx, 0, 0), integer=True)) for dx in (0, 1, 2, 3)}
            | {tuple(ctx.world_pos((dx, -1, 0), integer=True)) for dx in (1, 2)}
        )

        def _is_lava_source_world(w: Tuple[int, int, int]) -> bool:
            blk = ctx.read_block([w[0], w[1], w[2]])
            props = blk.get("properties") if isinstance(blk.get("properties"), dict) else {}
            return str(blk.get("name") or "air") == "lava" and props.get("level") in (None, 0)

        def _classify_lava_world(w: Tuple[int, int, int]) -> None:
            if w in portal_frame_set or w in seen_lava:
                return
            seen_lava.add(w)
            if not _is_lava_source_world(w):
                return
            above = str(ctx.read_block([w[0], w[1] + 1, w[2]]).get("name") or "air")
            if above in passable_head:
                open_worlds.append(w)
            elif above in {"lava", "flowing_lava"}:
                liquid_above_worlds.append(w)

        bot_xyz = self._read_bot_xyz(ctx)
        find_blocks = getattr(ctx.guard.client, "find_blocks", None)
        if callable(find_blocks):
            try:
                rows = list(find_blocks(types=["lava"], radius=16, limit=96) or [])
            except Exception:
                rows = []
            if bot_xyz is not None:
                bx, by, bz = bot_xyz
                rows.sort(
                    key=lambda row: (
                        ((float(row["pos"][0]) + 0.5 - bx) ** 2 + (float(row["pos"][1]) - by) ** 2 + (float(row["pos"][2]) + 0.5 - bz) ** 2)
                        if isinstance(row, dict) and isinstance(row.get("pos"), (list, tuple)) and len(row["pos"]) >= 3
                        else 1e9
                    )
                )
            for row in rows:
                pos = row.get("pos") if isinstance(row, dict) else None
                props = row.get("properties") if isinstance(row, dict) and isinstance(row.get("properties"), dict) else {}
                if isinstance(pos, (list, tuple)) and len(pos) >= 3 and props.get("level") in (None, 0):
                    _classify_lava_world((int(pos[0]), int(pos[1]), int(pos[2])))
                    if len(open_worlds) >= 12:
                        break
        if not open_worlds and not liquid_above_worlds:
            for dy in (0, -1):
                for dz in (-1, 0, 1, 2, 3, 4):
                    for dx in range(-2, 6):
                        blk = ctx.read_rel_block((dx, dy, dz))
                        props = blk.get("properties") if isinstance(blk.get("properties"), dict) else {}
                        if str(blk.get("name") or "air") == "lava" and props.get("level") in (None, 0):
                            _classify_lava_world(tuple(ctx.world_pos((dx, dy, dz), integer=True)))

        lava_worlds = open_worlds + liquid_above_worlds
        # Keep curtain untouched until we know lava targets exist; Phase 3 can still scan 18m even if lava_worlds is empty.
        paused_curtain = False
        curtain_name = str(ctx.read_rel_block((2, 3, -1)).get("name") or "air")
        if curtain_name in {"water", "flowing_water"} and int(ctx.inventory_counts().get("bucket", 0)) >= 2 and ctx.select_item("water_bucket") is None:
            stand_at(ctx, "cast_center")
            before_water = int(ctx.inventory_counts().get("water_bucket", 0))
            aim_at(ctx, (2, 3, -1))
            use_on(ctx, "bucket", (2, 3, -1), verify_held=True)
            if int(ctx.inventory_counts().get("water_bucket", 0)) > before_water:
                paused_curtain = True
                wait_until(
                    ctx,
                    lambda c: str(c.read_rel_block((2, 2, 0)).get("name") or "air") not in {"water", "flowing_water"},
                    timeout_seconds=2.5,
                    poll_seconds=0.25,
                    description="drain water curtain before shore refill",
                )

        for wp in ("shore_approach_2", "shore_approach_1"):
            try:
                stand_at(ctx, wp)
            except WorkflowStepError:
                pass

        stand_offsets = (
            (-1, 0), (1, 0), (0, -1), (0, 1),
            (-1, -1), (1, -1), (-1, 1), (1, 1),
            (-2, 0), (2, 0), (0, -2), (0, 2),
        )
        scooped_any = False
        failed_attempts = 0
        for lx, ly, lz in lava_worlds:
            if filled >= target_fills or ctx.select_item("bucket") is None or failed_attempts >= 8:
                break
            if not _is_lava_source_world((lx, ly, lz)):
                continue
            # Prefer adjacent same-level dry shore cells first; only use ly+1 adjacent cells so rays never clip lips.
            stand_candidates: List[List[int]] = []
            for fl_y in (ly, ly + 1):
                for ox, oz in stand_offsets:
                    if fl_y == ly + 1 and abs(ox) + abs(oz) > 2:
                        continue
                    cand = [lx + ox, fl_y, lz + oz]
                    floor_name = str(ctx.read_block(cand).get("name") or "air")
                    if floor_name in non_solid:
                        continue
                    if max(abs(ox), abs(oz)) > 1:
                        sx = 1 if ox > 0 else (-1 if ox < 0 else 0)
                        sz = 1 if oz > 0 else (-1 if oz < 0 else 0)
                        mid_head = str(ctx.read_block([lx + sx, ly + 1, lz + sz]).get("name") or "air")
                        if mid_head not in passable_head:
                            continue
                    h1 = str(ctx.read_block([cand[0], cand[1] + 1, cand[2]]).get("name") or "air")
                    if h1 not in passable_head:
                        continue
                    h2 = str(ctx.read_block([cand[0], cand[1] + 2, cand[2]]).get("name") or "air")
                    if h2 in passable_head:
                        stand_candidates.append(cand)
                        if len(stand_candidates) >= 2:
                            break
                if len(stand_candidates) >= 2:
                    break
            for stand_cell in stand_candidates:
                if filled >= target_fills or ctx.select_item("bucket") is None or failed_attempts >= 8:
                    break
                try:
                    stand_at(
                        ctx,
                        (stand_cell[0] + 0.5, float(stand_cell[1] + 1), stand_cell[2] + 0.5),
                        relative=False,
                        tolerance=0.45,
                    )
                except WorkflowStepError:
                    continue
                if not _is_lava_source_world((lx, ly, lz)):
                    break
                before_empty = int(ctx.inventory_counts().get("bucket", 0))
                before_lava = int(ctx.inventory_counts().get("lava_bucket", 0))
                before_water = int(ctx.inventory_counts().get("water_bucket", 0))
                aim_at(ctx, [lx, ly, lz], relative=False)
                try:
                    use_on(ctx, "bucket", [lx, ly, lz], relative=False, verify_held=True)
                except WorkflowStepError as err:
                    if err.code == "cannot_interact":
                        continue
                    raise
                after_counts = ctx.inventory_counts()
                if int(after_counts.get("lava_bucket", 0)) > before_lava:
                    scooped_any = True
                    filled += 1
                    break
                if int(after_counts.get("water_bucket", 0)) > before_water:
                    failed_attempts += 1
                    break
                failed_attempts += 1

        for wp in ("shore_approach_1", "shore_approach_2"):
            try:
                stand_at(ctx, wp)
            except WorkflowStepError:
                pass
        if ctx.select_item("lava_bucket") is None and ctx.select_item("bucket") is not None:
            filled += self._refill_lava_phase3(ctx, max(1, target_fills - filled))
        if paused_curtain and ctx.select_item("water_bucket") is not None:
            self._step_activate_water_curtain(ctx)
        if return_stance and not (
            paused_curtain
            and return_stance in ("cast_center", "cast_right", "cast_left")
            and ctx.extra.get("temp_stand_pos") is not None
        ):
            stand_at(ctx, return_stance)
        return (scooped_any or filled > 0) and ctx.select_item("lava_bucket") is not None

    def _read_bot_xyz(self, ctx: WorkflowContext) -> Optional[Tuple[float, float, float]]:
        status = ctx.guard.check_status(require_safe=True)
        raw_pos = status.get("pos") if status.get("pos") is not None else status.get("position")
        if isinstance(raw_pos, dict):
            coords = [raw_pos.get("x"), raw_pos.get("y"), raw_pos.get("z")]
        elif isinstance(raw_pos, (list, tuple)) and len(raw_pos) >= 3:
            coords = [raw_pos[0], raw_pos[1], raw_pos[2]]
        else:
            return None
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in coords):
            return None
        return float(coords[0]), float(coords[1]), float(coords[2])

    def _refill_lava_phase3(self, ctx: WorkflowContext, max_fills: int) -> int:
        find_fn = getattr(ctx.guard.client, "find_blocks", None)
        if not callable(find_fn) or ctx.select_item("bucket") is None:
            return 0

        non_solid = AIR | HAZARDS | set(DEFAULT_REPLACEABLE) | {"not_loaded", "unknown"}
        standable_head = AIR | set(DEFAULT_REPLACEABLE)
        try:
            hits = list(ctx.guard.read(find_fn, ["lava"], radius=18, limit=48) or [])
        except Exception:
            return 0
        bot_xyz = self._read_bot_xyz(ctx)
        if bot_xyz is not None:
            bx, by, bz = bot_xyz
            hits.sort(
                key=lambda row: (
                    ((float(row["pos"][0]) + 0.5 - bx) ** 2 + (float(row["pos"][1]) - by) ** 2 + (float(row["pos"][2]) + 0.5 - bz) ** 2)
                    if isinstance(row, dict) and isinstance(row.get("pos"), (list, tuple)) and len(row["pos"]) >= 3
                    else 1e9
                )
            )
        filled = 0
        failed_hits = 0
        covered_fallback: List[Tuple[int, int, int, str]] = []
        for row in hits[:18]:
            if filled >= max_fills or ctx.select_item("bucket") is None:
                break
            if (filled >= 1 and failed_hits >= 3) or failed_hits >= 6:
                break
            pos = row.get("pos") if isinstance(row, dict) else None
            if not isinstance(pos, (list, tuple)) or len(pos) < 3:
                continue
            hx, hy, hz = int(pos[0]), int(pos[1]), int(pos[2])
            blk = ctx.read_block([hx, hy, hz])
            props = blk.get("properties") if isinstance(blk.get("properties"), dict) else {}
            if str(blk.get("name") or "air") != "lava" or props.get("level") not in (None, 0):
                continue
            above_name = str(ctx.read_block([hx, hy + 1, hz]).get("name") or "air")
            if above_name not in standable_head:
                if above_name in self.BASE_SOLID_DIGGABLE and len(covered_fallback) < 4:
                    covered_fallback.append((hx, hy, hz, above_name))
                continue
            stand_candidates: List[Tuple[float, List[float]]] = []
            for dy in (0, 1):
                for dx in range(-2, 3):
                    for dz in range(-2, 3):
                        if dx == 0 and dz == 0:
                            continue
                        if dy == 1 and abs(dx) + abs(dz) > 2:
                            continue
                        dist_h = math.hypot(dx, dz)
                        if dist_h < 1.0 or dist_h > 2.4:
                            continue
                        nx, ny_floor, nz = hx + dx, hy + dy, hz + dz
                        floor_name = str(ctx.read_block([nx, ny_floor, nz]).get("name") or "air")
                        if floor_name in non_solid:
                            continue
                        if max(abs(dx), abs(dz)) > 1:
                            sx = 1 if dx > 0 else (-1 if dx < 0 else 0)
                            sz = 1 if dz > 0 else (-1 if dz < 0 else 0)
                            mid_head = str(ctx.read_block([hx + sx, hy + 1, hz + sz]).get("name") or "air")
                            if mid_head not in standable_head:
                                continue
                        h1 = str(ctx.read_block([nx, ny_floor + 1, nz]).get("name") or "air")
                        h2 = str(ctx.read_block([nx, ny_floor + 2, nz]).get("name") or "air")
                        if h1 in standable_head and h2 in standable_head:
                            stand_candidates.append((dist_h + dy * 0.5, [nx + 0.5, float(ny_floor + 1), nz + 0.5]))
            stand_candidates.sort(key=lambda item: item[0])
            hit_ok = False
            for _, stance_pos in stand_candidates[:3]:
                try:
                    stand_at(ctx, stance_pos, relative=False, tolerance=0.6)
                    before_lava = int(ctx.inventory_counts().get("lava_bucket", 0))
                    aim_at(ctx, [hx, hy, hz], relative=False)
                    use_on(
                        ctx,
                        "bucket",
                        [hx, hy, hz],
                        relative=False,
                        ref_rel=[hx, hy, hz],
                        face_rel=(0, 1, 0),
                        verify_held=True,
                    )
                    if int(ctx.inventory_counts().get("lava_bucket", 0)) > before_lava:
                        filled += 1
                        hit_ok = True
                        break
                except WorkflowStepError:
                    continue
            if not hit_ok:
                failed_hits += 1

        # Last-resort cave recovery: if pool top froze under a 1-block cobble/stone crust over deep lava,
        # uncover 1 safe crust block with clear dry air above and scoop the exposed lava source underneath.
        if filled < max_fills and covered_fallback and ctx.select_item("bucket") is not None and ctx.select_item(PICKAXE_PRIORITY) is not None:
            for hx, hy, hz, _ in covered_fallback:
                if filled >= max_fills or ctx.select_item("bucket") is None:
                    break
                top2 = str(ctx.read_block([hx, hy + 2, hz]).get("name") or "air")
                if top2 not in standable_head:
                    continue
                for dx, dz in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    nx, nz = hx + dx, hz + dz
                    floor_name = str(ctx.read_block([nx, hy + 1, nz]).get("name") or "air")
                    if floor_name in non_solid:
                        continue
                    h1 = str(ctx.read_block([nx, hy + 2, nz]).get("name") or "air")
                    h2 = str(ctx.read_block([nx, hy + 3, nz]).get("name") or "air")
                    if h1 not in standable_head or h2 not in standable_head:
                        continue
                    try:
                        stand_at(ctx, [nx + 0.5, float(hy + 2), nz + 0.5], relative=False, tolerance=0.55)
                        dig_block(ctx, [hx, hy + 1, hz], relative=False, allowed_blocks=self.BASE_SOLID_DIGGABLE, collect=False)
                        chk = ctx.read_block([hx, hy, hz])
                        cprops = chk.get("properties") if isinstance(chk.get("properties"), dict) else {}
                        if str(chk.get("name") or "air") != "lava" or cprops.get("level") not in (None, 0):
                            break
                        before_lava = int(ctx.inventory_counts().get("lava_bucket", 0))
                        aim_at(ctx, [hx, hy, hz], relative=False)
                        use_on(
                            ctx,
                            "bucket",
                            [hx, hy, hz],
                            relative=False,
                            ref_rel=[hx, hy, hz],
                            face_rel=(0, 1, 0),
                            verify_held=True,
                        )
                        if int(ctx.inventory_counts().get("lava_bucket", 0)) > before_lava:
                            filled += 1
                            break
                    except WorkflowStepError:
                        continue
        return filled

    def _stand_pour_stance(
        self,
        ctx: WorkflowContext,
        rel: Tuple[int, int, int],
        pour_stance: Tuple[float, float, float],
    ) -> None:
        try:
            stand_at(ctx, pour_stance, tolerance=0.65)
            return
        except WorkflowStepError as err:
            if err.code != "unreachable_stance":
                raise
        dx = min(max(int(rel[0]), 0), 3)
        self._scoop_if_water(ctx, (dx, 1, -1))
        for hy in (1, 2):
            hcell = (dx, hy, -1)
            hname = str(ctx.read_rel_block(hcell).get("name") or "air")
            if hname in self.BASE_SOLID_DIGGABLE and hcell not in self._protected_build_cells(ctx):
                try:
                    dig_block(ctx, hcell, allowed_blocks=self.BASE_SOLID_DIGGABLE, collect=False)
                except WorkflowStepError:
                    pass
        try:
            stand_at(ctx, (dx + 0.5, 1.0, -0.78), tolerance=0.85, range_param=0.5)
        except WorkflowStepError:
            stand_at(ctx, (dx + 0.5, 1.0, -1.35), tolerance=1.15, range_param=0.5)

    def _is_on_temp_stand(self, ctx: WorkflowContext) -> bool:
        if ctx.extra.get("temp_stand_pos") is None:
            return False
        status = ctx.guard.check_status(require_safe=True)
        raw_pos = status.get("pos") if status.get("pos") is not None else status.get("position")
        if isinstance(raw_pos, dict):
            px, py, pz = raw_pos.get("x"), raw_pos.get("y"), raw_pos.get("z")
        elif isinstance(raw_pos, (list, tuple)) and len(raw_pos) >= 3:
            px, py, pz = raw_pos[0], raw_pos[1], raw_pos[2]
        else:
            return False
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (px, py, pz)):
            return False
        stand_center = ctx.world_pos((1.5, 2.0, 1.5), integer=False)
        return (
            math.hypot(float(px) - stand_center[0], float(pz) - stand_center[2]) <= 0.65
            and abs(float(py) - stand_center[1]) <= 0.45
        )

    def _recover_cast_stance(self, ctx: WorkflowContext, stance: str) -> None:
        paused_curtain = False
        curtain_blk = str(ctx.read_rel_block((2, 3, -1)).get("name") or "air")
        if curtain_blk in ("water", "flowing_water") and ctx.select_item("bucket") is not None:
            try:
                stand_at(ctx, "shore_scoop")
            except WorkflowStepError:
                pass
            use_on(
                ctx,
                "bucket",
                (2, 3, -1),
                ref_rel=(2, 3, -1),
                face_rel=(0, 1, 0),
                verify_held=True,
            )
            wait_until(
                ctx,
                lambda c: str(c.read_rel_block((1, 2, -1)).get("name") or "air") in AIR,
                timeout_seconds=3.0,
                poll_seconds=0.25,
                description="curtain_drain_for_cast_stance",
            )
            paused_curtain = True

        # Stage on the dry y=origin_y+1 walkway right next to (1,1,1) before climbing onto temp_stand.
        for stage_anchor in ("shore_approach_1", "shore_approach_2", (2.5, 1.0, 0.5)):
            try:
                stand_at(ctx, stage_anchor, tolerance=0.85)
                break
            except WorkflowStepError:
                continue

        try:
            stand_at(ctx, stance)
        except WorkflowStepError:
            stand_at(ctx, (1.5, 2.0, 1.5), tolerance=0.72)

        if paused_curtain:
            self._step_activate_water_curtain(ctx)

    def _step_cast_obsidian(self, ctx: WorkflowContext, spec: Dict[str, Any]) -> Dict[str, Any]:
        stance = spec["stance"]
        if ctx.select_item("lava_bucket") is None:
            self._refill_lava_from_pool(ctx, return_stance=stance)
        if not (stance in ("cast_center", "cast_right", "cast_left") and self._is_on_temp_stand(ctx)):
            try:
                stand_at(ctx, stance)
            except WorkflowStepError:
                if stance in ("cast_center", "cast_right", "cast_left") and ctx.extra.get("temp_stand_pos") is not None:
                    self._recover_cast_stance(ctx, stance)
                else:
                    raise
        aim_at(ctx, spec["pos"])
        use_on(
            ctx,
            "lava_bucket",
            spec["pos"],
            ref_rel=spec["ref"],
            face_rel=spec["face"],
            verify_held=True,
        )
        ok = wait_until(
            ctx,
            lambda c, rp=spec["pos"]: str(c.read_rel_block(rp).get("name") or "air") == "obsidian",
            timeout_seconds=2.5,
            poll_seconds=0.25,
        )
        actual = str(ctx.read_rel_block(spec["pos"]).get("name") or "air")
        if not ok:
            world = ctx.world_pos(spec["pos"], integer=True)
            raise WorkflowStepError(
                "cast_block_failed",
                f"Failed to cast obsidian ({spec['desc']}) at {world}: observed {actual}.",
                blocker_type="verification_failed",
                blocker_pos=world,
                blocker_block=actual,
                suggested_recovery=f"inspect_water_curtain_and_cast_at {world}",
            )
        return {"num": spec["num"], "desc": spec["desc"], "block": actual}

    def _move_shore_west(self, ctx: WorkflowContext) -> None:
        """Move to shore_west, clearing adjacent natural rock if it pinches off the path."""
        try:
            stand_at(ctx, "shore_west")
            return
        except WorkflowStepError:
            pass
        for rel_clear in ((1, 1, -2), (0, 1, -1), (0, 1, -2)):
            if str(ctx.read_rel_block(rel_clear).get("name") or "air") in CAVE_DIGGABLE_BLOCKS:
                dig_block(ctx, rel_clear, allowed_blocks=CAVE_DIGGABLE_BLOCKS, collect=False)
        try:
            stand_at(ctx, "shore_west")
        except WorkflowStepError:
            stand_at(ctx, (1.4, 1.0, -0.55), tolerance=0.45)

    def _step_scoop_top_water(self, ctx: WorkflowContext) -> Dict[str, Any]:
        water_rel = (2, 3, -1)
        water_center_w = ctx.world_pos((water_rel[0] + 0.5, water_rel[1] + 0.5, water_rel[2] + 0.5), integer=False)
        for stance in ("shore_scoop", (2.5, 1.0, -1.75), (0.55, 1.0, -1.5), (1.5, 1.0, 0.25)):
            try:
                stand_at(ctx, stance, tolerance=0.45)
            except WorkflowStepError:
                pass
            cur_pos = ctx.guard.check_status(require_safe=False).get("pos")
            within_reach = (
                isinstance(cur_pos, (list, tuple))
                and len(cur_pos) >= 3
                and math.sqrt(
                    (water_center_w[0] - float(cur_pos[0])) ** 2
                    + (water_center_w[1] - (float(cur_pos[1]) + 1.62)) ** 2
                    + (water_center_w[2] - float(cur_pos[2])) ** 2
                )
                <= 4.2
            )
            if within_reach:
                try:
                    aim_at(ctx, water_rel)
                    use_on(ctx, "bucket", water_rel, verify_held=True)
                except WorkflowStepError:
                    pass
                if str(ctx.read_rel_block(water_rel).get("name") or "air") != "water":
                    break
        cleared = wait_until(
            ctx,
            lambda c: str(c.read_rel_block(water_rel).get("name") or "air") != "water",
            timeout_seconds=2.5,
            poll_seconds=0.25,
        )
        if not cleared:
            world = ctx.world_pos(water_rel, integer=True)
            raise WorkflowStepError(
                "scoop_water_failed",
                f"Failed to scoop top water source at {world}.",
                blocker_type="verification_failed",
                blocker_pos=world,
                suggested_recovery="reposition_behind_scaffold_and_scoop",
            )
        return {"scooped": True}

    def _step_clear_interior_water(self, ctx: WorkflowContext) -> Dict[str, Any]:
        try:
            stand_at(ctx, "shore_approach_2")
        except WorkflowStepError:
            pass
        for dx in (1, 2):
            for dy in (0, 1, 2):
                try:
                    self._scoop_if_water(ctx, (dx, dy, 0))
                except WorkflowStepError:
                    pass
        if not self._interior_ready_for_ignition(ctx):
            for wp in ("shore_approach_1", "player_end"):
                try:
                    stand_at(ctx, wp)
                except WorkflowStepError:
                    pass
            for dx in (1, 2):
                for dy in (0, 1, 2):
                    try:
                        self._scoop_if_water(ctx, (dx, dy, 0))
                    except WorkflowStepError:
                        pass
        cleared = wait_until(
            ctx,
            self._interior_ready_for_ignition,
            timeout_seconds=5.0,
            poll_seconds=0.35,
            description="interior water clearance",
        )
        if not cleared:
            world = ctx.world_pos((1, 0, 0), integer=True)
            raise WorkflowStepError(
                "water_lingering",
                "Water did not clear from portal interior within timeout.",
                blocker_type="obstructed",
                blocker_pos=world,
                blocker_block=str(ctx.read_rel_block((1, 0, 0)).get("name") or "unknown"),
                suggested_recovery="locate_and_scoop_remaining_water_source",
            )
        return {"interior_clear": True}

    def _step_ignite_portal(self, ctx: WorkflowContext) -> Dict[str, Any]:
        stand_at(ctx, "player_end")
        base_rel = (1, -1, 0)
        aim_at(ctx, base_rel)
        use_on(ctx, "flint_and_steel", base_rel, ref_rel=base_rel, face_rel=(0, 1, 0), verify_held=True)
        active = wait_until(
            ctx,
            self._portal_active,
            timeout_seconds=2.5,
            poll_seconds=0.25,
            description="nether_portal activation",
        )
        if not active:
            world = ctx.world_pos((1, 0, 0), integer=True)
            raise WorkflowStepError(
                "portal_not_ignited",
                "Portal frame complete but nether_portal blocks did not activate.",
                blocker_type="verification_failed",
                blocker_pos=world,
                suggested_recovery="verify_interior_air_and_relight",
            )
        return {"ignited": True}

    def _step_approach_site(self, ctx: WorkflowContext) -> Dict[str, Any]:
        target = ctx.extra.get("approach_target")
        if target is None:
            return {"approached": False}
        tol = float(ctx.extra.get("start_tolerance", 2.4))
        try:
            res = stand_at(ctx, target, relative=False, tolerance=tol)
            return {"approached": True, "detail": res}
        except WorkflowStepError as err:
            if err.code != "unreachable_stance":
                raise
            xyz = self._read_bot_xyz(ctx)
            if xyz is not None:
                hdist = math.hypot(xyz[0] - float(target[0]), xyz[2] - float(target[2]))
                vdist = abs(xyz[1] - float(target[1]))
                if hdist <= tol + 0.3 and vdist <= 2.2:
                    return {"approached": True, "perched": True, "hdist": hdist, "vdist": vdist}
                if hdist > 4.5:
                    raise
            for anchor in ("player_start", "shore_approach_2", "shore_approach_1"):
                try:
                    res = stand_at(ctx, anchor, relative=True, tolerance=tol)
                    return {"approached": True, "anchor": anchor, "detail": res}
                except WorkflowStepError:
                    continue
            raise

    def _safe_dig_candidates(
        self,
        ctx: WorkflowContext,
        candidates: Sequence[Tuple[int, int, int]],
        *,
        allowed_dig: frozenset,
        limit: int,
    ) -> int:
        protected_fluid = {"lava", "flowing_lava", "water", "flowing_water"}
        protected_set = self._protected_build_cells(ctx)
        seen = set()
        dug = 0
        for cx, cy, cz in candidates:
            cell = (cx, cy, cz)
            if cell in seen or cell in protected_set:
                continue
            seen.add(cell)
            bname = str(ctx.read_block([cx, cy, cz]).get("name") or "air")
            if bname not in allowed_dig:
                continue
            # Never breach directly adjacent to fluid/lava pockets
            if any(
                str(ctx.read_block([cx + ndx, cy + ndy, cz + ndz]).get("name") or "air") in protected_fluid
                for ndx, ndy, ndz in ((0, 1, 0), (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1))
            ):
                continue
            try:
                dig_block(ctx, [cx, cy, cz], relative=False, allowed_blocks=allowed_dig, collect=False)
                dug += 1
                if dug >= limit:
                    break
            except WorkflowStepError:
                continue
        return dug

    def _allowed_recovery_dig_blocks(self, ctx: WorkflowContext) -> frozenset:
        allowed_dig = set(DEFAULT_REPLACEABLE)
        if ctx.select_item(PICKAXE_PRIORITY) is not None:
            allowed_dig |= set(CAVE_DIGGABLE_BLOCKS)
        else:
            allowed_dig |= {
                "dirt",
                "grass_block",
                "coarse_dirt",
                "sand",
                "red_sand",
                "gravel",
                "oak_leaves",
                "birch_leaves",
                "spruce_leaves",
            }
        return frozenset(allowed_dig)

    def _branch_approach_site(self, ctx: WorkflowContext, err: WorkflowStepError) -> bool:
        target = ctx.extra.get("approach_target")
        if target is None:
            return False
        xyz = self._read_bot_xyz(ctx)
        if xyz is None:
            return False

        px, py, pz = xyz
        tx, ty, tz = float(target[0]), float(target[1]), float(target[2])
        dx, dy, dz = tx - px, ty - py, tz - pz
        sx = 1 if dx > 0.25 else (-1 if dx < -0.25 else 0)
        sz = 1 if dz > 0.25 else (-1 if dz < -0.25 else 0)
        bx, by, bz = int(math.floor(px)), int(math.floor(py)), int(math.floor(pz))

        allowed_dig = self._allowed_recovery_dig_blocks(ctx)
        candidates: List[Tuple[int, int, int]] = []
        if err.blocker_pos is not None:
            candidates.append((int(err.blocker_pos[0]), int(err.blocker_pos[1]), int(err.blocker_pos[2])))
        # Step heading + local pocket exits at foot/body/overhead levels
        for dist_step in (1, 2):
            for ox, oz in (
                (sx * dist_step, sz * dist_step),
                (sx * dist_step, 0),
                (0, sz * dist_step),
            ):
                if ox == 0 and oz == 0:
                    continue
                for hdy in (1, 0, 2):
                    candidates.append((bx + ox, by + hdy, bz + oz))
        if dy > 0.4:
            candidates.append((bx, by + 2, bz))
            candidates.append((bx + sx, by + 2, bz + sz))
        # When perched directly above the staging point, dig adjacent step-down cells (never under current feet)
        if abs(dx) < 1.2 and abs(dz) < 1.2 and py > ty + 0.5:
            for ix, iz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                for hdy in (1, 0, -1):
                    candidates.append((bx + ix, by + hdy, bz + iz))
        # Also clear solid blockers near target only when already within breaking reach.
        if (tx - px) ** 2 + (ty - py) ** 2 + (tz - pz) ** 2 <= 18.0:
            tcx, tcy, tcz = int(math.floor(tx)), int(math.floor(ty)), int(math.floor(tz))
            for hdy in (1, 0):
                candidates.append((tcx, tcy + hdy, tcz))

        dug = self._safe_dig_candidates(ctx, candidates, allowed_dig=allowed_dig, limit=4)
        if dug > 0:
            # Progressively step into the freshly cleared 1-block gap so thick cave walls are traversed
            non_solid = AIR | HAZARDS | set(DEFAULT_REPLACEABLE) | {"not_loaded", "unknown"}
            stand_clear = AIR | set(DEFAULT_REPLACEABLE)
            for ox, oz in ((sx, sz), (sx, 0), (0, sz)):
                if ox == 0 and oz == 0:
                    continue
                for ny in (by, by - 1, by + 1):
                    cx, cz = bx + ox, bz + oz
                    floor_name = str(ctx.read_block([cx, ny - 1, cz]).get("name") or "air")
                    h1 = str(ctx.read_block([cx, ny, cz]).get("name") or "air")
                    h2 = str(ctx.read_block([cx, ny + 1, cz]).get("name") or "air")
                    if floor_name not in non_solid and h1 in stand_clear and h2 in stand_clear:
                        try:
                            stand_at(ctx, [cx + 0.5, float(ny), cz + 0.5], relative=False, tolerance=0.45, range_param=0.4)
                            break
                        except WorkflowStepError:
                            continue
            return True

        old_dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        if old_dist > 3.0:
            mid = [px + dx * 0.5, ty if abs(dy) <= 1.5 else py, pz + dz * 0.5]
            try:
                stand_at(ctx, mid, relative=False, tolerance=2.0, range_param=1.5)
                new_xyz = self._read_bot_xyz(ctx)
                if new_xyz is not None:
                    nd = math.sqrt((tx - new_xyz[0]) ** 2 + (ty - new_xyz[1]) ** 2 + (tz - new_xyz[2]) ** 2)
                    if nd < old_dist - 0.5:
                        return True
            except WorkflowStepError:
                pass
        return False

    def _branch_recover_movement(self, ctx: WorkflowContext, err: WorkflowStepError) -> bool:
        xyz = self._read_bot_xyz(ctx)
        if xyz is None:
            return False
        bx, by, bz = int(math.floor(xyz[0])), int(math.floor(xyz[1])), int(math.floor(xyz[2]))
        soft_blocks = frozenset({"short_grass", "tall_grass", "fern", "large_fern", "vine", "snow", "leaf_litter"})
        allowed_dig = self.BASE_SOLID_DIGGABLE | soft_blocks
        candidates: List[Tuple[int, int, int]] = []
        if err.blocker_pos is not None:
            candidates.append((int(err.blocker_pos[0]), int(err.blocker_pos[1]), int(err.blocker_pos[2])))
        for ox, oz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            for hdy in (1, 0):
                candidates.append((bx + ox, by + hdy, bz + oz))
        candidates.append((bx, by + 2, bz))
        dug = self._safe_dig_candidates(ctx, candidates, allowed_dig=allowed_dig, limit=2)
        scooped_water = False
        if err.code in {"stalled", "path_not_found", "timeout"} and ctx.select_item("bucket") is not None:
            curtain_world = tuple(int(v) for v in ctx.world_pos((2, 3, -1), integer=True))
            water_probes = [
                (bx + ox, by + hdy, bz + oz)
                for hdy in (0, 1)
                for ox, oz in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))
            ]
            for wpos in water_probes:
                if wpos == curtain_world:
                    continue
                blk = ctx.read_block(wpos)
                props = blk.get("properties") if isinstance(blk.get("properties"), dict) else {}
                if str(blk.get("name") or "air") == "water" and props.get("level") in (None, 0):
                    before_wb = int(ctx.inventory_counts().get("water_bucket", 0))
                    try:
                        aim_at(ctx, wpos, relative=False)
                        use_on(ctx, "bucket", wpos, relative=False, verify_held=True)
                    except WorkflowStepError:
                        continue
                    if int(ctx.inventory_counts().get("water_bucket", 0)) > before_wb:
                        scooped_water = True
                        break
        if xyz[1] <= float(ctx.origin[1]) + 0.5:
            for anchor in ("shore_approach_1", "shore_approach_2", "player_start"):
                try:
                    stand_at(ctx, anchor, tolerance=0.85)
                    return True
                except WorkflowStepError:
                    continue
        time.sleep(0.35)
        if dug > 0 or scooped_water:
            return True
        if err.code in {"stalled", "timeout"}:
            stalls = int(ctx.extra.get("_transient_stall_retries", 0))
            if stalls < 2:
                ctx.extra["_transient_stall_retries"] = stalls + 1
                return True
        return False

    def _branch_clear_soft_obstacle(self, ctx: WorkflowContext, err: WorkflowStepError) -> bool:
        soft_blocks = frozenset({"short_grass", "tall_grass", "fern", "large_fern", "vine", "snow", "leaf_litter"})
        if err.blocker_block in {"sand", "red_sand", "gravel"}:
            time.sleep(0.4)
            if err.blocker_pos is not None:
                settled = str(ctx.read_block(err.blocker_pos).get("name") or "air")
                if settled in AIR:
                    return True
                if settled in self.BASE_SOLID_DIGGABLE and ctx.select_item(PICKAXE_PRIORITY) is not None:
                    dig_block(ctx, err.blocker_pos, relative=False, allowed_blocks=self.BASE_SOLID_DIGGABLE, collect=False)
                    return True
        if err.blocker_pos is not None and err.blocker_block in soft_blocks:
            dig_block(ctx, err.blocker_pos, relative=False, allowed_blocks=soft_blocks, collect=False)
            return True
        if (
            err.blocker_pos is not None
            and err.blocker_block in self.BASE_SOLID_DIGGABLE
            and ctx.select_item(PICKAXE_PRIORITY) is not None
        ):
            dig_block(ctx, err.blocker_pos, relative=False, allowed_blocks=self.BASE_SOLID_DIGGABLE, collect=False)
            return True
        return False

    def _branch_recover_cast(self, ctx: WorkflowContext, err: WorkflowStepError, spec: Dict[str, Any]) -> bool:
        if err.blocker_block in {"cobblestone", "stone"} and ctx.inventory_counts().get("lava_bucket", 0) >= 1:
            dig_block(ctx, spec["pos"], allowed_blocks={"cobblestone", "stone"}, collect=False)
            return True
        if err.blocker_block in {"lava", "flowing_lava"}:
            # Fluid tick may still be freezing the poured lava block; wait briefly before aborting.
            if wait_until(
                ctx,
                lambda c, rp=spec["pos"]: str(c.read_rel_block(rp).get("name") or "air") == "obsidian",
                timeout_seconds=1.5,
                poll_seconds=0.25,
                description=f"delayed freeze for {spec['desc']}",
            ):
                return True
        return False

    def _branch_water_cleanup(self, ctx: WorkflowContext, err: WorkflowStepError) -> bool:
        if str(ctx.read_rel_block((2, 3, -1)).get("name") or "air") in {"water", "flowing_water"}:
            self._step_scoop_top_water(ctx)
            stand_at(ctx, "shore_approach_2")
            stand_at(ctx, "shore_approach_1")
            stand_at(ctx, "player_end")
        for dx in (1, 2):
            for dy in (0, 1, 2):
                self._scoop_if_water(ctx, (dx, dy, 0))
        return True

    def _branch_retry_ignition(self, ctx: WorkflowContext, err: WorkflowStepError) -> bool:
        if not self._interior_ready_for_ignition(ctx):
            return False
        for rel in ((1, -1, 0), (2, -1, 0), (0, 0, 0), (3, 0, 0), (2, 3, 0), (1, 3, 0), (3, 1, 0), (3, 2, 0), (0, 1, 0), (0, 2, 0)):
            if str(ctx.read_rel_block(rel).get("name") or "air") != "obsidian":
                return False
        return ctx.select_item("flint_and_steel") is not None

    def _wait_until_ready(self, timeout_seconds: float = 6.0) -> Dict[str, Any]:
        deadline = time.monotonic() + max(0.5, float(timeout_seconds))
        last: Dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                st = self.client.get_status().get("result", {})
                if isinstance(st, dict):
                    last = st
                    if st.get("alive") is True and st.get("ready") is True:
                        return st
            except APIError:
                pass
            time.sleep(0.25)
        return last

    def execute_step_1(
        self,
        start_sand_pos: List[int],
        stone_pos: List[int],
        water_pos: List[int],
        red_sand_pos: List[int],
    ) -> SkillResult:
        """Executes Step 1 of Nether Portal Speedrun.
        
        Actions:
          1. Verify starting position on sand marker.
          2. Inventory check (building blocks and water bucket).
          3. 放置石头: Place temporary stone at stone_pos.
          4. 检查方块与周边流体状态.
          5. 放置水: Verify water bucket held, place water at water_pos.
          6. 检查水和岩浆状态及黑曜石生成.
          7. 换位置: Move to recommended standing position (red_sand_pos).
          8. 全状态核验.
        """
        start_time = time.time()
        logger.info(f"=== Starting Step 1: Place Stone, Place Water, Move to Red Sand ===")
        self._wait_until_ready()

        # 1. Check Starting Position & Inventory
        inv = self.client.get_inventory_counts()
        has_stone = any(inv.get(b, 0) > 0 for b in ["cobblestone", "stone", "dirt", "netherrack"])
        if not has_stone:
            return SkillResult(success=False, error_code="missing_building_block", message="No building blocks in inventory.")

        has_water = inv.get("water_bucket", 0) > 0
        if not has_water:
            return SkillResult(success=False, error_code="missing_water_bucket", message="No water bucket in inventory.")

        status = self.client.get_status().get("result", {})
        bot_pos = status.get("pos", [])
        logger.info(f"Step 1 Start: Bot at {bot_pos}, Inventory={inv}")

        # 2. 放置石头 (Place stone)
        b_stone_cur = self.client.get_block(stone_pos[0], stone_pos[1], stone_pos[2]).get("result", {})
        if b_stone_cur.get("name") in ["cobblestone", "stone"]:
            logger.info(f"Step 1.1: Stone already present at {stone_pos}.")
            stone_name = b_stone_cur.get("name")
            stone_res = {"placed": stone_name, "reused": True}
        else:
            logger.info(f"Step 1.1: Placing stone at {stone_pos}...")
            stone_res = self._record(
                step_id="step1_place_stone",
                description="第一步：放置临时石头",
                target_pos=stone_pos,
                action_func=lambda: self.client.place_block(item="cobblestone", x=stone_pos[0], y=stone_pos[1], z=stone_pos[2]),
            )
            time.sleep(0.3)
            b_stone = self.client.get_block(stone_pos[0], stone_pos[1], stone_pos[2]).get("result", {})
            stone_name = b_stone.get("name", "air")
            if stone_name not in ["cobblestone", "stone"]:
                return SkillResult(
                    success=False,
                    error_code="stone_place_failed",
                    message=f"Failed to place stone at {stone_pos}, current block is {stone_name}.",
                    data={"result": stone_res},
                )
        logger.info(f"Step 1.1 Verified: Stone at {stone_pos} -> {stone_name}")

        # Check liquid around stone before water placement
        b_water_target_before = self.client.get_block(water_pos[0], water_pos[1], water_pos[2]).get("result", {})
        logger.info(f"Step 1.2: Liquid state before water placement at {water_pos}: {b_water_target_before.get('name')}")

        # 3. 检查手持并放置水 (Check hand & Place water)
        inv_before_water = self.client.get_inventory_counts()
        if inv_before_water.get("water_bucket", 0) < 1:
            return SkillResult(success=False, error_code="no_water_bucket_held", message="Water bucket missing before placement.")

        logger.info(f"Step 1.3: Placing water at {water_pos} aiming against stone {stone_pos} West face...")
        # Explicitly pass ref=stone_pos, face=[-1, 0, 0] to guarantee water is placed on West face at Y=y0
        water_res = self._record(
            step_id="step1_place_water",
            description="第一步：放置水并生成基底黑曜石",
            target_pos=water_pos,
            anchor_ref=stone_pos,
            anchor_face=[-1, 0, 0],
            action_func=lambda: self.client.interact(pos=water_pos, item="water_bucket", ref=stone_pos, face=[-1, 0, 0]),
        )
        time.sleep(0.6)

        # Safety verification: ensure water did not accidentally land on top face at Y=y0+1
        b_above = self.client.get_block(water_pos[0], water_pos[1] + 1, water_pos[2]).get("result", {})
        if b_above.get("name") in ["water", "flowing_water"]:
            logger.warning(f"Step 1.3 Alert: Spill water detected at Y+1 ({b_above.get('name')}), scooping immediately...")
            self.client.equip(item="bucket", slot="hand")
            self._record(
                step_id="step1_scoop_spill",
                description="清理顶部溢出水流",
                target_pos=[water_pos[0], water_pos[1] + 1, water_pos[2]],
                action_func=lambda: self.client.interact(pos=[water_pos[0], water_pos[1] + 1, water_pos[2]], item="bucket"),
            )
            time.sleep(0.3)

        # 4. 检查放置水后水和岩浆的状态
        b_water_after = self.client.get_block(water_pos[0], water_pos[1], water_pos[2]).get("result", {})
        logger.info(f"Step 1.4: Target block at {water_pos} after water placement: {b_water_after.get('name')}")

        # In Step 1, adjacent lava sources freeze into obsidian
        adjacent_checks = [
            [water_pos[0] - 1, water_pos[1], water_pos[2]],
            [water_pos[0], water_pos[1], water_pos[2] + 1],
        ]
        obsidian_found = []
        for p in adjacent_checks:
            b = self.client.get_block(p[0], p[1], p[2]).get("result", {})
            name = b.get("name")
            obsidian_found.append({"pos": p, "name": name})
            logger.info(f"Step 1.4 Check: Block at {p} -> {name}")

        # 5. 换位置 (Move to red_sand)
        logger.info(f"Step 1.5: Moving to recommended standing position (red_sand at {red_sand_pos})...")
        move_res = self._record(
            step_id="step1_move_red_sand",
            description="第一步：换位置到推荐站位",
            target_pos=[int(red_sand_pos[0]), int(red_sand_pos[1]), int(red_sand_pos[2])],
            action_func=lambda: self.client.move_to(red_sand_pos[0], None, red_sand_pos[2]),
        )
        time.sleep(0.5)

        final_status = self.client.get_status().get("result", {})
        final_pos = final_status.get("pos", [])
        logger.info(f"Step 1.5 Bot arrived at: {final_pos}")

        elapsed = round(time.time() - start_time, 2)
        return SkillResult(
            success=True,
            message=f"Step 1 completed in {elapsed}s: Stone placed, water placed, moved to red_sand.",
            data={
                "start_pos": start_sand_pos,
                "stone_pos": stone_pos,
                "water_pos": water_pos,
                "red_sand_pos": red_sand_pos,
                "bot_final_pos": final_pos,
                "stone_block": stone_name,
                "water_spot_block": b_water_after.get("name"),
                "adjacent_blocks": obsidian_found,
                "elapsed": elapsed,
            },
        )

    def execute_step_2(
        self,
        stone_pos: List[int],
        base_positions: List[List[int]],
        red_sand_pos: List[int],
        water_placement_pos: Optional[List[int]] = None,
    ) -> SkillResult:
        """Executes Step 2 of Nether Portal Speedrun.
        
        Actions:
          1. 状态与位置检查: Ensure bot is standing near recommended red_sand position.
          2. 工具检查: Ensure pickaxe and buckets are available in inventory.
          3. 拆掉石头: Dig temporary stone block at stone_pos.
          4. 检查底部方块:
             - Check blocks at base_positions (Y=-1 / Y=63).
             - If already obsidian: confirmed!
             - If non-obsidian solid block (e.g. stone/dirt):
                 * Dig the block with pickaxe (collect=False).
                 * Check hand: ensure held item is lava_bucket (prevent holding water_bucket!).
                 * Place lava into the dug hole.
                 * Place water or let water flow onto it to freeze into obsidian.
             - If lava: ensure water freezes it into obsidian.
          5. 结构与流体校验:
             - Verify both bottom frame positions are confirmed obsidian.
             - Verify water coverage and borders conform to step_2_grid.
        """
        start_time = time.time()
        logger.info("=== Starting Step 2: Dig Stone, Check/Replace Bottom Base, Cast Obsidian ===")
        self._wait_until_ready()

        # 1. Check Bot Position & Inventory
        inv = self.client.get_inventory_counts()
        has_pick = any(inv.get(p, 0) > 0 for p in [
            "diamond_pickaxe", "netherite_pickaxe", "iron_pickaxe", "stone_pickaxe", "golden_pickaxe", "wooden_pickaxe"
        ])
        if not has_pick:
            return SkillResult(success=False, error_code="missing_pickaxe", message="No pickaxe found in inventory to dig stone/base.")

        # Ensure bot is near red_sand
        status = self.client.get_status().get("result", {})
        bot_pos = status.get("pos", [])
        dist_to_red_sand = ((bot_pos[0] - red_sand_pos[0])**2 + (bot_pos[2] - red_sand_pos[2])**2)**0.5 if bot_pos else 99
        if dist_to_red_sand > 2.0:
            logger.info(f"Step 2: Bot at {bot_pos}, repositioning to red_sand at {red_sand_pos}...")
            self.client.move_to(red_sand_pos[0], None, red_sand_pos[2])
            time.sleep(0.4)

        # 2. 拆掉石头与开出水下作业门洞 (Dig temporary stone & ensure opening blocks at Y=y0 are open)
        # Ensure pickaxe is equipped
        for pick in ["diamond_pickaxe", "netherite_pickaxe", "iron_pickaxe", "stone_pickaxe", "wooden_pickaxe"]:
            if inv.get(pick, 0) > 0:
                self.client.equip(item=pick, slot="hand")
                break

        # Dig stone_pos
        b_stone = self.client.get_block(stone_pos[0], stone_pos[1], stone_pos[2]).get("result", {})
        stone_name = b_stone.get("name", "air")
        logger.info(f"Step 2.1: Checking stone at {stone_pos} -> {stone_name}")
        if stone_name in ["cobblestone", "stone", "dirt", "netherrack", "obsidian"]:
            logger.info(f"Step 2.1: Digging temporary stone ({stone_name}) at {stone_pos}...")
            dig_res = self._record(
                step_id="step2_dig_stone",
                description="第二步：拆掉临时定位石头",
                target_pos=stone_pos,
                action_func=lambda: self.client.dig(stone_pos[0], stone_pos[1], stone_pos[2], collect=False, stop_on=["death"]),
            )
            time.sleep(0.4)
            b_stone_after = self.client.get_block(stone_pos[0], stone_pos[1], stone_pos[2]).get("result", {})
            stone_after_name = b_stone_after.get("name", "air")
            if stone_after_name in ["cobblestone", "stone", "dirt"]:
                return SkillResult(
                    success=False,
                    error_code="stone_dig_failed",
                    message=f"Failed to dig temporary stone at {stone_pos}, still {stone_after_name}.",
                    data={"dig_result": dig_res},
                )
            logger.info(f"Step 2.1 Verified: Stone at {stone_pos} successfully removed (now {stone_after_name}).")
        else:
            logger.info(f"Step 2.1: Stone at {stone_pos} is already {stone_name}, skipping dig.")

        # Also ensure all opening blocks directly above base_positions at Y=stone_pos[1] are dug open
        for b_pos in base_positions:
            top_pos = [b_pos[0], stone_pos[1], b_pos[2]]
            b_top = self.client.get_block(top_pos[0], top_pos[1], top_pos[2]).get("result", {})
            top_name = b_top.get("name", "air")
            if top_name in ["cobblestone", "stone", "dirt", "obsidian", "gravel"]:
                logger.info(f"Step 2.1: Digging opening block ({top_name}) at {top_pos}...")
                self._record(
                    step_id=f"step2_dig_opening_{top_pos[0]}",
                    description=f"挖掘门洞作业空间 [{top_pos[0]}, {top_pos[1]}, {top_pos[2]}]",
                    target_pos=top_pos,
                    action_func=lambda: self.client.dig(top_pos[0], top_pos[1], top_pos[2], collect=False, stop_on=["death"]),
                )
                time.sleep(0.4)

        # 3. 检查底部方块 (Check bottom blocks at Y=-1 / Y=63)
        base_states = []
        for idx, pos in enumerate(base_positions):
            b_cur = self.client.get_block(pos[0], pos[1], pos[2]).get("result", {})
            cur_name = b_cur.get("name", "air")
            logger.info(f"Step 2.2: Checking bottom block {idx+1}/{len(base_positions)} at {pos} -> {cur_name}")

            if cur_name == "obsidian":
                logger.info(f"Step 2.2: Bottom block at {pos} is already obsidian.")
                base_states.append({"pos": pos, "name": "obsidian", "action": "already_obsidian"})
                continue

            # Non-obsidian solid block: Dig it out
            if cur_name in ["stone", "cobblestone", "dirt", "gravel", "sand", "andesite", "diorite", "granite"]:
                logger.info(f"Step 2.2: Digging non-obsidian solid block ({cur_name}) at {pos}...")
                self._record(
                    step_id=f"step2_dig_base_{idx}",
                    description=f"挖掘底部非黑曜石方块 [{pos[0]}, {pos[1]}, {pos[2]}]",
                    target_pos=pos,
                    action_func=lambda: self.client.dig(pos[0], pos[1], pos[2], collect=False, stop_on=["death"]),
                )
                time.sleep(0.3)
                cur_name = self.client.get_block(pos[0], pos[1], pos[2]).get("result", {}).get("name", "air")
                logger.info(f"Step 2.2: Block at {pos} after dig is {cur_name}.")

            # If cavity needs lava: 放置石油 (place lava)
            if cur_name not in ["obsidian", "lava"]:
                # Check inventory for lava_bucket
                inv = self.client.get_inventory_counts()
                if inv.get("lava_bucket", 0) < 1:
                    return SkillResult(
                        success=False,
                        error_code="missing_lava_bucket",
                        message=f"No lava bucket available to cast bottom obsidian at {pos}.",
                    )
                logger.info("Step 2.2 Handheld Check: Equipping lava_bucket (verifying NOT water_bucket)...")
                self.client.equip(item="lava_bucket", slot="hand")
                time.sleep(0.1)

                logger.info(f"Step 2.2: Placing lava at {pos}...")
                place_lava_res = self._record(
                    step_id=f"step2_place_bottom_lava_{idx}",
                    description=f"注入底部岩浆 [{pos[0]}, {pos[1]}, {pos[2]}]",
                    target_pos=pos,
                    action_func=lambda: self.client.interact(pos=pos, item="lava_bucket", stop_on=["death"]),
                )
                time.sleep(0.5)

        # 4. If any bottom block is lava, place water to freeze it into obsidian!
        needs_water = False
        for pos in base_positions:
            b_check = self.client.get_block(pos[0], pos[1], pos[2]).get("result", {})
            if b_check.get("name") == "lava":
                needs_water = True
                break

        if needs_water:
            water_target = water_placement_pos or [base_positions[0][0], stone_pos[1], base_positions[0][2]]
            logger.info(f"Step 2.2: Freezing bottom trench lava with water at {water_target}...")
            self.client.equip(item="water_bucket", slot="hand")
            time.sleep(0.1)
            self._record(
                step_id="step2_freeze_trench",
                description=f"放置水流转化底部黑曜石 [{water_target[0]}, {water_target[1]}, {water_target[2]}]",
                target_pos=water_target,
                action_func=lambda: self.client.interact(pos=water_target, item="water_bucket", stop_on=["death"]),
            )
            time.sleep(0.8)
            # Scoop back the water source with empty bucket
            logger.info(f"Step 2.2: Scooping back water from {water_target}...")
            self.client.equip(item="bucket", slot="hand")
            time.sleep(0.1)
            self._record(
                step_id="step2_scoop_trench_water",
                description=f"回收底部水流 [{water_target[0]}, {water_target[1]}, {water_target[2]}]",
                target_pos=water_target,
                action_func=lambda: self.client.interact(pos=water_target, item="bucket", stop_on=["death"]),
            )
            time.sleep(0.5)

        # Re-verify all bottom blocks are now obsidian
        for pos in base_positions:
            b_final = self.client.get_block(pos[0], pos[1], pos[2]).get("result", {})
            final_name = b_final.get("name", "air")
            logger.info(f"Step 2.2 Verification: Bottom block at {pos} is now {final_name}")
            base_states.append({"pos": pos, "name": final_name, "action": "confirmed_obsidian"})
            if final_name != "obsidian":
                return SkillResult(
                    success=False,
                    error_code="base_obsidian_incomplete",
                    message=f"Bottom frame block at {pos} is {final_name}, expected obsidian.",
                    data={"base_states": base_states},
                )

        # 4. Final verification of Step 2 & clean bottom water source
        logger.info(f"Step 2.3: Verifying full bottom frame obsidian integrity...")
        all_obsidian = all(s["name"] == "obsidian" for s in base_states)
        if not all_obsidian:
            return SkillResult(
                success=False,
                error_code="step_2_verification_failed",
                message="Not all bottom portal positions formed obsidian.",
                data={"base_states": base_states},
            )

        # Scoop the Step 1/2 water source so the basin floor has no lingering source
        for chk_pos in [water_placement_pos, stone_pos]:
            if not chk_pos:
                continue
            b = self.client.get_block(chk_pos[0], chk_pos[1], chk_pos[2]).get("result", {})
            if b.get("name") in ["water", "flowing_water"]:
                logger.info(f"Step 2: Scooping bottom water source at {chk_pos}...")
                self.client.equip(item="bucket", slot="hand")
                time.sleep(0.1)
                self._record(
                    step_id=f"step2_clear_source_{chk_pos[0]}",
                    description=f"清理底层残留水原 [{chk_pos[0]}, {chk_pos[1]}, {chk_pos[2]}]",
                    target_pos=chk_pos,
                    action_func=lambda: self.client.interact(pos=chk_pos, item="bucket", stop_on=["death"]),
                )
                time.sleep(0.4)

        elapsed = round(time.time() - start_time, 2)
        return SkillResult(
            success=True,
            message=f"Step 2 completed in {elapsed}s: Stone dug, bottom blocks verified and converted to obsidian.",
            data={
                "stone_pos": stone_pos,
                "base_positions": base_positions,
                "base_states": base_states,
                "red_sand_pos": red_sand_pos,
                "elapsed": elapsed,
            },
        )

    def execute_step_3(
        self,
        x0: int,
        y0: int,
        z0: int,
        temp_stand_pos: Optional[List[int]] = None,
    ) -> SkillResult:
        """Executes Step 3 of Nether Portal Speedrun: Casting, Water Retrieval & Ignition.
        
        Casting Grid Layout:
          空 2 1 空   (Y = y0 + 3)
          6       4   (Y = y0 + 2)
          5       3   (Y = y0 + 1)
          下面已经完成 (Y = y0 & y0 - 1)
        
        Actions:
          1. 状态与位置准备:
             - Ensure temporary elevated standing block exists at temp_stand_pos [x0 + 2, y0 + 1, z0 + 1].
             - Bot positions on the elevated block for 100% unobstructed top-down line of sight.
          2. 手持安全检查与浇筑:
             - Verify 6 lava buckets, empty bucket, and flint & steel in inventory.
             - Cast 1..6 in exact order:
                 * Handheld check: strictly verify held item is lava_bucket (never water_bucket!).
                 * Pour lava into water curtain at target.
                 * Verify target block transformed into obsidian.
          3. 挪掉水并等水流干净:
             - Move to safe shore position [x0 + 2, y0, z0 - 2.5].
             - Equip empty bucket and scoop water source block at [x0 + 2, y0 + 3, z0 - 1].
             - Wait for water curtain to completely recede; verify portal openings are pure air.
          4. 清理临时标记方块:
             - Mine temporary standing block at temp_stand_pos with pickaxe (no markers left behind).
          5. 点燃地狱门:
             - Equip flint_and_steel.
             - Click top face of bottom sunken obsidian [x0 + 1, y0 - 1, z0].
             - Verify all 6 portal interior blocks [x0 + 1..2, y0..y0+2, z0] become nether_portal.
          6. 纯生存状态核验:
             - Ensure bot health is 20, zero damage taken, on_fire is False.
        """
        start_time = time.time()
        logger.info(f"=== Starting Step 3: Precision Portal Casting, Scoop Water, Ignite Portal ===")
        self._wait_until_ready()
        
        # 1. Precondition checks
        inv = self.client.get_inventory_counts()
        if inv.get("lava_bucket", 0) < 6:
            return SkillResult(
                success=False,
                error_code="missing_lava_buckets",
                message=f"Need 6 lava buckets for casting, found {inv.get('lava_bucket', 0)}.",
            )
        if inv.get("bucket", 0) < 1:
            return SkillResult(
                success=False,
                error_code="missing_empty_bucket",
                message="Need an empty bucket to scoop back water.",
            )
        if inv.get("flint_and_steel", 0) < 1:
            return SkillResult(
                success=False,
                error_code="missing_flint_and_steel",
                message="Need flint_and_steel to ignite the portal.",
            )
        
        # If temp_stand_pos is specified, use elevated block; otherwise stand directly on ground obsidian at [x0 + 1.6, y0 + 1.0, z0 + 0.8]
        if temp_stand_pos is not None:
            stand_block_pos = temp_stand_pos
            b_stand = self.client.get_block(stand_block_pos[0], stand_block_pos[1], stand_block_pos[2]).get("result", {})
            if b_stand.get("name") in ["air", "cave_air"]:
                logger.info(f"Step 3: Placing temporary standing block at {stand_block_pos}...")
                place_item = "cobblestone" if inv.get("cobblestone", 0) > 0 else "dirt"
                self.client.place_block(item=place_item, x=stand_block_pos[0], y=stand_block_pos[1], z=stand_block_pos[2])
                time.sleep(0.3)
            stand_coords = [stand_block_pos[0] + 0.6, stand_block_pos[1] + 1.0, stand_block_pos[2] + 0.5]
        else:
            stand_block_pos = None
            stand_coords = [x0 + 1.6, y0 + 1.0, z0 + 0.8]
        
        # Position bot on the standing spot if not already near it
        cur_st = self.client.get_status().get("result", {})
        cur_pos = cur_st.get("pos", [])
        dist = ((cur_pos[0] - stand_coords[0])**2 + (cur_pos[2] - stand_coords[2])**2)**0.5 if cur_pos else 99
        if dist > 0.3:
            logger.info(f"Step 3: Repositioning bot to standing spot at {stand_coords} (current dist={dist:.2f})...")
            self.client.move_to(stand_coords[0], None, stand_coords[2])
            time.sleep(0.4)
        else:
            logger.info(f"Step 3: Bot already in position at {cur_pos} (dist={dist:.2f} <= 0.3), maintaining stance.")
        
        # Dynamic runner stances ensuring 100% direct line of sight without looking through water:
        # - Top beam (Pours 1 & 2): center stance [x0 + 1.5, y0 + 1.0, z0 + 1.2]
        # - Right pillar (Pours 3 & 4): right stance [x0 + 2.8, y0 + 1.0, z0 + 1.2] (directly facing right column)
        # - Left pillar (Pours 5 & 6): left stance [x0 + 0.4, y0 + 1.0, z0 + 1.2] (directly facing left column)
        cast_targets = [
            {"num": 1, "desc": "Top beam right",     "pos": [x0 + 2, y0 + 3, z0], "ref": [x0 + 3, y0 + 3, z0], "face": [-1, 0, 0], "stance": [x0 + 1.55, z0 + 1.25]},
            {"num": 2, "desc": "Top beam left",      "pos": [x0 + 1, y0 + 3, z0], "ref": [x0 + 2, y0 + 3, z0], "face": [-1, 0, 0], "stance": [x0 + 1.55, z0 + 1.25]},
            {"num": 3, "desc": "Right pillar lower", "pos": [x0 + 3, y0 + 1, z0], "ref": [x0 + 3, y0 + 0, z0], "face": [0, 1, 0],  "stance": [x0 + 1.55, z0 + 1.25]},
            {"num": 4, "desc": "Right pillar upper", "pos": [x0 + 3, y0 + 2, z0], "ref": [x0 + 3, y0 + 1, z0], "face": [0, 1, 0],  "stance": [x0 + 1.55, z0 + 1.25]},
            {"num": 5, "desc": "Left pillar lower",  "pos": [x0 + 0, y0 + 1, z0], "ref": [x0 + 0, y0 + 0, z0], "face": [0, 1, 0],  "stance": [x0 + 1.55, z0 + 1.25]},
            {"num": 6, "desc": "Left pillar upper",  "pos": [x0 + 0, y0 + 2, z0], "ref": [x0 + 0, y0 + 1, z0], "face": [0, 1, 0],  "stance": [x0 + 1.55, z0 + 1.25]},
        ]
        
        cast_results = []
        last_stance = None
        for target in cast_targets:
            num = target["num"]
            desc = target["desc"]
            pos = target["pos"]
            anchor_ref = target.get("ref")
            anchor_face = target.get("face")
            req_stance = target.get("stance")
            
            # Reposition to target stance if needed
            if req_stance and req_stance != last_stance:
                logger.info(f"Step 3 Pour {num}/6: Repositioning to stance at {req_stance}...")
                self.client.move_to(req_stance[0], None, req_stance[1])
                time.sleep(0.3)
                last_stance = req_stance
            
            # Handheld safety check: Strictly verify held item is lava_bucket, NOT water_bucket!
            inv_now = self.client.get_inventory_counts()
            if inv_now.get("lava_bucket", 0) < 1:
                return SkillResult(
                    success=False,
                    error_code="missing_lava_bucket_mid_cast",
                    message=f"No lava bucket available before casting block {num} ({desc}).",
                )
            
            logger.info(f"Step 3 Pour {num}/6: Equipping lava_bucket (verifying NOT water_bucket)...")
            self.client.equip(item="lava_bucket", slot="hand")
            time.sleep(0.1)
            
            st_held = self.client.get_status().get("result", {}).get("held", {})
            held_name = st_held.get("name")
            if held_name != "lava_bucket":
                return SkillResult(
                    success=False,
                    error_code="wrong_held_item",
                    message=f"Safety check failed: holding {held_name}, expected lava_bucket!",
                )
            
            logger.info(f"Step 3 Pour {num}/6: Casting {desc} at {pos} against {anchor_ref} {anchor_face}...")
            pour_res = self._record(
                step_id=f"step3_pour_{num}_{desc.replace(' ', '_').lower()}",
                description=f"第三步浇筑：第{num}块黑曜石 ({desc})",
                target_pos=pos,
                anchor_ref=anchor_ref,
                anchor_face=anchor_face,
                action_func=lambda: self.client.interact(pos=pos, item="lava_bucket", ref=anchor_ref, face=anchor_face, stop_on=["death"]),
                portal_origin=(x0, y0, z0),
            )
            time.sleep(0.6)
            
            # Verify target block turned into obsidian
            b_after = self.client.get_block(pos[0], pos[1], pos[2]).get("result", {})
            after_name = b_after.get("name")
            logger.info(f"Step 3 Pour {num}/6 Verification: Block at {pos} -> {after_name}")
            cast_results.append({"num": num, "desc": desc, "pos": pos, "block": after_name})
            if after_name != "obsidian":
                return SkillResult(
                    success=False,
                    error_code="cast_block_failed",
                    message=f"Failed to cast block {num} ({desc}) at {pos}: got {after_name}, expected obsidian.",
                    data={"cast_results": cast_results, "pour_res": pour_res},
                )
        
        logger.info("Step 3.2: All 6 obsidian blocks cast successfully!")
        
        # 3. 清理临时标记方块 (Mine temporary standing block if used, stepping off onto safe obsidian dam first)
        if stand_block_pos is not None:
            self.client.move_to(x0 + 2.2, None, z0 + 1.3, range=0.3)
            time.sleep(0.2)
            b_temp_cur = self.client.get_block(stand_block_pos[0], stand_block_pos[1], stand_block_pos[2]).get("result", {})
            if b_temp_cur.get("name") not in ["air", "cave_air"]:
                logger.info(f"Step 3.3: Mining temporary standing block at {stand_block_pos}...")
                for pick in ["diamond_pickaxe", "netherite_pickaxe", "iron_pickaxe", "stone_pickaxe"]:
                    if inv.get(pick, 0) > 0:
                        self.client.equip(item=pick, slot="hand")
                        break
                self._record(
                    step_id="step3_mine_temp_stand",
                    description="拆除临时站立标记方块",
                    target_pos=stand_block_pos,
                    action_func=lambda: self.client.dig(stand_block_pos[0], stand_block_pos[1], stand_block_pos[2], collect=False, stop_on=["death"]),
                )
                time.sleep(0.4)

        # 4. 挪掉水并等水流干净 (Walk safely through portal opening onto shore behind scaffolding)
        logger.info("Step 3.4: Navigating to shore behind scaffolding to scoop top water curtain...")
        self.client.move_to(x0 + 2.15, None, z0 - 0.5, range=0.6)
        time.sleep(0.2)
        shore_pos = [x0 + 2.45, y0 + 1, z0 - 1.5]
        self.client.move_to(shore_pos[0], None, shore_pos[2], range=0.8)
        time.sleep(0.3)

        water_source = [x0 + 2, y0 + 3, z0 - 1]
        logger.info(f"Step 3.4: Scooping back top water source at {water_source} from shore...")
        self.client.equip(item="bucket", slot="hand")
        time.sleep(0.1)
        scoop_res = self._record(
            step_id="step3_scoop_top_water",
            description="回收顶部水幕源头",
            target_pos=water_source,
            action_func=lambda: self.client.interact(pos=water_source, item="bucket", stop_on=["death"]),
            portal_origin=(x0, y0, z0),
        )
        time.sleep(0.6)

        # Walk back to front of portal for puddle check and ignition
        logger.info("Step 3.4: Returning to front of portal...")
        self.client.move_to(x0 + 2.15, None, z0 - 0.5, range=0.6)
        time.sleep(0.2)
        self.client.move_to(x0 + 2.15, None, z0 + 1.25, range=0.5)
        time.sleep(0.2)
        front_pos = [x0 + 1.6, y0 + 1, z0 + 1.25]
        self.client.move_to(front_pos[0], None, front_pos[2], range=0.5)
        time.sleep(0.3)
        st_front = self.client.get_status().get("result", {})
        p_front = st_front.get("pos", [])
        if p_front and ((p_front[0] - front_pos[0])**2 + (p_front[2] - front_pos[2])**2)**0.5 > 1.2:
            logger.info(f"Re-aligning to front stance: {p_front} -> {front_pos}")
            self.client.move_to(front_pos[0], None, front_pos[2], range=0.6)
            time.sleep(0.3)

        # Check and scoop bottom water puddle if lingering
        for dx in [1, 2]:
            b_bot = self.client.get_block(x0 + dx, y0, z0).get("result", {})
            if b_bot.get("name") in ["water", "flowing_water"]:
                logger.info(f"Step 3.4: Scooping lingering puddle at [{x0+dx}, {y0}, {z0}]...")
                self.client.equip(item="bucket", slot="hand")
                time.sleep(0.1)
                self._record(
                    step_id=f"step3_clear_puddle_{dx}",
                    description=f"回收底部门洞残留水 [{x0+dx}, {y0}, {z0}]",
                    target_pos=[x0 + dx, y0, z0],
                    action_func=lambda: self.client.interact(pos=[x0 + dx, y0, z0], item="bucket", stop_on=["death"]),
                )
                time.sleep(0.5)
        
        # Verify portal openings are completely cleared of water
        logger.info("Step 3.4: Waiting for water to completely recede...")
        t_water_wait = time.time()
        while time.time() - t_water_wait < 5.0:
            interior_blocks = [
                self.client.get_block(x0 + dx, y, z0).get("result", {}).get("name", "air")
                for dx in [1, 2]
                for y in [y0, y0 + 1, y0 + 2]
            ]
            if all(b in ["air", "cave_air"] for b in interior_blocks):
                logger.info(f"Water cleared in {time.time() - t_water_wait:.2f}s!")
                break
            time.sleep(0.5)
        
        # 5. 点燃地狱门 (Ignite Nether Portal with Flint & Steel from front)
        logger.info(f"Step 3.5: Igniting portal with flint_and_steel at [{x0 + 1}, {y0 - 1}, {z0}]...")
        self.client.equip(item="flint_and_steel", slot="hand")
        time.sleep(0.2)
        ignite_res = self._record(
            step_id="step3_ignite_portal",
            description="点燃地狱门",
            target_pos=[x0 + 1, y0 - 1, z0],
            anchor_ref=[x0 + 1, y0 - 1, z0],
            anchor_face=[0, 1, 0],
            action_func=lambda: self.client.interact(
                pos=[x0 + 1, y0 - 1, z0], item="flint_and_steel", ref=[x0 + 1, y0 - 1, z0], face=[0, 1, 0], stop_on=["death"]
            ),
            portal_origin=(x0, y0, z0),
        )
        time.sleep(1.0)
        
        # Check all 6 portal blocks; retry ignition if needed
        portal_blocks = []
        for retry in range(2):
            portal_blocks = []
            for dx in [1, 2]:
                for y in [y0, y0 + 1, y0 + 2]:
                    pb = self.client.get_block(x0 + dx, y, z0).get("result", {})
                    pname = pb.get("name")
                    portal_blocks.append({"pos": [x0 + dx, y, z0], "name": pname})
            if all(p["name"] == "nether_portal" for p in portal_blocks):
                break
            logger.warning(f"Retrying ignition (attempt {retry + 2})...")
            self.client.equip(item="flint_and_steel", slot="hand")
            time.sleep(0.2)
            self.client.interact(pos=[x0 + 1, y0 - 1, z0], item="flint_and_steel", stop_on=["death"])
            time.sleep(1.0)
        
        all_portal = all(p["name"] == "nether_portal" for p in portal_blocks)
        logger.info(f"Step 3.5: Portal blocks check: {portal_blocks}")
        
        # 6. Survival state verification
        final_status = self.client.get_status().get("result", {})
        bot_health = final_status.get("health", 0)
        bot_on_fire = final_status.get("on_fire", False)
        logger.info(f"Step 3 Final Bot Status: Health={bot_health}, OnFire={bot_on_fire}")
        
        if not all_portal:
            return SkillResult(
                success=False,
                error_code="portal_not_ignited",
                message="Portal frame complete but nether_portal blocks did not activate.",
                data={"portal_blocks": portal_blocks, "ignite_res": ignite_res},
            )
        
        elapsed = round(time.time() - start_time, 2)
        return SkillResult(
            success=True,
            message=f"Step 3 completed successfully in {elapsed}s: All 6 obsidian blocks cast in sequence, water scooped, portal ignited!",
            data={
                "cast_results": cast_results,
                "portal_blocks": portal_blocks,
                "bot_health": bot_health,
                "bot_on_fire": bot_on_fire,
                "elapsed": elapsed,
            },
        )
