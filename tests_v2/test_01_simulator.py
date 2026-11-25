"""Phase 0 verification: VirtualMcApiServer faithful upstream quirks & loopback HTTP."""

from __future__ import annotations

import unittest
import requests

from tests_v2.sim.scenarios import (
    scenario_full_nether_speedrun,
    scenario_v30_deep_pit_and_canopy,
    scenario_v31_64m_lava_portal,
    scenario_v31_sloped_water_bank,
)
from tests_v2.sim.virtual_mc_api import VirtualMcApiServer, VirtualMcHttpServer


class TestVirtualMcApiServer(unittest.TestCase):
    def test_find_blocks_skips_air_and_non_exposed_and_formats_untyped(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        # Build 3x3x3 solid stone cube centered at (5, 64, 0)
        sim.fill(4, 63, -1, 6, 65, 1, "stone")
        # Replace buried center with iron_ore (not exposed!)
        sim.set_block(5, 64, 0, "iron_ore")
        # Place exposed iron_ore at (4, 64, 0)
        sim.set_block(4, 64, 0, "iron_ore")

        res = sim.handle("POST", "find_blocks", {"types": ["iron_ore", "air"], "radius": 16})
        self.assertTrue(res["ok"])
        blocks = res["result"]["blocks"]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["pos"], [4, 64, 0])

        untyped = sim.handle("POST", "find_blocks", {"radius": 16})
        self.assertTrue(untyped["ok"])
        self.assertIsInstance(untyped["result"]["blocks"], dict)
        self.assertIn("iron_ore", untyped["result"]["blocks"])
        self.assertEqual(untyped["result"]["blocks"]["iron_ore"]["count"], 1)

    def test_bucket_pickup_enforces_y_plus_085_ray_occlusion(self) -> None:
        sim = scenario_v31_sloped_water_bank()
        # Stand at (2.5, 64, 0.5) within bucket reach (2.5m), behind grass occluder at (3, 64, 0)
        sim.pos = [2.5, 64.0, 0.5]
        res = sim.handle("POST", "interact", {"pos": [4, 62, 0], "item": "bucket"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "target_occluded")
        self.assertEqual(res["error"]["blocker"], [3, 64, 0])

        # Clear occluder and move to bank edge -> succeeds
        dig_res = sim.handle("POST", "dig", {"x": 3, "y": 64, "z": 0})
        self.assertTrue(dig_res["ok"])
        sim.pos = [3.5, 64.0, 0.5]
        res2 = sim.handle("POST", "interact", {"pos": [4, 62, 0], "item": "bucket"})
        self.assertTrue(res2["ok"])
        self.assertEqual(sim.inventory_counts.get("water_bucket"), 1)

    def test_move_to_has_no_32m_artificial_error_and_respects_walls(self) -> None:
        sim = scenario_v31_64m_lava_portal()
        # Blocked by leaf wall at x=25
        blocked = sim.handle("POST", "move_to", {"x": 50, "z": 0, "range": 2})
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"]["code"], "path_not_found")
        # Clear a 1x2 gap at (25, 64..65, 0) -> 50-block move_to succeeds cleanly!
        sim.set_block(25, 64, 0, "air")
        sim.set_block(25, 65, 0, "air")
        ok = sim.handle("POST", "move_to", {"x": 50, "z": 0, "range": 2})
        self.assertTrue(ok["ok"])
        self.assertGreaterEqual(sim.pos[0], 48.0)

    def test_portal_cast_and_ignite_and_enter_nether(self) -> None:
        sim = VirtualMcApiServer(spawn_pos=(0.5, 64.0, 0.5))
        sim.fill(-4, 63, -4, 6, 63, 4, "stone")
        # Build 4x5 obsidian portal frame at x=3..4 (interior), y=64..66, z=0
        for x in (3, 4):
            sim.set_block(x, 63, 0, "obsidian")
            sim.set_block(x, 67, 0, "obsidian")
        for y in (64, 65, 66):
            sim.set_block(2, y, 0, "obsidian")
            sim.set_block(5, y, 0, "obsidian")
        sim.give("flint_and_steel", 1)
        sim.pos = [3.5, 64.0, -1.5]
        lit = sim.handle("POST", "interact", {"pos": [3, 63, 0], "item": "flint_and_steel"})
        self.assertTrue(lit["ok"])
        self.assertTrue(lit["result"]["portal"])
        self.assertEqual(sim.block_name(3, 64, 0), "nether_portal")
        # Step inside portal -> dimension switches to the_nether
        sim.pos = [3.5, 64.0, 0.5]
        st = sim.handle("GET", "status")
        self.assertEqual(st["result"]["dimension"], "the_nether")

    def test_http_server_loopback_roundtrip(self) -> None:
        sim = scenario_full_nether_speedrun()
        with VirtualMcHttpServer(sim) as srv:
            r = requests.get(f"{srv.base_url}/status", timeout=5)
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["result"]["pos"], [0.5, 64.0, 0.5])

    def test_pit_and_canopy_fixtures(self) -> None:
        pit, canopy = scenario_v30_deep_pit_and_canopy()
        self.assertEqual(pit.pos[1], 43.0)
        self.assertEqual(canopy.pos[1], 78.0)
        self.assertEqual(len(canopy.entities), 1)


if __name__ == "__main__":
    unittest.main()
