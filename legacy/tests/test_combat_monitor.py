"""Read-only monitor preserves advice freshness and explicit combat evidence."""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from debug.monitor import EventMonitor, REDACTED


def event(name, data):
    return {"event": name, "timestamp": "2026-11-16T00:00:00Z", "data": data}


class CombatMonitorTests(unittest.TestCase):
    def test_published_advice_replaces_stale_data_without_assessing_it(self):
        monitor = EventMonitor()
        plan = {"stage": "dragon", "state": "blocked", "fresh": False, "unsupported": ["dragon_combat"]}
        with patch("socket.create_connection", side_effect=AssertionError("No game or model reads")):
            for name in ("HEARTBEAT", "AGENT_STATE"):
                monitor.publish(event(name, {"playbook": plan, "combat": {"allowed": False}}))
                self.assertEqual(monitor.state()["playbook"], plan)
                self.assertEqual(monitor.state()["combat"], {"allowed": False})
            monitor.publish(event("HEARTBEAT", {"playbook": None, "combat": None}))
            self.assertIsNone(monitor.state()["playbook"])
            self.assertIsNone(monitor.state()["combat"])

    def test_success_does_not_invent_damage_or_kill_and_unrelated_results_do_not_replace_it(self):
        monitor = EventMonitor()
        result = {"success": True, "data": {"skill": "fight_hostile", "attack_sent": True, "damage_verified": False}}
        monitor.publish(event("STEP_RESULT", {"result": result}))
        saved = monitor.state()["combat_result"]
        self.assertEqual(saved["result"], result)
        self.assertNotIn("killed", saved["result"]["data"])
        monitor.publish(event("DECISION", {"decision": {"name": "fight_hostile"}}))
        monitor.publish(event("STEP_RESULT", {"result": {"success": True}}))
        self.assertEqual(monitor.state()["combat_result"], saved)
        for invalid in (["fight_hostile"], {"name": "fight_hostile"}):
            monitor.publish(event("STEP_RESULT", {"result": {"data": {"skill": invalid}}}))
            self.assertEqual(monitor.state()["combat_result"], saved)
        monitor.publish(event("STEP_RESULT", {"decision": {"name": "release_defense"},
                        "result": {"success": False, "error_code": "action_unconfirmed", "data": {}}}))
        self.assertEqual(monitor.state()["combat_result"]["skill"], "release_defense")

    def test_combat_evidence_is_redacted_and_copied(self):
        monitor = EventMonitor()
        monitor.publish(event("STEP_RESULT", {"decision": {"name": "hunt_with_lava"}, "result": {
            "success": False, "data": {"source_recovered": True, "api_key": "private-lava-key"},
            "message": "private-lava-key"}}))
        state = monitor.state()
        self.assertNotIn("private-lava-key", json.dumps(state))
        self.assertIn(REDACTED, json.dumps(state))
        state["combat_result"]["result"]["data"].clear()
        self.assertTrue(monitor.state()["combat_result"]["result"]["data"]["source_recovered"])

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the offline dashboard check")
    def test_actual_dashboard_preserves_unknown_and_stale_without_network(self):
        html = Path(__file__).resolve().parents[1].joinpath("debug/monitor.html").read_text(encoding="utf-8")
        script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
        script = script.replace("\npoll();\n})();", "\nglobalThis.renderSurvival=renderSurvival;\n})();")
        runner = r"""
const vm=require('node:vm'), assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const elements=new Map(input.ids.map(id=>[id,{textContent:'',classList:{toggle(){}},addEventListener(){}}]));
const context={document:{getElementById:id=>{assert(elements.has(id));return elements.get(id)},querySelectorAll:()=>[]},setInterval(){},fetch(){throw Error('No network')}};
vm.createContext(context);vm.runInContext(input.script,context);
context.renderSurvival({playbook:{stage:'dragon',state:'blocked',fresh:false,unsupported:['dragon_combat']},combat_result:{skill:'fight_hostile',result:{success:true,data:{attack_sent:true,damage_verified:false}}}});
const text=id=>elements.get(id).textContent;
assert(text('survival-freshness').includes('旧阶段建议'));
assert(text('survival-stage').includes('blocked'));
assert(text('combat-evidence').includes('伤害已确认：否'));
assert(text('combat-evidence').includes('击杀已确认：未确认'));
assert(text('combat-evidence').includes('岩浆源已回收：未确认'));
context.renderSurvival({playbook:{stage:'<script>alert(1)</script>',fresh:true}});
assert(text('survival-stage').includes('<script>alert(1)</script>'));
"""
        self.assertNotIn("innerHTML", html)
        result = subprocess.run([shutil.which("node"), "-e", runner], input=json.dumps({
            "ids": re.findall(r'id="([^"]+)"', html), "script": script}),
            capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
