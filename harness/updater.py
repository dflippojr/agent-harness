"""Transactional updater shared by the native Mac CLI and runner."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request

try:  # package import in the repo/runner bundle; top-level import in the installed CLI bundle
    from .compat import CLIENT_PROTOCOLS, MAC_CLIENT_VERSION
except ImportError:  # pragma: no cover - exercised by the installed artifact
    from harness_compat import CLIENT_PROTOCOLS, MAC_CLIENT_VERSION


def _download(url: str, destination: Path | None = None) -> bytes:
    request = urllib.request.Request(url, headers={
        "User-Agent": f"agent-harness-updater/{MAC_CLIENT_VERSION}",
        "X-Agent-Harness-Client": f"cli/{CLIENT_PROTOCOLS['cli']}",
    })
    with urllib.request.urlopen(request, timeout=120) as response:
        if destination is None:
            return response.read()
        with destination.open("wb") as out:
            shutil.copyfileobj(response, out, length=1024 * 1024)
    return b""


def _safe_extract(package: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(package, "r:gz") as archive:
        for member in archive.getmembers():
            target = (root / member.name).resolve()
            if root != target and root not in target.parents:
                raise ValueError(f"unsafe package path: {member.name}")
            if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                raise ValueError(f"unsupported package entry: {member.name}")
        archive.extractall(root)


LAUNCH_AGENT_LABEL = "dev.agent-harness.runner"
PREVIOUS_PLIST_NAME = "previous-plist"
_BOOTOUT_ATTEMPTS = 5
_BOOTOUT_POLL_SECONDS = 1.0
_HANDOFF_DELAY_SECONDS = 1.0
_PROGRAM_ARGUMENTS_RE = re.compile(
    r"<key>ProgramArguments</key>\s*<array>(.*?)</array>", re.DOTALL)
_PLIST_STRING_RE = re.compile(r"<string>([^<]*)</string>")


def _write_result(base: Path, result: dict) -> None:
    path = base / "runner" / "last-update.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".new")
    temp.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], check=check, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _launchd_target(uid: int | None = None) -> tuple[str, str]:
    uid = os.getuid() if uid is None else uid
    domain = f"gui/{uid}"
    return domain, f"{domain}/{LAUNCH_AGENT_LABEL}"


def previous_plist_path(base: Path) -> Path:
    return base / "runner" / PREVIOUS_PLIST_NAME


def unload_launch_agent(*, uid: int | None = None) -> None:
    _, target = _launchd_target(uid)
    _launchctl("bootout", target, check=False)
    for _ in range(_BOOTOUT_ATTEMPTS):
        probe = _launchctl("print", target, check=False)
        if probe.returncode != 0:
            return
        time.sleep(_BOOTOUT_POLL_SECONDS)
    raise RuntimeError(f"launchd job {target} did not unload after bootout")


def load_launch_agent(plist: Path, *, uid: int | None = None) -> None:
    domain, target = _launchd_target(uid)
    _launchctl("bootstrap", domain, str(plist))
    _launchctl("enable", target)


def kickstart_launch_agent(*, uid: int | None = None) -> None:
    _, target = _launchd_target(uid)
    _launchctl("kickstart", "-k", target)


def _program_arguments(plist: Path) -> list[str]:
    try:
        text = plist.read_text(encoding="utf-8")
    except OSError:
        return []
    match = _PROGRAM_ARGUMENTS_RE.search(text)
    if not match:
        return []
    return _PLIST_STRING_RE.findall(match.group(1))


def verify_launch_agent_loaded(plist: Path, *, uid: int | None = None) -> None:
    _, target = _launchd_target(uid)
    probe = _launchctl("print", target, check=False)
    stdout = probe.stdout or ""
    if probe.returncode != 0:
        raise RuntimeError(f"launchd job {target} is not loaded after reload")
    missing = [arg for arg in _program_arguments(plist) if arg and arg not in stdout]
    if missing:
        raise RuntimeError(
            f"launchd job {target} is not running the installed plist "
            f"(missing {missing[0]})")


def reload_launch_agent(plist: Path, *, uid: int | None = None) -> None:
    """Unload the current launchd job and load the installed plist.

    `kickstart -k` restarts the already-loaded definition and does not pick up
    ProgramArguments or other plist changes. install.sh uses bootout + bootstrap
    for the same reason.
    """
    unload_launch_agent(uid=uid)
    load_launch_agent(plist, uid=uid)


def activate_launch_agent(plist: Path, *, definition_changed: bool,
                         uid: int | None = None) -> None:
    """Make launchd run `plist`: reload the definition when it changed, else kickstart."""
    if definition_changed:
        reload_launch_agent(plist, uid=uid)
    else:
        kickstart_launch_agent(uid=uid)
    verify_launch_agent_loaded(plist, uid=uid)


def perform_launchd_handoff(plist: Path, *, definition_changed: bool,
                            previous_plist: Path | None, base: Path,
                            uid: int | None = None) -> dict:
    """Activate the installed plist and record the outcome in last-update.json.

    Used by the CLI updater synchronously and by the Mac runner's detached helper
    after the update result has been posted. Bootstrap failure restores the
    previous plist and reloads it.
    """
    prior = {}
    try:
        prior = json.loads((base / "runner" / "last-update.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    try:
        activate_launch_agent(plist, definition_changed=definition_changed, uid=uid)
        result = {
            "ok": True,
            "version": str(prior.get("version") or ""),
            "build_id": prior.get("build_id", ""),
            "at": time.time(),
            "message": str(prior.get("message") or "Mac client updated"),
            "plist_changed": definition_changed,
        }
        _write_result(base, result)
        if previous_plist is not None and previous_plist.exists():
            previous_plist.unlink()
        return result
    except Exception as exc:
        message = f"update failed; prior launchd job restored: {exc}"
        if previous_plist is not None and previous_plist.is_file():
            shutil.copy2(previous_plist, plist)
        try:
            if plist.is_file():
                reload_launch_agent(plist, uid=uid)
                verify_launch_agent_loaded(plist, uid=uid)
        except Exception as reload_exc:
            message = f"{message}; restored launchd job failed to reload: {reload_exc}"
        result = {
            "ok": False,
            "version": str(prior.get("version") or ""),
            "build_id": prior.get("build_id", ""),
            "at": time.time(),
            "message": message,
            "plist_changed": definition_changed,
        }
        _write_result(base, result)
        return result


def schedule_launchd_handoff(plist: Path, *, definition_changed: bool,
                             previous_plist: Path | None, base: Path,
                             python: str, app_dir: Path) -> subprocess.Popen:
    """Run the shared launchd activate helper after this process can return.

    The Mac runner is the launchd job being replaced, so bootout must happen in a
    detached helper after the HTTP result is posted.
    """
    env = os.environ.copy()
    env["HARNESS_LAUNCHD_HANDOFF"] = json.dumps({
        "plist": str(plist),
        "definition_changed": bool(definition_changed),
        "previous_plist": str(previous_plist) if previous_plist else "",
        "base": str(base),
        "delay": _HANDOFF_DELAY_SECONDS,
    })
    env["HARNESS_LAUNCHD_APP"] = str(app_dir)
    code = (
        "import json,os,sys,time;"
        "from pathlib import Path;"
        "cfg=json.loads(os.environ['HARNESS_LAUNCHD_HANDOFF']);"
        "time.sleep(float(cfg.get('delay') or 0));"
        "sys.path.insert(0, os.environ['HARNESS_LAUNCHD_APP']);"
        "from harness.updater import perform_launchd_handoff;"
        "prev=cfg.get('previous_plist');"
        "perform_launchd_handoff(Path(cfg['plist']),"
        " definition_changed=bool(cfg.get('definition_changed')),"
        " previous_plist=Path(prev) if prev else None,"
        " base=Path(cfg['base']))"
    )
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "env": env}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    return subprocess.Popen([python, "-c", code], **kwargs)


RUNNER_PLIST_NAME = "dev.agent-harness.runner.plist"


def _fetch_verified_package(server: str, work: Path) -> tuple[dict, Path]:
    """Download the manifest and package, verify size and SHA-256, and extract; returns (manifest, extracted dir)."""
    manifest_url = urllib.parse.urljoin(server.rstrip("/") + "/", "mac-client/manifest.json")
    manifest = json.loads(_download(manifest_url).decode("utf-8"))
    required = {"version", "sha256", "bytes", "package_url"}
    if not required <= set(manifest):
        raise ValueError("update manifest is missing required fields")
    package_url = urllib.parse.urljoin(server.rstrip("/") + "/", str(manifest["package_url"]).lstrip("/"))
    if urllib.parse.urlsplit(package_url).netloc != urllib.parse.urlsplit(server).netloc:
        raise ValueError("update manifest points outside the configured server")
    package = work / "package.tar.gz"
    _download(package_url, package)
    if package.stat().st_size != int(manifest["bytes"]):
        raise ValueError("downloaded package size does not match the manifest")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    if digest != str(manifest["sha256"]).lower():
        raise ValueError("downloaded package SHA-256 does not match the manifest")
    extracted = work / "extracted"
    _safe_extract(package, extracted)
    for required_path in ("app/harness_runner.py", "client/harness_cli.py", "client/harness_client.py",
                          RUNNER_PLIST_NAME):
        if not (extracted / required_path).is_file():
            raise ValueError(f"update package is missing {required_path}")
    return manifest, extracted


def _stage_targets(extracted: Path, work: Path, base: Path, home: Path,
                   plist_target: Path) -> list[tuple[Path, Path]]:
    """Prepare the new runtime, client, and plist next to the download; returns (staged, install target) pairs."""
    runner_new = work / "runner-app.new"
    client_new = work / "client.new"
    shutil.copytree(extracted / "app", runner_new)
    client_new.mkdir()
    for source in (extracted / "client").iterdir():
        shutil.copy2(source, client_new / source.name)
    client_config = base / "client" / "config.json"
    if client_config.exists():
        shutil.copy2(client_config, client_new / "config.json")

    targets = [(runner_new, base / "runner" / "app"), (client_new, base / "client")]
    plist_new = work / "plist.new"
    plist_text = (extracted / RUNNER_PLIST_NAME).read_text(encoding="utf-8")
    plist_text = plist_text.replace("__HOME__", str(home)).replace(
        "__PYTHON__", str(base / "venv" / "bin" / "python"))
    plist_new.write_text(plist_text, encoding="utf-8")
    targets.append((plist_new, plist_target))
    return targets


def _install_targets(targets: list[tuple[Path, Path]], backup: Path, moved: list[tuple[Path, Path]],
                     installed: list[Path]) -> None:
    backup.mkdir()
    for index, (source, target) in enumerate(targets):
        target.parent.mkdir(parents=True, exist_ok=True)
        prior = backup / str(index)
        if target.exists():
            os.replace(target, prior)
            moved.append((prior, target))
        os.replace(source, target)
        installed.append(target)


def _roll_back(installed: list[Path], moved: list[tuple[Path, Path]]) -> None:
    for target in reversed(installed):
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
    for prior, target in reversed(moved):
        if prior.exists():
            os.replace(prior, target)


def apply_update(server: str, base: Path | None = None, *, restart: bool = True,
                 home: Path | None = None) -> dict:
    """Download, verify, stage, atomically install, and optionally restart launchd.

    Client and runner configuration live outside the replaced runtime directories.
    Any failure rolls the prior runtime back before it is reported.
    """
    base = (base or Path.home() / ".agent-harness").expanduser()
    home = (home or Path.home()).expanduser()
    plist_target = home / "Library" / "LaunchAgents" / RUNNER_PLIST_NAME
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".update-", dir=str(base)))
    moved: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    launchd_unloaded = False
    result = {"ok": False, "version": "", "at": time.time(), "message": "update did not complete"}
    try:
        manifest, extracted = _fetch_verified_package(server, work)
        targets = _stage_targets(extracted, work, base, home, plist_target)
        prior_plist_text = plist_target.read_text(encoding="utf-8") if plist_target.is_file() else None
        _install_targets(targets, work / "backup", moved, installed)

        plist_changed = prior_plist_text != plist_target.read_text(encoding="utf-8")
        if not restart and prior_plist_text is not None:
            previous = previous_plist_path(base)
            previous.parent.mkdir(parents=True, exist_ok=True)
            previous.write_text(prior_plist_text, encoding="utf-8")
        if restart:
            launchd_unloaded = True
            activate_launch_agent(plist_target, definition_changed=plist_changed)
        result = {"ok": True, "version": str(manifest["version"]), "build_id": manifest.get("build_id", ""),
                  "at": time.time(), "message": "Mac client updated", "plist_changed": plist_changed}
        _write_result(base, result)
        return result
    except Exception as exc:
        _roll_back(installed, moved)
        message = f"update failed; prior client preserved: {exc}"
        if launchd_unloaded and plist_target.is_file():
            try:
                reload_launch_agent(plist_target)
                verify_launch_agent_loaded(plist_target)
            except Exception as reload_exc:
                message = f"{message}; restored launchd job failed to reload: {reload_exc}"
        result.update({"at": time.time(), "message": message})
        _write_result(base, result)
        raise RuntimeError(result["message"]) from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)
