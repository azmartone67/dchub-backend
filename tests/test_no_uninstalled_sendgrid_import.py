"""No module may import the sendgrid SDK — it is not installed.

MEASURED 2026-09-07. `sendgrid` is absent from requirements, yet main.py
imported it at four sites inside the three welcome senders. SENDGRID_API_KEY
*is* set in Railway, so the `if not sg_key: return` guard passed and every
welcome reached `from sendgrid import SendGridAPIClient` and raised.

It was invisible because each sender catches the exception and falls back to
Resend, and the fallback is a real, maintained email. So:

  welcome_email_log: 16 sent_via_resend, 1 exception:No module named 'sendgrid'

Customers were fine. Observability was not — every send printed "Welcome email
failed", and a GENUINE failure was indistinguishable from the routine one,
which is why the single real failure sat unnoticed from 2026-06-10.

★ WHY A TEST AND NOT JUST THE DELETION. The import is easy to reintroduce: the
  helper names (Mail, Email, To, HtmlContent) still read like the natural way
  to build an email, and three sibling modules still mention SendGrid in their
  prose. This fails the moment one comes back.

★ IF SENDGRID IS EVER GENUINELY ADOPTED, add it to requirements — the test
  reads the requirement file rather than hardcoding "never", so installing it
  legitimately makes this pass instead of forcing someone to delete a guard.
"""
import ast
import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG = "sendgrid"


def _requirements_text():
    out = []
    for fn in os.listdir(_ROOT):
        if fn.startswith("requirements") and fn.endswith(".txt"):
            with open(os.path.join(_ROOT, fn), encoding="utf-8") as fh:
                out.append(fh.read())
    return "\n".join(out)


def _is_declared():
    return bool(re.search(rf"^\s*{_PKG}\b", _requirements_text(), re.M | re.I))


def _imports_of(path):
    with open(path, encoding="utf-8") as fh:
        try:
            tree = ast.parse(fh.read())
        except SyntaxError:
            return []
    found = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            found += [(a.name, n.lineno) for a in n.names
                      if a.name.split(".")[0] == _PKG]
        elif isinstance(n, ast.ImportFrom) and n.module:
            if n.module.split(".")[0] == _PKG:
                found.append((n.module, n.lineno))
    return found


def test_sendgrid_is_not_in_requirements():
    """The premise. If this fails the package was adopted — then the guard
    below is free to pass, and this test should be updated deliberately."""
    assert not _is_declared(), (
        "sendgrid appears in requirements now — if that is intentional, the "
        "import guard below no longer applies and should be revisited")


def test_main_does_not_import_the_uninstalled_sdk():
    """★ The four sites that raised on every welcome."""
    if _is_declared():
        pytest.skip("sendgrid is installed; importing it is legitimate")
    hits = _imports_of(os.path.join(_ROOT, "main.py"))
    assert not hits, (
        f"main.py imports {_PKG} at lines {[l for _, l in hits]} but the "
        f"package is not installed — every call raises at runtime")


@pytest.mark.parametrize("mod", [
    "welcome_emails.py", "alert_processor.py", "usage_limit_emails.py",
    "email_fallback.py",
])
def test_the_sibling_mail_modules_stay_clean(mod):
    """These already migrated to the resilient Resend-first sender. They talk
    about SendGrid in comments; none may import it."""
    path = os.path.join(_ROOT, mod)
    if not os.path.exists(path) or _is_declared():
        pytest.skip(f"{mod} absent or sendgrid installed")
    hits = _imports_of(path)
    assert not hits, f"{mod} imports {_PKG} at {[l for _, l in hits]}"
