"""Remote Control extraction contracts. No Claude processes or real connections."""
from types import SimpleNamespace
from fastapi.testclient import TestClient

from harness import config
from harness.api import create_app
from harness.manager import Manager
from harness_modules.remote_control import LIMITS, settings
from harness_modules.remote_control import MODULE
from harness_modules.remote_control.service import RemoteControlError
from test_daemon import make_cfg, Script
from harness.llm import Completion


def rc_manager(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.runners = {}
    for section in (cfg.memory_library, cfg.remote_control, cfg.notify, cfg.gpu_guard):
        section.enabled = False
    return Manager(cfg, chat=Script([Completion(content='fake')]))


def test_runtime_without_starting_connections(tmp_path):
    m = rc_manager(tmp_path)
    cfg = m.cfg
    cfg.remote_control.enabled = True
    assert {s.key: s for s in settings.specs()}['remote_control.enabled'].enable_check(cfg) == []
    runtime = m.modules.get('remote_control')
    runtime.init()
    assert runtime.toolkit() is m.remote_control
    assert m.remote_control.discovery.settings is m.settings
    assert runtime.features() == {'remote_control': True}
    assert runtime.module.runtime_enabled(cfg, 'remote_control')
    sent = []
    monkey_notifier = type('Notifier', (), {'send': lambda self, data: sent.append(data)})()
    # A fake notification service keeps this test entirely local.
    m.modules.get('notifications').service = monkey_notifier
    runtime._ready({'title': 'fake'})
    assert sent == [{'topic': cfg.notify.topic, 'title': 'fake'}]
    cfg.remote_control.enabled = False
    assert not config.module_effective(cfg, 'remote_control')


def test_folders_yaml_key_survives_extraction(tmp_path):
    cfg_dir = tmp_path / 'cfg'
    cfg_dir.mkdir()
    (cfg_dir / 'harness.yaml').write_text(
        'models:\n  m:\n    base_url: http://127.0.0.1:1\nremote_control:\n  enabled: true\n',
        encoding='utf-8')
    (cfg_dir / 'harness.local.yaml').write_text(
        'remote_control:\n  folders:\n    separate: C:/Projects/separate\n', encoding='utf-8')
    cfg = config.load(cfg_dir, data_dir=tmp_path / "data")
    assert cfg.remote_control.folders == {'separate': 'C:/Projects/separate'}
    assert cfg.remote_control.enabled
    assert (tmp_path / "data" / "managed-config.lock").is_file()


def test_routes_use_fake_service_and_preserve_error_codes(tmp_path):
    m = rc_manager(tmp_path)

    class Fake:
        def status(self, include_owner_only=False):
            return [{'project': 'fake', 'owner': include_owner_only}]

        async def launch(self, project, **kwargs):
            if project == 'bad':
                raise RemoteControlError('fake launch failure')
            return dict(project=project, **kwargs)

        def open_trust_prompt(self, project, **kwargs):
            if project == 'bad':
                raise RemoteControlError('fake trust failure')
            return dict(project=project, **kwargs)

        async def stop(self, project, **kwargs):
            if project == 'bad':
                raise RemoteControlError('fake stop failure')
            return dict(project=project, **kwargs)

    m.modules.get('remote_control').service = Fake()
    app_record = m.db.create_api_key('rc-test', 'remote_control', kind='app')
    # create_api_key returns the record and the raw bearer token.
    headers = {'Authorization': 'Bearer ' + app_record[1]}
    with TestClient(create_app(m)) as client:
        assert client.get('/remote-control').json()['enabled']
        owner = client.get('/api/admin/v1/remote-control').json()
        assert owner['projects'][0]['owner']
        assert owner['discovery']['limits'] == LIMITS
        assert client.post('/api/admin/v1/remote-control/fake').json()['include_owner_only']
        assert client.post('/api/admin/v1/remote-control/fake/trust').json()['include_owner_only']
        assert client.post('/api/admin/v1/remote-control/fake/stop').json()['include_owner_only']
        assert client.get('/api/v1/remote-control', headers=headers).json()['projects'][0]['owner'] is False
        assert client.post('/api/v1/remote-control/fake', headers=headers).json()['started_by'] == 'app:rc-test'
        assert client.post('/api/v1/remote-control/fake/stop', headers=headers).status_code == 200
        for prefix, extra in [('/remote-control', {}), ('/api/v1/remote-control', headers)]:
            assert client.post(prefix + '/bad', headers=extra).status_code == 400
            assert client.post(prefix + '/bad/stop', headers=extra).status_code == 404
        assert client.post('/remote-control/bad/trust').status_code == 400
        m.modules.get('remote_control').service = None
        assert client.get('/api/v1/remote-control', headers=headers).json()['enabled'] is False
        assert client.post('/api/v1/remote-control/fake', headers=headers).status_code == 400
        assert client.post('/api/v1/remote-control/fake/stop', headers=headers).status_code == 400


def test_tool_gate_honors_owner_defaults_and_excludes_apps_and_runners():
    session = {'target': 'tower', 'app_id': ''}
    kit = SimpleNamespace(discovery=SimpleNamespace(settings=None))
    assert MODULE.tools.eligible(kit, session)
    assert not MODULE.tools.mcp and not MODULE.tools.members
    assert not MODULE.tools.eligible(kit, {**session, 'app_id': 'app-test'})
    assert not MODULE.tools.eligible(kit, {**session, 'target': 'macbook'})
    kit.discovery.settings = SimpleNamespace(app_defaults_for_session=lambda _: {'app.capabilities': ['web']})
    assert not MODULE.tools.eligible(kit, session)


def test_a_device_key_needs_the_owner_granted_scope(tmp_path):
    """#786: a device key launches and stops Remote Control only with the remote_control scope the owner gave it."""
    m = rc_manager(tmp_path)

    class Fake:
        async def launch(self, project, **kwargs):
            return dict(project=project, **kwargs)

        async def stop(self, project, **kwargs):
            return dict(project=project, **kwargs)

    m.modules.get('remote_control').service = Fake()
    scoped = {'Authorization': 'Bearer ' + m.db.create_api_key('rc-phone', 'remote_control', kind='device')[1]}
    unscoped = {'Authorization': 'Bearer ' + m.db.create_api_key('phone', 'sessions', kind='device')[1]}
    with TestClient(create_app(m)) as client:
        for path in ('/api/v1/remote-control/fake', '/api/v1/remote-control/fake/stop'):
            refused = client.post(path, headers=unscoped)
            assert refused.status_code == 403 and "'remote_control' scope" in refused.json()['detail']
        assert client.post('/api/v1/remote-control/fake', headers=scoped).json()['started_by'] == 'app:rc-phone'
        assert client.post('/api/v1/remote-control/fake/stop', headers=scoped).status_code == 200
