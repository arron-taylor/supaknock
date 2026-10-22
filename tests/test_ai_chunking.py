"""Full-text and adaptive-splitting contracts independent of model tokenizer."""
import pytest

from feedback.ai_chunking import chunk_markdown, split_chunk
from feedback.errors import Error


def assert_preserved(text, chunks):
    previous = 0
    for chunk in chunks:
        assert chunk['content'] == text[chunk['start']:chunk['end']]
        assert chunk['start'] >= previous and not text[previous:chunk['start']].strip()
        assert chunk['content'].strip()
        assert len(('title: ' + chunk['title'] + ' | text: ' + chunk['content']).encode()) <= 1800
        previous = chunk['end']
    assert not text[previous:].strip()


def test_adaptive_splits_preserve_all_source_characters_and_provenance():
    text = '# Heading\n' + ('﷽🙂 e\u0301 some source text\n' * 300) + 'THE_FINAL_ANSWER'
    chunks = chunk_markdown(text, 'Original', 'https://docs.example/help')
    expanded = []
    for chunk in chunks:
        pair = split_chunk(chunk)
        assert all(c['ordinal'] == chunk['ordinal'] for c in pair)
        assert ''.join(c['content'] for c in pair) == chunk['content']
        assert all(c['title'] == chunk['title'] and c['location'] == chunk['location'] for c in pair)
        assert all(len(c['content']) < len(chunk['content']) for c in pair)
        expanded.extend(pair)
    assert_preserved(text, expanded)
    assert expanded[-1]['content'].endswith('THE_FINAL_ANSWER')


def test_fence_length_and_info_string_do_not_create_headings_inside_code():
    text = '# Outside\n\n````markdown\n```\n# Not a heading\n```python\n## Still code\n````\n\n## Actual heading\nFinal source fact.\n'
    chunks = chunk_markdown(text, 'Guide')
    assert [c['title'] for c in chunks] == ['Outside', 'Actual heading']
    assert '# Not a heading' in chunks[0]['content']
    assert '## Still code' in chunks[0]['content']
    assert_preserved(text, chunks)


@pytest.mark.parametrize('content', ['a   b', ' ' * 900 + 'ab', 'ab' + ' ' * 900, '🙂🙂', 'α\n\nβ'])
def test_split_never_returns_a_whitespace_only_half(content):
    chunk = {'ordinal': 4, 'title': 'Guide', 'content': content, 'location': None, 'start': 10, 'end': 10 + len(content)}
    pair = split_chunk(chunk)
    assert pair[0]['start'] == 10 and pair[-1]['end'] == chunk['end']
    assert pair[0]['end'] == pair[1]['start']
    assert ''.join(c['content'] for c in pair) == content
    assert all(c['content'].strip() for c in pair)


@pytest.mark.parametrize('content', ['x', '    x    ', '🙂'])
def test_unsplittable_model_input_fails_explicitly(content):
    with pytest.raises(Error) as error:
        split_chunk({'content': content, 'start': 0, 'end': len(content)})
    assert error.value.code == 'embedding_input_too_long'


def test_large_whitespace_regions_do_not_drop_later_facts():
    text = '# Facts\nFirst fact.' + ' ' * 20000 + '\nSecond fact at the end.'
    assert_preserved(text, chunk_markdown(text, 'Guide'))


@pytest.mark.parametrize('text,title', [('x\ud800', 'Guide'), ('hello', '\ud800'), ('   ', 'Guide')])
def test_invalid_original_or_label_is_rejected(text, title):
    with pytest.raises(Error):
        chunk_markdown(text, title)
