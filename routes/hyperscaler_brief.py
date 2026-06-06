"""
hyperscaler_brief.py — Per-hyperscaler Brief pages (2026-06-06).

M&A bankers + PE deal teams + hedge funds tracking hyperscalers pay
$100K+/yr for similar coverage from Wells Fargo. DC Hub charges $499/mo.

For each hyperscaler (AWS, Azure, Google, Meta, Apple, Oracle, ByteDance,
Tencent, Alibaba, SoftBank), the brief gives a full pipeline view:

  1. Hero — total announced MW + capital + "Live as of"
  2. Announced Pipeline — last 12mo of announced builds (location, MW, ETA)
  3. Signed PPAs — power purchase agreements w/ renewable + nuclear utilities
  4. M&A Activity — acquisitions, land deals, JVs over 24mo
  5. Water Consumption — estimated annual water across the fleet
  6. ISO Concentration — fleet exposure by ISO (NN%)
  7. Time-to-Power Trajectory — avg months announce→operational, 36mo trend
  8. Capital Velocity — $/MW spend rate vs peers
  9. Outlook — LLM-generated narrative on forward bets

Endpoints:
  GET /hyperscalers/<slug>/brief       — HTML page (paywalled by tier)
  GET /api/v1/hyperscaler-brief/<slug> — JSON of the same data

Paywall:
  anon/FREE → Hero + Pipeline + Outlook teaser
  PRO+      → PPAs + M&A + Water + ISO + Time-to-Power + Capital Velocity

Sources (all real, ILIKE-matched):
  - discovered_facilities (provider ILIKE %alias%) — fleet + MW + status
  - deals                 (buyer / seller ILIKE %alias%) — M&A + land
  - news                  (title / summary ILIKE %alias%) — pipeline + PPAs
  - water_risk            (US-state join from fleet)
  - market_power_scores   (iso column from fleet markets)

Pattern reuse:
  - market_brief.py one-connection fan-out (one cursor, best-effort per section)
  - tier_gate._resolve_caller_tier() + tier_registry for the paywall gate
  - "amount undisclosed" when value_usd IS NULL (per spec)
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

hyperscaler_brief_bp = Blueprint("hyperscaler_brief", __name__)


# ─────────────────────────────────────────────────────────────────────
# Constants — 10 seed hyperscalers + alias map
# ─────────────────────────────────────────────────────────────────────

# 10 seed hyperscalers (sitemapped on day 1 + pre-warmed by the cron).
# Beyond these the surface still auto-renders for ANY slug in
# HYPERSCALER_ALIASES, but only the seed ten are hand-QA'd.
SEED_HYPERSCALERS = (
    "aws",
    "azure",
    "google-cloud",
    "meta",
    "apple",
    "oracle",
    "tiktok-bytedance",
    "tencent",
    "alibaba",
    "softbank",
)

# Slug → (display_name, [ILIKE_aliases]). Aliases are matched
# case-insensitively against `provider`, `buyer`, `seller`, `title`, and
# `summary` (LIKE '%<alias>%'). Order matters: the most specific alias
# should appear first so a partial match still tags the right hyperscaler.
HYPERSCALER_ALIASES: dict[str, dict] = {
    "aws": {
        "display": "Amazon Web Services",
        "short":   "AWS",
        "aliases": ["aws", "amazon web services", "amazon data center",
                    "amazon.com"],
        "color":   "#ff9900",
        "tagline": "Largest hyperscaler by MW · 27+ regions",
    },
    "azure": {
        "display": "Microsoft Azure",
        "short":   "Azure",
        "aliases": ["azure", "microsoft azure", "microsoft data center",
                    "microsoft corp"],
        "color":   "#0078d4",
        "tagline": "OpenAI compute backbone · 60+ regions",
    },
    "google-cloud": {
        "display": "Google Cloud",
        "short":   "Google",
        "aliases": ["google cloud", "google data center", "alphabet inc",
                    "google llc"],
        "color":   "#4285f4",
        "tagline": "TPU-led AI capacity · 35+ regions",
    },
    "meta": {
        "display": "Meta (Facebook)",
        "short":   "Meta",
        "aliases": ["meta platforms", "meta data center", "facebook data center",
                    "meta inc", "facebook inc"],
        "color":   "#0866ff",
        "tagline": "Llama training scale · self-built campuses",
    },
    "apple": {
        "display": "Apple",
        "short":   "Apple",
        "aliases": ["apple inc", "apple data center", "apple intelligence",
                    "apple silicon"],
        "color":   "#000000",
        "tagline": "Private AI compute · iCloud regions",
    },
    "oracle": {
        "display": "Oracle Cloud",
        "short":   "Oracle",
        "aliases": ["oracle cloud", "oracle corp", "oci ", "oracle data center"],
        "color":   "#c74634",
        "tagline": "OpenAI Stargate partner · Texas + UAE",
    },
    "tiktok-bytedance": {
        "display": "ByteDance (TikTok)",
        "short":   "ByteDance",
        "aliases": ["bytedance", "tiktok", "douyin"],
        "color":   "#ff0050",
        "tagline": "ML inference at consumer scale",
    },
    "tencent": {
        "display": "Tencent",
        "short":   "Tencent",
        "aliases": ["tencent", "tencent cloud", "wechat data center"],
        "color":   "#0052d9",
        "tagline": "China cloud + WeChat scale",
    },
    "alibaba": {
        "display": "Alibaba Cloud",
        "short":   "Alibaba",
        "aliases": ["alibaba cloud", "alibaba group", "aliyun"],
        "color":   "#ff6a00",
        "tagline": "APAC hyperscaler · Tongyi Qianwen",
    },
    "softbank": {
        "display": "SoftBank",
        "short":   "SoftBank",
        "aliases": ["softbank", "softbank group", "softbank corp"],
        "color":   "#85d10c",
        "tagline": "OpenAI Stargate lead investor · AI infra",
    },
}


# tier_registry rank for pro/founding
_PRO_RANK = 4


# ─────────────────────────────────────────────────────────────────────
# DB + helpers
# ─────────────────────────────────────────────────────────────────────

def _conn():
    """One read-only connection, short connect timeout — best-effort per
    section means a slow query degrades one tile, not the page."""
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


_SHORT_ALIASES = {
    "amazon":             "aws",
    "amazon-web-services": "aws",
    "microsoft":          "azure",
    "ms":                 "azure",
    "google":             "google-cloud",
    "alphabet":           "google-cloud",
    "gcp":                "google-cloud",
    "facebook":           "meta",
    "fb":                 "meta",
    "bytedance":          "tiktok-bytedance",
    "tiktok":             "tiktok-bytedance",
    "alibaba-cloud":      "alibaba",
    "aliyun":             "alibaba",
    "tencent-cloud":      "tencent",
}


def _resolve_slug(slug: str) -> tuple[str, dict] | tuple[None, None]:
    """Return (canonical_slug, meta) or (None, None) if unknown."""
    s = _norm_slug(slug)
    if s in HYPERSCALER_ALIASES:
        return s, HYPERSCALER_ALIASES[s]
    if s in _SHORT_ALIASES:
        c = _SHORT_ALIASES[s]
        return c, HYPERSCALER_ALIASES[c]
    # Try whole-alias resolution — e.g. "google" → "google-cloud" via the
    # display-alias list ("google cloud" normalized to "google-cloud").
    for canonical, meta in HYPERSCALER_ALIASES.items():
        for a in meta["aliases"]:
            if _norm_slug(a) == s:
                return canonical, meta
    return None, None


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


def _ilike_clauses(column: str, aliases: list[str]) -> tuple[str, list[str]]:
    """Build a parameterized OR-chain of `column ILIKE %s` clauses for each
    alias. Returns (sql_fragment, params_list).

    The aliases are wrapped with %...% so any substring match counts.
    Empty aliases are filtered out defensively."""
    clean = [a.strip() for a in aliases if a and a.strip()]
    if not clean:
        return "FALSE", []
    parts = [f"{column} ILIKE %s" for _ in clean]
    params = [f"%{a}%" for a in clean]
    return "(" + " OR ".join(parts) + ")", params


# Format a $ value, returning "amount undisclosed" when NULL (per spec).
def _fmt_money(v) -> str:
    if v is None:
        return "amount undisclosed"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "amount undisclosed"
    if n >= 1e9:
        return f"${n/1e9:.2f}B"
    if n >= 1e6:
        return f"${n/1e6:.1f}M"
    if n >= 1e3:
        return f"${n/1e3:.0f}K"
    return f"${n:.0f}"


# ─────────────────────────────────────────────────────────────────────
# Tier gate
# ─────────────────────────────────────────────────────────────────────

def _caller_tier() -> str:
    """Return the caller's tier as an UPPER string. Best-effort — defaults
    to FREE. Reuses the canonical resolver from routes.tier_gate."""
    try:
        from routes.tier_gate import _resolve_caller_tier
        tier, _ = _resolve_caller_tier()
        return (tier or "FREE").upper()
    except Exception:
        return "FREE"


def _is_pro(tier: str) -> bool:
    """True iff the tier unlocks the deep sections (PRO, FOUNDING,
    ENTERPRISE, RESEARCH_SEED, ADMIN). founding === pro per tier_registry."""
    t = (tier or "").upper()
    return t in ("PRO", "FOUNDING", "ENTERPRISE", "RESEARCH_SEED", "ADMIN")


# ─────────────────────────────────────────────────────────────────────
# Section fan-out (each is best-effort — one bad query never 404s the page)
# ─────────────────────────────────────────────────────────────────────

def _section_hero(cur, slug: str, meta: dict) -> dict:
    """Section 1: Hero. Total MW + invested capital + Live as of.

    Pulls aggregates from `discovered_facilities` (operational + pipeline)
    and `deals` (cumulative invested capital, value_usd-NULL-tolerant)."""
    aliases = meta["aliases"]
    where, params = _ilike_clauses("provider", aliases)
    out = {
        "slug":              slug,
        "name":              meta["display"],
        "short":             meta["short"],
        "tagline":           meta["tagline"],
        "color":             meta["color"],
        "operational_mw":    None,
        "pipeline_mw":       None,
        "planned_mw":        None,
        "total_announced_mw": None,
        "facility_count":    None,
        "invested_capital":  None,
        "invested_capital_display": "amount undisclosed",
        "computed_at":       datetime.datetime.utcnow().isoformat() + "Z",
    }
    # Fleet MW + facility count (best-effort).
    try:
        cur.execute(
            f"""
            SELECT COUNT(*),
                   COALESCE(SUM(CASE WHEN status ILIKE %s OR status ILIKE %s
                                     THEN power_mw ELSE 0 END), 0) AS operational,
                   COALESCE(SUM(CASE WHEN status ILIKE %s
                                     THEN power_mw ELSE 0 END), 0) AS pipeline,
                   COALESCE(SUM(CASE WHEN status ILIKE %s OR status ILIKE %s
                                     THEN power_mw ELSE 0 END), 0) AS planned,
                   COALESCE(SUM(power_mw), 0) AS total_mw
              FROM discovered_facilities
             WHERE {where}
               AND merged_at IS NULL AND is_duplicate = 0
            """,
            ['%operat%', '%active%', '%construction%', '%planned%', '%announced%'] + params,
        )
        r = cur.fetchone()
        if r:
            out["facility_count"]     = _as_int(r[0])
            out["operational_mw"]     = _as_float(r[1])
            out["pipeline_mw"]        = _as_float(r[2])
            out["planned_mw"]         = _as_float(r[3])
            out["total_announced_mw"] = _as_float(r[4])
    except Exception:
        pass
    # Invested capital — sum of value_usd on deals where this hyperscaler
    # is the buyer. NULL value_usd rows are excluded from the sum but
    # included in the count so we can render "(N deals, amount undisclosed)"
    # for cases where rows exist but $-amounts are sparse.
    try:
        buyer_where, buyer_params = _ilike_clauses("buyer", aliases)
        cur.execute(
            f"""
            SELECT COUNT(*) AS deal_count,
                   COUNT(value) FILTER (WHERE value IS NOT NULL) AS valued_count,
                   COALESCE(SUM(value), 0) AS total_value
              FROM deals
             WHERE {buyer_where}
            """,
            buyer_params,
        )
        r = cur.fetchone()
        if r:
            out["deal_count"]          = _as_int(r[0])
            out["valued_deal_count"]   = _as_int(r[1])
            out["invested_capital"]    = _as_float(r[2]) if (r[1] or 0) > 0 else None
            out["invested_capital_display"] = _fmt_money(out["invested_capital"])
    except Exception:
        pass
    return out


def _section_pipeline(cur, meta: dict) -> list[dict]:
    """Section 2: Announced Pipeline (FREE). Last 12mo of announced builds.

    Two passes — first reads `discovered_facilities` rows tagged with this
    provider and a future status; second falls back to keyword-matching
    against the `news` headlines so a fresh announcement still surfaces
    even before the build crawler has indexed it."""
    aliases = meta["aliases"]
    out: list[dict] = []
    # Pass 1: discovered_facilities pipeline (provider ILIKE %alias%).
    try:
        where, params = _ilike_clauses("provider", aliases)
        cur.execute(
            f"""
            SELECT provider,
                   COALESCE(facility_name, name, address) AS facility,
                   power_mw, status, eta_year,
                   COALESCE(city, '') AS city, COALESCE(state, country) AS region,
                   COALESCE(market, '') AS market
              FROM discovered_facilities
             WHERE {where}
               AND (status ILIKE %s OR status ILIKE %s OR status ILIKE %s)
               AND merged_at IS NULL AND is_duplicate = 0
             ORDER BY COALESCE(eta_year, 9999), power_mw DESC NULLS LAST
             LIMIT 12
            """,
            params + ['%construction%', '%planned%', '%announced%'],
        )
        for r in cur.fetchall():
            out.append({
                "operator": r[0], "facility": r[1],
                "power_mw": _as_float(r[2]),
                "status":   r[3],
                "eta":      r[4],
                "location": ", ".join([p for p in (r[5], r[6]) if p]),
                "market":   r[7],
                "source":   "discovered_facilities",
            })
    except Exception:
        # eta_year may be missing — retry without it
        try:
            where, params = _ilike_clauses("provider", aliases)
            cur.execute(
                f"""
                SELECT provider, COALESCE(name, address), power_mw, status,
                       COALESCE(city, ''), COALESCE(state, country),
                       COALESCE(market, '')
                  FROM discovered_facilities
                 WHERE {where}
                   AND (status ILIKE %s OR status ILIKE %s)
                   AND merged_at IS NULL AND is_duplicate = 0
                 ORDER BY power_mw DESC NULLS LAST
                 LIMIT 12
                """,
                params + ['%construction%', '%planned%'],
            )
            for r in cur.fetchall():
                out.append({
                    "operator": r[0], "facility": r[1],
                    "power_mw": _as_float(r[2]),
                    "status":   r[3], "eta": None,
                    "location": ", ".join([p for p in (r[4], r[5]) if p]),
                    "market":   r[6],
                    "source":   "discovered_facilities",
                })
        except Exception:
            pass
    # Pass 2 (back-fill to 12 entries): news headlines mentioning the
    # hyperscaler in the last 12 months.
    if len(out) < 12:
        try:
            news_where, news_params = _ilike_clauses("title", aliases)
            cur.execute(
                f"""
                SELECT title, source, url, published_date
                  FROM news
                 WHERE {news_where}
                   AND published_date > CURRENT_DATE - INTERVAL '12 months'
                 ORDER BY published_date DESC LIMIT %s
                """,
                news_params + [12 - len(out)],
            )
            for r in cur.fetchall():
                out.append({
                    "operator": meta["short"],
                    "facility": r[0],
                    "power_mw": None,
                    "status":   "announced",
                    "eta":      None,
                    "location": None,
                    "market":   None,
                    "url":      r[2],
                    "published": r[3].isoformat() if r[3] else None,
                    "news_source": r[1],
                    "source":   "news",
                })
        except Exception:
            pass
    return out[:12]


def _section_ppas(cur, meta: dict) -> list[dict]:
    """Section 3: Signed PPAs (PRO+). Power purchase agreements w/
    renewable + nuclear utilities — sourced from the news table by
    `<alias>` + (PPA | power purchase | nuclear | solar | wind | renewable)."""
    aliases = meta["aliases"]
    out: list[dict] = []
    try:
        title_where, params = _ilike_clauses("title", aliases)
        cur.execute(
            f"""
            SELECT title, source, url, published_date
              FROM news
             WHERE {title_where}
               AND (LOWER(title) LIKE '%%ppa%%'
                    OR LOWER(title) LIKE '%%power purchase%%'
                    OR LOWER(title) LIKE '%%nuclear%%'
                    OR LOWER(title) LIKE '%%smr%%'
                    OR LOWER(title) LIKE '%%solar%%'
                    OR LOWER(title) LIKE '%%wind farm%%'
                    OR LOWER(title) LIKE '%%renewable%%'
                    OR LOWER(title) LIKE '%%geothermal%%')
               AND published_date > CURRENT_DATE - INTERVAL '24 months'
             ORDER BY published_date DESC LIMIT 12
            """,
            params,
        )
        for r in cur.fetchall():
            out.append({
                "title":    r[0],
                "source":   r[1],
                "url":      r[2],
                "date":     r[3].isoformat() if r[3] else None,
            })
    except Exception:
        pass
    return out


def _section_ma(cur, meta: dict) -> list[dict]:
    """Section 4: M&A Activity (PRO+). Last 24mo — acquisitions, land
    deals, JVs where this hyperscaler is the buyer OR seller."""
    aliases = meta["aliases"]
    out: list[dict] = []
    try:
        buyer_where, buyer_params = _ilike_clauses("buyer", aliases)
        seller_where, seller_params = _ilike_clauses("seller", aliases)
        cur.execute(
            f"""
            SELECT date, buyer, seller, value, mw, type,
                   COALESCE(market, region) AS location, asset_name
              FROM deals
             WHERE ({buyer_where} OR {seller_where})
               AND (date IS NULL OR date >= CURRENT_DATE - INTERVAL '24 months')
             ORDER BY date DESC NULLS LAST LIMIT 15
            """,
            buyer_params + seller_params,
        )
        for r in cur.fetchall():
            v = _as_float(r[3])
            out.append({
                "date":   r[0].isoformat() if hasattr(r[0], 'isoformat') else (str(r[0]) if r[0] else None),
                "buyer":  r[1], "seller": r[2],
                "value":  v,
                "value_display": _fmt_money(v) if v is not None else "amount undisclosed",
                "mw":     _as_float(r[4]),
                "type":   r[5],
                "location": r[6],
                "asset":  r[7],
            })
    except Exception:
        pass
    return out


def _section_water(cur, meta: dict) -> dict:
    """Section 5: Water Consumption (PRO+).

    Estimates annual water-use across the fleet by joining the fleet's
    state-by-state distribution against `water_risk`. We don't have
    per-facility water draw — instead we report:
      total_us_mw         — operational US MW (for context)
      states_covered      — # of US states in the fleet
      avg_water_stress    — weighted avg WRI baseline_water_stress (0-5)
      stressed_state_pct  — % of fleet MW in states with stress >= 4
      estimated_gpy       — gallons/year @ 100 gal/MW-day industry avg
    """
    aliases = meta["aliases"]
    out = {
        "total_us_mw":        None,
        "states_covered":     None,
        "avg_water_stress":   None,
        "stressed_state_pct": None,
        "estimated_gpy":      None,
        "note":               None,
    }
    try:
        where, params = _ilike_clauses("provider", aliases)
        cur.execute(
            f"""
            SELECT UPPER(COALESCE(state, '')) AS state,
                   COALESCE(SUM(power_mw), 0) AS mw
              FROM discovered_facilities
             WHERE {where}
               AND COALESCE(country, 'US') IN ('US', 'United States', '')
               AND merged_at IS NULL AND is_duplicate = 0
             GROUP BY UPPER(COALESCE(state, ''))
             HAVING COALESCE(SUM(power_mw), 0) > 0
             ORDER BY mw DESC LIMIT 25
            """,
            params,
        )
        state_mw = [(r[0], float(r[1] or 0)) for r in cur.fetchall() if r[0]]
    except Exception:
        state_mw = []
    if not state_mw:
        out["note"] = "Insufficient state-level data to estimate water draw."
        return out
    total_mw = sum(mw for _, mw in state_mw)
    out["total_us_mw"]    = round(total_mw, 1)
    out["states_covered"] = len(state_mw)
    # Pull water_stress per state, weight by MW
    stress_sum = 0.0
    stressed_mw = 0.0
    matched_mw = 0.0
    for state, mw in state_mw:
        try:
            cur.execute(
                """
                SELECT water_stress_score, baseline_water_stress
                  FROM water_risk
                 WHERE UPPER(state) = UPPER(%s)
                 ORDER BY computed_at DESC NULLS LAST LIMIT 1
                """,
                (state,),
            )
            r = cur.fetchone()
            if r:
                stress = _as_float(r[0]) or _as_float(r[1])
                if stress is not None:
                    stress_sum += float(stress) * mw
                    matched_mw  += mw
                    if float(stress) >= 4.0:
                        stressed_mw += mw
        except Exception:
            continue
    if matched_mw > 0:
        out["avg_water_stress"]   = round(stress_sum / matched_mw, 2)
        out["stressed_state_pct"] = round(100 * stressed_mw / total_mw, 1)
    # Industry-avg evap: ~100 gal / MW-day (per Uptime + LBNL 2024).
    # Annual = mw * 100 * 365 * 24/24 = mw * 36,500 gal/yr (rough).
    out["estimated_gpy"] = int(total_mw * 100 * 365)
    out["note"] = ("Estimated via industry avg ~100 gal/MW-day (Uptime Institute, "
                   "LBNL 2024). Per-facility draw varies by cooling design.")
    return out


def _section_iso(cur, meta: dict) -> dict:
    """Section 6: ISO Concentration (PRO+). % of fleet MW by ISO.

    Joins fleet rows to `market_power_scores` (iso column) by market_slug
    OR market_name to bucket by ISO. CAISO / ERCOT / PJM / MISO / SPP /
    NYISO / ISONE are the canonical 7 — anything else falls under 'OTHER'.
    """
    aliases = meta["aliases"]
    out = {
        "by_iso":      [],
        "top_iso":     None,
        "top_iso_pct": None,
        "total_mw":    None,
    }
    try:
        where, params = _ilike_clauses("d.provider", aliases)
        cur.execute(
            f"""
            SELECT COALESCE(NULLIF(UPPER(m.iso), ''), 'OTHER') AS iso,
                   COALESCE(SUM(d.power_mw), 0) AS mw
              FROM discovered_facilities d
              LEFT JOIN market_power_scores m
                     ON LOWER(d.market) = LOWER(m.market_slug)
                     OR LOWER(d.market) = LOWER(m.market_name)
             WHERE {where}
               AND d.merged_at IS NULL AND d.is_duplicate = 0
               AND d.power_mw > 0
             GROUP BY 1
             HAVING COALESCE(SUM(d.power_mw), 0) > 0
             ORDER BY mw DESC LIMIT 10
            """,
            params,
        )
        rows = cur.fetchall()
    except Exception:
        rows = []
    if not rows:
        return out
    total_mw = sum(float(r[1] or 0) for r in rows)
    out["total_mw"] = round(total_mw, 1)
    items = []
    for r in rows:
        iso = r[0] or "OTHER"
        mw = float(r[1] or 0)
        pct = round(100 * mw / total_mw, 1) if total_mw else 0
        items.append({"iso": iso, "mw": round(mw, 1), "pct": pct})
    out["by_iso"]      = items
    if items:
        out["top_iso"]     = items[0]["iso"]
        out["top_iso_pct"] = items[0]["pct"]
    return out


def _section_time_to_power(cur, meta: dict) -> dict:
    """Section 7: Time-to-Power Trajectory (PRO+).

    Avg months from announce → operational across the fleet, plus the
    trend over the last 36mo. Best-effort — many facilities don't have
    both timestamps so we report sample size + the 3-tier verdict."""
    aliases = meta["aliases"]
    out = {
        "avg_months":       None,
        "sample_size":      0,
        "trend_36mo":       None,
        "trend_verdict":    None,
        "fastest_months":   None,
        "slowest_months":   None,
    }
    try:
        where, params = _ilike_clauses("provider", aliases)
        # discovered_facilities doesn't reliably store an `announced_at`
        # column — best-effort projection over (created_at → eta_year).
        cur.execute(
            f"""
            SELECT
                EXTRACT(YEAR FROM created_at) AS year_announced,
                eta_year,
                power_mw
              FROM discovered_facilities
             WHERE {where}
               AND merged_at IS NULL AND is_duplicate = 0
               AND eta_year IS NOT NULL
               AND created_at IS NOT NULL
             ORDER BY created_at DESC LIMIT 200
            """,
            params,
        )
        rows = cur.fetchall()
    except Exception:
        rows = []
    if not rows:
        out["trend_verdict"] = "Insufficient timestamped data — most facilities lack both announce + ETA."
        return out
    deltas = []
    for r in rows:
        try:
            yr_a = int(r[0]) if r[0] else None
            yr_e = int(r[1]) if r[1] else None
            if yr_a and yr_e and yr_e > yr_a and (yr_e - yr_a) < 10:
                deltas.append((yr_e - yr_a) * 12)
        except (TypeError, ValueError):
            continue
    if deltas:
        out["sample_size"]    = len(deltas)
        out["avg_months"]     = round(sum(deltas) / len(deltas), 1)
        out["fastest_months"] = min(deltas)
        out["slowest_months"] = max(deltas)
        # Verdict (vs the ~30-36mo industry baseline for hyperscale builds).
        avg = out["avg_months"]
        if avg < 28:
            out["trend_verdict"] = "AHEAD — faster than the 30-36mo industry norm."
        elif avg <= 36:
            out["trend_verdict"] = "INLINE — on par with the 30-36mo industry norm."
        else:
            out["trend_verdict"] = "BEHIND — slower than the 30-36mo industry norm; queue + permit risk."
    return out


def _section_capital_velocity(cur, meta: dict, hero: dict) -> dict:
    """Section 8: Capital Velocity (PRO+). $/MW spend rate vs peers.

    Uses the hero's `invested_capital` + `total_announced_mw` to compute
    $/MW. Compares to the cross-hyperscaler average over the same window."""
    out = {
        "spend_per_mw":     None,
        "spend_per_mw_display": "n/a",
        "peer_avg_per_mw":  None,
        "peer_avg_display": "n/a",
        "rank_vs_peers":    None,
        "rank_total":       None,
        "peer_table":       [],
        "verdict":          None,
    }
    invested = hero.get("invested_capital")
    total_mw = hero.get("total_announced_mw")
    if invested and total_mw and total_mw > 0:
        per_mw = invested / total_mw
        out["spend_per_mw"]         = round(per_mw, 0)
        out["spend_per_mw_display"] = _fmt_money(per_mw) + "/MW"
    # Peer table — compute the same metric for each seed hyperscaler.
    peers: list[dict] = []
    for peer_slug, peer_meta in HYPERSCALER_ALIASES.items():
        peer_aliases = peer_meta["aliases"]
        peer_invested = None
        peer_mw = None
        try:
            buyer_where, buyer_params = _ilike_clauses("buyer", peer_aliases)
            cur.execute(
                f"SELECT COALESCE(SUM(value), 0) FROM deals WHERE {buyer_where}",
                buyer_params,
            )
            r = cur.fetchone()
            peer_invested = _as_float(r[0]) if r and r[0] else None
        except Exception:
            pass
        try:
            prov_where, prov_params = _ilike_clauses("provider", peer_aliases)
            cur.execute(
                f"""
                SELECT COALESCE(SUM(power_mw), 0) FROM discovered_facilities
                 WHERE {prov_where}
                   AND merged_at IS NULL AND is_duplicate = 0
                """,
                prov_params,
            )
            r = cur.fetchone()
            peer_mw = _as_float(r[0]) if r else None
        except Exception:
            pass
        peer_per_mw = None
        if peer_invested and peer_mw and peer_mw > 0:
            peer_per_mw = peer_invested / peer_mw
        peers.append({
            "slug":         peer_slug,
            "name":         peer_meta["short"],
            "spend":        peer_invested,
            "mw":           peer_mw,
            "per_mw":       round(peer_per_mw, 0) if peer_per_mw else None,
            "per_mw_display": (_fmt_money(peer_per_mw) + "/MW") if peer_per_mw else "n/a",
        })
    # Avg across peers with a real number.
    real = [p["per_mw"] for p in peers if p["per_mw"]]
    if real:
        out["peer_avg_per_mw"]  = round(sum(real) / len(real), 0)
        out["peer_avg_display"] = _fmt_money(out["peer_avg_per_mw"]) + "/MW"
    # Rank
    ranked = sorted(peers, key=lambda p: (p["per_mw"] is None, -(p["per_mw"] or 0)))
    out["peer_table"] = ranked
    out["rank_total"] = len([p for p in ranked if p["per_mw"]])
    for i, p in enumerate(ranked, 1):
        if p["slug"] == hero.get("slug"):
            out["rank_vs_peers"] = i if p["per_mw"] else None
            break
    if out["spend_per_mw"] and out["peer_avg_per_mw"]:
        if out["spend_per_mw"] > out["peer_avg_per_mw"] * 1.25:
            out["verdict"] = "PREMIUM — paying more $/MW than the peer set."
        elif out["spend_per_mw"] < out["peer_avg_per_mw"] * 0.75:
            out["verdict"] = "VALUE — disciplined $/MW vs peers."
        else:
            out["verdict"] = "INLINE — $/MW close to the peer average."
    return out


def _section_outlook(cur, slug: str, meta: dict, hero: dict, pipeline: list[dict]) -> dict:
    """Section 9: Outlook (FREE teaser, full text PRO+).

    Built from the deterministic signal mix — no live LLM call so the
    free teaser is always fast + cheap. The full PRO+ narrative pulls in
    pipeline + ISO concentration + M&A frequency."""
    short = meta["short"]
    out = {
        "headline": None,
        "summary":  None,
        "teaser":   None,
        "bullets":  [],
    }
    total_mw = hero.get("total_announced_mw") or 0
    pipeline_mw = hero.get("pipeline_mw") or 0
    facility_count = hero.get("facility_count") or 0
    pipeline_count = len([p for p in pipeline if p.get("status") and
                          ('plan' in (p["status"] or "").lower()
                           or 'construct' in (p["status"] or "").lower()
                           or 'announced' in (p["status"] or "").lower())])
    if total_mw >= 5000:
        scale = "global-scale"
    elif total_mw >= 1000:
        scale = "established hyperscale"
    elif total_mw >= 100:
        scale = "emerging hyperscale"
    else:
        scale = "early-stage"
    out["headline"] = (
        f"{short}: {scale} fleet with {int(total_mw):,} MW tracked across "
        f"{facility_count or '—'} facilities."
    )
    bullets = []
    if pipeline_mw:
        bullets.append(
            f"Active pipeline ~{int(pipeline_mw):,} MW under construction or planned.")
    if pipeline_count:
        bullets.append(f"{pipeline_count} announced builds in the last 12mo.")
    if hero.get("invested_capital"):
        bullets.append(
            f"Tracked capex {_fmt_money(hero['invested_capital'])} across "
            f"{hero.get('valued_deal_count') or '—'} priced transactions.")
    else:
        bullets.append("Most deals undisclosed-value — see M&A tab for the full list.")
    out["bullets"] = bullets[:5]
    out["teaser"] = (
        f"Full Outlook covers {short}'s forward bets across regions, the "
        "PPA + nuclear mix, and the $/MW capital signal — PRO subscribers "
        "unlock the deep narrative.")
    out["summary"] = (
        f"{short} is operating a {scale} footprint of ~{int(total_mw):,} MW. "
        + (f"Pipeline of {int(pipeline_mw):,} MW is currently in flight. "
           if pipeline_mw else "")
        + ("Capex velocity tracks at " + _fmt_money(hero["invested_capital"]) + " "
           "of priced transactions in the M&A flow."
           if hero.get("invested_capital") else
           "Most M&A activity is undisclosed-value — public capex signal is light.")
    )
    return out


# ─────────────────────────────────────────────────────────────────────
# Build + render
# ─────────────────────────────────────────────────────────────────────

def _build_brief(slug: str, tier: str) -> dict:
    """One-connection fan-out, best-effort per section. Returns a dict
    with `ok` + the 9 sections (PRO sections are empty stubs for FREE)."""
    canonical, meta = _resolve_slug(slug)
    requested = _norm_slug(slug)
    out: dict = {
        "ok":          False,
        "slug":        canonical,
        "requested":   requested,
        "redirect_to": canonical if (canonical and canonical != requested) else None,
        "tier":        tier,
        "is_pro":      _is_pro(tier),
    }
    if not canonical:
        out["error"] = "hyperscaler_not_found"
        out["valid_slugs"] = list(HYPERSCALER_ALIASES.keys())
        return out
    is_pro = _is_pro(tier)
    c = _conn()
    if c is None:
        out["error"] = "no_database"
        return out
    try:
        with c.cursor() as cur:
            hero = _section_hero(cur, canonical, meta)
            out["hero"] = hero
            pipeline = _section_pipeline(cur, meta)
            out["pipeline"] = pipeline
            out["outlook"] = _section_outlook(cur, canonical, meta, hero, pipeline)
            if is_pro:
                out["ppas"]              = _section_ppas(cur, meta)
                out["ma"]                = _section_ma(cur, meta)
                out["water"]             = _section_water(cur, meta)
                out["iso"]               = _section_iso(cur, meta)
                out["time_to_power"]     = _section_time_to_power(cur, meta)
                out["capital_velocity"]  = _section_capital_velocity(cur, meta, hero)
            else:
                out["ppas"]              = []
                out["ma"]                = []
                out["water"]             = None
                out["iso"]               = None
                out["time_to_power"]     = None
                out["capital_velocity"]  = None
                out["paywall"] = {
                    "required_tier": "PRO",
                    "checkout_url":  "/pricing?utm_source=hyperscaler_brief",
                    "blurb":         (
                        "Signed PPAs, M&A flow, water consumption, ISO concentration, "
                        "time-to-power trajectory, and capital velocity are unlocked "
                        "for PRO subscribers — $499/mo, all hyperscalers."),
                }
            out["ok"] = True
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


def _render_html(brief: dict) -> str:
    """One-page HTML render. Inline CSS so a CF edge-cache MISS still
    renders correctly without a stylesheet round-trip."""
    hero  = brief.get("hero") or {}
    name  = hero.get("name") or "Hyperscaler"
    short = hero.get("short") or name
    color = hero.get("color") or "#6366f1"
    slug  = brief.get("slug") or ""
    is_pro = bool(brief.get("is_pro"))
    pipeline = brief.get("pipeline") or []
    outlook = brief.get("outlook") or {}

    total_mw = hero.get("total_announced_mw") or 0
    op_mw    = hero.get("operational_mw") or 0
    pl_mw    = hero.get("pipeline_mw") or 0
    fc       = hero.get("facility_count") or 0
    invested_disp = hero.get("invested_capital_display") or "amount undisclosed"

    def _esc(s):
        if s is None:
            return ""
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    # ── Pipeline rows
    pipe_rows = []
    for p in pipeline:
        mw = p.get("power_mw")
        mw_disp = f"{mw:,.0f} MW" if mw else "—"
        loc = p.get("location") or p.get("market") or ""
        eta = p.get("eta") or ""
        pipe_rows.append(
            f'<tr><td>{_esc(p.get("facility"))}</td>'
            f'<td>{_esc(loc)}</td>'
            f'<td style="text-align:right">{_esc(mw_disp)}</td>'
            f'<td>{_esc(p.get("status"))}</td>'
            f'<td>{_esc(eta)}</td></tr>'
        )
    pipeline_html = "\n".join(pipe_rows) or (
        '<tr><td colspan="5" style="color:#64748b;font-style:italic">'
        'No announced builds matched in the last 12 months — try the news feed.</td></tr>')

    # ── PRO sections
    pro_sections_html = ""
    if is_pro:
        # PPAs
        ppas = brief.get("ppas") or []
        ppa_rows = "\n".join(
            f'<li><a href="{_esc(p.get("url"))}" target="_blank" rel="noopener">{_esc(p.get("title"))}</a> '
            f'<span class="meta">· {_esc(p.get("source"))} · {_esc(p.get("date") or "")}</span></li>'
            for p in ppas
        ) or '<li class="muted">No PPA news in the last 24mo.</li>'

        # M&A
        ma = brief.get("ma") or []
        ma_rows = "\n".join(
            f'<tr><td>{_esc(d.get("date") or "")}</td>'
            f'<td>{_esc(d.get("buyer"))}</td>'
            f'<td>{_esc(d.get("seller"))}</td>'
            f'<td>{_esc(d.get("asset") or d.get("location") or "")}</td>'
            f'<td style="text-align:right">{_esc(d.get("value_display"))}</td>'
            f'<td>{_esc(d.get("type") or "")}</td></tr>'
            for d in ma
        ) or '<tr><td colspan="6" class="muted">No M&A activity in the last 24mo.</td></tr>'

        # Water
        water = brief.get("water") or {}
        water_html = ""
        if water.get("total_us_mw"):
            water_html = f"""
                <div class="grid3">
                  <div class="kpi"><div class="kpi-v">{water['total_us_mw']:,.0f}</div><div class="kpi-l">US MW</div></div>
                  <div class="kpi"><div class="kpi-v">{water.get('states_covered') or '—'}</div><div class="kpi-l">US States</div></div>
                  <div class="kpi"><div class="kpi-v">{(water.get('avg_water_stress') or 0):.2f}</div><div class="kpi-l">Avg Stress (WRI 0-5)</div></div>
                </div>
                <p>Estimated annual water draw: <strong>~{(water.get('estimated_gpy') or 0)/1e6:,.1f} M gallons/year</strong>.</p>
                <p class="note">{_esc(water.get('note') or '')}</p>"""
        else:
            water_html = f'<p class="muted">{_esc((water or {}).get("note") or "No state-level water data.")}</p>'

        # ISO
        iso = brief.get("iso") or {}
        iso_rows = ""
        for item in (iso.get("by_iso") or []):
            iso_rows += f'<tr><td>{_esc(item["iso"])}</td><td style="text-align:right">{item["mw"]:,.1f} MW</td><td style="text-align:right">{item["pct"]:.1f}%</td></tr>'
        if not iso_rows:
            iso_rows = '<tr><td colspan="3" class="muted">No ISO-tagged fleet rows.</td></tr>'

        # Time-to-power
        ttp = brief.get("time_to_power") or {}
        ttp_avg = ttp.get("avg_months")
        ttp_verdict = ttp.get("trend_verdict") or ""
        ttp_html = f"""
            <div class="grid3">
              <div class="kpi"><div class="kpi-v">{ttp_avg or '—'}<small> mo</small></div><div class="kpi-l">Avg announce→online</div></div>
              <div class="kpi"><div class="kpi-v">{ttp.get('sample_size') or 0}</div><div class="kpi-l">Sample size</div></div>
              <div class="kpi"><div class="kpi-v">{ttp.get('fastest_months') or '—'}<small> mo</small></div><div class="kpi-l">Fastest</div></div>
            </div>
            <p>{_esc(ttp_verdict)}</p>"""

        # Capital velocity
        cv = brief.get("capital_velocity") or {}
        peer_rows = "\n".join(
            f'<tr><td>{_esc(p["name"])}</td>'
            f'<td style="text-align:right">{_esc(_fmt_money(p["spend"]) if p["spend"] else "—")}</td>'
            f'<td style="text-align:right">{(p["mw"] or 0):,.0f} MW</td>'
            f'<td style="text-align:right">{_esc(p["per_mw_display"])}</td></tr>'
            for p in (cv.get("peer_table") or [])
        )
        cv_html = f"""
            <div class="grid3">
              <div class="kpi"><div class="kpi-v">{_esc(cv.get('spend_per_mw_display') or 'n/a')}</div><div class="kpi-l">{_esc(short)} $/MW</div></div>
              <div class="kpi"><div class="kpi-v">{_esc(cv.get('peer_avg_display') or 'n/a')}</div><div class="kpi-l">Peer Avg</div></div>
              <div class="kpi"><div class="kpi-v">{cv.get('rank_vs_peers') or '—'}<small> / {cv.get('rank_total') or 0}</small></div><div class="kpi-l">Rank vs peers</div></div>
            </div>
            <p>{_esc(cv.get('verdict') or '')}</p>
            <table class="t">
              <thead><tr><th>Hyperscaler</th><th style="text-align:right">Tracked Spend</th><th style="text-align:right">Tracked MW</th><th style="text-align:right">$/MW</th></tr></thead>
              <tbody>{peer_rows}</tbody>
            </table>"""

        pro_sections_html = f"""
        <section><h2>3 · Signed PPAs</h2><ul class="news">{ppa_rows}</ul></section>
        <section><h2>4 · M&amp;A Activity (24mo)</h2>
          <table class="t">
            <thead><tr><th>Date</th><th>Buyer</th><th>Seller</th><th>Asset</th><th style="text-align:right">Value</th><th>Type</th></tr></thead>
            <tbody>{ma_rows}</tbody>
          </table></section>
        <section><h2>5 · Water Consumption</h2>{water_html}</section>
        <section><h2>6 · ISO Concentration</h2>
          <table class="t">
            <thead><tr><th>ISO</th><th style="text-align:right">Fleet MW</th><th style="text-align:right">Share</th></tr></thead>
            <tbody>{iso_rows}</tbody>
          </table></section>
        <section><h2>7 · Time-to-Power Trajectory</h2>{ttp_html}</section>
        <section><h2>8 · Capital Velocity</h2>{cv_html}</section>"""
    else:
        pro_sections_html = f"""
        <section class="paywall">
          <h2>Unlock the full brief</h2>
          <p>{_esc((brief.get('paywall') or {}).get('blurb') or '')}</p>
          <p><a class="btn" href="/pricing?utm_source=hyperscaler_brief&hs={_esc(slug)}">Upgrade to PRO — $499/mo →</a></p>
          <p class="note">PRO unlocks: Signed PPAs · M&amp;A Activity · Water Consumption · ISO Concentration · Time-to-Power · Capital Velocity</p>
        </section>"""

    # Outlook bullets
    bullets = "\n".join(f"<li>{_esc(b)}</li>" for b in (outlook.get("bullets") or []))
    outlook_full = outlook.get("summary") if is_pro else outlook.get("teaser")

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(name)} · Hyperscaler Brief — DC Hub</title>
<meta name="description" content="Live {_esc(name)} pipeline view — MW, capex, M&amp;A, PPAs, ISO concentration, water, capital velocity. Updated daily.">
<meta property="og:title" content="{_esc(name)} · Hyperscaler Brief">
<meta property="og:description" content="Full-pipeline coverage on {_esc(name)} — built for M&amp;A bankers + PE deal teams + hedge funds.">
<meta property="og:image" content="https://dchub.cloud/static/og/hyperscaler-{_esc(slug)}.png">
<link rel="canonical" href="https://dchub.cloud/hyperscalers/{_esc(slug)}/brief">
<link rel="stylesheet" href="/static/dchub-brand.css">
<style>
 body{{max-width:1200px;margin:0 auto;padding:32px 24px;font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;line-height:1.5;color:#0f172a}}
 h1{{font-size:2.4rem;margin:.2em 0 .1em;letter-spacing:-.02em;color:{color}}}
 h2{{font-size:1.4rem;margin:1.6em 0 .6em;border-bottom:2px solid #e2e8f0;padding-bottom:6px}}
 .tagline{{color:#475569;font-size:1.05rem;margin:0 0 24px}}
 .grid3{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:20px 0}}
 .grid4{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin:20px 0}}
 .kpi{{background:#f8fafc;border:1px solid #e2e8f0;padding:18px 20px;border-radius:10px}}
 .kpi-v{{font-size:1.8rem;font-weight:700;font-family:ui-monospace,monospace;color:{color};line-height:1.1}}
 .kpi-v small{{font-size:1.1rem;color:#64748b;font-weight:500}}
 .kpi-l{{font-size:.82rem;color:#64748b;margin-top:4px;text-transform:uppercase;letter-spacing:.04em}}
 table.t{{width:100%;border-collapse:collapse;margin:12px 0}}
 table.t th{{text-align:left;padding:8px 10px;background:#f1f5f9;font-size:.82rem;color:#475569;border-bottom:1px solid #cbd5e1}}
 table.t td{{padding:8px 10px;border-bottom:1px solid #e2e8f0;font-size:.92rem}}
 .muted{{color:#94a3b8;font-style:italic}}
 .note{{color:#64748b;font-size:.85rem}}
 .news{{list-style:none;padding:0;margin:8px 0}}
 .news li{{padding:8px 0;border-bottom:1px solid #f1f5f9}}
 .news .meta{{color:#64748b;font-size:.82rem}}
 .paywall{{background:linear-gradient(180deg,#fff 0%,#f1f5f9 100%);border:2px solid {color};border-radius:14px;padding:32px;text-align:center;margin:32px 0}}
 .btn{{display:inline-block;background:{color};color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;margin:12px 0}}
 .live{{display:inline-block;padding:3px 10px;border-radius:999px;background:#dcfce7;color:#166534;font-size:.78rem;font-weight:600;margin-left:8px}}
 .pill{{display:inline-block;padding:2px 10px;border-radius:999px;background:{color};color:#fff;font-size:.75rem;font-weight:600;letter-spacing:.04em;text-transform:uppercase;margin-right:6px}}
 footer{{color:#64748b;font-size:.85rem;margin-top:32px;padding-top:16px;border-top:1px solid #e2e8f0}}
</style></head><body>
<header>
  <span class="pill">{_esc(short)}</span>
  <span class="live">Live as of {_esc(hero.get("computed_at") or "")[:10]}</span>
  <h1>{_esc(name)} · Hyperscaler Brief</h1>
  <p class="tagline">{_esc(hero.get("tagline") or "")}</p>
</header>

<section>
  <h2>1 · Hero</h2>
  <div class="grid4">
    <div class="kpi"><div class="kpi-v">{total_mw:,.0f}<small> MW</small></div><div class="kpi-l">Total Announced</div></div>
    <div class="kpi"><div class="kpi-v">{op_mw:,.0f}<small> MW</small></div><div class="kpi-l">Operational</div></div>
    <div class="kpi"><div class="kpi-v">{pl_mw:,.0f}<small> MW</small></div><div class="kpi-l">Under Construction</div></div>
    <div class="kpi"><div class="kpi-v">{_esc(invested_disp)}</div><div class="kpi-l">Tracked Capital</div></div>
  </div>
  <p class="note">{fc:,} facility records · sources: discovered_facilities, deals, news.</p>
</section>

<section>
  <h2>2 · Announced Pipeline (last 12mo)</h2>
  <table class="t">
    <thead><tr><th>Facility / Headline</th><th>Location</th><th style="text-align:right">MW</th><th>Status</th><th>ETA</th></tr></thead>
    <tbody>{pipeline_html}</tbody>
  </table>
</section>

{pro_sections_html}

<section>
  <h2>9 · Outlook</h2>
  <p><strong>{_esc(outlook.get("headline") or "")}</strong></p>
  <ul>{bullets}</ul>
  <p>{_esc(outlook_full or "")}</p>
</section>

<footer>
  <p>Powered by <a href="https://dchub.cloud">DC Hub</a> ·
     <a href="/api/v1/hyperscaler-brief/{_esc(slug)}">JSON</a> ·
     <a href="/hyperscaler-deals">All hyperscaler deals</a> ·
     <a href="/pricing">Pricing</a></p>
  <p class="note">M&amp;A bankers + PE deal teams + hedge funds: this brief is built for you.
     Wells Fargo charges $100K+/yr for similar coverage. DC Hub PRO is $499/mo.</p>
</footer>

</body></html>"""


# ─────────────────────────────────────────────────────────────────────
# Pre-warm
# ─────────────────────────────────────────────────────────────────────

def prewarm_seed_hyperscalers() -> dict:
    """Pre-build the brief for each seed hyperscaler so the first visitor
    doesn't pay the cold fan-out cost. Best-effort — never raises. Sleeps
    1s between hyperscalers (10 × ~250ms = ~2.5s + 9 sleeps = ~12s)."""
    import time as _time
    out: dict = {"warmed": 0, "errors": 0, "slugs": []}
    n = len(SEED_HYPERSCALERS)
    for i, slug in enumerate(SEED_HYPERSCALERS):
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

@hyperscaler_brief_bp.route("/api/v1/hyperscaler-brief/<slug>", methods=["GET"])
def api_hyperscaler_brief(slug):
    """JSON of the brief — same shape as the HTML page."""
    tier = _caller_tier()
    brief = _build_brief(slug, tier)
    status = 200
    if not brief.get("ok") and brief.get("error") == "hyperscaler_not_found":
        status = 404
    resp = jsonify(brief)
    # 6h edge cache per spec.
    resp.headers["Cache-Control"] = "public, max-age=21600, s-maxage=21600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, status


@hyperscaler_brief_bp.route("/hyperscalers/<slug>/brief", methods=["GET"])
def html_hyperscaler_brief(slug):
    """HTML render — 9 sections, paywalled by tier."""
    tier = _caller_tier()
    brief = _build_brief(slug, tier)
    if not brief.get("ok") and brief.get("error") == "hyperscaler_not_found":
        valid = brief.get("valid_slugs") or list(HYPERSCALER_ALIASES.keys())
        links = " · ".join(
            f'<a href="/hyperscalers/{s}/brief">{HYPERSCALER_ALIASES[s]["short"]}</a>'
            for s in valid[:10])
        body = (
            f'<h1>Hyperscaler Brief — {slug}</h1>'
            f'<p>Unknown hyperscaler. Try one of: {links}</p>'
            f'<p><a href="/hyperscaler-deals">All hyperscaler deals →</a></p>'
        )
        return Response(
            f"<!doctype html><html><head><meta charset=utf-8>"
            f"<title>Hyperscaler Brief · DC Hub</title>"
            f'<link rel="stylesheet" href="/static/dchub-brand.css"></head>'
            f"<body style='max-width:900px;margin:0 auto;padding:32px;font-family:system-ui'>"
            f"{body}</body></html>",
            mimetype="text/html",
            headers={"Cache-Control": "public, max-age=300"},
            status=404)
    if brief.get("redirect_to") and brief["redirect_to"] != _norm_slug(slug):
        from flask import redirect
        return redirect(f"/hyperscalers/{brief['redirect_to']}/brief", code=301)
    html = _render_html(brief)
    return Response(html, mimetype="text/html",
                    headers={
                        "Cache-Control": "public, max-age=21600, s-maxage=21600",
                        "X-Hyperscaler-Brief-Tier": tier,
                    })


@hyperscaler_brief_bp.route("/hyperscalers", methods=["GET"], strict_slashes=False)
def html_hyperscaler_index():
    """Tiny index page that lists the 10 seed hyperscalers."""
    cards = []
    for slug, meta in HYPERSCALER_ALIASES.items():
        cards.append(
            f'<a class="card" href="/hyperscalers/{slug}/brief" '
            f'style="border-left:4px solid {meta["color"]}">'
            f'<h3>{meta["display"]}</h3>'
            f'<p>{meta["tagline"]}</p></a>'
        )
    return Response(
        f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Hyperscaler Briefs · DC Hub</title>
<link rel="stylesheet" href="/static/dchub-brand.css">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap">
<script src="/js/dchub-nav.js" defer></script>
<style>
 :root{{--bg:#0a0a0f;--surf:#131319;--b:rgba(255,255,255,.09);--tx:#fafafa;--mut:#a1a1aa;--dim:#71717a;--ind:#818cf8;--cy:#22d3ee}}
 *{{box-sizing:border-box}}
 body{{max-width:1080px;margin:0 auto;padding:2.5rem 1.25rem 4rem;font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,system-ui,sans-serif;background:var(--bg);color:#d4d4d8;line-height:1.55}}
 .kick{{font-family:'JetBrains Mono',monospace;font-size:.72rem;letter-spacing:.16em;text-transform:uppercase;color:var(--cy);margin:0 0 .4rem}}
 h1{{font-size:2.2rem;margin:.1em 0 .3em;color:#fff;font-weight:800;letter-spacing:-.02em}}
 .lead{{color:var(--mut);max-width:760px}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px;margin:24px 0}}
 .card{{display:block;background:var(--surf);border:1px solid var(--b);padding:18px;border-radius:11px;text-decoration:none;color:inherit;transition:transform .12s,border-color .12s}}
 .card:hover{{transform:translateY(-2px);border-color:var(--ind)}}
 .card h3{{margin:0 0 6px;font-size:1.05rem;color:#fff}}
 .card p{{margin:0;color:var(--mut);font-size:.88rem}}
</style></head><body>
<p class=kick>DC Hub · Premium</p>
<h1>Hyperscaler Briefs</h1>
<p class="lead">Full-pipeline coverage on the 10 hyperscalers DC Hub tracks.
M&amp;A bankers, PE deal teams, and hedge funds use these briefs to size
$/MW velocity, ISO concentration risk, and PPA flow. 6h edge cache.</p>
<div class="grid">{''.join(cards)}</div>
<p style="color:#71717a;font-size:.85rem;margin-top:2rem;border-top:1px solid rgba(255,255,255,.09);padding-top:1.25rem;font-family:'JetBrains Mono',monospace">
<a href="/hyperscaler-deals" style="color:#818cf8;text-decoration:none">→ Live hyperscaler deal ticker</a> ·
<a href="/pricing" style="color:#818cf8;text-decoration:none">Pricing</a></p>
</body></html>""",
        mimetype="text/html",
        headers={"Cache-Control": "public, max-age=21600, s-maxage=21600"})
