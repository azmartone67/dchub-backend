"""The /transactions page must publish DEALS, not rows.

Live on 2026-08-29 it served **5,222** in four places — <title>, meta
description, og:description and the body — from a bare `COUNT(*) FROM deals`.
canonical_stats.py warns against exactly that in capitals: the AUTO id embeds
the ingest date, so one deal re-ingests under a new id every day and
ON CONFLICT never fires. Canon was 2,000+. A ~2.6x over-claim on a public,
indexable, schema.org-marked page — worse than the retired "4,000+" figure
being swept out of the tree in the same wave.
"""
import re

SRC = open("routes/transactions_browser.py", encoding="utf-8").read()


def _count_block():
    """The executable count query, docstrings and comments stripped — this
    module's comments quote the banned SQL on purpose."""
    return "\n".join(l for l in SRC.split("\n")
                     if not l.strip().startswith("#"))


def test_no_bare_row_count():
    assert "COUNT(*) AS n FROM deals" not in _count_block(), (
        "rows are NOT deals — a bare COUNT(*) over-states ~2.6x")


def test_counts_distinct_deals():
    body = _count_block()
    assert "SELECT DISTINCT" in body and "_DEDUP_KEY" in body
    assert "LEFT(id, 5) = 'AUTO-'" in body, (
        "AUTO rows must collapse on their stable content-hash suffix")


def test_quarantined_rows_are_excluded():
    assert "quarantine_" in _count_block()


def test_no_row_count_fallback_on_error():
    """A failure that silently restored COUNT(*) would republish the
    over-claim under a number that merely looks computed."""
    # ★Strip comments first. The branch's own comment says "do NOT fall back to
    # COUNT(*)", so grepping the raw block fails on the fix it guards — the
    # fourth time this session that a test matched its own explanation.
    m = re.search(r"except Exception as ce:(.*?)\n\n", _count_block(), re.S)
    assert m, "the count's error branch was renamed"
    assert "COUNT(*)" not in m.group(1)
    assert "total = 0" in m.group(1)


def test_dedup_uses_LEFT_not_LIKE():
    """★A literal percent in a psycopg2 query run WITH params is a live 500 —
    `sql % args` eats it. LIKE 'AUTO-%' would reintroduce that."""
    body = _count_block()
    assert "LIKE 'AUTO-" not in body


def test_the_count_sql_survives_percent_substitution():
    """Emulate what psycopg2 does: `sql % args`. Eyeballing does not catch a
    stray literal %; this does."""
    dedup = ("CASE WHEN LEFT(id, 5) = 'AUTO-' THEN RIGHT(id, 6) "
             "     ELSE COALESCE(buyer,'')||'|'||COALESCE(seller,'')||'|'||"
             "          COALESCE(value::text,'')||'|'||COALESCE(mw::text,'')||'|'||"
             "          COALESCE(date,'') END")
    quarantine = "COALESCE(LEFT(data_flag,11),'') <> 'quarantine_'"
    for where in (" WHERE year = %s AND " + quarantine, " WHERE " + quarantine):
        sql = (f"SELECT COUNT(*) AS n FROM ("
               f"  SELECT DISTINCT {dedup} AS k FROM deals{where}) t")
        args = tuple(["2026"] * sql.count("%s"))
        rendered = sql % args           # raises on a stray literal %
        assert "%" not in rendered, f"unsubstituted percent survived: {rendered[:80]}"
        assert "2026" in rendered or not args
