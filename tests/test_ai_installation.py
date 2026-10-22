"""Real SQLite lifecycle and HTTP boundaries; no network or model downloads."""
from concurrent.futures import Future
from pathlib import Path
import shutil
import time
from uuid import uuid4

import pytest

from feedback.ai_download import ModelDownloadError
from feedback.ai_installation import EmbeddingInstallation
from feedback.ai_runtime import EmbeddingRuntimeError
from feedback.security import Vault, digest


class FixtureRuntime:
    calls = 0

    def __init__(self, path):
        self.path = path

    def submit(self, kind, texts):
        type(self).calls += 1
        result = Future()
        result.set_result([[1.0] + [0.0] * 767 for _ in texts])
        return result

    def embed_queries(self, texts):
        return self.submit('query', texts).result()

    def embed_documents(self, texts, *, titles=None):
        return self.submit('document', texts).result()

    def close(self):
        pass


def fixture_verify(path, *, cancelled=None):
    if cancelled and cancelled():
        raise ModelDownloadError('cancelled')
    if path.is_symlink() or not (path / 'verified-fixture').is_file():
        raise ModelDownloadError('model_file_missing')


def fixture_download(path, token, *, progress, cancelled):
    assert token == 'hf_fixture_only'
    assert not cancelled()
    path.mkdir()
    (path / 'verified-fixture').write_text('fixture')
    progress(10, 10)


@pytest.fixture
def runtime(installation):
    app, cfg, admin, user, anon = installation
    manager = app.state.ai_runtime
    manager.downloader, manager.verifier, manager.runtime_factory = fixture_download, fixture_verify, FixtureRuntime
    session = app.state.storage.session(digest(admin.cookies.get('feedback_session')))
    principal = {'kind': 'admin', 'user_id': session['id'], 'session_id': session['session_id']}
    FixtureRuntime.calls = 0
    yield manager, principal, app, cfg, admin, user, anon
    manager.stop()


def begin(admin):
    version = admin.get('/api/v1/ai/runtime').json()['version']
    response = admin.post('/api/v1/ai/runtime/download', json={'version': version, 'token': 'hf_fixture_only'})
    assert response.status_code == 200, response.text
    return response.json()


def test_status_and_actions_require_native_administrator(workflow):
    w = workflow
    for client in (w['anon'], w['user'], w['trusted'], w['manager']):
        assert client.get('/api/v1/ai/runtime').status_code == 403
        assert client.post('/api/v1/ai/runtime/download', json={'version': 0, 'token': 'secret'}).status_code == 403
    response = w['admin'].get('/api/v1/ai/runtime', headers={'X-Chat-Token': 'cv_any'})
    assert response.status_code == 400
    assert w['admin'].get('/api/v1/ai/runtime', headers={'Origin': 'https://elsewhere.example'}).status_code == 403
    assert w['admin'].post('/api/v1/ai/runtime/download', json={'version': 0, 'token': 'secret'}, headers={'X-CSRF-Token': 'wrong'}).status_code == 403


def test_explicit_install_verified_then_lazy_runtime_and_secret_erasure(runtime):
    m, p, app, cfg, admin, *_ = runtime
    assert not m.root.exists()
    assert admin.get('/api/v1/ai/runtime').json()['state'] == 'not_downloaded'
    assert not m.root.exists() and not m.run_once() and FixtureRuntime.calls == 0
    response = begin(admin)
    assert 'hf_fixture_only' not in str(response)
    assert response['state'] == 'downloading' and FixtureRuntime.calls == 0
    secrets = list(app.state.storage.signing_secrets())
    assert any(Vault(cfg.secret).reveal(value) == 'hf_fixture_only' for value in secrets)
    assert m.run_once()
    ready = m.status(p)
    assert ready['state'] == 'ready' and ready['operation_id'] is None
    assert not list(app.state.storage.signing_secrets())
    assert FixtureRuntime.calls == 1
    assert len(m.embed_queries(['question'])[0]) == 768
    assert FixtureRuntime.calls == 2
    assert not list(m.root.glob('stage-*'))


@pytest.mark.parametrize('failure', ['download', 'verify', 'inference'])
def test_failed_replacement_preserves_working_cache(runtime, failure):
    m, p, _, _, admin, *_ = runtime
    begin(admin)
    m.run_once()
    old = m.status(p)['active_cache_id']
    def fail(*args, **kwargs):
        raise ModelDownloadError('private-upstream-body-must-not-leak')
    if failure == 'download':
        m.downloader = fail
    elif failure == 'verify':
        m.verifier = fail
    else:
        m.runtime_factory = fail
    begin(admin)
    m.run_once()
    state = m.status(p)
    assert state['state'] == 'ready' and state['active_cache_id'] == old
    assert state['error'] == ('download_failed' if failure == 'download' else 'verification_failed')
    assert 'private-upstream' not in str(state)
    assert [p.name for p in m.root.iterdir()] == ['embeddinggemma-' + old]


def test_cancel_and_revoked_authority_discard_candidate(runtime):
    m, p, app, _, admin, *_ = runtime
    original = m.downloader
    def cancel_after_download(path, token, **kwargs):
        original(path, token, **kwargs)
        row = m.status(p)
        m.cancel(p, row['version'])
    m.downloader = cancel_after_download
    begin(admin)
    m.run_once()
    assert m.status(p)['state'] == 'not_downloaded'
    assert not list(m.root.iterdir()) and FixtureRuntime.calls == 0
    def revoke_after_download(path, token, **kwargs):
        original(path, token, **kwargs)
        with app.state.storage.transaction(True) as c:
            c.execute('UPDATE sessions SET revoked_at=? WHERE id=?', ('revoked', p['session_id']))
    m.downloader = revoke_after_download
    begin(admin)
    m.run_once()
    assert app.state.storage.ai_runtime_local()['active_cache_id'] is None
    assert not list(m.root.iterdir()) and not list(app.state.storage.signing_secrets())


def test_remove_only_owned_files_and_do_not_follow_symlinks(runtime, tmp_path):
    m, p, _, _, admin, *_ = runtime
    begin(admin)
    m.run_once()
    unrelated = m.root / 'admin-notes'
    unrelated.write_text('keep')
    external = tmp_path / 'outside'
    external.mkdir()
    (external / 'keep').write_text('keep')
    (m.root / ('embeddinggemma-' + str(uuid4()))).symlink_to(external, target_is_directory=True)
    row = m.status(p)
    response = admin.post('/api/v1/ai/runtime/remove', json={'version': row['version']})
    assert response.status_code == 200 and response.json()['active_manifest'] is None
    assert admin.post('/api/v1/ai/runtime/cancel', json={'version': response.json()['version']}).status_code == 409
    with pytest.raises(EmbeddingRuntimeError):
        m.embed_queries(['question'])
    m.run_once()
    assert m.status(p)['state'] == 'not_downloaded'
    assert unrelated.read_text() == 'keep' and (external / 'keep').read_text() == 'keep'
    assert list(m.root.iterdir()) == [unrelated]


@pytest.mark.parametrize('pending', [False, True])
@pytest.mark.parametrize('empty_root', [False, True])
def test_restore_without_cache_never_resumes_download(runtime, pending, empty_root):
    m, p, app, cfg, admin, *_ = runtime
    begin(admin)
    if not pending:
        m.run_once()
    shutil.rmtree(m.root)
    if empty_root:
        m.root.mkdir()
    restored = EmbeddingInstallation(app.state.storage, Vault(cfg.secret), cfg,
        downloader=fixture_download, verifier=fixture_verify, runtime_factory=FixtureRuntime)
    assert restored.status(p)['state'] == 'not_downloaded'
    assert not restored.run_once()
    assert restored.root.exists() == empty_root
    assert not list(app.state.storage.signing_secrets())


def test_restart_validates_existing_cache_offline_and_corruption_fails_closed(runtime):
    m, p, app, cfg, admin, *_ = runtime
    begin(admin)
    m.run_once()
    before = FixtureRuntime.calls
    restored = EmbeddingInstallation(app.state.storage, Vault(cfg.secret), cfg,
        downloader=lambda *a, **k: pytest.fail('No startup network'), verifier=fixture_verify, runtime_factory=FixtureRuntime)
    assert restored.status(p)['state'] == 'ready' and FixtureRuntime.calls == before
    path = next(m.root.glob('embeddinggemma-*')) / 'verified-fixture'
    path.unlink()
    restored.reconcile()
    assert restored.status(p)['state'] == 'not_downloaded'


def test_conflicts_token_validation_and_failed_download_are_private(runtime):
    m, p, _, _, admin, *_ = runtime
    for body in ({'version': True, 'token': 'hf_fixture_only'}, {'version': 0, 'token': 'contains spaces'}, {'version': 0, 'token': 'x', 'repository': 'other'}):
        response = admin.post('/api/v1/ai/runtime/download', json=body)
        assert response.status_code == 400
        assert 'hf_fixture_only' not in response.text
    current = begin(admin)
    assert admin.post('/api/v1/ai/runtime/download', json={'version': current['version'], 'token': 'hf_fixture_only'}).status_code == 409
    assert admin.post('/api/v1/ai/runtime/cancel', json={'version': 0}).status_code == 409
    m.cancel(p, current['version'])
    assert m.status(p)['operation_id'] is None


def test_expired_lease_can_recover_without_stale_activation(runtime):
    m, p, app, _, admin, *_ = runtime
    begin(admin)
    first = app.state.storage.ai_runtime_claim(time.time(), 1)
    second = app.state.storage.ai_runtime_claim(first['lease_until'] + .1, 60)
    # The recovered lease is in the future but includes the real wall clock.
    m._perform(first)
    assert app.state.storage.ai_runtime_local()['active_cache_id'] is None
    m._perform(second)
    assert m.status(p)['state'] == 'ready'


def test_interrupted_explicit_download_resumes_only_with_matching_intent_marker(runtime):
    m, p, app, cfg, admin, *_ = runtime
    begin(admin)
    resumed = EmbeddingInstallation(app.state.storage, Vault(cfg.secret), cfg,
        downloader=fixture_download, verifier=fixture_verify, runtime_factory=FixtureRuntime)
    assert resumed.status(p)['state'] == 'downloading'
    assert resumed.run_once()
    assert resumed.status(p)['state'] == 'ready'
    assert not list(resumed.root.glob('.download-*'))


def test_replacement_verification_uses_only_one_embedding_worker(runtime):
    m, p, _, _, admin, *_ = runtime
    begin(admin)
    m.run_once()
    m.embed_queries(['Load the current worker'])
    assert m._runtime is not None
    def replacement(path):
        assert m._runtime is None
        with pytest.raises(EmbeddingRuntimeError) as error:
            m.embed_queries(['Concurrent query'])
        assert error.value.code == 'busy'
        return FixtureRuntime(path)
    m.runtime_factory = replacement
    begin(admin)
    m.run_once()
    assert m.status(p)['state'] == 'ready'
    m.runtime_factory = FixtureRuntime
    assert m.embed_queries(['After replacement'])[0][0] == 1


def test_warm_cache_restore_abandons_unmarked_replacement_but_keeps_old_model(runtime):
    m, p, app, cfg, admin, *_ = runtime
    begin(admin)
    m.run_once()
    old = m.status(p)['active_cache_id']
    pending = begin(admin)
    (m.root / ('.download-' + pending['operation_id'])).unlink()
    restored = EmbeddingInstallation(app.state.storage, Vault(cfg.secret), cfg,
        downloader=lambda *a, **k: pytest.fail('Restoration must not download'),
        verifier=fixture_verify, runtime_factory=FixtureRuntime)
    state = restored.status(p)
    assert state['state'] == 'ready' and state['active_cache_id'] == old
    assert state['operation_id'] is None and not restored.run_once()
    assert restored.embed_queries(['Restored local model'])[0][0] == 1
    assert not list(app.state.storage.signing_secrets())


def test_gated_model_access_failure_is_actionable_and_private(runtime):
    m, p, _, _, admin, *_ = runtime
    def denied(*a, **k):
        raise ModelDownloadError('model_access_required')
    m.downloader = denied
    begin(admin)
    m.run_once()
    status = admin.get('/api/v1/ai/runtime').json()
    assert status['state'] == 'failed' and status['error'] == 'model_access_required'
    assert 'hf_fixture_only' not in str(status)


@pytest.mark.parametrize('condition', ['disk_full', 'cache_file', 'cache_symlink'])
def test_failed_preflight_never_queues_download(runtime, monkeypatch, condition):
    from types import SimpleNamespace
    m, p, _, _, admin, *_ = runtime
    if condition == 'disk_full':
        monkeypatch.setattr('feedback.ai_installation.shutil.disk_usage', lambda path: SimpleNamespace(free=0))
    elif condition == 'cache_file':
        m.root.write_text('keep unrelated file')
    else:
        m.root.symlink_to(m.root.parent, target_is_directory=True)
    response = admin.post('/api/v1/ai/runtime/download', json={'version': 0, 'token': 'hf_fixture_only'})
    assert response.status_code == 409
    assert response.json()['error']['code'] == ('ai_disk_space' if condition == 'disk_full' else 'ai_cache_unavailable')
    assert m.status(p)['version'] == 0 and not m.run_once()
