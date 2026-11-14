import unittest

from core.client import APIError
from skills.doors import DoorSkills
from test_daily_skills import DailyWorld, envelope


class DoorWorld(DailyWorld):
    def __init__(self, opened=False):
        super().__init__()
        self.status["pos"] = [0.5, 64, -1.5]
        for y, half in ((64, "lower"), (65, "upper")):
            self.block([0, y, 0], "oak_door", properties={"half": half, "facing": "north", "hinge": "left", "open": opened})

    def interact(self, pos=None, **kwargs):
        self.calls.append(("interact", pos))
        if self.effects.get("interact"):
            return self.effects["interact"]()
        for y in (64, 65):
            prop = self.blocks[(0, y, 0)]["properties"]
            prop["open"] = not prop["open"]
        return envelope({"interacted": True})


class DoorTests(unittest.TestCase):
    def act(self, world, opened):
        return DoorSkills(world).execute("set_door", {"pos": [0, 64, 0], "open": opened})

    def test_open_then_close_are_verified_without_navigation(self):
        world = DoorWorld()
        for opened in (True, False):
            result = self.act(world, opened)
            self.assertTrue(result.success, result)
            self.assertIs(result.data["door"]["open"], opened)
        self.assertFalse(any(row[0] == "move" for row in world.calls))

    def test_already_requested_state_never_toggles(self):
        world = DoorWorld()
        result = self.act(world, False)
        self.assertTrue(result.success)
        self.assertFalse(result.data["executed"])
        self.assertFalse(any(row[0] == "interact" for row in world.calls))

    def test_nearby_threat_prevents_opening_but_does_not_prevent_closing(self):
        world = DoorWorld()
        world.status["nearest_hostile"] = {"dist": 4}
        self.assertEqual(self.act(world, True).error_code, "unsafe")
        for y in (64, 65): world.blocks[(0, y, 0)]["properties"]["open"] = True
        self.assertTrue(self.act(world, False).success)

    def test_far_door_requires_position_without_implicit_approach(self):
        world = DoorWorld()
        world.status["pos"] = [0, 64, -5]
        self.assertEqual(self.act(world, True).error_code, "needs_position")
        self.assertFalse(any(row[0] in {"interact", "move"} for row in world.calls))

    def test_mismatched_frame_is_rejected_without_a_write(self):
        world = DoorWorld()
        world.blocks[(0, 65, 0)]["properties"]["facing"] = "east"
        self.assertEqual(self.act(world, True).error_code, "door_unverified")
        self.assertFalse(any(row[0] == "interact" for row in world.calls))

    def test_unconfirmed_toggle_keeps_unknown_outcome(self):
        world = DoorWorld()
        world.effects["interact"] = lambda: envelope({"interacted": True})
        result = self.act(world, True)
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertTrue(result.data["executed"])
        self.assertEqual(result.data["action_state"], "unknown")

    def test_transport_timeout_is_never_retried(self):
        world = DoorWorld()
        def timeout(): raise APIError("request_timeout", "test")
        world.effects["interact"] = timeout
        result = self.act(world, True)
        self.assertEqual(result.error_code, "request_timeout")
        self.assertEqual(sum(row[0] == "interact" for row in world.calls), 1)


if __name__ == "__main__": unittest.main()
