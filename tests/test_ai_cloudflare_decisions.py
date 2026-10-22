"""Cloudflare protocol fixtures and real SQLite decision-role integration."""
import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from feedback.ai_decision_providers import CloudflareDecisionProvider, cloudflare_credentials
from feedback.ai_providers import ProviderError
from feedback.ai_topic_service import TopicSettingsService
from feedback.errors import Error
from feedback.security import Vault
from cryptography.fernet import Fernet
from feedback.ai_chat import AIChatWorker
from feedback.ai_generation import AIGeneration
from feedback.ai_retrieval import RetrievalService
from feedback.ai_topic_service import TopicGate
from test_ai_chat import Adapter, Embeddings
from test_ai_chat_isolation import ai_chat, conversation
from test_ai_settings_api import ai

ACCOUNT = 'a' * 32
TOKEN = 'cloudflare-synthetic-token'
KEY = cloudflare_credentials(ACCOUNT, TOKEN)
MODEL = '@cf/cloudflare/clef-flash'
POLICY = {'scope': 'Acorn support', 'allowed_examples': ['Export notes'], 'disallowed_examples': ['Recipes']}


def run(value):
    return asyncio.run(value)


def envelope(result):
    return {'success': True, 'errors': [], 'messages': [], 'result': result}


def response(value):
    return httpx.Response(200, headers={'Content-Type': 'application/json'},
                          stream=httpx.ByteStream(json.dumps(value).encode()))


def decision(probability=.99, model='clef-flash'):
    return {'model': model, 'answers': {'in_scope': {'type': 'noul', 'noul': probability}},
            'usage': {'input_tokens': 21, 'output_tokens': 0}}


def adapter(handler):
    return CloudflareDecisionProvider(transport=httpx.MockTransport(handler))


def classify(client, model=MODEL, key=KEY):
    return run(client.classify(key, model, policy=POLICY, question='How do I export?',
                              context=[{'speaker': 'assistant', 'text': 'Use Acorn settings.'}]))


@pytest.mark.parametrize('model', [MODEL, '@cf/cloudflare/clef'])
def test_typed_wire_exact_model_and_minimal_state(model):
    requests = []
    def handle(request):
        requests.append(request)
        return response(envelope(decision(model=model.rsplit('/', 1)[1])))
    result = classify(adapter(handle), model)
    assert result.model == model and result.probability == .99 and result.usage.total_tokens == 21
    request = requests[0]
    assert request.method == 'POST'
    assert str(request.url) == f'https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/ai/run/{model}'
    assert request.headers['authorization'] == 'Bearer ' + TOKEN
    body = json.loads(request.content)
    assert set(body) == {'model', 'state', 'questions'}
    assert body['model'] == model.rsplit('/', 1)[1]
    assert body['state']['current_request'] == 'How do I export?'
    assert body['state']['preceding_messages'][0]['text'] == 'Use Acorn settings.'
    question = body['questions']['in_scope']
    assert question['type'] == 'noul' and question['instructions']['scope'] == POLICY['scope']
    assert 'entire current' in question['instructions']['question']
    assert 'unrelated extra request' in question['instructions']['rules']
    assert ACCOUNT not in request.content.decode() and TOKEN not in request.content.decode()


def test_authenticated_catalogue_exact_reviewed_models_only():
    requests = []
    def handle(request):
        requests.append(request)
        names = [MODEL, '@cf/cloudflare/clef', '@cf/cloudflare/clef-future', '@cf/unreviewed/model', MODEL]
        return response(envelope([{'name': name} for name in names]))
    assert run(adapter(handle).list_models(KEY)) == (MODEL, '@cf/cloudflare/clef')
    request = requests[0]
    assert request.url.path == f'/client/v4/accounts/{ACCOUNT}/ai/models/search'
    assert dict(request.url.params) == {'search': '@cf/cloudflare/clef', 'per_page': '100', 'page': '1'}
    assert request.headers['authorization'] == 'Bearer ' + TOKEN


@pytest.mark.parametrize('rows', [{}, [None], [{}], [{'name': ''}], [{'name': MODEL}] * 100])
def test_malformed_or_truncated_catalogue_fails_closed(rows):
    with pytest.raises(ProviderError, match='invalid_response'):
        run(adapter(lambda _: response(envelope(rows))).list_models(KEY))


@pytest.mark.parametrize('probability', [None, True, '0.99', -1, 1.01, float('nan'), float('inf')])
def test_probability_must_be_typed_finite_and_bounded(probability):
    with pytest.raises(ProviderError, match='invalid_response'):
        classify(adapter(lambda _: response(envelope(decision(probability)))))


@pytest.mark.parametrize('change', ['wrong_model', 'missing_model', 'missing_probability', 'wrong_type',
    'extra_question', 'missing_usage', 'string_usage', 'bool_usage', 'negative_usage', 'overflow_usage', 'failed_envelope'])
def test_invalid_model_answer_usage_and_envelope(change):
    result = decision()
    if change == 'wrong_model': result['model'] = 'clef'
    elif change == 'missing_model': del result['model']
    elif change == 'missing_probability': del result['answers']['in_scope']['noul']
    elif change == 'wrong_type': result['answers']['in_scope']['type'] = 'choice'
    elif change == 'extra_question': result['answers']['other'] = result['answers']['in_scope']
    elif change == 'missing_usage': del result['usage']
    elif change == 'string_usage': result['usage']['input_tokens'] = '21'
    elif change == 'bool_usage': result['usage']['input_tokens'] = True
    elif change == 'negative_usage': result['usage']['input_tokens'] = -1
    elif change == 'overflow_usage': result['usage']['input_tokens'] = 2**63 - 1; result['usage']['output_tokens'] = 1
    value = envelope(result)
    if change == 'failed_envelope': value['success'] = False; value['errors'] = [{'message': TOKEN}]
    with pytest.raises(ProviderError, match='invalid_response') as caught:
        classify(adapter(lambda _: response(value)))
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize('status,code', [(401, 'invalid_credentials'), (403, 'access_denied'),
    (429, 'rate_limited'), (302, 'redirect_refused'), (503, 'unavailable')])
def test_http_errors_are_safe_and_redirects_never_followed(status, code):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(status, headers={'retry-after': '60', 'location': 'https://example.invalid/'}, text=TOKEN)
    with pytest.raises(ProviderError, match=code) as caught:
        classify(adapter(handle))
    assert len(requests) == 1 and TOKEN not in str(caught.value)
    if status == 429: assert caught.value.retry_after == 60


@pytest.mark.parametrize('credential', [TOKEN, '{}', KEY.replace(ACCOUNT, '../evil'), KEY.replace(TOKEN, 'has space'), KEY.replace('cloudflare', 'kodekey')])
def test_credentials_fail_before_dispatch(credential):
    calls = []
    with pytest.raises(ProviderError, match='invalid_credentials'):
        classify(adapter(lambda r: calls.append(r)), key=credential)
    assert calls == []


def prepare(ai, handler):
    service = TopicSettingsService(ai['storage'], Vault(ai['cfg'].secret), provider_factory=lambda _: adapter(handler))
    project, principal = ai['p']['id'], ai['principal']
    topic = service.read(project, principal)
    topic = service.save(project, principal, topic['version'], topic['policy'] | {'provider': 'cloudflare',
        'scope': 'Acorn notes support', 'public_summary': 'Acorn support'})
    topic = service.add_key(project, principal, topic['version'], 'Clef account', 'Invented group',
                            key=TOKEN, account_id=ACCOUNT)
    return service, project, principal, topic


def test_real_sqlite_encryption_groups_duplicates_and_provider_isolation(ai):
    service, project, principal, topic = prepare(ai, lambda _: None)
    key = topic['keys'][0]
    snapshot = ai['storage'].ai_decision_snapshot(project, principal, topic['version'], key['id'], key['version'])
    protected = snapshot['key']['protected_key']
    assert TOKEN not in protected and ACCOUNT not in protected
    assert Vault(ai['cfg'].secret).reveal(protected) == KEY
    assert TOKEN not in json.dumps(topic) and ACCOUNT not in json.dumps(topic)
    topic = service.add_key(project, principal, topic['version'], 'Second token', 'Different group',
                            key=TOKEN+'-2', account_id=ACCOUNT)
    assert topic['keys'][0]['quota_group'] == topic['keys'][1]['quota_group']
    with pytest.raises(Error, match='already saved'):
        service.add_key(project, principal, topic['version'], 'Duplicate', 'Another group', key=TOKEN, account_id=ACCOUNT)
    with pytest.raises(Error):
        service.add_key(project, principal, topic['version'], 'Invalid', 'group', key=TOKEN, account_id=None)
    topic = service.save(project, principal, topic['version'], topic['policy'] | {'provider': 'kodekey'})
    assert topic['keys'] == [] and topic['policy']['model'] is None and not topic['policy']['enabled']
    with pytest.raises(Error):
        service.add_key(project, principal, topic['version'], 'Wrong provider', 'group', key=TOKEN, account_id=ACCOUNT)


def test_real_sqlite_catalogue_probe_then_explicit_activation(ai):
    calls = []
    def handle(request):
        calls.append(request)
        result = [{'name': MODEL}] if request.method == 'GET' else decision()
        return response(envelope(result))
    service, project, principal, topic = prepare(ai, handle)
    key = topic['keys'][0]
    topic = run(service.refresh(project, principal, topic['version'], key['id'], key['version']))
    topic = service.save(project, principal, topic['version'], topic['policy'] | {'model': MODEL})
    with pytest.raises(Error):
        service.save(project, principal, topic['version'], topic['policy'] | {'enabled': True})
    result = run(service.test(project, principal, topic['version'], key['id'], key['version'], '', probe=True))
    assert result['outcome'] == 'allow' and result['probability'] == .99
    topic = service.read(project, principal)
    assert topic['keys'][0]['verified'] and not topic['policy']['enabled']
    assert ai['storage'].ai_usage(project, principal)['reserved_requests'] == 1
    topic = service.save(project, principal, topic['version'], topic['policy'] | {'enabled': True})
    assert topic['policy']['enabled'] and len(calls) == 2
    topic = service.save(project, principal, topic['version'], topic['policy'] | {'model': '@cf/cloudflare/clef', 'enabled': False})
    assert not topic['keys'][0]['verified']


def test_native_admin_transport_validates_hides_and_requires_token_pair(ai):
    admin, path = ai['admin'], ai['url'] + '/topics'
    providers = admin.get('/api/v1/ai/decision-providers').json()
    assert next(p for p in providers if p['id']=='cloudflare') == {
        'id':'cloudflare', 'name':'Cloudflare / Clef', 'available':True, 'status':'wire_supported', 'requires_account_id':True}
    topic = admin.get(path).json()
    topic = admin.put(path, json={'version':topic['version'], 'policy':topic['policy'] | {'provider':'cloudflare'}}).json()
    body = {'version':topic['version'], 'label':'CF', 'key':TOKEN}
    assert admin.post(path+'/keys', json=body).status_code == 400
    assert admin.post(path+'/keys', json=body | {'account_id':'../account'}).status_code == 400
    result = admin.post(path+'/keys', json=body | {'account_id':ACCOUNT})
    assert result.status_code == 200 and TOKEN not in result.text and ACCOUNT not in result.text
    for client in (ai['anon'], ai['user'], ai['trusted'], ai['manager']):
        assert client.post(path+'/keys', json=body | {'account_id':ACCOUNT}).status_code in (401,403)


@pytest.mark.parametrize('probability,expected', [(.99,'allow'), (.01,'refuse'), (.5,'clarify'), ('0.99','unavailable')])
def test_real_chat_cloudflare_decision_precedes_retrieval_and_generation(ai_chat, probability, expected):
    class CombinedVault(Vault):
        def reveal(self, value):
            return 'fixture-key' if value == 'protected-fixture-key' else super().reveal(value)
    vault = CombinedVault(Fernet.generate_key().decode())
    calls, current = [], [.99]
    def handle(request):
        calls.append(request)
        return response(envelope([{'name': MODEL}] if request.method == 'GET' else decision(current[0])))
    client = adapter(handle)
    store, project, principal = ai_chat['store'], ai_chat['project'], ai_chat['admin']
    service = TopicSettingsService(store, vault, provider_factory=lambda _: client)
    topic = service.read(project, principal)
    topic = service.save(project, principal, topic['version'], topic['policy'] | {'provider':'cloudflare', 'scope':'Acorn notes support', 'public_summary':'Acorn'})
    topic = service.add_key(project, principal, topic['version'], 'Cloudflare', 'ignored', key=TOKEN, account_id=ACCOUNT)
    key = topic['keys'][0]
    topic = run(service.refresh(project, principal, topic['version'], key['id'], key['version']))
    topic = service.save(project, principal, topic['version'], topic['policy'] | {'model':MODEL})
    assert run(service.test(project, principal, topic['version'], key['id'], key['version'], '', probe=True))['outcome']=='allow'
    service.save(project, principal, topic['version'], topic['policy'] | {'enabled':True})
    current[0] = probability
    class Counted(Embeddings):
        calls = 0
        def embed_queries(self, texts):
            self.calls += 1
            return super().embed_queries(texts)
    embeddings, answers = Counted(), Adapter()
    generation = AIGeneration(store, vault, RetrievalService(store, embeddings), provider_factory=lambda _: answers,
        topic_gate=TopicGate(store, vault, provider_factory=lambda _: client))
    customer = conversation(ai_chat)
    store.chat_send(customer['conversation']['id'], customer['principal'], str(uuid4()), 'How do I export Acorn notes?')
    assert AIChatWorker(store, generation).run_once()
    assert embeddings.calls == (1 if expected=='allow' else 0)
    assert len(answers.calls) == (2 if expected=='allow' else 0)
    assert store.ai_topic_decisions(project, principal, 25, 0)['items'][0]['outcome']==expected
    assert len(calls)==3
