"""Independent crawler boundary checks; all remote responses are fixtures."""
from contextlib import contextmanager
import io
import socket
import time

import pytest

from feedback.ai_crawler import (CrawlError, Robots, canonical_url, crawl,
    extract_html, public_request, sitemap_urls, supervised_crawl)


@pytest.mark.parametrize('value', [
    'file:///etc/passwd', 'ftp://docs.example/help', 'https://name:secret@docs.example/',
    'https://docs.example:8443/', 'https://docs.example/a?token=private',
    'https://127.0.0.1/', 'https://169.254.169.254/latest/', 'http://100.100.100.200/',
    'https://[::1]/', 'https://[::ffff:127.0.0.1]/', 'https://[fe80::1]/',
    'https://docs.example/%2e%2e/private', 'https://docs.example/%252e%252e/private',
    'https://docs.example/docs%2fprivate', 'https://docs.example/docs%5cprivate',
    'https://docs.example/a%0d%0aHeader:value', 'https://docs.example/a\\private',
])
def test_unsafe_or_ambiguous_urls_refused_before_transport(value):
    with pytest.raises(CrawlError) as error:
        canonical_url(value)
    assert error.value.code == 'invalid_url'


@pytest.mark.parametrize('blocked', ['127.0.0.1', '10.0.0.1', '169.254.169.254',
    '100.100.100.200', '::1', '::ffff:127.0.0.1', 'fe80::1', 'ff02::1'])
def test_one_private_dns_answer_blocks_whole_destination(monkeypatch, blocked):
    def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
                (socket.AF_INET6 if ':' in blocked else socket.AF_INET, socket.SOCK_STREAM, 6, '', (blocked, 443))]
    monkeypatch.setattr('feedback.webhooks.socket.getaddrinfo', resolve)
    monkeypatch.setattr('feedback.ai_crawler.PinnedHTTP', lambda *a: pytest.fail('Blocked DNS must never open a socket'))
    with pytest.raises(CrawlError) as error:
        with public_request('https://docs.example/help'):
            pytest.fail('Blocked destination yielded a response')
    assert error.value.code == 'blocked_destination'


def test_transport_pins_revalidated_public_address_and_never_sends_credentials(monkeypatch):
    seen, lookups = [], []
    def resolve(host, port, **kwargs):
        lookups.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
    class Connection:
        def __init__(self, *args): seen.append(args)
        def request(self, method, path, *, headers):
            seen.append((method, path, headers))
        def getresponse(self): return object()
        def close(self): seen.append('closed')
    monkeypatch.setattr('feedback.webhooks.socket.getaddrinfo', resolve)
    monkeypatch.setattr('feedback.ai_crawler.PinnedHTTP', Connection)
    monkeypatch.setenv('HTTPS_PROXY', 'http://name:private@127.0.0.1:1234')
    for _ in range(2):
        with public_request('https://docs.example/help'):
            pass
    assert lookups == [('docs.example', 443)] * 2
    assert seen[0] == ('docs.example', '93.184.216.34', 443, True)
    method, path, headers = seen[1]
    assert method == 'GET' and path == '/help'
    assert headers['Accept-Encoding'] == 'identity'
    assert not {'authorization', 'cookie', 'proxy-authorization'} & {key.lower() for key in headers}


class Response:
    def __init__(self, status=200, body=b'', content_type='text/html', **headers):
        self.status, self.body = status, io.BytesIO(body)
        self.headers = {'Content-Type': content_type, **headers}
    def read(self, size): return self.body.read(size)
    def getheader(self, name, default=None): return self.headers.get(name, default)


def run_fixture(responses):
    seen, pages = [], []
    @contextmanager
    def request(url):
        seen.append(url)
        yield responses.get(url, Response(404))
    result = crawl({'seed_url': 'https://docs.example/docs', 'approved_origins': ['https://docs.example'],
        'include_paths': ['/docs'], 'exclude_paths': ['/docs/private'], 'max_pages': 5, 'max_depth': 2},
        request=request, on_page=pages.append)
    return result, seen, pages


@pytest.mark.parametrize('status', [301, 302, 307, 308, 401, 403, 500])
def test_robots_failures_do_not_grant_permission(status):
    result, seen, pages = run_fixture({'https://docs.example/robots.txt': Response(status, Location='https://other.example/robots.txt')})
    assert not result['complete'] and pages[0]['error'] == 'robots_unavailable'
    assert seen == ['https://docs.example/robots.txt']


def test_page_redirect_is_reported_without_fetching_its_target():
    result, seen, pages = run_fixture({'https://docs.example/docs': Response(302, Location='http://169.254.169.254/latest/')})
    assert not result['complete'] and pages[0]['error'] == 'redirect_refused'
    assert all('169.254' not in url for url in seen)


def test_scope_query_and_fragment_traps_never_expand_fetches():
    body = b'''<title>Guide</title><main><h1>Useful support guide</h1><p>Documentation for installing the application.</p>
        <a href="/docs#one">one</a><a href="/docs#two">two</a><a href="/docs?page=1">query</a>
        <a href="/docs/private/secret">private</a><a href="/docs-other">prefix trick</a>
        <a href="//other.example/docs">other origin</a><a href="/docs/%2e%2e/private">encoded escape</a></main>'''
    result, seen, pages = run_fixture({'https://docs.example/docs': Response(body=body)})
    assert result['complete'] and len(pages) == 1
    assert seen == ['https://docs.example/robots.txt', 'https://docs.example/docs', 'https://docs.example/sitemap.xml']


def test_compressed_content_and_oversized_lies_are_refused():
    for response, code in [
        (Response(body=b'not decompressed', **{'Content-Encoding': 'gzip'}), 'unexpected_encoding'),
        (Response(body=b'x' * (262144 + 1), **{'Content-Length': '1'}), 'response_too_large'),
    ]:
        result, _, pages = run_fixture({'https://docs.example/docs': response})
        assert not result['complete'] and pages[0]['error'] == code


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-16', 'utf-16-le', 'utf-16-be'])
def test_sitemap_dtd_entity_declarations_always_refused(encoding):
    raw = '<!DOCTYPE urlset [<!ENTITY target "https://docs.example/docs">]><urlset><url><loc>&target;</loc></url></urlset>'.encode(encoding)
    with pytest.raises(CrawlError) as error:
        sitemap_urls(raw)
    assert error.value.code == 'invalid_sitemap'


@pytest.mark.parametrize('title', ['404 - Not Found', 'Page not found', 'Internal Server Error'])
def test_soft_error_pages_are_not_knowledge(title):
    body = f'<title>{title}</title><main><p>This resource could not be found. Please return to our homepage.</p></main>'.encode()
    with pytest.raises(CrawlError) as error:
        extract_html(body, 'text/html')
    assert error.value.code == 'error_page'


def test_robots_matching_merges_product_groups_and_honors_specific_allow():
    rules = Robots('User-agent: *\nDisallow: /\nUser-agent: SuggestionBoxCrawler\nDisallow: /docs\nUser-agent: suggestionboxcrawler\nAllow: /docs/public\nDisallow: /docs/public/private\n')
    assert rules.allowed('https://docs.example/elsewhere')
    assert not rules.allowed('https://docs.example/docs')
    assert rules.allowed('https://docs.example/docs/public')
    assert not rules.allowed('https://docs.example/docs/public/private')


def unresponsive_child(connection, config, timeout):
    time.sleep(30)


def test_process_deadline_bounds_unresponsive_dns_headers_or_parser():
    started = time.monotonic()
    with pytest.raises(CrawlError) as error:
        supervised_crawl({}, on_page=lambda page: None, timeout=.2, _target=unresponsive_child)
    assert error.value.code == 'timeout' and time.monotonic() - started < 3
