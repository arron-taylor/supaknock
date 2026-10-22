import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from connected_fixture import FixtureApp
from feedback.connected_apps_protocol import validated_response


def test_committed_watermark_excludes_late_backdated_commit():
    app=FixtureApp(count=5)
    query={'from':'2000-01-01T00:00:00Z','until':'2099-01-01T00:00:00Z','limit':2,'user_id':None,'cursor':None}
    # A writer prepares its event before discovery but commits after page one.
    prepared=dict(id='late',occurred_at='2026-01-01T00:00:00Z',type='user.created',title='Late commit',user_id=None,url=None)
    first=app.query('activity',query);app.commit(prepared)
    ids=[x['id'] for x in first['items']];page=first
    while page['next_cursor']:
        q=query|{'cursor':page['next_cursor']};page=validated_response('activity',app.query('activity',q),q,app.instance)
        assert page['snapshot']==first['snapshot'];ids.extend(x['id'] for x in page['items'])
    assert len(ids)==5 and 'late' not in ids
    refreshed=app.query('activity',query|{'limit':100});assert 'late' in [x['id'] for x in refreshed['items']]

def test_live_users_and_query_binding():
    import pytest
    app=FixtureApp(count=5);q={'limit':2,'query':'','joined':None,'cursor':None}
    a=app.query('users',q);assert a['next_cursor']
    with pytest.raises(ValueError):app.query('users',q|{'query':'changed','cursor':a['next_cursor']})
    app.users=app.users[:2]+app.users[3:] # Retention removes a later user; directory is live.
    b=app.query('users',q|{'cursor':a['next_cursor']});assert len(b['items'])==2
