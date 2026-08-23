"""
tests/test_page_usage_audience_split.py — the page-usage join must not count
robots as people, and must not count us as demand (2026-08-23).

WHY THIS FILE EXISTS. `routes/page_usage.py` answers "which of our pages is
nobody using" by joining the sitemap against Cloudflare edge requests. The
entire value of that answer rests on one function — `classify_ua` — and that
function has two failure modes that are both silent and both flattering:

  1. ★ CLASSIFIER ORDER IS LOAD-BEARING. Crawlers do not announce themselves
     as robots; they announce themselves as browsers and then append a token:

         Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)

     That string contains no browser-engine word of its own, but plenty of
     crawler UAs DO (`...AppleWebKit/537.36 (KHTML, like Gecko) ...
     compatible; ClaudeBot/1.0`). So if the human test runs before the agent
     test, a large share of the crawl fleet scores as HUMAN and the report
     invents an audience that does not exist. The order self -> agent -> human
     is the fix, and this file is what stops someone "simplifying" it back.

  2. ★ SELF-TRAFFIC IS THE LARGEST COHORT ON THIS ZONE. At the last user-agent
     decomposition our own probes, cron and health checks were ~40% of zone
     requests. Counting those as either humans or third-party agents restates
     our own noise as demand — the exact error already recorded twice in this
     codebase (a referrer read that was self-traffic, and a conversion count
     that was 66.5% self). `self` is therefore checked FIRST and reported in
     its own bucket, never folded into another.

The tests below are written so that each one fails for exactly one reason, and
so that reordering the branches in `classify_ua` breaks them loudly. They are
pure-function tests on purpose: the plumbing around them (HTTP, GraphQL) is
mocked or absent in CI, but the judgement is always exercisable.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes.page_usage import classify_ua, _norm_path  # noqa: E402


# ── 1. the ordering guard ────────────────────────────────────────────────

# Every one of these carries a real browser-engine token AND a bot token. A
# classifier that checks "human" first returns 'human' for all of them.
BROWSER_SHAPED_BOTS = [
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 (compatible; ClaudeBot/1.0)",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36 (compatible; GPTBot/1.1)",
    "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
    "Mozilla/5.0 (compatible; PerplexityBot/1.0; +https://perplexity.ai/bot)",
    "Mozilla/5.0 (compatible; SemrushBot/7~bl; +http://www.semrush.com/bot.html)",
    "Mozilla/5.0 (compatible; AhrefsBot/7.0; +http://ahrefs.com/robot/)",
    "Mozilla/5.0 (Linux; Android 5.0) AppleWebKit/537.36 (KHTML, like Gecko) Mobile Safari/537.36 (compatible; Bytespider)",
]


@pytest.mark.parametrize("ua", BROWSER_SHAPED_BOTS)
def test_browser_shaped_bots_are_agents_not_humans(ua):
    """A crawler that dresses as a browser must still be an agent.

    This is the test that fails if the human branch is ever moved above the
    agent branch — which is the single most likely 'harmless' refactor here.
    """
    assert classify_ua(ua) == "agent", (
        "UA carries a browser token AND a bot token; agent must win.\n"
        "  ua = %s\n  got = %s" % (ua, classify_ua(ua))
    )


# ── 2. our own traffic is never demand ───────────────────────────────────

OUR_OWN = [
    "dchub-clarity-probe/1.0",
    "DCHub-CronHeartbeat/1.0",
    "dchub-agent-probe/1.0",
    "DCHub-PageUsage/1.0",
    "UptimeRobot/2.0; +http://uptimerobot.com/",
]


@pytest.mark.parametrize("ua", OUR_OWN)
def test_self_traffic_is_its_own_bucket(ua):
    """Self must never be scored as human or as third-party agent demand."""
    got = classify_ua(ua)
    assert got == "self", (
        "our own probe scored as %r — this is how self-traffic becomes a"
        " growth number. ua=%s" % (got, ua)
    )


def test_self_beats_agent_for_our_own_sdk_shaped_probes():
    """A dchub probe that ALSO looks like an SDK is still self.

    'dchub-sync/1.0 python-requests/2.31.0' matches both patterns. Self is
    checked first precisely so our own traffic cannot hide inside the agent
    bucket and inflate apparent third-party adoption.
    """
    assert classify_ua("dchub-sync/1.0 python-requests/2.31.0") == "self"


# ── 3. real humans still register ────────────────────────────────────────

REAL_BROWSERS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36 Edg/125.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
]


@pytest.mark.parametrize("ua", REAL_BROWSERS)
def test_real_browsers_are_human(ua):
    """The control. If this fails alongside the bot tests, the classifier has
    simply stopped classifying — which a bots-only assertion would not catch."""
    assert classify_ua(ua) == "human"


# ── 4. never fabricate an audience ───────────────────────────────────────

def test_empty_ua_is_unknown_not_human_and_not_agent():
    """An empty UA is undeclared automation.

    Folding it into 'agent' would overstate agent demand; folding it into
    'human' would overstate the funnel. It gets its own bucket and is reported
    separately, so nobody can quietly absorb it later.
    """
    for blank in ("", "   ", None):
        assert classify_ua(blank) == "unknown"


def test_unrecognised_ua_is_unknown():
    assert classify_ua("SomeInternalTool/9.9") == "unknown"


# ── 5. path normalisation — one page, one key ────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("https://dchub.cloud/pricing/", "/pricing"),
    ("https://dchub.cloud/pricing", "/pricing"),
    ("/pricing/", "/pricing"),
    ("/pricing", "/pricing"),
    ("/facilities/x?utm_source=chatgpt.com", "/facilities/x"),
    ("/a#frag", "/a"),
    ("https://dchub.cloud/", "/"),
    ("/", "/"),
])
def test_norm_path_collapses_to_one_key(raw, expected):
    """The sitemap publishes absolute URLs; the edge reports bare paths, often
    with a query string. They must normalise to the same key or every page
    looks unvisited — a false 'dead page' report is worse than no report.
    """
    assert _norm_path(raw) == expected


def test_norm_path_keeps_distinct_pages_distinct():
    """Normalisation must not over-collapse: /markets and /markets/dallas are
    different pages and must not merge."""
    assert _norm_path("/markets") != _norm_path("/markets/dallas")


# ── 6. the admin gate is fail-CLOSED ─────────────────────────────────────

def test_admin_gate_is_fail_closed_without_a_key(monkeypatch):
    """With no admin key configured the endpoint must refuse (503), never
    serve. A gate that opens when the box is misconfigured is not a gate —
    see tests/test_admin_gate_fail_closed.py for the class."""
    import flask

    import routes.page_usage as pu

    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    monkeypatch.delenv("ADMIN_KEY", raising=False)

    app = flask.Flask(__name__)
    app.register_blueprint(pu.page_usage_bp)
    with app.test_client() as c:
        r = c.get("/api/v1/admin/page-usage")
    assert r.status_code == 503, "no key configured must fail closed, got %s" % r.status_code


def test_admin_gate_rejects_a_wrong_key(monkeypatch):
    import flask

    import routes.page_usage as pu

    monkeypatch.setenv("DCHUB_ADMIN_KEY", "right-key")
    app = flask.Flask(__name__)
    app.register_blueprint(pu.page_usage_bp)
    with app.test_client() as c:
        r = c.get("/api/v1/admin/page-usage", headers={"X-Admin-Key": "wrong-key"})
    assert r.status_code == 401


# ── 7. the datetime-scalar fallback ──────────────────────────────────────

def test_both_scalar_variants_render_a_complete_query():
    """Neither variant may leave an unsubstituted placeholder.

    The query is a %-template, so a stray literal % would both break the
    substitution and smuggle a percent into a string this repo already has a
    lint rule about.
    """
    from routes.page_usage import _EDGE_QUERY, _EDGE_SCALARS

    assert len(_EDGE_SCALARS) >= 2, "the fallback needs something to fall back to"
    for scalar in _EDGE_SCALARS:
        q = _EDGE_QUERY % {"scalar": scalar}
        assert "%" not in q, "unsubstituted placeholder left in the %s query" % scalar
        assert "$since: %s!" % scalar in q
        assert "httpRequestsAdaptiveGroups" in q
        assert "clientRequestPath" in q and "userAgent" in q


def test_second_scalar_is_tried_when_the_first_is_rejected(monkeypatch):
    """Cloudflare names this scalar `Time` on some datasets and `DateTime` on
    others. If the first is rejected the second must actually be attempted —
    otherwise the fallback is decoration and the endpoint 502s in production
    against a name nobody verified.

    Two queries are emitted (coverage, then audience split), and EACH must do
    its own scalar negotiation, so a first-scalar rejection means four calls.
    """
    import routes.page_usage as pu

    calls = []

    def fake_graphql(query, variables, token=None):
        calls.append(query)
        if "$since: Time!" in query:
            return {"errors": [{"message": "unknown type Time"}]}
        if "PathTotals" in query:
            return {"data": {"viewer": {"zones": [{"httpRequestsAdaptiveGroups": [
                {"count": 7, "dimensions": {"clientRequestPath": "/pricing"}}]}]}}}
        return {"data": {"viewer": {"zones": [{"httpRequestsAdaptiveGroups": [
            {"count": 7, "dimensions": {"clientRequestPath": "/pricing",
                                        "userAgent": "curl/8.5.0"}}]}]}}}

    import types
    fake_mod = types.ModuleType("routes.cf_analytics")
    fake_mod._cf_graphql = fake_graphql
    fake_mod._CF_ZONE_ID = "zone123"
    fake_mod._CF_ZONE_TOKEN = "tok"
    monkeypatch.setitem(sys.modules, "routes.cf_analytics", fake_mod)

    usage, meta, err = pu._edge_usage(7, 100)
    assert err is None, "fallback did not recover: %s" % err
    assert len(calls) == 4, (
        "expected 4 calls (2 queries x scalar retry), got %d — either a query"
        " stopped negotiating its scalar, or one of the two queries vanished"
        % len(calls))
    assert meta["datetime_scalar_accepted"] == "DateTime"
    assert usage["/pricing"]["agent"] == 7, "curl must land in the agent bucket"
    assert usage["/pricing"]["human"] == 0
    assert usage["/pricing"]["total"] == 7, (
        "total must come from the coverage query ONLY — if the split rows were"
        " added on top, every path present in both would be double-counted")


def test_coverage_query_reaches_pages_the_split_query_missed(monkeypatch):
    """★ The defect this whole two-query design exists to fix.

    Measured on the first live run: human_touched was 1,791 at days=1 but 225
    at days=7. A longer window reporting FEWER touched pages is impossible —
    it was the pair-grouped query spending its 10k row budget on (path, UA)
    pairs and never reaching the tail.

    Here /quiet-page appears in coverage but not in the split. It must be
    counted as SERVED with an unknown audience — never as absent (which would
    call a used page dead) and never as agent_only (which would turn a row
    limit into a claim about who visited).
    """
    import routes.page_usage as pu
    import types

    def fake_graphql(query, variables, token=None):
        if "PathTotals" in query:
            return {"data": {"viewer": {"zones": [{"httpRequestsAdaptiveGroups": [
                {"count": 900, "dimensions": {"clientRequestPath": "/busy"}},
                {"count": 2, "dimensions": {"clientRequestPath": "/quiet-page"}}]}]}}}
        return {"data": {"viewer": {"zones": [{"httpRequestsAdaptiveGroups": [
            {"count": 900, "dimensions": {"clientRequestPath": "/busy",
                                          "userAgent": "curl/8.5.0"}}]}]}}}

    fake_mod = types.ModuleType("routes.cf_analytics")
    fake_mod._cf_graphql = fake_graphql
    fake_mod._CF_ZONE_ID = "z"
    fake_mod._CF_ZONE_TOKEN = "t"
    monkeypatch.setitem(sys.modules, "routes.cf_analytics", fake_mod)

    usage, meta, err = pu._edge_usage(7, 100)
    assert err is None

    assert "/quiet-page" in usage, (
        "the coverage query saw it; it must not vanish just because the split"
        " query could not afford a row for it")
    assert usage["/quiet-page"]["total"] == 2
    assert usage["/quiet-page"]["split_known"] is False, (
        "no user-agent row was returned for this path, so the audience is"
        " genuinely unknown and must be marked as such")
    assert usage["/quiet-page"]["agent"] == 0 and usage["/quiet-page"]["human"] == 0

    assert usage["/busy"]["split_known"] is True
    assert usage["/busy"]["agent"] == 900
    assert usage["/busy"]["total"] == 900, "coverage total, not coverage+split"


def test_truncation_is_reported_per_query(monkeypatch):
    """Coverage truncation and split truncation mean different things and must
    not borrow each other's confidence: coverage truncation can make a used
    page look unused, split truncation only costs audience detail."""
    import routes.page_usage as pu
    import types

    def fake_graphql(query, variables, token=None):
        if "PathTotals" in query:  # exactly at the limit -> truncated
            return {"data": {"viewer": {"zones": [{"httpRequestsAdaptiveGroups": [
                {"count": 1, "dimensions": {"clientRequestPath": "/p%d" % i}}
                for i in range(3)]}]}}}
        return {"data": {"viewer": {"zones": [{"httpRequestsAdaptiveGroups": []}]}}}

    fake_mod = types.ModuleType("routes.cf_analytics")
    fake_mod._cf_graphql = fake_graphql
    fake_mod._CF_ZONE_ID = "z"
    fake_mod._CF_ZONE_TOKEN = "t"
    monkeypatch.setitem(sys.modules, "routes.cf_analytics", fake_mod)

    usage, meta, err = pu._edge_usage(7, 3)
    assert err is None
    assert meta["coverage_truncated"] is True, "3 rows against a limit of 3"
    assert meta["split_truncated"] is False, "0 rows against a limit of 3"
    assert meta["coverage_rows_returned"] == 3
    assert meta["split_rows_returned"] == 0


def test_all_scalars_rejected_reports_honestly(monkeypatch):
    """If every variant is rejected the endpoint must say so, naming what it
    tried — not return an empty join that reads as 'no traffic'."""
    import routes.page_usage as pu
    import types

    fake_mod = types.ModuleType("routes.cf_analytics")
    fake_mod._cf_graphql = lambda q, v, token=None: {"errors": [{"message": "nope"}]}
    fake_mod._CF_ZONE_ID = "zone123"
    fake_mod._CF_ZONE_TOKEN = "tok"
    monkeypatch.setitem(sys.modules, "routes.cf_analytics", fake_mod)

    usage, meta, err = pu._edge_usage(7, 100)
    assert usage == {} and err, "must surface an error, not a silent empty join"
    assert "scalar" in err.lower()
