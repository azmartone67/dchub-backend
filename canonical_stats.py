"""
canonical_stats.py — Phase FF (2026-05-22)
==========================================
ONE source of truth for DC Hub's headline platform numbers, so every
generator (press releases, LinkedIn posts, emails, prompts) quotes the SAME
figure instead of drifting (the feed showed 11,000 / 20,000 / 21,000+ facilities
in the same week).

Root cause of the drift: older helpers (agent_hub.get_live_stats,
data_layers_api.get_facility_stats) count the LEGACY `facilities` table
(~12k) and even hardcode a 9,603 fallback. The canonical count is
`discovered_facilities` — "what we actually track" per /api/v1/stats (~21,382).

Usage:
    from canonical_stats import get_canonical_stats, facilities_phrase
    s = get_canonical_stats()            # {'facilities': 21382, 'countries': 178, ...}
    text = facilities_phrase()           # "21,000+"  (conservative, citation-safe floor)

Fail-safe: every query is wrapped; on any error we return conservative floors
that are never higher than reality, so a generator can't over-claim.
"""

from __future__ import annotations

import os
import time
import threading

# Conservative floors — used as fallback AND as the rounding basis for the
# "*_phrase()" helpers. Never set these above the true live numbers.
_FALLBACK = {
    "facilities": 21000,            # raw "tracked" floor (discovery pile, incl unmerged dupes)
    "facilities_verified": 400,    # deduped/active floor — citation-safe. 2026-06-23: re-floored 1800->1000 (live=1,066, so 1800 was a ~69% over-claim on DB-failure — the canonical_floor_above_live_reality finding). Trend kept dropping 3,141->2,848->1,903->1,066 as re-ingestion churns dedup flags. MUST stay <= reality — floors round DOWN; re-floor whenever live drops below it. [flag RESOLVED 2026-07-10 (issue #1539): the 'shrinking' 3,141->1,066->427->5 was the pending QUEUE draining (old filter included merged_at IS NULL); true fleet ~4,903 — dedup was never over-merging.] 2026-06-30: re-floored 1000->400 (live verified ~427 per brain L15; 1000 was again above reality).
    "countries": 170,
    "countries_verified": 30,       # deduped/active distinct floor (live ~33; country field dirty -> conservative)
    "markets": 300,          # 2026-06-08: Neon-verified COUNT(DISTINCT market_name) minus 3 aggregates = 300 (grew from 232 via intl expansion). Live query below; this is the fallback.
    # Curated M&A deal count — the buyer+seller-present subset (~4,255 live
    # 2026-07-16). This is DELIBERATELY NOT the raw `deals` table COUNT(*)
    # (~11.5K), which is a broader pile incl. capex/undisclosed/junk rows: the
    # live /api/v1/stats `deals` field returns that broad count, and publishing
    # it as "M&A deals" is a ~2.5x over-claim (the 2026-07-16 double-count trap).
    # Live-queried below with the same curated filter as /api/transactions;
    # deals_phrase() floors DOWN to "4,000+" so we never over-claim.
    "deals": 4000,
    "isos": 7,               # 7 live US ISOs (ERCOT, CAISO, NYISO, MISO, PJM, SPP, ISO-NE)
    "grid_operators": 10,    # 10 North-American grid operators w/ live data (7 US ISOs + TVA + BPA + IESO)
    "utility_bas": 43,       # 43 US utility balancing authorities (live EIA-930)
    # #60 (2026-06-02): live grid telemetry is now GLOBAL — 4 continents.
    # r-intl-0711 (2026-07-11): 5 continents — Japan (OCCTO areas, TSO
    # eria_jukyu), South Korea (KPX) and Brazil (ONS, adds South America) now
    # rank full-mix; Singapore (EMA/NEMS) live partial (demand+USEP, no mix).
    # Intl live grids beyond N. America: Great Britain (NESO/Elexon), 24 EU
    # bidding zones (ENTSO-E), Taiwan (Taipower), Japan (OCCTO), South Korea
    # (KPX), Brazil (ONS) — ranked; Australia (AEMO) + Singapore (EMA) partial;
    # plus EU gas transmission flows (ENTSOG, 10 countries). LIVE, not modeled.
    "grid_continents": 5,
    "intl_grid_regions": 31,  # GB(1) + EU(24) + TW(1) + JP(1) + KR(1) + BR(1) + AU(1) + SG(1)
    "eu_zones": 24,           # live ENTSO-E bidding zones (verified get_grid_scoreboard 2026-06-25; was ~12)
    "substations": 126427,    # HIFLD substations (had no SoT home before)
    "pipeline_gw": 369,       # construction pipeline GW (had no SoT home before)
}

_TTL_S = 600          # 10-minute cache; these move slowly
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


def _query_live() -> dict:
    """Best-effort live counts from the CANONICAL tables. Any failure on an
    individual metric falls back to the LAST-KNOWN-GOOD live value (the cache),
    or the static floor only at cold start — never raises.

    r-floor-freshness (2026-06-21): starting from `_cache` (not the static
    `_FALLBACK`) means a transient per-metric query failure keeps the freshest
    real value instead of reverting to a seed that may have gone stale-high as
    data churns — the durable form of the floor<=live invariant (the static seed
    is only the process-cold-start floor)."""
    out = dict(_cache) if _cache else dict(_FALLBACK)
    c = _conn()
    if c is None:
        return out
    try:
        cur = c.cursor()
        # Canonical facility count — discovered_facilities is the authoritative
        # table ("what we actually track"), NOT the legacy `facilities` table.
        try:
            cur.execute("SELECT COUNT(*) FROM discovered_facilities")
            n = int((cur.fetchone() or [0])[0] or 0)
            if n > 0:
                out["facilities"] = n            # raw "tracked" discovery pile
        except Exception:
            pass
        # VERIFIED/ACTIVE subset (deduped): the FLEET filter — excludes only
        # flagged duplicates. Lead honest copy with this; "tracked" (raw, above)
        # is the discovery pile including flagged duplicates.
        # 2026-07-10 (issue #1539): dropped `AND merged_at IS NULL` — the merge
        # pipeline stamps merged_at on EVERY promoted fleet row, so the old
        # combined filter counted the *unmerged pending queue* (which drains to
        # ~0 as the pipeline works), not the verified fleet (~4.9K). That
        # artifact fired canonical_floor_above_live_reality ("live=5 vs floor
        # 400") and made a healthy pipeline look dead. Queue counts belong to
        # the dedup/approval loops, never to "verified".
        try:
            cur.execute("SELECT COUNT(*) FROM discovered_facilities "
                        "WHERE COALESCE(is_duplicate,0)=0")
            n = int((cur.fetchone() or [0])[0] or 0)
            if n > 0:
                out["facilities_verified"] = n
        except Exception:
            pass
        # Distinct countries we have facilities in.
        try:
            cur.execute("SELECT COUNT(DISTINCT country) FROM discovered_facilities "
                        "WHERE country IS NOT NULL AND country <> ''")
            n = int((cur.fetchone() or [0])[0] or 0)
            if n > 0:
                out["countries"] = n
        except Exception:
            pass
        # Verified/active distinct countries (deduped). NB the country field is
        # dirty (some non-US cities tagged 'US'), so this is approximate — the
        # floor stays conservative.
        try:
            cur.execute("SELECT COUNT(DISTINCT country) FROM discovered_facilities "
                        "WHERE country IS NOT NULL AND country <> '' "
                        "AND COALESCE(is_duplicate,0)=0")
            n = int((cur.fetchone() or [0])[0] or 0)
            if n > 0:
                out["countries_verified"] = n
        except Exception:
            pass
        # Markets in the DCPI index. r73 (2026-06-08): TRUE count is
        # COUNT(DISTINCT market_name) minus the 3 aggregate regions
        # (pacific-nw-rural, rural-spp, upper-michigan) = 300 (Neon-verified).
        # DISTINCT market_name (not slug) collapses the dupe variants
        # (cheyenne+cheyenne-wy, portland+portland-or, st-louis+st.-louis).
        # Markets genuinely grew 232->300 via international expansion — no cap,
        # this is the real live count.
        try:
            cur.execute("SELECT COUNT(DISTINCT market_name) FROM market_power_scores "
                        "WHERE COALESCE(published, true) = true "
                        "AND market_slug NOT IN ('pacific-nw-rural','rural-spp','upper-michigan')")
            n = int((cur.fetchone() or [0])[0] or 0)
            if n > 0:
                out["markets"] = n
        except Exception:
            pass
        # Curated M&A deal count. ★DO NOT use COUNT(*) FROM deals here: that is
        # the RAW ~11.5K pile (capex/undisclosed/junk rows) that the live
        # /api/v1/stats `deals` field returns — publishing it as "M&A deals" is a
        # ~2.5x over-claim (2026-07-16 double-count trap). The honest curated
        # count is the buyer+seller-present subset (matches the live
        # /api/transactions total ~4,255 and deals_public_api.is_quality_deal).
        # deals_phrase() floors this DOWN to "4,000+".
        try:
            cur.execute(
                "SELECT COUNT(*) FROM deals "
                "WHERE buyer IS NOT NULL AND buyer <> '' "
                "AND lower(buyer) NOT IN ('tbd','unknown','n/a','na','none','undisclosed') "
                "AND seller IS NOT NULL AND seller <> '' "
                "AND lower(seller) NOT IN ('tbd','unknown','n/a','na','none','undisclosed')")
            n = int((cur.fetchone() or [0])[0] or 0)
            if n > 0:
                out["deals"] = n
        except Exception:
            pass
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


def get_canonical_stats(force: bool = False) -> dict:
    """Cached canonical stats. Keys: facilities, countries, markets, isos.
    Always returns a complete dict (floors on failure) — never raises."""
    global _cache, _cache_ts
    now = time.time()
    with _lock:
        if not force and _cache is not None and (now - _cache_ts) < _TTL_S:
            return dict(_cache)
    try:
        live = _query_live()
    except Exception:
        live = dict(_FALLBACK)
    with _lock:
        _cache = live
        _cache_ts = now
    return dict(live)


def _floor_phrase(n: int, step: int = 1000) -> str:
    """Round DOWN to a clean 'X,000+' floor so we never over-claim."""
    floored = (int(n) // step) * step
    return f"{floored:,}+"


def facilities_phrase() -> str:
    """Tracked (raw) floor, e.g. '21,000+' — the discovery pile (back-compat)."""
    return _floor_phrase(get_canonical_stats().get("facilities", _FALLBACK["facilities"]))


def facilities_verified_phrase() -> str:
    """Deduped/active floor, e.g. '2,800+' — what we've actually verified."""
    return _floor_phrase(
        get_canonical_stats().get("facilities_verified", _FALLBACK["facilities_verified"]),
        step=100)


def facilities_phrase_full() -> str:
    """Honest dual claim: '21,000+ tracked · 2,800+ verified'. Prefer this in
    any marketing/SEO copy that previously made the bare '21,000+ facilities'
    claim — it keeps the discovery moat without implying 21k are confirmed."""
    return f"{facilities_phrase()} tracked · {facilities_verified_phrase()} verified"


def countries_phrase() -> str:
    n = get_canonical_stats().get("countries", _FALLBACK["countries"])
    floored = (int(n) // 10) * 10
    return f"{floored}+"


def countries_verified_phrase() -> str:
    n = get_canonical_stats().get("countries_verified", _FALLBACK["countries_verified"])
    floored = (int(n) // 10) * 10
    return f"{floored}+"


def markets_phrase() -> str:
    # Floor DOWN to a clean "300+" so we never over-claim as markets grow
    # (232->300->311 via intl expansion). Matches mcp_facts_export._markets_floor
    # and countries_phrase() — citation-safe rounding, never above reality.
    n = int(get_canonical_stats().get("markets", _FALLBACK["markets"]))
    return f"{(n // 100) * 100}+"


def deals_phrase() -> str:
    """Curated M&A deal-count floor, e.g. '4,000+'. Floors DOWN to the nearest
    1,000 so we never over-claim as the curated set grows (4,009 on 2026-07-10
    -> ~4,255 on 2026-07-16). ★This floors the CURATED count (buyer+seller
    present) — NOT the raw `deals` COUNT(*) (~11.5K broad pile), which the live
    /api/v1/stats `deals` field returns and which would be a ~2.5x over-claim if
    published as 'M&A deals'. Matches markets_phrase()/countries_phrase()
    citation-safe rounding. See _query_live() deals block."""
    n = int(get_canonical_stats().get("deals", _FALLBACK["deals"]))
    return f"{(n // 1000) * 1000:,}+"


def grid_coverage_phrase(style: str = "full") -> str:
    """Canonical, drift-proof description of live grid coverage. Every surface
    (pages, feeds, registries, prompts) should call THIS instead of hardcoding
    '10 North-American grid operators + 3 international modeled' — that copy
    pre-dates the #60 global expansion and undersells it.
      style='full'  → sentence with the regions
      style='short' → compact tag
    """
    if style == "short":
        return "live grid telemetry on 5 continents (US, UK, EU, Taiwan, Japan, South Korea, Brazil, Australia)"
    return ("live grid telemetry across 5 continents — 7 US ISOs (ERCOT, PJM, "
            "CAISO, MISO, SPP, NYISO, ISO-NE) + TVA/BPA + 43 US balancing "
            "authorities, Great Britain (NESO), 24 EU bidding zones (ENTSO-E), "
            "Taiwan (Taipower), Japan (OCCTO areas), South Korea (KPX) and "
            "Brazil (ONS) — all live full-mix; Australia (AEMO) and Singapore "
            "(EMA) live partial feeds; plus EU gas transmission flows (ENTSOG). "
            "(Hydro-Québec, AESO, Nord Pool remain modeled baselines.)")


def headline_blurb() -> str:
    """One-liner generators can drop into a prompt or post, always consistent.
    e.g. '21,000+ data center facilities across 170+ countries, 300+ markets,
    and live grid telemetry on 5 continents (US, UK, EU, Taiwan, Japan, South
    Korea, Brazil, Australia)'."""
    s = get_canonical_stats()
    return (f"{facilities_phrase()} data center facilities across "
            f"{countries_phrase()} countries, {markets_phrase()} markets, and "
            f"live grid telemetry across {s.get('grid_continents', 5)} continents "
            f"(US, UK, EU, Taiwan, Japan, South Korea, Brazil, Australia) + {s.get('utility_bas', 43)} US balancing authorities")
