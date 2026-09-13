"""scan_news_sources keys each ARTICLE, not the feed it was read from.

Measured live (dchub-worker, 2026-09-13 07:01 UTC): `articles_scanned: 24,
facilities_found: 24, facilities_inserted: 0`, with 18 "Rejected news-derived
facility candidate" lines, 0 "already staged" and 0 insert errors, so the other
6 took the source_url dedup, which logs at DEBUG. Every candidate carried the
FEED's URL as its source_url, so once a feed had a row nothing more from it
could be written; and each was extracted from the whole fetched page, so one
item's "400 MW" (and its "broke ground") was lent to every title on the feed.

These run the real scan over a fake feed into the real
insert_discovered_facility and judge what reaches the database. The
canonical-slug probe (tests/test_one_url_one_row.py) answers "not staged" here,
so the only dedup in play is the article key.
"""
import html
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import news_facility_extractor as nfe  # noqa: E402

FEED = "https://feeds.example.test/news/"

# (title, link, text). QTS and STACK pass the real write gates on their own text.
QTS = ("QTS Cedar Rapids Data Center", "https://news.example.test/qts-cedar-rapids",
       "QTS broke ground on its Cedar Rapids campus in Iowa. "
       "The 600-acre site will draw 400 MW.")
STACK = ("Stack Infrastructure Tokyo Two", "https://news.example.test/stack-tokyo",
         "Stack Infrastructure plans a new data center campus in Tokyo, Japan "
         "with 72 MW of capacity.")
# announces a facility in its own text, but names no MW of its own
ALIGNED = ("Aligned Data Centers Salt Lake City", "https://news.example.test/aligned",
           "Aligned is under construction on a new data center facility in Utah.")
# announces nothing
RAIL = ("Satellite Internet coming to UK rail network", "https://news.example.test/rail",
        "British rail policy update with no facility.")


def _rss(*articles, links=True):
    """A feed shaped like DCD's: escaped HTML in <description>, guid == link."""
    items = []
    for title, link, text in articles:
        parts = [f"<title>{html.escape(title)}</title>"]
        if links:
            parts += [f"<link>{link}</link>", f"<guid>{link}</guid>"]
        parts.append(f"<description>{html.escape('<p>' + text + '</p>')}</description>")
        items.append("<item>" + "".join(parts) + "</item>")
    return ('<?xml version="1.0"?><rss version="2.0"><channel>'
            "<title>Example industry news feed</title>" + "".join(items)
            + "</channel></rss>")


class _Db:
    """psycopg2-shaped and no better. Answers the source_url dedup from the
    rows it was actually sent, and every other probe with nothing."""

    def __init__(self):
        self.rows = []   # source_url of each INSERT that went out
        self.asked = []  # source_url each dedup SELECT asked about

    def cursor(self, *_a, **_kw):
        return _Cursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _Cursor:
    def __init__(self, db):
        self.db, self.result = db, []

    def execute(self, sql, params=()):
        text = " ".join(str(sql).split())
        self.result = []
        if text.startswith("INSERT INTO discovered_facilities"):
            columns = [c.strip() for c in text.split("(", 1)[1].split(")", 1)[0].split(",")]
            self.db.rows.append(params[columns.index("source_url")])
            self.result = [(len(self.db.rows),)]
        elif "WHERE source_url = %s" in text:
            self.db.asked.append(params[0])
            if params[0] in self.db.rows:
                self.result = [(1,)]

    def fetchone(self):
        return self.result[0] if self.result else None

    def close(self):
        pass


def _scan(monkeypatch, feed, conn):
    import requests
    monkeypatch.setattr(nfe, "NEWS_SOURCES", [{"name": "Example", "rss": FEED}])
    monkeypatch.setattr(requests, "get", lambda url, **_kw: types.SimpleNamespace(
        status_code=200, text=feed if url == FEED else ""))
    return nfe.scan_news_sources(conn)


def _candidates(monkeypatch):
    """Every facility the scan hands to the write, before any gate judges it."""
    seen, real = [], nfe.insert_discovered_facility

    def spy(conn, facility, *args, **kwargs):
        seen.append(facility)
        return real(conn, facility, *args, **kwargs)

    monkeypatch.setattr(nfe, "insert_discovered_facility", spy)
    return seen


def test_two_articles_from_one_feed_are_two_insert_attempts(monkeypatch):
    db = _Db()
    results = _scan(monkeypatch, _rss(QTS, STACK), db)
    assert db.rows == [QTS[1], STACK[1]], (
        f"each article must reach the INSERT under its own link, got {db.rows}")
    assert results["facilities_inserted"] == 2
    assert FEED not in db.asked


def test_the_same_article_on_the_next_scan_is_one_insert(monkeypatch):
    db = _Db()
    _scan(monkeypatch, _rss(QTS), db)
    results = _scan(monkeypatch, _rss(QTS), db)
    assert db.rows == [QTS[1]], f"a re-read article was inserted again: {db.rows}"
    assert db.asked == [QTS[1], QTS[1]]
    assert results["facilities_inserted"] == 0


def test_an_article_with_no_link_gets_a_stable_key_of_its_own(monkeypatch):
    db, feed = _Db(), _rss(QTS, STACK, links=False)
    _scan(monkeypatch, feed, db)
    _scan(monkeypatch, feed, db)
    assert len(db.rows) == 2 == len(set(db.rows)), (
        f"two linkless articles, read twice, must be two rows: {db.rows}")
    assert all(key.startswith(FEED + "#article-") for key in db.rows), db.rows


def test_each_candidate_is_read_from_its_own_item(monkeypatch):
    seen = _candidates(monkeypatch)
    _scan(monkeypatch, _rss(QTS, ALIGNED, RAIL), _Db())
    by_name = {fac["name"]: fac for fac in seen}
    assert set(by_name) == {QTS[0], ALIGNED[0]}, (
        f"only items announcing a facility in their OWN text: {sorted(by_name)}")
    assert by_name[QTS[0]]["power_mw"] == 400
    assert by_name[QTS[0]]["state"] == "IA"
    assert by_name[ALIGNED[0]]["power_mw"] is None, "the QTS item's 400 MW leaked"


def test_a_page_heading_is_judged_on_the_heading_alone(monkeypatch):
    seen = _candidates(monkeypatch)
    page = ("<html><head><title>Example News</title></head><body>"
            "<h2>Vantage Frontier Abilene Campus</h2>"
            "<p>Vantage broke ground on a 400 MW campus in Abilene, Texas.</p>"
            "<h2>Vantage begins construction on its Abilene data center</h2>"
            "</body></html>")
    _scan(monkeypatch, page, _Db())
    assert [fac["name"] for fac in seen] == [
        "Vantage begins construction on its Abilene data center"], seen
    assert seen[0]["power_mw"] is None
    assert seen[0]["source_url"].startswith(FEED + "#article-")

