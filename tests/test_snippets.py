"""Chat snippet runner (#85): explicit owner runs in isolated, throwaway, limited sandboxes."""

from __future__ import annotations

import asyncio
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from harness import snippets
from harness.config import GuestAccess
from harness.llm import Completion
from harness.manager import Manager
from harness.runner import Runner
from harness.snippets import (LANGUAGES, LIMITS, SnippetRunner, SnippetService, _Budget, _exec_blocking,
                              _parse_stats, container_args, java_names, new_result)

from test_api import LOGIN, make_client, wait_for

OWNER = {"Tailscale-User-Login": LOGIN}


class FakeRunner:
    """Stands in for Docker: records calls and returns a canned result, or waits for cancel when `block` is set."""

    def __init__(self, block: bool = False):
        self.languages = LANGUAGES
        self.calls: list[tuple[str, str]] = []
        self.block = block

    async def run(self, run_id, language, source, cancel: threading.Event) -> dict:
        self.calls.append((language, source))
        result = new_result(run_id, LANGUAGES[language])
        result["toolchain"]["version"] = "Fake 1.0"
        if self.block:
            while not cancel.is_set():
                await asyncio.sleep(0.02)
            result.update(reasons=["cancelled"], status="cancelled")
            return result
        result["run"] = {"exit_code": 0, "stdout": "<b>hi</b>\n", "stderr": "", "duration_ms": 5}
        result["status"] = "completed"
        return result


def chat_client(tmp_path, steps=None, block=False):
    client, m, _ = make_client(tmp_path, steps or [Completion(content="ok")])
    fake = FakeRunner(block=block)
    m.snippets.runner = fake
    return client, m, fake


def new_chat(client, m):
    chat = client.post("/chats", json={"prompt": "hello"}, headers=OWNER).json()
    wait_for(lambda: client.get(f"/chats/{chat['id']}").json()["status"] == "done")
    return chat["id"]


def snippet_events(m, sid):
    return [e for e in m.db.events(sid) if e["type"].startswith("snippet_")]


# ---------- isolation contract (no Docker needed) ----------

def test_container_args_are_the_isolation_checklist():
    for lang in LANGUAGES.values():
        args = container_args(lang, "sn-abc")
        joined = " ".join(args)
        for flag in ("--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
                     "--user 65534:65534", "--pull never", "--rm", "--init", "--memory 1g", "--memory-swap 1g",
                     "--cpus 1", "--pids-limit 64", "--shm-size 8m", "--log-driver none"):
            assert flag in joined, (lang.id, flag)
        assert "--tmpfs /sandbox:rw,exec,nosuid,nodev,size=120m,uid=65534,gid=65534,mode=0700" in joined
        for banned in ("-v", "--volume", "--mount", "--privileged", "--cap-add", "-p", "--publish", "--device",
                       "--env-file", "--ipc", "--pid", "--userns", "--security-opt=seccomp=unconfined"):
            assert banned not in args, (lang.id, banned)
        assert "docker.sock" not in joined
        assert "/workspace" not in joined
        assert "host" not in args
        assert args[-4:] == ["--entrypoint", "sleep", lang.image, str(snippets.LIFETIME_SECONDS)]
    assert LIMITS == {"timeout_seconds": 30, "cpus": 1, "memory_mib": 1024, "pids": 64, "tmp_mib": 128,
                      "output_bytes": 1 << 20}


def test_toolchains_are_pinned_by_digest_with_fixed_commands():
    assert set(LANGUAGES) == {"python", "javascript", "java", "csharp", "cpp"}
    for lang in LANGUAGES.values():
        assert re.fullmatch(r"[a-z0-9./-]+@sha256:[0-9a-f]{64}", lang.image), lang.image
        assert lang.version
        assert lang.run
        assert lang.tag
    assert not LANGUAGES["python"].compile
    assert not LANGUAGES["javascript"].compile
    for lid in ("java", "csharp", "cpp"):
        assert LANGUAGES[lid].compile.startswith("exec 2>&1;"), lid  # diagnostics are one stream, apart from run


def test_java_file_and_main_class():
    assert java_names("public class Hello { public static void main(String[] a) {} }") == ("Hello.java", "Hello")
    assert java_names("import java.util.*;\n\npublic final class App {}\n") == ("App.java", "App")
    assert java_names("class A {}\nclass B { public static void main(String[] a) {} }") == ("Main.java", "B")
    assert java_names('void main() { IO.println("hi"); }\nclass Helper {}') == ("Main.java", "Main")
    assert java_names("class Outer {\n    public class Inner {}\n    static void main(String[] a) {}\n}") == (
        "Main.java", "Outer")
    name, main = java_names("public class x$; rm -rf / {}")
    assert re.fullmatch(r"\w+\.java", name)
    assert re.fullmatch(r"\w+", main)


def test_parse_stats_reads_cgroup_evidence():
    out = "[memory]\nlow 0\nmax 12\noom 1\noom_kill 1\n[pids]\nmax 3\n[tmp]\ntmpfs 122880 122000 880 100% /sandbox\n"
    assert _parse_stats(out) == ["memory_limit", "pids_limit", "temp_storage_limit"]
    calm = "[memory]\nmax 5\noom_kill 0\n[pids]\nmax 0\n[tmp]\ntmpfs 122880 10 122870 1% /sandbox\n"
    assert _parse_stats(calm) == []  # memory.events "max" (reclaim) is not the pids limit


def test_capture_enforces_output_budget_timeout_and_cancel():
    stops = []
    budget = _Budget(1000)
    code, out, err, reason = _exec_blocking(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 50000); sys.stderr.write('e' * 50000)"],
        None, time.monotonic() + 20, budget, threading.Event(), lambda: stops.append(1))
    assert reason == "output_limit"
    assert code is None
    assert budget.over
    assert stops
    assert len(out) + len(err) == 1000

    stops.clear()
    t0 = time.monotonic()
    code, out, err, reason = _exec_blocking(
        [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"],
        None, time.monotonic() + 1, _Budget(1000), threading.Event(), lambda: stops.append(1))
    assert reason == "timeout"
    assert code is None
    assert stops
    assert time.monotonic() - t0 < 10

    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    code, out, err, reason = _exec_blocking(
        [sys.executable, "-c", "import time; time.sleep(30)"], None, time.monotonic() + 20, _Budget(1000), cancel,
        lambda: None)
    assert reason == "cancelled"
    assert code is None

    code, out, err, reason = _exec_blocking(
        [sys.executable, "-c", "import sys; data = sys.stdin.read(); print(len(data)); sys.exit(4)"],
        b"abc" * 10, time.monotonic() + 20, _Budget(1000), threading.Event(), lambda: None)
    assert (code, out.strip(), reason) == (4, b"30", "")


class FakeDocker:
    """run_cmd plus SnippetRunner._exec stand-ins that script each docker step."""

    def __init__(self, create=(0, "cid", ""), steps=None):
        self.create = create
        self.steps = list(steps or [])
        self.commands: list[list[str]] = []
        self.execs: list[tuple[str, str, bytes | None]] = []

    async def run_cmd(self, args, timeout=60, input_=None, env=None):
        self.commands.append(args)
        if args[:2] == ["docker", "run"]:
            return self.create
        return 0, "", ""

    def patch(self, monkeypatch, runner):
        monkeypatch.setattr(snippets, "run_cmd", self.run_cmd)

        async def fake_exec(name, script, arg, *, stdin=None, deadline, budget, cancel):
            self.execs.append((script, arg, stdin))
            step = self.steps.pop(0) if self.steps else (0, b"", b"", "")
            code, out, err, reason = step
            budget.take(len(out) + len(err))
            return code, out, err, reason
        runner._exec = fake_exec


def test_runner_reports_missing_image_and_always_removes_the_container(monkeypatch):
    runner = SnippetRunner()
    docker = FakeDocker(create=(125, "", "Unable to find image 'gcc@sha256:ead1' locally\nNo such image"))
    docker.patch(monkeypatch, runner)
    result = asyncio.run(runner.run("sn-1", "cpp", "int main(){}", threading.Event()))
    assert result["status"] == "error"
    assert "python -m harness.snippets pull" in result["error"]
    assert ["docker", "rm", "-f", "harness-snippet-sn-1"] in docker.commands


def test_runner_keeps_compile_diagnostics_apart_from_runtime_output(monkeypatch):
    runner = SnippetRunner()
    docker = FakeDocker(steps=[(0, b"g++ (GCC) 15.2.0\n", b"", ""),
                               (1, b"main.cpp:1:1: error: expected ';'\n", b"", "")])
    docker.patch(monkeypatch, runner)
    result = asyncio.run(runner.run("sn-2", "cpp", "int main() { return 0 }", threading.Event()))
    assert result["status"] == "compile_failed"
    assert result["run"] is None
    assert result["compile"]["exit_code"] == 1
    assert "expected ';'" in result["compile"]["output"]
    assert result["toolchain"] == {"image": "gcc:15", "digest": LANGUAGES["cpp"].image, "version": "g++ (GCC) 15.2.0"}
    setup, filename, stdin = docker.execs[0]
    assert filename == "main.cpp"
    assert stdin == b"int main() { return 0 }"
    assert "g++ --version" in setup
    assert docker.execs[1][0] == LANGUAGES["cpp"].compile

    docker = FakeDocker(steps=[(0, b"g++ (GCC) 15.2.0\n", b"", ""), (0, b"", b"", ""),
                               (2, b"out\n", b"boom\n", ""), (0, b"[memory]\noom_kill 0\n", b"", "")])
    docker.patch(monkeypatch, runner)
    result = asyncio.run(runner.run("sn-3", "cpp", "int main() { return 2; }", threading.Event()))
    assert result["status"] == "failed"
    assert result["compile"]["exit_code"] == 0
    assert result["run"]["exit_code"] == 2
    assert result["run"]["stdout"] == "out\n"
    assert result["run"]["stderr"] == "boom\n"
    assert ["docker", "rm", "-f", "harness-snippet-sn-3"] in docker.commands


def test_runner_passes_java_main_class_and_reports_limits(monkeypatch):
    runner = SnippetRunner()
    docker = FakeDocker(steps=[(0, b'openjdk version "25"\n', b"", ""), (0, b"", b"", ""),
                               (137, b"", b"", ""), (0, b"[memory]\noom_kill 1\n[pids]\nmax 0\n", b"", "")])
    docker.patch(monkeypatch, runner)
    result = asyncio.run(runner.run("sn-4", "java", "public class Big { public static void main(String[] a) {} }",
                                    threading.Event()))
    assert [e[1] for e in docker.execs] == ["Big.java", "Big.java", "Big", ""]
    assert result["status"] == "limit_exceeded"
    assert result["reasons"] == ["memory_limit"]

    docker = FakeDocker(steps=[(0, b"Python 3.12\n", b"", ""), (None, b"x" * 10, b"", "timeout")])
    docker.patch(monkeypatch, runner)
    result = asyncio.run(runner.run("sn-5", "python", "while True: pass", threading.Event()))
    assert result["status"] == "timeout"
    assert result["reasons"] == ["timeout"]
    assert result["compile"] is None


# ---------- API, transcript, access ----------

def test_owner_runs_snippet_and_result_survives_reload(tmp_path):
    client, m, fake = chat_client(tmp_path)
    with client:
        sid = new_chat(client, m)
        r = client.post(f"/chats/{sid}/snippets", json={"language": "python", "source": "print('hi')",
                                                         "origin": "block"}, headers=OWNER)
        assert r.status_code == 202
        assert r.json()["status"] == "running"
        run_id = r.json()["id"]
        wait_for(lambda: len(snippet_events(m, sid)) == 2)
        started, result = snippet_events(m, sid)
        assert started["data"] == {"id": run_id, "language": "python", "label": "Python", "toolchain": "python:3.12-slim",
                                   "source": "print('hi')", "origin": "block"}
        assert result["data"]["id"] == run_id
        assert result["data"]["status"] == "completed"
        assert result["data"]["toolchain"]["version"] == "Fake 1.0"
        assert fake.calls == [("python", "print('hi')")]
        replay = client.get(f"/chats/{sid}/events", params={"follow": "false"}, headers=OWNER).text
        assert "snippet_started" in replay
        assert "snippet_result" in replay
        assert "print('hi')" in replay
        langs = client.get("/chats/snippet-languages", headers=OWNER).json()
        assert [x["id"] for x in langs["languages"]] == list(LANGUAGES)
        assert langs["limits"] == LIMITS
        from harness import transcript
        text = transcript.render(m.db, sid)
        assert "print('hi')" in text
        assert "completed" in text


def test_snippet_request_accepts_no_flags_images_or_unknown_languages(tmp_path):
    client, m, fake = chat_client(tmp_path)
    with client:
        sid = new_chat(client, m)
        url = f"/chats/{sid}/snippets"
        for extra in ({"flags": "-O3"}, {"args": ["x"]}, {"image": "alpine"}, {"packages": ["numpy"]},
                      {"compiler": "clang"}, {"files": {"a": "b"}}):
            body = {"language": "python", "source": "print(1)", **extra}
            assert client.post(url, json=body, headers=OWNER).status_code == 422, extra
        assert client.post(url, json={"language": "ruby", "source": "puts 1"}, headers=OWNER).status_code == 400
        assert client.post(url, json={"language": "py", "source": "print(1)"}, headers=OWNER).status_code == 400
        assert client.post(url, json={"language": "python", "source": "  \n"}, headers=OWNER).status_code == 400
        big = "x" * (snippets.SOURCE_BYTES + 1)
        assert client.post(url, json={"language": "python", "source": big}, headers=OWNER).status_code == 413
        assert fake.calls == []
        assert snippet_events(m, sid) == []


def test_sending_code_never_runs_it_and_the_model_has_no_runner_tool(tmp_path):
    client, m, fake = chat_client(tmp_path, [Completion(content="Looks fine.")])
    with client:
        code = "```python\nimport os\nos.system('echo pwned')\n```"
        chat = client.post("/chats", json={"prompt": code}, headers=OWNER).json()
        wait_for(lambda: client.get(f"/chats/{chat['id']}").json()["status"] == "done")
        client.post(f"/chats/{chat['id']}/messages", json={"content": "run this " + code}, headers=OWNER)
        wait_for(lambda: client.get(f"/chats/{chat['id']}").json()["status"] == "done")
        s = m.db.get_session(chat["id"])
        names = {t["function"]["name"] for t in Runner.tool_schemas(m.runner, s, None)}
        assert not any("snippet" in n or "run" in n for n in names)
        assert fake.calls == []
        assert snippet_events(m, chat["id"]) == []


def test_guests_and_other_routes_cannot_run_snippets(tmp_path):
    client, m, fake = chat_client(tmp_path)
    guest = "buddy@example.com"
    m.cfg.guests = [GuestAccess(login=guest, until=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat())]
    gh = {"Tailscale-User-Login": guest}
    with client:
        sid = new_chat(client, m)
        body = {"language": "python", "source": "print(1)"}
        assert client.post(f"/chats/{sid}/snippets", json=body, headers=gh).status_code in (403, 404)
        assert client.get("/chats/snippet-languages", headers=gh).status_code in (401, 403)
        assert client.get(f"/chats/{sid}/events", params={"follow": "false"}, headers=gh).status_code in (403, 404)
        agent = client.post("/sessions", json={"prompt": "agent task"}, headers=OWNER).json()
        assert client.post(f"/chats/{agent['id']}/snippets", json=body, headers=OWNER).status_code == 404
        assert fake.calls == []


def test_owner_api_prefix_and_tokens(tmp_path):
    from harness.admin import ADMIN_SCOPE, OWNER_KIND, PREFIX
    client, m, fake = chat_client(tmp_path)
    with client:
        sid = new_chat(client, m)
        body = {"language": "javascript", "source": "console.log(1)"}
        app = client.post("/keys", json={"name": "shop", "kind": "app", "scopes": ["sessions", "sessions:all"]}).json()
        device = client.post("/keys", json={"name": "zed"}).json()
        owner = client.post("/keys", json={"name": "cc", "kind": OWNER_KIND, "scopes": [ADMIN_SCOPE]}).json()
        for key in (app["key"], device["key"]):
            auth = {"Authorization": f"Bearer {key}"}
            for path in (f"/chats/{sid}/snippets", f"{PREFIX}/chats/{sid}/snippets"):
                assert client.post(path, json=body, headers=auth).status_code in (401, 403, 404), (key[:3], path)
        assert fake.calls == []
        r = client.post(f"{PREFIX}/chats/{sid}/snippets", json=body, headers={"Authorization": f"Bearer {owner['key']}"})
        assert r.status_code == 202, r.text
        wait_for(lambda: len(snippet_events(m, sid)) == 2)
        assert client.get(f"{PREFIX}/chats/snippet-languages", headers=OWNER).status_code == 200
        assert fake.calls == [("javascript", "console.log(1)")]


def test_cancel_one_run_per_chat_and_delete_guard(tmp_path):
    client, m, fake = chat_client(tmp_path, block=True)
    with client:
        sid = new_chat(client, m)
        body = {"language": "cpp", "source": "int main(){for(;;);}"}
        run_id = client.post(f"/chats/{sid}/snippets", json=body, headers=OWNER).json()["id"]
        assert client.post(f"/chats/{sid}/snippets", json=body, headers=OWNER).status_code == 409
        assert client.delete(f"/chats/{sid}", headers=OWNER).status_code == 409
        assert client.post(f"/chats/{sid}/snippets/sn-nope/cancel", headers=OWNER).status_code == 409
        r = client.post(f"/chats/{sid}/snippets/{run_id}/cancel", headers=OWNER)
        assert r.status_code == 200
        assert r.json()["status"] == "cancelling"
        wait_for(lambda: len(snippet_events(m, sid)) == 2)
        assert snippet_events(m, sid)[1]["data"]["status"] == "cancelled"
        assert client.delete(f"/chats/{sid}", headers=OWNER).status_code == 200


def test_global_concurrency_cap(tmp_path):
    client, m, fake = chat_client(tmp_path, block=True)
    with client:
        sids = [new_chat(client, m) for _ in range(3)]
        body = {"language": "python", "source": "x"}
        ids = [client.post(f"/chats/{sid}/snippets", json=body, headers=OWNER) for sid in sids]
        assert [r.status_code for r in ids] == [202, 202, 429]
        for sid, r in zip(sids, ids[:2]):
            client.post(f"/chats/{sid}/snippets/{r.json()['id']}/cancel", headers=OWNER)


def test_results_reach_the_model_with_the_next_message_only(tmp_path):
    client, m, fake = chat_client(tmp_path, [Completion(content="ok")])
    with client:
        sid = new_chat(client, m)
        client.post(f"/chats/{sid}/snippets", json={"language": "python", "source": "print('hi')"}, headers=OWNER)
        wait_for(lambda: len(snippet_events(m, sid)) == 2)
        client.post(f"/chats/{sid}/messages", json={"content": "why?"}, headers=OWNER)
        wait_for(lambda: client.get(f"/chats/{sid}").json()["status"] == "done")
        client.post(f"/chats/{sid}/messages", json={"content": "thanks"}, headers=OWNER)
        wait_for(lambda: client.get(f"/chats/{sid}").json()["status"] == "done")
        users = [x["content"] for x in m.db.get_session(sid)["context"] if x["role"] == "user"]
        assert "untrusted" in users[-2]
        assert "print('hi')" in users[-2]
        assert "<b>hi</b>" in users[-2]
        assert users[-2].endswith("why?")
        assert users[-1] == "thanks"
        typed = [e["data"]["content"] for e in m.db.events(sid) if e["type"] == "user_message"]
        assert typed[-2:] == ["why?", "thanks"]


def test_restart_marks_unfinished_runs_interrupted_and_removes_containers(tmp_path, monkeypatch):
    client, m, fake = chat_client(tmp_path)
    with client:
        sid = new_chat(client, m)
    m.bus.emit(sid, "snippet_started", {"id": "sn-lost", "language": "java", "label": "Java", "source": "x"})
    removed = []

    async def fake_remove(run_ids):
        removed.append(run_ids)
    monkeypatch.setattr(snippets, "remove_orphans", fake_remove)
    m2 = Manager(m.cfg, db=m.db, chat=m.runner.chat)

    async def boot():
        await m2.start(maintenance=False)
        await asyncio.sleep(0.05)
        await m2.stop()
    asyncio.run(boot())
    result = snippet_events(m2, sid)[-1]
    assert result["type"] == "snippet_result"
    assert result["data"]["id"] == "sn-lost"
    assert result["data"]["status"] == "interrupted"
    assert result["data"]["reasons"] == ["daemon_restart"]
    assert removed == [["sn-lost"]]
    assert not SnippetService(m.db, m.bus).recover()  # nothing left to recover


def test_daemon_stop_interrupts_a_running_snippet(tmp_path):
    client, m, fake = chat_client(tmp_path, block=True)
    with client:
        sid = new_chat(client, m)
        client.post(f"/chats/{sid}/snippets", json={"language": "python", "source": "x"}, headers=OWNER)
        wait_for(lambda: fake.calls)
    result = snippet_events(m, sid)[-1]
    assert result["type"] == "snippet_result"
    assert result["data"]["status"] == "interrupted"
    assert result["data"]["reasons"] == ["daemon_restart"]


def test_web_client_run_controls_and_untrusted_rendering(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    from pathlib import Path
    script = Path(__file__).resolve().parent / "web_chat_snippets.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_docs_name_every_toolchain_and_the_pull_command():
    from pathlib import Path
    doc = (Path(__file__).resolve().parents[1] / "docs" / "web.md").read_text(encoding="utf-8")
    for lang in LANGUAGES.values():
        assert f"`{lang.tag}`" in doc, lang.tag
    assert "python -m harness.snippets pull" in doc


def test_web_client_languages_match_the_server():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "harness" / "web" / "app.js").read_text(encoding="utf-8")
    block = js[js.index("const SNIPPET_LANGUAGES"):]
    block = block[:block.index("};") + 2]
    for lang in SnippetService.languages():
        entry = re.search(rf"\b{lang['id']}: \{{ label: \"([^\"]+)\", aliases: \[([^\]]*)\]", block)
        assert entry, lang["id"]
        assert entry[1] == lang["label"]
        assert sorted(re.findall(r'"([^"]+)"', entry[2])) == sorted(lang["aliases"])


# ---------- live Docker: real toolchains and abuse cases ----------

def _have(image: str) -> bool:
    return bool(shutil.which("docker")) and subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True).returncode == 0


def live(language: str):
    image = LANGUAGES[language].image
    return pytest.mark.skipif(not _have(image), reason=f"needs Docker and {LANGUAGES[language].tag} pinned image "
                                                      "(python -m harness.snippets pull)")


def run_live(language: str, source: str, timeout: float = 30, output_bytes: int = snippets.OUTPUT_BYTES,
             cancel_after: float | None = None) -> dict:
    runner = SnippetRunner(timeout=timeout, output_bytes=output_bytes)
    cancel = threading.Event()
    if cancel_after is not None:
        threading.Timer(cancel_after, cancel.set).start()
    run_id = _live_run_id()
    result = asyncio.run(runner.run(run_id, language, source, cancel))
    leftover = subprocess.run(["docker", "ps", "-aq", "--filter", f"name=harness-snippet-{run_id}"],
                              capture_output=True, text=True).stdout.strip()
    assert leftover == "", "the sandbox container must be gone after the run"
    return result


def _live_run_id() -> str:
    """A Docker name shared across pytest-xdist workers needs more than a truncated monotonic timestamp."""
    return "t" + secrets.token_hex(8)


def test_live_run_ids_do_not_collide_when_workers_share_a_coarse_clock(monkeypatch):
    monkeypatch.setattr(time, "monotonic_ns", lambda: 93_000_000)
    nonces = iter(("a" * 16, "b" * 16))
    monkeypatch.setattr(snippets.secrets, "token_hex", lambda size: next(nonces))
    assert _live_run_id() == "t" + "a" * 16
    assert _live_run_id() == "t" + "b" * 16


HELLO = {
    "python": ("print('hello from python')", "import sys\nsys.exit('boom')"),
    "javascript": ("console.log('hello from javascript')", "throw new Error('boom')"),
    "java": ("public class Hello { public static void main(String[] a) { System.out.println(\"hello from java\"); } }",
             "public class Boom { public static void main(String[] a) { throw new RuntimeException(\"boom\"); } }"),
    "csharp": ('Console.WriteLine("hello from csharp");', 'throw new Exception("boom");'),
    "cpp": ("#include <iostream>\nint main() { std::cout << \"hello from cpp\\n\"; }",
            "#include <cstdio>\nint main() { std::fprintf(stderr, \"boom\\n\"); return 3; }"),
}
BROKEN = {"java": "public class Bad { void main( }", "csharp": "Console.WriteLine(\"x\"", "cpp": "int main() { return 0 }"}


@pytest.mark.parametrize("language", list(HELLO))
def test_live_hello_world_and_runtime_failure(language):
    if not _have(LANGUAGES[language].image):
        pytest.skip(f"needs Docker and the pinned {LANGUAGES[language].tag} image (python -m harness.snippets pull)")
    ok = run_live(language, HELLO[language][0])
    assert ok["status"] == "completed", ok
    assert ok["run"]["stdout"].strip() == f"hello from {language}"
    assert ok["toolchain"]["version"]
    assert ok["toolchain"]["digest"] == LANGUAGES[language].image
    if LANGUAGES[language].compile:
        assert ok["compile"]["exit_code"] == 0
    bad = run_live(language, HELLO[language][1])
    assert bad["status"] == "failed"
    assert bad["run"]["exit_code"] != 0
    assert "boom" in bad["run"]["stderr"]


@pytest.mark.parametrize("language", list(BROKEN))
def test_live_compile_failure_is_separate_from_runtime(language):
    if not _have(LANGUAGES[language].image):
        pytest.skip(f"needs Docker and the pinned {LANGUAGES[language].tag} image (python -m harness.snippets pull)")
    bad = run_live(language, BROKEN[language])
    assert bad["status"] == "compile_failed"
    assert bad["run"] is None
    assert bad["compile"]["exit_code"] != 0
    assert bad["compile"]["output"].strip()


@live("python")
def test_live_no_network_mounts_secrets_or_privileges(monkeypatch):
    monkeypatch.setenv("HARNESS_SNIPPET_CANARY", "canary-secret-value")
    src = r"""
import os, socket
try:
    socket.create_connection(("1.1.1.1", 53), timeout=3); print("NET OPEN")
except OSError: print("net blocked")
try:
    socket.getaddrinfo("example.com", 80); print("DNS OPEN")
except OSError: print("dns blocked")
mounts = open("/proc/self/mounts").read()
print("uid", os.getuid())
print("docker.sock", os.path.exists("/var/run/docker.sock") or os.path.exists("/run/docker.sock"))
print("workspace", os.path.exists("/workspace"))
print("secrets", os.path.exists("/run/secrets"))
print("canary", "canary-secret-value" in repr(dict(os.environ)))
print("binds", [l.split()[1] for l in mounts.splitlines() if l.split()[1] not in (
    "/", "/proc", "/dev", "/dev/pts", "/sys", "/sys/fs/cgroup", "/dev/mqueue", "/dev/shm", "/sandbox",
    "/etc/hosts", "/etc/hostname", "/etc/resolv.conf", "/dev/console", "/usr/sbin/docker-init") and not l.split()[1].startswith(("/proc/", "/sys/"))])
try:
    open("/etc/hosts", "a"); print("etc writable")
except OSError: print("etc read-only")
try:
    open("/usr/local/evil", "w"); print("root writable")
except OSError: print("root read-only")
"""
    out = run_live("python", src)["run"]["stdout"]
    for line in ("net blocked", "dns blocked", "uid 65534", "docker.sock False", "workspace False", "secrets False",
                 "canary False", "binds []", "etc read-only", "root read-only"):
        assert line in out, out


@live("python")
def test_live_timeout_kills_the_process_tree():
    src = "import subprocess, time\nsubprocess.Popen(['sleep', '300'])\ntime.sleep(300)"
    t0 = time.monotonic()
    r = run_live("python", src, timeout=3)
    assert r["status"] == "timeout"
    assert r["reasons"] == ["timeout"]
    assert time.monotonic() - t0 < 25


@live("python")
def test_live_detached_children_die_with_the_sandbox():
    src = ("import subprocess\nsubprocess.Popen(['sleep', '300'], stdout=subprocess.DEVNULL, "
           "stderr=subprocess.DEVNULL, start_new_session=True)\nprint('spawned')")
    r = run_live("python", src)  # run_live asserts the container, and so every process in it, is gone
    assert r["status"] == "completed"
    assert r["run"]["stdout"] == "spawned\n"


@live("python")
def test_live_memory_pids_and_temp_limits():
    mem = run_live("python", "b = []\nwhile True: b.append(bytearray(64 * 1024 * 1024))")
    assert mem["status"] == "limit_exceeded"
    assert "memory_limit" in mem["reasons"]
    pids = run_live("python", "import subprocess\nps = []\ntry:\n    for _ in range(200): "
                              "ps.append(subprocess.Popen(['sleep', '30']))\nexcept OSError: pass\n"
                              "print(len(ps))\nfor p in ps: p.kill(); p.wait()")
    assert "pids_limit" in pids["reasons"]
    assert int(pids["run"]["stdout"]) < snippets.PIDS
    tmp = run_live("python", "try:\n    with open('/sandbox/big', 'wb') as f:\n        for _ in range(300): "
                             "f.write(b'x' * 1024 * 1024)\nexcept OSError as e: print(e.errno)")
    assert "temp_storage_limit" in tmp["reasons"]
    assert tmp["run"]["stdout"].strip() == "28"  # ENOSPC


@live("python")
def test_live_restart_removes_the_orphaned_container_only():
    lang = LANGUAGES["python"]
    ids = ["t-orphan-" + str(time.monotonic_ns())[-8:], "t-other-" + str(time.monotonic_ns())[-8:]]
    for rid in ids:
        assert subprocess.run(container_args(lang, rid), capture_output=True).returncode == 0
    try:
        asyncio.run(snippets.remove_orphans(ids[:1]))
        names = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True).stdout
        assert f"harness-snippet-{ids[0]}" not in names.split()
        assert f"harness-snippet-{ids[1]}" in names.split()  # another instance's run is left alone
    finally:
        subprocess.run(["docker", "rm", "-f", f"harness-snippet-{ids[1]}"], capture_output=True)


@live("python")
def test_live_output_limit_truncates_explicitly():
    r = run_live("python", "import sys\nwhile True: sys.stdout.write('y' * 65536); sys.stderr.write('z' * 100)")
    assert r["status"] == "limit_exceeded"
    assert r["reasons"] == ["output_limit"]
    assert r["truncated"]
    assert len(r["run"]["stdout"].encode()) + len(r["run"]["stderr"].encode()) == snippets.OUTPUT_BYTES


@live("python")
def test_live_cancel_and_no_files_survive_between_runs():
    r = run_live("python", "import time\nprint('x', flush=True)\ntime.sleep(60)", cancel_after=2)
    assert r["status"] == "cancelled"
    assert r["reasons"] == ["cancelled"]
    first = run_live("python", "open('/sandbox/marker', 'w').write('left behind')\nprint('wrote')")
    assert first["run"]["stdout"] == "wrote\n"
    second = run_live("python", "import os\nprint(os.path.exists('/sandbox/marker'), sorted(os.listdir('/sandbox/src')))")
    assert second["run"]["stdout"] == "False ['main.py']\n"
