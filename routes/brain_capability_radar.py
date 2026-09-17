"""Capability / data-milestone radar — the autonomous "what can we announce" input.

The announce machinery already exists (media_editorial.rank_data_events ranks
leads -> the LinkedIn analyst quad posts them 4x/day). This is the missing INPUT:
it turns "we shipped or grew a capability" into a ranked analyst-voice lead, so
new feeds/tools AND coverage/reach milestones get announced WITHOUT hand-feeding.

Registry-driven (owner choice). Two entry modes:
  • mode="launch"    a NEW data source/feed we just shipped. Emits capability_launch
                     the first time it's seen (no baseline), then data_milestone on
                     a jump_pct growth.
  • mode="milestone" an EXISTING metric (coverage/reach) worth announcing on
                     round-number crossings (round_step) or jump_pct growth. Needs a
                     SEEDED baseline so it never announces the current level as "new"
                     — seed once with seed_milestone_baselines().

Bookkeeping (took care to get right): capability_radar_leads() is READ-ONLY. The
baseline (data_milestone_snapshots) advances ONLY via mark_capability_announced(),
called from linkedin_quad_daily AFTER a successful LinkedIn post — so a lead stays
visible until actually posted, and previews never consume it.

Adding a future feed/metric = ONE registry row.
"""
import logging
import os

import psycopg2
from routes._swallowed_writes import note_swallowed_write
from util.db_honesty import close_quietly, try_fetchall
from util.deals import DEALS_OK

logger = logging.getLogger("brain_capability_radar")


def _dsn() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


def _smithery_core_rank() -> dict | None:
    """LIVE external check: how many CORE Smithery search terms DC Hub ranks #1 for.
    A registry source can use `check` instead of `metric_sql` — this is the first.
    FAIL-SAFE: returns None if Smithery is unreachable (→ the radar skips this source,
    never a false announce). Per-term errors are non-fatal. Browser UA (Smithery 403s
    the default urllib UA). Used by the smithery_rank_1 launch source below."""
    import json as _json
    import urllib.parse
    import urllib.request
    terms = ["data center", "power grid", "fiber", "capacity",
             "grid interconnection", "interconnection"]
    ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    at1, ok_any = [], False
    for t in terms:
        try:
            url = "https://registry.smithery.ai/servers?" + urllib.parse.urlencode({"q": t, "pageSize": "5"})
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                d = _json.load(resp)
            ok_any = True
            servers = d.get("servers") or []
            if servers and "dchub" in (servers[0].get("qualifiedName") or "").lower():
                at1.append(t)
        except Exception:
            continue
    if not ok_any:
        return None
    return {"core_at_1": float(len(at1)), "terms": at1,
            "terms_str": ", ".join(f'"{t}"' for t in at1)}


_CANON_CACHE: dict = {"at": 0.0, "val": None}
_CANON_TTL = 300.0  # 5-min memo so 6 evergreen rows in one leads() pass = 1 query set


def _canonical_stats() -> dict | None:
    """LIVE check: DC Hub's canonical coverage numbers for the evergreen moat/pillar
    sources — verified/tracked/countries/deals/markets. Reads the SAME source-of-truth
    COUNTs as /api/v1/stats/canonical (routes.facilities_by_dims.stats_canonical) but
    IN-PROCESS over the DB, NOT via an HTTP self-request. The old self-request
    (backend -> Cloudflare -> backend) timed out on Railway and silently starved these
    leads, so the DCPI-Cheyenne build lead won every run. FAIL-SAFE: returns None if the
    DB is unreachable or the core counts look empty, so the radar SKIPS the source rather
    than announce a wrong/zero number. Honesty: 'verified' is the canonical fleet filter
    (COALESCE(is_duplicate,0)=0) — always reported as verified-inside-a-tracked-frontier,
    NEVER as the raw tracked total. Anything the copy calls a FACILITY quotes
    `distinct` (distinct buildings); `tracked` and `verified` are row counts and
    may appear only framed as source records."""
    import time as _time
    now = _time.time()
    if _CANON_CACHE["val"] is not None and (now - _CANON_CACHE["at"]) < _CANON_TTL:
        return _CANON_CACHE["val"]
    dsn = _dsn()
    if not dsn:
        return None
    # ★ 2026-08-01 — this was `with psycopg2.connect(...) as c:` and the
    # comment below claimed each COUNT was its own transaction. It was not:
    # psycopg2's connection context manager is a TRANSACTION manager, so the
    # first failing COUNT would abort the transaction and every later COUNT on
    # the connection would die with InFailedSqlTransaction and return 0.0 —
    # and this function feeds the MEDIA announcer, so a poisoned read here
    # publishes a wrong coverage number to LinkedIn/X, not just to an API
    # consumer. All five COUNTs pass against the live DB today; the structure
    # is fixed so that stays a property of the code, not a coincidence.
    # See util/db_honesty and #2071.
    c = None
    failed = []
    try:
        c = psycopg2.connect(dsn, sslmode="require", connect_timeout=8)
        c.autocommit = True
        try:
            with c.cursor() as cur:
                def _count(sql: str) -> float:
                    """A count, or NaN-by-omission: a failure is RECORDED.

                    Never 0.0 on failure. `_canonical_stats` is allowed to skip
                    a source entirely, which is the honest outcome; publishing
                    a 0 would announce "DC Hub tracks 0 deals".
                    """
                    rows, err = try_fetchall(cur, sql)
                    if err:
                        failed.append(f"{sql.split('FROM')[-1].strip()[:40]}: {err}")
                        return 0.0
                    return float(rows[0][0] or 0) if rows else 0.0
                # byte-for-byte the queries stats_canonical() runs, so media numbers
                # always agree with the public /api/v1/stats/canonical surface.
                out = {
                    # ★2026-08-23 — THE CITEABLE FACILITY COUNT. Every "N
                    # facilities" claim in the REGISTRY headlines below must read
                    # THIS key and nothing else. `tracked` is COUNT(*) = raw
                    # source ROWS (~1.4x buildings: the March 2026 backfill wrote
                    # several rows per site) and `verified` is a keeper-ROW count
                    # — both are row piles, neither is a building count. The
                    # 16:00 capability slot published `tracked` as "26,387
                    # facilities" and the claim-breaker refused it two days
                    # running (rows_ne_buildings, 2026-08-22/23).
                    # Byte-for-byte the query canonical_stats.get_canonical_stats()
                    # serves as facilities_verified — which IS the ceiling that
                    # media_fact_check_guard.check_facility_count_claims measures
                    # this copy against. Keep the two SQL strings identical: that
                    # is what makes composer and gate agree by construction
                    # instead of by coincidence.
                    "distinct":  _count("SELECT COUNT(DISTINCT canonical_slug) "
                                        "FROM discovered_facilities "
                                        "WHERE COALESCE(is_duplicate,0)=0 "
                                        "  AND canonical_slug IS NOT NULL"),
                    # DE-DUPLICATION STATES, not building counts and not source
                    # verifications — /api/v1/stats/canonical's own provenance
                    # block says so in as many words. Publishable ONLY in copy
                    # that names them as source records.
                    "verified":  _count("SELECT COUNT(*) FROM discovered_facilities WHERE COALESCE(is_duplicate,0)=0"),
                    "tracked":   _count("SELECT COUNT(*) FROM discovered_facilities"),
                    # ★2026-07-30 — WRONG-TABLE PAIRING (same class + same fix
                    # as countries_covered on /api/v1/stats/canonical, routes/
                    # facilities_by_dims.py): this read the LEGACY `facilities`
                    # table (mixed name/ISO formats — "USA"+"US") while
                    # verified/tracked above count discovered_facilities, and
                    # while this function's contract is byte-for-byte the
                    # stats_canonical queries. #1958 moved the canonical field
                    # to discovered_facilities; the mirror must follow.
                    "countries": _count("SELECT COUNT(DISTINCT country) FROM discovered_facilities WHERE country IS NOT NULL AND country <> ''"),
                    # ★2026-07-30 — same audit: this was COUNT(*) of
                    # market_power_scores ROWS — score rows, not scored
                    # markets, a different and larger number. Byte-for-byte
                    # the canonical markets query (canonical_stats.py):
                    # DISTINCT market_name, published only, minus the three
                    # aggregate regions.
                    "markets":   _count("SELECT COUNT(DISTINCT market_name) FROM market_power_scores "
                                        "WHERE COALESCE(published, true) = true "
                                        "AND market_slug NOT IN ('pacific-nw-rural','rural-spp','upper-michigan')"),
                    "deals":     _count(f"SELECT COUNT(*) FROM deals WHERE {DEALS_OK}"),
                    "tools":     73.0,
                }
        finally:
            close_quietly(c)
        # ★ ANY failed count -> skip the whole source. The pre-existing guard
        # below only covered verified/tracked/countries, so a failed `deals` or
        # `markets` read passed straight through as 0.0 and the radar would
        # have announced "1,400+ deals" as "0 deals" to a public feed. A number
        # we could not measure is not announceable at any value.
        if failed:
            return None
        # guard: core counts missing -> skip (never post a zero/garbage number)
        if (out["distinct"] <= 0 or out["verified"] <= 0
                or out["tracked"] <= 0 or out["countries"] <= 0):
            return None
        _CANON_CACHE["at"] = now
        _CANON_CACHE["val"] = out
        return out
    except Exception:
        close_quietly(c)
        return None


# ── The registry: one row per announceable capability/metric ────────────────
REGISTRY = [
    # ---- LAUNCH: new data feeds (announce once on ship) --------------------
    {
        "key": "planned_generation",
        "mode": "launch",
        "metric_sql": ("SELECT COUNT(*) AS n, COALESCE(SUM(capacity_mw),0) AS mw, "
                       "COUNT(DISTINCT state) AS states FROM planned_generators "
                       "WHERE source='eia860m_planned'"),
        "value_key": "mw", "jump_pct": 0.20, "score": 82,
        "source_url": "https://dchub.cloud/land-power",
        "headline": lambda r: (f"DC Hub now maps the full US generation build pipeline: "
                               f"{r['mw'] / 1000:.0f} GW of planned capacity across "
                               f"{int(r['n']):,} generators in {int(r['states'])} states"),
        "trend": ("planned, permitting and under-construction generators nationwide, "
                  "including the non-ISO regions (TVA, Southern, Arizona) the per-ISO "
                  "interconnection queues miss"),
        "so_what": ("the forward power-supply curve for every siting decision, on the "
                    "map and via the get_power_pipeline MCP tool."),
    },
    {
        "key": "operable_generation",
        "mode": "launch",
        "metric_sql": ("SELECT COUNT(*) AS n, COALESCE(SUM(capacity_mw),0) AS mw, "
                       "COUNT(DISTINCT ba_code) AS bas FROM generator_inventory "
                       "WHERE source='eia860m'"),
        "value_key": "mw", "jump_pct": 0.20, "score": 78,
        "source_url": "https://dchub.cloud/dcpi",
        "headline": lambda r: (f"DC Hub now tracks the full operable US generator fleet: "
                               f"{r['mw'] / 1000:.0f} GW across {int(r['n']):,} generators, "
                               f"keyed to {int(r['bas'])} balancing authorities"),
        "trend": ("operating, standby and returning-to-service capacity by ISO and fuel, "
                  "with the standby reserve that signals grid headroom"),
        "so_what": "the installed-supply side of the power picture, by ISO, behind the DCPI scores.",
    },
    # ---- MILESTONE: coverage (round-number crossings) ----------------------
    {
        "key": "facility_coverage",
        "mode": "milestone", "round_step": 1000, "value_key": "n", "score": 74,
        # ★2026-08-23 — THIS SOURCE WROTE THE 2026-08-17 POST the claim-breaker
        # was built to stop: "26,000 data-center facilities are now live in DC
        # Hub's index, spanning 179 countries" (lead_entity=facilitycoverage).
        # COUNT(*) is raw source ROWS; the headline called them facilities. The
        # value_key stays "n" (rows) ON PURPOSE — data_milestone_snapshots holds
        # a row-scale baseline (~26.3K) and mode="milestone" only fires when the
        # bucket INCREASES, so re-pointing value_key at the ~18.7K distinct count
        # would not misfire, it would go SILENTLY DEAD until distinct passed
        # 27,000 — roughly 8,000 facilities of growth, unannounced, with nothing
        # logged. seed_milestone_baselines() has no caller, so nothing would
        # re-scale it. What crosses is a source-record bucket, so that is what
        # the copy now says; the citeable building count rides in the same
        # sentence, off the same row.
        "metric_sql": ("SELECT COUNT(*) AS n, COUNT(DISTINCT country) AS countries, "
                       "COUNT(DISTINCT canonical_slug) FILTER ("
                       "WHERE COALESCE(is_duplicate,0)=0 AND canonical_slug IS NOT NULL"
                       ") AS distinct_buildings FROM discovered_facilities"),
        "source_url": "https://dchub.cloud/map",
        "headline": lambda r: (f"DC Hub's live index just crossed {int(r['_milestone']):,} "
                               f"source records — {int(r['distinct_buildings']):,} distinct "
                               f"data-center facilities across {int(r['countries'])} countries"),
        "trend": "the most complete machine-readable map of the physical AI buildout, refreshed daily",
        "so_what": "every facility queryable by AI agents and on the map, with power, fiber and tenant context.",
    },
    {
        "key": "country_coverage",
        "mode": "milestone", "round_step": 10, "value_key": "n", "score": 70,
        "metric_sql": "SELECT COUNT(DISTINCT country) AS n FROM discovered_facilities",
        "source_url": "https://dchub.cloud/map",
        "headline": lambda r: (f"DC Hub now tracks data-center infrastructure in "
                               f"{int(r['_milestone'])}+ countries"),
        "trend": "global coverage of the physical layer behind AI compute, one live dataset",
        "so_what": "cross-border site comparisons agents and developers can run in one query.",
    },
    {
        "key": "dcpi_markets",
        "mode": "milestone", "round_step": 25, "value_key": "n", "score": 70,
        # ★2026-07-30 — same audit as _canonical_stats "markets" above: this was
        # COUNT(*) of market_power_scores ROWS (317 live) announced as scored
        # MARKETS. Now the canonical markets query (canonical_stats.py) + AS n
        # for value_key. The canonical count (306 live) sits BELOW the stored
        # 317 baseline — safe: milestone fires only when the 25-bucket
        # INCREASES and _milestone derives from the CURRENT value, so the drop
        # re-announces nothing; the source stays quiet until canonical ≥ 325.
        "metric_sql": ("SELECT COUNT(DISTINCT market_name) AS n FROM market_power_scores "
                       "WHERE COALESCE(published, true) = true "
                       "AND market_slug NOT IN ('pacific-nw-rural','rural-spp','upper-michigan')"),
        "source_url": "https://dchub.cloud/dcpi",
        "headline": lambda r: (f"The DC Hub Power Index now scores {int(r['_milestone'])}+ "
                               f"markets on buildable headroom"),
        "trend": "a single comparable power-availability score per market, updated continuously",
        "so_what": "rank-and-shortlist any market on time-to-power without a consultant.",
    },
    # ---- MILESTONE: reach -------------------------------------------------
    {
        "key": "ai_citations",
        "mode": "milestone", "round_step": 10, "value_key": "n", "score": 80,
        "metric_sql": "SELECT COUNT(*) AS n, COUNT(DISTINCT engine) AS engines FROM ai_citations WHERE dchub_cited=TRUE",
        "source_url": "https://dchub.cloud",
        "headline": lambda r: (f"DC Hub has now been cited {int(r['_milestone'])}+ times in AI "
                               f"answers across {int(r['engines'])} engines"),
        "trend": "AI assistants are sourcing live data-center infrastructure facts from DC Hub",
        "so_what": "the MCP-native data layer is becoming the default ground truth agents cite.",
    },
    # r-requests-milestone (2026-07-27, owner directive 100003): total requests
    # served — the /ai headline counter — fires at each new MILLION crossed.
    # Honest labeling per the /ai-page rule: this is TOTAL (all sources); the
    # external AI-platform subset lives on the same page and is named in the
    # trend line so the quad's copy can't over-claim. Baseline seeded at
    # 3,000,000 on ship (the 3M crossing was announced by hand 2026-07-27) so
    # the first radar-fired post is 4,000,000.
    {
        "key": "requests_served_total",
        "mode": "milestone", "round_step": 1000000, "value_key": "n", "score": 84,
        "metric_sql": "SELECT COALESCE(SUM(total_requests),0)::bigint AS n FROM ai_cumulative",
        "source_url": "https://dchub.cloud/ai",
        "headline": lambda r: (f"DC Hub has now served {int(r['_milestone']):,}+ total requests "
                               f"across its live infrastructure data layer"),
        "trend": ("total queries served, all sources, per the public dchub.cloud/ai counter; "
                  "the external AI-platform subset is broken out on the same page"),
        "so_what": ("request volume is the adoption curve: agents query live infrastructure "
                    "data instead of guessing from static training sets."),
    },
    # ---- ACHIEVEMENT: competitive standing (live-verified, announce once) ---
    {
        "key": "smithery_rank_1",
        "mode": "launch",            # announce once when the achievement is first verified
        "check": _smithery_core_rank,  # live external check instead of metric_sql
        "value_key": "core_at_1",
        "min_value": 5,              # only fire once we lead a robust majority of the core cluster
        "score": 88,
        "source_url": "https://smithery.ai/servers/azmartone67/dchub",
        "headline": lambda r: (f"DC Hub is now the #1 data-center MCP server on Smithery — "
                               f"ranked #1 for {r['terms_str']}"),
        "trend": ("agents browsing the Smithery MCP marketplace now find DC Hub first for "
                  "data-center, power-grid and interconnection queries — ahead of every other "
                  "server in the data-center category"),
        "so_what": ("the live, MCP-native data layer agents reach for on data-center "
                    "infrastructure — query it and cite it at dchub.cloud/mcp."),
    },
    # ---- EVERGREEN: the moat/pillar + platform story goldmine (r-media-goldmine
    # 2026-07-14). mode="evergreen" re-emits every repost_days so first-party
    # product news stays on the LinkedIn/X board on ordinary days instead of the
    # DCPI-Cheyenne build lead. All numbers come from _canonical_stats (live,
    # fail-safe). Honesty guards baked into every headline: verified is ALWAYS
    # "inside a tracked frontier" (never raw-as-verified), no Brazil gas share, no
    # Singapore/Australia "ranked", CC-BY where a dataset claim is made. --------
    {
        "key": "provenance_envelope", "mode": "evergreen", "repost_days": 12,
        "check": _canonical_stats, "value_key": "verified", "score": 64,
        "source_url": "https://dchub.cloud/whats-new#platform",
        "headline": lambda r: (f"{int(r['distinct']):,} distinct facilities now carry a provenance "
                               f"stamp — every DC Hub record ships source, method, as-of and a "
                               f"CC-BY-4.0 citation, with a verified-vs-tracked confidence flag "
                               f"against a tracked frontier of {int(r['tracked']):,} source records, "
                               f"so agents cite live data instead of guessing"),
        "trend": ("Provenance Envelope v1 (provenance_version:1) on search_facilities and the "
                  "canonical stats endpoint — source, method, as-of and a CC-BY-4.0 citation "
                  "template on every record"),
        "so_what": ("agents cite with confidence instead of guessing; the live verified/tracked "
                    "split is public at /api/v1/stats/canonical."),
    },
    {
        "key": "intl_grid_telemetry", "mode": "evergreen", "repost_days": 12,
        "check": _canonical_stats, "value_key": "countries", "score": 63,
        "source_url": "https://dchub.cloud/playground",
        "headline": lambda r: ("Keyless grid telemetry, 10/day free: Japan (OCCTO), South Korea "
                               "(KPX) and Brazil (ONS) just joined the DC Hub scoreboard — ranking "
                               "beside the US ISOs, EU zones, Great Britain and Taiwan on one "
                               "real-time renewable-share scale across five continents"),
        "trend": ("one live, ranked scoreboard of national grids on a single renewable-share scale, "
                  "refreshed continuously — not a quarterly PDF"),
        "so_what": ("compare grids for LatAm and APAC siting the same way you compare US ISOs — "
                    "get_grid_scoreboard, no key."),
    },
    {
        "key": "agent_memory", "mode": "evergreen", "repost_days": 12,
        "check": _canonical_stats, "value_key": "markets", "score": 62,
        "source_url": "https://dchub.cloud/connect#start",
        "headline": lambda r: (f"{int(r['distinct']):,} facilities are now saveable to a durable, "
                               f"per-agent shortlist — DC Hub shipped memory: save_site remembers "
                               f"your sites, then get_changes returns per-site deltas next session "
                               f"(verdict flips, DCPI moves, new nearby facilities), not the whole "
                               f"planet"),
        "trend": ("persistent per-agent state — a data layer that remembers your shortlist, not a "
                  "stateless query API"),
        "so_what": "your agent returns to what changed on your list; save_site + get_changes over MCP.",
    },
    {
        "key": "error_envelope", "mode": "evergreen", "repost_days": 14,
        "check": _canonical_stats, "value_key": "verified", "score": 63,
        "source_url": "https://dchub.cloud/docs/error-codes",
        "headline": lambda r: (f"{int(r['deals']):,} deals, {int(r['markets'])} markets and "
                               f"{int(r['distinct']):,} facilities now speak one versioned error "
                               f"contract — DC Hub shipped error_version:1, an in-band, "
                               f"machine-readable contract, so a bad parameter returns a "
                               f"deterministic recovery hint, not a dead end"),
        "trend": ("a machine-readable error contract with a severity class and server-computed "
                  "suggested_params; the taxonomy is published at /docs/error-codes"),
        "so_what": "agents recover from edge cases deterministically — fewer dead-ends, more completed tasks.",
    },
    {
        "key": "tool_catalog", "mode": "evergreen", "repost_days": 10,
        "check": _canonical_stats, "value_key": "tools", "score": 63,
        "source_url": "https://dchub.cloud/capabilities",
        "headline": lambda r: (f"{int(r['markets'])} markets and {int(r['distinct']):,} facilities are "
                               f"now served by 2 new agent tools — get_retirement_headroom (filed US "
                               f"generator retirements + nearest substations) and "
                               f"cluster_sites_by_latency (physics-bounded fiber-latency clustering); "
                               f"the DC Hub MCP surface is now {int(r['tools'])} live tools"),
        "trend": ("new agent-callable primitives ship continuously — retirement headroom, latency "
                  "clustering, provenance-stamped search"),
        "so_what": "agents get site-selection signals no competitor exposes as a tool; browse them at /capabilities.",
    },
    {
        "key": "weekly_ledger", "mode": "evergreen", "repost_days": 7,
        "check": _canonical_stats, "value_key": "deals", "score": 62,
        "source_url": "https://dchub.cloud/whats-new",
        "headline": lambda r: (f"{int(r['distinct']):,} distinct facilities, {int(r['deals']):,} "
                               f"deals and {int(r['markets'])} markets — the DC Hub ledger across "
                               f"{int(r['countries'])}+ countries, deduplicated from "
                               f"{int(r['tracked']):,} raw source records, one live machine-readable "
                               f"layer refreshed daily and open under CC-BY-4.0"),
        "trend": ("the compounding coverage behind the physical AI buildout, queryable over MCP — "
                  "cite as DC Hub (dchub.cloud)"),
        "so_what": "one queryable ground-truth layer instead of a dozen stale PDFs; connect an agent in 60 seconds.",
    },
]


def _ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS data_milestone_snapshots (
            source_key   TEXT PRIMARY KEY,
            last_value   DOUBLE PRECISION,
            announced_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)


def _metric(cur, src):
    """Resolve a registry source's value: a live `check` callable (external, no DB)
    or metric_sql against the DB -> (row dict, diff value)."""
    if src.get("check"):
        r = src["check"]()
        if not r:
            return None, None
        return r, float(r.get(src["value_key"]) or 0)
    cur.execute(src["metric_sql"])
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    if not row:
        return None, None
    r = {k: (float(v) if isinstance(v, (int, float)) else v) for k, v in zip(cols, row)}
    return r, float(r.get(src["value_key"]) or 0)


def capability_radar_leads() -> list[dict]:
    """READ-ONLY. Return analyst-voice leads for new sources + milestone crossings.

    Shape matches media_editorial.rank_data_events() leads. Never writes (the
    baseline advances only on an actual post, via mark_capability_announced)."""
    dsn = _dsn()
    if not dsn:
        return []
    leads: list[dict] = []
    # Warm the canonical-stats memo BEFORE opening our own DB connection below, so the
    # per-source _canonical_stats() calls inside the loop hit the cache and never open a
    # NESTED connection while `c` is held. That nested connect fails under the pooler and
    # was silently starving every evergreen moat/pillar lead (returning None -> the source
    # skipped), so the DCPI-Cheyenne build lead won every run. Called with no open
    # connection here, it succeeds and caches (mirrors the working direct-call path).
    try:
        _canonical_stats()
    except Exception:
        pass
    c = None
    try:
        # ★ 2026-08-01 — was `with psycopg2.connect(...) as c`, the transaction
        # -manager trap. This loop survived it only because the per-source
        # `except` below already calls c.rollback(); `_ensure_table(cur)` had no
        # such cover, so a failure THERE would have poisoned every source in the
        # run and the radar would have gone quiet with nothing logged per source.
        # try/finally + close makes the rollback a second line of defence rather
        # than the only one. See util/db_honesty and #2085.
        c = psycopg2.connect(dsn, sslmode="require", connect_timeout=8)
        c.autocommit = True
        with c.cursor() as cur:
            _ensure_table(cur)
            for src in REGISTRY:
                try:
                    r, cur_val = _metric(cur, src)
                    if r is None or cur_val <= 0:
                        continue
                    if cur_val < float(src.get("min_value", 0)):
                        continue  # achievement threshold not met yet (e.g. not #1 enough)
                    cur.execute(
                        "SELECT last_value, "
                        "(announced_at IS NULL OR announced_at < NOW() - %s * INTERVAL '1 day') AS due "
                        "FROM data_milestone_snapshots WHERE source_key=%s",
                        (int(src.get("repost_days", 14)), src["key"]))
                    prev_row = cur.fetchone()
                    prev = prev_row[0] if prev_row else None
                    _ever_due = (prev_row[1] if prev_row else True)  # no baseline row -> due
                    mode = src.get("mode", "launch")
                    jp = src.get("jump_pct")
                    rs = src.get("round_step")
                    is_new = prev is None
                    kind = None

                    if mode == "launch":
                        if is_new:
                            kind = "capability_launch"
                        elif jp and prev and cur_val >= prev * (1 + jp):
                            kind = "data_milestone"
                    elif mode == "evergreen":
                        # re-postable moat/pillar/product news: fire when never announced
                        # OR when the last announce is older than repost_days. Distinct kind
                        # per capability (cap_<key>) so the kind-cooldown rotates THROUGH them
                        # instead of blocking the whole class.
                        if is_new or _ever_due:
                            kind = f"cap_{src['key']}"
                    else:  # milestone — needs a seeded baseline; never announces "new"
                        if is_new:
                            continue
                        crossed = rs and int(cur_val // rs) > int(prev // rs)
                        jumped = jp and cur_val >= prev * (1 + jp)
                        if crossed or jumped:
                            kind = "data_milestone"
                            r["_milestone"] = (int(cur_val // rs) * rs) if rs else cur_val
                    if not kind:
                        continue

                    headline = src["headline"](r) if callable(src["headline"]) else src["headline"]
                    trend = src.get("trend", "")
                    if kind == "data_milestone" and prev and mode == "launch":
                        trend = f"+{(cur_val / prev - 1) * 100:.0f}% since last reported. " + trend
                    lead = {
                        "kind": kind,
                        "headline_number": headline,
                        "trend": trend,
                        "so_what": src.get("so_what", ""),
                        "source_url": src.get("source_url", "https://dchub.cloud"),
                        "dedup_key": (f"capability:{src['key']}" if kind == "capability_launch"
                                      else f"cap:{src['key']}" if mode == "evergreen"
                                      else f"milestone:{src['key']}"),
                        "score": float(src.get("score", 70)),
                    }
                    # 2026-07-14: evergreen cap_* leads carry a `card` spec so the
                    # publisher renders the branded data-card (style=data_card) with
                    # this lead's LIVE canonical numbers, instead of the generic
                    # ai_hero card. Keyed by src['key'] → og_cards._dc_spec layout.
                    if mode == "evergreen":
                        lead["card"] = {
                            "kind": src["key"],
                            "nums": {
                                # `d` is the citeable distinct-BUILDING count and
                                # is what any card slot labelled "facilities"
                                # must render; v/t are row piles (see above).
                                "d":  int(r.get("distinct")  or 0),
                                "v":  int(r.get("verified")  or 0),
                                "t":  int(r.get("tracked")   or 0),
                                "m":  int(r.get("markets")   or 0),
                                "dl": int(r.get("deals")     or 0),
                                "c":  int(r.get("countries") or 0),
                                "tl": int(r.get("tools")     or 0),
                            },
                        }
                    leads.append(lead)
                except Exception as e:
                    logger.warning("[capability-radar] source %s skipped: %s",
                                   src.get("key"), str(e)[:140])
                    try:
                        c.rollback()
                    except Exception:
                        pass
    except Exception as e:
        logger.warning("[capability-radar] failed: %s", str(e)[:160])
    finally:
        close_quietly(c)
    return leads


def mark_capability_announced(dedup_key: str) -> bool:
    """Advance a source's baseline because its lead was just POSTED (success).

    Called from linkedin_quad_daily after a successful publish. The ONLY writer of
    the baseline. dedup_key is 'capability:<key>' or 'milestone:<key>'."""
    if not dedup_key or ":" not in dedup_key:
        return False
    key = dedup_key.split(":", 1)[1]
    src = next((s for s in REGISTRY if s["key"] == key), None)
    if not src or not _dsn():
        return False
    try:
        with psycopg2.connect(_dsn(), sslmode="require", connect_timeout=8) as c:
            c.autocommit = True
            with c.cursor() as cur:
                _ensure_table(cur)
                _, cur_val = _metric(cur, src)
                if cur_val is None:
                    return False
                cur.execute("""
                    INSERT INTO data_milestone_snapshots (source_key, last_value, announced_at)
                    VALUES (%s, %s, NOW() ON CONFLICT DO NOTHING)
                    ON CONFLICT (source_key)
                    DO UPDATE SET last_value=EXCLUDED.last_value, announced_at=NOW()
                """, (key, cur_val))
                return True
    except Exception as e:
        logger.warning("[capability-radar] mark %s failed: %s", key, str(e)[:120])
        return False


def seed_milestone_baselines() -> dict:
    """One-time: record the CURRENT value of every mode='milestone' source so it
    only fires on the NEXT crossing (never announces the existing level as new).
    Launch sources are intentionally left unseeded so they announce on ship.
    Idempotent: only seeds milestone sources that have no baseline yet."""
    dsn = _dsn()
    if not dsn:
        return {"ok": False, "error": "no dsn"}
    seeded = []
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            c.autocommit = True
            with c.cursor() as cur:
                _ensure_table(cur)
                for src in REGISTRY:
                    if src.get("mode") != "milestone":
                        continue
                    cur.execute("SELECT 1 FROM data_milestone_snapshots WHERE source_key=%s", (src["key"],))
                    if cur.fetchone():
                        continue
                    try:
                        _, cur_val = _metric(cur, src)
                        cur.execute("""INSERT INTO data_milestone_snapshots (source_key, last_value, announced_at)
                                       VALUES (%s, %s, NOW() ON CONFLICT DO NOTHING) ON CONFLICT (source_key) DO NOTHING""",
                                    (src["key"], cur_val))
                        seeded.append({src["key"]: cur_val})
                    except Exception:
                        note_swallowed_write("data_milestone_snapshots", where="brain_capability_radar.seed_milestone_baselines")
                        c.rollback()
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}
    return {"ok": True, "seeded": seeded}
