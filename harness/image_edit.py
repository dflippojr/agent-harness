"""Optional masked inpainting / photo editing with Qwen-Image-Edit (issue #88).

The official Apache-2.0 Qwen-Image-Edit weights are packaged for ComfyUI. This module never downloads them; the
installer only fetches the pinned artifact when the owner opts into the `image_edit` component. Text-to-image stays
usable when these files are absent.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

from .config import ImagesConfig
from .fileops import ToolError

EDIT_MODEL_ID = "edit"
OPERATION_GENERATE = "generate"
OPERATION_UPLOAD = "upload"
OPERATION_EDIT = "edit"
PUBLIC_OPERATIONS = {OPERATION_GENERATE}

# Official Apache-2.0 Qwen-Image-Edit, ComfyUI fp8 packaging for a 16 GB card (same RAM-streaming pattern as quality).
EDIT_MODEL = {
    "id": EDIT_MODEL_ID,
    "label": "Qwen-Image-Edit (Apache 2.0)",
    "license": "Apache-2.0",
    "source_repo": "Qwen/Qwen-Image-Edit",
    "packaged_repo": "Comfy-Org/Qwen-Image-Edit_ComfyUI",
    "revision": "7d41107b653d3039be20972fb82398b01b3213eb",
    "unet": {
        "name": "qwen_image_edit_fp8_e4m3fn.safetensors",
        "folders": ("diffusion_models",),
        "hf_path": "split_files/diffusion_models/qwen_image_edit_fp8_e4m3fn.safetensors",
        "sha256": "393c6743d1de2e9031b5197027b36116f2096958ccc0223526d34e1860266021",
        "bytes": 20430635136,
    },
    "clip": {
        "name": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
        "folders": ("text_encoders", "clip"),
    },
    "vae": {
        "name": "qwen_image_vae.safetensors",
        "folders": ("vae",),
    },
}
SETUP_GUIDANCE = (
    "Masked editing needs the optional image-edit component: Qwen-Image-Edit (Apache 2.0), ComfyUI fp8 "
    f"revision {EDIT_MODEL['revision'][:12]}, sha256 {EDIT_MODEL['unet']['sha256'][:12]}…. "
    "Re-run the installer with -EnableModules image_edit (or --enable-modules image_edit) after confirming "
    "about 22 GB free disk, 16 GB GPU, and 32 GB RAM. Existing fast/quality text-to-image still works without it."
)
MAX_UPLOAD_BYTES = 20 * 2**20
MAX_DECODE_PIXELS = 20_000_000
MAX_EDIT_SIDE = 1664
VAE_MULTIPLE = 16
MIN_SIDE = 64
MAX_FEATHER = 32
ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP"}

QWEN_EDIT_NEGATIVE = " "


def models_dir(cfg: ImagesConfig) -> Path:
    return Path(cfg.models_dir or (Path(cfg.comfy_dir) / "models"))


def find_asset(root: Path, spec: dict) -> Path | None:
    name = spec["name"]
    for folder in spec["folders"]:
        path = root / folder / name
        if path.is_file():
            return path
    direct = root / name
    return direct if direct.is_file() else None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def locate_assets(cfg: ImagesConfig) -> dict[str, Path | None]:
    root = models_dir(cfg)
    return {key: find_asset(root, EDIT_MODEL[key]) for key in ("unet", "clip", "vae")}


def unet_hash_ok(path: Path | None) -> bool | None:
    """True/False when the real artifact is present; None for a missing or test stub file."""
    if path is None or not path.is_file():
        return None
    size = path.stat().st_size
    expected = EDIT_MODEL["unet"]["bytes"]
    if size == expected:
        return file_sha256(path) == EDIT_MODEL["unet"]["sha256"]
    if size < 1_000_000:
        return None
    return False


def assets_status(cfg: ImagesConfig) -> dict:
    files = locate_assets(cfg)
    missing = [key for key, path in files.items() if path is None]
    hash_ok = unet_hash_ok(files.get("unet"))
    available = not missing and hash_ok is not False
    return {
        "available": available,
        "enabled": bool(getattr(cfg, "edit_enabled", False)),
        "missing": missing,
        "hash_ok": hash_ok,
        "model": EDIT_MODEL["source_repo"],
        "label": EDIT_MODEL["label"],
        "license": EDIT_MODEL["license"],
        "revision": EDIT_MODEL["revision"],
        "sha256": EDIT_MODEL["unet"]["sha256"],
        "bytes": EDIT_MODEL["unet"]["bytes"],
        "setup": "" if available else SETUP_GUIDANCE,
    }


def public_status(cfg: ImagesConfig) -> dict:
    """Owner/UI payload: no filesystem paths."""
    raw = assets_status(cfg)
    return {key: raw[key] for key in ("available", "enabled", "model", "label", "license", "revision", "sha256",
                                      "bytes", "setup")}


def is_private(job: dict | None) -> bool:
    return ((job or {}).get("operation") or OPERATION_GENERATE) not in PUBLIC_OPERATIONS


def _open_image(data: bytes, max_pixels: int):
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not data:
        raise ToolError("image is empty")
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Image.DecompressionBombError as exc:
        raise ToolError("image exceeds the pixel cap") from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise ToolError("malformed image") from exc
    fmt = (image.format or "").upper()
    if fmt == "JPG":
        fmt = "JPEG"
    if fmt not in ALLOWED_FORMATS:
        raise ToolError("unsupported image format; use PNG, JPEG, or WebP")
    return ImageOps.exif_transpose(image) or image


def _to_rgb(image) -> object:
    from PIL import Image

    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return image.convert("RGB")


def _png_bytes(image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def normalize_source(data: bytes, *, max_bytes: int = MAX_UPLOAD_BYTES,
                     max_pixels: int = MAX_DECODE_PIXELS) -> tuple[bytes, int, int]:
    """Orientation/color-normalized RGB PNG with metadata stripped. Rejects bombs and unsupported formats."""
    from PIL import Image

    if len(data) > max_bytes:
        raise ToolError(f"image is too large ({len(data)} bytes; max {max_bytes})")
    image = _to_rgb(_open_image(data, max_pixels))
    width, height = image.size
    if width * height > max_pixels or width > 8000 or height > 8000:
        raise ToolError("image exceeds the pixel cap")
    long_side = max(width, height)
    if long_side > MAX_EDIT_SIDE:
        scale = MAX_EDIT_SIDE / long_side
        width, height = max(MIN_SIDE, int(width * scale)), max(MIN_SIDE, int(height * scale))
        image = image.resize((width, height), resample=Image.Resampling.LANCZOS)
    width, height = width - width % VAE_MULTIPLE, height - height % VAE_MULTIPLE
    if width < MIN_SIDE or height < MIN_SIDE:
        raise ToolError("image is too small after normalization")
    image = image.crop((0, 0, width, height))
    return _png_bytes(image), width, height


def normalize_mask(data: bytes, width: int, height: int, *,
                   max_bytes: int = MAX_UPLOAD_BYTES, max_pixels: int = MAX_DECODE_PIXELS) -> bytes:
    """Store a white=editable / black=preserved mask at the source's exact pixel size."""
    if len(data) > max_bytes:
        raise ToolError(f"mask is too large ({len(data)} bytes; max {max_bytes})")
    image = _open_image(data, max_pixels).convert("L")
    if image.size != (width, height):
        raise ToolError(f"mask size {image.size[0]}×{image.size[1]} does not match source {width}×{height}")
    extrema = image.getextrema()
    if not extrema or extrema[1] == 0:
        raise ToolError("mask is empty; paint the region to edit in white")
    return _png_bytes(image)


def apply_feather(mask_png: bytes, feather: int) -> bytes:
    from PIL import ImageFilter

    if feather <= 0:
        return mask_png
    image = _open_image(mask_png, MAX_DECODE_PIXELS).convert("L")
    return _png_bytes(image.filter(ImageFilter.GaussianBlur(radius=float(feather))))


def parse_feather(value) -> int:
    try:
        feather = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise ToolError("feather must be an integer") from exc
    if not 0 <= feather <= MAX_FEATHER:
        raise ToolError(f"feather must be between 0 and {MAX_FEATHER}")
    return feather


def edit_workflow(prompt: str, source_name: str, mask_name: str, seed: int, prefix: str) -> dict:
    """ComfyUI API graph: official Qwen-Image-Edit encode + latent mask + composite to preserve black pixels."""
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": EDIT_MODEL["unet"]["name"], "weight_dtype": "default"}},
        "2": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3}},
        "3": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": EDIT_MODEL["clip"]["name"], "type": "qwen_image", "device": "default"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": EDIT_MODEL["vae"]["name"]}},
        "5": {"class_type": "LoadImage", "inputs": {"image": source_name}},
        "6": {"class_type": "LoadImage", "inputs": {"image": mask_name}},
        "7": {"class_type": "ImageToMask", "inputs": {"image": ["6", 0], "channel": "red"}},
        "8": {"class_type": "TextEncodeQwenImageEdit",
              "inputs": {"clip": ["3", 0], "prompt": prompt, "vae": ["4", 0], "image": ["5", 0]}},
        "9": {"class_type": "TextEncodeQwenImageEdit",
              "inputs": {"clip": ["3", 0], "prompt": QWEN_EDIT_NEGATIVE, "vae": ["4", 0], "image": ["5", 0]}},
        "10": {"class_type": "VAEEncode", "inputs": {"pixels": ["5", 0], "vae": ["4", 0]}},
        "11": {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["10", 0], "mask": ["7", 0]}},
        "12": {"class_type": "KSampler",
               "inputs": {"model": ["2", 0], "positive": ["8", 0], "negative": ["9", 0],
                          "latent_image": ["11", 0], "seed": seed, "steps": 50, "cfg": 4,
                          "sampler_name": "euler", "scheduler": "simple", "denoise": 1}},
        "13": {"class_type": "VAEDecode", "inputs": {"samples": ["12", 0], "vae": ["4", 0]}},
        "14": {"class_type": "ImageCompositeMasked",
               "inputs": {"destination": ["5", 0], "source": ["13", 0], "mask": ["7", 0],
                          "x": 0, "y": 0, "resize_source": False}},
        "15": {"class_type": "SaveImage", "inputs": {"images": ["14", 0], "filename_prefix": prefix}},
    }


def artifact_url() -> str:
    spec = EDIT_MODEL["unet"]
    return (f"https://huggingface.co/{EDIT_MODEL['packaged_repo']}/resolve/{EDIT_MODEL['revision']}/"
            f"{spec['hf_path']}")
