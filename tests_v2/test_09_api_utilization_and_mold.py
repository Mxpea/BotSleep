"""Round 2 regression tests: full mc-agent-api surface utilization, place_near_player fallback, and portal mold safety."""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer
from v2.bt import BTRuntime
from v2.bt.blackboard import Blackboard
from v2.gateway import VirtualWorldGateway
from v2.kernel import APIError, MinecraftAgentClient, PendingTransactionJournal, UnifiedWorldDB
from v2.primitives import DigCollectPrimitive, PlaceBuildPrimitive, PrimitiveContext
from v2.tasks import EnterNetherTask, TaskCatalogue


def _sync_bb(bb: Blackboard, sim: VirtualMcApiServer) -> None:
    bb.begin_tick()
    bb.status = {
        "pos": list(sim.pos),
        "dimension": sim.dimension,
        "health": sim.health,
        "food": sim.food,
        "oxygen": sim.oxygen,
    }
    bb.inventory_counts = dict(sim.inventory_counts)


class TestApiUtilizationAndMold(unittest.TestCase):
    def test_client_api_utilization_payloads(self) -> None:
        client = MinecraftAgentClient("http://127.0.0.1:18900/api/v1", "TestBot", "pass")
        calls = []

        def fake_request(method: str, path: str, payload=None, timeout_seconds=None):
            calls.append((method, path, dict(payload or {})))
            return {"ok": True, "result": {}}

        try:
            with patch.object(client, "_request", side_effect=fake_request):
                client.place_block("crafting_table")
                client.control(ms=None, wait=False)
                client.control(ms=200, forward=True, hit=True, wait=False)
                client.use_item("bucket", target={"pos": [1, 64, 2]})
                client.shoot(entity_id=77)
                client.shoot(pos=[5.5, 65.0, -2.5], weapon="crossbow")
                client.dismount()
                client.window_view(block="chest")
                client.window_view(entity_id=9)
                client.window_act(pos=[3, 64, 4], ops=[{"action": "close"}])
                client.window_act(block="furnace", ops=[{"action": "put", "item": "coal", "to": "fuel", "count": 1}])

            self.assertEqual(calls[0], ("POST", "place", {"item": "crafting_table"}))
            self.assertEqual(calls[1][1], "control")
            self.assertEqual(calls[1][2]["wait"], False)
            self.assertNotIn("ms", calls[1][2])
            self.assertEqual(calls[2][1], "control")
            self.assertEqual(calls[2][2]["forward"], True)
            self.assertEqual(calls[2][2]["hit"], True)
            self.assertEqual(calls[2][2]["wait"], False)
            self.assertEqual(calls[3][1], "use_item")
            self.assertEqual(calls[3][2]["target"], {"pos": [1, 64, 2]})
            self.assertEqual(calls[4][1], "shoot")
            self.assertEqual(calls[4][2]["entity_id"], 77)
            self.assertEqual(calls[5][1], "shoot")
            self.assertEqual(calls[5][2]["weapon"], "crossbow")
            self.assertEqual(calls[5][2]["x"], 5.5)
            self.assertEqual(calls[6], ("POST", "dismount", {}))
            self.assertEqual(calls[7], ("POST", "window/view", {"target": {"block": "chest"}}))
            self.assertEqual(calls[8], ("POST", "window/view", {"target": {"entity_id": 9}}))
            self.assertEqual(calls[9][1], "window/act")
            self.assertEqual(calls[9][2]["target"], {"pos": [3, 64, 4]})
            self.assertEqual(calls[10][1], "window/act")
            self.assertEqual(calls[10][2]["target"], {"block": "furnace"})
        finally:
            client.close()

    def test_place_near_player_auto_place_and_no_support_fallback(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-4, 63, -4, 4, 63, 4, "stone")
        sim.give("crafting_table", 2)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "PlaceBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "PlaceBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                ctx = PrimitiveContext(gw, journal)
                placer = PlaceBuildPrimitive(ctx)

                bb1 = Blackboard()
                _sync_bb(bb1, sim)
                res1 = placer.place_near_player(bb1, "crafting_table")
                self.assertEqual(res1.status, "OK")
                self.assertEqual(bb1.writes_this_tick, 1)

                orig_place = client.place_block
                auto_calls = {"count": 0}

                def flaky_auto_place(item, x=None, y=None, z=None, *, stop_on=None):
                    if x is None and y is None and z is None:
                        auto_calls["count"] += 1
                        raise APIError("no_support", "no horizontal support near player")
                    return orig_place(item, x, y, z, stop_on=stop_on)

                res2 = None
                with patch.object(client, "place_block", side_effect=flaky_auto_place):
                    for _ in range(4):
                        bb2 = Blackboard()
                        _sync_bb(bb2, sim)
                        res2 = placer.place_near_player(bb2, "crafting_table")
                        self.assertIn(res2.status, ("OK", "PROGRESSED"))
                        if res2.status == "OK":
                            break
                self.assertIsNotNone(res2)
                self.assertEqual(res2.status, "OK")
                self.assertGreaterEqual(auto_calls["count"], 1)
                self.assertEqual(sim.inventory_counts.get("crafting_table", 0), 0)
            finally:
                client.close()
                db.close()

    def test_nether_portal_properties_lava_query_and_protected_mold_non_cannibalization(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-10, 60, -10, 20, 63, 12, "stone")
        # Flowing lava (level=1) closer at x=4 must be ignored in favor of source lava (level=0) at x=8..13
        sim.set_block(4, 63, 0, "lava", properties={"level": 1})
        for x in range(8, 14):
            for z in (-1, 0, 1):
                sim.set_block(x, 63, z, "lava", properties={"level": 0})
        sim.fill(-4, 63, 5, -2, 63, 7, "water")
        sim.give("iron_pickaxe", 1)
        sim.give("bucket", 1)
        sim.give("flint_and_steel", 1)
        # Start with partial cobblestone so AcquireItemTask("cobblestone") runs WHILE mold exists!
        sim.give("cobblestone", 6)

        with tempfile.TemporaryDirectory() as td, VirtualMcHttpServer(sim) as srv:
            db = UnifiedWorldDB.for_scope(td, srv.base_url, "MoldBot", "w1")
            client = MinecraftAgentClient(srv.base_url, "MoldBot", "pass")
            try:
                client.login()
                journal = PendingTransactionJournal(db)
                gw = VirtualWorldGateway(client, db=db)
                rt = BTRuntime(gw, journal, enable_auto_sleep=False)
                ctx = PrimitiveContext(gw, journal)
                cat = TaskCatalogue()
                task = EnterNetherTask(ctx, cat)
                dig = DigCollectPrimitive(ctx)

                # Verify gateway scan_blocks filters level=0 lava cleanly
                lava_sources = gw.scan_blocks("overworld", ["lava"], radius=16, properties={"level": 0})
                lava_coords = {tuple(r["pos"]) for r in lava_sources}
                self.assertNotIn((4, 63, 0), lava_coords)
                self.assertIn((8, 63, 0), lava_coords)

                rt.goal_mount.mount(task, rt.bb)
                saw_protected = False
                for _ in range(320):
                    rt.tick_once()
                    self.assertLessEqual(rt.bb.writes_this_tick, 1)
                    if rt.bb.get("protected_cells"):
                        saw_protected = True
                    if sim.dimension == "the_nether":
                        break

                self.assertTrue(saw_protected)
                self.assertEqual(sim.dimension, "the_nether")

                # Explicit direct verification: DigCollectPrimitive refuses to dig protected cobblestone
                sim.dimension = "overworld"
                sim.pos = [0.5, 64.0, 0.5]
                sim.set_block(1, 64, 0, "cobblestone")
                all_cobble = {(int(r["pos"][0]), int(r["pos"][1]), int(r["pos"][2])) for r in gw.scan_blocks("overworld", ["cobblestone"], radius=8)}
                all_cobble.add((1, 64, 0))
                bb_guard = Blackboard()
                _sync_bb(bb_guard, sim)
                bb_guard.set("protected_cells", all_cobble, writer="test")
                out = dig.dig_nearest_of_types(bb_guard, ["cobblestone"], radius=4)
                self.assertEqual(out.status, "FAILED_LOCAL")
                self.assertEqual(sim.block_name(1, 64, 0), "cobblestone")
            finally:
                client.close()
                db.close()
