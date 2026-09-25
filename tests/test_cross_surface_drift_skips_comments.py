"""check_cross_surface_value_drift must not flag a number inside a comment.

Live 2026-09-25: routes/state_of_power.py:250 — a comment quoting the
"144 markets scored" claim it had fixed — was reported as a hardcoded
metric for 36 days, and its squasher row could never self-clear.

Runs the REAL detector over the REAL allow-listed files with a stubbed
canonical_stats, so it breaks if a comment is flagged again anywhere.
"""
from __future__ import annotations

import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _run(monkeypatch, markets=300, countries=170):
    cs = types.ModuleType("canonical_stats")
    cs._FALLBACK = {"facilities": 1}
    cs.get_canonical_stats = lambda force=False: {
        "facilities": 24600, "markets": markets, "countries": countries}
    monkeypatch.setitem(sys.modules, "canonical_stats", cs)
    from routes import brain_consistency_radar as r
    return r.check_cross_surface_value_drift()


def _line(url):
    rel, _, ln = url.rpartition(":")
    return (ROOT / rel).read_text(encoding="utf-8").splitlines()[int(ln) - 1]


def test_no_finding_points_at_a_comment(monkeypatch):
    found = [f for f in _run(monkeypatch)
             if f["issue"] == "cross_surface_metric_divergence"]
    comments = [(f["url"], _line(f["url"]).strip()) for f in found
                if _line(f["url"]).lstrip().startswith(("#", "//", "*"))]
    assert comments == []


def test_CONTROL_a_code_literal_is_still_flagged(monkeypatch):
    """With a live value far from every literal, real code literals must
    still fire — otherwise the test above passes on a detector that returns
    nothing."""
    found = [f for f in _run(monkeypatch, markets=5000, countries=5000)
             if f["issue"] == "cross_surface_metric_divergence"]
    assert found, "control: the detector flagged nothing at all"
    assert all(not _line(f["url"]).lstrip().startswith("#") for f in found)
