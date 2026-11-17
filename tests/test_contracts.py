"""Architecture contract, tool/skill consistency, and cross-process instance lock tests."""

import ast
import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import main as entrypoint
from agent.daily_survival import DAILY_SKILLS
from agent.tools import tools_for_objective
from core.competition_tasks import _KNOWN_SKILLS
from core.config import DEFAULTS
from core.domain import (
    BED_NAMES,
    FOOD_POINTS,
    LOG_NAMES,
    PLANK_NAMES,
    SAFE_FOOD,
    block_key,
    blocked_targets,
    cell_coords,
    extract_blocks,
    extract_hostiles,
    finite_number,
    has_tool_tier,
    inventory_counts,
    parse_pos,
)
from core.survival_state import SurvivalInstanceError, SurvivalJournal
from core.skill_registry import (
    INTERNAL_SKILLS,
    SKILL_REGISTRY,
    destination_checked_skills,
    resolve_skill_runner,
    skills_for_objective,
)
from skills.batch import BatchSkills
from skills.buckets import BucketSkills
from skills.combat import CombatSkills
from skills.daily_survival import DailySurvivalSkills
from skills.doors import DoorSkills
from skills.positioning import PositioningSkills
from skills.shelter import ShelterSkills
from skills.survival import SurvivalSkills


REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIRS = ("core", "agent", "skills", "debug", "integrations", "scripts", "tests")


class ArchitectureContractTests(unittest.TestCase):
    def _python_files(self):
        files = [REPO_ROOT / "main.py", REPO_ROOT / "bot_sleep.py", REPO_ROOT / "client.py"]
        for folder in SOURCE_DIRS:
            base = REPO_ROOT / folder
            if base.exists():
                files.extend(sorted(base.rglob("*.py")))
        return files

    def test_all_python_files_parse_and_avoid_unclosed_sqlite_with_statement(self):
        for path in self._python_files():
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.With):
                    for item in node.items:
                        expr = item.context_expr
                        if (isinstance(expr, ast.Call)
                                and isinstance(expr.func, ast.Attribute)
                                and isinstance(expr.func.value, ast.Name)
                                and expr.func.value.id == "sqlite3"
                                and expr.func.attr == "connect"):
                            self.fail(
                                f"{path.relative_to(REPO_ROOT)}:{node.lineno} uses bare "
                                "'with sqlite3.connect(...)'; wrap with contextlib.closing() to avoid WinError 32"
                            )

    def test_tool_catalogs_and_skill_argument_contracts_stay_in_sync(self):
        combined_arguments = {
            **SurvivalSkills.ARGUMENTS,
            **DailySurvivalSkills.ARGUMENTS,
            **ShelterSkills.ARGUMENTS,
            **BatchSkills.ARGUMENTS,
            **DoorSkills.ARGUMENTS,
            **PositioningSkills.ARGUMENTS,
            **CombatSkills.ARGUMENTS,
            **BucketSkills.ARGUMENTS,
        }
        starter_tools = {t["function"]["name"]: t["function"] for t in tools_for_objective("starter")}
        daily_tools = {t["function"]["name"]: t["function"] for t in tools_for_objective("daily")}
        competition_tools = {t["function"]["name"]: t["function"] for t in tools_for_objective("competition")}

        self.assertEqual(set(starter_tools), set(skills_for_objective("starter")))
        self.assertEqual(set(daily_tools), set(DAILY_SKILLS))
        self.assertEqual(set(daily_tools), set(skills_for_objective("daily")))
        self.assertEqual(set(competition_tools), set(skills_for_objective("competition")))
        self.assertEqual(set(SKILL_REGISTRY), set(starter_tools) | set(competition_tools) | INTERNAL_SKILLS)
        self.assertFalse(INTERNAL_SKILLS & (set(starter_tools) | set(competition_tools)))
        self.assertTrue(_KNOWN_SKILLS.issubset(set(daily_tools)))
        self.assertEqual(destination_checked_skills(daily=False), {"move_to", "gather_block", "place_item"})
        self.assertEqual(len(destination_checked_skills(daily=True)), 25)
        self.assertNotIn("fight_hostile", destination_checked_skills(daily=True))
        self.assertEqual([len(starter_tools), len(daily_tools), len(competition_tools)], [9, 35, 40])

        dummy_harness = Mock(
            daily=True,
            skills="base_runner",
            shelter_skills="shelter_runner",
            batch_skills="batch_runner",
            door_skills="door_runner",
            positioning_skills="pos_runner",
            combat_skills="combat_runner",
            bucket_skills="bucket_runner",
        )
        self.assertEqual(resolve_skill_runner(dummy_harness, "gather_block"), "base_runner")
        self.assertEqual(resolve_skill_runner(dummy_harness, "plan_shelter"), "shelter_runner")
        self.assertEqual(resolve_skill_runner(dummy_harness, "gather_materials"), "batch_runner")
        self.assertEqual(resolve_skill_runner(dummy_harness, "set_door"), "door_runner")
        self.assertEqual(resolve_skill_runner(dummy_harness, "position_for_task"), "pos_runner")
        self.assertEqual(resolve_skill_runner(dummy_harness, "fight_hostile"), "combat_runner")
        self.assertEqual(resolve_skill_runner(dummy_harness, "collect_liquid"), "bucket_runner")
        self.assertEqual(resolve_skill_runner(dummy_harness, "hunt_with_lava"), "bucket_runner")
        self.assertEqual(SKILL_REGISTRY["run_survival_task"].runner_slot, "autonomy")
        self.assertEqual(
            set(competition_tools) - set(daily_tools),
            {"select_task", "update_progress", "finish_task", "force_exit_task", "end_competition"},
        )

        # Every skill-backed tool in starter/daily must match its runner ARGUMENTS contract.
        meta_tools = {"auto_sleep", "wait", "goal_complete", "run_survival_task"}
        for catalog_name, catalog in (("starter", starter_tools), ("daily", daily_tools)):
            for name, fn in catalog.items():
                if name in meta_tools:
                    continue
                self.assertIn(name, combined_arguments, f"{catalog_name} tool '{name}' missing from skill ARGUMENTS")
                allowed_keys, required_keys = combined_arguments[name]
                props = set(fn["parameters"]["properties"]) - {"reason"}
                req = set(fn["parameters"].get("required", ()))
                self.assertEqual(props, allowed_keys, f"{catalog_name} tool '{name}' properties mismatch")
                self.assertEqual(req, required_keys, f"{catalog_name} tool '{name}' required keys mismatch")

        # Verify enum synchronization between tools and skill constants.
        self.assertEqual(
            set(starter_tools["gather_block"]["parameters"]["properties"]["expected_name"]["enum"]),
            set(SurvivalSkills.GATHERABLE),
        )
        self.assertEqual(
            set(starter_tools["place_item"]["parameters"]["properties"]["item"]["enum"]),
            set(SurvivalSkills.PLACEABLE),
        )
        self.assertEqual(
            set(starter_tools["eat_food"]["parameters"]["properties"]["item"]["enum"]),
            set(SurvivalSkills.FOODS),
        )
        self.assertEqual(
            set(daily_tools["place_item"]["parameters"]["properties"]["item"]["enum"]),
            set(DailySurvivalSkills.PLACEABLE),
        )
        self.assertEqual(
            set(daily_tools["eat_food"]["parameters"]["properties"]["item"]["enum"]),
            set(DailySurvivalSkills.FOODS),
        )
        self.assertEqual(
            set(daily_tools["gather_resource"]["parameters"]["properties"]["expected_name"]["enum"]),
            set(DailySurvivalSkills.IRON),
        )
        self.assertEqual(
            set(daily_tools["clear_foliage"]["parameters"]["properties"]["expected_name"]["enum"]),
            set(DailySurvivalSkills.FOLIAGE),
        )


class SurvivalInstanceLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def make_journal(self, **overrides):
        params = {
            "endpoint": "http://127.0.0.1:18900/api/v1",
            "username": "LockBot",
            "world_id": "default",
            **overrides,
        }
        return SurvivalJournal(self.temp.name, **params)

    def test_instance_lock_blocks_same_scope_and_allows_distinct_scope(self):
        journal = self.make_journal()
        self.assertFalse(journal.instance_status()["locked"])
        with journal.instance_lock():
            status = journal.instance_status()
            self.assertTrue(status["locked"])
            self.assertIsInstance(status["holder_pid"], int)
            duplicate = self.make_journal()
            with self.assertRaises(SurvivalInstanceError) as ctx:
                with duplicate.instance_lock():
                    pass
            self.assertEqual(ctx.exception.code, "instance_already_running")
            other_world = self.make_journal(world_id="second_world")
            with other_world.instance_lock():
                self.assertTrue(other_world.instance_status()["locked"])
        self.assertFalse(journal.instance_status()["locked"])

    def test_instance_lock_blocks_child_process_on_same_scope(self):
        journal = self.make_journal()
        code = "\n".join([
            "import sys",
            "from core.survival_state import SurvivalJournal, SurvivalInstanceError",
            f"j = SurvivalJournal({self.temp.name!r}, endpoint='http://127.0.0.1:18900/api/v1', "
            "username='LockBot', world_id='default')",
            "try:",
            "    with j.instance_lock():",
            "        sys.exit(0)",
            "except SurvivalInstanceError as e:",
            "    sys.exit(42 if e.code == 'instance_already_running' else 1)",
        ])
        with journal.instance_lock():
            proc = subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT), timeout=10, check=False)
            self.assertEqual(proc.returncode, 42)
        proc_after = subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT), timeout=10, check=False)
        self.assertEqual(proc_after.returncode, 0)

    def test_main_refuses_concurrent_survival_runner_before_login_or_logout(self):
        journal = self.make_journal()
        config = copy.deepcopy(DEFAULTS)
        config["agent"].update(name="LockBot", mode="survival")
        config["survival"].update(state_dir=self.temp.name, world_id="default", max_steps=1)
        config["debug"]["flight_recorder"]["enabled"] = False
        config["debug"]["monitor"].update(enabled=False, console=False)
        client = Mock(username="LockBot", base_url="http://127.0.0.1:18900/api/v1", token=None)

        stdout, stderr = io.StringIO(), io.StringIO()
        with journal.instance_lock():
            with patch.object(entrypoint, "load_config", return_value=config), \
                    patch.object(entrypoint, "MinecraftAgentClient", return_value=client), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = entrypoint.main(["--once"])
        self.assertEqual(exit_code, 1)
        self.assertIn("already owns survival scope", stderr.getvalue())
        client.login.assert_not_called()
        client.logout.assert_not_called()


class DomainPrimitivesTests(unittest.TestCase):
    def test_finite_number_and_parse_pos_reject_booleans_and_non_finite_values(self):
        self.assertTrue(finite_number(0))
        self.assertTrue(finite_number(-3.5))
        self.assertFalse(finite_number(True))
        self.assertFalse(finite_number(float("nan")))
        self.assertFalse(finite_number(float("inf")))

        self.assertEqual(parse_pos((1, 2.5, -3)), [1, 2.5, -3])
        self.assertIsNone(parse_pos([1, True, 3]))
        self.assertIsNone(parse_pos([1, 2]))
        self.assertEqual(parse_pos([1, 2, -3], integer=True), [1, 2, -3])
        self.assertIsNone(parse_pos([1.0, 2, 3], integer=True))

    def test_cell_coords_block_key_and_blocked_targets_normalize_coordinates(self):
        self.assertEqual(cell_coords([1.99995, 64.0, -2.5]), (2, 64, -3))
        self.assertEqual(block_key([1.9, 64.0, -2.1]), "1,64,-3")
        self.assertEqual(blocked_targets({"blocked_targets": ["1.9,64,-2.1", "custom_tag"]}), {"1,64,-3", "custom_tag"})

    def test_inventory_counts_blocks_hostiles_and_tool_tiers(self):
        obs = {
            "inventory": [
                {"name": "oak_log", "count": 2},
                {"name": "oak_log", "count": 3},
                {"name": "stone_pickaxe", "count": 1},
                {"name": "dirt", "count": 0},
            ],
            "blocks": [{"name": "oak_log", "pos": [2, 64, 0]}, {"name": "bad", "pos": [1, 2]}],
            "terrain": [{"name": "grass_block", "pos": [0, 63, 0]}],
            "entities": [
                {"type": "hostile", "name": "zombie", "pos": [4, 64, 0]},
                {"type": "passive", "name": "cow", "pos": [1, 64, 0]},
                {"type": "hostile", "name": "skeleton", "pos": [2, 64, 0]},
            ],
        }
        counts = inventory_counts(obs)
        self.assertEqual(counts, {"oak_log": 5, "stone_pickaxe": 1})
        self.assertTrue(has_tool_tier(counts, "pickaxe"))
        self.assertFalse(has_tool_tier(counts, "axe", wooden=True))
        self.assertEqual(len(extract_blocks(obs, include_terrain=False)), 1)
        self.assertEqual(len(extract_blocks(obs, include_terrain=True)), 2)
        hostiles = extract_hostiles(obs, [0, 64, 0])
        self.assertEqual([pos for _, pos in hostiles], [[2, 64, 0], [4, 64, 0]])
        self.assertEqual(extract_hostiles(obs, None), [])
        self.assertTrue(set(SAFE_FOOD).issubset(set(FOOD_POINTS)))
        self.assertEqual(len(LOG_NAMES), len(PLANK_NAMES))
        self.assertEqual(len(BED_NAMES), 16)


if __name__ == "__main__":
    unittest.main()
