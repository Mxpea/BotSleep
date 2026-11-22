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
from core.domain import hostile_threat_radius
from core.policy_relaxation import RelaxedWork, allows_atomic

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
    GATHERABLE = LOGS | {"stone", "cobblestone", "coal_ore", "deepslate_coal_ore", "dead_bush"}
    PICKAXES = frozenset(tier + "_pickaxe" for tier in ("wooden", "copper", "stone", "iron", "golden", "diamond", "netherite"))
    AIR = frozenset({"air", "cave_air", "void_air"})
    RETREAT_PASSABLE = AIR | {
        "short_grass", "tall_grass", "short_dry_grass", "tall_dry_grass",
        "fern", "large_fern", "leaf_litter", "wildflowers", "bush", "firefly_bush", "cactus_flower",
        "pink_petals", "dead_bush", "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet",
        "red_tulip", "orange_tulip", "white_tulip", "pink_tulip", "oxeye_daisy", "cornflower",
        "lily_of_the_valley", "sunflower", "lilac", "rose_bush", "peony",
        "brown_mushroom", "red_mushroom", "torch", "wall_torch", "sugar_cane",
        "vine", "glow_lichen", "hanging_roots", "moss_carpet", "pale_moss_carpet",
        "pale_hanging_moss", "eyeblossom", "open_eyeblossom", "closed_eyeblossom", "snow",
    }
    RETREAT_LEAVES = frozenset(wood + "_leaves" for wood in WOODS)
    RETREAT_TRUNKS = LOGS | frozenset(wood + "_wood" for wood in WOODS)
    HAZARDS = frozenset({"water", "lava", "bubble_column", "fire", "soul_fire", "magma_block",
                         "campfire", "soul_campfire", "cactus", "sweet_berry_bush", "powder_snow"})
    SUPPORT = LOGS | PLANKS | {
        "stone", "cobblestone", "dirt", "grass_block", "coarse_dirt", "rooted_dirt",
        "podzol", "mycelium", "mud", "packed_mud",
        "sand", "red_sand", "sandstone", "cut_sandstone", "chiseled_sandstone", "smooth_sandstone",
        "red_sandstone", "cut_red_sandstone", "chiseled_red_sandstone", "smooth_red_sandstone",
        "terracotta", "gravel",
        *(color + "_terracotta" for color in (
            "white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray",
            "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black",
        )),
        "andesite", "granite", "diorite", "deepslate", "cobbled_deepslate",
        "tuff", "calcite", "dripstone_block", "snow_block", "moss_block", "pale_moss_block",
        "crafting_table", "furnace",
    }
    PLACEABLE = LOGS | PLANKS | {
        "stone", "cobblestone", "cobbled_deepslate", "dirt", "sand", "sandstone",
        "crafting_table", "furnace", "chest", "torch",
    }
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
        # Internal safety actions; deliberately absent from the model catalog.
        "canopy_escape": (set(), set()),
        "water_escape": (set(), set()),
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
        if "navigation_settle_deadline" in context:
            remaining = context["navigation_settle_deadline"] - time.monotonic()
            if remaining <= 0:
                raise _Failure("action_unconfirmed", "Navigation landing observation budget expired")
            return {"timeout_seconds": remaining}
        if "recovery_deadline" not in context:
            return {}
        remaining = context["recovery_deadline"] - time.monotonic()
        if remaining <= 0:
            raise _Failure("recovery_budget_exhausted", "Dig recovery observation budget expired")
        return {"timeout_seconds": remaining}

    @staticmethod
    def _disconnection_error(error, context):
        details = getattr(error, "details", None)
        details = details if isinstance(details, dict) else {}
        if (getattr(error, "code", None) == "action_unconfirmed"
                and details.get("api_code") == "not_connected"):
            context["disconnected_unknown"] = True
            context["disconnection_details"] = details
        if getattr(error, "code", None) != "not_connected" or not context.get("write_sent"):
            return error
        context["disconnected_unknown"] = True
        details = {**details, "api_code": "not_connected", "action_state": "unknown", "executed": True}
        context["disconnection_details"] = details
        return APIError("action_unconfirmed", "Connection lost before the action outcome was verified",
                        getattr(error, "status_code", None), details)

    def _call_result(self, context, operation, *args, **kwargs):
        try:
            return self._result(operation(*args, **kwargs))
        except Exception as error:
            converted = self._disconnection_error(error, context)
            if converted is not error:
                raise converted from None
            raise

    def _status(self, context):
        self._check(context)
        status = self._call_result(context, self.client.get_status, **self._read_options(context))
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
        result = self._call_result(context, self.client.inventory, **self._read_options(context))
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
        block = self._call_result(context, self.client.get_block, pos, **self._read_options(context))
        self._check(context)
        if not isinstance(block.get("name"), str):
            raise _Failure("invalid_response", "Block query has no name")
        return block

    def _action(self, context, operation, *args, _validate_status=None, **kwargs):
        status = self._status(context)
        if context.get("relaxed_work") is not None:
            if operation not in (self.client.dig, self.client.craft):
                raise _Failure("unsafe_state", "Relaxed work cannot initiate navigation or another write")
            self._relaxed_work_guard(context, status)
        if context.get("cautious"):
            if operation == self.client.move_to:
                raise _Failure("unsafe_state", "Cautious bootstrap cannot initiate navigation")
            self._cautious_guard(context, status)
        if _validate_status is not None:
            _validate_status(status)
        context["acted"] = True
        if not context.get("daily_readonly_operation"):
            context["write_sent"] = True
        result = self._call_result(context, operation, *args, **kwargs)
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
        if context.get("cautious") and math.dist(status["pos"], pos) > 3:
            raise _Failure("out_of_reach", "Cautious bootstrap requires an already reachable target")
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
        if name not in self.LOGS | {"dead_bush"}:
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
        if name == "dead_bush":
            expected_drops = {"stick", "dead_bush"}
        try:
            dig_result = self._action(context, self.client.dig, pos,
                                      collect=not (context.get("cautious") or context.get("relaxed_work")))
        except APIError as error:
            if error.code != "dig_unconfirmed":
                raise
            return self._recover_dig(pos, name, before, expected_drops, status, context, error)
        self._status(context)
        after, _ = self._inventory(context)
        remaining = self._block(pos, context)
        gained = {item: after.get(item, 0) - before.get(item, 0) for item in expected_drops if after.get(item, 0) > before.get(item, 0)}
        if context.get("relaxed_work") is not None and remaining["name"] != name and not gained:
            # Vanilla drops can remain unpickable for several ticks. Give a
            # nearby drop time to enter inventory while keeping the bot still.
            deadline = time.monotonic() + 1.25
            while not gained and (remaining_time := deadline - time.monotonic()) > .25:
                if context["cancel"] is None:
                    time.sleep(.2)
                else:
                    context["cancel"].wait(.2)
                self._check(context)
                context["recovery_deadline"] = deadline
                try:
                    if not context["relaxed_work"].stable(self._status(context)):
                        break
                    after, _ = self._inventory(context)
                except (APIError, _Failure) as error:
                    if error.code not in {"request_timeout", "recovery_budget_exhausted"}:
                        raise
                    break  # The verified removal evidence is still valid.
                finally:
                    context.pop("recovery_deadline", None)
                gained = {item: after[item] - before.get(item, 0) for item in expected_drops
                          if after.get(item, 0) > before.get(item, 0)}
        evidence = {"pos": pos, "gathered": name, "gained": gained,
                    "remaining_block": remaining["name"], "block_removed": remaining["name"] != name}
        if isinstance(dig_result.get("collection"), dict):
            evidence["collection"] = dig_result["collection"]
        if evidence["block_removed"] and not gained:
            self._uncollected_drops(pos, expected_drops, context, evidence)
            if name == "dead_bush" and not evidence.get("visible_drops"):
                return evidence
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
            if getattr(error, "code", "") in {"request_timeout", "network_error", "invalid_json", "invalid_response", "cancelled", "interrupted", "not_connected", "action_unconfirmed"}:
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

    def _canopy_escape(self, args, context):
        recovery = context.setdefault("recovery_context", {})
        recovery["purpose"] = "canopy_escape"
        return self._retreat_from({"pos": [0, 0, 0], "distance": 8}, context)

    def _water_escape(self, args, context):
        before = self._status(context)
        self._overworld(before)
        if before.get("in_lava") or before.get("on_fire"):
            raise _Failure("unsafe", "Cannot run water escape while in lava or fire")
        def is_settled_dry(st):
            if st.get("in_water") or st.get("submerged"):
                return False
            # Jumping upward over water temporarily clears in_water while
            # airborne (on_ground=False); require landed ground contact when exposed.
            if "on_ground" in st and st.get("on_ground") is not None:
                return st.get("on_ground") is True
            return True

        def _confirm_dry_landing():
            nonlocal cur
            if is_settled_dry(cur):
                return True
            if not cur.get("in_water") and not cur.get("submerged") and cur.get("on_ground") is False:
                time.sleep(0.25)
                cur = self._status(context)
                return is_settled_dry(cur)
            return False

        cur = before
        if _confirm_dry_landing():
            return {"pos": before["pos"], "escaped_water": True, "stage": "already_dry", "progress_made": True}

        fatal_or_unknown = {
            "request_timeout", "network_error", "invalid_json", "invalid_response",
            "action_unconfirmed", "dig_unconfirmed", "not_connected",
            "cancelled", "dead", "not_ready", "dimension_changed", "interrupted",
            "busy", "life_changed", "lifecycle_changed", "unsafe_state",
        }

        def swim_step(**kw):
            nonlocal cur
            if not callable(getattr(self.client, "control", None)):
                return False
            context["navigation_write"] = True
            context["navigation_verified"] = False
            try:
                self._action(context, self.client.control, **kw)
            except (APIError, _Failure) as exc:
                if getattr(exc, "code", None) in fatal_or_unknown:
                    raise
                return False
            cur = self._status(context)
            return True

        # Phase 1: swim straight up if submerged or losing air, as long as overhead is safe.
        up_pulses = 0
        while (cur.get("submerged") or cur.get("oxygen", 20) < 16) and up_pulses < (8 if cur.get("submerged") else 3):
            fx = math.floor(cur["pos"][0] + 1e-4)
            fy = math.floor(cur["pos"][1] + 1e-4)
            fz = math.floor(cur["pos"][2] + 1e-4)
            head2 = self._block([fx, fy + 2, fz], context)
            if head2["name"] in (self.HAZARDS - {"water"}) or head2["name"] not in (self.RETREAT_PASSABLE | {"water", "flowing_water"}):
                break
            if not swim_step(jump=True, ms=700, wait=True):
                break
            up_pulses += 1
            if _confirm_dry_landing():
                context["navigation_verified"] = True
                return {"pos": cur["pos"], "escaped_water": True, "stage": "surfaced_dry", "progress_made": True}

        # Phase 2: locate nearest verified dry shoreline support cell (direct ring + wider find_blocks).
        cx = math.floor(cur["pos"][0] + 1e-4)
        cy = math.floor(cur["pos"][1] + 1e-4)
        cz = math.floor(cur["pos"][2] + 1e-4)
        shore_supports = []
        seen_cells = set()

        def _valid_dry_landing(bx, by, bz):
            key = (bx, by, bz)
            if key in seen_cells or (bx == cx and bz == cz):
                return None
            seen_cells.add(key)
            try:
                floor_blk = self._block([bx, by, bz], context)
                if (floor_blk["name"] not in self.SUPPORT or not self._safe_block(floor_blk)
                        or (floor_blk.get("properties") or {}).get("waterlogged") in (True, "true")):
                    return None
                feet_blk = self._block([bx, by + 1, bz], context)
                head_blk = self._block([bx, by + 2, bz], context)
                if (feet_blk["name"] not in self.RETREAT_PASSABLE or not self._safe_block(feet_blk)
                        or head_blk["name"] not in self.RETREAT_PASSABLE or not self._safe_block(head_blk)):
                    return None
                return [bx, by + 1, bz]
            except (APIError, _Failure) as exc:
                if getattr(exc, "code", None) in fatal_or_unknown:
                    raise
                return None

        for dx, dz in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1),
                       (-2, 0), (2, 0), (0, -2), (0, 2)):
            for dy in (0, -1, 1):
                landing = _valid_dry_landing(cx + dx, cy + dy, cz + dz)
                if landing is not None:
                    shore_supports.append(landing)

        if not shore_supports and callable(getattr(self.client, "get_blocks", None)):
            try:
                rows = self.client.get_blocks(
                    radius=20,
                    types=["grass_block", "dirt", "sand", "stone", "gravel", "mud", "sandstone", "cobblestone"],
                    limit=48,
                )
            except (APIError, _Failure) as exc:
                if getattr(exc, "code", None) in fatal_or_unknown:
                    raise
                rows = []
            raw_candidates = []
            for row in rows if isinstance(rows, list) else ():
                pos = row.get("pos") if isinstance(row, dict) else None
                if isinstance(pos, (list, tuple)) and len(pos) == 3 and abs(int(pos[1]) - cy) <= 3:
                    raw_candidates.append([int(pos[0]), int(pos[1]), int(pos[2])])
            raw_candidates.sort(key=lambda p: (abs(p[1] - cy), math.hypot(p[0] + 0.5 - cur["pos"][0], p[2] + 0.5 - cur["pos"][2])))
            for bx, by, bz in raw_candidates[:16]:
                landing = _valid_dry_landing(bx, by, bz)
                if landing is not None:
                    shore_supports.append(landing)
                    if len(shore_supports) >= 4:
                        break

        shore_supports.sort(key=lambda p: (0 if p[1] <= cy + 1 else 1, math.hypot(p[0] + 0.5 - cur["pos"][0], p[2] + 0.5 - cur["pos"][2])))
        for tx, ty, tz in shore_supports[:3]:
            dist = math.hypot(tx + 0.5 - cur["pos"][0], tz + 0.5 - cur["pos"][2])
            if dist <= 16.0 and not cur.get("submerged"):
                context["navigation_write"] = True
                context["navigation_verified"] = False
                try:
                    self._action(context, self.client.move_to, tx + 0.5, ty, tz + 0.5, range=0.5, timeout_ms=6500)
                except (APIError, _Failure) as exc:
                    if getattr(exc, "code", None) in fatal_or_unknown:
                        raise
                cur = self._status(context)
                if _confirm_dry_landing():
                    context["navigation_verified"] = True
                    return {"pos": cur["pos"], "target": [tx, ty, tz], "escaped_water": True, "stage": "shore_move", "progress_made": True}
            # Direct bank lip swim-hop when Mineflayer move_to stalls at shoreline edge
            if math.hypot(tx + 0.5 - cur["pos"][0], tz + 0.5 - cur["pos"][2]) <= 6.5:
                step_before_h = list(cur["pos"])
                yaw = math.atan2(-(tx + 0.5 - cur["pos"][0]), -(tz + 0.5 - cur["pos"][2]))
                swim_step(forward=True, jump=True, yaw=yaw, pitch=0.0, ms=850, wait=True)
                if _confirm_dry_landing():
                    context["navigation_verified"] = True
                    return {"pos": cur["pos"], "target": [tx, ty, tz], "escaped_water": True, "stage": "shore_swim", "progress_made": True}
                if math.hypot(cur["pos"][0] - step_before_h[0], cur["pos"][2] - step_before_h[2]) >= 0.5:
                    break

        # Phase 3: fallback directional swim toward wider shore or cardinal headings
        moved_since_start = math.hypot(cur["pos"][0] - before["pos"][0], cur["pos"][2] - before["pos"][2])
        if not is_settled_dry(cur) and moved_since_start < 0.6:
            yaw = float(cur.get("yaw", 0.0) or 0.0)
            if callable(getattr(self.client, "get_blocks", None)):
                try:
                    far_rows = self.client.get_blocks(radius=48, types=["grass_block", "sand", "stone", "dirt"], limit=16) or []
                    pts = [r["pos"] for r in far_rows if isinstance(r, dict) and isinstance(r.get("pos"), (list, tuple)) and len(r["pos"]) == 3]
                    if pts:
                        nearest_shore = min(pts, key=lambda p: math.hypot(p[0] - cur["pos"][0], p[2] - cur["pos"][2]))
                        yaw = math.atan2(-(nearest_shore[0] + 0.5 - cur["pos"][0]), -(nearest_shore[2] + 0.5 - cur["pos"][2]))
                except (APIError, _Failure) as exc:
                    if getattr(exc, "code", None) in fatal_or_unknown:
                        raise
            swim_step(forward=True, jump=True, yaw=yaw, pitch=0.0, ms=900, wait=True)
            if _confirm_dry_landing():
                context["navigation_verified"] = True
                return {"pos": cur["pos"], "escaped_water": True, "stage": "swim_dry", "progress_made": True}
            # If pressing straight toward bank moved <0.35m horizontally (steep cliff/overhang), swim laterally to skirt it.
            if math.hypot(cur["pos"][0] - before["pos"][0], cur["pos"][2] - before["pos"][2]) < 0.35:
                step_idx = (int((context.get("recovery_context") or {}).get("steps", 0) or 0) // 4) % 4
                lateral_yaw = yaw + (math.pi / 2 if step_idx % 2 == 0 else -math.pi / 2)
                swim_step(forward=True, jump=True, yaw=lateral_yaw, pitch=0.0, ms=900, wait=True)
                if _confirm_dry_landing():
                    context["navigation_verified"] = True
                    return {"pos": cur["pos"], "escaped_water": True, "stage": "swim_dry", "progress_made": True}

        context["navigation_verified"] = True
        moved_h = math.hypot(cur["pos"][0] - before["pos"][0], cur["pos"][2] - before["pos"][2])
        surfaced = bool(before.get("submerged") and not cur.get("submerged"))
        gained_air = cur.get("oxygen", 20) > before.get("oxygen", 20)
        progress_made = moved_h >= 0.8 or surfaced or gained_air
        raise _Failure(
            "still_in_water",
            "Moved toward shoreline or air; still in water",
            {"pos": cur["pos"], "progress_made": progress_made, "executed": bool(context.get("write_sent"))},
        )

    def _settle_navigation(self, context, before, target, evidence):
        """Observe a transient fall after the single move; never send recovery writes."""
        deadline = time.monotonic() + 1.5
        context["navigation_settle_deadline"] = deadline
        context["navigation_observe_only"] = True
        count = 0
        try:
            while True:
                status = self._status(context)
                count += 1
                evidence["landing_observations"] = count
                evidence["landing_pos"] = status["pos"]
                if time.monotonic() > deadline:
                    raise _Failure("action_unconfirmed", "Landing status arrived outside its observation deadline")
                if (status.get("last_death") != before.get("last_death")
                        or any(status.get(k) for k in ("in_water", "in_lava", "on_fire", "submerged"))):
                    raise _Failure("action_unconfirmed", "Life, health or body state changed before landing verification")
                same_column = all(math.floor(status["pos"][i] + .0001) == math.floor(target[i]) for i in (0, 2))
                height = status["pos"][1] - target[1]
                health_lost = max(0, before["health"] - status["health"])
                if (same_column and abs(height) <= .15 and status.get("on_ground") is True
                        and status.get("jumping") is not True):
                    evidence["landing_settled"] = True
                    if health_lost:
                        # Injury does not make an observed arrival unknown.
                        # Success still requires the fresh footing check below.
                        context["navigation_landed_hurt"] = True
                        evidence.update(health_lost=health_lost, safety_changed="health_lost")
                    return status
                if health_lost:
                    raise _Failure("action_unconfirmed", "Health fell before the checked landing was verified")
                minimum_height = -1.1 if target[1] > before["pos"][1] else -.15
                max_drop_height = max(2.1, before["pos"][1] - target[1] + 0.15)
                if not same_column or not minimum_height <= height <= max_drop_height or status.get("on_ground") is True:
                    # A stable wrong landing is known, not a transient fall.
                    raise _Failure("retreat_unverified", "Observed movement landed outside the checked step",
                                   {"pos": status["pos"], "candidate_failed": True})
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _Failure("action_unconfirmed", "Bot did not settle within the bounded landing observation")
                delay = min(.1, remaining)
                if context.get("cancel") is not None:
                    context["cancel"].wait(delay)
                else:
                    time.sleep(delay)
                self._check(context)
        finally:
            context.pop("navigation_settle_deadline", None)

    def _clear_canopy_descent(self, context, before, read, refresh, threat_check, excluded, outcome, *, allow_level=False):
        """Clear one proven obstruction, not the supporting tree or a guessed route."""
        origin = before["pos"]
        start = tuple(math.floor(v + .0001) for v in origin)
        candidates = []

        def dry_leaf(block):
            return (block is not None and block.get("name") in self.RETREAT_LEAVES
                    and (block.get("properties") or {}).get("waterlogged") in (False, "false")
                    and type((block.get("properties") or {}).get("waterlogged")) in (bool, str))

        def supported(block):
            return block is not None and self._safe_block(block) and (block["name"] in self.SUPPORT | self.RETREAT_TRUNKS or dry_leaf(block))

        def same_support(a, b):
            if not supported(a) or not isinstance(b, dict):
                return False
            if a.get("name") != b.get("name"):
                return False
            pa = a.get("properties") if isinstance(a.get("properties"), dict) else {}
            pb = b.get("properties") if isinstance(b.get("properties"), dict) else {}
            return pa.get("waterlogged") == pb.get("waterlogged")

        def source_contact(cell, point=None):
            # Include the API's rounding interval: never remove a leaf that
            # might currently support any part of the player's footprint.
            point = origin if point is None else point
            return (cell[1] == start[1] - 1 and
                    all(point[i] + .3501 > cell[i] and point[i] - .3501 < cell[i] + 1 for i in (0, 2)))

        def corridor(cell):
            support = read((cell[0], cell[1] - 1, cell[2]))
            if not supported(support):
                return None
            leaves = []
            for level in range(cell[1], start[1] + 2):
                key = (cell[0], level, cell[2])
                block = read(key)
                if block is None or not self._safe_block(block):
                    return None
                if block["name"] in self.RETREAT_PASSABLE:
                    continue
                if not dry_leaf(block) or block.get("diggable") is not True or source_contact(key):
                    return None
                leaves.append(key)
            return support, leaves

        source = read((start[0], start[1] - 1, start[2]))
        if not supported(source) or source["name"] not in self.RETREAT_LEAVES | self.RETREAT_TRUNKS:
            return None  # Edge contact and solid platforms do not authorize digging.
        allow_three_drop = self._number(before.get("health", 0), "health", 0, 1024) > 10
        drops = ((1, 2, 3, 0) if allow_three_drop else (1, 2, 0)) if allow_level else ((1, 2, 3) if allow_three_drop else (1, 2))
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            for drop in drops:
                cell = (start[0] + dx, start[1] - drop, start[2] + dz)
                if cell in excluded:
                    continue
                checked = corridor(cell)
                if checked and checked[1]:
                    candidates.append((len(checked[1]), -drop, cell, checked[1]))
        for _, _, cell, leaves in sorted(candidates):
            leaf = max(leaves, key=lambda p: p[1])
            block = read(leaf)
            if math.dist(origin, leaf) > 3 or block.get("can_dig", block.get("harvest_tool_ok")) is not True:
                continue
            neighbors = [(leaf[0] + dx, leaf[1] + dy, leaf[2] + dz)
                         for dx, dy, dz in ((0, -1, 0), (0, 1, 0), (-1, 0, 0), (1, 0, 0), (0, 0, -1), (0, 0, 1))]
            if any((read(key) is None or not self._safe_block(read(key))) for key in neighbors):
                continue
            above = read((leaf[0], leaf[1] + 1, leaf[2]))["name"]
            if above in {"sand", "red_sand", "gravel", "anvil", "chipped_anvil", "damaged_anvil"} or above.endswith("_concrete_powder"):
                continue
            visible = block.get("can_see")
            if type(visible) is not bool:
                visible = trace_block_visibility(origin, list(leaf), lambda p: read(tuple(p)),
                                                 max_queries=24, max_distance=4)["visible"]
            if visible is not True:
                continue
            # Refresh every safety-relevant cell before a write. Cached search
            # results are hints, not authority to clear a changed corridor.
            refresh()
            fresh_source = read((start[0], start[1] - 1, start[2]))
            checked = corridor(cell)
            block = read(leaf)
            if (not same_support(fresh_source, source) or checked is None or set(checked[1]) != set(leaves)
                    or not dry_leaf(block) or block.get("can_dig", block.get("harvest_tool_ok")) is not True
                    or any((read(key) is None or not self._safe_block(read(key))) for key in neighbors)):
                return None
            visible = block.get("can_see")
            if type(visible) is not bool:
                visible = trace_block_visibility(origin, list(leaf), lambda p: read(tuple(p)),
                                                 max_queries=24, max_distance=4)["visible"]
            if visible is not True:
                return None
            destination = [cell[0] + .5, cell[1], cell[2] + .5]
            support_before = checked[0]
            threat_check(before, destination)

            def stable(status):
                current = tuple(math.floor(v + .0001) for v in status["pos"])
                if (current != start or math.dist(status["pos"], origin) > .2
                        or status.get("last_death") != before.get("last_death") or status["health"] < before["health"]
                        or status.get("on_ground") is not True or status.get("gamemode", "survival") != "survival"
                        or any(status.get(k) for k in ("in_water", "in_lava", "on_fire", "submerged"))):
                    raise _Failure("safety_changed", "Canopy clearing lost its original safe stance")
                if source_contact(leaf, status["pos"]):
                    raise _Failure("safety_changed", "The final player footprint may now depend on the leaf being cleared")
                nearest = status.get("nearest_hostile") or {}
                radius = (context.get("recovery_context") or {}).get("threat_radius", 8)
                limit = max(radius, 16 if nearest.get("name") in {"skeleton", "stray", "bogged", "pillager"} else 8)
                if type(nearest.get("dist")) in (int, float) and nearest["dist"] <= limit:
                    raise _Failure("near_hostile", "A hostile appeared before the canopy clearing write")

            outcome.update(operation="clear_foliage", cleared_pos=list(leaf), cleared_name=block["name"],
                           descent_target=destination, target=None, planned_path=None, final_target=None,
                           moved=0, ground_route_verified=False, retreat_complete=False)
            context["navigation_write"] = True
            context["navigation_observe_only"] = True
            def dig_once(*args, **kwargs):
                outcome["executed"] = True
                return self.client.dig(*args, **kwargs)
            self._action(context, dig_once, list(leaf), collect=False, _validate_status=stable)
            after = self._status(context)
            try:
                stable(after)
            except _Failure as error:
                raise _Failure("action_unconfirmed", "Canopy clearing was interrupted before its outcome was verified",
                               {"recovery_error": error.code}) from None
            refresh()
            remaining = read(leaf)
            after_source = read((start[0], start[1] - 1, start[2]))
            after_support = read((cell[0], cell[1] - 1, cell[2]))
            if (remaining is None or remaining["name"] not in self.AIR or not same_support(after_source, source)
                    or not same_support(after_support, support_before)):
                raise _Failure("action_unconfirmed", "Leaf removal and both unchanged supports were not confirmed")
            context["navigation_verified"] = context["daily_verified"] = True
            outcome.update(plant_removed=True, action_state="verified", evacuation_stage="relay",
                           pos=after["pos"], clearance_remaining=len(leaves) - 1,
                           next_step_requires_replan=True, health_before=before["health"], health_after=after["health"],
                           canopy_progress={"cleared_leaves": 1, "observed_descent": start[1] - cell[1],
                                            "movement_performed": False, "ground_observed": False})
            return dict(outcome)
        return None

    def _retreat_from(self, args, context):
        threat = self._pos(args["pos"])
        distance = self._number(args.get("distance", 8), "distance", 2, 12)
        before = self._status(context)
        self._overworld(before)
        origin = before["pos"]
        recovery = context.get("recovery_context") or {}
        escaping = recovery.get("purpose") == "canopy_escape"
        anchor = recovery.get("evacuation_origin", origin)
        excluded = {tuple(pos) for pos in recovery.get("excluded_targets", []) + recovery.get("recent_targets", [])}
        if escaping:
            # This is a terrain exit, not a fabricated enemy coordinate.
            threat = list(origin)
        dx, dz = origin[0] - threat[0], origin[2] - threat[2]
        norm = math.hypot(dx, dz)
        origin_distance = math.dist(origin, threat)
        anchor_horizontal = math.hypot(anchor[0] - threat[0], anchor[2] - threat[2])
        if norm < 0.1 and not escaping:
            raise _Failure("unsafe", "No horizontal retreat direction can be inferred",
                           {"origin": origin, "threat_pos": threat, "blocked_cells": [],
                            "target": None, "executed": False, "planned_path": None,
                            "terrain_search": {"termination": "no_direction"}})
        def feet(pos):
            return tuple(math.floor(value + 1e-4) for value in pos)

        start = feet(origin)
        excluded.discard(start)
        y = start[1]
        cache = {}
        blocked = {}
        search = {"used": True, "nodes": 0, "block_reads": 0, "termination": "planning",
                  "solid_nodes": 0, "distance_rejections": 0, "height_guard_rejections": 0}
        # Up to five fresh swept-body/support reads before a two-block drop,
        # plus three actual landing reads afterwards.
        read_limit = 208 if escaping else 160  # Bound combat retreat reads while preserving canopy/multi-hop fixture coverage.
        planned_path = None
        target = None
        final_target = None
        edge_sweeps = {}
        relay_candidates = []
        best_partial = None
        outcome = {"origin": origin, "threat_pos": None if escaping else threat, "target": None,
                   "executed": False, "moved": 0, "planned_path": None,
                   "terrain_search": search, "blocked_cells": [], "retreat_complete": False,
                   "edge_start": False, "origin_support": {"contacts": [], "entry_rejections": {}},
                   "purpose": "canopy_escape" if escaping else "retreat", "ground_route_verified": False}
        context["retreat_evidence"] = outcome
        ranged_names = {"skeleton", "stray", "bogged", "pillager"}
        threats = []

        def hostile_snapshot():
            radius = recovery.get("threat_radius", 8)
            # A sampled route may descend six while moving eight horizontally;
            # include shooters beyond the origin's own danger radius as well.
            entities = self.client.get_entities(radius=min(64, max(32, radius + 12)), types=["hostile"],
                                                **self._read_options(context))
            self._check(context)
            if not isinstance(entities, list):
                raise _Failure("invalid_response", "Hostile observation has no entity list")
            if len(entities) >= 40:
                raise _Failure("unsafe", "Hostile observation reached its limit; other threats may be hidden", evidence())
            rows = []
            for entity in entities:
                if not isinstance(entity, dict):
                    raise _Failure("invalid_response", "Hostile observation contains a malformed entity")
                if entity.get("type") != "hostile":
                    continue
                try:
                    point = self._pos(entity.get("pos"))
                except _Failure:
                    raise _Failure("invalid_response", "A hostile has no valid position") from None
                rows.append({"id": entity.get("id"), "name": entity.get("name"), "pos": point,
                             "radius": max(radius, 16 if entity.get("name") in ranged_names else 8)})
            return rows

        def refresh_retreat_threats():
            nonlocal threats
            threats = hostile_snapshot()
            # Retain the caller's primary position when no current entity can
            # be matched; a missing row is not proof that a threat disappeared.
            if not any(math.dist(row["pos"], threat) <= 2 for row in threats):
                threats.append({"id": None, "name": None, "pos": list(threat),
                                "radius": max(8, recovery.get("threat_radius", 8))})
            primary = min(threats, key=lambda row: math.dist(row["pos"], threat))
            for row in threats:
                row["primary"] = row is primary
            outcome["threat_assessment"] = {"count": len(threats), "fresh": True,
                "threats": [{"id": row["id"], "name": row["name"], "pos": row["pos"],
                             "radius": row["radius"], "origin_margin": round(math.dist(origin, row["pos"]) - row["radius"], 3)}
                            for row in threats[:8]], "all_threats_evaluated": True}

        def threat_safe(point, previous=None, baseline=None, *, allow_distant_secondary=False):
            if escaping:
                return True
            baseline = origin if baseline is None else baseline
            previous = baseline if previous is None else previous
            for row in threats:
                # Preserve horizontal retreat from the primary below a crown;
                # its vertical exclusion is checked separately. Other mobs
                # retain their actual 3D danger circles, including shooters.
                gap = (lambda p: math.hypot(p[0] - row["pos"][0], p[2] - row["pos"][2])) if row["primary"] else (lambda p: math.dist(p, row["pos"]))
                initial, prior, current = gap(baseline), gap(previous), gap(point)
                if allow_distant_secondary and not row["primary"] and initial >= 7.5 and current >= 7.5:
                    continue
                # The API rounds coordinates. Allow at most 0.1 of uncertainty,
                # never cumulative deterioration along a route.
                if ((initial > row["radius"] and current <= row["radius"])
                        or prior > row["radius"] and current <= row["radius"]
                        or initial <= row["radius"] and current + .1 < initial
                        or prior <= row["radius"] and current + .1 < prior):
                    return False
                entering_height = abs(point[1] - row["pos"][1]) <= 2.5 and abs(baseline[1] - row["pos"][1]) > 2.5
                if entering_height and math.hypot(point[0] - row["pos"][0], point[2] - row["pos"][2]) < 3:
                    return False
            return True

        def threat_score(point):
            margins = [math.dist(point, row["pos"]) - row["radius"] for row in threats]
            shooters = [math.dist(point, row["pos"]) - row["radius"] for row in threats if row["name"] in ranged_names]
            return (min(shooters, default=64), min(margins, default=64),
                    math.hypot(point[0] - threat[0], point[2] - threat[2]))

        def check_escape_threats(status, destination=None):
            if not escaping:
                return
            radius = recovery.get("threat_radius", 8)
            entities = hostile_snapshot()
            nearest = status.get("nearest_hostile")
            if isinstance(nearest, dict):
                limit = max(radius, 16 if nearest.get("name") in {"skeleton", "stray", "bogged", "pillager"} else 8)
                gap = nearest.get("dist")
                if isinstance(gap, (int, float)) and not isinstance(gap, bool) and gap <= limit:
                    raise _Failure("near_hostile", "A hostile requires threat-directed retreat before canopy exploration", evidence())
            for entity in entities:
                point, limit = entity["pos"], entity["radius"]
                if any(math.dist(point, pos) <= limit for pos in (status["pos"], destination) if pos is not None):
                    raise _Failure("near_hostile", "A hostile entered the canopy exit safety radius", evidence())

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
            trunk_support = temporary_leaves and ground["name"] in self.RETREAT_TRUNKS
            if ground["name"] not in self.SUPPORT and not leaf_support and not trunk_support:
                return reject(key, ground["name"], "unsupported_ground")
            if not clear_body((x, cy, z)) or not clear_body((x, cy + 1, z)):
                return False
            return ground["name"]

        def farther(point):
            # Descending safely may reduce 3D distance to a threat below the
            # canopy. Retreat progress is horizontal; vertical proximity is a
            # separate exclusion, never a reward for staying high in a tree.
            return escaping or math.hypot(point[0] - threat[0], point[2] - threat[2]) >= anchor_horizontal + 2

        def safe_height_band(point):
            if escaping:
                return True
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
            read_limit = min(read_limit, 244)  # Worst edge sweep: 9 pre-move + 3 landing reads.
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
                    if ((not escaping and math.hypot(point[0] - threat[0], point[2] - threat[2]) < norm - 0.5)
                            or not safe_height_band(point) or not threat_safe(point)):
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

        def terrain_fallback(allow_deep_drop=False, allow_distant_secondary=False):
            nonlocal best_partial
            search.update(used=True, termination="no_safe_destination")
            entries = starting_entries()
            if not entries:
                return None, None
            source = outcome["origin_support"]
            leaf_start = (source.get("center_name") in self.RETREAT_LEAVES | self.RETREAT_TRUNKS
                          or any(row["name"] in self.RETREAT_LEAVES | self.RETREAT_TRUNKS for row in source["contacts"]))
            if escaping and not leaf_start:
                search["termination"] = "not_on_canopy"
                return None, None
            blocked.clear()
            visited = {cell for cell, _ in entries}
            search["nodes"] = len(visited)
            queue = deque(entries)
            best_partial = None
            node_cap = 96 if escaping else 64
            directions = sorted(((1, 0), (-1, 0), (0, 1), (0, -1)),
                                key=lambda delta: delta[0] * dx + delta[1] * dz, reverse=True)
            while queue:
                self._check(context)
                if escaping:
                    # Breadth-first expansion spends its read budget on a
                    # broad flat crown before seeing the descent at its edge.
                    # Prefer already verified lower terraces, then outward
                    # progress; no unobserved block is treated as walkable.
                    current, path = min(queue, key=lambda row: (
                        row[0][1],
                        -math.hypot(row[0][0] + 0.5 - anchor[0], row[0][2] + 0.5 - anchor[2]),
                        len(row[1])))
                    queue.remove((current, path))
                else:
                    # Score the step we would actually execute, not only a
                    # distant destination that could hide a dangerous turn.
                    current, path = max(queue, key=lambda row: threat_score(
                        origin if len(row[1]) == 1 else [row[1][1][0] + .5, row[1][1][1], row[1][1][2] + .5]))
                    queue.remove((current, path))
                if len(path) - 1 >= 8:
                    continue
                current_point = origin if len(path) == 1 else [current[0] + 0.5, current[1], current[2] + 0.5]
                current_distance = math.hypot(current_point[0] - threat[0], current_point[2] - threat[2])
                tolerance = 0.5 if len(path) == 1 else 1e-9
                ordered = directions if escaping else sorted(directions, key=lambda delta: threat_score(
                    [current[0] + delta[0] + .5, current[1], current[2] + delta[1] + .5]), reverse=True)
                step_ys = (
                    ((0, -1, -2, -3, -4, -5, 1) if before["health"] >= 16 else (0, -1, -2, -3, -4, 1))
                    if allow_deep_drop
                    else ((0, -1, -2, -3, 1) if (escaping and before["health"] > 10)
                          else ((0, -1, 1, -2) if (not escaping and not leaf_start) else (0, -1, -2, 1)))
                )
                min_dy = -12 if escaping else -6
                for sx, sz in ordered:
                    for step_y in step_ys:
                        cell = (current[0] + sx, current[1] + step_y, current[2] + sz)
                        if cell in visited or cell in excluded:
                            continue
                        point = [cell[0] + 0.5, cell[1], cell[2] + 0.5]
                        horizontal = math.hypot(point[0] - threat[0], point[2] - threat[2])
                        if (math.hypot(point[0] - origin[0], point[2] - origin[2]) > min(distance, 8)
                                or not min_dy <= point[1] - origin[1] <= 2
                                or (not escaping and (horizontal < norm - 0.5
                                                     or horizontal + tolerance < current_distance))):
                            continue
                        if not safe_height_band(point):
                            search["height_guard_rejections"] += 1
                            continue
                        if not threat_safe(point, current_point, allow_distant_secondary=allow_distant_secondary):
                            search["multi_threat_rejections"] = search.get("multi_threat_rejections", 0) + 1
                            continue
                        if len(visited) >= node_cap:
                            search["termination"] = "node_budget"
                            if best_partial is not None:
                                _, b_point, b_route = best_partial
                                search["termination"] = "partial_progress"
                                return b_point, b_route
                            return None, None
                        support = safe_cell(cell, temporary_leaves=True)
                        if not support or (allow_deep_drop and step_y < -3 and support not in self.SUPPORT - self.RETREAT_TRUNKS):
                            continue
                        if not clear_transition(current, cell):
                            continue
                        visited.add(cell)
                        search["nodes"] = len(visited)
                        next_path = path + [cell]
                        # A verified lower leaf terrace or outward route is
                        # useful even when the finite scan cannot yet see land.
                        # Store terrain evidence only; execution is still one
                        # adjacent step and never replays this sampled route.
                        radial_after = math.hypot(point[0] - anchor[0], point[2] - anchor[2])
                        progress = point[1] < origin[1] - 0.5
                        if escaping:
                            progress |= point[1] <= origin[1] and next_path[1][1] == start[1]
                            # A locally verified upward exit may be the only
                            # way off a tree. It remains a last-choice relay,
                            # never evidence that solid ground was reached.
                            progress |= point[1] <= origin[1] + 1 and next_path[1][1] == start[1] + 1
                        else:
                            progress |= point[1] <= origin[1] + 1 and horizontal > norm + .5
                        if leaf_start and progress:
                            relay_candidates.append((origin[1] - point[1],
                                                     radial_after if escaping else horizontal - norm,
                                                     -len(next_path), next_path))
                        if support in self.SUPPORT - self.RETREAT_TRUNKS:
                            search["solid_nodes"] += 1
                            if farther(point):
                                search["termination"] = "target_found"
                                outcome["ground_route_verified"] = True
                                route = [[c[0] + 0.5, c[1], c[2] + 0.5] for c in next_path]
                                if outcome["edge_start"]:
                                    route[0] = list(origin)
                                return point, route
                            if (not escaping and horizontal >= norm + 0.5
                                    and math.hypot(point[0] - origin[0], point[2] - origin[2]) >= 0.9):
                                p_route = [[c[0] + 0.5, c[1], c[2] + 0.5] for c in next_path]
                                if outcome["edge_start"]:
                                    p_route[0] = list(origin)
                                score = (horizontal - norm, len(p_route))
                                if best_partial is None or score > best_partial[0]:
                                    best_partial = (score, point, p_route)
                            search["distance_rejections"] += 1
                        queue.append((cell, next_path))
                        # A two-block lower floor can coexist with a floor one
                        # block above the source; inspect that upper alternative.
                        if step_y != -2:
                            break
            if best_partial is not None:
                _, b_point, b_route = best_partial
                search["termination"] = "partial_progress"
                return b_point, b_route
            return None, None

        if before.get("on_ground") is not True:
            search["termination"] = "not_grounded"
            raise _Failure("unsafe", "Wait for stable footing before planning a retreat step", evidence())
        if before.get("gamemode", "survival") != "survival" or any(before.get(key) for key in ("in_water", "in_lava", "on_fire", "submerged")):
            raise _Failure("unsafe", "Retreat requires dry survival footing", evidence())
        check_escape_threats(before)
        if not escaping:
            refresh_retreat_threats()
        try:
            final_target, planned_path = terrain_fallback()
            if planned_path:
                target = planned_path[1]
        except _Failure as error:
            if error.code != "retreat_budget_exhausted":
                raise
            if best_partial is not None:
                _, final_target, planned_path = best_partial
                target = planned_path[1]
                search["termination"] = "partial_progress"
            else:
                search["termination"] = "read_budget"
        relaxed_secondary = False
        if not escaping and target is None and search.get("multi_threat_rejections", 0) > 0:
            relaxed_secondary = True
            read_limit = min(240, max(read_limit, search["block_reads"] + 64))
            try:
                final_target, planned_path = terrain_fallback(allow_distant_secondary=True)
                if planned_path:
                    target = planned_path[1]
            except _Failure as error:
                if error.code != "retreat_budget_exhausted":
                    raise
                if best_partial is not None:
                    _, final_target, planned_path = best_partial
                    target = planned_path[1]
                    search["termination"] = "partial_progress"
                else:
                    search["termination"] = "read_budget"
        if (escaping and final_target is None and not outcome["edge_start"]
                and not any(row[0] > .5 for row in relay_candidates)):
            read_limit = 253  # Leave three reads for confirming the one clearing write.
            def refresh_clear():
                nonlocal read_limit
                cache.clear()
                if context.get("write_sent"):
                    read_limit = 256
            try:
                cleared = self._clear_canopy_descent(context, before, read, refresh_clear,
                                                    check_escape_threats, excluded, outcome,
                                                    allow_level=not relay_candidates and planned_path is None)
                if cleared is not None:
                    search["termination"] = "cleared_descent_obstruction"
                    cleared["terrain_search"] = dict(search)
                    return cleared
            except _Failure as error:
                if error.code != "retreat_budget_exhausted" or context.get("write_sent"):
                    raise
                # Insufficient proof budget authorizes no dig. A separately
                # verified non-destructive relay may still be available.
                search["clearing_skipped"] = "read_budget"
        if (escaping and target is None and planned_path is None and not relay_candidates
                and self._number(before.get("health", 0), "health", 0, 1024) >= 12):
            try:
                final_target, planned_path = terrain_fallback(allow_deep_drop=True)
                if planned_path:
                    target = planned_path[1]
            except _Failure as error:
                if error.code != "retreat_budget_exhausted":
                    raise
                search["termination"] = "read_budget"
        if target is None and relay_candidates:
            def relay_score(row):
                first = row[3][1]
                if escaping:
                    return (first[1] <= start[1], *row[:3])
                return (*threat_score([first[0] + .5, first[1], first[2] + .5]), *row[:3])
            _, _, _, route = max(relay_candidates, key=relay_score)
            planned_path = [[c[0] + 0.5, c[1], c[2] + 0.5] for c in route]
            if outcome["edge_start"]:
                planned_path[0] = list(origin)
            target = planned_path[1]
            outcome["relay_goal"] = planned_path[-1]
            outcome["canopy_progress"] = {"observed_descent": origin[1] - planned_path[-1][1],
                                          "ground_observed": False}
            search["search_boundary"] = search["termination"]
            search["termination"] = "verified_leaf_relay"
        if target is None:
            raise _Failure("unsafe", "No safe retreat destination was found within the bounded terrain search", evidence())
        read_limit = 256
        def validate_start(status):
            nearest = status.get("nearest_hostile") or {}
            if escaping and isinstance(nearest, dict):
                limit = max(16, recovery.get("threat_radius", 8)) if nearest.get("name") in {"skeleton", "stray", "bogged", "pillager"} else recovery.get("threat_radius", 8)
                distance_now = nearest.get("dist")
                if type(distance_now) in (int, float) and math.isfinite(distance_now) and distance_now <= limit:
                    raise _Failure("near_hostile", "A hostile appeared at the final canopy movement check", evidence())
            if not escaping:
                if not threat_safe(target, baseline=status["pos"], allow_distant_secondary=relaxed_secondary):
                    raise _Failure("unsafe", "Latest position makes the retreat step approach another threat", evidence())
                if isinstance(nearest, dict) and nearest:
                    gap = nearest.get("dist")
                    limit = max(recovery.get("threat_radius", 8), 16 if nearest.get("name") in ranged_names else 8)
                    if type(gap) in (int, float) and math.isfinite(gap) and gap <= limit:
                        matches = [row for row in threats if (nearest.get("id") is not None and row["id"] == nearest["id"])
                                   or (nearest.get("id") is None and row["name"] == nearest.get("name"))]
                        if not any(abs(math.dist(status["pos"], row["pos"]) - gap) <= 1.8 for row in matches):
                            raise _Failure("unsafe", "Latest hostile summary changed after the retreat threat scan", evidence())
            if status.get("last_death") != before.get("last_death") or status["health"] < before["health"]:
                raise _Failure("safety_changed", "Life or health changed while inspecting the retreat corridor", evidence())
            if (feet(status["pos"]) != start or abs(status["pos"][1] - origin[1]) >= 0.5
                    or math.dist(status["pos"], origin) > 1):
                raise _Failure("unsafe", "Bot position changed while inspecting the retreat corridor", evidence(pos=status["pos"]))
            if status.get("on_ground") is not True:
                raise _Failure("unsafe", "Bot left stable footing before the retreat step", evidence())
            if any(status.get(key) for key in ("in_water", "in_lava", "on_fire", "submerged")):
                raise _Failure("unsafe", "Body state became hazardous before movement", evidence())
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
        check_escape_threats(before, target)
        if not escaping:
            refresh_retreat_threats()
            if not all(threat_safe(point, prior, allow_distant_secondary=relaxed_secondary)
                       for prior, point in zip(planned_path, planned_path[1:])):
                raise _Failure("unsafe", "A hostile changed the checked retreat corridor before movement", evidence())
            outcome["threat_assessment"]["target_score"] = list(threat_score(target))
        try:
            context["navigation_write"] = True
            self._action(context, self.client.move_to, *target, range=0, timeout_ms=10000,
                         _validate_status=validate_start)
            outcome["executed"] = True
        except Exception as error:
            outcome["executed"] = context["acted"]
            outcome["candidate_failed"] = getattr(error, "code", "") in {"path_not_found", "stalled", "out_of_reach"}
            details = getattr(error, "details", None)
            interruption = getattr(error, "data", {})
            hurt = ((isinstance(error, APIError) and error.status_code == 200
                     and isinstance(details, dict) and details.get("cause") == "hurt")
                    or isinstance(error, _Failure) and interruption.get("interrupted_by") == "hurt")
            if not escaping and context.get("write_sent") and getattr(error, "code", "") == "interrupted" and hurt:
                cache.clear()
                observed = self._verify_interrupted_retreat(context, before, feet, safe_cell)
                if observed is not None:
                    raise _Failure("retreat_interrupted", "Retreat stopped by damage; replan from verified footing",
                                   evidence(pos=observed["pos"], landing_settled=True, candidate_failed=True,
                                       safety_changed="health_lost", api_details=details or {"cause": "hurt"})) from None
            raise
        after = self._settle_navigation(context, before, target, outcome)
        horizontal_after = math.hypot(after["pos"][0] - threat[0], after["pos"][2] - threat[2])
        if (feet(after["pos"]) != feet(target) or after.get("on_ground") is not True
                or (not escaping and horizontal_after < norm - 0.5) or not safe_height_band(after["pos"])):
            raise _Failure("retreat_unverified", "Observed movement did not reach the checked retreat step", evidence(pos=after["pos"], candidate_failed=True))
        cache.clear()
        blocked.clear()
        support = safe_cell(feet(after["pos"]), temporary_leaves=True)
        if not support:
            code = "action_unconfirmed" if context.get("navigation_landed_hurt") else "retreat_unverified"
            raise _Failure(code, "Retreat destination no longer has safe footing", evidence(pos=after["pos"], target=target))
        if escaping:
            try:
                check_escape_threats(after, after["pos"])
            except _Failure as error:
                if error.code != "near_hostile":
                    raise
                # Arrival was verified, but this is no longer an idle escape
                # situation. Return known evidence for immediate threat policy.
                outcome["retreat_required"] = True
                outcome["safety_changed"] = "near_hostile"
        complete = support in self.SUPPORT - self.RETREAT_TRUNKS and farther(after["pos"])
        context["navigation_verified"] = True
        context["daily_verified"] = True
        return evidence(pos=after["pos"], target=target, moved=round(math.dist(origin, after["pos"]), 3),
                        retreat_complete=complete, evacuation_stage="ground" if complete else "relay",
                        health_before=before["health"], health_after=after["health"],
                        horizontal_before=norm, horizontal_after=horizontal_after,
                        vertical_before=origin[1] - threat[1], vertical_after=after["pos"][1] - threat[1],
                        distance_before=origin_distance, distance_after=math.dist(after["pos"], threat),
                        planned_path=planned_path, path_following_verified=False,
                        path_caveat="Candidate terrain was checked; the backend may choose a different route.")

    def _verify_interrupted_retreat(self, context, before, feet, safe_cell):
        """Confirm a stopped movement's endpoint without repeating or stopping it again."""
        deadline = time.monotonic() + 1.5
        context.update(navigation_settle_deadline=deadline, navigation_observe_only=True)

        def stable(status):
            return (status.get("last_death") == before.get("last_death")
                    and status.get("on_ground") is True and status.get("jumping") is not True
                    and "busy" in status and status["busy"] in (None, False)
                    and not any(status.get(key) for key in ("in_water", "in_lava", "on_fire", "submerged")))

        try:
            while True:
                observed = self._status(context)
                if (observed.get("last_death") != before.get("last_death")
                        or observed["health"] >= before["health"]
                        or any(observed.get(key) for key in ("in_water", "in_lava", "on_fire", "submerged"))):
                    return None
                if stable(observed):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                delay = min(0.12, remaining)
                if context.get("cancel") is not None:
                    context["cancel"].wait(delay)
                else:
                    time.sleep(delay)
            if not safe_cell(feet(observed["pos"]), temporary_leaves=True):
                return None
            final = self._status(context)
            if (stable(final) and time.monotonic() <= deadline
                    and final["health"] == observed["health"]
                    and feet(final["pos"]) == feet(observed["pos"])
                    and math.dist(final["pos"], observed["pos"]) <= .2):
                return final
        except (APIError, _Failure):
            return None
        finally:
            context.pop("navigation_settle_deadline", None)
        return None

    def _cautious_guard(self, context, status):
        """Trusted local emergency bootstrap: no backend pickup/table travel."""
        self._overworld(status)
        baseline = context["cautious_baseline"]
        if (status.get("gamemode") != "survival"
                or not 4 < status["health"] <= 20 or status["health"] < baseline["health"]
                or self._number(status.get("food"), "food", 0, 20) < 12
                or status.get("last_death") != baseline.get("last_death")
                or math.dist(status["pos"], baseline["pos"]) > .2
                or status.get("on_ground") is not True or status.get("is_day") is not True
                or status.get("thundering") is not False
                or self._number(status.get("time_of_day"), "time_of_day", 0, 24000) >= 11000
                or any(status.get(k) for k in ("in_water", "in_lava", "on_fire", "submerged"))):
            raise _Failure("unsafe_state", "Cautious bootstrap lost its stable daylight recovery conditions")
        feet = [math.floor(v + .0001) for v in status["pos"]]
        ground = self._block([feet[0], feet[1] - 1, feet[2]], context)
        if ground["name"] not in self.SUPPORT - self.LOGS or not self._safe_block(ground):
            raise _Failure("unsafe_state", "Cautious bootstrap requires solid dry ground")
        self._check(context)
        entities = self.client.get_entities(radius=18, timeout_seconds=2)
        self._check(context)
        if not isinstance(entities, list) or len(entities) >= 40:
            raise _Failure("unsafe_state", "Cautious bootstrap needs a complete bounded entity scan")
        for entity in entities:
            if not isinstance(entity, dict):
                raise _Failure("invalid_response", "Entity scan is malformed")
            if entity.get("type") == "hostile" and math.dist(status["pos"], self._pos(entity.get("pos"))) <= 16:
                raise _Failure("unsafe_state", "A nearby enemy prevents cautious bootstrap")
        name, args = context["cautious_action"]
        basic = {"stick", "crafting_table", "wooden_pickaxe", "wooden_sword", "wooden_axe"} | self.PLANKS
        if name == "gather_block":
            pos = self._pos(args.get("pos"), block=True)
            block = self._block(pos, context)
            if (args.get("expected_name") not in self.LOGS or block["name"] != args["expected_name"]
                    or math.dist(status["pos"], pos) > 3
                    or block.get("can_dig", block.get("harvest_tool_ok")) is not True):
                raise _Failure("unsafe_state", "Cautious gathering requires a currently reachable nearby log")
            visible = block.get("can_see")
            if type(visible) is not bool:
                visible = trace_block_visibility(status["pos"], pos,
                    lambda point: self._block(point, context), max_queries=32, max_distance=4)["visible"]
            if visible is not True:
                raise _Failure("target_occluded", "Cautious gathering lost its fresh target visibility")
        elif name == "place_item":
            if args.get("item") != "crafting_table" or math.dist(status["pos"], self._pos(args.get("pos"))) > 3:
                raise _Failure("unsafe_state", "Only an adjacent workbench may be placed during cautious bootstrap")
        elif name == "craft_item" and args.get("item") in basic:
            if args["item"] in {"wooden_pickaxe", "wooden_sword", "wooden_axe"}:
                rows = self.client.get_blocks(radius=32, types=["crafting_table"], limit=64)
                self._check(context)
                positions = [self._pos(row.get("pos")) for row in rows if row.get("name") == "crafting_table"]
                if not positions or min(math.dist(status["pos"], pos) for pos in positions) > 3:
                    raise _Failure("missing_workstation", "Cautious crafting cannot walk to a distant workbench")
        else:
            raise _Failure("unsafe_state", "This action is outside cautious bootstrap")

    def _relaxed_work_guard(self, context, status):
        permit = context["relaxed_work"]
        if not permit.stable(status):
            raise _Failure("safety_changed", "Relaxed work lost its stationary safety conditions")
        name, args = context["relaxed_action"]
        x, y, z = (math.floor(v) for v in status["pos"])
        ground = self._block([x, y - 1, z], context)
        if (ground["name"] not in self.SUPPORT - self.RETREAT_TRUNKS
                or not self._safe_block(ground)
                or any(self._block([x, y + dy, z], context)["name"] not in self.AIR for dy in (0, 1))):
            raise _Failure("unsafe_state", "Relaxed work requires verified solid ground and clear body space")
        target = status["pos"]
        if name == "gather_block":
            target = self._pos(args["pos"], block=True)
            block = self._gather_guard(target, args["expected_name"], status, context)
            if (math.dist(status["pos"], target) > 2.8
                    or block.get("can_dig", block.get("harvest_tool_ok")) is not True):
                raise _Failure("out_of_reach", "Relaxed work cannot approach a log")
            visible = block.get("can_see")
            if type(visible) is not bool:
                visible = trace_block_visibility(status["pos"], target,
                    lambda point: self._block(point, context), max_queries=32, max_distance=4)["visible"]
            if visible is not True:
                raise _Failure("target_occluded", "Relaxed work needs a visible log")
        self._check(context)
        rows = self.client.get_entities(radius=min(64, max(16, permit.threat_radius) + 3), timeout_seconds=2)
        if not isinstance(rows, list) or len(rows) >= 40:
            raise _Failure("unsafe_state", "Relaxed work needs a complete bounded entity scan")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("type"), str):
                raise _Failure("unsafe_state", "Relaxed work has malformed entity evidence")
            if row["type"] == "hostile":
                pos = self._pos(row.get("pos"))
                limit = hostile_threat_radius(row.get("name"), permit.threat_radius)
                if min(math.dist(pos, status["pos"]), math.dist(pos, target)) <= limit:
                    raise _Failure("near_hostile", "New enemy evidence prevents relaxed work")
        if not permit.stable(self._status(context)):
            raise _Failure("safety_changed", "Relaxed work state changed during its final checks")

    def execute(self, name: str, args: Dict[str, Any], cancel_event=None, *, recovery_context=None,
                cautious=False, relaxed_work=None) -> SkillResult:
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
                if (name not in {"retreat_from", "canopy_escape", "water_escape"} or not isinstance(recovery_context, dict)
                        or set(recovery_context) - {"excluded_targets", "evacuating", "evacuation_origin", "purpose", "recent_targets", "threat_radius", "steps"}):
                    raise _Failure("bad_args", "Unsupported retreat recovery context")
                excluded = recovery_context.get("excluded_targets", [])
                recent = recovery_context.get("recent_targets", [])
                if (not isinstance(excluded, list) or len(excluded) > 8 or not isinstance(recent, list) or len(recent) > 8
                        or type(recovery_context.get("evacuating", False)) is not bool
                        or recovery_context.get("purpose", "retreat") not in {"retreat", "canopy_escape", "water_escape"}):
                    raise _Failure("bad_args", "Invalid retreat recovery context")
                steps_val = recovery_context.get("steps", 0)
                if type(steps_val) is not int or steps_val < 0:
                    raise _Failure("bad_args", "Invalid retreat recovery context")
                context["recovery_context"] = {"excluded_targets": [self._pos(pos, block=True) for pos in excluded],
                                               "evacuating": recovery_context.get("evacuating", False),
                                               "recent_targets": [self._pos(pos, block=True) for pos in recent],
                                               "purpose": recovery_context.get("purpose", "retreat"),
                                               "steps": steps_val,
                                               "threat_radius": self._number(recovery_context.get("threat_radius", 8), "threat_radius", 1, 64)}
                if "evacuation_origin" in recovery_context:
                    context["recovery_context"]["evacuation_origin"] = self._pos(recovery_context["evacuation_origin"])
            self._check(context)
            status = self._status(context)
            context["dimension"] = status["dimension"]
            if type(cautious) is not bool:
                raise _Failure("bad_args", "Invalid trusted cautious flag")
            if relaxed_work is not None:
                if (not isinstance(relaxed_work, RelaxedWork) or cautious
                        or not allows_atomic(name, args, relaxed_work.mode)):
                    raise _Failure("bad_args", "Unsupported relaxed-work permission")
                self._overworld(status)
                context.update(relaxed_work=relaxed_work, relaxed_action=(name, args))
                if not relaxed_work.stable(status):
                    raise _Failure("safety_changed", "Relaxed work state changed before execution")
            if cautious:
                context.update(cautious=True, cautious_baseline=dict(status), cautious_action=(name, args))
                self._cautious_guard(context, status)
                data["cautious_bootstrap"] = True
            data["pos_before"] = status["pos"]
            data.update(getattr(self, "_" + name)(args, context))
            self._check(context)
            message = ("Already within the requested range; no movement was performed"
                       if name == "move_to" and data.get("already_in_range")
                       else f"{name} verified from fresh game state")
            data.setdefault("executed", context.get("write_sent") is True)
            return SkillResult(True, message, data)
        except Exception as error:
            error = self._disconnection_error(error, context)
            code = getattr(error, "code", "internal_error")
            interrupted_details = getattr(error, "details", None)
            if code == "interrupted" and isinstance(interrupted_details, dict) and interrupted_details:
                data["api_details"] = interrupted_details
            if code == "interrupted" and getattr(error, "data", {}).get("interrupted_by"):
                data["interrupted_by"] = error.data["interrupted_by"]
            if (context.get("navigation_write") and context.get("write_sent") and not context.get("navigation_verified")
                    and code in {"request_timeout", "network_error", "invalid_json", "invalid_response", "action_unconfirmed", "dig_unconfirmed", "retreat_budget_exhausted",
                                 "cancelled", "dead", "not_ready", "dimension_changed", "interrupted", "busy",
                                 "life_changed", "lifecycle_changed", "internal", "internal_error"}):
                context["navigation_unknown"] = True
                data.update(executed=True, action_state="unknown", cleanup_skipped="navigation_unknown",
                            recovery_error=code)
                if code in {"cancelled", "dead", "not_ready", "dimension_changed", "interrupted", "busy", "life_changed", "lifecycle_changed", "retreat_budget_exhausted"}:
                    error = _Failure("action_unconfirmed", "Navigation write was interrupted before verification")
                    code = error.code
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
            if (context.get("relaxed_work") is not None and context.get("write_sent")
                    and code in {"cancelled", "interrupted", "dead", "not_ready", "busy", "dimension_changed", "unsafe_state"}):
                error = _Failure("action_unconfirmed", "Relaxed write was interrupted before its outcome was verified",
                                 {"verification_error": code, "action_state": "unknown"})
                code = error.code
            # Preserve bounded API path diagnostics (goal, endpoint, last path
            # status) so the next model turn can change its approach.
            details = getattr(error, "details", None)
            if isinstance(details, dict) and details:
                data["api_details"] = details
            data.update(getattr(error, "data", {}))
            if code == "action_unconfirmed" and context.get("write_sent"):
                data.update(executed=True, action_state="unknown")
            if context.get("disconnected_unknown"):
                data.update(cleanup_skipped="disconnected_unknown", action_state="unknown", executed=True,
                            api_details=context["disconnection_details"])
            if (context["acted"] and code != "busy" and not context.get("cleanup_attempted")
                    and not context.get("disconnected_unknown") and not context.get("navigation_unknown")
                    and not context.get("navigation_observe_only")):
                try:
                    self.client.stop()
                    data["cleanup"] = "stop_requested"
                except Exception as cleanup_error:
                    data["cleanup_error"] = getattr(cleanup_error, "code", "internal_error")
            data.setdefault("executed", context.get("write_sent") is True)
            return SkillResult(False, str(error), data, code)
