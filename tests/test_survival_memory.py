import concurrent.futures
import json
import math
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from core.survival_memory import SurvivalMemory


@dataclass
class Decision:
    name: str
    args: dict


@dataclass
class Result:
    success: bool
    data: dict
    error_code: str = None
    message: str = "must never enter factual memory"


class SurvivalMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.memory = self.make_memory()
        self.observation = {
            "fresh": True,
            "status": {"ready": True, "alive": True, "dimension": "overworld", "pos": [0, 64, 0]},
            "blocks": [], "counts": {},
        }

    def make_memory(self, **changes):
        settings = dict(endpoint="http://localhost:18920/api/v1", username="MemoryBot",
                        world_id="world-one", clock=lambda: self.now)
        settings.update(changes)
        memory = SurvivalMemory(self.temp.name, **settings)
        self.addCleanup(memory.close)
        return memory

    def recall(self, **changes):
        return self.memory.recall(self.observation, **changes)

    def place(self, name="red_bed", pos=None, **changes):
        self.memory.remember_place("bed", name, pos or [1, 64, 1], dimension="overworld", **changes)

    def test_scope_isolates_endpoint_account_and_world(self):
        self.place()
        for settings in ({"endpoint": "http://localhost:18921/api/v1"},
                         {"username": "OtherBot"}, {"world_id": "world-two"}):
            other = self.make_memory(**settings)
            self.assertNotEqual(other.path, self.memory.path)
            self.assertEqual(other.recall(self.observation)["places"], [])

    def test_scope_discards_url_credentials_query_and_fragment(self):
        private = self.make_memory(endpoint="http://user:private-password@localhost:18920/api/v1/?token=private-token#private-fragment")
        self.assertEqual(private.path, self.memory.path)
        private.record_episode("test", dimension="overworld", data={"name": "private-password"})
        raw = private.path.read_bytes()
        for secret in (b"private-password", b"private-token", b"private-fragment", b"localhost", b"MemoryBot"):
            self.assertNotIn(secret, raw)

    def test_restart_preserves_facts_and_returns_detached_data(self):
        self.place()
        retrieved = self.recall()
        retrieved["places"][0]["pos"][0] = 999
        other = self.make_memory()
        row = other.recall(self.observation)["places"][0]
        self.assertEqual(row["pos"], [1, 64, 1])
        self.assertTrue(row["requires_revalidation"])
        self.assertEqual(row["source"], "api_verified")
        self.assertEqual(row["observed_at"], self.now)

    def test_same_named_places_at_different_positions_are_not_overwritten(self):
        self.place(pos=[1, 64, 1])
        self.place(pos=[4, 64, 4])
        self.place(pos=[1.2, 64.1, 1.2], confidence=0.9)
        places = self.recall()["places"]
        self.assertEqual(len(places), 2)
        self.assertEqual({tuple(p["pos"]) for p in places}, {(1, 64, 1), (4, 64, 4)})

    def test_dimension_filter_never_uses_other_dimensions(self):
        self.place()
        self.memory.remember_place("bed", "red_bed", [1, 64, 1], dimension="the_nether")
        nether = {"status": {"dimension": "the_nether", "pos": [0, 64, 0]}}
        self.assertEqual(len(self.recall()["places"]), 1)
        self.assertEqual(self.memory.recall(nether)["places"][0]["dimension"], "the_nether")
        self.assertEqual(self.memory.recall({})["places"], [])

    def test_expiry_is_inclusive_and_refreshes_only_observed_place(self):
        self.place(ttl_seconds=10)
        self.now += 9
        self.assertEqual(len(self.recall()["places"]), 1)
        self.now += 1
        self.assertEqual(self.recall()["places"], [])
        self.place(ttl_seconds=20)
        self.assertEqual(self.recall()["places"][0]["expires_at"], self.now + 20)

    def test_partial_unknown_or_stale_observations_do_not_erase_places(self):
        self.place()
        for observation in (None, {}, {"status": None}, {"status": {"dimension": "overworld"}},
                            {**self.observation, "blocks": None},
                            {**self.observation, "fresh": False, "blocks": [{"name": "furnace", "pos": [1, 64, 1]}]}):
            self.memory.observe(observation)
        self.assertEqual(self.recall()["places"][0]["name"], "red_bed")

    def test_verified_block_replacement_removes_old_block_identity(self):
        self.place()
        self.memory.observe({**self.observation, "blocks": [{"name": "furnace", "pos": [1, 64, 1]}]})
        places = self.recall()["places"]
        self.assertEqual([(p["name"], p["category"]) for p in places], [("furnace", "workstation")])

    def test_observation_records_known_types_and_skips_malformed_rows(self):
        self.memory.observe({**self.observation, "blocks": [None, {}, {"name": "oak_log", "pos": [2, 64, 2]},
            {"name": "lava", "pos": [3, 64, 3]}, {"name": "dirt", "pos": [4, 64, 4]},
            {"name": "crafting_table", "pos": [1, 64, 1], "can_see": True},
            {"name": "red_bed", "pos": [math.nan, 64, 1]}]})
        self.assertEqual({p["category"] for p in self.recall()["places"]}, {"resource", "hazard", "workstation"})
        self.assertTrue(all(p["requires_revalidation"] for p in self.recall()["places"]))

    def test_death_invalidates_temporary_facts_but_preserves_historical_episode(self):
        self.memory.observe(self.observation)
        self.place()
        self.memory.remember_place("resource", "oak_log", [2, 64, 2], dimension="overworld")
        self.memory.remember_failure("gather", "stalled", dimension="overworld", target=[2, 64, 2])
        self.memory.record_episode("skill_result", dimension="overworld", data={"action": "craft", "success": True})
        dead = {**self.observation, "status": {**self.observation["status"], "alive": False,
            "last_death": {"t": 123, "pos": [0, 64, 0]}}}
        self.memory.observe(dead)
        recalled = self.recall()
        self.assertEqual(recalled["strategies"], [])
        self.assertEqual([p["category"] for p in recalled["places"]], ["bed"])
        self.assertEqual(recalled["places"][0]["confidence"], 0.425)
        self.assertEqual(recalled["places"][0]["invalidated_by"], "death")
        self.assertIn("skill_result", {p["event"] for p in recalled["episodes"]})
        self.assertNotIn("inventory", recalled)

    def test_death_marker_transition_is_counted_once_and_null_does_not_erase_it(self):
        self.memory.observe(self.observation)
        status = self.observation["status"]
        self.memory.observe({"status": {**status, "alive": False}})
        self.memory.observe({"status": {**status, "last_death": {"t": 123, "pos": [0, 64, 0]}}})
        self.memory.observe({"status": {**status, "last_death": None}})
        self.memory.observe({"status": {**status, "last_death": {"t": 123, "pos": [0, 64, 0]}}})
        self.assertEqual(sum(e["event"] == "death_observed" for e in self.recall()["episodes"]), 1)

    def test_first_login_historical_death_is_not_new_death(self):
        self.memory.observe({"status": {**self.observation["status"], "last_death": {"t": 123, "pos": [0, 64, 0]}}})
        self.assertEqual(self.recall()["episodes"], [])

    def test_dimension_change_clears_strategies_and_old_places_require_revalidation(self):
        self.memory.observe(self.observation)
        self.place()
        self.memory.remember_failure("gather", "stalled", dimension="overworld", target=[1, 64, 1])
        nether = {"status": {**self.observation["status"], "dimension": "the_nether"}}
        self.memory.observe(nether)
        self.assertEqual(self.memory.recall(nether)["places"], [])
        self.assertEqual(self.recall()["strategies"], [])
        self.assertEqual(self.recall()["places"][0]["invalidated_by"], "dimension_change")

    def test_failure_repetition_and_success_clears_only_same_target(self):
        a = Decision("gather", {"pos": [1, 64, 1]})
        b = Decision("gather", {"pos": [2, 64, 1]})
        failed = Result(False, {}, "stalled")
        for decision in (a, a, b):
            self.memory.record_result(decision, failed, self.observation)
        strategies = self.recall()["strategies"]
        self.assertEqual(sorted(p["count"] for p in strategies), [1, 2])
        self.assertEqual({p["strategy"] for p in strategies}, {"choose_other_target"})
        self.memory.record_result(a, Result(True, {"gained": 1, "block_removed": True}), self.observation)
        self.assertEqual(self.recall()["strategies"][0]["target"], [2, 64, 1])

    def test_uncertain_dig_remembers_verification_and_no_replay_command(self):
        self.memory.record_result(Decision("gather", {"pos": [1, 64, 1]}),
                                  Result(False, {"executed": True}, "dig_unconfirmed"), self.observation)
        strategy = self.recall()["strategies"][0]
        self.assertEqual(strategy["strategy"], "verify_before_retry")
        self.assertTrue(strategy["requires_revalidation"])
        self.assertNotIn("execute", strategy)

    def test_provider_failure_is_not_a_game_target_strategy(self):
        self.memory.record_result(Decision("observe", {}), Result(False, {"executed": False}, "llm_timeout"), self.observation)
        self.assertEqual(self.recall()["strategies"], [])
        self.assertFalse(self.recall()["episodes"][0]["data"]["executed"])

    def test_wait_polling_does_not_evict_meaningful_episode(self):
        self.memory.record_episode("gather_confirmed", dimension="overworld", data={"gained": 1})
        for _ in range(300):
            self.memory.record_result(Decision("wait", {}), Result(True, {}), self.observation)
        self.assertEqual([e["event"] for e in self.recall()["episodes"]], ["gather_confirmed"])

    def test_success_without_motion_does_not_erase_failed_route(self):
        decision = Decision("move_to", {"pos": [1, 64, 1]})
        self.memory.record_result(decision, Result(False, {}, "stalled"), self.observation)
        self.memory.record_result(decision, Result(True, {"moved": False, "already_in_range": True}), self.observation)
        strategies = self.recall()["strategies"]
        self.assertEqual({s["error_code"] for s in strategies}, {"stalled", "no_movement"})

    def test_partial_results_are_tolerated_and_do_not_fabricate_success(self):
        for decision, result in ((None, None), ({}, {}), ({"name": "gather"}, {"success": "true"})):
            self.memory.record_result(decision, result, None)
        self.memory.record_result(Decision("gather", {}), Result(False, {}, "unknown"), None)
        self.assertEqual(self.recall()["episodes"], [])  # Unknown dimension is not inferred.

    def test_only_structured_evidence_survives_not_chat_thought_or_credentials(self):
        self.memory.add_secret("private-secret")
        self.memory.record_result(Decision("craft", {"item": "crafting_table", "reason": "private chat narrative"}),
            Result(True, {"before": 0, "after": 1, "api_key": "private-secret", "reasoning": "secret reasoning",
                          "messages": ["chat text"], "before_counts": {"oak_planks": 4}, "password": "bad"}), self.observation)
        raw = self.memory.path.read_bytes()
        for phrase in (b"private-secret", b"private chat narrative", b"secret reasoning", b"chat text", b"must never enter factual memory"):
            self.assertNotIn(phrase, raw)
        data = self.recall()["episodes"][0]["data"]
        self.assertEqual((data["count_before"], data["count_after"]), (0, 1))
        self.assertNotIn("counts", self.recall())

    def test_redaction_handles_short_secrets_and_late_registration(self):
        self.memory.record_episode("test", dimension="overworld", data={"name": "privatevalue"})
        self.memory.add_secret("privatevalue")
        self.assertNotIn(b"privatevalue", self.memory.path.read_bytes())
        self.memory.add_secret("a")
        self.memory.record_episode("test", dimension="overworld", data={"action": "craft", "success": True})
        row = self.recall()["episodes"][0]
        self.assertIn("action", row["data"])
        self.assertIn("observed_at", row)

    def test_capacity_bounds_each_kind_and_removes_oldest(self):
        tiny = self.make_memory(world_id="tiny", max_episodes=2, max_places=2, max_strategies=2)
        for i in range(5):
            self.now += 1
            tiny.record_episode("e" + str(i), dimension="overworld")
            tiny.remember_place("bed", "red_bed", [i, 64, 0], dimension="overworld")
            tiny.remember_failure("gather", "stalled", dimension="overworld", target=[i, 64, 0])
        result = tiny.recall(self.observation, limit=64)
        self.assertEqual([len(result[k]) for k in tiny.KINDS], [2, 2, 2])
        self.assertEqual({e["event"] for e in result["episodes"]}, {"e3", "e4"})

    def test_total_retrieval_count_and_full_json_byte_budget(self):
        for i in range(10):
            self.place(pos=[i, 64, 0])
            self.memory.record_episode("event" + str(i), dimension="overworld")
            self.memory.remember_failure("gather", "stalled", dimension="overworld", target=[i, 64, 0])
        result = self.recall(limit=4)
        self.assertEqual(sum(map(len, result.values())), 4)
        for budget in (48, 100, 400, 800, 1500, 6000):
            result = self.recall(limit=64, max_bytes=budget)
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode("utf-8")), budget)
        with self.assertRaises(ValueError):
            self.recall(max_bytes=1)

    def test_resource_flood_keeps_verified_workstation_and_bounds_database(self):
        tiny = self.make_memory(world_id="flood", max_places=4)
        tiny.remember_place("workstation", "crafting_table", [20, 64, 10], dimension="overworld")
        for i in range(40):
            self.now += 1
            tiny.remember_place("resource", "oak_log", [i, 64, 0], dimension="overworld")
        recalled = tiny.recall(self.observation, limit=1)
        self.assertEqual(recalled["places"][0]["name"], "crafting_table")
        self.assertLessEqual(tiny.path.stat().st_size, tiny.max_database_bytes)

    def test_invalid_ttl_confidence_coordinates_and_budget_are_rejected(self):
        for ttl in (0, -1, True, math.inf, math.nan):
            with self.assertRaises(ValueError):
                self.place(ttl_seconds=ttl)
        for confidence in (-0.1, 1.1, True, math.nan):
            with self.assertRaises(ValueError):
                self.place(confidence=confidence)
        with self.assertRaises(ValueError):
            self.place(pos=[math.inf, 1, 1])
        with self.assertRaises(ValueError):
            self.recall(limit=True)

    def test_transaction_failure_does_not_partially_write(self):
        self.place()
        before = self.recall()
        with patch.object(self.memory, "_prune", side_effect=RuntimeError("simulated disk operation failure")):
            with self.assertRaises(RuntimeError):
                self.place(pos=[5, 64, 5])
        self.assertEqual(self.recall(), before)

    def test_multiple_instances_and_threads_do_not_lose_failure_counts(self):
        other = self.make_memory()
        def write(i):
            memory = self.memory if i % 2 else other
            memory.remember_failure("gather", "stalled", dimension="overworld", target=[1, 64, 1])
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(write, range(20)))
        self.assertEqual(self.recall()["strategies"][0]["count"], 20)

    def test_corrupt_and_future_schema_databases_are_preserved(self):
        self.memory.close()
        damaged = b"not a SQLite database; retain original"
        self.memory.path.write_bytes(damaged)
        with self.assertRaisesRegex(ValueError, "preserve"):
            self.make_memory()
        self.assertEqual(self.memory.path.read_bytes(), damaged)
        future = self.make_memory(world_id="future")
        with sqlite3.connect(future.path) as db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaisesRegex(ValueError, "incompatible"):
            self.make_memory(world_id="future")
        with sqlite3.connect(future.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_memory_operations_use_no_network_or_model(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            self.memory.observe(self.observation)
            self.memory.record_result(Decision("wait", {}), Result(True, {}), self.observation)
            self.recall()

    def test_close_rejects_later_writes(self):
        self.memory.close()
        with self.assertRaises(RuntimeError):
            self.place()


if __name__ == "__main__":
    unittest.main()
