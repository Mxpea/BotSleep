import time
import unittest
from unittest.mock import Mock

from core.combat_reaction import CombatReaction


class CombatReactionTests(unittest.TestCase):
    def setUp(self):
        self.now = int(time.time() * 1000)
        self.status = {"alive": True, "ready": True, "dimension": "overworld", "health": 16}
        self.client = Mock(token="game-session")

    def events(self, *rows):
        self.client.get_events_page.return_value = {"result": {
            "events": [{"seq": i, "t": self.now, **row} for i, row in enumerate(rows, 1)],
            "last_seq": len(rows),
        }}

    def test_native_arrow_confirms_damage_before_an_attacker_is_visible(self):
        self.events({"type": "hurt", "source": "arrow", "source_entity": {"id": 17, "name": "skeleton"}})
        hit = CombatReaction().observe(self.client, self.status)
        self.assertEqual(hit["attacker"]["id"], 17)

    def test_fall_next_to_an_enemy_does_not_become_a_combat_hit(self):
        self.events({"type": "hurt", "source": "fall"})
        self.status["nearest_hostile"] = {"id": 1, "name": "zombie", "dist": 2}
        self.assertIsNone(CombatReaction().observe(self.client, self.status, {**self.status, "health": 20}))

    def test_damage_from_the_previous_life_is_not_replayed(self):
        self.events({"type": "hurt", "source": "mob_attack"}, {"type": "spawn"})
        self.status["last_death"] = {"t": self.now}
        self.assertIsNone(CombatReaction().observe(self.client, self.status))
