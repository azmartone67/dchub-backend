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
    "facilities_verified": 1800,    # deduped/active floor — citation-safe. 2026-06-21: live=1,903 (kept dropping 3,141->2,848->1,903 as re-ingestion churns dedup flags), so the old 2,800 seed had gone STALE-HIGH and a DB-failure fallback would OVERSTATE by ~47% (this is the canonical_floor_above_live_reality finding). MUST stay <= reality — floors round DOWN; re-floor whenever live drops below it. [flag: verified set is shrinking fast — investigate whether dedup is over-merging.]
    "countries": 170,
    "countries_verified": 30,       # deduped/active distinct floor (live ~33; country field dirty -> conservative)
    "markets": 300,          # 2026-06-08: Neon-verified COUNT(DISTINCT market_name) minus 3 aggregates = 300 (grew from 232 via intl expansion). Live query below; this is the fallback.
    "isos": 7,               # 7 live US ISOs (ERCOT, CAISO, NYISO, MISO, PJM, SPP, ISO-NE)
    "grid_operators": 10,    # 10 North-American grid operators w/ live data (7 US ISOs + TVA + BPA + IESO)
    "utility_bas": 43,       # 43 US utility balancing authorities (live EIA-930)
    # #60 (2026-06-02): live grid telemetry is now GLOBAL — 4 continents.
    # Intl live grids beyond N. America: Great Britain (NESO/Elexon), ~12 EU
    # bidding zones (ENTSO-E), Taiwan (Taipower), Australia (AEMO); plus EU gas
    # transmission flows (ENTSOG, 10 countries). All LIVE, not modeled.
    "grid_continents": 4,
    "intl_grid_regions": 15,  # GB(1) + EU(~12) + Taiwan(1) + Australia(1)
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
        # VERIFIED/ACTIVE subset (deduped): excludes duplicate + merged rows.
        # This is the count the map + dedup pipeline already use internally
        # (is_duplicate=0 AND merged_at IS NULL). Lead honest copy with this;
        # "tracked" (raw, above) is the discovery pile incl unmerged candidates.
        try:
            cur.execute("SELECT COUNT(*) FROM discovered_facilities "
                        "WHERE COALESCE(is_duplicate,0)=0 AND merged_at IS NULL")
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
                        "AND COALESCE(is_duplicate,0)=0 AND merged_at IS NULL")
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
    n = get_canonical_stats().get("markets", _FALLBACK["markets"])
    return f"{n}"


def grid_coverage_phrase(style: str = "full") -> str:
    """Canonical, drift-proof description of live grid coverage. Every surface
    (pages, feeds, registries, prompts) should call THIS instead of hardcoding
    '10 North-American grid operators + 3 international modeled' — that copy
    pre-dates the #60 global expansion and undersells it.
      style='full'  → sentence with the regions
      style='short' → compact tag
    """
    if style == "short":
        return "live grid telemetry on 4 continents (US, UK, EU, Taiwan, Australia)"
    return ("live grid telemetry across 4 continents — 7 US ISOs (ERCOT, PJM, "
            "CAISO, MISO, SPP, NYISO, ISO-NE) + TVA/BPA + 43 US balancing "
            "authorities, Great Britain (NESO), ~12 EU bidding zones (ENTSO-E), "
            "Taiwan (Taipower) and Australia (AEMO) — all live; plus EU gas "
            "transmission flows (ENTSOG). (Hydro-Québec, AESO, Nord Pool remain "
            "modeled baselines.)")


def headline_blurb() -> str:
    """One-liner generators can drop into a prompt or post, always consistent.
    e.g. '21,000+ data center facilities across 170+ countries, 232 markets,
    and live grid telemetry on 4 continents (US, UK, EU, Taiwan, Australia)'."""
    s = get_canonical_stats()
    return (f"{facilities_phrase()} data center facilities across "
            f"{countries_phrase()} countries, {markets_phrase()} markets, and "
            f"live grid telemetry across {s.get('grid_continents', 4)} continents "
            f"(US, UK, EU, Taiwan, Australia) + {s.get('utility_bas', 43)} US balancing authorities")
