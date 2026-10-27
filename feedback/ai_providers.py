"""Provider protocol translation only; callers own authorization and tool execution.

No credentials, prompts, or raw upstream errors are logged or retained here. Calls
make one attempt: the durable application worker owns retries and usage budgets.
"""
import base64
import binascii
import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import re
from types import MappingProxyType
from typing import Literal, Protocol
from uuid import uuid4

import anyio
import httpx


KODEKEY_BASE_URL = 'https://api.ai.kodekloud.com/v1'
GEMINI_BASE_URL = 'https://generativelanguage.googleapis.com/v1beta'
MISTRAL_BASE_URL = 'https://api.mistral.ai/v1'
GROQ_BASE_URL = 'https://api.groq.com/openai/v1'
MAX_REQUEST_BYTES = 262_144
MAX_RESPONSE_BYTES = 1_048_576
MAX_MODELS = 1_000
MAX_MESSAGES = 128
MAX_TOOLS = 8
MAX_ARGUMENT_BYTES = 16_384
MAX_OUTPUT_TOKENS = 8_192
MAX_PROVIDER_STATE_BYTES = 131_072
TOOL_NAME = re.compile(r'[a-zA-Z_][a-zA-Z0-9_]{0,63}\Z')


class ProviderError(Exception):
    """Safe diagnostics only. Never construct these fields from upstream text."""

    def __init__(self, code, *, retryable=False, status=None, retry_after=None):
        self.code = code
        self.retryable = retryable
        self.status = status
        self.retry_after = retry_after
        super().__init__(code)


@dataclass(frozen=True)
class ModelCapabilities:
    # The application stores this evidence with the exact provider/key/model
    # configuration version. Catalogue membership cannot set these booleans.
    text_generation: bool = False
    tool_calling: bool = False
    streaming: bool = False
    structured_output: bool = False


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str = field(repr=False)
    parameters: dict = field(repr=False)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict = field(repr=False)


@dataclass(frozen=True)
class Message:
    role: str
    content: str | None = field(default=None, repr=False)
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    # Transient adapter-owned continuation, never persisted or sent to a client.
    provider_state: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class GenerationResult:
    text: str = field(repr=False)
    tool_calls: tuple[ToolCall, ...]
    usage: Usage
    finish_reason: str
    provider_state: bytes | None = field(default=None, repr=False)

    def message(self):
        return Message('assistant', self.text or None, self.tool_calls,
                       provider_state=self.provider_state)


class Provider(Protocol):
    id: str
    name: str
    # Reviewed wire support only, not observed account/model capability evidence.
    # JSON mode is requested only without currently advertised tools; callers
    # still validate the complete result and its application-specific schema.
    native_json_output: bool
    native_strict_json_output: bool
    plain_text_context: bool

    async def list_models(self, key: str) -> tuple[str, ...]: ...

    async def probe_model(self, key: str, model: str, *, tools: bool = False) -> ModelCapabilities: ...

    async def generate(self, key: str, model: str, messages: tuple[Message, ...], *,
                       capabilities: ModelCapabilities, tools: tuple[ToolSpec, ...] = (),
                       max_output_tokens: int = 1024,
                       required_tool: str | None = None,
                       response_format: Literal['text', 'json_object', 'json_schema'] = 'text',
                       response_schema: dict | None = None) -> GenerationResult: ...


def _identifier(value, maximum=256):
    return isinstance(value, str) and 1 <= len(value) <= maximum and all(32 < ord(c) < 127 for c in value)


def _json_bytes(value, maximum, code='invalid_request'):
    try:
        body = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise ProviderError(code) from None
    if len(body) > maximum:
        raise ProviderError(code)
    return body


def _validate_output_schema(schema):
    """Bound an application-owned schema. It is never supplied by HTTP clients."""
    _json_bytes(schema, 32768)
    if (not isinstance(schema, dict) or schema.get('type') != 'object'
            or not isinstance(schema.get('properties'), dict)
            or not isinstance(schema.get('required'), list)
            or any(not isinstance(name, str) for name in schema['required'])
            or len(schema['required']) != len(schema['properties'])
            or set(schema['required']) != set(schema['properties'])
            or schema.get('additionalProperties') is not False):
        raise ProviderError('invalid_request')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError()


def _json_loads(raw):
    try:
        return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise ProviderError('invalid_response') from None


def _tool_call_wire(call):
    if not isinstance(call, ToolCall) or not _identifier(call.id, 128) or not isinstance(call.name, str) or not TOOL_NAME.fullmatch(call.name) or not isinstance(call.arguments, dict):
        raise ProviderError('invalid_request')
    arguments = _json_bytes(call.arguments, MAX_ARGUMENT_BYTES).decode('utf-8')
    return {'id': call.id, 'type': 'function', 'function': {'name': call.name, 'arguments': arguments}}


def _message_wire(message):
    if not isinstance(message, Message) or message.role not in ('system', 'user', 'assistant', 'tool'):
        raise ProviderError('invalid_request')
    if message.provider_state is not None:
        raise ProviderError('invalid_request')
    if message.content is not None and not isinstance(message.content, str):
        raise ProviderError('invalid_request')
    result = {'role': message.role, 'content': message.content}
    if message.tool_calls:
        if message.role != 'assistant' or len(message.tool_calls) > MAX_TOOLS:
            raise ProviderError('invalid_request')
        result['tool_calls'] = [_tool_call_wire(call) for call in message.tool_calls]
    if message.role == 'tool':
        if not _identifier(message.tool_call_id, 128):
            raise ProviderError('invalid_request')
        result['tool_call_id'] = message.tool_call_id
    elif message.tool_call_id is not None:
        raise ProviderError('invalid_request')
    if message.content is None and not message.tool_calls:
        raise ProviderError('invalid_request')
    return result


def _tool_wire(tool):
    if not isinstance(tool, ToolSpec) or not isinstance(tool.name, str) or not TOOL_NAME.fullmatch(tool.name) or not isinstance(tool.description, str) or len(tool.description) > 2048 or not isinstance(tool.parameters, dict) or tool.parameters.get('type') != 'object':
        raise ProviderError('invalid_request')
    _json_bytes(tool.parameters, MAX_ARGUMENT_BYTES)
    return {'type': 'function', 'function': {'name': tool.name, 'description': tool.description, 'parameters': tool.parameters}}


def _usage(value):
    if value is None:
        return Usage()
    if not isinstance(value, dict):
        raise ProviderError('invalid_response')
    counts = []
    for name in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
        count = value.get(name)
        if count is not None and (type(count) is not int or not 0 <= count < 2 ** 63):
            raise ProviderError('invalid_response')
        counts.append(count)
    return Usage(*counts)


def _retry_after(value):
    if not value or len(value) > 128:
        return None
    try:
        if value.isascii() and value.isdigit():
            seconds = int(value)
        else:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                return None
            seconds = int((target - datetime.now(timezone.utc)).total_seconds())
        # Recovery workers never wait longer than one day on an untrusted
        # header. Daily organization limits must not become hourly retries.
        return min(86_400, max(1, seconds))
    except (ValueError, TypeError, OverflowError):
        return None


def _http_error(status, retry_after):
    if status == 401:
        return ProviderError('invalid_credentials', status=status)
    if status == 402:
        return ProviderError('payment_required', status=status)
    if status == 403:
        return ProviderError('access_denied', status=status)
    if status == 404:
        return ProviderError('model_or_endpoint_unavailable', status=status)
    if 300 <= status < 400:
        return ProviderError('redirect_refused', status=status)
    if status == 429:
        return ProviderError('rate_limited', retryable=True, status=status, retry_after=_retry_after(retry_after))
    if status in (408, 425) or 500 <= status < 600:
        return ProviderError('provider_unavailable', retryable=True, status=status, retry_after=_retry_after(retry_after))
    return ProviderError('request_rejected', status=status)


async def _groq_quota_error(response):
    """Recognize one documented code, without retaining any upstream text.

    https://console.groq.com/docs/spend-limits documents organization-wide
    HTTP 400 / blocked_api_access when the monthly spend cap is reached.
    All malformed, oversized or differently encoded bodies stay generic 400.
    """
    if response.headers.get('content-encoding', 'identity').lower() != 'identity' or response.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
        return False
    length = response.headers.get('content-length')
    if length is not None and (not length.isascii() or not length.isdigit() or len(length) > 12 or int(length) > 16_384):
        return False
    raw = bytearray()
    try:
        async for chunk in response.aiter_raw(chunk_size=4096):
            if len(raw) + len(chunk) > 16_384:
                return False
            raw.extend(chunk)
        value = _json_loads(raw)
        return isinstance(value, dict) and isinstance(value.get('error'), dict) and value['error'].get('code') == 'blocked_api_access'
    except (ProviderError, httpx.HTTPError, OSError):
        return False
    finally:
        raw.clear()


async def _request_json(transport, total_timeout, method, url, headers, body=None, params=None, *, groq_errors=False, extensions=None, read_timeout=15):
    """One bounded request to an adapter-owned fixed URL; no raw error bodies."""
    encoded = _json_bytes(body, MAX_REQUEST_BYTES) if body is not None else None
    try:
        with anyio.fail_after(total_timeout):
            async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                         timeout=httpx.Timeout(15, connect=5, pool=5, read=min(read_timeout, total_timeout)),
                                         limits=httpx.Limits(max_connections=1, max_keepalive_connections=0)) as client:
                async with client.stream(method, url, params=params, headers=headers, content=encoded, extensions=extensions) as response:
                    if not 200 <= response.status_code < 300:
                        if groq_errors and response.status_code == 400 and await _groq_quota_error(response):
                            raise ProviderError('quota_exhausted', status=400)
                        raise _http_error(response.status_code, response.headers.get('retry-after'))
                    if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                        raise ProviderError('invalid_response')
                    if response.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
                        raise ProviderError('invalid_response')
                    length = response.headers.get('content-length')
                    if length is not None and (not length.isascii() or not length.isdigit() or len(length) > 12 or int(length) > MAX_RESPONSE_BYTES):
                        raise ProviderError('response_too_large')
                    raw = bytearray()
                    async for chunk in response.aiter_raw(chunk_size=16_384):
                        if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ProviderError('response_too_large')
                        raw.extend(chunk)
                    return _json_loads(raw)
    except ProviderError:
        raise
    except (httpx.TimeoutException, TimeoutError):
        raise ProviderError('timeout', retryable=True) from None
    except (httpx.HTTPError, OSError):
        raise ProviderError('network_error', retryable=True) from None


class KodeKeyProvider:
    id = 'kodekey'
    name = 'KodeKloud KodeKey'
    # Do not assume optional OpenAI parameters are supported by this gateway.
    native_json_output = False
    native_strict_json_output = True
    plain_text_context = True

    def __init__(self, *, transport=None, total_timeout=45.0):
        # Transport injection is only for application-owned fixture tests, never
        # an API/configuration field. There is deliberately no base URL option.
        if isinstance(total_timeout, bool) or not isinstance(total_timeout, (int, float)) or not 0 < total_timeout <= 60:
            raise ValueError('Provider timeout must be between zero and 60 seconds')
        self._transport = transport
        self._total_timeout = total_timeout

    async def _request(self, key, method, path, body=None):
        if not _identifier(key, 4096):
            raise ProviderError('invalid_credentials')
        headers = {'Authorization': 'Bearer ' + key, 'Accept': 'application/json',
                   'Accept-Encoding': 'identity', 'Content-Type': 'application/json',
                   'User-Agent': 'SuggestionBox/0.2'}
        return await _request_json(self._transport, self._total_timeout, method,
                                   KODEKEY_BASE_URL + path, headers, body)

    async def list_models(self, key):
        value = await self._request(key, 'GET', '/models')
        if not isinstance(value, dict) or not isinstance(value.get('data'), list) or len(value['data']) > MAX_MODELS:
            raise ProviderError('invalid_response')
        models = []
        for item in value['data']:
            if not isinstance(item, dict) or not _identifier(item.get('id')):
                raise ProviderError('invalid_response')
            models.append(item['id'])
        # Exact IDs are kept; arbitrary metadata cannot advertise permissions.
        return tuple(sorted(set(models)))

    async def generate(self, key, model, messages, *, capabilities, tools=(), max_output_tokens=1024, required_tool=None, response_format='text', response_schema=None):
        if not isinstance(capabilities, ModelCapabilities) or capabilities.text_generation is not True:
            raise ProviderError('capability_unverified')
        if not isinstance(response_format, str) or response_format not in ('text', 'json_object', 'json_schema'):
            raise ProviderError('invalid_request')
        if response_format == 'json_schema':
            _validate_output_schema(response_schema)
            if capabilities.structured_output is not True:
                raise ProviderError('structured_output_unverified')
            if tools or required_tool is not None:
                raise ProviderError('invalid_request')
        elif response_schema is not None:
            raise ProviderError('invalid_request')
        if not _identifier(model) or type(max_output_tokens) is not int or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS:
            raise ProviderError('invalid_request')
        if not isinstance(messages, (tuple, list)) or not 1 <= len(messages) <= MAX_MESSAGES or not isinstance(tools, (tuple, list)) or len(tools) > MAX_TOOLS:
            raise ProviderError('invalid_request')
        if tools and capabilities.tool_calling is not True:
            raise ProviderError('tool_capability_unverified')
        wire_tools = [_tool_wire(tool) for tool in tools]
        enabled_names = {tool.name for tool in tools}
        if len(enabled_names) != len(tools) or (required_tool is not None and (not isinstance(required_tool, str) or required_tool not in enabled_names)):
            raise ProviderError('invalid_request')
        body = {'model': model, 'messages': [_message_wire(message) for message in messages],
                'max_tokens': max_output_tokens, 'stream': False}
        if response_format == 'json_schema':
            body['response_format'] = {'type': 'json_schema', 'json_schema': {
                'name': 'support_result', 'strict': True, 'schema': response_schema}}
        if wire_tools:
            body['tools'] = wire_tools
            body['tool_choice'] = {'type': 'function', 'function': {'name': required_tool}} if required_tool else 'auto'
        value = await self._request(key, 'POST', '/chat/completions', body)
        if not isinstance(value, dict) or not isinstance(value.get('choices'), list) or len(value['choices']) != 1:
            raise ProviderError('invalid_response')
        choice = value['choices'][0]
        if not isinstance(choice, dict) or not isinstance(choice.get('message'), dict):
            raise ProviderError('invalid_response')
        message = choice['message']
        if message.get('role') != 'assistant':
            raise ProviderError('invalid_response')
        if message.get('refusal') or choice.get('finish_reason') == 'content_filter':
            raise ProviderError('refused')
        if choice.get('finish_reason') == 'length':
            raise ProviderError('incomplete_response')
        if choice.get('finish_reason') not in ('stop', 'tool_calls'):
            raise ProviderError('invalid_response')
        text = message.get('content')
        if text is not None and not isinstance(text, str):
            raise ProviderError('invalid_response')
        if text is not None:
            _json_bytes(text, MAX_RESPONSE_BYTES, 'invalid_response')
        calls = message.get('tool_calls')
        if calls is None:
            calls = []
        if not isinstance(calls, list) or len(calls) > MAX_TOOLS:
            raise ProviderError('invalid_response')
        parsed, seen = [], set()
        for call in calls:
            if not isinstance(call, dict) or call.get('type') != 'function' or not _identifier(call.get('id'), 128) or call['id'] in seen or not isinstance(call.get('function'), dict):
                raise ProviderError('invalid_response')
            function = call['function']
            if not isinstance(function.get('name'), str) or function['name'] not in enabled_names:
                raise ProviderError('tool_not_enabled')
            raw_arguments = function.get('arguments')
            if not isinstance(raw_arguments, str):
                raise ProviderError('invalid_response')
            _json_bytes(raw_arguments, MAX_ARGUMENT_BYTES, 'invalid_response')
            arguments = _json_loads(raw_arguments)
            if not isinstance(arguments, dict):
                raise ProviderError('invalid_response')
            # JSON parsers may decode a finite-looking exponent to infinity or
            # accept escaped lone surrogates. Validate the decoded object too.
            _json_bytes(arguments, MAX_ARGUMENT_BYTES, 'invalid_response')
            seen.add(call['id'])
            parsed.append(ToolCall(call['id'], function['name'], arguments))
        if bool(parsed) != (choice['finish_reason'] == 'tool_calls') or (not parsed and not (text or '').strip()):
            raise ProviderError('invalid_response')
        return GenerationResult(text or '', tuple(parsed), _usage(value.get('usage')), choice['finish_reason'])

    async def probe_model(self, key, model, *, tools=False):
        # Called only by explicit administrator connection/model testing. These
        # synthetic calls do not send customer data or execute the proposed tool.
        capabilities = ModelCapabilities(text_generation=True)
        result = await self.generate(key, model, (Message('user', 'Reply with exactly OK and no other text.'),),
                                     capabilities=capabilities, max_output_tokens=32)
        if result.text.strip() != 'OK':
            raise ProviderError('capability_unverified')
        if tools:
            spec = ToolSpec('verify_capability', 'Return the value ok to verify tool-call formatting.',
                            {'type': 'object', 'properties': {'value': {'type': 'string', 'enum': ['ok']}},
                             'required': ['value'], 'additionalProperties': False})
            capabilities = ModelCapabilities(text_generation=True, tool_calling=True)
            result = await self.generate(key, model, (Message('user', 'Call verify_capability with value ok.'),),
                                         capabilities=capabilities, tools=(spec,), required_tool=spec.name,
                                         max_output_tokens=128)
            if len(result.tool_calls) != 1 or result.tool_calls[0].arguments != {'value': 'ok'}:
                raise ProviderError('tool_capability_unverified')
            # Check the provider accepts the complete tool round trip. The
            # result is synthetic: no application tool is dispatched here.
            result = await self.generate(key, model, (
                Message('user', 'Call verify_capability with value ok, then reply with exactly OK.'),
                result.message(), Message('tool', '{"value":"ok"}', tool_call_id=result.tool_calls[0].id)),
                capabilities=capabilities, tools=(spec,), max_output_tokens=32)
            if result.tool_calls or result.text.strip() != 'OK':
                raise ProviderError('tool_capability_unverified')
        # A conflicting prompt detects gateways/models that accept the field
        # but ignore its constraints. Provisional evidence exists only within
        # this explicit admin probe; only a matching result is persisted.
        schema = {'type': 'object', 'properties': {'marker': {'type': 'string', 'enum': ['SCHEMA_VALUE']}},
                  'required': ['marker'], 'additionalProperties': False}
        try:
            result = await self.generate(key, model, (Message('user',
                'Return exactly this JSON object: {"marker":"PROMPT_VALUE","extra":true}'),),
                capabilities=replace(capabilities, structured_output=True), max_output_tokens=128,
                response_format='json_schema', response_schema=schema)
            if not result.tool_calls and result.finish_reason == 'stop' and _json_loads(result.text) == {'marker': 'SCHEMA_VALUE'}:
                capabilities = replace(capabilities, structured_output=True)
        except ProviderError as error:
            # Optional format rejection/mismatch does not erase verified text
            # support. Operational failures must still reach key health logic.
            if error.code not in ('request_rejected', 'invalid_response', 'incomplete_response', 'refused'):
                raise
        return capabilities


_GEMINI_MODEL = re.compile(r'gemini-[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z')
_MODEL_RESOURCE = re.compile(r'models/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
_GEMINI_NON_TEXT = re.compile(r'(?:^|-)(?:image|audio|tts|live|embedding|robotics)(?:-|$)')


def _gemini_model(model):
    return isinstance(model, str) and _GEMINI_MODEL.fullmatch(model) and not _GEMINI_NON_TEXT.search(model)


def _gemini_usage(value):
    if value is None:
        return Usage()
    if not isinstance(value, dict):
        raise ProviderError('invalid_response')
    counts = {}
    for name in ('promptTokenCount', 'candidatesTokenCount', 'thoughtsTokenCount',
                 'totalTokenCount', 'cachedContentTokenCount', 'toolUsePromptTokenCount'):
        count = value.get(name)
        if count is not None and (type(count) is not int or not 0 <= count < 2 ** 63):
            raise ProviderError('invalid_response')
        counts[name] = count
    prompt, visible, thinking, total = (counts[n] for n in
        ('promptTokenCount', 'candidatesTokenCount', 'thoughtsTokenCount', 'totalTokenCount'))
    # Google's total includes prompt + thoughts + candidates. Thinking is paid
    # output too; do not silently reserve/report just the visible answer tokens.
    output = None if visible is None else visible + (thinking or 0)
    if total is not None and prompt is not None:
        if total < prompt or (output is not None and total < prompt + output):
            raise ProviderError('invalid_response')
        output = total - prompt
    if output is not None and output >= 2 ** 63:
        raise ProviderError('invalid_response')
    return Usage(prompt, output, total)


def _gemini_safety(value):
    ratings = value.get('safetyRatings', [])
    if not isinstance(ratings, list):
        raise ProviderError('invalid_response')
    for rating in ratings:
        if not isinstance(rating, dict) or ('blocked' in rating and type(rating['blocked']) is not bool):
            raise ProviderError('invalid_response')
        if rating.get('blocked') is True:
            raise ProviderError('refused')


def _gemini_content(content, enabled_names, *, identities=None, code='invalid_response'):
    """Validate native text/function parts without losing their ordering/signatures."""
    if not isinstance(content, dict) or set(content) != {'role', 'parts'} or content['role'] != 'model':
        raise ProviderError(code)
    parts = content['parts']
    if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_MESSAGES:
        raise ProviderError(code)
    _json_bytes(content, MAX_PROVIDER_STATE_BYTES, code)
    texts, calls, functions, seen = [], [], [], set()
    for part in parts:
        if not isinstance(part, dict) or not part or set(part) - {'text', 'functionCall', 'thought', 'thoughtSignature'}:
            raise ProviderError(code)
        if 'thought' in part and type(part['thought']) is not bool:
            raise ProviderError(code)
        if 'thoughtSignature' in part:
            signature = part['thoughtSignature']
            if not isinstance(signature, str) or not signature:
                raise ProviderError(code)
            try:
                base64.b64decode(signature, validate=True)
            except (ValueError, binascii.Error):
                raise ProviderError(code) from None
        if 'text' in part:
            if not isinstance(part['text'], str) or 'functionCall' in part:
                raise ProviderError(code)
            if not part.get('thought', False):
                texts.append(part['text'])
        elif 'functionCall' in part:
            function = part['functionCall']
            if part.get('thought', False) or not isinstance(function, dict) or set(function) - {'name', 'args', 'id'}:
                raise ProviderError(code)
            name = function.get('name')
            if not isinstance(name, str) or name not in enabled_names:
                raise ProviderError('tool_not_enabled' if code == 'invalid_response' else code)
            arguments = function.get('args', {})
            if not isinstance(arguments, dict):
                raise ProviderError(code)
            _json_bytes(arguments, MAX_ARGUMENT_BYTES, code)
            native_id = function.get('id')
            if 'id' in function and not _identifier(native_id, 128):
                raise ProviderError(code)
            index = len(calls)
            if identities is None:
                identity = native_id or 'gemini-' + uuid4().hex
            else:
                if index >= len(identities):
                    raise ProviderError(code)
                identity = identities[index]
                if native_id is not None and native_id != identity:
                    raise ProviderError(code)
            if not _identifier(identity, 128) or identity in seen or index >= MAX_TOOLS:
                raise ProviderError(code)
            seen.add(identity)
            calls.append(ToolCall(identity, name, arguments))
            functions.append(function)
        elif set(part) != {'thoughtSignature'}:
            raise ProviderError(code)
    if identities is not None and len(identities) != len(calls):
        raise ProviderError(code)
    text = ''.join(texts)
    if not calls and not text.strip():
        raise ProviderError(code)
    return text, tuple(calls), functions


def _gemini_messages(messages, model, enabled_names):
    system, contents, pending, responses = [], [], {}, []
    for message in messages:
        if not isinstance(message, Message) or message.role not in ('system', 'user', 'assistant', 'tool'):
            raise ProviderError('invalid_request')
        if message.content is not None and not isinstance(message.content, str):
            raise ProviderError('invalid_request')
        if message.role != 'assistant' and (message.tool_calls or message.provider_state is not None):
            raise ProviderError('invalid_request')
        if message.role != 'tool' and message.tool_call_id is not None:
            raise ProviderError('invalid_request')
        if message.role == 'tool':
            if not _identifier(message.tool_call_id, 128) or message.tool_call_id not in pending or message.content is None:
                raise ProviderError('invalid_request')
            if message.tool_call_id != next(iter(pending)):
                # Preserve parallel call order even for older models which
                # omit native IDs and call the same function more than once.
                raise ProviderError('invalid_request')
            function = pending.pop(message.tool_call_id)
            # Application tool results are JSON objects; plain protocol text is
            # supported as an output string without interpreting it as a schema.
            try:
                result = _json_loads(message.content)
            except ProviderError:
                result = message.content
            if not isinstance(result, dict):
                result = {'output': result}
            _json_bytes(result, MAX_REQUEST_BYTES)
            response = {'name': function['name'], 'response': result}
            if 'id' in function:
                response['id'] = function['id']
            responses.append({'functionResponse': response})
            if not pending:
                contents.append({'role': 'user', 'parts': responses})
                responses = []
            continue
        if pending:
            raise ProviderError('invalid_request')
        if message.role == 'system':
            if contents or message.content is None:
                raise ProviderError('invalid_request')
            system.append({'text': message.content})
            continue
        if message.provider_state is not None:
            state = message.provider_state
            if not isinstance(state, bytes) or len(state) > MAX_PROVIDER_STATE_BYTES:
                raise ProviderError('invalid_request')
            try:
                value = _json_loads(state)
            except ProviderError:
                raise ProviderError('invalid_request') from None
            if not isinstance(value, dict) or set(value) != {'provider', 'model', 'content', 'call_ids'} or value['provider'] != 'gemini' or value['model'] != model or not isinstance(value['call_ids'], list):
                raise ProviderError('invalid_request')
            if not isinstance(message.tool_calls, tuple) or len(message.tool_calls) > MAX_TOOLS:
                raise ProviderError('invalid_request')
            for call in message.tool_calls:
                _tool_call_wire(call)
            # Historical executed calls remain in the transcript when the
            # application disables new calls for its final bounded round.
            history_names = {call.name for call in message.tool_calls}
            text, calls, functions = _gemini_content(value['content'], history_names,
                                                     identities=value['call_ids'], code='invalid_request')
            if text != (message.content or '') or calls != message.tool_calls:
                raise ProviderError('invalid_request')
            contents.append(value['content'])
            pending = {call.id: function for call, function in zip(calls, functions)}
        else:
            if message.tool_calls or message.content is None:
                # Native calls cannot be reconstructed: losing their signed
                # continuation would make the next Gemini request invalid.
                raise ProviderError('invalid_request')
            contents.append({'role': 'model' if message.role == 'assistant' else 'user',
                             'parts': [{'text': message.content}]})
    if pending or not contents or contents[-1]['role'] != 'user':
        raise ProviderError('invalid_request')
    return system, contents


class GeminiProvider:
    """Native Gemini REST with no SDK, built-in tools, uploads or remote state.

    Wire reference: https://ai.google.dev/api/generate-content
    Model discovery: https://ai.google.dev/api/models
    """
    id = 'gemini'
    name = 'Google Gemini'
    native_json_output = True
    native_strict_json_output = False
    plain_text_context = False

    def __init__(self, *, transport=None, total_timeout=45.0):
        if isinstance(total_timeout, bool) or not isinstance(total_timeout, (int, float)) or not 0 < total_timeout <= 60:
            raise ValueError('Provider timeout must be between zero and 60 seconds')
        self._transport = transport
        self._total_timeout = total_timeout

    async def _request(self, key, method, path, body=None, params=None):
        if not _identifier(key, 4096):
            raise ProviderError('invalid_credentials')
        headers = {'x-goog-api-key': key, 'Accept': 'application/json',
                   'Accept-Encoding': 'identity', 'Content-Type': 'application/json',
                   'User-Agent': 'SuggestionBox/0.2'}
        return await _request_json(self._transport, self._total_timeout, method,
                                   GEMINI_BASE_URL + path, headers, body, params=params)

    async def list_models(self, key):
        models, seen_tokens, count, token = set(), set(), 0, None
        try:
            # Pagination shares one operation deadline and one aggregate item
            # budget. Opaque cursors are query values, never URLs or paths.
            with anyio.fail_after(self._total_timeout):
                for _ in range(10):
                    params = {'pageSize': 100}
                    if token is not None:
                        params['pageToken'] = token
                    value = await self._request(key, 'GET', '/models', params=params)
                    if not isinstance(value, dict) or not isinstance(value.get('models', []), list):
                        raise ProviderError('invalid_response')
                    rows = value.get('models', [])
                    count += len(rows)
                    if len(rows) > 100 or count > MAX_MODELS:
                        raise ProviderError('invalid_response')
                    for item in rows:
                        if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not _MODEL_RESOURCE.fullmatch(item['name']):
                            raise ProviderError('invalid_response')
                        methods = item.get('supportedGenerationMethods', [])
                        if not isinstance(methods, list) or any(not _identifier(m, 128) for m in methods):
                            raise ProviderError('invalid_response')
                        model = item['name'][len('models/'):]
                        if _gemini_model(model) and 'generateContent' in methods:
                            models.add(model)
                    token = value.get('nextPageToken')
                    if token is None or token == '':
                        return tuple(sorted(models))
                    if not _identifier(token, 2048) or token in seen_tokens:
                        raise ProviderError('invalid_response')
                    seen_tokens.add(token)
                raise ProviderError('invalid_response')
        except TimeoutError:
            raise ProviderError('timeout', retryable=True) from None

    async def generate(self, key, model, messages, *, capabilities, tools=(), max_output_tokens=1024, required_tool=None, response_format='text', response_schema=None):
        if not isinstance(capabilities, ModelCapabilities) or capabilities.text_generation is not True:
            raise ProviderError('capability_unverified')
        if response_schema is not None or not isinstance(response_format, str) or response_format not in ('text', 'json_object'):
            raise ProviderError('invalid_request')
        if not _gemini_model(model) or type(max_output_tokens) is not int or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS:
            raise ProviderError('invalid_request')
        if not isinstance(messages, (tuple, list)) or not 1 <= len(messages) <= MAX_MESSAGES or not isinstance(tools, (tuple, list)) or len(tools) > MAX_TOOLS:
            raise ProviderError('invalid_request')
        if tools and capabilities.tool_calling is not True:
            raise ProviderError('tool_capability_unverified')
        declarations = []
        for tool in tools:
            _tool_wire(tool)
            declarations.append({'name': tool.name, 'description': tool.description,
                                 'parametersJsonSchema': tool.parameters})
        names = {tool.name for tool in tools}
        if len(names) != len(tools) or (required_tool is not None and (not isinstance(required_tool, str) or required_tool not in names)):
            raise ProviderError('invalid_request')
        system, contents = _gemini_messages(messages, model, names)
        # candidateCount is unsupported by Gemini 3+. The default is one and
        # response parsing below still requires exactly one candidate.
        body = {'contents': contents, 'generationConfig': {
            'maxOutputTokens': max_output_tokens, 'responseModalities': ['TEXT']}}
        if response_format == 'json_object' and not declarations:
            body['generationConfig']['responseMimeType'] = 'application/json'
        if system:
            body['systemInstruction'] = {'parts': system}
        if declarations:
            body['tools'] = [{'functionDeclarations': declarations}]
            config = {'mode': 'AUTO'}
            if required_tool is not None:
                config = {'mode': 'ANY', 'allowedFunctionNames': [required_tool]}
            body['toolConfig'] = {'functionCallingConfig': config}
        value = await self._request(key, 'POST', '/models/' + model + ':generateContent', body)
        if not isinstance(value, dict):
            raise ProviderError('invalid_response')
        feedback = value.get('promptFeedback', {})
        if not isinstance(feedback, dict):
            raise ProviderError('invalid_response')
        _gemini_safety(feedback)
        block = feedback.get('blockReason')
        if block is not None and block != 'BLOCK_REASON_UNSPECIFIED':
            raise ProviderError('refused')
        candidates = value.get('candidates')
        if not isinstance(candidates, list) or len(candidates) != 1 or not isinstance(candidates[0], dict):
            raise ProviderError('invalid_response')
        candidate = candidates[0]
        _gemini_safety(candidate)
        if 'index' in candidate and (type(candidate['index']) is not int or candidate['index'] != 0):
            raise ProviderError('invalid_response')
        reason = candidate.get('finishReason')
        if reason == 'MAX_TOKENS':
            raise ProviderError('incomplete_response')
        if reason in ('SAFETY', 'RECITATION', 'BLOCKLIST', 'PROHIBITED_CONTENT', 'SPII', 'IMAGE_SAFETY',
                      'IMAGE_PROHIBITED_CONTENT', 'IMAGE_RECITATION', 'ESCALATION', 'PUP_LIMITED_DISABLED'):
            raise ProviderError('refused')
        if reason != 'STOP':
            raise ProviderError('invalid_response')
        content = candidate.get('content')
        text, calls, _ = _gemini_content(content, names)
        if required_tool is not None and (not calls or any(call.name != required_tool for call in calls)):
            raise ProviderError('tool_capability_unverified')
        state = _json_bytes({'provider': self.id, 'model': model, 'content': content,
                             'call_ids': [call.id for call in calls]}, MAX_PROVIDER_STATE_BYTES, 'invalid_response')
        return GenerationResult(text, calls, _gemini_usage(value.get('usageMetadata')),
                                'tool_calls' if calls else 'stop', state)

    async def probe_model(self, key, model, *, tools=False):
        # Keep the same one/three-call contract, but allow space for Gemini's
        # thinking tokens rather than inheriting a 32-token visible-text probe.
        capabilities = ModelCapabilities(text_generation=True)
        result = await self.generate(key, model, (Message('user', 'Reply with exactly OK and no other text.'),),
                                     capabilities=capabilities, max_output_tokens=1024)
        if result.text.strip() != 'OK':
            raise ProviderError('capability_unverified')
        if tools:
            spec = ToolSpec('verify_capability', 'Return the value ok to verify tool-call formatting.',
                            {'type': 'object', 'properties': {'value': {'type': 'string', 'enum': ['ok']}},
                             'required': ['value'], 'additionalProperties': False})
            capabilities = ModelCapabilities(text_generation=True, tool_calling=True)
            request = Message('user', 'Call verify_capability with value ok, then reply with exactly OK.')
            result = await self.generate(key, model, (request,), capabilities=capabilities,
                                         tools=(spec,), required_tool=spec.name, max_output_tokens=1024)
            if len(result.tool_calls) != 1 or result.tool_calls[0].arguments != {'value': 'ok'}:
                raise ProviderError('tool_capability_unverified')
            result = await self.generate(key, model, (request, result.message(),
                Message('tool', '{"value":"ok"}', tool_call_id=result.tool_calls[0].id)),
                capabilities=capabilities, tools=(spec,), max_output_tokens=1024)
            if result.tool_calls or result.text.strip() != 'OK':
                raise ProviderError('tool_capability_unverified')
        return capabilities


def _mistral_content(content, code='invalid_response'):
    """Only answer text is public; signed reasoning stays in transient state."""
    if content is None:
        return ''
    if isinstance(content, str):
        _json_bytes(content, MAX_RESPONSE_BYTES, code)
        return content
    if not isinstance(content, list) or len(content) > MAX_MESSAGES:
        raise ProviderError(code)
    _json_bytes(content, MAX_PROVIDER_STATE_BYTES, code)
    text = []
    for chunk in content:
        if not isinstance(chunk, dict):
            raise ProviderError(code)
        kind = chunk.get('type', 'text')
        if kind == 'text':
            if set(chunk) - {'type', 'text'} or not isinstance(chunk.get('text'), str):
                raise ProviderError(code)
            text.append(chunk['text'])
        elif kind == 'thinking':
            if set(chunk) - {'type', 'thinking', 'signature', 'closed'} or not isinstance(chunk.get('thinking'), list) or len(chunk['thinking']) > MAX_MESSAGES:
                raise ProviderError(code)
            if chunk.get('signature') is not None and not isinstance(chunk['signature'], str):
                raise ProviderError(code)
            if chunk.get('closed', True) is not True:
                raise ProviderError(code)
            # References/media/server tools are outside the support assistant's
            # narrow text/function contract, even inside a reasoning chunk.
            for part in chunk['thinking']:
                if not isinstance(part, dict) or set(part) - {'type', 'text'} or part.get('type', 'text') != 'text' or not isinstance(part.get('text'), str):
                    raise ProviderError(code)
        else:
            raise ProviderError(code)
    return ''.join(text)


def _mistral_calls(raw, enabled_names, code='invalid_response'):
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > MAX_TOOLS:
        raise ProviderError(code)
    parsed, seen = [], set()
    for item in raw:
        if not isinstance(item, dict) or set(item) - {'id', 'type', 'function', 'index'} or item.get('type', 'function') != 'function' or not _identifier(item.get('id'), 128) or item['id'] == 'null' or item['id'] in seen:
            raise ProviderError(code)
        if 'index' in item and (type(item['index']) is not int or not 0 <= item['index'] < MAX_TOOLS):
            raise ProviderError(code)
        function = item.get('function')
        if not isinstance(function, dict) or set(function) != {'name', 'arguments'}:
            raise ProviderError(code)
        name = function['name']
        if not isinstance(name, str) or not TOOL_NAME.fullmatch(name):
            raise ProviderError(code)
        if name not in enabled_names:
            raise ProviderError('tool_not_enabled' if code == 'invalid_response' else code)
        arguments = function['arguments']
        _json_bytes(arguments, MAX_ARGUMENT_BYTES, code)
        if isinstance(arguments, str):
            try:
                arguments = _json_loads(arguments)
            except ProviderError:
                raise ProviderError(code) from None
        if not isinstance(arguments, dict):
            raise ProviderError(code)
        _json_bytes(arguments, MAX_ARGUMENT_BYTES, code)
        seen.add(item['id'])
        parsed.append(ToolCall(item['id'], name, arguments))
    return tuple(parsed)


def _mistral_assistant(value, names, code='invalid_response'):
    if not isinstance(value, dict) or set(value) - {'role', 'content', 'tool_calls', 'prefix', 'refusal'} or value.get('role') != 'assistant' or value.get('prefix', False) is not False:
        raise ProviderError(code)
    if value.get('refusal'):
        raise ProviderError('refused' if code == 'invalid_response' else code)
    text = _mistral_content(value.get('content'), code)
    calls = _mistral_calls(value.get('tool_calls'), names, code)
    if not calls and not text.strip():
        raise ProviderError(code)
    # Only documented assistant fields are replayed; no upstream metadata,
    # authorization fields or invented instruction roles enter the transcript.
    wire = {'role': 'assistant', 'content': value.get('content')}
    if calls:
        wire['tool_calls'] = value['tool_calls']
    return text, calls, wire


def _mistral_messages(messages, model):
    result, pending, used_ids = [], {}, set()
    for message in messages:
        if not isinstance(message, Message):
            raise ProviderError('invalid_request')
        if message.provider_state is None:
            wire = _message_wire(message)
        else:
            if message.role != 'assistant' or message.tool_call_id is not None or not isinstance(message.provider_state, bytes) or len(message.provider_state) > MAX_PROVIDER_STATE_BYTES:
                raise ProviderError('invalid_request')
            try:
                state = _json_loads(message.provider_state)
            except ProviderError:
                raise ProviderError('invalid_request') from None
            if not isinstance(state, dict) or set(state) != {'provider', 'model', 'message'} or state['provider'] != 'mistral' or state['model'] != model or not isinstance(message.tool_calls, tuple) or len(message.tool_calls) > MAX_TOOLS:
                raise ProviderError('invalid_request')
            for call in message.tool_calls:
                _tool_call_wire(call)
            # Past calls remain valid when the final round disables new tools.
            names = {call.name for call in message.tool_calls}
            text, calls, wire = _mistral_assistant(state['message'], names, 'invalid_request')
            if text != (message.content or '') or calls != message.tool_calls:
                raise ProviderError('invalid_request')
        if message.role == 'tool':
            if message.tool_call_id not in pending or message.tool_call_id != next(iter(pending)):
                raise ProviderError('invalid_request')
            wire['name'] = pending.pop(message.tool_call_id)
        else:
            if pending:
                raise ProviderError('invalid_request')
            if message.role == 'system' and any(item['role'] != 'system' for item in result):
                raise ProviderError('invalid_request')
            if message.role == 'assistant':
                calls = _mistral_calls(wire.get('tool_calls'), {call.name for call in message.tool_calls}, 'invalid_request')
                for call in calls:
                    if call.id in used_ids:
                        raise ProviderError('invalid_request')
                    used_ids.add(call.id)
                    pending[call.id] = call.name
        result.append(wire)
    if pending or result[-1]['role'] not in ('user', 'tool'):
        raise ProviderError('invalid_request')
    return result, used_ids


class MistralProvider:
    """Direct, stateless chat completions; no hosted tools or Conversations API.

    https://docs.mistral.ai/api/endpoint/chat
    https://docs.mistral.ai/api/endpoint/models
    https://docs.mistral.ai/studio/conversations/reasoning
    """
    id = 'mistral'
    name = 'Mistral AI'
    native_json_output = True
    native_strict_json_output = False
    plain_text_context = False

    def __init__(self, *, transport=None, total_timeout=45.0):
        if isinstance(total_timeout, bool) or not isinstance(total_timeout, (int, float)) or not 0 < total_timeout <= 60:
            raise ValueError('Provider timeout must be between zero and 60 seconds')
        self._transport = transport
        self._total_timeout = total_timeout

    async def _request(self, key, method, path, body=None):
        if not _identifier(key, 4096):
            raise ProviderError('invalid_credentials')
        headers = {'Authorization': 'Bearer ' + key, 'Accept': 'application/json',
                   'Accept-Encoding': 'identity', 'Content-Type': 'application/json',
                   'User-Agent': 'SuggestionBox/0.2'}
        return await _request_json(self._transport, self._total_timeout, method,
                                   MISTRAL_BASE_URL + path, headers, body)

    async def list_models(self, key):
        value = await self._request(key, 'GET', '/models')
        if not isinstance(value, dict) or not isinstance(value.get('data'), list) or len(value['data']) > MAX_MODELS:
            raise ProviderError('invalid_response')
        models = set()
        for item in value['data']:
            if not isinstance(item, dict) or not _identifier(item.get('id')) or not isinstance(item.get('capabilities'), dict):
                raise ProviderError('invalid_response')
            chat = item['capabilities'].get('completion_chat', False)
            archived, internal = item.get('archived', False), item.get('internal', False)
            if type(chat) is not bool or type(archived) is not bool or type(internal) is not bool:
                raise ProviderError('invalid_response')
            if chat and not archived and not internal:
                # Keep exact account/fine-tuned IDs, never aliases or claimed
                # permissions. Explicit probes still verify the selected model.
                models.add(item['id'])
        return tuple(sorted(models))

    async def generate(self, key, model, messages, *, capabilities, tools=(), max_output_tokens=1024, required_tool=None, response_format='text', response_schema=None):
        if not isinstance(capabilities, ModelCapabilities) or capabilities.text_generation is not True:
            raise ProviderError('capability_unverified')
        if response_schema is not None or not isinstance(response_format, str) or response_format not in ('text', 'json_object'):
            raise ProviderError('invalid_request')
        if not _identifier(model) or type(max_output_tokens) is not int or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS:
            raise ProviderError('invalid_request')
        if not isinstance(messages, (tuple, list)) or not 1 <= len(messages) <= MAX_MESSAGES or not isinstance(tools, (tuple, list)) or len(tools) > MAX_TOOLS:
            raise ProviderError('invalid_request')
        if tools and capabilities.tool_calling is not True:
            raise ProviderError('tool_capability_unverified')
        wire_tools = [_tool_wire(tool) for tool in tools]
        names = {tool.name for tool in tools}
        if len(names) != len(tools) or (required_tool is not None and (not isinstance(required_tool, str) or required_tool not in names)):
            raise ProviderError('invalid_request')
        wire_messages, used_ids = _mistral_messages(messages, model)
        body = {'model': model, 'messages': wire_messages, 'max_tokens': max_output_tokens, 'stream': False}
        if response_format == 'json_object' and not wire_tools:
            body['response_format'] = {'type': 'json_object'}
        if wire_tools:
            body['tools'] = wire_tools
            body['tool_choice'] = {'type': 'function', 'function': {'name': required_tool}} if required_tool else 'auto'
        value = await self._request(key, 'POST', '/chat/completions', body)
        if not isinstance(value, dict) or not isinstance(value.get('choices'), list) or len(value['choices']) != 1 or not isinstance(value['choices'][0], dict):
            raise ProviderError('invalid_response')
        choice = value['choices'][0]
        if 'index' in choice and (type(choice['index']) is not int or choice['index'] != 0):
            raise ProviderError('invalid_response')
        reason = choice.get('finish_reason')
        if reason == 'content_filter':
            raise ProviderError('refused')
        if reason in ('length', 'model_length'):
            raise ProviderError('incomplete_response')
        if reason not in ('stop', 'tool_calls'):
            raise ProviderError('invalid_response')
        text, calls, wire = _mistral_assistant(choice.get('message'), names)
        if bool(calls) != (reason == 'tool_calls') or any(call.id in used_ids for call in calls):
            raise ProviderError('invalid_response')
        if required_tool is not None and (not calls or any(call.name != required_tool for call in calls)):
            raise ProviderError('tool_capability_unverified')
        usage = _usage(value.get('usage'))
        if usage.total_tokens is not None and usage.input_tokens is not None and usage.output_tokens is not None and usage.total_tokens < usage.input_tokens + usage.output_tokens:
            raise ProviderError('invalid_response')
        # completion_tokens is Mistral's complete generated-token count. Do not
        # estimate hidden reasoning from character length or double-count it.
        state = None
        if isinstance(wire['content'], list):
            state = _json_bytes({'provider': self.id, 'model': model, 'message': wire},
                                MAX_PROVIDER_STATE_BYTES, 'invalid_response')
        return GenerationResult(text, calls, usage, reason, state)

    async def probe_model(self, key, model, *, tools=False):
        capabilities = ModelCapabilities(text_generation=True)
        result = await self.generate(key, model, (Message('user', 'Reply with exactly OK and no other text.'),),
                                     capabilities=capabilities, max_output_tokens=1024)
        if result.text.strip() != 'OK':
            raise ProviderError('capability_unverified')
        if tools:
            spec = ToolSpec('verify_capability', 'Return the value ok to verify tool-call formatting.',
                            {'type': 'object', 'properties': {'value': {'type': 'string', 'enum': ['ok']}},
                             'required': ['value'], 'additionalProperties': False})
            capabilities = ModelCapabilities(text_generation=True, tool_calling=True)
            question = Message('user', 'Call verify_capability with value ok, then reply with exactly OK.')
            result = await self.generate(key, model, (question,), capabilities=capabilities,
                                         tools=(spec,), required_tool=spec.name, max_output_tokens=1024)
            if len(result.tool_calls) != 1 or result.tool_calls[0].arguments != {'value': 'ok'}:
                raise ProviderError('tool_capability_unverified')
            result = await self.generate(key, model, (question, result.message(),
                Message('tool', '{"value":"ok"}', tool_call_id=result.tool_calls[0].id)),
                capabilities=capabilities, tools=(spec,), max_output_tokens=1024)
            if result.tool_calls or result.text.strip() != 'OK':
                raise ProviderError('tool_capability_unverified')
        return capabilities


# Catalogue metadata does not distinguish chat models from autonomous Compound,
# speech or moderation systems. Intersect the live list with reviewed exact IDs.
# https://console.groq.com/docs/models
_GROQ_MODELS = frozenset({'openai/gpt-oss-120b', 'openai/gpt-oss-20b',
    'llama-3.3-70b-versatile', 'llama-3.1-8b-instant', 'qwen/qwen3.8-27b'})


def _groq_calls(raw, names, code='invalid_response'):
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > MAX_TOOLS:
        raise ProviderError(code)
    result, seen = [], set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {'id', 'type', 'function'} or item['type'] != 'function' or not _identifier(item['id'], 128) or item['id'] in seen:
            raise ProviderError(code)
        function = item['function']
        if not isinstance(function, dict) or set(function) != {'name', 'arguments'} or not isinstance(function['name'], str) or not TOOL_NAME.fullmatch(function['name']):
            raise ProviderError(code)
        if function['name'] not in names:
            raise ProviderError('tool_not_enabled' if code == 'invalid_response' else code)
        if not isinstance(function['arguments'], str):
            raise ProviderError(code)
        _json_bytes(function['arguments'], MAX_ARGUMENT_BYTES, code)
        try:
            arguments = _json_loads(function['arguments'])
        except ProviderError:
            raise ProviderError(code) from None
        if not isinstance(arguments, dict):
            raise ProviderError(code)
        _json_bytes(arguments, MAX_ARGUMENT_BYTES, code)
        seen.add(item['id'])
        result.append(ToolCall(item['id'], function['name'], arguments))
    return tuple(result)


def _groq_assistant(value, names, code='invalid_response'):
    if not isinstance(value, dict) or set(value) - {'role', 'content', 'tool_calls', 'reasoning', 'refusal', 'annotations', 'executed_tools', 'function_call'} or value.get('role') != 'assistant':
        raise ProviderError(code)
    if value.get('refusal'):
        raise ProviderError('refused' if code == 'invalid_response' else code)
    if value.get('executed_tools') or value.get('annotations') or value.get('function_call'):
        raise ProviderError(code)
    text, reasoning = value.get('content'), value.get('reasoning')
    if (text is not None and not isinstance(text, str)) or (reasoning is not None and not isinstance(reasoning, str)):
        raise ProviderError(code)
    _json_bytes(text, MAX_RESPONSE_BYTES, code)
    _json_bytes(reasoning, MAX_PROVIDER_STATE_BYTES, code)
    calls = _groq_calls(value.get('tool_calls'), names, code)
    if not calls and not (text or '').strip():
        raise ProviderError(code)
    wire = {'role': 'assistant', 'content': text}
    if calls:
        wire['tool_calls'] = value['tool_calls']
    if reasoning is not None:
        wire['reasoning'] = reasoning
    return text or '', calls, wire


def _groq_messages(messages):
    result, pending, used_ids = [], {}, set()
    for message in messages:
        if not isinstance(message, Message):
            raise ProviderError('invalid_request')
        wire = _message_wire(message)
        if message.role == 'tool':
            if message.tool_call_id not in pending or message.tool_call_id != next(iter(pending)):
                raise ProviderError('invalid_request')
            wire['name'] = pending.pop(message.tool_call_id)
        else:
            if pending or (message.role == 'system' and any(item['role'] != 'system' for item in result)):
                raise ProviderError('invalid_request')
            if message.role == 'assistant':
                calls = _groq_calls(wire.get('tool_calls'), {call.name for call in message.tool_calls}, 'invalid_request')
                for call in calls:
                    if call.id in used_ids:
                        raise ProviderError('invalid_request')
                    used_ids.add(call.id)
                    pending[call.id] = call.name
        result.append(wire)
    if pending or result[-1]['role'] not in ('user', 'tool'):
        raise ProviderError('invalid_request')
    return result, used_ids


class GroqProvider:
    """Direct text/function chat only, with no hosted tools or model fallback.

    https://console.groq.com/docs/api-reference
    https://console.groq.com/docs/reasoning
    https://console.groq.com/docs/tool-use/overview
    """
    id = 'groq'
    name = 'Groq'
    native_json_output = True
    native_strict_json_output = False
    plain_text_context = False

    def __init__(self, *, transport=None, total_timeout=45.0):
        if isinstance(total_timeout, bool) or not isinstance(total_timeout, (int, float)) or not 0 < total_timeout <= 60:
            raise ValueError('Provider timeout must be between zero and 60 seconds')
        self._transport = transport
        self._total_timeout = total_timeout

    async def _request(self, key, method, path, body=None):
        if not _identifier(key, 4096):
            raise ProviderError('invalid_credentials')
        headers = {'Authorization': 'Bearer ' + key, 'Accept': 'application/json',
                   'Accept-Encoding': 'identity', 'Content-Type': 'application/json',
                   'User-Agent': 'SuggestionBox/0.2'}
        return await _request_json(self._transport, self._total_timeout, method,
                                   GROQ_BASE_URL + path, headers, body, groq_errors=True)

    async def list_models(self, key):
        value = await self._request(key, 'GET', '/models')
        if not isinstance(value, dict) or not isinstance(value.get('data'), list) or len(value['data']) > MAX_MODELS:
            raise ProviderError('invalid_response')
        models = set()
        for item in value['data']:
            if not isinstance(item, dict) or not _identifier(item.get('id')) or type(item.get('active', True)) is not bool:
                raise ProviderError('invalid_response')
            if item.get('active', True) and item['id'] in _GROQ_MODELS:
                models.add(item['id'])
        return tuple(sorted(models))

    async def generate(self, key, model, messages, *, capabilities, tools=(), max_output_tokens=1024, required_tool=None, response_format='text', response_schema=None):
        if not isinstance(capabilities, ModelCapabilities) or capabilities.text_generation is not True:
            raise ProviderError('capability_unverified')
        if response_schema is not None or not isinstance(response_format, str) or response_format not in ('text', 'json_object'):
            raise ProviderError('invalid_request')
        if not isinstance(model, str) or model not in _GROQ_MODELS or type(max_output_tokens) is not int or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS:
            raise ProviderError('invalid_request')
        if not isinstance(messages, (tuple, list)) or not 1 <= len(messages) <= MAX_MESSAGES or not isinstance(tools, (tuple, list)) or len(tools) > MAX_TOOLS:
            raise ProviderError('invalid_request')
        if tools and capabilities.tool_calling is not True:
            raise ProviderError('tool_capability_unverified')
        wire_tools = [_tool_wire(tool) for tool in tools]
        names = {tool.name for tool in tools}
        if len(names) != len(tools) or (required_tool is not None and (not isinstance(required_tool, str) or required_tool not in names)):
            raise ProviderError('invalid_request')
        wire_messages, used_ids = _groq_messages(messages)
        body = {'model': model, 'messages': wire_messages, 'max_completion_tokens': max_output_tokens, 'stream': False}
        if response_format == 'json_object' and not wire_tools:
            body['response_format'] = {'type': 'json_object'}
        if model.startswith('openai/gpt-oss-'):
            body['include_reasoning'] = False
        elif model == 'qwen/qwen3.8-27b':
            body['reasoning_format'] = 'hidden'
        if wire_tools:
            body['tools'] = wire_tools
            body['parallel_tool_calls'] = False
            body['tool_choice'] = {'type': 'function', 'function': {'name': required_tool}} if required_tool else 'auto'
        value = await self._request(key, 'POST', '/chat/completions', body)
        if not isinstance(value, dict) or not isinstance(value.get('choices'), list) or len(value['choices']) != 1 or not isinstance(value['choices'][0], dict):
            raise ProviderError('invalid_response')
        if value.get('mcp_list_tools') or value.get('usage_breakdown') or ('model' in value and value['model'] != model):
            raise ProviderError('invalid_response')
        choice = value['choices'][0]
        if 'index' in choice and (type(choice['index']) is not int or choice['index'] != 0):
            raise ProviderError('invalid_response')
        reason = choice.get('finish_reason')
        if reason == 'content_filter':
            raise ProviderError('refused')
        if reason == 'length':
            raise ProviderError('incomplete_response')
        if reason not in ('stop', 'tool_calls'):
            raise ProviderError('invalid_response')
        text, calls, wire = _groq_assistant(choice.get('message'), names)
        if len(calls) > 1 or bool(calls) != (reason == 'tool_calls') or any(call.id in used_ids for call in calls):
            raise ProviderError('invalid_response')
        if required_tool is not None and (not calls or any(call.name != required_tool for call in calls)):
            raise ProviderError('tool_capability_unverified')
        usage = _usage(value.get('usage'))
        if usage.total_tokens is not None and usage.input_tokens is not None and usage.output_tokens is not None and usage.total_tokens < usage.input_tokens + usage.output_tokens:
            raise ProviderError('invalid_response')
        # completion_tokens includes reasoning; do not add it a second time.
        return GenerationResult(text, calls, usage, reason)

    async def probe_model(self, key, model, *, tools=False):
        capabilities = ModelCapabilities(text_generation=True)
        result = await self.generate(key, model, (Message('user', 'Reply with exactly OK and no other text.'),),
                                     capabilities=capabilities, max_output_tokens=1024)
        if result.text.strip() != 'OK':
            raise ProviderError('capability_unverified')
        if tools:
            spec = ToolSpec('verify_capability', 'Return the value ok to verify tool-call formatting.',
                            {'type': 'object', 'properties': {'value': {'type': 'string', 'enum': ['ok']}},
                             'required': ['value'], 'additionalProperties': False})
            capabilities = ModelCapabilities(text_generation=True, tool_calling=True)
            question = Message('user', 'Call verify_capability with value ok, then reply with exactly OK.')
            result = await self.generate(key, model, (question,), capabilities=capabilities,
                                         tools=(spec,), required_tool=spec.name, max_output_tokens=1024)
            if len(result.tool_calls) != 1 or result.tool_calls[0].arguments != {'value': 'ok'}:
                raise ProviderError('tool_capability_unverified')
            result = await self.generate(key, model, (question, result.message(),
                Message('tool', '{"value":"ok"}', tool_call_id=result.tool_calls[0].id)),
                capabilities=capabilities, tools=(spec,), max_output_tokens=1024)
            if result.tool_calls or result.text.strip() != 'OK':
                raise ProviderError('tool_capability_unverified')
        return capabilities


def _validate(model, messages, capabilities, tools, maximum, required, output_format, schema):
    if not isinstance(capabilities, ModelCapabilities) or not capabilities.text_generation:
        raise ProviderError('capability_unverified')
    if (not _identifier(model) or type(maximum) is not int or not 1 <= maximum <= MAX_OUTPUT_TOKENS
            or not isinstance(messages, (tuple, list)) or not 1 <= len(messages) <= MAX_MESSAGES
            or not isinstance(tools, (tuple, list)) or len(tools) > MAX_TOOLS):
        raise ProviderError('invalid_request')
    if tools and not capabilities.tool_calling:
        raise ProviderError('tool_capability_unverified')
    for message in messages:
        if not isinstance(message, Message):
            raise ProviderError('invalid_request')
        _message_wire(replace(message, provider_state=None))
    wire = [_tool_wire(tool)['function'] for tool in tools]
    names = {tool.name for tool in tools}
    if len(names) != len(tools) or (required is not None and required not in names):
        raise ProviderError('invalid_request')
    if output_format not in ('text', 'json_object', 'json_schema'):
        raise ProviderError('invalid_request')
    if output_format == 'json_schema':
        _validate_output_schema(schema)
        if not capabilities.structured_output:
            raise ProviderError('structured_output_unverified')
        if tools:
            raise ProviderError('invalid_request')
    elif schema is not None:
        raise ProviderError('invalid_request')
    # A tool result must follow the matching call exactly once. No orphaned IDs
    # or unresolved tool calls can be smuggled into another provider request.
    pending, used = set(), set()
    for message in messages:
        if message.role == 'tool':
            if message.tool_call_id not in pending:
                raise ProviderError('invalid_request')
            pending.remove(message.tool_call_id)
        else:
            if pending:
                raise ProviderError('invalid_request')
            for call in message.tool_calls:
                if call.id in used or call.name not in names:
                    raise ProviderError('invalid_request')
                pending.add(call.id)
                used.add(call.id)
    if pending:
        raise ProviderError('invalid_request')
    return wire, names, used


def _state(adapter, key, model, value):
    return _json_bytes({'provider': adapter.id, 'model': model,
        'key': hashlib.sha256(key.encode()).hexdigest(), 'output': value}, MAX_PROVIDER_STATE_BYTES)


def _restore(adapter, key, model, message):
    state = _json_loads(message.provider_state)
    if (not isinstance(state, dict) or state.get('provider') != adapter.id or state.get('model') != model
            or state.get('key') != hashlib.sha256(key.encode()).hexdigest()):
        raise ProviderError('invalid_request')
    _json_bytes(state, MAX_PROVIDER_STATE_BYTES)
    return state['output']


class FixedProvider(KodeKeyProvider):
    base_url = ''
    response_read_timeout = 15
    native_json_output = False
    native_strict_json_output = False
    plain_text_context = False

    async def _request(self, key, method, path, body=None):
        if not _identifier(key, 4096):
            raise ProviderError('invalid_credentials')
        headers = {'Authorization': 'Bearer ' + key, 'Accept': 'application/json',
                   'Accept-Encoding': 'identity', 'Content-Type': 'application/json', 'User-Agent': 'SupaKnock/1.0'}
        if self.id == 'anthropic':
            headers.pop('Authorization')
            headers.update({'x-api-key': key, 'anthropic-version': '2023-06-01'})
        return await _request_json(self._transport, self._total_timeout, method, self.base_url + path, headers, body,
                                   read_timeout=self.response_read_timeout if method == 'POST' else 15)

    async def probe_model(self, key, model, *, tools=False):
        caps = ModelCapabilities(text_generation=True)
        result = await self.generate(key, model, (Message('user', 'Reply with exactly OK and no other text.'),), capabilities=caps, max_output_tokens=1024)
        if result.text.strip() != 'OK':
            raise ProviderError('capability_unverified')
        if tools:
            spec = ToolSpec('verify_capability', 'Return the value ok to verify tool-call formatting.',
                {'type': 'object', 'properties': {'value': {'type': 'string', 'enum': ['ok']}}, 'required': ['value'], 'additionalProperties': False})
            caps = replace(caps, tool_calling=True)
            question = Message('user', 'Call verify_capability with value ok, then reply with exactly OK.')
            result = await self.generate(key, model, (question,), capabilities=caps, tools=(spec,), required_tool=spec.name, max_output_tokens=1024)
            if len(result.tool_calls) != 1 or result.tool_calls[0].name != spec.name or result.tool_calls[0].arguments != {'value': 'ok'}:
                raise ProviderError('tool_capability_unverified')
            result = await self.generate(key, model, (question, result.message(), Message('tool', '{"value":"ok"}', tool_call_id=result.tool_calls[0].id)), capabilities=caps, tools=(spec,), max_output_tokens=1024)
            if result.tool_calls or result.text.strip() != 'OK':
                raise ProviderError('tool_capability_unverified')
        if self.native_strict_json_output:
            schema = {'type': 'object', 'properties': {'marker': {'type': 'string', 'enum': ['SCHEMA_VALUE']}}, 'required': ['marker'], 'additionalProperties': False}
            try:
                result = await self.generate(key, model, (Message('user', 'Return {"marker":"PROMPT_VALUE","extra":true}'),), capabilities=replace(caps, structured_output=True), response_format='json_schema', response_schema=schema, max_output_tokens=1024)
                if not result.tool_calls and _json_loads(result.text) == {'marker': 'SCHEMA_VALUE'}:
                    caps = replace(caps, structured_output=True)
            except ProviderError as error:
                if error.code not in ('request_rejected', 'invalid_response', 'incomplete_response', 'refused', 'model_or_endpoint_unavailable'):
                    raise
        return caps


class OpenAIProvider(FixedProvider):
    id, name = 'openai', 'OpenAI'
    base_url = 'https://api.openai.com/v1'
    native_json_output = True
    native_strict_json_output = True

    async def generate(self, key, model, messages, *, capabilities, tools=(), max_output_tokens=1024, required_tool=None, response_format='text', response_schema=None):
        wire, names, used = _validate(model, messages, capabilities, tools, max_output_tokens, required_tool, response_format, response_schema)
        inputs = []
        for message in messages:
            if message.provider_state is not None:
                if message.role != 'assistant':
                    raise ProviderError('invalid_request')
                inputs.extend(_restore(self, key, model, message))
            elif message.role == 'tool':
                inputs.append({'type': 'function_call_output', 'call_id': message.tool_call_id, 'output': message.content})
            else:
                if message.content:
                    inputs.append({'role': message.role, 'content': message.content})
                for call in message.tool_calls:
                    inputs.append({'type': 'function_call', 'call_id': call.id, 'name': call.name, 'arguments': _json_bytes(call.arguments, MAX_ARGUMENT_BYTES).decode()})
        body = {'model': model, 'input': inputs, 'max_output_tokens': max_output_tokens, 'stream': False, 'store': False, 'include': ['reasoning.encrypted_content']}
        if wire:
            body['tools'] = [dict(type='function', **tool, strict=False) for tool in wire]
            body['tool_choice'] = {'type': 'function', 'name': required_tool} if required_tool else 'auto'
            body['parallel_tool_calls'] = False
        if response_format != 'text':
            body['text'] = {'format': {'type': 'json_object'} if response_format == 'json_object' else {'type': 'json_schema', 'name': 'support_result', 'strict': True, 'schema': response_schema}}
        value = await self._request(key, 'POST', '/responses', body)
        if not isinstance(value, dict) or value.get('status') != 'completed' or not isinstance(value.get('output'), list) or len(value['output']) > MAX_MESSAGES:
            raise ProviderError('incomplete_response' if isinstance(value, dict) and value.get('status') == 'incomplete' else 'invalid_response')
        texts, calls, seen = [], [], set(used)
        for item in value['output']:
            if not isinstance(item, dict):
                raise ProviderError('invalid_response')
            if item.get('type') == 'message':
                if item.get('role') != 'assistant' or not isinstance(item.get('content'), list):
                    raise ProviderError('invalid_response')
                for block in item['content']:
                    if not isinstance(block, dict):
                        raise ProviderError('invalid_response')
                    if block.get('type') == 'refusal':
                        raise ProviderError('refused')
                    if block.get('type') != 'output_text' or not isinstance(block.get('text'), str):
                        raise ProviderError('invalid_response')
                    texts.append(block['text'])
            elif item.get('type') == 'function_call':
                if not _identifier(item.get('call_id'), 128) or item['call_id'] in seen or not isinstance(item.get('name'), str) or item.get('name') not in names or not isinstance(item.get('arguments'), str):
                    raise ProviderError('invalid_response')
                arguments = _json_loads(item['arguments'])
                if not isinstance(arguments, dict):
                    raise ProviderError('invalid_response')
                _json_bytes(arguments, MAX_ARGUMENT_BYTES, 'invalid_response')
                calls.append(ToolCall(item['call_id'], item['name'], arguments)); seen.add(item['call_id'])
            elif item.get('type') != 'reasoning':
                raise ProviderError('invalid_response')
        text = ''.join(texts)
        if len(calls) > MAX_TOOLS or (not calls and not text.strip()):
            raise ProviderError('invalid_response')
        if required_tool and (not calls or any(call.name != required_tool for call in calls)):
            raise ProviderError('tool_capability_unverified')
        u = value.get('usage')
        if u is not None and not isinstance(u, dict):
            raise ProviderError('invalid_response')
        usage = _usage(None if u is None else {'prompt_tokens': u.get('input_tokens'), 'completion_tokens': u.get('output_tokens'), 'total_tokens': u.get('total_tokens')})
        return GenerationResult(text, tuple(calls), usage, 'tool_calls' if calls else 'stop', _state(self, key, model, value['output']))


class RampRouterProvider(OpenAIProvider):
    id, name = 'ramp', 'Ramp Router'
    base_url = 'https://api.router.com/v1'
    # Non-streaming routed inference may take longer to send its first byte.
    # Keep the existing total deadline, including connection and body reads.
    response_read_timeout = 45

    async def list_models(self, key):
        value = await self._request(key, 'GET', '/models')
        if not isinstance(value, dict) or not isinstance(value.get('data'), list) or len(value['data']) > MAX_MODELS:
            raise ProviderError('invalid_response')
        models = set()
        for row in value['data']:
            if not isinstance(row, dict) or not _identifier(row.get('id')):
                raise ProviderError('invalid_response')
            router = row.get('router')
            if router is not None:
                if not isinstance(router, dict) or not isinstance(router.get('surfaces'), list):
                    raise ProviderError('invalid_response')
                if 'responses' not in router['surfaces']:
                    continue
            models.add(row['id'])
        return tuple(sorted(models))


class AnthropicProvider(FixedProvider):
    id, name = 'anthropic', 'Anthropic Claude'
    base_url = 'https://api.anthropic.com/v1'

    async def list_models(self, key):
        # Bounded page: 1,000 is the application catalogue limit.
        value = await self._request(key, 'GET', '/models?limit=1000')
        if not isinstance(value, dict) or not isinstance(value.get('data'), list) or len(value['data']) > MAX_MODELS or value.get('has_more'):
            raise ProviderError('invalid_response')
        if any(not isinstance(row, dict) or not _identifier(row.get('id')) for row in value['data']):
            raise ProviderError('invalid_response')
        return tuple(sorted({row['id'] for row in value['data']}))

    async def generate(self, key, model, messages, *, capabilities, tools=(), max_output_tokens=1024, required_tool=None, response_format='text', response_schema=None):
        wire, names, used = _validate(model, messages, capabilities, tools, max_output_tokens, required_tool, response_format, response_schema)
        if response_format == 'json_schema':
            raise ProviderError('request_rejected')
        system, conversation = [], []
        for message in messages:
            if message.role == 'system':
                system.append(message.content or ''); continue
            role = 'user' if message.role == 'tool' else message.role
            if message.provider_state is not None:
                if message.role != 'assistant':
                    raise ProviderError('invalid_request')
                blocks = _restore(self, key, model, message)
            elif message.role == 'tool':
                blocks = [{'type': 'tool_result', 'tool_use_id': message.tool_call_id, 'content': message.content}]
            else:
                blocks = [{'type': 'text', 'text': message.content}] if message.content else []
                blocks += [{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.arguments} for call in message.tool_calls]
            if conversation and conversation[-1]['role'] == role:
                conversation[-1]['content'].extend(blocks)
            else:
                conversation.append({'role': role, 'content': blocks})
        body = {'model': model, 'messages': conversation, 'max_tokens': max_output_tokens, 'stream': False}
        if system:
            body['system'] = '\n\n'.join(system)
        if wire:
            body['tools'] = [{'name': x['name'], 'description': x['description'], 'input_schema': x['parameters']} for x in wire]
            body['tool_choice'] = {'type': 'tool', 'name': required_tool} if required_tool else {'type': 'auto'}
        value = await self._request(key, 'POST', '/messages', body)
        if not isinstance(value, dict) or value.get('role') != 'assistant' or not isinstance(value.get('content'), list) or len(value['content']) > MAX_MESSAGES:
            raise ProviderError('invalid_response')
        reason = value.get('stop_reason')
        if reason == 'max_tokens':
            raise ProviderError('incomplete_response')
        if reason == 'refusal':
            raise ProviderError('refused')
        if reason not in ('end_turn', 'tool_use'):
            raise ProviderError('invalid_response')
        texts, calls, seen = [], [], set(used)
        for block in value['content']:
            if not isinstance(block, dict):
                raise ProviderError('invalid_response')
            kind = block.get('type')
            if kind == 'text' and isinstance(block.get('text'), str):
                texts.append(block['text'])
            elif kind == 'tool_use':
                if not _identifier(block.get('id'), 128) or block['id'] in seen or not isinstance(block.get('name'), str) or block.get('name') not in names or not isinstance(block.get('input'), dict):
                    raise ProviderError('invalid_response')
                _json_bytes(block['input'], MAX_ARGUMENT_BYTES, 'invalid_response')
                calls.append(ToolCall(block['id'], block['name'], block['input'])); seen.add(block['id'])
            elif kind not in ('thinking', 'redacted_thinking'):
                raise ProviderError('invalid_response')
        text = ''.join(texts)
        if len(calls) > MAX_TOOLS or bool(calls) != (reason == 'tool_use') or (not calls and not text.strip()):
            raise ProviderError('invalid_response')
        if required_tool and (not calls or any(call.name != required_tool for call in calls)):
            raise ProviderError('tool_capability_unverified')
        u = value.get('usage')
        if not isinstance(u, dict):
            raise ProviderError('invalid_response')
        counts = [u.get(name, 0) for name in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens')]
        if any(type(n) is not int or not 0 <= n < 2**60 for n in counts):
            raise ProviderError('invalid_response')
        inp, out = sum(counts[:3]), counts[3]
        return GenerationResult(text, tuple(calls), Usage(inp, out, inp + out), 'tool_calls' if calls else 'stop', _state(self, key, model, value['content']))


class OpenRouterProvider(FixedProvider):
    id, name = 'openrouter', 'OpenRouter'
    base_url = 'https://openrouter.ai/api/v1'
    native_strict_json_output = True

    async def _request(self, key, method, path, body=None):
        if body is not None:
            body = dict(body, provider={'require_parameters': True}, reasoning={'exclude': True})
        return await super()._request(key, method, path, body)


class CustomProvider(FixedProvider):
    id, name = 'custom', 'Custom OpenAI-compatible'
    native_strict_json_output = True

    def __init__(self, *, allow_internal=False, **options):
        super().__init__(**options)
        self.allow_internal = allow_internal

    def _credentials(self, key):
        from feedback.ai_endpoints import unpack_credentials
        return unpack_credentials(key, allow_internal=self.allow_internal)

    async def list_models(self, key):
        # Explicit IDs support servers without /models. This is not evidence:
        # each selected model must still pass its synthetic capability checks.
        return tuple(self._credentials(key)['models'])

    async def _request(self, key, method, path, body=None):
        from feedback.ai_endpoints import endpoint_request
        config = self._credentials(key)
        if body and body.get('model') not in config['models']:
            raise ProviderError('model_or_endpoint_unavailable')
        return await endpoint_request(self._transport, self._total_timeout, method, config['base_url'], path, config['key'], body, allow_internal=self.allow_internal)

# Reviewed modules only. No import paths or executable plugins come from users.
PROVIDERS = MappingProxyType({'kodekey': KodeKeyProvider, 'gemini': GeminiProvider,
                            'mistral': MistralProvider, 'groq': GroqProvider,
                            'openai': OpenAIProvider, 'anthropic': AnthropicProvider,
                            'openrouter': OpenRouterProvider, 'ramp': RampRouterProvider, 'custom': CustomProvider})


def provider(provider_id, *, allow_internal=False, **test_options) -> Provider:
    if not isinstance(provider_id, str) or provider_id not in PROVIDERS:
        raise ProviderError('unsupported_provider')
    return PROVIDERS[provider_id](**test_options, **({'allow_internal': allow_internal} if provider_id == 'custom' else {}))
