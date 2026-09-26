"""
paywall_hint_middleware.py — Phase r57 (2026-05-25).

Closes the MCP funnel UX loop without touching the CF worker.

r57 update: A/B/C copy variants. Deterministic per-caller selection
keyed by hash(IP+UA) so a given agent always sees the same variant
(important for measuring conversion lift). Variant choice is exposed
on the response in `_upgrade_hint.variant` and logged to the
`ab_funnel_log` table for retrospective analysis.

r56 baseline: User's 0.04% conversion problem — AI agents hit any
gated endpoint, get a bare 4xx, give up. The user behind the agent
never sees DC Hub's value proposition.

This middleware intercepts every 4xx response from /api/* paths and
ENRICHES it with an _upgrade_hint field containing:
  - agent_quotable copy the AI can paste verbatim to its user
  - claim_key endpoint
  - signup_url
  - what_you_get description
  - variant: A|B|C  (r57)

Works for:
  - 401 Unauthorized (no API key)
  - 403 Forbidden (insufficient tier)
  - 429 Rate Limited (over quota)

Idempotent: skips the enrichment if response already has
_upgrade_hint (e.g. from a tier_gate decorator) OR if response body
isn't valid JSON.

Public side effect: every blocked AI request now carries the
recovery path in the response itself. No registry lookup, no
documentation cross-reference needed.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import threading
import time

from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write
from ai_surface_canon import canon_text
from tier_registry import price_display as _canon_price_display
import tier_registry as _tr


paywall_ab_admin_bp = Blueprint("paywall_ab_admin", __name__)


def _pack_offer() -> str:
    """The pack offer ("$10 one-time = 1,000 API credits"), read from the
    webhook's own pack constants through canon, or "" when they cannot be
    read."""
    try:
        return canon_text("{canon_pack_offer}") or ""
    except Exception:  # noqa: BLE001
        return ""


# ★2026-09-21 — THE LADDER, NOT THE RETIRED STARTER PLAN. This block led with
# a Starter checkout, a Starter line in what_you_get and a Starter rung in the
# price line, on every 401/403/429 it enriched: a plan /pricing does not sell.
# The rungs are now the ones /pricing sells, every price read, never typed:
# the $10 pack and Developer for an agent, Pro for the Pro-only tools.
_HINT_BASE = {
    "claim_key":       "POST https://dchub.cloud/api/v1/keys/claim",
    "signup_url":      "https://dchub.cloud/signup",
    # ★ r-anonattrib-hint (2026-09-20): these were bare buy.stripe.com URLs.
    # An agent that surfaced one sent its human straight to Stripe with no
    # client_reference_id — the same unattributable sale #4872 and #4886
    # closed on the other two emitters. /checkout/start is attributable by the
    # session it creates on arrival, so even this static default can be
    # traced; the per-response pack_url and the inheritance below do better.
    "developer_url":   "https://dchub.cloud/checkout/start?tier=developer",
    "pro_url":         "https://dchub.cloud/checkout/start?tier=pro",
    "preview_url":     "https://dchub.cloud/api/v1/mcp/preview/<tool>",
    "upgrade_hint":    "https://dchub.cloud/api/v1/upgrade-hint",
    # r47.37.2 (2026-05-26): expose the enterprise data-licensing surface
    # alongside the self-serve tiers — heavy due-diligence sessions are
    # enterprise prospects, not self-serve buyers.
    "enterprise_url":  "https://dchub.cloud/enterprise",
    "enterprise_quick": (f"For hedge fund / REIT / broker / infra GP use: from "
                         f"${_tr.ENTERPRISE_FROM_USD_YEAR:,}/yr data licensing with raw exports, "
                         f"custom DCPI weights, monthly briefings. https://dchub.cloud/enterprise"),
    # ★ r-noinstruct (2026-09-20): the free dev key needs NO email — one POST
    # to /api/v1/keys/claim, the single property a catalogue partner featured
    # us for.
    # ★2026-09-22 (owner wording rule): the $10 pack is API capacity only —
    # never "full depth", numbers or an unlock. Developer and Pro unchanged.
    "what_you_get":    (f"Free dev key (one POST, no email, no card) = {_tr.calls_per_day('free')} calls/day. "
                        f"More API capacity: {_pack_offer()} (1 per call, 5 for heavy tools). "
                        f"Developer {_tr.price_display('developer')}: full depth on every tool except the "
                        f"Pro-only ones; Pro {_tr.price_display('pro')} adds the Pro-only tools."),
    # "$25K+/yr Enterprise" disagreed with ENTERPRISE_FROM_USD_YEAR ($12,000),
    # the anchor r-price-collapse set for the human-sold lane.
    # ★ r-noinstruct (2026-09-20): the effective anonymous allowance is the
    # FREE one — get_request_tier returns 'anon', tier_registry.limits() has no
    # entry for that alias and falls through to TIER_LIMITS['free'] — so both
    # resolve from the registry rather than as hand-typed literals.
    "pricing_quick":   (f"Anonymous {_tr.calls_per_day('anon')}/day · "
                        f"Free key {_tr.calls_per_day('free')}/day · {_pack_offer()} (API capacity) · "
                        f"Developer {_tr.price_display('developer', '')} {_tr.calls_per_day('developer')}/day · "
                        f"Pro {_tr.price_display('pro', '')} {_tr.calls_per_day('pro')}/day · "
                        f"from ${_tr.ENTERPRISE_FROM_USD_YEAR:,}/yr Enterprise data licensing"),
}


# ── A/B/C/D copy variants ──────────────────────────────────────────
#
# Variant A: "factual / direct".
# Variant B: "operator-addressed" — written for the person behind the agent.
# Variant C: "loss aversion" — leads with what the free key already covers.
# Variant D: minimum-viable, one line.
#
# ★2026-09-21: every variant sold the retired Starter plan (as "the cheapest
# paid unlock") and C quoted retired counts (21k facilities, 32+ DCPI markets,
# 4,000+ deals). The rungs are now the ones /pricing sells and every count is
# a canon placeholder, so each copy is a lambda: canon_text() resolves on THIS
# response, not once at import (see _copy). The copy describes; it does not
# instruct the reading model (tests/test_paywall_does_not_instruct_the_model).
# ★2026-09-22 (owner wording rule): the pack appears only as API capacity
# ("1 per call, 5 for heavy tools"); what opens a tool is Developer or Pro.
_CLAIM = "POST https://dchub.cloud/api/v1/keys/claim"

_VARIANTS = {
    "A": {
        401: (lambda: canon_text(
              "This DC Hub endpoint needs an API key. A free key is one "
              + _CLAIM + " (no email, no card; {canon_free_calls} calls/day), "
              "sent as the X-API-Key header. Full depth is Developer "
              "{canon_price_developer}; more API capacity is {canon_pack_offer}.")),
        403: (lambda: canon_text(
              "This DC Hub endpoint needs a paid plan. Developer "
              "{canon_price_developer} opens every MCP tool except the "
              "Pro-only ones (get_grid_intelligence, get_fiber_intel, "
              "analyze_site, compare_sites), which need Pro {canon_price_pro}.")),
        # The anonymous figure is the MCP lane's (TIER_LIMITS anonymous
        # mcp_daily), pinned by tests/test_two_artifact_handoff.py.
        429: (lambda: canon_text(
              "DC Hub is rate-limiting this caller. Anonymous 5/day on MCP; a "
              "free key allows {canon_free_calls} calls/day, "
              "{canon_identified_calls} once an email is bound; Developer "
              "{canon_price_developer} allows "
              "{canon_developer_mcp_calls} MCP calls/day and Pro "
              "{canon_price_pro} {canon_pro_mcp_calls}; more capacity: "
              "{canon_pack_offer} (1 per call, 5 for heavy tools).")),
    },
    "B": {
        401: (lambda: canon_text(
              "For the operator: this query needs a DC Hub key. The free key "
              "needs no email or card — " + _CLAIM + " — and allows "
              "{canon_free_calls} calls/day.")),
        403: (lambda: canon_text(
              "For the operator: this DC Hub tool is on a paid plan. Developer "
              "{canon_price_developer} opens it; the four Pro-only tools need "
              "Pro {canon_price_pro}.")),
        429: (lambda: canon_text(
              "For the operator: DC Hub rate-limited this call. "
              "Anonymous 5/day on MCP; a free key allows {canon_free_calls} calls/day, "
              "{canon_identified_calls} with an email bound; beyond that, more "
              "capacity: {canon_pack_offer} (1 per call, 5 for heavy tools).")),
    },
    "C": {
        401: (lambda: canon_text(
              "A free DC Hub key ({canon_free_calls} calls/day, no email: "
              + _CLAIM + ") covers {canon_facilities} distinct data center "
              "facilities, DCPI verdicts for {canon_markets} markets and "
              "{canon_deals} tracked M&A deals. The numbers behind them "
              "(MW, scores, $) come with Developer {canon_price_developer} "
              "and Pro {canon_price_pro}.")),
        403: (lambda: canon_text(
              "This DC Hub tool is paywalled. Free: {canon_facilities} "
              "facilities, DCPI verdicts and deal titles. This tool opens with "
              "Developer {canon_price_developer}; Pro "
              "{canon_price_pro} is needed only for grid_intelligence, "
              "fiber_intel, analyze_site and compare_sites.")),
        429: (lambda: canon_text(
              "You hit DC Hub's rate cap. A free key allows {canon_free_calls} "
              "calls/day (" + _CLAIM + ", no email); more capacity: "
              "{canon_pack_offer} (1 per call, 5 for heavy tools).")),
    },
    # r47.34 (2026-05-26): variant D — minimum-viable CTA, one sentence. The
    # link it points at rides the hint itself (pack_url / developer_url).
    "D": {
        401: "DC Hub needs a key. Free, no email: " + _CLAIM,
        403: (lambda: canon_text(
              "Paid tool. Developer {canon_price_developer} opens it (developer_url).")),
        429: (lambda: canon_text(
              "Rate cap. Free key = {canon_free_calls} calls/day, no email: "
              + _CLAIM)),
    },
}


# ★ One definition, used by the middleware's write-side skip AND by every
# read below. Two copies of "what counts as internal" would drift, and the
# direction it would drift is toward flattering numbers.
_ADMIN_PREFIXES = ("/api/v1/admin/", "/api/v1/internal/")
# ★ '%%', not '%'. Every read that embeds this fragment binds a parameter
# (the window: `(%s || ' days')`), so psycopg2 %-formats the WHOLE string
# and a bare percent consumes a tuple slot — /api/v1/admin/funnel-ab
# answered {"error":"tuple index out of range"} for every request from the
# day this landed until 2026-09-02 (live at the Railway origin 00:24Z). The
# doubled form is right in BOTH contexts: bound (→ one '%') and unbound
# (LIKE '%%' is two wildcards, which matches exactly what '%' matches).
_ADMIN_EXCLUDE = ("path NOT LIKE '/api/v1/admin/%%' "
                  "AND path NOT LIKE '/api/v1/internal/%%'")


def _pick_variant(ip: str, ua: str) -> str:
    """Deterministic A/B/C/D selection. Same caller → same variant.

    r47.34: 4-way split so the new minimum-viable CTA (D) gets ~25%
    of traffic alongside the original three. /api/v1/admin/paywall-ab/stats
    rolls this up so we can see which variant moves 0.048% paywall→click."""
    h = hashlib.sha256(f"{ip}|{ua}".encode()).hexdigest()
    bucket = int(h[:8], 16) % 4
    return ["A", "B", "C", "D"][bucket]


def _copy(copy) -> str:
    """One variant's copy as served. Copy that carries canon is a lambda in
    _VARIANTS, so its count resolves on THIS response: canon_text() inside the
    dict ran once, at import, and quoted the cold-start floor for the life of
    the process (★2026-09-13)."""
    return copy() if callable(copy) else copy


def _agent_quotable_for(variant: str, status: int) -> str:
    """Status-specific copy keyed by variant. Falls back to A."""
    v = _VARIANTS.get(variant) or _VARIANTS["A"]
    return _copy(v.get(status) or v.get(401))


# DDL is bootstrapped ONCE per process (guarded below) and kept OUT of the
# per-event INSERT path. 2026-07-15: the prior version ran CREATE TABLE /
# CREATE INDEX IF NOT EXISTS in the SAME transaction as the INSERT, on the
# after_request hot path, for EVERY 4xx. CREATE INDEX takes a ShareLock on
# ab_funnel_log which conflicts with the RowExclusiveLock a concurrent INSERT
# holds, so two overlapping after_request calls formed a lock cycle and Postgres
# killed one with 40P01 DeadlockDetected — the funnel write was then swallowed
# (corrupting the conversion-funnel metric) and the deadlocking txn held a pooled
# connection for ~deadlock_timeout, feeding pool saturation. An INSERT-only hot
# path (no unique constraint, sequence PK) cannot deadlock.
_AB_DDL_LOCK = threading.Lock()
_ab_ddl_ready = False


def _ensure_ab_funnel_table() -> bool:
    """Create ab_funnel_log + its index exactly once per process, in its own
    short committed txn. Returns True once the table is known to exist. Uses the
    raw psycopg2 cursor (getattr(_c,'_cur',_c)) because the safe_db wrapper SKIPS
    DDL (SKIP_DDL default-on) and the lazy CREATE TABLE is load-bearing. Never
    raises."""
    global _ab_ddl_ready
    if _ab_ddl_ready:
        return True
    with _AB_DDL_LOCK:
        if _ab_ddl_ready:
            return True
        try:
            from db_utils import safe_db
            with safe_db() as conn:
                _c = conn.cursor()
                cur = getattr(_c, "_cur", _c)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS ab_funnel_log (
                        id          BIGSERIAL PRIMARY KEY,
                        variant     TEXT NOT NULL,
                        status      INT  NOT NULL,
                        path        TEXT NOT NULL,
                        ip_hash     TEXT NOT NULL,
                        ts          TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS ab_funnel_log_variant_ts_idx
                    ON ab_funnel_log (variant, ts DESC)
                """)
                conn.commit()
            _ab_ddl_ready = True
        except Exception:
            # Leave _ab_ddl_ready False so a later call retries the bootstrap.
            note_swallowed_write("ab_funnel_log",
                                 where="paywall_hint_middleware._ensure_ab_funnel_table")
        return _ab_ddl_ready


def _log_ab_event(variant: str, status: int, path: str,
                   ip_hash: str) -> None:
    """Log to ab_funnel_log table. Best-effort, never raises. INSERT-only hot
    path (DDL is bootstrapped once via _ensure_ab_funnel_table) plus a bounded
    retry on 40P01/40001 so a transient serialization error re-lands the funnel
    event instead of silently dropping it (the conversion metric depends on it
    landing)."""
    if not _ensure_ab_funnel_table():
        note_swallowed_write("ab_funnel_log",
                             where="paywall_hint_middleware._log_ab_event")
        return
    for _attempt in range(3):
        try:
            from db_utils import safe_db
            with safe_db() as conn:
                _c = conn.cursor()
                cur = getattr(_c, "_cur", _c)
                cur.execute("""
                    INSERT INTO ab_funnel_log
                        (variant, status, path, ip_hash)
                    VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING
                """, (variant, status, path[:200], ip_hash))
                conn.commit()
            return
        except Exception as _e:
            # 40P01 = deadlock_detected, 40001 = serialization_failure: both are
            # transient; retry on a fresh conn before giving up so the metric
            # lands. Any other error is a real failure — note + stop.
            pgcode = getattr(_e, "pgcode", None)
            if pgcode in ("40P01", "40001") and _attempt < 2:
                time.sleep(0.05 * (_attempt + 1))
                continue
            note_swallowed_write("ab_funnel_log",
                                 where="paywall_hint_middleware._log_ab_event")
            return


def _safe_caller_id():
    """Return (ip, ua, ip_hash). Hash truncates the IP so we don't
    persist raw IPs in the funnel log."""
    try:
        ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
              or request.remote_addr or "0.0.0.0")
    except Exception:
        ip = "0.0.0.0"
    try:
        ua = request.headers.get("User-Agent", "")[:200]
    except Exception:
        ua = ""
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16]
    return ip, ua, ip_hash


# r70-c (2026-05-26): platform-targeted paywall hint.
# When the requesting UA matches a known agent class (Claude / Cursor
# / ChatGPT / Perplexity / Gemini / Groq), append a 1-line CTA tailored
# to that platform's onboarding ergonomics, with the partner landing
# page URL pre-filled. Reduces friction from generic /signup to a
# page that already knows who you are.

_PLATFORM_HINTS = {
    "claude":     {"name": "Claude",
                    "url":  "https://dchub.cloud/partners/perplexity",  # placeholder; will swap once /partners/claude exists
                    "cta":  ("Claude user — claim a Claude-optimized "
                             "DC Hub key in 30s at https://dchub.cloud/signup?ref=claude. "
                             "Pre-fills client_name so your usage tracks correctly.")},
    "cursor":     {"name": "Cursor",
                    "url":  "https://dchub.cloud/signup?ref=cursor",
                    "cta":  ("Cursor user — Cursor MCP support is first-class "
                             "on DC Hub. Free key (10 calls/day) at "
                             "https://dchub.cloud/signup?ref=cursor.")},
    "chatgpt":    {"name": "ChatGPT / OpenAI",
                    "url":  "https://dchub.cloud/signup?ref=chatgpt",
                    "cta":  ("ChatGPT / OpenAI user — claim a key at "
                             "https://dchub.cloud/signup?ref=chatgpt to keep "
                             "DC Hub citations live in your answers.")},
    "perplexity": {"name": "Perplexity",
                    "url":  "https://dchub.cloud/partners/perplexity",
                    "cta":  ("Perplexity user — see the dedicated partner page "
                             "at https://dchub.cloud/partners/perplexity (built for "
                             "your citation engine).")},
    "gemini":     {"name": "Gemini",
                    "url":  "https://dchub.cloud/partners/gemini",
                    "cta":  ("Gemini / DeepMind user — partner page at "
                             "https://dchub.cloud/partners/gemini covers the "
                             "non-Google competitive-intel use case.")},
    "groq":       {"name": "Groq",
                    "url":  "https://dchub.cloud/partners/groq",
                    "cta":  ("Groq user — partner page at "
                             "https://dchub.cloud/partners/groq covers location "
                             "transparency for your inference customers.")},
}


def _platform_targeted_cta(ua: str) -> str:
    """Return a platform-specific CTA when UA matches a known agent."""
    ua_low = (ua or "").lower()
    for needle, info in _PLATFORM_HINTS.items():
        if needle in ua_low:
            return info["cta"]
    return ""


# r67-b (2026-05-26): per-caller hit-count personalizer.
# When a 403 fires on a paid-tool path, look up how many times THIS
# caller has hit the same tool in the last 30 days. Append a
# one-liner to agent_quotable: "You've called X 47 times this month
# — $199 unblocks all future calls."
#
# Cheap query against mcp_connections, capped by 50ms statement
# timeout so a slow DB never blocks the response. Returns "" on any
# failure (the rest of the hint still ships).

_PAID_TOOL_PATH_TO_NAME = {
    "/api/v1/grid/intelligence":          "get_grid_intelligence",
    "/api/v1/grid-intelligence":          "get_grid_intelligence",
    "/api/v1/fiber/intel":                "get_fiber_intel",
    "/api/v1/fiber-intel":                "get_fiber_intel",
    "/api/v1/site/analyze":               "analyze_site",
    "/api/v1/analyze-site":               "analyze_site",
    "/api/v1/sites/compare":              "compare_sites",
    "/api/v1/compare-sites":              "compare_sites",
    "/api/v1/dchub-recommendation":       "get_dchub_recommendation",
}


def _tool_name_for_path(path: str) -> str | None:
    """Best-effort path → tool-name mapper. Returns None for
    non-paid-tool paths (most 4xx paths). Quick prefix matches only."""
    for prefix, tool in _PAID_TOOL_PATH_TO_NAME.items():
        if path.startswith(prefix):
            return tool
    return None


def _personal_hit_pitch(ip: str, ua: str, path: str, status: int) -> str:
    """Look up the caller's prior call count for this paid tool +
    return a 1-sentence personalized pitch, or '' if nothing useful."""
    if status != 403:
        return ""
    tool = _tool_name_for_path(path)
    if not tool:
        return ""
    try:
        import psycopg2
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        if not url:
            return ""
        conn = psycopg2.connect(url, connect_timeout=2)
        try:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = '300ms'")
                cur.execute("""
                    SELECT COUNT(*),
                           COUNT(*) FILTER (WHERE status_code = 403)
                      FROM mcp_connections
                     WHERE ip_address = %s AND user_agent = %s
                       AND tool_name = %s
                       AND created_at > NOW() - INTERVAL '30 days'
                """, (ip, ua, tool))
                r = cur.fetchone() or (0, 0)
                total = int(r[0] or 0)
                blocked = int(r[1] or 0)
        except Exception:
            try: conn.close()
            except Exception: pass
            return ""
        finally:
            try: conn.close()
            except Exception: pass
    except Exception:
        return ""

    if blocked < 2:
        # Too few hits to be a near-converter signal — fall back to
        # generic copy (don't make a noisy claim for a first-time hit)
        return ""
    if blocked >= 10:
        intensity = "heavy"
        urgency = ("Every additional call wastes a round-trip — "
                     "upgrade pays for itself in days.")
    elif blocked >= 5:
        intensity = "frequent"
        urgency = ("Five+ blocks in 30 days = clear upgrade signal.")
    else:
        intensity = "starting to"
        urgency = ""

    return (f"Personalized: you've called {tool} {total} times this "
              f"month, hitting the paywall {blocked} times ({intensity} "
              f"usage). {urgency} "
              f"Pro ({_canon_price_display('pro')}, pro_url in this hint) opens "
              f"{tool} and the 3 other Pro-only tools.").strip()


def _names_what_opens_it(body):
    """True for a body whose upgrade_options all say where they open the
    endpoint: rest_wall_ladder's output, and nothing else we emit."""
    opts = body.get("upgrade_options")
    return (isinstance(opts, list) and bool(opts)
            and all(isinstance(o, dict) and o.get("opens") for o in opts))


def _partner_of_request():
    """rate_limiter's own classification: keyless, from a declared partner egress."""
    try:
        from rate_limiter import partner_of_request
        return partner_of_request()
    except Exception:
        return None


def register_paywall_hint_middleware(app):
    """Attach the after_request enricher. Idempotent."""
    if getattr(app, "_paywall_hint_attached", False):
        return
    app._paywall_hint_attached = True

    @app.after_request
    def _enrich_4xx_with_hint(response):
        try:
            path = request.path or ""
            # Only enrich /api/* paths (don't touch HTML pages)
            if not path.startswith("/api/"):
                return response

            # ★ NEVER the admin/internal surfaces. 698 routes live under
            # /api/v1/admin/ and every one of them 403s on a wrong or absent
            # X-Admin-Key — an operator mistyping a key, a health probe, a
            # scheduled tick whose key rotated. Two things went wrong when
            # those reached the block below:
            #
            #   1. the 403 came back carrying Stripe checkout links, the tier
            #      table and the enterprise pitch, so anyone probing an admin
            #      path was handed the price list;
            #   2. worse, _log_ab_event() ran FIRST, so every internal auth
            #      failure was recorded in ab_funnel_log as a blocked prospect.
            #      /api/v1/admin/funnel-ab-stats reads that table to score the
            #      A/B variants — the experiment has been counting our own
            #      probes in its denominator.
            #
            # A conversion metric contaminated by internal traffic is worse
            # than no metric: it moves, so it looks alive.
            if path.startswith(_ADMIN_PREFIXES):
                return response
            # Same reasoning for the ops read gate's own refusals
            # (ops_viewer_gate): a keyless read of /api/v1/ops/* or the MCP
            # funnel is not a blocked prospect, so no pitch and no A/B event.
            try:
                from flask import g as _g
                if getattr(_g, "ops_gate_denied", False):
                    return response
            except Exception:
                pass

            # Only enrich 401/403/429
            if response.status_code not in (401, 403, 429):
                return response

            # A declared partner's shared keyless bucket (rate_limiter's
            # 'partner' tier): its 429 already states the way out, and the
            # partner relays error bodies verbatim to its customers' agents.
            # The hint instructs the reader and quotes prices, so it is not
            # added there, and no variant was shown, so no A/B event is logged.
            # Every other 429 is enriched exactly as before.
            if response.status_code == 429 and _partner_of_request():
                return response

            # Don't enrich responses that aren't JSON
            ct = (response.content_type or "").lower()
            if "json" not in ct:
                return response

            # Don't enrich if body is huge — these should be tiny error envelopes
            if response.content_length and response.content_length > 5000:
                return response

            # Read + parse existing body
            try:
                raw = response.get_data(as_text=True)
                body = json.loads(raw) if raw else {}
            except Exception:
                return response

            if not isinstance(body, dict):
                return response

            # Skip if already enriched (some endpoints inject their own hint)
            if "_upgrade_hint" in body or body.get("_gated"):
                return response
            # A wall built by checkout_click_tracker.rest_wall_ladder already
            # names every rung that opens its endpoint (each option says where
            # it `opens`). The hint would add rungs that do not (Starter, the
            # free key), which is the false offer that wall exists to remove.
            if _names_what_opens_it(body):
                return response

            # r57: pick A/B/C variant + log
            ip, ua, ip_hash = _safe_caller_id()
            variant = _pick_variant(ip, ua)
            _log_ab_event(variant, response.status_code, path, ip_hash)

            # r67-b (2026-05-26): personalize the hint with per-caller
            # usage. The MCP funnel showed 114 callers hit
            # get_grid_intelligence (paid) 5,382 times in 30d — every
            # 403 was a wasted round-trip. Telling the caller "you've
            # hit this paywall N times this month, ROI of upgrade is
            # measurable" closes a $0.06%-conversion gap.
            personal_pitch = _personal_hit_pitch(ip, ua, path,
                                                  response.status_code)

            # Enrich
            agent_q = _agent_quotable_for(variant, response.status_code)
            if personal_pitch:
                agent_q = f"{agent_q}\n\n{personal_pitch}"

            # r70-c: platform-targeted CTA on top of the personalized
            # block. If the UA matches Claude / Cursor / Perplexity /
            # Gemini / Groq / ChatGPT, append a 1-line CTA pointing at
            # that platform's partner landing page or pre-filled signup.
            platform_cta = _platform_targeted_cta(ua)
            if platform_cta:
                agent_q = f"{agent_q}\n\n{platform_cta}"

            # The paywall builder already minted a pair code for THIS caller and
            # wrote it into the body's upgrade URLs. Inherit those rather than
            # mint again, so the hint names the same destination the rest of the
            # response does and carries the same client_reference_id. The
            # _HINT_BASE defaults stay as the fallback for bodies that have none.
            _hint_over = {}
            # The pack's measured checkout, caller-independent: /go/c stamps
            # the click with its plan. Absent when no link can be minted (the
            # fallback is the bare pricing page, which this hint never names).
            try:
                from routes.checkout_click_tracker import checkout_url as _checkout_url
                _pack = _checkout_url("metered")
                if _pack.startswith("https://dchub.cloud/go/c/"):
                    _hint_over["pack_url"] = _pack
            except Exception:  # noqa: BLE001
                pass
            for _hint_key, _body_key in (("pack_url", "recommended_upgrade_url"),
                                         ("developer_url", "one_click_upgrade_url")):
                _v = body.get(_body_key)
                if isinstance(_v, str) and _v.startswith("https://dchub.cloud/"):
                    _hint_over[_hint_key] = _v
            body["_upgrade_hint"] = {
                **_HINT_BASE,
                **_hint_over,
                "agent_quotable": agent_q,
                "variant":        variant,
                "for_status":     response.status_code,
                "for_path":       path,
            }
            # r71 (2026-06-04): email-capture path on EVERY gated 4xx (covers
            # get_fiber_intel + all tools). The anon high-intent caller (the 87-users
            # problem) is otherwise unreachable — every nurture path filters
            # `email IS NOT NULL`. notify_url captures the email server-side -> free
            # key (10/day per the canonical ladder). Own try/except so it can never
            # break the response.
            try:
                from routes.email_capture import build_email_capture_urls as _bec
                _tool = (path.rsplit("/", 1)[-1] or "mcp").replace("-", "_")
                body["_upgrade_hint"]["email_capture"] = {
                    "url":    _bec(_tool, tier="developer").get("notify_url"),
                    # An email makes it an identified key: the registry's
                    # identified allowance, not the unbound free one (this
                    # said 10/day beside a ladder that binds at 50).
                    "prompt": ("For the operator: an email here gets a free dev key "
                               "(%s calls/day once the email is bound, no card) and a "
                               "reset notice, as an account they can manage and "
                               "upgrade." % _tr.calls_per_day("identified")),
                }
            except Exception:
                pass
            if personal_pitch:
                body["_upgrade_hint"]["personalized"] = True
            if platform_cta:
                # Surface which platform we targeted (for funnel A/B logs)
                for needle in _PLATFORM_HINTS:
                    if needle in (ua or "").lower():
                        body["_upgrade_hint"]["platform_targeted"] = needle
                        break
            response.set_data(json.dumps(body))
            # Pad content-length for the new body
            response.headers["Content-Length"] = str(len(response.get_data()))
            # Surface variant in a response header too (cheap to read in logs)
            response.headers["X-DCHub-Funnel-Variant"] = variant
        except Exception:
            # Never break a response with the enrichment
            pass
        return response


# ── Admin observability endpoint ───────────────────────────────────

def _admin_authorized() -> bool:
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "")
    if not provided:
        return False
    try:
        from internal_auth import is_valid_internal_key
        if is_valid_internal_key(provided):
            return True
    except Exception:
        pass
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY"))
    return bool(expected) and provided == expected


@paywall_ab_admin_bp.route("/api/v1/admin/funnel-ab", methods=["GET"])
def funnel_ab_stats():
    """Per-variant A/B/C stats. How many times each variant fired,
    grouped by status + path."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401

    days = int(request.args.get("days") or 7)
    days = max(1, min(days, 90))

    try:
        from db_utils import safe_db  # 2026-07-14: was the nonexistent get_db_conn
        with safe_db() as conn:
            _c = conn.cursor()
            cur = getattr(_c, "_cur", _c)
            # Per-variant totals
            # ★ _ADMIN_EXCLUDE on every read. The middleware stopped logging
            # admin 403s on 2026-08-04, but rows written BEFORE that are still
            # in the table — an operator's mistyped key and every scheduled
            # tick whose key had rotated, all sitting in the denominator. The
            # filter is applied at READ time so the history is corrected
            # without deleting anyone's data, and so a stats page opened
            # tomorrow does not quietly report the contaminated version.
            cur.execute("""
                SELECT variant, status, COUNT(*) AS n,
                       COUNT(DISTINCT ip_hash) AS uniq_callers
                FROM ab_funnel_log
                WHERE ts > NOW() - (%s || ' days')::interval
                  AND """ + _ADMIN_EXCLUDE + """
                GROUP BY variant, status
                ORDER BY variant, status
            """, (str(days),))
            rows = cur.fetchall() or []

            # Top paths by variant
            cur.execute("""
                SELECT variant, path, COUNT(*) AS n
                FROM ab_funnel_log
                WHERE ts > NOW() - (%s || ' days')::interval
                  AND """ + _ADMIN_EXCLUDE + """
                GROUP BY variant, path
                ORDER BY n DESC
                LIMIT 30
            """, (str(days),))
            top_rows = cur.fetchall() or []

            # Total
            cur.execute("""
                SELECT COUNT(*) FROM ab_funnel_log
                WHERE ts > NOW() - (%s || ' days')::interval
                  AND """ + _ADMIN_EXCLUDE + """
            """, (str(days),))
            _row = cur.fetchone()
            total = int(_row[0] or 0) if _row else 0
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500

    by_variant = {}
    for r in rows:
        variant, status, n, uniq = r[0], r[1], r[2], r[3]
        by_variant.setdefault(variant, {"by_status": {}, "uniq_callers": 0})
        by_variant[variant]["by_status"][str(status)] = n
        # Note: uniq aggregates per status; we sum to approximate but
        # the same caller can hit multiple statuses, so this is a ceiling
        by_variant[variant]["uniq_callers"] += uniq

    top_paths = [
        {"variant": r[0], "path": r[1], "n": r[2]} for r in top_rows
    ]

    return jsonify({
        "ok":          True,
        "window_days": days,
        "total_4xx":   total,
        "by_variant":  by_variant,
        "top_paths":   top_paths,
        "interpretation": (
            "Higher uniq_callers for a variant means more agents got "
            "that copy. Cross-reference against /api/v1/keys/claim "
            "events to compute conversion lift per variant."
        ),
        "as_of": datetime.datetime.utcnow().isoformat() + "Z",
    }), 200


@paywall_ab_admin_bp.route("/api/v1/admin/funnel-ab/variants",
                             methods=["GET"])
def funnel_ab_variants():
    """Public-ish: dump the 3 copy variants so admin can preview them
    side-by-side without scraping logs."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    return jsonify({
        "ok":       True,
        "variants": {
            v: {str(k): _copy(copy) for k, copy in body.items()}
            for v, body in _VARIANTS.items()
        },
        "selection_rule": "hash(ip + ua) % 3",
        "log_table":      "ab_funnel_log",
    }), 200
