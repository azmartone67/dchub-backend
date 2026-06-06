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

import csv
import datetime
import hashlib
import hmac
import io
import json
import logging
import os
import re
import time
from urllib.parse import urlparse
from flask import (Blueprint, Response, jsonify, render_template, request,
                   stream_with_context, url_for)

logger = logging.getLogger(__name__)

market_brief_bp = Blueprint("market_brief", __name__)


# ─────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────

# 15 seed markets — top US data-center markets by operational MW +
# transaction volume. Wave 1 (5) shipped 2026-06-06; wave 2 (10) added
# same day. Beyond these the surface still auto-renders for any market
# with a market_power_scores row, but only the seed fifteen are
# hand-QA'd and pre-warmed by the cron.
#
# Per-market verification (2026-06-06 vs live /api/v1/dcpi/scores/<slug>):
#   silicon-valley       → DCPI canonical = santa-clara (CA)
#   new-york             → market_slug=new-york (NY)
#   portland             → market_slug=portland (state=ME on DCPI — sole
#                          row; no portland-or in market_power_scores yet)
#   hillsboro            → market_slug=hillsboro (OR)
#   reno                 → market_slug=reno (NV)
#   columbus             → market_slug=columbus (OH)
#   salt-lake-city       → market_slug=salt-lake-city (UT)
#   charlotte            → market_slug=charlotte (NC)
#   denver               → market_slug=denver (CO)
#   madison              → market_slug=madison (WI)
SEED_MARKETS = (
    # Wave 1 (2026-06-06)
    "northern-virginia",
    "dallas",
    "phoenix",
    "atlanta",
    "chicago",
    # Wave 2 (2026-06-06) — top US markets by ops MW + transaction volume
    "silicon-valley",
    "new-york",
    "portland",
    "hillsboro",
    "reno",
    "columbus",
    "salt-lake-city",
    "charlotte",
    "denver",
    "madison",
)

# Alias map: alternate slug → canonical slug. Mirrors the spec's "canonicalize
# to city slug" rule (and prevents the 6.6k/day 404 incident from
# /markets vs /dcpi slug drift — per the market-slugs memory). The canonical
# form for the Market Brief is the metro slug used by /markets/<slug>, so
# `ashburn` resolves to `northern-virginia`, and `san-jose`/`santa-clara`
# resolve to `silicon-valley`.
_CANONICAL_SLUG: dict[str, str] = {
    "ashburn": "northern-virginia",
    "nova": "northern-virginia",
    "dfw": "dallas",
    "dallas-fort-worth": "dallas",
    "phx": "phoenix",
    "atl": "atlanta",
    "chi": "chicago",
    # Wave 2 aliases — metro form is the canonical Market Brief URL,
    # DCPI city slug routes via MARKET_ALIAS below.
    "san-jose": "silicon-valley",
    "santa-clara": "silicon-valley",
    "sv": "silicon-valley",
    "nyc": "new-york",
    "new-york-city": "new-york",
    "ny": "new-york",
    "pdx": "portland",
    "slc": "salt-lake-city",
    "clt": "charlotte",
    "den": "denver",
    "msn": "madison",
}

# Reverse map: canonical METRO slug → CITY slug used by market_power_scores
# (DCPI uses city slugs, /markets uses metro slugs per market-slugs memory).
# Used by _section_hero's third match clause so the DB lookup resolves
# even when the URL uses the metro form.
MARKET_ALIAS: dict[str, str] = {
    "northern-virginia": "ashburn",
    # silicon-valley → DCPI stores as `santa-clara` (verified live
    # 2026-06-06: GET /api/v1/dcpi/scores/silicon-valley returns
    # _canonical_slug=santa-clara, market_slug=santa-clara).
    "silicon-valley": "santa-clara",
    # dallas, phoenix, atlanta, chicago, new-york, portland, hillsboro,
    # reno, columbus, salt-lake-city, charlotte, denver, madison:
    # DCPI uses the same slug as the URL.
}


_PRO_RANK = 4  # tier_registry rank for pro/founding


# ─────────────────────────────────────────────────────────────────────
# Embeddable Market Brief Widget (2026-06-06)
# -----------------------------------------------------------------
# Brokers / REITs embed the Market Brief on their own marketing sites
# via <iframe src="https://dchub.cloud/markets/<slug>/brief?embed=1">.
#
# Three surfaces:
#   1. GET /markets/<slug>/brief?embed=1  — the iframe payload
#      (strips nav/footer/share/upgrade strip; adds a non-removable
#      "Powered by DC Hub" attribution bar at the bottom for free
#      tier; X-Frame-Options ALLOWALL + CSP frame-ancestors *).
#   2. GET /markets/<slug>/brief/embed     — the embed-code generator
#      page (iframe code + copy button + live preview + PRO+ toggle).
#   3. GET /api/v1/admin/widget-embeds/stats?days=30 — admin diagnostic
#      and GET /api/v1/widget-embeds/recent — public press-release feed.
#
# Watermark removal for PRO+ uses an HMAC-signed pro_token:
#   pro_token = HMAC-SHA256(DCHUB_SESSION_SECRET, "embed|<slug>|<ts>")
# truncated to 24 hex chars (96 bits). Valid for 365 days from the ts.
# A small "DC Hub" corner attribution remains for legal/SEO even when
# the bar is stripped.
#
# Attribution is logged to widget_embeds via the upsert below on every
# embed render. Best-effort (one bad write never breaks an iframe).
# ─────────────────────────────────────────────────────────────────────

# Same secret-resolution chain as routes/session_cookie.py so a single
# DCHUB_SESSION_SECRET env var rotates everything.
_WIDGET_SIGN_SECRET = (
    os.environ.get("DCHUB_SESSION_SECRET")
    or os.environ.get("DCHUB_ADMIN_KEY")
    or "dchub-default-rotate-via-DCHUB_SESSION_SECRET-env"
).encode()

# Pro tokens valid for 1 year (brokers shouldn't have to refresh embed
# codes mid-campaign). Verification still re-checks the caller's tier
# at render time, so a token issued at PRO+ silently downgrades back
# to watermark if the underlying account churns.
_PRO_TOKEN_MAX_AGE_S = 365 * 24 * 3600


def _sign_pro_token(slug: str, issued_ts: int | None = None) -> str:
    """Return a `<ts>.<sig>` pro_token bound to `slug`. `ts` is the issue
    epoch (defaults to now); `sig` is HMAC-SHA256(secret, "embed|<slug>|<ts>")
    truncated to 24 hex chars."""
    ts = int(issued_ts if issued_ts is not None else time.time())
    payload = f"embed|{slug}|{ts}".encode()
    sig = hmac.new(_WIDGET_SIGN_SECRET, payload, hashlib.sha256).hexdigest()[:24]
    return f"{ts}.{sig}"


def _verify_pro_token(token: str | None, slug: str) -> bool:
    """True iff the pro_token is well-formed, within MAX_AGE, and the
    HMAC matches the (slug, ts) pair. NOTE: tier check is the CALLER's
    responsibility — this only validates the token itself."""
    if not token or "." not in token:
        return False
    try:
        ts_str, sig = token.split(".", 1)
        issued = int(ts_str)
    except (ValueError, AttributeError):
        return False
    if time.time() - issued > _PRO_TOKEN_MAX_AGE_S or issued < 0:
        return False
    payload = f"embed|{slug}|{issued}".encode()
    expected = hmac.new(_WIDGET_SIGN_SECRET, payload, hashlib.sha256).hexdigest()[:24]
    return hmac.compare_digest(sig, expected)


def _embed_host_from_referer(referer: str | None) -> tuple[str, str]:
    """Return (host, full_url) parsed from the Referer header. Empty
    strings if the header is missing/malformed. Strips port + lowercases
    host."""
    if not referer:
        return ("", "")
    try:
        p = urlparse(referer)
        host = (p.hostname or "").lower()
        # Drop default ports; keep odd ports as part of host for diagnostics.
        full = referer[:512]  # cap for the DB column
        return (host, full)
    except Exception:
        return ("", "")


def init_widget_embed_tables():
    """Defensive ALTER pattern (mirrors init_content_tables + feedback_forum
    + market_verdict_shifts). Creates widget_embeds and idempotently adds
    any missing columns. Safe on both fresh-boot and existing prod tables.

    Schema (UNIQUE on (market_slug, embed_host) — one row per market×host
    pair, view_count++ on each render):

      id              SERIAL PRIMARY KEY
      market_slug     TEXT NOT NULL
      embed_host      TEXT NOT NULL  -- parsed from Referer; '' if unknown
      embed_url       TEXT           -- full Referer URL, capped 512 chars
      embed_tier      TEXT           -- FREE / PRO+ / ANON
      first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
      last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
      view_count      INTEGER NOT NULL DEFAULT 0
    """
    conn = _conn()
    if conn is None:
        logger.warning("init_widget_embed_tables skipped: no DATABASE_URL")
        return
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS widget_embeds (
                        id              SERIAL PRIMARY KEY,
                        market_slug     TEXT NOT NULL,
                        embed_host      TEXT NOT NULL DEFAULT '',
                        embed_url       TEXT,
                        embed_tier      TEXT,
                        first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        view_count      INTEGER NOT NULL DEFAULT 0
                    )
                """)
                conn.commit()
            except Exception as e:
                try: conn.rollback()
                except Exception: pass
                logger.warning("widget_embeds CREATE skipped: %s", e)
            for col_def in [
                "embed_host TEXT NOT NULL DEFAULT ''",
                "embed_url TEXT",
                "embed_tier TEXT",
                "first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
                "last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
                "view_count INTEGER NOT NULL DEFAULT 0",
            ]:
                col = col_def.split()[0]
                try:
                    cur.execute(
                        f"ALTER TABLE widget_embeds ADD COLUMN IF NOT EXISTS {col_def}")
                    conn.commit()
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
            # UNIQUE index on (market_slug, embed_host) so the upsert below
            # uses ON CONFLICT (not partial — no WHERE — to avoid the PG
            # partial-index ON CONFLICT trap memorized in the codebase).
            try:
                cur.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS widget_embeds_uq
                       ON widget_embeds (market_slug, embed_host)
                """)
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
            # Sortable index for the public + admin stats endpoints.
            try:
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS widget_embeds_last_seen_idx
                       ON widget_embeds (last_seen_at DESC)
                """)
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
    finally:
        try: conn.close()
        except Exception: pass


def _log_widget_embed(slug: str, host: str, url: str, tier: str) -> None:
    """Upsert one row into widget_embeds. Best-effort — failures are
    swallowed so a missing column never breaks an iframe render."""
    if not slug:
        return
    conn = _conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    INSERT INTO widget_embeds
                        (market_slug, embed_host, embed_url, embed_tier, view_count)
                    VALUES (%s, %s, %s, %s, 1)
                    ON CONFLICT (market_slug, embed_host) DO UPDATE
                        SET last_seen_at = NOW(),
                            view_count   = widget_embeds.view_count + 1,
                            embed_url    = COALESCE(EXCLUDED.embed_url, widget_embeds.embed_url),
                            embed_tier   = COALESCE(EXCLUDED.embed_tier, widget_embeds.embed_tier)
                """, (slug, host or "", url or None, tier or None))
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
    except Exception:
        pass
    finally:
        try: conn.close()
        except Exception: pass


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
    # r-fix-3 (2026-06-06) — AUTHORITATIVE per the two live writers
    # (routes/dcpi.py:1473 + main.py:28548) and the CREATE TABLE
    # (routes/dcpi.py:153). The REAL columns are: market_slug, market_name,
    # state, iso, constraint_score, excess_power_score, time_to_power_months,
    # queue_wait_months, verdict, computed_at, ... — `verdict` DOES exist;
    # NEITHER `score` NOR `composite_score` exists (the DCPI composite is
    # computed on the fly in index_api, never stored). Earlier attempts kept
    # `score` (r-fix) then swapped to `composite_score` (r-fix-2) — BOTH
    # non-existent, so the query kept throwing into the bare except → None →
    # "not in coverage" for every market. Use only writer-guaranteed columns;
    # composite_score stays None (hero shows verdict + excess + constraint).
    # MARKET_ALIAS handles the metro↔city slug trap (northern-virginia↔ashburn).
    try:
        cur.execute("""
            SELECT market_slug, market_name, verdict,
                   excess_power_score, constraint_score, computed_at, iso
              FROM market_power_scores
             WHERE LOWER(market_slug) = LOWER(%s)
                OR LOWER(REPLACE(market_name, ' ', '-')) = LOWER(%s)
                OR LOWER(market_slug) = LOWER(%s)
             ORDER BY computed_at DESC LIMIT 1
        """, (slug, slug, MARKET_ALIAS.get(slug, slug)))
        r = cur.fetchone()
    except Exception:
        return None
    if not r:
        return None
    # Derive verdict from (constraint, excess) per Phase 229 matrix in
    # scripts/bulk_dcpi_score.py:37. Same thresholds the canonical scorer uses.
    constraint = float(r[4] or 0)
    excess     = float(r[3] or 0)
    if constraint == 0 and excess == 0:
        verdict = "NODATA"
    elif excess >= 60 and constraint <= 40:
        verdict = "BUILD"
    elif excess >= 50 and constraint <= 50:
        verdict = "BUILD"
    elif constraint >= 70 and excess <= 40:
        verdict = "AVOID"
    elif constraint >= 60 and excess <= 30:
        verdict = "AVOID"
    else:
        verdict = "CAUTION"
    return {
        "slug":              r[0],
        "name":              r[1],
        "state":             None,
        "iso":               r[6],
        "verdict":           (r[2] or verdict),
        "composite_score":   None,
        "excess_power":      _as_float(r[3]),
        "constraint_score":  _as_float(r[4]),
        "queue_wait_months": None,
        "time_to_power_mo":  None,
        "computed_at":       r[5].isoformat() if r[5] else None,
        "_computed_at_dt":   r[5],  # internal — used to compute live-as-of
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
    pdf_url  = f"https://dchub.cloud/markets/{slug}/brief.pdf"
    share_x = (f"https://twitter.com/intent/tweet?text="
               f"{name.replace(' ', '+')}+data+center+market+brief+%E2%80%94+DCPI+"
               f"{score_str.replace('/', '%2F')}+verdict+{verdict}"
               f"&url={page_url}")
    share_li = f"https://www.linkedin.com/sharing/share-offsite/?url={page_url}"

    # PRO+ users see a real Download PDF button; anon/free see an upgrade CTA.
    # Both render the same `.pdf-btn` slot in the share strip below.
    if is_pro:
        pdf_btn_html = (f'<a href="{pdf_url}" class="pdf-btn pro" '
                        f'download="dchub-market-brief-{slug}.pdf">'
                        f'Download PDF</a>')
    else:
        pdf_btn_html = ('<a href="/pricing?utm_source=market_brief_pdf" '
                        'class="pdf-btn upgrade">'
                        'Upgrade to download PDF</a>')

    # ── Citation block ────────────────────────────────────────────────
    citation_url = page_url
    citation_iso = (live_iso or "")[:19].replace("T", " ")
    citation = (f"DC Hub · <a href=\"{citation_url}\">{citation_url}</a> · "
                f"Live as of {citation_iso} UTC")

    _html = f"""<!doctype html>
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
:root{{--bg:#0a0a0f;--surf:#15151c;--surf2:#1b1b24;--b:rgba(255,255,255,0.08);--b2:rgba(255,255,255,0.14);--tx:#fafafa;--mut:#a1a1aa;--dim:#71717a;--ind:#818cf8;--vio:#a855f7;--cy:#22d3ee;--acc:{colors['pill_bg']};--grad:linear-gradient(135deg,#6366f1,#a855f7)}}
*{{box-sizing:border-box}}
body{{font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,sans-serif;max-width:920px;margin:0 auto;padding:2.75rem 1.25rem 4rem;background:var(--bg);color:#d4d4d8;line-height:1.65;-webkit-font-smoothing:antialiased;position:relative}}
body::before{{content:"";position:fixed;inset:0 0 auto 0;height:440px;background:radial-gradient(115% 380px at 16% -45%,color-mix(in srgb,var(--acc) 20%,transparent),transparent 72%);pointer-events:none;z-index:-1}}
h1{{font-weight:800;letter-spacing:-.025em;margin:0 0 .35rem;font-size:2.6rem;line-height:1.04;color:var(--tx)}}
h2{{font-size:1.18rem;font-weight:700;color:var(--tx);margin:2.5rem 0 .9rem;letter-spacing:-.01em;display:flex;align-items:center;gap:.6rem}}
h2::before{{content:"";width:.5rem;height:1.05rem;border-radius:3px;background:var(--acc);opacity:.92}}
h3.sub{{font-size:.95rem;font-weight:600;color:var(--mut);margin:1.5rem 0 .5rem;letter-spacing:.02em;text-transform:uppercase}}
.live-pill{{display:inline-flex;align-items:center;gap:.4rem;background:var(--surf);border:1px solid var(--b);border-radius:999px;padding:.32rem .78rem;font-size:.7rem;color:var(--mut);font-family:'JetBrains Mono',monospace;margin-left:.6rem;vertical-align:middle}}
.live-dot{{width:.5rem;height:.5rem;background:#10b981;border-radius:50%;animation:pulse 2.5s ease-in-out infinite;box-shadow:0 0 0 3px rgba(16,185,129,.14)}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.35}}}}
.verdict-pill{{display:inline-block;background:{colors['pill_bg']};color:{colors['pill_fg']};font-weight:700;font-size:.8rem;padding:.42rem .95rem;border-radius:8px;letter-spacing:.05em;text-transform:uppercase;box-shadow:0 2px 18px -5px color-mix(in srgb,var(--acc) 65%,transparent)}}
.score{{font-family:'JetBrains Mono',monospace;color:var(--tx);font-weight:600;font-size:.95rem;margin-left:.6rem}}
.sub{{color:var(--dim);font-size:.82rem;margin:0 0 1.75rem;font-family:'JetBrains Mono',monospace}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:.75rem;margin:1rem 0 2rem}}
.kpi{{background:linear-gradient(160deg,var(--surf) 0%,var(--surf2) 100%);border:1px solid var(--b);border-left:3px solid var(--acc);border-radius:12px;padding:1rem 1.15rem;display:flex;flex-direction:column;gap:.4rem;transition:transform .14s ease,border-color .14s ease}}
.kpi:hover{{transform:translateY(-2px);border-color:var(--b2)}}
.kpi-l{{font-size:.66rem;color:var(--dim);text-transform:uppercase;letter-spacing:.08em;font-family:'JetBrains Mono',monospace}}
.kpi-v{{font-size:1.45rem;color:var(--tx);font-weight:700;letter-spacing:-.01em}}
.grid3{{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));gap:.6rem;margin:.5rem 0 1.5rem}}
.cell{{background:var(--surf);border:1px solid var(--b);border-radius:11px;padding:.8rem 1rem;display:flex;justify-content:space-between;align-items:center;gap:.75rem;font-size:.88rem;transition:border-color .14s ease}}
.cell:hover{{border-color:var(--b2)}}
.cell b{{color:var(--dim);font-size:.66rem;text-transform:uppercase;letter-spacing:.06em;font-weight:500;font-family:'JetBrains Mono',monospace}}
.cell span{{color:var(--tx);font-weight:600;font-family:'JetBrains Mono',monospace;text-align:right}}
.na{{color:var(--dim)!important;opacity:.45;font-weight:400!important}}
table{{width:100%;border-collapse:collapse;margin:.5rem 0 1.5rem;font-size:.88rem;background:var(--surf);border:1px solid var(--b);border-radius:12px;overflow:hidden}}
th,td{{padding:.6rem .9rem;text-align:left;border-bottom:1px solid var(--b)}}
th{{background:rgba(255,255,255,0.03);font-size:.68rem;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);font-family:'JetBrains Mono',monospace;font-weight:500}}
td{{color:var(--tx);font-family:'JetBrains Mono',monospace}}
tbody tr{{transition:background .14s ease}}
tbody tr:hover{{background:rgba(255,255,255,0.018)}}
tbody tr:last-child td{{border-bottom:none}}
.risk-list{{padding-left:0;list-style:none;margin:.5rem 0 1.5rem}}
.risk-list li{{background:var(--surf);border:1px solid var(--b);border-radius:10px;padding:.7rem .95rem;margin-bottom:.5rem;font-size:.88rem}}
.risk-list b{{color:var(--mut);font-weight:600;margin-right:.4rem}}
.outlook p{{font-size:1.02rem;margin:1rem 0;color:#d4d4d8}}
.share{{display:flex;gap:.5rem;flex-wrap:wrap;margin:2rem 0}}
.share a{{background:var(--surf);border:1px solid var(--b);border-radius:8px;padding:.5rem 1rem;color:var(--ind);text-decoration:none;font-size:.85rem;font-family:'JetBrains Mono',monospace}}
.share a:hover{{border-color:var(--ind)}}
.pdf-btn.pro{{background:var(--grad)!important;color:#fff!important;font-weight:600;border:none!important}}
.pdf-btn.upgrade{{background:rgba(99,102,241,.12)!important;color:#a5b4fc!important;border:1px dashed #6366f1!important}}
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
  <a href="/markets/{slug}/brief/embed" rel="nofollow">Embed this brief</a>
  {pdf_btn_html}
</div>

<div class="citation">{citation}</div>

<p class="footer">Powered by <a href="https://dchub.cloud">DC Hub</a> · Source-of-truth data center market intelligence · 2,000+ tracked deals · 21,433 facilities · 232 markets · JSON: <a href="/api/v1/market-brief/{slug}">/api/v1/market-brief/{slug}</a></p>

<script src="/js/dchub-nav.js" defer></script>
</body>
</html>"""
    # Elegance (2026-06-06): dim "not available" values so a thin-data market
    # (e.g. Cheyenne) reads as intentional, not broken — instead of a wall of
    # stark em-dashes. Safe no-op when a market has full data.
    for _pat, _rep in (
        ('<span class="kpi-v">—</span>', '<span class="kpi-v na">—</span>'),
        ('<span>—</span>',               '<span class="na">—</span>'),
        ('<td>—</td>',                   '<td class="na">—</td>'),
    ):
        _html = _html.replace(_pat, _rep)
    return _html


# ─────────────────────────────────────────────────────────────────────
# Embed render — slim iframe payload (no nav, no footer, no upgrade
# upsell, no share strip). Watermark bar at the bottom is non-removable
# for FREE/ANON; PRO+ with a valid pro_token gets a small corner mark
# instead. Same 9-section data the live brief uses.
# ─────────────────────────────────────────────────────────────────────


def _render_embed_html(brief: dict, *, watermark_off: bool) -> str:
    """Slim iframe-friendly HTML. Drops nav/footer/share/upgrade. Always
    shows a "Powered by DC Hub" attribution: as a full-width bar for
    FREE / unverified callers (`watermark_off=False`), or as a small
    bottom-right corner link for PRO+ callers with a valid pro_token
    (`watermark_off=True`). Caller is responsible for sending the
    iframe-friendly response headers."""
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
    citation_iso = (live_iso or "")[:19].replace("T", " ")

    def _fmt_mw(v):
        if v is None:
            return "—"
        try: return f"{float(v):,.0f} MW"
        except (TypeError, ValueError): return "—"

    def _fmt_int(v):
        if v is None:
            return "—"
        try: return f"{int(v):,}"
        except (TypeError, ValueError): return "—"

    def _fmt_months(v):
        if v is None:
            return "—"
        try: return f"{float(v):.1f} mo"
        except (TypeError, ValueError): return "—"

    # KPI tiles — same as the full brief but capped at 4 for the iframe
    # width budget (most embedders use ~640-960px wide).
    kpi_pairs = [
        ("Operational", _fmt_mw(kpis.get("operational_mw"))),
        ("Pipeline",    _fmt_mw(kpis.get("pipeline_mw"))),
        ("Facilities",  _fmt_int(kpis.get("facility_count"))),
        ("Queue Wait",  _fmt_months(kpis.get("queue_months"))),
    ]
    kpi_html = "\n".join(
        f'<div class="kpi"><span class="kpi-l">{lab}</span>'
        f'<span class="kpi-v">{val}</span></div>'
        for lab, val in kpi_pairs)

    # Teaser narrative — first 50 words (more aggressive than the full
    # brief's 80, because iframes are usually shorter).
    narrative = outlook.get("narrative_md") or ""
    words = narrative.split()
    teaser = " ".join(words[:50])
    if len(words) > 50:
        teaser += "…"

    canonical_url = f"https://dchub.cloud/markets/{slug}/brief"
    powered_by_url = (
        f"https://dchub.cloud/markets/{slug}/brief?utm_source=embed"
        f"&utm_medium=widget&utm_campaign=powered_by")

    # Attribution: full bar when watermark is ON, small corner link when OFF.
    if watermark_off:
        attribution_html = (
            f'<a class="dc-corner" href="{powered_by_url}" '
            f'target="_blank" rel="noopener">DC Hub</a>'
        )
        body_extra_pad = ""
    else:
        attribution_html = (
            '<div class="dc-bar">'
            f'<span>Powered by <a href="{powered_by_url}" target="_blank" '
            f'rel="noopener">DC Hub</a> · '
            f'<a href="{powered_by_url}" target="_blank" rel="noopener">'
            f'Source-of-truth data center market intelligence</a></span>'
            '<a class="dc-bar-cta" href="https://dchub.cloud/pricing'
            '?utm_source=embed&utm_medium=widget" target="_blank" '
            'rel="noopener">Remove watermark</a>'
            '</div>'
        )
        body_extra_pad = "padding-bottom:3.5rem;"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} Market Brief · DC Hub</title>
<meta name="robots" content="noindex,nofollow">
<link rel="canonical" href="{canonical_url}">
<style>
:root{{--bg:#0a0a0f;--surf:#131319;--b:rgba(255,255,255,0.08);--tx:#fafafa;--mut:#a1a1aa;--dim:#71717a;--ind:#818cf8}}
*{{box-sizing:border-box}}
html,body{{margin:0;padding:0}}
body{{font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,sans-serif;background:var(--bg);color:#d4d4d8;line-height:1.55;-webkit-font-smoothing:antialiased;padding:1.25rem 1.25rem 1rem;{body_extra_pad}}}
h1{{font-weight:700;letter-spacing:-.02em;margin:0 0 .25rem;font-size:1.5rem;color:var(--tx)}}
h2{{font-size:.95rem;font-weight:600;color:var(--tx);margin:1rem 0 .5rem;letter-spacing:-.01em;text-transform:uppercase}}
.live-pill{{display:inline-flex;align-items:center;gap:.3rem;background:var(--surf);border:1px solid var(--b);border-radius:999px;padding:.2rem .55rem;font-size:.65rem;color:var(--mut);font-family:'JetBrains Mono',monospace;margin-left:.4rem;vertical-align:middle}}
.live-dot{{width:.4rem;height:.4rem;background:#10b981;border-radius:50%}}
.verdict-pill{{display:inline-block;background:{colors['pill_bg']};color:{colors['pill_fg']};font-weight:700;font-size:.7rem;padding:.3rem .7rem;border-radius:6px;letter-spacing:.04em;text-transform:uppercase}}
.score{{font-family:'JetBrains Mono',monospace;color:var(--tx);font-weight:600;font-size:.8rem;margin-left:.5rem}}
.sub{{color:var(--dim);font-size:.72rem;margin:.25rem 0 1rem;font-family:'JetBrains Mono',monospace}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:.5rem;margin:.5rem 0 1rem}}
.kpi{{background:var(--surf);border:1px solid var(--b);border-radius:8px;padding:.55rem .7rem;display:flex;flex-direction:column;gap:.2rem}}
.kpi-l{{font-size:.6rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em;font-family:'JetBrains Mono',monospace}}
.kpi-v{{font-size:1rem;color:var(--tx);font-weight:600}}
.outlook{{font-size:.85rem;color:#d4d4d8;background:var(--surf);border:1px solid var(--b);border-radius:8px;padding:.7rem .85rem;margin:.5rem 0 1rem}}
.outlook a{{color:var(--ind);text-decoration:none}}
.deep-cta{{display:block;text-align:center;background:rgba(99,102,241,.12);border:1px dashed #6366f1;border-radius:8px;padding:.55rem .85rem;color:#a5b4fc;text-decoration:none;font-size:.78rem;font-family:'JetBrains Mono',monospace;margin:.5rem 0 1rem}}
.deep-cta:hover{{background:rgba(99,102,241,.2)}}
.dc-bar{{position:fixed;left:0;right:0;bottom:0;background:linear-gradient(90deg,#0a0a0f 0%,#131319 100%);border-top:1px solid var(--b);padding:.55rem 1rem;display:flex;align-items:center;justify-content:space-between;gap:.75rem;font-size:.7rem;color:var(--mut);font-family:'JetBrains Mono',monospace;z-index:100}}
.dc-bar a{{color:var(--ind);text-decoration:none}}
.dc-bar-cta{{background:rgba(99,102,241,.16);border:1px solid rgba(129,140,248,.45);border-radius:6px;padding:.25rem .65rem;color:#c7d2fe!important;font-weight:600;white-space:nowrap}}
.dc-corner{{position:fixed;right:.6rem;bottom:.5rem;font-size:.6rem;color:var(--dim);font-family:'JetBrains Mono',monospace;text-decoration:none;opacity:.55;letter-spacing:.04em;z-index:100}}
.dc-corner:hover{{opacity:1;color:var(--ind)}}
</style>
</head>
<body>

<h1>{name}<span class="live-pill"><span class="live-dot"></span>Live · {live_age_str}</span></h1>
<p class="sub"><span class="verdict-pill">{verdict}</span><span class="score">DCPI {score_str}</span> · Live as of {citation_iso} UTC</p>

<h2>At a Glance</h2>
<div class="kpis">
{kpi_html}
</div>

<h2>12-Month Outlook</h2>
<div class="outlook">{teaser or 'Outlook narrative will render on the next cron pass.'}</div>

<a class="deep-cta" href="{canonical_url}" target="_blank" rel="noopener">
Open the full brief on dchub.cloud → Power &amp; Grid · Pipeline · Operators · M&amp;A · Comps · Risk
</a>

{attribution_html}

</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────
# Embed-code generator page — public, shows the iframe code with a
# copy button + a live preview iframe + (for PRO+) a "remove watermark"
# toggle wired to a freshly minted signed pro_token. Anon sees the
# free iframe + "Sign up to remove watermark" upsell.
# ─────────────────────────────────────────────────────────────────────


def _render_embed_codegen_html(slug: str, name: str, verdict: str,
                                score_str: str, tier: str,
                                is_pro: bool) -> str:
    """Embed-code generator (public). PRO+ callers get a watermark-off
    toggle wired to a fresh signed token. Anon/FREE callers see the
    free embed + an upgrade CTA — the toggle is still visible but
    disabled, so the value prop is legible."""
    pro_token = _sign_pro_token(slug) if is_pro else ""
    free_iframe_src = f"https://dchub.cloud/markets/{slug}/brief?embed=1"
    pro_iframe_src = (f"https://dchub.cloud/markets/{slug}/brief?embed=1"
                       f"&pro_token={pro_token}") if pro_token else ""
    # Embed code shown in the <textarea>: defaults to the free iframe;
    # the toggle JS swaps in the pro-token version when checked.
    free_code = (
        f'<iframe src="{free_iframe_src}" '
        f'width="100%" height="540" frameborder="0" '
        f'style="border:1px solid #1f2937;border-radius:10px;'
        f'max-width:720px" loading="lazy" '
        f'title="{name} Market Brief — DC Hub"></iframe>')
    pro_code = (
        f'<iframe src="{pro_iframe_src}" '
        f'width="100%" height="540" frameborder="0" '
        f'style="border:1px solid #1f2937;border-radius:10px;'
        f'max-width:720px" loading="lazy" '
        f'title="{name} Market Brief — DC Hub"></iframe>') if pro_token else ""
    # HTML escape the code-block contents (it's literal HTML the user
    # will copy — we want to display the <iframe> source, not render it).
    from html import escape as _esc
    free_code_disp = _esc(free_code)
    pro_code_disp = _esc(pro_code) if pro_code else ""

    if is_pro:
        toggle_html = f"""
<label class="toggle">
  <input type="checkbox" id="watermark-toggle" checked>
  <span>Show "Powered by DC Hub" bar (uncheck for PRO+ watermark-off)</span>
</label>
<p class="hint">Your PRO+ token is good for 1 year. Re-load this page to mint a fresh one.</p>"""
    else:
        toggle_html = """
<label class="toggle disabled">
  <input type="checkbox" id="watermark-toggle" checked disabled>
  <span>Show "Powered by DC Hub" bar — watermark-off requires PRO+</span>
</label>
<p class="hint"><a href="/pricing?utm_source=embed_codegen">Upgrade to PRO ($499/mo)</a> to remove the watermark.</p>"""

    title_safe = _esc(name)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Embed the {title_safe} Market Brief · DC Hub</title>
<meta name="description" content="Embed the live DC Hub Market Brief for {title_safe} on your own site — verdict {verdict} ({score_str}), refreshed every 6 hours. Free with a 'Powered by DC Hub' bar; PRO+ removes the watermark.">
<link rel="canonical" href="https://dchub.cloud/markets/{slug}/brief/embed">
<link rel="stylesheet" href="/static/dchub-brand.css">
<style>
:root{{--bg:#0a0a0f;--surf:#131319;--b:rgba(255,255,255,0.08);--tx:#fafafa;--mut:#a1a1aa;--dim:#71717a;--ind:#818cf8;--grad:linear-gradient(135deg,#6366f1,#a855f7)}}
*{{box-sizing:border-box}}
body{{font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,sans-serif;max-width:960px;margin:0 auto;padding:2rem 1.25rem 3rem;background:var(--bg);color:#d4d4d8;line-height:1.6}}
h1{{font-weight:700;letter-spacing:-.02em;margin:0 0 .25rem;font-size:2rem;color:var(--tx)}}
h2{{font-size:1.1rem;font-weight:600;color:var(--tx);margin:1.75rem 0 .5rem}}
.sub{{color:var(--dim);font-size:.85rem;margin:.25rem 0 1.5rem;font-family:'JetBrains Mono',monospace}}
textarea{{width:100%;min-height:140px;background:var(--surf);border:1px solid var(--b);border-radius:10px;padding:.85rem 1rem;color:#e4e4e7;font-family:'JetBrains Mono','SF Mono',monospace;font-size:.78rem;line-height:1.5;resize:vertical}}
.copy-row{{display:flex;gap:.5rem;margin-top:.5rem}}
.btn{{background:var(--grad);color:#fff;border:none;border-radius:8px;padding:.55rem 1.1rem;font-weight:600;font-size:.85rem;cursor:pointer;font-family:inherit}}
.btn.sec{{background:transparent;color:var(--ind);border:1px solid var(--b)}}
.toggle{{display:flex;align-items:center;gap:.55rem;background:var(--surf);border:1px solid var(--b);border-radius:8px;padding:.6rem .85rem;margin:.75rem 0 .25rem;font-size:.85rem;color:var(--tx)}}
.toggle.disabled{{opacity:.6}}
.toggle input{{width:1rem;height:1rem;accent-color:#818cf8}}
.hint{{font-size:.78rem;color:var(--mut);margin:.25rem 0 1rem;font-family:'JetBrains Mono',monospace}}
.hint a{{color:var(--ind);text-decoration:none}}
.preview{{margin:1rem 0 2rem;background:var(--surf);border:1px solid var(--b);border-radius:12px;padding:1rem;overflow:hidden}}
.preview iframe{{display:block;border:1px solid var(--b);border-radius:8px;width:100%;height:540px}}
.code-note{{background:rgba(99,102,241,.08);border:1px solid rgba(129,140,248,.25);border-radius:8px;padding:.7rem .9rem;font-size:.8rem;color:#c7d2fe;margin:1.5rem 0;font-family:'JetBrains Mono',monospace}}
.code-note a{{color:#a5b4fc;text-decoration:none}}
.footer{{color:var(--dim);font-size:.78rem;margin-top:2.5rem;padding-top:1rem;border-top:1px solid var(--b);font-family:'JetBrains Mono',monospace}}
.footer a{{color:var(--ind);text-decoration:none}}
</style>
</head>
<body>

<h1>Embed the {title_safe} Market Brief</h1>
<p class="sub">A turnkey, live-updating market intelligence widget for your broker / REIT / fund site. Refreshed every 6 hours. Verdict {verdict} ({score_str}).</p>

<h2>1. Copy the embed code</h2>
<textarea id="embed-code" readonly>{free_code_disp}</textarea>
<div class="copy-row">
  <button class="btn" onclick="copyEmbed()">Copy embed code</button>
  <a class="btn sec" href="/markets/{slug}/brief?embed=1" target="_blank" rel="noopener">Open standalone</a>
</div>

<h2>2. Watermark</h2>
{toggle_html}

<h2>3. Live preview</h2>
<div class="preview">
  <iframe id="preview-frame" src="{free_iframe_src}" loading="lazy"
          title="{title_safe} Market Brief preview"></iframe>
</div>

<div class="code-note">
The widget is responsive — width:100% adapts to your container. Default height is 540px;
adjust as needed. The brief data is fetched server-side and rendered with a 6h edge
cache. <a href="/widget-example.html">See a full example</a> · <a href="/api/v1/widget-embeds/recent">Who else embeds DC Hub?</a>
</div>

<p class="footer">Powered by <a href="https://dchub.cloud">DC Hub</a> · <a href="/markets/{slug}/brief">Open the brief on dchub.cloud</a> · <a href="/premium">Browse all PRO tools</a></p>

<script>
const FREE_CODE = {json.dumps(free_code)};
const PRO_CODE  = {json.dumps(pro_code)};
const FREE_SRC  = {json.dumps(free_iframe_src)};
const PRO_SRC   = {json.dumps(pro_iframe_src)};
const IS_PRO    = {('true' if is_pro else 'false')};

function copyEmbed() {{
  const ta = document.getElementById('embed-code');
  ta.select();
  document.execCommand('copy');
  const btn = document.querySelector('.btn');
  const t = btn.textContent;
  btn.textContent = 'Copied!';
  setTimeout(() => {{ btn.textContent = t; }}, 1500);
}}

const toggle = document.getElementById('watermark-toggle');
if (toggle && IS_PRO) {{
  toggle.addEventListener('change', () => {{
    const watermarkOn = toggle.checked;
    const ta = document.getElementById('embed-code');
    const frame = document.getElementById('preview-frame');
    if (watermarkOn) {{
      ta.value = FREE_CODE;
      frame.src = FREE_SRC;
    }} else {{
      ta.value = PRO_CODE;
      frame.src = PRO_SRC;
    }}
  }});
}}
</script>

</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────
# PDF export — Phase 2 (2026-06-06)
#
# Brokers want to drop the brief into PowerPoint decks. We render the
# SAME 9-section data through a print-tuned HTML shell + weasyprint,
# adding a cover page, page numbers, and a footer watermark. PRO+ gated
# (anon → 402); 1h edge cache because a fresh render is expensive.
#
# Linux Docker runtime needs the native libs (libpango1.0-0 + libcairo2
# + libpangoft2-1.0-0). Local arm64 macOS dev needs them via Homebrew —
# CI tests against the Linux image. On import failure we raise 503
# from the route (NOT silently render a TODO stub).
# ─────────────────────────────────────────────────────────────────────

# Slimmer print-mode HTML: drops nav/share/blur/script, swaps the dark
# theme for paper, sized for letter, leaves room for @page header/footer.
# Footer watermark is fully baked into the @page rule via a Python f-string
# at render time (the watermark and date are fixed for a given render).
_PDF_BASE_CSS_TMPL = """
@page {
  size: Letter;
  margin: 0.75in 0.6in 0.95in 0.6in;
  @bottom-left {
    content: "DC Hub · WATERMARK_TOKEN · Generated DATE_TOKEN";
    font: 8pt 'Helvetica', sans-serif;
    color: #71717a;
    padding-top: 6pt;
  }
  @bottom-right {
    content: "Page " counter(page) " of " counter(pages);
    font: 8pt 'Helvetica', sans-serif;
    color: #71717a;
    padding-top: 6pt;
  }
}
@page :first {
  margin: 0;
  @bottom-left { content: none; }
  @bottom-right { content: none; }
}
body { font-family: 'Helvetica', 'Arial', sans-serif; color: #0a0a0f; font-size: 10pt; line-height: 1.55; margin: 0; }

/* ── COVER PAGE ── */
.cover { page: cover; height: 100vh; padding: 0; margin: 0; page-break-after: always; position: relative;
         background: linear-gradient(160deg, #0a0a0f 0%, #131319 60%, #1a1a26 100%); color: #fafafa; }
.cover-inner { padding: 1.4in 0.9in 1in 0.9in; height: 100%; display: flex; flex-direction: column; }
.cover-logo { font-size: 11pt; letter-spacing: .08em; text-transform: uppercase; color: #a1a1aa; font-weight: 600; }
.cover-title { font-size: 38pt; font-weight: 700; letter-spacing: -0.02em; margin: 0.55in 0 0.15in 0; line-height: 1.05; }
.cover-sub { font-size: 13pt; color: #a1a1aa; margin: 0 0 0.5in 0; }
.cover-verdict-pill { display: inline-block; font-weight: 700; font-size: 11pt; padding: 0.10in 0.30in;
                      border-radius: 6pt; letter-spacing: .04em; text-transform: uppercase; }
.cover-meta { margin-top: auto; font-size: 10pt; color: #a1a1aa; border-top: 1px solid rgba(255,255,255,0.12);
              padding-top: 0.25in; line-height: 1.7; }
.cover-meta b { color: #fafafa; font-weight: 600; display: inline-block; min-width: 1.4in; }
.cover-prepared { margin-top: 0.4in; font-size: 11pt; color: #fafafa; }
.cover-prepared .line { display: inline-block; border-bottom: 1pt solid #6366f1; min-width: 3.2in; margin-left: 0.15in; padding-bottom: 1pt; }

/* ── BODY PAGES ── */
h1 { font-size: 18pt; font-weight: 700; margin: 0 0 6pt 0; color: #0a0a0f; letter-spacing: -.01em; }
h2 { font-size: 12pt; font-weight: 700; margin: 18pt 0 6pt 0; color: #131319; border-bottom: 1pt solid #d4d4d8; padding-bottom: 3pt; }
.lead { color: #52525b; font-size: 10pt; margin: 0 0 12pt 0; }
.verdict-pill { display: inline-block; font-weight: 700; font-size: 9pt; padding: 2pt 8pt; border-radius: 4pt; letter-spacing: .04em; text-transform: uppercase; vertical-align: middle; }
.score { font-family: 'JetBrains Mono','Courier New', monospace; color: #131319; font-weight: 600; margin-left: 6pt; }
.kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6pt; margin: 4pt 0 14pt 0; }
.kpi { border: 1px solid #d4d4d8; border-radius: 4pt; padding: 6pt 8pt; }
.kpi-l { font-size: 7pt; color: #71717a; text-transform: uppercase; letter-spacing: .05em; font-weight: 500; }
.kpi-v { font-size: 11pt; color: #0a0a0f; font-weight: 600; margin-top: 2pt; }
table { width: 100%; border-collapse: collapse; margin: 4pt 0 12pt 0; font-size: 9pt; }
th, td { padding: 4pt 6pt; text-align: left; border-bottom: 1px solid #e4e4e7; }
th { background: #f4f4f5; font-size: 7pt; text-transform: uppercase; letter-spacing: .05em; color: #52525b; font-weight: 600; }
td { color: #18181b; vertical-align: top; }
.grid3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 5pt; margin: 4pt 0 12pt 0; }
.cell { border: 1px solid #d4d4d8; border-radius: 4pt; padding: 5pt 7pt; font-size: 9pt; }
.cell b { color: #71717a; font-size: 7pt; text-transform: uppercase; letter-spacing: .04em; font-weight: 500; display: block; margin-bottom: 2pt; }
.cell span { color: #0a0a0f; font-weight: 600; }
.risk-list { padding-left: 0; list-style: none; margin: 4pt 0 12pt 0; }
.risk-list li { border: 1px solid #d4d4d8; border-radius: 4pt; padding: 5pt 8pt; margin-bottom: 3pt; font-size: 9pt; }
.risk-list b { color: #52525b; font-weight: 600; margin-right: 4pt; }
.outlook p { font-size: 10pt; margin: 6pt 0; color: #18181b; }
.citation { border: 1px solid #d4d4d8; border-radius: 4pt; padding: 6pt 8pt; font-size: 8pt;
            color: #52525b; margin: 16pt 0 6pt 0; font-family: 'JetBrains Mono','Courier New',monospace; }
.section { page-break-inside: avoid; }
"""


def _render_pdf_html(brief: dict) -> str:
    """Build a print-tuned HTML for weasyprint — cover page first, then
    the same data the live brief renders but stripped of nav/blur/share/
    print-fragile CSS. PRO-only path (called only by the PDF route after
    `_is_pro()` gate)."""
    slug = brief.get("slug") or ""
    hero = brief.get("hero") or {}
    live = brief.get("live_as_of") or {}
    kpis = brief.get("kpis") or {}
    outlook = brief.get("outlook") or {}
    pg = brief.get("power_grid") or {}
    pipe = brief.get("pipeline") or []
    ops = brief.get("operators") or []
    ma = brief.get("ma") or []
    comps = brief.get("comps") or {}
    risk = brief.get("risk") or {}

    name = hero.get("name") or slug.replace("-", " ").title()
    verdict = hero.get("verdict") or "—"
    score = hero.get("composite_score")
    score_str = f"{score}/100" if score is not None else "—"
    colors = _verdict_colors(verdict)

    live_iso = live.get("iso") or hero.get("computed_at") or ""
    citation_iso = (live_iso or "")[:19].replace("T", " ")
    today_iso = datetime.date.today().isoformat()
    page_url = f"dchub.cloud/markets/{slug}/brief"

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

    def _money(v):
        if v is None:
            return "—"
        try:
            return f"${float(v):,.0f}"
        except (TypeError, ValueError):
            return "—"

    def _row(cells):
        return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"

    # ── KPI tiles (limit to top 4 so the at-a-glance fits a single row) ──
    kpi_pairs = [
        ("Operational", _fmt_mw(kpis.get("operational_mw"))),
        ("Pipeline",    _fmt_mw(kpis.get("pipeline_mw"))),
        ("Facilities",  _fmt_int(kpis.get("facility_count"))),
        ("Queue Wait",  _fmt_months(kpis.get("queue_months"))),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="kpi-l">{lab}</div>'
        f'<div class="kpi-v">{val}</div></div>'
        for lab, val in kpi_pairs)

    # ── Power & Grid grid ──
    pg_html = (
        '<div class="grid3">'
        f'<div class="cell"><b>ISO</b><span>{pg.get("iso") or "—"}</span></div>'
        f'<div class="cell"><b>Queue Capacity</b><span>{_fmt_mw(pg.get("queue_capacity_mw"))}</span></div>'
        f'<div class="cell"><b>Queue Wait</b><span>{_fmt_months(pg.get("queue_wait_months"))}</span></div>'
        f'<div class="cell"><b>Reserve Margin</b><span>{_fmt_pct(pg.get("reserve_margin_pct"))}</span></div>'
        f'<div class="cell"><b>Gen Additions 12mo</b><span>{_fmt_mw(pg.get("gen_additions_mw"))}</span></div>'
        f'<div class="cell"><b>Interconnect Pending</b><span>{_fmt_mw(pg.get("interconnection_pending_mw"))}</span></div>'
        '</div>'
    )

    # ── Pipeline (top 8 — trim for print) ──
    pipe_top = pipe[:8]
    pipe_rows = "\n".join(
        _row([p.get("operator") or "—",
              (p.get("facility") or "—")[:60],
              _fmt_mw(p.get("power_mw")),
              (p.get("status") or "—")[:20],
              p.get("eta") or "—"])
        for p in pipe_top) or _row(["—", "No pipeline tracked", "—", "—", "—"])
    pipe_html = (
        '<table><thead><tr><th>Operator</th><th>Facility</th>'
        '<th>Power</th><th>Status</th><th>ETA</th></tr></thead>'
        f'<tbody>{pipe_rows}</tbody></table>')

    # ── Operators (top 5) ──
    ops_rows = "\n".join(
        _row([o.get("operator") or "—",
              _fmt_int(o.get("facility_count")),
              _fmt_mw(o.get("total_mw"))])
        for o in ops[:5]) or _row(["No operator data yet", "—", "—"])
    ops_html = (
        '<table><thead><tr><th>Operator</th>'
        '<th>Facilities</th><th>Total MW</th></tr></thead>'
        f'<tbody>{ops_rows}</tbody></table>')

    # ── M&A (top 8) ──
    ma_rows = "\n".join(
        _row([m.get("date") or "—",
              (m.get("buyer") or "—")[:30],
              (m.get("seller") or "—")[:30],
              _money(m.get("value")),
              _fmt_mw(m.get("mw"))])
        for m in ma[:8]) or _row(["—", "No M&A in last 24mo", "—", "—", "—"])
    ma_html = (
        '<table><thead><tr><th>Date</th><th>Buyer</th>'
        '<th>Seller</th><th>Value</th><th>MW</th></tr></thead>'
        f'<tbody>{ma_rows}</tbody></table>')

    # ── Comps (compressed: 4 powered-shell + 4 land) ──
    comps_ps = (comps.get("powered_shell") or [])[:4]
    comps_ld = (comps.get("land") or [])[:4]
    def _comp_rows(items, label):
        return "\n".join(
            _row([c.get("date") or "—",
                  (c.get("asset") or c.get("buyer") or "—")[:40],
                  _money(c.get("value")), _fmt_mw(c.get("mw"))])
            for c in items) or _row(["—", f"No {label} comps", "—", "—"])
    comps_html = (
        '<table><thead><tr><th colspan="4" style="background:#fff;color:#71717a;'
        'font-weight:600;letter-spacing:.04em">Powered Shell</th></tr>'
        '<tr><th>Date</th><th>Asset</th><th>Value</th><th>MW</th></tr></thead>'
        f'<tbody>{_comp_rows(comps_ps, "powered-shell")}</tbody></table>'
        '<table><thead><tr><th colspan="4" style="background:#fff;color:#71717a;'
        'font-weight:600;letter-spacing:.04em">Land</th></tr>'
        '<tr><th>Date</th><th>Asset</th><th>Value</th><th>MW</th></tr></thead>'
        f'<tbody>{_comp_rows(comps_ld, "land")}</tbody></table>')

    # ── Risk ──
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

    # ── Outlook (full narrative — PRO+ gate already enforced upstream) ──
    narrative = outlook.get("narrative_md") or ""
    out_paragraphs = "".join(
        f"<p>{p.strip()}</p>" for p in narrative.split("\n\n") if p.strip()
    ) or "<p>Outlook narrative will be generated on the next cron pass.</p>"

    citation = f"DC Hub · https://{page_url} · Live as of {citation_iso} UTC"

    # Cover-page verdict pill background uses the same palette as the web pill
    cover_pill_style = (f"background:{colors['pill_bg']};"
                        f"color:{colors['pill_fg']};")
    # Body verdict pill (small) — same colors but on white background
    body_pill_style = (f"background:{colors['pill_bg']};"
                       f"color:{colors['pill_fg']};")

    # Watermark + date are baked into the @page rule via the CSS template
    # (string-set + attr() didn't survive weasyprint's @page footer slot
    # cleanly, so we substitute at Python render time).
    css = (_PDF_BASE_CSS_TMPL
           .replace("WATERMARK_TOKEN", f"dchub.cloud/markets/{slug}/brief")
           .replace("DATE_TOKEN", today_iso))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{name} Market Brief · DC Hub</title>
<style>{css}</style>
</head>
<body>

<!-- ── COVER PAGE ── -->
<section class="cover">
  <div class="cover-inner">
    <div class="cover-logo">DC Hub · Source-of-Truth Data Center Market Intelligence</div>
    <div class="cover-title">{name}<br/>Market Brief</div>
    <div class="cover-sub">DCPI {score_str} · Live as of {citation_iso} UTC</div>
    <div><span class="cover-verdict-pill" style="{cover_pill_style}">{verdict}</span></div>
    <div class="cover-prepared">Prepared for:<span class="line">&nbsp;</span></div>
    <div class="cover-meta">
      <div><b>Market</b> {name}</div>
      <div><b>Verdict</b> {verdict} ({score_str})</div>
      <div><b>Generated</b> {today_iso}</div>
      <div><b>Source URL</b> https://{page_url}</div>
    </div>
  </div>
</section>

<!-- ── HEADER ── -->
<h1>{name}</h1>
<p class="lead">Market Brief · <span class="verdict-pill" style="{body_pill_style}">{verdict}</span><span class="score">DCPI {score_str}</span> · Live as of {citation_iso} UTC</p>

<div class="section">
  <h2>At a Glance</h2>
  <div class="kpis">{kpi_html}</div>
</div>

<div class="section">
  <h2>Power &amp; Grid</h2>
  {pg_html}
</div>

<div class="section">
  <h2>Pipeline (top 8)</h2>
  {pipe_html}
</div>

<div class="section">
  <h2>Operator Footprint (top 5)</h2>
  {ops_html}
</div>

<div class="section">
  <h2>M&amp;A Activity (24mo)</h2>
  {ma_html}
</div>

<div class="section">
  <h2>Comps</h2>
  {comps_html}
</div>

<div class="section">
  <h2>Risk Factors</h2>
  {risk_html}
</div>

<div class="section">
  <h2>12-Month Outlook</h2>
  <div class="outlook">{out_paragraphs}</div>
</div>

<div class="citation">{citation}</div>

</body>
</html>"""


def _render_pdf_for_slug(slug: str, tier: str = "PRO") -> bytes:
    """End-to-end: build the brief data → render print HTML → weasyprint PDF.
    Raises ImportError if weasyprint native libs aren't loadable; raises
    ValueError if the market isn't covered. Callers (the route) translate
    those into HTTP status codes. Used directly by the requested smoke test:

        python3 -c "from routes.market_brief import _render_pdf_for_slug; \\
                    _render_pdf_for_slug('dallas')"
    """
    # Force a PRO render so the deep sections populate even when called
    # outside an HTTP context (smoke tests + the future prewarm cron).
    brief = _build_brief(slug, tier=tier)
    if not brief.get("ok"):
        raise ValueError(f"brief_unavailable:{brief.get('error') or 'unknown'}")
    html = _render_pdf_html(brief)
    # Lazy import — keeps the module importable even when weasyprint's
    # native deps aren't installed (the JSON + HTML routes still serve).
    from weasyprint import HTML  # noqa: WPS433  (deferred import is deliberate)
    return HTML(string=html, base_url="https://dchub.cloud/").write_pdf()


# ─────────────────────────────────────────────────────────────────────
# Pre-warm helper (called by crawler_scheduler._run_market_brief_warm)
# ─────────────────────────────────────────────────────────────────────

def prewarm_seed_markets() -> dict:
    """Pre-build the brief for each seed slug so the first visitor doesn't
    pay the cold fan-out cost. Best-effort — never raises. Sleeps 1s
    between markets so a 15-market warm doesn't hammer the 1-replica
    backend's connection pool (per the dchub-backend-flapping memory).
    Returns a short status dict for logging."""
    import time as _time
    out: dict = {"warmed": 0, "errors": 0, "slugs": []}
    n = len(SEED_MARKETS)
    for i, slug in enumerate(SEED_MARKETS):
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
        # 1s between markets to avoid DB connection-pool exhaustion;
        # skip the final sleep so the cron isn't idle for nothing.
        if i < n - 1:
            try:
                _time.sleep(1.0)
            except Exception:
                pass
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


def _admin_authorized() -> bool:
    """Gate /api/v1/admin/* endpoints on the canonical X-Admin-Key header.
    Same convention as routes/api_usage_tracker.py — DCHUB_ADMIN_KEY env
    var is the shared secret. Returns False if either side is missing."""
    import hmac as _hmac
    ac = (request.headers.get("X-Admin-Key", "") or "").split()
    ac = ac[0] if ac else ""
    ae = (os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("ADMIN_API_KEY") or "").split()
    ae = ae[0] if ae else ""
    return bool(ac and ae and _hmac.compare_digest(ac, ae))


@market_brief_bp.route("/api/v1/admin/market-brief/discover-eligible-markets",
                       methods=["GET"])
def admin_discover_eligible_markets():
    """List every market in `market_power_scores` that is currently
    eligible to be added to SEED_MARKETS — i.e. has a published row
    computed within the last 7 days. Operators can spot-check the list
    and add slugs to SEED_MARKETS without code changes elsewhere.

    Gated on X-Admin-Key (DCHUB_ADMIN_KEY env var). Response shape:
      {
        "ok": true,
        "count": <int>,
        "already_seeded": [<slug>, ...],
        "candidates": [
          {"slug": ..., "name": ..., "state": ..., "iso": ...,
           "verdict": ..., "computed_at": ..., "is_seeded": bool}
        ]
      }
    """
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    c = _conn()
    if c is None:
        return jsonify({"ok": False, "error": "no_database"}), 500
    rows: list[dict] = []
    try:
        with c.cursor() as cur:
            # Try the published-flag form first; some deployments don't
            # have the column yet, so fall back to a plain freshness gate.
            try:
                cur.execute("""
                    SELECT market_slug, market_name, state, iso, verdict, computed_at
                      FROM market_power_scores
                     WHERE published = TRUE
                       AND computed_at > NOW() - INTERVAL '7 days'
                     ORDER BY market_slug
                """)
            except Exception:
                # `published` column absent — fall back to freshness only.
                c.rollback()
                cur.execute("""
                    SELECT market_slug, market_name, state, iso, verdict, computed_at
                      FROM market_power_scores
                     WHERE computed_at > NOW() - INTERVAL '7 days'
                     ORDER BY market_slug
                """)
            # Build the set of canonical seeded slugs (via _CANONICAL_SLUG
            # for aliases) AND the DCPI city-slug form (via MARKET_ALIAS),
            # so e.g. `santa-clara` lights up as seeded by `silicon-valley`.
            seeded = set(SEED_MARKETS)
            for canonical in SEED_MARKETS:
                city = MARKET_ALIAS.get(canonical)
                if city:
                    seeded.add(city)
            for r in cur.fetchall():
                slug = r[0]
                rows.append({
                    "slug":        slug,
                    "name":        r[1],
                    "state":       r[2],
                    "iso":         r[3],
                    "verdict":     r[4],
                    "computed_at": r[5].isoformat() if r[5] else None,
                    "is_seeded":   slug in seeded,
                })
    except Exception as e:
        try:
            c.close()
        except Exception:
            pass
        return jsonify({"ok": False, "error": f"db_error:{type(e).__name__}"}), 500
    finally:
        try:
            c.close()
        except Exception:
            pass
    return jsonify({
        "ok":             True,
        "count":          len(rows),
        "already_seeded": sorted(SEED_MARKETS),
        "candidates":     rows,
    }), 200


@market_brief_bp.route("/brief", methods=["GET"])
@market_brief_bp.route("/briefs", methods=["GET"])
def html_market_brief_index():
    """Brief INDEX / picker — choose any covered market, go to its full brief.
    Replaces the hard-coded single-market deep link the nav used to carry.
    Dark site brand + global nav so it reads as part of the main site."""
    import html as _h
    rows = []
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT DISTINCT ON (market_slug)
                   market_slug, market_name, state, iso, verdict,
                   excess_power_score, constraint_score
                 FROM market_power_scores
                 ORDER BY market_slug, computed_at DESC""")
            for r in cur.fetchall():
                rows.append({"slug": r[0], "name": r[1] or r[0], "state": r[2] or "",
                             "iso": r[3] or "", "verdict": (r[4] or "WATCH"),
                             "excess": r[5], "constraint": r[6]})
    except Exception:
        rows = []

    def _rank(m):
        v = (m["verdict"] or "").upper()
        # BUILD first (best excess), then WATCH, then AVOID (worst constraint)
        if "BUILD" in v:  return (0, -(m["excess"] or 0))
        if "AVOID" in v:  return (2, -(m["constraint"] or 0))
        return (1, -(m["excess"] or 0))
    rows.sort(key=_rank)

    cards = []
    for m in rows:
        v = (m["verdict"] or "WATCH").upper()
        col = "#34d399" if "BUILD" in v else ("#f87171" if "AVOID" in v else "#fbbf24")
        if m["excess"] is not None and "AVOID" not in v:
            metric = f"Excess {float(m['excess']):.0f}"
        elif m["constraint"] is not None:
            metric = f"Constraint {float(m['constraint']):.0f}"
        else:
            metric = ""
        nm = _h.escape(m["name"]); st = _h.escape(m["state"]); iso = _h.escape(m["iso"])
        search = _h.escape(f"{m['name']} {m['state']} {m['iso']} {v}".lower())
        sub = " · ".join(x for x in [st, iso] if x)
        cards.append(
            f'<a class="mc" href="/markets/{_h.escape(m["slug"])}/brief" data-s="{search}">'
            f'<div class="mc-top"><span class="mc-name">{nm}</span>'
            f'<span class="mc-v" style="color:{col};border-color:{col}">{_h.escape(v)}</span></div>'
            f'<div class="mc-sub">{sub}</div>'
            f'<div class="mc-metric">{metric}</div></a>'
        )
    cards_html = "".join(cards) or '<p class="empty">Market scores are refreshing — check back shortly.</p>'
    count = len(rows)

    CSS = """
:root{--bg:#0a0a0f;--surf:#131319;--surf2:#1a1a22;--b:rgba(255,255,255,.09);--tx:#fafafa;--mut:#a1a1aa;--dim:#71717a;--ind:#818cf8;--vio:#a855f7;--cy:#22d3ee}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#d4d4d8;font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,system-ui,sans-serif;line-height:1.55}
.wrap{max-width:1100px;margin:0 auto;padding:2.5rem 1.25rem 4rem}
.kick{font-family:'JetBrains Mono',monospace;font-size:.72rem;letter-spacing:.16em;text-transform:uppercase;color:var(--cy);margin:0 0 .5rem}
h1{font-size:2rem;font-weight:800;letter-spacing:-.02em;color:#fff;margin:0 0 .4rem}
.lead{color:var(--mut);max-width:640px;margin:0 0 1.5rem}
.search{width:100%;max-width:440px;background:var(--surf);border:1px solid var(--b);border-radius:10px;padding:.7rem .9rem;color:var(--tx);font-size:.95rem;margin:0 0 1.5rem}
.search::placeholder{color:var(--dim)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:.85rem}
.mc{display:block;background:var(--surf);border:1px solid var(--b);border-radius:11px;padding:.95rem 1.05rem;text-decoration:none;color:inherit;transition:transform .12s,border-color .12s}
.mc:hover{transform:translateY(-2px);border-color:var(--ind)}
.mc-top{display:flex;justify-content:space-between;align-items:center;gap:.5rem}
.mc-name{font-weight:700;color:#fff;font-size:1.02rem}
.mc-v{font-family:'JetBrains Mono',monospace;font-size:.6rem;font-weight:700;letter-spacing:.05em;border:1px solid;border-radius:999px;padding:.12rem .5rem}
.mc-sub{color:var(--dim);font-size:.74rem;font-family:'JetBrains Mono',monospace;margin:.3rem 0 .15rem}
.mc-metric{color:var(--mut);font-size:.82rem;font-weight:600}
.empty{color:var(--dim)}
.foot{margin-top:2.5rem;padding-top:1.25rem;border-top:1px solid var(--b);color:var(--dim);font-size:.8rem;font-family:'JetBrains Mono',monospace}
.foot a{color:var(--ind);text-decoration:none}
"""
    HTML = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<meta name=theme-color content='#0a0a0f'>"
        f"<title>Market Briefs · {count} markets · DC Hub</title>"
        "<meta name='description' content='Live per-market data-center intelligence briefs — power, grid, pipeline, operators, M&A, comps, risk. Pick any market.'>"
        "<link rel=stylesheet href='https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap'>"
        "<link rel=stylesheet href='/static/dchub-brand.css'>"
        "<script src='/js/dchub-nav.js' defer></script>"
        f"<style>{CSS}</style></head><body><div class=wrap>"
        "<p class=kick>Premium · Market Brief</p>"
        "<h1>Market Briefs</h1>"
        f"<p class=lead>Live per-market intelligence — power &amp; grid, construction pipeline, operators, M&amp;A, lease comps, and risk. Pick any of the {count} scored markets for its full brief.</p>"
        "<input class=search id=q placeholder='Search market, state, ISO, or verdict…' autocomplete=off>"
        f"<div class=grid id=grid>{cards_html}</div>"
        "<div class=foot>DC Hub · agent-native data-center &amp; power intelligence · "
        "<a href='/dcpi'>Open DCPI</a> · <a href='/pricing'>Pricing</a></div>"
        "</div>"
        "<script>(function(){var q=document.getElementById('q'),g=document.getElementById('grid');"
        "if(!q)return;q.addEventListener('input',function(){var t=q.value.trim().toLowerCase();"
        "g.querySelectorAll('.mc').forEach(function(c){c.style.display=(!t||(c.getAttribute('data-s')||'').indexOf(t)>-1)?'':'none';});});})();</script>"
        "</body></html>"
    )
    return Response(HTML, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=600"})


@market_brief_bp.route("/markets/<slug>/brief", methods=["GET"])
def html_market_brief(slug):
    """HTML render — 9 sections, paywalled by tier.

    Supports `?embed=1` for iframe embedding (slim payload, no nav/footer/
    share/upgrade, X-Frame-Options ALLOWALL, CSP frame-ancestors *,
    Referer logged to widget_embeds). PRO+ embedders pass a signed
    `pro_token=<ts>.<sig>` to strip the watermark bar — verified via
    HMAC against DCHUB_SESSION_SECRET, gated on the caller's tier.
    """
    tier = _caller_tier()
    is_embed = request.args.get("embed", "").lower() in ("1", "true", "yes")
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
        suffix = "?embed=1" if is_embed else ""
        return redirect(f"/markets/{brief['redirect_to']}/brief{suffix}", code=301)

    # ── EMBED MODE ─────────────────────────────────────────────────
    if is_embed:
        canonical_slug = brief.get("slug") or _canonical(slug)
        # Watermark removal requires BOTH a valid signed token AND
        # an underlying PRO+ tier. Token alone is not enough — a churned
        # PRO subscriber loses watermark-off automatically.
        pro_token = request.args.get("pro_token", "").strip()
        watermark_off = (
            bool(pro_token)
            and _verify_pro_token(pro_token, canonical_slug)
            and _is_pro(tier)
        )
        # Attribution logging — best-effort, never blocks render.
        try:
            referer = request.headers.get("Referer") or request.headers.get("Referrer")
            host, full_url = _embed_host_from_referer(referer)
            tier_label = "PRO+" if watermark_off else ("FREE" if _is_pro(tier) else "ANON")
            _log_widget_embed(canonical_slug, host, full_url, tier_label)
        except Exception:
            pass
        html = _render_embed_html(brief, watermark_off=watermark_off)
        resp = Response(html, mimetype="text/html")
        resp.headers["Cache-Control"] = "public, max-age=21600, s-maxage=21600"
        resp.headers["X-Market-Brief-Tier"] = tier
        resp.headers["X-Market-Brief-Embed"] = "1"
        # Iframe-friendly: ALLOWALL + frame-ancestors *. The brief is
        # public data and the whole point of the embed is third-party
        # framing, so we explicitly opt-OUT of the default DENY/SAMEORIGIN.
        resp.headers["X-Frame-Options"] = "ALLOWALL"
        resp.headers["Content-Security-Policy"] = "frame-ancestors *"
        # CORS pre-flight friendliness for the live preview iframe.
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    # ── REGULAR HTML ───────────────────────────────────────────────
    html = _render_html(brief)
    return Response(html, mimetype="text/html",
                    headers={
                        # 2026-06-06 FIX: this page is PER-TIER gated (PRO sees
                        # 9 sections, anon sees the teaser + paywall). It was
                        # served `public, s-maxage=21600` — a 6h SHARED edge
                        # cache keyed only on URL — so whichever tier filled the
                        # cache (usually anon) was served to EVERYONE, incl PRO
                        # users → "gated even though I'm pro". Per-user pages must
                        # never sit in a shared cache. private+no-store; the CF
                        # worker's _originSaysNoStore honor-path makes it stick
                        # edge-side. (The cookieless catalog page /markets/<slug>
                        # stays cacheable; only the gated /brief goes dynamic.)
                        "Cache-Control": "private, no-store, no-cache, must-revalidate",
                        "CDN-Cache-Control": "no-store",
                        "Vary": "Cookie",
                        "X-Market-Brief-Tier": tier,
                    })


@market_brief_bp.route("/markets/<slug>/brief/embed", methods=["GET"])
def html_market_brief_embed_codegen(slug):
    """Embed-code generator page — public. Shows the iframe code with
    a copy button, a live preview iframe, and (for PRO+) a watermark-
    removal toggle wired to a freshly minted signed pro_token. Anon/FREE
    callers see the toggle disabled with an upgrade CTA."""
    tier = _caller_tier()
    canonical = _canonical(slug)
    if canonical != _norm_slug(slug):
        from flask import redirect
        return redirect(f"/markets/{canonical}/brief/embed", code=301)

    # We need the market name + verdict to render a useful codegen page;
    # cheapest path is the same _build_brief fan-out. PRO render so the
    # hero / verdict populate even for anon visitors (the codegen page
    # is read-only metadata, not paywalled data).
    brief = _build_brief(canonical, tier="ADMIN")
    if not brief.get("ok") and brief.get("error") == "market_not_found":
        # Same 200-with-shell pattern as the main brief route.
        sample = brief.get("sample_markets") or []
        sample_html = ", ".join(
            f'<a href="/markets/{s}/brief/embed">{s}</a>' for s in sample[:8])
        body = (
            f'<h1>Embed code — {slug}</h1>'
            f'<p>This market is not yet in our DCPI coverage. Try: {sample_html}</p>'
        )
        return Response(
            f"<!doctype html><html><head><meta charset=utf-8><title>Embed code · DC Hub</title>"
            f'<link rel="stylesheet" href="/static/dchub-brand.css"></head><body>{body}</body></html>',
            mimetype="text/html",
            headers={"Cache-Control": "public, max-age=300"})

    hero = brief.get("hero") or {}
    name = hero.get("name") or canonical.replace("-", " ").title()
    verdict = hero.get("verdict") or "—"
    score = hero.get("composite_score")
    score_str = f"{score}/100" if score is not None else "—"
    is_pro = _is_pro(tier)
    html = _render_embed_codegen_html(
        slug=canonical, name=name, verdict=verdict, score_str=score_str,
        tier=tier, is_pro=is_pro)
    # No edge cache: the page contains a freshly minted pro_token for
    # PRO+ callers. Aggressive caching would leak one user's token to
    # the next. 5 minutes is enough for "click copy, paste in CMS."
    return Response(html, mimetype="text/html", headers={
        "Cache-Control": "private, max-age=300",
        "X-Market-Brief-Tier": tier,
    })


@market_brief_bp.route("/api/v1/widget-embeds/recent", methods=["GET"])
def widget_embeds_recent():
    """PUBLIC — recent embed-host activity. Great for press-release
    bullets ("DC Hub is embedded by N broker / REIT sites"). We expose
    the host + market_slug + last_seen, never the full Referer URL
    (which could leak internal CMS preview paths)."""
    limit = max(1, min(50, int(request.args.get("limit", 20))))
    c = _conn()
    out = {"ok": True, "rows": [], "total_hosts": 0,
            "since": None, "until": datetime.datetime.utcnow().isoformat()}
    if c is None:
        out["ok"] = False
        out["error"] = "no_database"
        return jsonify(out), 200
    try:
        with c.cursor() as cur:
            try:
                cur.execute("""
                    SELECT embed_host,
                           market_slug,
                           SUM(view_count)        AS views,
                           MIN(first_seen_at)     AS first_seen,
                           MAX(last_seen_at)      AS last_seen
                      FROM widget_embeds
                     WHERE embed_host <> ''
                       AND embed_host NOT IN ('dchub.cloud', 'localhost')
                       AND embed_host NOT LIKE '%%.dchub.cloud'
                     GROUP BY embed_host, market_slug
                     ORDER BY last_seen DESC
                     LIMIT %s
                """, (limit,))
                for r in cur.fetchall():
                    out["rows"].append({
                        "embed_host":  r[0],
                        "market_slug": r[1],
                        "view_count":  int(r[2] or 0),
                        "first_seen":  r[3].isoformat() if r[3] else None,
                        "last_seen":   r[4].isoformat() if r[4] else None,
                    })
                # Distinct host count for the headline number.
                cur.execute("""
                    SELECT COUNT(DISTINCT embed_host)
                      FROM widget_embeds
                     WHERE embed_host <> ''
                       AND embed_host NOT IN ('dchub.cloud', 'localhost')
                       AND embed_host NOT LIKE '%%.dchub.cloud'
                """)
                r = cur.fetchone()
                out["total_hosts"] = int((r and r[0]) or 0)
                cur.execute("SELECT MIN(first_seen_at) FROM widget_embeds")
                r = cur.fetchone()
                if r and r[0]:
                    out["since"] = r[0].isoformat()
            except Exception as e:
                out["ok"] = False
                out["error"] = f"db_error:{type(e).__name__}"
    finally:
        try: c.close()
        except Exception: pass
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@market_brief_bp.route("/api/v1/admin/widget-embeds/stats", methods=["GET"])
def admin_widget_embeds_stats():
    """Admin diagnostic — top embedding domains in the last `days` window
    (default 30). Returns per-host roll-ups + per-market roll-ups + a
    full event count. Gated on X-Admin-Key (DCHUB_ADMIN_KEY)."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    days = max(1, min(365, int(request.args.get("days", 30))))
    c = _conn()
    if c is None:
        return jsonify({"ok": False, "error": "no_database"}), 500
    out = {"ok": True, "window_days": days, "top_hosts": [],
           "top_markets": [], "total_views": 0, "distinct_hosts": 0,
           "rows_in_window": 0}
    interval = f"{days} days"
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT embed_host, SUM(view_count), MAX(last_seen_at)
                  FROM widget_embeds
                 WHERE last_seen_at > NOW() - INTERVAL %s
                   AND embed_host <> ''
                 GROUP BY embed_host
                 ORDER BY SUM(view_count) DESC
                 LIMIT 25
            """, (interval,))
            for r in cur.fetchall():
                out["top_hosts"].append({
                    "host":       r[0],
                    "views":      int(r[1] or 0),
                    "last_seen":  r[2].isoformat() if r[2] else None,
                })
            cur.execute("""
                SELECT market_slug,
                       SUM(view_count),
                       COUNT(DISTINCT embed_host)
                  FROM widget_embeds
                 WHERE last_seen_at > NOW() - INTERVAL %s
                 GROUP BY market_slug
                 ORDER BY SUM(view_count) DESC
                 LIMIT 25
            """, (interval,))
            for r in cur.fetchall():
                out["top_markets"].append({
                    "market_slug":     r[0],
                    "views":           int(r[1] or 0),
                    "distinct_hosts":  int(r[2] or 0),
                })
            cur.execute("""
                SELECT COALESCE(SUM(view_count), 0),
                       COUNT(DISTINCT embed_host),
                       COUNT(*)
                  FROM widget_embeds
                 WHERE last_seen_at > NOW() - INTERVAL %s
            """, (interval,))
            r = cur.fetchone()
            if r:
                out["total_views"]    = int(r[0] or 0)
                out["distinct_hosts"] = int(r[1] or 0)
                out["rows_in_window"] = int(r[2] or 0)
    except Exception as e:
        out["ok"] = False
        out["error"] = f"db_error:{type(e).__name__}: {str(e)[:200]}"
        return jsonify(out), 500
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(out), 200


@market_brief_bp.route("/markets/<slug>/brief.pdf", methods=["GET"])
def pdf_market_brief(slug):
    """PDF export — PRO+ only.

    Phase 2 of the Market Brief spec. Same 9-section data, print-tuned
    HTML, weasyprint → PDF, cover page + page numbers + footer watermark.
    1h edge cache; filename headers so the broker's Downloads folder
    ends up with `dchub-market-brief-<slug>-YYYY-MM-DD.pdf`.

    Auth contract:
      - Anon/FREE  → 402 Payment Required + upgrade JSON
      - PRO+       → 200 application/pdf
      - missing slug → 404 (consistent with the JSON endpoint)
      - weasyprint native libs missing → 503 (Docker image regression)
    """
    tier = _caller_tier()
    if not _is_pro(tier):
        # Spec says: anon gets a clean 402 JSON, not a paywall HTML page,
        # so MCP / curl callers can react programmatically.
        return jsonify({
            "error":       "pdf_requires_pro",
            "upgrade_url": "/pricing",
            "message":     ("PDF export is a PRO feature ($499/mo). "
                            "Visit dchub.cloud/pricing to unlock."),
        }), 402
    canonical = _canonical(slug)
    if canonical != _norm_slug(slug):
        # 301 alias — preserves link equity AND the broker-shared filename
        # still says `northern-virginia` even if they typed `nova`.
        from flask import redirect
        return redirect(f"/markets/{canonical}/brief.pdf", code=301)
    try:
        pdf_bytes = _render_pdf_for_slug(canonical, tier=tier)
    except ImportError as e:
        # weasyprint native libs not loadable — surface clearly so the
        # ops team fixes the Docker image rather than the page silently
        # falling back to HTML.
        return jsonify({
            "error":   "pdf_engine_unavailable",
            "detail":  f"{type(e).__name__}: {str(e)[:200]}",
            "hint":    ("weasyprint native libs (libpango1.0-0, libcairo2, "
                        "libpangoft2-1.0-0) missing from the runtime image."),
        }), 503
    except ValueError as e:
        msg = str(e)
        if msg.startswith("brief_unavailable:market_not_found"):
            return jsonify({"error": "market_not_found", "slug": slug}), 404
        return jsonify({"error": "brief_unavailable", "detail": msg}), 502
    except Exception as e:
        return jsonify({
            "error":  "pdf_render_failed",
            "detail": f"{type(e).__name__}: {str(e)[:200]}",
        }), 500

    today_iso = datetime.date.today().isoformat()
    filename = f"dchub-market-brief-{canonical}-{today_iso}.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={
            # 1h edge cache — PDF render is expensive (~1-3s for weasyprint).
            "Cache-Control":       "public, max-age=3600, s-maxage=3600",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Market-Brief-Tier": tier,
            "X-Content-Type-Options": "nosniff",
        })


# ─────────────────────────────────────────────────────────────────────
# Bulk Market Brief API (2026-06-06) — Phase ZZZZZ-bulk
# -------------------------------------------------------------------
# BI/fintech integration surface. Tableau, Power BI, Hex, Snowflake all
# want ONE endpoint that returns every brief in a single round-trip
# (vs. N+1 polling of /api/v1/market-brief/<slug>). Three new endpoints:
#
#   GET /api/v1/market-brief/all           — all briefs (tier-scoped),
#                                            streamed if >50 markets
#   GET /api/v1/market-brief/diff?since=…  — only briefs computed after
#                                            the given iso8601 timestamp
#   GET /api/v1/market-brief/all.csv       — same data as /all, but CSV
#                                            (Excel-friendly download)
#
# Pagination: ?limit=50&offset=0 on /all (default 50, max 500 PRO+, max
# 50 free/anon — page through ENTERPRISE's 232 markets in 5 calls).
#
# Tier slicing (paywall on PER-MARKET sections still applies inside each
# brief — anon gets teaser sections; what changes here is the COUNT of
# markets returned):
#   ANON / FREE        → 5 markets   (SEED_MARKETS[:5] — wave 1)
#   IDENTIFIED         → 5 markets   (same — gated content unlocks at PRO)
#   DEVELOPER          → 5 markets
#   PRO / FOUNDING+    → 15 markets  (all SEED_MARKETS — wave 1 + 2)
#   ENTERPRISE         → ALL markets in market_power_scores (~232)
#
# Daily-cap rate limiting via mcp_call_log row-count over 24h window:
#   ANON               → 10 calls/day on /all
#   FREE (identified)  → 50 calls/day
#   PRO+               → unlimited
#
# Telemetry: every /all and /diff call writes one row to mcp_call_log
# (tool='market_brief_all' or 'market_brief_diff'), so the BI surface
# shows up in /by-the-numbers + the conversion-funnel reports.
# ─────────────────────────────────────────────────────────────────────

# Pagination defaults / caps (per spec — default 50 markets per page).
_BULK_DEFAULT_LIMIT = 50
_BULK_FREE_MAX_LIMIT = 50    # anon/free can't override beyond default
_BULK_PRO_MAX_LIMIT = 500    # PRO+ can pull 500 in one shot
_BULK_STREAM_THRESHOLD = 50  # spec: stream when >50 markets

# Daily-cap thresholds. None = unlimited.
_BULK_DAILY_CAPS: dict[str, int | None] = {
    "FREE":         10,    # anon / no key
    "IDENTIFIED":   50,    # identified key but no paid plan
    "DEVELOPER":    50,
    "PRO":          None,
    "FOUNDING":     None,
    "RESEARCH_SEED": None,
    "ENTERPRISE":   None,
    "ADMIN":        None,
}

# Canonical CSV column order (per spec item #4). Stable across releases
# so BI imports don't break when we add new internal sections.
_BULK_CSV_COLUMNS = (
    "market_slug", "market_name", "verdict", "composite_score",
    "excess_power", "constraint_score", "queue_wait_months", "iso",
    "state", "operational_mw", "pipeline_mw", "facility_count",
    "vacancy_pct", "lease_rate", "top_operator",
    "outlook_word_count", "live_as_of", "computed_at",
)


def _bulk_caller_id() -> str:
    """Stable per-caller key for mcp_call_log telemetry + daily-cap counts.
    Prefers api_key; falls back to CF-Connecting-IP, then remote_addr."""
    return (
        (request.headers.get("X-API-Key") or "").strip()
        or (request.cookies.get("dchub_token") or "").strip()
        or request.headers.get("CF-Connecting-IP", "")
        or request.remote_addr
        or "anon"
    )[:128]


def _bulk_log_call(tool: str, tier: str, status: str = "ok",
                   extra: str | None = None) -> None:
    """Insert one telemetry row into mcp_call_log. Best-effort — if the
    insert fails (column drift, DB down) we swallow it; an unobservable
    call is better than a 500 on the bulk endpoint."""
    caller = _bulk_caller_id()
    conn = _conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO mcp_call_log "
                    "  (api_key, tool, status, event_type, referrer, user_agent, timestamp) "
                    "VALUES (%s, %s, %s, %s, %s, %s, NOW())",
                    (
                        caller, tool, status, f"bulk:{tier}",
                        (request.headers.get("Referer") or extra or "")[:512],
                        (request.headers.get("User-Agent", "") or "")[:500],
                    ),
                )
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
    except Exception:
        pass
    finally:
        try: conn.close()
        except Exception: pass


def _bulk_daily_count(tool: str, caller: str) -> int:
    """Return the count of mcp_call_log rows for this (tool, api_key) in
    the last 24h. Best-effort — returns 0 on any DB error so we never
    falsely cap a paying caller because the counter is unreachable."""
    conn = _conn()
    if conn is None:
        return 0
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM mcp_call_log "
                    " WHERE tool = %s AND api_key = %s "
                    "   AND timestamp >= NOW() - INTERVAL '24 hours'",
                    (tool, caller),
                )
                r = cur.fetchone()
                return int(r[0] or 0) if r else 0
            except Exception:
                return 0
    finally:
        try: conn.close()
        except Exception: pass


def _bulk_check_daily_cap(tier: str, tool: str) -> tuple[bool, int | None, int]:
    """Returns (allowed, cap, current_count). cap=None means unlimited."""
    cap = _BULK_DAILY_CAPS.get((tier or "FREE").upper(), 10)
    if cap is None:
        return True, None, 0
    caller = _bulk_caller_id()
    count = _bulk_daily_count(tool, caller)
    return count < cap, cap, count


def _bulk_slugs_for_tier(tier: str) -> list[str]:
    """Return the list of market slugs the caller is entitled to in a
    bulk pull. ANON/FREE → 5 seed markets; PRO+ → 15 seed markets;
    ENTERPRISE → all market_power_scores rows in canonical slug form.

    Falls back to SEED_MARKETS on any DB error (so ENTERPRISE never
    silently downgrades to PRO if the lookup fails — they still get the
    15 hand-QA'd markets at minimum)."""
    t = (tier or "FREE").upper()
    if t in ("ENTERPRISE", "ADMIN", "RESEARCH_SEED"):
        # Pull every market in market_power_scores, canonicalize to the
        # /markets/<slug> form so the returned briefs use the same slug
        # as the /<slug> endpoint.
        conn = _conn()
        if conn is None:
            return list(SEED_MARKETS)
        try:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "SELECT DISTINCT market_slug FROM market_power_scores "
                        " WHERE market_slug IS NOT NULL "
                        " ORDER BY market_slug")
                    rows = [r[0] for r in cur.fetchall() if r[0]]
                except Exception:
                    rows = []
            # Reverse-map DCPI city slugs back to canonical metro slugs
            # where we have a Market Brief alias (so silicon-valley shows
            # up as silicon-valley, not santa-clara).
            city_to_metro = {v: k for k, v in MARKET_ALIAS.items()}
            canonical: list[str] = []
            seen: set[str] = set()
            for s in rows:
                slug = city_to_metro.get(s, _canonical(s))
                if slug not in seen:
                    seen.add(slug)
                    canonical.append(slug)
            # Make sure every SEED is included even if not yet in the
            # power-scores table (defensive, eager seeding).
            for s in SEED_MARKETS:
                if s not in seen:
                    seen.add(s)
                    canonical.append(s)
            return canonical
        finally:
            try: conn.close()
            except Exception: pass
    elif _is_pro(t):
        return list(SEED_MARKETS)  # all 15 wave-1+wave-2 seed markets
    else:
        # Anon / FREE / IDENTIFIED / DEVELOPER → first 5 seed markets
        # (wave 1 — northern-virginia, dallas, phoenix, atlanta, chicago)
        return list(SEED_MARKETS[:5])


def _bulk_parse_pagination(tier: str) -> tuple[int, int]:
    """Parse ?limit & ?offset query params with tier-aware caps."""
    try:
        raw_limit = int(request.args.get("limit", _BULK_DEFAULT_LIMIT))
    except (TypeError, ValueError):
        raw_limit = _BULK_DEFAULT_LIMIT
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    max_limit = _BULK_PRO_MAX_LIMIT if _is_pro(tier) else _BULK_FREE_MAX_LIMIT
    limit = max(1, min(raw_limit, max_limit))
    return limit, offset


def _bulk_build_briefs(slugs: list[str], tier: str) -> list[dict]:
    """Build briefs for every slug in the batched (non-streaming) path.

    Each brief still uses the existing best-effort per-section fan-out
    via _build_brief(); one brief failing never sinks the whole batch.
    """
    out: list[dict] = []
    for slug in slugs:
        try:
            brief = _build_brief(slug, tier)
            if brief.get("ok") or brief.get("error"):
                out.append(brief)
        except Exception as e:
            out.append({
                "ok":    False,
                "slug":  slug,
                "error": f"build_failed:{type(e).__name__}:{str(e)[:80]}",
            })
    return out


def _bulk_filter_changed(slugs: list[str], since: datetime.datetime) -> list[str]:
    """Return only the slugs whose market_power_scores.computed_at is
    after `since`. Used by the /diff endpoint for incremental refresh
    (Tableau-style poll every 6h, get only what shifted).

    One DB round-trip — IN-list query against the full slug set."""
    if not slugs:
        return []
    conn = _conn()
    if conn is None:
        return list(slugs)  # fail-open: better to over-return than skip shifts
    changed: list[str] = []
    try:
        with conn.cursor() as cur:
            try:
                # Canonical metro and DCPI city forms both possible — match either.
                city_forms = [MARKET_ALIAS.get(s, s) for s in slugs]
                lookup = list({*slugs, *city_forms})
                cur.execute(
                    "SELECT DISTINCT market_slug FROM market_power_scores "
                    " WHERE market_slug = ANY(%s) "
                    "   AND computed_at > %s",
                    (lookup, since),
                )
                hit = {r[0] for r in cur.fetchall() if r[0]}
                city_to_metro = {v: k for k, v in MARKET_ALIAS.items()}
                for s in slugs:
                    if s in hit or MARKET_ALIAS.get(s) in hit:
                        changed.append(s)
                    elif city_to_metro.get(s) in hit:
                        changed.append(s)
            except Exception:
                # Fail-open: if the diff query fails, return everything.
                return list(slugs)
    finally:
        try: conn.close()
        except Exception: pass
    return changed


def _bulk_csv_row_for_brief(brief: dict) -> list:
    """Project one brief into the canonical CSV column order. Missing
    fields are emitted as empty cells (Excel/Tableau handle that natively)."""
    hero = brief.get("hero") or {}
    kpis = brief.get("kpis") or {}
    outlook = brief.get("outlook") or {}
    live = brief.get("live_as_of") or {}
    return [
        hero.get("slug") or brief.get("slug") or "",
        hero.get("name") or "",
        hero.get("verdict") or "",
        hero.get("composite_score") if hero.get("composite_score") is not None else "",
        hero.get("excess_power") if hero.get("excess_power") is not None else "",
        hero.get("constraint_score") if hero.get("constraint_score") is not None else "",
        hero.get("queue_wait_months") if hero.get("queue_wait_months") is not None else "",
        hero.get("iso") or "",
        hero.get("state") or "",
        kpis.get("operational_mw") if kpis.get("operational_mw") is not None else "",
        kpis.get("pipeline_mw") if kpis.get("pipeline_mw") is not None else "",
        kpis.get("facility_count") if kpis.get("facility_count") is not None else "",
        kpis.get("vacancy_pct") if kpis.get("vacancy_pct") is not None else "",
        kpis.get("lease_rate") if kpis.get("lease_rate") is not None else "",
        kpis.get("top_operator") or "",
        outlook.get("word_count") if outlook.get("word_count") is not None else 0,
        live.get("iso") or "",
        hero.get("computed_at") or "",
    ]


def _bulk_429_response(tier: str, cap: int, count: int, tool: str):
    """Standard 429 payload when the per-tier daily cap is exhausted."""
    payload = {
        "error":         "daily_cap_exceeded",
        "tool":          tool,
        "tier":          tier,
        "limit":         f"{cap}/day",
        "current_count": count,
        "upgrade_url":   "/pricing?utm_source=market_brief_bulk",
        "message":       (f"Daily limit of {cap} calls reached for tier {tier}. "
                          "Upgrade to PRO ($499/mo) for unlimited bulk pulls."),
    }
    resp = jsonify(payload)
    resp.headers["Retry-After"] = "3600"
    resp.headers["X-RateLimit-Limit"] = str(cap)
    resp.headers["X-RateLimit-Remaining"] = "0"
    return resp, 429


@market_brief_bp.route("/api/v1/market-brief/all", methods=["GET"])
def api_market_brief_bulk():
    """Bulk endpoint — return every brief the caller's tier is entitled to.

    Tier scoping (count of markets returned):
      ANON/FREE   → 5 seed markets
      PRO+        → 15 seed markets
      ENTERPRISE  → all market_power_scores markets (~232)

    Section paywalls inside each brief are unchanged: anon/free still
    see Hero + KPIs + Outlook teaser; PRO+ sections (Power & Grid,
    Pipeline, Operators, M&A, Comps, Risk) remain locked at the per-brief
    level even within /all.

    Query params:
      ?limit=N    1..50 (FREE) or 1..500 (PRO+).  Default 50.
      ?offset=N   page offset.                    Default 0.

    Streams (Transfer-Encoding: chunked) when the response would include
    >50 briefs; otherwise returns a single JSON object.

    Rate-limit (daily, per api_key, via mcp_call_log):
      anon  →  10/day      free key → 50/day      PRO+ → unlimited
    """
    tier = _caller_tier()
    tool = "market_brief_all"

    # 1. Daily-cap gate (spec item #7).
    allowed, cap, count = _bulk_check_daily_cap(tier, tool)
    if not allowed:
        _bulk_log_call(tool, tier, status="rate_limited")
        return _bulk_429_response(tier, cap or 0, count, tool)

    # 2. Tier-scoped slug list + pagination.
    all_slugs = _bulk_slugs_for_tier(tier)
    total_available = len(all_slugs)
    limit, offset = _bulk_parse_pagination(tier)
    page_slugs = all_slugs[offset:offset + limit]
    page_count = len(page_slugs)
    as_of = datetime.datetime.utcnow().isoformat() + "Z"

    # 3. Telemetry (spec item #8).
    _bulk_log_call(tool, tier, status="ok",
                   extra=f"n={page_count}/{total_available};off={offset}")

    # 4. Streaming path — only when the actual response is large
    #    (>_BULK_STREAM_THRESHOLD briefs) so small ANON pulls stay
    #    single-shot JSON (Tableau dislikes chunked when it can avoid it).
    if page_count > _BULK_STREAM_THRESHOLD:
        def _stream():
            head = (
                '{"as_of":' + json.dumps(as_of) +
                ',"tier":' + json.dumps(tier) +
                ',"count":' + str(page_count) +
                ',"total_available":' + str(total_available) +
                ',"limit":' + str(limit) +
                ',"offset":' + str(offset) +
                ',"streamed":true' +
                ',"briefs":['
            )
            yield head
            first = True
            for slug in page_slugs:
                try:
                    brief = _build_brief(slug, tier)
                except Exception as e:
                    brief = {"ok": False, "slug": slug,
                             "error": f"build_failed:{type(e).__name__}"}
                if not first:
                    yield ","
                yield json.dumps(brief, default=str)
                first = False
            yield "]}"

        resp = Response(stream_with_context(_stream()), mimetype="application/json")
        resp.headers["Cache-Control"]      = "public, max-age=21600, s-maxage=21600"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["X-Market-Brief-Tier"] = tier
        resp.headers["X-Market-Brief-Mode"] = "stream"
        resp.headers["X-Total-Available"]   = str(total_available)
        return resp

    # 5. Batched (single-payload) path.
    briefs = _bulk_build_briefs(page_slugs, tier)
    payload = {
        "as_of":           as_of,
        "tier":            tier,
        "count":           len(briefs),
        "total_available": total_available,
        "limit":           limit,
        "offset":          offset,
        "streamed":        False,
        "briefs":          briefs,
    }
    resp = jsonify(payload)
    resp.headers["Cache-Control"]      = "public, max-age=21600, s-maxage=21600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["X-Market-Brief-Tier"] = tier
    resp.headers["X-Market-Brief-Mode"] = "batch"
    resp.headers["X-Total-Available"]   = str(total_available)
    return resp


@market_brief_bp.route("/api/v1/market-brief/diff", methods=["GET"])
def api_market_brief_diff():
    """Incremental-refresh endpoint — return only the briefs whose
    `market_power_scores.computed_at` is later than `?since=<iso8601>`.

    Use case: a BI tool that wants to do a 6-hourly refresh without
    re-downloading every brief — call /diff with the timestamp of the
    last successful pull and only get markets that have shifted.

    Same response shape + tier scoping as /all. If `?since` is missing
    or malformed, returns everything (equivalent to /all).
    """
    tier = _caller_tier()
    tool = "market_brief_diff"

    allowed, cap, count = _bulk_check_daily_cap(tier, tool)
    if not allowed:
        _bulk_log_call(tool, tier, status="rate_limited")
        return _bulk_429_response(tier, cap or 0, count, tool)

    # Parse `since`. Accept iso8601 with/without trailing Z + plain dates.
    since_raw = (request.args.get("since") or "").strip()
    since_dt: datetime.datetime | None = None
    if since_raw:
        try:
            normalized = since_raw[:-1] if since_raw.endswith("Z") else since_raw
            since_dt = datetime.datetime.fromisoformat(normalized)
            if since_dt.tzinfo is not None:
                since_dt = since_dt.replace(tzinfo=None)
        except (ValueError, AttributeError):
            since_dt = None

    all_slugs = _bulk_slugs_for_tier(tier)
    total_available = len(all_slugs)
    if since_dt is None:
        changed_slugs = all_slugs  # missing/invalid since → return all
        since_iso = None
    else:
        changed_slugs = _bulk_filter_changed(all_slugs, since_dt)
        since_iso = since_dt.isoformat() + "Z"

    limit, offset = _bulk_parse_pagination(tier)
    page_slugs = changed_slugs[offset:offset + limit]
    page_count = len(page_slugs)
    as_of = datetime.datetime.utcnow().isoformat() + "Z"

    _bulk_log_call(tool, tier, status="ok",
                   extra=f"since={since_iso};n={page_count}")

    if page_count > _BULK_STREAM_THRESHOLD:
        def _stream():
            head = (
                '{"as_of":' + json.dumps(as_of) +
                ',"since":' + json.dumps(since_iso) +
                ',"tier":' + json.dumps(tier) +
                ',"count":' + str(page_count) +
                ',"total_available":' + str(total_available) +
                ',"changed_total":' + str(len(changed_slugs)) +
                ',"limit":' + str(limit) +
                ',"offset":' + str(offset) +
                ',"streamed":true' +
                ',"briefs":['
            )
            yield head
            first = True
            for slug in page_slugs:
                try:
                    brief = _build_brief(slug, tier)
                except Exception as e:
                    brief = {"ok": False, "slug": slug,
                             "error": f"build_failed:{type(e).__name__}"}
                if not first:
                    yield ","
                yield json.dumps(brief, default=str)
                first = False
            yield "]}"

        resp = Response(stream_with_context(_stream()), mimetype="application/json")
        resp.headers["Cache-Control"]      = "public, max-age=21600, s-maxage=21600"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["X-Market-Brief-Tier"] = tier
        resp.headers["X-Market-Brief-Mode"] = "stream-diff"
        return resp

    briefs = _bulk_build_briefs(page_slugs, tier)
    payload = {
        "as_of":           as_of,
        "since":           since_iso,
        "tier":            tier,
        "count":           len(briefs),
        "total_available": total_available,
        "changed_total":   len(changed_slugs),
        "limit":           limit,
        "offset":          offset,
        "streamed":        False,
        "briefs":          briefs,
    }
    resp = jsonify(payload)
    resp.headers["Cache-Control"]      = "public, max-age=21600, s-maxage=21600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["X-Market-Brief-Tier"] = tier
    resp.headers["X-Market-Brief-Mode"] = "batch-diff"
    return resp


@market_brief_bp.route("/api/v1/market-brief/all.csv", methods=["GET"])
def api_market_brief_bulk_csv():
    """CSV mirror of /all — tier-gated identically, canonical column
    order in `_BULK_CSV_COLUMNS`. Streamed when >50 markets.

    Sets Content-Disposition so the browser/curl save it as
    `dchub-market-briefs-YYYY-MM-DD.csv`."""
    tier = _caller_tier()
    tool = "market_brief_all_csv"

    allowed, cap, count = _bulk_check_daily_cap(tier, tool)
    if not allowed:
        _bulk_log_call(tool, tier, status="rate_limited")
        return _bulk_429_response(tier, cap or 0, count, tool)

    all_slugs = _bulk_slugs_for_tier(tier)
    limit, offset = _bulk_parse_pagination(tier)
    page_slugs = all_slugs[offset:offset + limit]
    page_count = len(page_slugs)

    _bulk_log_call(tool, tier, status="ok",
                   extra=f"csv;n={page_count}/{len(all_slugs)}")

    today_iso = datetime.date.today().isoformat()
    filename = f"dchub-market-briefs-{today_iso}.csv"

    def _row_to_csv(row: list) -> str:
        """Render one CSV row using the stdlib csv module so quoting +
        escaping match Excel/RFC 4180."""
        sio = io.StringIO()
        csv.writer(sio).writerow(row)
        return sio.getvalue()

    if page_count > _BULK_STREAM_THRESHOLD:
        def _stream():
            yield _row_to_csv(list(_BULK_CSV_COLUMNS))
            for slug in page_slugs:
                try:
                    brief = _build_brief(slug, tier)
                    yield _row_to_csv(_bulk_csv_row_for_brief(brief))
                except Exception as e:
                    yield _row_to_csv([slug] + [""] * (len(_BULK_CSV_COLUMNS) - 2) +
                                       [f"build_failed:{type(e).__name__}"])

        resp = Response(stream_with_context(_stream()), mimetype="text/csv")
    else:
        sio = io.StringIO()
        w = csv.writer(sio)
        w.writerow(list(_BULK_CSV_COLUMNS))
        for brief in _bulk_build_briefs(page_slugs, tier):
            w.writerow(_bulk_csv_row_for_brief(brief))
        resp = Response(sio.getvalue(), mimetype="text/csv")

    resp.headers["Cache-Control"]      = "public, max-age=21600, s-maxage=21600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.headers["X-Market-Brief-Tier"] = tier
    return resp
