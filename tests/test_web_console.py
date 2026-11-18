import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import requests
import yaml

import main as bot_main
from core.client import APIError
from core.model_gate import ModelRequestGate
from core.survival_harness import SurvivalHarness
from core.survival_state import SurvivalJournal
from core.world_3d import VoxelWorld3D
from debug.scene_3d import Scene3DCollector, block_color, classify_block, find_world_3d_db
from debug.web_backend import WebConsoleRuntime
from debug.web_console import (
    COMPACT_FLAT_CSS,
    _ensure_loopback_no_proxy,
    _format_events_rows,
    _format_live_cockpit_cards,
    _format_runtime_code,
    _format_status_banner,
    build_demo,
)


class FakeGameClient:
    def __init__(self, base_url="http://127.0.0.1:18900/api/v1", username="WebBot", password="", timeout_seconds=15):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout_seconds = timeout_seconds
        self.token = None
        self.logged_in = False
        self.logged_out = False
        self.stop_calls = 0
        self.chats = []
        self.controls = []
        self.moves = []
        self.looks = []
        self.pos = [10.5, 64.0, -4.5]
        self.dimension = "overworld"
        self.blocks_world = {
            (10, 63, -5): "stone",
            (11, 64, -4): "iron_ore",
            (9, 64, -4): "lava",
            (12, 64, -4): "crafting_table",
        }
        self.events = [
            {"seq": 1, "type": "chat", "text": "<Tester> hello bot", "timestamp": "12:00:01"},
        ]

    def login(self, max_wait_seconds=15):
        self.token = "fake-token-secret"
        self.logged_in = True
        return self.get_status()["result"]

    def attach_or_login(self, session_file=None, *, max_wait_seconds=15):
        return self.login(max_wait_seconds=max_wait_seconds)

    def logout(self):
        self.logged_out = True
        self.token = None
        return {"ok": True}

    def save_session(self, session_file):
        return Path(session_file)

    def get_status(self, timeout_seconds=None):
        return {
            "ok": True,
            "result": {
                "ready": True,
                "alive": True,
                "busy": False,
                "username": self.username,
                "dimension": self.dimension,
                "pos": list(self.pos),
                "yaw": 1.2,
                "pitch": 0.1,
                "health": 20,
                "food": 18,
                "is_day": True,
                "time_of_day": 3000,
                "on_ground": True,
                "is_sleeping": False,
                "in_vehicle": False,
                "in_water": False,
                "in_lava": False,
                "on_fire": False,
                "submerged": False,
                "gamemode": "survival",
            },
        }

    def inventory(self, timeout_seconds=None):
        return {
            "ok": True,
            "result": {
                "items": [{"name": "stone_pickaxe", "count": 1, "durability": 120}, {"name": "bread", "count": 4}]
            },
        }

    def get_blocks(self, radius=16, types=None, limit=64):
        rows = [
            {"name": name, "pos": list(coord), "dist": 1.5}
            for coord, name in self.blocks_world.items()
            if (types is None or name in types)
        ]
        return rows[:limit]

    def get_entities(self, radius=16, types=None, timeout_seconds=None):
        return [{"id": 42, "name": "cow", "type": "passive", "pos": [12.5, 64.0, -3.5], "dist": 2.2}]

    def get_block(self, pos_or_x, y=None, z=None, *, timeout_seconds=None):
        coord = tuple(int(v) for v in (pos_or_x if isinstance(pos_or_x, (list, tuple)) else (pos_or_x, y, z)))
        if coord == (10, 62, -5):
            raise APIError("not_loaded", "chunk not loaded")
        name = self.blocks_world.get(coord, "air")
        return {"ok": True, "result": {"name": name, "pos": list(coord), "properties": {}}}

    def get_events(self, since=0, limit=50):
        return [ev for ev in self.events if ev["seq"] > since][:limit]

    def chat(self, message):
        self.chats.append(message)
        return {"ok": True}

    def stop(self, timeout_seconds=None):
        self.stop_calls += 1
        return {"ok": True}

    def control(self, **kwargs):
        self.controls.append(kwargs)
        if kwargs.get("forward"):
            self.pos[2] += 0.5
        return {"ok": True, "result": {"pos": list(self.pos)}}

    def move_to(self, x, y=None, z=None, timeout_ms=15000, range=1, stop_on=None):
        self.moves.append((x, y, z, range))
        self.pos = [float(x), float(y if y is not None else self.pos[1]), float(z)]
        return {"ok": True, "result": {"pos": list(self.pos)}}

    def look_at(self, x, y, z, timeout_seconds=None):
        self.looks.append((x, y, z))
        return {"ok": True, "result": {}}


def _write_test_config(root: Path, overrides=None) -> Path:
    cfg = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    cfg["agent"]["name"] = "WebTestBot"
    cfg["agent"]["password"] = "pw-secret-123"
    cfg["agent"]["mode"] = "survival"
    cfg["survival"]["objective"] = "daily"
    cfg["survival"]["state_dir"] = str(root / "survival_state")
    cfg["survival"]["world_id"] = "testworld"
    cfg["debug"]["flight_recorder"]["output_dir"] = str(root / "logs")
    cfg["debug"]["flight_recorder"]["enabled"] = True
    cfg["llm"]["enabled"] = False
    cfg["llm"]["api_key"] = "sk-secret-key-987654321"
    cfg["rcon"] = {"enabled": False, "host": "127.0.0.1", "port": 25575, "password": "rcon-secret-password"}
    if overrides:
        for sec, vals in overrides.items():
            if isinstance(vals, dict) and isinstance(cfg.get(sec), dict):
                cfg[sec].update(vals)
            else:
                cfg[sec] = vals
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


class Scene3DTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="botsleep_scene3d_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_classify_and_colors(self):
        self.assertEqual(classify_block("air"), "air")
        self.assertEqual(classify_block("cave_air"), "air")
        self.assertEqual(classify_block("deepslate_diamond_ore"), "ore")
        self.assertEqual(classify_block("flowing_lava"), "hazard")
        self.assertEqual(classify_block("red_bed"), "station")
        self.assertEqual(classify_block("oak_leaves"), "foliage")
        self.assertEqual(classify_block("stone"), "terrain")
        self.assertTrue(block_color("diamond_ore", "ore").startswith("#"))

    def test_world_3d_readonly_roundtrip_without_creating_dirs(self):
        world_base = self.tmp / "world_3d"
        missing = find_world_3d_db(
            endpoint="http://127.0.0.1:18900/api/v1",
            username="WebTestBot",
            world_id="testworld",
            dimension="overworld",
            base_dir=world_base,
        )
        self.assertIsNone(missing)
        self.assertFalse(world_base.exists())

        w = VoxelWorld3D.for_runtime(
            endpoint="http://127.0.0.1:18900/api/v1",
            username="WebTestBot",
            world_id="testworld",
            dimension="overworld",
            base_dir=world_base,
        )
        w.set_block((10, 63, -4), {"name": "stone"})
        w.set_block((11, 64, -4), {"name": "coal_ore"})
        w.record_stance((10, 64, -4))
        w.record_blocked_edge((10, 64, -4), (11, 64, -4))
        w.mark_ore_status((11, 64, -4), "pending", name="coal_ore")
        w.save()

        collector = Scene3DCollector(world_3d_base_dir=world_base)
        scene = collector.build_scene(
            observation={
                "status": {"pos": [10.5, 64.0, -4.5], "dimension": "minecraft:overworld", "health": 20, "food": 20},
                "blocks": [{"name": "iron_ore", "pos": [11, 64, -4]}],  # overrides coal_ore from sqlite
                "entities": [{"id": 7, "name": "zombie", "type": "hostile", "pos": [14.0, 64.0, -4.0], "dist": 3.5}],
            },
            scope={"endpoint": "http://127.0.0.1:18900/api/v1", "username": "WebTestBot", "world_id": "testworld"},
            radius=24,
        )
        self.assertEqual(scene["world_source"], "sqlite")
        by_coord = {(v["x"], v["y"], v["z"]): v for v in scene["voxels"]}
        self.assertEqual(by_coord[(11, 64, -4)]["name"], "iron_ore")
        self.assertEqual(by_coord[(11, 64, -4)]["src"], "obs")
        self.assertEqual(by_coord[(10, 63, -4)]["name"], "stone")
        self.assertEqual(by_coord[(10, 63, -4)]["src"], "world3d")
        self.assertEqual(len(scene["entities"]), 1)
        self.assertEqual(len(scene["visited_stances"]), 1)
        self.assertEqual(len(scene["blocked_edges"]), 1)

    def test_scan_local_box_and_voxel_priority_cap(self):
        client = FakeGameClient()
        collector = Scene3DCollector(world_3d_base_dir=self.tmp / "w3d")
        meta = collector.scan_local_box(client, radius_xz=2, radius_y=2, max_queries=200)
        self.assertEqual(meta["queried"], 125)
        self.assertEqual(meta["not_loaded"], 1)
        self.assertGreaterEqual(meta["solid_count"], 4)

        # Add many distant terrain blocks; cap to 4 and ensure ores/hazards survive
        obs_blocks = [{"name": "stone", "pos": [10 + i, 64, 10]} for i in range(1, 20)]
        scene = collector.build_scene(
            observation={"status": {"pos": [10.5, 64.0, -4.5], "dimension": "overworld"}, "blocks": obs_blocks},
            max_voxels=4,
            radius=32,
        )
        cats = {v["cat"] for v in scene["voxels"]}
        self.assertIn("ore", cats)
        self.assertIn("hazard", cats)


class WebBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="botsleep_web_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_host_mode_lifecycle_chat_and_emergency_stop(self):
        cfg_path = _write_test_config(self.tmp)
        client = FakeGameClient()
        rt = WebConsoleRuntime(
            cfg_path,
            client_factory=lambda _cfg: client,
            world_3d_base_dir=self.tmp / "w3d",
        )
        try:
            rep = rt.start(auto_connect=True, start_paused=True)
            self.assertEqual(rep["host_mode"], "host")
            self.assertTrue(client.logged_in)
            self.assertTrue(rt.controller.paused)

            # Poll chat manually once and send chat
            rt._poll_chat_once(client)
            ok, _ = rt.send_chat("hello from web")
            self.assertTrue(ok)
            self.assertEqual(client.chats[-1], "hello from web")
            chat_texts = [row["text"] for row in rt.status_report()["chat"]]
            self.assertTrue(any("Tester" in t for t in chat_texts))
            self.assertTrue(any("hello from web" in t for t in chat_texts))

            # Emergency stop always reaches client.stop()
            ok, _ = rt.emergency_stop()
            self.assertTrue(ok)
            self.assertEqual(client.stop_calls, 1)

            # Radar & 3D scan work
            ok, data, _ = rt.manual_radar_scan(kind="poi", radius=16)
            self.assertTrue(ok)
            self.assertIn("crafting", data)
            ok, msg = rt.manual_3d_scan(radius_xz=2, radius_y=1)
            self.assertTrue(ok)
            self.assertIn("局部 3D 采样完成", msg)
        finally:
            rt.close()
        self.assertTrue(client.logged_out)

    def test_launch_join_disconnect_and_rejoin_lifecycle(self):
        cfg_path = _write_test_config(self.tmp)
        clients = []
        fail_next = [False]

        def make_client(cfg):
            c = FakeGameClient(username=cfg["agent"]["name"])
            if fail_next[0]:
                def fail_login(max_wait_seconds=15):
                    raise APIError("network_error", "auth/login request failed")
                c.login = fail_login
                c.attach_or_login = lambda session_file=None, *, max_wait_seconds=15: fail_login(max_wait_seconds)
            clients.append(c)
            return c

        rt = WebConsoleRuntime(
            cfg_path,
            client_factory=make_client,
            world_3d_base_dir=self.tmp / "w3d",
        )
        try:
            rep = rt.start(auto_connect=False)
            self.assertEqual(rep["host_mode"], "disconnected")
            self.assertEqual(len(clients), 0)

            ok, msg = rt.connect_or_restart(
                start_paused=True,
                mode="survival",
                objective="daily",
                username="JoinBot",
            )
            self.assertTrue(ok, msg)
            self.assertEqual(rt.mode, "host")
            self.assertEqual(clients[-1].username, "JoinBot")
            self.assertTrue(clients[-1].logged_in)
            self.assertTrue(rt.controller.paused)

            ok, msg = rt.disconnect_from_game()
            self.assertTrue(ok, msg)
            self.assertEqual(rt.mode, "disconnected")
            self.assertTrue(clients[-1].logged_out)
            # Instance lock must be released on disconnect
            self.assertFalse(rt.journal_guard.instance_status()["locked"])

            # Can join again without restarting process
            ok, msg = rt.connect_or_restart(start_paused=False, mode="sleep", username="SleepJoiner")
            self.assertTrue(ok, msg)
            self.assertEqual(rt.mode, "host")
            self.assertEqual(clients[-1].username, "SleepJoiner")

            # Unreachable mc-agent-api yields actionable error message
            fail_next[0] = True
            ok, msg = rt.connect_or_restart(start_paused=True, mode="sleep", username="OfflineBot")
            self.assertFalse(ok)
            self.assertIn("mc-agent-api", msg)
            self.assertIn("25565", msg)
        finally:
            rt.close()

    def test_readonly_attach_when_instance_locked(self):
        cfg_path = _write_test_config(self.tmp)
        external_journal = SurvivalJournal(
            str(self.tmp / "survival_state"),
            endpoint="http://127.0.0.1:18900/api/v1",
            username="WebTestBot",
            world_id="testworld",
        )
        client = FakeGameClient()
        with external_journal.instance_lock():
            external_journal.update(pending={"decision": {"name": "dig"}})
            external_journal.record("EXTERNAL_TEST", {"ok": True})
            rt = WebConsoleRuntime(
                cfg_path,
                client_factory=lambda _cfg: client,
                world_3d_base_dir=self.tmp / "w3d",
            )
            try:
                rep = rt.start(auto_connect=True)
                self.assertEqual(rep["host_mode"], "readonly_attach")
                self.assertFalse(client.logged_in)
                self.assertEqual(rep["pending"], {"decision": {"name": "dig"}})

                ok, _ = rt.manual_move_step("forward", force_override=True)
                self.assertFalse(ok)
                ok, _ = rt.emergency_stop()
                self.assertFalse(ok)
                rt._poll_readonly_attach()
                events = rt.get_events()
                self.assertTrue(any(ev["event"] == "EXTERNAL_TEST" for ev in events))
            finally:
                rt.close()

    def test_manual_write_guards_for_paused_pending_and_model_uncertain(self):
        cfg_path = _write_test_config(self.tmp)
        client = FakeGameClient()
        rt = WebConsoleRuntime(
            cfg_path,
            client_factory=lambda _cfg: client,
            world_3d_base_dir=self.tmp / "w3d",
        )
        try:
            rt.start(auto_connect=True, start_paused=False)
            # 1. Unpaused AI blocks ordinary manual move
            ok, msg = rt.manual_move_step("forward")
            self.assertFalse(ok)
            self.assertIn("暂停 AI", msg)
            # Chat & emergency stop remain allowed
            ok, _ = rt.send_chat("status ping")
            self.assertTrue(ok)

            # 2. Pause AI, wait for any in-flight tick to settle, then inject pending -> blocks unless force_override=True
            rt.pause_ai()
            deadline = time.monotonic() + 2.0
            while rt.controller.busy and time.monotonic() < deadline:
                time.sleep(0.02)
            rt.controller.journal.update(pending={"decision": {"name": "place_item"}})
            ok, msg = rt.manual_move_to(12, 64, -4)
            self.assertFalse(ok)
            self.assertIn("pending", msg)

            ok, _ = rt.manual_move_to(12, 64, -4, force_override=True)
            self.assertTrue(ok)
            self.assertEqual(client.moves[-1], (12.0, 64.0, -4.0, 1.0))

            # Clear pending; simulate uncertain model gate
            rt.controller.journal.update(pending=None)
            gate_dir = self.tmp / "gate"
            gate = ModelRequestGate("http://127.0.0.1:8000/v1", "sk-secret-key-987654321", directory=gate_dir)
            gate._write({"state": "uncertain", "error_code": "llm_timeout"})
            with mock.patch("debug.web_backend.ModelRequestGate", return_value=gate):
                # Temporarily enable llm flag in config for snapshot check
                rt.config["llm"]["enabled"] = True
                ok, msg = rt.manual_run_skill("wait", '{"seconds": 1}')
                self.assertFalse(ok)
                self.assertIn("uncertain", msg)
                ok, _ = rt.manual_run_skill("wait", '{"seconds": 0.05}', force_override=True)
                self.assertTrue(ok)
                ok, msg = rt.manual_run_skill("run_survival_task", '{"task": "scout"}', force_override=True)
                self.assertFalse(ok)
                self.assertIn("内部状态机", msg)
                ok, msg = rt.manual_run_skill("move_to", '{"pos": [12, 64, -4], "range": 1}', force_override=True)
                self.assertTrue(ok, msg)
                ok, _ = rt.manual_look_at(13, 64, -4, force_override=True)
                self.assertTrue(ok)
                self.assertEqual(client.looks[-1], (13.0, 64.0, -4.0))
        finally:
            rt.close()

    def test_sample_runtime_code_captures_active_worker_thread_frames(self):
        cfg_path = _write_test_config(self.tmp)
        client = FakeGameClient()
        rt = WebConsoleRuntime(
            cfg_path,
            client_factory=lambda _cfg: client,
            world_3d_base_dir=self.tmp / "w3d",
        )
        entered = threading.Event()
        release = threading.Event()

        def blocking_helper():
            entered.set()
            release.wait(timeout=3.0)

        try:
            rt.start(auto_connect=True, start_paused=True)
            fut = rt.controller._executor.submit(blocking_helper)
            self.assertTrue(entered.wait(timeout=2.0))
            code_info = rt.sample_runtime_code()
            thread_labels = [t["thread"] for t in code_info["threads"]]
            self.assertIn("ActionWorker#1", thread_labels)
            worker_snap = next(t for t in code_info["threads"] if t["thread"] == "ActionWorker#1")
            self.assertTrue(any(f["function"] == "blocking_helper" and f["in_repo"] for f in worker_snap["frames"]))
        finally:
            release.set()
            fut.result(timeout=3.0)
            rt.close()

    def test_update_and_save_settings_preserves_secrets_validates_and_hot_applies(self):
        cfg_path = _write_test_config(self.tmp)
        client = FakeGameClient()
        rt = WebConsoleRuntime(
            cfg_path,
            client_factory=lambda _cfg: client,
            world_3d_base_dir=self.tmp / "w3d",
        )
        try:
            rt.start(auto_connect=True, start_paused=True)
            editable = rt.get_editable_settings()
            self.assertTrue(editable["agent"]["has_password"])
            self.assertTrue(editable["llm"]["has_api_key"])
            dumped_editable = json.dumps(editable)
            self.assertNotIn("pw-secret-123", dumped_editable)
            self.assertNotIn("sk-secret-key-987654321", dumped_editable)

            # Invalid value rejected without writing disk
            ok, err = rt.update_and_save_settings({"survival": {"scan_radius": 999}})
            self.assertFalse(ok)
            self.assertIn("配置校验失败", err)
            self.assertEqual(rt.config["survival"]["scan_radius"], 24)

            # Valid update preserves disk secrets and hot-updates live harness objects
            ok, msg = rt.update_and_save_settings({
                "survival": {
                    "scan_radius": 36,
                    "low_health_threshold": 11,
                    "autonomy": {"max_steps": 64},
                },
                "llm": {"api_key": ""},  # blank must keep original secret
                "mining": {"cave_policy": {"flood_max_cells": 220}},
            })
            self.assertTrue(ok, msg)
            self.assertFalse(rt.restart_required)
            self.assertEqual(rt.controller.collector.radius, 36)
            self.assertEqual(rt.controller._safety_rules.low_health, 11)
            self.assertEqual(rt.controller.daily_policy.low_health, 11)
            self.assertEqual(rt.controller.task_controller.limits["max_steps"], 64)

            saved_raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_raw["agent"]["password"], "pw-secret-123")
            self.assertEqual(saved_raw["llm"]["api_key"], "sk-secret-key-987654321")
            self.assertEqual(saved_raw["rcon"]["password"], "rcon-secret-password")
            self.assertEqual(saved_raw["survival"]["scan_radius"], 36)
            self.assertEqual(saved_raw["mining"]["cave_policy"]["flood_max_cells"], 220)

            # Structural change triggers restart_required flag
            ok, msg = rt.update_and_save_settings({"agent": {"mode": "sleep"}})
            self.assertTrue(ok)
            self.assertTrue(rt.restart_required)
            self.assertIn("热重启", msg)
        finally:
            rt.close()


class WebConsoleUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="botsleep_ui_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_build_demo_formatters_and_http_launch(self):
        cfg_path = _write_test_config(self.tmp)
        rt = WebConsoleRuntime(cfg_path, world_3d_base_dir=self.tmp / "w3d")
        rt.start(auto_connect=False)
        try:
            rep = rt.status_report()
            rep["connect_error"] = "<script>alert(1)</script>"
            banner = _format_status_banner(rep)
            self.assertNotIn("<script>alert(1)</script>", banner)
            self.assertIn("&lt;script&gt;", banner)

            stack_md, task_json, res_json = _format_runtime_code(rep["runtime_code"], rep["boards"])
            self.assertIn("阶段:", stack_md)
            self.assertEqual(task_json, "{}")
            self.assertEqual(res_json, "{}")
            self.assertEqual(_format_events_rows([]), [])
            ca, cb, cc = _format_live_cockpit_cards(rep, [])
            self.assertIn("实时代码位置", ca)
            self.assertIn("背包摘要", cb)
            self.assertIn("最近事件流", cc)

            _ensure_loopback_no_proxy()
            demo = build_demo(rt)
            html_components = [comp for comp in demo.blocks.values() if getattr(comp, "server_fns", None)]
            self.assertTrue(html_components)
            viewport = html_components[0]
            scene_payload = viewport.fetch_scene3d({"radius": 20})
            self.assertIn("voxels", scene_payload)
            self.assertIn("bot", scene_payload)

            _ensure_loopback_no_proxy()
            _, url, _ = demo.launch(
                server_name="127.0.0.1",
                server_port=None,
                css=COMPACT_FLAT_CSS,
                prevent_thread_lock=True,
                quiet=True,
            )
            try:
                resp = requests.get(url, proxies={"http": None, "https": None}, timeout=5)
                self.assertEqual(resp.status_code, 200)
                self.assertIn("BotSleep", resp.text)
                rpc = requests.post(
                    url.rstrip("/") + "/gradio_api/component_server/",
                    json={
                        "component_id": viewport._id,
                        "fn_name": "fetch_scene3d",
                        "session_hash": "test-session-1",
                        "data": {"radius": 18},
                    },
                    proxies={"http": None, "https": None},
                    timeout=5,
                )
                self.assertEqual(rpc.status_code, 200)
                rpc_data = rpc.json()
                self.assertIn("voxels", rpc_data)
                self.assertIn("bot", rpc_data)
            finally:
                demo.close()
        finally:
            rt.close()

    def test_main_cli_check_config_and_web_view_dispatch(self):
        cfg_path = _write_test_config(self.tmp)
        self.assertEqual(bot_main.main(["--config", str(cfg_path), "--check-config"]), 0)
        with mock.patch("debug.web_console.launch_web_console", return_value=0) as mocked:
            rc = bot_main.main([
                "--config", str(cfg_path),
                "--mode", "survival",
                "--objective", "daily",
                "--web-view",
                "--web-port", "18945",
                "--web-paused",
                "--web-no-connect",
            ])
        self.assertEqual(rc, 0)
        mocked.assert_called_once()
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["port"], 18945)
        self.assertTrue(kwargs["start_paused"])
        self.assertFalse(kwargs["auto_connect"])


if __name__ == "__main__":
    unittest.main()
