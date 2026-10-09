"""Stage (c)'s last add-on: supervision is optional; the local backend stays core."""
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from harness import cli, modules
from harness.api import create_app
from harness.config import GpuGuardConfig
from harness.local_inference import NoWarmer, READY
from harness.manager import Manager
from harness.settings_keys import build_registry
from harness_modules.local_model import doctor
from harness_modules.local_model.warmup import ModelWarmer
from test_daemon import make_cfg, Script
from harness.llm import Completion

ROUTES = {"/models/status", "/models/warm", "/gpu", "/gpu/{action}", "/resources",
          "/resources/diagnostics", "/resources/{action}", "/api/v1/models/status", "/api/v1/models/warm"}
KEYS = {"gpu_guard.enabled", "gpu_guard.poll_seconds", "gpu_guard.resume_after_seconds",
        "gpu_guard.drain_timeout_seconds", "modules.gpu_guard", "modules.local_model"}


def manager(tmp_path, *, enabled=False, packages=None):
    cfg = make_cfg(tmp_path)
    cfg.module_packages = packages
    cfg.gpu_guard = GpuGuardConfig(enabled=enabled, pause_flag=str(tmp_path / 'paused'))
    return Manager(cfg, chat=Script([Completion(content='ok')]))


@pytest.mark.parametrize('enabled', [False, True])
def test_present_and_guard_switched_off(tmp_path, enabled):
    m = manager(tmp_path, enabled=enabled)
    assert isinstance(m.warmer, ModelWarmer) and m.runner.warmer is m.warmer
    assert (m.guard is not None) is enabled
    assert (m.runner.ram is not None) is enabled
    assert KEYS <= build_registry(m.cfg).specs.keys()
    assert m.cfg.module_effective('local_model')
    assert m.cfg.module_effective('gpu_guard') is enabled
    app = create_app(m)
    assert ROUTES <= {route.path for route in app.routes}
    with TestClient(app) as client:
        assert client.get('/models/status').json()[0]['state'] == READY
        assert client.post('/models/warm').status_code == 200
        assert client.get('/api/admin/v1/resources').status_code == 200
        if not enabled:
            assert client.post('/resources/pause').status_code == 400
        root = client.get('/api/v1').json()
        assert root['capabilities']['modules']['gpu_guard'] is enabled
    m.db.close()


def test_absent_supervision_retains_external_local_adapter(tmp_path):
    m = manager(tmp_path, packages=[])
    assert m.guard is None and m.runner.ram is None and isinstance(m.warmer, NoWarmer)
    assert m.runner.warmer is m.warmer
    assert not KEYS & build_registry(m.cfg).specs.keys()
    assert 'backends.local.model' in build_registry(m.cfg).specs
    assert not m.cfg.module_effective('local_model')
    assert m.cfg.modules.local_model  # backend selection, not supervision
    assert not ROUTES & {route.path for route in create_app(m).routes}
    assert not m.modules.model_control()
    with TestClient(create_app(m)) as client:
        assert client.get('/health').json()['ok']
        assert client.get('/models').json()[0]['name'] == 'fake'
        assert client.get('/api/v1/models').json()[0]['name'] == 'fake'
        assert client.get('/resources').status_code == 404
        assert 'models:warm' not in client.get('/api/v1').json()['scopes']
    async def call():
        assert await m.warmer.state(None) == READY
        await m.warmer.ensure_loaded(None)
        assert not m.warmer.parked(None) and not m.warmer.blocked()
        assert m.warmer.waking_for(None) is None
    asyncio.run(call())
    m.db.close()


def test_images_refuses_gpu_takeover_without_supervision(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.module_packages = ['harness_modules.images']
    cfg.images.enabled = True
    cfg.images.work_dir = str(tmp_path / 'images')
    cfg.images.comfy_dir = str(tmp_path / 'comfy')
    cfg.images.models_dir = str(tmp_path / 'models')
    m = Manager(cfg, chat=Script([Completion(content='ok')]))
    from harness.fileops import ToolError
    for submit in (lambda: m.images.submit('lighthouse'),
                   lambda: m.images.submit_upscale('missing', '2x'),
                   lambda: m.images.submit_edit('missing', 'lighthouse', b''),
                   lambda: asyncio.run(m.images.warmup())):
        with pytest.raises(ToolError, match='requires the local_model supervision module'):
            submit()
    with TestClient(create_app(m)) as client:
        for path, payload in [('/images', {'prompt': 'lighthouse'}), ('/images/warmup', {})]:
            response = client.post(path, json=payload)
            assert response.status_code == 400 and 'supervision module' in response.json()['detail']
    assert not m.images.queue.qsize() and not m.images.db.list_images()
    assert not m.runner.gate.exclusive
    m.db.close()


def test_recovered_images_fail_visibly_without_supervision(tmp_path):
    m = manager(tmp_path)
    m.cfg.images.enabled = True
    m.cfg.images.work_dir = str(tmp_path / 'images')
    m.cfg.images.comfy_dir = str(tmp_path / 'comfy')
    m.cfg.images.models_dir = str(tmp_path / 'models')
    # Build the service with supervision, then model the next startup after the package is removed.
    rt = m.modules.get('images')
    rt.init()
    svc = m.images
    async def run():
        jobs = [svc.submit('lighthouse'), svc.submit('cabin')]
        events = [svc._done[job['id']] for job in jobs]
        svc.control = None
        async def no_probe():
            raise AssertionError('absent supervision must not probe ComfyUI')
        svc.comfy.ready = no_probe
        notifications = []
        svc.notify = notifications.append
        svc.start()
        try:
            for job in jobs:
                result = await asyncio.wait_for(svc.wait(job['id']), 2)
                assert result['status'] == 'failed' and 'supervision module' in result['error']
                assert result['finished_at'] is not None
            assert all(event.is_set() for event in events)
            assert len(notifications) == 2
            await svc._run_batch(None)
            assert svc.phase == 'idle' and not svc._keep_warm and not m.runner.gate.exclusive
        finally:
            await svc.stop()
    asyncio.run(run())
    m.db.close()


@pytest.mark.parametrize('packages', [[], ['harness_modules.images']])
def test_absent_saved_guard_settings_remain_dormant(tmp_path, packages):
    m = manager(tmp_path)
    m.settings.patch_admin({'gpu_guard.poll_seconds': 12}, revision=None, actor={'kind': 'owner'})
    m.settings.confirm_startup()
    m.db.close()
    absent = manager(tmp_path, packages=packages)
    assert absent.settings.store.read_status().get('recovery') is None
    assert absent.settings.store.read_active().values['gpu_guard.poll_seconds'] == 12
    absent.db.close()


def test_guard_registry_accessors_checks_and_cli(tmp_path):
    m = manager(tmp_path)
    registry = build_registry(m.cfg)
    for key, value in [('gpu_guard.poll_seconds', 12), ('gpu_guard.resume_after_seconds', 40),
                       ('gpu_guard.drain_timeout_seconds', 60), ('gpu_guard.enabled', True)]:
        spec = registry.get(key)
        spec.setter(m.cfg, value)
        assert spec.getter(m.cfg) == value
    check = registry.get('gpu_guard.enabled').enable_check
    assert check(m.cfg) == []
    m.cfg.installed.local_model = False
    m.cfg.gpu_guard.pause_flag = ''
    assert len(check(m.cfg)) == 2
    assert {'models warm', 'gpu pause', 'resources diagnostics'} <= {row[0] for row in cli.admin_commands()}
    m.db.close()


def test_core_doctor_does_not_probe_absent_supervision(tmp_path, monkeypatch):
    from harness import doctor as core_doctor
    m = manager(tmp_path, packages=[])
    paths = []
    def get(url, **kwargs):
        paths.append(url)
        assert url.endswith('/health')
        return httpx.Response(200, json={'profile': m.cfg.profile})
    monkeypatch.setattr(core_doctor.httpx, 'get', get)
    report = core_doctor.Report()
    core_doctor.check_daemon(report, m.cfg)
    assert not report.failed and len(paths) == 1
    m.db.close()


def test_core_doctor_sends_the_local_owner_token_and_reports_a_refusal(tmp_path, monkeypatch):
    from harness import doctor as core_doctor, local_owner
    m = manager(tmp_path, packages=[])
    m.cfg.modules.local_model = False
    sent = []
    def get(url, headers=None, **kwargs):
        sent.append(headers.get(local_owner.HEADER))
        if url.endswith('/health'):
            return httpx.Response(200, json={'profile': m.cfg.profile})
        return httpx.Response(401, json={'detail': local_owner.REFUSED})
    monkeypatch.setattr(core_doctor.httpx, 'get', get)
    report = core_doctor.Report()
    core_doctor.check_daemon(report, m.cfg)
    assert sent and set(sent) == {m.local_owner_token}
    assert report.failed
    m.db.close()


@pytest.mark.parametrize('packages', [
    ['harness_modules.local_model', 'harness_modules.images'],
    ['harness_modules.images', 'harness_modules.local_model'],
])
def test_resource_wiring_independent_of_discovery_order(tmp_path, packages):
    cfg = make_cfg(tmp_path)
    cfg.module_packages = packages
    cfg.images.enabled = True
    cfg.images.work_dir = str(tmp_path / 'images')
    cfg.images.comfy_dir = str(tmp_path / 'comfy')
    cfg.images.models_dir = str(tmp_path / 'models')
    cfg.gpu_guard.enabled = True
    m = Manager(cfg, chat=Script([Completion(content='ok')]))
    assert m.images.control is not None
    assert m.runner.ram is m.guard.memory
    assert m.images.memory_low() is False
    assert m.images.want_model() is False
    m.db.close()


@pytest.mark.parametrize('output,failed,warned', [
    ((1, ''), 1, 0), ((0, 'Fake GPU, 579.0, 12000 MiB, 0 MiB'), 0, 2),
    ((0, 'Fake GPU, 580.0, 16000 MiB, 0 MiB'), 0, 0),
])
def test_doctor_gpu_uses_fake_output(tmp_path, monkeypatch, output, failed, warned):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(doctor, 'run_command', lambda *_: output)
    from harness.doctor import Report
    report = Report()
    doctor.check_gpu(report, cfg)
    assert (report.failed, report.warned) == (failed, warned)


def test_doctor_model_and_disabled_branches(tmp_path, monkeypatch):
    from harness.doctor import Report
    cfg, report = make_cfg(tmp_path), Report()
    monkeypatch.setattr(doctor.httpx, 'get', lambda *_args, **_kwargs: httpx.Response(200, json={
        'is_sleeping': True, 'default_generation_settings': {'n_ctx': 1}}))
    doctor.check_model_server(report, cfg)
    assert report.warned == 1
    def fail(*_args, **_kwargs):
        raise httpx.ConnectError('fake server offline')
    monkeypatch.setattr(doctor.httpx, 'get', fail)
    cfg.gpu_guard.enabled = True
    cfg.gpu_guard.pause_flag = str(tmp_path / 'paused')
    doctor.check_model_server(report, cfg)
    assert report.failed == 1
    (tmp_path / 'paused').write_text('fake')
    doctor.check_model_server(report, cfg)
    doctor.check_guard(report, cfg)
    cfg.modules.local_model = False
    doctor.check_gpu(report, cfg)
    doctor.check_model_server(report, cfg)
    assert report.warned == 3
