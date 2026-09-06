"""White-glove lane 7 (registry onboarding) — found is not onboarded.

★THE 48-DAY LEAD NOBODY CALLED. Measured 2026-09-06. `registry-discover.mjs`
in dchub-mcp-server had been crawling for curated MCP lists every Monday,
exiting GREEN, and refreshing tracking issue #73 with the SAME five candidates
since 2026-07-20. Zero were onboarded. The owner found out by asking.

Two distinct failures, and the shell saw neither:

  1. THE BRIDGE WAS WEDGED — the discover→onboard step logged
     `contents PUT failed 409 — skip` and exited 0, every Monday. Root cause
     fixed 2026-09-01, the day AFTER the last scheduled run, so the fix sat
     unexecuted until a manual dispatch on 09-06 opened the first scaffold PR
     in seven weeks.
  2. NOBODY DISPOSED THE PROPOSALS — discovery only PROPOSES. A candidate left
     sitting is a lead nobody called, which is what lane 1 already asserts for
     partner keys. Same shape, different noun.

NO network, NO DB. `requests.get` is replaced, so these drive the REAL lane
function over synthetic GitHub payloads and assert the semantics.

★THE ONE THAT MATTERS: an unreachable GitHub must read UNMEASURED (None), never
PASS. A lane that cannot check and says "fine" is the false-green this whole
shell exists to prevent.

★EVERY STATEMENT IS INSIDE A FUNCTION — a module-scope exit aborts collection.

Run:  python3 -m pytest tests/test_white_glove_registry_onboarding_lane.py -v
"""
import datetime as dt
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _shell():
    """Import the shell module, shimming flask if it is absent."""
    if "flask" not in sys.modules:
        fake = types.ModuleType("flask")
        fake.Blueprint = lambda *a, **k: types.SimpleNamespace(
            route=lambda *a, **k: (lambda f: f))
        fake.Response = object
        fake.jsonify = lambda *a, **k: None
        fake.request = types.SimpleNamespace(headers={}, args={})
        sys.modules["flask"] = fake
    import routes.white_glove_loop_master_shell as m
    return m


def _iso(days_ago):
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


def _wire(monkeypatch, *, issue_created=None, issue_updated=0, pulls=(),
          boom=False):
    """Replace requests.get with synthetic GitHub answers."""
    import requests

    issue_items = ([] if issue_created is None else
                   [{"number": 73, "created_at": _iso(issue_created),
                     "updated_at": _iso(issue_updated)}])

    def fake_get(url, **kw):
        if boom:
            raise RuntimeError("github down")
        if "search/issues" in url:
            return _Resp({"items": issue_items})
        return _Resp(list(pulls))

    monkeypatch.setattr(requests, "get", fake_get)


def _by_id(checks):
    return {c["id"]: c for c in checks}


def _scaffold(days_ago, state="open"):
    return {"head": {"ref": "discover/add-target-awesome-mcp-gateways"},
            "created_at": _iso(days_ago), "state": state}


def test_a_candidate_left_past_the_sla_is_red(monkeypatch):
    """THE DEFECT: 48 days, five candidates, zero onboarded, shell silent."""
    m = _shell()
    _wire(monkeypatch, issue_created=48, issue_updated=0,
          pulls=[_scaffold(0)])
    c = _by_id(m._lane_registry_onboarding())
    assert c["candidates_disposed"]["pass"] is False, (
        "a candidate open 48d against a 21d SLA must be RED — this is the "
        "check whose absence let the ledger sit for seven weeks")
    assert "48" in str(c["candidates_disposed"]["detail"])


def test_a_freshly_disposed_ledger_is_green(monkeypatch):
    """The fix must not red permanently — a young ledger is fine."""
    m = _shell()
    _wire(monkeypatch, issue_created=3, issue_updated=0, pulls=[_scaffold(1)])
    c = _by_id(m._lane_registry_onboarding())
    assert c["candidates_disposed"]["pass"] is True
    assert c["discovery_running"]["pass"] is True
    assert c["onboarding_bridge"]["pass"] is True


def test_the_409_wedge_shape_is_red(monkeypatch):
    """Candidates accumulating with NO scaffold PR ever opened — seven green
    Mondays produced exactly this."""
    m = _shell()
    _wire(monkeypatch, issue_created=10, issue_updated=0, pulls=[])
    c = _by_id(m._lane_registry_onboarding())
    assert c["onboarding_bridge"]["pass"] is False, (
        "no scaffold PR while candidates are open is the wedged bridge")
    assert "NO scaffold PR" in str(c["onboarding_bridge"]["detail"])


def test_a_scaffold_older_than_the_ledger_does_not_count(monkeypatch):
    """A PR from a PREVIOUS cycle must not vouch for the current candidates —
    otherwise one ancient scaffold marks the bridge healthy forever."""
    m = _shell()
    _wire(monkeypatch, issue_created=10, issue_updated=0,
          pulls=[_scaffold(90)])
    c = _by_id(m._lane_registry_onboarding())
    assert c["onboarding_bridge"]["pass"] is False


def test_a_stalled_crawl_is_red(monkeypatch):
    """If the weekly cron stops, the ledger stops being refreshed."""
    m = _shell()
    _wire(monkeypatch, issue_created=5, issue_updated=30, pulls=[_scaffold(1)])
    c = _by_id(m._lane_registry_onboarding())
    assert c["discovery_running"]["pass"] is False


def test_unreachable_github_is_unmeasured_not_pass(monkeypatch):
    """★A lane that cannot check must never read PASS."""
    m = _shell()
    _wire(monkeypatch, boom=True)
    checks = m._lane_registry_onboarding()
    assert all(c["pass"] is None for c in checks), (
        "GitHub unreachable must yield UNMEASURED, not a green lane")
    assert "UNMEASURED" in str(checks[0]["detail"])


def test_no_open_ledger_is_not_an_error(monkeypatch):
    """Everything triaged and the issue closed is a legitimate green."""
    m = _shell()
    _wire(monkeypatch, issue_created=None)
    c = _by_id(m._lane_registry_onboarding())
    assert c["registry_candidates"]["pass"] is True


def test_pull_requests_that_are_not_scaffolds_are_ignored(monkeypatch):
    """Ordinary PRs must not be mistaken for onboarding progress."""
    m = _shell()
    unrelated = {"head": {"ref": "fix/some-bug"}, "created_at": _iso(0),
                 "state": "open"}
    _wire(monkeypatch, issue_created=10, issue_updated=0, pulls=[unrelated])
    c = _by_id(m._lane_registry_onboarding())
    assert c["onboarding_bridge"]["pass"] is False, (
        "a normal PR is not a scaffold — only discover/add-target-* counts")


def test_the_lane_is_registered_in_the_shell():
    """A lane nothing calls is dead code that tests green."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "routes" / "white_glove_loop_master_shell.py").read_text(
        encoding="utf-8")
    assert "_lane_registry_onboarding()" in src.split("def _lane_registry_onboarding")[-1], (
        "the lane is defined but never added to the lanes list — it would "
        "never run and this file would still pass")
