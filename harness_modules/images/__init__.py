"""Local image generation with ComfyUI, as an add-on module (#334; docs/modules.md, docs/images.md).

Everything images contributes to the daemon is registered here, in ``MODULE``. This file stays light: the CLI
imports it to list commands, so routes, settings and the service load only when the core asks for them.
"""

from __future__ import annotations

from harness.modules import Module, ToolGate


def _runtime(manager, module):
    from .runtime import ImagesRuntime
    return ImagesRuntime(manager, module)


def _owner_routes():
    from .routes import owner_routes
    return owner_routes


def _app_routes():
    from .routes import app_routes
    return app_routes


def _settings():
    from .settings import specs
    return specs()


def _doctor(report, cfg) -> None:
    from .doctor import run
    run(report, cfg)


def _runtime_enabled(cfg, switch: str) -> bool:
    """``images.enabled`` switches generation; ``images.edit_enabled`` the optional masked editing."""
    images = cfg.images
    return bool(images.edit_enabled if switch == "image_edit" else images.enabled)


def _principal_capabilities(owner: bool, scopes: frozenset) -> dict:
    return {"images": owner or "images" in scopes}


MODULE = Module(
    name="images",
    switches=("images", "image_edit"),
    title="Image generation",
    docs=("docs/modules.md", "docs/flux-fast.md", "docs/resource-guard.md"),
    runtime_enabled=_runtime_enabled,
    runtime=_runtime,
    owner_routes=_owner_routes,
    admin_paths=frozenset({
        "/images",
        "/images/uploads",
        "/images/warmup",
        "/images/cooldown",
        "/images/{iid}",
        "/images/{iid}/edit",
        "/images/{iid}/cancel",
        "/images/{iid}/upscale",
        "/maintenance/image-archive/retention/preview",
        "/maintenance/image-archive/retention/apply",
    }),
    app_routes=_app_routes,
    app_scopes={"images": "generate images, upscale them, and read them"},
    app_capabilities={"images": "images"},
    tools=ToolGate(project_flag="images", capability="images", members=False, mcp=True, workspace=True,
                   mutating=("generate_image",), span="image_job"),
    tool_names=("generate_image",),
    settings=_settings,
    cli=(
        ("maintenance image-retention-preview", "POST", "/maintenance/image-archive/retention/preview",
         "show what image-archive retention would remove", ()),
        ("maintenance image-retention-apply", "POST", "/maintenance/image-archive/retention/apply",
         "apply image-archive retention", ("confirmation",)),
        ("images list", "GET", "/images", "list generated images", ("--limit:int",)),
        ("images show", "GET", "/images/{iid}", "show an image job", ()),
        ("images create", "POST", "/images", "generate an image",
         ("prompt", "--model", "--aspect_ratio", "--resolution", "--seed:int", "--upscale")),
        ("images upload", "POST", "/images/uploads", "upload a source image to edit", ("file:file",)),
        ("images edit", "POST", "/images/{iid}/edit", "repaint an image inside a mask",
         ("prompt", "mask:file", "--feather:int", "--seed:int")),
        ("images upscale", "POST", "/images/{iid}/upscale", "upscale an image", ("--upscale",)),
        ("images cancel", "POST", "/images/{iid}/cancel", "cancel an image job", ()),
        ("images delete", "DELETE", "/images/{iid}", "delete an image", ()),
        ("images warmup", "POST", "/images/warmup", "load the image model", ()),
        ("images cooldown", "POST", "/images/cooldown", "unload the image model", ()),
    ),
    cli_groups={"images": "image generation"},
    principal_capabilities=_principal_capabilities,
    doctor=_doctor,
)
