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
- A wheel holds the `.py` sources of its packages and the files the tables list, nothing else, so an untracked or
  private file in a package folder never ships. Repository files an add-on reads from outside its package
  (`repo-files`) go into the package's `_repo_files/`, where `harness.modules.repo_file` finds them.

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
    license_text: bytes = b""
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


def load_pyproject(source: Path) -> dict:
    try:
        return tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise BuildError(f"pyproject.toml: {e}") from None


def _settings(pyproject: dict) -> dict:
    settings = pyproject.get("tool", {}).get("agent-harness")
    if not isinstance(settings, dict):
        raise BuildError("pyproject.toml has no [tool.agent-harness] tables")
    return settings


def project_version(source: Path, pyproject: dict) -> str:
    """The static [project] version, or the Server's release (harness/compat.py RELEASE) when version is dynamic."""
    project = pyproject.get("project", {})
    if "version" in project:
        version = str(project["version"])
    elif "version" in project.get("dynamic", []):
        origin = _settings(pyproject).get("build", {}).get("version-from", "")
        match = RELEASE.search(_read(source, origin).decode("utf-8")) if origin else None
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


def _read(source: Path, path: str) -> bytes:
    """A regular file of the tree. A link is refused: its target could be anything on the building machine."""
    full = source / _relative(path, "file")
    try:
        mode = os.lstat(full).st_mode
    except OSError:
        raise BuildError(f"{path} is missing") from None
    if not stat.S_ISREG(mode):
        raise BuildError(f"{path} is not a regular file (links are not packaged)")
    return full.read_bytes()


def _matches(rel: str, pattern: str) -> bool:
    """Glob match by path segment: `*` stays within one folder, and the whole path must match."""
    parts, globs = rel.split("/"), pattern.split("/")
    return len(parts) == len(globs) and all(fnmatch.fnmatchcase(p, g) for p, g in zip(parts, globs, strict=True))


def _package_files(source: Path, package: str, data_globs: list[str]) -> dict[str, bytes]:
    """Every .py source under `package` and the files matching `data_globs` (relative to the repository root).
    Hidden files and folders and __pycache__ are never visited."""
    root = source / _relative(package, "package")
    if not (root / "__init__.py").is_file():
        raise BuildError(f"{package}/ is not a package (no __init__.py)")
    files: dict[str, bytes] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__pycache__")
        for name in dirnames:
            if os.path.islink(os.path.join(dirpath, name)):
                raise BuildError(f"{(Path(dirpath) / name).relative_to(source).as_posix()} is a link to a folder "
                                 "(links are not packaged)")
        for name in sorted(filenames):
            rel = (Path(dirpath) / name).relative_to(source).as_posix()
            if name.startswith("."):
                continue
            if name.endswith(".py") or any(_matches(rel, pattern) for pattern in data_globs):
                files[rel] = _read(source, rel)
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


def core_distribution(source: Path, pyproject: dict | None = None) -> Distribution:
    """The `agent-harness` wheel: the harness package and the SDK module."""
    pyproject = pyproject or load_pyproject(source)
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
        files.update(_package_files(source, package, list(build.get("package-data", []))))
    for module, path in build.get("modules", {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module) or not path.endswith(".py"):
            raise BuildError(f"tool.agent-harness.build.modules: {module} = {path!r} is not a module and a .py file")
        files[f"{module}.py"] = _read(source, path)
    if not files:
        raise BuildError("tool.agent-harness.build lists no packages or modules")
    readme, license = project.get("readme", ""), project.get("license", "")
    if not isinstance(readme, str) or not isinstance(license, str):
        raise BuildError("pyproject.toml: readme and license must be strings (a file name, an SPDX expression)")
    license_files = project.get("license-files", [])
    return Distribution(
        name=name, version=project_version(source, pyproject), summary=project.get("description", ""),
        requires_python=project.get("requires-python", ""), requires=requires, files=files, license=license,
        license_text=b"\n".join(_read(source, path) for path in license_files),
        readme=_read(source, readme).decode("utf-8") if readme else "", extras=extras,
        scripts={str(k): str(v) for k, v in project.get("scripts", {}).items()})


def addon_names(source: Path, pyproject: dict | None = None) -> list[str]:
    """The packages under the add-on namespace, by name."""
    namespace = _settings(pyproject or load_pyproject(source)).get("addons", {}).get("namespace", "")
    root = source / _relative(namespace, "tool.agent-harness.addons.namespace")
    if (root / "__init__.py").exists():
        raise BuildError(f"{namespace}/__init__.py exists: the add-on namespace must stay a PEP 420 namespace")
    return sorted(p.name for p in root.iterdir() if (p / "__init__.py").is_file() and not p.name.startswith("."))


def _module_title(init: bytes) -> str:
    """The `title=` of the package's `MODULE = Module(...)`, read without importing it."""
    for node in ast.walk(ast.parse(init)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Module":
            for keyword in node.keywords:
                if keyword.arg == "title" and isinstance(keyword.value, ast.Constant):
                    return str(keyword.value.value)
    return ""


def addon_distribution(source: Path, name: str, pyproject: dict | None = None,
                       core: Distribution | None = None) -> Distribution:
    """The `agent-harness-module-<name>` wheel: one harness_modules package, requiring exactly this core version."""
    pyproject = pyproject or load_pyproject(source)
    addons = _settings(pyproject).get("addons", {})
    core = core or core_distribution(source, pyproject)
    package = f"{addons.get('namespace', '')}/{name}"
    data = [f"{package}/{pattern}" for pattern in addons.get("package-data", {}).get(name, [])]
    files = _package_files(source, package, data)
    for path in addons.get("repo-files", {}).get(name, []):
        files[f"{package}/{REPO_FILES}/{_relative(path, 'repo file')}"] = _read(source, path)
    title = _module_title(files[f"{package}/__init__.py"]) or name
    return Distribution(
        name=str(addons.get("distribution", "agent-harness-module-{name}")).format(name=name.replace("_", "-")),
        version=core.version, summary=f"Agent Harness add-on module: {title} (harness_modules.{name})",
        requires_python=core.requires_python, requires=[f"{core.name}=={core.version}"], files=files,
        license=core.license, license_text=core.license_text)


def unknown_repo_file_addons(source: Path, pyproject: dict | None = None) -> list[str]:
    """`repo-files` or `package-data` entries naming a package that isn't under the namespace (a typo ships nothing)."""
    pyproject = pyproject or load_pyproject(source)
    addons = _settings(pyproject).get("addons", {})
    known = set(addon_names(source, pyproject))
    return sorted((set(addons.get("repo-files", {})) | set(addons.get("package-data", {}))) - known)


# ------------------------------------------------------------------------------------------------- wheel writing

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
    if dist.license_text:
        files[f"{dist.dist_info}/licenses/LICENSE"] = dist.license_text
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


def write_wheel(dist: Distribution, out: Path) -> Path:
    """Write `dist` to `out` and return the wheel's path."""
    out.mkdir(parents=True, exist_ok=True)
    entries = {**dist.files, **dist_info_files(dist)}
    record = f"{dist.dist_info}/RECORD"
    lines = [f"{name},{_digest(data)},{len(data)}" for name, data in sorted(entries.items())] + [f"{record},,"]
    entries[record] = ("\n".join(lines) + "\n").encode("utf-8")
    target = out / dist.wheel_name
    epoch = _epoch()
    temporary = target.with_name(target.name + ".part")
    with zipfile.ZipFile(temporary, "w") as wheel:
        for name in sorted(entries, key=lambda n: (n.startswith(dist.dist_info), n)):  # .dist-info last
            wheel.writestr(_zip_info(name, epoch), entries[name])
    temporary.replace(target)
    return target


def write_sdist(source: Path, dist: Distribution, pyproject: dict, out: Path) -> Path:
    """The core's source distribution: what building its wheel again needs, under <name>-<version>/."""
    sdist = _settings(pyproject).get("build", {}).get("sdist", [])
    base = f"{dist.escaped}-{dist.version}"
    entries = {f"{base}/{rel}": data for rel, data in dist.files.items() if rel.startswith(tuple(
        f"{package}/" for package in _settings(pyproject).get("build", {}).get("packages", [])))}
    for path in ["pyproject.toml", *sdist]:
        entries[f"{base}/{_relative(path, 'sdist file')}"] = _read(source, path)
    entries[f"{base}/PKG-INFO"] = _metadata(dist)
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"{base}.tar.gz"
    epoch = _epoch()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name in sorted(entries):
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(entries[name]), epoch, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(entries[name]))
    target.write_bytes(gzip.compress(raw.getvalue(), mtime=epoch))
    return target


def build_all(source: Path, out: Path) -> list[Path]:
    """The core wheel, then one wheel per add-on module, in `out`."""
    pyproject = load_pyproject(source)
    stray = unknown_repo_file_addons(source, pyproject)
    if stray:
        raise BuildError(f"tool.agent-harness.addons names packages that don't exist: {', '.join(stray)}")
    core = core_distribution(source, pyproject)
    return [write_wheel(core, out)] + [write_wheel(addon_distribution(source, name, pyproject, core), out)
                                       for name in addon_names(source, pyproject)]


# ------------------------------------------------------------------------------------------------- PEP 517 hooks

def get_requires_for_build_wheel(config_settings=None) -> list[str]:
    return []


def get_requires_for_build_sdist(config_settings=None) -> list[str]:
    return []


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None) -> str:
    dist = core_distribution(Path.cwd())
    for name, data in dist_info_files(dist).items():
        path = Path(metadata_directory) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return dist.dist_info


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None) -> str:
    return write_wheel(core_distribution(Path.cwd()), Path(wheel_directory)).name


def build_sdist(sdist_directory, config_settings=None) -> str:
    source = Path.cwd()
    pyproject = load_pyproject(source)
    return write_sdist(source, core_distribution(source, pyproject), pyproject, Path(sdist_directory)).name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the core wheel and every add-on module wheel.")
    parser.add_argument("--out", default="dist", help="directory for the wheels (default: dist)")
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[2]),
                        help="the repository checkout to build (default: this script's)")
    args = parser.parse_args(argv)
    try:
        for path in build_all(Path(args.source), Path(args.out)):
            print(path)
    except BuildError as e:
        print(f"build failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
