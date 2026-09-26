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
from util.dcpi_score_row import PUBLISHED_ONLY

# Conservative floors — used as fallback AND as the rounding basis for the
# "*_phrase()" helpers. Never set these above the true live numbers.
_FALLBACK = {
    "facilities": 21000,            # raw "tracked" floor (discovery pile, incl unmerged dupes)
    # DISTINCT canonical_slug among rows that have a keeper — a
    # de-duplication state, not a source verification. Citation-safe
    # cold-start floor: MUST stay <= reality; floors round DOWN.
    # ★2026-09-20 renamed from `facilities_verified`; ★2026-09-25 the
    # deprecated alias was RETIRED (no seed, no write, no read alias) —
    # the public /api/v1/stats and /api/v1/stats/canonical keep the name
    # `facilities_verified` for a DIFFERENT predicate (duplicate_of_id IS
    # NULL), and one name meaning two numbers is the defect retired.
    # History of this seed (under its old name): deduped/active floor — citation-safe. 2026-06-23: re-floored 1800->1000 (live=1,066, so 1800 was a ~69% over-claim on DB-failure — the canonical_floor_above_live_reality finding). Trend kept dropping 3,141->2,848->1,903->1,066 as re-ingestion churns dedup flags. MUST stay <= reality — floors round DOWN; re-floor whenever live drops below it. [flag RESOLVED 2026-07-10 (issue #1539): the 'shrinking' 3,141->1,066->427->5 was the pending QUEUE draining (old filter included merged_at IS NULL); true fleet ~4,903 — dedup was never over-merging.] 2026-06-30: re-floored 1000->400 (live verified ~427 per brain L15; 1000 was again above reality).
    "facilities_with_keeper_distinct": 400,
    # ★2026-09-20: the CITEABLE population — COUNT(DISTINCT canonical_slug)
    # over every row, no de-duplication-state filter. Same citation-safe 400
    # seed and the same rule: floors round DOWN, so this must stay <= reality.
    "facilities_distinct": 400,
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
    # ★2026-09-06 r-news-sources. DISTINCT `source` values in the rolling
    # 90-day announcements corpus — the measured owner of the "40+ sources"
    # claim that had none. Seeded at the PUBLISHED floor (2,000), not at the
    # measured 2,442, because this is the DB-DOWN cold start: a seed above
    # reality is the defect that re-floored facilities_verified three times in
    # June 2026, and a rolling window is the one metric here that can shrink.
    # Re-floor DOWNWARD if the live corpus ever drops below 2,000.
    "news_sources": 2000,
    # ★2026-09-07 — 126427 -> 127289. This seed is the ORIGINAL sin: the
    #  _PUBLIC_FLOOR_SPECS note above records it being "pasted into prose and
    #  then frozen" on /.well-known/mcp.json. It also sat BELOW the pin it is
    #  supposed to floor — ai_surface_canon published "127,000+" against this
    #  126,427, so a cold start claimed 573 more substations than the module
    #  itself believed it had. Caught by
    #  tests/test_public_floor_specs_are_queried.py::test_the_pins_do_not_exceed_their_seeds
    #  on the day that guard was written. Re-measured live: 127,289.
    "substations": 127289,    # COUNT(*) FROM substations, measured 2026-09-07
    # ★2026-09-07 seeds for the two new floor specs. Raw measured ints, same
    # convention as `substations` above; _floor_phrase(step=1000) is what turns
    # them into publishable floors (58,141 -> "58,000+", 94,633 -> "94,000+").
    # ★2026-09-13 fiber_routes re-seeded 66,699 -> 58,141. The table held 9,695
    # HIFLD power transmission lines (route_type 'transmission', source 'hifld',
    # no geometry), written by the fiber lane between 2026-03-30 and 2026-08-14.
    # They are not fiber. 58,141 = COUNT(*) WHERE route_type <> 'transmission',
    # measured 2026-09-13 against a raw COUNT(*) of 67,836.
    # repair_fiber_routes_hifld_transmission.py deletes them, after which the live
    # COUNT(*) below counts the same basis. Until then this seed is below live, so
    # a cold start under-claims rather than publishing power lines as fiber.
    "fiber_routes": 58141,
    # ★2026-09-20 94,633 -> 95,569. COUNT(*) FROM transmission_lines (EIA
    # population). Walked WITH the "95,000+" pin in ai_surface_canon, and it had
    # to move first: the pin floors to 95,000 and this seed is what a cold start
    # believes it has, so leaving it at 94,633 would have published a floor
    # ABOVE the module's own fallback -- the invariant
    # test_every_asset_seed_sits_at_or_above_the_pin_it_seeds now states over
    # every asset key, having previously covered `assets` alone.
    # NOT transmission_lines_geocoded_snapshot (56,108): the stats endpoint
    # EXCLUDES it as a stale geocoded snapshot of the SAME population, and
    # mistaking a superseded layer for the live one is how "52,000 transmission
    # lines" was typed against a live 94,633 (see _PUBLIC_FLOOR_SPECS).
    # ★2026-09-21 95,569 -> 94,635, walked DOWN with the "94,000+" pin: the live
    # COUNT(*) is back beside its 09-19 value. The seed still sits at or above
    # the floor it seeds (94,635 >= 94,000).
    "transmission_lines": 94635,
    # ★2026-09-19 cold-start seed for {canon_assets}. Raw measured int, same
    # convention as the three above; _floor_phrase(step=10000) publishes it
    # ("330,961 -> 330,000+"), the SAME step mcp_facts_export._floor() applies
    # to infrastructure_assets_total, so the two surfaces round identically.
    # Measured 2026-09-19 off live /api/v1/infrastructure/stats
    # infrastructure_assets_total = 330,961 with basis.complete = true and
    # members_unmeasured = []. It must stay ABOVE the "320,000+" pin it seeds
    # or tests/test_public_floor_specs_are_queried.py fails the same way it
    # did for substations on 2026-09-07.
    "assets": 330961,
    "pipeline_gw": 369,       # construction pipeline GW (had no SoT home before)
}

_TTL_S = 600          # 10-minute cache; these move slowly
_cache: dict | None = None
_cache_ts: float = 0.0
_lock = threading.Lock()

# Metrics a real query has populated at least once in this process.
#
# ★ WHY A VALUE ALONE CANNOT SAY THIS. The _FALLBACK seeds above are
# deliberately FAR below reality (facilities_with_keeper_distinct = 400 against a live
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
    # ── Countries facilities_hub already named, plus the rest of the Gulf ──
    # AE, AR, CN, IL, RO, RU and SA are in facilities_hub._COUNTRY_NAMES but
    # were missing here, so the two country maps disagreed: a Capacity Source
    # listing in those countries resolved to no region at all (see
    # routes/exclusive_listings._region_of, which reads THIS map). The
    # remaining Gulf states complete the set that route searches as
    # middle_east_africa. Spellings match facilities_hub exactly so the two
    # maps stay joinable by name.
    #
    # These are FORWARD entries in the sense the region block below
    # documents: routes.dcpi.market_country cannot emit any of these codes
    # today, so none of them can enter the DCPI span until a market under
    # such an operator is actually scored. Adding them changes no published
    # count.
    "AE": "United Arab Emirates", "SA": "Saudi Arabia", "QA": "Qatar",
    "KW": "Kuwait", "BH": "Bahrain", "OM": "Oman", "IL": "Israel",
    "TR": "Turkey", "RU": "Russia", "RO": "Romania",
    "CN": "China", "AR": "Argentina",
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
    "New Zealand": "Asia-Pacific", "China": "Asia-Pacific",
    "Argentina": "Latin America",
    "Romania": "Europe",
    # Turkey and Russia sit across the conventional Europe line. Both are
    # placed by the thing this platform actually models — the grid their
    # markets draw on, and the market reports those markets are tracked in:
    # Istanbul with Europe, Moscow and St Petersburg with Europe.
    "Turkey": "Europe", "Russia": "Europe",
    # The Gulf. _REGION_ORDER has always listed the Middle East so it would
    # publish itself on the first recompute after a Gulf market is scored;
    # these are the countries that make that possible, and they are what
    # routes/exclusive_listings.py searches as middle_east_africa.
    "United Arab Emirates": "the Middle East", "Saudi Arabia": "the Middle East",
    "Qatar": "the Middle East", "Kuwait": "the Middle East",
    "Bahrain": "the Middle East", "Oman": "the Middle East",
    "Israel": "the Middle East",
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


def _measure_asset_total(cur, conn):
    """COUNT(*) every mapped-asset layer and return the sum, or None.

    ★2026-09-19. ONE OWNER FOR THE MAPPED-ASSET TOTAL. `assets` was the only
    public floor with no derivation at all: PINNED['public']['assets'] =
    "320,000+" was hand-walked once on 2026-08-01 and never again, while
    /api/v1/infrastructure/stats summed the same layers live. Measured that
    morning: pin "320,000+" against a live infrastructure_assets_total of
    330,961 — a full 10k bucket stale — and the MCP server instructions blob
    had already moved to "330,000+" off the live number, so the two surfaces
    disagreed in public.

    WHY THIS IMPORTS THE MEMBER TABLE rather than re-listing the layers: a
    second hand-written sum is a second owner, which is the defect being
    fixed, not a fix for it. _STATS_MEMBERS is the one place the asset
    population is defined — which layers count as assets, which table each
    lives in, and which are EXCLUDED as facilities or as a subset of a layer
    already counted — and _measure_member is the one place "0 is unmeasured,
    not a count" is enforced. Reusing both means this total and the endpoint's
    cannot drift apart without someone editing the shared definition.

    ALL-OR-NOTHING, deliberately, and this is where it differs from the
    endpoint. /api/v1/infrastructure/stats MAY publish a partial sum because
    it ships `complete: false` and a `members_unmeasured` list beside it, so a
    reader can see the figure is a floor. A canon PHRASE carries no such block
    — "180,000+ assets" reads as the whole population — so a partial sum here
    would be a silent under-claim of whatever failed to measure. Absent beats
    partial: return None and the pin stands.

    Returns int > 0 when EVERY asset member measured, else None. Never raises:
    an unavailable routes import (a non-web process) is an absent total, not
    an error, for the same fail-soft reason _live_public_floors() returns {}.
    """
    try:
        from routes.infrastructure_data_routes import (
            _STATS_MEMBERS, _measure_member,
        )
    except Exception:
        return None
    total = 0
    seen = 0
    for key, table, role in _STATS_MEMBERS:
        if role != 'asset':
            continue
        seen += 1
        value, _reason = _measure_member(cur, conn, key, table)
        if value is None:
            return None
        total += value
    return total if seen and total > 0 else None


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
        # ★2026-09-23 AUTOCOMMIT: every read below is its own transaction.
        # psycopg2 opens a transaction on the first statement and aborts it on
        # the first failure, after which every statement on the connection
        # raises InFailedSqlTransaction. Each metric here is caught by its own
        # `except Exception: pass`, so one failing read (a statement timeout, a
        # dropped column) used to leave every read AFTER it on the seed or
        # cached value, never marked live: stat_is_live("markets") read False
        # because the keeper-distinct count above it had failed. Autocommit
        # ends that for the reads already here and for any added later, which
        # a rollback in each except would not. The counts need no shared
        # transaction: under READ COMMITTED each statement took its own
        # snapshot anyway. Set here, not in _conn(), so a replaced _conn() is
        # still covered. Tested against a real Postgres in
        # tests/test_canon_live_reads_independent_sql.py.
        c.autocommit = True
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
                # ★2026-09-20 RENAMED. This is DISTINCT canonical_slug among
                # rows that have a keeper — it is not a source "verification"
                # of anything, and publishing it under that word is what made
                # canon serve the keeper count labelled "verified".
                #
                # ★ NOT the bare `facilities_with_keeper`: that name is TAKEN,
                # by routes/facilities_by_dims.py:191, and it means
                # COUNT(*) WHERE COALESCE(is_duplicate,0)=0 — ROWS (live
                # 22,955) against this query's DISTINCT SLUGS (live 22,949).
                # Same filter, different population. Reusing the bare name
                # would move the rows-vs-distinct collision into a new word
                # instead of ending it; the ★★2026-07-27 note above is this
                # module refusing rows once already.
                out["facilities_with_keeper_distinct"] = n
                _live_keys.add("facilities_with_keeper_distinct")
                # ★2026-09-25 the deprecated `facilities_verified` alias is
                # no longer written. Every internal reader was migrated to
                # the name above, and the one PUBLIC reader — the
                # setdefault in routes/facilities_by_dims.stats_canonical —
                # was republishing this keeper count under the public
                # `facilities_verified` name (duplicate_of_id IS NULL), a
                # different population. tests/test_canon_keeper_rename.py
                # fences the alias staying gone.
        except Exception:
            pass
        # ★2026-09-20 THE CITEABLE COUNT — distinct buildings, no de-duplication
        # -state filter. This module did not measure it at all, which is why
        # main.py:23002 records "canonical_stats returns None for
        # facilities_distinct" as its reason for re-deriving the number locally.
        #
        # WHY IT IS THE ONE TO PUBLISH. An is_duplicate-based count SUPPRESSES
        # real facilities: routes/brain_consistency_radar's QA note measured
        # 9,318 of 14,686 distinct facilities with NO is_duplicate=0 row at all,
        # so the facility is invisible to any such count — the suppressed set
        # named Meta Hyperion, Stargate Abilene, CoreWeave Project Horizon and
        # Microsoft Wisconsin. /api/v1/stats/canonical's own `purpose` and
        # /api/v1/stats' _facility_count_notes.primary both say facilities_
        # distinct is THE FIELD TO CITE. Canon published the keeper count
        # instead and under-claimed by ~1,500 buildings.
        try:
            cur.execute("SELECT COUNT(DISTINCT canonical_slug) "
                        "FROM discovered_facilities "
                        "WHERE canonical_slug IS NOT NULL")
            n = int((cur.fetchone() or [0])[0] or 0)
            if n > 0:
                out["facilities_distinct"] = n
                _live_keys.add("facilities_distinct")
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
            # r-published-not-coalesced (2026-09-05): `published = true`, not
            # COALESCE(published, true) = true.
            #
            # The column is NULLABLE and its schema DEFAULT is false. Coalescing
            # a NULL to true therefore inverts the table's own stated default:
            # it asks "is this row live?" and answers yes for a row nobody ever
            # released. Canon is the number every other surface is measured
            # against, so it is the worst place to lean permissive.
            #
            # No-op today — there are zero NULLs, and canon reads the same
            # either way — which is exactly why it is worth fixing now rather
            # than after the first NULL arrives.
            cur.execute(f"SELECT COUNT(DISTINCT market_name) FROM market_power_scores "
                        f"WHERE {PUBLISHED_ONLY} "
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
            cur.execute(f"SELECT iso, state, market_slug, market_name "
                        "FROM market_power_scores "
                        f"WHERE {PUBLISHED_ONLY} "
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
        # ── DISTINCT news sources in the live corpus ──────────────────
        # r-news-sources (2026-09-06). "40+ sources" was the one agent-facing
        # headline with NO canonical owner: no pin, no derivation, no endpoint
        # publishing it, so it could not be verified in EITHER direction. It was
        # typed by hand on ~47 files and served live on /llms.txt, /llms-full.txt,
        # /ai, /connect and two integration manifests, plus a bare unlabelled
        # integer `sources: 40` in /ai/learn's capabilities dict.
        #
        # Measured 2026-09-06 against this exact query: 2,442 distinct sources
        # over 15,050 rows. The claim was not stale-high, it was stale-LOW by
        # ~61x — the inverse of the `deals` defect two fields up, and the reason
        # "check it against the claim before adopting it" is in the runbook.
        #
        # ★ WHY announcements AND NOT news_engine.RSS_FEEDS. There IS a feed
        #   registry (34 entries) and counting it was the obvious move, but it
        #   answers a DIFFERENT question: what we POLL. `source` is the
        #   publisher credited ON THE ITEM, so syndication and the crawler and
        #   discovery writers (news_engine, crawler_scheduler, discovery_nexus,
        #   discovery_engine_v3, main.py) contribute publishers no feed list
        #   holds. It is also the column get_news's `source=` filter matches, so
        #   this is the number that answers the only question an agent actually
        #   asks of it: how many sources can I filter by. A registry count would
        #   publish 34 and be wrong by two orders of magnitude against the tool's
        #   own parameter.
        #
        # ★ ROLLING, NOT CUMULATIVE. news_engine.py:823 prunes announcements at
        #   90 days, so this is "distinct sources seen in the last 90 days" and
        #   it can genuinely FALL — unlike every cumulative metric above it.
        #   That is why the floor spec below uses step=1000 rather than the
        #   deals-style step=100: a 100-step would publish "2,400+" against a
        #   live 2,442 and go false on a 1.7% dip. Measured 30-day windows over
        #   the same corpus: 1,120 / 1,265 / 1,326 — the breadth is stable and
        #   growing, but the headroom has to survive an ingest outage.
        #
        # ★ DISTINCT STRINGS ARE A CEILING ON DISTINCT PUBLISHERS. 493 of the
        #   2,442 are bare domains and 135 collapse into a sibling under light
        #   normalisation ('Data Center Knowledge' / 'datacenterknowledge.com'),
        #   so the deduped publisher count is ~2,292. The floor is checked
        #   against the DEDUPED number, not this one: 2,000 < 2,292 < 2,442.
        #   Counting strings and publishing them as entities is precisely how
        #   `deals` published a 2.9x over-claim for months.
        try:
            cur.execute("SELECT COUNT(DISTINCT source) FROM announcements "
                        "WHERE source IS NOT NULL AND btrim(source) <> ''")
            n = int((cur.fetchone() or [0])[0] or 0)
            if n > 0:
                out["news_sources"] = n
                _live_keys.add("news_sources")
        except Exception:
            pass
        # ── infrastructure-asset counts ────────────────────────────────────
        # ★2026-09-07 — `substations` has had a _PUBLIC_FLOOR_SPECS entry since
        #   2026-09-02 but was NEVER QUERIED HERE, so stat_is_live("substations")
        #   was permanently False, live_public_floors() skipped it, and
        #   {canon_substations} resolved to the PIN every time. The spec has
        #   never once fired. Adding the query is what makes it real; the two new
        #   keys beside it exist so fiber and transmission surfaces stop having to
        #   hardcode, the same reason substations/dcpi_countries/news_sources were
        #   each added in turn.
        #
        # ★ EACH COUNT IS INDEPENDENT. psycopg2 aborts the whole transaction on
        #   a failed statement, so in one transaction a missing table in the
        #   first of these would make the other two fail too and silently
        #   publish the pin for all three — the "cascade into a row of zeros"
        #   that fiber_integration.py documents at length. The connection is in
        #   autocommit (top of this function), so a failure here ends only its
        #   own statement. This loop used to roll back in its except instead,
        #   which covered these three counts and none of the reads above them.
        for _pub_key, _sql in (
            ("substations",        "SELECT COUNT(*) FROM substations"),
            # ★ fiber: COUNT(*) FROM fiber_routes is the DOCUMENTED canonical
            #   count — "the one /api/v1/stats publishes and every agent surface
            #   advertises" (fiber_integration.py). NOT fiber_route_geometry,
            #   which is the wrong-table bug that note exists to record.
            ("fiber_routes",       "SELECT COUNT(*) FROM fiber_routes"),
            # ★ transmission: the `transmission_lines` TABLE, which is the
            #   published inventory /api/v1/stats already serves under this exact
            #   name. Deliberately NOT one of the three ArcGIS
            #   Electric_Power_Transmission_Lines layers (services5 ~89.7k
            #   canonical-for-spatial-queries, services1 52.2k SUPERSEDED,
            #   services2/EIA ~94.6k which feeds this table). Those are spatial
            #   query layers with different roles and id spaces, not competing
            #   totals — see the tx-layer note. The floor rounds DOWN, so a
            #   reader who means the services5 population is not over-claimed by
            #   more than the gap between them; if that ever matters, split the
            #   key rather than re-point this count.
            ("transmission_lines", "SELECT COUNT(*) FROM transmission_lines"),
        ):
            try:
                cur.execute(_sql)
                n = int((cur.fetchone() or [0])[0] or 0)
                if n > 0:
                    out[_pub_key] = n
                    _live_keys.add(_pub_key)
            except Exception:
                # An absent count must stay ABSENT, never 0 — a zero is a
                # measurement.
                pass

        # ── assets: ONE owner for the mapped-asset total ──────────────────
        # See _measure_asset_total() for why this imports the member table
        # instead of re-listing the layers, and why it is all-or-nothing.
        _asset_sum = _measure_asset_total(cur, c)
        if _asset_sum:
            out["assets"] = _asset_sum
            _live_keys.add("assets")
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


def facilities_with_keeper_distinct_phrase() -> str:
    """Floor over DISTINCT canonical_slug among rows that have a keeper,
    e.g. '22,900+'.

    ★2026-09-20 this is the renamed facilities_verified_phrase() (the alias
    was deleted 2026-09-25 once every caller had migrated). Nothing here
    is a source VERIFICATION — a keeper election is a de-duplication state —
    and the old word is why canon published the keeper count as "verified"
    while /api/v1/stats and /api/v1/stats/canonical both use
    `facilities_verified` for a different predicate (duplicate_of_id IS NULL,
    live 22,166). One name, two numbers, on three public surfaces."""
    _v = _read_metric(get_canonical_stats(), "facilities_with_keeper_distinct")
    if _v is None:
        _v = _FALLBACK["facilities_with_keeper_distinct"]
    return _floor_phrase(_v, step=100)


def facilities_distinct_phrase() -> str:
    """The CITEABLE facility floor, e.g. '24,400+' — distinct buildings.

    COUNT(DISTINCT canonical_slug) with no de-duplication-state filter. This is
    what resolve_canon() and _PUBLIC_FLOOR_SPECS both publish as of 2026-09-20;
    the keeper-count helper below remains for callers that genuinely want the
    narrower population."""
    _v = _read_metric(get_canonical_stats(), "facilities_distinct")
    if _v is None:
        _v = _FALLBACK["facilities_distinct"]
    return _floor_phrase(_v, step=100)


def facilities_phrase_full() -> str:
    """Honest dual claim: '21,000+ tracked · 2,800+ verified'. Prefer this in
    any marketing/SEO copy that previously made the bare '21,000+ facilities'
    claim — it keeps the discovery moat without implying 21k are confirmed."""
    return f"{facilities_phrase()} tracked · {facilities_with_keeper_distinct_phrase()} verified"


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


def news_sources_phrase() -> str:
    """DISTINCT news-source floor, e.g. '2,000+'.

    ★2026-09-06 r-news-sources. Floors to the nearest 1,000, NOT the nearest
    100 that deals_phrase() uses, and the difference is not cosmetic: `deals`
    is a cumulative tracked set that only grows, while this counts a corpus
    news_engine.py:823 PRUNES at 90 days. A step of 100 would publish "2,400+"
    against a live 2,442 and go false the first time ingest stalls for a week.

    Floors round DOWN and never above reality — the invariant every entry in
    _FALLBACK's history was re-floored to restore."""
    return _floor_phrase(
        get_canonical_stats().get("news_sources", _FALLBACK["news_sources"]),
        step=1000)


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
    return (f"{facilities_with_keeper_distinct_phrase()} data center facilities across "
            f"{countries_phrase()} countries, {markets_phrase()} markets, and "
            f"live grid telemetry across {s.get('grid_continents', 5)} continents "
            f"(US, UK, EU, Taiwan, Japan, South Korea, Brazil, Australia) + {s.get('utility_bas', 43)} US balancing authorities")


# ── The published-floor derivation ────────────────────────────────────────
# Maps a PINNED['public'] key in ai_surface_canon to the cache metric that
# measures it and the floor that publishes it. The floor callables are the
# SAME ones the *_phrase() helpers above use, so a consumer reading this map
# can never round a number differently from resolve_canon() — which would be a
# new drift class inside the module that exists to kill drift.
# ★2026-09-20 DEPRECATION ALIASES — READS accept either name.
#
# A rename that moves only the WRITE side is not a rename, it is a silent
# miss. _query_live emits both names, but every OTHER producer of a stats
# mapping — a caller building one by hand, a fallback path, a test fixture —
# emits only the name it knew about. The new key is then absent and the read
# falls through to the citation-safe _FALLBACK seed (400 against a live
# ~22,900: a 57x under-claim, published as a floor).
#
# MEASURED on this branch before this map existed: 20 tests across
# test_canon_floor_derivation, test_connect_install_pages_derive_canon,
# test_agent_landing_derives_canon and test_media_standing_totals went red,
# every one of them because its fixture set `facilities_verified` and the
# floor spec had moved to the new name. Those fixtures are standing in for
# real callers that do exactly the same thing.
#
# Reads resolve in order and take the FIRST name present, so the new name wins
# once a producer emits it. Retire an entry only when nothing writes the alias.
#
# ★2026-09-25 RETIRED: facilities_with_keeper_distinct <- facilities_verified.
# _query_live stopped writing the alias and every internal producer/reader was
# migrated. Leaving the read alias would keep resolving a hand-built mapping's
# `facilities_verified` — which, on the public stats endpoints, is a DIFFERENT
# population (duplicate_of_id IS NULL) — as the keeper count. The table stays
# (empty) so _metric_names/_read_metric keep one code path for future renames.
_METRIC_ALIASES: dict = {}


def _metric_names(stat_key: str) -> tuple:
    """(canonical, *deprecated aliases) for one metric."""
    return (stat_key,) + tuple(_METRIC_ALIASES.get(stat_key, ()))


def _read_metric(snap, stat_key):
    """The metric's value under whichever of its names carries it, or None.

    ★ A MEASURED name beats a merely-PRESENT one, and that ordering is the
    whole correctness of this helper. Snapshots in this codebase are built as
    {**_FALLBACK, **measured}, so EVERY canonical name is present as a seed
    whether or not anything measured it. Resolving canonical-name-first would
    therefore return the 400 citation-safe seed and shadow a real ~22,900 that
    arrived under the deprecated alias — a 57x under-claim, published as a
    floor, on a code path whose entire job is to stop under-claims. Measured
    on this branch: that ordering reddened 4 derivation tests before the
    stat_is_live() pass below was added.

    None means ABSENT, never zero: a present 0 is returned as 0, so callers
    keep the `.get(key, default)` semantics this replaced."""
    names = _metric_names(stat_key)
    for name in names:
        if stat_is_live(name) and (snap or {}).get(name) is not None:
            return snap[name]
    for name in names:
        v = (snap or {}).get(name)
        if v is not None:
            return v
    return None


def _metric_is_live(stat_key: str) -> bool:
    """True when a real query measured the metric under ANY of its names."""
    return any(stat_is_live(n) for n in _metric_names(stat_key))


_PUBLIC_FLOOR_SPECS = {
    # ★2026-09-20: reads facilities_with_keeper_distinct, not the (since
    # retired) facilities_verified alias. Same number; this map is what turns it into
    # canon's published `facilities` phrase, so it is the one place the honest
    # name has to win.
    # ★2026-09-20 REBASED onto facilities_distinct. It used to read the keeper
    # count, which is a DE-DUPLICATION state and suppresses ~1,500 real
    # buildings (see _query_live). Both /api/v1/stats and
    # /api/v1/stats/canonical name facilities_distinct as the citeable field;
    # canon now agrees with them. The floor RISES 22,900+ -> 24,400+, which the
    # raise-only overlay accepts.
    "facilities": ("facilities_distinct", lambda n: _floor_phrase(n, step=100)),
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
    # ★2026-09-06 r-news-sources. Added for the third time for the same reason
    # `substations` and `dcpi_countries` were: a surface that needs a number and
    # has no {canon_*} placeholder to reach it HAS to hardcode. "40+ sources"
    # proves it — it reached ~47 files and six live surfaces without ever
    # touching a measurement, because there was nothing to touch.
    #
    # Same callable as news_sources_phrase() above, so the floor here and the
    # phrase there can never round the same number two different ways.
    "news_sources": ("news_sources",      lambda n: _floor_phrase(n, step=1000)),
    # ★2026-09-07 — the FOURTH and FIFTH additions for the reason stated three
    # times above: a surface that needs a number and has no {canon_*} placeholder
    # to reach it HAS to hardcode. These two proved it the same way "40+ sources"
    # did. routes/quick_redirects.py typed "50,000+ fiber routes, 52,000
    # transmission lines" against a live 66,699 / 94,633 — and the 52,000 is the
    # SUPERSEDED services1 layer, i.e. the literal had drifted not just in
    # freshness but in which population it described.
    #
    # step=1000 matches the sibling infrastructure floors. Both counts are now
    # queried in _query_live above, so unlike `substations` before today these
    # specs can actually fire.
    "fiber_routes":       ("fiber_routes",       lambda n: _floor_phrase(n, step=1000)),
    "transmission_lines": ("transmission_lines", lambda n: _floor_phrase(n, step=1000)),
    # step=10000 matches mcp_facts_export._floor(.., 10000) on the same total.
    "assets":             ("assets",             lambda n: _floor_phrase(n, step=10000)),
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
        The static _FALLBACK seeds are citation-safe (facilities_with_keeper_distinct = 400)
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
        # alias-aware: a snapshot written under a metric's DEPRECATED name is
        # still a measurement, and suppressing it would republish the seed.
        if not _metric_is_live(stat_key):
            continue
        try:
            n = int(_read_metric(snap, stat_key) or 0)
            if n > 0:
                out[pub_key] = floor(n)
        except Exception:
            continue
    return out
