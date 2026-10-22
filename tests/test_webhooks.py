from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import time
import pytest
from feedback.security import Vault
from feedback.webhooks import WebhookWorker, signature, destination, MAX_ATTEMPTS
from feedback.errors import Error
from feedback.domain import Actor
from feedback.storage.sqlite import SQLiteStorage
from conftest import submission


def configured(w):
    response=w['admin'].put('/api/v1/projects/'+w['p']['id']+'/webhook',json={'url':'http://127.0.0.1:9999/receive','enabled':True,'events':['request.created','request.accepted','request.status_changed','request.deleted'],'version':1})
    assert response.status_code==200,response.text
    return response.json()['signing_secret']


def test_signing_retries_immutable_payload_rotation_and_delete(workflow):
    w=workflow;secret=configured(w);s=w['app'].state.storage
    r=submission(w['trusted'],w['b'],'glean-person')
    seen=[]
    def transport(url,headers,body,internal):
        expected=hmac.new(secret.encode(),headers['X-Feedback-Timestamp'].encode()+b'.'+body,hashlib.sha256).hexdigest()
        assert hmac.compare_digest(headers['X-Feedback-Signature'],'v1='+expected)
        seen.append((url,headers['X-Feedback-Event-ID'],body))
        return 503 if len(seen)==1 else 204
    worker=WebhookWorker(s,Vault(w['cfg'].secret),True,transport)
    assert worker.once()
    d=w['admin'].get('/api/v1/projects/'+w['p']['id']+'/deliveries').json()['items'][0]
    assert d['state']=='queued' and d['attempt_count']==1 and d['last_http_status']==503
    assert isinstance(d['next_attempt_at'],str) and d['next_attempt_at'].endswith('Z')
    version=s.project(w['p']['id'])['version']
    rotated=w['admin'].put('/api/v1/projects/'+w['p']['id']+'/webhook',json={'url':'http://127.0.0.1:9998/changed','enabled':True,'events':[],'rotate_secret':True,'version':version}).json()
    assert rotated['signing_secret']!=secret
    assert worker.once(time.time()+10000)
    assert seen[0]==seen[1]
    d=s.delivery(w['p']['id'],d['id'])
    assert d['state']=='succeeded' and d['delivered_at']
    assert d['payload']['creator']['external_user_id']=='glean-person'
    assert w['trusted'].delete('/api/v1/requests/'+r['id'],params={'version':1,'external_user_id':'glean-person'}).status_code==204
    assert s.delivery(w['p']['id'],d['id'])['payload']['request']['title']=='Offline mode'
    body=json.dumps(d['payload'])
    for private in [secret,w['k']['secret'],w['cfg'].secret,'password_hash','session_token','secret_hash']:
        assert private not in body


@pytest.mark.parametrize('status,expected',[(400,'failed'),(302,'failed'),(429,'queued'),(500,'queued'),(201,'succeeded')])
def test_delivery_failure_classification_and_manual_retry(workflow,status,expected):
    w=workflow;configured(w);s=w['app'].state.storage
    submission(w['user'],w['b'])
    worker=WebhookWorker(s,Vault(w['cfg'].secret),True,lambda *args:status)
    worker.once()
    d=s.deliveries(w['p']['id'],25,0)[0]
    assert d['state']==expected
    if expected=='failed':
        assert w['admin'].get('/api/v1/projects/'+w['p']['id']+'/deliveries/'+d['id']).status_code==200
        assert w['trusted'].post('/api/v1/projects/'+w['p']['id']+'/deliveries/'+d['id']+'/retry').status_code==403
        assert w['admin'].post('/api/v1/projects/'+w['p']['id']+'/deliveries/'+d['id']+'/retry').status_code==202
        assert s.delivery(w['p']['id'],d['id'])['state']=='queued'
    else:
        assert w['admin'].post('/api/v1/projects/'+w['p']['id']+'/deliveries/'+d['id']+'/retry').status_code==409


def test_network_retry_limit_restart_and_lease_fencing(workflow):
    w=workflow;configured(w);s=w['app'].state.storage
    submission(w['user'],w['b'])
    s=SQLiteStorage(w['cfg'].database_path,w['cfg'].base_url)
    def failed(*args):raise OSError('raw secret exception must not be persisted')
    worker=WebhookWorker(s,Vault(w['cfg'].secret),True,failed)
    first=s.claim_delivery(time.time())
    assert first and s.claim_delivery(time.time()) is None
    second=s.claim_delivery(time.time()+61)
    assert second['id']==first['id'] and second['lease_token']!=first['lease_token']
    s.finish_delivery(first['id'],first['lease_token'],'succeeded',200,None,time.time())
    assert s.delivery(w['p']['id'],first['id'])['state']=='delivering'
    s.finish_delivery(second['id'],second['lease_token'],'queued',None,'Network failure',time.time())
    for _ in range(MAX_ATTEMPTS):worker.once(time.time()+10000)
    d=s.delivery(w['p']['id'],first['id'])
    assert d['state']=='failed' and d['attempt_count']==MAX_ATTEMPTS
    assert 'raw secret' not in d['error']


def test_claim_is_exclusive_and_event_job_rollback(workflow):
    w=workflow;configured(w);s=w['app'].state.storage
    submission(w['user'],w['b'])
    with ThreadPoolExecutor(max_workers=5) as pool:
        claims=list(pool.map(lambda _:s.claim_delivery(time.time()),range(5)))
    assert sum(x is not None for x in claims)==1
    uid=s.credentials('user@example.com')['id']
    original=s._persist_event
    def fail(*a,**kw):
        original(*a,**kw)
        raise RuntimeError('transaction interrupted')
    s._persist_event=fail
    with pytest.raises(RuntimeError):s.create_request(w['b']['id'],'Should roll back','',Actor(user_id=uid))
    s._persist_event=original
    with s.transaction() as c:
        assert c.execute('SELECT count(*) FROM requests').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM request_events').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM webhook_deliveries').fetchone()[0]==1


@pytest.mark.parametrize('url',['http://169.254.169.254/latest','http://127.0.0.1/test','http://10.0.0.1/','http://[::1]/','file:///etc/passwd','http://user:pass@example.com/'])
def test_ssrf_default_block(url):
    with pytest.raises(Error):destination(url)


def test_internal_opt_in_and_metadata_stays_blocked():
    assert destination('http://127.0.0.1:9999/hook',True)[1]=='127.0.0.1'
    with pytest.raises(Error):destination('http://169.254.169.254/',True)


def test_test_webhook_settings_and_encrypted_snapshots(workflow):
    w=workflow;secret=configured(w);s=w['app'].state.storage
    response=w['admin'].post('/api/v1/projects/'+w['p']['id']+'/webhook/test')
    assert response.status_code==202
    d=s.deliveries(w['p']['id'],25,0)[0]
    assert s.delivery(w['p']['id'],d['id'])['payload']['test'] is True
    settings=w['admin'].get('/api/v1/projects/'+w['p']['id']+'/webhook').text
    assert secret not in settings
    with s.transaction() as c:
        raw=c.execute('SELECT signing_secret FROM webhook_deliveries').fetchone()[0]
        assert raw!=secret and Vault(w['cfg'].secret).reveal(raw)==secret
    assert w['anon'].get('/api/v1/projects/'+w['p']['id']+'/webhook').status_code==401


def test_real_http_signed_delivery_and_redirect_control(workflow):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    w=workflow;s=w['app'].state.storage
    received=[]
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            body=self.rfile.read(int(self.headers['Content-Length']))
            received.append((dict(self.headers),body,self.path))
            self.send_response(204)
            self.end_headers()
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Receiver)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        settings=w['admin'].put('/api/v1/projects/'+w['p']['id']+'/webhook',json={
            'url':f'http://127.0.0.1:{server.server_port}/hook?source=feedback',
            'enabled':True,'events':['request.created'],'version':1}).json()
        submission(w['user'],w['b'])
        worker=WebhookWorker(s,Vault(w['cfg'].secret),True)
        assert worker.once()
        headers,body,path=received[0]
        assert path=='/hook?source=feedback'
        assert headers['X-Feedback-Signature']=='v1='+signature(settings['signing_secret'],headers['X-Feedback-Timestamp'],body)
        assert json.loads(body)['type']=='request.created'
        assert s.deliveries(w['p']['id'],25,0)[0]['state']=='succeeded'
    finally:
        server.shutdown();server.server_close();thread.join()


def test_dns_mixed_addresses_rejected_and_ipv4_mapped_metadata(monkeypatch):
    import socket
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**kw:[(2,1,6,'',('8.8.8.8',80)),(2,1,6,'',('127.0.0.1',80))])
    with pytest.raises(Error):destination('http://example.com/')
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**kw:[(10,1,6,'',('::ffff:169.254.169.254',80,0,0))])
    with pytest.raises(Error):destination('http://metadata.example/',True)
