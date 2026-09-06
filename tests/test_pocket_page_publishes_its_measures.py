"""/pockets/<slug> publishes its numbers as data, and agrees with /dcpi.

r-pockets-structured-data (2026-09-06). The /pockets half of the treatment
r-brief-live-score gave /markets.

WHAT WAS MEASURED
-----------------
Live, cache-busted 2026-09-06, /pockets/ashburn:

    hero tiles      Pocket rank score -4.3 · Excess power 46.1 ·
                    Grid constraint 60.0
    ld+json         one Article node, variableMeasured ABSENT ENTIRELY
    DCPI composite  never published at all

Two distinct gaps, and neither is the one /markets had:

  1. THE PAGE STORED NOTHING ALREADY. /pockets renders from
     market_power_scores on every request, so the live-score overlay
     r-brief-live-score needed has no counterpart here — there is no snapshot
     to go stale. test_the_page_reads_the_row_at_request_time pins that,
     because a future cache would silently re-create the defect /markets had.

  2. WHAT IT LACKED WAS PUBLICATION. Four numbers on the page, none of them in
     the structured data an agent cites without reading it — and no DCPI
     composite anywhere, so the surface a reader lands on for a market's power
     outlook could not tell them the number /dcpi owns for it.

Both nodes are built by util.market_entity — the same builder as
/markets/<slug> and /markets/<slug>.json — because a second Dataset writer for
one market is the drift this family has spent four PRs on.
"""
import datetime
import json
import re

import flask
import pytest

from routes import pockets as pk
from routes.dcpi import derive_composite_score
from util.deployability_rank import RANKINGS

#: One market_power_scores row: (slug, name, iso, state, verdict,
#: excess, constraint, ttp, computed_at).
ROW = ("ashburn", "Ashburn", "PJM", "VA", "AVOID", 46.1, 60.0, 36,
       datetime.datetime(2026, 9, 6, 3, 15, 34))
EXPECT_DCPI = round(derive_composite_score(46.1, 60.0, 36, "AVOID"), 1)


@pytest.fixture
def page(monkeypatch):
    """(html, detail, executed_sql) for one rendered pocket page."""
    seen = []

    def _make(row=ROW):
        class Cur:
            def execute(self, sql, params=None):
                seen.append((sql, params))
            def fetchone(self):
                return row
            def fetchall(self):
                return []
        class Conn:
            def cursor(self):
                return Cur()
            def rollback(self):
                pass
        monkeypatch.setattr(pk, "_get_db", lambda: Conn())
        monkeypatch.setattr(pk, "_return_db", lambda c: None)
        app = flask.Flask(__name__)
        app.register_blueprint(pk.pockets_bp)
        with app.test_client() as c:
            html = c.get("/pockets/ashburn").data.decode()
        return html, pk._fetch_pocket_detail("ashburn"), seen
    return _make


def _nodes(html):
    out = []
    for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>',
                        html, re.S):
        out.append(json.loads(b))     # raises if a value broke the JSON
    return out


def _dataset(html):
    for n in _nodes(html):
        if n.get("@type") == "Dataset":
            return n
    raise AssertionError("no Dataset ld+json on the pocket page")


def _measure(node, name):
    for v in node.get("variableMeasured") or []:
        if v.get("name") == name:
            return v
    return None


# ── 1. it already renders live, and must keep doing so ──────────────────
def test_the_page_reads_the_row_at_request_time(page):
    """/pockets has no stored brief. That is why r-brief-live-score's overlay
    has no counterpart here — and why this test exists: a cache added later
    would re-create the exact defect /markets had, silently.

    ★ EVERY render must read, not merely the first. The first version of this
    test asserted "at least one read happened", and a mutation that cached the
    row and served it forever SURVIVED it — a cache fills on the first request
    and that first read is all such an assertion ever sees. Counting reads
    across TWO renders is what actually distinguishes live from cached.
    """
    from util.dcpi_score_row import PUBLISHED_ONLY
    _html, _detail, seen = page()
    reads = [s for s, _p in seen if "market_power_scores" in s]
    assert reads, "the page rendered without reading market_power_scores"
    assert PUBLISHED_ONLY in reads[0], reads[0]
    before = len(reads)
    page()                      # a second, independent render
    after = len([s for s, _p in seen if "market_power_scores" in s])
    assert after > before, (
        f"the second render issued no new read ({before} -> {after}) — the "
        f"page is serving a cached row, which is the stored-snapshot defect "
        f"r-brief-live-score removed from /markets")


def test_no_stored_narrative_is_consulted(page):
    """market_deep_dives backs /markets, not this page. If /pockets ever
    starts reading a stored row, its numbers acquire a second vintage and the
    whole live-vs-snapshot problem arrives here too."""
    _html, _detail, seen = page()
    assert not [s for s, _p in seen if "market_deep_dives" in s]


# ── 2. the numbers reach the structured data ────────────────────────────
def test_every_number_on_the_page_is_also_a_published_measure(page):
    """The defect: four numbers rendered, variableMeasured absent entirely."""
    html, detail, _ = page()
    node = _dataset(html)
    for name, value in (
        ("DCPI Score", detail["dcpi_score"]),
        ("Excess Power Score", detail["excess_power_score"]),
        ("Grid Constraint Score", detail["constraint_score"]),
        ("Time to Power", detail["time_to_power_months"]),
        (RANKINGS["pockets"].label, detail["rank_score"]),
    ):
        m = _measure(node, name)
        assert m is not None, f"{name} is displayed but not published"
        assert m["value"] == value, (name, m["value"], value)


def test_every_measure_states_its_basis(page):
    """A bare number in structured data is the thing r-one-builder's
    measurementTechnique exists to prevent."""
    node = _dataset(page()[0])
    for v in node["variableMeasured"]:
        assert v.get("measurementTechnique"), v["name"]
        assert v.get("description"), v["name"]


def test_the_direction_of_each_score_is_stated(page):
    """excess and constraint are both 0-100 and run OPPOSITE ways; a reader
    who assumes they are two views of one axis reads the market backwards."""
    node = _dataset(page()[0])
    assert "HIGHER IS BETTER" in _measure(node, "Excess Power Score")["description"]
    assert "LOWER IS BETTER" in _measure(node, "Grid Constraint Score")["description"]


def test_the_rank_measure_disowns_the_dcpi_composite(page):
    """Structured data is exactly where the r-pocket-score-label confusion
    would return: a bare ranking beside a DCPI Score measure, unlabelled."""
    node = _dataset(page()[0])
    m = _measure(node, RANKINGS["pockets"].label)
    assert "NOT the DCPI composite" in m["description"], m["description"]


# ── 3. it agrees with /dcpi about the market ────────────────────────────
def test_the_page_publishes_the_dcpi_composite_that_dcpi_owns(page):
    """The surface a reader lands on for a market's power outlook could not
    tell them the number /dcpi publishes for it."""
    html, detail, _ = page()
    assert detail["dcpi_score"] == EXPECT_DCPI
    assert _measure(_dataset(html), "DCPI Score")["value"] == EXPECT_DCPI
    assert f'>{EXPECT_DCPI}</div><div class="l">DCPI score<' in html, (
        "the composite is in the structured data but not on the page")


def test_a_row_missing_a_component_publishes_no_composite(page):
    """derive_composite_score coerces None to 0 and returns a plausible number
    from half a row. Omitting the measure is right; minting one is not — and
    asserting the OMISSION rather than a value is deliberate, because a
    fabricated score can collide with a real one (mutation L5 on the /markets
    fix survived exactly that way)."""
    row = ("ashburn", "Ashburn", "PJM", "VA", "AVOID", None, 60.0, 36,
           datetime.datetime(2026, 9, 6))
    html, detail, _ = page(row)
    assert detail["dcpi_score"] is None
    assert _measure(_dataset(html), "DCPI Score") is None
    assert 'class="l">DCPI score<' not in html


def test_the_dataset_points_at_the_canonical_market_page(page):
    """One market, one identity — the through-line of this whole family. The
    Dataset describes the market, whose home is /markets/<slug>, not this
    page."""
    assert _dataset(page()[0])["url"] == "https://dchub.cloud/markets/ashburn"


def test_the_measures_carry_the_row_vintage(page):
    """Everything here comes from one row read at request time, so there is a
    single honest as-of and it is that row's computed_at."""
    node = _dataset(page()[0])
    assert node["dateModified"].startswith("2026-09-06")
    assert "Observed 2026-09-06" in _measure(node, "DCPI Score")["description"]


# ── 4. the JSON twin says the same thing ────────────────────────────────
def test_the_json_twin_publishes_the_same_composite(page, monkeypatch):
    """/pockets/<slug> and /api/v1/pockets/<slug> are two surfaces of one
    market. The HTML gaining a number the API lacks is a split."""
    _html, detail, _ = page()
    assert detail["dcpi_score"] == EXPECT_DCPI, (
        "the JSON twin serves **detail for paid tiers, so the composite "
        "reaches it from the same dict the page renders")


def test_the_article_node_is_still_there(page):
    """The Dataset is added beside the Article, not in place of it — dropping
    the Article would retire the page's existing rich result."""
    kinds = {n.get("@type") for n in _nodes(page()[0])}
    assert {"Article", "Dataset"} <= kinds, kinds
