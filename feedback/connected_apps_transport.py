"""Read-only, public HTTPS transport with an interruptible whole-request deadline."""
import asyncio
import json
import multiprocessing
import re
import time
from urllib.parse import urlsplit
from feedback.errors import Error
from feedback.webhooks import destination, PinnedHTTP
from feedback.connected_apps_protocol import PATHS

MAX_BYTES=1024*1024
DEADLINE=10


def base_address(value):
    try:
        p=urlsplit(value)
        if any(ord(x)<32 or ord(x)==127 for x in value) or len(value)>2048 or p.scheme!='https' or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError()
        if p.port is not None and not 1<=p.port<=65535:raise ValueError()
        # Deliberately conservative path prefix; no encoded separators or dot segments.
        if not re.fullmatch(r'(?:/[A-Za-z0-9_-]+)*/?',p.path):raise ValueError()
        host=p.hostname.lower()
        if ':' in host:host='['+host+']'
        return 'https://'+host+(':'+str(p.port) if p.port and p.port!=443 else '')+p.path.rstrip('/')
    except (ValueError,TypeError):raise Error(400,'invalid_app_url','Use a public HTTPS address with a simple path and no query or credentials') from None


def decode_body(body):
    if len(body)>MAX_BYTES:raise ValueError()
    def pairs(items):
        d={}
        for k,v in items:
            if k in d:raise ValueError()
            d[k]=v
        return d
    def bad(_):raise ValueError()
    raw=json.loads(body.decode('utf8'),object_pairs_hook=pairs,parse_constant=bad)
    def walk(v,depth=0):
        if depth>12:raise ValueError()
        if isinstance(v,str):v.encode('utf8',errors='strict')
        elif isinstance(v,dict):
            for k,x in v.items():walk(k,depth+1);walk(x,depth+1)
        elif isinstance(v,list):
            for x in v:walk(x,depth+1)
    walk(raw)
    if not isinstance(raw,dict):raise ValueError()
    return raw


def _request(url,token,operation,query):
    method,path=PATHS[operation]
    parsed,address,port=destination(base_address(url)+path,False)
    conn=PinnedHTTP(parsed.hostname,address,port,True)
    try:
        conn.request(method,parsed.path,json.dumps(query).encode() if method=='POST' else None,
                     {'Authorization':'Bearer '+token,'Accept':'application/json','Content-Type':'application/json','Accept-Encoding':'identity','User-Agent':'SuggestionBox-ConnectedApps/1'})
        response=conn.getresponse()
        if response.status!=200:
            # Do not read or echo app error bodies. Redirects never receive the token.
            code={401:'app_auth_failed',403:'app_auth_failed',404:'app_not_found',409:'app_snapshot_expired',410:'app_snapshot_expired',429:'app_rate_limited'}.get(response.status,'app_unavailable')
            return {'error':code}
        if response.getheader('Content-Encoding','identity').lower()!='identity':return {'error':'app_invalid_response'}
        if response.getheader('Content-Type','').split(';')[0].strip().lower()!='application/json':return {'error':'app_invalid_response'}
        body=response.read(MAX_BYTES+1)
        return {'data':decode_body(body)}
    finally:conn.close()


def _child(pipe,url,token,operation,query):
    try:
        try:result=_request(url,token,operation,query)
        except Exception:result={'error':'app_unavailable'}
        pipe.send_bytes(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode())
    finally:pipe.close()


async def public_request(url,token,operation,query,deadline):
    ctx=multiprocessing.get_context('spawn')
    receive,send=ctx.Pipe(duplex=False)
    process=ctx.Process(target=_child,args=(send,url,token,operation,query),daemon=True)
    process.start();send.close()
    try:
        while not receive.poll():
            if time.monotonic()>=deadline:raise Error(504,'app_timeout','The app took too long. Try again.')
            if not process.is_alive():raise Error(502,'app_unavailable','The app is unavailable')
            await asyncio.sleep(.015)
        # Child only sends already bounded JSON and owns the sole writer.
        try:result=json.loads(receive.recv_bytes(MAX_BYTES*2))
        except (ValueError,OSError,EOFError):raise Error(502,'app_invalid_response','The app returned an invalid response') from None
        if 'error' in result:raise Error(502,result['error'],'The app could not complete this read. Check its connection or refresh.')
        return result['data']
    finally:
        receive.close()
        if process.is_alive():process.terminate()
        process.join(timeout=.2)
        if process.is_alive():process.kill();process.join(timeout=.2)
        process.close()


class Admission:
    """One instance-wide budget. No payload or browse-session retention."""
    def __init__(self):
        self.active=0;self.pending=0;self.connections={};self.buckets={}

    def budget(self,principal):
        minute=int(time.time()/60)
        self.buckets={k:v for k,v in self.buckets.items() if k[1]==minute}
        key=(principal['user_id'],minute)
        if self.buckets.get(key,0)>=240:raise Error(429,'app_read_limited','Too many app reads. Try again in one minute.')
        self.buckets[key]=self.buckets.get(key,0)+1

    async def run(self,key,call,check):
        deadline=time.monotonic()+DEADLINE
        if self.pending>=16:raise Error(503,'app_busy','App connections are busy. Try again shortly.')
        self.pending+=1
        acquired=False
        try:
            while self.active>=4 or self.connections.get(key,0)>=2:
                if time.monotonic()>=deadline:raise Error(503,'app_busy','App connections are busy. Try again shortly.')
                await asyncio.sleep(.02)
            self.active+=1;self.connections[key]=self.connections.get(key,0)+1;acquired=True
            check()
            async with asyncio.timeout(max(.001,deadline-time.monotonic())):
                return await call(deadline)
        except TimeoutError:raise Error(504,'app_timeout','The app took too long. Try again.') from None
        finally:
            self.pending-=1
            if acquired:
                self.active-=1;self.connections[key]-=1
                if not self.connections[key]:del self.connections[key]
