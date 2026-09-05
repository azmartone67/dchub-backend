"""crawler_externality.py — THE single source of the CRAWLER-channel path split.

★ WHY THIS FILE EXISTS (2026-08-06)

The crawler & citation channel was the last major surface with NO externality
filter of any kind. mcp_calls_deloop.py declares its de-loop predicates for
`mcp_calls_identity` / `mcp_call_log` ONLY; this channel's own published
definition said, in full, "crawler & citation requests by AI-platform
user-agent (NOT agent tool calls)". Nothing in it — inside the codebase or
outside it — could subtract DC Hub's own traffic.

It was also, that week, our best-looking number. Claude's weekly crawler reach
read 3,557 -> 4,370 -> 5,067 -> 7,194 across four readings on 08-04..08-06,
+102% in two days, while the tool-call channel went the other way (agents
48 -> 41, calls 6,764 -> 6,363). Across those exact two days we ran a campaign
instructing AI assistants — Claude among them — to fetch our surfaces before
answering, with the standing rule "fetch https://dchub.cloud/mcp with Accept:
application/json before answering anything about DC Hub", the operator pasted
our URLs into Claude conversations repeatedly, and an engineering session hit
dchub.cloud endpoints hundreds of times. Every Claude-UA request lands in the
crawler bucket.

★ THIS MODULE DOES NOT CLAIM THE CLIMB WAS SELF-TRAFFIC. It is unproven in
both directions, and that is the whole point: the channel could not answer the
question. The fix is disclosure, not revision. Historical reach figures are
left exactly as published.

★ THE SPLIT

You cannot tell "Anthropic's fetcher acting for a real user" from "Anthropic's
fetcher because we told it to" by User-Agent. You CAN split by PATH:

  · ORGANIC citation crawling hits CONTENT — /facilities/, /markets/, /dcpi/,
    /locations/, /press-release/, article and report pages.
  · INSTRUCTED fetching hits METADATA — /mcp, /api/v1/canon/phrases,
    /openapi.json, /.well-known/mcp.json, /llms.txt, /api/v1/reports/*,
    /api/v1/mcp/*.

A platform that reads our metadata because we asked it to is not a platform
citing us to a user. Conflating them is how "reach" became a number nobody
could defend, so the two are published separately and are NEVER summed.

★★★ AND THE ANSWER THE SPLIT GIVES IS ITSELF THE FINDING

Measured 2026-08-06 by reading the two collectors rather than the data:
organic content crawling is NOT INSTRUMENTED. It cannot appear in this
channel at all, for two independent structural reasons:

  1. The in-process Flask hook (ai_tracking._log_ai_request) logs a request
     only if `is_ai_endpoint(path)` — an allowlist (AI_ENDPOINT_PATTERNS) that
     is almost entirely API and metadata prefixes. `/facilities/<slug>` is not
     in it and never was.
  2. The Cloudflare forward (worker trackRequest -> /api/ai/track-request)
     drops anything not starting /api/, /ai/ or /mcp — see
     WORKER_TRACKED_PREFIXES below, pinned against the worker source by
     tests/test_crawler_externality_split.py.

  And independently of both: content pages are served by Cloudflare Pages and
  never reach the Flask app at all (verified 2026-08-06 — /facilities/ returns
  no `x-dc-hub-backend` header while /api/v1/ai/reach returns `railway`).

So the organic bucket read NULL — "not instrumented", never 0 — and the
honest reading of the crawler channel is that ~all of it is metadata and API
traffic. That did not mean no AI platform reads our content. It meant we had
never counted it, and the number labelled "crawler & citation requests" has
been carrying almost no citation-of-content traffic at all.

★★★ UPDATE 2026-08-29 — A THIRD COLLECTOR NOW EXISTS, AND THE BUCKET IS STILL
NULL FOR A DIFFERENT REASON.

dchub-frontend #1274 added an organic-crawl beacon to the LIVE Cloudflare
worker (`dchub-frontend/_worker.js`, v4.75.1): for an AI-platform user agent on
one of the ORGANIC_CONTENT prefixes below it fires a waitUntil POST to
/api/ai/track-request, which does NOT apply is_ai_endpoint() and therefore
accepts a content path. Reason (2) above is now addressed at the edge; reasons
(1) and (3) are unchanged and still true of the two collectors named there.

★ THE NULL HAS NOT BECOME A ZERO, and the distinction is the whole point of
this module. Measured 2026-08-29T23:10Z, ~2h after the deploy: organic_content
= null, rows_classified = 2,181. A collector existing is not a measurement.
The bucket turns into a NUMBER when a real AI crawler next reads a content
page; until then it still means "not yet observed", never "none happened".
Do not upgrade the claim without a row.

★ WORKER_SOURCE below names a DECLARED COPY IN THIS REPO, NOT THE DEPLOYED
WORKER. The live worker on dchub.cloud is the frontend's `_worker.js` — read
per-path 2026-08-29, /facilities/<slug> and /markets/<slug> both answer
`x-dc-worker-version: 4.75.1`, and that file had no tracking call at all until
#1274. cloudflare/worker-mcp-proxy-v3.8.5.js is a v3.8.5 artifact here. So
test_crawler_externality_split pins the PREFIX LIST AS DECLARED, which is worth
having, but it cannot and does not verify the deployed edge. Neither repo's CI
can read the other.

No DB import at module scope; pure SQL-string assembly over trusted hardcoded
constants (no bound params, so any literal % in an imported predicate is left
alone — see the psycopg2 empty-tuple % trap).
"""
from __future__ import annotations

# The table. ai_requests is the ONLY crawler-channel table that retains the
# path: ai_daily_stats is (date, platform, count) and ai_cumulative is
# per-platform totals — both path-less, which is why the published weekly
# reach series cannot be re-split retroactively from its own source. See
# HISTORICAL_RECLASSIFICATION below for what is and is not possible.
CRAWLER_TABLE = "ai_requests"
CRAWLER_PATH_COLUMN = "endpoint"


# ── Path buckets ────────────────────────────────────────────────────────────
# ONE ordered rule list. The pure-Python classifier and the SQL CASE are BOTH
# rendered from it, in this order, so they cannot drift into a regex twin —
# the failure this repo has paid for repeatedly (REGISTRY_CRAWLER_FAMILIES,
# 2026-07-30). A prefix lives here or it does not exist.
#
# Order defines PRECEDENCE — first matching rule wins, in both forms. It has
# no effect as written, because the three prefix sets are currently disjoint,
# and saying otherwise would be a comment describing a protection this file
# does not have. (A mutation run on 2026-08-06 reversed the CASE rendering and
# every test still passed, which is how the claim was caught.) A guard test
# asserts the disjointness, so the day someone adds an overlapping prefix — a
# bare '/reports' to the metadata set, say — the test fails and precedence
# becomes a decision made on purpose rather than one inherited.
#
# Prefixes must contain no SQL LIKE wildcards. `_` matches any single
# character in LIKE but is literal in str.startswith(), so an underscore in a
# prefix would make the two forms disagree on inputs neither author imagined.
# A guard test asserts the prohibition.
_BUCKET_RULES = (
    # ── INSTRUCTED: metadata / integration surfaces ─────────────────────
    # What a platform fetches when it has been TOLD to read about us: the
    # standing-rule targets, the discovery protocol files, the self-describing
    # manifests, and our own published reports and funnels.
    ("instructed_metadata", (
        "/mcp",
        "/.well-known/",
        "/llms.txt",
        "/llms-full.txt",
        "/AGENTS.md",
        "/agents.md",
        "/openapi.json",
        "/ai-agents.json",
        "/skill.json",
        "/skill.md",
        "/ai.txt",
        "/api/v1/canon/",
        "/api/v1/mcp/",
        "/api/v1/reports/",
        "/api/v1/ai/",
        "/api/ai/",
        "/api/v1/discovery",
        "/ai/discovery",
        "/ai/learn",
        "/api/v1/agent/",
        "/api/v1/agents/",
        "/api/v1/onboard",
        "/api/v1/citations/",
        "/api/v1/stats/",
        "/api/v1/brain/",
    )),
    # ── ORGANIC: content surfaces ───────────────────────────────────────
    # Grounded in the LIVE sitemap families (read 2026-08-06), not invented:
    # sitemap-facilities-1/2, sitemap-markets (markets + pockets), sitemap-dcpi,
    # sitemap-press, sitemap-static (locations, operators, hyperscalers,
    # answers, states, vs, for, grid, reports, partners).
    #
    # ★ Every prefix in this tuple is currently UNREACHABLE by both collectors
    # (see COLLECTOR_COVERAGE). It is declared in full anyway: the bucket must
    # exist and read null-with-a-reason, because a split that silently omits
    # the organic half would read as "we checked and there is none".
    ("organic_content", (
        "/facilities/",
        "/markets/",
        "/pockets/",
        "/dcpi/",
        "/locations/",
        "/press-release/",
        "/operators/",
        "/hyperscalers/",
        "/data-centers/",
        "/answers/",
        "/states/",
        "/for/",
        "/vs/",
        "/reports/",
        "/partners/",
        "/news",
        "/transactions",
        "/land-power",
        "/map",
    )),
    # ── AMBIGUOUS: substantive data queries ─────────────────────────────
    # Neither metadata-about-us nor a content page: a real data question,
    # which an instructed fetch and an organic agent are equally likely to
    # ask. Named for what it is and NEVER folded into either headline.
    ("ambiguous_data_api", (
        "/api/v1/facilities",
        "/api/v1/markets",
        "/api/v1/grid",
        "/api/v1/power",
        "/api/v1/fiber",
        "/api/v1/deals",
        "/api/v1/news",
        "/api/v1/search",
        "/api/facilities",
        "/api/deals",
        "/api/news",
        "/api/market",
        "/api/grid",
        "/api/site-score",
    )),
)

UNCLASSIFIED_BUCKET = "unclassified"
BUCKETS = tuple(b for b, _ in _BUCKET_RULES) + (UNCLASSIFIED_BUCKET,)

# The two headline buckets, named so a caller cannot accidentally sum them.
HEADLINE_BUCKETS = ("organic_content", "instructed_metadata")


def classify_path(path: str) -> str:
    """Pure classifier — the Python twin of path_bucket_case().

    Rendered from _BUCKET_RULES in the same order the SQL CASE evaluates, so
    the two agree by construction. A guard test asserts agreement over a
    corpus rather than trusting that sentence.
    """
    p = path or ""
    for bucket, prefixes in _BUCKET_RULES:
        for prefix in prefixes:
            if p.startswith(prefix):
                return bucket
    return UNCLASSIFIED_BUCKET


def _sql_str(value: str) -> str:
    """Single-quoted SQL literal. Trusted hardcoded constants only."""
    return "'" + str(value).replace("'", "''") + "'"


def path_bucket_case(col: str = CRAWLER_PATH_COLUMN) -> str:
    """SQL CASE assigning each row's path to a bucket.

    Built from _BUCKET_RULES — the same ordered list classify_path() walks.
    Inlined literals, no bound params.
    """
    whens = []
    for bucket, prefixes in _BUCKET_RULES:
        tests = " OR ".join(
            f"COALESCE({col}, '') LIKE {_sql_str(prefix + '%')}"
            for prefix in prefixes)
        whens.append(f"WHEN {tests} THEN {_sql_str(bucket)}")
    return ("CASE " + " ".join(whens)
            + f" ELSE {_sql_str(UNCLASSIFIED_BUCKET)} END")


# ── Collector coverage: what this channel can OBSERVE at all ────────────────
# The observation population is bounded by the collector before any query runs.
# Declaring the WHERE clause while leaving this implicit would publish a split
# whose empty half looks like a measurement.
#
# The Cloudflare worker is JavaScript and cannot be imported here, so its
# filter is recorded as a dated observation of a named file — and pinned by a
# guard test that re-reads that file, so a change there fails loudly instead of
# silently invalidating this declaration.
WORKER_SOURCE = "cloudflare/worker-mcp-proxy-v3.8.5.js :: trackRequest()"
WORKER_TRACKED_PREFIXES = ("/api/", "/ai/", "/mcp")


def collector_coverage() -> dict:
    """What the two write paths are capable of recording, from their own
    constants. AI_ENDPOINT_PATTERNS is imported, never re-typed."""
    try:
        from ai_tracking import AI_ENDPOINT_PATTERNS as _pats
        flask_gate = list(_pats)
        flask_note = ("ai_tracking.AI_ENDPOINT_PATTERNS, imported live — the "
                      "regex allowlist is_ai_endpoint() applies before any "
                      "row is written")
    except Exception:
        flask_gate = None
        flask_note = ("ai_tracking import unavailable — gate unknown, not "
                      "assumed open")
    return {
        "flask_in_process_hook": {
            "gate": flask_gate,
            "note": flask_note,
        },
        "cloudflare_forward": {
            "gate": list(WORKER_TRACKED_PREFIXES),
            "source": WORKER_SOURCE,
            "note": ("a pathname matching none of these prefixes is dropped "
                     "before it reaches /api/ai/track-request; recorded from "
                     "the worker source on 2026-08-06 and pinned by "
                     "tests/test_crawler_externality_split.py. NOTE this "
                     "names a DECLARED COPY in the backend repo, not the "
                     "deployed edge — the live worker is the frontend's "
                     "_worker.js, which since 2026-08-29 also beacons "
                     "AI-platform hits on the organic content prefixes"),
        },
        "content_pages_reach_neither": (
            "content surfaces are served by Cloudflare Pages and never reach "
            "the Flask app — verified 2026-08-06: /facilities/ returns no "
            "x-dc-hub-backend header while /api/v1/ai/reach returns railway. "
            "Still true of these two collectors; see edge_organic_beacon"),
        "edge_organic_beacon": (
            "since 2026-08-29 the LIVE Cloudflare worker (the frontend's "
            "_worker.js, not the declared copy named above) POSTs "
            "AI-platform hits on the organic content prefixes to "
            "/api/ai/track-request, which does not apply is_ai_endpoint(). "
            "This is the first collector that can see a content-page crawl"),
        "consequence": (
            "organic content crawling was NOT INSTRUMENTED until 2026-08-29, "
            "and the bucket IS NOW A NUMBER: the edge beacon has recorded "
            "content-page crawls continuously since. ★ Two consequences that "
            "outlive the fix. (1) There is NO PRE-2026-08-29 BASELINE for this "
            "bucket — any comparison spanning that date measures the instrument "
            "turning on, not the crawler. The published reach series showed "
            "+1,419.8% WoW on the first full week (34,189 vs 2,250) and that "
            "number is an artefact, not growth. (2) A ZERO over a window "
            "reaching back before 2026-08-29 still means unknown, not none; "
            "the route publishes collector_started / partial_window on the "
            "bucket so a ?days=90 read cannot be mistaken for a ?days=7 one."),
    }


# ── The executed filters ────────────────────────────────────────────────────
# Same rule as routes/canonical_benchmarks._p50_filters() (#2253): the
# published sql_filters must BE the list the query joins into its WHERE, so an
# edit to one is an edit to both. A hand-written description of a filter is a
# second source of truth, and second sources drift.

def _roster_platform_list() -> str:
    """SQL IN-list of the platforms the public crawler roster publishes.

    Imported from ai_tracking.AI_PLATFORMS — the same curated allowlist
    main._is_real_ai_platform() gates the roster, the /ai chart and
    /api/v1/ai-tracking/stats on. Re-typing it here would create a second
    roster that drifts from the one on the page.
    """
    from ai_tracking import AI_PLATFORMS
    return ",".join(_sql_str(k) for k in sorted(AI_PLATFORMS))


def window_clause(days: int = 7, calendar: bool = False) -> str:
    """The time bound, in one of two shapes.

    rolling  — `days` × 24h ending NOW. The headline window.
    calendar — the same `days` CALENDAR DATES the path-less counter buckets
               by, so the cross-check compares like with like. For days=7
               that is D-6..D inclusive: seven dates, matching
               `date > CURRENT_DATE - 7` in published_series_sql().
    """
    if calendar:
        return f"created_at >= CURRENT_DATE - {int(days) - 1}"
    return f"created_at >= NOW() - INTERVAL '{int(days)} days'"


def crawler_filters(days: int = 7, calendar: bool = False) -> list[str]:
    """Every WHERE clause applied to the crawler-split population, in order.

    Returned as a list so it can be BOTH joined into the query and published
    verbatim. Inlined literals only — callers must pass no bound params.

    `calendar` swaps ONLY the time clause. The exclusions are the same objects
    either way, by construction, so the cross-check can never be computed over
    a differently-filtered population than the headline — a guard test asserts
    the two lists differ in exactly one element.
    """
    from mcp_calls_deloop import real_ua_predicate
    return [
        window_clause(days, calendar),
        f"LOWER(COALESCE(platform, '')) IN ({_roster_platform_list()})",
        real_ua_predicate("user_agent"),
    ]


# Why external_platform_predicate() is NOT in that list, stated so nobody
# "completes" it later: on mcp_call_log the `platform` column is a tag the
# CLIENT declares, so it needs an exclusion list. Here it is the output of
# ai_tracking.detect_platform(), a closed allowlist of 21 curated vendor keys —
# and the filter above admits ONLY those keys. That is strictly stronger than
# excluding internal families from an open string space, and the guard test
# proves the containment (every AI_PLATFORMS key passes
# external_platform_predicate) rather than asserting it in prose.
EXTERNAL_PREDICATE_RATIONALE = (
    "external_platform_predicate is not applied: this table's platform column "
    "is a detector output from a closed 21-key allowlist, and the roster "
    "filter admits only those keys — strictly stronger than excluding "
    "internal families from an open tag space. Proven by a guard test, not "
    "asserted. real_ua_predicate IS applied: a probe of ours carrying a "
    "platform Referer can be attributed to a real vendor by detect_platform, "
    "and its user-agent is what gives it away.")


HISTORICAL_RECLASSIFICATION = (
    "PARTIAL, and it is not a revision. ai_requests retains endpoint + "
    "created_at, so the split can be computed over any window that table "
    "covers — this endpoint does exactly that, including for past days. What "
    "CANNOT be rebuilt is the published weekly reach series itself: it is "
    "served from ai_cumulative.requests_7d and ai_daily_stats, both of which "
    "store (platform, count) with no path, so the historical numbers cannot "
    "be re-split at their own source and are deliberately left untouched. The "
    "split is a parallel view over a different table; it will not reconcile "
    "exactly with the published series (see cross_check) and must never be "
    "presented as a correction of it.")


def crawler_population(days: int = 7) -> dict:
    """What is counted, in prose and in the exact SQL that counts it."""
    return {
        "statistic": "row count per path bucket",
        "observations": (
            "crawler & citation HTTP requests attributed to a rostered AI "
            "platform by User-Agent (+ Referer) — NOT agent tool calls, which "
            "arrive through the MCP gateway and never touch this table"),
        "window": f"{int(days)} days, rolling, ending now",
        "source": CRAWLER_TABLE,
        "buckets": list(BUCKETS),
        "never_summed": (
            "organic_content and instructed_metadata answer different "
            "questions and are never added into one 'reach' figure. "
            "ambiguous_data_api is neither and is folded into neither."),
        "includes": (
            "the 21 curated vendor keys in ai_tracking.AI_PLATFORMS — the "
            "same allowlist the public /ai roster renders"),
        "excludes": (
            "our own internal/probe buckets (they are not rostered "
            "platforms), and rostered rows whose user-agent is a "
            "scripting/self UA. " + EXTERNAL_PREDICATE_RATIONALE),
        "not_excluded": (
            "Referer is NOT retained by this channel — ai_requests has no "
            "referer column — so requests referred from our own surfaces "
            "cannot be excluded, retroactively or otherwise. "
            "detect_platform() already DISCARDS a dchub.cloud Referer when "
            "attributing a platform, so a browser on our own page is never "
            "counted as that vendor; that is a different guarantee from "
            "excluding the request, and it is the weaker one."),
        "why_it_matters": (
            "before 2026-08-06 this channel had NO exclusion of any kind, "
            "inside the codebase or out. Same class as the execute_plan p50 "
            "(#2252) that turned out to be ~80% our own probes and read 786ms "
            "faster than the truth — bigger surface."),
        "sql_filters": crawler_filters(days),
        "collector_coverage": collector_coverage(),
        "historical_reclassification": HISTORICAL_RECLASSIFICATION,
    }


def bucket_counts_sql(days: int = 7, by_platform: bool = False) -> str:
    """Row counts per bucket over the declared population.

    The WHERE is `" AND ".join(crawler_filters(days))` — the same list
    crawler_population() publishes as sql_filters.
    """
    case = path_bucket_case()
    where = " AND ".join(crawler_filters(days))
    if by_platform:
        return ("SELECT LOWER(COALESCE(platform, '')) AS platform, "
                f"({case}) AS bucket, COUNT(*) AS n "
                f"FROM {CRAWLER_TABLE} WHERE {where} "
                "GROUP BY 1, 2 ORDER BY 3 DESC")
    return (f"SELECT ({case}) AS bucket, COUNT(*) AS n "
            f"FROM {CRAWLER_TABLE} WHERE {where} "
            "GROUP BY 1 ORDER BY 2 DESC")


def coverage_sql(days: int = 7) -> str:
    """Earliest/latest classifiable row + total, so a reader can see how far
    back the reclassifiable history actually goes instead of taking a claim
    about it on trust."""
    where = " AND ".join(crawler_filters(days))
    return ("SELECT COUNT(*) AS rows_in_window, MIN(created_at) AS earliest, "
            f"MAX(created_at) AS latest FROM {CRAWLER_TABLE} WHERE {where}")


def table_span_sql() -> str:
    """Full span of the path-retaining table, unfiltered by window — the
    honest bound on how much history could ever be re-split."""
    return (f"SELECT MIN(created_at) AS earliest, MAX(created_at) AS latest, "
            f"COUNT(*) AS rows FROM {CRAWLER_TABLE}")


def aligned_rows_sql(days: int = 7) -> str:
    """ai_requests rows over the SAME calendar dates the path-less counter
    sums, with the SAME exclusions — the only comparison that means anything.
    """
    where = " AND ".join(crawler_filters(days, calendar=True))
    return f"SELECT COUNT(*) AS n FROM {CRAWLER_TABLE} WHERE {where}"


def published_series_sql(days: int = 7) -> str:
    """The path-LESS counter the public weekly reach is served from, over the
    same platforms — published beside the split so the gap between the two is
    visible rather than assumed away.

    ★ 2026-08-06, measured after shipping: this clause was
    `date >= CURRENT_DATE - days`, which spans days+1 CALENDAR DATES (D-7..D
    for days=7), and it was being compared against a days×24h ROLLING count.
    Live, that published delta = -3,544 on a 20,540-row split. A reader saw
    -15% and would reasonably conclude the split was losing traffic. It was
    not: measuring ai_requests over 8 rolling days returned 24,639 against the
    counter's 24,084 — the extra calendar day alone accounts for +4,099 of the
    gap, and once the windows are aligned the two counters agree to +555
    (~2%), with ai_requests slightly AHEAD.
    So the number was not wrong, it was uninterpretable, which on a disclosure
    surface is the same defect wearing a different hat. `>` gives exactly
    `days` dates (D-6..D for days=7), matching window_clause(calendar=True).
    """
    return ("SELECT COALESCE(SUM(request_count), 0) AS n FROM ai_daily_stats "
            f"WHERE date > CURRENT_DATE - {int(days)} "
            f"AND LOWER(COALESCE(platform, '')) IN ({_roster_platform_list()})")
