"""tests/test_brain_issue_janitor.py — the two lanes added 2026-07-19.

Context: the janitor only swept brain-l15-auto + brain-l23-lifecycle, so
l22 investigation issues (#1604) and the rolling brain-mirror alert
(#1523) had NO close-out path and sat open forever. These tests pin the
new lanes' decision logic with GitHub + the mirror probe stubbed — no
network, no DB.

Safety semantics under test:
  · l22: stale-close ONLY (default 14d), not_planned — young issues
    untouched.
  · mirror: closes ONLY on live-verified score recovery (completed);
    a low or unreachable score closes NOTHING (fail closed), regardless
    of issue age.
"""
import datetime

import pytest

ij = pytest.importorskip("routes.brain_issue_janitor")


def _issue(number, label, age_days, title="t"):
    created = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(days=age_days))
    return {"number": number, "title": title,
            "created_at": created.isoformat().replace("+00:00", "Z"),
            "labels": [{"name": label}]}


def _stub_labels(monkeypatch, by_label):
    monkeypatch.setattr(ij, "_open_issues",
                        lambda label: by_label.get(label, []))
    monkeypatch.setattr(ij, "_token", lambda: "ghp_test")
    # No L14 chains reachable → l15 resolution-closes fail closed (not
    # under test here); age-based lanes still run.
    monkeypatch.setattr(ij, "_current_chain_titles", lambda: None)


def test_l22_stale_close_and_grace(monkeypatch):
    _stub_labels(monkeypatch, {
        ij._L22_LABEL: [_issue(1604, ij._L22_LABEL, 20.0),
                        _issue(1700, ij._L22_LABEL, 3.0)],
    })
    out = ij.janitor_sweep(dry_run=True)
    closes = {c["number"]: c for c in out["would_close"]}
    assert 1604 in closes
    assert closes[1604]["state_reason"] == "not_planned"
    assert closes[1604]["reason"].startswith("l22_stale_")
    assert 1700 not in closes  # 3d < 14d default window


def test_mirror_closes_completed_on_recovery(monkeypatch):
    _stub_labels(monkeypatch, {
        ij._MIRROR_LABEL: [_issue(1523, ij._MIRROR_LABEL, 7.0)],
    })
    monkeypatch.setattr(ij, "_mirror_recovered", lambda: 3.4)
    out = ij.janitor_sweep(dry_run=True)
    closes = {c["number"]: c for c in out["would_close"]}
    assert closes[1523]["state_reason"] == "completed"
    assert closes[1523]["reason"] == "mirror_score_recovered"
    assert "3.40/4" in closes[1523]["comment"]


def test_mirror_never_stale_closes_while_low(monkeypatch):
    """A 90-day-old mirror issue with a still-low (or unknown) score is a
    LIVE alert — the janitor must not touch it."""
    _stub_labels(monkeypatch, {
        ij._MIRROR_LABEL: [_issue(1523, ij._MIRROR_LABEL, 90.0)],
    })
    monkeypatch.setattr(ij, "_mirror_recovered", lambda: None)
    out = ij.janitor_sweep(dry_run=True)
    assert out["would_close"] == []


def test_mirror_probe_not_called_when_no_mirror_issues(monkeypatch):
    """No open mirror issues → no report fetch (keeps the sweep cheap)."""
    _stub_labels(monkeypatch, {})
    boom = lambda: (_ for _ in ()).throw(AssertionError("probed"))
    monkeypatch.setattr(ij, "_mirror_recovered", boom)
    out = ij.janitor_sweep(dry_run=True)
    assert out["would_close"] == []


def test_mirror_recovered_parses_and_fails_closed(monkeypatch):
    """_mirror_recovered: >= 3.0 → score; < 3.0 or missing → None."""
    import io
    import json

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(payload):
        return lambda req, timeout=0: _Resp(json.dumps(payload).encode())

    import urllib.request as rq
    monkeypatch.setattr(rq, "urlopen",
                        _fake_urlopen({"honest_grade": {"honest_score": 3.2}}))
    assert ij._mirror_recovered() == 3.2
    monkeypatch.setattr(rq, "urlopen",
                        _fake_urlopen({"honest_grade": {"honest_score": 2.1}}))
    assert ij._mirror_recovered() is None
    monkeypatch.setattr(rq, "urlopen", _fake_urlopen({"ok": True}))
    assert ij._mirror_recovered() is None  # missing key → fail closed
