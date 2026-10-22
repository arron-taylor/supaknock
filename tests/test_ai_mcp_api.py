import json
import pytest
from test_ai_settings_api import ai
from test_ai_mcp_generation import PodcastClient

BASE='/api/v1/ai/tool-connections'

def test_admin_connection_lifecycle_and_read_test(ai):
    admin=ai['admin'];app=ai['app'];service=app.state.ai_mcp_admin
    service.client_factory=PodcastClient
    body={'version':0,'label':'Podcast tools','url':'https://podcasts.example/mcp','enabled':True,'token':'test-secret-only','link_origins':['https://podcasts.example']}
    response=admin.post(BASE,json=body)
    assert response.status_code==200,response.text
    saved=response.json();cid=saved['id']
    assert saved['has_token'] and 'test-secret-only' not in response.text
    response=admin.post(BASE+'/'+cid+'/discover',json={'version':1})
    assert response.status_code==200,response.text
    assert len(response.json()['catalogue'])==3
    response=admin.post(BASE+'/'+cid+'/test',json={'version':1,'name':'get_episode','arguments':{'episode_id':'alias'}})
    assert response.status_code==200,response.text
    assert response.json()['result']['episode_id']=='canonical'
    body.update(version=1,url='https://other.example/mcp');body.pop('token')
    assert admin.patch(BASE+'/'+cid,json=body).status_code==400
    body['token']=None
    assert admin.patch(BASE+'/'+cid,json=body).json()['has_token'] is False
    assert admin.request('DELETE',BASE+'/'+cid,json={'version':1}).status_code==409
    assert admin.request('DELETE',BASE+'/'+cid,json={'version':2}).status_code==200
    assert admin.get(BASE).json()['items']==[]


def test_api_endpoints_never_accept_project_or_participant_authority(ai):
    from fastapi.testclient import TestClient
    anon=TestClient(ai['app'])
    assert anon.get(BASE).status_code in (401,403)
    for name in ('user','trusted','manager','other'):
        assert ai[name].get(BASE).status_code==403


def test_context_schema_preserves_opaque_id_and_rejects_extra_fields():
    from feedback.chat_api import MessageContext
    from pydantic import ValidationError
    value=MessageContext.model_validate({'version':1,'resource':{'kind':'podcast_episode','id':' X '},'position_seconds':4})
    assert value.resource.id==' X '
    for key,value in [('user_id','bad'),('prompt','ignore rules')]:
        with pytest.raises(ValidationError):MessageContext.model_validate({'version':1,'resource':{'kind':'podcast_episode','id':'x'},key:value})
