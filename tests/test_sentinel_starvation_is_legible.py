"""`scanned=0` must say WHY, or it gets re-investigated forever.

The sentinel auto-merge lane has never fired. Its log line —
"SWEEP: scanned=0 allowed=0 rejected=0 merged=0" — is indistinguishable from
"the gate rejected everything", and that ambiguity has now cost three separate
audit sessions (2026-07-31, 09-07, 09-08) re-deriving the same conclusion.

Measured 2026-09-08, the lane is correctly idle:
  * `page_persistent_5xx:%` rows in brain_findings, ALL TIME: 0. The upstream
    event has not happened, so no proposal carries the issue key
    (0 of 585 in brain_proposed_code_fixes.issue_key) and
    GATE_SENTINEL_DERIVED has nothing it could accept.
  * The candidate query is FINE — `draft:true "brain-l5" in:title` matches 14
    PRs over 30 days. A human merges them within the hour; this sweep runs
    twice a day and arrives after the queue is empty.

So an empty result is an EMPTY INPUT, not a throttle and not a broken gate.
This guard pins the distinction into the output so the next reader gets it
from the response instead of from archaeology.
"""
import pathlib
import re

from routes import sentinel_auto_merge as sam

SRC = pathlib.Path(sam.__file__).read_text()


def _sweep_body() -> str:
    i = SRC.index("def run_auto_merge_sweep")
    return SRC[i:i + 6000]


def test_sweep_publishes_per_source_input_counts():
    """One aggregate `scanned` cannot distinguish which source is empty."""
    body = _sweep_body()
    assert '"inputs"' in body
    for key in ("l5_draft_prs", "l22_alias_prs", "l22_cron_prs"):
        assert key in body, f"per-source count {key} is not published"


def test_empty_input_is_named_as_such():
    body = _sweep_body()
    assert "starved_because" in body, (
        "an empty sweep must say it had no input, or it reads as a rejection")
    assert re.search(r"if not prs:", body), (
        "the explanation must be attached to the empty case specifically")


def test_the_three_sources_are_still_counted_separately():
    """If someone re-collapses these into one list comprehension the per-source
    counts silently become meaningless."""
    body = _sweep_body()
    assert "_l5 = _list_brain_draft_prs()" in body
    assert "_alias = _list_l22_alias_prs()" in body
    assert "_cron = _list_l22_cron_prs()" in body
    assert "prs = _l5 + _alias + _cron" in body, (
        "the scanned total must still be the sum of exactly the three "
        "sources it reports")
