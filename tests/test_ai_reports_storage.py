"""Real SQLite confirmation, guest identity and owner lifecycle boundaries."""
from copy import deepcopy
import json
import time
from uuid import uuid4

import pytest

from feedback.domain import Actor
from feedback.errors import Error
from feedback.storage.sqlite import SQLiteStorage
from test_ai_chat_isolation import ai_chat, configure, conversation, race, turn


@pytest.fixture
def reports(ai_chat):
    env, store = ai_chat, ai_chat['store']
    cfg = store.ai_settings(env['project'], env['admin'])
    store.ai_store_provider_evidence(env['project'], env['admin'], cfg['version'], cfg['credential_version'], cfg['settings']['model'], capabilities={
        'text_generation': True, 'tool_calling': True, 'streaming': False, 'structured_output': False})
    board = store.create_board(env['project'], {'identifier': 'features', 'name': 'Features', 'description': ''})
    flags = deepcopy(cfg['settings']['tools'])
    flags.update(create_feature_request=True, create_bug_report=True)
    configure(env, tools=flags, feature_board_id=board['id'], bug_board_id=board['id'], allow_guest_reports=True)
    env['board'] = board
    return env


def draft(env, kind='visitor', report_kind='feature'):
    customer, _, job, material = turn(env, conversation(env, kind))
    proposal = env['store'].ai_tool_prepare_report(job['id'], job['lease_token'], 'provider-call', report_kind, 'Requested change', 'Only the report, never a transcript.')
    return customer, job, material, proposal


def confirm(env, customer, proposal, client_id=None, *, store=None):
    return (store or env['store']).ai_proposal_confirm(customer['conversation']['id'], customer['principal'], proposal['id'], proposal['version'], proposal['draft_hash'], client_id or str(uuid4()))


def counts(store):
    with store.transaction() as c:
        return {table: c.execute('SELECT count(*) FROM ' + table).fetchone()[0] for table in ('users', 'requests', 'ai_guest_owners', 'ai_reports', 'ai_report_confirmations', 'request_events', 'admin_notifications', 'webhook_deliveries', 'push_deliveries')}


def notifications(env):
    store, admin = env['store'], env['admin']
    with store.transaction(True) as c:
        c.execute('UPDATE projects SET webhook_enabled=1,webhook_url=?,webhook_secret=?,webhook_events=? WHERE id=?',
                  ('https://receiver.example/events', 'protected-fixture', json.dumps(['request.created']), env['project']))
    store.push_prepare(admin['user_id'], admin['session_id'], 'https://relay.example', 'proof-fixture')
    store.push_enable(admin['user_id'], admin['session_id'], 1, 'protected-relay-fixture')
    store.register_device(admin['user_id'], admin['session_id'], str(uuid4()), 'protected-device-fixture', str(uuid4()),
        {'name': 'Phone', 'environment': 'sandbox', 'enabled': True, 'request_notifications': True, 'chat_notifications': False})


@pytest.mark.parametrize('kind', ['visitor', 'external', 'native'])
def test_prepare_does_not_create_identity_and_confirmation_has_exact_owner(reports, kind):
    env, store = reports, reports['store']
    customer = conversation(env, kind)
    customer, _, job, _ = turn(env, customer)
    before = counts(store)
    p = store.ai_tool_prepare_report(job['id'], job['lease_token'], 'first', 'feature', 'Title', 'Report only')
    assert counts(store) == before
    assert p['can_confirm'] and p['can_edit']
    assert p['board'] == {'id': env['board']['id'], 'name': 'Features'}
    assert store.ai_tool_prepare_report(job['id'], job['lease_token'], 'different-call', 'feature', 'Title', 'Report only')['id'] == p['id']
    with pytest.raises(Error):
        store.ai_tool_prepare_report(job['id'], job['lease_token'], 'different-call', 'feature', 'Changed', 'Report only')
    result = confirm(env, customer, p)
    assert result['moderation'] == 'pending' and result['public_url'] is None
    assert result['guest'] == (kind == 'visitor')
    assert result['can_edit'] and result['can_delete']
    with store.transaction() as c:
        r = c.execute('SELECT * FROM requests WHERE id=?', (result['request_id'],)).fetchone()
        owner = c.execute('SELECT * FROM chat_customers WHERE id=(SELECT customer_id FROM chat_conversations WHERE id=?)', (customer['conversation']['id'],)).fetchone()
        event = c.execute("SELECT * FROM request_events WHERE request_id=? AND type='request.created'", (r['id'],)).fetchone()
        payload = json.loads(event['payload'])
        assert payload['actor']['kind'] == {'visitor': 'guest', 'external': 'integration', 'native': 'native'}[kind]
        assert r['submitted_ip_address'] == '203.0.113.8' and event['ip_address'] == '203.0.113.8'
        assert '203.0.113.8' not in event['payload']
        if kind == 'visitor':
            assert owner['user_id'] is None and owner['kind'] == 'anonymous'
            assert r['creator_id'] != owner['id']
            assert c.execute('SELECT user_id FROM ai_guest_owners WHERE customer_id=?', (owner['id'],)).fetchone()[0] == r['creator_id']
            assert not c.execute('SELECT 1 FROM local_credentials WHERE user_id=?', (r['creator_id'],)).fetchone()
            assert not c.execute('SELECT 1 FROM external_identities WHERE user_id=?', (r['creator_id'],)).fetchone()
        else:
            assert r['creator_id'] == owner['user_id']
        if kind == 'external':
            assert payload['creator']['external_user_id'] == ' exact external account '
    assert not {'creator_id', 'customer_id', 'guest_session_id', 'ip_address', 'external_user_id'} & set(result)


def test_exact_concurrent_confirmations_create_one_request_owner_event_and_receipt(reports):
    env, store = reports, reports['store']
    notifications(env)
    customer, _, _, p = draft(env)
    before, key = counts(store), str(uuid4())
    results = race(env, lambda s, _: confirm(env, customer, p, key, store=s))
    assert all(value == results[0] for value in results)
    after = counts(store)
    for table in ('users', 'requests', 'ai_guest_owners', 'ai_reports', 'ai_report_confirmations', 'request_events', 'admin_notifications', 'webhook_deliveries', 'push_deliveries'):
        assert after[table] == before[table] + 1
    assert confirm(env, customer, p) == results[0]  # Different delivery key cannot duplicate exact confirmation.
    changed_ip = customer | {'principal': customer['principal'] | {'ip_address': '198.51.100.4'}}
    assert confirm(env, changed_ip, p, key) == results[0]
    assert store.request(results[0]['request_id'])['submitted_ip_address'] == '203.0.113.8'
    with pytest.raises(Error):
        store.ai_proposal_confirm(customer['conversation']['id'], customer['principal'], p['id'], p['version'], '0' * 64, key)


def test_confirmation_event_failure_rolls_back_owner_request_receipt_and_notifications(reports, monkeypatch):
    env, store = reports, reports['store']
    notifications(env)
    customer, _, _, p = draft(env)
    before, original = counts(store), store._persist_event
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('fixture rollback')
    monkeypatch.setattr(store, '_persist_event', fail)
    with pytest.raises(RuntimeError):
        confirm(env, customer, p)
    assert counts(store) == before
    assert store.ai_proposal(customer['conversation']['id'], customer['principal'], p['id']) == p


def test_edit_exact_draft_and_successful_ai_acknowledgment_can_be_confirmed(reports):
    env, store = reports, reports['store']
    customer, job, material, p = draft(env)
    cid, principal = customer['conversation']['id'], customer['principal']
    assert store.ai_commit_reply(job['id'], job['lease_token'], 'Review your draft before submitting.', [], material['job']['authority']['knowledge_version'])
    edited = store.ai_proposal_update(cid, principal, p['id'], p['version'], 'Customer corrected title', 'Customer reviewed body')
    assert edited['version'] == p['version'] + 1 and edited['draft_hash'] != p['draft_hash']
    assert edited['expires_at'] == p['expires_at']
    with pytest.raises(Error):
        confirm(env, customer, p)
    assert confirm(env, customer, edited)['title'] == 'Customer corrected title'


@pytest.mark.parametrize('change', ['off_on', 'tool', 'guest', 'mode', 'close', 'message', 'origin', 'revoke', 'expire', 'destination'])
def test_stale_or_revoked_guest_confirmation_never_creates_owner(reports, change):
    env, store = reports, reports['store']
    customer, _, _, p = draft(env)
    cid, principal = customer['conversation']['id'], customer['principal']
    if change == 'off_on':
        configure(env, enabled=False)
        configure(env, enabled=True)
    elif change == 'tool':
        flags = store.ai_settings(env['project'], env['admin'])['settings']['tools']
        flags['create_feature_request'] = False
        configure(env, tools=flags)
    elif change == 'guest':
        configure(env, allow_guest_reports=False)
    elif change == 'mode':
        store.chat_support_mode(cid, principal, 'waiting_human', customer['conversation']['mode_version'])
    elif change == 'close':
        store.chat_change_state(cid, env['admin'], 'closed', store.chat_conversation(cid, env['admin'])['version'])
    elif change == 'message':
        store.chat_send(cid, principal, str(uuid4()), 'Actually, do something else')
    elif change == 'origin':
        principal['origin'] = 'https://other.example'
    elif change == 'revoke':
        store.chat_revoke_customer_session(principal)
    elif change == 'expire':
        with store.transaction(True) as c:
            c.execute('UPDATE ai_proposals SET expires_at=? WHERE id=?', (time.time() - 1, p['id']))
    else:
        configure(env, feedback_url='https://customer.example/feedback?mode=features#submit')
    with pytest.raises(Error):
        confirm(env, customer, p)
    with store.transaction() as c:
        assert c.execute('SELECT count(*) FROM ai_guest_owners').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM ai_reports').fetchone()[0] == 0


def test_guest_off_draft_has_notice_and_current_custom_destination_without_submission(reports):
    env, store = reports, reports['store']
    configure(env, allow_guest_reports=False, feedback_url='https://customer.example/feedback', feature_feedback_url='https://customer.example/features?tab=requests#new')
    customer, _, _, p = draft(env)
    assert not p['can_confirm'] and p['can_edit']
    assert p['feedback_url'] == 'https://customer.example/features?tab=requests#new'
    assert "won't appear in your account" in p['tracking_notice']
    with pytest.raises(Error):
        confirm(env, customer, p)
    cancelled = store.ai_proposal_cancel(customer['conversation']['id'], customer['principal'], p['id'], p['version'])
    assert cancelled['state'] == 'cancelled' and not cancelled['can_confirm']


@pytest.mark.parametrize('kind', ['visitor', 'external', 'native'])
def test_committed_owner_rules_survive_flags_handoff_rejection_and_backup(reports, tmp_path, kind):
    env, store = reports, reports['store']
    customer, _, _, p = draft(env, kind)
    report = confirm(env, customer, p)
    cid, principal = customer['conversation']['id'], customer['principal']
    configure(env, enabled=False, allow_guest_reports=False, feedback_url='https://customer.example/current')
    cv = store.chat_conversation(cid, principal)
    store.chat_support_mode(cid, principal, 'waiting_human', cv['mode_version'])
    edited = store.ai_report_update(cid, principal, report['id'], report['version'], 'Revised report', 'Corrected body')
    assert edited['can_edit'] and edited['feedback_url'] == 'https://customer.example/current'
    assert store.ai_report_update(cid, principal, edited['id'], edited['version'], edited['title'], edited['description']) == edited
    admin = Actor(user_id=env['admin']['user_id'], role='admin')
    rejected = store.moderate_request(report['request_id'], 'reject', 'More detail', edited['version'], admin)
    resubmitted = store.ai_report_update(cid, principal, report['id'], rejected['version'], edited['title'], edited['description'])
    assert resubmitted['moderation'] == 'pending' and resubmitted['version'] == rejected['version'] + 1
    path = tmp_path / 'reports-backup.sqlite'
    store.backup(path)
    restored = SQLiteStorage(path, store.base_url)
    assert restored.ai_report(cid, principal, report['id']) == resubmitted
    deleted = restored.ai_report_delete(cid, principal, report['id'], resubmitted['version'])
    assert deleted['deleted'] and not deleted['can_edit'] and not deleted['can_delete']
    assert restored.ai_report_delete(cid, principal, report['id'], resubmitted['version']) == deleted
    assert confirm(env, customer, p, store=restored) == deleted
    assert restored.ai_reports(cid, principal)['items'] == [deleted]


def test_acceptance_permanently_locks_creator_and_custom_link_keeps_priority(reports):
    env, store = reports, reports['store']
    configure(env, feedback_url='https://customer.example/feedback')
    customer, _, _, p = draft(env)
    report = confirm(env, customer, p)
    admin = Actor(user_id=env['admin']['user_id'], role='admin')
    accepted = store.moderate_request(report['request_id'], 'accept', None, report['version'], admin)
    cid, principal = customer['conversation']['id'], customer['principal']
    receipt = store.ai_report(cid, principal, report['id'])
    assert receipt['feedback_url'] == 'https://customer.example/feedback' and receipt['public_url'] is None
    assert not receipt['can_edit'] and not receipt['can_delete']
    for status in ('planned', 'in_progress', 'shipped'):
        accepted = store.moderate_request(report['request_id'], 'status', status, accepted['version'], admin)
        with pytest.raises(Error):
            store.ai_report_update(cid, principal, report['id'], accepted['version'], 'No', 'No')
        with pytest.raises(Error):
            store.ai_report_delete(cid, principal, report['id'], accepted['version'])
    configure(env, feedback_url=None)
    assert store.ai_report(cid, principal, report['id'])['public_url'].endswith('/requests/' + report['request_id'])


def test_guest_mapping_cannot_acquire_credentials_or_votes_or_admin_privilege(reports):
    env, store = reports, reports['store']
    customer, _, _, p = draft(env)
    report = confirm(env, customer, p)
    with store.transaction() as c:
        uid = c.execute('SELECT creator_id FROM ai_reports WHERE proposal_id=?', (p['id'],)).fetchone()[0]
    statements = [
        ("INSERT INTO local_credentials VALUES(?,'fake@example.com','hash','now')", (uid,)),
        ("INSERT INTO external_identities VALUES(?,?,?,?,'now')", (str(uuid4()), env['project'], 'fake external', uid)),
        ("INSERT INTO sessions(id,user_id,token_hash,csrf_hash,created_at,expires_at) VALUES(?,?,?,?,'now',?)", (str(uuid4()), uid, str(uuid4()), 'csrf', time.time() + 3600)),
        ("UPDATE users SET role='admin' WHERE id=?", (uid,)),
        ("INSERT INTO votes VALUES(?,?,'now')", (report['request_id'], uid)),
    ]
    for statement, parameters in statements:
        with pytest.raises(Error), store.transaction(True) as c:
            c.execute(statement, parameters)


def test_distinct_guest_and_native_account_cannot_read_guest_report(reports):
    env, store = reports, reports['store']
    customer, _, _, p = draft(env)
    report = confirm(env, customer, p)
    for kind in ('visitor', 'native', 'external'):
        other = conversation(env, kind)
        with pytest.raises(Error):
            store.ai_report(customer['conversation']['id'], other['principal'], report['id'])
        with pytest.raises(Error):
            store.ai_report(other['conversation']['id'], other['principal'], report['id'])
    store.chat_revoke_customer_session(customer['principal'])
    with pytest.raises(Error):
        store.ai_report(customer['conversation']['id'], customer['principal'], report['id'])


def test_guest_daily_limit_is_durable_and_drafts_do_not_bypass_it(reports):
    env, store = reports, reports['store']
    customer = conversation(env)
    first_owner = None
    for index in range(6):
        customer, _, job, _ = turn(env, customer)
        p = store.ai_tool_prepare_report(job['id'], job['lease_token'], 'call', 'feature', 'Report ' + str(index), 'Description')
        if index == 5:
            with pytest.raises(Error) as caught:
                confirm(env, customer, p)
            assert caught.value.status == 429
        else:
            report = confirm(env, customer, p)
            owner = store.request(report['request_id'])['creator_id']
            first_owner = first_owner or owner
            assert owner == first_owner
            store.ai_report_delete(customer['conversation']['id'], customer['principal'], report['id'], report['version'])
    with store.transaction() as c:
        assert c.execute('SELECT count(*) FROM ai_guest_owners').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM ai_reports').fetchone()[0] == 5


def test_duplicate_confirmation_alias_is_bound_and_cannot_submit_another_draft(reports):
    env, store = reports, reports['store']
    customer, _, _, p = draft(env)
    original = confirm(env, customer, p)
    alias = str(uuid4())
    assert confirm(env, customer, p, alias) == original
    customer, _, job, _ = turn(env, customer)
    another = store.ai_tool_prepare_report(job['id'], job['lease_token'], 'next', 'bug', 'Different report', 'Different details')
    with pytest.raises(Error):
        confirm(env, customer, another, alias)
    assert len(store.ai_reports(customer['conversation']['id'], customer['principal'])['items']) == 1


@pytest.mark.parametrize('kind', ['external', 'native'])
def test_authenticated_renewal_keeps_report_access_but_original_revocation_fences_pending_draft(reports, kind):
    env, store = reports, reports['store']
    customer, _, _, p = draft(env, kind)
    report = confirm(env, customer, p)
    customer, _, job, _ = turn(env, customer)
    pending = store.ai_tool_prepare_report(job['id'], job['lease_token'], 'next', 'bug', 'Pending report', 'Details')
    token_hash = str(uuid4())
    store.chat_create_customer_session(env['project'], customer['actor'], ' exact external account ' if kind == 'external' else None,
        token_hash, time.time() + 3600, customer['principal']['origin'], customer['parent'])
    renewed = store.chat_customer_session(token_hash, customer['principal']['origin'])
    assert store.ai_report(customer['conversation']['id'], renewed, report['id']) == report
    store.chat_revoke_customer_session(customer['principal'])
    assert store.ai_report(customer['conversation']['id'], renewed, report['id']) == report
    with pytest.raises(Error):
        confirm(env, customer | {'principal': renewed}, pending)
    if kind == 'external':
        with store.transaction(True) as c:
            c.execute("UPDATE api_keys SET revoked_at='now' WHERE id=?", (customer['actor'].key_id,))
    else:
        with store.transaction(True) as c:
            c.execute("UPDATE sessions SET revoked_at='now' WHERE id=?", (customer['parent'],))
    with pytest.raises(Error):
        store.ai_report(customer['conversation']['id'], renewed, report['id'])


def test_confirmed_guest_history_survives_credential_cleanup_without_recovery(reports, tmp_path):
    env, store = reports, reports['store']
    customer, _, _, p = draft(env)
    report = confirm(env, customer, p)
    with store.transaction(True) as c:
        c.execute('DELETE FROM chat_sessions WHERE id=?', (customer['principal']['session_id'],))
    path = tmp_path / 'guest-cleanup.sqlite'
    store.backup(path)
    restored = SQLiteStorage(path, store.base_url)
    with pytest.raises(Error):
        restored.ai_report(customer['conversation']['id'], customer['principal'], report['id'])
    assert restored.request(report['request_id'])['title'] == report['title']
    with restored.transaction() as c:
        assert c.execute('SELECT count(*) FROM ai_guest_owners').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM ai_reports').fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM request_events WHERE request_id=? AND type='request.created'", (report['request_id'],)).fetchone()[0] == 1
