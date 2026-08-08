"""r-null-not-zero + r-one-dcpi (2026-08-08) — the market page must not lie.

TWO defects, both measured live on /dcpi/tokyo and /dcpi/johor before this fix.

1. NULLS RENDERED AS MEASURED ZEROS. The page printed "0 MW Stranded Capacity"
   and "0 MW Generation Additions" while /api/v1/dcpi/scores/<slug> returned
   null for both. The renderer was `{{ (s.stranded_capacity_mw or 0)|round(0)|int }} MW`
   — `or 0` turns an absent measurement into a confident zero, which a reader
   takes as "we looked and there is none".

2. TWO NUMBERS BOTH CALLED "DCPI". The <title> rendered excess_power_score
   under the bare label "DCPI" while the API returned a different
   composite_score for the same market, and the composite appeared nowhere on
   the page. Measured across every market; the US flagships were worst:

       dallas    title "DCPI 65.8"  vs composite 43.6   (22.2 apart)
       phoenix   title "DCPI 62.5"  vs composite 42.7   (19.8)
       ashburn   title "DCPI 45.5"  vs composite 27.1   (18.4)
       tokyo     title "DCPI 19.8"  vs composite 14.4

   An analyst quoting the page and an agent quoting the API were both citing
   DC Hub correctly, and disagreeing.

These tests RENDER the shipped Jinja template against a null-heavy market
rather than grepping for the fix, because grepping would only prove the string
changed — not that the page a reader receives is right. Per the house rule,
nothing here imports main.py: the template is pulled out of source with `ast`.
"""
import ast
import pathlib
import re

import pytest

jinja2 = pytest.importorskip("jinja2")

SRC = pathlib.Path(__file__).resolve().parent.parent / "routes" / "dcpi.py"

# A market shaped exactly like the live Tokyo row: signal_tier low, with the
# three fields that are genuinely null in production left null.
TOKYO = {
    "market_name": "Tokyo", "state": "JP", "iso": "TEPCO", "verdict": "AVOID",
    "excess_power_score": 19.8, "constraint_score": 66.5,
    "time_to_power_months": 39.9, "composite_score": 14.4,
    "queue_wait_months": None, "reserve_margin_pct": 12.0,
    "gen_additions_12mo_mw": None, "curtailment_pct": 2.0,
    "stranded_capacity_mw": None, "queue_capacity_mw": None,
    "latitude": 35.68, "longitude": 139.69,
    "computed_at": "2026-08-08T03:30:00Z", "signal_tier": "low",
    "signal_tier_basis": "live_adapter_count_at_score_time",
    "data_basis": "modeled_estimate", "emergency_count_30d": 0,
    "method_version": "2.0.1", "iso_type": None,
}

NULLABLE_METRICS = ("queue_wait_months", "gen_additions_12mo_mw",
                    "stranded_capacity_mw", "time_to_power_months",
                    "reserve_margin_pct", "curtailment_pct")


@pytest.fixture(scope="module")
def template():
    for node in ast.walk(ast.parse(SRC.read_text(encoding="utf-8"))):
        if (isinstance(node, ast.Assign)
                and getattr(node.targets[0], "id", "") == "DCPI_MARKET_TEMPLATE"):
            return jinja2.Environment().from_string(ast.literal_eval(node.value))
    raise AssertionError("DCPI_MARKET_TEMPLATE not found in routes/dcpi.py")


def _render(template, s, gated=False):
    return template.render(s=s, risks=[], opps=[], gated=gated, narrative="n",
                           place_label="Tokyo, JP", facilities_html="")


def _text(html):
    return re.sub(r"<[^>]+>", " ", html)


def test_a_null_metric_never_renders_as_zero(template):
    """The whole defect: absence must not look like a measurement."""
    html = _render(template, TOKYO)
    txt = _text(html)
    assert "0 MW" not in txt, (
        "a null metric rendered as '0 MW' — that is a measured zero to a reader")
    assert "not measured" in txt, (
        "null metrics must render an explicit not-measured state")


def test_every_nullable_metric_survives_being_null(template):
    """Null each one in turn. Any that renders a number is fabricating."""
    offenders = []
    for field in NULLABLE_METRICS:
        txt = _text(_render(template, {**TOKYO, field: None}))
        # the tile for this field must not carry a bare numeric value
        if re.search(r"\b0 (?:MW|mo)\b", txt) or re.search(r"\b0\.0%", txt):
            offenders.append(field)
    assert not offenders, (
        "these fields render a zero when null: " + ", ".join(offenders))


def test_real_values_still_render_with_their_units(template):
    """The fix must not blank out genuine measurements."""
    txt = _text(_render(template, TOKYO))
    assert "12.0%" in txt, "reserve_margin_pct 12.0 should render as 12.0%"
    assert "2.0%" in txt, "curtailment_pct 2.0 should render as 2.0%"
    assert "40 mo" in txt, "time_to_power_months 39.9 should render as 40 mo"


def test_the_title_publishes_the_same_number_the_api_calls_dcpi(template):
    """<title> must carry composite_score, not excess_power_score.

    composite_score is the value every ranking endpoint sorts on, so it is the
    one entitled to the name.
    """
    html = _render(template, TOKYO)
    title = re.search(r"<title>(.*?)</title>", html).group(1)
    assert "DCPI 14.4" in title, f"title should carry composite_score: {title!r}"
    assert "DCPI 19.8" not in title, (
        f"title is still branding excess_power_score as DCPI: {title!r}")


def test_both_numbers_appear_so_nothing_is_hidden(template):
    """Fixing the name must not hide the component score."""
    txt = _text(_render(template, TOKYO))
    assert "19.8" in txt, "excess_power_score must still be published"
    assert "14.4" in txt, "composite_score must appear on the page"
    assert re.search(r"Excess[- ]power score", txt, re.I), (
        "the excess score must be labelled as such, not left bare")


def test_gated_render_does_not_crash_or_leak_a_number(template):
    """Anonymous visitors get the verdict, no numerics — and no zeros."""
    txt = _text(_render(template, {**TOKYO, "composite_score": None}, gated=True))
    assert "0 MW" not in txt
    assert "14.4" not in txt and "19.8" not in txt, "gated page leaked a score"
