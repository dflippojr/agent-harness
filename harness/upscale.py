"""Opt-in Real-ESRGAN 2×/4× upscaling for completed harness PNGs.

Weights are an optional image component (BSD-3-Clause, xinntao/Real-ESRGAN). Ordinary generation keeps working
when they are absent. Inference is tiled so a 16 GB GPU can upscale native Qwen/Z-Image sizes; outputs above
MAX_PIXELS are refused before allocation.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from .fileops import ToolError

CHOICES = ("none", "2x", "4x")
SCALES = {"2x": 2, "4x": 4}
TILE = 512          # matches ComfyUI ImageUpscaleWithModel's starting tile
OVERLAP = 32
MAX_PIXELS = 36_000_000  # ~6k×6k; 4× of native high 1:1 (5312² ≈ 28.2 MP) fits, a second 4× does not


@dataclass(frozen=True)
class ModelSpec:
    key: str
    filename: str
    scale: int
    version: str
    url: str
    sha256: str
    license: str = "BSD-3-Clause"


MODELS = {
    2: ModelSpec(
        key="RealESRGAN_x2plus",
        filename="RealESRGAN_x2plus.pth",
        scale=2,
        version="v0.2.1",
        url="https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth",
        sha256="49fafd45f8fd7aa8d31ab2a22d14d91b536c34494a5cfe31eb5d89c2fa266abb",
    ),
    4: ModelSpec(
        key="RealESRGAN_x4plus",
        filename="RealESRGAN_x4plus.pth",
        scale=4,
        version="v0.1.0",
        url="https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth",
        sha256="4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1",
    ),
}


@dataclass(frozen=True)
class Tile:
    x: int
    y: int
    w: int
    h: int


def parse_choice(value) -> str:
    choice = str(value or "none").strip().lower().replace("×", "x")
    if choice in ("", "0", "1x", "off", "false"):
        choice = "none"
    if choice not in CHOICES:
        raise ToolError(f"upscale must be one of {', '.join(CHOICES)}")
    return choice


def spec_for(scale: int) -> ModelSpec:
    if scale not in MODELS:
        raise ToolError("upscale scale must be 2 or 4")
    return MODELS[scale]


def models_dir(cfg) -> Path:
    if getattr(cfg, "upscale_dir", ""):
        return Path(cfg.upscale_dir)
    return Path(cfg.comfy_dir) / "ComfyUI" / "models" / "upscale_models"


def max_pixels(cfg) -> int:
    return int(getattr(cfg, "upscale_max_pixels", 0) or MAX_PIXELS)


def tile_size(cfg) -> int:
    return int(getattr(cfg, "upscale_tile", 0) or TILE)


def tile_overlap(cfg) -> int:
    return int(getattr(cfg, "upscale_overlap", 0) or OVERLAP)


def weight_path(cfg, scale: int) -> Path:
    return models_dir(cfg) / spec_for(scale).filename


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def missing_weights(cfg, *, verify_hash: bool = False) -> list[str]:
    """Human-readable problems for each scale. Empty means both general-image models are present."""
    problems = []
    dest = models_dir(cfg)
    for scale, spec in MODELS.items():
        path = dest / spec.filename
        if not path.is_file():
            problems.append(f"{spec.filename} not in {dest}")
            continue
        if verify_hash:
            digest = file_sha256(path)
            if digest != spec.sha256:
                problems.append(f"{spec.filename} sha256 {digest} != {spec.sha256}")
    return problems


def available_scales(cfg) -> list[int]:
    dest = models_dir(cfg)
    return [scale for scale, spec in MODELS.items() if (dest / spec.filename).is_file()]


def require_weights(cfg, scale: int) -> ModelSpec:
    spec = spec_for(scale)
    path = models_dir(cfg) / spec.filename
    if not path.is_file():
        raise ToolError(remediation(cfg))
    return spec


def remediation(cfg) -> str:
    dest = models_dir(cfg)
    lines = [
        "Real-ESRGAN upscaling is optional and not installed. Image generation still works without it.",
        f"To enable 2× and 4×, download the BSD-3-Clause general-image weights into {dest}:",
    ]
    for spec in MODELS.values():
        lines.append(f"  {spec.filename}  {spec.url}  sha256:{spec.sha256}")
    return " ".join(lines)


def check_dimensions(width: int, height: int, scale: int, max_px: int = MAX_PIXELS) -> tuple[int, int]:
    if width < 1 or height < 1:
        raise ToolError("image has no pixels")
    if scale not in MODELS:
        raise ToolError("upscale scale must be 2 or 4")
    out_w, out_h = width * scale, height * scale
    pixels = out_w * out_h
    if pixels > max_px:
        raise ToolError(
            f"upscaling {width}×{height} by {scale}× would produce {out_w}×{out_h} ({pixels:,} pixels), "
            f"above the {max_px:,} pixel cap that keeps a 16 GB GPU from host OOM. Use a smaller source."
        )
    return out_w, out_h


def plan_tiles(width: int, height: int, tile: int = TILE, overlap: int = OVERLAP) -> list[Tile]:
    """Cover an image with overlapping tiles no larger than `tile` (ComfyUI ImageUpscaleWithModel defaults)."""
    if width < 1 or height < 1:
        raise ToolError("image has no pixels")
    tile = max(int(tile), 1)
    overlap = min(max(int(overlap), 0), tile - 1)
    step = max(tile - overlap, 1)
    tiles: list[Tile] = []
    seen: set[tuple[int, int, int, int]] = set()
    y = 0
    while y < height:
        x = 0
        th = min(tile, height - y)
        if y > 0 and y + tile >= height:
            y = max(0, height - tile)
            th = height - y
        while x < width:
            tw = min(tile, width - x)
            if x > 0 and x + tile >= width:
                x = max(0, width - tile)
                tw = width - x
            key = (x, y, tw, th)
            if key not in seen:
                seen.add(key)
                tiles.append(Tile(*key))
            if x + tw >= width:
                break
            x += step
        if y + th >= height:
            break
        y += step
    return tiles


def blend_weight(pos: int, size: int, overlap: int) -> float:
    """Linear fade across the overlap on each edge so stitched tiles don't seam."""
    if size <= 0:
        return 0.0
    if overlap <= 0 or size <= overlap * 2:
        return 1.0
    if pos < overlap:
        return (pos + 1) / overlap
    if pos >= size - overlap:
        return (size - pos) / overlap
    return 1.0


def stitch_tiles(tiles: list[tuple[Tile, list[list[tuple[int, int, int]]]]],
                 out_w: int, out_h: int, scale: int, overlap: int) -> list[list[tuple[int, int, int]]]:
    """Blend overlapping RGB tiles (row-major, already scaled) onto a blank canvas."""
    acc = [[(0.0, 0.0, 0.0, 0.0) for _ in range(out_w)] for _ in range(out_h)]
    scaled_overlap = overlap * scale
    for tile, pixels in tiles:
        ox, oy = tile.x * scale, tile.y * scale
        tw, th = tile.w * scale, tile.h * scale
        for j in range(th):
            wy = blend_weight(j, th, scaled_overlap)
            for i in range(tw):
                wx = blend_weight(i, tw, scaled_overlap)
                weight = wx * wy
                r, g, b = pixels[j][i]
                ar, ag, ab, aw = acc[oy + j][ox + i]
                acc[oy + j][ox + i] = (ar + r * weight, ag + g * weight, ab + b * weight, aw + weight)
    out = []
    for row in acc:
        out.append([
            (int(round(r / w)), int(round(g / w)), int(round(b / w))) if w else (0, 0, 0)
            for r, g, b, w in row
        ])
    return out


def nearest_scale(pixels: list[list[tuple[int, int, int]]], scale: int) -> list[list[tuple[int, int, int]]]:
    """Deterministic stand-in for a 2×/4× model, used to test tiling without GPU weights."""
    out = []
    for row in pixels:
        scaled = [px for px in row for _ in range(scale)]
        for _ in range(scale):
            out.append(list(scaled))
    return out


def tiled_scale(pixels: list[list[tuple[int, int, int]]], scale: int, tile: int = TILE,
                overlap: int = OVERLAP) -> list[list[tuple[int, int, int]]]:
    height, width = len(pixels), len(pixels[0]) if pixels else 0
    check_dimensions(width, height, scale)
    planned = plan_tiles(width, height, tile, overlap)
    scaled_tiles = []
    for t in planned:
        crop = [row[t.x:t.x + t.w] for row in pixels[t.y:t.y + t.h]]
        scaled_tiles.append((t, nearest_scale(crop, scale)))
    return stitch_tiles(scaled_tiles, width * scale, height * scale, scale, overlap)


def workflow(image_name: str, model_filename: str, width: int, height: int, prefix: str) -> dict:
    """ComfyUI graph: LoadImage → Real-ESRGAN (tiled in-node) → exact size → PNG.

    ImageUpscaleWithModel starts at a 512 px tile with 32 px overlap and halves the tile on CUDA OOM.
    """
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": model_filename}},
        "3": {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}},
        "4": {"class_type": "ImageScale", "inputs": {
            "image": ["3", 0], "upscale_method": "lanczos", "width": width, "height": height, "crop": "disabled"}},
        "5": {"class_type": "SaveImage", "inputs": {"images": ["4", 0], "filename_prefix": prefix}},
    }


def preserve_alpha(source_png: bytes, result_png: bytes) -> bytes:
    """Reattach a resized source alpha channel. RGB results pass through. Invalid PNGs are left unchanged."""
    try:
        from PIL import Image
    except ImportError:
        return result_png
    try:
        src = Image.open(io.BytesIO(source_png))
        out = Image.open(io.BytesIO(result_png))
    except Exception:  # noqa: BLE001 - keep the ComfyUI bytes if either file isn't a real PNG
        return result_png
    if "A" not in src.getbands():
        buf = io.BytesIO()
        out.save(buf, format="PNG")
        return buf.getvalue()
    alpha = src.getchannel("A").resize(out.size, Image.Resampling.LANCZOS)
    merged = out.convert("RGBA")
    merged.putalpha(alpha)
    buf = io.BytesIO()
    merged.save(buf, format="PNG")
    return buf.getvalue()


def png_size(data: bytes) -> tuple[int, int] | None:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:  # noqa: BLE001
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR" and len(data) >= 24:
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        return None


def status(cfg) -> dict:
    dest = models_dir(cfg)
    missing = missing_weights(cfg)
    installed = []
    for scale, spec in MODELS.items():
        path = dest / spec.filename
        installed.append({
            "scale": f"{scale}x",
            "model": spec.key,
            "version": spec.version,
            "license": spec.license,
            "filename": spec.filename,
            "url": spec.url,
            "sha256": spec.sha256,
            "installed": path.is_file(),
        })
    return {
        "available": not missing,
        "choices": list(CHOICES),
        "default": "none",
        "max_pixels": max_pixels(cfg),
        "tile": tile_size(cfg),
        "overlap": tile_overlap(cfg),
        "dir": str(dest),
        "models": installed,
        "missing": missing,
        "remediation": remediation(cfg) if missing else "",
    }
