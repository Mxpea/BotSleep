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
from core.survival_state import SurvivalInstanceError, SurvivalJournal
from skills.batch import BatchSkills
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
        }
        starter_tools = {t["function"]["name"]: t["function"] for t in tools_for_objective("starter")}
        daily_tools = {t["function"]["name"]: t["function"] for t in tools_for_objective("daily")}
        competition_tools = {t["function"]["name"]: t["function"] for t in tools_for_objective("competition")}

        self.assertEqual(set(daily_tools), set(DAILY_SKILLS))
        self.assertTrue(_KNOWN_SKILLS.issubset(set(daily_tools)))
        self.assertEqual(
            set(competition_tools) - set(daily_tools),
            {"select_task", "update_progress", "finish_task", "force_exit_task", "end_competition"},
        )

        # Every skill-backed tool in starter/daily must match its runner ARGUMENTS contract.
        meta_tools = {"auto_sleep", "wait", "goal_complete"}
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


if __name__ == "__main__":
    unittest.main()
