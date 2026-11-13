"""Regression checks for the real engine, coordinate conventions, and capture."""

from dataclasses import asdict
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from integrations.ninjabrain.calculator import CalculationError, EyeThrow, JAR_PATH, StrongholdCalculator
from integrations.ninjabrain.observations import EyeObservationCollector, ObservationError, fit_trajectory
from skills.stronghold import CalculateStrongholdSkill
from core.client import MinecraftAgentClient, MinecraftAPIError


class ObservationTests(unittest.TestCase):
    def test_vanilla_yaw_all_four_quadrants(self):
        for point, expected in (([0, 64, 10], 0), ([-10, 64, 0], 90), ([0, 64, -10], -180), ([10, 64, 0], -90)):
            with self.subTest(point=point):
                self.assertAlmostEqual(EyeThrow.from_positions([0, 64, 0], point).bearing_deg, expected)

    def test_fits_direction_without_player_hand_offset(self):
        # Known eastward path anchored 0.3 blocks off the player's Z coordinate.
        eye, quality = fit_trajectory([[i, 65 + i / 4, .3] for i in (1, 1, 3, 6, 10)])
        self.assertAlmostEqual(eye.bearing_deg, -90)
        self.assertAlmostEqual(eye.z, .3)
        self.assertEqual(quality["unique_samples"], 4)
        self.assertGreater(eye.sigma_deg, .05)

    def test_negative_quadrant_trajectory(self):
        eye, _ = fit_trajectory([[100 - i, 70, 100 - i] for i in (0, 2, 4, 8)], .1)
        self.assertAlmostEqual(eye.bearing_deg, 135)

    def test_rejects_short_curved_and_backward_tracks(self):
        bad = (
            [[0, 0, 0], [1, 0, 0], [2, 0, 0]],
            [[0, 0, 0], [3, 0, 4], [6, 0, 0]],
            [[0, 0, 0], [8, 0, 0], [4, 0, 0]],
            [[0, 0, 0], [0, 4, 0], [0, 8, 0]],
        )
        for positions in bad:
            with self.subTest(positions=positions), self.assertRaises(ObservationError):
                fit_trajectory(positions)

    def test_rejects_nonfinite_and_zero_uncertainty(self):
        for values in ((float("nan"), 0, 0, .1), (0, 0, float("inf"), .1), (0, 0, 0, 0), (True, 0, 0, .1)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                EyeThrow(*values)


class ApiTests(unittest.TestCase):
    @patch("core.client.requests.request")
    def test_entities_use_post_and_preserve_filters(self, request):
        request.return_value.json.return_value = {"ok": True, "result": {"entities": [{"id": 12}]}}
        client = MinecraftAgentClient()
        client.token = "test-token"
        self.assertEqual(client.get_entities(64, ["eye_of_ender"]), [{"id": 12}])
        args, kwargs = request.call_args
        self.assertEqual(args, ("POST", client.base_url + "/get_entities"))
        self.assertEqual(kwargs["json"], {"radius": 64, "types": ["eye_of_ender"]})

    @patch("core.client.requests.request")
    def test_business_error_is_not_empty_entities(self, request):
        request.return_value.json.return_value = {"ok": False, "error": {"code": "wrong_server"}}
        with self.assertRaises(MinecraftAPIError) as error:
            MinecraftAgentClient().get_entities()
        self.assertEqual(error.exception.code, "wrong_server")

    @patch("core.client.requests.request")
    def test_use_item_error_is_not_a_successful_throw(self, request):
        request.return_value.json.return_value = {"ok": False, "error": {"code": "missing_item"}}
        with self.assertRaises(MinecraftAPIError):
            MinecraftAgentClient().use_item("ender_eye")

    @patch("core.client.requests.request")
    def test_null_success_result_is_an_explicit_error(self, request):
        request.return_value.json.return_value = {"ok": True, "result": None}
        with self.assertRaises(MinecraftAPIError):
            MinecraftAgentClient().get_entities()

    @patch("core.client.requests.request")
    def test_malformed_entities_are_rejected(self, request):
        request.return_value.json.return_value = {"ok": True, "result": {}}
        with self.assertRaises(MinecraftAPIError):
            MinecraftAgentClient().get_entities()


class CaptureTests(unittest.TestCase):
    def make_client(self):
        client = Mock()
        client.get_status.return_value = {"ok": True, "result": {
            "ready": True, "alive": True, "dimension": "overworld", "pos": [0, 64, 0], "busy": None}}
        client.get_inventory.return_value = {"items": [{"name": "ender_eye", "count": 2}]}
        client.use_item.return_value = {"ok": True, "result": {"thrown": True, "projectile": "zombie"}}
        return client

    def run_capture(self, client):
        # Four deterministic samples, without sleeping or touching a server.
        with patch("integrations.ninjabrain.observations.time.monotonic", side_effect=[i * .125 for i in range(100)]), patch("integrations.ninjabrain.observations.time.sleep"):
            return EyeObservationCollector(client, duration_seconds=1).capture()

    def test_no_eye_inventory_does_not_consume_anything(self):
        client = self.make_client()
        client.get_inventory.return_value = {"items": []}
        with self.assertRaises(ObservationError):
            EyeObservationCollector(client).capture()
        client.use_item.assert_not_called()

    def test_uses_identified_entity_not_use_item_projectile(self):
        client = self.make_client()
        client.get_entities.side_effect = [[]] + [[{"id": 77, "name": "eye_of_ender", "pos": [i, 65, 0]}] for i in (1, 3, 6, 10)]
        observation = self.run_capture(client)
        self.assertAlmostEqual(observation["throw"]["bearing_deg"], -90)
        self.assertEqual(observation["entity_id"], 77)
        self.assertEqual(observation["diagnostics"]["use_item"]["result"]["projectile"], "zombie")
        client.use_item.assert_called_once_with("ender_eye")

    def test_multiple_eyes_fail_with_raw_evidence(self):
        client = self.make_client()
        entities = [{"id": i, "name": "eye_of_ender", "pos": [i, 65, 0]} for i in (1, 2)]
        client.get_entities.side_effect = [[], entities, entities, entities, entities]
        with self.assertRaises(ObservationError) as error:
            self.run_capture(client)
        self.assertIn("exactly one", str(error.exception))
        self.assertTrue(error.exception.diagnostics["samples"])

    def test_wrong_dimension_does_not_throw(self):
        client = self.make_client()
        client.get_status.return_value["result"]["dimension"] = "the_nether"
        with self.assertRaises(ObservationError):
            EyeObservationCollector(client).capture()
        client.use_item.assert_not_called()

    def test_moving_player_is_rejected(self):
        client = self.make_client()
        before = client.get_status.return_value
        after = {"ok": True, "result": {**before["result"], "pos": [2, 64, 0]}}
        client.get_status.side_effect = [before, after]
        client.get_entities.side_effect = [[]] + [[{"id": 77, "name": "eye_of_ender", "pos": [i, 65, 0]}] for i in (1, 3, 6, 10)]
        with self.assertRaises(ObservationError) as error:
            self.run_capture(client)
        self.assertIn("moved", str(error.exception))


class DependencyTests(unittest.TestCase):
    def test_cli_does_not_overwrite_capture_session(self):
        from integrations.ninjabrain.__main__ import main
        with patch("integrations.ninjabrain.__main__._capture") as capture, patch("sys.stderr"):
            self.assertEqual(main(["capture", "--session", "eyes.json", "--world", "test", "--output", "eyes.json"]), 1)
        capture.assert_not_called()

    def test_missing_and_corrupt_jar_are_explicit_errors(self):
        eye = EyeThrow(0, 0, -90)
        with tempfile.TemporaryDirectory() as folder:
            jar = Path(folder) / "missing.jar"
            with self.assertRaises(CalculationError):
                StrongholdCalculator(jar).calculate([eye])
            jar.write_bytes(b"not a jar")
            with self.assertRaisesRegex(CalculationError, "checksum"):
                StrongholdCalculator(jar).calculate([eye])

    def test_repeated_observation_does_not_inflate_confidence(self):
        eye = EyeThrow(0, 0, -90)
        with self.assertRaisesRegex(ValueError, "independent"):
            StrongholdCalculator().calculate([eye, eye])

    def test_skill_returns_failure_instead_of_success_for_bad_input(self):
        self.assertFalse(CalculateStrongholdSkill().execute([]).success)
        self.assertFalse(CalculateStrongholdSkill().execute([{"x": 0}]).success)


@unittest.skipUnless(JAR_PATH.exists(), "run python -m integrations.ninjabrain setup for real-engine tests")
class EngineTests(unittest.TestCase):
    def observations(self, target, origins, sigma=.05):
        return [EyeThrow(x, z, math.degrees(math.atan2(-(target[0] - x), target[1] - z)), sigma) for x, z in origins]

    def test_known_chunk_and_longer_baseline(self):
        calc = StrongholdCalculator()
        short = calc.calculate(self.observations((2048, 0), [(0, 0), (0, 200)]))
        longer = calc.calculate(self.observations((2048, 0), [(0, 0), (0, 600)]))
        for result in (short, longer):
            best = result["candidates"][0]
            self.assertEqual(best["chunk"], [128, 0])
            self.assertEqual(best["eye_target"], {"x": 2048, "z": 0})
            self.assertEqual(best["dig"], {"x": 2052, "z": 4})
            self.assertLessEqual(sum(c["probability"] for c in result["candidates"]), 1.000001)
            self.assertTrue(result["advanced"])
        self.assertGreater(longer["candidates"][0]["probability"], short["candidates"][0]["probability"])

    def test_negative_chunks_and_third_throw(self):
        result = StrongholdCalculator().calculate(self.observations((-2048, -512), [(0, 0), (500, 0), (0, 500)], .02))
        self.assertEqual(result["candidates"][0]["chunk"], [-128, -32])
        self.assertEqual(result["throws"], 3)

    def test_one_eye_reports_ambiguity(self):
        result = StrongholdCalculator().calculate([EyeThrow(0, 0, -90)])
        self.assertGreater(result["candidate_count"], 1)
        self.assertTrue(any("One throw" in warning for warning in result["warnings"]))

    def test_contradictory_rays_fail_instead_of_nan(self):
        with self.assertRaises(CalculationError):
            StrongholdCalculator().calculate([EyeThrow(0, 0, -90, .01), EyeThrow(0, 100, 90, .01)])

    def test_offline_cli_runs_without_game_credentials(self):
        process = subprocess.run([sys.executable, "-m", "integrations.ninjabrain", "calculate", "--input", "data/examples/stronghold_throws.json"], capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["candidates"][0]["chunk"], [128, 0])


if __name__ == "__main__":
    unittest.main()
