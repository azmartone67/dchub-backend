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

from util.deals import DEALS_OK

# Conservative floors — used as fallback AND as the rounding basis for the
# "*_phrase()" helpers. Never set these above the true live numbers.
_FALLBACK = {
    "facilities": 21000,            # raw "tracked" floor (discovery pile, incl unmerged dupes)
    "facilities_verified": 400,    # deduped/active floor — citation-safe. 2026-06-23: re-floored 1800->1000 (live=1,066, so 1800 was a ~69% over-claim on DB-failure — the canonical_floor_above_live_reality finding). Trend kept dropping 3,141->2,848->1,903->1,066 as re-ingestion churns dedup flags. MUST stay <= reality — floors round DOWN; re-floor whenever live drops below it. [flag RESOLVED 2026-07-10 (issue #1539): the 'shrinking' 3,141->1,066->427->5 was the pending QUEUE draining (old filter included merged_at IS NULL); true fleet ~4,903 — dedup was never over-merging.] 2026-06-30: re-floored 1000->400 (live verified ~427 per brain L15; 1000 was again above reality).
    "countries": 170,
    "countries_verified": 170,      # ★2026-07-30 re-floored 30 -> 170: live = 178 distinct ISO codes over the deduped fleet (measured; incl. territories — the field is clean codes now, not the dirty mix the old "live ~33" note feared). The stale 30 meant a DB-down cold start published "30+ countries" via countries_verified_phrase — a 5.9x UNDER-claim, and resolve_canon() now serves this phrase on /api/v1/canon/phrases. Floors round DOWN; 170 <= 178. Re-floor downward if the fleet ever shrinks below it.
    "markets": 300,          # 2026-06-08: Neon-verified COUNT(DISTINCT market_name) minus 3 aggregates = 300 (grew from 232 via intl expansion). Live query below; this is the fallback.
    # DISTINCT tracked deals — deduplicated, quarantined rows excluded.
    # ★2026-07-17: the previous "4,000+" was itself an over-claim. It floored a
    # count of ROWS, and the `deals` table carries ~2.9x duplication: the AUTO id
    # embeds the ingest DATE (AUTO-<yyyymmdd>-<contenthash>), so a re-ingest of
    # the same deal never conflicts and accrues one row per day — one Google/
    # Dallas deal held 46 rows, one atNorth deal 945. 4,275 raw rows collapse to
    # ~1,420 distinct real deals. The live query below dedups (AUTO by content
    # hash, everything else by content tuple) and drops data_flag quarantine rows
    # (fabricated example.com seeds + misparsed headline fragments).
    # deals_phrase() floors DOWN to "1,400+" so we never over-claim.
    "deals": 1400,
    # ★2026-09-03 r-dcpi-regions. The COUNTRY span of the DCPI scoring
    # universe — the number the hardcoded region list in main.py's
    # /.well-known/ai-agents.json description was standing in for.
    # Measured live 2026-09-04 against /api/v1/dcpi/scores; the span grows,
    # which is why no figure is repeated here
    # (/api/v1/dcpi/scores, verified at the Railway origin AND the edge).
    # Floors to "30+" via _countries_floor. Floors round DOWN; re-floor
    # downward only if the scored span ever shrinks below 30.
    "dcpi_countries": 30,
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
    # LIVE count — the zones get_grid_scoreboard actually returned (verified
    # 2026-06-25). NOT the configured count: routes/iso_eu_entsoe._ZONE_REGISTRY
    # holds 33 rows as of ws2-entsoe (2026-07-29), and a zone reaches the
    # scoreboard only if its ENTSO-E call answered (BG is chronically absent).
    # RE-MEASURE before raising this — never publish the configured number:
    #   GET /api/v1/iso/eu/snapshot (privileged key) → zone_coverage.returned
    #   or count the EU_* rows in get_grid_scoreboard.
    "eu_zones": 24,
    "substations": 126427,    # HIFLD substations (had no SoT home before)
    "pipeline_gw": 369,       # construction pipeline GW (had no SoT home before)
}

_TTL_S = 600          # 10-minute cache; these move slowly
_cache: dict | None = None
_cache_ts: float = 0.0
_lock = threading.Lock()

# Metrics a real query has populated at least once in this process.
#
# ★ WHY A VALUE ALONE CANNOT SAY THIS. The _FALLBACK seeds above are
# deliberately FAR below reality (facilities_verified = 400 against a live
# ~18,800) because they are CITATION-safe cold-start floors: on a DB outage,
# under-claiming is the safe direction for a cited number. It is the WRONG
# direction for PUBLISHED COPY — the same seed would put "400+ facilities" on
# /llms.txt, /agent and the registry manifests, a ~47x under-claim. So any
# consumer that PUBLISHES a floor has to tell "measured" from "seed", and the
# number by itself does not carry that. This set does.
#
# Never cleared: _query_live() starts from `_cache`, so once a metric has been
# measured the cache keeps a real last-known-good for the life of the process.
_live_keys: set = set()


def stat_is_live(key: str) -> bool:
    """True when `key` in the cache came from a real query, not the static seed.

    Fails CLOSED — an unmeasured or unknown key reads False, because the caller
    is asking whether it may publish the value as measured. Same contract as
    ai_surface_canon.canon_is_live(), which asks the same question of a
    resolve_canon() payload."""
    return key in _live_keys


def peek_canonical_stats():
    """The cached stats WITHOUT triggering a query. None until one has run.

    get_canonical_stats() blocks on a DB round-trip whenever the 10-minute TTL
    has lapsed. Read-time surface rendering (ai_surface_canon.canon_text, called
    on every agent-facing page render) must never pay that — a saturated pool
    would turn one lapsed TTL into a slow page on every surface at once — so
    publishers peek at whatever the cache already holds and fall back to their
    own pinned floor when it is cold."""
    with _lock:
        return dict(_cache) if _cache is not None else None


def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        import psycopg2
        return psycopg2.connect(db, sslmode="require", connect_timeout=6)
    except Exception:
        return None


# ── ISO-3166 alpha-2 → display name, for the derived DCPI phrases ─────────
# r-dcpi-regions (2026-09-03). THIS MODULE DECLARES NO COUNTRY MAP. The
# operator→country resolution lives in ONE place — routes/dcpi.py's
# _market_country, exposed as market_country — and _query_live() binds to it.
# A second operator map here would be the exact defect util/iso_taxonomy.py's
# docstring opens with ("FOUR divergent copies of a state→ISO map"), one column
# over; the first draft of this change wrote one and it had to be deleted.
#
# What IS this module's business is PRESENTATION: an alpha-2 code is what
# schema.org wants and "South Korea" is what a sentence wants. Two different
# questions, so two different maps — and only this one is here.
#
# ★ The US TERRITORIES fold into the United States. _market_country returns
#   PR/GU/VI deliberately ("more precisely themselves than US", and
#   schema.org accepts either) — correct for a Place, wrong for a COUNTRY
#   count, where it would publish Puerto Rico as a nation and put the span
#   3 above reality. Floors round DOWN; this one does too.
_COUNTRY_NAME = {
    "US": "United States", "PR": "United States", "GU": "United States",
    "VI": "United States", "AS": "United States", "MP": "United States",
    "CA": "Canada", "MX": "Mexico",
    "BR": "Brazil", "CO": "Colombia", "CL": "Chile",
    "GB": "United Kingdom", "IE": "Ireland", "DE": "Germany",
    "NL": "Netherlands", "FR": "France", "ES": "Spain", "IT": "Italy",
    "PL": "Poland", "AT": "Austria", "BE": "Belgium", "PT": "Portugal",
    "CH": "Switzerland", "GR": "Greece", "CZ": "Czechia", "SE": "Sweden",
    "DK": "Denmark", "FI": "Finland", "NO": "Norway",
    "JP": "Japan", "KR": "South Korea", "TW": "Taiwan", "HK": "Hong Kong",
    "SG": "Singapore", "IN": "India", "MY": "Malaysia", "ID": "Indonesia",
    "TH": "Thailand", "PH": "Philippines", "VN": "Vietnam",
    "AU": "Australia", "NZ": "New Zealand",
    "ZA": "South Africa",
}


# ── Country → continental region, for the derived DCPI region phrase ──────
# r-dcpi-regions (2026-09-03). The phrase names REGIONS, not countries, on
# purpose: an enumeration of dozens of countries does not belong in a
# manifest description, and a
# top-N-by-market-count list would churn every recompute. Regions are the
# coarsest true statement, so the sentence stays short AND stops going stale.
# The exact country list is published as STRUCTURE instead —
# live_dcpi_international_markets() below feeds the ai-agents.json
# dcpi_coverage.international_markets block, which is where an agent that
# actually wants the enumeration should read it.
#
# Every country in util.iso_taxonomy.ISO_COUNTRY must appear here;
# tests/test_dcpi_region_derivation.py asserts it, so adding an operator
# without a region cannot silently drop its region from the phrase.
_COUNTRY_REGION = {
    "United States": "North America", "Canada": "North America",
    "Mexico": "North America",
    # ★ Colombia and Chile were added here BEFORE their markets existed, as
    #   forward entries for the LatAm branch; that branch has since merged and
    #   bogota/santiago are live behind the XM/CEN operators. A country with no
    #   scored market contributes no region — the span is counted from live
    #   ROWS, never from these maps — so a forward entry is free, and it stops
    #   a market landing with no region. Keep doing it that way.
    "Brazil": "Latin America", "Colombia": "Latin America",
    "Chile": "Latin America",
    "United Kingdom": "Europe", "Ireland": "Europe", "Germany": "Europe",
    "Netherlands": "Europe", "France": "Europe", "Spain": "Europe",
    "Italy": "Europe", "Poland": "Europe", "Austria": "Europe",
    "Belgium": "Europe", "Portugal": "Europe", "Switzerland": "Europe",
    "Greece": "Europe", "Czechia": "Europe", "Sweden": "Europe",
    "Denmark": "Europe", "Finland": "Europe", "Norway": "Europe",
    "South Africa": "Africa",
    "Japan": "Asia-Pacific", "South Korea": "Asia-Pacific",
    "Taiwan": "Asia-Pacific", "Hong Kong": "Asia-Pacific",
    "Singapore": "Asia-Pacific", "India": "Asia-Pacific",
    "Malaysia": "Asia-Pacific", "Indonesia": "Asia-Pacific",
    "Thailand": "Asia-Pacific", "Philippines": "Asia-Pacific",
    "Vietnam": "Asia-Pacific", "Australia": "Asia-Pacific",
    "New Zealand": "Asia-Pacific",
}

#: Reading order for the phrase. A region absent from the live set is simply
#: not named — the Middle East is listed here so it publishes ITSELF on the
#: recompute after the first Gulf market is scored, with no edit.
_REGION_ORDER = ("North America", "Latin America", "Europe",
                 "the Middle East", "Africa", "Asia-Pacific")


def _regions_for(countries) -> tuple:
    """Continental regions actually represented in `countries`, in reading
    order. Unknown countries contribute no region rather than a wrong one."""
    seen = {_COUNTRY_REGION.get(c) for c in countries}
    return tuple(r for r in _REGION_ORDER if r in seen)


def _join_series(items) -> str:
    """'A, B and C' — the Oxford-free serial join the surfaces already use."""
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


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
                # ★2026-09-01: this was the ONLY metric here that set `out` but
                # never marked itself live, so stat_is_live("facilities") read
                # False even right after a successful COUNT. Harmless while
                # nothing gated on it — but the moment a publisher asks "may I
                # serve this as measured?" (routes/provenance.
                # facility_verification_counts now does), an unmarked key means
                # a correct, measured number is suppressed FOREVER. The four
                # metrics below have always marked themselves; this one was
                # simply missed.
                _live_keys.add("facilities")
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
        # ★★2026-07-27: count DISTINCT canonical_slug, not ROWS. The keeper-
        # election repair (repair_dedup_keeper_election.py) elected a survivor
        # for the 9,318 facilities that had none, taking keeper ROWS from 5,737
        # to 15,055 — but there are only 14,686 distinct facilities, because 41
        # groups carry more than one keeper. Counting rows made this phrase
        # read "15,000+ verified" against a reality of 14,686: an over-claim,
        # and exactly the canonical_floor_above_live_reality failure this module
        # exists to prevent. Distinct-slug is the facility count; rows are not.
        try:
            cur.execute("SELECT COUNT(DISTINCT canonical_slug) "
                        "FROM discovered_facilities "
                        "WHERE COALESCE(is_duplicate,0)=0 "
                        "  AND canonical_slug IS NOT NULL")
            n = int((cur.fetchone() or [0])[0] or 0)
            if n > 0:
                out["facilities_verified"] = n
                _live_keys.add("facilities_verified")
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
                _live_keys.add("countries_verified")
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
                _live_keys.add("markets")
        except Exception:
            pass
        # ── The COUNTRY span of that same scoring universe ────────────
        # r-dcpi-regions (2026-09-03). main.py's /.well-known/ai-agents.json
        # described DCPI as covering "300+ markets across the U.S., UK, EU,
        # Japan, Australia, Singapore, and Canada" — a HAND-TYPED region list
        # beside a canon-derived count, in the same sentence. The count floored
        # safely; the list did not, and by 2026-09-03 it omitted Mexico, India,
        # Brazil, South Africa, Malaysia, Indonesia, Taiwan, South Korea, Hong
        # Kong, Thailand, Vietnam, the Philippines, New Zealand, thirteen more
        # European countries and the US territories — many times the seven it
        # named. Same defect class as the DCPI-scored-markets literal in
        # the very next field of the same payload: one document, two answers,
        # and the hardcoded one under-claims.
        #
        # ★ SAME universe predicate as the markets query above, deliberately:
        #   two counts of the same set must not be able to disagree about what
        #   the set IS.
        # ★ Country resolves from the OPERATOR label, never from `state` —
        #   see util.iso_taxonomy.country_of_market for the live two-letter
        #   collisions (IN=India/Indiana, DE=Germany/Delaware, ID=Indonesia/
        #   Idaho, WA=Western Australia/Washington).
        # ★ An UNRESOLVED label is EXCLUDED and RECORDED, never absorbed into
        #   the US bucket. That default is what let main.py's grid-telemetry
        #   map class Brazil and Korea as American until a test caught it;
        #   excluding under-claims by one country and says so, which a guard
        #   can see. dcpi_unmapped is the tell.
        try:
            from routes.dcpi import market_country as _cm
            cur.execute("SELECT iso, state, market_slug, market_name "
                        "FROM market_power_scores "
                        "WHERE COALESCE(published, true) = true "
                        "AND market_slug NOT IN "
                        "('pacific-nw-rural','rural-spp','upper-michigan')")
            rows = cur.fetchall() or []
            by_country = {}
            unmapped = []
            for _iso, _state, _slug, _name in rows:
                # NB argument order is (state, iso, slug) — routes/dcpi.py's
                # signature, not this module's read order.
                ctry = _COUNTRY_NAME.get(_cm(_state, _iso, _slug) or "")
                if not ctry:
                    unmapped.append((_iso or "", _slug or ""))
                    continue
                ent = by_country.setdefault(ctry, {"isos": set(), "markets": set()})
                if (_iso or "").strip() and (_iso or "").upper().strip() != "UNK":
                    ent["isos"].add(_iso.strip())
                ent["markets"].add(_name or _slug or "")
            if by_country:
                out["dcpi_countries"] = len(by_country)
                out["dcpi_regions"] = _regions_for(by_country)
                out["dcpi_intl"] = tuple(
                    (c, "/".join(sorted(v["isos"])),
                     tuple(sorted(m for m in v["markets"] if m)))
                    for c, v in sorted(by_country.items())
                    if c != "United States")
                out["dcpi_unmapped"] = tuple(sorted(set(unmapped)))
                _live_keys.add("dcpi_countries")
        except Exception:
            pass
        # DISTINCT tracked deals. ★DO NOT use a bare COUNT(*) FROM deals here:
        # rows are NOT deals. The AUTO id embeds the ingest date, so the same
        # deal re-ingests under a new id every day and ON CONFLICT never fires —
        # the raw count over-states reality ~2.9x (4,275 rows -> ~1,420 deals).
        # Dedup AUTO rows by their content hash (the id suffix, which is stable
        # across ingest days) and everything else by content tuple, and drop
        # data_flag quarantine rows. deals_phrase() floors this DOWN.
        # NOTE: LEFT() not LIKE 'AUTO-x' on purpose — a literal percent-sign in a
        # psycopg2 query string is a live 500 hazard.
        # ★The previous filter here demanded buyer AND seller, which returns 633
        # live; floored to the nearest 1,000 that produced the string "0+" —
        # ai_surface_canon.resolve_canon() publishes deals_phrase() straight to
        # the public surfaces, so the canon was emitting "0+ tracked deals".
        try:
            cur.execute(
                "SELECT COUNT(*) FROM ("
                "  SELECT DISTINCT CASE"
                "    WHEN LEFT(id, 5) = 'AUTO-' THEN RIGHT(id, 6)"
                "    ELSE COALESCE(buyer,'')||'|'||COALESCE(seller,'')||'|'||"
                "         COALESCE(value::text,'')||'|'||COALESCE(mw::text,'')||'|'||"
                "         COALESCE(date,'')"
                "  END AS k"
                "  FROM deals"
                "  WHERE " + DEALS_OK +
                ") t")
            n = int((cur.fetchone() or [0])[0] or 0)
            if n > 0:
                out["deals"] = n
                _live_keys.add("deals")
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
        # ★ Keep the last-known-good. _query_live() is documented never to raise
        # and itself starts from `_cache` for exactly this reason (r-floor-
        # freshness, 2026-06-21) — but this path did the opposite, reverting a
        # measured cache to the static seed. That is invisible while the seed is
        # only a fallback; it is a ~47x under-claim once stat_is_live() lets a
        # publisher serve the cache, because _live_keys would still read True
        # over a dict that had been reset to _FALLBACK.
        live = dict(_cache) if _cache is not None else dict(_FALLBACK)
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


def _countries_floor(n) -> str:
    """'170+' — floors to 10, no thousands separator (countries never reach 4
    digits). Pure: takes the count, so the *_phrase() helpers below and
    live_public_floors() cannot round the same number two different ways."""
    return f"{(int(n) // 10) * 10}+"


def countries_phrase() -> str:
    return _countries_floor(get_canonical_stats().get("countries", _FALLBACK["countries"]))


def countries_verified_phrase() -> str:
    return _countries_floor(
        get_canonical_stats().get("countries_verified", _FALLBACK["countries_verified"]))


def _markets_floor(n) -> str:
    """'300+' — floors to 100, no thousands separator."""
    return f"{(int(n) // 100) * 100}+"


def _deals_floor(n) -> str:
    """'1,900+' — floors to 100 WITH a thousands separator."""
    return f"{(int(n) // 100) * 100:,}+"


def markets_phrase() -> str:
    # Floor DOWN to a clean "300+" so we never over-claim as markets grow
    # (232->300->306 via intl expansion). Matches mcp_facts_export (which floors
    # dcpi_markets_scored the same way — the exact "311" it used to publish
    # counted score ROWS, not scored markets) and countries_phrase() —
    # citation-safe rounding, never above reality.
    return _markets_floor(get_canonical_stats().get("markets", _FALLBACK["markets"]))


def deals_phrase() -> str:
    """DISTINCT tracked-deal floor, e.g. '1,400+'.

    ★2026-07-17 — this floors DEDUPLICATED deals, not rows. `deals` rows
    over-state reality ~2.9x (the AUTO id embeds the ingest date, so one deal
    accrues a row per day); _query_live() dedups and excludes quarantined rows.

    ★Floors to the nearest 100, NOT 1,000. At 1,000-granularity the live count
    (~1,420) would publish as "1,000+" — a 30 percent under-claim — and the
    previous buyer+seller filter (633 live) floored all the way to the string
    "0+", which resolve_canon() was feeding to the public surfaces. Matches
    markets_phrase() rounding: citation-safe, never above reality."""
    return _deals_floor(get_canonical_stats().get("deals", _FALLBACK["deals"]))


def dcpi_countries_phrase() -> str:
    """Country span of the DCPI scoring universe, e.g. '30+'.

    Floors to 10 like countries_phrase() — citation-safe, never above reality.
    Measured against the live scored universe; no figure is repeated here
    because this docstring would rot exactly as the literal it replaced did."""
    return _countries_floor(
        get_canonical_stats().get("dcpi_countries", _FALLBACK["dcpi_countries"]))


def dcpi_regions_phrase() -> str:
    """Derived region span, e.g. 'North America, Latin America, Europe, Africa
    and Asia-Pacific'.

    ★THE POINT. This replaces the hand-typed "the U.S., UK, EU, Japan,
    Australia, Singapore, and Canada" that main.py's /.well-known/ai-agents.json
    description carried beside a canon-derived market count. That list named 7
    regions against a live country span many times larger, and had been stale
    since at least the
    2026-08-07 Mexico addition — the same shape as the "233 DCPI-scored markets"
    literal in the very next field of the same payload.

    Fail-open to "" (a region-free sentence) exactly like canon_text(): a
    missing clause is visible, a stale one is not.
    """
    regions = get_canonical_stats().get("dcpi_regions") or ()
    return _join_series(list(regions))


def live_dcpi_regions_phrase() -> str:
    """dcpi_regions_phrase() under the live_public_floors() PEEK-ONLY contract.

    Never triggers a query, so a manifest render can never block on the DB;
    returns "" when no real query has measured the span, and the caller's
    pinned literal stands. Same contract, same reason — see live_public_floors.
    """
    snap = peek_canonical_stats()
    if snap is None or not stat_is_live("dcpi_countries"):
        return ""
    return _join_series(list(snap.get("dcpi_regions") or ()))


def live_dcpi_international_markets() -> list:
    """The non-US half of the scoring universe as STRUCTURE, peek-only.

    Shape is preserved from the hand-written list this replaces in
    /.well-known/ai-agents.json — [{"country","iso","markets":[...]}] — so an
    agent already parsing that block does not break. What changes is that the
    rows are now measured: the hand-written version froze at the 2026-05-25
    launch set (10 countries, 16 markets — the PIN itself, exact by
    construction) and never grew, while the live universe grew past it.

    Returns [] when unmeasured, so the caller's pinned list stands.
    """
    snap = peek_canonical_stats()
    if snap is None or not stat_is_live("dcpi_countries"):
        return []
    return [{"country": c, "iso": iso, "markets": list(names)}
            for (c, iso, names) in (snap.get("dcpi_intl") or ())]


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
    # ★2026-08-17: this composed facilities_phrase() (= COUNT(*) rows) directly
    # with the words "data center facilities", so every consumer of the blurb
    # published the raw discovery pile as a building count. Leads with distinct
    # buildings now, matching /api/v1/canon/phrases and ai_surface_canon.
    # Use facilities_phrase_full() when you want the tracked pile as well.
    return (f"{facilities_verified_phrase()} data center facilities across "
            f"{countries_phrase()} countries, {markets_phrase()} markets, and "
            f"live grid telemetry across {s.get('grid_continents', 5)} continents "
            f"(US, UK, EU, Taiwan, Japan, South Korea, Brazil, Australia) + {s.get('utility_bas', 43)} US balancing authorities")


# ── The published-floor derivation ────────────────────────────────────────
# Maps a PINNED['public'] key in ai_surface_canon to the cache metric that
# measures it and the floor that publishes it. The floor callables are the
# SAME ones the *_phrase() helpers above use, so a consumer reading this map
# can never round a number differently from resolve_canon() — which would be a
# new drift class inside the module that exists to kill drift.
_PUBLIC_FLOOR_SPECS = {
    "facilities": ("facilities_verified", lambda n: _floor_phrase(n, step=100)),
    "countries":  ("countries_verified",  _countries_floor),
    "markets":    ("markets",             _markets_floor),
    "deals":      ("deals",               _deals_floor),
    # ★2026-09-02: substations added because /.well-known/mcp.json — the single
    # most agent-quotable string on the domain, rendered verbatim by registries
    # and MCP clients — carried the LITERAL "126,427 substations" in its
    # top-level .description. That number is this file's own _FALLBACK seed
    # (line ~71, "HIFLD substations (had no SoT home before)"), pasted into
    # prose and then frozen: the snapshot measured 127,269 while the manifest
    # published 126,427, i.e. the surface was serving the DB-DOWN fallback as
    # though it were the measurement, permanently.
    #
    # It is the shape this whole module exists to end — a hand-typed literal
    # beside a live one — and the reason it survived is that substations had a
    # snapshot key but no FLOOR SPEC, so no {canon_*} placeholder could reach it
    # and every surface had to hardcode. step=1000 matches the other
    # infrastructure-scale floors; an exact count in prose invites a diff every
    # ingest, which is how 126,427 became something nobody dared touch.
    "substations": ("substations",        lambda n: _floor_phrase(n, step=1000)),
    # ★2026-09-03: the DCPI country span. Added for the same reason
    # `substations` was — a surface that needs a number and has no
    # {canon_*} placeholder to reach it HAS to hardcode, and main.py's
    # ai-agents.json description proved it by hardcoding a region list
    # instead. _countries_floor matches countries_phrase(), so the two
    # country spans on the same page can never round differently.
    "dcpi_countries": ("dcpi_countries",  _countries_floor),
}


def live_public_floors() -> dict:
    """Published floor phrases for the public metrics a real query has measured.

    THE POINT: ai_surface_canon.PINNED['public'] is a hand-typed fallback that
    surfaces which never call resolve_canon() (/llms.txt, /agent, /connect,
    /.well-known/mcp.json, agent_concierge) serve DIRECTLY. It has been walked
    by hand six consecutive times — 15,700 -> 17,000 -> 18,000 -> 18,300 ->
    18,400 -> 18,500 — always trailing the resolver, because resolve_canon()
    self-heals and a literal cannot. This function is the derivation that ends
    that: the pin becomes a cold-start floor, and the resolver's last-known-good
    is what actually publishes.

    Contract:
      * PEEK ONLY — never triggers a query, so no surface render can block on
        the DB. A cold cache returns {} and the caller's pin stands.
      * A key is present ONLY when stat_is_live() says a real query measured it.
        The static _FALLBACK seeds are citation-safe (facilities_verified = 400)
        and would be a ~47x under-claim if published, so "unmeasured" must read
        as absent rather than as a small number.
      * Heals in BOTH directions. A metric that genuinely shrinks republishes
        lower on the next TTL, which a max()-against-the-pin would not do — and
        floors that drift ABOVE reality are the exact defect that re-floored
        facilities_verified three times in June 2026.
    """
    snap = peek_canonical_stats()
    if snap is None:
        return {}
    out = {}
    for pub_key, (stat_key, floor) in _PUBLIC_FLOOR_SPECS.items():
        if not stat_is_live(stat_key):
            continue
        try:
            n = int(snap.get(stat_key) or 0)
            if n > 0:
                out[pub_key] = floor(n)
        except Exception:
            continue
    return out
