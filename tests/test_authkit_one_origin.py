"""The AuthKit authorization server has ONE origin, and it cannot drift.

WHY (2026-09-01). The AS URL was a literal in 8 places across three repos plus
one out-of-repo Cloudflare worker, and `routes/agent_a2a.py` carried two
different shapes of it in the same file. The AS is a CONSENSUS value: the
protected-resource document advertises it and the gateway validates every
token's `iss` against it. Repoint one and not the other and `jwtVerify` rejects
every token -- OAuth does not degrade, it stops (the ERR_JWKS_NO_MATCHING_KEY
class already recorded against the previous domain change).

Two assertions, deliberately different in strictness:

  1. PYTHON must not contain the literal at all -- it can import the helper, so
     there is no excuse. Enforced at zero.
  2. STATIC ARTIFACTS (worker.js, *.json) cannot read an env var, so they are
     allowed to carry the literal -- but only the value `workos_authkit._DEFAULT`
     currently holds. That is what makes the cutover safe: changing the default
     turns these files RED and names each one, instead of leaving a discovery
     document quietly advertising the retired AS.

So this file is not a style rule. It is the thing that converts "remember to
update eight places" into "the suite tells you which ones you missed".
"""

import ast
import re
from pathlib import Path

import workos_authkit

REPO = Path(__file__).resolve().parent.parent

# Any AuthKit-shaped AS URL, whoever's it is.
AUTHKIT_RE = re.compile(r"https://[A-Za-z0-9.-]+\.authkit\.app")

SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__",
             ".claude", "dist", "build", ".pytest_cache"}
SCAN_SUFFIXES = {".py", ".js", ".mjs", ".json", ".ts"}

# The one file allowed to define the value.
ORIGIN = "workos_authkit.py"


def _scan():
    """(relative path, set-of-domains) for every scanned file that names one."""
    for p in REPO.rglob("*"):
        if not p.is_file() or p.suffix not in SCAN_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        found = set(AUTHKIT_RE.findall(text))
        if found:
            yield p.relative_to(REPO).as_posix(), found


def _docstring_nodes(tree):
    """Every Constant node that is a docstring, so prose can be excluded."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def _python_value_literals(path):
    """AS-shaped strings that are real VALUES in this file, not prose.

    ★ASSERT OVER THE AST, NEVER A SUBSTRING. The first cut of this guard was a
    grep and it failed on `routes/mcp_oauth_2025_06_18.py`, whose module
    docstring names the domain as an EXAMPLE of what WORKOS_AUTHKIT_DOMAIN
    holds. That file is the one module that was already doing the right thing.
    A guard that fires on the documentation of correct behaviour is the same
    bug this repo hit with test_no_fake_push_reintroduced and the urllib ban --
    comments and docstrings are not code, and a grep cannot tell.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return set()
    docs = _docstring_nodes(tree)
    found = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docs):
            found.update(AUTHKIT_RE.findall(node.value))
    return found


def test_python_never_hardcodes_the_authorization_server():
    """Python has `workos_authkit`; a literal VALUE here is always avoidable."""
    offenders = {}
    for p in REPO.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        rel = p.relative_to(REPO).as_posix()
        if rel == ORIGIN or rel.startswith("tests/"):
            continue
        found = _python_value_literals(p)
        if found:
            offenders[rel] = sorted(found)
    assert not offenders, (
        "Python must import workos_authkit.authkit_domain()/authkit_endpoints() "
        f"instead of hardcoding the AS. Offenders: {offenders}"
    )


def test_static_artifacts_may_carry_the_literal_but_only_the_current_one():
    """worker.js / *.json cannot read env -- so pin them to the single origin.

    A cutover therefore FAILS LOUD here and names every file still on the old
    AS, which is the whole point: these are public discovery documents, and a
    stale one sends a client to an authorization server we no longer accept
    tokens from.
    """
    current = workos_authkit.authkit_domain()
    stale = {}
    for rel, doms in _scan():
        # tests/ is exempt on purpose: a fixture SHOULD use a synthetic domain,
        # so that "the env was honoured" is the only way for it to pass.
        if rel == ORIGIN or rel.startswith("tests/"):
            continue
        wrong = sorted(d for d in doms if d != current)
        if wrong:
            stale[rel] = wrong
    assert not stale, (
        f"These files name an authorization server that is not the current one "
        f"({current}). Update each, then re-run. Files: {stale}"
    )


def test_the_origin_actually_resolves_and_normalises(monkeypatch):
    """The helper is the only thing standing between us and a bad `iss`."""
    monkeypatch.setenv("WORKOS_AUTHKIT_DOMAIN", "  https://auth.dchub.cloud/  ")
    assert workos_authkit.authkit_domain() == "https://auth.dchub.cloud"

    # Whitespace is a DEMONSTRATED failure mode on this project, not a
    # hypothetical: WORKOS_API_KEY on dchub-backend is stored with a trailing
    # space. An un-stripped issuer fails jwtVerify's equality check and the
    # error names the JWT, not the env var.
    monkeypatch.setenv("WORKOS_AUTHKIT_DOMAIN", "https://auth.dchub.cloud ")
    assert workos_authkit.authkit_domain() == "https://auth.dchub.cloud"

    # Blank means "unset", never "no authorization server" -- publishing an
    # empty issuer is worse than publishing the default.
    monkeypatch.setenv("WORKOS_AUTHKIT_DOMAIN", "   ")
    assert workos_authkit.authkit_domain() == workos_authkit._DEFAULT


def test_endpoints_all_derive_from_one_domain(monkeypatch):
    monkeypatch.setenv("WORKOS_AUTHKIT_DOMAIN", "https://auth.dchub.cloud")
    eps = workos_authkit.authkit_endpoints()
    for key, value in eps.items():
        assert value.startswith("https://auth.dchub.cloud"), (key, value)
    # Both naming conventions must agree -- they are the same URL rendered for
    # RFC 8414 consumers and for agent-card/marketplace consumers.
    assert eps["authorization_endpoint"] == eps["authorizationUrl"]
    assert eps["token_endpoint"] == eps["tokenUrl"]
    assert eps["registration_endpoint"] == eps["registrationUrl"]
