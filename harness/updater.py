"""Transactional updater shared by the native Mac CLI and runner."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
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


def _write_result(base: Path, result: dict) -> None:
    path = base / "runner" / "last-update.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".new")
    temp.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def apply_update(server: str, base: Path | None = None, *, restart: bool = True,
                 home: Path | None = None) -> dict:
    """Download, verify, stage, atomically install, and optionally restart launchd.

    Client and runner configuration live outside the replaced runtime directories.
    Any failure rolls the prior runtime back before it is reported.
    """
    base = (base or Path.home() / ".agent-harness").expanduser()
    home = (home or Path.home()).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".update-", dir=str(base)))
    moved: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    result = {"ok": False, "version": "", "at": time.time(), "message": "update did not complete"}
    try:
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
                              "dev.agent-harness.runner.plist"):
            if not (extracted / required_path).is_file():
                raise ValueError(f"update package is missing {required_path}")

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
        plist_target = home / "Library" / "LaunchAgents" / "dev.agent-harness.runner.plist"
        plist_new = work / "plist.new"
        plist_text = (extracted / "dev.agent-harness.runner.plist").read_text(encoding="utf-8")
        plist_text = plist_text.replace("__HOME__", str(home)).replace(
            "__PYTHON__", str(base / "venv" / "bin" / "python"))
        plist_new.write_text(plist_text, encoding="utf-8")
        targets.append((plist_new, plist_target))
        backup = work / "backup"
        backup.mkdir()
        for index, (source, target) in enumerate(targets):
            target.parent.mkdir(parents=True, exist_ok=True)
            prior = backup / str(index)
            if target.exists():
                os.replace(target, prior)
                moved.append((prior, target))
            os.replace(source, target)
            installed.append(target)

        if restart:
            domain = f"gui/{os.getuid()}/dev.agent-harness.runner"
            subprocess.run(["launchctl", "kickstart", "-k", domain], check=True, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        result = {"ok": True, "version": str(manifest["version"]), "build_id": manifest.get("build_id", ""),
                  "at": time.time(), "message": "Mac client updated"}
        _write_result(base, result)
        return result
    except Exception as exc:
        for target in reversed(installed):
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
        for prior, target in reversed(moved):
            if prior.exists():
                os.replace(prior, target)
        result.update({"at": time.time(), "message": f"update failed; prior client preserved: {exc}"})
        _write_result(base, result)
        raise RuntimeError(result["message"]) from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)
