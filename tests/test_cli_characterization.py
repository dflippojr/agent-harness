"""Issue #236: characterization tests for harness.cli watch/main, written before their complexity refactor."""

from __future__ import annotations

import json
import subprocess
import types

import httpx
import pytest

from harness import cli


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home" / ".agent-harness"
    monkeypatch.setattr(cli, "HARNESS_HOME", home)
    monkeypatch.setattr(cli, "DEFAULT_CONFIG", home / "client" / "config.json")
    monkeypatch.setattr(cli, "DEFAULT_RUNNER_CONFIG", home / "runner" / "config.json")
    monkeypatch.delenv("HARNESS_URL", raising=False)
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    return home


class Calls(list):
    """Records cli.api calls; `responses` maps (method, path) to a value or a callable."""

    def __init__(self):
        super().__init__()
        self.responses = {}


@pytest.fixture
def calls(monkeypatch):
    seen = Calls()

    def fake(method, path, retries=30, **kwargs):
        seen.append((method, path, kwargs.get("json")))
        value = seen.responses[(method, path)]
        return value(kwargs) if callable(value) else value

    monkeypatch.setattr(cli, "api", fake)
    return seen


def run_main(monkeypatch, *argv):
    monkeypatch.setattr(cli.sys, "argv", ["harness", *argv])
    return cli.main()


def write_runner_config(home, **data):
    (home / "runner").mkdir(parents=True, exist_ok=True)
    (home / "runner" / "config.json").write_text(json.dumps({"name": "mac", **data}))


# ---- main: dispatch ----------------------------------------------------------------------------------------

def test_main_requires_a_command(home, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exit_:
        run_main(monkeypatch)
    assert exit_.value.code == 2
    assert "required" in capsys.readouterr().err


def test_main_loads_config_server_and_token(home, monkeypatch, calls):
    (home / "client").mkdir(parents=True)
    (home / "client" / "config.json").write_text(json.dumps({"server": "https://x.example/", "token": " tok "}))
    calls.responses[("GET", "/queue")] = []
    assert run_main(monkeypatch, "queue") == 0
    assert (cli.BASE, cli.TOKEN) == ("https://x.example", "tok")


def test_main_invalid_config_exits_with_message(home, monkeypatch):
    (home / "client").mkdir(parents=True)
    (home / "client" / "config.json").write_text("{not json")
    with pytest.raises(SystemExit) as exit_:
        run_main(monkeypatch, "queue")
    assert "invalid client config" in str(exit_.value.code)


def test_main_env_overrides_config(home, monkeypatch, calls):
    monkeypatch.setenv("HARNESS_URL", "http://env.example/")
    monkeypatch.setenv("HARNESS_TOKEN", "envtok")
    calls.responses[("GET", "/queue")] = []
    run_main(monkeypatch, "queue")
    assert (cli.BASE, cli.TOKEN) == ("http://env.example", "envtok")


def test_main_list_show_transcript_cancel_queue(home, monkeypatch, calls, capsys):
    calls.responses[("GET", "/sessions")] = [
        {"id": "s1", "created_at": 0, "status": "done", "project": "scratch", "title": "hello"}]
    calls.responses[("GET", "/sessions/s1")] = {"id": "s1"}
    calls.responses[("GET", "/sessions/s1/transcript")] = "# transcript"
    calls.responses[("POST", "/sessions/s1/cancel")] = {"status": "cancelled"}
    calls.responses[("GET", "/queue")] = [{"position": 1, "session_id": "s9"}]
    assert run_main(monkeypatch, "list") == 0
    out = capsys.readouterr().out
    assert out.startswith("s1  ") and "done" in out and "scratch" in out and out.rstrip().endswith("hello")
    assert run_main(monkeypatch, "show", "s1") == 0
    assert json.loads(capsys.readouterr().out) == {"id": "s1"}
    assert run_main(monkeypatch, "transcript", "s1") == 0
    assert capsys.readouterr().out == "# transcript\n"
    assert run_main(monkeypatch, "cancel", "s1") == 0
    assert capsys.readouterr().out == "cancelled\n"
    assert run_main(monkeypatch, "queue") == 0
    assert capsys.readouterr().out == "1  s9\n"


def test_main_approve_and_deny(home, monkeypatch, calls, capsys):
    calls.responses[("POST", "/sessions/s1/approvals/pending")] = {"id": "a1", "status": "approved"}
    calls.responses[("POST", "/sessions/s1/approvals/a2")] = {"id": "a2", "status": "denied"}
    assert run_main(monkeypatch, "approve", "s1") == 0
    assert run_main(monkeypatch, "deny", "s1", "a2", "--note", "no") == 0
    assert capsys.readouterr().out == "a1 approved\na2 denied\n"
    assert calls == [("POST", "/sessions/s1/approvals/pending", {"decision": "approve", "note": ""}),
                     ("POST", "/sessions/s1/approvals/a2", {"decision": "deny", "note": "no"})]


def test_main_new_detached_and_watching(home, monkeypatch, calls, capsys):
    calls.responses[("POST", "/sessions")] = {"id": "s1", "project": "p", "model": "m"}
    watched = []
    monkeypatch.setattr(cli, "watch", lambda sid, args, after=0: watched.append((sid, args, after)) or 7)
    assert run_main(monkeypatch, "new", "do it", "--project", "p", "--detach") == 0
    assert capsys.readouterr().out == "session s1 (p, m)\n"
    assert watched == []
    assert calls[0][2] == {"prompt": "do it", "project": "p", "backend": "local", "model": None, "title": None}
    assert run_main(monkeypatch, "new", "do it", "--reasoning", "--full") == 7
    sid, args, after = watched[0]
    assert (sid, after, args.reasoning, args.full, args.no_prompt) == ("s1", 0, True, True, False)


def test_main_watch_resolves_session_id(home, monkeypatch, calls):
    calls.responses[("GET", "/sessions/abc")] = {"id": "abc-full"}
    monkeypatch.setattr(cli, "watch", lambda sid, args, after=0: (sid, after))
    assert run_main(monkeypatch, "watch", "abc") == ("abc-full", 0)


def test_main_send_without_and_with_watch(home, monkeypatch, calls, capsys):
    calls.responses[("GET", "/sessions/s1")] = {"id": "s1", "last_event_seq": 41}
    calls.responses[("POST", "/sessions/s1/messages")] = {"id": "s1", "status": "running"}
    seen = []
    monkeypatch.setattr(cli, "watch", lambda sid, args, after=0: seen.append((sid, args, after)) or 3)
    assert run_main(monkeypatch, "send", "s1", "hi") == 0
    assert capsys.readouterr().out == "sent; session is running\n"
    assert seen == []
    assert run_main(monkeypatch, "send", "s1", "hi", "--watch") == 3
    sid, args, after = seen[0]
    assert (sid, after, args.reasoning, args.full, args.no_prompt) == ("s1", 41, False, False, False)


def test_main_pair_prints_summary_without_secrets(home, monkeypatch, capsys):
    class Response:
        status_code = 201

        @staticmethod
        def json():
            return {"server": "https://t.example", "owner_token": "ho-secret",
                    "runner": {"name": "mac", "token": "runner-secret"}}

    monkeypatch.setattr(cli.httpx, "post", lambda *a, **k: Response())
    assert run_main(monkeypatch, "pair", "https://t.example", "hrp-1") == 0
    out = capsys.readouterr()
    assert out.out == "paired mac with https://t.example\n"
    assert "secret" not in out.out + out.err


def test_pair_native_errors(home, monkeypatch):
    def fail(*a, **k):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(cli.httpx, "post", fail)
    with pytest.raises(SystemExit) as exit_:
        cli.pair_native("https://t.example/", "c", home / "client" / "c.json", home / "runner" / "r.json")
    assert "daemon not reachable at https://t.example" in str(exit_.value.code)

    class Bad:
        status_code = 400
        text = "plain"

        @staticmethod
        def json():
            raise ValueError

    monkeypatch.setattr(cli.httpx, "post", lambda *a, **k: Bad())
    with pytest.raises(SystemExit) as exit_:
        cli.pair_native("https://t.example", "c", home / "client" / "c.json", home / "runner" / "r.json")
    assert exit_.value.code == "pairing failed (400): plain"
    assert not (home / "client").exists()


# ---- main: version -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("remote,state", [
    ({"protocols": {"admin": {"min": 1, "max": 9}}}, "compatible"),
    ({"protocols": {"admin": {"min": 99, "max": 100}}}, "client update required"),
    ({"protocols": {"admin": {"min": 0, "max": 0}}}, "Server update required"),
    ({}, "compatible"),
])
def test_main_version_compatibility(home, monkeypatch, capsys, remote, state):
    monkeypatch.setattr(cli, "server_version", lambda: {"release": "r1", "build_id": "b1", **remote})
    assert run_main(monkeypatch, "version") == 0
    out = capsys.readouterr().out
    assert out.startswith(f"Agent Harness CLI {cli.MAC_CLIENT_VERSION} (admin protocol {cli.CLIENT_PROTOCOLS['cli']})")
    assert "Agent Harness Server r1 build b1" in out
    assert f"compatibility: {state} (Server supports admin protocol" in out


def test_main_version_unreachable_and_unknown_fields(home, monkeypatch, capsys):
    def down():
        raise RuntimeError("daemon not reachable at x")

    monkeypatch.setattr(cli, "server_version", down)
    assert run_main(monkeypatch, "version") == 1
    assert capsys.readouterr().out.splitlines()[-1] == "daemon not reachable at x"
    monkeypatch.setattr(cli, "server_version", lambda: {})
    run_main(monkeypatch, "version")
    out = capsys.readouterr().out
    assert "Server unknown build unknown" in out and "protocol ?–?" in out


def test_server_version_wraps_http_errors(monkeypatch):
    monkeypatch.setattr(cli, "BASE", "http://d")
    monkeypatch.setattr(cli.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("x")))
    with pytest.raises(RuntimeError, match="daemon not reachable at http://d"):
        cli.server_version()


# ---- main: update / projects / runner ----------------------------------------------------------------------

def test_main_update(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "apply_update", lambda base: {"version": "9.9"})
    assert run_main(monkeypatch, "update") == 0
    assert capsys.readouterr().out == "updated Agent Harness for Mac to 9.9\n"

    def bad(base):
        raise RuntimeError("no package")

    monkeypatch.setattr(cli, "apply_update", bad)
    with pytest.raises(SystemExit) as exit_:
        run_main(monkeypatch, "update")
    assert exit_.value.code == "no package"


def test_main_projects_add_success_and_failures(home, monkeypatch, capsys, tmp_path):
    write_runner_config(home)
    project = tmp_path / "proj"
    project.mkdir()
    kicks = []
    monkeypatch.setattr(cli, "launchctl", lambda *a, **k: kicks.append((a, k)))
    assert run_main(monkeypatch, "projects", "add", str(project)) == 0
    assert capsys.readouterr().out == f"allowed project root {project.resolve()}\n"
    assert kicks == [(("kickstart", "-k"), {"check": True})]
    cfg = json.loads((home / "runner" / "config.json").read_text())
    assert cfg["repo_roots"] == [str(project.resolve())]

    with pytest.raises(SystemExit) as exit_:
        run_main(monkeypatch, "projects", "add", str(tmp_path / "missing"))
    assert "could not add project: project directory does not exist" in exit_.value.code

    def fail(*a, **k):
        raise subprocess.CalledProcessError(1, "launchctl")

    monkeypatch.setattr(cli, "launchctl", fail)
    with pytest.raises(SystemExit) as exit_:
        run_main(monkeypatch, "projects", "add", str(project))
    assert exit_.value.code.startswith("could not add project:")


def test_add_project_root_needs_pairing(home, tmp_path):
    with pytest.raises(ValueError, match="pair this Mac"):
        cli.add_project_root(str(tmp_path), home / "runner" / "config.json")


def test_main_runner_restart(home, monkeypatch, capsys):
    kicks = []
    monkeypatch.setattr(cli, "launchctl", lambda *a, **k: kicks.append((a, k)))
    assert run_main(monkeypatch, "runner", "restart") == 0
    assert capsys.readouterr().out == "runner restarted\n"
    assert kicks == [(("kickstart", "-k"), {"check": True})]


@pytest.mark.parametrize("returncode,loaded", [(0, "loaded"), (113, "not loaded")])
def test_main_runner_status(home, monkeypatch, calls, capsys, returncode, loaded):
    write_runner_config(home)
    monkeypatch.setattr(cli, "launchctl", lambda *a, **k: types.SimpleNamespace(returncode=returncode))
    calls.responses[("GET", "/runners")] = [{"name": "other"}, {"name": "mac", "online": True}]
    assert run_main(monkeypatch, "runner", "status") == 0
    out = capsys.readouterr().out
    assert out.startswith(f"launchd: {loaded}\ndaemon: {{")
    assert '"online": true' in out
    calls.responses[("GET", "/runners")] = []
    run_main(monkeypatch, "runner", "status")
    assert "daemon: runner not configured on daemon" in capsys.readouterr().out


def test_launchctl_builds_fixed_argument_list(monkeypatch):
    seen = []
    monkeypatch.setattr(cli.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(cli.subprocess, "run", lambda args, **kw: seen.append((args, kw)))
    cli.launchctl("print")
    assert seen == [(["launchctl", "print", "gui/501/dev.agent-harness.runner"], {"check": False, "text": True})]


def test_main_runner_logs_missing_file_and_bad_lines(home, monkeypatch, capsys):
    assert run_main(monkeypatch, "runner", "logs") == 1
    assert "could not open runner log" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exit_:
        run_main(monkeypatch, "runner", "logs", "--lines", "abc")
    assert exit_.value.code == 2


# ---- api / helpers -----------------------------------------------------------------------------------------

class Resp:
    def __init__(self, status=200, payload=None, text="", ctype="application/json"):
        self.status_code, self._payload, self.text = status, payload, text
        self.headers = {"content-type": ctype}

    def json(self):
        if self._payload is None:
            raise ValueError
        return self._payload


def api_error(monkeypatch, response):
    monkeypatch.setattr(cli.httpx, "request", lambda *a, **k: response)
    with pytest.raises(SystemExit) as exit_:
        cli.api("GET", "/x")
    return exit_.value.code


def test_api_retries_then_returns_json(monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "BASE", "http://d")
    monkeypatch.setattr(cli, "TOKEN", "")
    attempts = []

    def flaky(method, url, **kw):
        attempts.append(url)
        if len(attempts) < 3:
            raise httpx.ConnectError("x")
        return Resp(payload={"ok": 1})

    monkeypatch.setattr(cli.httpx, "request", flaky)
    assert cli.api("GET", "/x") == {"ok": 1}
    assert attempts[0] == "http://d/api/admin/v1/x"


def test_api_gives_up_after_retries_and_reports_errors(monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "BASE", "http://d")
    monkeypatch.setattr(cli.httpx, "request", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("x")))
    with pytest.raises(SystemExit, match="daemon not reachable at http://d"):
        cli.api("GET", "/x", retries=1)
    monkeypatch.setattr(cli.httpx, "request", lambda *a, **k: Resp(ctype="text/plain", text="hello"))
    assert cli.api("GET", "/x") == "hello"
    assert api_error(monkeypatch, Resp(500, {"detail": "bad"})) == "error 500: bad"
    assert api_error(monkeypatch, Resp(500, text="raw")) == "error 500: raw"
    old = Resp(426, {"detail": "old", "error": {"code": "client_update_required"}})
    assert api_error(monkeypatch, old) == "error 426: old; run `harness update`"


def test_indent_truncates():
    assert cli.indent("a\nb\nc", None) == "    a\n    b\n    c"
    assert cli.indent("a\nb\nc", 2) == "    a\n    b\n    ... (1 more lines)"
    assert cli.indent("", 5) == "    "


class FakeStream:
    def __init__(self, status, lines):
        self.status_code, self._lines = status, lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self):
        return iter(self._lines)

    def read(self):
        return b"nope"


def test_iter_sse_parses_events_and_reports_drops(monkeypatch, capsys):
    lines = ["data: " + json.dumps({"type": "a", "seq": 1, "data": {}}), "", ": comment",
             'data: {"type": "b",', 'data: "seq": 2, "data": {}}', ""]
    seen = {}

    def stream(method, url, params, **kw):
        seen["params"] = params
        return FakeStream(200, lines)

    monkeypatch.setattr(cli.httpx, "stream", stream)
    assert [e["type"] for e in cli.iter_sse("s1", 5)] == ["a", "b"]
    assert seen["params"] == {"after": 5}

    def drop(*a, **k):
        raise httpx.ReadError("x")

    monkeypatch.setattr(cli.httpx, "stream", drop)
    assert list(cli.iter_sse("s1", 0)) == []
    assert "connection lost (ReadError); reconnecting" in capsys.readouterr().out
    monkeypatch.setattr(cli.httpx, "stream", lambda *a, **k: FakeStream(500, []))
    with pytest.raises(SystemExit) as exit_:
        list(cli.iter_sse("s1", 0))
    assert exit_.value.code == "error 500: nope"


# ---- watch -------------------------------------------------------------------------------------------------

def ev(t, seq=None, **data):
    return {"type": t, "seq": seq, "data": data}


def args_ns(**kw):
    return types.SimpleNamespace(**{"reasoning": False, "full": False, "no_prompt": True, **kw})


class Connections(list):
    """`after` of each connection; `script` holds the events each successive connection yields."""

    def __init__(self):
        super().__init__()
        self.script = []


@pytest.fixture
def stream(monkeypatch):
    connections = Connections()

    def fake(sid, after):
        connections.append(after)
        yield from connections.script.pop(0)

    monkeypatch.setattr(cli, "iter_sse", fake)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    return connections


def plain(text):
    for code in (cli.DIM, cli.BOLD, cli.YELLOW, cli.GREEN, cli.RED, cli.CYAN, cli.RESET):
        text = text.replace(code, "")
    return text


def test_watch_streams_deltas_then_assistant_and_done(calls, stream, capsys):
    calls.responses[("GET", "/sessions/s1")] = {"status": "done"}
    assistant = dict(content=" hi ", tool_calls=[{"function": {"name": "run", "arguments": "{}"}}],
                     prompt_tokens=10, completion_tokens=2, gen_tps=5)
    stream.script.append([
        ev("delta", 1, kind="reasoning", text="think"),   # hidden without --reasoning
        ev("delta", 2, kind="content", text="hi"),
        ev("assistant", 3, **assistant),
        ev("status", 4, status="done", answer="hi", stop_reason=None)])
    assert cli.watch("s1", args_ns()) == 0
    assert plain(capsys.readouterr().out) == (
        "assistant: hi\n  → run {}\n  [10 prompt / 2 completion tokens, 5 tok/s]\nstatus: done\n")
    assert stream == [0]


def test_watch_reasoning_visible_and_resets_before_content(calls, stream, capsys):
    calls.responses[("GET", "/sessions/s1")] = {"status": "failed"}
    stream.script.append([
        ev("delta", 1, kind="reasoning", text="think"),
        ev("delta", 2, kind="reasoning", text="more"),
        ev("delta", 3, kind="content", text="ans"),
        ev("assistant", 4, content="ans", tool_calls=[], prompt_tokens=1, completion_tokens=1),
        ev("status", 5, status="failed", answer="other", stop_reason="boom")])
    assert cli.watch("s1", args_ns(reasoning=True)) == 1
    raw = capsys.readouterr().out
    assert raw.startswith(f"{cli.CYAN}assistant:{cli.RESET} {cli.DIM}think")
    assert f"thinkmore{cli.RESET}\nans{cli.RESET}\n" in raw
    assert "[1 prompt / 1 completion tokens]" in raw
    assert "status: failed (boom)" in plain(raw)
    assert "answer:\nother" in plain(raw)   # differs from the streamed content


def test_watch_answer_not_repeated_when_equal(calls, stream, capsys):
    calls.responses[("GET", "/sessions/s1")] = {"status": "cancelled"}
    stream.script.append([
        ev("assistant", 1, content="same", tool_calls=[], prompt_tokens=1, completion_tokens=1),
        ev("status", 2, status="cancelled", answer=" same ")])
    assert cli.watch("s1", args_ns()) == 1
    assert "answer:" not in plain(capsys.readouterr().out)


def test_watch_ignores_replayed_terminal_status_and_reconnects(calls, stream, capsys):
    states = iter(["running", "done"])   # the session is only really finished on the second check
    calls.responses[("GET", "/sessions/s1")] = lambda kw: {"status": next(states)}
    stream.script.append([ev("status", 7, status="done"), ev("status", 8, status="running")])
    stream.script.append([ev("status", 9, status="done")])
    assert cli.watch("s1", args_ns(), after=3) == 0
    assert stream == [3, 8]   # the dropped stream reconnects from the last seq
    assert plain(capsys.readouterr().out).count("status: done") == 2


def test_watch_queue_compaction_and_print_only_events(calls, stream, capsys):
    calls.responses[("GET", "/sessions/s1")] = {"status": "done"}
    stream.script.append([
        ev("queue", None, position=2), ev("queue", None, position=2), ev("queue", None, position=0),
        ev("queue", None, position=1),
        ev("compacting", None, messages=12),
        ev("user_message", 1, content="hello"),
        ev("tool_call", 2, decision="allow", reason="r"),
        ev("tool_call", 3, decision="deny", reason="nope"),
        ev("tool_result", 4, ok=True, name="run", seconds=1.5, output="l1\nl2"),
        ev("tool_result", 5, ok=False, name="bad", seconds=2, output="\n".join(f"L{i}" for i in range(20))),
        ev("compaction", 6, tier="t1", tokens_before=100, tokens_after=50),
        ev("error", 7, message="oops"), ev("llm_retry", 8, error="again"),
        ev("model_waking", 9, expected_seconds=30), ev("model_ready", 10, seconds=12),
        ev("resumed", 11), ev("unknown", 12),
        ev("status", 13, status="done")])
    assert cli.watch("s1", args_ns()) == 0
    out = plain(capsys.readouterr().out)
    assert out.count("queued: position") == 2 and "queued: position 2" in out and "queued: position 1" in out
    assert "compacting 12 messages..." in out
    assert "user: hello" in out
    assert "policy deny: nope" in out and "policy allow" not in out
    assert "← run (1.5s)\n    l1\n    l2" in out
    assert "← bad (2s)" in out and "    L11\n    ... (8 more lines)" in out
    assert "context compacted (t1): ~100 → ~50 tokens" in out
    assert "error: oops" in out and "llm_retry: again" in out
    assert "model is asleep; waking it (about 30 s)..." in out
    assert "model ready after 12 s" in out and "daemon restarted; session resumed" in out


def test_watch_full_output_not_truncated(calls, stream, capsys):
    calls.responses[("GET", "/sessions/s1")] = {"status": "done"}
    stream.script.append([ev("tool_result", 1, ok=True, name="n", seconds=0, output="\n".join(map(str, range(30)))),
                          ev("status", 2, status="done")])
    cli.watch("s1", args_ns(full=True))
    out = capsys.readouterr().out
    assert "more lines" not in out and "    29" in out


def test_watch_approval_flow_non_interactive(calls, stream, capsys):
    calls.responses[("GET", "/sessions/s1")] = {"status": "done"}
    stream.script.append([
        ev("approval_requested", 1, id="a1", tool="run", reason="risky", args={"x": 1}, detail="why\nnow"),
        ev("approval_decided", 2, id="a1", status="approved"),
        ev("approval_requested", 3, id="a2", tool="run", reason="r", args={}),
        ev("status", 4, status="done")])
    assert cli.watch("s1", args_ns(no_prompt=True)) == 0
    out = plain(capsys.readouterr().out)
    assert "approval needed [a1]: run — risky" in out and '"x": 1' in out and "    why\n    now" in out
    assert "approval a1 approved" in out
    assert not any(c[1].endswith("/approvals") for c in calls)


@pytest.mark.parametrize("answer,expected,notes", [
    ("y", {"decision": "approve"}, []),
    ("YES", {"decision": "approve"}, []),
    ("n", {"decision": "deny", "note": "because"}, ["because"]),
    ("s", None, []),
    ("", None, []),
])
def test_watch_interactive_approval_prompts_then_reconnects(monkeypatch, calls, stream, capsys,
                                                             answer, expected, notes):
    calls.responses[("GET", "/sessions/s1/approvals")] = [{"id": "other"}, {"id": "a1"}]
    calls.responses[("POST", "/sessions/s1/approvals/a1")] = {}
    calls.responses[("GET", "/sessions/s1")] = {"status": "done"}
    stream.script.append([ev("approval_requested", 5, id="a1", tool="t", reason="r", args={}),
                          ev("delta", 6, kind="content", text="never seen")])
    stream.script.append([ev("status", 6, status="done")])
    monkeypatch.setattr(cli.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    inputs = iter([answer, *notes])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    assert cli.watch("s1", args_ns(no_prompt=False)) == 0
    assert stream == [0, 5]   # left the stream at the request, resumed from its seq
    posts = [c[2] for c in calls if c[0] == "POST"]
    if expected:
        assert posts == [{"decision": expected["decision"], **({"note": expected["note"]} if "note" in expected else {})}]
    else:
        assert posts == []
    out = plain(capsys.readouterr().out)
    assert "never seen" not in out
    if expected is None:
        assert "left pending; approve later with: approve s1 a1" in out


def test_watch_interactive_skips_prompt_when_already_decided(monkeypatch, calls, stream):
    calls.responses[("GET", "/sessions/s1/approvals")] = []
    calls.responses[("GET", "/sessions/s1")] = {"status": "done"}
    stream.script.append([ev("approval_requested", 1, id="a1", tool="t", reason="r", args={}),
                          ev("status", 2, status="done")])
    monkeypatch.setattr(cli.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("must not prompt"))
    assert cli.watch("s1", args_ns(no_prompt=False)) == 0


def test_watch_sleeps_and_reconnects_when_stream_ends(monkeypatch, calls, stream):
    calls.responses[("GET", "/sessions/s1")] = {"status": "done"}
    sleeps = []
    monkeypatch.setattr(cli.time, "sleep", sleeps.append)
    stream.script.append([ev("delta", 4, kind="content", text="x")])
    stream.script.append([ev("status", 5, status="done")])
    assert cli.watch("s1", args_ns()) == 0
    assert sleeps == [2] and stream == [0, 4]
