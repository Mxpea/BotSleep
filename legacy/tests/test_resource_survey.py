import threading
import unittest
from unittest.mock import Mock
from core.survival_state import ObservationCollector


class ResourceSurveyTests(unittest.TestCase):
    def test_blocked_survey_allows_two_fresh_entity_scans(self):
        release = threading.Event()
        class Client:
            def scan_blocks_detached(self, **kwargs):
                if not release.wait(3):
                    raise AssertionError('observation waited for resource scan')
                return []
        client = Client()
        status = dict(pos=[0, 64, 0], alive=True, ready=True, dimension='overworld')
        client.get_status = Mock(return_value={'result': status})
        client.get_entities = Mock(side_effect=[[{'id': 1}], [{'id': 2}]])
        client.get_blocks = Mock(return_value=[])
        collector = ObservationCollector(client)
        collector._inventory_evidence = Mock(return_value={'counts': {}, 'inventory': []})
        try:
            for entity in (1, 2):
                self.assertEqual(collector.collect()['entities'], [{'id': entity}])
            self.assertFalse(collector._resource_job.done())
        finally:
            release.set()
            collector.close()
