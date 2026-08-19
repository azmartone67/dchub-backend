"""Guard: a literal percent sign in a parameterized query, and the silent
empty board it produces.

What happened (2026-08-19, in production for ~50 minutes):

A SQL COMMENT inside customer_white_glove._measure's query read
``-- only 'sent<pct>' is a delivery``. psycopg2 scans the ENTIRE query string
for format specifiers — comments included — so that one character raised
``tuple index out of range``. `_measure` caught it, returned ``[]``, and the
customer board served:

    payers 0 · stranded 0 · needs_human 0 · systemic_activation_failure false

over 20 real paying customers. A perfectly healthy-looking dashboard, showing
nothing. This is the whole failure family this repo keeps rediscovering: the
lie is not the exception, it is the zero that replaces it.

Two guards, because the bug had two halves:
  1. no bare percent in a parameterized query (the trigger)
  2. an unmeasured board must not read as a healthy one (the amplifier)
"""
import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP = (".claude/", "worktrees/", "node_modules/", "/.git/", "/tests/")

# A percent that is neither doubled nor part of a psycopg2 placeholder.
# %s %(name)s %d %% are all fine; a lone % before a quote or letter is not.
_BARE_PCT = re.compile(r"(?<!%)%(?![%s(diouxXeEfFgGcr])")


def _param_queries():
    """(file, lineno, sql) for every execute()/executemany() call that passes a
    literal SQL string AND a params argument — the only shape where a literal
    percent is a bug."""
    out = []
    for f in ROOT.rglob("*.py"):
        if any(d in str(f).replace("\\", "/") for d in SKIP):
            continue
        if not f.stem.isidentifier():
            continue
        try:
            src = f.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src)
        except Exception:
            continue
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            if getattr(n.func, "attr", None) not in ("execute", "executemany"):
                continue
            if len(n.args) < 2:          # no params -> literal % is harmless
                continue
            q = n.args[0]
            if isinstance(q, ast.Constant) and isinstance(q.value, str):
                out.append((f.relative_to(ROOT), n.lineno, q.value))
            elif isinstance(q, ast.JoinedStr):
                parts = "".join(v.value for v in q.values
                                if isinstance(v, ast.Constant)
                                and isinstance(v.value, str))
                out.append((f.relative_to(ROOT), n.lineno, parts))
    return out


def test_the_scan_finds_queries():
    """Non-vacuity floor — a broken walk would make the guard below silently
    green, which is the same shape as the bug it guards."""
    qs = _param_queries()
    assert len(qs) >= 50, (
        f"only {len(qs)} parameterized queries found — the AST walk is broken"
    )


# ★ PRE-EXISTING, NOT APPROVED. Found by this guard the day it was written.
# Each is a parameterized query carrying a literal percent, i.e. the same
# runtime bomb — psycopg2 raises when the query executes. They are pinned so
# this guard can land green and stop NEW ones, exactly how regression_lint
# treats its 1,402 pre-existing violations. They are NOT blessed:
#   * routes/linkedin_quad_daily.py:634 — the offending comment literally reads
#     "so psycopg2 interpolates and a lone % would throw", and contains one.
#   * routes/osm_crawler.py:384 — a comment saying "100% of discovered rows".
#   * the rest are LIKE patterns ('%acqui%', '%construct%') alongside a %s
#     param, which means either the call is dead code or it has been throwing
#     in production unnoticed. Worth an audit — that is how this one hid.
# Removing a file from this list is always correct; adding one needs a reason.
_KNOWN_BARE_PCT = {
    "fix_capacity.py:188",
    "intelligence_engine.py:473",
    "proactive_discovery.py:618",
    "tools/email_blast_developer_launch.py:118",
    "routes/iso_snapshot.py:298",
    "routes/linkedin_quad_daily.py:634",
    "routes/osm_crawler.py:384",
    "routes/market_brief.py:2577",
}


def test_the_known_baseline_is_still_real():
    """If a pinned entry disappears (fixed, moved, deleted), say so rather than
    letting the allowlist quietly grow stale and hide a real regression."""
    found = {f"{f}:{line}" for f, line, sql in _param_queries()
             if _BARE_PCT.search(sql)}
    gone = sorted(_KNOWN_BARE_PCT - found)
    assert not gone, (
        "these were pinned as pre-existing but no longer match — remove them "
        f"from _KNOWN_BARE_PCT: {', '.join(gone)}"
    )


def test_no_bare_percent_in_a_parameterized_query():
    """★ THE REGRESSION. Comments count: psycopg2 does not parse SQL, it scans
    the string."""
    offenders = []
    for f, line, sql in _param_queries():
        m = _BARE_PCT.search(sql)
        if m and f"{f}:{line}" not in _KNOWN_BARE_PCT:
            ctx = sql[max(0, m.start() - 50):m.start() + 25].replace("\n", " ")
            offenders.append(f"{f}:{line}  …{ctx}…")
    assert not offenders, (
        "literal percent sign in a parameterized query — psycopg2 will raise "
        "'tuple index out of range' or 'unsupported format character' at "
        "RUNTIME, and a fail-soft caller will render it as an empty result:\n  "
        + "\n  ".join(offenders)
        + "\nDouble it (%%) in SQL, or reword the comment to avoid it."
    )


def test_the_specific_query_that_broke():
    """Pin the exact call site, so this cannot regress behind a scan change."""
    src = (ROOT / "routes" / "customer_white_glove.py").read_text(encoding="utf-8")
    i = src.find("SELECT u.email")
    j = src.find('""", (PAID_PLANS,))')
    assert i != -1 and j > i, "the _measure query moved — re-point this guard"
    assert not _BARE_PCT.search(src[i:j]), (
        "bare percent is back in the _measure roster query"
    )


# ── the amplifier: an unmeasured board must not look like a healthy one ──

def test_unmeasured_board_is_not_reported_as_healthy(monkeypatch):
    import routes.customer_white_glove as cwg
    monkeypatch.setitem(cwg._MEASURE_ERROR, "last", "tuple index out of range")
    monkeypatch.setitem(cwg._MEASURE_ERROR, "at", "2026-08-19T21:43:14+00:00")
    h = cwg._self_health([])
    assert h["measured"] is False, (
        "a board whose query failed reports measured:true — every zero in it "
        "then reads as 'no problems'"
    )
    assert h["measure_error"], "the error text is not surfaced"
    assert h["systemic_activation_failure"] is False


def test_a_genuinely_empty_board_still_reads_as_measured(monkeypatch):
    """The flag must be reachable in BOTH directions or it is just noise."""
    import routes.customer_white_glove as cwg
    monkeypatch.setitem(cwg._MEASURE_ERROR, "last", None)
    monkeypatch.setitem(cwg._MEASURE_ERROR, "at", None)
    h = cwg._self_health([])
    assert h["measured"] is True
    assert h["measure_error"] is None


@pytest.mark.parametrize("sql,bad", [
    ("SELECT 1 WHERE a LIKE 'x%%' AND b = %s", False),
    ("SELECT 1 WHERE a = %s -- only 'sent%' is a delivery", True),
    ("SELECT 1 WHERE a = %(name)s", False),
    ("SELECT 1 WHERE a LIKE 'x%' AND b = %s", True),
])
def test_the_detector_itself(sql, bad):
    """Guard the guard: it must clear the correctly-escaped control and flag
    the real offender, or it is a count-decoy."""
    assert bool(_BARE_PCT.search(sql)) is bad
