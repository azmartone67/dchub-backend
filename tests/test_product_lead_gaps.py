"""The product lead's machine source: product gaps callers already measured.

Measured 2026-09-02: /api/v1/brain/product-lead-intake board_as_of=null,
judged_total=0 — the refuted-claims feed has had ONE row in its life. Two
instruments count what the product lacks (agentic_query_misses,
mcp_upgrade_signals); they become `product_gap:<intent|tool>` findings through
the same discipline as the claims: trust gate -> eligibility -> cap+rotate ->
persisted refusal. Pure; never imports main.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from routes import brain_product_lead_intake as pl


def _iso(age_h=1.0):
    return (datetime.now(timezone.utc) - timedelta(hours=age_h)).isoformat()


def _unmet(norm="water risk by parcel", count=5, age_h=1.0):
    return {"norm": norm, "count": count, "last": _iso(age_h),
            "samples": ["water risk for parcel 12", "parcel water"]}


def _press(tool="analyze_parcel", count=4, distinct=3, age_h=1.0):
    return {"tool": tool, "count": count, "distinct": distinct, "last": _iso(age_h)}


def test_both_sources_normalise_into_one_row_shape():
    rows = pl.gap_rows([_unmet()], [_press()])
    assert [(r["kind"], r["key"], r["count"]) for r in rows] == [
        ("intent", "water risk by parcel", 5), ("tool", "analyze_parcel", 4)]
    assert rows[1]["distinct"] == 3 and rows[0]["samples"]
    assert pl.gap_rows([{"norm": ""}], [{"tool": None}]) == []


# ── trust gate ───────────────────────────────────────────────────────────

def test_a_failed_source_read_is_refused_not_empty():
    # Kills: treating a read error as "no gaps" (silence dressed as clean).
    why = pl.gap_refusal({"error": "OperationalError: x", "rows": []})
    assert why and "read failed" in why
    assert pl.gap_refusal(None)


def test_a_stale_source_is_refused_and_a_fresh_one_is_not():
    rows = pl.gap_rows([_unmet(age_h=800)], [])
    src = {"rows": rows, "newest_at": _iso(800)}
    assert "old" in pl.gap_refusal(src, max_age_h=720)
    src["newest_at"] = _iso(1)
    assert pl.gap_refusal(src, max_age_h=720) is None


def test_an_empty_source_is_quiet_not_refused():
    assert pl.gap_refusal({"rows": [], "newest_at": None}) is None


# ── eligibility + cap + rotate ───────────────────────────────────────────

def test_one_miss_is_noise_min_count_gates_it():
    # Kills: seeding every single-miss norm (40 rows/refresh of typos).
    rows = pl.gap_rows([_unmet("a", 1), _unmet("b", 3), _unmet("c", 9)], [])
    got, total = pl.select_seedable_gaps(rows, limit=9, cycle=0, min_count=3)
    assert [r["key"] for r in got] == ["c", "b"] and total == 2


def test_capped_and_rotated_so_the_tail_gets_budget():
    rows = pl.gap_rows([_unmet(k, 10 - i) for i, k in enumerate("abcde")], [])
    c0 = [r["key"] for r in pl.select_seedable_gaps(rows, limit=2, cycle=0, min_count=1)[0]]
    c1 = [r["key"] for r in pl.select_seedable_gaps(rows, limit=2, cycle=1, min_count=1)[0]]
    assert c0 == ["a", "b"] and c1 == ["c", "d"]


# ── shape ────────────────────────────────────────────────────────────────

def test_findings_carry_the_plead_prefix_the_count_and_its_kind():
    # Kills: dropping the prefix (FIX_MAP could body-substitute it) or
    # publishing the ask-count as a recurrence tally.
    f = pl.to_gap_findings(pl.gap_rows([_unmet(count=7)], []), "2026-09-02")[0]
    assert f["issue"] == "plead_product_gap:water risk by parcel"
    assert f["url"] == "dchub://product-lead/gap/intent/water risk by parcel"
    assert f["count"] == 7 and f["count_kind"] == "item_count"
    assert "not an opinion" in f["detail"] and "2026-09-02" in f["detail"]
    t = pl.to_gap_findings(pl.gap_rows([], [_press()]))[0]
    assert "paywall" in t["detail"] and "3 distinct callers" in t["detail"]


# ── the refresh persists the refusal; the read serves both lanes ────────

def test_refresh_persists_a_gap_refusal_and_seeds_no_gaps(monkeypatch):
    saved = {}
    monkeypatch.setattr(pl, "state_get", lambda k: None)
    monkeypatch.setattr(pl, "state_set", lambda k, v: saved.update(v) or True)
    out = pl.refresh_snapshot(force=True, load_fn=lambda: {"claims": []},
                              gap_fn=lambda: {"error": "boom", "rows": []})
    assert out["ok"] and out["gap_rows"] == 0 and "read failed" in out["gap_refused"]
    assert saved["gap_rows"] == [] and saved["gap_refused"]


def test_refresh_seeds_gaps_even_when_the_claims_board_is_empty(monkeypatch):
    # Kills: gating the machine source on the (empty-forever) claims board.
    saved = {}
    monkeypatch.setattr(pl, "state_get", lambda k: None)
    monkeypatch.setattr(pl, "state_set", lambda k, v: saved.update(v) or True)
    monkeypatch.setenv("PLEAD_GAP_MIN_COUNT", "3")
    src = {"rows": pl.gap_rows([_unmet(count=5)], [_press(count=1)]),
           "newest_at": _iso(1), "error": None}
    out = pl.refresh_snapshot(force=True, load_fn=lambda: {"claims": []},
                              gap_fn=lambda: src)
    assert out["gap_rows"] == 1 and out["gap_eligible_total"] == 1
    assert saved["gap_rows"][0]["key"] == "water risk by parcel"


def test_the_hot_path_serves_gaps_from_the_snapshot_only(monkeypatch):
    monkeypatch.setattr(pl, "state_get", lambda k: {
        "rows": [], "gap_rows": pl.gap_rows([_unmet(count=4)], []),
        "gap_as_of": "2026-09-02"})
    monkeypatch.setattr(pl, "_load_gap_source", lambda: (_ for _ in ()).throw(
        AssertionError("live read on the hot path")))
    out = pl.product_lead_findings()
    assert len(out) == 1 and out[0]["issue"].startswith("plead_product_gap:")
