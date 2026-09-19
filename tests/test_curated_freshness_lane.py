"""GUARD — the curated-reference lane must convict on the WORST row, must never
read a write timestamp, and must never call an unreadable endpoint stale.

FENCES _curated_checks in routes/ingestion_freshness_master_shell.py. Every
test drives the real shipped function against real row dicts.

──────────────────────────────────────────────────────────────────────────
★ THE REGRESSION THIS LANE EXISTS FOR (test_write_stamp_cannot_vouch_for_a_row)

tax_incentives is re-seeded from DEFAULT_INCENTIVES on EVERY boot
(INSERT .. ON CONFLICT DO UPDATE), so every row's last_modified reads
"written seconds ago" forever. A lane keyed on write time would rank this the
FRESHEST layer on the board while it served a program repealed 14 months ago —
a permanent false green, the worst form of RELOAD-IS-NOT-GROWTH. Freshness
here comes from last_verified in the row CONTENT and nothing else.

THE FOUR PROPERTIES, each with a MUST-FAIL CONTROL so a vacuous test cannot
masquerade as a passing one:

1. NEVER-VERIFIED CONVICTS. Absent last_verified is infinitely stale, not a
   row to skip — skipping would make the lane pass loudest exactly when the
   data is least checked. Control: all rows stamped -> passes.
2. THE WORST ROW IS THE VERDICT. One row verified today must not vouch for a
   row nobody has looked at in 400 days. Control: all rows fresh -> passes.
3. UNREADABLE IS NOT STALE. A dead endpoint renders pass=None WITH a reason,
   asserted `is None` — `assert not passed` would pass on False and let the
   exact defect through.
4. A MALFORMED STAMP IS NOT A VERIFICATION. It is worse than absent because
   it LOOKS like one.
"""
import datetime as dt

import routes.ingestion_freshness_master_shell as sh

SPEC = dict(key="tax_incentives", label="state tax incentive programs",
            endpoint="/api/v1/tax-incentives", id_field="abbr",
            cadence_days=180, cadence="test cadence", source="test")
TODAY = dt.date(2026, 9, 18)


def _by_id(checks):
    return {c["id"]: c for c in checks}


def _rows(n, verified_on="2026-09-18", **extra):
    return [dict(abbr=f"S{i:02d}", last_verified=verified_on, **extra)
            for i in range(n)]


# ── 1. never-verified convicts ──────────────────────────────────────────────
def test_unverified_rows_convict():
    rows = _rows(9) + [dict(abbr=f"U{i:02d}") for i in range(41)]
    c = _by_id(sh._curated_checks(SPEC, rows, None, today=TODAY))
    assert c["tax_incentives_all_verified"]["pass"] is False
    assert "41 of 50" in c["tax_incentives_all_verified"]["detail"]


def test_all_verified_does_not_convict():
    """CONTROL for 1 — a lane that convicted everything would fail here."""
    c = _by_id(sh._curated_checks(SPEC, _rows(50), None, today=TODAY))
    assert c["tax_incentives_all_verified"]["pass"] is True


# ── 2. the worst row is the verdict ─────────────────────────────────────────
def test_oldest_row_is_the_verdict_not_the_freshest():
    """THE regression. 49 rows verified today, one 400 days ago."""
    rows = _rows(49) + [dict(abbr="ZZ", last_verified="2025-08-14")]
    c = _by_id(sh._curated_checks(SPEC, rows, None, today=TODAY))
    assert c["tax_incentives_oldest"]["pass"] is False
    d = c["tax_incentives_oldest"]["detail"]
    assert "ZZ" in d, "the worst row must be named"
    assert "400d ago" in d


def test_one_fresh_row_cannot_clear_the_lane():
    """The failure this lane exists to catch: everything stale but one."""
    rows = [dict(abbr="AA", last_verified="2026-09-18")] + [
        dict(abbr=f"S{i:02d}", last_verified="2025-01-01") for i in range(49)]
    c = _by_id(sh._curated_checks(SPEC, rows, None, today=TODAY))
    assert c["tax_incentives_oldest"]["pass"] is False, \
        "a single freshly-verified row must not vouch for 49 stale ones"


def test_all_fresh_does_not_convict():
    """CONTROL for 2."""
    c = _by_id(sh._curated_checks(SPEC, _rows(50), None, today=TODAY))
    assert c["tax_incentives_oldest"]["pass"] is True


# ── ★ 3. a write stamp cannot vouch for a row ───────────────────────────────
def test_write_stamp_cannot_vouch_for_a_row():
    """★ THE PERMANENT-FALSE-GREEN REGRESSION.

    Every row carries a last_modified written seconds ago by the boot re-seed,
    and NOT ONE has ever been verified. A lane reading write time reports the
    freshest layer on the board. This lane must still convict."""
    now = TODAY.isoformat()
    rows = [dict(abbr=f"S{i:02d}", last_modified=now, updated_at=now)
            for i in range(50)]
    c = _by_id(sh._curated_checks(SPEC, rows, None, today=TODAY))
    assert c["tax_incentives_all_verified"]["pass"] is False, \
        "a fresh write stamp must never stand in for a verification"
    assert c["tax_incentives_oldest"]["pass"] is None, \
        "no usable last_verified means UNKNOWN age, not a pass"


# ── 4. unreadable is not stale ──────────────────────────────────────────────
def test_unreadable_endpoint_is_indeterminate_not_stale():
    checks = sh._curated_checks(SPEC, None, "URLError: timed out", today=TODAY)
    assert len(checks) == 1
    # `is None`, not falsy: `assert not passed` would also pass on False.
    assert checks[0]["pass"] is None
    assert checks[0]["critical"] is True
    assert "timed out" in checks[0]["detail"]


def test_malformed_stamp_counts_as_never_verified():
    rows = _rows(49) + [dict(abbr="BAD", last_verified="not-a-date")]
    c = _by_id(sh._curated_checks(SPEC, rows, None, today=TODAY))
    assert c["tax_incentives_all_verified"]["pass"] is False
    assert "BAD" in c["tax_incentives_all_verified"]["detail"]


# ── lane verdict wiring ─────────────────────────────────────────────────────
def test_lane_verdict_is_fail_when_rows_unverified():
    rows = _rows(9) + [dict(abbr=f"U{i:02d}") for i in range(41)]
    assert sh._lane_verdict(sh._curated_checks(SPEC, rows, None, today=TODAY)) == "FAIL"


def test_lane_verdict_is_question_mark_when_unreadable():
    checks = sh._curated_checks(SPEC, None, "boom", today=TODAY)
    assert sh._lane_verdict(checks) == "?", "unreadable must be '?', never PASS"


def test_lane_verdict_passes_when_every_row_fresh():
    """CONTROL for the wiring — proves FAIL is not hardcoded."""
    assert sh._lane_verdict(sh._curated_checks(SPEC, _rows(50), None, today=TODAY)) == "PASS"
