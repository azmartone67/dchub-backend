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


def test_as_of_is_not_truncated():
    """Slicing the stamp cuts its ZONE off, and the consumer reads a zoneless
    datetime as local time.

    Measured against dchub-mcp-server lib/attribution.mjs (parseStamp ->
    Date.parse), which is what turns this field into the envelope's `as_of`:

        '2026-09-08'             -> 2026-09-08T00:00:00.000Z
        '2026-09-08T14:22:31'    -> 2026-09-08T21:22:31.000Z   <- 7h invented
        '2026-09-08T14:22:31Z'   -> 2026-09-08T14:22:31.000Z

    So a [:19] tidy-up on an ISO stamp with a zone silently moves the date an
    agent is instructed to cite beside the number.
    """
    seg = BODY[BODY.index("newest_discovered_at')"):BODY.index("'as_of': _as_of")]
    assert "[:19]" not in seg and "[:10]" not in seg, (
        "as_of is sliced — a truncated ISO stamp loses its zone and the "
        "consumer reads it as local time")


# ── r-prose-leaks-the-gate (2026-09-21) ──────────────────────────────────────
# The free-tier trim in dchub-mcp-server is key-pattern AND type based
# (_isMetricKey + `typeof v === 'number'`). A STRING is invisible to it, and a
# string that restates a figure therefore publishes what the gate withheld.
# Measured live on anonymous get_market_intel(market="dallas") the day #5010
# shipped: facility_count null, total_power_mw null, mw_reporting_count null,
# and mw_coverage "54 of 248 report MW" beside them.
#
# The rule is not "delete mw_coverage" — that is one instance. It is: this
# view publishes figures as NUMBERS under gateable names, and never inside
# prose. Both assertions below exist because the second one alone would pass
# on an exact reintroduction of the old helper call.

def _code_only(src: str) -> str:
    """`src` with comments removed.

    ★ The assertion below bans a NAME, and the block comment explaining why
    names it repeatedly. Reading the raw slice, the explanation of the defect
    IS the defect — the guard fails on the commit that fixes it, and the only
    way to make it pass is to delete the reasoning. Tokenize instead, so the
    ban lands on code and the history stays written down.
    """
    import io as _io
    import tokenize as _tok
    try:
        out, row, col = [], 1, 0
        for t in _tok.generate_tokens(_io.StringIO(src).readline):
            if t.type == _tok.COMMENT:
                continue
            srow, scol = t.start
            if srow > row:
                out.append("\n" * (srow - row))
                col = 0
            if scol > col:
                out.append(" " * (scol - col))   # keep columns, or names fuse
            out.append(t.string)
            row, col = t.end
        return "".join(out)
    except Exception:                     # unparseable slice — fail LOUD
        raise AssertionError(
            "get_market_stats no longer tokenizes on its own; this guard "
            "cannot tell code from comment and must not pass silently")


CODE = _code_only(BODY)


def test_code_only_view_is_real():
    """Anti-vacuity: prove the strip removed comments and kept the code."""
    # Code kept. (A length comparison does NOT work here: the rebuild emits a
    # newline per NL token, so the stripped copy can be LONGER than the source
    # it stripped. An exact marker is the honest check.)
    assert "def get_market_stats" in CODE and "_stats_out" in CODE
    # Comments dropped — proven against a marker that exists ONLY in a comment.
    marker = "r-prose-leaks-the-gate"
    assert marker in BODY, (
        f"{marker!r} is gone from the source, so its absence from CODE proves "
        f"nothing — this anti-vacuity check needs a live comment to track")
    assert marker not in CODE, "comments survived the strip"


def test_no_prose_field_restates_a_figure():
    """No mw_coverage_note() (or any renamed import of it) in this view."""
    assert "mw_coverage_note" not in CODE and "_fc_mw_note" not in CODE, (
        "a formatted coverage string in this response walks through the "
        "consumer's tier gate carrying the numbers the gate just withheld")
    assert "'mw_coverage'" not in CODE, "mw_coverage is published again"


def test_basis_notes_carry_no_figures():
    """The general form: prose written at this call site states no number.

    A note reading "…/markets/<slug> reads 386 on the same market" would be
    exactly the same leak with different words, and the assertion above would
    not see it.
    """
    for note in _re_notes():
        assert not re.search(r"\d", note), (
            f"a basis note states a figure, which no tier gate can mask: {note!r}")


def test_the_gateable_number_is_still_published():
    """Anti-hollowing: removing the leak must not remove the denominator.

    The whole point of #5010 was that SUM(power_mw) travelled with the count
    of rows that reported anything. `mw_reporting_count` matches the consumer
    trim's `_count$` pattern, so it is masked in lockstep with the total it
    qualifies instead of slipping past it.
    """
    assert "'mw_reporting_count'" in BODY
    assert "COUNT(*) FILTER (WHERE power_mw > 0) as mw_reporting_count" in BODY
