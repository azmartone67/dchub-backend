"""The claim probe must look BOTH ways. NO NETWORK.

Every honest-numbers fence in this repo bans over-claims and none looks the
other way, which is how two agent-facing surfaces under-sold DC Hub for weeks
with every guard green:

    mcp_facts.json  facilities 15,300+ against 19,500+   27% under, 30 days old
    ai-agents.json  news_articles 3,503 against 13,089   3.7x under, wrong table

These tests pin the DIRECTION. They exercise the probe's pure comparison — the
same function the live probe calls, not a copy of it — so a change that quietly
drops the under-claim branch fails here rather than at some future audit.
"""
import subprocess
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.agent_surface_claim_probe import (      # noqa: E402
    DEFAULT_TOLERANCE_PCT, compare,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(ROOT, "scripts", "agent_surface_claim_probe.py")


def test_the_mcp_facts_understatement_is_caught():
    """15,300 published against a 19,500 canon — the real 2026-08-30 state."""
    verdict, gap = compare(15300, 19500)
    assert verdict == "under"
    # ★ 21.5%, not the "27%" quoted in earlier notes. Both come from the same
    # two numbers and differ only in denominator: 4,200/19,500 = 21.5% BELOW
    # CANON, while 4,200/15,300 = 27.5% is how much canon EXCEEDS what was
    # published. This probe reports the first, because the question it answers
    # is "how far is this surface from the truth", and the truth is canon.
    assert 20 < gap < 25


def test_the_news_articles_understatement_is_caught():
    """3,503 against 13,089 — the front door counting the abandoned table."""
    verdict, gap = compare(3503, 13089)
    assert verdict == "under"
    assert gap > 70


def test_a_daily_generated_file_one_step_behind_is_not_a_violation():
    """mcp_facts.json is rebuilt daily and legitimately lags canon between runs.
    A fence that flags that is a fence nobody will leave switched on."""
    assert compare(19500, 19700)[0] == "ok"


def test_over_claims_are_still_caught():
    """The direction the older fences already cover must not regress."""
    assert compare(25000, 19700)[0] == "over"


def test_an_unreadable_figure_is_unmeasured_never_ok():
    """`could not check` has never been `clean` in this repo."""
    assert compare(None, 19700)[0] == "unmeasured"
    assert compare(19700, None)[0] == "unmeasured"


def test_the_tolerance_is_narrow_enough_to_be_worth_having():
    """A tolerance wide enough to swallow the defects it exists for is
    decoration. Both real gaps must sit far outside it."""
    assert DEFAULT_TOLERANCE_PCT < 10
    assert compare(15300, 19500)[1] > DEFAULT_TOLERANCE_PCT * 5


def test_the_probes_own_self_test_passes():
    """The script ships must-fail controls; run them in CI rather than trusting
    that someone ran them once by hand."""
    r = subprocess.run([sys.executable, PROBE, "--self-test"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
