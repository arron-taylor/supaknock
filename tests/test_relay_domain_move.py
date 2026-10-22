import time
import pytest
from conftest import submission
from test_push import device
from feedback.storage.relay_domain_move import move_supaknock_relay, OLD_ORIGIN, NEW_ORIGIN


def test_offline_relay_move_preserves_enrollment_and_pending_delivery(workflow, monkeypatch):
    w = workflow
    s = w['app'].state.storage
    assert move_supaknock_relay(s) is False
    monkeypatch.setattr('feedback.push.relay_origin', lambda url, allow_internal: url)
    w['app'].state.push.transport = lambda url, path, credential, body, internal: {
        'installation_id': body['installation_id'], 'credential': 'sr_' + 'A' * 43}
    assert w['admin'].put('/api/v1/admin/push', json={'enabled': True, 'relay_url': OLD_ORIGIN}).status_code == 200
    device(w)
    submission(w['user'], w['b'])
    before = s.push_settings()
    jobs = s.push_deliveries(100, 0)
    installation_id = s.installation_id()
    assert move_supaknock_relay(s) is True
    assert s.push_settings() == {**before, 'relay_url': NEW_ORIGIN, 'version': before['version'] + 1}
    assert s.push_deliveries(100, 0) == jobs
    assert s.installation_id() == installation_id
    assert move_supaknock_relay(s) is False
    job = s.claim_push(time.time())
    assert job['relay_url'] == NEW_ORIGIN
    assert job['credential'] == before['credential']


def test_offline_move_refuses_other_relays(workflow, monkeypatch):
    from test_push import configure
    configure(workflow, monkeypatch)
    s = workflow['app'].state.storage
    before = s.push_settings()
    with pytest.raises(ValueError, match='only moves'):
        move_supaknock_relay(s)
    assert s.push_settings() == before
