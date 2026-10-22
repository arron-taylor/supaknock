"""Exact customer confirmation, navigation and ownership through real HTTP/SQLite."""
from concurrent.futures import ThreadPoolExecutor
import json
from uuid import uuid4

import pytest

from feedback.ai_providers import GenerationResult, ToolCall, Usage
from feedback.security import digest
from test_ai_authoring import authoring
from test_ai_tools import enable_tools
from test_ai_chat_isolation import configure
from test_chat import ORIGIN, enable, visitor, send
from test_chat_customers import customer as authenticated_customer, conversation as authenticated_conversation


@pytest.fixture
def reports(authoring):
    ai = authoring
    enable(ai)
    env = {'store': ai['s'], 'project': ai['p']['id'], 'admin': ai['principal'], 'source': ai['source']}
    board = enable_tools(env, ['create_feature_request', 'create_bug_report'], guests=True)
    configure(env, enabled=True)
    return ai | {'env': env, 'report_board': board}


def customer(ai, kind='visitor'):
    if kind == 'visitor':
        client, cv, token = visitor(ai)
    else:
        client, data = authenticated_customer(ai, external=' Exact 0001 ' if kind == 'external' else None,
                                               issuer=ai['trusted'] if kind == 'external' else ai['user'])
        cv, token = authenticated_conversation(client), data['token']
    result = client.put('/api/v1/chat/conversations/' + cv['id'] + '/support-mode', json={
        'mode': 'ai', 'mode_version': cv['mode_version']})
    assert result.status_code == 200, result.text
    return client, result.json(), token


def prepare(ai, client, cv, *, kind='feature', title='Requested improvement', description='Please add an export option.'):
    name = 'create_feature_request' if kind == 'feature' else 'create_bug_report'
    ai['fake'].responses = [GenerationResult('', (ToolCall('draft-call', name, {
        'title': title, 'description': description}),), Usage(7, 5), 'tool_calls')]
    assert send(client, cv, 'Please prepare the requested feedback').status_code == 201
    assert ai['app'].state.ai_chat_worker.run_once()
    prefix = '/api/v1/chat/conversations/' + cv['id']
    response = client.get(prefix + '/proposals')
    assert response.status_code == 200, response.text
    drafts = response.json()['items']
    draft = next(row for row in drafts if row['state'] == 'pending')
    return draft, prefix + '/proposals/' + draft['id']


def confirmation(draft):
    return {'version': draft['version'], 'draft_hash': draft['draft_hash'], 'client_request_id': str(uuid4())}


def test_guest_draft_edit_and_exact_confirmation_are_explicit_and_private(reports):
    ai, store = reports, reports['s']
    client, cv, token = customer(ai)
    draft, url = prepare(ai, client, cv)
    assert draft['guest'] and draft['can_confirm'] and draft['can_edit']
    assert draft['board'] == {'id': ai['report_board']['id'], 'name': 'Feedback'}
    assert "won't appear in your account" in draft['tracking_notice']
    assert draft['feedback_url'].endswith('/boards/' + ai['report_board']['id'])
    assert draft['copy_text'] == draft['title'] + '\n\n' + draft['description']
    assert client.get(url).json() == draft
    with store.transaction() as c:
        assert c.execute('SELECT count(*) FROM requests').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM ai_guest_owners').fetchone()[0] == 0
    old = confirmation(draft)
    edited = client.put(url, json={'version': draft['version'], 'title': 'Edited exact title', 'description': 'Only this exact edited description.'})
    assert edited.status_code == 200, edited.text
    draft = edited.json()
    assert draft['version'] == old['version'] + 1 and draft['draft_hash'] != old['draft_hash']
    assert client.post(url + '/confirm', json=old).status_code == 409
    assert client.post(url + '/confirm', json=confirmation(draft) | {'draft_hash': old['draft_hash']}).status_code == 409
    body = confirmation(draft)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.post(url + '/confirm', json=body), range(4)))
    assert all(response.status_code == 200 for response in responses)
    receipts = [response.json() for response in responses]
    assert len({receipt['request_id'] for receipt in receipts}) == 1
    receipt = receipts[0]
    assert receipt['title'] == 'Edited exact title' and receipt['moderation'] == 'pending'
    assert receipt['guest'] and receipt['can_edit'] and receipt['public_url'] is None
    assert ai['anon'].get('/api/v1/requests/' + receipt['request_id']).status_code == 404
    assert ai['user'].get('/api/v1/requests/' + receipt['request_id']).status_code == 404
    with store.transaction() as c:
        owner = c.execute('SELECT user_id FROM ai_guest_owners').fetchone()[0]
        assert c.execute('SELECT count(*) FROM requests').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM request_events WHERE request_id=? AND type=\'request.created\'', (receipt['request_id'],)).fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM local_credentials WHERE user_id=?', (owner,)).fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM external_identities WHERE user_id=?', (owner,)).fetchone()[0] == 0
        assert c.execute('SELECT user_id FROM chat_customers WHERE id=?', (cv['customer']['id'],)).fetchone()[0] is None
    for response in (draft, receipt):
        assert not any(key in response for key in ('user_id', 'customer_id', 'session_id', 'external_user_id', 'ip_address', 'authority', 'protected_key'))
        assert token not in str(response)


def test_guest_off_still_offers_draft_copy_and_configured_page_without_submission(reports):
    ai = reports
    destination = 'https://support.customer.example/ideas?category=feature#submit'
    configure(ai['env'], allow_guest_reports=False, feedback_url='https://support.customer.example/feedback',
              feature_feedback_url=destination)
    client, cv, _ = customer(ai)
    draft, url = prepare(ai, client, cv)
    assert not draft['can_confirm'] and draft['can_edit']
    assert draft['tracking_notice'] and draft['feedback_url'] == destination and draft['copy_text']
    result = client.post(url + '/confirm', json=confirmation(draft))
    assert result.status_code == 403 and result.json()['error']['code'] == 'guest_reports_disabled'
    configure(ai['env'], allow_guest_reports=True)
    assert client.post(url + '/confirm', json=confirmation(draft)).status_code == 409
    with ai['s'].transaction() as c:
        assert c.execute('SELECT count(*) FROM ai_guest_owners').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM requests').fetchone()[0] == 0


@pytest.mark.parametrize('kind', ['feature', 'bug'])
def test_custom_destination_is_preserved_after_submission_and_public_acceptance(reports, kind):
    ai = reports
    destination = 'https://customer.example/' + kind + '?view=feedback#track'
    configure(ai['env'], feedback_url='https://customer.example/general', **{kind + '_feedback_url': destination})
    client, cv, _ = customer(ai)
    draft, url = prepare(ai, client, cv, kind=kind)
    receipt = client.post(url + '/confirm', json=confirmation(draft)).json()
    assert draft['feedback_url'] == receipt['feedback_url'] == destination
    accepted = ai['admin'].post('/api/v1/requests/' + receipt['request_id'] + '/accept', json={'version': receipt['version']})
    assert accepted.status_code == 200, accepted.text
    report_url = '/api/v1/chat/conversations/' + cv['id'] + '/reports/' + receipt['id']
    current = client.get(report_url).json()
    assert current['moderation'] == 'accepted' and current['feedback_url'] == destination
    assert current['public_url'] is None and not current['can_edit'] and not current['can_delete']
    progressed = ai['admin'].put('/api/v1/requests/' + receipt['request_id'] + '/status', json={'version': current['version'], 'status': 'planned'})
    assert progressed.status_code == 200, progressed.text
    still_locked = client.get(report_url).json()
    assert still_locked['accepted_at'] == current['accepted_at']
    assert client.put(report_url, json={'version': still_locked['version'], 'title': 'Forbidden edit', 'description': ''}).status_code == 409
    assert client.delete(report_url, params={'version': still_locked['version']}).status_code == 409


def test_existing_report_owner_edits_and_deletes_survive_disabled_creation_and_handoff(reports):
    ai = reports
    client, cv, _ = customer(ai)
    draft, url = prepare(ai, client, cv)
    receipt = client.post(url + '/confirm', json=confirmation(draft)).json()
    current_cv = client.get('/api/v1/chat/conversations/' + cv['id']).json()
    assert client.put('/api/v1/chat/conversations/' + cv['id'] + '/support-mode', json={
        'mode': 'waiting_human', 'mode_version': current_cv['mode_version']}).status_code == 200
    cfg = ai['s'].ai_settings(ai['p']['id'], ai['principal'])
    configure(ai['env'], enabled=False, allow_guest_reports=False, tools={key: False for key in cfg['settings']['tools']})
    report_url = '/api/v1/chat/conversations/' + cv['id'] + '/reports/' + receipt['id']
    edited = client.put(report_url, json={'version': receipt['version'], 'title': 'Owner correction', 'description': 'Updated before acceptance'})
    assert edited.status_code == 200, edited.text
    assert client.get(report_url).json()['title'] == 'Owner correction'
    deleted = client.delete(report_url, params={'version': edited.json()['version']})
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()['deleted'] and deleted.json()['moderation'] is None
    assert not deleted.json()['can_edit'] and not deleted.json()['can_delete']
    assert client.get(report_url).json() == deleted.json()


@pytest.mark.parametrize('kind', ['external', 'native'])
def test_authenticated_customer_report_uses_existing_identity_without_guest_owner(reports, kind):
    ai, store = reports, reports['s']
    configure(ai['env'], allow_guest_reports=False)
    client, cv, token = customer(ai, kind)
    draft, url = prepare(ai, client, cv)
    assert not draft['guest'] and draft['can_confirm'] and draft['tracking_notice'] is None
    receipt = client.post(url + '/confirm', json=confirmation(draft))
    assert receipt.status_code == 200, receipt.text
    principal = store.chat_customer_session(digest(token), None)
    with store.transaction() as c:
        owner = c.execute('SELECT creator_id FROM requests WHERE id=?', (receipt.json()['request_id'],)).fetchone()[0]
        assert owner == principal['user_id']
        assert c.execute('SELECT count(*) FROM ai_guest_owners').fetchone()[0] == 0


def test_report_routes_require_owned_chat_credential_and_narrow_cors(reports):
    ai = reports
    client, cv, token = customer(ai)
    draft, url = prepare(ai, client, cv)
    stranger, _, _ = visitor(ai)
    assert stranger.get(url).status_code == 404
    assert stranger.post(url + '/confirm', json=confirmation(draft)).status_code == 404
    for actor in ('admin', 'user', 'trusted', 'manager', 'anon'):
        assert ai[actor].get(url).status_code in (401, 403)
        assert ai[actor].post(url + '/confirm', json=confirmation(draft)).status_code in (401, 403)
    assert client.post(url + '/confirm', json=confirmation(draft) | {'user_id': str(uuid4())}).status_code == 400
    assert client.put(url, json={'version': True, 'title': 'Boolean version', 'description': ''}).status_code == 400
    for target in (url, '/api/v1/chat/conversations/' + cv['id'] + '/reports/' + str(uuid4())):
        preflight = client.options(target, headers={'Access-Control-Request-Method': 'PUT',
                                                   'Access-Control-Request-Headers': 'Content-Type,X-Chat-Token'})
        assert preflight.status_code == 204 and 'PUT' in preflight.headers['access-control-allow-methods']
        denied = client.options(target, headers={'Origin': 'https://attacker.example', 'Access-Control-Request-Method': 'PUT'})
        assert denied.status_code == 403
    assert client.get(url, headers={'Origin': 'https://attacker.example'}).status_code == 403
    ai['s'].chat_revoke_customer_session(ai['s'].chat_visitor(digest(token), ORIGIN))
    assert client.get(url).status_code in (401, 404)


@pytest.mark.parametrize('resource', ['proposal', 'report'])
@pytest.mark.parametrize('escaped', [False, True], ids=['utf8', 'escaped-json'])
def test_declared_report_edit_unicode_limit_is_reachable_without_widening_chat(reports, resource, escaped):
    ai = reports
    client, cv, _ = customer(ai)
    draft, url = prepare(ai, client, cv)
    version = draft['version']
    if resource == 'report':
        receipt = client.post(url + '/confirm', json=confirmation(draft))
        assert receipt.status_code == 200, receipt.text
        version = receipt.json()['version']
        url = '/api/v1/chat/conversations/' + cv['id'] + '/reports/' + receipt.json()['id']
    description = '🪐' * 20000
    payload = {'version': version, 'title': 'A customer-authored report', 'description': description}
    response = client.put(url, content=json.dumps(payload, ensure_ascii=escaped), headers={'Content-Type': 'application/json'})
    assert response.status_code == 200, response.text
    assert response.json()['description'] == description
    too_long = payload | {'version': response.json()['version'], 'description': description + 'x'}
    assert client.put(url, json=too_long).status_code == 400
    assert send(client, cv, description).status_code == 413
