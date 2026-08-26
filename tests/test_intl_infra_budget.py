"""Guard: a slow Overpass must cost COVERAGE, never the whole run.

WHAT THIS PINS
──────────────
`worker:intl_infra_ingest` reported `status=timeout` at 1800s on 2026-08-26
05:30 and wrote NOTHING. Measured, not assumed — max(infrastructure.fetched_at)
was 2026-08-25 05:08:13Z, so the previous day's run landed 23 rows in ~8 min and
the next one landed zero.

★★★ THE DAMAGE WAS THE DISCARD, NOT THE SLOWNESS. main() called fetch_intl()
over all 18 metros accumulating into one in-memory list, and upserted ONCE
after the loop. crawler_scheduler._run_with_guard abandons the thread at
HARD_TIMEOUT_SECONDS (1800s), so a timeout anywhere in the fetch threw away
every record already fetched. There was no checkpoint and no partial commit.

★★ WHY IT RAN LONG — measured 2026-08-26 07:1xZ, one metro (london-uk),
all three endpoints in OVERPASS_ENDPOINTS order:

    overpass-api.de            HTTP 200            10.5s   4745 elements
    overpass.kumi.systems      HTTP 502             4.9s
    overpass.openstreetmap.ru  ConnectTimeout      75.5s

Endpoint #3 is unreachable, so it contributes latency with no chance of
success. `requests` applies a SCALAR `timeout=` to connect AND read alike, so
failing to open a socket to a dead host burned the full 180s read budget in the
worst case, and 75s in the measured one — 3 attempts x 3 endpoints, per metro,
with no overall deadline. One bad metro could consume 1665s of the 1800s guard
budget while 17 metros went unqueried.

This file exists because BOTH failure modes are invisible in a green run: the
old code reported the same "TOTAL unique REAL records" line whether 18 metros
answered or 2 did, and the deadman stamps last_run=NOW() even on a timeout, so
the lane's freshness clock read current while it wrote nothing.

THE CONTRACT
────────────
  B1. fetch_intl() takes a monotonic `deadline`. Once it passes, every
      remaining metro is reported WITHOUT a network call.
  B2. Records fetched BEFORE the deadline are still returned. This is the
      whole point: partial coverage beats a discarded run.
  B3. 'deadline_skipped' is a DISTINCT status from 'source_unavailable'.
      We never asked upstream, so we must not report upstream as down.
  B4. deadline=None preserves the pre-2026-08-26 behaviour (query everything).
  B5. call_overpass passes a (connect, read) TUPLE, with connect strictly
      smaller — a dead host costs the handshake, not the query budget.
  B6. The default budget leaves headroom under HARD_TIMEOUT_SECONDS so the
      upsert in main() commits instead of being abandoned mid-write.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import intl_infra_ingest as iii  # noqa: E402


# crawler_scheduler.HARD_TIMEOUT_SECONDS, pinned here so this test does not
# import the scheduler (which pulls the whole app). If that constant moves,
# B6 below is the check that should be revisited.
HARD_TIMEOUT_SECONDS = 30 * 60


def _fake_metros(n):
    return [(f"m{i}", f"Metro {i}", "XX", "EMEA", (0.0, 0.0, 1.0, 1.0))
            for i in range(n)]


def _element(osm_id):
    """One valid OSM substation node, shaped as classify_element() expects."""
    return {"type": "node", "id": osm_id, "lat": 1.0, "lon": 2.0,
            "tags": {"power": "substation", "name": f"S{osm_id}"}}


# ── B1 / B2 / B3: the deadline ───────────────────────────────────────────────

def test_deadline_skips_remaining_metros_without_calling_overpass(monkeypatch):
    """B1: past the deadline, no network call is made for the rest."""
    calls = []

    def _spy(query, retries=3):
        calls.append(query)
        return [_element(100 + len(calls))]

    monkeypatch.setattr(iii, "call_overpass", _spy)
    # Not for speed on the happy path (the deadline skips every sleep) but so
    # a MUTATED build that ignores the deadline fails fast instead of sitting
    # in 4 x 5s of polite delay.
    monkeypatch.setattr(iii.time, "sleep", lambda s: None)

    # Deadline already in the past → the very first metro is skipped.
    records, report = iii.fetch_intl(_fake_metros(4),
                                     deadline=time.monotonic() - 1)

    assert calls == [], f"expected zero Overpass calls past the deadline, got {len(calls)}"
    assert len(report) == 4
    assert all(r["status"] == "deadline_skipped" for r in report.values())
    assert records == []


def test_records_fetched_before_the_deadline_survive(monkeypatch):
    """B2: ★ the zero-rows fix. A run that runs out of budget still returns
    everything it already fetched, so main() upserts partial coverage."""
    state = {"n": 0}

    def _spy(query, retries=3):
        state["n"] += 1
        return [_element(1000 + state["n"])]

    monkeypatch.setattr(iii, "call_overpass", _spy)

    # Fake clock driven by how many metros have been fetched, NOT by a finite
    # iterator: pytest itself calls time.monotonic(), so a fake that can run
    # out would raise StopIteration from somewhere unrelated and read as a
    # failure of this guard.
    base = 1_000_000.0
    monkeypatch.setattr(iii.time, "monotonic",
                        lambda: base + (0 if state["n"] < 2 else 9999))
    monkeypatch.setattr(iii.time, "sleep", lambda s: None)

    records, report = iii.fetch_intl(_fake_metros(4), deadline=base + 100)

    assert state["n"] == 2, f"expected exactly 2 metros queried, got {state['n']}"
    assert len(records) == 2, f"records fetched before the deadline were discarded: {records}"
    ok = [k for k, r in report.items() if r["status"] == "ok"]
    skipped = [k for k, r in report.items() if r["status"] == "deadline_skipped"]
    assert len(ok) == 2 and len(skipped) == 2, f"ok={ok} skipped={skipped}"


def test_deadline_skipped_is_not_source_unavailable(monkeypatch):
    """B3: ★ the honesty check. 'we never asked' must not read as
    'upstream was down' — they are different facts about the world."""
    monkeypatch.setattr(iii, "call_overpass", lambda q, retries=3: None)
    monkeypatch.setattr(iii.time, "sleep", lambda s: None)

    # Upstream genuinely down, budget intact → source_unavailable.
    _, down = iii.fetch_intl(_fake_metros(1), deadline=time.monotonic() + 9999)
    # Budget spent → deadline_skipped.
    _, late = iii.fetch_intl(_fake_metros(1), deadline=time.monotonic() - 1)

    assert down["m0"]["status"] == "source_unavailable"
    assert late["m0"]["status"] == "deadline_skipped"
    assert down["m0"]["status"] != late["m0"]["status"]


def test_no_deadline_queries_every_metro(monkeypatch):
    """B4: deadline=None is the pre-fix behaviour, unchanged."""
    calls = []
    monkeypatch.setattr(iii, "call_overpass",
                        lambda q, retries=3: calls.append(q) or [_element(len(calls))])
    monkeypatch.setattr(iii.time, "sleep", lambda s: None)

    records, report = iii.fetch_intl(_fake_metros(5), deadline=None)

    assert len(calls) == 5
    assert all(r["status"] == "ok" for r in report.values())
    assert len(records) == 5


# ── B5: split connect/read timeout ───────────────────────────────────────────

def test_call_overpass_splits_connect_and_read_timeouts(monkeypatch):
    """B5: ★ a dead host must cost the handshake, not the query budget.
    A scalar timeout applies to both, which is how a 75s ConnectTimeout to
    overpass.openstreetmap.ru got charged against the 180s read budget."""
    seen = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"elements": []}

    def _post(url, **kw):
        seen["timeout"] = kw.get("timeout")
        return _Resp()

    monkeypatch.setattr(iii.requests, "post", _post)
    iii.call_overpass("[out:json];out;")

    t = seen["timeout"]
    assert isinstance(t, tuple), f"timeout must be a (connect, read) tuple, got {t!r}"
    assert len(t) == 2
    connect, read = t
    assert connect < read, f"connect ({connect}) must be smaller than read ({read})"
    assert connect <= 15, f"connect timeout {connect}s is too slow to shed a dead host"


# ── B6: the budget fits under the scheduler's hard timeout ───────────────────

def test_default_budget_leaves_headroom_for_the_upsert(monkeypatch):
    """B6: ★ the whole fix is pointless if the budget exceeds the guard's
    hard timeout — the upsert would be abandoned mid-write anyway."""
    monkeypatch.delenv("INTL_INFRA_BUDGET_SECONDS", raising=False)
    budget = iii._budget_seconds()

    assert budget > 0
    assert budget < HARD_TIMEOUT_SECONDS, (
        f"budget {budget}s >= HARD_TIMEOUT_SECONDS {HARD_TIMEOUT_SECONDS}s — "
        "the fetch would still be abandoned before the upsert commits")
    assert HARD_TIMEOUT_SECONDS - budget >= 300, (
        f"only {HARD_TIMEOUT_SECONDS - budget}s left for the upsert of tens of "
        "thousands of row-at-a-time INSERTs — not enough headroom")


@pytest.mark.parametrize("raw,expected", [
    ("600", 600),
    ("  900  ", 900),
    ("", 1200),
    ("not-an-int", 1200),
])
def test_budget_env_override(monkeypatch, raw, expected):
    """B6: operators can retune it; a malformed value falls back rather than
    crashing the lane."""
    monkeypatch.setenv("INTL_INFRA_BUDGET_SECONDS", raw)
    assert iii._budget_seconds() == expected


def test_budget_can_be_disabled(monkeypatch):
    """B6: a non-positive budget disables the deadline entirely (escape hatch
    for a manual full backfill run outside the scheduler)."""
    monkeypatch.setenv("INTL_INFRA_BUDGET_SECONDS", "0")
    assert iii._budget_seconds() == 0
