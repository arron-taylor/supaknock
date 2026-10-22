"""Crawl traversal/extraction/limits using local HTTP fixtures, never the network."""
from contextlib import contextmanager
import io
import time

import pytest

from feedback.ai_crawler import (CrawlError, Robots, canonical_url, crawl,
    extract_html, in_scope, sitemap_urls, supervised_crawl, validate_crawl_config)
from feedback.errors import Error


ORIGIN = 'https://docs.example'


def config(**changes):
    return dict(seed_url=ORIGIN + '/docs', approved_origins=[ORIGIN],
        include_paths=['/docs'], exclude_paths=['/docs/private'], max_pages=20, max_depth=3) | changes


def html(title='Guide', links=''):
    return ('<title>' + title + '</title><main><h1>Installation guide</h1>'
        '<p>Follow these documented steps to configure support.</p>' + links + '</main>').encode()


class Response:
    def __init__(self, body=b'', status=200, kind='text/html', **headers):
        self.status, self.body = status, io.BytesIO(body)
        self.headers = {'Content-Type': kind, **headers}
    def getheader(self, name, default=None): return self.headers.get(name, default)
    def read(self, size): return self.body.read(size)


def fixture(responses, settings=None):
    seen, pages = [], []
    @contextmanager
    def request(url):
        seen.append(url)
        response = responses.get(url, Response(status=404))
        if isinstance(response, Exception): raise response
        yield response
    result = crawl(settings or config(), request=request, on_page=pages.append)
    return result, seen, pages


def test_canonical_fragment_idna_default_port_and_segment_scope():
    assert canonical_url('https://DOCS.Example:443/docs#topic') == ORIGIN + '/docs'
    assert canonical_url('https://bücher.example/über') == 'https://xn--bcher-kva.example/%C3%BCber'
    assert canonical_url('../docs/help', ORIGIN + '/docs/start') == ORIGIN + '/docs/help'
    normalized = validate_crawl_config(config(include_paths=['/docs/', '/docs']))
    assert normalized['include_paths'] == ['/docs']
    assert in_scope(ORIGIN + '/docs/help', normalized)
    assert not in_scope(ORIGIN + '/docstring', normalized)
    assert not in_scope(ORIGIN + '/docs/private/nested', normalized)


@pytest.mark.parametrize('changes', [
    {'max_pages': True}, {'max_pages': 51}, {'max_depth': 6},
    {'approved_origins': []}, {'approved_origins': [ORIGIN + '/docs']},
    {'approved_origins': [ORIGIN, ORIGIN + '/']}, {'approved_origins': ['https://other.example']},
    {'include_paths': []}, {'exclude_paths': ['/docs']}, {'include_paths': ['/docs?token=x']},
])
def test_invalid_scope_is_rejected_without_network(changes):
    with pytest.raises(Error) as error:
        validate_crawl_config(config(**changes))
    assert error.value.code == 'invalid_crawl_config'


def test_document_structure_and_links_survive_boilerplate_removal():
    raw = b'''<title>How to configure</title><nav>Ignore navigation</nav><div>Ignore outside main</div>
    <main><h1>Quick start</h1><p>Use these steps for a working setup.</p><ul><li>Create project</li><li>Choose model</li></ul>
    <pre><code>if ready:\n    configure()</code></pre><table><tr><th>Setting</th><td>Value</td></tr></table>
    <div hidden>Hidden secret</div><script>evil()</script><style>css</style><a href="/docs/next">Continue</a></main><footer>Ignore footer</footer>'''
    title, content, links = extract_html(raw, 'text/html; charset=utf-8')
    assert title == 'How to configure' and '# Quick start' in content
    assert '- Create project' in content and '\n```\nif ready:\n    configure()\n```' in content
    assert ' | Setting | Value' in content and links == ['/docs/next']
    assert not any(x in content for x in ('Ignore', 'Hidden secret', 'evil()', 'css'))


def test_visible_navigation_links_are_discovered_without_boilerplate_text():
    _, content, links = extract_html(b'<nav><a href="/docs/setup">Navigation copy</a></nav><div hidden><a href="/docs/hidden">Hidden</a></div>' + html(), 'text/html')
    assert 'Navigation copy' not in content and 'Hidden' not in content
    assert links == ['/docs/setup']


@pytest.mark.parametrize('raw,code', [
    (b'<title>Account</title><input type="password"><p>Sign in to continue using documentation.</p>', 'login_page'),
    (html('Just a moment...'), 'challenge_page'),
    (b'<meta name="robots" content="noindex">' + html(), 'robots_denied'),
    (b'<title>Loading</title><script>render_application()</script>', 'empty_page'),
    (html() + b'\xff', 'invalid_text'),
])
def test_non_document_pages_are_rejected(raw, code):
    with pytest.raises(CrawlError) as error:
        extract_html(raw, 'text/html')
    assert error.value.code == code


def test_robots_longest_rule_tie_allow_unicode_reserved_and_end_anchor():
    policy = Robots('User-agent: *\nDisallow: /docs/*\nAllow: /docs/public\nDisallow: /docs/public\n'
        'Disallow: /docs/%7Eprivate\nAllow: /docs/über\nDisallow: /end$\nDisallow: /encoded%2Fpath\n')
    assert policy.allowed(ORIGIN + '/docs/public/help')
    assert not policy.allowed(ORIGIN + '/docs/~private')
    assert policy.allowed(ORIGIN + '/docs/%C3%BCber')
    assert not policy.allowed(ORIGIN + '/end') and policy.allowed(ORIGIN + '/end/child')
    assert not policy.allowed(ORIGIN + '/encoded%2Fpath') and policy.allowed(ORIGIN + '/encoded/path')


def test_explicit_depth_limit_and_fragment_deduplication():
    result, seen, pages = fixture({ORIGIN + '/docs': Response(html(links='<a href="/docs#same">same</a><a href="/docs/child">child</a>')),
        ORIGIN + '/docs/child': Response(html(links='<a href="/docs/grandchild">deep</a>'))}, config(max_depth=1))
    assert result['complete'] and len(pages) == 2
    assert ORIGIN + '/docs/grandchild' not in seen


def test_sitemap_index_is_bounded_to_same_origin_and_page_scope():
    result, seen, pages = fixture({
        ORIGIN + '/robots.txt': Response(b'User-agent: *\nSitemap: https://docs.example/map.xml', kind='text/plain'),
        ORIGIN + '/docs': Response(html()),
        ORIGIN + '/map.xml': Response(b'<sitemapindex><sitemap><loc>https://docs.example/pages.xml</loc></sitemap><sitemap><loc>https://private.example/map.xml</loc></sitemap></sitemapindex>', kind='application/xml'),
        ORIGIN + '/pages.xml': Response(b'<urlset><url><loc>https://docs.example/docs/from-map</loc></url><url><loc>https://docs.example/docs/private/secret</loc></url><url><loc>https://other.example/docs</loc></url></urlset>', kind='text/xml'),
        ORIGIN + '/docs/from-map': Response(html('Mapped documentation')),
    })
    assert result['complete'] and [p['url'] for p in pages] == [ORIGIN + '/docs', ORIGIN + '/docs/from-map']
    assert ORIGIN + '/sitemap.xml' not in seen
    assert not any('private.example' in url or '/secret' in url or 'other.example' in url for url in seen)


def test_page_limit_is_successful_completion_without_fetching_child_or_sitemap():
    responses = {ORIGIN + '/docs': Response(html(links='<a href="/docs/child">child</a>'))}
    result, seen, pages = fixture(responses, config(max_pages=1))
    assert result['complete'] and result['error'] is None and len(pages) == 1
    assert result['stop_reason'] == 'page_limit'
    assert ORIGIN + '/docs/child' not in seen and ORIGIN + '/sitemap.xml' not in seen


def test_failed_child_is_skipped_without_discarding_good_pages():
    result, _, pages = fixture({ORIGIN + '/docs': Response(html(links='<a href="/docs/child">child</a>'))})
    assert result['complete'] and result['error'] is None
    assert pages[-1]['url'].endswith('/child') and pages[-1]['content'] == ''
    assert pages[-1]['state'] == 'skipped' and pages[-1]['error'] == 'http_failed'
    assert result['stop_reason'] is None


def test_skips_count_toward_the_normal_page_cap():
    result, _, pages = fixture({ORIGIN + '/docs': Response(html(links='<a href="/docs/bad">bad</a><a href="/docs/later">later</a>'))}, config(max_pages=2))
    assert result['complete'] and result['error'] is None
    assert len(pages) == 2 and result['stop_reason'] == 'page_limit'
    assert pages[-1]['state'] == 'skipped'


def test_invalid_sitemap_or_robots_never_becomes_document_content():
    result, _, pages = fixture({ORIGIN + '/docs': Response(html()), ORIGIN + '/sitemap.xml': Response(b'<not-xml', kind='application/xml')})
    assert result['complete'] and result['error'] is None
    assert pages[-1]['state'] == 'skipped' and pages[-1]['error'] == 'invalid_sitemap'
    assert pages[-1]['content'] == '' and pages[-1]['url'].endswith('sitemap.xml')
    result, seen, pages = fixture({ORIGIN + '/robots.txt': Response(b'User-agent: *\nDisallow: /docs', kind='text/plain')})
    assert not result['complete'] and pages[0]['error'] == 'robots_denied'
    assert ORIGIN + '/docs' not in seen


def test_sitemap_utf8_namespace_and_entities():
    assert sitemap_urls(b'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://docs.example/docs</loc></url></urlset>') == ('urlset', [ORIGIN + '/docs'])
    with pytest.raises(CrawlError):
        sitemap_urls(b'<?xml version="1.0" encoding="ISO-8859-1"?><urlset/>')


def test_sitemap_metadata_exception_never_overrides_excluded_paths():
    result, seen, _ = fixture({
        ORIGIN + '/robots.txt': Response(b'User-agent: *\nSitemap: https://docs.example/docs/private/sitemap.xml', kind='text/plain'),
        ORIGIN + '/docs': Response(html()),
    })
    assert result['complete']
    assert ORIGIN + '/docs/private/sitemap.xml' not in seen


def test_each_explicitly_approved_origin_gets_its_own_robots_check():
    second = 'https://help.example'
    result, seen, pages = fixture({
        ORIGIN + '/docs': Response(html(links='<a href="https://help.example/docs/answer">answer</a>')),
        second + '/docs/answer': Response(html('Other approved support source')),
    }, config(approved_origins=[ORIGIN, second]))
    assert result['complete'] and len(pages) == 2
    assert seen.index(second + '/robots.txt') < seen.index(second + '/docs/answer')


def cancellation_child(connection, config, timeout):
    connection.send(('page', {'fixture': True}))
    time.sleep(30)


def test_cancel_stops_process_even_when_remote_is_blocked():
    seen = []
    started = time.monotonic()
    with pytest.raises(CrawlError) as error:
        supervised_crawl({}, on_page=seen.append, cancelled=lambda: bool(seen), timeout=3, _target=cancellation_child)
    assert error.value.code == 'cancelled' and seen == [{'fixture': True}]
    assert time.monotonic() - started < 3


def test_mixed_unusable_pages_are_skipped_and_later_pages_still_import():
    cases = {
        'login': Response(b'<input type="password"><p>Please sign in to your account.</p>'),
        'blank': Response(b'<script>render()</script>'),
        'redirect': Response(status=302, Location='http://127.0.0.1/private'),
        'missing': Response(status=404),
        'network': CrawlError('network_failed'),
        'file': Response(b'file', kind='application/pdf'),
        'later': Response(html('Later useful guide')),
    }
    links = ''.join('<a href="/docs/' + name + '">' + name + '</a>' for name in cases)
    result, seen, pages = fixture({ORIGIN + '/docs': Response(html(links=links)),
        **{ORIGIN + '/docs/' + name: response for name, response in cases.items()}})
    assert result['complete'] and result['error'] is None
    assert [p['state'] for p in pages] == ['succeeded'] + ['skipped'] * 6 + ['succeeded']
    assert [p['error'] for p in pages[1:-1]] == ['login_page', 'empty_page', 'redirect_refused', 'http_failed', 'network_failed', 'unexpected_type']
    assert all(p['content'] == '' for p in pages[1:-1])
    assert 'http://127.0.0.1/private' not in seen


def test_unusable_seed_can_recover_through_sitemap():
    result, _, pages = fixture({ORIGIN + '/docs': Response(b''),
        ORIGIN + '/sitemap.xml': Response(b'<urlset><url><loc>https://docs.example/docs/guide</loc></url></urlset>', kind='application/xml'),
        ORIGIN + '/docs/guide': Response(html())})
    assert result['complete'] and result['error'] is None
    assert [p['state'] for p in pages] == ['skipped', 'succeeded']


def test_all_skipped_pages_do_not_claim_a_successful_empty_import():
    result, _, pages = fixture({ORIGIN + '/docs': Response(b'')})
    assert not result['complete'] and result['error'] == 'empty_page'
    assert pages[0]['state'] == 'skipped' and pages[0]['content'] == ''


def test_unavailable_robots_origin_is_checked_once_and_other_origin_survives():
    second = 'https://help.example'
    links = ''.join(f'<a href="{second}/docs/{n}">Help</a>' for n in range(3))
    result, seen, pages = fixture({ORIGIN + '/docs': Response(html(links=links)),
        second + '/robots.txt': Response(status=503)}, config(approved_origins=[ORIGIN, second]))
    assert result['complete'] and len(pages) == 4
    assert all(p['error'] == 'robots_unavailable' for p in pages[1:])
    assert seen.count(second + '/robots.txt') == 1
    assert not any(url.startswith(second + '/docs') for url in seen)


@pytest.mark.parametrize('budget,limit,code', [('MAX_TOTAL_BYTES', 100, 'byte_limit'), ('MAX_TOTAL_TEXT', 40, 'total_text_limit')])
def test_whole_crawl_budgets_still_abort(budget, limit, code, monkeypatch):
    monkeypatch.setattr('feedback.ai_crawler.' + budget, limit)
    with pytest.raises(CrawlError) as error:
        fixture({ORIGIN + '/docs': Response(html())})
    assert error.value.code == code


def test_oversized_page_is_skipped_without_spending_the_whole_crawl_budget():
    result, _, pages = fixture({ORIGIN + '/docs': Response(html(links='<a href="/docs/huge">Huge</a>')),
        ORIGIN + '/docs/huge': Response(b'x' * 270000)})
    assert result['complete'] and pages[-1]['state'] == 'skipped'
    assert pages[-1]['error'] == 'response_too_large'


def test_sitemap_also_linked_as_a_page_has_one_immutable_outcome():
    @contextmanager
    def request(url):
        if url.endswith('/robots.txt'):
            yield Response(b'User-agent: *\nSitemap: https://docs.example/docs/map.xml', kind='text/plain')
        elif url.endswith('/map.xml'):
            yield Response(b'<bad-xml', kind='application/xml')
        else:
            yield Response(html(links='<a href="/docs/map.xml">Site map</a>'))
    pages = []
    result = crawl(config(), request=request, on_page=pages.append)
    assert result['complete'] and result['error'] is None
    assert len(pages) == 2 and pages[-1]['error'] == 'unexpected_type'
    assert len({p['url'] for p in pages}) == len(pages)
