"""Unit tests for the two-tier `core.loop_supervisor.LoopSupervisor`."""

import unittest

from agent.daily_survival import DailySurvivalGoals
from agent.survival_planner import Decision
from core.loop_supervisor import LoopSupervisor
from skills import SkillResult


def _obs(pos=None, *, is_day=False, armor=0):
    return {
        "status": {
            "ready": True,
            "alive": True,
            "busy": None,
            "pos": pos or [0.5, 64.0, 0.5],
            "dimension": "overworld",
            "gamemode": "survival",
            "health": 20.0,
            "food": 20,
            "is_day": is_day,
            "time_of_day": 2000 if is_day else 15000,
            "thundering": False,
            "armor_points": armor,
        },
        "counts": {},
        "blocks": [],
        "entities": [],
        "fresh": True,
    }


class LoopSupervisorTests(unittest.TestCase):
    def test_preemption_thrash_shield_activates_after_three_quick_cancels(self):
        events = []
        sup = LoopSupervisor(action_watchdog_seconds=30.0, record_fn=lambda l, e, d: events.append((e, d)))
        ctx = {}
        d = Decision("wait", {"seconds": 0.25}, "interrupted", "safety")
        res = SkillResult(False, "cancelled", {"executed": False}, "llm_cancelled")
        for t in (100.0, 102.0, 104.0):
            sup.record_step_outcome(t, 1000.0 + t, d, res, _obs(), ctx)
        self.assertTrue(sup.preemption_shield_active(105.0))
        self.assertFalse(sup.preemption_shield_active(116.0))
        self.assertTrue(any(e == "LOOP_PREEMPT_SHIELD_ACTIVE" for e, _ in events))

    def test_stationary_wait_livelock_alert_raises_and_clears_on_action(self):
        sup = LoopSupervisor(action_watchdog_seconds=30.0)
        ctx = {}
        w = Decision("wait", {"seconds": 2}, "Short safety observation", "observe")
        ok = SkillResult(True, "waiting", {"waiting": True})
        for step in range(3):
            sup.record_step_outcome(10.0 + step * 2, 1000.0 + step * 2, w, ok, _obs([1.2, 64.0, 1.2]), ctx)
        self.assertIn("loop_supervisor_alert", ctx)
        self.assertEqual(ctx["loop_supervisor_alert"]["type"], "stationary_wait_livelock")

        move = Decision("move_to", {"pos": [4.0, 64.0, 1.0], "range": 1}, "Move", "explore")
        sup.record_step_outcome(20.0, 1020.0, move, ok, _obs([4.2, 64.0, 1.2]), ctx)
        self.assertNotIn("loop_supervisor_alert", ctx)

    def test_signature_bad_args_cooldown_clears_on_inventory_change(self):
        sup = LoopSupervisor()
        ctx = {}
        bad = Decision("run_survival_task", {"task": "craft", "item": "sticks"}, "Craft sticks", "craft")
        fail = SkillResult(False, "craft requires supported item", {"executed": False}, "bad_args")
        sup.sync_inventory_counts({"oak_planks": 4})
        sup.record_step_outcome(1.0, 100.0, bad, fail, _obs(), ctx)
        self.assertIsNone(sup.is_signature_blocked(bad, 101.0))

        sup.record_step_outcome(2.0, 102.0, bad, fail, _obs(), ctx)
        self.assertIsNotNone(sup.is_signature_blocked(bad, 103.0))
        self.assertIn("run_survival_task:craft:sticks", ctx.get("blocked_task_signatures", {}))

        # New inventory items clear stale failure cooldowns.
        sup.sync_inventory_counts({"oak_planks": 4, "cobblestone": 3})
        self.assertIsNone(sup.is_signature_blocked(bad, 103.0))

    def test_night_death_hotspot_suspends_surface_recover_drops(self):
        night = _obs(is_day=False, armor=0)
        night["entities"] = [{"id": 5, "type": "item", "pos": [2, 64, 0], "item": {"name": "stone_pickaxe"}}]
        day = _obs(is_day=True, armor=0)
        day["entities"] = night["entities"]

        # Unarmored >=2 night deaths suspend surface drop rush until dawn.
        self.assertTrue(LoopSupervisor.should_suspend_drop_recovery(night, {"death_count": 2, "recent_spawn_deaths": 2}))
        self.assertFalse(LoopSupervisor.should_suspend_drop_recovery(day, {"death_count": 2, "recent_spawn_deaths": 2}))
        self.assertFalse(LoopSupervisor.should_suspend_drop_recovery(night, {"death_count": 1, "recent_spawn_deaths": 1}))

        goals = DailySurvivalGoals({})
        board = goals.build(night, None, {"death_count": 3, "recent_spawn_deaths": 3, "spawn_camp_alert": True})
        rec = next(row for row in board["tasks"] if row["id"] == "recover_drops")
        self.assertEqual(rec["priority"], 0)
        self.assertIsNone(rec["next_action"])
        self.assertIn("hotspot", rec["blocked_reason"].lower())


if __name__ == "__main__":
    unittest.main()
