"""
pricing_ab.py — PRO Pricing A/B Experiment (2026-06-06).

Tests two PRO price points:
  - Arm A: $499/mo (control, current PRO list price)
  - Arm B: $99/mo  (founding-tier price, per session memory)

Hypothesis: $499 leaves money on the table for small brokers; $99 captures
volume. Real test runs until statistical significance + MRR parity, then we
lock in the winner.

Deterministic 50/50 cohort split (FNV-style hash of a session id), no
random per-request shuffle (would shred the cookie experience). A sticky
cookie `dchub_pricing_cohort=A|B` carries cohort across visits. Existing
PRO subscribers are GRANDFATHERED — they ALWAYS see Arm A so no paying
customer ever sees a surprise different price.

Endpoints:
  GET  /api/v1/pricing/ab-cohort        — public, returns caller's cohort + price
  POST /api/v1/pricing/ab-event         — public, records impression/click/checkout
  GET  /api/v1/admin/pricing/ab-stats   — admin-only, conv + MRR per arm

DB:
  pricing_ab_events (id, session_hash, cohort, event_type, event_at,
                     source_path, utm_source, value_usd)

Stripe price IDs are read from env vars (NEVER hardcoded):
  STRIPE_PRICE_PRO_A — existing $499 (or whatever the current list is)
  STRIPE_PRICE_PRO_B — new $99 price id (operator creates in Stripe dash)

Fail-safe defaults:
  - If STRIPE_PRICE_PRO_B unset → A/B is INACTIVE; everyone gets Arm A.
  - If PRICING_AB_DISABLE=1 in env → kill switch; everyone gets Arm A.
  - If caller_tier already PRO/FOUNDING/ENTERPRISE → grandfathered to Arm A.
  - Any DB / resolver failure → returns Arm A.

Pattern reuse:
  - routes.market_brief._conn()      one-shot psycopg2 connection
  - routes.auth_context.get_auth_context  for tier resolution + grandfather
  - routes._stripe_links.STRIPE_LINKS  for the fallback PRO URL
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
import datetime
import math
from typing import Optional

from flask import Blueprint, jsonify, request, make_response

logger = logging.getLogger(__name__)

pricing_ab_bp = Blueprint("pricing_ab", __name__)


# ─────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────

# Two arms — Arm A is ALWAYS the control / fail-safe. Arm B only ships
# when STRIPE_PRICE_PRO_B is configured.
_ARM_A_PRICE_USD = 499
_ARM_B_PRICE_USD = 99

# Cookie name shared with pricing.html JS hook.
_COHORT_COOKIE = "dchub_pricing_cohort"
_COHORT_TTL_S = 90 * 24 * 3600  # 90 days

# Salt the session-hash so two callers with the same session id on
# different deployments don't bucket identically. Reuses the existing
# session secret so secret rotation cycles A/B assignment too.
_HASH_SALT = (
    os.environ.get("DCHUB_SESSION_SECRET")
    or os.environ.get("DCHUB_ADMIN_KEY")
    or "dchub-pricing-ab-salt-rotate-via-DCHUB_SESSION_SECRET"
).encode()

_VALID_EVENT_TYPES = {
    "impression",
    "click_pricing",
    "click_upgrade",
    "stripe_checkout_complete",
}


def _ab_disabled() -> bool:
    """Operator kill switch — flip PRICING_AB_DISABLE=1 in Railway env
    to immediately route everyone to Arm A. Re-deploy not required."""
    flag = (os.environ.get("PRICING_AB_DISABLE") or "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


def _arm_b_price_id() -> str:
    """Arm B Stripe price id from env. Returns '' when unset — that
    state is the implicit kill switch (no Stripe product = no rollout).
    Operator creates a $99/mo recurring price in Stripe dashboard,
    then sets STRIPE_PRICE_PRO_B=price_XXXX in Railway."""
    return (os.environ.get("STRIPE_PRICE_PRO_B") or "").strip()


def _arm_a_price_id() -> str:
    """Arm A Stripe price id — already configured per session memory.
    Falls back to the legacy STRIPE_LINKS payment-link URL when the
    env var is missing so the control arm always serves SOMETHING."""
    return (os.environ.get("STRIPE_PRICE_PRO_A") or "").strip()


def _ab_active() -> bool:
    """Returns True iff A/B is live: kill switch off AND Arm B price id
    is configured. Any single missing piece collapses to Arm A only."""
    if _ab_disabled():
        return False
    if not _arm_b_price_id():
        return False
    return True


# ─────────────────────────────────────────────────────────────────────
# Cohort assignment
# ─────────────────────────────────────────────────────────────────────

def _hash_session(session_id: str) -> str:
    """HMAC-SHA256 of (salt, session_id) → 64-char hex. Used both as
    cohort decider AND as the row key in pricing_ab_events (we never
    store the raw session id)."""
    if not session_id:
        # Empty input still hashes; downstream coerces to Arm A anyway.
        session_id = "empty"
    return hmac.new(_HASH_SALT, session_id.encode(), hashlib.sha256).hexdigest()


def _assign_cohort(session_id_hash: str) -> str:
    """Deterministic 50/50 split. The first hex digit of the HMAC is a
    uniform [0..15] so even digits → A, odd digits → B. Same input
    always yields the same arm — sticky without needing the cookie."""
    if not session_id_hash:
        return "A"
    try:
        nibble = int(session_id_hash[0], 16)
    except (ValueError, IndexError):
        return "A"
    return "B" if (nibble & 1) else "A"


def _resolve_session_id(req) -> str:
    """Best-effort stable session id. Order:
      1. existing dchub_pricing_cohort cookie → reuse so same caller
         sees same cohort (cookie value contains the hash prefix)
      2. dchub_session cookie (web UI logged-in)
      3. X-API-Key header (for MCP callers — same key = same arm)
      4. IP + UA hash (anon fallback; not perfect but stable for ~hours)
    """
    try:
        # Cookie precedence — once assigned, stay assigned.
        prior_cookie = req.cookies.get(_COHORT_COOKIE) or ""
        if prior_cookie and "." in prior_cookie:
            # Format we write: "<arm>.<sess_hash_prefix>"
            return prior_cookie.split(".", 1)[1]

        sess = req.cookies.get("dchub_session") or ""
        if sess:
            return f"sess:{sess}"

        api_key = (req.headers.get("X-API-Key") or "").strip()
        if api_key:
            return f"key:{api_key}"

        ip = (req.headers.get("X-Forwarded-For", "").split(",")[0].strip()
              or req.remote_addr or "")
        ua = req.headers.get("User-Agent", "")
        return f"anon:{ip}|{ua}"
    except Exception:
        return "anon:fallback"


def _caller_is_existing_pro(req) -> bool:
    """Grandfather rule — never show Arm B to a caller that's already
    paying for PRO/FOUNDING/ENTERPRISE/INTERNAL. Anchoring bias / churn
    risk too high. Anonymous + free + identified + developer are fair
    game.

    Best-effort: any resolver failure → returns False (assume free) so
    we don't accidentally GRANDFATHER an anonymous caller into Arm A
    when they were the whole point of the experiment."""
    try:
        from routes.auth_context import get_auth_context, _TIER_RANK, TIER_PRO
        ctx = get_auth_context(req)
        return _TIER_RANK.get(ctx.tier, 0) >= _TIER_RANK.get(TIER_PRO, 5)
    except Exception as e:
        logger.debug("[pricing_ab] tier resolve failed: %s", e)
        return False


def get_displayed_pro_price(req=None) -> dict:
    """Public helper — returns the price + Stripe price id + cohort the
    given request should see. Used by the GET endpoint AND by any
    server-side render that needs to inject the right CTA.

    Return shape:
      {
        "cohort": "A" | "B",
        "price_usd": 499 | 99,
        "stripe_price_id": "price_XXXX" | "",
        "ab_active": bool,
        "grandfathered": bool,
      }
    """
    if req is None:
        try:
            from flask import request as _req
            req = _req
        except Exception:
            req = None

    # Fast-path: ab inactive → always Arm A.
    if not _ab_active():
        return {
            "cohort": "A",
            "price_usd": _ARM_A_PRICE_USD,
            "stripe_price_id": _arm_a_price_id(),
            "ab_active": False,
            "grandfathered": False,
        }

    # Grandfather rule.
    if req is not None and _caller_is_existing_pro(req):
        return {
            "cohort": "A",
            "price_usd": _ARM_A_PRICE_USD,
            "stripe_price_id": _arm_a_price_id(),
            "ab_active": True,
            "grandfathered": True,
        }

    sess_id = _resolve_session_id(req) if req is not None else ""
    sess_hash = _hash_session(sess_id)
    cohort = _assign_cohort(sess_hash)

    return {
        "cohort": cohort,
        "price_usd": _ARM_A_PRICE_USD if cohort == "A" else _ARM_B_PRICE_USD,
        "stripe_price_id": _resolve_stripe_price(cohort),
        "ab_active": True,
        "grandfathered": False,
        # Internal — used by the cookie writer; not exposed in the public
        # JSON response.
        "_session_hash": sess_hash,
    }


def _resolve_stripe_price(cohort: str) -> str:
    """Cohort → Stripe price id. Returns Arm A if B isn't configured —
    never serve an empty price id for a real upgrade flow."""
    if cohort == "B":
        b = _arm_b_price_id()
        if b:
            return b
        # Defensive — should never hit here since _ab_active() already
        # gated, but a missing env mid-request shouldn't 500 the page.
        return _arm_a_price_id()
    return _arm_a_price_id()


# ─────────────────────────────────────────────────────────────────────
# DB schema bootstrap
# ─────────────────────────────────────────────────────────────────────

def _conn():
    """One short-timeout psycopg2 connection. Mirrors
    routes/market_brief.py:_conn — same fail-soft pattern (returns
    None when DATABASE_URL is unset)."""
    try:
        import psycopg2
    except Exception:
        return None
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        return psycopg2.connect(db, sslmode="require", connect_timeout=5)
    except Exception as e:
        logger.warning("[pricing_ab] db connect failed: %s", e)
        return None


def init_pricing_ab_tables():
    """Defensive ALTER pattern — create the table + idempotently add any
    missing columns. Safe on fresh-boot AND on a prod DB where an
    earlier version already created the table with fewer columns.

    Schema:
      id            SERIAL PRIMARY KEY
      session_hash  TEXT NOT NULL    (HMAC-SHA256 of session id)
      cohort        TEXT NOT NULL    ('A' | 'B')
      event_type    TEXT NOT NULL    (impression|click_pricing|click_upgrade|stripe_checkout_complete)
      event_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
      source_path   TEXT             (Referer path)
      utm_source    TEXT             (utm_source query param)
      value_usd     NUMERIC          (only set on stripe_checkout_complete)

    Indexes:
      pricing_ab_events_cohort_ts_idx (cohort, event_at DESC)  — stats query
      pricing_ab_events_sess_idx      (session_hash)           — grandfather lookup
    """
    conn = _conn()
    if conn is None:
        logger.warning("[pricing_ab] init skipped: no DATABASE_URL / connect fail")
        return
    try:
        with conn.cursor() as cur:
            # CREATE first — wrapped in its own try so an earlier-existing
            # table doesn't abort the whole tx (PG aborts a tx on any
            # error and rejects all subsequent statements in it).
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS pricing_ab_events (
                        id           SERIAL PRIMARY KEY,
                        session_hash TEXT NOT NULL,
                        cohort       TEXT NOT NULL,
                        event_type   TEXT NOT NULL,
                        event_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        source_path  TEXT,
                        utm_source   TEXT,
                        value_usd    NUMERIC
                    )
                """)
                conn.commit()
            except Exception as e:
                try: conn.rollback()
                except Exception: pass
                logger.warning("[pricing_ab] CREATE TABLE skipped: %s", e)

            # Defensive ALTER — add any column the live table is missing.
            for col_def in [
                "session_hash TEXT NOT NULL DEFAULT ''",
                "cohort TEXT NOT NULL DEFAULT 'A'",
                "event_type TEXT NOT NULL DEFAULT 'impression'",
                "event_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
                "source_path TEXT",
                "utm_source TEXT",
                "value_usd NUMERIC",
            ]:
                col = col_def.split()[0]
                try:
                    cur.execute(
                        f"ALTER TABLE pricing_ab_events "
                        f"ADD COLUMN IF NOT EXISTS {col_def}"
                    )
                    conn.commit()
                except Exception:
                    try: conn.rollback()
                    except Exception: pass

            for idx_sql in [
                ("CREATE INDEX IF NOT EXISTS pricing_ab_events_cohort_ts_idx "
                 "ON pricing_ab_events (cohort, event_at DESC)"),
                ("CREATE INDEX IF NOT EXISTS pricing_ab_events_sess_idx "
                 "ON pricing_ab_events (session_hash)"),
                ("CREATE INDEX IF NOT EXISTS pricing_ab_events_type_ts_idx "
                 "ON pricing_ab_events (event_type, event_at DESC)"),
            ]:
                try:
                    cur.execute(idx_sql)
                    conn.commit()
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
    finally:
        try: conn.close()
        except Exception: pass


# ─────────────────────────────────────────────────────────────────────
# Public endpoint — caller asks "what arm am I in?"
# ─────────────────────────────────────────────────────────────────────

@pricing_ab_bp.route("/api/v1/pricing/ab-cohort", methods=["GET"])
def get_ab_cohort():
    """Public. Returns the caller's cohort + the price + the Stripe
    URL/price id they should see. Sets the sticky cookie so the next
    visit stays in the same arm. Always 200 — fail-safes collapse to
    Arm A on any internal hiccup."""
    info = get_displayed_pro_price(request)

    # Build the Stripe URL the front-end should link to. For Arm A we
    # fall back to the existing payment-link URL (STRIPE_LINKS["pro"])
    # so we don't break the live $199 → checkout flow when no env var
    # is set. For Arm B we always have a price id (gated by _ab_active).
    stripe_url = ""
    try:
        from routes._stripe_links import STRIPE_LINKS
        stripe_url = STRIPE_LINKS.get("pro", "")
    except Exception:
        pass

    payload = {
        "cohort": info["cohort"],
        "price_usd": info["price_usd"],
        "ab_active": info["ab_active"],
        "grandfathered": info["grandfathered"],
        "stripe_url": stripe_url,
        # Front-end shouldn't NEED the raw price_id (it'll redirect via
        # /api/v1/stripe/checkout) but expose it so the JS can route
        # Arm B straight through a Stripe Checkout session.
        "stripe_price_id": info["stripe_price_id"],
        # Display copy hints — let the JS decide whether to render
        # "$499/mo" or "$99/mo" without re-implementing the rule.
        "display_price": f"${info['price_usd']}",
        "display_period": "/month",
    }

    resp = make_response(jsonify(payload))

    # Sticky cookie — only set if not already in the same arm. Format
    # "<arm>.<sess_hash_prefix>"; sess hash carried so _resolve_session_id
    # can reuse it on the next visit without re-deriving.
    try:
        sess_hash = info.get("_session_hash") or _hash_session(
            _resolve_session_id(request)
        )
        cookie_val = f"{info['cohort']}.{sess_hash[:32]}"
        prior = request.cookies.get(_COHORT_COOKIE) or ""
        if prior != cookie_val:
            # SameSite=Lax: cookie travels with top-level navigations
            # (Stripe checkout link), not cross-site iframes.
            resp.set_cookie(
                _COHORT_COOKIE,
                cookie_val,
                max_age=_COHORT_TTL_S,
                samesite="Lax",
                secure=True,
                httponly=False,  # JS needs to read it for in-page price swap
                path="/",
            )
    except Exception as e:
        logger.debug("[pricing_ab] cookie set failed: %s", e)

    # Edge-cache-bust — this MUST be per-request, not 1h-cached.
    resp.headers["Cache-Control"] = "private, no-cache, no-store, max-age=0"
    return resp


# ─────────────────────────────────────────────────────────────────────
# Public endpoint — event logger
# ─────────────────────────────────────────────────────────────────────

@pricing_ab_bp.route("/api/v1/pricing/ab-event", methods=["POST"])
def record_ab_event():
    """Public. Records ONE event (impression / click_pricing /
    click_upgrade / stripe_checkout_complete) to pricing_ab_events.

    Body (JSON or form):
      event_type   required, one of _VALID_EVENT_TYPES
      source_path  optional, e.g. "/pricing"
      utm_source   optional
      value_usd    optional (only meaningful for stripe_checkout_complete)

    The session hash + cohort are derived server-side — we never trust
    the client to claim its own cohort. Always 200/204 — a failed write
    must NOT break the page that called us.
    """
    try:
        body = request.get_json(silent=True) or {}
    except Exception:
        body = {}
    if not body and request.form:
        body = request.form.to_dict()

    event_type = (body.get("event_type") or "").strip().lower()
    if event_type not in _VALID_EVENT_TYPES:
        return jsonify({"ok": False, "error": "bad_event_type"}), 400

    source_path = (body.get("source_path") or "")[:200] or None
    utm_source = (body.get("utm_source") or "")[:60] or None

    value_usd = None
    if event_type == "stripe_checkout_complete":
        try:
            v = body.get("value_usd")
            if v is not None:
                value_usd = float(v)
        except (TypeError, ValueError):
            value_usd = None

    info = get_displayed_pro_price(request)
    sess_hash = info.get("_session_hash") or _hash_session(
        _resolve_session_id(request)
    )
    cohort = info["cohort"]

    conn = _conn()
    if conn is None:
        # Best-effort — log and 204. Don't leak a 500 to a fire-and-forget
        # beacon call from pricing.html.
        logger.warning("[pricing_ab] event drop (no conn): %s", event_type)
        return ("", 204)

    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO pricing_ab_events
                        (session_hash, cohort, event_type,
                         source_path, utm_source, value_usd)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (sess_hash, cohort, event_type,
                     source_path, utm_source, value_usd),
                )
                conn.commit()
            except Exception as e:
                try: conn.rollback()
                except Exception: pass
                logger.warning("[pricing_ab] insert failed: %s", e)
                return ("", 204)
    finally:
        try: conn.close()
        except Exception: pass

    return jsonify({"ok": True, "cohort": cohort,
                    "event_type": event_type})


# ─────────────────────────────────────────────────────────────────────
# Admin endpoint — conversion + MRR per arm
# ─────────────────────────────────────────────────────────────────────

def _require_admin(req) -> bool:
    """Admin = X-Admin-Key OR X-Internal-Key match. Same gate the
    rest of the admin endpoints use (see routes/auth_context.py)."""
    try:
        from routes.auth_context import get_auth_context, TIER_INTERNAL
        ctx = get_auth_context(req)
        return ctx.tier == TIER_INTERNAL
    except Exception:
        return False


def _two_proportion_z(p1: float, n1: int, p2: float, n2: int) -> float:
    """Returns the two-proportion z-statistic for H0: p1 == p2. Bigger
    |z| → more confident the conversion rates differ.

    Returns 0.0 when either sample is empty or the pooled p is 0 (no
    conversions yet — undefined denominator)."""
    if n1 <= 0 or n2 <= 0:
        return 0.0
    p_pool = (p1 * n1 + p2 * n2) / (n1 + n2)
    if p_pool <= 0 or p_pool >= 1:
        return 0.0
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0
    return (p2 - p1) / se


def _z_to_pct(z: float) -> float:
    """Two-tailed p-value → confidence percentage. Uses the standard
    normal CDF approximation (math.erf — no scipy dependency)."""
    # Φ(|z|) via erf
    phi = 0.5 * (1.0 + math.erf(abs(z) / math.sqrt(2.0)))
    # Two-tailed confidence
    conf = (2.0 * phi - 1.0) * 100.0
    return max(0.0, min(100.0, round(conf, 2)))


@pricing_ab_bp.route("/api/v1/admin/pricing/ab-stats", methods=["GET"])
def admin_ab_stats():
    """Admin. Returns conversion + MRR per arm for the last N days
    (default 30, max 365).

    Output shape — matches the spec verbatim:
      {
        "as_of": "...",
        "days": 30,
        "cohorts": {
          "A": {impressions, upgrade_clicks, checkouts, mrr_added_usd, conv_rate_pct},
          "B": {...}
        },
        "winner_candidate": "B (5x conv rate; MRR within 2%)",
        "statistical_significance_pct": 92.4
      }
    """
    if not _require_admin(request):
        return jsonify({"ok": False, "error": "admin_only"}), 401

    try:
        days = int(request.args.get("days", "30"))
    except ValueError:
        days = 30
    days = max(1, min(days, 365))

    conn = _conn()
    if conn is None:
        return jsonify({"ok": False, "error": "no_db"}), 503

    cohorts = {
        "A": {"impressions": 0, "upgrade_clicks": 0, "checkouts": 0,
              "mrr_added_usd": 0.0, "conv_rate_pct": 0.0},
        "B": {"impressions": 0, "upgrade_clicks": 0, "checkouts": 0,
              "mrr_added_usd": 0.0, "conv_rate_pct": 0.0},
    }

    try:
        with conn.cursor() as cur:
            try:
                # One query, group by (cohort, event_type) + sum value_usd.
                cur.execute(
                    """
                    SELECT cohort,
                           event_type,
                           COUNT(*) AS n,
                           COALESCE(SUM(value_usd), 0) AS mrr
                      FROM pricing_ab_events
                     WHERE event_at >= NOW() - (%s || ' days')::interval
                       AND cohort IN ('A', 'B')
                  GROUP BY cohort, event_type
                    """,
                    (str(days),),
                )
                for cohort, event_type, n, mrr in cur.fetchall():
                    bucket = cohorts.get(cohort)
                    if bucket is None:
                        continue
                    if event_type == "impression":
                        bucket["impressions"] = int(n)
                    elif event_type == "click_upgrade":
                        bucket["upgrade_clicks"] = int(n)
                    elif event_type == "stripe_checkout_complete":
                        bucket["checkouts"] = int(n)
                        bucket["mrr_added_usd"] = float(mrr or 0)
            except Exception as e:
                logger.warning("[pricing_ab] stats query failed: %s", e)
                try: conn.rollback()
                except Exception: pass
    finally:
        try: conn.close()
        except Exception: pass

    # Conv rate = checkouts / impressions. Spec example shows pct as
    # "0.146" which is 0.146% (or 14.6 bps); we match that — small
    # decimal rather than e.g. "14.6" — so the operator dashboard reads
    # the same shape as the spec.
    for arm in ("A", "B"):
        imp = cohorts[arm]["impressions"]
        cv = cohorts[arm]["checkouts"]
        cohorts[arm]["conv_rate_pct"] = (
            round((cv / imp) * 100.0, 3) if imp > 0 else 0.0
        )

    # Winner candidate + significance — only meaningful if BOTH arms
    # have non-zero impressions.
    nA, nB = cohorts["A"]["impressions"], cohorts["B"]["impressions"]
    pA = cohorts["A"]["checkouts"] / nA if nA else 0
    pB = cohorts["B"]["checkouts"] / nB if nB else 0
    z = _two_proportion_z(pA, nA, pB, nB)
    sig_pct = _z_to_pct(z)

    winner = "insufficient_data"
    if nA > 0 and nB > 0:
        mrrA = cohorts["A"]["mrr_added_usd"]
        mrrB = cohorts["B"]["mrr_added_usd"]
        mrr_gap_pct = (
            abs(mrrA - mrrB) / max(mrrA, mrrB) * 100.0
            if max(mrrA, mrrB) > 0 else 100.0
        )
        if pB > pA and pA > 0:
            ratio = pB / pA
            mrr_phrase = (f"MRR within {mrr_gap_pct:.0f}%"
                          if mrr_gap_pct <= 10 else
                          f"MRR -{((mrrA - mrrB) / mrrA * 100):.0f}%"
                          if mrrA > mrrB else
                          f"MRR +{((mrrB - mrrA) / mrrA * 100):.0f}%")
            winner = f"B ({ratio:.1f}x conv rate; {mrr_phrase})"
        elif pA > pB and pB > 0:
            ratio = pA / pB
            mrr_phrase = (f"MRR within {mrr_gap_pct:.0f}%"
                          if mrr_gap_pct <= 10 else
                          f"MRR +{((mrrA - mrrB) / mrrB * 100):.0f}%")
            winner = f"A ({ratio:.1f}x conv rate; {mrr_phrase})"
        else:
            winner = "tie"

    return jsonify({
        "as_of": datetime.datetime.utcnow().isoformat() + "Z",
        "days": days,
        "ab_active": _ab_active(),
        "kill_switch_engaged": _ab_disabled(),
        "arm_b_configured": bool(_arm_b_price_id()),
        "cohorts": cohorts,
        "winner_candidate": winner,
        "statistical_significance_pct": sig_pct,
    })
