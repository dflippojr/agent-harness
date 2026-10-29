"""Read-only daemon inventory for the standalone Hub app (#520)."""
from harness.modules import Module, enabled_flag, lazy


def _register(app, mgr, require_admin):
    from .routes import register_admin
    return register_admin(app, mgr, require_admin)


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


MODULE = Module(
    name="hub", switches=("hub",), title="Hub inventory", docs=("docs/hub.md",),
    runtime=lazy(".runtime", "HubRuntime", call=True), runtime_enabled=enabled_flag("hub"), register_admin=_register,
    settings=lazy(".settings", "specs", call=True), doctor=_doctor, local_cli=_local_cli,
    cli=(("hub status", "GET", "/hub", "show module, paired-app and local-entry inventory", ()),),
)
