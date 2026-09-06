"""Registry Surface Shell #42 (2026-07-29) — lane guards.

The Glama listing served "33 tools ... 21,000+ facilities ... 232 US power
markets" with an EMPTY tools array for 25 days after the repo documented that
exact string as a QA finding. Nothing corrected it, because the two mechanisms
that could have were each pointed at a different wrong surface:

  * the FIXER PATCHed /api/v1/mcp/servers/dchub (404) while the reader used
    /api/mcp/v1/servers/azmartone67/dchub-mcp-server (200). The 2026-07-09 fix
    landed on one copy of the URL and not the other.
  * the DETECTOR probed the JS-rendered HTML page, which reads as unreadable —
    and unreadable scored as drift=FALSE.

These tests guard the properties that keep that from recurring.

Run:  python3 -m pytest tests/test_registry_surface_shell.py -v
"""
from __future__ import annotations

import inspect

from routes import mcp_presence_crawler as pc
from routes.registry_surface_shell import (
    LANES, RANK_CLAIMS, _check, _lane_verdict,
)


def test_one_origin_for_the_glama_url():
    """THE BUG. A second hardcoded copy is a second thing to fix, and the copy
    nobody tests is the one that stays broken."""
    src = inspect.getsource(pc)
    literals = src.count("https://glama.ai/api/")
    assert literals <= 2, (
        f"{literals} literal glama.ai/api/ occurrences — only the f-string inside "
        f"_glama_api_url() (and its docstring) may remain")
    assert callable(pc._glama_api_url), "the single-origin helper is gone"


def test_the_url_helper_uses_the_namespaced_slug_and_correct_path():
    """Both defects the writer carried: an un-namespaced slug AND a transposed
    path segment. Either one alone still 404s."""
    url = pc._glama_api_url()
    assert "/api/mcp/v1/servers/" in url, (
        "path segments are transposed again (/api/v1/mcp/ 404s)")
    assert url.endswith(pc._DCHUB_GLAMA_SLUG), "the slug is no longer namespaced"
    assert "/servers/dchub" not in url.replace(pc._DCHUB_GLAMA_SLUG, ""), \
        "the bare 'dchub' slug is back — it 400s/404s"


def test_reader_and_writer_both_derive_from_the_helper():
    """The failure was not a wrong URL. It was TWO urls, one of them corrected."""
    src = inspect.getsource(pc)
    assert src.count("_glama_api_url(") >= 3, (
        "fewer than the expected call sites (definition + reader + writer) — one "
        "of them has gone back to a literal")


def test_an_unreadable_listing_is_never_a_pass():
    """Glama is the standing proof: an unreadable HTML page scored drift=FALSE
    for 25 days while the listing showed a server with no tools."""
    assert _lane_verdict([_check("x", "n", None, "unreadable")]) == "INDETERMINATE"
    assert _lane_verdict([]) == "INDETERMINATE"
    assert _lane_verdict([_check("a", "n", True, "ok"),
                          _check("b", "n", None, "unreadable")]) == "INDETERMINATE"


def test_empty_tool_array_is_its_own_check():
    """A wrong count still shows a product; an empty array shows nothing. They
    are different failures and must not collapse into one assertion."""
    from routes.registry_surface_shell import _lane_listing_content
    src = inspect.getsource(_lane_listing_content)
    assert "L1.1" in src and "len(tools) > 0" in src, \
        "the empty-array check is gone or folded into the count comparison"
    assert "L1.2" in src, "the count-vs-live check is gone"


def _rank_fetch(positions, dead=()):
    """A registry that puts us at `positions[term]` (1-based) for each term."""
    from routes.mcp_ecosystem_board import OUR_QUALIFIED_NAME

    def fetch(url, params):
        q = params.get("q", "")
        if q in dead:
            return None, "HTTP 503"
        at = positions.get(q)
        servers = []
        for i in range(1, (at or 3) + 1):
            servers.append({"qualifiedName": OUR_QUALIFIED_NAME if i == at
                            else f"rival/{q}-{i}"})
        return {"servers": servers, "pagination": {"totalCount": 100}}, None
    return fetch


def test_rank_claim_is_measured_now_not_declared_unverifiable():
    """★THIS LANE USED TO ASSERT ITS OWN BLINDNESS. It returned INDETERMINATE
    forever on the premise that "Smithery's public API exposes no ranked search
    endpoint we have verified" — which was false when written, and stayed in the
    tree for six weeks while registry_monitor.py in the mcp-server repo read our
    position out of that very endpoint. A lane that reports a gap it could have
    closed by looking is worse than no lane."""
    from routes.registry_surface_shell import _lane_rank_claim
    checks = _lane_rank_claim(fetch=_rank_fetch(
        {"data center": 1, "energy": 1, "grid": 1}))
    assert len(checks) == 1
    assert checks[0]["status"] == "PASS", checks[0]["detail"]
    assert "#1 of 100" in checks[0]["detail"], "the measurement is not shown"
    assert RANK_CLAIMS, "the published claim list is empty, so nothing is tracked"


def test_a_slipped_claimed_term_fails_critically_and_names_the_leader():
    """A published rank we do not hold is a false statement in our own copy."""
    from routes.registry_surface_shell import _lane_rank_claim
    checks = _lane_rank_claim(fetch=_rank_fetch(
        {"data center": 1, "energy": 3, "grid": 1}))
    assert checks[0]["status"] == "FAIL"
    assert checks[0]["critical"] is True
    assert "SLIPPED" in checks[0]["detail"]
    assert "rival/energy-1" in checks[0]["detail"], "the leader is not named"


def test_unreadable_registry_is_indeterminate_never_a_slip():
    """A registry that did not answer has not taken a term from us. Rendering
    that as FAIL would put a term we still hold on the reclaim worklist."""
    from routes.registry_surface_shell import _lane_rank_claim, _lane_verdict
    checks = _lane_rank_claim(fetch=_rank_fetch(
        {}, dead=("data center", "energy", "grid")))
    assert checks[0]["status"] == "INDETERMINATE"
    assert checks[0]["critical"] is False
    assert _lane_verdict(checks) == "INDETERMINATE"


def test_one_unreadable_term_does_not_hide_a_real_slip_on_another():
    """Partial readability must still report the term that was measured lost."""
    from routes.registry_surface_shell import _lane_rank_claim
    checks = _lane_rank_claim(fetch=_rank_fetch(
        {"data center": 1, "grid": 4}, dead=("energy",)))
    assert checks[0]["status"] == "FAIL"
    assert "grid" in checks[0]["detail"]
    assert "energy" in checks[0]["detail"], "the unreadable term is not disclosed"


def test_all_three_lanes_registered_in_order():
    keys = [k for k, _, _ in LANES]
    assert keys == ["listing_content", "fixer_target", "rank_claim"], \
        f"lane set drifted: {keys}"
