"""Trial emails say "trial", not "payment" (r-trial-copy, 2026-09-24 live gate).

The live Pro-trial gate found, for a $0 trial start:
  (a) receipt: "Thanks, payment received, 0.00 USD" and a 28-char Reference
      that, pasted into /upgrade/h/done?cs=, could not resolve;
  (b) key welcome: "Welcome to DC Hub Paid:Mint!" and "10,000 API calls/day"
      (Pro is 2,000); the upgrade welcome: "Your Upgrade is Active";
  (d) /api/v1/account/entitlements: tier "anonymous", email_bound false for a
      bound free key.
The receipt is RENDERED here (lifted from main.py with its I/O stubbed), not
grepped.
"""
import ast
import copy
import pathlib
import threading

import pytest

from routes import trial_copy as tc

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN_SRC = (ROOT / "main.py").read_text(encoding="utf-8")
MAIN = ast.parse(MAIN_SRC)
OCT1 = 1790812800  # 2026-10-01 00:00:00 UTC


def _fn(name, tree=MAIN):
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(fns) == 1, f"{name} not found exactly once"
    return fns[0]


# ── the shared wording ──────────────────────────────────────────────────

def test_trial_block_states_start_price_date_and_cancel():
    from tier_registry import price_display
    html = tc.trial_block_html(OCT1)
    assert "7-day Pro trial has started" in html
    assert "Nothing was charged today" in html
    assert price_display("pro") in html and "October 1, 2026" in html
    assert "https://dchub.cloud/dashboard" in html and "cancel" in html.lower()


def test_trial_block_without_a_date_still_says_when():
    html = tc.trial_block_html(None)
    assert "when the trial ends" in html and "October" not in html


@pytest.mark.parametrize("plan,trial,want", [
    ("paid:mint", None, "Paid"),
    ("paid:upgrade", None, "Paid"),
    ("pro", None, "Pro"),
    ("developer", None, "Developer"),
    ("pro_monthly", OCT1, "Pro (7-day free trial)"),
    ("paid:mint", OCT1, "Pro (7-day free trial)"),
])
def test_plan_display_never_shows_provenance(plan, trial, want):
    got = tc.plan_display(plan, trial)
    assert got == want and ":" not in got


def test_daily_calls_come_from_tier_registry():
    from tier_registry import calls_per_day
    assert tc.daily_calls("pro") == calls_per_day("pro") == 2000
    assert tc.daily_calls("paid:mint", OCT1) == calls_per_day("pro")
    assert tc.daily_calls("paid:mint") is None       # unknown plan: omit, never guess
    assert tc.daily_calls("1,000 API credits") is None


def test_trial_end_for_subscription_only_while_trialing():
    assert tc.trial_end_for_subscription({"status": "trialing", "trial_end": OCT1}) == OCT1
    assert tc.trial_end_for_subscription({"status": "active", "trial_end": OCT1}) is None
    assert tc.trial_end_for_subscription(None) is None


def test_trial_end_for_checkout_reads_only_the_trial_checkout(monkeypatch):
    from routes._stripe_links import PRO_TRIAL_PAYMENT_LINK_ID
    tc._CACHE.clear()
    trial = {"id": "cs_t1", "mode": "subscription", "payment_link": PRO_TRIAL_PAYMENT_LINK_ID,
             "subscription": {"id": "sub_1", "status": "trialing", "trial_end": OCT1}}
    assert tc.trial_end_for_checkout(trial) == OCT1
    other = dict(trial, id="cs_p1", payment_link="plink_1UCTZKJ9ey2ATcQlByJCXN3W")
    assert tc.trial_end_for_checkout(other) is None
    assert tc.trial_end_for_checkout(None) is None


# ── (a) the receipt, rendered ───────────────────────────────────────────

class _Resp:
    status_code = 200

    def json(self):
        return {"id": "re_1"}


def _render_receipt(monkeypatch, **kw):
    fn = copy.deepcopy(_fn("_send_payment_receipt"))
    sent = []

    class _Req:
        @staticmethod
        def post(url, json=None, headers=None, timeout=None):
            sent.append(json)
            return _Resp()

    class _SyncThread:
        def __init__(self, target=None, daemon=None):
            self.t = target

        def start(self):
            self.t()

    import os
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    ns = {"os": os, "requests": _Req, "print": lambda *a, **k: None,
          "threading": type("T", (), {"Thread": _SyncThread}),
          "_pg_execute": lambda sql, params=None, fetch=False: (1, [(7,)])}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), ns)
    ns["_send_payment_receipt"]("buyer@example.com", 0, "usd",
                                "cs_live_a1TwUsspY9FwyK3UwzFkvv3asL4O4DniiWF04LoYvTDiQZXHqX7Z1wtZKR", **kw)
    assert len(sent) == 1
    return sent[0]


def test_trial_receipt_says_trial_not_payment(monkeypatch):
    msg = _render_receipt(monkeypatch, trial_end=OCT1)
    assert msg["subject"] == "Your DC Hub Pro trial has started"
    assert "payment received" not in msg["html"].lower()
    assert "7-day Pro trial has started" in msg["html"] and "October 1, 2026" in msg["html"]
    assert "Charged today" in msg["html"] and "0.00 USD" in msg["html"]


def test_paid_receipt_is_unchanged(monkeypatch):
    msg = _render_receipt(monkeypatch)
    assert msg["subject"] == "Your DC Hub receipt"
    assert "Thanks — payment received" in msg["html"] and "trial" not in msg["html"].lower()


def test_receipt_reference_is_the_full_session_id(monkeypatch):
    msg = _render_receipt(monkeypatch)
    assert "cs_live_a1TwUsspY9FwyK3UwzFkvv3asL4O4DniiWF04LoYvTDiQZXHqX7Z1wtZKR" in msg["html"]


# ── (b) the welcomes: wired, and the stale limit is gone ────────────────

def _kw_names(call):
    return {k.arg for k in call.keywords}


def _calls(fn, callee):
    return [n for n in ast.walk(fn) if isinstance(n, ast.Call)
            and getattr(n.func, "id", getattr(n.func, "attr", "")) == callee]


def test_key_welcome_uses_canon_limit_and_clean_plan():
    fn = _fn("send_welcome_email_sendgrid")
    src = ast.get_source_segment(MAIN_SRC, fn)
    assert "10,000 API calls/day" not in src
    assert "plan_name.replace('_', ' ').title()" not in src
    assert "trial_end" in {a.arg for a in fn.args.args + fn.args.kwonlyargs}
    for c in _calls(fn, "_welcome_email_resend_fallback"):
        assert "trial_end" in _kw_names(c)


def test_every_welcome_and_receipt_call_on_checkout_passes_trial_end():
    co = _fn("handle_checkout_completed")
    for c in _calls(co, "send_welcome_email_sendgrid"):
        assert "trial_end" in _kw_names(c)
    # the webhook dispatcher's receipt + upgrade welcome
    hook = [n for n in ast.walk(MAIN) if isinstance(n, ast.FunctionDef)
            and _calls(n, "_send_payment_receipt")]
    assert hook
    for n in hook:
        for c in _calls(n, "_send_payment_receipt") + _calls(n, "send_pro_welcome_email_sendgrid"):
            assert "trial_end" in _kw_names(c)


def test_subscription_welcome_passes_trial_end():
    fme = ast.parse((ROOT / "flask_mcp_endpoints.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(fme) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "send_welcome_email_sendgrid"]
    assert calls and all("trial_end" in _kw_names(c) for c in calls)


def test_upgrade_welcome_has_a_trial_variant():
    fn = _fn("send_pro_welcome_email_sendgrid")
    src = ast.get_source_segment(MAIN_SRC, fn)
    assert "trial_end" in {a.arg for a in fn.args.args}
    assert "Your DC Hub Pro trial has started" in src and "trial_block_html" in src


# ── (d) entitlements ────────────────────────────────────────────────────

class _ECur:
    def __init__(self, mk_row):
        self.mk_row, self.sql = mk_row, []

    def execute(self, sql, params=None):
        self.sql.append(sql)

    def fetchone(self):
        return None if "FROM api_keys" in self.sql[-1] else self.mk_row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _EConn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur

    def close(self):
        pass


def _entitlements(monkeypatch, mk_row, tier, ctx):
    from flask import Flask
    from routes import account_entitlements as ae
    import util.tier_gate as tg
    cur = _ECur(mk_row)
    monkeypatch.setattr(ae, "_conn", lambda: _EConn(cur))
    monkeypatch.setattr(tg, "resolve_tier", lambda *a, **k: (tier, ctx))
    app = Flask("t")
    app.register_blueprint(ae.account_entitlements_bp)
    r = app.test_client().get("/api/v1/account/entitlements",
                              headers={"X-API-Key": "dch_live_bf7xxxxxxxxxxxx"})
    return r.get_json(), cur


def test_entitlements_bound_free_key_reads_bound_and_free(monkeypatch):
    from util.tier_gate import Tier
    out, cur = _entitlements(monkeypatch, ("active", "someone@icloud.com", "2026-09-24", "free"),
                             Tier.ANONYMOUS, {"source": "api_key", "plan": "free"})
    assert "COALESCE(NULLIF(email, ''), metadata->>'email')" in cur.sql[-1]
    assert out["sources"]["mcp_dev_keys"]["email_bound"] is True
    assert out["sources"]["mcp_dev_keys"]["tier"] == "free"
    assert out["resolved"]["tier"] == "free" and out["resolved"]["gate_level"] == "anonymous"
    assert out["resolved"]["plan"] == "free"
    # a BOUND free key says what it has, not what binding would give it
    from tier_registry import calls_per_day
    assert out["resolved"]["email_bound"] is True
    assert "email-bound" in out["resolved"]["unlocks"]
    assert "once email-bound" not in out["resolved"]["unlocks"]
    assert f"{calls_per_day('identified'):,} calls/day" in out["resolved"]["unlocks"]


def test_entitlements_unbound_free_key_keeps_the_binding_offer(monkeypatch):
    from util.tier_gate import Tier
    out, _ = _entitlements(monkeypatch, ("active", None, "2026-09-24", "free"),
                           Tier.ANONYMOUS, {"source": "api_key", "plan": "free"})
    assert out["resolved"]["tier"] == "free" and out["resolved"]["email_bound"] is False
    assert "once email-bound" in out["resolved"]["unlocks"]


def test_entitlements_unknown_key_still_reads_anonymous(monkeypatch):
    from util.tier_gate import Tier
    out, _ = _entitlements(monkeypatch, None, Tier.ANONYMOUS,
                           {"source": "anonymous", "plan": None})
    assert out["resolved"]["tier"] == "anonymous" and out["resolved"]["plan"] is None


def test_entitlements_paid_key_is_unchanged(monkeypatch):
    from util.tier_gate import Tier
    out, _ = _entitlements(monkeypatch, ("active", "p@x.com", "2026-09-01", "paid"),
                           Tier.PRO, {"source": "api_key", "plan": "pro"})
    assert out["resolved"]["tier"] == "pro" and out["resolved"]["gate_level"] == "pro"


# ── r-repeat-receipt (2026-09-24, live gate run 3) ──────────────────────
# A repeat trial is ended now and charged on its own invoice; the receipt said
# "payment received, 0.00 USD" beside a $99 card charge.

def test_repeat_receipt_states_the_real_charge(monkeypatch):
    msg = _render_receipt(monkeypatch, repeat_charge={"amount_cents": 9900, "currency": "usd"})
    h = msg["html"]
    assert msg["subject"] == "Your DC Hub Pro subscription has started"
    assert "already used your free trial" in h
    assert "$99.00 charged today, renews monthly" in h
    assert "Charged today" in h and "99.00 USD" in h
    assert "0.00 USD" not in h and "payment received" not in h.lower()
    assert "https://dchub.cloud/dashboard" in h


def test_repeat_receipt_without_a_known_amount_claims_no_number(monkeypatch):
    from tier_registry import price_display
    msg = _render_receipt(monkeypatch, repeat_charge={"x": 1})
    assert "billed at %s, renews monthly" % price_display("pro") in msg["html"].replace("Billed", "billed")
    assert "charged today, renews" not in msg["html"]


def test_repeat_block_formats_other_amounts():
    assert "$1,188.00 charged today" in tc.repeat_block_html({"amount_cents": 118800})


def test_repeat_charge_for_without_a_sub_is_empty():
    assert tc.repeat_charge_for(None) == {} and tc.repeat_charge_for("") == {}


def test_webhook_passes_the_guard_result_to_the_receipt():
    hook = _fn("stripe_webhook")
    src = ast.get_source_segment(MAIN_SRC, hook)
    guard_at = src.index("_trial_guard = _ot")
    receipt_at = src.index("repeat_charge=_repeat_charge")
    assert guard_at < receipt_at, "the receipt must run after the guard"
    assert "== 'trial_ended_now'" in src and "repeat_charge_for" in src
