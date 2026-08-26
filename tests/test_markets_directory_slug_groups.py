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

r-directory-real-slugs (2026-08-26) added the half that was missing: one row
per market did not mean the row's href RESOLVED. The slug was a constructed
city+state string, checked against nothing. Measured live over 609 of the
2,362 published links: 446 404 (73%), 160 301 (26%), 3 200 (0.5%), and 0 of
315 sampled hrefs present in sitemap-markets.xml. The sitemap earned this
guard on 2026-07-28; the directory was never covered by it.

★ The behavioural tests register market_deep_dive_bp as well, because BOTH it
  and seo_pages_bp define /markets/<slug> and main.py registers deep-dive
  first — so seo_pages.py:730 is shadowed in production. Driving the shadowed
  handler is how a directory of ~73% dead links passed a "the link resolves"
  assertion for a month.

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

    # r-directory-real-slugs (2026-08-26): the emitted slug is now the RESOLVED
    # market slug, not the raw city+state string — the raw form is only a join
    # input. The invariant is unchanged and so is what it protects: whatever
    # expression the SELECT emits as the href, the GROUP BY must be that same
    # expression text, or one listing can still re-collide into a duplicate.
    slug_sql = getattr(sp, "_MKT_RESOLVED_SLUG_SQL", None)
    assert slug_sql, f"_MKT_RESOLVED_SLUG_SQL missing from {REL}"
    assert f"GROUP BY {slug_sql}" in sql, (
        "the GROUP BY is no longer the slug expression itself, so a normalised "
        "key can re-collide on the way out into a duplicate href")
    assert f"SELECT {slug_sql} AS slug" in sql, (
        "the SELECT and the GROUP BY must be the SAME expression text — that "
        "identity is the invariant")

    assert "MODE() WITHIN GROUP" in sql, (
        "city/state must be reported as a real spelling from the group, not "
        "the normalised key — the page should read 'Ashburn', not 'ashburn'")
    assert "NULLIF(city, UPPER(city))" in sql, (
        "the caps-avoiding city pick is gone — merging the groups will now "
        "collapse Boydton/Lakewood/Hoffman Estates DOWN to the shouty spelling")


def test_directory_only_lists_markets_that_exist(monkeypatch):
    """Every href must come from a market table, not from a built string.

    The sitemap earned this guard on 2026-07-28
    (test_market_link_validation.py::test_sitemap_only_lists_markets_that_exist).
    The directory was never covered by it and kept publishing city+state slugs
    for markets no page was ever built at.

    MEASURED 2026-08-26 on the live route, 609 of 2,362 published links: 446
    404 (73%), 160 301 (26%), 3 200 (0.5%), and 0 of 315 sampled hrefs present
    in sitemap-markets.xml. /markets/directory is itself sitemapped at
    priority 0.8/weekly, so every crawl rediscovered all of them.
    """
    sql, _ = _executed_sql(monkeypatch)

    assert "market_power_scores" in sql, (
        "the directory must resolve its hrefs against a real market table — "
        "otherwise it emits URLs the /markets/<slug> route never serves")
    assert "JOIN mkt" in sql, (
        "the market slug set is no longer joined in; a filter applied after "
        "the fetch is not the invariant this fences")
    assert "SELECT DISTINCT market_slug" in sql, (
        "market_power_scores must be DISTINCT before the join — a market "
        "carrying several score rows would fan each facility out and inflate "
        "both the facility count and the MW total on the listing")


def test_directory_never_emits_a_raw_city_state_href(monkeypatch):
    """The raw city+state slug may be a join INPUT, never the emitted href.

    This is the regression that matters: the fix is undone the moment the
    SELECT goes back to emitting the constructed string directly.
    """
    import routes.seo_pages as sp

    sql, _ = _executed_sql(monkeypatch)
    raw = sp._MKT_SLUG_SQL

    assert f"SELECT {raw} AS slug" not in sql, (
        "the directory is emitting the constructed city+state slug as the href "
        "again — that string is not a market, and ~73% of them 404")
    assert f"{raw}      AS combo_slug" in sql or f"{raw} AS combo_slug" in sql, (
        "the raw slug should still be built, but only as a join key against "
        "market_power_scores")


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


def _app_like_production(sp):
    """Flask app with the blueprints registered in PRODUCTION order.

    ★ market_deep_dive_bp and seo_pages_bp BOTH define /markets/<slug>, and
      main.py registers market_deep_dive_bp first (main.py:2036) so IT is the
      handler production runs; seo_pages.py:730 is shadowed for that path.

      The superseded version of this test registered only seo_pages_bp, so its
      "the directory link resolves" assertion drove the SHADOWED handler, which
      answers 200 for any slug. That is why a directory publishing ~73% dead
      links passed a guard that appeared to check exactly that. Register both,
      in production order, or the check proves nothing.
    """
    from flask import Flask

    app = Flask(__name__)
    try:
        from routes.market_deep_dive import market_deep_dive_bp
        app.register_blueprint(market_deep_dive_bp)
    except Exception as _e:                                  # pragma: no cover
        pytest.skip(f"market_deep_dive_bp unavailable: {_e}")
    app.register_blueprint(sp.seo_pages_bp)
    return app


@pytest.mark.skipif(not _DB, reason="no database URL in the environment")
def test_directory_renders_no_duplicate_hrefs(monkeypatch):
    """Drive the shipped handler over every page and count duplicate links.

    The MUST-FAIL control comes first: "no duplicate hrefs" is trivially true
    of a table with no case variants, so prove the OLD grouping still splits on
    today's data before trusting the result.
    """
    from collections import Counter

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

    # r-directory-real-slugs (2026-08-26): the reference set is the MARKET
    # table, not every string the slug expression can build. The old version of
    # this test compared the page against the constructed city+state set and so
    # could only ever confirm that the directory kept publishing it.
    cur.execute("""
        SELECT DISTINCT market_slug
          FROM market_power_scores
         WHERE market_slug IS NOT NULL AND market_slug <> ''
    """)
    real_slugs = {r[0] for r in cur.fetchall()}
    assert real_slugs, (
        "CONTROL FAILED: market_power_scores returned no slugs, so 'every href "
        "is a real market' is vacuously true")

    # The facilities that RESOLVE to a market — the page's new denominator.
    cur.execute("""
        WITH mkt AS (
            SELECT DISTINCT market_slug
              FROM market_power_scores
             WHERE market_slug IS NOT NULL AND market_slug <> ''
        ),
        fac AS (
            SELECT LOWER(REPLACE(TRIM(city),' ','-')) || '-' ||
                   LOWER(TRIM(state))                   AS combo_slug,
                   LOWER(REPLACE(TRIM(city),' ','-'))   AS city_slug
              FROM discovered_facilities
             WHERE city IS NOT NULL AND city <> ''
               AND state IS NOT NULL AND state <> ''
               AND COALESCE(is_duplicate,0) = 0
        )
        SELECT COUNT(*)
          FROM fac
          LEFT JOIN mkt mc ON mc.market_slug = fac.combo_slug
          LEFT JOIN mkt mk ON mk.market_slug = fac.city_slug
         WHERE COALESCE(mc.market_slug, mk.market_slug) IS NOT NULL
    """)
    resolvable_facilities = cur.fetchone()[0]
    conn.close()

    monkeypatch.setattr(sp, "_conn", _readonly_conn)
    app = _app_like_production(sp)

    items = []
    with app.test_client() as cl:
        first = cl.get("/markets/directory")
        assert first.status_code == 200, first.status_code
        m = re.search(r"page 1 of (\d+)", first.get_data(as_text=True))
        assert m, "could not read the page count out of the rendered directory"
        pages = int(m.group(1))

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

    # ★ THE ASSERTION THIS FILE EXISTS FOR. Every href must name a market that
    #   exists. Measured 2026-08-26 before the fix: 446 of 609 sampled links
    #   404'd (73%) and 0 of 315 appeared in sitemap-markets.xml.
    invented = set(counts) - real_slugs
    assert not invented, (
        f"the directory links to {len(invented)} slug(s) that are not markets "
        f"in market_power_scores — these are the 404s: {sorted(invented)[:5]}")

    # Nothing that resolves may be silently dropped: every facility behind a
    # real market is accounted for exactly once.
    rendered = sum(int(re.search(r"(\d+) facilities", meta).group(1))
                   for _, _, meta in items if "facilities" in meta)
    assert rendered == resolvable_facilities, (
        f"the page accounts for {rendered:,} facilities but {resolvable_facilities:,} "
        "resolve to a real market — the regrouping dropped or double-counted rows")


@pytest.mark.skipif(not _DB, reason="no database URL in the environment")
def test_every_directory_link_resolves(monkeypatch):
    """Fetch a sample of the published hrefs through the REAL /markets handler.

    The shape fence proves the query joins a market table. This proves the
    resulting page does not publish a dead link — driven through
    market_deep_dive_bp, the handler production actually serves.
    """
    import routes.seo_pages as sp

    monkeypatch.setattr(sp, "_conn", _readonly_conn)
    app = _app_like_production(sp)

    hrefs = []
    with app.test_client() as cl:
        first = cl.get("/markets/directory")
        pages = int(re.search(r"page 1 of (\d+)",
                              first.get_data(as_text=True)).group(1))
        for p in range(1, pages + 1):
            resp = cl.get("/markets/directory" + (f"/{p}" if p > 1 else ""))
            hrefs.extend(h for h, _, _ in _LI.findall(resp.get_data(as_text=True)))

        assert hrefs, "the directory published no links at all"
        # Every page contributes; cap the fetch so the suite stays quick.
        sample = hrefs[::max(1, len(hrefs) // 60)][:60]
        dead = []
        for slug in sample:
            code = cl.get(f"/markets/{slug}").status_code
            if code >= 400:
                dead.append((slug, code))
        assert not dead, (
            f"{len(dead)} of {len(sample)} sampled directory links do not "
            f"resolve: {dead[:5]}")


@pytest.mark.skipif(not _DB, reason="no database URL in the environment")
def test_merged_flagship_markets_read_and_resolve(monkeypatch):
    """The merged rows must read as the real market, not a case variant.

    Ashburn is the flagship: before the 07-31 merge the page carried
    'Ashburn, VA — 199 facilities' and 'ASHBURN, VA — 5 facilities · 0 MW' as
    two links. Boydton is the case where the SHOUTY spelling is the more common
    one, so a plain MODE() would have merged it the wrong way.

    ★ The href is NOT asserted to be 'ashburn-va' any more — which slug a city
      resolves to is market_power_scores' business, and pinning the string here
      is what let the city+state form look sanctioned. Assert the label and
      that the link works; the slug set is fenced by the test above.
    """
    import routes.seo_pages as sp

    monkeypatch.setattr(sp, "_conn", _readonly_conn)
    app = _app_like_production(sp)

    by_label = {}
    with app.test_client() as cl:
        first = cl.get("/markets/directory")
        pages = int(re.search(r"page 1 of (\d+)",
                              first.get_data(as_text=True)).group(1))
        for p in range(1, pages + 1):
            resp = cl.get("/markets/directory" + (f"/{p}" if p > 1 else ""))
            for href, label, meta in _LI.findall(resp.get_data(as_text=True)):
                by_label[label] = (href, meta)

        for label in ("Ashburn, VA", "Boydton, VA"):
            assert label in by_label, (
                f"{label!r} is not on the directory — either the market was "
                "dropped, or the case-variant spelling won the display pick "
                f"(saw: {[k for k in by_label if k.split(',')[0].lower() == label.split(',')[0].lower()]})")
            href, meta = by_label[label]
            assert cl.get(f"/markets/{href}").status_code == 200, (
                f"the directory lists {label!r} at /markets/{href}, "
                "which does not resolve")

        _, ash_meta = by_label["Ashburn, VA"]
        n_ash = int(re.search(r"(\d+) facilities", ash_meta).group(1))
        assert n_ash > 200, (
            f"Ashburn lists {n_ash} facilities — it should carry the whole "
            "market, not one spelling of it")
