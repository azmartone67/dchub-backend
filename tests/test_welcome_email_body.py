"""Guard: assert on the BYTES WE ACTUALLY SEND, not on a call signature.

★ THIS FILE EXISTS BECAUSE THE PREVIOUS GUARD WATCHED THE DEAD PATH.

tests/test_onboarding_entry_path.py (PR #2949) asserts that every
send_welcome_email_sendgrid(...) call passes reset_url. That guard is green and
was worth landing — and it protected nothing a customer can see, because
send_welcome_email_sendgrid's rich HTML never executes in production:

  * `sendgrid` is absent from requirements.txt.
  * main.py's own comment, dated 2026-07-03, above the Resend body:
      "SendGrid is dead in prod (no module), so THIS fallback is the email
       that actually sends."
  * When SENDGRID_API_KEY is unset, send_welcome_email_sendgrid calls
    _welcome_email_resend_fallback(...) and RETURNS before ever building the
    SendGrid HTML that carries the reset link.
  * rob@hedmarkholdings.com's welcome_email_log row is `sent_via_resend`, and
    the subject this function builds — "Welcome to DC Hub — you're all set",
    the 'there' variant, because his users.name is '' — is verbatim the subject
    he hit reply on to ask how to reset his password.

So the fix in #2949 changed zero bytes of what a buyer receives. A guard aimed
at the wrong function is worth nothing, and "it passes reset_url" is not the
same claim as "the customer can get in."

These tests therefore EXECUTE the real _welcome_email_resend_fallback (pulled
out of main.py with ast — the house pattern, since tests never import main) and
assert on the HTML that would go over the wire to Resend.
"""
import ast
import json
import os
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"
FN = "_welcome_email_resend_fallback"


def _load_sender():
    """Pull FN out of main.py and exec it against stubs. Never imports main."""
    src = MAIN.read_text(encoding="utf-8")
    tree = ast.parse(src, filename="main.py")
    node = next((n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == FN), None)
    assert node is not None, (
        f"{FN} not found in main.py — this guard is watching a function that no "
        "longer exists, which is exactly how a check rots into a green tick"
    )
    sent = {}

    class _Resp:
        status = 200
        def read(self):
            return json.dumps({"id": "msg_test"}).encode()

    def _fake_urlopen(req, timeout=None):
        sent["url"] = req.full_url
        sent["payload"] = json.loads(req.data.decode())
        return _Resp()

    import urllib.request as _real_u
    ns = {
        "_pg_execute": lambda *a, **k: (0, []),          # no users row -> 'there'
        "_welcome_mcp_connector_html": lambda *a, **k: "<div>connector</div>",
        "print": lambda *a, **k: None,
        "__builtins__": __builtins__,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), ns)
    return ns[FN], sent, _fake_urlopen, _real_u


def _send(reset_url, monkeypatch):
    fn, sent, fake, real_u = _load_sender()
    monkeypatch.setenv("DCHUB_RESEND_API_KEY", "re_test_key_not_real")
    monkeypatch.setattr(real_u, "urlopen", fake)
    out = fn("buyer@example.com", "dchub_testkey123", "developer",
             reset_url=reset_url)
    assert out, "the stubbed send did not report success — the harness is wrong"
    return sent["payload"]


# Any of these in an href is a route into the account. A body with none of them
# is the bug that reached Rob.
_ENTRY = re.compile(r"href=['\"][^'\"]*(reset-password|forgot-password)", re.I)


def test_the_email_that_actually_sends_carries_an_entry_path(monkeypatch):
    """★ THE REGRESSION. Assert on the delivered body, not the call signature."""
    payload = _send("https://dchub.cloud/reset-password.html?token=abc123",
                    monkeypatch)
    html = payload["html"]
    assert _ENTRY.search(html), (
        "the welcome email that ACTUALLY SENDS (Resend) contains no link into "
        "the account. This is the exact body rob@hedmarkholdings.com received "
        "before replying to ask how to reset his password."
    )
    assert "token=abc123" in html, (
        "the minted reset token never reached the delivered HTML — the link in "
        "the customer's inbox points somewhere other than their own token"
    )


def test_no_token_still_leaves_a_route_in(monkeypatch):
    """mint_reset_url returns None on a DB blip or a missing users row. That
    must degrade to the public self-serve page, never to a dead end."""
    html = _send(None, monkeypatch)["html"]
    assert _ENTRY.search(html), (
        "with no token minted the body offers no way into the account at all"
    )
    assert "forgot-password" in html


def test_the_api_key_is_still_delivered(monkeypatch):
    """The entry block must not displace the thing they actually bought."""
    html = _send("https://dchub.cloud/reset-password.html?token=t", monkeypatch)["html"]
    assert "dchub_testkey123" in html
    assert "connector" in html, "the Connect-to-Claude block went missing"


def test_it_is_really_going_to_resend(monkeypatch):
    """Pin the harness: if this ever stops exercising the real transport the
    assertions above become theatre."""
    fn, sent, fake, real_u = _load_sender()
    monkeypatch.setenv("DCHUB_RESEND_API_KEY", "re_test_key_not_real")
    monkeypatch.setattr(real_u, "urlopen", fake)
    fn("buyer@example.com", "k", "developer", reset_url="https://x/reset-password?token=t")
    assert sent["url"] == "https://api.resend.com/emails"
    assert sent["payload"]["to"] == ["buyer@example.com"]


def test_every_resend_call_site_passes_reset_url():
    """Static companion: the body can only carry the link if the callers hand
    it over. Covers the LIVE sender — the previous guard covered the dead one."""
    tree = ast.parse(MAIN.read_text(encoding="utf-8"), filename="main.py")
    assert tree.body, "main.py parsed empty — guard is looking at nothing"
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", None)) == FN]
    assert len(calls) >= 3, (
        f"expected >=3 {FN} call sites, found {len(calls)} — the walk is not "
        "finding them and every assertion below would pass vacuously"
    )
    bad = [c.lineno for c in calls
           if "reset_url" not in {kw.arg for kw in c.keywords} and len(c.args) < 4]
    assert not bad, (
        f"{FN} called without reset_url at main.py:{bad} — that call sends a "
        "paying customer a welcome email with no way into their account"
    )


@pytest.mark.parametrize("dead", ["sendgrid"])
def test_sendgrid_is_still_dead(dead):
    """If SendGrid is ever reinstalled the delivery path changes and the
    assumption this whole file rests on is void — fail loudly and make someone
    re-check which body a customer actually receives."""
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    installed = any(line.strip().startswith(dead)
                    for line in req.splitlines())
    assert not installed, (
        "sendgrid is back in requirements.txt. The live welcome path may now be "
        "send_welcome_email_sendgrid's HTML rather than the Resend fallback — "
        "re-verify which body reaches a customer before trusting these tests."
    )
