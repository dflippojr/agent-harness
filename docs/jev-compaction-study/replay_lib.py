"""Read-only helpers for the #172 study: load tool turns from the events table and select the corpus.
Output is aggregate only; no transcript content is printed or stored."""
import hashlib, json, sqlite3

DB = "D:/Agents/harness/harness.sqlite3"
READ_TOOLS = {"read_file", "read_service_config", "list_files", "session_read", "memory_read"}


def connect(path=DB):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def sig(name, args):
    return name + ":" + hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()[:12]


def load_sessions(c):
    """session_id -> {last_ts, turns: [ {assistant_seq, calls: [ {id,name,sig,ok,out,out_chars,trunc} ]} ]}"""
    sess = {}
    for seq, sid, ts, typ, data in c.execute(
            "select seq,session_id,ts,type,data from events where type in ('assistant','tool_call','tool_result','user_message') order by seq"):
        d = json.loads(data)
        s = sess.setdefault(sid, {"last_ts": 0, "turns": [], "calls": {}})
        s["last_ts"] = max(s["last_ts"], ts)
        if typ == "user_message":
            s["task"] = d.get("text") or d.get("content") or json.dumps(d)
        elif typ == "assistant":
            s["turns"].append({"seq": seq, "calls": [], "task": s.get("task", "")})
        elif typ == "tool_call":
            call = {"id": d["id"], "name": d["name"], "sig": sig(d["name"], d.get("args")), "ok": None, "out": "",
                    "out_chars": 0, "trunc": False, "args": d.get("args") or {}}
            s["calls"][d["id"]] = call
            if s["turns"]:
                s["turns"][-1]["calls"].append(call)
        else:
            call = s["calls"].get(d["id"])
            if call:
                call["ok"] = d.get("ok")
                call["out"] = d.get("output") or ""
                call["out_chars"] = d.get("output_chars", len(call["out"]))
                call["trunc"] = call["out_chars"] > len(call["out"])
    return sess


def flat(s):
    return [(t, c) for t, turn in enumerate(s["turns"]) for c in turn["calls"]]


def criteria(s):
    calls = [c for _, c in flat(s)]
    err = any(c["ok"] is False for c in calls)
    seen, repeat_fail = {}, False
    for c in calls:
        if c["ok"] is False and seen.get(c["sig"]):
            repeat_fail = True
        if c["ok"] is False:
            seen[c["sig"]] = seen.get(c["sig"], 0) + 1
    # a failed call is "repeated" when the identical call fails again or is re-issued after failing
    fails = {c["sig"] for c in calls if c["ok"] is False}
    cnt = {}
    for c in calls:
        cnt[c["sig"]] = cnt.get(c["sig"], 0) + 1
    repeat_fail = repeat_fail or any(cnt[x] >= 2 for x in fails)
    reread = any(c["name"] in READ_TOOLS and cnt[c["sig"]] >= 2 for c in calls)
    return err, repeat_fail, reread


def select(sess, n=20):
    ok = [(sid, s) for sid, s in sess.items() if all(criteria(s))]
    ok.sort(key=lambda x: -x[1]["last_ts"])
    return ok[:n], len(ok)
