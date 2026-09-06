"""/digest published three measurements it had not taken.

1. MOVERS. _movers_from_snapshots ended `except Exception: return []`, and the
   template renders empty as:

       "Markets held steady — no market shifted by 1+ excess-power point
        over the last 7 days."

   A missing table, a renamed column, or under 7 days of snapshot history all
   produced that sentence. It is a claim about the market, manufactured by a
   bare except, on a page whose footer offers it "free for citation".

2. NEWS. The try wrapped the WHOLE four-table fan-out, so the FIRST table that
   does not exist aborted the loop and the other three were never queried.
   news_count_24h stayed at its 0 initialiser and the page — and its og card —
   stated the industry produced zero news items in 24h, having read nothing.

3. BUILD COUNT. og:description said "{top_build|length} BUILD-verdict markets".
   top_build is `rows.sort(by excess_power_score)[:5]` — it never reads the
   verdict column, so that number was always 5 (or len(rows)), whatever the
   verdicts were.

All three are the same defect: a value that doubles as its own absence. The
fix in each case is a separate `*_measured` flag, so "we looked and found none"
and "we could not look" stop sharing a representation.
"""
import os
import re
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from routes import digest as dg  # noqa: E402


class _Boom(Exception):
    pass


class _Cur:
    """Rows by SQL shape; `fail_on` makes exactly one table raise."""

    def __init__(self, rows, fail_on=(), movers=None):
        self.rows, self.fail_on, self.movers = rows, tuple(fail_on), movers or []
        self._out = []
        self.connection = type("C", (), {"rollback": lambda s: None})()

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        for bad in self.fail_on:
            if bad in s:
                raise _Boom(bad)
        if "from market_power_scores" in s:
            self._out = self.rows
        elif "dcpi_daily_snapshots" in s:
            self._out = self.movers
        elif "count(*)" in s:
            self._out = [{"n": 7}]
        else:
            self._out = []

    def fetchall(self):
        return list(self._out)

    def fetchone(self):
        return self._out[0] if self._out else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _row(slug, excess, verdict, constraint=10):
    return {"market_name": slug.title(), "market_slug": slug,
            "excess_power_score": excess, "constraint_score": constraint,
            "verdict": verdict, "time_to_power_months": 12}


ROWS = [_row("ashburn", 90, "BUILD"), _row("dallas", 80, "BUILD"),
        _row("phoenix", 70, "AVOID"), _row("reno", 60, "CAUTION"),
        _row("atlanta", 50, "AVOID"), _row("boise", 40, "AVOID")]


@pytest.fixture
def build(monkeypatch):
    def _go(fail_on=(), movers=None, rows=ROWS):
        cur = _Cur(rows, fail_on=fail_on, movers=movers)
        conn = type("Conn", (), {
            "cursor": lambda s, **k: cur,
            "__enter__": lambda s: s, "__exit__": lambda s, *a: False})()
        monkeypatch.setattr(dg, "_conn", lambda: conn)
        return dg._today_summary()
    return _go


# ── movers ────────────────────────────────────────────────────────────

def test_a_failed_movers_query_is_not_reported_as_measured(build):
    d = build(fail_on=("dcpi_daily_snapshots",))
    assert d["biggest_movers"] == []
    assert d["movers_measured"] is False, (
        "a failed snapshot comparison still claims to have measured movement")
    assert d.get("movers_error")


def test_a_genuinely_quiet_week_IS_measured(build):
    """The distinction the fix exists for. Both give [] — only one may say
    'held steady'."""
    d = build(movers=[])
    assert d["biggest_movers"] == []
    assert d["movers_measured"] is True


def test_both_render_sites_are_gated(build):
    """★ There are TWO renderers -- the HTML page and the email -- and the
    first pass of this fix gated only the page. Found by counting occurrences
    of the sentence in the module and getting 4 where 2 were expected.

    Behavioural, not a source scan. The earlier version of this test looked for
    the flag in a window of source text around the sentence and failed on BOTH
    sites for reasons unrelated to correctness: the email checks the flag below
    the string, and the disclaimer is split across two adjacent literals so no
    contiguous match exists. Render it and read the output instead.
    """
    from flask import Flask
    app = Flask(__name__)

    measured = build(movers=[])                             # ran, found nothing
    unmeasured = build(fail_on=("dcpi_daily_snapshots",))   # never ran
    assert measured["movers_measured"] is True
    assert unmeasured["movers_measured"] is False

    for name, render in (
        ("email", lambda d: dg._render_digest_email_html(d)),
        ("page", lambda d: _render_page(app, d)),
    ):
        ok, no = render(measured), render(unmeasured)
        assert "held steady" in ok, f"{name}: a quiet week lost its wording"
        assert "held steady" not in no, (
            f"{name}: asserts market stability after a FAILED comparison")
        assert "could not be measured" in no, f"{name}: silent about the failure"
        assert "not a statement that markets were stable" in _flat(no), (
            f"{name}: does not disclaim the stability reading")


def _flat(html):
    return " ".join(html.split())


def _render_page(app, d):
    """Render the page template the route uses, without the DB round trip.

    Pulled off the AST node, not by indexing for quote characters -- the module
    contains several triple-quoted blocks and a character scan picks whichever
    comes first.
    """
    import ast
    from flask import render_template_string
    tree = ast.parse(open(dg.__file__, encoding="utf-8").read())
    tpl = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "HTML"
                        for t in node.targets)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            tpl = node.value.value
            break
    assert tpl and "biggest_movers" in tpl, "page template not found on the AST"
    with app.app_context():
        return render_template_string(tpl, d=d)


# ── news fan-out ──────────────────────────────────────────────────────

def test_a_dead_first_table_does_not_kill_the_other_three(build):
    """THE BUG: the try wrapped the loop, so industry_news failing meant
    news_articles / news / press_releases were never tried."""
    d = build(fail_on=("from industry_news",))
    assert d["news_measured"] is True, (
        "one missing table aborted the whole fan-out")
    assert d["news_count_24h"] == 7


def test_all_tables_dead_reports_unmeasured_not_zero(build):
    d = build(fail_on=("from industry_news", "from news_articles",
                       "from news ", "from press_releases"))
    assert d["news_measured"] is False
    assert d["news_count_24h"] is None, (
        "no table answered, yet the digest carries a number")


def test_the_news_tile_and_og_card_both_respect_the_flag():
    src = open(dg.__file__, encoding="utf-8").read()
    og = [l for l in src.splitlines() if 'og:description' in l][0]
    assert "news_measured" in og, og
    tile = [l for l in src.splitlines() if 'News (24h)' in l][0]
    assert "news_measured" in tile, tile


# ── the BUILD count ───────────────────────────────────────────────────

def test_build_count_reads_the_verdict_column(build):
    """ROWS holds exactly 2 BUILD. top_build is the top 5 by excess score and
    contains 2 BUILD + 3 others -- the old card would have said 5."""
    d = build()
    assert d["build_verdict_count"] == 2, d["build_verdict_count"]
    assert len(d["top_build"]) == 5
    assert d["build_verdict_count"] != len(d["top_build"])


def test_the_og_card_uses_the_counted_value_not_the_slice_length():
    src = open(dg.__file__, encoding="utf-8").read()
    og = [l for l in src.splitlines() if 'og:description' in l][0]
    assert "build_verdict_count" in og, og
    assert "top_build|length" not in og, og


def test_the_og_mover_count_does_not_claim_to_be_a_census():
    """biggest_movers is de-duplicated and capped at 5, so it is a display
    slice. The card may not present it as the number of markets that moved."""
    src = open(dg.__file__, encoding="utf-8").read()
    og = [l for l in src.splitlines() if 'og:description' in l][0]
    assert re.search(r"top \{\{ d\.biggest_movers\|length \}\} movers", og), og
