import copy,json
from pathlib import Path
import pytest
from feedback.connected_apps_protocol import *

BASE={'protocol':{'major':1,'minor':0},'app_instance_id':'fixture-production','observed_at':'2026-10-05T12:00:00Z'}
USER={'id':'Customer A','display_name':'Avery','email':None,'joined_at':'2026-10-01T10:00:00Z'}
EXAMPLES={
'manifest':BASE|{'capabilities':{'summary':True,'users':True,'profiles':True,'activity':True,'user_activity':True},'user_filters':['query','joined_range'],'link_origins':['https://app.example.com'],'activity_history':{'available_from':None,'complete':True}},
'summary':BASE|{'interval':{'from':'2026-10-01T00:00:00Z','until':'2026-10-06T00:00:00Z'},'total_accounts':{'count':1},'new_accounts':{'count':1,'basis':'current_accounts','complete':True}},
'users':BASE|{'items':[USER],'scan_until':'2026-10-05T12:00:00Z','expires_at':'2026-10-05T12:30:00Z','next_cursor':None},
'profile':BASE|{'user':USER,'details':[{'key':'projects','label':'Projects','type':'number','value':3}],'url':'https://app.example.com/admin/users/a'},
'activity':BASE|{'items':[{'id':'event-1','occurred_at':'2026-10-05T10:00:00.000001Z','type':'user.created','title':'Account created','user_id':'Customer A'}],'snapshot':'snapshot-1','expires_at':'2026-10-05T12:30:00Z','next_cursor':None,'coverage':{'available_from':None,'complete':True}}
}
QUERIES={'manifest':{},'summary':EXAMPLES['summary']['interval'],'users':UserQuery().model_dump(),'profile':{'user_id':'Customer A'},'activity':ActivityQuery.model_validate({'from':'2026-10-01T00:00:00Z','until':'2026-10-06T00:00:00Z'}).model_dump(by_alias=True)}

@pytest.mark.parametrize('op',list(EXAMPLES))
def test_examples_and_schema_export(op):
    result=validated_response(op,EXAMPLES[op],QUERIES[op],BASE['app_instance_id'],['https://app.example.com'])
    assert result['app_instance_id']==BASE['app_instance_id']
    schema=json.loads(Path(f'docs/protocol/v1/{op}-response.json').read_text());schema.pop('$schema')
    assert schema==RESPONSES[op].model_json_schema(by_alias=True)


@pytest.mark.parametrize('op', list(RESPONSES))
def test_published_adapter_examples_match_runtime(op):
    """Validate the actual file implementers copy, including request defaults."""
    examples = json.loads(Path('docs/protocol/v1/examples.json').read_text())
    request_models = {
        'manifest': Input, 'summary': Range, 'users': UserQuery,
        'profile': Lookup, 'activity': ActivityQuery,
    }
    query = request_models[op].model_validate(examples['requests'][op]).model_dump(by_alias=True)
    manifest = examples['responses']['manifest']
    response = validated_response(
        op, examples['responses'][op], query,
        manifest['app_instance_id'], manifest['link_origins'],
    )
    assert response['app_instance_id'] == manifest['app_instance_id']


@pytest.mark.parametrize('op',list(EXAMPLES))
def test_identity_cannot_change(op):
    with pytest.raises(ValueError):validated_response(op,EXAMPLES[op],QUERIES[op],'another-app')

@pytest.mark.parametrize('update',[{'count':-1},{'count':True},{'count':1.5},{'count':None},{'count':9007199254740992}])
def test_counts_fail_closed(update):
    with pytest.raises(ValueError):Metric.model_validate(update)

def test_activity_precision_ids_and_bounds():
    page=copy.deepcopy(EXAMPLES['activity']);a=page['items'][0];b=a|{'id':'e\u0301','occurred_at':'2026-10-05T10:00:00.000002Z'}
    page['items']=[b,a];validated_response('activity',page,QUERIES['activity'])
    page['items']=[a,b]
    with pytest.raises(ValueError):validated_response('activity',page,QUERIES['activity'])
    page['items']=[a,a]
    with pytest.raises(ValueError):validated_response('activity',page,QUERIES['activity'])

def test_invalid_link_and_mismatched_user():
    page=copy.deepcopy(EXAMPLES['profile']);page['url']='https://evil.example/a'
    with pytest.raises(ValueError):validated_response('profile',page,QUERIES['profile'],origins=['https://app.example.com'])
    with pytest.raises(ValueError):validated_response('profile',EXAMPLES['profile'],{'user_id':'customer a'})

def test_user_ids_not_normalized():
    assert User.model_validate(USER|{'id':' e\u0301 '}).id==' e\u0301 '
    assert User.model_validate(USER|{'id':'é'}).id != User.model_validate(USER|{'id':'e\u0301'}).id

def test_bad_time_and_filter():
    with pytest.raises(ValueError):User.model_validate(USER|{'joined_at':'2026-99-05T10:00:00Z'})
    with pytest.raises(ValueError):UserQuery.model_validate({'status':'active'})
    with pytest.raises(ValueError):Capabilities(user_activity=True,users=True)

@pytest.mark.parametrize('major',[True,1.0,'1',2])
def test_protocol_major_is_exact_integer(major):
    with pytest.raises(ValueError):Protocol.model_validate({'major':major})

@pytest.mark.parametrize('phone',[None,'+13125550142','+1 (312) 555-0142 ext. 9','0'*64])
@pytest.mark.parametrize('op',['users','profile'])
def test_optional_phone_preserves_display_format(op,phone):
    page=copy.deepcopy(EXAMPLES[op])
    user=page['items'][0] if op=='users' else page['user']
    user['phone_number']=phone
    result=validated_response(op,page,QUERIES[op],BASE['app_instance_id'],['https://app.example.com'])
    assert (result['items'][0] if op=='users' else result['user'])['phone_number']==phone

def test_legacy_user_phone_is_unknown():
    assert User.model_validate(USER).phone_number is None

@pytest.mark.parametrize('phone',[13125550142,True,{},['+13125550142'],'0'*65])
def test_phone_is_bounded_text_not_a_number(phone):
    with pytest.raises(ValueError):User.model_validate(USER|{'phone_number':phone})
