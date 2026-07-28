"""Funnel leakage stage 4 (2026-07-27) — a dead metric must not read as zero.

/api/v1/admin/funnel/leakage counted mcp_pair_codes.redeemed_at IS NOT NULL and
published "4_codes_redeemed: 0, drop_codes_to_redeemed_pct: 100.0" on the admin
board. That column has 0 rows EVER — issue #1551 established it on 2026-07-13
and rewired brain_consistency_radar to the canonical mcp_conversions ledger, but
this endpoint was never rewired. Meanwhile 9 non-test conversions landed in the
ledger in the trailing 30 days.

A dead metric reported as a measured zero is worse than no metric: it reads as a
total conversion failure and sends people to fix a funnel that isn't broken
there. These tests pin the ledger as the source and pin UNMEASURED != 0.

CI-SAFETY: source-level assertions, no network, no DB.
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _code_only(text):
    """Strip comment lines. An earlier test in this repo failed on the very
    comment documenting its own fix; assert against code, not prose."""
    return "\n".join(l for l in text.split("\n") if not l.lstrip().startswith("#"))


@pytest.fixture(scope="module")
def leak_block():
    src = _read(os.path.join("routes", "schema_repair.py"))
    i = src.index("# Stage 4: real conversions")
    return _code_only(src[i:src.index("return jsonify(ok=True, **out)", i)])


# ── the bug: counting a column that has never had a row ──────────────

def test_stage4_no_longer_reads_the_dead_pair_code_column(leak_block):
    assert "redeemed_at IS NOT NULL" not in leak_block
    assert "mcp_pair_codes" not in leak_block


def test_stage4_reads_the_canonical_ledger(leak_block):
    assert "FROM mcp_conversions" in leak_block


def test_stage4_excludes_test_conversions(leak_block):
    """The radar's query filters is_test; a leakage board that counted test
    rows would overstate in the opposite direction."""
    assert "COALESCE(is_test, FALSE) = FALSE" in leak_block


# ── unmeasured must never be zero ────────────────────────────────────

def test_missing_ledger_is_none_not_zero(leak_block):
    """If mcp_conversions is absent the stage is UNMEASURED. Reporting 0 would
    recreate the exact bug — an unreadable stage rendering as total failure."""
    assert 'out["stages"]["4_conversions"] = None' in leak_block
    assert "to_regclass('public.mcp_conversions')" in leak_block


def test_drop_rate_is_suppressed_when_unmeasured(leak_block):
    """A suppressed rate says 'we could not check'. A 100% drop says
    'everybody left'. They must not be the same output."""
    assert "_cr is not None" in leak_block


def test_stage4_source_is_declared(leak_block):
    """The board should say which source produced the number, so a future
    reader can tell a real zero from an unreadable one without reading code."""
    assert 'out["stage_4_source"]' in leak_block


def test_no_stale_key_names_left_behind():
    """The renamed keys must not coexist with the old ones — two stage-4 names
    on one board is how a corrected metric gets read off the wrong row."""
    src = _code_only(_read(os.path.join("routes", "schema_repair.py")))
    assert '"4_codes_redeemed"' not in src
    assert '"drop_codes_to_redeemed_pct"' not in src
