"""Guards on the crawler-channel externality split.

The value of this split is that a reader can reproduce every verdict from what
we publish. These tests keep the failure modes that would destroy that:

  1. The SQL classifier and the Python classifier drifting apart — the
     regex-twin defect this repo has paid for more than once
     (REGISTRY_CRAWLER_FAMILIES, 2026-07-30, where the LIKE form gained nine
     families and the regex form kept serving the old verdict).
  2. A published population that DESCRIBES filters instead of BEING them
     (#2253), so an edit to the query silently stops matching the disclosure.
  3. A bucket rendering 0 where nothing was measured — and, since 2026-09-05,
     the inverse: a bucket withholding a real count. organic_content HAS been
     instrumented since 2026-08-29 (the edge beacon), and this endpoint went on
     publishing "no collector can record this" over a 28,706-row count for a
     week, because the withholding tuple was hand-listed in this repo while the
     collector shipped in another. Both directions are tested.
  4. The Cloudflare worker's tracking filter changing under us. It is
     JavaScript and cannot be imported, so the declared copy is pinned against
     the worker source here — a change there fails loudly instead of silently
     invalidating the coverage declaration.

Assertions that scan text run against COMMENT-STRIPPED source: three separate
tests in this repo have passed by matching the comment that explained the bug
they were meant to catch.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODULE_SRC = ROOT / "crawler_externality.py"
ROUTE_SRC = ROOT / "routes" / "crawler_split.py"
WORKER_SRC = ROOT / "cloudflare" / "worker-mcp-proxy-v3.8.5.js"


def _stripped(path: pathlib.Path) -> str:
    """Source with comments and docstrings removed."""
    raw = path.read_text(encoding="utf-8")
    no_comments = re.sub(r"(?m)#.*$", "", raw)
    tree = ast.parse(raw)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                no_comments = no_comments.replace(doc, "")
    return no_comments


def test_modules_parse_and_names_resolve():
    """An empty or gutted module would pass every text scan below."""
    for src, expected in (
        (MODULE_SRC, {"classify_path", "path_bucket_case", "crawler_filters",
                      "crawler_population", "bucket_counts_sql",
                      "collector_coverage"}),
        (ROUTE_SRC, {"crawler_split", "_bucket_result", "_split_payload"}),
    ):
        tree = ast.parse(src.read_text(encoding="utf-8"))
        funcs = {n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert expected <= funcs, (
            f"{src.name}: expected functions missing — module may have been "
            f"gutted: {sorted(funcs)}")


# ── 1. the two classifiers cannot drift ─────────────────────────────────────

_CORPUS = (
    # instructed / metadata
    "/mcp",
    "/mcp/",
    "/.well-known/mcp.json",
    "/llms.txt",
    "/openapi.json",
    "/AGENTS.md",
    "/api/v1/canon/phrases",
    "/api/v1/mcp/funnel",
    "/api/v1/mcp/handoff-funnel",
    "/api/v1/reports/canonical-benchmarks",
    "/api/v1/ai/reach",
    "/api/v1/onboard",
    "/api/v1/agent/cookbook",
    "/api/v1/brain/mcp-registries",
    "/api/v1/citations/by-agent",
    # organic / content
    "/facilities/equinix-dc1-ashburn-va",
    "/markets/northern-virginia",
    "/pockets/loudoun-county",
    "/dcpi/ashburn-va",
    "/locations/ashburn-va",
    "/press-release/some-headline",
    "/operators/equinix",
    "/hyperscalers/aws",
    "/data-centers/foo",
    "/answers/what-is-a-hyperscaler",
    "/states/virginia",
    "/for/developers",
    "/vs/datacenterhawk",
    "/news",
    "/transactions",
    "/map",
    "/land-power",
    # ambiguous data api
    "/api/v1/facilities/search",
    "/api/v1/markets/rank",
    "/api/v1/grid/status",
    "/api/facilities",
    "/api/site-score",
    # unclassified
    "/",
    "/pricing",
    "/api/v2/something-new",
    "/robots.txt",
    "",
)


def _sqlite_case_eval(case_sql: str, value: str) -> str:
    """Evaluate the rendered SQL CASE for one path, using SQLite.

    SQLite's LIKE has the same %/_ wildcard semantics as Postgres for these
    patterns, so it is a faithful stand-in and needs no database server. This
    exercises the ACTUAL rendered SQL string rather than re-implementing what
    we think it means — the only version of this test that can catch a
    rendering bug.
    """
    import sqlite3
    con = sqlite3.connect(":memory:")
    try:
        cur = con.cursor()
        cur.execute("CREATE TABLE t (endpoint TEXT)")
        cur.execute("INSERT INTO t VALUES (?)", (value,))
        cur.execute(f"SELECT ({case_sql}) FROM t")
        return cur.fetchone()[0]
    finally:
        con.close()


def test_sql_and_python_classifiers_agree():
    """The SQL CASE and classify_path() must return the same bucket.

    They are rendered from one ordered rule list; this proves the rendering,
    not the intention.
    """
    from crawler_externality import classify_path, path_bucket_case

    case_sql = path_bucket_case("endpoint")
    for path in _CORPUS:
        py = classify_path(path)
        sql = _sqlite_case_eval(case_sql, path)
        assert py == sql, (
            f"classifier drift on {path!r}: python={py} sql={sql}")


def test_corpus_actually_exercises_every_bucket():
    """A corpus that only ever hits one bucket would pass the agreement test
    while proving nothing. Pin that all four are covered."""
    from crawler_externality import BUCKETS, classify_path

    seen = {classify_path(p) for p in _CORPUS}
    assert set(BUCKETS) <= seen, (
        f"corpus never exercises {set(BUCKETS) - seen} — the agreement test "
        "above is weaker than it looks")


def test_prefixes_carry_no_like_wildcards():
    """`_` matches any single character in LIKE but is literal in
    str.startswith(). A prefix containing one would make the two forms
    disagree on inputs neither author imagined."""
    from crawler_externality import _BUCKET_RULES

    for bucket, prefixes in _BUCKET_RULES:
        for prefix in prefixes:
            assert "%" not in prefix and "_" not in prefix, (
                f"{bucket}: prefix {prefix!r} contains a LIKE wildcard — the "
                "SQL and Python classifiers would diverge")
            assert prefix.startswith("/"), (
                f"{bucket}: prefix {prefix!r} is not path-anchored")


def test_bucket_prefixes_are_disjoint():
    """No path may match two buckets.

    A mutation run on 2026-08-06 reversed the CASE rendering order and every
    test still passed — because the sets are disjoint, precedence never fires,
    and a comment claiming order was "load-bearing" was describing a
    protection that did not exist. Rather than keep a vacuous ordering test,
    pin the property that actually holds. The day someone adds an overlapping
    prefix — a bare '/reports' to the metadata set, say — this fails, and
    precedence becomes a decision made on purpose.
    """
    from crawler_externality import _BUCKET_RULES

    rules = [(b, list(p)) for b, p in _BUCKET_RULES]
    for i, (b1, p1) in enumerate(rules):
        for b2, p2 in rules[i + 1:]:
            for a in p1:
                for c in p2:
                    assert not (a.startswith(c) or c.startswith(a)), (
                        f"{b1}:{a!r} overlaps {b2}:{c!r} — a path can now "
                        "match two buckets, so rule ORDER silently decides "
                        "which. Decide it deliberately and update this test.")


def test_metadata_and_content_reports_do_not_collide():
    """/api/v1/reports/* is metadata about our own service; /reports/ is a
    content page. Both exist, and conflating them is exactly the mistake this
    split exists to prevent."""
    from crawler_externality import classify_path

    assert classify_path("/api/v1/reports/monthly") == "instructed_metadata"
    assert classify_path("/reports/state-of-the-market") == "organic_content"


# ── 2. the published population IS the executed query ───────────────────────

def test_published_population_is_the_query_that_ran():
    """The declared filters must BE the list the query joins into its WHERE.

    A hand-written description of a filter is a second source of truth, and
    second sources drift — that is how a docstring came to claim "real
    external calls" over a population with no externality filter at all
    (#2253).
    """
    from crawler_externality import (bucket_counts_sql, coverage_sql,
                                     crawler_filters, crawler_population)

    filters = crawler_filters(7)
    pop = crawler_population(7)
    assert pop["sql_filters"] == filters, (
        "the published filters are not the executed filters")
    assert len(filters) >= 3, "a filter was dropped from the population"

    where = " AND ".join(filters)
    for sql in (bucket_counts_sql(7), coverage_sql(7)):
        assert where in sql, (
            "the executed query does not contain the published WHERE — the "
            "declaration and the query have separated")

    src = _stripped(MODULE_SRC)
    assert '" AND ".join(crawler_filters(days))' in src, (
        "the WHERE must be joined from crawler_filters() so an edit to one is "
        "an edit to both")


def test_population_travels_with_every_outcome():
    """A number published without its population is the defect this whole
    change is about. The endpoint must attach it on the happy path AND on
    every failure path."""
    src = _stripped(ROUTE_SRC)
    assert src.count("crawler_population(days)") >= 1, (
        "the route never builds the population declaration")
    # The skeleton assembled before any DB work already carries it, so a
    # db-unavailable early return cannot ship a bare payload.
    assert re.search(r'"population":\s*crawler_population\(days\)', src), (
        "the population is not part of the payload skeleton — a degraded "
        "response could publish buckets with no declaration")


def test_ua_exclusion_is_imported_not_retyped():
    """The externality verdict must come from the canonical de-loop module.

    A second hand-maintained exclusion list is how regex twins drift apart.
    Checked behaviourally against the real filter list so it survives a
    refactor of how the SQL is assembled.
    """
    from mcp_calls_deloop import real_ua_predicate
    from crawler_externality import crawler_filters

    src = _stripped(MODULE_SRC)
    assert "from mcp_calls_deloop import" in src, (
        "predicates must be imported from the canonical de-loop module")
    assert real_ua_predicate("user_agent") in crawler_filters(7), (
        "the user-agent exclusion never reaches the query")


def test_roster_allowlist_is_strictly_stronger_than_the_platform_predicate():
    """crawler_externality deliberately does NOT apply
    external_platform_predicate, on the grounds that admitting only the closed
    AI_PLATFORMS allowlist is strictly stronger. That is a claim, so prove it:
    every rostered key must itself pass the canonical predicate. If someone
    adds a vendor key that would have been excluded as internal — 'mcp-health',
    say — this fails rather than letting the omission become a hole.
    """
    import sqlite3

    from ai_tracking import AI_PLATFORMS
    from mcp_calls_deloop import external_platform_predicate

    pred = external_platform_predicate("platform")
    con = sqlite3.connect(":memory:")
    try:
        cur = con.cursor()
        cur.execute("CREATE TABLE t (platform TEXT)")
        for key in AI_PLATFORMS:
            cur.execute("DELETE FROM t")
            cur.execute("INSERT INTO t VALUES (?)", (key,))
            cur.execute(f"SELECT ({pred}) FROM t")
            assert cur.fetchone()[0], (
                f"rostered platform {key!r} would be EXCLUDED by "
                "external_platform_predicate — the allowlist is no longer "
                "strictly stronger, so omitting that predicate is now a hole")
    finally:
        con.close()

    # And the roster IN-list must actually be built from that same allowlist.
    from crawler_externality import crawler_filters
    joined = " AND ".join(crawler_filters(7))
    for key in AI_PLATFORMS:
        assert f"'{key}'" in joined, (
            f"rostered platform {key!r} missing from the executed filter — "
            "the split's population disagrees with the page's roster")


# ── 3. no bucket ever renders a flattering zero ─────────────────────────────

def test_organic_content_reports_its_count_now_that_a_collector_exists():
    """★ REGRESSION GUARD for the 2026-09-05 defect.

    organic_content was hand-listed in _NOT_INSTRUMENTED, and _bucket_result()
    returns on that tuple BEFORE reading `counts`. So the endpoint computed the
    bucket correctly and then threw it away, publishing "no collector on this
    channel can record a content-page crawl" over a real 28,706-row count —
    during the largest content crawl the site has ever received.

    The edge beacon (dchub-frontend #1274, 2026-08-29) is the collector. This
    test fails if anything ever puts organic_content back behind the tuple.
    """
    from routes.crawler_split import _bucket_result, _NOT_INSTRUMENTED

    assert "organic_content" not in _NOT_INSTRUMENTED, (
        "organic_content has had a collector since 2026-08-29 — withholding it "
        "publishes a false reason over a real count")

    res = _bucket_result({"organic_content": 28706, "instructed_metadata": 1114},
                         "organic_content", 34167, window_days=7,
                         today=__import__("datetime").date(2026, 11, 1))
    assert res["requests"] == 28706, (
        "a bucket with a live collector and rows must publish its count")
    assert res["reason"] is None


def test_withholding_mechanism_still_works_for_a_genuinely_blind_bucket():
    """_NOT_INSTRUMENTED is empty, not deleted. The mechanism must still be
    able to withhold, or the next unobservable bucket renders a confident 0 and
    nothing catches it. Exercised against a bucket forced into the tuple."""
    import routes.crawler_split as cs

    prev = cs._NOT_INSTRUMENTED
    cs._NOT_INSTRUMENTED = ("ambiguous_data_api",)
    try:
        res = cs._bucket_result({"ambiguous_data_api": 999}, "ambiguous_data_api",
                                999, window_days=7)
        assert res["requests"] is None, (
            "the withholding mechanism no longer withholds — an unobservable "
            "bucket would publish a number it cannot defend")
        assert "not instrumented" in res["reason"]
    finally:
        cs._NOT_INSTRUMENTED = prev


def test_zero_over_a_window_predating_the_collector_reads_as_unknown():
    """A 0 for organic_content is ambiguous in a way other buckets' zeros are
    not: ?days=90 reaches back before the beacon existed. The reason must say
    so, or a 90-day read gets quoted as "no content crawling"."""
    from routes.crawler_split import _bucket_result

    res = _bucket_result({"instructed_metadata": 900}, "organic_content", 900,
                         window_days=90)
    assert res["requests"] is None
    assert "2026-08-29" in res["reason"], (
        "an empty organic bucket must name the collector's start date — "
        "otherwise unknown is indistinguishable from none")


def test_a_count_over_a_window_predating_the_collector_is_labelled_a_floor():
    """?days=90 DOES return organic rows (the instrumented tail of the window).
    That number is a floor for the window, not a measurement of it, and it is
    not comparable to buckets that were instrumented throughout."""
    from routes.crawler_split import _bucket_result

    import datetime as dt
    # PINNED, never date.today(): a caveat that fires on "7 days ago" is true in
    # September 2026 and false in October, and a test asserting either would
    # start failing on a date nobody chose. See feedback on wall-clock in tests.
    pinned = dt.date(2026, 11, 1)

    res = _bucket_result({"organic_content": 28706}, "organic_content", 34167,
                         window_days=90, today=pinned)
    assert res["requests"] == 28706
    assert "partial_window" in res, (
        "a window opening before the collector must be labelled, or the count "
        "reads as covering the whole window")
    assert "FLOOR" in res["partial_window"]

    short = _bucket_result({"organic_content": 28706}, "organic_content", 34167,
                           window_days=7, today=pinned)
    assert "partial_window" not in short, (
        "a fully-instrumented window must NOT carry the caveat, or the label "
        "is noise and gets ignored where it matters")


def test_buckets_reconcile_names_a_dropped_bucket():
    """★ The durable guard. The buckets PARTITION the classified population
    (path_bucket_case ends in ELSE 'unclassified'), so buckets_sum must equal
    rows_classified. When organic_content was withheld the two differed by
    28,706 and NOTHING in the payload said so — a reader could only find it by
    summing the buckets by hand. This is what makes the next one loud."""
    from routes.crawler_split import _buckets_reconcile

    balanced = _buckets_reconcile(
        {"organic_content": 28706, "ambiguous_data_api": 2372,
         "instructed_metadata": 1114, "unclassified": 1975}, 34167)
    assert balanced["delta"] == 0, (
        "the four buckets partition the population — a non-zero delta on a "
        "complete count map means the reconciliation itself is wrong")
    assert balanced["buckets_sum"] == 34167

    leaking = _buckets_reconcile(
        {"ambiguous_data_api": 2372, "instructed_metadata": 1114,
         "unclassified": 1975}, 34167)
    assert leaking["delta"] == -28706, (
        "a bucket missing from the payload must surface as a non-zero delta")


def test_empty_bucket_is_null_not_zero():
    """A measurable bucket with no rows is still null-with-a-reason."""
    from routes.crawler_split import _bucket_result

    res = _bucket_result({"instructed_metadata": 12}, "ambiguous_data_api", 12)
    assert res["requests"] is None
    assert res["reason"]

    unavailable = _bucket_result(None, "instructed_metadata", None)
    assert unavailable["requests"] is None
    assert "unknown, not zero" in unavailable["reason"], (
        "a failed query must report UNKNOWN, not a count")


def test_a_measured_bucket_publishes_its_count():
    """The guard must not withhold everything — a bucket with rows reports
    them, or the endpoint is useless and the tests above are vacuous."""
    from routes.crawler_split import _bucket_result

    res = _bucket_result({"instructed_metadata": 7194}, "instructed_metadata",
                         7194)
    assert res["requests"] == 7194
    assert res["reason"] is None


def test_cross_check_compares_identical_calendar_windows():
    """Both sides of the cross-check must cover the SAME calendar dates.

    Measured in prod 2026-08-06 after shipping: the counter side used
    `date >= CURRENT_DATE - days` (days+1 DATES) and was compared against a
    days x 24h ROLLING count. Published delta: -3,544 on a 20,540-row split.
    A reader sees -15% and concludes the split is losing traffic. It is not —
    ai_requests over 8 rolling days returned 24,639 against the counter's
    24,084, so one extra calendar date accounted for +4,099 of the gap and the
    aligned counters agree to +555. An uninterpretable number on a disclosure
    surface is the same defect as a wrong one.
    """
    from crawler_externality import (aligned_rows_sql, published_series_sql,
                                     window_clause)

    days = 7
    # ai_daily_stats: `> CURRENT_DATE - 7` == dates D-6..D == 7 dates.
    assert f"date > CURRENT_DATE - {days}" in published_series_sql(days), (
        "the counter side must span exactly `days` calendar dates; `>=` spans "
        "days+1 and reintroduces the skew")
    # ai_requests: from midnight of D-6 — the same first date.
    assert f"created_at >= CURRENT_DATE - {days - 1}" in aligned_rows_sql(days)
    assert window_clause(days, calendar=True) in aligned_rows_sql(days)

    # And the rolling window must NOT be what the delta is computed from.
    assert window_clause(days, calendar=False) not in aligned_rows_sql(days), (
        "the aligned comparison is using the rolling window — that is the "
        "skew this test exists to prevent")


def test_both_windows_share_one_exclusion_list():
    """The calendar variant may differ from the rolling one in the TIME clause
    and nothing else.

    If the cross-check ever ran over a differently-filtered population, its
    delta would silently mix window skew with filter drift — and a reader
    would have no way to tell which they were looking at.
    """
    from crawler_externality import crawler_filters

    rolling = crawler_filters(7, calendar=False)
    calendar = crawler_filters(7, calendar=True)
    assert len(rolling) == len(calendar)
    assert rolling[0] != calendar[0], "the time clause should differ"
    assert rolling[1:] == calendar[1:], (
        "the exclusions differ between the rolling and calendar windows — the "
        "cross-check delta would conflate filter drift with window skew")


def test_cross_check_warns_against_the_wrong_comparison():
    """The payload still carries the rolling count next to the calendar sum.
    That juxtaposition is exactly the mistake that shipped, so the note must
    tell a reader not to subtract those two."""
    src = _stripped(ROUTE_SRC)
    assert "rows_classified_rolling" in src, (
        "the rolling count must be named distinctly from the aligned one")
    assert "compared_on" in src, (
        "the payload must state which window the delta was computed over")


def test_no_optional_block_fails_into_a_bare_null():
    """A null with no reason is indistinguishable from an empty measurement.

    Caught in prod on 2026-08-06 while verifying #2295: at days=30 the grouped
    per-platform scan exceeds the statement timeout, the handler set
    `by_platform: null` with no explanation, and a consumer read that as
    "claude: 0 instructed, 0 data" for a window whose 7d subset held 6,779.
    That is this endpoint's own defect, one level up. Every `except` that
    empties a published block must attach a reason.
    """
    src = ROUTE_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)

    offenders = []
    for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)):
        # Collect the keys this handler assigns into `out`.
        assigned = set()
        for node in ast.walk(handler):
            if not isinstance(node, ast.Assign):
                continue
            for tgt in node.targets:
                if (isinstance(tgt, ast.Subscript)
                        and isinstance(tgt.value, ast.Name)
                        and tgt.value.id == "out"
                        and isinstance(tgt.slice, ast.Constant)):
                    assigned.add((tgt.slice.value, node.value))
        for key, value in assigned:
            if key in ("degraded", "buckets", "rows_classified"):
                continue  # covered by their own tests / carry reasons already
            # A bare `out[key] = None` is the defect. A dict is fine only if
            # it carries a reason-ish field.
            if isinstance(value, ast.Constant) and value.value is None:
                reason_keys = {k for k, _ in assigned}
                if f"{key}_reason" not in reason_keys:
                    offenders.append(key)
            elif isinstance(value, ast.Dict):
                keys = {k.value for k in value.keys
                        if isinstance(k, ast.Constant)}
                if not (keys & {"note", "reason"}):
                    offenders.append(key)

    assert not offenders, (
        f"these blocks fail into a null/empty with no stated reason: "
        f"{sorted(set(offenders))} — a reader cannot tell 'we could not "
        "measure' from 'we measured nothing'")


def test_headline_buckets_are_never_summed_in_code():
    """The two headline buckets answer different questions. Summing them
    rebuilds the undefendable figure."""
    src = _stripped(ROUTE_SRC) + _stripped(MODULE_SRC)
    assert not re.search(
        r"organic_content.*\+.*instructed_metadata", src), (
        "organic and instructed are being added together somewhere")


# ── 4. the cross-language coverage declaration is pinned ────────────────────

def test_worker_tracking_filter_matches_the_declared_copy():
    """The CF worker's trackRequest() filter bounds what this channel can
    observe, and it is JavaScript we cannot import. Re-read it and compare, so
    a change there fails HERE instead of silently invalidating the published
    coverage declaration.
    """
    from crawler_externality import WORKER_TRACKED_PREFIXES

    assert WORKER_SRC.exists(), (
        f"{WORKER_SRC} is gone — the coverage declaration names a file that "
        "no longer exists and can no longer be checked")
    js = WORKER_SRC.read_text(encoding="utf-8")
    match = re.search(r"function trackRequest\([^)]*\)\s*\{(.*?)\n\}", js,
                      re.S)
    assert match, "trackRequest() not found in the worker source"
    body = match.group(1)
    found = set(re.findall(r"pathname\.startsWith\('([^']+)'\)", body))
    assert found == set(WORKER_TRACKED_PREFIXES), (
        f"worker tracking filter changed: source has {sorted(found)}, "
        f"crawler_externality declares {sorted(WORKER_TRACKED_PREFIXES)}. "
        "Update WORKER_TRACKED_PREFIXES and re-check whether organic content "
        "paths are now instrumented.")


def test_organic_paths_are_unreachable_by_both_collectors():
    """The load-bearing claim behind the null organic bucket, checked against
    both collectors' real constants rather than asserted in a comment.

    If either gate is ever widened to admit content paths, this fails — which
    is the signal to start publishing a real organic number.
    """
    from ai_tracking import is_ai_endpoint
    from crawler_externality import (WORKER_TRACKED_PREFIXES, _BUCKET_RULES,
                                     classify_path)

    organic = dict(_BUCKET_RULES)["organic_content"]
    samples = [p + "some-slug" if p.endswith("/") else p for p in organic]
    for path in samples:
        assert classify_path(path) == "organic_content", (
            f"{path} is no longer classified organic — fix the sample, not "
            "the assertion")
        assert not is_ai_endpoint(path), (
            f"{path} now PASSES the Flask collector gate — organic traffic is "
            "instrumented and the null bucket is no longer honest")
        assert not any(path.startswith(pre)
                       for pre in WORKER_TRACKED_PREFIXES), (
            f"{path} now passes the worker forward filter — organic traffic "
            "is instrumented and the null bucket is no longer honest")


def test_coverage_declaration_imports_the_flask_gate():
    """The Flask half of the coverage declaration must be the live constant,
    not a copy — that half CAN be imported, so there is no excuse."""
    from ai_tracking import AI_ENDPOINT_PATTERNS
    from crawler_externality import collector_coverage

    gate = collector_coverage()["flask_in_process_hook"]["gate"]
    assert gate == list(AI_ENDPOINT_PATTERNS), (
        "the published Flask gate is not ai_tracking.AI_ENDPOINT_PATTERNS")


def test_historical_numbers_are_not_revised():
    """The fix is disclosure, not revision. Nothing here may UPDATE or DELETE
    the published counters."""
    src = _stripped(MODULE_SRC) + _stripped(ROUTE_SRC)
    for verb in ("UPDATE ai_cumulative", "DELETE FROM ai_", "UPDATE ai_daily"):
        assert verb not in src, (
            f"{verb!r} present — historical reach figures must be left "
            "exactly as published")


# ── the null must not quietly become a zero (2026-08-29) ──────────────────
def test_coverage_names_the_beacon_and_the_missing_baseline():
    """dchub-frontend #1274 added the first collector that can see a
    content-page crawl, and it is now recording. The claim that must survive is
    the one that outlives the fix: there is NO pre-2026-08-29 baseline, so any
    comparison spanning that date measures the instrument, not the crawler.

    This test fails if the module drops the beacon and reverts to "nothing can
    see this", or if it starts presenting the cross-2026-08-29 jump as growth.
    """
    from crawler_externality import collector_coverage
    cov = collector_coverage()
    assert "edge_organic_beacon" in cov, (
        "the third collector is undocumented — a reader would conclude "
        "organic crawling is still structurally impossible")
    consequence = cov["consequence"]
    assert "2026-08-29" in consequence, (
        "the consequence must name the date the instrument was born — it is "
        "the boundary every comparison has to respect")
    assert "BASELINE" in consequence.upper(), (
        "the missing baseline is the durable finding; without it the WoW step "
        "gets read as demand growth")
    assert "unknown, not none" in consequence.lower(), (
        "a zero over a window predating the collector is still not a measured "
        "zero, and the coverage declaration is where a reader learns that")


def test_worker_source_is_labelled_as_a_declared_copy():
    """WORKER_SOURCE names a file in THIS repo that is not the deployed edge —
    the live worker is the frontend's _worker.js (x-dc-worker-version 4.75.1,
    read per-path 2026-08-29). The pin is still worth having, but it must not
    read as verification of production."""
    from crawler_externality import collector_coverage
    cov = collector_coverage()
    note = cov["cloudflare_forward"]["note"]
    assert "DECLARED COPY" in note or "declared copy" in note.lower(), (
        "a pin against a non-deployed file must say so, or it reads as a "
        "guarantee about the live edge")
