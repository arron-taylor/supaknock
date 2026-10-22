import json
import time
from copy import deepcopy
from uuid import uuid4
import pytest
from feedback.ai_mcp_protocol import definition
from feedback.errors import Error
from test_ai_chat_isolation import ai_chat,conversation,turn
from test_ai_tools import enable_tools
from test_ai_mcp_protocol import TOOL


def connect(env):
    enable_tools(env,[])
    s,p=env['store'],env['admin']
    row=s.mcp_save(p,None,0,label='Public episodes',url='https://podcasts.example/mcp',enabled=True,protected_token='encrypted-fixture',link_origins=['https://podcasts.example'])
    t=definition(TOOL)
    s.mcp_discovered(p,row['id'],row['version'],[t])
    binding={'connection_id':row['id'],'connection_version':row['version'],'tools':[{'name':t['name'],'digest':t['digest']}],'context_kind':None}
    s.mcp_bind(p,env['project'],0,'combined',[binding])
    return row,binding


def test_library_permissions_no_secret_readback_and_edit_fence(ai_chat):
    env=ai_chat;s=env['store'];row,b=connect(env)
    assert 'encrypted-fixture' not in json.dumps(s.mcp_connections(env['admin']))
    customer,msg,job,mat=turn(env)
    assert mat['mcp_tools']
    for principal in [{'kind':'customer'},{'kind':'admin','user_id':env['admin']['user_id'],'session_id':'expired'}]:
        with pytest.raises(Error):s.mcp_connections(principal)
    s.mcp_save(env['admin'],row['id'],1,label='Edited',url=row['url'],enabled=False,protected_token=None,link_origins=[])
    assert s.ai_job_material(job['id'],job['lease_token']) is None
    with pytest.raises(Error):s.mcp_bind(env['admin'],env['project'],1,'tools',[b])


def test_context_is_immutable_and_not_in_events(ai_chat):
    env=ai_chat;s=env['store'];customer=conversation(env);cid=customer['conversation']['id'];p=customer['principal'];mid=str(uuid4())
    context={'version':1,'resource':{'kind':'podcast_episode','id':'Exact Episode ID'},'position_seconds':23.4}
    first=s.chat_send(cid,p,mid,'Explain that',context=context)
    assert first['context']==context
    assert s.chat_send(cid,p,mid,'Explain that',context=context)['id']==first['id']
    with pytest.raises(Error):s.chat_send(cid,p,mid,'Explain that',context=context|{'position_seconds':24})
    with s.transaction() as c:
        events=c.execute('SELECT payload FROM request_events WHERE id=?',(first['id'],)).fetchone()[0]
        assert 'Exact Episode ID' not in events and '23.4' not in events
    job=s.ai_claim_job(time.time(),120,kinds=('turn',))
    assert s.ai_job_material(job['id'],job['lease_token'])['context']==context


def test_only_exact_reviewed_tools_and_versions_bind(ai_chat):
    env=ai_chat;s=env['store'];row,b=connect(env)
    bad=deepcopy(b);bad['tools'][0]['digest']='0'*64
    with pytest.raises(Error):s.mcp_bind(env['admin'],env['project'],1,'tools',[bad])
    with pytest.raises(Error):s.mcp_bind(env['admin'],env['project'],0,'tools',[b])
    assert s.mcp_project(env['admin'],env['project'])['version']==1


def test_receipt_evidence_scoped_to_attempt_and_connection(ai_chat):
    env=ai_chat;s=env['store'];row,b=connect(env)
    _,_,job,_=turn(env)
    snapshot=s.mcp_job_snapshot(job['id'],job['lease_token']);t=snapshot['tools'][0]
    rid=s.mcp_reserve(job['id'],job['lease_token'],t,'read-1')
    with pytest.raises(Error):s.mcp_reserve(job['id'],job['lease_token'],t,'read-1')
    evidence=s.mcp_finish(job['id'],job['lease_token'],rid,[{'title':'Episode','excerpt':'Original statement','url':None}],.1)
    checked=s.ai_job_validate_citations(job['id'],job['lease_token'],[evidence[0]['id']])
    assert checked['tool_sources']==evidence and checked['passages']==[]
    with pytest.raises(Error):s.ai_job_validate_citations(job['id'],'other-attempt',[evidence[0]['id']])
    reply=s.ai_commit_reply(job['id'],job['lease_token'],'Original statement',[evidence[0]['id']],checked['knowledge_version'])
    assert reply['tool_sources']==evidence and reply['citations']==[]


def test_tools_only_does_not_need_knowledge_or_embedding(ai_chat):
    env=ai_chat;s=env['store'];enable_tools(env,[]);row,b=connect(env)
    s.mcp_bind(env['admin'],env['project'],1,'tools',[b])
    with s.transaction(True) as c:
        c.execute("UPDATE ai_runtime SET state='not_downloaded',active_manifest=NULL")
        c.execute('UPDATE ai_sources SET approved=0')
    ready=s.ai_settings(env['project'],env['admin'])['readiness']
    assert ready['ready'] and not ready['knowledge_ready'] and ready['tools_ready']
    with s.transaction() as c:
        assert not s._ai_readiness(c,s._ai_config(c,env['project']),authoring=True)['ready']


def test_mcp_secrets_and_committed_sources_survive_supported_backup(ai_chat,tmp_path,database_url):
    if database_url:pytest.skip('SQLite bundle restore; PostgreSQL backup is verified separately')
    from cryptography.fernet import Fernet
    from feedback.security import Vault
    from feedback.config import Config
    from feedback.api import create_app
    from feedback.maintenance import backup
    env=ai_chat;s=env['store'];row,_=connect(env)
    _,_,job,_=turn(env)
    tool=s.mcp_job_snapshot(job['id'],job['lease_token'])['tools'][0]
    receipt=s.mcp_reserve(job['id'],job['lease_token'],tool,'persistent-source')
    sources=s.mcp_finish(job['id'],job['lease_token'],receipt,[{'title':'Episode','excerpt':'A retained public quote','url':None,'start_seconds':42,'end_seconds':49,'timing_kind':'aligned'}],.1)
    ids=[sources[0]['id']]
    checked=s.ai_job_validate_citations(job['id'],job['lease_token'],ids)
    reply=s.ai_commit_reply(job['id'],job['lease_token'],'A retained public quote',ids,checked['knowledge_version'])
    secret=Fernet.generate_key().decode();vault=Vault(secret)
    # Replace deliberately non-encrypted unit-fixture values for a real boot.
    with s.transaction(True) as c:
        c.execute('UPDATE ai_settings SET protected_key=?',(vault.protect('generation-fixture'),))
        c.execute('UPDATE ai_provider_keys SET protected_key=?',(vault.protect('generation-fixture'),))
        c.execute('UPDATE ai_mcp_connections SET protected_token=? WHERE id=?',(vault.protect('mcp-fixture-token'),row['id']))
    config=Config(s.path,secret,'s'*32,base_url=s.base_url,worker=False)
    create_app(config)
    bundle=tmp_path/'mcp-backup';backup(config,bundle)
    restored_config=Config(bundle/'feedback.sqlite',(bundle/'application.key').read_text().strip(),'s'*32,base_url=s.base_url,worker=False)
    restored=create_app(restored_config).state.storage
    assert 'mcp-fixture-token' in [vault.reveal(v) for v in restored.signing_secrets()]
    assert restored.mcp_connections(env['admin'])==s.mcp_connections(env['admin'])
    assert not getattr(restored,'_mcp_memory',{})
    with restored.transaction() as c:
        assert json.loads(c.execute('SELECT tool_sources FROM chat_messages WHERE id=?',(reply['id'],)).fetchone()[0])==sources
    with restored.transaction(True) as c:
        c.execute('UPDATE ai_mcp_connections SET protected_token=?',(Vault(Fernet.generate_key().decode()).protect('wrong-key'),))
    with pytest.raises(RuntimeError,match='installation secrets'):create_app(restored_config)


def test_unused_evidence_is_not_durable_and_committed_reply_retries_survive_cleanup(ai_chat):
    env=ai_chat;s=env['store'];connect(env)
    _,_,job,_=turn(env);t=s.mcp_job_snapshot(job['id'],job['lease_token'])['tools'][0]
    rid=s.mcp_reserve(job['id'],job['lease_token'],t,'read')
    sources=s.mcp_finish(job['id'],job['lease_token'],rid,[{'title':'Episode','excerpt':'An exact public statement','url':None}],.1)
    ids=[sources[0]['id']];version=s.ai_job_validate_citations(job['id'],job['lease_token'],ids)['knowledge_version']
    reply=s.ai_commit_reply(job['id'],job['lease_token'],'An exact public statement',ids,version)
    s.mcp_clear_attempt(job['id'],job['lease_token'])
    assert not s._mcp_memory
    assert s.ai_commit_reply(job['id'],job['lease_token'],'An exact public statement',ids,version)['id']==reply['id']


def test_readiness_requires_tool_capability_for_remote_only(ai_chat):
    env=ai_chat;s=env['store'];_,b=connect(env)
    s.mcp_bind(env['admin'],env['project'],1,'tools',[b])
    cfg=s.ai_settings(env['project'],env['admin'])
    s.ai_store_provider_evidence(env['project'],env['admin'],cfg['version'],cfg['credential_version'],'fixture-model',catalogue=['fixture-model'],capabilities={'text_generation':True,'tool_calling':False,'streaming':False,'structured_output':False})
    assert 'answer_tools_unverified' in s.ai_settings(env['project'],env['admin'])['readiness']['issues']


def test_twenty_sources_are_total_across_local_and_remote_and_reject_duplicates(ai_chat):
    env=ai_chat;s=env['store'];connect(env)
    _,_,job,_=turn(env);t=s.mcp_job_snapshot(job['id'],job['lease_token'])['tools'][0]
    rid=s.mcp_reserve(job['id'],job['lease_token'],t,'read')
    sources=s.mcp_finish(job['id'],job['lease_token'],rid,
        [{'title':'Source '+str(n),'excerpt':'Exact quote '+str(n),'url':None} for n in range(20)],.1)
    ids=[r['id'] for r in sources]
    assert len(s.ai_job_validate_citations(job['id'],job['lease_token'],ids)['tool_sources'])==20
    for invalid in (ids+[env['chunk']], [ids[0],ids[0]], [1], 'not a list'):
        with pytest.raises(Error):s.ai_job_validate_citations(job['id'],job['lease_token'],invalid)
        with pytest.raises(Error):s.ai_commit_reply(job['id'],job['lease_token'],'A reply',invalid,1)
    mixed=s.ai_job_validate_citations(job['id'],job['lease_token'],ids[:19]+[env['chunk']])
    assert len(mixed['tool_sources'])==19 and len(mixed['passages'])==1


def test_source_deduplication_does_not_cross_connections(ai_chat):
    env=ai_chat;s=env['store'];row,binding=connect(env)
    other=s.mcp_save(env['admin'],None,0,label='Other',url='https://other.example/mcp',enabled=True,protected_token=None,link_origins=[])
    t=definition(TOOL);s.mcp_discovered(env['admin'],other['id'],1,[t])
    s.mcp_bind(env['admin'],env['project'],1,'combined',[binding,binding|{'connection_id':other['id']}])
    _,_,job,_=turn(env);tools=s.mcp_job_snapshot(job['id'],job['lease_token'])['tools']
    ids=[]
    for index,tool in enumerate(tools):
        rid=s.mcp_reserve(job['id'],job['lease_token'],tool,str(index))
        ids.append(s.mcp_finish(job['id'],job['lease_token'],rid,[{'title':'Same','excerpt':'Same public quote','url':None}],.1)[0]['id'])
    assert len(set(ids))==2
