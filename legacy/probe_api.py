#!/usr/bin/env python3
"""mc-agent-api 探测脚本(仅标准库)。

默认只做观察类调用。--lock-test 会让 bot 原地跳 3 秒,用来验证 busy 与 /stop 的语义。
注意:登录会顶掉同名角色的旧会话(例如正在运行的 BotSleep),结束时会登出。

用法(PowerShell):
  $env:MC_AGENT_USER="Agent_gugugaga"; $env:MC_AGENT_PASSWORD="<config.json 里的口令>"
  python probe_api.py [--base http://127.0.0.1:18900/api/v1] [--lock-test]
结果同时写入脚本同目录的 probe_result.json(不含 token)。
"""
import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

R = {}


def http(base, method, path, token=None, body=None, timeout=20):
    headers = {"Authorization": "Bearer " + token} if token else {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        code, raw = e.code, e.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = {"_raw": raw[:200].decode("utf-8", "replace")}
    return code, payload, round((time.time() - t0) * 1000)


def brief(code, p, ms):
    err = p.get("error") or {}
    return {"http": code, "ok": p.get("ok"), "error_code": err.get("code"), "rtt_ms": ms}


def step_login(base, user, pw):
    code, p, ms = http(base, "POST", "/auth/login", body={"username": user, "password": pw})
    R["login"] = {**brief(code, p, ms), "bot_username": p.get("bot_username"), "error": p.get("error")}
    print("[login]", R["login"])
    return p.get("token")


def step_ready(base, tok, wait_s=60):
    t0 = time.time()
    st = None
    while time.time() - t0 < wait_s:
        code, p, ms = http(base, "GET", "/status", tok)
        st = p.get("result") or {}
        if st.get("ready"):
            break
        time.sleep(2)
    R["ready_after_s"] = round(time.time() - t0, 1)
    R["status_keys"] = sorted(st.keys()) if st else None
    R["status_has_velocity"] = any("vel" in k.lower() for k in (st or {}))
    R["status_sample"] = {k: st.get(k) for k in ("username", "pos", "dimension", "gamemode", "health", "food", "ready", "alive") if k in (st or {})}
    print("[status]", json.dumps(R["status_sample"], ensure_ascii=False))
    print("[status keys]", R["status_keys"], "velocity field:", R["status_has_velocity"])
    return bool(st and st.get("ready"))


def step_paths(base, tok):
    """BotSleep 用的旧路径 vs API.md 的正确路径。"""
    old = [("GET", "/blocks", None), ("GET", "/entities", None),
           ("POST", "/look_at", {"yaw": 0, "pitch": 0})]
    new = [("POST", "/find_blocks", {"radius": 16}),
           ("POST", "/get_entities", {"radius": 32}),
           ("POST", "/look", {"yaw": 0, "pitch": 0})]
    out = {"old": {}, "new": {}}
    for tag, group in (("old", old), ("new", new)):
        for m, path, body in group:
            code, p, ms = http(base, m, path, tok, body)
            info = brief(code, p, ms)
            res = p.get("result")
            if isinstance(res, dict):
                info["result_keys"] = sorted(res.keys())
            out[tag][path] = info
            print("[%s] %s %s -> %s" % (tag, m, path, info))
    R["paths"] = out


def step_observe(base, tok):
    code, p, ms = http(base, "GET", "/events?since=0&limit=500", tok)
    res = p.get("result") or {}
    evs = res.get("events") or []
    R["events"] = {**brief(code, p, ms), "result_keys": sorted(res.keys()),
                   "count": len(evs), "last_seq": res.get("last_seq"),
                   "first_seq": evs[0].get("seq") if evs else None,
                   "types": sorted({e.get("type") for e in evs})}
    print("[events]", R["events"])

    code, p, ms = http(base, "POST", "/advancements", tok, {"status": "completed", "include_recipes": False})
    res = p.get("result") or {}
    R["advancements"] = {**brief(code, p, ms), "result_keys": sorted(res.keys()),
                         "ready": res.get("ready"), "summary": res.get("summary"),
                         "completed_ids": [a.get("id") for a in (res.get("advancements") or [])][:20],
                         "error": p.get("error")}
    print("[advancements]", json.dumps(R["advancements"], ensure_ascii=False))

    code, p, ms = http(base, "POST", "/stop", tok)
    R["stop_idle"] = {**brief(code, p, ms), "result": p.get("result")}
    print("[stop (idle)]", R["stop_idle"])


def step_lock(base, tok):
    """/control 跳 3 秒:验证 busy、观察接口并发、/stop 打断。会让 bot 原地跳几下。"""
    box = {}

    def long_call():
        box["first"] = http(base, "POST", "/control", tok, {"jump": True, "ms": 3000, "stop_on": []}, timeout=30)

    th = threading.Thread(target=long_call)
    t0 = time.time()
    th.start()
    time.sleep(0.6)
    c2 = http(base, "POST", "/control", tok, {"jump": True, "ms": 100})
    c3 = http(base, "GET", "/status", tok)
    c4 = http(base, "POST", "/stop", tok)
    th.join()
    first = box.get("first")
    R["lock_test"] = {
        "second_action": brief(*c2),
        "status_during_action": brief(*c3),
        "stop_during_action": brief(*c4),
        "first_action": brief(*first) if first else None,
        "first_action_total_s": round(time.time() - t0, 2),
        "first_action_error": (first[1].get("error") if first else None),
    }
    print("[lock-test]", json.dumps(R["lock_test"], ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18900/api/v1")
    ap.add_argument("--lock-test", action="store_true")
    a = ap.parse_args()
    user = os.environ.get("MC_AGENT_USER", "Agent_gugugaga")
    pw = os.environ.get("MC_AGENT_PASSWORD")
    if not pw:
        sys.exit("请先设置环境变量 MC_AGENT_PASSWORD")
    tok = None
    try:
        tok = step_login(a.base, user, pw)
        if not tok:
            return
        if not step_ready(a.base, tok):
            print("bot 60 秒内未 ready,后续结果可能是 wrong_server")
        step_paths(a.base, tok)
        step_observe(a.base, tok)
        if a.lock_test:
            step_lock(a.base, tok)
    finally:
        if tok:
            http(a.base, "POST", "/auth/logout", tok)
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_result.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(R, f, ensure_ascii=False, indent=2)
        print("已写入", out)


if __name__ == "__main__":
    main()
