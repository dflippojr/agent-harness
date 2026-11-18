"""Stage (e) of #334: the pip packaging.

pyproject.toml builds a core wheel (`harness` and the SDK) and one add-on wheel per `harness_modules` package with the
in-tree backend scripts/packaging/agent_harness_build.py, which needs no network. These tests build the wheels and
check what each one holds. The end-to-end test installs them in a new virtual environment and runs the Server from
there, on loopback with throwaway config and data, first with the core wheel alone and then with the add-ons.

That virtual environment borrows the third-party dependencies from this test run's interpreter through a .pth file,
so the test needs no package index. The checkout itself is never on its path, and the test checks every import origin.
"""

from __future__ import annotations

import ast
import base64
import contextlib
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import tomllib
import types
import urllib.error
import urllib.request
import venv
import zipfile
from pathlib import Path, PurePosixPath

import pytest
import yaml

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


def _git_files(*paths: str) -> set[str]:
    """The files git tracks under `paths`."""
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", *paths], capture_output=True,
                         check=True).stdout.decode("utf-8")
    return {name for name in out.split("\0") if name}


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
    # Exactly the package's tracked files: a new data file must be listed in pyproject.toml, and nothing untracked or
    # ignored ships (test_from_a_checkout_only_tracked_files_ship).
    assert package == _git_files("harness")
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
        assert own == _git_files(package), name
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


def test_every_platform_builds_the_same_bytes(tmp_path, monkeypatch):
    """The review of #1001 (r3): zipfile marks each entry with the platform that wrote it (create_system 0 on Windows,
    3 elsewhere), so the same files made different wheels on Windows and Linux. The entries now say Unix, as their mode
    bits do, and nothing else the backend writes depends on the platform it runs on."""
    made = {}
    for platform in ("win32", "linux"):
        with monkeypatch.context() as patch:
            patch.setattr(sys, "platform", platform)
            tree = build.Tree(ROOT)
            pyproject = build.load_pyproject(tree)
            paths = [*build.build_all(tree, tmp_path / platform),
                     build.write_sdist(tree, build.core_distribution(tree, pyproject), pyproject, tmp_path / platform)]
            made[platform] = {path.name: path.read_bytes() for path in paths}
    assert made["win32"] == made["linux"]
    for name, data in made["win32"].items():
        if name.endswith(".whl"):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                assert {info.create_system for info in z.infolist()} == {3}, name


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
    _layout(tmp_path, 'package-data = ["pkg/*.json"]\n')
    (tmp_path / "pkg" / "__init__.py").unlink()
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


def test_the_command_line_writes_only_inside_the_working_directory(tmp_path, monkeypatch, capsys):
    """SonarCloud on #1001: the command line's --source and --out are checked before anything is read or written."""
    monkeypatch.chdir(tmp_path)
    assert build.main(["--source", str(tmp_path), "--out", "dist"]) == 1  # no pyproject.toml there
    assert "is not a checkout" in capsys.readouterr().err
    assert build.main(["--source", str(ROOT), "--out", str(tmp_path.parent / "elsewhere")]) == 1
    assert "inside the working directory" in capsys.readouterr().err
    assert not (tmp_path.parent / "elsewhere").exists()
    assert build.main(["--source", str(ROOT), "--out", "dist"]) == 0
    assert sorted(p.name for p in (tmp_path / "dist").iterdir())[0] == CORE_WHEEL


def _layout(root: Path, extra: str = "") -> Path:
    """A one-package project: pkg/__init__.py and a pyproject.toml naming it."""
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.0"\n'
                                         f'[tool.agent-harness.build]\npackages = ["pkg"]\n{extra}', encoding="utf-8")
    return root


def _link(link: Path, target: Path) -> Path:
    """Make `link` point at `target` and return the path to read through it. Windows makes a symlink only with a
    privilege; without it a folder gets a junction (no privilege needed), and a file is reached through one."""
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
        return link
    except OSError:
        if os.name != "nt":
            raise
    import _winapi
    folder = target if target.is_dir() else target.parent
    _winapi.CreateJunction(str(folder), str(link))
    return link if target.is_dir() else link / target.name


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_a_path_outside_the_tree_is_refused(tmp_path):
    with pytest.raises(build.BuildError, match="inside the repository"):
        build.core_distribution(_layout(tmp_path, 'modules = { other = "../outside.py" }\n'))


@pytest.mark.parametrize("where", ["file", "folder", "package root"])
def test_a_link_anywhere_on_a_path_is_refused(tmp_path, where):
    """The review of #1001: a linked folder or package root used to be walked, shipping files from outside the tree."""
    root = _layout(tmp_path / "project")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "__init__.py").write_text("", encoding="utf-8")
    (outside / "secret.py").write_text("TOKEN = 'not for a wheel'\n", encoding="utf-8")
    if where == "file":  # named in pyproject.toml, or listed by git: read directly
        read = _link(root / "pkg" / "leak.py", outside / "secret.py").relative_to(root).as_posix()
        with pytest.raises(build.BuildError, match="goes through a link"):
            build.Tree(root).read(read)
    elif where == "folder":  # met while walking a package
        _link(root / "pkg" / "sub", outside)
        with pytest.raises(build.BuildError, match="is a link to a folder"):
            build.core_distribution(root)
    else:  # the package itself
        for child in (root / "pkg").iterdir():
            child.unlink()
        (root / "pkg").rmdir()
        _link(root / "pkg", outside)
        with pytest.raises(build.BuildError, match="goes through a link"):
            build.core_distribution(root)


def test_from_a_checkout_only_tracked_files_ship(tmp_path):
    """The review of #1001: an untracked file, or one git ignores, in a package folder must not reach the wheel."""
    root = _layout(tmp_path, 'package-data = ["pkg/*.json"]\nmodules = { notes = "notes.py" }\n')
    (root / "pkg" / "data.json").write_text("{}", encoding="utf-8")
    (root / "notes.py").write_text("", encoding="utf-8")
    (root / ".gitignore").write_text("runs/\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "pyproject.toml", ".gitignore", "pkg/__init__.py", "pkg/data.json", "notes.py")
    (root / "pkg" / "untracked.py").write_text("SECRET = 1\n", encoding="utf-8")
    (root / "pkg" / "stray.json").write_text("{}", encoding="utf-8")
    (root / "pkg" / "runs").mkdir()
    (root / "pkg" / "runs" / "private_settings.py").write_text("TOKEN = 'x'\n", encoding="utf-8")
    assert set(build.core_distribution(root).files) == {"pkg/__init__.py", "pkg/data.json", "notes.py"}
    (root / "pyproject.toml").write_text((root / "pyproject.toml").read_text(encoding="utf-8").replace(
        'notes = "notes.py"', 'notes = "pkg/untracked.py"'), encoding="utf-8")
    with pytest.raises(build.BuildError, match="not tracked by git"):  # a file named in pyproject.toml, too
        build.core_distribution(root)


def test_a_checkout_whose_files_git_cannot_list_is_refused(tmp_path, monkeypatch):
    root = _layout(tmp_path)
    (root / ".git").mkdir()

    def no_git(*args, **kwargs):
        raise OSError("git: not found")
    monkeypatch.setattr(build.subprocess, "run", no_git)
    with pytest.raises(build.BuildError, match="could not list its tracked files"):
        build.core_distribution(root)


def test_the_readme_has_the_same_line_ends_on_every_checkout(tmp_path):
    root = _layout(tmp_path, "")
    text = (root / "pyproject.toml").read_text(encoding="utf-8").replace('version = "1.0"', 'version = "1.0"\nreadme = "README.md"')
    (root / "pyproject.toml").write_text(text, encoding="utf-8")
    (root / "README.md").write_bytes(b"# Title\r\n\r\nBody\r\n")  # as a Windows clone with autocrlf has it
    assert build.core_distribution(root).readme == "# Title\n\nBody\n"


def test_nothing_the_build_reads_is_on_the_ci_skip_list():
    """A file on CI_SKIP_UNREAD_PATHS can change without any test job running, so no build input may be one. The
    skip-list scan (tests/test_ci_skip_list.py) reads code, not pyproject.toml, so this checks the paths
    pyproject.toml hands the backend: the review of #1001 found the sdist list naming one."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    step = next(s for s in workflow["jobs"]["changes"]["steps"] if s.get("id") == "filter")
    skipped = set(step["env"]["CI_SKIP_UNREAD_PATHS"].split())
    project, settings = PYPROJECT["project"], PYPROJECT["tool"]["agent-harness"]
    files = {"pyproject.toml", settings["build"]["version-from"], project.get("readme", ""),
             *project.get("license-files", []), *settings["build"]["sdist"], *settings["build"]["modules"].values(),
             *(path for paths in settings["addons"]["repo-files"].values() for path in paths)}
    folders = [*settings["build"]["packages"], settings["addons"]["namespace"]]
    assert skipped and not files & skipped
    assert not [path for path in skipped if any(path.startswith(f"{folder}/") for folder in folders)]


def test_a_project_inside_a_bigger_checkout_ships_only_what_git_tracks(tmp_path):
    """A project vendored into another repository has no .git of its own, and git still tracks its files or not."""
    outer = tmp_path / "outer"
    root = _layout(outer / "vendor" / "project", 'modules = { notes = "notes.py" }\n')
    (root / "notes.py").write_text("", encoding="utf-8")
    _git(outer, "init", "-q")
    _git(outer, "add", "vendor/project/pyproject.toml", "vendor/project/pkg/__init__.py", "vendor/project/notes.py")
    (root / "pkg" / "untracked.py").write_text("SECRET = 1\n", encoding="utf-8")
    assert set(build.core_distribution(root).files) == {"pkg/__init__.py", "notes.py"}
    _git(outer, "rm", "-q", "--cached", "vendor/project/notes.py")
    with pytest.raises(build.BuildError, match="notes.py is not tracked by git"):
        build.core_distribution(root)


def test_a_folder_git_tracks_nothing_in_is_built_as_it_is(tmp_path):
    """An unpacked source distribution has no .git. Unpacked in a checkout's ignored folder (a temporary folder kept
    there), git tracks none of it, so it is built as it is, as it would be anywhere else."""
    outer = tmp_path / "outer"
    root = _layout(outer / "build" / "unpacked")
    (root / "pkg" / "extra.py").write_text("", encoding="utf-8")
    (outer / ".gitignore").write_text("build/\n", encoding="utf-8")
    _git(outer, "init", "-q")
    _git(outer, "add", ".gitignore")
    assert set(build.core_distribution(root).files) == {"pkg/__init__.py", "pkg/extra.py"}


def test_a_project_in_a_checkout_git_cannot_list_is_refused(tmp_path, monkeypatch):
    root = _layout(tmp_path / "outer" / "project")
    (tmp_path / "outer" / ".git").mkdir()

    def no_git(*args, **kwargs):
        raise OSError("git: not found")
    monkeypatch.setattr(build.subprocess, "run", no_git)
    with pytest.raises(build.BuildError, match="could not list its tracked files"):
        build.core_distribution(root)


# ---------------------------------------------------------------- one way to read, one way to write (#1001, review r2)

BACKEND = ROOT / "scripts" / "packaging" / "agent_harness_build.py"
# The review of #1001 found reads that skipped Tree's checks: package folders walked whole (r1) and pyproject.toml read
# directly (r2). So the backend touches the disk in two places only: Tree reads the source (tracked files, never through
# a link) and Out writes the artifacts, reading nothing. These tables say what each reference in the backend does.
SIDES = {"Tree": {"read", "locate"}, "Out": {"write", "locate"}}
MODULE_NAMES = {  # by dotted prefix, the first match deciding; None touches nothing
    **dict.fromkeys(("os.environ", "os.sep", "os.name", "io.BytesIO", "tomllib.loads", "tomllib.TOMLDecodeError",
                     "zipfile.ZipInfo", "zipfile.ZIP_DEFLATED", "tarfile.TarInfo", "tarfile.PAX_FORMAT",
                     "gzip.compress")),
    **dict.fromkeys(("os.path.realpath", "os.path.join", "os.getcwd"), "locate"),
    **dict.fromkeys(("os.replace", "os.rename", "os.remove", "os.unlink", "os.mkdir", "os.makedirs", "os.rmdir",
                     "os.chmod", "os.symlink", "os.link", "os.truncate", "os.utime", "os.write"), "write"),
    **dict.fromkeys(("os", "subprocess", "glob", "tomllib"), "read"),
    **dict.fromkeys(("shutil", "tempfile", "io", "zipfile", "tarfile", "gzip", "sys.modules"), "both"),
}
MODULE_BASES = {name.split(".")[0] for name in MODULE_NAMES}
PURE_MODULES = {"argparse", "ast", "base64", "fnmatch", "hashlib", "re", "stat", "time"}  # ast.walk walks a syntax tree
ARCHIVES = {"zipfile.ZipFile", "tarfile.open", "tarfile.TarFile", "gzip.GzipFile", "gzip.open"}  # fine on a BytesIO
METHODS = {  # whatever the receiver: the backend's paths are pathlib objects
    **dict.fromkeys(("read_bytes", "read_text", "iterdir", "glob", "rglob", "walk", "exists", "is_file", "is_dir",
                     "is_symlink", "is_junction", "is_mount", "stat", "lstat", "readlink", "samefile"), "read"),
    **dict.fromkeys(("write_bytes", "write_text", "mkdir", "touch", "unlink", "rmdir", "rename", "chmod", "lchmod",
                     "symlink_to", "hardlink_to"), "write"),  # and replace(target): str.replace takes two arguments
    **dict.fromkeys(("resolve", "absolute", "expanduser", "cwd", "home"), "locate"),
    **dict.fromkeys(("open", "__dict__", "__getattribute__"), "both"),
}
BUILTINS = {"open", "getattr", "setattr", "delattr", "__import__", "__builtins__", "exec", "eval", "compile",
            "globals", "vars", "locals", "breakpoint"}  # open, and the ways around this scan
IMPORTS = {"__future__", "argparse", "ast", "base64", "dataclasses", "fnmatch", "gzip", "hashlib", "io", "os",
           "pathlib", "re", "stat", "subprocess", "sys", "tarfile", "time", "tomllib", "zipfile"}
FROM_IMPORTS = {"__future__": {"annotations"}, "dataclasses": {"dataclass", "field"},
                "pathlib": {"Path", "PurePosixPath"}}


def _dotted(node: ast.AST | None) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return ".".join([node.id, *reversed(parts)]) if isinstance(node, ast.Name) else ""


def _on_a_buffer(call: ast.Call, parents: dict) -> bool:
    """An archive opened on an io.BytesIO its own function made: it lives in memory, not on the disk."""
    function: ast.AST = call
    while function in parents and not isinstance(function, ast.FunctionDef):
        function = parents[function]
    buffers = {target.id for node in ast.walk(function) if isinstance(node, ast.Assign)
               and isinstance(node.value, ast.Call) and _dotted(node.value.func) == "io.BytesIO"
               for target in node.targets if isinstance(target, ast.Name)}
    files = [*call.args[:1], *(k.value for k in call.keywords if k.arg in ("file", "fileobj", "name", "filename"))]
    return bool(files) and all(isinstance(f, ast.Name) and f.id in buffers for f in files)


def _kind(node: ast.AST, parents: dict) -> tuple[str, str | None]:
    """What `node` names, and what that does to the disk: read, write, locate, both, or nothing (None)."""
    parent = parents.get(node)
    call = parent if isinstance(parent, ast.Call) and parent.func is node else None
    inner = isinstance(parent, ast.Attribute) and parent.value is node  # only the whole of a dotted name counts
    if isinstance(node, ast.Name):
        if node.id in BUILTINS or (node.id in MODULE_BASES - {"sys"} and not inner):
            return node.id, "both"
        return node.id, None
    if not isinstance(node, ast.Attribute) or inner:
        return "", None
    dotted = _dotted(node)
    if dotted.split(".")[0] in PURE_MODULES:
        return dotted, None
    if dotted.split(".")[0] in MODULE_BASES:
        if dotted in ARCHIVES and call is not None and _on_a_buffer(call, parents):
            return dotted, None
        return dotted, next((kind for prefix, kind in MODULE_NAMES.items()
                             if dotted == prefix or dotted.startswith(f"{prefix}.")), None)
    if node.attr == "replace":  # str.replace(old, new) touches nothing; Path.replace(target) moves a file
        return "replace", "write" if call is None or len(call.args) + len(call.keywords) == 1 else None
    return node.attr, METHODS.get(node.attr)


def _import_problems(node: ast.Import | ast.ImportFrom, module: ast.Module) -> list[str]:
    where = "" if node in module.body else " (not at the top)"
    if isinstance(node, ast.Import):
        return [f"import {a.name}{f' as {a.asname}' if a.asname else ''}{where}" for a in node.names
                if a.name not in IMPORTS or a.asname or where]
    allowed = FROM_IMPORTS.get(node.module or "", set())
    return [f"from {node.module} import {a.name}{where}" for a in node.names
            if node.level or a.name not in allowed or a.asname or where]


def _disk_access(source: str) -> list[tuple[int, str]]:
    """(line, problem) for each reference in `source` that touches the disk where its scope may not, and for each
    import this scan can't vouch for."""
    module = ast.parse(source)
    parents = {child: node for node in ast.walk(module) for child in ast.iter_child_nodes(node)}
    scopes = {inner: top.name for top in module.body if isinstance(top, ast.ClassDef) and top.name in SIDES
              for inner in ast.walk(top)}
    found = []
    for node in ast.walk(module):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found += [(node.lineno, problem) for problem in _import_problems(node, module)]
            continue
        what, kind = _kind(node, parents)
        scope = scopes.get(node, "module")
        if kind is not None and kind not in SIDES.get(scope, set()):
            found.append((getattr(node, "lineno", 0), f"{what} ({kind}) in {scope}"))
    return sorted(found)


def test_the_backend_touches_the_disk_only_through_tree_and_out():
    """Nothing but Tree reads and nothing but Out writes, so no input can skip Tree's checks: the review of #1001 found
    one that did (r2: pyproject.toml), after the first round had added Tree for the package folders."""
    found = [f"line {line}: {problem}" for line, problem in _disk_access(BACKEND.read_text(encoding="utf-8"))]
    assert not found, "Read the source through Tree and write through Out:\n" + "\n".join(found)


@pytest.mark.parametrize(("code", "expected"), [
    ("def f(p):\n    return p.read_text()\n", ["read_text (read) in module"]),
    ("class Tree:\n    def f(self, p):\n        p.write_bytes(b'')\n", ["write_bytes (write) in Tree"]),
    ("class Out:\n    def f(self, p):\n        return p.read_bytes()\n", ["read_bytes (read) in Out"]),
    ("import os\n\n\ndef f():\n    return os.listdir('.')\n", ["os.listdir (read) in module"]),
    ("def f(p, q):\n    p.replace(q)\n    return 'a'.replace('a', 'b')\n", ["replace (write) in module"]),
    ("import zipfile\n\n\ndef f(p):\n    zipfile.ZipFile(p, 'w')\n", ["zipfile.ZipFile (both) in module"]),
    ("import io\nimport zipfile\n\n\ndef f():\n    b = io.BytesIO()\n    zipfile.ZipFile(b, 'w')\n", []),
    ("def f(p):\n    return getattr(p, 'read_' + 'text')()\n", ["getattr (both) in module"]),
    ("import shutil as s\n", ["import shutil as s"]),
    ("def f():\n    import os\n", ["import os (not at the top)"]),
    ("import ast\nimport re\n\n\ndef f(s):\n    return list(ast.walk(ast.parse(s))), re.match('a', s).group(0)\n", []),
], ids=["read", "write-in-tree", "read-in-out", "os-call", "path-replace", "zip-on-disk", "zip-in-memory", "getattr",
        "new-import", "late-import", "pure-modules"])
def test_the_disk_scan_catches_each_way_onto_the_disk(code, expected):
    assert [problem for _line, problem in _disk_access(code)] == expected


# The grid: every kind of input the backend reads, in every state it can be in. Each file sits in a folder of its own,
# so linking that folder touches that kind alone.
GRID = {
    "pyproject.toml": "pyproject.toml",
    "version-from": "release/version.py",
    "readme": "text/README.md",
    "license-files": "legal/COPYING.txt",
    "sdist entry": "tools/notes.txt",
    "modules mapping": "client/client.py",
    "repo-files": "shared/manifest.json",
    "package source": "pkg/sub/mod.py",
    "package data": "pkg/assets/data.json",
    "add-on source": "addons/one/sub/mod.py",
    "add-on data": "addons/one/assets/data.json",
}
LISTED = {"package source", "package data", "add-on source", "add-on data"}  # found by listing a folder, not named
STATES = ("tracked", "untracked", "linked file", "linked parent", "recorded link", "unlistable folder")
# pyproject.toml has no linked-parent cell: it sits at the root, the folder the build runs in. Only a folder the build
# lists can be unlistable, and only outside a checkout, where the build walks folders (a checkout lists git's index).
GRID_CELLS = [(kind, state) for kind in GRID for state in STATES
              if (state != "linked parent" or "/" in GRID[kind]) and (state != "unlistable folder" or kind in LISTED)]
GRID_PYPROJECT = """\
[project]
name = "grid"
description = "{marker}"
dynamic = ["version"]
readme = "text/README.md"
license-files = ["legal/COPYING.txt"]

[tool.agent-harness.build]
version-from = "release/version.py"
packages = ["pkg"]
modules = {{ client = "client/client.py" }}
package-data = ["pkg/assets/*.json"]
sdist = ["tools/notes.txt"]

[tool.agent-harness.addons]
namespace = "addons"
package-data = {{ one = ["assets/*.json"] }}
repo-files = {{ one = ["shared/manifest.json"] }}
"""


def _grid_project(root: Path, outside: Path, git: bool = True) -> None:
    """The grid's project with every input present (and tracked, in a checkout), and beside it a copy that a link
    could lead to. Each file says which copy it is."""
    for where, folder in (("inside", root), ("outside", outside)):
        files = {path: f"# <{kind}: {where}>\n" for kind, path in GRID.items()}
        files.update({"pyproject.toml": GRID_PYPROJECT.format(marker=f"<pyproject.toml: {where}>"),
                      "release/version.py": f'RELEASE = "{"2.3.4" if where == "inside" else "6.6.6"}"\n',
                      "pkg/__init__.py": "", "pkg/assets/other.json": "{}\n",
                      "addons/one/__init__.py": 'MODULE = Module(title="One")\n', "addons/one/assets/other.json": "{}\n"})
        for path, text in files.items():
            (folder / path).parent.mkdir(parents=True, exist_ok=True)
            (folder / path).write_text(text, encoding="utf-8")
    if git:
        _git(root, "init", "-q")
        _git(root, "add", "-A")


def _git_out(root: Path, *args: str, stdin: bytes | None = None) -> str:
    return subprocess.run(["git", "-C", str(root), *args], input=stdin, capture_output=True,
                          check=True).stdout.decode("utf-8").strip()


def _unlistable(patch: pytest.MonkeyPatch, folder: Path) -> None:
    """Make listing `folder` fail the way a folder without read permission does (Windows can't set one up)."""
    real = os.scandir

    def scandir(path="."):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(folder)):
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return real(path)
    patch.setattr(os, "scandir", scandir)


def _wheels(root: Path, out: Path) -> list[Path]:
    return build.build_all(root, out)


def _sdist(root: Path, out: Path) -> Path:
    tree = build.Tree(root)
    pyproject = build.load_pyproject(tree)
    return build.write_sdist(tree, build.core_distribution(tree, pyproject), pyproject, out)


def _unpacked(paths: list[Path]) -> bytes:
    """The names and unpacked contents of wheels and source distributions, in one blob to search."""
    blob = []
    for path in paths:
        blob.append(path.name.encode())
        if path.suffix == ".whl":
            with zipfile.ZipFile(path) as z:
                blob += [z.read(name) for name in z.namelist()]
        else:
            with tarfile.open(path) as archive:
                blob += [archive.extractfile(m).read() for m in archive.getmembers() if m.isfile()]
    return b"\n".join(blob)


def _grid_cell(base: Path, patch: pytest.MonkeyPatch, kind: str, state: str) -> tuple[bytes, str | None, bytes]:
    """Set up one cell of the grid and build it. Returns what was written, as one blob; the refusal, or None when the
    build went through; and the text a recorded link holds on the disk (empty in every other state)."""
    path = GRID[kind]
    parent = PurePosixPath(path).parent.as_posix()
    root, outside, out = base / "project", base / "outside", base / "out"
    _grid_project(root, outside, git=state != "unlistable folder")
    placeholder = b""
    if state == "untracked":
        _git(root, "rm", "-q", "--cached", path)
    elif state == "linked file":
        (root / path).unlink()
        _link(root / path, outside / path)
    elif state == "linked parent":
        shutil.rmtree(root / parent)
        _link(root / parent, outside / parent)
    elif state == "recorded link":  # a Windows checkout without symlinks: mode 120000 in git, the link's text on disk
        placeholder = os.path.relpath(outside / path, (root / path).parent).replace(os.sep, "/").encode()
        (root / path).write_bytes(placeholder)
        blob = _git_out(root, "hash-object", "-w", "--stdin", stdin=placeholder)
        _git(root, "update-index", "--add", "--cacheinfo", f"120000,{blob},{path}")
    elif state == "unlistable folder":  # outside a checkout the build walks each folder, and this one can't be listed
        _unlistable(patch, root / parent)
    if state in ("linked file", "linked parent"):
        _git(root, "add", "-A")  # committed, as a clone of a repository holding links has them
    if state == "tracked" or (state == "untracked" and kind in LISTED):
        return _unpacked([*_wheels(root, out), _sdist(root, out)]), None, placeholder
    door = _sdist if kind == "sdist entry" else _wheels  # the wheels never read an sdist entry
    try:
        door(root, out)
    except build.BuildError as refused:
        return _unpacked(sorted(out.glob("*"))), str(refused), placeholder
    return _unpacked(sorted(out.glob("*"))), None, placeholder


@pytest.mark.parametrize(("kind", "state"), GRID_CELLS, ids=[f"{k}-{s}".replace(" ", "_") for k, s in GRID_CELLS])
def test_every_input_is_read_while_tracked_and_never_otherwise(tmp_path, monkeypatch, kind, state):
    """The review of #1001 (r1, r2, r4): every byte the wheels, the sdist or their metadata hold comes through Tree. So
    each input is read while git tracks it, and refused when it's untracked, a link (on the disk or recorded by git),
    behind a linked folder, or in a folder that can't be listed; a file found by listing a folder is left out instead
    when untracked. Nothing from the copy outside, and no recorded link's text, may reach anything written."""
    made, refusal, placeholder = _grid_cell(tmp_path, monkeypatch, kind, state)
    path = GRID[kind]
    parent = PurePosixPath(path).parent.as_posix()
    if state == "tracked" or (state == "untracked" and kind in LISTED):
        evidence = b"Version: 2.3.4" if kind == "version-from" else f"<{kind}: inside>".encode()
        assert refusal is None and (evidence in made) == (state == "tracked"), (kind, state, refusal)
    else:
        reason = {"untracked": f"{path} is not tracked by git", "linked file": f"link at {path}",
                  "linked parent": f"link at {parent}", "recorded link": f"link at {path}",
                  "unlistable folder": "could not be listed"}[state]
        assert refusal is not None and reason in refusal, (kind, state, refusal)
        assert state != "unlistable folder" or PurePosixPath(parent).name in refusal
    assert b": outside>" not in made and b"6.6.6" not in made
    assert not placeholder or placeholder not in made


def _refuse_command_line(base: Path, patch: pytest.MonkeyPatch) -> None:
    base.mkdir(parents=True, exist_ok=True)
    build.Tree.for_command_line(str(base))


def _refuse_unlistable_checkout(base: Path, patch: pytest.MonkeyPatch) -> None:
    root = _layout(base)
    (root / ".git").mkdir()

    def no_git(*args, **kwargs):
        raise OSError("git: not found")
    patch.setattr(build.subprocess, "run", no_git)
    build.Tree(root)


def _refuse_unreadable(base: Path, patch: pytest.MonkeyPatch) -> None:
    root = _layout(base)
    real = os.lstat

    def lstat(path, *args, **kwargs):
        if os.path.basename(path) == "__init__.py":
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return real(path, *args, **kwargs)
    patch.setattr(os, "lstat", lstat)
    build.core_distribution(root)


def _refuse_link_on_disk(base: Path, patch: pytest.MonkeyPatch) -> None:
    """Outside a checkout, git records nothing: the disk's own link."""
    root, outside = _layout(base / "project"), base / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("TOKEN = 'not for a wheel'\n", encoding="utf-8")
    build.Tree(root).read(_link(root / "pkg" / "leak.py", outside / "secret.py").relative_to(root).as_posix())


def _refuse_linked_folder_walked(base: Path, patch: pytest.MonkeyPatch) -> None:
    root, outside = _layout(base / "project"), base / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("TOKEN = 'not for a wheel'\n", encoding="utf-8")
    _link(root / "pkg" / "sub", outside)
    build.core_distribution(root)


def _refuse_missing(base: Path, patch: pytest.MonkeyPatch) -> None:
    build.core_distribution(_layout(base, 'modules = { gone = "gone.py" }\n'))


def _refuse_folder_for_file(base: Path, patch: pytest.MonkeyPatch) -> None:
    root = _layout(base, 'modules = { folder = "folder.py" }\n')
    (root / "folder.py").mkdir()
    build.core_distribution(root)


# Every other refusal Tree makes, with the reason it must give: the grid's states don't reach these.
REFUSALS = {
    "command line without pyproject.toml": (_refuse_command_line, "is not a checkout with a pyproject.toml"),
    "checkout git can't list": (_refuse_unlistable_checkout, "could not list its tracked files"),
    "unreadable path": (_refuse_unreadable, "pkg/__init__.py could not be read"),
    "link on the disk outside a checkout": (_refuse_link_on_disk, "link at pkg/leak.py (links are not packaged)"),
    "linked folder met while walking": (_refuse_linked_folder_walked, "pkg/sub is a link to a folder"),
    "missing file": (_refuse_missing, "gone.py is missing"),
    "folder where a file should be": (_refuse_folder_for_file, "folder.py is not a regular file"),
}


@pytest.mark.parametrize("name", list(REFUSALS), ids=[name.replace(" ", "_") for name in REFUSALS])
def test_each_other_refusal_gives_its_reason(tmp_path, monkeypatch, name):
    refuse, reason = REFUSALS[name]
    with pytest.raises(build.BuildError) as refused:
        refuse(tmp_path, monkeypatch)
    assert reason in str(refused.value)


def _tree_refusals() -> set[int]:
    """The line of each `raise BuildError` in the backend's Tree."""
    module = ast.parse(BACKEND.read_text(encoding="utf-8"))
    tree = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "Tree")
    return {node.lineno for node in ast.walk(tree) if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
            and isinstance(node.exc.func, ast.Name) and node.exc.func.id == "BuildError"}


def _code_objects(code: types.CodeType):
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from _code_objects(const)


@contextlib.contextmanager
def _lines_run(cls: type):
    """Yield the set of line numbers of `cls`'s code that run inside the block. sys.monitoring counts them, so the
    trace function, and the coverage run that uses it, are left alone."""
    monitoring = sys.monitoring
    tool = next(tool for tool in (4, 3, 5) if monitoring.get_tool(tool) is None)
    functions = [getattr(value, "__func__", value) for value in vars(cls).values()]  # a classmethod holds its function
    codes = [code for function in functions if isinstance(function, types.FunctionType)
             for code in _code_objects(function.__code__)]
    ran: set[int] = set()
    monitoring.use_tool_id(tool, "test_packaging: Tree's refusals")
    monitoring.register_callback(tool, monitoring.events.LINE, lambda code, line: ran.add(line))
    for code in codes:
        monitoring.set_local_events(tool, code, monitoring.events.LINE)
    try:
        yield ran
    finally:
        for code in codes:
            monitoring.set_local_events(tool, code, 0)
        monitoring.register_callback(tool, monitoring.events.LINE, None)
        monitoring.free_tool_id(tool)


def test_every_refusal_in_tree_runs(tmp_path, monkeypatch):
    """The review of #1001 (r4) found refusals that Tree made and no test did. This runs every grid cell and every other
    refusal with a line counter on Tree, and fails if any `raise BuildError` in Tree never ran, so no refusal lands
    without a test that makes it."""
    with _lines_run(build.Tree) as ran:
        for n, (kind, state) in enumerate(GRID_CELLS):
            with monkeypatch.context() as patch:
                _grid_cell(tmp_path / f"cell{n}", patch, kind, state)
        for n, (refuse, _reason) in enumerate(REFUSALS.values()):
            with monkeypatch.context() as patch, pytest.raises(build.BuildError):
                refuse(tmp_path / f"refusal{n}", patch)
    never = sorted(_tree_refusals() - ran)
    assert not never, ("These `raise BuildError` lines in Tree never ran: give each a grid state or a REFUSALS entry "
                       f"that reaches it: {never}")


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
    """`python -m harness` with no arguments: it finds the per-user install's config itself. (Not the console script:
    on Windows that is a launcher process with the interpreter as its child.)"""

    def __init__(self, installed: Installed, port: int, data_dir: Path):
        self.port, self.data_dir = port, data_dir
        self.log = installed.root / f"server-{time.monotonic_ns()}.log"
        with self.log.open("w", encoding="utf-8") as log:
            self.proc = subprocess.Popen([str(installed.python), "-m", "harness"], cwd=installed.work,
                                         env=installed.env, stdout=log, stderr=subprocess.STDOUT)
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
    assert "agent-harness daemon" in env.run(env.script("harness-server"), "--help").stdout  # the console scripts
    assert "usage:" in env.run(env.script("harness"), "--help").stdout
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
        # The installed CLI finds this install's port (not 8100) and local token by itself (the review of #1001).
        assert env.run(env.script("harness"), "list").returncode == 0
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
    port = _free_port()  # a new one: on Windows the last server's closed connections can hold the old port a while
    env.run(str(env.python), "-m", "harness.setup_config", "--config-dir", probe["config_dir"],
            "--data-dir", str(data_dir), "--profile", "service", "--port", str(port), "--force",
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
            json.loads((ROOT / "docs/hub.entries.example.json").read_text(encoding="utf-8"))["entries"])
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
