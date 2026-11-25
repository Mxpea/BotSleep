"""Read-only confirmation for one previously issued foliage dig."""

import copy
from datetime import datetime
import json
import math
import time

from core.client import APIError
from core.domain import cell_coords, finite_number, parse_pos
from skills import SkillResult
from skills.survival import SurvivalSkills


LEAVES = SurvivalSkills.RETREAT_LEAVES
NEIGHBOURS = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))


def foliage_intent(decision, result, status, generation, footing=None):
    """Bind the exact issued action to its before-state, never to a guessed leaf."""
    data = result.get("data") if isinstance(result, dict) else None
    if (not isinstance(decision, dict) or not isinstance(status, dict) or not isinstance(data, dict)
            or result.get("success") is not False or data.get("executed") is not True
            or data.get("action_state") != "unknown" or type(generation) is not int
            or generation < 0 or status.get("dimension") not in ("overworld", "minecraft:overworld")
            or not finite_number(status.get("health")) or status["health"] <= 0):
        return None
    kind = decision.get("name")
    if kind not in ("canopy_escape", "clear_foliage") or data.get("skill") != kind:
        return None
    origin = parse_pos(data.get("pos_before"))
    measured = parse_pos(status.get("pos"))
    if origin is None or measured is None or math.dist(measured, origin) > .2:
        return None
    feet = cell_coords(origin)
    source = [feet[0], feet[1] - 1, feet[2]]
    destination = None
    if kind == "canopy_escape":
        leaf = parse_pos(data.get("cleared_pos"), integer=True)
        name = data.get("cleared_name")
        ground = data.get("origin_support") or {}
        step = parse_pos(data.get("descent_target"))
        if (decision.get("args") != {} or data.get("operation") != "clear_foliage" or data.get("moved") != 0
                or data.get("target") is not None or data.get("origin") != origin or step is None
                or leaf is None or not isinstance(name, str) or name not in LEAVES
                or ground.get("center") != source or ground.get("state") != "center_supported"
                or step[0] != leaf[0] + .5 or step[2] != leaf[2] + .5
                or step[1] != math.floor(step[1]) or not 0 <= feet[1] - step[1] <= 2
                or not step[1] <= leaf[1] <= feet[1] + 1
                or abs(leaf[0] - feet[0]) + abs(leaf[2] - feet[2]) != 1):
            return None
        source_name = ground.get("center_name")
        destination = [leaf[0], int(step[1]) - 1, leaf[2]]
    else:
        args = decision.get("args") or {}
        leaf = parse_pos(args.get("pos"), integer=True)
        name = args.get("expected_name")
        if (leaf is None or not isinstance(name, str) or name not in LEAVES | {"vine"}
                or max(abs(leaf[0] - feet[0]), abs(leaf[2] - feet[2])) != 1
                or leaf[1] not in (feet[1], feet[1] + 1)
                or data.get("pos", leaf) != leaf or data.get("expected_name", name) != name):
            return None
        source_name = (footing or {}).get("name") if (footing or {}).get("pos") == source else None
    if (not isinstance(source_name, str) or source_name not in SurvivalSkills.SUPPORT | LEAVES | SurvivalSkills.RETREAT_TRUNKS
            or source == leaf or destination == leaf or math.dist(origin, leaf) > 3):
        return None
    return {"decision": copy.deepcopy(decision), "pos": leaf, "before_name": name, "origin": origin,
            "source": source, "source_name": source_name, "destination": destination,
            "destination_name": data.get("destination_support_name"), "dimension": status["dimension"],
            "life": copy.deepcopy(status.get("last_death")), "generation": generation, "health": status["health"]}


def _latest_events(path):
    # A paused bot can write hours of monitoring after its last world action.
    # Look back to the last command without loading the whole journal into RAM.
    with path.open("rb") as stream:
        offset = stream.seek(0, 2)
        partial = b""
        while offset:
            size = min(offset, 64 * 1024)
            offset -= size
            stream.seek(offset)
            parts = (stream.read(size) + partial).split(b"\n")
            partial = parts[0]
            for line in reversed(parts[1:]):
                if line.strip():
                    yield json.loads(line)
        if partial.strip():
            yield json.loads(partial)


def pending_foliage(journal):
    """Migrate old atomic checkpoints only when the matching recorded command and result agree."""
    state, scope = journal.state, journal.scope
    pending = state.get("pending") or {}
    if state.get("scope") != scope or not isinstance(pending, dict):
        return None
    saved = pending.get("foliage_readback")
    context = state.get("context") or {}
    if isinstance(saved, dict):
        baseline = {"pos": saved.get("origin"), "dimension": saved.get("dimension"), "health": saved.get("health"),
                    "last_death": saved.get("life")}
        checked = foliage_intent(pending.get("decision"), pending.get("foliage_result"), baseline, saved.get("generation"),
                                 {"name": saved.get("source_name"), "pos": saved.get("source")})
        if not checked or {**checked, "scope": scope} != saved:
            return {"invalid": True}
        if saved["generation"] == context.get("life_generation"):
            return saved
    decision = pending.get("decision") or {}
    result = state.get("last_result") or {}
    if (decision.get("name") not in ("canopy_escape", "clear_foliage")
            or result != context.get("last_result") or not isinstance(result.get("data"), dict)
            or result["data"].get("action_state") != "unknown"):
        return None
    # A stale previous result cannot resolve a later interrupted command with identical arguments.
    path = getattr(journal, "events_path", None)
    if path is None:
        return None
    try:
        matched = False
        deaths = []
        generation_at_write = (saved or {}).get("generation")
        for row in _latest_events(path):
            info = row.get("data") or {}
            kind = row.get("event")
            if kind == "DEATH_OBSERVED" and not matched:
                death = info.get("last_death")
                timestamp = (death or {}).get("t")
                if (not isinstance(death, dict) or type(timestamp) is not int
                        or parse_pos(death.get("pos")) is None):
                    return {"invalid": True}
                deaths.append(death)
            elif kind == "STEP_RESULT":
                if matched or info.get("decision") != decision or info.get("result") != result:
                    return None
                matched = True
                result_time = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp() * 1000
            elif kind == "LOCAL_RECOVERY_UNKNOWN" and matched and info.get("kind") == "canopy_recovery":
                recovery = info.get("recovery") or {}
                if (recovery.get("unknown") is True and recovery.get("origin") == result["data"].get("origin")
                        and recovery.get("dimension") == context.get("last_dimension")
                        and type(recovery.get("life_generation")) is int):
                    if generation_at_write is not None and generation_at_write != recovery["life_generation"]:
                        return {"invalid": True}
                    generation_at_write = recovery["life_generation"]
            elif kind == "DECISION":
                before = info.get("status") or {}
                if (not matched or info.get("decision") != decision or info.get("counts") != pending.get("before_counts")
                        or before.get("username", scope["username"]) != scope["username"]):
                    return None
                if deaths and generation_at_write is None:
                    return {"invalid": True}
                gen = generation_at_write if generation_at_write is not None else context.get("life_generation")
                intent = saved or foliage_intent(decision, result, before, gen,
                                                 (context.get("local_footing") or {}).get("support"))
                if not intent or gen == context.get("life_generation") and not deaths:
                    return intent
                deaths.reverse()
                times = [death["t"] for death in deaths]
                # A reconnect can expose a later death marker on its first
                # heartbeat without incrementing the local counter. Earlier
                # checked transitions already prove the digging life is over.
                latest = json.loads(context.get("last_death") or "null")
                if (not times or times != sorted(set(times)) or times[0] <= result_time
                        or type(context.get("life_generation")) is not int or gen >= context["life_generation"]
                        or context.get("was_alive") is not True
                        or not isinstance(latest, dict) or type(latest.get("t")) is not int
                        or parse_pos(latest.get("pos")) is None or latest["t"] < times[-1]
                        or latest["t"] == times[-1] and latest != deaths[-1]):
                    return {"invalid": True}
                return {**intent, "completed_life": {"new_generation": context["life_generation"],
                    "last_death": latest, "death_times": times}}
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    return None


def confirm_foliage(client, intent, generation, cancel, threat_radius=8):
    """Two stable samples within a single short deadline. Only GET-like observations."""
    transition = intent.get("completed_life")
    if generation != (transition["new_generation"] if transition else intent["generation"]):
        return None, "life_generation_changed"
    deadline = time.monotonic() + 3

    def options():
        if cancel.is_set() or time.monotonic() >= deadline:
            raise ValueError("observation_cancelled_or_timed_out")
        return {"timeout_seconds": min(1.5, deadline - time.monotonic())}

    def read(p, *, historical=False):
        r = client.get_block(p, **options())
        b = r.get("result") if isinstance(r, dict) and r.get("ok") is True else None
        if (not isinstance(b, dict) or b.get("pos") != p or not isinstance(b.get("name"), str)
                or b["name"] == "unknown" or not isinstance(b.get("properties", {}), dict)
                or not historical and not SurvivalSkills._safe_block(b)):
            raise ValueError("unsafe_or_unloaded_block")
        return b

    def status():
        r = client.get_status(**options())
        s = r.get("result") if isinstance(r, dict) and r.get("ok") is True else None
        if (not isinstance(s, dict) or s.get("alive") is not True or s.get("ready") is not True
                or "busy" not in s or s["busy"] not in (None, False) or s.get("on_ground") is not True
                or s.get("gamemode") != "survival" or s.get("dimension") != intent["dimension"]
                or s.get("last_death") != (transition["last_death"] if transition else intent["life"])
                or not finite_number(s.get("health"))
                or s["health"] <= 0 or parse_pos(s.get("pos")) is None
                or any(s.get(k) for k in ("jumping", "held_controls", "in_water", "in_lava", "submerged", "on_fire", "in_vehicle", "is_sleeping"))
                or not finite_number(s.get("speed")) or s["speed"] > .1 or parse_pos(s.get("velocity")) is None
                or math.dist(s["velocity"], [0, 0, 0]) > .1):
            raise ValueError("stance_life_or_activity_changed")
        return s

    def landing(s, danger):
        # The old dig was the only issued write. A stable new landing proves
        # we may abandon that intention, not who removed a block or moved us.
        x, y, z = cell_coords(s["pos"])
        if abs(s["pos"][1] - y) > .1 or not transition and math.dist(s["pos"], intent["origin"]) > 16:
            raise ValueError("unverified_new_landing")
        px, _, pz = s["pos"]
        supports = []
        for cx in range(math.floor(px-.3501), math.floor(px+.3501)+1):
            for cz in range(math.floor(pz-.3501), math.floor(pz+.3501)+1):
                point = [cx, y-1, cz]
                support = read(point)
                if support["name"] not in SurvivalSkills.AIR:
                    if (support["name"] not in SurvivalSkills.SUPPORT | LEAVES | SurvivalSkills.RETREAT_TRUNKS or
                            support["name"] in LEAVES and (support.get("properties") or {}).get("waterlogged") not in (False, "false")):
                        raise ValueError("unsafe_new_support")
                    if min(SurvivalSkills.contact_overlap(s["pos"], point)) > 1e-4:
                        supports.append({"pos": point, "name": support["name"]})
                for dy in (0, 1):
                    if read([cx, y+dy, cz])["name"] not in SurvivalSkills.RETREAT_PASSABLE:
                        raise ValueError("new_stance_body_blocked")
        if not supports:
            raise ValueError("unsafe_new_support")
        for dy in (-1, 0, 1):
            for dx, dz in ((-1,0),(1,0),(0,-1),(0,1)):
                read([x+dx, y+dy, z+dz])
        prior = {}
        for key in (() if transition else ("pos", "source", "destination")):
            if intent.get(key) is not None:
                prior[key] = {"pos": intent[key], "name": read(intent[key], historical=True)["name"]}
        after = status()
        if (cell_coords(s["pos"]) != cell_coords(after["pos"]) or math.dist(s["pos"], after["pos"]) > .1
                or s["health"] != after["health"]):
            raise ValueError("new_landing_changed")
        return {"remaining_block": (prior.get("pos") or {}).get("name"), "supports": supports,
                "hostile_nearby": danger, "new_stance": list(after["pos"]), "health": after["health"],
                "resolution": "superseded_life" if transition else "superseded_stance",
                "completed_life": transition, "current_death_marker": after.get("last_death"), "original_terrain_after": prior,
                "original_action_confirmed": False}

    def sample():
        s = status()
        entities = client.get_entities(radius=max(16, threat_radius), types=["hostile"], **options())
        if (not isinstance(entities, list) or any(
                not isinstance(e, dict) or parse_pos(e.get("pos")) is None or not e.get("name") for e in entities)):
            raise ValueError("unverified_hostile_observation")
        # An approaching enemy does not change an observed air block. Tell
        # the harness to pass the next decision straight back to local safety.
        danger = len(entities) >= 40 or any(
            math.dist(s["pos"], e["pos"]) <= max(threat_radius, 16 if e["name"] in
                {"skeleton", "stray", "bogged", "pillager"} else 8) for e in entities)
        if transition or cell_coords(s["pos"]) != cell_coords(intent["origin"]):
            return landing(s, danger)
        if s["health"] < intent["health"] or math.dist(s["pos"], intent["origin"]) > .2:
            raise ValueError("stance_life_or_activity_changed")
        p = intent["pos"]
        target = read(p)["name"]
        if target not in SurvivalSkills.AIR and target != intent["before_name"]:
            raise ValueError("target_replaced")
        for dx, dy, dz in NEIGHBOURS:
            name = read([p[0]+dx, p[1]+dy, p[2]+dz])["name"]
            if dy == 1 and (name in {"sand", "red_sand", "gravel", "anvil", "chipped_anvil", "damaged_anvil"}
                            or name.endswith("_concrete_powder")):
                raise ValueError("unstable_overhead_block")
        supports = []
        for key in ("source", "destination"):
            point = intent[key]
            if point is None:
                continue
            b = read(point)
            if (b["name"] not in SurvivalSkills.SUPPORT | LEAVES | SurvivalSkills.RETREAT_TRUNKS
                    or b["name"] in LEAVES and (b.get("properties") or {}).get("waterlogged") not in (False, "false")
                    or intent[key+"_name"] is not None and b["name"] != intent[key+"_name"]):
                raise ValueError("support_changed")
            supports.append({"pos": point, "name": b["name"]})
        after = status()
        if (cell_coords(after["pos"]) != cell_coords(intent["origin"]) or math.dist(after["pos"], intent["origin"]) > .2
                or after["health"] < intent["health"]):
            raise ValueError("stance_life_or_activity_changed")
        return {"remaining_block": target, "supports": supports, "hostile_nearby": danger}

    try:
        # The backend may briefly expose its pre-dig cache. An unchanged leaf is never redigged.
        last = None
        reason = "stable_air_samples_missing"
        for attempt in range(4):
            if attempt and cancel.wait(.15):
                return None, "cancelled"
            evidence = sample()
            relocated = evidence.get("resolution") in ("superseded_stance", "superseded_life")
            gone = not relocated and evidence["remaining_block"] in SurvivalSkills.AIR
            # Give delayed removal the entire read window. Stable unchanged
            # geometry can only abandon this dig and exclude its target.
            comparable = {k:v for k,v in evidence.items() if k != "hostile_nearby"}
            if (last == comparable and not cancel.is_set() and (relocated or gone or attempt == 3)):
                return {**evidence, "removed": gone, "read_only": True, "stable_samples": 2, "pos": intent["pos"]}, None
            last = comparable
        return None, reason
    except (APIError, ValueError, TypeError, KeyError, AttributeError) as error:
        return None, getattr(error, "code", None) or str(error)[:120]


def resolved_result(result, proof):
    if proof.get("resolution") in ("superseded_stance", "superseded_life"):
        return SkillResult(False, "Current stance verified; abandon the earlier dig with its effect unknown",
            {"executed": True, "action_state": "superseded", "readback": proof,
             "next_step_requires_replan": True, "original_action_confirmed": False}, "foliage_site_changed")
    if not proof["removed"]:
        return SkillResult(False, "Foliage still present after fresh reads; exclude this dig and replan",
            {"executed": True, "action_state": "reobserved", "readback": proof,
             "next_step_requires_replan": True, "block_removed": False}, "foliage_still_present")
    data = {**result.data, "action_state": "verified", "block_removed": True, "plant_removed": True,
            "remaining_block": proof["remaining_block"], "readback": proof, "next_step_requires_replan": True}
    if data.get("operation") == "clear_foliage":
        data.update(evacuation_stage="relay", moved=0, ground_route_verified=False, retreat_complete=False)
    for key in ("recovery_error", "cleanup_skipped", "verification_error"):
        data.pop(key, None)
    return SkillResult(True, "Fresh read-only observations confirmed the issued foliage removal", data)
