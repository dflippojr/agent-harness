"""Docker sandbox: one throwaway container per task, with only the workspace mounted."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

IMAGE = "agent-harness-sandbox:py312"
OUTPUT_LIMIT = 8000


def build_image(dockerfile_dir: Path) -> None:
    subprocess.run(["docker", "build", "-t", IMAGE, str(dockerfile_dir)], check=True)


def truncate_middle(text: str, limit: int = OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} characters truncated] ...\n{text[-half:]}"


class Sandbox:
    def __init__(self, workspace: Path, memory: str = "2g", cpus: str = "2"):
        self.workspace = workspace.resolve()
        self.name = f"harness-{uuid.uuid4().hex[:10]}"
        self.memory = memory
        self.cpus = cpus

    def __enter__(self) -> "Sandbox":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        subprocess.run(
            [
                "docker", "run", "-d", "--rm",
                "--name", self.name,
                "--network", "none",
                "--memory", self.memory,
                "--cpus", self.cpus,
                "--mount", f"type=bind,source={self.workspace},target=/workspace",
                "-w", "/workspace",
                IMAGE, "sleep", "infinity",
            ],
            check=True, capture_output=True,
        )

    def exec(self, command: str, timeout: int = 60) -> tuple[int, str]:
        """Run a shell command in the container. Returns (exit_code, combined output)."""
        # `timeout` inside the container actually stops the process; the host-side
        # timeout is only a backstop in case docker exec itself hangs.
        wrapped = ["timeout", "-k", "5", str(timeout), "sh", "-c", command]
        try:
            proc = subprocess.run(
                ["docker", "exec", "-w", "/workspace", self.name, *wrapped],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout + 30,
            )
        except subprocess.TimeoutExpired:
            return 124, f"command timed out after {timeout}s"
        output = proc.stdout + (("\n" + proc.stderr) if proc.stderr else "")
        if proc.returncode == 124:
            output += f"\n[command timed out after {timeout}s]"
        return proc.returncode, output

    def stop(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)
