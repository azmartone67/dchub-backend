"""Guards for r-markets-basis (2026-09-03) — /api/v1/markets(/list) counting basis.

/api/v1/markets was the LAST market surface reading a raw COUNT(*) /
SUM(power_mw): no de-duplication, no lifecycle filter. Every sibling surface had
already been moved off that basis — rank_markets applies both, routes/dcpi
deduped all three of its market reads on 2026-08-08 — so the public, citable
endpoint published the highest number in the building. Live 2026-09-03 it served
Northern Virginia at 12,438 MW / 768 facilities.

The direction of that error is not in doubt; the DCPI dedup measured it on the
same table ("boardman read 51 facilities against 5 real"). An undeduped market
aggregate counts the same building more than once.

Source-level only — never imports main (it needs a DB at import time), the same
house rule test_market_canon_aurora.py follows.
"""
import io
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
MAIN = io.open(REPO / "main.py", encoding="utf-8").read()


def _slice(defn: str) -> str:
    """Source of one top-level function, up to the next route decorator."""
    i = MAIN.index(defn)
    j = MAIN.index("\n@app.route", i)
    return MAIN[i:j]


def _list_markets_body() -> str:
    """The source that BUILDS the published market list.

    r-markets-api-ident (2026-09-05): the build moved out of the
    list_markets view into build_market_universe, so /api/v1/markets/<id>
    could resolve against the same rows the list publishes. This slice used
    to read list_markets ALONE, and when the build left, every assertion
    below went looking in a function that no longer contained the queries.

    ★ The old `len(body) > 2000` floor did NOT catch it. list_markets is
    still well over 2000 characters — tier gating, redaction and the
    response envelope all stayed — so the slice looked healthy while the
    thing it exists to inspect had gone. A size floor measures that we read
    SOMETHING, not that we read the right thing; the marker assertion below
    is what actually pins it.

    Both functions are concatenated so this guard keeps working whichever
    side of the split the queries live on.
    """
    body = _slice("def build_market_universe(c):") + _slice("def list_markets():")
    assert len(body) > 2000, "slice looks wrong — did the market build move?"
    # ★ Read the RIGHT thing, not merely something. Each marker is a query
    # this file asserts about; if a refactor moves one out of reach, this
    # fails loudly instead of every test below passing on an empty search.
    for marker in ("SELECT COUNT(*) as count",
                   "SELECT LOWER(city), city, state,",
                   "_mkt_contained"):
        assert marker in body, (
            f"{marker!r} is no longer in the sliced source — this guard has "
            "gone blind; re-point _slice() at whatever function holds it now")
    return body


class TestDedupAndLifecycle:
    def test_curated_inventory_query_is_deduped_and_operational(self):
        body = _list_markets_body()
        # the curated COUNT(*)/SUM(power_mw) query
        m = re.search(r"SELECT COUNT\(\*\) as count, COALESCE\(SUM\(power_mw\), 0\) as total_power"
                      r".*?\"\"\"", body, re.S)
        assert m, "curated inventory query not found"
        q = m.group(0)
        assert "_MKT_DEDUP" in q, "curated inventory query is NOT de-duplicated"
        assert "_mkt_operational" in q, "curated inventory query has NO lifecycle filter"

    def test_auto_discovered_query_is_deduped_and_operational(self):
        body = _list_markets_body()
        m = re.search(r"SELECT LOWER\(city\), city, state,.*?ORDER BY n DESC LIMIT 60;", body, re.S)
        assert m, "auto-discovered query not found"
        q = m.group(0)
        assert "_MKT_DEDUP" in q, "auto-discovered query is NOT de-duplicated"
        assert q.count("_mkt_operational") >= 3, (
            "auto-discovered query must filter COUNT, SUM and HAVING on lifecycle; "
            f"found {q.count('_mkt_operational')} uses"
        )

    def test_pipeline_sum_is_deduped_but_NOT_lifecycle_filtered(self):
        # pipeline is construction/planned by definition — applying the
        # operational filter to it would zero the column, not correct it.
        body = _list_markets_body()
        m = re.search(r"SELECT COALESCE\(SUM\(power_mw\), 0\)\s*\n\s*FROM discovered_facilities"
                      r".*?\"\"\"", body, re.S)
        assert m, "curated pipeline query not found"
        q = m.group(0)
        assert "_MKT_DEDUP" in q, "pipeline sum is NOT de-duplicated"
        assert "_mkt_operational" not in q, (
            "pipeline sum must NOT be lifecycle-filtered — that would zero the column"
        )

    def test_dedup_predicate_uses_COALESCE_not_a_bare_flag(self):
        # `is_duplicate = 0` drops NULLs; COALESCE keeps never-evaluated rows.
        assert '_MKT_DEDUP = "COALESCE(is_duplicate, 0) = 0"' in MAIN


class TestOverlapIsPublished:
    def test_contained_in_is_emitted_on_curated_rows(self):
        body = _list_markets_body()
        assert "'contained_in': _mkt_contained.get(market_key, [])" in body

    def test_containment_is_a_STRICT_subset_not_any_overlap(self):
        body = _list_markets_body()
        # `<` not `<=`: a market is not contained in itself, and two markets with
        # identical city sets are twins, not a containment pair.
        assert "_mkt_sets[k] < ov" in body, "containment must use strict subset (<)"

    def test_ashburn_really_is_inside_northern_virginia(self):
        """The relation the fix exists for, computed from the live table."""
        i = MAIN.index("MARKET_ALIASES = {")
        block = MAIN[i:MAIN.index("\n}", i) + 2]
        ns = {}
        exec(block, ns)                      # noqa: S102 — literal dict of str lists
        aliases = ns["MARKET_ALIASES"]
        ash = {c.lower() for c in aliases["ashburn"]}
        nova = {c.lower() for c in aliases["northern virginia"]}
        assert ash < nova, (
            "ashburn must be a strict subset of northern virginia — if this "
            "changed, contained_in stops flagging the double-count"
        )

    def test_ashburn_is_NOT_deleted_from_the_alias_table(self):
        # /api/v1/markets/<market> gates on `market_lower not in MARKET_ALIASES`
        # and smoke_test.py pins /api/v1/markets/ashburn at 200. Publishing the
        # containment is the fix; removing the row is a 404.
        assert re.search(r"^\s*'ashburn':", MAIN, re.M), "ashburn removed from MARKET_ALIASES"
        assert "/api/v1/markets/ashburn" in io.open(REPO / "smoke_test.py", encoding="utf-8").read()


class TestCitability:
    """An agent that cannot attribute a number will quote someone else's."""

    def test_response_carries_a_provenance_block(self):
        body = _list_markets_body()
        assert "'provenance': {" in body

    def test_provenance_names_the_basis_the_asof_and_the_citation(self):
        body = _list_markets_body()
        for key in ("'source'", "'method'", "'basis'", "'as_of'", "'cite_as'", "'license'"):
            assert key in body, f"provenance is missing {key}"
        assert "'basis': 'operational_deduped'" in body

    def test_provenance_warns_against_summing_overlapping_markets(self):
        body = _list_markets_body()
        m = re.search(r"'summing_note':(.*?)\},", body, re.S)
        assert m, "no summing_note"
        note = m.group(1)
        assert "contained_in" in note, "summing_note must name the field that flags overlap"

    def test_as_of_is_computed_not_hardcoded(self):
        body = _list_markets_body()
        m = re.search(r"'as_of': (.+?),\n", body)
        assert m, "no as_of"
        assert "datetime" in m.group(1), f"as_of looks hardcoded: {m.group(1)}"
