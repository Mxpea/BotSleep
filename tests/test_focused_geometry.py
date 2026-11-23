import unittest
from unittest.mock import Mock

from core.survival_state import ObservationCollector


class FocusedGeometryTests(unittest.TestCase):
    def scan(self, hazard):
        def block(pos):
            name = "stone" if pos[1] == 63 else "air"
            if hazard and pos == [1, 64, 0]:
                name = "lava"
            return {"result": {"name": name}}
        read = Mock(side_effect=block)
        result = ObservationCollector(None)._local_geometry({"pos": [.5, 64, .5]}, None,
                                                           read_block=read, target=[5, 64, 0])
        return result, read.call_count

    def test_direct_ground_checks_every_body_cell(self):
        result, calls = self.scan(False)
        self.assertEqual(calls, 3)
        self.assertEqual(result["safe_stand_candidates"][0]["pos"], [1.5, 64, .5])

    def test_blocked_forward_expands_without_accepting_lava(self):
        result, calls = self.scan(True)
        self.assertGreater(calls, 3)
        self.assertNotIn([1.5, 64, .5], [r["pos"] for r in result["safe_stand_candidates"]])
