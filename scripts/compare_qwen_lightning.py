"""Same-seed tower comparison of Qwen 50-step quality vs Lightning 4-step quality-fast.

Talks to a running local daemon (default http://127.0.0.1:8100). Submit a batch per mode so the first job is
cold (ComfyUI start + model load) and the rest are warm. Poll nvidia-smi for peak RAM/VRAM. Writes a JSON
summary and copies PNGs into --out.

    python scripts/compare_qwen_lightning.py --out docs/issue-13-outputs
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROMPTS = [
    {"id": "people-portrait", "aspect": "3:4", "prompt":
     "Photorealistic portrait of a red-haired woman in a wool coat, window light, sharp eyes, natural skin texture"},
    {"id": "people-hikers", "aspect": "16:9", "prompt":
     "Two hikers on a mountain ridge at sunrise, backpacks, long shadows, photorealistic landscape"},
    {"id": "landscape", "aspect": "16:9", "prompt":
     "Misty pine forest around a still lake, distant mountains, golden hour, fine natural detail"},
    {"id": "illustration", "aspect": "1:1", "prompt":
     "Flat vector mascot of a friendly robot holding a wrench, simple shapes, bold colors, clean edges"},
    {"id": "small-text", "aspect": "4:3", "prompt":
     "A coffee shop chalkboard menu that clearly reads OPEN 7AM, handwritten white lettering, wooden frame"},
    {"id": "dense-text", "aspect": "3:4", "prompt":
     'Paperback book cover titled "AGENT HARNESS" with subtitle "Local agents, private GPU", serif type, night-blue'},
    {"id": "awkward-9-16", "aspect": "9:16", "prompt":
     "A tall neon alley at night with a vertical shop sign that says NIGHT MARKET, wet pavement reflections"},
    {"id": "awkward-2-3", "aspect": "2:3", "prompt":
     "A poster of a bicycle on a cream background with the words RIDE TODAY in large clean sans-serif type"},
    {"id": "interior-lab", "aspect": "16:9", "prompt":
     "Cluttered home-lab desk, labeled cables, a monitor that shows READY, warm lamp, photorealistic"},
    {"id": "animal-detail", "aspect": "3:2", "prompt":
     "A wet otter on a river rock, water droplets, sharp fur, shallow depth of field, natural light"},
]
SEED = 2512
MODES = ("quality-fast", "quality")
JOB_ID_RE = re.compile(r"^[0-9a-fA-F]{8,32}$")
PROMPT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MODE_RE = re.compile(r"^(quality|quality-fast)$")
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


def confine_out_dir(raw: str, root: Path | None = None) -> Path:
    """Resolve --out under root (cwd by default). Absolute paths and .. are rejected."""
    base = (root or Path.cwd()).resolve()
    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        raise ValueError("--out is empty")
    rel = Path(text)
    if rel.is_absolute() or bool(rel.anchor) or ".." in rel.parts:
        raise ValueError(f"--out must be a relative path under {base}")
    target = (base / rel).resolve()
    if target != base and not target.is_relative_to(base):
        raise ValueError(f"--out escapes {base}: {raw}")
    return target


def confine_output_file(directory: Path, name: str) -> Path:
    directory = directory.resolve()
    if "/" in name or "\\" in name or name in {".", ".."} or ".." in Path(name).parts:
        raise ValueError(f"refusing output name: {name}")
    target = (directory / name).resolve()
    if not target.is_relative_to(directory):
        raise ValueError(f"output path escapes {directory}: {name}")
    return target


def daemon_base(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    if parsed.scheme != "http" or parsed.hostname not in LOCAL_HOSTS:
        raise ValueError("--base-url must be http://127.0.0.1 or http://localhost")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("--base-url must not include a path, query, userinfo, or fragment")
    host = parsed.hostname
    port = parsed.port or 8100
    if host is None:
        raise ValueError("--base-url host is missing")
    return f"http://{host}:{port}"


def sanitize_job_id(value: object) -> str:
    text = str(value or "")
    if not JOB_ID_RE.fullmatch(text):
        raise ValueError(f"invalid image job id: {value!r}")
    return text


def sanitize_prompt_id(value: object) -> str:
    text = str(value or "")
    if not PROMPT_ID_RE.fullmatch(text):
        raise ValueError(f"invalid prompt id: {value!r}")
    return text


def sanitize_mode(value: object) -> str:
    text = str(value or "")
    if not MODE_RE.fullmatch(text):
        raise ValueError(f"invalid mode: {value!r}")
    return text


def daemon_url(base: str, *segments: str) -> str:
    parsed = urllib.parse.urlsplit(daemon_base(base))
    parts = [urllib.parse.quote(segment, safe=".-") for segment in segments]
    path = "/" + "/".join(parts)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def request(url: str, method: str = "GET", body: dict | None = None) -> dict:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in LOCAL_HOSTS:
        raise ValueError(f"refusing non-local URL: {url}")
    if ".." in parsed.path.split("/"):
        raise ValueError(f"refusing URL path: {url}")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def download_png(url: str, dest: Path) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in LOCAL_HOSTS or not parsed.path.endswith(".png"):
        raise ValueError(f"refusing image URL: {url}")
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = response.read()
    dest.write_bytes(payload)


def nvidia() -> dict:
    try:
        raw = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,memory.reserved", "--format=csv,noheader,nounits"],
            text=True, timeout=5)
        used, total, reserved = [int(part.strip()) for part in raw.splitlines()[0].split(",")]
        return {"vram_used_mib": used, "vram_total_mib": total, "vram_reserved_mib": reserved}
    except (OSError, subprocess.CalledProcessError, ValueError):
        return {}


def wait_job(base: str, job_id: str, timeout: float = 1800) -> dict:
    job_id = sanitize_job_id(job_id)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = request(daemon_url(base, "images", job_id))
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(2)
    raise TimeoutError(f"image {job_id} did not finish")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://127.0.0.1:8100")
    ap.add_argument("--out", default="issue-13-lightning-outputs")
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args(argv)
    try:
        base = daemon_base(args.base_url)
        out = confine_out_dir(args.out)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)

    listing_url = urllib.parse.urlunsplit((*urllib.parse.urlsplit(daemon_url(base, "images"))[:3], "limit=1", ""))
    listing = request(listing_url)
    modes = listing["status"]["modes"]
    if not modes.get("quality-fast", {}).get("available"):
        print(modes.get("quality-fast", {}).get("setup") or "quality-fast is unavailable", file=sys.stderr)
        return 2

    summary = {"seed": args.seed, "base_url": base, "modes": {}, "prompts": PROMPTS, "samples": []}
    for mode in MODES:
        mode = sanitize_mode(mode)
        submitted = []
        peak = nvidia()
        batch_started = time.time()
        for item in PROMPTS:
            job = request(daemon_url(base, "images"), "POST", {
                "prompt": item["prompt"], "model": mode, "aspect_ratio": item["aspect"],
                "resolution": "high", "seed": args.seed,
            })
            submitted.append((item, sanitize_job_id(job["id"])))
        results = []
        for item, job_id in submitted:
            done = wait_job(base, job_id)
            sample = nvidia()
            if sample.get("vram_used_mib", 0) > peak.get("vram_used_mib", 0):
                peak = sample
            done_id = sanitize_job_id(done["id"])
            prompt_id = sanitize_prompt_id(item["id"])
            row = {
                "id": prompt_id, "mode": mode, "aspect": item["aspect"], "status": done["status"],
                "job_id": done_id, "width": done.get("width"), "height": done.get("height"),
                "seconds": done.get("seconds"), "bytes": done.get("bytes"), "error": done.get("error", ""),
                "base_model": done.get("base_model", ""), "lora": done.get("lora", ""),
                "lora_revision": done.get("lora_revision", ""), "lora_sha256": done.get("lora_sha256", ""),
                "seed": done.get("seed"), "wall_seconds": round((done.get("finished_at") or 0) - done["created_at"], 1)
                if done.get("finished_at") else None,
            }
            results.append(row)
            if done["status"] == "done":
                dest = confine_output_file(out, f"{prompt_id}-{mode}.png")
                download_png(daemon_url(base, "images", f"{done_id}.png"), dest)
                row["file"] = str(dest)
        summary["modes"][mode] = {
            "jobs": results, "batch_seconds": round(time.time() - batch_started, 1),
            "cold_wall_seconds": results[0]["wall_seconds"] if results else None,
            "warm_seconds": [row["seconds"] for row in results[1:] if row["seconds"]],
            "peak_nvidia": peak,
        }
        print(f"{mode}: cold wall {summary['modes'][mode]['cold_wall_seconds']} s, "
              f"batch {summary['modes'][mode]['batch_seconds']} s, peak {peak}", flush=True)

    summary_json = confine_output_file(out, "summary.json")
    summary_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    table = confine_output_file(out, "summary.md")
    lines = ["# Qwen quality vs quality-fast", "",
             f"Seed `{args.seed}`. First job in each batch is cold (GPU hand-over + ComfyUI).", ""]
    lines += ["| Prompt | Aspect | quality-fast s | quality s | fast/quality |",
              "| --- | --- | --- | --- | --- |"]
    by_id = {mode: {row["id"]: row for row in summary["modes"][mode]["jobs"]} for mode in MODES}
    for item in PROMPTS:
        prompt_id = sanitize_prompt_id(item["id"])
        fast = by_id["quality-fast"][prompt_id]["seconds"]
        quality = by_id["quality"][prompt_id]["seconds"]
        ratio = round(fast / quality, 2) if fast and quality else ""
        lines.append(f"| {prompt_id} | {item['aspect']} | {fast} | {quality} | {ratio} |")
    table.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {summary_json}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as exc:
        print(f"daemon not reachable: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
