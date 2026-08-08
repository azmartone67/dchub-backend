"""r-shift-source (2026-08-08) — the agent feed's "verdict shift" must be
read from the table that actually has a per-day history.

THE BUG: routes/agent_broadcast.py::_fetch_dcpi_verdict_shifts is two-pass.
Pass 1 was supposed to find genuine verdict SHIFTS by joining the current
score against the verdict `days` ago, reading prior verdicts from
`market_power_scores_history`. Pass 2 is a fallback to the CURRENT decisive
BUILD/AVOID verdicts, so the feed is never empty.

Pass 1 could never return a row, so every response since the archive froze
has been Pass 2 — current state relabelled as a shift, on
/api/v1/agent-broadcast, /today, /dcpi-shifts and the RSS variant.

WHY IT IS PERMANENT, not merely lagging. `market_power_scores` has carried
UNIQUE(market_slug) since 2026-05-11 (market_power_scores_slug_key and
market_power_scores_slug_unique), so every writer is UPDATE-in-place and a
non-latest row has never existed for dchub_self_heal's collapse to archive.
The collapse is a no-op by construction. Measured on the live DB 2026-08-08:

    market_power_scores_history — 1,346 rows, 289 slugs, every computed_at
    between 2026-05-09 and 2026-05-11. Nothing in the three months since.

So this is NOT the kind of empty table that fills in later, and PR #2432 —
which fixed a column-drift crash in this same function — explicitly did not
start archiving, because there is nothing to archive. Any future edit that
points a shift query back at *_history is reintroducing a guaranteed-dead
read, which is why it is banned here rather than merely discouraged.

THE REAL SERIES is `dcpi_daily_snapshots` (routes/dcpi.py): snapshot_date +
market_slug unique, one row per market per day, method_version stamped since
r-ws3-methodology. routes/dcpi.py created it as a NEW table precisely because
the legacy *_history was built `LIKE market_power_scores INCLUDING ALL` and
therefore inherited the very UNIQUE(market_slug) that makes per-day rows
impossible. movers/trending already read it for prev_excess, and
quarterly_report.py::_verdict_shifts already reads it for the same
verdict-shift question. Live on 2026-08-08: 21,666 rows, 324 markets, 71
distinct snapshot_dates over 2026-05-30..2026-08-08 with no gaps, and 114
markets whose verdict differs from their 7-days-ago verdict.

WHAT IS PINNED HERE, read out of the AST so a table named only in prose
cannot satisfy or trip it:

  1. BAN — no statement in Pass 1 may read market_power_scores_history.
     This is the regression guard the bug earns.
  2. SOURCE — Pass 1 must actually read dcpi_daily_snapshots. Without this,
     deleting Pass 1 outright would satisfy the ban and still ship a feed
     that reports current state as a shift.
  3. FALLBACK — Pass 2 must survive. It is what keeps the feed non-empty on
     a deploy whose snapshot table has not bootstrapped yet, and it must not
     be collateral damage of a future "simplification".

The docstring is deliberately excluded from the string census: it explains at
length WHY *_history is dead, and a guard that cannot tell an explanation
from a query would forbid documenting the bug it protects against.
"""
import ast
import os
import re

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "routes", "agent_broadcast.py")

FUNC = "_fetch_dcpi_verdict_shifts"

# FROM / JOIN <table>, ignoring the LATERAL subquery keyword so the derived
# table in Pass 1 is not mistaken for a real relation.
_TABLE_RE = re.compile(r"\b(?:FROM|JOIN)\s+(?:LATERAL\s+)?([a-z_][a-z0-9_]*)",
                       re.IGNORECASE)


def _load_function():
    """Pull the shipped function out of the source with ast.

    Tests never import main.py (it opens DB pools and registers ~200
    blueprints); routes/agent_broadcast.py pulls in flask and the URL
    registry, so it is read rather than imported.
    """
    with open(SRC, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == FUNC:
            return node
    raise AssertionError(
        f"{FUNC} not found in {SRC} — if it was renamed, move this guard with "
        f"it rather than deleting it.")


def _body_strings(fn):
    """Every string constant in the function EXCEPT its docstring."""
    doc = fn.body[0] if fn.body else None
    is_doc = (isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant)
              and isinstance(doc.value.value, str))
    body = fn.body[1:] if is_doc else fn.body
    out = []
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.append(node.value)
    return out


def _statements(fn):
    """The SQL statements in the function body, longest first."""
    return sorted((s for s in _body_strings(fn) if "SELECT" in s.upper()),
                  key=len, reverse=True)


def _tables(sql):
    return {m.lower() for m in _TABLE_RE.findall(sql)}


def _pass_one(fn):
    """Pass 1 = the shift statement: the one that compares two verdicts."""
    for sql in _statements(fn):
        if "prior_verdict" in sql.lower():
            return sql
    raise AssertionError(
        "no verdict-shift statement found in %s — Pass 1 appears to have been "
        "deleted. The feed would then report current state as a shift, which "
        "is the bug this guard exists to prevent." % FUNC)


def test_pass_one_does_not_read_market_power_scores_history():
    """THE REGRESSION GUARD. *_history froze 2026-05-11 and cannot refill:
    UNIQUE(market_slug) means no non-latest row has ever existed to archive.
    """
    sql = _pass_one(_load_function())
    assert "market_power_scores_history" not in _tables(sql), (
        "Pass 1 of %s reads market_power_scores_history again. That table is "
        "frozen at 1,346 rows (computed_at 2026-05-09..2026-05-11, measured "
        "live 2026-08-08) and cannot refill, because market_power_scores has "
        "UNIQUE(market_slug) so every writer is UPDATE-in-place and self-heal "
        "has no non-latest row to collapse. The join returns zero rows every "
        "time and the feed silently falls through to Pass 2, publishing "
        "current state under a 'verdict shift' label. Read "
        "dcpi_daily_snapshots instead." % FUNC)


def test_pass_one_reads_the_daily_snapshot_series():
    """Banning the dead table is not enough — deleting Pass 1 would also
    satisfy the ban while leaving the feed just as dishonest."""
    sql = _pass_one(_load_function())
    assert "dcpi_daily_snapshots" in _tables(sql), (
        "Pass 1 of %s must read dcpi_daily_snapshots — the only per-day DCPI "
        "series that exists (snapshot_date + market_slug unique, gapless "
        "2026-05-30..2026-08-08). It is already what movers/trending and "
        "quarterly_report._verdict_shifts read. Tables found: %s"
        % (FUNC, sorted(_tables(sql))))


def test_pass_two_fallback_survives():
    """Pass 2 keeps the feed non-empty on a deploy whose snapshot table has
    not bootstrapped. Repointing Pass 1 must not cost us the fallback."""
    fn = _load_function()
    shift_sql = _pass_one(fn)
    others = [s for s in _statements(fn) if s != shift_sql]
    fallbacks = [s for s in others
                 if "market_power_scores" in _tables(s)
                 and "BUILD" in s and "AVOID" in s]
    assert fallbacks, (
        "the Pass 2 fallback in %s is gone. It reads current decisive "
        "BUILD/AVOID verdicts from market_power_scores so the agent feed is "
        "never empty while DCPI data exists; Pass 1 alone returns nothing on "
        "a deploy where dcpi_daily_snapshots has not bootstrapped yet. "
        "Statements found: %d" % (FUNC, len(others)))


def test_shift_statement_compares_two_points_in_time():
    """A shift needs a BEFORE and an AFTER. Pins the window parameter and the
    stale-market guard, so Pass 1 cannot decay into 'current state' with a
    snapshot table name on it."""
    sql = _pass_one(_load_function()).lower()
    assert "is distinct from" in sql, (
        "Pass 1 no longer compares a prior verdict against the current one.")
    assert "current_date -" in sql, (
        "Pass 1 no longer offsets by the `days` window — it would compare a "
        "market against itself and report every market as unchanged.")
    assert "prior_date < l.snapshot_date" in sql, (
        "Pass 1 lost the guard that the prior snapshot predates the latest "
        "one. Without it a market that STOPPED being snapshotted resolves to "
        "the same row on both sides of the join.")
