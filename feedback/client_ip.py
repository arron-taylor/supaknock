"""Validated connection metadata, after the ASGI server's trusted-proxy handling."""
from ipaddress import ip_address


def client_ip(connection) -> str | None:
    # Never inspect forwarding headers here. Uvicorn only rewrites the peer for
    # explicitly trusted proxies. Hostnames, ports and IPv6 zones are not IPs.
    host = connection.client.host if connection.client else None
    if not isinstance(host, str) or '%' in host:
        return None
    try:
        return str(ip_address(host))
    except ValueError:
        return None
