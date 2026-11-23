"""A suspended player's partial contact at the real Nov 18 starter-mine approach."""

import unittest

from core.autonomous_tasks import AutonomousTaskController
from skills.positioning import PositioningSkills
from test_autonomous_tasks import verified, world
from test_positioning_wall_margin import WallScene


class GroundEdge(WallScene):
    ORIGIN = [7.1, 69, 2.3]
    TARGET = [6.5, 69, 2.5]

    def __init__(self):
        super().__init__()
        self.status.update(pos=list(self.ORIGIN), health=11.28529, food=17, is_day=True,
                           time_of_day=500, thundering=False)
        self.blocks.clear()
        for x in (6, 7):
            for z in (1, 2):
                self.block([x, 69, z], "air")
                self.block([x, 70, z], "air")
                self.block([x, 68, z], "grass_block" if x == 6 else "air")
        self.block([7, 67, 1], "grass_block")
        self.block([7, 67, 2], "air")
        self.block([7, 66, 2], "dirt")

    def move(self):
        return PositioningSkills(self).execute("position_for_task", {"pos": self.TARGET})

    def sends(self):
        return [c for c in self.calls if c[0] in {"move", "stop", "dig", "control"}]


class SupportedEdgeTests(unittest.TestCase):
    def test_mining_uses_existing_contact_before_attempting_nearby_stone(self):
        w = GroundEdge()
        obs = world({"wooden_pickaxe": 1, "stick": 2}, **w.status)
        obs.update(footing_origin=list(w.ORIGIN), local_geometry_origin=list(w.ORIGIN),
                   footing=verified(pos=[7, 68, 2], name="air", properties={}),
                   safe_stand_candidates=[{"pos": list(w.TARGET), "support": "grass_block"}],
                   blocks=[{"pos": [8, 68, 2], "name": "stone", "can_see": True, "can_dig": True}])
        ctl = AutonomousTaskController({"objective": "daily"})
        ctl.start({"task": "craft", "item": "stone_pickaxe"}, obs)
        step = ctl.next_action(obs)
        self.assertEqual((step.name, step.args), ("position_for_task", {"pos": w.TARGET}))
        result = w.move()
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["edge_support_recovery"])
        self.assertTrue(result.data["landing_stable"])
        self.assertEqual(w.sends(), [("move", w.TARGET, {"range": 0, "timeout_ms": 8000})])

    def test_rejects_wet_deep_obstructed_or_unloaded_geometry(self):
        for pos, name in (([7, 67, 2], "lava"), ([7, 66, 2], "air"), ([6, 68, 1], "air"),
                          ([6, 69, 2], "oak_leaves"), ([6, 68, 2], "stone_slab"),
                          ([6, 68, 2], None)):
            with self.subTest(pos=pos, name=name):
                w = GroundEdge()
                if name is None:
                    del w.blocks[tuple(pos)]
                else:
                    w.block(pos, name)
                self.assertFalse(w.move().success)
                self.assertFalse(w.sends())

    def test_only_moves_inward_at_day_in_safe_stable_condition(self):
        for fields in ({"pos": [7.3, 69, 2.3]}, {"is_day": False}, {"health": 8},
                       {"thundering": True}, {"on_ground": False}, {"speed": .3},
                       {"nearest_hostile": {"name": "skeleton", "dist": 15}}):
            with self.subTest(fields=fields):
                w = GroundEdge()
                w.status.update(fields)
                self.assertFalse(w.move().success)
                self.assertFalse(w.sends())

    def test_rechecks_support_before_writing_and_observes_landing_before_declaring_success(self):
        w = GroundEdge()
        original = w.get_block
        visits = []

        def changing(pos, **kw):
            if pos == [7, 66, 2]:
                visits.append(pos)
                if len(visits) == 2:
                    w.block(pos, "water")
            return original(pos, **kw)

        w.get_block = changing
        self.assertFalse(w.move().success)
        self.assertFalse(w.sends())
        w = GroundEdge()

        def no_progress():
            w.status["pos"] = list(w.ORIGIN)
            return {"ok": True, "result": {}}

        w.effects["move"] = no_progress
        outcome = w.move()
        self.assertEqual((outcome.error_code, outcome.data["action_state"]), ("action_unconfirmed", "unknown"))
        self.assertEqual(len(w.sends()), 1)
