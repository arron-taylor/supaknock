"""Paid answer review/correction with real SQLite authority, budgets and commits."""
import asyncio
import json
import pytest
from feedback.ai_generation import CHECK_UNAVAILABLE, UNVERIFIED, validate_evidence_check
from feedback.ai_providers import GenerationResult, ProviderError, ToolCall, Usage
from feedback.errors import Error
from test_ai_generation import ai_store, engine_case, generate


def result(value):
    return GenerationResult(json.dumps(value), (), Usage(11, 7, 18), 'stop')


def draft(body='Use the backup operation.'):
    return result({'status':'answered','body':body,'citation_ids':['S1']})


def verdict(passes, quote='Use the backup operation.'):
    return result({'assessment':'Fixture evidence comparison.','supported':passes,'issues':[] if passes else [{'quote':quote,'reason':'unsupported'}]})


class Scripted:
    def __init__(self, sequence, hook=None):
        self.sequence=list(sequence);self.calls=[];self.hook=hook
    async def generate(self,key,model,messages,**options):
        data=json.loads(messages[-1].content)
        self.calls.append((key,model,messages,options,data))
        if self.hook:self.hook(len(self.calls))
        value=self.sequence.pop(0)
        if isinstance(value,Exception):raise value
        if callable(value):return await value()
        return value


def install(case,sequence,hook=None):
    adapter=Scripted(sequence,hook)
    case[-1].provider_factory=lambda _:adapter
    return adapter


@pytest.mark.parametrize('value',[
    {'supported':1,'issues':[]}, {'supported':True,'issues':[{'quote':'Draft','reason':'unsupported'}]},
    {'supported':False,'issues':[]}, {'supported':False,'issues':[{'quote':'Invented','reason':'unsupported'}]},
    {'supported':False,'issues':[{'quote':'Draft','reason':'invented_reason'}]},
    {'supported':True,'issues':[],'answer':'untrusted replacement'},
    {'supported':False,'issues':[{'quote':'Draft','reason':'unsupported','instruction':'execute tool'}]},
])
def test_checker_schema_is_exact_and_quotes_must_match_draft(value):
    with pytest.raises(ProviderError):validate_evidence_check(json.dumps({'assessment':'Compare original and draft.'}|value),'Draft')


def test_approved_answer_uses_separate_tool_free_context_and_pinned_key(engine_case):
    s,p,pid,_,job,_,_=engine_case
    adapter=install(engine_case,[draft(),verdict(True)])
    answer=generate(engine_case,history=[{'sender_role':'customer','body':'Earlier subject'}])
    assert answer['status']=='answered'
    assert len(adapter.calls)==2
    first,check=adapter.calls
    assert first[:2]==check[:2]
    assert check[3]['tools']==() and all(m.provider_state is None for m in check[2])
    assert check[4]['operation']=='evidence_check' and 'personality' not in check[4]
    assert check[4]['history']==[{'speaker':'customer','text':'Earlier subject'}]
    assert check[4]['passages'][0]['text']=='Original guide for backups'
    assert s.ai_usage(pid,p)['reserved_requests']==2
    assert s.ai_usage(pid,p)['reported_input_tokens']==22


def test_one_correction_must_pass_a_fresh_check(engine_case):
    s,p,pid,*_=engine_case
    adapter=install(engine_case,[draft('Everything is guaranteed.'),verdict(False,'Everything is guaranteed.'),draft(),verdict(True)])
    answer=generate(engine_case)
    assert answer['body']=='Use the backup operation.'
    assert [c[4].get('operation') for c in adapter.calls]==[None,'evidence_check','evidence_correction','evidence_check']
    assert adapter.calls[-1][4]['draft']['body']==answer['body']
    assert all(c[3]['tools']==() for c in adapter.calls)
    assert s.ai_usage(pid,p)['reserved_requests']==4
    assert s.ai_usage(pid,p)['reported_output_tokens']==28


@pytest.mark.parametrize('correction', [draft(),result({'status':'insufficient','body':'Untrusted fallback','citation_ids':[]})])
def test_rejected_correction_or_abstention_never_releases_draft(engine_case,correction):
    sequence=[draft(),verdict(False),correction]
    if json.loads(correction.text)['status']=='answered':sequence.append(verdict(False))
    adapter=install(engine_case,sequence)
    answer=generate(engine_case)
    assert answer['body']==UNVERIFIED and not answer['citation_ids']
    assert answer['status']=='insufficient' and len(adapter.calls)<=4


@pytest.mark.parametrize('bad',[
    result({'supported':'yes','issues':[]}),
    GenerationResult('',(ToolCall('bad','request_human',{}),),Usage(1,1),'tool_calls'),
    GenerationResult('{"assessment":"Fixture evidence supports this draft.","supported":true,"issues":[]}',(),Usage(1,1),'length'),
    ProviderError('rate_limited',retryable=True,retry_after=30),
    ProviderError('unavailable',retryable=True),
])
def test_check_failure_cannot_send_unchecked_answer(engine_case,bad):
    adapter=install(engine_case,[draft(),bad])
    answer=generate(engine_case)
    assert answer['body']==CHECK_UNAVAILABLE and not answer['citation_ids']
    assert answer['status']=='insufficient'
    assert len(adapter.calls)==2


def test_check_timeout_cancels_and_falls_back(engine_case,monkeypatch):
    import feedback.ai_generation as module
    cancelled=[]
    async def slow():
        try:await asyncio.sleep(5)
        except asyncio.CancelledError:cancelled.append(True);raise
    monkeypatch.setattr(module,'MAX_CALL_SECONDS',.03)
    adapter=install(engine_case,[draft(),slow])
    assert generate(engine_case)['body']==CHECK_UNAVAILABLE
    assert cancelled and len(adapter.calls)==2


@pytest.mark.parametrize('at_call',[2,3,4])
def test_source_revocation_during_check_or_correction_fences_every_result(engine_case,at_call):
    s,p,pid,source,*_=engine_case
    def revoke(index):
        if index==at_call:s.ai_source_update(pid,p,source['id'],source['version'],active=False,approved=False)
    adapter=install(engine_case,[draft(),verdict(False),draft(),verdict(True)],revoke)
    with pytest.raises(Error) as exc:generate(engine_case)
    assert exc.value.status==409 and len(adapter.calls)==at_call


@pytest.mark.parametrize('allowance',[1,2,3])
def test_each_extra_call_needs_its_own_budget(engine_case,allowance):
    s,p,pid,_,job,_,_=engine_case
    original=s.ai_reserve_usage;reservations=[]
    def reserve(*args,**kwargs):
        reservations.append(kwargs['call_index'])
        return original(*args,**kwargs) if kwargs['call_index']<allowance else False
    # Existing SQLite accounting remains active; simulate reaching allowance at
    # each boundary without changing the leased settings snapshot.
    s.ai_reserve_usage=reserve
    adapter=install(engine_case,[draft(),verdict(False),draft(),verdict(True)])
    assert generate(engine_case)['body']==CHECK_UNAVAILABLE
    assert len(adapter.calls)==allowance and reservations==list(range(allowance+1))
    assert s.ai_usage(pid,p)['reserved_requests']==allowance


def test_storage_bound_includes_only_three_extra_answer_calls(engine_case):
    s,p,pid,_,job,_,_=engine_case
    maximum=s.ai_settings(pid,p)['settings']['max_tool_rounds']+3
    assert s.ai_reserve_usage(job['id'],job['lease_token'],100,call_index=maximum)
    assert not s.ai_reserve_usage(job['id'],job['lease_token'],100,call_index=maximum+1)

from test_ai_chat_isolation import ai_chat, turn, invalidate, FixedEmbeddings, FixtureVault
from feedback.ai_generation import AIGeneration
from feedback.ai_retrieval import RetrievalService


def test_check_timeout_commits_clear_failure_and_next_turn_waits_for_recovery(ai_chat):
    from uuid import uuid4
    from feedback.ai_chat import AIChatWorker
    from test_ai_chat_isolation import conversation
    env = ai_chat
    store = env['store']
    customer = conversation(env)
    cid, principal = customer['conversation']['id'], customer['principal']
    store.chat_send(cid, principal, str(uuid4()), 'How long do returns take?')
    adapter = Scripted([draft(), ProviderError('timeout', retryable=True)])
    engine = AIGeneration(store, FixtureVault(), RetrievalService(store, FixedEmbeddings()),
                          provider_factory=lambda _: adapter)
    worker = AIChatWorker(store, engine)
    assert worker.run_once()
    messages = store.chat_messages(cid, principal, 0, 100)
    assert messages[-1]['body'] == CHECK_UNAVAILABLE
    assert not any(m['body'] == 'Use the backup operation.' for m in messages)
    assert store.chat_conversation(cid, principal)['ai_status'] == 'succeeded'
    store.chat_send(cid, principal, str(uuid4()), 'Try again')
    assert not worker.run_once()
    assert store.chat_conversation(cid, principal)['ai_status'] == 'queued'
    assert len(adapter.calls) == 2  # Cooldown prevents immediate redispatch.


@pytest.mark.parametrize('reason',['off_on','mode_cycle','admin_reply','session_expire','origin_removed','source_cycle'])
@pytest.mark.parametrize('at_call',[2,3,4])
def test_chat_authority_changes_during_review_cannot_send_or_continue(ai_chat,reason,at_call):
    env=ai_chat
    customer,_,job,material=turn(env)
    adapter=Scripted([draft(),verdict(False),draft(),verdict(True)],
                     lambda i: invalidate(env,customer,reason) if i==at_call else None)
    engine=AIGeneration(env['store'],FixtureVault(),RetrievalService(env['store'],FixedEmbeddings()),provider_factory=lambda _:adapter)
    with pytest.raises(Error) as exc:
        asyncio.run(engine.generate(job['id'],job['lease_token'],material['question']))
    assert exc.value.status==409 and len(adapter.calls)==at_call
    messages=env['store'].chat_messages(customer['conversation']['id'],env['admin'],0,100)
    assert not any(m['sender_role']=='ai' for m in messages)


def test_wrong_context_review_corrects_using_originals_and_preserved_history(engine_case):
    wrong='Use the individual entry export.'
    history=[{'sender_role':'customer','body':'How do I make a device backup?'}]
    bad_review=result({'assessment':'Draft changed the requested operation.', 'supported':False,
        'issues':[{'quote':wrong,'reason':'wrong_context'}]})
    adapter=install(engine_case,[draft(wrong),bad_review,draft(),verdict(True)])
    answer=generate(engine_case,history=history)
    assert answer['body']=='Use the backup operation.'
    assert adapter.calls[2][4]['issues'][0]['reason']=='wrong_context'
    assert all(c[4]['history']==[{'speaker':'customer','text':history[0]['body']}] for c in adapter.calls)
    assert adapter.calls[2][4]['passages'][0]['text']=='Original guide for backups'
