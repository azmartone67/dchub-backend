"""An unpublished monthly file must not be reported as a broken URL.

★ THE STATE THIS EXISTS FOR, live on /api/v1/ops/deadman 2026-09-03 JST:

    iso-intl  status=degraded
    note: failed: OCCTO (mix: hokkaido:http_404 — no fuel mix written
                          for these areas)

`http_404` is what you write when a URL is WRONG, so that note sends every
reader off to re-derive a URL that is correct. Measured the same morning:

    eria_jukyu_202609_01.csv -> 404   this month, not published yet
    eria_jukyu_202608_01.csv -> 200   last row 2026/08/31 23:30
    eria_jukyu_202607_01.csv -> 200

HEPCO had simply not posted September, which _MIX_STALE_AFTER_H's own comment
already records ("Hokkaido lags ~1wk"). These files are MONTHLY, so this
recurs at the start of EVERY month.

★ THE LANE MUST STAY DEGRADED. The fuel mix really is missing and laundering
it green would be the worse bug — falling back further cannot help either,
because August's newest row already exceeds the 6h freshness bar, so the
choice is between a false 404 and stale data published as current. What
changes is only that the note now says WHICH of the two it is.

Behavioural: drives _month_gap_note and _fetch_eria's real return path.
"""
from datetime import datetime

import pytest

from routes.iso_jp_denkiyoho import (
    _MONTH_ROLLOVER_GRACE_DAYS,
    _month_gap_note,
    _JST,
)


def _sep3():
    return datetime(2026, 9, 3, 7, 40, tzinfo=_JST)


def test_it_names_the_unpublished_month_not_a_broken_url():
    note = _month_gap_note(_sep3(), "http_404")
    assert "month_not_published_yet:202609" in note
    assert not note.startswith("http_404")


def test_it_keeps_the_raw_status_so_nothing_is_hidden():
    """Renaming the symptom must not delete the evidence."""
    assert "raw=http_404" in _month_gap_note(_sep3(), "http_404")


def test_it_carries_the_day_so_a_reader_can_judge_the_lag():
    assert "day 3 JST" in _month_gap_note(_sep3(), "http_404")


def test_it_says_the_url_template_is_unchanged():
    """The single most useful fact: do NOT go re-derive the URL."""
    assert "URL template unchanged" in _month_gap_note(_sep3(), "http_404")


def test_the_month_tracks_the_clock_not_a_constant():
    """A hardcoded month would read correct in September and lie in October."""
    oct2 = datetime(2026, 10, 2, 3, 0, tzinfo=_JST)
    assert "month_not_published_yet:202610" in _month_gap_note(oct2, "http_404")
    jan1 = datetime(2027, 1, 1, 3, 0, tzinfo=_JST)
    assert "month_not_published_yet:202701" in _month_gap_note(jan1, "http_404")


def test_the_rollover_grace_window_is_a_named_constant():
    """It was the bare literal 2 inside the fetch loop, which is why nobody
    noticed the window had lapsed by the 3rd."""
    assert isinstance(_MONTH_ROLLOVER_GRACE_DAYS, int)
    assert _MONTH_ROLLOVER_GRACE_DAYS >= 1


# ── the real return path, driven end to end with a stubbed HTTP layer ──
class _Resp:
    def __init__(self, status):
        self.status_code = status
        self.ok = status == 200
        self.content = b""


def test_fetch_eria_returns_the_honest_note_when_this_month_404s(monkeypatch):
    """★ END TO END. A 404 on this month's file must surface as the month-gap
    note, not http_404 — this is the string that reaches /ops/deadman."""
    import routes.iso_jp_denkiyoho as m

    monkeypatch.setattr(m, "_rq", type("RQ", (), {
        "get": staticmethod(lambda url, **kw: _Resp(404))})())
    parsed, note = m._fetch_eria("hokkaido")
    assert parsed is None
    assert note.startswith("month_not_published_yet:"), note


def test_a_non_404_failure_is_still_reported_verbatim(monkeypatch):
    """★ THE GREEN DIRECTION, inverted: the rename must be NARROW. A 500 is a
    real upstream fault and must not be dressed up as a publishing lag."""
    import routes.iso_jp_denkiyoho as m

    monkeypatch.setattr(m, "_rq", type("RQ", (), {
        "get": staticmethod(lambda url, **kw: _Resp(500))})())
    parsed, note = m._fetch_eria("hokkaido")
    assert parsed is None
    assert note == "http_500", note
    assert "month_not_published" not in note
