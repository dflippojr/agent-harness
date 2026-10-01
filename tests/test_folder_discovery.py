"""Owner discovery contracts; fake metadata backend exercises races on every OS."""
import asyncio
import json
import ntpath
import socket
import subprocess
import ctypes
import sys
from functools import wraps
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from harness.discovery_paths import DiscoveryError, Identity, WindowsDirectories, beneath, lexical
from harness.folder_discovery import FolderDiscovery, FolderStore, LIMITS, MARKERS, SUFFIXES
from harness.remote_control import RemoteControl
from harness.fileops import ToolError
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


class WinFunction:
    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


def fake_windows(monkeypatch, bad_component='', tag=0, drive_type=3, device='\\Device\\HarddiskVolume3', fs='NTFS'):
    handles, closed = {}, []
    def create(path, *_):
        handle = len(handles) + 1
        handles[handle] = path
        return handle
    def info(handle, kind, output, size):
        if kind == 9:
            output._obj.attributes = 0x10 | (0x400 if bad_component and handles[handle].endswith(bad_component) else 0)
            output._obj.tag = tag
        elif kind == 18:
            output._obj.volume = 17
            output._obj.id[0] = 19
        return 1
    def final(handle, output, *_):
        output.value = handles[handle]
        return len(output.value)
    def device_name(drive, output, size):
        output.value = device
        return len(device)
    def volume(handle, name, count, serial, length, flags, output, size):
        output.value = fs
        return 1
    api = SimpleNamespace(**{k: WinFunction(v) for k, v in dict(
        CreateFileW=create, CloseHandle=lambda h: closed.append(h), GetFileInformationByHandleEx=info,
        GetFinalPathNameByHandleW=final, GetVolumeInformationByHandleW=volume,
        QueryDosDeviceW=device_name, GetDriveTypeW=lambda _: drive_type).items()})
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *a, **kw: api, raising=False)
    return handles, closed


@pytest.mark.parametrize('component', ['C:\\', 'parent', 'project'])
@pytest.mark.parametrize('tag', [0xA000000C, 0xA0000003, 0x9000001A, 0x9000001B, 0x80000000])
def test_all_ancestor_reparse_tags_rejected(tmp_path, monkeypatch, component, tag):
    fs = WindowsDirectories(make_cfg(tmp_path))
    handles, closed = fake_windows(monkeypatch, bad_component=component, tag=tag)
    with pytest.raises(DiscoveryError, match='reparse_point'):
        with fs.opened('C:\\parent\\project'):
            pytest.fail('reparse point accepted')
    assert len(handles) == len(closed)


@pytest.mark.parametrize('drive_type,device,filesystem', [(2, '\\Device\\HarddiskVolume3', 'NTFS'),
                       (4, '\\Device\\LanmanRedirector', 'NTFS'), (5, '', 'NTFS'), (6, '', 'NTFS'),
                       (3, '\\??\\C:\\parent', 'NTFS'), (3, '\\Device\\HarddiskVolume3', 'FAT32')])
def test_nonlocal_nonfixed_and_non_ntfs_volumes(tmp_path, monkeypatch, drive_type, device, filesystem):
    fs = WindowsDirectories(make_cfg(tmp_path))
    fake_windows(monkeypatch, drive_type=drive_type, device=device, fs=filesystem)
    with pytest.raises(DiscoveryError):
        with fs.opened('C:\\parent\\project'):
            pytest.fail('unsupported volume accepted')


def test_canonical_case_overlap_and_sensitive_roots(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    cfg.data_dir = 'C:\\daemon\\data'
    cfg.provider_secret_files = {'provider': 'C:\\credentials-parent\\secret.txt'}
    fs = WindowsDirectories(cfg)
    fake_windows(monkeypatch)
    roots = fs.roots(['C:\\parent\\project', 'c:\\PARENT', 'C:\\parent', 'C:\\parent-sibling'])
    assert len(roots) == 2
    for path in ['C:\\daemon\\data\\other', 'C:\\credentials-parent', 'C:\\parent\\.ssh',
                 'C:\\Windows', 'C:\\parent\\node_modules', 'C:\\$Recycle.Bin']:
        with pytest.raises(DiscoveryError):
            fs.safe(path)


@run_async
async def test_managed_isolation_revalidation_and_removal(tmp_path, monkeypatch):
    service, fs = discovery(tmp_path)
    fs.tree['C:\\Projects'] = [('package.json', False, False)]
    scan = await scanned(service)
    promote(service, scan)
    rc = service.rc
    assert 'project' not in rc.eligible()
    assert not rc.status()
    assert 'project' in rc.eligible(include_owner_only=True)
    assert rc.status(include_owner_only=True)[0]['managed']
    assert 'project' not in rc.schemas()[0]['function']['parameters']['properties']['project'].get('enum', [])
    with pytest.raises(ToolError):
        await rc.call('open_claude_remote_control', {'project': 'project'})
    with pytest.raises(ToolError):
        rc.open_trust_prompt('project')
    with pytest.raises(ToolError):
        await rc.stop('project')
    fs.unsafe.add('C:\\Projects')
    monkeypatch.setattr(rc, 'popen', lambda *a, **kw: pytest.fail('unsafe directory launched'))
    with pytest.raises(ToolError, match='reparse_point'):
        await rc.launch('project', include_owner_only=True)
    with pytest.raises(ToolError, match='reparse_point'):
        rc.open_trust_prompt('project', include_owner_only=True)
    assert rc.status(include_owner_only=True)[0]['invalid'] == 'reparse_point'
    fs.unsafe.clear()
    rc._trust_processes['project'] = SimpleNamespace(poll=lambda: None)
    with pytest.raises(DiscoveryError, match='entry_active'):
        await service.remove('project', 'owner')
    rc._trust_processes.clear()
    with pytest.raises(DiscoveryError, match='managed_folder_unknown'):
        await service.remove('file-defined', 'owner')
    assert (await service.remove('project', 'owner'))['removed']
    assert fs.tree['C:\\Projects'] == [('package.json', False, False)]


def test_authorization_on_every_discovery_endpoint(tmp_path, monkeypatch):
    from harness import discovery_api
    monkeypatch.setattr(discovery_api, 'sys', SimpleNamespace(platform='win32'))
    from test_admin import make_client, bearer, PREFIX
    from harness.config import GuestAccess
    client, manager = make_client(tmp_path)
    cfg = manager.cfg
    cfg.guests = [GuestAccess(login='guest@example.com', until='2099-01-01T00:00:00+00:00')]
    cfg.remote_control.discovery.enabled = True
    cfg.remote_control.discovery.roots = ['C:\\Projects']
    cfg.remote_control.spawn = 'same-dir'
    rc = RemoteControl(cfg, cfg.remote_control, claude_json=tmp_path / 'claude.json')
    rc.discovery.fs = Metadata()
    rc.discovery.settings = manager.settings
    manager.remote_control = rc
    rc.discovery.fs.tree['C:\\Projects'] = [('package.json', False, False)]
    scan = asyncio.run(scanned(rc.discovery))
    candidate = scan['candidates'][0]
    root = PREFIX + '/remote-control'
    operations = [('POST', root + '/discovery/scans', None), ('GET', root + '/discovery/scans/' + scan['id'], None),
        ('DELETE', root + '/discovery/scans/' + scan['id'], None),
        ('POST', root + '/discovery/scans/' + scan['id'] + '/candidates/' + candidate['id'] + '/promote',
         dict(slug='project', confirmed_path=candidate['path'], confirmed_markers=candidate['markers'])),
        ('DELETE', root + '/folders/project', None)]
    with client:
        app = client.post('/keys', json=dict(name='app', kind='app', scopes=['sessions', 'sessions:all',
                            'approvals', 'images', 'inference', 'remote_control'])).json()
        device = client.post('/keys', json=dict(name='device')).json()
        _, runner_token = manager.db.create_api_key('runner', 'admin remote_control', kind='runner')
        owner = client.post('/keys', json=dict(name='owner', kind='owner', scopes=['admin'])).json()
        member = client.post(PREFIX + '/accounts', json=dict(login='member@example.com', display_name='Member'))
        assert member.status_code == 201
        refused = [bearer(app['key']), bearer(device['key']), bearer(runner_token), {'Tailscale-User-Login': 'member@example.com'},
                   {'Tailscale-User-Login': 'guest@example.com'}, {'Tailscale-User-Login': 'unknown@example.com'},
                   {**bearer(owner['key']), 'Origin': 'https://evil.example'}]
        for method, path, body in operations:
            for headers in refused:
                response = client.request(method, path, headers=headers, json=body)
                assert response.status_code in (401, 403, 404), response.text
                assert 'Projects' not in response.text and candidate['id'] not in response.text
        assert client.get(operations[1][1], headers=bearer(owner['key'])).status_code == 200
        response = client.post(operations[3][1], headers=bearer(owner['key']), json=operations[3][2])
        assert response.status_code == 200, response.text
        assert not client.get('/api/v1/remote-control', headers=bearer(app['key'])).json()['projects']
        assert client.post('/api/v1/remote-control/project', headers=bearer(app['key'])).status_code != 200
        assert 'remote_control.discovery' not in json.dumps(client.get('/api/v1', headers=bearer(app['key'])).json())
        assert '/remote-control/discovery' not in json.dumps(client.get('/api/v1', headers=bearer(app['key'])).json())
        assert client.get(root, headers=bearer(owner['key'])).json()['projects'][0]['managed']
        assert client.delete(root + '/folders/project', headers=bearer(owner['key'])).status_code == 200
        monkeypatch.setattr(discovery_api, 'sys', SimpleNamespace(platform='linux'))
        for method, path, body in operations:
            response = client.request(method, path, headers=bearer(owner['key']), json=body)
            assert response.status_code == 400 and response.json()['detail'] == 'unsupported_platform'


@run_async
async def test_scanner_never_reads_files_or_runs_commands_or_network(tmp_path, monkeypatch):
    service, fs = discovery(tmp_path)
    fs.tree['C:\\Projects'] = [('package.json', False, False)]
    started = await service.start('owner')
    await service._task
    scan = service.get(started['id'])
    monkeypatch.setattr(service, 'audit', lambda *a, **kw: None)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **kw: pytest.fail('command invoked'))
    monkeypatch.setattr(socket, 'socket', lambda *a, **kw: pytest.fail('network invoked'))
    monkeypatch.setattr('builtins.open', lambda *a, **kw: pytest.fail('file opened'))
    scan.reason = ''
    service._run(scan)
    assert scan.status == 'finished'


@run_async
async def test_add_trust_and_launch_are_separate_and_revalidate(tmp_path, monkeypatch):
    from harness import remote_control
    service, fs = discovery(tmp_path)
    fs.tree['C:\\Projects'] = [('package.json', False, False)]
    launches = []
    process = SimpleNamespace(pid=999999999, poll=lambda: None)
    service.rc.popen = lambda *a, **kw: launches.append((a, kw)) or process
    scan = await scanned(service)
    promote(service, scan)
    assert not launches and not service.rc.claude_json.exists()
    monkeypatch.setattr(remote_control, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(subprocess, 'CREATE_NEW_CONSOLE', 16, raising=False)
    monkeypatch.setattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 512, raising=False)
    monkeypatch.setattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000, raising=False)
    monkeypatch.setattr(remote_control.shutil, 'which', lambda _: 'powershell.exe')
    monkeypatch.setattr(service.rc, '_claude', lambda: 'claude.exe')
    view = service.rc.open_trust_prompt('project', include_owner_only=True)
    assert view['trust_prompt_open']
    assert len(launches) == 1 and launches[0][1]['creationflags'] & 16
    assert launches[0][1]['cwd'] == 'C:\\Projects'
    assert not service.rc.claude_json.exists()
    with pytest.raises(ToolError):
        await service.rc.launch('project', include_owner_only=True)
    service.rc.claude_json.write_text(json.dumps({'projects': {'C:/Projects': {'hasTrustDialogAccepted': True}}}))
    service.rc._spawn_logged = lambda *a, **kw: launches.append((a, kw)) or process
    service.rc._alive = lambda entry: bool(entry)
    service.rc._log_text = lambda entry: 'https://claude.ai/code?environment=test_01'
    view = await service.rc.launch('project', include_owner_only=True)
    assert view['running'] and len(launches) == 2
    fs.ids['C:\\Projects'] = 'changed-id'
    with pytest.raises(ToolError):
        await service.rc.launch('project', include_owner_only=True)
    assert len(launches) == 2
    with pytest.raises(DiscoveryError, match='entry_active'):
        await service.remove('project', 'owner')
    fs.ids.clear()
    service.rc._alive = lambda _: False
    service.rc._trust_processes.clear()
    before = service.rc.claude_json.read_text()
    await service.remove('project', 'owner')
    assert service.rc.claude_json.read_text() == before


@run_async
async def test_candidate_error_and_time_caps_and_reparse_markers(tmp_path, monkeypatch):
    service, fs = discovery(tmp_path)
    fs.tree['C:\\Projects'] = [('a', True, False), ('b', True, False)]
    for name in ('a', 'b'):
        fs.tree['C:\\Projects\\' + name] = [('package.json', False, False)]
    monkeypatch.setitem(LIMITS, 'candidates', 1)
    scan = await scanned(service)
    assert scan['truncated'] and scan['reason'] == 'candidate_limit' and len(scan['candidates']) == 1
    monkeypatch.setitem(LIMITS, 'candidates', 500)
    fs.tree['C:\\Projects'] = [(str(i), True, False) for i in range(60)]
    scan = await scanned(service)
    assert len(scan['errors']) == 50 and scan['status'] == 'finished'
    monkeypatch.setitem(LIMITS, 'seconds', 0)
    scan = await scanned(service)
    assert scan['reason'] == 'time_limit' and scan['truncated']
    monkeypatch.setitem(LIMITS, 'seconds', 30)
    fs.tree['C:\\Projects'] = [('.git', True, False, True), ('hidden', True, True, False)]
    scan = await scanned(service)
    assert not scan['candidates']


@run_async
async def test_path_identity_conflicts_and_file_entries_win(tmp_path):
    service, fs = discovery(tmp_path)
    fs.tree['C:\\Projects'] = [('package.json', False, False)]
    scan = await scanned(service)
    promote(service, scan)
    fs.ids['C:\\Projects'] = 'replacement'
    scan = await scanned(service)
    with pytest.raises(DiscoveryError, match='managed_path_conflict'):
        promote(service, scan, 'another')
    fs.ids.clear()
    local = tmp_path / 'local'
    local.mkdir()
    service.rc.rc.folders['project'] = str(local)
    assert service.rc.folder('project', include_owner_only=True) == local
    status = service.rc.status(include_owner_only=True)
    assert any(row.get('managed') and row['invalid'] == 'slug_conflict' for row in status)
    assert any(not row.get('managed') and row['path'] == str(local) for row in status)


@run_async
async def test_configured_duplicate_expands_server_environment(tmp_path, monkeypatch):
    service, fs = discovery(tmp_path)
    fs.tree['C:\\Projects'] = [('package.json', False, False)]
    monkeypatch.setenv('DISCOVERY_TEST_ROOT', 'C:\\Projects')
    service.rc.rc.folders['configured'] = '$DISCOVERY_TEST_ROOT'
    scan = await scanned(service)
    assert scan['candidates'][0]['configured_duplicate']
    with pytest.raises(DiscoveryError, match='configured_duplicate'):
        promote(service, scan)


@run_async
async def test_managed_launch_keeps_real_trust_and_worktree_errors(tmp_path):
    service, fs = discovery(tmp_path)
    fs.tree['C:\\Projects'] = [('package.json', False, False)]
    scan = await scanned(service)
    promote(service, scan)
    with pytest.raises(ToolError, match="hasn't been trusted"):
        await service.rc.launch('project', include_owner_only=True)
    service.rc.claude_json.write_text(json.dumps({'projects': {'C:/Projects': {'hasTrustDialogAccepted': True}}}))
    service.rc.rc.spawn = 'worktree'
    with pytest.raises(ToolError, match="isn't a git repository"):
        await service.rc.launch('project', include_owner_only=True)
