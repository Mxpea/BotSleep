"""L4 IntentCompiler: dynamic tool schema generator (<= 1.2KB) + validated L3 Task compiler."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from v2.bt.blackboard import Blackboard
from v2.domain import block_coord, compact_shelter_spec
from v2.primitives.base import PrimitiveContext
from v2.tasks.acquire import AcquireItemTask
from v2.tasks.base import Task
from v2.tasks.catalogue import TaskCatalogue
from v2.tasks.competition.manager import CompetitionManager
from v2.tasks.explore import ExploreUntilTask
from v2.tasks.mining import MineResourceTask
from v2.tasks.nether_portal import EnterNetherTask
from v2.tasks.shelter import BuildShelterTask
from v2.tasks.stronghold_end import StrongholdLocateTask


def _fn_tool(name: str, desc: str, props: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


_RAW_DROP_ALIASES: Dict[str, str] = {
    "iron_ore": "raw_iron",
    "deepslate_iron_ore": "raw_iron",
    "gold_ore": "raw_gold",
    "deepslate_gold_ore": "raw_gold",
    "copper_ore": "raw_copper",
    "deepslate_copper_ore": "raw_copper",
    "coal_ore": "coal",
    "deepslate_coal_ore": "coal",
    "diamond_ore": "diamond",
    "deepslate_diamond_ore": "diamond",
}


class IntentCompiler:
    """Generates dynamic minimal tool schemas and compiles LLM tool calls into L3 Task instances."""

    def __init__(
        self,
        ctx: PrimitiveContext,
        catalogue: TaskCatalogue,
        *,
        objective: str = "daily",
        competition: Optional[CompetitionManager] = None,
    ) -> None:
        self.ctx = ctx
        self.catalogue = catalogue
        self.objective = objective
        self.competition = competition

    def tool_schemas(self, *, needs_ai: bool = False) -> List[Dict[str, Any]]:
        """Dynamic tool exposure staying strictly compact (<= 1,200 bytes)."""
        if self.objective == "competition":
            return [
                _fn_tool("select_task", "Select & execute eligible scoring advancement", {"task_id": {"type": "string"}}, ["task_id"]),
                _fn_tool("update_progress", "Record text note ONLY (does not move or act; call select_task to run task)", {"note": {"type": "string"}}, ["note"]),
                _fn_tool("finish_task", "Verify active task via /advancements", {}, []),
                _fn_tool("force_exit_task", "Defer blocked task", {"reason": {"type": "string"}}, ["reason"]),
                _fn_tool("end_competition", "End competition run", {"reason": {"type": "string"}}, ["reason"]),
            ]

        tools = [
            _fn_tool(
                "run_goal",
                "Mount stateless L3 goal",
                {
                    "goal": {
                        "type": "string",
                        "enum": [
                            "acquire", "mine", "enter_nether", "build_shelter",
                            "explore", "locate_stronghold", "recover_corpse", "hunt_food",
                        ],
                    },
                    "item": {"type": "string"},
                    "count": {"type": "integer"},
                },
                ["goal"],
            ),
            _fn_tool("wait", "Pause 1 cycle without writing", {"reason": {"type": "string"}}, ["reason"]),
        ]
        if needs_ai:
            tools.append(
                _fn_tool(
                    "repair_step",
                    "Local escape/reposition step",
                    {"direction": {"type": "string", "enum": ["north", "south", "east", "west"]}},
                    ["direction"],
                )
            )
        return tools

    def _normalize_item_and_count(self, raw_item: str, raw_count: int) -> Tuple[str, int]:
        """Normalize accidental 'acquire:furnacex1' or 'furnacex1' item strings from LLM tool calls."""
        item = str(raw_item or "").strip()
        count = max(1, int(raw_count or 1))
        if item.startswith(("acquire:", "mine:")):
            item = item.split(":", 1)[1].strip()
        m = re.fullmatch(r"(.+?)x(\d+)", item)
        if m:
            is_known = (
                self.catalogue.get_best_recipe(item, {}) is not None
                or item in self.catalogue.gathering
                or item in self.catalogue.smelting
            )
            if not is_known:
                item = m.group(1)
                if raw_count <= 1:
                    count = max(1, int(m.group(2)))
        item = _RAW_DROP_ALIASES.get(item, item)
        return item, count

    def compile_tool_call(
        self,
        name: str,
        args: Dict[str, Any],
        bb: Blackboard,
    ) -> Tuple[Optional[Task], Dict[str, Any]]:
        """Validate & compile one tool call into (Optional[Task], tool_result_dict)."""
        if bb.get("pending_frozen") and name != "wait":
            return None, {"ok": False, "error": "pending_unknown_frozen"}

        if name == "wait":
            return None, {"ok": True, "action": "wait", "reason": str(args.get("reason", "wait"))}

        if self.objective == "competition" and self.competition is not None:
            if name == "select_task":
                tid = str(args.get("task_id", ""))
                res = self.competition.select_task(tid)
                if not res.get("ok"):
                    res = {**res, "selectable_sample": [e.id for e in self.competition.selectable_tasks()[:5]]}
                    return None, res
                task = self.competition.build_task(self.ctx, tid)
                return task, res
            if name == "update_progress":
                last_res = bb.get("last_plan_result")
                if not bb.active_goal_name and isinstance(last_res, dict) and "note" in last_res:
                    return None, {
                        "ok": False,
                        "error": "update_progress_is_note_only_call_select_task_or_force_exit",
                        "active_task_id": self.competition.active_task_id,
                    }
                return None, self.competition.update_progress(str(args.get("note", "")))
            if name == "finish_task":
                return None, self.competition.finish_task(self.ctx.client)
            if name == "force_exit_task":
                return None, self.competition.force_exit_task(str(args.get("reason", "deferred")))
            if name == "end_competition":
                return None, self.competition.end_competition(str(args.get("reason", "completed")))

        if name == "run_goal":
            goal = str(args.get("goal", ""))
            raw_item = str(args.get("item", "")).strip()
            raw_count = int(args.get("count", 1) or 1)
            item, count = self._normalize_item_and_count(raw_item, raw_count)
            if goal == "acquire":
                if not item:
                    return None, {"ok": False, "error": "missing_item"}
                return AcquireItemTask(self.ctx, self.catalogue, item, count), {"ok": True, "mounted": f"acquire:{item}x{count}"}
            if goal == "mine":
                if not item:
                    return None, {"ok": False, "error": "missing_item"}
                return MineResourceTask(self.ctx, self.catalogue, item, count), {"ok": True, "mounted": f"mine:{item}x{count}"}
            if goal == "enter_nether":
                return EnterNetherTask(self.ctx, self.catalogue), {"ok": True, "mounted": "enter_nether"}
            if goal == "build_shelter":
                spec = compact_shelter_spec(block_coord(bb.pos), facing="north")
                return BuildShelterTask(self.ctx, self.catalogue, spec), {"ok": True, "mounted": "build_shelter"}
            if goal == "explore":
                return ExploreUntilTask(self.ctx, lambda _b, _g: False, name="explore_open", max_steps=4), {"ok": True, "mounted": "explore"}
            if goal == "locate_stronghold":
                return StrongholdLocateTask(self.ctx, self.catalogue), {"ok": True, "mounted": "locate_stronghold"}
            if goal == "recover_corpse":
                from v2.tasks.corpse import RecoverDeathDropTask

                return RecoverDeathDropTask(self.ctx, self.catalogue), {"ok": True, "mounted": "recover_corpse"}
            if goal == "hunt_food":
                from v2.tasks.food import HuntFoodTask

                return HuntFoodTask(self.ctx), {"ok": True, "mounted": "hunt_food"}
            return None, {"ok": False, "error": f"unknown_goal:{goal}"}

        if name == "repair_step":
            direction = str(args.get("direction", "north"))
            return ExploreUntilTask(self.ctx, lambda _b, _g: False, name=f"repair_{direction}", step_distance=12.0, max_steps=2), {
                "ok": True, "mounted": f"repair_{direction}"
            }

        return None, {"ok": False, "error": f"unregistered_tool:{name}"}
