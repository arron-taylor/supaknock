"""Specific administrator generation prerequisites against real SQLite."""
from uuid import uuid4
import pytest

from feedback.errors import Error
from test_ai_storage import ai_store, ready_project


def test_readiness_reports_prerequisites_without_enabling_ai(ai_store):
    s, p, pid, _ = ai_store
    view = s.ai_settings(pid, p)
    assert set(view['readiness']['issues']) == {'answer_key_missing', 'answer_model_missing', 'embedding_model_not_ready', 'knowledge_not_ready'}
    assert not view['readiness']['ready']
    cfg, source, _ = ready_project(s, p, pid)
    assert cfg['readiness'] == {'ready': True, 'issues': [], 'knowledge_ready':True, 'tools_ready':False, 'source_mode':'knowledge'}
    assert not cfg['settings']['enabled']
    run = s.ai_authoring_begin(pid, p, str(uuid4()), 'sections', cfg['version'], source_id=source['id'], source_version=source['version'])
    assert run['status'] == 'queued'
    assert not s.ai_settings(pid, p)['settings']['enabled']


def test_unavailable_model_has_specific_generation_error(ai_store):
    s, p, pid, _ = ai_store
    cfg, _, _ = ready_project(s, p, pid)
    changed = s.ai_configure(pid, p, cfg['version'], cfg['settings'] | {'model': 'unverified'})
    assert 'answer_model_unavailable' in changed['readiness']['issues']
    with pytest.raises(Error, match='Refresh the selected answer') as error:
        s.ai_authoring_begin(pid, p, str(uuid4()), 'preview', changed['version'], query='How do backups work?')
    assert error.value.code == 'ai_not_ready'


def test_catalogue_model_requires_verification_before_generating(ai_store):
    s, p, pid, _ = ai_store
    cfg, source, _ = ready_project(s, p, pid)
    s.ai_store_provider_evidence(pid, p, cfg['version'], cfg['credential_version'], cfg['settings']['model'], catalogue=[cfg['settings']['model'], 'gpt-6-luna'])
    cfg = s.ai_configure(pid, p, cfg['version'], cfg['settings'] | {'model': 'gpt-6-luna'})
    assert cfg['readiness']['issues'] == ['answer_model_unverified']
    with pytest.raises(Error, match='Verify the selected answer model') as error:
        s.ai_authoring_begin(pid, p, str(uuid4()), 'sections', cfg['version'], source_id=source['id'], source_version=source['version'])
    assert error.value.code == 'ai_not_ready'


def test_source_approval_and_stale_versions_are_specific(ai_store):
    s, p, pid, _ = ai_store
    cfg, source, _ = ready_project(s, p, pid)
    source = s.ai_source_update(pid, p, source['id'], source['version'], active=True, approved=False)
    assert s.ai_knowledge_source(pid, p, source['id'])['index_ready']
    with pytest.raises(Error, match='Review the original source and approve') as error:
        s.ai_authoring_begin(pid, p, str(uuid4()), 'sections', cfg['version'], source_id=source['id'], source_version=source['version'])
    assert error.value.code == 'ai_not_ready'
    with pytest.raises(Error) as error:
        s.ai_authoring_begin(pid, p, str(uuid4()), 'preview', cfg['version'] - 1, query='Backups?')
    assert error.value.code == 'conflict'
