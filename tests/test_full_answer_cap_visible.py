"""tests/test_full_answer_cap_visible.py — the gate a free caller actually hits
is READ by the funnel.

Background (measured 2026-09-07): the mcp-server labels its deprivation branch
`status='trial_cap_exceeded'` on the mcp_call_log row and had written 39,344 of
them since 2026-08-06 (~8,400/week). Nothing read that column. The funnel
published `quota_wall.hits_month = 0` — a DIFFERENT gate, the monthly quota,
which has never fired in its lifetime — and the dashboard rendered it as
"Quota wall hits 0 · enforce ON", which reads as "nothing is being walled".

House rule: never import main. flask_mcp_endpoints is read as source.

Mutations that must turn this RED (recorded in the PR body):
  1. status literal changed / block reads a table other than mcp_call_log
  2. `full_answer_cap` no longer emitted, or loses a required key
  3. the sibling-status floor removed (a rename would then read as a silent 0)
  4. quota_wall loses its `see_instead` pointer
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SRC_PATH = os.path.join(ROOT, "flask_mcp_endpoints.py")


def _src() -> str:
    with open(SRC_PATH, encoding="utf-8") as fh:
        s = fh.read()
    assert len(s) > 500, "flask_mcp_endpoints.py read as %d bytes" % len(s)
    return s


def _block() -> str:
    """The full_answer_cap region: from its SQL up to the emitted dict's end.

    Anchored on the ASSIGNMENT, not on a bare substring — the status literal
    also appears in the surrounding comment, and asserting on the blob would
    pass with the query deleted.
    """
    s = _src()
    start = s.find('out["full_answer_cap"] = {')
    assert start != -1, 'funnel no longer emits out["full_answer_cap"]'
    end = s.find('out["full_answer_cap_error"]', start)
    assert end != -1, "full_answer_cap block lost its error branch"
    # Walk back to the try: that opens the block so the SQL is in scope.
    open_try = s.rfind("            try:", 0, start)
    assert open_try != -1
    return s[open_try:end]


# The block issues three queries: the headline rollup, the by_week trend and
# the sibling-status floor. EVERY one of them must read mcp_call_log, and the
# two that filter on the gate must use the literal the mcp-server writes.
#
# ★ These are COUNT assertions, not `in` assertions, and that is the whole
# point: with three queries in the block, `"FROM mcp_call_log" in b` still
# passes after one of them has been repointed at another table. The first cut
# of this test did exactly that and survived both mutations M1 and M5.
#
# The counts deliberately include the `basis` string, which quotes the same
# table and predicate the SQL runs. That makes the published basis and the
# executed query ONE fact: change either alone and the count moves.
_EXPECT_CALL_LOG_READS = 4  # 3 executed queries + the published `basis` string
_EXPECT_GATE_FILTERS = 3  # headline SQL + by_week SQL + the published `basis`


def test_every_query_in_the_block_reads_the_call_log():
    b = _block()
    n = b.count("FROM mcp_call_log")
    assert n == _EXPECT_CALL_LOG_READS, (
        "expected %d reads of mcp_call_log in the full_answer_cap block, found "
        "%d — a query repointed at another table cannot see the gate it claims "
        "to measure, and the remaining reads would hide it"
        % (_EXPECT_CALL_LOG_READS, n)
    )


def test_every_gate_filter_uses_the_literal_the_server_writes():
    b = _block()
    n = b.count("status = 'trial_cap_exceeded'")
    assert n == _EXPECT_GATE_FILTERS, (
        "expected %d filters on status = 'trial_cap_exceeded' (headline + "
        "by_week), found %d — the mcp-server writes that literal in server.mjs; "
        "a filter that drifts off it silently counts nothing"
        % (_EXPECT_GATE_FILTERS, n)
    )


def test_status_literal_is_published_so_a_rename_is_detectable():
    """The literal is emitted as data, not only buried in SQL."""
    b = _block()
    assert '"status_literal": "trial_cap_exceeded"' in b, (
        "status_literal missing — a consumer cannot tell which label produced "
        "these counts, so an mcp-server rename reads as the wall going quiet"
    )


def test_required_keys_are_emitted():
    b = _block()
    for key in ("hits_7d", "hits_30d", "sessions_7d", "sessions_30d",
                "keys_30d", "first_hit_at", "last_hit_at", "by_week",
                "measures", "basis", "not_measured_here"):
        assert '"%s"' % key in b, "full_answer_cap lost required key %r" % key


def test_sibling_status_floor_exists():
    """A scan that can find nothing needs a floor.

    If the mcp-server renames the status, hits_* go to 0. Without the sibling
    rollup that 0 is indistinguishable from "the wall stopped firing".
    """
    b = _block()
    assert '"sibling_gate_statuses_30d"' in b, (
        "sibling_gate_statuses_30d removed — headline 0 becomes unexplainable"
    )
    assert "GROUP BY 1 ORDER BY n DESC" in b, (
        "the sibling rollup no longer aggregates by status"
    )


def test_quota_wall_points_at_the_gate_that_actually_fires():
    """quota_wall must carry the pointer, in the payload, not just a comment."""
    s = _src()
    assert s.count('out["quota_wall"]["see_instead"]') == 1, (
        "quota_wall lost its see_instead pointer (or gained a duplicate) — a "
        "reader landing on the monthly-quota block gets no route to the gate "
        "that actually fires"
    )
    m = re.search(r'out\["quota_wall"\]\["see_instead"\] = \(\s*\n\s*"([^"]+)"', s)
    assert m and "full_answer_cap" in m.group(1), (
        "see_instead no longer names full_answer_cap"
    )
