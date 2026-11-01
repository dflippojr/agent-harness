"""The verify skill (.claude/skills/verify, #735): argument parsing, the doctor's verdicts, token redaction, and that
cleanup leaves nothing behind after a failed step.

A full run needs a Stables checkout, Node.js and a browser; these use synthetic answers and a stand-in instance.
"""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from unittest.mock import Mock

import httpx
import psutil
import pytest
import yaml

from harness import fake_instance

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".claude" / "skills" / "verify" / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(f"verify_skill_{name}", SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


doctor = load("doctor")
verify = load("verify")
REAL_PROCESS_ITER = psutil.process_iter  # before conftest's isolated_local_supervision replaces it


@pytest.fixture(autouse=True)
def real_process_table(isolated_local_supervision, monkeypatch):
    """The POSIX containment finds a run's orphans by scanning the process table; conftest's autouse supervision
    isolation empties it, which would make cleanup find nothing (#744 review). Requesting that fixture orders this
    one after it."""
    monkeypatch.setattr(psutil, "process_iter", REAL_PROCESS_ITER)
SERVER, WEB = 50101, 50102
HANDLE = {"status": "ready", "server": f"http://127.0.0.1:{SERVER}", "web": f"http://127.0.0.1:{WEB}"}
LISTENING = [("127.0.0.1", SERVER), ("127.0.0.1", WEB)]
ANY = "0.0.0.0"  # noqa: S104 - a listener the doctor must refuse
SEEDED = {
    "/api/v1/sessions": [{"title": t, "status": s} for t, _, s in fake_instance.SEED_SESSIONS],
    "/api/admin/v1/chats": [{"title": p, "status": "done"} for p in fake_instance.SEED_CHATS],
    "/api/admin/v1/jobs": [{"name": j["name"]} for j in fake_instance.SEED_JOBS],
}
HEALTHY = {"/health": {"ok": True}, "/ready": {"status": "ready"}}


def client(answers: dict) -> httpx.Client:
    def answer(request):
        body = answers.get(request.url.path)
        if isinstance(body, int):
            return httpx.Response(body)
        return httpx.Response(404) if body is None else httpx.Response(200, json=body)
    return httpx.Client(transport=httpx.MockTransport(answer), base_url=f"http://127.0.0.1:{SERVER}")


@pytest.fixture
def instance(tmp_path):
    root = tmp_path / "instance"
    (root / "config").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "data" / "fake-model-instance").write_text("x", encoding="utf-8")
    (root / "config" / "harness.yaml").write_text(yaml.safe_dump(
        {"data_dir": (root / "data").as_posix(), "repos_dir": (root / "repos").as_posix()}), encoding="utf-8")
    return root


CLEAN_ENV = {"PATH": "/usr/bin", "HARNESS_FAKE_MODEL": "fixtures"}


# Argument parsing ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("argv", "message"), [
    (["run", "--feature", "nope"], "invalid choice"),
    ([], "required"),
    (["cleanup"], "--evidence"),
])
def test_bad_arguments_exit_with_usage(argv, message, capsys):
    with pytest.raises(SystemExit) as exit_:
        verify.main(argv)
    assert exit_.value.code == 2
    assert message in capsys.readouterr().err


def test_run_passes_features_once_each_and_keep(monkeypatch, tmp_path):
    seen = {}

    def run(self, features, keep=False):
        seen.update(evidence=self.evidence, features=features, keep=keep)
        return 0
    monkeypatch.setattr(verify.Run, "run", run)
    assert verify.main(["run", "--feature", "task", "--feature", "task", "--keep",
                        "--evidence", str(tmp_path / "ev")]) == 0
    assert seen == {"evidence": (tmp_path / "ev").resolve(), "features": ["task"], "keep": True}
    assert verify.main(["run", "--evidence", str(tmp_path / "ev2")]) == 0
    assert seen["features"] == [] and seen["keep"] is False


def test_every_run_gets_its_own_evidence_folder(tmp_path, capsys):
    assert len({verify.run_name() for _ in range(5)}) == 5
    used = tmp_path / "used"
    used.mkdir()
    (used / "launch.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit):
        verify.main(["run", "--evidence", str(used)])
    assert "not empty" in capsys.readouterr().err


def test_recovery_cleanup_redacts_the_evidence(tmp_path):
    evidence = tmp_path / "evidence"
    (evidence / "playwright").mkdir(parents=True)
    (evidence / "launch.json").write_text(json.dumps({"evidence": str(evidence), "processes": []}), encoding="utf-8")
    with zipfile.ZipFile(evidence / "playwright" / "trace.zip", "w") as z:
        z.writestr("0-trace.network", '{"html": "harness.ownerToken: ho-killedrunowner1234", "h": "Bearer abc"}')
    (evidence / "drive.txt").write_text("token ho-killedrunowner1234\n", encoding="utf-8")
    assert verify.main(["cleanup", "--evidence", str(evidence)]) == 0
    with zipfile.ZipFile(evidence / "playwright" / "trace.zip") as z:
        assert z.read("0-trace.network").decode() == '{"html": "harness.ownerToken: [REDACTED]", "h": "Bearer [REDACTED]"}'
    assert (evidence / "drive.txt").read_text(encoding="utf-8") == "token [REDACTED]\n"


def test_the_process_table_is_real_here():
    assert os.getpid() in {p.pid for p in psutil.process_iter()}


def test_cleanup_needs_a_launch_record(tmp_path, capsys):
    with pytest.raises(SystemExit):
        verify.main(["cleanup", "--evidence", str(tmp_path)])
    assert "no readable launch.json" in capsys.readouterr().err


def test_drive_always_runs_the_smoke_journey():
    argv = verify.Run.drive_argv(Mock(), ["task"])
    assert argv[argv.index("--grep") + 1] == "@smoke|@task"
    assert "--workers=1" in argv and str(verify.DRIVE_CONFIG) in argv


# Doctor: healthy ----------------------------------------------------------------------------------------------------
def test_a_healthy_instance_passes_every_check(instance):
    assert doctor.check_ready(HANDLE, client(HEALTHY)).ok
    assert doctor.check_loopback(HANDLE, LISTENING).ok
    assert doctor.check_isolation(instance, CLEAN_ENV).ok
    assert doctor.check_seed(client(SEEDED)).ok


# Doctor: not ready --------------------------------------------------------------------------------------------------
def test_not_ready_until_the_handle_says_so_and_both_servers_answer():
    starting = doctor.check_ready(dict(HANDLE, status="starting"), client(HEALTHY))
    assert not starting.ok and "'starting'" in starting.detail
    down = doctor.check_ready(HANDLE, client(dict(HEALTHY, **{"/ready": 503})))
    assert not down.ok and "HTTP 503" in down.detail
    not_ok = doctor.check_ready(HANDLE, client(dict(HEALTHY, **{"/health": {"ok": False}})))
    assert not not_ok.ok and "did not report ok" in not_ok.detail

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)
    gone = doctor.check_ready(HANDLE, httpx.Client(transport=httpx.MockTransport(refuse)))
    assert not gone.ok and "did not answer" in gone.detail


@pytest.mark.parametrize("handle", [dict(HANDLE, server="http://127.0.0.1:8100"),
                                    dict(HANDLE, web="https://tower.example.ts.net"),
                                    dict(HANDLE, server=f"http://{ANY}:{SERVER}")])
def test_unsafe_urls_are_never_contacted(tmp_path, monkeypatch, handle):
    requests = []

    def record(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={})
    real = httpx.Client
    monkeypatch.setattr(doctor.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(record)))
    monkeypatch.setattr(doctor, "instance_processes", lambda handle: [])
    (tmp_path / "instance.json").write_text(json.dumps(handle), encoding="utf-8")
    lines = []
    assert doctor.run(tmp_path, token="ho-synthetic", out=lines.append) == 1
    assert requests == []
    assert lines[0].startswith("FAIL ready: not contacted")
    assert lines[-1].startswith("FAIL seed: skipped")


@pytest.mark.parametrize("content", [None, "not json", "[1, 2]"])
def test_doctor_fails_without_a_readable_instance_handle(tmp_path, content):
    if content is not None:
        (tmp_path / "instance.json").write_text(content, encoding="utf-8")
    lines = []
    assert doctor.run(tmp_path, out=lines.append) == 1
    assert lines == [lines[0]] and lines[0].startswith("FAIL ready: no readable instance.json")


# Doctor: loopback ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("handle", "listeners", "why"), [
    (HANDLE, [(ANY, SERVER), ("127.0.0.1", WEB)], "beyond loopback"),
    (HANDLE, [*LISTENING, ("::", 50999)], "beyond loopback"),
    (dict(HANDLE, server=f"http://{ANY}:{SERVER}"), LISTENING, "is not http://127.0.0.1"),
    (dict(HANDLE, web="http://localhost"), LISTENING, "is not http://127.0.0.1"),
    (dict(HANDLE, web="http://127.0.0.1:8100"), LISTENING, "live port 8100"),
    (HANDLE, [("127.0.0.1", WEB)], f"does not listen on {SERVER}"),
    (HANDLE, [], "does not listen on"),
])
def test_loopback_fails_beyond_loopback_or_without_the_listeners(handle, listeners, why):
    check = doctor.check_loopback(handle, listeners)
    assert not check.ok
    assert why in check.detail


def test_loopback_fails_when_a_live_process_cannot_be_inspected():
    import psutil
    conn = Mock(laddr=Mock(ip="127.0.0.1", port=SERVER), status=psutil.CONN_LISTEN)
    parent = Mock(pid=1, **{"net_connections.return_value": [conn, Mock(laddr=Mock(ip="127.0.0.1", port=WEB),
                                                                       status=psutil.CONN_LISTEN)]})
    child = Mock(pid=2, **{"net_connections.side_effect": psutil.AccessDenied(2)})
    gone = Mock(pid=3, **{"net_connections.side_effect": psutil.NoSuchProcess(3)})
    assert doctor.live_listeners([parent, gone]) == LISTENING
    listeners = doctor.live_listeners([parent, child])
    check = doctor.check_loopback(HANDLE, listeners)
    assert not check.ok and "process 2's sockets could not be inspected (AccessDenied)" in check.detail


def test_listeners_work_with_psutil_5():
    import psutil
    old = Mock(spec=["pid", "connections"], pid=1, **{"connections.return_value": [
        Mock(laddr=Mock(ip="127.0.0.1", port=SERVER), status=psutil.CONN_LISTEN),
        Mock(laddr=Mock(ip="127.0.0.1", port=60000), status="ESTABLISHED")]})
    assert doctor.live_listeners([old]) == [("127.0.0.1", SERVER)]
    old.connections.assert_called_once_with(kind="inet")


# Doctor: isolation --------------------------------------------------------------------------------------------------
def test_isolation_fails_on_data_outside_the_instance_or_credentials(instance, tmp_path):
    leaked = doctor.check_isolation(instance, dict(CLEAN_ENV, GITHUB_TOKEN="x", OPENAI_API_KEY="y"))
    assert not leaked.ok and "GITHUB_TOKEN" in leaked.detail and "OPENAI_API_KEY" in leaked.detail
    uninspected = doctor.check_isolation(instance, None)
    assert not uninspected.ok and "could not be inspected" in uninspected.detail
    (instance / "config" / "harness.yaml").write_text(yaml.safe_dump(
        {"data_dir": (tmp_path / "real-data").as_posix(), "repos_dir": (instance / "repos").as_posix()}),
        encoding="utf-8")
    outside = doctor.check_isolation(instance, CLEAN_ENV)
    assert not outside.ok and "data_dir" in outside.detail and "repos_dir" not in outside.detail
    (instance / "config" / "harness.yaml").unlink()
    unreadable = doctor.check_isolation(instance, CLEAN_ENV)
    assert not unreadable.ok and "could not be read" in unreadable.detail
    (instance / "data" / "fake-model-instance").unlink()
    unmarked = doctor.check_isolation(instance, CLEAN_ENV)
    assert not unmarked.ok and "marker" in unmarked.detail


def test_isolation_refuses_the_checkout_itself():
    check = doctor.check_isolation(ROOT, CLEAN_ENV)
    assert not check.ok and "this checkout" in check.detail


# Doctor: seed -------------------------------------------------------------------------------------------------------
def test_seed_fails_when_a_row_is_missing_or_unsettled():
    sessions = [dict(s) for s in SEEDED["/api/v1/sessions"] if s["title"] != "Tool rows"]
    sessions[0]["status"] = "running"
    check = doctor.check_seed(client(dict(SEEDED, **{"/api/v1/sessions": sessions, "/api/admin/v1/jobs": 502})))
    assert not check.ok
    assert "'Tool rows' (missing, want done)" in check.detail
    assert f"{sessions[0]['title']!r} (running" in check.detail
    assert "job 'Nightly report'" in check.detail and "job 'Weekly cleanup'" in check.detail
    assert "chat" not in check.detail


def test_doctor_reads_the_seed_only_with_the_owner_token(instance, monkeypatch):
    (instance / "instance.json").write_text(json.dumps(HANDLE), encoding="utf-8")
    monkeypatch.setattr(doctor, "check_ready", lambda handle, http: doctor.Check("ready", True, "stand-in"))
    monkeypatch.setattr(doctor, "instance_processes", lambda handle: ["proc"])
    monkeypatch.setattr(doctor, "live_listeners", lambda procs: LISTENING)
    monkeypatch.setattr(doctor, "live_environment", lambda procs: CLEAN_ENV)
    lines = []
    assert doctor.run(instance, out=lines.append) == 1
    assert lines[-1].startswith("FAIL seed: no owner token") and doctor.TOKEN_ENV in lines[-1]
    seen = []
    monkeypatch.setattr(doctor, "owner_client", lambda server, token: seen.append(token) or client(SEEDED))
    lines.clear()
    assert doctor.run(instance, token="ho-synthetic", out=lines.append) == 0
    assert seen == ["ho-synthetic"] and all(line.startswith("PASS") for line in lines)


# Tokens and environment ---------------------------------------------------------------------------------------------
def test_the_instance_gets_no_credentials(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("HARNESS_FAKE_MODEL", "elsewhere")
    monkeypatch.setenv("HARNESS_STABLES_DIR", "stables")
    env = verify.instance_env()
    assert "GH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env and "HARNESS_FAKE_MODEL" not in env
    assert env["HARNESS_STABLES_DIR"] == "stables" and env["PYTHONUTF8"] == "1"


def test_tokens_never_reach_the_evidence(tmp_path):
    run = verify.Run(tmp_path / "evidence")
    run.evidence.mkdir()
    stdout = [b"Agent Harness fake model instance is up\n", b"  Owner token:      ho-abcdefghijklmnop\n",
              b"  Local token:      LocalSecret123   (header X-Agent-Harness-Local-Token)\n", b'READY {"server": 1}\n']
    run.read_launcher(Mock(stdout=stdout))
    assert run.tokens == ["ho-abcdefghijklmnop", "LocalSecret123"] and run.owner_token() == "ho-abcdefghijklmnop"
    assert run.ready_line == 'READY {"server": 1}'
    launcher = (run.evidence / "launcher.txt").read_text(encoding="utf-8")
    assert "ho-abcdefghijklmnop" not in launcher and "LocalSecret123" not in launcher and "[REDACTED]" in launcher
    (run.evidence / "logs").mkdir()
    (run.evidence / "logs" / "server.log").write_text("local LocalSecret123; Bearer ho-abcdefghijklmnop\n",
                                                      encoding="utf-8")
    (run.evidence / "playwright").mkdir()
    trace = run.evidence / "playwright" / "trace.zip"
    with zipfile.ZipFile(trace, "w") as z:
        z.writestr("trace.network", '{"authorization": "Bearer abc.def", "html": "ho-abcdefghijklmnop"}')
        z.writestr("frame.jpeg", b"\xff\xd8\xff\xe0binary")
    run.redact()
    log = (run.evidence / "logs" / "server.log").read_text(encoding="utf-8")
    assert log == "local [REDACTED]; Bearer [REDACTED]\n"
    with zipfile.ZipFile(trace) as z:
        assert z.read("trace.network").decode() == '{"authorization": "Bearer [REDACTED]", "html": "[REDACTED]"}'
        assert z.read("frame.jpeg") == b"\xff\xd8\xff\xe0binary"


def test_launch_names_a_missing_stables_shell_before_starting_anything(tmp_path, monkeypatch):
    from harness import stables_shell
    monkeypatch.setattr(stables_shell, "locate", lambda: None)
    run = verify.Run(tmp_path / "evidence")
    run.contained.popen = Mock(side_effect=AssertionError("started"))
    assert not run.launch()
    assert "HARNESS_STABLES_DIR" in run.steps[0][2] and "instance_dir" not in run.record


def test_launch_names_the_playwright_setup(tmp_path, monkeypatch):
    from harness import stables_shell
    monkeypatch.setattr(stables_shell, "locate", lambda: tmp_path)
    monkeypatch.setattr(verify, "PLAYWRIGHT_CLI", tmp_path / "missing-cli.js")
    problem = verify.Run(tmp_path / "evidence").prerequisite_problem()
    assert "npm --prefix tools/ui-tests ci" in problem


# Cleanup after a failed step ----------------------------------------------------------------------------------------
STAND_IN = """
import json, os, socket, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, {repo!r})
from harness import fake_instance, fake_model
if sys.argv[1] == "stop":
    sys.exit(fake_instance.main(sys.argv[1:]))  # the real Teardown
root = Path(sys.argv[sys.argv.index("--dir") + 1])
(root / "data").mkdir(parents=True, exist_ok=True)
(root / "data" / fake_model.MARKER).write_text("stand-in", encoding="utf-8")
ports = []
for _ in range(2):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        ports.append(s.getsockname()[1])
# A child listening for the server, as uvicorn would; the real Teardown stops children too.
server = subprocess.Popen([sys.executable, "-c", "import socket, time; s = socket.socket(); "
                           f"s.bind(('127.0.0.1', {{ports[0]}})); s.listen(); time.sleep(600)"])
import psutil
state = {{"status": "ready", "pid": os.getpid(), "pid_created": psutil.Process().create_time(), "dir": str(root),
          "server": f"http://127.0.0.1:{{ports[0]}}", "web": f"http://127.0.0.1:{{ports[1]}}"}}
state["connect"] = state["web"] + "/connect"
(root / "instance.json").write_text(json.dumps(state), encoding="utf-8")
for _ in range(200):
    try:
        socket.create_connection(("127.0.0.1", ports[0]), timeout=1).close()
        break
    except OSError:
        time.sleep(0.05)
print("  Owner token:      ho-standinstandin1234", flush=True)
print("READY " + json.dumps({{k: state[k] for k in ("server", "web", "connect", "dir", "pid")}}), flush=True)
time.sleep(600)
"""

FAILING_STEP = """
import pathlib, subprocess, sys
# A browser the step leaves behind, then it fails at once: no sampling of its tree can see the orphan.
orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"],
                          start_new_session=sys.argv[2] == "own-session")
pathlib.Path(sys.argv[1]).write_text(str(orphan.pid), encoding="utf-8")
sys.exit(3)
"""


def stand_in_run(tmp_path, monkeypatch):
    stand_in = tmp_path / "stand-in" / "harness.fake_instance.py"  # Teardown only stops a harness.fake_instance
    stand_in.parent.mkdir()
    stand_in.write_text(STAND_IN.format(repo=str(ROOT)), encoding="utf-8")
    monkeypatch.setattr(verify.tempfile, "gettempdir", lambda: str(tmp_path))
    run = verify.Run(tmp_path / "evidence", launcher=(str(stand_in),))
    run.prerequisite_problem = lambda: None
    return run


def wait_gone(entries):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(verify.alive(p) for p in entries):
        time.sleep(0.2)
    return [p for p in entries if verify.alive(p)]


def pid_running(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def wait_pid_gone(pid: int, seconds: float = 10) -> bool:
    """Whether `pid` still runs after up to `seconds`. A process just killed can outlive the kill for a moment (on
    Windows), so this waits as wait_gone does for the recorded processes."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and pid_running(pid):
        time.sleep(0.2)
    return pid_running(pid)


def test_the_orphan_check_waits_for_a_process_that_exits_late():
    late = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])  # exits about a second from now
    try:
        assert not wait_pid_gone(late.pid)
    finally:
        late.wait()


def test_the_orphan_check_still_fails_for_a_process_that_stays_alive():
    stays = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert wait_pid_gone(stays.pid, seconds=0.5)
    finally:
        stays.kill()
        stays.wait()


@pytest.mark.parametrize("session", ["same-session", "own-session"])
def test_cleanup_after_a_failed_drive_leaves_nothing_running(tmp_path, monkeypatch, session):
    pytest.importorskip("psutil")
    run = stand_in_run(tmp_path, monkeypatch)
    run.doctor = lambda: run.step("doctor", True, "stand-in")
    orphan_file = tmp_path / "orphan.pid"
    run.drive_argv = lambda features: [sys.executable, "-c", FAILING_STEP, str(orphan_file), session]

    assert run.run(["task"]) == 1

    assert {name: ok for name, ok, _ in run.steps} == {"launch": True, "doctor": True, "drive": False,
                                                        "cleanup": True}
    record = json.loads((tmp_path / "evidence" / "launch.json").read_text(encoding="utf-8"))
    assert {"instance", "drive"} <= {p["name"] for p in record["processes"]}
    orphan = int(orphan_file.read_text(encoding="utf-8"))
    assert not wait_pid_gone(orphan)
    assert not wait_gone(record["processes"])
    assert not Path(record["instance_dir"]).exists()
    for port in (record["server_port"], record["web_port"]):
        with socket.socket() as s:
            assert s.connect_ex(("127.0.0.1", port)) != 0
    evidence = tmp_path / "evidence"
    summary = (evidence / "summary.md").read_text(encoding="utf-8")
    assert "# Verify: FAIL" in summary and "| drive | FAIL |" in summary and "| cleanup | PASS |" in summary
    assert "deleted" in (evidence / "cleanup.txt").read_text(encoding="utf-8")
    assert not [p for p in evidence.rglob("*") if p.is_file() and b"ho-standin" in p.read_bytes()]
    assert verify.main(["cleanup", "--evidence", str(evidence)]) == 0  # safe to repeat


TEMP_WRITING_STEP = """
import pathlib, sys, tempfile
# A browser profile in the system temp dir, as Playwright makes one; the step dies before it can remove it.
profile = pathlib.Path(tempfile.mkdtemp(prefix="playwright_chromiumdev_profile-"))
(profile / "Cookies").write_text("stand-in", encoding="utf-8")
pathlib.Path(sys.argv[1]).write_text(str(profile), encoding="utf-8")
sys.exit(3)
"""


def test_the_drive_keeps_its_temp_files_in_the_run_and_cleanup_removes_them(tmp_path, monkeypatch):
    run = stand_in_run(tmp_path, monkeypatch)
    run.doctor = lambda: run.step("doctor", True, "stand-in")
    profile_file = tmp_path / "profile.path"
    run.drive_argv = lambda features: [sys.executable, "-c", TEMP_WRITING_STEP, str(profile_file)]

    assert run.run([]) == 1

    record = json.loads((tmp_path / "evidence" / "launch.json").read_text(encoding="utf-8"))
    scratch = Path(record["scratch_dir"])
    profile = Path(profile_file.read_text(encoding="utf-8"))
    assert scratch in profile.parents  # not the system temp dir, which nothing records
    assert not scratch.exists() and not profile.exists()
    assert run.steps[-1][:2] == ("cleanup", True)


def test_recovery_fails_when_the_runs_temp_files_remain(tmp_path, monkeypatch):
    evidence = tmp_path / "evidence"
    scratch = tmp_path / "harness-verify-tmp-x"
    (scratch / "playwright-artifacts-x").mkdir(parents=True)
    evidence.mkdir()
    (evidence / "launch.json").write_text(json.dumps({"evidence": str(evidence), "processes": [],
                                                      "scratch_dir": str(scratch)}), encoding="utf-8")
    with monkeypatch.context() as held:
        held.setattr(verify.shutil, "rmtree", lambda *a, **kw: None)  # a handle the killed browser still holds
        held.setattr(verify.time, "sleep", lambda seconds: None)
        assert verify.main(["cleanup", "--evidence", str(evidence)]) == 1
    assert f"{scratch} still exists" in (evidence / "cleanup.txt").read_text(encoding="utf-8")

    assert verify.main(["cleanup", "--evidence", str(evidence)]) == 0
    assert not scratch.exists()


def test_cleanup_after_a_failed_doctor_skips_the_drive(tmp_path, monkeypatch):
    run = stand_in_run(tmp_path, monkeypatch)
    # The real doctor: the stand-in's listener speaks no HTTP, so `ready` fails.
    run.drive = Mock(side_effect=AssertionError("drove"))

    assert run.run([]) == 1

    steps = {name: ok for name, ok, _ in run.steps}
    assert steps == {"launch": True, "doctor": False, "cleanup": True}
    doctor_txt = (tmp_path / "evidence" / "doctor.txt").read_text(encoding="utf-8")
    assert doctor_txt.startswith("FAIL ready:") and "FAIL seed: skipped" in doctor_txt
    assert not wait_gone(run.record["processes"])
    assert not Path(run.record["instance_dir"]).exists()
    assert "| doctor | FAIL |" in (tmp_path / "evidence" / "summary.md").read_text(encoding="utf-8")


def test_a_failed_launch_still_cleans_up(tmp_path, monkeypatch):
    run = stand_in_run(tmp_path, monkeypatch)
    run.launcher = ["-c", "import sys; print('FAILED a port is in use', file=sys.stderr); sys.exit(1)"]
    real = Mock(python=sys.executable, launcher=list(verify.LAUNCHER))  # the real Teardown removes the empty dir
    run.teardown = lambda instance: verify.Run.teardown(real, instance)
    assert run.run([]) == 1
    assert run.steps[0][0] == "launch" and "FAILED a port is in use" in run.steps[0][2]
    assert run.steps[-1][:2] == ("cleanup", True)
    assert not Path(run.record["instance_dir"]).exists()


def test_each_run_gets_its_own_instance_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(verify.tempfile, "gettempdir", lambda: str(tmp_path))
    exited = Mock(pid=0, returncode=2, stdout=[], **{"poll.return_value": 2})
    dirs = []
    for parent in ("run-a", "run-b"):  # the same evidence basename under different parents
        run = verify.Run(tmp_path / parent / "evidence")
        run.prerequisite_problem = lambda: None
        run.contained.popen = lambda argv, **kw: exited
        run.track = run.track_tree = lambda name, pid: None
        assert not run.launch()
        dirs.append(run.record["instance_dir"])
    assert dirs[0] != dirs[1]


def test_an_interrupted_run_is_a_failure_and_still_cleans_up(tmp_path):
    run = verify.Run(tmp_path / "evidence")
    run.launch = lambda: run.step("launch", True, "stand-in")
    run.doctor = lambda: run.step("doctor", True, "stand-in")

    def interrupt(features):
        raise KeyboardInterrupt
    run.drive = interrupt
    cleaned = []
    run.cleanup = lambda: cleaned.append(True) or run.step("cleanup", True, "stand-in")

    assert run.run([]) == 1
    assert cleaned
    summary = (tmp_path / "evidence" / "summary.md").read_text(encoding="utf-8")
    assert "# Verify: FAIL" in summary and "| interrupted | FAIL |" in summary


def test_keep_holds_the_instance_until_ctrl_c_then_cleans_up(tmp_path):
    run = verify.Run(tmp_path / "evidence")
    run.record["connect"] = "http://127.0.0.1:50102/connect"
    run.launch = lambda: run.step("launch", True, "stand-in")
    run.doctor = lambda: run.step("doctor", True, "stand-in")
    run.drive = lambda features: run.step("drive", True, "stand-in")
    order = []
    run.hold = lambda: order.append("hold")
    run.cleanup = lambda: order.append("cleanup") or run.step("cleanup", True, "stand-in")
    assert run.run([], keep=True) == 0
    assert order == ["hold", "cleanup"]


def test_hold_returns_on_ctrl_c(tmp_path, monkeypatch, capsys):
    run = verify.Run(tmp_path / "evidence")
    run.record.update(connect="http://127.0.0.1:50102/connect", processes=[{"name": "instance", "pid": 1}])
    monkeypatch.setattr(verify, "alive", lambda entry: object())
    monkeypatch.setattr(verify.time, "sleep", Mock(side_effect=KeyboardInterrupt))
    run.hold()
    assert "kept: open http://127.0.0.1:50102/connect" in capsys.readouterr().out


def test_a_run_missing_a_step_does_not_pass(tmp_path):
    run = verify.Run(tmp_path / "evidence")
    for name in ("launch", "doctor", "cleanup"):
        run.step(name, True, "")
    assert not run.passed()
    run.step("drive", True, "")
    assert run.passed()


def test_ctrl_c_during_cleanup_does_not_stop_it(tmp_path):
    import signal
    run = verify.Run(tmp_path / "evidence")
    run.launch = lambda: run.step("launch", False, "stand-in")
    finished = []

    def cleanup():
        signal.raise_signal(signal.SIGINT)  # Ctrl+C while Teardown waits
        finished.append(True)
        return run.step("cleanup", True, "stand-in")
    run.cleanup = cleanup
    assert run.run([]) == 1
    assert finished
    assert (tmp_path / "evidence" / "summary.md").is_file()
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_the_summary_marks_the_seeded_failure_as_expected(tmp_path):
    run = verify.Run(tmp_path / "evidence")
    (run.evidence / "logs").mkdir(parents=True)
    (run.evidence / "logs" / "server.log").write_text(
        "ERROR harness.runner: session x crashed\nTraceback (most recent call last):\n"
        f"harness.llm.LLMError: HTTP 400: {verify.SEEDED_FAILURE}\n", encoding="utf-8")
    run.doctor_lines = ["PASS ready: stand-in"]
    for name in verify.STEPS:
        run.step(name, True, "a | b")
    summary = run.summary(["task"]).read_text(encoding="utf-8")
    assert "# Verify: PASS" in summary and "smoke, task" in summary and "| launch | PASS | a / b |" in summary
    assert "- PASS ready: stand-in" in summary and "expected: the seeded Failed run" in summary
    assert "head `" in summary or "head unknown" in summary


# POSIX containment --------------------------------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform == "win32", reason="the run token: the POSIX containment")
def test_a_process_without_the_run_token_is_never_touched():
    pytest.importorskip("psutil")
    import subprocess
    # Not this run's: not its descendant (no subreaper here) and without its token, like a process that reused an id.
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        contained = verify.Containment()
        contained.reaper = False
        assert stranger.pid not in {p.pid for p in contained._members()}
        assert contained.kill() == 0
        assert stranger.poll() is None
    finally:
        stranger.kill()
        stranger.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="the run token: the POSIX containment")
def test_an_orphan_outside_the_tree_and_group_is_found_by_the_run_token(tmp_path):
    psutil = pytest.importorskip("psutil")
    contained = verify.Containment()
    contained.reaper = False  # as on macOS
    orphan_file = tmp_path / "orphan.pid"
    step = contained.popen([sys.executable, "-c", FAILING_STEP, str(orphan_file), "own-session"])
    step.wait(timeout=30)
    orphan = int(orphan_file.read_text(encoding="utf-8"))
    assert contained.kill() >= 1  # not a descendant any more: only the token finds it
    assert not psutil.pid_exists(orphan) or psutil.Process(orphan).status() == psutil.STATUS_ZOMBIE
