"""The browser attestation cookie must never sit in a JWT-decode chain.

THE DEFECT THIS PINS
────────────────────
`require_plan()` resolved a caller's plan from the first non-empty cookie in an
`or` chain:

    session_token = (request.cookies.get('session_token') or
                     request.cookies.get('dchub_session') or      # <-- attestation
                     request.cookies.get('dchub_token') or        # <-- the real JWT
                     request.cookies.get('token'))

`or` short-circuits on the first TRUTHY value, and `dchub_session` was the
anti-scrape attestation, which `routes/session_cookie.py` issues to EVERY
visitor. So on every browser `session_token` was the attestation,
`decode_jwt()` always raised on its `<issued_ts>|<ip_prefix>|<hmac_sig>` value,
and the `except: pass` dropped through — the login JWT sitting in the
`dchub_token` cookie was NEVER read. A user whose credential lived only in that
cookie, with no Bearer header, silently resolved to no plan.

Not dead code. Shadowing. The same chain appeared three times
(api_tier_gating.py twice, land_power_usage_limiter.py once) and all three were
proven to pick the attestation by EXECUTING them against a browser holding both
cookies.

There is a second, latent half. The attestation carries no user id and no plan,
and any anonymous caller mints one by loading a public page — map_tier_gating.py
("★ WHY THIS IS SAFE") makes it a rule that it may never stand in for paid
status. Listing it beside real credentials is how it would BECOME one: the day
anyone makes that cookie carry a JWT, an anonymous browser cookie turns into a
tier credential. So the name must stay out of these chains even now that it
decodes to nothing.

WHY THIS EXECUTES THE CHAIN INSTEAD OF GREPPING IT
──────────────────────────────────────────────────
A source assert ("api_tier_gating.py does not contain 'dchub_session'") is
satisfied by deleting a comment, and would have passed against a chain that
merely reordered the names while keeping the bug. The only assertion that can
catch shadowing is one made against the VALUE the expression actually selects,
so each chain is lifted out of the shipped source by AST and evaluated against a
stub cookie jar. Per CLAUDE.md no test imports main.py; nothing here imports the
modules either, so no blueprint or DB pool is touched.

★ CANNOT-CHECK IS NOT PASS. A file that will not parse, or a scan that finds
fewer chains than the floor, fails rather than reporting green on the remainder.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Files known to resolve a caller from cookie-borne JWTs. A file that stops
# containing a chain trips the floor below rather than silently dropping out.
FILES = ("api_tier_gating.py", "land_power_usage_limiter.py")

# Floor: three chains existed when this guard was written. Fewer means either a
# chain moved somewhere this guard does not look, or the AST matcher stopped
# matching — both make a green run meaningless.
MIN_CHAINS = 3

# Names that are attestations, NOT credentials. `dchub_browser` is the current
# name; `dchub_session` is the pre-2026-09-07 one, still accepted on read by
# routes/session_cookie.py during its transition window and therefore still
# present on browsers — so it is still able to shadow.
ATTESTATION_COOKIES = ("dchub_browser", "dchub_session")

# What a browser mid-transition actually carries: an attestation AND a real JWT.
BROWSER_COOKIES = {
    "dchub_browser": "1788000000|72.208|aabbccdd11223344",
    "dchub_session": "1788000000|72.208|aabbccdd11223344",
    "dchub_token": "eyJhbGciOiJIUzI1NiJ9.REAL_LOGIN_JWT.sig",
}


class _Jar:
    def __init__(self, data): self._data = data
    def get(self, key, default=None): return self._data.get(key, default)


class _Req:
    def __init__(self, data): self.cookies = _Jar(data)


def _looks_like_jwt(value: str) -> bool:
    return isinstance(value, str) and value.count(".") == 2


def _chains():
    """(file, lineno, ast_node, [cookie names in order]) for every
    `session_token = <or-chain of request.cookies.get(...)>` in FILES."""
    found = []
    for name in FILES:
        path = REPO / name
        if not path.exists():
            pytest.fail(f"{name} missing — cannot check is not pass")
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError as e:
            pytest.fail(f"{name} does not parse: {e}")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and t.id == "session_token"
                       for t in node.targets):
                continue
            # Match a single `request.cookies.get(...)` as well as an or-chain:
            # collapsing a chain to one name is exactly what fixing one looks
            # like, and a one-name chain can still name an attestation. Keying
            # only on BoolOp would let a fixed file drop out of the scan and
            # take its coverage with it.
            names = [c.args[0].value for c in ast.walk(node.value)
                     if isinstance(c, ast.Call)
                     and getattr(c.func, "attr", "") == "get"
                     and c.args and isinstance(c.args[0], ast.Constant)]
            if not names:
                continue
            found.append((name, node.lineno, node.value, names))
    if len(found) < MIN_CHAINS:
        pytest.fail(
            f"found only {len(found)} cookie chain(s) across {FILES}; floor is "
            f"{MIN_CHAINS}. A chain moved, or the AST matcher stopped matching — "
            f"either way this guard is no longer checking what it claims to."
        )
    return found


def _evaluate(node, cookies):
    return eval(compile(ast.Expression(node), "<chain>", "eval"),
                {"request": _Req(cookies)})


# ── the guard ────────────────────────────────────────────────────────────

def test_no_attestation_cookie_in_a_jwt_chain():
    offenders = [
        (f, ln, [n for n in names if n in ATTESTATION_COOKIES])
        for f, ln, _node, names in _chains()
        if any(n in ATTESTATION_COOKIES for n in names)
    ]
    assert not offenders, (
        "the browser attestation cookie appears in a JWT-decode chain at "
        + "; ".join(f"{f}:{ln} {hits}" for f, ln, hits in offenders)
        + ". It is issued to every anonymous visitor and can never decode, so it "
          "shadows any credential listed after it — and listing it beside real "
          "credentials is how it would become one."
    )


def test_a_browser_with_both_cookies_resolves_the_real_jwt():
    """The behavioural half: execute each shipped chain against a browser that
    carries an attestation AND a login JWT, and require the JWT to win."""
    for f, ln, node, names in _chains():
        picked = _evaluate(node, BROWSER_COOKIES)
        if "dchub_token" not in names:
            # This chain cannot see the JWT cookie at all; it must at least not
            # have selected an attestation value.
            assert picked not in BROWSER_COOKIES.values() or _looks_like_jwt(picked), (
                f"{f}:{ln} selected {picked!r} — an attestation, not a credential"
            )
            continue
        assert _looks_like_jwt(picked), (
            f"{f}:{ln} selected {picked!r} instead of the login JWT. Order is "
            f"{names}; `or` short-circuits on the first truthy value, so a name "
            f"that is always present shadows every credential after it."
        )


# ── proof the guard can fail ─────────────────────────────────────────────

def test_guard_can_actually_fail():
    """Mutation check, permanent rather than a one-time manual step.

    Rebuilds the pre-fix chain and runs BOTH assertions above against it. If
    either passes, the matcher or the evaluator is broken and the real tests
    prove nothing.
    """
    pre_fix = ast.parse(
        "(request.cookies.get('session_token') or "
        " request.cookies.get('dchub_session') or "
        " request.cookies.get('dchub_token') or "
        " request.cookies.get('token'))"
    ).body[0].value

    names = [c.args[0].value for c in ast.walk(pre_fix)
             if isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "get"
             and c.args and isinstance(c.args[0], ast.Constant)]
    assert any(n in ATTESTATION_COOKIES for n in names), (
        "the name matcher no longer flags the pre-fix chain — "
        "test_no_attestation_cookie_in_a_jwt_chain is vacuous"
    )

    picked = _evaluate(pre_fix, BROWSER_COOKIES)
    assert not _looks_like_jwt(picked), (
        "the pre-fix chain no longer selects the attestation — the evaluator is "
        "broken, so test_a_browser_with_both_cookies_resolves_the_real_jwt is "
        "vacuous"
    )
    assert picked == BROWSER_COOKIES["dchub_session"], (
        f"expected the pre-fix chain to select the attestation, got {picked!r}"
    )
