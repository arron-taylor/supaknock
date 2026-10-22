"""Native-only reported versus reserved provider usage; no generation on reads."""
from datetime import datetime, timezone


def test_usage_is_native_admin_only_and_project_scoped(workflow):
    w = workflow
    url = '/api/v1/projects/' + w['p']['id'] + '/ai/usage'
    for client in (w['anon'], w['user'], w['trusted'], w['manager'], w['other']):
        assert client.get(url).status_code in (401, 403)
    result = w['admin'].get(url)
    assert result.status_code == 200
    assert result.json() == {'project_id': w['p']['id'], 'day': datetime.now(timezone.utc).date().isoformat(),
        'reserved_requests': 0, 'reserved_tokens': 0, 'reported_input_tokens': 0, 'reported_output_tokens': 0}
    with w['app'].state.storage.transaction(True) as c:
        c.execute("INSERT INTO ai_usage(project_id,day,reserved_requests,reserved_tokens,reported_input_tokens,reported_output_tokens) VALUES(?,date('now'),2,5000,30,20)", (w['p']['id'],))
    assert w['admin'].get(url).json()['reserved_tokens'] == 5000
    assert w['admin'].get(url).json()['reported_output_tokens'] == 20
    assert w['admin'].get('/api/v1/projects/' + w['q']['id'] + '/ai/usage').json()['reserved_tokens'] == 0
    assert 'Anonymous public read' not in w['app'].openapi()['paths']['/api/v1/projects/{project}/ai/usage']['get']['description']
