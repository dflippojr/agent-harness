"""The `harness` CLI against a real disposable daemon (#710).

`harness.cli.main` runs in this process, so harness/cli.py coverage counts. The daemon is a `harness.fake_instance`
on loopback in fake model mode (docs/ui-testing.md): the real runner, policy and approvals with a scripted model, and
no Docker, GPU or model server. The prompts below pick scripts from harness/fake_fixtures/ui.yaml.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from harness import cli
from waits import scaled

ROOT = Path(__file__).resolve().parents[1]
ADMIN = "/api/admin/v1"
TWO_APPROVALS = "Please make two approvals"       # script approval-batch: a.json and b.json under protected/
PROTECTED = "Update the protected settings"       # script approval-sheet: protected/settings.json, then "Finished."
SLOW = "Run a slow task"                          # script slow-running: six turns 8 s apart
HELLO = "Hello there"                             # script tab-bar-tour


class Daemon:
    def __init__(self, url: str, token: str, local_token: str, directory: Path):
        self.url, self.token, self.local_token, self.directory = url, token, local_token, directory

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "X-Agent-Harness-Client": f"cli/{cli.CLIENT_PROTOCOLS['cli']}"}

    def get(self, path: str):
        response = httpx.get(self.url + ADMIN + path, headers=self.headers(), timeout=30, trust_env=False)
        response.raise_for_status()
        return response.json()

    def wait_status(self, sid: str, *statuses: str) -> dict:
        deadline = time.monotonic() + scaled(60)
        while True:
            session = self.get(f"/sessions/{sid}")
            if session["status"] in statuses:
                return session
            assert time.monotonic() < deadline, f"session {sid} stayed {session['status']}, not {statuses}"
            time.sleep(0.2)

    def wait_approval(self, sid: str, count: int = 1) -> list[dict]:
        deadline = time.monotonic() + scaled(60)
        while True:
            pending = self.get(f"/sessions/{sid}/approvals")
            if len(pending) >= count:
                return pending
            assert time.monotonic() < deadline, f"session {sid} never asked for {count} approval(s)"
            time.sleep(0.2)


def _read_lines(stream, lines: queue.Queue) -> None:
    for line in stream:
        lines.put(line.rstrip("\n"))
    lines.put(None)


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    """One disposable daemon for the module; teardown always stops it, whether or not a test failed."""
    base = tmp_path_factory.mktemp("cli-e2e")
    instance = base / "instance"
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
           # The only outbound request in fake mode is the scanner download at first start. A closed proxy port makes it
           # fail at once; the daemon still starts and no step here pushes or merges. (--dir must start empty, so the
           # cached scanner cannot be copied in first.)
           "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9"}
    env.pop("NO_PROXY", None)
    env.pop("no_proxy", None)
    log = (base / "server.log").open("w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "harness.fake_instance", "--dir", str(instance), "--no-seed"],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8")
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=_read_lines, args=(proc.stdout, lines), daemon=True).start()
    try:
        seen, deadline = [], time.monotonic() + scaled(120)
        while not any(line.startswith("READY ") for line in seen):
            try:
                line = lines.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                line = None
            assert line is not None and time.monotonic() < deadline, (
                "the instance never got ready:\n" + "\n".join(seen) + "\n" + (base / "server.log").read_text("utf-8"))
            seen.append(line)
        text = "\n".join(seen)
        handle = json.loads((instance / "instance.json").read_text(encoding="utf-8"))
        assert handle["status"] == "ready"
        token, local_token = (re.search(rf"{name} token:\s+(\S+)", text) for name in ("Owner", "Local"))
        assert token and local_token, text
        yield Daemon(handle["server"], token.group(1), local_token.group(1), instance)
    finally:
        try:
            subprocess.run([sys.executable, "-m", "harness.fake_instance", "stop", "--dir", str(instance)],
                           cwd=ROOT, timeout=120, capture_output=True, check=False)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=30)
            log.close()


class Harness:
    """Runs `harness <argv>` in this process: call it for (exit code, stdout without colors)."""

    def __init__(self, home: Path, monkeypatch, capsys):
        self.home, self._monkeypatch, self._capsys = home, monkeypatch, capsys
        self._lock = threading.Lock()

    def __call__(self, *argv: str) -> tuple[int, str]:
        with self._lock:
            self._monkeypatch.setattr(sys, "argv", ["harness", *argv])
            self._capsys.readouterr()
            code = cli.main()
            return code, cli_plain(self._capsys.readouterr().out)


@pytest.fixture
def harness(daemon, tmp_path, monkeypatch, capsys):
    """A CLI pointed at the daemon, with its config home in a temp dir."""
    home = tmp_path / ".agent-harness"
    monkeypatch.setattr(cli, "HARNESS_HOME", home)
    monkeypatch.setattr(cli, "DEFAULT_CONFIG", home / "client" / "config.json")
    monkeypatch.setattr(cli, "DEFAULT_RUNNER_CONFIG", home / "runner" / "config.json")
    for name in ("BASE", "TOKEN", "LOCAL_TOKEN", "CONFIG_PATH"):   # configure() sets these: restore them afterwards
        monkeypatch.setattr(cli, name, getattr(cli, name))
    monkeypatch.setenv("HARNESS_URL", daemon.url)
    monkeypatch.setenv("HARNESS_TOKEN", daemon.token)
    monkeypatch.setenv("HARNESS_LOCAL_TOKEN", daemon.local_token)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    return Harness(home, monkeypatch, capsys)


def cli_plain(text: str) -> str:
    for code in (cli.DIM, cli.BOLD, cli.YELLOW, cli.GREEN, cli.RED, cli.CYAN, cli.RESET):
        text = text.replace(code, "")
    return text


def session_id(output: str) -> str:
    match = re.search(r"^session (\S+) \(", output, re.MULTILINE)
    assert match, output
    return match.group(1)


def test_the_config_file_names_the_daemon_and_the_environment_wins(harness, daemon, monkeypatch):
    path = harness.home / "client" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"server": daemon.url + "/", "token": f" {daemon.token} "}), encoding="utf-8")
    for name in ("HARNESS_URL", "HARNESS_TOKEN"):
        monkeypatch.delenv(name)
    assert harness("queue") == (0, "")
    assert (cli.BASE, cli.TOKEN) == (daemon.url, daemon.token)
    path.write_text(json.dumps({"server": "http://127.0.0.1:9", "token": "not-the-token"}), encoding="utf-8")
    monkeypatch.setenv("HARNESS_URL", daemon.url)
    monkeypatch.setenv("HARNESS_TOKEN", daemon.token)
    assert harness("queue") == (0, "")
    assert (cli.BASE, cli.TOKEN) == (daemon.url, daemon.token)


def test_new_list_show_transcript_approve_deny_and_send_watch(harness, daemon):
    code, out = harness("new", TWO_APPROVALS, "--project", "ui", "--detach")
    sid = session_id(out)
    assert code == 0 and out.startswith(f"session {sid} (ui, ")
    first = daemon.wait_approval(sid)[0]
    code, out = harness("list")
    row = next(line for line in out.splitlines() if line.startswith(f"{sid}  "))
    assert code == 0 and "waiting_approval" in row and " ui " in row
    code, out = harness("show", sid)
    assert code == 0 and json.loads(out)["id"] == sid
    code, out = harness("transcript", sid)
    assert code == 0 and TWO_APPROVALS in out
    assert harness("approve", sid) == (0, f"{first['id']} approved\n")
    second = next(a for a in daemon.wait_approval(sid) if a["id"] != first["id"])
    assert harness("deny", sid, second["id"], "--note", "not this one") == (0, f"{second['id']} denied\n")
    daemon.wait_status(sid, "done")
    code, out = harness("watch", sid, "--no-prompt")   # replays the whole session
    assert code == 0
    assert f"approval needed [{first['id']}]: write_file — protected file (fake-model UI testing)" in out
    assert f"approval {first['id']} approved" in out and f"approval {second['id']} denied" in out
    assert out.rstrip().splitlines()[-1].startswith("status: done")
    before = daemon.get(f"/sessions/{sid}")["last_event_seq"]
    code, out = harness("send", sid, HELLO, "--watch")   # a follow-up streams only the new events
    assert code == 0 and out.startswith("sent; session is ")
    assert "assistant: Hi! This is the fake model." in out and out.rstrip().splitlines()[-1].startswith("status: done")
    assert f"approval needed [{first['id']}]" not in out
    assert daemon.get(f"/sessions/{sid}")["last_event_seq"] > before


def test_watch_prints_the_approval_needed_line_while_the_owner_approves_elsewhere(harness, daemon):
    approved: list[dict] = []

    def owner_elsewhere():
        deadline = time.monotonic() + scaled(60)
        while not approved and time.monotonic() < deadline:
            for session in daemon.get("/sessions"):
                if session["status"] == "waiting_approval" and session["title"].startswith(PROTECTED[:20]):
                    response = httpx.post(f"{daemon.url}{ADMIN}/sessions/{session['id']}/approvals/pending",
                                          json={"decision": "approve", "note": ""}, headers=daemon.headers(),
                                          timeout=30, trust_env=False)
                    approved.append(response.json())
            time.sleep(0.2)

    helper = threading.Thread(target=owner_elsewhere, daemon=True)
    helper.start()
    code, out = harness("new", PROTECTED, "--project", "ui", "--no-prompt")
    helper.join(timeout=scaled(30))
    assert code == 0 and approved
    assert re.search(r"approval needed \[\S+\]: write_file — protected file \(fake-model UI testing\)", out)
    assert '"path": "protected/settings.json"' in out
    assert f"approval {approved[0]['id']} approved" in out
    assert "Finished. The tool row shows whether the write was approved or denied." in out
    assert out.rstrip().splitlines()[-1].startswith("status: done")


def test_queue_lists_waiting_sessions_and_cancel_stops_the_running_one(harness, daemon):
    code, out = harness("new", SLOW, "--project", "scratch", "--detach")
    running = session_id(out)
    daemon.wait_status(running, "running")
    code, out = harness("new", HELLO, "--project", "scratch", "--detach")
    waiting = session_id(out)
    assert daemon.get(f"/sessions/{waiting}")["status"] == "queued"
    assert harness("queue") == (0, f"0  {running}\n1  {waiting}\n")   # position 0 holds the slot
    code, out = harness("cancel", running)
    assert code == 0 and out.strip() in ("cancelled", "cancelling")
    assert daemon.wait_status(running, "cancelled")["status"] == "cancelled"
    code, out = harness("watch", waiting, "--no-prompt")   # the queued session runs once the slot is free
    assert code == 0 and "assistant: Hi! This is the fake model." in out
    assert harness("queue") == (0, "")


def test_version_reports_a_compatible_server(harness):
    code, out = harness("version")
    assert code == 0
    assert f"Agent Harness CLI {cli.MAC_CLIENT_VERSION}" in out
    assert "Agent Harness Server " in out and "compatibility: compatible" in out
