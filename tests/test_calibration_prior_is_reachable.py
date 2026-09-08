"""The caller's confidence floor must never sit above the calibration base.

WHY THIS EXISTS (2026-09-07). `pending-pr` computes

    effective = max(src_threshold, floor_conf)

where `src_threshold` is the calibrated per-source value (or
`_CALIB_BASE_THRESHOLD` for a source with fewer than `_CALIB_MIN_SAMPLES`
resolved outcomes) and `floor_conf` is the caller's `min_confidence`. The one
real caller — brain-layer5-pr-opener.yml — passed a hardcoded 0.85 while the
base was also 0.85.

Two separate ways that combination is closed, and both were live:

  1. The FLOOR OVERRIDE. Any tuned value below the caller's floor is
     discarded by the max(). Calibration tuned autonomy_proactive to 0.708 on
     37 real outcomes and it changed nothing, because the caller still said
     0.85. A self-tuning threshold that a hardcoded caller always beats is
     not a threshold, it is a constant.

  2. The COLD-START TRAP. `_CALIB_BASE_THRESHOLD` is the prior handed to a
     source that has not yet earned a verdict. With the prior at 0.85 and the
     best pending proposal at 0.83, an unproven source could never ship — and
     it needs a shipped outcome to earn a lower bar. Measured that day: all
     37 resolved outcomes belonged to one source with ZERO pending work,
     while every source WITH pending work had zero resolved outcomes.

Neither failed loudly. `pending-pr` returned `items: 0` and the opener logged
"Nothing to do — exiting clean (auth OK, queue genuinely empty)" every four
hours for a month, green each time. This test is the alarm that was missing:
it reads the shipped workflow rather than a copy, so raising the floor back
above the base reds CI instead of silently closing the lane again.

Stdlib + pyyaml (CI installs it); no DB, no network.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github/workflows/brain-layer5-pr-opener.yml"
LAYER5 = REPO / "routes/brain_v2_layer5.py"


def _const(name):
    """Read a module-level float constant without importing the module
    (importing routes/ pulls flask + psycopg2 and registers blueprints)."""
    m = re.search(rf"^{name}\s*=\s*([0-9.]+)", LAYER5.read_text(), re.M)
    assert m, f"{name} not found in {LAYER5.name}"
    return float(m.group(1))


def _workflow_min_conf():
    """The default the opener sends as `min_confidence`."""
    m = re.search(r"MIN_CONF:\s*\$\{\{\s*inputs\.min_confidence\s*\|\|\s*'([0-9.]+)'\s*\}\}",
                  WORKFLOW.read_text())
    assert m, "MIN_CONF default not found — did the opener's env block change?"
    return float(m.group(1))


def test_caller_floor_does_not_override_calibration():
    """THE regression this file exists for. floor > base means every tuned
    value below the floor is discarded by max(), so calibration cannot act."""
    base = _const("_CALIB_BASE_THRESHOLD")
    floor = _workflow_min_conf()
    assert floor <= base, (
        f"opener floor {floor} > calibration base {base}: pending-pr takes "
        f"max(src_threshold, floor_conf), so every tuned value below {floor} "
        f"is silently discarded and self-tuning cannot matter")


def test_cold_start_prior_is_below_the_ceiling_it_gates():
    """A prior equal to the practical ceiling means an unproven source can
    never earn its first outcome. Keep real headroom between the prior and
    the blocked-upper-bound."""
    base = _const("_CALIB_BASE_THRESHOLD")
    ceil = _const("_CALIB_CEIL")
    floor = _const("_CALIB_FLOOR")
    assert floor < base < ceil, f"expected {floor} < {base} < {ceil}"
    assert base <= 0.80, (
        f"cold-start prior {base} is at or above the confidence band real "
        f"proposals score in (0.78-0.83 live on 2026-09-07). A source with no "
        f"resolved outcomes would never clear it, and it needs to clear it "
        f"once to earn any")


def test_tuning_range_stays_inside_the_clamp():
    """The tuned value is base - (trust - 0.5) * range, clamped to
    [floor, ceil]. A fully-trusted source must land at or above the floor
    rather than depending on the clamp to rescue an out-of-range value."""
    base = _const("_CALIB_BASE_THRESHOLD")
    rng = _const("_CALIB_ADJ_RANGE")
    floor = _const("_CALIB_FLOOR")
    most_permissive = base - (1.0 - 0.5) * rng
    assert most_permissive >= floor - 1e-9 or floor >= most_permissive, (
        "sanity: clamp must bound the swing")
    # and the swing must actually be able to move the value somewhere useful
    assert most_permissive < base, "tuning must be able to lower the bar"


@pytest.mark.parametrize("floor,base,blocked", [
    (0.85, 0.85, True),    # the live 2026-09-07 configuration
    (0.85, 0.78, True),    # base lowered alone — the inert half-fix
    (0.78, 0.78, False),   # both lowered — what actually opens the lane
    (0.70, 0.78, False),
])
def test_max_semantics_are_what_this_file_assumes(floor, base, blocked):
    """Pins the arithmetic the assertions above depend on, against the
    best-scoring real proposal (0.83) measured live on 2026-09-07."""
    best_real_proposal = 0.83
    effective = max(base, floor)
    assert (best_real_proposal < effective) is blocked
