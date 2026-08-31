"""The WorkOS challenge probe must assert the contract the SERVER actually has.

★2026-08-31 — WHY THIS FILE EXISTS. The probe required anonymous `initialize`
to answer 401. dchub-mcp-server stopped challenging on initialize in
r-challenge-method (2026-07-03):

    if (method !== 'tools/call') return false;   // never on initialize — ask after value

so initialize=200 is CORRECT, and the probe reported it as "durable identity
DISABLED" on every single scan. Its remedy told an operator to set
DCHUB_OAUTH_CHALLENGE_DISABLE=0 — a no-op, since that env is a kill switch read
as /^(1|true|yes|on)$/ and unset already means enabled.

That false RED did not merely fail to catch something: it fed the brain radar,
which opened PR #3417 and two `routes/_proposed_*` scaffolds chasing the same
phantom — two of which contradict each other on the flag's direction.

★ OFFLINE, like every gate in this repo. `requests.post` is stubbed in each
test; nothing here may leave the process. Same rule as
tests/test_qa_probe_registries.py: BLOCKING AND NETWORKED ARE THE TWO THINGS A
GATE CANNOT BE AT ONCE.
"""
import pytest

from routes import site_sentinel as ss


class _Resp:
    def __init__(self, code, headers=None):
        self.status_code = code
        self.headers = headers or {}


def _stub(monkeypatch, script, record=None):
    """Drive requests.post from a per-method script.

    `script` maps an MCP method to a status code, or to a list consumed in
    order (so the Nth anonymous tools/call can differ from the first).
    """
    state = {}

    def _post(url, timeout=None, headers=None, data=None):
        import json
        method = json.loads(data)["method"]
        if record is not None:
            record.append((method, (headers or {}).get("Mcp-Session-Id")))
        val = script[method]
        if isinstance(val, list):
            i = state.get(method, 0)
            state[method] = i + 1
            val = val[min(i, len(val) - 1)]
        hdrs = {"Mcp-Session-Id": "sess-123"} if method == "initialize" else {}
        return _Resp(val, hdrs)

    monkeypatch.setattr("requests.post", _post)


def test_initialize_200_is_correct_and_must_not_red(monkeypatch):
    """★ THE BUG. The old probe RED'd here on every scan."""
    _stub(monkeypatch, {"initialize": 200, "tools/list": 200,
                        "tools/call": [200, 401]})
    _entry, scan = ss._probe_workos_challenge()
    assert scan["healthy"] is True, scan["reason"]
    assert "challenge firing" in scan["reason"]
    # And it must not repeat the no-op remedy that sent an operator to a kill switch.
    assert "DCHUB_OAUTH_CHALLENGE_DISABLE=0" not in scan["reason"]


def test_challenge_on_the_first_call_is_also_healthy(monkeypatch):
    _stub(monkeypatch, {"initialize": 200, "tools/list": 200, "tools/call": 401})
    _entry, scan = ss._probe_workos_challenge()
    assert scan["healthy"] is True
    assert "#1" in scan["reason"]


def test_no_challenge_within_the_bound_is_INDETERMINATE_not_red(monkeypatch):
    """The ambiguous case must not be guessed either way.

    A generous DCHUB_CHALLENGE_AFTER_N and a disabled challenge look identical
    from outside; inventing a verdict is what produced the phantom.
    """
    _stub(monkeypatch, {"initialize": 200, "tools/list": 200, "tools/call": 200})
    _entry, scan = ss._probe_workos_challenge()
    assert scan["healthy"] is True, "an unattributable observation must not alert"
    assert "INDETERMINATE" in scan["reason"]
    assert "DCHUB_OAUTH_CHALLENGE_DISABLE" in scan["reason"], (
        "the reason must still name the authoritative check a human can run")


def test_initialize_401_is_a_REAL_red_the_removed_lockout_came_back(monkeypatch):
    _stub(monkeypatch, {"initialize": 401, "tools/list": 200, "tools/call": 200})
    _entry, scan = ss._probe_workos_challenge()
    assert scan["healthy"] is False
    assert "initialize=401" in scan["reason"]


def test_unreachable_endpoint_is_red(monkeypatch):
    def _boom(*a, **kw):
        raise OSError("connection refused")
    monkeypatch.setattr("requests.post", _boom)
    monkeypatch.setattr(ss.time, "sleep", lambda *_a: None)
    _entry, scan = ss._probe_workos_challenge()
    assert scan["healthy"] is False
    assert "unreachable" in scan["reason"]


def test_unexpected_initialize_status_is_red(monkeypatch):
    _stub(monkeypatch, {"initialize": 503, "tools/list": 200, "tools/call": 200})
    _entry, scan = ss._probe_workos_challenge()
    assert scan["healthy"] is False
    assert "503" in scan["reason"]


def test_the_session_id_is_threaded_or_the_tools_call_leg_is_theatre(monkeypatch):
    """★ Without Mcp-Session-Id the server counts every call as a first call.

    The allowance is per-session prior-anonymous-calls, so a probe that drops
    the session id could never observe a challenge no matter how many calls it
    made — it would look exactly like "disabled" forever.
    """
    seen = []
    _stub(monkeypatch, {"initialize": 200, "tools/list": 200,
                        "tools/call": [200, 401]}, record=seen)
    ss._probe_workos_challenge()
    after_init = [sid for (m, sid) in seen if m != "initialize"]
    assert after_init, "no calls followed initialize"
    assert all(sid == "sess-123" for sid in after_init), (
        f"session id dropped on a follow-up call: {seen}")


def test_the_probe_is_bounded_and_does_not_burn_the_allowance(monkeypatch):
    seen = []
    _stub(monkeypatch, {"initialize": 200, "tools/list": 200, "tools/call": 200},
          record=seen)
    ss._probe_workos_challenge()
    calls = [m for (m, _s) in seen if m == "tools/call"]
    assert len(calls) == ss._WORKOS_PROBE_MAX_CALLS, (
        f"expected at most {ss._WORKOS_PROBE_MAX_CALLS} tools/call, got {len(calls)}")


def test_disabled_probe_short_circuits_without_network(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("probe must not touch the network when disabled")
    monkeypatch.setattr("requests.post", _boom)
    monkeypatch.setattr(ss, "_WORKOS_PROBE_ENABLED", False)
    _entry, scan = ss._probe_workos_challenge()
    assert scan["healthy"] is True
    assert "disabled" in scan["reason"]
