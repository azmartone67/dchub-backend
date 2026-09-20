"""Lane 5 of the surface-truth shell: "free, no key" is a claim about TIER.

2026-09-20. /llms.txt published 12 endpoints under "FREE API — No Auth, No
Signup, Start Now" and four were gated (403 plan_required x3, 402 x1), one of
them the endpoint the no-MCP policy's own rule 4 told an agent to GET. No lane
in this shell could see it: every other lane compares canon NUMBERS in a body,
and a tier is not a number in a body.

★ These tests pin the lane's FAILURE MODES, not its happy path. A lane that
only goes green when things are fine is worth very little here — the shell
exists because a check that could not check used to read PASS. So each test
drives the lane into one indeterminate state and asserts it renders '?' rather
than PASS, and the one genuinely-bad state and asserts FAIL.

No network: _fetch and _probe_status are stubbed per test.

Run:  python3 -m pytest tests/test_surface_truth_keyless_lane.py -v
"""
from __future__ import annotations

import pytest

stm = pytest.importorskip("routes.surface_truth_master_shell")


_BODY = """# DC Hub
## FREE API — No Auth, No Signup, Start Now
- [Platform Stats](https://dchub.cloud/api/v1/stats): stats
- [Facilities](https://dchub.cloud/api/v1/facilities?q=Virginia): search
- [Markets](https://dchub.cloud/api/v1/markets): markets
- [Compare](https://dchub.cloud/api/v1/markets/compare?markets=a,b): compare
- [News](https://dchub.cloud/api/news?limit=10): news
- [Deals](https://dchub.cloud/api/v1/transactions?limit=10): deals
- [Solar](https://dchub.cloud/api/renewable/solar?lat=1&lon=2): solar

## KEY REQUIRED — these four are NOT keyless
- [Fuel Mix](https://dchub.cloud/api/grid/fuel-mix?iso=ERCOT): needs Pro
"""


def _wire(monkeypatch, body=_BODY, statuses=None, canary_open=False):
    """Stub the two I/O helpers. statuses maps path -> code (default 200)."""
    statuses = statuses or {}
    monkeypatch.setattr(stm, "_fetch", lambda path, base=None: (body, None))

    def fake_probe(path):
        for canary, _tier in stm._TIER_CANARY:
            if path == canary:
                return (200 if canary_open else 403), None
        code = statuses.get(path, 200)
        return (None, "stubbed failure") if code is None else (code, None)

    monkeypatch.setattr(stm, "_probe_status", fake_probe)


def _verdict(checks):
    return stm._lane_verdict(checks)


def test_all_keyless_endpoints_open_is_a_pass(monkeypatch):
    _wire(monkeypatch)
    checks = stm._lane_keyless_is_keyless("22,900+")
    assert _verdict(checks) == "PASS", [c for c in checks if c["pass"] is not True]


def test_a_gated_endpoint_in_the_free_list_fails_the_lane(monkeypatch):
    """The defect this lane exists for."""
    _wire(monkeypatch, statuses={"/api/v1/markets": 403})
    checks = stm._lane_keyless_is_keyless("22,900+")
    assert _verdict(checks) == "FAIL"
    bad = [c for c in checks if c["pass"] is False]
    assert len(bad) == 1 and "/api/v1/markets" in bad[0]["name"]
    assert "403" in bad[0]["detail"]


@pytest.mark.parametrize("code", [401, 402, 403])
def test_every_auth_status_is_a_failure_not_just_403(monkeypatch, code):
    _wire(monkeypatch, statuses={"/api/news?limit=10": code})
    assert _verdict(stm._lane_keyless_is_keyless("22,900+")) == "FAIL"


def test_a_5xx_is_indeterminate_not_a_tier_verdict(monkeypatch):
    """A server error says nothing about tier. It must not read as a gate."""
    _wire(monkeypatch, statuses={"/api/v1/stats": 503})
    checks = stm._lane_keyless_is_keyless("22,900+")
    assert _verdict(checks) == "?"
    assert not [c for c in checks if c["pass"] is False]


def test_an_unprobeable_endpoint_is_indeterminate(monkeypatch):
    _wire(monkeypatch, statuses={"/api/v1/stats": None})
    assert _verdict(stm._lane_keyless_is_keyless("22,900+")) == "?"


def test_a_short_free_list_is_indeterminate_not_a_pass(monkeypatch):
    """The floor. Without it, an emptied block is a green board."""
    short = _BODY.replace(
        "- [Compare](https://dchub.cloud/api/v1/markets/compare?markets=a,b): compare\n"
        "- [News](https://dchub.cloud/api/news?limit=10): news\n"
        "- [Deals](https://dchub.cloud/api/v1/transactions?limit=10): deals\n"
        "- [Solar](https://dchub.cloud/api/renewable/solar?lat=1&lon=2): solar\n", "")
    _wire(monkeypatch, body=short)
    checks = stm._lane_keyless_is_keyless("22,900+")
    assert _verdict(checks) == "?"
    assert "vacuous" in checks[0]["detail"]


def test_a_missing_heading_is_indeterminate(monkeypatch):
    _wire(monkeypatch, body=_BODY.replace("## KEY REQUIRED", "## SOMETHING ELSE"))
    assert _verdict(stm._lane_keyless_is_keyless("22,900+")) == "?"


def test_an_unreachable_llms_txt_is_indeterminate(monkeypatch):
    monkeypatch.setattr(stm, "_fetch", lambda path, base=None: (None, "HTTP 503"))
    checks = stm._lane_keyless_is_keyless("22,900+")
    assert _verdict(checks) == "?"
    assert "could not fetch" in checks[0]["detail"]


def test_a_privileged_vantage_suspends_the_lane_rather_than_passing_it(monkeypatch):
    """★ The false-green this lane could otherwise produce.

    This shell runs on our own infrastructure and the gate meters by IP. If our
    egress were privileged, every gated endpoint would answer 200 and the lane
    would certify a dishonest list. The canary makes that state visible and
    UNJUDGED — and it must not silently become a PASS.
    """
    _wire(monkeypatch, canary_open=True)
    checks = stm._lane_keyless_is_keyless("22,900+")
    assert _verdict(checks) == "?"
    vantage = [c for c in checks if c["id"] == "keyless_vantage"]
    assert vantage and vantage[0]["pass"] is None
    assert "privileged" in vantage[0]["detail"]
    # and it stopped BEFORE publishing any per-endpoint verdict
    assert not [c for c in checks if c["id"].startswith("keyless_api")]


def test_the_lane_is_registered_in_the_tick():
    """A lane that exists but is never called is the bug this shell had."""
    import inspect
    src = inspect.getsource(stm)
    assert '"id": "keyless_is_keyless"' in src, (
        "lane 5 is defined but not in the tick's lane table — it would never run")
    assert "_safe_lane(_lane_keyless_is_keyless, canon)" in src, (
        "lane 5 is registered without _safe_lane; a crash would 500 the tick")


def test_a_canary_is_never_an_endpoint_we_advertise_as_keyless(monkeypatch):
    """★ The coupling that be #4928 exposed, now a guard.

    The canary's controls were /api/grid/fuel-mix and /api/v1/pipeline — two of
    the four endpoints that PR proposed opening. Had they opened, both would
    have answered 200, fired the privileged-vantage branch, and suspended this
    lane permanently on a TRUE condition: the lane goes quiet at exactly the
    moment the list it audits changes.

    The invariant is not "these two specific paths". It is that a control and
    its subject may not be the same endpoint — a canary drawn from the keyless
    list cannot distinguish "we are privileged" from "this is simply free".
    """
    _wire(monkeypatch)
    body = _BODY
    free_block = body[body.find("## FREE API"):body.find("## KEY REQUIRED")]
    for path, _tier in stm._TIER_CANARY:
        bare = path.split("?")[0]
        assert bare not in free_block, (
            "canary control %r is advertised in the keyless block. A control "
            "drawn from the subject cannot detect privilege — when it answers "
            "200 the lane cannot tell 'our egress is privileged' from 'this "
            "endpoint is simply free', and suspends itself forever." % bare)


def test_the_canary_has_at_least_two_controls_from_different_gates():
    """One control is a single point of failure; two cover both gate families.

    free_tier_gate has two independent mechanisms — GATED_PREFIXES (401,
    key-based) and METERED_MAP_PREFIXES (402, session-metered). A canary drawn
    only from one cannot see privilege granted by the other.
    """
    assert len(stm._TIER_CANARY) >= 2, (
        "the canary is down to %d control(s); a single control makes the "
        "privileged-vantage check a coin flip." % len(stm._TIER_CANARY))
    paths = [p.split("?")[0] for p, _ in stm._TIER_CANARY]
    assert len(set(paths)) == len(paths), "duplicate canary controls: %s" % paths
