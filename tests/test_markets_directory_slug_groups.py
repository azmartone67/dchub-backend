"""/markets/directory lists one row per market, not one row per spelling.

r-directory-split-groups (2026-07-31), the public-page twin of #2074.

The route built its href as `LOWER(REPLACE(city,' ','-'))||'-'||LOWER(state)`
but grouped on the RAW columns, so each letter-case variant of a city earned
its own listing. The directory published TWO rows carrying the SAME href, the
smaller one reading as an empty market — 82 duplicate listings over 81 slugs
out of 2,438 on the read replica, 70 of them with a 0 MW sibling.

Two halves, because neither is sufficient alone:

  * the SHAPE fence asserts against the SQL the handler actually EXECUTES
    (not its source text), so a revert of the grouping key is caught with no
    database in the loop;
  * the BEHAVIOURAL test drives the real shipped handler over every rendered
    page and counts duplicate hrefs in the HTML, because the defect is a
    property of the page a reader sees. It carries a MUST-FAIL control: it
    first proves the OLD grouping still splits on today's table, so the day
    the data stops having case variants this test reports itself vacuous
    instead of passing for the wrong reason.

★ pytest functions only — no module-scope work. A module-scope failure is a
  COLLECTION error, which kills the whole session rather than one test.
"""
import os
import re

import pytest

REL = "routes/seo_pages.py"

_DB = (os.environ.get("NEON_REPLICA_URL") or os.environ.get("DATABASE_URL")
       or os.environ.get("NEON_DATABASE_URL"))


# ── the SQL the handler executes ──────────────────────────────────────────

class _RecordingCursor:
    """Captures the SQL and returns one plausible row."""

    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sink.append(sql)

    def fetchall(self):
        return [("ashburn-va", "Ashburn", "VA", 204, 6942.0)]


class _RecordingConn:
    def __init__(self, sink):
        self.sink = sink

    def cursor(self, *a, **kw):
        return _RecordingCursor(self.sink)

    def close(self):
        pass


def _executed_sql(monkeypatch):
    """Render the page against a stub DB and hand back the SQL it ran."""
    from flask import Flask

    import routes.seo_pages as sp

    sink = []
    monkeypatch.setattr(sp, "_conn", lambda: _RecordingConn(sink))
    app = Flask(__name__)
    app.register_blueprint(sp.seo_pages_bp)
    with app.test_client() as cl:
        resp = cl.get("/markets/directory")
    assert resp.status_code == 200, (
        f"the directory did not render at all ({resp.status_code}) — the shape "
        "assertions below would be vacuous")
    assert len(sink) == 1, f"expected exactly one query, captured {len(sink)}"
    return sink[0], resp.get_data(as_text=True)


def test_directory_groups_on_the_slug_it_emits(monkeypatch):
    """The grouping key must BE the emitted slug expression.

    Grouping on a normalised (city, state) pair also erases today's 82
    duplicates, but the pair still has to be rendered back into a slug and that
    trip can re-collide — LOWER(UPPER('Bakırköy')) folds the Turkish dotless ı
    and moves a live indexed URL. Grouping on the emitted expression makes
    one-row-per-slug true by construction, so that is what is fenced.
    """
    import routes.seo_pages as sp

    sql, _ = _executed_sql(monkeypatch)

    assert "GROUP BY city, state" not in sql, (
        "the directory is grouping on the RAW columns again — every letter-case "
        "variant of a city gets its own listing, and the page publishes two "
        "rows pointing at the same /markets/<slug> URL")

    slug_sql = getattr(sp, "_MKT_SLUG_SQL", None)
    assert slug_sql, f"_MKT_SLUG_SQL missing from {REL}"
    assert f"GROUP BY {slug_sql}" in sql, (
        "the GROUP BY is no longer the slug expression itself, so a normalised "
        "key can re-collide on the way out into a duplicate href")
    assert sql.count(slug_sql) == 2, (
        "the SELECT and the GROUP BY must be the SAME expression text — that "
        f"identity is the invariant; found {sql.count(slug_sql)} occurrence(s)")

    assert "MODE() WITHIN GROUP" in sql, (
        "city/state must be reported as a real spelling from the group, not "
        "the normalised key — the page should read 'Ashburn', not 'ashburn'")
    assert "NULLIF(city, UPPER(city))" in sql, (
        "the caps-avoiding city pick is gone — merging the groups will now "
        "collapse Boydton/Lakewood/Hoffman Estates DOWN to the shouty spelling")


def test_directory_slug_expression_normalises_case_and_whitespace(monkeypatch):
    """The pieces the grouping key is built from, spelled out.

    A renamed or loosened constant leaves the shape fence above passing while
    the split quietly reopens, so assert the normalisation explicitly.
    """
    import routes.seo_pages as sp

    slug_sql = sp._MKT_SLUG_SQL
    for frag in ("LOWER(", "TRIM(city)", "TRIM(state)", "REPLACE(", "' ','-'"):
        assert frag in slug_sql, (
            f"{frag!r} missing from _MKT_SLUG_SQL — {slug_sql!r}")
    assert "UPPER(" not in slug_sql, (
        "the slug key round-trips through UPPER(), which folds Turkish dotless "
        "ı to i and moves the live /markets/istanbul-bakırköy URL")


# ── behavioural proof (needs a DB; skipped without one) ───────────────────

_LI = re.compile(r'<li><a href="/markets/([^"]+)">([^<]*)</a>([^<]*)</li>')


def _readonly_conn():
    import psycopg2

    c = psycopg2.connect(_DB, connect_timeout=30)
    c.set_session(readonly=True, autocommit=True)
    return c


@pytest.mark.skipif(not _DB, reason="no database URL in the environment")
def test_directory_renders_no_duplicate_hrefs(monkeypatch):
    """Drive the shipped handler over every page and count duplicate links.

    The MUST-FAIL control comes first: "no duplicate hrefs" is trivially true
    of a table with no case variants, so prove the OLD grouping still splits on
    today's data before trusting the result.
    """
    from collections import Counter

    from flask import Flask

    import routes.seo_pages as sp

    conn = _readonly_conn()
    cur = conn.cursor()

    # CONTROL — the superseded key, on the same rows the page reads.
    cur.execute("""
        SELECT COUNT(*) - COUNT(DISTINCT slug) FROM (
          SELECT LOWER(REPLACE(city,' ','-')) || '-' || LOWER(state) AS slug
            FROM discovered_facilities
           WHERE city IS NOT NULL AND city <> ''
             AND state IS NOT NULL AND state <> ''
             AND COALESCE(is_duplicate,0) = 0
           GROUP BY city, state
        ) t
    """)
    would_split = cur.fetchone()[0]
    assert would_split > 0, (
        "CONTROL FAILED: the raw (city, state) key no longer splits any slug on "
        "this table, so a green result here proves nothing about the fix")

    cur.execute("""
        SELECT COUNT(*), COALESCE(SUM(power_mw),0)::float
          FROM discovered_facilities
         WHERE city IS NOT NULL AND city <> ''
           AND state IS NOT NULL AND state <> ''
           AND COALESCE(is_duplicate,0) = 0
    """)
    table_facilities, _table_mw = cur.fetchone()

    # The URL set that ships TODAY. Deduping must delete rows, never URLs: a
    # key that normalises into a (city, state) pair and renders a slug back out
    # of it also erases the duplicates, but LOWER(UPPER('Bakırköy')) folds the
    # Turkish dotless ı and quietly retargets a live indexed page.
    cur.execute("""
        SELECT DISTINCT LOWER(REPLACE(city,' ','-')) || '-' || LOWER(state)
          FROM discovered_facilities
         WHERE city IS NOT NULL AND city <> ''
           AND state IS NOT NULL AND state <> ''
           AND COALESCE(is_duplicate,0) = 0
    """)
    live_slugs = {r[0] for r in cur.fetchall()}
    conn.close()

    monkeypatch.setattr(sp, "_conn", _readonly_conn)
    app = Flask(__name__)
    app.register_blueprint(sp.seo_pages_bp)

    items = []
    with app.test_client() as cl:
        first = cl.get("/markets/directory")
        assert first.status_code == 200, first.status_code
        m = re.search(r"page 1 of (\d+)", first.get_data(as_text=True))
        assert m, "could not read the page count out of the rendered directory"
        pages = int(m.group(1))
        assert pages > 1, "expected a paginated directory"

        for p in range(1, pages + 1):
            resp = cl.get("/markets/directory" + (f"/{p}" if p > 1 else ""))
            assert resp.status_code == 200, (p, resp.status_code)
            found = _LI.findall(resp.get_data(as_text=True))
            assert found, f"page {p} rendered zero listings"
            items.extend(found)

    counts = Counter(href for href, _, _ in items)
    dupes = {h: n for h, n in counts.items() if n > 1}
    assert not dupes, (
        f"{sum(n - 1 for n in dupes.values())} duplicate listing(s) on the "
        f"public directory — two rows linking to the same /markets/<slug>: "
        f"{sorted(dupes)[:5]}")

    # The merge must not lose anything: this route has no status filter, so
    # every qualifying row is on the page exactly once.
    rendered = sum(int(re.search(r"(\d+) facilities", meta).group(1))
                   for _, _, meta in items if "facilities" in meta)
    assert rendered == table_facilities, (
        f"the page accounts for {rendered:,} facilities but the table holds "
        f"{table_facilities:,} — the regrouping dropped or double-counted rows")

    # Rows go away; URLs do not. The thin-page guard drops nothing on today's
    # data, so these two sets are equal — an inequality either way means the
    # grouping key invented or retargeted a public URL.
    invented = set(counts) - live_slugs
    assert not invented, (
        f"the directory now links to {len(invented)} URL(s) the slug "
        f"expression never produced: {sorted(invented)[:5]}")
    dropped = live_slugs - set(counts)
    assert not dropped, (
        f"{len(dropped)} market URL(s) fell off the directory entirely: "
        f"{sorted(dropped)[:5]}")


@pytest.mark.skipif(not _DB, reason="no database URL in the environment")
def test_merged_flagship_markets_read_and_resolve(monkeypatch):
    """The merged rows must read as the real market and their links must work.

    Ashburn is the flagship: before the fix the page carried 'Ashburn, VA —
    199 facilities' and 'ASHBURN, VA — 5 facilities · 0 MW' as two links to
    /markets/ashburn-va. Boydton is the case where the SHOUTY spelling is the
    more common one, so a plain MODE() would have merged it the wrong way.
    """
    from flask import Flask

    import routes.seo_pages as sp

    monkeypatch.setattr(sp, "_conn", _readonly_conn)
    app = Flask(__name__)
    app.register_blueprint(sp.seo_pages_bp)

    labels = {}
    with app.test_client() as cl:
        first = cl.get("/markets/directory")
        pages = int(re.search(r"page 1 of (\d+)",
                              first.get_data(as_text=True)).group(1))
        for p in range(1, pages + 1):
            resp = cl.get("/markets/directory" + (f"/{p}" if p > 1 else ""))
            for href, label, meta in _LI.findall(resp.get_data(as_text=True)):
                labels[href] = (label, meta)

        ash_label, ash_meta = labels.get("ashburn-va", ("", ""))
        assert ash_label == "Ashburn, VA", (
            f"flagship market listed as {ash_label!r} — the case-variant "
            "spelling won the display pick")
        n_ash = int(re.search(r"(\d+) facilities", ash_meta).group(1))
        assert n_ash > 200, (
            f"ashburn-va lists {n_ash} facilities — it should carry the whole "
            "market, not one spelling of it")

        assert labels.get("boydton-va", ("",))[0] == "Boydton, VA", (
            f"boydton-va listed as {labels.get('boydton-va')} — the "
            "caps-avoiding display pick is not working")

        # A merged slug must still resolve; the fix must not invent a dead link.
        for slug in ("ashburn-va", "sterling-va", "boydton-va"):
            assert slug in labels, f"{slug} vanished from the directory"
            assert cl.get(f"/markets/{slug}").status_code == 200, (
                f"the directory links to /markets/{slug}, which no longer 200s")
