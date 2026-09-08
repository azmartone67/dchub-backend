"""client_class explains the unrecognised bucket. It must never inflate the
published "N platforms calling MCP tools" headline.

★ THE PRESSURE THIS RESISTS. The unrecognised bucket was 60% of MCP requests
(345 of 577, measured 2026-09-07), and the obvious "fix" is to name those ids
so the headline goes up. That would be fabrication: measured over 30d,
`chain-hire` called ONE tool 1,473 times and `actionist-apps-verification`
swept ~15 tools at 5-15 calls each. Neither is a platform whose agents chose
DC Hub. Class and vendor answer different questions and must stay separate.
"""
from __future__ import annotations

import ast
import pathlib

import ai_platform_canon as canon

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_no_class_token_is_also_a_vendor_alias():
    """A token that classifies must not also resolve to a vendor."""
    vendor_tokens = {t for t, _v in canon._VENDOR_ALIASES}
    class_tokens = {t for t, _c in canon._CLIENT_CLASS_TOKENS}
    overlap = vendor_tokens & class_tokens
    assert not overlap, (
        "these tokens both name a vendor and a client class, so classifying a "
        "caller would also count it as a platform: %s" % sorted(overlap))


def test_the_measured_unrecognised_ids_stay_unrecognised():
    """The live ids that prompted this work must not have become vendors.

    Pinned to what was actually observed on the endpoint, so a future alias
    added for convenience cannot quietly promote a harness into the headline.
    """
    for pid in ("mcp", "mcp-generic-client", "connectors-manager",
                "actionist-apps-verification", "chain-hire",
                "robinsaige-verifier", "mcp-spec-study"):
        assert canon.canonical_platform(pid) is None, (
            "%r now resolves to a vendor and would be counted in the published "
            "platforms headline; it is a client id, not a platform" % pid)


def test_unknown_is_not_a_synonym_for_tooling():
    """★ An id we cannot place must classify as UNKNOWN.

    Reading unknown as tooling is exactly how a real caller stops being
    counted — and the measured case is not hypothetical: `connectors-manager`
    reads like plumbing and behaves like an agent (execute_plan, why_dchub,
    site_selection_canvas, rank_markets, analyze_site).
    """
    assert canon.client_class("connectors-manager") == canon.CLASS_UNKNOWN
    assert canon.client_class("mcp-generic-client") == canon.CLASS_UNKNOWN
    assert canon.client_class("some-client-nobody-has-seen") == canon.CLASS_UNKNOWN
    assert canon.CLASS_UNKNOWN != canon.CLASS_REGISTRY


def test_a_recognised_vendor_classifies_as_assistant():
    for pid in ("claude", "anthropic/api", "chatgpt", "grok"):
        assert canon.client_class(pid) == canon.CLASS_ASSISTANT


def test_verifier_ids_are_recognised_as_probes():
    for pid in ("actionist-apps-verification", "robinsaige-verifier",
                "reviewer-sim", "mcp-spec-study"):
        assert canon.client_class(pid) == canon.CLASS_VERIFIER


def test_reach_computes_the_split_per_row():
    """★ NAMED FOR WHAT IT ACTUALLY CHECKS.

    This was called ...publishes_the_split_and_it_reconciles and asserted the
    key NAMES appear in routes/ai_reach.py. They did — inside _stamp_vendor's
    return statement — while both call sites copied keys by name and dropped
    the new ones. The endpoint shipped without them for ~40 minutes and this
    test stayed green, because presence in a file is not publication.

    Whether the keys reach the payload is now
    tests/test_stamp_vendor_keys_reach_the_payload.py, which compares the
    returned key set to the copied key set per call site via AST. This one
    keeps the narrower claim it can actually support: the split is computed
    per row, so it is re-derivable rather than asserted.
    """
    src = (ROOT / "routes" / "ai_reach.py").read_text(encoding="utf-8")
    assert 'r["client_class"] = _client_class(' in src, (
        "client_class is not stamped per row, so the rollup cannot be "
        "re-derived from the payload it ships in")
    assert '"unrecognised_by_class_requests": _by_class_reqs' in src, (
        "the request rollup is not accumulated alongside the id rollup")


def test_the_canon_import_fallback_fails_to_unknown():
    """If the canon cannot import, every id must read UNCLASSIFIED — never
    'tooling', which is the comfortable answer that hides a real caller."""
    src = (ROOT / "routes" / "ai_reach.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_client_class":
            rets = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
            assert rets, "_client_class fallback returns nothing"
            for r in rets:
                assert isinstance(r.value, ast.Constant) and r.value.value == "unknown", (
                    "the fallback returns %r, not 'unknown'" % getattr(r.value, "value", None))
            found = True
    assert found, "no _client_class fallback defined for a failed canon import"
