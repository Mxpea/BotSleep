"""Unknown child actions never authorize another workflow or fresh guard."""

import tempfile
import unittest
from unittest.mock import Mock, patch

from core.survival_state import SurvivalJournal
from core.client import APIError
from skills import SkillResult
from skills.action_workflow import WorkflowContext, WorkflowUncertainError
from skills.mining import MineShaftSkill
from skills.portal_speedrun import NetherPortalSpeedrunSkill
from test_mining_workflow import WorkflowMineWorld


def unknown(code="action_unconfirmed", **extra):
    return SkillResult(False, "unconfirmed child action", {
        "action_state": "unknown", "executed": True,
        "cleanup_skipped": "workflow_unknown", **extra}, code)


class MiningUnknownBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.get_status.return_value = {"ok": True, "result": {"pos": [.5, 64, .5]}}
        self.client.get_inventory_counts.return_value = {}
        self.skill = MineShaftSkill(self.client, memory=Mock())
        self.targets = {"iron": {"count": 1, "min_tool": "stone_pickaxe"}}

    def test_shaft_unknown_does_not_return_to_surface_or_hide_failure(self):
        failure = unknown()
        self.skill.build_mine_shaft = Mock(return_value=failure)
        self.skill._ensure_returned_to_surface = Mock(return_value={"on_surface": True})
        result = self.skill.mine_targets_from_surface(
            origin=[0, 64, 0], prefer_caves_first=False, targets=self.targets)
        self.assertIs(result, failure)
        # One preflight return is allowed; no movement after the shaft result.
        self.assertEqual(self.skill._ensure_returned_to_surface.call_count, 1)

    def test_cave_unknown_does_not_start_shaft_or_return_helper(self):
        failure = unknown("stalled")
        self.skill.build_mine_shaft = Mock()
        self.skill._ensure_returned_to_surface = Mock()
        with patch("skills.mining.CaveExploreSkill") as factory:
            factory.return_value.discover_and_explore_caves.return_value = failure
            result = self.skill.mine_targets_from_surface(origin=[0, 64, 0], targets=self.targets)
        self.assertIs(result, failure)
        self.skill.build_mine_shaft.assert_not_called()
        self.skill._ensure_returned_to_surface.assert_not_called()

    def test_unknown_site_approach_does_not_try_another_candidate(self):
        failure = unknown("path_not_found")
        self.skill.find_mine_sites = Mock(return_value=[
            {"origin": [10, 64, 0], "orientation": "south", "surface_entry": [10, 64, 0]},
            {"origin": [20, 64, 0], "orientation": "south", "surface_entry": [20, 64, 0]},
        ])
        with patch("skills.mining.CaveExploreSkill") as factory, \
                patch("skills.mining.ActionWorkflowRunner") as runner:
            factory.return_value.navigate_with_ops.return_value = failure
            result = self.skill.build_mine_shaft()
            factory.return_value.navigate_with_ops.assert_called_once()
            runner.assert_not_called()
        self.assertEqual(result.error_code, failure.error_code)
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertTrue(result.data["executed"])
        self.client.get_status.assert_called_once()

    def test_unknown_trail_does_not_create_fresh_navigation_guard(self):
        self.client.get_status.return_value["result"]["pos"] = [.5, 5, .5]
        self.skill._retreat_downsampled = Mock(side_effect=WorkflowUncertainError(
            "action_unconfirmed", "unknown trail move", {"executed": True}))
        with patch("skills.mining.CaveExploreSkill") as factory:
            with self.assertRaises(WorkflowUncertainError):
                self.skill._ensure_returned_to_surface([0, 64, 0], retreat_trail=[[0, 6, 0], [0, 5, 0]])
            factory.assert_not_called()
        self.client.get_status.assert_called_once()

    def test_disconnect_after_site_approach_cannot_restart_excavation(self):
        self.skill.find_mine_sites = Mock(return_value=[
            {"origin": [10, 64, 0], "orientation": "south", "surface_entry": [10, 64, 0]}])
        self.client.get_status.side_effect = [
            {"ok": True, "result": {"pos": [.5, 64, .5]}}, APIError("not_connected", "lost after move")]
        with patch("skills.mining.CaveExploreSkill") as factory, \
                patch("skills.mining.ActionWorkflowRunner") as runner:
            factory.return_value.navigate_with_ops.return_value = SkillResult(True, "moved", {"executed": True})
            result = self.skill.build_mine_shaft()
            runner.assert_not_called()
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertTrue(result.data["executed"])

    def test_unknown_navigation_does_not_attempt_pillar_escape(self):
        self.client.get_status.return_value["result"]["pos"] = [.5, 5, .5]
        with patch("skills.mining.CaveExploreSkill") as factory, \
                patch("skills.mining.escape_to_surface") as escape:
            factory.return_value.navigate_with_ops.return_value = unknown()
            with self.assertRaises(WorkflowUncertainError):
                self.skill._ensure_returned_to_surface([0, 64, 0])
            escape.assert_not_called()
        self.client.get_status.assert_called_once()

    def test_return_helper_uncertainty_stays_a_public_skill_result(self):
        self.skill._ensure_returned_to_surface = Mock(side_effect=WorkflowUncertainError(
            "action_unconfirmed", "unknown return move", {"executed": True}))
        self.skill.build_mine_shaft = Mock()
        result = self.skill.mine_targets_from_surface(
            origin=[0, 64, 0], prefer_caves_first=False, targets=self.targets)
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["action_state"], "unknown")
        self.skill.build_mine_shaft.assert_not_called()

    def test_journal_cave_phase_unknown_never_starts_shaft(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SurvivalJournal(directory, endpoint="http://offline.invalid", username="Test", world_id="fixture")
            self.skill.build_mine_shaft = Mock()
            failure = unknown()
            with patch("skills.mining.CaveExploreSkill") as factory:
                factory.return_value.discover_and_explore_caves.return_value = failure
                result = self.skill.run_with_journal(
                    journal, origin=[0, 64, 0], prefer_caves_first=True, targets=self.targets)
            self.assertIs(result, failure)
            self.skill.build_mine_shaft.assert_not_called()
            self.assertEqual(journal.state["pending"]["action_state"], "unknown")
            self.client.reset_mock()
            second = self.skill.run_with_journal(journal, origin=[0, 64, 0], targets=self.targets)
            self.assertEqual(second.error_code, "pending_intent_active")
            self.assertFalse(self.client.mock_calls)

    def test_journal_writes_intent_before_action_and_clears_only_known_result(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SurvivalJournal(directory, endpoint="http://offline.invalid", username="Test", world_id="fixture")
            def known_action(**kwargs):
                self.assertEqual(journal.state["pending"]["operation"], "mine_shaft")
                return SkillResult(False, "No valid plot", {"executed": False}, "no_mine_site_found")
            self.skill.build_mine_shaft = Mock(side_effect=known_action)
            result = self.skill.run_with_journal(journal, origin=[0, 64, 0])
            self.assertEqual(result.error_code, "no_mine_site_found")
            self.assertIsNone(journal.state["pending"])

    def test_journal_exception_remains_pending_across_reload_without_game_io(self):
        with tempfile.TemporaryDirectory() as directory:
            scope = dict(endpoint="http://offline.invalid", username="Test", world_id="fixture")
            journal = SurvivalJournal(directory, **scope)
            self.skill.build_mine_shaft = Mock(side_effect=RuntimeError("unexpected after write"))
            result = self.skill.run_with_journal(journal, origin=[0, 64, 0])
            self.assertEqual(result.data["action_state"], "unknown")
            loaded = SurvivalJournal(directory, **scope)
            pending = dict(loaded.state["pending"])
            self.skill.build_mine_shaft.reset_mock()
            self.client.reset_mock()
            second = self.skill.run_with_journal(loaded, origin=[0, 64, 0])
            self.assertEqual(second.error_code, "pending_intent_active")
            self.skill.build_mine_shaft.assert_not_called()
            self.assertFalse(self.client.mock_calls)
            self.assertEqual(loaded.state["pending"], pending)

    def test_completed_runner_unknown_skips_post_inventory_and_success_rewrite(self):
        world = WorkflowMineWorld()
        failure = unknown()
        with patch("skills.mining.ActionWorkflowRunner.run", return_value=failure), \
                patch.object(WorkflowContext, "inventory_counts") as inventory:
            result = MineShaftSkill(world, memory=Mock()).build_mine_shaft(
                origin=[0, 64, 0], orientation="south", place_workstation=False)
            inventory.assert_not_called()
        self.assertIs(result, failure)

    def test_unknown_workstation_deposit_stops_other_resupply_actions(self):
        self.client.inventory.return_value = {"ok": True, "result": {"items": [
            {"name": "dirt", "count": 10}, {"name": "raw_iron", "count": 4}]}}
        with patch("skills.container.ContainerSkill") as factory:
            factory.return_value.deposit.return_value = unknown()
            with self.assertRaises(WorkflowUncertainError):
                self.skill.rest_at_workstation({"workstation_chest_1": [0, 64, 0],
                                               "workstation_furnace": [1, 64, 0]})
            factory.return_value.smelt.assert_not_called()
        self.client.get_status.assert_not_called()


class PortalUnknownBoundaryTests(unittest.TestCase):
    def test_retryable_code_with_explicit_unknown_never_changes_portal_site(self):
        skill = NetherPortalSpeedrunSkill(Mock())
        skill._locate_standard_portal_sites = Mock(return_value=[
            {"origin": [0, 64, 0], "orientation": "south"},
            {"origin": [10, 64, 0], "orientation": "north"}])
        failure = unknown("path_not_found", failed_step="move_to_start", writes=1, steps=[])
        skill._execute_portal_site = Mock(return_value=failure)
        self.assertIs(skill.build_portal(), failure)
        skill._execute_portal_site.assert_called_once()

    def test_dig_unconfirmed_still_there_raises_retryable_verification_error(self):
        from skills.action_workflow import WorkflowGuard, WorkflowStepError
        client = Mock()
        client.dig.side_effect = APIError("dig_unconfirmed", "dig_unconfirmed: server did not confirm breaking stone; it is still there")
        guard = WorkflowGuard(client, timeout_seconds=10)
        with self.assertRaises(WorkflowStepError) as caught:
            guard.write(client.dig, [0, 64, 0])
        self.assertEqual(caught.exception.code, "gather_unverified")


if __name__ == "__main__":
    unittest.main()
