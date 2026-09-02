"""The Stripe webhook records the sale in the checkout ledgers (2026-09-02, 4a/4c).

MEASURED 2026-09-02T00:32Z: /api/v1/admin/pricing/ab-stats?days=60 read
checkouts:0 for both arms while Stripe listed 13 completed checkout sessions
in 8 weeks — `stripe_checkout_complete` only ever arrived from a browser
beacon on the success page, which no browser sent. /api/v1/checkout/
funnel-stats read converted:0 and identified_checkout_signals.converted was
FALSE for tj@karklins.com, captured 08-08 and paid $49 on Stripe the same
day: nothing had a writer for that column.

Pinned here:
  · routes.pricing_ab.record_stripe_checkout_complete — one row per Stripe
    session (partial UNIQUE + ON CONFLICT carrying the SAME predicate, the
    memory-documented trap), never raises;
  · routes.checkout_email_capture.mark_converted — WHERE converted = FALSE,
    never raises;
  · main.py's checkout.session.completed branch calls both, each inside its
    own try (AST, not grep — a call outside a Try could fail a payment).

★ Every stub cursor here BINDS the SQL first (`sql % params`), as psycopg2
does client-side: a stub more forgiving than the driver certifies SQL the
driver rejects (the literal-percent trap, four instances in this repo).
House rule: tests never import main.
"""
import ast
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _bind(sql, params):
    """psycopg2-shaped binding: every %s consumes one param; a stray % raises."""
    if params is None:
        return sql
    return sql % tuple(repr(p) for p in params)


class _Cur:
    def __init__(self, db):
        self.db = db
        self.rowcount = -1

    def execute(self, sql, params=None):
        _bind(sql, params)
        self.db.executed.append((" ".join(sql.split()), params))
        if self.db.raise_on_execute:
            raise RuntimeError("boom")
        self.rowcount = self.db.rowcount

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rowcount=1, raise_on_execute=False):
        self.executed = []
        self.rowcount = rowcount
        self.raise_on_execute = raise_on_execute
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True

    # context-manager form for checkout_email_capture's `with factory() as c`
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


SESSION = {"id": "cs_test_1", "amount_total": 9900, "mode": "subscription",
           "payment_status": "paid", "client_reference_id": "ref_pricing-page__tool_none",
           "customer_details": {"email": "Buyer@Example.com"}}


# ── pricing_ab ───────────────────────────────────────────────────────

def test_a_completed_session_becomes_one_checkout_row():
    from routes import pricing_ab
    conn = _Conn(rowcount=1)
    out = pricing_ab.record_stripe_checkout_complete(SESSION, conn_factory=lambda: conn)
    assert out["ok"] is True and out["inserted"] is True
    assert out["value_usd"] == 99.0 and out["cohort"] == "A"
    sql, params = conn.executed[-1]
    assert "INSERT INTO pricing_ab_events" in sql
    assert "stripe_checkout_complete" in params and "cs_test_1" in params
    assert 99.0 in params and "ref_pricing-page__tool_none" in params
    assert conn.commits == 1 and conn.closed


def test_the_conflict_clause_repeats_the_partial_index_predicate():
    """★ Postgres will not use a partial UNIQUE index as the ON CONFLICT
    arbiter unless the clause carries the same WHERE (memory: gas_pipelines
    restart loop, 2026-05-21). Pin the INSERT and the index together."""
    from routes import pricing_ab
    conn = _Conn()
    pricing_ab.record_stripe_checkout_complete(SESSION, conn_factory=lambda: conn)
    sql, _ = conn.executed[-1]
    assert re.search(r"ON CONFLICT \(stripe_session_id\) WHERE stripe_session_id IS NOT NULL DO NOTHING",
                     sql), sql
    src = open(os.path.join(ROOT, "routes", "pricing_ab.py"), encoding="utf-8").read()
    i = src.index("pricing_ab_events_stripe_sess_uidx")
    assert "WHERE stripe_session_id IS NOT NULL" in src[i:i + 300]
    assert '"stripe_session_id TEXT"' in src, "the column must be in the defensive ALTER list"


def test_a_redelivered_session_is_not_counted_twice():
    from routes import pricing_ab
    conn = _Conn(rowcount=0)
    out = pricing_ab.record_stripe_checkout_complete(SESSION, conn_factory=lambda: conn)
    assert out["ok"] is True and out["inserted"] is False


def test_cohort_comes_from_metadata_never_from_the_amount():
    """$99 is Arm B's test price AND the founding price — an amount rule
    would book every founding sale as a B conversion."""
    from routes import pricing_ab
    conn = _Conn()
    a = pricing_ab.record_stripe_checkout_complete(SESSION, conn_factory=lambda: conn)
    b = pricing_ab.record_stripe_checkout_complete(
        dict(SESSION, id="cs_2", metadata={"cohort": "b"}), conn_factory=lambda: conn)
    assert a["cohort"] == "A" and b["cohort"] == "B"


@pytest.mark.parametrize("session", [{}, {"id": ""}, dict(SESSION, payment_status="unpaid")])
def test_nothing_is_written_without_a_paid_session(session):
    from routes import pricing_ab
    conn = _Conn()
    out = pricing_ab.record_stripe_checkout_complete(session, conn_factory=lambda: conn)
    assert out["ok"] is False and conn.executed == []


def test_a_stats_failure_never_reaches_payment_handling():
    from routes import pricing_ab
    conn = _Conn(raise_on_execute=True)
    out = pricing_ab.record_stripe_checkout_complete(SESSION, conn_factory=lambda: conn)
    assert out["ok"] is False and "boom" in out["reason"]
    assert conn.rollbacks == 1 and conn.closed
    assert pricing_ab.record_stripe_checkout_complete(SESSION, conn_factory=lambda: None)["reason"] == "no_db"


# ── identified signals ───────────────────────────────────────────────

def test_a_paying_email_is_marked_converted_once():
    from routes import checkout_email_capture as cec
    conn = _Conn(rowcount=1)
    out = cec.mark_converted("Buyer@Example.com", "cs_test_1", conn_factory=lambda: conn)
    assert out == {"ok": True, "updated": 1}
    sql, params = conn.executed[-1]
    assert "UPDATE identified_checkout_signals" in sql
    assert "SET converted = TRUE" in sql and "converted_at = NOW()" in sql
    assert "AND converted = FALSE" in sql, "idempotent by construction"
    assert "lower(email) = %s" in sql and "buyer@example.com" in params
    assert conn.commits == 1


def test_mark_converted_never_raises():
    from routes import checkout_email_capture as cec
    conn = _Conn(raise_on_execute=True)
    out = cec.mark_converted("x@y.z", "cs", conn_factory=lambda: conn)
    assert out["ok"] is False
    assert cec.mark_converted("", "cs", conn_factory=lambda: conn)["reason"] == "no_email"
    assert cec.mark_converted("not-an-email", "cs", conn_factory=lambda: conn)["reason"] == "no_email"


# ── main.py wiring (AST) ─────────────────────────────────────────────

def _webhook_checkout_branch():
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "stripe_webhook")
    parents = {}
    for node in ast.walk(fn):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    branch = None
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and "checkout.session.completed" in ast.get_source_segment(src, node.test):
            branch = node
            break
    assert branch is not None, "checkout.session.completed branch not found"
    return branch, parents


def src_main():
    return open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()


def _calls_named(node, name):
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if (isinstance(f, ast.Name) and f.id == name) or \
               (isinstance(f, ast.Attribute) and f.attr == name):
                out.append(n)
    return out


@pytest.mark.parametrize("alias, helper, module", [
    ("_rec_ab", "record_stripe_checkout_complete", "routes.pricing_ab"),
    ("_mark_conv", "mark_converted", "routes.checkout_email_capture"),
])
def test_the_checkout_branch_calls_each_recorder_inside_its_own_try(alias, helper, module):
    """★ AST, not grep: the CALL must exist in the checkout branch and every
    ancestor chain must pass through a Try, so a ledger failure cannot become
    the r43-H 500-and-retry that is reserved for provisioning."""
    branch, parents = _webhook_checkout_branch()
    calls = _calls_named(branch, alias)
    assert calls, f"{alias}() is not called in the checkout.session.completed branch"
    for call in calls:
        # The NEAREST enclosing Try must be the recorder's OWN fence: its body
        # is nothing but the import and the one statement that makes the call,
        # and its handler swallows (catches Exception, never re-raises). The
        # whole checkout branch already sits inside the r43-H try (main.py
        # ~15610), so "some Try above it" is true of every statement here and
        # proves nothing — a mutation that deleted the inner try stayed green
        # under that weaker check (2026-09-02).
        node, stmt = call, None
        while node in parents and node is not branch and not isinstance(node, ast.Try):
            if isinstance(node, ast.stmt):
                stmt = node
            node = parents[node]
        assert isinstance(node, ast.Try), f"{alias}() is not inside a try"
        assert stmt in node.body, f"{alias}() is not in the try BODY of its fence"
        assert 1 <= len(node.body) <= 2, \
            f"{alias}()'s try fences {len(node.body)} statements, not just the call"
        for extra in node.body:
            assert extra is stmt or isinstance(extra, ast.ImportFrom), \
                f"{alias}()'s try also fences: {ast.dump(extra)[:80]}"
        assert node.handlers and all(
            h.type is not None and ast.get_source_segment(src_main(), h.type) == "Exception"
            for h in node.handlers), f"{alias}()'s fence must catch Exception"
        assert not any(isinstance(n, ast.Raise) for h in node.handlers for n in ast.walk(h)), \
            f"{alias}()'s fence re-raises"
    imports = [n for n in ast.walk(branch) if isinstance(n, ast.ImportFrom)
               and n.module == module and any(a.name == helper and a.asname == alias for a in n.names)]
    assert imports, f"{helper} must be imported from {module} as {alias}"


def test_the_recorders_run_after_the_email_is_known_and_before_provisioning_checks():
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    i = src.index("_rec_ab(data)")
    j = src.index("_mark_conv(customer_email")
    email_at = src.rfind("customer_email = (", 0, i)
    assert email_at != -1 and i < j
    assert "r-receipt (2026-08-17)" in src[j:j + 1500], \
        "the ledger writes sit before the receipt, both fenced"
