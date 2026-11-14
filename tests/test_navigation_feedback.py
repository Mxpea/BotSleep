"""A no-op arrival must not be mistaken for recovery after stalled movement."""

import copy
from concurrent.futures import Future
import tempfile
import unittest
from unittest.mock import Mock

from agent.survival_planner import Decision
from core.config import DEFAULTS
from core.survival_harness import SurvivalHarness
from skills import SkillResult


class NavigationFeedbackTests(unittest.TestCase):
    def test_arrival_without_movement_does_not_erase_failure_count(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = {**copy.deepcopy(DEFAULTS["survival"]), "state_dir": directory}
            client = Mock(username="NavigationTest", base_url="http://local/api/v1")
            harness = SurvivalHarness(client, settings, DEFAULTS["sleep"])
            try:
                harness.failures = 2
                harness._future = Future()
                decision = Decision("move_to", {"pos": [4, 108, -6], "range": 2}, "Recover", "explore")
                result = SkillResult(True, "Already in range", {
                    "already_in_range": True, "executed": False, "moved": 0})
                harness._future.set_result((decision, result, {"status": {"ready": True}, "counts": {}}))
                self.assertTrue(harness.tick(automatic=False).success)
                self.assertEqual(harness.failures, 2)
                self.assertIn("NO_MOVEMENT", harness.journal.events_path.read_text())
            finally:
                harness.close()


if __name__ == "__main__":
    unittest.main()
