"""
_attribution_ref.py — single source of truth for per-surface web→Stripe
conversion attribution refs.

Why this exists
───────────────
Until now 100% of paid conversions driven by the website landed in
mcp_conversions as source='web:pricing-page' / web_source='pricing-page'
/ web_tool='none' — totally undifferentiated. The operator could not see
WHICH public page (a market page, a facility page, a DCPI city page, the
pricing page) actually drove a signup, so the SEO/market/facility content
that converts was invisible.

The canonical Stripe webhook (flask_mcp_endpoints.py:stripe_webhook_mcp)
already records mcp_conversions.web_source / web_tool when the checkout's
client_reference_id is parseable. This module mints those refs in ONE
place so every surface emits the SAME, parseable, URL-safe shape.

Scheme
──────
    web__<surface>__<slug>

  e.g.  web__market__northern-virginia
        web__facility__a1b2c3d4
        web__dcpi__ashburn
        web__pricing__none

`surface` lands in mcp_conversions.web_source, `slug` lands in
mcp_conversions.web_tool, so the operator can GROUP BY web_source to see
which surface type sells, and drill into web_tool for the exact page.

Collision safety
────────────────
This `web__` prefix is deliberately DISJOINT from the existing reserved
client_reference_id prefixes the webhooks special-case:
    DCM-…   pair-code redemption   (main.py ~11154)
    tu-…    top-up token           (main.py ~11159)
    ref_…   the legacy frontend ref_<src>__tool_<tool>__ts_ shape
    ref_…   phase17 ref_<src>__tool_<tool>
A bare MCP agent session id is a UUID and also won't collide. The webhook
parse for `web__` is added guarded so it NEVER swallows a DCM-/tu-/ref_
ref — those branches keep running first / untouched.
"""
import os
import re

# Only the chars Stripe round-trips cleanly in client_reference_id and that
# survive our parse: lowercase alnum + dash. Everything else collapses to '-'.
_SAFE = re.compile(r"[^a-z0-9-]+")

# ── Referral-partner attribution (2026-08-07) ──────────────────────────────
# A reseller sends us traffic and expects commission on what it converts.
# Before this, nothing survived the hop: /go/partners/<slug> logged the click
# then 302'd to /partners, and by checkout the client_reference_id read
# web__pricing__none — indistinguishable from organic. Click counts on one
# side, payments on the other, no join.
#
# `partner` is just another surface in the existing web__<surface>__<slug>
# scheme, so the Stripe webhook parse and mcp_conversions.web_source/web_tool
# need no change at all — a partner conversion lands as
# web_source='partner', web_tool='data-center-signals'.
PARTNER_SURFACE = "partner"
PARTNER_COOKIE = "dchub_partner_ref"


def partner_cookie_max_age():
    """Attribution window in seconds. Default 30d, override with
    DCHUB_PARTNER_REF_TTL_DAYS. Agree this number in the contract — for a
    product with a long consideration window a short cookie under-credits
    the partner, and a disputed invoice is worse than a generous window."""
    try:
        days = int(os.environ.get("DCHUB_PARTNER_REF_TTL_DAYS", "30"))
    except (TypeError, ValueError):
        days = 30
    return max(1, min(days, 365)) * 86400


def known_partners():
    """Allow-listed referral slugs, from DCHUB_REFERRAL_PARTNERS (comma-sep).

    ★ This allow-list is the anti-forgery control, not decoration. Surface and
    ref arrive as QUERY PARAMS on /pricing/upgrade, so without it any visitor
    could hand-craft ?surface=partner&ref=whoever and mint a commissionable
    conversion. Unknown slug → no attribution, silently."""
    raw = os.environ.get("DCHUB_REFERRAL_PARTNERS", "") or ""
    out = set()
    for part in raw.replace("\n", ",").split(","):
        slug = _clean(part, "")
        if slug and slug != "none":
            out.add(slug)
    return frozenset(out)


def normalize_partner(slug):
    """Cleaned slug if it is an allow-listed partner, else None."""
    s = _clean(slug, "")
    if not s or s == "none":
        return None
    return s if s in known_partners() else None


def resolve_attribution(surface, ref, partner_cookie=None):
    """Decide the (surface, ref) a checkout should be attributed to.

    Precedence, deliberately in this order:

      1. An explicit ?surface= on the request always wins. A page that knows
         what drove the click is better evidence than a cookie set days ago,
         and this keeps every existing caller byte-identical.
      2. Otherwise an allow-listed partner cookie attributes to that partner.
      3. Otherwise nothing changes — callers fall through to their existing
         legacy `mcp:…` shape.

    Pure: no flask, no db, no clock. Callers pass request.cookies.get(...).

    >>> resolve_attribution("market", "ashburn", "data-center-signals")
    ('market', 'ashburn')
    >>> resolve_attribution("", "paywall", "data-center-signals")  # allow-listed
    ('partner', 'data-center-signals')
    >>> resolve_attribution("", "paywall", "not-a-partner")
    ('', 'paywall')
    """
    if (surface or "").strip():
        return (surface, ref)
    slug = normalize_partner(partner_cookie)
    if slug:
        return (PARTNER_SURFACE, slug)
    return (surface, ref)


def _clean(part, fallback="none"):
    """Lowercase, collapse unsafe chars to '-', trim dashes, cap length."""
    s = _SAFE.sub("-", str(part or "").strip().lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)          # never produce a stray '__' separator
    return (s or fallback)[:64]


def build_web_ref(surface, slug=None):
    """Build a per-surface attribution client_reference_id.

    >>> build_web_ref("market", "Northern Virginia")
    'web__market__northern-virginia'
    >>> build_web_ref("facility", "a1b2c3d4")
    'web__facility__a1b2c3d4'
    >>> build_web_ref("pricing")
    'web__pricing__none'
    """
    surf = _clean(surface, "web")
    sl   = _clean(slug, "none")
    return f"web__{surf}__{sl}"[:180]


def append_web_ref(stripe_url, surface, slug=None):
    """Append ?/&client_reference_id=web__<surface>__<slug> to a Stripe link.

    Idempotent-ish: uses the right separator for the URL. Returns the URL
    unchanged-but-decorated string; never raises.
    """
    try:
        from urllib.parse import quote
        ref = build_web_ref(surface, slug)
        sep = "&" if "?" in (stripe_url or "") else "?"
        return f"{stripe_url}{sep}client_reference_id={quote(ref)}"
    except Exception:
        return stripe_url


# Webhook side: parse a web__ ref back into (surface, slug). Returns
# (None, None) for anything that is not the web__ shape — so the caller can
# fall through to the existing ref_/DCM-/tu- handling untouched.
_WEB_RE = re.compile(r"^web__([a-z0-9-]+)__([a-z0-9-]+)$")


def parse_web_ref(client_reference_id):
    """(surface, slug) or (None, None). Safe on any input."""
    m = _WEB_RE.match(str(client_reference_id or ""))
    if not m:
        return (None, None)
    return (m.group(1), m.group(2))
