"""The one-pager's arithmetic must foot — 2026-09-08.

THE ONE SENTENCE
----------------
These tests FAIL if a client-facing valuation PDF prints "$X/MW × N MW" beside
a total that is not X × N.

WHAT WENT WRONG (measured on a PDF that reached a client, 2026-09-08)
---------------------------------------------------------------------
Engine v2.3 made `site_value_usd_mid` a PV-weighted figure — MW that energize
years out are discounted. The two PDF builders (`dcOnePager`, `dcTwoTier`) were
NOT updated, and kept printing the pre-v2.3 identity `$/mw_mid × target_mw`.
With a delivery schedule those are different numbers. A two-tier sheet went out
reading:

    Tier A   "$337,155/MW × 1800 MW"   ->  $606.9M
             stated midpoint            ->  $451.3M   (+ $36.1M abatement)
    Tier B   "$631,058/MW × 1800 MW"   ->  $1,135.9M
             stated midpoint            ->  $844.7M   (+ $67.6M abatement)

Both tiers carried the same unnamed 0.7436 factor. Every figure was individually
correct; the page simply never said the delivery discount was in there, so the
multiplication a reader can do in their head disagreed with the total.

Two related mislabels fixed alongside:
  - "Time to full 1800 MW: 24 mo" read the GRID SCENARIO's time-to-power, which
    v2.3 sets from the schedule's FIRST MW. A six-year ramp was labelled as a
    two-year one. It now reads months_to_full_mw when a schedule exists.
  - `dcTitle` fell back to the NEAREST MARKET when no site label was entered, so
    a Plains Township, PA campus was titled "Powered-Land Site — Bethlehem, PA"
    — a different metro 45 miles away. It now falls back to coordinates.

WHY EXECUTION AND NOT A STRING MATCH
------------------------------------
Following tests/test_worker_og_body_survives_edge_cache.py: the shipped
functions are EXTRACTED from _PAGE_HTML and EXECUTED in node against a response
shaped like the live API's. A substring assertion on the template would have
passed throughout the outage — the template was syntactically fine and said
exactly what it always said.
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from routes.site_valuation_engine import _PAGE_HTML  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not available")


def _fn(name):
    """Extract one top-level `function name(...){...}` from the shipped page."""
    src = re.search(r"<script[^>]*>(.*?)</script>", _PAGE_HTML, re.S).group(1)
    i = src.index(f"function {name}(")
    depth, j, started = 0, i, False
    while j < len(src):
        if src[j] == "{":
            depth += 1; started = True
        elif src[j] == "}":
            depth -= 1
            if started and depth == 0:
                return src[i:j + 1]
        j += 1
    raise AssertionError(f"could not extract {name}")


HELPERS = """
function dcD0(n){ return '$' + Math.round(Number(n)||0).toLocaleString('en-US'); }
function dcMoney(n){ return '$' + ((Number(n)||0)/1e6).toFixed(1) + 'M'; }
"""


def _run(js):
    r = subprocess.run(["node", "-e", HELPERS + js],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


# A response shaped like the live API's, with the schedule that shipped.
PER_MW = 337155.0
MW = 1800
PV = 0.7436


def _valuation(pv=PV, applied=True):
    return {
        "$/mw_mid": PER_MW,
        "site_value_usd_mid": PER_MW * MW * pv * 1.08,
        "power_delivery": {
            "applied": applied, "pv_factor": pv, "nameplate_mw": MW,
            "pv_equivalent_mw": round(MW * pv, 1),
            "months_to_first_mw": 18.8, "months_to_full_mw": 88.0,
        },
    }


def test_printed_per_mw_line_multiplies_out_to_the_stated_total():
    """THE fence. Whatever the line says, doing that multiplication must land
    on the total printed beside it (before the abatement line)."""
    js = _fn("dcPerMwLine") + f"""
    const v = {json.dumps(_valuation())};
    console.log(dcPerMwLine(v, {MW}));"""
    line = _run(js)
    nums = [float(x.replace(",", "")) for x in
            re.findall(r"[\d,]+\.?\d*", line.replace("$", ""))]
    product = 1.0
    for n in nums:
        product *= n
    expected = PER_MW * MW * PV
    assert product == pytest.approx(expected, rel=1e-3), (
        f"line {line!r} multiplies to {product:,.0f}, "
        f"but the MW contribution is {expected:,.0f}")


def test_line_names_the_delivery_factor_when_one_applies():
    js = _fn("dcPerMwLine") + f"""
    console.log(dcPerMwLine({json.dumps(_valuation())}, {MW}));"""
    assert "delivery PV" in _run(js)


def test_line_is_unchanged_when_no_schedule_applies():
    """A caller with no schedule must get the plain, correct identity."""
    js = _fn("dcPerMwLine") + f"""
    console.log(dcPerMwLine({json.dumps(_valuation(pv=1.0, applied=False))}, {MW}));"""
    out = _run(js)
    assert "delivery PV" not in out
    assert out == f"$337,155/MW × {MW} MW"


def test_pv_factor_of_one_does_not_add_a_meaningless_multiplier():
    js = _fn("dcPerMwLine") + f"""
    console.log(dcPerMwLine({json.dumps(_valuation(pv=1.0, applied=True))}, {MW}));"""
    assert "delivery PV" not in _run(js)


# ── time-to-full ──────────────────────────────────────────────────

def test_time_to_full_reports_the_last_mw_not_the_first():
    """The shipped sheet said 'Time to full 1800 MW: 24 mo' for a ramp whose
    last MW lands at month 88, because it read the grid scenario's
    time-to-power — which v2.3 sets from the FIRST MW."""
    js = _fn("dcTimeToFull") + f"""
    const r = dcTimeToFull({json.dumps(_valuation())},
                           {{grid_only:{{time_to_power_months:24}}}}, 'grid_only');
    console.log(JSON.stringify(r));"""
    r = json.loads(_run(js))
    assert r["mo"] == 88, r
    assert r["phased"] is True


def test_time_to_power_falls_back_to_the_scenario_without_a_schedule():
    js = _fn("dcTimeToFull") + f"""
    const r = dcTimeToFull({json.dumps(_valuation(pv=1.0, applied=False))},
                           {{grid_only:{{time_to_power_months:24}}}}, 'grid_only');
    console.log(JSON.stringify(r));"""
    r = json.loads(_run(js))
    assert r["mo"] == 24 and r["phased"] is False


# ── title ─────────────────────────────────────────────────────────

def test_untitled_sheet_does_not_name_the_nearest_market_as_the_site():
    """Plains Township, PA snaps to the Bethlehem market 45 miles away. The
    fallback title must not put that city on a client deliverable."""
    js = _fn("dcTitle").replace(
        "var el = document.getElementById('site_label');", "var el = null;") + """
    console.log(dcTitle({market_context:{nearest_market_slug:'bethlehem',
                                         site_state:'PA'},
                         input:{lat:41.2662, lon:-75.7621}}));"""
    out = _run(js)
    assert "ethlehem" not in out, out
    assert "41.2662" in out and "-75.7621" in out and "PA" in out
