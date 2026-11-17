"""Short verified ground runs; no game, model, or route replay."""

import copy
import math
import unittest

from core.client import APIError
from core.retreat_controller import RetreatController
from agent.survival_planner import Decision
from skills.survival import SurvivalSkills
from test_canopy_escape import terrain
from test_survival_skills import World, envelope


class GroundRetreatTests(unittest.TestCase):
    def world(self, primary=None):
        world = World()
        world.entities = [{"id": 1, "name": "zombie", "type": "hostile", "pos": primary or [-3, 64, .5]}]
        world.status.update(health=2, gamemode="survival", jumping=False)
        return world

    def run_retreat(self, world, recovery=None):
        return SurvivalSkills(world).execute("retreat_from", {"pos": world.entities[0]["pos"]},
                                              recovery_context=recovery)

    def test_close_enemy_runs_three_ground_cells_once_without_claiming_escape(self):
        world = self.world()
        result = self.run_retreat(world)
        self.assertTrue(result.success, result)
        self.assertEqual(world.writes(), [("move_to", [3.5, 64, .5], {"range": 0, "timeout_ms": 10000})])
        self.assertEqual(result.data["ground_run"]["steps"], 3)
        self.assertTrue(result.data["ground_reached"])
        self.assertFalse(result.data["retreat_complete"])
        self.assertTrue(result.data["retreat_required"])
        self.assertEqual(result.data["evacuation_stage"], "relay")
        for x in range(4):
            self.assertGreaterEqual(world.calls.count(("get_block", (x, 63, 0))), 2)
        self.assertLessEqual(result.data["terrain_search"]["block_reads"], 256)

    def test_existing_recovery_anchor_does_not_reduce_next_ground_run_to_one_cell(self):
        world = self.world()
        world.status["pos"] = [2.5, 64, .5]
        result = self.run_retreat(world, {"evacuating": True, "evacuation_origin": [.5, 64, .5]})
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["target"], [5.5, 64, .5])

    def test_only_fresh_all_enemy_clearance_can_complete_retreat(self):
        world = self.world([-6, 64, .5])
        result = self.run_retreat(world)
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["retreat_complete"])
        self.assertTrue(result.data["retreat_clearance"]["verified"])
        self.assertEqual(len([row for row in world.calls if row[0] == "get_entities"]), 3)

    def test_new_shooter_at_arrival_prevents_completion(self):
        world = self.world([-6, 64, .5])
        original = world.get_entities
        def entities(*args, **kwargs):
            if world.writes():
                world.entities.append({"id": 2, "name": "skeleton", "type": "hostile", "pos": [10, 64, .5]})
            return original(*args, **kwargs)
        world.get_entities = entities
        result = self.run_retreat(world)
        self.assertTrue(result.success, result)
        self.assertFalse(result.data["retreat_complete"])
        self.assertEqual(result.data["evacuation_stage"], "relay")

    def test_malformed_final_hostile_summary_keeps_known_arrival_but_needs_fresh_clearance(self):
        for summary in ([], False, {}, {"id": 2, "name": "skeleton"},
                        {"id": 2, "name": "skeleton", "dist": math.nan},
                        {"id": 2, "name": "skeleton", "dist": -1},
                        {"name": "skeleton", "dist": 20},
                        {"id": 2, "name": [], "dist": 20}):
            with self.subTest(summary=summary):
                world = self.world([-6, 64, .5])
                original = world.get_entities
                def entities(*args, **kwargs):
                    if world.writes():
                        world.status["nearest_hostile"] = summary
                    return original(*args, **kwargs)
                world.get_entities = entities
                result = self.run_retreat(world)
                self.assertTrue(result.success, result)
                self.assertTrue(result.data["ground_reached"])
                self.assertFalse(result.data["retreat_complete"])
                self.assertFalse(result.data["retreat_clearance"]["verified"])
                self.assertTrue(result.data["retreat_required"])
                self.assertTrue(result.data["requires_fresh"])
                self.assertEqual(result.data["evacuation_stage"], "relay")
                self.assertEqual([row[0] for row in world.writes()], ["move_to"])

    def test_malformed_or_saturated_final_scan_does_not_undo_known_arrival_or_claim_escape(self):
        for final_rows in ([{}], [None], [{"id": 2, "name": "skeleton", "type": "hostile"}],
                           [{"type": "hostile", "pos": [20, 64, .5]}],
                           [{"id": 2, "name": "skeleton", "type": "hostile", "pos": [20, 64, .5]}] * 40):
            with self.subTest(rows=final_rows[:1], count=len(final_rows)):
                world = self.world([-6, 64, .5])
                original = world.get_entities
                def entities(*args, **kwargs):
                    rows = original(*args, **kwargs)
                    return copy.deepcopy(final_rows) if world.writes() else rows
                world.get_entities = entities
                result = self.run_retreat(world)
                self.assertTrue(result.success, result)
                self.assertFalse(result.data["retreat_complete"])
                self.assertFalse(result.data["retreat_clearance"]["verified"])
                self.assertTrue(result.data["retreat_required"])
                self.assertTrue(result.data["requires_fresh"])
                self.assertEqual([row[0] for row in world.writes()], ["move_to"])

    def test_absent_null_or_valid_distant_summary_allows_a_complete_fresh_scan(self):
        for summary in (None, {"id": 2, "name": "skeleton", "dist": 20}):
            with self.subTest(summary=summary):
                world = self.world([-6, 64, .5])
                world.status["nearest_hostile"] = summary
                result = self.run_retreat(world)
                self.assertTrue(result.success, result)
                self.assertTrue(result.data["retreat_complete"])

    def test_incomplete_clearance_retains_controller_evacuation_for_fresh_planning(self):
        world = self.world([-6, 64, .5])
        world.status["nearest_hostile"] = {}
        context = {"retreat_recovery": {"evacuating": True, "evacuation_origin": [.5, 64, .5]},
                   "combat_retreat": {"active": True}}
        controller = RetreatController(context, {"threat_radius": 8, "target_cooldown_seconds": 30})
        result = self.run_retreat(world)
        decision = Decision("retreat_from", {"pos": [-6, 64, .5]}, "near hostile", "safety")
        controller.handle_result(decision, result, {"status": world.status, "entities": world.entities, "fresh": True},
                                 uncertain_codes=set(), fatal_codes=set())
        self.assertTrue(result.success, result)
        self.assertTrue(context["retreat_recovery"]["evacuating"])
        self.assertEqual(context["retreat_recovery"]["evacuation_origin"], [.5, 64, .5])
        self.assertIn("combat_retreat", context)
        self.assertEqual([row[0] for row in world.writes()], ["move_to"])

    def test_final_clearance_transport_failure_remains_unknown_without_cleanup(self):
        for code in ("network_error", "request_timeout", "not_connected", "invalid_response"):
            with self.subTest(code=code):
                world = self.world([-6, 64, .5])
                original = world.get_entities
                def entities(*args, **kwargs):
                    if world.writes():
                        raise APIError(code, "Final scan unavailable")
                    return original(*args, **kwargs)
                world.get_entities = entities
                result = self.run_retreat(world)
                self.assertFalse(result.success, result)
                self.assertEqual(result.data["action_state"], "unknown")
                self.assertFalse(result.data["retreat_complete"])
                self.assertEqual([row[0] for row in world.writes()], ["move_to"])

    def test_turn_step_up_drop_and_canopy_still_execute_only_one_adjacent_step(self):
        for nodes in (
            [(0,64,0,"stone"),(1,64,0,"stone"),(1,64,1,"stone"),(2,64,1,"stone")],
            [(0,64,0,"stone"),(1,65,0,"stone"),(2,65,0,"stone")],
            [(0,64,0,"stone"),(1,63,0,"stone"),(2,63,0,"stone")],
            [(0,64,0,"oak_leaves"),(1,64,0,"stone"),(2,64,0,"stone")]):
            with self.subTest(nodes=nodes):
                world = terrain(nodes)
                world.entities = copy.deepcopy(self.world().entities)
                result = self.run_retreat(world)
                self.assertTrue(result.success, result)
                self.assertNotIn("ground_run", result.data)
                self.assertEqual(result.data["target"], [nodes[1][0]+.5,nodes[1][1],nodes[1][2]+.5])
                self.assertEqual(len(world.writes()), 1)

    def test_third_cell_void_hazard_or_ceiling_caps_the_run_at_two(self):
        for pos, name in (([3,63,0],"air"),([3,63,0],"water"),([3,65,0],"stone")):
            with self.subTest(pos=pos, name=name):
                world = self.world()
                world.set_block(pos, name)
                result = self.run_retreat(world)
                self.assertTrue(result.success, result)
                self.assertEqual(result.data["target"], [2.5,64,.5])

    def test_changed_middle_cell_refuses_the_entire_write(self):
        world = self.world()
        original = world.get_block
        reads = 0
        def block(pos, **kwargs):
            nonlocal reads
            if pos == [2,64,0]:
                reads += 1
                if reads == 2:
                    world.set_block(pos,"stone")
            return original(pos, **kwargs)
        world.get_block = block
        result = self.run_retreat(world)
        self.assertEqual(result.error_code,"unsafe",result)
        self.assertFalse(world.writes())

    def test_final_scan_cannot_put_a_second_enemy_on_the_ground_corridor(self):
        world = self.world()
        original = world.get_entities
        scans = 0
        def entities(*args, **kwargs):
            nonlocal scans
            scans += 1
            if scans == 2:
                world.entities.append({"id":2,"name":"skeleton","type":"hostile","pos":[2,64,.5]})
            return original(*args, **kwargs)
        world.get_entities = entities
        result = self.run_retreat(world)
        self.assertEqual(result.error_code,"unsafe",result)
        self.assertFalse(world.writes())

    def test_second_shooter_circle_cannot_be_crossed_between_two_safe_endpoints(self):
        world = self.world()
        original = world.get_entities
        scans = 0
        def entities(*args, **kwargs):
            nonlocal scans
            scans += 1
            if scans == 2:
                world.entities.append({"id": 2, "name": "skeleton", "type": "hostile", "pos": [2, 64, 16.45]})
            return original(*args, **kwargs)
        world.get_entities = entities
        result = self.run_retreat(world)
        self.assertEqual(result.error_code, "unsafe", result)
        self.assertFalse(world.writes())

    def test_transport_unknown_never_replays_any_part_of_a_ground_run(self):
        world = self.world()
        def move(pos, options):
            raise APIError("network_error","Lost response")
        world.move_effect = move
        result = self.run_retreat(world)
        self.assertEqual(result.data["action_state"],"unknown")
        self.assertEqual(len(world.writes()),1)
        self.assertEqual(world.writes()[0][0],"move_to")

    def test_hurt_in_the_middle_keeps_the_known_termination_adapter_without_replay(self):
        world = self.world()
        world.status.update(health=20, speed=0, velocity=[0,0,0], last_death={"t":1})
        def move(pos, options):
            world.status.update(pos=[2.5,64,.5],health=16)
            raise APIError("interrupted","hurt",200,{"cause":"hurt"})
        world.move_effect = move
        result = self.run_retreat(world)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code,"retreat_interrupted",result)
        self.assertEqual(result.data["action_state"],"terminated")
        self.assertEqual(result.data["target"],[3.5,64,.5])
        self.assertEqual(result.data["pos"],[2.5,64,.5])
        self.assertTrue(result.data["actual_footing_verified"])
        self.assertFalse(result.data["retreat_complete"])
        self.assertEqual([row[0] for row in world.writes()],["move_to"])
        self.assertLessEqual(result.data["terrain_search"]["block_reads"],256)

    def test_final_status_shift_must_fit_the_entire_checked_corridor(self):
        world = self.world()
        original = world.get_status
        def status(**kwargs):
            if len([row for row in world.calls if row[0]=="get_entities"]) == 2:
                world.status["pos"]=[.5,64,.9]
            return original(**kwargs)
        world.get_status=status
        result=self.run_retreat(world)
        self.assertEqual(result.error_code,"unsafe",result)
        self.assertFalse(world.writes())

    def test_unknown_middle_corridor_cannot_be_coalesced(self):
        world=self.world()
        original=world.get_block
        def block(pos,**kwargs):
            if pos == [3,63,0]:
                raise APIError("not_loaded","Unobserved corridor")
            return original(pos,**kwargs)
        world.get_block=block
        result=self.run_retreat(world)
        self.assertTrue(result.success,result)
        self.assertEqual(result.data["target"],[2.5,64,.5])
        self.assertEqual(len(world.writes()),1)


if __name__ == "__main__":
    unittest.main()
