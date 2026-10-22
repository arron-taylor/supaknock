"""Shared pinned transport: DNS-family preference must not weaken validation."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading

import pytest

from feedback.ai_crawler import public_request
from feedback.ai_download import ModelDownloadError, model_request
from feedback.errors import Error
from feedback.webhooks import PinnedHTTP, deliver, destination


PUBLIC_V4 = '93.184.216.34'
PUBLIC_V6 = '2606:4700:4700::1111'


def answer(address, port=443):
    family = socket.AF_INET6 if ':' in address else socket.AF_INET
    sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return family, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', sockaddr


@pytest.mark.parametrize('addresses', [(PUBLIC_V6, PUBLIC_V4), (PUBLIC_V4, PUBLIC_V6)])
def test_dual_stack_chooses_public_ipv4_independent_of_dns_order(monkeypatch, addresses):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [answer(ip) for ip in addresses])
    parsed, selected, port = destination('https://model.example/file')
    assert (parsed.hostname, selected, port) == ('model.example', PUBLIC_V4, 443)


def test_ipv6_only_remains_supported_and_stable(monkeypatch):
    addresses = ['2606:4700:4700::1111', '2001:4860:4860::8888']
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [answer(ip) for ip in addresses])
    assert destination('https://v6.example/')[1] == min(addresses)


@pytest.mark.parametrize(('blocked', 'internal'), [
    ('127.0.0.1', False), ('10.0.0.1', False), ('::1', False),
    ('::ffff:127.0.0.1', False), ('fe80::1', True),
    ('169.254.169.254', True), ('100.100.100.200', True),
])
def test_preferred_public_ipv4_cannot_hide_any_prohibited_answer(monkeypatch, blocked, internal):
    monkeypatch.setattr(socket, 'getaddrinfo',
                        lambda *a, **kw: [answer(PUBLIC_V4), answer(PUBLIC_V6), answer(blocked)])
    with pytest.raises(Error) as error:
        destination('https://mixed.example/', allow_internal=internal)
    assert error.value.code == 'invalid_webhook_url'


@pytest.mark.parametrize('transport', ['download', 'crawl'])
@pytest.mark.parametrize('addresses', [(PUBLIC_V6, PUBLIC_V4), (PUBLIC_V6,)])
def test_model_and_crawler_transports_use_selected_address_and_original_host(monkeypatch, transport, addresses):
    calls, lookups = [], []
    def resolve(host, port, **kwargs):
        lookups.append((host, port))
        return [answer(ip, port) for ip in addresses]
    class Connection:
        def __init__(self, *args): calls.append(args)
        def request(self, method, path, *args, **kwargs): calls.append((method, path))
        def getresponse(self): return object()
        def close(self): calls.append('closed')
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    module = 'feedback.ai_download' if transport == 'download' else 'feedback.ai_crawler'
    monkeypatch.setattr(module + '.PinnedHTTP', Connection)
    hostname = 'huggingface.co' if transport == 'download' else 'docs.example'
    request = model_request('GET', 'https://huggingface.co/model?version=1', {}) if transport == 'download' else public_request('https://docs.example/help')
    with request:
        pass
    assert lookups == [(hostname, 443)]
    assert calls[0] == (hostname, PUBLIC_V4 if PUBLIC_V4 in addresses else PUBLIC_V6, 443, True)
    assert calls[-1] == 'closed'


def test_model_download_refuses_mixed_answer_before_constructing_connection(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo',
                        lambda *a, **kw: [answer(PUBLIC_V4), answer('::ffff:127.0.0.1')])
    monkeypatch.setattr('feedback.ai_download.PinnedHTTP',
                        lambda *a: pytest.fail('Blocked answer set must not construct a connection'))
    with pytest.raises(ModelDownloadError, match='network_failed'):
        with model_request('GET', 'https://huggingface.co/model', {}):
            pytest.fail('Blocked DNS must not yield a response')


def test_selected_address_keeps_original_tls_server_name(monkeypatch):
    connected, tls = [], []
    class Sock:
        def settimeout(self, timeout): pass
    sock = Sock()
    def connect(address, timeout):
        connected.append(address)
        return sock
    class Context:
        def wrap_socket(self, connection, *, server_hostname):
            tls.append((connection, server_hostname))
            return connection
    monkeypatch.setattr(socket, 'create_connection', connect)
    monkeypatch.setattr('feedback.webhooks.ssl.create_default_context', Context)
    connection = PinnedHTTP('huggingface.co', PUBLIC_V4, 443, True)
    connection.connect()
    assert connected == [(PUBLIC_V4, 443)]
    assert tls == [(sock, 'huggingface.co')]


def test_real_local_dns_and_http_delivery_keep_host_header():
    received = []
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.headers['Host'], self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(204)
            self.end_headers()
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f'http://localhost:{server.server_port}/hook'
        assert destination(url, allow_internal=True)[1] == '127.0.0.1'
        assert deliver(url, {'Content-Type': 'application/json'}, b'{}', True) == 204
        assert received == [(f'localhost:{server.server_port}', b'{}')]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
