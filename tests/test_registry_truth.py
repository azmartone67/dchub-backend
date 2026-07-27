"""Registry truth (2026-07-27) — pins the verdict semantics.

This exists because the registry loop has been "fixed" more than once and
kept regressing to the same shape: it reported healthy on listings it could
not read. On 2026-07-27, 11 of 16 tracked listings were unreadable (403
pulsemcp, 429 cursor.directory, 200-redirect-to-search glama) and every one
recorded drift_detected=FALSE — so the board said "3 drifted of 16" when the
truth was "3 drifted, 11 UNKNOWN, 2 verified".

A boolean cannot express "I could not look." These tests pin the four-state
verdict, and specifically that the two states which have burned us —
unreadable, and resolves-to-someone-else's-page — can never read as clean.

CI-SAFETY: pure classifier, no network, no DB.
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def rt():
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import registry_truth as m
    return m


OURS = "<html>DC Hub — dchub.cloud/mcp — 80 tools</html>"


# ── the two states that have burned us ───────────────────────────────

def test_unreadable_is_never_clean(rt):
    # 403 (pulsemcp) / 429 (cursor.directory) / timeout must all be
    # 'unverified'. The old boolean recorded these as drift=False.
    for status in (403, 429, 401):
        v = rt.classify("u", status, "u", "", 80)
        assert v["verdict"] == "unverified", status
    v = rt.classify("u", None, "u", "", 80)          # timeout / conn error
    assert v["verdict"] == "unverified"


def test_redirect_to_search_is_broken_not_ok(rt):
    # Glama: /mcp/servers/dchub -> 200 at /mcp/servers?query=author%3Adchub.
    # A 200 on a SEARCH page is not our listing; reading a tool count off it
    # is how "0 tools" got recorded as a real reading.
    v = rt.classify("https://glama.ai/mcp/servers/dchub", 200,
                    "https://glama.ai/mcp/servers?query=author%3Adchub",
                    "<html>search results</html>", 80)
    assert v["verdict"] == "broken"
    assert "search" in v["reason"].lower()


def test_page_without_our_identity_is_broken(rt):
    # Identity BEFORE content — never believe counts from a page that is
    # not ours (wrong URL, or delisted).
    v = rt.classify("u", 200, "u", "<html>some other MCP server, 80 tools</html>", 80)
    assert v["verdict"] == "broken"


# ── the healthy / lagging states ─────────────────────────────────────

def test_our_page_in_sync_is_ok(rt):
    v = rt.classify("u", 200, "u", OURS, 80)
    assert v["verdict"] == "verified_ok"
    assert v["found_tools"] == 80


def test_our_page_behind_canon_is_drift_not_broken(rt):
    # Registries re-crawl on their own schedule; Smithery's index lags BY
    # DESIGN (the 2026-06 "dead URL" misdiagnosis). Lag must stay
    # distinguishable from breakage — it self-heals, breakage does not.
    v = rt.classify("u", 200, "u", "<html>DC Hub dchub.cloud — 73 tools</html>", 80)
    assert v["verdict"] == "verified_drift"
    assert v["found_tools"] == 73


def test_our_page_without_a_count_is_ok(rt):
    v = rt.classify("u", 200, "u", "<html>DC Hub — dchub.cloud/mcp</html>", 80)
    assert v["verdict"] == "verified_ok"


def test_json_api_endpoint_is_not_a_search_page(rt):
    """v2 false-positive fix. The official MCP registry's tracked URL IS an
    API query (/v0/servers?search=cloud.dchub) and returns JSON that contains
    us. The first version applied the search-page rule to it and reported a
    HEALTHY listing as broken — crying wolf is how earlier versions of this
    check got ignored."""
    body = '{"servers":[{"server":{"name":"cloud.dchub/mcp-server"}}]}'
    v = rt.classify("https://registry.modelcontextprotocol.io/v0/servers?search=cloud.dchub",
                    200, "https://registry.modelcontextprotocol.io/v0/servers?search=cloud.dchub",
                    body, 80)
    assert v["verdict"] != "broken", v
    # a JSON payload that does NOT contain us is still broken
    v2 = rt.classify("https://x/api?search=y", 200, "https://x/api?search=y",
                     '{"servers":[]}', 80)
    assert v2["verdict"] == "broken"


def test_multiple_counts_on_one_page_prefers_the_dominant_and_reports_both(rt):
    """Surface Truth #30 found four different numbers live simultaneously.
    Taking the FIRST regex match made the verdict depend on page order —
    mcp.so carries "79 tools" x6 and "55 tools" x1."""
    body = ("<html>DC Hub dchub.cloud — 79 tools … 79 tools … 55 tools … "
            "79 tools … 79 tools … 79 tools … 79 tools</html>")
    v = rt.classify("u", 200, "u", body, 79)
    assert v["found_tools"] == 79           # dominant, not first-seen
    assert v["all_counts"] == {55: 1, 79: 6}
    assert "mixed numbers" in v["reason"]


def test_hard_404_is_broken(rt):
    assert rt.classify("u", 404, "u", "", 80)["verdict"] == "broken"


# ── wiring: the lane must be CRITICAL, and the read must be pure-DB ──

def test_lane_is_registered_and_critical():
    src = _read(os.path.join("routes", "seven_levers_master_shell.py"))
    assert "_lane_registry_truth" in src
    assert '"id": "registry_truth"' in src
    # both guards critical=True — a broken or long-unread listing is a RED,
    # not an advisory note.
    i = src.index("def _lane_registry_truth")
    body = src[i:i + 2600]
    assert body.count("critical=True") >= 2


def test_shell_read_path_does_no_outbound_http():
    # The 2026-07-06 flywheel outage: a shell making self-requests took the
    # site down. Crawling lives in the scan endpoint; the shell lane may only
    # read the persisted verdicts.
    src = _read(os.path.join("routes", "registry_truth.py"))
    i = src.index("def read_state")
    body = src[i:src.index("@registry_truth_bp", i)]
    for banned in ("requests.", "_fetch(", "urlopen"):
        assert banned not in body, banned


def test_registered_and_cron_ticked():
    assert "register_blueprint(registry_truth_bp)" in _read("main.py")
    cron = _read(os.path.join("routes", "cron_heartbeat.py"))
    assert "/api/v1/admin/registry-truth/scan" in cron
    assert "REGISTRY_TRUTH_DISABLE" in cron


def test_no_fake_push_reintroduced():
    """The speculative /refresh + /reindex webhooks were deleted 2026-07-17
    after every POST 404'd. Registries have no working push API; the only
    real path is bump-our-manifest -> they re-crawl.

    Asserted as "this module never POSTs at all", which is both stronger and
    immune to the history being described in a comment (an earlier version of
    this test matched the docstring above and failed on its own explanation).
    """
    src = _read(os.path.join("routes", "registry_truth.py"))
    code = "\n".join(l for l in src.split("\n")
                     if not l.lstrip().startswith("#"))
    for banned in ("requests.post", ".post(", "method=\"POST\"", "urlopen("):
        assert banned not in code, banned
