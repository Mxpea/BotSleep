"""Bounded forward surface scouting skill (~200-block one-way span, low stamina, decisive abort)."""

import math
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.domain import BED_NAMES, LOG_NAMES, parse_pos
from core.memory import MemoryEngine
from skills.action_workflow import WorkflowGuard, WorkflowStepError, WorkflowUncertainError
from skills.survival import SurvivalSkills
from . import BaseSkill, SkillResult


STRUCTURE_BLOCK_TYPES: Tuple[str, ...] = (
    "crafting_table",
    "furnace",
    "blast_furnace",
    "smoker",
    "chest",
    "barrel",
    "bell",
    "hay_block",
    "composter",
    "lectern",
    "brewing_stand",
    "enchanting_table",
    "obsidian",
    "crying_obsidian",
    "gilded_blackstone",
    *BED_NAMES,
)
SURFACE_ORE_TYPES: Tuple[str, ...] = (
    "coal_ore",
    "deepslate_coal_ore",
    "iron_ore",
    "deepslate_iron_ore",
    "copper_ore",
)
LAVA_TYPES: Tuple[str, ...] = ("lava",)
VILLAGE_MARKER_TYPES: frozenset = frozenset({"bell", "hay_block", "composter", "lectern", "blast_furnace", "smoker", *BED_NAMES})
CHEST_TYPES: frozenset = frozenset({"chest", "barrel"})
PORTAL_RELIC_TYPES: frozenset = frozenset({"obsidian", "crying_obsidian", "gilded_blackstone"})
FOOD_BLOCK_TYPES: Tuple[str, ...] = (
    "wheat",
    "carrots",
    "potatoes",
    "beetroots",
    "sweet_berry_bush",
    "melon",
    "pumpkin",
)
WOOD_FOOD_STICK_TYPES: Tuple[str, ...] = (
    *LOG_NAMES,
    *FOOD_BLOCK_TYPES,
    "dead_bush",
)
STRUCTURE_AND_ORE_TYPES: Tuple[str, ...] = (*STRUCTURE_BLOCK_TYPES, *SURFACE_ORE_TYPES)
VILLAGE_ENTITY_NAMES: frozenset = frozenset({"villager", "iron_golem"})


class SurfaceScoutSkill(BaseSkill):
    """Forward-moving surface scout that covers ~200 blocks from spawn without circling."""

    def __init__(self, client: Any, memory: Optional[MemoryEngine] = None):
        super().__init__(client)
        self.memory = memory

    def validate_preconditions(self, **kwargs) -> bool:
        """Verify bot is connected, alive, and ready in overworld."""
        try:
            raw = self.client.get_status()
            st = raw.get("result") if isinstance(raw, dict) and "result" in raw else raw
            return bool(isinstance(st, dict) and st.get("ready") and st.get("alive") is not False)
        except Exception:
            return False

    def execute(
        self,
        *,
        scan_radius: int = 48,
        hop_distance: float = 55.0,
        max_hops: int = 3,
        distance: Optional[float] = None,
        prefer_wood: bool = True,
        goal_focus: str = "auto",
        min_food: float = 14.0,
        max_food_drop: float = 2.0,
        attempt: int = 0,
        timeout_seconds: float = 55.0,
        cancel_event: Optional[threading.Event] = None,
    ) -> SkillResult:
        """Scan forward along verified waypoints up to 256 blocks; stop early on target finds or stamina drop."""
        scan_r = max(16, min(64, int(scan_radius)))
        hop_d = max(16.0, min(64.0, float(hop_distance)))
        hops_limit = max(1, min(6, int(max_hops)))
        focus = str(goal_focus or "auto").lower()
        travel_limit = hop_d * hops_limit if distance is None else float(distance)
        if not math.isfinite(travel_limit) or not 0 < travel_limit <= 256:
            return SkillResult(False, "Scout distance must be between 0 and 256 blocks", error_code="bad_args")
        guard = WorkflowGuard(
            self.client,
            cancel_event=cancel_event,
            timeout_seconds=max(10.0, float(timeout_seconds)),
            max_reads=360,
            allow_damage=True,
        )

        findings: Dict[str, Dict[Tuple[int, int, int], Dict[str, Any]]] = {
            "structures": {},
            "chests": {},
            "lava": {},
            "ores": {},
            "wood": {},
            "food": {},
            "dead_bush": {},
            "villagers": {},
        }
        waypoints: List[List[float]] = []
        travelled = 0.0
        early_stop_reason = "completed_forward_span" if hop_d * hops_limit >= travel_limit else "planned_hops_complete"

        try:
            st0, origin = self._stance(guard)
            start_food = float(st0.get("food", 20.0))
            dim = str(st0.get("dimension") or "overworld")
            waypoints.append([round(origin[0], 2), round(origin[1], 2), round(origin[2], 2)])

            cur_st = st0
            cur_pos = list(origin)
            self._scan_checkpoint(guard, cur_pos, dim, scan_r, findings, origin=origin)

            if self._should_stop_early(findings, prefer_wood=prefer_wood, goal_focus=focus):
                early_stop_reason = self._discovery_reason(findings, goal_focus=focus)
            else:
                headings = self._pick_headings(cur_pos, attempt=attempt)
                heading_idx = 0
                for _ in range(hops_limit):
                    cur_st, cur_pos = self._stance(guard)
                    cur_food = float(cur_st.get("food", 20.0))
                    if cur_food <= float(min_food) or (start_food - cur_food) >= float(max_food_drop):
                        early_stop_reason = "stamina_conserved"
                        break
                    if guard.remaining_timeout(60.0) < 12.0:
                        early_stop_reason = "time_budget_conserved"
                        break

                    stride = min(hop_d, max(0.0, travel_limit - travelled))
                    if stride <= 4:
                        break
                    hx, hz = headings[heading_idx % len(headings)]
                    tx = cur_pos[0] + hx * stride
                    tz = cur_pos[2] + hz * stride
                    landing = self._advance_hop(guard, tx, tz)
                    if math.dist(landing, cur_pos) <= .1 and heading_idx + 1 < len(headings):
                        heading_idx += 1
                        hx2, hz2 = headings[heading_idx % len(headings)]
                        cur_pos = landing
                        landing = self._advance_hop(guard, cur_pos[0] + hx2 * stride, cur_pos[2] + hz2 * stride)
                    if math.dist(landing, cur_pos) <= .1:
                        cur_pos = landing
                        early_stop_reason = "terrain_blocked_abort"
                        break

                    travelled += math.hypot(landing[0] - cur_pos[0], landing[2] - cur_pos[2])
                    cur_pos = landing
                    waypoints.append([round(cur_pos[0], 2), round(cur_pos[1], 2), round(cur_pos[2], 2)])
                    self._scan_checkpoint(guard, cur_pos, dim, scan_r, findings, origin=origin)
                    if self._should_stop_early(findings, prefer_wood=prefer_wood, goal_focus=focus):
                        early_stop_reason = self._discovery_reason(findings, goal_focus=focus)
                        break

            max_dist = max([math.dist(origin, wp) for wp in waypoints] or [0.0]) + scan_r
            report = self._build_report(
                origin=origin,
                final_pos=cur_pos,
                waypoints=waypoints,
                estimated_reach=round(max_dist, 1),
                early_stop_reason=early_stop_reason,
                findings=findings,
            )
            msg = (
                f"Scout discovered {', '.join(report['highlights'])} within ~{report['estimated_reach']} blocks."
                if report["highlights"]
                else f"Scout covered ~{report['estimated_reach']} blocks forward with no structures; stopping cleanly to save stamina."
            )
            return SkillResult(True, msg, data={**report, "requested_distance": travel_limit,
                "waypoint_distance": round(travelled, 2), "executed": guard.writes > 0})
        except WorkflowUncertainError as exc:
            return SkillResult(
                False,
                str(exc),
                error_code=exc.code,
                data={
                    **exc.data,
                    "executed": guard.writes > 0,
                    "completed": False,
                },
            )
        except WorkflowStepError as exc:
            return SkillResult(
                False,
                str(exc),
                error_code=exc.code,
                data={"executed": guard.writes > 0, "waypoints": waypoints},
            )

    @staticmethod
    def _pick_headings(origin: Sequence[float], *, attempt: int = 0) -> List[Tuple[float, float]]:
        # Spread across 8 outward bearings without circling back on origin.
        ring = (
            (1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0),
            (0.707, 0.707), (-0.707, 0.707), (-0.707, -0.707), (0.707, -0.707),
        )
        base = (math.floor(abs(origin[0])) + math.floor(abs(origin[2])) + max(0, int(attempt)) * 3) % len(ring)
        return [ring[(base + offset) % len(ring)] for offset in (0, 2, 6, 1)]

    @staticmethod
    def _stance(guard: WorkflowGuard):
        status = guard.check_status()
        pos = parse_pos(status.get("pos"))
        if pos is None or status.get("alive") is not True or status.get("ready") is not True:
            guard.fail_uncertain("action_unconfirmed" if guard.writes else "invalid_response",
                                 "Scout requires a current position before choosing another heading")
        if status.get("on_ground") is False:
            raise WorkflowStepError("unsafe_state", "Scout is not on stable ground", blocker_type="safety_interrupted")
        return status, pos

    def _advance_hop(self, guard: WorkflowGuard, tx: float, tz: float) -> List[float]:
        move_timeout_ms = int(min(18000, max(5000, guard.remaining_timeout(18.0) * 800)))
        try:
            response = guard.write(
                self.client.move_to,
                round(tx, 1),
                None,
                round(tz, 1),
                timeout_ms=move_timeout_ms,
                range=4,
            )
            receipt = response.get("result") if isinstance(response, dict) else None
            if isinstance(receipt, dict):
                if receipt.get("dimension_changed"):
                    raise WorkflowStepError("life_changed", "Dimension changed during the scout hop")
                if receipt.get("interrupted_by"):
                    raise WorkflowStepError("interrupted", f"Scout movement interrupted by {receipt['interrupted_by']}")
        except WorkflowStepError as exc:
            if exc.code not in {"path_not_found", "stalled", "out_of_reach"}:
                raise
        # Even a known path failure can move partway. Check life and actual
        # footing before deciding whether a different heading is warranted.
        try:
            _, landing = self._stance(guard)
            return landing
        except WorkflowStepError as exc:
            if ((exc.blocker_type == "safety_interrupted" and exc.code != "workflow_budget_exhausted")
                    or exc.code in {"dead", "life_changed", "cancelled", "unsafe_state"}):
                raise
            guard.fail_uncertain("action_unconfirmed", "Unable to verify the scout landing", {"read_error": exc.code})

    def _scan_checkpoint(
        self,
        guard: WorkflowGuard,
        cur_pos: Sequence[float],
        dim: str,
        scan_r: int,
        findings: Dict[str, Dict[Tuple[int, int, int], Dict[str, Any]]],
        *,
        origin: Optional[Sequence[float]] = None,
    ) -> None:
        self._query_blocks(guard, STRUCTURE_AND_ORE_TYPES, scan_r, findings, cur_pos, dim, origin=origin)
        self._query_blocks(guard, WOOD_FOOD_STICK_TYPES, scan_r, findings, cur_pos, dim, origin=origin)
        self._query_blocks(guard, LAVA_TYPES, scan_r, findings, cur_pos, dim, origin=origin)
        if self.memory is not None and len(findings["lava"]) >= 4:
            pts = [row["pos"] for row in findings["lava"].values()]
            anchor = min(pts, key=lambda p: math.dist(cur_pos, p))
            cluster = sum(1 for p in pts if math.dist(anchor, p) <= 12.0)
            if cluster >= 4:
                self.memory.record_landmark(name="lava_pool", category="hazard_resource", pos=list(anchor), dimension=dim)
        try:
            entities = guard.read(self.client.get_entities, radius=scan_r, timeout_seconds=guard.remaining_timeout(5.0))
        except WorkflowStepError:
            entities = []
        if isinstance(entities, list):
            for ent in entities:
                if not isinstance(ent, dict):
                    continue
                ename = ent.get("name")
                pos = parse_pos(ent.get("pos"))
                if ename in VILLAGE_ENTITY_NAMES and pos is not None:
                    key = (int(math.floor(pos[0])), int(math.floor(pos[1])), int(math.floor(pos[2])))
                    findings["villagers"][key] = {
                        "name": ename,
                        "pos": [round(pos[0], 1), round(pos[1], 1), round(pos[2], 1)],
                    }

    def _query_blocks(
        self,
        guard: WorkflowGuard,
        types: Sequence[str],
        scan_r: int,
        findings: Dict[str, Dict[Tuple[int, int, int], Dict[str, Any]]],
        cur_pos: Sequence[float],
        dim: str,
        *,
        origin: Optional[Sequence[float]] = None,
    ) -> None:
        rows: List[Dict[str, Any]] = []
        if callable(getattr(self.client, "find_blocks", None)):
            rows = guard.read(self.client.find_blocks, list(types), radius=scan_r, limit=16) or []
        elif callable(getattr(self.client, "get_blocks", None)):
            rows = guard.read(self.client.get_blocks, radius=scan_r, types=list(types), limit=16) or []
        if not isinstance(rows, list):
            return
        local_workstations = frozenset({"crafting_table", "furnace", *BED_NAMES})
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            pos = parse_pos(row.get("pos"))
            if not isinstance(name, str) or pos is None:
                continue
            key = (int(math.floor(pos[0])), int(math.floor(pos[1])), int(math.floor(pos[2])))
            entry = {"name": name, "pos": key_list(key)}
            if name in STRUCTURE_BLOCK_TYPES:
                if origin is not None and name in local_workstations and math.hypot(key[0] - origin[0], key[2] - origin[2]) <= 16.0:
                    continue
                findings["structures"][key] = entry
                if name in CHEST_TYPES:
                    findings["chests"][key] = entry
                if self.memory is not None and name in {"chest", "barrel", "furnace", "blast_furnace", "smoker", "crafting_table", *BED_NAMES}:
                    category = "container" if name in {"chest", "barrel"} else "bed" if name in BED_NAMES else "crafting"
                    self.memory.record_landmark(name=name, category=category, pos=list(key), dimension=dim)
            elif name == "lava":
                findings["lava"][key] = entry
            elif name in SURFACE_ORE_TYPES:
                findings["ores"][key] = entry
            elif name in SurvivalSkills.LOGS:
                findings["wood"][key] = entry
            elif name in FOOD_BLOCK_TYPES:
                findings["food"][key] = entry
            elif name == "dead_bush":
                findings["dead_bush"][key] = entry

    @staticmethod
    def _should_stop_early(
        findings: Dict[str, Dict[Tuple[int, int, int], Dict[str, Any]]],
        *,
        prefer_wood: bool,
        goal_focus: str = "auto",
    ) -> bool:
        if goal_focus == "lava":
            has_portal = any(row.get("name") in PORTAL_RELIC_TYPES for row in findings["structures"].values())
            return len(findings["lava"]) >= 4 or has_portal
        if goal_focus in {"village", "chest"}:
            has_village_marker = any(row.get("name") in VILLAGE_MARKER_TYPES for row in findings["structures"].values())
            return bool(findings["chests"] or findings["villagers"] or has_village_marker)
        if findings["structures"] or findings["villagers"] or len(findings["lava"]) >= 4:
            return True
        if prefer_wood and findings["wood"]:
            return True
        if len(findings["ores"]) >= 2 and not prefer_wood:
            return True
        return False

    @staticmethod
    def _discovery_reason(
        findings: Dict[str, Dict[Tuple[int, int, int], Dict[str, Any]]],
        *,
        goal_focus: str = "auto",
    ) -> str:
        if goal_focus == "lava" and len(findings["lava"]) >= 4:
            return "lava_pool_found"
        if goal_focus in {"village", "chest"} and findings["chests"]:
            return "chest_or_village_found"
        if findings["structures"] or findings["villagers"]:
            return "structure_or_village_found"
        if len(findings["lava"]) >= 4:
            return "lava_pool_found"
        if findings["wood"]:
            return "wood_found"
        if len(findings["ores"]) >= 2:
            return "surface_ores_found"
        return "target_found"

    @staticmethod
    def _build_report(
        *,
        origin: List[float],
        final_pos: List[float],
        waypoints: List[List[float]],
        estimated_reach: float,
        early_stop_reason: str,
        findings: Dict[str, Dict[Tuple[int, int, int], Dict[str, Any]]],
    ) -> Dict[str, Any]:
        sorted_cat = lambda name: sorted(findings[name].values(), key=lambda r: math.dist(origin, r["pos"]))[:6]
        structures = sorted_cat("structures")
        chests = sorted_cat("chests")
        lava = sorted_cat("lava")
        villagers = sorted_cat("villagers")
        wood = sorted_cat("wood")
        ores = sorted_cat("ores")
        food = sorted_cat("food")
        dead_bush = sorted_cat("dead_bush")
        highlights: List[str] = []
        if chests:
            highlights.append(f"{len(chests)} chests")
        if structures or villagers:
            highlights.append(f"{len(structures)} structure blocks/{len(villagers)} villagers")
        if lava:
            highlights.append(f"{len(lava)} lava cells")
        if wood:
            highlights.append(f"{len(wood)} logs")
        if ores:
            highlights.append(f"{len(ores)} exposed ores")
        if food:
            highlights.append(f"{len(food)} crops")
        return {
            "origin": [round(v, 2) for v in origin],
            "final_pos": [round(v, 2) for v in final_pos],
            "waypoints": waypoints,
            "hops_completed": max(0, len(waypoints) - 1),
            "estimated_reach": estimated_reach,
            "early_stop_reason": early_stop_reason,
            "aborted_empty": not bool(structures or villagers or wood or ores or lava or chests),
            "found_structures": structures,
            "found_chests": chests,
            "found_lava": lava,
            "found_villagers": villagers,
            "found_wood": wood,
            "found_ores": ores,
            "found_food": food,
            "found_dead_bush": dead_bush,
            "highlights": highlights,
        }


def key_list(key: Tuple[int, int, int]) -> List[int]:
    return [key[0], key[1], key[2]]
