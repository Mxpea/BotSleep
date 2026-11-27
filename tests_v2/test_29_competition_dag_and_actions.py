"""Tests for Competition DAG Graph, PPM Scheduler, GatherMobDropTask, 6 Behavioral Plugins, Recon & Telemetry."""

from __future__ import annotations

import tempfile
import time
import unittest

from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt.blackboard import Blackboard
from v2.bt.observe_node import SyncBlackboardNode
from v2.bt.runtime import BTRuntime
from v2.kernel.client import MinecraftAgentClient
from v2.kernel.journal import PendingTransactionJournal
from v2.kernel.storage import UnifiedWorldDB
from v2.planner.competition_scheduler import CompetitionScheduler
from v2.planner.deps_planner import DepsPlanner
from v2.planner.horizon_selector import HorizonSelector
from v2.planner.intent_compiler import IntentCompiler
from v2.primitives.base import PrimitiveContext
from v2.tasks.catalogue import TaskCatalogue
from v2.tasks.competition import CompetitionManager
from v2.tasks.competition.graph import ADVANCEMENT_GRAPH, validate_graph
from v2.tasks.mob_gather import GatherMobDropTask


def _make_runtime_ctx(sim: VirtualMcApiServer, td: str, url: str, name: str = "CompTestBot"):
    db = UnifiedWorldDB.for_scope(td, url, name, "w1")
    journal = PendingTransactionJournal(db)
    client = MinecraftAgentClient(url, name, "pass")
    client.login()
    from v2.gateway.world_proxy import VirtualWorldGateway

    gw = VirtualWorldGateway(client, db=db)
    ctx = PrimitiveContext(gw, journal)
    return db, journal, client, gw, ctx


class TestCompetitionDagAndActions(unittest.TestCase):
    def test_graph_validation_and_catalogue_sync(self) -> None:
        validate_graph()
        cat = TaskCatalogue()
        self.assertGreaterEqual(len(ADVANCEMENT_GRAPH), 35)
        self.assertEqual(cat.advancement_route("minecraft:story/obtain_armor"), ("acquire", "iron_boots"))
        self.assertEqual(cat.advancement_route("minecraft:story/shiny_gear"), ("acquire", "diamond_boots"))
        self.assertEqual(cat.advancement_route("minecraft:husbandry/breed_an_animal"), ("interact_entity", "breed_an_animal"))
        self.assertEqual(cat.advancement_route("minecraft:adventure/trade"), ("window_action", "trade"))
        self.assertTrue(cat.can_produce("white_wool"))
        self.assertTrue(cat.can_produce("wheat_seeds"))
        self.assertTrue(cat.can_produce("lodestone"))
        self.assertTrue(cat.can_produce("glow_ink_sac"))

    def test_ppm_scheduler_synergy_time_fit_opportunistic_and_decay(self) -> None:
        cat = TaskCatalogue()
        mgr = CompetitionManager(cat)
        sched = CompetitionScheduler(cat, mgr)

        # 1) Blank start prioritizes T0 unlockers/low-cost stone over diamond/obsidian
        bb = Blackboard()
        bb.status = {"dimension": "overworld", "pos": [0.5, 64.0, 0.5], "time_of_day": 1000, "food": 20}
        bb.inventory_counts = {}
        top = sched.select_top(bb, limit=8)
        top_ids = [c.item for c in top]
        self.assertTrue(any(aid in top_ids[:5] for aid in ("minecraft:story/mine_stone", "minecraft:story/upgrade_tools", "minecraft:husbandry/plant_seed")))
        self.assertNotIn("minecraft:story/shiny_gear", top_ids[:3])

        # 2) Opportunistic 24m trigger boosts trade / breed / deflect_arrow
        bb_opp = Blackboard()
        bb_opp.status = {"dimension": "overworld", "pos": [0.5, 64.0, 0.5], "time_of_day": 2000, "food": 20}
        bb_opp.inventory_counts = {"stone_pickaxe": 1, "shield": 1}
        bb_opp.entities = [
            {"id": 10, "name": "skeleton", "type": "hostile", "pos": [8.0, 64.0, 0.5]},
            {"id": 11, "name": "villager", "type": "passive", "pos": [5.0, 64.0, 0.5]},
        ]
        opp_top = sched.select_top(bb_opp, limit=6)
        opp_tags = {c.item: c.priority_tag for c in opp_top}
        self.assertIn("OPP_", opp_tags.get("minecraft:adventure/trade", "") + opp_tags.get("minecraft:story/deflect_arrow", ""))

        # 3) Time-fit endgame pruning: with 45s left, 8m shiny_gear plunges below quick plant_seed
        bb_end = Blackboard()
        bb_end.status = {"dimension": "overworld", "pos": [0.5, 64.0, 0.5], "time_of_day": 2000, "food": 20}
        bb_end.inventory_counts = {"iron_pickaxe": 1, "wheat_seeds": 1, "wooden_hoe": 1}
        bb_end.set("competition_remaining_seconds", 45.0, writer="test")
        end_ids = [c.item for c in HorizonSelector(cat, objective="competition", competition=mgr).select_top(bb_end, limit=8)]
        self.assertNotIn("minecraft:story/shiny_gear", end_ids[:4])

        # 4) Failure exponential backoff & decay
        mgr.select_task("minecraft:husbandry/fishy_business")
        exit_res = mgr.force_exit_task("timeout")
        self.assertEqual(exit_res["fails"], 1)
        self.assertGreaterEqual(exit_res["defer_seconds"], 300.0)
        self.assertNotIn("minecraft:husbandry/fishy_business", [e.id for e in mgr.selectable_tasks(now=time.time())])

    def test_gather_mob_drop_shears_and_cobweb(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-6, 63, -6, 6, 63, 6, "grass_block")
        sim.give("shears", 1)
        sim.add_entity(201, "sheep", [2.0, 64.0, 0.5], kind="passive", health=8.0)
        sim.set_block(2, 64, 2, "cobweb")

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db, _j, client, gw, ctx = _make_runtime_ctx(sim, td, srv.base_url, "MobBot")
            try:
                bb = Blackboard()
                obs = SyncBlackboardNode(gw)
                # Shear wool cleanly
                t_wool = GatherMobDropTask(ctx, "white_wool", 1, ("sheep",))
                for _ in range(6):
                    obs.tick(bb)
                    if t_wool.tick(bb) == "COMPLETED":
                        break
                self.assertGreaterEqual(bb.inventory_counts.get("white_wool", 0), 1)
                self.assertTrue(any(e["id"] == 201 for e in sim.entities))  # sheep still alive!

                # Cobweb harvest shortcut for string
                t_str = GatherMobDropTask(ctx, "string", 1, ("spider",))
                for _ in range(6):
                    obs.tick(bb)
                    res = t_str.tick(bb)
                    if res == "COMPLETED":
                        break
                # cobweb dug cleanly
                self.assertEqual(sim.block_name(2, 64, 2), "air")
            finally:
                client.close()
                db.close()

    def test_all_six_behavioral_advancement_plugins(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-16, 63, -16, 16, 63, 16, "grass_block")
        sim.set_block(4, 63, 0, "water", {"level": "0"})
        sim.give("wheat_seeds", 4)
        sim.give("wooden_hoe", 1)
        sim.give("fishing_rod", 1)
        sim.give("stick", 8)
        sim.give("bow", 1)
        sim.give("arrow", 4)
        sim.give("enchanting_table", 1)
        sim.give("lapis_lazuli", 2)
        sim.give("stone_pickaxe", 1)
        sim.give("iron_block", 4)
        sim.give("carved_pumpkin", 1)
        sim.add_entity(301, "chicken", [2.0, 64.0, 0.5], kind="passive")
        sim.add_entity(302, "chicken", [2.5, 64.0, 1.5], kind="passive")
        sim.add_entity(303, "villager", [-2.0, 64.0, 0.5], kind="passive")
        sim.add_entity(304, "zombie", [0.5, 64.0, 5.0], kind="hostile", health=8.0)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db, _j, client, gw, ctx = _make_runtime_ctx(sim, td, srv.base_url, "PluginBot")
            try:
                cat = TaskCatalogue()
                mgr = CompetitionManager(cat, eligibility_overrides={"minecraft:adventure/trade": True})
                bb = Blackboard()
                obs = SyncBlackboardNode(gw)

                for adv_id in (
                    "minecraft:husbandry/plant_seed",
                    "minecraft:husbandry/breed_an_animal",
                    "minecraft:husbandry/fishy_business",
                    "minecraft:adventure/shoot_arrow",
                    "minecraft:adventure/trade",
                    "minecraft:story/enchant_item",
                    "minecraft:adventure/summon_iron_golem",
                ):
                    task = mgr.build_task(ctx, adv_id)
                    self.assertIsNotNone(task, f"missing plugin for {adv_id}")
                    status = "RUNNING"
                    for _ in range(18):
                        obs.tick(bb)
                        bb.writes_this_tick = 0
                        status = task.tick(bb)
                        self.assertLessEqual(bb.writes_this_tick, 1)
                        if status in {"COMPLETED", "NEEDS_AI", "FAILED_LOCAL"}:
                            break
                    self.assertEqual(status, "COMPLETED", f"{adv_id} ended with {status} ({bb.last_failure_reason})")

                self.assertEqual(sim.inventory_counts.get("emerald", 0), 1)
                self.assertTrue(any(rec.get("name") == "carved_pumpkin" for rec in sim.blocks.values()))
            finally:
                client.close()
                db.close()

    def test_void_edge_guard_supervisor_timeout_recon_and_prompt_budget(self) -> None:
        # 1) HardSafety void-edge guard retreats from void column
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-2, 63, -2, 2, 63, 2, "grass_block")
        for y in range(58, 64):
            sim.set_block(0, y, 0, "void_air")
        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db, journal, client, gw, ctx = _make_runtime_ctx(sim, td, srv.base_url, "VoidBot")
            try:
                from v2.app import CompetitionSupervisor, update_island_recon

                rt = BTRuntime(gw, journal, mode="survival")
                rt.observe.tick(rt.bb)
                prefetched = gw.prefetch_blocks("overworld", [(0, y, 0) for y in range(58, 64)] + [(1, 63, 0), (1, 64, 0)])
                sim.add_entity(401, "villager", [3.0, 64.0, 0.5], kind="passive")
                rt.observe.tick(rt.bb)
                recon = update_island_recon(rt.bb, gw)
                self.assertIn("villager", recon)

                res_status = rt.tick_once()
                self.assertEqual(rt.bb.last_action_outcome, "safety_void_edge_guard", f"prefetched={[(k, v.name if v else None) for k, v in prefetched.items()]} fail={rt.bb.last_failure_reason}")
                self.assertEqual(res_status.value, "RUNNING")

                # 2) Timeout stoploss + DepsPlanner payload <= 6600 bytes
                cat = TaskCatalogue()
                mgr = CompetitionManager(cat, defer_seconds=120.0)
                mgr.select_task("minecraft:husbandry/fishy_business")
                sup = CompetitionSupervisor(mgr, start_mono=100.0)
                sup.step(rt, client, now_mono=100.0)
                out_t = sup.step(rt, client, now_mono=400.0)
                self.assertEqual(out_t, "timeout:minecraft:husbandry/fishy_business")
                self.assertIsNone(mgr.active_task_id)

                comp_c = IntentCompiler(ctx, cat, objective="competition", competition=mgr)
                hs_comp = HorizonSelector(cat, objective="competition", competition=mgr)
                planner = DepsPlanner(None, comp_c, hs_comp)
                msgs, tools = planner.build_request_payload(rt.bb)
                import json

                payload_bytes = len(json.dumps({"messages": msgs, "tools": tools}, ensure_ascii=False).encode("utf-8"))
                self.assertLessEqual(payload_bytes, 6600)
            finally:
                client.close()
                db.close()

    def test_multi_bot_runner_and_workbench_competition_summary(self) -> None:
        from scripts.multi_bot_runner import build_bot_config
        from scripts.debug_workbench import summarize_competition_scores
        from pathlib import Path

        cfg, _, _ = build_bot_config(
            {"agent": {}, "mc_api": {}, "survival": {"objective": "competition"}, "debug": {}},
            index=1,
            run_id="r_test",
            api_base_url="http://127.0.0.1:18900/api/v1",
            runtime_root=Path("runtime"),
            log_root=Path("logs"),
            max_runtime_seconds=7200,
            max_steps=16000,
        )
        self.assertEqual(cfg["survival"]["max_steps"], 10000)
        self.assertEqual(cfg["survival"]["competition"]["max_seconds"], 7200)
        self.assertEqual(cfg["survival"]["competition"]["max_steps"], 16000)

        comp = summarize_competition_scores(
            {"bots": [{"bot_id": "bot_1", "total_ticks": 120, "total_stuck_ticks": 6}]},
            {"bots": {"bot_1": {"player": "SleepBot1", "comp_timeouts": 1, "comp_selects": 3, "comp_scores": []}}},
            {"players": {"SleepBot1": {"advancements": [{"key": "minecraft:story/mine_stone", "ts_ms": 1000}], "deaths": [], "bare_hand_rock_breaks": []}}},
        )
        self.assertTrue(comp["has_scores"])
        self.assertEqual(comp["max_score"], 5)


if __name__ == "__main__":
    unittest.main()
