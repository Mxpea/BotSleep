"""Local execution dashboard regressions; mocked listener and offline DOM only."""

import copy
import io
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from debug.monitor import EventMonitor, MonitorServer, REDACTED


def event(name="HEARTBEAT", data=None):
    return {"timestamp": "2026-11-16T00:00:00Z", "module": "test", "event": name,
            "level": "INFO", "data": data or {}}


def summary(phase="running", steps=1, reason=None):
    return {"phase": phase, "reason": reason, "steps": steps,
            "recent": [{"action": "gather_block", "success": True, "progress": True,
                        "error_code": None}]}


class LocalMonitorTests(unittest.TestCase):
    def test_snapshots_keep_published_fields_and_last_six_actions(self):
        monitor = EventMonitor()
        for name in ("HEARTBEAT", "AGENT_STATE"):
            with self.subTest(event=name):
                published = summary(steps=9)
                published["recent"] = [{"action": f"action_{i}"} for i in range(9)]
                original = copy.deepcopy(published)
                monitor.publish(event(name, {"local_execution": published}))
                expected = {**original, "recent": original["recent"][-6:]}
                self.assertEqual(monitor.state()["local_execution"], expected)
                self.assertEqual(published, original)
                published["recent"][-1]["action"] = "caller mutation"
                returned = monitor.state()
                returned["local_execution"]["recent"][-1]["action"] = "reader mutation"
                self.assertEqual(monitor.state()["local_execution"], expected)
                self.assertEqual(monitor.snapshot()["events"][-1]["data"]["local_execution"], original)

    def test_all_local_events_use_summary_without_inventing_model_requests(self):
        monitor = EventMonitor()
        for name, phase, reason in (
                ("LOCAL_EXECUTION_STEP", "running", None),
                ("LOCAL_EXECUTION_RESULT", "blocked_unknown", "uncertain_action"),
                ("LOCAL_EXECUTION_YIELDED", "model_takeover", "no_verified_local_step"),
                ("MODEL_TAKEOVER", "model_takeover", "local_budget_exhausted")):
            with self.subTest(event=name):
                published = summary(phase, reason=reason)
                monitor.publish(event(name, {"summary": published}))
                self.assertEqual(monitor.state()["local_execution"], published)
                self.assertEqual(monitor.state()["llm"]["state"], "idle")
                self.assertIsNone(monitor.state()["llm"]["request"])
                self.assertNotIn("slot_state", monitor.state()["llm"])
        monitor.publish(event("LLM_REQUEST", {"model": "mock-route"}))
        monitor.publish(event("LOCAL_EXECUTION_YIELDED", {"summary": summary("model_takeover")}))
        self.assertEqual(monitor.state()["llm"]["state"], "requesting")

    def test_missing_disabled_and_malformed_state_clear_previous_summary(self):
        monitor = EventMonitor()
        self.assertIsNone(monitor.state()["local_execution"])
        for name in ("HEARTBEAT", "AGENT_STATE", "LOCAL_EXECUTION_RESULT", "MODEL_TAKEOVER"):
            for invalid in (None, False, [], "disabled"):
                with self.subTest(event=name, invalid=invalid):
                    monitor.publish(event("LOCAL_EXECUTION_STEP", {"summary": summary()}))
                    key = "local_execution" if name in {"HEARTBEAT", "AGENT_STATE"} else "summary"
                    monitor.publish(event(name, {key: invalid}))
                    self.assertIsNone(monitor.state()["local_execution"])
            monitor.publish(event("LOCAL_EXECUTION_STEP", {"summary": summary()}))
            monitor.publish(event(name))
            self.assertIsNone(monitor.state()["local_execution"])
        empty = dict.fromkeys(("phase", "reason", "steps", "recent"))
        monitor.publish(event(data={"local_execution": empty}))
        self.assertEqual(monitor.state()["local_execution"], empty)
        monitor.publish(event("AGENT_PHASE", {"phase": "observing"}))
        self.assertEqual(monitor.state()["local_execution"], empty)
        for recent in (None, False, "bad", {}):
            monitor.publish(event(data={"local_execution": {"phase": "running", "recent": recent}}))
            self.assertIsNone(monitor.state()["local_execution"]["recent"])

    def test_latest_decision_source_is_explicit_and_not_inferred_from_planner(self):
        monitor = EventMonitor()
        self.assertIsNone(monitor.state()["decision_source"])
        decision = {"name": "wait", "args": {}, "reason": "fixture"}
        for source in ("local", "model", "manual", "safety", "rules"):
            with self.subTest(source=source):
                monitor.publish(event("DECISION", {"decision": decision, "source": source}))
                monitor.publish(event("STEP_RESULT", {"decision": decision, "result": {"success": True}}))
                monitor.publish(event(data={"planner": "llm", "local_execution": summary()}))
                self.assertEqual(monitor.state()["decision_source"], source)
        for source in (None, "", "future", {"source": "model"}, ["local"]):
            monitor.publish(event("DECISION", {"decision": decision, "source": source}))
            self.assertIsNone(monitor.state()["decision_source"])
        monitor.publish(event("DECISION", {"decision": decision, "source": "local"}))
        monitor.publish(event("DECISION", {"decision": decision}))
        self.assertIsNone(monitor.state()["decision_source"])

    def test_summaries_use_redaction_and_existing_record_limits(self):
        monitor = EventMonitor(max_events=2, max_bytes=4096)
        published = summary(reason="private-local-value Bearer freeform-credential")
        published["recent"][0].update(api_key="private-local-value", action="private-local-value")
        original = copy.deepcopy(published)
        monitor.publish(event("LOCAL_EXECUTION_RESULT", {"summary": published}))
        self.assertEqual(published, original)
        for payload in (monitor.state(), monitor.snapshot()):
            encoded = json.dumps(payload)
            self.assertNotIn("private-local-value", encoded)
            self.assertNotIn("freeform-credential", encoded)
            self.assertIn(REDACTED, encoded)
        retained = monitor.state()["local_execution"]
        for name, key in (("LOCAL_EXECUTION_RESULT", "summary"), ("HEARTBEAT", "local_execution")):
            monitor.publish(event(name, {key: summary(reason="x" * 8000)}))
            self.assertTrue(monitor.snapshot()["events"][-1]["data"]["_monitor_truncated"])
            self.assertEqual(monitor.state()["local_execution"], retained)
        self.assertEqual(len(monitor.snapshot()["events"]), 2)
        before = monitor.state()
        with patch("socket.create_connection", side_effect=AssertionError("No network reads")):
            self.assertEqual(monitor.state(), before)
            monitor.snapshot()


class LocalMonitorHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fake_server = Mock(server_address=("127.0.0.1", 18930))
        self.server = MonitorServer(port=18930)
        with patch("debug.monitor.ThreadingHTTPServer", return_value=self.fake_server) as factory:
            self.server.start()
        self.handler = factory.call_args.args[1]
        self.addCleanup(self.server.close)

    def fetch(self, path):
        connection = Mock()
        connection.makefile.return_value = io.BytesIO(
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:18930\r\nConnection: close\r\n\r\n".encode())
        chunks = []
        connection.sendall.side_effect = chunks.append
        self.handler(connection, ("127.0.0.1", 12345), self.fake_server)
        header, body = b"".join(chunks).split(b"\r\n\r\n", 1)
        self.assertEqual(int(header.split()[1]), 200)
        return body.decode("utf-8")

    def test_state_and_events_expose_only_redacted_published_copies(self):
        published = summary("model_takeover", reason="private-http-key")
        published["recent"][0]["api_key"] = "private-http-key"
        self.server.publish(event("MODEL_TAKEOVER", {"summary": published}))
        self.server.publish(event("DECISION", {"source": "safety", "decision": {"name": "wait"}}))
        before = self.server.state()
        with patch("socket.create_connection", side_effect=AssertionError("No network reads")):
            body = self.fetch("/api/state")
            state = json.loads(body)
            self.assertEqual(state["local_execution"]["reason"], REDACTED)
            self.assertEqual(state["decision_source"], "safety")
            self.assertEqual(state["llm"]["state"], "idle")
            self.assertNotIn("private-http-key", body + self.fetch("/api/events?since=0"))
            self.assertIn('id="local-phase"', self.fetch("/"))
        self.assertEqual(self.server.state(), before)
        self.assertEqual(published["reason"], "private-http-key")

    def test_old_heartbeat_exposes_unknown_instead_of_stale_local_state(self):
        self.server.publish(event("LOCAL_EXECUTION_STEP", {"summary": summary()}))
        self.server.publish(event(data={"planner": "llm"}))
        state = json.loads(self.fetch("/api/state"))
        self.assertIsNone(state["local_execution"])
        self.assertIsNone(state["decision_source"])
        self.assertIsNone(state["llm"]["request"])


class LocalMonitorDashboardTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed for the offline dashboard rendering check")
    def test_actual_script_renders_local_phases_sources_unknowns_and_hostile_text(self):
        html = Path(__file__).resolve().parents[1].joinpath("debug/monitor.html").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", html)
        script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
        script = script.replace("\npoll();\n})();", "\nglobalThis.dashboard={renderState,describe,isAction};\n})();")
        runner = r"""
const vm=require('node:vm'), assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
class Element {
  constructor(){this.children=[];this.text='';this.style={};this.classes=new Set();this.classList={toggle:(name,on)=>on?this.classes.add(name):this.classes.delete(name)};}
  set textContent(value){this.text=String(value);this.children=[];}
  get textContent(){return this.text+this.children.map(item=>item.textContent).join('');}
  set innerHTML(value){throw Error('Unsafe HTML assignment');}
  append(...items){for(const item of items) if(item.fragment)this.children.push(...item.children);else this.children.push(item);}
  replaceChildren(...items){this.text='';this.children=[];this.append(...items);}
  addEventListener(){}
}
const elements=new Map(input.ids.map(id=>[id,new Element()]));
const get=id=>{assert(elements.has(id),`Missing HTML element ${id}`);return elements.get(id)};
const context={document:{getElementById:get,createElement:()=>new Element(),createDocumentFragment:()=>Object.assign(new Element(),{fragment:true}),querySelectorAll:()=>[]},setInterval:()=>{},fetch:()=>{throw Error('No network permitted')}};
vm.createContext(context);vm.runInContext(input.script,context);
const {renderState,describe,isAction}=context.dashboard, text=id=>get(id).textContent;
const state=input.state, hostile='<img src=x onerror=alert(1)>';
renderState(state);
for(const id of ['local-phase','local-steps','local-reason','local-recent','decision-source']) assert(text(id).includes('未确认'),id);
for(const phase of ['running','model_takeover','blocked_unknown']) {
  state.local_execution={phase,steps:0,reason:hostile,recent:[{action:hostile,success:false,progress:false,error_code:'action_unconfirmed'}]};
  renderState(state);
  assert(text('local-phase').includes(phase));assert.equal(text('local-steps'),'本地步数：0');
  assert(text('local-reason').includes(hostile));assert(text('local-recent').includes(hostile));
  assert(text('local-recent').includes('失败'));assert(text('local-recent').includes('无进展'));assert(text('local-recent').includes('action_unconfirmed'));
  assert(!text('llm-state').includes('模型请求中'));assert(!text('model-slot').includes('inflight'));
}
state.local_execution.recent=Array.from({length:9},(_,i)=>({action:'action_'+i,success:true,progress:true}));renderState(state);
assert.equal(get('local-recent').children.length,6);assert(!text('local-recent').includes('action_2'));assert(text('local-recent').includes('action_8'));
for(const [source,label] of Object.entries({local:'本地',model:'模型',manual:'手动',safety:'安全',rules:'规则'})) {
  state.decision_source=source;renderState(state);assert.equal(text('decision-source'),label+' · '+source);
  assert(describe({event:'DECISION',data:{source,decision:{name:'wait'}}}).includes(label));
}
for(const source of [null,undefined,'future',hostile,'toString']) {state.decision_source=source;renderState(state);assert.equal(text('decision-source'),'未确认');}
for(const value of [null,undefined,false,[],{},'disabled',{phase:null,reason:null,steps:null,recent:null}]) {
  state.local_execution=value;renderState(state);
  for(const id of ['local-phase','local-steps','local-reason','local-recent']) assert(text(id).includes('未确认'),id);
}
for(const steps of [-1,true,'4',2.5]) {state.local_execution={steps,recent:{}};renderState(state);assert.equal(text('local-steps'),'本地步数：未确认');assert.equal(text('local-recent'),'未确认');}
state.local_execution={phase:hostile,recent:[null,'bad',{}, {action:'wait',success:null,progress:null}]};renderState(state);
assert.equal(text('local-phase'),hostile);assert(text('local-recent').includes('结果未确认'));assert(text('local-recent').includes('进展未确认'));
state.local_execution={recent:[]};renderState(state);assert.equal(text('local-recent'),'尚无已发布动作');
for(const event of ['LOCAL_EXECUTION_STEP','LOCAL_EXECUTION_RESULT','LOCAL_EXECUTION_YIELDED']) assert(isAction({event}));
assert(describe({event:'MODEL_TAKEOVER',data:{summary:{phase:'model_takeover',reason:'budget',steps:6}}}).includes('budget'));
state.llm.state='requesting';renderState(state);assert.equal(text('llm-state'),'模型请求中');
console.log('Local dashboard rendering checks passed');
"""
        result = subprocess.run([shutil.which("node"), "-e", runner],
                                input=json.dumps({"script": script, "state": EventMonitor().state(),
                                                  "ids": re.findall(r'id="([^"]+)"', html)}),
                                text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
