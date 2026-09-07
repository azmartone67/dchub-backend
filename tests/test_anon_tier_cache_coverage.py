"""Tests for the anon tier-varying cache-coverage guard and its CF evaluator.

The evaluator decides which cache rule wins for an anonymous request. If it is
wrong, the guard is worse than nothing — it would certify exposure as coverage.
So it is pinned three ways: unit tests per operator, an ORACLE of dispositions
MEASURED against the live edge, and an equivalence test against the route
extractor this guard reuses.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cfx = _load("cf_expression", "scripts/cf_expression.py")
guard = _load("anon_cov", "scripts/check_anon_tier_cache_coverage.py")


@pytest.fixture(scope="module")
def rules():
    return json.loads((ROOT / "scripts" / "cf_cache_ruleset_canon.json").read_text())["rules"]


# ------------------------------------------------------------------ operators
@pytest.mark.parametrize(
    "expression,path,expected",
    [
        ('starts_with(http.request.uri.path, "/api/v1/")', "/api/v1/x", True),
        ('starts_with(http.request.uri.path, "/api/v1/")', "/api/v2/x", False),
        ('http.request.uri.path eq "/grid"', "/grid", True),
        ('http.request.uri.path eq "/grid"', "/grid/", False),
        ('http.request.uri.path contains "/mcp"', "/api/v1/mcp/tools/x", True),
        ('ends_with(http.request.uri.path, ".png")', "/a/b.png", True),
        ('http.request.uri.path in {"/a" "/b"}', "/b", True),
        ('http.request.uri.path in {"/a" "/b"}', "/c", False),
        ('http.request.uri.path wildcard r"/api/v1/grid/*/card.png"',
         "/api/v1/grid/pjm/card.png", True),
        ('lower(http.request.uri.path) eq "/grid"', "/GRID", True),
        # or / and / not / parens
        ('(http.request.uri.path eq "/a") or (http.request.uri.path eq "/b")', "/b", True),
        ('starts_with(http.request.uri.path, "/api/") and '
         'http.request.uri.path contains "v1"', "/api/v1/x", True),
        ('not (http.request.uri.path eq "/a")', "/b", True),
    ],
)
def test_operator_semantics(expression, path, expected):
    assert cfx.evaluate(cfx.parse(expression), path) is expected


def test_credential_predicates_are_false_for_an_anonymous_request():
    """★ The whole premise: rule 24 must NOT fire for a caller with no key."""
    expr = ('starts_with(http.request.uri.path, "/api/v1/") and '
            '(any(http.request.headers["x-api-key"][*] != "") or '
            'any(http.request.uri.args["api_key"][*] != ""))')
    assert cfx.evaluate(cfx.parse(expr), "/api/v1/anything") is False


def test_cookie_predicates_are_false_for_an_anonymous_request():
    assert cfx.evaluate(cfx.parse('http.cookie contains "dchub_token"'), "/dcpi") is False


def test_unparseable_expression_raises_rather_than_returning_no_match():
    """★ 'I could not read this rule' must never collapse into 'it does not apply'."""
    with pytest.raises(cfx.ParseError):
        cfx.parse("http.request.uri.path !!! broken")


def test_disposition_reports_unknown_not_bypass_when_a_rule_will_not_parse():
    bad = [{"position": 1, "id": "x" * 32, "enabled": True,
            "expression": "totally ??? broken", "action_parameters": {"cache": False}}]
    verdict, _, error = cfx.disposition(bad, "/anything")
    assert verdict == "unknown" and error


# ------------------------------------------------------------------ the oracle
def test_evaluator_reproduces_dispositions_measured_at_the_edge(rules):
    """★ ANTI-VACUITY ANCHOR.

    These four verdicts were OBSERVED with two consecutive un-cache-busted GETs
    on 2026-09-07: /api/v1/stats returned HIT age=1148 with a frozen
    x-dc-response-time; the other three returned DYNAMIC twice. An evaluator
    that cannot reproduce real edge behaviour cannot be trusted to find gaps in
    it.
    """
    for path, expected in guard.ORACLE.items():
        verdict, winner, error = cfx.disposition(rules, path)
        assert verdict == expected, (
            f"{path}: measured {expected!r} at the edge, evaluator said "
            f"{verdict!r} (winner={winner and winner['position']}, err={error})"
        )


def test_every_live_rule_parses(rules):
    unparsed = []
    for rule in rules:
        try:
            cfx.parse(rule["expression"])
        except cfx.ParseError as exc:
            unparsed.append((rule["position"], str(exc)))
    assert not unparsed, f"expressions that will not parse: {unparsed}"


def test_last_match_wins_not_first(rules):
    """The 2026-09-06 mechanism: an EARLIER bypass loses to a LATER cache rule."""
    ordered = [
        {"position": 1, "id": "a" * 32, "enabled": True,
         "expression": 'http.request.uri.path contains "/mcp"',
         "action_parameters": {"cache": False}},
        {"position": 2, "id": "b" * 32, "enabled": True,
         "expression": 'starts_with(http.request.uri.path, "/api/v1/")',
         "action_parameters": {"cache": True}},
    ]
    verdict, winner, _ = cfx.disposition(ordered, "/api/v1/mcp/tools/x")
    assert verdict == "cached" and winner["position"] == 2
    ordered.reverse()
    for i, rule in enumerate(ordered, 1):
        rule["position"] = i
    assert cfx.disposition(ordered, "/api/v1/mcp/tools/x")[0] == "bypass"


def test_a_disabled_rule_does_not_win(rules):
    disabled = [{"position": 1, "id": "c" * 32, "enabled": False,
                 "expression": 'starts_with(http.request.uri.path, "/x")',
                 "action_parameters": {"cache": False}}]
    assert cfx.disposition(disabled, "/x/y")[0] == "no-rule"


# ------------------------------------------------------------------ the scan
@pytest.fixture(scope="module")
def scan():
    return guard.scan_routes()


def test_scan_clears_its_own_floors(scan):
    gated, total, _ = scan
    assert total >= guard.MIN_ROUTES_SCANNED
    assert len(gated) >= guard.MIN_GATED_ROUTES


def test_scanner_agrees_with_the_route_extractor_it_reuses(scan):
    """★ This guard re-walks the AST to keep the view FUNCTION, which the route
    extractor discards. That is a second loop over the same data, so pin the
    two together: every path this scanner reports must be one the extractor
    also finds. Otherwise the prefix semantics drift apart silently.
    """
    rtc = _load("_rtc", "scripts/check_route_table_coherence.py")
    extractor_paths = set(rtc.extract_flask_paths(ROOT))
    gated, _, _ = scan
    drifted = sorted(set(gated) - extractor_paths)
    assert not drifted, f"paths this scanner invented: {drifted[:10]}"


def test_post_only_routes_are_not_treated_as_exposed():
    """Cloudflare does not cache POST, so a POST-only route is not exposed."""
    import ast

    rtc = _load("_rtc2", "scripts/check_route_table_coherence.py")
    get_dec = ast.parse('@bp.route("/x")\ndef f(): pass').body[0].decorator_list[0]
    post_dec = ast.parse('@bp.route("/x", methods=["POST"])\ndef f(): pass').body[0].decorator_list[0]
    both_dec = ast.parse('@bp.route("/x", methods=["GET","POST"])\ndef f(): pass').body[0].decorator_list[0]
    assert guard._get_capable(get_dec, rtc) is True
    assert guard._get_capable(post_dec, rtc) is False
    assert guard._get_capable(both_dec, rtc) is True


def test_baseline_entries_are_all_still_real_routes(scan):
    """A baseline that outlives its routes hides shrinking coverage."""
    baseline = json.loads((ROOT / "scripts" / "anon_tier_cache_baseline.json").read_text())
    gated, _, _ = scan
    stale = [p for p in baseline["known_gaps"] if p not in gated]
    assert not stale, f"baselined paths that no longer exist or are no longer gated: {stale}"


def test_no_NEW_anon_cacheable_tier_gated_routes(monkeypatch, capsys):
    """★ THE GATE. Runs the guard itself on every PR via pre-merge.

    No new workflow: pre-merge.yml already runs tests/ with no path filter, so
    riding it means this cannot be forgotten when someone adds a route. A new
    tier-gated GET route under a caching rule fails here.

    Mutation-verified: injecting
    `@alerts_bp.route("/api/v1/mutation-probe-uncovered")` with `@require_auth`
    makes this fail with "cached by rule 2 [ef1b5109]".
    """
    monkeypatch.setattr("sys.argv", ["check_anon_tier_cache_coverage.py"])
    status = guard.main()
    captured = capsys.readouterr()
    assert status == 0, (
        "new anon-cacheable tier-gated route(s) — a tier-varying response will "
        f"be cached under a URL-only key:\n{captured.err or captured.out}"
    )
