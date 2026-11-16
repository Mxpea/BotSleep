"""Versioned, offline Java survival guidance; suggestions are never completion.

Only explicit runtime capabilities can become next_actions. Later speedrun
operations remain blocked until adapters and fresh evidence actually exist.
See docs/SURVIVAL_PLAYBOOK.md for the evidence contract and researched sources.
"""

import json
import math
import re


SCHEMA_VERSION = 1
MAX_BYTES = 2048
JAVA_VERSION = "1.21.11"
API_VERSION = "1.3.14"
KNOWN_TOOLS = frozenset({
    "gather_block", "gather_resource", "craft_item", "place_item", "eat_food",
    "hunt_animal", "harvest_crop", "gather_seeds", "plant_crop", "till_soil",
    "inspect_container", "smelt_start", "smelt_collect", "collect_drops",
    "plan_shelter", "inspect_shelter", "auto_sleep", "retreat_from",
    "fight_hostile", "collect_liquid", "hunt_with_lava",
})
LOGS = tuple(name + "_log" for name in (
    "oak", "spruce", "birch", "jungle", "acacia", "dark_oak", "mangrove", "cherry", "pale_oak"))
PLANKS = tuple(name.removesuffix("_log") + "_planks" for name in LOGS)
PICKS = ("wooden_pickaxe", "stone_pickaxe", "iron_pickaxe", "diamond_pickaxe", "netherite_pickaxe")
FOODS = ("cooked_beef", "cooked_porkchop", "cooked_mutton", "cooked_chicken",
         "cooked_rabbit", "cooked_cod", "cooked_salmon", "bread", "baked_potato", "carrot", "apple")
RAW_FOOD = {"beef": "cooked_beef", "porkchop": "cooked_porkchop", "mutton": "cooked_mutton",
            "chicken": "cooked_chicken", "rabbit": "cooked_rabbit", "cod": "cooked_cod",
            "salmon": "cooked_salmon", "potato": "baked_potato"}
RECIPES = {
    "crafting_table": {"planks": 4}, "stick": {"planks": 2},
    "wooden_pickaxe": {"planks": 3, "stick": 2},
    "stone_pickaxe": {"cobblestone": 3, "stick": 2}, "furnace": {"cobblestone": 8},
    "shield": {"planks": 6, "iron_ingot": 1}, "bucket": {"iron_ingot": 3},
    "iron_pickaxe": {"iron_ingot": 3, "stick": 2}, "iron_boots": {"iron_ingot": 4},
    "flint_and_steel": {"iron_ingot": 1, "flint": 1},
}
STAGES = {
    "opening": ("stone_pickaxe", "food_reserve", "furnace"),
    "iron_defense": ("shield", "bucket", "iron_pickaxe"),
    "home_farm": ("verified_shelter", "verified_bed", "verified_storage", "renewable_food_site"),
    "maintenance": ("fresh_food", "tools", "rechecked_home"),
    "nether_preparation": ("food:12", "solid_blocks:32", "water_bucket", "ignition", "gold_armor"),
    "nether_portal": ("verified_frame_and_ignition", "safe_entry_and_return_route"),
    "nether_resources": ("blaze_powder_equivalent:14", "ender_pearl_equivalent:14", "safe_return"),
    "stronghold": ("eyes_with_spares", "fresh_eye_samples", "verified_portal_room"),
    "end_preparation": ("checked_empty_frames", "food:12", "water_bucket", "bow_and_arrows", "armor", "safe_entry"),
    "dragon": ("crystals_observed", "ranged_and_melee_adapters", "void_safe_routes"),
    "post_dragon": ("server_confirmed_dragon_defeat", "verified_exit"),
    "recovery": ("fresh_life_and_inventory", "no_unknown_write", "safe_observed_drops"),
    "safety": ("fresh_status", "supported_geometry", "local_threat_gate"),
}


def _pos(value):
    return (isinstance(value, (list, tuple)) and len(value) == 3
            and all(type(n) in (int, float) and math.isfinite(n) and abs(n) <= 30000000 for n in value))


def _dimension(value):
    return value.removeprefix("minecraft:") if isinstance(value, str) else None


class SurvivalPlaybook:
    """Pure bounded advice for a caller-selected set of executable tool names."""

    def __init__(self, available_tools=(), *, java_version=JAVA_VERSION, goal="survival"):
        self.available_tools = frozenset(name for name in available_tools if isinstance(name, str)) & KNOWN_TOOLS
        self.java_version = java_version
        self.goal = goal

    def build(self, observation: dict, context=None) -> dict:
        """Return <=2048 UTF-8 JSON bytes; no API, model, disk or state mutation."""
        obs = observation if isinstance(observation, dict) else {}
        ctx = context if isinstance(context, dict) else {}
        status = obs.get("status") if isinstance(obs.get("status"), dict) else {}
        dim = _dimension(status.get("dimension"))
        report = {"schema_version": SCHEMA_VERSION, "profile": "java_1_21_11", "goal": self.goal,
                  "stage": "safety", "state": "blocked", "prerequisites": [], "missing": [],
                  "next_actions": [], "risk_notes": ["Suggestions require fresh skill validation."],
                  "bindings": [], "unsupported": [], "completed_evidence": [], "source_ids": ["java12111"]}

        def finish(stage, missing=(), actions=(), risks=(), unsupported=(), evidence=(), state=None, sources=()):
            report.update(stage=stage, prerequisites=list(STAGES.get(stage, ())), missing=list(missing)[:8],
                          next_actions=list(actions)[:3], unsupported=list(unsupported)[:6],
                          completed_evidence=list(evidence)[:4], source_ids=["java12111", *sources][:4])
            report["risk_notes"] += list(risks)[:3]
            report["bindings"] = sorted({row["name"] for row in report["next_actions"]})
            report["state"] = state or ("prerequisites" if missing and actions else "ready" if actions else "blocked")
            # All text is bounded constants or a validated item ID. Fail closed
            # if a future change accidentally expands this compact contract.
            if len(json.dumps(report, allow_nan=False).encode("utf-8")) > MAX_BYTES:
                report.update(state="blocked", next_actions=[], bindings=[], missing=["playbook_context_budget"],
                              risk_notes=["Reobserve; no advice was emitted."], completed_evidence=[])
            return report

        def action(name, args, reason):
            return ([{"name": name, "args": args, "reason": reason}]
                    if name in self.available_tools else [])

        if self.java_version != JAVA_VERSION or not isinstance(self.goal, str) or self.goal not in {"survival", "speedrun"}:
            report["goal"] = self.goal if isinstance(self.goal, str) and self.goal in {"survival", "speedrun"} else "unknown"
            return finish("safety", ["unsupported_version_or_goal"], risks=["Never apply Java 1.16.1 or Bedrock routes unchanged."])
        last = ctx.get("last_result") if isinstance(ctx.get("last_result"), dict) else {}
        last_data = last.get("data") if isinstance(last.get("data"), dict) else {}
        if (ctx.get("pending") or ctx.get("action_state") == "unknown" or last_data.get("action_state") == "unknown"
                or last.get("error_code") in {"request_timeout", "dig_unconfirmed", "llm_request_uncertain"}):
            return finish("recovery", ["resolve_unknown_action"], risks=["Do not replay, replace or retry an uncertain write."])
        if (obs.get("fresh") is not True or obs.get("stale") is True
                or obs.get("observation_scope") == "safety" or obs.get("inventory_fresh") is False):
            return finish("safety", ["fresh_complete_observation"])
        if status.get("ready") is not True or status.get("alive") is not True or not _pos(status.get("pos")):
            return finish("recovery", ["ready_alive_position"], risks=["Natural respawn requires a new inventory observation."])
        if status.get("gamemode") != "survival" or dim not in {"overworld", "the_nether", "the_end", "nether", "end"}:
            return finish("safety", ["supported_survival_dimension"])
        if status.get("busy") or status.get("on_ground") is not True or any(status.get(key) for key in ("on_fire", "in_lava", "submerged", "in_vehicle", "is_sleeping")):
            return finish("safety", ["stable_safe_state"])
        raw_counts = obs.get("counts")
        if (not isinstance(raw_counts, dict) or any(not isinstance(key, str) or type(value) is not int
                or not 0 <= value <= 1000000 for key, value in raw_counts.items())):
            return finish("safety", ["fresh_inventory_counts"])
        counts = {key: value for key, value in raw_counts.items()
                  if isinstance(key, str) and type(value) is int and 0 <= value <= 1000000}
        offhand = status.get("offhand") if isinstance(status.get("offhand"), dict) else {}
        shield_owned = counts.get("shield", 0) or (offhand.get("name") == "shield"
                                                  and type(offhand.get("count")) is int and offhand["count"] > 0)
        armor = obs.get("armor", status.get("armor", {}))
        equipped = {row.get("name") for row in armor.values() if isinstance(row, dict)
                    and type(row.get("count")) is int and row["count"] > 0} if isinstance(armor, dict) else set()
        origin = status["pos"]
        blocks = [row for row in (obs.get("blocks") or []) if isinstance(row, dict) and _pos(row.get("pos"))]
        entities = [row for row in (obs.get("entities") or []) if isinstance(row, dict) and _pos(row.get("pos"))]

        def distance(row):
            return math.dist(origin, row["pos"])

        def nearby(names, visible=False):
            return sorted((row for row in blocks if row.get("name") in names and distance(row) <= 8
                           and (not visible or row.get("can_see") is True)
                           and all(type(n) is int for n in row["pos"])), key=distance)

        def evidence(name):
            rows = obs.get("progression_evidence")
            row = rows.get(name) if isinstance(rows, dict) else None
            return (row if isinstance(row, dict) and row.get("fresh") is True and row.get("verified") is True
                    and _dimension(row.get("dimension")) == dim
                    and row.get("source") in {"api_verified", "skill_verified", "server_advancement"} else {})

        def amount(item):
            return sum(counts.get(name, 0) for name in PLANKS) if item == "planks" else counts.get(item, 0)

        def has_pick(minimum=0):
            return any(counts.get(item, 0) for item in PICKS[minimum:])

        def station(name):
            return bool(nearby({name}))

        def gather(names, tool="gather_block"):
            for row in nearby(names, visible=True):
                if (math.floor(origin[0]), math.floor(origin[2])) == (row["pos"][0], row["pos"][2]):
                    continue
                return action(tool, {"pos": list(row["pos"]), "expected_name": row["name"]}, "Observed resource; skill must recheck reach and hazards.")
            return []

        def craft(item):
            recipe = RECIPES.get(item)
            if (recipe and all(amount(k) >= n for k, n in recipe.items())
                    and (item in {"crafting_table", "stick", "flint_and_steel"} or station("crafting_table"))):
                return action("craft_item", {"item": item, "count": 4 if item == "stick" else 1}, "Current ingredients observed; verify inventory gain.")
            return []

        def wood():
            for item in LOGS:
                if counts.get(item, 0):
                    return action("craft_item", {"item": item.removesuffix("_log") + "_planks", "count": 4}, "Convert one owned log to planks.")
            return gather(set(LOGS))

        def place_owned(item):
            for row in (obs.get("placement_candidates") or []):
                pos = row.get("pos") if isinstance(row, dict) else None
                if _pos(pos) and all(type(n) is int for n in pos):
                    return action("place_item", {"item": item, "pos": list(pos)}, "Revalidate this placement candidate before using the station.")
            return []

        def prerequisites(item):
            if item in {"furnace", "crafting_table"} and counts.get(item, 0) and not station(item):
                return place_owned(item)
            direct = craft(item)
            if direct:
                return direct
            if item not in {"crafting_table", "stick", "flint_and_steel"} and not station("crafting_table"):
                if counts.get("crafting_table", 0):
                    return place_owned("crafting_table")
                return craft("crafting_table") or wood()
            recipe = RECIPES.get(item, {})
            if amount("planks") < recipe.get("planks", 0):
                return wood()
            if amount("stick") < recipe.get("stick", 0):
                return craft("stick") or wood()
            if amount("cobblestone") < recipe.get("cobblestone", 0):
                return gather({"stone", "cobblestone"}) if has_pick() else prerequisites("wooden_pickaxe")
            return []

        def smelt(input_names, output_names):
            for row in (obs.get("furnaces") or []):
                if (not isinstance(row, dict) or row.get("fresh") is not True or row.get("verified") is not True
                        or not _pos(row.get("pos")) or any(type(n) is not int for n in row["pos"])):
                    continue
                if _dimension(row.get("dimension")) != dim:
                    continue
                output = row.get("output")
                if isinstance(output, dict) and output.get("name") in output_names and type(output.get("count")) is int and output["count"] > 0:
                    return action("smelt_collect", {"pos": list(row["pos"]), "item": output["name"], "count": min(16, output["count"])}, "Only current furnace output is collectible.")
                info = row.get("info") if isinstance(row.get("info"), dict) else {}
                burning = row.get("burning", info.get("burning"))
                if row.get("input") or row.get("fuel") or row.get("output") or burning is True:
                    return action("inspect_container", {"pos": list(row["pos"])}, "Existing furnace job; inspect instead of replaying transfers.")
                if burning is not False:
                    continue
                source = next((name for name in input_names if counts.get(name, 0)), None)
                for fuel in ("coal", "charcoal", *PLANKS, *LOGS, "stick"):
                    smelts = 8 if fuel in {"coal", "charcoal"} else .5 if fuel == "stick" else 1.5
                    capacity = math.floor(counts.get(fuel, 0) * smelts)
                    if source and capacity:
                        quantity = min(8, counts[source], capacity)
                        return action("smelt_start", {"pos": list(row["pos"]), "input_item": source, "count": quantity,
                                      "fuel_item": fuel, "fuel_count": math.ceil(quantity / smelts)}, "Load an empty verified furnace; starting is not finished output.")
            rows = nearby({"furnace"}) if any(counts.get(name, 0) for name in input_names) else []
            return (action("inspect_container", {"pos": list(rows[0]["pos"])}, "Obtain actual furnace slots first.") if rows else [])

        def iron():
            return (smelt(("raw_iron", "iron_ore", "deepslate_iron_ore"), {"iron_ingot"})
                    or (gather({"iron_ore", "deepslate_iron_ore"}, "gather_resource") if has_pick(1)
                        else prerequisites("stone_pickaxe")))

        food = sum(counts.get(name, 0) for name in FOODS)
        health, hunger = status.get("health"), status.get("food")
        if type(health) not in (int, float) or type(hunger) not in (int, float) or not math.isfinite(health) or not math.isfinite(hunger):
            return finish("safety", ["known_health_and_hunger"])
        hostile = next((row for row in sorted(entities, key=distance) if row.get("type") == "hostile"
                        or row.get("name") in {"skeleton", "zombie", "creeper", "spider", "witch", "piglin_brute", "blaze", "wither_skeleton"}), None)
        nearest = status.get("nearest_hostile") if isinstance(status.get("nearest_hostile"), dict) else {}
        if ((hostile and distance(hostile) <= 8)
                or type(nearest.get("dist")) in (int, float) and nearest["dist"] <= 8):
            return finish("safety", ["local_combat_or_retreat_gate"], risks=[
                "Shield possession is not active frontal blocking.",
                "No-shield creepers: retreat/cover; do not assume a timed melee or lava kill."],
                unsupported=[] if "fight_hostile" in self.available_tools else ["bounded_hostile_combat_adapter"], sources=["shield"])
        if health <= 8 or hunger <= 6:
            edible = next((name for name in FOODS if counts.get(name, 0)), None)
            return finish("safety", ["recover_health_and_food"],
                          action("eat_food", {"item": edible}, "Eat owned food before production.") if edible and hunger < 20 else [],
                          risks=["Low health without food is not permission for blind hunting."])
        if dim == "overworld" and status.get("is_day") is not True:
            beds = nearby({row.get("name") for row in blocks if isinstance(row.get("name"), str) and row["name"].endswith("_bed")})
            return finish("safety", ["safe_night_or_storm_handling"], action("auto_sleep", {}, "Skill verifies a usable bed and actual sleep.") if beds else [],
                          risks=["Night is not permission for blind gathering or fighting."])
        if ctx.get("recovering") is True:
            for row in sorted(entities, key=distance):
                item = row.get("item")
                name = item.get("name") if isinstance(item, dict) else None
                match = re.fullmatch(r"([a-z0-9_]+)x([1-9][0-9]{0,5})", item) if isinstance(item, str) else None
                name = match.group(1) if match else name
                if name and re.fullmatch(r"[a-z0-9_]{1,64}", name) and type(row.get("id")) is int and row["id"] >= 0 and distance(row) <= 8:
                    return finish("recovery", ["verify_recovered_inventory"], action("collect_drops", {"entity_id": row["id"], "expected_item": name}, "Only current visible drops, never a remembered death location."))
            return finish("recovery", ["safe_observed_drops"], risks=["Missing drops are unknown, not guaranteed recoverable; replan from actual inventory."])
        if dim in {"the_end", "end"}:
            if evidence("dragon_defeated"):
                return finish("post_dragon", ["verified_exit_adapter"], evidence=["fresh_server_dragon_defeat"],
                              unsupported=["end_exit_navigation"], sources=["dragon"])
            return finish("dragon", ["fresh_crystal_and_dragon_state"], unsupported=["ranged_combat", "dragon_phase_combat", "void_safe_navigation"],
                          risks=["Bed explosions are a separate unimplemented speedrun strategy."], sources=["dragon"])
        eyes = counts.get("ender_eye", 0)
        powder = counts.get("blaze_powder", 0) + 2 * counts.get("blaze_rod", 0) + eyes
        pearls = counts.get("ender_pearl", 0) + eyes
        if dim in {"the_nether", "nether"}:
            missing = [name for name, ok in (("blaze_powder_equivalent:14", powder >= 14), ("ender_pearl_equivalent:14", pearls >= 14),
                                            ("verified_return_portal", bool(evidence("return_portal")))) if not ok]
            return finish("nether_resources", missing or ["safe_return_adapter"],
                          unsupported=["fortress_navigation_and_blaze_combat", "piglin_bartering", "nether_return_navigation"],
                          risks=["Gold does not pacify brutes or protect theft.", "Water and beds are not Overworld-safe in the Nether."],
                          sources=["nether116", "java1162", "blaze"])
        if self.goal == "speedrun" and (eyes >= 12 or powder >= 14 and pearls >= 14):
            if eyes < 14 and counts.get("blaze_powder", 0) and counts.get("ender_pearl", 0):
                return finish("stronghold", ["eyes_with_spares"], action("craft_item", {"item": "ender_eye", "count": min(14 - eyes, counts["blaze_powder"], counts["ender_pearl"])}, "Reserve portal eyes and navigation spares."), sources=["stronghold"])
            if eyes < 14 and counts.get("blaze_rod", 0):
                return finish("stronghold", ["blaze_powder"], action("craft_item", {"item": "blaze_powder", "count": 2}, "One rod yields two powder; retain spare eyes."), sources=["stronghold"])
            portal = evidence("end_portal")
            if portal:
                empty = portal.get("empty_frames")
                if type(empty) is not int or not 0 <= empty <= 12:
                    return finish("end_preparation", ["checked_empty_frames"], sources=["stronghold"])
                missing = []
                for key, ok in (("eyes_for_empty_frames", eyes >= empty), ("food:12", food >= 12),
                                ("water_bucket", counts.get("water_bucket", 0)), ("bow_and_arrows", counts.get("bow", 0) and counts.get("arrow", 0) >= 32),
                                ("verified_equipped_armor", bool(evidence("armor_ready")))):
                    if not ok:
                        missing.append(key)
                return finish("end_preparation", missing or ["safe_end_entry_adapter"], unsupported=["activate_end_frames", "safe_end_entry", "dragon_combat"], sources=["stronghold", "dragon"])
            return finish("stronghold", ["fresh_eye_samples", "verified_portal_room"],
                          unsupported=["bound_eye_capture_and_calculation", "stronghold_navigation"],
                          risks=["Calculated coordinates are candidates; eye throws may break."], sources=["stronghold"])
        if food < 4:
            candidates = smelt(tuple(RAW_FOOD), set(RAW_FOOD.values()))
            if not candidates and any(counts.get(name, 0) for name in RAW_FOOD) and not station("furnace"):
                candidates = prerequisites("furnace")
            if not candidates:
                for row in nearby({"wheat", "carrots", "potatoes", "beetroots"}, visible=True):
                    age = (row.get("properties") or {}).get("age")
                    if str(age) == ("3" if row["name"] == "beetroots" else "7"):
                        candidates = action("harvest_crop", {"pos": list(row["pos"]), "expected_name": row["name"]}, "Harvest only mature observed crops; reserve planting stock.")
                        break
            if not candidates:
                animal = next((row for row in sorted(entities, key=distance) if row.get("name") in {"cow", "pig", "sheep", "chicken"}
                               and type(row.get("id")) is int and row["id"] >= 0 and distance(row) <= 2.8), None)
                if animal:
                    candidates = action("hunt_animal", {"entity_id": animal["id"], "expected_name": animal["name"]}, "API cannot verify age or ownership. Recheck weapon and terrain; only inventory gains count.")
            if not candidates and not has_pick():
                candidates = prerequisites("wooden_pickaxe")
            return finish("opening", ["food_reserve:4"], candidates,
                          risks=["Do not spend the last breeding stock without a recovery plan."], sources=["wheat", "cow"])
        if not has_pick(1):
            return finish("opening", ["stone_or_better_pickaxe"], prerequisites("stone_pickaxe"))
        if not station("furnace") and not counts.get("furnace", 0):
            return finish("opening", ["furnace"], prerequisites("furnace"))
        for item, ready in (("shield", shield_owned), ("bucket", any(counts.get(n, 0) for n in ("bucket", "water_bucket", "lava_bucket"))),
                            ("iron_pickaxe", has_pick(2))):
            if not ready:
                gaps = [item, *(f"{name}:{needed - amount(name)}" for name, needed in RECIPES[item].items() if amount(name) < needed)]
                if not station("crafting_table"):
                    gaps.append("placed_crafting_table")
                return finish("iron_defense", gaps, prerequisites(item) or iron(), sources=["shield", "bucket"])
        if self.goal == "survival":
            daily = obs.get("daily_evidence") if isinstance(obs.get("daily_evidence"), dict) else {}
            confirmed = []
            for key in ("shelter", "bed", "storage", "food_production"):
                row = daily.get(key)
                if (isinstance(row, dict) and row.get("fresh") is True and row.get("verified") is True
                        and _dimension(row.get("dimension")) == dim
                        and (key != "shelter" or row.get("shelter_verified") is True)
                        and (key != "food_production" or row.get("renewable") is True)):
                    confirmed.append(key)
            shelter = obs.get("shelter") if isinstance(obs.get("shelter"), dict) else {}
            if ("bed" not in confirmed and shelter.get("fresh") is True and _dimension(shelter.get("dimension")) == dim
                    and any(isinstance(row, dict) and row.get("verified") is True for row in (shelter.get("beds") or []))):
                confirmed.append("bed")
            if "storage" not in confirmed and any(isinstance(row, dict) and row.get("fresh") is True and row.get("verified") is True
                    and _dimension(row.get("dimension")) == dim for row in (obs.get("containers") or [])):
                confirmed.append("storage")
            missing = [key for key in ("shelter", "bed", "storage", "food_production") if key not in confirmed]
            if not missing:
                return finish("maintenance", evidence=confirmed, state="ready", risks=["Current facilities do not prove permanent food yield or spawn protection."], sources=["wheat"])
            candidate = action("inspect_shelter", {"pos": list(shelter["pos"])}, "Recheck current house geometry.") if _pos(shelter.get("pos")) and all(type(n) is int for n in shelter["pos"]) else []
            return finish("home_farm", missing, candidate, unsupported=["animal_breeding_and_fencing"],
                          risks=["Daily task graph supplies incremental farm and shelter actions."], evidence=confirmed, sources=["wheat", "cow"])
        building = sum(counts.get(name, 0) for name in ("cobblestone", "stone", "dirt"))
        gold = any(counts.get("golden_" + part, 0) or "golden_" + part in equipped
                   for part in ("helmet", "chestplate", "leggings", "boots"))
        missing = [name for name, ok in (("food:12", food >= 12), ("solid_blocks:32", building >= 32),
                    ("water_bucket", counts.get("water_bucket", 0)), ("ignition", counts.get("flint_and_steel", 0)), ("gold_armor", gold)) if not ok]
        if missing:
            candidates = []
            if not counts.get("water_bucket", 0) and counts.get("bucket", 0):
                water = next((row for row in nearby({"water"}, visible=True) if distance(row) <= 2.9
                              and isinstance(row.get("properties"), dict) and row["properties"].get("level") in (0, "0")), None)
                if water:
                    candidates = action("collect_liquid", {"pos": list(water["pos"]), "expected_name": "water"}, "Collect a checked source, preserving empty-bucket verification.")
            if not candidates and not counts.get("flint_and_steel", 0):
                candidates = craft("flint_and_steel")
            return finish("nether_preparation", missing, candidates, risks=["These are conservative bot supplies, not universal speedrun minima."], sources=["nether116", "bucket"])
        return finish("nether_portal", ["bound_safe_portal_and_travel_adapter"], unsupported=["automatic_lava_portal_site_and_casting", "nether_entry_and_return_navigation"],
                      risks=["Independent portal scripts require coordinates and do not imply a registered model skill."], sources=["obsidian", "nether116"])
