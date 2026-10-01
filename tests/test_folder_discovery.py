"""Owner discovery contracts; fake metadata backend exercises races on every OS."""
import asyncio
import json
import ntpath
import socket
import subprocess
from functools import wraps
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from harness.discovery_paths import DiscoveryError, Identity, WindowsDirectories, beneath, lexical
from harness.folder_discovery import FolderDiscovery, FolderStore, LIMITS, MARKERS, SUFFIXES
from harness.remote_control import RemoteControl
from harness.settings_keys import build_registry
from harness.settings_service import SettingsError, SettingsService
from test_daemon import make_cfg


def run_async(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


class Metadata:
    def __init__(self):
        self.tree = {'C:\\Projects': []}
        self.ids = {}
        self.unsafe = set()
        self.calls = []

    def supported(self):
        pass

    def safe(self, path):
        if any(beneath(path, root) for root in self.unsafe):
            raise DiscoveryError('reparse_point')

    @contextmanager
    def opened(self, path):
        self.safe(path)
        if path not in self.tree:
            raise DiscoveryError('directory_unavailable')
        self.calls.append(path)
        yield Identity(path, 1, self.ids.get(path, path))

    def roots(self, values):
        out = []
        for path in values:
            with self.opened(path) as ident:
                out.append(ident)
        return out

    @contextmanager
    def entries(self, path):
        yield iter(self.tree[path])

    def entry(self, entry):
        if isinstance(entry, Exception):
            raise entry
        return (*entry, False) if len(entry) == 3 else entry


def discovery(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.remote_control.discovery.enabled = True
    cfg.remote_control.discovery.roots = ['C:\\Projects']
    cfg.remote_control.spawn = 'same-dir'
    rc = RemoteControl(cfg, cfg.remote_control, claude_json=tmp_path / 'claude.json')
    fs = Metadata()
    service = FolderDiscovery(rc, directories=fs)
    rc.discovery = service
    return service, fs


async def scanned(service):
    started = await service.start('owner-id')
    await service._task
    return service.view(service.get(started['id']))


def promote(service, scan, slug='project'):
    candidate = scan['candidates'][0]
    return service.promote(scan['id'], candidate['id'], slug, candidate['path'], candidate['markers'], 'owner-id')


@pytest.mark.parametrize('path', ['C:\\', '\\\\host\\share', '\\\\?\\C:\\Projects', '\\\\.\\C:\\Projects',
                                  'relative', 'C:Projects', 'C:\\Projects:stream', 'C:\\Projects.',
                                  'C:\\Projects ', 'C:\\Projects\\..\\Other'])
def test_lexical_rejections(path):
    with pytest.raises(DiscoveryError):
        lexical(path)


def test_component_containment_and_overlap():
    assert beneath('c:\\projects\\folder', 'C:\\Projects')
    assert not beneath('C:\\Projects-secret', 'C:\\Projects')
    assert not beneath('D:\\Projects', 'C:\\Projects')


@pytest.mark.parametrize('name', sorted(MARKERS) + ['project' + s for s in SUFFIXES])
@run_async
async def test_all_markers_stop_descent(tmp_path, name):
    service, fs = discovery(tmp_path)
    fs.tree['C:\\Projects'] = [(name, name == '.git', False), ('child', True, False)]
    fs.tree['C:\\Projects\\child'] = [('package.json', False, False)]
    scan = await scanned(service)
    assert scan['status'] == 'finished'
    assert scan['candidates'][0]['markers'] == [name]
    assert 'C:\\Projects\\child' not in fs.calls
    assert not service.store.active_path.exists()


@run_async
async def test_skips_depth_partial_errors_limits_cancel_expiry(tmp_path, monkeypatch):
    service, fs = discovery(tmp_path)
    fs.tree['C:\\Projects'] = [(n, True, False) for n in ('node_modules', '.ssh', 'build', 'a')] + [('hidden', True, True)]
    fs.tree['C:\\Projects\\a'] = [PermissionError('secret raw path')]
    scan = await scanned(service)
    assert scan['errors'] == [{'location': '*', 'code': 'directory_unavailable'}]
    assert not any(n in fs.calls for n in ['C:\\Projects\\.ssh', 'C:\\Projects\\build'])
    fs.tree['C:\\Projects\\a'] = [('child', True, False)]
    fs.tree['C:\\Projects\\a\\child'] = [('package.json', False, False)]
    service.cfg.remote_control.discovery.max_depth = 1
    assert not (await scanned(service))['candidates']
    monkeypatch.setitem(LIMITS, 'visited_directories', 1)
    truncated = await scanned(service)
    assert truncated['truncated'] and truncated['visited'] == 1
    first = await service.start('owner-id')
    second = await service.start('owner-id')
    assert first['id'] == second['id']
    service.cancel(first['id'], 'owner-id')
    service.cancel(first['id'], 'owner-id')
    await service._task
    assert service.get(first['id']).status == 'cancelled'
    service.clock = lambda: service.get.__self__.scans[first['id']].created + 901
    # Avoid recursive clock evaluation after expiry by capturing now.
    now = service.scans[first['id']].created + 901
    service.clock = lambda: now
    with pytest.raises(DiscoveryError, match='scan_expired'):
        service.get(first['id'])


@run_async
async def test_promotion_identity_roots_revision_confirmation_and_lkg(tmp_path):
    service, fs = discovery(tmp_path)
    fs.tree['C:\\Projects'] = [('package.json', False, False)]
    scan = await scanned(service)
    fs.ids['C:\\Projects'] = 'replacement'
    with pytest.raises(DiscoveryError):
        promote(service, scan)
    fs.ids.clear()
    fs.unsafe.add('C:\\Projects')
    with pytest.raises(DiscoveryError):
        promote(service, scan)
    fs.unsafe.clear()
    candidate = scan['candidates'][0]
    with pytest.raises(DiscoveryError, match='confirmation_required'):
        service.promote(scan['id'], candidate['id'], 'x', 'C:\\Injected', candidate['markers'], 'owner')
    service.revision = lambda: 1
    with pytest.raises(DiscoveryError, match='config_revision_changed'):
        promote(service, scan)
    service.revision = lambda: 0
    assert promote(service, scan)['slug'] == 'project'
    assert promote(service, scan, 'other')['already_added']
    entry = service.entries()['project']
    assert entry['owner_only'] is True
    with service.store.lock():
        service.store.save(service.store.load(), service.entries())
    service.store.active_path.write_text('broken', encoding='utf-8')
    assert service.entries()['project'] == entry
    audit = (service.rc.dir / 'discovery-audit.jsonl').read_text()
    assert 'C:\\' not in audit and 'Projects' not in audit and 'package.json' not in audit


@run_async
async def test_git_required_and_slug_conflicts(tmp_path):
    service, fs = discovery(tmp_path)
    fs.tree['C:\\Projects'] = [('package.json', False, False)]
    scan = await scanned(service)
    service.rc.rc.spawn = 'worktree'
    with pytest.raises(DiscoveryError, match='git_required'):
        promote(service, scan)
    fs.tree['C:\\Projects'].append(('.git', False, True))
    service.rc.rc.folders['project'] = 'C:\\Other'
    with pytest.raises(DiscoveryError, match='slug_conflict'):
        promote(service, scan)
    del service.rc.rc.folders['project']
    assert promote(service, scan)['slug'] == 'project'


def test_registry_has_only_dedicated_owner_settings(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    registry = build_registry(cfg)
    specs = [s for s in registry.specs.values() if s.key.startswith('remote_control.discovery.')]
    assert len(specs) == 3
    assert all(s.scope == 'admin' and s.apply_mode == 'live' and s.platforms == ('win32',) for s in specs)
    roots = registry.get('remote_control.discovery.roots')
    assert roots.value_type == 'discovery_root_list'
    assert not any(s.value_type == roots.value_type and s.key != roots.key for s in registry.specs.values())
    service = SettingsService(cfg)
    assert 'discovery' not in json.dumps(service.app_schema({'scopes': 'remote_control'}))
    monkeypatch.setattr(WindowsDirectories, 'roots', lambda self, values: [])
    with pytest.raises(SettingsError):
        service.patch_admin({'remote_control.discovery.enabled': True}, 0)


def test_real_windows_root_fail_closed(tmp_path):
    fs = WindowsDirectories(make_cfg(tmp_path))
    # The checkout is under OneDrive; actual availability/attributes depend on
    # the host. Either accept metadata or reject with a stable sanitized code.
    try:
        with fs.opened(str(tmp_path)) as identity:
            assert identity.file_id and identity.volume_serial
    except DiscoveryError as error:
        assert str(tmp_path) not in str(error)
