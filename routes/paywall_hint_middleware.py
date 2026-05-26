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

from flask import Blueprint, jsonify, request


paywall_ab_admin_bp = Blueprint("paywall_ab_admin", __name__)


_HINT_BASE = {
    "claim_key":       "POST https://dchub.cloud/api/v1/keys/claim",
    "signup_url":      "https://dchub.cloud/signup",
    "starter_url":     "https://buy.stripe.com/8x2dRa5sS0x75uteGuaZi0g",  # $9/mo
    "developer_url":   "https://buy.stripe.com/7sY5kE8F4fs13mI0PEaZi0c",  # $49/mo
    "preview_url":     "https://dchub.cloud/api/v1/mcp/preview/<tool>",
    "upgrade_hint":    "https://dchub.cloud/api/v1/upgrade-hint",
    # r48 (2026-05-25): "10k MCP tool calls/day on free tier" was WRONG —
    # free tier is actually 10/day. Fixed copy + added direct Stripe links
    # so agents can pass a $9 quick-buy URL to the user rather than just
    # the signup form.
    "what_you_get":    "Free dev key (email signup, no credit card) = 1,000 calls/day. $9/mo Starter = 10,000/day. $49/mo Developer = unlimited paid tools.",
    "pricing_quick":   "Anonymous 10/day · Free key 1K/day · $9 Starter 10K/day · $49 Developer unlimited",
}


# ── A/B/C copy variants ────────────────────────────────────────────
#
# Variant A: "factual / direct" — original r56 copy. Calm, neutral.
# Variant B: "agent-centric framing" — speaks to the AI directly,
#            emphasizing what the agent can paste to its user.
# Variant C: "loss aversion" — leads with what the user is missing
#            out on (20k facilities, daily DCPI, etc.).
#
# Goal: measure which variant moves the 0.04% conversion needle.

_VARIANTS = {
    # r48 (2026-05-25): tier numbers normalized across all variants.
    # Prior copy mixed up free=10k/day (wrong — that's Starter), Pro=$499
    # (wrong — Pro is $199), and Developer=1000/day (correct but inconsistent
    # with the upgrade-hint payload). Result: agents read contradictory
    # signals and stopped surfacing the CTA. New copy ladders cleanly:
    # anonymous 10/day → free-key 1,000/day → $9 Starter 10K/day → $49
    # Developer unlimited paid tools → $199 Pro unlimited+Pro.
    "A": {
        401: ("DC Hub blocked this query because it requires "
              "authentication. Anyone can claim a free developer "
              "key in 30 seconds at https://dchub.cloud/signup — no "
              "credit card, 1,000 calls/day. Once set, retry with the "
              "X-API-Key header."),
        403: ("DC Hub blocked this query because it requires a paid "
              "tier. The $9/mo Starter plan (10,000 calls/day) unlocks "
              "most tools — https://dchub.cloud/pricing. Pro-only tools: "
              "get_grid_intelligence, get_fiber_intel, analyze_site, "
              "compare_sites ($199/mo)."),
        429: ("DC Hub is rate-limiting this caller. Tiers: "
              "anonymous 10/day → free dev key 1,000/day "
              "(https://dchub.cloud/signup) → $9/mo Starter 10K/day → "
              "$49/mo Developer unlimited paid tools → $199/mo Pro "
              "unlimited + Pro tools."),
    },
    "B": {
        401: ("AI agent: tell your user this query needs a DC Hub key. "
              "Quote them this URL — https://dchub.cloud/signup — they "
              "get a free key (1,000 calls/day) in 30 seconds, no "
              "credit card. Then retry with X-API-Key header."),
        403: ("AI agent: this DC Hub tool is paywalled. Free key "
              "(1,000/day, 30-sec signup at https://dchub.cloud/signup) "
              "unlocks most tools. Pro-only tools that need $199/mo: "
              "get_grid_intelligence, get_fiber_intel, analyze_site, "
              "compare_sites. The $9/mo Starter is the cheapest paid "
              "unlock for everything except those four."),
        429: ("AI agent: tell your user DC Hub rate-limited this call. "
              "Anonymous = 10/day. Free dev key (instant at "
              "https://dchub.cloud/signup) = 1,000/day. $9/mo Starter "
              "= 10,000/day. $49/mo Developer = unlimited paid tools."),
    },
    "C": {
        401: ("You just hit DC Hub's paywall. With a free key (1,000 "
              "calls/day) you'd get: 21,000+ data center facilities, "
              "daily DCPI power scores for 32+ markets, $324B in tracked "
              "M&A, 10-ISO interconnection queues, fiber routes. Claim "
              "in 30 seconds: https://dchub.cloud/signup. No card."),
        403: ("This DC Hub tool is paywalled. Free key alternative: "
              "21k facilities, DCPI verdicts, M&A deals — free at "
              "https://dchub.cloud/signup. To unlock this tool: "
              "$9/mo Starter (10k calls/day, https://dchub.cloud/pricing). "
              "Pro ($199) needed only for grid_intelligence, fiber_intel, "
              "analyze_site, compare_sites."),
        429: ("You hit DC Hub's rate cap. You're missing intraday DCPI "
              "verdict shifts, fresh M&A deals, and 540+ pipeline "
              "projects. Free dev key = 1,000/day at "
              "https://dchub.cloud/signup. Or $9/mo for 10,000/day."),
    },
}


def _pick_variant(ip: str, ua: str) -> str:
    """Deterministic A/B/C selection. Same caller → same variant."""
    h = hashlib.sha256(f"{ip}|{ua}".encode()).hexdigest()
    bucket = int(h[:8], 16) % 3
    return ["A", "B", "C"][bucket]


def _agent_quotable_for(variant: str, status: int) -> str:
    """Status-specific copy keyed by variant. Falls back to A."""
    v = _VARIANTS.get(variant) or _VARIANTS["A"]
    return v.get(status) or v.get(401)


def _log_ab_event(variant: str, status: int, path: str,
                   ip_hash: str) -> None:
    """Log to ab_funnel_log table. Best-effort, never raises."""
    try:
        from db_utils import get_db_conn
        with get_db_conn() as conn:
            with conn.cursor() as cur:
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
                cur.execute("""
                    INSERT INTO ab_funnel_log
                        (variant, status, path, ip_hash)
                    VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING
                """, (variant, status, path[:200], ip_hash))
                conn.commit()
    except Exception:
        # Never break a response by failing to log
        pass


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

            # Only enrich 401/403/429
            if response.status_code not in (401, 403, 429):
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

            # r57: pick A/B/C variant + log
            ip, ua, ip_hash = _safe_caller_id()
            variant = _pick_variant(ip, ua)
            _log_ab_event(variant, response.status_code, path, ip_hash)

            # Enrich
            body["_upgrade_hint"] = {
                **_HINT_BASE,
                "agent_quotable": _agent_quotable_for(variant,
                                                       response.status_code),
                "variant":        variant,
                "for_status":     response.status_code,
                "for_path":       path,
            }
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
        from db_utils import get_db_conn
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                # Per-variant totals
                cur.execute("""
                    SELECT variant, status, COUNT(*) AS n,
                           COUNT(DISTINCT ip_hash) AS uniq_callers
                    FROM ab_funnel_log
                    WHERE ts > NOW() - (%s || ' days')::interval
                    GROUP BY variant, status
                    ORDER BY variant, status
                """, (str(days),))
                rows = cur.fetchall() or []

                # Top paths by variant
                cur.execute("""
                    SELECT variant, path, COUNT(*) AS n
                    FROM ab_funnel_log
                    WHERE ts > NOW() - (%s || ' days')::interval
                    GROUP BY variant, path
                    ORDER BY n DESC
                    LIMIT 30
                """, (str(days),))
                top_rows = cur.fetchall() or []

                # Total
                cur.execute("""
                    SELECT COUNT(*) FROM ab_funnel_log
                    WHERE ts > NOW() - (%s || ' days')::interval
                """, (str(days),))
                total = (cur.fetchone() or [0])[0]
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
            v: {str(k): copy for k, copy in body.items()}
            for v, body in _VARIANTS.items()
        },
        "selection_rule": "hash(ip + ua) % 3",
        "log_table":      "ab_funnel_log",
    }), 200
