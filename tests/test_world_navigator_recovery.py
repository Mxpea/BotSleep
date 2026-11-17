"""Recovery must preserve safety stops and never claim an unverified return."""

import unittest
from unittest.mock import Mock, patch

from core.world_3d import VoxelWorld3D
from skills.action_workflow import WorkflowStepError, WorkflowUncertainError
from skills.world_navigator import ReversibleTrail, WorldNavigator


class NavigatorRecoveryTests(unittest.TestCase):
    def test_micro_dig_safety_stop_propagates_without_second_candidate(self):
        for failure in (
            WorkflowStepError("cancelled", "cancelled"),
            WorkflowStepError("dead", "dead"),
            WorkflowStepError("life_changed", "respawn"),
            WorkflowStepError("damage_taken", "hurt"),
            WorkflowUncertainError("action_unconfirmed", "dig outcome unknown"),
        ):
            with self.subTest(error=failure.code):
                ctx = Mock()
                world = VoxelWorld3D()
                read = lambda pos: {"name": "stone"}
                with patch("skills.world_navigator.stand_cell", side_effect=WorkflowStepError("stalled", "blocked")) as stand, \
                        patch("skills.world_navigator._can_dig_step_cell", return_value=True), \
                        patch("skills.world_navigator.aim_at"), \
                        patch("skills.world_navigator.dig_block", side_effect=failure) as dig:
                    with self.assertRaises(type(failure)) as caught:
                        WorldNavigator.execute_step(ctx, world, [0, 64, 0], {"to": [1, 65, 0], "mode": "walk"},
                                                    read, allow_micro_dig=True)
                self.assertIs(caught.exception, failure)
                self.assertEqual(stand.call_count, 1)
                self.assertEqual(dig.call_count, 1)
                ctx.guard.check_status.assert_not_called()

    def test_near_entry_failure_is_not_return_success(self):
        ctx = Mock()
        ctx.guard.check_status.return_value = {"pos": [1.5, 64, .5]}
        trail = ReversibleTrail([0, 64, 0])
        trail.push_step([1, 64, 0])
        with patch("skills.world_navigator.stand_cell", side_effect=WorkflowStepError("stalled", "blocked")):
            result = WorldNavigator.retreat_to_entry(ctx, VoxelWorld3D(), trail, [0, 64, 0], Mock())
        self.assertEqual(result, (False, False))

    def test_near_entry_safety_stop_does_not_become_success(self):
        for failure in (WorkflowStepError("dead", "dead"), WorkflowStepError("cancelled", "stop"),
                        WorkflowUncertainError("action_unconfirmed", "move unknown")):
            with self.subTest(code=failure.code):
                ctx = Mock()
                ctx.guard.check_status.return_value = {"pos": [1.5, 64, .5]}
                trail = ReversibleTrail([0, 64, 0])
                trail.push_step([1, 64, 0])
                with patch("skills.world_navigator.stand_cell", side_effect=[WorkflowStepError("stalled", "blocked"), failure]) as stand, \
                        patch("skills.world_navigator.escape_to_surface") as escape:
                    with self.assertRaises(type(failure)) as caught:
                        WorldNavigator.retreat_to_entry(ctx, VoxelWorld3D(), trail, [0, 64, 0], Mock())
                self.assertIs(caught.exception, failure)
                self.assertEqual(stand.call_count, 2)
                escape.assert_not_called()

    def test_single_breadcrumb_still_verifies_current_arrival(self):
        failure = WorkflowUncertainError("invalid_response", "no current position")
        with patch("skills.world_navigator.stand_cell", side_effect=failure) as stand:
            with self.assertRaises(WorkflowUncertainError):
                WorldNavigator.retreat_to_entry(Mock(), VoxelWorld3D(), ReversibleTrail([0, 64, 0]),
                                               [0, 64, 0], Mock())
        stand.assert_called_once()

    def test_live_position_safety_failures_are_not_silently_fallbacks(self):
        for code in ("cancelled", "dead", "life_changed", "damage_taken"):
            with self.subTest(code=code):
                ctx = Mock()
                ctx.guard.check_status.side_effect = WorkflowStepError(code, code)
                with self.assertRaises(WorkflowStepError) as caught:
                    WorldNavigator.sync_live_pos(ctx)
                self.assertEqual(caught.exception.code, code)


if __name__ == "__main__":
    unittest.main()
