"""Offline regressions for dual-API overflow routing, SafetyArbiter planning, tight retreat, and dead-loop fixes."""

import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from agent.brain import LLMError, OpenAIBrain
from agent.llm_planner import LLMSurvivalPlanner
from agent.survival_planner import Decision, SurvivalPlanner
from core.autonomous_tasks import AutonomousTaskController
from core.config import DEFAULTS, load_config
from core.daily_policy import DailySafetyPolicy
from core.loop_supervisor import LoopSupervisor
from core.model_gate import ModelGateError, ModelRequestGate
from core.retreat_controller import RetreatController
from core.safety_arbiter import SafetyArbiter
from skills import SkillResult
from skills.daily_survival import DailySurvivalSkills
from test_autonomous_tasks import world as auto_world


def _obs(pos=None, *, health=18, food=18, is_day=False, on_ground=True, in_water=False, blocks=None, entities=None, counts=None, nearest_hostile=None):
    return {
        "fresh": True,
        "stale": False,
        "inventory_fresh": True,
        "terrain_fresh": True,
        "observation_scope": "full",
        "status": {
            "ready": True,
            "alive": True,
            "busy": None,
            "gamemode": "survival",
            "dimension": "overworld",
            "pos": list(pos or [0.5, 64.0, 0.5]),
            "health": health,
            "food": food,
            "oxygen": 20,
            "on_ground": on_ground,
            "is_day": is_day,
            "time_of_day": 15000 if not is_day else 2000,
            "thundering": False,
            "is_sleeping": False,
            "in_vehicle": False,
            "in_water": in_water,
            "submerged": False,
            "in_lava": False,
            "on_fire": False,
            "last_death": None,
            "nearest_hostile": nearest_hostile,
        },
        "counts": dict(counts or {}),
        "inventory": [{"name": k, "count": v} for k, v in (counts or {}).items() if v > 0],
        "blocks": list(blocks or []),
        "entities": list(entities or []),
    }


class OverflowAndLoopFixesTests(unittest.TestCase):
    # ------------------------------------------------------------------
    # 1. Config & competition isolation + scope_id concurrent ModelGate
    # ------------------------------------------------------------------
    def test_overflow_config_defaults_env_overrides_and_competition_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "c.yaml"
            cfg_path.write_text(
                "\n".join([
                    "agent:",
                    "  mode: survival",
                    "survival:",
                    "  objective: daily",
                    "llm:",
                    "  enabled: true",
                    "  base_url: https://primary.example/v1",
                    "  model: qwen-primary",
                    "  api_key: secret-primary",
                ]),
                encoding="utf-8",
            )
            base = load_config(cfg_path, environ={})
            self.assertFalse(base["llm"]["overflow"]["enabled"])
            self.assertEqual(base["llm"]["overflow"]["base_url"], "https://doro.lat/v1")
            self.assertEqual(base["llm"]["overflow"]["model"], "deepseek-v4.1-flash")

            # Enabled by env for daily multi-bot runs.
            enabled_cfg = load_config(
                cfg_path,
                environ={
                    "BOTSLEEP_LLM_OVERFLOW_ENABLED": "true",
                    "BOTSLEEP_LLM_OVERFLOW_API_KEY": "secret-overflow",
                },
            )
            self.assertTrue(enabled_cfg["llm"]["overflow"]["enabled"])
            self.assertEqual(enabled_cfg["llm"]["overflow"]["api_key"], "secret-overflow")

            # Strictly forced off in competition objective unless explicitly permitted.
            comp_path = Path(tmp) / "comp.yaml"
            comp_path.write_text(
                "\n".join([
                    "agent:",
                    "  mode: survival",
                    "survival:",
                    "  objective: competition",
                    "llm:",
                    "  enabled: true",
                    "  base_url: https://primary.example/v1",
                    "  model: qwen-primary",
                    "  api_key: secret-primary",
                    "  overflow:",
                    "    enabled: true",
                    "    api_key: secret-overflow",
                ]),
                encoding="utf-8",
            )
            comp_cfg = load_config(comp_path, environ={})
            self.assertFalse(comp_cfg["llm"]["overflow"]["enabled"])

    def test_model_gate_scope_isolation_allows_parallel_overflow_and_busy_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate_1 = ModelRequestGate("https://doro.lat/v1", "k", directory=tmp, global_concurrency=False, scope_id="bot_1")
            gate_2 = ModelRequestGate("https://doro.lat/v1", "k", directory=tmp, global_concurrency=False, scope_id="bot_2")
            entered = threading.Event()
            release = threading.Event()
            second_ok = threading.Event()

            def holder():
                with gate_1.request():
                    entered.set()
                    release.wait(2.0)

            t = threading.Thread(target=holder, daemon=True)
            t.start()
            self.assertTrue(entered.wait(1.0))
            with gate_2.request(wait_seconds=0.5):
                second_ok.set()
            self.assertTrue(second_ok.is_set(), "Distinct scope_id overflow gates must not block each other")

            # Same-scope request times out quickly with wait_seconds override.
            gate_1_dup = ModelRequestGate("https://doro.lat/v1", "k", directory=tmp, global_concurrency=False, scope_id="bot_1")
            with self.assertRaises(ModelGateError) as busy_err:
                with gate_1_dup.request(wait_seconds=0.1):
                    pass
            self.assertEqual(busy_err.exception.code, "llm_busy")
            release.set()
            t.join(timeout=2.0)

    def test_openai_brain_overflow_on_busy_error_and_transport_cooldown_and_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            holder_gate = ModelRequestGate("https://primary.example/v1", "sec-p", directory=tmp, wait_seconds=5)
            overflow = OpenAIBrain(
                base_url="https://doro.lat/v1",
                api_key="sec-o-super-secret",
                model="deepseek-v4.1-flash",
                request_gate=None,
            )
            overflow._chat_completion = Mock(return_value={
                "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
                "choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "wait", "arguments": "{\"seconds\": 1}"}}]}}],
            })
            primary_gate = ModelRequestGate("https://primary.example/v1", "sec-p", directory=tmp, wait_seconds=5)
            primary = OpenAIBrain(
                base_url="https://primary.example/v1",
                api_key="sec-p",
                model="default",
                overflow_brain=overflow,
                primary_wait_seconds=0.1,
                primary_cooldown_seconds=30.0,
                request_gate=primary_gate,
            )

            # Case 1: Primary slot held -> fast overflow after 0.1s wait (no HTTP sent on primary).
            held = threading.Event()
            release = threading.Event()
            def hold():
                with holder_gate.request():
                    held.set()
                    release.wait(2.0)
            t = threading.Thread(target=hold, daemon=True)
            t.start()
            self.assertTrue(held.wait(1.0))
            res = primary.chat_completion([{"role": "user", "content": "hi"}])
            self.assertEqual(res["model"], "deepseek-ai/DeepSeek-V4.1-Flash-1017")
            self.assertEqual(primary.last_request_meta["route"], "overflow")
            self.assertEqual(primary.last_request_meta["overflow_reason"], "primary_busy")
            release.set()
            t.join(timeout=2.0)

            # Case 2: Primary HTTP 500 error -> marks primary cooldown & immediately serves overflow.
            primary._chat_completion = Mock(side_effect=LLMError("llm_http_error", "HTTP 500", {"http_status": 500}))
            res2 = primary.chat_completion([{"role": "user", "content": "hi"}])
            self.assertEqual(res2["model"], "deepseek-ai/DeepSeek-V4.1-Flash-1017")
            self.assertEqual(primary.last_request_meta["overflow_reason"], "primary_llm_http_error")

            # Subsequent call during cooldown skips primary completely.
            res3 = primary.chat_completion([{"role": "user", "content": "hi"}])
            self.assertEqual(primary.last_request_meta["overflow_reason"], "primary_cooldown")
            self.assertEqual(primary._chat_completion.call_count, 1)

            # Case 3: Secret redaction covers both primary and overflow keys.
            planner = LLMSurvivalPlanner(
                {
                    "enabled": True,
                    "base_url": "https://primary.example/v1",
                    "api_key": "sec-p-secret",
                    "model": "default",
                    "overflow": {
                        "enabled": True,
                        "base_url": "https://doro.lat/v1",
                        "api_key": "sec-o-super-secret",
                        "model": "deepseek-v4.1-flash",
                    },
                },
                {"objective": "daily", "world_id": "test_scope"},
            )
            redacted = planner._redact("p=sec-p-secret o=sec-o-super-secret")
            self.assertNotIn("sec-p-secret", redacted)
            self.assertNotIn("sec-o-super-secret", redacted)
            planner.close()

    # ------------------------------------------------------------------
    # 2. SafetyArbiter tiered planning preemption (no medium-range cancels)
    # ------------------------------------------------------------------
    def test_safety_arbiter_planning_ignores_mid_range_hostiles_but_cancels_melee_or_hurt(self):
        rules = SurvivalPlanner(copy.deepcopy(DEFAULTS["survival"]))
        arbiter = SafetyArbiter(rules, copy.deepcopy(DEFAULTS["survival"]), daily=True, llm_override_on_block=True)
        base_status = _obs()["status"]

        # Zombie at 3.5 blocks while planning (no damage, no hazard) -> must NOT preempt planning!
        mid_zombie = {**base_status, "nearest_hostile": {"id": 1, "name": "zombie", "dist": 3.5}}
        res_mid = arbiter.evaluate_monitor(mid_zombie, base_status, decision=None, planning=True, context={})
        self.assertFalse(res_mid.danger, res_mid)

        # Zombie inside immediate 2.0-block melee radius -> DOES preempt planning.
        close_zombie = {**base_status, "nearest_hostile": {"id": 1, "name": "zombie", "dist": 2.0}}
        res_close = arbiter.evaluate_monitor(close_zombie, base_status, decision=None, planning=True, context={})
        self.assertTrue(res_close.danger)
        self.assertIn("hostile_within_limit", res_close.reasons)

    # ------------------------------------------------------------------
    # 3. Minimal close-range retreat thresholds (melee <= 2.5, ranged <= 5.0, |dy| <= 2.5)
    # ------------------------------------------------------------------
    def test_tight_close_range_retreat_skips_distant_and_offlevel_mobs(self):
        policy = DailySafetyPolicy({
            "low_health_threshold": 8,
            "food_threshold": 16,
            "threat_radius": 8,
            "disable_sleep": True,
            "sleep_policy": "strict",
            "llm_override_on_block": True,
        })
        baseline = Decision("wait", {"seconds": 1}, "idle", "idle")
        counts = {"stone_pickaxe": 1, "raw_iron": 3}

        # Daytime exploration with stone_pickaxe (no sword/shield) ignores distant/off-level mobs.
        obs_z = _obs(is_day=True, counts=counts, entities=[{"id": 1, "type": "hostile", "name": "zombie", "pos": [4.0, 64.0, 0.5]}])
        self.assertIsNone(policy.choose(obs_z, baseline, context={}))

        # Skeleton at 6.0m -> > 5.0m ranged threshold -> does NOT force retreat_from!
        obs_s = _obs(is_day=True, counts=counts, entities=[{"id": 2, "type": "hostile", "name": "skeleton", "pos": [6.5, 64.0, 0.5]}])
        self.assertIsNone(policy.choose(obs_s, baseline, context={}))

        # Zombie at 2.0m horizontally but 4.0m higher (different cave layer, |dy| > 2.5) -> ignored!
        obs_dy = _obs(is_day=True, counts=counts, entities=[{"id": 3, "type": "hostile", "name": "zombie", "pos": [1.5, 68.0, 0.5]}])
        self.assertIsNone(policy.choose(obs_dy, baseline, context={}))

        # Close same-level ranged skeleton within 5.0m (no shield) triggers close-range retreat_from.
        obs_close_skel = _obs(is_day=True, counts=counts, entities=[{"id": 4, "type": "hostile", "name": "skeleton", "pos": [4.5, 64.0, 0.5]}])
        chosen = policy.choose(obs_close_skel, baseline, context={})
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.name, "retreat_from")

    # ------------------------------------------------------------------
    # 4. Dead-loop fixes: hostile_position_unknown, unstable, repeated failure, retreat, gravel
    # ------------------------------------------------------------------
    def test_hostile_position_unknown_and_unstable_wait_streaks_break_after_two_cycles(self):
        policy = DailySafetyPolicy({
            "low_health_threshold": 8,
            "food_threshold": 16,
            "threat_radius": 8,
            "disable_sleep": True,
            "llm_override_on_block": True,
        })
        baseline = Decision("wait", {"seconds": 1}, "idle", "idle")
        ctx = {}

        # Bot3 underground scenario: wall-occluded cave_spider without entity coords.
        obs_unknown = _obs(pos=[-2117.3, 8.0, 42.5], nearest_hostile={"id": 14856, "name": "cave_spider", "dist": 4.7})
        d1 = policy.choose(obs_unknown, baseline, context=ctx)
        self.assertEqual(d1.name, "wait")
        d2 = policy.choose(obs_unknown, baseline, context=ctx)
        self.assertIsNone(d2, "Must release hostile_position_unknown wait streak rather than looping 939 times")
        self.assertTrue(any("Released repeated local wait" in w for w in policy.warnings(obs_unknown, ctx)))

        # Unstable footing loop (`on_ground=False` air-edge snag -> releases after 2 waits).
        ctx2 = {}
        obs_unstable = _obs(is_day=True, on_ground=False, in_water=False)
        self.assertEqual(policy.choose(obs_unstable, baseline, context=ctx2).name, "wait")
        self.assertEqual(policy.choose(obs_unstable, baseline, context=ctx2).name, "wait")
        self.assertIsNone(policy.choose(obs_unstable, baseline, context=ctx2))

    def test_loop_supervisor_records_planner_repeated_failure_cooldown(self):
        events = []
        sup = LoopSupervisor(record_fn=lambda l, e, d: events.append((e, d)))
        ctx = {}
        sig = LoopSupervisor.action_signature_parts("run_survival_task", {"task": "craft", "item": "furnace"})
        self.assertEqual(sig, "run_survival_task:craft:furnace")
        sup.record_planner_rejection(sig, "llm_repeated_failure", "Already failed", 1000.0, ctx)
        self.assertIn(sig, ctx.get("blocked_task_signatures", {}))
        self.assertTrue(any(e == "LOOP_ACTION_COOLDOWN" for e, _ in events))

    def test_retreat_controller_stops_jitter_reset_after_two_cooldown_cycles(self):
        ctx = {}
        ctl = RetreatController(ctx, {"threat_radius": 8, "target_cooldown_seconds": 30})
        obs = _obs(entities=[{"id": 1, "type": "hostile", "name": "zombie", "pos": [2.5, 64.0, 0.5]}], nearest_hostile={"id": 1, "name": "zombie", "dist": 2.0})
        d = Decision("retreat_from", {"pos": [2.5, 64.0, 0.5], "distance": 8}, "retreat", "safety")
        ctl.choose_decision(obs, d)
        ctl.handle_result(d, SkillResult(False, "No path", {"executed": True}, "no_path"), obs, uncertain_codes=set(), fatal_codes=set())
        ctx["retreat_recovery"]["cooldown_cycles"] = 2
        # Enemy wanders 3 blocks away while bot stayed at same block with full health -> must NOT reset cooldown!
        obs_moved_enemy = _obs(entities=[{"id": 1, "type": "hostile", "name": "zombie", "pos": [5.5, 64.0, 0.5]}], nearest_hostile={"id": 1, "name": "zombie", "dist": 5.0})
        self.assertFalse(ctl.changed(obs_moved_enemy, [5.5, 64.0, 0.5], ctx["retreat_recovery"]))

    def test_nether_portal_approaches_distant_gravel_and_allows_gravel_placement(self):
        self.assertIn("gravel", DailySurvivalSkills.PLACEABLE)
        obs = auto_world({"water_bucket": 1, "iron_ingot": 1, "cobblestone": 12, "iron_pickaxe": 1})
        obs["blocks"] = [{"name": "gravel", "pos": [12, 64, 0], "can_see": True, "can_dig": True}]
        ctl = AutonomousTaskController({})
        ctl.start({"task": "nether_portal"}, obs)
        step = ctl.next_action(obs)
        self.assertIsNotNone(step)
        self.assertEqual(step.name, "move_to")
        self.assertEqual(step.goal, "nether_portal_approach")
        self.assertEqual(step.args["range"], 2)


if __name__ == "__main__":
    unittest.main()
