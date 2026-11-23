import unittest
from unittest.mock import Mock

from core.client import APIError, MinecraftAgentClient


class MovementContractTests(unittest.TestCase):
    def setUp(self):
        self.client = MinecraftAgentClient()
        self.client._request = Mock()

    def test_entity_target_uses_server_default(self):
        self.client.move_to(entity_id=12)
        self.assertEqual(self.client._request.call_args.kwargs['payload'], {'entity_id': 12, 'timeout': 15000})
        with self.assertRaises(APIError):
            self.client.move_to(1, 2, 3, entity_id=12)
        self.client._request.assert_called_once()

    def test_exact_stance_and_partial_scout_have_distinct_payloads(self):
        self.client.move_to(1, 64, 2, range=0)
        exact = self.client._request.call_args.kwargs['payload']
        self.assertEqual(exact['range'], 0)
        self.assertNotIn('allow_partial', exact)
        self.client.move_to(1, z=2, allow_partial=True)
        self.assertEqual(self.client._request.call_args.kwargs['payload'],
                         {'x': 1, 'z': 2, 'timeout': 15000, 'allow_partial': True})
