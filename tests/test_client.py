"""Offline regressions for the mc-agent-api protocol boundary."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from core.client import APIError, MinecraftAgentClient


def response(data=None, status=200, json_error=None):
    result = Mock(status_code=status)
    if json_error is not None:
        result.json.side_effect = json_error
    else:
        result.json.return_value = data
    return result


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = MinecraftAgentClient("http://test.invalid/api/v1/", "TestBot", "test-password")
        self.request_patch = patch("core.client.requests.request")
        self.request = self.request_patch.start()
        self.addCleanup(self.request_patch.stop)

    def test_filtered_block_query_keeps_native_maturity_and_properties(self):
        rows = [{"name": "wheat", "pos": [1, 64, 2], "mature": True, "growth": [7, 7]}]
        self.request.return_value = response({"ok": True, "result": {"blocks": rows}})
        self.assertEqual(self.client.find_blocks(["wheat"], radius=12, limit=4, mature=True, properties={"age": 7}), rows)
        self.assertEqual(self.request.call_args.kwargs["json"],
                         {"types": ["wheat"], "radius": 12, "limit": 4, "mature": True, "properties": {"age": 7}})

    def test_window_wrappers_use_slash_routes_and_nested_target(self):
        self.request.return_value = response({"ok": True, "result": {"sections": {"container": []}}})
        self.client.window_view([1, 64, 2])
        self.assertTrue(self.request.call_args.args[1].endswith("/window/view"))
        self.assertEqual(self.request.call_args.kwargs["json"], {"target": {"pos": [1, 64, 2]}})
        self.assertGreaterEqual(self.request.call_args.kwargs["timeout"], 68)
        ops = [{"op": "put", "to": "fuel", "item": "coal", "count": 1}]
        self.client.window_act([1, 64, 2], ops, timeout_seconds=12)
        self.assertTrue(self.request.call_args.args[1].endswith("/window/act"))
        self.assertEqual(self.request.call_args.kwargs["json"], {"target": {"pos": [1, 64, 2]}, "ops": ops})
        self.assertEqual(self.request.call_args.kwargs["timeout"], 12)

    def test_window_partial_failure_keeps_done_and_is_sent_once(self):
        done = [{"op": "put", "put": "raw_iron", "count": 2, "to": "input"}]
        self.request.return_value = response({"ok": False, "error": {"code": "section_full", "message": "Fuel rejected", "done": done, "failed_op": 1}})
        with self.assertRaises(APIError) as caught:
            self.client.window_act([1, 64, 2], [{"op": "put", "item": "raw_iron", "count": 2, "to": "input"}])
        self.assertEqual(caught.exception.details["done"], done)
        self.assertEqual(self.request.call_count, 1)

    def test_attack_has_hurt_stop_and_collection_timeout_margin(self):
        self.request.return_value = response({"ok": True, "result": {"killed": False}})
        self.client.attack(12, timeout_ms=6000, max_hits=4)
        self.assertEqual(self.request.call_args.kwargs["json"],
                         {"entity_id": 12, "timeout": 6000, "max_hits": 4, "stop_on": ["hurt", "death"]})
        self.assertGreaterEqual(self.request.call_args.kwargs["timeout"], 44)
        self.assertEqual(self.request.call_count, 1)

    def test_login_waits_for_readiness_and_returns_status(self):
        status = {"ready": True, "pos": [0, 64, 0]}
        self.request.side_effect = [
            response({"ok": True, "token": "test-token"}),
            response({"ok": True, "result": {"ready": False}}),
            response({"ok": True, "result": status}),
        ]
        with patch("core.client.time.sleep"):
            self.assertEqual(self.client.login(), status)
        calls = self.request.call_args_list
        self.assertEqual(calls[0].args, ("POST", "http://test.invalid/api/v1/auth/login"))
        self.assertEqual(calls[0].kwargs["headers"], {})
        self.assertEqual(calls[1].kwargs["headers"], {"Authorization": "Bearer test-token"})
        self.assertEqual(self.client.token, "test-token")

    def test_ready_timeout_logs_out_and_clears_token(self):
        self.request.side_effect = [
            response({"ok": True, "token": "test-token"}),
            response({"ok": True}),
        ]
        with patch("core.client.time.monotonic", side_effect=[0, 2]):
            with self.assertRaises(APIError) as caught:
                self.client.login(max_wait_seconds=1)
        self.assertEqual(caught.exception.code, "ready_timeout")
        self.assertTrue(self.request.call_args.args[1].endswith("/auth/logout"))
        self.assertIsNone(self.client.token)

    def test_login_retries_only_startup_status_errors_until_ready(self):
        for code in ("not_connected", "not_ready", "wrong_server"):
            with self.subTest(code=code):
                self.client.token = None
                self.request.reset_mock()
                self.request.side_effect = [
                    response({"ok": True, "token": "test-token"}),
                    response({"ok": False, "error": {"code": code}}),
                    response({"ok": True, "result": {"ready": True}}),
                ]
                with patch("core.client.time.sleep"):
                    self.assertTrue(self.client.login()["ready"])
                self.assertEqual(self.request.call_count, 3)
                self.assertTrue(all(not call.args[1].endswith("/auth/logout") for call in self.request.call_args_list))

    def test_startup_status_errors_are_bounded_by_login_deadline(self):
        self.request.side_effect = [
            response({"ok": True, "token": "test-token"}),
            response({"ok": False, "error": {"code": "not_connected"}}),
            response({"ok": True}),
        ]
        with patch("core.client.time.monotonic", side_effect=[0, 0, 2, 2]):
            with self.assertRaises(APIError) as caught:
                self.client.login(max_wait_seconds=1)
        self.assertEqual(caught.exception.code, "ready_timeout")
        self.assertTrue(self.request.call_args.args[1].endswith("/auth/logout"))
        self.assertIsNone(self.client.token)

    def test_login_waits_for_exact_spawn_numeric_field_error_without_relogin(self):
        startup_error = {"ok": False, "error": {
            "code": "internal", "message": "Cannot read properties of undefined (reading 'toFixed')"}}
        self.request.side_effect = [
            response({"ok": True, "token": "test-token"}),
            response(startup_error), response(startup_error),
            response({"ok": True, "result": {"ready": True}}),
        ]
        with patch("core.client.time.sleep"):
            self.assertTrue(self.client.login()["ready"])
        paths = [call.args[1].rsplit("/", 1)[-1] for call in self.request.call_args_list]
        self.assertEqual(paths, ["login", "status", "status", "status"])
        self.assertEqual(self.client.token, "test-token")

    def test_spawn_numeric_field_error_still_expires_at_original_deadline(self):
        self.request.side_effect = [
            response({"ok": True, "token": "test-token"}),
            response({"ok": False, "error": {
                "code": "internal", "message": "Cannot read properties of undefined (reading 'toFixed')"}}),
            response({"ok": True}),
        ]
        with patch("core.client.time.monotonic", side_effect=[0, 0, 2, 2]):
            with self.assertRaises(APIError) as caught:
                self.client.login(max_wait_seconds=1)
        self.assertEqual(caught.exception.code, "ready_timeout")
        self.assertEqual([call.args[1].rsplit("/", 1)[-1] for call in self.request.call_args_list],
                         ["login", "status", "logout"])
        self.assertIsNone(self.client.token)

    def test_other_internal_errors_during_login_fail_and_logout_immediately(self):
        for message, status in (
            ("Cannot read properties of undefined (reading 'position')", 200),
            ("Cannot read properties of null (reading 'toFixed')", 200),
            ("Cannot read properties of undefined (reading 'toFixed')", 500),
        ):
            with self.subTest(message=message, status=status):
                self.request.reset_mock()
                self.request.side_effect = [
                    response({"ok": True, "token": "test-token"}),
                    response({"ok": False, "error": {"code": "internal", "message": message}}, status),
                    response({"ok": True}),
                ]
                with self.assertRaises(APIError) as caught:
                    self.client.login()
                self.assertEqual(caught.exception.code, "internal")
                self.assertEqual([call.args[1].rsplit("/", 1)[-1] for call in self.request.call_args_list],
                                 ["login", "status", "logout"])
                self.assertIsNone(self.client.token)

    def test_spawn_error_outside_login_is_not_retried(self):
        self.request.return_value = response({"ok": False, "error": {
            "code": "internal", "message": "Cannot read properties of undefined (reading 'toFixed')"}})
        with self.assertRaises(APIError) as caught:
            self.client.get_status()
        self.assertEqual(caught.exception.code, "internal")
        self.assertEqual(self.request.call_count, 1)

    def test_readiness_failure_preserves_error_when_cleanup_fails(self):
        self.request.side_effect = [
            response({"ok": True, "token": "test-token"}),
            response({"ok": False, "error": {"code": "invalid_token", "message": "expired"}}, 401),
            requests.ConnectionError("test-password test-token"),
        ]
        with self.assertRaises(APIError) as caught:
            self.client.login()
        self.assertEqual(caught.exception.code, "invalid_token")
        self.assertIsNone(self.client.token)
        self.assertEqual(self.request.call_count, 3)

    def test_login_rejects_missing_token(self):
        self.request.return_value = response({"ok": True})
        with self.assertRaises(APIError) as caught:
            self.client.login()
        self.assertEqual(caught.exception.code, "invalid_response")
        self.assertIsNone(self.client.token)

    def test_logout_clears_local_token_on_network_failure(self):
        self.client.token = "test-token"
        self.request.side_effect = requests.Timeout("test-token")
        with self.assertRaises(APIError):
            self.client.logout()
        self.assertIsNone(self.client.token)

    def test_business_error_is_not_an_empty_entity_list(self):
        self.request.return_value = response({"ok": False, "error": {"code": "wrong_server", "message": "not ready", "retry_after_s": 4}})
        with self.assertRaises(APIError) as caught:
            self.client.get_entities()
        error = caught.exception
        self.assertEqual(error.code, "wrong_server")
        self.assertEqual(error.status_code, 200)
        self.assertEqual(error.details, {"retry_after_s": 4})

    def test_recovery_reads_and_stop_honor_per_request_deadline(self):
        self.request.return_value = response({"ok": True, "result": {"entities": []}})
        operations = (
            lambda **kwargs: self.client.get_block([1, 64, 2], **kwargs),
            self.client.inventory,
            self.client.stop,
            lambda **kwargs: self.client.get_entities(radius=6, types=["item"], **kwargs),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                operation(timeout_seconds=0.25)
                self.assertEqual(self.request.call_args.kwargs["timeout"], 0.25)
                operation()
                self.assertEqual(self.request.call_args.kwargs["timeout"], self.client.timeout_seconds)
                with self.assertRaises(APIError) as caught:
                    operation(timeout_seconds=0)
                self.assertEqual(caught.exception.code, "bad_args")

    def test_unconfirmed_dig_preserves_server_diagnostics_without_retry(self):
        diagnostics = {"pos": [1.5, 64, 2.5], "eye_dist": 4.8, "on_ground": True, "ping_ms": 30}
        self.request.return_value = response({"ok": False, "error": {
            "code": "dig_unconfirmed", "message": "server did not confirm breaking oak_log", **diagnostics}})
        with self.assertRaises(APIError) as caught:
            self.client.dig([2, 65, 3])
        self.assertEqual(caught.exception.code, "dig_unconfirmed")
        self.assertEqual(caught.exception.details, diagnostics)
        self.assertEqual(self.request.call_count, 1)

    def test_http_error_is_not_an_empty_block_list(self):
        self.request.return_value = response({"ok": False, "error": {"code": "unknown_api"}}, 404)
        with self.assertRaises(APIError) as caught:
            self.client.get_blocks()
        self.assertEqual((caught.exception.code, caught.exception.status_code), ("unknown_api", 404))

    def test_non_json_http_failure_has_structured_error(self):
        self.request.return_value = response(status=502, json_error=ValueError("private upstream response"))
        with self.assertRaises(APIError) as caught:
            self.client.get_status()
        self.assertEqual((caught.exception.code, caught.exception.status_code), ("invalid_json", 502))
        self.assertNotIn("private upstream", str(caught.exception))

    def test_invalid_envelopes_do_not_become_success(self):
        for data in (None, [], {}, {"ok": "true"}):
            with self.subTest(data=data):
                self.request.return_value = response(data)
                with self.assertRaises(APIError) as caught:
                    self.client.get_status()
                self.assertEqual(caught.exception.code, "invalid_response")

    def test_malformed_success_lists_do_not_become_empty_results(self):
        self.request.return_value = response({"ok": True, "result": {}})
        for method in (self.client.get_entities, self.client.get_events, self.client.get_blocks):
            with self.subTest(method=method.__name__), self.assertRaises(APIError) as caught:
                method()
            self.assertEqual(caught.exception.code, "invalid_response")

    def test_write_timeout_is_not_retried_or_leaked(self):
        self.client.token = "test-token"
        self.request.side_effect = requests.Timeout("test-password test-token")
        with self.assertRaises(APIError) as caught:
            self.client.interact(pos=[1, 64, 1])
        self.assertEqual(caught.exception.code, "request_timeout")
        self.assertEqual(caught.exception.details, {"action_state": "unknown"})
        self.assertEqual(self.request.call_count, 1)
        self.assertNotIn("test-password", str(caught.exception))
        self.assertNotIn("test-token", str(caught.exception))

    def test_network_failure_is_structured_without_request_credentials(self):
        self.request.side_effect = requests.ConnectionError("test-password")
        with self.assertRaises(APIError) as caught:
            self.client.chat("hello")
        self.assertEqual(caught.exception.code, "network_error")
        self.assertNotIn("test-password", str(caught.exception))

    def test_server_error_message_redacts_credentials(self):
        self.client.token = "test-token"
        self.request.return_value = response({"ok": False, "error": {"code": "auth_error", "message": "test-password test-token"}}, 401)
        with self.assertRaises(APIError) as caught:
            self.client.get_status()
        self.assertNotIn("test-password", str(caught.exception))
        self.assertNotIn("test-token", str(caught.exception))

    def test_typed_block_query_preserves_coordinates_and_properties(self):
        blocks = [{"name": "red_bed", "pos": [1, 64, 2], "dist": 2, "properties": {"part": "head"}}]
        self.request.return_value = response({"ok": True, "result": {"blocks": blocks}})
        self.assertEqual(self.client.get_blocks(types=["red_bed"]), blocks)
        self.assertEqual(self.request.call_args.args, ("POST", "http://test.invalid/api/v1/find_blocks"))
        self.assertEqual(self.request.call_args.kwargs["json"], {"radius": 16, "limit": 64, "types": ["red_bed"]})

    def test_block_summary_becomes_compatible_nearest_per_type_rows(self):
        self.request.return_value = response({"ok": True, "result": {"blocks": {"stone": {"nearest": [1, 63, 2], "dist": 3.1, "count": 20}}}})
        self.assertEqual(self.client.get_blocks(), [{"name": "stone", "pos": [1, 63, 2], "dist": 3.1, "count": 20}])
        self.assertNotIn("types", self.request.call_args.kwargs["json"])

    def test_entity_query_uses_supported_post_route_and_filter(self):
        entities = [{"id": 1, "type": "hostile", "name": "zombie"}]
        self.request.return_value = response({"ok": True, "result": {"entities": entities}})
        self.assertEqual(self.client.get_entities(8, ["hostile"]), entities)
        self.assertEqual(self.request.call_args.args, ("POST", "http://test.invalid/api/v1/get_entities"))
        self.assertEqual(self.request.call_args.kwargs["json"], {"radius": 8, "types": ["hostile"]})

    def test_events_page_preserves_cursor_and_list_convenience(self):
        envelope = {"ok": True, "result": {"events": [{"seq": 11}], "last_seq": 11, "has_more": True, "total": 7}}
        self.request.return_value = response(envelope)
        self.assertEqual(self.client.get_events_page(10, 1), envelope)
        self.assertEqual(self.request.call_args.kwargs["params"], {"since": 10, "limit": 1})
        self.assertEqual(self.client.get_events(10, 1), [{"seq": 11}])

    def test_move_sends_backend_deadline_and_waits_longer_than_it(self):
        envelope = {"ok": True, "result": {"pos": [1, 64, 2]}}
        self.request.return_value = response(envelope)
        self.assertEqual(self.client.move_to(1, 64, 2, timeout_ms=35000, range=2), envelope)
        call = self.request.call_args
        self.assertEqual(call.kwargs["json"], {"x": 1, "y": 64, "z": 2, "timeout": 35000, "range": 2})
        self.assertGreaterEqual(call.kwargs["timeout"], 40)

    def test_interaction_default_wait_covers_backend_pathfinding(self):
        self.request.return_value = response({"ok": True, "result": {"slept": True}})
        self.client.interact(pos=[1, 64, 2])
        self.assertGreaterEqual(self.request.call_args.kwargs["timeout"], 65)

    def test_explicit_interaction_timeout_respects_skill_budget(self):
        self.request.return_value = response({"ok": True, "result": {"slept": True}})
        self.client.interact(pos=[1, 64, 2], timeout_seconds=2.5)
        self.assertEqual(self.request.call_args.kwargs["timeout"], 2.5)

    def test_look_inventory_and_stop_keep_envelopes(self):
        envelope = {"ok": True, "result": {}}
        self.request.return_value = response(envelope)
        methods = [(lambda: self.client.look_at(1, 64, 2), "POST", "look"), (self.client.inventory, "GET", "inventory"), (self.client.stop, "POST", "stop")]
        for method, verb, endpoint in methods:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(method(), envelope)
                self.assertEqual(self.request.call_args.args, (verb, f"http://test.invalid/api/v1/{endpoint}"))

    def test_root_client_is_shared_and_sleep_approaches_with_range_two(self):
        from client import MinecraftAgentClient as LegacyClient

        self.assertIs(LegacyClient, MinecraftAgentClient)
        self.request.side_effect = [response({"ok": True, "result": {}}), response({"ok": True, "result": {"slept": True}})]
        self.assertTrue(self.client.sleep_in_bed([1, 64, 2])["result"]["slept"])
        self.assertEqual(self.request.call_args_list[0].kwargs["json"]["range"], 2)
        self.assertTrue(self.request.call_args_list[1].args[1].endswith("/interact"))

    def test_nearest_block_helper_selects_nearest_matching_name(self):
        with patch.object(self.client, "get_blocks", return_value=[
            {"name": "stone", "dist": 1, "pos": [0, 0, 0]},
            {"name": "blue_bed", "dist": 5, "pos": [1, 0, 0]},
            {"name": "red_bed", "dist": 2, "pos": [2, 0, 0]},
        ]):
            self.assertEqual(self.client.find_nearest_block()["name"], "red_bed")

    def test_invalid_timeout_is_rejected_before_network(self):
        for value in (0, -1, True, float("nan")):
            with self.subTest(value=value), self.assertRaises(APIError):
                self.client.move_to(1, 64, 2, timeout_ms=value)
        self.request.assert_not_called()


class OptionalDependencyTests(unittest.TestCase):
    def test_core_and_debug_exports_do_not_import_optional_rcon(self):
        code = """
import builtins
original = builtins.__import__
def checked(name, *args, **kwargs):
    if name == 'mcrcon':
        raise AssertionError('optional RCON imported')
    return original(name, *args, **kwargs)
builtins.__import__ = checked
from core import APIError, MinecraftAgentClient, TelemetryManager, MemoryEngine
from debug import FlightRecorder, ChatDebugger
from client import MinecraftAgentClient as LegacyClient
assert LegacyClient is MinecraftAgentClient
"""
        completed = subprocess.run([sys.executable, "-B", "-c", code], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
