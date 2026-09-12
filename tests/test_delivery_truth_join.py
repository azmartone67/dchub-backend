"""The delivery-truth join matched nothing, all time, and now it can.

THE DEFECT (measured live 2026-09-11, before the fix)

    SELECT count(*) FROM welcome_email_log w
      JOIN email_events e ON e.resend_message_id = w.resend_message_id
    -> 0, all time

It was never a provenance mismatch. Resend's POST /emails `id` and its
webhook's data.email_id are the same value, and both columns held 36-char
UUIDs. The two populations simply never overlapped IN TIME:

  · #4198 moved the paid welcome off the dead SendGrid import onto
    main._resend_email, which returned `200 <= status < 300` and threw the
    response body away. The send stopped recording an id at the exact moment
    it started succeeding.
  · the newest id-carrying row in ANY table in the database was 2026-08-28;
    the verified event stream began 2026-08-29 03:10Z. Neither side has ever
    had a row the other could match.

WHAT THESE PIN
  · _resend_email hands back the message id — the real shipped function,
    executed against a stubbed transport, not a substring in the file blob;
  · it stays truthy on a body with no id, and falsy on a failure, so all ~19
    boolean call sites are unaffected;
  · the welcome lane stamps THAT value into welcome_email_log — bound to the
    send's own result, so a literal None cannot satisfy the assertion;
  · delivery_verdict reads the WELCOME-scoped event count, not the
    all-senders count that made BLIND unreachable;
  · a zero that means "the webhook is dead" and a zero that means "no welcome
    mail arrived" give different, separately actionable verdicts;
  · the endpoint publishes the senders it can NEVER confirm.

House rule: never import main (tests/test_activation_emails.py:2).
"""
import ast
import json
import os
import re
import sys
import time
from unittest import mock

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(REPO, "main.py")
RECOVER = os.path.join(REPO, "routes", "onboarding_recover.py")
sys.path.insert(0, REPO)

from routes.onboarding_recover import (  # noqa: E402
    UNRECONCILED_SENDERS,
    delivery_verdict,
)

MID = "4ef9a417-02e9-4d39-ad75-9611e0fcc33c"   # a real Resend id shape


def _tree(path=MAIN):
    with open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _func(tree, name, path="main.py"):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(
        f"{name}() not found in {path} — the guard is aimed at a dead target")


class _Resp:
    """Exactly the surface the shipped code touches: .status and .read().

    Deliberately NO __enter__: _resend_email does not use a context manager,
    and a fixture more capable than the real object goes green on code that
    could never work in production."""

    def __init__(self, status, body):
        self.status = status
        self._body = body.encode() if isinstance(body, str) else body

    def read(self):
        return self._body


def _load(names):
    """Execute the REAL shipped functions, pulled out of main.py by AST.

    Importing main.py opens DB pools and registers ~200 blueprints, so the
    house rule forbids it; these still run the shipped bytes, not a copy."""
    tree = _tree()
    consts = {n.targets[0].id: n.value.value
              for n in tree.body
              if isinstance(n, ast.Assign) and len(n.targets) == 1
              and isinstance(n.targets[0], ast.Name)
              and isinstance(n.value, ast.Constant)
              and n.targets[0].id == "_RESEND_MAX_ATTEMPTS"}
    assert consts, "_RESEND_MAX_ATTEMPTS is gone — the retry loop moved"
    ns = {"time": time, "_resend_pace": lambda: None}
    ns.update(consts)
    mod = ast.Module(body=[_func(tree, n) for n in names], type_ignores=[])
    exec(compile(mod, "main.py<extracted>", "exec"), ns)   # noqa: S102
    return ns


@pytest.fixture()
def sender(monkeypatch):
    monkeypatch.setenv("DCHUB_RESEND_API_KEY", "re_test_key")
    return _load(["_resend_message_id", "_resend_email"])


# ── 1. the sender hands back the id ──────────────────────────────────────
def test_resend_email_returns_the_message_id(sender):
    """THE regression. `return 200 <= status < 300` discarded the only value
    that can ever tie a send to its delivery event."""
    with mock.patch("urllib.request.urlopen",
                    return_value=_Resp(200, json.dumps({"id": MID}))):
        got = sender["_resend_email"]("buyer@example.test", "s", "<p>h</p>")
    assert got == MID, (
        f"_resend_email returned {got!r} instead of the Resend message id. "
        "A send that cannot say WHAT it sent can never be confirmed delivered "
        "— that is the whole defect.")


def test_a_send_with_no_id_in_the_body_is_still_truthy(sender):
    """Callers test this for success. A 2xx whose body lost its id is a
    SUCCESSFUL send, and must never be recorded as a failure."""
    with mock.patch("urllib.request.urlopen", return_value=_Resp(200, "{}")):
        got = sender["_resend_email"]("buyer@example.test", "s", "<p>h</p>")
    assert got == "sent-no-id" and bool(got) is True, (
        "a 2xx with no id must stay truthy — the sentinel matches the "
        "convention _welcome_email_resend_fallback already ships")


def test_unparseable_body_does_not_turn_a_delivered_send_into_a_failure(sender):
    with mock.patch("urllib.request.urlopen", return_value=_Resp(200, "not json")):
        assert bool(sender["_resend_email"]("b@example.test", "s", "h")) is True


def test_failure_is_still_falsy(sender):
    """The boolean contract every existing call site depends on."""
    with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
        assert sender["_resend_email"]("b@example.test", "s", "h") is False
    with mock.patch("urllib.request.urlopen", return_value=_Resp(500, "{}")):
        assert sender["_resend_email"]("b@example.test", "s", "h") is False


def test_no_caller_compares_the_result_by_identity():
    """Widening bool -> str is safe only while every caller reads it as a
    truth value. `is True` / `== True` would now silently stop firing."""
    seen = 0
    for base, _dirs, files in os.walk(REPO):
        if any(p in base for p in (".git", "node_modules", "__pycache__")):
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            src = open(os.path.join(base, fn), encoding="utf-8",
                       errors="replace").read()
            if "_resend_email" not in src:
                continue
            seen += 1
            bad = re.findall(r"_resend_email\([^\n]*\)\s*(?:is|==)\s*True", src)
            assert not bad, f"{fn}: identity comparison on _resend_email: {bad}"
    assert seen >= 5, (
        f"only {seen} files mention _resend_email — this scan can pass by "
        "finding nothing, so it needs a floor")


def test_both_senders_share_one_message_id_extractor():
    """Two copies of "what the Resend id is" is how this breaks a second time.
    A sender that reads a different field than the reconciler joins on raises
    nothing and logs nothing — it just goes quiet, which is the failure mode
    this whole endpoint exists for."""
    tree = _tree()
    for name in ("_resend_email", "_welcome_email_resend_fallback"):
        fn = _func(tree, name)
        called = {n.func.id for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "_resend_message_id" in called, (
            f"{name} extracts the message id itself instead of sharing "
            "_resend_message_id — the two copies are free to drift apart")


# ── 2. the welcome lane stamps what it got ───────────────────────────────
def _log_call_in(fn):
    """The _log_welcome_email call on the ordinary (non-except) send path."""
    in_except = {id(c) for h in ast.walk(fn)
                 if isinstance(h, ast.ExceptHandler) for c in ast.walk(h)}
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_log_welcome_email" and id(n) not in in_except
             and any(k.arg == "status" and isinstance(k.value, ast.IfExp)
                     for k in n.keywords)]
    assert len(calls) == 1, (
        f"expected exactly one outcome-logging call on the send path, found "
        f"{len(calls)} — the guard would be aimed at the wrong one")
    return calls[0]


def test_the_welcome_lane_logs_the_id_the_send_returned():
    """★ Bind to the VALUE, not the keyword name. `resend_message_id=None`
    would satisfy a presence check while re-breaking the join exactly as
    before, so the assertion demands the send's own result."""
    fn = _func(_tree(), "send_welcome_email_sendgrid")

    sent = [n.targets[0].id for n in ast.walk(fn)
            if isinstance(n, ast.Assign) and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name)
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name)
            and n.value.func.id == "_resend_email"]
    assert sent, (
        "the welcome lane never binds _resend_email's result to a name — it "
        "is discarding the message id again")

    call = _log_call_in(fn)
    kw = [k for k in call.keywords if k.arg == "resend_message_id"]
    assert kw, (
        "the welcome lane logs its outcome without resend_message_id — every "
        "send it records is unmatchable by construction, which is the defect")

    names = {n.id for n in ast.walk(kw[0].value) if isinstance(n, ast.Name)}
    assert names & set(sent), (
        f"resend_message_id is not derived from the send result {sent} — it "
        f"reads {ast.dump(kw[0].value)[:90]}. A constant here is the bug.")


# ── 3. the verdict reads the scoped count ────────────────────────────────
def test_a_dead_webhook_and_a_silent_welcome_lane_are_different_verdicts():
    dead, healthy_dead = delivery_verdict(5, 0, 0, 30, events_all=0)
    live, healthy_live = delivery_verdict(5, 0, 0, 30, events_all=891)
    assert healthy_dead is False and healthy_live is False
    assert "BLIND" in dead and "BLIND" in live
    assert dead != live, (
        "a webhook that never fired and a welcome lane that sent nothing get "
        "the same verdict — one of the two operators is being misdirected")
    assert "RESEND_WEBHOOK_SECRET" in dead, (
        "the owner-side fix must survive on the branch that needs it")
    assert "RESEND_WEBHOOK_SECRET" not in live, (
        "telling the owner to configure a webhook that is demonstrably "
        "delivering 891 events sends them to fix the wrong thing")
    assert "891" in live, "the live branch must show what IS arriving"


def test_events_all_defaults_to_events_so_old_callers_are_unchanged():
    assert delivery_verdict(5, 0, 0, 30) == delivery_verdict(5, 0, 0, 30, events_all=0)


def test_unproven_still_reads_as_unproven():
    """The one thing this change must not do is make the number look better."""
    verdict, healthy = delivery_verdict(8, 23, 0, 30, events_all=891)
    assert healthy is False and "PARTIAL" in verdict and "0 of 8" in verdict


def test_the_verdict_is_fed_the_welcome_scoped_count():
    """★ The all-senders count is ~891 a window, so passing it here made BLIND
    unreachable: welcome delivery could stop dead and the weekly digest would
    hold the verdict open. Anchor to the ARGUMENT, not to the query text."""
    src = open(RECOVER, encoding="utf-8").read()
    fn = _func(ast.parse(src), "delivery_truth", "onboarding_recover.py")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "delivery_verdict"]
    assert len(calls) == 1, "expected one delivery_verdict call"
    call = calls[0]
    scoped = call.args[1].id
    broad = [k.value.id for k in call.keywords if k.arg == "events_all"]
    assert broad, "delivery_verdict is not told the all-senders count"
    assert scoped != broad[0], (
        f"delivery_verdict's events argument and events_all are the same "
        f"variable ({scoped}) — the verdict is reading every sender's mail "
        "as evidence about welcome delivery")

    # ...and the response labels each number with the one it actually is.
    out = [n for n in ast.walk(fn) if isinstance(n, ast.Dict)]
    labelled = {k.value: v.id for d in out for k, v in zip(d.keys, d.values)
                if isinstance(k, ast.Constant) and isinstance(v, ast.Name)}
    assert labelled.get("delivery_events_in_window") == broad[0]
    assert labelled.get("welcome_delivery_events_in_window") == scoped


def test_every_query_in_the_module_doubles_its_percent():
    """psycopg2 scans the WHOLE query for format specs, so a bare % in an
    ILIKE pattern raises 'tuple index out of range' at runtime. The existing
    guard walks only strings naming welcome_email_log — the new email_events
    query is not one of them, and that is exactly how this reaches prod."""
    src = open(RECOVER, encoding="utf-8").read()
    qs = [n.value for n in ast.walk(ast.parse(src))
          if isinstance(n, ast.Constant) and isinstance(n.value, str)
          and re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", n.value)]
    assert len(qs) >= 4, f"only {len(qs)} queries found — the scan needs a floor"
    for q in qs:
        assert not re.search(r"(?<!%)%(?![%s(])", q), f"bare percent in: {q[:70]}"


# ── 4. the surface names what it cannot see ──────────────────────────────
def test_the_endpoint_publishes_what_it_can_never_confirm():
    """22 of 23 delivered welcome-subject events in the window were free-tier
    welcomes, which write no log row. A surface that omits that silently
    invites 'welcome mail is not arriving' from a number that never covered
    it."""
    assert len(UNRECONCILED_SENDERS) >= 3
    blob = " ".join(UNRECONCILED_SENDERS).lower()
    for lane in ("free", "pro", "keys_recover"):
        assert lane in blob, f"{lane} send lane is not declared out of scope"

    src = open(RECOVER, encoding="utf-8").read()
    fn = _func(ast.parse(src), "delivery_truth", "onboarding_recover.py")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "UNRECONCILED_SENDERS" in names, (
        "the scope limit is documented in the module but never reaches the "
        "response — a reader of the endpoint still cannot see it")
