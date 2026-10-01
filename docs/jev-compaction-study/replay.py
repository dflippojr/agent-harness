"""#172 replay: compare elide(), #155 masking and a Jev-style relevance selector on recorded tool histories.
Run from the repo root: python docs/jev-compaction-study/replay.py [--no-qwen]. Aggregate output only."""
import copy, json, sys, time, math, urllib.request
sys.path.insert(0, "docs/jev-compaction-study"); sys.path.insert(0, ".")
import replay_lib as L
from harness import compaction as C

QWEN = "http://127.0.0.1:8090/v1/chat/completions"
THRESH, MIN_CHARS, MAX_STATE, HORIZON = 0.2, 200, 4000, 10
USE_QWEN = "--no-qwen" not in sys.argv
STUB = "[Tool result removed: judged no longer relevant]"
lat, failures = [], []


def score(task, call):
    q = (f"Task: {task[:2000]}\n\nTool call: {call['name']} {json.dumps(call['args'])[:500]}\n"
         f"Result: {call['out'][:MAX_STATE]}\n\nIs this tool exchange still necessary to complete the task? Answer YES or NO.")
    body = {"model": "qwen3.6-35b-a3b", "messages": [{"role": "user", "content": q}], "max_tokens": 1, "temperature": 0,
            "logprobs": True, "top_logprobs": 10, "chat_template_kwargs": {"enable_thinking": False}}
    t0 = time.time()
    req = urllib.request.Request(QWEN, json.dumps(body).encode(), {"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            r = json.load(urllib.request.urlopen(req, timeout=120))
            break
        except OSError:
            failures.append(1)
            time.sleep(10)
    else:
        return None  # unavailable: the exchange is kept (fail-open)
    lat.append(time.time() - t0)
    yes = no = 0.0
    for t in r["choices"][0]["logprobs"]["content"][0]["top_logprobs"]:
        w = t["token"].strip().upper()
        if w == "YES": yes += math.exp(t["logprob"])
        elif w == "NO": no += math.exp(t["logprob"])
    return yes / (yes + no) if yes + no else None


def build(s, t):
    """Messages visible when the model is about to answer after turn t's results (assistant t is the newest assistant,
    so turn t's results are the untouched newest results)."""
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
    for i in range(t + 1):
        calls = s["turns"][i]["calls"]
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": c["id"], "function": {"name": c["name"], "arguments": json.dumps(c["args"])}} for c in calls]})
        if True:
            for c in calls:
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": c["out"]})
    return msgs


def tool_idx(msgs):
    return {m["tool_call_id"]: i for i, m in enumerate(msgs) if m["role"] == "tool"}


def policies(s, t, msgs, cache):
    """-> {policy: {call_id: 'drop'|'keep'}} plus context chars per policy."""
    calls = {c["id"]: c for _, c in L.flat(s)}
    out = {}
    el, _ = C.elide(msgs)
    out["elide"] = el
    outcomes = {cid: {"ok": c["ok"], "name": c["name"]} for cid, c in calls.items()}
    mk, _, _ = C.mask_used_results(msgs, outcomes, 2000)
    out["mask"] = mk
    jv = copy.deepcopy(msgs)
    if USE_QWEN:
        cut = C.last_turn_start(jv)
        for i, m in enumerate(jv):
            if m["role"] != "tool" or i >= cut:
                continue
            c = calls[m["tool_call_id"]]
            if c["out_chars"] < MIN_CHARS:
                continue
            key = (c["id"], t)
            if key not in cache:
                cache[key] = score(s["turns"][t]["task"] or "", c)
            if cache[key] is not None and cache[key] < THRESH:
                m["content"] = STUB
        out["jev"] = jv
    return out


def dropped(orig, new):
    """tool call ids whose result content was replaced or shortened."""
    return {o["tool_call_id"] for o, n in zip(orig, new) if o["role"] == "tool" and n["content"] != o["content"]}


def run(sid, s, stats):
    cache, ntur = {}, len(s["turns"])
    calls = {c["id"]: (t, c) for t, c in L.flat(s)}
    keyof = lambda c: c["sig"]
    key2 = lambda c: c["name"] + ":" + next((str(v) for v in c["args"].values() if isinstance(v, str)), "")
    prev = {}
    for t in range(ntur):
        msgs = build(s, t)
        full = sum(C.message_chars(m) for m in msgs)
        pol = policies(s, t, msgs, cache)
        for p, new in pol.items():
            st = stats.setdefault(p, dict(full=0, kept=0, dropped=set(), needed=set(), ambig=set(), err_dropped=set(),
                                          reuse_num=0, reuse_den=0, items=set()))
            st["full"] += full
            st["kept"] += sum(C.message_chars(m) for m in new)
            d = dropped(msgs, new)
            for cid in d:
                tt, c = calls[cid]
                st["dropped"].add((sid, cid))
                if c["ok"] is False:
                    st["err_dropped"].add((sid, cid))
                later = [c2 for t2, c2 in L.flat(s) if t < t2 <= tt + HORIZON]
                if any(keyof(c2) == keyof(c) for c2 in later):
                    st["needed"].add((sid, cid))
                elif any(key2(c2) == key2(c) for c2 in later):
                    st["ambig"].add((sid, cid))
            # cache prefix reuse: chars before first message that differs from the previous turn's context
            if p in prev:
                old = prev[p]
                n = 0
                for a, b in zip(old, new):
                    if a != b: break
                    n += C.message_chars(a)
                st["reuse_num"] += n
                st["reuse_den"] += sum(C.message_chars(m) for m in old)
            prev[p] = new if p not in prev else prev[p]
        for p, new in pol.items():
            prev[p] = new


def main():
    c = L.connect()
    sess = L.load_sessions(c)
    sel, nstrict = L.select(sess)
    expl = sorted([(sid, v) for sid, v in sess.items() if any(L.criteria(v)) and len(v["turns"]) > 1],
                  key=lambda x: -x[1]["last_ts"])
    print("sessions", len(sess), "strict matches", nstrict, "exploratory", len(expl))
    stats = {}
    for i, (sid, s) in enumerate(expl):
        trunc = sum(c["trunc"] for _, c in L.flat(s))
        print(f"S{i + 1} turns={len(s['turns'])} calls={len(L.flat(s))} criteria={L.criteria(s)} truncated_results={trunc}", flush=True)
        run(sid, s, stats)
    for p, st in stats.items():
        n = len(st["dropped"])
        print(p, "ctx_chars_full", st["full"], "kept", st["kept"], "saved_pct", round(100 * (1 - st["kept"] / max(st["full"], 1)), 1),
              "dropped_items", n, "needed", len(st["needed"]), "ambiguous", len(st["ambig"]),
              "error_results_dropped", len(st["err_dropped"]),
              "prefix_reuse_pct", round(100 * st["reuse_num"] / max(st["reuse_den"], 1), 1))
    if lat:
        print("qwen calls", len(lat), "mean_s", round(sum(lat) / len(lat), 3), "max_s", round(max(lat), 3), "total_s", round(sum(lat)))
    print("qwen request failures", len(failures))
    print("session ids (sha8):", [__import__("hashlib").sha256(sid.encode()).hexdigest()[:8] for sid, _ in expl])


main()
