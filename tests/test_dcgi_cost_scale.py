"""The DCGI cost term must actually discriminate, and its unit must be true.

2026-08-30. Two defects, one fix.

UNIT. `_PRICE_FLOOR`/`_PRICE_CEIL` and three copies of the PUBLISHED
methodology called the price `$/Mcf`. It is not. eia_gas_prices_loader.py
divides the EIA $/Mcf series by 1.037 before storing, and all 24,310 rows carry
units='usd_per_mmbtu'. A published index that mislabels its own unit is wrong
in the way a reader cannot detect.

SCALE. The 2.5/12.0 linear scale was fitted to the INDUSTRIAL (PIN) series,
which carries the LDC distribution margin. Once dchub-backend#3397 made the
electric-power (PEU) series the basis and #3407 backfilled the nine states the
loader had been dropping, 15 of 50 states pegged an endpoint of a term worth
40% of the composite. Arizona at $1.05 and Ohio at $2.47 both scored exactly
100.0 — the term could not see a 2.3x price difference across precisely the
range a siting decision turns on.

The fence is a REAL distribution, not a hand-picked one: the PEU-preferred
latest price for all 50 states as measured on 2026-08-30. A calibration that
saturates this many real states is not a calibration.
"""
import ast
import math
import os

import pytest

DCGI_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "routes", "dcgi.py")

# PEU-preferred latest delivered price per state, $/MMBtu, measured 2026-08-30
# against production eia_gas_prices (the same DISTINCT ON the rollup runs).
PEU_PRICES = {
    "AK": 8.071, "AL": 3.124, "AR": 2.854, "AZ": 1.051, "CA": 2.546,
    "CO": 3.674, "CT": 4.031, "DE": 5.391, "FL": 4.745, "GA": 3.857,
    "HI": 51.842, "IA": 1.948, "ID": 2.314, "IL": 2.652, "IN": 3.365,
    "KS": 2.932, "KY": 4.021, "LA": 2.777, "MA": 2.507, "MD": 2.825,
    "ME": 11.929, "MI": 2.864, "MN": 3.925, "MO": 3.761, "MS": 3.317,
    "MT": 1.495, "NC": 5.458, "ND": 2.285, "NE": 3.269, "NH": 14.658,
    "NJ": 2.237, "NM": 2.758, "NV": 1.553, "NY": 2.382, "OH": 2.469,
    "OK": 4.291, "OR": 2.517, "PA": 2.247, "RI": 8.120, "SC": 3.713,
    "SD": 2.874, "TN": 3.317, "TX": 1.967, "UT": 2.141, "VA": 2.816,
    "VT": 4.937, "WA": 17.830, "WI": 3.626, "WV": 4.426, "WY": 2.845,
}
MAX_SATURATED_SHARE = 0.10   # the old scale sat at 0.30; log 1.0/20.0 sits at 0.02


def _consts():
    """FLOOR/CEIL read from source, so the test pins the shipped values."""
    out = {}
    for node in ast.parse(open(DCGI_SRC, encoding="utf-8").read()).body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") in ("_PRICE_FLOOR", "_PRICE_CEIL"):
                    out[t.id] = ast.literal_eval(node.value)
    return out


def _cost(price, floor, ceil):
    span = math.log(ceil) - math.log(floor)
    return max(0.0, min(100.0, (math.log(ceil) - math.log(price)) / span * 100.0))


def test_bounds_are_the_pinned_values():
    """Percentiles recomputed per query would move a state's score when OTHER
    states moved. These are pinned on purpose; changing them is a PR."""
    c = _consts()
    assert c == {"_PRICE_FLOOR": 1.0, "_PRICE_CEIL": 20.0}, (
        f"cost-scale bounds changed to {c}. That re-scores every state and "
        "breaks comparability with published history — do it deliberately, and "
        "update the derivation comment and the saturation numbers with it.")


def test_the_scale_is_log_not_linear():
    """A linear scale is what mis-fit the PEU distribution. Pin the shape."""
    src = open(DCGI_SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    rollup = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_gas_state_rollup")
    body = ast.get_source_segment(src, rollup) or ""
    cost_lines = [l for l in body.splitlines() if "_PRICE_CEIL" in l]
    assert cost_lines, "cost term no longer references _PRICE_CEIL — detector lost its anchor"
    assert any("log" in l for l in cost_lines), (
        "the cost term is no longer log-scaled. The PEU price distribution is "
        "log-normal (half the states inside a 1.6x band, tail to 49x); a linear "
        "scale over it saturated 30% of states.")


def test_the_shipped_calibration_does_not_saturate_real_states():
    """The regression, measured on the real distribution."""
    c = _consts()
    scores = {s: _cost(p, c["_PRICE_FLOOR"], c["_PRICE_CEIL"])
              for s, p in PEU_PRICES.items()}
    pegged = {s: v for s, v in scores.items() if v >= 99.95 or v <= 0.05}
    share = len(pegged) / len(scores)
    assert share <= MAX_SATURATED_SHARE, (
        f"{len(pegged)}/{len(scores)} states ({share:.0%}) peg an endpoint of a "
        f"term worth 40% of the composite: {sorted(pegged)}. "
        f"Budget is {MAX_SATURATED_SHARE:.0%}.")


def test_the_old_linear_calibration_would_fail_this_fence():
    """Proves the fence discriminates. A threshold no bad input trips is not
    a threshold — this pins that the SHIPPED defect is caught."""
    def linear(p):
        return max(0.0, min(100.0, (12.0 - p) / (12.0 - 2.5) * 100.0))
    pegged = [s for s, p in PEU_PRICES.items()
              if linear(p) >= 99.95 or linear(p) <= 0.05]
    share = len(pegged) / len(PEU_PRICES)
    assert share > MAX_SATURATED_SHARE, (
        "the old linear 2.5/12.0 scale no longer trips the saturation budget, "
        "so this fence would not have caught the defect it was written for")
    assert share == pytest.approx(0.30, abs=0.01), share


def test_cheaper_gas_never_scores_worse():
    c = _consts()
    ordered = sorted(PEU_PRICES.values())
    scores = [_cost(p, c["_PRICE_FLOOR"], c["_PRICE_CEIL"]) for p in ordered]
    assert scores == sorted(scores, reverse=True), "cost must be monotone in price"


def test_scores_stay_in_band_including_the_outliers():
    c = _consts()
    for s, p in PEU_PRICES.items():
        v = _cost(p, c["_PRICE_FLOOR"], c["_PRICE_CEIL"])
        assert 0.0 <= v <= 100.0, f"{s} scored {v}"
    # HI is an SNG-from-naphtha market at 51.84 and its only row is PIN.
    # Clamping it to 0 is the correct reading, not a lost datum.
    assert _cost(PEU_PRICES["HI"], c["_PRICE_FLOOR"], c["_PRICE_CEIL"]) == 0.0
    # No REAL state may score a perfect 100 — that is why FLOOR sits under the
    # observed minimum rather than on it.
    assert max(_cost(p, c["_PRICE_FLOOR"], c["_PRICE_CEIL"])
               for p in PEU_PRICES.values()) < 100.0


def test_the_2x_range_that_decides_siting_is_resolvable():
    """The concrete defect: AZ $1.05 and OH $2.47 both scored exactly 100.0."""
    c = _consts()
    az = _cost(PEU_PRICES["AZ"], c["_PRICE_FLOOR"], c["_PRICE_CEIL"])
    oh = _cost(PEU_PRICES["OH"], c["_PRICE_FLOOR"], c["_PRICE_CEIL"])
    assert az - oh >= 20.0, (
        f"AZ ({PEU_PRICES['AZ']}) and OH ({PEU_PRICES['OH']}) are 2.3x apart in "
        f"price but only {az - oh:.1f} points apart in cost score")


def test_published_methodology_does_not_mislabel_the_unit():
    """The stored value is $/MMBtu. Three published copies said $/Mcf."""
    src = open(DCGI_SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "$/Mcf" not in node.value:
                continue
            # ★ DATA vs EXPLANATION. A string that names BOTH units is prose
            #   explaining the correction, not a label applied to a number.
            #   A fence that cannot tell the two apart forces the correction
            #   record to be written around it — which is how a guard starts
            #   deciding what the product may say about itself.
            if "$/MMBtu" in node.value:
                continue
            offenders.append(node.value[:90])
    assert not offenders, (
        "a user-visible string still labels the gas price $/Mcf; the loader "
        "divides by 1.037 and stores $/MMBtu:\n  " + "\n  ".join(offenders))
