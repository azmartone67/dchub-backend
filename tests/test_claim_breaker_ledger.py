"""The claim breaker keeps a durable record of running (2026-09-02).

Measured: /api/v1/brain/claim-breaker/status `calls 0, blocked 0, recent []`
for the gate's whole life, while content_publisher._should_skip_publish has
run it on every LinkedIn/X/Bluesky post since 08-21 — the counters were per
process and reset on each of ~40 deploys/day. Guards: every decision is
persisted with its platform; the summary reads the durable record; an
unreadable ledger is UNMEASURED, never zero. DB-free; never imports main.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

cb = pytest.importorskip("routes.claim_breaker")  # noqa: E402
cp = pytest.importorskip("content_publisher")  # noqa: E402


def test_every_decision_is_persisted_with_its_platform(monkeypatch):
    # Kills: dropping the _persist call from breaker() (the 0-forever shape).
    seen = []
    monkeypatch.setattr(cb, "_persist", lambda d, ctx, n: seen.append((d, ctx, n)) or True)
    d = cb.breaker("7 of 7 US ISOs report live demand.", "post",
                   {"platform": "bluesky", "source": "content_publisher"})
    assert d["kind"] == "post"
    assert len(seen) == 1
    assert seen[0][1]["platform"] == "bluesky" and seen[0][2] > 0


def test_the_disabled_branch_is_persisted_too(monkeypatch):
    # A kill-switched gate that leaves no trace is indistinguishable from a
    # gate that never ran.
    seen = []
    monkeypatch.setattr(cb, "_persist", lambda d, ctx, n: seen.append(d) or True)
    monkeypatch.setenv(cb.KILL_SWITCH_ENV, "1")
    cb.breaker("x", "post", {"platform": "x"})
    assert seen and seen[0]["disabled"] is True


def test_ledger_row_shape_matches_the_insert():
    row = cb.ledger_row(
        {"kind": "post", "ok": False, "trusted": True, "disabled": False,
         "control": {"ok": True},
         "violations": [{"cls": "rows_ne_buildings", "detail": "d" * 500}],
         "classes_run": ["rows_ne_buildings"]},
        {"platform": "LinkedIn", "source": "content_publisher"}, 120)
    assert len(row) == 10
    assert row[0] == "post" and row[1] == "LinkedIn" and row[2] == "content_publisher"
    assert row[3] is False and row[4] is True and row[6] is True
    viol = json.loads(row[7])
    assert viol[0]["cls"] == "rows_ne_buildings" and len(viol[0]["detail"]) == 300
    assert json.loads(row[8]) == ["rows_ne_buildings"] and row[9] == 120


def test_a_broken_ledger_never_reaches_the_publisher(monkeypatch):
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(cb, "_ledger_conn", _boom)
    assert cb._persist({"kind": "post"}, {}, 1) is False
    d = cb.breaker("7 of 7 US ISOs report live demand.", "post", {})
    assert "ok" in d  # the gate still decided


def test_unreadable_ledger_is_unmeasured_not_zero(monkeypatch):
    # Kills: defaulting calls to 0 when the table cannot be read — the
    # original lie in a new coat.
    monkeypatch.setattr(cb, "_ledger_conn", lambda: None)
    s = cb.ledger_summary()
    assert s["measured"] is False and s["calls"] is None


def test_summary_exposes_the_durable_record(monkeypatch):
    monkeypatch.setattr(cb, "ledger_summary",
                        lambda days=7, limit=20: {"measured": True, "calls": 3})
    s = cb.breaker_summary()
    assert s["durable"] == {"measured": True, "calls": 3}
    assert "per worker" in s["counts_note"]


def test_publisher_hands_the_platform_to_the_gate(monkeypatch):
    # Kills: the seam dropping the platform (every row would read platform=None
    # and "did the gate run for Bluesky" would still be unanswerable).
    seen = {}

    def _fake(text, kind, ctx=None):
        seen.update(ctx or {})
        return {"ok": True, "trusted": True, "disabled": False, "violations": []}
    monkeypatch.setattr(cb, "breaker", _fake)
    cp._run_claim_breaker("hello", "post", "bluesky")
    assert seen == {"platform": "bluesky", "source": "content_publisher"}
