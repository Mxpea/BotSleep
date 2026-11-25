import unittest
from unittest.mock import Mock

from core.client import APIError
from skills.survival import SurvivalSkills


class BurialTests(unittest.TestCase):
    def run_skill(self, replies):
        client = Mock()
        status = dict(ready=True, alive=True, busy=None, health=16, pos=[.5, 64, .5], dimension="overworld")
        client.get_status.return_value = {"ok": True, "result": status}
        client.inventory.return_value = {"ok": True, "result": {"items": [{"name": "torch", "count": 1}]}}
        client.get_block.side_effect = [{"ok": True, "result": {"name": n}} for n in ("gravel", "air", "gravel", "air")]
        client.place_block.side_effect = replies
        client.dig.return_value = {"ok": True, "result": {"broken": True}}
        return client, SurvivalSkills(client).execute("clear_burial", {"pos": [0, 65, 0]})

    def test_occupied_then_placed_clears_head(self):
        client, result = self.run_skill([APIError("occupied", "falling"), {"ok": True, "result": {"placed": "torch", "pos": [0, 64, 0]}}])
        self.assertTrue(result.success, result)
        self.assertEqual(client.place_block.call_count, 2)
        client.dig.assert_called_once()

    def test_timeout_never_replays_or_digs(self):
        client, result = self.run_skill([APIError("request_timeout", "unknown")])
        self.assertEqual(result.data["action_state"], "unknown")
        client.place_block.assert_called_once()
        client.dig.assert_not_called()
