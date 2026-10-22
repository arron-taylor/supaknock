"""Independent topic policy; personality, retrieval and tools confer no scope."""
import hashlib
import math
import re

from feedback.errors import Error

# Provisional v1 thresholds: calibration with the actual gateway remains required.
PRESETS = {'balanced': (0.20, 0.80), 'strict': (0.20, 0.95)}
POLICY_VERSION = 'topics-v1-provisional'
CLARIFY = 'Could you clarify how your question relates to {scope}? I can help with those topics, or you can choose human support.'
UNAVAILABLE = 'I can’t check the allowed topics right now. Please try again later or choose human support.'
DEFAULT_REFUSAL = 'Sorry, I can only help with {scope}. You can choose human support for other questions.'


def default_topic_policy():
    return {'enabled': False, 'provider': 'kodekey', 'model': None,
            'scope': '', 'public_summary': '', 'allowed_examples': [],
            'disallowed_examples': [], 'preset': 'balanced', 'refusal_messages': []}


def _text(value, maximum):
    if (not isinstance(value, str) or len(value) > maximum
            or any(ord(c) < 32 and c not in '\n\t' or ord(c) == 127 for c in value)):
        raise Error(400, 'invalid_input', 'Invalid topic policy text')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise Error(400, 'invalid_input', 'Invalid topic policy text') from None
    return value


def validate_topic_policy(value):
    from feedback.ai_decision_providers import DECISION_PROVIDERS
    if (not isinstance(value, dict) or set(value) != set(default_topic_policy())
            or type(value['enabled']) is not bool or not isinstance(value['provider'], str) or value['provider'] not in DECISION_PROVIDERS
            or not isinstance(value['preset'], str) or value['preset'] not in PRESETS):
        raise Error(400, 'invalid_input', 'Supply every topic policy setting')
    if value['model'] is not None and (not isinstance(value['model'], str) or
            not 1 <= len(value['model']) <= 256 or any(not 32 < ord(c) < 127 for c in value['model'])):
        raise Error(400, 'invalid_input', 'Invalid decision model')
    _text(value['scope'], 4000)
    _text(value['public_summary'], 240)
    for name, count, maximum in [('allowed_examples', 10, 300), ('disallowed_examples', 10, 300), ('refusal_messages', 5, 500)]:
        if not isinstance(value[name], list) or len(value[name]) > count:
            raise Error(400, 'invalid_input', 'Too many topic policy entries')
        for item in value[name]:
            if not _text(item, maximum).strip():
                raise Error(400, 'invalid_input', 'Topic policy entries cannot be empty')
    # These templates are local plain text, not executable instructions or links.
    for item in [value['public_summary'], *value['refusal_messages']]:
        if re.search(r'(?:[a-z][a-z0-9+.-]*://|\bwww\.|<[^>]*>|\]\s*\()', item, re.I):
            raise Error(400, 'invalid_input', 'Use plain text without links or HTML')
    for item in value['refusal_messages']:
        if '{' in item.replace('{scope}', '') or '}' in item.replace('{scope}', ''):
            raise Error(400, 'invalid_input', 'Only the {scope} placeholder is supported')
        if len(item.replace('{scope}', value['public_summary'])) > 4000:
            raise Error(400, 'invalid_input', 'A rendered refusal must be at most 4,000 characters')
    if value['enabled'] and (not value['scope'].strip() or not value['public_summary'].strip() or not value['model']):
        raise Error(400, 'invalid_input', 'A scope, public summary and decision model are required')
    return value


def topic_context(history):
    """Keep whole preceding messages. Never guess after an oversized exchange."""
    if not isinstance(history, (list, tuple)):
        raise Error(400, 'invalid_input', 'Invalid decision context')
    selected, size = [], 0
    for item in reversed(history[-4:]):
        if not isinstance(item, dict) or item.get('sender_role') not in ('customer', 'visitor', 'ai', 'admin'):
            raise Error(400, 'invalid_input', 'Invalid decision context')
        body = _text(item.get('body'), 16000)
        size += len(body.encode('utf-8'))
        if size > 8192:
            return [], True
        selected.append({'speaker': item['sender_role'], 'text': body})
    return list(reversed(selected)), False


def probability_outcome(probability, preset):
    if type(probability) not in (int, float) or not math.isfinite(probability) or not 0 <= probability <= 1:
        raise Error(502, 'ai_decision_invalid', 'The decision provider returned an invalid probability')
    refuse, allow = PRESETS[preset]
    return 'allow' if probability >= allow else 'refuse' if probability <= refuse else 'clarify'


def policy_reply(policy, outcome, identity):
    if outcome == 'greeting':
        return 'Hello! I can help with ' + policy['public_summary'] + '. You can also choose human support.'
    if outcome == 'clarify':
        template = CLARIFY
    elif outcome == 'unavailable':
        return UNAVAILABLE
    elif outcome == 'refuse':
        templates = policy['refusal_messages'] or [DEFAULT_REFUSAL]
        index = int(hashlib.sha256(identity.encode()).hexdigest(), 16) % len(templates)
        template = templates[index]
    else:
        raise Error(400, 'invalid_input', 'Invalid policy reply outcome')
    return template.replace('{scope}', policy['public_summary'])


def is_greeting(question):
    # Full-string matching prevents “Hi, write a workout plan” from passing.
    return bool(re.fullmatch(r'\s*(?:hi|hello|hey|thanks|thank you|good morning|good evening)[!.\s]*', question, re.I))
