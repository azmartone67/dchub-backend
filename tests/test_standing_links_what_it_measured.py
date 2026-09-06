"""A published number and its "verify →" link must describe the same page.

★ WHAT WAS LIVE. /mcp-standing published, publicly and to be shared:

    Registry   Status                   Tools
    Smithery   ✅ verified 2026-09-05   83
    Glama      ✅ verified 2026-08-29   82      ← canon was 83

Two defects in one row.

1. THE NUMBER AND THE LINK CAME FROM DIFFERENT PAGES. `CONFIRMED_REGISTRIES`
   hardcodes the URL a reader clicks (`/mcp/servers/azmartone67/…`), while the
   count is looked up by `db: "glama"` in mcp_presence_listings — which the
   crawler had written from `/mcp/connectors/cloud.dchub/dc-hub-…`, the
   duplicate. "verify →" invited a reader to check a claim against a document
   that never carried it.

   The module already guarded the ADJACENT case, in its own words: "ONLY the
   count the verifying scan itself measured… pairing it with truth_checked_at
   would date a number nobody took that day." Pairing a count with a DATE from
   another run was caught. Pairing it with a URL from another PAGE was not.

2. A 7-DAY-OLD COUNT PASSED AS FRESH. `_STALE_RED_DAYS = 7` gated both the
   checkmark and the number, and the Glama row published 82 at exactly 7 days
   while canon had moved to 83. Those are different claims: "we saw this
   listing" survives a week; "this listing says N tools" does not, because our
   canon moves every few days. The count now needs _STALE_COUNT_DAYS.

Pure functions, no DB, no network — the row builder is exercised by injecting
the verified-map, which is the only thing that made this testable at all.
"""
import datetime as dt

import pytest

ms = pytest.importorskip("routes.mcp_standing")


def _now():
    return dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.timezone.utc)


def _ago(days):
    return _now() - dt.timedelta(days=days)


# ── the checkmark's contract must not have moved ────────────────────────────

def test_checkmark_still_fresh_for_a_week():
    assert ms._is_fresh(_ago(0), _now()) is True
    assert ms._is_fresh(_ago(7), _now()) is True
    assert ms._is_fresh(_ago(8), _now()) is False


def test_checkmark_still_fails_open_on_a_bad_date_and_closed_on_none():
    """Both behaviours predate this change and are load-bearing: never 500 a
    public page, but never invent a verification either."""
    assert ms._is_fresh("not-a-date", _now()) is True
    assert ms._is_fresh(None, _now()) is False


# ── the count's sharper bar ─────────────────────────────────────────────────

def test_a_count_needs_fresher_evidence_than_a_checkmark():
    assert ms._STALE_COUNT_DAYS < ms._STALE_RED_DAYS


def test_the_exact_row_that_shipped_wrong_no_longer_qualifies():
    """Glama: verified 2026-08-29, read on 09-05 — 7 days old, published 82.

    Exercises the REAL gate, not the constants. An earlier version of this file
    asserted `_STALE_COUNT_DAYS < _STALE_RED_DAYS` and called it covered; a
    mutation swapping the gate back to the checkmark's window passed all nine
    tests, because the decision lived inline in a DB-bound function that no
    test could reach.
    """
    assert ms._publishable_count(82, "verified", _ago(7), 83, _now()) is None
    assert ms._is_fresh(_ago(7), _now()) is True   # checkmark still earned


def test_a_fresh_verified_plausible_count_is_published():
    assert ms._publishable_count(83, "verified", _ago(1), 83, _now()) == 83


@pytest.mark.parametrize("tools,verdict,age,canon,why", [
    (83, "broken",      1, 83, "unverified scan"),
    (83, None,          1, 83, "no verdict"),
    (83, "verified",    9, 83, "older than the checkmark window too"),
    (83, "verified", None, 83, "unknown age fails CLOSED for a number"),
    (30, "verified",    1, 83, "implausible vs canon — a parse artifact"),
    (0,  "verified",    1, 83, "zero is not a count"),
    (None, "verified",  1, 83, "nothing measured"),
])
def test_every_bar_a_number_must_clear(tools, verdict, age, canon, why):
    when = None if age is None else _ago(age)
    assert ms._publishable_count(tools, verdict, when, canon, _now()) is None, why


def test_age_is_unknown_rather_than_zero_when_undeterminable():
    """Returning a number here would let a broken date publish a count."""
    assert ms._age_days(None) is None
    assert ms._age_days("not-a-date") is None


# ── the link must point at what was measured ────────────────────────────────

def _rows(monkeypatch, vm):
    monkeypatch.setattr(ms, "_verified_map", lambda: vm)
    return {r["registry"]: r for r in ms._registries_live()}


def test_publishing_a_count_links_the_page_it_was_measured_on(monkeypatch):
    measured = "https://glama.ai/mcp/connectors/cloud.dchub/mcp-server"
    rows = _rows(monkeypatch, {"glama": {"tools": 83, "verified_at": "2026-09-05",
                                         "measured_url": measured}})
    row = rows["Glama"]
    assert row["tools"] == 83
    assert row["url"] == measured
    assert row["url_basis"] == "measured"


def test_no_count_keeps_the_curated_url(monkeypatch):
    """The curated URL still owns the SET — an unmeasured row is not silently
    re-pointed at whatever the crawler last touched."""
    rows = _rows(monkeypatch, {"glama": {"tools": None, "verified_at": None,
                                         "measured_url": "https://example.invalid/x"}})
    row = rows["Glama"]
    assert row["tools"] is None
    assert row["url_basis"] == "curated"
    assert "example.invalid" not in row["url"]


def test_a_missing_measured_url_falls_back_rather_than_emitting_none(monkeypatch):
    rows = _rows(monkeypatch, {"glama": {"tools": 83, "verified_at": "2026-09-05",
                                         "measured_url": None}})
    row = rows["Glama"]
    assert row["url"], "a row must always carry a link"
    assert row["url_basis"] == "curated"


def test_every_row_declares_which_url_it_is_showing(monkeypatch):
    rows = _rows(monkeypatch, {})
    assert rows, "no registries built"
    for name, row in rows.items():
        assert row["url_basis"] in ("measured", "curated"), name
        assert row["url"], name
