"""r-country-code-collision (2026-08-08) — a non-US market must never read a
US state's live grid tables.

MEASURED LIVE before the fix, every one byte-identical to its US twin:

    Pune       state='IN' -> INDIANA. queue_capacity_mw 35,229.5 and
               gen_additions_12mo_mw 1,666.5, identical to Indianapolis.
    Batam      state='ID' -> IDAHO. gen_additions_12mo_mw 400.0, identical
               to Boise.
    Querétaro  state='MX' -> 1,272.8 MW of CAISO-queue wind farms in Tecate,
               Baja California, ~1,900 km away on a different grid.
    Johor      state='MY' -> no US collision; correctly published nulls and
               signal_tier='low'. The control case.

`interconnect_queue` and `planned_generators` are US tables keyed on a 2-letter
USPS state code. A non-US market's `state` holds a 2-letter ISO-3166 COUNTRY
code. The queries carried no country predicate.

★★★ WHY THE EARLIER GUARD GAVE FALSE ASSURANCE. When Pune was added,
tests/test_dcpi_str_coverage_markets.py::test_pune_state_code_collision_is_defused_by_the_label_guard
asserted that `_normalize_us_isos` does not rewrite Pune's ISO label from
POSOCO to MISO. That assertion was TRUE and it is still true — and it was
irrelevant. The live adapters key on `state` DIRECTLY and never consult the ISO
label at all, so protecting the label protected nothing about the data. A guard
that verifies the mechanism you thought about, while the damage flows through a
mechanism you did not, reads green forever.

This file tests the DATA PATH, not the label.
"""
import pytest

pytest.importorskip("flask")
pytest.importorskip("psycopg2")

from routes import dcpi  # noqa: E402

# (state, iso, may_read_us_state_tables, why)
CASES = [
    # ── the four measured failures / the control ──────────────────────────
    ("IN", "POSOCO",     False, "Pune, India — 'IN' is also Indiana"),
    ("ID", "PLN-BATAM",  False, "Batam, Indonesia — 'ID' is also Idaho"),
    ("MX", "CENACE",     False, "Queretaro, Mexico — matched Baja rows"),
    ("MY", "TNB",        False, "Johor, Malaysia — the control case"),
    # ── other international markets that share a US state code ───────────
    ("DE", "ENTSOE-DE",  False, "Germany — 'DE' is also Delaware"),
    ("PA", "ENTSOE-PT",  False, "'PA' is Pennsylvania; never for a non-US ISO"),
    ("SG", "EMA",        False, "Singapore"),
    ("JP", "TEPCO",      False, "Japan"),
    ("KR", "KEPCO-KR",   False, "South Korea"),
    ("ON", "IESO",       False, "Ontario — Canadian, not a US state read"),
    ("QC", "HQ",         False, "Quebec"),
    # ── US markets must be UNAFFECTED ─────────────────────────────────────
    ("IN", "MISO",       True,  "Indianapolis — genuinely Indiana"),
    ("ID", "WECC",       True,  "Boise — genuinely Idaho"),
    ("TX", "ERCOT",      True,  "Dallas"),
    ("VA", "PJM",        True,  "Ashburn"),
    ("CA", "CAISO",      True,  "Santa Clara"),
    ("PR", "PREPA",      True,  "Puerto Rico — a US territory in the ISO set"),
    ("GU", "GPA",        True,  "Guam"),
    # ── empty ISO: allowed only when the state is genuinely a USPS code ──
    ("WY", "",           True,  "US market whose ISO label is blank"),
    ("GP", "",           False, "johannesburg — Gauteng, not a US state"),
    ("MX", "",           False, "bare country code with no ISO"),
    (None, None,         False, "nothing to go on — must not read US tables"),
]


def test_live_state_reads_gated_on_country_not_on_code_shape():
    wrong = []
    for state, iso, expected, why in CASES:
        got = dcpi._live_state_reads_allowed(state, iso)
        if got is not expected:
            wrong.append(f"state={state!r} iso={iso!r} -> {got}, want {expected} ({why})")
    assert not wrong, "\n  ".join([""] + wrong)


def test_the_two_worst_cases_are_real_us_states_so_an_allowlist_cannot_fix_this():
    """Pins WHY the fix is a country predicate and not a code allow-list.

    'IN' and 'ID' are genuine USPS codes. Any future refactor that reduces this
    guard to "is the state a US state code?" silently reopens Pune and Batam —
    the two worst cases — while still passing a naive test.
    """
    assert "IN" in dcpi._US_STATE_CODES
    assert "ID" in dcpi._US_STATE_CODES
    # Same code, opposite answers, decided purely by the market's grid label.
    assert dcpi._live_state_reads_allowed("IN", "MISO") is True
    assert dcpi._live_state_reads_allowed("IN", "POSOCO") is False
    assert dcpi._live_state_reads_allowed("ID", "WECC") is True
    assert dcpi._live_state_reads_allowed("ID", "PLN-BATAM") is False


def test_no_us_iso_is_missing_from_the_gate():
    """Every ISO the scorer treats as US must pass the gate.

    If a US grid label were absent from _US_DCPI_ISOS, this fix would REMOVE
    live data from a real US market — a regression that would look like a
    scoring change rather than a bug.
    """
    for iso in sorted(dcpi._US_DCPI_ISOS):
        assert dcpi._live_state_reads_allowed("TX", iso) is True, iso


def test_gate_is_actually_wired_into_both_adapter_call_sites():
    """The predicate existing is not the same as the predicate being applied.

    Reads the shipped source: both _state_queue_depth and _state_gen_additions
    must be guarded. An unguarded call site is the whole bug.
    """
    import inspect
    import re
    src = inspect.getsource(dcpi.gather_metrics_for_market)
    for fn in ("_state_queue_depth", "_state_gen_additions"):
        calls = re.findall(rf"{fn}\(state\)(?:\s+if\s+(\w+))?", src)
        assert calls, f"{fn} is not called in gather_metrics_for_market at all"
        for guard in calls:
            assert guard, (
                f"{fn}(state) is called UNGUARDED — a non-US market will read "
                f"US state data through it")
            assert "live_state" in guard, (
                f"{fn} is guarded by {guard!r}, which is not the "
                f"country predicate")
