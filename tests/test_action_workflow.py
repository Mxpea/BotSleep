"""Offline unit tests for skills.action_workflow."""

import math
import threading
import unittest

from core.client import APIError
from core.structure_spec import AnchorPoint, CellRule, StructureSpec
from skills.action_workflow import (
    ActionStep,
    ActionWorkflowRunner,
    WorkflowContext,
    WorkflowGuard,
    WorkflowUncertainError,
    dig_block,
    place_block,
    stand_at,
    escape_to_surface,
)


class _DummyClient:
    def __init__(self):
        self.pos = [0.5, 64.0, 2.5]
        self.health = 20.0
        self.alive = True
        self.ready = True
        self.dimension = "overworld"
        self.blocks = {}
        self.items = {"cobblestone": 8, "stone_pickaxe": 1}
        self.sneak_history = []
        self.fail_move_targets = set()
        self.timeout_on_place = False

    def get_status(self, timeout_seconds=None):
        return {
            "ok": True,
            "result": {
                "pos": list(self.pos),
                "health": self.health,
                "alive": self.alive,
                "ready": self.ready,
                "dimension": self.dimension,
                "last_death": None,
                "in_lava": False,
                "on_fire": False,
                "held": {"name": "cobblestone"},
            },
        }

    def inventory(self, timeout_seconds=None):
        return {
            "ok": True,
            "result": {"items": [{"name": k, "count": v} for k, v in self.items.items() if v > 0]},
        }

    def get_block(self, x, y, z):
        return {"ok": True, "result": dict(self.blocks.get((int(x), int(y), int(z)), {"name": "air"}))}

    def move_to(self, x, y=None, z=None, range=1.0):
        tx = float(x)
        ty = self.pos[1] if y is None else float(y)
        tz = self.pos[2] if z is None else float(z)
        if (round(tx, 1), round(tz, 1)) in self.fail_move_targets:
            return {"ok": True, "result": {"moved": False}}
        self.pos = [tx, ty, tz]
        return {"ok": True, "result": {"moved": True}}

    def place_block(self, item=None, x=0, y=0, z=0):
        if self.timeout_on_place:
            raise APIError("request_timeout", "place timed out", details={"action_state": "unknown"})
        if self.items.get(item, 0) <= 0:
            raise APIError("missing_item", f"no {item}")
        self.items[item] -= 1
        self.blocks[(int(x), int(y), int(z))] = {"name": item}
        return {"ok": True, "result": {"placed": item}}

    def look_at(self, x, y, z):
        return {"ok": True, "result": {}}

    def equip(self, item, slot="hand"):
        return {"ok": True, "result": {"equipped": item}}

    def dig(self, x, y, z, collect=False, stop_on=None):
        self.blocks[(int(x), int(y), int(z))] = {"name": "air"}
        return {"ok": True, "result": {"dug": True}}

    def posture(self, sneak=False, shield=False, jump=False):
        self.sneak_history.append(sneak)
        return {"ok": True, "result": {"sneak": sneak}}


def _mini_spec():
    return StructureSpec(
        spec_id="mini_pillar",
        stages=("build",),
        cells=(
            CellRule((0, 0, 0), frozenset({"cobblestone"}), "build", priority=1, label="b1"),
            CellRule((0, 1, 0), frozenset({"cobblestone"}), "build", priority=2, label="b2"),
        ),
        anchors={
            "player_start": AnchorPoint("player_start", (0.5, 0.0, 2.5), tolerance=1.0),
            "player_end": AnchorPoint("player_end", (2.5, 0.0, 2.5), tolerance=0.8),
        },
    )


class ActionWorkflowTests(unittest.TestCase):
    def test_full_lifecycle_idempotent_skip_and_end_move(self):
        client = _DummyClient()
        spec = _mini_spec()
        origin = [10, 64, 10]
        # Pre-populate first block so it should be skipped idempotently.
        client.blocks[(10, 64, 10)] = {"name": "cobblestone"}

        ctx = WorkflowContext(
            guard=WorkflowGuard(client, timeout_seconds=30),
            spec=spec,
            origin=origin,
            orientation="south",
            player_start=list(spec.anchors["player_start"].rel_pos),
            player_end=[12.5, 64.0, 12.5],
        )
        # Use relative world start from spec
        ctx.player_start = ctx.anchor_world("player_start")

        steps = [
            ActionStep("place_b1", "b1", lambda c: place_block(c, "cobblestone", (0, 0, 0)), stage="build"),
            ActionStep("place_b2", "b2", lambda c: place_block(c, "cobblestone", (0, 1, 0)), stage="build"),
        ]
        res = ActionWorkflowRunner().run(ctx, steps)
        self.assertTrue(res.success, res.message)
        self.assertTrue(res.data["structure_verified"])
        self.assertEqual(client.items["cobblestone"], 7)  # Only b2 placed
        self.assertEqual(res.data["step_logs"][1]["detail"]["skipped"], True)
        self.assertEqual(client.pos, [12.5, 64.0, 12.5])

    def test_local_branch_recovers_from_soft_obstacle(self):
        client = _DummyClient()
        spec = _mini_spec()
        origin = [10, 64, 10]
        client.blocks[(10, 64, 10)] = {"name": "fern"}
        client.blocks[(10, 65, 10)] = {"name": "cobblestone"}

        ctx = WorkflowContext(
            guard=WorkflowGuard(client, timeout_seconds=30),
            spec=spec,
            origin=origin,
            orientation="south",
        )

        def clear_fern(c, err):
            if err.blocker_block == "fern":
                dig_block(c, err.blocker_pos, relative=False, allowed_blocks={"fern"})
                return True
            return False

        steps = [
            ActionStep(
                "place_b1",
                "b1",
                lambda c: place_block(c, "cobblestone", (0, 0, 0)),
                stage="build",
                branches={"target_obstructed": clear_fern},
            )
        ]
        res = ActionWorkflowRunner().run(ctx, steps)
        self.assertTrue(res.success, res.message)
        self.assertEqual(client.blocks[(10, 64, 10)]["name"], "cobblestone")
        self.assertEqual(res.data["step_logs"][0]["status"], "branch_recovered")

    def test_uncertain_write_stops_immediately_without_retry(self):
        client = _DummyClient()
        client.timeout_on_place = True
        spec = _mini_spec()
        ctx = WorkflowContext(
            guard=WorkflowGuard(client, timeout_seconds=30),
            spec=spec,
            origin=[10, 64, 10],
            orientation="south",
        )
        branch_called = {"count": 0}

        def branch(c, err):
            branch_called["count"] += 1
            return True

        steps = [
            ActionStep(
                "place_b1",
                "b1",
                lambda c: place_block(c, "cobblestone", (0, 0, 0)),
                branches={"*": branch},
            )
        ]
        res = ActionWorkflowRunner().run(ctx, steps)
        self.assertFalse(res.success)
        self.assertEqual(res.error_code, "request_timeout")
        self.assertEqual(res.data["action_state"], "unknown")
        self.assertEqual(res.data["blocker_type"], "uncertain_write")
        self.assertEqual(branch_called["count"], 0)

    def test_cancel_restores_sneak_posture_in_finally(self):
        client = _DummyClient()
        spec = _mini_spec()
        cancel = threading.Event()
        ctx = WorkflowContext(
            guard=WorkflowGuard(client, cancel_event=cancel, timeout_seconds=30),
            spec=spec,
            origin=[10, 64, 10],
            orientation="south",
        )

        def sneak_and_cancel(c):
            c.guard.set_sneak(True)
            cancel.set()
            c.guard.check_budget()

        res = ActionWorkflowRunner().run(ctx, [ActionStep("sneak_step", "sneak", sneak_and_cancel)])
        self.assertFalse(res.success)
        self.assertEqual(res.error_code, "cancelled")
        self.assertEqual(client.sneak_history, [True, False])

    def test_ambiguous_writes_never_retry_branch_or_restore_posture(self):
        cases = [("not_connected", {}, "action_unconfirmed"),
                 ("request_timeout", {}, "request_timeout"),
                 ("backend_error", {"action_state": "unknown"}, "backend_error")]
        for code, details, expected in cases:
            with self.subTest(code=code):
                client = _DummyClient()
                ctx = WorkflowContext(guard=WorkflowGuard(client), spec=_mini_spec(),
                                      origin=[10, 64, 10], orientation="south")
                attempts = []
                branches = []

                def ambiguous_write():
                    attempts.append("write")
                    raise APIError(code, "ambiguous result", details=details)

                def action(c):
                    c.guard.set_sneak(True)
                    c.guard.write(ambiguous_write)

                result = ActionWorkflowRunner().run(ctx, [ActionStep(
                    "ambiguous", "write", action,
                    branches={"*": lambda c, err: branches.append(err) or True})])
                self.assertFalse(result.success)
                self.assertEqual(result.error_code, expected)
                self.assertEqual(result.data["action_state"], "unknown")
                self.assertTrue(result.data["cleanup_skipped"].endswith("unknown"))
                self.assertEqual(client.sneak_history, [True])
                self.assertEqual(branches, [])
                with self.assertRaises(WorkflowUncertainError):
                    ctx.guard.write(ambiguous_write)
                self.assertEqual(attempts, ["write"])

    def test_disconnect_after_write_preserves_unknown_without_cleanup(self):
        client = _DummyClient()
        ctx = WorkflowContext(guard=WorkflowGuard(client), spec=_mini_spec(),
                              origin=[10, 64, 10], orientation="south")

        def action(c):
            c.guard.set_sneak(True)
            def disconnected():
                raise APIError("not_connected", "connection lost")
            c.guard.read(disconnected)

        result = ActionWorkflowRunner().run(ctx, [ActionStep("read_after_write", "verify", action)])
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["api_error_code"], "not_connected")
        self.assertEqual(result.data["cleanup_skipped"], "disconnected_unknown")
        self.assertEqual(client.sneak_history, [True])

    def test_disconnect_before_any_write_remains_not_executed(self):
        client = _DummyClient()
        def disconnected(**kwargs):
            raise APIError("not_connected", "not logged in")
        client.get_status = disconnected
        ctx = WorkflowContext(guard=WorkflowGuard(client), spec=_mini_spec(),
                              origin=[10, 64, 10], orientation="south")
        result = ActionWorkflowRunner().run(ctx, [])
        self.assertEqual(result.error_code, "not_connected")
        self.assertFalse(result.data["executed"])
        self.assertNotEqual(result.data.get("action_state"), "unknown")
        self.assertEqual(client.sneak_history, [])

    def test_end_move_failure_preserves_structure_verified_flag(self):
        client = _DummyClient()
        spec = _mini_spec()
        origin = [10, 64, 10]
        client.blocks[(10, 64, 10)] = {"name": "cobblestone"}
        client.blocks[(10, 65, 10)] = {"name": "cobblestone"}
        client.fail_move_targets.add((12.5, 12.5))

        ctx = WorkflowContext(
            guard=WorkflowGuard(client, timeout_seconds=30),
            spec=spec,
            origin=origin,
            orientation="south",
            player_end=[12.5, 64.0, 12.5],
        )
        res = ActionWorkflowRunner().run(ctx, [])
        self.assertFalse(res.success)
        self.assertEqual(res.error_code, "end_move_failed")
        self.assertTrue(res.data["structure_verified"])
        self.assertIsNotNone(res.data["end_move_error"])

    def test_escape_to_surface_handles_wall_torch_coal_ore_and_falling_gravel(self):
        client = _DummyClient()
        # Bot at z=-847.09 falls inside (-135, 27, -848) column with wall_torch at y=30,
        # adjacent chimney (-135, 27, -847) open through y=30, then coal_ore + falling gravel above.
        client.pos = [-134.5, 27.0, -847.09]
        client.items = {"cobblestone": 64, "iron_pickaxe": 1}
        for x in range(-137, -133):
            for z in range(-850, -845):
                client.blocks[(x, 26, z)] = {"name": "stone"}
                for y in range(31, 34):
                    client.blocks[(x, y, z)] = {"name": "stone"}
        client.blocks[(-135, 30, -848)] = {"name": "wall_torch"}
        client.blocks[(-135, 31, -847)] = {"name": "coal_ore"}
        client.blocks[(-135, 32, -847)] = {"name": "stone"}
        client.blocks[(-135, 33, -847)] = {"name": "gravel"}
        # Solid ground stops at y=33; y>=34 open sky!
        orig_place = client.place_block
        orig_dig = client.dig
        gravel_dropped = {"done": False}

        def custom_dig(x, y, z, collect=False, stop_on=None):
            res = orig_dig(x, y, z, collect=collect, stop_on=stop_on)
            # Simulate falling gravel into foot cell once (-135, 31, -847) -> step at y=29 digs y=31 stone/gravel
            if (int(x), int(y), int(z)) == (-135, 32, -847) and not gravel_dropped["done"]:
                gravel_dropped["done"] = True
                client.blocks[(-135, 30, -847)] = {"name": "gravel"}
                client.blocks[(-135, 33, -847)] = {"name": "air"}
            return res

        def custom_place(item=None, x=0, y=0, z=0):
            cur = client.blocks.get((int(x), int(y), int(z)), {"name": "air"})["name"]
            if cur not in {"air", "cave_air"}:
                raise APIError("occupied", f"{cur} already at target")
            r = orig_place(item=item, x=x, y=y, z=z)
            px, py, pz = math.floor(client.pos[0] + 0.0001), math.floor(client.pos[1] + 0.0001), math.floor(client.pos[2] + 0.0001)
            if (px, py, pz) == (int(x), int(y), int(z)):
                client.pos[1] = float(int(y) + 1)
            return r

        client.dig = custom_dig
        client.place_block = custom_place
        ctx = WorkflowContext(
            guard=WorkflowGuard(client, timeout_seconds=30, max_reads=2000),
            spec=_mini_spec(),
            origin=[-135, 27, -848],
            orientation="south",
        )
        esc = escape_to_surface(ctx, max_ascend=20)
        self.assertTrue(esc["escaped"], esc)
        self.assertGreaterEqual(esc["pos"][1], 32)


if __name__ == "__main__":
    unittest.main()
