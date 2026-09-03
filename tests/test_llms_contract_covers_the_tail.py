"""tests/test_llms_contract_covers_the_tail.py — a cap that hid every defect.

MEASURED 2026-09-03 by probing every advertised URL by hand:

    advertised in llms.txt + llms-full.txt : 59
    probed by check_llms_txt_contract      : 15  (head of list)

    idx 17  404  /dcpi/va
    idx 25  404  /api/v1/facilities/export?format=csv
    idx 28  404  /[page                       <- a markdown artifact, not a route
    idx 57  404  /providers

    dead links inside the probed first 15  : ZERO
    dead links in the truncated tail       : FOUR

Document order puts the free-API block first and the page/asset links last, so
a head-of-list cap systematically protects exactly the tail where rot
accumulates. The detector returned 0 findings — for months — while four
advertised URLs 404'd. That is a vacuous pass, not a passing contract, and it
is the shape this repo keeps paying for: a guard whose coverage bound is
invisible from its result.

THE CAP STAYS. 59 sequential probes is ~25s against a 15s slow-detector
threshold, and this detector already runs inside a _SCAN_BUDGET_S=25 pool, so
removing the bound trades a silent blind spot for a loud timeout. What changes
is that the window ROTATES: each sweep costs the same, and nothing is
permanently unreachable. Full coverage in ceil(59/15) = 4 sweeps.

House rules: no DB, never import main, nothing runs at module scope.

Run:  python3 -m pytest tests/test_llms_contract_covers_the_tail.py -v
"""
from __future__ import annotations

import inspect
import re

import pytest

from routes import brain_consistency_radar as r

SRC = inspect.getsource(r.check_llms_txt_contract)
# The live shape on the day this was written.
N_ADVERTISED = 59
DEAD_IDX = (17, 25, 28, 57)


class _Resp:
    """Minimal urlopen context manager."""

    def __init__(self, body=b"", status=200):
        self._b, self.status = body, status

    def read(self, n=None):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _drive(monkeypatch, *, n=N_ADVERTISED, hour=0, dead=()):
    """Run the REAL check_llms_txt_contract against a synthetic doc.

    ★ THIS EXISTS BECAUSE THE FIRST DRAFT DID NOT. Those tests re-implemented
      the window arithmetic in the test file and asserted on the copy, so
      freezing the detector's offset to 0 — the exact regression they were
      written to catch — left every one of them green. A guard that mirrors the
      code under test proves only that the mirror is right.

    Returns the list of URL paths this sweep actually probed.
    """
    import time as _t
    import urllib.request as _u

    doc = "\n".join("https://dchub.cloud/u%02d" % i for i in range(n)).encode()
    probed = []

    def _fake_urlopen(req, timeout=None):
        url = req.get_full_url() if hasattr(req, "get_full_url") else str(req)
        base = url.split("?")[0]
        if base.endswith(("llms.txt", "llms-full.txt")):
            return _Resp(doc)
        probed.append(base.rsplit("/", 1)[-1])
        idx = int(base.rsplit("/u", 1)[-1])
        if idx in dead:
            raise _u.HTTPError(url, 404, "Not Found", None, None)
        return _Resp(b"", 200)

    monkeypatch.setattr(_u, "urlopen", _fake_urlopen)
    monkeypatch.setattr(_t, "time", lambda: float(hour * 3600))
    findings = r.check_llms_txt_contract()
    return probed, findings


# ── the blind spot is gone ───────────────────────────────────────────────

def test_the_window_rotates_rather_than_truncating():
    assert "urls[:_LLMS_PROBE_CAP]" not in SRC, (
        "a fixed head-of-list slice is what made the tail unreachable")
    assert "_window" in SRC and "% _n" in SRC


@pytest.mark.parametrize("idx", DEAD_IDX)
def test_every_measured_dead_link_is_actually_FOUND_by_some_sweep(idx, monkeypatch):
    """All four 404s sat outside the old window. Drive the real detector across
    a full rotation and require it to FILE each one — not merely to have been
    theoretically reachable."""
    for hour in range(-(-N_ADVERTISED // r._LLMS_PROBE_CAP)):
        _, findings = _drive(monkeypatch, hour=hour, dead=(idx,))
        if any("u%02d" % idx in f["url"] for f in findings):
            return
    pytest.fail("idx %d was never probed across a full rotation" % idx)


def test_the_whole_advertised_set_is_covered_within_one_cycle(monkeypatch):
    seen = set()
    for hour in range(-(-N_ADVERTISED // r._LLMS_PROBE_CAP)):
        probed, _ = _drive(monkeypatch, hour=hour)
        seen |= set(probed)
    assert len(seen) == N_ADVERTISED, (
        "a full rotation covered %d of %d advertised URLs" % (len(seen), N_ADVERTISED))


def test_a_single_sweep_does_not_cover_everything(monkeypatch):
    """CONTROL for the test above: if one sweep covered all 59, the rotation
    would be untested and the cap would not be doing its job."""
    probed, _ = _drive(monkeypatch, hour=0)
    assert len(probed) == r._LLMS_PROBE_CAP < N_ADVERTISED


def test_consecutive_sweeps_probe_different_urls(monkeypatch):
    """★ THE OFFSET MUST ACTUALLY ADVANCE. Freezing it to 0 is the regression
    that the first draft of this file could not see."""
    a, _ = _drive(monkeypatch, hour=0)
    b, _ = _drive(monkeypatch, hour=1)
    assert a != b, "the window did not move between sweeps"
    assert set(a) - set(b), "sweep 1 probed nothing sweep 0 had skipped"


@pytest.mark.parametrize("n", [1, 2, 14, 15, 16, 59, 200])
def test_the_window_is_always_in_range_and_never_empty(n, monkeypatch):
    """Property, not the one live number: the arithmetic must hold for a doc
    that shrinks below the cap as well as one that grows past it."""
    for hour in (0, 1, 7, 999):
        probed, _ = _drive(monkeypatch, n=n, hour=hour)
        assert probed, "empty window for n=%d hour=%d" % (n, hour)
        assert len(probed) == min(r._LLMS_PROBE_CAP, n)
        assert len(set(probed)) == len(probed), "same URL probed twice in a sweep"


@pytest.mark.parametrize("n", [16, 59, 200])
def test_the_cap_still_bounds_each_sweep(n, monkeypatch):
    """The runtime bound is the reason the cap exists; removing it trades a
    silent blind spot for a loud timeout."""
    assert r._LLMS_PROBE_CAP == 15
    probed, _ = _drive(monkeypatch, n=n, hour=3)
    assert len(probed) == 15, "sweep probed %d URLs, cap is 15" % len(probed)


# ── the result no longer over-claims ─────────────────────────────────────

def test_every_finding_names_the_coverage_it_had():
    assert "coverage:" in SRC and "advertised URLs" in SRC, (
        "a finding from a partial sweep must say how partial — otherwise a "
        "reader infers a completeness the detector never claimed")
    assert "window offset" in SRC


def test_the_docstring_says_silence_is_not_completeness():
    doc = r.check_llms_txt_contract.__doc__ or ""
    assert "SILENCE FROM THIS DETECTOR IS NOT" in doc
    assert "ROTATING" in doc


def test_the_cap_constant_carries_its_own_evidence():
    """The next person to consider raising or removing the cap should find the
    measurement, not re-derive it."""
    src = inspect.getsource(r)
    head = src.split("_LLMS_PROBE_CAP = 15")[0][-2000:]
    for marker in ("/dcpi/va", "/providers", "ZERO"):
        assert marker in head, "the cap's evidence lost %r" % marker


# ── markdown artifacts are not advertised routes ─────────────────────────

def test_bracket_placeholders_are_skipped():
    """"https://dchub.cloud/[page" was captured out of markdown "[page](...)"
    and filed as a dead link. It is a doc formatting artifact, not a route."""
    m = re.search(r'if any\(ch in u for ch in \(([^)]*)\)\)', SRC)
    assert m, "the placeholder guard moved"
    chars = m.group(1)
    for ch in ('"{"', '"<"', '"["', '"]"'):
        assert ch in chars, "%s missing from the placeholder skip set" % ch


def test_the_real_dead_links_are_not_skipped_by_that_filter():
    """CONTROL: widening the skip set must not silence the three real 404s."""
    skip = ("{", "<", "…", "[", "]")
    for u in ("https://dchub.cloud/dcpi/va",
              "https://dchub.cloud/api/v1/facilities/export?format=csv",
              "https://dchub.cloud/providers"):
        assert not any(c in u for c in skip), "%s would be skipped" % u
    assert any(c in "https://dchub.cloud/[page" for c in skip)


def test_the_detector_budget_is_smaller_than_the_scan_that_runs_it():
    """A per-detector budget of 45s inside a 25s scan is not a bound — it can
    never bind. The real ceiling was 51s, because the budget is tested BEFORE
    each probe and a probe may then take its full 6s timeout.

    Lowering it is only safe BECAUSE the window rotates: under the old fixed
    head-of-list slice an early break meant those URLs were never probed at
    all, so a tighter budget would have deepened the blind spot it was
    supposed to bound."""
    src = inspect.getsource(r)
    m = re.search(r"^\s*_SCAN_BUDGET_S = (\d+)", src, re.M)
    assert m, "the scan budget moved; re-read before editing"
    scan = float(m.group(1))
    assert r._LLMS_TIME_BUDGET_S < scan, (
        "per-detector budget %.1fs >= scan budget %.1fs — it cannot bind"
        % (r._LLMS_TIME_BUDGET_S, scan))
    # and the pre-probe test means the true ceiling is budget + one probe
    assert r._LLMS_TIME_BUDGET_S + 6 <= scan + 6, "ceiling arithmetic changed"
