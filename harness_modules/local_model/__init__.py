"""Optional llama-server supervision, warm-up, GPU holds and RAM admission guard."""
from harness.modules import Module

def _runtime(manager, module):
    from .runtime import LocalModelRuntime
    return LocalModelRuntime(manager, module)

def _owner_routes():
    from .routes import owner_routes
    return owner_routes

def _app_routes():
    from .routes import app_routes
    return app_routes

def _settings():
    from .settings import specs
    return specs()

def _doctor(report, cfg):
    from .doctor import run
    run(report, cfg)

MODULE = Module(
    name="local_model", switches=("local_model", "gpu_guard"), title="Local model supervision",
    runtime=_runtime, runtime_enabled=lambda cfg, switch: switch == "local_model" or cfg.gpu_guard.enabled,
    owner_routes=_owner_routes, app_routes=_app_routes, settings=_settings, doctor=_doctor,
    app_scopes={"models:warm": "start loading the local model ahead of a chat (refused while the GPU or RAM guard says no)"},
    admin_paths=frozenset({"/models/status", "/models/warm", "/gpu", "/gpu/{action}",
                           "/resources", "/resources/diagnostics", "/resources/{action}"}),
    member_forbidden=((("/gpu", "/resources"), "members cannot change GPU or machine settings"),),
    cli=(
    ("models status", "GET", "/models/status", "show the local model's state", ()),
    ("models warm", "POST", "/models/warm", "load the local model ahead of use", ()),
    ("gpu status", "GET", "/gpu", "show the GPU hold", ()),
    ("gpu pause", "POST", "/gpu/pause", "hold the GPU (local models unload)", ("--duration_seconds:int",)),
    ("gpu resume", "POST", "/gpu/resume", "release the GPU hold", ()),
    ("resources status", "GET", "/resources", "show the resource guard and GPU hold", ()),
    ("resources diagnostics", "GET", "/resources/diagnostics", "show memory, VRAM and load details", ()),
    ("resources load", "POST", "/resources/load", "load the local model now",
     ("--duration_seconds:int", "--force:flag")),
    ("resources unload", "POST", "/resources/unload", "unload the local model", ()),
    ("resources pause", "POST", "/resources/pause", "hold the GPU", ("--duration_seconds:int",)),
    ("resources resume", "POST", "/resources/resume", "release the GPU hold", ()),
    ),
    cli_groups={"gpu": "GPU hold", "resources": "resource guard and local model"},
)
