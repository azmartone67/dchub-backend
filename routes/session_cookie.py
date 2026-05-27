"""
Phase r43-G (2026-05-27) — Signed session cookie for browser-frontend bypass.

Replaces the forgeable Referer-string match in require_plan with a server-
issued HMAC-signed cookie. Browser sessions from dchub.cloud get the
cookie automatically; scrapers running `curl -H "Referer: dchub.cloud"`
can no longer forge their way past the gate.

Threat model:
  - Attacker has a browser → they CAN obtain a cookie by loading any
    public page. That's intentional — public pages stay public, and
    the cookie just authenticates "real user, not curl loop."
  - Attacker writes a headless-browser scraper → they CAN extract the
    cookie. But this raises scraping cost from "1 curl line" to
    "Puppeteer/Playwright + IP rotation + cookie refresh." That's the
    realistic ceiling for client-side cookie auth.
  - Attacker steals an existing cookie via XSS → not the threat model
    here; we have CSP headers preventing XSS. Cookie is HttpOnly +
    Secure + SameSite=Lax for additional defense.

Cookie shape:
  dchub_session = "<issued_ts>|<ip_prefix>|<hmac_sig>"
  Signed with HMAC-SHA256, truncated to 16 hex chars (96 bits — enough
  to make brute force impossible at HTTPS request rates).

The cookie is tied to the client's /16 IP block so a leaked cookie
can't be replayed from a different geographic region. /16 (not /32)
gives mobile network handoffs and ISP load-balancing headroom.
"""

import os
import hmac
import time
import hashlib
import logging
from flask import request

logger = logging.getLogger(__name__)

_SECRET = (os.environ.get("DCHUB_SESSION_SECRET") or
            os.environ.get("DCHUB_ADMIN_KEY") or
            "dchub-default-rotate-via-DCHUB_SESSION_SECRET-env").encode()
COOKIE_NAME = "dchub_session"
MAX_AGE_S = 86400  # 24h


def _ip_prefix() -> str:
    """First 2 octets of IPv4 (or first 8 chars of IPv6 string) so the
    cookie can't be replayed from a different /16 net. Mobile handoffs
    typically stay within /16."""
    ip = (request.headers.get("CF-Connecting-IP") or
          request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or
          request.remote_addr or "")
    if ":" in ip:  # IPv6 — first 4 hextets
        return ":".join(ip.split(":")[:4])
    parts = ip.split(".")
    if len(parts) == 4:
        return ".".join(parts[:2])
    return ip[:16]


def issue_cookie_value() -> str:
    """Build a fresh signed cookie value for the current request's IP."""
    payload = f"{int(time.time())}|{_ip_prefix()}"
    sig = hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{payload}|{sig}"


def validate_cookie(cookie_value: str | None = None) -> bool:
    """Returns True iff the cookie was issued by us, within MAX_AGE,
    and pinned to the same /16 the request is coming from."""
    if cookie_value is None:
        cookie_value = request.cookies.get(COOKIE_NAME, "")
    if not cookie_value:
        return False
    parts = cookie_value.split("|")
    if len(parts) != 3:
        return False
    issued_str, ip_prefix, sig = parts
    payload = f"{issued_str}|{ip_prefix}"
    expected_sig = hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(sig, expected_sig):
        return False
    try:
        issued = int(issued_str)
    except ValueError:
        return False
    if time.time() - issued > MAX_AGE_S:
        return False
    if ip_prefix != _ip_prefix():
        return False
    return True


def set_cookie_on_response(response):
    """Attach a fresh session cookie to an outgoing Flask response."""
    try:
        response.set_cookie(
            COOKIE_NAME, issue_cookie_value(),
            max_age=MAX_AGE_S,
            httponly=True,        # JS can't read it (XSS defense)
            secure=True,          # HTTPS only
            samesite="Lax",       # blocks most CSRF; allows top-level nav
            domain=".dchub.cloud", # so subdomains can read
            path="/",
        )
    except Exception as e:
        logger.warning(f"set_cookie_on_response failed: {e}")
    return response


def has_valid_session() -> bool:
    """Public helper for require_plan + other gates."""
    return validate_cookie()
