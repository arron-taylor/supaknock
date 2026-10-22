"""Official Mistral wire fixtures; never real keys or billable calls."""
from dataclasses import replace
import json

import anyio
import httpx
import pytest

from feedback.ai_providers import (
    MISTRAL_BASE_URL, MAX_RESPONSE_BYTES, MAX_REQUEST_BYTES, MAX_PROVIDER_STATE_BYTES,
    MistralProvider, KodeKeyProvider, GeminiProvider, ModelCapabilities, Message,
    ToolCall, ToolSpec, Usage, ProviderError,
)

KEY = 'fixture-mistral-private-key'
MODEL = 'ministral-14b-2512'
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
    return {'choices': [{'index': 0, 'finish_reason': reason or ('tool_calls' if calls else 'stop'),
        'message': {'role': 'assistant', 'content': content, 'tool_calls': calls}}], 'usage': usage}


def call(identity='call12345', name=TOOL.name, arguments='{"query":"billing"}'):
    return {'id': identity, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}


def thinking():
    return {'type': 'thinking', 'thinking': [{'type': 'text', 'text': 'Private internal reasoning.'}],
            'signature': 'opaque-native-signature', 'closed': True}


def adapter(handler, **options):
    return MistralProvider(transport=httpx.MockTransport(handler), **options)


def generate(client, messages=None, **options):
    return run(client.generate(KEY, MODEL, messages or (Message('user', 'Where is billing help?'),),
                               **({'capabilities': CAPS} | options)))


def test_catalogue_keeps_exact_available_chat_ids_including_fine_tunes():
    requests = []
    tuned = 'ft:ministral-14b-2512:workspace:20261003:opaque-ID'
    def row(identity, **options):
        return {'id': identity, 'capabilities': {'completion_chat': True, 'function_calling': True}, **options}
    def handler(request):
        requests.append(request)
        return response({'object': 'list', 'data': [row(MODEL, aliases=['not-an-id']), row(tuned), row(tuned),
            row('case/Exact-ID'), row('case/exact-id'), row('hidden', internal=True), row('archived', archived=True),
            row('mistral-embed', capabilities={'completion_chat': False}), row('ocr', capabilities={})]})
    client = adapter(handler)
    assert run(client.list_models(KEY)) == tuple(sorted((MODEL, tuned, 'case/Exact-ID', 'case/exact-id')))
    request = requests[0]
    assert str(request.url) == MISTRAL_BASE_URL + '/models' and request.method == 'GET'
    assert request.headers['authorization'] == 'Bearer ' + KEY
    assert not request.url.query and 'x-goog-api-key' not in request.headers
    with pytest.raises(ProviderError, match='capability_unverified'):
        generate(client, capabilities=ModelCapabilities())
    assert len(requests) == 1


@pytest.mark.parametrize('value', [{}, [], {'data': [None]}, {'data': [{'id': MODEL}]},
    {'data': [{'id': 'contains space', 'capabilities': {}}]},
    {'data': [{'id': MODEL, 'capabilities': {'completion_chat': 1}}]},
    {'data': [{'id': MODEL, 'capabilities': {}, 'archived': 'false'}]},
    {'data': [{'id': MODEL, 'capabilities': {}, 'internal': None}]},
    {'data': [{'id': MODEL, 'capabilities': {}}] * 1001}])
def test_malformed_catalogues_fail_closed(value):
    with pytest.raises(ProviderError, match='invalid_response'):
        run(adapter(lambda r: response(value)).list_models(KEY))


def test_plain_text_wire_and_usage_omit_unrequested_features():
    requests = []
    def handler(request):
        requests.append(request)
        return response(completion(usage={'prompt_tokens': 12, 'completion_tokens': 4, 'total_tokens': 16}))
    result = generate(adapter(handler), (Message('system', 'Only approved knowledge.'), Message('user', 'Where is billing help?')))
    assert result.text == 'An approved answer.' and result.usage == Usage(12, 4, 16)
    assert result.provider_state is None and result.message() == Message('assistant', result.text)
    assert str(requests[0].url) == MISTRAL_BASE_URL + '/chat/completions'
    assert json.loads(requests[0].content) == {'model': MODEL, 'messages': [
        {'role': 'system', 'content': 'Only approved knowledge.'}, {'role': 'user', 'content': 'Where is billing help?'}],
        'max_tokens': 1024, 'stream': False}
    assert KEY not in repr(result) + str(requests[0].url)


@pytest.mark.parametrize('arguments', ['{"query":"billing"}', {'query': 'billing'}])
def test_function_arguments_accept_documented_string_or_object_and_roundtrip(arguments):
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return response(completion(None, [call(arguments=arguments)]) if len(bodies) == 1 else completion('See the guide.'))
    client = adapter(handler)
    result = generate(client, tools=(TOOL,))
    assert result.tool_calls == (ToolCall('call12345', TOOL.name, {'query': 'billing'}),)
    assert len(bodies) == 1
    assert bodies[0]['tools'] == [{'type': 'function', 'function': {
        'name': TOOL.name, 'description': TOOL.description, 'parameters': TOOL.parameters}}]
    assert bodies[0]['tool_choice'] == 'auto'
    final = generate(client, (Message('user', 'Where is billing help?'), result.message(),
        Message('tool', '{"excerpt":"Approved guide"}', tool_call_id='call12345')), tools=())
    assert final.text == 'See the guide.' and 'tools' not in bodies[1]
    assert bodies[1]['messages'][1]['tool_calls'] == [call()]
    assert bodies[1]['messages'][2] == {'role': 'tool', 'name': TOOL.name,
        'content': '{"excerpt":"Approved guide"}', 'tool_call_id': 'call12345'}


@pytest.mark.parametrize('response_format', ['text', 'json_object'])
def test_reasoning_and_signature_replay_privately_even_when_final_round_disables_tools(response_format):
    chunks = [thinking(), {'type': 'text', 'text': 'Checking the guide.'}]
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return response(completion(chunks, [call(arguments={'query': 'billing'})]))
        return response(completion([thinking(), {'type': 'text', 'text': 'Use '}, {'type': 'text', 'text': 'the guide.'}]))
    client = adapter(handler)
    first = generate(client, tools=(TOOL,))
    assert first.text == 'Checking the guide.'
    assert 'Private internal' not in repr(first) + repr(first.message())
    assert 'opaque-native' not in repr(first) + repr(first.message())
    history = (Message('user', 'Find guide.'), first.message(), Message('tool', '{}', tool_call_id='call12345'))
    final = generate(client, history, response_format=response_format)
    assert final.text == 'Use the guide.'
    assert bodies[1]['messages'][1]['content'] == chunks
    assert bodies[1]['messages'][1]['tool_calls'] == [call(arguments={'query': 'billing'})]
    assert 'tools' not in bodies[1] and 'tool_choice' not in bodies[1]
    assert bodies[1].get('response_format') == ({'type': 'json_object'} if response_format == 'json_object' else None)
    with pytest.raises(ProviderError, match='tool_not_enabled'):
        generate(adapter(lambda r: response(completion(None, [call(identity='other1234')]))), history, response_format=response_format)


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
    assert bool(result.tool_calls) is tools_enabled
    if tools_enabled:
        assert bodies[0]['tool_choice']['function']['name'] == TOOL.name
    else:
        assert result.text == '{"answer":"Guide"}'


def test_reasoning_continuation_is_bound_to_exact_model_text_and_calls():
    first = generate(adapter(lambda r: response(completion([thinking()], [call()]))), tools=(TOOL,))
    message = first.message()
    state = json.loads(message.provider_state)
    wrong_model = dict(state, model='other-model')
    wrong_provider = dict(state, provider='gemini')
    changes = [replace(message, content='different'), replace(message, tool_calls=()),
        replace(message, provider_state=json.dumps(wrong_model).encode()),
        replace(message, provider_state=json.dumps(wrong_provider).encode()),
        replace(message, provider_state=b'not-json'),
        replace(message, provider_state=b'x' * (MAX_PROVIDER_STATE_BYTES + 1))]
    def no_request(request):
        pytest.fail('Invalid continuation reached network')
    for changed in changes:
        with pytest.raises(ProviderError, match='invalid_request'):
            generate(adapter(no_request), (Message('user', 'Find guide.'), changed,
                Message('tool', '{}', tool_call_id='call12345')), tools=(TOOL,))
    for other, model in ((KodeKeyProvider, 'other-model'), (GeminiProvider, 'gemini-3.8-flash')):
        with pytest.raises(ProviderError, match='invalid_request'):
            run(other(transport=httpx.MockTransport(no_request)).generate(KEY, model,
                (Message('user', 'Find guide.'), message, Message('tool', '{}', tool_call_id='call12345')),
                capabilities=CAPS, tools=(TOOL,)))


@pytest.mark.parametrize('case', ['missing', 'orphan', 'duplicate', 'wrong_id', 'interleaved', 'reordered'])
def test_pending_tool_results_are_exact_once_in_order_before_new_messages(case):
    first = generate(adapter(lambda r: response(completion(None, [call(), call(identity='call67890')]))), tools=(TOOL,))
    one = Message('tool', '{}', tool_call_id='call12345')
    two = Message('tool', '{}', tool_call_id='call67890')
    prefix = [Message('user', 'Find guide.'), first.message()]
    if case == 'orphan':
        history = [Message('user', 'Find guide.'), one]
    else:
        suffix = {'missing': [one], 'duplicate': [one, one], 'wrong_id': [replace(one, tool_call_id='unknown')],
            'interleaved': [one, Message('user', 'Continue.')], 'reordered': [two, one]}[case]
        history = prefix + suffix
    with pytest.raises(ProviderError, match='invalid_request'):
        generate(adapter(lambda r: pytest.fail('Malformed tool history reached network')), history, tools=(TOOL,))


def test_tool_call_ids_cannot_be_reused_in_later_response():
    first = generate(adapter(lambda r: response(completion(None, [call()]))), tools=(TOOL,))
    with pytest.raises(ProviderError, match='invalid_response'):
        generate(adapter(lambda r: response(completion(None, [call()]))),
            (Message('user', 'Find guide.'), first.message(), Message('tool', '{}', tool_call_id='call12345')), tools=(TOOL,))


@pytest.mark.parametrize('content', [[thinking()], [{'type': 'text', 'text': 12}],
    [{'type': 'image_url', 'image_url': 'https://bad.invalid/image'}],
    [{'type': 'reference', 'reference_ids': ['invented']}],
    [dict(thinking(), signature=42)], [dict(thinking(), closed=False)],
    [dict(thinking(), thinking=[{'type': 'reference', 'text': 'hidden'}])],
    [{'type': 'text', 'text': '\ud800'}], [dict(thinking(), thinking='not-list')],
    [thinking()] * 129, {'text': 'not-a-valid-shape'}])
def test_malformed_or_thinking_only_outputs_are_not_customer_answers(content):
    with pytest.raises(ProviderError, match='invalid_response'):
        generate(adapter(lambda r: response(completion(content))))


@pytest.mark.parametrize('wire', [call(identity='null'), call(identity=''), call(identity='has space'),
    call(arguments='{"query":"one","query":"two"}'), call(arguments='[]'),
    call(arguments='{"value":1e309}'), call(arguments={'value': float('nan')}),
    call(arguments={'value': '\ud800'}), call(arguments='x' * 16385),
    dict(call(), type='web_search'), dict(call(), index=True)])
def test_untrusted_tool_shapes_fail_closed(wire):
    with pytest.raises(ProviderError, match='invalid_response'):
        generate(adapter(lambda r: response(completion(None, [wire]))), tools=(TOOL,))


@pytest.mark.parametrize('wire', [[call(), call()], [call(identity=str(i)) for i in range(9)]])
def test_duplicate_or_excess_calls_fail_closed(wire):
    with pytest.raises(ProviderError, match='invalid_response'):
        generate(adapter(lambda r: response(completion(None, wire))), tools=(TOOL,))


def test_disabled_unverified_or_unknown_tools_cannot_reach_dispatch():
    with pytest.raises(ProviderError, match='tool_not_enabled'):
        generate(adapter(lambda r: response(completion(None, [call()]))))
    with pytest.raises(ProviderError, match='tool_not_enabled'):
        generate(adapter(lambda r: response(completion(None, [call(name='delete_data')]))), tools=(TOOL,))
    with pytest.raises(ProviderError, match='tool_capability_unverified'):
        generate(adapter(lambda r: pytest.fail('Unverified tools reached network')),
                 capabilities=ModelCapabilities(text_generation=True), tools=(TOOL,))


@pytest.mark.parametrize('reason,code', [('length', 'incomplete_response'), ('model_length', 'incomplete_response'),
    ('error', 'invalid_response'), ('content_filter', 'refused'), ('unknown', 'invalid_response')])
def test_incomplete_refused_or_error_results_fail_closed(reason, code):
    with pytest.raises(ProviderError, match=code):
        generate(adapter(lambda r: response(completion(reason=reason))))


def test_choice_consistency_refusals_and_reported_usage():
    bad = [completion(None), completion(None, [call()], reason='stop'), completion(reason='tool_calls'),
           completion(usage={'total_tokens': True}), completion(usage={'prompt_tokens': -1}),
           completion(usage={'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 11})]
    for value in bad:
        with pytest.raises(ProviderError, match='invalid_response'):
            generate(adapter(lambda r: response(value)), tools=(TOOL,))
    refused = completion()
    refused['choices'][0]['message']['refusal'] = 'Untrusted refusal text'
    with pytest.raises(ProviderError, match='refused') as error:
        generate(adapter(lambda r: response(refused)))
    assert 'Untrusted' not in str(error.value)
    assert generate(adapter(lambda r: response(completion()))).usage == Usage()
    result = generate(adapter(lambda r: response(completion([thinking(), {'type': 'text', 'text': 'Answer.'}],
        usage={'prompt_tokens': 10, 'completion_tokens': 120, 'total_tokens': 130}))))
    assert result.usage == Usage(10, 120, 130)


def test_exact_model_probe_verifies_full_tool_roundtrip_without_actions():
    bodies = []
    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert body['model'] == MODEL and body['max_tokens'] == 1024
        if len(bodies) == 2:
            return response(completion([thinking()], [call(name='verify_capability', arguments={'value': 'ok'})]))
        return response(completion('OK'))
    assert run(adapter(handler).probe_model(KEY, MODEL, tools=True)) == ModelCapabilities(text_generation=True, tool_calling=True)
    assert len(bodies) == 3
    assert all('response_format' not in body for body in bodies)
    assert bodies[1]['tool_choice'] == {'type': 'function', 'function': {'name': 'verify_capability'}}
    assert bodies[2]['messages'][1]['content'] == [thinking()]
    assert bodies[2]['messages'][2] == {'role': 'tool', 'name': 'verify_capability', 'content': '{"value":"ok"}', 'tool_call_id': 'call12345'}


@pytest.mark.parametrize('failure_at', [1, 2, 3])
def test_probe_does_not_overstate_text_or_tool_support(failure_at):
    seen = []
    def handler(request):
        seen.append(request)
        if len(seen) == 2:
            return response(completion(None, [call(name='verify_capability', arguments={'value': 'wrong' if failure_at == 2 else 'ok'})]))
        return response(completion('Wrong' if len(seen) == failure_at else 'OK'))
    with pytest.raises(ProviderError, match='capability_unverified'):
        run(adapter(handler).probe_model(KEY, MODEL, tools=True))
    assert len(seen) == failure_at


class UnreadableBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise AssertionError('Error body must not be read')
        yield b''


@pytest.mark.parametrize('status,code', [(302, 'redirect_refused'), (400, 'request_rejected'),
    (401, 'invalid_credentials'), (403, 'access_denied'), (404, 'model_or_endpoint_unavailable'),
    (422, 'request_rejected'), (429, 'rate_limited'), (503, 'provider_unavailable')])
def test_errors_do_not_read_bodies_retry_follow_redirects_or_log_secrets(status, code, caplog):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(status, headers={'Location': 'https://bad.invalid/steal', 'Retry-After': '99999'}, stream=UnreadableBody())
    with pytest.raises(ProviderError) as error:
        generate(adapter(handler))
    assert error.value.code == code and error.value.status == status
    assert error.value.retryable is (status in (429, 503))
    assert len(seen) == 1 and KEY not in caplog.text + str(error.value) + repr(error.value)


def test_network_and_body_bounds_and_fixed_destination():
    with pytest.raises(ProviderError, match='invalid_request'):
        generate(adapter(lambda r: pytest.fail('Oversized prompt reached network')), (Message('user', 'x' * MAX_REQUEST_BYTES),))
    with pytest.raises(ProviderError, match='response_too_large'):
        generate(adapter(lambda r: httpx.Response(200, headers={'Content-Type': 'application/json'},
            stream=httpx.ByteStream(b'x' * (MAX_RESPONSE_BYTES + 1)))))
    with pytest.raises(ProviderError, match='invalid_response'):
        generate(adapter(lambda r: response(completion([{'type': 'text', 'text': 'x' * MAX_PROVIDER_STATE_BYTES}]))))
    async def slow(request):
        await anyio.sleep(0.05)
        return response(completion())
    with pytest.raises(ProviderError, match='timeout'):
        generate(adapter(slow, total_timeout=0.001))
    seen = []
    def fixed(request):
        seen.append(request)
        return response(completion())
    run(adapter(fixed).generate(KEY, 'ft:opaque/model:ID', (Message('user', 'hello'),), capabilities=CAPS))
    assert str(seen[0].url) == MISTRAL_BASE_URL + '/chat/completions'
    assert json.loads(seen[0].content)['model'] == 'ft:opaque/model:ID'
    with pytest.raises(TypeError):
        MistralProvider(base_url='https://bad.invalid')


def test_common_transport_still_disables_environment_proxies_and_redirects(monkeypatch):
    original = httpx.AsyncClient
    seen = []
    def client(*args, **kwargs):
        seen.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    monkeypatch.setenv('HTTPS_PROXY', 'https://bad.invalid/steal')
    generate(adapter(lambda r: response(completion())))
    assert seen[0]['trust_env'] is False and seen[0]['follow_redirects'] is False
    assert seen[0]['timeout'].read == 15 and seen[0]['timeout'].connect == 5
