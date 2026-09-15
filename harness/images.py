"""Image generation with a local ComfyUI (Phase 6d).

The tower's 16 GB GPU holds either the language model (Qwen, ~14.7 GB) or an image model, never both. An image job
therefore takes the GPU over:

1. wait until the GPU guard is clear (no game or Plex transcode) and take the InferenceGate exclusively (the model
   call in flight finishes; agent turns wait; endpoint requests get 503);
2. stop llama-server through the guard's pause flag (its supervisor waits while the flag exists);
3. start ComfyUI (portable install, launched hidden by the daemon only while jobs run) and run the workflow;
4. keep ComfyUI warm for `linger_seconds` in case more jobs come, then stop it, remove the flag (llama-server restarts
   and reloads Qwen, ~1 min) and release the gate.

Jobs come from the phone (POST /images) and from agents (the `generate_image` tool). Two workflows, from ComfyUI's own
templates: `fast` = Z-Image-Turbo (Apache 2.0, 8 steps; assets agents may ship) and `quality` = Qwen-Image-2512
(Apache 2.0, 20B fp8, best text rendering; slower, part of it runs from RAM). Inputs the workflow doesn't support
are ignored, upscaling isn't done by default (lessons from Hermes Agent's image tool, docs/phase6a-hermes-study.md).
Results are PNGs under data_dir/images, served by GET /images/{id}.png.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import httpx

from .config import ImagesConfig
from .fileops import ToolError

log = logging.getLogger("harness.images")

TOOLS = ("generate_image",)
ASPECTS = ("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3")
SIZES = {
    # Qwen-Image's native sizes (template notes); Z-Image-Turbo trained around 1024 px
    "quality": {"1:1": (1328, 1328), "16:9": (1664, 928), "9:16": (928, 1664), "4:3": (1472, 1104),
                "3:4": (1104, 1472), "3:2": (1584, 1056), "2:3": (1056, 1584)},
    "fast": {"1:1": (1024, 1024), "16:9": (1344, 768), "9:16": (768, 1344), "4:3": (1152, 864),
             "3:4": (864, 1152), "3:2": (1216, 832), "2:3": (832, 1216)},
}
MODELS = {
    "fast": {"label": "Z-Image-Turbo (fast, Apache 2.0)", "negative": False},
    "quality": {"label": "Qwen-Image-2512 (quality, Apache 2.0)", "negative": True},
}
QWEN_NEGATIVE = ("low resolution, low quality, deformed limbs, deformed fingers, oversaturated, waxy, no facial "
                 "detail, over-smoothed, AI look, cluttered composition, blurry text, distorted text")


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
            "model": {"type": "string", "description": "fast or quality. Default fast."},
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
        args = [str(root / "python_embeded" / "python.exe"), "-s", str(root / "ComfyUI" / "main.py"),
                "--listen", "127.0.0.1", "--port", str(self.cfg.port), "--disable-auto-launch",
                "--extra-model-paths-config", str(root / "ComfyUI" / "extra_model_paths.yaml"),
                "--output-directory", str(Path(self.cfg.work_dir) / "output"),
                "--temp-directory", str(Path(self.cfg.work_dir) / "temp")]
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
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.active_job: str = ""
        self.phase = "idle"            # idle | waiting | switching | starting | generating | lingering | restoring
        self.progress: dict = {}
        self._task: asyncio.Task | None = None
        self._done: dict[str, asyncio.Event] = {}
        self.images_dir = Path(cfg.work_dir) / "images"
        self.transport = None          # tests inject a fake ComfyUI

    def schemas(self) -> list[dict]:
        return schemas(self.cfg)

    @property
    def gpu_taken(self) -> bool:
        return self.phase in ("switching", "starting", "generating", "lingering", "restoring")

    def start(self) -> None:
        if self._task is None:
            asyncio.get_running_loop().create_task(self._stop_stray())
            for job in self.db.list_images(status=("queued", "running")):  # interrupted by a daemon restart
                self.db.update_image(job["id"], status="queued")
                self.queue.put_nowait(job["id"])
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
    def submit(self, prompt: str, model: str = "fast", aspect_ratio: str = "1:1", source: str = "phone",
               session_id: str = "", seed: int | None = None) -> dict:
        prompt = prompt.strip()
        if not prompt:
            raise ToolError("prompt is empty")
        if model not in MODELS:
            raise ToolError(f"model must be one of {', '.join(MODELS)}")
        if aspect_ratio not in ASPECTS:
            raise ToolError(f"aspect_ratio must be one of {', '.join(ASPECTS)}")
        width, height = SIZES[model][aspect_ratio]
        job = {"id": uuid.uuid4().hex[:12], "session_id": session_id, "source": source, "prompt": prompt[:4000],
               "model": model, "aspect_ratio": aspect_ratio, "width": width, "height": height,
               "seed": seed if seed is not None else random.randrange(2**48)}
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

    async def _run_batch(self, first: str) -> None:
        """Take the GPU over, run jobs until the queue has been empty for linger_seconds, give the GPU back."""
        guard = self.runner.guard
        self.phase = "waiting"
        paused = lambda: guard is not None and guard.active  # noqa: E731
        while paused():  # a game or a transcode: wait for it like agent turns do
            await asyncio.sleep(5)
        slot = await self.runner.gate.acquire_exclusive()
        flagged = False
        try:
            self.phase = "switching"
            await self.control.stop()  # stop llama-server; its supervisor waits while the flag exists
            flagged = True
            self.phase = "starting"
            await self.comfy.start()
            job_id: str | None = first
            while job_id is not None:
                if paused():
                    # a game started mid-batch: put the job back and hand the GPU to the game
                    self.db.update_image(job_id, status="queued")
                    self.queue.put_nowait(job_id)
                    break
                await self._run_job(job_id)
                self.phase = "lingering"
                try:
                    job_id = await asyncio.wait_for(self.queue.get(), timeout=self.cfg.linger_seconds)
                except asyncio.TimeoutError:
                    job_id = None
        finally:
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

    async def _run_job(self, job_id: str) -> None:
        job = self.db.get_image(job_id)
        if job is None or job["status"] in ("done", "failed"):
            return
        self.active_job = job_id
        self.phase = "generating"
        started = time.time()
        self.db.update_image(job_id, status="running", started_at=started)
        self.progress = {"job": job_id, "stage": "queued in ComfyUI"}
        try:
            prefix = f"harness/{job_id}"
            graph = workflow(job["model"], job["prompt"], job["width"], job["height"], job["seed"], prefix)
            async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
                resp = await client.post(f"{self.comfy.url}/prompt", json={"prompt": graph,
                                                                             "client_id": "agent-harness"})
                if resp.status_code != 200:
                    raise ToolError(f"ComfyUI refused the workflow: {resp.text[:500]}")
                prompt_id = resp.json()["prompt_id"]
                deadline = time.monotonic() + self.cfg.job_timeout_seconds
                while True:
                    if time.monotonic() > deadline:
                        await self.comfy.interrupt()
                        raise ToolError("image generation timed out")
                    hist = (await client.get(f"{self.comfy.url}/history/{prompt_id}")).json().get(prompt_id)
                    if hist:
                        status = hist.get("status") or {}
                        if status.get("status_str") == "error":
                            messages = [m for m in status.get("messages", []) if m and m[0] == "execution_error"]
                            detail = messages[0][1].get("exception_message", "") if messages else "unknown error"
                            raise ToolError(f"ComfyUI error: {detail[:500]}")
                        if status.get("completed"):
                            break
                    self.progress = {"job": job_id, "stage": "generating",
                                     "seconds": round(time.time() - started)}
                    await asyncio.sleep(1)
                images = [img for out in hist.get("outputs", {}).values() for img in out.get("images", [])]
                if not images:
                    raise ToolError("ComfyUI finished without an image")
                img = images[0]
                data = await client.get(f"{self.comfy.url}/view", params={
                    "filename": img["filename"], "subfolder": img.get("subfolder", ""), "type": img.get("type", "output")})
                data.raise_for_status()
            self.images_dir.mkdir(parents=True, exist_ok=True)
            self.path(job).write_bytes(data.content)
            self.db.update_image(job_id, status="done", finished_at=time.time(), seconds=round(time.time() - started, 1),
                                 bytes=len(data.content))
            log.info("image %s (%s) done in %.0f s", job_id, job["model"], time.time() - started)
        except (ToolError, httpx.HTTPError, KeyError, ValueError) as e:
            self.db.update_image(job_id, status="failed", finished_at=time.time(), error=str(e)[:1000])
            log.warning("image %s failed: %s", job_id, e)
        finally:
            self.active_job = ""
            event = self._done.pop(job_id, None)
            if event:
                event.set()
            finished = self.db.get_image(job_id)
            if self.notify and finished and finished["source"] == "phone":
                self.notify(finished)

    def status(self) -> dict:
        return {"enabled": self.cfg.enabled, "phase": self.phase, "active_job": self.active_job,
                "queued": self.queue.qsize(), "progress": self.progress,
                "models": {k: v["label"] for k, v in MODELS.items()}, "aspect_ratios": list(ASPECTS)}

    # agent tool
    async def call(self, name: str, args: dict, workspace_root: Path | None = None) -> str:
        filename = str(args.get("filename") or "").strip().replace("\\", "/").lstrip("/")
        if not filename.lower().endswith(".png"):
            filename += ".png"
        if workspace_root is None:
            raise ToolError("generate_image only works in tower sessions for now (the file couldn't be copied to "
                            "the MacBook)")
        target = (workspace_root / filename).resolve()
        if not target.is_relative_to(workspace_root.resolve()):
            raise ToolError(f"filename escapes the workspace: {filename}")
        job = self.submit(args["prompt"], model=args.get("model") or "fast",
                          aspect_ratio=args.get("aspect_ratio") or "1:1", source="agent",
                          session_id=args.get("_session", ""))
        job = await self.wait(job["id"])
        if job["status"] != "done":
            raise ToolError(f"image generation failed: {job['error']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.path(job), target)
        return (f"Saved {filename} ({job['width']}x{job['height']}, {job['model']} model, seed {job['seed']}, "
                f"{job['seconds']:.0f} s). The user can see it in the app's Images screen.")
