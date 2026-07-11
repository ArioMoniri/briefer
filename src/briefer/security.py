"""Security primitives.

The threat model: the bot ingests fully-untrusted content (message text,
URLs, uploaded files). None of it must ever reach a shell, and outbound
fetches must not be usable to reach the server's own network (SSRF).
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import socket
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Password hashing (in-memory only; we never persist the password).
# ---------------------------------------------------------------------------

def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return salt.hex(), dk.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    salt = bytes.fromhex(salt_hex)
    _, candidate = hash_password(password, salt)
    return hmac.compare_digest(candidate, hash_hex)


# ---------------------------------------------------------------------------
# Per-chat token-bucket rate limiting.
# ---------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = max(1, per_minute)
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, chat_id: int) -> bool:
        now = time.monotonic()
        window = self._hits[chat_id]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.per_minute:
            return False
        window.append(now)
        return True


# ---------------------------------------------------------------------------
# SSRF-safe URL validation. Reject anything that resolves to a private,
# loopback, link-local, or reserved address before we ever fetch it.
# ---------------------------------------------------------------------------

_ALLOWED_SCHEMES = {"http", "https"}


def _is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def safe_resolve(url: str) -> tuple[bool, str, str | None]:
    """Validate a URL and resolve it to a single pinned public IP.

    Returns (ok, reason, ip). The caller MUST connect to `ip` directly
    (not re-resolve the hostname) to avoid a DNS-rebinding TOCTOU: we do
    exactly one resolution here and hand back the concrete address.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "unparseable URL", None
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return False, f"scheme '{parsed.scheme}' not allowed", None
    host = parsed.hostname
    if not host:
        return False, "no host", None
    # Only allow the standard web ports; block SSRF to services on odd ports.
    if parsed.port is not None and parsed.port not in (80, 443):
        return False, f"port {parsed.port} not allowed", None
    # Reject obvious internal names outright.
    lowered = host.lower()
    if lowered in {"localhost", "metadata.google.internal"} or lowered.endswith(
        (".local", ".internal")
    ):
        return False, "internal hostname", None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False, "DNS resolution failed", None
    pinned: str | None = None
    for info in infos:
        ip = info[4][0]
        if not _is_public_ip(ip):
            return False, f"resolves to non-public address {ip}", None
        if pinned is None:
            pinned = ip
    if pinned is None:
        return False, "no address", None
    return True, "ok", pinned


def is_safe_url(url: str) -> tuple[bool, str]:
    """Return (ok, reason). Resolves the host and rejects private targets."""
    ok, reason, _ = safe_resolve(url)
    return ok, reason


# ---------------------------------------------------------------------------
# Text hardening.
# ---------------------------------------------------------------------------

def sanitize_for_log(text: str, limit: int = 200) -> str:
    text = text.replace("\n", " ").replace("\r", " ")
    return text[:limit]


def clamp(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"
