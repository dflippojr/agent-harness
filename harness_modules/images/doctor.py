"""`python -m harness.doctor` checks for image generation, run when images is enabled (Module.doctor)."""

from __future__ import annotations

import shutil
from pathlib import Path

from harness.modules import module_effective

from . import edit as image_edit
from . import upscale as upscale_mod
from .models import doctor_warning, inspect_flux_fast
from .service import LIGHTNING_LORA, lightning_lora_status

IMAGE_EDITING = "Image editing"


def run(r, cfg) -> None:
    if cfg.images.enabled:
        check_images(r, cfg)


def _check_image_models(r, cfg) -> None:
    py = Path(cfg.images.comfy_dir) / "python_embeded" / "python.exe"
    (r.ok if py.exists() else r.fail)("Image generation", f"ComfyUI at {cfg.images.comfy_dir}"
                                      + ("" if py.exists() else " not found"))
    lora = lightning_lora_status(cfg.images)
    if lora["available"]:
        r.ok("Qwen quality-fast LoRA",
             f"{lora['path']} ({LIGHTNING_LORA['filename']}, {LIGHTNING_LORA['bytes']} bytes, "
             f"revision {LIGHTNING_LORA['revision']})")
    else:
        r.warn("Qwen quality-fast LoRA", lora["setup"])
    flux_warning = doctor_warning(inspect_flux_fast(cfg.images))
    if flux_warning:
        r.warn("FLUX.2 klein 4B (optional)", flux_warning)
    else:
        r.ok("FLUX.2 klein 4B (optional)", "flux-fast assets and nodes are ready")


def _check_image_edit_ram(r) -> None:
    try:
        import psutil
        ram_gb = psutil.virtual_memory().total / 2**30
        (r.ok if ram_gb >= 30 else r.warn)(
            "Image editing RAM", f"{ram_gb:.0f} GB (Qwen-Image-Edit fp8 was tested with 32 GB)")
    except (ImportError, OSError):
        r.warn("Image editing RAM", "could not read installed RAM")


def _check_image_edit_disk(r, cfg, edit: dict) -> None:
    models = image_edit.models_dir(cfg.images)
    try:
        free = shutil.disk_usage(models if models.exists() else cfg.data_dir).free / 2**30
        need = 22 if not edit["available"] else 1
        (r.ok if free >= need else r.fail)(
            "Image editing disk", f"{free:.0f} GB free at {models} (need about {need} GB)")
    except OSError as e:
        r.warn("Image editing disk", str(e))


def _check_image_editing(r, cfg) -> None:
    edit = image_edit.assets_status(cfg.images, verify_hash=True)
    if not module_effective(cfg, "image_edit"):
        if bool(getattr(cfg.installed, "image_edit", False)):
            r.ok(IMAGE_EDITING, "installed but disabled; text-to-image is unchanged")
        else:
            r.ok(IMAGE_EDITING, "optional component not installed; text-to-image is unchanged")
        return
    if edit["available"] and edit["hash_ok"] is not False:
        extra = "checksum verified" if edit["hash_ok"] else "stub or unpackaged file present"
        r.ok(IMAGE_EDITING, f"{edit['model']} {edit['revision'][:12]} ({extra})")
    else:
        missing = ", ".join(edit["missing"]) or "checksum mismatch"
        r.fail(IMAGE_EDITING, f"image_edit is enabled but assets are not ready ({missing}). {edit['setup']}")
    _check_image_edit_ram(r)
    _check_image_edit_disk(r, cfg, edit)


def check_images(r, cfg) -> None:
    _check_image_models(r, cfg)
    _check_image_editing(r, cfg)
    if upscale_mod.missing_weights(cfg.images, verify_hash=True):
        r.warn("Image upscaling", upscale_mod.remediation(cfg.images))
    else:
        r.ok("Image upscaling", f"Real-ESRGAN x2plus/x4plus in {upscale_mod.models_dir(cfg.images)}")
