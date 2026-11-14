"""Offline configuration contract and validation tests."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from core.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "config.yaml"

    def write(self, config):
        self.path.write_text(yaml.safe_dump(config), encoding="utf-8")
        return self.path

    def test_empty_file_uses_defaults_without_creating_runtime_files(self):
        self.path.write_text("# intentionally empty\n", encoding="utf-8")
        config = load_config(self.path, environ={})
        self.assertEqual(config["agent"], {"name": "SleepBot", "password": "", "mode": "sleep"})
        self.assertEqual(config["mc_api"]["timeout_seconds"], 15)
        self.assertEqual(config["mc_api"]["login_timeout_seconds"], 30)
        self.assertTrue(config["sleep"]["enabled"])
        self.assertFalse(config["debug"]["enable_in_game_commands"])
        self.assertEqual(config["debug"]["allowed_usernames"], [])
        self.assertFalse(config["rcon"]["enabled"])
        self.assertEqual(config["debug"]["flight_recorder"]["output_dir"], str(self.root / "logs"))
        self.assertFalse((self.root / "logs").exists())

    def test_partial_nested_yaml_keeps_unspecified_defaults(self):
        self.write({"sleep": {"radius": 32}, "debug": {"flight_recorder": {"enabled": False}}})
        config = load_config(self.path, environ={})
        self.assertEqual(config["sleep"]["radius"], 32)
        self.assertTrue(config["sleep"]["threat_check"])
        self.assertFalse(config["debug"]["flight_recorder"]["enabled"])
        self.assertEqual(config["debug"]["command_prefix"], "#")

    def test_survival_defaults_and_world_partition_are_offline(self):
        config = load_config(self.write({"agent": {"mode": "survival"},
                                         "survival": {"world_id": "normal-world"}}), environ={})
        self.assertEqual(config["survival"]["scan_radius"], 24)
        self.assertEqual(config["survival"]["state_dir"], str(self.root / "runtime/survival"))
        self.assertFalse((self.root / "runtime").exists())

    def test_survival_rejects_unsafe_or_mistyped_limits(self):
        for changes in ({"max_steps": True}, {"max_deaths": 0}, {"scan_radius": 65},
                        {"world_id": "../private"}, {"explore_distance": 33}, {"food_threshold": 21},
                        {"retry_seconds": float("inf")}, {"unknown": "secret"}):
            with self.subTest(changes=changes), self.assertRaises(ConfigError):
                load_config(self.write({"survival": changes}), environ={})
        with self.assertRaises(ConfigError):
            load_config(self.write({"agent": {"mode": "invalid"}}), environ={})

    def test_unknown_sleep_options_are_rejected_without_echoing_values(self):
        self.write({"sleep": {"max_retries": "private-config-value"}})
        with self.assertRaises(ConfigError) as caught:
            load_config(self.path, environ={})
        self.assertIn("unsupported", str(caught.exception))
        self.assertNotIn("private-config-value", str(caught.exception))

    def test_threat_distance_matches_skill_upper_bound(self):
        self.write({"sleep": {"threat_distance": 61}})
        self.assertEqual(load_config(self.path, environ={})["sleep"]["threat_distance"], 61)
        self.write({"sleep": {"threat_distance": 61.01}})
        with self.assertRaises(ConfigError):
            load_config(self.path, environ={})

    def test_cli_over_environment_over_yaml_and_empty_password_override(self):
        self.write({
            "agent": {"name": "YamlBot", "password": "dummy-yaml-secret"},
            "mc_api": {"base_url": "http://yaml.invalid/api/v1"},
        })
        env = {
            "MC_AGENT_USER": "EnvBot",
            "MC_AGENT_PASSWORD": "",
            "MC_AGENT_API": "https://env.invalid/api/v1/",
        }
        config = load_config(self.path, environ=env, username="CliBot")
        self.assertEqual(config["agent"]["name"], "CliBot")
        self.assertEqual(config["agent"]["password"], "")
        self.assertEqual(config["mc_api"]["base_url"], "https://env.invalid/api/v1")
        self.assertEqual(load_config(self.path, environ=env)["agent"]["name"], "EnvBot")
        self.assertEqual(load_config(self.path, environ={})["agent"]["name"], "YamlBot")

    def test_environ_none_reads_process_environment_but_empty_mapping_does_not(self):
        self.write({})
        with patch.dict(os.environ, {"MC_AGENT_USER": "EnvBot"}, clear=True):
            self.assertEqual(load_config(self.path)["agent"]["name"], "EnvBot")
            self.assertEqual(load_config(self.path, environ={})["agent"]["name"], "SleepBot")

    def test_config_path_selection_belongs_to_caller(self):
        self.write({"agent": {"name": "FileBot"}})
        config = load_config(self.path, environ={"BOTSLEEP_CONFIG": "missing.yaml"})
        self.assertEqual(config["agent"]["name"], "FileBot")

    def test_log_directory_resolves_against_config_directory(self):
        self.write({"debug": {"flight_recorder": {"output_dir": "runtime/../flight"}}})
        config = load_config(self.path, environ={})
        self.assertEqual(config["debug"]["flight_recorder"]["output_dir"], str(self.root / "flight"))

    def test_absolute_log_directory_is_preserved(self):
        output = self.root / "absolute-logs"
        self.write({"debug": {"flight_recorder": {"output_dir": str(output)}}})
        config = load_config(self.path, environ={})
        self.assertEqual(config["debug"]["flight_recorder"]["output_dir"], str(output))

    def test_loading_does_not_share_mutable_defaults(self):
        self.write({})
        config = load_config(self.path, environ={})
        config["debug"]["allowed_usernames"].append("PlayerOne")
        config["sleep"]["radius"] = 1
        fresh = load_config(self.path, environ={})
        self.assertEqual(fresh["debug"]["allowed_usernames"], [])
        self.assertEqual(fresh["sleep"]["radius"], 16)

    def test_missing_file_and_directory_are_clear_errors(self):
        with self.assertRaisesRegex(ConfigError, "does not exist"):
            load_config(self.root / "missing.yaml", environ={})
        with self.assertRaisesRegex(ConfigError, "could not be read"):
            load_config(self.root, environ={})

    def test_malformed_yaml_does_not_echo_secret(self):
        secret = "UNIQUE_TEST_SECRET_123"
        self.path.write_text(f'agent:\n  password: "{secret}\n', encoding="utf-8")
        with self.assertRaises(ConfigError) as error:
            load_config(self.path, environ={})
        self.assertIn("not valid YAML", str(error.exception))
        self.assertNotIn(secret, str(error.exception))

    def test_safe_loader_rejects_python_object_tags(self):
        self.path.write_text("!!python/object:unittest.TestCase {}", encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "not valid YAML"):
            load_config(self.path, environ={})

    def test_invalid_root_and_section_shapes(self):
        values = [[], "text", 42, {1: "value"}]
        values += [{key: value} for key in ("mc_api", "agent", "sleep", "debug", "llm", "rcon")
                   for value in (None, [], "text")]
        values.append({"debug": {"flight_recorder": []}})
        for value in values:
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaisesRegex(ConfigError, "mapping"):
                    load_config(self.path, environ={})

    def test_usernames_reject_non_ascii_invalid_lengths_and_punctuation(self):
        for name in ("", "ab", "a" * 17, "with-dash", "with space", "用户名", 123, None, "Abc\n"):
            with self.subTest(name=name):
                self.write({"agent": {"name": name}})
                with self.assertRaisesRegex(ConfigError, "agent.name"):
                    load_config(self.path, environ={})
        for name in ("abc", "A_1", "a" * 16):
            with self.subTest(valid_name=name):
                self.write({"agent": {"name": name}})
                self.assertEqual(load_config(self.path, environ={})["agent"]["name"], name)

    def test_invalid_cli_and_environment_overrides_are_validated(self):
        self.write({})
        for kwargs in ({"username": ""}, {"username": "bad name"},
                       {"environ": {"MC_AGENT_USER": ""}}, {"environ": {"MC_AGENT_API": ""}}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ConfigError):
                    load_config(self.path, **{"environ": {}, **kwargs})

    def test_password_requires_string_but_can_be_empty(self):
        for password in (None, 123, False, ["dummy-secret"]):
            with self.subTest(password_type=type(password).__name__):
                self.write({"agent": {"password": password}})
                with self.assertRaisesRegex(ConfigError, "agent.password"):
                    load_config(self.path, environ={})

    def test_api_url_validation_and_secret_redaction(self):
        for url in ("", " ", "localhost:18900", "ftp://example.invalid", "http://",
                    "http://host.invalid:99999", "http://host.invalid:bad",
                    "http://host.invalid:0", "http://bad host.invalid", "http://[broken",
                    "http://user:UNIQUE_TEST_SECRET_123@host.invalid", "http://host.invalid?token=secret",
                    "http://host.invalid/#fragment"):
            with self.subTest(url_category="invalid"):
                self.write({"mc_api": {"base_url": url}})
                with self.assertRaises(ConfigError) as error:
                    load_config(self.path, environ={})
                self.assertNotIn("UNIQUE_TEST_SECRET_123", str(error.exception))

    def test_positive_numbers_reject_bool_nan_infinity_zero_and_strings(self):
        fields = {
            "mc_api": ("timeout_seconds", "login_timeout_seconds"),
            "sleep": ("threat_distance", "poll_interval_seconds", "retry_seconds", "timeout_seconds",
                      "move_timeout_seconds", "confirm_timeout_seconds"),
        }
        for section, names in fields.items():
            for name in names:
                for value in (0, -1, True, "1", float("nan"), float("inf"), -float("inf")):
                    with self.subTest(section=section, name=name, value=value):
                        self.write({section: {name: value}})
                        with self.assertRaisesRegex(ConfigError, "finite positive"):
                            load_config(self.path, environ={})

    def test_boolean_fields_require_actual_booleans(self):
        paths = (
            ("sleep", "enabled"), ("sleep", "threat_check"),
            ("debug", "enable_in_game_commands"), ("debug", "flight_recorder", "enabled"),
            ("rcon", "enabled"), ("llm", "enabled"), ("agent", "reflex_enabled"),
        )
        for path in paths:
            for value in (0, 1, "false", "true", None):
                nested = value
                for key in reversed(path):
                    nested = {key: nested}
                with self.subTest(path=path, value=value):
                    self.write(nested)
                    with self.assertRaisesRegex(ConfigError, "boolean"):
                        load_config(self.path, environ={})

    def test_radius_has_inclusive_integer_bounds(self):
        for radius in (0, 65, -1, 1.5, True, "16", None):
            with self.subTest(radius=radius):
                self.write({"sleep": {"radius": radius}})
                with self.assertRaisesRegex(ConfigError, "sleep.radius"):
                    load_config(self.path, environ={})
        for radius in (1, 64):
            self.write({"sleep": {"radius": radius}})
            self.assertEqual(load_config(self.path, environ={})["sleep"]["radius"], radius)

    def test_debug_allowlist_requires_explicit_valid_player_names(self):
        for allowed in ("PlayerOne", None, ["x"], ["Player One"], [False]):
            with self.subTest(allowed=allowed):
                self.write({"debug": {"allowed_usernames": allowed}})
                with self.assertRaisesRegex(ConfigError, "allowed_usernames"):
                    load_config(self.path, environ={})
        self.write({"debug": {"allowed_usernames": ["PlayerOne", "Player_2"]}})
        self.assertEqual(load_config(self.path, environ={})["debug"]["allowed_usernames"], ["PlayerOne", "Player_2"])

    def test_prefix_and_output_directory_reject_invalid_values(self):
        for prefix in ("", " ", "# ", None):
            self.write({"debug": {"command_prefix": prefix}})
            with self.assertRaisesRegex(ConfigError, "command_prefix"):
                load_config(self.path, environ={})
        for output in ("", None, False, "bad\x00path"):
            self.write({"debug": {"flight_recorder": {"output_dir": output}}})
            with self.assertRaisesRegex(ConfigError, "output_dir"):
                load_config(self.path, environ={})

    def test_legacy_llm_and_rcon_fields_remain_available(self):
        self.write({
            "llm": {"model": "old-model", "api_key": "dummy-key"},
            "rcon": {"host": "localhost", "port": 25575, "password": "dummy-password"},
        })
        config = load_config(self.path, environ={})
        self.assertEqual(config["llm"]["model"], "old-model")
        self.assertEqual(config["rcon"]["port"], 25575)
        self.assertFalse(config["rcon"]["enabled"])

    def test_disabled_legacy_empty_model_config_still_loads(self):
        config = load_config(self.write({"llm": {"enabled": False, "model": "", "base_url": ""}}), environ={})
        self.assertFalse(config["llm"]["enabled"])
        with self.assertRaises(ConfigError):
            load_config(self.write({"llm": {"enabled": True, "model": "", "base_url": ""}}), environ={})

    def test_model_environment_overrides_are_validated_without_echoing_secrets(self):
        config = load_config(self.write({"llm": {"enabled": True}}), environ={
            "BOTSLEEP_LLM_API_KEY": "test-private-key", "BOTSLEEP_LLM_MODEL": "default",
            "BOTSLEEP_LLM_BASE_URL": "https://model.invalid/v1/"})
        self.assertEqual(config["llm"]["model"], "default")
        self.assertEqual(config["llm"]["base_url"], "https://model.invalid/v1")
        self.assertEqual(config["llm"]["api_key"], "test-private-key")
        with self.assertRaises(ConfigError) as caught:
            load_config(self.path, environ={"BOTSLEEP_LLM_BASE_URL": "https://user:test-private-key@model.invalid"})
        self.assertNotIn("test-private-key", str(caught.exception))

    def test_model_and_monitor_limits_reject_invalid_values(self):
        for supplied in ({"llm": {"temperature": 10 ** 1000}}, {"llm": {"max_tokens": True}},
                         {"llm": {"max_history_turns": 0}}, {"llm": {"max_repair_attempts": 3}},
                         {"llm": {"tls_version": 1.2}}, {"llm": {"api_key": 123}},
                         {"debug": {"monitor": {"host": "0.0.0.0"}}},
                         {"debug": {"monitor": {"port": 0}}},
                         {"debug": {"monitor": {"heartbeat_seconds": float("nan")}}}):
            with self.subTest(supplied=supplied), self.assertRaises(ConfigError):
                load_config(self.write(supplied), environ={})

    def test_example_configuration_loads(self):
        example = Path(__file__).resolve().parents[1] / "config.example.yaml"
        config = load_config(example, environ={})
        self.assertTrue(config["sleep"]["enabled"])
        self.assertFalse(config["rcon"]["enabled"])
        self.assertFalse(config["llm"]["enabled"])


if __name__ == "__main__":
    unittest.main()
