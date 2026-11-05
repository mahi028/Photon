"""Fetch an image from a user-supplied URL without becoming an SSRF proxy.

Used by POST /api/upload ({"url": ...}) and the MCP upload_image tool. Both
take URLs from callers we don't fully trust (a browser user, or a remote LLM
that may be prompt-injected), so:

  * only http/https;
  * every hop's host must resolve exclusively to public addresses — loopback,
    private (RFC 1918), link-local (incl. cloud metadata 169.254.169.254),
    CGNAT, multicast and reserved ranges are refused. Redirects are followed
    manually (max 3) so each hop is checked;
  * the body is streamed and capped at MAX_UPLOAD_BYTES.

PHOTON_ALLOW_PRIVATE_URLS=1 disables the address check (e.g. to pull from an
internal image server). There is a small DNS-rebinding window between our
resolve and requests' own resolve; acceptable for this threat model.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urljoin, urlsplit

import requests

from ...config import config

_MAX_REDIRECTS = 3
_TIMEOUT = 30


class FetchError(Exception):
    pass


def _allow_private() -> bool:
    return os.getenv("PHOTON_ALLOW_PRIVATE_URLS", "").lower() in ("1", "true", "yes")


def _check_host(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchError("URL must be http(s).")
    host = parts.hostname
    if not host:
        raise FetchError("URL has no host.")
    if _allow_private():
        return
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError(f"Cannot resolve host '{host}': {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise FetchError(f"Refusing to fetch from non-public address {ip} ('{host}').")


def fetch_url(url: str) -> tuple[bytes, str]:
    """Return (body, filename-from-final-URL). Raises FetchError."""
    cap = config.MAX_UPLOAD_BYTES
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        _check_host(current)
        try:
            resp = requests.get(current, timeout=_TIMEOUT, stream=True, allow_redirects=False,
                                headers={"User-Agent": "Photon/1.0 (image fetch)"})
        except requests.RequestException as e:
            raise FetchError(f"Failed to fetch URL: {e}")
        with resp:
            if resp.is_redirect:
                loc = resp.headers.get("Location")
                if not loc:
                    raise FetchError("Redirect without Location header.")
                current = urljoin(current, loc)
                continue
            if resp.status_code >= 400:
                raise FetchError(f"Failed to fetch URL: HTTP {resp.status_code}")
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > cap:
                raise FetchError(f"Remote file too large (> {config.MAX_UPLOAD_MB} MB).")
            buf = bytearray()
            for chunk in resp.iter_content(64 * 1024):
                buf += chunk
                if len(buf) > cap:
                    raise FetchError(f"Remote file too large (> {config.MAX_UPLOAD_MB} MB).")
        name = urlsplit(current).path.rstrip("/").split("/")[-1] or "download"
        return bytes(buf), name
    raise FetchError(f"Too many redirects (> {_MAX_REDIRECTS}).")
