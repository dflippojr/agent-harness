"""Read-only daemon inventory for the standalone Hub app (#520)."""
from harness.modules import Module


def _runtime(manager, module):
    from .runtime import HubRuntime
    return HubRuntime(manager, module)


def _register(app, mgr, require_admin):
    from .routes import register_admin
    return register_admin(app, mgr, require_admin)


def _settings():
    from .settings import specs
    return specs()


def _local_cli(groups):
    from .entries import add_cli
    add_cli(groups)


def _doctor(report, cfg):
    from .entries import EntriesError, load
    try:
        load(cfg.config_dir / "hub.entries.json")
    except EntriesError as exc:
        report.fail("Hub entries", str(exc))
    else:
        report.ok("Hub entries", "local entries are readable and valid (or absent)")


def _enabled(cfg, switch):
    return cfg.hub.enabled


MODULE = Module(
    name="hub", switches=("hub",), title="Hub inventory", docs=("docs/hub.md",),
    runtime=_runtime, runtime_enabled=_enabled, register_admin=_register,
    settings=_settings, doctor=_doctor, local_cli=_local_cli,
    cli=(("hub status", "GET", "/hub", "show module, paired-app and local-entry inventory", ()),),
)
