"""The frozen-slug lookup must prefer a LIVE row over a SUPPRESSED one
(2026-09-08, r-slug-live-row).

`is_duplicate` is a VISIBILITY flag — a row carrying it is one we decided not
to show. The exact-slug lookup in routes/facility_profile_page did not order on
it, so when two discovered rows share a frozen slug and neither carries
power_mw, `id ASC` handed the page to whichever was ingested first.

Measured live 2026-09-08: **1,366** slugs served their page from a suppressed
row while a live row sat on the same slug. The whole page comes from that row —
name, provider, coordinates, and duplicate_of_id, which is how this was found:
/facilities/coresite-coresite-sv3-f828fc2b rendered a self-canonical because the
served row (id 8346, is_duplicate=1) carried no pointer, while the live row on
the same slug (id 18356) pointed at the keeper.

★ An ORDER, not a WHERE: 47 slugs are served ONLY by suppressed rows. Filtering
  them out would 404 forty-seven live URLs to fix 1,366 — this test pins both
  halves.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
_RAW = (ROOT / "routes" / "facility_profile_page.py").read_text()
# Comments are not SQL. Strip whole-line `#` comments so the guard reads what
# actually executes — otherwise a comment mentioning is_duplicate would satisfy
# it, and the check would pass on a file that still orders the old way.
SRC = "\n".join(l for l in _RAW.splitlines() if not l.lstrip().startswith("#"))


def _exact_slug_query():
    """The executing SQL of the frozen-slug exact lookup, read off the source
    with comments removed."""
    # include the closing quote so the captured group starts BETWEEN string
    # literals — otherwise the stray quote pairs with the next one and the
    # findall below returns the whitespace instead of the SQL.
    # Capture THROUGH the closing quote of the last literal. Stopping at
    # `LIMIT 1` cuts inside it, leaving it unpaired, and the findall below then
    # silently drops the whole `power_mw ... id ASC` fragment — the guard read
    # only the first line and would have passed on a half-written ORDER BY.
    m = re.search(r'FROM \{_tbl\} WHERE canonical_slug = %s"(.*?LIMIT 1")', SRC, re.S)
    assert m, "the frozen-slug exact lookup is gone — this guard is inert"
    sql = " ".join(re.findall(r'"([^"]*)"', m.group(1)))
    return " ".join(sql.split())


def test_the_lookup_orders_suppressed_rows_last():
    order = _exact_slug_query()
    assert "ORDER BY" in order, order
    assert "is_duplicate" in order, (
        "the exact-slug lookup does not order on is_duplicate — a suppressed "
        "row can win the slug and serve the page (1,366 live slugs, 2026-09-08)")
    # is_duplicate must come FIRST: power and id are tie-breaks BELOW it, and a
    # suppressed row with power_mw set would otherwise still win.
    pos_dup = order.index("is_duplicate")
    for later in ("power_mw", "id ASC"):
        assert pos_dup < order.index(later), (
            f"is_duplicate must outrank {later} in the ORDER BY: {order}")
    assert "ASC" in order[pos_dup:pos_dup + 40], order


def test_it_is_an_order_not_a_filter():
    """47 slugs are served ONLY by suppressed rows. A WHERE clause here 404s
    them; the fix must leave them reachable."""
    sql = _exact_slug_query()
    assert "AND" not in sql.upper().split("ORDER BY")[0], (
        "a predicate was added to the exact-slug WHERE — 47 slugs are served "
        "only by suppressed rows and would 404: " + sql)


def test_the_ordering_expression_is_null_safe():
    """A NULL is_duplicate must sort with the live rows, not last: most rows
    carry NULL rather than 0."""
    order = _exact_slug_query()
    seg = order[order.index("ORDER BY"):]
    assert "COALESCE(is_duplicate, 0)" in seg or "COALESCE(is_duplicate,0)" in seg, seg
