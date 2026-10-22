"""Synthetic wire contracts; no live keys, customer data or billable calls."""
import json
import httpx
import pytest
from feedback.ai_providers import provider, ProviderError, Message, ModelCapabilities
from feedback.ai_endpoints import custom_credentials, endpoint_url
from test_ai_providers import run, response, completion, call, KEY, SEARCH, CAPABILITIES

NEW = ('openai', 'anthropic', 'openrouter', 'ramp', 'custom')


def secret(name):
    return custom_credentials(KEY, 'https://llm.example/v1', ['model']) if name == 'custom' else KEY


def answer(name, *, tools=False, text='OK'):
    if name in ('openai', 'ramp'):
        output = [{'type': 'function_call', 'call_id': 'call-1', 'name': SEARCH.name, 'arguments': '{"query":"billing"}'}] if tools else [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}]
        return {'status': 'completed', 'output': output, 'usage': {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}}
    if name == 'anthropic':
        content = [{'type': 'tool_use', 'id': 'call-1', 'name': SEARCH.name, 'input': {'query': 'billing'}}] if tools else [{'type': 'text', 'text': text}]
        return {'role': 'assistant', 'content': content, 'stop_reason': 'tool_use' if tools else 'end_turn', 'usage': {'input_tokens': 4, 'output_tokens': 2}}
    return completion(text=None if tools else text, calls=[call()] if tools else None)


@pytest.mark.parametrize('name', NEW)
def test_text_and_tool_roundtrip_on_each_protocol(name):
    seen = []
    def handler(request):
        seen.append(request)
        return response(answer(name, tools=len(seen) == 1))
    adapter = provider(name, transport=httpx.MockTransport(handler))
    result = run(adapter.generate(secret(name), 'model', (Message('system', 'Safe support'), Message('user', 'Help')), capabilities=CAPABILITIES, tools=(SEARCH,)))
    assert result.tool_calls[0].arguments == {'query': 'billing'}
    final = run(adapter.generate(secret(name), 'model', (Message('system', 'Safe support'), Message('user', 'Help'), result.message(), Message('tool', '{"answer":"billing"}', tool_call_id='call-1')), capabilities=CAPABILITIES, tools=(SEARCH,)))
    assert final.text == 'OK' and not final.tool_calls
    body = json.loads(seen[-1].content)
    if name in ('openai', 'ramp'):
        assert seen[0].url.path == '/v1/responses' and body['store'] is False
        assert body['input'][-1]['call_id'] == 'call-1'
    elif name == 'anthropic':
        assert seen[0].url.path == '/v1/messages'
        assert seen[0].headers['x-api-key'] == KEY and seen[0].headers['anthropic-version'] == '2023-06-01'
        assert body['system'] == 'Safe support'
        assert body['messages'][-1]['content'][0]['tool_use_id'] == 'call-1'
    else:
        assert seen[0].url.path.endswith('/chat/completions')
        assert body['messages'][-1]['tool_call_id'] == 'call-1'
    assert KEY not in repr(result)


@pytest.mark.parametrize('name', NEW)
@pytest.mark.parametrize('status,code', [(401,'invalid_credentials'),(402,'payment_required'),(429,'rate_limited'),(302,'redirect_refused'),(503,'provider_unavailable')])
def test_upstream_failures_remain_safe_and_do_not_retry(name, status, code):
    seen = []
    def handler(request):
        seen.append(request)
        return response({'secret': KEY}, status=status, headers={'retry-after': '30'})
    with pytest.raises(ProviderError) as caught:
        run(provider(name, transport=httpx.MockTransport(handler)).generate(secret(name), 'model', (Message('user','Hi'),), capabilities=CAPABILITIES))
    assert caught.value.code == code and KEY not in str(caught.value) and len(seen) == 1


@pytest.mark.parametrize('name', ('openai', 'ramp', 'anthropic'))
def test_native_continuation_cannot_cross_credentials_or_models(name):
    seen = []
    def handler(request):
        seen.append(request)
        return response(answer(name, tools=True))
    adapter = provider(name, transport=httpx.MockTransport(handler))
    result = run(adapter.generate(KEY, 'model', (Message('user', 'Help'),), capabilities=CAPABILITIES, tools=(SEARCH,)))
    messages = (Message('user', 'Help'), result.message(), Message('tool', 'ok', tool_call_id='call-1'))
    for key, model in [('other-secret', 'model'), (KEY, 'other-model')]:
        with pytest.raises(ProviderError, match='invalid_request'):
            run(adapter.generate(key, model, messages, capabilities=CAPABILITIES, tools=(SEARCH,)))
    assert len(seen) == 1


@pytest.mark.parametrize('name', NEW)
def test_unverified_and_unadvertised_tools_never_run(name):
    adapter = provider(name, transport=httpx.MockTransport(lambda r: response(answer(name, tools=True))))
    with pytest.raises(ProviderError):
        run(adapter.generate(secret(name), 'model', (Message('user','Help'),), capabilities=ModelCapabilities()))
    with pytest.raises(ProviderError):
        run(adapter.generate(secret(name), 'model', (Message('user','Help'),), capabilities=CAPABILITIES))


@pytest.mark.parametrize('url', ['http://example.com/v1','https://user:secret@example.com/v1','https://example.com/v1?key=secret','https://example.com/#x','https://127.0.0.1/v1','https://[::1]/v1','https://169.254.169.254/v1','https://100.100.100.200/v1','https://localhost/v1','https://example.com/a/../v1','https://example.com/%2f/'])
def test_custom_endpoint_validation(url):
    with pytest.raises(ProviderError): endpoint_url(url)


def test_private_endpoints_require_installation_opt_in_and_metadata_stays_blocked():
    assert endpoint_url('http://localhost:11434/v1', allow_internal=True) == 'http://localhost:11434/v1'
    for url in ['http://169.254.169.254/v1','http://100.100.100.200/v1','http://[fe80::1]/v1']:
        with pytest.raises(ProviderError): endpoint_url(url, allow_internal=True)


def test_dns_is_pinned_and_all_answers_are_checked(monkeypatch):
    import feedback.ai_endpoints as endpoints
    import feedback.webhooks as networking
    monkeypatch.setattr(networking.socket, 'getaddrinfo', lambda *a, **kw: [(2,1,6,'',('93.184.216.34',443)),(2,1,6,'',('127.0.0.1',443))])
    with pytest.raises(ProviderError, match='blocked_endpoint'):
        run(endpoints.endpoint_request(None, 5, 'POST', 'https://llm.example/v1', '/embeddings', KEY, {}))
    monkeypatch.setattr(networking.socket, 'getaddrinfo', lambda *a, **kw: [(2,1,6,'',('93.184.216.34',443))])
    async def capture(transport, timeout, method, url, headers, body, **kw):
        assert url.host == '93.184.216.34' and headers['Host'] == 'llm.example'
        assert kw['extensions']['sni_hostname'] == b'llm.example'
        return {'ok': True}
    monkeypatch.setattr(endpoints, '_request_json', capture)
    assert run(endpoints.endpoint_request(None,5,'POST','https://llm.example/v1','/embeddings',KEY,{})) == {'ok': True}


def test_ramp_catalogue_filters_unsupported_surfaces():
    p=provider('ramp',transport=httpx.MockTransport(lambda req:response({'data':[
        {'id':'text','router':{'surfaces':['responses','messages']}},
        {'id':'messages-only','router':{'surfaces':['messages']}}]})))
    assert run(p.list_models('synthetic-key')) == ('text',)


def test_custom_credential_envelope_is_bounded_before_storage():
    from feedback.ai_endpoints import custom_credentials
    with pytest.raises(ProviderError):
        custom_credentials('key','https://llm.example/v1',['m'+str(i)+'x'*200 for i in range(100)])
