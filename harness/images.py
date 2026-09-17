"""Image generation with a local ComfyUI (Phase 6d).

The tower's 16 GB GPU holds either the language model (Qwen, ~14.7 GB) or an image model, never both. An image job
therefore takes the GPU over:

1. wait until the GPU guard is clear (no game or Plex transcode) and take the InferenceGate exclusively (the model
   call in flight finishes; agent turns wait; endpoint requests get 503);
2. stop llama-server through the guard's pause flag (its supervisor waits while the flag exists);
3. start ComfyUI (portable install, launched hidden by the daemon) without loading a checkpoint until a prompt runs;
4. run queued workflows, then stop ComfyUI immediately, remove the flag (llama-server restarts and reloads Qwen, ~1 min)
   and release the gate.

Opening the Images tab (owner only) starts ComfyUI in this same GPU session so Generate is not paying the ~45s boot.
A CUDA context is unavoidable on the portable install, so warmup unloads Qwen rather than sharing the 16 GB card.
After the last job the GPU is given back immediately; there is no linger. Leaving the Images tab cancels an unused
warmup.

Jobs come from the phone (POST /images) and from agents (the `generate_image` tool). Two workflows, from ComfyUI's own
templates: `fast` = Z-Image-Turbo (Apache 2.0, 8 steps; assets agents may ship) and `quality` = Qwen-Image-2512
(Apache 2.0, 20B fp8, best text rendering; slower, part of it runs from RAM). Inputs the workflow doesn't support
are ignored. Upscaling is opt-in Real-ESRGAN 2×/4× (never the default; Hermes lesson: default-on degraded text and
faces). A requested upscale runs in the same GPU occupancy; a later gallery action is a queued image job. The original
PNG is preserved; the result is a linked row. Results are PNGs under data_dir/images, served by GET /images/{id}.png.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import httpx

from .config import ImagesConfig
from .fileops import ToolError
from . import upscale as upscale_mod

log = logging.getLogger("harness.images")

TOOLS = ("generate_image",)
ASPECTS = ("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3")
RESOLUTIONS = {"standard": "Standard", "high": "High"}
RESOLUTION_SIZES = {
    # Qwen-Image's native sizes (template notes); Z-Image-Turbo trained around 1024 px
    "high": {"1:1": (1328, 1328), "16:9": (1664, 928), "9:16": (928, 1664), "4:3": (1472, 1104),
                "3:4": (1104, 1472), "3:2": (1584, 1056), "2:3": (1056, 1584)},
    "standard": {"1:1": (1024, 1024), "16:9": (1344, 768), "9:16": (768, 1344), "4:3": (1152, 864),
             "3:4": (864, 1152), "3:2": (1216, 832), "2:3": (832, 1216)},
}
MODEL_RESOLUTION = {"fast": "standard", "quality": "high"}
MODELS = {
    "fast": {"label": "Z-Image-Turbo (fast, Apache 2.0)", "negative": False},
    "quality": {"label": "Qwen-Image-2512 (quality, Apache 2.0)", "negative": True},
}
QWEN_NEGATIVE = ("low resolution, low quality, deformed limbs, deformed fingers, oversaturated, waxy, no facial "
                 "detail, over-smoothed, AI look, cluttered composition, blurry text, distorted text")


def workspace_png_name(filename: str) -> str:
    """Workspace-relative PNG path. Rejects escapes before any bytes are written or transferred."""
    name = str(filename or "").strip().replace("\\", "/").lstrip("/")
    if not name.lower().endswith(".png"):
        name += ".png"
    parts = Path(name).parts
    if not name or Path(name).is_absolute() or ".." in parts:
        raise ToolError(f"filename escapes the workspace: {filename}")
    return name


def schemas(cfg: ImagesConfig) -> list[dict]:
    return [{"type": "function", "function": {
        "name": "generate_image",
        "description": "Generate an image from a text prompt with a local model and save it as a PNG in the workspace. "
                       "Slow: the language model is unloaded while it runs (about 1-3 minutes in total). 'fast' "
                       "(default) suits icons, placeholders and illustrations; 'quality' renders text and detail "
                       "better but takes several minutes.",
        "parameters": {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "Detailed description of the image."},
            "filename": {"type": "string", "description": "Where to save it in the workspace, e.g. assets/logo.png"},
            "aspect_ratio": {"type": "string", "description": f"One of {', '.join(ASPECTS)}. Default 1:1."},
            "resolution": {"type": "string", "description": "standard or high. Defaults to the model's native size."},
            "model": {"type": "string", "description": "fast or quality. Default fast."},
            "upscale": {"type": "string", "description": "none (default), 2x, or 4x. Opt-in Real-ESRGAN; omitted or "
                        "none leaves the generated PNG unchanged. Requires optional Real-ESRGAN weights."},
        }, "required": ["prompt", "filename"]},
    }}]


def workflow(model: str, prompt: str, width: int, height: int, seed: int, prefix: str) -> dict:
    """ComfyUI API-format graph, transcribed from the bundled templates image_z_image_turbo.json and
    image_qwen_Image_2512.json (without the optional Lightning LoRA)."""
    if model == "fast":
        return {
            "28": {"class_type": "UNETLoader", "inputs": {"unet_name": "z_image_turbo_bf16.safetensors",
                                                          "weight_dtype": "default"}},
            "11": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["28", 0], "shift": 3}},
            "30": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_3_4b.safetensors", "type": "lumina2",
                                                          "device": "default"}},
            "27": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["30", 0], "text": prompt}},
            "33": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["27", 0]}},
            "13": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
            "3": {"class_type": "KSampler", "inputs": {"model": ["11", 0], "positive": ["27", 0], "negative": ["33", 0],
                                                       "latent_image": ["13", 0], "seed": seed, "steps": 8, "cfg": 1,
                                                       "sampler_name": "res_multistep", "scheduler": "simple",
                                                       "denoise": 1}},
            "29": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["29", 0]}},
            "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": prefix}},
        }
    return {
        "226": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen_image_2512_fp8_e4m3fn.safetensors",
                                                       "weight_dtype": "default"}},
        "222": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["226", 0], "shift": 3.1}},
        "219": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
                                                       "type": "qwen_image", "device": "default"}},
        "227": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["219", 0], "text": prompt}},
        "228": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["219", 0], "text": QWEN_NEGATIVE}},
        "232": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "230": {"class_type": "KSampler", "inputs": {"model": ["222", 0], "positive": ["227", 0], "negative": ["228", 0],
                                                     "latent_image": ["232", 0], "seed": seed, "steps": 50, "cfg": 4,
                                                     "sampler_name": "euler", "scheduler": "simple", "denoise": 1}},
        "220": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
        "231": {"class_type": "VAEDecode", "inputs": {"samples": ["230", 0], "vae": ["220", 0]}},
        "60": {"class_type": "SaveImage", "inputs": {"images": ["231", 0], "filename_prefix": prefix}},
    }


async def _run(args: list[str]) -> tuple[int, str, str]:
    from .sandbox import run_cmd
    return await run_cmd(args, timeout=30)


async def _ws_read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    hdr = await reader.readexactly(2)
    opcode = hdr[0] & 0x0F
    masked = hdr[1] & 0x80
    length = hdr[1] & 0x7F
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), "big")
    mask = await reader.readexactly(4) if masked else b""
    payload = bytearray(await reader.readexactly(length))
    if mask:
        for i, byte in enumerate(payload):
            payload[i] = byte ^ mask[i % 4]
    return opcode, bytes(payload)


class ComfyProcess:
    """Starts and stops the portable ComfyUI as a hidden child process."""

    def __init__(self, cfg: ImagesConfig):
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.cfg.port}"

    async def ready(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                return (await client.get(f"{self.url}/system_stats")).status_code == 200
        except httpx.HTTPError:
            return False

    async def start(self) -> None:
        if await self.ready():
            return
        root = Path(self.cfg.comfy_dir)
        log_dir = Path(self.cfg.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        out = open(log_dir / "comfyui.log", "ab")
        work = Path(self.cfg.work_dir)
        for name in ("output", "temp", "input"):
            (work / name).mkdir(parents=True, exist_ok=True)
        args = [str(root / "python_embeded" / "python.exe"), "-s", str(root / "ComfyUI" / "main.py"),
                "--listen", "127.0.0.1", "--port", str(self.cfg.port), "--disable-auto-launch",
                "--extra-model-paths-config", str(root / "ComfyUI" / "extra_model_paths.yaml"),
                "--output-directory", str(work / "output"),
                "--temp-directory", str(work / "temp"),
                "--input-directory", str(work / "input")]
        log.info("starting ComfyUI")
        self.proc = subprocess.Popen(args, cwd=str(root), stdout=out, stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        started = time.monotonic()
        while time.monotonic() - started < self.cfg.start_timeout_seconds:
            if self.proc.poll() is not None:
                raise ToolError(f"ComfyUI exited while starting (code {self.proc.returncode}); see "
                                f"{log_dir / 'comfyui.log'}")
            if await self.ready():
                log.info("ComfyUI ready after %.0f s", time.monotonic() - started)
                return
            await asyncio.sleep(1)
        raise ToolError("ComfyUI didn't start in time")

    async def stop(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(f"{self.url}/free", json={"unload_models": True, "free_memory": True})
        except httpx.HTTPError:
            pass
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                await asyncio.to_thread(self.proc.wait, 30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        log.info("ComfyUI stopped")

    async def interrupt(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(f"{self.url}/interrupt")
        except httpx.HTTPError:
            pass


class ImageService:
    tool_names = TOOLS

    def __init__(self, cfg: ImagesConfig, db, runner, control, notify=None):
        self.cfg = cfg
        self.control = control         # gpu_guard.ServerControl for the language model server
        self.db = db
        self.runner = runner
        self.comfy = ComfyProcess(cfg)
        self.notify = notify           # callable(job) for finished phone jobs
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.active_job: str = ""
        self.phase = "idle"            # idle | waiting | switching | starting | warm | generating | restoring
        self.progress: dict = {}
        self._task: asyncio.Task | None = None
        self._done: dict[str, asyncio.Event] = {}
        self._keep_warm = False
        self._wake: asyncio.Event | None = None
        self._guard_wake = asyncio.Event()
        self._drain_sessions: set[str] = set()
        self.images_dir = Path(cfg.work_dir) / "images"
        self.input_dir = Path(cfg.work_dir) / "input"
        self.transport = None          # tests inject a fake ComfyUI
        self.on_stored = None          # optional archive hook: callable(job) after a PNG is written

    def schemas(self) -> list[dict]:
        return schemas(self.cfg)

    @property
    def gpu_taken(self) -> bool:
        return self.phase in ("switching", "starting", "warm", "generating", "restoring")

    def start(self) -> None:
        if self._task is None:
            asyncio.get_running_loop().create_task(self._stop_stray())
            for job in self.db.list_images(status=("queued", "running")):  # interrupted by a daemon restart
                self.db.update_image(job["id"], status="queued")
                self.queue.put_nowait(job["id"])
            for job in self.db.list_images(limit=500):
                if job["status"] != "done":
                    continue
                requested = job.get("requested_upscale") or "none"
                if job.get("operation", "generate") != "generate" or requested in ("", "none"):
                    continue
                if self.db.find_image_upscale(job["id"], requested) is None:
                    try:
                        child = self.submit_upscale(job["id"], requested, source=job.get("source") or "phone",
                                                    session_id=job.get("session_id") or "")
                        log.info("re-queued upscale %s for image %s after restart", child["id"], job["id"])
                    except ToolError as e:
                        log.warning("could not recover upscale for %s: %s", job["id"], e)
            self._task = asyncio.create_task(self._loop(), name="images")

    async def _stop_stray(self) -> None:
        """A ComfyUI left running by a daemon that crashed mid-batch holds the GPU: stop it and restore the model."""
        if self.phase == "idle" and await self.comfy.ready():
            log.warning("stopping a ComfyUI left over from before the daemon started")
            await self.comfy.stop()
            code, out, _ = await _run(["netstat", "-ano", "-p", "TCP"])
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{self.cfg.port}"):
                    await _run(["taskkill", "/PID", parts[4], "/F", "/T"])

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self.comfy.proc:
            await self.comfy.stop()

    # jobs
    def submit(self, prompt: str, model: str = "fast", aspect_ratio: str = "1:1", resolution: str = "auto",
               source: str = "phone", session_id: str = "", seed: int | None = None,
               upscale: str = "none") -> dict:
        prompt = prompt.strip()
        if not prompt:
            raise ToolError("prompt is empty")
        if model not in MODELS:
            raise ToolError(f"model must be one of {', '.join(MODELS)}")
        if aspect_ratio not in ASPECTS:
            raise ToolError(f"aspect_ratio must be one of {', '.join(ASPECTS)}")
        if resolution == "auto":
            resolution = MODEL_RESOLUTION[model]
        if resolution not in RESOLUTIONS:
            raise ToolError(f"resolution must be one of {', '.join(RESOLUTIONS)}")
        requested = upscale_mod.parse_choice(upscale)
        if requested != "none":
            scale = upscale_mod.SCALES[requested]
            width, height = RESOLUTION_SIZES[resolution][aspect_ratio]
            upscale_mod.require_weights(self.cfg, scale)
            upscale_mod.check_dimensions(width, height, scale, upscale_mod.max_pixels(self.cfg))
        width, height = RESOLUTION_SIZES[resolution][aspect_ratio]
        job = {"id": uuid.uuid4().hex[:12], "session_id": session_id, "source": source, "prompt": prompt[:4000],
               "model": model, "aspect_ratio": aspect_ratio, "resolution": resolution, "width": width, "height": height,
               "seed": seed if seed is not None else random.randrange(2**48),
               "parent_id": "", "operation": "generate", "scale": 1, "upscale_model": "",
               "requested_upscale": requested}
        self.db.insert_image(job)
        self._done[job["id"]] = asyncio.Event()
        self.queue.put_nowait(job["id"])
        return self.db.get_image(job["id"])

    def submit_upscale(self, parent_id: str, upscale: str, source: str = "phone", session_id: str = "") -> dict:
        """Queue a 2×/4× Real-ESRGAN job from a completed gallery PNG. Idempotent per parent+scale."""
        requested = upscale_mod.parse_choice(upscale)
        if requested == "none":
            raise ToolError("upscale must be 2x or 4x")
        parent = self.db.get_image(parent_id)
        if parent is None or parent["status"] != "done":
            raise ToolError("upscale needs a completed image")
        if not self.path(parent).is_file():
            raise ToolError("upscale needs the original PNG")
        scale = upscale_mod.SCALES[requested]
        spec = upscale_mod.require_weights(self.cfg, scale)
        out_w, out_h = upscale_mod.check_dimensions(
            parent["width"], parent["height"], scale, upscale_mod.max_pixels(self.cfg))
        existing = self.db.find_image_upscale(parent["id"], requested)
        if existing is not None:
            if existing["status"] in ("queued", "running", "done"):
                return existing
            self.db.update_image(existing["id"], status="queued", error="", started_at=None, finished_at=None,
                                 seconds=0, bytes=0)
            self._done[existing["id"]] = asyncio.Event()
            self.queue.put_nowait(existing["id"])
            return self.db.get_image(existing["id"])
        job = {"id": uuid.uuid4().hex[:12], "session_id": session_id or parent.get("session_id") or "",
               "source": source or parent.get("source") or "phone",
               "prompt": parent["prompt"], "model": parent["model"], "aspect_ratio": parent["aspect_ratio"],
               "resolution": parent.get("resolution") or "auto", "width": out_w, "height": out_h,
               "seed": parent["seed"], "parent_id": parent["id"], "operation": "upscale", "scale": scale,
               "upscale_model": spec.key, "requested_upscale": requested}
        self.db.insert_image(job)
        self._done[job["id"]] = asyncio.Event()
        self.queue.put_nowait(job["id"])
        return self.db.get_image(job["id"])

    async def wait(self, job_id: str) -> dict:
        event = self._done.setdefault(job_id, asyncio.Event())
        while True:
            job = self.db.get_image(job_id)
            if job["status"] in ("done", "failed"):
                return job
            try:
                await asyncio.wait_for(event.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass

    def path(self, job: dict) -> Path:
        return self.images_dir / f"{job['id']}.png"

    async def _loop(self) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                await self._run_batch(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep serving jobs
                log.exception("image batch failed")
                self.phase = "idle"

    async def warmup(self) -> dict:
        """Owner opened the Images tab: start ComfyUI without a checkpoint so Generate skips the boot."""
        self._keep_warm = True
        if self.phase == "idle":
            self.queue.put_nowait(None)
        return self.status()

    def cooldown(self) -> dict:
        """Owner left the Images tab: drop an unused warmup and restore the language model."""
        self._keep_warm = False
        if self._wake is not None:
            self._wake.set()
        return self.status()

    def hold(self) -> None:
        """Cancel an idle warmup when a GPU hold begins; a running image is allowed to finish."""
        self._keep_warm = False
        if self._wake is not None:
            self._wake.set()

    def drain_after_sessions(self, session_ids) -> None:
        """Make queued images wait for the local sessions that were held by the guard."""
        self._drain_sessions = set(session_ids) if self.phase == "waiting" or not self.queue.empty() else set()
        self._guard_wake.set()

    def apply_comfy_progress(self, job_id: str, started: float, message: dict) -> None:
        """Update status from a ComfyUI websocket `progress` event (`data.value` / `data.max`)."""
        if message.get("type") != "progress":
            return
        data = message.get("data") or {}
        maximum = data.get("max")
        if not maximum:
            return
        self.progress = {
            **self.progress,
            "job": job_id,
            "stage": "generating",
            "value": int(data.get("value") or 0),
            "max": int(maximum),
            "seconds": round(time.time() - started),
        }

    async def _run_batch(self, first: str | None) -> None:
        """Take the GPU over, run queued jobs (or sit warm until Generate), then give the GPU back."""
        guard = self.runner.guard
        self.phase = "waiting"
        paused = lambda: guard is not None and (guard.active or guard.manual)  # noqa: E731
        while paused():  # a game or a transcode: wait for it like agent turns do
            try:
                await asyncio.wait_for(self._guard_wake.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            self._guard_wake.clear()
        if self._drain_sessions:
            drain = self._drain_sessions
            self._drain_sessions = set()
            await self.runner.scheduler.wait_for_drain(drain)
        slot = await self.runner.gate.acquire_exclusive()
        flagged = False
        ran_job = False
        try:
            self.phase = "switching"
            await self.control.stop()  # stop llama-server; its supervisor waits while the flag exists
            flagged = True
            self.phase = "starting"
            await self.comfy.start()
            job_id: str | None = first
            while True:
                if paused():
                    if job_id:
                        self.db.update_image(job_id, status="queued")
                        self.queue.put_nowait(job_id)
                    break
                if job_id:
                    await self._run_job(job_id)
                    ran_job = True
                    job_id = None
                if not self.queue.empty():
                    job_id = self.queue.get_nowait()
                    continue
                if self._keep_warm and not ran_job:
                    self.phase = "warm"
                    self.progress = {}
                    self._wake = asyncio.Event()
                    waiter = asyncio.create_task(self.queue.get())
                    wake = asyncio.create_task(self._wake.wait())
                    done, pending = await asyncio.wait({waiter, wake}, return_when=asyncio.FIRST_COMPLETED)
                    for task in pending:
                        task.cancel()
                    for task in pending:
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                    self._wake = None
                    if waiter in done:
                        job_id = waiter.result()
                        continue
                    break
                break
        finally:
            self._keep_warm = False
            self._wake = None
            self.phase = "restoring"
            await self.comfy.stop()
            if flagged and not paused():  # when the guard is paused it restores the model itself later
                await self.control.start()  # llama-server restarts and reloads the model
                for _ in range(150):
                    if await self.control.healthy():
                        break
                    await asyncio.sleep(2)
            await slot.release()
            self.phase = "idle"
            self.progress = {}

    async def _run_job(self, job_id: str | None) -> None:
        if not job_id:
            return
        job = self.db.get_image(job_id)
        if job is None or job["status"] in ("done", "failed"):
            return
        self.active_job = job_id
        self.phase = "generating"
        started = time.time()
        self.db.update_image(job_id, status="running", started_at=started)
        upscaling = job.get("operation") == "upscale"
        self.progress = {"job": job_id, "stage": "upscaling" if upscaling else "queued in ComfyUI"}
        stop_progress = asyncio.Event()
        listener = asyncio.create_task(self._listen_progress(job_id, started, stop_progress))
        try:
            if upscaling:
                content = await self._run_upscale(job, started)
            else:
                content = await self._run_generate(job, started)
            self.images_dir.mkdir(parents=True, exist_ok=True)
            self.path(job).write_bytes(content)
            self.db.update_image(job_id, status="done", finished_at=time.time(), seconds=round(time.time() - started, 1),
                                 bytes=len(content), width=job["width"], height=job["height"])
            if self.on_stored:
                try:
                    self.on_stored(self.db.get_image(job_id))
                except Exception:  # noqa: BLE001 - archive must not fail the image job
                    log.exception("image archive hook failed for %s", job_id)
            log.info("image %s (%s) done in %.0f s", job_id, job.get("upscale_model") or job["model"],
                     time.time() - started)
            if not upscaling:
                await self._queue_requested_upscale(job)
        except (ToolError, httpx.HTTPError, KeyError, ValueError, OSError) as e:
            self.db.update_image(job_id, status="failed", finished_at=time.time(), error=str(e)[:1000])
            log.warning("image %s failed: %s", job_id, e)
        finally:
            stop_progress.set()
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
            self.active_job = ""
            event = self._done.pop(job_id, None)
            if event:
                event.set()
            finished = self.db.get_image(job_id)
            if self.notify and finished and finished["source"] == "phone":
                if finished.get("operation") == "generate" and (finished.get("requested_upscale") or "none") != "none":
                    pass  # notify when the derived upscale settles
                else:
                    self.notify(finished)

    async def _queue_requested_upscale(self, job: dict) -> None:
        requested = job.get("requested_upscale") or "none"
        if requested in ("", "none"):
            return
        try:
            self.submit_upscale(job["id"], requested, source=job.get("source") or "phone",
                                session_id=job.get("session_id") or "")
        except ToolError as e:
            failed = {"id": uuid.uuid4().hex[:12], "session_id": job.get("session_id") or "",
                      "source": job.get("source") or "phone", "prompt": job["prompt"], "model": job["model"],
                      "aspect_ratio": job["aspect_ratio"], "resolution": job.get("resolution") or "auto",
                      "width": job["width"], "height": job["height"], "seed": job["seed"],
                      "parent_id": job["id"], "operation": "upscale", "scale": upscale_mod.SCALES.get(requested, 1),
                      "upscale_model": "", "requested_upscale": requested}
            self.db.insert_image(failed)
            self.db.update_image(failed["id"], status="failed", finished_at=time.time(), error=str(e)[:1000])
            event = self._done.pop(failed["id"], None)
            if event:
                event.set()
            if self.notify and str(job.get("source") or "") == "phone":
                self.notify({**failed, "status": "failed", "error": str(e)[:1000]})

    async def _run_generate(self, job: dict, started: float) -> bytes:
        prefix = f"harness/{job['id']}"
        graph = workflow(job["model"], job["prompt"], job["width"], job["height"], job["seed"], prefix)
        return await self._comfy_png(job["id"], graph, started, stage="generating")

    async def _run_upscale(self, job: dict, started: float) -> bytes:
        parent = self.db.get_image(job["parent_id"])
        if parent is None or not self.path(parent).is_file():
            raise ToolError("upscale needs the original PNG")
        source = self.path(parent).read_bytes()
        spec = upscale_mod.require_weights(self.cfg, int(job.get("scale") or 0))
        self.input_dir.mkdir(parents=True, exist_ok=True)
        image_name = f"{parent['id']}.png"
        (self.input_dir / image_name).write_bytes(source)
        prefix = f"harness/{job['id']}"
        graph = upscale_mod.workflow(image_name, spec.filename, job["width"], job["height"], prefix)
        result = await self._comfy_png(job["id"], graph, started, stage="upscaling")
        content = upscale_mod.preserve_alpha(source, result)
        size = upscale_mod.png_size(content)
        if size:
            job["width"], job["height"] = size
        if self.path(parent).read_bytes() != source:
            raise ToolError("upscale refused to modify the original PNG")
        return content

    async def _comfy_png(self, job_id: str, graph: dict, started: float, stage: str) -> bytes:
        async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
            resp = await client.post(f"{self.comfy.url}/prompt", json={"prompt": graph,
                                                                         "client_id": "agent-harness"})
            if resp.status_code != 200:
                raise ToolError(f"ComfyUI refused the workflow: {resp.text[:500]}")
            prompt_id = resp.json()["prompt_id"]
            deadline = time.monotonic() + self.cfg.job_timeout_seconds
            hist = None
            while True:
                if time.monotonic() > deadline:
                    await self.comfy.interrupt()
                    raise ToolError("image generation timed out" if stage != "upscaling" else "upscale timed out")
                hist = (await client.get(f"{self.comfy.url}/history/{prompt_id}")).json().get(prompt_id)
                if hist:
                    status = hist.get("status") or {}
                    if status.get("status_str") == "error":
                        messages = [m for m in status.get("messages", []) if m and m[0] == "execution_error"]
                        detail = messages[0][1].get("exception_message", "") if messages else "unknown error"
                        if "out of memory" in detail.lower() or "oom" in detail.lower():
                            raise ToolError(f"upscale ran out of memory: {detail[:500]}" if stage == "upscaling"
                                            else f"ComfyUI error: {detail[:500]}")
                        raise ToolError(f"ComfyUI error: {detail[:500]}")
                    if status.get("completed"):
                        break
                self.progress = {**self.progress, "job": job_id, "stage": stage,
                                 "seconds": round(time.time() - started)}
                await asyncio.sleep(0.4)
            images = [img for out in hist.get("outputs", {}).values() for img in out.get("images", [])]
            if not images:
                raise ToolError("ComfyUI finished without an image")
            img = images[0]
            data = await client.get(f"{self.comfy.url}/view", params={
                "filename": img["filename"], "subfolder": img.get("subfolder", ""), "type": img.get("type", "output")})
            data.raise_for_status()
        return data.content

    async def _listen_progress(self, job_id: str, started: float, stop: asyncio.Event) -> None:
        """Read ComfyUI websocket progress until the job finishes. No-op in tests (MockTransport)."""
        if self.transport is not None:
            return
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.cfg.port), timeout=2)
            key = base64.b64encode(os.urandom(16)).decode()
            writer.write(
                f"GET /ws?clientId=agent-harness-{job_id} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.cfg.port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
            await writer.drain()
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            if b" 101 " not in header.split(b"\r\n", 1)[0]:
                return
            while not stop.is_set():
                opcode, payload = await _ws_read_frame(reader)
                if opcode == 8:
                    break
                if opcode != 1:
                    continue
                try:
                    self.apply_comfy_progress(job_id, started, json.loads(payload))
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.CancelledError):
            return
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    def status(self) -> dict:
        return {"enabled": self.cfg.enabled, "phase": self.phase, "active_job": self.active_job,
                "queued": self.queue.qsize(), "progress": self.progress,
                "models": {k: v["label"] for k, v in MODELS.items()}, "aspect_ratios": list(ASPECTS),
                "resolutions": {name: {"label": label, "sizes": {aspect: list(size) for aspect, size in RESOLUTION_SIZES[name].items()}}
                                for name, label in RESOLUTIONS.items()},
                "upscale": upscale_mod.status(self.cfg)}

    # agent tool
    async def call(self, name: str, args: dict, workspace_root: Path | None = None, put_bytes=None) -> str:
        filename = workspace_png_name(args.get("filename") or "")
        target: Path | None = None
        if workspace_root is not None:
            target = (workspace_root / filename).resolve()
            if not target.is_relative_to(workspace_root.resolve()):
                raise ToolError(f"filename escapes the workspace: {filename}")
        elif put_bytes is None:
            raise ToolError("generate_image needs a workspace path or a runner transfer")
        requested = upscale_mod.parse_choice(args.get("upscale"))
        job = self.submit(args["prompt"], model=args.get("model") or "fast",
                          aspect_ratio=args.get("aspect_ratio") or "1:1", resolution=args.get("resolution") or "auto",
                          source="agent",
                          session_id=args.get("_session", ""), upscale=requested)
        job = await self.wait(job["id"])
        if job["status"] != "done":
            raise ToolError(f"image generation failed: {job['error']}")
        result = job
        if requested != "none":
            child = self.db.find_image_upscale(job["id"], requested)
            if child is None:
                child = self.submit_upscale(job["id"], requested, source="agent",
                                            session_id=args.get("_session", ""))
            child = await self.wait(child["id"])
            if child["status"] != "done":
                raise ToolError(f"image generated but upscale failed: {child['error']}")
            result = child
        if put_bytes is not None:
            await put_bytes(filename, self.path(result).read_bytes())
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.path(result), target)
        extra = ""
        if requested != "none":
            extra = f", upscaled {requested} with {result.get('upscale_model') or 'Real-ESRGAN'}"
        return (f"Saved {filename} ({result['width']}x{result['height']}, {job['model']} model, seed {job['seed']}, "
                f"{result['seconds']:.0f} s{extra}). The user can see it in the app's Images screen.")
