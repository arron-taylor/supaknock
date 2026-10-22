"""Independent receipt authority and creator/manager races against real SQLite."""
from uuid import uuid4

import pytest

from feedback.domain import Actor
from feedback.errors import Error
from test_ai_chat_isolation import ai_chat, race
from test_ai_reports_storage import reports, draft, confirm


@pytest.mark.parametrize('kind', ['visitor', 'external', 'native'])
@pytest.mark.parametrize('change', ['expired', 'origin_removed', 'chat_disabled'])
def test_every_committed_receipt_operation_rechecks_current_chat_authority(reports, kind, change):
    env, store = reports, reports['store']
    customer, _, _, proposal = draft(env, kind)
    key = str(uuid4())
    receipt = confirm(env, customer, proposal, key)
    cid, principal = customer['conversation']['id'], customer['principal']
    if change == 'expired':
        table = 'chat_sessions' if kind == 'visitor' else 'chat_customer_sessions'
        with store.transaction(True) as c:
            c.execute('UPDATE ' + table + ' SET expires_at=0 WHERE id=?', (principal['session_id'],))
    else:
        config = store.chat_settings(env['project'])
        store.chat_configure(env['project'], env['admin'], change != 'chat_disabled',
                             [] if change == 'origin_removed' else config['origins'], config['version'])
    operations = [
        lambda: store.ai_reports(cid, principal),
        lambda: store.ai_report(cid, principal, receipt['id']),
        lambda: store.ai_proposals(cid, principal),
        lambda: store.ai_proposal(cid, principal, proposal['id']),
        lambda: confirm(env, customer, proposal, key),
        lambda: confirm(env, customer, proposal, str(uuid4())),
        lambda: store.ai_report_update(cid, principal, receipt['id'], receipt['version'], 'Unauthorized', ''),
        lambda: store.ai_report_delete(cid, principal, receipt['id'], receipt['version']),
    ]
    for operation in operations:
        with pytest.raises(Error):
            operation()
    with store.transaction() as c:
        assert c.execute('SELECT count(*) FROM ai_report_confirmations WHERE proposal_id=?', (proposal['id'],)).fetchone()[0] == 1
        assert c.execute('SELECT title FROM requests WHERE id=?', (receipt['request_id'],)).fetchone()[0] == receipt['title']


@pytest.mark.parametrize('kind', ['visitor', 'external', 'native'])
def test_acceptance_and_creator_edit_are_one_serialized_version_transition(reports, kind):
    env, store = reports, reports['store']
    customer, _, _, proposal = draft(env, kind)
    receipt = confirm(env, customer, proposal)
    cid, principal = customer['conversation']['id'], customer['principal']
    admin = Actor(user_id=env['admin']['user_id'], role='admin')
    def operation(handle, index):
        try:
            if index % 2:
                handle.moderate_request(receipt['request_id'], 'accept', None, receipt['version'], admin)
                return 'accepted'
            handle.ai_report_update(cid, principal, receipt['id'], receipt['version'], 'Final customer edit', 'Reviewed text')
            return 'edited'
        except Error as error:
            assert error.status == 409
            return 'conflict'
    outcomes = race(env, operation)
    assert outcomes.count('conflict') == 7
    current = store.ai_report(cid, principal, receipt['id'])
    assert current['version'] == receipt['version'] + 1
    if outcomes.count('edited'):
        assert current['title'] == 'Final customer edit' and current['moderation'] == 'pending'
        store.moderate_request(receipt['request_id'], 'accept', None, current['version'], admin)
        current = store.ai_report(cid, principal, receipt['id'])
    else:
        assert current['title'] == receipt['title'] and current['moderation'] == 'accepted'
    assert current['accepted_at'] and not current['can_edit'] and not current['can_delete']
    for operation in (
        lambda: store.ai_report_update(cid, principal, receipt['id'], current['version'], 'Too late', ''),
        lambda: store.ai_report_delete(cid, principal, receipt['id'], current['version']),
    ):
        with pytest.raises(Error) as caught:
            operation()
        assert caught.value.status == 409
    assert store.ai_report(cid, principal, receipt['id']) == current
