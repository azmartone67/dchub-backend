"""A "paywall signal" is not one thing (2026-09-20).

/api/v1/admin/funnel/leakage ranked top_leak_tools on COUNT(*) over EVERY
signal_type. The dominant one, `trial_preview`, is an ANSWER going back with an
upsell riding along — mcp_signal_canonical's own header measured it at 5,255 of
6,267 signals in 30d (84%). So the board ranked tools for ANSWERING people, and
get_market_intel sat 4th on a leak board for working correctly.

Worse, `checkout_link_issued` and `redeem_url_viewed` happen AFTER a code
exists, so counting them in stage 2 and dividing by stage 3 divided the funnel
against itself.

These tests pin the taxonomy, the ranking key, and the unclassified bucket.

CI-SAFETY: pure imports + source-level assertions, no network, no DB.
"""
import os
import re

import pytest

from mcp_signal_canonical import SIGNAL_CLASS, SIGNAL_CLASSES, signal_class

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _code_only(text):
    return "\n".join(l for l in text.split("\n") if not l.lstrip().startswith("#"))


# ── the taxonomy itself ──────────────────────────────────────────────

def test_an_answer_is_not_a_block():
    """The whole defect in one assertion."""
    assert signal_class("trial_preview") == "served"
    assert signal_class("paywall_hit") == "blocked"


def test_downstream_events_are_neither():
    """They happen after a code exists. Counting them in stage 2 and dividing
    by stage 3 divides the funnel against itself."""
    for t in ("checkout_link_issued", "redeem_url_viewed"):
        assert signal_class(t) == "downstream"


def test_every_denial_is_classified_blocked():
    for t in ("paywall_hit", "paid_tool_blocked", "trial_cap_exceeded",
              "monthly_quota_exhausted", "metered_grid_fiber_enforced"):
        assert signal_class(t) == "blocked", t


def test_unknown_is_unclassified_never_a_guess():
    """A signal_type added in dchub-mcp-server cannot update this map. Either
    default would corrupt the ranking, and the safe-looking one ('served')
    would hide a real wall."""
    for t in ("", None, "   ", "some_new_type_from_server_mjs"):
        assert signal_class(t) == "unclassified"
    assert "unclassified" in SIGNAL_CLASSES
    assert "unclassified" not in set(SIGNAL_CLASS.values())


# ── drift: this repo's own writers must be covered ───────────────────

def test_every_signal_type_this_repo_writes_is_classified():
    """Scans the backend's own sources for signal_type literals. Cannot see
    server.mjs — which is exactly why 'unclassified' exists."""
    # Scan EVERY python file in the repo root and routes/, not a hand-listed
    # few: `daily_limit_hit` was missed by an enumeration shaped for the other
    # writers' quoting, because it is passed as a kwarg rather than a dict key.
    rels = [f for f in os.listdir(ROOT) if f.endswith(".py")]
    rdir = os.path.join(ROOT, "routes")
    if os.path.isdir(rdir):
        rels += [os.path.join("routes", f) for f in os.listdir(rdir) if f.endswith(".py")]
    found = set()
    for rel in rels:
        try:
            src = _code_only(_read(rel))
        except (OSError, UnicodeDecodeError):
            continue
        found |= set(re.findall(r"signal_type\s*=\s*[\"']([a-z_]+)[\"']", src))
        found |= set(re.findall(r"[\"']signal_type[\"']\s*:\s*[\"']([a-z_]+)[\"']", src))
    found.discard("signal_type")
    assert len(found) >= 6, (
        f"scan found only {sorted(found)} — the regex or the file set has gone "
        "stale; this repo writes at least six distinct signal_types")
    missing = sorted(t for t in found if t not in SIGNAL_CLASS)
    assert not missing, f"unclassified signal_type(s) written by this repo: {missing}"


# ── the endpoint uses it ─────────────────────────────────────────────

@pytest.fixture(scope="module")
def leak_block():
    src = _read(os.path.join("routes", "schema_repair.py"))
    i = src.index("# Per-tool drop-off")
    return _code_only(src[i:src.index('out["signal_class_totals"]', i)])


def test_ranking_key_is_blocked_not_total(leak_block):
    """The ask was a ranking that means something. Ranking on the total ranks
    tools for answering people.

    ★ `ORDER BY signals DESC` legitimately REMAINS in the SQL: #4926 uses it to
    pick a deterministic window, and the published rank is applied in Python
    after classification. So assert the PUBLISHED ordering, not the absence of
    a SQL clause — an earlier version of this test asserted the latter and
    failed on a correct merge."""
    assert 'out["top_leak_tools"] = sorted(' in leak_block
    assert 'key=lambda r: (r["blocked"], r["signals"])' in leak_block


def test_window_is_wider_than_the_published_list(leak_block):
    """The top 15 by BLOCKED is not a subset of the top 15 by total, so the SQL
    window must exceed the slice or a genuinely blocked tool can fall off."""
    assert "LIMIT 50" in leak_block
    assert "[:15]" in leak_block


def test_it_groups_by_signal_type(leak_block):
    assert "GROUP BY tool_requested, signal_type" in leak_block


def test_taxonomy_is_imported_not_redeclared():
    """A second copy is how a reader and a writer come to disagree."""
    code = _code_only(_read(os.path.join("routes", "schema_repair.py")))
    assert "from mcp_signal_canonical import" in code
    assert "SIGNAL_CLASS = {" not in code


def test_unclassified_types_surface_on_the_board():
    code = _code_only(_read(os.path.join("routes", "schema_repair.py")))
    assert 'out["unclassified_signal_types"]' in code
    assert 'out["unclassified_warning"]' in code


def test_sessions_stays_a_true_distinct_count(leak_block):
    """#4926's `sessions` is COUNT(DISTINCT session_id) at TOOL grain. Adding
    signal_type to that GROUP BY would silently turn it into a sum over buckets
    that double-counts any session producing two types. The classes therefore
    come from a SECOND query, and `sessions` is never renamed to an estimate."""
    assert "COUNT(DISTINCT session_id)" in leak_block
    assert leak_block.count("cur.execute(") == 2, (
        "expected two queries: #4926's tool-grain row and the class query")
    assert "sessions_upper_bound" not in leak_block
    # the class query must be the one grouped by signal_type
    i = leak_block.index("GROUP BY tool_requested, signal_type")
    j = leak_block.index("COUNT(DISTINCT session_id)")
    assert j < i, "the distinct-session query must be the FIRST one"
