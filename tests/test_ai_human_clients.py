"""Administrator human-support context never widens customer access."""
import json
from uuid import uuid4

from test_ai_reports_api import reports, customer, prepare, confirmation
from test_ai_authoring import authoring
from test_ai_chat_isolation import configure


def test_report_context_and_readiness_are_native_admin_only(reports):
    ai = reports
    client, cv, token = customer(ai)
    draft, path = prepare(ai, client, cv)
    receipt = client.post(path + '/confirm', json=confirmation(draft)).json()
    cid = cv['id']
    owner = client.get('/api/v1/chat/conversations/' + cid).json()
    assert not {'reports', 'report_count', 'ai_available'} & set(owner)
    admin = ai['admin'].get('/api/v1/admin/chat/conversations/' + cid)
    assert admin.status_code == 200
    value = admin.json()
    assert value['ai_available'] and value['report_count'] == 1
    assert value['reports'] == [{'request_id': receipt['request_id'], 'title': receipt['title'], 'kind': 'feature', 'moderation': 'pending', 'status': None, 'deleted': False, 'guest': True}]
    assert token not in json.dumps(value['reports'])
    for actor in (ai['anon'], ai['user'], ai['trusted']):
        assert actor.get('/api/v1/admin/chat/conversations/' + cid).status_code in (401, 403)
    deleted = client.delete('/api/v1/chat/conversations/' + cid + '/reports/' + receipt['id'] + '?version=' + str(receipt['version']))
    assert deleted.status_code == 200
    value = ai['admin'].get('/api/v1/admin/chat/conversations/' + cid).json()
    assert value['report_count'] == 1 and value['reports'][0]['deleted']


def test_newest_twenty_context_retains_total_count_and_uses_current_request_title(reports, monkeypatch):
    ai = reports
    configure(ai['env'], daily_token_limit=10000000)
    client, cv, _ = customer(ai, 'external')
    ids = []
    for index in range(21):
        # Advance the fixture's UTC day to exercise pagination without bypassing quotas.
        monkeypatch.setattr('feedback.storage.relational.now', lambda i=index: f'2026-10-{i + 4:02d}T12:00:00.000Z')
        draft, path = prepare(ai, client, cv, title=f'Report {index}')
        result = client.post(path + '/confirm', json=confirmation(draft))
        assert result.status_code == 200
        ids.append(result.json()['request_id'])
    context = ai['admin'].get('/api/v1/admin/chat/conversations/' + cv['id']).json()
    assert context['report_count'] == 21 and len(context['reports']) == 20
    assert [r['request_id'] for r in context['reports']] == ids[:0:-1]
    assert all(not r['guest'] for r in context['reports'])
