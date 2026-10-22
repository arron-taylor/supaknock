"""Progress is fixed metadata owned by the current, authorized leased turn."""
import time
from uuid import uuid4
import pytest
from feedback.errors import Error
from test_ai_chat_isolation import ai_chat, turn, conversation, configure, reply


def test_progress_stages_and_reply_clear_without_draft_text(ai_chat):
    env=ai_chat;s=env['store'];customer,_,job,mat=turn(env)
    cid=customer['conversation']['id'];p=customer['principal']
    for stage in ('thinking','reading','writing','checking','refining'):
        assert s.ai_job_progress(job['id'],job['lease_token'],stage)
        view=s.chat_conversation(cid,p)
        assert view['ai_progress']==stage and view['ai_trigger_sequence']==1
        assert s.chat_messages(cid,p,0,100)[-1]['sender']=='visitor'
    with pytest.raises(Error):s.ai_job_progress(job['id'],job['lease_token'],'private draft text')
    reply(env,job,mat)
    assert s.chat_conversation(cid,p)['ai_progress'] is None
    assert not s.ai_job_progress(job['id'],job['lease_token'],'reading')


@pytest.mark.parametrize('change',['human','new_message','settings','revoke','expired','retry','failure'])
def test_progress_cannot_survive_authority_changes(ai_chat,change):
    env=ai_chat;s=env['store'];customer,_,job,_=turn(env)
    cid=customer['conversation']['id'];p=customer['principal']
    assert s.ai_job_progress(job['id'],job['lease_token'],'checking')
    if change=='human':s.chat_support_mode(cid,p,'waiting_human',s.chat_conversation(cid,p)['mode_version'])
    elif change=='new_message':s.chat_send(cid,p,str(uuid4()),'A new question')
    elif change=='settings':configure(env,enabled=False)
    elif change=='revoke':s.chat_revoke(cid,p)
    elif change in ('expired','retry'):
        with s.transaction(True) as c:c.execute('UPDATE ai_jobs SET lease_until=? WHERE id=?',(time.time()-1,job['id']))
        if change=='retry':assert s.ai_claim_job(time.time(),120,kinds=('turn',))['lease_token']!=job['lease_token']
    elif change=='failure':s.ai_finish_job(job['id'],job['lease_token'],'failed','invalid_response')
    assert not s.ai_job_progress(job['id'],job['lease_token'],'writing')
    if change=='revoke':
        with pytest.raises(Error):s.chat_conversation(cid,p)
    else:assert s.chat_conversation(cid,p)['ai_progress'] is None


def test_progress_uses_same_conversation_ownership(ai_chat):
    env=ai_chat;s=env['store'];customer,_,job,_=turn(env);other=conversation(env)
    s.ai_job_progress(job['id'],job['lease_token'],'reading')
    with pytest.raises(Error):s.chat_conversation(customer['conversation']['id'],other['principal'])
    assert s.chat_conversation(other['conversation']['id'],other['principal'])['ai_progress'] is None
