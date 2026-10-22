"""Exercise a real 0.1.0 -> current image upgrade on one disposable volume.

Run with host Python/httpx and both Docker images already built locally:
    python scripts/docker-upgrade-smoke.py

Optional: UPGRADE_OLD_IMAGE, UPGRADE_NEW_IMAGE, UPGRADE_SMOKE_PORT (default 18082).
No images are pulled or pushed. Only this run's container and volume are removed.
Push remains disabled, and device tokens are synthetic.
"""
from contextlib import ExitStack
import json
import os
import secrets
import subprocess
import time
from uuid import uuid4

import httpx


def docker(*args):
    return subprocess.check_output(
        ['docker', *args], text=True, stderr=subprocess.STDOUT,
    ).strip()


def main():
    old_tag = os.getenv('UPGRADE_OLD_IMAGE', 'taylorarronj/suggestion-box:0.1.0')
    new_tag = os.getenv('UPGRADE_NEW_IMAGE', 'suggestion-box:local')
    # Pin each image before creating resources, so concurrent tag changes cannot
    # change which builds this run verifies. inspect never pulls missing images.
    old_image = docker('image', 'inspect', old_tag, '--format', '{{.Id}}')
    new_image = docker('image', 'inspect', new_tag, '--format', '{{.Id}}')
    assert old_image != new_image, 'Upgrade requires two distinct local image IDs'
    port = int(os.getenv('UPGRADE_SMOKE_PORT', '18082'))
    assert 1024 <= port <= 65535
    base = f'http://127.0.0.1:{port}'
    suffix = secrets.token_hex(6)
    container = 'suggestion-box-upgrade-' + suffix
    volume = container + '-data'
    token, password = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    email = 'upgrade-admin@example.com'
    created_volume = False

    def start(image):
        docker('run', '-d', '--pull=never', '--name', container,
               '-p', f'127.0.0.1:{port}:8000', '-v', volume + ':/data',
               '-e', 'PUBLIC_BASE_URL=' + base, '-e', 'SETUP_TOKEN=' + token,
               image)
        for _ in range(150):
            try:
                if httpx.get(base + '/readyz', timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(.2)
        raise AssertionError('Upgrade test container readiness failed')

    def application_key_digest():
        # Compare hashes only; never return or print the actual application key.
        return docker('exec', container, 'python', '-c',
                      'from pathlib import Path; import hashlib; '
                      'print(hashlib.sha256(Path("/data/application.key").read_bytes()).hexdigest())')

    try:
        docker('volume', 'create', volume)
        created_volume = True
        start(old_image)
        with ExitStack() as clients:
            def client(**kwargs):
                return clients.enter_context(httpx.Client(
                    base_url=base + '/api/v1', timeout=10, **kwargs))

            admin = client()
            old_paths = admin.get('/openapi.json').raise_for_status().json()['paths']
            assert '/api/v1/auth/native/login' not in old_paths, 'Old image already has native-session migrations'
            setup = admin.post('/setup', json={
                'email': email, 'password': password, 'setup_token': token,
            }).raise_for_status().json()
            admin.headers.update({'Origin': base, 'X-CSRF-Token': setup['csrf_token']})
            account = admin.get('/auth/me').raise_for_status().json()
            session_cookies = dict(admin.cookies)
            assert session_cookies
            project = admin.post('/projects', json={
                'identifier': 'upgrade', 'name': 'Upgrade project',
                'description': 'Created by release 0.1.0',
            }).raise_for_status().json()
            pid = project['id']
            board = admin.post(f'/projects/{pid}/boards', json={
                'identifier': 'ideas', 'name': 'Upgrade ideas',
            }).raise_for_status().json()
            key = admin.post(f'/projects/{pid}/keys', json={
                'name': 'Existing backend', 'permissions': ['read', 'participate', 'audience:read'],
            }).raise_for_status().json()
            backend = client(headers={'Authorization': 'Bearer ' + key['secret']})
            external_id = '0001 Upgrade Case '
            request = backend.post(f"/boards/{board['id']}/requests", json={
                'title': 'Keep existing feedback', 'description': 'Written by the old container',
                'external_user_id': external_id,
            }).raise_for_status().json()
            rid = request['id']
            request = admin.post(f'/requests/{rid}/accept', json={
                'version': request['version'],
            }).raise_for_status().json()
            assert backend.put(f'/requests/{rid}/vote', json={
                'present': True, 'external_user_id': external_id,
            }).raise_for_status().json() == {'vote_count': 1, 'voted': True}
            history = admin.get(f'/requests/{rid}/history').raise_for_status().json()
            audience = backend.get(f'/requests/{rid}/audience').raise_for_status().json()
            original_key_digest = application_key_digest()

            docker('rm', '-f', container)
            start(new_image)
            assert application_key_digest() == original_key_digest
            # Reuse the exact browser cookie/CSRF pair and backend credential;
            # none of these clients logs in again after the upgrade.
            assert dict(admin.cookies) == session_cookies
            assert admin.get('/auth/me').raise_for_status().json() == account
            assert admin.get('/setup').raise_for_status().json()['available'] is False
            assert admin.post('/setup', json={
                'email': 'cannot-reclaim@example.com', 'password': password, 'setup_token': token,
            }).status_code == 409
            assert backend.get(f'/projects/{pid}').raise_for_status().json()['name'] == project['name']
            assert backend.get(f'/projects/{pid}/boards').raise_for_status().json()['items'][0]['id'] == board['id']
            public = backend.get(f'/requests/{rid}').raise_for_status().json()
            assert public['id'] == rid and public['vote_count'] == 1
            preserved = admin.get(f'/requests/{rid}').raise_for_status().json()
            for field in ('id', 'title', 'description', 'moderation', 'status', 'accepted_at', 'version'):
                assert preserved[field] == request[field], field
            assert preserved['vote_count'] == 1
            assert backend.get(f'/requests/{rid}/vote', params={
                'external_user_id': external_id,
            }).raise_for_status().json() == {'vote_count': 1, 'voted': True}
            assert backend.get(f'/requests/{rid}/audience').raise_for_status().json() == audience
            upgraded_history = admin.get(f'/requests/{rid}/history').raise_for_status().json()
            assert set(upgraded_history) == set(history)
            assert len(upgraded_history['items']) == len(history['items'])
            for previous, current in zip(history['items'], upgraded_history['items']):
                # The native-admin view gained private IP metadata. Old immutable
                # events have an unknown address; migration must not invent one.
                assert 'ip_address' not in previous
                assert set(current) == set(previous) | {'ip_address'}
                assert current['ip_address'] is None
            assert dict(upgraded_history, items=[
                {name: value for name, value in event.items() if name != 'ip_address'}
                for event in upgraded_history['items']
            ]) == history
            own = backend.get('/me/requests', params={'external_user_id': external_id}).raise_for_status().json()
            assert [item['id'] for item in own['items']] == [rid]
            # An actual post-upgrade mutation verifies the preserved CSRF hash.
            changed = admin.put(f'/requests/{rid}/status', json={
                'version': preserved['version'], 'status': 'planned',
            }).raise_for_status().json()
            assert changed['status'] == 'planned' and changed['accepted_at'] == request['accepted_at']

            native = client()
            login = native.post('/auth/native/login', json={
                'email': email, 'password': password,
            }).raise_for_status().json()
            assert login['token'].startswith('ns_') and not native.cookies
            native.headers['Authorization'] = 'Bearer ' + login['token']
            device_id, device_token = str(uuid4()), secrets.token_hex(32)
            registered = native.put('/admin/devices/' + device_id, json={
                'token': device_token, 'environment': 'sandbox', 'name': 'Synthetic upgrade device',
            }).raise_for_status().json()
            assert registered['id'] == device_id and device_token not in json.dumps(registered)
            settings = native.get('/admin/push').raise_for_status().json()
            assert settings['installation_id'] == login['installation_id']
            assert settings['enabled'] is False and settings['connected'] is False
            chat_origin = 'https://upgrade-widget.example'
            native.put('/admin/chat/projects/' + pid, json={
                'enabled': True, 'origins': [chat_origin], 'version': 0,
            }).raise_for_status()
            visitor = client(headers={'Origin': chat_origin})
            chat = visitor.post('/chat/projects/upgrade/sessions').raise_for_status().json()
            visitor.headers['X-Chat-Token'] = chat['token']
            path = '/chat/conversations/' + chat['conversation']['id'] + '/messages'
            visitor.post(path, json={
                'client_message_id': str(uuid4()), 'body': 'Chat after upgrade',
            }).raise_for_status()
            native.post(path, json={
                'client_message_id': str(uuid4()), 'body': 'Admin reply after upgrade',
            }).raise_for_status()
            assert [m['body'] for m in visitor.get(path).raise_for_status().json()['items']] == [
                'Chat after upgrade', 'Admin reply after upgrade',
            ]
            assert native.get('/admin/push/deliveries').raise_for_status().json()['items'] == []
            assert native.post('/auth/logout').status_code == 204
            assert native.get('/auth/me').status_code == 401
            assert admin.get('/admin/devices').raise_for_status().json()['items'][0]['enabled'] is False
        print(f'Upgrade smoke passed: {old_tag} -> {new_tag}; accounts, browser session/CSRF, '
              'API credential/external identity, project/board, accepted request/history/vote, '
              'and application key preserved. Native login/devices/chat/logout work. Push stayed disabled.')
        print(f'Verified immutable image IDs: {old_image} -> {new_image}')
    finally:
        subprocess.run(['docker', 'rm', '-f', container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if created_volume:
            subprocess.run(['docker', 'volume', 'rm', volume], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
