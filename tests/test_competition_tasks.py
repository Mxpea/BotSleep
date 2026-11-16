"""Offline tests for competition lifecycle, evidence and thread-safe snapshots."""

import copy
import json
import threading
import unittest

from core.competition_rules import scoring_catalog
from core.competition_tasks import CompetitionTasks, SCHEMA_VERSION


CATALOG = scoring_catalog()
IDS = {row["id"] for row in CATALOG}
NOW = [1_700_000_000.0]
CAPABILITIES = {
    "gather_block", "gather_resource", "craft_item", "place_item", "smelt_start",
    "smelt_collect", "equip_armor", "auto_sleep", "plant_crop", "eat_food",
    "place_bed", "gather_seeds", "harvest_crop", "till_soil", "hunt_animal", "wait",
}


def obs(counts=None, blocks=None, **status):
    return {
        "fresh": True,
        "status": {"ready": True, "alive": True, "busy": False, "pos": [0.5, 64, 0.5],
                   "dimension": "overworld", "is_day": True, "thundering": False, **status},
        "counts": counts or {},
        "blocks": blocks or [],
        "entities": [],
    }


def api(overrides=None, *, total=None, partial=False):
    overrides = overrides or {}
    rows = [{"id": row["id"], "title": row["title"], "frame": "task",
             "status": overrides.get(row["id"], "not_started"), "completed_at": None,
             "progress": {"criteria_done": 0, "criteria_total": 1}}
            for row in CATALOG]
    counts = {key: sum(row["status"] == key for row in rows)
              for key in ("completed", "in_progress", "not_started")}
    result = {"ready": True, "updated_at": NOW[0] * 1000, "packets_received": 1,
              "summary": {"total": len(rows), **counts},
              "advancements": rows[:-1] if partial else rows}
    if partial:
        result["summary"]["total"] = len(rows)
    if total is not None:
        result["summary"]["total"] = total
    return {"ok": True, "result": result}


class CompetitionTasksTests(unittest.TestCase):
    def setUp(self):
        NOW[0] = 1_700_000_000.0
        self.manager = CompetitionTasks(catalog=CATALOG, clock=lambda: NOW[0])

    def update(self, observation=None, advancements="default", capabilities=CAPABILITIES):
        envelope = api() if advancements == "default" else advancements
        return self.manager.update(observation or obs(), envelope, capabilities=capabilities)

    def task(self, snapshot, ident):
        return next(row for row in snapshot["tasks"] if row["id"] == ident)

    def test_catalog_board_and_scoring_do_not_invent_normalized_score(self):
        snap = self.update()
        self.assertEqual(len(snap["tasks"]), len(CATALOG))
        self.assertEqual(snap["rules_version"], "v2-draft")
        self.assertTrue(snap["updated_at"].endswith("Z"))
        self.assertEqual(snap["availability"], "unknown")
        self.assertEqual(snap["assessment"], "unknown")
        self.assertEqual(self.task(snap, "minecraft:story/iron_tools")["assessment"], "not_started")
        self.assertIsNone(snap["score"]["normalized"])
        self.assertEqual(snap["score"]["raw"], 0)
        self.assertEqual(snap["score"]["completed_count"], 0)
        self.assertEqual(snap["score"]["difficulty_counts"], {str(i): 0 for i in range(1, 6)})
        self.assertTrue(all(len(row["next_steps"]) <= 5 for row in snap["tasks"]))
        non_roots = sum(not row.get("is_root") for row in CATALOG)
        self.assertEqual(snap["catalog_assessment"]["checked"], non_roots)
        self.assertEqual(snap["catalog_assessment"]["total"], non_roots)
        self.assertTrue(snap["catalog_assessment"]["complete"])
        self.assertIn("category", self.task(snap, "minecraft:story/iron_tools"))
        self.assertIn("source_points", self.task(snap, "minecraft:story/iron_tools"))
        json.dumps(snap)

    def test_saved_and_snapshot_are_detached_and_version_checked(self):
        self.update()
        snap = self.manager.snapshot()
        snap["tasks"].clear()
        state = self.manager.state()
        state["confirmed"].append("corruption")
        self.assertEqual(len(self.manager.snapshot()["tasks"]), len(CATALOG))
        with self.assertRaises(ValueError):
            CompetitionTasks(catalog=CATALOG, saved={"version": SCHEMA_VERSION + 1}, clock=lambda: NOW[0])
        malformed = self.manager.state()
        malformed["phase"] = "future"
        with self.assertRaises(ValueError):
            CompetitionTasks(saved=malformed, catalog=CATALOG, clock=lambda: NOW[0])

    def test_selection_requires_scored_selectable_row(self):
        snap = self.update(obs({}, [{"name": "white_bed", "pos": [1, 64, 1]}]))
        sleep = "minecraft:adventure/sleep_in_bed"
        pending = next(row["id"] for row in CATALOG if row["eligibility"] == "pending")
        excluded = next(row["id"] for row in CATALOG if row["eligibility"] == "excluded")
        self.assertEqual(self.task(snap, sleep)["availability"], "needs_prerequisites")
        self.assertTrue(self.manager.select(sleep)["success"])
        self.assertFalse(self.manager.select(pending)["success"])
        self.assertFalse(self.manager.select(excluded)["success"])
        self.assertEqual(self.manager.snapshot()["phase"], "active")

    def test_sleep_planner_has_real_stages_and_capability_gate(self):
        goal = "minecraft:adventure/sleep_in_bed"
        snap = self.update(obs({"oak_log": 1}, [{"name": "white_bed", "pos": [1, 64, 1]}]),
                           capabilities=CAPABILITIES)
        row = self.task(snap, goal)
        self.assertEqual(row["availability"], "needs_prerequisites")
        self.assertIn("night_or_thunderstorm", row["missing"])
        self.assertIn("auto_sleep", row["required_skills"])
        self.assertIn("auto_sleep", {s for step in row["next_steps"] for s in row["required_skills"]})
        night = obs({}, [{"name": "white_bed", "pos": [1, 64, 1]}], is_day=False)
        row = self.task(self.update(night), goal)
        self.assertEqual(row["availability"], "ready")
        no_sleep_tool = self.task(self.update(night, capabilities=CAPABILITIES - {"auto_sleep"}), goal)
        self.assertEqual(no_sleep_tool["availability"], "unsupported")

    def test_seed_planner_requires_seed_and_farmland_then_ready(self):
        goal = "minecraft:husbandry/plant_seed"
        world = obs({"wheat_seeds": 2}, [{"name": "farmland", "pos": [1, 64, 1]}])
        row = self.task(self.update(world), goal)
        self.assertEqual(row["availability"], "ready")
        self.assertIn("plant_crop", row["required_skills"])
        row = self.task(self.update(obs({}, [{"name": "short_grass", "pos": [2, 64, 1]}])), goal)
        self.assertEqual(row["availability"], "needs_prerequisites")
        self.assertIn("gather_seeds", row["required_skills"])
        self.assertIn("observed_farmland", row["missing"])

    def test_hostile_mob_goal_is_not_advertised_as_hunt_skill(self):
        row = self.task(self.update(), "minecraft:adventure/kill_a_mob")
        self.assertEqual(row["availability"], "unsupported")
        self.assertEqual(row["reason"], "hostile_combat_unavailable")

    def test_story_plans_cover_iron_goals_without_claiming_completion(self):
        world = obs({"iron_ingot": 4, "stick": 2}, [{"name": "crafting_table", "pos": [1, 64, 1]}])
        snap = self.update(world)
        for ident in ("minecraft:story/smelt_iron", "minecraft:story/iron_tools",
                      "minecraft:story/obtain_armor"):
            self.assertIn(self.task(snap, ident)["availability"], ("ready", "needs_prerequisites"))
        self.assertFalse(self.task(snap, "minecraft:story/smelt_iron")["confirmed"])
        unknown = "minecraft:story/iron_tools"
        self.assertEqual(self.task(snap, unknown)["completion"], "not_completed")

    def test_server_status_only_scores_and_missing_ids_stay_unknown(self):
        goal = "minecraft:adventure/sleep_in_bed"
        snap = self.update(advancements=api({goal: "completed"}))
        self.assertEqual(self.task(snap, goal)["completion"], "completed")
        self.assertEqual(snap["score"]["raw"], 5)
        sparse = {"ok": True, "result": {"ready": True, "updated_at": NOW[0] * 1000,
            "packets_received": 1, "summary": {"total": 1, "completed": 0, "in_progress": 0, "not_started": 1},
            "advancements": [{"id": "minecraft:visible/other", "title": "Other", "frame": "task",
                               "status": "not_started", "completed_at": None, "progress": {}}]}}
        snap = self.update(advancements=sparse)
        self.assertEqual(self.task(snap, goal)["completion"], "unknown")

    def test_failed_unready_and_incomplete_pages_are_stale(self):
        failed = self.update(advancements={"ok": False})
        self.assertTrue(failed["advancements"]["stale"])
        not_ready = self.update(advancements={"ok": True, "result": {"ready": False}})
        self.assertFalse(self.manager.finish("try")["success"])
        self.assertTrue(not_ready["advancements"]["stale"])
        partial = self.update(advancements=api(partial=True))
        self.assertTrue(partial["advancements"]["stale"])
        wrong_count = self.update(advancements=api(total=10))
        self.assertTrue(wrong_count["advancements"]["stale"])

    def test_missing_visible_goal_is_assessed_unknown_and_finish_does_not_score(self):
        goal = "minecraft:adventure/sleep_in_bed"
        omitted_id = "minecraft:story/iron_tools"
        world = obs({}, [{"name": "white_bed", "pos": [1, 64, 1]}], is_day=False)
        self.update(world, api())
        self.assertTrue(self.manager.select(goal)["success"])

        visible = api()
        visible["result"]["advancements"] = [row for row in visible["result"]["advancements"]
                                              if row["id"] != omitted_id]
        rows = visible["result"]["advancements"]
        visible["result"]["summary"] = {
            "total": len(rows),
            **{status: sum(row["status"] == status for row in rows)
               for status in ("completed", "in_progress", "not_started")},
        }
        self.update(world, visible)
        assessment = self.manager.snapshot()["catalog_assessment"]
        self.assertEqual(assessment["checked"], assessment["total"])
        self.assertEqual(assessment["known_count"], assessment["total"] - 1)
        self.assertTrue(assessment["complete"])
        self.assertIn(omitted_id, assessment["unknown_ids"])

        finish = self.manager.finish("visible advancement checked; omitted goal unknown")
        self.assertTrue(finish["success"])
        self.assertEqual(self.manager.snapshot()["phase"], "selection")
        self.assertNotIn(omitted_id, self.manager.state()["confirmed"])
        self.assertEqual(self.manager.snapshot()["score"]["raw"], 0)

    def test_end_competition_rejects_unready_and_stale_assessments(self):
        result = self.manager.end_competition("done")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "assessment_not_ready")

        self.update(advancements={"ok": True, "result": {"ready": False}})
        self.assertFalse(self.manager.end_competition("done")["success"])

        self.update(advancements=api())
        NOW[0] += 6
        stale = self.manager.end_competition("done")
        self.assertFalse(stale["success"])
        self.assertEqual(stale["error_code"], "assessment_not_ready")
        self.assertEqual(stale["data"]["catalog_assessment"]["checked"], 0)

    def test_end_competition_ends_incomplete_session_without_scoring_and_restores(self):
        omitted_id = "minecraft:story/iron_tools"
        visible = api()
        visible["result"]["advancements"] = [row for row in visible["result"]["advancements"]
                                              if row["id"] != omitted_id]
        rows = visible["result"]["advancements"]
        visible["result"]["summary"] = {
            "total": len(rows),
            **{status: sum(row["status"] == status for row in rows)
               for status in ("completed", "in_progress", "not_started")},
        }
        self.update(advancements=visible)  # Fresh response; omitted goal remains unknown, not completed.
        ended = self.manager.end_competition("model chose to stop")
        self.assertTrue(ended["success"])
        self.assertTrue(ended["data"]["completed_session"])
        self.assertEqual(ended["data"]["phase"], "completed")
        self.assertTrue(ended["data"]["catalog_assessment"]["complete"])
        self.assertIn(omitted_id, ended["data"]["catalog_assessment"]["unknown_ids"])
        self.assertEqual(ended["data"]["catalog_assessment"]["known_count"],
                         ended["data"]["catalog_assessment"]["total"] - 1)
        board = self.manager.snapshot()
        self.assertEqual(board["phase"], "completed")
        self.assertTrue(all(not row["selectable"] for row in board["tasks"]))
        self.assertEqual(board["score"]["raw"], 0)
        self.assertEqual(self.manager.state()["end_reason"], "model chose to stop")

        restored = CompetitionTasks(catalog=CATALOG, saved=self.manager.state(), clock=lambda: NOW[0])
        restored_board = restored.snapshot()
        self.assertEqual(restored_board["phase"], "completed")
        self.assertEqual(restored_board["end_reason"], "model chose to stop")
        self.assertTrue(all(not row["selectable"] for row in restored_board["tasks"]))
        self.assertEqual(restored.snapshot()["score"]["raw"], 0)

    def test_active_task_must_be_finished_before_ending_competition(self):
        goal = "minecraft:adventure/sleep_in_bed"
        self.update(obs({}, [{"name": "white_bed", "pos": [1, 64, 1]}], is_day=False), api())
        self.assertTrue(self.manager.select(goal)["success"])
        result = self.manager.end_competition("do not bypass active goal")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "task_active")
        self.assertEqual(self.manager.snapshot()["phase"], "active")
        self.assertEqual(self.manager.snapshot()["active_goal_id"], goal)

    def test_reused_advancement_envelope_expires_from_original_fetch_time(self):
        fetched_at = NOW[0]
        fresh = self.manager.update(obs(), api(), capabilities=CAPABILITIES,
                                    advancements_fetched_at=fetched_at)
        self.assertTrue(fresh["catalog_assessment"]["complete"])
        NOW[0] += 6
        stale = self.manager.update(obs(), api(), capabilities=CAPABILITIES,
                                    advancements_fetched_at=fetched_at)
        self.assertFalse(stale["catalog_assessment"]["complete"])
        self.assertTrue(stale["advancements"]["stale"])
        self.assertEqual(stale["advancements"]["error_code"], "advancements_stale")
        result = self.manager.end_competition("stale cached evidence")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "assessment_not_ready")

    def test_packet_event_timestamp_does_not_make_a_fresh_fetch_stale(self):
        goal = "minecraft:adventure/sleep_in_bed"
        world = obs({}, [{"name": "white_bed", "pos": [1, 64, 1]}], is_day=False)
        self.update(world, api())
        self.assertTrue(self.manager.select(goal)["success"])
        envelope = api({goal: "completed"})
        envelope["result"]["updated_at"] -= 60 * 60 * 1000
        self.update(world, envelope)
        board = self.manager.snapshot()
        self.assertTrue(board["catalog_assessment"]["complete"])
        self.assertEqual(board["advancements"]["updated_at"], envelope["result"]["updated_at"])
        self.assertTrue(self.manager.finish("all catalog rows detected")["success"])

    def test_local_assessment_age_makes_catalog_check_stale(self):
        goal = "minecraft:adventure/sleep_in_bed"
        world = obs({}, [{"name": "white_bed", "pos": [1, 64, 1]}], is_day=False)
        self.update(world)
        self.assertTrue(self.manager.select(goal)["success"])
        NOW[0] += 6
        assessment = self.manager.snapshot()["catalog_assessment"]
        self.assertFalse(assessment["complete"])
        self.assertEqual(assessment["checked"], 0)
        self.assertFalse(self.manager.finish("stale local assessment")["success"])

    def test_only_fresh_ready_assessment_finishes_and_no_completion_returns_selection(self):
        goal = "minecraft:adventure/sleep_in_bed"
        self.update(obs({}, [{"name": "white_bed", "pos": [1, 64, 1]}], is_day=False))
        self.assertTrue(self.manager.select(goal)["success"])
        self.assertFalse(self.manager.finish("done")["success"])
        self.update(obs({}, [{"name": "white_bed", "pos": [1, 64, 1]}], is_day=False), api())
        self.assertTrue(self.manager.finish("detected")["success"])
        self.assertEqual(self.manager.snapshot()["phase"], "selection")
        self.assertEqual(self.manager.state()["last_exit"]["outcome"], "finished_without_completion")
        self.assertEqual(self.manager.snapshot()["score"]["raw"], 0)

    def test_completed_finish_scores_and_stale_cache_blocks(self):
        goal = "minecraft:adventure/sleep_in_bed"
        world = obs({}, [{"name": "white_bed", "pos": [1, 64, 1]}], is_day=False)
        self.update(world)
        self.assertTrue(self.manager.select(goal)["success"])
        snap = self.update(world, api({goal: "completed"}))
        self.assertTrue(snap["assessment_complete"])
        NOW[0] += 6
        self.assertFalse(self.manager.finish("stale")["success"])
        self.update(world, api({goal: "completed"}))
        self.assertTrue(self.manager.finish("confirmed")["success"])
        self.assertEqual(self.manager.snapshot()["score"]["raw"], 5)

    def test_pending_and_excluded_confirmations_never_score(self):
        pending = next(row["id"] for row in CATALOG if row["eligibility"] == "pending")
        excluded = next(row["id"] for row in CATALOG if row["eligibility"] == "excluded")
        snap = self.update(advancements=api({pending: "completed", excluded: "completed"}))
        self.assertEqual(snap["score"]["raw"], 0)
        self.assertEqual(snap["score"]["conditional_pending"]["confirmed_count"], 1)
        self.assertEqual(snap["score"]["excluded"]["confirmed_count"], 1)

    def test_none_api_marks_stale_and_does_not_clear_prior_confirmations(self):
        goal = "minecraft:adventure/sleep_in_bed"
        completed = self.update(advancements=api({goal: "completed"}))
        self.assertEqual(completed["score"]["raw"], 5)
        stale = self.update(advancements=None)
        self.assertTrue(stale["advancements"]["stale"])
        self.assertEqual(stale["score"]["raw"], 5)
        self.assertFalse(self.manager.finish("stale")["success"])

    def test_force_exit_defers_and_checkpoint_keeps_budget(self):
        goal = "minecraft:adventure/sleep_in_bed"
        world = obs({}, [{"name": "white_bed", "pos": [1, 64, 1]}], is_day=False)
        self.update(world)
        self.assertTrue(self.manager.select(goal)["success"])
        self.manager.update_progress("short progress")
        exit_result = self.manager.force_exit("blocked")
        self.assertTrue(exit_result["success"])
        self.assertEqual(self.manager.snapshot()["phase"], "selection")
        self.assertFalse(self.manager.select(goal)["success"])
        checkpoint = self.manager.state()
        NOW[0] += 30
        resumed = CompetitionTasks(catalog=CATALOG, saved=checkpoint, clock=lambda: NOW[0])
        self.assertEqual(resumed.snapshot()["remaining_seconds"], 28800 - 30)
        self.assertEqual(resumed.state()["started_at"], checkpoint["started_at"])

    def test_budget_deadline_persists_across_restart(self):
        configured = CompetitionTasks(settings={"max_seconds": 10}, catalog=CATALOG, clock=lambda: NOW[0])
        saved = configured.state()
        NOW[0] += 11
        resumed = CompetitionTasks(settings={"max_seconds": 10}, saved=saved, catalog=CATALOG, clock=lambda: NOW[0])
        snap = resumed.update(obs(), api())
        self.assertEqual(snap["remaining_seconds"], 0)
        self.assertTrue(snap["budget_exhausted"])
        self.assertTrue(snap["started_at_utc"].endswith("Z"))

    def test_repeated_action_requests_replan_then_exits_only_after_explicit_attempt(self):
        goal = "minecraft:adventure/sleep_in_bed"
        world = obs({}, [{"name": "white_bed", "pos": [1, 64, 1]}], is_day=False)
        self.update(world)
        self.assertTrue(self.manager.select(goal)["success"])
        action = ("wait", {"seconds": 1}, {"success": True, "data": {"same": True}})
        for _ in range(3):
            response = self.manager.record_action(*action)
        self.assertTrue(response["data"]["replan_required"])
        for _ in range(3):
            response = self.manager.record_action(*action)
        self.assertFalse(response["data"]["forced_exit"])
        self.manager.record_action("replan", {}, {"success": True, "data": {"replan_attempted": True}})
        for _ in range(3):
            response = self.manager.record_action(*action)
        self.assertTrue(response["data"]["forced_exit"])
        self.assertEqual(self.manager.snapshot()["phase"], "selection")

    def test_action_outcome_unknown_is_recorded_not_replayed(self):
        goal = "minecraft:adventure/sleep_in_bed"
        self.update(obs({}, [{"name": "white_bed", "pos": [1, 64, 1]}], is_day=False))
        self.manager.select(goal)
        result = self.manager.record_action("place_bed", {"item": "white_bed"},
                                           {"success": False, "error_code": "outcome_unknown"})
        self.assertTrue(result["data"]["outcome_unknown"])
        self.assertTrue(self.manager.snapshot()["progress_card"]["recent_actions"][-1]["outcome_unknown"])

    def test_rlock_provides_consistent_concurrent_snapshots(self):
        self.update()
        failures = []

        def writer():
            try:
                for _ in range(30):
                    self.manager.update(obs(), api())
            except Exception as exc:  # pragma: no cover - failure payload
                failures.append(exc)

        def reader():
            try:
                for _ in range(50):
                    snap = self.manager.snapshot()
                    self.assertEqual(len(snap["tasks"]), len(CATALOG))
                    self.assertEqual(snap["phase"], self.manager.state()["phase"])
                    json.dumps(snap)
            except Exception as exc:  # pragma: no cover - failure payload
                failures.append(exc)

        threads = [threading.Thread(target=writer), threading.Thread(target=reader), threading.Thread(target=reader)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
