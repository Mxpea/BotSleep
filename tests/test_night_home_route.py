"""Verify the real respawn/house coordinates with an in-memory, level return corridor."""

import copy
import unittest

from core.daily_observation import DailyObservationCollector
from core.daily_policy import DailySafetyPolicy
from skills.doors import DoorSkills
from skills.positioning import PositioningSkills
from skills.shelter import ShelterSkills
from skills.survival import SurvivalSkills
from test_autonomous_tasks import world
from test_daily_skills import envelope
from test_shelter_approach import WalkingShelterClient


class NightWorld(WalkingShelterClient):
    center = [-5, 67, -1]
    door = [-3, 67, -1]

    def __init__(self):
        super().__init__()
        self.status.update(pos=[-.5, 67, 9.5], busy=None, speed=0, velocity=[0, 0, 0],
                           is_day=False, time_of_day=18000, thundering=False, health=1.33)
        self.blocks = {(x, 66, z): {"name": "grass_block", "properties": {}}
                       for x in range(-9, 2) for z in range(-5, 11)}
        walls, roof, _ = ShelterSkills(self)._blueprint(self.center, "east")
        for pos in walls + roof:
            self.blocks[pos] = {"name": "birch_planks", "properties": {}}
        for y, half in ((67, "lower"), (68, "upper")):
            self.blocks[(-3, y, -1)] = {"name": "birch_door", "properties": {
                "half": half, "open": False, "facing": "west", "hinge": "right"}}
        self.collector = DailyObservationCollector(self)
        self.collector.set_context({"base_project": {"pos": self.center, "orientation": "east",
                                                     "dimension": "overworld", "material": "birch_planks"}})

    def inspect(self):
        obs = world({}, **copy.deepcopy(self.status))
        obs["entities"] = self.get_entities()
        self.collector._home(obs, None)
        return obs

    def interact(self, pos, **kwargs):
        self.actions.append(("door", list(pos)))
        for y in (67, 68):
            props = self.blocks[(-3, y, -1)]["properties"]
            props["open"] = props["open"] in (False, "false")
        return envelope({"interacted": True})


class NightHomeRouteTests(unittest.TestCase):
    def test_verified_level_approach_then_open_enter_and_close_at_low_health(self):
        w = NightWorld()
        policy = DailySafetyPolicy()
        actions = []
        for _ in range(18):
            obs = w.inspect()
            step = policy.choose(obs, None)
            if step.name == "wait":
                break
            self.assertIsNone(policy.validate(step, obs))
            if step.name == "position_for_task":
                route = obs["daily_evidence"]["shelter"]["exterior_route"]
                self.assertEqual(route["path"][-1], [-2, 67, -1])
                result = PositioningSkills(w).execute(step.name, step.args)
                self.assertTrue(result.data.get("night_home_step"), result)
                self.assertTrue(result.data.get("landing_stable"), result)
            elif step.name == "set_door":
                result = DoorSkills(w).execute(step.name, step.args)
            else:
                self.assertEqual(step.name, "move_to")
                result = SurvivalSkills(w).execute(step.name, step.args)
            self.assertTrue(result.success, result)
            actions.append(step.name)
        self.assertEqual(actions, ["position_for_task"] * 11 + ["set_door", "move_to", "set_door"])
        self.assertTrue(w.inspect()["daily_evidence"]["shelter"]["inside"])
        self.assertFalse(w.blocks[tuple(w.door)]["properties"]["open"])

    def test_refuses_unverified_far_end_unloaded_missing_door_and_ranged_threat(self):
        for change in ("water", "unknown", "door", "ranged", "out_of_range", "airborne"):
            with self.subTest(change=change):
                w = NightWorld()
                if change == "door":
                    del w.blocks[tuple(w.door)]
                elif change == "water":
                    w.blocks[-2, 66, -1] = {"name": "water", "properties": {}}
                elif change == "unknown":
                    w.blocks[-2, 66, 1] = None
                    w.blocks[-1, 66, 1] = None
                elif change == "ranged":
                    w.entities = [{"id": 3, "type": "hostile", "name": "skeleton", "pos": [-2, 67, -14]}]
                elif change == "out_of_range":
                    w.status["pos"][2] += 15
                else:
                    w.status["on_ground"] = False
                self.assertNotEqual(DailySafetyPolicy().choose(w.inspect(), None).name, "position_for_task")
                self.assertEqual(w.actions, [])

    def test_step_checks_threats_again_after_plan_and_uncertain_arrival_never_replays(self):
        for change in ("enemy", "no_motion"):
            w = NightWorld()
            step = DailySafetyPolicy().choose(w.inspect(), None)
            if change == "enemy":
                w.entities = [{"id": 1, "type": "hostile", "name": "skeleton", "pos": [8, 67, 7]}]
            else:
                def stand_still(*args, **kw):
                    w.actions.append(("move", args))
                    return envelope({"pos": w.status["pos"]})
                w.move_to = stand_still
            result = PositioningSkills(w).execute(step.name, step.args)
            self.assertFalse(result.success, result)
            if change == "enemy":
                self.assertEqual(w.actions, [])
            else:
                self.assertEqual(result.data["action_state"], "unknown")
                self.assertEqual(len(w.actions), 1)
