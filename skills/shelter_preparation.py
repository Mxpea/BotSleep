"""Internal, one-block preparation of a fully observed natural shelter plot.

Planning never authorizes a base. Each mutation rechecks the entire plot and
must be followed by the ordinary no-demolition plan_shelter inspection.
"""

import math
import time

from core.block_geometry import PASS_THROUGH, trace_block_visibility
from core.structure_spec import (SHELTER_5X5_SPEC, SHELTER_GROUND_BLOCKS, evaluate_shelter_site,
                                 normalize_orientation, transform_rel_pos)
from .daily_survival import DailySurvivalSkills
from .positioning import PositioningSkills
from .shelter import ShelterSkills
from .survival import _Failure


class ShelterPreparationSkills(DailySurvivalSkills):
    ARGUMENTS = {name: ({"pos", "orientation"}, {"pos"}) for name in
                 ("plan_shelter_preparation", "prepare_shelter_step")}
    MAX_READS = 320
    READ_SECONDS = 15
    CLEARABLE = frozenset({"dirt", "grass_block"})
    MAX_OBSTACLES = 8
    POSTWRITE_READ_RESERVE = 20
    RANGED = frozenset({"skeleton", "stray", "bogged", "pillager"})
    FALLING = frozenset({"sand", "red_sand", "gravel", "anvil", "chipped_anvil", "damaged_anvil"})

    @staticmethod
    def _check(context):
        DailySurvivalSkills._check(context)
        if not context.get("daily_verified") and time.monotonic() >= context.get("preparation_deadline", math.inf):
            sent = context.get("write_sent") is True
            raise _Failure("action_unconfirmed" if sent else "preparation_budget_exhausted",
                           "Shelter preparation observation deadline expired",
                           {"action_state": "unknown" if sent else "not_executed"})

    @staticmethod
    def _read_options(context):
        ShelterPreparationSkills._check(context)
        return {"timeout_seconds": min(2, context.get("preparation_deadline", time.monotonic() + 2) - time.monotonic())}

    def _count_read(self, context):
        self._check(context)
        if context.get("preparation_reads", 0) >= self.MAX_READS:
            sent = context.get("write_sent") is True
            raise _Failure("action_unconfirmed" if sent else "preparation_budget_exhausted",
                           "Shelter preparation reached its read limit",
                           {"action_state": "unknown" if sent else "not_executed"})
        context["preparation_reads"] = context.get("preparation_reads", 0) + 1
        context.setdefault("retreat_evidence", {"executed": False})["read_calls"] = context["preparation_reads"]

    def _status(self, context):
        context.setdefault("preparation_deadline", time.monotonic() + self.READ_SECONDS)
        self._count_read(context)
        return super()._status(context)

    def _block(self, pos, context):
        self._count_read(context)
        return super()._block(pos, context)

    def _stable(self, status, baseline):
        self._safe_status(status)
        speed = self._number(status.get("speed"), "speed", 0, 1024)
        velocity = self._pos(status.get("velocity"))
        if ("busy" not in status or status["busy"] is not None or speed > .1
                or math.dist(velocity, [0, 0, 0]) > .1
                or status.get("last_death") != baseline.get("last_death")
                or status["health"] < max(12, baseline["health"])
                or self._number(status.get("food"), "food", 0, 20) < 8
                or math.dist(status["pos"], baseline["pos"]) > .05
                or abs(status["pos"][1] - round(status["pos"][1])) > .05
                or status.get("jumping") is not False or status.get("held_controls")
                or status.get("is_day") is not True or status.get("thundering") is not False
                or not 0 <= self._number(status.get("time_of_day"), "time_of_day", 0, 24000) < 11000):
            raise _Failure("unsafe_state", "Preparing terrain requires unchanged stable daylight footing")
        nearest = status.get("nearest_hostile")
        if nearest is not None:
            if (not isinstance(nearest, dict) or not isinstance(nearest.get("name"), str) or not nearest["name"]
                    or type(nearest.get("id")) is not int or type(nearest.get("dist")) not in (int, float)
                    or not math.isfinite(nearest["dist"]) or nearest["dist"] < 0):
                raise _Failure("invalid_response", "Preparation needs a valid nearest-hostile summary")
            if nearest["dist"] <= (16 if nearest["name"] in self.RANGED else 8):
                raise _Failure("unsafe", "A hostile is too close to prepare a shelter site")

    def _threats(self, pos, context, rows=None):
        if rows is None:
            self._count_read(context)
            rows = self.client.get_entities(radius=32, types=["hostile"], **self._read_options(context))
            self._check(context)
        if not isinstance(rows, list) or len(rows) >= 40:
            raise _Failure("invalid_response", "Preparation needs a complete hostile scan")
        for row in rows:
            if (not isinstance(row, dict) or row.get("type") != "hostile"
                    or type(row.get("id")) is not int or not isinstance(row.get("name"), str) or not row["name"]):
                raise _Failure("invalid_response", "Preparation hostile row is incomplete")
            point = self._pos(row.get("pos"))
            if math.dist(pos, point) <= (16 if row["name"] in self.RANGED else 8):
                raise _Failure("unsafe", "A hostile is too close to the actual preparation stance")
        return rows

    def _footing(self, pos, context):
        cell = PositioningSkills._cell(pos)
        columns = set(PositioningSkills._columns(pos, pos))
        for x, z in sorted(columns):
            for dy in (-1, 0, 1):
                block = self._block([x, cell[1] + dy, z], context)
                allowed = SHELTER_GROUND_BLOCKS if dy == -1 else PASS_THROUGH
                if block["name"] not in allowed or not self._safe_block(block):
                    raise _Failure("unsafe_state", "Preparation stance lacks full dry natural support and body clearance")
        return columns

    def _read_plan(self, anchor, orientation, status, context):
        observed = {}
        def read(pos):
            try:
                block = self._block(pos, context)
            except Exception as error:
                if getattr(error, "code", None) == "not_loaded":
                    context["preparation_unknown_geometry"] = True
                raise
            if block["name"] in {"unknown", "not_loaded"}:
                context["preparation_unknown_geometry"] = True
            observed[tuple(pos)] = block
            return block
        site = evaluate_shelter_site(anchor, orientation, read, bot_pos=status["pos"],
            building_blocks=frozenset(), already_built_blocks=frozenset(), ground_blocks=SHELTER_GROUND_BLOCKS,
            clearable_blocks=self.CLEARABLE, allow_clearing=True, allow_floor_patch=False,
            max_clear_cells=self.MAX_OBSTACLES, max_floor_patches=0)
        if site is None or site["bridge_cells"]:
            if context.get("preparation_unknown_geometry"):
                raise _Failure("unknown_geometry", "The shelter preparation candidate has uncaptured geometry")
            raise _Failure("unsafe_site", "Preparation only accepts a complete natural floor with at most eight dirt or grass obstacles")
        # The independent workflow permits more replaceable shapes than the
        # ordinary no-demolition planner. Preparation must lead back to that
        # planner without implicitly authorizing snow/plant/tree clearance.
        for rel in SHELTER_5X5_SPEC.clearance_rel:
            key = tuple(transform_rel_pos(rel, anchor, orientation, integer=True))
            if observed[key]["name"] not in ShelterSkills.REPLACEABLE_SPACE | self.CLEARABLE:
                raise _Failure("unsafe_site", "Preparation would leave a shape unsupported by the shelter planner")
        obstacles = [{"pos": list(pos), "name": observed[tuple(pos)]["name"]} for pos in site["clear_cells"]]
        if any(row["name"] not in self.CLEARABLE for row in obstacles):
            raise _Failure("unsafe_site", "Preparation cannot clear buildings, trees, or unknown material")
        return obstacles

    def _start(self, args, context):
        anchor = self._pos(args["pos"], block=True)
        try:
            orientation = normalize_orientation(args.get("orientation", "north"))
        except (TypeError, ValueError):
            raise _Failure("bad_args", "Unsupported shelter orientation") from None
        status = self._prepare(context)
        self._stable(status, status)
        if math.dist(status["pos"], anchor) > 8:
            raise _Failure("out_of_range", "Shelter preparation candidate must be within eight blocks")
        self._footing(status["pos"], context)
        self._threats(status["pos"], context)
        context["preparation_origin_status"] = status
        context["retreat_evidence"].update(pos=anchor, candidate_pos=anchor, orientation=orientation,
            dimension=status["dimension"], preparation_plan_verified=False, shelter_verified=False)
        rows = self._read_plan(anchor, orientation, status, context)
        evidence = context["retreat_evidence"]
        evidence.update(pos=anchor, candidate_pos=anchor, orientation=orientation, dimension=status["dimension"],
                        fresh=True, preparation_plan_verified=True, shelter_verified=False,
                        clear_cells=[row["pos"] for row in rows], remaining_obstacles=rows,
                        clear_remaining=len(rows), remaining=len(rows), preparation_complete=not rows,
                        prepared=not rows, next_target=rows[0]["pos"] if rows else None,
                        read_budget=self.MAX_READS, observation_budget_seconds=self.READ_SECONDS)
        return anchor, orientation, status, rows

    def _plan_shelter_preparation(self, args, context):
        try:
            _, _, before, _ = self._start(args, context)
        except _Failure as error:
            before = context.get("preparation_origin_status")
            if error.code != "unsafe_site" or before is None or context.get("preparation_unknown_geometry"):
                raise
            latest = self._status(context)
            self._stable(latest, before)
            self._threats(latest["pos"], context)
            self._check(context)
            context["retreat_evidence"].update(preparation_rejected=True, fresh=True, executed=False)
            raise error
        latest = self._status(context)
        self._stable(latest, before)
        self._threats(latest["pos"], context)
        self._check(context)
        return dict(context["retreat_evidence"])

    def _target(self, row, status, context):
        pos = row["pos"]
        footprint = set(PositioningSkills._columns(status["pos"], status["pos"]))
        if (pos[0], pos[2]) in footprint or math.dist(status["pos"], pos) > 3.5:
            return False
        block = self._gather_guard(pos, row["name"], status, context)
        if block.get("diggable") is not True:
            return False
        for dx, dy, dz in ((0, -1, 0), (0, 1, 0), (-1, 0, 0), (1, 0, 0), (0, 0, -1), (0, 0, 1)):
            neighbor = self._block([pos[0] + dx, pos[1] + dy, pos[2] + dz], context)
            name = neighbor["name"]
            if (name in self.FALLING or name.endswith("_concrete_powder") or not self._safe_block(neighbor)
                    or name in {"unknown", "not_loaded"}):
                raise _Failure("unsafe_site", "Preparation cannot expose liquids, unknown cells, or gravity blocks")
        sight = trace_block_visibility(status["pos"], pos, lambda p: self._block(p, context), max_queries=16, max_distance=4.5)
        return sight["visible"] is True

    def _prepare_shelter_step(self, args, context):
        anchor, orientation, before, rows = self._start(args, context)
        evidence = context["retreat_evidence"]
        if not rows:
            return dict(evidence)
        # Do not turn a blocked or distant target into implicit navigation.
        row = next((candidate for candidate in rows if self._target(candidate, before, context)), None)
        if row is None:
            evidence["needs_position"] = True
            raise _Failure("needs_position", "No planned dirt obstacle is safely reachable from the actual stance")
        evidence["next_target"] = row["pos"]

        def validate(current):
            self._stable(current, before)
            current_rows = self._read_plan(anchor, orientation, current, context)
            if current_rows != rows or row not in current_rows:
                raise _Failure("site_changed", "Preparation plan changed before the single dig")
            if not self._target(row, current, context):
                raise _Failure("needs_position", "Preparation target is no longer visibly reachable")
            columns = self._footing(current["pos"], context)
            threats = self._threats(current["pos"], context)
            final = self._status(context)
            self._stable(final, before)
            if (PositioningSkills._cell(final["pos"]) != PositioningSkills._cell(current["pos"])
                    or not set(PositioningSkills._columns(final["pos"], final["pos"])).issubset(columns)
                    or (row["pos"][0], row["pos"][2]) in PositioningSkills._columns(final["pos"], final["pos"])):
                raise _Failure("safety_changed", "Preparation source footprint changed before digging")
            self._threats(final["pos"], context, threats)
            if (context["preparation_reads"] + self.POSTWRITE_READ_RESERVE > self.MAX_READS
                    or context["preparation_deadline"] - time.monotonic() <= 1.5):
                raise _Failure("preparation_budget_exhausted", "Preparation has no reserved budget for actual-footing confirmation")

        # After a write, all further operations are reads. Never append stop,
        # movement, a second dig, collection, or workflow posture restoration.
        context["navigation_observe_only"] = True
        self._daily_action(context, self.client.dig, row["pos"], collect=False, validate=validate)
        context["preparation_deadline"] = min(context["preparation_deadline"], time.monotonic() + 1.5)
        try:
            after = self._status(context)
            self._stable(after, before)
            block = self._block(row["pos"], context)
            if block["name"] not in self.AIR:
                raise _Failure("action_unconfirmed", "The prepared cell was not observed as air")
            columns = self._footing(after["pos"], context)
            threats = self._threats(after["pos"], context)
            if context.get("cancel") is not None:
                context["cancel"].wait(.1)
            else:
                time.sleep(.1)
            final = self._status(context)
            self._stable(final, after)
            if (PositioningSkills._cell(final["pos"]) != PositioningSkills._cell(after["pos"])
                    or not set(PositioningSkills._columns(final["pos"], final["pos"])).issubset(columns)):
                raise _Failure("action_unconfirmed", "Actual footing changed after preparation")
            self._threats(final["pos"], context, threats)
            self._check(context)
        except Exception as error:
            raise _Failure("action_unconfirmed", "Shelter preparation outcome could not be fully verified",
                           {"action_state": "unknown", "verification_error": getattr(error, "code", "internal_error")}) from None
        remaining = [candidate for candidate in rows if candidate != row]
        return self._verified(context, fresh=True, cleared_pos=row["pos"], cleared_name=row["name"],
            block_removed=True, collected=False, pos_after=final["pos"],
            clear_cells=[candidate["pos"] for candidate in remaining], remaining_obstacles=remaining,
            clear_remaining=len(remaining), remaining=len(remaining), next_target=remaining[0]["pos"] if remaining else None,
            preparation_complete=not remaining, prepared=not remaining, requires_shelter_replan=True,
            shelter_verified=False)
