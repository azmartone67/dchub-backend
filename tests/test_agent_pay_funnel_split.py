"""tests/test_agent_pay_funnel_split.py — one counter per DISTINCT pay event.

THE DEFECT THIS GUARDS. `mpp_challenge` records "a signed quote was ISSUED to
the caller". It was read everywhere — including DC Hub's own agent brief, which
said "13 agents reached the pay step" — as "the agent attempted to pay". The
funnel had no counter between those two events, so 13 issued quotes plus 1
verify failure was indistinguishable from 13 attempts that mostly broke. The two
readings demand opposite fixes (repair the settlement path vs. fix price and
framing), so the board could not tell anyone which to work on.

What is asserted here, and why each assertion can fail:

  1. BASIS COVERAGE — every published counter states which event increments it.
     A counter without a basis is literally the original defect.
  2. SIBLING ISOLATION — each stage reports its OWN input. `_funnel_block` is
     pure, so the wrong-counter mutation ("make paid fire on verify_failed") is
     exercised directly rather than argued about. Every stage gets a distinct
     prime so a swapped pair cannot coincidentally agree.
  3. ABANDONMENT IS PUBLISHED, NOT INFERRED — issued minus returned appears as
     a named number, which was the whole point.
  4. SQL DISJOINTNESS — the FILTER predicates cannot double-count a status into
     two sibling stage counters.
  5. BACK-COMPAT — `mpp_challenge` keeps its wire name and stays in every status
     universe, so no existing published reader silently zeroes.
  6. NO INVENTED HISTORY — the returned-credential series is declared as
     starting at instrumentation, not backfilled.

DB-free: `_funnel_block` and the SQL string are exercised directly.
"""
import re

import pytest

fh = pytest.importorskip("routes.funnel_health")

# Distinct primes: if any stage reads a sibling's value the equality fails.
PREWALL, QUOTED, RETURNED, NO_TERM, FAILED, PAID = 101, 103, 107, 109, 113, 127


@pytest.fixture(scope="module")
def block():
    return fh._funnel_block(PREWALL, QUOTED, RETURNED, NO_TERM, FAILED, PAID, 120)


# ── 1. every counter carries a basis ─────────────────────────────────

def test_every_published_stage_has_a_basis(block):
    """A counter whose name implies an event it does not record is the defect."""
    for stage in block["stages"]:
        assert stage in fh._FUNNEL_BASIS, (
            f"stage {stage!r} is published with no BASIS naming which event "
            f"increments it — that is the exact defect this split exists to fix")
        assert len(fh._FUNNEL_BASIS[stage]) > 40, (
            f"BASIS for {stage!r} is too short to state which rows increment it")


def test_abandonment_carries_its_own_basis(block):
    assert "abandonment.quotes_never_returned" in fh._FUNNEL_BASIS
    assert block["abandonment"]["meaning"]


def test_no_orphan_basis_entries(block):
    """A basis for a counter nobody publishes means the two have drifted."""
    published = set(block["stages"]) | {"abandonment.quotes_never_returned"}
    assert set(fh._FUNNEL_BASIS) == published


def test_quote_issued_basis_says_issuance_not_attempt():
    """The incident in one assertion: the name implied an event it never recorded."""
    b = fh._FUNNEL_BASIS["quote_issued"]
    assert "ISSUANCE ONLY" in b
    assert "Nothing came back" in b


# ── 2. sibling isolation — each counter reports its OWN event ─────────

@pytest.mark.parametrize("stage,expected", [
    ("offer_prewall_shown", PREWALL),
    ("quote_issued", QUOTED),
    ("credential_returned", RETURNED),
    ("verify_failed", FAILED),
    ("paid", PAID),
])
def test_each_stage_reports_its_own_event_not_a_sibling(block, stage, expected):
    assert block["stages"][stage] == expected, (
        f"{stage} is reporting a sibling's count — the counters have re-merged")


def test_stage_values_are_pairwise_distinct(block):
    """Kills a mutation that wires two stages to the same source."""
    vals = list(block["stages"].values())
    assert len(set(vals)) == len(vals), "two stages read the same source value"


def test_returned_without_terminal_outcome_is_its_own_number(block):
    """A credential presented that never settled AND never failed was invisible."""
    assert block["returned_without_terminal_outcome"] == NO_TERM


# ── 3. the abandonment gap is published, not left to arithmetic ───────

def test_abandonment_gap_is_issued_minus_returned(block):
    assert block["abandonment"]["quotes_never_returned"] == QUOTED - RETURNED


def test_abandonment_is_named_in_the_payload_not_derived_by_the_reader(block):
    assert "abandonment" in block
    assert "quotes_never_returned" in block["abandonment"]


def test_abandonment_gap_may_be_negative_and_says_so(block):
    """The one-step passive offer ships a payable challenge without stamping
    mpp_challenge, so a return can outrun the issued count. Clamping that to
    zero would hide a real signal behind a flattering floor."""
    neg = fh._funnel_block(0, 1, 9, 0, 0, 0, 30)
    assert neg["abandonment"]["quotes_never_returned"] == -8, (
        "the gap was clamped — a flattering zero is a bug")
    assert "NEGATIVE" in neg["abandonment"]["sign_note"]


def test_abandonment_rate_is_none_not_zero_when_nothing_was_issued(block):
    """Three-valued: no quotes issued is UNMEASURED, not a 0% abandonment rate."""
    empty = fh._funnel_block(0, 0, 0, 0, 0, 0, 30)
    assert empty["abandonment"]["rate"] is None


# ── 4. the SQL cannot double-count a status into two stages ───────────

def _filters(sql):
    return re.findall(r"FILTER \(WHERE (.+?)\) AS (\w+)", sql, re.S)


def test_funnel_sql_has_one_filter_per_stage():
    f = _filters(fh._FUNNEL_SQL)
    assert len(f) == 6, f"expected 6 stage filters, parsed {len(f)} — guard is vacuous"


def test_terminal_statuses_are_not_shared_between_sibling_stages():
    """paid/verify_failed/prewall/quote must each key off exactly one status.
    Only `credential_returned` is deliberately a union, because a terminal
    outcome implies a return."""
    by_alias = {alias: pred for pred, alias in _filters(fh._FUNNEL_SQL)}
    assert by_alias["paid"].strip() == "status = 'mpp_paid'"
    assert by_alias["failed"].strip() == "status = 'mpp_verify_failed'"
    assert by_alias["quoted"].strip() == "status = 'mpp_challenge'"
    assert by_alias["prewall"].strip() == "status = 'mpp_offer_prewall'"
    assert by_alias["returned_no_terminal"].strip() == "status = 'mpp_credential_returned'"
    # the one intentional union, pinned so it cannot silently widen
    ret = by_alias["returned"]
    for st in ("mpp_credential_returned", "mpp_verify_failed", "mpp_paid"):
        assert st in ret
    assert "mpp_challenge" not in ret, (
        "quote issuance folded into the RETURN counter — that re-creates the "
        "exact conflation this split removes")
    assert "mpp_offer_prewall" not in ret


# ── 5. back-compat: no published reader silently zeroes ──────────────

def test_mpp_challenge_keeps_its_wire_name():
    assert fh._CHAL_ST == "mpp_challenge"


def test_mpp_challenge_still_in_the_status_universe():
    assert "mpp_challenge" in fh._ALL_PAY_ST


def test_the_new_return_status_was_added_to_the_universe():
    """In the universe but unprojected is how mpp_offer_prewall stayed invisible
    for months; and unprojected but in the universe is equally useless."""
    assert fh._FUNNEL_RETURNED_ST in fh._ALL_PAY_ST
    assert fh._FUNNEL_RETURNED_ST in fh._FUNNEL_SQL


def test_quote_issued_is_published_as_an_alias_not_a_rename(block):
    assert block["aliases"]["quote_issued"] == "mpp_challenge"


# ── 6. no invented history ───────────────────────────────────────────

def test_history_declares_the_series_start_and_that_it_was_not_backfilled(block):
    h = block["history"]
    assert h["credential_returned_exact_since"] == fh._FUNNEL_RETURNED_LIVE_UTC
    assert "NOT BACKFILLED" in h["pre_instrumentation_basis"]


def test_a_window_reaching_before_instrumentation_reads_as_a_lower_bound():
    old = fh._funnel_block(0, 13, 1, 0, 1, 0, 120)
    assert old["history"]["window_predates_instrumentation"] is True
    assert old["history"]["credential_returned_measurement"].startswith("LOWER_BOUND")


def test_blind_spots_are_declared_rather_than_reported_as_zero():
    joined = " ".join(fh._FUNNEL_UNMEASURED)
    assert "gate ALLOWED" in joined
    assert "correlation id" in joined
