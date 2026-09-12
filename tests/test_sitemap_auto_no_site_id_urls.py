#!/usr/bin/env python3
"""The dynamic sitemap must not list /sites/<id> pages. NO NETWORK, NO DB.

r-sites-dead (2026-09-11). routes/sitemap_auto._generate_sitemap appended 200
`/sites/<facilities.id>` URLs, built from
`SELECT id, COALESCE(updated_at, first_seen) FROM facilities`. Measured on the
live app that day:

  * `facilities` has NO `updated_at` column. GET /api/health/diag reports the
    live information_schema for that table: 45 columns, `first_seen` and
    `last_updated` both TEXT, no `updated_at`, no `created_at`. So the SELECT
    raised on every build, `_safe` returned [], and the block emitted ZERO URLs
    for as long as the column has been absent — while `/api/v1/sitemap/health`
    counted 2,103 `facilities` rows with power_mw.
  * Every `/sites/<id>` URL is ROBOTS-BLOCKED. Live robots.txt carries
    `Disallow: /sites/` with `Allow: /sites/$`, so the bare landing page is
    crawlable and the per-id pages are not; a sitemap entry for a robots-blocked
    URL is reported as "Submitted URL blocked by robots.txt" against the whole
    sitemap. They also answer 200 from the static shell for ANY id
    (`/sites/zzz-not-a-facility-zzz` included), canonicalising to `/sites/`.

★ CORRECTION (2026-09-12). The first version of this file said the page's data
  call 404s and the page type was dead. That was a bad probe: the endpoint is
  `/api/v1/sites/<id_or_slug>/capacity-report` — the /api/v1 prefix was missing
  — and it returns 200 with a Site Capacity Report teaser for a real slug, 404
  `site_not_found` for junk. The page WORKS and is withheld on purpose.

Reviving the block would publish 200 robots-blocked soft-404s — the
`/facilities/in/<cc>` 676-shell lesson. The bare `/sites/` landing page is
WITHHELD from both sitemaps as well (tests/test_sitemap_covers_linked_pages.py),
and this file pins that too.

★ THE CONTROL IS THE POINT. A generator that produced nothing at all would pass
  the /sites/<id> assertion trivially, so the fake database feeds every other
  block and the test asserts those URLs ARE emitted.
★ THE SOURCE CHECK IS SEPARATE ON PURPOSE. Every block sits under
  `except Exception: pass` with `_safe` swallowing inside it, so a revived block
  that still names a missing column emits nothing and no behavioural test can
  see it. The AST check reads what the module ASKS FOR, not what it returns.
"""
import ast
import datetime as _dt
import importlib.util
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SOURCE = ROOT / "routes" / "sitemap_auto.py"
# /sites/ followed by at least one character — the bare landing page is fine.
SITE_ID_URL = re.compile(r"<loc>https://dchub\.cloud/sites/.+?</loc>")


def _load():
    spec = importlib.util.spec_from_file_location("_sitemap_auto_probe", SOURCE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "_generate_sitemap"), "loaded something that is not sitemap_auto"
    return mod


class _Cur:
    """Answers each block's SELECT from the table it names, so every block that
    can emit does emit."""

    def __init__(self):
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        s = " ".join(str(sql).split())
        when = _dt.datetime(2026, 9, 1, 12, 0, 0)

        def reads(table):
            """★ Word-boundary, never substring. Matching "FROM
            market_power_scores" inside "FROM market_power_scores_gone" fed rows
            to a query against a table that does not exist, and the control
            mutation that renames the table SURVIVED on it."""
            return re.search(rf"\bFROM\s+{table}\b", s, re.I) is not None

        if reads("market_power_scores"):
            self._rows = [("northern-virginia", when)]
        elif reads("exclusive_listings"):
            self._rows = [("a-listing", when)]
        elif reads("news"):
            self._rows = [("https://dchub.cloud/news/a-release", when)]
        elif reads("facilities"):
            # If the block ever comes back, hand it rows so it emits and the
            # behavioural test goes red rather than passing on an empty read.
            self._rows = [("1f0e2d3c4b5a6978", when)]
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self, *_a, **_k):
        return _Cur()

    def close(self):
        pass


def _xml(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "_conn", lambda: _Conn())
    return mod._generate_sitemap()


def test_the_sitemap_lists_no_site_id_pages(monkeypatch):
    hits = SITE_ID_URL.findall(_xml(monkeypatch))
    assert not hits, (
        "the dynamic sitemap lists /sites/<id> pages again — each answers 200 "
        f"from a shell that canonicalises to /sites/ and has no data: {hits[:5]}")


def test_every_other_block_still_emits(monkeypatch):
    """The control. Without it, a generator that emitted nothing at all — a
    broken _conn, an exception in the first block — would satisfy the test
    above and prove nothing."""
    xml = _xml(monkeypatch)
    for expected in ("https://dchub.cloud/markets/northern-virginia",
                     # ?l= on purpose: /listings/<slug> 404s at the edge, the
                     # same edge-routing lesson that retires /sites/<id> here
                     "https://dchub.cloud/listings?l=a-listing",
                     "https://dchub.cloud/news/a-release"):
        assert f"<loc>{expected}</loc>" in xml, (
            f"{expected} is missing — the fake database fed this generator "
            "nothing, so the /sites/<id> check above was vacuous")
    assert "<loc>https://dchub.cloud/sites/</loc>" not in xml, (
        "the bare /sites/ landing page is listed again. It is WITHHELD in "
        "tests/test_sitemap_covers_linked_pages.py because robots.txt carries "
        "`Disallow: /sites/`, and main.py's static_pages withholds it for that "
        "reason — a sitemap entry for a robots-blocked URL counts against the "
        "whole sitemap. (The served robots.txt also has `Allow: /sites/$`; "
        "unblocking starts in dchub-frontend, not here.)")


def test_the_generator_does_not_query_the_facilities_table():
    """Comment-proof: reads the SQL _generate_sitemap ASKS FOR, from string
    constants in its AST, so the prose above can neither satisfy nor trip it. A
    revived block naming a column the live table lacks emits nothing and would
    slip past the behavioural test.

    Scoped to the generator, not the module: sitemap_health() legitimately
    counts `facilities` rows, and that COUNT is the oracle that showed this
    block was raising (0 /sites URLs beside facilities_with_power = 2,103)."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    fns = [n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef) and n.name == "_generate_sitemap"]
    assert len(fns) == 1, f"expected one _generate_sitemap(), found {len(fns)}"
    asks = [n.value for n in ast.walk(fns[0])
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and re.search(r"\bFROM\s+facilities\b", " ".join(n.value.split()), re.I)]
    assert not asks, (
        "_generate_sitemap queries `facilities` again. The live table has no "
        "updated_at/created_at column and the /sites/<id> pages it fed are "
        f"soft-404s: {[a[:80] for a in asks]}")
