"""Signed HTTP transport. Pin DNS resolution to the validated address per attempt."""
import hashlib
import hmac
import http.client
import ipaddress
import logging
import random
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit
from feedback.errors import Error

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 8
TIMEOUT = 10


def signature(secret, timestamp, body):
    return hmac.new(secret.encode(), str(timestamp).encode() + b'.' + body, hashlib.sha256).hexdigest()


def destination(url, allow_internal=False):
    try:
        u = urlsplit(url)
        if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.fragment or len(url) > 2048:
            raise ValueError()
        port = u.port or (443 if u.scheme == 'https' else 80)
        addresses = sorted({x[4][0] for x in socket.getaddrinfo(u.hostname, port, type=socket.SOCK_STREAM)})
        if not addresses:
            raise ValueError()
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            # Metadata remains blocked even with internal endpoints explicitly enabled.
            if str(ip) == '100.100.100.200' or ip.is_link_local or ip.is_unspecified or ip.is_multicast or (not allow_internal and not ip.is_global):
                raise ValueError()
        # Validate the entire answer set before choosing a connection address.
        # Prefer IPv4 when both families are advertised: container networks may
        # resolve IPv6 successfully without having an IPv6 route. IPv6-only
        # destinations remain supported, with no re-resolution or fallback.
        address = min(addresses, key=lambda value: (ipaddress.ip_address(value).version, value))
        return u, address, port
    except (ValueError, OSError) as exc:
        raise Error(400, 'invalid_webhook_url', 'Webhook destination is invalid or resolves to a blocked network') from exc


class PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, hostname, address, port, tls):
        super().__init__(hostname, port, timeout=TIMEOUT)
        self.address, self.tls = address, tls

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)
        self.sock.settimeout(self.timeout)
        if self.tls:
            self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)


def deliver(url, headers, body, allow_internal):
    u, address, port = destination(url, allow_internal)
    conn = PinnedHTTP(u.hostname, address, port, u.scheme == 'https')
    try:
        conn.request('POST', (u.path or '/') + ('?' + u.query if u.query else ''), body, headers)
        response = conn.getresponse()
        # No response body is retained; malicious endpoints cannot inject secrets into diagnostics.
        return response.status
    finally:
        conn.close()


class WebhookWorker:
    def __init__(self, storage, vault, allow_internal=False, transport=deliver):
        self.storage, self.vault, self.allow_internal, self.transport = storage, vault, allow_internal, transport
        self.stop_event = threading.Event()
        self.thread = None

    def once(self, current_time=None):
        current_time = time.time() if current_time is None else current_time
        job = self.storage.claim_delivery(current_time)
        if not job:
            return False
        body = job['payload'].encode()
        timestamp = int(time.time())
        state, status, error = 'failed', None, None
        try:
            secret = self.vault.reveal(job['signing_secret'])
            headers = {'Content-Type': 'application/json', 'User-Agent': 'SuggestionBox/0.1',
                       'X-Feedback-Event-ID': job['event_id'], 'X-Feedback-Timestamp': str(timestamp),
                       'X-Feedback-Signature': 'v1=' + signature(secret, timestamp, body)}
            status = self.transport(job['destination'], headers, body, self.allow_internal)
            if 200 <= status < 300:
                state = 'succeeded'
            elif status in (408, 425, 429) or status >= 500:
                state = 'queued'
                error = f'Temporary HTTP failure ({status})'
            else:
                error = f'Permanent HTTP failure ({status}); redirects are not followed'
        except Error:
            error = 'Destination validation failed'
        except Exception:
            state, error = 'queued', 'Network, TLS, or signing configuration failure'
        if state == 'queued' and job['attempt_count'] >= MAX_ATTEMPTS:
            state, error = 'failed', 'Retry limit reached: ' + (error or 'temporary failure')
        backoff = min(3600, 5 * 2 ** (job['attempt_count'] - 1)) * random.uniform(0.75, 1.25)
        self.storage.finish_delivery(job['id'], job['lease_token'], state, status, error, time.time() + backoff)
        return True

    def start(self):
        def loop():
            while not self.stop_event.is_set():
                try:
                    busy = self.once()
                except Exception:
                    # Never log raw database/HTTP exceptions or secrets.
                    log.error('Webhook worker could not process a delivery; will retry')
                    busy = False
                if not busy:
                    self.stop_event.wait(1)
        self.thread = threading.Thread(target=loop, name='webhook-worker', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=15)
