"""r-ci-selftag (2026-08-18) — our own CI must not read as external demand.

The defect: dchub-mcp-server's live smoke suite runs against production on every
push. It self-identifies, but the tag is session-scoped and was lost whenever a
tools/call was served by a process that never saw its initialize, so the call
was written as an anonymous EXTERNAL agent. Runner IPs rotate and agent_id is
derived from the forwarded IP, so every CI run minted a brand-new "distinct
agent": 1,700 of 2,114 real 7d calls (80.4%) and 49 of 68 real agents (72.1%).

These tests pin the MEASUREMENT, not the classifier — the classifier lives in
dchub-mcp-server and is mutation-tested there. What must hold here:

  1. a CI-heavy window FAILS the check   (the branch the whole thing is for)
  2. a clean window PASSES               (it is not stuck red)
  3. an unreadable range list is UNMEASURED, never a passing 0% — #1858
  4. no caller IP ever reaches the rendered detail

(3) is the one that has bitten this repo repeatedly: a fail-soft read that
returns an empty list turns "I could not tell" into "there is none".
"""
import importlib

import pytest

asr = importlib.import_module("routes.agent_success_report")
shell = importlib.import_module("routes.growth_integrity_master_shell")

# Real GitHub Actions egress addresses (observed in the 08-18 bursts) and a
# non-GitHub address, so the classifier is exercised on genuine input rather
# than on ranges invented to match it.
CI_IPS = ["52.234.41.65", "20.64.182.19", "4.227.169.193"]
HUMAN_IPS = ["72.208.88.69", "145.132.14.9"]

_RANGES = ["52.234.0.0/16", "20.64.0.0/16", "4.227.0.0/16", "2a06:98c0::/29"]


class _Cur:
    """Stands in for a psycopg2 cursor: _bounded issues BEGIN/SET/COMMIT then
    fetchall(). Only the row payload matters here."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_a, **_k):
        return None

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _rows(ci_calls_per_ip=0, human_calls_per_ip=0):
    rows = []
    for ip in CI_IPS:
        if ci_calls_per_ip:
            rows.append((ip, ci_calls_per_ip, 1))
    for ip in HUMAN_IPS:
        if human_calls_per_ip:
            rows.append((ip, human_calls_per_ip, 1))
    return rows


@pytest.fixture(autouse=True)
def _ranges(monkeypatch):
    import ipaddress
    nets = [ipaddress.ip_network(c) for c in _RANGES]
    monkeypatch.setattr(asr, "github_actions_ranges", lambda force=False: nets)
    # Cold cache per test — a cached verdict from a previous case would make the
    # next one vacuous (the 08-17 lesson: two branches resolving to one answer).
    shell._ci_origin_cache.update({"at": 0.0, "check": None})
    yield
    shell._ci_origin_cache.update({"at": 0.0, "check": None})


def test_ci_heavy_window_is_measured_as_ci():
    """THE REGRESSION: the 08-18 shape — CI dominating a real window."""
    m = asr.measure_ci_origin_share(_Cur(_rows(ci_calls_per_ip=500,
                                               human_calls_per_ip=50)))
    assert m is not None
    assert m["ci_calls"] == 1500 and m["calls"] == 1600
    assert m["share"] == pytest.approx(0.9375, abs=1e-4)
    assert m["ci_agents"] == 3 and m["agents"] == 5


def test_clean_window_is_not_flagged():
    """The False branch, stated: with no CI traffic the share is a real 0.0."""
    m = asr.measure_ci_origin_share(_Cur(_rows(human_calls_per_ip=100)))
    assert m is not None
    assert m["ci_calls"] == 0 and m["share"] == 0.0


def test_unreadable_range_list_is_unmeasured_not_zero(monkeypatch):
    """An unreadable list must NOT render as '0% CI'."""
    monkeypatch.setattr(asr, "github_actions_ranges", lambda force=False: None)
    assert asr.measure_ci_origin_share(_Cur(_rows(ci_calls_per_ip=500))) is None


def test_empty_range_list_is_also_unmeasured(monkeypatch):
    """A present-but-EMPTY list is the same failure wearing a different hat: it
    would classify every address as non-CI and certify a clean window."""
    monkeypatch.setattr(asr, "github_actions_ranges", lambda force=False: [])
    assert asr.measure_ci_origin_share(_Cur(_rows(ci_calls_per_ip=500))) is None


def test_empty_window_is_unmeasured_not_zero():
    assert asr.measure_ci_origin_share(_Cur([])) is None


def test_shell_check_fails_on_a_ci_heavy_window(monkeypatch):
    monkeypatch.setattr(asr, "_conn", lambda: _FakeConn(_rows(ci_calls_per_ip=500,
                                                              human_calls_per_ip=50)))
    c = shell._ci_origin_check()
    assert c["id"] == "a_ci_origin"
    assert c["pass"] is False, c["detail"]
    assert "OUR OWN CI" in c["detail"]


def test_shell_check_passes_on_a_clean_window(monkeypatch):
    monkeypatch.setattr(asr, "_conn", lambda: _FakeConn(_rows(human_calls_per_ip=100)))
    c = shell._ci_origin_check()
    assert c["pass"] is True, c["detail"]


def test_shell_check_is_unmeasured_when_ranges_are_unreadable(monkeypatch):
    monkeypatch.setattr(asr, "github_actions_ranges", lambda force=False: None)
    monkeypatch.setattr(asr, "_conn", lambda: _FakeConn(_rows(ci_calls_per_ip=500)))
    c = shell._ci_origin_check()
    assert c["pass"] is None, c["detail"]
    assert "unmeasured" in c["detail"].lower()


def test_no_caller_ip_reaches_the_rendered_detail(monkeypatch):
    """Caller IPs have no public surface and must not gain one via the shell."""
    monkeypatch.setattr(asr, "_conn", lambda: _FakeConn(_rows(ci_calls_per_ip=500,
                                                              human_calls_per_ip=50)))
    detail = shell._ci_origin_check()["detail"]
    for ip in CI_IPS + HUMAN_IPS:
        assert ip not in detail


def test_the_result_cache_never_holds_an_unmeasured_verdict(monkeypatch):
    """A transient read failure must not be cached for 15 minutes — the next
    tick has to be able to read a real value."""
    shell._ci_origin_cache.update({"at": 0.0, "check": None})
    monkeypatch.setattr(asr, "github_actions_ranges", lambda force=False: None)
    monkeypatch.setattr(asr, "_conn", lambda: _FakeConn(_rows(ci_calls_per_ip=500)))
    assert shell._ci_origin_check()["pass"] is None
    assert shell._ci_origin_cache["check"] is None, "UNMEASURED was cached"


def test_a_measured_verdict_is_cached(monkeypatch):
    shell._ci_origin_cache.update({"at": 0.0, "check": None})
    monkeypatch.setattr(asr, "_conn", lambda: _FakeConn(_rows(human_calls_per_ip=100)))
    assert shell._ci_origin_check()["pass"] is True
    assert shell._ci_origin_cache["check"] is not None
    shell._ci_origin_cache.update({"at": 0.0, "check": None})


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _Cur(self._rows)

    def close(self):
        return None
