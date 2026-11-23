"""Projection loading, stock transactions and actual multi-stage block placement."""

import math
from collections import Counter, deque
import unittest

from core.build_projection import load_projection
from core.campaign import Campaign
from core.client import APIError
from core.structure_spec import transform_rel_pos
from skills.campaign import CampaignSkills
from test_campaign import CampaignWorld, world
from test_daily_skills import envelope


class BuildWorld(CampaignWorld):
    def move_to(self, x, y, z, **kwargs):
        start = tuple(map(math.floor, self.status["pos"]))
        goal = tuple(map(math.floor, (x, y, z)))
        if start == goal:
            return super().move_to(x, y, z, **kwargs)

        def air(cell):
            return self.blocks.get(cell, {"name": "air" if cell[1] >= 64 else "stone"})["name"] in {"air", "torch"}

        scan = deque([start])
        visited = {start}
        while scan and len(visited) < 1400:
            a, b, c = scan.popleft()
            for dx, dz in ((1, 0), (0, 1), (-1, 0), (0, -1)):
                for dy in (0, 1, -1):
                    cell = (a + dx, b + dy, c + dz)
                    i, j, k = cell
                    if (cell in visited or abs(i) > 15 or abs(k) > 15
                            or not air(cell) or not air((i, j + 1, k)) or air((i, j - 1, k))
                            or dy > 0 and not air((a, b + 2, c))
                            or dy < 0 and not air((i, b + 1, k))):
                        continue
                    if cell == goal:
                        return super().move_to(x, y, z, **kwargs)
                    visited.add(cell)
                    scan.append(cell)
        raise APIError("path_not_found", "No one-block-jump approach through the assembled house")

    def place_block(self, x, y, z, *, item, **kwargs):
        if self.counts.get(item, 0) <= 0:
            raise APIError("missing_item", item)
        if math.dist(self.status["pos"], [x, y, z]) > 4:
            raise APIError("out_of_reach", "Placement exceeds a checked near stance")
        neighbours = ((x, y - 1, z), (x + 1, y, z), (x - 1, y, z),
                      (x, y, z + 1), (x, y, z - 1), (x, y + 1, z))
        if all(self.get_block(pos)["result"]["name"] in {"air", "torch", "oak_door"} for pos in neighbours):
            raise APIError("no_support", f"No solid support: {x,y,z}")
        self.calls.append(("place_projection", (x, y, z), item))
        self.counts[item] -= 1
        if item.endswith("_door"):
            self.block((x, y, z), item, properties={"half": "lower"})
            self.block((x, y + 1, z), item, properties={"half": "upper"})
        else:
            self.block((x, y, z), item)
        return envelope({"placed": item})


class BuildProjectionTests(unittest.TestCase):
    def test_load_and_rotate_preset(self):
        house = load_projection("timber_house")
        self.assertEqual(sum(house.materials.values()), len(house.blocks))
        self.assertEqual(house.materials["glass"], 32)
        self.assertEqual(max(row.pos[1] for row in house.blocks), 7)
        self.assertEqual(len({row.pos for row in house.at([0, 64, 0], "east")}), len(house.blocks))
        with self.assertRaises(ValueError):
            load_projection("../timber_house")

    def test_projection_survey_and_stage_stock_are_connected_to_campaign(self):
        house = load_projection("timber_house")
        game = BuildWorld()
        game.counts = {"stone_pickaxe": 1, "stone_sword": 1, "oak_planks": 40}
        campaign = Campaign({}, projection=house)
        skill = CampaignSkills(game, projection=house)
        observation = {**world(game.counts), "status": game.status}
        for _ in range(7):
            action = campaign.projection_action(observation)
            self.assertEqual(action.args["operation"], "build_projection")
            result = skill.execute(action.args, campaign.state)
            self.assertTrue(result.success, result)
            self.assertFalse(result.data["executed"])
            campaign.record(action, result)
        action = campaign.projection_action(observation)
        self.assertEqual(action.name, "run_survival_task")
        self.assertIn(action.args.get("item") or action.args["resource"], {"oak_planks", "cobblestone"})
        self.assertFalse(any(call[0] == "place_projection" for call in game.calls))

    def test_existing_matches_credit_stock_and_foreign_blocks_stop_before_writes(self):
        house = load_projection("timber_house")
        game = BuildWorld()
        projected = house.at([0, 64, 0], "north")
        first = projected[0]
        game.block(first.pos, first.item)
        skill = CampaignSkills(game, projection=house)
        args = {"operation": "build_projection", "anchor": [0, 64, 0], "orientation": "north"}
        result = skill.execute(args, {})
        self.assertEqual(result.data["projection"]["confirmed"], 1)
        self.assertEqual(result.data["projection"]["remaining"][first.item], house.materials[first.item] - 1)
        game.block(projected[1].pos, "chest")
        blocked = skill.execute(args, {})
        self.assertEqual(blocked.error_code, "projection_site_blocked")
        self.assertFalse(blocked.data["executed"])
        self.assertEqual(game.blocks[projected[1].pos]["name"], "chest")

    def test_legacy_project_resurveys_and_keeps_the_already_placed_species(self):
        house = load_projection("timber_house")
        game = BuildWorld()
        game.counts.update(stone_pickaxe=1, stone_sword=1, spruce_planks=64, spruce_log=32)
        key = [house.digest, [0, 64, 0], "north", "overworld"]
        game.block(house.at(key[1], key[2])[0].pos, "oak_planks")
        campaign = Campaign({"projection_project": {"key": key, "scan": len(house.blocks), "placed": [0],
            "stage_materials": {"oak_planks": 98}, "confirmed": 1}}, projection=house)
        for _ in range(7):
            obs = {**world(game.counts), "status": game.status}
            action = campaign.projection_action(obs)
            self.assertEqual(action.args["operation"], "build_projection")
            result = CampaignSkills(game, projection=house).execute(action.args, campaign.state)
            self.assertTrue(result.success, result)
            self.assertEqual(result.data["projection"]["wood_species"], "oak")
            campaign.record(action, result)
            # Restart both planner and executor with the saved choice.
            campaign = Campaign(campaign.state, projection=house)
        next_goal = campaign.projection_action(obs)
        self.assertEqual(next_goal.args["item"], "oak_planks")
        recover = campaign.recover_task(next_goal, {"args": next_goal.args, "task_id": "stock-1", "status": "needs_ai"}, obs)
        self.assertEqual(recover.args["log"], "oak_log")
        self.assertFalse(any(call[0] == "place_projection" for call in game.calls))

    def test_existing_mixed_species_meets_a_conflict_without_writes(self):
        house = load_projection("timber_house")
        game = BuildWorld()
        cells = house.at([0, 64, 0], "north")
        game.block(cells[0].pos, "spruce_planks")
        game.block(cells[1].pos, "oak_planks")
        args = {"operation": "build_projection", "anchor": [0, 64, 0], "orientation": "north"}
        result = CampaignSkills(game, projection=house).execute(args, {})
        self.assertEqual(result.error_code, "projection_site_blocked")
        self.assertEqual(result.data["blocker_pos"], list(cells[1].pos))
        self.assertFalse(result.data["executed"])

    def test_selects_nearby_timber_over_one_leftover_plank(self):
        house = load_projection("timber_house")
        self.assertEqual(house.preferred_wood({"oak_planks": 1}, [
            {"name": "dark_oak_log", "pos": [5, 64, 1]},
            {"name": "dark_oak_log", "pos": [6, 64, 1]},
        ]), "dark_oak")
        self.assertIsNone(house.preferred_wood({"oak_planks": 1}, []))
        self.assertIsNone(house.preferred_wood({}, []))

    def test_entire_preset_and_door_are_written_and_reobserved(self):
        house = load_projection("timber_house")
        game = BuildWorld()
        expected = house.at([0, 64, 0], "north", "spruce")
        game.counts.update(Counter(block.item for block in expected))
        game.counts.update(stone_pickaxe=1, stone_sword=1, oak_log=64, oak_planks=3, oak_door=1)
        campaign = Campaign({}, projection=house)
        skill = CampaignSkills(game, projection=house)
        for _ in range(600):
            observation = {**world(game.counts), "status": game.status}
            action = campaign.projection_action(observation)
            self.assertEqual(action.args.get("operation"), "build_projection", action)
            result = skill.execute(action.args, campaign.state)
            previous = campaign.state.get("projection_project", {})
            self.assertTrue(result.success, (game.status["pos"], previous.get("stage"), previous.get("confirmed"), result))
            campaign.record(action, result)
            if campaign.state["projection_project"].get("complete"):
                break
        self.assertTrue(campaign.state["projection_project"].get("complete"))
        self.assertEqual(campaign.state["projection_project"]["confirmed"], len(house.blocks))
        self.assertEqual(campaign.state["projection_project"]["wood_species"], "spruce")
        placements = [call for call in game.calls if call[0] == "place_projection"]
        self.assertEqual(len(placements), len(house.blocks))
        self.assertEqual({call[1]: call[2] for call in placements}, {block.pos: block.item for block in expected})
        self.assertEqual(game.counts["oak_planks"], 3)
        self.assertEqual(game.counts["oak_log"], 64)
        self.assertEqual(game.counts["oak_door"], 1)
        door = next(block for block in house.blocks if block.item == "oak_door")
        p = transform_rel_pos(door.pos, [0, 64, 0], "north", integer=True)
        self.assertEqual(game.blocks[(p[0], p[1] + 1, p[2])]["properties"]["half"], "upper")
