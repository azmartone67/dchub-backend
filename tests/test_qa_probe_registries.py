"""Tests for the third-party listing drift probe (QA surface 7).

Pure parsing/comparison logic — no network. The dangerous failure here is a
FALSE RED against a registry we do not control, so the over/under/floor rules
get the most coverage.
"""
import pytest

from tools.qa_superuser import probe_registries as pr

# ★2026-08-30 — THE DOCSTRING ABOVE PROMISED "no network" AND THE FILE CALLED ONE.
#   probe() -> tools_rendered(spec) -> fetch(spec["schema_page"]) GETs the
#   registry's rendered schema page over the internet. Nothing stubbed it, so
#   test_a_healthy_listing_passes pinned canon at 82 and then asked glama.ai how
#   many tools it renders. The moment Glama began rendering 83, the test went red
#   on `main` and reddened every open branch — including three that touched
#   nothing near it.
#
#   Same class as #3361 (a tool-count gate that called production) and the rule
#   from #267/#3362: BLOCKING AND NETWORKED ARE THE TWO THINGS A GATE CANNOT BE
#   AT ONCE. This file is the blocking kind, so it must be the offline kind.
#
#   The autouse stub makes the promise true for every test here. A test that
#   wants a different rendered count overrides it; a test that wants the
#   unreadable-page path returns None.
RENDERED_TOOLS = 82   # matches the canon the tests below monkeypatch


@pytest.fixture(autouse=True)
def _no_registry_network(monkeypatch):
    """No test in this file may reach a third-party registry."""
    monkeypatch.setattr(pr, "tools_rendered", lambda spec: RENDERED_TOOLS)

    def _no_fetch(*a, **kw):                      # belt and braces
        raise AssertionError(
            "a test in this file called pr.fetch() — this suite is offline by "
            "contract; stub the helper you need instead of reaching the network")
    monkeypatch.setattr(pr, "fetch", _no_fetch)

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
    by = {f.title.split()[1]: f for f in pr.probe() if "advertises" in f.title}
    over = next(f for f in pr.probe() if "facilities" in f.title)
    under = next(f for f in pr.probe() if "tools (canon" in f.title)
    assert over.severity == pr.CRITICAL   # a false public claim
    assert under.severity == pr.MAJOR     # selling ourselves short


# ★2026-08-30 — THIS FILE REACHED THE LIVE INTERNET. probe_registries counts
# the tool inventory from glama.ai's RENDERED schema page via `fetch()`, and the
# tests below mock only `get_json`. So every test that called pr.probe() made a
# real GET to a third party, and the tool count it asserted against was
# whatever glama happened to be serving.
#
# It broke exactly as that arrangement always does: glama's page moved from 82
# tool links to 83, and `test_a_healthy_listing_passes` — a test whose entire
# claim is "a consistent listing produces no findings" — went red on main and
# blocked every open PR in the repo. The fixture said 82, the internet said 83.
#
# A unit test must not depend on a page we do not control. `schema_page` builds
# a synthetic page with exactly N distinct /tools/<name> links so the count is
# an input to the test rather than an observation of someone else's deploy.
def test_a_healthy_listing_passes(monkeypatch):
    monkeypatch.setattr(pr, "read_canon",
                        lambda: {"tools": 82, "facilities": 17096})
    monkeypatch.setattr(pr, "get_json", lambda url, **kw: (200, {
        "description": "82 tools covering 17,000+ facilities",
        "tools": [{"name": f"t{i}"} for i in range(82)]}))
    out = pr.probe()
    assert out and all(f.verdict == pr.PASS for f in out)
    assert not any(f.counts_as_failure for f in out)


def test_the_schema_page_count_is_an_input_not_the_live_internet(monkeypatch):
    """The regression that broke main: canon and the rendered page disagree.

    With the page pinned, this is a real assertion about the probe. Without it,
    the same code silently asserted whatever glama was serving.
    """
    monkeypatch.setattr(pr, "read_canon", lambda: {"tools": 82})
    # ★ tools_rendered, NOT fetch. #3409's autouse fixture stubs tools_rendered
    # directly, so the count never passes through fetch at all — mocking fetch
    # here changed nothing and this test asserted against the stub's 82 while
    # claiming to test 83.
    monkeypatch.setattr(pr, "tools_rendered", lambda spec: 83)
    monkeypatch.setattr(pr, "get_json", lambda url, **kw: (200, {
        "description": "82 tools", "tools": []}))
    listed = [f for f in pr.probe() if "renders" in f.title]
    assert listed and all(f.verdict == pr.RED for f in listed), (
        "a registry rendering a different tool count than canon must go RED")



def test_remedy_names_the_human_action_since_code_cannot_fix_it(monkeypatch):
    monkeypatch.setattr(pr, "read_canon", lambda: {"tools": 82})
    monkeypatch.setattr(pr, "get_json", lambda url, **kw: (200, {
        "description": "33 tools", "tools": []}))
    out = pr.probe()
    assert any("maintainer" in (f.remedy or "").lower() for f in out)
