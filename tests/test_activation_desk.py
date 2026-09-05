#!/usr/bin/env python3
"""tests/test_activation_desk.py — the desk that consumes the escalation queue.

NO NETWORK, NO DB.

CONTEXT. brain_escalation_queue (#3310) is the catcher and it WORKS — measured
2026-09-05 it held 10 rows, all status='open', calls_at_open=0, none wrongly
auto-activated, daily sync clean. What was missing is a consumer: nothing read
brain_escalations except its own module and tests, and ten named payers had sat
open 7.3 days with contacted_at NULL on every one.

THE THREE PROPERTIES THIS FILE PROTECTS:

  1. ★ IT CANNOT SEND. The classifier's finding is that the automated nudge
     failed 9 of 9 and asks for "human touch, not another email". A module that
     grows an outbound path turns this desk into the tenth automated email.

  2. The SEGMENT comes from last_login vs created_at and nothing else. Deriving
     it from plan or payment would sort people by what they spent rather than by
     what they did, and the three groups need opposite messages: someone who
     logged in last week and still made no call is failing at onboarding;
     someone who never logged in may never have got the key into their code.

  3. An unmatched `users` row is UNMEASURED, never a segment. A missing join
     rendered as "no name, never logged in" would put a real person in the wrong
     group — and that join has already bitten once: saved_lp_sites.user_id is a
     key hash (`k_<hex>`), so matching it against an email returns zero whether
     or not data exists.
"""
import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "routes", "activation_desk.py")


def _src(strip_comments=True):
    s = open(SRC, encoding="utf-8").read()
    if strip_comments:
        s = "\n".join(l for l in s.splitlines()
                      if not l.lstrip().startswith("#"))
    return s


def test_the_desk_has_no_outbound_path():
    """★ The load-bearing safety property. Comment-stripped: this module's
    docstring explains at length why it must not send, and a substring check
    over raw source would be satisfied by that explanation."""
    body = _src()
    banned = ["smtplib", "sendgrid", "resend", "postmark", "mailgun",
              "send_email", "send_mail", "_send(", "requests.post",
              "urlopen", "httpx", "email_drip", "boto3"]
    hits = [b for b in banned if b in body]
    assert not hits, (
        "activation_desk gained an outbound path (%s). The automated nudge "
        "already failed 9/9; this desk exists to hand a human a draft, and a "
        "send here makes it the tenth automated email." % ", ".join(hits))


def test_no_write_statements_at_all():
    """Read-only. A desk that marks people contacted, or resolves their row,
    would let the queue be cleared without anyone actually reaching out —
    exactly the auto_activated defect the catcher was fixed for."""
    body = _src().upper()
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert verb not in body, (
            "activation_desk contains a %s — it must be read-only; clearing a "
            "row without contact is how the queue lied before" % verb.strip())


def test_segment_is_derived_from_login_only():
    import sys
    sys.path.insert(0, ROOT)
    from routes.activation_desk import _segment
    import datetime as dt
    created = dt.datetime(2026, 2, 6)
    later = dt.datetime(2026, 8, 22)
    assert _segment(later, created, None) == "logged_in_never_called"
    assert _segment(None, created, None) == "never_logged_in"
    assert _segment(created, created, None) == "paid_and_vanished"
    # the signature must not even accept plan/payment
    src = _src()
    sig = src[src.index("def _segment("):src.index(")", src.index("def _segment("))]
    for banned in ("plan", "payment", "priority", "amount"):
        assert banned not in sig, (
            "_segment takes %r — segment must depend on behaviour, not on what "
            "someone spent" % banned)


def test_an_unmatched_user_is_unmeasured_not_a_segment():
    body = _src()
    assert '"unmatched"' in body, (
        "no unmatched state — a row with no users match would be silently "
        "classified, and the wrong group gets the wrong note")
    assert "matched_user" in body, (
        "matched_user is not published, so a reader cannot tell a real "
        "'never logged in' from a failed join")
    i = body.index("row[\"segment\"] =")
    assert "matched_user" in body[i:i + 220], (
        "segment is assigned without checking matched_user first")


def test_drafts_invent_no_prior_interest():
    """saved_searches and saved_markets are empty for every one of these
    accounts. A 'you were looking at X' hook would be fabrication."""
    import sys
    sys.path.insert(0, ROOT)
    from routes.activation_desk import _draft
    for seg in ("logged_in_never_called", "never_logged_in", "paid_and_vanished"):
        d = _draft({"name": "Ada", "plan": "pro", "segment": seg,
                    "nudge_days": 47.0})
        assert d.startswith("Hi Ada"), d
        low = d.lower()
        for invented in ("you were looking", "your search", "you saved",
                         "based on your interest", "we noticed you viewed"):
            assert invented not in low, (
                "draft for %s claims prior activity we have none of: %r"
                % (seg, invented))
    # and the three segments must actually differ, or the split is decorative
    drafts = {s: _draft({"name": "Ada", "plan": "pro", "segment": s,
                         "nudge_days": 47.0})
              for s in ("logged_in_never_called", "never_logged_in",
                        "paid_and_vanished")}
    assert len(set(drafts.values())) == 3, (
        "two segments produce the same note — sending everyone the same thing "
        "is what the failed nudge did")


def test_a_failed_query_says_unread_not_empty():
    body = _src()
    i = body.index("query failed")
    seg = body[i - 300:i + 200]
    assert "UNREAD" in seg and "not empty" in seg, (
        "a failed read must not render as an empty queue — that is the "
        "absence-vs-zero defect this codebase keeps paying for")
    assert '"rows": None' in seg, "failed read must publish rows=None, not []"


def test_module_parses_and_is_admin_gated():
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    fns = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "_admin_ok" in fns and "_disabled" in fns
    body = _src()
    for route in ("/api/v1/admin/activation-desk", "/admin/activation-desk"):
        assert route in body
    # ★ Count CALL SITES, not the identifier. `_admin_ok()` appears three
    # times — once as its own `def` — so a `>= 2` check on the bare name still
    # passed after a route's gate was deleted. Found by mutation, not by
    # reading. Assert the guarded form, once per route.
    gates = len(re.findall(r"if not _admin_ok\(\):", body))
    assert gates >= 2, (
        "only %d route(s) check the admin gate — this page renders customer "
        "names and email addresses" % gates)
