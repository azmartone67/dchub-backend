"""main.handle_invoice_paid must restore a dunning-demoted payer's tier.

r46-restore (main.py ~19407): a prior payer pulled to 'free' by the dunning
guard whose card later succeeds fires invoice.paid — NOT a new checkout — so
without this block they stay locked on free tier despite paying again.

SCOPE (owner decision 2026-09-22): both reasons handle_payment_failed writes are
restored, 'dunning_prior_payer' and 'first_charge_never_succeeded'. The second
was pinned NOT restored until then, while the demote email promised the next
payment restores the tier and the web path served paid again on
status='active', so both MCP gates (which read rate_limit_tier since
#5193/#5205) served a paying customer FREE. 'tier_expired_onetime', 'manual'
and 'abuse' stay excluded.

★ IT HAD NO TEST. tests/test_web_path_honors_dunning_demote.py imports
`api_tier_gating` and nothing else, so it cannot reach this handler: its two
mentions of `handle_invoice_paid` are DOCSTRINGS (l.33, l.75), and its 12 tests
all exercise resolve_effective_plan, the WEB predicate. A handler nothing
executes is a handler nothing checks.

HOW THIS REACHES IT. main.py cannot be imported in a unit test (it needs a DB),
so the FunctionDef is compiled out of main.py's AST with its globals stubbed —
the established pattern here. The stub for `_pg_execute` runs the handler's SQL
against a REAL in-memory sqlite3, translating only the dialect (%s -> ?,
NOW() -> CURRENT_TIMESTAMP, GREATEST -> MAX). A fake cursor that returned canned
rows would pass no matter what SQL the handler sent; this one cannot, because
the engine decides the outcome.
"""
import ast
import copy
import sqlite3

import pytest

MAIN = "main.py"
HANDLER = "handle_invoice_paid"
CUSTOMER = "cus_prior_payer"
# What a paid invoice lifts (both written by handle_payment_failed), and what it
# must never touch.
RESTORED = ("dunning_prior_payer", "first_charge_never_succeeded")
EXCLUDED = ("tier_expired_onetime", "manual", "abuse")


def _load_handler(ns_extra):
    src = open(MAIN, encoding="utf-8").read()
    # The handler calls the r-trial-dunning helpers, so lift them with it; they
    # run against the same stubbed globals (STRIPE_AVAILABLE=False -> None/False).
    names = (HANDLER, "_real_paid_invoice_count", "_is_post_trial_first_charge")
    fns = [copy.deepcopy(n) for n in ast.parse(src).body
           if isinstance(n, ast.FunctionDef) and n.name in names]
    for fn in fns:
        fn.decorator_list = []
    ns = dict(ns_extra)
    exec(compile(ast.Module(body=fns, type_ignores=[]), MAIN, "exec"), ns)
    return ns[HANDLER]


class _Harness:
    """A real engine behind _pg_execute, so the handler's own SQL decides."""

    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                stripe_customer_id TEXT,
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
        """)
        self.sql_log = []

    def pg_execute(self, sql, params=()):
        self.sql_log.append(" ".join(sql.split()))
        translated = (sql.replace("%s", "?")
                         .replace("NOW()", "CURRENT_TIMESTAMP")
                         .replace("GREATEST", "MAX"))
        self.db.execute(translated, params)
        self.db.commit()

    # -- fixture helpers -------------------------------------------------
    def add_user(self, uid, *, reason, demoted_at="2026-09-01 00:00:00",
                 tier="free", plan="pro", customer=CUSTOMER):
        self.db.execute(
            "INSERT INTO users (id, stripe_customer_id, demoted_at, demoted_reason)"
            " VALUES (?,?,?,?)", (uid, customer, demoted_at, reason))
        self.db.execute(
            "INSERT INTO api_keys (user_id, rate_limit_tier, plan) VALUES (?,?,?) ON CONFLICT DO NOTHING",
            (uid, tier, plan))
        self.db.commit()

    def key(self, uid):
        r = self.db.execute(
            "SELECT rate_limit_tier, plan FROM api_keys WHERE user_id=?", (uid,)).fetchone()
        return {"rate_limit_tier": r[0], "plan": r[1]}

    def user(self, uid):
        r = self.db.execute(
            "SELECT demoted_at, demoted_reason FROM users WHERE id=?", (uid,)).fetchone()
        return {"demoted_at": r[0], "demoted_reason": r[1]}


@pytest.fixture
def harness():
    h = _Harness()
    sqlite_mirror = sqlite3.connect(":memory:")
    sqlite_mirror.executescript(
        "CREATE TABLE users (stripe_customer_id TEXT, invoices_paid_count INTEGER,"
        " payment_failed_count INTEGER, subscription_status TEXT);")
    h.handler = _load_handler({
        "STRIPE_AVAILABLE": False,          # skip the Stripe recount path
        "stripe": None,
        "_pg_execute": h.pg_execute,
        "get_db": lambda: sqlite_mirror,
        "_sync_tables_bg": lambda *a, **k: None,
        "note_swallowed_write": lambda *a, **k: None,
    })
    return h


def _invoice(customer=CUSTOMER):
    return {"customer": customer}


# ── the restore ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("reason", RESTORED)
def test_a_dunning_demoted_payer_is_restored_from_plan(harness, reason):
    """The whole point of r46-restore. The demote lowered rate_limit_tier and
    left `plan` intact, so `plan` is what the tier comes back to.

    Both dunning reasons: the demote email tells either customer "the next
    successful payment restores your tier automatically"."""
    harness.add_user(1, reason=reason, tier="free", plan="pro")
    harness.handler(_invoice())
    assert harness.key(1)["rate_limit_tier"] == "pro", (
        "tier stayed %r — a payer whose card recovered is still locked on free"
        % harness.key(1)["rate_limit_tier"]
    )
    assert harness.user(1) == {"demoted_at": None, "demoted_reason": None}


def test_the_keys_are_restored_before_the_stamp_they_select_on_is_cleared(harness):
    """★ THE ORDERING IS LOAD-BEARING, and it is invisible in a source read.

    The api_keys UPDATE selects on `demoted_reason='dunning_prior_payer' AND
    demoted_at IS NOT NULL`. The users UPDATE erases exactly that stamp. Run the
    users UPDATE first and the subquery matches nothing, so the tier is NEVER
    restored — while the stamp is gone, so the next invoice.paid cannot fix it
    either. Silent, permanent, and it looks fine in both statements."""
    harness.add_user(1, reason="dunning_prior_payer", tier="free", plan="pro")
    harness.handler(_invoice())
    restore = [i for i, s in enumerate(harness.sql_log)
               if s.startswith("UPDATE api_keys") and "rate_limit_tier = plan" in s]
    clear = [i for i, s in enumerate(harness.sql_log)
             if s.startswith("UPDATE users") and "demoted_at = NULL" in s]
    assert restore and clear, "expected both restore statements, got %s" % harness.sql_log
    assert restore[0] < clear[0], "the stamp was cleared before the keys were restored"
    assert harness.key(1)["rate_limit_tier"] == "pro"


# ── the scoping ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("reason", EXCLUDED)
def test_a_demote_with_another_reason_is_never_touched(harness, reason):
    """Scoped to the two dunning reasons. A manual or abuse demote is a
    decision, not a payment failure, and paying an invoice must not undo it.
    'tier_expired_onetime' (routes/expired_demote.py) also set users.plan to
    'free', so restoring its rate_limit_tier from api_keys.plan would make the
    API path outrank the web path: the inversion the other way round."""
    harness.add_user(1, reason=reason, tier="free", plan="pro")
    harness.handler(_invoice())
    assert harness.key(1)["rate_limit_tier"] == "free", (
        "%r demote was lifted by an invoice payment" % reason
    )
    assert harness.user(1)["demoted_reason"] == reason


@pytest.mark.parametrize("demoted_at", ["2026-09-01 00:00:00", None],
                         ids=["dated", "undated"])
@pytest.mark.parametrize("reason", RESTORED + EXCLUDED)
def test_both_statements_select_the_same_rows(harness, reason, demoted_at):
    """★ #4932's invariant, for EVERY reason. The two restore statements are
    halves of one operation: a stamp cleared without its key restored leaves
    the key on 'free' with nothing left for a later invoice.paid to match, and
    a key restored with its stamp kept still reads as demoted. Widening one
    statement's reason list and not the other breaks exactly one half, so this
    compares the two outcomes row by row instead of trusting the SQL text."""
    harness.add_user(1, reason=reason, demoted_at=demoted_at, tier="free", plan="pro")
    harness.handler(_invoice())
    restored = harness.key(1)["rate_limit_tier"] == "pro"
    cleared = harness.user(1)["demoted_reason"] is None
    assert restored == cleared, (
        "%r (demoted_at=%r): key restored=%s but stamp cleared=%s, so the two "
        "statements selected different rows" % (reason, demoted_at, restored, cleared))
    assert restored == (reason in RESTORED and demoted_at is not None)


def test_another_customers_demote_is_not_restored(harness):
    harness.add_user(1, reason="dunning_prior_payer", tier="free", plan="pro")
    harness.add_user(2, reason="dunning_prior_payer", tier="free", plan="pro",
                     customer="cus_someone_else")
    harness.handler(_invoice())
    assert harness.key(1)["rate_limit_tier"] == "pro"
    assert harness.key(2)["rate_limit_tier"] == "free", "restored the wrong customer"


def test_an_undemoted_payer_is_left_alone(harness):
    """invoice.paid fires on every renewal. The overwhelming majority of them
    involve nobody demoted, and those keys must not be rewritten."""
    harness.add_user(1, reason=None, demoted_at=None, tier="starter", plan="pro")
    harness.handler(_invoice())
    assert harness.key(1)["rate_limit_tier"] == "starter", (
        "a healthy key was overwritten from plan on an ordinary renewal"
    )


def test_restoring_twice_is_idempotent(harness):
    """Stripe can deliver invoice.paid more than once, and the handler is
    dispatched on both invoice.paid and invoice.payment_succeeded."""
    harness.add_user(1, reason="dunning_prior_payer", tier="free", plan="pro")
    harness.handler(_invoice())
    first = harness.key(1)
    harness.handler(_invoice())
    assert harness.key(1) == first
    assert harness.user(1) == {"demoted_at": None, "demoted_reason": None}


@pytest.mark.parametrize("reason", RESTORED)
def test_a_stamp_with_no_demoted_at_is_left_intact_not_erased(harness, reason):
    """Both restore statements carry `demoted_at IS NOT NULL`, so a row whose
    reason is stamped with a NULL demoted_at is selected by NEITHER.

    This was a CHARACTERISATION until #4932. The users UPDATE lacked the clause
    while the api_keys UPDATE had it, so the reason was erased while the key was
    left on 'free' — and the reason is exactly what a later invoice.paid selects
    on, so that combination was unrecoverable. The requirement is that a stamp
    OUTLIVE a restore that did not happen."""
    harness.add_user(1, reason=reason, demoted_at=None,
                     tier="free", plan="pro")
    harness.handler(_invoice())
    assert harness.key(1)["rate_limit_tier"] == "free", (
        "no demoted_at means no demote to reverse — the tier is not raised"
    )
    assert harness.user(1)["demoted_reason"] == reason, (
        "the stamp must survive; it is what a later restore matches on"
    )


@pytest.mark.parametrize("reason", RESTORED)
def test_a_stamp_that_survives_is_still_restorable_once_dated(harness, reason):
    """The point of leaving the stamp alone: recovery stays possible.

    Proves the whole path end to end — a NULL-dated stamp survives one
    invoice.paid, and once the missing demoted_at is supplied the NEXT
    invoice.paid restores the tier normally. Without the fix the first call
    erases the reason and this second call has nothing to match."""
    harness.add_user(1, reason=reason, demoted_at=None,
                     tier="free", plan="pro")
    harness.handler(_invoice())

    harness.db.execute(
        "UPDATE users SET demoted_at = ? WHERE id = 1", ("2026-09-01 00:00:00",))
    harness.db.commit()

    harness.handler(_invoice())
    assert harness.key(1)["rate_limit_tier"] == "pro", (
        "the surviving stamp let the dated row restore on the next payment"
    )
    assert harness.user(1)["demoted_reason"] is None


# ── characterisations: recorded hazards, NOT requirements ────────────────

def test_restore_follows_api_keys_plan_even_when_plan_was_lowered(harness):
    """★ CHARACTERISATION — records today's behaviour and a live hazard.

    Restore reads `api_keys.plan` and trusts it. Other writers CAN lower `plan`
    while a user is demoted — see the "no-downgrade floor (v2)" comment at
    api_tier_gating.py:1525, where a pack purchase demoted admin001 pro->starter.
    A `plan` lowered during the dunning window is restored to the LOWER tier and
    the stamp is cleared, so nothing records that the customer was ever pro.

    This is not asserted as correct. If restore is changed to source the tier
    from somewhere `plan` cannot be lowered under it, CHANGE THIS TEST — it
    pins a hazard, not a requirement."""
    harness.add_user(1, reason="dunning_prior_payer", tier="free", plan="starter")
    harness.handler(_invoice())
    assert harness.key(1)["rate_limit_tier"] == "starter"
    assert harness.user(1)["demoted_reason"] is None, (
        "the stamp is cleared regardless, so the pro-ness is unrecoverable"
    )

