"""Widget recovery contracts over real HTTP, WebSocket and SQLite boundaries."""
from uuid import uuid4

import pytest

from test_ai_authoring import authoring
from test_ai_reports_api import reports, customer, prepare, confirmation
from test_ai_chat_isolation import configure
from test_chat import ORIGIN, send
from test_chat_websocket import frame, ticket


def test_draft_and_confirmed_receipt_recover_beyond_the_message_cursor(reports):
    ai = reports
    client, cv, token = customer(ai)
    prefix = '/api/v1/chat/conversations/' + cv['id']
    with client.websocket_connect('/api/v1/chat/ws', headers={'Origin': ORIGIN}) as ws:
        ws.send_json({'ticket': ticket(client, cv), 'after': 0})
        assert frame(ws, 'ready')['conversation_id'] == cv['id']
        assert frame(ws, 'state')['conversation']['support_mode'] == 'ai'
        draft, url = prepare(ai, client, cv)
        customer_message = frame(ws, 'message')['message']
        acknowledgement = frame(ws, 'message')['message']
        assert customer_message['sender_role'] == 'customer'
        assert acknowledgement['sender_role'] == 'ai'
        assert 'Nothing has been submitted.' in acknowledgement['body']
        assert draft['message_sequence'] == acknowledgement['sequence']
        assert client.get(prefix + '/proposals').json()['items'][0]['id'] == draft['id']
        assert client.get(prefix + '/reports').json()['items'] == []

    # A reconnect may already have acknowledged every message. Its ready/state
    # frames still let the widget reload private cards independently of history.
    with client.websocket_connect('/api/v1/chat/ws', headers={'Origin': ORIGIN}) as ws:
        ws.send_json({'ticket': ticket(client, cv), 'after': acknowledgement['sequence']})
        assert frame(ws, 'ready')['conversation_id'] == cv['id']
        current = frame(ws, 'state')['conversation']
        assert current['last_sequence'] == acknowledgement['sequence']
        recovered = client.get(prefix + '/proposals').json()['items']
        assert len(recovered) == 1 and recovered[0] == draft
        body = confirmation(draft)
        receipt = client.post(url + '/confirm', json=body).json()
        assert receipt['id'] == draft['id'] and receipt['moderation'] == 'pending'
        assert receipt['message_sequence'] == draft['message_sequence']

    with client.websocket_connect('/api/v1/chat/ws', headers={'Origin': ORIGIN}) as ws:
        ws.send_json({'ticket': ticket(client, cv), 'after': acknowledgement['sequence']})
        frame(ws, 'ready')
        assert frame(ws, 'state')['conversation']['last_sequence'] == acknowledgement['sequence']
        assert client.get(prefix + '/reports').json()['items'] == [receipt]
        assert client.get(prefix + '/proposals').json()['items'][0]['state'] == 'confirmed'
        assert client.post(url + '/confirm', json=body).json() == receipt
        assert client.get(prefix + '/messages', params={'after': acknowledgement['sequence']}).json()['items'] == []
    assert len(ai['fake'].calls) == 1
    assert token not in str(receipt)


def test_report_refresh_observes_current_destination_acceptance_and_lifecycle_without_new_chat(reports):
    ai = reports
    client, cv, _ = customer(ai)
    prefix = '/api/v1/chat/conversations/' + cv['id']
    draft, url = prepare(ai, client, cv)
    receipt = client.post(url + '/confirm', json=confirmation(draft)).json()
    sequence = client.get(prefix).json()['last_sequence']
    destination = 'https://feedback.customer.example/features?from=widget#status'
    configure(ai['env'], enabled=False, allow_guest_reports=False, feature_feedback_url=destination)
    accepted = ai['admin'].post('/api/v1/requests/' + receipt['request_id'] + '/accept', json={'version': receipt['version']})
    assert accepted.status_code == 200, accepted.text
    moved = ai['admin'].put('/api/v1/requests/' + receipt['request_id'] + '/status', json={
        'version': accepted.json()['version'], 'status': 'planned'})
    assert moved.status_code == 200, moved.text
    current = client.get(prefix + '/reports').json()['items'][0]
    assert current['moderation'] == 'accepted' and current['status'] == 'planned'
    assert current['message_sequence'] == receipt['message_sequence']
    assert current['accepted_at'] and not current['can_edit'] and not current['can_delete']
    assert current['feedback_url'] == destination and current['public_url'] is None
    assert client.get(prefix + '/reports/' + receipt['id']).json() == current
    assert client.get(prefix).json()['last_sequence'] == sequence
    assert client.get(prefix + '/messages', params={'after': sequence}).json()['items'] == []
    assert len(ai['fake'].calls) == 1


@pytest.mark.parametrize('change', ['disabled', 'knowledge_removed'])
def test_stale_public_readiness_cannot_start_ai_and_preserves_explicit_human_access(reports, change):
    ai = reports
    client, cv, _ = customer(ai)
    prefix = '/api/v1/chat/conversations/' + cv['id']
    config_url = '/api/v1/chat/projects/' + ai['p']['id'] + '/config'
    assert client.get(config_url).json()['ai_available']
    draft, url = prepare(ai, client, cv)
    if change == 'disabled':
        configure(ai['env'], enabled=False)
    else:
        source = ai['s'].ai_knowledge_source(ai['p']['id'], ai['principal'], ai['source']['id'])
        ai['s'].ai_knowledge_change(ai['p']['id'], ai['principal'], source['id'], source['version'], active=False, approved=False)
    current_config = client.get(config_url).json()
    assert not current_config['ai_available']
    assert set(current_config) == {'project', 'name', 'enabled', 'ai_available', 'response_time', 'business_hours'}
    stale = client.get(url).json()
    assert not stale['can_confirm']
    assert client.post(url + '/confirm', json=confirmation(draft)).status_code == 409
    current = client.get(prefix).json()
    human = client.put(prefix + '/support-mode', json={'mode': 'waiting_human', 'mode_version': current['mode_version']})
    assert human.status_code == 200, human.text
    assert human.json()['support_mode'] == 'waiting_human'
    rejected = client.put(prefix + '/support-mode', json={'mode': 'ai', 'mode_version': human.json()['mode_version']})
    assert rejected.status_code == 409 and rejected.json()['error']['code'] == 'ai_not_ready'
    assert send(client, cv, 'A human can still help me.', str(uuid4())).status_code == 201
    assert not ai['app'].state.ai_chat_worker.run_once()
    assert len(ai['fake'].calls) == 1


def test_follow_up_email_never_enters_ai_context(reports):
    from test_business_hours import schedule
    ai=reports;client,cv,_=customer(ai);prefix='/api/v1/chat/conversations/'+cv['id']
    assert ai['admin'].put('/api/v1/admin/chat/business-hours',json={'version':0,'schedule':schedule()}).status_code==200
    body={'client_message_id':str(uuid4()),'body':'A question for the team','follow_up_email':'private-contact@example.test'}
    assert client.post(prefix+'/messages',json=body).status_code==409
    current=client.get(prefix).json()
    human=client.put(prefix+'/support-mode',json={'mode':'waiting_human','mode_version':current['mode_version']}).json()
    assert client.post(prefix+'/messages',json=body).status_code==201
    assert client.put(prefix+'/support-mode',json={'mode':'ai','mode_version':human['mode_version']}).status_code==200
    send(client,cv,'How do backups work?',str(uuid4()))
    ai['app'].state.ai_chat_worker.run_once()
    assert ai['fake'].calls
    assert 'private-contact@example.test' not in str(ai['fake'].calls)
