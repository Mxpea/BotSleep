"""Old game observations must not look like current health after failed reads."""

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from unittest.mock import patch

from debug.monitor import EventMonitor


def record(name, data=None, second=0):
    return {"event": name, "timestamp": f"2026-11-16T10:00:{second:02d}Z", "data": data or {}}


class MonitorFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.monitor = EventMonitor()
        self.status = {"ready": True, "alive": True, "health": .33, "food": 17,
                       "pos": [5.5, 121, 5.4], "dimension": "overworld", "busy": None}
        self.monitor.publish(record("HEARTBEAT", {"status": self.status}))

    def test_read_failure_keeps_last_status_and_observation_time_but_invalidates_freshness(self):
        before = self.monitor.state()
        self.assertTrue(before["status_fresh"])
        self.monitor.publish(record("HEARTBEAT_FAILED", {"code": "death_loop_cooldown"}, 2))
        state = self.monitor.state()
        self.assertEqual(state["status"], self.status)
        self.assertTrue(state["status"]["alive"])  # Historical fact, not changed by an inference.
        self.assertFalse(state["status_fresh"])
        self.assertEqual(state["status_connection"], "unavailable")
        self.assertEqual(state["status_updated_at"], before["status_updated_at"])
        self.assertEqual(state["heartbeat_at"], before["heartbeat_at"])
        self.assertEqual(state["updated_at"], "2026-11-16T10:00:02Z")
        self.assertEqual(state["status_error"]["code"], "death_loop_cooldown")

    def test_unrelated_activity_and_empty_heartbeat_cannot_restore_status(self):
        self.monitor.publish(record("HEARTBEAT_FAILED", {"code": "not_connected"}, 1))
        for name, data in (("LLM_RESPONSE", {"response": {"model": "test", "choices": []}}),
                           ("AGENT_PHASE", {"phase": "observing"}), ("RESUMED", {}),
                           ("HEARTBEAT", {"steps": 5}), ("AGENT_STATE", {"status": {}})):
            with self.subTest(name=name):
                self.monitor.publish(record(name, data, 3))
                state = self.monitor.state()
                self.assertFalse(state["status_fresh"])
                self.assertEqual(state["status_connection"], "unavailable")
                self.assertEqual(state["status"], self.status)
                self.assertEqual(state["status_updated_at"], "2026-11-16T10:00:00Z")

    def test_successful_new_status_restores_connection_without_inferring_alive_from_health(self):
        for name in ("HEARTBEAT", "AGENT_STATE", "CONNECTED"):
            with self.subTest(name=name):
                self.monitor.publish(record("HEARTBEAT_FAILED", {"code": "not_connected"}, 2))
                fresh = {**self.status, "alive": False, "health": 20}
                self.monitor.publish(record(name, {"status": fresh}, 3))
                state = self.monitor.state()
                self.assertTrue(state["status_fresh"])
                self.assertEqual(state["status_connection"], "available")
                self.assertIsNone(state["status_error"])
                self.assertFalse(state["status"]["alive"])
                self.assertEqual(state["status_updated_at"], "2026-11-16T10:00:03Z")

    def test_explicit_disconnection_maintenance_and_stale_observation_never_restore_freshness(self):
        cases = [({"connected": False}, {}), ({"ready": False}, {}),
                 ({"maintenance": True, "last_observed_at": "2026-11-14T06:19:29Z"}, {}),
                 ({"fresh": False}, {}), ({}, {"status_fresh": False}), ({}, {"fresh": False})]
        for changes, metadata in cases:
            with self.subTest(changes=changes, metadata=metadata):
                status = {**self.status, **changes}
                self.monitor.publish(record("AGENT_STATE", {"status": status, **metadata}, 4))
                state = self.monitor.state()
                self.assertFalse(state["status_fresh"])
                self.assertEqual(state["status"], status)
                if "last_observed_at" in status:
                    self.assertEqual(state["status_updated_at"], status["last_observed_at"])

    def test_connection_errors_in_step_results_and_idle_reads_invalidate_status(self):
        failures = [("STEP_RESULT", {"result": {"success": False, "error_code": code}})
                    for code in ("not_connected", "death_loop", "death_loop_cooldown", "unauthorized")]
        failures += [("IDLE_STATUS_UNAVAILABLE", {"code": "network_error"}),
                     ("PAUSED", {"reason": "status_unavailable_during_action"})]
        for name, data in failures:
            with self.subTest(name=name, data=data):
                self.monitor.publish(record("HEARTBEAT", {"status": self.status}))
                self.monitor.publish(record(name, data, 2))
                self.assertFalse(self.monitor.state()["status_fresh"])
                self.assertEqual(self.monitor.state()["status_connection"], "unavailable")

    def test_republished_observation_from_before_failure_does_not_appear_reconnected(self):
        self.monitor.publish(record("HEARTBEAT_FAILED", {"code": "not_connected"}, 2))
        self.monitor.publish(record("AGENT_STATE", {"status": self.status,
            "status_updated_at": "2026-11-16T10:00:00Z"}, 3))
        state = self.monitor.state()
        self.assertFalse(state["status_fresh"])
        self.assertEqual(state["status_connection"], "unavailable")
        self.assertEqual(state["status_error"]["code"], "not_connected")
        self.monitor.publish(record("AGENT_STATE", {"status": self.status,
            "status_updated_at": "2026-11-16T10:00:04Z"}, 4))
        self.assertTrue(self.monitor.state()["status_fresh"])

    def test_known_skill_failure_is_not_evidence_of_disconnection(self):
        self.monitor.publish(record("STEP_RESULT", {"result": {"success": False, "error_code": "path_not_found"}}, 1))
        self.assertTrue(self.monitor.state()["status_fresh"])
        self.assertEqual(self.monitor.state()["status_connection"], "available")

    def test_projection_is_read_only_and_shutdown_leaves_historical_status(self):
        with patch("socket.create_connection", side_effect=AssertionError("No network")):
            self.monitor.publish(record("SHUTDOWN", {}, 2))
            state = self.monitor.state()
            self.monitor.snapshot()
        self.assertEqual(state["status"], self.status)
        self.assertFalse(state["status_fresh"])
        self.assertEqual(state["status_error"]["code"], "agent_stopped")
        self.assertEqual(state["run"]["state"], "stopped")

    @unittest.skipUnless(shutil.which("node"), "Node is required for the offline dashboard check")
    def test_actual_script_labels_old_health_connection_and_time_then_recovers(self):
        fresh = self.monitor.state()
        self.monitor.publish(record("HEARTBEAT_FAILED", {"code": "death_loop_cooldown"}, 2))
        stale = self.monitor.state()
        html = Path(__file__).resolve().parents[1].joinpath("debug/monitor.html").read_text(encoding="utf-8")
        script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
        script = script.replace("\npoll();\n})();", "\nglobalThis.statusDashboard={renderStatusObservation,setState:s=>{currentState=s;renderFreshness();}};\n})();")
        runner = r"""
const vm=require('node:vm'), assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const elements=new Map(input.ids.map(id=>[id,{textContent:'',style:{},classList:{toggle(){}},addEventListener(){}}]));
let now=Date.parse('2026-11-16T10:00:03Z');
class ClockDate extends Date {static now(){return now;}}
let intervals=[];
const context={Date:ClockDate,document:{getElementById:id=>{assert(elements.has(id));return elements.get(id)},querySelectorAll:()=>[]},setInterval:fn=>intervals.push(fn),fetch(){throw Error('No network')}};
vm.createContext(context);vm.runInContext(input.script,context);
const render=context.statusDashboard.setState, text=id=>elements.get(id).textContent;
render(input.fresh);
assert.equal(text('health'),'0.33 / 20');assert(text('status-observation').includes('游戏连接可用'));assert(text('status-observation').includes('当前存活：是'));
render(input.stale);
assert.equal(text('health'),'上次 0.33 / 20');assert(text('position').startsWith('上次 '));assert(text('food').startsWith('上次 '));
assert(text('status-observation').includes('游戏连接不可用'));assert(text('status-observation').includes('旧观察'));assert(text('status-observation').includes('当前生命状态未确认'));assert(text('status-observation').includes('death_loop_cooldown'));assert(!text('status-observation').includes('当前存活：是'));
assert.equal(elements.get('health-bar').style.opacity,'.35');
render(input.fresh);assert.equal(text('health'),'0.33 / 20');assert.equal(elements.get('health-bar').style.opacity,'1');assert(!text('status-observation').includes('death_loop'));
now+=16000;intervals.forEach(fn=>fn());assert(text('health').startsWith('上次 '));assert(text('status-observation').includes('当前生命状态未确认'));
const explicitDead={...input.fresh,status_updated_at:new ClockDate(now).toISOString(),status:{health:20,alive:false}};
render(explicitDead);assert(text('status-observation').includes('当前存活：否'));
delete explicitDead.status.alive;render(explicitDead);assert(text('status-observation').includes('当前生命状态未确认'));
render({});assert.equal(text('health'),'— / 20');assert(text('status-observation').includes('未确认'));
"""
        result = subprocess.run([shutil.which("node"), "-e", runner], input=json.dumps({
            "ids": re.findall(r'id="([^"]+)"', html), "script": script,
            "fresh": fresh, "stale": stale}), capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
