"""bakeoff/checkpoint_replay.py (#856): replays captured sessions request by request and prices each prefill."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx

from bakeoff import checkpoint_replay
from harness import compaction


def _capture(path: Path, rows: list[dict]) -> Path:
    path.write_bytes("".join(json.dumps(r) + "\n" for r in rows).encode())
    return path


def _rows() -> list[dict]:
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    system = {"role": "system", "content": "sys"}
    task = {"role": "user", "content": "task"}
    return [
        {"seq": 1, "messages": [system, task], "tools": tools, "prompt_tokens": 100, "cache_tokens": 0,
         "extra": {"temperature": 0.6, "max_tokens": 8192}},
        {"seq": 2, "messages": [system, task], "tools": tools, "error": "ReadTimeout: boom"},
        {"seq": 3, "messages": [{"role": "system", "content": compaction.SUMMARY_SYSTEM}, task], "tools": None,
         "prompt_tokens": 40, "cache_tokens": 0, "extra": {"chat_template_kwargs": {"enable_thinking": False}}},
    ]


def _server(sent: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        n = len(sent)
        return httpx.Response(200, json={"timings": {"prompt_n": 10 * n, "cache_n": 90, "prompt_ms": 12.34 * n}})
    return httpx.MockTransport(handler)


def test_replays_every_answered_call_with_its_template_options_and_one_token(tmp_path):
    sent: list[dict] = []
    capture = _capture(tmp_path / "s.prompts.jsonl", _rows())
    with httpx.Client(base_url="http://llama", transport=_server(sent)) as client:
        result = checkpoint_replay.replay([capture], "http://llama", client=client)

    # the failed call (seq 2) is not sent again; the rest go in order with their own options
    assert [b["messages"][0]["content"] for b in sent] == ["sys", compaction.SUMMARY_SYSTEM]
    assert all(b["max_tokens"] == 1 and b["stream"] is False for b in sent)
    assert sent[0]["temperature"] == 0.6 and sent[0]["tools"][0]["function"]["name"] == "read_file"
    assert "tools" not in sent[1] and sent[1]["chat_template_kwargs"] == {"enable_thinking": False}

    session = result["sessions"][0]
    assert [r["seq"] for r in session["per_request"]] == [1, 3]
    assert [r["summary"] for r in session["per_request"]] == [False, True]
    assert session["per_request"][0]["run_prompt_tokens"] == 100
    assert result["total"] == {"requests": 2, "prompt_tokens": 210, "cached": 180, "read": 30,
                               "reuse": round(180 / 210, 3), "prefill_s": round((12.34 + 24.68) / 1000, 1)}
    assert "memory" not in result


def test_memory_sampler_keeps_the_peak_of_anonymous_memory_and_swap(tmp_path):
    (tmp_path / "memory.stat").write_text("anon 1048576\nfile 999999999\n")
    (tmp_path / "memory.swap.current").write_text("0\n")
    sampler = checkpoint_replay.MemorySampler(tmp_path, interval=60)  # only enter, sample() and exit read
    with sampler:
        (tmp_path / "memory.stat").write_text("anon 3145728\nfile 1\n")
        (tmp_path / "memory.swap.current").write_text("2097152\n")
        sampler.sample()
        (tmp_path / "memory.stat").write_text("anon 2097152\nfile 1\n")
    # file cache (the mmap'd model) never counts; the peak survives the later drop
    assert sampler.result() == {"peak_anon_mib": 3, "peak_swap_mib": 2}


def test_main_writes_the_result_with_label_and_memory(tmp_path, monkeypatch, capsys):
    sent: list[dict] = []
    capture = _capture(tmp_path / "s.prompts.jsonl", _rows()[:1])
    cgroup = tmp_path / "docker-abc.scope"
    cgroup.mkdir()
    (cgroup / "memory.stat").write_text("anon 5242880\n")
    real_client = httpx.Client
    monkeypatch.setattr(checkpoint_replay.httpx, "Client",
                        lambda **kw: real_client(transport=_server(sent), **kw))
    monkeypatch.setattr(checkpoint_replay, "CGROUP_ROOT", tmp_path)

    def inspect(cmd, **kw):
        assert cmd[:2] == ["docker", "inspect"] and cmd[-1] == "llama-test"
        return subprocess.CompletedProcess(cmd, 0, stdout="abc\n", stderr="")
    monkeypatch.setattr(checkpoint_replay.subprocess, "run", inspect)

    out = tmp_path / "out" / "r.json"
    assert checkpoint_replay.main([str(out), str(capture), "--container", "llama-test", "--label", "512",
                                   "--sample-seconds", "60"]) == 0
    result = json.loads(out.read_text())
    assert result["label"] == "512" and result["memory"] == {"peak_anon_mib": 5, "peak_swap_mib": 0}
    assert result["total"]["requests"] == 1 and len(sent) == 1
    assert '"label": "512"' in capsys.readouterr().out
