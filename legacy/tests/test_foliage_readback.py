"""The historical step-82 canopy layout, observed through a read-only fake."""

import copy
from dataclasses import asdict
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from agent.survival_planner import Decision
from core.config import DEFAULTS
from core.foliage_reconciliation import confirm_foliage, foliage_intent, pending_foliage, resolved_result
from core.survival_harness import SurvivalHarness
from core.survival_state import SurvivalJournal
from skills import SkillResult


class ReadOnlyScene:
    username = "ForestBot"
    base_url = "http://offline.invalid/api/v1"
    password = token = ""

    def __init__(self):
        self.status = dict(pos=[7.7,72,2.3], ready=True, alive=True, health=20, food=20, on_ground=True,
            velocity=[0,0,0], speed=0, last_death=None, gamemode="survival", dimension="overworld",
            busy=None, is_day=True, thundering=False, time_of_day=800)
        self.terrain = {(7,71,2): "oak_leaves", (6,70,2): "oak_leaves"}
        self.entities = []
        self.reads = []

    def get_status(self, **kwargs):
        return {"ok":True, "result":copy.deepcopy(self.status)}

    def get_entities(self, **kwargs):
        return copy.deepcopy(self.entities)

    def get_block(self, point, **kwargs):
        self.reads.append(tuple(point))
        name = self.terrain.get(tuple(point), "air")
        return {"ok":True, "result":{"name":name, "pos":list(point),
                                    "properties":{"waterlogged":False} if name.endswith("_leaves") else {}}}

    def snapshot(self, *_):
        return {"fresh":True, "inventory_fresh":True, "status":copy.deepcopy(self.status), "entities":copy.deepcopy(self.entities),
                "blocks":[], "terrain":[], "inventory":[], "counts":{}, "footing":
                {"name":"oak_leaves","pos":[7,71,2],"properties":{"waterlogged":False}}}


class FoliageReadbackTests(unittest.TestCase):
    def setUp(self):
        self.world = ReadOnlyScene()
        self.decision = Decision("canopy_escape", {}, "Clear an observed obstruction", "safety")
        self.result = SkillResult(False, "Leaf removal and both unchanged supports were not confirmed", {
            "skill":"canopy_escape","pos_before":[7.7,72,2.3],"executed":True,"action_state":"unknown",
            "cleanup_skipped":"navigation_unknown","recovery_error":"action_unconfirmed","origin":[7.7,72,2.3],
            "target":None,"moved":0,"origin_support":{"center":[7,71,2],"center_name":"oak_leaves","state":"center_supported"},
            "operation":"clear_foliage","cleared_pos":[6,71,2],"cleared_name":"oak_leaves","descent_target":[6.5,71,2.5]},
            "action_unconfirmed")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = {**copy.deepcopy(DEFAULTS["survival"]), "state_dir":self.temp.name, "objective":"daily",
                         "autonomy":{"enabled":True,"mainline":False}}
        self.journal = SurvivalJournal(self.temp.name, endpoint=self.world.base_url, username=self.world.username,
                                       world_id=self.settings["world_id"])
        self.intent = self.make_intent()

    def make_intent(self):
        return foliage_intent(asdict(self.decision), asdict(self.result), self.world.status, 4)

    def old_checkpoint(self, write_generation=True):
        pending = {"decision":asdict(self.decision),"before_counts":{},"before_counts_fresh":True,"task_id":None}
        self.journal.update(context={"life_generation":4,"last_dimension":"overworld","last_result":asdict(self.result)},
                            last_result=asdict(self.result), pending=pending)
        self.journal.record("DECISION", {"decision":asdict(self.decision),"counts":{},"status":copy.deepcopy(self.world.status)})
        if write_generation:
            self.journal.record("LOCAL_RECOVERY_UNKNOWN", {"kind":"canopy_recovery", "recovery":{
                "unknown":True, "life_generation":4, "dimension":"overworld", "origin":[7.7,72,2.3]}})
        self.journal.record("STEP_RESULT", {"step":82,"decision":asdict(self.decision),"result":asdict(self.result)})
        return pending

    def harness(self):
        collector = Mock()
        collector.collect.side_effect = self.world.snapshot
        h = SurvivalHarness(self.world,self.settings,DEFAULTS["sleep"],journal=self.journal,collector=collector)
        self.addCleanup(h.close)
        return h

    def finish(self, h):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            r = h.tick(automatic=False)
            if r is not None: return r
            time.sleep(.002)
        self.fail("Foliage readback did not finish")

    def test_step82_legacy_startup_reobserves_and_releases_to_existing_emergency_policy(self):
        self.old_checkpoint()
        self.assertEqual(pending_foliage(self.journal),self.intent)
        self.world.entities = [{"name":"creeper","type":"hostile","id":28,"pos":[10.5,72,2.3],"dist":2.8}]
        self.world.status["nearest_hostile"] = {"name":"creeper","id":28,"dist":2.8}
        h = self.harness()
        self.assertFalse(h.paused)
        h.tick(automatic=True)
        result = self.finish(h)
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["readback"]["hostile_nearby"])
        self.assertTrue(result.data["readback"]["removed"])
        self.assertFalse(h.paused)
        self.assertIsNone(h.journal.state["pending"])
        self.assertIn(h._mandatory_safety(self.world.snapshot()).name, {"retreat_from", "fight_hostile"})
        self.assertEqual(h.snapshot()["task_execution"]["status"],"idle")

    def test_prewrite_or_move_unknown_cannot_become_a_leaf_confirmation(self):
        for change in ({"executed":False},{"moved":1},{"cleared_pos":[7,71,2]}, {"descent_target":[7.5,71,2.5]},
                       {"operation":"move"}, {"skill":"position_for_task"}):
            with self.subTest(change=change):
                res = asdict(self.result)
                res["data"].update(change)
                self.assertIsNone(foliage_intent(asdict(self.decision),res,self.world.status,4))

    def test_later_identical_decision_without_a_result_is_not_the_old_dig(self):
        self.old_checkpoint()
        self.journal.record("DECISION", {"decision":asdict(self.decision),"counts":{},"status":self.world.status})
        self.assertIsNone(pending_foliage(self.journal))

    def test_unavailable_or_incompatible_world_keeps_exact_checkpoint_for_later_fresh_resume(self):
        original = self.old_checkpoint()
        self.world.terrain[(6,70,2)] = "lava"
        h = self.harness()
        self.assertTrue(h._start())
        self.assertFalse(self.finish(h).success)
        self.assertTrue(h.paused)
        self.assertEqual(self.journal.state["pending"], original)
        self.assertEqual(self.journal.state["last_result"], asdict(self.result))
        self.world.terrain[(6,70,2)] = "oak_leaves"
        self.assertTrue(h.resume())
        self.assertTrue(h._start())
        self.assertTrue(self.finish(h).success)
        self.assertIsNone(self.journal.state["pending"])

    def test_still_present_does_not_claim_success_and_excludes_leaf_from_same_stance(self):
        self.old_checkpoint()
        self.world.terrain[(6,71,2)] = "oak_leaves"
        h = self.harness()
        self.assertTrue(h._start())
        outcome = self.finish(h)
        proof = outcome.data["readback"]
        self.assertFalse(proof["removed"])
        self.assertFalse(resolved_result(self.result,proof).success)
        self.assertIn("foliage_no_retry",h.context)
        h._prepare_context(self.world.snapshot())
        self.assertIn("6,71,2",h.context["blocked_targets"])
        self.world.status["pos"] = [8.5,72,2.5]
        h._prepare_context(self.world.snapshot())
        self.assertNotIn("foliage_no_retry",h.context)

    def test_lifecycle_drift_and_substituted_material_are_unconfirmed(self):
        for field,value in (("last_death",{"t":1}),("busy","dig"),("on_ground",False),("health",18),
                            ("pos",[7.7,71,2.3]),("dimension","the_nether")):
            with self.subTest(field=field):
                world = ReadOnlyScene()
                world.status[field] = value
                self.assertIsNone(confirm_foliage(world,self.intent,4,threading.Event())[0])
        self.world.terrain[(6,71,2)] = "stone"
        self.assertEqual(confirm_foliage(self.world,self.intent,4,threading.Event()),(None,"target_replaced"))
        self.assertEqual(confirm_foliage(self.world,self.intent,5,threading.Event()),(None,"life_generation_changed"))

    def relocate_to_observed_blast_edge(self):
        self.world.status.update(pos=[7.1,69,2.3],health=11.285292,food=17)
        self.world.terrain = {(6,68,1): "grass_block", (6,68,2): "grass_block"}

    def test_new_stable_edge_abandons_only_old_leaf_without_counting_a_success(self):
        self.old_checkpoint()
        self.relocate_to_observed_blast_edge()
        h = self.harness()
        self.assertTrue(h._start())
        result = self.finish(h)
        self.assertTrue(result.success, result)
        proof = result.data["readback"]
        self.assertEqual(proof["resolution"], "superseded_stance")
        self.assertFalse(proof["original_action_confirmed"])
        self.assertFalse(proof["removed"])
        self.assertFalse(resolved_result(self.result,proof).success)
        self.assertIn({"pos":[6,68,2],"name":"grass_block"}, proof["supports"])
        self.assertTrue(all(row["name"] == "air" for row in proof["original_terrain_after"].values()))
        self.assertIsNone(h.journal.state["pending"])
        h._prepare_context(self.world.snapshot())
        self.assertIn("6,71,2",h.context["blocked_targets"])
        self.assertNotIn("canopy_recovery",h.context)

    def test_unsettled_new_cell_failed_footprint_and_hazards_keep_old_leaf_pending(self):
        self.relocate_to_observed_blast_edge()
        for change in ("no_support","near_hazard","head_blocked","falling","busy","death"):
            with self.subTest(change=change):
                w=copy.deepcopy(self.world)
                if change == "no_support": w.terrain.clear()
                if change == "near_hazard": w.terrain[(8,68,2)]="lava"
                if change == "head_blocked": w.terrain[(6,70,2)]="stone"
                if change == "falling": w.status["on_ground"]=False
                if change == "busy": w.status["busy"]="dig"
                if change == "death": w.status["last_death"]={"t":99}
                self.assertIsNone(confirm_foliage(w,self.intent,4,threading.Event())[0])

    def respawn(self, record_deaths=True):
        t = time.time_ns() // 1_000_000 + 1000
        markers = [{"t":t+i*1000,"pos":[-0.8,68.3,10.4]} for i in range(3)]
        if record_deaths:
            for i, marker in enumerate(markers[:2]):
                self.journal.record("DEATH_OBSERVED", {"count":i+1,"last_death":marker})
        self.world.status.update(last_death=markers[-1],pos=[-0.5,67,9.5],food=20,health=20)
        self.world.terrain = {(-1,66,9):"grass_block",(-1,67,9):"leaf_litter"}
        self.journal.update(context={**self.journal.state["context"],"life_generation":6,
            "last_death":json.dumps(markers[-1],sort_keys=True),"was_alive":True})
        return markers

    def test_old_action_expires_only_after_scoped_death_and_stable_fresh_respawn(self):
        self.old_checkpoint()
        markers = self.respawn()
        # Old processes may leave more than 1 MiB of telemetry after the dig.
        for _ in range(17): self.journal.record("STATUS", {"past": "a"*64000})
        binding = pending_foliage(self.journal)
        self.assertEqual(binding["generation"],4)
        self.assertEqual(binding["completed_life"]["death_times"],[m["t"] for m in markers[:2]])
        h = self.harness()
        self.assertTrue(h._start())
        outcome = self.finish(h)
        self.assertTrue(outcome.success, outcome)
        proof = outcome.data["readback"]
        self.assertEqual(proof["resolution"],"superseded_life")
        self.assertFalse(proof["original_action_confirmed"])
        self.assertFalse(resolved_result(self.result,proof).success)
        self.assertNotIn((6,71,2),self.world.reads)
        self.assertIsNone(h.journal.state["pending"])
        self.assertNotIn("foliage_no_retry",h.context)

    def test_just_a_death_marker_or_missing_generation_cannot_cancel_old_dig(self):
        self.old_checkpoint(write_generation=False)
        markers = self.respawn(record_deaths=False)
        binding = pending_foliage(self.journal)
        self.assertFalse((binding or {}).get("completed_life"))
        self.assertIsNone(confirm_foliage(self.world,binding,6,threading.Event())[0])
        self.journal.record("DEATH_OBSERVED",{"count":1,"last_death":markers[0]})
        self.assertEqual(pending_foliage(self.journal),{"invalid":True})
        self.journal.state["scope"] = {**self.journal.scope,"world_id":"other"}
        self.assertIsNone(pending_foliage(self.journal))

    def test_dead_unsettled_or_unsafe_respawn_remains_pending(self):
        self.old_checkpoint()
        markers = self.respawn()
        binding = pending_foliage(self.journal)
        for case in ("dead","suspended","old_marker","lava"):
            with self.subTest(case=case):
                w=copy.deepcopy(self.world)
                if case=="dead": w.status["alive"]=False
                if case=="suspended": w.status["on_ground"]=False
                if case=="old_marker": w.status["last_death"]=markers[0]
                if case=="lava": w.terrain[(-1,66,9)]="lava"
                self.assertIsNone(confirm_foliage(w,binding,6,threading.Event())[0])

    def paused_scene_then_respawn(self):
        self.old_checkpoint()
        self.world.terrain[(6,70,2)]="lava"
        h=self.harness()
        self.assertTrue(h._start())
        self.assertFalse(self.finish(h).success)
        self.assertEqual(h.pause_reason,"action_unconfirmed")
        h.tick(automatic=False)  # first living heartbeat before the death
        marker={"t":time.time_ns()//1_000_000+1000,"pos":[7.1,69,2.3]}
        self.world.status.update(last_death=marker,pos=[-0.5,67,9.5])
        self.world.terrain={(-1,66,9):"grass_block"}
        h._next_idle_monitor=0
        return h

    def test_automatic_heartbeat_reobserves_once_after_a_new_witnessed_death(self):
        h=self.paused_scene_then_respawn()
        h.tick(automatic=True)
        self.assertTrue(h.busy)
        result=self.finish(h)
        self.assertTrue(result.success,result)
        self.assertEqual(result.data["readback"]["resolution"],"superseded_life")
        self.assertIsNone(h.journal.state["pending"])

    def test_failed_automatic_respawn_readback_remains_paused_until_another_life(self):
        h=self.paused_scene_then_respawn()
        self.world.terrain={}
        h.tick(automatic=True)
        self.assertFalse(self.finish(h).success)
        self.world.terrain={(-1,66,9):"grass_block"}
        h._next_idle_monitor=0
        h.tick(automatic=True)
        self.assertFalse(h.busy)
        self.assertTrue(h.paused)

    def test_explicit_pause_never_releases_after_a_death(self):
        h=self.paused_scene_then_respawn()
        h.pause()
        h.tick(automatic=True)
        self.assertFalse(h.busy)
        self.assertEqual(h.pause_reason,"user_requested")
        self.assertIsNotNone(h.journal.state["pending"])
        h.close()
        restarted=self.harness()
        restarted.tick(automatic=True)
        self.assertFalse(restarted.busy)
        self.assertEqual(restarted.pause_reason,"user_requested")
        self.assertTrue(restarted.resume())
        self.assertNotIn("foliage_manually_paused",self.journal.state["pending"])

    def test_finalizer_matches_direct_foliage_with_checked_source_and_consumes_no_items(self):
        direct = Decision("clear_foliage", {"pos":[6,72,2],"expected_name":"oak_leaves"}, "Clear a visible leaf", "shelter")
        fail = SkillResult(False, "Unconfirmed", {"skill":"clear_foliage","pos_before":[7.7,72,2.3],
            "executed":True,"action_state":"unknown"}, "dig_unconfirmed")
        binding=foliage_intent(asdict(direct),asdict(fail),self.world.status,4,self.world.snapshot()["footing"])
        self.assertIsNotNone(binding)
        proof,reason=confirm_foliage(self.world,binding,4,threading.Event())
        self.assertIsNone(reason)
        fixed=resolved_result(fail,proof)
        self.assertTrue(fixed.success)
        self.assertTrue(fixed.data["block_removed"])
        self.assertNotIn("gained",fixed.data)
