"""One bounded combat step; no pathfinder chase and no despawn-as-kill claim."""

import math
import time

from core.domain import hostile_threat_radius
from core.block_geometry import PASS_THROUGH, _ray_cells
from core.client import APIError
from . import SkillResult
from .survival import SurvivalSkills, _Failure


HOSTILES = frozenset({
    "skeleton", "zombie", "creeper", "spider", "cave_spider",
    "husk", "drowned", "stray", "zombie_villager", "bogged", "witch", "slime", "pillager",
})
TIERS = ("netherite", "diamond", "iron", "stone", "copper", "wooden", "golden")
MELEE = frozenset(tier + suffix for tier in TIERS for suffix in ("_sword", "_axe"))
MIN_HEALTH_SHIELD = 10
MIN_HEALTH_UNSHIELDED = 18
MIN_HEALTH_EASY_MELEE = 8
MIN_FOOD = 8
MELEE_RANGE = 2.8
SHIELD_ADVANCE_MAX = 10.0
RANGED_MIN = 6
RANGED_MAX = 12
SHIELD_WARMUP = .30


def melee_weapon(inventory, *, prefer_tier=False):
    """Choose an observed sword or axe and its conservative cooldown."""
    names = {row.get("name") for row in inventory if isinstance(row, dict) and row.get("count", 0) > 0}
    kinds = ("_sword", "_axe")
    order = ((tier, kind) for tier in TIERS for kind in kinds) if prefer_tier else (
        (tier, kind) for kind in kinds for tier in TIERS)
    for tier, kind in order:
        name = tier + kind
        if name in names:
            cooldown = .65 if kind == "_sword" else 1.30 if tier in {"wooden", "stone", "copper"} else 1.15 if tier == "iron" else 1.05
            return name, cooldown
    return None


def aim_angles(origin, target, height=.9):
    """Mineflayer yaw/pitch from player eyes to the entity's torso."""
    dx, dy, dz = target[0] - origin[0], target[1] + height - origin[1] - 1.62, target[2] - origin[2]
    return math.atan2(-dx, -dz), math.atan2(dy, math.hypot(dx, dz))


def clear_entity_ray(origin, target, get_block, max_queries=48):
    """Check the actual torso ray including its terminal cell, not a block face."""
    origin, target = SurvivalSkills._pos(origin), SurvivalSkills._pos(target)
    start, end = (origin[0], origin[1] + 1.62, origin[2]), (target[0], target[1] + .9, target[2])
    visited = set()
    for cell in _ray_cells(start, end):
        if cell in visited:
            continue
        if len(visited) >= max_queries:
            return {"visible": None, "queries": len(visited), "reason": "query_budget"}
        visited.add(cell)
        block = get_block(list(cell))
        if not isinstance(block, dict) or not isinstance(block.get("name"), str):
            return {"visible": None, "queries": len(visited), "reason": "unknown_block"}
        if block["name"] not in PASS_THROUGH or not SurvivalSkills._safe_block(block):
            return {"visible": False, "queries": len(visited), "reason": "blocked", "blocker": list(cell)}
    return {"visible": True, "queries": len(visited), "reason": "clear_ray"}


def combat_options(observation: dict, entity_id: int | None = None, expected_name: str | None = None,
                   *, starter_defense: bool = False, threat_radius: float = 8) -> dict:
    """Pure eligibility hint; the executing skill rereads geometry and state."""
    denied = lambda reason: {"allowed": False, "reason": reason}
    if (not isinstance(observation, dict) or observation.get("fresh") is not True
            or observation.get("stale") is True or observation.get("inventory_fresh") is False):
        return denied("fresh_inventory_required")
    status, rows, inventory = observation.get("status", {}), observation.get("entities"), observation.get("inventory")
    if not isinstance(status, dict) or not isinstance(rows, list) or not isinstance(inventory, list):
        return denied("invalid_observation")
    if any(not isinstance(r, dict) or not isinstance(r.get("name"), str)
           or type(r.get("count")) is not int or r["count"] < 0 for r in inventory):
        return denied("invalid_inventory")
    if (status.get("ready") is not True or status.get("alive") is not True or status.get("busy")
            or status.get("dimension") not in {"overworld", "minecraft:overworld"}
            or status.get("gamemode") != "survival" or status.get("on_ground") is not True
            or any(status.get(k) for k in ("in_water", "submerged", "in_lava", "on_fire", "is_sleeping", "in_vehicle"))):
        return denied("unsafe_state")
    try:
        origin = SurvivalSkills._pos(status.get("pos"))
        for row in rows:
            if not isinstance(row, dict) or type(row.get("id")) is not int or not isinstance(row.get("name"), str):
                return denied("invalid_entities")
            SurvivalSkills._pos(row.get("pos"))
        if len(rows) >= 40:
            return denied("entity_scan_saturated")
        names = {row["name"] for row in inventory if isinstance(row, dict) and type(row.get("count")) is int and row["count"] > 0}
        shield = "shield" in names or (status.get("offhand") or {}).get("name") == "shield"
        hp = SurvivalSkills._number(status.get("health"), "health", 0, 1024)
        food = SurvivalSkills._number(status.get("food"), "food", 0, 20)
    except (KeyError, TypeError, AttributeError, _Failure):
        return denied("invalid_observation")
    melee = melee_weapon(inventory, prefer_tier=starter_defense)
    targets = [r for r in rows if r["name"] in HOSTILES and r.get("type") == "hostile"
               and (entity_id is None or r["id"] == entity_id)
               and (expected_name is None or r["name"] == expected_name)]
    if not targets:
        if hp < MIN_HEALTH_UNSHIELDED or food < MIN_FOOD:
            return denied("insufficient_health_or_food")
        return denied("target_not_observed")
    target = min(targets, key=lambda r: math.dist(origin, r["pos"]))
    easy_melee_ok = (not starter_defense and melee is not None
                     and target["name"] in {"zombie", "husk", "drowned", "spider", "cave_spider", "zombie_villager", "slime"})
    min_hp = MIN_HEALTH_SHIELD if shield else (MIN_HEALTH_EASY_MELEE if easy_melee_ok else MIN_HEALTH_UNSHIELDED)
    if hp < min_hp or food < MIN_FOOD:
        return denied("insufficient_health_or_food")
    distance = math.dist(origin, target["pos"])
    danger_radius = max(8, threat_radius) if starter_defense else (4 if easy_melee_ok and distance <= MELEE_RANGE else 8)
    if any(r["id"] != target["id"] and r.get("type") == "hostile"
           and math.dist(origin, r["pos"]) <= (
               hostile_threat_radius(r["name"], danger_radius) if starter_defense else danger_radius)
           for r in rows):
        return denied("multiple_hostiles")
    nearest = status.get("nearest_hostile")
    if starter_defense and isinstance(nearest, dict) and nearest.get("id") != target["id"]:
        gap = nearest.get("dist")
        limit = hostile_threat_radius(nearest.get("name"), danger_radius)
        if type(gap) in (int, float) and 0 <= gap <= limit:
            return denied("other_hostile_reported")
    if distance > RANGED_MAX or abs(origin[1] - target["pos"][1]) > 2:
        return denied("target_out_of_range")
    ranged = next((name for name in ("bow", "crossbow") if name in names and "arrow" in names), None)
    mode, weapon = None, None
    if ranged and (8 if target["name"] == "creeper" else RANGED_MIN) <= distance <= RANGED_MAX:
        mode, weapon = "ranged", ranged
    elif target["name"] == "creeper":
        if not starter_defense and melee and shield and hp >= 12 and distance <= MELEE_RANGE:
            mode, weapon = "melee", melee[0]
        else:
            mode = "withdraw"
    elif melee and (1.5 if starter_defense else 0.0) <= distance <= MELEE_RANGE:
        mode, weapon = "melee", melee[0]
    elif (not starter_defense and melee and shield and hp >= MIN_HEALTH_SHIELD
          and target["name"] in {"skeleton", "stray", "bogged", "pillager"}
          and MELEE_RANGE < distance <= SHIELD_ADVANCE_MAX):
        mode, weapon = "shield_advance", melee[0]
    elif distance < 2:
        mode = "withdraw"
    else:
        return denied("no_safe_attack_range")
    if starter_defense:
        if mode != "melee" or target["name"] != "zombie":
            return denied("starter_counterattack_only")
        if weapon.startswith(("wooden_", "golden_")) and hp < 20:
            return denied("weak_weapon_needs_full_health")
    return {"allowed": True, "reason": "bounded_step_available", "mode": mode,
            "target": dict(target), "shield": shield, "weapon": weapon, "distance": distance}


class CombatSkills(SurvivalSkills):
    HOSTILES = HOSTILES
    ARGUMENTS = {"fight_hostile": ({"entity_id", "expected_name"}, {"entity_id", "expected_name"})}
    MAX_SECONDS = 12
    MAX_BLOCK_READS = 128
    UNKNOWN = frozenset({"request_timeout", "network_error", "invalid_json", "invalid_response",
                         "action_unconfirmed", "internal", "internal_error"})

    def __init__(self, client, *, starter_defense=False, threat_radius=8):
        super().__init__(client)
        self.starter_defense = starter_defense
        self.threat_radius = threat_radius
        self.entity_radius = max(16, threat_radius) if starter_defense else 16
        self._encounters = {}
        self._defense_owned = False

    def options(self, observation: dict, entity_id: int | None = None,
                expected_name: str | None = None) -> dict:
        """Use the same eligibility policy for scheduling and write rechecks."""
        return combat_options(observation, entity_id, expected_name, starter_defense=self.starter_defense,
                              threat_radius=self.threat_radius)

    @property
    def defense_active(self):
        return self._defense_owned

    def _encounter(self, context, observation, target, mode):
        status = observation["status"]
        death = status.get("last_death")
        key = (getattr(self.client, "token", None), status["dimension"],
               death.get("t") if isinstance(death, dict) else None, target["id"], target["name"])
        now = time.monotonic()
        record = self._encounters.get(key)
        if record and record.get("cooldown_until", 0) > now:
            raise _Failure("combat_cooldown", "This encounter needs withdrawal and a fresh strategy",
                           {"retreat_required": True, "cooldown_seconds": math.ceil(record["cooldown_until"] - now)})
        if record is None or record.get("cooldown_until"):
            record = {"started": now, "attacks": 0, "unverified": 0}
            self._encounters[key] = record
            while len(self._encounters) > 32:
                self._encounters.pop(next(iter(self._encounters)))
        context["encounter"] = record
        limit = 4 if mode == "ranged" else 6
        if record["attacks"] >= limit or now - record["started"] >= 20:
            record["cooldown_until"] = now + 30
            raise _Failure("combat_budget", "The bounded encounter budget requires withdrawal",
                           {"retreat_required": True, "cooldown_seconds": 30, "encounter_attacks": record["attacks"]})

    def _read_options(self, context):
        self._check(context)
        remaining = context["deadline"] - time.monotonic()
        if remaining <= 0:
            raise _Failure("combat_budget", "The bounded combat step expired")
        return {"timeout_seconds": min(2, remaining)}

    def _block(self, pos, context):
        if context["block_reads"] >= self.MAX_BLOCK_READS:
            raise _Failure("combat_budget", "Combat terrain read budget exhausted")
        context["block_reads"] += 1
        return super()._block(pos, context)

    def _snapshot(self, context):
        first = self._status(context)
        if context.get("life") is None:
            context["life"] = (first.get("last_death"), first["dimension"])
        if context["life"] != (first.get("last_death"), first["dimension"]):
            raise _Failure("life_changed", "Life changed during combat")
        rows = self.client.get_entities(radius=self.entity_radius, **self._read_options(context))
        inv = self._call_result(context, self.client.inventory, **self._read_options(context))
        final = self._status(context)
        if (context["life"] != (final.get("last_death"), final["dimension"])
                or math.dist(first["pos"], final["pos"]) > .4):
            raise _Failure("safety_changed", "Combat observation changed while being read")
        if not isinstance(rows, list) or not isinstance(inv.get("items"), list):
            raise _Failure("invalid_response", "Combat observations are incomplete")
        items = list(inv["items"])
        if isinstance(inv.get("offhand"), dict):
            items.append(inv["offhand"])
        return {"status": final, "entities": rows, "inventory": items, "fresh": True, "inventory_fresh": True}

    def _wait(self, context, seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self._read_options(context)
            duration = min(.05, until - time.monotonic())
            if context["cancel"] is not None:
                context["cancel"].wait(duration)
            else:
                time.sleep(duration)

    @staticmethod
    def _feet(pos):
        return tuple(math.floor(v + .0001) for v in pos)

    COMBAT_EXTRA_SUPPORT = frozenset({
        "dirt_path", "farmland", "stone_bricks", "mossy_cobblestone", "obsidian", "crying_obsidian",
        "netherrack", "basalt", "blackstone", "smooth_basalt",
        "coal_ore", "iron_ore", "copper_ore", "gold_ore", "redstone_ore", "lapis_ore", "diamond_ore", "emerald_ore",
        "deepslate_coal_ore", "deepslate_iron_ore", "deepslate_copper_ore", "deepslate_gold_ore", "deepslate_diamond_ore",
    })
    SUNKEN_SURFACE_SUPPORT = frozenset({"dirt_path", "farmland", "soul_sand", "mud"})

    def _footing(self, context, pos):
        """Known dry floor and full player-width body clearance; never leaves."""
        y = math.floor(pos[1] + .0001)
        support_blocks = self.SUPPORT | self.COMBAT_EXTRA_SUPPORT
        sunken_top = (pos[1] - y) >= 0.8
        for x in range(math.floor(pos[0] - .3 + 1e-5), math.floor(pos[0] + .3 - 1e-5) + 1):
            for z in range(math.floor(pos[2] - .3 + 1e-5), math.floor(pos[2] + .3 - 1e-5) + 1):
                for dy in (-1, 0, 1):
                    block = self._block([x, y + dy, z], context)
                    allowed = support_blocks if dy == -1 else (
                        self.RETREAT_PASSABLE | self.SUNKEN_SURFACE_SUPPORT if (dy == 0 and sunken_top)
                        else self.RETREAT_PASSABLE
                    )
                    if block["name"] not in allowed or not self._safe_block(block):
                        raise _Failure("unsafe_footing", "Combat needs known dry solid footing and body clearance",
                                       {"blocker": [x, y + dy, z], "block": block["name"]})

    def _geometry(self, context, observation, target):
        origin = observation["status"]["pos"]
        self._footing(context, origin)
        # /control selects the first ray hit. Reject nearby bystanders, including
        # players and passive animals, before a sword can also sweep them.
        for row in observation["entities"]:
            if row.get("id") == target["id"] or row.get("name") in {"item", "arrow", "spectral_arrow", "experience_orb"}:
                continue
            p = self._pos(row.get("pos"))
            delta = [target["pos"][i] - origin[i] for i in range(3)]
            length = delta[0] ** 2 + delta[2] ** 2
            t = max(0, min(1, sum((p[i] - origin[i]) * delta[i] for i in (0, 2)) / max(length, .001)))
            line = [origin[i] + t * delta[i] for i in range(3)]
            # Entity dimensions are not exposed; include tall mobs' bodies and
            # the short projectile arc instead of using feet-point distance.
            if (math.hypot(p[0] - line[0], p[2] - line[2]) < 1.5 and abs(p[1] - line[1]) <= 4
                    or math.dist(p, target["pos"]) < 1.5):
                raise _Failure("bystander_risk", "Another observed entity is in the attack corridor")
        sight = clear_entity_ray(origin, target["pos"], lambda pos: self._block(pos, context))
        context["data"]["visibility"] = sight
        if sight["visible"] is not True:
            raise _Failure("target_occluded", "The entity attack ray is not known clear", sight)

    def _write(self, context, operation, *args, status=None, attack_observation=None, **kwargs):
        self._read_options(context)
        latest = self._status(context)
        if status is not None and (self._feet(latest["pos"]) != self._feet(status["pos"])
                                   or abs(latest["pos"][1] - status["pos"][1]) >= .3
                                   or latest.get("health", 0) < status.get("health", 0)):
            raise _Failure("safety_changed", "Position or health changed immediately before the combat write")
        if latest.get("on_ground") is not True or any(latest.get(k) for k in ("in_lava", "in_water", "submerged", "on_fire")):
            raise _Failure("unsafe_state", "Combat write requires stable dry footing")
        if context.get("life") != (latest.get("last_death"), latest["dimension"]):
            raise _Failure("life_changed", "Life changed immediately before the combat write")
        if attack_observation is not None:
            rows = self.client.get_entities(radius=self.entity_radius, **self._read_options(context))
            current = {**attack_observation, "status": latest, "entities": rows}
            expected = context["data"]["target"]
            option = self.options(current, expected["id"], expected["name"])
            if (not option["allowed"] or option.get("mode") != context["data"]["mode"]
                    or math.dist(option["target"]["pos"], expected["pos"]) > .35):
                raise _Failure("safety_changed", "The target or nearby entities changed before attack", {"retreat_required": True})
            self._geometry(context, current, option["target"])
            newest = self._status(context)
            if (self._feet(newest["pos"]) != self._feet(latest["pos"])
                    or math.dist(newest["pos"], latest["pos"]) > .0001
                    or newest["health"] < latest["health"] or newest.get("on_ground") is not True
                    or any(newest.get(k) for k in ("in_lava", "in_water", "submerged", "on_fire"))
                    or context["life"] != (newest.get("last_death"), newest["dimension"])):
                raise _Failure("safety_changed", "Stance changed during final attack geometry verification", {"retreat_required": True})
            if self.starter_defense:
                final_option = self.options({**current, "status": newest}, expected["id"], expected["name"])
                if not final_option["allowed"] or final_option["mode"] != option["mode"]:
                    raise _Failure("safety_changed", "The final status no longer allows a counterattack",
                                   {"retreat_required": True, "reason": final_option["reason"]})
                if (newest.get("held") or {}).get("name") != final_option["weapon"]:
                    raise _Failure("weapon_unverified", "The counterattack weapon changed before the hit",
                                   {"retreat_required": True})
            context["data"]["target"] = option["target"]
            if kwargs.get("hit") is not None:
                # /control uses these angles, unlike /shoot's entity-id aim.
                # They must match the last target and origin whose ray passed.
                kwargs["yaw"], kwargs["pitch"] = aim_angles(latest["pos"], option["target"]["pos"])
        self._read_options(context)
        remaining = context["deadline"] - time.monotonic()
        if remaining <= 0:
            raise _Failure("combat_budget", "The bounded combat step expired before its write")
        kwargs["timeout_seconds"] = min(5, remaining)
        wrote_before = context["write_sent"]
        context["write_sent"] = context["acted"] = context["pending_write"] = True
        if attack_observation is not None:
            context["encounter"]["attacks"] += 1
        context["data"]["executed"] = True
        try:
            reply = self._call_result(context, operation, *args, **kwargs)
        except APIError as error:
            # Upstream equip reports missing_item before changing equipment.
            if (operation == self.client.equip and error.status_code == 200 and error.code == "missing_item"
                    and error.details.get("action_state") != "unknown"):
                context.update(pending_write=False, write_sent=wrote_before, acted=wrote_before)
                context["data"]["executed"] = wrote_before
            raise
        context["pending_write"] = False
        return reply

    def _ensure_shield_offhand(self, context, observation):
        """Ensure shield sits in off-hand slot 45 before posture(shield=True)."""
        status = observation["status"]
        offhand = status.get("offhand") or {}
        if offhand.get("name") == "shield":
            return
        names = {row.get("name") for row in observation.get("inventory", [])
                 if isinstance(row, dict) and type(row.get("count")) is int and row["count"] > 0}
        if "shield" in names:
            self._write(context, self.client.equip, "shield", slot="off-hand", status=status)

    def _shield(self, context, enabled, status, *, warmup=True):
        reply = self._write(context, self.client.posture, shield=enabled, status=status)
        if reply.get("blocking") is not enabled:
            raise _Failure("action_unconfirmed", "Requested shield posture was not observed", {"action_state": "unknown"})
        context["data"]["shield_commanded"] = enabled
        self._defense_owned = enabled
        if enabled and warmup:
            self._wait(context, SHIELD_WARMUP)
        # Upstream blocking is client intention plus equipped item, not proof of
        # server shield cooldown or that a particular incoming hit was blocked.
        context["data"]["shield_protection_verified"] = False

    def _withdraw(self, context, observation, target):
        if self.starter_defense:
            raise _Failure("retreat_required", "Replan withdrawal using the current threats",
                           {"retreat_required": True})
        origin = observation["status"]["pos"]
        dx, dz = origin[0] - target["pos"][0], origin[2] - target["pos"][2]
        norm = math.hypot(dx, dz)
        if norm < .1:
            raise _Failure("retreat_required", "No reliable away direction", {"retreat_required": True})
        # At most a quarter-second of backward walking. Sweep 1.5 blocks before
        # pressing a key; this does not ask the pathfinder to choose a route.
        for step in range(7):
            distance = step * .25
            self._footing(context, [origin[0] + dx / norm * distance, origin[1], origin[2] + dz / norm * distance])
        if context["data"].get("shield") and observation["status"].get("blocking") is not True:
            self._ensure_shield_offhand(context, observation)
            self._write(context, self.client.look_at, target["pos"][0], target["pos"][1] + .9, target["pos"][2],
                        status=observation["status"])
            # Do not stand still near a creeper waiting for the shield. Retreat
            # while it starts; movement safety never relies on the shield.
            self._shield(context, True, observation["status"], warmup=False)
        yaw, _ = aim_angles(origin, target["pos"])
        reply = self._write(context, self.client.control, back=True, yaw=yaw, pitch=0, ms=250, wait=True,
                            status=observation["status"])
        after = self._snapshot(context)
        position = after["status"]["pos"]
        current = next((r for r in after["entities"] if r.get("id") == target["id"]), target)
        farther = math.hypot(position[0] - current["pos"][0], position[2] - current["pos"][2])
        self._footing(context, position)
        if (after["status"].get("on_ground") is not True or abs(position[1] - origin[1]) > .3
                or math.dist(position, origin) > 1.6 or farther < norm + .1):
            raise _Failure("retreat_unverified", "The short withdrawal did not gain confirmed separation",
                           {"retreat_required": True, "pos_after": position})
        context["data"].update(phase="withdraw", retreat_required=True, pos_after=position,
                               moved=math.dist(origin, position), interrupted_by=reply.get("interrupted_by"))

    def _fight(self, context, args):
        observation = self._snapshot(context)
        option = self.options(observation, args["entity_id"], args["expected_name"])
        if not option["allowed"] and context.get("force_override") and option.get("reason") in {"target_out_of_range", "no_safe_attack_range"}:
            target_row = option.get("target") or next(
                (r for r in observation.get("entities", [])
                 if isinstance(r, dict) and r.get("id") == args["entity_id"] and r.get("name") == args["expected_name"]),
                None,
            )
            if isinstance(target_row, dict) and isinstance(target_row.get("pos"), list):
                try:
                    self._write(
                        context,
                        self.client.move_to,
                        float(target_row["pos"][0]),
                        float(target_row["pos"][1]),
                        float(target_row["pos"][2]),
                        range=2.2,
                        timeout_ms=4000,
                        status=observation["status"],
                    )
                    context["data"]["approached"] = True
                    observation = self._snapshot(context)
                    option = self.options(observation, args["entity_id"], args["expected_name"])
                except Exception as move_err:
                    context["data"]["approach_error"] = str(move_err)
        if not option["allowed"]:
            raise _Failure("combat_unsafe", option["reason"], {"retreat_required": True, "reason": option["reason"]})
        target, shield = option["target"], option["shield"]
        before_health = target.get("health")
        context["data"].update(target=target, mode=option["mode"], shield=shield, weapon=option["weapon"])
        if option["mode"] != "withdraw":
            self._encounter(context, observation, target, option["mode"])
        self._footing(context, observation["status"]["pos"])
        if option["mode"] == "withdraw":
            self._withdraw(context, observation, target)
            return
        self._geometry(context, observation, target)
        if observation["status"].get("blocking") and option["mode"] != "shield_advance":
            self._shield(context, False, observation["status"])
        if option["mode"] == "shield_advance":
            weapon, cooldown = melee_weapon(observation["inventory"], prefer_tier=self.starter_defense)
            self._ensure_shield_offhand(context, observation)
            self._write(context, self.client.equip, weapon, status=observation["status"])
            self._write(context, self.client.look_at, target["pos"][0], target["pos"][1] + .9, target["pos"][2], status=observation["status"])
            if observation["status"].get("blocking") is not True:
                self._shield(context, True, observation["status"], warmup=True)
            # If an arrow is already airborne nearby, absorb it briefly before advancing.
            origin = observation["status"]["pos"]
            incoming_arrow = any(
                r.get("name") == "arrow" and math.dist(origin, self._pos(r.get("pos"))) <= 6.5
                for r in observation.get("entities", [])
            )
            if incoming_arrow:
                self._wait(context, .25)
            dx, dz = target["pos"][0] - origin[0], target["pos"][2] - origin[2]
            norm = math.hypot(dx, dz)
            if norm >= .5:
                step_check = [origin[0] + dx / norm * min(1.25, norm), origin[1], origin[2] + dz / norm * min(1.25, norm)]
                self._footing(context, step_check)
                yaw, _ = aim_angles(origin, target["pos"])
                self._write(context, self.client.control, forward=True, yaw=yaw, pitch=0, ms=450, wait=True,
                            status=observation["status"])
                context["data"]["shield_advanced"] = True
            self._wait(context, cooldown)
        elif option["mode"] == "melee":
            weapon, cooldown = melee_weapon(observation["inventory"], prefer_tier=self.starter_defense)
            if shield:
                self._ensure_shield_offhand(context, observation)
            self._write(context, self.client.equip, weapon, status=observation["status"])
            self._write(context, self.client.look_at, target["pos"][0], target["pos"][1] + .9, target["pos"][2], status=observation["status"])
            if shield:
                self._shield(context, True, observation["status"])
            self._wait(context, cooldown)
        refreshed = self._snapshot(context)
        fresh_option = self.options(refreshed, args["entity_id"], args["expected_name"])
        if not any(r.get("id") == target["id"] and r.get("name") == target["name"] for r in refreshed["entities"]):
            if self._defense_owned:
                self._shield(context, False, refreshed["status"])
            context["data"].update(phase="target_missing", target_missing=True)
            return
        mode_ok = (fresh_option.get("mode") == option["mode"]) or (
            option["mode"] == "shield_advance" and fresh_option.get("mode") == "melee"
        )
        if not fresh_option["allowed"] or not mode_ok or refreshed["status"]["health"] < observation["status"]["health"]:
            self._withdraw(context, refreshed, target)
            return
        if option["mode"] == "shield_advance" and fresh_option.get("mode") != "melee":
            # Closed distance while blocking; hold shield until next cycle closes the rest.
            self._write(context, self.client.look_at, fresh_option["target"]["pos"][0],
                        fresh_option["target"]["pos"][1] + .9, fresh_option["target"]["pos"][2], status=refreshed["status"])
            context["data"].update(phase="shield_advance", completed=False)
            return
        target = fresh_option["target"]
        context["data"]["target"] = target
        self._geometry(context, refreshed, target)
        if fresh_option["mode"] == "melee":
            if (refreshed["status"].get("held") or {}).get("name") != option["weapon"]:
                raise _Failure("weapon_unverified", "The selected weapon is no longer held")
            if shield:
                if refreshed["status"].get("blocking") is not True:
                    raise _Failure("shield_unverified", "The shield command is no longer active")
                self._shield(context, False, refreshed["status"])
            yaw, pitch = aim_angles(refreshed["status"]["pos"], target["pos"])
            reply = self._write(context, self.client.control, yaw=yaw, pitch=pitch, hit=1, ms=50, wait=True,
                                status=refreshed["status"], attack_observation=refreshed)
            hits = reply.get("hits")
            if not isinstance(hits, list) or len(hits) != 1 or not isinstance(hits[0], dict):
                raise _Failure("action_unconfirmed", "Single-hit acknowledgement is malformed")
            selected = hits[0].get("entity")
            if not isinstance(selected, dict) or selected.get("id") != target["id"]:
                raise _Failure("action_unconfirmed" if selected is not None else "attack_unverified", "The bounded ray did not select the expected entity",
                               {"selected_entity": selected, "retreat_required": True})
            context["data"]["attack_sent"] = True
        else:
            before_ammo = sum(r.get("count", 0) for r in refreshed["inventory"] if r.get("name") == "arrow")
            charge_ms = 1400 if option["weapon"] == "crossbow" else 1100
            reply = self._write(context, self.client.shoot, target["id"], weapon=option["weapon"], charge_ms=charge_ms,
                                status=refreshed["status"], attack_observation=refreshed)
            context["data"].update(attack_sent=reply.get("fired") is True, projectiles=reply.get("projectiles", [])[:8])
        context["attack_needs_verification"] = True
        after = self._snapshot(context)
        current = next((r for r in after["entities"] if r.get("id") == target["id"] and r.get("name") == target["name"]), None)
        health = current.get("health") if current else None
        measured = type(before_health) in (int, float) and type(health) in (int, float) and math.isfinite(before_health) and math.isfinite(health)
        if option["mode"] == "ranged":
            ammo = sum(r.get("count", 0) for r in after["inventory"] if r.get("name") == "arrow")
            context["data"]["ammo_used"] = max(0, before_ammo - ammo)
            if not context["data"]["attack_sent"] or not (ammo < before_ammo or reply.get("projectiles")):
                raise _Failure("attack_unverified", "No projectile or ammunition change confirmed the shot")
        context["attack_needs_verification"] = False
        context["data"].update(phase="attack", damage_verified=bool(measured and health < before_health),
                               killed=bool(measured and health <= 0), completed=bool(measured and health <= 0),
                               target_missing=current is None, target_health_before=before_health, target_health_after=health)
        encounter = context["encounter"]
        encounter["unverified"] = 0 if context["data"]["damage_verified"] else encounter["unverified"] + 1
        context["data"].update(encounter_attacks=encounter["attacks"], unverified_attacks=encounter["unverified"])
        if current is not None and encounter["unverified"] >= (2 if option["mode"] == "ranged" else 3):
            encounter["cooldown_until"] = time.monotonic() + 30
            raise _Failure("combat_no_progress", "Repeated attacks have no verified damage; withdraw before replanning",
                           {"retreat_required": True, "cooldown_seconds": 30})
        if (target["name"] == "creeper" and current is not None and not context["data"]["completed"]):
            context["data"]["retreat_required"] = True
            context["data"]["creeper_kited"] = True
            self._withdraw(context, after, current)
        elif (after["status"]["health"] < observation["status"]["health"]
                or not self.options(after, args["entity_id"], args["expected_name"])["allowed"] and current is not None):
            context["data"]["retreat_required"] = True
            if current is not None:
                self._withdraw(context, after, current)
        elif shield and current is not None and not context["data"]["completed"]:
            self._write(context, self.client.look_at, current["pos"][0], current["pos"][1] + .9, current["pos"][2], status=after["status"])
            self._shield(context, True, after["status"])

    def release_defense(self, cancel_event=None):
        """Harness-only cleanup at a fresh idle boundary, never a model action."""
        data = {"executed": False, "released": False, "skill": "release_defense"}
        if not self._defense_owned:
            return SkillResult(True, "No combat-owned shield posture remains", data)
        context = {"cancel": cancel_event, "acted": False, "write_sent": False, "data": data,
                   "deadline": time.monotonic() + self.MAX_SECONDS, "block_reads": 0}
        try:
            observation = self._snapshot(context)
            if len(observation["entities"]) >= 40 or any(
                    r.get("type") == "hostile" and math.dist(observation["status"]["pos"], self._pos(r.get("pos"))) <= 12
                    for r in observation["entities"]):
                raise _Failure("combat_threat_present", "Keep the shield while a fresh nearby threat remains")
            if observation["status"].get("blocking") is not True:
                self._defense_owned = False
                return SkillResult(True, "The combat shield posture is already inactive", data)
            self._shield(context, False, observation["status"])
            after = self._status(context)
            if after.get("blocking") is not False:
                raise _Failure("action_unconfirmed", "Shield release could not be verified")
            data["released"] = True
            return SkillResult(True, "Combat shield posture was explicitly released", data)
        except Exception as error:
            error = self._disconnection_error(error, context)
            code = getattr(error, "code", "internal_error")
            if context["write_sent"]:
                code = "action_unconfirmed"
                data.update(action_state="unknown", cleanup_skipped="combat_unknown")
            if context.get("disconnected_unknown"):
                data.update(cleanup_skipped="disconnected_unknown", api_details=context["disconnection_details"])
            return SkillResult(False, str(error), data, code)

    def execute(self, name, args, cancel_event=None, *, force_override=False):
        data = {"skill": name, "executed": False, "attack_sent": False, "killed": False,
                "completed": False, "damage_verified": False, "phase": "checking"}
        context = {"cancel": cancel_event, "acted": False, "write_sent": False, "data": data,
                   "deadline": time.monotonic() + self.MAX_SECONDS, "block_reads": 0,
                   "force_override": bool(force_override)}
        try:
            if (name != "fight_hostile" or not isinstance(args, dict) or set(args) != {"entity_id", "expected_name"}
                    or type(args["entity_id"]) is not int or args["entity_id"] < 0
                    or not isinstance(args["expected_name"], str) or args["expected_name"] not in HOSTILES):
                raise _Failure("bad_args", "fight_hostile requires one supported observed entity id and name")
            self._fight(context, args)
            return SkillResult(True, "One bounded combat step completed; damage and kill evidence are reported separately", data)
        except Exception as error:
            error = self._disconnection_error(error, context)
            code = getattr(error, "code", "internal_error")
            data.update(getattr(error, "data", {}))
            if isinstance(getattr(error, "details", None), dict):
                data["api_details"] = error.details
            uncertain = (context.get("pending_write") or context.get("attack_needs_verification")
                         or code in self.UNKNOWN and context["write_sent"])
            if uncertain:
                data.update(action_state="unknown", cleanup_skipped="combat_unknown")
                code = "action_unconfirmed"
            if context.get("disconnected_unknown"):
                data.update(action_state="unknown", cleanup_skipped="disconnected_unknown",
                            api_details=context["disconnection_details"])
            if not context["write_sent"] and not uncertain:
                data.setdefault("action_state", "not_executed")
            data.setdefault("retreat_required", code not in {"bad_args", "cancelled"})
            return SkillResult(False, str(error), data, code)
        finally:
            data["block_reads"] = context["block_reads"]
