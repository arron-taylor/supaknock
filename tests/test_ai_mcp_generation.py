import asyncio
import json
import time
from uuid import uuid4
import pytest
from feedback.ai_mcp import MCPTools
from feedback.ai_mcp_protocol import definition
from feedback.ai_mcp_transport import MCPClient
from feedback.ai_generation import AIGeneration
from feedback.ai_tools import AIToolDispatcher
from feedback.ai_retrieval import RetrievalService
from feedback.ai_providers import GenerationResult,Usage
from test_ai_chat_isolation import ai_chat,conversation,FixtureVault,FixedEmbeddings
from test_ai_tools import enable_tools,SequenceProvider,called


def catalog():
    out=[]
    for name,props in [('get_episode',{}),('search_transcript',{'query':{'type':'string'}}),('get_transcript_window',{'start_seconds':{'type':'number'},'end_seconds':{'type':'number'}})]:
        fields={'episode_id':{'type':'string'},**props}
        if name!='get_episode':fields['transcript_revision']={'type':'string'}
        out.append(definition({'name':name,'description':'Read public podcast evidence','annotations':{'readOnlyHint':True,'destructiveHint':False},'inputSchema':{'type':'object','properties':fields,'required':list(fields),'additionalProperties':False}}))
    return out


class PodcastClient:
    calls=[]
    closed=0
    def __init__(self,url,secret,check):self.check=check
    async def discover(self):self.check();return catalog()
    async def call(self,tool,args):
        self.check();self.calls.append((tool['name'],args))
        if tool['name']=='get_episode':return {'contract_version':1,'episode_id':'canonical','requested_episode_id':'alias','title':'Episode title','url':'https://podcasts.example/episode/canonical','transcript':{'revision':'r1'}}
        assert args['episode_id']=='canonical' and args['transcript_revision']=='r1'
        return {'contract_version':1,'episode_id':'canonical','transcript_revision':'r1','matches':[{'text':'The host recommends taking a walk every morning.','segment_ids':['s1'],'start_seconds':42,'end_seconds':49,'timing_kind':'aligned','url':'https://podcasts.example/episode/canonical?t=42'}]}
    async def close(self):type(self).closed+=1


class WirePodcastClient(MCPClient):
    def __init__(self,url,secret,check):
        delegate=PodcastClient(url,secret,check)
        async def transport(url,secret,payload,session,limit,deadline,method='POST'):
            if method=='DELETE':
                await delegate.close();return {'result':{}}
            operation=payload['method']
            if operation=='initialize':result={'protocolVersion':'2025-11-25','capabilities':{'tools':{}}}
            elif operation=='notifications/initialized':result={}
            elif operation=='tools/list':result={'tools':[{k:v for k,v in t.items() if k!='digest'} for t in catalog()]}
            else:
                tool=next(t for t in catalog() if t['name']==payload['params']['name'])
                data=await delegate.call(tool,payload['params']['arguments'])
                result={'structuredContent':data,'content':[{'type':'text','text':json.dumps(data)}]}
            return {'result':result,'session':'fixture-session'}
        super().__init__(url,secret,check=check,transport=transport)


def setup(env):
    s,p=env['store'],env['admin'];enable_tools(env,[])
    row=s.mcp_save(p,None,0,label='Podcast',url='https://podcasts.example/mcp',enabled=True,protected_token=None,link_origins=['https://podcasts.example'])
    tools=catalog();s.mcp_discovered(p,row['id'],1,tools)
    s.mcp_bind(p,env['project'],0,'tools',[{'connection_id':row['id'],'connection_version':1,'context_kind':'podcast_episode','tools':[{'name':t['name'],'digest':t['digest']} for t in tools]}])
    # A podcast-only installation has neither local weights nor approved support
    # knowledge. Exercise claiming and generating under that actual readiness.
    with s.transaction(True) as c:
        c.execute("UPDATE ai_runtime SET state='not_downloaded',active_manifest=NULL")
        c.execute('UPDATE ai_sources SET approved=0')
    customer=conversation(env)
    s.chat_send(customer['conversation']['id'],customer['principal'],str(uuid4()),'What does the host recommend?',context={'version':1,'resource':{'kind':'podcast_episode','id':'alias'}})
    job=s.ai_claim_job(time.time(),120,kinds=('turn',));mat=s.ai_job_material(job['id'],job['lease_token'])
    return row,job,mat


@pytest.mark.parametrize("wire",[False,True])
def test_podcast_turn_resolves_context_retrieves_and_commits_timed_citation(ai_chat,wire):
    env=ai_chat;s=env['store'];row,job,mat=setup(env)
    remote=MCPTools(s,FixtureVault(),client_factory=WirePodcastClient if wire else PodcastClient)
    retrieval=RetrievalService(s,FixedEmbeddings());dispatcher=AIToolDispatcher(s,retrieval,mcp=remote)
    search=next(t for t in dispatcher.schemas(mat) if 'query' in t.parameters['properties'])
    assert 'episode_id' not in search.parameters['properties']
    def answer(messages):
        data=json.loads(messages[-1].content);passage=data['passages'][0]
        assert passage['start_seconds']==42
        return GenerationResult(json.dumps({'status':'answered','body':'The host recommends taking a walk every morning.','citation_ids':[passage['id']]}),(),Usage(10,10,20),'stop')
    provider=SequenceProvider([called(search.name,{'query':'recommendation'}),answer])
    gen=AIGeneration(s,FixtureVault(),retrieval,provider_factory=lambda _:provider,tool_dispatcher=dispatcher)
    result=asyncio.run(gen.generate(job['id'],job['lease_token'],mat['question']))
    reply=s.ai_commit_reply(job['id'],job['lease_token'],result['body'],result['citation_ids'],result['knowledge_version'])
    assert reply['tool_sources'][0]['start_seconds']==42
    assert reply['tool_sources'][0]['resource_id']=='canonical'
    assert reply['context']['resource']['id']=='alias'
    assert not remote.attempts and PodcastClient.closed
    assert reply['citations']==[]


def test_model_cannot_override_episode_or_revision(ai_chat):
    from feedback.ai_providers import ToolCall
    from feedback.errors import Error
    env=ai_chat;_,job,mat=setup(env);remote=MCPTools(env['store'],FixtureVault(),client_factory=PodcastClient)
    tool=next(t for t in remote.schemas(mat) if 'query' in t.parameters['properties'])
    with pytest.raises(Error):remote.validate(ToolCall('bad',tool.name,{'query':'q','episode_id':'another'}),mat)
    with pytest.raises(Error):remote.validate(ToolCall('bad',tool.name,{'query':'q','transcript_revision':'forged'}),mat)


def test_topic_refusal_makes_no_mcp_or_generation_calls(ai_chat):
    from feedback.ai_topic_service import TopicGate
    from test_ai_topics import ready_topics,Decision,TopicVault
    env=ai_chat;ready_topics(env)
    _,job,mat=setup(env)
    class NeverConnect:
        def __init__(self,*args,**kwargs):raise AssertionError('Refusal must not connect to MCP')
    def never_generate(_):raise AssertionError('Refusal must not call the answer provider')
    remote=MCPTools(env['store'],FixtureVault(),client_factory=NeverConnect)
    retrieval=RetrievalService(env['store'],FixedEmbeddings())
    gate=TopicGate(env['store'],TopicVault(),provider_factory=lambda _:Decision(.01))
    gen=AIGeneration(env['store'],FixtureVault(),retrieval,provider_factory=never_generate,topic_gate=gate,tool_dispatcher=AIToolDispatcher(env['store'],retrieval,mcp=remote))
    result=asyncio.run(gen.generate(job['id'],job['lease_token'],mat['question']))
    assert result['status']=='refuse' and not result['citation_ids']
    with env['store'].transaction() as c:
        assert c.execute('SELECT count(*) FROM ai_mcp_calls').fetchone()[0]==0


def test_connection_revocation_discards_inflight_result(ai_chat):
    from feedback.errors import Error
    from feedback.ai_providers import ToolCall
    env=ai_chat;s=env['store'];row,job,mat=setup(env)
    class Revoked(PodcastClient):
        async def call(self,tool,args):
            result=await super().call(tool,args)
            s.mcp_remove(env['admin'],row['id'],1)
            return result
    remote=MCPTools(s,FixtureVault(),client_factory=Revoked)
    tool=next(t for t in remote.schemas(mat) if not t.parameters['properties'])
    with pytest.raises(Error):asyncio.run(remote.execute(job['id'],job['lease_token'],ToolCall('revoked',tool.name,{})))
    asyncio.run(remote.close(job['id'],job['lease_token']))
    with s.transaction() as c:
        assert c.execute("SELECT count(*) FROM chat_messages WHERE sender='ai'").fetchone()[0]==0


def test_episode_evidence_rejects_wrong_resource_revision_and_unsafe_time():
    from feedback.ai_mcp import evidence
    from feedback.errors import Error
    item={'label':'Podcasts','context_kind':'podcast_episode','tool':{'name':'search_transcript'}}
    resolved={'episode_id':'a','transcript_revision':'r','title':'Episode'}
    base={'contract_version':1,'episode_id':'a','transcript_revision':'r','matches':[{'text':'A real quote','segment_ids':['s'],'start_seconds':3,'end_seconds':5,'timing_kind':'aligned','url':'https://evil.example/x'}]}
    assert evidence(base,item,['https://podcasts.example'],resolved)[0]['url'] is None
    for changed in [base|{'episode_id':'other'},base|{'transcript_revision':'old'},base|{'matches':[base['matches'][0]|{'start_seconds':-1}]}]:
        with pytest.raises(Error):evidence(changed,item,[],resolved)


@pytest.mark.parametrize('scenario', ['twenty_citations', 'correction', 'overflow', 'duplicate', 'cached'])
def test_bounded_sources_answer_without_handoff(ai_chat, scenario):
    """Actual generation/check/commit, including >16 KiB of saved sources."""
    from test_ai_chat_isolation import configure
    env=ai_chat;s=env['store'];configure(env,daily_token_limit=1000000);_,job,mat=setup(env)
    class ManyPassages(PodcastClient):
        reads=0
        async def call(self,tool,args):
            result=await super().call(tool,args)
            if 'matches' in result:
                type(self).reads+=1
                count=6 if scenario in ('overflow','duplicate','cached') else 10
                offset=0 if scenario in ('duplicate','cached') else (type(self).reads-1)*count
                result['matches']=[result['matches'][0]|{'text':('An exact source sentence. '*60)+str(offset+n),
                    'segment_ids':[str(offset+n)],'start_seconds':offset+n,'end_seconds':offset+n+1} for n in range(count)]
            return result
    remote=MCPTools(s,FixtureVault(),client_factory=ManyPassages)
    retrieval=RetrievalService(s,FixedEmbeddings());dispatcher=AIToolDispatcher(s,retrieval,mcp=remote)
    search=next(t for t in dispatcher.schemas(mat) if 'query' in t.parameters['properties'])
    from feedback.ai_providers import ToolCall
    calls=tuple(ToolCall('read-'+str(n),search.name,{'query':'same' if scenario=='cached' else str(n)})
                for n in range(4 if scenario=='overflow' else 2))
    expected=6 if scenario in ('duplicate','cached') else 20
    def answer(messages):
        sources=[p for m in messages if m.role=='tool' for p in json.loads(m.content)['passages']]
        assert len(sources)==expected
        last=json.loads(messages[-1].content)
        if scenario=='overflow':assert last['source_limit_reached'] and last['coverage']=='partial'
        if scenario in ('duplicate','cached'):assert len(last['previous_source_ids'])==6
        return GenerationResult(json.dumps({'status':'answered','body':'An exact source sentence.',
            'citation_ids':[p['id'] for p in sources[:1 if scenario=='correction' else expected]]}),(),Usage(10,10,20),'stop')
    class CheckedProvider(SequenceProvider):
        reviews=0
        async def generate(self,key,model,messages,**kwargs):
            data=json.loads(messages[-1].content)
            if data.get('operation')=='evidence_correction':
                assert len(data['passages'])==20
                return GenerationResult(json.dumps({'status':'answered','body':'An exact source sentence.',
                    'citation_ids':[data['passages'][0]['id']]}),(),Usage(10,10,20),'stop')
            if data.get('operation')=='evidence_check':
                self.reviews+=1
                if scenario=='correction' and self.reviews==1:
                    return GenerationResult(json.dumps({'assessment':'Fixture requests a revision.', 'supported':False,
                        'issues':[{'quote':'exact source','reason':'unsupported'}]}),(),Usage(10,10,20),'stop')
            return await super().generate(key,model,messages,**kwargs)
    provider=CheckedProvider([GenerationResult('',calls,Usage(10,10,20),'tool_calls'),answer])
    gen=AIGeneration(s,FixtureVault(),retrieval,provider_factory=lambda _:provider,tool_dispatcher=dispatcher)
    result=asyncio.run(gen.generate(job['id'],job['lease_token'],mat['question']))
    assert result['status']=='answered'
    reply=s.ai_commit_reply(job['id'],job['lease_token'],result['body'],result['citation_ids'],result['knowledge_version'])
    assert len(reply['tool_sources'])==(1 if scenario=='correction' else expected)
    if scenario=='cached':assert ManyPassages.reads==1
    with s.transaction() as c:
        assert c.execute('SELECT support_mode FROM chat_conversations').fetchone()[0]=='ai'
        assert c.execute('SELECT state FROM ai_jobs WHERE id=?',(job['id'],)).fetchone()[0]=='succeeded'


def test_cached_read_rechecks_revoked_connection(ai_chat):
    from feedback.ai_providers import ToolCall
    from feedback.errors import Error
    env=ai_chat;s=env['store'];row,job,mat=setup(env)
    remote=MCPTools(s,FixtureVault(),client_factory=PodcastClient)
    search=next(t for t in remote.schemas(mat) if 'query' in t.parameters['properties'])
    async def run():
        await remote.execute(job['id'],job['lease_token'],ToolCall('first',search.name,{'query':'walk'}))
        s.mcp_remove(env['admin'],row['id'],1)
        with pytest.raises(Error):
            await remote.execute(job['id'],job['lease_token'],ToolCall('second',search.name,{'query':'walk'}))
        await remote.close(job['id'],job['lease_token'])
    asyncio.run(run())


def test_twenty_excerpt_protocol_and_output_boundaries():
    from feedback.ai_mcp import evidence
    from feedback.ai_mcp_protocol import tool_result
    from feedback.ai_output import output_schema
    from feedback.ai_generation import validate_result
    from feedback.ai_providers import ProviderError
    from feedback.errors import Error
    item={'label':'Podcast','context_kind':'podcast_episode','tool':{'name':'search_transcript'}}
    resolved={'episode_id':'episode','transcript_revision':'r','title':'Episode'}
    sample={'text':'\\'*1500,'segment_ids':['s'],'timing_kind':'unavailable'}
    result={'contract_version':1,'episode_id':'episode','transcript_revision':'r','matches':[sample for _ in range(20)]}
    wire={'structuredContent':result,'content':[{'type':'text','text':json.dumps(result)}]}
    assert len(json.dumps(wire).encode())>65536
    assert len(evidence(tool_result(wire),item,[],resolved))==20
    with pytest.raises(Error):evidence(result|{'matches':result['matches']+[sample]},item,[],resolved)
    ids=['S'+str(n) for n in range(21)]
    draft={'status':'answered','body':'A supported answer.','citation_ids':ids[:20]}
    assert len(validate_result(json.dumps(draft),ids)['citation_ids'])==20
    with pytest.raises(ProviderError):validate_result(json.dumps(draft|{'citation_ids':ids}),ids)
    assert output_schema('answer',ids[:20])['properties']['citation_ids']['maxItems']==20
