"""db_honesty.py — read helpers for surfaces where an empty result gets PUBLISHED.

Generalises the fix in routes/agent_index.py (#2071) so the other route
modules that carry the same shape can share one implementation instead of
hand-copying it five more times.

THE BUG THIS EXISTS TO PREVENT
------------------------------
`GET /api/v1/agent/index` served an all-zero coverage inventory — HTTP 200,
well-formed, no error field — to every AI agent that called it, for months.
Nothing alerted, because a 200 with a well-formed body is exactly what
monitoring looks for. Four things had to line up, and all four recur:

1. `with <psycopg2 connection>` is a TRANSACTION manager, not a closer.
   Entering it opens an explicit transaction that `autocommit = True` does
   NOT override. `conn.autocommit` keeps reporting True while the session
   sits in TRANSACTION_STATUS_INTRANS, so the defect is invisible from
   Python — every module that hit this had a comment claiming autocommit
   made its queries independent, and every one of those comments was false.
2. One query referenced a column or table that does not exist.
3. A transaction belongs to the CONNECTION, so per-section cursors bought
   nothing: every LATER query died with InFailedSqlTransaction — including
   perfectly valid ones.
4. A helper that caught everything and returned `[]` mapped each failure to
   an empty list, and the caller mapped that to `0`.

Steps 1-3 are ordinary bugs. Step 4 is what made it invisible.

Measured on the live DB, 2026-08-01, replaying investor_brief's real query
order for `?operator=Equinix`:

    footprint      -> (702, 5168.0, 41, 65)     ok
    ma_history     -> UndefinedTable            `transactions` never existed
    recent_news    -> InFailedSqlTransaction    cascade
    peer_operators -> InFailedSqlTransaction    cascade

`peer_operators` is a VALID facilities query. It was served as [] on every
call because a dead TABLE two reads earlier poisoned the transaction. The dead
query and the lie sat in different sections of the response, which is why
reading either one alone missed it.

★ A CASCADE NEEDS THE STATEMENT TO REACH POSTGRES. An early draft of this
module cited policy_brief as the example and was wrong: that handler's dead
read carried lone `%` signs in a call that also passes params, so psycopg2
raised IndexError CLIENT-SIDE, the statement never reached the server, and the
transaction was never poisoned. Same swallow, same silent absence — but no
cascade. Worth keeping straight, because it means "a swallowed read" and "a
swallowed read that also zeroes its neighbours" are different failures, and
only the second one needs the rollback.

HOW TO USE
----------
    c = None
    try:
        c = open_conn()
        with c.cursor() as cur:
            rows, err = try_fetchall(cur, "SELECT ...")
            if err:
                out["thing"] = None                 # NEVER 0, NEVER []
                errors["thing"] = err
            else:
                out["thing"] = len(rows)
    finally:
        close_quietly(c)

★ A VALUE THAT COULD NOT BE READ IS `None`, NEVER 0.
A consumer can branch on null. It cannot detect a lie told as 0 — and on an
agent-facing surface a 0 is not a neutral placeholder, it is an affirmative
claim that we hold nothing. A genuine zero stays 0; that distinction is the
entire point.

★ THIS MODULE IS IMPORTABLE ON PURPOSE.
util/capacity_pipeline.py records the same lesson from the other direction:
the 2026-07-27 "every read is guarded" claim was false for fourteen served
reads because the guard lived as a function-LOCAL variable — nothing could
import it, so nothing could check it. A fence can assert that route modules
import from here; it cannot assert anything about a private copy.

tests/test_route_read_honesty.py fails the build if a route in the audited
set reintroduces `with <conn>` or swallows a read into a published zero.
"""

from __future__ import annotations

import os
import re

__all__ = ["open_conn", "try_fetchall", "try_fetchone", "unpoison",
           "close_quietly", "DEAL_DATE",
           "column_population", "zero_is_measured",
           "POPULATED", "NEVER_SET", "CONSTANT", "EMPTY_TABLE", "UNKNOWN"]


# ★ NO `DEALS_OK` HERE — import it from util/deals.
# An earlier draft of this module shipped its own copy. util/deals.py exists
# precisely because seven files were carrying hand-written copies of that
# predicate in two spellings, and it names #2071's function-local `DEALS_OK`
# as one of them. An eighth copy in a module whose whole subject is "do not
# publish a number you cannot vouch for" would have been the wrong lesson
# learned twice. tests/test_deals_guard.py censuses it.


#: `deals.date` is TEXT, and `deals.deal_date` (timestamp) is populated on only
#: 280 of the 1,843 publishable rows and stops at 2026-03-02, while `date`
#: covers 813 and is current to today. A BARE cast is not safe: one unparseable
#: row throws, and the throw gets swallowed — the documented ai_cumulative
#: TEXT-timestamp trap, which replaces one silent zero with another. CASE fixes
#: the evaluation order so a malformed value becomes NULL instead of an error.
DEAL_DATE = ("(CASE WHEN date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' "
             "THEN date::date ELSE NULL END)")


def open_conn(dsn: str | None = None, connect_timeout: int = 8):
    """An autocommit connection whose autocommit actually holds.

    ★ DO NOT write `with open_conn() as c:`. psycopg2's connection context
    manager is a TRANSACTION manager, not a closer: entering it opens an
    explicit transaction block even when autocommit is True, and the
    attribute goes on reporting True while the session sits INTRANS. Use
    try/finally + close_quietly(), which is what the callers here do.
    """
    import psycopg2
    c = psycopg2.connect(dsn or os.environ.get("DATABASE_URL"),
                         connect_timeout=connect_timeout)
    c.autocommit = True
    return c


def unpoison(cur) -> None:
    """Roll back an aborted transaction so the NEXT query can still run.

    Belt-and-braces for the trap above: if anything ever re-opens an explicit
    transaction, one bad query would otherwise take down every later query on
    the same connection with InFailedSqlTransaction — ACROSS CURSORS, since a
    transaction belongs to the connection, not the cursor. rollback() on an
    idle autocommit connection is a no-op, so this is always safe to call.
    """
    try:
        cur.connection.rollback()
    except Exception:
        pass


def try_fetchall(cur, sql, params=None):
    """Run a read. Return (rows, None) on success, ([], "Type: msg") on failure.

    Use this anywhere an empty result would be PUBLISHED as a fact. `[]` and
    "the query blew up" are not the same answer, and a caller that cannot tell
    them apart will happily report a broken read as a confident zero.

    ★ `params` DEFAULTS TO None, NOT (). psycopg2 only attempts %-interpolation
    when params is not None, so an empty TUPLE is not the same as no params: it
    turns every literal `%` in the SQL into an interpolation target and raises
    `IndexError: tuple index out of range` CLIENT-SIDE, before the statement
    reaches Postgres. That is the same trap documented in
    util/capacity_pipeline and retracted-into by #2092 — and this helper used
    to spring it on any caller who handed over a param-less query containing a
    LIKE pattern. Caught 2026-08-01 by routing site_stats' `mcp_calls_7d_real`
    (whose filter carries `LIKE 'dchub-%'`) through here and watching a live
    read that had always worked start returning IndexError. Passing None
    through preserves psycopg2's own semantics exactly.
    """
    try:
        cur.execute(sql, params)
        return cur.fetchall(), None
    except Exception as e:
        unpoison(cur)
        return [], f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"


def try_fetchone(cur, sql, params=None):
    """(row, None) / (None, "Type: msg"). Same contract as try_fetchall.

    A caller that needs a scalar should treat `None` as unknown and publish
    null — not fall back to 0, which is the whole failure mode above.
    """
    rows, err = try_fetchall(cur, sql, params)
    if err:
        return None, err
    return (rows[0] if rows else None), None


# ─────────────────────────────────────────────────────────────────────
# A SUCCESSFUL READ CAN STILL FAIL TO BE A MEASUREMENT
# ─────────────────────────────────────────────────────────────────────
# Everything above separates "the read failed" (null) from "we counted"
# (a number). 2026-08-08 turned up the third case that neither covers:
#
#     SELECT COUNT(*) FROM subsea_landing_points WHERE is_major_hub = TRUE
#
# succeeds, returns 0, and gets published as `major_hubs: 0` — next to a
# basis_note promising "a null value means the read failed, it is never a
# measured zero". The query is fine. The COLUMN was never populated, so the
# 0 counts rows matching a predicate nothing was ever eligible to match.
# Measured live the same day:
#
#     subsea_cables.is_planned          NULL in all 699 rows
#                                       (?planned=true AND ?planned=false
#                                        each return 0 — NULL matches neither)
#     subsea_landing_points.is_major_hub FALSE in all 1,927 rows, never TRUE
#     subsea_landing_points.country      '' in all 1,927 rows
#
# `major_hubs: 0` reads as "we checked 1,927 landings and none is a major
# hub". The truth is "nothing ever set that flag". Those are different
# claims and only one of them is ours to make.
#
# ★ THE PROBE ONLY EARNS ITS COST WHEN THE COUNT IS ALREADY 0. A non-zero
# count is self-evidently backed by a populated column, so callers should
# gate on that and skip the extra round trip.
#
# ★ A GENUINE ZERO MUST SURVIVE THIS. If the column varies — some rows TRUE,
# some FALSE — then a 0 means we looked and found none, which is a real
# finding and stays 0. Suppressing those would trade one lie for another.

POPULATED = "populated"        # column carries data; a 0 is a real measurement
NEVER_SET = "never_set"        # zero non-null values in the entire table
CONSTANT = "constant"          # exactly one distinct value; carries no signal
EMPTY_TABLE = "empty_table"    # no rows at all; 0 is honest
UNKNOWN = "unknown"            # the probe itself failed; certify nothing

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def column_population(cur, table, column, kind="flag"):
    """Classify how populated `table.column` is across the WHOLE table.

    Returns ``(verdict, detail)`` where verdict is one of the module
    constants and detail carries the observed row counts, so a caller can
    quote real numbers in the reason it publishes rather than asserting
    "never populated" on faith.

    `kind="text"` treats NULL and whitespace-only/empty strings alike — the
    landing-point `country` column is `''` on every row, not NULL, and a
    plain COUNT() would call that populated.

    ★ Identifiers are validated, not escaped. They must be literals the route
    author wrote; nothing derived from a request may reach here.
    """
    if not _IDENT.match(table or "") or not _IDENT.match(column or ""):
        raise ValueError(
            f"column_population: identifiers must be bare literals, "
            f"got table={table!r} column={column!r}")

    expr = f"NULLIF(BTRIM({column}::text), '')" if kind == "text" else column
    # ★ params stays None. An empty tuple would make psycopg2 treat any
    # literal `%` as an interpolation target — the trap documented on
    # try_fetchall. This SQL carries none today; None keeps it that way if
    # someone later adds a LIKE.
    row, err = try_fetchone(
        cur,
        f"SELECT COUNT(*), COUNT({expr}), COUNT(DISTINCT {expr}) FROM {table}")
    if err:
        return UNKNOWN, {"error": err}

    total, non_null, distinct = (int(row[0]), int(row[1]), int(row[2]))
    detail = {"rows": total, "non_null": non_null, "distinct_values": distinct}

    if total == 0:
        return EMPTY_TABLE, detail
    if non_null == 0:
        return NEVER_SET, detail
    if distinct <= 1:
        return CONSTANT, detail
    return POPULATED, detail


def zero_is_measured(cur, table, column, kind="flag"):
    """May a 0 counted over `table.column` be published as a measurement?

    Returns ``(ok, reason)``. When ok is False the caller must publish null
    and put `reason` in its `unavailable[]` list; the reason quotes the
    observed row counts so a reader can check the claim.

    ★ UNKNOWN returns ok=True. If the probe itself could not run we have no
    evidence the column is dead, and demoting a possibly-real count to null
    on no evidence is its own fabrication. The count keeps whatever standing
    it already had.
    """
    verdict, detail = column_population(cur, table, column, kind)
    ref = f"{table}.{column}"

    if verdict == NEVER_SET:
        return False, (
            f"{ref} is unset on all {detail['rows']:,} rows — the column was "
            f"never populated, so 0 counts rows nothing could ever match; "
            f"this is a coverage gap, not a measured zero")
    if verdict == CONSTANT:
        return False, (
            f"{ref} holds a single value across all {detail['rows']:,} rows "
            f"and no row matches — the column carries no signal, so 0 is a "
            f"coverage gap, not a measured zero")
    return True, None


def close_quietly(c) -> None:
    """Close a connection without letting teardown mask a real error."""
    if c is None:
        return
    try:
        c.close()
    except Exception:
        pass
