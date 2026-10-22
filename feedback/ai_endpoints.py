"""Admin-selected API endpoints: strict URLs, pinned DNS and no redirects."""
import asyncio
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

import httpx

from feedback.ai_providers import ProviderError, _request_json, _identifier, _json_bytes, _json_loads
from feedback.webhooks import destination


def endpoint_url(value, *, allow_internal=False):
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 1024 or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
            raise ValueError()
        u = urlsplit(value)
        if (u.scheme not in (('https', 'http') if allow_internal else ('https',)) or not u.hostname
                or u.username is not None or u.password is not None or u.query or u.fragment
                or '\\' in value or '%' in value or not 1 <= (u.port or 443) <= 65535
                or any(p in ('.', '..') for p in u.path.split('/')) or '//' in u.path):
            raise ValueError()
        host = u.hostname.lower().rstrip('.')
        if not re.fullmatch(r'[a-z0-9.:-]+', host):
            raise ValueError()
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            if not allow_internal:
                raise ValueError()
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            ip = getattr(ip, 'ipv4_mapped', None) or ip
            if ip.is_link_local or ip.is_unspecified or ip.is_multicast or str(ip) == '100.100.100.200' or (not allow_internal and not ip.is_global):
                raise ValueError()
        authority = '[' + host + ']' if ':' in host else host
        if u.port:
            authority += ':' + str(u.port)
        return urlunsplit((u.scheme, authority, u.path.rstrip('/'), '', ''))
    except (ValueError, TypeError):
        raise ProviderError('invalid_endpoint') from None


def custom_credentials(key, base_url, models, *, allow_internal=False):
    if not isinstance(key, str) or (key and not _identifier(key, 4096)):
        raise ProviderError('invalid_credentials')
    base = endpoint_url(base_url, allow_internal=allow_internal)
    if not isinstance(models, list) or not 1 <= len(models) <= 100 or any(not _identifier(m) for m in models) or len(set(models)) != len(models):
        raise ProviderError('invalid_models')
    return _json_bytes({'key': key, 'base_url': base, 'models': sorted(models)}, 8192).decode()


def unpack_credentials(value, *, allow_internal=False):
    try:
        data = _json_loads(value)
        if not isinstance(data, dict) or set(data) != {'key', 'base_url', 'models'}:
            raise ProviderError('invalid_credentials')
        custom_credentials(data['key'], data['base_url'], data['models'], allow_internal=allow_internal)
        return data
    except (ProviderError, TypeError):
        raise ProviderError('invalid_credentials') from None


async def endpoint_request(transport, timeout, method, base_url, path, key, body=None, *, allow_internal=False):
    base = endpoint_url(base_url, allow_internal=allow_internal)
    url = base + path
    headers = {'Accept': 'application/json', 'Accept-Encoding': 'identity', 'Content-Type': 'application/json', 'User-Agent': 'SupaKnock/1.0'}
    if key:
        if not _identifier(key, 4096):
            raise ProviderError('invalid_credentials')
        headers['Authorization'] = 'Bearer ' + key
    extensions = None
    if transport is None:
        try:
            u, address, port = await asyncio.wait_for(asyncio.to_thread(destination, url, allow_internal), 5)
            target = httpx.URL(url).copy_with(host=address, port=port)
            headers['Host'] = u.netloc
            extensions = {'sni_hostname': u.hostname.encode('ascii')}
            url = target
        except Exception:
            raise ProviderError('blocked_endpoint') from None
    return await _request_json(transport, timeout, method, url, headers, body, extensions=extensions)
