"""tests/test_abandoned_claim_overcount.py — the abandoned-claims counter
contradicted its own premise (2026-08-17).

THE PREMISE, from the counter's own comment block:

    _claim_slot pre-inserts success=FALSE, error_msg='claimed_in_flight' and
    sets ONLY claimed_at; _record fills posted_at later. If the process dies in
    between the row is stranded forever — and because EVERY counter above
    filters on posted_at, WHICH A STRANDED ROW NEVER GETS, it was invisible
    even to attempts_7d.

So a stranded row is defined by having NO posted_at. The counter never checked
that. It is possible — and routine — for a row to carry posted_at and still read
'claimed_in_flight', because _claim_slot's re-claim path resets error_msg back
to 'claimed_in_flight' on a later tick, overwriting the outcome marker on a row
that already reached _record.

Live /api/v1/linkedin-quad/status, 2026-08-17. All three counted rows had
claimed_at LATER than posted_at:

    slot           posted_at     re-claimed_at
    08-13 16:00    16:01:11      19:52:21
    08-13 08:00    08:00:15      11:51:50
    08-12 08:00    08:03:12      19:54:25

/api/v1/media/pulse reported "3 slots claimed then abandoned in 7d — the run
died between claim and publish, so the row still reads 'claimed_in_flight' and
the slot produced nothing" about three slots that had all reached _record hours
earlier. That is the same class of wrong as the healthy-through-an-outage bug
this counter was added to fix, pointed the other way: it sends the operator
hunting a crash that never happened.

Run:  python3 -m pytest tests/test_abandoned_claim_overcount.py -v
"""
from __future__ import annotations

import inspect
import re

import pytest

from routes.dchub_media_revival import media_pulse


@pytest.fixture(scope="module")
def abandoned_filter() -> str:
    """The one FILTER clause that counts abandoned claims, comments stripped.

    Isolated rather than searched for across the whole function, so this cannot
    accidentally pass by matching the SUPPRESSED counter (its benign twin,
    which also keys on claimed_at) or a guard in another query."""
    src = inspect.getsource(media_pulse)
    src = re.sub(r"--[^\n]*", "", src)          # strip SQL comments
    clauses = [c for c in re.findall(r"COUNT\(\*\) FILTER \((.*?)\)\s*,", src, re.S)
               if "claimed_in_flight" in c]
    assert len(clauses) == 1, (
        f"expected exactly one abandoned-claims FILTER, found {len(clauses)} — "
        "the fixture is no longer isolating the right clause")
    return " ".join(clauses[0].split())


def test_abandoned_claims_require_no_posted_at(abandoned_filter):
    """THE PIN. A row with posted_at reached _record and recorded an outcome —
    by the counter's own definition it cannot be a death between claim and
    publish."""
    assert "posted_at IS NULL" in abandoned_filter, (
        "the abandoned-claims counter must exclude rows that already have a "
        "posted_at, or a re-claimed row that published hours earlier is "
        "reported as a mid-flight death")


def test_the_original_guards_survive(abandoned_filter):
    """Control: the fix must ADD a condition, not replace the ones that make
    this counter mean anything. Without this, deleting the whole clause and
    writing `posted_at IS NULL` alone would pass the pin above."""
    assert "success IS NOT TRUE" in abandoned_filter, "lost the done-row guard"
    assert "claimed_in_flight" in abandoned_filter, "lost the in-flight-state guard"
    assert "INTERVAL '7 days'" in abandoned_filter, "lost the 7d window"
    assert "INTERVAL '1 hour'" in abandoned_filter, (
        "lost the claim-TTL guard — a publish genuinely in flight right now "
        "would be counted as abandoned")


def test_suppressed_twin_is_not_the_clause_under_test():
    """The benign twin must still exist and must NOT have acquired the new
    condition: a deliberate editorial exit legitimately has no posted_at, and
    excluding those would zero the lead-supply signal."""
    src = re.sub(r"--[^\n]*", "", inspect.getsource(media_pulse))
    twin = [c for c in re.findall(r"COUNT\(\*\) FILTER \((.*?)\)\s*[,\n]", src, re.S)
            if "suppressed:" in c]
    assert twin, "the suppressed-slots counter disappeared"
    assert "posted_at IS NULL" not in " ".join(twin[0].split()), (
        "the suppressed counter must not inherit the posted_at guard")
