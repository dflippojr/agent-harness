"""bakeoff/context_test.py (#943): --base-url measures a server it did not start, and Linux reads memory from /proc."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bakeoff import context_test


def test_process_memory_linux_reads_proc_status(tmp_path):
    status = tmp_path / "4242" / "status"
    status.parent.mkdir()
    status.write_text("Name:\tllama-server\nVmRSS:\t 2097152 kB\nRssAnon:\t 1048576 kB\nRssFile:\t 1048576 kB\n",
                      encoding="utf-8")
    assert context_test.process_memory_linux(4242, proc_root=tmp_path) == (1024, 2048)  # (private, working set)


def test_system_memory_linux_reads_meminfo(tmp_path):
    (tmp_path / "meminfo").write_text(
        "MemTotal:       23068672 kB\nMemAvailable:   10485760 kB\nCommitted_AS:    8388608 kB\n"
        "CommitLimit:   16777216 kB\n", encoding="utf-8")
    assert context_test.system_memory_linux(proc_root=tmp_path) == (10240, 8192, 16384)


def test_process_memory_linux_names_a_missing_field(tmp_path):
    status = tmp_path / "7" / "status"
    status.parent.mkdir()
    status.write_text("Name:\tgone\nVmRSS:\t 1024 kB\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="RssAnon"):
        context_test.process_memory_linux(7, proc_root=tmp_path)


@pytest.fixture
def fake_llama():
    """A local stand-in for llama-server: the /tokenize and /v1/chat/completions calls perf.py makes."""
    paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            paths.append(self.path)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/tokenize":
                reply = {"tokens": body["content"].split()}
            else:
                reply = {"choices": [{"message": {"role": "assistant", "content": "ok"}}],
                         "timings": {"prompt_ms": 1000.0, "prompt_per_second": 900.0,
                                     "predicted_n": 200, "predicted_per_second": 40.0}}
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", paths
    finally:
        server.shutdown()
        server.server_close()


def test_base_url_measures_a_running_server_without_starting_one(tmp_path, monkeypatch, fake_llama):
    base_url, paths = fake_llama

    def refuse(*args, **kwargs):
        raise AssertionError("--base-url must not start a llama-server")

    monkeypatch.setattr(context_test, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(context_test, "LlamaServer", refuse)
    monkeypatch.setattr(context_test, "gpu_memory_used_mib", lambda: 1234)
    monkeypatch.setattr(context_test, "system_memory", lambda: (5000, 6000, 9000))

    context_test.main(["--base-url", base_url, "--model", "fake-model", "--ctx", "8192"])

    run_dir = next((tmp_path / "runs").iterdir())
    rows = json.loads((run_dir / "context.json").read_text(encoding="utf-8"))
    assert rows[0]["ctx"] == 8192 and "error" not in rows[0]
    assert rows[0]["perf"] and rows[0]["perf"][0]["context_tokens"] > 0
    assert "/v1/chat/completions" in paths
    assert rows[0]["after_load"]["server_private_mib"] is None  # no --server-pid, so no process memory
    report = (run_dir / "context.md").read_text(encoding="utf-8")
    assert "| 8192 | n/a |" in report


def test_base_url_takes_one_ctx_size(capsys):
    with pytest.raises(SystemExit):
        context_test.main(["--base-url", "http://127.0.0.1:1", "--ctx", "32768,65536"])
    assert "one value" in capsys.readouterr().err
