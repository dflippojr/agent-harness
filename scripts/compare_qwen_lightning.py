"""Same-seed tower comparison of Qwen 50-step quality vs Lightning 4-step quality-fast.

Talks to a running local daemon (default http://127.0.0.1:8100). Submit a batch per mode so the first job is
cold (ComfyUI start + model load) and the rest are warm. Poll nvidia-smi for peak RAM/VRAM. Writes a JSON
summary and copies PNGs into --out.

    python scripts/compare_qwen_lightning.py --out docs/issue-13-outputs
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
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


def request(url: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:  # noqa: S310 - caller supplies the daemon URL
        return json.load(response)


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
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = request(f"{base}/images/{job_id}")
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(2)
    raise TimeoutError(f"image {job_id} did not finish")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://127.0.0.1:8100")
    ap.add_argument("--out", default="issue-13-lightning-outputs")
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()
    base = args.base_url.rstrip("/")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    listing = request(f"{base}/images?limit=1")
    modes = listing["status"]["modes"]
    if not modes.get("quality-fast", {}).get("available"):
        print(modes.get("quality-fast", {}).get("setup") or "quality-fast is unavailable", file=sys.stderr)
        return 2

    summary = {"seed": args.seed, "base_url": base, "modes": {}, "prompts": PROMPTS, "samples": []}
    for mode in MODES:
        submitted = []
        peak = nvidia()
        batch_started = time.time()
        for item in PROMPTS:
            job = request(f"{base}/images", "POST", {
                "prompt": item["prompt"], "model": mode, "aspect_ratio": item["aspect"],
                "resolution": "high", "seed": args.seed,
            })
            submitted.append((item, job))
        results = []
        for item, job in submitted:
            done = wait_job(base, job["id"])
            sample = nvidia()
            if sample.get("vram_used_mib", 0) > peak.get("vram_used_mib", 0):
                peak = sample
            row = {
                "id": item["id"], "mode": mode, "aspect": item["aspect"], "status": done["status"],
                "job_id": done["id"], "width": done.get("width"), "height": done.get("height"),
                "seconds": done.get("seconds"), "bytes": done.get("bytes"), "error": done.get("error", ""),
                "base_model": done.get("base_model", ""), "lora": done.get("lora", ""),
                "lora_revision": done.get("lora_revision", ""), "lora_sha256": done.get("lora_sha256", ""),
                "seed": done.get("seed"), "wall_seconds": round((done.get("finished_at") or 0) - done["created_at"], 1)
                if done.get("finished_at") else None,
            }
            results.append(row)
            if done["status"] == "done":
                dest = out / f"{item['id']}-{mode}.png"
                urllib.request.urlretrieve(f"{base}/images/{done['id']}.png", dest)  # noqa: S310
                row["file"] = str(dest)
        summary["modes"][mode] = {
            "jobs": results, "batch_seconds": round(time.time() - batch_started, 1),
            "cold_wall_seconds": results[0]["wall_seconds"] if results else None,
            "warm_seconds": [row["seconds"] for row in results[1:] if row["seconds"]],
            "peak_nvidia": peak,
        }
        print(f"{mode}: cold wall {summary['modes'][mode]['cold_wall_seconds']} s, "
              f"batch {summary['modes'][mode]['batch_seconds']} s, peak {peak}", flush=True)

    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    table = out / "summary.md"
    lines = ["# Qwen quality vs quality-fast", "",
             f"Seed `{args.seed}`. First job in each batch is cold (GPU hand-over + ComfyUI).", ""]
    lines += ["| Prompt | Aspect | quality-fast s | quality s | fast/quality |",
              "| --- | --- | --- | --- | --- |"]
    by_id = {mode: {row["id"]: row for row in summary["modes"][mode]["jobs"]} for mode in MODES}
    for item in PROMPTS:
        fast = by_id["quality-fast"][item["id"]]["seconds"]
        quality = by_id["quality"][item["id"]]["seconds"]
        ratio = round(fast / quality, 2) if fast and quality else ""
        lines.append(f"| {item['id']} | {item['aspect']} | {fast} | {quality} | {ratio} |")
    table.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out / 'summary.json'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as exc:
        print(f"daemon not reachable: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
