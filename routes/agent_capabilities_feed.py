"""
agent_capabilities_feed.py — comprehensive live capabilities for AI agents.

Phase ZZZZZ-round47.25 (2026-05-26). Static manifests (/agent.json,
/.well-known/mcp.json) are fine for first discovery but don't help an
AI agent answer "what's new with DC Hub today?" or "how big is their
catalog?". This endpoint is the agent-first answer: pure JSON, live
numbers, refreshed on every request, schema.org Service block
embedded, agent-friendly fields.

Designed for AI clients that:
  - Cache DC Hub as a tool source and want delta-detection
  - Need cite-clean facts ("21,415 facilities as of 2026-05-26")
  - Want a single fetch that covers tools + stats + recent updates
  - Need a public license (CC-BY-4.0) so they can quote freely

Endpoint:
  GET /api/v1/agents/capabilities.json     full feed
  GET /api/v1/agents/capabilities          alias (browser-readable)
"""
import os
from routes.url_registry import build_public_url
# ★2026-08-20: `from util.deals import DEALS_OK` removed with the raw
#  `COUNT(*) FROM deals WHERE {DEALS_OK}` it served. deals_tracked now derives
#  from canonical_stats["deals"], which dedups (the AUTO id embeds the ingest
#  date, so one deal accrues a row per day) and drops data_flag quarantine rows.
#  DEALS_OK counted rows: 2,097 live against 1,892 deduped deals.


# ── Canon accessors (2026-08-19) ─────────────────────────────────────────────
# This feed is CC-BY-4.0 and explicitly built to be QUOTED by agents, so every
# number on it is a citable claim. Two were hand-typed and stale: "version"
# ("2.1.10" vs a live 2.12.0) and "tool_count" (29, being len() of a hand-typed
# excerpt, vs a live catalog of 82). Both derive now.
#
# Imported lazily inside the helpers rather than at module scope: this module is
# imported during blueprint registration, and a hard dependency on
# ai_surface_canon at import time would couple route registration to canon
# resolution. Both fail OPEN to None so a canon hiccup degrades one field
# instead of 500-ing the feed.
def _canon_tool_count():
    """Live advertised tool count, or None."""
    try:
        from ai_surface_canon import PINNED
        n = PINNED.get("tools_advertised") or len(PINNED.get("tool_manifest") or ())
        return int(n) if n else None
    except Exception:  # noqa: BLE001
        return None


def _canon_version():
    """The SERVED server version, falling back down to the pin — never a literal.

    ★2026-09-02: the first rung was canon_text("{canon_version}"), which
    substitutes out of ai_surface_canon.PINNED — the COLD-START FLOOR, not the
    truth. So this feed published 2.12.1 while the live `initialize` serverInfo
    handshake, the ONLY source of truth, answered 2.12.3.
    resolve_server_version_cached() is the accessor built for exactly this:
    it answers from memory and refreshes in a background thread, so it never
    blocks this request path and never raises, and it reaches the live handshake
    through _mcp_server_version() — never through /mcp/health or
    /.well-known/mcp.json, which are CF-synthesized and echo the canon back (the
    closed loop that let the pin sit six minor versions behind in August). It is
    monotonic, so it can only move TOWARD the live server. On a cold cache it
    returns PINNED["version"] — exactly what canon_text("{canon_version}")
    returned here before — so the cold-start answer does not change.

    ★ AND THE DEEPEST RUNG IS NOW THE PIN, NOT A ROTTING LITERAL. The old
    fallback was the hand-typed "2.1.10", ELEVEN minors behind the pin it was
    supposedly backing up: a canon hiccup made this CC-BY card publish a citable
    version claim WORSE than the one it was protecting — the cure was worse than
    the disease. The pin is chased upward in ai_surface_canon.py and leaves a
    diff; a literal here rots unwatched, and this endpoint is NOT in
    ai_surface_sentinel._SURFACES, so nothing would ever have flagged it. Only
    an un-importable ai_surface_canon now reaches None, which is the fail-open
    contract the module comment above states for these helpers.
    """
    try:
        from ai_surface_canon import resolve_server_version_cached
        v = (resolve_server_version_cached() or "").strip()
        if v:
            return v
    except Exception:  # noqa: BLE001
        pass
    # Next rung down: the exact expression this field carried before, so a
    # resolver outage degrades to the previous behaviour rather than to a new one.
    try:
        from ai_surface_canon import canon_text
        v = canon_text("{canon_version}").strip()
        if v:
            return v
    except Exception:  # noqa: BLE001
        pass
    # Deepest rung: the PIN itself, read directly, in case canon_text() is what
    # broke. This is the floor the old literal was pretending to be.
    try:
        from ai_surface_canon import PINNED
        return (PINNED.get("version") or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


# ★2026-08-20 — THE HEADLINE COUNTS DERIVE TOO.
#
# The 2026-08-19 pass above fixed `version` and `tool_count` and stopped there.
# The four numbers a reader actually quotes did NOT derive: three ran their own
# raw `COUNT(*)` and one was hand-typed. Measured live on 2026-08-20 against
# /api/v1/stats/canonical and /api/v1/canon/phrases:
#
#   field           this feed served   canon serves        basis defect
#   facilities      26,334             18,500+ (18,603)    COUNT(*) ROWS
#   markets_scored  330                300+                score ROWS, not markets
#   deals_tracked   2,097              1,800+ (1,892)      undeduped rows
#   countries       170 (hand-typed)   170+                literal
#
# ★facilities is the one that matters: this feed is CC-BY-4.0 and its
#  `agent_quotable` string is BUILT to be pasted by agents. It was publishing
#  "DC Hub tracks 26,334 data-center facilities" — the raw discovery pile
#  INCLUDING flagged duplicates, ~1.4x the 18,603 distinct buildings. That is
#  the identical over-claim the 2026-07-24 DEDUP REBASE (ai_surface_canon.py)
#  retired everywhere else, still shipping here in the most quotable string on
#  the site. canonical_stats.py already says which side is right, in its own
#  words: "Lead honest copy with this; 'tracked' (raw, above) is the discovery
#  pile including flagged duplicates."
#
# ★Bound to canonical_stats.get_canonical_stats() — the SAME dict
#  ai_surface_canon.resolve_canon() reads for /api/v1/canon/phrases — rather
#  than to a re-implemented query. Re-implementing is how this drifted: the raw
#  COUNT(*) here WAS somebody's idea of "the facility count". Deriving from the
#  accessor makes the feed agree with canon BY CONSTRUCTION, including when
#  canon changes basis again.
#
# ★Do NOT "improve" the degraded path by inventing a local fallback. If the DB
#  is down, get_canonical_stats() returns its own conservative floors and this
#  feed under-claims — in lockstep with /api/v1/canon/phrases, which degrades
#  the same way from the same dict. Two surfaces quietly disagreeing is the
#  failure being fixed here; both under-claiming together is not.
def _canon_stats():
    """canonical_stats.get_canonical_stats(), or {} — never raises.

    Lazy-imported for the reason given above: blueprint registration must not
    depend on canon resolution.
    """
    try:
        from canonical_stats import get_canonical_stats
        return get_canonical_stats() or {}
    except Exception:  # noqa: BLE001
        return {}


def _canon_facilities_floor():
    """The facility floor canon already publishes, as an int, or None.

    Reads the same "{canon_facilities}" placeholder every agent surface renders
    (e.g. the live floor phrase "N,NNN+ facilities" -> NNNN). Used only as a SANITY BOUND: this feed must
    never publish an exact count below the floor the rest of the site is already
    quoting. Fails OPEN to None — an unreadable floor must degrade to "no check",
    never to "delete the live count".
    """
    try:
        from ai_surface_canon import canon_text
        m = re.search(r"(\d[\d,]*)\+", canon_text("{canon_facilities}"))
        return int(m.group(1).replace(",", "")) if m else None
    except Exception:  # noqa: BLE001
        return None
import datetime
import json
import re
import threading
import time
from contextlib import contextmanager
from flask import Blueprint, jsonify, request

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

agent_capabilities_bp = Blueprint("agent_capabilities_feed", __name__)

# r47.31 (2026-05-26): process-local memo cache. The endpoint advertises
# cache_ttl_seconds=86400, so the server should hold the same data — there's
# no value in re-running 5+ DB queries per request when the answer changes
# at midnight UTC. Without this, a cold/busy Railway burst causes the Pages
# worker's subrequest to time out at ~5s, dropping us to 503 fallback.
#
# Keyed by data_version (YYYYMMDD int). One stale-while-revalidate slot
# per worker process. Lock-guarded so concurrent requests don't pile up
# on the same recompute.
_CAPS_CACHE: dict = {"data_version": None, "payload": None, "computed_at": 0.0}
_CAPS_LOCK = threading.Lock()


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    c.autocommit = True
    try: yield c
    finally: c.close()


def _gather():
    # r47.27 (2026-05-26): daily freshness baking — let agents that cache
    # us know when our data has materially changed. data_version is a
    # YYYYMMDD integer that flips at midnight UTC. staleness_hint tells
    # polite agents how often to re-fetch. last_significant_update
    # reflects when the underlying data last had a press-worthy change.
    today = datetime.date.today()
    data_version = int(today.strftime("%Y%m%d"))
    out = {
        "name":             "DC Hub",
        "namespace":        "cloud.dchub/mcp-server",
        # ★2026-08-19: was the literal "2.1.10" while the gateway served 2.12.0.
        # A version string on a CC-BY card is a citable claim like any other.
        # ★2026-09-02: ...and then it derived from the PIN, which is a cold-start
        # floor and not the truth — 2.12.1 served against a live 2.12.3. It now
        # reads the resolver. See _canon_version() above.
        "version":          _canon_version(),
        "description":      "Live data layer for data-center infrastructure. AI-agent native MCP server.",
        "license":          "CC-BY-4.0",
        "homepage":         "https://dchub.cloud",
        "mcp_endpoint":     "https://dchub.cloud/mcp",
        "transport":        "streamable-http",
        "protocol_version": "2024-11-05",
        "computed_at":      datetime.datetime.utcnow().isoformat() + "Z",
        "data_version":     data_version,
        "data_date":        today.isoformat(),
        "cache_ttl_seconds": 86400,  # 24h — bake-once-per-day
        "staleness_hint":   ("Cache this for up to 24 hours. data_version flips at "
                             "midnight UTC — re-fetch when your cached value is from "
                             "an older data_version."),
        "next_refresh_hint": "Re-fetch when data_version increments (daily at 00:00 UTC).",
    }

    # Live counters
    #
    # Grid-count canonical framing (2026-05-31): keep these mutually
    # consistent and literally true.
    #   • us_isos = the 7 live US ISOs ONLY (each has a working extractor in
    #     routes/iso_orchestrator.py). NEVER list SOCO/FRCC here — they have
    #     no extractor and are served (if at all) as utility BAs.
    #   • na_grid_operators = the live grid operators on 5 continents with live
    #     data = those 7 US ISOs + TVA + BPA + IESO (Ontario).
    #   • utility_bas_count = 43 US utility balancing authorities (live EIA-930).
    #   • international_isos_modeled = modeled baselines (Hydro-Québec, AESO, Nord Pool) that are a MODELED
    #     baseline, NOT live telemetry (Hydro-Québec, AESO, Nord Pool).
    #
    # ★The four headline counts derive from canonical_stats — see _canon_stats()
    #  above for what each one used to be and why. The keys are chosen to match
    #  what resolve_canon() reads, NOT what reads best:
    #    facilities     <- facilities_verified   (NOT `facilities`, the raw pile)
    #    markets_scored <- markets               (DISTINCT market_name - aggregates)
    #    deals_tracked  <- deals                 (deduped, quarantine excluded)
    #    countries      <- countries_verified    (deduped fleet, clean ISO codes)
    _cs = _canon_stats()
    counts = {
        "facilities":       int(_cs.get("facilities_verified") or 0),
        "markets_scored":   int(_cs.get("markets") or 0),
        "deals_tracked":    int(_cs.get("deals") or 0),
        "countries":        int(_cs.get("countries_verified") or 0),
        "us_isos":          ["PJM","CAISO","ERCOT","MISO","SPP","NYISO","ISO-NE"],
        "na_grid_operators": ["PJM","CAISO","ERCOT","MISO","SPP","NYISO","ISO-NE","TVA","BPA","IESO"],
        "utility_bas_count": 43,
        "international_isos_modeled": ["Hydro-Québec", "AESO", "Nord Pool"],
        "ai_platforms_citing": 96,
    }
    # get_canonical_stats() already falls back to its own maintained floors, so a
    # zero here means the key is genuinely absent from that dict — a canon-side
    # rename, not a DB outage. Drop the field rather than publish 0: "DC Hub
    # tracks 0 data-center facilities" is a worse citable claim than a missing
    # key, and a missing key is what the drift fence below can actually see.
    for _k in ("facilities", "markets_scored", "deals_tracked", "countries"):
        if not counts[_k]:
            counts.pop(_k)

    # ★NEVER PUBLISH BELOW OUR OWN PUBLISHED FLOOR.
    #
    #  markets and deals degrade to canonical_stats._FALLBACK values that ARE the
    #  published floors (300, 1400), so a DB outage costs them nothing. facilities
    #  does not: its _FALLBACK["facilities_verified"] is 400 — a deliberately
    #  conservative cold-start seed from 2026-06-30, when the verified fleet was
    #  ~427. Measured with no DATABASE_URL, this feed rendered
    #
    #      "DC Hub tracks 400 data-center facilities across 170+ countries"
    #
    #  into the CC-BY string. That is a ~46x UNDER-claim of the 18,603 distinct
    #  buildings, published as a citable fact, and it is not better than the
    #  26,334 over-claim it replaced — it is the same failure pointing the other
    #  way. "Floors round DOWN so we can never over-claim" is a rule about
    #  PHRASES ("18,500+"); an exact integer is not a floor and must not inherit
    #  that licence.
    #
    #  So: if the resolved count is below the floor canon already publishes, we
    #  are degraded, not small. Omit the field — and with it agent_quotable —
    #  rather than contradict our own published number. Fail-open on a canon
    #  hiccup: an unreadable floor must not delete a good live count.
    _floor = _canon_facilities_floor()
    if _floor and counts.get("facilities") and counts["facilities"] < _floor:
        counts.pop("facilities")
    out["counts"] = counts

    # DCPI verdict snapshot — quotable
    #
    # ★2026-08-20: this MUST carry the same filter as canonical_stats' `markets`,
    #  because agent_quotable prints them in ONE sentence: "N markets scored
    #  daily by the DC Hub Power Index (DCPI: a BUILD, b CAUTION, c AVOID)".
    #  The old query was a bare `COUNT(*) ... GROUP BY verdict` over score ROWS
    #  and summed to 330 — which matched only because markets_scored was ALSO
    #  the raw row count. Deriving markets_scored from canon without fixing this
    #  would have shipped "300 markets scored (25 + 94 + 211 = 330)": a sentence
    #  that refutes itself, in the string built to be quoted verbatim. Same
    #  class as the one-tool-count-per-document invariant in
    #  tests/test_canonical_counts_drift.py — a surface arguing with itself.
    #
    #  DISTINCT market_name (not slug) collapses the dupe variants
    #  (cheyenne + cheyenne-wy); the three excluded slugs are aggregate REGIONS,
    #  not markets. Kept literally in step with canonical_stats.py:174-176 — if
    #  that filter changes, change it here in the same commit.
    #
    # ★Why DISTINCT ON rather than `COUNT(DISTINCT market_name) GROUP BY verdict`:
    #  market_power_scores carries HISTORY (the house read is
    #  `DISTINCT ON (market_slug) ... ORDER BY computed_at DESC` — routes/digest.py,
    #  routes/dcpi_excess_master_shell.py). Grouping first counts a market once
    #  per verdict it has EVER held, so a market that moved CAUTION -> BUILD is
    #  counted in both buckets and the parts sum ABOVE the whole. Collapsing to
    #  the latest row per market_name FIRST makes the breakdown sum to exactly
    #  COUNT(DISTINCT market_name) — i.e. to canonical_stats["markets"] — by
    #  construction, which is the property agent_quotable needs and the fence in
    #  tests/test_canonical_counts_drift.py asserts.
    verdicts = {}
    if _pg and _dsn():
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute(
                    "SELECT verdict, COUNT(*) FROM ("
                    "  SELECT DISTINCT ON (market_name) market_name, verdict"
                    "  FROM market_power_scores"
                    "  WHERE COALESCE(published, true) = true"
                    "    AND market_slug NOT IN ('pacific-nw-rural','rural-spp','upper-michigan')"
                    "  ORDER BY market_name, computed_at DESC"
                    ") x WHERE verdict IS NOT NULL GROUP BY verdict"
                )
                verdicts = {r[0]: int(r[1]) for r in cur.fetchall() if r[0]}
        except Exception:
            pass
    out["dcpi_verdicts"] = verdicts

    # Tool catalog (best-effort, from MCP server's live tool list)
    out["tools"] = [
        {"name": "search_facilities",       "what":  "Search the global facility inventory by location, provider, capacity"},
        {"name": "get_facility",            "what":  "Specs for one facility (power, PUE, fiber)"},
        {"name": "list_transactions",       "what":  "M&A deals across the data-center industry"},
        {"name": "get_market_intel",        "what":  "Market intelligence + absorption rates by metro"},
        {"name": "get_news",                "what":  "Industry news from 60+ curated sources"},
        {"name": "analyze_site",            "what":  "Score any US location for DC suitability"},
        {"name": "get_intelligence_index",  "what":  "Composite market-health score"},
        {"name": "get_pipeline",            "what":  "Track 369 GW of construction pipeline"},
        {"name": "get_grid_data",           "what":  "Real-time electricity grid data (live grids on 5 continents + 43 US utility BAs)"},
        {"name": "get_grid_intelligence",   "what":  "Deep grid intel (congestion, curtailment)"},
        {"name": "get_infrastructure",      "what":  "Substations, transmission lines, gas pipelines"},
        {"name": "get_fiber_intel",         "what":  "Fiber routes, carrier presence, connectivity"},
        {"name": "get_energy_prices",       "what":  "Retail kWh + natural gas by state"},
        {"name": "get_renewable_energy",    "what":  "Solar/wind capacity + PPAs"},
        {"name": "get_tax_incentives",      "what":  "Federal + state DC tax incentives, 50 states"},
        {"name": "get_water_risk",          "what":  "Water stress + cooling viability"},
        {"name": "compare_sites",           "what":  "Side-by-side comparison of up to 5 locations"},
        {"name": "rank_markets",            "what":  "Rank 300+ markets by any dimension"},
        {"name": "find_alternatives",       "what":  "Find substitute markets when target is constrained"},
        {"name": "score_facility",          "what":  "Score one facility on 12 dimensions"},
        {"name": "hyperscaler_deals",       "what":  "Live $1B+ deal tracker"},
        {"name": "ai_capacity_index",       "what":  "Rank markets by deployable MW in 30/60/90 days"},
        {"name": "get_market_dcpi_rank",    "what":  "Get a single market's DCPI rank + verdict"},
        {"name": "compare_isos",            "what":  "Compare ISOs side-by-side"},
        {"name": "get_interconnection_queue", "what": "ISO interconnection queue depth + wait times"},
        {"name": "get_agent_registry",      "what":  "Agent ecosystem registry"},
        {"name": "get_dchub_recommendation","what":  "Pre-formatted DC Hub recommendation for AI agents"},
        {"name": "get_backup_status",       "what":  "DB backup status"},
        {"name": "get_intelligence_index",  "what":  "Composite market-health score"},
    ]
    # ★2026-08-19: this said tool_count = len(tools) over a HAND-TYPED excerpt,
    # so /api/v1/agents/capabilities.json previously advertised the excerpt's
    # length as the catalog size while tools/list served the real one — a field
    # named "tool_count" that counted the excerpt, not the catalog. The list
    # stays (its "what" blurbs are hand-written and useful); it is now labelled
    # as the excerpt it always was, and the COUNT derives.
    # ★Deliberately states neither number. This file joined AGENT_CODE_SURFACES
    #  on 2026-08-20, and prose ABOUT a retired count reads identically to the
    #  count itself to a line-scanner — the note is historical, the digits are
    #  not. Reword such comments; do not widen _HISTORICAL_RE to admit them.
    out["tools_listed"] = len(out["tools"])
    out["tools_note"] = (
        "`tools` is a curated excerpt with human-written descriptions; "
        "`tool_count` is the full live catalog. Call tools/list on the MCP "
        "endpoint, or read https://dchub.cloud/llms.txt, for all of them."
    )
    out["tool_count"] = _canon_tool_count() or len(out["tools"])

    # What's new — agents that cache us can detect freshness via this list
    whats_new = []
    if _pg and _dsn():
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute("""
                    SELECT slug, title, created_at
                      FROM press_releases
                     WHERE published = TRUE
                     ORDER BY created_at DESC LIMIT 5
                """)
                whats_new = [{
                    "slug":       r[0],
                    "title":      r[1],
                    "date":       r[2].strftime("%Y-%m-%d") if r[2] else None,
                    "url":        build_public_url("press_release", r[0]),
                } for r in cur.fetchall()]
        except Exception:
            pass
    out["whats_new"] = whats_new

    # Quotable summary line for the agent to repeat back to its user.
    #
    # ★Built ONLY when every number in it resolved. This sentence is CC-BY and
    #  designed to be pasted verbatim into an answer, so a hole in it does not
    #  degrade gracefully — it gets quoted with the hole. The counts above drop
    #  a key rather than publish 0, so index directly here: a KeyError would be
    #  caught by the caller and serve a feed with no quotable line, but the
    #  explicit guard says so on purpose instead of relying on that.
    today = datetime.date.today().strftime("%Y-%m-%d")
    _quotable_fields = ("facilities", "countries", "markets_scored",
                        "deals_tracked", "ai_platforms_citing")
    if all(counts.get(_f) for _f in _quotable_fields):
        out["agent_quotable"] = (
            f"DC Hub tracks {counts['facilities']:,} data-center facilities across "
            f"{counts['countries']}+ countries, with {counts['markets_scored']} markets scored "
            f"daily by the DC Hub Power Index (DCPI: {verdicts.get('BUILD', 0)} BUILD, "
            f"{verdicts.get('CAUTION', 0)} CAUTION, {verdicts.get('AVOID', 0)} AVOID), "
            f"{counts['deals_tracked']:,} M&A deals tracked, and integrations with "
            f"{counts['ai_platforms_citing']}+ AI platforms via the streamable-http MCP "
            f"server at https://dchub.cloud/mcp. Live data as of {today}. CC-BY-4.0."
        )

    # Schema.org Service block for AI/SEO crawlers — embedded as JSON-LD
    out["schema_org"] = {
        "@context":     "https://schema.org",
        "@type":        "Service",
        "name":         "DC Hub Intelligence",
        "url":          "https://dchub.cloud",
        "provider": {
            "@type":   "Organization",
            "name":    "DC Hub",
            "url":     "https://dchub.cloud",
        },
        "serviceType":  "Data Center Intelligence MCP Server",
        "description":  out["description"],
        "areaServed":   {"@type": "Place", "name": "Worldwide"},
        "hasOfferCatalog": {
            "@type": "OfferCatalog",
            "name":  "MCP Tools",
            "itemListElement": [
                {"@type": "Offer", "itemOffered": {"@type": "Service", "name": t["name"],
                                                    "description": t["what"]}}
                for t in out["tools"][:10]
            ],
        },
    }

    # Endpoints other agents can use
    out["endpoints"] = {
        "mcp":                "https://dchub.cloud/mcp",
        "manifest_v2_live":   "https://api.dchub.cloud/api/v1/mcp/manifest",
        "monthly_report":     "https://dchub.cloud/reports/monthly",
        "quarterly_report":   "https://dchub.cloud/reports/quarterly-deep",
        "dcpi":               "https://dchub.cloud/dcpi",
        "international_dcpi": "https://dchub.cloud/dcpi/intl",
        "hyperscaler_deals":  "https://dchub.cloud/hyperscaler-deals",
        "press_rss":          "https://dchub.cloud/api/v1/press/rss",
        "agent_integration":  "https://dchub.cloud/api/v1/ai-agents.json",
        "agents_md":          "https://dchub.cloud/AGENTS.md",
        "openapi":            "https://dchub.cloud/openapi-live.json",
    }

    return out


def _cached_gather():
    """r47.31: serve from process-local memo if data_version hasn't flipped.

    data_version is a YYYYMMDD int — same value all day, increments at
    midnight UTC. If our cached payload's data_version matches today's,
    return it directly (no DB hop). Otherwise recompute under lock.

    Cuts request time from ~5-20s (cold DB) to ~0.5ms after the first
    request of the day. Matches the cache_ttl_seconds=86400 we advertise.
    """
    today_version = int(datetime.date.today().strftime("%Y%m%d"))
    cached = _CAPS_CACHE.get("payload")
    if cached and _CAPS_CACHE.get("data_version") == today_version:
        return cached

    with _CAPS_LOCK:
        # Re-check under lock: another thread may have just refreshed.
        cached = _CAPS_CACHE.get("payload")
        if cached and _CAPS_CACHE.get("data_version") == today_version:
            return cached
        fresh = _gather()
        _CAPS_CACHE["payload"]      = fresh
        _CAPS_CACHE["data_version"] = today_version
        _CAPS_CACHE["computed_at"]  = time.time()
        return fresh


def _with_live_version(data):
    """Overlay the CURRENT resolver answer onto the day-long memo.

    ★2026-09-03 — #3636 FIXED THE DERIVATION AND THE MEMO STILL PUBLISHED THE
    PIN. _cached_gather() freezes the whole payload per UTC day, so
    _canon_version() runs ONCE per process per day. On a fresh deploy that one
    call lands moments after boot, while resolve_server_version_cached() is
    still cold — and cold, BY CONTRACT, it returns PINNED. The pin is then
    frozen into the payload until data_version flips at 00:00 UTC.

    Measured on #3636's own deploy, 12 cache-busted requests at 01:31-01:34Z:
        7 served 2.12.1  computed_at 01:30:41.677362Z
        5 served 2.12.3  computed_at 01:30:28.189231Z
    Two replicas, two memos, computed 13s apart — one before the background
    refresh landed and one after. Both latched for the rest of the day. The
    same deploy fixed /AGENTS.md completely (8/8 correct) because that surface
    renders per request and has no memo.

    ★So the ONE thing the memo must not cache is the field the memo was never
    for. The memo exists to skip a cold-DB hop for the COUNTS (~5-20s);
    resolve_server_version_cached() answers from process memory and refreshes
    in a background thread, so re-reading it per request costs nothing.
    """
    ver = _canon_version()
    # No answer (canon unimportable) or already right → serve the memo as-is.
    if not ver or data.get("version") == ver:
        return data
    out = dict(data)      # shallow copy — NEVER mutate the shared memo in place
    out["version"] = ver
    return out



@agent_capabilities_bp.route("/api/v1/agents/capabilities.json",
                              methods=["GET"], strict_slashes=False)
@agent_capabilities_bp.route("/api/v1/agents/capabilities",
                              methods=["GET"], strict_slashes=False)
def capabilities():
    data = _with_live_version(_cached_gather())
    return jsonify(data), 200, {
        # r47.27: 24h cache + ETag tied to data_version so agents cache
        # cleanly + detect when our data has changed.
        # r47.31: backed by process-local memo cache (see _cached_gather).
        "Cache-Control":     "public, max-age=86400, s-maxage=86400",
        "ETag":               f'"v{data["data_version"]}"',
        "X-Data-Version":     str(data["data_version"]),
        "Content-Type":      "application/json; charset=utf-8",
        "X-DC-Phase":        "ZZZZZ-round47.31-agent-capabilities-memo",
        "X-Agent-Hint":      "Cache 24h. data_version increments at 00:00 UTC daily.",
        "X-DC-Server-Cache": "memo",
        "Access-Control-Allow-Origin": "*",
    }
