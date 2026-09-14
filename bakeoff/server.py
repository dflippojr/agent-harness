"""Start/stop llama-server for one model profile and sample GPU memory."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import httpx
import yaml

CONFIG_PATH = Path(__file__).with_name("models.yaml")


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text())


def gpu_memory_used_mib() -> int:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True,
    ).stdout
    return int(out.strip().splitlines()[0])


class GpuSampler:
    """Polls nvidia-smi in the background and remembers the peak."""

    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self.peak_mib = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.peak_mib = max(self.peak_mib, gpu_memory_used_mib())
            except (subprocess.SubprocessError, ValueError):
                pass
            self._stop.wait(self.interval)

    def __enter__(self) -> "GpuSampler":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()


class LlamaServer:
    def __init__(self, model_name: str, log_path: Path, config: dict | None = None):
        self.config = config or load_config()
        self.model_name = model_name
        self.profile = self.config["models"][model_name]
        self.port = self.config["port"]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None
        self.load_seconds: float | None = None
        self.vram_after_load_mib: int | None = None

    def command(self) -> list[str]:
        return [
            self.config["llama_server"],
            "-m", self.profile["path"],
            "--port", str(self.port),
            "--ctx-size", str(self.config["ctx_size"]),
            "--alias", self.model_name,
            *self.config["common_args"],
            *self.profile.get("args", []),
        ]

    def __enter__(self) -> "LlamaServer":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self, timeout: float = 900) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(self.log_path, "w", encoding="utf-8")
        started = time.monotonic()
        self.proc = subprocess.Popen(self.command(), stdout=log, stderr=subprocess.STDOUT)
        while time.monotonic() - started < timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server exited with {self.proc.returncode}; see {self.log_path}")
            try:
                if httpx.get(f"{self.base_url}/health", timeout=5).status_code == 200:
                    self.load_seconds = time.monotonic() - started
                    self.vram_after_load_mib = gpu_memory_used_mib()
                    return
            except httpx.HTTPError:
                pass
            time.sleep(2)
        self.stop()
        raise TimeoutError(f"llama-server did not become healthy in {timeout}s; see {self.log_path}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
