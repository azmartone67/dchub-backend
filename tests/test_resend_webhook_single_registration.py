"""/api/v1/webhooks/resend is registered exactly ONCE.

From 2026-07-17 to 2026-08-28 it was registered TWICE:

    resend_webhook.resend_webhook     main.py:13024  -> email_events
    email_engagement.resend_webhook   main.py:41403  -> email_engagement

Flask serves the FIRST registration, so every event would have landed in
email_events and the email_engagement blueprint was dead code — its webhook
never ran, and its public /api/v1/email/{engagement,recent} read a table that
therefore nothing wrote. Both surfaces served zeros in production while
reporting "Until configured, all counters stay 0", which pointed at the wrong
cause: configuring the webhook would NOT have moved them.

(The shadowing itself was not the reason no delivery events exist. Both tables
were empty — email_engagement had 0 rows across the two months it was live
BEFORE being shadowed — so nothing was ever POSTed to the URL at all. The
Resend dashboard endpoint has never been configured. The shadow is why fixing
that would still not have lit up /api/v1/email/*.)

email_engagement.py is deleted. This file fails if a second registration of the
path ever returns, whatever module it comes from.

Static AST — CI runs with no DATABASE_URL, and importing main to read url_map
would need one.
"""

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = "/api/v1/webhooks/resend"
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__",
             "tests", "site-packages", "build", "dist"}


def _route_paths(dec):
    """The literal path strings on a @<anything>.route('/x') decorator."""
    if not isinstance(dec, ast.Call):
        return []
    f = dec.func
    if not (isinstance(f, ast.Attribute) and f.attr in ("route", "add_url_rule")):
        return []
    return [a.value for a in dec.args
            if isinstance(a, ast.Constant) and isinstance(a.value, str)]


def _registrations():
    """[(relpath, funcname, lineno)] declaring PATH, across the whole repo."""
    hits = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT)
            try:
                with open(full, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in node.decorator_list:
                    if PATH in _route_paths(dec):
                        hits.append((rel, node.name, node.lineno))
    return hits


def test_the_scan_finds_the_live_handler():
    """Anchor: an empty scan satisfies every 'exactly one' assertion below."""
    hits = _registrations()
    assert hits, (
        f"scan found NO registration of {PATH} — the scanner is broken or the "
        "handler moved; this file is not passing, it is blind")


def test_registered_exactly_once():
    hits = _registrations()
    assert len(hits) == 1, (
        f"{PATH} is registered {len(hits)} times — Flask serves the FIRST and "
        "the rest are dead code that silently diverts nothing while looking "
        f"live: {hits}")


def test_the_one_registration_is_the_delivery_truth_handler():
    (rel, func, _), = _registrations()
    assert rel.replace(os.sep, "/") == "routes/resend_webhook.py", (
        f"{PATH} is served from {rel}, not routes/resend_webhook.py — the "
        "handler that writes email_events, which /api/v1/admin/email-events "
        "and welcome_email_log.resend_message_id are joined against")
    assert func == "resend_webhook"


def test_the_shadowed_blueprint_stays_deleted():
    assert not os.path.exists(os.path.join(ROOT, "routes", "email_engagement.py")), (
        "routes/email_engagement.py is back — it shadowed nothing and was "
        "shadowed BY resend_webhook; if it is genuinely needed, its route must "
        "not collide with the live one")
    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as fh:
        main_src = fh.read()
    assert "register_blueprint(email_engagement_bp)" not in main_src, (
        "email_engagement_bp is being registered in main.py again")
