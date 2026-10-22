"""Disposable Docker acceptance test. Removes only resources it creates.

Run after docker build -t suggestion-box:local . with host Python/httpx installed.
Set SMOKE_IMAGE and SMOKE_PORT to test a candidate without retagging it.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import tempfile
import threading
import time
from uuid import uuid4
import httpx

suffix = secrets.token_hex(6)
container = 'suggestion-box-check-' + suffix
restore_container = container + '-restore'
volume = container + '-data'
restore_volume = container + '-restored'
image = os.environ.get('SMOKE_IMAGE', 'suggestion-box:local')
port = int(os.environ.get('SMOKE_PORT', '18081'))
assert 1 <= port <= 65535, 'SMOKE_PORT must be a valid TCP port'
base = f'http://127.0.0.1:{port}'
token = secrets.token_urlsafe(32)
password = secrets.token_urlsafe(24)
received = []
received_lock = threading.Lock()
network_fields = {'ip_address', 'submitted_ip_address', 'initial_ip_address', 'last_customer_ip_address'}


def canonical_ip(value):
    assert isinstance(value, str) and '%' not in value, 'Expected a recorded connection address'
    assert str(ip_address(value)) == value, 'Expected a canonical IPv4 or IPv6 address'
    return value


def no_network_metadata(value):
    if isinstance(value, dict):
        assert not (network_fields & value.keys()), 'Private connection metadata leaked'
        for item in value.values():
            no_network_metadata(item)
    elif isinstance(value, list):
        for item in value:
            no_network_metadata(item)


def customer_message_view(value):
    # Administrator send responses include private metadata; customer history does not.
    return {key: item for key, item in value.items() if key not in network_fields}


def docker(*args):
    try:
        return subprocess.check_output(['docker', *args], text=True, stderr=subprocess.STDOUT).strip()
    except subprocess.CalledProcessError:
        # A run command contains a disposable setup secret. Never echo its argv
        # or captured process output when reporting a failed Docker operation.
        raise RuntimeError('Docker operation failed: ' + args[0]) from None


def start(name, data_volume):
    docker('run', '-d', '--name', name, '-p', f'127.0.0.1:{port}:8000',
           '-v', data_volume + ':/data', '-e', 'PUBLIC_BASE_URL=' + base,
           '-e', 'SETUP_TOKEN=' + token, '-e', 'ALLOW_INTERNAL_WEBHOOKS=true', image)
    for _ in range(100):
        try:
            if httpx.get(base + '/readyz', timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(.2)
    raise AssertionError('Container readiness failed')


class Receiver(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        with received_lock:
            received.append((dict(self.headers), raw))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


receiver = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
thread = threading.Thread(target=receiver.serve_forever, daemon=True)
thread.start()
try:
    docker('volume', 'create', volume)
    start(container, volume)
    admin = httpx.Client(base_url=base + '/api/v1', timeout=10)
    response = admin.post('/setup', json={'email':'smoke-admin@example.com','password':password,'setup_token':token}).raise_for_status().json()
    admin.headers.update({'Origin':base,'X-CSRF-Token':response['csrf_token']})
    participant = httpx.Client(base_url=base + '/api/v1', timeout=10)
    participant_signup = participant.post('/auth/register', json={'email':'smoke-participant@example.com','password':password}).raise_for_status().json()
    participant.headers.update({'Origin':base,'X-CSRF-Token':participant_signup['csrf_token']})
    no_network_metadata(response)
    no_network_metadata(participant_signup)
    project = admin.post('/projects', json={'identifier':'smoke','name':'Smoke test'}).raise_for_status().json()
    pid = project['id']
    app_connection_path = f'/projects/{pid}/app-data/connection'
    app_connection = admin.put(app_connection_path,json={'version':0,'base_url':'https://synthetic-app.example','token':'synthetic-app-read-token'}).raise_for_status().json()
    assert app_connection['has_token'] and not app_connection['enabled']
    assert 'synthetic-app-read-token' not in str(app_connection)
    # Native sessions/device tokens remain synthetic; relay enrollment stays disabled.
    native = httpx.Client(base_url=base + '/api/v1', timeout=10)
    native_login = native.post('/auth/native/login', json={'email':'smoke-admin@example.com','password':password}).raise_for_status().json()
    assert native_login['token'].startswith('ns_') and not native.cookies
    native.headers['Authorization'] = 'Bearer ' + native_login['token']
    device_id, device_token = str(uuid4()), secrets.token_hex(32)
    device_body = {'token':device_token,'environment':'sandbox','name':'Synthetic Docker smoke device'}
    registration = native.put('/admin/devices/' + device_id, json=device_body).raise_for_status().json()
    assert device_token not in json.dumps(registration)
    assert native.get('/admin/push').raise_for_status().json()['enabled'] is False
    chat_origin = 'https://widget-smoke.example'
    native.put('/admin/chat/projects/' + pid, json={'enabled':True,'origins':[chat_origin],'version':0}).raise_for_status()
    visitor = httpx.Client(base_url=base + '/api/v1', timeout=10, headers={'Origin':chat_origin})
    chat_session = visitor.post('/chat/projects/smoke/sessions').raise_for_status().json()
    no_network_metadata(chat_session)
    visitor.headers['X-Chat-Token'] = chat_session['token']
    conversation_id = chat_session['conversation']['id']
    message_path = '/chat/conversations/' + conversation_id + '/messages'
    hours_body={'version':0,'schedule':{'enabled':True,'timezone':'America/Chicago','days':[[{'start':'09:00','end':'17:00'}] for _ in range(5)]+[[],[]],'collect_email':True}}
    saved_hours=native.put('/admin/chat/business-hours',json=hours_body).raise_for_status().json()
    visitor_message = {'client_message_id':str(uuid4()),'body':'Durable visitor message','follow_up_email':'smoke-follow-up@example.test'}
    first_message = visitor.post(message_path, json=visitor_message).raise_for_status().json()
    no_network_metadata(first_message)
    native_reply = native.post(message_path, json={'client_message_id':str(uuid4()),'body':'Durable native admin reply'}).raise_for_status().json()
    canonical_ip(native_reply['ip_address'])
    native.post('/chat/conversations/' + conversation_id + '/read', json={'sequence':2}).raise_for_status()
    visitor.post('/chat/conversations/' + conversation_id + '/read', json={'sequence':1}).raise_for_status()

    archive_project = native.post('/projects', json={'identifier':'archive-smoke','name':'Archived smoke project'}).raise_for_status().json()
    archive_board = native.post('/projects/'+archive_project['id']+'/boards', json={'identifier':'archived-board','name':'Archived board'}).raise_for_status().json()
    archive_board = native.put('/boards/'+archive_board['id']+'/archive', json={'version':archive_board['version'],'archived':True}).raise_for_status().json()
    archive_project = native.put('/projects/'+archive_project['id']+'/archive', json={'version':archive_project['version'],'archived':True}).raise_for_status().json()

    def verify_native_chat():
        assert native.get('/projects/'+archive_project['id']).raise_for_status().json()==archive_project
        assert native.get('/boards/'+archive_board['id']).raise_for_status().json()['archived_at']==archive_board['archived_at']
        assert [item['id'] for item in native.get('/projects?archive=archived').raise_for_status().json()['items']]==[archive_project['id']]
        assert participant.get('/projects/'+archive_project['id']).status_code==404
        assert native.get('/admin/chat/business-hours').raise_for_status().json()==saved_hours
        contact=native.get('/admin/chat/conversations/'+conversation_id).raise_for_status().json()
        assert contact['follow_up_email']=='smoke-follow-up@example.test' and contact['contact_version']==1
        assert native.get('/auth/me').status_code == 200
        settings = native.get('/admin/push').raise_for_status().json()
        assert settings['installation_id'] == native_login['installation_id']
        assert settings['enabled'] is False and settings['connected'] is False
        devices = native.get('/admin/devices').raise_for_status().json()['items']
        assert len(devices) == 1 and devices[0]['id'] == device_id and devices[0]['enabled']
        assert devices[0]['generation'] == registration['generation']
        assert device_token not in json.dumps(devices)
        unchanged = native.put('/admin/devices/' + device_id, json=device_body).raise_for_status().json()
        assert unchanged['generation'] == registration['generation']
        messages = visitor.get(message_path).raise_for_status().json()['items']
        no_network_metadata(messages)
        assert [item['body'] for item in messages] == ['Durable visitor message','Durable native admin reply']
        assert [item['sequence'] for item in messages] == [1,2]
        assert visitor.get('/chat/conversations/' + conversation_id).raise_for_status().json()['unread_count'] == 1
        assert native.get('/chat/conversations/' + conversation_id).raise_for_status().json()['unread_count'] == 0
        # Retrying a lost browser acknowledgement must retain the same saved message.
        duplicate = visitor.post(message_path, json=visitor_message).raise_for_status().json()
        assert duplicate['id'] == first_message['id'] and duplicate['sequence'] == 1
        assert native.get('/admin/push/deliveries').raise_for_status().json()['items'] == []

    verify_native_chat()
    board = admin.post(f'/projects/{pid}/boards', json={'identifier':'ideas','name':'Ideas'}).raise_for_status().json()
    key = admin.post(f'/projects/{pid}/keys', json={'name':'Test backend','permissions':['read','participate','audience:read']}).raise_for_status().json()
    manager_key = admin.post(f'/projects/{pid}/keys', json={'name':'Management smoke backend','permissions':['manage']}).raise_for_status().json()
    settings = admin.put(f'/projects/{pid}/webhook', json={
        'url':f'http://host.docker.internal:{receiver.server_port}/hook', 'enabled':True,
        'events':['request.created','request.accepted','request.status_changed','chat.message_created'], 'version':1
    }).raise_for_status().json()
    client = httpx.Client(base_url=base + '/api/v1', headers={'Authorization':'Bearer ' + key['secret']})
    manager = httpx.Client(base_url=base + '/api/v1', headers={'Authorization':'Bearer ' + manager_key['secret']})
    request = client.post(f"/boards/{board['id']}/requests", json={
        'title':'Persistent feedback', 'description':'Survives container recreation', 'external_user_id':'0001 Case '
    }).raise_for_status().json()
    no_network_metadata(request)
    rid = request['id']
    assert httpx.get(base + '/api/v1/requests/' + rid).status_code == 404
    request = admin.post(f'/requests/{rid}/accept', json={'version':request['version']}).raise_for_status().json()
    vote = client.put(f'/requests/{rid}/vote', json={'present':True,'external_user_id':'0001 Case '}).raise_for_status().json()
    assert vote == {'vote_count':1,'voted':True}
    request = admin.put(f'/requests/{rid}/status', json={'version':request['version'],'status':'shipped'}).raise_for_status().json()
    audience = client.get(f'/requests/{rid}/audience').raise_for_status().json()['items']
    assert len(audience) == 1 and audience[0]['external_user_id'] == '0001 Case '
    feedback_user_id = audience[0]['user_id']
    exchange_body = {'external_user_id':'0001 Case ','client_type':'native','display_name':'Docker customer'}
    customer_session = client.post('/chat/projects/smoke/customer-sessions',json=exchange_body).raise_for_status().json()
    no_network_metadata(customer_session)
    assert customer_session['token'].startswith('cc_')
    identity = customer_session['customer']
    assert identity['kind'] == 'external' and identity['user_id'] == feedback_user_id
    customer_client = httpx.Client(base_url=base+'/api/v1',timeout=10,headers={'X-Chat-Token':customer_session['token']})
    conversation_body = {'client_conversation_id':str(uuid4())}
    customer_conversation = customer_client.post('/chat/conversations',json=conversation_body).raise_for_status().json()
    no_network_metadata(customer_conversation)
    customer_path = '/chat/conversations/'+customer_conversation['id']
    customer_message_body = {'client_message_id':str(uuid4()),'body':'Durable authenticated customer message'}
    customer_message = customer_client.post(customer_path+'/messages',json=customer_message_body).raise_for_status().json()
    no_network_metadata(customer_message)
    customer_reply = native.post(customer_path+'/messages',json={'client_message_id':str(uuid4()),'body':'Initial private customer reply'}).raise_for_status().json()
    canonical_ip(customer_reply['ip_address'])
    customer_messages = [customer_message, customer_message_view(customer_reply)]
    customer_client.post(customer_path+'/read',json={'sequence':1}).raise_for_status()
    form=admin.post('/admin/projects/'+pid+'/forms',json={'name':'Early access','enabled':True,'public_enabled':True,'embed_enabled':True,'embed_origins':['https://embed.example'],'fields':[{'key':'email','label':'Email','type':'email','required':True}]}).raise_for_status().json()
    form_key=admin.post('/projects/'+pid+'/keys',json={'name':'Forms backend','permissions':['forms:submit']}).raise_for_status().json()['secret']
    forms_backend=httpx.Client(base_url=base+'/api/v1',timeout=15,headers={'Authorization':'Bearer '+form_key})
    form_body={'client_request_id':str(uuid4()),'answers':{'email':'test@example.com'}}
    form_receipt=forms_backend.post('/forms/'+form['id']+'/submissions',json=form_body).raise_for_status().json()
    public_forms=httpx.Client(base_url=base+'/api/v1',timeout=15,headers={'Origin':base})
    public_ticket=public_forms.get('/public/forms/'+form['id']).raise_for_status().json()['ticket']
    embed_forms=httpx.Client(base_url=base+'/api/v1',timeout=15,headers={'Origin':'https://embed.example'})
    embed_ticket=embed_forms.get('/embed/forms/'+form['id']).raise_for_status().json()['ticket']
    embed_body={'client_request_id':str(uuid4()),'answers':{'email':'embed@example.com'},'ticket':embed_ticket}
    assert embed_forms.post('/embed/forms/'+form['id']+'/submissions',json=embed_body).raise_for_status().json()=={'ok':True}
    public_body={'client_request_id':str(uuid4()),'ticket':public_ticket,'answers':{'email':'public@example.com'}}
    assert public_forms.post('/public/forms/'+form['id']+'/submissions',json=public_body).raise_for_status().json()=={'ok':True}
    # Notification acknowledgements are one shared installation inbox, separate
    # from chat read positions and device push preferences.
    notification_page = admin.get('/admin/notifications').raise_for_status().json()
    assert notification_page['unread_count'] == 6
    request_notice = next(n for n in notification_page['items'] if n['resource_id'] == rid)
    native.put('/admin/notifications/' + request_notice['id'] + '/read', json={'read':True}).raise_for_status()
    expected_notifications = admin.get('/admin/notifications').raise_for_status().json()
    no_network_metadata(expected_notifications)
    assert expected_notifications['unread_count'] == 5
    assert native.get('/admin/notifications').raise_for_status().json() == expected_notifications

    private_board = admin.post(f"/projects/{pid}/boards", json={'identifier':'private-check','name':'Private intake','visibility':'private'}).raise_for_status().json()
    private_request = client.post(f"/boards/{private_board['id']}/requests", json={'title':'Private durable feedback','external_user_id':'0001 Case '}).raise_for_status().json()
    admin.post(f"/requests/{private_request['id']}/accept", json={'version':private_request['version']}).raise_for_status()
    expected_notifications = admin.get('/admin/notifications').raise_for_status().json()

    def verify_notifications():
        assert admin.get(f"/boards/{private_board['id']}").raise_for_status().json()['visibility'] == 'private'
        with httpx.Client(base_url=base + '/api/v1', timeout=10) as public:
            assert public.get(f"/boards/{private_board['id']}").status_code == 404
            assert public.get(f"/requests/{private_request['id']}").status_code == 404
        assert public_forms.get('/public/forms/'+form['id']).status_code==200
        assert embed_forms.get('/embed/forms/'+form['id']).status_code==200
        assert embed_forms.post('/embed/forms/'+form['id']+'/submissions',json=embed_body).raise_for_status().json()=={'ok':True}
        assert public_forms.post('/public/forms/'+form['id']+'/submissions',json=public_body).raise_for_status().json()=={'ok':True}
        assert len(admin.get('/admin/forms/'+form['id']+'/submissions').raise_for_status().json()['items'])==3
        assert native.get('/admin/form-submissions/'+form_receipt['id']).raise_for_status().json()['answers']==form_body['answers']
        assert forms_backend.post('/forms/'+form['id']+'/submissions',json=form_body).raise_for_status().json()==form_receipt
        assert admin.get('/admin/notifications').raise_for_status().json() == expected_notifications
        assert native.get('/admin/notifications').raise_for_status().json() == expected_notifications
        assert client.get('/admin/notifications').status_code == 403

    expected_activity = native.get('/admin/account-activity?limit=100').raise_for_status().json()
    activity_items = expected_activity['items']
    assert len(activity_items) == 5 and expected_activity['has_more'] is False
    assert {(item['event_type'],item['transport']) for item in activity_items if item['email']=='smoke-admin@example.com'} == {
        ('setup',None), ('login','cookie'), ('login','native')}
    assert {(item['event_type'],item['transport']) for item in activity_items if item['email']=='smoke-participant@example.com'} == {
        ('signup',None), ('login','cookie')}
    for item in activity_items:
        canonical_ip(item['ip_address'])
    expected_request_ip = canonical_ip(native.get(f'/requests/{rid}').raise_for_status().json()['submitted_ip_address'])
    expected_request_history = native.get(f'/requests/{rid}/history').raise_for_status().json()['items']
    assert len(expected_request_history) == 3
    for item in expected_request_history:
        canonical_ip(item['ip_address'])
    expected_conversation_ips = {}
    expected_message_ips = {}
    for cid in (conversation_id, customer_conversation['id']):
        detail = native.get('/chat/conversations/' + cid).raise_for_status().json()
        expected_conversation_ips[cid] = {field:canonical_ip(detail[field]) for field in ('initial_ip_address','last_customer_ip_address')}
        messages = native.get('/chat/conversations/' + cid + '/messages').raise_for_status().json()['items']
        expected_message_ips[cid] = {message['id']:canonical_ip(message['ip_address']) for message in messages}

    def verify_network_metadata():
        assert native.get(app_connection_path).raise_for_status().json() == app_connection
        assert participant.get(app_connection_path).status_code == 403
        # All snapshots must survive process restart, container recreation, and
        # restoring the online backup. Reads do not invent or overwrite IPs.
        assert native.get('/admin/account-activity?limit=100').raise_for_status().json() == expected_activity
        assert admin.get('/admin/account-activity?limit=100').raise_for_status().json() == expected_activity
        filtered = native.get('/admin/account-activity',params={'user_id':participant_signup['user']['id']}).raise_for_status().json()['items']
        assert len(filtered) == 2 and all(item['user_id']==participant_signup['user']['id'] for item in filtered)
        for unprivileged in (participant, client, manager, visitor, customer_client):
            assert unprivileged.get('/admin/account-activity').status_code in (401,403)
        with httpx.Client(base_url=base + '/api/v1',timeout=10) as public:
            assert public.get('/admin/account-activity').status_code == 401
            for unprivileged in (public, participant, client, manager):
                no_network_metadata(unprivileged.get(f'/requests/{rid}').raise_for_status().json())
                no_network_metadata(unprivileged.get(f'/requests/{rid}/history').raise_for_status().json())
                no_network_metadata(unprivileged.get(f"/boards/{board['id']}/requests").raise_for_status().json())
        assert native.get(f'/requests/{rid}').raise_for_status().json()['submitted_ip_address'] == expected_request_ip
        assert native.get(f'/requests/{rid}/history').raise_for_status().json()['items'] == expected_request_history
        for cid, private_fields in expected_conversation_ips.items():
            detail = native.get('/chat/conversations/' + cid).raise_for_status().json()
            assert {field:detail[field] for field in private_fields} == private_fields
            messages = native.get('/chat/conversations/' + cid + '/messages').raise_for_status().json()['items']
            assert {message['id']:canonical_ip(message['ip_address']) for message in messages} == expected_message_ips[cid]
        for customer, path in ((visitor,'/chat/conversations/' + conversation_id),(customer_client,customer_path)):
            no_network_metadata(customer.get(path).raise_for_status().json())
            no_network_metadata(customer.get(path + '/messages').raise_for_status().json())
        no_network_metadata(customer_client.get('/chat/conversations').raise_for_status().json())

    expected_chat = {}
    protected_values = [token,password,key['secret'],manager_key['secret'],settings['signing_secret'],native_login['token'],device_token,chat_session['token'],customer_session['token']]

    def expect_chat_event(message, conversation, customer):
        expected_chat[message['id']] = {
            'conversation':{'id':conversation['id']},
            'message':{'id':message['id'],'sequence':message['sequence'],'sender_role':message['sender_role']},
            'customer':customer,
        }
        protected_values.append(message['body'])

    expect_chat_event(customer_message,customer_conversation,identity)
    expect_chat_event(customer_reply,customer_conversation,identity)

    def verify_webhooks():
        expected_count = 5 + len(expected_chat)
        for _ in range(100):
            with received_lock:
                unique_ids = {json.loads(raw)['id'] for _,raw in received}
            deliveries = admin.get(f'/projects/{pid}/deliveries').raise_for_status().json()['items']
            if len(unique_ids) == expected_count and len(deliveries) == expected_count and all(d['state']=='succeeded' for d in deliveries):
                break
            time.sleep(.2)
        assert len(unique_ids) == expected_count, 'Running worker did not deliver all expected events'
        assert len(deliveries) == expected_count and all(d['state']=='succeeded' for d in deliveries), 'Webhook delivery status did not persist'
        with received_lock:
            snapshots = list(received)
        request_events = {}
        chat_events = {}
        for headers, raw in snapshots:
            sig = hmac.new(settings['signing_secret'].encode(), headers['X-Feedback-Timestamp'].encode() + b'.' + raw, hashlib.sha256).hexdigest()
            assert hmac.compare_digest(headers['X-Feedback-Signature'], 'v1=' + sig)
            payload = json.loads(raw)
            no_network_metadata(payload)
            assert payload['id'] == headers['X-Feedback-Event-ID']
            assert abs(time.time()-int(headers['X-Feedback-Timestamp'])) < 300
            assert payload['project'] == {'id':pid,'identifier':'smoke'}
            assert all(secret.encode() not in raw for secret in protected_values)
            if payload['type']=='chat.message_created':
                assert payload['id'] in expected_chat
                assert all(payload[k]==v for k,v in expected_chat[payload['id']].items())
                assert 'body' not in payload['message'] and 'body' not in payload
                chat_events[payload['id']] = payload
            else:
                assert payload['creator']['external_user_id'] == '0001 Case '
                request_events[payload['id']] = payload
        # At-least-once deliveries may repeat an ID; unique committed events may not.
        assert set(chat_events) == set(expected_chat)
        assert len(request_events) == 5
        assert {e['type'] for e in request_events.values()} == {'request.created','request.accepted','request.status_changed'}

    def verify_customer_chat(stage):
        verify_notifications()
        verify_network_metadata()
        # A freshly exchanged credential must recover the same customer's data,
        # never create another user or expose the previous transport credential.
        renewed = client.post('/chat/projects/smoke/customer-sessions',json=exchange_body).raise_for_status().json()
        no_network_metadata(renewed)
        assert renewed['customer'] == identity and renewed['token'] != customer_session['token']
        protected_values.append(renewed['token'])
        with httpx.Client(base_url=base+'/api/v1',timeout=10,headers={'X-Chat-Token':renewed['token']}) as fresh:
            listing = fresh.get('/chat/conversations').raise_for_status().json()['items']
            no_network_metadata(listing)
            assert [cv['id'] for cv in listing] == [customer_conversation['id']]
            assert listing[0]['unread_count'] == 1
            assert fresh.post('/chat/conversations',json=conversation_body).raise_for_status().json()['id'] == customer_conversation['id']
            history = fresh.get(customer_path+'/messages').raise_for_status().json()['items']
            no_network_metadata(history)
            assert history == customer_messages
            assert [m['sequence'] for m in history] == list(range(1,len(history)+1))
            duplicate = fresh.post(customer_path+'/messages',json=customer_message_body).raise_for_status().json()
            assert duplicate == customer_message
            assert customer_client.get(customer_path).raise_for_status().json()['unread_count'] == 1
            # New admin replies must be deliverable and signed after every
            # restart, recreation, and restoration, using the restored secret.
            reply = native.post(customer_path+'/messages',json={'client_message_id':str(uuid4()),'body':'Private reply after '+stage}).raise_for_status().json()
            expected_message_ips[customer_conversation['id']][reply['id']] = canonical_ip(reply['ip_address'])
            customer_messages.append(customer_message_view(reply))
            expect_chat_event(reply,customer_conversation,identity)
            assert fresh.get(customer_path).raise_for_status().json()['unread_count'] == 2
            cursor = reply['sequence']-1
            assert fresh.post(customer_path+'/read',json={'sequence':cursor}).raise_for_status().json()['read_sequence'] == cursor
            assert customer_client.get(customer_path).raise_for_status().json()['unread_count'] == 1
        verify_webhooks()

    verify_customer_chat('initial startup')
    docker('restart', container)
    # start() cannot create an existing container; wait directly after restart.
    for _ in range(100):
        try:
            if httpx.get(base + '/readyz', timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(.2)
    assert admin.get('/auth/me').status_code == 200
    verify_native_chat()
    verify_customer_chat('container restart')
    assert client.get(f'/requests/{rid}').json()['vote_count'] == 1
    docker('rm', '-f', container)
    start(container, volume)
    assert admin.get('/auth/me').status_code == 200
    verify_native_chat()
    verify_customer_chat('container recreation')
    assert client.get(f'/requests/{rid}').json()['status'] == 'shipped'
    assert admin.get('/setup').json()['available'] is False
    assert len(admin.get(f'/projects/{pid}/deliveries').json()['items']) == 5 + len(expected_chat)
    docker('exec', container, 'python', '-m', 'feedback.maintenance', 'backup', '/data/smoke-backup')
    with tempfile.TemporaryDirectory() as directory:
        docker('cp', container + ':/data/smoke-backup/.', directory)
        assert (Path(directory) / 'application.key').is_file()
        docker('rm', '-f', container)
        docker('volume', 'create', restore_volume)
        docker('run', '-d', '--name', restore_container, '-v', restore_volume + ':/data', image, 'sleep', 'infinity')
        docker('cp', directory + '/.', restore_container + ':/data/')
        docker('exec', '--user', '0', restore_container, 'chown', '-R', '10001:10001', '/data')
        docker('rm', '-f', restore_container)
        start(restore_container, restore_volume)
        assert admin.get('/auth/me').status_code == 200
        verify_native_chat()
        verify_customer_chat('backup restoration')
        assert client.get(f'/requests/{rid}').json()['vote_count'] == 1
        assert admin.get(f'/projects/{pid}/deliveries').json()['items'][0]['state'] == 'succeeded'
    third = visitor.post(message_path, json={'client_message_id':str(uuid4()),'body':'Writable after backup restoration'}).raise_for_status().json()
    assert third['sequence'] == 3
    assert admin.get('/admin/notifications/summary').raise_for_status().json()['unread_count'] == expected_notifications['unread_count'] + 1
    assert len(admin.get('/admin/notifications').raise_for_status().json()['items']) == len(expected_notifications['items']) + 1
    expect_chat_event(third,chat_session['conversation'],chat_session['conversation']['customer'])
    verify_webhooks()
    # Revoking the issuer immediately invalidates retained customer credentials;
    # the durable chat remains available to the installation administrator.
    admin.delete(f"/projects/{pid}/keys/{key['id']}").raise_for_status()
    assert customer_client.get(customer_path).status_code == 401
    assert native.get(customer_path).raise_for_status().json()['last_sequence'] == len(customer_messages)
    assert native.post('/auth/logout').status_code == 204
    assert native.get('/auth/me').status_code == 401
    assert native.get('/admin/account-activity').status_code == 401
    assert admin.get('/admin/devices').raise_for_status().json()['items'][0]['enabled'] is False
    admin.close(); client.close(); manager.close(); native.close(); visitor.close(); customer_client.close(); participant.close()
    print('Docker smoke passed: feedback/webhooks, native session/device, shared notification acknowledgements, anonymous and authenticated chat history/read/idempotence, renewed credentials, admin-only account activity and private request/chat IP persistence after restart/recreation/restore, signed body-free reply events, issuer/logout revocation. Project and board archives, plus connected-app encrypted configuration, survived restart/recreation/restore without app probes. Push remained disabled.')
finally:
    receiver.shutdown(); receiver.server_close(); thread.join()
    for name in [container, restore_container]:
        subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    for name in [volume, restore_volume]:
        subprocess.run(['docker','volume','rm',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
