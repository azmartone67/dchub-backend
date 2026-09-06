"""/markets/<slug> publishes the DCPI components, like /pockets/<slug> does.

r-markets-components (2026-09-06). Closes the parity gap left by
r-pockets-structured-data.

WHAT WAS MEASURED
-----------------
Live, cache-busted 2026-09-06, after #4010 shipped the /pockets Dataset:

    /pockets/ashburn        5 measures: DCPI Score, Excess Power Score,
                            Grid Constraint Score, Time to Power,
                            Pocket rank score
    /markets/ashburn.json   3 measures: Total Capacity, Facilities, DCPI Score

Two surfaces of one market, publishing different amounts about it. The
components were not missing from the data — `live_dcpi_reading` already
SELECTed constraint_score, excess_power_score and time_to_power_months to
derive the composite, then discarded them. Same row, same read, same vintage.

★ THE STORED SNAPSHOT ALSO HAS COMPONENTS, AND THEY ARE NOT THESE.
key_stats carries `excess: 46` and `constraint: 60` — integers, frozen when
the narrative was written, under different key names. Publishing those beside
a live composite would re-create the mixed-vintage defect r-brief-live-score
removed. The live values (46.1) are what ship, and
test_the_components_are_live_not_the_stored_snapshot pins the difference.

★ WHAT /markets DOES NOT PUBLISH, CORRECTLY: a deployability rank. It does not
rank. The asymmetry with /pockets is real and the parity test is written to
allow exactly it, rather than demanding the two surfaces be identical.
"""
import datetime
import json
import re

import flask
import pytest

from routes import market_deep_dive as m
from util.deployability_rank import RANKINGS

#: The live row. `excess` differs from the stored snapshot's integer on
#: purpose — it is how these tests tell a live read from a frozen one.
LIVE_ROW = (60.0, 46.1, "AVOID", 24, datetime.datetime(2026, 9, 6, 6, 33, 27))
STORED_EXCESS, STORED_CONSTRAINT = 46, 60

BRIEF = {
    "market_name": "Ashburn",
    "narrative_md": "Para one.\n\nPara two.",
    "key_stats": {"dcpi_score": 27.4, "facility_count": 304,
                  "total_mw": 8662.0, "verdict": "AVOID",
                  "excess": STORED_EXCESS, "constraint": STORED_CONSTRAINT},
    "word_count": 298,
    "generated_at": datetime.datetime(2026, 9, 6, 4, 42, 51),
    "model_used": "haiku",
}

#: Everything /pockets/<slug> publishes except its own ranking, which
#: /markets has no equivalent of because it does not rank.
SHARED_MEASURES = {"DCPI Score", "Excess Power Score",
                   "Grid Constraint Score", "Time to Power"}

#: The subset that exists ONLY in the live row. The composite is not in here:
#: key_stats carries a stored `dcpi_score`, so with no live reading the page
#: still publishes it (dated to the brief, never stamped as observed) — that is
#: r-brief-live-score's fallback and it is deliberate. The components have no
#: stored counterpart under these names, so absent is the only honest answer
#: for them.
LIVE_ONLY_MEASURES = SHARED_MEASURES - {"DCPI Score"}


@pytest.fixture
def render(monkeypatch):
    def _go(row=LIVE_ROW):
        class Cur:
            def execute(self, sql, params=None): pass
            def fetchone(self): return row
            def __enter__(self): return self
            def __exit__(self, *a): return False
        class Conn:
            def cursor(self): return Cur()
            def close(self): pass
        monkeypatch.setattr(m, "read_deep_dive", lambda s: dict(BRIEF))
        monkeypatch.setattr(m, "_conn",
                            (lambda: None) if row is None else (lambda: Conn()))
        with flask.Flask(__name__).app_context():
            return m._render_deep_dive_body("ashburn").get_data(as_text=True)
    return _go


def _dataset(html):
    for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>',
                        html, re.S):
        node = json.loads(b)          # raises if a value broke the JSON
        if node.get("@type") == "Dataset":
            return node
    raise AssertionError("no Dataset ld+json on the market page")


def _by_name(node):
    return {v["name"]: v for v in node.get("variableMeasured") or []}


# ── parity with /pockets ────────────────────────────────────────────────
def test_the_measure_sets_are_not_empty():
    """Anti-vacuity: emptied, the parity and fallback tests below would both
    pass against a page publishing nothing."""
    assert len(SHARED_MEASURES) == 4
    assert len(LIVE_ONLY_MEASURES) == 3


def test_markets_publishes_everything_pockets_does_except_the_ranking(render):
    got = set(_by_name(_dataset(render())))
    missing = SHARED_MEASURES - got
    assert not missing, (
        f"/pockets/<slug> publishes {sorted(missing)} and /markets/<slug> does "
        f"not — two surfaces of one market, publishing different amounts "
        f"about it")
    assert RANKINGS["pockets"].label not in got, (
        "/markets does not rank markets, so it must not publish a "
        "deployability rank as if it did")


def test_the_page_keeps_its_own_measures_too(render):
    """The components are ADDED. Facilities and Total Capacity come from the
    narrative's snapshot and are the only measures the page ever had."""
    got = set(_by_name(_dataset(render())))
    assert {"Total Capacity", "Facilities"} <= got


# ── the components are LIVE ─────────────────────────────────────────────
def test_the_components_are_live_not_the_stored_snapshot(render):
    """key_stats carries `excess: 46` / `constraint: 60` — the snapshot's
    integers, frozen when the narrative was written. Publishing those beside a
    live composite is the mixed-vintage defect r-brief-live-score removed."""
    by = _by_name(_dataset(render()))
    assert by["Excess Power Score"]["value"] == 46.1
    assert by["Excess Power Score"]["value"] != STORED_EXCESS, (
        "the page published the snapshot's excess score, not the live one")
    assert by["Grid Constraint Score"]["value"] == 60.0
    assert by["Time to Power"]["value"] == 24.0


def test_the_components_share_the_composites_vintage(render):
    """One row, one read, one as-of. If they ever came from different reads
    the page would publish a composite that its own components do not
    reproduce."""
    by = _by_name(_dataset(render()))
    for name in ("DCPI Score", "Excess Power Score", "Grid Constraint Score"):
        assert "Observed 2026-09-06" in by[name]["description"], name


def test_each_component_states_its_basis_and_direction(render):
    """excess and constraint are both 0-100 and run OPPOSITE ways; a reader
    who assumes they are two views of one axis reads the market backwards."""
    by = _by_name(_dataset(render()))
    assert "HIGHER IS BETTER" in by["Excess Power Score"]["description"]
    assert "LOWER IS BETTER" in by["Grid Constraint Score"]["description"]
    for name in SHARED_MEASURES:
        assert by[name].get("measurementTechnique"), name


# ── the .json twin says the same thing ──────────────────────────────────
def test_the_json_twin_publishes_the_same_measures(monkeypatch):
    """Both surfaces run the same overlay. If only the HTML gained the
    components, /markets/<slug> and /markets/<slug>.json would describe one
    market differently."""
    class Cur:
        def execute(self, sql, params=None): pass
        def fetchone(self): return LIVE_ROW
        def __enter__(self): return self
        def __exit__(self, *a): return False
    class Conn:
        def cursor(self): return Cur()
        def close(self): pass
    monkeypatch.setattr(m, "read_deep_dive", lambda s: dict(BRIEF))
    monkeypatch.setattr(m, "_conn", lambda: Conn())
    app = flask.Flask(__name__)
    app.register_blueprint(m.market_deep_dive_bp)
    with app.test_client() as c:
        body = json.loads(c.get("/markets/ashburn.json").data)
    assert SHARED_MEASURES <= set(_by_name(body))
    assert _by_name(body)["Excess Power Score"]["value"] == 46.1


# ── fail-soft: absent, never stale ──────────────────────────────────────
def test_no_live_reading_omits_the_components_rather_than_staling_them(render):
    """The stored snapshot HAS an excess and a constraint. Falling back to
    them would publish a frozen integer as a current measurement — worse than
    an absent field, because a consumer cannot tell it from a live one."""
    by = _by_name(_dataset(render(None)))
    assert LIVE_ONLY_MEASURES.isdisjoint(set(by)), (
        f"components published with no live reading: "
        f"{sorted(LIVE_ONLY_MEASURES & set(by))}")
    assert by["DCPI Score"]["value"] == 27.4
    assert "Observed" not in by["DCPI Score"]["description"], (
        "the stored composite claims a live observation time")


def test_a_row_without_a_time_to_power_omits_only_that_measure(render):
    """One absent column must not take the other components with it."""
    by = _by_name(_dataset(render((60.0, 46.1, "AVOID", None,
                                   datetime.datetime(2026, 9, 6, 6, 33)))))
    assert "Time to Power" not in by
    assert by["Excess Power Score"]["value"] == 46.1
