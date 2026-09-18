"""check_closed_brain_prs_leave_branches — the detector shipped with the
brain-spec branch-collision fix (2026-09-17).

The janitor closed brain PRs and left their branches. Spec branch names are a
pure function of the item, so each leftover was a permanent GitHub 422
"Reference already exists" on that item's next re-file. 92 of 205 branches
were in that state. This watches the precondition.
"""
import json
import urllib.request

import pytest


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def radar(monkeypatch):
    r = pytest.importorskip("routes.brain_consistency_radar")
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    monkeypatch.setenv("GITHUB_REPO", "azmartone67/dchub-backend")
    return r


def _wire(monkeypatch, refs, open_prs, prs_ok=True):
    """Fake the two API calls. `refs` is page 1; page 2 comes back empty."""
    served = {"refs": False}

    def _urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "matching-refs" in url:
            if served["refs"]:
                return _Resp([])
            served["refs"] = True
            return _Resp([{"ref": "refs/heads/" + n} for n in refs])
        if "pulls?state=open" in url:
            if not prs_ok:
                return _Resp({"message": "Bad credentials"})
            return _Resp([{"head": {"ref": n}} for n in open_prs])
        raise AssertionError("unexpected call: " + url)

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)


def _many(n, prefix="brain-spec/prop-"):
    return [f"{prefix}{i}-a-finding" for i in range(n)]


def test_a_pile_of_branches_no_open_pr_points_at_is_a_finding(radar, monkeypatch):
    _wire(monkeypatch, _many(40), open_prs=[])
    out = radar.check_closed_brain_prs_leave_branches()

    assert len(out) == 1, out
    f = out[0]
    assert f["issue"] == "closed_brain_prs_leave_branches"
    assert f["dead_branches"] == 40 and f["total_spec_branches"] == 40
    assert f["count"] == 40
    assert "422" in f["detail"] and "_delete_pr_branch" in f["detail"]


def test_branches_an_open_pr_still_uses_are_not_dead(radar, monkeypatch):
    """Control: live work must never be counted as a leak."""
    refs = _many(40)
    _wire(monkeypatch, refs, open_prs=refs)
    assert radar.check_closed_brain_prs_leave_branches() == []


def test_it_stays_quiet_below_the_threshold(radar, monkeypatch):
    """A couple in flight is the janitor's next tick, not a leak."""
    _wire(monkeypatch, _many(3), open_prs=[])
    assert radar.check_closed_brain_prs_leave_branches() == []


def test_only_the_dead_ones_are_counted(radar, monkeypatch):
    """Mixed repo: 30 dead + 10 in use -> 30 of 40, not 40."""
    refs = _many(40)
    _wire(monkeypatch, refs, open_prs=refs[:10])
    f = radar.check_closed_brain_prs_leave_branches()[0]
    assert f["dead_branches"] == 30 and f["total_spec_branches"] == 40


def test_no_token_is_silent_not_a_finding(radar, monkeypatch):
    """Fail closed — a radar with no credentials must not invent a leak."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("called the API with no token"))
    assert radar.check_closed_brain_prs_leave_branches() == []


def test_an_unreadable_pr_list_never_becomes_a_leak(radar, monkeypatch):
    """★ If the OPEN-PR call fails, every branch would look unused. Reporting
    that as a leak would be a fabricated finding — stay silent."""
    _wire(monkeypatch, _many(40), open_prs=[], prs_ok=False)
    assert radar.check_closed_brain_prs_leave_branches() == []


def test_no_branches_at_all_is_silent(radar, monkeypatch):
    _wire(monkeypatch, [], open_prs=[])
    assert radar.check_closed_brain_prs_leave_branches() == []


def test_the_detector_is_registered_in_the_sweep():
    """Defined but unregistered = never runs. Checked with ast against the
    executable tuple, so a name in a comment cannot satisfy it."""
    from util import brain_detector_rule as rule
    import os
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), rule.RADAR_PATH), encoding="utf-8").read()
    assert "check_closed_brain_prs_leave_branches" in rule.registered_checks(src)
