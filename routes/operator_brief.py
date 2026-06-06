"""
operator_brief.py — Per-operator Brief pages (2026-06-06).

Mirror of market_brief.py but for OPERATORS. Investors + brokers
comparing operators (Aligned vs QTS vs Digital Realty vs Vantage,
etc.) want a single shareable URL that fact-cites: footprint, market
concentration, pipeline, lease velocity, capital structure, M&A
history, peer rank, 12-month outlook.

Endpoints:
  GET /operators/<slug>/brief         — HTML page (9 sections, paywalled by tier)
  GET /api/v1/operator-brief/<slug>   — JSON of the same data

Pattern reuse:
  - market_brief.py for the section-fan-out + paywall pattern
  - operators.py for the canonical operator slug → provider resolution
  - brain_v2_layer4._call_claude for the Outlook narrative
  - freshness — youngest of (facility last_seen, deal date) per operator

Paywall: anon/free sees Hero + Footprint + Outlook teaser; PRO+ unlocks
Market Concentration + Pipeline + Lease + Capital + M&A + Competitive.

NOTE on schema: discovered_facilities canonical columns are `provider`,
`power_mw`, `status`, `market`, `city`, `state`, `country`,
`merged_at`, `is_duplicate`, `name`, `facility_name`, `eta_year`,
`first_seen`, `last_seen`. The `deals` canonical columns are
`date, buyer, seller, value, mw, type, region, market, asset_name`.
(Bit by capacity_mw vs power_mw on /ai/facts before — power_mw is the
real column on discovered_facilities.)
"""

from __future__ import annotations

import datetime
import os
import re
from flask import Blueprint, Response, jsonify, request

operator_brief_bp = Blueprint("operator_brief", __name__)


# ─────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────

# 10 seed operators — hand-QA'd + pre-warmed by the cron. Beyond these
# the surface still auto-renders for ANY operator the DB knows about
# (the slug resolver falls through to discovered_facilities.provider).
SEED_OPERATORS = (
    "aligned",
    "qts",
    "digital-realty",
    "equinix",
    "vantage",
    "cyrusone",
    "cologix",
    "core-scientific",
    "airtrunk",
    "iron-mountain",
)

# OPERATOR_ALIASES: URL slug → canonical provider name (string used in
# discovered_facilities.provider). Map the marketing/short slug to the
# raw provider string the data uses; the DB resolver does ILIKE matching
# below to catch suffix drift ("Aligned" vs "Aligned Data Centers" vs
# "Aligned, Inc."). Mirrors operators.SLUG_ALIASES but is scoped to the
# 10 seed slugs the spec asks for.
OPERATOR_ALIASES: dict[str, str] = {
    # spec-required
    "aligned":             "Aligned Data Centers",
    "qts":                 "QTS",
    "digital-realty":      "Digital Realty",
    "equinix":             "Equinix",
    "vantage":             "Vantage Data Centers",
    "cyrusone":            "CyrusOne",
    "cologix":             "Cologix",
    "core-scientific":     "Core Scientific",
    "airtrunk":            "AirTrunk",
    "iron-mountain":       "Iron Mountain",
    # convenience aliases — common short forms; these 301 to the
    # canonical seed slug in html_operator_brief().
    "aligned-data-centers":  "Aligned Data Centers",
    "qts-data-centers":      "QTS",
    "vantage-data-centers":  "Vantage Data Centers",
    "iron-mountain-data-centers": "Iron Mountain",
}

# Slug-to-canonical-slug rewrites (used to 301 alternate forms to the seed slug).
_CANONICAL_SLUG: dict[str, str] = {
    "aligned-data-centers":         "aligned",
    "qts-data-centers":             "qts",
    "vantage-data-centers":         "vantage",
    "iron-mountain-data-centers":   "iron-mountain",
}


_PRO_RANK = 4  # tier_registry rank for pro/founding


# ─────────────────────────────────────────────────────────────────────
# DB + helpers
# ─────────────────────────────────────────────────────────────────────

def _conn():
    """One read-only connection — best-effort per section."""
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        return psycopg2.connect(db, sslmode="require", connect_timeout=5)
    except Exception:
        return None


def _norm_slug(s: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", (s or "").strip().lower()).strip("-")


def _canonical(slug: str) -> str:
    s = _norm_slug(slug)
    return _CANONICAL_SLUG.get(s, s)


def _as_float(v):
    try:
        return round(float(v), 2) if v is not None else None
    except (TypeError, ValueError):
        return None


def _as_int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────
# Tier gate (reuse the same resolver as market_brief)
# ─────────────────────────────────────────────────────────────────────

def _caller_tier() -> str:
    try:
        from routes.tier_gate import _resolve_caller_tier
        tier, _ = _resolve_caller_tier()
        return (tier or "FREE").upper()
    except Exception:
        return "FREE"


def _is_pro(tier: str) -> bool:
    t = (tier or "").upper()
    return t in ("PRO", "FOUNDING", "ENTERPRISE", "RESEARCH_SEED", "ADMIN")


# ─────────────────────────────────────────────────────────────────────
# Operator slug → canonical provider string
# ─────────────────────────────────────────────────────────────────────

def _resolve_operator(cur, slug: str) -> str | None:
    """Map a URL slug to the canonical provider name in
    discovered_facilities.provider. Tries (a) OPERATOR_ALIASES, then
    (b) the live operators.SLUG_ALIASES, then (c) ILIKE fuzzy match
    against distinct providers.

    ILIKE catches suffix drift — "Aligned" vs "Aligned Data Centers"
    vs "Aligned, Inc." — by matching on the canonical alias substring.
    Returns the first provider with non-zero facility count for the slug;
    None when nothing plausible matches.
    """
    canonical_slug = _canonical(slug)
    # (a) seed alias map
    alias_target = OPERATOR_ALIASES.get(canonical_slug)
    if not alias_target:
        # (b) borrow operators.SLUG_ALIASES for short forms (aws, azure, etc.)
        try:
            from routes.operators import SLUG_ALIASES
            alias_target = SLUG_ALIASES.get(canonical_slug)
        except Exception:
            alias_target = None
    if alias_target:
        # Use ILIKE on the alias prefix to pick the actual DB string. Picks
        # the provider with the most facilities so suffix drift collapses
        # to the canonical record.
        try:
            cur.execute("""
                SELECT provider, COUNT(*) AS n
                  FROM discovered_facilities
                 WHERE LOWER(provider) ILIKE LOWER(%s)
                   AND provider IS NOT NULL AND provider <> ''
                   AND merged_at IS NULL AND is_duplicate = 0
                 GROUP BY provider
                 ORDER BY n DESC LIMIT 1
            """, (f"{alias_target}%",))
            r = cur.fetchone()
            if r:
                return r[0]
        except Exception:
            pass
        # ILIKE fan-out had no hits — return the alias string anyway so
        # downstream sections render an honest "no facilities tracked".
        return alias_target
    # (c) last resort: slugify every distinct provider + exact match
    try:
        cur.execute("""
            SELECT DISTINCT provider FROM discovered_facilities
             WHERE provider IS NOT NULL AND provider <> ''
               AND merged_at IS NULL AND is_duplicate = 0
        """)
        for row in cur.fetchall():
            if _norm_slug(row[0]) == canonical_slug:
                return row[0]
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────
# Section fan-out (each best-effort)
# ─────────────────────────────────────────────────────────────────────

def _section_hero(cur, provider: str) -> dict | None:
    """Section 1: Hero. Operator name, total MW (operational + pipeline),
    # facilities, # markets. None if no facilities tracked."""
    try:
        cur.execute("""
            SELECT COUNT(*) AS facility_count,
                   COALESCE(SUM(power_mw), 0) AS total_mw,
                   COALESCE(SUM(CASE WHEN LOWER(COALESCE(status,'')) IN
                       ('operational','operating','live','active','running','in-service')
                       THEN power_mw ELSE 0 END), 0) AS operational_mw,
                   COALESCE(SUM(CASE WHEN status ILIKE %s OR status ILIKE %s OR status ILIKE %s
                       THEN power_mw ELSE 0 END), 0) AS pipeline_mw,
                   COUNT(DISTINCT COALESCE(market, city)) AS market_count,
                   COUNT(DISTINCT country) AS country_count,
                   MAX(COALESCE(last_seen, first_seen)) AS last_seen
              FROM discovered_facilities
             WHERE LOWER(COALESCE(provider, '')) = LOWER(%s)
               AND merged_at IS NULL AND is_duplicate = 0
        """, ('%construction%', '%planned%', '%announced%', provider))
        r = cur.fetchone()
    except Exception:
        return None
    if not r or not r[0]:
        return None
    return {
        "name":              provider,
        "facility_count":    _as_int(r[0]),
        "total_mw":          _as_float(r[1]),
        "operational_mw":    _as_float(r[2]),
        "pipeline_mw":       _as_float(r[3]),
        "market_count":      _as_int(r[4]),
        "country_count":     _as_int(r[5]),
        "_last_seen_dt":     r[6],   # internal — drives live-as-of
    }


def _section_footprint(cur, provider: str) -> list[dict]:
    """Section 2: Footprint Map (FREE). List of facilities with
    city/state/MW. Cap at 50 so a 200-facility operator (Equinix)
    doesn't blow up the page."""
    try:
        cur.execute("""
            SELECT COALESCE(facility_name, name) AS facility,
                   city, state, country, market, power_mw, status
              FROM discovered_facilities
             WHERE LOWER(COALESCE(provider, '')) = LOWER(%s)
               AND merged_at IS NULL AND is_duplicate = 0
             ORDER BY power_mw DESC NULLS LAST
             LIMIT 50
        """, (provider,))
        return [{
            "facility": r[0],
            "city":     r[1],
            "state":    r[2],
            "country":  r[3],
            "market":   r[4],
            "power_mw": _as_float(r[5]),
            "status":   r[6],
        } for r in cur.fetchall()]
    except Exception:
        # facility_name may not exist on every shard — fall back to name.
        try:
            cur.execute("""
                SELECT name, city, state, country, market, power_mw, status
                  FROM discovered_facilities
                 WHERE LOWER(COALESCE(provider, '')) = LOWER(%s)
                   AND merged_at IS NULL AND is_duplicate = 0
                 ORDER BY power_mw DESC NULLS LAST
                 LIMIT 50
            """, (provider,))
            return [{
                "facility": r[0], "city": r[1], "state": r[2],
                "country":  r[3], "market": r[4],
                "power_mw": _as_float(r[5]), "status": r[6],
            } for r in cur.fetchall()]
        except Exception:
            return []


def _section_market_concentration(cur, provider: str) -> list[dict]:
    """Section 3: Top 5 markets by MW share (PRO+). Each row links to
    /markets/<slug>/brief — concrete cross-discovery between briefs."""
    try:
        cur.execute("""
            SELECT COALESCE(market, city, '') AS m,
                   COUNT(*) AS n,
                   COALESCE(SUM(power_mw), 0) AS mw
              FROM discovered_facilities
             WHERE LOWER(COALESCE(provider, '')) = LOWER(%s)
               AND merged_at IS NULL AND is_duplicate = 0
               AND COALESCE(market, city) IS NOT NULL
               AND COALESCE(market, city) <> ''
             GROUP BY COALESCE(market, city)
             ORDER BY mw DESC NULLS LAST, n DESC
             LIMIT 5
        """, (provider,))
        rows = cur.fetchall()
    except Exception:
        return []
    # Compute total MW across this operator's footprint so we can
    # surface "share of operator footprint" per-market.
    try:
        cur.execute("""
            SELECT COALESCE(SUM(power_mw), 0)
              FROM discovered_facilities
             WHERE LOWER(COALESCE(provider, '')) = LOWER(%s)
               AND merged_at IS NULL AND is_duplicate = 0
        """, (provider,))
        total = float((cur.fetchone() or (0,))[0] or 0)
    except Exception:
        total = 0.0
    out = []
    for r in rows:
        mw = float(r[2] or 0)
        share = round(100.0 * mw / total, 1) if total else None
        out.append({
            "market":         r[0],
            "market_slug":    _norm_slug(r[0]),
            "facility_count": _as_int(r[1]),
            "total_mw":       _as_float(mw),
            "share_pct":      share,
        })
    return out


def _section_pipeline(cur, provider: str) -> list[dict]:
    """Section 4: Pipeline (PRO+). Under-construction + planned MW
    with timeline."""
    try:
        cur.execute("""
            SELECT COALESCE(facility_name, name) AS facility,
                   city, state, market, power_mw, status, eta_year
              FROM discovered_facilities
             WHERE LOWER(COALESCE(provider, '')) = LOWER(%s)
               AND (status ILIKE %s OR status ILIKE %s OR status ILIKE %s)
               AND merged_at IS NULL AND is_duplicate = 0
             ORDER BY power_mw DESC NULLS LAST
             LIMIT 20
        """, (provider, '%construction%', '%planned%', '%announced%'))
        return [{
            "facility": r[0], "city": r[1], "state": r[2],
            "market":   r[3], "power_mw": _as_float(r[4]),
            "status":   r[5], "eta": r[6],
        } for r in cur.fetchall()]
    except Exception:
        # facility_name / eta_year shard fallback
        try:
            cur.execute("""
                SELECT name, city, state, market, power_mw, status
                  FROM discovered_facilities
                 WHERE LOWER(COALESCE(provider, '')) = LOWER(%s)
                   AND (status ILIKE %s OR status ILIKE %s)
                   AND merged_at IS NULL AND is_duplicate = 0
                 ORDER BY power_mw DESC NULLS LAST LIMIT 20
            """, (provider, '%construction%', '%planned%'))
            return [{
                "facility": r[0], "city": r[1], "state": r[2],
                "market":   r[3], "power_mw": _as_float(r[4]),
                "status":   r[5], "eta": None,
            } for r in cur.fetchall()]
        except Exception:
            return []


def _section_lease_velocity(cur, provider: str) -> dict:
    """Section 5: Lease Velocity (PRO+). # signed leases over 24mo where
    buyer ILIKE operator. Best-effort — deals.type often blank, so we
    surface both "as buyer" and "as seller/landlord" counts."""
    out = {"leases_24mo": 0, "buyer_leases": 0, "seller_leases": 0,
           "recent_leases": []}
    try:
        cur.execute("""
            SELECT COUNT(*)
              FROM deals
             WHERE LOWER(COALESCE(buyer, '')) ILIKE %s
               AND (LOWER(COALESCE(type, '')) LIKE '%%lease%%'
                    OR LOWER(COALESCE(type, '')) LIKE '%%signed%%')
               AND (date IS NULL OR date >= (CURRENT_DATE - INTERVAL '24 months'))
        """, (f"%{provider}%",))
        r = cur.fetchone()
        out["buyer_leases"] = _as_int(r[0]) if r else 0
    except Exception:
        pass
    try:
        cur.execute("""
            SELECT COUNT(*)
              FROM deals
             WHERE LOWER(COALESCE(seller, '')) ILIKE %s
               AND (LOWER(COALESCE(type, '')) LIKE '%%lease%%'
                    OR LOWER(COALESCE(type, '')) LIKE '%%signed%%')
               AND (date IS NULL OR date >= (CURRENT_DATE - INTERVAL '24 months'))
        """, (f"%{provider}%",))
        r = cur.fetchone()
        out["seller_leases"] = _as_int(r[0]) if r else 0
    except Exception:
        pass
    out["leases_24mo"] = (out["buyer_leases"] or 0) + (out["seller_leases"] or 0)
    # Sample of the last 5 lease deals involving the operator.
    try:
        cur.execute("""
            SELECT date, buyer, seller, value, mw, type, region
              FROM deals
             WHERE (LOWER(COALESCE(buyer, '')) ILIKE %s
                    OR LOWER(COALESCE(seller, '')) ILIKE %s)
               AND (LOWER(COALESCE(type, '')) LIKE '%%lease%%'
                    OR LOWER(COALESCE(type, '')) LIKE '%%signed%%')
               AND (date IS NULL OR date >= (CURRENT_DATE - INTERVAL '24 months'))
             ORDER BY date DESC NULLS LAST LIMIT 5
        """, (f"%{provider}%", f"%{provider}%"))
        out["recent_leases"] = [{
            "date":   d[0].isoformat() if hasattr(d[0], "isoformat") else (str(d[0]) if d[0] else None),
            "buyer":  d[1], "seller": d[2],
            "value":  _as_float(d[3]),
            "mw":     _as_float(d[4]),
            "type":   d[5], "region": d[6],
        } for d in cur.fetchall()]
    except Exception:
        pass
    return out


def _section_capital(cur, provider: str) -> dict:
    """Section 6: Capital Structure (PRO+). Latest funding round + lead
    investor. Best-effort: pulls from hyperscaler_deals if there's a hit
    on the operator name, otherwise from deals filtered by funding-ish
    type keywords. Returns None when neither table has a row — the
    HTML renders 'No funding rounds tracked' rather than a fake stub."""
    out = {"latest_round": None, "lead_investor": None, "rounds": []}
    # Try the deals table first for typed funding events.
    try:
        cur.execute("""
            SELECT date, buyer, seller, value, mw, type, asset_name
              FROM deals
             WHERE (LOWER(COALESCE(seller, '')) ILIKE %s
                    OR LOWER(COALESCE(buyer, '')) ILIKE %s)
               AND (LOWER(COALESCE(type, '')) LIKE '%%funding%%'
                    OR LOWER(COALESCE(type, '')) LIKE '%%equity%%'
                    OR LOWER(COALESCE(type, '')) LIKE '%%investment%%'
                    OR LOWER(COALESCE(type, '')) LIKE '%%series%%'
                    OR LOWER(COALESCE(type, '')) LIKE '%%capital%%')
             ORDER BY date DESC NULLS LAST LIMIT 6
        """, (f"%{provider}%", f"%{provider}%"))
        rounds = []
        for r in cur.fetchall():
            rounds.append({
                "date":  r[0].isoformat() if hasattr(r[0], "isoformat") else (str(r[0]) if r[0] else None),
                "lead":  r[1],
                "company": r[2],
                "value": _as_float(r[3]),
                "type":  r[5],
                "asset": r[6],
            })
        if rounds:
            out["rounds"] = rounds
            out["latest_round"] = rounds[0]
            out["lead_investor"] = rounds[0].get("lead")
    except Exception:
        pass
    # Also try hyperscaler_deals (news-derived). Best-effort: the table
    # may or may not exist depending on env.
    if not out["rounds"]:
        try:
            cur.execute("""
                SELECT published_at, headline, value_usd, mw
                  FROM hyperscaler_deals
                 WHERE LOWER(COALESCE(headline, '')) ILIKE %s
                 ORDER BY published_at DESC NULLS LAST LIMIT 4
            """, (f"%{provider}%",))
            for r in cur.fetchall():
                out["rounds"].append({
                    "date":     r[0].isoformat() if hasattr(r[0], "isoformat") else (str(r[0]) if r[0] else None),
                    "lead":     None,
                    "company":  provider,
                    "value":    _as_float(r[2]),
                    "type":     "news",
                    "asset":    (r[1] or "")[:120],
                })
        except Exception:
            pass
        if out["rounds"]:
            out["latest_round"] = out["rounds"][0]
    return out


def _section_ma(cur, provider: str) -> list[dict]:
    """Section 7: M&A History (PRO+). Acquisitions/divestitures last 24mo
    where the operator is buyer OR seller."""
    try:
        cur.execute("""
            SELECT date, buyer, seller, value, mw, type, asset_name, region
              FROM deals
             WHERE (LOWER(COALESCE(buyer, '')) ILIKE %s
                    OR LOWER(COALESCE(seller, '')) ILIKE %s)
               AND (LOWER(COALESCE(type, '')) LIKE '%%acquisition%%'
                    OR LOWER(COALESCE(type, '')) LIKE '%%divest%%'
                    OR LOWER(COALESCE(type, '')) LIKE '%%sale%%'
                    OR LOWER(COALESCE(type, '')) LIKE '%%merger%%'
                    OR LOWER(COALESCE(type, '')) LIKE '%%m&a%%'
                    OR LOWER(COALESCE(type, '')) LIKE '%%buyout%%')
               AND (date IS NULL OR date >= (CURRENT_DATE - INTERVAL '24 months'))
             ORDER BY date DESC NULLS LAST LIMIT 12
        """, (f"%{provider}%", f"%{provider}%"))
        return [{
            "date":   d[0].isoformat() if hasattr(d[0], "isoformat") else (str(d[0]) if d[0] else None),
            "buyer":  d[1], "seller": d[2],
            "value":  _as_float(d[3]),
            "mw":     _as_float(d[4]),
            "type":   d[5], "asset": d[6], "region": d[7],
            "role":   "buyer" if (d[1] and provider.lower() in (d[1] or "").lower()) else "seller",
        } for d in cur.fetchall()]
    except Exception:
        return []


def _section_competitive(cur, hero: dict) -> dict:
    """Section 8: Competitive Position (PRO+). How this operator ranks vs
    peers on MW, market count, and pipeline. Best-effort — if the
    leaderboard query fails we return None and the HTML degrades to
    a 'peer data unavailable' tile."""
    name = hero.get("name") or ""
    own_mw = hero.get("total_mw") or 0
    out = {
        "mw_rank":           None,
        "mw_rank_of":        None,
        "market_count_rank": None,
        "facility_rank":     None,
        "peers":             [],
    }
    try:
        cur.execute("""
            WITH rank_set AS (
              SELECT provider,
                     COUNT(*) AS n,
                     COALESCE(SUM(power_mw), 0) AS mw,
                     COUNT(DISTINCT COALESCE(market, city)) AS markets
                FROM discovered_facilities
               WHERE provider IS NOT NULL AND provider <> ''
                 AND merged_at IS NULL AND is_duplicate = 0
               GROUP BY provider
              HAVING COUNT(*) >= 3
            )
            SELECT provider, n, mw, markets,
                   RANK() OVER (ORDER BY mw DESC NULLS LAST) AS mw_rank,
                   RANK() OVER (ORDER BY markets DESC NULLS LAST) AS markets_rank,
                   RANK() OVER (ORDER BY n DESC NULLS LAST) AS n_rank,
                   COUNT(*) OVER () AS total
              FROM rank_set
             WHERE LOWER(provider) = LOWER(%s)
        """, (name,))
        r = cur.fetchone()
        if r:
            out["mw_rank"]           = _as_int(r[4])
            out["market_count_rank"] = _as_int(r[5])
            out["facility_rank"]     = _as_int(r[6])
            out["mw_rank_of"]        = _as_int(r[7])
    except Exception:
        pass
    # 5 nearest peers by total MW (above + below — gives the broker
    # comparative context). Spec says "how does this operator rank vs
    # others" — surface a numeric rank PLUS the closest peer set.
    try:
        # peers above
        cur.execute("""
            SELECT provider, COUNT(*) AS n, COALESCE(SUM(power_mw), 0) AS mw
              FROM discovered_facilities
             WHERE LOWER(COALESCE(provider, '')) <> LOWER(%s)
               AND provider IS NOT NULL AND provider <> ''
               AND merged_at IS NULL AND is_duplicate = 0
             GROUP BY provider
            HAVING COALESCE(SUM(power_mw), 0) > %s
             ORDER BY COALESCE(SUM(power_mw), 0) ASC LIMIT 2
        """, (name, own_mw))
        peers_above = [{"name": r[0], "facility_count": _as_int(r[1]),
                         "total_mw": _as_float(r[2]), "position": "above"}
                       for r in cur.fetchall()]
        # peers below
        cur.execute("""
            SELECT provider, COUNT(*) AS n, COALESCE(SUM(power_mw), 0) AS mw
              FROM discovered_facilities
             WHERE LOWER(COALESCE(provider, '')) <> LOWER(%s)
               AND provider IS NOT NULL AND provider <> ''
               AND merged_at IS NULL AND is_duplicate = 0
             GROUP BY provider
            HAVING COALESCE(SUM(power_mw), 0) < %s
             ORDER BY COALESCE(SUM(power_mw), 0) DESC LIMIT 3
        """, (name, own_mw))
        peers_below = [{"name": r[0], "facility_count": _as_int(r[1]),
                         "total_mw": _as_float(r[2]), "position": "below"}
                       for r in cur.fetchall()]
        out["peers"] = peers_above + peers_below
    except Exception:
        out["peers"] = []
    return out


def _section_outlook(provider: str, hero: dict, sections: dict) -> dict:
    """Section 9: Outlook narrative (FREE teaser, PRO+ full). Calls
    brain_v2_layer4._call_claude with a tight prompt built from the
    operator's own facts. Falls back to a templated paragraph on
    failure so the URL still 200s.
    """
    out = {"narrative_md": None, "generated_at": None,
           "model_used": None, "rationale": None}

    facts = {
        "operator":        hero.get("name"),
        "facility_count":  hero.get("facility_count"),
        "total_mw":        hero.get("total_mw"),
        "operational_mw":  hero.get("operational_mw"),
        "pipeline_mw":     hero.get("pipeline_mw"),
        "market_count":    hero.get("market_count"),
        "country_count":   hero.get("country_count"),
        "top_markets":     [m.get("market") for m in (sections.get("market_concentration") or [])][:3],
        "pipeline_top":    [(p.get("facility"), p.get("power_mw")) for p in (sections.get("pipeline") or [])][:3],
        "ma_count":        len(sections.get("ma") or []),
        "lease_velocity":  (sections.get("lease_velocity") or {}).get("leases_24mo"),
        "competitive":     sections.get("competitive") or {},
    }
    sys_prompt = (
        "You are DC Hub's data-center analyst. Write a single 80-120 word "
        "paragraph forecasting this operator's next 12 months. Lead with the "
        "headline (where they win or struggle), then ONE concrete number "
        "from the facts (MW, market rank, lease count). End with a "
        "next-watch trigger. Don't fabricate numbers; use only the facts dict. "
        "Plain prose, no markdown headers."
    )
    user_prompt = (
        f"Operator: {provider}\n\nFacts (JSON-ish):\n{facts}\n\n"
        f"Write the 12-month outlook paragraph."
    )
    try:
        from routes.brain_v2_layer4 import _call_claude
        text, err = _call_claude(user_prompt, sys_prompt)
        if text and not err:
            out["narrative_md"] = text.strip()
            out["model_used"]   = "brain_v2_layer4"
            out["generated_at"] = datetime.datetime.utcnow().isoformat() + "Z"
            return out
    except Exception:
        pass
    # Fallback template — honest about what we have. No fake forward-looking
    # numbers; sticks to the facts already on the hero.
    mw    = hero.get("total_mw") or 0
    mkts  = hero.get("market_count") or 0
    pipe  = hero.get("pipeline_mw") or 0
    out["narrative_md"] = (
        f"{provider} operates {hero.get('facility_count') or 0} tracked "
        f"facilities across {mkts} market{'s' if mkts != 1 else ''} totaling "
        f"{mw:,.0f} MW, with {pipe:,.0f} MW in pipeline. A Claude-written "
        "12-month outlook will be generated on the next brain cycle — this "
        "is the data-only fallback (LLM call unavailable at render time)."
    )
    out["rationale"] = "fallback_template_outlook"
    return out


# ─────────────────────────────────────────────────────────────────────
# Live-as-of timestamp — youngest of (facility last_seen, deal date).
# ─────────────────────────────────────────────────────────────────────

def _live_as_of(cur, hero: dict) -> dict:
    youngest = hero.get("_last_seen_dt")
    name = hero.get("name") or ""
    try:
        cur.execute("""
            SELECT MAX(date)
              FROM deals
             WHERE LOWER(COALESCE(buyer, '')) ILIKE %s
                OR LOWER(COALESCE(seller, '')) ILIKE %s
        """, (f"%{name}%", f"%{name}%"))
        r = cur.fetchone()
        if r and r[0]:
            try:
                _d = r[0]
                if not hasattr(_d, "isoformat"):
                    _d = datetime.datetime.fromisoformat(str(_d))
                if youngest is None or _d > youngest:
                    youngest = _d
            except Exception:
                pass
    except Exception:
        pass
    if youngest is None:
        return {"iso": None, "age_hours": None}
    age_h = None
    try:
        now = datetime.datetime.utcnow()
        ts = youngest
        if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        age_h = round((now - ts).total_seconds() / 3600.0, 1)
    except Exception:
        pass
    return {"iso": youngest.isoformat() if hasattr(youngest, "isoformat") else str(youngest),
            "age_hours": age_h}


# ─────────────────────────────────────────────────────────────────────
# Build full brief
# ─────────────────────────────────────────────────────────────────────

def _build_brief(slug: str, tier: str) -> dict:
    canonical = _canonical(slug)
    requested = _norm_slug(slug)
    redirect_to = canonical if canonical != requested else None

    is_pro = _is_pro(tier)
    out: dict = {
        "ok":          False,
        "slug":        canonical,
        "requested":   requested,
        "redirect_to": redirect_to,
        "tier":        tier,
        "is_pro":      is_pro,
    }
    c = _conn()
    if c is None:
        out["error"] = "no_database"
        return out
    try:
        with c.cursor() as cur:
            provider = _resolve_operator(cur, canonical)
            if not provider:
                out["error"] = "operator_not_found"
                try:
                    cur.execute("""
                        SELECT provider, COUNT(*) AS n
                          FROM discovered_facilities
                         WHERE provider IS NOT NULL AND provider <> ''
                           AND merged_at IS NULL AND is_duplicate = 0
                         GROUP BY provider
                         ORDER BY n DESC LIMIT 8
                    """)
                    out["sample_operators"] = [r[0] for r in cur.fetchall()]
                except Exception:
                    out["sample_operators"] = []
                return out
            hero = _section_hero(cur, provider)
            if not hero:
                out["error"] = "operator_not_found"
                out["sample_operators"] = list(SEED_OPERATORS)
                return out
            # FREE sections (hero + footprint always rendered)
            footprint = _section_footprint(cur, provider)
            live = _live_as_of(cur, hero)
            hero_public = {k: v for k, v in hero.items() if not k.startswith("_")}
            out["hero"] = hero_public
            out["footprint"] = footprint
            out["live_as_of"] = live
            # PRO sections
            sections = {}
            if is_pro:
                sections["market_concentration"] = _section_market_concentration(cur, provider)
                sections["pipeline"]    = _section_pipeline(cur, provider)
                sections["lease_velocity"] = _section_lease_velocity(cur, provider)
                sections["capital"]     = _section_capital(cur, provider)
                sections["ma"]          = _section_ma(cur, provider)
                sections["competitive"] = _section_competitive(cur, hero)
                out["market_concentration"] = sections["market_concentration"]
                out["pipeline"]    = sections["pipeline"]
                out["lease_velocity"] = sections["lease_velocity"]
                out["capital"]     = sections["capital"]
                out["ma"]          = sections["ma"]
                out["competitive"] = sections["competitive"]
            else:
                out["market_concentration"] = []
                out["pipeline"]    = []
                out["lease_velocity"] = None
                out["capital"]     = None
                out["ma"]          = []
                out["competitive"] = None
                out["paywall"]     = {
                    "required_tier": "PRO",
                    "checkout_url":  "/pricing?utm_source=operator_brief",
                    "blurb":         ("Market Concentration, Pipeline, Lease Velocity, "
                                      "Capital Structure, M&A History, and Competitive "
                                      "Position are unlocked for PRO subscribers — "
                                      "$499/mo, all operators."),
                }
            # Outlook narrative (free teaser at render time)
            out["outlook"] = _section_outlook(provider, hero, sections)
        out["ok"] = True
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


# ─────────────────────────────────────────────────────────────────────
# HTML rendering
# ─────────────────────────────────────────────────────────────────────

def _render_html(brief: dict) -> str:
    slug = brief.get("slug") or ""
    hero = brief.get("hero") or {}
    live = brief.get("live_as_of") or {}
    outlook = brief.get("outlook") or {}
    is_pro = bool(brief.get("is_pro"))

    name = hero.get("name") or slug.replace("-", " ").title()
    fac_count = hero.get("facility_count") or 0
    total_mw  = hero.get("total_mw") or 0
    op_mw     = hero.get("operational_mw") or 0
    pipe_mw   = hero.get("pipeline_mw") or 0
    mkt_count = hero.get("market_count") or 0
    country_count = hero.get("country_count") or 0

    live_iso = live.get("iso") or ""
    live_age = live.get("age_hours")
    live_age_str = f"{live_age:.1f}h" if isinstance(live_age, (int, float)) else "—"
    citation_iso = (live_iso or "")[:19].replace("T", " ")

    def _fmt_mw(v):
        if v is None:
            return "—"
        try:
            return f"{float(v):,.0f} MW"
        except (TypeError, ValueError):
            return "—"

    def _fmt_int(v):
        if v is None:
            return "—"
        try:
            return f"{int(v):,}"
        except (TypeError, ValueError):
            return "—"

    def _money(v):
        if v is None:
            return "—"
        try:
            return f"${float(v):,.0f}"
        except (TypeError, ValueError):
            return "—"

    def _row(cells):
        return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"

    # ── KPI hero tiles ──────────────────────────────────────────────
    kpi_pairs = [
        ("Total MW",     _fmt_mw(total_mw)),
        ("Operational",  _fmt_mw(op_mw)),
        ("Pipeline",     _fmt_mw(pipe_mw)),
        ("Facilities",   _fmt_int(fac_count)),
        ("Markets",      _fmt_int(mkt_count)),
        ("Countries",    _fmt_int(country_count)),
    ]
    kpi_html = "\n".join(
        f'<div class="kpi"><span class="kpi-l">{lab}</span>'
        f'<span class="kpi-v">{val}</span></div>'
        for lab, val in kpi_pairs)

    # ── Footprint (FREE — always rendered) ──────────────────────────
    footprint = brief.get("footprint") or []
    foot_rows = "\n".join(
        _row([(f.get("facility") or "—"),
              (f.get("city") or "—"),
              (f.get("state") or f.get("country") or "—"),
              _fmt_mw(f.get("power_mw")),
              (f.get("status") or "—")])
        for f in footprint[:25]
    ) or _row(["—", "No facilities tracked", "—", "—", "—"])
    foot_html = (
        '<table><thead><tr><th>Facility</th><th>City</th>'
        '<th>State/Country</th><th>Power</th><th>Status</th></tr></thead>'
        f'<tbody>{foot_rows}</tbody></table>'
    )
    if len(footprint) > 25:
        foot_html += (f'<p class="sub" style="margin-top:.5rem">'
                      f'Showing top 25 of {len(footprint)} tracked facilities · '
                      f'<a href="/api/v1/operator-brief/{slug}">JSON for full list</a></p>')

    # ── PRO sections ────────────────────────────────────────────────
    mc = brief.get("market_concentration") or []
    pipe = brief.get("pipeline") or []
    lv = brief.get("lease_velocity") or {}
    cap = brief.get("capital") or {}
    ma = brief.get("ma") or []
    comp = brief.get("competitive") or {}

    if is_pro:
        # Market concentration with /markets/<slug>/brief deep-links
        mc_rows = "\n".join(
            _row([
                f'<a href="/markets/{m.get("market_slug")}/brief" '
                f'style="color:#a5b4fc">{m.get("market") or "—"}</a>',
                _fmt_int(m.get("facility_count")),
                _fmt_mw(m.get("total_mw")),
                (f"{m['share_pct']:.1f}%" if m.get("share_pct") is not None else "—"),
            ])
            for m in mc
        ) or _row(["—", "No market concentration data", "—", "—"])
        mc_html = (
            '<table><thead><tr><th>Market</th><th>Facilities</th>'
            '<th>Total MW</th><th>Share of footprint</th></tr></thead>'
            f'<tbody>{mc_rows}</tbody></table>'
        )

        # Pipeline — defaults to "No pipeline tracked" for operators
        # with thin pipeline records (Iron Mountain often falls here).
        pipe_rows = "\n".join(
            _row([(p.get("facility") or "—"),
                  (p.get("city") or "—"),
                  (p.get("market") or "—"),
                  _fmt_mw(p.get("power_mw")),
                  (p.get("status") or "—"),
                  (p.get("eta") or "—")])
            for p in pipe
        ) or _row(["—", "No pipeline tracked", "—", "—", "—", "—"])
        pipe_html = (
            '<table><thead><tr><th>Facility</th><th>City</th>'
            '<th>Market</th><th>Power</th><th>Status</th><th>ETA</th></tr></thead>'
            f'<tbody>{pipe_rows}</tbody></table>'
        )

        # Lease velocity — surface the count + 5 recent rows
        lease_count = lv.get("leases_24mo") or 0
        buyer_c     = lv.get("buyer_leases") or 0
        seller_c    = lv.get("seller_leases") or 0
        lease_top   = lv.get("recent_leases") or []
        lease_rows  = "\n".join(
            _row([(d.get("date") or "—"),
                  (d.get("buyer") or "—"),
                  (d.get("seller") or "—"),
                  _money(d.get("value")),
                  _fmt_mw(d.get("mw"))])
            for d in lease_top
        ) or _row(["—", "No recent leases tracked", "—", "—", "—"])
        lv_html = (
            f'<div class="grid3" style="margin-bottom:1rem">'
            f'<div class="cell"><b>24mo lease count</b><span>{lease_count}</span></div>'
            f'<div class="cell"><b>As tenant</b><span>{buyer_c}</span></div>'
            f'<div class="cell"><b>As landlord</b><span>{seller_c}</span></div>'
            f'</div>'
            '<table><thead><tr><th>Date</th><th>Tenant</th>'
            '<th>Landlord</th><th>Value</th><th>MW</th></tr></thead>'
            f'<tbody>{lease_rows}</tbody></table>'
        )

        # Capital structure
        rounds = cap.get("rounds") or []
        if rounds:
            latest = cap.get("latest_round") or {}
            cap_top = (
                f'<div class="grid3">'
                f'<div class="cell"><b>Latest round</b><span>{latest.get("date") or "—"}</span></div>'
                f'<div class="cell"><b>Lead</b><span>{latest.get("lead") or "—"}</span></div>'
                f'<div class="cell"><b>Value</b><span>{_money(latest.get("value"))}</span></div>'
                f'</div>'
            )
            cap_rows = "\n".join(
                _row([(d.get("date") or "—"),
                      (d.get("lead") or "—"),
                      (d.get("type") or "—"),
                      _money(d.get("value")),
                      (d.get("asset") or "")[:60]])
                for d in rounds
            )
            cap_html = cap_top + (
                '<table><thead><tr><th>Date</th><th>Lead</th>'
                '<th>Type</th><th>Value</th><th>Note</th></tr></thead>'
                f'<tbody>{cap_rows}</tbody></table>'
            )
        else:
            cap_html = (
                '<div class="cell" style="display:block;padding:1.1rem">'
                f'No funding rounds tracked for {name} in the last 24 months. '
                'Capital activity for private operators is often disclosed via '
                'press releases — this section will populate as the deals '
                'crawler picks them up.</div>'
            )

        # M&A history
        ma_rows = "\n".join(
            _row([(d.get("date") or "—"),
                  (d.get("buyer") or "—"),
                  (d.get("seller") or "—"),
                  _money(d.get("value")),
                  _fmt_mw(d.get("mw")),
                  (d.get("type") or "—")])
            for d in ma
        ) or _row(["—", "No M&A activity in the last 24 months", "—", "—", "—", "—"])
        ma_html = (
            '<table><thead><tr><th>Date</th><th>Buyer</th>'
            '<th>Seller</th><th>Value</th><th>MW</th><th>Type</th></tr></thead>'
            f'<tbody>{ma_rows}</tbody></table>'
        )

        # Competitive — rank + nearest peers
        if comp and (comp.get("mw_rank") or comp.get("peers")):
            rank_html = (
                f'<div class="grid3" style="margin-bottom:1rem">'
                f'<div class="cell"><b>MW rank</b><span>#{comp.get("mw_rank") or "—"}'
                f' of {comp.get("mw_rank_of") or "—"}</span></div>'
                f'<div class="cell"><b>Market-count rank</b><span>#{comp.get("market_count_rank") or "—"}</span></div>'
                f'<div class="cell"><b>Facility rank</b><span>#{comp.get("facility_rank") or "—"}</span></div>'
                f'</div>'
            )
            peer_rows = "\n".join(
                _row([
                    f'<a href="/operators/{_norm_slug(p.get("name") or "")}/brief" style="color:#a5b4fc">{p.get("name") or "—"}</a>',
                    _fmt_int(p.get("facility_count")),
                    _fmt_mw(p.get("total_mw")),
                    (p.get("position") or "—"),
                ])
                for p in (comp.get("peers") or [])
            ) or _row(["—", "No peer data", "—", "—"])
            comp_html = rank_html + (
                '<h3 class="sub">Nearest peers (by total MW)</h3>'
                '<table><thead><tr><th>Operator</th><th>Facilities</th>'
                '<th>Total MW</th><th>Position</th></tr></thead>'
                f'<tbody>{peer_rows}</tbody></table>'
            )
        else:
            comp_html = (
                '<div class="cell" style="display:block;padding:1.1rem">'
                'Peer comparison data unavailable for this operator '
                '(needs at least 3 tracked facilities to enter the peer set).</div>'
            )
    else:
        blur = (
            '<div class="blur-card">'
            '<div class="blur-overlay">'
            '<div class="blur-title">PRO unlocks all operator sections</div>'
            '<div class="blur-body">Market Concentration · Pipeline · Lease Velocity '
            '· Capital Structure · M&amp;A History · Competitive Position — every '
            'operator, live updates, share-ready URL.</div>'
            '<a class="cta" href="/pricing?utm_source=operator_brief">Unlock with PRO — $499/mo</a>'
            '</div>'
            '<div class="blur-fake">'
            '<div class="fake-row"></div><div class="fake-row"></div>'
            '<div class="fake-row"></div><div class="fake-row"></div>'
            '</div></div>'
        )
        mc_html = pipe_html = lv_html = cap_html = ma_html = comp_html = blur

    # ── Outlook narrative ────────────────────────────────────────────
    narrative = outlook.get("narrative_md") or ""
    if not is_pro:
        words = narrative.split()
        narrative = " ".join(words[:60])
        if len(words) > 60:
            narrative += "… <em>(continued for PRO subscribers)</em>"
    out_paragraphs = "".join(
        f"<p>{p.strip()}</p>" for p in narrative.split("\n\n") if p.strip()
    ) or "<p>Outlook narrative will be generated on the next cron pass.</p>"

    # ── Share URLs ──────────────────────────────────────────────────
    page_url = f"https://dchub.cloud/operators/{slug}/brief"
    share_x = (f"https://twitter.com/intent/tweet?text="
               f"{name.replace(' ', '+')}+data+center+operator+brief+"
               f"%E2%80%94+{int(total_mw):,}+MW+across+{mkt_count}+markets"
               f"&url={page_url}")
    share_li = f"https://www.linkedin.com/sharing/share-offsite/?url={page_url}"

    citation = (f"DC Hub · <a href=\"{page_url}\">{page_url}</a> · "
                f"Live as of {citation_iso} UTC")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} Operator Brief · DC Hub</title>
<meta name="description" content="{name} data center operator brief — {fac_count} facilities, {total_mw:,.0f} MW total, {mkt_count} markets, {country_count} countries. Live footprint, pipeline, lease velocity, capital, M&amp;A, competitive position, 12-month outlook.">
<meta name="robots" content="index,follow">
<link rel="canonical" href="{page_url}">
<meta property="og:title" content="{name} Operator Brief · DC Hub">
<meta property="og:description" content="{fac_count} facilities · {total_mw:,.0f} MW · {mkt_count} markets · Live as of {citation_iso} UTC">
<meta property="og:url" content="{page_url}">
<meta property="og:type" content="article">
<script type="application/ld+json">{{
 "@context":"https://schema.org","@type":"Organization",
 "name":"{name}","url":"https://dchub.cloud/operators/{slug}/brief",
 "description":"Data center operator tracked by DC Hub. {fac_count} facilities, {total_mw:,.0f} MW total, {country_count} countries."
}}</script>
<link rel="stylesheet" href="/static/dchub-brand.css">
<style>
:root{{--bg:#0a0a0f;--surf:#131319;--b:rgba(255,255,255,0.08);--tx:#fafafa;--mut:#a1a1aa;--dim:#71717a;--ind:#818cf8;--grad:linear-gradient(135deg,#6366f1,#a855f7)}}
*{{box-sizing:border-box}}
body{{font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,sans-serif;max-width:980px;margin:0 auto;padding:2.5rem 1.25rem;background:var(--bg);color:#d4d4d8;line-height:1.65;-webkit-font-smoothing:antialiased}}
h1{{font-weight:700;letter-spacing:-.02em;margin:0 0 .25rem;font-size:2.4rem;color:var(--tx)}}
h2{{font-size:1.25rem;font-weight:600;color:var(--tx);margin:2.25rem 0 .75rem;letter-spacing:-.01em}}
h3.sub{{font-size:.9rem;font-weight:600;color:var(--mut);margin:1.5rem 0 .5rem;letter-spacing:.02em;text-transform:uppercase}}
.live-pill{{display:inline-flex;align-items:center;gap:.4rem;background:var(--surf);border:1px solid var(--b);border-radius:999px;padding:.35rem .8rem;font-size:.72rem;color:var(--mut);font-family:'JetBrains Mono',monospace;margin-left:.5rem}}
.live-dot{{width:.5rem;height:.5rem;background:#10b981;border-radius:50%;animation:pulse 2.5s ease-in-out infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.35}}}}
.sub{{color:var(--dim);font-size:.85rem;margin:0 0 1.5rem;font-family:'JetBrains Mono',monospace}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:.75rem;margin:1rem 0 2rem}}
.kpi{{background:var(--surf);border:1px solid var(--b);border-radius:12px;padding:.95rem 1.1rem;display:flex;flex-direction:column;gap:.35rem}}
.kpi-l{{font-size:.68rem;color:var(--dim);text-transform:uppercase;letter-spacing:.06em;font-family:'JetBrains Mono',monospace}}
.kpi-v{{font-size:1.35rem;color:var(--tx);font-weight:600;letter-spacing:-.01em}}
.grid3{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.6rem;margin:.5rem 0 1.5rem}}
.cell{{background:var(--surf);border:1px solid var(--b);border-radius:10px;padding:.75rem .95rem;display:flex;justify-content:space-between;align-items:center;font-size:.88rem}}
.cell b{{color:var(--dim);font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;font-weight:500;font-family:'JetBrains Mono',monospace}}
.cell span{{color:var(--tx);font-weight:600;font-family:'JetBrains Mono',monospace}}
table{{width:100%;border-collapse:collapse;margin:.5rem 0 1.5rem;font-size:.88rem;background:var(--surf);border:1px solid var(--b);border-radius:10px;overflow:hidden}}
th,td{{padding:.55rem .85rem;text-align:left;border-bottom:1px solid var(--b)}}
th{{background:rgba(255,255,255,0.025);font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;color:var(--dim);font-family:'JetBrains Mono',monospace;font-weight:500}}
td{{color:var(--tx);font-family:'JetBrains Mono',monospace}}
tbody tr:last-child td{{border-bottom:none}}
.outlook p{{font-size:1.02rem;margin:1rem 0;color:#d4d4d8}}
.share{{display:flex;gap:.5rem;flex-wrap:wrap;margin:2rem 0}}
.share a{{background:var(--surf);border:1px solid var(--b);border-radius:8px;padding:.5rem 1rem;color:var(--ind);text-decoration:none;font-size:.85rem;font-family:'JetBrains Mono',monospace}}
.share a:hover{{border-color:var(--ind)}}
.blur-card{{position:relative;background:var(--surf);border:1px solid var(--b);border-radius:14px;padding:2rem;margin:.5rem 0 1.5rem;overflow:hidden;min-height:180px}}
.blur-overlay{{position:absolute;inset:0;background:linear-gradient(180deg,rgba(10,10,15,.4) 0%,rgba(10,10,15,.92) 100%);display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:1.5rem;z-index:2;backdrop-filter:blur(6px)}}
.blur-title{{font-size:1.05rem;font-weight:600;color:var(--tx);margin-bottom:.5rem}}
.blur-body{{font-size:.85rem;color:var(--mut);max-width:400px;margin-bottom:1.25rem}}
.cta{{background:var(--grad);color:#fff;text-decoration:none;padding:.65rem 1.4rem;border-radius:8px;font-weight:600;font-size:.9rem;letter-spacing:.01em}}
.blur-fake{{filter:blur(8px);opacity:.45}}
.fake-row{{height:1.5rem;background:linear-gradient(90deg,#27272a 0%,#3f3f46 50%,#27272a 100%);margin:.5rem 0;border-radius:6px}}
.citation{{background:var(--surf);border:1px solid var(--b);border-radius:10px;padding:.85rem 1rem;font-size:.78rem;color:var(--mut);font-family:'JetBrains Mono',monospace;margin:2rem 0 1rem}}
.citation a{{color:var(--ind);text-decoration:none}}
.footer{{color:var(--dim);font-size:.78rem;margin-top:1.5rem;padding-top:1.25rem;border-top:1px solid var(--b);font-family:'JetBrains Mono',monospace}}
.footer a{{color:var(--ind);text-decoration:none}}
</style>
</head>
<body>

<h1>{name}<span class="live-pill"><span class="live-dot"></span>Live · {live_age_str} fresh</span></h1>
<p class="sub">Operator Brief · {fac_count:,} facilities · {total_mw:,.0f} MW · {mkt_count} markets · Live as of {citation_iso} UTC</p>

<h2>At a Glance</h2>
<div class="kpis">
{kpi_html}
</div>

<h2>Footprint</h2>
{foot_html}

<h2>Market Concentration</h2>
{mc_html}

<h2>Pipeline</h2>
{pipe_html}

<h2>Lease Velocity (24mo)</h2>
{lv_html}

<h2>Capital Structure</h2>
{cap_html}

<h2>M&amp;A History (24mo)</h2>
{ma_html}

<h2>Competitive Position</h2>
{comp_html}

<h2>12-Month Outlook</h2>
<div class="outlook">
{out_paragraphs}
</div>

<div class="share">
  <a href="{share_x}" target="_blank" rel="noopener">Share on X</a>
  <a href="{share_li}" target="_blank" rel="noopener">Share on LinkedIn</a>
  <a href="#" onclick="navigator.clipboard.writeText('{page_url}');this.textContent='Copied!';return false;">Copy URL</a>
  <a href="/operators/{slug}">Full operator profile</a>
</div>

<div class="citation">{citation}</div>

<p class="footer">Powered by <a href="https://dchub.cloud">DC Hub</a> · Source-of-truth data center operator intelligence · 2,000+ tracked deals · JSON: <a href="/api/v1/operator-brief/{slug}">/api/v1/operator-brief/{slug}</a></p>

<script src="/js/dchub-nav.js" defer></script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────
# Pre-warm helper (called by crawler_scheduler._run_operator_brief_warm)
# ─────────────────────────────────────────────────────────────────────

def prewarm_seed_operators() -> dict:
    """Pre-build the brief for each seed operator. Best-effort; sleeps
    1s between operators to avoid hammering the 1-replica backend pool."""
    import time as _time
    out: dict = {"warmed": 0, "errors": 0, "slugs": []}
    n = len(SEED_OPERATORS)
    for i, slug in enumerate(SEED_OPERATORS):
        try:
            brief = _build_brief(slug, tier="ADMIN")
            ok = bool(brief.get("ok"))
            out["slugs"].append({"slug": slug, "ok": ok,
                                 "error": brief.get("error")})
            if ok:
                out["warmed"] += 1
            else:
                out["errors"] += 1
        except Exception as e:
            out["errors"] += 1
            out["slugs"].append({"slug": slug, "ok": False,
                                 "error": f"{type(e).__name__}:{str(e)[:80]}"})
        if i < n - 1:
            try:
                _time.sleep(1.0)
            except Exception:
                pass
    return out


# ─────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────

@operator_brief_bp.route("/api/v1/operator-brief/<slug>", methods=["GET"])
def api_operator_brief(slug):
    """JSON of the brief — same shape as the HTML page."""
    tier = _caller_tier()
    brief = _build_brief(slug, tier)
    status = 200 if brief.get("ok") else (404 if brief.get("error") == "operator_not_found" else 200)
    resp = jsonify(brief)
    # 6h edge cache per spec
    resp.headers["Cache-Control"] = "public, max-age=21600, s-maxage=21600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, status


@operator_brief_bp.route("/operators/<slug>/brief", methods=["GET"])
def html_operator_brief(slug):
    """HTML render — 9 sections, paywalled by tier."""
    tier = _caller_tier()
    brief = _build_brief(slug, tier)
    if not brief.get("ok") and brief.get("error") == "operator_not_found":
        sample = brief.get("sample_operators") or list(SEED_OPERATORS)
        sample_html = ", ".join(
            f'<a href="/operators/{_norm_slug(s)}/brief">{s}</a>' for s in sample[:8])
        body = (
            f'<h1>Operator Brief — {slug}</h1>'
            f'<p class="sub">This operator is not yet in our tracked set. Try one of: {sample_html}</p>'
            f'<p class="footer">Powered by <a href="https://dchub.cloud">DC Hub</a></p>'
        )
        return Response(
            f"<!doctype html><html><head><meta charset=utf-8><title>Operator Brief · DC Hub</title>"
            f'<link rel="stylesheet" href="/static/dchub-brand.css"></head><body>{body}</body></html>',
            mimetype="text/html",
            headers={"Cache-Control": "public, max-age=300"})
    if brief.get("redirect_to") and brief["redirect_to"] != _norm_slug(slug):
        from flask import redirect
        return redirect(f"/operators/{brief['redirect_to']}/brief", code=301)
    html = _render_html(brief)
    return Response(html, mimetype="text/html",
                    headers={
                        # 6h edge cache
                        "Cache-Control": "public, max-age=21600, s-maxage=21600",
                        "X-Operator-Brief-Tier": tier,
                    })
