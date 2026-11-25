import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent.llm_planner import LLMSurvivalPlanner
from agent.survival_planner import Decision
from core.autonomy_runtime import AutonomyRuntime, _TaskWindow
from core.config import DEFAULTS
from core.survival_memory import SurvivalMemory
from skills import SkillResult
from skills.mining import MineShaftSkill

try:
    from tests.test_llm_planner import Brain, response
except ImportError:
    from test_llm_planner import Brain, response


class _StubMiningClient:
    def __init__(self, pos):
        self._pos = list(pos)
        self.blocks_read = []

    def get_status(self):
        return {
            "ok": True,
            "result": {
                "ready": True,
                "alive": True,
                "dimension": "overworld",
                "pos": list(self._pos),
                "health": 20.0,
                "food": 20.0,
                "on_ground": True,
                "in_water": False,
                "in_lava": False,
            },
        }

    def get_inventory_counts(self):
        return {"stone_pickaxe": 1, "cobblestone": 16}

    def get_block(self, x, y, z):
        self.blocks_read.append((x, y, z))
        return {"ok": True, "result": {"name": "stone"}}


class Phase4MiningAndPortalTests(unittest.TestCase):
    def test_underground_reentry_skips_surface_caves_and_staircase(self):
        client = _StubMiningClient(pos=[10.5, 48.0, 20.5])
        skill = MineShaftSkill(client, memory=Mock())
        recorded_steps = []

        def fake_run(ctx, steps, *, precheck=None, verify_structure=None, ready_timeout=0.0):
            recorded_steps.extend(s.step_id for s in steps)
            self.assertTrue(ctx.extra.get("underground_reentry"))
            self.assertEqual(ctx.extra.get("deep_pos"), [10, 48, 20])
            self.assertEqual(ctx.extra.get("deep_trail"), [[10, 48, 20]])
            report = verify_structure(ctx)
            self.assertTrue(report.get("complete"))
            ctx.extra["stop_reason"] = "target_reached"
            return SkillResult(
                True,
                "done",
                data={"mining_progress": {"deep_trail": [[10, 48, 20]]}},
            )

        with patch("skills.mining.CaveExploreSkill") as mock_cave, patch(
            "skills.mining.ActionWorkflowRunner.run", side_effect=fake_run
        ):
            res = skill.mine_targets_from_surface(
                targets={"iron": {"count": 4, "min_tool": "stone_pickaxe"}},
                prefer_caves_first=True,
                total_timeout_seconds=120,
            )

        mock_cave.return_value.discover_and_explore_caves.assert_not_called()
        self.assertEqual(recorded_steps, ["excavate_branch", "settle_pose"])
        self.assertTrue(res.success)
        self.assertTrue(res.data.get("underground_reentry"))

    def test_mine_targets_propagates_concrete_shaft_error_code(self):
        client = _StubMiningClient(pos=[10.5, 64.0, 20.5])
        skill = MineShaftSkill(client, memory=Mock())

        with patch.object(
            skill,
            "build_mine_shaft",
            return_value=SkillResult(False, "No clean shaft plot", error_code="no_mine_site_found"),
        ), patch.object(
            skill, "_ensure_returned_to_surface", return_value={"on_surface": True, "pos": [10.5, 64.0, 20.5]}
        ):
            res = skill.mine_targets_from_surface(
                targets={"iron": {"count": 4, "min_tool": "stone_pickaxe"}},
                prefer_caves_first=False,
                total_timeout_seconds=180,
            )

        self.assertFalse(res.success)
        self.assertEqual(res.error_code, "no_mine_site_found")
        self.assertEqual((res.data or {}).get("stop_reason"), "no_mine_site_found")

    def test_survival_memory_preserves_surface_lava_across_death(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [1000.0]
            memory = SurvivalMemory(
                tmp,
                endpoint="http://localhost:18920/api/v1",
                username="LavaTester",
                world_id="world-1",
                clock=lambda: now[0],
            )
            self.addCleanup(memory.close)

            obs = {
                "fresh": True,
                "status": {"ready": True, "alive": True, "dimension": "overworld", "pos": [0, 64, 0]},
                "blocks": [
                    {"name": "lava", "pos": [24, 63, 12]},
                    {"name": "lava", "pos": [4, 14, 4]},
                ],
                "counts": {},
            }
            memory.observe(obs)
            self.assertEqual(memory._place_type("lava", pos=[24, 63, 12]), ("hazard", 3600))
            self.assertEqual(memory._place_type("lava", pos=[4, 14, 4]), ("hazard", 300))

            now[0] += 10.0
            memory.observe({
                "fresh": True,
                "status": {
                    "ready": True,
                    "alive": False,
                    "dimension": "overworld",
                    "pos": [0, 64, 0],
                    "last_death": {"t": 1010, "pos": [0, 64, 0]},
                },
                "blocks": [],
                "counts": {},
            })
            now[0] += 5.0
            candidates = memory.recall_lava_candidates(obs, limit=4)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["pos"], [24, 63, 12])
            self.assertEqual(candidates[0].get("invalidated_by"), "death")
            self.assertAlmostEqual(candidates[0]["confidence"], 0.425, places=3)

    def test_four_iron_triggers_portal_ready_and_lava_scout_focus(self):
        settings = copy.deepcopy(DEFAULTS["survival"])
        settings.update(objective="daily", intent="speedrun")
        brain = Brain(response("run_survival_task", {"task": "scout", "distance": 128}))
        planner = LLMSurvivalPlanner({"enabled": True, "max_repair_attempts": 0}, settings, brain=brain)
        obs = {
            "fresh": True,
            "observation_scope": "full",
            "inventory_fresh": True,
            "terrain_fresh": True,
            "status": {
                "ready": True,
                "alive": True,
                "busy": None,
                "gamemode": "survival",
                "dimension": "overworld",
                "pos": [0.5, 64.0, 0.5],
                "health": 20.0,
                "food": 20,
                "is_day": True,
                "time_of_day": 1000,
                "on_ground": True,
            },
            "counts": {"stone_pickaxe": 1, "iron_ingot": 4, "cobblestone": 16},
            "blocks": [],
            "entities": [],
        }
        decision = planner.choose(
            obs,
            context={"milestones": {}, "last_scout_report": {"found_lava": []}},
        )
        self.assertEqual(decision.name, "run_survival_task")
        self.assertEqual(decision.args.get("task"), "scout")
        self.assertEqual(decision.args.get("distance"), 128)
        last_call = brain.requests[-1]
        payload = json.loads(last_call["messages"][-1]["content"])
        warnings = (payload.get("context") or {}).get("safety_warnings", [])
        self.assertTrue(any(str(w).startswith("PORTAL_IRON_READY:") for w in warnings))

        scout_task_prop = next(
            t["function"]["parameters"]["properties"]["task"]
            for t in last_call["tools"]
            if t["function"]["name"] == "run_survival_task"
        )
        self.assertIn("scout", scout_task_prop["enum"])

    def test_autonomy_scout_and_nether_portal_use_lava_focus_and_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = object.__new__(AutonomyRuntime)
            runtime.continuous = False
            runtime.context = {}
            runtime.journal = Mock()
            runtime.journal.directory = Path(tmp)
            runtime.client = Mock()
            runtime.client.find_blocks.return_value = []
            runtime.memory = SurvivalMemory(
                tmp,
                endpoint="http://localhost:18920/api/v1",
                username="PortalBot",
                world_id="world-1",
            )
            self.addCleanup(runtime.memory.close)
            runtime.memory.remember_place(
                "hazard", "lava", [30, 63, 40], dimension="overworld", source="api_verified", ttl_seconds=3600
            )
            runtime._cancel = threading.Event()

            obs = {
                "fresh": True,
                "status": {"ready": True, "alive": True, "dimension": "overworld", "pos": [0, 64, 0]},
                "counts": {"iron_ingot": 4, "stone_pickaxe": 1},
            }

            with patch("skills.scouting.SurfaceScoutSkill.execute") as mock_scout:
                mock_scout.return_value = SkillResult(True, "ok", data={"found_lava": []})
                res = runtime._run_task_scout(
                    Decision("run_survival_task", {"task": "scout", "distance": 128}, "test", "scout"),
                    obs,
                    _TaskWindow(threading.Event(), 45),
                )
            self.assertEqual(mock_scout.call_args.kwargs.get("goal_focus"), "lava")
            self.assertTrue(res.data.get("lava_from_memory"))
            self.assertEqual(res.data["found_lava"][0]["pos"], [30, 63, 40])

            with patch.object(runtime, "_enter_lit_portal", return_value=None), patch(
                "skills.portal_speedrun.NetherPortalSpeedrunSkill.build_portal",
                side_effect=[
                    SkillResult(False, "none", error_code="no_portal_site_found"),
                    SkillResult(True, "built", data={"portal_active": True}),
                ],
            ), patch(
                "skills.survival.SurvivalSkills.execute",
                return_value=SkillResult(True, "moved", data={"executed": True}),
            ) as mock_move:
                portal_res = runtime._run_task_nether_portal(
                    Decision("run_survival_task", {"task": "nether_portal"}, "test", "nether_portal"),
                    obs,
                    _TaskWindow(threading.Event(), 120),
                )
            self.assertTrue(portal_res.success)
            self.assertEqual(mock_move.call_args.args[1]["pos"], [30, 64, 40])


if __name__ == "__main__":
    unittest.main()
