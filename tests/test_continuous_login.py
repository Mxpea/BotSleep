import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.client import APIError, MinecraftAgentClient
from core.config import ConfigError, continuous_daily, load_config
from main import _connect_continuously


class ContinuousLoginTests(unittest.TestCase):
    def test_config_is_opt_in_and_scoped_to_daily_survival(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            for profile, enabled in (("objective: daily", False), ("objective: daily, continuous: true", True),
                                     ("objective: starter, continuous: true", False),
                                     ("objective: daily, intent: speedrun, continuous: true", False)):
                path.write_text("survival: {" + profile + "}", encoding="utf-8")
                self.assertEqual(continuous_daily(load_config(path, environ={})["survival"]), enabled)
            path.write_text("survival: {continuous: 'true'}", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path, environ={})

    def test_cooldown_keeps_same_session_then_resumes_after_server_delay(self):
        client = MinecraftAgentClient()
        client.token = "original"
        ready = {"ok": True, "result": {"ready": True, "alive": True}}
        cooldown = APIError("death_loop_cooldown", "bot is in death loop cooldown, 298s remaining")
        with (patch.object(client, "get_status", side_effect=[APIError("not_connected", "offline"), ready]),
              patch.object(client, "connect", side_effect=cooldown) as connect,
              patch.object(client, "_open_session") as login, patch("main.time.sleep") as sleep):
            self.assertEqual(_connect_continuously(client, None, Mock()), ready["result"])
        sleep.assert_called_once_with(298)
        connect.assert_called_once_with()
        login.assert_not_called()
        self.assertEqual(client.token, "original")

    def test_expired_token_reauthenticates_once_and_reuses_the_new_token(self):
        client = MinecraftAgentClient()
        client.token = "expired"
        with (patch.object(client, "get_status", side_effect=[APIError("unauthorized", "expired"),
                    {"ok": True, "result": {"ready": True}}]),
              patch.object(client, "_request", return_value={"ok": True, "token": "new"}) as request,
              patch("main.time.sleep")):
            self.assertTrue(_connect_continuously(client, None, Mock())["ready"])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args, ("POST", "auth/login"))
        self.assertEqual(client.token, "new")

    def test_rate_limit_and_authentication_errors_have_different_recovery(self):
        client = Mock()
        client.connect_continuous.side_effect = [
            APIError("rate_limited", "busy", 429, {"retry_after_s": 75}),
            APIError("invalid_credentials", "bad credentials", 401)]
        with patch("main.time.sleep") as sleep, self.assertRaisesRegex(APIError, "invalid_credentials"):
            _connect_continuously(client, None, Mock())
        sleep.assert_called_once_with(75)

    def test_session_refresh_during_run_updates_configured_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "session.json"
            client = MinecraftAgentClient()
            client.token = "previous"
            client.save_session(cache)
            client.token = None
            with (patch.object(client, "get_status", side_effect=[{"ok": True, "result": {"ready": True}},
                                                                  APIError("invalid_token", "expired")]),
                  patch.object(client, "_request", return_value={"ok": True, "token": "refreshed"})):
                self.assertTrue(client.connect_continuous(session_file=cache)["ready"])
                with self.assertRaisesRegex(APIError, "not_ready"):
                    client.connect_continuous()
            restored = MinecraftAgentClient()
            self.assertTrue(restored.load_session(cache))
            self.assertEqual(restored.token, "refreshed")
