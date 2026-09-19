"""Optional image-model components: owner-run install/status/remove, never on daemon start.

`flux-fast` is the first component. Downloads are explicit, hash-verified, and resume to a `.part` file. The Z-Image
`qwen_3_4b.safetensors` encoder is reused only when SHA-256 matches the pin; otherwise FLUX gets a distinct filename.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import threading
import urllib.parse
from pathlib import Path

import httpx

from .config import resolve_images_models_dir

log = logging.getLogger("harness.images_models")

MANIFEST_PATH = Path(__file__).with_name("images_flux_fast.json")
RESERVE_BYTES = 5 * 1024 ** 3
CHUNK = 8 * 1024 * 1024
ZIMAGE_ENCODER = "qwen_3_4b.safetensors"
REQUIRED_CLIP_TYPE = "flux2"


def load_manifest(path: Path | None = None) -> dict:
    return json.loads((path or MANIFEST_PATH).read_text(encoding="utf-8"))


def redact_url(url: str) -> str:
    """Log scheme/host/path only — never query strings or fragments (tokens live there)."""
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def models_dir(cfg) -> Path:
    return resolve_images_models_dir(cfg)


def comfy_dir(cfg) -> Path:
    return Path(cfg.comfy_dir)


def staged_comfy_dir(cfg) -> Path:
    return Path(getattr(cfg, "staged_comfy_dir", None) or (str(comfy_dir(cfg).resolve()) + ".staged"))


def previous_comfy_dir(cfg) -> Path:
    return Path(getattr(cfg, "previous_comfy_dir", None) or (str(comfy_dir(cfg).resolve()) + ".prev"))


def failed_comfy_dir(cfg) -> Path:
    return Path(str(comfy_dir(cfg).resolve()) + ".failed")


def encoder_paths(manifest: dict, root: Path) -> tuple[Path, Path]:
    enc = manifest["assets"]["encoder"]
    sub = root / enc["subdir"]
    return sub / enc["filename"], sub / enc["alt_filename"]


def pin_registry(manifest: dict) -> tuple[tuple[str, str, str], ...]:
    """(mode, subdir, filename) pins. Ownership follows the registry, not whatever file is on disk."""
    pins = {("fast", "text_encoders", ZIMAGE_ENCODER)}
    for asset in manifest.get("assets", {}).values():
        sub = asset["subdir"]
        pins.add(("flux-fast", sub, asset["filename"]))
        if asset.get("alt_filename"):
            pins.add(("flux-fast", sub, asset["alt_filename"]))
        shared = asset.get("shared_with")
        if shared:
            pins.add((str(shared), sub, asset["filename"]))
    return tuple(sorted(pins))


def modes_pinning(manifest: dict, subdir: str, filename: str) -> set[str]:
    return {mode for mode, sub, name in pin_registry(manifest) if sub == subdir and name == filename}


def exclusively_owned_by_flux_fast(manifest: dict, subdir: str, filename: str) -> bool:
    return modes_pinning(manifest, subdir, filename) == {"flux-fast"}


def asset_dest(asset: dict, root: Path, filename: str | None = None) -> Path:
    return root / asset["subdir"] / (filename or asset["filename"])


def sha256_file(path: Path, expected: int | None = None) -> str:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    if expected is not None and size != expected:
        raise ValueError(f"{path.name} is {size} bytes, expected {expected}")
    return digest.hexdigest()


# Verified (and fail-closed corrupt) results keyed by identity of the bytes on disk.
# Status polling must stay O(stat): a 4 GB checkpoint is hashed once, not every GET /images/{id}.
_FILE_STATE_CACHE: dict[tuple, str] = {}
_FILE_STATE_LOCK = threading.Lock()


def clear_file_state_cache() -> None:
    with _FILE_STATE_LOCK:
        _FILE_STATE_CACHE.clear()


def _mtime_ns(st) -> int:
    return int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))


def _cache_path_key(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def file_state(path: Path, sha256: str, size: int, *, hash_if_needed: bool = True) -> str:
    """ok | missing | corrupt | verifying. Unchanged files are not re-hashed."""
    if not path.is_file():
        return "missing"
    try:
        st = path.stat()
    except OSError:
        return "corrupt"
    if st.st_size != size:
        return "corrupt"
    pin = sha256.lower()
    key = (_cache_path_key(path), st.st_size, _mtime_ns(st), pin, size)
    with _FILE_STATE_LOCK:
        cached = _FILE_STATE_CACHE.get(key)
        if cached is not None:
            return cached
    if not hash_if_needed:
        return "verifying"
    try:
        state = "ok" if sha256_file(path, size).lower() == pin else "corrupt"
    except (OSError, ValueError):
        state = "corrupt"
    try:
        st2 = path.stat()
    except OSError:
        return "corrupt"
    if st2.st_size != st.st_size or _mtime_ns(st2) != _mtime_ns(st):
        return state
    with _FILE_STATE_LOCK:
        stale = [old for old in _FILE_STATE_CACHE if old[0] == key[0] and old != key]
        for old in stale:
            _FILE_STATE_CACHE.pop(old, None)
        _FILE_STATE_CACHE[key] = state
    return state


def _free_bytes(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free


def encoder_plan(manifest: dict, root: Path, *, hash_if_needed: bool = True) -> dict:
    """Decide whether the existing Z-Image encoder is bit-identical to the FLUX pin."""
    enc = manifest["assets"]["encoder"]
    shared, alt = encoder_paths(manifest, root)
    pin = enc["sha256"]
    size = enc["bytes"]
    shared_state = file_state(shared, pin, size, hash_if_needed=hash_if_needed) if shared.exists() else "missing"
    if shared_state == "ok":
        return {"filename": enc["filename"], "path": shared, "shared": True, "state": "ok",
                "action": "reuse", "reason": "existing qwen_3_4b.safetensors matches the FLUX pin"}
    if shared_state == "verifying":
        return {"filename": enc["filename"], "path": shared, "shared": True, "state": "verifying",
                "action": "verify", "reason": "shared encoder present; SHA-256 not yet verified"}
    alt_state = file_state(alt, pin, size, hash_if_needed=hash_if_needed) if alt.exists() else "missing"
    if alt_state == "ok":
        return {"filename": enc["alt_filename"], "path": alt, "shared": False, "state": "ok",
                "action": "keep", "reason": "FLUX-only encoder already installed under a distinct name"}
    if alt_state == "verifying":
        return {"filename": enc["alt_filename"], "path": alt, "shared": False, "state": "verifying",
                "action": "verify", "reason": "FLUX-only encoder present; SHA-256 not yet verified"}
    if shared.exists() and shared_state == "corrupt":
        return {"filename": enc["alt_filename"], "path": alt, "shared": False, "state": "missing",
                "action": "install-alt",
                "reason": ("existing qwen_3_4b.safetensors does not match the FLUX pin; will install "
                           f"{enc['alt_filename']} and leave the Z-Image file untouched")}
    return {"filename": enc["filename"] if not shared.exists() else enc["alt_filename"],
            "path": shared if not shared.exists() else alt, "shared": False, "state": "missing",
            "action": "install" if not shared.exists() else "install-alt",
            "reason": ("no shared encoder present; will install the pinned file as qwen_3_4b.safetensors"
                       if not shared.exists() else
                       "shared encoder hash differs; will install a FLUX-only copy under a distinct name")}


def _iter_comfy_python(comfy_root: Path):
    inner = comfy_root / "ComfyUI"
    if not inner.is_dir():
        inner = comfy_root if (comfy_root / "main.py").is_file() else None
    if inner is None or not inner.is_dir():
        return
    direct = [inner / "nodes.py"]
    for extra in ("comfy_extras", "comfy_api"):
        folder = inner / extra
        if folder.is_dir():
            direct.extend(folder.glob("*.py"))
    comfy_pkg = inner / "comfy"
    if comfy_pkg.is_dir():
        direct.extend(comfy_pkg.glob("*.py"))
    for path in inner.glob("*flux*.py"):
        direct.append(path)
    seen: set[Path] = set()
    for path in direct:
        if path.is_file() and path not in seen:
            seen.add(path)
            yield path


def scan_node_classes(comfy_root: Path) -> set[str]:
    """Find `class Foo(` definitions under a portable ComfyUI tree without starting it."""
    found: set[str] = set()
    if not comfy_root.exists():
        return found
    pattern = re.compile(r"^class\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.M)
    for path in _iter_comfy_python(comfy_root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        found.update(pattern.findall(text))
    return found


def clip_type_present(comfy_root: Path, clip_type: str = REQUIRED_CLIP_TYPE) -> bool:
    if not comfy_root.exists():
        return False
    needle = f'"{clip_type}"'
    for path in _iter_comfy_python(comfy_root):
        try:
            if needle in path.read_text(encoding="utf-8", errors="ignore"):
                return True
        except OSError:
            continue
    return False


def nodes_from_object_info(object_info: dict | None) -> set[str]:
    if not object_info:
        return set()
    return set(object_info)


def inspect_flux_fast(cfg, *, manifest: dict | None = None, object_info: dict | None = None,
                      comfy_root: Path | None = None, hash_if_needed: bool = True) -> dict:
    """Availability of flux-fast: assets, encoder sharing, and required ComfyUI nodes. Never starts ComfyUI."""
    manifest = manifest or load_manifest()
    root = models_dir(cfg)
    comfy_root = Path(comfy_root) if comfy_root is not None else comfy_dir(cfg)
    enc_plan = encoder_plan(manifest, root, hash_if_needed=hash_if_needed)
    assets = {}
    missing, corrupt, verifying = [], [], []
    for key, asset in manifest["assets"].items():
        if key == "encoder":
            dest = enc_plan["path"]
            filename = enc_plan["filename"]
            state = file_state(dest, asset["sha256"], asset["bytes"],
                              hash_if_needed=hash_if_needed) if dest.exists() else "missing"
            if enc_plan["action"] == "reuse":
                state = "ok"
            elif enc_plan["state"] == "verifying":
                state = "verifying"
        else:
            dest = asset_dest(asset, root)
            filename = asset["filename"]
            state = file_state(dest, asset["sha256"], asset["bytes"], hash_if_needed=hash_if_needed)
        assets[key] = {"filename": filename, "path": str(dest), "state": state, "sha256": asset["sha256"],
                       "bytes": asset["bytes"], "revision": asset["revision"]}
        if state == "missing":
            missing.append(filename)
        elif state == "corrupt":
            corrupt.append(filename)
        elif state == "verifying":
            verifying.append(filename)

    required = list(manifest["required_nodes"])
    if object_info is not None:
        present = nodes_from_object_info(object_info)
        clip_ok = True
        if object_info:
            clip = object_info.get("CLIPLoader") or {}
            info = clip.get("input") or clip
            combo = ((info.get("required") or {}).get("type") or [None])[0]
            if isinstance(combo, list):
                clip_ok = REQUIRED_CLIP_TYPE in combo
    elif hash_if_needed or not (missing or verifying or corrupt):
        present = scan_node_classes(comfy_root)
        clip_ok = clip_type_present(comfy_root) if present else False
    else:
        present = set(required)
        clip_ok = True
    missing_nodes = [name for name in required if name not in present]
    py = comfy_root / "python_embeded" / "python.exe"
    comfy_present = py.is_file() or (comfy_root / "ComfyUI" / "main.py").is_file()

    reasons = []
    remediation = ""
    if missing:
        reasons.append("missing " + ", ".join(missing))
        remediation = "run ops/images-models.ps1 install flux-fast"
    if verifying:
        reasons.append("verifying " + ", ".join(verifying))
        remediation = remediation or "flux-fast assets are being verified"
    if corrupt:
        reasons.append("corrupt " + ", ".join(corrupt))
        remediation = remediation or "re-run ops/images-models.ps1 install flux-fast (corrupt files are not reused)"
    if not comfy_present:
        reasons.append(f"ComfyUI not found at {comfy_root}")
        remediation = remediation or "install ComfyUI portable, then ops/images-models.ps1 stage-comfyui"
    elif missing_nodes:
        reasons.append("ComfyUI missing nodes " + ", ".join(missing_nodes))
        remediation = (remediation or
                       "stage the pinned ComfyUI portable with ops/images-models.ps1 stage-comfyui; "
                       "production stays on the current build until validate/promote succeed")
    elif not clip_ok:
        reasons.append(f"CLIPLoader has no {REQUIRED_CLIP_TYPE} type")
        remediation = remediation or "stage the pinned ComfyUI portable (CLIP type flux2 is required)"

    available = not reasons
    reason = "; ".join(reasons)
    if available:
        remediation = ""
    return {
        "component": "flux-fast",
        "display_name": manifest["display_name"],
        "license": manifest["license"],
        "license_url": manifest["license_url"],
        "model_card": manifest["model_card"],
        "available": available,
        "unavailable_reason": reason,
        "verifying": bool(verifying),
        "remediation": remediation,
        "checkpoint_revision": manifest["assets"]["checkpoint"]["revision"],
        "checkpoint_sha256": manifest["assets"]["checkpoint"]["sha256"],
        "encoder_sha256": manifest["assets"]["encoder"]["sha256"],
        "vae_sha256": manifest["assets"]["vae"]["sha256"],
        "encoder_name": enc_plan["filename"],
        "encoder_shared": bool(enc_plan["shared"] and enc_plan["state"] == "ok"),
        "encoder_plan": enc_plan["action"],
        "steps": manifest["steps"],
        "guidance": manifest["guidance"],
        "sampler": manifest["sampler"],
        "scheduler": manifest["scheduler"],
        "negative_prompt": manifest["negative_prompt"],
        "supported_resolutions": list(manifest["supported_resolutions"]),
        "assets": assets,
        "missing_nodes": missing_nodes,
        "comfy_dir": str(comfy_root),
        "comfy_present": comfy_present,
        "pinned_comfyui": manifest["comfyui"]["pinned_portable"]["tag"],
        "first_stable_comfyui": manifest["comfyui"]["first_stable_with_nodes"],
    }


def doctor_warning(status: dict) -> str | None:
    """None when the optional component is ready; otherwise an owner-facing warning (not a stack failure)."""
    if status.get("available"):
        return None
    reason = status.get("unavailable_reason") or "not installed"
    extra = status.get("remediation") or "run ops/images-models.ps1 status flux-fast"
    return f"flux-fast unavailable ({reason}). {extra}"


def _headers() -> dict:
    return {"User-Agent": "agent-harness-flux-fast"}


def download_file(url: str, dest: Path, sha256: str, size: int, *, client: httpx.Client | None = None,
                  timeout: float | httpx.Timeout | None = None) -> dict:
    """Stream to dest.part, resume with Range when the server honors it, verify, then atomically promote."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    own_client = client is None
    timeout = timeout if timeout is not None else httpx.Timeout(60.0, read=600.0)
    client = client or httpx.Client(timeout=timeout, follow_redirects=True, headers=_headers())

    def promote(*, resumed: bool) -> dict:
        actual = part.stat().st_size
        if actual != size:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"{dest.name} size {actual} != {size}; partial file removed")
        digest = sha256_file(part, size)
        if digest.lower() != sha256.lower():
            part.unlink(missing_ok=True)
            raise RuntimeError(f"{dest.name} SHA-256 mismatch; partial file removed")
        os.replace(part, dest)
        return {"path": str(dest), "bytes": size, "sha256": digest, "resumed": resumed}

    try:
        have = part.stat().st_size if part.is_file() else 0
        if have > size:
            part.unlink(missing_ok=True)
            have = 0
        if have == size:
            # Finished streaming but crashed before os.replace: do not Range past EOF (HTTP 416).
            return promote(resumed=True)
        headers = dict(_headers())
        if have:
            headers["Range"] = f"bytes={have}-"
        log.info("downloading %s (%s bytes, resume %s)", redact_url(url), size, have)
        with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code == 416:
                have_now = part.stat().st_size if part.is_file() else 0
                if have_now == size:
                    return promote(resumed=True)
                part.unlink(missing_ok=True)
                raise RuntimeError(f"download failed HTTP 416 for {redact_url(url)}")
            if resp.status_code not in (200, 206):
                raise RuntimeError(f"download failed HTTP {resp.status_code} for {redact_url(url)}")
            if have and resp.status_code == 200:
                have = 0
                part.unlink(missing_ok=True)
            mode = "ab" if have and resp.status_code == 206 else "wb"
            if mode == "wb":
                have = 0
            with part.open(mode) as fh:
                for chunk in resp.iter_bytes(CHUNK):
                    if chunk:
                        fh.write(chunk)
                        have += len(chunk)
        return promote(resumed=bool(headers.get("Range")))
    finally:
        if own_client:
            client.close()


def _missing_payload(manifest: dict, root: Path) -> list[tuple[dict, Path]]:
    plan = encoder_plan(manifest, root)
    needed = []
    for key, asset in manifest["assets"].items():
        if key == "encoder":
            if plan["action"] in ("reuse", "keep"):
                continue
            dest = plan["path"]
        else:
            dest = asset_dest(asset, root)
        if file_state(dest, asset["sha256"], asset["bytes"]) != "ok":
            needed.append((asset, dest))
    return needed


def preflight_space(manifest: dict, root: Path, *, free_bytes=None) -> dict:
    needed_files = _missing_payload(manifest, root)
    payload = sum(asset["bytes"] for asset, _ in needed_files)
    largest = max((asset["bytes"] for asset, _ in needed_files), default=0)
    required = payload + largest + RESERVE_BYTES  # dest payload + one .part + 5 GiB
    free = (_free_bytes if free_bytes is None else free_bytes)(root)
    ok = free >= required
    return {"ok": ok, "free": free, "required": required, "payload": payload, "reserve": RESERVE_BYTES,
            "files": [dest.name for _, dest in needed_files]}


def install_flux_fast(cfg, *, manifest: dict | None = None, client: httpx.Client | None = None,
                      free_bytes=None) -> dict:
    manifest = manifest or load_manifest()
    root = models_dir(cfg)
    space = preflight_space(manifest, root, free_bytes=free_bytes)
    if not space["ok"]:
        raise RuntimeError(
            f"refusing to download flux-fast: need {space['required']} bytes free "
            f"(payload {space['payload']} + temp + 5 GiB reserve), have {space['free']}"
        )
    results = []
    plan = encoder_plan(manifest, root)
    for key, asset in manifest["assets"].items():
        if key == "encoder":
            if plan["action"] in ("reuse", "keep"):
                results.append({"role": key, "filename": plan["filename"], "action": plan["action"],
                                "path": str(plan["path"]), "shared": plan["shared"]})
                continue
            dest = plan["path"]
            filename = plan["filename"]
        else:
            dest = asset_dest(asset, root)
            filename = asset["filename"]
        if file_state(dest, asset["sha256"], asset["bytes"]) == "ok":
            results.append({"role": key, "filename": filename, "action": "skip", "path": str(dest)})
            continue
        download_file(asset["url"], dest, asset["sha256"], asset["bytes"], client=client)
        results.append({"role": key, "filename": filename, "action": "installed", "path": str(dest)})
    status = inspect_flux_fast(cfg, manifest=manifest)
    return {"installed": True, "files": results, "encoder_shared": status["encoder_shared"],
            "status": status, "space": space}


def _dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def remove_flux_fast(cfg, *, manifest: dict | None = None) -> dict:
    """Remove files owned solely by flux-fast. Never delete a shared Z-Image encoder or its .part."""
    manifest = manifest or load_manifest()
    root = models_dir(cfg)
    removed, skipped, recovered = [], [], 0
    considered: set[str] = set()

    def consider(subdir: str, name: str, path: Path) -> None:
        nonlocal recovered
        key = str(path)
        if key in considered:
            return
        considered.add(key)
        if not exclusively_owned_by_flux_fast(manifest, subdir, name):
            if path.exists():
                skipped.append({"path": key, "reason": "shared with Z-Image fast; left in place"})
            return
        if path.exists():
            recovered += path.stat().st_size if path.is_file() else _dir_bytes(path)
            path.unlink()
            removed.append(key)

    for asset in manifest["assets"].values():
        names = [asset["filename"]]
        if asset.get("alt_filename"):
            names.append(asset["alt_filename"])
        for name in names:
            dest = root / asset["subdir"] / name
            consider(asset["subdir"], name, dest)
            consider(asset["subdir"], name, dest.with_name(name + ".part"))
    return {"removed": removed, "skipped": skipped, "recovered_bytes": recovered,
            "status": inspect_flux_fast(cfg, manifest=manifest)}


def flux_fast_status(cfg, **kwargs) -> dict:
    return inspect_flux_fast(cfg, **kwargs)


# --- ComfyUI portable stage / validate / promote / rollback ---

def comfy_version_label(root: Path) -> str:
    for rel in ("ComfyUI/comfyui_version.py", "ComfyUI/comfy/version.py", "comfyui_version.py"):
        path = root / rel
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"""__version__\s*=\s*['\"]([^'\"]+)['\"]""", text)
            if m:
                return m.group(1)
    pin = root / "harness-comfyui-release.json"
    if pin.is_file():
        try:
            return json.loads(pin.read_text(encoding="utf-8")).get("tag") or ""
        except json.JSONDecodeError:
            return ""
    return ""


def graph_node_classes() -> dict[str, tuple[str, ...]]:
    from . import images
    return {
        "fast": tuple(sorted({n["class_type"] for n in images.workflow("fast", "x", 1024, 1024, 1, "t").values()})),
        "quality": tuple(sorted({n["class_type"] for n in images.workflow("quality", "x", 1024, 1024, 1, "t").values()})),
        "flux-fast": tuple(sorted({n["class_type"] for n in images.workflow(
            "flux-fast", "x", 1024, 1024, 1, "t", encoder_name="qwen_3_4b.safetensors").values()})),
    }


def preflight_graphs(object_info: dict, *, modes: tuple[str, ...] = ("fast", "quality", "flux-fast")) -> dict:
    classes = graph_node_classes()
    missing = {}
    present = set(object_info or ())
    for mode in modes:
        miss = [name for name in classes[mode] if name not in present]
        if miss:
            missing[mode] = miss
    return {"ok": not missing, "missing": missing}


def stage_comfyui(cfg, *, manifest: dict | None = None, client: httpx.Client | None = None,
                  extract=None, free_bytes=None) -> dict:
    """Download the pinned portable into <comfy>.staged without touching the production path."""
    manifest = manifest or load_manifest()
    pin = manifest["comfyui"]["pinned_portable"]
    staged = staged_comfy_dir(cfg)
    archive = staged.parent / pin["filename"]
    need = pin["bytes"] + RESERVE_BYTES
    free = (_free_bytes if free_bytes is None else free_bytes)(staged.parent)
    if free < need:
        raise RuntimeError(f"refusing ComfyUI download: need {need} bytes, have {free}")
    if not (archive.is_file() and file_state(archive, pin["sha256"], pin["bytes"]) == "ok"):
        download_file(pin["url"], archive, pin["sha256"], pin["bytes"], client=client)
    staged.mkdir(parents=True, exist_ok=True)
    if extract is None:
        raise RuntimeError(
            f"archive verified at {archive}; extract it to {staged} with 7z (owner step), then validate. "
            "Production ComfyUI was not changed."
        )
    extract(archive, staged)
    (staged / "harness-comfyui-release.json").write_text(
        json.dumps({"tag": pin["tag"], "sha256": pin["sha256"], "filename": pin["filename"]}, indent=2) + "\n",
        encoding="utf-8")
    current = comfy_dir(cfg)
    extra = current / "ComfyUI" / "extra_model_paths.yaml"
    if extra.is_file():
        dest = staged / "ComfyUI" / "extra_model_paths.yaml"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(extra, dest)
    return {"staged": str(staged), "archive": str(archive), "tag": pin["tag"], "production": str(current)}


def validate_comfyui(cfg, *, object_info: dict | None = None, probe=None, root: Path | None = None) -> dict:
    """Check a staged (or current) tree. Does not change the production path."""
    target = Path(root) if root is not None else staged_comfy_dir(cfg)
    if object_info is None and probe is not None:
        object_info = probe(target)
    if object_info is None:
        names = scan_node_classes(target)
        object_info = {name: {} for name in names}
        if clip_type_present(target):
            object_info["CLIPLoader"] = {"input": {"required": {"type": [[REQUIRED_CLIP_TYPE]]}}}
    graphs = preflight_graphs(object_info)
    py = (target / "python_embeded" / "python.exe").is_file()
    main = (target / "ComfyUI" / "main.py").is_file()
    ok = py and main and graphs["ok"]
    return {"ok": ok, "root": str(target), "python": py, "main": main, "version": comfy_version_label(target),
            "graphs": graphs, "production": str(comfy_dir(cfg))}


def _swap_dirs(src: Path, dest: Path) -> None:
    if dest.exists():
        raise RuntimeError(f"cannot move {src} -> {dest}: destination exists")
    os.replace(src, dest)


def promote_comfyui(cfg, *, validated: dict | None = None) -> dict:
    """Point production at the staged build. Caller must have validated. Keeps the previous tree for rollback."""
    if validated is not None and not validated.get("ok"):
        raise RuntimeError("refusing to promote a ComfyUI build that failed validation")
    current, staged, previous = comfy_dir(cfg), staged_comfy_dir(cfg), previous_comfy_dir(cfg)
    if not staged.exists():
        raise RuntimeError(f"no staged ComfyUI at {staged}")
    if previous.exists():
        shutil.rmtree(previous)
    if current.exists():
        _swap_dirs(current, previous)
    _swap_dirs(staged, current)
    return {"production": str(current), "previous": str(previous), "version": comfy_version_label(current)}


def rollback_comfyui(cfg) -> dict:
    """One-command restore of the tree saved at promote. Production path is unchanged in config."""
    current, previous, failed = comfy_dir(cfg), previous_comfy_dir(cfg), failed_comfy_dir(cfg)
    if not previous.exists():
        raise RuntimeError(f"no previous ComfyUI at {previous}")
    if failed.exists():
        shutil.rmtree(failed)
    if current.exists():
        _swap_dirs(current, failed)
    _swap_dirs(previous, current)
    return {"production": str(current), "failed_kept_at": str(failed), "version": comfy_version_label(current)}


def load_images_config(config_dir: str | None = None):
    from . import config as config_mod
    return config_mod.load(config_dir).images


def _print(data: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))
        return
    if "unavailable_reason" in data:
        flag = "ready" if data.get("available") else "unavailable"
        print(f"flux-fast: {flag}")
        if data.get("unavailable_reason"):
            print(f"  {data['unavailable_reason']}")
        if data.get("remediation"):
            print(f"  {data['remediation']}")
        print(f"  encoder: {data.get('encoder_name')} shared={data.get('encoder_shared')}")
        return
    print(json.dumps(data, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Install or inspect optional image components (never runs on daemon start)")
    p.add_argument("action", choices=("install", "remove", "status", "stage-comfyui", "validate-comfyui",
                                      "promote-comfyui", "rollback-comfyui"))
    p.add_argument("component", nargs="?", default="flux-fast")
    p.add_argument("--config-dir")
    p.add_argument("--json", action="store_true")
    p.add_argument("--extract-comfyui", help="7z executable used to unpack a staged portable")
    args = p.parse_args(argv)
    if args.component not in ("flux-fast", "comfyui"):
        print(f"unknown component {args.component}; supported: flux-fast", file=sys.stderr)
        return 2
    cfg = load_images_config(args.config_dir)
    try:
        if args.action == "status":
            _print(flux_fast_status(cfg), args.json)
            return 0
        if args.action == "install":
            _print(install_flux_fast(cfg), args.json)
            return 0
        if args.action == "remove":
            _print(remove_flux_fast(cfg), args.json)
            return 0
        if args.action == "stage-comfyui":
            extract = None
            if args.extract_comfyui:
                def extract(archive, dest):  # noqa: ANN001
                    import subprocess
                    subprocess.check_call([args.extract_comfyui, "x", f"-o{dest}", "-y", str(archive)])
            _print(stage_comfyui(cfg, extract=extract), args.json)
            return 0
        if args.action == "validate-comfyui":
            data = validate_comfyui(cfg)
            _print(data, args.json)
            return 0 if data["ok"] else 1
        if args.action == "promote-comfyui":
            checked = validate_comfyui(cfg, root=staged_comfy_dir(cfg))
            if not checked["ok"]:
                print("validation failed; production ComfyUI was not changed", file=sys.stderr)
                _print(checked, args.json)
                return 1
            _print(promote_comfyui(cfg, validated=checked), args.json)
            return 0
        if args.action == "rollback-comfyui":
            _print(rollback_comfyui(cfg), args.json)
            return 0
    except (OSError, RuntimeError, httpx.HTTPError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
