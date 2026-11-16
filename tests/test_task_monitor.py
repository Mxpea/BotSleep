"""Published long-task snapshots and socket-free dashboard rendering checks."""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from debug.monitor import EventMonitor, REDACTED


def event(name, task):
    return {"event": name, "timestamp": "2026-11-16T08:00:00Z",
            "data": {"task_execution": task}}


def snapshot():
    return {"task_id": "task-1", "task": "gather", "status": "running",
            "args": {"resource": "logs", "count": 8},
            "progress": {"current": 2, "target": 8, "unit": "items"},
            "current_step": {"name": "gather_block", "args": {"pos": [1, 64, 2]}},
            "local_recoveries": 1, "emergency_interruptions": 2, "ai_escalations": 0,
            "last_escalation_reason": None, "reason": "Verified inventory increased",
            "fresh": True, "updated_at": "2026-11-16T08:00:00Z", "steps": 3, "max_steps": 128,
            "recent_results": [{"name": "gather_block", "success": True, "gained": 1}]}


class TaskMonitorTests(unittest.TestCase):
    def test_full_snapshots_replace_without_inventing_task_completion(self):
        monitor = EventMonitor()
        self.assertIsNone(monitor.state()["task_execution"])
        task = snapshot()
        monitor.publish(event("TASK_STARTED", task))
        self.assertEqual(monitor.state()["task_execution"], task)
        monitor.publish({"event": "STEP_RESULT", "data": {"result": {"success": True,
                        "data": {"completed": True}}, "decision": {"name": "gather_block"}}})
        self.assertEqual(monitor.state()["task_execution"], task)
        partial = {"task_id": "task-2", "progress": {"current": 8, "target": 8}}
        monitor.publish(event("TASK_COMPLETED", partial))
        self.assertEqual(monitor.state()["task_execution"], partial)
        self.assertNotEqual(monitor.state()["run"]["state"], "completed")
        self.assertNotIn("status", monitor.state()["task_execution"])

    def test_every_task_event_and_heartbeat_preserves_published_counters(self):
        monitor = EventMonitor()
        names = ("HEARTBEAT", "AGENT_STATE", "TASK_STARTED", "TASK_PROGRESS", "TASK_SUSPENDED",
                 "TASK_COMPLETED", "TASK_NEEDS_AI", "TASK_UNKNOWN", "TASK_CANCELLED")
        task = snapshot()
        task.update(model_planning_rounds=0, model_calls_avoided=5)
        for name in names:
            with self.subTest(event=name):
                monitor.publish(event(name, task))
                monitor.publish(event(name, task))
                self.assertEqual(monitor.state()["task_execution"], task)
        self.assertEqual(monitor.state()["task_execution"]["ai_escalations"], 0)

    def test_unknown_result_and_suspension_do_not_change_native_model_or_pending(self):
        monitor = EventMonitor()
        monitor.publish({"event": "LLM_REQUEST", "data": {"model": "route"}})
        monitor.publish({"event": "HEARTBEAT", "data": {"pending": {"name": "gather_block"},
                        "paused": True, "daily_tasks": {"tasks": []}}})
        for status in ("suspended", "needs_ai", "unknown", "cancelled", "completed"):
            task = {"status": status, "fresh": False}
            monitor.publish(event("TASK_PROGRESS", task))
            state = monitor.state()
            self.assertEqual(state["task_execution"], task)
            self.assertEqual(state["llm"]["state"], "requesting")
            self.assertEqual(state["run"]["pending"], {"name": "gather_block"})
            self.assertTrue(state["run"]["paused"])
            self.assertEqual(state["daily"]["board"], {"tasks": []})

    def test_clear_and_invalid_snapshot_handling_never_merge_stale_counters(self):
        monitor = EventMonitor()
        task = snapshot()
        monitor.publish(event("TASK_PROGRESS", task))
        for invalid in (False, [], "running"):
            monitor.publish(event("HEARTBEAT", invalid))
            self.assertEqual(monitor.state()["task_execution"], task)
        monitor.publish(event("AGENT_STATE", {}))
        self.assertEqual(monitor.state()["task_execution"], {})
        monitor.publish(event("HEARTBEAT", None))
        self.assertIsNone(monitor.state()["task_execution"])

    def test_redaction_copy_and_bounded_recent_results_are_reused(self):
        monitor = EventMonitor()
        task = snapshot()
        task.update(api_key="private-task-key", reason="private-task-key Bearer private-token")
        task["last_escalation_reason"] = "password=private-password"
        task["recent_results"] = [{"step": i, "token": "private-task-token"} for i in range(12)]
        monitor.publish(event("TASK_NEEDS_AI", task))
        state = monitor.state()
        text = json.dumps(state) + json.dumps(monitor.snapshot())
        for secret in ("private-task-key", "private-token", "private-password", "private-task-token"):
            self.assertNotIn(secret, text)
        self.assertIn(REDACTED, text)
        self.assertEqual([row["step"] for row in state["task_execution"]["recent_results"]], list(range(4, 12)))
        task["args"].clear()
        state["task_execution"]["args"].clear()
        self.assertEqual(monitor.state()["task_execution"]["args"], {"resource": "logs", "count": 8})

    def test_oversized_event_does_not_replace_last_known_snapshot(self):
        monitor = EventMonitor(max_bytes=4096)
        task = snapshot()
        monitor.publish(event("TASK_STARTED", task))
        monitor.publish(event("TASK_PROGRESS", {"reason": "x" * 10000}))
        self.assertTrue(monitor.snapshot()["events"][-1]["data"]["_monitor_truncated"])
        self.assertEqual(monitor.state()["task_execution"], task)

    def test_state_reads_and_projection_never_contact_game_or_provider(self):
        monitor = EventMonitor()
        with patch("socket.create_connection", side_effect=AssertionError("Read-only event projection")):
            monitor.publish(event("TASK_PROGRESS", snapshot()))
            before = monitor.state()
            for _ in range(3):
                self.assertEqual(monitor.state(), before)
                monitor.snapshot()

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the offline dashboard check")
    def test_actual_dashboard_renders_unknowns_counters_and_atomic_evidence_without_network(self):
        html = Path(__file__).resolve().parents[1].joinpath("debug/monitor.html").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", html)
        script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
        script = script.replace("\npoll();\n})();", "\nglobalThis.taskDashboard={renderTask,isAction,describe};\n})();")
        runner = r"""
const vm=require('node:vm'), assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const elements=new Map(input.ids.map(id=>[id,{textContent:'',className:'',classes:new Set(),classList:{toggle(name,on){const target=elements.get(id);on?target.classes.add(name):target.classes.delete(name)}},addEventListener(){}}]));
const context={document:{getElementById:id=>{assert(elements.has(id));return elements.get(id)},querySelectorAll:()=>[]},setInterval(){},fetch(){throw Error('No network')}};
vm.createContext(context);vm.runInContext(input.script,context);
const {renderTask,isAction,describe}=context.taskDashboard;
const text=id=>elements.get(id).textContent;
renderTask({});assert(elements.get('task-panel').classes.has('hidden'));
renderTask({task_execution:input.task});
assert(!elements.get('task-panel').classes.has('hidden'));
assert(text('task-name').includes('采集材料'));assert(text('task-id').includes('task-1'));
assert(text('task-progress').includes('2 / 8'));assert(text('task-steps').includes('3 / 128'));
assert(text('task-current-step').includes('gather_block'));assert(text('task-recent-results').includes('gained'));
assert(text('task-recoveries').includes('1'));assert(text('task-interruptions').includes('2'));
assert(text('task-escalations').includes('0'));assert(text('task-model-counts').includes('AI规划轮次：未确认'));
assert(!text('task-status').includes('已完成'));
for(const status of ['idle','running','suspended','completed','needs_ai','unknown','cancelled']) {
 renderTask({task_execution:{status,fresh:false,reason:'published',last_escalation_reason:'no_route',ai_escalations:0}});
 assert(!text('task-status').includes('未确认'));assert(text('task-freshness').includes('旧快照'));
 assert(text('task-escalation-reason').includes('no_route'));assert(text('task-escalations').includes('0'));
}
for(const bad of [null,{}, {progress:{current:8,target:8}}, {progress:[],status:null}, {current_step:[],recent_results:{}}, {local_recoveries:'0',emergency_interruptions:-1,ai_escalations:true}]) {
 renderTask({task_execution:bad});assert.equal(text('task-status'),'未确认');
 assert(text('task-model-counts').includes('未确认'));
}
assert(text('task-recoveries').includes('未确认'));assert(text('task-interruptions').includes('未确认'));assert(text('task-escalations').includes('未确认'));
renderTask({task_execution:{state:'running',progress:{current:0,target:0,unit:'items'},model_planning_rounds:0,model_calls_avoided:0,current_step:{name:'<img src=x onerror=alert(1)>'},recent_results:Array.from({length:10},(_,i)=>({step:i}))}});
assert(text('task-status').includes('执行中'));assert(text('task-progress').includes('0 / 0'));assert(!text('task-status').includes('已完成'));
assert(text('task-current-step').includes('<img src=x'));assert(text('task-model-counts').includes('AI规划轮次：0'));assert(text('task-model-counts').includes('程序子步骤：0'));
assert.equal(JSON.parse(text('task-recent-results')).length,8);assert.equal(JSON.parse(text('task-recent-results'))[0].step,2);
assert(isAction({event:'TASK_NEEDS_AI'}));assert(describe({event:'TASK_PROGRESS',data:{task_execution:{task:'gather',status:'unknown',reason:'bounded search'}}}).includes('bounded search'));
"""
        result = subprocess.run([shutil.which("node"), "-e", runner], input=json.dumps({
            "ids": re.findall(r'id="([^"]+)"', html), "script": script, "task": snapshot()}),
            capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
