"""Guards for r-rank-provenance (2026-09-03).

rank_markets was the only tier-1 tool shipping no provenance block. Measured
live on anonymous calls, 2026-09-03:

    rank_markets          provenance=NO
    get_market_dcpi_rank  provenance=YES  cite_as="DC Hub, dchub.cloud"
    search_facilities     provenance=YES
    get_grid_scoreboard   provenance=YES

It is the tool an agent reaches for on "rank the largest markets" — the exact
question that otherwise gets answered from a named operator inventory. A number
with no cite_as and no as_of loses on attribution whatever its quality.

Pure import of the helper only; the view itself needs a DB.
"""
import io
import pathlib
import re

from routes.mcp_tier1_tools import _rank_markets_provenance

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = io.open(REPO / "routes" / "mcp_tier1_tools.py", encoding="utf-8").read()


class TestBlockShape:
    def test_carries_every_field_an_agent_needs_to_cite(self):
        p = _rank_markets_provenance("best_overall")
        for k in ("source", "as_of", "license", "cite_as", "basis", "method",
                  "cite_url_template"):
            assert k in p, f"provenance missing {k}"

    def test_citation_line_matches_the_other_tools(self):
        p = _rank_markets_provenance("best_overall")
        assert p["cite_as"] == "DC Hub, dchub.cloud"
        assert p["license"] == "CC-BY-4.0"

    def test_as_of_is_a_real_runtime_date_not_a_hardcoded_one(self):
        import datetime
        p = _rank_markets_provenance("best_overall")
        assert p["as_of"] == datetime.datetime.now().strftime("%Y-%m-%d")
        # and the SOURCE must not contain a literal date
        assert not re.search(r"20\d\d-\d\d-\d\d", SRC.split(
            "def _rank_markets_provenance")[1].split("except Exception")[0]
            .replace('_runtime_as_of', '')), "a date is hardcoded in the helper"

    def test_basis_names_BOTH_filters_the_query_applies(self):
        p = _rank_markets_provenance("best_overall")
        assert p["basis"] == "operational_deduped"
        m = p["method"].lower()
        assert "operational" in m, "method must say the rows are operational-only"
        assert "is_duplicate" in m, "method must say the rows are de-duplicated"

    def test_method_names_the_criteria_actually_used(self):
        # an agent citing a ranking must be able to say WHAT it was ranked by
        for crit in ("best_overall", "most_capacity", "cheapest_power"):
            assert crit in _rank_markets_provenance(crit)["method"]


class TestFailSoft:
    def test_a_broken_import_degrades_and_never_raises(self, monkeypatch):
        import builtins
        real = builtins.__import__

        def boom(name, *a, **k):
            if name == "routes.error_envelope":
                raise ImportError("simulated")
            return real(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", boom)
        p = _rank_markets_provenance("best_overall")
        assert p["cite_as"] == "DC Hub, dchub.cloud"   # still citable

    def test_the_helper_is_wired_into_the_response(self):
        assert '"provenance":     _rank_markets_provenance(criteria),' in SRC
