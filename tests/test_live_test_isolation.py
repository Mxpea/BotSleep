"""Live fixtures stay inert during ordinary offline verification."""

import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = """
import builtins, runpy, socket, sys
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name in ('mcrcon', 'core.rcon'):
        raise AssertionError('Offline fixture imported optional RCON dependency')
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
def reject_connection(*args, **kwargs):
    raise AssertionError('Offline fixture attempted a network connection')
socket.socket.connect = reject_connection
sys.argv = [sys.argv[1]]
runpy.run_path(sys.argv[0], run_name='__main__')
"""


class LiveTestIsolationTests(unittest.TestCase):
    def run_fixture(self, filename):
        environment = dict(os.environ, BOTSLEEP_RUN_LIVE_TESTS="0", PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, "-B", "-c", LAUNCHER, str(ROOT / "tests" / filename)],
                              cwd=ROOT, env=environment, text=True, encoding="utf-8",
                              capture_output=True, timeout=15)

    def test_portal_unittest_skips_all_live_steps_without_rcon_or_network(self):
        result = self.run_fixture("test_speedrun_portal.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("skipped=3", result.stderr)

    def test_crafting_script_refuses_live_setup_without_opt_in(self):
        result = self.run_fixture("test_crafting.py")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Live RCON crafting fixture disabled", result.stderr)
        self.assertNotIn("AssertionError", result.stderr)

    def test_matrix_script_refuses_terrain_generation_without_opt_in(self):
        result = self.run_fixture("test_random_lava_matrix.py")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Live RCON terrain generation disabled", result.stderr)
        self.assertNotIn("AssertionError", result.stderr)


if __name__ == "__main__":
    unittest.main()
