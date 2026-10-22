import asyncio
import io
import pytest
from feedback.errors import Error
from feedback.ai_mcp_protocol import decode,endpoint,schema,validate,tool_result,definition
from feedback.ai_mcp_transport import MCPClient,read_sse,envelope

TOOL={'name':'get_episode','description':'Read public episode','annotations':{'readOnlyHint':True},'inputSchema':{'type':'object','properties':{'episode_id':{'type':'string','maxLength':200}},'required':['episode_id'],'additionalProperties':False}}

@pytest.mark.parametrize('url',['http://example.com/mcp','https://u:p@example.com/mcp','https://example.com/a/../mcp','https://example.com/mcp?key=x','https://example.com/a%2fb','https://example.com/mcp#x'])
def test_endpoint_rejects(url):
    with pytest.raises(Error):endpoint(url)

def test_schema_and_arguments_are_independent():
    s=schema(TOOL['inputSchema']);validate(s,{'episode_id':'AbC'})
    for value in [{'episode_id':'ok','user_id':'other'},{'episode_id':4},{}]:
        with pytest.raises(Error):validate(s,value)
    for value in [{'$ref':'https://example.com'}, {'type':'object','additionalProperties':True},{'type':'string','pattern':'.*'}]:
        with pytest.raises(Error):schema(value)
    with pytest.raises(Error):definition(TOOL|{'annotations':{'destructiveHint':True}})

@pytest.mark.parametrize('body',[b'{"x":1,"x":2}',b'{"x":NaN}',b'[]'])
def test_strict_json(body):
    with pytest.raises(Error):decode(body)

def test_result_disagreement_and_error_not_evidence():
    assert tool_result({'content':[{'type':'text','text':'{"a":1}'}],'structuredContent':{'a':1}})=={'a':1}
    for r in [{'isError':True,'content':[]},{'content':[{'type':'image','data':'x'}]},{'content':[{'type':'text','text':'different'}],'structuredContent':{'a':1}}]:
        with pytest.raises(Error):tool_result(r)

def test_sse_and_envelope():
    raw=b': keepalive\n\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\ndata: {"jsonrpc":"2.0",\ndata: "id":3,"result":{"ok":true}}\n\n'
    assert read_sse(io.BytesIO(raw),3,65536)=={'ok':True}
    for raw in [b'data: {"jsonrpc":"2.0","id":4,"result":{}}\n\n',b'data: {"jsonrpc":"2.0","id":3,"method":"sampling/createMessage"}\n\n',b': eof\n']:
        with pytest.raises(Error):read_sse(io.BytesIO(raw),3,65536)


def test_lifecycle_no_automatic_call_and_session_cleanup():
    seen=[]
    async def transport(url,secret,payload,session,limit,deadline,method='POST'):
        seen.append((payload,session,method))
        if method=='DELETE':return {'result':{}}
        operation=payload['method']
        result=({'protocolVersion':'2025-11-25','capabilities':{'tools':{}}} if operation=='initialize' else {'tools':[TOOL]} if operation=='tools/list' else {'content':[{'type':'text','text':'ok'}]} if operation=='tools/call' else {})
        return {'result':result,'session':'session'}
    async def run():
        c=MCPClient('https://example.com/api/mcp','secret',transport=transport)
        tools=await c.discover()
        assert len(seen)==3
        assert await c.call(tools[0],{'episode_id':'abc'})=={'text':'ok'}
        await c.close()
    asyncio.run(run())
    assert seen[-1][2]=='DELETE'
    assert seen[1][1]=='session'

def test_pinned_transport_does_not_follow_redirect_or_read_error(monkeypatch):
    import feedback.ai_mcp_transport as transport
    from urllib.parse import urlsplit
    sent=[]
    class Response:
        status=302
        def getheader(self,*_):return ''
        def read(self,*_):raise AssertionError('Error body must not be read')
    class Connection:
        def __init__(self,*args):sent.append(args)
        def request(self,*args):sent.append(args)
        def getresponse(self):return Response()
        def close(self):pass
    monkeypatch.setattr(transport,'destination',lambda url,internal:(urlsplit(url),'93.184.216.34',443))
    monkeypatch.setattr(transport,'PinnedHTTP',Connection)
    out=transport._request('https://example.com/mcp','test-secret',{'jsonrpc':'2.0','id':1},None,1000,'POST')
    assert out['error']=='mcp_unavailable'
    assert len(sent)==2
    assert 'test-secret' not in str(out)

def test_dns_mixed_answer_rejected(monkeypatch):
    import socket
    from feedback.webhooks import destination
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(2,1,6,'',('93.184.216.34',443)),(2,1,6,'',('127.0.0.1',443))])
    with pytest.raises(Error):destination('https://example.com/mcp',False)

def test_protocol_mismatch_stops_before_discovery():
    seen=[]
    async def transport(*args):
        seen.append(args)
        return {'result':{'protocolVersion':'other','capabilities':{'tools':{}}}}
    with pytest.raises(Error):asyncio.run(MCPClient('https://example.com/mcp',transport=transport).discover())
    assert len(seen)==1


def test_child_process_transport_blocks_private_destination_without_exposing_token():
    from feedback.ai_mcp_transport import public_rpc
    import time
    result=asyncio.run(public_rpc('https://127.0.0.1/mcp','not-a-real-secret',{'jsonrpc':'2.0','id':1,'method':'initialize'},None,1024,time.monotonic()+5))
    assert 'error' in result
    assert 'not-a-real-secret' not in str(result)


def test_schema_keyword_type_mismatch_and_huge_numbers_fail_safely():
    from feedback.ai_mcp_protocol import schema,validate
    with pytest.raises(Error):schema({'type':'string','properties':{}})
    with pytest.raises(Error):validate({'type':'number'},10**1000)


def test_repeated_catalog_cursor_is_rejected():
    async def transport(url,secret,payload,session,limit,deadline,method='POST'):
        if payload['method']=='initialize':return {'result':{'protocolVersion':'2025-11-25','capabilities':{'tools':{}}}}
        if payload['method']=='notifications/initialized':return {'result':{}}
        cursor=payload['params'].get('cursor')
        return {'result':{'tools':[],'nextCursor':'a' if cursor in (None,'b') else 'b'}}
    with pytest.raises(Error):asyncio.run(MCPClient('https://example.com/mcp',transport=transport).discover())
