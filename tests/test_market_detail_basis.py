"""Guards for r-market-grain (2026-09-20) — /api/v1/markets/<market>'s grain.

This is the row `get_market_intel` proxies (the MCP tool calls it with ?rag=1),
and it published two bare numbers and no date. Measured live 2026-09-20:

    /api/v1/markets/dallas   308 facilities   3,690 MW   this route
    /markets/dallas          386 facilities   7,067 MW   the page + .json twin

Both are correct — util/facility_count_basis.py has defined the three axes that
separate them since 2026-08-01 — but only the page declared its grain (through
util/market_entity), so a caller that read both saw a contradiction. The same
response also carries `related_intel`, whose market_narratives passages quote
the PAGE's numbers, so the contradiction fits inside one payload.

Source-level only — never imports main (it needs a DB at import time), the same
house rule test_markets_list_basis.py follows.
"""
import io
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
MAIN = io.open(REPO / "main.py", encoding="utf-8").read()


def _body() -> str:
    """Source of get_market_stats, up to the next route decorator.

    ★ Marker-checked, not size-checked. test_markets_list_basis.py learned
    this the hard way: a slice can stay large while the queries it exists to
    inspect move out of it, and every assertion below would then pass on a
    string that contains none of them.
    """
    i = MAIN.index("def get_market_stats(market):")
    j = MAIN.index("\n@app.route", i)
    body = MAIN[i:j]
    for marker in ("COUNT(*) as facility_count",
                   "SELECT status, COUNT(*) as count",
                   "'stats': _stats_out"):
        assert marker in body, (
            f"{marker!r} is no longer inside get_market_stats — this guard is "
            f"reading the wrong source and would pass vacuously.")
    return body


BODY = _body()

# Every read of the fleet table inside this view, as executed SQL.
_READS = re.findall(r"FROM discovered_facilities\s*\n\s*WHERE \(\{where_clause\}\)[^\n]*",
                    BODY)


def test_reads_are_found():
    """Anti-vacuity: the dedup assertion below must have something to assert on."""
    assert len(_READS) == 4, (
        f"expected the 4 discovered_facilities reads this view makes, found "
        f"{len(_READS)}: {_READS}")


def test_every_fleet_read_applies_the_fleet_filter():
    """_MKT_DEDUP, the constant /api/v1/markets has used since 2026-07-28.

    Without it this route counts one building once per keeper row, and it then
    publishes a `count_basis` whose `fleet_filter` field states the filter it
    did not apply — a basis block that lies is worse than no basis block.
    """
    for sql in _READS:
        assert "{_MKT_DEDUP}" in sql, (
            f"fleet read without the dedup filter: {sql!r}")


def test_mw_total_travels_with_its_denominator():
    """power_mw is NULL for 94.9% of the table and SUM COALESCEs it to 0."""
    assert "COUNT(*) FILTER (WHERE power_mw > 0) as mw_reporting_count" in BODY
    assert "'mw_reporting_count'" in BODY, "the denominator must be PUBLISHED"


def test_response_declares_both_bases():
    assert "'count_basis'" in BODY and "'capacity_basis'" in BODY
    assert "from util.facility_count_basis import" in BODY, (
        "the basis must come from the shared vocabulary, not from a literal "
        "written here — a second copy drifts from the first.")


def test_declared_grain_is_this_surface_not_the_page():
    """The axes published must be the ones this SQL actually runs.

    The page publishes distinct_site / market_slug (util/market_entity). This
    route counts rows matched on city. Copying the page's axes over here would
    make the numbers agree on paper and mean nothing.
    """
    assert "'tracked', 'row', 'city'" in BODY, "count axes not declared"
    assert "'tracked', 'sum_rows', 'city'" in BODY, "capacity axes not declared"
    # The QUOTED term, i.e. an argument literal. The prose notes name the
    # page's axes on purpose — banning the bare word would ban the very
    # cross-reference test_the_other_published_cut_is_named requires.
    assert "'distinct_site'" not in BODY, (
        "this route does not collapse identities — passing distinct_site to "
        "the basis builder would publish the page's grain over this route's "
        "numbers.")


def test_the_other_published_cut_is_named():
    """A basis that does not name the sibling reading leaves the caller to
    discover the disagreement on their own, which is how this started."""
    for note in _re_notes():
        if "/markets/<slug>" in note:
            return
    raise AssertionError(
        "neither basis note names /markets/<slug>, the surface that reads "
        "higher on the same market")


def _re_notes():
    return re.findall(r"note=\(([^)]*)\)", BODY, re.S)


def test_as_of_is_published_and_does_not_overclaim():
    """The MCP envelope stamped as_of:null on every response because nothing
    in this payload carried a date. It carries one now — and it must claim the
    newest OBSERVATION, not a re-verification of every row."""
    assert "'as_of': _as_of" in BODY
    assert "MAX(discovered_at) as newest_discovered_at" in BODY
    basis_txt = BODY[BODY.index("'as_of_basis'"):]
    assert "not a re-verification" in basis_txt, (
        "as_of_basis must say what it does NOT prove")
    assert "UNMEASURED" in basis_txt, (
        "a cut with no dated row must still say so rather than omit the field")
