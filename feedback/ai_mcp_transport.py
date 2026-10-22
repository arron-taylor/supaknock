"""Pinned public HTTPS MCP client. No redirects, ambient credentials or server callbacks."""
import asyncio
import json
import multiprocessing
import re
import time
import threading
from contextlib import asynccontextmanager
from feedback.ai_mcp_protocol import VERSION, decode, encode, endpoint, fail, definition, tool_result
from feedback.errors import Error
from feedback.ai_limits import MAX_TOOL_RESULT_BYTES
from feedback.webhooks import destination, PinnedHTTP


def envelope(value, identity):
    if value.get('jsonrpc')!='2.0':fail()
    if 'method' in value:
        # Server requests (sampling, roots, elicitation, etc.) are never executed.
        if 'id' in value:fail('mcp_unsupported','Server callbacks are unsupported')
        if value['method'] not in ('notifications/progress','notifications/message','notifications/tools/list_changed'):fail()
        return None
    if type(value.get('id')) is not int or value['id']!=identity or ('result' in value)==('error' in value):fail()
    if 'error' in value:fail('mcp_rpc_error','The tool server could not complete the operation')
    if not isinstance(value['result'],dict):fail()
    return value['result']


def read_sse(response, identity, limit):
    total=events=0;data=[]
    while True:
        raw=response.readline(limit-total+1)
        if not raw:fail('mcp_incomplete','The tool stream ended before its result')
        total+=len(raw)
        if total>limit:fail('mcp_limit')
        try:line=raw.decode('utf8').rstrip('\r\n')
        except UnicodeError:fail()
        if not line:
            events+=1
            if events>128:fail('mcp_limit')
            if data:
                result=envelope(decode('\n'.join(data).encode(),limit),identity)
                if result is not None:return result
                data=[]
        elif line.startswith('data:'):data.append(line[5:].removeprefix(' '))
        elif line.startswith(':') or line.startswith(('event:','id:','retry:')):continue
        else:fail()


def _request(url, secret, payload, session, limit, method):
    parsed,address,port=destination(endpoint(url),False)
    conn=PinnedHTTP(parsed.hostname,address,port,True)
    try:
        headers={'Accept':'application/json, text/event-stream','Accept-Encoding':'identity','Content-Type':'application/json',
                 'MCP-Protocol-Version':VERSION,'User-Agent':'SupaKnock-MCP/1'}
        if secret:headers['Authorization']='Bearer '+secret
        if session:headers['Mcp-Session-Id']=session
        conn.request(method,parsed.path or '/',encode(payload,32768) if payload is not None else None,headers)
        response=conn.getresponse()
        if response.status not in (200,202,204):
            code={401:'mcp_auth',403:'mcp_auth',404:'mcp_session_expired',429:'mcp_rate_limited'}.get(response.status,'mcp_unavailable')
            retry=response.getheader('Retry-After','')
            return {'error':code,'retry_after':min(int(retry),3600) if retry.isdigit() else 60}
        sid=response.getheader('Mcp-Session-Id')
        if sid is not None and not re.fullmatch(r'[!-~]{1,256}',sid):fail()
        if session and sid and sid!=session:fail()
        if response.getheader('Content-Encoding','identity').lower()!='identity':fail()
        if method=='DELETE' or (payload and 'id' not in payload):
            if response.status not in (202,204):fail()
            return {'result':{},'session':sid or session}
        if response.status!=200:fail()
        kind=response.getheader('Content-Type','').split(';')[0].strip().lower()
        if kind=='application/json':result=envelope(decode(response.read(limit+1),limit),payload['id'])
        elif kind=='text/event-stream':result=read_sse(response,payload['id'],limit)
        else:fail('mcp_content')
        if result is None:fail()
        return {'result':result,'session':sid or session}
    finally:conn.close()


def _child(pipe,*args):
    try:
        try:result=_request(*args)
        except Error as error:result={'error':error.code}
        except Exception:result={'error':'mcp_unavailable'}
        pipe.send_bytes(encode(result,524288))
    finally:pipe.close()


async def public_rpc(url, secret, payload, session, limit, deadline, method='POST'):
    ctx=multiprocessing.get_context('spawn');receive,send=ctx.Pipe(duplex=False)
    process=ctx.Process(target=_child,args=(send,url,secret,payload,session,limit,method),daemon=True)
    process.start();send.close()
    try:
        while not receive.poll():
            if time.monotonic()>=deadline:fail('mcp_timeout','The connected tool took too long')
            if not process.is_alive():fail('mcp_unavailable')
            await asyncio.sleep(.015)
        try:result=decode(receive.recv_bytes(524288),524288)
        except (OSError,EOFError):fail()
        return result
    finally:
        receive.close()
        if process.is_alive():process.terminate()
        process.join(timeout=.2)
        if process.is_alive():process.kill();process.join(timeout=.2)
        process.close()


class MCPClient:
    def __init__(self,url,secret='',*,transport=public_rpc,check=lambda:None):
        self.url=endpoint(url);self.secret=secret;self.transport=transport;self.check=check
        self.session=None;self.counter=0;self.elapsed=0

    async def rpc(self,method,params=None,*,notification=False,deadline=None):
        self.check();self.counter+=1
        start=time.monotonic();deadline=min(deadline or start+10,start+10)
        payload={'jsonrpc':'2.0','method':method,'params':params or {}}
        if not notification:payload['id']=self.counter
        try:
            async with asyncio.timeout(max(.001,deadline-start)):
                reply=await self.transport(self.url,self.secret,payload,self.session,262144 if method=='tools/list' else MAX_TOOL_RESULT_BYTES,deadline)
        except TimeoutError:fail('mcp_timeout','The connected tool took too long')
        finally:self.elapsed+=time.monotonic()-start
        self.check()
        if reply.get('error'):
            err=Error(502,reply['error'],'The connected tool could not complete this read')
            err.retry_after=reply.get('retry_after',60)
            raise err
        self.session=reply.get('session')
        return reply['result']

    async def discover(self):
        deadline=time.monotonic()+10
        init=await self.rpc('initialize',{'protocolVersion':VERSION,'capabilities':{},'clientInfo':{'name':'SupaKnock','version':'1'}},deadline=deadline)
        if init.get('protocolVersion')!=VERSION or not isinstance(init.get('capabilities'),dict) or 'tools' not in init['capabilities']:fail('mcp_protocol','Use an MCP server supporting 2025-11-25 tools over Streamable HTTP')
        await self.rpc('notifications/initialized',notification=True,deadline=deadline)
        tools=[];seen=set();cursors=set();cursor=None;wire=0
        for _ in range(10):
            page=await self.rpc('tools/list',{'cursor':cursor} if cursor else {},deadline=deadline)
            wire+=len(encode(page));items=page.get('tools')
            if wire>262144 or not isinstance(items,list) or len(tools)+len(items)>100:fail('mcp_limit')
            for item in items:
                try:tool=definition(item)
                except Error as error:
                    if isinstance(item,dict) and isinstance(item.get('name'),str) and len(item['name'])<=128:
                        tool={'name':item['name'],'unsupported':error.code,'reason':error.message}
                    else:raise
                if tool['name'] in seen:fail()
                seen.add(tool['name']);tools.append(tool)
            next_cursor=page.get('nextCursor')
            if next_cursor is None:return tools
            if not isinstance(next_cursor,str) or not 1<=len(next_cursor)<=1000 or next_cursor in cursors:fail()
            cursors.add(next_cursor);cursor=next_cursor
        fail('mcp_limit','The tool catalog is too large to approve')

    async def call(self,tool,arguments):
        from feedback.ai_mcp_protocol import validate
        validate(tool['inputSchema'],arguments);encode(arguments,16384)
        result=await self.rpc('tools/call',{'name':tool['name'],'arguments':arguments})
        return tool_result(result,tool.get('outputSchema'))

    async def close(self):
        if not self.session:return
        try:
            async with asyncio.timeout(1):
                await self.transport(self.url,self.secret,None,self.session,1024,time.monotonic()+1,'DELETE')
        except Exception:pass
        finally:self.session=None


class Admission:
    def __init__(self):
        self.active=0;self.pending=0;self.connections={};self.buckets={};self.cooldowns={}
        self.lock=threading.Lock()

    @asynccontextmanager
    async def slot(self,connection,project,visitor):
        start=time.monotonic();acquired=False
        with self.lock:
            if self.pending>=16:raise Error(503,'mcp_busy','Connected tools are busy; try again shortly')
            self.pending+=1
        try:
            while not acquired:
                with self.lock:
                    minute=int(time.time()/60)
                    self.buckets={k:v for k,v in self.buckets.items() if k[-1]==minute}
                    self.cooldowns={k:v for k,v in self.cooldowns.items() if v>time.time()}
                    if connection in self.cooldowns:raise Error(429,'mcp_rate_limited','The connected tool is cooling down')
                    entries=[('connection',connection,240),('project',project,120),('visitor',visitor,30)]
                    for kind,identity,limit in entries:
                        if self.buckets.get((kind,identity,minute),0)>=limit:raise Error(429,'mcp_admission_limit','Connected tool rate limit reached; try again shortly')
                    if self.active<4 and self.connections.get(connection,0)<2:
                        self.active+=1;self.connections[connection]=self.connections.get(connection,0)+1;acquired=True
                        for kind,identity,_ in entries:
                            key=(kind,identity,minute);self.buckets[key]=self.buckets.get(key,0)+1
                if not acquired:
                    if time.monotonic()-start>=10:fail('mcp_timeout')
                    await asyncio.sleep(.02)
            yield
        except Error as error:
            if error.code=='mcp_rate_limited':
                with self.lock:self.cooldowns[connection]=time.time()+min(max(getattr(error,'retry_after',60),1),3600)
            raise
        finally:
            with self.lock:
                self.pending-=1
                if acquired:self.active-=1;self.connections[connection]-=1
