"""Local daily-collector contract tests; no real Minecraft or model requests."""

import copy
import math
import threading
import unittest
from unittest.mock import patch

from agent.daily_survival import DailySurvivalGoals
from core.client import APIError
from core.daily_observation import DailyObservationCollector
from core.survival_state import ObservationCollector
from skills import SkillResult
from skills.shelter import ShelterSkills


def status(**changes):
    return {"ready": True, "alive": True, "busy": None, "on_ground": True, "health": 20,
            "food": 20, "pos": [.5, 64, .5], "dimension": "overworld", "gamemode": "survival",
            "is_day": True, "thundering": False, "time_of_day": 1000, **changes}


def block(name, pos, **properties):
    return {"name": name, "pos": list(pos), "properties": properties}


def observation(rows=None, **changes):
    return {"fresh": True, "status": status(**changes), "blocks": rows or [], "entities": [],
            "counts": {}, "inventory": [], "daily_evidence": {}}


class DailyClient:
    def __init__(self):
        self.status = status()
        self.blocks = {(x, 63, z): block("grass_block", [x, 63, z])
                       for x in range(-6, 7) for z in range(-6, 7)}
        self.windows = {}
        self.calls = []
        self.after_window = None
        self.after_block = None
        self.status_changes = []
        self.discovered = []
        self.items = []

    def get_status(self, **kwargs):
        self.calls.append(("status",))
        if self.status_changes:
            self.status.update(self.status_changes.pop(0))
        return {"ok": True, "result": copy.deepcopy(self.status)}

    def get_block(self, pos, **kwargs):
        self.calls.append(("block", list(pos)))
        value = self.blocks.get(tuple(pos), block("air", pos))
        if isinstance(value, Exception):
            raise value
        if self.after_block:
            self.after_block(pos)
        return {"ok": True, "result": copy.deepcopy(value)}

    def get_blocks(self, **kwargs):
        self.calls.append(("find", kwargs.get("types")))
        names = kwargs.get("types")
        return copy.deepcopy([row for row in self.discovered if names is None or row["name"] in names][:kwargs.get("limit", 64)])

    def get_entities(self, **kwargs):
        self.calls.append(("entities",))
        return []

    def inventory(self, **kwargs):
        self.calls.append(("inventory",))
        return {"ok": True, "result": {"items": copy.deepcopy(self.items), "armor": {}}}

    def window_view(self, pos, **kwargs):
        self.calls.append(("window", list(pos)))
        value = self.windows[tuple(pos)]
        if self.after_window:
            self.after_window(pos)
        if isinstance(value, Exception):
            raise value
        return {"ok": True, "result": copy.deepcopy(value)}

    def station(self, name, pos, *, burning=False, items=None):
        self.blocks[tuple(pos)] = block(name, pos)
        self.windows[tuple(pos)] = {"target": name, "sections":
            {"input": [], "fuel": [], "output": []} if name in DailyObservationCollector.FURNACES else {"container": items or []},
            "info": {"burning": burning}}
        return copy.deepcopy(self.blocks[tuple(pos)])


class DailyObservationTests(unittest.TestCase):
    def setUp(self):
        self.client = DailyClient()
        self.collector = DailyObservationCollector(self.client)

    def collect(self, obs=None, cancel=None):
        obs = observation() if obs is None else obs
        with patch.object(ObservationCollector, "collect", return_value=copy.deepcopy(obs)):
            return self.collector.collect(cancel)

    def windows(self, obs):
        self.collector._windows(obs, None)
        return obs

    def home_data(self, **changes):
        return {"pos": [0, 64, 0], "dimension": "overworld", "fresh": True, "inside": False,
                "geometry": {key: True for key in ("floor_verified", "exit_verified", "walls_complete", "roof_complete", "door_complete")},
                "shelter_verified": True, "structure_complete": True,
                "door": {"pos": [0, 64, -2], "name": "oak_door", "facing": "north",
                         "frame_verified": True, "open": False, "complete": True, "reason": None},
                "hazards": [], "occupied_blueprint_cells": [],
                "next_gap": None, **changes}

    def test_two_distinct_nearby_windows_at_most(self):
        rows = [self.client.station("chest", [1, 64, 0]), self.client.station("furnace", [2, 64, 0]),
                self.client.station("barrel", [0, 64, 2]), self.client.station("chest", [6, 64, 0])]
        obs = self.windows(observation([rows[0], rows[0], *rows[1:]]))
        calls = [call for call in self.client.calls if call[0] == "window"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(len({tuple(call[1]) for call in calls}), 2)
        self.assertEqual(len(obs["containers"]) + len(obs["furnaces"]), 2)

    def test_far_and_ungrounded_windows_are_not_opened(self):
        row = self.client.station("chest", [4, 64, 0])
        self.windows(observation([row]))
        row = self.client.station("chest", [1, 64, 0])
        self.windows(observation([row], on_ground=False))
        self.assertFalse(any(call[0] == "window" for call in self.client.calls))

    def test_burning_maps_to_goal_contract_and_prevents_refill(self):
        row = self.client.station("furnace", [2, 64, 0], burning=True)
        obs = self.windows(observation([row]))
        obs["counts"] = {"beef": 3, "coal": 1}
        self.assertTrue(obs["furnaces"][0]["burning"])
        board = DailySurvivalGoals().build(obs)
        food = next(task for task in board["tasks"] if task["id"] == "food_reserve")
        self.assertIsNone(food["next_action"])

    def test_explicit_empty_slots_are_known_empty(self):
        row = self.client.station("furnace", [2, 64, 0])
        checked = self.windows(observation([row]))["furnaces"][0]
        self.assertEqual(checked["input"], {"name": None, "count": 0})
        self.assertIs(checked["burning"], False)
        self.assertTrue(checked["fresh"])

    def test_malformed_missing_slots_or_unknown_burning_are_not_empty(self):
        for mode in ("missing", "none", "negative", "zero", "bool", "duplicate", "burning_missing", "burning_string"):
            with self.subTest(mode=mode):
                row = self.client.station("furnace", [2, 64, 0])
                window = self.client.windows[(2, 64, 0)]
                if mode == "missing": window["sections"].pop("input")
                elif mode == "none": window["sections"]["input"] = None
                elif mode == "burning_missing": window["info"] = {}
                elif mode == "burning_string": window["info"]["burning"] = "false"
                else:
                    count = {"negative": -1, "zero": 0, "bool": True, "duplicate": 1}[mode]
                    window["sections"]["input"] = [{"name": "beef", "count": count}] * (2 if mode == "duplicate" else 1)
                with self.assertRaises(APIError):
                    self.windows(observation([row]))

    def test_window_target_mismatch_is_rejected(self):
        row = self.client.station("chest", [1, 64, 0])
        self.client.windows[(1, 64, 0)]["target"] = "barrel"
        with self.assertRaises(APIError): self.windows(observation([row]))

    def test_timeout_aborts_collection_before_other_window_farm_or_home(self):
        rows = [self.client.station("chest", [1, 64, 0]), self.client.station("barrel", [2, 64, 0])]
        self.client.windows[(1, 64, 0)] = APIError("request_timeout", "GUI read pending")
        with patch.object(self.collector, "_farm") as farm, patch.object(self.collector, "_home") as home:
            with self.assertRaises(APIError) as raised: self.collect(observation(rows))
        self.assertEqual(raised.exception.code, "window_observation_uncertain")
        self.assertEqual(raised.exception.details, {"cause": "request_timeout"})
        self.assertEqual(sum(call[0] == "window" for call in self.client.calls), 1)
        farm.assert_not_called()
        home.assert_not_called()

    def test_window_busy_after_read_prevents_next_window(self):
        rows = [self.client.station("chest", [1, 64, 0]), self.client.station("barrel", [2, 64, 0])]
        self.client.after_window = lambda _: self.client.status.update(busy="window_view")
        with self.assertRaises(APIError) as raised: self.windows(observation(rows))
        self.assertEqual(raised.exception.code, "observation_changed")
        self.assertEqual(sum(call[0] == "window" for call in self.client.calls), 1)

    def test_busy_or_life_change_before_window_prevents_gui_operation(self):
        row = self.client.station("chest", [1, 64, 0])
        for changes in ({"busy": "dig"}, {"alive": False}, {"dimension": "the_nether"},
                        {"last_death": {"t": 1}}, {"on_ground": False}, {"pos": [1.5, 64, .5]}):
            with self.subTest(changes=changes):
                self.client.status = status(**changes)
                self.client.calls.clear()
                with self.assertRaises(APIError): self.windows(observation([row]))
                self.assertFalse(any(call[0] == "window" for call in self.client.calls))

    def test_station_replacement_drops_old_typed_row_without_opening_it(self):
        row = self.client.station("chest", [1, 64, 0])
        self.client.blocks[(1, 64, 0)] = block("air", [1, 64, 0])
        obs = self.windows(observation([row]))
        self.assertEqual(obs["containers"], [])
        self.assertEqual(obs["blocks"][0]["name"], "air")
        self.assertFalse(any(call[0] == "window" for call in self.client.calls))

    def crops(self, count=4):
        rows = []
        for x in range(count):
            pos = [x, 64, 2]
            self.client.blocks[tuple(pos)] = block("wheat", pos, age="3")
            self.client.blocks[(x, 63, 2)] = block("farmland", [x, 63, 2], moisture=7)
            rows.append(block("wheat", pos, age=7))
        return rows

    def test_four_live_crops_over_fresh_farmland_map_structural_renewable(self):
        obs = observation(self.crops())
        self.collector._farm(obs, None)
        production = obs["daily_evidence"]["food_production"]
        self.assertTrue(production["renewable"])
        self.assertFalse(production["sustained_yield_guaranteed"])
        self.assertEqual(production["lighting"], "unknown")
        self.assertEqual(len(production["growing_crops"]), 4)
        self.assertTrue(all(r["properties"]["age"] == 3 for r in obs["blocks"] if r["name"] == "wheat"))
        task = next(t for t in DailySurvivalGoals().build(obs)["tasks"] if t["id"] == "food_production")
        self.assertEqual(task["status"], "satisfied")

    def test_duplicate_crop_or_absent_farmland_cannot_satisfy_production(self):
        rows = self.crops(1)
        obs = observation(rows * 4)
        self.collector._farm(obs, None)
        self.assertNotIn("food_production", obs["daily_evidence"])
        obs = observation(self.crops())
        self.client.blocks[(0, 63, 2)] = block("dirt", [0, 63, 2])
        self.collector._farm(obs, None)
        self.assertNotIn("food_production", obs["daily_evidence"])

    def test_changed_crop_age_or_block_never_reuses_stale_maturity(self):
        row = self.crops(1)[0]
        for value in (block("air", row["pos"]), block("wheat", row["pos"], age=True), block("wheat", row["pos"], age=99)):
            self.client.blocks[tuple(row["pos"])] = value
            obs = observation([copy.deepcopy(row)])
            self.collector._farm(obs, None)
            self.assertNotIn("food_production", obs["daily_evidence"])
            self.assertEqual(obs["blocks"][0], {**value, "fresh": True, "source": "api_verified"})

    def test_tuple_candidate_position_is_replaced_without_stale_duplicate(self):
        row = self.crops(1)[0]
        row["pos"] = tuple(row["pos"])
        self.client.blocks[row["pos"]] = block("air", row["pos"])
        obs = observation([row])
        self.collector._farm(obs, None)
        self.assertEqual(len(obs["blocks"]), 1)
        self.assertEqual(obs["blocks"][0]["name"], "air")

    def test_unknown_farmland_aborts_instead_of_claiming_empty_soil(self):
        rows = self.crops()
        self.client.blocks[(0, 63, 2)] = APIError("not_loaded", "soil unknown")
        with self.assertRaises(APIError): self.collector._farm(observation(rows), None)

    def test_memory_alone_never_becomes_fresh_shelter(self):
        self.collector.set_context({"memory": {"places": [self.home_data()]}})
        with patch.object(ShelterSkills, "execute") as inspect:
            obs = self.collect()
        inspect.assert_not_called()
        self.assertNotIn("shelter", obs["daily_evidence"])

    def test_home_preserves_inside_and_nested_route_contract(self):
        self.collector.set_context({"base_project": {"pos": [0, 64, 0], "dimension": "overworld"}})
        route = {"fresh": True, "verified": True, "origin": [.5, 64, .5], "path": [[1, 64, 0]]}
        obs = observation()
        with patch.object(ShelterSkills, "execute", return_value=SkillResult(True, "observed", self.home_data())), \
                patch.object(self.collector, "_return_route", return_value=route):
            self.collector._home(obs, None)
        self.assertIs(obs["daily_evidence"]["shelter"]["inside"], False)
        self.assertEqual(obs["daily_evidence"]["shelter"]["return_route"], route)
        self.assertNotIn("return_route", obs)

    def test_mismatched_stale_or_incomplete_shelter_is_not_promoted(self):
        self.collector.set_context({"base_project": {"pos": [0, 64, 0], "dimension": "overworld"}})
        for changes in ({"fresh": False}, {"dimension": "the_nether"}, {"inside": None}, {"pos": [1, 64, 0]}):
            with patch.object(ShelterSkills, "execute", return_value=SkillResult(True, "bad", self.home_data(**changes))):
                with self.assertRaises(APIError): self.collector._home(observation(), None)

    def test_actual_shelter_inspect_derives_fresh_construction_position(self):
        self.client.status = status(pos=[6.5, 64, .5])
        obs = observation(pos=[6.5, 64, .5])
        self.collector.set_context({"base_project": {"pos": [0, 64, 0], "dimension": "overworld", "material": "cobblestone"}})
        self.collector._home(obs, None)
        self.assertTrue(obs["shelter"]["needs_position"])
        self.assertTrue(obs["shelter"]["stand_candidates"])
        self.assertTrue(obs["daily_evidence"]["base_site"]["verified"])
        self.assertNotIn("shelter", obs["daily_evidence"])

    def test_return_route_checks_start_support_and_every_body_cell(self):
        origin = status(pos=[-2.5, 64, .5])
        for pos, name, properties in (((-3, 63, 0), "air", {}), ((-3, 64, 0), "stone", {}),
                                     ((-3, 65, 0), "stone", {}), ((-2, 65, 0), "short_grass", {"waterlogged": True})):
            with self.subTest(pos=pos):
                old = self.client.blocks.get(pos)
                self.client.blocks[pos] = block(name, pos, **properties)
                self.assertIsNone(self.collector._return_route(origin, [0, 64, 0], None))
                if old is None: self.client.blocks.pop(pos)
                else: self.client.blocks[pos] = old

    def test_starting_body_at_cell_edge_checks_adjacent_head_voxel(self):
        self.client.blocks[(-4, 65, 0)] = block("stone", [-4, 65, 0])
        self.assertIsNone(self.collector._return_route(status(pos=[-2.99, 64, .5]), [0, 64, 0], None))
        self.client.blocks[(-4, 65, 0)] = block("air", [-4, 65, 0])
        self.client.blocks[(-4, 63, 0)] = block("air", [-4, 63, 0])
        self.assertIsNone(self.collector._return_route(status(pos=[-2.99, 64, .5]), [0, 64, 0], None))

    def test_short_route_contains_integer_adjacent_feet_and_is_not_actual_path(self):
        result = self.collector._return_route(status(pos=[-3.5, 64, .5]), [0, 64, 0], None)
        self.assertEqual(result["path"], [[-3, 64, 0], [-2, 64, 0], [-1, 64, 0], [0, 64, 0]])
        self.assertTrue(result["start_verified"])
        self.assertTrue(result["body_and_support_verified"])
        self.assertFalse(result["actual_path_verified"])

    def test_long_vertical_ungrounded_or_closed_door_route_is_rejected(self):
        for current in (status(pos=[-4.5, 64, .5]), status(pos=[-2.5, 65, .5]), status(pos=[-2.5, 64, .5], on_ground=False)):
            self.assertIsNone(self.collector._return_route(current, [0, 64, 0], None))
        self.client.blocks[(-1, 64, 0)] = block("oak_door", [-1, 64, 0], open=False)
        self.assertIsNone(self.collector._return_route(status(pos=[-2.5, 64, .5]), [0, 64, 0], None))

    def put_door(self, *, opened=False, facing="north"):
        for y, half in ((64, "lower"), (65, "upper")):
            self.client.blocks[(0, y, -2)] = block("oak_door", [0, y, -2],
                half=half, facing=facing, hinge="left", open=opened)

    def fill_home(self, *, opened=False):
        walls, roof, _ = ShelterSkills._blueprint([0, 64, 0])
        for p in walls + roof:
            self.client.blocks[p] = block("cobblestone", p)
        self.put_door(opened=opened)
        self.collector.set_context({"base_project": {"pos": [0, 64, 0], "dimension": "overworld"}})

    def test_closed_verified_door_yields_blocked_route_after_all_other_checks(self):
        self.put_door()
        route = self.collector._return_route(status(pos=[.5, 64, -2.5]), [0, 64, 0], None)
        self.assertFalse(route["verified"])
        self.assertEqual(route["blocked_door"], {"pos": [0, 64, -2], "verified": True, "open": False})
        self.assertIn(route["blocked_door"]["pos"], route["path"])
        self.assertFalse(route["body_and_support_verified"])
        self.assertTrue(route["all_other_body_and_support_verified"])
        self.client.blocks[(0, 65, -1)] = block("stone", [0, 65, -1])
        self.assertIsNone(self.collector._return_route(status(pos=[.5, 64, -2.5]), [0, 64, 0], None))

    def test_open_verified_door_allows_center_path_but_never_unknown_frame(self):
        self.put_door(opened=True)
        route = self.collector._return_route(status(pos=[.5, 64, -2.5]), [0, 64, 0], None)
        self.assertTrue(route["verified"])
        self.assertNotIn("blocked_door", route)
        for change in ({"facing": "east"}, {"half": "lower"}, {"hinge": "right"}, {"open": False}, {"open": None}):
            self.put_door(opened=True)
            self.client.blocks[(0, 65, -2)]["properties"].update(change)
            self.assertIsNone(self.collector._return_route(status(pos=[.5, 64, -2.5]), [0, 64, 0], None))

    def test_open_home_is_current_structure_not_completed_closed_shelter(self):
        self.fill_home(opened=True)
        self.client.status = status(pos=[.5, 64, -2.5])
        obs = observation(pos=[.5, 64, -2.5])
        self.collector._home(obs, None)
        home = obs["daily_evidence"]["shelter"]
        self.assertTrue(home["verified"])
        self.assertTrue(home["structure_complete"])
        self.assertFalse(home["shelter_verified"])
        self.assertFalse(home["geometry_complete"])
        self.assertTrue(home["return_route"]["verified"])
        task = next(t for t in DailySurvivalGoals().build(obs)["tasks"] if t["id"] == "shelter")
        self.assertNotEqual(task["status"], "satisfied")

    def test_closed_home_has_only_door_blocked_route(self):
        self.fill_home()
        self.client.status = status(pos=[.5, 64, -2.5])
        obs = observation(pos=[.5, 64, -2.5])
        self.collector._home(obs, None)
        home = obs["daily_evidence"]["shelter"]
        self.assertTrue(home["shelter_verified"])
        self.assertFalse(home["return_route"]["verified"])
        self.assertTrue(home["return_route"]["blocked_door"]["verified"])

    def test_inside_far_from_open_door_gets_fresh_route_to_center(self):
        self.fill_home(opened=True)
        self.client.status = status(pos=[-.5, 64, 1.5])
        obs = observation(pos=[-.5, 64, 1.5])
        self.collector._home(obs, None)
        home = obs["daily_evidence"]["shelter"]
        self.assertTrue(home["inside"])
        self.assertGreater(math.dist(self.client.status["pos"], home["door"]["pos"]), 3)
        self.assertTrue(home["return_route"]["verified"])
        self.assertEqual(home["return_route"]["path"][-1], [0, 64, 0])

    def test_busy_initial_snapshot_performs_no_extra_reads(self):
        obs = self.collect(observation(busy="window_view"))
        self.assertEqual(obs["daily_evidence"], {})
        self.assertEqual(self.client.calls, [])

    def test_final_movement_and_missing_busy_are_not_fresh_success(self):
        self.client.status["pos"] = [1.5, 64, .5]
        with self.assertRaises(APIError): self.collect()
        self.client.status = status()
        self.client.status.pop("busy")
        with self.assertRaises(APIError): self.collect()
        self.client.status = status(busy=0)
        with self.assertRaises(APIError): self.collect(observation(busy=0))

    def test_cancellation_stops_partial_block_scan(self):
        event = threading.Event()
        self.client.after_block = lambda _: event.set()
        with self.assertRaises(APIError) as raised:
            self.collector._farm(observation(self.crops()), event)
        self.assertEqual(raised.exception.code, "cancelled")
        self.assertEqual(sum(call[0] == "block" for call in self.client.calls), 1)

    def test_full_collector_runs_with_real_parent_collector(self):
        row = self.client.station("furnace", [2, 64, 0])
        self.client.discovered = [row]
        result = self.collector.collect()
        self.assertTrue(result["fresh"])
        self.assertEqual(result["furnaces"][0]["name"], "furnace")
        self.assertIs(result["furnaces"][0]["burning"], False)

    def test_grass_has_separate_eight_plant_quota_and_bounded_fresh_visibility(self):
        soil = [block("dirt", [x, 63, z]) for x in range(8) for z in range(6)]
        grass = [block("short_grass", [x, 64, z]) for x in (2, 3, 4) for z in range(4)]
        self.client.discovered = soil + grass
        for row in grass:
            self.client.blocks[tuple(row["pos"])] = row
        result = self.collect()
        plants = [row for row in result["blocks"] if row["name"] == "short_grass"]
        self.assertEqual(len(plants), 8)
        self.assertTrue(all(row["fresh"] is True for row in plants))
        self.assertTrue(any(row["can_see"] is True for row in plants))
        self.assertIn(("find", ["short_grass", "tall_grass"]), self.client.calls)
        self.assertLessEqual(sum(call[0] == "block" for call in self.client.calls), 48)
        self.assertNotIn("food_production", result["daily_evidence"])

    def test_occluded_grass_is_not_marked_visible(self):
        row = block("short_grass", [3, 64, 0])
        self.client.discovered = [row]
        self.client.blocks[(3, 64, 0)] = row
        for y in (64, 65, 66):
            for z in (-1, 0, 1):
                self.client.blocks[(1, y, z)] = block("dirt", [1, y, z])
        obs = observation()
        self.collector._seed_plants(obs, None)
        self.assertIs(obs["blocks"][0]["can_see"], False)

    def test_changed_grass_target_is_replaced_by_actual_air(self):
        self.client.discovered = [block("short_grass", [2, 64, 0])]
        obs = observation(self.client.discovered)
        self.collector._seed_plants(obs, None)
        self.assertEqual(obs["blocks"][0]["name"], "air")
        self.assertTrue(obs["blocks"][0]["fresh"])
        self.assertIsNone(obs["blocks"][0]["can_see"])

    def test_seed_plant_scan_honours_cancellation_before_visibility_reads(self):
        event = threading.Event()
        row = block("short_grass", [2, 64, 0])
        self.client.discovered = [row]
        self.client.blocks[(2, 64, 0)] = row
        self.client.after_block = lambda pos: event.set()
        with self.assertRaises(APIError) as raised:
            self.collector._seed_plants(observation(), event)
        self.assertEqual(raised.exception.code, "cancelled")
        self.assertEqual(sum(call[0] == "block" for call in self.client.calls), 1)

    def test_foliage_scan_checks_only_sixteen_neighbour_body_cells_and_preserves_crop_evidence(self):
        crop = {**block("carrots", [1, 64, 0], age=7), "fresh": True, "can_see": True}
        self.client.blocks[(1, 64, 0)] = block("carrots", [1, 64, 0], age="7")
        obs = observation([copy.deepcopy(crop)])
        self.collector._local_foliage(obs, None)
        cells = [tuple(call[1]) for call in self.client.calls if call[0] == "block"]
        self.assertEqual(len(cells), 16)
        self.assertEqual(len(set(cells)), 16)
        self.assertTrue(all(y in (64, 65) and max(abs(x), abs(z)) == 1 for x, y, z in cells))
        self.assertEqual(obs["blocks"], [crop])

    def test_foliage_visibility_is_fresh_bounded_and_uses_shared_leaf_whitelist(self):
        for x, z in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            for y in (64, 65):
                self.client.blocks[(x, y, z)] = block("azalea_leaves", [x, y, z], waterlogged=False)
        self.client.blocks[(0, 64, 0)] = block("oak_leaves", [0, 64, 0], waterlogged=False)
        self.client.blocks[(2, 64, 0)] = block("oak_leaves", [2, 64, 0], waterlogged=False)
        obs = observation()
        self.collector._local_foliage(obs, None)
        self.assertEqual(len(obs["blocks"]), 16)
        self.assertTrue(all(row["fresh"] is True and "visibility" in row for row in obs["blocks"]))
        self.assertTrue(any(row["can_see"] is True for row in obs["blocks"]))
        self.assertNotIn([0, 64, 0], [row["pos"] for row in obs["blocks"]])
        self.assertNotIn([2, 64, 0], [row["pos"] for row in obs["blocks"]])
        self.assertLessEqual(sum(call[0] == "block" for call in self.client.calls), 32)

    def test_occluded_or_unknown_wet_leaf_is_never_a_visible_clear_candidate(self):
        for y in (64, 65):
            self.client.blocks[(1, y, 0)] = block("stone", [1, y, 0])
            self.client.blocks[(0, y, 1)] = block("stone", [0, y, 1])
        self.client.blocks[(1, 65, 1)] = block("oak_leaves", [1, 65, 1], waterlogged=False)
        self.client.blocks[(-1, 65, 0)] = block("birch_leaves", [-1, 65, 0])
        self.client.blocks[(0, 65, -1)] = block("spruce_leaves", [0, 65, -1], waterlogged=True)
        obs = observation()
        self.collector._local_foliage(obs, None)
        rows = {tuple(row["pos"]): row for row in obs["blocks"]}
        self.assertIs(rows[(1, 65, 1)]["can_see"], False)
        self.assertIsNone(rows[(-1, 65, 0)]["can_see"])
        self.assertIsNone(rows[(0, 65, -1)]["can_see"])

    def test_unloaded_foliage_is_not_replaced_with_air_and_stale_leaf_is_removed(self):
        stale = {**block("oak_leaves", [1, 64, 0], waterlogged=False), "can_see": True, "fresh": True}
        self.client.blocks[(1, 64, 0)] = APIError("not_loaded", "Unknown cell")
        self.client.blocks[(-1, 65, 0)] = block("vine", [-1, 65, 0])
        obs = observation([stale])
        self.collector._local_foliage(obs, None)
        self.assertNotIn([1, 64, 0], [row["pos"] for row in obs["blocks"]])
        self.assertFalse(any(row["name"] == "air" for row in obs["blocks"]))
        self.assertEqual(obs["blocks"][0]["name"], "vine")
        self.assertIs(obs["blocks"][0]["can_see"], True)

    def test_foliage_scan_cancels_before_additional_reads(self):
        event = threading.Event()
        self.client.after_block = lambda pos: event.set()
        with self.assertRaises(APIError) as raised:
            self.collector._local_foliage(observation(), event)
        self.assertEqual(raised.exception.code, "cancelled")
        self.assertEqual(sum(call[0] == "block" for call in self.client.calls), 1)

    def test_real_collector_checks_low_health_crop_neighbours_for_policy(self):
        from core.daily_policy import DailySafetyPolicy
        self.client.status.update(health=6, food=4)
        crop = block("carrots", [1, 64, 0], age="7")
        self.client.blocks[(1, 64, 0)] = crop
        self.client.blocks[(1, 63, 0)] = block("farmland", [1, 63, 0])
        self.client.discovered = [crop]
        result = self.collector.collect()
        self.assertTrue(DailySafetyPolicy().constraints(result)["model_allowed"])
        indexed = {tuple(row["pos"]): row for row in result["blocks"]}
        self.assertEqual(indexed[(1, 64, 0)]["properties"]["age"], 7)
        self.assertEqual(indexed[(2, 64, 0)]["name"], "air")
        self.assertTrue(indexed[(2, 64, 0)]["fresh"])

    def test_food_neighbourhood_scan_is_nearby_mature_and_at_most_two(self):
        rows = [block("carrots", pos, age=age) for pos, age in (
            ([1, 64, 0], 7), ([0, 64, 1], 7), ([1, 64, 1], 7), ([-1, 64, 0], 2), ([8, 64, 0], 7))]
        for row in rows:
            row["fresh"] = True
            self.client.blocks[tuple(row["pos"])] = row
        obs = observation(rows, health=6, food=4)
        self.collector._food_neighborhoods(obs, None)
        self.assertEqual(len(self.client.calls), 14)
        self.assertTrue(all(call[0] == "block" for call in self.client.calls))
        self.assertNotIn(("block", [8, 64, 0]), self.client.calls)

    def test_food_neighbourhoods_skip_healthy_night_unknown_or_regenerating(self):
        for changed in ({"health": 20}, {"food": 18}, {"is_day": False}, {"time_of_day": 11000},
                        {"thundering": True}, {"on_ground": False}, {"health": None}, {"food": True}):
            row = {**block("carrots", [1, 64, 0], age=7), "fresh": True}
            self.collector._food_neighborhoods(observation([row], **{"health": 6, "food": 4, **changed}), None)
        self.assertEqual(self.client.calls, [])

    def test_unknown_food_neighbour_or_changed_target_aborts_instead_of_air(self):
        row = {**block("carrots", [1, 64, 0], age=7), "fresh": True}
        self.client.blocks[(1, 64, 0)] = row
        self.client.blocks[(2, 64, 0)] = {"pos": [2, 64, 0]}
        with self.assertRaises(APIError) as raised:
            self.collector._food_neighborhoods(observation([row], health=6, food=4), None)
        self.assertEqual(raised.exception.code, "invalid_response")
        self.client.blocks[(1, 64, 0)] = block("air", [1, 64, 0])
        with self.assertRaises(APIError) as raised:
            self.collector._food_neighborhoods(observation([row], health=6, food=4), None)
        self.assertEqual(raised.exception.code, "observation_changed")

    def test_food_neighbourhoods_require_current_edible_window_contents(self):
        chest = self.client.station("chest", [1, 64, 0], items=[{"name": "bread", "count": 1}])
        obs = observation([chest], health=6, food=4)
        self.windows(obs)
        self.client.calls.clear()
        self.collector._food_neighborhoods(obs, None)
        self.assertEqual(len(self.client.calls), 7)
        self.client.calls.clear()
        obs["containers"][0]["fresh"] = False
        self.collector._food_neighborhoods(obs, None)
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
