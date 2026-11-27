"""Tests for L4 planner/competition wiring of outpost, loot, Sneak 100, and deep-mining diamond gates."""

from __future__ import annotations

import json
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from v2.bt.blackboard import Blackboard
from v2.planner import DepsPlanner, HorizonSelector, IntentCompiler
from v2.tasks import TaskCatalogue
from v2.tasks.competition import CompetitionManager


def _make_stub_outpost_and_loot_modules():
    outpost_mod = types.ModuleType("v2.tasks.outpost")
    loot_mod = types.ModuleType("v2.tasks.loot")

    class _StubEstablishOutpostTask:
        def __init__(self, ctx, catalogue, *, mode: str = "establish") -> None:
            self.ctx = ctx
            self.catalogue = catalogue
            self.mode = mode

    class _StubNetherPortalShelterTask:
        def __init__(self, ctx, catalogue) -> None:
            self.ctx = ctx
            self.catalogue = catalogue

    class _StubAvoidVibrationTask:
        def __init__(self, ctx, *, radius: int = 32) -> None:
            self.ctx = ctx
            self.radius = radius

    class _StubLootChestTask:
        def __init__(self, ctx, *, radius: int = 32, max_high_risk_trips: int = 3) -> None:
            self.ctx = ctx
            self.radius = radius
            self.max_high_risk_trips = max_high_risk_trips

    outpost_mod.EstablishOutpostTask = _StubEstablishOutpostTask  # type: ignore[attr-defined]
    outpost_mod.NetherPortalShelterTask = _StubNetherPortalShelterTask  # type: ignore[attr-defined]
    loot_mod.AvoidVibrationTask = _StubAvoidVibrationTask  # type: ignore[attr-defined]
    loot_mod.LootChestTask = _StubLootChestTask  # type: ignore[attr-defined]
    return outpost_mod, loot_mod


class TestPlannerOutpostLootWiring(unittest.TestCase):
    def test_tool_schemas_include_new_goals_and_stay_within_byte_budgets(self) -> None:
        cat = TaskCatalogue()
        ctx = MagicMock()
        compiler = IntentCompiler(ctx, cat, objective="daily")

        for needs_ai in (False, True):
            tools = compiler.tool_schemas(needs_ai=needs_ai)
            payload_bytes = len(json.dumps(tools, ensure_ascii=False).encode("utf-8"))
            self.assertLessEqual(payload_bytes, 1200, f"needs_ai={needs_ai} exceeded 1200B: {payload_bytes}")
            run_goal = next(t for t in tools if t["function"]["name"] == "run_goal")
            enum = run_goal["function"]["parameters"]["properties"]["goal"]["enum"]
            for expected in ("loot_chest", "setup_outpost", "avoid_vibration"):
                self.assertIn(expected, enum)

        planner = DepsPlanner(None, compiler, HorizonSelector(cat, objective="daily"))
        bb = Blackboard()
        bb.status = {"dimension": "overworld", "pos": [0.5, 64.0, 0.5], "health": 20.0, "food": 20}
        msgs, tools = planner.build_request_payload(bb, needs_ai=True)
        deps_bytes = len(json.dumps({"messages": msgs, "tools": tools}, ensure_ascii=False).encode("utf-8"))
        self.assertLessEqual(deps_bytes, 6600)

    def test_compile_tool_call_lazy_compiles_outpost_and_loot_goals(self) -> None:
        cat = TaskCatalogue()
        ctx = MagicMock()
        compiler = IntentCompiler(ctx, cat, objective="daily")
        bb = Blackboard()

        outpost_mod, loot_mod = _make_stub_outpost_and_loot_modules()
        with patch.dict(sys.modules, {"v2.tasks.outpost": outpost_mod, "v2.tasks.loot": loot_mod}):
            t_loot, r_loot = compiler.compile_tool_call("run_goal", {"goal": "loot_chest"}, bb)
            self.assertTrue(r_loot["ok"])
            self.assertEqual(r_loot["mounted"], "loot_chest")
            self.assertIsInstance(t_loot, loot_mod.LootChestTask)
            self.assertIs(t_loot.ctx, ctx)

            t_avoid, r_avoid = compiler.compile_tool_call("run_goal", {"goal": "avoid_vibration"}, bb)
            self.assertTrue(r_avoid["ok"])
            self.assertEqual(r_avoid["mounted"], "avoid_vibration")
            self.assertIsInstance(t_avoid, loot_mod.AvoidVibrationTask)
            self.assertIs(t_avoid.ctx, ctx)

            t_shelter, r_shelter = compiler.compile_tool_call(
                "run_goal", {"goal": "setup_outpost", "item": "nether_shelter"}, bb
            )
            self.assertTrue(r_shelter["ok"])
            self.assertEqual(r_shelter["mounted"], "nether_portal_shelter")
            self.assertIsInstance(t_shelter, outpost_mod.NetherPortalShelterTask)
            self.assertIs(t_shelter.ctx, ctx)
            self.assertIs(t_shelter.catalogue, cat)

            for raw_item, expected_mode in (
                ("", "establish"),
                ("unknown_mode", "establish"),
                ("establish", "establish"),
                ("stash", "stash"),
                ("withdraw", "withdraw"),
            ):
                t_outpost, r_outpost = compiler.compile_tool_call(
                    "run_goal", {"goal": "setup_outpost", "item": raw_item}, bb
                )
                self.assertTrue(r_outpost["ok"])
                self.assertEqual(r_outpost["mounted"], f"outpost:{expected_mode}")
                self.assertIsInstance(t_outpost, outpost_mod.EstablishOutpostTask)
                self.assertIs(t_outpost.ctx, ctx)
                self.assertIs(t_outpost.catalogue, cat)
                self.assertEqual(t_outpost.mode, expected_mode)

    def test_avoid_vibration_catalogue_route_competition_manager_and_cost(self) -> None:
        cat = TaskCatalogue()
        self.assertEqual(
            cat.advancement_route("minecraft:adventure/avoid_vibration"),
            ("avoid_vibration", "sculk_sensor"),
        )

        # Conditional advancement: not executable unless overridden
        comp_default = CompetitionManager(cat)
        self.assertFalse(comp_default.is_executable("minecraft:adventure/avoid_vibration"))

        comp = CompetitionManager(cat, eligibility_overrides={"minecraft:adventure/avoid_vibration": True})
        self.assertTrue(comp.is_executable("minecraft:adventure/avoid_vibration"))
        self.assertIn(
            "minecraft:adventure/avoid_vibration",
            [e.id for e in comp.selectable_tasks()],
        )

        # Competition HorizonSelector uses est=4 + difficulty penalty (4 + 1*40 = 44)
        hs = HorizonSelector(cat, objective="competition", competition=comp)
        bb = Blackboard()
        bb.status = {"dimension": "overworld", "pos": [0.5, 64.0, 0.5]}
        cands = hs.select_top(bb, limit=40)
        sneak_cand = next(c for c in cands if c.item == "minecraft:adventure/avoid_vibration")
        self.assertEqual(sneak_cand.est_steps, 44)
        self.assertEqual(sneak_cand.points, 15)

        ctx = MagicMock()
        _, loot_mod = _make_stub_outpost_and_loot_modules()
        with patch.dict(sys.modules, {"v2.tasks.loot": loot_mod}):
            task = comp.build_task(ctx, "minecraft:adventure/avoid_vibration")
            self.assertIsInstance(task, loot_mod.AvoidVibrationTask)
            self.assertIs(task.ctx, ctx)

    def test_horizon_selector_surfaces_sneak_loot_nether_and_deep_diamond_respecting_pins(self) -> None:
        cat = TaskCatalogue()
        hs = HorizonSelector(cat, objective="daily")

        # 1) SNEAK_100 + LOOT_CHEST surface in overworld without displacing CORPSE_RECOVERY or PORTAL_GATE_READY
        bb = Blackboard()
        bb.status = {"dimension": "overworld", "pos": [0.5, 64.0, 0.5], "time_of_day": 1000, "food": 20, "health": 20.0}
        bb.inventory_counts = {"stone_pickaxe": 1, "stone_sword": 1}
        bb.set("nearby_sculk_sensor", True, writer="test")
        bb.set("nearby_loot_chest", True, writer="test")

        top = hs.select_top(bb, limit=5)
        self.assertEqual(top[0].priority_tag, "SNEAK_100")
        self.assertEqual((top[0].goal, top[0].item, top[0].est_steps), ("avoid_vibration", "sculk_sensor", 2))
        self.assertEqual(top[1].priority_tag, "LOOT_CHEST")
        self.assertEqual((top[1].goal, top[1].item, top[1].est_steps), ("loot_chest", "chest", 3))

        # Completing avoid_vibration clears SNEAK_100
        bb.set("avoid_vibration_done", True, writer="test")
        tags_after = [c.priority_tag for c in hs.select_top(bb, limit=5)]
        self.assertNotIn("SNEAK_100", tags_after)
        self.assertEqual(tags_after[0], "LOOT_CHEST")

        # PORTAL_GATE_READY stays #1 ahead of SNEAK_100 & LOOT_CHEST; CORPSE_RECOVERY stays #1 ahead of PORTAL_GATE_READY
        bb.set("avoid_vibration_done", False, writer="test")
        bb.inventory_counts = {"iron_pickaxe": 1, "oak_planks": 8}
        top_portal = hs.select_top(bb, limit=5)
        self.assertEqual(top_portal[0].priority_tag, "PORTAL_GATE_READY")
        self.assertEqual(top_portal[1].priority_tag, "SNEAK_100")
        self.assertEqual(top_portal[2].priority_tag, "LOOT_CHEST")

        bb.set("active_corpse_drop", {"id": "c1", "dim": "overworld", "pos": [5.0, 64.0, 5.0], "loaded_elapsed": 10.0}, writer="test")
        top_corpse = hs.select_top(bb, limit=5)
        self.assertEqual(top_corpse[0].priority_tag, "CORPSE_RECOVERY")
        self.assertEqual(top_corpse[1].priority_tag, "PORTAL_GATE_READY")
        self.assertNotIn("LOOT_CHEST", [c.priority_tag for c in top_corpse])

        # 2) NETHER_OUTPOST surfaces in the_nether unless sheltered or corpse active
        bb_nether = Blackboard()
        bb_nether.status = {"dimension": "the_nether", "pos": [0.5, 64.0, 0.5], "food": 20, "health": 20.0}
        bb_nether.inventory_counts = {"iron_pickaxe": 1, "stone_sword": 1}
        top_nether = hs.select_top(bb_nether, limit=5)
        nether_outpost = next(c for c in top_nether if c.priority_tag == "NETHER_OUTPOST")
        self.assertEqual((nether_outpost.goal, nether_outpost.item, nether_outpost.est_steps), ("setup_outpost", "nether_shelter", 3))

        bb_nether.set("nether_portal_sheltered", True, writer="test")
        self.assertNotIn("NETHER_OUTPOST", [c.priority_tag for c in hs.select_top(bb_nether, limit=5)])

        bb_nether.set("nether_portal_sheltered", False, writer="test")
        bb_nether.set("active_corpse_drop", {"id": "c2", "dim": "the_nether", "pos": [2.0, 64.0, 2.0], "loaded_elapsed": 5.0}, writer="test")
        top_nether_corpse = hs.select_top(bb_nether, limit=5)
        self.assertEqual(top_nether_corpse[0].priority_tag, "CORPSE_RECOVERY")
        self.assertNotIn("NETHER_OUTPOST", [c.priority_tag for c in top_nether_corpse])

        # 3) Deep-mining kit unlocks diamond milestone even above y=16 without iron_chestplate
        bb_deep = Blackboard()
        bb_deep.status = {"dimension": "overworld", "pos": [0.5, 64.0, 0.5], "time_of_day": 1000, "food": 20, "health": 14.0}
        bb_deep.inventory_counts = {
            "iron_pickaxe": 1,
            "water_bucket": 1,
            "cobblestone": 6,
            "cobbled_deepslate": 10,
            "stone_sword": 1,
        }
        deep_items = [(c.goal, c.item, c.count) for c in hs.select_top(bb_deep, limit=10)]
        self.assertIn(("mine", "diamond", 3), deep_items)

        # Low health (< 14.0) or insufficient stone/deepslate (< 16) blocks deep diamond unlock at y=64
        bb_deep.status["health"] = 13.5
        self.assertNotIn("diamond", [c.item for c in hs.select_top(bb_deep, limit=10)])

        bb_deep.status["health"] = 18.0
        bb_deep.inventory_counts["cobbled_deepslate"] = 9  # 6 + 9 = 15 < 16
        self.assertNotIn("diamond", [c.item for c in hs.select_top(bb_deep, limit=10)])


if __name__ == "__main__":
    unittest.main()
