"""
canonical_funnel.py — (2026-06-27)
==================================
ONE source of truth for DC Hub's FUNNEL / GROWTH KPIs, so every surface
(/admin/funnel-health, /api/v1/mcp/funnel, /api/v1/site/stats, mcp_funnel_diag,
the brain) quotes the SAME definition instead of drifting.

Sibling of canonical_stats.py (which owns the CONTENT metrics: facilities,
countries, markets, isos). This module owns the FUNNEL metrics that were the
`cross_surface_metric_divergence` finding — the same number computed N
inconsistent ways inline:

  • active dev keys — 4 different denominators across surfaces:
        funnel_health   WHERE COALESCE(status,'active')='active'   (NULL-as-active → HIGH)
        site_stats      COUNT(*)  no filter at all                 (lifetime → HIGHEST, 175)
        flask_mcp       WHERE status='active'                      (← CANONICAL here)
        strategic_gaps  WHERE tier IN ('paid','enterprise') ...    (paid-only → LOW)
  • MRR — two incompatible bases (plan×price run-rate vs invoiced Stripe cents).
  • conversions 30d — raw vs seed/comp-filtered (9 vs 6).
  • tool calls 7d — same de-loop classifier, different date window (113 vs 117).

Canonical choices (encoded below):
  active_dev_keys    = status='active', ALL tiers (NULL status NOT counted active)
  mrr_run_rate_usd   = users.plan × price-map (the plan run-rate; comp-inflated — keep
                       the honest caveat. real cash-recurring is a FRACTION of this)
  mrr_invoiced_usd   = SUM(mcp_conversions.mrr_cents)/100 over 30d (real Stripe cash)
                       — BOTH are exposed and NAMED so a surface never silently swaps basis.
  conversions_30d_real = stripe_customer_id NOT NULL + seed/comp/NLR excluded (the honest #)
  conversions_30d      = raw COUNT(mcp_conversions) 30d (kept for back-compat)
  tool_calls_7d_real   = de-looped (shared deloop_calls_where), COMPLETE 7 days
                       (CURRENT_DATE-7 .. CURRENT_DATE) — drift-proof, no partial-day wobble.

Fail-safe: mirrors canonical_stats — every query is wrapped; on any error we keep
the LAST-KNOWN-GOOD cached value (or a conservative floor at cold start) and NEVER
raise. A transient DB timeout degrades one field, never the whole dashboard.
"""

from __future__ import annotations

import os
import time
import threading

# Canonical plan → monthly-USD price map (the run-rate basis). This is the SoT
# home for the map; funnel_health.py keeps a local copy for back-compat but
# should import from here. Keep in lock-step with Stripe pricing.
PLAN_MONTHLY_USD = {
    "starter":            9,
    # pro: $99 list since r-price-collapse 2026-09-05. The house rule here is
    # "never above reality", and 99 is now BOTH the floor and the list: checked
    # against live Stripe on 2026-09-05, there is no active subscription above
    # $99/mo except the NLR annual research seed, which is not plan 'pro'.
    # (Was 199 as a conservative floor while $199/$299 pro links were live.)
    # Real cash truth is still mrr_invoiced_usd from mcp_conversions.mrr_cents.
    "developer":          49,
    "pro":                99,
    "pro_annual":         99,    # $1188/yr ≈ $99/mo equivalent
    "pro_annual_onetime": 99,
    # brain-ascension #28 (2026-07-25): team + founding were MISSING — a $699
    # Team or $99 Founding subscriber contributed $0 to every MRR run-rate.
    "team":               699,   # $699/mo, 5 seats (r-reprice 2026-06-19)
    "founding":           99,    # $99/mo r-founder99 link (legacy display $199;
                                 # 99 is the conservative floor for the cohort)
    "enterprise":         500,
    "enterprise_annual":  500,
    "research_seed_nlr":  250,   # $3,000/yr ≈ $250/mo
    "metered":            0,     # PAYG — counted via mcp_call_log if needed
}
_PAID_PLANS = tuple(p for p, v in PLAN_MONTHLY_USD.items() if v > 0)

# Conservative cold-start floors (never above reality; funnel numbers are small,
# so these are near-zero — the point is to never emit a misleading None/0 spike).
_FALLBACK = {
    "active_dev_keys":        0,
    "dev_keys_by_tier":       {},
    "paid_keys":              0,
    "mcp_dev_keys_registered": 0,
    "mrr_run_rate_usd":       0,
    "mrr_invoiced_usd":       0.0,
    "conversions_30d":        0,
    "conversions_30d_real":   0,
    "tool_calls_7d_real":     None,   # None = degraded/unknown, NOT zero
}

_TTL_S = 90           # funnel KPIs move faster than content stats; 90s cache
_cache: dict | None = None
_cache_ts: float = 0.0
_lock = threading.Lock()


def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        import psycopg2
        return psycopg2.connect(db, sslmode="require", connect_timeout=6)
    except Exception:
        return None


def _deloop_where() -> str | None:
    """The SINGLE shared de-loop predicate (no leading AND). Same classifier
    funnel_health + flask_mcp_endpoints use, so this can't introduce a NEW
    divergence. Returns None if the module is unavailable (then we skip the
    de-looped count rather than emit a wrong number)."""
    try:
        from mcp_calls_deloop import deloop_calls_where
        return deloop_calls_where()
    except Exception:
        return None


def _query_live() -> dict:
    """Best-effort live funnel KPIs from canonical definitions. Any per-metric
    failure keeps the last-known-good cached value (canonical_stats pattern);
    never raises."""
    out = dict(_cache) if _cache else dict(_FALLBACK)
    c = _conn()
    if c is None:
        return out
    try:
        # IMPORTANT: every cur.execute below passes NO params tuple — the de-loop
        # predicate contains literal % (LIKE) and execute(sql, ()) would trigger
        # psycopg2 %-substitution and error (the documented empty-tuple % trap).
        cur = c.cursor()

        # ── Active dev keys (CANONICAL: status='active', NULL excluded) ──
        try:
            cur.execute("SELECT tier, COUNT(*) FROM mcp_dev_keys "
                        "WHERE status = 'active' GROUP BY tier")
            tiers: dict = {}
            total = 0
            for tier, n in cur.fetchall() or []:
                n = int(n or 0)
                tiers[str(tier or "unknown")] = n
                total += n
            out["active_dev_keys"] = total
            out["dev_keys_by_tier"] = tiers
            out["paid_keys"] = sum(v for k, v in tiers.items()
                                   if k in ("paid", "enterprise"))
        except Exception:
            try: c.rollback()
            except Exception: pass

        # ── Lifetime registered dev keys (the public social-proof number) ──
        # NOT the same as active — kept distinct & honestly named so the public
        # /api/v1/site/stats "developers" count stops masquerading as active keys.
        try:
            cur.execute("SELECT COUNT(*) FROM mcp_dev_keys")
            out["mcp_dev_keys_registered"] = int((cur.fetchone() or [0])[0] or 0)
        except Exception:
            try: c.rollback()
            except Exception: pass

        # ── MRR run-rate (plan × price map; comp-inflated — honest caveat) ──
        try:
            cur.execute(
                "SELECT plan, COUNT(*) FROM users "
                "WHERE COALESCE(subscription_status,'active')='active' "
                "GROUP BY plan")
            mrr = 0
            for plan, n in cur.fetchall() or []:
                mrr += int(n or 0) * PLAN_MONTHLY_USD.get(str(plan) or "", 0)
            out["mrr_run_rate_usd"] = mrr
        except Exception:
            try: c.rollback()
            except Exception: pass

        # ── MRR invoiced (real Stripe cash, 30d) ──
        try:
            # ★2026-07-28: exclude refunded sales. Refunds were never reversed —
            # no Stripe refund event was handled at all — so a refunded row stayed
            # in the ledger as revenue forever. Two $3,000 rows (one customer,
            # double-billed then refunded by hand) carried 77% of May and 96% of
            # June, manufacturing an apparent 84% MRR "collapse" into July that
            # never happened. The row is KEPT as the audit trail and stamped;
            # money reads filter it out.
            cur.execute("SELECT COALESCE(SUM(mrr_cents),0) FROM mcp_conversions "
                        "WHERE created_at >= NOW() - INTERVAL '30 days' "
                        "  AND refunded_at IS NULL")
            out["mrr_invoiced_usd"] = round(float((cur.fetchone() or [0])[0] or 0) / 100.0, 2)
        except Exception:
            try: c.rollback()
            except Exception: pass

        # ── Conversions 30d (raw + honest-filtered) ──
        try:
            cur.execute("SELECT COUNT(*) FROM mcp_conversions "
                        "WHERE created_at >= NOW() - INTERVAL '30 days'")
            out["conversions_30d"] = int((cur.fetchone() or [0])[0] or 0)
        except Exception:
            try: c.rollback()
            except Exception: pass
        try:
            # Matches routes/funnel_health.py:497 real_conversions_30d exactly.
            cur.execute(
                "SELECT COUNT(*) FROM mcp_conversions "
                "WHERE created_at >= NOW() - INTERVAL '30 days' "
                "  AND stripe_customer_id IS NOT NULL "
                "  AND LOWER(COALESCE(plan_to,'')) NOT IN "
                "      ('comp','complimentary','research_seed_nlr','seed') "
                # ★2026-08-01: seed labels are FREE TEXT — the live NLR rows carry
                # plan_to='Year 1 Research Seed — FY2026 calibration' (the Stripe
                # price nickname), which the NOT IN list can never enumerate.
                # POSITION, not LIKE: this SQL runs with no params tuple, and a
                # literal % re-introduces the empty-tuple %-substitution trap the
                # comment at the top of this function exists to prevent.
                "  AND POSITION('seed' IN LOWER(COALESCE(plan_to,''))) = 0 "
                "  AND LOWER(COALESCE(source,'')) <> 'seed' "
                # ★2026-07-28: a refunded sale is not an honest conversion.
                # Kept out of `conversions_30d` (the documented RAW count) so
                # that field keeps its back-compat meaning.
                "  AND refunded_at IS NULL")
            out["conversions_30d_real"] = int((cur.fetchone() or [0])[0] or 0)
        except Exception:
            try: c.rollback()
            except Exception: pass

        # ── Tool calls 7d (de-looped, COMPLETE 7 days — drift-proof) ──
        _where = _deloop_where()
        if _where:
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM mcp_tool_calls "
                    "WHERE created_at >= CURRENT_DATE - INTERVAL '7 days' "
                    "  AND created_at <  CURRENT_DATE "
                    "  AND " + _where)
                out["tool_calls_7d_real"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                # transient timeout on the slow de-loop scan → keep last-known,
                # never emit a false 0 (the documented degraded-not-zero rule).
                try: c.rollback()
                except Exception: pass
    finally:
        try: c.close()
        except Exception: pass
    return out


def get_canonical_funnel(force: bool = False) -> dict:
    """Cached canonical funnel KPIs. Always returns a complete dict (last-known
    or floors on failure) — never raises. Keys: active_dev_keys, dev_keys_by_tier,
    paid_keys, mcp_dev_keys_registered, mrr_run_rate_usd, mrr_invoiced_usd,
    conversions_30d, conversions_30d_real, tool_calls_7d_real."""
    global _cache, _cache_ts
    now = time.time()
    with _lock:
        if not force and _cache is not None and (now - _cache_ts) < _TTL_S:
            return dict(_cache)
    try:
        live = _query_live()
    except Exception:
        live = dict(_cache) if _cache else dict(_FALLBACK)
    with _lock:
        _cache = live
        _cache_ts = now
    return dict(live)
