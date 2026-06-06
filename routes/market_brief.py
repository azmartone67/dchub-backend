"""
market_brief.py — Market Brief v1 (MVP).

Source-of-truth: feature_specs/MARKET_BRIEF_v1.md.

Replaces the dcHawk/dcByte PDF in a broker/REIT/fund deck with a live,
shareable, brandable DC Hub URL. The URL is always public, always 200 —
anon/free users see Hero + At-a-Glance + Outlook teaser; PRO+ unlocks
the deep sections. 6h edge cache. "Live as of" tags REAL source-table
age (per the freshness-architecture memory) not re-stamp drift.

Endpoints:
  GET /markets/<slug>/brief       — HTML page (9 sections, paywalled by tier)
  GET /api/v1/market-brief/<slug> — JSON of the same data

Pattern reuse:
  - site_brief.py one-connection fan-out (one cursor, best-effort per section)
  - market_deep_dive.read_deep_dive() for the Outlook narrative (Claude-written)
  - tier_gate._resolve_caller_tier() + tier_registry for the paywall gate
  - freshness_public._DOMAIN_SOURCE convention for live-as-of timestamps

The 5 seed markets (northern-virginia, dallas, phoenix, atlanta,
chicago) are hand-QA'd; any other slug renders as long as it has a
market_power_scores row.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from flask import (Blueprint, Response, jsonify, render_template, request,
                   url_for)

market_brief_bp = Blueprint("market_brief", __name__)


# ─────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────

# 5 seed markets — first launch wave. Beyond these the surface still
# auto-renders for any market with a market_power_scores row, but only
# the seed five are hand-QA'd and pre-warmed by the cron.
SEED_MARKETS = (
    "northern-virginia",
    "dallas",
    "phoenix",
    "atlanta",
    "chicago",
)

# Alias map: alternate slug → canonical slug. Mirrors the spec's "canonicalize
# to city slug" rule (and prevents the 6.6k/day 404 incident from
# /markets vs /dcpi slug drift — per the market-slugs memory). The canonical
# form for the Market Brief is the metro slug used by /markets/<slug>, so
# `ashburn` resolves to `northern-virginia`.
_CANONICAL_SLUG: dict[str, str] = {
    "ashburn": "northern-virginia",
    "nova": "northern-virginia",
    "dfw": "dallas",
    "dallas-fort-worth": "dallas",
    "phx": "phoenix",
    "atl": "atlanta",
    "chi": "chicago",
}


_PRO_RANK = 4  # tier_registry rank for pro/founding


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

def _section_hero(cur, slug: str) -> dict | None:
    """Section 1: Hero. market name + DCPI verdict + composite score +
    excess-power score. Pulled from market_power_scores (the same table
    /api/v1/dcpi reads off).
    """
    # market_power_scores key columns are: market_slug, market_name, state, iso,
    # verdict, score (or composite_score depending on column drift),
    # excess_power_score, constraint_score, computed_at, queue_wait_months.
    # Try the full SELECT; if `score` is the canonical column (per
    # market_deep_dive._gather_market_facts) use it.
    # r-fix (2026-06-06): the previous SELECT also asked for state, iso,
    # queue_wait_months, time_to_power_months — columns that DON'T exist on
    # the live market_power_scores table. The query threw, the bare `except`
    # below swallowed it, and `_section_hero` returned None → the gating check
    # rendered "not yet in our DCPI coverage" for EVERY market (the brief was
    # 100% broken). Now we SELECT only the columns market_deep_dive (the proven
    # sibling) uses; the optional fields default to None and downstream
    # sections (power/queue) fill what they can, best-effort.
    try:
        cur.execute("""
            SELECT market_slug, market_name, verdict, score,
                   excess_power_score, constraint_score, computed_at
              FROM market_power_scores
             WHERE LOWER(market_slug) = LOWER(%s)
                OR LOWER(REPLACE(market_name, ' ', '-')) = LOWER(%s)
             ORDER BY computed_at DESC LIMIT 1
        """, (slug, slug))
        r = cur.fetchone()
    except Exception:
        return None
    if not r:
        return None
    return {
        "slug":              r[0],
        "name":              r[1],
        "state":             None,
        "iso":               None,
        "verdict":           r[2],
        "composite_score":   _as_int(r[3]),
        "excess_power":      _as_float(r[4]),
        "constraint_score":  _as_float(r[5]),
        "queue_wait_months": None,
        "time_to_power_mo":  None,
        "computed_at":       r[6].isoformat() if r[6] else None,
        "_computed_at_dt":   r[6],  # internal — used to compute live-as-of
    }


def _section_kpis(cur, hero: dict) -> dict:
    """Section 2: At-a-Glance KPIs (FREE)."""
    name = (hero or {}).get("name") or ""
    out = {
        "operational_mw": None,
        "pipeline_mw":    None,
        "facility_count": None,
        "vacancy_pct":    None,
        "lease_rate":     None,
        "queue_months":   (hero or {}).get("queue_wait_months"),
        "top_operator":   None,
    }
    # operational + pipeline MW from discovered_facilities
    try:
        cur.execute("""
            SELECT COUNT(*),
                   COALESCE(SUM(power_mw), 0),
                   COALESCE(SUM(CASE WHEN status ILIKE %s OR status ILIKE %s
                                     THEN power_mw ELSE 0 END), 0)
              FROM discovered_facilities
             WHERE LOWER(COALESCE(market, '')) = LOWER(%s)
               AND merged_at IS NULL AND is_duplicate = 0
        """, ('%construction%', '%planned%', name))
        f = cur.fetchone() or (None, None, None)
        out["facility_count"] = _as_int(f[0])
        out["operational_mw"] = _as_float(f[1])
        out["pipeline_mw"]    = _as_float(f[2])
    except Exception:
        pass
    # Top operator by facility count in this market
    try:
        cur.execute("""
            SELECT provider, COUNT(*) AS n
              FROM discovered_facilities
             WHERE LOWER(COALESCE(market, '')) = LOWER(%s)
               AND provider IS NOT NULL AND provider <> ''
               AND merged_at IS NULL AND is_duplicate = 0
             GROUP BY provider ORDER BY n DESC LIMIT 1
        """, (name,))
        r = cur.fetchone()
        if r:
            out["top_operator"] = {"name": r[0], "facility_count": _as_int(r[1])}
    except Exception:
        pass
    return out


def _section_power_grid(cur, hero: dict) -> dict:
    """Section 3: Power & Grid (PRO+). ISO depth + queue + substations."""
    out = {
        "iso":                hero.get("iso"),
        "queue_capacity_mw":  None,
        "queue_wait_months":  hero.get("queue_wait_months"),
        "reserve_margin_pct": None,
        "gen_additions_mw":   None,
        "interconnection_pending_mw": None,
    }
    try:
        cur.execute("""
            SELECT queue_capacity_mw, reserve_margin_pct,
                   gen_additions_12mo_mw, curtailment_pct,
                   stranded_capacity_mw
              FROM market_power_scores
             WHERE market_slug = %s
             ORDER BY computed_at DESC LIMIT 1
        """, (hero.get("slug"),))
        r = cur.fetchone()
        if r:
            out["queue_capacity_mw"]  = _as_float(r[0])
            out["reserve_margin_pct"] = _as_float(r[1])
            out["gen_additions_mw"]   = _as_float(r[2])
    except Exception:
        pass
    # Live interconnection queue pending MW (if the table exists)
    try:
        cur.execute("""
            SELECT COALESCE(SUM(capacity_mw), 0)
              FROM interconnection_queue
             WHERE UPPER(COALESCE(iso, '')) = UPPER(%s)
               AND (LOWER(COALESCE(status, '')) LIKE '%%pending%%'
                    OR LOWER(COALESCE(status, '')) LIKE '%%active%%'
                    OR LOWER(COALESCE(status, '')) LIKE '%%study%%')
        """, (hero.get("iso") or "",))
        r = cur.fetchone()
        if r and r[0]:
            out["interconnection_pending_mw"] = _as_float(r[0])
    except Exception:
        pass
    return out


def _section_pipeline(cur, name: str) -> list[dict]:
    """Section 4: Pipeline (PRO+). Under-construction + planned projects."""
    try:
        cur.execute("""
            SELECT provider, COALESCE(facility_name, name, address),
                   power_mw, status, eta_year
              FROM discovered_facilities
             WHERE LOWER(COALESCE(market, '')) = LOWER(%s)
               AND (status ILIKE %s OR status ILIKE %s OR status ILIKE %s)
               AND merged_at IS NULL AND is_duplicate = 0
             ORDER BY power_mw DESC NULLS LAST
             LIMIT 12
        """, (name, '%construction%', '%planned%', '%announced%'))
        return [{
            "operator": r[0],
            "facility": r[1],
            "power_mw": _as_float(r[2]),
            "status":   r[3],
            "eta":      r[4],
        } for r in cur.fetchall()]
    except Exception:
        # eta_year / facility_name columns may not exist — fall back to a
        # minimal projection rather than failing the whole section.
        try:
            cur.execute("""
                SELECT provider, COALESCE(name, address),
                       power_mw, status
                  FROM discovered_facilities
                 WHERE LOWER(COALESCE(market, '')) = LOWER(%s)
                   AND (status ILIKE %s OR status ILIKE %s)
                   AND merged_at IS NULL AND is_duplicate = 0
                 ORDER BY power_mw DESC NULLS LAST
                 LIMIT 12
            """, (name, '%construction%', '%planned%'))
            return [{
                "operator": r[0], "facility": r[1],
                "power_mw": _as_float(r[2]), "status": r[3], "eta": None,
            } for r in cur.fetchall()]
        except Exception:
            return []


def _section_operators(cur, name: str) -> list[dict]:
    """Section 5: Operator Footprint (PRO+). Top 5 operators by total MW."""
    try:
        cur.execute("""
            SELECT provider,
                   COUNT(*) AS n,
                   COALESCE(SUM(power_mw), 0) AS mw
              FROM discovered_facilities
             WHERE LOWER(COALESCE(market, '')) = LOWER(%s)
               AND provider IS NOT NULL AND provider <> ''
               AND merged_at IS NULL AND is_duplicate = 0
             GROUP BY provider
             ORDER BY mw DESC NULLS LAST, n DESC
             LIMIT 5
        """, (name,))
        return [{
            "operator":       r[0],
            "facility_count": _as_int(r[1]),
            "total_mw":       _as_float(r[2]),
        } for r in cur.fetchall()]
    except Exception:
        return []


def _section_ma(cur, name: str) -> list[dict]:
    """Section 6: M&A Activity (PRO+). Last 24 months."""
    try:
        cur.execute("""
            SELECT date, buyer, seller, value, mw, type
              FROM deals
             WHERE (LOWER(COALESCE(market, '')) = LOWER(%s)
                    OR LOWER(COALESCE(region, '')) = LOWER(%s))
               AND (date IS NULL OR date >= (CURRENT_DATE - INTERVAL '24 months'))
             ORDER BY date DESC NULLS LAST
             LIMIT 15
        """, (name, name))
        return [{
            "date":   d[0].isoformat() if hasattr(d[0], "isoformat") else (str(d[0]) if d[0] else None),
            "buyer":  d[1], "seller": d[2],
            "value":  _as_float(d[3]),
            "mw":     _as_float(d[4]),
            "type":   d[5],
        } for d in cur.fetchall()]
    except Exception:
        return []


def _section_comps(cur, name: str) -> dict:
    """Section 7: Comps (PRO+). Powered-shell + land transactions, filtered
    by deal type. Spec calls out `powered_shell` + `land_transactions` tables
    but those aren't standalone in this codebase — the canonical source is
    `deals` filtered by deal_type, per the spec line 'use what exists'."""
    out = {"powered_shell": [], "land": []}
    try:
        cur.execute("""
            SELECT date, buyer, seller, value, mw, type, asset_name
              FROM deals
             WHERE (LOWER(COALESCE(market, '')) = LOWER(%s)
                    OR LOWER(COALESCE(region, '')) = LOWER(%s))
               AND (LOWER(COALESCE(type, '')) LIKE '%%powered%%'
                    OR LOWER(COALESCE(type, '')) LIKE '%%shell%%')
             ORDER BY date DESC NULLS LAST LIMIT 8
        """, (name, name))
        out["powered_shell"] = [{
            "date":  d[0].isoformat() if hasattr(d[0], "isoformat") else (str(d[0]) if d[0] else None),
            "buyer": d[1], "seller": d[2],
            "value": _as_float(d[3]), "mw": _as_float(d[4]),
            "type":  d[5], "asset": d[6],
        } for d in cur.fetchall()]
    except Exception:
        pass
    try:
        cur.execute("""
            SELECT date, buyer, seller, value, mw, type, asset_name
              FROM deals
             WHERE (LOWER(COALESCE(market, '')) = LOWER(%s)
                    OR LOWER(COALESCE(region, '')) = LOWER(%s))
               AND (LOWER(COALESCE(type, '')) LIKE '%%land%%'
                    OR LOWER(COALESCE(type, '')) LIKE '%%site%%')
             ORDER BY date DESC NULLS LAST LIMIT 8
        """, (name, name))
        out["land"] = [{
            "date":  d[0].isoformat() if hasattr(d[0], "isoformat") else (str(d[0]) if d[0] else None),
            "buyer": d[1], "seller": d[2],
            "value": _as_float(d[3]), "mw": _as_float(d[4]),
            "type":  d[5], "asset": d[6],
        } for d in cur.fetchall()]
    except Exception:
        pass
    return out


def _section_risk(cur, hero: dict) -> dict:
    """Section 8: Risk Factors (PRO+). Water stress, drought, seismic."""
    out = {"water_stress": None, "drought_months_d2_plus": None,
           "wildfire_seismic_note": None}
    state = hero.get("state")
    try:
        cur.execute("""
            SELECT water_stress_score, drought_d2_months
              FROM water_risk
             WHERE UPPER(state) = UPPER(%s)
             ORDER BY computed_at DESC NULLS LAST LIMIT 1
        """, (state or "",))
        r = cur.fetchone()
        if r:
            out["water_stress"]            = _as_float(r[0])
            out["drought_months_d2_plus"]  = _as_int(r[1])
    except Exception:
        # water_risk schema varies; try the per-market column shape
        try:
            cur.execute("""
                SELECT stress_score, baseline_water_stress
                  FROM water_risk
                 WHERE LOWER(market) = LOWER(%s)
                 LIMIT 1
            """, ((hero.get("name") or ""),))
            r = cur.fetchone()
            if r:
                out["water_stress"] = _as_float(r[0]) or _as_float(r[1])
        except Exception:
            pass
    # California/Pacific NW wildfire note (best-effort, hardcoded heuristic)
    if (state or "").upper() in ("CA", "OR", "WA", "NV", "ID"):
        out["wildfire_seismic_note"] = (
            "WUI wildfire exposure + Pacific seismic zones; check CalOES + "
            "USGS hazard layers before final site selection.")
    elif (state or "").upper() in ("AK", "HI"):
        out["wildfire_seismic_note"] = "Seismic activity material to siting."
    return out


def _section_outlook(slug: str, hero: dict) -> dict:
    """Section 9: Outlook (FREE teaser, PRO+ full). Re-uses the existing
    market_deep_dive Claude-written narrative. Falls back to a template if
    no narrative is on file (the spec says "fall back to template if no
    API key" — same idea).
    """
    out = {"narrative_md": None, "word_count": 0, "generated_at": None,
           "verdict": hero.get("verdict"), "rationale": None}
    try:
        from routes.market_deep_dive import read_deep_dive
        r = read_deep_dive(slug)
        if r:
            out["narrative_md"] = r.get("narrative_md")
            out["word_count"]   = _as_int(r.get("word_count")) or 0
            ga = r.get("generated_at")
            out["generated_at"] = ga.isoformat() if hasattr(ga, "isoformat") else ga
    except Exception:
        pass
    if not out["narrative_md"]:
        verdict = (hero.get("verdict") or "CAUTION").upper()
        score = hero.get("composite_score")
        score_str = f"{score}/100" if score is not None else "(score pending)"
        out["narrative_md"] = (
            f"**12-month outlook: {verdict}** (DCPI {score_str}). "
            f"{hero.get('name','This market')} carries an excess-power score "
            f"of {hero.get('excess_power') or '—'} and a queue-wait of "
            f"{hero.get('queue_wait_months') or '—'} months. "
            "A Claude-written deep-dive narrative is generated nightly — "
            "if this is the first render after seed, the placeholder will "
            "be replaced on the next cron pass."
        )
        out["rationale"] = "Auto-template (deep-dive not yet generated)."
    return out


# ─────────────────────────────────────────────────────────────────────
# Live-as-of timestamp — REAL source-table age, not re-stamp drift.
# Mirrors freshness_public._DOMAIN_SOURCE convention.
# ─────────────────────────────────────────────────────────────────────

def _live_as_of(cur, hero: dict) -> dict:
    """Return {iso, age_hours} — the YOUNGEST of (market_power_scores
    computed_at, discovered_facilities first_seen for this market, deals
    date for this market). Per the freshness-architecture memory: judge
    the breach on real source-table age, not re-stamp drift."""
    youngest = hero.get("_computed_at_dt")
    name = hero.get("name") or ""
    # Latest facility for this market
    try:
        cur.execute("""
            SELECT MAX(COALESCE(last_seen, first_seen))
              FROM discovered_facilities
             WHERE LOWER(COALESCE(market, '')) = LOWER(%s)
        """, (name,))
        r = cur.fetchone()
        if r and r[0]:
            if youngest is None or r[0] > youngest:
                youngest = r[0]
    except Exception:
        pass
    # Latest deal for this market
    try:
        cur.execute("""
            SELECT MAX(date)
              FROM deals
             WHERE LOWER(COALESCE(market, '')) = LOWER(%s)
                OR LOWER(COALESCE(region, '')) = LOWER(%s)
        """, (name, name))
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
# Build the full brief (used by both the JSON endpoint and the HTML page)
# ─────────────────────────────────────────────────────────────────────

def _build_brief(slug: str, tier: str) -> dict:
    """Run the full per-section fan-out in ONE connection. Each section is
    best-effort — if one query fails, the rest still render. Mirrors the
    site_brief.py pattern."""
    canonical = _canonical(slug)
    requested_slug = _norm_slug(slug)
    redirect_to = canonical if canonical != requested_slug else None

    is_pro = _is_pro(tier)
    out: dict = {
        "ok":          False,
        "slug":        canonical,
        "requested":   requested_slug,
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
            hero = _section_hero(cur, canonical)
            if not hero:
                out["error"] = "market_not_found"
                # Surface a few valid slugs so a stray /brief URL helps the caller.
                try:
                    cur.execute(
                        "SELECT DISTINCT market_slug FROM market_power_scores "
                        "ORDER BY market_slug LIMIT 12")
                    out["sample_markets"] = [r[0] for r in cur.fetchall()]
                except Exception:
                    out["sample_markets"] = []
                return out
            # Live-as-of BEFORE we drop the internal _computed_at_dt
            live = _live_as_of(cur, hero)
            hero_public = {k: v for k, v in hero.items() if not k.startswith("_")}
            out["hero"] = hero_public
            out["live_as_of"] = live
            out["kpis"]    = _section_kpis(cur, hero)
            out["outlook"] = _section_outlook(canonical, hero)
            # PRO+ sections — anon/free callers get an empty stub so the JSON
            # shape is stable; the HTML template handles blur/teaser display.
            if is_pro:
                out["power_grid"] = _section_power_grid(cur, hero)
                out["pipeline"]   = _section_pipeline(cur, hero.get("name") or "")
                out["operators"]  = _section_operators(cur, hero.get("name") or "")
                out["ma"]         = _section_ma(cur, hero.get("name") or "")
                out["comps"]      = _section_comps(cur, hero.get("name") or "")
                out["risk"]       = _section_risk(cur, hero)
            else:
                # Empty arrays / nulls so callers don't NPE on missing keys.
                out["power_grid"] = None
                out["pipeline"]   = []
                out["operators"]  = []
                out["ma"]         = []
                out["comps"]      = {"powered_shell": [], "land": []}
                out["risk"]       = None
                out["paywall"]    = {
                    "required_tier": "PRO",
                    "checkout_url":  "/pricing?utm_source=market_brief",
                    "blurb":         ("Power & Grid, Pipeline, Operator footprint, "
                                      "M&A, Comps, and Risk are unlocked for PRO "
                                      "subscribers — $499/mo, all markets."),
                }
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

_VERDICT_PALETTE = {
    "BUILD":   {"pill_bg": "#10b981", "pill_fg": "#06251b"},  # green
    "GO":      {"pill_bg": "#10b981", "pill_fg": "#06251b"},
    "STRONG":  {"pill_bg": "#10b981", "pill_fg": "#06251b"},
    "CAUTION": {"pill_bg": "#f59e0b", "pill_fg": "#28190a"},  # amber
    "HOLD":    {"pill_bg": "#f59e0b", "pill_fg": "#28190a"},
    "AVOID":   {"pill_bg": "#ef4444", "pill_fg": "#280808"},  # red
    "STOP":    {"pill_bg": "#ef4444", "pill_fg": "#280808"},
}


def _verdict_colors(verdict: str | None) -> dict:
    v = (verdict or "").upper().strip()
    # Try the exact key first, then partial-match the first word.
    if v in _VERDICT_PALETTE:
        return _VERDICT_PALETTE[v]
    for k, c in _VERDICT_PALETTE.items():
        if v.startswith(k):
            return c
    # Neutral fallback (grey)
    return {"pill_bg": "#71717a", "pill_fg": "#fafafa"}


def _render_html(brief: dict) -> str:
    """Render the 9-section HTML. Inline-styled (single file) so the page
    survives the 1-replica backend flap without an extra round-trip to a
    template loader — the spec offered either path. Uses brand variables
    + brand.css from /static."""
    slug = brief.get("slug") or ""
    hero = brief.get("hero") or {}
    live = brief.get("live_as_of") or {}
    kpis = brief.get("kpis") or {}
    outlook = brief.get("outlook") or {}
    is_pro = bool(brief.get("is_pro"))

    name = hero.get("name") or slug.replace("-", " ").title()
    verdict = hero.get("verdict") or "—"
    score = hero.get("composite_score")
    score_str = f"{score}/100" if score is not None else "—"
    colors = _verdict_colors(verdict)

    live_iso = live.get("iso") or hero.get("computed_at") or ""
    live_age = live.get("age_hours")
    live_age_str = f"{live_age:.1f}h" if isinstance(live_age, (int, float)) else "—"

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

    def _fmt_months(v):
        if v is None:
            return "—"
        try:
            return f"{float(v):.1f} mo"
        except (TypeError, ValueError):
            return "—"

    def _fmt_pct(v):
        if v is None:
            return "—"
        try:
            return f"{float(v):.1f}%"
        except (TypeError, ValueError):
            return "—"

    # ── KPI tiles ────────────────────────────────────────────────────
    kpi_tiles = []
    kpi_tiles.append(("Operational", _fmt_mw(kpis.get("operational_mw"))))
    kpi_tiles.append(("Pipeline",    _fmt_mw(kpis.get("pipeline_mw"))))
    kpi_tiles.append(("Facilities",  _fmt_int(kpis.get("facility_count"))))
    kpi_tiles.append(("Queue Wait",  _fmt_months(kpis.get("queue_months"))))
    if kpis.get("top_operator"):
        kpi_tiles.append(("Top Operator", kpis["top_operator"].get("name") or "—"))
    if kpis.get("lease_rate") is not None:
        kpi_tiles.append(("Lease Rate", f"${kpis['lease_rate']:.2f}/kW-mo"))
    if kpis.get("vacancy_pct") is not None:
        kpi_tiles.append(("Vacancy", _fmt_pct(kpis.get("vacancy_pct"))))
    kpi_html = "\n".join(
        f'<div class="kpi"><span class="kpi-l">{lab}</span>'
        f'<span class="kpi-v">{val}</span></div>'
        for lab, val in kpi_tiles)

    # ── PRO sections — either render real data or render a teaser blur ──
    pg   = brief.get("power_grid") or {}
    pipe = brief.get("pipeline") or []
    ops  = brief.get("operators") or []
    ma   = brief.get("ma") or []
    comps = brief.get("comps") or {}
    risk = brief.get("risk") or {}

    if is_pro:
        pg_html = (
            f'<div class="grid3">'
            f'<div class="cell"><b>ISO</b><span>{pg.get("iso") or "—"}</span></div>'
            f'<div class="cell"><b>Queue Capacity</b><span>{_fmt_mw(pg.get("queue_capacity_mw"))}</span></div>'
            f'<div class="cell"><b>Queue Wait</b><span>{_fmt_months(pg.get("queue_wait_months"))}</span></div>'
            f'<div class="cell"><b>Reserve Margin</b><span>{_fmt_pct(pg.get("reserve_margin_pct"))}</span></div>'
            f'<div class="cell"><b>Gen Additions 12mo</b><span>{_fmt_mw(pg.get("gen_additions_mw"))}</span></div>'
            f'<div class="cell"><b>Interconnect Pending</b><span>{_fmt_mw(pg.get("interconnection_pending_mw"))}</span></div>'
            f'</div>'
        )

        def _row(cells):
            return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"

        pipe_rows = "\n".join(
            _row([p.get("operator") or "—",
                  p.get("facility") or "—",
                  _fmt_mw(p.get("power_mw")),
                  p.get("status") or "—",
                  p.get("eta") or "—"])
            for p in pipe) or _row(["—", "No pipeline tracked", "—", "—", "—"])
        pipe_html = (
            '<table><thead><tr><th>Operator</th><th>Facility</th>'
            '<th>Power</th><th>Status</th><th>ETA</th></tr></thead>'
            f'<tbody>{pipe_rows}</tbody></table>')

        ops_rows = "\n".join(
            _row([o.get("operator") or "—",
                  _fmt_int(o.get("facility_count")),
                  _fmt_mw(o.get("total_mw"))])
            for o in ops) or _row(["No operator data yet", "—", "—"])
        ops_html = (
            '<table><thead><tr><th>Operator</th>'
            '<th>Facilities</th><th>Total MW</th></tr></thead>'
            f'<tbody>{ops_rows}</tbody></table>')

        def _money(v):
            if v is None:
                return "—"
            try:
                return f"${float(v):,.0f}"
            except (TypeError, ValueError):
                return "—"

        ma_rows = "\n".join(
            _row([m.get("date") or "—",
                  m.get("buyer") or "—",
                  m.get("seller") or "—",
                  _money(m.get("value")),
                  _fmt_mw(m.get("mw"))])
            for m in ma) or _row(["—", "No M&A in last 24mo", "—", "—", "—"])
        ma_html = (
            '<table><thead><tr><th>Date</th><th>Buyer</th>'
            '<th>Seller</th><th>Value</th><th>MW</th></tr></thead>'
            f'<tbody>{ma_rows}</tbody></table>')

        comps_ps = comps.get("powered_shell") or []
        comps_ld = comps.get("land") or []
        ps_rows = "\n".join(
            _row([c.get("date") or "—",
                  c.get("asset") or c.get("buyer") or "—",
                  _money(c.get("value")), _fmt_mw(c.get("mw"))])
            for c in comps_ps) or _row(["—", "No powered-shell comps", "—", "—"])
        ld_rows = "\n".join(
            _row([c.get("date") or "—",
                  c.get("asset") or c.get("buyer") or "—",
                  _money(c.get("value")), _fmt_mw(c.get("mw"))])
            for c in comps_ld) or _row(["—", "No land comps", "—", "—"])
        comps_html = (
            '<h3 class="sub">Powered Shell</h3>'
            '<table><thead><tr><th>Date</th><th>Asset</th>'
            '<th>Value</th><th>MW</th></tr></thead>'
            f'<tbody>{ps_rows}</tbody></table>'
            '<h3 class="sub">Land</h3>'
            '<table><thead><tr><th>Date</th><th>Asset</th>'
            '<th>Value</th><th>MW</th></tr></thead>'
            f'<tbody>{ld_rows}</tbody></table>')

        risk_items = []
        if risk.get("water_stress") is not None:
            risk_items.append(("Water Stress", f"{risk['water_stress']:.2f}"))
        if risk.get("drought_months_d2_plus") is not None:
            risk_items.append(("Drought (D2+)", f"{risk['drought_months_d2_plus']} mo"))
        if risk.get("wildfire_seismic_note"):
            risk_items.append(("Wildfire / Seismic", risk["wildfire_seismic_note"]))
        if not risk_items:
            risk_items.append(("Risk", "Data thin — fewer than 3 risk signals available"))
        risk_html = "<ul class='risk-list'>" + "".join(
            f"<li><b>{lab}:</b> {val}</li>" for lab, val in risk_items) + "</ul>"
    else:
        # Free teaser: blurred cards + "Unlock with PRO" CTA. URL still 200s.
        blur = (
            '<div class="blur-card">'
            '<div class="blur-overlay">'
            '<div class="blur-title">PRO unlocks all sections</div>'
            '<div class="blur-body">Power &amp; Grid · Pipeline · Operator Footprint '
            '· M&amp;A · Comps · Risk — all markets, live updates, share-ready.</div>'
            '<a class="cta" href="/pricing?utm_source=market_brief">Unlock with PRO — $499/mo</a>'
            '</div>'
            '<div class="blur-fake">'
            '<div class="fake-row"></div><div class="fake-row"></div>'
            '<div class="fake-row"></div><div class="fake-row"></div>'
            '</div></div>'
        )
        pg_html = blur
        pipe_html = blur
        ops_html = blur
        ma_html = blur
        comps_html = blur
        risk_html = blur

    # ── Outlook narrative ──────────────────────────────────────────────
    narrative = outlook.get("narrative_md") or ""
    # Free teaser = first 80 words
    if not is_pro:
        words = narrative.split()
        narrative = " ".join(words[:80])
        if len(words) > 80:
            narrative += "… <em>(continued for PRO subscribers)</em>"
    # Simple paragraph wrap
    out_paragraphs = "".join(
        f"<p>{p.strip()}</p>" for p in narrative.split("\n\n") if p.strip()
    ) or "<p>Outlook narrative will be generated on the next cron pass.</p>"

    # ── Share URLs ────────────────────────────────────────────────────
    page_url = f"https://dchub.cloud/markets/{slug}/brief"
    share_x = (f"https://twitter.com/intent/tweet?text="
               f"{name.replace(' ', '+')}+data+center+market+brief+%E2%80%94+DCPI+"
               f"{score_str.replace('/', '%2F')}+verdict+{verdict}"
               f"&url={page_url}")
    share_li = f"https://www.linkedin.com/sharing/share-offsite/?url={page_url}"

    # ── Citation block ────────────────────────────────────────────────
    citation_url = page_url
    citation_iso = (live_iso or "")[:19].replace("T", " ")
    citation = (f"DC Hub · <a href=\"{citation_url}\">{citation_url}</a> · "
                f"Live as of {citation_iso} UTC")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} Market Brief · DC Hub</title>
<meta name="description" content="{name} data center market brief — DCPI verdict {verdict} ({score_str}). Operational + pipeline MW, queue wait, operator footprint, M&amp;A, comps, risk, 12-month outlook.">
<meta name="robots" content="index,follow">
<link rel="canonical" href="{page_url}">
<meta property="og:title" content="{name} Market Brief · DC Hub">
<meta property="og:description" content="DCPI verdict {verdict} ({score_str}). Live as of {citation_iso} UTC.">
<meta property="og:url" content="{page_url}">
<meta property="og:type" content="article">
<link rel="stylesheet" href="/static/dchub-brand.css">
<style>
:root{{--bg:#0a0a0f;--surf:#131319;--b:rgba(255,255,255,0.08);--tx:#fafafa;--mut:#a1a1aa;--dim:#71717a;--ind:#818cf8;--grad:linear-gradient(135deg,#6366f1,#a855f7)}}
*{{box-sizing:border-box}}
body{{font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,sans-serif;max-width:900px;margin:0 auto;padding:2.5rem 1.25rem;background:var(--bg);color:#d4d4d8;line-height:1.65;-webkit-font-smoothing:antialiased}}
h1{{font-weight:700;letter-spacing:-.02em;margin:0 0 .25rem;font-size:2.4rem;color:var(--tx)}}
h2{{font-size:1.25rem;font-weight:600;color:var(--tx);margin:2.25rem 0 .75rem;letter-spacing:-.01em}}
h3.sub{{font-size:1rem;font-weight:600;color:var(--mut);margin:1.5rem 0 .5rem;letter-spacing:.02em;text-transform:uppercase}}
.live-pill{{display:inline-flex;align-items:center;gap:.4rem;background:var(--surf);border:1px solid var(--b);border-radius:999px;padding:.35rem .8rem;font-size:.72rem;color:var(--mut);font-family:'JetBrains Mono',monospace;margin-left:.5rem}}
.live-dot{{width:.5rem;height:.5rem;background:#10b981;border-radius:50%;animation:pulse 2.5s ease-in-out infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.35}}}}
.verdict-pill{{display:inline-block;background:{colors['pill_bg']};color:{colors['pill_fg']};font-weight:700;font-size:.85rem;padding:.45rem 1rem;border-radius:8px;letter-spacing:.04em;text-transform:uppercase}}
.score{{font-family:'JetBrains Mono',monospace;color:var(--tx);font-weight:600;font-size:1rem;margin-left:.75rem}}
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
.risk-list{{padding-left:0;list-style:none;margin:.5rem 0 1.5rem}}
.risk-list li{{background:var(--surf);border:1px solid var(--b);border-radius:10px;padding:.7rem .95rem;margin-bottom:.5rem;font-size:.88rem}}
.risk-list b{{color:var(--mut);font-weight:600;margin-right:.4rem}}
.outlook p{{font-size:1.02rem;margin:1rem 0;color:#d4d4d8}}
.share{{display:flex;gap:.5rem;flex-wrap:wrap;margin:2rem 0}}
.share a{{background:var(--surf);border:1px solid var(--b);border-radius:8px;padding:.5rem 1rem;color:var(--ind);text-decoration:none;font-size:.85rem;font-family:'JetBrains Mono',monospace}}
.share a:hover{{border-color:var(--ind)}}
.copy-btn{{cursor:pointer;background:none;border:none;font-family:inherit;font-size:inherit;padding:0;color:inherit}}
.blur-card{{position:relative;background:var(--surf);border:1px solid var(--b);border-radius:14px;padding:2rem;margin:.5rem 0 1.5rem;overflow:hidden;min-height:180px}}
.blur-overlay{{position:absolute;inset:0;background:linear-gradient(180deg,rgba(10,10,15,.4) 0%,rgba(10,10,15,.92) 100%);display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:1.5rem;z-index:2;backdrop-filter:blur(6px)}}
.blur-title{{font-size:1.05rem;font-weight:600;color:var(--tx);margin-bottom:.5rem}}
.blur-body{{font-size:.85rem;color:var(--mut);max-width:380px;margin-bottom:1.25rem}}
.cta{{background:var(--grad);color:#fff;text-decoration:none;padding:.65rem 1.4rem;border-radius:8px;font-weight:600;font-size:.9rem;letter-spacing:.01em}}
.blur-fake{{filter:blur(8px);opacity:.45}}
.fake-row{{height:1.5rem;background:linear-gradient(90deg,#27272a 0%,#3f3f46 50%,#27272a 100%);margin:.5rem 0;border-radius:6px}}
.citation{{background:var(--surf);border:1px solid var(--b);border-radius:10px;padding:.85rem 1rem;font-size:.78rem;color:var(--mut);font-family:'JetBrains Mono',monospace;margin:2rem 0 1rem}}
.citation a{{color:var(--ind);text-decoration:none}}
.footer{{color:var(--dim);font-size:.78rem;margin-top:1.5rem;padding-top:1.25rem;border-top:1px solid var(--b);font-family:'JetBrains Mono',monospace}}
.footer a{{color:var(--ind);text-decoration:none}}
@media print{{
  body{{max-width:none;padding:0 1rem;background:#fff;color:#0a0a0f}}
  h1,h2,h3,.kpi-v,.cell span,td{{color:#0a0a0f}}
  .share,.blur-overlay,nav,#dchub-nav,script{{display:none!important}}
  .blur-card,.blur-fake{{filter:none;opacity:1}}
  .kpi,.cell,table,.risk-list li,.citation{{background:#fff;border:1px solid #d4d4d8;color:#0a0a0f}}
  th{{background:#f4f4f5;color:#0a0a0f}}
  .verdict-pill{{box-shadow:0 0 0 1px #d4d4d8}}
  .live-pill{{background:#fff;border:1px solid #d4d4d8;color:#0a0a0f}}
  .footer{{color:#71717a;border-color:#d4d4d8}}
  p,td,.kpi-v,.cell span{{color:#0a0a0f}}
}}
</style>
</head>
<body>

<h1>{name}<span class="live-pill"><span class="live-dot"></span>Live · {live_age_str} fresh</span></h1>
<p class="sub">Market Brief · <span class="verdict-pill">{verdict}</span><span class="score">DCPI {score_str}</span> · Live as of {citation_iso} UTC</p>

<h2>At a Glance</h2>
<div class="kpis">
{kpi_html}
</div>

<h2>Power &amp; Grid</h2>
{pg_html}

<h2>Pipeline</h2>
{pipe_html}

<h2>Operator Footprint</h2>
{ops_html}

<h2>M&amp;A Activity (24mo)</h2>
{ma_html}

<h2>Comps</h2>
{comps_html}

<h2>Risk Factors</h2>
{risk_html}

<h2>12-Month Outlook</h2>
<div class="outlook">
{out_paragraphs}
</div>

<div class="share">
  <a href="{share_x}" target="_blank" rel="noopener">Share on X</a>
  <a href="{share_li}" target="_blank" rel="noopener">Share on LinkedIn</a>
  <a href="#" class="copy-btn" onclick="navigator.clipboard.writeText('{page_url}');this.textContent='Copied!';return false;">Copy URL</a>
  <a href="javascript:window.print()">Print / PDF</a>
</div>

<div class="citation">{citation}</div>

<p class="footer">Powered by <a href="https://dchub.cloud">DC Hub</a> · Source-of-truth data center market intelligence · 2,000+ tracked deals · 21,433 facilities · 232 markets · JSON: <a href="/api/v1/market-brief/{slug}">/api/v1/market-brief/{slug}</a></p>

<script src="/js/dchub-nav.js" defer></script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────
# Pre-warm helper (called by crawler_scheduler._run_market_brief_warm)
# ─────────────────────────────────────────────────────────────────────

def prewarm_seed_markets() -> dict:
    """Pre-build the brief for each seed slug so the first visitor doesn't
    pay the cold fan-out cost. Best-effort — never raises. Returns a
    short status dict for logging."""
    out: dict = {"warmed": 0, "errors": 0, "slugs": []}
    for slug in SEED_MARKETS:
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
    return out


# ─────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────

@market_brief_bp.route("/api/v1/market-brief/<slug>", methods=["GET"])
def api_market_brief(slug):
    """JSON of the brief — same shape as the HTML page."""
    tier = _caller_tier()
    brief = _build_brief(slug, tier)
    status = 200 if brief.get("ok") else (404 if brief.get("error") == "market_not_found" else 200)
    resp = jsonify(brief)
    # 6h edge cache per spec — section 6.
    resp.headers["Cache-Control"] = "public, max-age=21600, s-maxage=21600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, status


@market_brief_bp.route("/markets/<slug>/brief", methods=["GET"])
def html_market_brief(slug):
    """HTML render — 9 sections, paywalled by tier."""
    tier = _caller_tier()
    brief = _build_brief(slug, tier)
    if not brief.get("ok") and brief.get("error") == "market_not_found":
        # Don't 404 — the spec says URLs are always 200 so emailed links
        # never break. Render a minimal "not yet covered" shell.
        sample = brief.get("sample_markets") or []
        sample_html = ", ".join(
            f'<a href="/markets/{s}/brief">{s}</a>' for s in sample[:8])
        body = (
            f'<h1>Market Brief — {slug}</h1>'
            f'<p class="sub">This market is not yet in our DCPI coverage. Try one of: {sample_html}</p>'
            f'<p class="footer">Powered by <a href="https://dchub.cloud">DC Hub</a></p>'
        )
        return Response(
            f"<!doctype html><html><head><meta charset=utf-8><title>Market Brief · DC Hub</title>"
            f'<link rel="stylesheet" href="/static/dchub-brand.css"></head><body>{body}</body></html>',
            mimetype="text/html",
            headers={"Cache-Control": "public, max-age=300"})
    if brief.get("redirect_to") and brief["redirect_to"] != slug:
        # 301 alias to canonical slug — preserves link equity, prevents
        # duplicate-content. (Per spec section 7 + market-slugs memory.)
        from flask import redirect
        return redirect(f"/markets/{brief['redirect_to']}/brief", code=301)
    html = _render_html(brief)
    return Response(html, mimetype="text/html",
                    headers={
                        # 6h edge cache — see spec section 6
                        "Cache-Control": "public, max-age=21600, s-maxage=21600",
                        "X-Market-Brief-Tier": tier,
                    })
