"""/markets/<slug> serves a stored narrative but a LIVE DCPI score.

r-brief-live-score (2026-09-06).

WHAT WAS MEASURED
-----------------
Live through the edge, cache-busted 2026-09-06, before the flagship set was
regenerated:

    /markets/santa-clara   DCPI score 42.4     /dcpi/santa-clara   40.8
    /markets/los-angeles   DCPI score 44.3     /dcpi/los-angeles   40.6

32 of the 34 curated briefs agreed; those two did not. Neither was the
canon bug — both were ordinary SNAPSHOT AGE. `key_stats.dcpi_score` is frozen
when the narrative is written, and DCPI recomputes 4x/day while briefs rotate
roughly monthly. Regenerating all 34 closed the gap for a day; the next
recompute re-opens it on whichever briefs the rotation has not reached. A
treadmill is not a fix, so the number stopped being stored.

WHAT IS OVERLAID, AND WHAT IS NOT
---------------------------------
Only dcpi_score and verdict. facility_count and total_mw stay from the
snapshot, because they are what the NARRATIVE talks about — a live count
beside prose reading "116 tracked facilities" would move the contradiction
one layer in rather than remove it.

★ THE PROSE STILL CARRIES THE OLD SCORE. That is unavoidable without
regenerating every narrative, so the page DISCLOSES it: when the live score
differs from the one the brief was written against, a note names both. Every
assertion below about the note exists because serving a live headline over
stale prose in silence would be this session's own defect, committed while
fixing it.

Never imports main. The scorer is executed for real (routes.dcpi imports
without a DB, it only warns), and the render path is driven through a stub
cursor so every branch — drift, no drift, and no live reading at all — is
exercised rather than reasoned about.
"""
import datetime
import json
import re
import sys

import flask
import pytest

from routes import market_deep_dive as m
from routes.dcpi import derive_composite_score

# ── calibrated fixtures ─────────────────────────────────────────────────
# The stored brief and the live rows are CALIBRATED against the real scorer,
# not guessed: a hand-picked row that happens not to reproduce the stored
# score turns the "no drift" case into a second drift case, silently.
STORED_SCORE = 42.4
#: derive_composite_score(54.8, 60.0, 30, "CAUTION") == 42.4 exactly.
ROW_SAME = (60.0, 54.8, "CAUTION", 30, datetime.datetime(2026, 9, 6, 12, 0))
#: derive_composite_score(59.4, 65.6, 30, "AVOID") == 30.6 — a real move.
ROW_DRIFTED = (65.6, 59.4, "AVOID", 30, datetime.datetime(2026, 9, 6, 12, 0))
DRIFTED_SCORE = 30.6

BRIEF = {
    "market_name": "Santa Clara",
    "narrative_md": "Santa Clara sits at a DCPI score of 42.4.\n\nSecond para.",
    "key_stats": {"dcpi_score": STORED_SCORE, "facility_count": 116,
                  "total_mw": 1012.0, "verdict": "CAUTION"},
    "word_count": 336,
    "generated_at": datetime.datetime(2026, 9, 5, 19, 45),
    "model_used": "haiku",
}


def test_the_fixtures_reproduce_the_real_scorer():
    """Anti-vacuity for every case below. If ROW_SAME stopped yielding the
    stored score, `test_no_note_when_the_score_has_not_moved` would be
    asserting the drift path and nobody would notice."""
    assert round(derive_composite_score(ROW_SAME[1], ROW_SAME[0], ROW_SAME[3],
                                        ROW_SAME[2]), 1) == STORED_SCORE
    assert round(derive_composite_score(ROW_DRIFTED[1], ROW_DRIFTED[0],
                                        ROW_DRIFTED[3], ROW_DRIFTED[2]),
                 1) == DRIFTED_SCORE
    assert DRIFTED_SCORE != STORED_SCORE


@pytest.fixture
def render(monkeypatch):
    """Render the page against a chosen live row. `row=None` = no DB."""
    def _go(row):
        class Cur:
            def execute(self, sql, params=None):
                self.sql, self.params = sql, params
            def fetchone(self):
                return row
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        class Conn:
            def cursor(self):
                return Cur()
            def close(self):
                pass
        monkeypatch.setattr(m, "read_deep_dive", lambda s: dict(BRIEF))
        monkeypatch.setattr(m, "_conn", (lambda: None) if row is None
                            else (lambda: Conn()))
        with flask.Flask(__name__).app_context():
            return m._render_deep_dive_body("santa-clara").get_data(as_text=True)
    return _go


def _tile(html):
    return re.search(r"DCPI Score<b>([^<]*)/100</b>", html).group(1)


def _meta(html):
    return re.search(r'<meta name="description" content="([^"]*)"', html).group(1)


def _dataset(html):
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>',
                        html, re.S)
    for b in blocks:
        node = json.loads(b)          # raises if a value broke the JSON
        if node.get("@type") == "Dataset":
            return node
    raise AssertionError("no Dataset ld+json on the page")


def _measure(node, name):
    for v in node.get("variableMeasured") or []:
        if v.get("name") == name:
            return v
    return None


# ── the headline figure is live, everywhere it appears ──────────────────
def test_the_tile_shows_the_live_score_not_the_stored_one(render):
    assert _tile(render(ROW_DRIFTED)) == str(DRIFTED_SCORE)


def test_the_meta_description_carries_the_live_score(render):
    meta = _meta(render(ROW_DRIFTED))
    assert f"DCPI score {DRIFTED_SCORE}/100" in meta, meta
    assert str(STORED_SCORE) not in meta, (
        f"the stored score is still in the meta description: {meta}")


def test_the_structured_data_carries_the_live_score(render):
    """The ld+json is what an agent cites without loading the page. If the
    tile went live and this did not, the page would contradict itself."""
    node = _dataset(render(ROW_DRIFTED))
    assert _measure(node, "DCPI Score")["value"] == DRIFTED_SCORE


def test_the_json_twin_publishes_the_same_score_as_the_page(monkeypatch):
    """/markets/<slug> and /markets/<slug>.json are two surfaces of one
    market. Taking only one of them live would open a fresh split — the exact
    defect this family exists to close."""
    class Cur:
        def execute(self, sql, params=None): pass
        def fetchone(self): return ROW_DRIFTED
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
        body = json.loads(c.get("/markets/santa-clara.json").data)
    assert _measure(body, "DCPI Score")["value"] == DRIFTED_SCORE


# ── the narrative's own numbers are NOT overlaid ────────────────────────
def test_the_snapshot_counts_are_left_alone(render):
    """They are what the prose talks about. Making them live would move the
    contradiction one layer in rather than remove it."""
    node = _dataset(render(ROW_DRIFTED))
    assert _measure(node, "Facilities")["value"] == 116
    assert _measure(node, "Total Capacity")["value"] == 1012.0


def test_each_figure_states_its_own_vintage(render):
    """Two clocks in one node. dateModified stays the brief's, because it is
    the vintage of most of what is in there; the score says its own or it
    inherits a date it does not have."""
    node = _dataset(render(ROW_DRIFTED))
    assert node["dateModified"].startswith("2026-09-05")
    desc = _measure(node, "DCPI Score")["description"]
    assert "Observed 2026-09-06" in desc, desc
    meta = _meta(render(ROW_DRIFTED))
    assert "live 2026-09-06" in meta and "as of 2026-09-05" in meta, meta


# ── the drift disclosure ────────────────────────────────────────────────
def test_the_page_discloses_the_gap_between_headline_and_prose(render):
    html = render(ROW_DRIFTED)
    note = re.search(r'<p class="drift">([^<]*)</p>', html)
    assert note, "live score differs from the narrative's and the page is silent"
    text = note.group(1)
    assert str(STORED_SCORE) in text and str(DRIFTED_SCORE) in text, text
    assert "2026-09-05" in text, text


def test_no_note_when_the_score_has_not_moved(render):
    """A page with nothing to disclose must say nothing — otherwise the note
    becomes furniture and stops being read."""
    html = render(ROW_SAME)
    assert _tile(html) == str(STORED_SCORE)
    assert 'class="drift"' not in html


# ── fail-soft ───────────────────────────────────────────────────────────
def test_no_live_reading_falls_back_to_the_stored_snapshot(render):
    """A DCPI blip must never blank a page that already has an answer, and
    must never present the stored number as live."""
    html = render(None)
    assert _tile(html) == str(STORED_SCORE)
    assert 'class="drift"' not in html
    assert "live 2026" not in _meta(html), (
        "the page claims a live reading it did not get")


def test_a_row_missing_a_component_is_not_scored(render):
    """derive_composite_score coerces None to 0, which mints a plausible score
    for a market with no data. Falling back to the stored value is right;
    publishing a fabricated one is not.

    ★ ASSERTED ON THE MECHANISM, NOT THE VALUE, and that is not fussiness.
    The first version of this test compared the tile to STORED_SCORE and
    PASSED with the guard deleted: derive_composite_score(59.4, None, 30,
    "AVOID") returns exactly 42.4, which is this fixture's stored score. A
    fabricated number collided with the real one and the mutation survived.
    "No live vintage was claimed" cannot collide.
    """
    row = (None, 59.4, "AVOID", 30, datetime.datetime(2026, 9, 6))
    assert round(derive_composite_score(row[1], 0, row[3], row[2]), 1) \
        == STORED_SCORE, ("the collision this test was rewritten for is gone; "
                          "keep the mechanism assertions anyway")
    html = render(row)
    assert _tile(html) == str(STORED_SCORE)
    assert "live 2026" not in _meta(html), (
        "a half-empty row produced a score the page published as live")
    assert "Observed" not in _measure(_dataset(html), "DCPI Score")["description"]


# ── the read itself cannot pick up a retired twin ───────────────────────
def test_the_live_read_is_canonical_and_published_only():
    """Same two lines as _gather_market_facts and PR #3841. Executed against a
    recording cursor: the SQL that reaches the driver is where the predicate
    and the resolved slug either are or are not."""
    from util.dcpi_score_row import PUBLISHED_ONLY
    calls = []
    class Cur:
        def execute(self, sql, params=None):
            calls.append((sql, params))
        def fetchone(self):
            return None
    m.live_dcpi_reading(Cur(), "northern-virginia")
    assert calls, "no query issued"
    sql, params = calls[0]
    assert PUBLISHED_ONLY in sql, sql
    assert "ashburn" in [str(p).lower() for p in params]
    assert "northern-virginia" not in [str(p).lower() for p in params], (
        "the live read binds the alias — an exact-slug match finds the retired "
        "twin's frozen row, which is what this page must never publish")
