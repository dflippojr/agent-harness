"""Stage (e) of #334: the pip packaging.

pyproject.toml builds a core wheel (`harness` and the SDK) and one add-on wheel per `harness_modules` package with the
in-tree backend scripts/packaging/agent_harness_build.py, which needs no network. These tests build the wheels and
check what each one holds. The end-to-end test installs them in a new virtual environment and runs the Server from
there, on loopback with throwaway config and data, first with the core wheel alone and then with the add-ons.

That virtual environment borrows the third-party dependencies from this test run's interpreter through a .pth file,
so the test needs no package index. The checkout itself is never on its path, and the test checks every import origin.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.error
import urllib.request
import venv
import zipfile
from pathlib import Path

import pytest

from harness import compat, config, modules

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "packaging"))
import agent_harness_build as build  # noqa: E402

PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
VERSION = compat.RELEASE
CORE_WHEEL = f"agent_harness-{VERSION}-py3-none-any.whl"
TOKEN_HEADER = "X-Agent-Harness-Local-Token"


def _requirements(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")]


def _git_files(*paths: str, untracked: bool = False) -> set[str]:
    """Tracked files under `paths`, or with `untracked` also the untracked ones git doesn't ignore."""
    args = ["git", "-C", str(ROOT), "ls-files", "-z", *(["-co", "--exclude-standard"] if untracked else []), "--"]
    out = subprocess.run([*args, *paths], capture_output=True, check=True).stdout.decode("utf-8")
    return {name for name in out.split("\0") if name and (ROOT / name).is_file()}


def _payload(wheel: Path) -> dict[str, bytes]:
    """The wheel's files outside its .dist-info."""
    with zipfile.ZipFile(wheel) as z:
        return {n: z.read(n) for n in z.namelist() if ".dist-info/" not in n}


def _metadata(wheel: Path) -> tuple[dict[str, list[str]], str]:
    with zipfile.ZipFile(wheel) as z:
        text = z.read(next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))).decode("utf-8")
    head, _, body = text.partition("\n\n")
    fields: dict[str, list[str]] = {}
    for line in head.splitlines():
        key, _, value = line.partition(": ")
        fields.setdefault(key, []).append(value)
    return fields, body


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> dict[str, Path]:
    return {path.name: path for path in build.build_all(ROOT, tmp_path_factory.mktemp("dist"))}


# ------------------------------------------------------------------------------------------------- pyproject.toml

def test_the_declared_dependencies_are_requirements_txt():
    """Every requirements.txt line, verbatim. pytest is the test extra: the daemon never imports it."""
    project = PYPROJECT["project"]
    test_extra = project["optional-dependencies"]["test"]
    assert sorted(project["dependencies"] + test_extra) == sorted(_requirements(ROOT / "requirements.txt"))
    assert [requirement.split(">")[0] for requirement in test_extra] == ["pytest"]


def test_the_build_needs_nothing_installed():
    """The backend is in the tree and uses the standard library only, so `pip wheel .` works offline."""
    assert PYPROJECT["build-system"] == {"requires": [], "build-backend": "agent_harness_build",
                                         "backend-path": ["scripts/packaging"]}
    assert build.get_requires_for_build_wheel() == build.get_requires_for_build_sdist() == []


# -------------------------------------------------------------------------------------------- what each wheel holds

def test_the_core_wheel_holds_the_core_package_and_the_sdk_module(built):
    payload = _payload(built[CORE_WHEEL])
    package = set(payload) - {"harness_client.py"}
    # Every tracked file of the package ships (a new data file must be listed in pyproject.toml), and nothing git
    # ignores or that sits outside the package does.
    assert _git_files("harness") <= package <= _git_files("harness", untracked=True)
    assert payload["harness_client.py"] == (ROOT / "sdk" / "harness_client.py").read_bytes()
    assert all(payload[name] == (ROOT / name).read_bytes() for name in package)
    assert "harness/__init__.py" in payload  # a regular package: never merged with another harness folder


def test_the_core_wheel_metadata_matches_pyproject(built):
    fields, readme = _metadata(built[CORE_WHEEL])
    project = PYPROJECT["project"]
    assert fields["Name"] == ["agent-harness"] and fields["Version"] == [VERSION]
    assert fields["Requires-Python"] == [project["requires-python"]]
    assert fields["Requires-Dist"] == project["dependencies"] + [
        f'{requirement}; extra == "test"' for requirement in project["optional-dependencies"]["test"]]
    assert fields["Provides-Extra"] == ["test"]
    assert readme == (ROOT / "README.md").read_text(encoding="utf-8")
    with zipfile.ZipFile(built[CORE_WHEEL]) as z:
        entry_points = z.read(f"agent_harness-{VERSION}.dist-info/entry_points.txt").decode("utf-8")
        assert z.read(f"agent_harness-{VERSION}.dist-info/licenses/LICENSE") == (ROOT / "LICENSE").read_bytes()
    assert entry_points.splitlines() == ["[console_scripts]", "harness = harness.cli:main",
                                         "harness-server = harness.__main__:main"]


def test_every_record_lists_each_file_with_its_hash(built):
    for wheel in built.values():
        with zipfile.ZipFile(wheel) as z:
            names = z.namelist()
            record_name = next(n for n in names if n.endswith(".dist-info/RECORD"))
            rows = [line.split(",") for line in z.read(record_name).decode("utf-8").splitlines()]
            assert sorted(row[0] for row in rows) == sorted(names), wheel.name
            for name, digest, size in rows:
                if name == record_name:
                    assert (digest, size) == ("", "")
                    continue
                data = z.read(name)
                expected = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                assert (digest, size) == (f"sha256={expected}", str(len(data))), name


def test_each_module_is_an_add_on_wheel_of_its_own_on_this_core(built):
    names = sorted(p.name for p in (ROOT / "harness_modules").iterdir() if (p / "__init__.py").is_file())
    assert build.addon_names(ROOT) == names and len(built) == 1 + len(names)
    repo_files = PYPROJECT["tool"]["agent-harness"]["addons"]["repo-files"]
    for name in names:
        wheel = built[f"agent_harness_module_{name}-{VERSION}-py3-none-any.whl"]
        package = f"harness_modules/{name}"
        payload = _payload(wheel)
        carried = {n for n in payload if n.startswith(f"{package}/{modules.REPO_FILES}/")}
        own = set(payload) - carried
        assert _git_files(package) <= own <= _git_files(package, untracked=True), name
        assert all(payload[n] == (ROOT / n).read_bytes() for n in own)
        assert {n.removeprefix(f"{package}/{modules.REPO_FILES}/") for n in carried} == set(repo_files.get(name, []))
        assert all(payload[n] == (ROOT / n.removeprefix(f"{package}/{modules.REPO_FILES}/")).read_bytes()
                   for n in carried)
        assert "harness_modules/__init__.py" not in payload  # the namespace stays open to every wheel
        fields, _ = _metadata(wheel)
        assert fields["Name"] == [f"agent-harness-module-{name.replace('_', '-')}"]
        assert fields["Requires-Dist"] == [f"agent-harness=={VERSION}"]
    assert build.REPO_FILES == modules.REPO_FILES
    assert build.unknown_repo_file_addons(ROOT) == []


def test_builds_are_reproducible(built, tmp_path, monkeypatch):
    again = {path.name: path for path in build.build_all(ROOT, tmp_path / "again")}
    assert {name: path.read_bytes() for name, path in again.items()} == {
        name: path.read_bytes() for name, path in built.items()}
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    stamped = build.write_wheel(build.core_distribution(ROOT), tmp_path / "stamped")
    with zipfile.ZipFile(stamped) as z:
        assert {info.date_time for info in z.infolist()} == {time.gmtime(1700000000)[:6]}


# ------------------------------------------------------------------------------------------ the PEP 517 front doors

def test_the_source_distribution_builds_the_same_wheel(built, tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    sdist = tmp_path / "sdist" / build.build_sdist(str(tmp_path / "sdist"))
    with tarfile.open(sdist) as archive:
        archive.extractall(tmp_path / "src", filter="data")
    source = tmp_path / "src" / f"agent_harness-{VERSION}"
    assert (source / "PKG-INFO").is_file() and not (source / "tests").exists()
    monkeypatch.chdir(source)
    metadata_dir = build.prepare_metadata_for_build_wheel(str(tmp_path / "meta"))
    assert (tmp_path / "meta" / metadata_dir / "METADATA").is_file()
    wheel = tmp_path / "wheel" / build.build_wheel(str(tmp_path / "wheel"))
    assert wheel.read_bytes() == built[CORE_WHEEL].read_bytes()


def test_pip_builds_the_core_wheel_through_the_backend_offline(built, tmp_path):
    out = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-index",
                          "--disable-pip-version-check", "-w", str(tmp_path), str(ROOT)],
                         capture_output=True, text=True, timeout=300, cwd=tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr
    assert (tmp_path / CORE_WHEEL).read_bytes() == built[CORE_WHEEL].read_bytes()


def test_a_bad_layout_is_refused_with_its_name(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.0"\n'
                                             '[tool.agent-harness.build]\npackages = ["pkg"]\n'
                                             'package-data = ["pkg/*.json"]\n', encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    with pytest.raises(build.BuildError, match="no __init__.py"):
        build.core_distribution(tmp_path)
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    with pytest.raises(build.BuildError, match="matches no file"):
        build.core_distribution(tmp_path)
    (tmp_path / "pkg" / "data.json").write_text("{}", encoding="utf-8")
    assert set(build.core_distribution(tmp_path).files) == {"pkg/__init__.py", "pkg/data.json"}
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "one"\n'
                                             '[tool.agent-harness.build]\npackages = ["pkg"]\n', encoding="utf-8")
    with pytest.raises(build.BuildError, match="release number"):
        build.core_distribution(tmp_path)
    assert build.main(["--source", str(tmp_path), "--out", str(tmp_path / "out")]) == 1


def test_links_and_paths_outside_the_tree_are_refused(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.0"\n'
                                             '[tool.agent-harness.build]\npackages = ["pkg"]\n'
                                             'modules = { other = "../outside.py" }\n', encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    with pytest.raises(build.BuildError, match="inside the repository"):
        build.core_distribution(tmp_path)
    (tmp_path / "secret.txt").write_text("not for a wheel", encoding="utf-8")
    try:
        (tmp_path / "pkg" / "leak.py").symlink_to(tmp_path / "secret.txt")
    except OSError:  # Windows without the symlink privilege: a copy shows the file is otherwise packaged
        (tmp_path / "pkg" / "leak.py").write_text("x = 1\n", encoding="utf-8")
        expected = "pkg/leak.py"
    else:
        expected = "is not a regular file"
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.0"\n'
                                             '[tool.agent-harness.build]\npackages = ["pkg"]\n', encoding="utf-8")
    if expected == "pkg/leak.py":
        assert expected in build.core_distribution(tmp_path).files
    else:
        with pytest.raises(build.BuildError, match=expected):
            build.core_distribution(tmp_path)


# ------------------------------------------------------------------------------------------------ installed, for real

class Installed:
    """A virtual environment outside the checkout's import path, holding only what pip put there, plus the test
    interpreter's third-party packages through a .pth file."""

    def __init__(self, root: Path):
        self.root = root
        self.work = root / "work"  # every command's working directory: never the checkout
        self.home = root / "home"
        self.work.mkdir(parents=True)
        self.home.mkdir()
        venv.EnvBuilder(with_pip=True).create(root / "venv")
        python = root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.python = python
        paths = json.loads(self.run(str(python), "-I", "-c", "import json, sysconfig; print(json.dumps("
                                    "{k: sysconfig.get_paths()[k] for k in ('purelib', 'scripts')}))").stdout)
        self.site, self.scripts = Path(paths["purelib"]), Path(paths["scripts"])
        own = {sysconfig_path("purelib"), sysconfig_path("platlib")}
        (self.site / "_borrowed_dependencies.pth").write_text("\n".join(sorted(own)) + "\n", encoding="utf-8")

    @property
    def env(self) -> dict[str, str]:
        """No variable from this test run names a harness directory or a Python path. HOME and the data homes are
        throwaway, and every proxy points at a closed port, so the Server reaches nothing outside this machine (the
        secret scanner's start-up fetch fails fast and is logged)."""
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith(("HARNESS_", "PYTHON", "VIRTUAL_ENV", "PIP_"))
               and k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")}
        local = self.home / "AppData" / "Local"
        closed = "http://127.0.0.1:1"
        env.update(HOME=str(self.home), USERPROFILE=str(self.home), LOCALAPPDATA=str(local),
                   XDG_DATA_HOME=str(self.home / ".local" / "share"), PIP_DISABLE_PIP_VERSION_CHECK="1",
                   PIP_NO_INPUT="1", HTTP_PROXY=closed, HTTPS_PROXY=closed, http_proxy=closed, https_proxy=closed,
                   NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
        return env

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        out = subprocess.run(list(args), cwd=self.work, env=self.env, capture_output=True, text=True, timeout=300)
        if check:
            assert out.returncode == 0, f"{args}: {out.stdout}\n{out.stderr}"
        return out

    def script(self, name: str) -> str:
        return str(self.scripts / (f"{name}.exe" if os.name == "nt" else name))

    def pip_install(self, *wheels: Path) -> None:
        self.run(str(self.python), "-m", "pip", "install", "--no-index", "--no-deps", *map(str, wheels))

    def probe(self) -> dict:
        """Where everything comes from, asked of the installed core with -I (no working directory on the path)."""
        code = (
            "import importlib.util, json, harness, harness_client\n"
            "from harness import config, modules, stables_shell\n"
            "names = modules.namespace_packages()\n"
            "loaded = {n: importlib.import_module(n).__file__ for n in names}\n"
            "print(json.dumps({'harness': harness.__file__, 'harness_path': list(harness.__path__),\n"
            "    'sdk': harness_client.__file__, 'config': config.__file__, 'checkout': config.CHECKOUT,\n"
            "    'config_dir': str(config.resolve_config_dir()), 'data_dir': str(config._default_data_dir()),\n"
            "    'stables': stables_shell.locate(), 'modules': loaded,\n"
            "    'module_names': sorted(m.name for m in modules.discover())}, default=str))\n")
        return json.loads(self.run(str(self.python), "-I", "-c", code).stdout)


def sysconfig_path(name: str) -> str:
    import sysconfig
    return sysconfig.get_paths()[name]


class Server:
    """`harness-server` with no arguments: it finds the per-user install's config itself."""

    def __init__(self, installed: Installed, port: int, data_dir: Path):
        self.port, self.data_dir = port, data_dir
        self.log = installed.root / f"server-{time.monotonic_ns()}.log"
        with self.log.open("w", encoding="utf-8") as log:
            self.proc = subprocess.Popen([installed.script("harness-server")], cwd=installed.work, env=installed.env,
                                         stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 120
        while True:
            if self.proc.poll() is not None:
                raise AssertionError(f"the server exited ({self.proc.returncode}):\n{self.log.read_text()}")
            try:
                if self.get("/health", owner=False)[0] == 200:
                    break
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise AssertionError(f"no /health after 120 s:\n{self.log.read_text()}")
            time.sleep(0.25)
        self.token = (data_dir / "local-owner.token").read_text(encoding="utf-8").strip()

    def get(self, path: str, owner: bool = True) -> tuple[int, bytes]:
        headers = {TOKEN_HEADER: self.token} if owner else {}
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback, never a proxy
        try:
            with opener.open(request, timeout=30) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def json(self, path: str) -> dict:
        status, body = self.get(path)
        assert status == 200, (path, status, body[:300])
        return json.loads(body)

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(30)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _module_paths() -> set[str]:
    """Every route path the checkout's add-on modules declare in their route tables."""
    paths = set()
    for module in modules.discover():
        for hook in (module.owner_routes, module.app_routes, module.public_routes):
            if hook is not None:
                paths |= {path for _method, path, _fn, _kwargs in hook()._routes}
        paths |= {"/api/admin/v1" + path for path in module.admin_paths}
    return paths


def _under(path: str, root: Path) -> bool:
    return Path(path).resolve().is_relative_to(root.resolve())


def test_the_wheels_install_and_run_the_server_without_the_checkout(built, tmp_path):
    env = Installed(tmp_path)
    install = env.home / ".local" / "share" / "agent-harness"
    if os.name == "nt":
        install = env.home / "AppData" / "Local" / "agent-harness"
    data_dir, port = tmp_path / "data", _free_port()

    # The core wheel alone.
    env.pip_install(built[CORE_WHEEL])
    probe = env.probe()
    for key in ("harness", "sdk", "config"):
        assert _under(probe[key], env.site), (key, probe[key])
    assert all(_under(path, env.site) for path in probe["harness_path"])
    assert probe["checkout"] is None and probe["stables"] is None
    assert (Path(probe["config_dir"]), Path(probe["data_dir"])) == (install / "config", install / "data")
    assert probe["modules"] == {} and probe["module_names"] == []
    # The installed core writes a service-profile config to its default place, and the Server finds it there.
    env.run(str(env.python), "-m", "harness.setup_config", "--config-dir", probe["config_dir"],
            "--data-dir", str(data_dir), "--profile", "service", "--port", str(port),
            "--pause-flag", str(tmp_path / "paused"))
    server = Server(env, port, data_dir)
    try:
        health = server.json("/health")
        assert health["ok"] is True and health["profile"] == "service" and health["release"] == VERSION
        assert set(health["capabilities"]["modules"]) <= set(config.CORE_MODULE_NAMES)
        assert server.get("/")[0] == 404  # no Stables: the API runs alone
        assert server.json("/api/v1")["server"] == "agent-harness"
        assert "workspaces" in server.json("/maintenance")
        paths = set(server.json("/openapi.json")["paths"])
        assert paths and not paths & _module_paths()
        for path in ("/jobs", "/mac-client/install.sh", "/api/admin/v1/hub"):
            assert server.get(path)[0] == 404, path
    finally:
        server.stop()

    # The add-on wheels: every module is discovered through the namespace, and a switched-on one has its routes.
    env.pip_install(*(path for name, path in built.items() if name != CORE_WHEEL))
    probe = env.probe()
    expected = sorted(p.name for p in (ROOT / "harness_modules").iterdir() if (p / "__init__.py").is_file())
    assert sorted(probe["modules"]) == [f"harness_modules.{name}" for name in expected]
    assert all(_under(path, env.site) for path in probe["modules"].values())
    assert probe["module_names"] == sorted(module.name for module in modules.discover())
    env.run(str(env.python), "-m", "harness.setup_config", "--config-dir", probe["config_dir"],
            "--data-dir", str(data_dir), "--profile", "service", "--port", str(port),
            "--pause-flag", str(tmp_path / "paused"), "--enable-module", "jobs", "--enable-module", "runners",
            "--enable-module", "hub")
    # The hub module validates its example against the schema its wheel carries, into the default config dir.
    env.run(env.script("harness"), "hub", "entries", "init")
    server = Server(env, port, data_dir)
    try:
        health = server.json("/health")
        assert {"jobs", "runners", "hub"} <= set(health["capabilities"]["modules"])
        paths = set(server.json("/openapi.json")["paths"])
        assert {"/jobs", "/api/admin/v1/hub"} <= paths
        assert server.json("/jobs") == []
        assert len(server.json("/api/admin/v1/hub")["entries"]) == len(
            json.loads((ROOT / "docs" / "hub.entries.example.json").read_text(encoding="utf-8"))["entries"])
        status, script = server.get("/mac-client/install.sh")
        assert (status, script) == (200, (ROOT / "macrunner" / "install.sh").read_bytes())
        status, package = server.get("/mac-client/package.tar.gz")
        assert status == 200
        with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
            bundle = {member.name: archive.extractfile(member).read() for member in archive.getmembers()}
        assert bundle["app/harness_runner.py"] == (ROOT / "macrunner" / "harness_runner.py").read_bytes()
        assert bundle["client/harness_client.py"] == (ROOT / "sdk" / "harness_client.py").read_bytes()
        assert bundle["client/harness_cli.py"] == (ROOT / "harness" / "cli.py").read_bytes()
    finally:
        server.stop()
