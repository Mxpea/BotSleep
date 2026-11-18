"""Real local skill geometry: enter the door instead of crossing new walls."""

import copy
import unittest

from core.autonomous_tasks import AutonomousTaskController
from core.daily_observation import DailyObservationCollector
from core.shelter_approach import avoids_shell, entrance_geometry, entrance_waypoint
from core.structure_spec import transform_rel_pos
from core.survival_state import ObservationCollector
from skills.positioning import PositioningSkills
from skills.shelter import ShelterSkills
from test_autonomous_tasks import world
from test_shelter import ShelterClient


class WalkingShelterClient(ShelterClient):
    def move_to(self, x, y, z, **kwargs):
        self.actions.append(("move", [x, y, z]))
        self.status["pos"] = [x, y, z]
        return {"ok": True, "result": {"pos": [x, y, z]}}


class ShelterApproachTests(unittest.TestCase):
    def setup_scene(self, orientation="east"):
        client = WalkingShelterClient()
        client.status.update(busy=None, is_day=True, time_of_day=1000, thundering=False,
                             speed=0, velocity=[0, 0, 0])
        client.status["pos"] = transform_rel_pos((-.5, 0, 3.4), [0, 64, 0], orientation, integer=False)
        client.items = {"oak_planks": 64, "dirt": 64, "oak_door": 1, "crafting_table": 1}
        client.blocks = {(x, 63, z): {"name": "grass_block", "properties": {}}
                         for x in range(-6, 7) for z in range(-6, 7)}
        skills = ShelterSkills(client)
        walls, _, _ = skills._blueprint([0, 64, 0], orientation)
        gap = tuple(transform_rel_pos((0, 0, -2), [0, 64, 0], orientation, integer=True))
        for pos in walls:
            if pos != gap:
                client.blocks[pos] = {"name": "oak_planks", "properties": {}}
        collector = DailyObservationCollector(client)
        collector.set_context({"base_project": {"pos": [0, 64, 0], "orientation": orientation,
                                                 "material": "oak_planks", "dimension": "overworld"}})
        return client, skills, collector

    def observe(self, client, collector):
        obs = world(client.items, **copy.deepcopy(client.status))
        obs["blocks"] = [{"name": b["name"], "pos": list(pos), "properties": b["properties"], "fresh": True}
                         for pos, b in client.blocks.items()]
        obs["local_geometry_origin"] = list(client.status["pos"])
        collector._home(obs, None)
        return obs

    def test_four_rotations_align_outside_enter_then_resume_real_placement_without_ai(self):
        for orientation in ("north", "east", "south", "west"):
            with self.subTest(orientation=orientation):
                client, skills, collector = self.setup_scene(orientation)
                obs = self.observe(client, collector)
                self.assertFalse(obs["shelter"]["inside"])
                entrance = entrance_geometry(obs["shelter"])
                self.assertIsNotNone(entrance)
                # A close internal cell has real support but its straight sweep hits the wall.
                blocked = transform_rel_pos((-.5, 0, 1.5), [0, 64, 0], orientation, integer=False)
                refusal = PositioningSkills(client).execute("position_for_task", {"pos": blocked})
                self.assertEqual(refusal.error_code, "unsafe_route", refusal)
                self.assertFalse(client.actions)
                controller = AutonomousTaskController()
                controller.start({"task": "shelter"}, obs)
                moved = []
                for _ in range(6):
                    obs = self.observe(client, collector)
                    action = controller.next_action(obs)
                    self.assertIsNotNone(action, controller.snapshot())
                    if action.name == "position_for_task":
                        result = PositioningSkills(client).execute(action.name, action.args)
                        moved.append(action.args["pos"])
                    else:
                        self.assertEqual(action.name, "build_shelter_step", action)
                        result = skills.execute(action.name, action.args)
                    self.assertTrue(result.success, result)
                    controller.record_result(action, result, self.observe(client, collector))
                    if action.name == "build_shelter_step":
                        break
                else:
                    self.fail("Did not resume actual construction after entering")
                self.assertEqual(moved[:2], [entrance["outside"], entrance["inside"]])
                self.assertEqual(controller.snapshot()["ai_escalations"], 0)
                self.assertEqual(controller.snapshot()["local_recoveries"], 0)
                self.assertEqual(sum(row[0] == "place" for row in client.actions), 1)

    def test_rounded_live_origin_uses_exterior_alignment_instead_of_all_four_wall_crossings(self):
        client, _, collector = self.setup_scene()
        client.status["pos"] = [3.4, 64, 1.5]
        obs = self.observe(client, collector)
        entrance = entrance_geometry(obs["shelter"])
        self.assertEqual(entrance_waypoint(client.status["pos"], entrance), [3.5, 64, .5])
        for target in ([1.5, 64, 1.5], [1.5, 64, .5], [.5, 64, 1.5], [2.5, 64, .5]):
            self.assertFalse(avoids_shell(client.status["pos"], target, entrance), target)
        self.assertTrue(avoids_shell(client.status["pos"], [3.5, 64, .5], entrance))
        self.assertTrue(avoids_shell([3.4, 64, .5], [1.5, 64, .5], entrance))

    def test_stale_unverified_or_closed_door_cannot_create_a_passage(self):
        client, _, collector = self.setup_scene()
        shelter = self.observe(client, collector)["shelter"]
        changes = ({"fresh": False}, {"inside": True}, {"hazards": [[2, 64, 1]]},
                   {"geometry": {"floor_verified": True, "exit_verified": False}},
                   {"door": {"pos": [2, 64, 0], "frame_verified": True, "open": False}},
                   {"door": {"pos": [2, 64, 1], "frame_verified": False, "reason": "door_missing"}})
        for change in changes:
            with self.subTest(change=change):
                self.assertIsNone(entrance_geometry({**shelter, **change}))

    def test_changed_doorway_is_rejected_by_real_skill_before_movement(self):
        client, _, collector = self.setup_scene()
        entrance = entrance_geometry(self.observe(client, collector)["shelter"])
        client.status["pos"] = list(entrance["outside"])
        client.blocks[tuple(entrance["doorway"])] = {"name": "oak_planks", "properties": {}}
        result = PositioningSkills(client).execute("position_for_task", {"pos": entrance["inside"]})
        self.assertEqual(result.error_code, "unsafe_route")
        self.assertFalse(client.actions)

    def complete_shell(self):
        client, skills, collector = self.setup_scene()
        walls, roof, _ = skills._blueprint([0, 64, 0], "east")
        for pos in walls + roof:
            client.blocks[pos] = {"name": "oak_planks", "properties": {}}
        client.status["pos"] = [-.5, 64, -3.5]
        project = {"version": 1, "scope": {"world_id": None}, "source": "verified_build",
            "pos": [0, 64, 0], "orientation": "east", "dimension": "overworld", "material": "oak_planks",
            "missing_blocks_hint": 0, "door_needed_hint": True, "requires_revalidation": True, "complete": False}
        return client, collector, {"shelter_project": project}

    def test_full_shell_north_side_reaches_east_door_around_walls_using_real_moves(self):
        client, collector, context = self.complete_shell()
        obs = self.observe(client, collector)
        controller = AutonomousTaskController()
        controller.start({"task": "shelter"}, obs, context)
        destination = obs["shelter"]["needs_door"]["stand_pos"]
        trajectory = [list(client.status["pos"])]
        for _ in range(16):
            obs = self.observe(client, collector)
            obs.update(ObservationCollector(client)._local_geometry(client.status, None))
            action = controller.next_action(obs, context)
            self.assertIsNotNone(action, controller.snapshot())
            if action.name == "place_door":
                break
            self.assertEqual(action.name, "position_for_task", action)
            result = PositioningSkills(client).execute(action.name, action.args)
            self.assertTrue(result.success, result)
            trajectory.append(list(client.status["pos"]))
            controller.record_result(action, result, self.observe(client, collector), context)
        else:
            self.fail("Door approach did not finish its bounded perimeter route")
        self.assertGreater(len(trajectory), 2)
        self.assertEqual(trajectory[-1], destination)
        self.assertEqual(controller.snapshot()["ai_escalations"], 0)
        self.assertEqual(controller.snapshot()["local_recoveries"], 0)

    def test_cooling_door_destination_still_approaches_it_instead_of_exploring_away(self):
        client, collector, context = self.complete_shell()
        obs = self.observe(client, collector)
        obs.update(ObservationCollector(client)._local_geometry(client.status, None))
        controller = AutonomousTaskController()
        destination = obs["shelter"]["needs_door"]["stand_pos"]
        context["blocked_targets"] = [controller.rules._key(destination)]
        controller.start({"task": "shelter"}, obs, context)
        action = controller.next_action(obs, context)
        self.assertIsNotNone(action, controller.snapshot())
        self.assertEqual((action.name, action.goal), ("position_for_task", "shelter_return"))
        self.assertNotEqual(action.args["pos"], destination)
        result = PositioningSkills(client).execute(action.name, action.args)
        self.assertTrue(result.success, result)

    def test_existing_start_margin_can_only_be_left_away_from_the_wall(self):
        client, collector, _ = self.complete_shell()
        client.status["pos"] = [-1.5, 64, -2.3]
        entrance = entrance_geometry(self.observe(client, collector)["shelter"])
        origin = list(client.status["pos"])
        # North wall occupies z=-2; .3 real player width just touches its face.
        self.assertTrue(avoids_shell(origin, [-2.5, 64, -2.5], entrance))
        self.assertFalse(avoids_shell(origin, [-2.5, 64, -2.3], entrance))
        self.assertFalse(avoids_shell(origin, [-1.5, 64, -1.5], entrance))
        self.assertFalse(avoids_shell([-1.5, 64, -2.2], [-2.5, 64, -2.5], entrance))

    def test_selector_prefers_verified_outward_margin_exit_over_uphill_return(self):
        client, collector, context = self.complete_shell()
        client.status["pos"] = [-1.5, 64, -2.3]
        # Like the saved north-side scene: east/north terrain is one block up,
        # while the adjacent west/north cell can leave the existing wall margin.
        client.blocks[(-1, 64, -3)] = {"name": "grass_block", "properties": {}}
        client.blocks[(-2, 64, -4)] = {"name": "grass_block", "properties": {}}
        obs = self.observe(client, collector)
        obs.update(ObservationCollector(client)._local_geometry(client.status, None))
        controller = AutonomousTaskController()
        controller.start({"task": "shelter"}, obs, context)
        action = controller.next_action(obs, context)
        self.assertIsNotNone(action, controller.snapshot())
        self.assertEqual(action.args["pos"], [-2.5, 64, -2.5])
        result = PositioningSkills(client).execute(action.name, action.args)
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["start_margin_recovery"])
        self.assertEqual(client.actions, [("move", [-2.5, 64, -2.5])])


if __name__ == "__main__":
    unittest.main()
