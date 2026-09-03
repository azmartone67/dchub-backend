"""Guards for r-url-no-redirect + the r-markets-basis wiring (2026-09-03).

THE URL WE HAND AN AGENT REDIRECTED. rank_markets published the city-state slug
('ashburn-va'); measured live:

    /markets/ashburn-va  301 -> 301 -> 200   (2 hops, lands on northern-virginia)
    /markets/dallas-tx   301 -> 200          (1 hop)
    /markets/dallas      200                 (0 hops)

Every hop is a chance for a crawler to drop the fetch, and the page it lands on
is titled differently from the slug we sent — a third identity for one market.
"""
import sys
import types

import pytest

from routes.mcp_tier1_tools import _market_page_slug, _rank_markets_provenance


class TestPageSlugResolves:
    def test_resolves_the_two_hop_case_in_one_step(self):
        assert _market_page_slug("ashburn") == "northern-virginia"
        assert _market_page_slug("nova") == "northern-virginia"

    def test_leaves_an_already_canonical_slug_alone(self):
        for s in ("dallas", "chicago", "phoenix", "northern-virginia"):
            assert _market_page_slug(s) == s

    def test_an_unknown_slug_passes_through_unchanged(self):
        # fail-soft: worst case is today's behaviour, never a broken link
        assert _market_page_slug("made-up-market") == "made-up-market"
        assert _market_page_slug("") == ""

    def test_it_uses_the_WEB_canon_not_the_DCPI_canon(self):
        """The two maps point OPPOSITE ways and both are right.

        util/market_aliases.DCPI_METRO_ALIASES: 'northern-virginia' -> 'ashburn'
        market_deep_dive.MARKETS_CANONICAL_REDIRECT: 'ashburn' -> 'northern-virginia'

        A page URL must follow the web's canon or it 301s. Pointing this at the
        DCPI map would reintroduce the redirect it exists to remove.
        """
        from util.market_aliases import DCPI_METRO_ALIASES
        assert DCPI_METRO_ALIASES.get("northern-virginia") == "ashburn"   # opposite
        assert _market_page_slug("ashburn") == "northern-virginia"        # web wins

    def test_the_published_url_is_built_from_the_RESOLVED_slug(self):
        import io, pathlib, re
        src = io.open(pathlib.Path(__file__).resolve().parent.parent
                      / "routes" / "mcp_tier1_tools.py", encoding="utf-8").read()
        assert '"url":              f"https://dchub.cloud/markets/{_page_slug}"' in src
        assert "_page_slug = _market_page_slug(_metro_slug)" in src


class TestBasisEnrichmentIsIndependentlyFailSoft:
    def test_the_citation_survives_when_capacity_basis_is_ABSENT(self):
        """The enrichment must never be able to strip the thing it enriches.

        capacity_basis lands in a separate PR. If its import shared the
        provenance block's try, an older util/ would collapse the whole
        citation to the two-key fallback.
        """
        real = sys.modules.pop("util.facility_count_basis", None)
        broken = types.ModuleType("util.facility_count_basis")   # no capacity_basis
        sys.modules["util.facility_count_basis"] = broken
        try:
            p = _rank_markets_provenance("best_overall")
            assert p["cite_as"] == "DC Hub, dchub.cloud"
            assert p["basis"] == "operational_deduped"
            assert "method" in p and "as_of" in p and "license" in p
            assert "capacity_basis" not in p          # omitted, not fatal
        finally:
            sys.modules.pop("util.facility_count_basis", None)
            if real is not None:
                sys.modules["util.facility_count_basis"] = real

    def test_it_LIGHTS_UP_when_capacity_basis_becomes_available(self):
        real = sys.modules.pop("util.facility_count_basis", None)
        stub = types.ModuleType("util.facility_count_basis")
        stub.capacity_basis = lambda p, a, g, note=None: {
            "population": p, "aggregation": a, "grouping": g}
        sys.modules["util.facility_count_basis"] = stub
        try:
            p = _rank_markets_provenance("best_overall")
            cb = p.get("capacity_basis")
            assert cb, "capacity_basis did not appear when the module provides it"
            assert cb["population"] == "operational"
            assert cb["aggregation"] == "sum_rows"
            assert cb["grouping"] == "city_state"
        finally:
            sys.modules.pop("util.facility_count_basis", None)
            if real is not None:
                sys.modules["util.facility_count_basis"] = real

    def test_the_axes_match_what_rank_markets_ACTUALLY_does(self):
        import io, pathlib
        src = io.open(pathlib.Path(__file__).resolve().parent.parent
                      / "routes" / "mcp_tier1_tools.py", encoding="utf-8").read()
        # operational: the query filters on operational_sql()
        assert "operational_sql()" in src
        # sum_rows: plain SUM(power_mw), not a per-site collapse
        assert "COALESCE(SUM(power_mw), 0)" in src
        assert "MAX(mw)" not in src
        # city_state: GROUP BY city, state
        assert "GROUP BY city, state" in src
