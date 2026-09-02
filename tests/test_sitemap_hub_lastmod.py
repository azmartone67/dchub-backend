#!/usr/bin/env python3
"""/facilities/in/<cc> sitemap entries carry MAX(first_seen), not the pin (seo F11).

NO NETWORK, NO DB — facilities_hub._conn is monkeypatched (the
tests/test_facilities_hub_seo.py pattern) and main.py's builder is checked
by source shape (tests/test_sitemap_thin_gate.py pattern).

MEASURED 2026-09-02 ~04:35Z: sitemap-static.xml = 560 URLs, 559 of them
<lastmod>2026-08-19</lastmod> — including 302 /facilities/in/* hubs whose
member lists change daily. main.py's own note reserves that pin for
"hardcoded/curated URLs"; these hubs are DB-driven. The city-state markets
shard already carries MAX(f.first_seen) (322 entries dated 2026-09-02 in
sitemap-markets.xml on the same probe) — this brings the hubs to the same
honesty.
"""
import datetime
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import facilities_hub as fh  # noqa: E402

MAIN = os.path.join(ROOT, "main.py")


class _Cur:
    def __init__(self, results):
        self._results = results
        self._i = -1
        self.sql = []

    def execute(self, sql, *_a, **_k):
        self.sql.append(sql)
        self._i += 1

    def fetchall(self):
        return self._results[self._i] if 0 <= self._i < len(self._results) else []


class _Conn:
    def __init__(self, results):
        self.cur = _Cur(results)

    def cursor(self, **_kw):
        return self.cur

    def close(self):
        pass


def test_lastmod_is_max_first_seen_per_country_and_state(monkeypatch):
    conn = _Conn([
        [("us", datetime.datetime(2026, 8, 30, 11, 5)),
         ("de", "2026-08-28 10:00:00"), ("", "2026-01-01"), ("cl", None)],
        [("TX", "2026-08-31"), ("Texas", datetime.date(2026, 8, 29)),
         ("Nowhere", "2026-08-30"), ("VA", None)],
    ])
    monkeypatch.setattr(fh, "_conn", lambda: conn)
    countries, states = fh.hub_sitemap_lastmod()
    assert countries == {"us": "2026-08-30", "de": "2026-08-28"}
    # 'Texas' and 'TX' collapse to one slug and keep the newest date
    assert states == {"texas": "2026-08-31"}


def test_lastmod_uses_the_same_filters_as_the_counts(monkeypatch):
    """The date must describe the rows the page renders: same three filters
    as hub_sitemap_counts, in both queries."""
    conn = _Conn([[], []])
    monkeypatch.setattr(fh, "_conn", lambda: conn)
    fh.hub_sitemap_lastmod()
    assert len(conn.cur.sql) == 2
    for sql in conn.cur.sql:
        assert "MAX(first_seen)" in sql
        assert "duplicate_of_id IS NULL" in sql
        assert "name IS NOT NULL AND name <> ''" in sql
    assert "LOWER(btrim(country)) = 'us'" in conn.cur.sql[1]


def test_lastmod_fails_soft_to_empty(monkeypatch):
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(fh, "_conn", _boom)
    assert fh.hub_sitemap_lastmod() == ({}, {})


def _builder():
    s = open(MAIN, encoding="utf-8").read()
    i = s.index("def _build_sitemap_sections(")
    j = s.index("\ndef ", i + 100)
    return "\n".join(l for l in s[i:j].splitlines()
                     if not l.lstrip().startswith("#"))


def test_builder_stamps_hub_urls_with_the_real_date():
    b = _builder()
    assert "hub_sitemap_lastmod" in b, "the builder no longer asks for hub dates"
    hub_lines = [l for l in b.splitlines()
                 if "dchub.cloud/facilities/in/" in l and "<lastmod>" in l]
    assert len(hub_lines) == 4, f"expected 4 hub emissions, got {len(hub_lines)}"
    for l in hub_lines:
        assert "<lastmod>{_STATIC_LASTMOD}" not in l, (
            f"a hub emission is pinned again: {l.strip()[:90]}")
    country_lines = [l for l in hub_lines if "/facilities/in/{_hc}" in l]
    state_lines = [l for l in hub_lines if "/facilities/in/us/{_ss}" in l]
    assert len(country_lines) == 2 and len(state_lines) == 2
    assert all("<lastmod>{_hlm}</lastmod>" in l for l in country_lines)
    assert all("<lastmod>{_slm}</lastmod>" in l for l in state_lines)
    # the names above must be bound to the REAL date, pin as fallback only
    assert "_hlm = _hub_lm.get(_hc) or _STATIC_LASTMOD" in b
    assert "_slm = _us_lm.get(_ss) or _STATIC_LASTMOD" in b
    assert "_hub_lm, _us_lm = _hub_lmf()" in b


def test_curated_tuples_keep_the_pin():
    b = _builder()
    assert "for path, pri, freq in static_pages:" in b
    seg = b.split("for path, pri, freq in static_pages:", 1)[1][:400]
    assert "<lastmod>{_STATIC_LASTMOD}" in seg, (
        "hand-curated pages have no per-URL change signal; they keep the pin")
