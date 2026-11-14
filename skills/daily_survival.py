"""Daily survival primitives verified against fresh world and inventory evidence.

The API cannot identify ownership, animal age, or a persistent furnace job ID.
These actions never claim those facts, replay uncertain writes, or wait for an
entire smelting job. A window inspection itself can walk/open/close a GUI.
"""

import math
import re

from core.block_geometry import trace_block_visibility
from core.client import APIError
from .survival import SurvivalSkills, _Failure


class DailySurvivalSkills(SurvivalSkills):
    PLACEABLE = SurvivalSkills.PLACEABLE | {"barrel", "smoker", "blast_furnace"}
    FOODS = SurvivalSkills.FOODS | {"beef", "porkchop", "rabbit", "mutton", "cod", "salmon", "potato"}
    CROPS = {"wheat": ("wheat", "wheat_seeds"), "carrots": ("carrot", "carrot"),
             "potatoes": ("potato", "potato"), "beetroots": ("beetroot", "beetroot_seeds")}
    SEEDS = {value[1]: key for key, value in CROPS.items()}
    IRON = frozenset({"iron_ore", "deepslate_iron_ore"})
    IRON_PICKS = frozenset({"stone_pickaxe", "iron_pickaxe", "diamond_pickaxe", "netherite_pickaxe"})
    ANIMALS = {"cow": {"beef", "leather"}, "pig": {"porkchop"},
               "sheep": {"mutton"} | {color + "_wool" for color in (
                   "white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray",
                   "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black")},
               "chicken": {"chicken", "feather"}}
    COLORS = ("white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray",
              "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black")
    BEDS = frozenset(color + "_bed" for color in COLORS)
    DOORS = frozenset(wood + "_door" for wood in SurvivalSkills.WOODS)
    ARMOR = {slot: frozenset(material + "_" + suffix for material in
                            ("leather", "copper", "chainmail", "iron", "golden", "diamond", "netherite"))
             for slot, suffix in (("head", "helmet"), ("torso", "chestplate"), ("legs", "leggings"), ("feet", "boots"))}
    SMELTABLE = {"raw_iron": "iron_ingot", "iron_ore": "iron_ingot", "deepslate_iron_ore": "iron_ingot",
                 "beef": "cooked_beef", "porkchop": "cooked_porkchop", "mutton": "cooked_mutton",
                 "chicken": "cooked_chicken", "rabbit": "cooked_rabbit", "cod": "cooked_cod",
                 "salmon": "cooked_salmon", "potato": "baked_potato",
                 **{wood + "_log": "charcoal" for wood in SurvivalSkills.WOODS}}
    FUELS = SurvivalSkills.LOGS | SurvivalSkills.PLANKS | {"coal", "charcoal", "stick"}
    FURNACES = frozenset({"furnace", "smoker", "blast_furnace"})
    CONTAINERS = frozenset({"chest", "barrel"})
    FOLIAGE = SurvivalSkills.RETREAT_LEAVES | {"azalea_leaves", "flowering_azalea_leaves", "vine"}
    UNKNOWN = frozenset({"request_timeout", "network_error", "invalid_json", "invalid_response",
                         "internal", "internal_error", "dig_unconfirmed", "action_unconfirmed"})
    ARGUMENTS = {**SurvivalSkills.ARGUMENTS,
                **{name: (set(keys), set(keys)) for name, keys in {
                    "gather_resource": ("pos", "expected_name"), "harvest_crop": ("pos", "expected_name"),
                    "gather_seeds": ("pos", "expected_name"),
                    "clear_foliage": ("pos", "expected_name"),
                    "plant_crop": ("pos", "item"), "till_soil": ("pos",),
                    "hunt_animal": ("entity_id", "expected_name"), "equip_armor": ("item", "slot"),
                    "smelt_start": ("pos", "input_item", "count", "fuel_item", "fuel_count"),
                    "smelt_collect": ("pos", "item", "count"), "inspect_container": ("pos",),
                    "store_items": ("pos", "item", "count"), "take_items": ("pos", "item", "count"),
                    "collect_drops": ("entity_id", "expected_item"),
                    "place_bed": ("pos", "item"), "place_door": ("pos", "item"),
                }.items()}}

    def execute(self, name, args, cancel_event=None, *, recovery_context=None):
        result = super().execute(name, args, cancel_event, recovery_context=recovery_context)
        if name not in SurvivalSkills.ARGUMENTS:
            result.data.setdefault("executed", False)
            if (not result.success and result.data["executed"] and result.data.get("action_state") != "verified"
                    and result.error_code in {"cancelled", "dead", "interrupted", "dimension_changed", "life_changed", "lifecycle_changed", "not_ready"}):
                result.data.setdefault("interruption_code", result.error_code)
                result.error_code = "action_unconfirmed"
                result.message = "Daily write was interrupted before its outcome could be verified"
            if not result.success and result.data["executed"] and result.error_code in self.UNKNOWN:
                result.data["action_state"] = "unknown"
        return result

    @staticmethod
    def _check(context):
        if context.get("cancel") is not None and context["cancel"].is_set():
            evidence = context.get("retreat_evidence", {})
            if evidence.get("executed") and not context.get("daily_verified"):
                raise _Failure("action_unconfirmed", "Cancelled before the daily write result was verified",
                               {**evidence, "action_state": "unknown", "recovery_error": "cancelled"})
        SurvivalSkills._check(context)

    def _prepare(self, context):
        context.setdefault("retreat_evidence", {"executed": False})
        status = self._status(context)
        death = status.get("last_death")
        context.setdefault("daily_life", death.get("t") if isinstance(death, dict) else None)
        self._safe_status(status)
        return status

    def _status(self, context):
        status = super()._status(context)
        if "daily_life" in context:
            death = status.get("last_death")
            current = death.get("t") if isinstance(death, dict) else None
            if current != context["daily_life"]:
                executed = context.get("retreat_evidence", {}).get("executed")
                raise _Failure("action_unconfirmed" if executed else "lifecycle_changed",
                               "Life changed during the daily task; previous inventory evidence cannot be reused",
                               {"action_state": "unknown" if executed else "not_executed"})
        return status

    def _safe_status(self, status):
        self._overworld(status)
        if status.get("gamemode", "survival") != "survival" or status.get("on_ground") is not True:
            raise _Failure("unsafe_state", "Daily actions require survival mode and stable ground")
        if any(status.get(key) for key in ("in_water", "in_lava", "on_fire", "submerged")):
            raise _Failure("unsafe", "Daily actions cannot run in a hazardous body state")
        hostile = status.get("nearest_hostile")
        if isinstance(hostile, dict) and isinstance(hostile.get("dist"), (int, float)) and hostile["dist"] < 8:
            raise _Failure("unsafe", "A hostile entity is too close for a daily task")

    def _daily_action(self, context, operation, *args, readonly=False, validate=None, **kwargs):
        def guard(status):
            self._safe_status(status)
            if validate is not None:
                validate(status)

        def send(*a, **kw):
            if not readonly:
                context["retreat_evidence"]["executed"] = True
            return operation(*a, **kw)

        context["daily_readonly_operation"] = readonly
        try:
            return self._action(context, send, *args, _validate_status=guard, **kwargs)
        finally:
            context.pop("daily_readonly_operation", None)

    def _action(self, context, operation, *args, _validate_status=None, **kwargs):
        def send(*a, **kw):
            if "retreat_evidence" in context and not context.get("daily_readonly_operation") and not context.get("inspect_only"):
                context["retreat_evidence"]["executed"] = True
            return operation(*a, **kw)
        return super()._action(context, send, *args, _validate_status=_validate_status, **kwargs)

    @staticmethod
    def _verified(context, **data):
        context["daily_verified"] = True
        evidence = context["retreat_evidence"]
        evidence.update(data, verified=True, action_state="verified")
        return dict(evidence)

    @staticmethod
    def _count(value, maximum=64):
        if type(value) is not int or not 1 <= value <= maximum:
            raise _Failure("bad_args", f"count must be an integer from 1 to {maximum}")
        return value

    def _available(self, item, count, context):
        counts, items = self._inventory(context)
        if counts.get(item, 0) < count:
            raise _Failure("missing_item", "Requested quantity is not in inventory")
        return counts, items

    def _visible_target(self, pos, name, context, *, crop=False):
        status = self._prepare(context)
        target = self._gather_guard(pos, name, status, context)
        if target.get("harvest_tool_ok") is not True or math.dist(status["pos"], pos) > 4:
            status = self._approach(pos, context)
        target = self._gather_guard(pos, name, status, context)
        if crop and target.get("mature") is not True:
            raise _Failure("not_mature", "Only a freshly observed mature crop may be harvested")
        sight = trace_block_visibility(status["pos"], pos, lambda p: self._block(p, context), max_queries=32, max_distance=8)
        if sight["visible"] is not True:
            raise _Failure("target_occluded" if sight["visible"] is False else "visibility_unknown",
                           "The harvesting target is not visibly reachable", {"visibility": sight})
        return status

    def _dig_daily(self, pos, name, expected, context, *, crop=False):
        status = self._visible_target(pos, name, context, crop=crop)
        before, _ = self._inventory(context)
        try:
            self._daily_action(context, self.client.dig, pos, collect=True)
        except APIError as error:
            if error.code != "dig_unconfirmed":
                raise
            data = self._recover_dig(pos, name, before, expected, status, context, error)
            return self._verified(context, **data)
        self._prepare(context)
        after, _ = self._inventory(context)
        remaining = self._block(pos, context)
        gained = {item: after.get(item, 0) - before.get(item, 0) for item in expected if after.get(item, 0) > before.get(item, 0)}
        evidence = {"pos": pos, "gathered": name, "remaining_block": remaining["name"],
                    "block_removed": remaining["name"] != name, "gained": gained}
        if not evidence["block_removed"] or not gained:
            if evidence["block_removed"]:
                self._uncollected_drops(pos, expected, context, evidence)
            raise _Failure("gather_unverified", "Harvest requires block removal and an actual expected inventory gain", evidence)
        return self._verified(context, **evidence)

    def _gather_resource(self, args, context):
        pos, name = self._pos(args["pos"], block=True), self._item(args["expected_name"])
        self._prepare(context)
        if name not in self.IRON:
            raise _Failure("unsupported_block", "Daily resource gathering currently supports iron ores only")
        counts, _ = self._inventory(context)
        if not any(counts.get(item, 0) for item in self.IRON_PICKS):
            raise _Failure("missing_tool", "Iron ore requires a stone, iron, diamond or netherite pickaxe")
        return self._dig_daily(pos, name, {"raw_iron", name}, context)

    def _harvest_crop(self, args, context):
        pos, name = self._pos(args["pos"], block=True), self._item(args["expected_name"])
        if name not in self.CROPS:
            raise _Failure("unsupported_crop", "Only wheat, carrots, potatoes and beetroots are supported")
        return self._dig_daily(pos, name, {self.CROPS[name][0]}, context, crop=True)

    def _gather_seeds(self, args, context):
        """Remove one visible grass plant; random zero-seed drops are legitimate."""
        requested = self._pos(args["pos"], block=True)
        name = self._item(args["expected_name"])
        if name not in {"short_grass", "tall_grass"}:
            raise _Failure("unsupported_plant", "Only short_grass and tall_grass may be sampled for seeds")
        status = self._prepare(context)
        first = self._block(requested, context)
        pos = list(requested)
        if name == "tall_grass" and first.get("properties", {}).get("half") == "upper":
            pos[1] -= 1
        upper = [pos[0], pos[1] + 1, pos[2]] if name == "tall_grass" else None

        def site(current):
            if math.dist(current["pos"], pos) > 4:
                raise _Failure("needs_position", "Seed gathering requires a nearby plant; no implicit approach is performed")
            target = self._gather_guard(pos, name, current, context)
            if upper is not None:
                head = self._block(upper, context)
                if (target.get("properties", {}).get("half") != "lower" or head["name"] != name
                        or head.get("properties", {}).get("half") != "upper"):
                    raise _Failure("target_changed", "Tall grass must have both matching plant halves")
                self._gather_guard(upper, name, current, context)
            soil = self._block([pos[0], pos[1] - 1, pos[2]], context)
            if soil["name"] not in {"grass_block", "dirt", "coarse_dirt", "podzol", "rooted_dirt", "moss_block", "pale_moss_block"}:
                raise _Failure("unsafe", "Grass needs freshly observed natural soil support")
            sight = trace_block_visibility(current["pos"], pos, lambda p: self._block(p, context), max_queries=32, max_distance=8)
            if sight["visible"] is not True:
                raise _Failure("target_occluded" if sight["visible"] is False else "visibility_unknown",
                               "The seed plant is not visibly reachable", {"visibility": sight})

        site(status)
        before, _ = self._inventory(context)
        recovered = None
        try:
            self._daily_action(context, self.client.dig, pos, collect=True, validate=site)
        except (APIError, _Failure) as error:
            if error.code != "dig_unconfirmed":
                if error.code in self.UNKNOWN:
                    # An uncertain transport outcome must not trigger another write.
                    context["cleanup_attempted"] = True
                raise
            try:
                recovered = self._recover_dig(pos, name, before, {"wheat_seeds"}, status, context, error)
            except _Failure as failure:
                if failure.code != "gather_unverified" or failure.data.get("action_state") != "block_removed":
                    raise
                recovered = failure.data
            recovered["recovered_from"] = error.code
        try:
            self._prepare(context)
            after, _ = self._inventory(context)
            remaining = self._block(pos, context)
            upper_remaining = self._block(upper, context) if upper is not None else None
            removed = remaining["name"] in self.AIR and (upper_remaining is None or upper_remaining["name"] in self.AIR)
            if not removed:
                raise _Failure("plant_unverified", "The entire grass plant was not observed removed")
        except Exception as error:
            if getattr(error, "code", None) in self.UNKNOWN:
                context["cleanup_attempted"] = True
            if recovered is not None:
                raise _Failure("dig_unconfirmed", "Grass removal could not be fully verified after recovery",
                               {**recovered, "action_state": "unknown", "recovery_stage": "plant_verification",
                                "recovery_error": getattr(error, "code", "internal_error")}) from None
            raise
        gained = max(0, after.get("wheat_seeds", 0) - before.get("wheat_seeds", 0))
        evidence = {**(recovered or {}), "pos": pos, "requested_pos": requested,
                    "plant": name, "plant_removed": True, "block_removed": True,
                    "gained": gained, "gained_items": {"wheat_seeds": gained} if gained else {},
                    "completed": gained > 0, "seed_drop_guaranteed": False}
        return self._verified(context, **evidence)

    def _clear_foliage(self, args, context):
        """Clear one adjacent body-height obstacle, without collecting or walking."""
        pos, name = self._pos(args["pos"], block=True), self._item(args["expected_name"])
        if name not in self.FOLIAGE:
            raise _Failure("unsupported_foliage", "Only known leaves and vine may be cleared")
        initial = self._prepare(context)
        initial_feet = [math.floor(value + 1e-4) for value in initial["pos"]]

        def site(status):
            feet = [math.floor(value + 1e-4) for value in status["pos"]]
            if feet != initial_feet or abs(status["pos"][1] - initial["pos"][1]) >= .5:
                raise _Failure("position_changed", "The stance changed before clearing foliage")
            if (status.get("is_day") is not True
                    or type(status.get("time_of_day")) not in (int, float)
                    or not 0 <= status["time_of_day"] < 11000
                    or status.get("thundering")):
                raise _Failure("unsafe_time", "Foliage clearing requires freshly confirmed daylight")
            if (self._number(status.get("health"), "health", 0, 1024) < 12
                    or self._number(status.get("food"), "food", 0, 20) < 8):
                raise _Failure("unsafe_state", "Foliage clearing requires health >= 12 and food >= 8")
            if (max(abs(pos[0] - feet[0]), abs(pos[2] - feet[2])) != 1
                    or pos[1] not in (feet[1], feet[1] + 1)
                    or math.dist(status["pos"], pos) > 3):
                raise _Failure("unsafe_target", "Only a horizontally adjacent eight-neighbor body-height foliage cell may be cleared")
            target = self._gather_guard(pos, name, status, context)
            if name != "vine" and target.get("properties", {}).get("waterlogged") not in (False, "false"):
                raise _Failure("unsafe", "Leaves must be explicitly observed as not waterlogged")
            sight = trace_block_visibility(status["pos"], pos, lambda p: self._block(p, context), max_queries=24, max_distance=4)
            if sight["visible"] is not True:
                raise _Failure("target_occluded" if sight["visible"] is False else "visibility_unknown",
                               "The foliage obstacle is not visibly reachable", {"visibility": sight})

        site(initial)
        try:
            self._daily_action(context, self.client.dig, pos, collect=False, validate=site)
        except (APIError, _Failure) as error:
            if error.code in self.UNKNOWN:
                # The one write may still have executed. Do not add a stop,
                # recovery dig, drop collection or read after an unknown reply.
                context["cleanup_attempted"] = True
            raise
        try:
            after = self._status(context)
            remaining = self._block(pos, context)
            self._status(context)
            if remaining["name"] not in self.AIR:
                raise _Failure("foliage_not_removed", "The target has not been freshly observed as air")
        except Exception as error:
            context["cleanup_attempted"] = True
            raise _Failure("action_unconfirmed", "The foliage write outcome could not be confirmed",
                           {"action_state": "unknown", "verification_error": getattr(error, "code", "internal_error"),
                            "pos": pos, "expected_name": name}) from None
        return self._verified(context, pos=pos, cleared=name, block_removed=True,
                              remaining_block=remaining["name"], collected=False,
                              inventory_verified=False, pos_after=after["pos"])

    def _plant_crop(self, args, context):
        pos, item = self._pos(args["pos"], block=True), self._item(args["item"])
        self._prepare(context)
        if item not in self.SEEDS:
            raise _Failure("unsupported_crop", "This item is not a supported crop seed")
        before, _ = self._available(item, 1, context)
        above = [pos[0], pos[1] + 1, pos[2]]

        def site(status):
            soil, space = self._block(pos, context), self._block(above, context)
            if soil["name"] != "farmland" or space["name"] not in self.AIR or not self._safe_block(soil):
                raise _Failure("unsafe", "Planting requires farmland with clear air above it")

        site(self._approach(pos, context))
        self._daily_action(context, self.client.interact, pos=pos, item=item, validate=site)
        self._prepare(context)
        after, _ = self._inventory(context)
        planted = self._block(above, context)
        used = before.get(item, 0) - after.get(item, 0)
        if planted["name"] != self.SEEDS[item] or used < 1:
            raise _Failure("plant_unverified", "Crop block and seed consumption were not both observed", {"used": used, "block": planted["name"]})
        return self._verified(context, pos=above, planted=planted["name"], used=used)

    def _till_soil(self, args, context):
        pos = self._pos(args["pos"], block=True)
        self._prepare(context)
        counts, _ = self._inventory(context)
        hoes = [tier + "_hoe" for tier in ("netherite", "diamond", "iron", "stone", "copper", "wooden", "golden")]
        hoe = next((name for name in hoes if counts.get(name, 0)), None)
        if hoe is None:
            raise _Failure("missing_tool", "A hoe is required to till soil")
        sources = self.client.find_blocks(["water"], radius=6, limit=64)
        self._check(context)
        water = None
        for row in sources:
            p = self._pos(row.get("pos"), block=True)
            if abs(p[0] - pos[0]) <= 4 and abs(p[2] - pos[2]) <= 4 and p[1] in (pos[1], pos[1] + 1):
                if self._block(p, context)["name"] == "water":
                    water = p
                    break
        if water is None:
            raise _Failure("no_water", "No freshly verified hydration water is within four horizontal blocks")

        def site(status):
            soil = self._block(pos, context)
            above = self._block([pos[0], pos[1] + 1, pos[2]], context)
            if soil["name"] not in {"dirt", "grass_block"} or above["name"] not in self.AIR:
                raise _Failure("unsafe", "Tilling requires clear ordinary dirt or grass")
            if self._block(water, context)["name"] != "water":
                raise _Failure("target_changed", "Hydration water disappeared")

        site(self._approach(pos, context))
        self._daily_action(context, self.client.interact, pos=pos, item=hoe, validate=site)
        self._prepare(context)
        block = self._block(pos, context)
        if block["name"] != "farmland":
            raise _Failure("till_unverified", "Farmland was not observed after hoe use")
        return self._verified(context, pos=pos, tilled="farmland", water=water)

    def _entity(self, entity_id, expected_name, context, *, dropped=False):
        if type(entity_id) is not int or entity_id < 0:
            raise _Failure("bad_args", "entity_id must be a nonnegative integer")
        entities = self.client.get_entities(radius=16, types=["item"] if dropped else None)
        self._check(context)
        entity = next((e for e in entities if e.get("id") == entity_id), None)
        if entity is None or (not dropped and entity.get("name") != expected_name):
            raise _Failure("target_changed", "The requested entity is not freshly observed with the expected identity")
        if entity.get("type") == "player":
            raise _Failure("unsafe_target", "Players cannot be a daily action target")
        entity = dict(entity, pos=self._pos(entity.get("pos")))
        if dropped and (entity.get("name") != "item" or not re.fullmatch(re.escape(expected_name) + r"x[1-9][0-9]*", entity.get("item", ""))):
            raise _Failure("target_changed", "Dropped entity does not contain the expected item")
        return entity

    def _hunt_animal(self, args, context):
        name = self._item(args["expected_name"])
        if name not in self.ANIMALS:
            raise _Failure("unsafe_target", "Only cows, pigs, sheep and chickens may be hunted")
        status = self._prepare(context)
        if status["health"] < 12 or status.get("food", 0) < 10:
            raise _Failure("unsafe", "Hunting requires at least 12 health and 10 food")
        entity = self._entity(args["entity_id"], name, context)
        before, _ = self._inventory(context)
        if not any(count and (item.endswith("_sword") or item.endswith("_axe")) for item, count in before.items()):
            raise _Failure("missing_tool", "A sword or axe is required for bounded hunting")
        if math.dist(status["pos"], entity["pos"]) > 8:
            raise _Failure("out_of_range", "Hunting is limited to observed animals within eight blocks")

        def target(status):
            fresh = self._entity(args["entity_id"], name, context)
            if math.dist(status["pos"], fresh["pos"]) > 8:
                raise _Failure("target_changed", "The animal moved outside the hunting limit")

        result = self._daily_action(context, self.client.attack, args["entity_id"], timeout_ms=6000, max_hits=6, validate=target)
        self._prepare(context)
        after, _ = self._inventory(context)
        gained = {item: after.get(item, 0) - before.get(item, 0) for item in self.ANIMALS[name] if after.get(item, 0) > before.get(item, 0)}
        data = {"entity_id": args["entity_id"], "animal": name, "gained": gained, "killed_reported": result.get("killed") is True}
        if not gained:
            self._uncollected_drops(entity["pos"], self.ANIMALS[name], context, data)
            raise _Failure("hunt_unverified", "The attack did not produce a verified inventory gain", data)
        return self._verified(context, **data)

    def _equip_armor(self, args, context):
        item, slot = self._item(args["item"]), args["slot"]
        self._prepare(context)
        if not isinstance(slot, str) or item not in self.ARMOR.get(slot, ()):
            raise _Failure("bad_args", "Armor item does not match a supported equipment slot")
        self._available(item, 1, context)
        self._daily_action(context, self.client.equip, item, slot=slot)
        self._prepare(context)
        inv = self._result(self.client.inventory())
        armor = inv.get("armor", {}).get("chest" if slot == "torso" else slot)
        if not isinstance(armor, dict) or armor.get("name") != item:
            raise _Failure("equip_unverified", "The requested armor was not observed in its armor slot")
        return self._verified(context, item=item, slot=slot, equipped=armor)

    def _window(self, pos, context, names):
        block = self._block(pos, context)
        if block["name"] not in names:
            raise _Failure("unsupported_container", "Target is not a supported observed container")
        self._approach(pos, context)
        def target(status):
            if self._block(pos, context)["name"] != block["name"]:
                raise _Failure("target_changed", "The observed container changed before opening")
        window = self._daily_action(context, self.client.window_view, pos, readonly=True, validate=target)
        context["window_target"] = (list(pos), block["name"])
        sections = window.get("sections")
        if not isinstance(sections, dict) or any(not isinstance(value, list) for value in sections.values()):
            raise _Failure("invalid_response", "Window has invalid sections")
        for rows in sections.values():
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("name"), str) or type(row.get("count")) is not int or row["count"] <= 0:
                    raise _Failure("invalid_response", "Window contains an invalid item row")
        expected = {"input", "fuel", "output"} if block["name"] in self.FURNACES else {"container"}
        if not expected.issubset(sections):
            raise _Failure("invalid_response", "Window sections do not match the target block")
        return window

    @staticmethod
    def _section_count(window, section, item):
        return sum(row["count"] for row in window["sections"][section] if row["name"] == item)

    def _window_write(self, pos, ops, context):
        def target(status):
            expected = context.get("window_target")
            if not expected or expected[0] != pos or self._block(pos, context)["name"] != expected[1]:
                raise _Failure("target_changed", "The inspected container changed before transfer")
        def transfer(*args):
            response = self.client.window_act(*args)
            result = response.get("result") if isinstance(response, dict) else None
            if isinstance(result, dict) and isinstance(result.get("done"), list):
                context["retreat_evidence"]["transferred"] = result["done"]
            return response
        try:
            result = self._daily_action(context, transfer, pos, ops, validate=target)
        except (APIError, _Failure) as error:
            if isinstance(error, _Failure) and error.code != "interrupted":
                raise
            details = getattr(error, "details", None)
            details = details if isinstance(details, dict) else getattr(error, "data", {})
            done = details.get("done", context["retreat_evidence"].get("transferred", []))
            context["retreat_evidence"]["transferred"] = done
            # An operation may transfer items before its own `done` entry is
            # appended. A thrown transport/click error cannot prove zero writes.
            if error.code in {"window_op_failed", "window_closed", "interrupted"}:
                raise _Failure("action_unconfirmed", "Window transfer stopped with an uncertain in-flight operation",
                               {"action_state": "unknown", "api_code": error.code, "api_details": details,
                                "transferred": done}) from None
            if details.get("done") and error.code not in self.UNKNOWN:
                raise _Failure("partial_completion", "Only part of the window operation completed; re-observe before planning",
                               {"transferred": details["done"], "api_details": details, "completed": False}) from None
            raise
        context["retreat_evidence"]["transferred"] = result.get("done", [])
        return result

    def _inspect_container(self, args, context):
        pos = self._pos(args["pos"], block=True)
        context["inspect_only"] = True
        self._prepare(context)
        window = self._window(pos, context, self.CONTAINERS | self.FURNACES)
        self._prepare(context)
        return self._verified(context, pos=pos, window=window, inventory_mutated=False)

    def _smelt_start(self, args, context):
        pos = self._pos(args["pos"], block=True)
        item, fuel = self._item(args["input_item"]), self._item(args["fuel_item"])
        count, fuel_count = self._count(args["count"], 16), self._count(args["fuel_count"], 16)
        self._prepare(context)
        if item not in self.SMELTABLE or fuel not in self.FUELS:
            raise _Failure("unsupported_recipe", "The smelting input or fuel is not supported")
        before, _ = self._available(item, count + (fuel_count if item == fuel else 0), context)
        self._available(fuel, fuel_count, context)
        window = self._window(pos, context, self.FURNACES)
        kind = self._block(pos, context)["name"]
        if (kind == "smoker" and not self.SMELTABLE[item].startswith("cooked_") and item != "potato"
                or kind == "blast_furnace" and self.SMELTABLE[item] != "iron_ingot"):
            raise _Failure("unsupported_recipe", "This furnace type cannot process that input")
        if any(window["sections"][section] for section in ("input", "fuel", "output")) or window.get("info", {}).get("burning"):
            raise _Failure("container_busy", "Starting a job requires an empty idle furnace; inspect or collect the existing job")
        self._window_write(pos, [{"op": "put", "item": item, "count": count, "to": "input"},
                                 {"op": "put", "item": fuel, "count": fuel_count, "to": "fuel"}], context)
        self._prepare(context)
        after, _ = self._inventory(context)
        current = self._window(pos, context, self.FURNACES)
        expected = self.SMELTABLE[item]
        consumed = before.get(item, 0) - after.get(item, 0)
        fuel_used = before.get(fuel, 0) - after.get(fuel, 0)
        expected_used = count + fuel_count if item == fuel else count
        load = self._section_count(current, "input", item) + self._section_count(current, "output", expected)
        data = {"pos": pos, "stage": "started", "completed": False, "input_item": item, "expected_output": expected,
                "count": count, "fuel_item": fuel, "fuel_count": fuel_count, "window": current,
                "inventory_removed": {item: consumed, fuel: fuel_used}}
        if consumed < expected_used or fuel_used < (expected_used if item == fuel else fuel_count) or load < count:
            raise _Failure("smelt_unverified", "The complete requested input and fuel transfer was not independently observed", data)
        return self._verified(context, **data)

    def _smelt_collect(self, args, context):
        return self._take_or_store(args, context, take=True, furnace=True)

    def _store_items(self, args, context):
        return self._take_or_store(args, context, take=False)

    def _take_items(self, args, context):
        return self._take_or_store(args, context, take=True)

    def _take_or_store(self, args, context, *, take, furnace=False):
        pos, item, count = self._pos(args["pos"], block=True), self._item(args["item"]), self._count(args["count"])
        self._prepare(context)
        before, _ = self._inventory(context)
        window = self._window(pos, context, self.FURNACES if furnace else self.CONTAINERS)
        section = "output" if furnace else "container"
        stored = self._section_count(window, section, item)
        if (take and stored < count) or (not take and before.get(item, 0) < count):
            raise _Failure("not_ready" if furnace else "missing_item", "The requested quantity is not available to transfer")
        if furnace and item not in self.SMELTABLE.values():
            raise _Failure("unsupported_recipe", "This is not a supported smelting output")
        op = {"op": "take", "from": section, "item": item, "count": count} if take else {"op": "put", "to": section, "item": item, "count": count}
        self._window_write(pos, [op], context)
        self._prepare(context)
        after, _ = self._inventory(context)
        current = self._window(pos, context, self.FURNACES if furnace else self.CONTAINERS)
        now = self._section_count(current, section, item)
        delta = (after.get(item, 0) - before.get(item, 0)) * (1 if take else -1)
        container_delta = (stored - now) * (1 if take else -1)
        data = {"pos": pos, "item": item, "requested": count, "gained" if take else "stored": delta,
                "container_before": stored, "container_after": now, "window": current}
        # Furnace output can be replenished while the GUI is closed. The actual
        # item gain is still required, but a simultaneous output delta is not.
        if delta < count or (not furnace and container_delta < count):
            raise _Failure("partial_completion" if delta > 0 else "transfer_unverified", "Requested transfer was not fully verified", data)
        return self._verified(context, **data)

    def _collect_drops(self, args, context):
        item = self._item(args["expected_item"])
        status = self._prepare(context)
        entity = self._entity(args["entity_id"], item, context, dropped=True)
        target = [math.floor(v) for v in entity["pos"]]
        if math.dist(status["pos"], entity["pos"]) > 8:
            raise _Failure("out_of_range", "Pickup is limited to visible drops within eight blocks")
        before, _ = self._inventory(context)

        def site(status):
            fresh = self._entity(args["entity_id"], item, context, dropped=True)
            if [math.floor(v) for v in fresh["pos"]] != target:
                raise _Failure("target_changed", "The dropped item moved to another cell")
            for dy in (0, 1):
                if self._block([target[0], target[1] + dy, target[2]], context)["name"] not in self.RETREAT_PASSABLE:
                    raise _Failure("unsafe", "Dropped item does not have a clear standing cell")
            support = self._block([target[0], target[1] - 1, target[2]], context)
            if support["name"] not in self.SUPPORT or not self._safe_block(support):
                raise _Failure("unsafe", "Pickup requires known solid safe support")

        site(status)
        self._daily_action(context, self.client.move_to, target[0] + .5, target[1], target[2] + .5,
                           range=0, timeout_ms=8000, validate=site)
        after_status = self._prepare(context)
        after, _ = self._inventory(context)
        gained = after.get(item, 0) - before.get(item, 0)
        data = {"entity_id": args["entity_id"], "item": item, "target": target, "pos": after_status["pos"], "gained": gained}
        if gained <= 0:
            raise _Failure("pickup_unverified", "Approaching the drop produced no verified inventory gain", data)
        return self._verified(context, **data)

    def _place_bed(self, args, context):
        return self._place_double(args, context, bed=True)

    def _place_door(self, args, context):
        return self._place_double(args, context, bed=False)

    @classmethod
    def placement_facings(cls, pos, origin):
        """Possible vanilla facing after API.place looks at the bottom support.

        session.js chooses the below block first. Mineflayer generic_place then
        looks at that block's upper face centre, overriding an earlier /look.
        BedBlock uses that horizontal direction for both FACING and its head.
        Include API coordinate rounding (+/- .05) and mouse yaw quantization;
        diagonal uncertainty must validate both possible head cells.
        """
        target, source = cls._pos(pos, block=True), cls._pos(origin)
        dx, dz = target[0] + .5 - source[0], target[2] + .5 - source[2]
        error = .05001
        turn = math.hypot(abs(dx) + error, abs(dz) + error) * math.tan(math.radians(.15)) + .0001
        min_x, min_z = max(0, abs(dx) - error), max(0, abs(dz) - error)
        possible = []
        for name, forward, across in (("east", dx, min_z), ("west", -dx, min_z),
                                       ("south", dz, min_x), ("north", -dz, min_x)):
            if forward + error > 0 and forward + error + turn >= across:
                possible.append(name)
        return sorted(possible)

    @staticmethod
    def _body_at(cell, origin):
        return (origin[0] - .36 < cell[0] + 1 and origin[0] + .36 > cell[0]
                and origin[2] - .36 < cell[2] + 1 and origin[2] + .36 > cell[2]
                and origin[1] - .05 < cell[1] + 1 and origin[1] + 1.85 > cell[1])

    def _place_bed_oriented(self, args, context):
        pos, item = self._pos(args["pos"], block=True), self._item(args["item"])
        self._prepare(context)
        if item not in self.BEDS:
            raise _Failure("unsupported_item", "Unsupported bed item")
        before, _ = self._available(item, 1, context)
        directions = {"north": (0, -1), "south": (0, 1), "west": (-1, 0), "east": (1, 0)}
        evidence = context["retreat_evidence"]
        evidence.update(pos=pos, item=item, stance_geometry_only=True)

        def clear_cell(cell, cache):
            def read(point):
                key = tuple(point)
                if key not in cache:
                    cache[key] = self._block(point, context)
                return cache[key]
            at = read(cell)
            above = read([cell[0], cell[1] + 1, cell[2]])
            ground = read([cell[0], cell[1] - 1, cell[2]])
            return (at["name"] in self.AIR and above["name"] in self.AIR
                    and ground["name"] in self.SUPPORT and self._safe_block(ground))

        def stances(cache):
            candidates = []
            for facing, (dx, dz) in directions.items():
                head = [pos[0] + dx, pos[1], pos[2] + dz]
                stand_cell = [pos[0] - dx, pos[1], pos[2] - dz]
                if clear_cell(head, cache) and clear_cell(stand_cell, cache):
                    candidates.append({"pos": [stand_cell[0] + .5, stand_cell[1], stand_cell[2] + .5], "facing": facing})
            return candidates

        def site(status):
            self._safe_status(status)
            if math.dist(status["pos"], pos) > 3.5:
                raise _Failure("needs_position", "Bed placement requires a nearby stance with no backend approach")
            speed = status.get("speed")
            if type(speed) in (int, float) and (not math.isfinite(speed) or speed > .05):
                raise _Failure("needs_position", "Stop moving before deriving bed placement direction")
            cache = {}
            if self._body_at(pos, status["pos"]) or not clear_cell(pos, cache):
                raise _Failure("unsafe", "Bed foot needs empty supported space outside the bot body")
            facings = self.placement_facings(pos, status["pos"])
            heads = [[pos[0] + directions[facing][0], pos[1], pos[2] + directions[facing][1]] for facing in facings]
            evidence.update(predicted_facings=facings, possible_heads=heads, placement_origin=list(status["pos"]))
            if not facings or any(self._body_at(head, status["pos"]) or not clear_cell(head, cache) for head in heads):
                evidence["stand_candidates"] = stances(cache)
                raise _Failure("needs_position", "This stance can orient the bed into an occupied or unsupported head cell", dict(evidence))
            # Reads above take time. Use the final status for both body and
            # orientation checks; do not leave an unchecked action status read.
            latest = self._status(context)
            self._safe_status(latest)
            current = self.placement_facings(pos, latest["pos"])
            latest_speed = latest.get("speed")
            if (math.dist(status["pos"], latest["pos"]) > .1 or abs(status["pos"][1] - latest["pos"][1]) > .05
                    or math.dist(latest["pos"], pos) > 3.5 or not set(current).issubset(facings)
                    or type(latest_speed) in (int, float) and (not math.isfinite(latest_speed) or latest_speed > .05)
                    or any(self._body_at(cell, latest["pos"]) for cell in [pos] + heads)):
                raise _Failure("needs_position", "Bed placement stance changed during the final cell checks")
            evidence["placement_origin"] = list(latest["pos"])

        site(self._approach(pos, context))
        self._daily_action(context, self.client.place_block, *pos, item=item, validate=site)
        self._prepare(context)
        foot = self._block(pos, context)
        props = foot.get("properties") or {}
        facing = props.get("facing")
        head_pos = ([pos[0] + directions[facing][0], pos[1], pos[2] + directions[facing][1]]
                    if facing in directions else None)
        head = self._block(head_pos, context) if head_pos is not None else {}
        hp = head.get("properties") or {}
        after, _ = self._inventory(context)
        used = before.get(item, 0) - after.get(item, 0)
        data = {"pos": pos, "second_pos": head_pos, "item": item, "facing": facing, "used": used, "blocks": [foot, head]}
        if facing not in evidence.get("predicted_facings", []):
            raise _Failure("action_unconfirmed", "Actual bed direction differed from the verified prewrite direction",
                           {**data, "action_state": "unknown"})
        if (foot.get("name") != item or head.get("name") != item or props.get("part") != "foot"
                or hp.get("part") != "head" or hp.get("facing") != facing or used != 1):
            raise _Failure("place_unverified", "The actual matching bed halves and one-item consumption were not both observed", data)
        return self._verified(context, **data)

    def _place_double(self, args, context, *, bed):
        if bed:
            return self._place_bed_oriented(args, context)
        pos, item = self._pos(args["pos"], block=True), self._item(args["item"])
        self._prepare(context)
        if item not in (self.BEDS if bed else self.DOORS):
            raise _Failure("unsupported_item", "Unsupported bed or wooden door item")
        before, _ = self._available(item, 1, context)
        directions = {"north": (0, -1), "south": (0, 1), "west": (-1, 0), "east": (1, 0)}
        extras = [[pos[0] + dx, pos[1], pos[2] + dz] for dx, dz in directions.values()] if bed else [[pos[0], pos[1] + 1, pos[2]]]

        def site(status):
            feet = [math.floor(v) for v in status["pos"]]
            for cell in [pos] + extras:
                if cell in (feet, [feet[0], feet[1] + 1, feet[2]]) or self._block(cell, context)["name"] not in self.AIR:
                    raise _Failure("unsafe", "Both structure cells must have clear space outside the bot body")
                if bed or cell == pos:
                    ground = self._block([cell[0], cell[1] - 1, cell[2]], context)
                    if ground["name"] not in self.SUPPORT or not self._safe_block(ground):
                        raise _Failure("unsafe", "Every possible bed orientation or the door base needs safe support")

        site(self._approach(pos, context))
        self._daily_action(context, self.client.place_block, *pos, item=item, validate=site)
        self._prepare(context)
        first = self._block(pos, context)
        props = first.get("properties", {})
        facing = props.get("facing")
        second_pos = None
        if bed and facing in directions:
            dx, dz = directions[facing]
            second_pos = [pos[0] + dx, pos[1], pos[2] + dz]
        elif not bed:
            second_pos = extras[0]
        second = self._block(second_pos, context) if second_pos is not None else {}
        other = second.get("properties", {})
        after, _ = self._inventory(context)
        used = before.get(item, 0) - after.get(item, 0)
        halves = (props.get("part") == "foot" and other.get("part") == "head") if bed else (props.get("half") == "lower" and other.get("half") == "upper")
        closed = (bed or (props.get("open") in (False, "false") and other.get("open") in (False, "false")
                         and props.get("hinge") in {"left", "right"} and props.get("hinge") == other.get("hinge")))
        data = {"pos": pos, "second_pos": second_pos, "item": item, "facing": facing, "used": used, "blocks": [first, second]}
        if first.get("name") != item or second.get("name") != item or not halves or not closed or facing not in directions or other.get("facing") != facing or used != 1:
            raise _Failure("place_unverified", "The matching two-part structure and one-item consumption were not observed", data)
        return self._verified(context, **data)
