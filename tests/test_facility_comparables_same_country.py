#!/usr/bin/env python3
"""tests/test_facility_comparables_same_country.py — "other data centers
nearby" must not mean "same string, any continent".

★ THE DEFECT. _comparables_html matched neighbours on a bare LOWER(city) /
LOWER(state) compare with NO country predicate. City names are not unique
across countries, and this dataset also uses 'Regional' as a PLACEHOLDER when
the real city is unknown — so every unlocated facility on earth was mutually
"nearby". Measured live on discovered_facilities 2026-08-14:

    'Regional'   314 facilities across 30 countries
    'San Juan'   spans 4 countries
    London / Dublin / Santiago / Vienna / Manchester / San Jose /
    Barcelona / Rome / Richmond   each span 3

Rendered impact, simulating the SHIPPED query (limit 6, same ORDER BY) over all
17,948 live rows:

    16,426 pages render the module
     2,004 of them (12.2%) showed >= 1 wrong-country neighbour
     7,144 of 91,409 rendered links (7.8%) pointed to another country
    worst single group: London GB, 236 pages

A Romanian facility offered Mexican, Brazilian and Lithuanian data centers as
"comparable facilities in Regional".

★★ WHY IT MATTERS MORE THAN IT LOOKS. This module exists specifically to make a
thin facility page worth indexing — it is the one block of unique content on a
page that otherwise renders Status / City / Country. Filling it with
cross-continent links makes the page worse than empty, and 3,563 facility pages
sit in Google's "Crawled – currently not indexed" bucket.

After the fix, same simulation:

    16,000 pages render the module   (426 fewer)
         0 wrong-country pages
         0 / 87,947 wrong links
    3,682 CORRECT links were gained — same-country neighbours that previously
    lost the ORDER BY power_mw race to a bigger facility in the wrong country.

★★★ NULL COUNTRY IS NOT A WILDCARD. A row with no country matches only other
rows with no country. Treating unknown as "matches anything" is exactly how
'Regional' became global.

This test EXECUTES the shipped function against a recording stub, so it asserts
what the query is actually given — not what the source looks like.
"""
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402


class _Cur:
    """Records the SQL + params the function issues; returns no rows."""

    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params=None):
        # The function probes information_schema first; only record the real one.
        if "discovered_facilities" in sql and "information_schema" not in sql:
            self.sink.append((sql, params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _Conn:
    def __init__(self, sink):
        self.sink = sink

    def cursor(self):
        return _Cur(self.sink)

    def rollback(self):
        pass

    def close(self):
        pass


def _run(fac):
    """Call the SHIPPED _comparables_html with a stubbed DB; return (sql, params)."""
    sink = []
    fake_main = types.ModuleType("main")
    fake_main.get_read_db = lambda: _Conn(sink)
    saved = sys.modules.get("main")
    sys.modules["main"] = fake_main
    try:
        from routes.facility_profile_page import _comparables_html
        html = _comparables_html(fac)
    finally:
        if saved is not None:
            sys.modules["main"] = saved
        else:
            sys.modules.pop("main", None)
    return sink, html


def test_country_is_part_of_the_match():
    """The country the caller is in must reach the query as a bound param."""
    sink, _ = _run({"id": 1, "city": "London", "state": "", "country": "GB"})
    assert sink, "the comparables query never ran — stub or signature drifted"
    sql, params = sink[0]
    norm = " ".join(sql.split()).lower()
    assert "lower(coalesce(country, ''))" in norm or "lower(coalesce(country,''))" in norm, (
        "no country predicate in the comparables WHERE clause — 'nearby' is a "
        "bare city/state string compare again, which linked London GB to "
        "London CA on 236 pages")
    assert "GB" in [p for p in params if isinstance(p, str)], (
        f"the facility's country never reached the query params: {params}")


def test_placeholder_city_does_not_match_on_city():
    """'Regional' is not a place; it must not drive the city branch."""
    sink, html = _run({"id": 1, "city": "Regional", "state": "", "country": "RO"})
    # city-only + placeholder + no state => the function should bail entirely
    assert not sink, (
        "a facility whose city is the placeholder 'Regional' still issued a "
        "city match — 314 rows across 30 countries carry that value")
    assert html == "", "expected no module at all for placeholder-city, no-state"


def test_placeholder_city_still_allows_the_state_branch():
    """State is the honest weaker signal when city is a placeholder."""
    sink, _ = _run({"id": 1, "city": "Regional", "state": "Texas", "country": "US"})
    assert sink, "state branch should still run when city is a placeholder"
    sql, params = sink[0]
    strs = [p for p in params if isinstance(p, str)]
    assert "Texas" in strs, f"state not bound: {params}"
    assert "Regional" not in strs, (
        "the placeholder city was still bound into the query — it must be "
        "blanked before the SQL runs")


def test_real_regional_market_labels_are_not_treated_as_placeholders():
    """'California Regional' / 'Connecticut Regional' are REAL market labels.

    136 rows each. Over-broad placeholder matching would silently delete the
    module from real pages, which is the opposite failure.
    """
    for city in ("California Regional", "Connecticut Regional"):
        sink, _ = _run({"id": 1, "city": city, "state": "", "country": "US"})
        assert sink, f"{city!r} was wrongly treated as a placeholder"
        strs = [p for p in sink[0][1] if isinstance(p, str)]
        assert city in strs, f"{city!r} should still be bound as a city: {sink[0][1]}"


def test_missing_country_is_not_a_wildcard():
    """A row that does not know where it is must not claim to be near anything."""
    sink, _ = _run({"id": 1, "city": "Springfield", "state": "", "country": ""})
    assert sink, "query should still run"
    sql, params = sink[0]
    norm = " ".join(sql.split()).lower()
    assert "lower(coalesce(country, ''))" in norm or "lower(coalesce(country,''))" in norm, (
        "empty country must still be compared, not skipped — skipping it makes "
        "unknown-country rows match every country")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ★ 2026-08-21 — rows served from the `facilities` table carry TEXT ids.
# discovered_facilities.id is INTEGER, so binding a slug into `WHERE id <> %s`
# raised "invalid input syntax for type integer" and the whole module rendered
# empty on every such page (fail-soft swallowed it; the Railway log did not).
def test_text_id_rows_bind_an_integer_so_the_query_can_run():
    sink, _ = _run({"id": "meta-rosemount-mn", "city": "Rosemount",
                    "state": "MN", "country": "US"})
    assert sink, "the comparables query never ran"
    _sql, params = sink[0]
    assert isinstance(params[0], int), (
        f"a TEXT facility id reached the integer column as {params[0]!r} — "
        "Postgres rejects the whole query and the module renders empty")
    assert params[0] == -1, "a non-discovered row has nothing to exclude"


def test_integer_ids_still_exclude_the_page_itself():
    sink, _ = _run({"id": "4242", "city": "Ashburn", "state": "VA", "country": "US"})
    assert sink and sink[0][1][0] == 4242
    sink, _ = _run({"id": 17, "city": "Ashburn", "state": "VA", "country": "US"})
    assert sink and sink[0][1][0] == 17
