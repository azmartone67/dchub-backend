"""The claims feed's Cache-Control must survive main.py's after_request.

★ WHY (2026-09-06). routes/ops_claims.py was changed to emit
`public, max-age=0, s-maxage=60, must-revalidate` on a successful read so a
cold origin hit is absorbed once instead of by every visitor who lands on it.
That header SHIPPED AND WAS INERT. main.py's cache-policy after_request is an
if/elif chain, and the new header carries neither 'private' nor 'no-store', so
it fell past the respect-clause into:

    elif path.startswith('/api/') or path.startswith('/mcp'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'

and came back out as no-store. Nothing failed. The route's own unit tests passed
— they assert what the VIEW returns, and the view was right. Only measuring the
served response found it.

This asserts the ORDER, because order is the whole bug: a branch for this path
must be reached BEFORE the /api/ catch-all. Structural on purpose — the chain
lives inside a DB-gated module that cannot be imported in CI, and a test that
skipped would be the same silence that let the header ship inert.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CLAIMS_PATH = "/api/v1/ops/claims"


def _cache_chain():
    """-> the ordered list of if/elif tests in the cache-policy chain."""
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        src = ast.unparse(node)
        if "'/api/' " in src or '"/api/"' in src:
            pass
        if "startswith('/api/')" in src and "Cache-Control" in src:
            chain, cur = [], node
            while isinstance(cur, ast.If):
                chain.append(ast.unparse(cur.test))
                cur = cur.orelse[0] if (len(cur.orelse) == 1
                                        and isinstance(cur.orelse[0], ast.If)) else None
            return chain
    return []


CHAIN = _cache_chain()


def test_the_cache_policy_chain_was_found():
    assert CHAIN, (
        "could not locate main.py's cache-policy if/elif chain — this guard is "
        "reporting green over a chain it never read")
    assert any("startswith('/api/')" in t for t in CHAIN), CHAIN


def test_the_claims_feed_is_decided_before_the_api_catch_all():
    idx_claims = next((i for i, t in enumerate(CHAIN) if CLAIMS_PATH in t), None)
    idx_catch = next((i for i, t in enumerate(CHAIN) if "startswith('/api/')" in t), None)
    assert idx_claims is not None, (
        f"no branch in the cache-policy chain names {CLAIMS_PATH}. Its route emits "
        f"a shareable Cache-Control that carries neither 'private' nor 'no-store', "
        f"so without its own branch it falls through to the /api/ catch-all and is "
        f"rewritten to no-store — shipped, and inert.")
    assert idx_catch is not None
    assert idx_claims < idx_catch, (
        f"the {CLAIMS_PATH} branch is at position {idx_claims}, AFTER the /api/ "
        f"catch-all at {idx_catch}. An elif chain stops at the first match, so the "
        f"catch-all wins and the header never survives.")


def test_the_claims_branch_still_respects_an_explicit_no_store():
    """A failed read answers 200 with ok=false and no-store. That must not be
    force-published: caching ok=false pins "the ledger is down" at an edge for a
    minute after it is fine again."""
    branch = next((t for t in CHAIN if CLAIMS_PATH in t), "")
    assert "no-store" in branch, (
        f"the {CLAIMS_PATH} branch does not check for an existing no-store, so a "
        f"failed read would be made cacheable: {branch!r}")


@pytest.mark.parametrize("path", ["/api/v1/stats", "/api/v1/site/stats",
                                  "/api/v1/discovery/last-7d"])
def test_the_existing_force_public_paths_are_untouched(path):
    assert any(path in t for t in CHAIN), (
        f"{path} lost its branch in the cache-policy chain — r-stats-edge-cache "
        f"exists because the anti-scrape cookie pinned these at DYNAMIC")
