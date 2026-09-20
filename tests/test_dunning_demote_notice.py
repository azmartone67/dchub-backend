"""The dunning demote must tell the human whose card failed.

Before r-demote-notice the demote wrote rate_limit_tier='free', stamped
users.demoted_at/demoted_reason, and emitted a print() plus an in-memory list.
Nothing else. `demoted` appears in neither flask_mcp_endpoints.py nor
mcp_gatekeeper.py, so the agent is served free tier indistinguishably from an
ordinary free key — and the HUMAN, the only party who can fix the card, was
never told.

★ WHY THE GATE IS A ROWCOUNT. The demote UPDATE carries `AND demoted_at IS
NULL`, so it touches a row only on the TRANSITION into demoted. A dunning cycle
fires repeatedly and Stripe re-delivers webhooks, so emailing per failed charge
would spam a customer whose card is already failing. These tests drive the real
handler against a real sqlite engine, so the rowcount is decided by the SQL, not
by a fixture asserting what it hopes the SQL does.

main.py cannot be imported in a unit test, so both functions are compiled out of
its AST with their globals stubbed — the established pattern here.
"""
import ast
import copy
import sqlite3

import pytest

MAIN = "main.py"
CUSTOMER = "cus_dunning"


def _load(name, ns_extra):
    src = open(MAIN, encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == name)
    fn = copy.deepcopy(fn)
    fn.decorator_list = []
    ns = dict(ns_extra)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), MAIN, "exec"), ns)
    return ns[name], ns


class _Inline:
    """threading.Thread stand-in that runs the target NOW, so assertions are
    deterministic. The production call is fire-and-forget on purpose — a mail
    failure must never turn a completed demote into a webhook exception."""
    def __init__(self, target=None, daemon=None, **kw):
        self._t = target
    def start(self):
        self._t()


class _Mail:
    def __init__(self, result="msg_1"):
        self.sent = []
        self.result = result
    def __call__(self, to_email, subject, html, **kw):
        self.sent.append({"to": to_email, "subject": subject, "html": html})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _db():
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, stripe_customer_id TEXT, email TEXT,
            plan TEXT, subscription_status TEXT,
            invoices_paid_count INTEGER DEFAULT 0,
            payment_failed_count INTEGER DEFAULT 0,
            demoted_at TEXT, demoted_reason TEXT);
        CREATE TABLE api_keys (user_id INTEGER, rate_limit_tier TEXT,
                               plan TEXT, last_used_at TEXT);
        CREATE TABLE welcome_email_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT, plan TEXT,
            status TEXT, resend_message_id TEXT, attempted_at TEXT);
    """)
    return db


def _pg(db, log=None):
    def _pg_execute(query, params=(), fetch=False):
        sql = (query.replace("%s", "?").replace("NOW()", "CURRENT_TIMESTAMP")
                    .replace("GREATEST", "MAX"))
        if log is not None:
            log.append(" ".join(query.split()))
        try:
            cur = db.execute(sql, params)
            rows = cur.fetchall() if fetch else []
            db.commit()
            return (cur.rowcount, rows)
        except Exception:
            return (0, [])
    return _pg_execute


# ── the sender ───────────────────────────────────────────────────────────

def _sender(db, mail, env=None):
    import os as _os
    fake_env = dict(env or {})

    class _Os:
        environ = fake_env
    fn, _ = _load("_send_dunning_demote_notice", {
        "_pg_execute": _pg(db), "_resend_email": mail,
        "os": _Os, "threading": type("T", (), {"Thread": _Inline}),
    })
    return fn


def test_the_notice_is_sent_and_recorded():
    db, mail = _db(), _Mail()
    _sender(db, mail)("payer@example.com", "pro", "dunning_prior_payer", 7, "2026-09-20 01:00:00")
    assert len(mail.sent) == 1
    assert mail.sent[0]["to"] == "payer@example.com"
    row = db.execute("SELECT plan, status, resend_message_id FROM welcome_email_log").fetchone()
    assert row[0] == "demote:7:2026-09-20 01:00:00"
    assert row[1] == "sent_via_resend"
    assert row[2] == "msg_1"


def test_the_body_says_the_key_still_works():
    """The demote deliberately keeps the key ACTIVE at free tier. An email that
    read like a revocation would cost a customer who is still being served."""
    db, mail = _db(), _Mail()
    _sender(db, mail)("payer@example.com", "pro", "dunning_prior_payer", 7, "t")
    # Collapse whitespace: the template wraps, so these phrases span newline +
    # indent in the source and a bare `in html` misses them.
    html = " ".join(mail.sent[0]["html"].split())
    assert "still works" in html
    assert "free tier" in html
    assert "restores your tier automatically" in html


def test_a_second_delivery_of_the_same_demote_does_not_resend():
    """Stripe re-delivers webhooks. The claim row keys on this demote."""
    db, mail = _db(), _Mail()
    send = _sender(db, mail)
    send("payer@example.com", "pro", "dunning_prior_payer", 7, "2026-09-20 01:00:00")
    send("payer@example.com", "pro", "dunning_prior_payer", 7, "2026-09-20 01:00:00")
    assert len(mail.sent) == 1


def test_a_later_demote_of_the_same_user_does_send():
    """★ The claim key carries the STAMP, not just the user. Keying it on the
    user alone would silence every future demote for anyone demoted once."""
    db, mail = _db(), _Mail()
    send = _sender(db, mail)
    send("payer@example.com", "pro", "dunning_prior_payer", 7, "2026-09-20 01:00:00")
    send("payer@example.com", "pro", "dunning_prior_payer", 7, "2026-12-01 09:00:00")
    assert len(mail.sent) == 2


def test_the_kill_switch_silences_it():
    db, mail = _db(), _Mail()
    _sender(db, mail, env={"DCHUB_DEMOTE_NOTICE_DISABLE": "1"})(
        "payer@example.com", "pro", "dunning_prior_payer", 7, "t")
    assert mail.sent == []
    assert db.execute("SELECT COUNT(*) FROM welcome_email_log").fetchone()[0] == 0


def test_no_address_means_no_send_and_no_claim():
    db, mail = _db(), _Mail()
    _sender(db, mail)(None, "pro", "dunning_prior_payer", 7, "t")
    assert mail.sent == []
    assert db.execute("SELECT COUNT(*) FROM welcome_email_log").fetchone()[0] == 0


def test_a_send_failure_is_recorded_and_never_raises():
    """A mail failure must not turn a completed demote into an exception."""
    db, mail = _db(), _Mail(result=RuntimeError("resend down"))
    _sender(db, mail)("payer@example.com", "pro", "dunning_prior_payer", 7, "t")
    status = db.execute("SELECT status FROM welcome_email_log").fetchone()[0]
    assert status == "claimed", "the claim row should survive a failed send"


def test_a_falsy_send_result_is_recorded_as_failed():
    db, mail = _db(), _Mail(result=False)
    _sender(db, mail)("payer@example.com", "pro", "dunning_prior_payer", 7, "t")
    assert db.execute("SELECT status FROM welcome_email_log").fetchone()[0] == "failed"


# ── the call site: only on the transition ────────────────────────────────

def _handler(db, notices):
    fn, _ = _load("handle_payment_failed", {
        "STRIPE_AVAILABLE": False, "stripe": None,
        "_pg_execute": _pg(db),
        "_send_dunning_demote_notice": lambda *a, **k: notices.append(a),
        "_sync_tables_bg": lambda *a, **k: None,
        "get_db": lambda: _db(),
        "note_swallowed_write": lambda *a, **k: None,
        "utc_iso_z": lambda: "2026-09-20T01:00:00Z",
    })
    return fn


def _seed(db, *, paid, failed, demoted_at=None, reason=None):
    db.execute("INSERT INTO users (id, stripe_customer_id, email, plan,"
               " invoices_paid_count, payment_failed_count, demoted_at, demoted_reason)"
               " VALUES (1,?,?,?,?,?,?,?)",
               (CUSTOMER, "payer@example.com", "pro", paid, failed, demoted_at, reason))
    db.execute("INSERT INTO api_keys (user_id, rate_limit_tier, plan)"
               " VALUES (1,'pro','pro')")
    db.commit()


def test_the_notice_fires_on_the_demote_transition():
    db, notices = _db(), []
    # DEMOTE_PRIOR_PAYER_AFTER_N_FAILURES is 4 and the handler bumps the count
    # first, so 3 + this failure == 4 is the first pass that demotes.
    _seed(db, paid=3, failed=3)
    _handler(db, notices)({"customer": CUSTOMER, "attempt_count": 3})
    assert db.execute("SELECT demoted_reason FROM users WHERE id=1").fetchone()[0] \
        == "dunning_prior_payer", "fixture did not actually demote"
    assert len(notices) == 1, "the demote sent no notice"


def test_a_repeat_failure_after_the_demote_sends_nothing():
    """★ THE SPAM GUARD. The dunning cycle keeps firing on an already-demoted
    user; the UPDATE's `AND demoted_at IS NULL` means rowcount 0, and the notice
    must follow the rowcount rather than the fact a demote was 'applied'."""
    db, notices = _db(), []
    _seed(db, paid=3, failed=9, demoted_at="2026-09-01", reason="dunning_prior_payer")
    _handler(db, notices)({"customer": CUSTOMER, "attempt_count": 9})
    # The demote BRANCH must really have been entered, or this passes for the
    # wrong reason — a threshold that was never reached proves nothing about
    # the rowcount gate.
    row = db.execute("SELECT payment_failed_count, demoted_at FROM users WHERE id=1").fetchone()
    assert row[0] >= 4, "fixture never reached the demote threshold"
    assert row[1] == "2026-09-01", "the original stamp should be untouched"
    assert notices == [], "a second dunning failure re-notified an already-demoted user"


def test_an_ordinary_failure_below_the_threshold_sends_nothing():
    db, notices = _db(), []
    _seed(db, paid=3, failed=0)
    _handler(db, notices)({"customer": CUSTOMER, "attempt_count": 1})
    assert db.execute("SELECT demoted_reason FROM users WHERE id=1").fetchone()[0] is None
    assert notices == []
