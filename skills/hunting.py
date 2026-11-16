"""Short, stationary animal hunts with fresh aim and actual inventory evidence."""

import math
import time

from .combat import aim_angles, clear_entity_ray, melee_weapon
from .survival import _Failure


COOKED_DROPS = {"cow": "cooked_beef", "pig": "cooked_porkchop",
                "sheep": "cooked_mutton", "chicken": "cooked_chicken"}


def _rest(seconds, cancel):
    if cancel is not None:
        if cancel.wait(seconds):
            raise _Failure("cancelled", "Hunt cancelled during weapon cooldown")
    else:
        time.sleep(seconds)


def hunt_animal(skill, args, context):
    """Use the daily skill's evidence/unknown-write handling, never /attack chase."""
    context["operation_deadline"] = time.monotonic() + 8
    context["read_timeout"] = 1.5
    try:
        return _hunt_animal(skill, args, context)
    except Exception as error:
        if context.get("hunting_pending"):
            context["cleanup_attempted"] = True
            evidence = context.get("retreat_evidence", {})
            evidence.update(action_state="unknown", cleanup_skipped="combat_unknown",
                            verification_error=getattr(error, "code", "internal_error"))
            if isinstance(getattr(error, "details", None), dict):
                evidence["api_details"] = error.details
            raise _Failure("action_unconfirmed", "Hunt write has no complete outcome observation", evidence) from None
        if getattr(error, "code", None) == "operation_budget_exhausted":
            # No continuous movement is used. A known-complete equip/swing does
            # not need a new, potentially long stop request after this deadline.
            context["cleanup_attempted"] = True
        raise


def _hunt_animal(skill, args, context):
    def budget():
        skill._check(context)
        return skill._read_options(context)

    def cooldown(seconds):
        budget()
        if context["operation_deadline"] - time.monotonic() <= seconds:
            raise _Failure("operation_budget_exhausted", "Not enough hunt time remains for a full weapon cooldown")
        _rest(seconds, context.get("cancel"))
        budget()

    name = skill._item(args["expected_name"])
    if name not in skill.ANIMALS:
        raise _Failure("unsafe_target", "Only cows, pigs, sheep and chickens may be hunted")
    status = skill._prepare(context)
    initial_health, origin = status["health"], list(status["pos"])
    before, inventory = skill._inventory(context)
    budget()
    weapon = melee_weapon(inventory)
    if weapon is None:
        raise _Failure("missing_tool", "A sword or axe is required for hunting")
    expected = skill.ANIMALS[name] | {COOKED_DROPS[name]}
    evidence = context["retreat_evidence"]
    evidence.update(entity_id=args["entity_id"], animal=name, weapon=weapon[0], hits=0,
                    gained={}, completed=False, killed_verified=False, automatic_chase=False)
    def target(current):
        budget()
        if current["health"] < 12 or current.get("food", 0) < 10 or current["health"] < initial_health:
            raise _Failure("unsafe", "Hunting stops on damage, low health or hunger", evidence)
        if math.dist(current["pos"], origin) > .3:
            raise _Failure("unsafe", "Position changed during the stationary hunt", evidence)
        entity = skill._entity(args["entity_id"], name, context)
        if entity.get("type") in {"player", "hostile"}:
            raise _Failure("unsafe_target", "The observed animal identity is not passive", evidence)
        if math.dist(current["pos"], entity["pos"]) > 2.8:
            raise _Failure("out_of_range", "Animal is outside checked melee reach; reobserve and choose a safe stance",
                           {**evidence, "target_pos": entity["pos"], "next_step": "reobserve_before_approach"})
        feet = [math.floor(v) for v in current["pos"]]
        if (skill._block([feet[0], feet[1] - 1, feet[2]], context)["name"] not in skill.SUPPORT
                or any(skill._block([feet[0], feet[1] + y, feet[2]], context)["name"] not in skill.AIR for y in (0, 1))):
            raise _Failure("unsafe", "Hunting needs solid footing and clear body space", evidence)
        rows = skill.client.get_entities(radius=8, **budget())
        budget()
        if not isinstance(rows, list) or len(rows) >= 40:
            raise _Failure("invalid_response", "Entity scan is missing or saturated", evidence)
        for row in rows:
            if not isinstance(row, dict) or type(row.get("id")) is not int:
                raise _Failure("invalid_response", "Entity observation is malformed", evidence)
        latest = [row for row in rows if row["id"] == args["entity_id"]]
        if len(latest) > 1:
            raise _Failure("invalid_response", "Entity scan contains duplicate target identities", evidence)
        if not latest or latest[0].get("name") != name or latest[0].get("type") != entity.get("type"):
            raise _Failure("target_changed", "The latest entity scan no longer confirms the same passive animal", evidence)
        entity = dict(latest[0], pos=skill._pos(latest[0].get("pos")))
        if math.dist(current["pos"], entity["pos"]) > 2.8:
            raise _Failure("out_of_range", "Animal moved outside checked melee reach during validation",
                           {**evidence, "target_pos": entity["pos"], "next_step": "reobserve_before_approach"})
        for row in rows:
            if row["id"] == args["entity_id"]:
                continue
            pos = skill._pos(row.get("pos"))
            # The API's ray click selects the first entity. A sword can sweep,
            # so neither the attack corridor nor the victim's vicinity is shared.
            if (row.get("type") in {"hostile", "player"}
                    or math.dist(pos, entity["pos"]) < 1.5 or math.dist(pos, current["pos"]) < 3.5):
                raise _Failure("unsafe_target", "Another entity could be hit, swept or draw danger", evidence)
        ray_target = list(entity["pos"])
        if name == "chicken":
            ray_target[1] -= .55
        ray = clear_entity_ray(current["pos"], ray_target, lambda p: skill._block(p, context))
        if ray["visible"] is not True:
            raise _Failure("target_occluded", "Animal torso ray is not freshly clear", {**evidence, "visibility": ray})
        budget()
        return entity

    entity = target(status)
    def equip(*args, **kwargs):
        options = budget()  # Recomputed after the final fresh target validation.
        context["hunting_pending"] = True
        return skill.client.equip(*args, **kwargs, **options)
    skill._daily_action(context, equip, weapon[0], slot="hand", validate=target)
    context["hunting_pending"] = False
    budget()
    # Changing equipment does not establish the previous attack's cooldown.
    cooldown(weapon[1])
    for _ in range(4):
        budget()
        current = skill._prepare(context)
        entity = target(current)
        aim = {}
        def validate_swing(current):
            fresh_entity = target(current)
            yaw, pitch = aim_angles(current["pos"], fresh_entity["pos"], height=.35 if name == "chicken" else .9)
            aim.update(yaw=yaw, pitch=pitch, entity=fresh_entity)
        def swing(**kwargs):
            options = budget()
            if options["timeout_seconds"] <= .05:
                raise _Failure("operation_budget_exhausted", "Not enough hunt time remains for a bounded swing")
            context["hunting_pending"] = True
            return skill.client.control(**kwargs, yaw=aim["yaw"], pitch=aim["pitch"], **options)
        try:
            reply = skill._daily_action(context, swing, hit=1, ms=50, wait=True, validate=validate_swing)
        except Exception as error:
            if getattr(error, "code", "") in skill.UNKNOWN:
                context["cleanup_attempted"] = True
                evidence.update(action_state="unknown", cleanup_skipped="combat_unknown")
            raise
        entity = aim["entity"]
        hits = reply.get("hits")
        if (not isinstance(hits, list) or len(hits) != 1
                or not isinstance(hits[0].get("entity"), dict)
                or hits[0]["entity"].get("id") != args["entity_id"]):
            raise _Failure("hunt_unverified", "The one ray swing did not confirm the selected animal", evidence)
        evidence["hits"] += 1
        current = skill._prepare(context)
        if current["health"] < initial_health:
            raise _Failure("unsafe", "Damage interrupted the hunt", evidence)
        after, _ = skill._inventory(context)
        skill._prepare(context)  # Reject respawn/life changes during inventory IO.
        budget()
        gained = {item: after.get(item, 0) - before.get(item, 0) for item in expected
                  if after.get(item, 0) > before.get(item, 0)}
        evidence["gained"] = gained
        if gained:
            context["hunting_pending"] = False
            return skill._verified(context, **{**evidence, "completed": True, "inventory_verified": True})
        rows = skill.client.get_entities(radius=8, **budget())
        skill._prepare(context)
        budget()
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise _Failure("invalid_response", "Post-hit entity observation is invalid")
        context["hunting_pending"] = False
        remaining = next((r for r in rows if r.get("id") == args["entity_id"]), None)
        if remaining is None:
            evidence["target_missing"] = True
            break
        if type(remaining.get("health")) in (int, float) and remaining["health"] <= 0:
            evidence["killed_verified"] = True
            break
        cooldown(weapon[1])
    skill._uncollected_drops(entity["pos"], expected, context, evidence)
    raise _Failure("hunt_unverified", "Hunt ended without actual food/material gain; inspect drops before another action", evidence)
