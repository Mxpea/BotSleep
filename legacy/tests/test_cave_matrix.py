"""Isolated optional live cave exploration matrix test (requires BOTSLEEP_RUN_LIVE_TESTS=1 and RCON)."""

import os
import time
import unittest

LIVE_ENABLED = os.environ.get("BOTSLEEP_RUN_LIVE_TESTS") == "1"


@unittest.skipUnless(LIVE_ENABLED, "Set BOTSLEEP_RUN_LIVE_TESTS=1 with dedicated server & RCON to run live cave matrix.")
class LiveCaveMatrixTests(unittest.TestCase):
    """Live server verification using dedicated RCON fill chambers (doMobSpawning false)."""

    @staticmethod
    def _wait_ready(client):
        for _ in range(30):
            if client.get_status().get("result", {}).get("ready"):
                return
            time.sleep(0.4)

    def test_live_workstation_mineshaft_target_mining_and_rest(self):
        from core.config import load_config
        from core.client import MinecraftAgentClient
        from mcrcon import MCRcon
        from skills.mining import MineShaftSkill

        cfg = load_config(os.environ.get("BOTSLEEP_CONFIG", "config.yaml"))
        mc, ag, rc = cfg["mc_api"], cfg["agent"], cfg.get("rcon") or {}
        if not rc.get("enabled"):
            self.skipTest("RCON not enabled in configuration")

        client = MinecraftAgentClient(base_url=mc["base_url"], username=ag["name"], password=ag.get("password", ""))
        client.login()
        try:
            self._wait_ready(client)
            bx, by, bz = 210, 72, -120
            bot = ag["name"]
            with MCRcon(rc["host"], rc["password"], port=int(rc.get("port", 25575))) as rcon:
                rcon.command("gamerule doMobSpawning false")
                rcon.command("time set day")
                rcon.command(f"tp {bot} {bx} {by} {bz - 2}")
                time.sleep(0.8)
                # Solid stone subterranean slab + open flat surface above
                rcon.command(f"fill {bx - 6} {by - 9} {bz - 5} {bx + 8} {by - 1} {bz + 16} stone")
                rcon.command(f"fill {bx - 6} {by} {bz - 5} {bx + 8} {by + 8} {bz + 16} air")
                # Mineshaft gallery breaching directly across centerline at dz=10 (x=0..3, z=10..12, y=-5..-3)
                rcon.command(f"fill {bx} {by - 5} {bz + 10} {bx + 3} {by - 3} {bz + 12} air")
                rcon.command(f"setblock {bx + 1} {by - 6} {bz + 10} oak_planks")
                rcon.command(f"setblock {bx + 1} {by - 5} {bz + 10} rail")
                rcon.command(f"setblock {bx + 2} {by - 5} {bz + 10} oak_fence")
                rcon.command(f"setblock {bx + 2} {by - 6} {bz + 11} oak_planks")
                # Target iron_ore (head/foot accessible) & unlisted copper_ore on mineshaft wall
                rcon.command(f"setblock {bx + 4} {by - 5} {bz + 11} iron_ore")
                rcon.command(f"setblock {bx + 4} {by - 4} {bz + 11} air")
                rcon.command(f"setblock {bx + 4} {by - 4} {bz + 10} copper_ore")

                rcon.command(f"clear {bot}")
                rcon.command(f"give {bot} iron_pickaxe 1")
                rcon.command(f"give {bot} white_bed 1")
                rcon.command(f"give {bot} crafting_table 1")
                rcon.command(f"give {bot} furnace 1")
                rcon.command(f"give {bot} chest 2")
                rcon.command(f"give {bot} torch 16")
                rcon.command(f"give {bot} coal 4")
                rcon.command(f"give {bot} cobblestone 32")
            time.sleep(1.0)

            skill = MineShaftSkill(client)
            res = skill.build_mine_shaft(
                origin=[bx, by, bz],
                orientation="south",
                branch_length=4,
                place_workstation=True,
                targets={"iron": {"count": 4, "min_tool": "stone_pickaxe"}},
            )
            self.assertTrue(res.success, res)
            self.assertEqual(res.data["stop_reason"], "branch_done")
            self.assertTrue(res.data["caves"] and res.data["caves"][0]["kind"] == "mineshaft")
            by_pos = {tuple(o["pos"]): o["status"] for o in res.data["ores"]}
            self.assertEqual(by_pos.get((bx + 4, by - 5, bz + 11)), "mined")
            self.assertEqual(by_pos.get((bx + 4, by - 4, bz + 10)), "seen")
            self.assertGreaterEqual(res.data["torches_placed"], 1)

            # Verify underground workstation rest cycle stores excess cobblestone and starts iron smelt
            rest = skill.rest_at_workstation(
                res.data["workstation_slots"],
                targets={"iron": {"count": 4, "min_tool": "stone_pickaxe"}},
            )
            self.assertTrue(rest["actions"], f"Expected workstation rest actions, got {rest}")
        finally:
            client.logout()

    def test_live_unsafe_caves_seal_and_rotate_to_safe_branch(self):
        from core.config import load_config
        from core.client import MinecraftAgentClient
        from mcrcon import MCRcon
        from skills.mining import MineShaftSkill

        cfg = load_config(os.environ.get("BOTSLEEP_CONFIG", "config.yaml"))
        mc, ag, rc = cfg["mc_api"], cfg["agent"], cfg.get("rcon") or {}
        if not rc.get("enabled"):
            self.skipTest("RCON not enabled in configuration")

        client = MinecraftAgentClient(base_url=mc["base_url"], username=ag["name"], password=ag.get("password", ""))
        client.login()
        try:
            self._wait_ready(client)
            bx, by, bz = 250, 72, -120
            bot = ag["name"]
            with MCRcon(rc["host"], rc["password"], port=int(rc.get("port", 25575))) as rcon:
                rcon.command("gamerule doMobSpawning false")
                rcon.command(f"tp {bot} {bx} {by} {bz - 2}")
                time.sleep(0.8)
                rcon.command(f"fill {bx - 12} {by - 10} {bz - 5} {bx + 8} {by - 1} {bz + 18} stone")
                rcon.command(f"fill {bx - 12} {by} {bz - 5} {bx + 8} {by + 10} {bz + 18} air")
                # Heading 0 (dz=9+): air tunnel to sunken water pool >=3 water at dz=14 (recessed y=by-6 so it doesn't flood up to dz=10)
                rcon.command(f"fill {bx} {by - 5} {bz + 10} {bx} {by - 4} {bz + 14} air")
                rcon.command(f"fill {bx - 1} {by - 5} {bz + 14} {bx + 1} {by - 4} {bz + 14} air")
                rcon.command(f"fill {bx - 1} {by - 6} {bz + 14} {bx + 1} {by - 6} {bz + 14} water")
                # Heading 1 ((0,-5,5) -> -x): tall cave height=10 (breaks ceiling slightly below surface or height=10 stone chamber)
                rcon.command(f"fill {bx - 3} {by - 5} {bz + 5} {bx - 2} {by + 5} {bz + 5} air")
                # Heading 2 ((0,-5,8) -> -x): solid stone! Safe branch finishes cleanly.

                rcon.command(f"clear {bot}")
                rcon.command(f"give {bot} iron_pickaxe 1")
                rcon.command(f"give {bot} torch 16")
                rcon.command(f"give {bot} cobblestone 32")
            time.sleep(1.0)

            res = MineShaftSkill(client).build_mine_shaft(
                origin=[bx, by, bz],
                orientation="south",
                branch_length=4,
            )
            self.assertTrue(res.success, res)
            self.assertEqual(res.data["stop_reason"], "branch_done")
            caves = res.data["caves"]
            self.assertEqual([c["kind"] for c in caves], ["water_cave", "tall_cave"])
            # Real cobblestone seals placed at Heading 0 and Heading 1 openings
            b0 = client.get_block(bx, by - 5, bz + 10).get("result", {}).get("name")
            b1 = client.get_block(bx - 2, by - 5, bz + 5).get("result", {}).get("name")
            self.assertEqual(b0, "cobblestone")
            self.assertEqual(b1, "cobblestone")
            self.assertEqual(res.data["mining_progress"]["heading_index"], 2)
        finally:
            client.logout()


if __name__ == "__main__":
    unittest.main()
