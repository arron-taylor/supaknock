"""Groq protocol fixtures: no real credentials, traffic or model execution."""
from dataclasses import replace
import json

import anyio
import httpx
import pytest

from feedback.ai_providers import (
    GROQ_BASE_URL, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, GroqProvider,
    KodeKeyProvider, GeminiProvider, MistralProvider, ModelCapabilities, Message,
    ToolCall, ToolSpec, Usage, ProviderError, _retry_after,
)

KEY = 'fixture-groq-key-private'
MODEL = 'openai/gpt-oss-120b'
CAPS = ModelCapabilities(text_generation=True, tool_calling=True)
TOOL = ToolSpec('search_knowledge', 'Search approved support passages.',
    {'type': 'object', 'properties': {'query': {'type': 'string'}},
     'required': ['query'], 'additionalProperties': False})


def run(awaitable):
    async def invoke():
        return await awaitable
    return anyio.run(invoke)


def response(value, status=200, headers=None):
    return httpx.Response(status, headers={'Content-Type': 'application/json', **(headers or {})},
                          stream=httpx.ByteStream(json.dumps(value).encode()))


def completion(content='An approved answer.', calls=None, reason=None, usage=None):
    return {'model': MODEL, 'choices': [{'index': 0, 'finish_reason': reason or ('tool_calls' if calls else 'stop'),
        'message': {'role': 'assistant', 'content': content, 'tool_calls': calls}}], 'usage': usage}


def call(identity='call_123', name=TOOL.name, arguments='{"query":"billing"}'):
    return {'id': identity, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}


def adapter(handler, **options):
    return GroqProvider(transport=httpx.MockTransport(handler), **options)


def generate(client, messages=None, model=MODEL, **options):
    return run(client.generate(KEY, model, messages or (Message('user', 'Where is billing help?'),),
                               **({'capabilities': CAPS} | options)))


def test_catalogue_intersects_exact_reviewed_active_chat_models():
    requests = []
    allowed = [MODEL, 'openai/gpt-oss-20b', 'llama-3.1-8b-instant',
               'llama-3.3-70b-versatile', 'qwen/qwen3.8-27b']
    excluded = ['groq/compound', 'groq/compound-mini', 'openai/gpt-oss-safeguard-20b',
        'meta-llama/llama-prompt-guard-2-22m', 'whisper-large-v3',
        'canopylabs/orpheus-v1-english', 'minimaxai/minimax-m2.7',
        'unreviewed/chat-model', 'OpenAI/gpt-oss-120b', 'openai/gpt-oss-120b/suffix']
    def handler(request):
        requests.append(request)
        rows = [{'id': identity, 'active': True, 'capabilities': {'chat': True},
                 'aliases': ['invented']} for identity in allowed + excluded + [MODEL]]
        return response({'data': rows})
    client = adapter(handler)
    assert run(client.list_models(KEY)) == tuple(sorted(allowed))
    request = requests[0]
    assert request.method == 'GET' and str(request.url) == GROQ_BASE_URL + '/models'
    assert request.headers['authorization'] == 'Bearer ' + KEY
    assert not request.url.query and 'x-goog-api-key' not in request.headers
    with pytest.raises(ProviderError, match='capability_unverified'):
        generate(client, capabilities=ModelCapabilities())
    assert len(requests) == 1
    assert run(adapter(lambda r: response({'data': [{'id': MODEL, 'active': False}]})).list_models(KEY)) == ()
    # The endpoint itself lists active models; older responses may omit active.
    assert run(adapter(lambda r: response({'data': [{'id': MODEL}]})).list_models(KEY)) == (MODEL,)


@pytest.mark.parametrize('value', [{}, [], {'data': [None]}, {'data': [{}]},
    {'data': [{'id': 'contains space'}]}, {'data': [{'id': MODEL, 'active': 1}]},
    {'data': [{'id': MODEL, 'active': None}]}, {'data': [{'id': MODEL}] * 1001}])
def test_malformed_catalogues_fail_closed(value):
    with pytest.raises(ProviderError, match='invalid_response'):
        run(adapter(lambda r: response(value)).list_models(KEY))


@pytest.mark.parametrize('model,reasoning', [(MODEL, {'include_reasoning': False}),
    ('openai/gpt-oss-20b', {'include_reasoning': False}),
    ('qwen/qwen3.8-27b', {'reasoning_format': 'hidden'}),
    ('llama-3.3-70b-versatile', {}), ('llama-3.1-8b-instant', {})])
def test_exact_model_wire_uses_documented_reasoning_mode_and_completion_budget(model, reasoning):
    requests = []
    def handler(request):
        requests.append(request)
        value = completion(usage={'prompt_tokens': 12, 'completion_tokens': 9, 'total_tokens': 21,
                                  'completion_tokens_details': {'reasoning_tokens': 5}})
        value['model'] = model
        value['choices'][0]['message']['reasoning'] = 'Private reasoning is never an answer.'
        return response(value)
    result = generate(adapter(handler), model=model)
    assert result.text == 'An approved answer.' and result.usage == Usage(12, 9, 21)
    assert result.provider_state is None and result.message() == Message('assistant', result.text)
    assert str(requests[0].url) == GROQ_BASE_URL + '/chat/completions'
    assert json.loads(requests[0].content) == {'model': model,
        'messages': [{'role': 'user', 'content': 'Where is billing help?'}],
        'max_completion_tokens': 1024, 'stream': False, **reasoning}
    assert KEY not in repr(result) + str(requests[0].url)
    assert 'Private reasoning' not in repr(result) + repr(result.message())


@pytest.mark.parametrize('response_format', ['text', 'json_object'])
def test_narrow_function_roundtrip_supports_final_tools_off_and_preserves_ids(response_format):
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return response(completion(None, [call()]) if len(bodies) == 1 else completion('See the guide.'))
    client = adapter(handler)
    first = generate(client, tools=(TOOL,), required_tool=TOOL.name)
    assert first.tool_calls == (ToolCall('call_123', TOOL.name, {'query': 'billing'}),)
    assert bodies[0]['tools'] == [{'type': 'function', 'function': {
        'name': TOOL.name, 'description': TOOL.description, 'parameters': TOOL.parameters}}]
    assert bodies[0]['tool_choice'] == {'type': 'function', 'function': {'name': TOOL.name}}
    assert bodies[0]['parallel_tool_calls'] is False
    final = generate(client, (Message('user', 'Where is billing help?'), first.message(),
        Message('tool', '{"excerpt":"Approved guide"}', tool_call_id='call_123')), response_format=response_format)
    assert final.text == 'See the guide.'
    assert not {'tools', 'tool_choice', 'parallel_tool_calls'} & set(bodies[1])
    assert bodies[1].get('response_format') == ({'type': 'json_object'} if response_format == 'json_object' else None)
    assert bodies[1]['messages'][1]['tool_calls'] == [call()]
    assert bodies[1]['messages'][2] == {'role': 'tool', 'name': TOOL.name,
        'content': '{"excerpt":"Approved guide"}', 'tool_call_id': 'call_123'}


@pytest.mark.parametrize('response_format', ['text', 'json_object'])
@pytest.mark.parametrize('tools_enabled', [False, True])
def test_json_mode_is_explicit_and_never_combined_with_advertised_tools(response_format, tools_enabled):
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return response(completion(None, [call()]) if tools_enabled else completion('{"answer":"Guide"}'))
    client = adapter(handler)
    result = generate(client, tools=(TOOL,) if tools_enabled else (),
        required_tool=TOOL.name if tools_enabled else None, response_format=response_format)
    assert client.native_json_output is True
    assert CAPS.structured_output is False
    assert bodies[0].get('response_format') == ({'type': 'json_object'} if response_format == 'json_object' and not tools_enabled else None)
    assert bodies[0]['include_reasoning'] is False
    assert bool(result.tool_calls) is tools_enabled
    if tools_enabled:
        assert bodies[0]['tool_choice']['function']['name'] == TOOL.name
        assert bodies[0]['parallel_tool_calls'] is False
    else:
        assert result.text == '{"answer":"Guide"}'


@pytest.mark.parametrize('case', ['missing', 'orphan', 'duplicate', 'wrong_id', 'interleaved', 'reordered', 'repeat_id'])
def test_history_requires_exact_once_ordered_tool_results(case):
    calls = (ToolCall('call_123', TOOL.name, {}), ToolCall('call_456', TOOL.name, {}))
    one = Message('tool', '{}', tool_call_id='call_123')
    two = Message('tool', '{}', tool_call_id='call_456')
    prefix = [Message('user', 'Find guide.'), Message('assistant', tool_calls=calls)]
    if case == 'orphan':
        history = [Message('user', 'Find guide.'), one]
    elif case == 'repeat_id':
        history = prefix + [one, two, Message('assistant', tool_calls=calls), one, two]
    else:
        history = prefix + {'missing': [one], 'duplicate': [one, one],
            'wrong_id': [replace(one, tool_call_id='unknown')],
            'interleaved': [one, Message('user', 'Continue.')], 'reordered': [two, one]}[case]
    with pytest.raises(ProviderError, match='invalid_request'):
        generate(adapter(lambda r: pytest.fail('Bad history reached network')), history, tools=(TOOL,))


def test_current_tool_authority_is_independent_of_historical_tools():
    first = generate(adapter(lambda r: response(completion(None, [call()]))), tools=(TOOL,))
    history = (Message('user', 'Find guide.'), first.message(), Message('tool', '{}', tool_call_id='call_123'))
    with pytest.raises(ProviderError, match='tool_not_enabled'):
        generate(adapter(lambda r: response(completion(None, [call(identity='new')]))), history)
    with pytest.raises(ProviderError, match='invalid_response'):
        generate(adapter(lambda r: response(completion(None, [call()]))), history, tools=(TOOL,))


@pytest.mark.parametrize('wire', [call(identity=''), call(identity='with space'),
    call(arguments={'query': 'billing'}), call(arguments='{"x":1,"x":2}'),
    call(arguments='[]'), call(arguments='{"value":1e309}'), call(arguments='{"value":NaN}'),
    call(arguments='{"value":"\\ud800"}'), call(arguments='x' * 16385),
    dict(call(), type='browser_search'), dict(call(), extra='untrusted')])
def test_untrusted_function_shapes_fail_closed(wire):
    with pytest.raises(ProviderError, match='invalid_response'):
        generate(adapter(lambda r: response(completion(None, [wire]))), tools=(TOOL,))


@pytest.mark.parametrize('calls', [[call(), call()], [call(), call(identity='second')],
                                 [call(identity=str(i)) for i in range(9)]])
def test_duplicate_parallel_or_excess_calls_fail_closed(calls):
    with pytest.raises(ProviderError, match='invalid_response'):
        generate(adapter(lambda r: response(completion(None, calls))), tools=(TOOL,))


@pytest.mark.parametrize('patch', [{'executed_tools': [{'type': 'browser_search'}]},
    {'annotations': [{'type': 'document_citation'}]}, {'function_call': {'name': 'legacy'}},
    {'content': [{'type': 'text', 'text': 'chunk'}]}, {'reasoning': []},
    {'content': None, 'reasoning': 'Only thinking'}, {'role': 'user'}, {'unexpected': 'field'}])
def test_hosted_actions_nontext_and_thinking_only_are_not_answers(patch):
    value = completion()
    value['choices'][0]['message'].update(patch)
    with pytest.raises(ProviderError, match='invalid_response'):
        generate(adapter(lambda r: response(value)))


@pytest.mark.parametrize('patch', [{'mcp_list_tools': [{'tools': ['browser']}]},
    {'usage_breakdown': {'models': [{'model': 'other'}]}}, {'model': 'unrequested'}])
def test_compound_remote_tools_and_implicit_model_fallback_are_rejected(patch):
    with pytest.raises(ProviderError, match='invalid_response'):
        generate(adapter(lambda r: response(completion() | patch)))


@pytest.mark.parametrize('reason,code', [('length', 'incomplete_response'),
    ('content_filter', 'refused'), ('function_call', 'invalid_response'), ('error', 'invalid_response')])
def test_finish_reasons_are_strict(reason, code):
    with pytest.raises(ProviderError, match=code):
        generate(adapter(lambda r: response(completion(reason=reason))))


@pytest.mark.parametrize('usage', [{'prompt_tokens': True}, {'completion_tokens': -1},
    {'total_tokens': 2 ** 63}, {'prompt_tokens': 5, 'completion_tokens': 10, 'total_tokens': 14}])
def test_invalid_usage_fails_closed(usage):
    with pytest.raises(ProviderError, match='invalid_response'):
        generate(adapter(lambda r: response(completion(usage=usage))))


@pytest.mark.parametrize('case', ['foreign_state', 'system_late', 'assistant_last', 'unknown_model',
    'tools_unverified', 'duplicate_tools', 'unknown_required', 'oversize', 'invalid_key'])
def test_invalid_requests_never_dispatch(case):
    client = adapter(lambda r: pytest.fail('Invalid request reached network'))
    messages, options = (Message('user', 'Question'),), {}
    if case == 'foreign_state':
        messages = (Message('assistant', 'Text', provider_state=b'{}'), Message('user', 'Next'))
    elif case == 'system_late':
        messages = (Message('user', 'Question'), Message('system', 'Late'), Message('user', 'Next'))
    elif case == 'assistant_last':
        messages = (Message('assistant', 'Text'),)
    elif case == 'unknown_model':
        options['model'] = 'groq/compound'
    elif case == 'tools_unverified':
        options.update(capabilities=ModelCapabilities(text_generation=True), tools=(TOOL,))
    elif case == 'duplicate_tools':
        options['tools'] = (TOOL, TOOL)
    elif case == 'unknown_required':
        options.update(tools=(TOOL,), required_tool='invented')
    elif case == 'oversize':
        messages = (Message('user', 'x' * MAX_REQUEST_BYTES),)
    if case == 'invalid_key':
        with pytest.raises(ProviderError, match='invalid_credentials'):
            run(client.list_models('bad\nkey'))
    else:
        with pytest.raises(ProviderError, match='tool_capability_unverified' if case == 'tools_unverified' else 'invalid_request'):
            generate(client, messages, **options)


@pytest.mark.parametrize('tools', [False, True])
def test_explicit_capability_probe_is_one_or_three_calls_without_execution(tools):
    bodies = []
    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 2:
            return response(completion(None, [call(name='verify_capability', arguments='{"value":"ok"}')]))
        return response(completion('OK'))
    capabilities = run(adapter(handler).probe_model(KEY, MODEL, tools=tools))
    assert capabilities == ModelCapabilities(text_generation=True, tool_calling=tools)
    assert len(bodies) == (3 if tools else 1)
    assert all(body['max_completion_tokens'] == 1024 for body in bodies)
    assert all('response_format' not in body for body in bodies)
    if tools:
        assert bodies[1]['tool_choice']['function']['name'] == 'verify_capability'
        assert bodies[2]['messages'][1]['tool_calls'][0]['id'] == 'call_123'
        assert bodies[2]['messages'][2]['tool_call_id'] == 'call_123'


@pytest.mark.parametrize('tools,step', [(False, 1), (True, 2), (True, 3)])
def test_failed_capability_probe_stops_without_retries(tools, step):
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        if count == step:
            return response(completion('Wrong'))
        return response(completion(None, [call(name='verify_capability', arguments='{"value":"ok"}')]) if count == 2 else completion('OK'))
    with pytest.raises(ProviderError, match='capability_unverified'):
        run(adapter(handler).probe_model(KEY, MODEL, tools=tools))
    assert count == step


class UnreadError(httpx.AsyncByteStream):
    async def __aiter__(self):
        pytest.fail('This error body must remain unread')
        yield b''


@pytest.mark.parametrize('status,code,retryable', [(301, 'redirect_refused', False),
    (401, 'invalid_credentials', False), (402, 'payment_required', False),
    (403, 'access_denied', False), (404, 'model_or_endpoint_unavailable', False),
    (422, 'request_rejected', False), (429, 'rate_limited', True),
    (503, 'provider_unavailable', True)])
def test_non400_errors_remain_unread_safe_and_never_follow_or_retry(status, code, retryable):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={'Location': 'https://evil.invalid', 'Retry-After': '7200'}, stream=UnreadError())
    with pytest.raises(ProviderError) as error:
        generate(adapter(handler))
    assert len(requests) == 1 and str(error.value) == code
    assert error.value.retryable is retryable and error.value.status == status
    assert error.value.retry_after == (7200 if retryable else None)
    assert KEY not in repr(error.value) and 'evil.invalid' not in repr(error.value)


def test_groq_monthly_spend_limit_has_one_exact_bounded_classifier():
    body = {'error': {'code': 'blocked_api_access', 'message': 'secret private body ' + KEY}}
    with pytest.raises(ProviderError) as error:
        generate(adapter(lambda r: response(body, 400)))
    assert str(error.value) == 'quota_exhausted' and error.value.status == 400
    assert error.value.retryable is False and error.value.retry_after is None
    assert KEY not in repr(error.value) + repr(error.value.__dict__)


@pytest.mark.parametrize('body', [{'error': {'code': 'BLOCKED_API_ACCESS'}},
    {'error': {'message': 'blocked_api_access'}}, {'code': 'blocked_api_access'},
    {'error': {'code': ['blocked_api_access']}}, {'error': None}, [],
    {'error': {'code': 'blocked_api_access'}, 'padding': 'x' * 16384}])
def test_unknown_or_oversize_groq400_remains_generic(body):
    with pytest.raises(ProviderError, match='request_rejected'):
        generate(adapter(lambda r: response(body, 400)))


@pytest.mark.parametrize('raw', [b'not-json', b'{"error":{"code":"x","code":"blocked_api_access"}}'])
def test_malformed_groq400_remains_generic(raw):
    with pytest.raises(ProviderError, match='request_rejected'):
        generate(adapter(lambda r: httpx.Response(400, headers={'Content-Type': 'application/json'}, stream=httpx.ByteStream(raw))))


@pytest.mark.parametrize('headers', [{'Content-Encoding': 'gzip'}, {'Content-Type': 'text/plain'},
    {'Content-Length': '16385'}, {'Content-Length': '-1'}, {'Content-Length': '1' * 13}])
def test_unsupported_error_encoding_or_declared_size_is_not_read(headers):
    with pytest.raises(ProviderError, match='request_rejected'):
        generate(adapter(lambda r: httpx.Response(400,
            headers={'Content-Type': 'application/json', **headers}, stream=UnreadError())))


def test_undeclared_oversize_error_read_stops_before_unbounded_stream():
    class Oversize(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(5):
                yield b'x' * 4096
            pytest.fail('Read beyond first over-limit chunk')
    with pytest.raises(ProviderError, match='request_rejected'):
        generate(adapter(lambda r: httpx.Response(400, headers={'Content-Type': 'application/json'}, stream=Oversize())))


@pytest.mark.parametrize('provider,model', [(KodeKeyProvider, 'gpt-6-luna'),
    (GeminiProvider, 'gemini-3.8-flash'), (MistralProvider, 'ministral-14b-latest')])
def test_other_providers_do_not_read_or_classify400(provider, model):
    client = provider(transport=httpx.MockTransport(lambda r: httpx.Response(400,
        headers={'Content-Type': 'application/json'}, stream=UnreadError())))
    with pytest.raises(ProviderError, match='request_rejected'):
        run(client.generate(KEY, model, (Message('user', 'Question'),), capabilities=CAPS))


@pytest.mark.parametrize('header,expected', [('7200', 7200), ('86400', 86400),
    ('5184000', 86400), ('0', 1), ('-1', None), ('NaN', None), ('1' * 129, None)])
def test_retry_after_preserves_daily_limits_with_documented_one_day_bound(header, expected):
    assert _retry_after(header) == expected


def test_shared_transport_keeps_response_and_total_time_bounds():
    with pytest.raises(ProviderError, match='response_too_large'):
        generate(adapter(lambda r: response(completion('x' * MAX_RESPONSE_BYTES))))
    async def slow(request):
        await anyio.sleep(1)
        return response(completion())
    with pytest.raises(ProviderError, match='timeout'):
        generate(adapter(slow, total_timeout=0.001))
    with pytest.raises(TypeError):
        GroqProvider(base_url='https://evil.invalid')
