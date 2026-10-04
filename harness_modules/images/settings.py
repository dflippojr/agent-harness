"""The settings registry keys image generation owns (Module.settings). Present only while the module is."""

from __future__ import annotations

from pathlib import Path

from harness.modules import module_installed, setting_bool, setting_float, setting_int

KEY_IMAGES_EDIT_ENABLED = "images.edit_enabled"
IMAGES = "Images"


def check_images(cfg) -> list[str]:
    errors = []
    if not module_installed(cfg, "local_model"):
        errors.append("images requires the local_model module")
    if not cfg.images.comfy_dir or not Path(cfg.images.comfy_dir).exists():
        errors.append("images.comfy_dir is missing")
    return errors


def check_image_edit(cfg) -> list[str]:
    errors = check_images(cfg)
    if not module_installed(cfg, "image_edit"):
        errors.append("image_edit is not installed for this profile")
    from . import edit as image_edit
    status = image_edit.assets_status(cfg.images)
    if status["missing"] or status["hash_ok"] is False:
        errors.append(status["setup"])
    return errors


def _get_images_enabled(cfg):
    return cfg.images.enabled


def _set_images_enabled(cfg, value):
    cfg.images.enabled = bool(value)


def _get_edit_enabled(cfg):
    return cfg.images.edit_enabled


def _set_edit_enabled(cfg, value):
    cfg.images.edit_enabled = bool(value)


def _get_img_start(cfg):
    return cfg.images.start_timeout_seconds


def _set_img_start(cfg, value):
    cfg.images.start_timeout_seconds = float(value)


def _get_img_job(cfg):
    return cfg.images.job_timeout_seconds


def _set_img_job(cfg, value):
    cfg.images.job_timeout_seconds = float(value)


def _get_img_upload_bytes(cfg):
    return cfg.images.max_upload_bytes


def _set_img_upload_bytes(cfg, value):
    cfg.images.max_upload_bytes = int(value)


def _get_img_pixels(cfg):
    return cfg.images.max_pixels


def _set_img_pixels(cfg, value):
    cfg.images.max_pixels = int(value)


def specs() -> list:
    return [
        setting_float("images.start_timeout_seconds", "Image startup timeout (seconds)",
                      "How long to wait for ComfyUI to become ready.",
                      IMAGES, 180, _get_img_start, _set_img_start, 30, 600, ("images", "start_timeout_seconds"),
                      modules=("images",)),
        setting_float("images.job_timeout_seconds", "Image job timeout (seconds)",
                      "How long a single image job may run.",
                      IMAGES, 1200, _get_img_job, _set_img_job, 60, 7200, ("images", "job_timeout_seconds"),
                      modules=("images",)),
        setting_int("images.max_upload_bytes", "Image-edit upload byte limit",
                    "Maximum source or mask upload size for owner-only masked editing.",
                    IMAGES, 20 * 2**20, _get_img_upload_bytes, _set_img_upload_bytes,
                    2**20, 100 * 2**20, ("images", "max_upload_bytes"), modules=("image_edit",)),
        setting_int("images.max_pixels", "Image-edit decoded pixel limit",
                    "Reject gallery edits and decoded uploads/masks above this pixel count (long side is also "
                    "capped at 1664).",
                    IMAGES, 20_000_000, _get_img_pixels, _set_img_pixels,
                    1_000_000, 100_000_000, ("images", "max_pixels"), modules=("image_edit",)),
        setting_bool("images.enabled", "Image generation",
                     "Runtime enable for local image generation.",
                     "Features", False, _get_images_enabled, _set_images_enabled, ("images", "enabled"),
                     apply_mode="daemon_restart", modules=("images",), enable_check=check_images),
        setting_bool(KEY_IMAGES_EDIT_ENABLED, "Masked image editing",
                     "Runtime enable for the installed Qwen-Image-Edit component. Does not download model weights.",
                     "Features", False, _get_edit_enabled, _set_edit_enabled,
                     ("images", "edit_enabled"), apply_mode="daemon_restart", modules=("image_edit",),
                     enable_check=check_image_edit),
    ]
