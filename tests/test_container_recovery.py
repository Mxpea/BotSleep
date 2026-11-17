"""Container workflows retain uncertain outcomes and require actual output."""

import unittest
from unittest.mock import Mock, patch

from core.client import APIError
from skills.container import ContainerSkill
from test_container_skill import SimContainerWorld


class ContainerRecoveryTests(unittest.TestCase):
    def assert_unknown(self, result):
        self.assertFalse(result.success)
        self.assertFalse(result.data["completed"])
        self.assertTrue(result.data["executed"])
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual(result.data["cleanup_skipped"], "workflow_unknown")

    def test_all_public_entrypoints_return_unknown_without_followup_io(self):
        for action, pos, kwargs in (
            ("inspect", [2, 64, 0], {}),
            ("deposit", [2, 64, 0], {"items": {"dirt": 1}}),
            ("withdraw", [2, 64, 0], {"items": {"dirt": 1}}),
            ("smelt", [2, 65, 0], {"input_item": "raw_iron", "count": 1}),
        ):
            with self.subTest(action=action):
                world = SimContainerWorld()
                with patch.object(world, "window_view", side_effect=APIError("action_unconfirmed", "window disconnected")) as view, \
                        patch.object(world, "window_act") as act, patch.object(world, "inventory") as inv, \
                        patch.object(world, "stop") as stop:
                    result = ContainerSkill(world).execute(action, pos, **kwargs)
                self.assert_unknown(result)
                self.assertEqual(result.error_code, "action_unconfirmed")
                view.assert_called_once()
                act.assert_not_called()
                inv.assert_not_called()
                stop.assert_not_called()

    def test_unknown_transfer_never_reads_another_window_or_replays(self):
        for action, pos, kwargs in (
            ("deposit", [2, 64, 0], {"items": {"dirt": 1}}),
            ("withdraw", [2, 64, 0], {"items": {"dirt": 1}}),
            ("smelt", [2, 65, 0], {"input_item": "raw_iron", "count": 1}),
        ):
            with self.subTest(action=action):
                world = SimContainerWorld()
                world.windows[(2, 64, 0)]["container"]["dirt"] = 1
                at_write = {}
                with patch.object(world, "window_view", wraps=world.window_view) as view, \
                        patch.object(world, "inventory", wraps=world.inventory) as inv, \
                        patch.object(world, "window_act") as act, patch.object(world, "stop") as stop:
                    def uncertain(*args, **kwargs):
                        at_write.update(views=view.call_count, inventory=inv.call_count)
                        raise APIError("action_unconfirmed", "partial transfer", details={"done": [{"op": "put"}]})
                    act.side_effect = uncertain
                    result = ContainerSkill(world).execute(action, pos, **kwargs)
                self.assert_unknown(result)
                act.assert_called_once()
                self.assertEqual(view.call_count, at_write["views"])
                self.assertEqual(inv.call_count, at_write["inventory"])
                self.assertEqual(result.data["api_details"]["done"], [{"op": "put"}])
                stop.assert_not_called()

    def test_missing_container_sections_are_unknown_not_empty(self):
        for payload in ({}, {"sections": {}}, {"sections": {"container": None}},
                        {"sections": {"container": [{"name": "dirt", "count": True}]}}):
            with self.subTest(payload=payload):
                world = SimContainerWorld()
                with patch.object(world, "window_view", return_value={"ok": True, "result": payload}), \
                        patch.object(world, "window_act") as act:
                    result = ContainerSkill(world).inspect([2, 64, 0])
                self.assert_unknown(result)
                self.assertEqual(result.error_code, "invalid_response")
                act.assert_not_called()

    def test_missing_inventory_is_unknown_not_empty(self):
        world = SimContainerWorld()
        with patch.object(world, "inventory", return_value={"ok": True, "result": {}}), \
                patch.object(world, "window_act") as act:
            result = ContainerSkill(world).deposit([2, 64, 0], {"dirt": 1})
        self.assert_unknown(result)
        self.assertEqual(result.error_code, "invalid_response")
        act.assert_not_called()

    def test_stalled_furnace_does_not_report_completed_without_output(self):
        world = SimContainerWorld()

        def stalled_view(pos):
            window = world.windows[tuple(pos)]
            return {"ok": True, "result": {"sections": {
                "input": [{"name": k, "count": v} for k, v in window["input"].items()],
                "fuel": [], "output": [],
            }, "info": {"burning": False}}}

        with patch.object(world, "window_view", side_effect=stalled_view):
            result = ContainerSkill(world).smelt([2, 65, 0], "raw_iron", 4)
        self.assertFalse(result.success)
        self.assertFalse(result.data["completed"])
        self.assertEqual(result.error_code, "smelt_unverified")
        self.assertEqual(result.data["gained"], 0)
        self.assertEqual(result.data["stage"], "partial")


if __name__ == "__main__":
    unittest.main()
