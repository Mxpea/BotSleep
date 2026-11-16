"""Offline pure-logic unit tests for core/cave_probe.py."""

import unittest

from core.cave_probe import (
    classify_cavity,
    flood_safe_cave_graph,
    has_exposed_air_face,
    new_exposed_face_positions,
    probe_cavity,
    reachable_exposed_ores,
    walkable_neighbors,
)
from core.mining_targets import parse_mining_targets


class _CubeWorld:
    """In-memory stone world with carve/set helpers."""

    def __init__(self, *, x_range=(-20, 21), y_range=(-20, 21), z_range=(-20, 21), default="stone"):
        self.blocks = {}
        self.default = default
        self.xr = x_range
        self.yr = y_range
        self.zr = z_range

    def carve(self, xs, ys, zs, name="air", props=None):
        for x in xs:
            for y in ys:
                for z in zs:
                    self.blocks[(x, y, z)] = {"name": name, "properties": dict(props or {})}

    def set_block(self, x, y, z, name, props=None):
        self.blocks[(x, y, z)] = {"name": name, "properties": dict(props or {})}

    def read(self, pos):
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        if key in self.blocks:
            return self.blocks[key]
        x, y, z = key
        if self.xr[0] <= x < self.xr[1] and self.yr[0] <= y < self.yr[1] and self.zr[0] <= z < self.zr[1]:
            return {"name": self.default, "properties": {}}
        return {"name": "stone", "properties": {}}


class CaveProbeTests(unittest.TestCase):
    def test_water_like_and_lava_and_spawner_rejected(self):
        # Water cave with >=3 water-like blocks (water + seagrass + waterlogged fence)
        w = _CubeWorld()
        w.carve(range(0, 4), range(0, 3), range(1, 5), "air")
        w.set_block(2, 0, 2, "water")
        w.set_block(2, 0, 3, "seagrass")
        w.set_block(3, 0, 2, "oak_fence", {"waterlogged": True})
        rep = probe_cavity([0, 0, 1], [0, 0, 0], w.read)
        cls = classify_cavity(rep, torch_count=8)
        self.assertGreaterEqual(rep["water_count"], 3)
        self.assertEqual(cls["decision"], "unsafe")
        self.assertEqual(cls["kind"], "water_cave")

        # Lava anywhere
        w_lava = _CubeWorld()
        w_lava.carve(range(0, 4), range(0, 3), range(1, 5), "air")
        w_lava.set_block(3, 0, 4, "flowing_lava")
        rep_l = probe_cavity([0, 0, 1], [0, 0, 0], w_lava.read)
        self.assertEqual(classify_cavity(rep_l, torch_count=8)["kind"], "lava")

        # Spawner anywhere
        w_sp = _CubeWorld()
        w_sp.carve(range(0, 4), range(0, 3), range(1, 5), "air")
        w_sp.set_block(2, 1, 3, "spawner")
        rep_s = probe_cavity([0, 0, 1], [0, 0, 0], w_sp.read)
        self.assertEqual(classify_cavity(rep_s, torch_count=8)["kind"], "spawner")

    def test_tall_cave_and_deep_drop_rejected(self):
        # Tall cave: height = 11 (>= 10)
        w_tall = _CubeWorld()
        w_tall.carve(range(0, 3), range(0, 11), range(1, 4), "air")
        rep_t = probe_cavity([0, 0, 1], [0, 0, 0], w_tall.read)
        self.assertGreaterEqual(rep_t["height"], 10)
        self.assertEqual(classify_cavity(rep_t, torch_count=8)["kind"], "tall_cave")

        # Deep drop: floor drops 5 blocks below entry_foot (y=0 -> floor air at y=-1..-5, solid at y=-6 -> foot y=-5 -> drop=5)
        w_drop = _CubeWorld()
        w_drop.carve(range(0, 3), range(-5, 2), range(1, 4), "air")
        rep_d = probe_cavity([0, 0, 1], [0, 0, 0], w_drop.read)
        self.assertEqual(rep_d["drop"], 5)
        self.assertEqual(classify_cavity(rep_d, torch_count=8)["kind"], "deep_drop")

    def test_wide_low_cave_above_160_cells_enters_by_height_and_drop(self):
        # Wide flat cavern: 12 x 6 x 12 open air (> 160 limit), height=6 (< 10), drop=2 (< 4)
        w = _CubeWorld()
        w.carve(range(-2, 10), range(-2, 4), range(1, 13), "air")
        w.carve([0], range(0, 2), [0], "air")
        rep = probe_cavity([0, 0, 1], [0, 0, 0], w.read)
        self.assertTrue(rep["hit_limit"])
        self.assertLess(rep["height"], 10)
        self.assertLess(rep["drop"], 4)
        self.assertEqual(classify_cavity(rep, torch_count=8)["decision"], "enter")
        self.assertEqual(classify_cavity(rep, torch_count=8)["kind"], "small")
        # Default policy allows entering safe caves even with 0 torches; explicit min_enter_torches=1 enforces torch gating
        self.assertEqual(classify_cavity(rep, torch_count=0)["decision"], "enter")
        self.assertEqual(classify_cavity(rep, torch_count=0, min_enter_torches=1)["decision"], "unsafe")
        self.assertEqual(classify_cavity(rep, torch_count=0, min_enter_torches=1)["kind"], "missing_torch")

    def test_mineshaft_and_small_bubble_classification(self):
        # Mineshaft corridor (planks + rails + fence >= 3) enters cleanly when lighted
        w_ms = _CubeWorld()
        w_ms.carve(range(-1, 3), range(0, 4), range(1, 8), "air")
        w_ms.set_block(0, -1, 2, "oak_planks")
        w_ms.set_block(0, 0, 2, "rail")
        w_ms.set_block(1, 0, 2, "oak_fence")
        w_ms.set_block(1, -1, 3, "oak_planks")
        rep_ms = probe_cavity([0, 0, 1], [0, 0, 0], w_ms.read)
        self.assertGreaterEqual(rep_ms["mineshaft_markers"], 3)
        self.assertEqual(classify_cavity(rep_ms, torch_count=4)["kind"], "mineshaft")
        self.assertEqual(classify_cavity(rep_ms, torch_count=0)["kind"], "mineshaft")
        self.assertEqual(classify_cavity(rep_ms, torch_count=0, min_enter_torches=1)["kind"], "missing_torch")

        # Small pocket (air <= 4)
        w_tiny = _CubeWorld()
        w_tiny.carve([0], [0, 1], [1, 2], "air")
        rep_t = probe_cavity([0, 0, 1], [0, 0, 0], w_tiny.read)
        self.assertEqual(rep_t["air_count"], 4)
        self.assertEqual(classify_cavity(rep_t, torch_count=0)["decision"], "small_bubble")

    def test_walkable_neighbors_filters_drops_webs_gaps_and_chest_minecarts(self):
        w = _CubeWorld()
        w.carve([0], [0, 1], [0], "air")
        w.set_block(0, -1, 0, "stone")
        # +x: flat solid floor with rail on top (valid!)
        w.carve([1], [0, 1], [0], "air")
        w.set_block(1, -1, 0, "oak_planks")
        w.set_block(1, 0, 0, "rail")
        # -x: cobweb blocking (invalid without sword/shears, valid with)
        w.carve([-1], [0, 1], [0], "air")
        w.set_block(-1, 0, 0, "cobweb")
        # +z: 2-block drop (invalid!)
        w.carve([0], [-2, -1, 0, 1], [1], "air")
        # -z: chest_minecart (invalid!)
        w.carve([0], [0, 1], [-1], "air")
        w.set_block(0, 0, -1, "chest_minecart")

        n_no_web = walkable_neighbors([0, 0, 0], w.read, allow_cobweb=False)
        self.assertEqual(n_no_web, [[1, 0, 0]])
        n_with_web = walkable_neighbors([0, 0, 0], w.read, allow_cobweb=True)
        self.assertEqual(sorted(n_with_web), [[-1, 0, 0], [1, 0, 0]])

    def test_reachable_exposed_ores_and_eight_face_slice(self):
        targets = parse_mining_targets({"iron": {"count": 10, "min_tool": "stone_pickaxe"}})
        w = _CubeWorld()
        w.carve(range(-1, 2), range(0, 2), range(0, 2), "air")
        # Exposed safe iron on side wall (within reach)
        w.set_block(2, 1, 0, "iron_ore")
        # Exposed copper (unlisted -> seen)
        w.set_block(-2, 1, 0, "copper_ore")
        # Exposed iron adjacent to lava -> skipped_hazard
        w.set_block(0, 2, 1, "deepslate_iron_ore")
        w.set_block(0, 3, 1, "lava")
        # Hidden buried diamond completely surrounded by stone (no air face -> excluded!)
        w.set_block(3, 1, 0, "diamond_ore")

        self.assertFalse(has_exposed_air_face([3, 1, 0], w.read))
        found = reachable_exposed_ores(
            [0, 0, 0],
            w.read,
            targets,
            harvested_counts={},
            best_tool_tier=2,
            max_dist=4.5,
        )
        by_pos = {tuple(row["pos"]): row for row in found}
        self.assertEqual(by_pos[(2, 1, 0)]["status"], "mined")
        self.assertEqual(by_pos[(-2, 1, 0)]["status"], "seen")
        self.assertEqual(by_pos[(0, 2, 1)]["status"], "skipped_hazard")
        self.assertNotIn((3, 1, 0), by_pos)

        faces = new_exposed_face_positions([0, 59, 9], [0, 0, 1])
        self.assertEqual(len(faces), 8)

    def test_flood_safe_cave_graph_finds_pillar_ledge_and_micro_dig_to_ores(self):
        targets = parse_mining_targets({"iron": {"count": 6, "min_tool": "stone_pickaxe"}})
        w = _CubeWorld()
        # Tall cavern room (y=0..11) with safe floor and lava pit separated by curb
        w.carve(range(0, 6), range(0, 12), range(0, 5), "air")
        w.set_block(4, -1, 4, "lava")
        # 2-block high obsidian shelf at x=3..4, y=2 (cannot be dug through; must pillar_up) with iron_ore at (5, 6, 2)
        w.set_block(3, 0, 2, "obsidian")
        w.set_block(3, 1, 2, "obsidian")
        w.set_block(4, 0, 2, "obsidian")
        w.set_block(4, 1, 2, "obsidian")
        w.set_block(5, 6, 2, "iron_ore")
        # Also test micro-dig through 1-block choke at (0, 0..1, -1) -> deep pocket with iron_ore at (0, 1, -6) (>4.5 away from z=0)
        w.carve([0], [0, 1], range(-5, -1), "air")
        w.set_block(0, 1, -6, "iron_ore")

        g = flood_safe_cave_graph(
            [1, 0, 2],
            w.read,
            max_nodes=160,
            max_radius=8,
            allow_pillar=True,
            max_pillar_height=2,
            allow_dig=True,
            max_dig_steps=1,
            targets=targets,
            harvested_counts={},
            best_tool_tier=2,
        )
        ore_positions = {tuple(row["ore_pos"]): row for row in g["ore_targets"]}
        self.assertIn((5, 6, 2), ore_positions)
        modes_upper = {s["mode"] for s in ore_positions[(5, 6, 2)]["path"]}
        self.assertIn("pillar", modes_upper)
        self.assertIn((0, 1, -6), ore_positions)
        modes_choke = {s["mode"] for s in ore_positions[(0, 1, -6)]["path"]}
        self.assertIn("dig", modes_choke)
        # Lava-adjacent border must be excluded from safe nodes when allow_bridge=False
        self.assertNotIn([4, 0, 4], g["nodes"])

    def test_flood_graph_bridges_lava_cushions_drop_and_prefers_cave_center(self):
        from core.cave_probe import lateral_air_clearance, reconstruct_flood_path

        targets = parse_mining_targets({"iron": {"count": 4, "min_tool": "stone_pickaxe"}})
        w = _CubeWorld()
        # Wide 3-wide corridor z=0..5 (x=-1..1), lava pool across z=3 (y=-1), 4-block drop at z=5 (floor y=-5) to iron_ore at (0,-3,6)
        w.carve(range(-1, 2), range(0, 4), range(0, 6), "air")
        for x in (-1, 0, 1):
            for z in (0, 1, 2, 4):
                w.set_block(x, -1, z, "stone")
            w.set_block(x, -1, 3, "lava")
        w.carve([0], range(-4, 0), [5], "air")
        w.set_block(0, -5, 5, "stone")
        w.set_block(0, -3, 6, "iron_ore")

        # Center cell [0,0,1] scores strictly higher than wall-hugging cell [-1,0,1]
        self.assertGreater(lateral_air_clearance([0, 0, 1], w.read), lateral_air_clearance([-1, 0, 1], w.read))

        g = flood_safe_cave_graph(
            [0, 0, 0],
            w.read,
            max_nodes=180,
            max_radius=10,
            allow_pillar=True,
            allow_bridge=True,
            allow_drop=True,
            targets=targets,
            harvested_counts={},
            best_tool_tier=2,
        )
        ore_positions = {tuple(row["ore_pos"]): row for row in g["ore_targets"]}
        self.assertIn((0, -3, 6), ore_positions)
        path = ore_positions[(0, -3, 6)]["path"]
        modes = [s["mode"] for s in path]
        self.assertIn("bridge", modes)
        self.assertIn("scaffold_drop", modes)
        # Path stays on centerline x=0 across the bridge rather than wall-hugging x=-1
        self.assertTrue(all(s["to"][0] == 0 for s in path))

    def test_flood_graph_steps_one_block_ledge_without_meaningless_pillar(self):
        targets = parse_mining_targets({"iron": {"count": 2, "min_tool": "stone_pickaxe"}})
        w = _CubeWorld()
        # Corridor at y=0 steps up 1 block onto y=1 stone ledge at x=2..4 -> iron_ore at (5, 2, 0)
        w.carve(range(0, 6), range(0, 5), [-1, 0, 1], "air")
        for x in range(2, 5):
            for z in (-1, 0, 1):
                w.set_block(x, 0, z, "stone")  # 1-block diggable stone shelf (free dy=+1 walk!)
        w.set_block(5, 2, 0, "iron_ore")

        g = flood_safe_cave_graph(
            [0, 0, 0],
            w.read,
            max_nodes=120,
            max_radius=8,
            allow_pillar=True,
            allow_dig=True,
            targets=targets,
            harvested_counts={},
            best_tool_tier=2,
        )
        ore_positions = {tuple(row["ore_pos"]): row for row in g["ore_targets"]}
        self.assertIn((5, 2, 0), ore_positions)
        modes = [s["mode"] for s in ore_positions[(5, 2, 0)]["path"]]
        # Must climb the 1-block ledge strictly via walk(dy=+1), with zero pillar or dig
        self.assertNotIn("pillar", modes)
        self.assertNotIn("dig", modes)

    def test_anchor_radius_preserves_inside_frontiers(self):
        w = _CubeWorld()
        # Start near east boundary of anchor [0,0,0] (anchor_radius=4):
        # tunnel continues out to x=12 AND branches west to an uncovered wing at x=-2..0, z=1..3
        w.carve(range(-3, 13), [0, 1], [0], "air")
        w.carve(range(-2, 1), [0, 1], range(0, 4), "air")
        visited = {(x, 0, 0) for x in range(1, 5)}
        covered = {(x, 0, 0) for x in range(1, 5)}

        g = flood_safe_cave_graph(
            [4, 0, 0],
            w.read,
            max_nodes=80,
            max_radius=12,
            visited_cells=visited,
            covered_cells=covered,
            anchor_pos=[0, 0, 0],
            anchor_radius=4,
        )
        # All expanded nodes and frontiers must strictly stay within anchor_radius=4 around [0, 0, 0]
        self.assertTrue(all(abs(n[0]) <= 4 and abs(n[2]) <= 4 for n in g["nodes"]))
        frontier_coords = {tuple(f["pos"]) for f in g["frontiers"]}
        self.assertTrue(any(fc[0] <= 0 and fc[2] >= 1 for fc in frontier_coords), frontier_coords)


if __name__ == "__main__":
    unittest.main()
