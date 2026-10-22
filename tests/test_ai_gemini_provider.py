"""Native Gemini fixtures: no credentials, models or external network needed."""
from dataclasses import replace
import json

import anyio
import httpx
import pytest

from feedback.ai_providers import (
    GEMINI_BASE_URL, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, MAX_PROVIDER_STATE_BYTES,
    GeminiProvider, KodeKeyProvider, Message, ModelCapabilities, ProviderError,
    ToolSpec, Usage,
)

KEY = 'fixture-google-key'
MODEL = 'gemini-3.8-flash'
CAPS = ModelCapabilities(text_generation=True, tool_calling=True)
TOOL = ToolSpec('support_source', 'Return an approved support passage.',
    {'type': 'object', 'properties': {'source_id': {'type': 'string'}},
     'required': ['source_id'], 'additionalProperties': False})


def run(awaitable):
    async def invoke():
        return await awaitable
    return anyio.run(invoke)


def response(value, status=200, headers=None):
    return httpx.Response(status, headers={'Content-Type': 'application/json', **(headers or {})},
                          stream=httpx.ByteStream(json.dumps(value).encode()))


def completion(parts=None, reason='STOP', usage=None):
    return {'candidates': [{'content': {'role': 'model', 'parts': parts or [{'text': 'A grounded answer.'}]},
                            'finishReason': reason}], 'usageMetadata': usage}


def function(name=TOOL.name, args=None, identity=None, signature='c2lnbmF0dXJl'):
    value = {'name': name, 'args': {'source_id': 'source-1'} if args is None else args}
    if identity is not None:
        value['id'] = identity
    part = {'functionCall': value}
    if signature is not None:
        part['thoughtSignature'] = signature
    return part


def adapter(handler, **options):
    return GeminiProvider(transport=httpx.MockTransport(handler), **options)


def generate(client, messages=None, **options):
    return run(client.generate(KEY, MODEL, messages or (Message('user', 'Where is the guide?'),),
        **({'capabilities': CAPS} | options)))


def test_native_text_wire_fixed_host_header_and_thinking_usage():
    seen = []
    def handler(request):
        seen.append(request)
        return response(completion([
            {'thought': True, 'text': 'Private reasoning must not be a customer answer.', 'thoughtSignature': 'YWJj'},
            {'text': 'Use '}, {'text': 'the guide.', 'thoughtSignature': 'ZGVm'}],
            usage={'promptTokenCount': 10, 'candidatesTokenCount': 3, 'thoughtsTokenCount': 7, 'totalTokenCount': 20}))
    result = generate(adapter(handler), (Message('system', 'Use approved knowledge.'), Message('user', 'Where is the guide?')))
    assert result.text == 'Use the guide.' and result.usage == Usage(10, 10, 20)
    assert result.finish_reason == 'stop' and not result.tool_calls
    request = seen[0]
    assert str(request.url) == GEMINI_BASE_URL + '/models/' + MODEL + ':generateContent'
    assert request.headers['x-goog-api-key'] == KEY
    assert 'authorization' not in request.headers and KEY not in str(request.url)
    assert json.loads(request.content) == {
        'systemInstruction': {'parts': [{'text': 'Use approved knowledge.'}]},
        'contents': [{'role': 'user', 'parts': [{'text': 'Where is the guide?'}]}],
        'generationConfig': {'maxOutputTokens': 1024, 'responseModalities': ['TEXT']}}
    assert result.message().provider_state == result.provider_state
    assert 'Private reasoning' not in repr(result) + repr(result.message())
    assert 'YWJj' not in repr(result) + repr(result.message())


def test_catalogue_paginates_opaque_cursors_and_only_returns_text_candidates():
    requests = []
    def row(name, methods=('generateContent',)):
        return {'name': 'models/' + name, 'supportedGenerationMethods': list(methods)}
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return response({'models': [row(MODEL), row('gemini-2.5-flash'), row('embedding-001', ('embedContent',)),
                row('gemini-2.5-flash-image'), row('gemini-2.5-flash-preview-tts'), row('gemini-live-2.5-flash'),
                row('gemini-2.5-flash-native-audio-preview')], 'nextPageToken': 'cursor+/=&?target=https://bad.invalid'})
        return response({'models': [row('gemini-3.1-pro-preview'), row(MODEL)]})
    client = adapter(handler)
    assert run(client.list_models(KEY)) == ('gemini-2.5-flash', 'gemini-3.1-pro-preview', MODEL)
    assert len(requests) == 2
    assert all(r.url.host == 'generativelanguage.googleapis.com' and r.url.path == '/v1beta/models' for r in requests)
    assert requests[1].url.params['pageToken'] == 'cursor+/=&?target=https://bad.invalid'
    assert all(r.url.params['pageSize'] == '100' and 'key' not in r.url.params for r in requests)
    with pytest.raises(ProviderError, match='capability_unverified'):
        generate(client, capabilities=ModelCapabilities())
    assert len(requests) == 2


@pytest.mark.parametrize('value', [
    {'models': None}, {'models': [None]}, {'models': [{'name': 'models/../../foreign'}]},
    {'models': [{'name': 'models/gemini-3.8-flash', 'supportedGenerationMethods': 'generateContent'}]},
    {'models': [], 'nextPageToken': 12}, {'models': [], 'nextPageToken': 'x' * 2049},
    {'models': [{'name': 'models/gemini-3.8-flash'}] * 101},
])
def test_malformed_catalogues_fail_closed(value):
    with pytest.raises(ProviderError, match='invalid_response'):
        run(adapter(lambda r: response(value)).list_models(KEY))


def test_catalogue_cycles_and_endless_distinct_cursors_are_bounded():
    for same, expected in ((True, 2), (False, 10)):
        seen = []
        def handler(request):
            seen.append(request)
            return response({'models': [], 'nextPageToken': 'same' if same else str(len(seen))})
        with pytest.raises(ProviderError, match='invalid_response'):
            run(adapter(handler).list_models(KEY))
        assert len(seen) == expected


def test_parallel_tools_keep_order_ids_and_signed_parts_across_roundtrip():
    bodies = []
    parts = [{'text': 'Checking sources.'}, function(identity='native-id'),
             function(args={'source_id': 'source-2'}, signature=None)]
    def handler(request):
        bodies.append(json.loads(request.content))
        return response(completion(parts if len(bodies) == 1 else [{'text': 'Done.'}]))
    client = adapter(handler)
    result = generate(client, tools=(TOOL,))
    assert result.tool_calls[0].id == 'native-id'
    assert result.tool_calls[1].id.startswith('gemini-')
    assert len(bodies) == 1  # No implicit execution or second provider call.
    assert bodies[0]['tools'] == [{'functionDeclarations': [{'name': TOOL.name,
        'description': TOOL.description, 'parametersJsonSchema': TOOL.parameters}]}]
    assert bodies[0]['toolConfig'] == {'functionCallingConfig': {'mode': 'AUTO'}}
    final = generate(client, (Message('user', 'Find sources.'), result.message(),
        Message('tool', '{"excerpt":"First source"}', tool_call_id=result.tool_calls[0].id),
        Message('tool', 'Second source', tool_call_id=result.tool_calls[1].id)), tools=(TOOL,))
    assert final.text == 'Done.'
    assert bodies[1]['contents'][1] == {'role': 'model', 'parts': parts}
    assert bodies[1]['contents'][2] == {'role': 'user', 'parts': [
        {'functionResponse': {'name': TOOL.name, 'id': 'native-id', 'response': {'excerpt': 'First source'}}},
        {'functionResponse': {'name': TOOL.name, 'response': {'output': 'Second source'}}}]}


@pytest.mark.parametrize('response_format', ['text', 'json_object'])
def test_final_round_retains_signed_history_without_authorizing_new_tools(response_format):
    result = generate(adapter(lambda r: response(completion([function()]))), tools=(TOOL,))
    messages = (Message('user', 'Find it.'), result.message(),
                Message('tool', '{}', tool_call_id=result.tool_calls[0].id))
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return response(completion())
    assert generate(adapter(handler), messages, tools=(), response_format=response_format).text == 'A grounded answer.'
    assert 'tools' not in seen[0] and 'toolConfig' not in seen[0]
    assert seen[0]['contents'][1]['parts'][0] == function()
    assert seen[0]['generationConfig'].get('responseMimeType') == ('application/json' if response_format == 'json_object' else None)
    with pytest.raises(ProviderError, match='tool_not_enabled'):
        generate(adapter(lambda r: response(completion([function()]))), messages, tools=(), response_format=response_format)


@pytest.mark.parametrize('response_format', ['text', 'json_object'])
@pytest.mark.parametrize('tools_enabled', [False, True])
def test_json_mode_is_explicit_and_never_combined_with_advertised_tools(response_format, tools_enabled):
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return response(completion([function()] if tools_enabled else [{'text': '{"answer":"Guide"}'}]))
    client = adapter(handler)
    result = generate(client, tools=(TOOL,) if tools_enabled else (),
        required_tool=TOOL.name if tools_enabled else None, response_format=response_format)
    assert client.native_json_output is True
    assert CAPS.structured_output is False
    config = bodies[0]['generationConfig']
    assert 'candidateCount' not in config  # Unsupported by Gemini 3+; default is one.
    assert config.get('responseMimeType') == ('application/json' if response_format == 'json_object' and not tools_enabled else None)
    assert bool(result.tool_calls) is tools_enabled
    if tools_enabled:
        assert bodies[0]['toolConfig']['functionCallingConfig'] == {'mode': 'ANY', 'allowedFunctionNames': [TOOL.name]}
    else:
        assert result.text == '{"answer":"Guide"}'


def test_parallel_results_cannot_swap_same_named_unsigned_call_order():
    result = generate(adapter(lambda r: response(completion([
        function(signature=None), function(args={'source_id': 'source-2'}, signature=None)]))), tools=(TOOL,))
    with pytest.raises(ProviderError, match='invalid_request'):
        generate(adapter(lambda r: pytest.fail('Reordered results reached provider')),
            (Message('user', 'Find them.'), result.message(),
             Message('tool', '{}', tool_call_id=result.tool_calls[1].id),
             Message('tool', '{}', tool_call_id=result.tool_calls[0].id)), tools=(TOOL,))


def test_continuation_is_bound_to_provider_model_text_and_calls():
    result = generate(adapter(lambda r: response(completion([function()]))), tools=(TOOL,))
    message = result.message()
    foreign = json.loads(message.provider_state)
    foreign['model'] = 'gemini-other-model'
    mutations = [replace(message, content='changed'), replace(message, tool_calls=()),
                 replace(message, provider_state=json.dumps(foreign).encode()),
                 replace(message, provider_state=b'not json'), replace(message, provider_state=None),
                 replace(message, provider_state=b'x' * (MAX_PROVIDER_STATE_BYTES + 1))]
    def no_request(request):
        pytest.fail('Invalid continuation reached network')
    for changed in mutations:
        with pytest.raises(ProviderError, match='invalid_request'):
            generate(adapter(no_request), (Message('user', 'Find it.'), changed,
                Message('tool', '{}', tool_call_id=result.tool_calls[0].id)), tools=(TOOL,))
    with pytest.raises(ProviderError, match='invalid_request'):
        run(KodeKeyProvider(transport=httpx.MockTransport(no_request)).generate(KEY, 'kode-model',
            (Message('user', 'Find it.'), message), capabilities=CAPS, tools=(TOOL,)))


@pytest.mark.parametrize('tail', ['missing', 'wrong_id', 'duplicate', 'user_before_result'])
def test_tool_results_must_answer_exact_pending_calls_once(tail):
    result = generate(adapter(lambda r: response(completion([function(identity='call-1')]))), tools=(TOOL,))
    response_message = Message('tool', '{}', tool_call_id='call-1')
    extra = {'missing': [], 'wrong_id': [replace(response_message, tool_call_id='not-a-call')],
             'duplicate': [response_message, response_message], 'user_before_result': [Message('user', 'Skip result.')]}[tail]
    with pytest.raises(ProviderError, match='invalid_request'):
        generate(adapter(lambda r: pytest.fail('Invalid history reached provider')),
                 (Message('user', 'Find it.'), result.message(), *extra), tools=(TOOL,))


@pytest.mark.parametrize('parts,code', [
    ([{'text': 'x', 'inlineData': {'data': 'AAAA'}}], 'invalid_response'),
    ([{'text': 'hidden', 'thought': True}], 'invalid_response'),
    ([{'text': 'x', 'thought': 'true'}], 'invalid_response'),
    ([{'text': 'x', 'thoughtSignature': 'not base64!'}], 'invalid_response'),
    ([{'text': '\ud800'}], 'invalid_response'),
    ([{'functionCall': {'name': TOOL.name, 'args': []}}], 'invalid_response'),
    ([function(name='delete_everything')], 'tool_not_enabled'),
    ([function(identity='same'), function(identity='same')], 'invalid_response'),
    ([function()] * 9, 'invalid_response'),
    ([{'text': 'answer', 'functionCall': {'name': TOOL.name}}], 'invalid_response'),
    ([{'executableCode': {'code': 'dangerous'}}], 'invalid_response'),
    ([function(args={'amount': float('inf')})], 'invalid_response'),
])
def test_untrusted_parts_fail_closed(parts, code):
    with pytest.raises(ProviderError, match=code):
        generate(adapter(lambda r: response(completion(parts))), tools=(TOOL,))


@pytest.mark.parametrize('reason,code', [('MAX_TOKENS', 'incomplete_response'), ('SAFETY', 'refused'),
    ('RECITATION', 'refused'), ('SPII', 'refused'), ('PROHIBITED_CONTENT', 'refused'),
    ('MISSING_THOUGHT_SIGNATURE', 'invalid_response'), ('MALFORMED_FUNCTION_CALL', 'invalid_response'),
    ('UNEXPECTED_TOOL_CALL', 'invalid_response'), ('UNKNOWN', 'invalid_response')])
def test_unfinished_or_refused_answers_are_never_accepted(reason, code):
    with pytest.raises(ProviderError, match=code):
        generate(adapter(lambda r: response(completion(reason=reason))))


@pytest.mark.parametrize('value,code', [
    ({'promptFeedback': {'blockReason': 'SAFETY'}}, 'refused'),
    ({'promptFeedback': {'safetyRatings': [{'blocked': True}]}}, 'refused'),
    ({'candidates': []}, 'invalid_response'), ({'candidates': [None]}, 'invalid_response'),
    (completion(usage={'promptTokenCount': True}), 'invalid_response'),
    (completion(usage={'thoughtsTokenCount': -1}), 'invalid_response'),
    (completion(usage={'promptTokenCount': 10, 'totalTokenCount': 9}), 'invalid_response'),
    (completion(usage={'promptTokenCount': 10, 'candidatesTokenCount': 3, 'thoughtsTokenCount': 5, 'totalTokenCount': 12}), 'invalid_response'),
])
def test_prompt_blocks_candidate_shape_and_invalid_usage(value, code):
    with pytest.raises(ProviderError, match=code):
        generate(adapter(lambda r: response(value)))


def test_usage_missing_fields_remain_unknown_but_reported_thoughts_are_counted():
    assert generate(adapter(lambda r: response(completion()))).usage == Usage()
    assert generate(adapter(lambda r: response(completion(usage={
        'candidatesTokenCount': 2, 'thoughtsTokenCount': 8})))).usage == Usage(None, 10, None)
    assert generate(adapter(lambda r: response(completion(usage={
        'promptTokenCount': 10, 'totalTokenCount': 25})))).usage == Usage(10, 15, 25)


def test_probe_verifies_exact_model_and_full_signed_tool_roundtrip():
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        assert request.url.path.endswith('/' + MODEL + ':generateContent')
        if len(requests) == 2:
            return response(completion([function(name='verify_capability', args={'value': 'ok'})]))
        return response(completion([{'text': 'OK'}]))
    result = run(adapter(handler).probe_model(KEY, MODEL, tools=True))
    assert result == ModelCapabilities(text_generation=True, tool_calling=True)
    assert len(requests) == 3 and all(r['generationConfig']['maxOutputTokens'] == 1024 for r in requests)
    assert all('responseMimeType' not in r['generationConfig'] and 'candidateCount' not in r['generationConfig'] for r in requests)
    assert requests[1]['toolConfig'] == {'functionCallingConfig': {'mode': 'ANY', 'allowedFunctionNames': ['verify_capability']}}
    assert requests[2]['contents'][1]['parts'][0]['thoughtSignature'] == 'c2lnbmF0dXJl'
    assert requests[2]['contents'][2]['parts'][0]['functionResponse']['response'] == {'value': 'ok'}


def test_probe_does_not_claim_capability_after_wrong_text_tool_args_or_final_reply():
    for failure_at in (1, 2, 3):
        seen = []
        def handler(request):
            seen.append(request)
            if len(seen) == 2:
                return response(completion([function(name='verify_capability',
                    args={'value': 'wrong' if failure_at == 2 else 'ok'})]))
            return response(completion([{'text': 'Not OK' if len(seen) == failure_at else 'OK'}]))
        code = 'capability_unverified' if failure_at == 1 else 'tool_capability_unverified'
        with pytest.raises(ProviderError, match=code):
            run(adapter(handler).probe_model(KEY, MODEL, tools=True))
        assert len(seen) == failure_at


def test_catalogue_deadline_covers_all_pages_not_each_page_separately():
    seen = []
    async def handler(request):
        seen.append(request)
        await anyio.sleep(0.02)
        return response({'models': [], 'nextPageToken': str(len(seen))})
    with pytest.raises(ProviderError, match='timeout'):
        run(adapter(handler, total_timeout=0.03).list_models(KEY))
    assert len(seen) <= 2


def test_no_tools_or_unverified_tools_cannot_leak_through_adapter():
    with pytest.raises(ProviderError, match='tool_not_enabled'):
        generate(adapter(lambda r: response(completion([function()]))))
    with pytest.raises(ProviderError, match='tool_capability_unverified'):
        generate(adapter(lambda r: pytest.fail('Unverified capability reached provider')),
                 capabilities=ModelCapabilities(text_generation=True), tools=(TOOL,))


class UnreadableBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise AssertionError('Error body must never be read')
        yield b''


@pytest.mark.parametrize('status,code', [(302, 'redirect_refused'), (400, 'request_rejected'),
    (401, 'invalid_credentials'), (403, 'access_denied'), (404, 'model_or_endpoint_unavailable'),
    (429, 'rate_limited'), (503, 'provider_unavailable')])
def test_errors_never_read_raw_body_follow_redirects_retry_or_leak_key(status, code, caplog):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={'Location': 'https://bad.invalid', 'Retry-After': '99999'}, stream=UnreadableBody())
    with pytest.raises(ProviderError) as error:
        generate(adapter(handler))
    assert error.value.code == code and error.value.status == status
    assert error.value.retryable is (status in (429, 503))
    assert len(requests) == 1 and KEY not in caplog.text + str(error.value)


@pytest.mark.parametrize('model', ['models/gemini-3.8-flash', 'gemini-a/../../models', 'https://bad.invalid',
    'gemini-a?key=secret', 'gemini-a#fragment', 'gemini-a:generateContent', 'gemini-tts', 'gemini-image-preview'])
def test_model_cannot_change_destination_or_select_unsupported_output(model):
    with pytest.raises(ProviderError, match='invalid_request'):
        run(adapter(lambda r: pytest.fail('Invalid destination reached network')).generate(
            KEY, model, (Message('user', 'hello'),), capabilities=CAPS))


def test_requests_responses_and_deadlines_are_bounded():
    with pytest.raises(ProviderError, match='invalid_request'):
        generate(adapter(lambda r: pytest.fail('Oversized request reached network')),
                 (Message('user', 'x' * MAX_REQUEST_BYTES),))
    with pytest.raises(ProviderError, match='response_too_large'):
        generate(adapter(lambda r: httpx.Response(200, headers={'Content-Type': 'application/json'},
            stream=httpx.ByteStream(b'x' * (MAX_RESPONSE_BYTES + 1)))))
    with pytest.raises(ProviderError, match='invalid_response'):
        generate(adapter(lambda r: response(completion([{'text': 'x' * MAX_PROVIDER_STATE_BYTES}]))))
    async def slow(request):
        await anyio.sleep(0.05)
        return response(completion())
    with pytest.raises(ProviderError, match='timeout'):
        generate(adapter(slow, total_timeout=0.001))


@pytest.mark.parametrize('headers,raw,code', [
    ({'Content-Type': 'text/html'}, b'<html>private upstream body</html>', 'invalid_response'),
    ({'Content-Type': 'application/json', 'Content-Encoding': 'gzip'}, b'unread compressed body', 'invalid_response'),
    ({'Content-Type': 'application/json', 'Content-Length': str(MAX_RESPONSE_BYTES + 1)}, b'{}', 'response_too_large'),
    ({'Content-Type': 'application/json'}, b'{"candidates":[],"candidates":[]}', 'invalid_response'),
    ({'Content-Type': 'application/json'}, b'{not-json private upstream body', 'invalid_response'),
])
def test_bad_success_envelopes_are_safe(headers, raw, code, caplog):
    with pytest.raises(ProviderError, match=code) as error:
        generate(adapter(lambda r: httpx.Response(200, headers=headers, stream=httpx.ByteStream(raw))))
    assert 'private upstream body' not in caplog.text + str(error.value)
