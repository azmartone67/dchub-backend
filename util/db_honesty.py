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

__all__ = ["open_conn", "try_fetchall", "try_fetchone", "unpoison",
           "close_quietly", "DEAL_DATE"]


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


def close_quietly(c) -> None:
    """Close a connection without letting teardown mask a real error."""
    if c is None:
        return
    try:
        c.close()
    except Exception:
        pass
