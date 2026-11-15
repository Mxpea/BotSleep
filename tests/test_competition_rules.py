"""Offline regression tests against the complete RULES.md v2 table snapshot."""

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from core.competition_rules import load_rules, scoring_catalog


# Independent transcription of all table IDs, grouped in original table order.
TABLE_IDS = {
    "story": """cure_zombie_villager enter_the_end follow_ender_eye shiny_gear
        enchant_item enter_the_nether deflect_arrow form_obsidian lava_bucket
        mine_diamond iron_tools mine_stone obtain_armor smelt_iron upgrade_tools""",
    "nether": """all_effects all_potions create_full_beacon netherite_armor summon_wither
        uneasy_alliance create_beacon fast_travel get_wither_skull obtain_ancient_debris
        ride_strider_in_overworld_lava charge_respawn_anchor explore_nether find_bastion
        find_fortress loot_bastion obtain_blaze_rod return_to_sender ride_strider
        brew_potion obtain_crying_obsidian distract_piglin""",
    "end": """elytra find_end_city kill_dragon levitate respawn_dragon dragon_breath
        dragon_egg enter_end_gateway""",
    "adventure": """adventuring_time arbalistic kill_all_mobs
        trim_with_all_exclusive_armor_patterns very_very_frightening blowback
        fall_from_world_height hero_of_the_village lightning_rod_with_villager_no_fire
        overoverkill revaulting spear_many_mobs spyglass_at_dragon throw_trident
        totem_of_undying trade_at_world_height two_birds_one_arrow
        craft_decorated_pot_using_only_sherds heart_transplanter kill_mob_near_sculk_catalyst
        play_jukebox_in_meadows sniper_duel summon_iron_golem trim_with_any_armor_pattern
        under_lock_and_key voluntary_exile who_needs_rockets avoid_vibration brush_armadillo
        bullseye crafters_crafting_crafters honey_block_slide lighten_up
        minecraft_trials_edition ol_betsy read_power_of_chiseled_bookshelf salvage_sherd
        spyglass_at_ghast spyglass_at_parrot trade use_lodestone
        walk_on_powder_snow_with_leather_boots whos_the_pillager_now kill_a_mob
        shoot_arrow sleep_in_bed""",
    "husbandry": """balanced_diet bred_all_animals complete_catalogue whole_pack feed_snifflet
        froglights leash_all_frog_variants obtain_netherite_hoe allay_deliver_cake_to_note_block
        allay_deliver_item_to_player axolotl_in_a_bucket kill_axolotl_target make_a_sign_glow
        obtain_sniffer_egg plant_any_sniffer_seed remove_wolf_armor repair_wolf_armor
        ride_a_boat_with_a_goat silk_touch_nest tadpole_in_a_bucket fishy_business
        place_dried_ghast_in_water safely_harvest_honey tactical_fishing tame_an_animal
        wax_on breed_an_animal plant_seed wax_off""",
}
SOURCE_SHA256 = "e896244ae7e3f15a816d1aedeab30478035e32eee6758f1c97cca2da43cca0b9"
# SHA256 of UTF-8 compact JSON rows [id, title, difficulty, source_points, condition].
TABLE_SHA256 = "a348b0aabaf26665fe31fb8849798f59cece941fa37724b5f548aeec142771a3"


class CompetitionRulesTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_rules()

    def load_document(self, document):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            return load_rules(path)

    def test_every_table_row_and_root_present_in_source_order(self):
        entries = self.catalog["entries"]
        expected = [f"minecraft:{category}/{name}" for category, names in TABLE_IDS.items() for name in names.split()]
        self.assertEqual(len(expected), 120)
        self.assertEqual([entry["id"] for entry in entries if not entry["is_root"]], expected)
        self.assertEqual(len(entries), 125)
        self.assertEqual(
            {entry["id"] for entry in entries if entry["is_root"]},
            {f"minecraft:{category}/root" for category in TABLE_IDS},
        )

    def test_original_titles_levels_points_and_conditions_preserved_for_all_rows(self):
        rows = [[entry[key] for key in ("id", "title", "difficulty", "source_points", "condition")]
                for entry in self.catalog["entries"] if not entry["is_root"]]
        digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(digest, TABLE_SHA256)
        for entry in self.catalog["entries"]:
            with self.subTest(id=entry["id"]):
                if entry["classification"] == "excluded":
                    self.assertEqual(entry["points"], 0)
                else:
                    self.assertEqual(entry["points"], {1: 5, 2: 15, 3: 30, 4: 60, 5: 100}[entry["difficulty"]])

    def test_provenance_has_draft_version_hash_and_honest_dates(self):
        self.assertEqual(self.catalog["version"], "v2")
        self.assertEqual(self.catalog["status"], "draft")
        self.assertEqual(self.catalog["date"], "2026-11-15")
        self.assertEqual(self.catalog["date_kind"], "catalog_snapshot")
        source = self.catalog["source"]
        self.assertEqual(source["sha256"], SOURCE_SHA256)
        self.assertEqual(source["document"], "mc-agent-api/docs/RULES.md")
        self.assertIsNone(source["date"])
        self.assertIn("未标注发布日期", source["date_note"])

    def test_defaults_require_explicit_confirmation_of_all_conditions(self):
        counts = {"scored": 0, "pending": 0, "excluded": 0}
        for row in scoring_catalog():
            counts[row["eligibility"]] += 1
            self.assertEqual(row["eligibility"], {
                "unconditional": "scored", "conditional": "pending", "excluded": "excluded",
            }[row["classification"]])
            self.assertTrue({"id", "title", "difficulty", "points", "condition", "eligibility"} <= row.keys())
        self.assertEqual(counts, {"scored": 45, "pending": 54, "excluded": 26})

    def test_every_conditional_rule_accepts_explicit_on_and_off_without_persisting(self):
        for entry in self.catalog["entries"]:
            if entry["classification"] != "conditional":
                continue
            ident = entry["id"]
            for value, eligibility in ((True, "scored"), (False, "excluded")):
                with self.subTest(id=ident, enabled=value):
                    row = next(row for row in scoring_catalog({ident: value}) if row["id"] == ident)
                    self.assertEqual(row["eligibility"], eligibility)
                    self.assertEqual(row["points"], entry["points"])
                    self.assertEqual(row["condition"], entry["condition"])
        self.assertEqual(sum(row["eligibility"] == "pending" for row in scoring_catalog()), 54)

    def test_every_excluded_rule_and_root_cannot_be_enabled(self):
        for entry in self.catalog["entries"]:
            if entry["classification"] != "excluded":
                continue
            with self.subTest(id=entry["id"]):
                with self.assertRaisesRegex(ValueError, "Cannot enable excluded"):
                    scoring_catalog({entry["id"]: True})
                row = next(row for row in scoring_catalog({entry["id"]: False}) if row["id"] == entry["id"])
                self.assertEqual(row["eligibility"], "excluded")
                self.assertEqual(row["points"], 0)
        self.assertFalse(any(row["is_root"] for row in scoring_catalog() if row["eligibility"] == "scored"))

    def test_unconditional_can_be_disabled_and_reenabled(self):
        ident = "minecraft:adventure/sleep_in_bed"
        for enabled, expected in ((False, "excluded"), (True, "scored")):
            row = next(row for row in scoring_catalog({ident: enabled}) if row["id"] == ident)
            self.assertEqual(row["eligibility"], expected)

    def test_invalid_overrides_raise_value_error(self):
        for overrides in ([], True, "enabled", {"minecraft:story/unknown": True}, {7: True},
                          {"minecraft:story/follow_ender_eye": 1},
                          {"minecraft:story/follow_ender_eye": "true"},
                          {"minecraft:story/follow_ender_eye": None},
                          {"observed_structures": {"nether_fortress": True}}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                scoring_catalog(overrides)

    def test_follow_ender_eye_retains_nether_fortress_condition(self):
        row = next(row for row in scoring_catalog() if row["id"] == "minecraft:story/follow_ender_eye")
        self.assertEqual(row["condition"], "岛上有下界要塞时计分")
        self.assertEqual(row["eligibility"], "pending")

    def test_results_are_fresh_serializable_and_do_not_modify_overrides(self):
        overrides = {"minecraft:story/follow_ender_eye": True}
        snapshot = copy.deepcopy(overrides)
        rows = scoring_catalog(overrides)
        self.assertEqual(json.loads(json.dumps(rows, ensure_ascii=False, allow_nan=False)), rows)
        self.assertEqual(json.loads(json.dumps(self.catalog, ensure_ascii=False, allow_nan=False)), self.catalog)
        rows[0]["condition"] = "changed"
        self.catalog["entries"][0]["title"] = "changed"
        self.assertNotEqual(scoring_catalog()[0]["condition"], "changed")
        self.assertNotEqual(load_rules()["entries"][0]["title"], "changed")
        self.assertEqual(overrides, snapshot)

    def test_custom_path_reads_without_rewriting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            payload = json.dumps(self.catalog, ensure_ascii=False).encode("utf-8")
            path.write_bytes(payload)
            self.assertEqual(load_rules(path), self.catalog)
            self.assertEqual(load_rules(str(path)), self.catalog)
            self.assertEqual(path.read_bytes(), payload)

    def test_illegal_points_levels_and_duplicate_ids_rejected(self):
        for key, invalid in (("points", 59), ("points", "60"), ("points", True),
                             ("points", 60.0), ("points", 0), ("difficulty", 0),
                             ("difficulty", 6), ("difficulty", True), ("difficulty", 4.0),
                             ("source_points", "100")):
            document = copy.deepcopy(self.catalog)
            document["entries"][0][key] = invalid
            with self.subTest(key=key, invalid=invalid), self.assertRaises(ValueError):
                self.load_document(document)
        duplicate = copy.deepcopy(self.catalog)
        duplicate["entries"].append(copy.deepcopy(duplicate["entries"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate advancement ID"):
            self.load_document(duplicate)

    def test_exclusion_and_root_cannot_be_reclassified_as_score_tasks(self):
        for entry in self.catalog["entries"]:
            if entry["classification"] != "excluded":
                continue
            document = copy.deepcopy(self.catalog)
            target = next(row for row in document["entries"] if row["id"] == entry["id"])
            target.update(classification="conditional", points=60, source_points="60")
            with self.subTest(id=entry["id"]), self.assertRaises(ValueError):
                self.load_document(document)

    def test_invalid_catalog_shapes_and_metadata_raise_value_error(self):
        documents = [None, [], {}, {**self.catalog, "entries": []}]
        for key, value in (("schema_version", True), ("status", "unknown"), ("date", "tomorrow"),
                           ("entries", [None]), ("difficulty_points", {"1": 6})):
            documents.append({**self.catalog, key: value})
        for key, value in (("document", "G:/private/RULES.md"), ("sha256", "bad"),
                           ("date", "tomorrow"), ("version", "v3")):
            document = copy.deepcopy(self.catalog)
            document["source"][key] = value
            documents.append(document)
        for key, value in (("id", "minecraft:recipes/example"), ("title", ""),
                           ("category", []), ("condition", "计分"),
                           ("classification", "unconditional"), ("is_root", True)):
            document = copy.deepcopy(self.catalog)
            document["entries"][0][key] = value
            documents.append(document)
        for index, document in enumerate(documents):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.load_document(document)

    def test_nonfinite_extra_metadata_is_rejected(self):
        document = copy.deepcopy(self.catalog)
        document["extra"] = float("inf")
        with self.assertRaises(ValueError):
            self.load_document(document)

    def test_bad_files_and_json_raise_value_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            with self.assertRaises(ValueError):
                load_rules(path)
            with self.assertRaises(ValueError):
                load_rules([])
            for payload in (b'{', b'\xff', b'{"entries": [], "entries": []}', b'{"number": NaN}', b'{"number": Infinity}', b'{"number": 1e999}'):
                path.write_bytes(payload)
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    load_rules(path)

    def test_default_catalog_path_is_independent_of_working_directory(self):
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                self.assertEqual(load_rules()["source"]["sha256"], SOURCE_SHA256)
            finally:
                os.chdir(original)


if __name__ == "__main__":
    unittest.main()
