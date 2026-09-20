"""Funnel leakage stages 1-3 (2026-09-20) — a demand metric must not count us.

/api/v1/admin/funnel/leakage published "2_paywall_signals: 2051" and
"drop_signals_to_codes_pct: 99.61" off `COUNT(*) FROM mcp_upgrade_signals` — the
RAW table. The canonical view built earlier in the same file carries the
instruction "every reader queries this, not the raw table", and the
r-self-traffic note on it records what the filter removes: 318 callers / 4,589
signals in 30d, ZERO of which ever converted or bound an email.

So DC Hub's own probes, QA sweeps and monitors were counted as unconverted
demand. Two consequences, both load-bearing:

  * the published signal-to-code rate got WORSE every time the platform added a
    monitor, and
  * a 2026-09-19 handoff named that rate "the biggest item on this list",
    which is a priority set by our own traffic.

The agent-pay board measures the same 30 days WITH the real-platform predicate
and reports 15 gated real-agent payable-tool calls. Two instruments, two orders
of magnitude, one of them wrong.

This is tests/test_funnel_leakage_stage4.py's bug one stage earlier — right
question, wrong source — so these tests follow its shape: pin the source, and
pin UNMEASURED != the contaminated number.

CI-SAFETY: source-level assertions, no network, no DB.
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _code_only(text):
    """Strip comment lines. The fix's own comment block names the raw table in
    prose; asserting over comments would let the explanation satisfy the test."""
    return "\n".join(l for l in text.split("\n") if not l.lstrip().startswith("#"))


@pytest.fixture(scope="module")
def src():
    return _read(os.path.join("routes", "schema_repair.py"))


@pytest.fixture(scope="module")
def stage2(src):
    i = src.index("# Stage 2: paywall-hit signals")
    return _code_only(src[i:src.index("# Stage 3: pair codes minted", i)])


@pytest.fixture(scope="module")
def per_tool(src):
    i = src.index("# Per-tool drop-off")
    return _code_only(src[i:src.index('out["top_leak_tools"] =', i)])


# ── the bug: a demand metric counting our own traffic ────────────────

def test_stage2_no_longer_counts_the_raw_signals_table(stage2):
    """Pinned on the SQL, not the identifier: the else-branch message names the
    raw table on purpose, to say why it is not substituted."""
    assert "FROM mcp_upgrade_signals" not in stage2


def test_stage2_reads_the_real_caller_view(stage2):
    assert "FROM mcp_funnel_real" in stage2


def test_per_tool_leak_table_is_real_callers_only(per_tool):
    """top_leak_tools is what names the tool a fix gets aimed at. Ranked over
    self-traffic it points at whichever tool our own monitors call most."""
    assert "FROM mcp_upgrade_signals" not in per_tool
    assert "FROM mcp_funnel_real" in per_tool


# ── unmeasured must never fall back to the contaminated number ───────

def test_missing_view_is_none_not_the_raw_count(stage2):
    """A fallback to the raw table would reinstate the bug silently, on a path
    nobody watches. Absent view => UNMEASURED, and the rate suppresses itself."""
    assert "to_regclass('public.mcp_funnel_real')" in stage2
    assert 'out["stages"]["2_paywall_signals"] = None' in stage2


def test_stage2_source_is_declared(stage2):
    """Stage 4 already declares its source. A reader must be able to tell a real
    number from an unreadable one without reading code."""
    assert 'out["stage_2_source"]' in stage2


# ── the populations that could not be filtered must be named ─────────

def test_board_declares_which_stages_are_real_callers_only(src):
    """Only mcp_upgrade_signals has a canonical real-caller view. Stages 1, 3
    and 5 stay unfiltered, so the board says so per stage instead of letting a
    reader assume one population across five rows."""
    code = _code_only(src)
    assert 'out["stage_sources"]' in code
    assert '"real_callers_only": False' in code
    assert '"real_callers_only": True' in code


def test_rates_spanning_two_populations_are_named(src):
    """Every published drop rate now has a filtered side and an unfiltered side.
    Naming them is the difference between a caveat and a silent apples-to-
    oranges percentage on an admin board that sets priorities."""
    code = _code_only(src)
    assert 'out["mixed_population_rates"]' in code
    for rate in ("drop_calls_to_signals_pct", "drop_signals_to_codes_pct",
                 "drop_codes_to_conversions_pct", "drop_conversions_to_paid_pct"):
        assert rate in code


# ── a source block must not read a source that is not set yet ────────

def test_stage_sources_reads_a_set_stage_4_source(src):
    """be#4890 shipped stage_sources reading out.get("stage_4_source") while
    that key was assigned BELOW it, so the nested source was always None —
    verified live on the deployed board: top-level correct, nested None.

    The one row that could not say where its number came from was stage 4, the
    only other real-callers-only stage. Pinned by POSITION because that is the
    actual failure: a dict literal reading a key the same function sets later
    cannot be caught by asserting the key exists."""
    code = _code_only(src)
    assign = code.index('out["stage_4_source"] =')
    reader = code.index('out["stage_sources"]')
    assert assign < reader, (
        "out[\"stage_4_source\"] is assigned after the stage_sources block "
        "that reads it — the nested source will be None")


def test_every_declared_source_is_resolvable(src):
    """No stage may name its source with a bare out.get() of a key this
    function has not set by that point. Catches the same class for stage 2."""
    code = _code_only(src)
    block_start = code.index('out["stage_sources"]')
    block = code[block_start:code.index("}", code.index("top_leak_tools", block_start))]
    for key in ("stage_2_source", "stage_4_source"):
        if f'out.get("{key}")' in block:
            assert code.index(f'out["{key}"] =') < block_start, (
                f"stage_sources reads {key} before it is assigned")
