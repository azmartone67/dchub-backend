"""r-markets-api-ident (2026-09-05) — the PDF report builder SILENTLY dropped
markets it could not resolve.

THE BUG. generate_market_pdf looped over the requested markets and ran

    market_lower = market.lower().replace('-', ' ')
    if market_lower not in MARKET_ALIASES:
        continue

against a hand-written dict of 34 curated US keys, while /api/v1/markets
publishes 132 markets. The `continue` is what makes this worse than the sibling
404s fixed in the same family:

  * the PDF header printed EVERY market the caller asked for
    (f"Markets: {', '.join([m.title() for m in markets])}"),
  * the download was named dc-hub-<all-of-them>-report.pdf,
  * the `reports` row stored json.dumps(markets) — all of them,
  * and the body contained only the ones that happened to be curated.

A Pro customer ($299/mo, this route is @require_plan('pro')) asking for
"Ashburn, London" received a file titled "Markets: Ashburn, London" containing
Ashburn alone, with nothing anywhere saying London had been dropped. A 404 is a
bad answer. A document that asserts coverage it does not have is a wrong one,
and it is wrong in the direction nobody checks.

The builder also carried `city ILIKE '%city%'` with NO country predicate — the
substring bleed r43-H fixed in get_market_stats on 2026-05-27 and which reached
neither this builder nor /api/v1/markets/compare. Measured live 2026-09-05 on
compare, which had the identical predicate: reno read 43 facilities against the
detail route's 22, because 'reno' matches Grenoble. Resolving international
markets here WITHOUT fixing that would have printed inflated numbers into a paid
PDF, so both move together.

Source-level: tests/ must not import main (the green-main convention), so the
route is read out of the AST. The resolver itself is executed in
tests/test_markets_api_identifier_resolution.py.
"""
import ast
import os

MAIN_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")

from util.market_aliases import (  # noqa: E402
    COVERAGE_GAP_PREFIX, report_coverage_lines, resolve_market_list)


def _fn(name):
    src = open(MAIN_PY, encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node, src
    raise AssertionError(f"{name} not found in main.py")


def _pdf_src():
    node, src = _fn("generate_market_pdf")
    seg = ast.get_source_segment(src, node)
    assert seg and len(seg) > 2000, "generate_market_pdf slice looks wrong"
    # Read the RIGHT thing: anchor the slice to the function these assertions
    # are about, so none of them can pass on a mis-sliced or empty string.
    assert "elements.append" in seg and "doc.build(elements)" in seg
    return seg


def _calls(node):
    return {
        n.func.id if isinstance(n.func, ast.Name) else
        (n.func.attr if isinstance(n.func, ast.Attribute) else "")
        for n in ast.walk(node) if isinstance(n, ast.Call)
    }


def test_the_pdf_builder_resolves_through_the_published_universe():
    """★ MUTATION TARGET. Without the resolver the builder is back to 34
    curated keys while /api/v1/markets publishes 132."""
    calls = _calls(_fn("generate_market_pdf")[0])
    assert "resolve_market_list" in calls, (
        "generate_market_pdf no longer resolves identifiers — every "
        "non-curated market is silently dropped from a paid report again")
    assert "build_market_universe" in calls, (
        "generate_market_pdf no longer reads the published universe, so it "
        "resolves against a subset of what /api/v1/markets lists")


# ---------------------------------------------------------------------------
# ★ EXECUTED, not grepped.
#
# A substring version of these two tests was written first and MUTATION-TESTED
# GREEN — it could not fail. Both of the mutations that matter survived it:
#
#   replacing `unresolved.append(raw)` with `pass`   -> still green, because
#       the word "unresolved" survives in the declaration one line above;
#   wrapping the whole disclosure in `if False:`     -> still green, because
#       the "Not covered:" template is still present in the dead branch.
#
# Presence of a string is not reachability of the code that emits it. So the
# partition and the header rendering moved into pure helpers in
# util/market_aliases.py, and these tests run them.
# ---------------------------------------------------------------------------
UNIVERSE = [
    {"id": "london-gb", "name": "London", "cities": ["London"],
     "auto_discovered": True, "international": True, "country": "GB"},
    {"id": "santa-clara", "name": "Santa Clara", "cities": ["Santa Clara"],
     "auto_discovered": True, "state": "CA", "country": "US"},
]
CURATED = {"ashburn": ["Ashburn", "Loudoun"]}


def test_an_unresolved_market_is_never_dropped_in_silence():
    """★ THE ONE THAT MATTERS. The old `continue` produced a document that
    claimed markets it did not contain."""
    resolved, unresolved = resolve_market_list(
        ["Ashburn", "London", "Atlantis"], UNIVERSE, curated=CURATED)
    assert [m["id"] for m in resolved] == ["ashburn", "london-gb"]
    assert unresolved == ["Atlantis"], (
        "an unresolvable market vanished instead of being reported")


def test_every_requested_market_lands_in_exactly_one_bucket():
    """The invariant behind 'nothing is dropped': resolved + unresolved
    accounts for every input, so there is nowhere for one to go missing."""
    requested = ["Ashburn", "london-gb", "LONDON", "Atlantis", "", "Santa Clara"]
    resolved, unresolved = resolve_market_list(
        requested, UNIVERSE, curated=CURATED)
    assert len(resolved) + len(unresolved) == len(requested)


def test_the_gap_is_named_in_the_report_body():
    """The disclosure must be a line the document actually carries — not a
    template sitting in an unreachable branch."""
    resolved, unresolved = resolve_market_list(
        ["Ashburn", "Atlantis"], UNIVERSE, curated=CURATED)
    lines = report_coverage_lines(resolved, unresolved)
    gap = [l for l in lines if l.startswith(COVERAGE_GAP_PREFIX)]
    assert gap, "the report renders no coverage-gap line at all"
    assert "Atlantis" in gap[0], "the gap line does not name the missing market"
    assert "/api/v1/markets" in gap[0], "the gap line offers no way to recover"


def test_a_complete_report_carries_no_gap_line():
    """The other direction. A disclosure that always fires is noise, and would
    make the test above pass without measuring anything."""
    resolved, unresolved = resolve_market_list(
        ["Ashburn", "London"], UNIVERSE, curated=CURATED)
    assert unresolved == []
    lines = report_coverage_lines(resolved, unresolved)
    assert not any(l.startswith(COVERAGE_GAP_PREFIX) for l in lines)


def test_the_title_names_what_the_report_covers_not_what_was_requested():
    """The header used to join the CALLER's raw list, which is precisely the
    claim the body could not back."""
    resolved, unresolved = resolve_market_list(
        ["Ashburn", "Atlantis"], UNIVERSE, curated=CURATED)
    header = report_coverage_lines(resolved, unresolved)[0]
    assert header.startswith("Markets: ")
    assert "Ashburn" in header
    assert "Atlantis" not in header, (
        "the title advertises a market the document does not contain — the "
        "exact claim that made the silent drop a wrong answer, not a missing "
        "one")


def test_the_builder_renders_the_coverage_lines_it_computes():
    """Ties the executed helpers back to the route: the PDF must render the
    lines report_coverage_lines returns, not re-derive a title of its own."""
    seg = _pdf_src()
    assert "for _line in report_coverage_lines(resolved, unresolved):" in seg, (
        "generate_market_pdf no longer renders the computed coverage lines")
    assert "for m in markets]" not in seg, (
        "the title is joining the requested markets verbatim again")


def test_the_pdf_builder_does_not_substring_match_city_names():
    """`city ILIKE '%reno%'` matched Grenoble; live on the sibling endpoint
    that inflated reno from 22 facilities to 43."""
    seg = _pdf_src()
    assert "f'%{city}%'" not in seg and 'f"%{city}%"' not in seg, (
        "generate_market_pdf is substring-matching city names again — this "
        "prints double-counted namesake cities into a paid report")
    assert "LOWER(city) = LOWER(%s)" in seg


def test_every_pdf_facility_query_is_country_scoped():
    """The builder had no country predicate at all. With international
    markets now reachable, an unscoped query is how 'london' starts counting
    London, Ontario into a customer's report."""
    seg = _pdf_src()
    assert "market_scope_sql" in seg, "generate_market_pdf has no country scope"
    assert seg.count("{_scope_sql}") >= 2, (
        f"only {seg.count('{_scope_sql}')} of the builder's 2 facility "
        "queries carry the scope guard")
