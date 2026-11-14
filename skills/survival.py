"""Conservative survival actions with fresh observations and result evidence.

The public API cannot establish whether a log/stone block was placed by a player.
Gathering restricts block types, not ownership. Calls already sent to the server
remain subject to their HTTP deadlines; cancellation is checked between calls.
"""

import math
import re
from typing import Any, Dict, List

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

    def _status(self, context):
        self._check(context)
        status = self._result(self.client.get_status())
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
        result = self._result(self.client.inventory())
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
        block = self._result(self.client.get_block(pos))
        self._check(context)
        if not isinstance(block.get("name"), str):
            raise _Failure("invalid_response", "Block query has no name")
        return block

    def _action(self, context, operation, *args, **kwargs):
        self._status(context)
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
        if target.get("can_see") is False:
            raise _Failure("target_occluded", "Target is hidden by terrain; choose a visible resource or a different safe standing position",
                           {"target": pos, "can_see": False, "can_dig": reachable, "executed": False})
        if not reachable:
            status = self._approach(pos, context)
        target = self._gather_guard(pos, name, status, context)
        if target.get("can_see") is False:
            raise _Failure("target_occluded", "Target became hidden after approach; choose another safe standing position",
                           {"target": pos, "can_see": False})
        dig_result = self._action(context, self.client.dig, pos, collect=True)
        self._status(context)
        after, _ = self._inventory(context)
        remaining = self._block(pos, context)
        expected_drops = {name}
        if name == "stone":
            expected_drops.add("cobblestone")
        if name in {"coal_ore", "deepslate_coal_ore"}:
            expected_drops.add("coal")
        gained = {item: after.get(item, 0) - before.get(item, 0) for item in expected_drops if after.get(item, 0) > before.get(item, 0)}
        evidence = {"pos": pos, "gathered": name, "gained": gained,
                    "remaining_block": remaining["name"], "block_removed": remaining["name"] != name}
        if isinstance(dig_result.get("collection"), dict):
            evidence["collection"] = dig_result["collection"]
        if remaining["name"] == name or not gained:
            raise _Failure("gather_unverified", "Block removal and expected inventory gain were not both observed", evidence)
        return evidence

    def _craft_item(self, args, context):
        item = self._item(args["item"])
        count = args.get("count", 1)
        if type(count) is not int or not 1 <= count <= 64:
            raise _Failure("bad_args", "count must be an integer between 1 and 64")
        before, _ = self._inventory(context)
        try:
            self._action(context, self.client.craft, item, count=count)
        except Exception as error:
            if getattr(error, "code", "") in {"request_timeout", "network_error", "cancelled", "interrupted"}:
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
        dx, dz = origin[0] - threat[0], origin[2] - threat[2]
        norm = math.hypot(dx, dz)
        if norm < 0.1:
            raise _Failure("unsafe", "No horizontal retreat direction can be inferred")
        y = math.floor(origin[1])
        cache = {}

        def safe_cell(x, z):
            for dy in (-1, 0, 1):
                key = (x, y + dy, z)
                if key not in cache:
                    try:
                        cache[key] = self._block(list(key), context)
                    except Exception as error:
                        if getattr(error, "code", "") == "not_loaded":
                            return False
                        raise
                block = cache[key]
                if not self._safe_block(block) or (block["name"] not in self.SUPPORT if dy == -1 else block["name"] not in self.AIR):
                    return False
            return True

        target = None
        for length in (distance, max(2, distance / 2)):
            for angle in (0, math.pi / 6, -math.pi / 6):
                vx = (dx * math.cos(angle) - dz * math.sin(angle)) / norm
                vz = (dx * math.sin(angle) + dz * math.cos(angle)) / norm
                candidate = [math.floor(origin[0] + vx * length) + 0.5, y, math.floor(origin[2] + vz * length) + 0.5]
                steps = math.ceil(math.dist(origin, candidate) * 2)
                cells = {(math.floor(origin[0] + (candidate[0] - origin[0]) * step / steps),
                          math.floor(origin[2] + (candidate[2] - origin[2]) * step / steps)) for step in range(steps + 1)}
                if all(safe_cell(x, z) for x, z in sorted(cells)) and math.dist(candidate, threat) > math.dist(origin, threat) + 1:
                    target = candidate
                    break
            if target is not None:
                break
        if target is None:
            raise _Failure("unsafe", "No loaded same-level retreat corridor with safe footing was found")
        latest = self._status(context)
        if math.dist(latest["pos"], origin) > 1:
            raise _Failure("unsafe", "Bot position changed while inspecting the retreat corridor")
        # Re-read the destination after planning instead of trusting only cached blocks.
        cache.clear()
        if not safe_cell(math.floor(target[0]), math.floor(target[2])):
            raise _Failure("unsafe", "Retreat destination changed before movement")
        self._action(context, self.client.move_to, *target, range=0.5, timeout_ms=10000)
        after = self._status(context)
        if math.dist(after["pos"], target) > 1 or math.dist(after["pos"], threat) <= math.dist(origin, threat) + 1:
            raise _Failure("retreat_unverified", "Observed movement did not reach safety farther from the threat", {"pos": after["pos"], "target": target})
        cache.clear()
        if not safe_cell(math.floor(after["pos"][0]), math.floor(after["pos"][2])):
            raise _Failure("retreat_unverified", "Retreat destination no longer has safe footing")
        return {"pos": after["pos"], "target": target, "threat_pos": threat,
                "distance_before": math.dist(origin, threat), "distance_after": math.dist(after["pos"], threat)}

    def execute(self, name: str, args: Dict[str, Any], cancel_event=None) -> SkillResult:
        """Execute one registered action; partial and unverified results fail explicitly."""
        context = {"cancel": cancel_event, "acted": False}
        data = {"skill": name}
        try:
            if not isinstance(name, str) or name not in self.ARGUMENTS:
                raise _Failure("unknown_skill", "Unknown survival skill")
            allowed, required = self.ARGUMENTS[name]
            if not isinstance(args, dict) or set(args) - allowed or required - set(args):
                raise _Failure("bad_args", "Missing or unsupported survival skill arguments")
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
            # Preserve bounded API path diagnostics (goal, endpoint, last path
            # status) so the next model turn can change its approach.
            details = getattr(error, "details", None)
            if isinstance(details, dict) and details:
                data["api_details"] = details
            data.update(getattr(error, "data", {}))
            if context["acted"] and code != "busy":
                try:
                    self.client.stop()
                    data["cleanup"] = "stop_requested"
                except Exception as cleanup_error:
                    data["cleanup_error"] = getattr(cleanup_error, "code", "internal_error")
            return SkillResult(False, str(error), data, code)
