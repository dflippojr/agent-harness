"""`python -m bakeoff.canary --driver pi` (#942): the Pi driver, built and tested with a fake Pi process.

No Docker, no model and no real Pi: the fake Pi records what it was started with and prints a canned JSON transcript
in Pi 1.5.0's event format (packages/coding-agent/docs/json.md at the pin)."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bakeoff import canary, pi_driver
from bakeoff.run_measures import file_access, native_measures
from harness.config import ModelConfig

ROOT = Path(__file__).resolve().parent.parent
MODEL = ModelConfig(name="qwen3.6-35b-a3b", base_url="http://127.0.0.1:8090", context_tokens=65536, max_tokens=8192,
                    sampling={"temperature": 0.6, "top_k": 20})
PROMPT = "Fix the failing tests in calc.py. Do not edit tests/."


def _usage(inp: int, out: int, cache_read: int = 0, cache_write: int = 0) -> dict:
    return {"input": inp, "output": out, "cacheRead": cache_read, "cacheWrite": cache_write,
            "totalTokens": inp + out + cache_read + cache_write,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}


def _assistant(content: list[dict], usage: dict, stop: str = "toolUse", **extra) -> dict:
    return {"role": "assistant", "content": content, "api": "openai-completions", "provider": "canary",
            "model": MODEL.name, "usage": usage, "stopReason": stop, "timestamp": 1, **extra}


def _turn(content: list[dict], usage: dict, stop: str = "toolUse", **extra) -> list[dict]:
    """One Pi turn: the assistant message, an execution start/end per tool call, then turn_end."""
    msg = _assistant(content, usage, stop, **extra)
    out = [{"type": "turn_start"}, {"type": "message_start", "message": {**msg, "stopReason": "pending"}},
           {"type": "message_update", "usage": usage,
            "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "x"}},
           {"type": "message_end", "message": msg}]
    for block in content:
        if block["type"] == "toolCall":
            out += [{"type": "tool_execution_start", "toolCallId": block["id"], "toolName": block["name"],
                     "args": block["arguments"]},
                    {"type": "tool_execution_end", "toolCallId": block["id"], "toolName": block["name"],
                     "result": {"content": [{"type": "text", "text": "ok"}], "details": {}}, "isError": False,
                     "durationMs": 3}]
    return out + [{"type": "turn_end", "message": msg, "toolResults": []}]


def _call(i: int, name: str, **args) -> dict:
    return {"type": "toolCall", "id": f"call_{i}", "name": name, "arguments": args}


def transcript(answer: str = "Fixed calc.add; all tests pass.") -> list[dict]:
    """Six turns: read calc.py and edit it (read first), show notes.md through `cat` then rewrite it, overwrite
    util.py without reading it, add a new file, run the tests, answer."""
    return [
        {"type": "session", "version": 3, "id": "s-1", "timestamp": "2026-11-07T00:00:00.000Z", "cwd": "/workspace"},
        {"type": "agent_start"},
        {"type": "message_start", "message": {"role": "user", "content": PROMPT, "timestamp": 1}},
        {"type": "message_end", "message": {"role": "user", "content": PROMPT, "timestamp": 1}},
        *_turn([{"type": "thinking", "thinking": "look first"}, _call(1, "read", path="calc.py")],
               _usage(3000, 40)),
        *_turn([_call(2, "edit", path="/workspace/calc.py", edits=[{"oldText": "a - b", "newText": "a + b"}])],
               _usage(200, 60, cache_read=3000)),
        *_turn([_call(3, "bash", command="cat ./notes.md | head -n 5"), _call(4, "write", path="notes.md",
                                                                               content="n")],
               _usage(150, 30, cache_read=3200, cache_write=10)),
        *_turn([_call(5, "write", path="util.py", content="u"), _call(6, "write", path="tests/new_test.py",
                                                                      content="t")],
               _usage(120, 50, cache_read=3400)),
        *_turn([_call(7, "bash", command="python -m pytest -q")], _usage(90, 20, cache_read=3600)),
        *_turn([{"type": "text", "text": answer}], _usage(80, 25, cache_read=3700), stop="stop"),
        {"type": "agent_end", "messages": [], "willRetry": False},
        {"type": "agent_settled", "aborted": False},
    ]


EXISTING = {"calc.py", "notes.md", "util.py", "tests/test_calc.py"}


def _jsonl(records: list[dict]) -> str:
    return "".join(json.dumps(r) + "\n" for r in records)


# result parsing
def test_parsing_a_pi_transcript_yields_every_measure():
    m = pi_driver.parse_transcript("npm warn: noise\n" + _jsonl(transcript()), EXISTING)
    assert m["answer"] == "Fixed calc.add; all tests pass."
    assert m["turns"] == 6
    # Pi splits llama-server's prompt_tokens into input + cacheRead + cacheWrite; the record adds them back
    assert m["prompt_tokens"] == (3000 + 200 + 150 + 120 + 90 + 80) + (3000 + 3200 + 3400 + 3600 + 3700) + 10
    assert m["completion_tokens"] == 40 + 60 + 30 + 50 + 20 + 25
    assert m["cached_prompt_tokens"] == 3000 + 3200 + 3400 + 3600 + 3700
    assert m["tool_calls"] == {"bash": 2, "edit": 1, "read": 1, "write": 3}
    assert m["read_before_edit"] is False and m["edited_unread"] == ["util.py"]
    assert m["settled"] is True and m["error"] == "" and m["stop_reason"] == "stop"


def test_tool_calls_come_from_the_messages_when_the_stream_has_no_execution_events():
    records = [r for r in transcript() if not r["type"].startswith("tool_execution")]
    m = pi_driver.parse_transcript(_jsonl(records), EXISTING)
    assert m["tool_calls"] == {"bash": 2, "edit": 1, "read": 1, "write": 3}
    assert m["edited_unread"] == ["util.py"]


@pytest.mark.parametrize("command", ["printf fixed > calc.py", "echo x >> ./calc.py", "cat patch.txt | tee calc.py",
                                     "sed -i 's/a - b/a + b/' calc.py", "perl -pi -e 's/-/+/' calc.py",
                                     "cp /tmp/new.py calc.py", "python gen.py && mv out.py /workspace/calc.py",
                                     "truncate -s 0 calc.py"])
def test_a_shell_write_to_an_unread_task_file_is_an_edit_without_reading(command):
    m = pi_driver.parse_transcript(_jsonl(_turn([_call(1, "bash", command=command)], _usage(10, 1))), EXISTING)
    assert m["read_before_edit"] is False and m["edited_unread"] == ["calc.py"]


def test_a_newline_ends_a_command_like_a_semicolon():
    m = pi_driver.parse_transcript(_jsonl(_turn([_call(1, "bash", command="cat calc.py\nprintf fixed > calc.py")],
                                                _usage(10, 1))), EXISTING)
    assert m["read_before_edit"] is True and m["edited_unread"] == []
    assert file_access([("bash", {"command": "printf fixed > calc.py\ncat calc.py"})], EXISTING)[
        "edited_unread"] == ["calc.py"]
    # a newline inside quotes is data
    assert file_access([("bash", {"command": 'printf "a\nb" > calc.py'})], EXISTING)["edited_unread"] == ["calc.py"]


@pytest.mark.parametrize("command", ["sed -i -e's/a - b/a + b/' calc.py", "perl -pi -e's/-/+/' calc.py",
                                     "perl -pie 's/-/+/' calc.py", "sed -i -es/a/b/ -e s/c/d/ calc.py"])
def test_a_script_attached_to_its_option_still_leaves_the_file_as_the_operand(command):
    m = pi_driver.parse_transcript(_jsonl(_turn([_call(1, "bash", command=command)], _usage(10, 1))), EXISTING)
    assert m["read_before_edit"] is False and m["edited_unread"] == ["calc.py"]


def test_compaction_summaries_count_towards_prompt_and_completion_tokens_only():
    summary = {"type": "compaction_end", "reason": "threshold", "aborted": False, "willRetry": False,
               "result": {"summary": "s", "firstKeptEntryId": "e1", "tokensBefore": 30000,
                          "estimatedTokensAfter": 4000, "usage": _usage(9000, 400, cache_read=1000), "details": {}}}
    failed = {"type": "compaction_end", "reason": "overflow", "aborted": False, "willRetry": False,
              "errorMessage": "summary failed"}
    records = transcript()
    plain = pi_driver.parse_transcript(_jsonl(records), EXISTING)
    m = pi_driver.parse_transcript(_jsonl([*records[:10], {"type": "compaction_start", "reason": "threshold"},
                                           summary, failed, *records[10:]]), EXISTING)
    assert m["prompt_tokens"] == plain["prompt_tokens"] + 10000
    assert m["completion_tokens"] == plain["completion_tokens"] + 400
    # cached-prompt tokens count agent turns only: the native loop records no cached prefix for its summaries
    assert m["cached_prompt_tokens"] == plain["cached_prompt_tokens"]
    assert m["turns"] == plain["turns"] == 6


def test_an_error_or_exhausted_retries_is_recorded():
    records = transcript()[:-2]
    records[-2]["message"] = _assistant([], _usage(10, 0), stop="error", errorMessage="400 context too long")
    assert pi_driver.parse_transcript(_jsonl(records), EXISTING)["error"] == "400 context too long"
    retry = [*transcript(), {"type": "auto_retry_end", "success": False, "attempt": 3, "finalError": "503"}]
    assert pi_driver.parse_transcript(_jsonl(retry), EXISTING)["error"] == "503"
    empty = pi_driver.parse_transcript("", set())
    assert empty["turns"] == 0 and empty["answer"] == "" and empty["read_before_edit"] is None


# the command, with a fake Pi process
FAKE_PI = r'''"""A stand-in for `pi`: records how it was started, edits calc.py like the model would, prints a transcript."""
import json, os, sys
from pathlib import Path
Path(os.environ["FAKE_PI_RECORD"]).write_text(json.dumps({
    "argv": sys.argv[1:], "cwd": os.getcwd(), "models": json.loads(os.environ["PI_MODELS_JSON"]),
    "agent_dir": os.environ["PI_CODING_AGENT_DIR"], "offline": os.environ.get("PI_OFFLINE")}))
calc = Path("calc.py")
calc.write_text(calc.read_text().replace("a - b", "a + b"))
mode = os.environ.get("FAKE_PI_MODE", "ok")
if mode == "hang":
    import time; time.sleep(30)
sys.stdout.write(Path(os.environ["FAKE_PI_TRANSCRIPT"]).read_text())
sys.exit(3 if mode == "crash" else 0)
'''


@pytest.fixture
def fake_pi(tmp_path, monkeypatch):
    path = tmp_path / "bin" / "fake_pi.py"
    path.parent.mkdir()
    path.write_text(FAKE_PI, encoding="utf-8")
    record, script = tmp_path / "record.json", tmp_path / "transcript.jsonl"
    script.write_text(_jsonl(transcript()), encoding="utf-8")
    monkeypatch.setenv("FAKE_PI_RECORD", str(record))
    monkeypatch.setenv("FAKE_PI_TRANSCRIPT", str(script))
    started = []

    def launch(argv, env, ws, timeout, name):
        """`pi ...` as the driver builds it, run as `python fake_pi.py ...` (Windows runs no shebang scripts)."""
        started.append(argv)
        assert argv[0] == "pi"
        return pi_driver.launch_local([sys.executable, str(path), *argv[1:]], env, ws, timeout, name)

    return SimpleNamespace(launch=launch, started=started, record=record, script=script)


def _task(**kw):
    return SimpleNamespace(**{"id": "calc_fix", "prompt": PROMPT, "max_turns": 50, "wall_limit": 60, **kw})


def _prepare(task, ws: Path) -> dict[str, str]:
    from bakeoff.tasks import hash_tree, materialize
    materialize(ws, {"calc.py": "def add(a, b):\n    return a - b\n", "notes.md": "n\n", "util.py": "u\n",
                     "tests/test_calc.py": "from calc import add\n"})
    return hash_tree(ws)


def _grade(task, ws: Path, answer: str, baseline) -> tuple[bool, str]:
    fixed = "a + b" in (ws / "calc.py").read_text()
    return fixed and "all tests pass" in answer, "graded by the hidden checker" if fixed else "calc.py unchanged"


def _attempt(tmp_path, fake_pi, task=None):
    return pi_driver.run_attempt(task or _task(), 0, base_url="http://canary-pi-x-llm:8090", model=MODEL,
                                 launch=fake_pi.launch, prepare=_prepare, grade=_grade, workdir=tmp_path / "work")


def test_the_pi_command_passes_the_endpoint_model_prompt_and_task_directory(tmp_path, fake_pi):
    res = _attempt(tmp_path, fake_pi)
    seen = json.loads(fake_pi.record.read_text())
    ws = tmp_path / "work" / "calc_fix-0"
    assert Path(seen["cwd"]).resolve() == ws.resolve()  # Pi ran in the task's workspace, which the checker graded
    argv = seen["argv"]
    assert argv[-2:] == ["--", PROMPT]  # the task's prompt text, verbatim and last
    assert argv[argv.index("--mode") + 1] == "json" and "--no-session" in argv and "--no-extensions" in argv
    assert argv[argv.index("--provider") + 1] == "canary" and argv[argv.index("--model") + 1] == MODEL.name
    provider = seen["models"]["providers"]["canary"]
    assert provider["baseUrl"] == "http://canary-pi-x-llm:8090/v1" and provider["api"] == "openai-completions"
    assert provider["models"] == [{"id": MODEL.name, "contextWindow": 65536, "maxTokens": 8192,
                                   "samplingParams": {"temperature": 0.6, "top_k": 20}}]
    assert seen["agent_dir"] == pi_driver.AGENT_DIR and seen["offline"] == "1"
    assert res == {"task": "calc_fix", "repeat": 0, "driver": "pi", "ok": True, "note": "graded by the hidden checker",
                   "status": "done", "seconds": res["seconds"], "exit_code": 0, "turns": 6,
                   "prompt_tokens": 20550, "completion_tokens": 225, "cached_prompt_tokens": 16900,
                   "tool_calls": {"bash": 2, "edit": 1, "read": 1, "write": 3}, "read_before_edit": False,
                   "edited_unread": ["util.py"], "stop_reason": "stop", "events": len(transcript())}
    assert isinstance(res["seconds"], float)


def test_a_crash_a_turn_overrun_and_the_wall_limit_are_not_passes(tmp_path, fake_pi, monkeypatch):
    monkeypatch.setenv("FAKE_PI_MODE", "crash")
    crashed = _attempt(tmp_path / "a", fake_pi)
    assert crashed["status"] == "failed" and not crashed["ok"] and crashed["note"].endswith("pi exited 3")
    monkeypatch.setenv("FAKE_PI_MODE", "ok")
    over = _attempt(tmp_path / "b", fake_pi, _task(max_turns=5))
    assert over["status"] == "max_turns" and not over["ok"] and "6 turns, over the task's 5" in over["note"]
    monkeypatch.setenv("FAKE_PI_MODE", "hang")
    slow = _attempt(tmp_path / "c", fake_pi, _task(wall_limit=1))
    assert slow["status"] == "wall_limit" and not slow["ok"] and slow["exit_code"] is None
    assert "stopped at the 1 s wall-clock limit" in slow["note"]


def test_a_last_reply_cut_off_at_the_token_limit_is_no_finished_answer(tmp_path, fake_pi):
    records = transcript()
    for r in records[-4:]:
        if r["type"] in ("message_end", "turn_end"):
            r["message"] = {**r["message"], "stopReason": "length"}
    fake_pi.script.write_text(_jsonl(records), encoding="utf-8")
    res = _attempt(tmp_path, fake_pi)
    assert res["status"] == "failed" and not res["ok"] and res["stop_reason"] == "length"
    assert res["note"].endswith("pi stopped at the token limit")


def test_a_run_with_no_events_says_what_pi_printed(tmp_path, fake_pi):
    fake_pi.script.write_text("", encoding="utf-8")
    res = _attempt(tmp_path, fake_pi)
    assert res["status"] == "failed" and res["note"].endswith("pi never settled; no events (stderr: )")


def _route(calls=None) -> pi_driver.Route:
    return pi_driver.Route("http://127.0.0.1:8090", docker=lambda *a, check=True: (calls if calls is not None else []).append(a))


def test_docker_gives_pi_no_network_and_the_workspace_as_its_only_host_mount(tmp_path):
    route = _route()
    argv = pi_driver.docker_argv("canary-pi-a", tmp_path, route, {"K": "v"}, ["pi", "--mode", "json"])
    assert argv[:3] == ["docker", "run", "--rm"]
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"] == [
        f"type=bind,source={tmp_path.resolve()},target=/workspace",
        f"type=volume,source={route.volume},target=/run/llm"]  # a Docker volume holding only the socket
    assert not {"-v", "--volume", "--privileged", "-p", "--publish"} & set(argv)
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--network"] == ["none"]
    env = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert env == ["K=v", "PI_LLM_PORT=8090", f"HOME={pi_driver.HOME}"] and argv[argv.index("--cap-drop") + 1] == "ALL"
    if hasattr(os, "getuid"):  # as the host user that prepared the workspace: no capabilities needed to write it
        assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert argv[argv.index(pi_driver.IMAGE):] == [pi_driver.IMAGE, "pi", "--mode", "json"]


def test_launch_docker_runs_that_command_and_removes_a_container_past_the_limit(tmp_path, monkeypatch):
    runs, removed = [], []

    def fake_run(cmd, **kw):
        runs.append(cmd)
        if kw["timeout"] < 1:
            raise subprocess.TimeoutExpired(cmd, kw["timeout"], output=b'{"type":"agent_start"}\n')
        return SimpleNamespace(stdout="{}", stderr="", returncode=0)

    monkeypatch.setattr(pi_driver.subprocess, "run", fake_run)
    launch = pi_driver.launch_docker(_route(), docker=lambda *a, check=True: removed.append(a))
    ok = launch(["pi"], {}, tmp_path, 60, "canary-pi-a")
    assert ok.exit_code == 0 and not ok.timed_out and runs[0][-2:] == [pi_driver.IMAGE, "pi"]
    slow = launch(["pi"], {}, tmp_path, 0.5, "canary-pi-b")
    assert slow.timed_out and slow.stdout == '{"type":"agent_start"}\n' and removed == [("rm", "-f", "canary-pi-b")]


def test_the_route_forwards_only_the_llama_server_port_and_cleans_up():
    calls = []
    with _route(calls) as route:
        assert route.base_url == "http://127.0.0.1:8090"  # Pi's own loopback, relayed to the socket
    assert calls[0] == ("volume", "create", route.volume)
    forwarder = next(c for c in calls if c[0] == "run")
    # On the host network, so a llama-server bound to the host's loopback (the tower's) is reachable; it listens
    # only on the run's socket and connects only to llama-server's port.
    assert forwarder[forwarder.index("--network") + 1] == "host"
    assert forwarder[-2:] == ("UNIX-LISTEN:/run/llm/llm.sock,fork,unlink-early,mode=666", "TCP:127.0.0.1:8090")
    assert forwarder[forwarder.index("--mount") + 1] == f"type=volume,source={route.volume},target=/run/llm"
    assert calls[-2:] == [("rm", "-f", route.forwarder), ("volume", "rm", "-f", route.volume)]
    lan = pi_driver.Route("http://gpu-box:8091", docker=lambda *a, check=True: None)
    assert lan.target == "TCP:gpu-box:8091" and lan.port == 8091
    assert pi_driver.Route("http://localhost:8090").target == "TCP:127.0.0.1:8090"
    assert pi_driver.Route("http://[::1]:8090").target == "TCP6:[::1]:8090"
    # a reverse proxy's path prefix is kept, as the native driver keeps it (base_url + /v1/chat/completions)
    prefixed = pi_driver.Route("http://127.0.0.1:8443/llama/", docker=lambda *a, check=True: None)
    assert prefixed.base_url == "http://127.0.0.1:8443/llama" and prefixed.target == "TCP:127.0.0.1:8443"
    assert pi_driver.models_json(prefixed.base_url, MODEL)["providers"]["canary"]["baseUrl"] == \
        "http://127.0.0.1:8443/llama/v1"
    with pytest.raises(ValueError):
        pi_driver.Route("https://api.example.com")


def test_a_route_that_fails_half_way_removes_what_it_made():
    calls = []

    def docker(*a, check=True):
        calls.append(a)
        if a[0] == "run":
            raise subprocess.CalledProcessError(125, "docker run")

    with pytest.raises(subprocess.CalledProcessError):
        pi_driver.Route("http://127.0.0.1:8090", docker=docker).__enter__()
    assert [c[:2] for c in calls[-2:]] == [("rm", "-f"), ("volume", "rm")]


def test_root_owned_setup_files_are_handed_back_before_pi_runs_and_after_grading(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(pi_driver, "_docker", lambda *a, check=True: calls.append(a))
    (tmp_path / "ws" / ".git").mkdir(parents=True)
    ws = tmp_path / "ws"
    if not hasattr(os, "getuid"):
        assert pi_driver.give_back(ws) is False and not calls
        return
    assert pi_driver.give_back(ws) is False and not calls  # all the host user's already: no container
    # setup ran in the root sandbox (merge_conflict's .git): the files belong to someone else
    monkeypatch.setattr(pi_driver.os, "getuid", lambda: os.stat(ws).st_uid + 1)
    baseline = pi_driver.prepare_for_pi(lambda task, w: {"urls.py": "h"}, _task(), ws)
    assert baseline == {"urls.py": "h"}
    uid, gid = os.stat(ws).st_uid + 1, os.getgid()
    assert calls == [("run", "--rm", "--network", "none", "--mount", f"type=bind,source={ws.resolve()},target=/w",
                      pi_driver.SANDBOX_IMAGE, "chown", "-R", f"{uid}:{gid}", "/w")]

    def broken_checker(*a):
        raise RuntimeError("checker crashed")
    with pytest.raises(RuntimeError):
        pi_driver.grade_for_pi(broken_checker, _task(), ws, "", baseline)
    assert len(calls) == 2  # handed back even when the checker fails


def test_the_image_pins_the_release_the_driver_records():
    dockerfile = (ROOT / "reference" / "pi" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^ARG PI_VERSION=(\S+)$", dockerfile, re.M).group(1) == pi_driver.PI_VERSION
    assert f'"{pi_driver.PI_PACKAGE}@${{PI_VERSION}}"' in dockerfile
    assert f"org.opencontainers.image.revision={pi_driver.PI_COMMIT}" in dockerfile
    assert re.search(r"^FROM (\S+)$", dockerfile.split("AS pi")[1], re.M).group(1) == pi_driver.SANDBOX_IMAGE
    entrypoint = (ROOT / "reference" / "pi" / "entrypoint.sh").read_text(encoding="utf-8")
    assert '"$PI_CODING_AGENT_DIR/models.json"' in entrypoint and 'exec "$@"' in entrypoint
    assert '"TCP-LISTEN:${PI_LLM_PORT},bind=127.0.0.1,fork,reuseaddr" UNIX-CONNECT:/run/llm/llm.sock' in entrypoint
    assert re.search(r"apt-get install .*\bsocat\b", dockerfile)
    assert pi_driver.pin()["docs_url"] == pi_driver.PI_DOCS and pi_driver.PI_DOCS.startswith(f"https://github.com/earendil-works/pi/blob/v{pi_driver.PI_VERSION}/")


# the manual runner
def test_the_cli_passes_driver_pi_through_and_keeps_native_the_default(monkeypatch):
    assert inspect.signature(canary.run_standalone).parameters["driver"].default == "native"
    seen = []

    async def fake_run(task_ids, repeats, fixture, **kw):
        seen.append(dict(kw, task_ids=task_ids, repeats=repeats))

    monkeypatch.setattr(canary, "run_standalone", fake_run)
    canary.main(["--driver", "pi", "--tasks", "sqlite_report", "--repeats", "1"])
    canary.main(["--tasks", "sqlite_report"])
    assert seen[0]["driver"] == "pi" and seen[0]["task_ids"] == ["sqlite_report"] and seen[0]["repeats"] == 1
    assert "driver" not in seen[1]  # native: run_standalone's default


@pytest.mark.parametrize("flags", [["--plan-reinject"], ["--mask-batch-at", "0.45"], ["--system-line", "be careful"],
                                   ["--keep-metrics", "m.json"], ["--capture-prompts", "p.jsonl"],
                                   ["--tasks", "pdf_transformer"], ["--driver", "opencode"]])
def test_the_cli_refuses_what_pi_cannot_run(monkeypatch, flags, capsys):
    monkeypatch.setattr(canary, "run_standalone", lambda *a, **k: pytest.fail("must not run"))
    args = flags if flags[0] == "--driver" else ["--driver", "pi", *flags]
    with pytest.raises(SystemExit):
        canary.main(args)


def test_the_nightly_path_never_selects_pi():
    from harness.manager import Manager
    # The nightly entry point builds the native CanaryRunner, which has no driver to choose.
    nightly = inspect.getsource(Manager._build_canary)
    assert "CanaryRunner(self, self.cfg.canary, Path(self.cfg.canary.fixture_dir))" in nightly
    assert "driver" not in nightly and "pi_driver" not in nightly
    assert "driver" not in inspect.signature(canary.CanaryRunner).parameters
    with pytest.raises(TypeError):
        canary.CanaryRunner(SimpleNamespace(), SimpleNamespace(suite=""), driver="pi")
    assert not [p for p in (ROOT / "harness").rglob("*.py") if "pi_driver" in p.read_text(encoding="utf-8")]


def test_run_standalone_with_driver_pi_runs_the_suite_through_pi_and_writes_the_pin(monkeypatch, tmp_path, fake_pi):
    from harness import config as config_mod
    base = SimpleNamespace(default_model="qwen", models={"qwen": MODEL})
    monkeypatch.setattr(config_mod, "load", lambda: base)
    monkeypatch.setattr("harness.manager.Manager", lambda *a, **k: pytest.fail("the Pi driver needs no Manager"))
    built, docker = [], []
    monkeypatch.setattr(pi_driver, "build_images", lambda: built.append(True))
    monkeypatch.setattr(pi_driver, "_docker", lambda *a, check=True: docker.append(a))
    started = []

    def launcher(route):
        def launch(argv, env, ws, timeout, name):
            started.append((route.volume, argv[0], timeout))
            return fake_pi.launch(argv, env, ws, timeout, name)
        return launch

    monkeypatch.setattr(pi_driver, "launch_docker", launcher)
    monkeypatch.setattr("bakeoff.canary.prepare_hard_task", _prepare)
    monkeypatch.setattr("bakeoff.canary.grade_hard_task", _grade)
    monkeypatch.setattr(pi_driver, "ROOT", tmp_path)
    task = _task(id="sqlite_report")
    monkeypatch.setattr(canary, "load_suite", lambda: {"repeats": 2, "hard": [task, _task(id="other")], "web": []})
    out = asyncio.run(canary.run_standalone(["sqlite_report"], None, tmp_path, driver="pi"))
    data = json.loads(out.read_text(encoding="utf-8"))
    assert out.parent == tmp_path / "runs" and out.name.startswith("pi-canary-")
    assert data["driver"] == "pi" and data["model"] == MODEL.name and data["pi"]["version"] == pi_driver.PI_VERSION
    assert data["pi"]["commit"] == pi_driver.PI_COMMIT
    assert [(o["task"], o["repeat"], o["ok"]) for o in data["outcomes"]] == [("sqlite_report", 0, True),
                                                                             ("sqlite_report", 1, True)]
    assert built == [True] and {s[1] for s in started} == {"pi"} and {s[2] for s in started} == {60}
    volume = next(c[2] for c in docker if c[:2] == ("volume", "create"))
    assert {s[0] for s in started} == {volume}
    assert ("volume", "rm", "-f", volume) in docker
    models = json.loads(fake_pi.record.read_text())["models"]
    assert models["providers"]["canary"]["baseUrl"] == "http://127.0.0.1:8090/v1"
    with pytest.raises(ValueError):
        asyncio.run(canary.run_standalone([], None, tmp_path, driver="opencode"))


# the native driver records the same measures by hand


def test_a_native_hard_attempt_by_hand_records_them_and_the_nightly_one_does_not(tmp_path):
    from bakeoff.canary import CanaryRunner
    from bakeoff.tasks_hard import HARD_TASKS
    from harness.llm import Completion
    from harness.manager import Manager
    from test_daemon import Script, call, make_cfg
    task = next(t for t in HARD_TASKS if t.id == "merge_conflict")
    steps = [Completion(tool_calls=[call("read_file", 0, path="calc.py")], prompt_tokens=500, completion_tokens=20),
             Completion(tool_calls=[call("write_file", 1, path="util.py", content="u\n")], prompt_tokens=600,
                        completion_tokens=30),
             Completion(content="done", prompt_tokens=700, completion_tokens=5)]

    def prepare(tk, ws: Path):
        (ws / "calc.py").write_text("x\n")
        (ws / "util.py").write_text("u0\n")
        return {"calc.py": "h", "util.py": "h"}

    async def body(measure):
        cfg = make_cfg(tmp_path / ("by-hand" if measure else "nightly"))
        m = Manager(cfg, chat=Script(steps))
        await m.start(maintenance=False)
        try:
            runner = CanaryRunner(m, cfg.canary, tmp_path, {"repeats": 1, "hard": [task], "web": []}, poll_seconds=0.01,
                                  prepare_hard=prepare, grade_hard=lambda tk, ws, answer, base: (True, "graded"),
                                  measure=measure)
            return (await runner.run("manual")).outcomes[0]
        finally:
            await m.stop()
            m.db.close()

    by_hand = asyncio.run(body(native_measures))
    assert by_hand["status"] == "done" and by_hand["driver"] == "native" and by_hand["completion_tokens"] == 55
    assert by_hand["tool_calls"] == {"read_file": 1, "write_file": 1}
    assert by_hand["read_before_edit"] is False and by_hand["edited_unread"] == ["util.py"]
    nightly = asyncio.run(body(None))
    assert nightly["status"] == "done" and not {"driver", "tool_calls", "read_before_edit"} & set(nightly)


# stopping a run by hand stops Pi
def test_the_pi_suite_runs_on_the_caller_s_thread_so_an_interrupt_stops_it(monkeypatch, tmp_path):
    import threading
    seen = []
    monkeypatch.setattr(pi_driver, "run_standalone", lambda *a, **k: seen.append(threading.current_thread()) or tmp_path)
    monkeypatch.setattr(canary, "load_suite", lambda: {"repeats": 1, "hard": [_task()], "web": []})
    asyncio.run(canary.run_standalone([], None, tmp_path, driver="pi"))
    # not a worker thread: cancelling an await would leave one running attempt after attempt
    assert seen == [threading.main_thread()]


def test_an_interrupted_docker_run_removes_its_container_and_re_raises(tmp_path, monkeypatch):
    removed = []

    def interrupted(cmd, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(pi_driver.subprocess, "run", interrupted)
    launch = pi_driver.launch_docker(_route(), docker=lambda *a, check=True: removed.append(a))
    with pytest.raises(KeyboardInterrupt):
        launch(["pi"], {}, tmp_path, 60, "canary-pi-a")
    # killing the docker CLI leaves a --rm container running: it is removed by name
    assert removed == [("rm", "-f", "canary-pi-a")]


def test_an_interrupt_ends_the_suite_without_another_attempt(tmp_path):
    launched = []

    def launch(argv, env, ws, timeout, name):
        launched.append(name)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        pi_driver.run_suite({"repeats": 2, "hard": [_task(), _task(id="other")]}, None, "http://127.0.0.1:8090",
                            MODEL, launch, _prepare, _grade, tmp_path / "work")
    assert len(launched) == 1


SIGINT_RUN = r'''
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[2])
from bakeoff import canary, pi_driver
from harness import config as config_mod
from harness.config import ModelConfig
work = Path(sys.argv[1])
model = ModelConfig(name="m", base_url="http://127.0.0.1:8090")
config_mod.load = lambda: SimpleNamespace(default_model="m", models={"m": model})
pi_driver.build_images = lambda: None
pi_driver._docker = lambda *a, check=True: None
pi_driver.ROOT = work


def launcher(route):
    def launch(argv, env, ws, timeout, name):  # a slow fake Pi: one line per attempt started, then 3 s of "work"
        return pi_driver.launch_local([sys.executable, str(work / "slow_pi.py"), *argv[1:]], env, ws, timeout, name)
    return launch


pi_driver.launch_docker = launcher
task = SimpleNamespace(id="t", prompt="p", max_turns=50, wall_limit=60)
canary.load_suite = lambda: {"repeats": 4, "hard": [task], "web": []}
canary.prepare_hard_task = lambda t, ws: {}
canary.grade_hard_task = lambda t, ws, answer, baseline: (False, "graded")
canary.main(["--driver", "pi"])
'''
SLOW_PI = r'''
import sys, time
from pathlib import Path
with open(Path(__file__).with_name("started.log"), "a") as f:
    f.write("attempt\n")
time.sleep(3)
'''


@pytest.mark.skipif(sys.platform == "win32", reason="sends a real SIGINT to a child process (POSIX signals)")
def test_a_real_sigint_stops_a_manual_pi_run_after_the_attempt_in_flight(tmp_path):
    import signal
    import time
    (tmp_path / "run.py").write_text(SIGINT_RUN, encoding="utf-8")
    (tmp_path / "slow_pi.py").write_text(SLOW_PI, encoding="utf-8")
    log = tmp_path / "started.log"
    proc = subprocess.Popen([sys.executable, str(tmp_path / "run.py"), str(tmp_path), str(ROOT)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 60
        while not log.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert log.exists(), proc.stderr.read() if proc.poll() is not None else "no attempt started"
        proc.send_signal(signal.SIGINT)  # what Ctrl-C sends
        _, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert log.read_text().count("attempt") == 1, "attempts kept starting after the interrupt"
    assert proc.returncode != 0 and "KeyboardInterrupt" in err
