import asyncio
import json
import socket
import time
import pytest
from feedback.errors import Error
import feedback.connected_apps_transport as t

@pytest.mark.parametrize('url',['http://example.com','https://u:p@example.com','https://example.com/?x=1','https://example.com/#x','https://example.com/a/../b','https://example.com/%2fadmin','https://example.com:99999','https://example.com//x'])
def test_reject_addresses(url):
    with pytest.raises(Error):t.base_address(url)

@pytest.mark.parametrize('body',[b'{"x":1,"x":2}',b'{"x":NaN}',b'[]',b'{"x":"\\ud800"}',b'x'*(t.MAX_BYTES+1),json.dumps({'x':[[[[[[[[[[[[[[1]]]]]]]]]]]]]]}).encode()])
def test_reject_bad_json(body):
    with pytest.raises((ValueError,UnicodeError)):t.decode_body(body)

def test_mixed_dns_fails_before_connect(monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(2,1,6,'',('8.8.8.8',443)),(2,1,6,'',('127.0.0.1',443))])
    with pytest.raises(Error):t._request('https://example.com','secret','manifest',{})

def test_response_contract_and_redirect(monkeypatch):
    requests=[]
    class Response:
        status=302
        def getheader(self,k,default=None):return {'Content-Type':'application/json'}.get(k,default)
        def read(self,n):return b'{"safe":true}'
    class Conn:
        def __init__(self,*args):assert args==('example.com','8.8.8.8',443,True)
        def request(self,*args):requests.append(args)
        def getresponse(self):return Response()
        def close(self):pass
    monkeypatch.setattr(t,'destination',lambda url,internal:(t.urlsplit(url),'8.8.8.8',443))
    monkeypatch.setattr(t,'PinnedHTTP',Conn)
    assert t._request('https://example.com/api','secret','users',{'cursor':'https://evil.example'})=={'error':'app_unavailable'}
    assert len(requests)==1 and requests[0][1]=='/api/v1/users/query'
    Response.status=200
    assert t._request('https://example.com/api','secret','manifest',{})=={'data':{'safe':True}}

def hanging_child(pipe,*args):
    time.sleep(30)

def test_deadline_and_cancellation_reap_process(monkeypatch):
    import multiprocessing
    monkeypatch.setattr(t,'_child',hanging_child)
    async def run():
        with pytest.raises(Error) as e:await t.public_request('https://example.com','secret','manifest',{},time.monotonic()+.1)
        assert e.value.code=='app_timeout'
        task=asyncio.create_task(t.public_request('https://example.com','secret','manifest',{},time.monotonic()+30))
        await asyncio.sleep(.05);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(run())
    assert not multiprocessing.active_children()

def test_admission_is_shared_and_cancellable():
    async def run():
        a=t.Admission();active=0;peak=0;per={};peaks={}
        async def call(key,deadline):
            nonlocal active,peak
            active+=1;peak=max(peak,active);per[key]=per.get(key,0)+1;peaks[key]=max(peaks.get(key,0),per[key])
            await asyncio.sleep(.01)
            active-=1;per[key]-=1
        await asyncio.gather(*(a.run(str(i%3),lambda d,k=str(i%3):call(k,d),lambda:None) for i in range(12)))
        assert peak<=4 and max(peaks.values())<=2 and a.active==a.pending==0
    asyncio.run(run())
