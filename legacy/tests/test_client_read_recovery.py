"""Keep-alive recovery must not extend a caller deadline or replay writes."""

import unittest
from unittest.mock import Mock, patch

import requests

from core.client import APIError, MinecraftAgentClient


def response(result=None):
    reply = Mock(status_code=200)
    reply.json.return_value = {"ok": True, "result": result or {}}
    return reply


class ReadRecoveryTests(unittest.TestCase):
    def test_native_read_routes_recover_once_with_remaining_budget(self):
        for method, args, kwargs, route, result in (
            ("get_status", (), {"timeout_seconds": 2}, "status", {"ready": True}),
            ("inventory", (), {"timeout_seconds": 2}, "inventory", {"items": []}),
            ("get_block", ([1, 64, 2],), {"timeout_seconds": 2}, "get_block", {"name": "stone"}),
            ("find_blocks", (["stone"],), {}, "find_blocks", {"blocks": []}),
        ):
            with self.subTest(route=route):
                first, fresh = Mock(), Mock()
                first.request.side_effect = requests.ConnectionError("socket reset")
                fresh.request.return_value = response(result)
                with patch("core.client.requests.Session", side_effect=[first, fresh]), \
                        patch("core.client.time.monotonic", side_effect=[10, 10.75]):
                    client = MinecraftAgentClient(timeout_seconds=2)
                    getattr(client, method)(*args, **kwargs)
                self.assertEqual(first.request.call_count, 1)
                first.close.assert_called_once_with()
                self.assertEqual(fresh.request.call_count, 1)
                self.assertTrue(fresh.request.call_args.args[1].endswith("/" + route))
                self.assertAlmostEqual(fresh.request.call_args.kwargs["timeout"], 1.25)

    def test_exhausted_read_budget_does_not_retry(self):
        first = Mock()
        first.request.side_effect = requests.ConnectionError("socket reset")
        with patch("core.client.requests.Session", return_value=first) as factory, \
                patch("core.client.time.monotonic", side_effect=[10, 12]):
            client = MinecraftAgentClient(timeout_seconds=2)
            with self.assertRaises(APIError) as caught:
                client.get_status()
        self.assertEqual(caught.exception.code, "request_timeout")
        self.assertEqual(first.request.call_count, 1)
        self.assertEqual(factory.call_count, 1)

    def test_read_recovery_is_bounded_to_one_retry(self):
        first, fresh = Mock(), Mock()
        first.request.side_effect = requests.ConnectionError("first")
        fresh.request.side_effect = requests.ConnectionError("second")
        with patch("core.client.requests.Session", side_effect=[first, fresh]) as factory:
            client = MinecraftAgentClient()
            with self.assertRaises(APIError) as caught:
                client.get_status()
        self.assertEqual(caught.exception.code, "network_error")
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(fresh.request.call_count, 1)

    def test_write_and_window_connection_failures_never_retry(self):
        for method, args in (("dig", ([1, 64, 2], False)), ("window_view", ([1, 64, 2],)),
                             ("window_act", ([1, 64, 2], [{"op": "put", "to": "fuel", "item": "coal", "count": 1}])),
                             ("chat", ("test",))):
            with self.subTest(method=method):
                session = Mock()
                session.request.side_effect = requests.ConnectionError("socket reset")
                with patch("core.client.requests.Session", return_value=session) as factory:
                    client = MinecraftAgentClient()
                    with self.assertRaises(APIError) as caught:
                        getattr(client, method)(*args)
                self.assertEqual(caught.exception.code, "network_error")
                self.assertEqual(session.request.call_count, 1)
                self.assertEqual(factory.call_count, 1)

    def test_known_http_failure_does_not_retry(self):
        session = Mock()
        reply = response()
        reply.json.return_value = {"ok": False, "error": {"code": "not_ready", "message": "wait"}}
        session.request.return_value = reply
        with patch("core.client.requests.Session", return_value=session) as factory:
            client = MinecraftAgentClient()
            with self.assertRaises(APIError) as caught:
                client.get_status()
        self.assertEqual(caught.exception.code, "not_ready")
        self.assertEqual(session.request.call_count, 1)
        self.assertEqual(factory.call_count, 1)


if __name__ == "__main__":
    unittest.main()
