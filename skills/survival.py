"""Conservative survival actions with fresh observations and result evidence.

The public API cannot establish whether a log/stone block was placed by a player.
Gathering restricts block types, not ownership. Calls already sent to the server
remain subject to their HTTP deadlines; cancellation is checked between calls.
"""

import math
import re
import time
from collections import deque
from typing import Any, Dict, List

from core.block_geometry import trace_block_visibility
from core.client import APIError

from . import SkillResult


class _Failure(Exception):
    def __init__(self, code: str, message: str, data=None):
        super().__init__(message)
        self.code = code
        self.data = data or {}


class SurvivalSkills:
    WOODS = ("oak", "spruce", "birch", "jungle", "acacia", "dark_oak", "mangrove", "cherry", "pale_oak")
    LOGS = frozenset(wood + "_log" for wood in WOODS)
    PLANKS = frozenset(wood + "_planks" for wood in WOODS)
    GATHERABLE = LOGS | {"stone", "cobblestone", "coal_ore", "deepslate_coal_ore"}
    PICKAXES = frozenset(tier + "_pickaxe" for tier in ("wooden", "copper", "stone", "iron", "golden", "diamond", "netherite"))
    AIR = frozenset({"air", "cave_air", "void_air"})
    RETREAT_PASSABLE = AIR | {"short_grass", "leaf_litter"}
    RETREAT_LEAVES = frozenset(wood + "_leaves" for wood in WOODS)
    HAZARDS = frozenset({"water", "lava", "bubble_column", "fire", "soul_fire", "magma_block",
                         "campfire", "soul_campfire", "cactus", "sweet_berry_bush", "powder_snow"})
    SUPPORT = LOGS | PLANKS | {"stone", "cobblestone", "dirt", "grass_block", "coarse_dirt", "rooted_dirt",
                               "andesite", "granite", "diorite", "deepslate", "cobbled_deepslate", "moss_block",
                               "crafting_table", "furnace"}
    PLACEABLE = LOGS | PLANKS | {"stone", "cobblestone", "dirt", "crafting_table", "furnace", "chest", "torch"}
    FOODS = frozenset({"apple", "bread", "carrot", "baked_potato", "beetroot", "melon_slice", "cookie",
                       "pumpkin_pie", "cooked_beef", "cooked_porkchop", "cooked_chicken", "cooked_mutton",
                       "cooked_rabbit", "cooked_cod", "cooked_salmon", "golden_carrot", "dried_kelp",
                       "sweet_berries", "glow_berries", "mushroom_stew", "rabbit_stew", "beetroot_soup"})
    ARGUMENTS = {
        "gather_block": ({"pos", "expected_name"}, {"pos", "expected_name"}),
        "craft_item": ({"item", "count"}, {"item"}),
        "place_item": ({"item", "pos"}, {"item", "pos"}),
        "eat_food": ({"item"}, {"item"}),
        "move_to": ({"pos", "range"}, {"pos"}),
        "retreat_from": ({"pos", "distance"}, {"pos"}),
    }

    def __init__(self, client):
        self.client = client

    @staticmethod
    def _result(response):
        if not isinstance(response, dict) or response.get("ok") is not True:
            error = response.get("error", {}) if isinstance(response, dict) else {}
            raise _Failure(error.get("code", "invalid_response"), error.get("message", "API action failed"))
        result = response.get("result")
        if not isinstance(result, dict):
            raise _Failure("invalid_response", "API response has no result object")
        return result

    @staticmethod
    def _number(value, name, minimum, maximum):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _Failure("bad_args", f"{name} must be a finite number")
        try:
            valid = math.isfinite(value) and minimum <= value <= maximum
        except OverflowError:
            valid = False
        if not valid:
            raise _Failure("bad_args", f"{name} is outside the supported range")
        return value

    @classmethod
    def _pos(cls, value, block=False):
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise _Failure("bad_args", "pos must contain three coordinates")
        pos = [cls._number(v, "pos", -30000000, 30000000) for v in value]
        if block and any(v != math.floor(v) for v in pos):
            raise _Failure("bad_args", "Block coordinates must be integers")
        return [int(v) for v in pos] if block else pos

    @staticmethod
    def _item(value):
        if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
            raise _Failure("bad_args", "item must be a simple Minecraft item name")
        return value

    @staticmethod
    def _check(context):
        if context["cancel"] is not None and context["cancel"].is_set():
            raise _Failure("cancelled", "Survival action cancelled")

    @staticmethod
    def _read_options(context):
        if "recovery_deadline" not in context:
            return {}
        remaining = context["recovery_deadline"] - time.monotonic()
        if remaining <= 0:
            raise _Failure("recovery_budget_exhausted", "Dig recovery observation budget expired")
        return {"timeout_seconds": remaining}

    def _status(self, context):
        self._check(context)
        status = self._result(self.client.get_status(**self._read_options(context)))
        self._check(context)
        if status.get("alive") is False:
            raise _Failure("dead", "Bot is dead")
        try:
            health = self._number(status.get("health"), "health", 0, 1024)
            position = self._pos(status.get("pos"))
        except _Failure:
            raise _Failure("invalid_response", "Status has invalid health or position") from None
        if health <= 0:
            raise _Failure("dead", "Bot is dead")
        if status.get("ready") is not True or status.get("alive") is not True:
            raise _Failure("not_ready", "Bot must be ready and alive")
        if status.get("busy"):
            raise _Failure("busy", "Another backend action is still running")
        if status.get("is_sleeping") or status.get("in_vehicle"):
            raise _Failure("unsafe_state", "Leave the bed or vehicle before this action")
        dimension = status.get("dimension")
        if not isinstance(dimension, str) or not dimension:
            raise _Failure("invalid_response", "Status has no dimension")
        if context.get("dimension") is not None and dimension != context["dimension"]:
            raise _Failure("dimension_changed", "The bot changed dimension during the action")
        status["pos"] = position
        return status

    def _inventory(self, context):
        self._check(context)
        result = self._result(self.client.inventory(**self._read_options(context)))
        self._check(context)
        items = result.get("items")
        if not isinstance(items, list):
            raise _Failure("invalid_response", "Inventory has no item list")
        counts = {}
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or type(item.get("count")) is not int or item["count"] < 0:
                raise _Failure("invalid_response", "Inventory contains an invalid item")
            counts[item["name"]] = counts.get(item["name"], 0) + item["count"]
        return counts, items

    def _block(self, pos, context):
        self._check(context)
        block = self._result(self.client.get_block(pos, **self._read_options(context)))
        self._check(context)
        if not isinstance(block.get("name"), str):
            raise _Failure("invalid_response", "Block query has no name")
        return block

    def _action(self, context, operation, *args, _validate_status=None, **kwargs):
        status = self._status(context)
        if _validate_status is not None:
            _validate_status(status)
        context["acted"] = True
        result = self._result(operation(*args, **kwargs))
        self._check(context)
        if result.get("interrupted_by"):
            raise _Failure("interrupted", "Backend action was interrupted", {"interrupted_by": result["interrupted_by"]})
        return result

    @staticmethod
    def _overworld(status):
        if status["dimension"] not in {"overworld", "minecraft:overworld"}:
            raise _Failure("unsafe_dimension", "This world-changing action is limited to the overworld")

    @classmethod
    def _safe_block(cls, block):
        return block["name"] not in cls.HAZARDS and (block.get("properties") or {}).get("waterlogged") not in (True, "true")

    def _approach(self, pos, context):
        status = self._status(context)
        if math.dist(status["pos"], pos) > 32:
            raise _Failure("out_of_range", "Target is more than 32 blocks away")
        if math.dist(status["pos"], pos) > 3.5:
            # GoalNear measures integer path nodes, while the feet position has
            # a cell-center offset. Leave room for it before checking reach.
            self._action(context, self.client.move_to, *pos, range=2, timeout_ms=15000)
            status = self._status(context)
        if math.dist(status["pos"], pos) > 3.5:
            raise _Failure("out_of_reach", "Approach did not reach interaction distance")
        return status

    def _gather_guard(self, pos, name, status, context):
        self._overworld(status)
        if status.get("in_water") or status.get("in_lava") or status.get("on_fire"):
            raise _Failure("unsafe", "Do not gather while in water, lava or fire")
        feet = [math.floor(v) for v in status["pos"]]
        if pos[0] == feet[0] and pos[2] == feet[2]:
            raise _Failure("unsafe", "Do not mine the column directly below or above the bot")
        block = self._block(pos, context)
        if block["name"] != name:
            raise _Failure("target_changed", "The requested gathering target changed")
        if block.get("diggable") is not True:
            raise _Failure("undiggable", "The observed block is not diggable")
        for dx, dy, dz in ((0, 0, 0), (0, -1, 0), (0, 1, 0), (-1, 0, 0), (1, 0, 0), (0, 0, -1), (0, 0, 1)):
            neighbor = self._block([pos[0] + dx, pos[1] + dy, pos[2] + dz], context)
            if not self._safe_block(neighbor):
                raise _Failure("unsafe", "Gathering target is adjacent to liquid, fire or a damaging block")
            if dy == 1 and (neighbor["name"] in {"sand", "red_sand", "gravel", "anvil", "chipped_anvil", "damaged_anvil"} or neighbor["name"].endswith("_concrete_powder")):
                raise _Failure("unsafe", "A falling block is above the gathering target")
        return block

    def _gather_block(self, args, context):
        pos = self._pos(args["pos"], block=True)
        name = self._item(args["expected_name"])
        if name not in self.GATHERABLE:
            raise _Failure("unsupported_block", "Only whitelisted logs, stone and coal blocks may be gathered")
        status = self._status(context)
        target = self._gather_guard(pos, name, status, context)
        before, items = self._inventory(context)
        if name not in self.LOGS:
            usable = [item for item in items if item["name"] in self.PICKAXES and item["count"] > 0
                      and (not item.get("durability") or item["durability"][0] > 0)]
            if not usable:
                raise _Failure("missing_tool", "A usable pickaxe is required for stone or coal")
        # Dig reach is measured from the eyes to the block, not from the feet
        # to its integer origin. Forcing range=2 first can demand climbing a
        # wall even though a visible log is already within interaction reach.
        reachable = target.get("can_dig", target.get("harvest_tool_ok")) is True
        # The original API still walks closer when feet-to-origin distance is
        # over 4, even if canDigBlock is true. Do that before testing sight so
        # the following dig does not silently choose a different viewpoint.
        if not reachable or math.dist(status["pos"], pos) > 4:
            status = self._approach(pos, context)
        target = self._gather_guard(pos, name, status, context)
        visible = target.get("can_see")
        visibility = {"source": "api", "visible": visible}
        if type(visible) is not bool:
            # Original mc-agent-api exposes canDigBlock as harvest_tool_ok,
            # which establishes reach but says nothing about intervening walls.
            visibility = trace_block_visibility(
                status["pos"], pos, lambda point: self._block(point, context),
                max_queries=32, max_distance=8)
            visible = visibility["visible"]
        if visible is not True:
            raise _Failure(
                "target_occluded" if visible is False else "visibility_unknown",
                "Target visibility is not clear; choose a visible resource or a different safe standing position",
                {"target": pos, "can_see": visible, "visibility": visibility,
                 "can_dig": reachable, "executed": context["acted"]})
        expected_drops = {name}
        if name == "stone":
            expected_drops.add("cobblestone")
        if name in {"coal_ore", "deepslate_coal_ore"}:
            expected_drops.add("coal")
        try:
            dig_result = self._action(context, self.client.dig, pos, collect=True)
        except APIError as error:
            if error.code != "dig_unconfirmed":
                raise
            return self._recover_dig(pos, name, before, expected_drops, status, context, error)
        self._status(context)
        after, _ = self._inventory(context)
        remaining = self._block(pos, context)
        gained = {item: after.get(item, 0) - before.get(item, 0) for item in expected_drops if after.get(item, 0) > before.get(item, 0)}
        evidence = {"pos": pos, "gathered": name, "gained": gained,
                    "remaining_block": remaining["name"], "block_removed": remaining["name"] != name}
        if isinstance(dig_result.get("collection"), dict):
            evidence["collection"] = dig_result["collection"]
        if evidence["block_removed"] and not gained:
            self._uncollected_drops(pos, expected_drops, context, evidence)
        if remaining["name"] == name or not gained:
            raise _Failure("gather_unverified", "Block removal and expected inventory gain were not both observed", evidence)
        return evidence

    def _recover_dig(self, pos, name, before, expected_drops, initial_status, context, error):
        """Stop once, then reconcile an unconfirmed dig without replaying it."""
        evidence = {"pos": pos, "gathered": name, "backend_error": error.code,
                    "api_details": error.details, "action_state": "unknown", "recovery_attempts": 0,
                    "recovery_hint": "Do not replay this dig: the original block may be a restored client cache entry."}
        context["dig_recovery"] = evidence
        context["recovery_deadline"] = time.monotonic() + 2.0
        stage = "stop"
        try:
            # Even a cancellation received with the error must request stop.
            # Do not repeat this request in execute's ordinary failure cleanup.
            context["cleanup_attempted"] = True
            stopped = self._result(self.client.stop(**self._read_options(context)))
            if stopped.get("stopped") is not True:
                raise _Failure("stop_unverified", "Backend did not confirm the stop request")
            evidence["cleanup"] = "stop_requested"
            stage = "cancelled"
            self._check(context)
            for attempt in range(1, 4):
                evidence["recovery_attempts"] = attempt
                stage = "status"
                status = self._status(context)
                if status.get("last_death") != initial_status.get("last_death"):
                    raise _Failure("death_changed", "Death state changed while reconciling the dig")
                stage = "inventory"
                after, _ = self._inventory(context)
                stage = "block"
                remaining = self._block(pos, context)
                stage = "status_after"
                checked = self._status(context)
                if checked.get("last_death") != initial_status.get("last_death"):
                    raise _Failure("death_changed", "Death state changed while reconciling the dig")
                gained = {item: after.get(item, 0) - before.get(item, 0)
                          for item in expected_drops if after.get(item, 0) > before.get(item, 0)}
                # The API restores its cache after missing a confirmation. An
                # unchanged name therefore cannot establish that no dig happened.
                evidence.update(gained=gained, remaining_block=remaining["name"],
                                block_removed=True if remaining["name"] != name else None)
                if evidence["block_removed"] and gained:
                    evidence.update(recovered_from=error.code, action_state="verified", recovery_stage="verified")
                    evidence.pop("recovery_hint", None)
                    return evidence
                if attempt < 3:
                    stage = "wait"
                    delay = min(0.4, self._read_options(context)["timeout_seconds"])
                    if context["cancel"] is None:
                        time.sleep(delay)
                    else:
                        context["cancel"].wait(delay)
                    self._check(context)
            if evidence.get("block_removed"):
                stage = "drops"
                self._uncollected_drops(pos, expected_drops, context, evidence)
                self._check(context)
                evidence.update(action_state="block_removed", recovery_stage="verified_removal")
            else:
                stage = "verification"
                raise _Failure("unconfirmed_block_state", "No authoritative removal and inventory gain were observed")
        except Exception as recovery_error:
            evidence.update(action_state="unknown", recovery_stage=stage,
                            recovery_error=getattr(recovery_error, "code", "internal_error"))
            if stage == "stop":
                evidence["cleanup_error"] = evidence["recovery_error"]
            raise _Failure("dig_unconfirmed", "Dig result remains unknown after bounded read-only recovery", evidence) from None
        raise _Failure("gather_unverified", "Block was removed but expected inventory gain was not observed; do not dig it again", evidence)

    def _uncollected_drops(self, pos, expected_drops, context, evidence):
        """Add one bounded, read-only recovery observation without retrying dig."""
        evidence["recovery_hint"] = (
            "The block was removed. Inspect nearby drops and inventory after moving safely; do not dig this target again.")
        try:
            self._check(context)
            entities = self.client.get_entities(radius=16, types=["item"], **self._read_options(context))
            self._check(context)
            if not isinstance(entities, list):
                raise _Failure("invalid_response", "Entity query has no item list")
            drops = []
            # The upstream endpoint returns at most 40 entities. Keep this
            # bound even with a malformed or alternative API implementation.
            for entity in entities[:40]:
                if not isinstance(entity, dict) or entity.get("name") != "item":
                    continue
                item = entity.get("item")
                match = re.fullmatch(r"([a-z][a-z0-9_]{0,63})x([1-9][0-9]{0,5})", item) if isinstance(item, str) else None
                if match is None or match[1] not in expected_drops:
                    continue
                try:
                    drop_pos = self._pos(entity.get("pos"))
                except _Failure:
                    continue
                if math.dist(drop_pos, pos) > 6:
                    continue
                drop = {"name": match[1], "count": int(match[2]), "pos": drop_pos}
                if type(entity.get("id")) is int:
                    drop["id"] = entity["id"]
                drops.append(drop)
            drops.sort(key=lambda drop: math.dist(drop["pos"], pos))
            evidence["visible_drops"] = drops[:8]
        except Exception as error:
            # Supplemental diagnostics must not replace the established
            # block-removed/no-inventory-gain outcome or expose error text.
            code = getattr(error, "code", "query_failed")
            evidence["drop_query_error"] = code if isinstance(code, str) else "query_failed"

    def _craft_item(self, args, context):
        item = self._item(args["item"])
        count = args.get("count", 1)
        if type(count) is not int or not 1 <= count <= 64:
            raise _Failure("bad_args", "count must be an integer between 1 and 64")
        before, _ = self._inventory(context)
        try:
            self._action(context, self.client.craft, item, count=count)
        except Exception as error:
            if getattr(error, "code", "") in {"request_timeout", "network_error", "invalid_json", "invalid_response", "cancelled", "interrupted"}:
                raise
            after, _ = self._inventory(context)
            gained = after.get(item, 0) - before.get(item, 0)
            if gained > 0:
                raise _Failure("partial_completion", "Crafting stopped after a partial inventory gain", {"item": item, "requested": count, "gained": gained, "cause": getattr(error, "code", "internal")}) from None
            raise
        self._status(context)
        after, _ = self._inventory(context)
        gained = after.get(item, 0) - before.get(item, 0)
        data = {"item": item, "requested": count, "gained": gained, "before": before.get(item, 0), "after": after.get(item, 0)}
        if gained < count:
            raise _Failure("partial_completion" if gained > 0 else "craft_unverified", "Requested crafting increment was not fully observed", data)
        return data

    def _place_item(self, args, context):
        item = self._item(args["item"])
        pos = self._pos(args["pos"], block=True)
        if item not in self.PLACEABLE:
            raise _Failure("unsupported_item", "This item is not in the safe basic building whitelist")
        initial = self._status(context)
        self._overworld(initial)
        before, _ = self._inventory(context)
        if before.get(item, 0) < 1:
            raise _Failure("missing_item", "No requested building item is in inventory")

        def validate_site(status):
            self._overworld(status)
            feet = [math.floor(v) for v in status["pos"]]
            if pos in (feet, [feet[0], feet[1] + 1, feet[2]]):
                raise _Failure("unsafe", "Cannot place a block in the bot's body")
            target = self._block(pos, context)
            below = self._block([pos[0], pos[1] - 1, pos[2]], context)
            if target["name"] not in self.AIR or below["name"] not in self.SUPPORT or not self._safe_block(below):
                raise _Failure("unsafe", "Placement needs an empty cell above safe solid support")

        validate_site(initial)
        validate_site(self._approach(pos, context))
        self._action(context, self.client.place_block, *pos, item=item)
        self._status(context)
        placed = self._block(pos, context)
        after, _ = self._inventory(context)
        allowed = {item, "wall_torch"} if item == "torch" else {item}
        used = before.get(item, 0) - after.get(item, 0)
        if placed["name"] not in allowed or used < 1:
            raise _Failure("place_unverified", "Matching world block and item consumption were not both observed", {"pos": pos, "block": placed["name"], "used": used})
        return {"pos": pos, "placed": placed["name"], "used": used}

    def _eat_food(self, args, context):
        item = self._item(args["item"])
        if item not in self.FOODS:
            raise _Failure("unsafe_food", "Only ordinary food without harmful or teleport effects is allowed")
        status = self._status(context)
        try:
            food_before = self._number(status.get("food"), "food", 0, 20)
        except _Failure:
            raise _Failure("invalid_response", "Status has an invalid food level") from None
        if food_before >= 20:
            raise _Failure("not_hungry", "Food level is already full")
        before, _ = self._inventory(context)
        if before.get(item, 0) < 1:
            raise _Failure("missing_item", "The requested food is not in inventory")
        self._action(context, self.client.use_item, item)
        status = self._status(context)
        after, _ = self._inventory(context)
        try:
            food_after = self._number(status.get("food"), "food", 0, 20)
        except _Failure:
            raise _Failure("invalid_response", "Status has an invalid food level after eating") from None
        used = before.get(item, 0) - after.get(item, 0)
        if food_after <= food_before or used < 1:
            raise _Failure("eat_unverified", "Food increase and item consumption were not both observed", {"food_before": food_before, "food_after": food_after, "used": used})
        return {"item": item, "food_before": food_before, "food_after": food_after, "used": used}

    def _move_to(self, args, context):
        pos = self._pos(args["pos"])
        radius = self._number(args.get("range", 2), "range", 0.5, 4)
        before = self._status(context)
        distance_before = math.dist(before["pos"], pos)
        if distance_before > 32:
            raise _Failure("out_of_range", "Only local movement within 32 blocks is supported")
        already_in_range = distance_before <= radius + 0.5
        if not already_in_range:
            self._action(context, self.client.move_to, *pos, range=radius, timeout_ms=15000)
        after = self._status(context)
        distance = math.dist(after["pos"], pos)
        if distance > radius + 0.5:
            raise _Failure("move_unverified", "The observed bot position did not reach the requested range", {"pos": after["pos"], "dist": distance})
        return {"pos": after["pos"], "target": pos, "dist": distance, "range": radius,
                "moved": round(math.dist(before["pos"], after["pos"]), 3),
                "already_in_range": already_in_range, "executed": not already_in_range}

    def _retreat_from(self, args, context):
        threat = self._pos(args["pos"])
        distance = self._number(args.get("distance", 8), "distance", 2, 12)
        before = self._status(context)
        self._overworld(before)
        origin = before["pos"]
        recovery = context.get("recovery_context") or {}
        anchor = recovery.get("evacuation_origin", origin)
        excluded = {tuple(pos) for pos in recovery.get("excluded_targets", [])}
        dx, dz = origin[0] - threat[0], origin[2] - threat[2]
        norm = math.hypot(dx, dz)
        origin_distance = math.dist(origin, threat)
        anchor_horizontal = math.hypot(anchor[0] - threat[0], anchor[2] - threat[2])
        if norm < 0.1:
            raise _Failure("unsafe", "No horizontal retreat direction can be inferred",
                           {"origin": origin, "threat_pos": threat, "blocked_cells": [],
                            "target": None, "executed": False, "planned_path": None,
                            "terrain_search": {"termination": "no_direction"}})
        def feet(pos):
            return tuple(math.floor(value + 1e-4) for value in pos)

        start = feet(origin)
        y = start[1]
        cache = {}
        blocked = {}
        search = {"used": True, "nodes": 0, "block_reads": 0, "termination": "planning",
                  "solid_nodes": 0, "distance_rejections": 0, "height_guard_rejections": 0}
        # Up to five fresh swept-body/support reads before a two-block drop,
        # plus three actual landing reads afterwards.
        read_limit = 248
        planned_path = None
        target = None
        final_target = None
        edge_sweeps = {}
        outcome = {"origin": origin, "threat_pos": threat, "target": None,
                   "executed": False, "moved": 0, "planned_path": None,
                   "terrain_search": search, "blocked_cells": [], "retreat_complete": False,
                   "edge_start": False, "origin_support": {"contacts": [], "entry_rejections": {}}}
        context["retreat_evidence"] = outcome

        def reject(key, name, reason):
            # Report only a few cells already read for planning, never a map.
            if key not in blocked and len(blocked) < 4:
                blocked[key] = {"pos": list(key), "name": name, "reason": reason}
            return False

        def evidence(**extra):
            outcome.update(target=target, planned_path=planned_path, final_target=final_target,
                           blocked_cells=list(blocked.values()), terrain_search=dict(search))
            outcome.update(extra)
            return dict(outcome)

        def read(key):
            if key not in cache:
                if search["block_reads"] >= read_limit:
                    raise _Failure("retreat_budget_exhausted", "Retreat block observation budget expired")
                search["block_reads"] += 1
                try:
                    cache[key] = self._block(list(key), context)
                except Exception as error:
                    if getattr(error, "code", "") != "not_loaded":
                        raise
                    cache[key] = None
            return cache[key]

        def clear_body(key):
            block = read(key)
            if block is None:
                return reject(key, None, "not_loaded")
            if not self._safe_block(block):
                return reject(key, block["name"], "hazard")
            if block["name"] not in self.RETREAT_PASSABLE:
                return reject(key, block["name"], "body_blocked")
            return True

        def safe_cell(cell, temporary_leaves=False):
            x, cy, z = cell
            key = (x, cy - 1, z)
            ground = read(key)
            if ground is None:
                return reject(key, None, "not_loaded")
            if not self._safe_block(ground):
                return reject(key, ground["name"], "hazard")
            waterlogged = (ground.get("properties") or {}).get("waterlogged")
            leaf_support = (temporary_leaves and ground["name"] in self.RETREAT_LEAVES
                            and (waterlogged is False or waterlogged == "false"))
            if ground["name"] not in self.SUPPORT and not leaf_support:
                return reject(key, ground["name"], "unsupported_ground")
            if not clear_body((x, cy, z)) or not clear_body((x, cy + 1, z)):
                return False
            return ground["name"]

        def farther(point):
            # Descending safely may reduce 3D distance to a threat below the
            # canopy. Retreat progress is horizontal; vertical proximity is a
            # separate exclusion, never a reward for staying high in a tree.
            return math.hypot(point[0] - threat[0], point[2] - threat[2]) >= anchor_horizontal + 2

        def safe_height_band(point):
            horizontal = math.hypot(point[0] - threat[0], point[2] - threat[2])
            entering_near_height = abs(point[1] - threat[1]) <= 2.5 and abs(origin[1] - threat[1]) > 2.5
            return not entering_near_height or horizontal >= 3

        def clear_transition(current, cell):
            change = cell[1] - current[1]
            if change == 1:
                return clear_body((current[0], current[1] + 2, current[2]))
            if change < 0:
                return all(clear_body((cell[0], level, cell[2]))
                           for level in range(cell[1] + 2, current[1] + 2))
            return True

        def contact_overlap(point, cell):
            # mc-agent-api rounds position to 0.1; its physical half-width is
            # 0.3001. Require positive contact for every possible true position.
            overlaps = []
            for axis in (0, 2):
                overlaps.append(min(max(0, min(center + 0.3001, cell[axis] + 1)
                                             - max(center - 0.3001, cell[axis]))
                                    for center in (point[axis] - 0.05, point[axis] + 0.05)))
            return overlaps

        def swept_entry_cells(point, cell):
            center = [cell[0] + 0.5, cell[1], cell[2] + 0.5]
            low_x = math.floor(min(point[0] - 0.05, center[0]) - 0.3001)
            high_x = math.floor(max(point[0] + 0.05, center[0]) + 0.3001 - 1e-9)
            low_z = math.floor(min(point[2] - 0.05, center[2]) - 0.3001)
            high_z = math.floor(max(point[2] + 0.05, center[2]) + 0.3001 - 1e-9)
            return {(x, level, z) for x in range(low_x, high_x + 1) for z in range(low_z, high_z + 1)
                    for level in (cell[1], cell[1] + 1)}

        def starting_entries():
            nonlocal read_limit
            central = safe_cell(start, temporary_leaves=True)
            source = outcome["origin_support"]
            source.update(center=[start[0], start[1] - 1, start[2]], center_name=(read((start[0], start[1] - 1, start[2])) or {}).get("name"))
            if central:
                source["state"] = "center_supported"
                return [(start, [start])]
            # An unknown, liquid, or unapproved center support is not evidence
            # of an edge. Only loaded air with clear source body may use contact.
            if source["center_name"] not in self.AIR or not clear_body(start) or not clear_body((start[0], start[1] + 1, start[2])):
                source["state"] = "unverified"
                return []
            read_limit = 244  # Worst edge sweep: 9 pre-move + 3 landing reads.
            entries = []
            for x in range(math.floor(origin[0] - 0.3501), math.floor(origin[0] + 0.3501) + 1):
                for z in range(math.floor(origin[2] - 0.3501), math.floor(origin[2] + 0.3501) + 1):
                    cell = (x, y, z)
                    overlap = contact_overlap(origin, cell)
                    if cell == start or min(overlap) <= 1e-4 or cell in excluded:
                        continue
                    support = safe_cell(cell, temporary_leaves=True)
                    if not support:
                        continue
                    source["contacts"].append({"pos": [x, y - 1, z], "name": support,
                                               "minimum_overlap": [round(value, 4) for value in overlap]})
                    outcome["edge_start"] = True
                    point = [x + 0.5, y, z + 0.5]
                    if math.hypot(point[0] - threat[0], point[2] - threat[2]) < norm - 0.5 or not safe_height_band(point):
                        reason = "threat_proximity"
                    else:
                        sweep = swept_entry_cells(origin, cell)
                        reason = None if all(clear_body(key) for key in sorted(sweep)) else "swept_body_blocked"
                    if reason:
                        source["entry_rejections"][reason] = source["entry_rejections"].get(reason, 0) + 1
                        continue
                    edge_sweeps[cell] = sweep
                    entries.append((cell, [start, cell]))
            source["state"] = "edge_entry_verified" if entries else "no_verified_edge_entry"
            return entries

        def terrain_fallback():
            search.update(used=True, termination="no_safe_destination")
            entries = starting_entries()
            if not entries:
                return None, None
            blocked.clear()
            visited = {cell for cell, _ in entries}
            search["nodes"] = len(visited)
            queue = deque(entries)
            directions = sorted(((1, 0), (-1, 0), (0, 1), (0, -1)),
                                key=lambda delta: delta[0] * dx + delta[1] * dz, reverse=True)
            while queue:
                self._check(context)
                current, path = queue.popleft()
                if len(path) - 1 >= 8:
                    continue
                current_point = origin if len(path) == 1 else [current[0] + 0.5, current[1], current[2] + 0.5]
                current_distance = math.hypot(current_point[0] - threat[0], current_point[2] - threat[2])
                tolerance = 0.5 if len(path) == 1 else 1e-9
                for sx, sz in directions:
                    for step_y in (0, -1, -2, 1):
                        cell = (current[0] + sx, current[1] + step_y, current[2] + sz)
                        if cell in visited or cell in excluded:
                            continue
                        point = [cell[0] + 0.5, cell[1], cell[2] + 0.5]
                        horizontal = math.hypot(point[0] - threat[0], point[2] - threat[2])
                        if (math.hypot(point[0] - origin[0], point[2] - origin[2]) > min(distance, 8)
                                or not -6 <= point[1] - origin[1] <= 2 or horizontal < norm - 0.5
                                or horizontal + tolerance < current_distance):
                            continue
                        if not safe_height_band(point):
                            search["height_guard_rejections"] += 1
                            continue
                        if len(visited) >= 64:
                            search["termination"] = "node_budget"
                            return None, None
                        support = safe_cell(cell, temporary_leaves=True)
                        if not support:
                            continue
                        if not clear_transition(current, cell):
                            continue
                        visited.add(cell)
                        search["nodes"] = len(visited)
                        next_path = path + [cell]
                        if support in self.SUPPORT:
                            search["solid_nodes"] += 1
                            if farther(point):
                                search["termination"] = "target_found"
                                route = [[c[0] + 0.5, c[1], c[2] + 0.5] for c in next_path]
                                if outcome["edge_start"]:
                                    route[0] = list(origin)
                                return point, route
                            search["distance_rejections"] += 1
                        queue.append((cell, next_path))
                        # A two-block lower floor can coexist with a floor one
                        # block above the source; inspect that upper alternative.
                        if step_y != -2:
                            break
            return None, None

        if before.get("on_ground") is not True:
            search["termination"] = "not_grounded"
            raise _Failure("unsafe", "Wait for stable footing before planning a retreat step", evidence())
        try:
            final_target, planned_path = terrain_fallback()
            if planned_path:
                target = planned_path[1]
        except _Failure as error:
            if error.code != "retreat_budget_exhausted":
                raise
            search["termination"] = "read_budget"
        if target is None:
            raise _Failure("unsafe", "No safe retreat destination was found within the bounded terrain search", evidence())
        read_limit = 256
        def validate_start(status):
            if (feet(status["pos"]) != start or abs(status["pos"][1] - origin[1]) >= 0.5
                    or math.dist(status["pos"], origin) > 1):
                raise _Failure("unsafe", "Bot position changed while inspecting the retreat corridor", evidence(pos=status["pos"]))
            if status.get("on_ground") is not True:
                raise _Failure("unsafe", "Bot left stable footing before the retreat step", evidence())
            if outcome["edge_start"]:
                entry = feet(target)
                if (min(contact_overlap(status["pos"], entry)) <= 1e-4
                        or not swept_entry_cells(status["pos"], entry).issubset(edge_sweeps.get(entry, set()))):
                    raise _Failure("unsafe", "Edge support contact changed before the retreat step", evidence(pos=status["pos"]))

        validate_start(self._status(context))
        # Re-read the destination after planning instead of trusting only cached blocks.
        cache.clear()
        blocked.clear()
        if (not safe_cell(feet(target), temporary_leaves=True) or not clear_transition(start, feet(target))
                or not all(clear_body(key) for key in sorted(edge_sweeps.get(feet(target), set())))):
            raise _Failure("unsafe", "Retreat destination changed before movement", evidence(target=target))
        evidence()
        try:
            self._action(context, self.client.move_to, *target, range=0, timeout_ms=10000,
                         _validate_status=validate_start)
            outcome["executed"] = True
        except Exception as error:
            outcome["executed"] = context["acted"]
            outcome["candidate_failed"] = getattr(error, "code", "") in {"path_not_found", "stalled", "out_of_reach"}
            raise
        after = self._status(context)
        horizontal_after = math.hypot(after["pos"][0] - threat[0], after["pos"][2] - threat[2])
        if (feet(after["pos"]) != feet(target) or after.get("on_ground") is not True
                or horizontal_after < norm - 0.5 or not safe_height_band(after["pos"])):
            raise _Failure("retreat_unverified", "Observed movement did not reach the checked retreat step", evidence(pos=after["pos"], candidate_failed=True))
        cache.clear()
        blocked.clear()
        support = safe_cell(feet(after["pos"]), temporary_leaves=True)
        if not support:
            raise _Failure("retreat_unverified", "Retreat destination no longer has safe footing", evidence(pos=after["pos"], target=target))
        complete = support in self.SUPPORT and farther(after["pos"])
        return evidence(pos=after["pos"], target=target, moved=round(math.dist(origin, after["pos"]), 3),
                        retreat_complete=complete, evacuation_stage="ground" if complete else "relay",
                        horizontal_before=norm, horizontal_after=horizontal_after,
                        vertical_before=origin[1] - threat[1], vertical_after=after["pos"][1] - threat[1],
                        distance_before=origin_distance, distance_after=math.dist(after["pos"], threat),
                        planned_path=planned_path, path_following_verified=False,
                        path_caveat="Candidate terrain was checked; the backend may choose a different route.")

    def execute(self, name: str, args: Dict[str, Any], cancel_event=None, *, recovery_context=None) -> SkillResult:
        """Execute one registered action; partial and unverified results fail explicitly."""
        context = {"cancel": cancel_event, "acted": False}
        data = {"skill": name}
        try:
            if not isinstance(name, str) or name not in self.ARGUMENTS:
                raise _Failure("unknown_skill", "Unknown survival skill")
            allowed, required = self.ARGUMENTS[name]
            if not isinstance(args, dict) or set(args) - allowed or required - set(args):
                raise _Failure("bad_args", "Missing or unsupported survival skill arguments")
            if recovery_context is not None:
                if (name != "retreat_from" or not isinstance(recovery_context, dict)
                        or set(recovery_context) - {"excluded_targets", "evacuating", "evacuation_origin"}):
                    raise _Failure("bad_args", "Unsupported retreat recovery context")
                excluded = recovery_context.get("excluded_targets", [])
                if not isinstance(excluded, list) or len(excluded) > 8 or type(recovery_context.get("evacuating", False)) is not bool:
                    raise _Failure("bad_args", "Invalid retreat recovery context")
                context["recovery_context"] = {"excluded_targets": [self._pos(pos, block=True) for pos in excluded],
                                               "evacuating": recovery_context.get("evacuating", False)}
                if "evacuation_origin" in recovery_context:
                    context["recovery_context"]["evacuation_origin"] = self._pos(recovery_context["evacuation_origin"])
            self._check(context)
            status = self._status(context)
            context["dimension"] = status["dimension"]
            data["pos_before"] = status["pos"]
            data.update(getattr(self, "_" + name)(args, context))
            self._check(context)
            message = ("Already within the requested range; no movement was performed"
                       if name == "move_to" and data.get("already_in_range")
                       else f"{name} verified from fresh game state")
            return SkillResult(True, message, data)
        except Exception as error:
            code = getattr(error, "code", "internal_error")
            data.update(context.get("retreat_evidence", {}))
            if code == "cancelled" and context.get("dig_recovery") is not None:
                # A cancellation immediately after the final recovery read must
                # not erase the original unknown write outcome either.
                evidence = context["dig_recovery"]
                evidence.update(action_state="unknown", recovery_stage="cancelled", recovery_error="cancelled")
                evidence.pop("recovered_from", None)
                data.pop("recovered_from", None)
                error = _Failure("dig_unconfirmed", "Dig result remains unknown after cancellation", evidence)
                code = error.code
            # Preserve bounded API path diagnostics (goal, endpoint, last path
            # status) so the next model turn can change its approach.
            details = getattr(error, "details", None)
            if isinstance(details, dict) and details:
                data["api_details"] = details
            data.update(getattr(error, "data", {}))
            if context["acted"] and code != "busy" and not context.get("cleanup_attempted"):
                try:
                    self.client.stop()
                    data["cleanup"] = "stop_requested"
                except Exception as cleanup_error:
                    data["cleanup_error"] = getattr(cleanup_error, "code", "internal_error")
            return SkillResult(False, str(error), data, code)
