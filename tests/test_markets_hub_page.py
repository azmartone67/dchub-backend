#!/usr/bin/env python3
"""/markets is a live, SELF-canonical hub listing what sitemap-markets lists (seo F6).

NO NETWORK, NO DB. routes.market_deep_dive is imported directly; its _conn
and routes.pockets._fetch_pockets are monkeypatched. main.py is checked by
source shape only (it is never imported here).

MEASURED 2026-09-02 00:40Z (and again 04:30Z): GET https://dchub.cloud/markets
and /markets/ -> 200, <title>Market Intelligence - DC Hub</title>,
<link rel=canonical href="https://dchub.cloud/market-intelligence">. So the
hub of a 580-URL shard (249 /markets/, 330 /pockets/) told Google it was a
copy of another page (SH52-072 OPEN-RED), and 561 market/pocket pages had
no internal link pointing at them (SH52-092). /markets was absent from
sitemap-static.xml (0 matches) and present once in sitemap-markets.xml.

THE LIMIT: a hub makes the pages reachable and gives Google one page to
crawl from; the geo head terms ("singapore data centers" pos 86-96) move,
or do not, in GSC after deploy.
"""
import os
import pathlib
import re
import sys

from flask import Flask

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.market_deep_dive as md  # noqa: E402
import routes.pockets as pk  # noqa: E402

MAIN = os.path.join(ROOT, "main.py")


class _Cur:
    def __init__(self, results, fail_first=False):
        self._results, self._i, self.fail_first = results, -1, fail_first
        self.sql = []

    def execute(self, sql, *_a, **_k):
        self.sql.append(sql)
        self._i += 1
        if self.fail_first and self._i == 0:
            raise RuntimeError('column "first_seen" does not exist')

    def fetchall(self):
        return self._results[self._i] if 0 <= self._i < len(self._results) else []

    def close(self):
        pass


class _Conn:
    def __init__(self, results, fail_first=False):
        self.cur = _Cur(results, fail_first)
        self.rolled_back = 0

    def cursor(self, **_kw):
        return self.cur

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        pass


_DB_ROWS = [("miami", "2026-08-30 10:00"), ("ashburn", "2026-08-29"),
            ("st.-louis", "2026-08-01"), ("dallas", "2026-08-02"),
            ("charlotte", None), ("-", None), ("miami", "2026-01-01")]

_POCKETS = [
    {"market_slug": "miami", "market_name": "Miami", "state": "FL"},
    {"market_slug": "st.-louis", "market_name": "St. Louis", "state": "MO"},
    {"market_slug": "", "market_name": "blank", "state": ""},
    {"market_slug": "cheyenne", "market_name": None, "state": "WY"},
]


# ── the shared inventory ─────────────────────────────────────────────────

def test_listable_market_slug_applies_the_sitemap_filters():
    seen = set(md.CURATED_MARKET_SLUGS)
    assert md.listable_market_slug("miami", seen) == "miami"
    assert md.listable_market_slug("miami", seen) is None, "dedupe"
    assert md.listable_market_slug("dallas", seen) is None, "curated already"
    # r-market-canon-split (2026-09-05): the pair points the other way now —
    # 'ashburn' is the page and 'northern-virginia' 301s to it.
    assert md.listable_market_slug("northern-virginia", seen) is None, \
        "301s to ashburn"
    for junk in ("st.-louis", "-", "x-", "-x", "ab", "", None, "---"):
        assert md.listable_market_slug(junk, seen) is None, junk
    assert "northern-virginia" not in seen and "st.-louis" not in seen


def test_inventory_lists_exactly_what_the_shard_would(monkeypatch):
    conn = _Conn([_DB_ROWS])
    monkeypatch.setattr(md, "_conn", lambda: conn)
    monkeypatch.setattr(pk, "_fetch_pockets", lambda limit_hint=100: list(_POCKETS))
    inv = md.markets_hub_inventory()
    assert [s for s, _n in inv["metros"]] == list(md.CURATED_MARKET_SLUGS)
    assert inv["us_markets"] == [("charlotte", "Charlotte"), ("miami", "Miami")]
    assert inv["pockets"] == [("miami", "Miami", "FL"), ("cheyenne", "Cheyenne", "WY")]
    assert "MAX(f.first_seen)" in conn.cur.sql[0]


def test_us_city_rows_fall_back_to_the_dateless_query(monkeypatch):
    conn = _Conn([[], [("miami",), ("tucson",)]], fail_first=True)
    rows = md.us_city_market_rows(conn)
    assert rows == [("miami", None), ("tucson", None)]
    assert conn.rolled_back == 1
    assert "first_seen" not in conn.cur.sql[1]
    assert "JOIN market_power_scores" in conn.cur.sql[1], (
        "the degraded path must keep the market-existence join (the 404 fix)")


def test_inventory_degrades_to_the_curated_list(monkeypatch):
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(md, "_conn", _boom)
    monkeypatch.setattr(pk, "_fetch_pockets", _boom)
    inv = md.markets_hub_inventory()
    assert len(inv["metros"]) == len(md.CURATED_MARKET_SLUGS)
    assert inv["us_markets"] == [] and inv["pockets"] == []


# ── the page ─────────────────────────────────────────────────────────────

def _client(monkeypatch):
    monkeypatch.setattr(md, "_HUB_CACHE", {"html": None, "at": 0.0})
    monkeypatch.setattr(md, "_conn", lambda: _Conn([_DB_ROWS]))
    monkeypatch.setattr(pk, "_fetch_pockets", lambda limit_hint=100: list(_POCKETS))
    app = Flask("t")
    app.register_blueprint(md.market_deep_dive_bp)
    return app.test_client()


def test_hub_is_self_canonical_and_indexable(monkeypatch):
    c = _client(monkeypatch)
    for path in ("/markets", "/markets/"):
        r = c.get(path)
        assert r.status_code == 200, path
        html = r.data.decode()
        head = html.split("<body", 1)[0]
        assert '<link rel="canonical" href="https://dchub.cloud/markets">' in head
        assert "market-intelligence" not in head, "the old cross-canonical is back"
        assert 'content="index, follow"' in head
        assert r.headers.get("X-DC-Hub-Source") == "markets-hub"


def test_hub_links_every_market_and_pocket_page(monkeypatch):
    html = _client(monkeypatch).get("/markets").data.decode()
    for slug in md.CURATED_MARKET_SLUGS:
        assert f'href="https://dchub.cloud/markets/{slug}"' in html, slug
    assert 'href="https://dchub.cloud/markets/miami"' in html
    assert 'href="https://dchub.cloud/markets/charlotte"' in html
    assert 'href="https://dchub.cloud/pockets/miami"' in html
    assert 'href="https://dchub.cloud/pockets/cheyenne"' in html
    # what the sitemap skips, the hub skips
    assert "/markets/northern-virginia" not in html
    assert "st.-louis" not in html
    assert "<h1>Data Center Markets</h1>" in html
    assert 'href="https://dchub.cloud/market-intelligence"' in html
    assert 'href="https://dchub.cloud/markets/directory"' in html


def test_hub_jsonld_itemlist_counts_the_links(monkeypatch):
    html = _client(monkeypatch).get("/markets").data.decode()
    m = re.search(r'"@type": "ItemList".*?"numberOfItems": (\d+)', html, re.S)
    assert m, "ItemList JSON-LD missing"
    assert int(m.group(1)) == len(md.CURATED_MARKET_SLUGS) + 2 + 2


# ── main.py wiring (source shape) ────────────────────────────────────────

def _main_src():
    return open(MAIN, encoding="utf-8").read()


def _no_comments(s):
    return "\n".join(l for l in s.splitlines() if not l.lstrip().startswith("#"))


def test_market_intelligence_page_no_longer_owns_the_markets_routes():
    s = _main_src()
    i = s.index("def market_intelligence_page():")
    decorators = _no_comments(s[s.rindex("\n\n", 0, i):i])
    assert "@app.route('/market-intelligence')" in decorators
    assert "/markets" not in decorators, (
        "/markets is served by routes/market_deep_dive.markets_hub_page now; "
        "an app.route here would shadow it with the cross-canonical page")
    body = s[i:i + 4000]
    assert '"url": "https://dchub.cloud/market-intelligence"' in body, (
        "the Dataset JSON-LD must name the page it sits on")


def _builder():
    s = _main_src()
    i = s.index("def _build_sitemap_sections(")
    j = s.index("\ndef ", i + 100)
    return _no_comments(s[i:j])


def test_sitemap_markets_shard_uses_the_shared_inventory():
    b = _builder()
    assert "CURATED_MARKET_SLUGS" in b
    assert "us_city_market_rows" in b
    assert "listable_market_slug" in b
    assert "HAVING COUNT(*) >= 3" not in b, "a second copy of the market SQL"
    assert "MARKETS_CANONICAL_REDIRECT" not in b, "a second copy of the filter"


def test_markets_hub_is_in_the_static_shard_once_and_not_in_the_markets_shard():
    b = _builder()
    assert re.search(r"\(\s*'/markets'\s*,\s*'[0-9.]+'\s*,\s*'[a-z]+'\s*\)", b), (
        "/markets must be a static_pages tuple")
    assert "dchub.cloud/markets</loc>" not in b, (
        "listed twice — the markets shard used to carry the hub too")
