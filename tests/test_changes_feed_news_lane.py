"""The news_new lane of /api/v1/changes.

★ 2026-08-23: this lane read `news` — the legacy radar table — while
get_news and /api/v1/news serve `news_articles`. Measured on prod: `news`
held 3 rows in 14 days while news_articles carried 34 in the last 24h alone,
so the DEFAULT 24h window answered `news_new: 0`. discover_tools names
get_changes as THE refresh path, so every returning agent that followed our
own documented sync pattern was told the corpus was static.

The repoint ALONE would have replaced a silent zero with a dead lane:
`news_articles.published_at` is TEXT on prod (TIMESTAMP elsewhere), and
binding the tz-aware `since` straight at a TEXT column raises
`operator does not exist: text > timestamp with time zone`, which lane()
publishes as null + a domain_error. Proven against a real PostgreSQL 18
instance across all three column shapes before shipping.

These tests pin the properties that keep the repair safe, because both
failure modes are SILENT to an agent: a lane that returns nothing and a lane
that could not look are indistinguishable from "no news today".

Source-level assertions (no DB): DB-backed tests skip in CI, so a test that
only ran against a live database would be green-by-absence here.
"""
import ast
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "routes" / "changes_feed.py").read_text()


def _news_lane_region() -> str:
    """The source from the news comment through the end of its lane call."""
    i = SRC.index("# News: new articles")
    j = SRC.index("# DCPI:", i)
    return SRC[i:j]


def _news_ts_expression() -> str:
    """ONLY the SQL of the shared `_news_ts` expression — no surrounding
    comment. Asserting SQL shape against the comment-bearing region is
    vacuous: a mutation that stripped `AT TIME ZONE 'UTC'` from the SQL but
    left the prose describing it survived exactly that way."""
    region = _news_lane_region()
    i = region.index("_news_ts = (")
    j = region.index("lane(\"news_new\"", i)
    body = region[i:j]
    # concatenate the r"..." fragments the way Python does
    return "".join(re.findall(r'r"([^"]*)"', body))


def test_module_parses_and_the_lane_region_is_found():
    # Guards every assertion below against passing vacuously on an empty or
    # unparseable read (the empty-parse-passes-all trap).
    assert ast.parse(SRC)
    region = _news_lane_region()
    assert len(region) > 200
    assert "news_new" in region


def test_the_lane_reads_the_table_that_is_actually_served():
    """get_news serves news_articles. The 'what changed' feed must read the
    same table, or it reports on a corpus no one is being shown."""
    region = _news_lane_region()
    assert "FROM news_articles" in region
    # The legacy table must not survive as the read target.
    assert not re.search(r"FROM\s+news\b(?!_articles)", region)
    # ...and the citation must name the table actually read.
    assert '"news_articles"' in region


def test_the_bare_column_is_never_compared_to_a_bound_parameter():
    """THE bug this lane must never regress into. `published_at > %s` against
    prod's TEXT column raises UndefinedFunction, which lane() turns into a
    null lane — silent to the agent."""
    region = _news_lane_region()
    for op in (">", "<=", ">=", "<"):
        assert not re.search(rf"published_at\s*{re.escape(op)}\s*%s", region), (
            f"bare `published_at {op} %s` reintroduces the TEXT/timestamptz "
            "operator error")


def test_the_cast_goes_through_text_so_it_survives_either_column_type():
    """published_at is TEXT on prod and TIMESTAMP elsewhere. `::text` is a
    no-op on the former and renders the latter, so one expression serves
    both; the regex operator `~` alone would not work on a real timestamp."""
    expr = _news_ts_expression()
    assert expr, "could not extract the _news_ts SQL"
    assert "published_at::text ~" in expr, "the shape check must run on text"
    assert "published_at::text::timestamp" in expr, "the cast must go via text"


def test_the_cast_is_regex_guarded_so_it_cannot_throw():
    """CASE guarantees the shape check runs BEFORE the cast — the same
    pattern _row_ts uses for capacity_pipeline. Without it, one malformed
    row takes down the whole lane and comes back as 'no new articles'."""
    expr = _news_ts_expression()
    assert re.search(r"CASE WHEN published_at::text ~", expr), (
        "the cast must sit inside a regex-guarded CASE")
    assert expr.index("published_at::text ~") < expr.index(
        "published_at::text::timestamp")


def test_the_naive_strings_are_pinned_to_utc():
    """Prod stores naive strings written in UTC. Without an explicit zone the
    cast inherits the session TimeZone, so the same query would answer
    differently depending on the connection that ran it."""
    expr = _news_ts_expression()
    assert "AT TIME ZONE 'UTC'" in expr, (
        "the zone must be in the SQL, not only in the comment above it")


def test_one_expression_drives_where_order_by_and_the_returned_field():
    """If the filter, the sort and the emitted value can disagree about which
    clock they are on, the feed can return rows it says are outside its own
    window. _news_ts is interpolated, so all three are the same expression."""
    region = _news_lane_region()
    assert "_news_ts = (" in region
    for clause in ("SELECT title, url, {_news_ts} AS published_date",
                   "WHERE {_news_ts} > %s",
                   "ORDER BY {_news_ts} DESC"):
        assert clause in region, f"expected {clause!r} to use the shared expression"


def test_future_dated_rows_cannot_pin_themselves_to_the_top():
    """The news domain declares a 6h future grace for itself on the REST
    path. Without the same bound here, one bad row sits at the top of 'what
    changed' permanently — the class that served a 2026-09-21 article."""
    region = _news_lane_region()
    assert "{_news_ts} <= %s" in region
    assert "hours=6" in region


def test_the_wire_contract_field_name_is_unchanged():
    """`published_date` is the feed's field name, not the column name.
    Renaming it to match the new column would break existing consumers."""
    region = _news_lane_region()
    assert '"published_date": r[2]' in region
    assert "AS published_date" in region
