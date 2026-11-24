"""Verified source pickup and contained lava ignition; never open-ground lava."""

import math
import time

from core.block_geometry import _ray_cells
from . import SkillResult
from .daily_survival import DailySurvivalSkills
from .hunting import COOKED_DROPS
from .survival import _Failure


class BucketSkills(DailySurvivalSkills):
    ARGUMENTS = {
        "collect_liquid": ({"pos", "expected_name"}, {"pos", "expected_name"}),
        "hunt_with_lava": ({"entity_id", "expected_name"}, {"entity_id", "expected_name"}),
    }
    # Full, non-falling, non-flammable blocks only. Unknown blocks are refused.
    CONTAINMENT = frozenset({"stone", "cobblestone", "deepslate", "cobbled_deepslate",
        "granite", "diorite", "andesite", "obsidian", "crying_obsidian", "bedrock",
        "stone_bricks", "polished_andesite", "polished_diorite", "polished_granite"})
    MAX_READS = 380

    def __init__(self, client):
        super().__init__(client)
        self._ignitions = {}

    def execute(self, name, args, cancel_event=None, **kwargs):
        force_override = bool(kwargs.pop("force_override", False))
        context = {"cancel": cancel_event, "acted": False, "reads": 0,
                   "force_override": force_override,
                   "deadline": time.monotonic() + 12, "evidence": {"skill": name, "executed": False}}
        data = context["evidence"]
        try:
            if name not in self.ARGUMENTS:
                raise _Failure("unknown_skill", "Unknown bucket skill")
            allowed, required = self.ARGUMENTS[name]
            if not isinstance(args, dict) or set(args) - allowed or required - set(args) or kwargs:
                raise _Failure("bad_args", "Missing or unsupported bucket arguments")
            status = self._prepare(context)
            context.update(dimension=status["dimension"], origin=list(status["pos"]))
            getattr(self, "_" + name)(args, context)
            self._check(context)
            return SkillResult(True, "Bucket operation verified from current blocks and inventory", data)
        except Exception as error:
            error = self._disconnection_error(error, context)
            code = getattr(error, "code", "internal_error")
            data.update(getattr(error, "data", {}))
            if isinstance(getattr(error, "details", None), dict):
                data["api_details"] = error.details
            if context.get("unverified_write") or context.get("disconnected_unknown") or (context.get("write_sent") and code in self.UNKNOWN):
                data.update(action_state="unknown", cleanup_skipped="unknown_bucket")
                data["verification_error"] = code
                code = "action_unconfirmed"
            if context.get("lava_outstanding"):
                data.update(lava_outstanding=True, recovery_required=True,
                            recovery_instruction="Inspect the recorded source before any further action; do not replay ignition")
                code = "action_unconfirmed"
                data["action_state"] = "unknown"
                data["cleanup_skipped"] = "unknown_bucket"
            return SkillResult(False, str(error), data, code)
        finally:
            data["executed"] = bool(context.get("write_sent"))

    def _check(self, context):
        super()._check(context)
        if time.monotonic() > context["deadline"]:
            raise _Failure("bucket_budget_exhausted", "Bucket work exceeded its bounded time")

    def _block(self, pos, context):
        context["reads"] += 1
        if context["reads"] > self.MAX_READS:
            raise _Failure("observation_budget", "Bucket terrain observation budget exceeded")
        return super()._block(pos, context)

    def _read_options(self, context):
        return {"timeout_seconds": min(1.5, max(.01, context["deadline"] - time.monotonic()))}

    def _inventory_full(self, context):
        result = self._call_result(context, self.client.inventory, timeout_seconds=2)
        rows = result.get("items")
        if not isinstance(rows, list):
            raise _Failure("invalid_response", "Inventory items are missing")
        counts = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str) or type(row.get("count")) is not int or row["count"] < 0:
                raise _Failure("invalid_response", "Invalid bucket inventory")
            counts[row["name"]] = counts.get(row["name"], 0) + row["count"]
        return counts, result

    def _station(self, pos, context, *, item="bucket", liquid_name=None):
        status = self._prepare(context)
        if status["health"] < 12 or status.get("food", 0) < 8:
            raise _Failure("unsafe", "Bucket use requires adequate health and food")
        if math.dist(status["pos"], context["origin"]) > .25:
            raise _Failure("unsafe", "Bucket stance changed")
        if math.dist(status["pos"], pos) > 2.9:
            raise _Failure("out_of_reach", "Source must be within stationary reach; no implicit approach")
        feet = [math.floor(v) for v in status["pos"]]
        if feet[0] == pos[0] and feet[2] == pos[2]:
            raise _Failure("unsafe", "Never use liquid in the player's own column")
        allowed_support = (
            self.CONTAINMENT | DailySurvivalSkills.SUPPORT
            if (item == "bucket" and liquid_name == "water")
            else self.CONTAINMENT
        )
        if self._block([feet[0], feet[1] - 1, feet[2]], context)["name"] not in allowed_support:
            raise _Failure("unsafe", "Bucket stance needs a nonflammable full support block")
        for height in (0, 1):
            if self._block([feet[0], feet[1] + height, feet[2]], context)["name"] not in self.AIR:
                raise _Failure("unsafe", "Player body space is not clear")
        eye = [status["pos"][0], status["pos"][1] + 1.62, status["pos"][2]]
        endpoint = pos
        target = [pos[0] + .5, pos[1] + .85, pos[2] + .5]
        if item == "lava_bucket":
            # Official useBucket clicks the full block below an empty cell,
            # aiming .49 above its centre. The pickup ray at y+.85 can clear
            # a pit wall even when this lower placement ray hits that wall.
            endpoint = [pos[0], pos[1] - 1, pos[2]]
            if (self._block(pos, context)["name"] not in self.AIR
                    or self._block(endpoint, context)["name"] not in self.CONTAINMENT):
                raise _Failure("unsafe_lava_site", "Lava placement needs empty air over a known full containment block")
            target = [pos[0] + .5, pos[1] - .01, pos[2] + .5]
            context["evidence"].update(placement_reference=endpoint,
                                       placement_face=[0, 1, 0], placement_aim=target)
        if math.dist(eye, target) > 4.5:
            raise _Failure("out_of_reach", "Liquid surface is outside the eye ray reach")
        for cell in dict.fromkeys(_ray_cells(eye, target)):
            if tuple(cell) == tuple(endpoint):
                break
            if self._block(list(cell), context)["name"] not in self.AIR:
                raise _Failure("target_occluded", "Official bucket aim ray is obstructed",
                               {"blocker": list(cell), "bucket_item": item, "aim": target})
        return status

    def _write(self, pos, item, context, *, liquid_name=None):
        self._station(pos, context, item=item, liquid_name=liquid_name)
        context["acted"] = context["write_sent"] = True
        context["unverified_write"] = True
        if item == "lava_bucket":
            context["lava_outstanding"] = True
        result = self._call_result(context, self.client.interact, pos=pos, item=item,
                                   timeout_seconds=2, stop_on=["hurt", "death"])
        if result.get("interrupted_by"):
            raise _Failure("action_unconfirmed", "Bucket action was interrupted before verification")
        return result  # Caller records a known reply before checking cancellation.

    @staticmethod
    def _source(block, name):
        return block.get("name") == name and str((block.get("properties") or {}).get("level")) == "0"

    def _collect_liquid(self, args, context):
        pos, name = self._pos(args["pos"], block=True), self._item(args["expected_name"])
        if name not in {"water", "lava"}:
            raise _Failure("bad_args", "Only water and lava sources are supported")
        self._station(pos, context, item="bucket", liquid_name=name)
        before, inv = self._inventory_full(context)
        if before.get("bucket", 0) < 1:
            raise _Failure("missing_item", "An existing empty bucket is required")
        if before["bucket"] > 1 and (type(inv.get("empty_slots")) is not int or inv["empty_slots"] < 1):
            raise _Failure("inventory_full", "A stacked empty bucket needs a spare inventory slot")
        block = self._block(pos, context)
        if not self._source(block, name):
            raise _Failure("not_liquid_source", "Only an explicitly observed level-zero source may be collected")
        context["evidence"].update(pos=pos, liquid=name)
        self._write(pos, "bucket", context, liquid_name=name)
        self._check(context)
        self._station(pos, context, item="bucket", liquid_name=name)
        after, _ = self._inventory_full(context)
        current = self._block(pos, context)
        filled = name + "_bucket"
        if after.get(filled, 0) - before.get(filled, 0) != 1 or before["bucket"] - after.get("bucket", 0) != 1:
            raise _Failure("action_unconfirmed", "Filled bucket and empty bucket changes were not both observed")
        # Infinite water may immediately regenerate; confirmed inventory still
        # proves pickup. Lava cannot be treated as an infinite source.
        if name == "lava" and current["name"] not in self.AIR:
            raise _Failure("action_unconfirmed", "Lava source removal was not confirmed")
        context["unverified_write"] = False
        context["evidence"].update(completed=True, action_state="verified", gained={filled: 1},
                                   remaining_block=current["name"], source_removed=current["name"] in self.AIR)

    def _pit(self, pos, context):
        # Existing two-high walls contain the animal and prevent sideways flow.
        for dx, dz in ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1)):
            levels = (-1,) if dx == dz == 0 else (-1, 0, 1)
            for dy in levels:
                if self._block([pos[0] + dx, pos[1] + dy, pos[2] + dz], context)["name"] not in self.CONTAINMENT:
                    raise _Failure("unsafe_lava_site", "Lava hunting needs an existing solid nonflammable containment pit")
        if self._block(pos, context)["name"] not in self.AIR or self._block([pos[0], pos[1] + 1, pos[2]], context)["name"] not in self.AIR:
            raise _Failure("unsafe_lava_site", "Containment cell must be empty air before ignition")
        # Lava can ignite adjacent air next to combustible blocks above it. A
        # broad finite whitelist scan refuses vegetation, wooden builds, liquids
        # and unknown blocks instead of assuming a fireproof ground surface.
        for dx in range(-3, 4):
            for dz in range(-3, 4):
                for dy in range(-1, 5):
                    block = self._block([pos[0] + dx, pos[1] + dy, pos[2] + dz], context)
                    if block["name"] not in self.AIR | self.CONTAINMENT:
                        raise _Failure("unsafe_lava_site", "Nearby flammable, fluid or unknown block excludes lava use")

    def _recover_lava(self, pos, context, before):
        # Only a positively confirmed placement reaches this function. An
        # unknown HTTP outcome never triggers another write or blind pickup.
        cancel = context["cancel"]
        context["cancel"] = None
        try:
            self._station(pos, context)
            if not self._source(self._block(pos, context), "lava"):
                raise _Failure("action_unconfirmed", "Recorded lava source changed before recovery")
            counts, _ = self._inventory_full(context)
            if counts.get("bucket", 0) < 1:
                raise _Failure("action_unconfirmed", "No empty bucket available for lava recovery")
            self._write(pos, "bucket", context)
            after, _ = self._inventory_full(context)
            if (self._block(pos, context)["name"] not in self.AIR
                    or after.get("lava_bucket", 0) != before.get("lava_bucket", 0)
                    or after.get("bucket", 0) != before.get("bucket", 0)):
                raise _Failure("action_unconfirmed", "Lava source and bucket round trip are not both verified")
            context["lava_outstanding"] = False
            context["unverified_write"] = False
            context["evidence"].update(source_recovered=True, lava_outstanding=False)
        finally:
            context["cancel"] = cancel

    def _hunt_with_lava(self, args, context):
        name = self._item(args["expected_name"])
        if name not in self.ANIMALS:
            raise _Failure("unsafe_target", "Lava hunting only supports pigs, cows, sheep and chickens")
        status = self._prepare(context)
        if status["health"] < 16 or status.get("food", 0) < 10 or status.get("is_day") is not True or status.get("thundering"):
            raise _Failure("unsafe", "Lava hunting requires healthy, fed, dry daylight conditions")
        entity = self._entity(args["entity_id"], name, context)
        key = (status["dimension"], repr(status.get("last_death")), args["entity_id"], name)
        if time.monotonic() - self._ignitions.get(key, -float("inf")) < 30:
            raise _Failure("hunt_cooldown", "Observe the previous ignition or collect its drops before considering lava again")
        pos = [math.floor(v) for v in entity["pos"]]
        self._station(pos, context, item="lava_bucket")
        if status["pos"][1] < pos[1] + 2:
            raise _Failure("unsafe_lava_site", "Stand above the containment pit, never alongside the animal")
        before, inv = self._inventory_full(context)
        if before.get("lava_bucket", 0) < 1:
            raise _Failure("missing_item", "An existing lava bucket is required")
        if type(inv.get("empty_slots")) is not int or inv["empty_slots"] < 1:
            raise _Failure("inventory_full", "Reserve a slot for bucket recovery and drops")
        self._pit(pos, context)
        self._station(pos, context, item="lava_bucket")
        fresh = self._entity(args["entity_id"], name, context)
        if [math.floor(v) for v in fresh["pos"]] != pos or any(not .2 <= fresh["pos"][i] - pos[i] <= .8 for i in (0, 2)):
            raise _Failure("target_changed", "Animal is no longer centred inside the containment cell")
        rows = self.client.get_entities(radius=8)
        if len(rows) >= 40 or any(row.get("id") != args["entity_id"] and
                math.dist(self._pos(row.get("pos")), [v + .5 for v in pos]) < 7 for row in rows):
            raise _Failure("unsafe_target", "Another entity or dropped item could be exposed to lava")
        data = context["evidence"]
        data.update(entity_id=args["entity_id"], animal=name, source_pos=pos, source_recovered=False,
                    killed_verified=False, completed=False, gained={})
        self._ignitions[key] = time.monotonic()
        if len(self._ignitions) > 128:
            self._ignitions.pop(next(iter(self._ignitions)))
        reply = self._write(pos, "lava_bucket", context)
        # Cancellation after a known placement still permits the specifically
        # verified source recovery, never a new attack or movement.
        cancel = context["cancel"]
        context["cancel"] = None
        try:
            counts, _ = self._inventory_full(context)
            if (reply.get("target") != pos or not self._source(self._block(pos, context), "lava")
                    or before["lava_bucket"] - counts.get("lava_bucket", 0) != 1
                    or counts.get("bucket", 0) - before.get("bucket", 0) != 1):
                raise _Failure("action_unconfirmed", "Lava placement location and bucket transition were not confirmed")
        finally:
            context["cancel"] = cancel
        self._recover_lava(pos, context, before)
        self._check(context)
        after, _ = self._inventory_full(context)
        expected = self.ANIMALS[name] | {COOKED_DROPS[name]}
        gained = {item: after.get(item, 0) - before.get(item, 0) for item in expected if after.get(item, 0) > before.get(item, 0)}
        data.update(gained=gained, action_state="verified", next_step="observe_or_collect_drops")
        if gained:
            data.update(completed=True, inventory_verified=True)
            return
        self._uncollected_drops(entity["pos"], expected, context, data)
        raise _Failure("hunt_unverified", "Lava was recovered; no food gain yet. Reobserve burning target or drops, do not immediately reignite", data)
