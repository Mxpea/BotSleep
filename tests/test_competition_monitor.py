"""Published competition snapshots and socket-free read-only dashboard checks."""

import io
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from debug.monitor import EventMonitor, MonitorServer, REDACTED


def event(name="HEARTBEAT", data=None, timestamp="2026-11-15T00:00:00Z"):
    return {"timestamp": timestamp, "level": "INFO", "module": "test",
            "event": name, "data": data or {}}


def board():
    return {"phase": "active", "active_goal_id": "rule.full.id", "rules_version": "v1",
            "progress_card": {"observed": 1, "required": 2},
            "score": {"raw": 7, "completed_count": 1, "difficulty_counts": {"easy": 1}, "normalized": None},
            "tasks": [{"id": "rule.full.id", "title": "任务", "points": 7, "difficulty": "easy",
                       "eligibility": "scored", "availability": "ready", "assessment": "completed"},
                      {"id": "rule.pending", "eligibility": "pending", "availability": "unknown", "assessment": "unknown"},
                      {"id": "rule.prerequisite", "eligibility": "scored", "availability": "needs_prerequisites", "assessment": "not_completed"},
                      {"id": "rule.unsupported", "eligibility": "excluded", "availability": "unsupported"}],
            "last_exit": {"reason": "paused"}, "updated_at": "2026-11-15T00:00:00Z", "stale": False}


class CompetitionMonitorTests(unittest.TestCase):
    def test_board_retains_all_tasks_and_unknown_fields_without_inventing_completion(self):
        monitor = EventMonitor()
        snapshot = board()
        snapshot["future_field"] = {"evidence": ["published"]}
        monitor.publish(event("COMPETITION_TASKS", {"board": snapshot}))
        self.assertEqual(monitor.state()["competition"]["board"], snapshot)
        self.assertEqual(monitor.snapshot()["events"][0]["data"]["board"], snapshot)
        self.assertTrue(all("completion" not in task for task in monitor.state()["competition"]["board"]["tasks"]))
        snapshot["tasks"].clear()
        retrieved = monitor.state()
        retrieved["competition"]["board"]["tasks"].clear()
        self.assertEqual(len(monitor.state()["competition"]["board"]["tasks"]), 4)

    def test_unknown_scores_are_not_calculated_from_task_points_or_assessments(self):
        for score in (None, {}, {"raw": None, "completed_count": None, "normalized": None},
                      {"raw": 0, "completed_count": 0, "difficulty_counts": {}, "normalized": None}):
            with self.subTest(score=score):
                monitor = EventMonitor()
                snapshot = board()
                snapshot["score"] = score
                monitor.publish(event("COMPETITION_TASKS", {"board": snapshot}))
                self.assertEqual(monitor.state()["competition"]["board"]["score"], score)

    def test_heartbeat_and_agent_state_accept_competition_without_changing_daily(self):
        monitor = EventMonitor()
        daily = {"phase": "maintenance", "tasks": [{"id": "food_reserve", "status": "ready"}]}
        monitor.publish(event("DAILY_TASKS", {"board": daily, "memory_counts": {"places": 2}}))
        for name in ("HEARTBEAT", "AGENT_STATE"):
            for wrapped in (False, True):
                with self.subTest(name=name, wrapped=wrapped):
                    snapshot = board()
                    monitor.publish(event(name, {"objective": "competition", "competition": {"board": snapshot} if wrapped else snapshot}))
                    self.assertEqual(monitor.state()["competition"]["board"], snapshot)
                    self.assertEqual(monitor.state()["daily"], {"board": daily, "memory_counts": {"places": 2}})
        monitor.publish(event(data={"objective": "daily", "daily_tasks": daily, "memory_summary": {"places": 3}}))
        self.assertEqual(monitor.state()["daily"], {"board": daily, "memory_counts": {"places": 3}})
        self.assertEqual(monitor.state()["competition"]["board"], snapshot)

    def test_partial_and_invalid_boards_do_not_crash_or_merge_old_completion(self):
        monitor = EventMonitor()
        monitor.publish(event("COMPETITION_TASKS", {"board": board()}))
        monitor.publish(event("COMPETITION_TASKS", {"board": {"phase": "selection", "tasks": [{}]}}))
        self.assertEqual(monitor.state()["competition"]["board"], {"phase": "selection", "tasks": [{}]})
        for invalid in (False, [], "unknown"):
            monitor.publish(event("COMPETITION_TASKS", {"board": invalid}))
            self.assertEqual(monitor.state()["competition"]["board"]["tasks"], [{}])
        monitor.publish(event("COMPETITION_TASKS", {"board": None}))
        self.assertIsNone(monitor.state()["competition"]["board"])

    def test_competition_uses_existing_redaction_and_event_size_limits(self):
        monitor = EventMonitor(max_events=2, max_bytes=4096)
        snapshot = board()
        snapshot["api_key"] = "private-competition-key"
        snapshot["tasks"][0]["completion"] = {"authorization": "Bearer private-authorization"}
        snapshot["message"] = "private-competition-key Bearer another-private-token"
        monitor.publish(event("COMPETITION_TASKS", {"board": snapshot}))
        for value in (monitor.state(), monitor.snapshot()):
            encoded = json.dumps(value)
            self.assertNotIn("private-competition-key", encoded)
            self.assertNotIn("private-authorization", encoded)
            self.assertNotIn("another-private-token", encoded)
            self.assertIn(REDACTED, encoded)
        retained = monitor.state()["competition"]["board"]
        monitor.publish(event("COMPETITION_TASKS", {"board": {"tasks": [{"title": "x" * 8000}]}}))
        self.assertTrue(monitor.snapshot()["events"][-1]["data"]["_monitor_truncated"])
        self.assertEqual(monitor.state()["competition"]["board"], retained)
        for _ in range(4):
            monitor.publish(event("AGENT_PHASE", {"phase": "waiting"}))
        self.assertLessEqual(len(monitor.snapshot()["events"]), 2)

    def test_heartbeat_age_is_independent_of_other_events_and_reads_are_passive(self):
        monitor = EventMonitor()
        self.assertIsNone(monitor.state()["heartbeat_at"])
        monitor.publish(event())
        monitor.publish(event("COMPETITION_TASKS", {"board": board()}, timestamp="2026-11-15T00:01:00Z"))
        before = monitor.state()
        self.assertEqual(before["heartbeat_at"], "2026-11-15T00:00:00Z")
        self.assertNotEqual(before["heartbeat_at"], before["updated_at"])
        with patch("socket.create_connection", side_effect=AssertionError("No network reads")):
            for _ in range(3):
                self.assertEqual(monitor.state(), before)
                monitor.snapshot()

    def test_successful_competition_end_is_terminal_in_monitor_immediately(self):
        monitor = EventMonitor()
        monitor.publish(event("STEP_RESULT", {"step": 4, "decision": {"name": "end_competition"},
            "result": {"success": True, "data": {"phase": "completed", "completed_session": True}}}))
        self.assertEqual(monitor.state()["run"]["state"], "completed")
        monitor.publish(event("STEP_RESULT", {"step": 5, "decision": {"name": "end_competition"},
            "result": {"success": False, "data": {"phase": "completed", "completed_session": True}}}))
        self.assertNotEqual(monitor.state()["run"]["state"], "completed")

    def test_model_slots_and_game_pending_remain_independent(self):
        monitor = EventMonitor()
        for name, expected in (("MODEL_SLOT_ACQUIRED", "inflight"), ("MODEL_REQUEST_UNCERTAIN", "uncertain"),
                               ("MODEL_IDLE_ACKNOWLEDGED", "idle")):
            monitor.publish(event(name))
            monitor.publish(event(data={"objective": "competition", "paused": True, "busy": False,
                                       "pending": {"goal_id": "rule.full.id"}, "competition": board()}))
            state = monitor.state()
            self.assertEqual(state["llm"]["slot_state"], expected)
            self.assertEqual(state["run"]["pending"], {"goal_id": "rule.full.id"})
            self.assertTrue(state["run"]["paused"])


class CompetitionHTTPTests(unittest.TestCase):
    """Run the actual HTTP handler using in-memory streams, without opening sockets."""

    def setUp(self):
        self.fake_server = Mock(server_address=("127.0.0.1", 18930))
        self.server = MonitorServer(port=18930)
        with patch("debug.monitor.ThreadingHTTPServer", return_value=self.fake_server) as factory:
            self.server.start()
        self.handler = factory.call_args.args[1]
        self.addCleanup(self.server.close)
        self.server.publish(event("COMPETITION_TASKS", {"board": board()}))

    def fetch(self, path, method="GET"):
        connection = Mock()
        connection.makefile.return_value = io.BytesIO(
            f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:18930\r\nConnection: close\r\n\r\n".encode())
        chunks = []
        connection.sendall.side_effect = chunks.append
        self.handler(connection, ("127.0.0.1", 12345), self.fake_server)
        header, body = b"".join(chunks).split(b"\r\n\r\n", 1)
        return int(header.split()[1]), body.decode("utf-8")

    def test_only_dashboard_and_published_data_are_served(self):
        with patch("socket.create_connection", side_effect=AssertionError("No network reads")):
            self.assertEqual(self.fetch("/")[0], 200)
            code, body = self.fetch("/api/state")
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)["competition"]["board"], board())
            self.assertEqual(self.fetch("/api/events?since=0")[0], 200)

    def test_control_admin_unlock_and_write_endpoints_cannot_change_state(self):
        before = self.server.state()
        for path in ("/api/resume", "/api/stop", "/api/admin", "/api/unlock", "/api/acknowledge-model-idle",
                     "/api/competition", "/config.yaml"):
            with self.subTest(path=path):
                self.assertEqual(self.fetch(path)[0], 404)
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"):
            for path in ("/api/state", "/api/competition", "/api/unlock"):
                with self.subTest(method=method, path=path):
                    self.assertEqual(self.fetch(path, method)[0], 405)
        self.assertEqual(self.server.state(), before)


class CompetitionDashboardTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed for the offline dashboard rendering check")
    def test_actual_script_renders_missing_fields_scores_task_states_and_daily(self):
        html = Path(__file__).resolve().parents[1].joinpath("debug/monitor.html").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", html)
        self.assertNotIn("compact", html.lower())
        script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
        script = script.replace("\npoll();\n})();", "\nglobalThis.dashboard={renderState,renderFreshness};\n})();")
        runner = r"""
const vm=require('node:vm'), assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
class Element {
  constructor(){this.children=[];this.text='';this.style={};this.classes=new Set();this.classList={toggle:(name,on)=>on?this.classes.add(name):this.classes.delete(name)};}
  set textContent(value){this.text=String(value);this.children=[];}
  get textContent(){return this.text+this.children.map(item=>item.textContent).join('');}
  append(...items){this.children.push(...items.flatMap(item=>item.fragment?item.children:[item]));}
  replaceChildren(...items){this.text='';this.children=[];this.append(...items);}
  addEventListener(){}
}
const elements=new Map(input.ids.map(id=>[id,new Element()]));
const get=id=>{assert(elements.has(id),`Missing HTML element ${id}`);return elements.get(id)};
let intervals=[];
const context={document:{getElementById:get,createElement:()=>new Element(),createDocumentFragment:()=>Object.assign(new Element(),{fragment:true}),querySelectorAll:()=>[]},setInterval:fn=>intervals.push(fn),fetch:()=>{throw Error('No network permitted')}};
vm.createContext(context);vm.runInContext(input.script,context);
const render=context.dashboard.renderState, text=id=>get(id).textContent;
const base={has_data:true,run:{objective:'competition',busy:false,paused:false},llm:{state:'idle'},competition:{board:input.board}};
render(base);
assert.equal(text('competition-score'),'7');
assert.equal(text('competition-normalized'),'归一化分：未确认');
for(const label of ['rule.full.id','可计分','待条件确认','待前置','blocked','已完成','未完成']) assert(text('competition-tasks').includes(label),label);
assert(text('model-slot').startsWith('idle'));
base.llm.slot_state='inflight';render(base);assert(text('model-slot').startsWith('inflight'));
base.llm.slot_state='uncertain';base.run.pending={goal:'unverified'};base.run.paused=true;render(base);
assert(text('model-slot').startsWith('uncertain'));assert(!text('model-blocked-banner').includes('避险继续'));assert(!text('paused-banner').includes('#resume'));assert(text('game-state').includes('paused 是'));assert(text('game-state-note').includes('unverified'));
for(const score of [null,{}, {raw:null,normalized:null}, {raw:'7'}]) {base.competition.board={tasks:input.board.tasks,score};render(base);assert.equal(text('competition-score'),'未确认');}
base.competition.board={score:{raw:0,completed_count:0,normalized:null}};render(base);assert.equal(text('competition-score'),'0');assert.equal(text('competition-completed'),'已完成数量：0');
for(const malformed of [null,{}, {tasks:null}, {tasks:{}}, {tasks:[null,'bad',{}, {assessment:{status:'unknown'}}]}, {score:[],progress_card:['safe']}]) {base.competition.board=malformed;render(base);}
base.competition.board={tasks:[{eligibility:'scored',availability:'ready'}]};render(base);
assert(text('competition-tasks').includes('未确认'));assert(!text('competition-tasks').includes('已完成'));
base.competition.board={tasks:[{title:'<img src=x onerror=alert(1)>',assessment:'future_status'}]};render(base);assert(text('competition-tasks').includes('<img src=x'));assert(text('competition-tasks').includes('future_status'));
base.competition.board={catalog_assessment:{checked:120,total:120,known_count:99,complete:true},end_reason:'done',stale:true};render(base);assert(text('competition-message').includes('120 / 120'));assert(text('competition-message').includes('99'));assert(text('competition-message').includes('done'));assert(text('competition-freshness').includes('是（运行时标记）'));
base.run.pending=null;render(base);assert(text('game-state-note').includes('无待核验意图'));
base.heartbeat_at=new Date(Date.now()-60000).toISOString();render(base);assert(text('heartbeat-state').includes('stale'));
base.heartbeat_at=new Date().toISOString();render(base);assert(text('heartbeat-state').includes('新鲜'));
base.heartbeat_at='invalid';render(base);assert(text('heartbeat-state').includes('未确认'));
base.heartbeat_at=new Date(Date.now()-60000).toISOString();intervals.forEach(fn=>fn());assert(text('heartbeat-state').includes('stale'));
const daily={has_data:true,status:{busy:false},run:{objective:'daily',phase:'acting',busy:true,paused:false},competition:base.competition,llm:{state:'idle'},daily:{board:{tasks:[{id:'food_reserve',status:'ready'}]},memory_counts:{places:2}}};
render(daily);assert(get('competition-panel').classes.has('hidden'));assert(!get('daily-panel').classes.has('hidden'));assert(text('daily-tasks').includes('食物储备'));assert(text('daily-tasks').includes('可推进'));assert(text('memory-summary').includes('地点 2'));assert(text('game-state').includes('busy 是'));
console.log('Dashboard rendering checks passed');
"""
        result = subprocess.run([shutil.which("node"), "-e", runner],
                                input=json.dumps({"script": script, "board": board(), "ids": re.findall(r'id="([^"]+)"', html)}),
                                text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
