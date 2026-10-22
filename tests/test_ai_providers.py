"""Provider protocol fixtures, never live keys or billable network calls."""
import json

import anyio
import httpx
import pytest

from feedback.ai_providers import (
    KODEKEY_BASE_URL, MAX_RESPONSE_BYTES, MAX_REQUEST_BYTES, KodeKeyProvider,
    ModelCapabilities, ToolSpec, ToolCall, Message, Usage, GenerationResult,
    ProviderError, provider, PROVIDERS, GeminiProvider, MistralProvider, GroqProvider,
)


KEY = 'fixture-private-key'
CAPABILITIES = ModelCapabilities(text_generation=True, tool_calling=True)
SEARCH = ToolSpec('search_knowledge', 'Search approved support passages.',
                  {'type': 'object', 'properties': {'query': {'type': 'string'}},
                   'required': ['query'], 'additionalProperties': False})


def run(awaitable):
    async def invoke():
        return await awaitable
    return anyio.run(invoke)


def response(payload, status=200, headers=None):
    raw = json.dumps(payload).encode()
    return httpx.Response(status, headers={'Content-Type': 'application/json', **(headers or {})},
                          stream=httpx.ByteStream(raw))


def completion(text='An answer.', calls=None, reason=None, usage=None):
    message = {'role': 'assistant', 'content': text}
    if calls is not None:
        message['tool_calls'] = calls
    return {'choices': [{'finish_reason': reason or ('tool_calls' if calls else 'stop'), 'message': message}],
            'usage': usage}


def call(name='search_knowledge', arguments='{"query":"billing"}', identity='call-1'):
    return {'id': identity, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}


def adapter(handler, **options):
    return KodeKeyProvider(transport=httpx.MockTransport(handler), **options)


def generate(client, **options):
    defaults = {'capabilities': CAPABILITIES}
    return run(client.generate(KEY, 'exact/model-ID', (Message('user', 'Where are billing docs?'),),
                               **(defaults | options)))


def test_catalogue_retains_exact_ids_without_claiming_access():
    seen = []
    def handler(request):
        seen.append(request)
        return response({'data': [{'id': 'Vendor/Model-A', 'capabilities': ['all'], 'allowed': True},
                                  {'id': 'vendor/model-a'}, {'id': 'Vendor/Model-A'}]})
    client = adapter(handler)
    assert run(client.list_models(KEY)) == ('Vendor/Model-A', 'vendor/model-a')
    assert str(seen[0].url) == KODEKEY_BASE_URL + '/models'
    assert seen[0].headers['authorization'] == 'Bearer ' + KEY
    assert not seen[0].url.query
    with pytest.raises(ProviderError, match='capability_unverified'):
        generate(client, capabilities=ModelCapabilities())
    assert len(seen) == 1


def test_no_tool_chat_omits_schemas_and_normalizes_usage_without_inventing_cost():
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return response(completion(usage={'prompt_tokens': 11, 'completion_tokens': 3,
                                           'total_tokens': 14, 'cost': 'untrusted-currency'}))
    result = generate(adapter(handler), capabilities=ModelCapabilities(text_generation=True))
    assert result.text == 'An answer.' and result.tool_calls == ()
    assert result.usage == Usage(11, 3, 14)
    assert seen == [{'model': 'exact/model-ID', 'messages': [{'role': 'user', 'content': 'Where are billing docs?'}],
                     'max_tokens': 1024, 'stream': False}]
    assert result.message() == Message('assistant', 'An answer.')
    assert KEY not in repr(result) and result.text not in repr(result)


@pytest.mark.parametrize('response_format', ['text', 'json_object'])
def test_kodekey_legacy_hint_does_not_enable_unverified_strict_output(response_format):
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return response(completion('{"answer":"Approved source"}'))
    client = adapter(handler)
    result = generate(client, response_format=response_format)
    assert client.native_json_output is False
    assert 'response_format' not in bodies[0]
    assert result.text == '{"answer":"Approved source"}'
    assert CAPABILITIES.structured_output is False


@pytest.mark.parametrize('provider_class,model', [
    (KodeKeyProvider, 'exact/model-ID'), (GeminiProvider, 'gemini-3.8-flash'),
    (MistralProvider, 'ministral-14b-2512'), (GroqProvider, 'openai/gpt-oss-120b')])
@pytest.mark.parametrize('response_format', [None, True, 1, {}, [], '', 'JSON', 'json_schema', 'json_object\n'])
def test_response_format_is_closed_and_rejected_before_dispatch(provider_class, model, response_format):
    client = provider_class(transport=httpx.MockTransport(lambda request: pytest.fail('Invalid format reached network')))
    with pytest.raises(ProviderError, match='invalid_request'):
        run(client.generate(KEY, model, (Message('user', 'Answer as JSON.'),),
            capabilities=CAPABILITIES, response_format=response_format))


def test_enabled_tool_translation_and_followup_does_not_execute_an_action():
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return response(completion(None, [call()]))
        return response(completion('See the billing page.'))
    client = adapter(handler)
    result = generate(client, tools=(SEARCH,))
    assert result.tool_calls == (ToolCall('call-1', SEARCH.name, {'query': 'billing'}),)
    assert len(bodies) == 1  # Returning a call never invokes an application tool or continues the loop.
    assert [tool['function']['name'] for tool in bodies[0]['tools']] == [SEARCH.name]
    assert bodies[0]['tool_choice'] == 'auto'
    second = run(client.generate(KEY, 'exact/model-ID', (
        Message('user', 'Where are billing docs?'), result.message(),
        Message('tool', 'Approved source excerpt', tool_call_id='call-1')),
        capabilities=CAPABILITIES, tools=(SEARCH,)))
    assert second.text == 'See the billing page.'
    assert bodies[1]['messages'][1]['tool_calls'][0] == call()
    assert bodies[1]['messages'][2] == {'role': 'tool', 'content': 'Approved source excerpt', 'tool_call_id': 'call-1'}


@pytest.mark.parametrize('tools,name', [((), 'search_knowledge'), ((SEARCH,), 'create_bug_report'), ((SEARCH,), ['not-a-name'])])
def test_forged_disabled_or_unknown_tools_are_rejected(tools, name):
    with pytest.raises(ProviderError, match='tool_not_enabled'):
        generate(adapter(lambda r: response(completion(None, [call(name=name)]))), tools=tools)


def test_tool_capability_must_be_verified_before_sending():
    def should_not_call(request):
        raise AssertionError('Unverified tools reached provider')
    with pytest.raises(ProviderError, match='tool_capability_unverified'):
        generate(adapter(should_not_call), capabilities=ModelCapabilities(text_generation=True), tools=(SEARCH,))


@pytest.mark.parametrize('payload,code', [
    ({'choices': []}, 'invalid_response'),
    ({'choices': [None]}, 'invalid_response'),
    (completion('partial', reason='length'), 'incomplete_response'),
    (completion(None, reason='content_filter'), 'refused'),
    (completion('untrusted', reason='unknown'), 'invalid_response'),
    (completion(None), 'invalid_response'),
    (completion({'text': 'unsupported shape'}), 'invalid_response'),
    (completion('\ud800'), 'invalid_response'),
    (completion(None, [call(arguments='{"query":"one","query":"two"}')]), 'invalid_response'),
    (completion(None, [call(arguments='{"query":NaN}')]), 'invalid_response'),
    (completion(None, [call(arguments='{"nested":{"amount":1e309}}')]), 'invalid_response'),
    (completion(None, [call(arguments='{"nested":["\\ud800"]}')]), 'invalid_response'),
    (completion(None, [call(arguments='["not an object"]')]), 'invalid_response'),
    (completion(None, [call(arguments='\ud800')]), 'invalid_response'),
    (completion(None, [call(), call()]), 'invalid_response'),
    (completion(None, [call()], reason='stop'), 'invalid_response'),
    (completion('answer', calls={}), 'invalid_response'),
    (completion(usage={'prompt_tokens': True}), 'invalid_response'),
    (completion(usage={'total_tokens': -1}), 'invalid_response'),
])
def test_untrusted_provider_shapes_fail_closed(payload, code):
    with pytest.raises(ProviderError, match=code):
        generate(adapter(lambda r: response(payload)), tools=(SEARCH,))


@pytest.mark.parametrize('data', [{}, {'data': [None]}, {'data': [{'id': ' contains-space'}]}, {'data': [{'id': 'x'}] * 1001}])
def test_invalid_catalogues_fail_closed(data):
    with pytest.raises(ProviderError, match='invalid_response'):
        run(adapter(lambda r: response(data)).list_models(KEY))


def test_missing_usage_is_unknown_not_zero():
    assert generate(adapter(lambda r: response(completion()))).usage == Usage()


class UnreadableBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise AssertionError('Error/redirect body must never be read')
        yield b''


@pytest.mark.parametrize('status,code,retryable', [(301, 'redirect_refused', False), (401, 'invalid_credentials', False),
                                                  (402, 'payment_required', False), (403, 'access_denied', False), (404, 'model_or_endpoint_unavailable', False),
                                                  (429, 'rate_limited', True), (503, 'provider_unavailable', True),
                                                  (400, 'request_rejected', False)])
def test_errors_are_safe_do_not_read_body_and_never_retry_or_follow(status, code, retryable, caplog):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(status, headers={'Location': 'https://untrusted.invalid/steal', 'Retry-After': '999999'},
                              stream=UnreadableBody())
    with pytest.raises(ProviderError) as error:
        generate(adapter(handler))
    assert error.value.code == code and error.value.retryable is retryable
    assert error.value.status == status
    assert error.value.retry_after == (86400 if retryable else None)
    assert len(seen) == 1
    assert KEY not in caplog.text + str(error.value) + repr(error.value)
    assert 'untrusted.invalid' not in caplog.text + str(error.value)


@pytest.mark.parametrize('exception,code', [(httpx.ReadTimeout('secret-body'), 'timeout'),
                                           (httpx.ConnectError('secret-body'), 'network_error')])
def test_network_errors_never_return_or_log_upstream_text(exception, code, caplog):
    def handler(request):
        raise exception
    with pytest.raises(ProviderError) as error:
        generate(adapter(handler))
    assert error.value.code == code and error.value.retryable
    assert 'secret-body' not in caplog.text + str(error.value)
    assert error.value.__suppress_context__


def test_total_deadline_cancels_a_slow_stream():
    closed = []
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await anyio.sleep(10)
            yield b'{}'
        async def aclose(self):
            closed.append(True)
    client = adapter(lambda r: httpx.Response(200, headers={'Content-Type': 'application/json'}, stream=SlowBody()),
                     total_timeout=0.02)
    with pytest.raises(ProviderError, match='timeout'):
        generate(client)
    assert closed == [True]


def test_caller_cancellation_is_not_converted_to_retryable_failure():
    async def scenario():
        async def handler(request):
            await anyio.sleep(10)
        client = adapter(handler)
        with anyio.move_on_after(0.01) as scope:
            await client.list_models(KEY)
            raise AssertionError('Cancellation was swallowed')
        assert scope.cancel_called
    anyio.run(scenario)


@pytest.mark.parametrize('headers,body,code', [
    ({'Content-Encoding': 'gzip'}, b'compressed', 'invalid_response'),
    ({'Content-Type': 'text/html'}, b'<h1>secret</h1>', 'invalid_response'),
    ({'Content-Length': str(MAX_RESPONSE_BYTES + 1)}, b'', 'response_too_large'),
    ({}, b'x' * (MAX_RESPONSE_BYTES + 1), 'response_too_large'),
    ({}, b'{"data":[],"data":[]}', 'invalid_response'),
    ({}, b'{"data":NaN}', 'invalid_response'),
    ({}, b'not-json secret-body', 'invalid_response'),
])
def test_bounded_raw_response_and_strict_json(headers, body, code):
    client = adapter(lambda r: httpx.Response(200, headers={'Content-Type': 'application/json', **headers},
                                              stream=httpx.ByteStream(body)))
    with pytest.raises(ProviderError, match=code):
        run(client.list_models(KEY))


@pytest.mark.parametrize('key', ['', 'key\r\nX-Injected: yes', ' a-key', 'x' * 4097])
def test_credentials_reject_header_injection_without_reflection(key):
    with pytest.raises(ProviderError) as error:
        run(adapter(lambda r: pytest.fail('Invalid key was sent')).list_models(key))
    assert str(error.value) == 'invalid_credentials'


def test_bounded_requests_reject_oversize_or_invalid_values_before_network():
    client = adapter(lambda r: pytest.fail('Invalid request was sent'))
    with pytest.raises(ProviderError, match='invalid_request'):
        run(client.generate(KEY, 'model', (Message('user', 'x' * MAX_REQUEST_BYTES),), capabilities=CAPABILITIES))
    for kwargs in ({'max_output_tokens': 8193}, {'max_output_tokens': True}, {'required_tool': []},
                   {'tools': (SEARCH, SEARCH)}):
        with pytest.raises(ProviderError, match='invalid_request'):
            generate(client, **kwargs)


def test_transport_disables_environment_proxy_redirects_and_uses_fixed_endpoint(monkeypatch):
    seen = []
    actual_client = httpx.AsyncClient
    def checked_client(**kwargs):
        seen.append(kwargs)
        assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        assert kwargs['timeout'].connect == 5 and kwargs['timeout'].read == 15
        return actual_client(**kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', checked_client)
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.invalid:8080')
    def handler(request):
        assert str(request.url) == KODEKEY_BASE_URL + '/chat/completions'
        assert request.headers['accept-encoding'] == 'identity'
        assert request.headers['authorization'] == 'Bearer ' + KEY
        return response(completion())
    generate(adapter(handler))
    assert len(seen) == 1
    with pytest.raises(TypeError):
        KodeKeyProvider(base_url='https://untrusted.invalid')


def test_admin_probe_separately_checks_text_tool_call_and_tool_result_roundtrip():
    seen = []
    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 2:
            assert body['tool_choice']['function']['name'] == 'verify_capability'
            return response(completion(None, [call('verify_capability', '{"value":"ok"}')]))
        return response(completion('OK'))
    result = run(adapter(handler).probe_model(KEY, 'my-model', tools=True))
    assert result == ModelCapabilities(text_generation=True, tool_calling=True)
    assert len(seen) == 4
    assert all('response_format' not in body for body in seen[:3])
    assert seen[3]['response_format']['json_schema']['strict'] is True
    assert 'tools' not in seen[0]
    assert seen[2]['messages'][-1] == {'role': 'tool', 'content': '{"value":"ok"}', 'tool_call_id': 'call-1'}
    assert [body['max_tokens'] for body in seen] == [32, 128, 32, 128]


def test_text_only_probe_works_without_tools_and_bad_probe_does_not_claim_support():
    calls = []
    def handler(request):
        calls.append(request)
        return response(completion('OK'))
    assert run(adapter(handler).probe_model(KEY, 'text-only')) == ModelCapabilities(text_generation=True)
    assert len(calls) == 2
    with pytest.raises(ProviderError, match='tool_capability_unverified'):
        run(adapter(handler).probe_model(KEY, 'text-only', tools=True))
    with pytest.raises(ProviderError, match='capability_unverified'):
        run(adapter(lambda r: response(completion('Something else.'))).probe_model(KEY, 'bad-probe'))


def test_reviewed_registry_and_protocol_allow_a_fake_without_executing_tools():
    assert tuple(PROVIDERS) == ('kodekey', 'gemini', 'mistral', 'groq', 'openai', 'anthropic', 'openrouter', 'ramp', 'custom')
    assert provider('kodekey').name == 'KodeKloud KodeKey'
    assert provider('gemini').name == 'Google Gemini'
    assert provider('mistral').name == 'Mistral AI'
    assert provider('groq').name == 'Groq'
    with pytest.raises(ProviderError, match='unsupported_provider'):
        provider('https://untrusted.invalid/plugin.py')
    with pytest.raises(TypeError):
        PROVIDERS['unreviewed'] = object
    class FakeProvider:
        id, name = 'fixture', 'Fixture only'
        async def generate(self, key, model, messages, **kwargs):
            assert kwargs['tools'] == (SEARCH,)
            return GenerationResult('', (ToolCall('fake-call', SEARCH.name, {'query': 'billing'}),), Usage(), 'tool_calls')
    # Application consumes normalized output independent of the wire format.
    assert generate(FakeProvider(), tools=(SEARCH,)).tool_calls[0].name == SEARCH.name
