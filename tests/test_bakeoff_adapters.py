"""Stage A: event contracts and real CLI/Manager runs with a fake HTTP model."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import threading
import sys

import httpx
import pytest

from bakeoff import reference
from bakeoff.current_harness import run_current, session_result
from bakeoff.fake_endpoint import ScriptedServer
from bakeoff.fake_network import FakeEndpoint
from bakeoff.results import classify, outcome, report_lines
from bakeoff.run import prepare, write_report
from bakeoff.tasks import Context, TASKS


FACTS = {"finished": True, "stop_reason": "exit 0", "exit_code": 0, "wall_seconds": 1}
ANSWER = "The port is 8731, defined in app/config.py."
PARSERS = {
    "openhands": (reference.openhands_result,
                  [{"source": "agent", "action": {"kind": "RunAction"}},
                   {"source": "agent", "action": {"kind": "FinishAction", "message": ANSWER}}]),
    "opencode": (reference.opencode_result,
                 [{"type": "tool_use", "part": {"state": {"status": "completed"}}},
                  {"type": "text", "part": {"messageID": "1", "text": ANSWER}},
                  {"type": "step_finish", "part": {"tokens": {"input": 100, "output": 10}}}]),
    "hermes": (reference.hermes_result,
               [{"type": "tool_use", "name": "terminal"},
                {"type": "result", "text": ANSWER, "tokens": {"input": 100, "output": 10}, "exit_code": 0}]),
    "openclaw": (reference.openclaw_result,
                 [{"payloads": [{"text": ANSWER}], "meta": {"agentMeta": {"usage": {"input": 100, "output": 10}}}},
                  {"type": "message", "message": {"role": "assistant", "content": [{"type": "toolCall", "name": "read"}]}}]),
}


@pytest.mark.parametrize("name", PARSERS)
def test_candidate_final_tool_model_error_timeout_and_classification(name):
    parser, events = PARSERS[name]
    result = parser(events, FACTS)
    assert result["answer"] == ANSWER and result["tool_calls"] == 1 and result["finished"]
    assert classify({**result, "passed": True}) is None
    assert outcome(result) == "completion"
    timeout = parser(events, {**FACTS, "finished": False, "exit_code": None, "stop_reason": "wall_limit"})
    assert outcome(timeout) == "timeout" and classify(timeout) == "model"
    error_events = [{"type": "error", "error": "HTTP 500"}]
    if name == "openclaw":
        error_events = [{"payloads": [], "meta": {"error": {"message": "HTTP 500"}}}]
    if name == "hermes":
        error_events = [{"type": "result", "exit_code": 1, "error": "HTTP 500"}]
    error = parser(error_events, FACTS)
    assert not error["finished"] and error["model_errors"] == 1
    assert classify(error) == "model" and outcome(error) == "model_error"


def test_current_session_metrics_and_classification():
    final = {"answer": ANSWER, "status": "done", "stop_reason": "answer",
             "totals": {"turns": 2, "prompt_tokens": 200, "completion_tokens": 20},
             "run": {"context_tokens": 110}}
    events = [{"type": "tool_result", "data": {"ok": False}},
              {"type": "compaction", "data": {"tier": "mask"}},
              {"type": "compaction", "data": {"tier": "summary"}},
              {"type": "llm_retry", "data": {"error": "HTTP 500"}},
              {"type": "error", "data": {"message": "compaction summary failed"}}]
    result = session_result(final, events)
    assert result["answer"] == ANSWER and result["turns"] == 2 and result["tool_calls"] == 1
    assert result["compactions"] == result["masking_events"] == result["compaction_failures"] == 1
    assert result["retries"] == 1 and result["context_tokens"] == 110
    assert classify(result) == "harness/tooling"
    assert outcome({**result, "status": "timeout"}) == "timeout"
    failed = session_result({**final, "status": "failed"}, [{"type": "error", "data": {"message": "HTTP 500"}}])
    assert classify(failed) == "model" and not failed["finished"]


def test_parse_noise_pretty_envelope_and_non_objects():
    assert reference.parse_events('banner\n[]\n{"type":"text"}\n{broken') == [{"type": "text"}]
    assert reference.parse_events(json.dumps({"payloads": []}, indent=2)) == [{"payloads": []}]


def test_missing_envelope_is_adapter_failure():
    for parser in (reference.hermes_result, reference.openclaw_result):
        assert classify(parser([], FACTS)) == "adapter"


@pytest.mark.parametrize("name", PARSERS)
def test_candidate_tool_error_events(name):
    parser, events = PARSERS[name]
    errors = {
        "openhands": {"source": "environment", "observation": {"is_error": True}},
        "opencode": {"type": "tool_use", "part": {"state": {"status": "error"}}},
        "hermes": {"type": "tool_result", "is_error": True},
        "openclaw": {"type": "message", "message": {"role": "toolResult", "isError": True}},
    }
    result = parser([*events, errors[name]], FACTS)
    assert result["tool_errors"] == 1 and classify(result) == "harness/tooling"


def test_container_timeout_preserves_events_and_removes_only_owned_container(tmp_path, monkeypatch):
    task = next(t for t in TASKS if t.id == "repo_qa")
    removed = []
    monkeypatch.setattr(reference, "docker", lambda *args, **kwargs: removed.append(args))

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], task.wall_limit, output=b'{"type":"text"}\n')

    monkeypatch.setattr(reference.subprocess, "run", timeout)
    events, facts = reference.run_container("opencode", task, tmp_path, {}, ["opencode"])
    assert events == [{"type": "text"}] and outcome(facts) == "timeout"
    assert len(removed) == 1 and removed[0][:2] == ("rm", "-f") and removed[0][2].startswith("opencode-")


def test_container_missing_docker_is_infrastructure(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("docker unavailable")
    monkeypatch.setattr(reference.subprocess, "run", unavailable)
    task = next(t for t in TASKS if t.id == "repo_qa")
    events, facts = reference.run_container("opencode", task, tmp_path, {}, ["opencode"])
    assert not events and classify(facts) == "infrastructure"


def test_reports_repeat_percentiles_unknown_and_failure_classes(tmp_path):
    rows = [{**FACTS, "task": "repo_qa", "repeat": i % 2, "passed": i == 0, "turns": None,
             "tool_errors": 0, "invalid_tool_calls": 0, "wall_seconds": i + 1} for i in range(4)]
    summaries = [{"model": "hermes/fake", "tasks": rows, "load_seconds": 0, "peak_vram_mib": None}]
    lines = "\n".join(report_lines(summaries))
    assert "| hermes/fake | 0 | 1/2 |" in lines and "| hermes/fake | 1 | 0/2 |" in lines
    assert "| prompt_tokens | unknown |" in lines and "| model |" in lines
    assert "Stage A measures" in write_report(summaries, tmp_path).read_text()
    assert classify({"note": "checker error: docker unavailable"}) == "infrastructure"


def test_fake_http_stream_nonstream_tools_errors_and_exhaustion(tmp_path):
    call = {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"app/config.py"}'}}
    server = ScriptedServer(("127.0.0.1", 0), [{"message": {"content": None, "tool_calls": [call]}},
                                               {"message": {"content": ANSWER}}, {"status": 503}], tmp_path / "requests.jsonl")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False) as client:
            assert client.get("/health").status_code == 200
            first = client.post("/v1/chat/completions", json={"stream": False}).json()
            assert first["choices"][0]["message"]["tool_calls"] == [call]
            assert "[DONE]" in client.post("/v1/chat/completions", json={"stream": True}).text
            assert client.post("/v1/chat/completions", json={}).status_code == 503
            assert client.post("/v1/chat/completions", json={}).status_code == 500
            assert client.post("/v1/responses", json={}).status_code == 404
        assert len((tmp_path / "requests.jsonl").read_text().splitlines()) == 4
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def docker_ready():
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.docker
@pytest.mark.parametrize("name", sorted(reference.HARNESSES))
def test_core_task_with_fake_endpoint_and_hidden_checker(name, tmp_path):
    if os.environ.get("BAKEOFF_DOCKER_TESTS") != "1" or not docker_ready():
        pytest.skip("set BAKEOFF_DOCKER_TESTS=1 on a Docker host with the reference images built")
    task = next(t for t in TASKS if t.id == "repo_qa")
    tool, arguments = {
        "agent-harness": ("read_file", {"path": "app/config.py"}),
        "openhands": ("terminal", {"command": "cat app/config.py", "security_risk": "LOW"}),
        "opencode": ("read", {"filePath": "/workspace/app/config.py"}),
        "hermes": ("terminal", {"command": "cat app/config.py"}),
        "openclaw": ("read", {"path": "/workspace/app/config.py"}),
    }[name]
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"steps": [{"message": {"content": None, "tool_calls": [
        {"id": "call_fixture", "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments)}}]}},
                                  {"message": {"content": ANSWER}}],
                                 "auxiliary": [{"contains": "title generator", "message": {"content": "Core fixture"}},
                                               {"contains": "You name chat sessions", "message": {"content": '{"title":"Core fixture"}'}}]}))
    run_dir = tmp_path / "run"
    sandbox, baseline = prepare(task, run_dir / "workspace")
    try:
        with FakeEndpoint(script, tmp_path / "endpoint") as endpoint:
            assert json.loads(reference.docker("network", "inspect", endpoint.network).stdout)[0]["Internal"]
            if name == "agent-harness":
                result = asyncio.run(run_current(task, run_dir, "fake", f"http://127.0.0.1:{endpoint.port}"))
            else:
                result = reference.HARNESSES[name](task, run_dir, "fake", endpoint.container_port)
            passed, note = task.check(Context(run_dir / "workspace", sandbox, result["answer"], baseline))
            assert passed, (note, result, list(run_dir.glob("*.log")))
            assert result["finished"] and result["tool_calls"] == 1, result
            requests = [row["request"] for line in (tmp_path / "endpoint/requests.jsonl").read_text().splitlines()
                        for row in [json.loads(line)] if row["index"] is not None]
            assert len(requests) == 2
            assert any(m.get("role") == "tool" for m in requests[-1]["messages"])
    finally:
        sandbox.stop()


@pytest.mark.docker
def test_reference_module_cli_fake_run_and_rescore(tmp_path):
    if os.environ.get("BAKEOFF_DOCKER_TESTS") != "1" or not docker_ready():
        pytest.skip("requires opt-in Docker reference images")
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"steps": [{"message": {"content": ANSWER}}],
                                 "auxiliary": [{"contains": "title generator", "message": {"content": "Core fixture"}}]}))
    run_dir = tmp_path / "cli-run"
    subprocess.run([sys.executable, "-m", "bakeoff.reference", "--harness", "opencode", "--suite", "core",
                    "--tasks", "repo_qa", "--fake-script", str(script), "--no-build", "--resume", str(run_dir)],
                   check=True, timeout=120, capture_output=True)
    record = run_dir / "fake/repo_qa-0/result.json"
    assert json.loads(record.read_text())["passed"]
    assert (run_dir / "fake-endpoint/requests.jsonl").is_file()
    assert not (run_dir / "memory.csv").exists()
    subprocess.run([sys.executable, "-m", "bakeoff.rescore", str(run_dir)], check=True, timeout=60, capture_output=True)
    assert json.loads(record.read_text())["failure_class"] is None
