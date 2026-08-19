"""Guard: a paying customer's welcome email must carry a way INTO the account.

Why this file exists
────────────────────
2026-08-19, rob@hedmarkholdings.com: signed up free on 08-17, bought Developer
on 08-19 at 14:26 UTC, emailed support at 15:17 UTC asking how to reset his
password. His welcome email had no link to do it, because the branch that
minted his key — main.py's "existing user, no prior key" path — called
``send_welcome_email_sendgrid(email, key, plan)`` with neither ``temp_password``
nor ``reset_url``. So did the MCP-key path in flask_mcp_endpoints. Only the
COLD-buyer branch passed a link. A customer who tried the product before buying
got strictly worse onboarding than one who didn't.

★ Why nothing caught it: the Onboarding Master Shell (#43) lane 2 asks "did
each payer get EXACTLY ONE welcome email" — a COUNT. No check anywhere asserted
anything about what is IN that email, and zero tests referenced
``send_welcome_email_sendgrid`` at all. A welcome email with no way in passed
every gate we had. These tests are that missing gate.

The AST check is deliberately static: main.py cannot be imported in a unit test
(Flask app + DB at module scope), and the defect is a missing KEYWORD ARGUMENT,
which is visible in the parse tree without executing anything.
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every file that emails a paying customer their key.
PROVISIONING_FILES = ("main.py", "flask_mcp_endpoints.py")

SENDER = "send_welcome_email_sendgrid"

# The ONE call site legitimately exempt: the $10 one-time credit-pack receipt.
# It is not a subscription welcome, its buyers frequently have no users row at
# all (agent-driven purchases), and mint_reset_url refuses to mint for an
# address with no account anyway. Identified by its plan_name literal rather
# than a line number so the exemption cannot silently drift onto a new call.
EXEMPT_PLAN_NAMES = {"1,000 API credits"}


def _sender_calls():
    """Every send_welcome_email_sendgrid(...) call across the provisioning
    files, as (file, lineno, ast.Call)."""
    found = []
    for fname in PROVISIONING_FILES:
        path = os.path.join(ROOT, fname)
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=fname)
        # Guard the guard: an empty/unparseable file would make every
        # assertion below vacuously true.
        assert tree.body, f"{fname} parsed to an EMPTY tree — the guard is not looking at anything"
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name == SENDER:
                found.append((fname, node.lineno, node))
    return found


def _plan_name_literal(call):
    for kw in call.keywords:
        if kw.arg == "plan_name" and isinstance(kw.value, ast.Constant):
            return kw.value.value
    # positional plan_name is the 3rd arg
    if len(call.args) >= 3 and isinstance(call.args[2], ast.Constant):
        return call.args[2].value
    return None


def test_guard_is_not_vacuous():
    """If the walk finds nothing, every other test in this file passes for the
    wrong reason. Pin a floor."""
    calls = _sender_calls()
    assert len(calls) >= 3, (
        f"expected >=3 {SENDER} call sites across {PROVISIONING_FILES}, "
        f"found {len(calls)} — the AST walk is not finding the real call sites"
    )


def test_every_paid_welcome_carries_an_entry_path():
    """★ THE REGRESSION. Every subscription welcome must pass reset_url."""
    offenders = []
    for fname, lineno, call in _sender_calls():
        if _plan_name_literal(call) in EXEMPT_PLAN_NAMES:
            continue
        kwargs = {kw.arg for kw in call.keywords}
        if "reset_url" not in kwargs:
            offenders.append(f"{fname}:{lineno}")
    assert not offenders, (
        "welcome email sent to a paying customer with NO way into the account "
        f"(missing reset_url=) at: {', '.join(offenders)}. "
        "Pass reset_url=mint_reset_url(email) — see routes/_password_reset_link.py. "
        "This is the defect that made rob@hedmarkholdings.com email support 51 "
        "minutes after paying."
    )


def test_reset_link_helper_is_the_single_source():
    """Nobody may hand-roll the reset URL again — that duplication is what let
    the two provisioning paths drift apart in the first place."""
    inline = []
    for fname in PROVISIONING_FILES:
        with open(os.path.join(ROOT, fname), "r", encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if "reset-password.html?token=" in line:
                    inline.append(f"{fname}:{i}")
    assert not inline, (
        f"reset URL built inline at {', '.join(inline)} — import mint_reset_url "
        "from routes._password_reset_link instead"
    )


# ── mint_reset_url behaviour ──────────────────────────────────────────────

class _FakeRunner:
    """Stands in for main._pg_execute: (sql, params[, fetch]) -> (rowcount, rows)."""

    def __init__(self, account_exists=True, insert_rowcount=1, raise_on_insert=False):
        self.account_exists = account_exists
        self.insert_rowcount = insert_rowcount
        self.raise_on_insert = raise_on_insert
        self.inserted = []

    def __call__(self, sql, params=(), fetch=False):
        if sql.strip().upper().startswith("SELECT"):
            return (1, [(1,)] if self.account_exists else [])
        if "INSERT INTO password_reset_tokens" in sql:
            if self.raise_on_insert:
                raise RuntimeError("simulated DB failure")
            self.inserted.append(params)
            return (self.insert_rowcount, [])
        return (1, [])


def _mint(**kw):
    from routes._password_reset_link import mint_reset_url
    runner = _FakeRunner(**kw)
    return mint_reset_url("buyer@example.com", execute=runner), runner


def test_happy_path_returns_url_carrying_the_persisted_token():
    url, runner = _mint()
    assert url and url.startswith("https://dchub.cloud/reset-password.html?token=")
    token = url.split("token=", 1)[1]
    assert runner.inserted, "no token was written"
    assert runner.inserted[0][1] == token, (
        "the URL's token is not the token that was persisted — the link is dead"
    )


def test_zero_rowcount_yields_none_not_a_dead_link():
    """★ main._pg_execute swallows exceptions and returns (0, []), so rowcount
    is the ONLY evidence the INSERT landed. A URL here would 'Invalid or
    expired' in the customer's face."""
    url, _ = _mint(insert_rowcount=0)
    assert url is None


def test_db_exception_yields_none():
    url, _ = _mint(raise_on_insert=True)
    assert url is None


def test_no_account_yields_none():
    """reset_password UPDATEs 0 rows and still returns success:true — never
    hand out a link that silently does nothing."""
    url, _ = _mint(account_exists=False)
    assert url is None


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_blank_email_yields_none(bad):
    from routes._password_reset_link import mint_reset_url
    assert mint_reset_url(bad, execute=_FakeRunner()) is None
