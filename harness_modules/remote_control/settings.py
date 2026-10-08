"""Remote Control runtime and owner discovery settings."""
from harness.modules import SettingSpec, Bounds, setting_bool as _bool, setting_int as _int

def _get_discovery_enabled(cfg):
    return cfg.remote_control.discovery.enabled


def _set_discovery_enabled(cfg, value):
    cfg.remote_control.discovery.enabled = value


def _get_discovery_roots(cfg):
    return list(cfg.remote_control.discovery.roots)


def _set_discovery_roots(cfg, value):
    # Lexical only: this setter re-runs for every settings PATCH and at startup, so it must not
    # depend on the filesystem. Live checks run in validate_discovery_roots_change (changes only)
    # and again at scan start.
    from .discovery_paths import DiscoveryError, lexical
    if not isinstance(value, list) or len(value) > 8 or not all(isinstance(v, str) for v in value):
        raise DiscoveryError('up_to_eight_roots')
    cfg.remote_control.discovery.roots = [lexical(v) for v in value]


def _get_discovery_depth(cfg):
    return cfg.remote_control.discovery.max_depth


def _set_discovery_depth(cfg, value):
    cfg.remote_control.discovery.max_depth = value


def validate_discovery(cfg, proposed):
    """Cheap cross-field rule; runs on the full merged overlay, so no filesystem access."""
    if cfg.remote_control.discovery.enabled and not cfg.remote_control.discovery.roots:
        return [{'key': 'remote_control.discovery.roots', 'code': 'valid_root_required',
                 'message': 'valid_root_required'}]
    return []


def validate_discovery_roots_change(cfg, proposed):
    """Live Windows validation and canonicalization, only when this request changes discovery."""
    from .discovery_paths import DiscoveryError, WindowsDirectories
    if not any(k.startswith('remote_control.discovery.') for k in proposed):
        return []
    try:
        roots = WindowsDirectories(cfg).roots(cfg.remote_control.discovery.roots)
        if cfg.remote_control.discovery.enabled and not roots:
            raise DiscoveryError('valid_root_required')
    except DiscoveryError as error:
        return [{'key': 'remote_control.discovery.roots', 'code': error.code, 'message': error.code}]
    cfg.remote_control.discovery.roots = [i.path for i in roots]
    return []


def discovery_specs():
    help_text = ('Windows owner-only, default-off metadata discovery. No file contents, trust or launch. '
                 'Limits: 20,000 directories; 500 candidates; 30 seconds; 50 errors; '
                 'one active scan; results expire after 15 minutes. Hidden/system entries, '
                 'all reparse points (including OneDrive), credentials, caches and build folders are excluded.')
    specs = [
        _bool('remote_control.discovery.enabled', 'Folder discovery', help_text,
              'Remote Control discovery', False, _get_discovery_enabled, _set_discovery_enabled,
              ('remote_control', 'discovery', 'enabled')),
        SettingSpec(key='remote_control.discovery.roots', label='Discovery roots', help=help_text,
                    category='Remote Control discovery', value_type='discovery_root_list', default=[],
                    scope='admin', apply_mode='live', getter=_get_discovery_roots, setter=_set_discovery_roots,
                    bounds=Bounds(max_length=8), yaml_path=('remote_control', 'discovery', 'roots')),
        _int('remote_control.discovery.max_depth', 'Discovery depth', help_text,
             'Remote Control discovery', 3, _get_discovery_depth, _set_discovery_depth, 1, 5,
             ('remote_control', 'discovery', 'max_depth')),
    ]
    for spec in specs:
        spec.platforms = ('win32',)
    return specs


def _get_enabled(cfg):
    return cfg.remote_control.enabled


def _set_enabled(cfg, value):
    cfg.remote_control.enabled = bool(value)


def check_remote_control(cfg):
    return []


def normalize_roots(cfg, value):
    from .discovery_paths import WindowsDirectories
    return [i.path for i in WindowsDirectories(cfg).roots(value)]


def specs():
    result = discovery_specs()
    for spec in result:
        spec.modules = ('remote_control',)
        if spec.value_type == 'discovery_root_list':
            spec.normalize_change = normalize_roots
    return [*result, _bool('remote_control.enabled', 'Remote Control',
            'Runtime enable for Remote Control. Does not install the module.', 'Features', False,
            _get_enabled, _set_enabled, ('remote_control', 'enabled'),
            apply_mode='daemon_restart', modules=('remote_control',), enable_check=check_remote_control)]
