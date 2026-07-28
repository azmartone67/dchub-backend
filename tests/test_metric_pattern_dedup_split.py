"""_METRIC_PATTERNS stops doing two jobs — 2026-07-28.

Membership of that list used to mean BOTH "this counts as a headline stat"
(quality score) AND "two posts sharing this label are the same story" (entity
dedup). Two unrelated jobs wanting opposite breadth: score wants every real
measurement, dedup wants only labels where collapsing is right.

The cost was visible in the code — SIX of thirteen labels had to be opted back
out via a separate _NO_METRIC_DEDUP set, i.e. added for score credit and then
excluded from the dedup they had silently joined. And the default was
BACKWARDS: a pattern added for scoring started collapsing posts unless someone
remembered an opt-out set two screens away. "7 of 7 ISOs" would have suppressed
"5 of 6 markets" for the whole 5-day window.

Now each pattern is (label, regex, DEDUP_MODE) and declares its own behaviour
where it is written. _NO_METRIC_DEDUP and _VALUE_DEDUP_LABELS are gone; the
dedup reads sig["dedup_label"], which is populated ONLY for patterns that
declared DEDUP_LABEL/DEDUP_VALUE.

The refactor was verified behaviour-identical over a 729-pair cross-product
(26 collapsing pairs, 0 differences) before landing. These tests lock the
invariants that made that true.

Pure functions only; never imports main.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

cp = pytest.importorskip("content_publisher")  # noqa: E402

LINK = " https://dchub.cloud/dcpi"

# The dedup behaviour every label must have. This is the frozen contract — if a
# label moves between these groups, posting behaviour changes and the move must
# be deliberate.
DEDUPS_ON_LABEL = {"mcp_tool_calls", "mcp_requests", "coverage_added",
                   "unique_callers"}
DEDUPS_ON_VALUE = {"gw_figure", "usd_billion", "mw_figure"}
SCORE_ONLY = {"dcpi_score", "facilities_count", "markets_count",
              "countries_count", "tools_count", "coverage_ratio"}


# ── the structural invariants ──────────────────────────────────────────────

def test_the_optout_sets_are_gone():
    """Their existence WAS the bug — six labels opting out of a default they
    should never have had."""
    assert not hasattr(cp, "_NO_METRIC_DEDUP")
    assert not hasattr(cp, "_VALUE_DEDUP_LABELS")


def test_every_pattern_declares_a_dedup_mode():
    """A pattern added without the third field must not silently default into
    deduping — that is the failure this refactor removes."""
    valid = (cp.DEDUP_NONE, cp.DEDUP_LABEL, cp.DEDUP_VALUE)
    for entry in cp._METRIC_PATTERNS:
        assert len(entry) == 3, "pattern is not (label, regex, dedup_mode): %r" % (entry,)
        label, _pat, mode = entry
        assert mode in valid, "%s declares an unknown dedup mode %r" % (label, mode)


def test_the_dedup_contract_is_unchanged():
    assert set(cp._METRIC_DEDUP_MODE) == DEDUPS_ON_LABEL | DEDUPS_ON_VALUE | SCORE_ONLY
    for lbl in DEDUPS_ON_LABEL:
        assert cp._METRIC_DEDUP_MODE[lbl] == cp.DEDUP_LABEL
    for lbl in DEDUPS_ON_VALUE:
        assert cp._METRIC_DEDUP_MODE[lbl] == cp.DEDUP_VALUE
    for lbl in SCORE_ONLY:
        assert cp._METRIC_DEDUP_MODE[lbl] is cp.DEDUP_NONE


# ── the separation, observed through the signature ─────────────────────────

@pytest.mark.parametrize("text,label", [
    ("Cheyenne hit DCPI score 69.5 (BUILD) today." + LINK, "dcpi_score"),
    ("DC Hub tracks 12,650 facilities." + LINK, "facilities_count"),
    ("DC Hub covers 311 scored markets." + LINK, "markets_count"),
    ("DC Hub covers 170+ countries." + LINK, "countries_count"),
    ("DC Hub exposes 79 live agent tools." + LINK, "tools_count"),
    ("7 of 7 US ISOs report live demand." + LINK, "coverage_ratio"),
])
def test_score_only_metrics_lift_the_score_but_never_dedup(text, label):
    """The whole point: these are recognised as quantities (metric_label set,
    so the stat credit applies) yet carry NO dedup identity."""
    sig = cp._post_headline_signature(text)
    assert sig["metric_label"] == label
    assert sig["metric_value"] is not None
    assert sig["dedup_label"] is None, (
        "%s can collapse posts — it must be score-only" % label)
    assert sig["dedup_mode"] is None


@pytest.mark.parametrize("text,label", [
    ("DC Hub MCP served 142,318 AI tool calls in the last 24h." + LINK,
     "mcp_tool_calls"),
    ("There were 4,000 MCP requests today." + LINK, "mcp_requests"),
    ("We added 41 markets in the last 7 days." + LINK, "coverage_added"),
    ("3,100 unique AI callers hit the API today." + LINK, "unique_callers"),
    ("ERCOT added 427 GW to its queue this week." + LINK, "gw_figure"),
    ("A $3.2B deal closed this week." + LINK, "usd_billion"),
    ("The largest feeder publishes 14.5 MW." + LINK, "mw_figure"),
])
def test_real_dedup_metrics_still_carry_their_identity(text, label):
    sig = cp._post_headline_signature(text)
    assert sig["metric_label"] == label
    assert sig["dedup_label"] == label
    assert sig["dedup_mode"] in (cp.DEDUP_LABEL, cp.DEDUP_VALUE)


# ── the behaviour those fields drive ───────────────────────────────────────

def _collapses(a, b):
    """The dedup decision, mirroring _should_skip_publish's entity block."""
    sa, sb = cp._post_headline_signature(a), cp._post_headline_signature(b)
    if sa.get("market_verdict") and sb.get("market_verdict") == sa.get("market_verdict"):
        return True
    lbl = sa.get("dedup_label")
    if lbl and sb.get("dedup_label") == lbl:
        if sa.get("dedup_mode") == cp.DEDUP_VALUE:
            x = float(sa.get("metric_value") or 0)
            y = float(sb.get("metric_value") or 0)
            return abs(x - y) <= max(abs(x), abs(y)) * 0.01
        return True
    return False


def test_two_coverage_posts_do_not_suppress_each_other():
    """The concrete hazard the old default created: one completeness statement
    locking out every other for the whole lookback window."""
    assert _collapses("7 of 7 US ISOs report live demand." + LINK,
                      "5 of 6 markets moved this week." + LINK) is False


def test_two_capability_posts_do_not_suppress_each_other():
    assert _collapses("DC Hub tracks 12,650 facilities." + LINK,
                      "DC Hub tracks 4,923 facilities." + LINK) is False


def test_same_gw_figure_still_collapses():
    """The "427 GW x 6 posts" repeat must still be caught."""
    assert _collapses("ERCOT added 427 GW to its queue this week." + LINK,
                      "MISO added 427 GW to its queue this week." + LINK) is True


def test_different_gw_figures_do_not_collapse():
    assert _collapses("ERCOT added 427 GW to its queue this week." + LINK,
                      "PJM added 171 GW to its queue this week." + LINK) is False


def test_same_label_metric_still_collapses():
    assert _collapses(
        "DC Hub MCP served 142,318 AI tool calls in the last 24h." + LINK,
        "DC Hub MCP served 98,000 AI tool calls in the last 24h." + LINK) is True


def test_market_verdict_dedup_is_untouched():
    assert _collapses("📍 Cheyenne · WECC · DCPI verdict: BUILD" + LINK,
                      "📍 Cheyenne · WECC · DCPI verdict: BUILD" + LINK) is True
    assert _collapses("📍 Cheyenne · WECC · DCPI verdict: BUILD" + LINK,
                      "📍 Ashburn · PJM · DCPI verdict: AVOID" + LINK) is False


# ── nothing else moved ─────────────────────────────────────────────────────

def test_zero_stat_hard_block_still_fires():
    sig = cp._post_headline_signature(
        "DC Hub MCP served 0 AI tool calls in the last 24h." + LINK)
    assert sig["zero_stat"] is True


def test_signature_is_fail_open_and_complete_on_garbage():
    for bad in ("", None, "no numbers at all"):
        sig = cp._post_headline_signature(bad)
        for k in ("market_verdict", "metric_label", "metric_value",
                  "zero_stat", "dedup_label", "dedup_mode"):
            assert k in sig, "%r missing from a fail-open signature" % k
