"""Press rail in the media feeds — MUST carry press releases (audit SH52-062).

The bug: RSS/JSON feeds shipped 0 of 143 published press releases while
/api/press-releases/list returned all 143. Cause: the press rail rendered the
date with `r[5].isoformat()`, but press_releases.date comes back as a STRING in
production, so the first row raised AttributeError, the whole rail hit its bare
`except: c.rollback()`, and every press release silently vanished — invisible
because the exception was swallowed.

These tests pin the fix and are a MUST-FAIL guard: they feed a STRING date (the
exact production shape) and assert press items still flow. A regression to
`.isoformat()` re-breaks them.

CI-SAFETY: _conn is stubbed with a fake in-memory connection; no DB, no network.
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeCursor:
    def __init__(self, rows_for):
        self._rows_for = rows_for
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if "press_releases" in sql:
            self._rows = self._rows_for.get("press", [])
        elif "ai_testimonials_auto" in sql:
            self._rows = self._rows_for.get("testimonial", [])
        elif "market_power_scores" in sql:
            self._rows = self._rows_for.get("dcpi", [])
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _FakeConn:
    def __init__(self, rows_for):
        self._rows_for = rows_for
        self.autocommit = False

    def cursor(self):
        return _FakeCursor(self._rows_for)

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture()
def hub(monkeypatch):
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import routes.dchub_media_hub as m
    return m


def _press_row(slug, date_val):
    # SELECT slug, title, subheadline, body, meta_description, date, kind
    return (slug, f"{slug} title", "subhead", "body text", "meta", date_val,
            "press_release")


def test_press_flows_with_a_STRING_date(hub, monkeypatch):
    """The exact production shape: date is text, not a date object."""
    monkeypatch.setattr(hub, "_conn", lambda: _FakeConn({
        "press": [_press_row("neso-609", "2026-08-04"),
                  _press_row("coreweave", "2026-08-03")],
        "testimonial": [], "dcpi": [],
    }))
    items = hub._aggregate_for_feeds(limit_per_rail=15)
    press = [i for i in items if i["kind"] == "press_release"]
    assert len(press) == 2, "string-dated press releases were dropped again"
    assert press[0]["published_at"] == "2026-08-04"
    assert press[0]["url"] == "https://dchub.cloud/news/neso-609"


def test_press_flows_with_a_date_object(hub, monkeypatch):
    import datetime as _dt
    monkeypatch.setattr(hub, "_conn", lambda: _FakeConn({
        "press": [_press_row("x", _dt.date(2026, 8, 4))],
        "testimonial": [], "dcpi": [],
    }))
    press = [i for i in hub._aggregate_for_feeds() if i["kind"] == "press_release"]
    assert len(press) == 1
    assert press[0]["published_at"] == "2026-08-04"


def test_a_failing_rail_does_not_kill_the_others(hub, monkeypatch):
    """Press rows present; a rail that raises must not empty the feed."""
    class _BoomConn(_FakeConn):
        def cursor(self):
            c = _FakeCursor(self._rows_for)
            orig = c.execute
            def execute(sql, params=None):
                if "market_power_scores" in sql:
                    raise RuntimeError("dcpi boom")
                return orig(sql, params)
            c.execute = execute
            return c
    monkeypatch.setattr(hub, "_conn", lambda: _BoomConn({
        "press": [_press_row("keep", "2026-08-04")],
        "testimonial": [], "dcpi": [("m", "M", 50.0, "BUILD", None)],
    }))
    items = hub._aggregate_for_feeds()
    assert any(i["kind"] == "press_release" for i in items), \
        "a failing dcpi rail wiped out the press rail"


def test_iso_helper_handles_every_shape(hub):
    import datetime as _dt
    assert hub._iso(None) is None
    assert hub._iso("2026-08-04") == "2026-08-04"
    assert hub._iso(_dt.date(2026, 8, 4)) == "2026-08-04"
    assert hub._iso(_dt.datetime(2026, 8, 4, 12, 0)).startswith("2026-08-04T")
