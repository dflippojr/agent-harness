"""Optional Claude Code Remote Control and owner folder discovery."""
from harness.modules import Module, ToolGate, app_allows

LIMITS = dict(visited_directories=20_000, candidates=500, seconds=30, errors=50,
              active_scans=1, expiry_seconds=900)


def _runtime(manager, module):
    from .runtime import RemoteControlRuntime
    return RemoteControlRuntime(manager, module)


def _owner_routes():
    from .routes import owner_routes
    return owner_routes


def _app_routes():
    from .routes import app_routes
    return app_routes


def _settings():
    from .settings import specs
    return specs()


def _validate(cfg, proposed):
    from .settings import validate_discovery
    return validate_discovery(cfg, proposed)


def _validate_change(cfg, proposed):
    from .settings import validate_discovery_roots_change
    return validate_discovery_roots_change(cfg, proposed)


def _register_admin(app, mgr, require_admin):
    from .discovery_api import register
    return register(app, mgr, require_admin)


def _eligible(kit, session):
    settings = kit.discovery.settings
    defaults = settings.app_defaults_for_session(session) if settings is not None else {}
    return (session['target'] == 'tower' and session.get('app_id', '') == ''
            and app_allows(defaults, 'remote_control'))


MODULE = Module(
    name='remote_control', switches=('remote_control',), title='Remote Control',
    docs=('docs/modules.md',), runtime=_runtime,
    runtime_enabled=lambda cfg, switch: bool(cfg.remote_control.enabled),
    owner_routes=_owner_routes, app_routes=_app_routes,
    app_scopes={'remote_control': 'start and stop Claude Code Remote Control servers in project folders'},
    app_capabilities={'remote_control': 'remote_control'},
    admin_paths=frozenset({'/remote-control', '/remote-control/{project}',
                           '/remote-control/{project}/trust', '/remote-control/{project}/stop'}),
    register_admin=_register_admin, settings=_settings,
    settings_validators=(_validate,), settings_change_validators=(_validate_change,),
    settings_schema={'discovery_limits': LIMITS},
    tool_names=('open_claude_remote_control',),
    tools=ToolGate(project_flag='', capability='remote_control', mcp=False, eligible=_eligible),
    cli=(
    ("remote-control list", "GET", "/remote-control", "list Remote Control folders and sessions", ()),
    ("remote-control launch", "POST", "/remote-control/{project}", "start Remote Control in a folder", ()),
    ("remote-control stop", "POST", "/remote-control/{project}/stop", "stop Remote Control in a folder", ()),
    ("remote-control trust", "POST", "/remote-control/{project}/trust", "trust a folder for Remote Control", ()),
    ("remote-control scan", "POST", "/remote-control/discovery/scans", "scan for project folders", ()),
    ("remote-control scan-show", "GET", "/remote-control/discovery/scans/{scan_id}", "show a folder scan", ()),
    ("remote-control scan-cancel", "DELETE", "/remote-control/discovery/scans/{scan_id}", "cancel a folder scan", ()),
    ("remote-control promote", "POST", "/remote-control/discovery/scans/{scan_id}/candidates/{candidate_id}/promote",
     "add a scanned folder to Remote Control", ("slug", "confirmed_path", "confirmed_markers:list")),
    ("remote-control forget", "DELETE", "/remote-control/folders/{slug}", "remove a discovered folder", ()),
    ), cli_groups={'remote-control': 'Remote Control folders'},
)
