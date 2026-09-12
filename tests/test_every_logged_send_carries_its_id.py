"""Every logged SEND must carry the id the reconciliation joins on.

#4433 fixed the paid welcome lane: main._resend_email returned a bare bool, so
the send recorded no Resend message id and
/api/v1/admin/welcome-log/delivery-truth could never confirm it. That fix was
scoped to the lane the investigation started from — and two more lanes were
writing 'sent' rows with no id for exactly the same reason:

  · main.py, the SENDGRID_API_KEY-unset branch — called
    _welcome_email_resend_fallback (which RETURNS the id) inside an `if`,
    testing it for truth and discarding it;
  · routes/onboarding_recover.py, the admin resend endpoint — the operator
    resending a welcome BY HAND could never prove it arrived, in the very
    module that reports the shortfall.

So this is not three one-off fixes; it is one class, and this guard is the
class. A repo-wide rule costs the same as a third patch and ends the sequence.

THE RULE. A _log_welcome_email call whose status is, or can be, a 'sent'-prefixed
literal MUST pass resend_message_id. Such a row counts as a send on the
delivery-truth surface, and without an id it is unmatchable by construction —
it lands in sends_without_a_message_id permanently. Non-send outcomes
(skipped_duplicate, failed_*, exception:*, upgrade_provisioned) are exempt:
there is no id to record because nothing was sent.
"""
from __future__ import annotations

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every module that may log a welcome outcome. main.py plus the whole routes
# package — not a hand-listed set, which is how the third lane stayed invisible.
#
# ★ This file is deliberately ABSENT from tests/scan_floors.json, and that is
# not an oversight: os.listdir is not one of the primitives tests/_scan_floors.py
# wraps, so the shared floor mechanism never observes this scan and cannot
# protect it. test_the_scan_actually_finds_the_call_sites below is the floor
# instead, and it is a better one — it counts the _log_welcome_email CALL SITES
# found, not the files walked, so a rename that leaves the file count intact
# still turns it red. Do not "fix" the absence by pinning a file count.
def _sources():
    yield os.path.join(ROOT, "main.py")
    rdir = os.path.join(ROOT, "routes")
    for fn in sorted(os.listdir(rdir)):
        if fn.endswith(".py"):
            yield os.path.join(rdir, fn)


def _status_literals(call):
    """Every string constant the `status` argument can evaluate to.

    Handles the positional third argument, the keyword form, and the
    `'sent' if ok else 'failed'` shape both real lanes actually use."""
    node = None
    for kw in call.keywords:
        if kw.arg == "status":
            node = kw.value
    if node is None and len(call.args) >= 3:
        node = call.args[2]
    if node is None:
        return []
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
    return out


def _calls():
    """(path, lineno, call) for every _log_welcome_email invocation."""
    found = []
    for path in _sources():
        with open(path, encoding="utf-8") as fh:
            try:
                tree = ast.parse(fh.read())
            except SyntaxError:  # pragma: no cover
                continue
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "_log_welcome_email"):
                found.append((os.path.relpath(path, ROOT), n.lineno, n))
    return found


def test_the_scan_actually_finds_the_call_sites():
    """A rule that matches nothing passes forever. Pin a floor so a rename or
    a moved module turns this red instead of quietly green."""
    calls = _calls()
    assert len(calls) >= 8, (
        f"only {len(calls)} _log_welcome_email call sites found — the scan has "
        "lost the code it is meant to police")
    senders = [c for c in calls
               if any(s.startswith("sent") for s in _status_literals(c[2]))]
    assert len(senders) >= 5, (
        f"only {len(senders)} send-logging sites found; the rule below would "
        "be vacuous")


def test_every_send_logged_carries_a_resend_message_id():
    """THE RULE. A 'sent' row with no id can never be confirmed delivered."""
    missing = []
    for rel, lineno, call in _calls():
        statuses = _status_literals(call)
        if not any(s.startswith("sent") for s in statuses):
            continue                      # not a send — nothing to record
        if not any(kw.arg == "resend_message_id" for kw in call.keywords):
            missing.append(f"{rel}:{lineno} status={statuses}")

    assert not missing, (
        "these lanes log a SEND with no resend_message_id, so every row they "
        "write is unmatchable by construction and lands in "
        "sends_without_a_message_id forever:\n  - " + "\n  - ".join(missing))


def test_the_id_is_never_a_bare_constant():
    """★ Bind to the VALUE. `resend_message_id=None` satisfies a presence check
    while re-breaking the join exactly as before."""
    constant = []
    for rel, lineno, call in _calls():
        if not any(s.startswith("sent") for s in _status_literals(call)):
            continue
        for kw in call.keywords:
            if kw.arg != "resend_message_id":
                continue
            names = {n.id for n in ast.walk(kw.value) if isinstance(n, ast.Name)}
            if not names:
                constant.append(f"{rel}:{lineno} -> {ast.dump(kw.value)[:70]}")
    assert not constant, (
        "resend_message_id is a constant here, not the value the send "
        "returned:\n  - " + "\n  - ".join(constant))
