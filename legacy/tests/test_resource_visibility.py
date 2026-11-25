"""Resource visibility is derived in BotSleep using the unchanged block API."""

import copy
import unittest
from unittest.mock import Mock

from core.survival_state import ObservationCollector


class ResourceVisibilityTests(unittest.TestCase):
    def client(self, rows, blocks):
        client = Mock()
        client.get_status.return_value = {"result": {"pos": [0.5, 64, 0.5], "health": 20,
            "alive": True, "ready": True, "busy": None, "dimension": "overworld"}}
        client.inventory.return_value = {"result": {"items": []}}
        client.get_entities.return_value = []
        client.get_blocks.side_effect = lambda **kwargs: copy.deepcopy(rows) if kwargs.get("types") and "oak_log" in kwargs["types"] else []
        def get_block(pos):
            block = blocks.get(tuple(pos), {"name": "stone" if pos[1] < 64 else "air"})
            return {"result": {"properties": {}, "diggable": True, **block}}
        client.get_block.side_effect = get_block
        return client

    def test_legacy_api_reach_is_not_mistaken_for_visibility(self):
        rows = [{"name": "oak_log", "pos": [3, 64, 0]},
                {"name": "birch_log", "pos": [0, 64, 3]}]
        blocks = {(3, 64, 0): {"name": "oak_log", "harvest_tool_ok": True},
                  (0, 64, 3): {"name": "birch_log", "harvest_tool_ok": True},
                  (1, 65, 0): {"name": "stone"}, (1, 64, 0): {"name": "stone"}}
        client = self.client(rows, blocks)
        result = ObservationCollector(client, include_terrain=True).collect()
        observed = {row["name"]: row for row in result["blocks"]}
        self.assertTrue(observed["oak_log"]["can_dig"])
        self.assertFalse(observed["oak_log"]["can_see"])
        self.assertTrue(observed["birch_log"]["can_see"])
        self.assertLessEqual(client.get_block.call_count, 156)
        self.assertNotIn("can_see", rows[0])

    def test_far_resource_visibility_remains_unknown(self):
        client = self.client([{"name": "oak_log", "pos": [20, 64, 0]}], {})
        result = ObservationCollector(client, include_terrain=True).collect()
        self.assertNotIn("can_see", result["blocks"][0])
        self.assertNotIn("can_dig", result["blocks"][0])


if __name__ == "__main__":
    unittest.main()
