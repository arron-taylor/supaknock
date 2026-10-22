"""Bounded public documentation crawling. Source text never grants authority."""
from collections import deque
from contextlib import contextmanager
from html.parser import HTMLParser
import ipaddress
import multiprocessing
import os
import re
import threading
import time
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

from feedback.errors import Error
from feedback.webhooks import destination, PinnedHTTP


AGENT = 'SuggestionBoxCrawler'
MAX_SECONDS = 180
MAX_HTML_BYTES = 262144
MAX_TEXT_BYTES = 65536
MAX_TOTAL_BYTES = 5 * 1024 * 1024
MAX_TOTAL_TEXT = 512 * 1024
MAX_DISCOVERED = 500
MAX_SITEMAPS = 10
MAX_ROBOTS_BYTES = 512 * 1024
MAX_XML_BYTES = 262144
_UNRESERVED = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~'


class CrawlError(Exception):
    CODES = frozenset({'invalid_url', 'blocked_destination', 'network_failed', 'redirect_refused',
        'http_failed', 'unexpected_type', 'unexpected_encoding', 'response_too_large',
        'invalid_text', 'empty_page', 'login_page', 'challenge_page', 'error_page', 'robots_denied',
        'robots_unavailable', 'invalid_robots', 'invalid_sitemap', 'cancelled', 'timeout',
        'page_limit', 'discovery_limit', 'depth_limit', 'text_limit', 'byte_limit', 'total_text_limit', 'crawl_failed'})

    def __init__(self, code):
        self.code = code if code in self.CODES else 'crawl_failed'
        super().__init__(self.code)


def canonical_url(value, base=None):
    """No credential, query, ambiguous path, or unsafe scheme is ever fetched."""
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in value) or '\\' in value:
            raise ValueError()
        raw = urljoin(base, value) if base else value
        u = urlsplit(raw)
        if u.scheme not in ('https', 'http') or not u.hostname or u.username is not None or u.password is not None or u.query:
            raise ValueError()
        port = 443 if u.scheme == 'https' else 80
        if u.port not in (None, port):
            raise ValueError()
        host = u.hostname.encode('idna').decode('ascii').lower().rstrip('.')
        if not host or '%' in host:
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host):
                raise ValueError()
        else:
            if address.version == 6 and address.ipv4_mapped:
                address = address.ipv4_mapped
            if not address.is_global or address.is_multicast or str(address) == '100.100.100.200':
                raise ValueError()
        path = u.path or '/'
        if re.search(r'%(?![0-9a-fA-F]{2})|%(?:25|2f|5c)', path, re.I):
            raise ValueError()
        decoded = unquote(path, encoding='utf-8', errors='strict')
        if any(part in ('.', '..') for part in decoded.split('/')) or '//' in decoded or any(ord(c) <= 32 or ord(c) == 127 for c in decoded):
            raise ValueError()
        path = quote(decoded, safe="/!$&'()*+,-.:;=@_~")
        authority = '[' + host + ']' if ':' in host else host
        result = urlunsplit((u.scheme, authority, path, '', ''))
        if len(result) > 2048:
            raise ValueError()
        return result
    except (ValueError, TypeError, UnicodeError):
        raise CrawlError('invalid_url') from None


def origin(url):
    u = urlsplit(url)
    return u.scheme + '://' + u.netloc


def validate_crawl_config(config):
    keys = {'seed_url', 'approved_origins', 'include_paths', 'exclude_paths', 'max_pages', 'max_depth'}
    try:
        if not isinstance(config, dict) or set(config) != keys:
            raise ValueError()
        seed = canonical_url(config['seed_url'])
        origins = config['approved_origins']
        if not isinstance(origins, list) or not 1 <= len(origins) <= 5:
            raise ValueError()
        approved = []
        for value in origins:
            normalized = canonical_url(value)
            if urlsplit(normalized).path != '/' or urlsplit(value).fragment:
                raise ValueError()
            approved.append(origin(normalized))
        if len(set(approved)) != len(approved) or origin(seed) not in approved:
            raise ValueError()
        paths = {}
        for name in ('include_paths', 'exclude_paths'):
            values = config[name]
            if not isinstance(values, list) or len(values) > 20 or (name == 'include_paths' and not values):
                raise ValueError()
            normalized = []
            for value in values:
                if not isinstance(value, str) or not value.startswith('/') or len(value) > 512 or '?' in value or '#' in value:
                    raise ValueError()
                normalized.append(urlsplit(canonical_url('https://scope.example' + value)).path.rstrip('/') or '/')
            paths[name] = sorted(set(normalized))
        if type(config['max_pages']) is not int or not 1 <= config['max_pages'] <= 50 or type(config['max_depth']) is not int or not 0 <= config['max_depth'] <= 5:
            raise ValueError()
        result = dict(config, seed_url=seed, approved_origins=approved, **paths)
        if not in_scope(seed, result):
            raise ValueError()
        return result
    except (ValueError, TypeError, CrawlError):
        raise Error(400, 'invalid_crawl_config', 'Use a public HTTP(S) page, explicitly approved origins and valid path/page limits') from None


def in_scope(url, config):
    path = urlsplit(url).path
    def matches(prefix):
        return prefix == '/' or path == prefix or path.startswith(prefix + '/')
    return origin(url) in config['approved_origins'] and any(map(matches, config['include_paths'])) and not any(map(matches, config['exclude_paths']))


def metadata_scope(url, config):
    """Robots-advertised XML may sit outside included paths; exclusions still win."""
    path = urlsplit(url).path
    return origin(url) in config['approved_origins'] and not any(
        prefix == '/' or path == prefix or path.startswith(prefix + '/')
        for prefix in config['exclude_paths'])


@contextmanager
def public_request(url):
    url = canonical_url(url)
    try:
        # Never inherit ALLOW_INTERNAL_WEBHOOKS. Every DNS answer must be public;
        # the socket uses the validated address, with TLS for the original host.
        u, address, port = destination(url, allow_internal=False)
    except Exception:
        raise CrawlError('blocked_destination') from None
    conn = PinnedHTTP(u.hostname, address, port, u.scheme == 'https')
    conn.timeout = 10
    try:
        conn.request('GET', u.path or '/', headers={'User-Agent': AGENT + '/0.2 (public documentation import)',
            'Accept': 'text/html,application/xhtml+xml,text/plain,application/xml,text/xml',
            'Accept-Encoding': 'identity', 'Connection': 'close'})
        yield conn.getresponse()
    except CrawlError:
        raise
    except Exception:
        raise CrawlError('network_failed') from None
    finally:
        conn.close()


def _checkpoint(cancelled, deadline):
    if cancelled and cancelled():
        raise CrawlError('cancelled')
    if time.monotonic() >= deadline:
        raise CrawlError('timeout')


def _read(response, maximum, budget, cancelled, deadline):
    if response.getheader('Content-Encoding', '').lower() not in ('', 'identity'):
        raise CrawlError('unexpected_encoding')
    length = response.getheader('Content-Length')
    if length is not None and (not length.isascii() or not length.isdigit() or len(length) > 10 or int(length) > maximum):
        raise CrawlError('response_too_large')
    body = bytearray()
    while True:
        _checkpoint(cancelled, deadline)
        block = response.read(min(16384, maximum - len(body) + 1))
        if not block:
            break
        body.extend(block)
        budget[0] += len(block)
        if budget[0] > MAX_TOTAL_BYTES:
            raise CrawlError('byte_limit')
        if len(body) > maximum:
            raise CrawlError('response_too_large')
    return bytes(body)


def _text(raw, content_type):
    charset = re.search(r'charset\s*=\s*["\']?([^;"\'\s]+)', content_type, re.I)
    encoding = charset.group(1).lower() if charset else 'utf-8-sig'
    if encoding not in ('utf-8', 'utf8', 'utf-8-sig', 'us-ascii', 'iso-8859-1', 'windows-1252'):
        raise CrawlError('invalid_text')
    try:
        return raw.decode(encoding)
    except UnicodeError:
        raise CrawlError('invalid_text') from None


def _glob_prefix(value, pattern):
    # REP has only '*' and terminal '$', not general regular expressions.
    exact = pattern.endswith('$')
    pattern = pattern[:-1] if exact else pattern + '*'
    vi = pi = 0
    star = consumed = -1
    while vi < len(value):
        if pi < len(pattern) and pattern[pi] == '*':
            star, consumed, pi = pi, vi, pi + 1
        elif pi < len(pattern) and pattern[pi] == value[vi]:
            pi, vi = pi + 1, vi + 1
        elif star >= 0:
            consumed += 1
            vi, pi = consumed, star + 1
        else:
            return False
    return not pattern[pi:].strip('*')


def _robots_path(value):
    # Decode only percent-encoded ASCII unreserved bytes; preserve reserved bytes.
    def replace(match):
        character = chr(int(match.group(1), 16))
        return character if character in _UNRESERVED else '%' + match.group(1).upper()
    return re.sub(r'%([0-9a-fA-F]{2})', replace, quote(value, safe='/%*$' + _UNRESERVED + "!&'()+,;=:@"))


class Robots:
    def __init__(self, text):
        groups, agents, rules = [], [], []
        self.sitemaps, self.delay = [], 0.0
        for raw in text.splitlines():
            line = raw.split('#', 1)[0].strip()
            if ':' not in line:
                continue
            key, value = (part.strip() for part in line.split(':', 1))
            key = key.lower()
            if key == 'user-agent':
                if rules:
                    groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(value.lower())
            elif key in ('allow', 'disallow', 'crawl-delay') and agents:
                rules.append((key, value))
            elif key == 'sitemap' and len(self.sitemaps) < MAX_SITEMAPS:
                self.sitemaps.append(value)
        groups.append((agents, rules))
        selected = [rules for agents, rules in groups if AGENT.lower() in agents]
        if not selected:
            selected = [rules for agents, rules in groups if '*' in agents]
        self.rules = []
        for rules in selected:
            for key, value in rules:
                if key == 'crawl-delay':
                    try:
                        delay = float(value)
                        if not 0 <= delay <= 10:
                            raise ValueError()
                        self.delay = max(self.delay, delay)
                    except ValueError:
                        raise CrawlError('invalid_robots') from None
                elif value:
                    if not value.startswith('/') or len(value) > 2048 or len(self.rules) >= 2048:
                        raise CrawlError('invalid_robots')
                    value = _robots_path(value)
                    self.rules.append((value, key == 'allow', len(value.replace('*', '').rstrip('$').encode())))

    def allowed(self, url):
        path = _robots_path(urlsplit(url).path)
        matches = [(length, allow) for pattern, allow, length in self.rules if _glob_prefix(path, pattern)]
        return max(matches)[1] if matches else True


class DocumentationHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.main_parts, self.title_parts, self.links = [], [], [], []
        self.stack, self.main_depth, self.pre, self.title = [], 0, 0, False
        self.password, self.noindex, self.refresh = False, False, False

    def output(self, text):
        self.parts.append(text)
        if self.main_depth:
            self.main_parts.append(text)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        # Navigation/footer text is boilerplate, but its visible documentation
        # links still participate in bounded, scoped traversal.
        hidden = 1 if tag in ('nav', 'footer') else 0
        if tag in ('script', 'style', 'svg', 'noscript', 'template') or 'hidden' in attrs or (attrs.get('aria-hidden') or '').lower() == 'true':
            hidden = 3
        self.stack.append((tag, hidden)) if tag not in ('meta', 'link', 'br', 'hr', 'img', 'input', 'source', 'area', 'base', 'wbr', 'embed', 'param', 'col') else None
        if tag == 'title': self.title = True
        if tag in ('main', 'article'): self.main_depth += 1
        if tag == 'input' and attrs.get('type', '').lower() == 'password': self.password = True
        if tag == 'meta':
            if attrs.get('name', '').lower() in ('robots', AGENT.lower()) and re.search(r'\b(?:noindex|none)\b', attrs.get('content', ''), re.I): self.noindex = True
            if attrs.get('http-equiv', '').lower() == 'refresh': self.refresh = True
        if tag == 'a' and attrs.get('href') and not any(hidden & 2 for _, hidden in self.stack) and len(self.links) < MAX_DISCOVERED:
            self.links.append(attrs['href'])
        if any(hidden for _, hidden in self.stack): return
        if re.fullmatch('h[1-6]', tag): self.output('\n\n' + '#' * int(tag[1]) + ' ')
        elif tag == 'pre':
            self.pre += 1
            self.output('\n```\n')
        elif tag == 'li': self.output('\n- ')
        elif tag in ('p', 'div', 'section', 'br', 'tr'): self.output('\n')
        elif tag in ('td', 'th'): self.output(' | ')

    def handle_endtag(self, tag):
        hidden = any(value for _, value in self.stack)
        if not hidden:
            if tag == 'pre' and self.pre:
                self.pre -= 1
                self.output('\n```\n')
            elif re.fullmatch('h[1-6]', tag) or tag in ('p', 'section', 'li', 'tr'): self.output('\n')
        if tag == 'title': self.title = False
        if tag in ('main', 'article') and self.main_depth: self.main_depth -= 1
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.title: self.title_parts.append(data)
        if self.title or any(hidden for _, hidden in self.stack): return
        self.output(data if self.pre else re.sub(r'\s+', ' ', data))


def extract_html(raw, content_type):
    parser = DocumentationHTML()
    try:
        parser.feed(_text(raw, content_type))
        parser.close()
    except CrawlError:
        raise
    except Exception:
        raise CrawlError('invalid_text') from None
    title = ' '.join(''.join(parser.title_parts).split())[:300]
    if parser.password or title.casefold() in ('login', 'log in', 'sign in', 'sign-in'):
        raise CrawlError('login_page')
    if parser.refresh or any(x in title.casefold() for x in ('just a moment', 'attention required', 'access denied', 'verify you are human', 'checking your browser')):
        raise CrawlError('challenge_page')
    if parser.noindex:
        raise CrawlError('robots_denied')
    content = ''.join(parser.main_parts or parser.parts).strip()
    error_title = re.compile(r'^(?:(?:404|500|502|503|504)\s*[-:|]?\s*)?(?:page not found|not found|internal server error|service unavailable|bad gateway|gateway timeout)(?:\s*[-:|].*)?$', re.I)
    if error_title.fullmatch(title) or error_title.fullmatch(content.split('\n', 1)[0].lstrip('# ').strip()):
        raise CrawlError('error_page')
    if not content or len(re.sub(r'\W', '', content)) < 20:
        raise CrawlError('empty_page')
    if any(ord(c) < 32 and c not in '\n\r\t' for c in content):
        raise CrawlError('invalid_text')
    if len(content.encode('utf-8')) > MAX_TEXT_BYTES:
        raise CrawlError('text_limit')
    return title, content, parser.links


def sitemap_urls(raw):
    try:
        # Sitemaps are UTF-8. Decode first so UTF-16 cannot hide a DTD/entity
        # declaration from the explicit ban, even with an alternate XML BOM.
        text = raw.decode('utf-8-sig')
        declaration = re.search(r'<\?xml[^>]*encoding\s*=\s*["\']([^"\']+)', text, re.I)
        if '\x00' in text or re.search(r'<!\s*(?:DOCTYPE|ENTITY)', text, re.I) or (declaration and declaration[1].lower() not in ('utf-8', 'utf8')):
            raise ValueError()
        root = ElementTree.fromstring(text)
        kind = root.tag.rsplit('}', 1)[-1]
        if kind not in ('urlset', 'sitemapindex'):
            raise ValueError()
        locations = [node.text.strip() for node in root.iter() if node.tag.rsplit('}', 1)[-1] == 'loc' and node.text]
        if len(locations) > MAX_DISCOVERED:
            raise ValueError()
        return kind, locations
    except (ElementTree.ParseError, ValueError, UnicodeError):
        raise CrawlError('invalid_sitemap') from None


def crawl(config, *, request=public_request, on_page=None, cancelled=None, timeout=MAX_SECONDS):
    """Synchronous engine. Production always invokes it through supervised_crawl."""
    config = validate_crawl_config(config)
    if type(timeout) not in (int, float) or not 0 < timeout <= MAX_SECONDS:
        raise CrawlError('timeout')
    deadline, budget = time.monotonic() + timeout, [0]
    rules, denied_origins, last_fetch, sitemaps = {}, set(), {}, deque()
    queue, discovered, visited_maps = deque([(config['seed_url'], 0)]), {config['seed_url']}, set()
    reported = set()
    count = text_bytes = fetched = 0
    complete, first_error, first_skip, stop_reason = True, None, None, None

    def checkpoint(): _checkpoint(cancelled, deadline)
    def emit(url, depth, state, *, error=None, title='', content='', size=0):
        # A sitemap may also appear in document links. Its first outcome is
        # immutable; a later metadata failure must not conflict with it.
        if url in reported: return
        reported.add(url)
        if on_page:
            on_page({'url': url, 'depth': depth, 'state': state, 'error': error,
                     'title': title, 'content': content, 'byte_count': size})
    def enqueue(value, base, depth):
        nonlocal complete, first_error
        try: url = canonical_url(value, base)
        except CrawlError: return
        if not in_scope(url, config) or url in discovered or url in reported: return
        if re.search(r'\.(?:pdf|png|jpe?g|gif|svg|webp|zip|mp[34]|css|js|woff2?)$', urlsplit(url).path, re.I): return
        if depth > config['max_depth']:
            # Depth is an explicit scope boundary, not a failed requested page.
            return
        if len(discovered) >= MAX_DISCOVERED:
            complete, first_error = False, first_error or 'discovery_limit'
            return
        discovered.add(url)
        queue.append((url, depth))
    def fetch(url, maximum, types, missing=False):
        checkpoint()
        host = origin(url)
        delay = rules[host].delay if host in rules else 0
        while time.monotonic() < last_fetch.get(host, 0) + delay:
            checkpoint()
            time.sleep(.05)
        with request(url) as response:
            last_fetch[host] = time.monotonic()
            if response.status == 404 and missing: return None, ''
            if 300 <= response.status < 400: raise CrawlError('redirect_refused')
            if response.status != 200: raise CrawlError('http_failed')
            content_type = response.getheader('Content-Type', '')
            if content_type.split(';')[0].strip().lower() not in types: raise CrawlError('unexpected_type')
            if re.search(r'\b(?:noindex|none)\b', response.getheader('X-Robots-Tag', ''), re.I): raise CrawlError('robots_denied')
            return _read(response, maximum, budget, cancelled, deadline), content_type
    def robots_for(url):
        host = origin(url)
        if host in rules: return rules[host]
        if host in denied_origins: raise CrawlError('robots_unavailable')
        try:
            raw, kind = fetch(host + '/robots.txt', MAX_ROBOTS_BYTES, {'text/plain'}, missing=True)
            policy = Robots('' if raw is None else _text(raw, kind))
        except CrawlError as exc:
            if exc.code in ('cancelled', 'timeout', 'byte_limit', 'total_text_limit'): raise
            denied_origins.add(host)
            raise CrawlError('robots_unavailable') from None
        rules[host] = policy
        for address in policy.sitemaps or [host + '/sitemap.xml']:
            try: address = canonical_url(address)
            except CrawlError: continue
            if origin(address) == host and metadata_scope(address, config) and len(sitemaps) < MAX_SITEMAPS:
                sitemaps.append(address)
        return policy

    while queue or sitemaps:
        if count >= config['max_pages']:
            stop_reason = 'page_limit'
            break
        checkpoint()
        if queue:
            url, depth = queue.popleft()
            count += 1
            try:
                if not robots_for(url).allowed(url): raise CrawlError('robots_denied')
                raw, kind = fetch(url, MAX_HTML_BYTES, {'text/html', 'application/xhtml+xml'})
                title, content, links = extract_html(raw, kind)
                text_bytes += len(content.encode())
                if text_bytes > MAX_TOTAL_TEXT: raise CrawlError('total_text_limit')
                emit(url, depth, 'succeeded', title=title or urlsplit(url).path, content=content, size=len(raw))
                fetched += 1
                if count < config['max_pages']:
                    for link in links: enqueue(link, url, depth + 1)
                else:
                    stop_reason = 'page_limit'
            except CrawlError as exc:
                if exc.code in ('cancelled', 'timeout', 'byte_limit', 'total_text_limit'): raise
                first_skip = first_skip or exc.code
                emit(url, depth, 'skipped', error=exc.code)
        else:
            address = sitemaps.popleft()
            if address in visited_maps: continue
            if len(visited_maps) >= MAX_SITEMAPS:
                complete, first_error = False, first_error or 'discovery_limit'
                break
            visited_maps.add(address)
            try:
                policy = robots_for(address)
                if not policy.allowed(address): continue
                raw, _ = fetch(address, MAX_XML_BYTES, {'application/xml', 'text/xml'}, missing=True)
                if raw is None: continue
                kind, locations = sitemap_urls(raw)
                for location in locations:
                    if kind == 'urlset': enqueue(location, address, 1)
                    else:
                        try: normalized = canonical_url(location)
                        except CrawlError: continue
                        if origin(normalized) == origin(address) and metadata_scope(normalized, config) and normalized not in visited_maps and len(sitemaps) < MAX_SITEMAPS:
                            sitemaps.append(normalized)
            except CrawlError as exc:
                if exc.code in ('cancelled', 'timeout', 'byte_limit', 'total_text_limit'): raise
                first_skip = first_skip or exc.code
                emit(address, 0, 'skipped', error=exc.code)
    usable = complete and fetched > 0
    return {'complete': usable, 'error': None if usable else (first_error or first_skip or 'empty_page'),
            'stop_reason': stop_reason if usable else None, 'pages': count, 'bytes': budget[0]}


def _crawl_child(connection, config, timeout):
    with open(os.devnull, 'w') as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    try:
        result = crawl(config, on_page=lambda page: connection.send(('page', page)), timeout=timeout)
        connection.send(('done', result))
    except CrawlError as exc:
        connection.send(('error', exc.code))
    except Exception:
        connection.send(('error', 'crawl_failed'))
    finally:
        connection.close()


def supervised_crawl(config, *, on_page, cancelled=None, timeout=MAX_SECONDS, _target=_crawl_child):
    if type(timeout) not in (int, float) or not 0 < timeout <= MAX_SECONDS:
        raise CrawlError('timeout')
    deadline = time.monotonic() + timeout
    _checkpoint(cancelled, deadline)
    ctx = multiprocessing.get_context('spawn')
    reader, writer = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_target, args=(writer, config, timeout), daemon=True)
    try:
        process.start()
        writer.close()
        while True:
            _checkpoint(cancelled, deadline)
            if reader.poll(.1):
                try: message = reader.recv()
                except EOFError: raise CrawlError('crawl_failed') from None
                _checkpoint(cancelled, deadline)
                if message[0] == 'page': on_page(message[1])
                elif message[0] == 'done': return message[1]
                elif message[0] == 'error': raise CrawlError(message[1])
                else: raise CrawlError('crawl_failed')
            elif not process.is_alive(): raise CrawlError('crawl_failed')
    finally:
        writer.close()
        reader.close()
        if process.pid is not None:
            if process.is_alive(): process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
            process.close()


class CrawlWorker:
    """One recoverable crawl at a time; never takes ingestion/generation jobs."""
    def __init__(self, storage, *, runner=supervised_crawl):
        self.storage, self.runner = storage, runner
        self._stop, self._wake = threading.Event(), threading.Event()
        self._work_lock, self._thread = threading.Lock(), None

    def wake(self):
        self._wake.set()

    def run_once(self):
        if not self._work_lock.acquire(blocking=False):
            return False
        try:
            job = self.storage.ai_claim_job(time.time(), lease_seconds=300, kinds=('crawl',))
            if not job:
                return False
            jid, token, renewed = job['id'], job['lease_token'], 0.0
            def cancelled():
                nonlocal renewed
                if self._stop.is_set(): return True
                if time.monotonic() - renewed >= 1:
                    if not self.storage.ai_renew_job(jid, token, lease_seconds=300): return True
                    renewed = time.monotonic()
                return False
            def record(page):
                if cancelled(): raise CrawlError('cancelled')
                outcome = {key: page[key] for key in ('url', 'title', 'content', 'error')}
                outcome['status'] = 'fetched' if page['state'] == 'succeeded' else ('skipped' if page['state'] == 'skipped' else 'failed')
                if not self.storage.ai_crawl_record(jid, token, outcome): raise CrawlError('cancelled')
            try:
                material = self.storage.ai_job_material(jid, token)
                if not material or 'crawl' not in material:
                    self.storage.ai_finish_job(jid, token, 'failed', 'source_failed')
                    return True
                result = self.runner(material['crawl']['config'], on_page=record, cancelled=cancelled)
                self.storage.ai_crawl_finish(jid, token, result['complete'], result['error'], stop_reason=result.get('stop_reason'))
            except Exception as exc:
                if not self._stop.is_set():
                    code = exc.code if isinstance(exc, CrawlError) else 'crawl_failed'
                    self.storage.ai_crawl_finish(jid, token, False, code)
            return True
        finally:
            self._work_lock.release()

    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        def loop():
            while not self._stop.is_set():
                try: busy = self.run_once()
                except Exception: busy = False  # No raw network/source exceptions in logs.
                if not busy:
                    self._wake.wait(1)
                    self._wake.clear()
        self._thread = threading.Thread(target=loop, name='knowledge-crawler', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread: self._thread.join(timeout=5)


class CrawlService:
    def __init__(self, storage, worker):
        self.storage, self.worker = storage, worker

    @staticmethod
    def _admin(principal):
        if not isinstance(principal, dict) or principal.get('kind') != 'admin' or not principal.get('user_id') or not principal.get('session_id'):
            raise Error(403, 'forbidden', 'Native administrator access required')

    def begin(self, project, principal, client_id, name, config, *, source_id=None, version=None):
        self._admin(principal)
        config = validate_crawl_config(config)
        if not isinstance(name, str) or not name.strip() or len(name) > 300:
            raise Error(400, 'invalid_input', 'Give this website source a name')
        result = self.storage.ai_crawl_begin(project, principal, client_id, name, config, source_id=source_id, version=version)
        self.worker.wake()
        return result

    def runs(self, project, principal, source_id, limit=25, offset=0):
        self._admin(principal)
        return self.storage.ai_crawl_runs(project, principal, source_id, limit, offset)

    def run(self, project, principal, source_id, run_id):
        self._admin(principal)
        return self.storage.ai_crawl_run(project, principal, source_id, run_id)

    def cancel(self, project, principal, source_id, run_id, version):
        self._admin(principal)
        result = self.storage.ai_crawl_cancel(project, principal, source_id, run_id, version)
        self.worker.wake()
        return result
