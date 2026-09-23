"""A subscription must retire the one-time expiry tracking it takes over from.

THE BUG (found in code 2026-09-22, fixed here). `users.tier_expires_at` and
`users.source_plan` are stamped by the one-time-payment branch of
handle_checkout_completed and NEVER cleared — before this change no statement
anywhere in the repo wrote NULL to either. The nightly cron
(crawler_scheduler slot 03/03 UTC → routes.expired_demote.run_expired_demote)
selects:

    tier_expires_at IS NOT NULL AND tier_expires_at < NOW()
    AND source_plan ILIKE '%_onetime' AND plan != 'free'

— no subscription_status filter, no demoted_at filter. So a former one-time
buyer who LATER SUBSCRIBES keeps a stale past expiry, checkout puts `plan` back
to a paid value, and the cron demotes the paying subscriber the next night:
plan='free', role='free', subscription_status='expired',
demoted_reason='tier_expired_onetime', api_keys.rate_limit_tier='free'.

Both the cron docstring and its slot comment asserted "Subscription-mode buyers
leave tier_expires_at NULL so the SELECT can never match them". True of an
account that has only ever subscribed. False of this one — and that sentence is
why the row was never looked at.

These tests compose the REAL shipped statements — the clear, pulled out of
handle_checkout_completed, and the cron's own _SELECT_EXPIRED_SQL — against one
sqlite database, so the fix is proved by the predicate that did the demoting
rather than by a restatement of it.
"""
import ast
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MAIN = REPO / "main.py"
CRON = REPO / "routes" / "expired_demote.py"
HANDLER = "handle_checkout_completed"

PAST = "2026-01-01 00:00:00"      # a one-time tier that expired months ago
FUTURE = "2027-01-01 00:00:00"    # one still running


def _sqlite(sql):
    return (sql.replace("%s", "?")
               .replace("NOW()", "CURRENT_TIMESTAMP")
               .replace("ILIKE", "LIKE"))


def _handler_ast():
    return next(n for n in ast.parse(MAIN.read_text(encoding="utf-8")).body
                if isinstance(n, ast.FunctionDef) and n.name == HANDLER)


def _clear_statements():
    """Every shipped statement in the handler that NULLs the one-time columns."""
    out = [n.value for n in ast.walk(_handler_ast())
           if isinstance(n, ast.Constant) and isinstance(n.value, str)
           and "tier_expires_at = NULL" in n.value]
    assert out, (
        "main.%s has no statement clearing tier_expires_at. Without one the "
        "nightly expired_onetime_demote cron re-demotes a new subscriber."
        % HANDLER)
    return out


def _cron_select():
    """routes/expired_demote._SELECT_EXPIRED_SQL, by AST — importing the module
    pulls Flask and the blueprint in."""
    for n in ast.parse(CRON.read_text(encoding="utf-8")).body:
        if (isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == "_SELECT_EXPIRED_SQL"
                        for t in n.targets)
                and isinstance(n.value, ast.Constant)):
            return n.value.value
    raise AssertionError("_SELECT_EXPIRED_SQL not found — renamed?")


@pytest.fixture
def db():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            email TEXT,
            plan TEXT,
            source_plan TEXT,
            tier_expires_at TEXT,
            stripe_customer_id TEXT,
            subscription_status TEXT,
            demoted_at TEXT,
            demoted_reason TEXT);
    """)
    return c


def _add(db, uid=1, *, plan="pro", source_plan="pro_annual_onetime",
         expires=PAST, status="active", demoted_at=None, demoted_reason=None,
         email="buyer@example.com"):
    db.execute(
        "INSERT INTO users (id, email, plan, source_plan, tier_expires_at,"
        " stripe_customer_id, subscription_status, demoted_at, demoted_reason)"
        " VALUES (?,?,?,?,?,'cus_x',?,?,?)",
        (uid, email, plan, source_plan, expires, status, demoted_at,
         demoted_reason))
    db.commit()


def _cron_matches(db):
    return db.execute(_sqlite(_cron_select()), ("%_onetime", 500)).fetchall()


def _run_clear(db, stmt, param):
    db.execute(_sqlite(stmt), (param,))
    db.commit()


# ── the defect, then the fix, through the cron's own predicate ───────────

def test_the_cron_matches_a_new_subscriber_before_the_clear(db):
    """★ THE BUG ITSELF, stated by the shipped SELECT rather than by me. A row
    that just subscribed still carries the one-time columns, so the nightly
    demote picks it up. If this ever stops matching, the clear below is no
    longer what is protecting these accounts and this file is measuring
    nothing."""
    _add(db)
    assert _cron_matches(db), (
        "the cron SELECT no longer matches a former one-time buyer who "
        "subscribed — the predicate moved; re-derive what protects this row")


@pytest.mark.parametrize("by", ["id", "email"])
def test_the_clear_takes_the_row_out_of_the_crons_reach(db, by):
    """Both selectors the handler writes: checkout resolves a user by id when
    it has one and by email otherwise, and a row reached by only one of them
    would still be demoted the next night."""
    _add(db)
    stmt = next(s for s in _clear_statements() if " %s AND" % by in s
                or s.rstrip().endswith("WHERE %s = %%s" % by)
                or "WHERE %s = %%s" % by in s)
    _run_clear(db, stmt, 1 if by == "id" else "buyer@example.com")

    row = db.execute("SELECT tier_expires_at, source_plan FROM users"
                     " WHERE id=1").fetchone()
    assert row == (None, None)
    assert not _cron_matches(db), (
        "the nightly expired_onetime_demote cron still matches a paying "
        "subscriber")


def test_the_clear_leaves_the_plan_the_checkout_just_granted(db):
    """It is a companion write, not a replacement for the plan UPDATE above
    it. A clear that also touched plan/role would undo the purchase."""
    _add(db)
    for stmt in _clear_statements():
        _run_clear(db, stmt, 1 if "id = %s" in stmt else "buyer@example.com")
    assert db.execute("SELECT plan, subscription_status FROM users WHERE id=1")\
             .fetchone() == ("pro", "active")


def test_the_clear_does_not_lift_an_operator_hold(db):
    """★ A PURCHASE MUST NOT CLEAR A STAMP. 'manual'/'abuse' are operator holds
    (owner decision 2026-09-22): if buying a subscription NULLed demoted_at,
    anyone under a hold could lift it with a credit card. The stale
    'tier_expired_onetime' stamp is retired by handle_payment_failed's own
    allowlist instead — see test_dunning_stamp_overwrites_a_stale_reason.py."""
    _add(db, demoted_at=PAST, demoted_reason="abuse")
    for stmt in _clear_statements():
        assert "demoted_at" not in stmt and "demoted_reason" not in stmt, (
            "the carryover clear writes a demote stamp column: %s" % stmt)
        _run_clear(db, stmt, 1 if "id = %s" in stmt else "buyer@example.com")
    assert db.execute("SELECT demoted_at, demoted_reason FROM users WHERE id=1")\
             .fetchone() == (PAST, "abuse")


# ── what the clear must not reach ────────────────────────────────────────

def test_the_clear_is_gated_on_a_subscription_checkout():
    """★ THE GUARD IS THE WHOLE SAFETY ARGUMENT, and it is invisible in the SQL.

    The statement must sit under `session_mode == 'subscription'`, NOT under
    `not set_tier_expiry`: the else branches of the plan write also carry a
    one-time payment whose plan the r-no-downgrade guard HELD, and a $10 pack
    bought by someone holding a genuine, still-VALID annual expiry must not
    erase it. Gate this on the wrong flag and the cron stops being able to
    demote anyone who ever bought a pack."""
    # ONE parse: node identity is meaningless across two ast.parse() calls,
    # and comparing across them makes this assertion unfalsifiable.
    tree = _handler_ast()
    targets = {id(n) for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and "tier_expires_at = NULL" in n.value}
    assert targets, "no clear statement in the handler to check the guard of"
    guarded = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = ast.dump(node.test)
        if "session_mode" not in test or "'subscription'" not in test:
            continue
        if any(id(c) in targets for c in ast.walk(node)):
            guarded = True
    assert guarded, (
        "the tier_expires_at clear is not inside an `if session_mode == "
        "'subscription'` block — a one-time pack purchase would erase a valid "
        "annual expiry and put the buyer beyond the cron's reach")


def test_a_live_onetime_tier_is_still_demotable_when_it_expires(db):
    """The cron must keep working for the population it was written for: a
    genuine one-time buyer, untouched by any subscription checkout."""
    _add(db, expires=FUTURE)
    assert not _cron_matches(db), "a tier that has not expired yet must not match"

    db.execute("UPDATE users SET tier_expires_at=? WHERE id=1", (PAST,))
    db.commit()
    assert _cron_matches(db), (
        "an expired one-time tier stopped matching — the enforcement leg is "
        "gone, and a $1,188 annual buyer keeps pro forever")


def test_the_clear_is_a_no_op_for_a_pure_subscriber(db):
    """Someone who has only ever subscribed has both columns NULL already. The
    statement carries its own `IS NOT NULL` predicate so the common case writes
    no rows at all."""
    _add(db, source_plan=None, expires=None)
    for stmt in _clear_statements():
        cur = db.execute(_sqlite(stmt),
                         (1 if "id = %s" in stmt else "buyer@example.com",))
        assert cur.rowcount == 0, (
            "the clear updates a row that had nothing to clear: %s" % stmt)
