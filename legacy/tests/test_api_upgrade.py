"""API 1.3.14 worker exits and legacy calls, entirely offline."""

import copy
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from agent.survival_planner import Decision
from core.client import APIError, MinecraftAgentClient
from core.config import DEFAULTS
from core.survival_harness import SurvivalHarness, _uncertain_result
from skills.daily_survival import DailySurvivalSkills
from skills.survival import SurvivalSkills
from skills.sleep import AutoSleepSkill
from skills.shelter import ShelterSkills
from skills.batch import BatchSkills
from test_batch import MaterialWorld
from test_daily_skills import DailyWorld
from test_shelter import ShelterClient
from test_sleep import Client as SleepClient
from test_survival_skills import World, envelope


def disconnected_response():
    return Mock(status_code=200, json=Mock(return_value={
        "ok": False, "error": {"code": "not_connected", "message": "bot process exited (1)"}}))


class WorkerExitClientTests(unittest.TestCase):
    def test_sent_writes_are_unknown_and_keep_original_backend_code(self):
        client = MinecraftAgentClient()
        for endpoint in sorted(client.WRITE_ENDPOINTS):
            with self.subTest(endpoint=endpoint), patch("core.client.requests.request", return_value=disconnected_response()) as request:
                with self.assertRaises(APIError) as caught:
                    client._request("POST", endpoint)
                error = caught.exception
                self.assertEqual(error.code, "action_unconfirmed")
                self.assertEqual(error.status_code, 200)
                self.assertEqual(error.details, {"api_code": "not_connected", "executed": True, "action_state": "unknown"})
                request.assert_called_once()

    def test_observations_and_connection_management_keep_not_connected(self):
        client = MinecraftAgentClient()
        for endpoint in ("status", "inventory", "get_block", "get_entities", "find_blocks", "events", "advancements", "connect", "stop"):
            with self.subTest(endpoint=endpoint), patch("core.client.requests.request", return_value=disconnected_response()) as request:
                with self.assertRaises(APIError) as caught:
                    client._request("POST", endpoint)
                self.assertEqual(caught.exception.code, "not_connected")
                self.assertNotIn("action_state", caught.exception.details)
                request.assert_called_once()

    def test_dig_preserves_pos_collect_and_new_xyz_overloads(self):
        client = MinecraftAgentClient()
        cases = [(([1, 64, 2], False), {}, False), (([1, 64, 2], True), {}, True),
                 (([1, 64, 2],), {"collect": False}, False),
                 ((1, 64, 2), {"collect": False}, False), ((1, 64, 2, False), {}, False)]
        with patch("core.client.requests.request", return_value=Mock(status_code=200, json=Mock(return_value=envelope({})))) as request:
            for args, kwargs, collect in cases:
                with self.subTest(args=args, kwargs=kwargs):
                    client.dig(*args, **kwargs)
                    self.assertEqual(request.call_args.kwargs["json"], {"x": 1, "y": 64, "z": 2, "collect": collect})
            for args in (([1, 64, 2], 7), ([1, 64, 2], False, 9)):
                before = request.call_count
                with self.assertRaises(APIError):
                    client.dig(*args)
                self.assertEqual(request.call_count, before)


class WorkerExitSkillTests(unittest.TestCase):
    def test_real_client_craft_worker_exit_is_not_reclassified_as_partial(self):
        world = World()
        world.counts = {"oak_log": 1}
        client = MinecraftAgentClient()
        client.get_status = world.get_status
        client.inventory = world.inventory
        client.stop = world.stop
        def request(*args, **kwargs):
            world.counts["oak_planks"] = 4
            return disconnected_response()
        with patch("core.client.requests.request", side_effect=request) as http:
            result = SurvivalSkills(client).execute("craft_item", {"item": "oak_planks", "count": 4})
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertTrue(_uncertain_result(result))
        self.assertEqual(result.data["api_details"]["api_code"], "not_connected")
        self.assertEqual(sum(c[0] == "inventory" for c in world.calls), 1)
        self.assertFalse(any(c[0] == "stop" for c in world.calls))
        http.assert_called_once()

    def test_raw_worker_exit_after_craft_is_unknown_without_extra_inventory(self):
        world = World()
        def craft(item, count):
            world.counts[item] = count
            raise APIError("not_connected", "worker exited")
        world.craft_effect = craft
        result = SurvivalSkills(world).execute("craft_item", {"item": "stick", "count": 4})
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertTrue(_uncertain_result(result))
        self.assertEqual(sum(c[0] == "craft" for c in world.calls), 1)
        self.assertEqual(sum(c[0] == "inventory" for c in world.calls), 1)

    def test_post_write_status_inventory_and_block_disconnect_are_unknown(self):
        for reader in ("get_status", "inventory", "get_block"):
            with self.subTest(reader=reader):
                world = World()
                world.counts["cobblestone"] = 1
                def place(item, pos):
                    world.set_block(pos, item)
                    world.counts[item] -= 1
                    setattr(world, reader, Mock(side_effect=APIError("not_connected", "verification disconnected")))
                    return envelope({"placed": item})
                world.place_effect = place
                result = SurvivalSkills(world).execute("place_item", {"item": "cobblestone", "pos": [2, 64, 0]})
                self.assertEqual(result.error_code, "action_unconfirmed")
                self.assertTrue(result.data["executed"])
                self.assertTrue(_uncertain_result(result))
                self.assertEqual(sum(c[0] == "place" for c in world.calls), 1)
                self.assertEqual(result.data["cleanup_skipped"], "disconnected_unknown")
                self.assertFalse(any(c[0] == "stop" for c in world.calls))

    def test_prewrite_disconnect_and_next_execution_do_not_inherit_write_state(self):
        world = World()
        skills = SurvivalSkills(world)
        self.assertTrue(skills.execute("craft_item", {"item": "stick", "count": 1}).success)
        world.get_status = Mock(side_effect=APIError("not_connected", "not ready"))
        result = skills.execute("craft_item", {"item": "stick", "count": 1})
        self.assertEqual(result.error_code, "not_connected")
        self.assertFalse(_uncertain_result(result))
        self.assertEqual(sum(c[0] == "craft" for c in world.calls), 1)

    def test_daily_foliage_worker_exit_keeps_unknown_without_redig_or_stop(self):
        world = DailyWorld()
        world.status.update(is_day=True, time_of_day=1000)
        world.block([1, 64, 0], "oak_leaves", properties={"waterlogged": False}, can_see=True)
        def dig():
            world.block([1, 64, 0], "air")
            raise APIError("not_connected", "worker exited")
        world.effects["dig"] = dig
        result = DailySurvivalSkills(world).execute("clear_foliage", {"pos": [1, 64, 0], "expected_name": "oak_leaves"})
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertTrue(_uncertain_result(result))
        self.assertEqual(sum(c[0] == "dig" for c in world.calls), 1)
        self.assertFalse(any(c[0] == "stop" for c in world.calls))

    def test_daily_readonly_inspection_disconnect_is_not_a_prior_write(self):
        world = DailyWorld()
        world.block([1, 64, 0], "chest")
        world.effects["view"] = lambda: (_ for _ in ()).throw(APIError("not_connected", "unavailable"))
        result = DailySurvivalSkills(world).execute("inspect_container", {"pos": [1, 64, 0]})
        self.assertEqual(result.error_code, "not_connected")
        self.assertFalse(result.data["executed"])
        self.assertFalse(_uncertain_result(result))

    def test_container_inspection_tracks_its_real_approach_move(self):
        for failure in ("write_reply", "post_status", None):
            with self.subTest(failure=failure):
                world = DailyWorld()
                world.block([8, 64, 0], "chest")
                def move():
                    if failure == "write_reply":
                        raise APIError("not_connected", "worker exited during approach")
                    if failure == "post_status":
                        world.get_status = Mock(side_effect=APIError("not_connected", "approach verification disconnected"))
                    return envelope({"pos": world.status["pos"]})
                world.effects["move"] = move
                result = DailySurvivalSkills(world).execute("inspect_container", {"pos": [8, 64, 0]})
                self.assertTrue(result.data["executed"])
                self.assertEqual(sum(call[0] == "move" for call in world.calls), 1)
                self.assertFalse(any(call[0] == "stop" for call in world.calls))
                if failure:
                    self.assertEqual(result.error_code, "action_unconfirmed")
                    self.assertTrue(_uncertain_result(result))
                    self.assertEqual(result.data["cleanup_skipped"], "disconnected_unknown")
                    self.assertFalse(any(call[0] == "window_view" for call in world.calls))
                else:
                    self.assertTrue(result.success, result)
                    self.assertFalse(result.data["inventory_mutated"])

    def test_foliage_postread_wrapper_keeps_original_disconnection_and_no_stop(self):
        world = DailyWorld()
        world.status.update(is_day=True, time_of_day=1000)
        world.block([1, 64, 0], "oak_leaves", properties={"waterlogged": False}, can_see=True)
        def dig():
            world.block([1, 64, 0], "air")
            world.get_block = Mock(side_effect=APIError("not_connected", "world gone"))
            return envelope({"dug": "oak_leaves"})
        world.effects["dig"] = dig
        result = DailySurvivalSkills(world).execute("clear_foliage", {"pos": [1, 64, 0], "expected_name": "oak_leaves"})
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["api_details"]["api_code"], "not_connected")
        self.assertEqual(result.data["cleanup_skipped"], "disconnected_unknown")
        self.assertFalse(any(c[0] == "stop" for c in world.calls))

    def test_shelter_postwrite_disconnect_does_not_stop_or_place_again(self):
        client = ShelterClient()
        client.status["pos"] = [-0.5, 64, -0.5]
        def after_place(client):
            client.get_status = Mock(side_effect=APIError("not_connected", "worker exited"))
        client.after_place = after_place
        result = ShelterSkills(client).execute("build_shelter_step", {"pos": [0, 64, 0], "material": "cobblestone"})
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertTrue(result.data["executed"])
        self.assertTrue(_uncertain_result(result))
        self.assertEqual(result.data["cleanup_skipped"], "disconnected_unknown")
        self.assertEqual([call[0] for call in client.actions], ["place"])

    def test_shelter_prewrite_disconnect_does_not_become_unknown(self):
        client = ShelterClient()
        client.get_status = Mock(side_effect=APIError("not_connected", "worker starting"))
        result = ShelterSkills(client).execute("build_shelter_step", {"pos": [0, 64, 0], "material": "cobblestone"})
        self.assertEqual(result.error_code, "not_connected")
        self.assertFalse(_uncertain_result(result))
        self.assertFalse(client.actions)

    def test_sleep_prewrite_disconnect_keeps_ordinary_rule(self):
        client = SleepClient()
        client.status_error = "not_connected"
        result = AutoSleepSkill(client).execute()
        self.assertEqual(result.error_code, "disconnected")
        self.assertFalse(any(c[0] in {"move", "interact", "stop"} for c in client.calls))

    def test_sleep_postwrite_disconnect_is_unknown_without_cleanup_or_replay(self):
        client = SleepClient()
        original = client.move_to
        def move(*args, **kwargs):
            result = original(*args, **kwargs)
            client.status_error = "not_connected"
            return result
        client.move_to = move
        result = AutoSleepSkill(client).execute()
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertTrue(_uncertain_result(result))
        self.assertTrue(result.data["executed"])
        self.assertEqual(sum(c[0] == "move" for c in client.calls), 1)
        self.assertFalse(any(c[0] in {"interact", "stop"} for c in client.calls))


class WorkerExitHarnessTests(unittest.TestCase):
    def test_worker_exit_retains_pending_and_restart_never_replays(self):
        world = World()
        def craft(item, count):
            world.counts[item] = count
            raise APIError("not_connected", "worker exited after craft")
        world.craft_effect = craft
        self.assert_unknown_paused(world, SurvivalSkills(world),
                                   Decision("craft_item", {"item": "stick", "count": 4}, "Make sticks", "tools"), "craft")

    def test_batch_promotes_disconnected_child_marker_and_harness_never_stops(self):
        world = MaterialWorld()
        world.resource([2, 64, 0])
        def dig(pos):
            world.set_block(pos, "air")
            raise APIError("not_connected", "worker exited during batch dig")
        world.dig_effect = dig
        self.assert_unknown_paused(world, BatchSkills(world),
                                   Decision("gather_materials", {"resource": "logs", "count": 1}, "Get wood", "wood"), "dig")

    def test_container_approach_postread_disconnect_retains_pending_without_stop(self):
        world = DailyWorld()
        world.block([8, 64, 0], "chest")
        def move():
            world.get_status = Mock(side_effect=APIError("not_connected", "worker exited after approach"))
            return envelope({"pos": world.status["pos"]})
        world.effects["move"] = move
        self.assert_unknown_paused(world, DailySurvivalSkills(world),
                                   Decision("inspect_container", {"pos": [8, 64, 0]}, "Inspect chest", "storage"), "move")

    def assert_unknown_paused(self, world, skills, decision, write_name):
        world.username, world.base_url = "OfflineBot", "http://offline.invalid/api/v1"
        status = {**world.status, "is_day": True, "gamemode": "survival"}
        observed = {"status": status, "counts": {}, "inventory": [], "blocks": [], "entities": [], "fresh": True}
        collector = Mock()
        collector.collect.side_effect = lambda *_: copy.deepcopy(observed)
        planner = Mock()
        planner.progress.return_value = {}
        planner.choose.return_value = decision
        with tempfile.TemporaryDirectory() as temp:
            settings = {**copy.deepcopy(DEFAULTS["survival"]), "state_dir": temp}
            def create():
                return SurvivalHarness(world, settings, DEFAULTS["sleep"], planner=planner,
                                       skills=skills, collector=collector)
            harness = create()
            try:
                self.assertTrue(harness.request_step())
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    result = harness.tick(automatic=False)
                    if result is not None:
                        break
                    time.sleep(.002)
                else:
                    self.fail("Worker did not complete")
                self.assertEqual(result.error_code, "action_unconfirmed")
                self.assertEqual(result.data["cleanup_skipped"], "disconnected_unknown")
                self.assertEqual(result.data["api_details"]["api_code"], "not_connected")
                self.assertTrue(harness.paused)
                pending = copy.deepcopy(harness.journal.state["pending"])
                self.assertEqual(pending["decision"]["name"], decision.name)
                harness.tick()
                self.assertEqual(sum(c[0] == write_name for c in world.calls), 1)
                self.assertFalse(any(c[0] == "stop" for c in world.calls))
            finally:
                harness.close()
            restarted = create()
            try:
                restarted.tick()
                self.assertTrue(restarted.paused)
                self.assertEqual(restarted.journal.state["pending"], pending)
                self.assertEqual(sum(c[0] == write_name for c in world.calls), 1)
                self.assertEqual(planner.choose.call_count, 1)
            finally:
                restarted.close()
