"""Application-owned output schemas and literal support-context rendering.

Schemas constrain shape, never truth or authority. Callers still validate every
result and citation against current originals before committing it.
"""
import json
from feedback.ai_limits import MAX_SOURCES


def _object(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties),
            'additionalProperties': False}


def output_schema(kind, citation_ids=()):
    if kind == 'check':
        return _object({'assessment': {'type': 'string'},
            'issues': {'type': 'array', 'items': _object({
                'quote': {'type': 'string'}, 'reason': {'type': 'string', 'enum': [
                    'unsupported', 'overstated', 'wrong_context', 'missing_qualification', 'missing_citation']}})},
            'supported': {'type': 'boolean'}})
    ids = list(citation_ids)
    # Empty enums are invalid JSON Schema. An empty selection only permits an
    # empty citation array; ordinary generation skips absent evidence already.
    citations = {'type': 'array', 'items': {'type': 'string'}, 'maxItems': MAX_SOURCES}
    if ids:
        citations['items']['enum'] = ids
    else:
        citations['maxItems'] = 0
    if kind == 'sections':
        return _object({'sections': {'type': 'array', 'items': _object({
            'kind': {'type': 'string', 'enum': ['section', 'faq']},
            'title': {'type': 'string'}, 'body': {'type': 'string'},
            'category': {'type': 'string'}, 'citation_ids': citations})}})
    if kind not in ('answer', 'preview'):
        raise ValueError('Unknown AI output kind')
    return _object({'status': {'type': 'string', 'enum': ['answered', 'insufficient']},
                    'body': {'type': 'string'}, 'citation_ids': citations})


def render_context(payload, *, plain=False):
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    if not plain:
        return encoded
    chunks = []

    def field(name, value):
        if isinstance(value, str):
            chunks.append(name + ':\n' + value)
        elif isinstance(value, list):
            if not value:
                chunks.append(name + ':\n(empty)')
            for index, item in enumerate(value, 1):
                field(name + ' ' + str(index), item)
        elif isinstance(value, dict):
            for key, item in value.items():
                field(name + ' / ' + key, item)
        else:
            chunks.append(name + ':\n' + json.dumps(value, allow_nan=False))

    for name, value in payload.items():
        field(name, value)
    # Labels organize untrusted text; they do not confer instruction authority.
    return '\n\n'.join(chunks)
