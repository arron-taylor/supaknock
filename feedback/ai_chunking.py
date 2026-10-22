"""Bounded original-text passages, with lossless fallback for token overflow."""
import re

from feedback.errors import Error


MAX_CHUNKS = 1024
MAX_PROMPT_BYTES = 1800


def _bounded_text(text, maximum):
    return text[:maximum].encode('utf-8')[:maximum].decode('utf-8', errors='ignore')


def plain_label(value):
    """A short plain label; the original source text remains unchanged."""
    plain = re.sub(r'<[^>]*>', '', value)
    return _bounded_text(' '.join(plain.split()), 128) or 'Support document'


def _boundary(text, start, upper):
    """Prefer nearby paragraph, line, then word endings without losing bytes."""
    lower = start + max(1, (upper - start) // 2)
    for separator in ('\n\n', '\n', ' '):
        match = text.rfind(separator, lower, upper)
        if match >= lower:
            return match + len(separator)
    return upper


def chunk_markdown(text, title, location=None):
    """Keep exact source spans, with ATX headings and fenced-code awareness.

    The initial UTF-8 budget is a resource bound, not a tokenizer guarantee.
    Ingestion must use split_chunk when the actual model rejects a token count.
    Only whitespace-only spans may be omitted; no source facts are truncated.
    """
    if (not isinstance(text, str) or not text.strip() or not isinstance(title, str)
            or (location is not None and not isinstance(location, str))):
        raise Error(400, 'invalid_knowledge', 'A source must contain readable text')
    try:
        if len(text.encode('utf-8')) > 1048576 or len(title.encode('utf-8')) > 32768:
            raise ValueError()
        if location is not None and len(location.encode('utf-8')) > 8192:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise Error(400, 'invalid_knowledge', 'The source text or label is invalid or too large') from None
    sections, start, cursor, label, fence = [], 0, 0, plain_label(title), None
    for line in text.splitlines(keepends=True):
        marker = re.match(r' {0,3}(`{3,}|~{3,})(.*?)(?:\r?\n)?$', line)
        if marker:
            run, rest = marker.groups()
            if fence is None and (run[0] != '`' or '`' not in rest):
                fence = (run[0], len(run))
            elif fence and run[0] == fence[0] and len(run) >= fence[1] and not rest.strip():
                fence = None
        heading = re.match(r' {0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*(?:\r?\n)?$', line) if fence is None else None
        if heading:
            if cursor > start:
                sections.append((start, cursor, label))
                start = cursor
            label = plain_label(heading.group(1))
        cursor += len(line)
    sections.append((start, len(text), label))
    chunks = []
    for begin, end, label in sections:
        budget = MAX_PROMPT_BYTES - len(('title: ' + label + ' | text: ').encode('utf-8'))
        cursor = begin
        while cursor < end:
            # No repeated encoding of the entire remaining (up to1MiB) source.
            prefix = _bounded_text(text[cursor:min(end, cursor + budget)], budget)
            upper = cursor + len(prefix)
            if upper <= cursor:
                raise Error(400, 'invalid_knowledge', 'A source cannot be split safely')
            finish = _boundary(text, cursor, upper) if upper < end else end
            content = text[cursor:finish]
            if content.strip():
                chunks.append({'ordinal': len(chunks), 'title': label, 'content': content,
                               'location': location, 'start': cursor, 'end': finish})
                if len(chunks) > MAX_CHUNKS:
                    raise Error(400, 'knowledge_limit', 'This source has too many passages to index')
            cursor = finish
    if not chunks:
        raise Error(400, 'invalid_knowledge', 'A source must contain readable text')
    return chunks


def split_chunk(chunk):
    """Split a rejected passage into two smaller exact, meaningful source spans.

    Both halves retain the original ordinal as their group identity. The worker
    assigns final unique ordinals only after all adaptive splits, before staging.
    """
    try:
        content, start, end = chunk['content'], chunk['start'], chunk['end']
        if (not isinstance(content, str) or type(start) is not int or type(end) is not int
                or start < 0 or end - start != len(content)):
            raise ValueError()
        nonspace = [match.start() for match in re.finditer(r'\S', content)]
        if len(nonspace) < 2:
            raise ValueError()
        middle = len(content) // 2
        cut = _boundary(content, 0, middle) if middle > 1 else middle
        # Very uneven whitespace must not create an unembeddable empty half.
        cut = min(max(cut, nonspace[0] + 1), nonspace[-1])
        if not content[:cut].strip() or not content[cut:].strip():
            raise ValueError()
        return [dict(chunk, content=content[:cut], end=start + cut),
                dict(chunk, content=content[cut:], start=start + cut)]
    except (KeyError, TypeError, ValueError):
        raise Error(400, 'embedding_input_too_long', 'A source passage cannot fit the embedding model input limit') from None
