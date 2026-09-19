"""Issue #92: optional flux-fast mode. No GPU, no multi-GB downloads — tiny fixtures only."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from harness.config import ImagesConfig
from harness.fileops import ToolError
from harness.images import workflow
from harness import images_models as images_models_mod
from harness.images_models import (
    RESERVE_BYTES, doctor_warning, download_file, file_state, install_flux_fast, inspect_flux_fast,
    load_manifest, preflight_graphs, promote_comfyui, redact_url, remove_flux_fast, rollback_comfyui,
    stage_comfyui, validate_comfyui,
)

from test_phase6 import PNG, image_manager


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tiny_manifest(url_base: str, payloads: dict[str, bytes]) -> dict:
    manifest = load_manifest()
    for key, asset in manifest["assets"].items():
        body = payloads[key]
        asset["bytes"] = len(body)
        asset["sha256"] = _sha(body)
        asset["url"] = f"{url_base}/{key}"
    return manifest


def plant_comfy(root: Path, nodes=None, clip_type: str = "flux2") -> None:
    (root / "python_embeded").mkdir(parents=True, exist_ok=True)
    (root / "python_embeded" / "python.exe").write_bytes(b"")
    cu = root / "ComfyUI"
    cu.mkdir(parents=True, exist_ok=True)
    (cu / "main.py").write_text("# fixture\n", encoding="utf-8")
    names = nodes if nodes is not None else load_manifest()["required_nodes"]
    body = "\n".join(f"class {name}:\n    pass\n" for name in names)
    if clip_type:
        body += f'\nTYPES = ["{clip_type}"]\n'
    (cu / "nodes_flux2.py").write_text(body, encoding="utf-8")


def plant_asset(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def object_info_for(nodes=None) -> dict:
    names = list(nodes if nodes is not None else load_manifest()["required_nodes"])
    info = {name: {} for name in names}
    info["CLIPLoader"] = {"input": {"required": {"type": [["flux2", "lumina2"]]}}}
    return info


class FixtureHandler(BaseHTTPRequestHandler):
    payloads: dict[str, bytes] = {}
    hits: list = []

    def log_message(self, fmt, *args):  # noqa: A003
        return

    def do_GET(self):
        self.hits.append((self.path, self.headers.get("Range"), self.headers.get("Authorization")))
        name = self.path.lstrip("/")
        body = self.payloads.get(name)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        start, end = 0, len(body)
        status = 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            spec = rng.split("=", 1)[1]
            a, _, b = spec.partition("-")
            start = int(a or 0)
            end = int(b) + 1 if b else len(body)
            if start >= len(body):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(body)}")
                self.end_headers()
                return
            status = 206
        chunk = body[start:end]
        self.send_response(status)
        self.send_header("Content-Length", str(len(chunk)))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{start + len(chunk) - 1}/{len(body)}")
        self.end_headers()
        self.wfile.write(chunk)


def start_fixture(payloads: dict[str, bytes]):
    FixtureHandler.payloads = payloads
    FixtureHandler.hits = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, url


def stop_fixture(server):
    server.shutdown()
    server.server_close()


def cfg_for(tmp_path: Path, **extra) -> ImagesConfig:
    return ImagesConfig(enabled=True, work_dir=str(tmp_path / "img"),
                        comfy_dir=str(tmp_path / "comfy"), models_dir=str(tmp_path / "models"), **extra)


# --- graph ---

def test_flux_fast_graph_is_four_step_distilled():
    graph = workflow("flux-fast", "a red cube", 1024, 1024, 42, "harness/abc",
                     encoder_name="qwen_3_4b_flux2.safetensors")
    assert graph["9"]["inputs"]["filename_prefix"] == "harness/abc"
    assert graph["62"]["class_type"] == "Flux2Scheduler" and graph["62"]["inputs"]["steps"] == 4
    assert graph["62"]["inputs"]["width"] == 1024 and graph["62"]["inputs"]["height"] == 1024
    assert graph["61"]["inputs"]["sampler_name"] == "euler"
    assert graph["63"]["inputs"]["cfg"] == 1.0
    assert graph["69"]["inputs"]["noise_seed"] == 42
    assert graph["70"]["inputs"]["unet_name"] == "flux-2-klein-4b-fp8.safetensors"
    assert graph["71"]["inputs"]["clip_name"] == "qwen_3_4b_flux2.safetensors"
    assert graph["71"]["inputs"]["type"] == "flux2"
    assert graph["72"]["inputs"]["vae_name"] == "flux2-vae.safetensors"
    assert graph["76"]["class_type"] == "ConditioningZeroOut"
    assert graph["66"]["class_type"] == "EmptyFlux2LatentImage"
    assert "KSampler" not in {n["class_type"] for n in graph.values()}
    fast = workflow("fast", "x", 1024, 1024, 1, "p")
    assert next(n["inputs"]["steps"] for n in fast.values() if n["class_type"] == "KSampler") == 8


def test_flux_fast_graph_keeps_existing_fast_and_quality_shapes():
    fast = workflow("fast", "x", 1344, 768, 7, "p")
    quality = workflow("quality", "x", 1328, 1328, 7, "p")
    assert fast["3"]["inputs"]["sampler_name"] == "res_multistep"
    assert quality["230"]["inputs"]["steps"] == 50
    with pytest.raises(ToolError, match="model must be"):
        workflow("flux", "x", 1024, 1024, 1, "p")


# --- capability ---

def test_capability_missing_corrupt_shared_and_missing_nodes(tmp_path):
    cfg = cfg_for(tmp_path)
    payloads = {"checkpoint": b"ckpt", "vae": b"vae-bytes", "encoder": b"enc-shared"}
    manifest = tiny_manifest("http://unused", payloads)
    plant_comfy(Path(cfg.comfy_dir))
    missing = inspect_flux_fast(cfg, manifest=manifest, object_info=object_info_for())
    assert missing["available"] is False and "missing" in missing["unavailable_reason"]
    assert "install flux-fast" in missing["remediation"]
    assert doctor_warning(missing)

    root = Path(cfg.models_dir)
    plant_asset(root / "diffusion_models" / "flux-2-klein-4b-fp8.safetensors", payloads["checkpoint"])
    plant_asset(root / "vae" / "flux2-vae.safetensors", payloads["vae"])
    plant_asset(root / "text_encoders" / "qwen_3_4b.safetensors", b"not-the-encoder")
    corrupt = inspect_flux_fast(cfg, manifest=manifest, object_info=object_info_for())
    assert corrupt["available"] is False
    assert corrupt["encoder_plan"] == "install-alt"
    assert not corrupt["encoder_shared"]
    assert "qwen_3_4b_flux2.safetensors" in corrupt["unavailable_reason"]

    plant_asset(root / "text_encoders" / "qwen_3_4b.safetensors", payloads["encoder"])
    shared = inspect_flux_fast(cfg, manifest=manifest, object_info=object_info_for())
    assert shared["available"] and shared["encoder_shared"] and shared["encoder_name"] == "qwen_3_4b.safetensors"

    nodes = [n for n in load_manifest()["required_nodes"] if n != "Flux2Scheduler"]
    plant_comfy(Path(cfg.comfy_dir), nodes=nodes)
    missing_node = inspect_flux_fast(cfg, manifest=manifest, object_info=object_info_for(nodes))
    assert missing_node["available"] is False and "Flux2Scheduler" in missing_node["unavailable_reason"]
    ready = inspect_flux_fast(cfg, manifest=manifest, object_info=object_info_for())
    assert ready["available"] and doctor_warning(ready) is None


def _count_sha256(monkeypatch):
    calls = {"n": 0}
    real = images_models_mod.sha256_file

    def counting(path, expected=None):
        calls["n"] += 1
        return real(path, expected)

    monkeypatch.setattr(images_models_mod, "sha256_file", counting)
    return calls


def test_file_state_caches_hash_and_invalidates_on_change(tmp_path, monkeypatch):
    images_models_mod.clear_file_state_cache()
    calls = _count_sha256(monkeypatch)
    path = tmp_path / "asset.bin"
    body = b"hello-asset"
    path.write_bytes(body)
    digest = _sha(body)
    assert file_state(path, digest, len(body)) == "ok"
    assert calls["n"] == 1
    assert file_state(path, digest, len(body)) == "ok"
    assert calls["n"] == 1
    assert file_state(path, digest, len(body) + 1) == "corrupt"
    assert calls["n"] == 1
    path.write_bytes(b"HELLO-ASSET")
    os.utime(path, ns=(time.time_ns(), time.time_ns()))
    assert file_state(path, digest, len(body)) == "corrupt"
    assert calls["n"] == 2
    path.unlink()
    assert file_state(path, digest, len(body)) == "missing"
    assert calls["n"] == 2


def test_repeated_inspect_and_status_do_not_rehash_unchanged_files(tmp_path, monkeypatch):
    images_models_mod.clear_file_state_cache()
    calls = _count_sha256(monkeypatch)
    cfg = cfg_for(tmp_path)
    payloads = {"checkpoint": b"ckpt-data", "vae": b"vae-bytes", "encoder": b"enc-shared"}
    manifest = tiny_manifest("http://unused", payloads)
    plant_comfy(Path(cfg.comfy_dir))
    root = Path(cfg.models_dir)
    plant_asset(root / "diffusion_models" / "flux-2-klein-4b-fp8.safetensors", payloads["checkpoint"])
    plant_asset(root / "vae" / "flux2-vae.safetensors", payloads["vae"])
    plant_asset(root / "text_encoders" / "qwen_3_4b.safetensors", payloads["encoder"])
    first = inspect_flux_fast(cfg, manifest=manifest, object_info=object_info_for())
    assert first["available"]
    hashed = calls["n"]
    assert hashed >= 1
    for _ in range(5):
        again = inspect_flux_fast(cfg, manifest=manifest, object_info=object_info_for())
        assert again["available"]
    assert calls["n"] == hashed

    from test_phase6 import image_manager
    m, _, _ = image_manager(tmp_path / "svc")
    m.cfg.images.models_dir = str(root)
    m.cfg.images.comfy_dir = str(cfg.comfy_dir)
    m.images.cfg.models_dir = str(root)
    m.images.cfg.comfy_dir = str(cfg.comfy_dir)
    m.images.flux_manifest = manifest
    m.images.object_info = object_info_for()
    before_status = calls["n"]
    for _ in range(8):
        listing = m.images.status()
        flux = listing["modes"]["flux-fast"]
        assert flux["available"] is True
    assert calls["n"] == before_status

    ckpt = root / "diffusion_models" / "flux-2-klein-4b-fp8.safetensors"
    ckpt.write_bytes(b"XXXX-data")
    os.utime(ckpt, ns=(time.time_ns(), time.time_ns()))
    broken = inspect_flux_fast(cfg, manifest=manifest, object_info=object_info_for())
    assert broken["available"] is False and "corrupt" in broken["unavailable_reason"]
    assert calls["n"] > hashed
    listing = m.images.status()
    flux = listing["modes"]["flux-fast"]
    assert flux["available"] is False


# --- install / remove ---

def test_install_hash_failure_space_shared_and_idempotent_remove(tmp_path):
    payloads = {"checkpoint": b"CKPT-DATA", "vae": b"VAE-DATA", "encoder": b"ENC-DATA"}
    bad = dict(payloads)
    server, url = start_fixture({**{k: v for k, v in payloads.items()}, "checkpoint": b"WRONG"})
    try:
        cfg = cfg_for(tmp_path)
        plant_comfy(Path(cfg.comfy_dir))
        manifest = tiny_manifest(url, payloads)
        with pytest.raises(RuntimeError, match="SHA-256|size"):
            install_flux_fast(cfg, manifest=manifest, free_bytes=lambda p: 10 * 1024 ** 3)
        dest = Path(cfg.models_dir) / "diffusion_models" / "flux-2-klein-4b-fp8.safetensors"
        assert not dest.exists()
        assert not dest.with_name(dest.name + ".part").exists()
    finally:
        stop_fixture(server)

    server, url = start_fixture(payloads)
    try:
        cfg = cfg_for(tmp_path)
        plant_comfy(Path(cfg.comfy_dir))
        manifest = tiny_manifest(url, payloads)
        with pytest.raises(RuntimeError, match="refusing to download"):
            install_flux_fast(cfg, manifest=manifest, free_bytes=lambda p: 100)
        assert not any(Path(cfg.models_dir).rglob("*")) or not list(Path(cfg.models_dir).rglob("*.safetensors"))

        # shared encoder already present with the pinned hash: reuse, do not download encoder
        plant_asset(Path(cfg.models_dir) / "text_encoders" / "qwen_3_4b.safetensors", payloads["encoder"])
        FixtureHandler.hits.clear()
        out = install_flux_fast(cfg, manifest=manifest, free_bytes=lambda p: 10 * 1024 ** 3)
        assert out["encoder_shared"] is True
        encoder_urls = [h for h in FixtureHandler.hits if h[0].rstrip("/").endswith("encoder")]
        assert encoder_urls == []
        again = install_flux_fast(cfg, manifest=manifest, free_bytes=lambda p: 10 * 1024 ** 3)
        assert all(f["action"] in ("skip", "reuse", "keep") for f in again["files"])

        first = remove_flux_fast(cfg, manifest=manifest)
        shared = Path(cfg.models_dir) / "text_encoders" / "qwen_3_4b.safetensors"
        assert shared.read_bytes() == payloads["encoder"]
        assert not (Path(cfg.models_dir) / "diffusion_models" / "flux-2-klein-4b-fp8.safetensors").exists()
        second = remove_flux_fast(cfg, manifest=manifest)
        assert shared.exists() and second["recovered_bytes"] == 0
        assert any("shared" in s["reason"] for s in first["skipped"])
    finally:
        stop_fixture(server)


def test_install_alt_encoder_when_shared_hash_differs(tmp_path):
    payloads = {"checkpoint": b"C", "vae": b"V", "encoder": b"E-PIN"}
    server, url = start_fixture(payloads)
    try:
        cfg = cfg_for(tmp_path)
        plant_comfy(Path(cfg.comfy_dir))
        plant_asset(Path(cfg.models_dir) / "text_encoders" / "qwen_3_4b.safetensors", b"z-image-original")
        manifest = tiny_manifest(url, payloads)
        out = install_flux_fast(cfg, manifest=manifest, free_bytes=lambda p: 10 * 1024 ** 3)
        assert out["encoder_shared"] is False
        shared = Path(cfg.models_dir) / "text_encoders" / "qwen_3_4b.safetensors"
        alt = Path(cfg.models_dir) / "text_encoders" / "qwen_3_4b_flux2.safetensors"
        assert shared.read_bytes() == b"z-image-original"
        assert alt.read_bytes() == b"E-PIN"
        remove_flux_fast(cfg, manifest=manifest)
        assert shared.read_bytes() == b"z-image-original"
        assert not alt.exists()
    finally:
        stop_fixture(server)


def test_download_file_promotes_complete_part_without_reget(tmp_path):
    """A .part already at the pinned size must not send Range: bytes={size}- (HTTP 416)."""
    body = b"complete-encoder-bytes"
    dest = tmp_path / "qwen_3_4b_flux2.safetensors"
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(body)
    hits = []

    def handler(request: httpx.Request):
        hits.append(request.headers.get("range") or request.headers.get("Range"))
        return httpx.Response(416, content=b"",
                              headers={"Content-Range": f"bytes */{len(body)}"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = download_file("http://example.test/encoder", dest, _sha(body), len(body), client=client)
    assert dest.read_bytes() == body
    assert not part.exists()
    assert out["bytes"] == len(body)
    assert hits == []


def test_download_file_416_on_complete_part_still_promotes(tmp_path, monkeypatch):
    """Defense: if a GET already went out, HTTP 416 must not delete a complete .part."""
    body = b"already-finished-stream"
    dest = tmp_path / "unet.safetensors"
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(body)
    remaining = {"n": 2}
    real = Path.stat

    def short_then_real(self, *args, **kwargs):
        info = real(self, *args, **kwargs)
        if self == part and remaining["n"] > 0:
            remaining["n"] -= 1
            return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink,
                                   info.st_uid, info.st_gid, 1, int(info.st_atime),
                                   int(info.st_mtime), int(info.st_ctime)))
        return info

    monkeypatch.setattr(Path, "stat", short_then_real)

    def handler(request: httpx.Request):
        assert (request.headers.get("range") or request.headers.get("Range")) == "bytes=1-"
        return httpx.Response(416, content=b"",
                              headers={"Content-Range": f"bytes */{len(body)}"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = download_file("http://example.test/unet", dest, _sha(body), len(body), client=client)
    assert dest.read_bytes() == body
    assert not part.exists()
    assert out["bytes"] == len(body)


def test_download_file_resumes_short_part_with_206(tmp_path):
    body = b"hello-world-payload"
    dest = tmp_path / "vae.safetensors"
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(body[:5])

    def handler(request: httpx.Request):
        rng = request.headers.get("range") or request.headers.get("Range")
        assert rng == "bytes=5-"
        return httpx.Response(206, content=body[5:],
                              headers={"Content-Range": f"bytes 5-{len(body) - 1}/{len(body)}"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    download_file("http://example.test/vae", dest, _sha(body), len(body), client=client)
    assert dest.read_bytes() == body
    assert not part.exists()


def test_install_promotes_leftover_complete_parts(tmp_path):
    payloads = {"checkpoint": b"CKPT-DATA", "vae": b"VAE-DATA", "encoder": b"ENC-DATA"}
    server, url = start_fixture(payloads)
    try:
        cfg = cfg_for(tmp_path)
        plant_comfy(Path(cfg.comfy_dir))
        manifest = tiny_manifest(url, payloads)
        root = Path(cfg.models_dir)
        for key, asset in manifest["assets"].items():
            dest = root / asset["subdir"] / asset["filename"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.with_name(dest.name + ".part").write_bytes(payloads[key])
        FixtureHandler.hits.clear()
        out = install_flux_fast(cfg, manifest=manifest, free_bytes=lambda p: 10 * 1024 ** 3)
        assert all(f["action"] == "installed" for f in out["files"])
        for key, asset in manifest["assets"].items():
            dest = root / asset["subdir"] / asset["filename"]
            assert dest.read_bytes() == payloads[key]
            assert not dest.with_name(dest.name + ".part").exists()
        assert FixtureHandler.hits == []
    finally:
        stop_fixture(server)


def test_redact_url_strips_query_and_fragment():
    assert redact_url("https://example/file?token=secret#x") == "https://example/file"
    assert "secret" not in redact_url("https://huggingface.co/x?authorization=Bearer+abc")


# --- HTTP / tool selection ---

def enable_flux(m, tmp_path, payloads, manifest):
    plant_comfy(Path(m.cfg.images.comfy_dir))
    root = Path(m.cfg.images.models_dir)
    plant_asset(root / "diffusion_models" / "flux-2-klein-4b-fp8.safetensors", payloads["checkpoint"])
    plant_asset(root / "vae" / "flux2-vae.safetensors", payloads["vae"])
    plant_asset(root / "text_encoders" / "qwen_3_4b.safetensors", payloads["encoder"])
    m.images.flux_manifest = manifest
    m.images.object_info = object_info_for()


def test_http_and_tool_select_flux_fast_without_fallback(tmp_path):
    from fastapi.testclient import TestClient
    from harness.api import create_app

    payloads = {"checkpoint": b"C", "vae": b"V", "encoder": b"E"}
    m, server, state = image_manager(tmp_path)
    m.cfg.images.models_dir = str(tmp_path / "models")
    m.cfg.images.comfy_dir = str(tmp_path / "comfy")
    m.images.cfg.models_dir = m.cfg.images.models_dir
    m.images.cfg.comfy_dir = m.cfg.images.comfy_dir
    manifest = tiny_manifest("http://unused", payloads)

    with TestClient(create_app(m)) as client:
        listing = client.get("/images").json()["status"]
        flux = listing["modes"]["flux-fast"]
        assert flux["available"] is False and flux["label"].startswith("FLUX.2 klein 4B")
        assert client.post("/images", json={"prompt": "a cat", "model": "flux-fast"}).status_code == 400
        enable_flux(m, tmp_path, payloads, manifest)
        r = client.post("/images", json={"prompt": "a cat", "model": "flux-fast", "resolution": "high"})
        assert r.status_code == 400 and "does not support" in r.json()["detail"]
        job = client.post("/images", json={"prompt": "a cat", "model": "flux-fast"}).json()
        for _ in range(200):
            if client.get(f"/images/{job['id']}").json()["status"] == "done":
                break
            time.sleep(0.02)
        done = client.get(f"/images/{job['id']}").json()
        assert done["status"] == "done" and done["model"] == "flux-fast"
        assert done["provenance"]["steps"] == 4 and done["provenance"]["scheduler"] == "Flux2Scheduler"
        graph = state["graphs"][-1]
        assert graph["62"]["inputs"]["steps"] == 4
        assert graph["70"]["inputs"]["unet_name"] == "flux-2-klein-4b-fp8.safetensors"

    async def body():
        m2, _, state2 = image_manager(tmp_path / "b")
        m2.cfg.images.models_dir = str(tmp_path / "b" / "models")
        m2.cfg.images.comfy_dir = str(tmp_path / "b" / "comfy")
        m2.images.cfg.models_dir = m2.cfg.images.models_dir
        m2.images.cfg.comfy_dir = m2.cfg.images.comfy_dir
        enable_flux(m2, tmp_path / "b", payloads, manifest)
        await m2.start(maintenance=False)
        with pytest.raises(ToolError, match="does not support"):
            await m2.runner.images.call("generate_image",
                                        {"prompt": "x", "filename": "a.png", "model": "flux-fast",
                                         "resolution": "high"},
                                        workspace_root=tmp_path / "b" / "ws")
        (tmp_path / "b" / "ws").mkdir()
        out = await m2.runner.images.call("generate_image",
                                          {"prompt": "icon", "filename": "logo.png", "model": "flux-fast"},
                                          workspace_root=tmp_path / "b" / "ws")
        assert "flux-fast" in out
        assert state2["graphs"][-1]["62"]["inputs"]["steps"] == 4
        await m2.stop()
    asyncio.run(body())


def test_quality_fast_and_flux_fast_share_one_gpu_batch(tmp_path):
    """The two optional fast modes keep separate graphs while sharing one GPU occupancy."""
    async def body():
        payloads = {"checkpoint": b"C", "vae": b"V", "encoder": b"E"}
        manifest = tiny_manifest("http://unused", payloads)
        m, server, state = image_manager(tmp_path)
        m.cfg.images.models_dir = str(tmp_path / "models")
        m.cfg.images.comfy_dir = str(tmp_path / "comfy")
        m.images.cfg.models_dir = m.cfg.images.models_dir
        m.images.cfg.comfy_dir = m.cfg.images.comfy_dir
        enable_flux(m, tmp_path, payloads, manifest)
        m.images._lora_available = True
        assert list(m.images.status()["modes"]) == ["fast", "quality", "quality-fast", "flux-fast"]
        model_help = m.images.schemas()[0]["function"]["parameters"]["properties"]["model"]["description"]
        assert "quality-fast" in model_help and "flux-fast" in model_help

        quality = m.images.submit("fast poster", model="quality-fast", seed=11)
        flux = m.images.submit("fast illustration", model="flux-fast", seed=12)
        await m.start(maintenance=False)
        done = [await m.images.wait(job["id"]) for job in (quality, flux)]
        assert [job["model"] for job in done] == ["quality-fast", "flux-fast"]
        assert all(job["status"] == "done" for job in done)
        assert any(node["class_type"] == "LoraLoaderModelOnly" for node in state["graphs"][0].values())
        assert any(node["class_type"] == "Flux2Scheduler" for node in state["graphs"][1].values())
        for _ in range(100):
            if m.images.phase == "idle":
                break
            await asyncio.sleep(0.02)
        assert server.calls == ["stop", "start"]
        await m.stop()

    asyncio.run(body())


def test_old_image_rows_remain_readable(tmp_path):
    from harness.db import Database
    db = Database(tmp_path / "db.sqlite")
    db.conn.execute(
        "INSERT INTO images (id, source, prompt, model, aspect_ratio, width, height, seed, status, created_at) "
        "VALUES ('oldimg', 'phone', 'a lamp', 'fast', '1:1', 1024, 1024, 1, 'done', 1)")
    row = db.get_image("oldimg")
    assert row["model"] == "fast" and row["provenance"] == {}
    db.close()


# --- ComfyUI stage / rollback ---

def test_staged_comfyui_validation_and_rollback(tmp_path):
    cfg = cfg_for(tmp_path)
    prod, staged = Path(cfg.comfy_dir), Path(str(Path(cfg.comfy_dir).resolve()) + ".staged")
    plant_comfy(prod)
    (prod / "ComfyUI" / "extra_model_paths.yaml").write_text("models:\n", encoding="utf-8")
    (prod / "ComfyUI" / "comfyui_version.py").write_text("__version__ = '0.35.0'\n", encoding="utf-8")
    plant_comfy(staged)
    (staged / "ComfyUI" / "comfyui_version.py").write_text("__version__ = '0.36.0'\n", encoding="utf-8")
    info = object_info_for()
    for extra in ("ModelSamplingAuraFlow", "EmptySD3LatentImage", "KSampler"):
        info.setdefault(extra, {})
    checked = validate_comfyui(cfg, object_info=info, root=staged)
    assert checked["ok"] and checked["graphs"]["ok"]
    missing = validate_comfyui(cfg, object_info={"UNETLoader": {}}, root=staged)
    assert not missing["ok"]

    archive = tmp_path / "ComfyUI_windows_portable_nvidia.7z"
    pin = load_manifest()["comfyui"]["pinned_portable"]

    def fake_extract(src, dest):
        plant_comfy(Path(dest))

    # refuse promote when validation failed
    with pytest.raises(RuntimeError, match="failed validation"):
        promote_comfyui(cfg, validated={"ok": False})
    assert (prod / "ComfyUI" / "comfyui_version.py").read_text(encoding="utf-8").find("0.35.0") >= 0

    promoted = promote_comfyui(cfg, validated=checked)
    assert Path(promoted["production"]).joinpath("ComfyUI", "comfyui_version.py").read_text(
        encoding="utf-8").find("0.36.0") >= 0
    rolled = rollback_comfyui(cfg)
    assert Path(rolled["production"]).joinpath("ComfyUI", "comfyui_version.py").read_text(
        encoding="utf-8").find("0.35.0") >= 0

    # staging does not touch production: download refused on low space
    with pytest.raises(RuntimeError, match="refusing ComfyUI"):
        stage_comfyui(cfg, extract=fake_extract, free_bytes=lambda p: 10)

    graphs = preflight_graphs(info)
    assert graphs["ok"]
    bad = preflight_graphs({"UNETLoader": {}})
    assert not bad["ok"] and "fast" in bad["missing"] and "flux-fast" in bad["missing"]


# --- queue / GPU handoff ---

def hanging_comfy():
    def handler(request: httpx.Request):
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "hang"})
        if request.url.path.startswith("/history/"):
            return httpx.Response(200, json={})
        if request.url.path == "/view":
            return httpx.Response(200, content=PNG)
        return httpx.Response(404)
    return handler


def test_queue_gpu_cleanup_on_timeout_cancel_reject_and_restart(tmp_path):
    async def body():
        m, server, _ = image_manager(tmp_path)
        m.images.cfg.job_timeout_seconds = 0.15
        m.images.transport = httpx.MockTransport(hanging_comfy())
        await m.start(maintenance=False)
        timed = m.images.submit("slow")
        timed = await m.images.wait(timed["id"])
        assert timed["status"] == "failed" and "timed out" in timed["error"]
        for _ in range(100):
            if m.images.phase == "idle":
                break
            await asyncio.sleep(0.02)
        assert server.calls[-2:] == ["stop", "start"] or server.calls == ["stop", "start"]
        assert m.images.phase == "idle" and not m.images.gpu_taken
        await m.stop()

        m, server, _ = image_manager(tmp_path / "c")
        m.images.transport = httpx.MockTransport(hanging_comfy())
        await m.start(maintenance=False)
        job = m.images.submit("cancel me")
        for _ in range(100):
            if m.images.active_job == job["id"]:
                break
            await asyncio.sleep(0.01)
        m.images.cancel(job["id"])
        done = await m.images.wait(job["id"])
        assert done["status"] == "failed" and "cancelled" in done["error"]
        for _ in range(100):
            if m.images.phase == "idle":
                break
            await asyncio.sleep(0.02)
        assert not m.images.gpu_taken
        await m.stop()

        m, server, state = image_manager(tmp_path / "r", fail_prompts=("nope",))
        await m.start(maintenance=False)
        bad = m.images.submit("nope")
        bad = await m.images.wait(bad["id"])
        assert bad["status"] == "failed" and "CUDA" in bad["error"]
        for _ in range(100):
            if m.images.phase == "idle":
                break
            await asyncio.sleep(0.02)
        assert server.calls == ["stop", "start"]
        await m.stop()

        m, server, _ = image_manager(tmp_path / "d")
        running = {"id": "restarted1", "session_id": "", "source": "phone", "prompt": "resume",
                   "model": "fast", "aspect_ratio": "1:1", "resolution": "standard",
                   "width": 1024, "height": 1024, "seed": 1, "provenance": {}}
        m.db.insert_image(running)
        m.db.update_image("restarted1", status="running")
        await m.start(maintenance=False)
        done = await m.images.wait("restarted1")
        assert done["status"] == "done"
        for _ in range(100):
            if m.images.phase == "idle":
                break
            await asyncio.sleep(0.02)
        assert server.calls == ["stop", "start"]
        await m.stop()
    asyncio.run(body())


def test_cancel_queued_job_does_not_boot_comfy_or_unload_llm(tmp_path):
    async def body():
        m, server, _ = image_manager(tmp_path / "direct")
        job = m.images.submit("never run")
        m.images.cancel(job["id"])
        await m.images._run_batch(job["id"])
        assert server.calls == []
        assert m.images.phase == "idle" and not m.images.gpu_taken

        m, server, _ = image_manager(tmp_path / "q")
        job = m.images.submit("never run")
        assert job["status"] == "queued"
        cancelled = m.images.cancel(job["id"])
        assert cancelled["status"] == "failed" and "cancelled" in cancelled["error"]
        await m.start(maintenance=False)
        done = await m.images.wait(job["id"])
        assert done["status"] == "failed"
        for _ in range(50):
            if m.images.phase == "idle":
                break
            await asyncio.sleep(0.02)
        assert server.calls == []
        assert not m.images.gpu_taken and m.images.phase == "idle"
        await m.stop()

        m, server, _ = image_manager(tmp_path / "mix")
        skipped = m.images.submit("skip me")
        kept = m.images.submit("keep me")
        m.images.cancel(skipped["id"])
        await m.start(maintenance=False)
        assert (await m.images.wait(skipped["id"]))["status"] == "failed"
        assert (await m.images.wait(kept["id"]))["status"] == "done"
        for _ in range(100):
            if m.images.phase == "idle":
                break
            await asyncio.sleep(0.02)
        assert server.calls == ["stop", "start"]
        assert not m.images.gpu_taken
        await m.stop()

        class Hold:
            active = True
            manual = False

        m, server, _ = image_manager(tmp_path / "hold")
        await m.start(maintenance=False)
        m.images.runner.guard = Hold()
        held = m.images.submit("held while queued")
        for _ in range(80):
            if m.images.phase == "waiting":
                break
            await asyncio.sleep(0.02)
        assert m.images.phase == "waiting"
        m.images.cancel(held["id"])
        Hold.active = False
        m.images._guard_wake.set()
        assert (await m.images.wait(held["id"]))["status"] == "failed"
        for _ in range(50):
            if m.images.phase == "idle":
                break
            await asyncio.sleep(0.02)
        assert server.calls == []
        assert m.images.phase == "idle"
        await m.stop()
    asyncio.run(body())


def test_manifest_pins_public_apache_artifacts():
    m = load_manifest()
    assert m["license"] == "Apache-2.0"
    ckpt = m["assets"]["checkpoint"]
    assert ckpt["revision"] == "5b4408e59397a4a37ccb46afe426d8ed86379441"
    assert ckpt["bytes"] == 4070624520
    assert m["steps"] == 4 and m["guidance"] == 1.0
    assert m["comfyui"]["pinned_portable"]["tag"] == "v0.36.0"
    assert "latest" not in m["comfyui"]["pinned_portable"]["url"]
    assert RESERVE_BYTES == 5 * 1024 ** 3
