"""BUILD/CAUTION/AVOID is a published measurement, not just page furniture.

r-publish-the-verdict (2026-09-06). The last of the measures /dcpi, /markets
and /pockets all DISPLAY and none of them published.

WHY THE BASIS IS GENERATED, NOT WRITTEN
---------------------------------------
util/dcpi_method.py owns VERDICT_BANDS and VERDICT_FALLBACK and serves them at
/api/v1/dcpi/methodology. A second hand-written account of the bands is not a
hypothetical risk here — that module records the last one:

    the static /dcpi/methodology page published a NEUTRAL band for months
    that derive_verdict could not produce, and 67% of published markets
    carried a verdict that page could not explain

So verdict_basis() is built from the bands themselves, and this file asserts
the properties that made the NEUTRAL defect possible cannot come back.

THREE TRAPS, ALL LIVE IN THE CODE TODAY
---------------------------------------
  1. `HOLD`. routes/pockets.py renders `{{ d.verdict or "HOLD" }}` in TEN
     places. It is a display default for a null; derive_verdict has never
     emitted it. Publishing it would invent a fourth verdict.

  2. `LOW_SIGNAL`. Documented, carries a 0.35 composite multiplier, accepted
     by ?verdict=, counted by iso_snapshot — and has NO WRITER AT ALL
     ("unreachable in practice"). Listing it as a value the scorer emits is
     the NEUTRAL defect with a different name.

  3. TIME-TO-POWER. derive_verdict(constraint, excess) takes two arguments,
     and the Dataset publishes Time to Power immediately beside the verdict.
     An agent reading them as one group concludes a faster interconnect can
     move a market out of AVOID. It cannot.
"""
import datetime
import json
import re

import flask
import pytest

from routes import market_deep_dive as mdd
from routes import pockets as pk
from util.dcpi_method import (VERDICT_BANDS, VERDICT_FALLBACK, verdict_basis,
                              verdict_domain)
from util.market_entity import market_entity

MEASURE = "DCPI Verdict"


def _measures(stats):
    node = market_entity("ashburn", "Ashburn", stats, canonical_slug="ashburn")
    return {v["name"]: v for v in node["variableMeasured"]}


# ── the domain ──────────────────────────────────────────────────────────
def test_the_domain_is_exactly_what_the_scorer_can_return():
    assert verdict_domain() == tuple(v for v, _b in VERDICT_BANDS) + (
        VERDICT_FALLBACK,)
    assert verdict_domain() == ("BUILD", "CAUTION", "AVOID")


@pytest.mark.parametrize("phantom", ["LOW_SIGNAL", "NEUTRAL", "HOLD"])
def test_no_unreachable_verdict_is_advertised(phantom):
    """Each of these is real somewhere in the system and producible by
    derive_verdict nowhere: LOW_SIGNAL has no writer, NEUTRAL was the defect,
    HOLD is a template default."""
    assert phantom not in verdict_domain()
    assert phantom not in verdict_basis(), (
        f"the published basis mentions {phantom}, which the scorer cannot "
        f"return — that is the NEUTRAL-band defect by another name")


@pytest.mark.parametrize("bad", ["HOLD", "NEUTRAL", "LOW_SIGNAL", "hold", ""])
def test_a_verdict_outside_the_domain_is_omitted_not_published(bad):
    """No measure at all is honest. A made-up label is not."""
    assert MEASURE not in _measures({"verdict": bad})


@pytest.mark.parametrize("good", ["BUILD", "CAUTION", "AVOID"])
def test_every_real_verdict_is_published(good):
    assert _measures({"verdict": good})[MEASURE]["value"] == good


def test_case_is_normalised_not_rejected():
    """The column is uppercase today, but a lowercase row is a real verdict
    spelled differently — dropping it would lose a market's headline fact."""
    assert _measures({"verdict": "build"})[MEASURE]["value"] == "BUILD"


# ── the basis ───────────────────────────────────────────────────────────
def test_the_basis_is_generated_from_the_bands():
    """Not a hand copy. Every threshold the scorer uses appears because both
    read the same table."""
    b = verdict_basis()
    for verdict, band in VERDICT_BANDS:
        assert verdict in b
        assert f"{band['excess_min']:g}" in b, verdict
        assert f"{band['constraint_max']:g}" in b, verdict
    assert VERDICT_FALLBACK in b


def test_the_basis_says_time_to_power_is_not_an_input():
    """The Dataset publishes Time to Power immediately beside the verdict."""
    b = verdict_basis()
    assert "Time-to-power is NOT an input" in b, b
    assert "cannot move a market out of" in b


def test_the_basis_says_the_fallback_is_not_a_judgement():
    """AVOID is what a market gets for clearing no band, including one with a
    missing input. Read as a judgement it overstates what DCPI claims."""
    assert "did not clear a band" in verdict_basis()
    assert "not a claim that the market is actively bad" in verdict_basis()


def test_the_basis_says_it_is_a_label_not_a_score():
    assert "A label, not a score" in verdict_basis()


def test_the_basis_is_safe_to_embed_in_ld_json():
    """Interpolated into <script type="application/ld+json">, where Jinja
    autoescape rewrites & < > ' " into entities a JSON parser does not decode.
    A band description is almost all comparison operators, so the naive
    spelling would have shipped `excess-power &gt;= 65` to every agent."""
    for ch in ("&", "<", ">", '"', "'"):
        assert ch not in verdict_basis(), ch
    assert "≥" in verdict_basis() and "≤" in verdict_basis()


# ── it reaches every surface ────────────────────────────────────────────
def _dataset(html):
    for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>',
                        html, re.S):
        node = json.loads(b)
        if node.get("@type") == "Dataset":
            return {v["name"]: v for v in node["variableMeasured"]}
    raise AssertionError("no Dataset ld+json")


BRIEF = {"market_name": "Ashburn", "narrative_md": "P.\n\nQ.",
         "key_stats": {"dcpi_score": 27.4, "facility_count": 304,
                       "total_mw": 8662.0, "verdict": "AVOID"},
         "word_count": 298,
         "generated_at": datetime.datetime(2026, 9, 6, 4, 42, 51),
         "model_used": "haiku"}
LIVE = (60.0, 46.1, "CAUTION", 24.2, datetime.datetime(2026, 9, 6, 6, 33, 27))
POCKET_ROW = ("ashburn", "Ashburn", "PJM", "VA", "AVOID", 46.1, 60.0, 24.2,
              datetime.datetime(2026, 9, 6, 6, 33, 27))


def _markets(monkeypatch, row=LIVE):
    class Cur:
        def execute(self, s, p=None): pass
        def fetchone(self): return row
        def __enter__(self): return self
        def __exit__(self, *a): return False
    class Conn:
        def cursor(self): return Cur()
        def close(self): pass
    monkeypatch.setattr(mdd, "read_deep_dive", lambda s: dict(BRIEF))
    monkeypatch.setattr(mdd, "_conn",
                        (lambda: None) if row is None else (lambda: Conn()))
    with flask.Flask(__name__).app_context():
        return _dataset(mdd._render_deep_dive_body("ashburn")
                        .get_data(as_text=True))


def _pockets(monkeypatch, row=POCKET_ROW):
    class Cur:
        def execute(self, s, p=None): pass
        def fetchone(self): return row
        def fetchall(self): return []
    class Conn:
        def cursor(self): return Cur()
        def rollback(self): pass
    monkeypatch.setattr(pk, "_get_db", lambda: Conn())
    monkeypatch.setattr(pk, "_return_db", lambda c: None)
    app = flask.Flask(__name__)
    app.register_blueprint(pk.pockets_bp)
    with app.test_client() as c:
        return _dataset(c.get("/pockets/ashburn").data.decode())


def test_markets_publishes_the_verdict(monkeypatch):
    assert _markets(monkeypatch)[MEASURE]["value"] == "CAUTION"


def test_pockets_publishes_the_verdict(monkeypatch):
    assert _pockets(monkeypatch)[MEASURE]["value"] == "AVOID"


def test_the_markets_verdict_is_live_not_the_stored_one(monkeypatch):
    """key_stats carries AVOID from when the narrative was written; the live
    row says CAUTION. Publishing the stored one beside a live composite is
    the mixed-vintage defect r-brief-live-score removed."""
    got = _markets(monkeypatch)[MEASURE]
    assert got["value"] == "CAUTION" != BRIEF["key_stats"]["verdict"]
    assert "Observed 2026-09-06" in got["description"]


def test_no_live_reading_falls_back_without_claiming_a_vintage(monkeypatch):
    got = _markets(monkeypatch, row=None)[MEASURE]
    assert got["value"] == "AVOID"
    assert "Observed" not in got["description"], (
        "the stored verdict claims a live observation time")


def test_the_json_twin_publishes_the_same_verdict(monkeypatch):
    class Cur:
        def execute(self, s, p=None): pass
        def fetchone(self): return LIVE
        def __enter__(self): return self
        def __exit__(self, *a): return False
    class Conn:
        def cursor(self): return Cur()
        def close(self): pass
    monkeypatch.setattr(mdd, "read_deep_dive", lambda s: dict(BRIEF))
    monkeypatch.setattr(mdd, "_conn", lambda: Conn())
    app = flask.Flask(__name__)
    app.register_blueprint(mdd.market_deep_dive_bp)
    with app.test_client() as c:
        body = json.loads(c.get("/markets/ashburn.json").data)
    got = {v["name"]: v["value"] for v in body["variableMeasured"]}
    assert got[MEASURE] == "CAUTION"


def test_the_pockets_template_default_never_reaches_the_measure(monkeypatch):
    """/pockets renders `{{ d.verdict or "HOLD" }}` in ten places. The
    Dataset must be built from the raw column, not the rendered string.

    ★ PROBED WITH A NULL-VERDICT ROW, because that is the only input on which
    the two differ. The first version of this test used the normal fixture
    (verdict "AVOID"), where `or "HOLD"` is unreachable — a mutation adding
    exactly that default to the Dataset call SURVIVED it.
    """
    src = open(pk.__file__, encoding="utf-8").read()
    assert "verdict or 'HOLD'" in src, (
        "the HOLD display default is gone, so this guard no longer probes "
        "anything — re-point it at whatever replaced it")
    assert MEASURE not in _measures({"verdict": "HOLD"})

    null_row = POCKET_ROW[:4] + (None,) + POCKET_ROW[5:]
    got = _pockets(monkeypatch, row=null_row)
    assert MEASURE not in got, (
        f"a market with NO verdict published {got.get(MEASURE, {}).get('value')!r} "
        f"— the page renders HOLD for a null, and that default has reached "
        f"the structured data")
