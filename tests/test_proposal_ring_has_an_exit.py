"""brain_enhancement_proposals was a ring by construction.

2026-09-08. Across 201 rows and eleven weeks the table's ENTIRE history held
only 'proposed' (156), 'duplicate' (42) and 'queued' (3). No shipped, no
implemented, no rejected — ever. Not because nothing shipped, but because no
code path could write one: the triage pass promotes the top-N to 'queued' and
demotes everything else back to 'proposed', forever.

And nothing consumes 'queued'. `grep -rn "'queued'"` over the tree returns only
OTHER tables (media_thread_generator, upgrade_nudger, detector_scout,
crm_reverse_etl). The "L22 handoff" the check named does not exist, so a row
promoted to 'queued' can sit there indefinitely — 49, 46 and 30 days for the
three that were there.

★ The reason nobody noticed is the check itself. `proposal_pipeline` asserted
`counts['queued'] == QUEUE_SIZE`, which three permanently-stuck rows satisfy
perfectly, and `queued_top3` passed `None` — informational. Both read GREEN for
eleven weeks on a pipeline that had never moved an item out. A check that
cannot tell "flowing" from "stuck" is not measuring the thing it names.
"""
import pathlib
import re

import pytest

from routes import brain_autonomy_master_shell as sh


@pytest.mark.parametrize("days,expected", [
    (None, False),     # unknown must never read as failure
    (0, False),
    (3, False),
    (sh.QUEUE_STALE_DAYS, False),        # boundary: at the threshold is fine
    (sh.QUEUE_STALE_DAYS + 1, True),     # one day past is not
    (49, True),                          # the live oldest when this shipped
    ("nonsense", False),
])
def test_stagnation_boundary(days, expected):
    assert sh.queue_is_stagnant(days) is expected


def test_a_stuck_queue_is_not_a_pass():
    """The load-bearing one. If this ever returns False for a long-stuck
    queue, the ring is invisible again."""
    assert sh.queue_is_stagnant(sh.QUEUE_STALE_DAYS + 1) is True


def test_terminal_statuses_exist_and_are_outside_the_ring():
    """The triage cycle must only ever move rows between proposed and queued,
    so a settled row is permanently out of it."""
    assert set(sh.TERMINAL_STATUSES) == {"shipped", "rejected"}
    src = pathlib.Path(sh.__file__).read_text()
    cycle = re.search(r"WITH top AS \(SELECT id FROM brain_enhancement_proposals.*?SELECT \(SELECT COUNT\(\*\) FROM up\)",
                      src, re.S)
    assert cycle, "triage cycle query not found — the extractor lost its target"
    body = cycle.group(0)
    assert "status IN ('proposed','queued')" in body, (
        "the promote candidate set must stay restricted to the two ring "
        "statuses, or a settled proposal gets pulled back in")
    for terminal in sh.TERMINAL_STATUSES:
        assert f"'{terminal}'" not in body, (
            f"the triage cycle references {terminal!r} — a terminal status "
            "must be untouchable by the ring")


def test_settle_endpoint_is_registered_and_gated():
    src = pathlib.Path(sh.__file__).read_text()
    assert "/api/v1/admin/brain/proposals/<int:pid>/settle" in src
    i = src.index("def settle_proposal")
    body = src[i:]
    assert "_admin_ok()" in body, "settle endpoint is not admin-gated"
    assert "c.commit()" in body, (
        "settle writes without committing — _conn() is not autocommit, so the "
        "write would ride on whatever transaction commits next")
    assert "AND status NOT IN %s" in body, (
        "settle must refuse to re-settle an already-terminal row")


def test_settle_rejects_a_non_terminal_status():
    src = pathlib.Path(sh.__file__).read_text()
    i = src.index("def settle_proposal")
    assert "if status not in TERMINAL_STATUSES:" in src[i:], (
        "settle must validate against TERMINAL_STATUSES, not accept free text")
