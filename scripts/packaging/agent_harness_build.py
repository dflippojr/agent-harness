"""Build Agent Harness Server's wheels from this repository, with the standard library only (#334, stage e).

pyproject.toml names this file as its PEP 517 build backend, so `pip wheel .`, `pip install .` and `python -m build`
build the core distribution. One command builds the core wheel and every add-on wheel:

    python scripts/packaging/agent_harness_build.py --out dist

The layout comes from pyproject.toml's `[tool.agent-harness]` tables:

- The core wheel, `agent-harness`, holds the `harness` package and the one-file SDK as the top-level module
  `harness_client`, its documented import.
- Every package under the `harness_modules` namespace is an add-on wheel of its own, `agent-harness-module-<name>`,
  which requires the same core version. A module is the unit the module interface adds or removes
  (docs/modules.md), and no module imports another, so a module's wheel needs only the core.
  The namespace has no `__init__.py` in any wheel, so each wheel adds its own package to it.
- A wheel holds the `.py` sources of its packages and the files the tables list, nothing else. Repository files an
  add-on reads from outside its package (`repo-files`) go into the package's `_repo_files/`, where
  `harness.modules.repo_file` finds them.

Every input, pyproject.toml included, is read through `Tree`, and every artifact is written through `Out`: nothing
else here touches the disk (tests/test_packaging.py scans for any other access). From a git checkout `Tree` reads only
the files git tracks, so an untracked or ignored file never ships, and a source distribution holds only what such a
build selected. No read follows a link anywhere on its path.

Builds need no network and no setuptools, and are reproducible: the same tree gives the same bytes (entries are
sorted, and timestamps come from SOURCE_DATE_EPOCH or a fixed date).
"""

from __future__ import annotations

import argparse
import ast
import base64
import fnmatch
import gzip
import hashlib
import io
import os
import re
import stat
import subprocess
import sys
import tarfile
import time
import tomllib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

BACKEND = "agent_harness_build"
REPO_FILES = "_repo_files"  # harness.modules.REPO_FILES: where an add-on carries the repository files it reads
TAG = "py3-none-any"
FIXED_EPOCH = 315532800  # 1980-01-01, the earliest time a zip entry can carry
VERSION = re.compile(r"^\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?$")
RELEASE = re.compile(r'^RELEASE\s*=\s*"([^"]+)"', re.MULTILINE)
REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


class BuildError(Exception):
    """The tree or pyproject.toml can't produce a correct wheel. The message names the file or the setting."""


@dataclass
class Distribution:
    """One wheel: its metadata and its files, as {path in the wheel: bytes}."""
    name: str
    version: str
    summary: str
    requires_python: str
    requires: list[str]
    files: dict[str, bytes]
    license: str = ""
    license_files: dict[str, bytes] = field(default_factory=dict)  # [project] license-files, by repository path
    readme: str = ""
    extras: dict[str, list[str]] = field(default_factory=dict)
    scripts: dict[str, str] = field(default_factory=dict)

    @property
    def escaped(self) -> str:
        return re.sub(r"[-_.]+", "_", self.name).lower()

    @property
    def dist_info(self) -> str:
        return f"{self.escaped}-{self.version}.dist-info"

    @property
    def wheel_name(self) -> str:
        return f"{self.escaped}-{self.version}-{TAG}.whl"


class Tree:
    """The files a build may read, and the one way it reads them. Apart from `Out`, which writes and reads nothing,
    nothing else in this file touches the disk, so pyproject.toml and every other input pass the same checks.

    In a git checkout the candidates are the files git tracks (staged ones included), so nothing untracked or ignored
    can reach a wheel. That holds for a project inside a bigger checkout too, with no .git of its own. A folder git
    tracks nothing in is taken as it is: an unpacked source distribution, which holds only what such a build selected,
    or the image build's context. Hidden files and folders and __pycache__ are never candidates.

    A link is never followed, wherever it is on a path: a linked file, folder or package root could lead anywhere on
    the building machine. A link on the disk or one git records is refused: a read through it, or one met while listing
    a folder, rather than left out quietly. (A Windows checkout without symlinks holds a recorded link as a plain
    file.) The tree is the builder's own: a process that can change it during a build can change tracked files too, so
    the checks don't guard against a race."""

    source: Path
    tracked: frozenset[str] | None  # None: no checkout here, so every file is a candidate
    links: frozenset[str]  # the paths git records as a link or a submodule

    def __init__(self, source: Path | Tree):
        if isinstance(source, Tree):
            self.source, self.tracked, self.links = source.source, source.tracked, source.links
            return
        self.source, self.tracked, self.links = Path(source), None, frozenset()
        own = os.path.lexists(self.source / ".git")
        if own or any(os.path.lexists(folder / ".git") for folder in Path(os.path.realpath(self.source)).parents):
            regular, links = self._git_listing()
            if own or regular or links:  # git tracks nothing in a bigger checkout's folder: not a checkout here
                self.tracked, self.links = regular, links

    @classmethod
    def here(cls) -> Tree:
        """The tree a PEP 517 frontend runs the backend in: the working directory is the project's root."""
        return cls(Path.cwd())

    @classmethod
    def for_command_line(cls, source: str) -> Tree:
        """The command line's --source, checked before anything is read: it must hold a pyproject.toml."""
        root = os.path.realpath(source)
        if not os.path.isfile(os.path.join(root, "pyproject.toml")):
            raise BuildError(f"--source {source!r} is not a checkout with a pyproject.toml")
        return cls(Path(root))

    def _git_listing(self) -> tuple[frozenset[str], frozenset[str]]:
        """What git tracks under the source, by path relative to it: (regular files, links and submodules)."""
        try:  # the source is git's working directory, so no path ever lands on its command line
            out = subprocess.run(["git", "ls-files", "-z", "--cached", "--stage"], cwd=self.source,
                                 capture_output=True, check=True, timeout=120).stdout.decode("utf-8")
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as e:
            raise BuildError(f"{self.source} is in a git checkout, but git could not list its tracked files: "
                             f"{e}") from None
        regular, links = set(), set()
        for entry in filter(None, out.split("\0")):  # "<mode> <object> <stage>\t<path>"
            meta, _, name = entry.partition("\t")
            (regular if meta.split(" ")[0] in ("100644", "100755") else links).add(name)
        return frozenset(regular), frozenset(links)

    def _no_link_on(self, rel: PurePosixPath, path: str) -> int | None:
        """Refuse a link anywhere on `rel`, recorded by git or on the disk. Returns the st_mode of the last part, or
        None when it doesn't exist."""
        prefixes = ["/".join(rel.parts[:depth]) for depth in range(1, len(rel.parts) + 1)]
        for where in prefixes:
            if where in self.links:
                raise BuildError(f"{path} goes through a link at {where}: git records a link or a submodule there "
                                 "(neither is packaged)")
        mode = None
        for where in prefixes:
            try:
                mode = os.lstat(self.source / where).st_mode
            except (FileNotFoundError, NotADirectoryError):
                return None
            except OSError as e:
                raise BuildError(f"{path} could not be read: {e}") from None
            if stat.S_ISLNK(mode) or os.path.isjunction(self.source / where):
                raise BuildError(f"{path} goes through a link at {where} (links are not packaged)")
        return mode

    def _walk(self, rel: PurePosixPath) -> list[str]:
        """Every file under `rel`, outside a checkout."""
        found = []

        def unreadable(error: OSError) -> None:
            raise BuildError(f"{rel}/ could not be listed: {error}")
        for dirpath, dirnames, filenames in os.walk(self.source / rel, onerror=unreadable):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__pycache__")
            for name in dirnames:  # the walk wouldn't enter one; say so rather than leave its files out quietly
                folder = Path(dirpath) / name
                if folder.is_symlink() or os.path.isjunction(folder):
                    where = folder.relative_to(self.source).as_posix()
                    raise BuildError(f"{where} is a link to a folder (links are not packaged)")
            found += [(Path(dirpath) / name).relative_to(self.source).as_posix() for name in filenames]
        return found

    def files(self, top: str) -> list[str]:
        """The candidate files under the folder `top`, as repository-relative posix paths. A link met there, file or
        folder, is refused."""
        rel = _relative(top, "folder")
        exists = self._no_link_on(rel, f"{top}/") is not None
        if self.tracked is not None:
            found = [name for name in self.tracked | self.links if name.startswith(f"{rel}/")]
        else:
            found = self._walk(rel) if exists else []
        candidates = sorted(name for name in found
                            if not any(part.startswith(".") or part == "__pycache__" for part in name.split("/")))
        for name in candidates:
            self._no_link_on(PurePosixPath(name), name)
        return candidates

    def read(self, path: str) -> bytes:
        """The bytes of the file `path`, refused when a link is anywhere on its path, when a checkout doesn't track it,
        and when it isn't a regular file."""
        rel = _relative(path, "file")
        mode = self._no_link_on(rel, path)
        if mode is None:
            raise BuildError(f"{path} is missing")
        if self.tracked is not None and rel.as_posix() not in self.tracked:
            raise BuildError(f"{path} is not tracked by git (a build from a checkout ships tracked files only)")
        if not stat.S_ISREG(mode):
            raise BuildError(f"{path} is not a regular file")
        return (self.source / rel).read_bytes()


def load_pyproject(source: Path | Tree) -> dict:
    """pyproject.toml, read through the tree like every other input."""
    try:
        return tomllib.loads(Tree(source).read("pyproject.toml").decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise BuildError(f"pyproject.toml: {e}") from None


def _settings(pyproject: dict) -> dict:
    settings = pyproject.get("tool", {}).get("agent-harness")
    if not isinstance(settings, dict):
        raise BuildError("pyproject.toml has no [tool.agent-harness] tables")
    return settings


def project_version(source: Path | Tree, pyproject: dict) -> str:
    """The static [project] version, or the Server's release (harness/compat.py RELEASE) when version is dynamic."""
    project = pyproject.get("project", {})
    if "version" in project:
        version = str(project["version"])
    elif "version" in project.get("dynamic", []):
        origin = _settings(pyproject).get("build", {}).get("version-from", "")
        match = RELEASE.search(Tree(source).read(origin).decode("utf-8")) if origin else None
        if match is None:
            raise BuildError(f"no RELEASE = \"x.y.z\" in {origin or 'tool.agent-harness.build.version-from'}")
        version = match.group(1)
    else:
        raise BuildError("pyproject.toml: [project] needs a version, or version in dynamic")
    if not VERSION.match(version):
        raise BuildError(f"version {version!r} is not a release number such as 1.2.3")
    return version


def _relative(path: str, what: str) -> PurePosixPath:
    rel = PurePosixPath(path)
    if not path or rel.is_absolute() or ".." in rel.parts or "\\" in path or ":" in path:
        raise BuildError(f"{what} {path!r} must be a relative path inside the repository")
    return rel


def _matches(rel: str, pattern: str) -> bool:
    """Glob match by path segment: `*` stays within one folder, and the whole path must match."""
    parts, globs = rel.split("/"), pattern.split("/")
    return len(parts) == len(globs) and all(fnmatch.fnmatchcase(p, g) for p, g in zip(parts, globs, strict=True))


def _package_files(tree: Tree, package: str, data_globs: list[str]) -> dict[str, bytes]:
    """Every .py source under `package` and the candidate files matching `data_globs` (relative to the repository
    root), read through `tree`, which refuses links."""
    candidates = tree.files(package)
    if f"{_relative(package, 'package')}/__init__.py" not in candidates:
        raise BuildError(f"{package}/ is not a package (no __init__.py)")
    files = {rel: tree.read(rel) for rel in candidates
             if rel.endswith(".py") or any(_matches(rel, pattern) for pattern in data_globs)}
    for pattern in data_globs:
        if not any(_matches(rel, pattern) for rel in files):
            raise BuildError(f"package data {pattern!r} matches no file under {package}/")
    return files


def _requirement_names(requirements: list[str]) -> list[str]:
    names = []
    for requirement in requirements:
        match = REQUIREMENT_NAME.match(requirement)
        if match is None:
            raise BuildError(f"dependency {requirement!r} is not a requirement such as name>=1.0")
        names.append(match.group(1))
    return names


def core_distribution(source: Path | Tree, pyproject: dict | None = None) -> Distribution:
    """The `agent-harness` wheel: the harness package and the SDK module."""
    tree = Tree(source)
    pyproject = pyproject or load_pyproject(tree)
    project = pyproject.get("project", {})
    build = _settings(pyproject).get("build", {})
    name = project.get("name")
    if not name:
        raise BuildError("pyproject.toml: [project] needs a name")
    requires = [str(item) for item in project.get("dependencies", [])]
    _requirement_names(requires)
    extras = {str(extra): [str(item) for item in items]
              for extra, items in project.get("optional-dependencies", {}).items()}
    files: dict[str, bytes] = {}
    for package in build.get("packages", []):
        files.update(_package_files(tree, package, list(build.get("package-data", []))))
    for module, path in build.get("modules", {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module) or not path.endswith(".py"):
            raise BuildError(f"tool.agent-harness.build.modules: {module} = {path!r} is not a module and a .py file")
        files[f"{module}.py"] = tree.read(path)
    if not files:
        raise BuildError("tool.agent-harness.build lists no packages or modules")
    readme, license = project.get("readme", ""), project.get("license", "")
    if not isinstance(readme, str) or not isinstance(license, str):
        raise BuildError("pyproject.toml: readme and license must be strings (a file name, an SPDX expression)")
    license_files = project.get("license-files", [])
    return Distribution(
        name=name, version=project_version(tree, pyproject), summary=project.get("description", ""),
        requires_python=project.get("requires-python", ""), requires=requires, files=files, license=license,
        license_files={str(_relative(path, "license file")): tree.read(path) for path in license_files},
        # Text, so the same on every checkout: a Windows clone has CRLF line ends in README.md.
        readme=tree.read(readme).decode("utf-8").replace("\r\n", "\n") if readme else "", extras=extras,
        scripts={str(k): str(v) for k, v in project.get("scripts", {}).items()})


def addon_names(source: Path | Tree, pyproject: dict | None = None) -> list[str]:
    """The packages under the add-on namespace, by name: each folder whose __init__.py is a candidate file."""
    tree = Tree(source)
    namespace = _settings(pyproject or load_pyproject(tree)).get("addons", {}).get("namespace", "")
    prefix = f"{_relative(namespace, 'tool.agent-harness.addons.namespace')}/"
    candidates = tree.files(namespace)
    if f"{prefix}__init__.py" in candidates:
        raise BuildError(f"{namespace}/__init__.py exists: the add-on namespace must stay a PEP 420 namespace")
    return sorted({rel.removeprefix(prefix).split("/")[0] for rel in candidates
                   if rel.count("/") == prefix.count("/") + 1 and rel.endswith("/__init__.py")})


def _module_title(init: bytes) -> str:
    """The `title=` of the package's `MODULE = Module(...)`, read without importing it."""
    for node in ast.walk(ast.parse(init)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Module":
            for keyword in node.keywords:
                if keyword.arg == "title" and isinstance(keyword.value, ast.Constant):
                    return str(keyword.value.value)
    return ""


def addon_distribution(source: Path | Tree, name: str, pyproject: dict | None = None,
                       core: Distribution | None = None) -> Distribution:
    """The `agent-harness-module-<name>` wheel: one harness_modules package, requiring exactly this core version."""
    tree = Tree(source)
    pyproject = pyproject or load_pyproject(tree)
    addons = _settings(pyproject).get("addons", {})
    core = core or core_distribution(tree, pyproject)
    package = f"{addons.get('namespace', '')}/{name}"
    data = [f"{package}/{pattern}" for pattern in addons.get("package-data", {}).get(name, [])]
    files = _package_files(tree, package, data)
    for path in addons.get("repo-files", {}).get(name, []):
        files[f"{package}/{REPO_FILES}/{_relative(path, 'repo file')}"] = tree.read(path)
    title = _module_title(files[f"{package}/__init__.py"]) or name
    return Distribution(
        name=str(addons.get("distribution", "agent-harness-module-{name}")).format(name=name.replace("_", "-")),
        version=core.version, summary=f"Agent Harness add-on module: {title} (harness_modules.{name})",
        requires_python=core.requires_python, requires=[f"{core.name}=={core.version}"], files=files,
        license=core.license, license_files=core.license_files)


def unknown_repo_file_addons(source: Path | Tree, pyproject: dict | None = None) -> list[str]:
    """`repo-files` or `package-data` entries naming a package that isn't under the namespace (a typo ships nothing)."""
    pyproject = pyproject or load_pyproject(source)
    addons = _settings(pyproject).get("addons", {})
    known = set(addon_names(source, pyproject))
    return sorted((set(addons.get("repo-files", {})) | set(addons.get("package-data", {}))) - known)


# ------------------------------------------------------------------------------------------------- wheel writing

class Out:
    """The folder a build writes into, and the one way it writes: each file whole, under a temporary name first and
    then renamed, so no reader meets half a wheel. It reads nothing."""

    def __init__(self, folder: str | Path | Out):
        self.folder = folder.folder if isinstance(folder, Out) else Path(folder)

    @classmethod
    def for_command_line(cls, out: str) -> Out:
        """The command line's --out, checked before anything is read or written: only a folder inside the working
        directory."""
        base, target = os.path.realpath(os.getcwd()), os.path.realpath(out)
        if target != base and not target.startswith(base + os.sep):
            raise BuildError(f"--out {out!r} must be a folder inside the working directory ({base})")
        return cls(target)

    def write(self, name: str, data: bytes) -> Path:
        """Write `data` as `name`, a relative path inside the folder, and return its path."""
        target = self.folder.joinpath(*_relative(name, "output file").parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".part")
        temporary.write_bytes(data)
        temporary.replace(target)
        return target


def _metadata(dist: Distribution) -> bytes:
    lines = ["Metadata-Version: 2.1", f"Name: {dist.name}", f"Version: {dist.version}"]
    if dist.summary:
        lines.append(f"Summary: {dist.summary}")
    if dist.license:
        lines.append(f"License: {dist.license}")
    if dist.requires_python:
        lines.append(f"Requires-Python: {dist.requires_python}")
    lines += [f"Requires-Dist: {requirement}" for requirement in dist.requires]
    for extra, requirements in sorted(dist.extras.items()):
        lines.append(f"Provides-Extra: {extra}")
        lines += [f'Requires-Dist: {requirement}; extra == "{extra}"' for requirement in requirements]
    if dist.readme:
        lines.append("Description-Content-Type: text/markdown")
    text = "\n".join(lines) + "\n"
    if dist.readme:
        text += "\n" + dist.readme
    return text.encode("utf-8")


def _wheel_file() -> bytes:
    return (f"Wheel-Version: 1.0\nGenerator: {BACKEND}\nRoot-Is-Purelib: true\nTag: {TAG}\n").encode("ascii")


def _entry_points(dist: Distribution) -> bytes:
    if not dist.scripts:
        return b""
    lines = ["[console_scripts]"] + [f"{name} = {target}" for name, target in sorted(dist.scripts.items())]
    return ("\n".join(lines) + "\n").encode("utf-8")


def dist_info_files(dist: Distribution) -> dict[str, bytes]:
    """The .dist-info files other than RECORD."""
    files = {f"{dist.dist_info}/METADATA": _metadata(dist), f"{dist.dist_info}/WHEEL": _wheel_file()}
    entry_points = _entry_points(dist)
    if entry_points:
        files[f"{dist.dist_info}/entry_points.txt"] = entry_points
    for path, data in dist.license_files.items():  # where PEP 639 puts them: licenses/<path in the project>
        files[f"{dist.dist_info}/licenses/{path}"] = data
    return files


def _digest(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")


def _epoch() -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    try:
        return max(int(raw), FIXED_EPOCH) if raw else FIXED_EPOCH
    except ValueError:
        raise BuildError(f"SOURCE_DATE_EPOCH {raw!r} is not a number of seconds") from None


def _zip_info(name: str, epoch: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=time.gmtime(epoch)[:6])
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _wheel_bytes(dist: Distribution) -> bytes:
    entries = {**dist.files, **dist_info_files(dist)}
    record = f"{dist.dist_info}/RECORD"
    lines = [f"{name},{_digest(data)},{len(data)}" for name, data in sorted(entries.items())] + [f"{record},,"]
    entries[record] = ("\n".join(lines) + "\n").encode("utf-8")
    epoch = _epoch()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as wheel:
        for name in sorted(entries, key=lambda n: (n.startswith(dist.dist_info), n)):  # .dist-info last
            wheel.writestr(_zip_info(name, epoch), entries[name])
    return buffer.getvalue()


def write_wheel(dist: Distribution, out: Path | Out) -> Path:
    """Write `dist` into `out` and return the wheel's path."""
    return Out(out).write(dist.wheel_name, _wheel_bytes(dist))


def write_sdist(source: Path | Tree, dist: Distribution, pyproject: dict, out: Path | Out) -> Path:
    """The core's source distribution: what building its wheel again needs, under <name>-<version>/."""
    tree = Tree(source)
    sdist = [*_settings(pyproject).get("build", {}).get("sdist", []), *dist.license_files]
    base = f"{dist.escaped}-{dist.version}"
    entries = {f"{base}/{rel}": data for rel, data in dist.files.items() if rel.startswith(tuple(
        f"{package}/" for package in _settings(pyproject).get("build", {}).get("packages", [])))}
    for path in ["pyproject.toml", *sdist]:
        entries[f"{base}/{_relative(path, 'sdist file')}"] = tree.read(path)
    entries[f"{base}/PKG-INFO"] = _metadata(dist)
    epoch = _epoch()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name in sorted(entries):
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(entries[name]), epoch, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(entries[name]))
    return Out(out).write(f"{base}.tar.gz", gzip.compress(raw.getvalue(), mtime=epoch))


def build_all(source: Path | Tree, out: Path | Out) -> list[Path]:
    """The core wheel, then one wheel per add-on module, in `out`."""
    tree, folder = Tree(source), Out(out)
    pyproject = load_pyproject(tree)
    stray = unknown_repo_file_addons(tree, pyproject)
    if stray:
        raise BuildError(f"tool.agent-harness.addons names packages that don't exist: {', '.join(stray)}")
    core = core_distribution(tree, pyproject)
    return [write_wheel(core, folder)] + [write_wheel(addon_distribution(tree, name, pyproject, core), folder)
                                          for name in addon_names(tree, pyproject)]


# ------------------------------------------------------------------------------------------------- PEP 517 hooks

def get_requires_for_build_wheel(config_settings=None) -> list[str]:
    return []


def get_requires_for_build_sdist(config_settings=None) -> list[str]:
    return []


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None) -> str:
    dist = core_distribution(Tree.here())
    out = Out(metadata_directory)
    for name, data in dist_info_files(dist).items():
        out.write(name, data)
    return dist.dist_info


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None) -> str:
    return write_wheel(core_distribution(Tree.here()), Out(wheel_directory)).name


def build_sdist(sdist_directory, config_settings=None) -> str:
    tree = Tree.here()
    pyproject = load_pyproject(tree)
    return write_sdist(tree, core_distribution(tree, pyproject), pyproject, Out(sdist_directory)).name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the core wheel and every add-on module wheel.")
    parser.add_argument("--out", default="dist", help="folder for the wheels, inside the working directory "
                                                      "(default: dist)")
    parser.add_argument("--source", default=str(Path(__file__).parents[2]),
                        help="the repository checkout to build (default: this script's)")
    args = parser.parse_args(argv)
    try:  # both are checked before anything is read or written
        out = Out.for_command_line(args.out)
        for path in build_all(Tree.for_command_line(args.source), out):
            print(path)
    except BuildError as e:
        print(f"build failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
