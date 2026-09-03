"""Resolve hostnames through DNS-over-HTTPS when the system resolver cannot.

Home routers sometimes fail Alchemy and other RPC lookups (hang or
SERVFAIL) even though the machines themselves are reachable. Queries go to
Cloudflare at 1.1.1.1 by IP, so this path does not need working local DNS.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import ssl
import threading
import time
from urllib.parse import urlencode


BOOTSTRAP_IPS = ("1.1.1.1", "1.0.0.1")
_DOH_SERVER_NAME = "cloudflare-dns.com"
_installed = False
_orig_getaddrinfo = None
_cache = {}
_lock = threading.Lock()


def _is_ip(host):
    text = str(host or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def _doh_a_record(name, timeout=3.0):
    """Return ``(ipv4, ttl)`` for ``name``, or ``(None, 0)`` on failure."""
    query = "/dns-query?" + urlencode({"name": name, "type": "A"})
    request = (
        f"GET {query} HTTP/1.1\r\n"
        f"Host: {_DOH_SERVER_NAME}\r\n"
        "Accept: application/dns-json\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    ctx = ssl.create_default_context()
    for ip in BOOTSTRAP_IPS:
        sock = None
        try:
            sock = socket.create_connection((ip, 443), timeout=timeout)
            ssock = ctx.wrap_socket(sock, server_hostname=_DOH_SERVER_NAME)
            sock = None
            try:
                ssock.sendall(request)
                data = b""
                while True:
                    chunk = ssock.recv(8192)
                    if not chunk:
                        break
                    data += chunk
            finally:
                ssock.close()
            _header, separator, body = data.partition(b"\r\n\r\n")
            if not separator:
                continue
            payload = json.loads(body.decode("utf-8"))
            for item in payload.get("Answer") or []:
                if int(item.get("type") or 0) != 1:
                    continue
                address = str(item.get("data") or "").strip()
                if not address:
                    continue
                try:
                    ttl = int(item.get("TTL") or 60)
                except (TypeError, ValueError):
                    ttl = 60
                return address, ttl
        except Exception:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            continue
    return None, 0


def resolve(host):
    """Return a cached IPv4 address for ``host``, or None."""
    host = str(host or "").strip().rstrip(".").lower()
    if not host:
        return None
    if _is_ip(host):
        return host
    now = time.monotonic()
    with _lock:
        hit = _cache.get(host)
        if hit and hit[1] > now:
            return hit[0]
    address, ttl = _doh_a_record(host)
    if not address:
        return None
    with _lock:
        _cache[host] = (address, now + max(30, min(int(ttl), 300)))
    return address


def install():
    """Route ``socket.getaddrinfo`` through DNS-over-HTTPS. Safe to call twice."""
    global _installed, _orig_getaddrinfo
    if _installed:
        return
    _orig_getaddrinfo = socket.getaddrinfo

    def wrapped(host, port, family=0, type=0, proto=0, flags=0):
        hostname = host.decode("idna") if isinstance(host, (bytes, bytearray)) else host
        if hostname and not _is_ip(hostname) and hostname not in BOOTSTRAP_IPS:
            address = resolve(hostname)
            if address:
                try:
                    return _orig_getaddrinfo(address, port, family, type, proto, flags)
                except socket.gaierror:
                    pass
        return _orig_getaddrinfo(host, port, family, type, proto, flags)

    socket.getaddrinfo = wrapped
    _installed = True
