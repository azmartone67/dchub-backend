"""The IDENTIFY rung: an email bound to the MCP session that earned it.

Measured live 2026-09-17 (/api/v1/mcp/handoff-funnel, 30d):
    paywall_hit 941 → high_intent 309 → relay_minted 307
    → human_acted 7 → identified 0 → paid_attributed 0

`identified` counts mcp_high_intent_sessions.claim_email. Nothing on the path a
human walks had ever written it, so the stage could not move — and a payment
could reach paid_attributed past an identified of 0, leaving a real customer
nameless. routes/relay_identify writes it from the two places a human is
actually present: the relay page's form, and the paid checkout's Stripe email.

WHAT EACH GUARD HERE PROTECTS is named on the test. The two that matter most:
  * the session comes from the SIGNED token, never from the posted form;
  * the checkout lookup is the SAME join paid_attributed uses, so one payment
    cannot be attributed to two different sessions by two stages.
"""
import ast
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RELAY_SRC = (ROOT / "routes" / "human_relay.py").read_text(encoding="utf-8")
IDENT_SRC = (ROOT / "routes" / "relay_identify.py").read_text(encoding="utf-8")
MAIN_SRC = (ROOT / "main.py").read_text(encoding="utf-8")

import routes.relay_identify as ri  # noqa: E402


# ── the shared join ──────────────────────────────────────────────────────
def test_the_checkout_lookup_is_the_paid_attributed_join_itself():
    """★ ONE definition, two readers.

    A second copy would let `identified` and `paid_attributed` attribute the
    same payment to two different sessions while both looked measured. Built
    from the same function under the same substitutions, the two strings are
    identical — so a change to the join reaches both or neither.
    """
    from routes.handoff_definition import (
        _relayed_click_session_for, paid_relayed_click_session_sql,
        relayed_click_session_for_ref_sql)

    funnel = paid_relayed_click_session_sql()
    live = relayed_click_session_for_ref_sql()

    assert funnel == _relayed_click_session_for(
        "pay.client_reference_id", "pay.paid_at")
    assert live == _relayed_click_session_for("%s", "now()")
    # The ONLY difference is the two substituted expressions.
    assert (funnel.replace("pay.client_reference_id", "%s")
                  .replace("pay.paid_at", "now()")) == live
    # And the lane's own filters ride both — not restated in either.
    # r-paid-attributed-keyed-refs (2026-09-23): this join's identity widened
    # past a session, to also accept a durable pack_key/sub_key ref —
    # paid_attributed_click_filters(), not relayed_checkout_session_filters()
    # (that one stays human_acted_v7's alone, unchanged).
    from routes.handoff_definition import paid_attributed_click_filters
    for sql in (funnel, live):
        assert paid_attributed_click_filters() in sql


def test_the_live_lookup_takes_exactly_one_bound_parameter():
    """A second %s would silently shift the ref onto the wrong slot."""
    from routes.handoff_definition import relayed_click_session_for_ref_sql
    assert relayed_click_session_for_ref_sql().count("%s") == 1


# ── capture(): what it refuses, and what it actually writes ──────────────
class _Cur:
    """A cursor that READS the SQL it is given. A fake that ignores its
    argument would make every assertion below vacuous."""

    def __init__(self, hi_rows=1, sig_rows=1, replay=False):
        self.sql = []
        self.params = []
        self.rowcount = 0
        self._hi, self._sig, self._replay = hi_rows, sig_rows, replay

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))
        self.params.append(params)
        low = self.sql[-1].lower()
        if "update mcp_high_intent_sessions" in low:
            self.rowcount = self._hi
        elif "update mcp_upgrade_signals" in low:
            self.rowcount = self._sig
        elif low.startswith("select to_regclass"):
            self._ret = [True]
        elif "insert into relay_identify_captures" in low:
            # RETURNING id — a row means inserted, None means a replayed
            # checkout webhook hit the partial unique index.
            self._ret = None if self._replay else [1]
            self.rowcount = 0 if self._replay else 1
        else:
            self.rowcount = 1

    def fetchone(self):
        return getattr(self, "_ret", [True])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur
        self.autocommit = False
        self.closed = False

    def cursor(self):
        return self._cur

    def close(self):
        self.closed = True


@pytest.fixture
def wired(monkeypatch):
    """relay_identify with a schema already present and a readable cursor."""
    cur = _Cur()
    monkeypatch.setattr(ri, "_pg", types.SimpleNamespace(
        connect=lambda *a, **k: _Conn(cur)))
    monkeypatch.setattr(ri, "_dsn", lambda: "postgres://test")
    monkeypatch.setattr(ri, "_SCHEMA_READY", [True])
    monkeypatch.setattr(ri, "deliverable", lambda e: True)
    monkeypatch.delenv("DCHUB_RELAY_IDENTIFY_DISABLE", raising=False)
    return cur


@pytest.mark.parametrize("sid,email,why", [
    ("", "a@b.co", "no_session"),
    ("no-session", "a@b.co", "no_session"),
    ("s-1", "", "invalid_email"),
    ("s-1", "not-an-email", "invalid_email"),
    ("s-1", "a@b", "invalid_email"),
])
def test_capture_refuses_what_it_cannot_bind(wired, sid, email, why):
    out = ri.capture(sid, email)
    assert out == {"ok": False, "skipped": why}
    assert wired.sql == [], "a refused capture still wrote"


def test_capture_stamps_the_rung_the_reachability_row_and_the_raw_fact(wired):
    out = ri.capture("sess-42", "  Human@Example.COM ", "relay_page",
                     tool="analyze_site")
    assert out["ok"] is True
    assert out["stamped_high_intent"] is True
    joined = " | ".join(wired.sql).lower()
    assert "update mcp_high_intent_sessions" in joined
    assert "update mcp_upgrade_signals" in joined
    assert "insert into relay_identify_captures" in joined
    # Normalised once, written everywhere the same.
    for p in wired.params:
        if p:
            assert "Human@Example.COM" not in p, "raw case/whitespace persisted"
    assert any("human@example.com" in [str(x) for x in (p or [])]
               for p in wired.params)


def test_capture_never_overwrites_an_email_already_bound(wired):
    ri.capture("sess-42", "a@b.co")
    upd = [q for q in wired.sql if q.lower().startswith("update mcp_high_intent")][0]
    assert "claim_email IS NULL OR claim_email = ''" in upd, (
        "the guard that makes a second capture non-destructive is gone")


def test_capture_never_inserts_a_high_intent_row():
    """★ The rung's denominator is the stage ABOVE it. Fabricating a row to
    make `identified` move would inflate high_intent and relay_minted too."""
    assert "INSERT INTO mcp_high_intent_sessions" not in IDENT_SRC
    assert "insert into mcp_high_intent_sessions" not in IDENT_SRC.lower()


def test_stamped_high_intent_is_read_back_not_assumed(monkeypatch):
    """★ False when the session has no row to stamp. If this were hardcoded
    True, a capture the rung cannot count would be published as one it can."""
    cur = _Cur(hi_rows=0)
    monkeypatch.setattr(ri, "_pg", types.SimpleNamespace(
        connect=lambda *a, **k: _Conn(cur)))
    monkeypatch.setattr(ri, "_dsn", lambda: "postgres://test")
    monkeypatch.setattr(ri, "_SCHEMA_READY", [True])
    monkeypatch.setattr(ri, "deliverable", lambda e: True)
    out = ri.capture("ghost-session", "a@b.co")
    assert out["ok"] is True
    assert out["stamped_high_intent"] is False
    ins = [p for q, p in zip(cur.sql, cur.params)
           if "insert into relay_identify_captures" in q.lower()][0]
    assert False in ins, "the raw row claimed a rung it did not reach"


def test_the_kill_switch_stops_the_capture_not_the_page(wired, monkeypatch):
    monkeypatch.setenv("DCHUB_RELAY_IDENTIFY_DISABLE", "1")
    assert ri.capture("sess-42", "a@b.co") == {"ok": False, "skipped": "disabled"}
    assert wired.sql == []


def test_deliverability_failing_open_is_deliberate(monkeypatch):
    """A validator that cannot be imported must not silently kill the rung."""
    monkeypatch.setitem(sys.modules, "routes.email_validation", None)
    assert ri.deliverable("a@b.co") is True


# ── capture_from_checkout(): the money side ──────────────────────────────
@pytest.mark.parametrize("session,why", [
    ({"payment_status": "unpaid", "client_reference_id": "r"}, "not_paid"),
    ({"payment_status": "paid", "client_reference_id": "r"}, "no_email"),
    ({"payment_status": "paid", "customer_email": "a@b.co"}, "no_ref"),
])
def test_checkout_capture_refuses_what_is_not_a_named_customer(session, why):
    assert ri.capture_from_checkout(session) == {"ok": False, "skipped": why}


def test_checkout_capture_says_so_when_no_relayed_click_sold_it(monkeypatch):
    """★ The same silence paid_attributed keeps. A payment with no signed,
    session-bearing click inside the lookback is unattributable to BOTH
    stages — never stamped onto a guessed session."""
    monkeypatch.setattr(ri, "session_for_checkout_ref", lambda ref: "")
    out = ri.capture_from_checkout({
        "payment_status": "paid", "client_reference_id": "pk-" + "a" * 64,
        "customer_details": {"email": "buyer@co.com"}, "id": "cs_1"})
    assert out == {"ok": False, "skipped": "no_relayed_click_session"}


def test_checkout_capture_binds_the_stripe_email_to_the_joined_session(monkeypatch):
    seen = {}
    monkeypatch.setattr(ri, "session_for_checkout_ref", lambda ref: "sess-joined")
    monkeypatch.setattr(ri, "capture",
                        lambda sid, email, src, **kw: seen.update(
                            sid=sid, email=email, src=src, **kw) or {"ok": True})
    ri.capture_from_checkout({
        "payment_status": "paid", "client_reference_id": "pk-" + "a" * 64,
        "customer_details": {"email": "buyer@co.com"}, "id": "cs_123"})
    assert seen["sid"] == "sess-joined"
    assert seen["email"] == "buyer@co.com"
    assert seen["src"] == "checkout"
    assert seen["stripe_session_id"] == "cs_123"


# ── the relay page ───────────────────────────────────────────────────────
def test_the_posted_form_cannot_choose_the_session():
    """★★ The session is read from the SIGNED token only. If _relay_identify
    ever read a sid out of request.form, anyone holding any link could stamp
    any session's email."""
    tree = ast.parse(RELAY_SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_relay_identify")
    body = ast.get_source_segment(RELAY_SRC, fn) or ""
    assert 'request.form.get("email")' in body
    for bad in ("request.form.get(\"sid\")", "request.form.get('sid')",
                "request.args.get(\"sid\")", "request.values"):
        assert bad not in body, f"the form reached the session identity via {bad}"
    assert '(info or {}).get("sid")' in body, "sid no longer comes from the token"


def test_the_form_and_the_button_sell_the_same_checkout():
    """One page, one price. The POST redirects to _upgrade_target, which is
    the same function that renders the button's href."""
    tree = ast.parse(RELAY_SRC)
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "_upgrade_target" in names
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_relay_identify")
    body = ast.get_source_segment(RELAY_SRC, fn) or ""
    assert "_upgrade_target(info)" in body
    assert "redirect(dest" in body


def test_capture_failure_still_reaches_checkout():
    """★ A payment surface must not be gated on telemetry. The redirect is
    outside the try, so a raising capture cannot strand a buyer."""
    tree = ast.parse(RELAY_SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_relay_identify")
    returns_in_try = [n for t in ast.walk(fn) if isinstance(t, ast.Try)
                      for n in ast.walk(t) if isinstance(n, ast.Return)]
    assert not returns_in_try, "the redirect is inside the try — a throw loses the buyer"
    assert isinstance(fn.body[-1], ast.Return)


def test_no_session_means_no_email_field():
    """Collecting a lead we cannot attach to anything is worse than not
    collecting it: it reads as a captured identity the rung will never show."""
    tree = ast.parse(RELAY_SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_identify_form")
    body = ast.get_source_segment(RELAY_SRC, fn) or ""
    assert 'if not (info or {}).get("sid")' in body
    assert 'return ""' in body


def test_a_post_is_not_logged_as_a_human_open():
    """human_acted reads relay_opens. Logging the submit too would count the
    human who filled the form in twice against the one who only looked."""
    tree = ast.parse(RELAY_SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "relay_page")
    body = ast.get_source_segment(RELAY_SRC, fn) or ""
    post_branch = body[body.index('if request.method == "POST"'):]
    early_return = post_branch[:post_branch.index("\n", post_branch.index("return"))]
    assert "_relay_identify" in early_return
    assert body.index('if request.method == "POST"') < body.index("_log_open(")


def test_the_route_accepts_the_post_on_its_own_path():
    """_routes.json is at the 98-rule deploy cap, so a new top-level path
    would be dropped silently at the edge and never reach Flask."""
    assert '"/upgrade/h/<token>", methods=["GET", "POST"]' in RELAY_SRC
    assert "action='/upgrade/h/%s'" in RELAY_SRC


# ── the webhook ──────────────────────────────────────────────────────────
def test_the_webhook_captures_beside_the_payment_row_and_cannot_break_it():
    i = MAIN_SRC.index("from routes.relay_identify import capture_from_checkout")
    seg = MAIN_SRC[i - 1200:i + 900]
    assert "record_checkout_payment" in seg, "capture drifted away from the payment row"
    assert "except Exception" in seg and "non-fatal" in seg


# ── the read side ────────────────────────────────────────────────────────
def test_captures_are_published_beside_the_rung_they_may_not_reach():
    funnel = (ROOT / "flask_mcp_endpoints.py").read_text(encoding="utf-8")
    assert '"identify_captures"' in funnel
    assert '"identify_captures_basis"' in funnel
    sql = ri.captures_count_sql("30 days")
    assert "stamped_high_intent" in sql and "reached_the_rung" in sql
    assert "interval '30 days'" in sql
    # The scanner counts a table as READ only from a single literal carrying
    # SELECT and an uppercase FROM.
    assert "FROM relay_identify_captures" in sql


def test_a_replayed_checkout_webhook_does_not_double_count_a_capture(monkeypatch):
    """★ Stripe re-delivers checkout.session.completed and more than one
    endpoint receives it. Without the partial unique index, one purchase would
    publish as two captured identities."""
    cur = _Cur(replay=True)
    monkeypatch.setattr(ri, "_pg", types.SimpleNamespace(
        connect=lambda *a, **k: _Conn(cur)))
    monkeypatch.setattr(ri, "_dsn", lambda: "postgres://test")
    monkeypatch.setattr(ri, "_SCHEMA_READY", [True])
    monkeypatch.setattr(ri, "deliverable", lambda e: True)
    out = ri.capture("sess-42", "a@b.co", "checkout", stripe_session_id="cs_9")
    assert out["ok"] is True and out["idempotent"] is True
    ins = [q for q in cur.sql if "insert into relay_identify_captures" in q.lower()][0]
    assert "ON CONFLICT DO NOTHING" in ins
    assert "ux_relay_identify_captures_stripe" in IDENT_SRC
    assert "WHERE stripe_session_id IS NOT NULL" in IDENT_SRC, (
        "a non-partial unique index would swallow a second relay-page capture too")
