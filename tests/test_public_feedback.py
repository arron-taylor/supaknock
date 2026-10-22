"""Public links are useful without exposing an installation-wide directory."""


def test_directory_is_admin_only_but_project_keys_remain_scoped(workflow):
    w = workflow
    assert w['anon'].get('/api/v1/projects').status_code == 401
    assert w['user'].get('/api/v1/projects').status_code == 403
    assert len(w['admin'].get('/api/v1/projects').json()['items']) == 2
    items = w['trusted'].get('/api/v1/projects').json()['items']
    assert [p['id'] for p in items] == [w['p']['id']]
    assert w['anon'].get('/api/v1/projects/' + w['p']['identifier']).status_code == 200
    assert w['anon'].get('/api/v1/projects/' + w['p']['id'] + '/boards').status_code == 200


def test_direct_link_read_then_authenticated_submission_and_vote(workflow):
    w = workflow
    board = '/api/v1/boards/' + w['b']['id']
    assert w['anon'].get('/boards/' + w['b']['id']).status_code == 200
    assert w['anon'].post(board + '/requests', json={'title': 'Anonymous idea'}).status_code == 401
    created = w['user'].post(board + '/requests', json={'title': 'A useful improvement'}).json()
    detail = '/api/v1/requests/' + created['id']
    assert w['anon'].get(detail).status_code == 404
    assert not w['anon'].get(board + '/requests').json()['items']
    accepted = w['admin'].post(detail + '/accept', json={'version': created['version']})
    assert accepted.status_code == 200
    assert w['anon'].get(detail).status_code == 200
    assert w['anon'].put(detail + '/vote', json={'present': True}).status_code == 401
    for _ in range(2):
        assert w['user'].put(detail + '/vote', json={'present': True}).status_code == 200
    public = w['anon'].get(detail).json()
    assert public['vote_count'] == 1
    assert 'submitted_ip_address' not in public
    assert 'external_user_id' not in public
    assert w['user'].get('/workspace', follow_redirects=False).headers['location'] == '/my-requests'
