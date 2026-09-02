"""r-exports-news (2026-09-02) — the news export is a LINK INDEX, and must
stay one.

/api/v1/news served 50 of 13,009 for three months. It carried no upsell
markers, so only the stub guard's count-vs-total test caught it — it was the
one export whose size (35KB) looked plausible.

The replacement must not overcorrect. news_engine.py:290 builds `summary` from
`entry.get('summary', entry.get('description',''))` — the PUBLISHER's text.
Exporting 13,009 of those is republishing someone else's writing at scale.
These guards exist so a later "why is summary missing?" cannot quietly put it
back.
"""
import datetime
import json
import os
import re
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes.r2_exports import (  # noqa: E402
    _BUILDERS, _NEWS_LICENCE, _NEWS_SQL, _gate_evidence, _row_count,
)

# Publisher-owned or publisher-hosted. Neither is DC Hub's to redistribute.
FORBIDDEN = {"summary", "description", "content", "body", "image_url",
             "thumbnail", "full_text"}


def _selected(sql):
    head = sql.split("FROM")[0].replace("SELECT", "")
    return {c.strip() for c in head.split(",") if c.strip()}


def test_the_query_excludes_publisher_owned_fields():
    """★ THE GUARD THAT MATTERS. summary is the publisher's prose."""
    bad = _selected(_NEWS_SQL) & FORBIDDEN
    assert bad == set(), bad


def test_that_guard_can_fail():
    assert _selected("SELECT id, title, summary FROM news_articles") & FORBIDDEN == {"summary"}


def test_the_query_is_explicit_never_star():
    assert "*" not in _NEWS_SQL


def test_the_index_covers_every_article_not_the_api_page():
    flat = " ".join(_NEWS_SQL.split())
    assert "FROM news_articles" in flat
    assert "LIMIT" not in flat.upper(), "an export with a LIMIT is another stub"


def test_rows_without_a_url_are_excluded():
    """A link index entry with no link is not an entry."""
    flat = " ".join(_NEWS_SQL.split())
    assert "url IS NOT NULL" in flat and "url <> ''" in flat


def test_news_is_built_in_process():
    assert "news" in _BUILDERS


def test_the_licence_says_the_text_is_not_ours():
    n = _NEWS_LICENCE["note"]
    assert "summary" in n and "image_url" in n
    assert "not DC Hub's to license" in n


class _Cur:
    def __init__(self, rows): self._r = rows
    def execute(self, *a, **k): return None
    def fetchall(self): return self._r


class _Conn:
    def __init__(self, rows): self._r = rows
    def cursor(self): return _Cur(self._r)
    def close(self): return None


def _build(rows, monkeypatch):
    monkeypatch.setitem(sys.modules, "db_utils",
                        types.SimpleNamespace(get_read_db=lambda: _Conn(rows)))
    from routes.r2_exports import _build_news_index
    return _build_news_index()


_ROW = (1, "Utility files for 500MW", "https://ex.com/a", "Example Wire",
        datetime.datetime(2026, 9, 1, 12, 0), "Industry", 0.91)


def test_a_datetime_is_serialised_not_left_raw(monkeypatch):
    """★ A raw datetime would blow up json.dumps inside build(), which reports
    it as a whole-dataset failure with no clue where it came from."""
    p = _build([_ROW], monkeypatch)
    assert p["data"][0]["published_at"] == "2026-09-01T12:00:00"
    json.dumps(p)  # must not raise


def test_a_null_published_at_stays_null(monkeypatch):
    p = _build([(2, "t", "u", "s", None, "c", 0.1)], monkeypatch)
    assert p["data"][0]["published_at"] is None


def test_the_payload_carries_no_forbidden_key(monkeypatch):
    p = _build([_ROW], monkeypatch)
    assert set(p["data"][0].keys()) & FORBIDDEN == set()


def test_the_index_does_not_trip_the_stub_guard(monkeypatch):
    """13,009 rows and no upsell envelope — the guard must let it through."""
    p = _build([_ROW] * 13009, monkeypatch)
    raw = json.dumps(p).encode()
    assert _gate_evidence(raw) is None
    assert _row_count(json.loads(raw)) == 13009
