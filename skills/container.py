"""Unified container & furnace skill: inspect, batch deposit/withdraw, and multi-batch smelting."""

import math
import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from core.domain import LOG_NAMES, PLANK_NAMES, parse_pos
from core.memory import MemoryEngine
from skills.action_workflow import WorkflowContext, WorkflowGuard, WorkflowStepError, WorkflowUncertainError, stand_at
from core.structure_spec import MINE_CHEST_BLOCKS, MINE_SHAFT_SPEC
from . import BaseSkill, SkillResult

CHEST_CONTAINER_BLOCKS: frozenset = frozenset(set(MINE_CHEST_BLOCKS) | {"barrel"})
FURNACE_CONTAINER_BLOCKS: frozenset = frozenset({"furnace", "blast_furnace", "smoker"})
SUPPORTED_CONTAINERS: frozenset = CHEST_CONTAINER_BLOCKS | FURNACE_CONTAINER_BLOCKS

SMELTABLE_OUTPUT: Dict[str, str] = {
    "raw_iron": "iron_ingot",
    "iron_ore": "iron_ingot",
    "deepslate_iron_ore": "iron_ingot",
    "raw_copper": "copper_ingot",
    "copper_ore": "copper_ingot",
    "deepslate_copper_ore": "copper_ingot",
    "raw_gold": "gold_ingot",
    "gold_ore": "gold_ingot",
    "deepslate_gold_ore": "gold_ingot",
    "nether_gold_ore": "gold_ingot",
    "cobblestone": "stone",
    "cobbled_deepslate": "deepslate",
    "sand": "glass",
    "red_sand": "glass",
    "clay_ball": "brick",
    "beef": "cooked_beef",
    "porkchop": "cooked_porkchop",
    "mutton": "cooked_mutton",
    "chicken": "cooked_chicken",
    "rabbit": "cooked_rabbit",
    "cod": "cooked_cod",
    "salmon": "cooked_salmon",
    "potato": "baked_potato",
    "kelp": "dried_kelp",
    **{wood: "charcoal" for wood in LOG_NAMES},
}

FOOD_SMELT_INPUTS: frozenset = frozenset({
    "beef", "porkchop", "mutton", "chicken", "rabbit", "cod", "salmon", "potato", "kelp",
})
METAL_SMELT_INPUTS: frozenset = frozenset({
    "raw_iron", "iron_ore", "deepslate_iron_ore",
    "raw_copper", "copper_ore", "deepslate_copper_ore",
    "raw_gold", "gold_ore", "deepslate_gold_ore", "nether_gold_ore",
})

FUEL_BURN_ITEMS: Dict[str, float] = {
    "coal": 8.0,
    "charcoal": 8.0,
    "coal_block": 80.0,
    "blaze_rod": 12.0,
    "lava_bucket": 100.0,
    "stick": 0.5,
    **{wood: 1.5 for wood in LOG_NAMES},
    **{plank: 1.5 for plank in PLANK_NAMES},
}

FUEL_PRIORITY: Tuple[str, ...] = (
    "coal",
    "charcoal",
    "coal_block",
    *PLANK_NAMES,
    *LOG_NAMES,
    "stick",
)


class ContainerSkill(BaseSkill):
    """Standard container & furnace macro skill supporting batch deposit, withdraw, and verified smelting."""

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
        action: str,
        pos: Sequence[int],
        *,
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: float = 180.0,
        **kwargs: Any,
    ) -> SkillResult:
        """Dispatch 'inspect', 'deposit' ('store'), 'withdraw' ('take'), or 'smelt'."""
        act = str(action or "").strip().lower()
        if act == "inspect":
            return self.inspect(pos, cancel_event=cancel_event, timeout_seconds=timeout_seconds)
        if act in {"deposit", "store"}:
            return self.deposit(pos, cancel_event=cancel_event, timeout_seconds=timeout_seconds, **kwargs)
        if act in {"withdraw", "take"}:
            return self.withdraw(pos, cancel_event=cancel_event, timeout_seconds=timeout_seconds, **kwargs)
        if act == "smelt":
            return self.smelt(pos, cancel_event=cancel_event, timeout_seconds=timeout_seconds, **kwargs)
        return SkillResult(False, f"Unsupported container action '{action}'.", error_code="bad_args")

    def inspect(
        self,
        pos: Sequence[int],
        *,
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: float = 20.0,
    ) -> SkillResult:
        """Open a container and summarize slots; opening can move the bot."""
        wpos = parse_pos(pos, integer=True)
        if wpos is None:
            return SkillResult(False, "Container pos must be three integers [x, y, z].", error_code="bad_args")
        guard = WorkflowGuard(self.client, cancel_event=cancel_event, timeout_seconds=timeout_seconds, max_reads=200, allow_damage=True)
        try:
            bname, win = self._open_view(guard, list(wpos), SUPPORTED_CONTAINERS)
            return SkillResult(
                True,
                f"Inspected {bname} at {list(wpos)}.",
                data={"pos": list(wpos), "block": bname, "window": win, "counts": self._sections_counts(win)},
            )
        except WorkflowUncertainError as exc:
            return self._uncertain_result(exc, guard, list(wpos))
        except WorkflowStepError as exc:
            return SkillResult(False, str(exc), error_code=exc.code, data={"pos": list(wpos)})

    def deposit(
        self,
        pos: Sequence[int],
        items: Union[str, Mapping[str, int]],
        *,
        count: Optional[int] = None,
        section: str = "container",
        ensure_all: bool = True,
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: float = 45.0,
    ) -> SkillResult:
        """Deposit one or multiple item stacks into chest/barrel/furnace with delta verification."""
        wpos = parse_pos(pos, integer=True)
        if wpos is None:
            return SkillResult(False, "Container pos must be three integers [x, y, z].", error_code="bad_args")
        wanted = self._normalize_item_map(items, single_count=count)
        if not wanted:
            return SkillResult(False, "At least one item with count >= 1 is required to deposit.", error_code="bad_args")

        guard = WorkflowGuard(self.client, cancel_event=cancel_event, timeout_seconds=timeout_seconds, max_reads=400, allow_damage=True)
        try:
            target_types = FURNACE_CONTAINER_BLOCKS if section in {"input", "fuel"} else CHEST_CONTAINER_BLOCKS
            bname, win_before = self._open_view(guard, list(wpos), target_types)
            if section not in (win_before.get("sections") or {}):
                return SkillResult(False, f"{bname} has no section '{section}'.", error_code="bad_section")

            inv_before = self._read_inventory_counts(guard)
            ops: List[Dict[str, Any]] = []
            expected_put: Dict[str, int] = {}
            for item_name, req_n in wanted.items():
                have = inv_before.get(item_name, 0)
                if have < req_n:
                    if ensure_all:
                        return SkillResult(
                            False,
                            f"Need {req_n} {item_name} in inventory to deposit, have {have}.",
                            error_code="missing_item",
                            data={"item": item_name, "required": req_n, "have": have},
                        )
                    req_n = have
                if req_n > 0:
                    expected_put[item_name] = req_n
                    ops.append({"op": "put", "item": item_name, "count": int(req_n), "to": section})

            if not ops:
                return SkillResult(True, "Nothing to deposit.", data={"pos": list(wpos), "deposited": {}, "partial": False})

            deposited: Dict[str, int] = {}
            partial_error: Optional[str] = None
            for chunk_start in range(0, len(ops), 8):
                chunk = ops[chunk_start : chunk_start + 8]
                try:
                    guard.write(self.client.window_act, list(wpos), chunk)
                except WorkflowStepError as exc:
                    if ensure_all or exc.code != "section_full":
                        raise
                    partial_error = str(exc)
                    break

            inv_after = self._read_inventory_counts(guard)
            _, win_after = self._open_view(guard, list(wpos), target_types)
            for item_name in expected_put:
                delta = max(0, inv_before.get(item_name, 0) - inv_after.get(item_name, 0))
                if delta > 0:
                    deposited[item_name] = delta

            short = {k: v - deposited.get(k, 0) for k, v in expected_put.items() if deposited.get(k, 0) < v}
            if short and ensure_all:
                return SkillResult(
                    False,
                    f"Partial or unverified deposit into {bname} at {list(wpos)}: short={short}",
                    error_code="transfer_unverified" if not deposited else "partial_completion",
                    data={"pos": list(wpos), "deposited": deposited, "short": short, "window": win_after},
                )
            return SkillResult(
                True,
                f"Deposited {deposited} into {bname} at {list(wpos)}.",
                data={
                    "pos": list(wpos),
                    "block": bname,
                    "deposited": deposited,
                    "short": short,
                    "partial": bool(short or partial_error),
                    "window": win_after,
                },
            )
        except WorkflowUncertainError as exc:
            return self._uncertain_result(exc, guard, list(wpos))
        except WorkflowStepError as exc:
            return SkillResult(False, str(exc), error_code=exc.code, data={"pos": list(wpos)})

    def withdraw(
        self,
        pos: Sequence[int],
        items: Union[str, Mapping[str, int]],
        *,
        count: Optional[int] = None,
        section: Optional[str] = None,
        ensure_all: bool = True,
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: float = 45.0,
    ) -> SkillResult:
        """Withdraw one or multiple items from chest/barrel/furnace output with delta verification."""
        wpos = parse_pos(pos, integer=True)
        if wpos is None:
            return SkillResult(False, "Container pos must be three integers [x, y, z].", error_code="bad_args")
        wanted = self._normalize_item_map(items, single_count=count)
        if not wanted:
            return SkillResult(False, "At least one item with count >= 1 is required to withdraw.", error_code="bad_args")

        guard = WorkflowGuard(self.client, cancel_event=cancel_event, timeout_seconds=timeout_seconds, max_reads=400, allow_damage=True)
        try:
            bname, win_before = self._open_view(guard, list(wpos), SUPPORTED_CONTAINERS)
            sec = section or ("output" if bname in FURNACE_CONTAINER_BLOCKS else "container")
            if sec not in (win_before.get("sections") or {}):
                return SkillResult(False, f"{bname} has no section '{sec}'.", error_code="bad_section")

            sec_counts = self._section_counts(win_before, sec)
            inv_before = self._read_inventory_counts(guard)
            ops: List[Dict[str, Any]] = []
            expected_take: Dict[str, int] = {}
            for item_name, req_n in wanted.items():
                avail = sec_counts.get(item_name, 0)
                if avail < req_n:
                    if ensure_all:
                        return SkillResult(
                            False,
                            f"{bname}.{sec} lacks {req_n} {item_name} (has {avail}).",
                            error_code="not_ready" if sec == "output" else "missing_item",
                            data={"item": item_name, "required": req_n, "available": avail},
                        )
                    req_n = avail
                if req_n > 0:
                    expected_take[item_name] = req_n
                    ops.append({"op": "take", "item": item_name, "count": int(req_n), "from": sec})

            if not ops:
                return SkillResult(True, "Nothing to withdraw.", data={"pos": list(wpos), "withdrawn": {}})

            for chunk_start in range(0, len(ops), 8):
                guard.write(self.client.window_act, list(wpos), ops[chunk_start : chunk_start + 8])

            inv_after = self._read_inventory_counts(guard)
            _, win_after = self._open_view(guard, list(wpos), SUPPORTED_CONTAINERS)
            withdrawn: Dict[str, int] = {}
            for item_name in expected_take:
                delta = max(0, inv_after.get(item_name, 0) - inv_before.get(item_name, 0))
                if delta > 0:
                    withdrawn[item_name] = delta

            short = {k: v - withdrawn.get(k, 0) for k, v in expected_take.items() if withdrawn.get(k, 0) < v}
            if short and ensure_all:
                return SkillResult(
                    False,
                    f"Incomplete withdraw from {bname} at {list(wpos)}: short={short}",
                    error_code="transfer_unverified" if not withdrawn else "partial_completion",
                    data={"pos": list(wpos), "withdrawn": withdrawn, "short": short, "window": win_after},
                )
            return SkillResult(
                True,
                f"Withdrew {withdrawn} from {bname}.{sec} at {list(wpos)}.",
                data={"pos": list(wpos), "block": bname, "withdrawn": withdrawn, "short": short, "window": win_after},
            )
        except WorkflowUncertainError as exc:
            return self._uncertain_result(exc, guard, list(wpos))
        except WorkflowStepError as exc:
            return SkillResult(False, str(exc), error_code=exc.code, data={"pos": list(wpos)})

    def smelt(
        self,
        pos: Sequence[int],
        input_item: str,
        count: int = 1,
        *,
        fuel_item: Optional[str] = None,
        wait_complete: bool = True,
        collect_output: bool = True,
        auto_collect_existing: bool = True,
        poll_interval_seconds: float = 2.0,
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: Optional[float] = None,
    ) -> SkillResult:
        """Load input + computed fuel into furnace, optionally wait until smelted, and collect outputs."""
        wpos = parse_pos(pos, integer=True)
        if wpos is None:
            return SkillResult(False, "Furnace pos must be three integers [x, y, z].", error_code="bad_args")
        item = str(input_item or "").strip().lower()
        target_count = int(count)
        if target_count < 1:
            return SkillResult(False, "Smelt count must be >= 1.", error_code="bad_args")
        if item not in SMELTABLE_OUTPUT:
            return SkillResult(False, f"Unsupported smelting input '{item}'.", error_code="unsupported_recipe")
        expected_out = SMELTABLE_OUTPUT[item]

        per_item_s = 10.0
        total_timeout = float(timeout_seconds if timeout_seconds is not None else max(30.0, target_count * per_item_s + 25.0))
        guard = WorkflowGuard(self.client, cancel_event=cancel_event, timeout_seconds=total_timeout, max_reads=1200, allow_damage=True)

        try:
            bname, win = self._open_view(guard, list(wpos), FURNACE_CONTAINER_BLOCKS)
            if bname == "smoker" and item not in FOOD_SMELT_INPUTS:
                return SkillResult(False, f"Smoker cannot smelt '{item}'.", error_code="unsupported_recipe")
            if bname == "blast_furnace" and item not in METAL_SMELT_INPUTS:
                return SkillResult(False, f"Blast furnace cannot smelt '{item}'.", error_code="unsupported_recipe")
            if bname in {"smoker", "blast_furnace"}:
                per_item_s = 5.0

            pre_collected: Dict[str, int] = {}
            out_before = self._section_counts(win, "output")
            if out_before:
                if not auto_collect_existing:
                    return SkillResult(False, "Furnace output slot is not empty.", error_code="container_busy")
                guard.write(self.client.window_act, list(wpos), [{"op": "take", "from": "output"}])
                pre_collected = dict(out_before)
                _, win = self._open_view(guard, list(wpos), FURNACE_CONTAINER_BLOCKS)

            in_counts = self._section_counts(win, "input")
            existing_input = in_counts.get(item, 0)
            if any(k != item for k in in_counts):
                return SkillResult(False, f"Furnace input occupied by {in_counts}.", error_code="container_busy")

            inv_before = self._read_inventory_counts(guard)
            remaining_target = target_count
            total_collected: Dict[str, int] = dict(pre_collected)
            total_loaded = 0

            while remaining_target > 0:
                _, win = self._open_view(guard, list(wpos), FURNACE_CONTAINER_BLOCKS)
                in_counts = self._section_counts(win, "input")
                fuel_counts = self._section_counts(win, "fuel")
                cur_in = in_counts.get(item, 0)
                batch_target = min(64, remaining_target)
                need_put_in = max(0, batch_target - cur_in)

                inv_now = self._read_inventory_counts(guard)
                if inv_now.get(item, 0) < need_put_in:
                    if total_loaded == 0 and cur_in == 0:
                        return SkillResult(
                            False,
                            f"Need {need_put_in} {item} in inventory, have {inv_now.get(item, 0)}.",
                            error_code="missing_item",
                        )
                    need_put_in = inv_now.get(item, 0)

                total_items_in_batch = cur_in + need_put_in
                if total_items_in_batch <= 0:
                    break

                # Compute fuel needed cleanly against burn yield + already burning/loaded fuel
                info = win.get("info") if isinstance(win.get("info"), dict) else {}
                already_burning_credit = 1.0 if info.get("burning") else 0.0
                existing_fuel_credit = sum(FUEL_BURN_ITEMS.get(fk, 1.0) * fv for fk, fv in fuel_counts.items())
                needed_credit = max(0.0, float(total_items_in_batch) - already_burning_credit - existing_fuel_credit)

                ops: List[Dict[str, Any]] = []
                if need_put_in > 0:
                    ops.append({"op": "put", "item": item, "count": int(need_put_in), "to": "input"})

                chosen_fuel = fuel_item
                if needed_credit > 1e-6:
                    avail_for_fuel = dict(inv_now)
                    if need_put_in > 0:
                        avail_for_fuel[item] = max(0, avail_for_fuel.get(item, 0) - need_put_in)
                    if chosen_fuel is None:
                        chosen_fuel = next((f for f in FUEL_PRIORITY if avail_for_fuel.get(f, 0) > 0), None)
                    if chosen_fuel is None or chosen_fuel not in FUEL_BURN_ITEMS:
                        if existing_fuel_credit <= 0 and not info.get("burning"):
                            return SkillResult(False, "No usable smelting fuel available.", error_code="missing_item")
                    else:
                        per_unit = FUEL_BURN_ITEMS[chosen_fuel]
                        fuel_needed = max(1, math.ceil(needed_credit / per_unit))
                        fuel_have = avail_for_fuel.get(chosen_fuel, 0)
                        fuel_put = min(64, fuel_have, fuel_needed)
                        if fuel_put > 0:
                            ops.append({"op": "put", "item": chosen_fuel, "count": int(fuel_put), "to": "fuel"})
                        elif existing_fuel_credit <= 0 and not info.get("burning"):
                            return SkillResult(False, f"Insufficient fuel '{chosen_fuel}'.", error_code="missing_item")

                if ops:
                    guard.write(self.client.window_act, list(wpos), ops)
                    total_loaded += need_put_in

                if not wait_complete:
                    _, win_now = self._open_view(guard, list(wpos), FURNACE_CONTAINER_BLOCKS)
                    return SkillResult(
                        True,
                        f"Started smelting {total_items_in_batch} {item} in {bname} at {list(wpos)}.",
                        data={
                            "pos": list(wpos),
                            "stage": "started",
                            "input_item": item,
                            "expected_output": expected_out,
                            "loaded_input": total_items_in_batch,
                            "window": win_now,
                        },
                    )

                # Poll synchronously until this batch completes or fuel runs out
                batch_done = 0
                batch_deadline = time.monotonic() + max(15.0, total_items_in_batch * per_item_s + 15.0)
                while time.monotonic() < batch_deadline:
                    guard.check_budget()
                    _, cur_win = self._open_view(guard, list(wpos), FURNACE_CONTAINER_BLOCKS)
                    cur_in_cnt = self._section_counts(cur_win, "input").get(item, 0)
                    cur_out_cnt = self._section_counts(cur_win, "output").get(expected_out, 0)
                    cur_fuel_cnt = sum(self._section_counts(cur_win, "fuel").values())
                    cur_info = cur_win.get("info") if isinstance(cur_win.get("info"), dict) else {}
                    burning = bool(cur_info.get("burning"))

                    if cur_out_cnt > 0 and collect_output and (cur_in_cnt == 0 or cur_out_cnt >= 64):
                        guard.write(self.client.window_act, list(wpos), [{"op": "take", "from": "output", "item": expected_out}])
                        total_collected[expected_out] = total_collected.get(expected_out, 0) + cur_out_cnt
                        batch_done += cur_out_cnt
                        cur_out_cnt = 0

                    if cur_in_cnt == 0 and (cur_out_cnt == 0 or not collect_output):
                        if not collect_output:
                            batch_done += cur_out_cnt
                        break
                    if not burning and cur_fuel_cnt == 0 and cur_in_cnt > 0:
                        break
                    time.sleep(min(float(poll_interval_seconds), max(0.1, batch_deadline - time.monotonic())))

                remaining_target -= max(batch_done, total_items_in_batch)
                if batch_done < total_items_in_batch and inv_now.get(item, 0) <= need_put_in:
                    break

            # Final collect sweep
            _, win_end = self._open_view(guard, list(wpos), FURNACE_CONTAINER_BLOCKS)
            out_end = self._section_counts(win_end, "output")
            if collect_output and out_end.get(expected_out, 0) > 0:
                guard.write(self.client.window_act, list(wpos), [{"op": "take", "from": "output", "item": expected_out}])
                _, win_end = self._open_view(guard, list(wpos), FURNACE_CONTAINER_BLOCKS)

            inv_after = self._read_inventory_counts(guard)
            net_gained = max(0, inv_after.get(expected_out, 0) - inv_before.get(expected_out, 0))
            verified_output = net_gained if collect_output else self._section_counts(win_end, "output").get(expected_out, 0)
            completed = verified_output >= target_count
            return SkillResult(
                completed,
                f"Smelted {item} -> {expected_out} (gained {net_gained}) at {list(wpos)}.",
                error_code=None if completed else "smelt_unverified",
                data={
                    "pos": list(wpos),
                    "stage": "completed" if completed else "partial",
                    "completed": completed,
                    "requested": target_count,
                    "input_item": item,
                    "expected_output": expected_out,
                    "gained": net_gained,
                    "pre_collected": pre_collected,
                    "window": win_end,
                },
            )
        except WorkflowUncertainError as exc:
            return self._uncertain_result(exc, guard, list(wpos))
        except WorkflowStepError as exc:
            return SkillResult(False, str(exc), error_code=exc.code, data={"pos": list(wpos)})

    @staticmethod
    def _uncertain_result(exc: WorkflowUncertainError, guard: WorkflowGuard, pos: List[int]) -> SkillResult:
        # The public skill contract remains SkillResult even when an internal
        # workflow cannot establish an outcome. No follow-up I/O or cleanup.
        return SkillResult(False, str(exc), error_code=exc.code, data={
            **exc.data, "pos": pos, "executed": guard.writes > 0,
            "completed": False, "action_state": "unknown", "cleanup_skipped": "workflow_unknown",
        })

    def _open_view(self, guard: WorkflowGuard, pos: List[int], allowed_blocks: frozenset) -> Tuple[str, Dict[str, Any]]:
        guard.check_status(require_safe=True)
        blk_raw = guard.read(self.client.get_block, pos[0], pos[1], pos[2])
        blk = blk_raw.get("result") if isinstance(blk_raw, dict) and "result" in blk_raw else blk_raw
        bname = str(blk.get("name") or "unknown") if isinstance(blk, dict) else "unknown"
        if bname not in allowed_blocks:
            raise WorkflowStepError("unsupported_container", f"Block '{bname}' at {pos} is not a supported container/furnace.", blocker_pos=pos, blocker_block=bname)
        ctx = WorkflowContext(guard=guard, spec=MINE_SHAFT_SPEC, origin=[0, 0, 0], orientation="south")
        # Stand comfortably near the container without requiring inside-block entry
        st = guard.check_status(require_safe=True)
        cur_p = parse_pos(st.get("pos"))
        if cur_p is not None and math.dist(cur_p, [pos[0] + 0.5, pos[1], pos[2] + 0.5]) > 3.5:
            guard.write(self.client.move_to, float(pos[0]) + 0.5, float(pos[1]), float(pos[2]) + 0.5, range=2.5)
        raw = guard.write(self.client.window_view, pos)
        win = raw.get("result") if isinstance(raw, dict) and "result" in raw else raw
        if not isinstance(win, dict) or not isinstance(win.get("sections"), dict):
            raise WorkflowUncertainError("invalid_response", f"Invalid window payload from {bname} at {pos}")
        expected_sections = {"input", "fuel", "output"} if bname in FURNACE_CONTAINER_BLOCKS else {"container"}
        for section in expected_sections:
            self._section_counts(win, section)
        return bname, win

    def _read_inventory_counts(self, guard: WorkflowGuard) -> Dict[str, int]:
        raw = guard.read(self.client.inventory, timeout_seconds=guard.remaining_timeout(10.0))
        res = raw.get("result") if isinstance(raw, dict) and "result" in raw else raw
        if not isinstance(res, dict) or not isinstance(res.get("items"), list):
            raise WorkflowUncertainError("invalid_response", "Inventory payload has no items list")
        items = res["items"]
        counts: Dict[str, int] = {}
        for row in items:
            if isinstance(row, dict) and isinstance(row.get("name"), str):
                c = row.get("count", 0)
                if isinstance(c, int) and not isinstance(c, bool) and c > 0:
                    counts[row["name"]] = counts.get(row["name"], 0) + c
        return counts

    @staticmethod
    def _section_counts(win: Mapping[str, Any], section: str) -> Dict[str, int]:
        sections = win.get("sections")
        rows = sections.get(section) if isinstance(sections, dict) else None
        if not isinstance(rows, list):
            raise WorkflowUncertainError("invalid_response", f"Container section {section} is unconfirmed")
        out: Dict[str, int] = {}
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("name"), str)
                    or type(row.get("count")) is not int or row["count"] < 0):
                raise WorkflowUncertainError("invalid_response", f"Container section {section} has an invalid item")
            out[row["name"]] = out.get(row["name"], 0) + row["count"]
        return out

    @classmethod
    def _sections_counts(cls, win: Mapping[str, Any]) -> Dict[str, Dict[str, int]]:
        return {sec: cls._section_counts(win, sec) for sec in (win.get("sections") or {})}

    @staticmethod
    def _normalize_item_map(items: Union[str, Mapping[str, int]], *, single_count: Optional[int]) -> Dict[str, int]:
        if isinstance(items, str):
            cnt = 1 if single_count is None else int(single_count)
            return {items.strip().lower(): cnt} if cnt >= 1 and items.strip() else {}
        if isinstance(items, Mapping):
            out: Dict[str, int] = {}
            for k, v in items.items():
                if isinstance(k, str) and k.strip() and isinstance(v, int) and not isinstance(v, bool) and v >= 1:
                    out[k.strip().lower()] = int(v)
            return out
        return {}
