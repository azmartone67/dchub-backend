"""Tests for the third-party listing drift probe (QA surface 7).

Pure parsing/comparison logic — no network. The dangerous failure here is a
FALSE RED against a registry we do not control, so the over/under/floor rules
get the most coverage.

★★★2026-08-30: "no network" was ASSERTED BY THIS DOCSTRING AND NOTHING ELSE.
`probe()` reaches the network through TWO functions — `get_json` for the JSON
API and `fetch` for the HTML schema page (`tools_rendered`). Every test patched
`get_json`; NOT ONE patched `fetch`. So all eight probe() tests were quietly
scraping live glama.ai on every run.

It stayed invisible because the other seven assert that a SPECIFIC finding
exists, and an extra live-derived finding does not disturb that. Only
`test_a_healthy_listing_passes` asserts that EVERYTHING passes, so only it
noticed — and only once the live page changed: it went red the day our catalog
moved 82 -> 83 tools, reporting "glama renders 83 (canon 82)" against a canon
the test had monkeypatched to 82. A hermetic test cannot be broken by the
outside world; this one was, which is the proof it was never hermetic.

★The fixture below makes that structural instead of remembered: both network
functions raise unless a test replaces them, so the next un-patched call fails
LOUDLY in CI rather than silently reading production.
"""
import pytest

from tools.qa_superuser import probe_registries as pr


@pytest.fixture(autouse=True)
def _seal_network(monkeypatch):
    """Both doors, not one. A test that needs data replaces these."""
    def _escaped(*a, **kw):  # noqa: ANN001
        raise AssertionError(
            "probe_registries reached the NETWORK inside a test. Patch BOTH "
            "pr.get_json and pr.fetch — tools_rendered() uses fetch(), and "
            "that single unpatched door made every probe() test read live "
            "glama.ai for months.")
    monkeypatch.setattr(pr, "fetch", _escaped)
    monkeypatch.setattr(pr, "get_json", _escaped)


def _renders(n: int):
    """A schema page rendering exactly `n` distinct tool links, matching
    spec['tool_link']. Returns the (status, headers, body) triple fetch() does."""
    body = "".join(
        f'<a href="/mcp/servers/azmartone67/dchub-mcp-server/tools/t{i}">t{i}</a>'
        for i in range(n))
    return lambda url, **kw: (200, {}, body)

# The real Glama description, verbatim as the API returns it (escaped M\&A).
GLAMA = ("Description: Data-center, power & gas intelligence MCP server. 33 "
         "tools covering 21,000+ data-center facilities (170+ countries), 232 "
         "US power markets scored by the DC Hub Power Index (DCPI), 2,000+ "
         "tracked M\\&A deals, ISO grid telemetry")


def test_parses_every_claim_from_the_real_listing_text():
    c = pr.claims_in(GLAMA)
    assert c["tools"] == 33
    assert c["facilities"] == 21000
    assert c["deals"] == 2000


def test_escaped_ampersand_does_not_hide_the_deals_claim():
    # ★ The first live run MISSED this: the API returns "M\&A", and a regex
    #   assuming a bare "&" silently dropped the deals OVER-claim — exactly the
    #   class this probe exists to catch.
    assert pr.claims_in("2,000+ tracked M\\&A deals")["deals"] == 2000
    assert pr.claims_in("2,000+ tracked M&A deals")["deals"] == 2000
    assert pr.claims_in("1,745 M&A transactions")["deals"] == 1745


def test_no_claims_in_text_yields_nothing_rather_than_zeros():
    # Kills: defaulting a missing claim to 0 and then reporting it as "under".
    assert pr.claims_in("a description with no numbers at all") == {}
    assert pr.claims_in("") == {}


# ── the over / under / floor rules ──────────────────────────────────────

def test_under_claiming_our_own_tool_count_is_drift():
    assert pr.compare(33, 82, plus=False) == "under"


def test_over_claiming_is_drift_even_with_a_plus():
    # "21,000+" asserts AT LEAST 21,000 against a real 17,096 — a false public
    # claim, not a stale floor.
    assert pr.compare(21000, 17096, plus=True) == "over"
    assert pr.compare(2000, 1745, plus=True) == "over"


def test_a_plus_BELOW_canon_is_a_satisfied_floor_not_drift():
    # ★ The most important false-positive to avoid: "17,000+" is TRUE at
    #   17,096, and calling it drift would train readers to ignore this probe.
    assert pr.compare(17000, 17096, plus=True) == "ok"
    assert pr.compare(1700, 1745, plus=True) == "ok"


def test_an_exact_match_is_ok():
    assert pr.compare(82, 82, plus=False) == "ok"


def test_small_rounding_slack_on_an_exact_claim():
    # Canon figures get rounded in prose; 5% slack, so 17,000 vs 17,096 passes.
    assert pr.compare(17000, 17096, plus=False) == "ok"
    assert pr.compare(10000, 17096, plus=False) == "under"


def test_unknown_canon_never_convicts():
    # If canon could not be read for a field, the probe must not guess.
    assert pr.compare(999, 0, plus=False) == "ok"


# ── the honesty rules the harness enforces at construction ──────────────

def test_every_finding_states_red_when_and_basis(monkeypatch):
    monkeypatch.setattr(pr, "read_canon",
                        lambda: {"tools": 82, "facilities": 17096, "deals": 1745})
    monkeypatch.setattr(pr, "get_json", lambda url, **kw: (200, {
        "description": GLAMA, "tools": []}))
    # ★fetch, not just get_json: tools_rendered() reads the HTML
    # schema page. Rendering canon exactly keeps this test about
    # what it claims to test instead of about live glama.ai.
    monkeypatch.setattr(pr, "fetch", _renders(82))
    out = pr.probe()
    assert out, "probe produced nothing against a known-bad listing"
    for f in out:
        assert f.red_when.strip()
        assert f.basis.strip()


def test_unreadable_canon_is_BLIND_not_a_sweep_of_reds(monkeypatch):
    # A dchub outage must not publish "every listing is wrong".
    monkeypatch.setattr(pr, "read_canon", lambda: None)
    out = pr.probe()
    assert len(out) == 1 and out[0].verdict == pr.BLIND
    assert not out[0].counts_as_failure


def test_a_registry_being_down_is_BLIND_not_RED(monkeypatch):
    # Someone else's server 503ing is not evidence our listing is wrong.
    monkeypatch.setattr(pr, "read_canon", lambda: {"tools": 82})
    # ★ Discriminate on the HOST, not on "dchub": the Glama listing URL is
    #   .../azmartone67/dchub-mcp-server, so a substring check on "dchub"
    #   matches the registry too and the mock silently returns a healthy
    #   record for the very call this test is about.
    monkeypatch.setattr(pr, "get_json", lambda url, **kw:
                        (503, None) if "glama.ai" in url else (200, {}))
    out = pr.probe()
    assert out and all(f.verdict == pr.BLIND for f in out)
    assert not any(f.counts_as_failure for f in out)


def test_over_claim_outranks_under_claim_in_severity(monkeypatch):
    monkeypatch.setattr(pr, "read_canon",
                        lambda: {"tools": 82, "facilities": 17096})
    monkeypatch.setattr(pr, "get_json", lambda url, **kw: (200, {
        "description": "33 tools covering 21,000+ facilities", "tools": []}))
    # ★fetch, not just get_json: tools_rendered() reads the HTML
    # schema page. Rendering canon exactly keeps this test about
    # what it claims to test instead of about live glama.ai.
    monkeypatch.setattr(pr, "fetch", _renders(82))
    by = {f.title.split()[1]: f for f in pr.probe() if "advertises" in f.title}
    over = next(f for f in pr.probe() if "facilities" in f.title)
    under = next(f for f in pr.probe() if "tools (canon" in f.title)
    assert over.severity == pr.CRITICAL   # a false public claim
    assert under.severity == pr.MAJOR     # selling ourselves short


def test_a_healthy_listing_passes(monkeypatch):
    monkeypatch.setattr(pr, "read_canon",
                        lambda: {"tools": 82, "facilities": 17096})
    monkeypatch.setattr(pr, "get_json", lambda url, **kw: (200, {
        "description": "82 tools covering 17,000+ facilities",
        "tools": [{"name": f"t{i}"} for i in range(82)]}))
    # ★fetch, not just get_json: tools_rendered() reads the HTML
    # schema page. Rendering canon exactly keeps this test about
    # what it claims to test instead of about live glama.ai.
    monkeypatch.setattr(pr, "fetch", _renders(82))
    out = pr.probe()
    assert out and all(f.verdict == pr.PASS for f in out)
    assert not any(f.counts_as_failure for f in out)


def test_remedy_names_the_human_action_since_code_cannot_fix_it(monkeypatch):
    monkeypatch.setattr(pr, "read_canon", lambda: {"tools": 82})
    monkeypatch.setattr(pr, "get_json", lambda url, **kw: (200, {
        "description": "33 tools", "tools": []}))
    # ★fetch, not just get_json: tools_rendered() reads the HTML
    # schema page. Rendering canon exactly keeps this test about
    # what it claims to test instead of about live glama.ai.
    monkeypatch.setattr(pr, "fetch", _renders(82))
    out = pr.probe()
    assert any("maintainer" in (f.remedy or "").lower() for f in out)
