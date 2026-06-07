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
_UA = "DCHub/1.0 (+https://dchub.cloud)"

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


def user_agent() -> str:
    """The UA string this shim installs (for callers that want it explicitly)."""
    return _UA
