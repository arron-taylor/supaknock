"""Concurrent transcript reads against real storage and deterministic barriers."""
import asyncio
import json
import time
import pytest

from feedback.ai_generation import AIGeneration
from feedback.ai_mcp import MCPTools
from feedback.ai_providers import GenerationResult, ToolCall, Usage
from feedback.ai_retrieval import RetrievalService
from feedback.ai_tools import AIToolDispatcher
from feedback.errors import Error
from test_ai_chat_isolation import ai_chat, FixtureVault, FixedEmbeddings
from test_ai_mcp_generation import setup, PodcastClient
from test_ai_tools import SequenceProvider


def test_search_batch_joins_in_request_order_and_reuses_duplicates(ai_chat):
    env=ai_chat;_,job,mat=setup(env)
    class Concurrent(PodcastClient):
        active=0;peak=0;finished=[];queries=[]
        async def call(self,tool,args):
            if tool['name']!='search_transcript':return await super().call(tool,args)
            query=args['query'];self.queries.append(query)
            self.active+=1;self.peak=max(self.peak,self.active)
            try:
                if query=='first':
                    self.second_done=asyncio.Event()
                    await asyncio.wait_for(self.second_done.wait(),1)
                elif query=='second':self.second_done.set()
                result=await super().call(tool,args)
                result['matches'][0]['segment_ids']=[query]
                result['matches'][0]['text']+=' '+query
                self.finished.append(query)
                return result
            finally:self.active-=1
        async def close(self):
            assert self.active==0
            assert self.peak==2
            assert self.finished==['warmup','second','first','third']
            assert self.queries==['warmup','first','second','third']
    remote=MCPTools(env['store'],FixtureVault(),client_factory=Concurrent)
    retrieval=RetrievalService(env['store'],FixedEmbeddings())
    dispatcher=AIToolDispatcher(env['store'],retrieval,mcp=remote)
    search=next(t for t in dispatcher.schemas(mat) if 'query' in t.parameters['properties'])
    queries=['warmup','first','second','third','third']
    calls=tuple(ToolCall(str(n),search.name,{'query':q}) for n,q in enumerate(queries))
    def answer(messages):
        replies=[m for m in messages if m.role=='tool']
        assert [m.tool_call_id for m in replies]==[str(n) for n in range(5)]
        data=[json.loads(m.content) for m in replies]
        assert [d['passages'][0]['text'].split()[-1] for d in data[:-1]]==queries[:-1]
        assert data[-1]['previous_source_ids']==[data[-2]['passages'][0]['id']]
        return GenerationResult(json.dumps({'status':'answered','body':'The host recommends taking a walk every morning.',
                                           'citation_ids':[data[1]['passages'][0]['id']]}),(),Usage(10,10,20),'stop')
    provider=SequenceProvider([GenerationResult('',calls,Usage(10,10,20),'tool_calls'),answer])
    gen=AIGeneration(env['store'],FixtureVault(),retrieval,provider_factory=lambda _:provider,tool_dispatcher=dispatcher)
    result=asyncio.run(gen.generate(job['id'],job['lease_token'],mat['question']))
    reply=env['store'].ai_commit_reply(job['id'],job['lease_token'],result['body'],result['citation_ids'],result['knowledge_version'])
    assert reply['tool_sources'][0]['segment_ids']==['first']
    assert not remote.attempts
    assert len(provider.calls)==3  # initial tools, answer, unchanged evidence check


@pytest.mark.parametrize('failure',['revision','revoked','cancelled'])
def test_failed_or_cancelled_batch_drains_sibling_before_cleanup(ai_chat,failure):
    env=ai_chat;s=env['store'];row,job,mat=setup(env)
    started=asyncio.Event();drained=[]
    class Interrupted(PodcastClient):
        async def call(self,tool,args):
            query=args.get('query')
            if query=='slow':
                started.set()
                try:await asyncio.Event().wait()
                finally:drained.append('slow')
            if query=='failure':
                await started.wait()
                if failure=='revision':return {'contract_version':1,'status':'revision_changed'}
                if failure=='revoked':s.mcp_remove(env['admin'],row['id'],1)
                if failure=='cancelled':await asyncio.Event().wait()
            return await super().call(tool,args)
        async def close(self):assert drained==['slow']
    remote=MCPTools(s,FixtureVault(),client_factory=Interrupted)
    dispatcher=AIToolDispatcher(s,RetrievalService(s,FixedEmbeddings()),mcp=remote)
    search=next(t for t in dispatcher.schemas(mat) if 'query' in t.parameters['properties'])
    async def run():
        await dispatcher.execute_async(job['id'],job['lease_token'],ToolCall('warm',search.name,{'query':'warm'}))
        calls=[ToolCall(q,search.name,{'query':q}) for q in ['slow','failure']]
        assert list(dispatcher.batches(job['id'],job['lease_token'],calls))==[calls]
        task=asyncio.create_task(dispatcher.execute_batch(job['id'],job['lease_token'],calls))
        await started.wait()
        if failure=='cancelled':task.cancel()
        with pytest.raises(asyncio.CancelledError if failure=='cancelled' else Error):
            await asyncio.wait_for(task,1)
        await remote.close(job['id'],job['lease_token'])
        assert not remote.attempts
    asyncio.run(run())
    with s.transaction() as c:
        assert c.execute("SELECT count(*) FROM chat_messages WHERE sender='ai'").fetchone()[0]==0


def test_parallel_budget_counts_combined_elapsed_and_drains_timeouts():
    remote=MCPTools(None,None)
    async def run():
        state={'elapsed':24.8};start=time.monotonic()
        async def read():
            with pytest.raises(TimeoutError):
                async with remote._budget(state):await asyncio.sleep(2)
        await asyncio.gather(read(),read())
        duration=time.monotonic()-start
        assert .07<duration<.3
        assert 25<=state['elapsed']<25.3
        assert not state['read_timeouts']
    asyncio.run(run())


def test_parallel_budget_releases_unused_time_to_remaining_read():
    remote=MCPTools(None,None)
    async def run():
        state={'elapsed':24.7}
        async def short():
            async with remote._budget(state):await asyncio.sleep(.03)
        async def long():
            async with remote._budget(state):await asyncio.sleep(.22)
        await asyncio.gather(short(),long())
        assert 24.94<state['elapsed']<25
    asyncio.run(run())
