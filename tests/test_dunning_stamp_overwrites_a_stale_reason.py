"""main.handle_payment_failed must own the stamp it acts on.

THE BUG (found in code 2026-09-22, fixed here). The stamp UPDATE was
`WHERE id = %s AND demoted_at IS NULL` — first write wins — while the api_keys
UPDATE beside it carried no such predicate. So on an account already stamped by
routes/expired_demote ('tier_expired_onetime'), a dunning demote:

  * pulled api_keys.rate_limit_tier to 'free'                    (it ran),
  * left demoted_reason reading 'tier_expired_onetime'           (rowcount 0),
  * sent no demote email — _send_dunning_demote_notice is gated on that
    rowcount, and _pg_execute returns (0, []) on failure too, so the one
    signal that would have surfaced this is silent by construction, and
  * was never undone: handle_invoice_paid's r46-restore selects on the two
    reasons THIS handler writes, so the row it needed to match no longer said
    one. The customer pays, subscription_status goes back to 'active', the web
    path (api_tier_gating.resolve_effective_plan) serves paid again, and both
    MCP gates — mcp_gatekeeper._tier_of_row and
    flask_mcp_endpoints._api_key_row_node_tier, which read rate_limit_tier
    first since #5193/#5205 — keep serving FREE. Forever.

OWNER DECISION 2026-09-22: overwrite a stale non-dunning stamp. An ALLOWLIST
(`demoted_reason = 'tier_expired_onetime'`), not a denylist, so a reason nobody
classified keeps today's behaviour instead of being silently overwritten.
'manual' and 'abuse' are operator holds: never overwritten here, never cleared
by r46-restore. test_every_written_demote_reason_is_classified below fails if a
new writer appears without a decision.

HOW THIS REACHES THE HANDLER. main.py cannot be imported in a unit test (it
needs a DB), so the FunctionDef is compiled out of main.py's AST with its
globals stubbed — the pattern tests/test_pro_restore_on_renewal.py established.
The `_pg_execute` stub runs the handler's own SQL against a REAL in-memory
sqlite3. A fake cursor returning canned rows would pass whatever SQL the
handler sent; this cannot, because the engine decides the outcome — which is
the whole point, since the defect WAS a WHERE clause.
"""
import ast
import copy
import re
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MAIN = REPO / "main.py"
CUSTOMER = "cus_stale_stamp"
STALE_AT = "2025-09-01 00:00:00"

# The reason routes/expired_demote.py writes. A dunning demote may take it.
OVERWRITABLE = ("tier_expired_onetime",)
# Operator holds. No code writes either; they are set by hand in the DB.
BLOCKING = ("manual", "abuse")
# What handle_payment_failed itself writes.
DUNNING = ("dunning_prior_payer", "first_charge_never_succeeded")

# (reason, invoices_paid_count, payment_failed_count) that reaches each demote.
# DEMOTE_AFTER_N_FAILURES = 2 for a never-payer; 4 for a prior payer.
REACHES = {
    "dunning_prior_payer": (3, 4),
    "first_charge_never_succeeded": (0, 2),
}


def _jsonb_to_sqlite(flat):
    """Mechanically translate a shipped jsonb statement so sqlite can run it.

    ★ NOT a hand-written copy of the statement. The copy is what breaks: the
    first draft of this harness ran its own `WHERE LOWER(email) = LOWER(?)` with
    `params[-1]`, and #5239 then widened the real predicate to
    `(LOWER(email) = LOWER(%s) OR metadata->>'stripe_customer_id' = %s)` with
    params `(customer, reason, email, customer)` — so params[-1] stopped being
    the email and the stand-in silently matched on the wrong value while still
    going green.

    So: the WHERE clause is carried across VERBATIM (only the -> / ->> / - json
    accessors are rewritten, mechanically, for any key), and the SET expression
    sqlite cannot evaluate is replaced by a no-op that consumes EXACTLY the same
    placeholders in the same order. The placeholder count is asserted, so a
    change to this statement's parameters fails here instead of binding
    something else.
    """
    head, sep, where = flat.partition(" WHERE ")
    assert sep, "no WHERE clause to carry across: %s" % flat
    n_set = head.count("%s")
    if "jsonb_build_object" in head:
        # The demote's metadata record. Its CONTENT is not what these tests
        # assert on; its predicate and its `tier = 'free'` are.
        assert "tier = 'free'" in head, head
        consume = " AND ".join(["? IS NOT NULL"] * n_set) or "1"
        head = ("UPDATE mcp_dev_keys SET tier = 'free', metadata ="
                " CASE WHEN %s THEN metadata ELSE metadata END" % consume)
    out = head + " WHERE " + where
    out = re.sub(r"metadata\s*->\s*'([a-z_]+)'\s*->>\s*'([a-z_]+)'",
                 r"json_extract(metadata, '$.\1.\2')", out)
    out = re.sub(r"metadata\s*->>\s*'([a-z_]+)'",
                 r"json_extract(metadata, '$.\1')", out)
    out = re.sub(r"metadata\s*-\s*'([a-z_]+)'",
                 r"json_remove(metadata, '$.\1')", out)
    out = (out.replace("::jsonb", "").replace("::text", "")
              .replace("NOW()", "CURRENT_TIMESTAMP"))
    out = out.replace("%s", "?")
    assert out.count("?") == flat.count("%s"), (
        "translation changed the parameter count (%d -> %d): the statement's "
        "params moved and this harness would bind the wrong values.\n%s"
        % (flat.count("%s"), out.count("?"), flat))
    return out


def _load(handler, ns_extra):
    # The dunning handlers call the r-trial-dunning helpers, so lift them too;
    # they run against the same stubbed globals.
    names = (handler, "_real_paid_invoice_count", "_is_post_trial_first_charge")
    fns = [copy.deepcopy(n) for n in ast.parse(MAIN.read_text(encoding="utf-8")).body
           if isinstance(n, ast.FunctionDef) and n.name in names]
    for fn in fns:
        fn.decorator_list = []
    ns = dict(ns_extra)
    exec(compile(ast.Module(body=fns, type_ignores=[]), str(MAIN), "exec"), ns)
    return ns[handler]


class _Harness:
    """A real engine behind _pg_execute, so the handler's own SQL decides."""

    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                email TEXT,
                stripe_customer_id TEXT,
                plan TEXT,
                demoted_at TEXT,
                demoted_reason TEXT,
                invoices_paid_count INTEGER DEFAULT 0,
                payment_failed_count INTEGER DEFAULT 0,
                subscription_status TEXT);
            CREATE TABLE api_keys (
                user_id INTEGER,
                rate_limit_tier TEXT,
                plan TEXT,
                last_used_at TEXT);
            CREATE TABLE mcp_dev_keys (
                email TEXT,
                tier TEXT,
                metadata TEXT);
        """)
        self.sql_log = []
        self.notices = []

    # -- the engine ------------------------------------------------------
    def pg_execute(self, sql, params=(), fetch=False):
        flat = " ".join(sql.split())
        self.sql_log.append(flat)
        if "jsonb" in flat:
            translated = _jsonb_to_sqlite(flat)
        else:
            translated = (sql.replace("%s", "?")
                             .replace("NOW()", "CURRENT_TIMESTAMP")
                             .replace("GREATEST", "MAX"))
        cur = self.db.execute(translated, params)
        rows = cur.fetchall() if fetch else []
        self.db.commit()
        return (cur.rowcount, rows)

    # -- fixture helpers -------------------------------------------------
    def add_user(self, uid, *, reason, demoted_at=STALE_AT, paid=3, failed=3,
                 plan="pro", tier="pro", email="payer@example.com"):
        self.db.execute(
            "INSERT INTO users (id, email, stripe_customer_id, plan, demoted_at,"
            " demoted_reason, invoices_paid_count, payment_failed_count,"
            " subscription_status) VALUES (?,?,?,?,?,?,?,?,'active')",
            (uid, email, CUSTOMER, plan, demoted_at, reason, paid, failed))
        self.db.execute(
            "INSERT INTO api_keys (user_id, rate_limit_tier, plan) VALUES (?,?,?)",
            (uid, tier, plan))
        self.db.execute(
            "INSERT INTO mcp_dev_keys (email, tier, metadata) VALUES (?,?,'{}')",
            (email, "paid"))
        self.db.commit()

    def key(self, uid):
        r = self.db.execute("SELECT rate_limit_tier, plan FROM api_keys"
                            " WHERE user_id=?", (uid,)).fetchone()
        return {"rate_limit_tier": r[0], "plan": r[1]}

    def user(self, uid):
        r = self.db.execute("SELECT demoted_at, demoted_reason,"
                            " subscription_status FROM users WHERE id=?",
                            (uid,)).fetchone()
        return {"demoted_at": r[0], "demoted_reason": r[1],
                "subscription_status": r[2]}

    def mcp_tier(self, email="payer@example.com"):
        return self.db.execute("SELECT tier FROM mcp_dev_keys WHERE email=?",
                               (email,)).fetchone()[0]


@pytest.fixture
def harness():
    h = _Harness()
    mirror = sqlite3.connect(":memory:")
    mirror.executescript(
        "CREATE TABLE users (stripe_customer_id TEXT, subscription_status TEXT,"
        " payment_failed_count INTEGER, invoices_paid_count INTEGER);")
    ns = {
        "STRIPE_AVAILABLE": False,      # skip the Stripe paid-invoice recount
        "stripe": None,
        "_pg_execute": h.pg_execute,
        "get_db": lambda: mirror,
        "_sync_tables_bg": lambda *a, **k: None,
        "note_swallowed_write": lambda *a, **k: None,
        "utc_iso_z": lambda: "2026-09-22T00:00:00Z",
        "_send_dunning_demote_notice": lambda *a, **k: h.notices.append(a),
    }
    h.failed = _load("handle_payment_failed", ns)
    h.paid = _load("handle_invoice_paid", ns)
    return h


def _invoice(customer=CUSTOMER, attempt=4):
    return {"customer": customer, "attempt_count": attempt}


# ── the overwrite ────────────────────────────────────────────────────────

@pytest.mark.parametrize("reason", DUNNING)
def test_a_stale_expiry_stamp_is_overwritten_by_the_dunning_reason(harness, reason):
    """The fix. The row arrives stamped 'tier_expired_onetime' — a demote that
    merely got there first — and must leave stamped with the reason the demote
    that is happening NOW writes, because that is the reason handle_invoice_paid
    restores on."""
    paid, failed = REACHES[reason]
    harness.add_user(1, reason="tier_expired_onetime", paid=paid, failed=failed - 1)

    harness.failed(_invoice())

    got = harness.user(1)
    assert got["demoted_reason"] == reason, (
        "stamp still reads %r — the demote pulled the tier but left a reason no "
        "payment resolves" % got["demoted_reason"])
    assert got["demoted_at"] != STALE_AT, (
        "demoted_at was not refreshed; the stamp is a year old and the demote "
        "is now")
    assert harness.key(1)["rate_limit_tier"] == "free"
    assert harness.key(1)["plan"] == "pro", "api_keys.plan must survive"


@pytest.mark.parametrize("reason", DUNNING)
def test_the_overwrite_is_what_lets_the_next_payment_restore_the_tier(harness, reason):
    """★ THE POINT OF THE WHOLE CHANGE, end to end through BOTH handlers'
    real SQL on one database.

    Demote a stale-stamped account, then pay. r46-restore in
    handle_invoice_paid selects `demoted_reason IN (the two dunning reasons)`;
    before the fix the row still said 'tier_expired_onetime' and was skipped,
    so the payer stayed on free tier on every surface that reads
    rate_limit_tier while the web path served paid."""
    p, f = REACHES[reason]
    harness.add_user(1, reason="tier_expired_onetime", paid=p, failed=f - 1)
    harness.failed(_invoice())
    assert harness.key(1)["rate_limit_tier"] == "free", "demote did not apply"

    harness.paid({"customer": CUSTOMER})

    assert harness.key(1)["rate_limit_tier"] == "pro", (
        "a customer who paid is still on free tier — exactly the stranding this "
        "change exists to stop")
    assert harness.user(1)["demoted_at"] is None
    assert harness.user(1)["demoted_reason"] is None


@pytest.mark.parametrize("reason", DUNNING)
def test_the_demote_email_goes_out_on_a_stale_stamp(harness, reason):
    """The notice is gated on the stamp's rowcount. With the stamp blocked, a
    customer lost paid access with no signal to them and none to us."""
    p, f = REACHES[reason]
    harness.add_user(1, reason="tier_expired_onetime", paid=p, failed=f - 1)

    harness.failed(_invoice())

    assert len(harness.notices) == 1, (
        "no demote email — the rowcount was 0, which is how this defect stayed "
        "invisible")
    assert harness.notices[0][2] == reason, harness.notices[0]


# ── what the overwrite must NOT take ─────────────────────────────────────

@pytest.mark.parametrize("reason", BLOCKING)
def test_an_operator_hold_is_never_overwritten(harness, reason):
    """Owner decision: 'manual'/'abuse' are human decisions, not payment
    failures. A webhook may not relabel one, and r46-restore may not lift it —
    otherwise an abuser's next successful charge clears their own hold."""
    harness.add_user(1, reason=reason, paid=3, failed=3)

    harness.failed(_invoice())

    assert harness.user(1) == {"demoted_at": STALE_AT, "demoted_reason": reason,
                               "subscription_status": "payment_failed"}
    assert not harness.notices, "an operator hold must not trigger a demote email"


@pytest.mark.parametrize("reason", BLOCKING)
def test_an_operator_hold_still_loses_its_paid_tier(harness, reason):
    """Also the owner's decision, and the half that is easy to lose by
    accident: the stamp is not overwritten, but the tier writes are NOT gated
    on it. The account is meant to be on free — leave it there."""
    harness.add_user(1, reason=reason, paid=3, failed=3, tier="pro")

    harness.failed(_invoice())

    assert harness.key(1)["rate_limit_tier"] == "free"
    assert harness.mcp_tier() == "free"


@pytest.mark.parametrize("reason", BLOCKING)
def test_a_payment_does_not_lift_an_operator_hold(harness, reason):
    """The other half of 'never restored': r46-restore's reason list."""
    harness.add_user(1, reason=reason, tier="free", paid=3, failed=3)

    harness.paid({"customer": CUSTOMER})

    assert harness.user(1)["demoted_reason"] == reason
    assert harness.user(1)["demoted_at"] == STALE_AT
    assert harness.key(1)["rate_limit_tier"] == "free", (
        "a paid invoice restored a hand-set hold")


@pytest.mark.parametrize("reason", DUNNING)
def test_a_repeat_dunning_pass_does_not_re_stamp_or_re_email(harness, reason):
    """The behaviour the `demoted_at IS NULL` half still protects. Stripe
    re-delivers webhooks and a dunning cycle re-runs this handler; neither may
    reset the clock or mail the customer again."""
    harness.add_user(1, reason=reason, paid=3, failed=4, tier="free")

    harness.failed(_invoice())

    assert harness.user(1)["demoted_at"] == STALE_AT, "the clock was reset"
    assert harness.user(1)["demoted_reason"] == reason
    assert not harness.notices, "a second email for a demote already in effect"


def test_an_undemoted_account_still_stamps_and_emails(harness):
    """The unchanged baseline: nothing about the ordinary path moved."""
    harness.add_user(1, reason=None, demoted_at=None, paid=3, failed=3)

    harness.failed(_invoice())

    assert harness.user(1)["demoted_reason"] == "dunning_prior_payer"
    assert harness.user(1)["demoted_at"] is not None
    assert harness.key(1)["rate_limit_tier"] == "free"
    assert len(harness.notices) == 1


def test_a_demote_below_the_threshold_does_not_touch_a_stale_stamp(harness):
    """The overwrite is reached only from inside `if demote_reason:`. Three
    failures is not four, so a prior payer's row must come out untouched —
    including the stale stamp."""
    harness.add_user(1, reason="tier_expired_onetime", paid=3, failed=2, tier="pro")

    harness.failed(_invoice(attempt=3))

    assert harness.user(1)["demoted_reason"] == "tier_expired_onetime"
    assert harness.key(1)["rate_limit_tier"] == "pro"
    assert not harness.notices


# ── the recurrence guard ─────────────────────────────────────────────────

_REASON_WRITE = re.compile(
    r"demoted_reason\s*=\s*'([a-z_]+)'"          # SET demoted_reason = 'x'
    r"|demoted_reason\s+IN\s*\(([^)]*)\)",       # WHERE demoted_reason IN (...)
    re.IGNORECASE)


def test_every_written_demote_reason_is_classified():
    """★ AN ALLOWLIST ONLY WORKS IF SOMEONE NOTICES THE NEXT ENTRY.

    The fix keys on one literal. A new demote reason added anywhere in the
    lifecycle — a new cron, a new hold — would reproduce this exact bug
    silently: stamped first, never overwritten, tiers pulled anyway, never
    restored. So fail HERE, where the decision is written down, rather than in
    production six months later.

    A new reason is not a bug in this test. Add it to OVERWRITABLE (a dunning
    demote may take it) or to BLOCKING (it must survive), and say which in the
    comment beside it in main.py.
    """
    known = set(OVERWRITABLE) | set(BLOCKING) | set(DUNNING)
    seen = {}
    for path in sorted(REPO.glob("*.py")) + sorted(REPO.glob("routes/*.py")):
        for m in _REASON_WRITE.finditer(path.read_text(encoding="utf-8")):
            for lit in re.findall(r"'([a-z_]+)'", m.group(0)):
                seen.setdefault(lit, set()).add(path.name)
    assert seen, ("found no demoted_reason literals at all — the pattern went "
                  "stale, which would make this guard vacuous")
    assert "tier_expired_onetime" in seen and "dunning_prior_payer" in seen, (
        "the two reasons this change is about are not being found: %s" % sorted(seen))
    unknown = {k: sorted(v) for k, v in seen.items() if k not in known}
    assert not unknown, (
        "unclassified demoted_reason literal(s): %s. Decide whether a dunning "
        "demote may overwrite each one, then add it to OVERWRITABLE or BLOCKING "
        "here and to the allowlist in main.handle_payment_failed." % unknown)


def test_the_stamp_allowlist_names_every_overwritable_reason():
    """The test's OVERWRITABLE and the shipped SQL must not drift apart: this
    test would keep passing against a handler that overwrote nothing."""
    fn = next(n for n in ast.parse(MAIN.read_text(encoding="utf-8")).body
              if isinstance(n, ast.FunctionDef) and n.name == "handle_payment_failed")
    stamps = [n.value for n in ast.walk(fn)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)
              and "SET demoted_at" in n.value]
    assert len(stamps) == 1, "expected exactly one stamp UPDATE, got %d" % len(stamps)
    sql = " ".join(stamps[0].split())
    for reason in OVERWRITABLE:
        assert "demoted_reason = '%s'" % reason in sql, (
            "%r is OVERWRITABLE here but absent from the shipped stamp: %s"
            % (reason, sql))
    for reason in BLOCKING:
        assert reason not in sql, (
            "%r is an operator hold and must not appear in the stamp's "
            "allowlist: %s" % (reason, sql))
    assert "demoted_at IS NULL" in sql, (
        "the undemoted case must still be matched, or no demote ever stamps")
