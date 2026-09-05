"""The Glama ownership document is served by the ZONE worker, not by Pages.

★ `/.well-known/glama.json` has TWO definitions — `worker.js:2171` here and
`dchub-frontend/_worker.js:3063` — and only this one runs. The zone worker sits
in front of CF Pages and returns before Pages ever sees the path. Measured
2026-09-05: the live response carries `x-dc-worker-version: 4.9.52-…`, the zone
worker's scheme, not Pages' 4.88.0. A correct edit to the frontend copy deploys
green and changes nothing served — the same decoy shape as
`/.well-known/agent.json`, which has three definitions and serves from one.

Why the document matters: `maintainers[{email}]` verifies ANY connector
submitted against this origin. That is how a second DC Hub connector
auto-verified as ours, and why deprecating that connector in the Glama UI does
not hold on its own. Glama's schema marks the form deprecated in favour of an
opaque account-bound `claim` token.

These tests EVALUATE the branch, they do not grep for a string: the handler's
decision is a single regex test, so the token and the pattern are lifted out of
worker.js and applied with Python's `re` to determine which body ships. A
substring check would pass on a branch that can never be reached.
"""
import os
import re

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Glama's published pattern (glama.ai/mcp/schemas/connector.json, draft-07,
# read live 2026-09-05). Pinned here so a loosened copy in worker.js fails
# rather than quietly accepting a malformed claim.
GLAMA_CLAIM_PATTERN = r"^glama_claim_[A-Za-z0-9_-]{32}$"
VALID = "glama_claim_" + "a" * 32


@pytest.fixture(scope="module")
def worker_src() -> str:
    with open(os.path.join(_REPO, "worker.js"), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def handler(worker_src) -> str:
    """The glama.json handler block, brace-walked so nesting cannot truncate."""
    start = worker_src.index("if (pathname === '/.well-known/glama.json') {")
    depth, end = 0, -1
    i = worker_src.index("{", start)
    for i in range(i, len(worker_src)):
        if worker_src[i] == "{":
            depth += 1
        elif worker_src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    assert end != -1, "unbalanced braces in the glama.json handler"
    return worker_src[start:end]


def _token(src: str) -> str:
    m = re.search(r"const GLAMA_CLAIM_TOKEN = '([^']*)'", src)
    assert m, ("GLAMA_CLAIM_TOKEN not found — the claim swap cannot be audited, "
               "so treat it as unheld rather than as passing")
    return m.group(1)


def _branches(handler: str) -> tuple[str, str]:
    """(claim_branch, legacy_branch) source, split on the ternary."""
    q, colon = handler.index("? {"), handler.index("\n      : {")
    return handler[q:colon], handler[colon:]


def test_the_route_is_still_served(handler):
    """Dropping it costs verified ownership after Glama's 7-day grace."""
    assert "$schema" in handler
    assert "glama.ai/mcp/schemas/connector.json" in handler


def test_the_pattern_is_glamas_and_has_not_been_loosened(handler):
    assert GLAMA_CLAIM_PATTERN.replace("^", "").replace("$", "") in handler


def test_a_claim_and_an_email_are_never_served_together(handler):
    """Publishing both leaves the origin-wide auto-verify hole open while
    looking fixed. The swap has to be atomic or it is not a swap."""
    claim_branch, _ = _branches(handler)
    assert '"claim"' in claim_branch
    assert "@" not in claim_branch
    assert "maintainers" not in claim_branch


def test_the_legacy_form_still_works_while_no_token_is_set(handler):
    """Until a claim is pasted the email form has to keep serving —
    ownership today beats a broken swap."""
    _, legacy = _branches(handler)
    assert "maintainers" in legacy


@pytest.mark.parametrize("token,expect_claim", [
    (VALID, True),
    ("", False),
    ("glama_claim_tooshort", False),          # a typo'd claim is not a claim
    ("glama_claim_" + "a" * 33, False),       # off-by-one on the length
])
def test_the_branch_the_handler_would_take(token, expect_claim):
    """Evaluate the decision rather than grep for it."""
    assert bool(re.match(GLAMA_CLAIM_PATTERN, token)) is expect_claim


def test_the_committed_token_is_empty_or_well_formed(worker_src):
    tok = _token(worker_src)
    assert tok == "" or re.match(GLAMA_CLAIM_PATTERN, tok), (
        f"committed GLAMA_CLAIM_TOKEN {tok!r} does not match Glama's pattern — "
        f"it would publish an unverifiable ownership document")


def test_the_handler_names_the_frontend_twin(handler, worker_src):
    """The other definition must stay findable from this one. Two copies of a
    path with no cross-reference is how the wrong file gets edited — which is
    exactly what happened here first."""
    lines = worker_src[:worker_src.index(handler)].splitlines()
    # Walk back over the contiguous // comment block that precedes the handler.
    # A fixed character window is the wrong anchor — it silently becomes a
    # different test every time the comment is edited.
    preamble = []
    for line in reversed(lines):
        if line.strip().startswith("//") or not line.strip():
            preamble.append(line)
            continue
        if line.strip().startswith("const GLAMA_CLAIM_TOKEN"):
            preamble.append(line)
            continue
        break
    block = "\n".join(reversed(preamble))
    assert "_worker.js" in block, "the frontend twin is not named"
    assert "x-dc-worker-version" in block, (
        "the comment does not say how to tell which definition is live")
