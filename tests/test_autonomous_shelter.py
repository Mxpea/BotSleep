"""Fresh house stances and real material-consuming local construction loop."""

import copy
import math
import unittest
from collections import Counter
from unittest.mock import patch

from agent.daily_survival import BUILDING_MATERIALS, DailySurvivalGoals
from agent.survival_planner import Decision
from agent.tools import tools_for_objective
from core.autonomous_tasks import AutonomousTaskController
from core.client import APIError
from core.structure_spec import SHELTER_BUILDING_BLOCKS
from skills import SkillResult
from skills.daily_survival import DailySurvivalSkills
from skills.doors import DoorSkills
from skills.positioning import PositioningSkills
from skills.shelter import ShelterSkills
from skills.survival import SurvivalSkills
from test_autonomous_tasks import apply_atomic, world, verified
from test_shelter import ShelterClient


class ConstructionWorld(ShelterClient):
    """Flat in-memory world; all placement consumes actual fixture inventory."""

    def __init__(self, orientation="north"):
        super().__init__()
        self.orientation = orientation
        self.status.update(is_day=True, time_of_day=1000, thundering=False)
        self.items = {"cobblestone": 55, "oak_door": 1}

    def move_to(self, x, y, z, **kwargs):
        self.actions.append(("move", [x, y, z]))
        self.status["pos"] = [x, y, z]
        return {"ok": True, "result": {"arrived": True}}

    def place_block(self, x, y, z, item=None):
        result = super().place_block(x, y, z, item)
        if item.endswith("_door"):
            self.blocks[(x, y, z)]["properties"] = {
                "half": "lower", "facing": self.orientation, "hinge": "left", "open": False}
            self.blocks[(x, y + 1, z)] = {"name": item, "properties": {
                "half": "upper", "facing": self.orientation, "hinge": "left", "open": False}}
        return result

    def observe(self):
        inspection = ShelterSkills(self).execute("inspect_shelter", {
            "pos": [0, 64, 0], "orientation": self.orientation})
        assert inspection.success, inspection
        obs = world(self.items, **self.status)
        obs["blocks"] = []
        obs["shelter"] = inspection.data
        obs["daily_evidence"] = {"base_site": verified(pos=[0, 64, 0], orientation=self.orientation)}
        if inspection.data["shelter_verified"]:
            obs["daily_evidence"]["shelter"] = verified(pos=[0, 64, 0], shelter_verified=True)
        return obs

    def existing_house(self, *, opened=True):
        walls, roof, door = ShelterSkills._blueprint([0, 64, 0], orientation=self.orientation)
        for pos in walls + roof:
            self.blocks[pos] = {"name": "cobblestone", "properties": {}}
        for height, half in ((0, "lower"), (1, "upper")):
            self.blocks[(door[0], door[1] + height, door[2])] = {"name": "oak_door", "properties": {
                "half": half, "facing": self.orientation, "hinge": "left", "open": opened}}

    def interact(self, pos, **kwargs):
        self.actions.append(("interact", list(pos)))
        lower = list(pos)
        if self.blocks[tuple(pos)]["properties"]["half"] == "upper":
            lower[1] -= 1
        opened = self.blocks[tuple(lower)]["properties"]["open"]
        for height in (0, 1):
            self.blocks[(lower[0], lower[1] + height, lower[2])]["properties"]["open"] = not opened
        if getattr(self, "lose_ack", False):
            raise APIError("request_timeout", "The fixture lost its acknowledgement after the toggle")
        return {"ok": True, "result": {"interacted": True}}


class AutonomousShelterTests(unittest.TestCase):
    def setUp(self):
        self.client = ConstructionWorld()

    def start(self, obs=None):
        obs = obs or self.client.observe()
        controller = AutonomousTaskController()
        controller.start({"task": "shelter"}, obs)
        return controller, obs

    def test_fresh_inspection_recomputes_position_hint_without_previous_failure(self):
        self.client.status["pos"] = [6.5, 64, .5]
        first = self.client.observe()["shelter"]
        self.assertTrue(first["needs_position"])
        self.assertTrue(first["stand_candidates"])
        self.client.status["pos"] = first["stand_candidates"][0]
        second = self.client.observe()["shelter"]
        self.assertFalse(second["needs_position"])
        self.assertTrue(second["visibility"]["visible"])
        self.assertEqual(self.client.actions, [])

    def test_shelter_stance_not_a_resource_is_a_checked_positioning_child(self):
        controller, obs = self.start()
        self.assertTrue(obs["shelter"]["needs_position"])
        decision = controller.next_action(obs)
        self.assertEqual(decision.name, "position_for_task")
        self.assertIn(decision.args["pos"], obs["shelter"]["stand_candidates"])
        self.assertLessEqual(math.dist(obs["status"]["pos"], decision.args["pos"]), 3)
        result = PositioningSkills(self.client).execute(decision.name, decision.args)
        self.assertTrue(result.success, result)
        controller.record_result(decision, result, self.client.observe())
        self.assertEqual(controller.snapshot()["ai_escalations"], 0)

    def test_stale_or_wrong_site_or_hazard_never_authorizes_a_house_move(self):
        controller, original = self.start()
        target = original["shelter"]["stand_candidates"][0]
        decision = Decision("move_to", {"pos": target, "range": .5}, "", "shelter")
        for change in ({"fresh": False}, {"dimension": "the_nether"}, {"pos": [9, 64, 9]},
                       {"orientation": "east"}, {"hazards": ["lava"]},
                       {"occupied_blueprint_cells": [[1, 64, 1]]},
                       {"geometry": {"floor_verified": False, "exit_verified": True}}):
            with self.subTest(change=change):
                obs = copy.deepcopy(original)
                obs["shelter"].update(change)
                self.assertIsNone(controller._shelter_stance(decision, obs, {}))
        blind = Decision("move_to", {"pos": [9.5, 64, 9.5]}, "", "shelter")
        self.assertIsNone(controller._shelter_stance(blind, original, {}))

    def test_fresh_distant_stance_only_approaches_via_short_observed_safe_ground(self):
        self.client.status["pos"] = [4.5, 64, .5]
        controller, obs = self.start()
        obs["local_geometry_origin"] = list(obs["status"]["pos"])
        obs["safe_stand_candidates"] = [{"pos": [2.5, 64, .5], "support": "grass_block"}]
        target = obs["shelter"]["stand_candidates"][0]
        decision = Decision("move_to", {"pos": target}, "", "shelter")
        approach = controller._shelter_stance(decision, obs, {})
        self.assertEqual(approach.name, "position_for_task")
        self.assertEqual(approach.args["pos"], [2.5, 64, .5])
        obs["local_geometry_origin"] = [1.5, 64, .5]
        self.assertIsNone(controller._shelter_stance(decision, obs, {}))

    def test_prewrite_needs_position_does_not_cool_down_whole_house(self):
        self.client.status["pos"] = [-.5, 64, -.5]
        controller, obs = self.start()
        decision = controller.next_action(obs)
        self.assertEqual(decision.name, "build_shelter_step")
        controller.record_result(decision, SkillResult(False, "Move first", {
            "executed": False, "needs_position": True}, "needs_position"), obs)
        self.assertNotIn(controller.rules._key([0, 64, 0]), controller.state()["blocked"])
        self.client.status["pos"] = [.5, 64, .5]
        next_decision = controller.next_action(self.client.observe())
        self.assertEqual(next_decision.name, "position_for_task")

    def test_ambiguous_or_real_failure_preserves_unknown_and_target_exclusions(self):
        for code, data in (("request_timeout", {"executed": False}),
                           ("needs_position", {"executed": True}),
                           ("unsafe_site", {"executed": False})):
            with self.subTest(code=code, data=data):
                self.client.status["pos"] = [-.5, 64, -.5]
                controller, obs = self.start()
                decision = controller.next_action(obs)
                controller.record_result(decision, SkillResult(False, "Failure", data, code), obs)
                if code == "request_timeout":
                    self.assertEqual(controller.snapshot()["status"], "unknown")
                    self.assertIsNone(controller.next_action(obs))
                else:
                    self.assertIn(controller.rules._key([0, 64, 0]), controller.state()["blocked"])

    def test_positioning_rechecks_live_corridor_before_writing(self):
        controller, obs = self.start()
        decision = controller.next_action(obs)
        target = decision.args["pos"]
        self.client.blocks[(math.floor(target[0]), 65, math.floor(target[2]))] = {"name": "stone"}
        result = PositioningSkills(self.client).execute(decision.name, decision.args)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "unsafe_route")
        self.assertFalse(any(row[0] == "move" for row in self.client.actions))

    def test_existing_fresh_open_door_is_closed_without_crafting_or_placing(self):
        self.client.existing_house()
        controller, obs = self.start()
        decision = controller.next_action(obs)
        self.assertEqual((decision.name, decision.args), ("set_door", {"pos": [0, 64, -2], "open": False}))
        result = DoorSkills(self.client).execute(decision.name, decision.args)
        self.assertTrue(result.success, result)
        after = self.client.observe()
        controller.record_result(decision, result, after)
        self.assertIsNone(controller.next_action(after))
        self.assertEqual(controller.snapshot()["status"], "completed")
        self.assertEqual(self.client.items, {"cobblestone": 55, "oak_door": 1})
        self.assertEqual(len(self.client.actions), 1)
        self.assertEqual(self.client.actions[0][0], "interact")

    def test_stale_unknown_or_inconsistent_door_never_selects_close_or_replacement(self):
        self.client.existing_house()
        original = self.client.observe()
        goals = DailySurvivalGoals()
        for change in ({"fresh": False}, {"dimension": "the_nether"},
                       {"door": {"frame_verified": False}},
                       {"needs_door": {"pos": [0, 64, -2], "reason": "door_unverified"}}):
            with self.subTest(change=change):
                obs = copy.deepcopy(original)
                obs["shelter"].update(change)
                decision = goals._shelter_action(obs, obs["counts"], obs["daily_evidence"]["base_site"], {})
                self.assertTrue(decision is None or decision["name"] == "inspect_shelter", decision)
        controller, obs = self.start()
        decision = controller.next_action(obs)
        self.client.blocks[(0, 65, -2)]["properties"].pop("open")
        result = DoorSkills(self.client).execute(decision.name, decision.args)
        self.assertEqual(result.error_code, "door_unverified")
        self.assertEqual(self.client.actions, [])

    def test_existing_door_lost_write_ack_stays_unknown_and_cannot_toggle_again(self):
        self.client.existing_house()
        self.client.lose_ack = True
        controller, obs = self.start()
        decision = controller.next_action(obs)
        result = DoorSkills(self.client).execute(decision.name, decision.args)
        self.assertFalse(result.success)
        controller.record_result(decision, result, self.client.observe())
        self.assertEqual(controller.snapshot()["status"], "unknown")
        self.assertIsNone(controller.next_action(self.client.observe()))
        self.assertEqual(sum(row[0] == "interact" for row in self.client.actions), 1)

    def test_one_accepted_goal_places_55_blocks_and_door_with_local_stances(self):
        for orientation in ("north", "east", "south", "west"):
            with self.subTest(orientation=orientation):
                self.client = ConstructionWorld(orientation)
                controller, obs = self.start()
                names = []
                for _ in range(128):
                    decision = controller.next_action(obs)
                    if decision is None:
                        break
                    names.append(decision.name)
                    implementation = (PositioningSkills if decision.name == "position_for_task"
                                      else DailySurvivalSkills if decision.name == "place_door" else ShelterSkills)
                    result = implementation(self.client).execute(decision.name, decision.args)
                    self.assertTrue(result.success, (decision, result, controller.snapshot()))
                    obs = self.client.observe()
                    controller.record_result(decision, result, obs)
                self.assertEqual(controller.snapshot()["status"], "completed", (names, controller.snapshot()))
                self.assertEqual(names.count("build_shelter_step"), 55)
                self.assertEqual(names.count("place_door"), 1)
                self.assertGreater(names.count("position_for_task"), 0)
                self.assertEqual(self.client.items, {"cobblestone": 0, "oak_door": 0})
                self.assertTrue(obs["shelter"]["shelter_verified"])
                self.assertEqual(controller.snapshot()["ai_escalations"], 0)

    def test_other_supported_wood_and_dirt_used_by_daily_house_chain(self):
        goals = DailySurvivalGoals()
        self.client.status["pos"] = [-.5, 64, -.5]
        obs = self.client.observe()
        base = obs["daily_evidence"]["base_site"]
        self.assertEqual(set(BUILDING_MATERIALS), SHELTER_BUILDING_BLOCKS)
        for item in SHELTER_BUILDING_BLOCKS:
            with self.subTest(item=item):
                decision = goals._shelter_action(obs, {item: 4}, base, {})
                self.assertEqual(decision["name"], "build_shelter_step")
                self.assertEqual(decision["args"]["material"], item)
        for wood in ("jungle", "acacia", "dark_oak", "mangrove", "cherry"):
            decision = goals._shelter_action(obs, {wood + "_log": 2}, base, {})
            self.assertEqual(decision["args"]["item"], wood + "_planks")
        tools = {row["function"]["name"]: row["function"] for row in tools_for_objective("daily")}
        for name in ("plan_shelter", "build_shelter_step", "build_shelter_batch"):
            self.assertEqual(set(tools[name]["parameters"]["properties"]["material"]["enum"]), SHELTER_BUILDING_BLOCKS)

    def test_empty_inventory_single_and_mixed_wood_complete_material_prerequisites(self):
        """Real controller/house skills, inventory-conserving gathering/crafting fixture."""
        for mixed in (False, True):
            with self.subTest(mixed=mixed):
                self.client = ConstructionWorld()
                self.client.items = {}
                saved = False
                positions = ([(x, 64, z) for x in (-3, 3) for z in range(-3, 4)]
                             + [(x, 64, 3) for x in range(-2, 3)])
                for index, pos in enumerate(positions):
                    self.client.blocks[pos] = {"name": "birch_log" if mixed and index % 2 else "oak_log",
                                               "properties": {}}

                def observe():
                    obs = self.client.observe()
                    obs["blocks"] = [{"pos": list(pos), "name": block["name"], "can_see": True,
                                      "can_dig": True, "properties": block.get("properties", {})}
                                     for pos, block in self.client.blocks.items()
                                     if block["name"] not in {"air", "grass_block"}]
                    if not saved:
                        obs["daily_evidence"] = {}
                        obs.pop("shelter")
                    return obs

                controller, obs = self.start(observe())
                actions = []
                for _ in range(128):
                    decision = controller.next_action(obs)
                    if decision is None:
                        break
                    actions.append(decision)
                    if decision.name in {"gather_block", "craft_item"}:
                        if decision.name == "craft_item" and decision.args["item"].endswith("_door"):
                            self.assertTrue(any(block["name"] == "crafting_table"
                                and math.dist(pos, self.client.status["pos"]) <= 3.5
                                for pos, block in self.client.blocks.items()))
                        result = apply_atomic(obs, decision)
                        self.client.items = obs["counts"].copy()
                        if decision.name == "gather_block":
                            self.client.blocks.pop(tuple(decision.args["pos"]))
                    else:
                        implementation = (PositioningSkills if decision.name == "position_for_task"
                            else DailySurvivalSkills if decision.name == "place_door"
                            else SurvivalSkills if decision.name == "place_item" else ShelterSkills)
                        result = implementation(self.client).execute(decision.name, decision.args)
                        if decision.name == "plan_shelter" and result.success:
                            saved = True  # Same boundary as the harness base_project checkpoint.
                    self.assertTrue(result.success, (decision, result))
                    obs = observe()
                    controller.record_result(decision, result, obs)
                self.assertEqual(controller.snapshot()["status"], "completed", controller.snapshot())
                totals = Counter(action.name for action in actions)
                self.assertEqual(totals["plan_shelter"], 1)
                self.assertEqual(totals["build_shelter_step"], 55)
                self.assertEqual(totals["gather_block"], 18 if mixed else 17)
                self.assertEqual(totals["place_item"], 1)
                self.assertEqual(totals["place_door"], 1)
                self.assertEqual(totals["inspect_shelter"], 0)
                self.assertEqual(sum(n for name, n in self.client.items.items() if name.endswith("_planks")),
                                 7 if mixed else 3)
                self.assertEqual(sum(n for name, n in self.client.items.items() if name.endswith("_door")), 2)
                self.assertEqual(sum(block["name"] == "crafting_table" for block in self.client.blocks.values()), 1)
                self.assertEqual(controller.snapshot()["ai_escalations"], 0)
                if not mixed:
                    self.assertEqual(len(actions), 110)

    def test_mixed_door_material_gather_retains_selected_species_and_fresh_gate(self):
        self.client.existing_house()
        self.client.blocks.pop((0, 64, -2))
        self.client.blocks.pop((0, 65, -2))
        self.client.items = {"oak_planks": 4, "birch_planks": 5, "crafting_table": 1}
        controller, obs = self.start()
        obs["blocks"] = [{"name": "oak_log", "pos": [2, 64, 0], "can_see": True},
                         {"name": "birch_log", "pos": [3, 64, 0], "can_see": True}]
        decision = controller.next_action(obs)
        self.assertEqual((decision.name, decision.args["expected_name"]), ("gather_block", "birch_log"))
        controller, obs = self.start()
        obs["shelter"]["fresh"] = False
        decision = controller.next_action(obs)
        self.assertEqual(decision.name, "inspect_shelter")

    @staticmethod
    def survey_rejection(decision, *, floor=False, exit=False, **extra):
        return SkillResult(False, "The current footprint was read and rejected", {
            "pos": decision.args["pos"], "orientation": decision.args.get("orientation", "north"),
            "dimension": "overworld", "fresh": True, "executed": False,
            "geometry": {"floor_verified": floor, "exit_verified": exit},
            "occupied_blueprint_cells": [], "hazards": [], **extra}, "unsafe_site")

    @staticmethod
    def survey_observation():
        obs = world()
        obs["blocks"] = []
        AutonomousShelterTests.refresh_survey_stands(obs)
        return obs

    @staticmethod
    def refresh_survey_stands(obs):
        x, y, z = obs["status"]["pos"]
        obs["local_geometry_origin"] = [x, y, z]
        obs["safe_stand_candidates"] = [{"pos": [x + dx, y, z + dz], "support": "grass_block"}
            for dx, dz in ((1, 0), (2, 0), (1, 1), (1, -1), (2, 1), (2, -1), (0, 1), (0, -1))]

    def test_four_readonly_site_rejections_reposition_locally_instead_of_asking_model(self):
        controller, obs = self.start(self.survey_observation())
        surveyed = set()
        for _ in range(4):
            decision = controller.next_action(obs)
            self.assertEqual(decision.name, "plan_shelter")
            self.assertNotIn(tuple(decision.args["pos"]), surveyed)
            surveyed.add(tuple(decision.args["pos"]))
            controller.record_result(decision, self.survey_rejection(decision), obs)
        self.assertEqual(controller.state()["local_failures"], 0)
        self.assertEqual(controller.state()["no_progress"], 0)
        self.assertEqual(controller.snapshot()["ai_escalations"], 0)
        move = controller.next_action(obs)
        self.assertEqual(move.name, "position_for_task")
        result = apply_atomic(obs, move)
        self.refresh_survey_stands(obs)
        controller.record_result(move, result, obs)
        with patch("core.autonomous_tasks.time.time", return_value=controller.state()["started_at"] + 40):
            decision = controller.next_action(obs)
        self.assertEqual(decision.name, "plan_shelter")
        self.assertNotIn(tuple(decision.args["pos"]), surveyed)
        self.assertEqual(controller.state()["survey_since_move"], 0)

    def test_exit_only_survey_failure_checks_other_orientations_without_blocking_center(self):
        obs = self.survey_observation()
        obs["safe_stand_candidates"] = []
        controller, obs = self.start(obs)
        orientations = []
        for _ in range(4):
            decision = controller.next_action(obs)
            self.assertEqual(decision.args["pos"], [0, 64, 0])
            orientations.append(decision.args.get("orientation", "north"))
            controller.record_result(decision, self.survey_rejection(decision, floor=True), obs)
        self.assertEqual(orientations, ["north", "east", "south", "west"])
        self.assertFalse(controller.state()["blocked"])
        self.assertEqual(controller.state()["local_failures"], 0)

    def test_survey_search_is_bounded_and_never_replays_a_rejected_orientation(self):
        controller, obs = self.start(self.survey_observation())
        surveyed = set()
        for _ in range(50):
            decision = controller.next_action(obs)
            if decision is None:
                break
            if decision.name == "plan_shelter":
                key = (tuple(decision.args["pos"]), decision.args.get("orientation", "north"))
                self.assertNotIn(key, surveyed)
                surveyed.add(key)
                result = self.survey_rejection(decision)
            else:
                self.assertEqual(decision.name, "position_for_task")
                result = apply_atomic(obs, decision)
                self.refresh_survey_stands(obs)
            controller.record_result(decision, result, obs)
        self.assertGreater(controller.state()["survey_steps"], 4)
        self.assertLessEqual(controller.state()["survey_steps"], 32)
        self.assertLessEqual(controller.state()["explore_steps"], 8)
        self.assertLessEqual(controller.snapshot()["steps"], 40)
        self.assertEqual(controller.snapshot()["status"], "needs_ai")
        self.assertEqual(controller.snapshot()["ai_escalations"], 1)
        self.assertEqual(controller.state()["local_failures"], 0)

    def test_survey_exception_requires_complete_known_prewrite_evidence(self):
        for change in ({"fresh": False}, {"executed": True}, {"orientation": None},
                       {"geometry": {}}, {"hazards": None}, {"action_state": "unknown"}):
            with self.subTest(change=change):
                controller, obs = self.start(self.survey_observation())
                decision = controller.next_action(obs)
                controller.record_result(decision, self.survey_rejection(decision, **change), obs)
                self.assertEqual(controller.state()["survey_steps"], 0)
                if change.get("action_state") == "unknown":
                    self.assertEqual(controller.snapshot()["status"], "unknown")
                    self.assertIsNone(controller.next_action(obs))
                else:
                    self.assertEqual(controller.state()["local_failures"], 1)
        controller, obs = self.start(self.survey_observation())
        controller._data["local_failures"] = 2  # Two earlier known write failures.
        decision = controller.next_action(obs)
        obs["blocks"] = [{"pos": [3, 64, 3], "name": "oak_log"}]
        controller.record_result(decision, self.survey_rejection(decision), obs)
        self.assertEqual(controller.state()["local_failures"], 2)

    def test_old_checkpoint_without_survey_fields_resumes_with_empty_search(self):
        controller, obs = self.start(self.survey_observation())
        saved = controller.state()
        for key in ("survey_steps", "survey_since_move", "rejected_sites"):
            saved.pop(key)
        restored = AutonomousTaskController(saved=saved)
        self.assertEqual(restored.snapshot()["status"], "suspended")
        self.assertEqual(restored.state()["survey_steps"], 0)
        self.assertEqual(restored.state()["survey_since_move"], 0)
        self.assertEqual(restored.state()["rejected_sites"], [])
        decision = restored.next_action(obs)
        self.assertEqual(decision.name, "plan_shelter")
        restored.record_result(decision, self.survey_rejection(decision), obs)
        self.assertEqual(restored.state()["survey_steps"], 1)

    def test_shelter_exploration_can_select_only_fresh_adjacent_cardinal_steps(self):
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            for dy in (-1, 1):
                with self.subTest(dx=dx, dy=dy, dz=dz):
                    obs = self.survey_observation()
                    obs["status"]["pos"] = [.5, 64, .5]
                    obs["local_geometry_origin"] = [.5, 64, .5]
                    target = [.5 + dx, 64 + dy, .5 + dz]
                    obs["safe_stand_candidates"] = [{"pos": target, "support": "grass_block"}]
                    controller, _ = self.start(obs)
                    decision = controller._explore(obs, {})
                    self.assertEqual((decision.name, decision.args), ("position_for_task", {"pos": target}))
                    controller._data["task"] = "gather"
                    self.assertIsNone(controller._explore(obs, {}))

    def test_shelter_step_selection_rejects_stale_diagonal_far_and_unsafe_candidates(self):
        for target, support, stale in (([1.5, 65, 1.5], "grass_block", False),
                ([2.5, 65, .5], "grass_block", False), ([1.5, 66, .5], "grass_block", False),
                ([1.5, 64.8, .5], "grass_block", False), ([1.4, 65, .5], "grass_block", False),
                ([1.5, 65, .5], "oak_leaves", False), ([1.5, 65, .5], "grass_block", True)):
            with self.subTest(target=target, support=support, stale=stale):
                obs = self.survey_observation()
                obs["status"]["pos"] = [.5, 64, .5]
                obs["local_geometry_origin"] = [2.5, 64, .5] if stale else [.5, 64, .5]
                obs["safe_stand_candidates"] = [{"pos": target, "support": support}]
                controller, _ = self.start(obs)
                self.assertIsNone(controller._explore(obs, {}))

    def test_shelter_return_step_uses_fresh_local_stance_and_not_remembered_terrain(self):
        target = self.client.observe()["shelter"]["stand_candidates"][0]
        self.client.status["pos"] = [target[0] - 1, target[1] - 1, target[2]]
        controller, obs = self.start()
        obs["local_geometry_origin"] = list(obs["status"]["pos"])
        obs["safe_stand_candidates"] = [{"pos": target, "support": "grass_block"}]
        requested = Decision("move_to", {"pos": target}, "", "shelter")
        decision = controller._shelter_stance(requested, obs, {})
        self.assertIsNotNone(decision)
        self.assertEqual((decision.name, decision.args), ("position_for_task", {"pos": target}))
        obs["safe_stand_candidates"] = []
        self.assertIsNone(controller._shelter_stance(requested, obs, {}))
        obs["safe_stand_candidates"] = [{"pos": target, "support": "grass_block"}]
        obs["shelter"]["fresh"] = False
        self.assertIsNone(controller._shelter_stance(requested, obs, {}))

    def test_shelter_step_cannot_bypass_cooldowns_visited_threats_or_search_budget(self):
        for rejection in ("blocked", "visited", "hostile", "budget"):
            with self.subTest(rejection=rejection):
                obs = self.survey_observation()
                obs["status"]["pos"] = [.5, 64, .5]
                obs["local_geometry_origin"] = [.5, 64, .5]
                target = [1.5, 65, .5]
                obs["safe_stand_candidates"] = [{"pos": target, "support": "grass_block"}]
                controller, _ = self.start(obs)
                context = {}
                if rejection == "blocked":
                    context["blocked_targets"] = [controller.rules._key(target)]
                elif rejection == "visited":
                    controller._data["visited"] = [controller.rules._key(target)]
                elif rejection == "hostile":
                    obs["entities"] = [{"id": 1, "type": "hostile", "name": "creeper", "pos": [2.5, 65, .5]}]
                else:
                    controller._data["explore_steps"] = controller.limits["max_explore_steps"]
                self.assertIsNone(controller._explore(obs, context))

    def test_real_write_failure_is_not_erased_by_following_readonly_survey(self):
        self.client.status["pos"] = [-.5, 64, -.5]
        controller, obs = self.start()
        build = controller.next_action(obs)
        self.assertEqual(build.name, "build_shelter_step")
        controller.record_result(build, SkillResult(False, "Write did not verify its block", {
            "executed": True}, "place_unverified"), obs)
        self.assertEqual(controller.state()["local_failures"], 1)
        failed_target = controller.rules._key(build.args["pos"])
        self.assertIn(failed_target, controller.state()["blocked"])
        # A later observation can lose the base evidence and discover a new
        # nearby footprint. Its known read rejection cannot forgive the write.
        after = self.survey_observation()
        decision = controller.next_action(after)
        self.assertEqual(decision.name, "plan_shelter")
        after["blocks"] = [{"pos": [3, 64, 3], "name": "oak_log"}]
        controller.record_result(decision, self.survey_rejection(decision), after)
        self.assertEqual(controller.state()["local_failures"], 1)
        self.assertIn(failed_target, controller.state()["blocked"])


if __name__ == "__main__":
    unittest.main()
