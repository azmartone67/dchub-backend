"""Force a real User-Agent on ALL outbound urllib + requests calls.

api.resend.com (and other Cloudflare-fronted APIs) reject the default
``Python-urllib/x`` — and often ``python-requests/x`` — User-Agents with
HTTP 403 "error code: 1010" (banned browser signature). That silently broke
every transactional email sent via a bare urllib/requests call without an
explicit UA (founding welcomes, dunning, digests, nudges, …).

Importing this module ONCE at startup patches both HTTP stacks so every
sender — including ones that forget to set a UA, and future code — gets a
normal UA. Senders that DO set their own UA are unaffected (explicit headers
win). r-sec 2026-06-07.
"""
# ★ 2026-09-05 — NAME THE ROLE, and cover httpx.
#
# Measured at the Cloudflare edge, 7 days, 546,632 successful POSTs to /mcp:
#     OURS (branded UA or our own network)      192,513  35.2%
#     generic UA on a network OUR agent uses    270,329  49.5%
#     genuinely unattributed                     68,641  12.6%
#     identifiable EXTERNAL MCP client           15,149   2.8%
# Half the traffic was unreadable, so no growth number on /mcp could be
# interpreted. This module was already doing its job where it was imported —
# DCHub/1.0 shows up 31,132 times from Railway — but two gaps let the rest
# through:
#
#   1. httpx was NEVER patched. python-httpx2/2.7.0 (30,760) and
#      python-httpx/0.28.1 (15,815) are ~46k calls this module never touched,
#      because it only knew urllib and requests.
#   2. Only main.py imported it, so every standalone script ran without it.
#      Python-urllib/3.13 + 3.14 (120k) and python-requests (50k) come from
#      processes that never load main.
#
# The role suffix follows the convention the rest of the codebase already uses
# explicitly (DCHub-SmokeTest/1.0, DCHub-GasPipelineLoader/1.0,
# DCHub-SitemapPing/1.0), so callers stay distinguishable in edge analytics
# instead of collapsing into one anonymous DCHub/1.0. The `DCHub` PREFIX is
# preserved, which is what every existing self-traffic filter keys on.
import os as _os
import re as _re


def _role() -> str:
    r = (_os.environ.get("DCHUB_ROLE")
         or _os.environ.get("RAILWAY_SERVICE_NAME")
         or "").strip()
    return _re.sub(r"[^A-Za-z0-9._-]", "-", r) if r else ""


_UA = (f"DCHub-{_role()}/1.0 (+https://dchub.cloud)" if _role()
       else "DCHub/1.0 (+https://dchub.cloud)")

# ── urllib: install a global opener that adds a UA when the request lacks one
try:
    import urllib.request as _u
    _opener = _u.build_opener()
    # addheaders are applied only if the Request doesn't already set the header
    _opener.addheaders = [("User-Agent", _UA)]
    _u.install_opener(_opener)
except Exception:  # pragma: no cover
    pass

# ── requests: force the default session User-Agent (explicit per-call UAs win)
try:
    import requests.utils as _ru
    import requests.sessions as _rs

    _ru.default_user_agent = lambda name="python-requests": _UA  # type: ignore

    _orig_default_headers = _rs.default_headers

    def _default_headers_with_ua():
        h = _orig_default_headers()
        h["User-Agent"] = _UA
        return h

    _rs.default_headers = _default_headers_with_ua  # type: ignore
except Exception:  # pragma: no cover
    pass


# ── httpx: the gap that let ~46k calls through as python-httpx/*
# httpx builds its default headers per-Client, so there is no single function to
# swap as with requests. Patch Client.__init__ to inject the UA when the caller
# supplied none — an explicit headers={"User-Agent": ...} still wins, exactly as
# it does for urllib and requests above.
try:
    import httpx as _hx

    def _patch_httpx(cls):
        _orig_init = cls.__init__

        def __init__(self, *a, **kw):
            try:
                hdrs = kw.get("headers") or {}
                has_ua = any(str(k).lower() == "user-agent" for k in
                             (hdrs.keys() if hasattr(hdrs, "keys") else
                              [h[0] for h in hdrs]))
                if not has_ua:
                    merged = dict(hdrs) if hasattr(hdrs, "keys") else dict(hdrs or [])
                    merged["User-Agent"] = _UA
                    kw["headers"] = merged
            except Exception:
                pass          # identity is never worth breaking a client
            return _orig_init(self, *a, **kw)

        cls.__init__ = __init__

    _patch_httpx(_hx.Client)
    _patch_httpx(_hx.AsyncClient)
except Exception:  # pragma: no cover — httpx absent in some processes
    pass


def user_agent() -> str:
    """The UA string this shim installs (for callers that want it explicitly)."""
    return _UA
