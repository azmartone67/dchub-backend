"""Did the AGENT act on the continuation? — the funnel's eighth row.

The instrumentation table for this funnel had seven answerable rows and one
that was called unmeasurable: "did the agent surface the continuation at all?"
It IS measurable, because the continuation instruction is a tool call — an
agent that preserved it calls unlock_more_data / claim_free_key / bind_email in
the same session, after the gate.

These tests pin the properties that make the answer trustworthy rather than
merely present:

  1. a rate over a ZERO denominator is UNMEASURED, never 0%. This feature
     deployed the same day it was written, so "no tagged signals yet" and
     "agents ignore us" produce the same 0 and mean opposite things — the one
     confusion that would waste the most time downstream;
  2. rows written before the tagging existed land in their own `untagged`
     bucket, never folded into the control arm, which would silently move
     whichever rate they were folded into;
  3. the two arms are only compared when BOTH have a real denominator;
  4. ★ a session that made NO call after the gate is UNMEASURED for compliance,
     not 0%. A hosted connector that mints a session per call cannot contain a
     post-gate call at all, so pooling it in reports its session model as an
     agent's refusal;
  5. ★ the rate is split by client and its concentration is published. Measured
     2026-09-03: 45 external agents called tools in 7 days and one was 65.8% of
     the calls. A pooled rate over that is one caller wearing the plural.

Pure-function test: tests/ never imports Flask or the DB, which is why the
logic lives in its own import-free module rather than in the route handler.
"""
import pathlib
import re

import pytest

from continuation_compliance import (
    CONTINUATION_TOOLS, GENERIC_CLIENT, OTHER_CLIENTS, UNATTRIBUTED,
    _CLIENT_ROWS_MAX, _GENERIC_CLIENT_NAMES, parse_arm, parse_client,
    summarize_compliance,
)

REPO = pathlib.Path(__file__).resolve().parents[1]


def row(shown, client="claude", gated=0, continued=None, acted=0):
    """A query row. `continued` defaults to `gated` — i.e. every session had a
    turn after the gate — so a test that is not ABOUT opportunity reads the way
    it did before continued_sessions existed."""
    return (shown, client, gated, gated if continued is None else continued, acted)


# ── the arm label ────────────────────────────────────────────────────────
@pytest.mark.parametrize("shown,expected", [
    ("trial_preview:treatment",  "treatment"),
    ("trial_preview:control",    "control"),
    ("trial_preview:ineligible", "ineligible"),
    ("trial_preview:TREATMENT",  "treatment"),   # normalized upstream, belt and braces
    # ★ pre-randomization labels: their own bucket, never an arm
    ("trial_preview:quantified", "legacy_shape_assigned"),
    ("trial_preview:generic",    "legacy_shape_assigned"),
    ("trial_preview",            "untagged"),     # pre-tagging rows
    ("trial_preview:maybe",      "untagged"),     # an arm we never emit
    ("",                         "untagged"),
    (None,                       "untagged"),
    (17,                         "untagged"),
])
def test_arm_is_parsed_or_bucketed_as_untagged(shown, expected):
    assert parse_arm(shown) == expected


def test_untagged_rows_are_never_folded_into_the_control_arm():
    # ★ Pre-tagging history is large and its arm is unknowable. Counting it as
    # `generic` would move the control rate by an unknown amount in an unknown
    # direction, and the comparison would look fine while being wrong.
    out = summarize_compliance([row("trial_preview", gated=900, acted=3)])
    assert out["arms"]["untagged"]["gated_sessions"] == 900
    assert out["arms"]["control"]["state"] == "UNMEASURED"
    assert out["arms"]["treatment"]["state"] == "UNMEASURED"


# ── the client label ─────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("claude", "claude"), ("Claude", "claude"), ("  CLAUDE  ", "claude"),
    ("chain-hire", "chain-hire"),
    # ★ generic strings are a real cohort of UNNAMED clients, not an absence
    ("mcp", GENERIC_CLIENT), ("MCP ", GENERIC_CLIENT),
    ("mcp-client", GENERIC_CLIENT), ("client", GENERIC_CLIENT),
    ("Default", GENERIC_CLIENT),
    ("2b6d9f11-0a3c-4d55-9f2e-7c1a4b8e0d33", GENERIC_CLIENT),  # a session id
    # nothing at all is a different thing again
    ("", UNATTRIBUTED), ("   ", UNATTRIBUTED), (None, UNATTRIBUTED),
    (17, UNATTRIBUTED),
])
def test_client_is_normalized_or_bucketed_as_unattributed(raw, expected):
    assert parse_client(raw) == expected


def test_the_protocol_name_is_never_reported_as_the_top_client():
    """★ THE THIRD CONTROL. The gate defaults mcp_client to the literal "mcp":
    `body.get('mcp_client') or body.get('platform') or 'mcp'`. Trusting that as
    an identity is how mcp_calls_identity put 87% of call volume into a bucket
    it described as unattributed, and here it would name a PROTOCOL as the
    dominant caller — in the one block whose entire job is naming the caller."""
    out = summarize_compliance([
        row("trial_preview:treatment", "mcp",    gated=800, acted=80),
        row("trial_preview:control",   "claude", gated=200, acted=20),
    ])
    assert "mcp" not in out["by_client"]
    assert out["by_client"][GENERIC_CLIENT]["gated_sessions"] == 800
    assert out["concentration"]["top_client"] == GENERIC_CLIENT
    # and it is NOT quietly relabelled as an absence — it is a real cohort
    assert UNATTRIBUTED not in out["by_client"]


def test_generic_client_vocabulary_matches_the_identity_view():
    """The same cohort must be named the same way on both surfaces. If the view
    widens its generic set and this module does not, by_client starts reporting
    a protocol string as a client again — silently, and only there."""
    migrations = sorted(REPO.glob("migrations/*mcp_calls_identity*.sql"))
    assert migrations, "no mcp_calls_identity migration found — this test proves nothing"
    sql = migrations[-1].read_text(encoding="utf-8", errors="replace")
    m = re.search(r"LOWER\(COALESCE\(client_name, ''\)\) IN \(([^)]*)\)", sql)
    assert m, f"generic-name branch not found in {migrations[-1].name}"
    in_view = {v.strip().strip("'").lower() for v in m.group(1).split(",")}
    assert in_view == set(_GENERIC_CLIENT_NAMES), (
        f"{migrations[-1].name} treats {sorted(in_view)} as generic; this "
        f"module treats {sorted(_GENERIC_CLIENT_NAMES)}")


def test_by_client_is_bounded_and_the_tail_is_folded_not_dropped():
    # mcp_client is caller-supplied text; an unbounded by_client is a response
    # that grows on someone else's input. Folding keeps the sums reconciling.
    rows = [row("trial_preview:treatment", "client-%03d" % i,
                gated=100 - i, acted=1) for i in range(_CLIENT_ROWS_MAX + 15)]
    out = summarize_compliance(rows)
    assert len(out["by_client"]) == _CLIENT_ROWS_MAX + 1     # + the folded row
    assert OTHER_CLIENTS in out["by_client"]
    assert out["by_client_rows"]["folded_into_other"] == 15
    assert out["by_client_rows"]["listed"] == _CLIENT_ROWS_MAX
    assert (sum(c["gated_sessions"] for c in out["by_client"].values())
            == out["totals"]["gated_sessions"])


def test_concentration_counts_every_client_not_just_the_listed_ones():
    # ★ Folding happens AFTER concentration. Truncating first could change
    # which client is on top, which is the one thing this block must get right.
    rows = [row("trial_preview:treatment", "client-%03d" % i,
                gated=100 - i, acted=1) for i in range(_CLIENT_ROWS_MAX + 15)]
    out = summarize_compliance(rows)
    assert out["concentration"]["distinct_clients"] == _CLIENT_ROWS_MAX + 15
    assert out["concentration"]["top_client"] == "client-000"


def test_client_casing_variants_are_one_row_not_three():
    # ★ A client split across casing variants would UNDERSTATE its own
    # concentration — the one number this block exists to state.
    out = summarize_compliance([
        row("trial_preview:treatment", "Claude",   gated=100, acted=10),
        row("trial_preview:treatment", "claude ",  gated=100, acted=10),
        row("trial_preview:treatment", "CLAUDE",   gated=100, acted=10),
    ])
    assert list(out["by_client"]) == ["claude"]
    assert out["by_client"]["claude"]["gated_sessions"] == 300
    assert out["concentration"]["top_client_share"] == 1.0


def test_unattributed_sessions_are_counted_not_dropped():
    # An unattributed session still met a gate. Hiding it would shrink the
    # denominator every rate in the payload is taken over.
    out = summarize_compliance([row("trial_preview:control", None, gated=40, acted=4)])
    assert out["by_client"][UNATTRIBUTED]["gated_sessions"] == 40
    assert out["totals"]["gated_sessions"] == 40


# ── the zero-denominator rule ────────────────────────────────────────────
def test_empty_window_is_unmeasured_not_zero_percent():
    # ★ THE CONTROL. The failure this whole module is shaped around.
    out = summarize_compliance([])
    for arm in ("treatment", "control", "ineligible", "legacy_shape_assigned", "untagged"):
        assert out["arms"][arm]["state"] == "UNMEASURED"
        assert "acted_rate" not in out["arms"][arm]
    assert out["totals"]["state"] == "UNMEASURED"
    assert out["comparison"]["state"] == "UNMEASURED"
    assert out["concentration"]["state"] == "UNMEASURED"
    assert out["by_client"] == {}


def test_zero_acted_on_a_real_denominator_IS_measured():
    # The mirror image: 0 of 400 is a real, alarming finding — not an absence.
    out = summarize_compliance([row("trial_preview:treatment", gated=400, acted=0)])
    q = out["arms"]["treatment"]
    assert q["state"] == "MEASURED"
    assert q["acted_rate"] == 0.0


# ── ★ opportunity: refusal vs no turn ────────────────────────────────────
def test_a_bucket_where_nothing_continued_is_unmeasured_not_zero():
    """★ THE SECOND CONTROL. 500 sessions met a gate and none made any further
    call. The agent had no turn in which to comply, so a 0% compliance rate
    would be reporting a gateway's session model as an agent's refusal."""
    out = summarize_compliance([
        row("trial_preview:treatment", "grok", gated=500, continued=0, acted=0),
    ])
    t = out["arms"]["treatment"]
    assert t["state"] == "UNMEASURED"
    assert "acted_rate" not in t
    assert t["gated_sessions"] == 500          # still counted, not hidden
    assert "no turn in which to comply" in t["why"]
    assert out["by_client"]["grok"]["state"] == "UNMEASURED"


def test_sessions_that_continued_and_did_not_act_ARE_measured():
    # The distinction that makes the previous test mean something: these
    # sessions had a turn and did not use it. That is refusal, and it counts.
    out = summarize_compliance([
        row("trial_preview:treatment", gated=500, continued=500, acted=0),
    ])
    t = out["arms"]["treatment"]
    assert t["state"] == "MEASURED"
    assert t["acted_rate"] == 0.0
    assert t["acted_rate_of_continued"] == 0.0


def test_dead_sessions_do_not_deflate_the_rate_of_those_that_had_a_turn():
    out = summarize_compliance([
        row("trial_preview:control", gated=100, continued=10, acted=5),
    ])
    c = out["arms"]["control"]
    assert c["acted_rate"] == 0.05               # over everything gated
    assert c["acted_rate_of_continued"] == 0.5   # over those with a turn
    assert "dead sessions, not refusal" in c["read"]


# ── arithmetic that cannot flatter itself ────────────────────────────────
def test_acted_can_never_exceed_gated():
    out = summarize_compliance([row("trial_preview:control", gated=10, acted=99)])
    g = out["arms"]["control"]
    assert g["acted_sessions"] == 10 and g["acted_rate"] == 1.0


def test_acted_can_never_exceed_continued():
    out = summarize_compliance([
        row("trial_preview:control", gated=100, continued=10, acted=99),
    ])
    g = out["arms"]["control"]
    assert g["acted_sessions"] == 10
    assert g["acted_rate_of_continued"] == 1.0


def test_continued_can_never_exceed_gated():
    out = summarize_compliance([
        row("trial_preview:control", gated=10, continued=99, acted=3),
    ])
    assert out["arms"]["control"]["continued_sessions"] == 10


@pytest.mark.parametrize("bad", [
    ("trial_preview:generic", "c", "x", 1, 1),   # gated not a number
    ("trial_preview:generic", "c", -5, 1, 1),    # negative gated
    ("trial_preview:control", "c", 5, 5, -1),    # negative acted
    ("trial_preview:control", "c", 5, -1, 1),    # negative continued
    ("trial_preview:control", 5, 0),             # ★ the OLD 3-field row shape
    (), None, ("only-one-field",),
])
def test_malformed_rows_are_counted_not_silently_guessed(bad):
    # ★ A query that quietly stopped matching looks exactly like agents that
    # quietly stopped complying. Dropped rows are PUBLISHED so the two can be
    # told apart, and no field is defaulted — there is no honest default for
    # `continued`: `gated` invents opportunity, `acted` invents its absence.
    out = summarize_compliance([bad])
    assert out["totals"]["state"] == "UNMEASURED"
    assert out["dropped_rows"] == 1


def test_multiple_rows_in_one_arm_accumulate():
    out = summarize_compliance([
        row("trial_preview:treatment", gated=100, acted=10),
        row("trial_preview:treatment", gated=100, acted=20),
    ])
    assert out["arms"]["treatment"]["gated_sessions"] == 200
    assert out["arms"]["treatment"]["acted_rate"] == 0.15


# ── ★ concentration: is this agents, or one caller? ──────────────────────
def test_concentration_names_the_top_client_and_its_share():
    out = summarize_compliance([
        row("trial_preview:treatment", "chain-hire", gated=660, acted=60),
        row("trial_preview:treatment", "claude",     gated=240, acted=40),
        row("trial_preview:control",   "grok",       gated=100, acted=10),
    ])
    con = out["concentration"]
    assert con["state"] == "MEASURED"
    assert con["top_client"] == "chain-hire"
    assert con["top_client_gated_sessions"] == 660
    assert con["top_client_share"] == 0.66
    assert con["dominated"] is True
    assert con["distinct_clients"] == 3


def test_concentration_publishes_the_total_net_of_the_top_client():
    out = summarize_compliance([
        row("trial_preview:treatment", "chain-hire", gated=660, acted=60),
        row("trial_preview:treatment", "claude",     gated=240, acted=40),
        row("trial_preview:control",   "grok",       gated=100, acted=10),
    ])
    net = out["concentration"]["net_of_top_client"]
    assert net["gated_sessions"] == 340          # 240 + 100
    assert net["acted_sessions"] == 50           # 40 + 10
    assert net["state"] == "MEASURED"


def test_concentration_unit_is_sessions_and_says_so():
    """★ /api/v1/ai/reach concentrates TOOL CALLS on mcp_calls_identity; this
    concentrates gated SESSIONS on mcp_upgrade_signals. Quoting one against the
    other is the cross-basis contradiction canonical_top_caller_sql exists to
    end, so the unit is named IN the payload."""
    out = summarize_compliance([row("trial_preview:treatment", gated=10, acted=1)])
    con = out["concentration"]
    assert con["unit"] == "gated_sessions"
    assert "never sum" in con["why"]


def test_an_evenly_spread_population_is_not_dominated():
    out = summarize_compliance([
        row("trial_preview:treatment", "claude", gated=100, acted=10),
        row("trial_preview:treatment", "grok",   gated=100, acted=10),
        row("trial_preview:control",   "chatgpt", gated=100, acted=10),
    ])
    assert out["concentration"]["dominated"] is False


# ── the comparison ───────────────────────────────────────────────────────
def test_comparison_needs_both_arms_populated():
    out = summarize_compliance([row("trial_preview:treatment", gated=400, acted=52)])
    assert out["comparison"]["state"] == "UNMEASURED"


def test_comparison_needs_both_arms_to_have_had_a_turn():
    # Both arms have a denominator, but the control's sessions all died at the
    # gate. Comparing a measured rate against an unobservable one would read as
    # "the control performed worse".
    out = summarize_compliance([
        row("trial_preview:treatment", gated=400, continued=400, acted=52),
        row("trial_preview:control",   gated=400, continued=0,   acted=0),
    ])
    assert out["comparison"]["state"] == "UNMEASURED"
    assert "continued past the" in out["comparison"]["why"]


def test_comparison_reports_the_raw_difference_and_says_it_is_raw():
    out = summarize_compliance([
        row("trial_preview:treatment", "claude", gated=400, acted=52),
        row("trial_preview:control",   "grok",   gated=400, acted=40),
    ])
    c = out["comparison"]
    assert c["state"] == "MEASURED"
    assert c["difference"] == pytest.approx(0.03, abs=1e-6)
    # It must not pass itself off as a significance test.
    assert "not a significance test" in c["note"]


def test_a_dominated_comparison_names_who_it_generalizes_to():
    """★ Randomization is per session and salted, so one caller dominating does
    NOT break the estimate internally — the split is still random inside it.
    What it changes is who the answer is ABOUT. The payload must say that
    without implying the estimate is invalid."""
    out = summarize_compliance([
        row("trial_preview:treatment", "chain-hire", gated=800, acted=80),
        row("trial_preview:control",   "chain-hire", gated=700, acted=70),
        row("trial_preview:control",   "claude",     gated=100, acted=20),
    ])
    c = out["comparison"]
    assert c["state"] == "MEASURED"
    assert "chain-hire" in c["generalizes_to"]
    assert "valid estimate" in c["generalizes_to"]   # not "invalid"
    assert "by_client" in c["generalizes_to"]


def test_an_undominated_comparison_makes_no_generalization_claim():
    out = summarize_compliance([
        row("trial_preview:treatment", "claude",  gated=100, acted=10),
        row("trial_preview:treatment", "grok",    gated=100, acted=12),
        row("trial_preview:control",   "chatgpt", gated=100, acted=10),
        row("trial_preview:control",   "you",     gated=100, acted=11),
    ])
    assert "generalizes_to" not in out["comparison"]


def test_the_tools_counted_as_compliance_are_published_in_the_response():
    # A consumer cannot interpret the rate without knowing what counted. And a
    # next_tool added to the server but not here under-counts compliance, which
    # reads as agents ignoring us — so the list is stated, not implied.
    out = summarize_compliance([row("trial_preview:control", gated=1, acted=0)])
    assert out["continuation_tools"] == list(CONTINUATION_TOOLS)
    assert "unlock_more_data" in out["continuation_tools"]


def test_pre_randomization_rows_are_excluded_from_the_comparison():
    """★ The first cut assigned quantified/generic by PAYLOAD SHAPE, not at
    random — array-returning tools in one arm, scalar-returning in the other.
    Those rows are a different experiment. Pooling them into the arms whose
    names they resemble is the contamination the rename exists to prevent, and
    doing it in the reader would be no better than doing it in the writer."""
    out = summarize_compliance([
        row("trial_preview:quantified", gated=150, acted=30),   # legacy
        row("trial_preview:generic",    gated=150, acted=3),    # legacy
    ])
    assert out["arms"]["legacy_shape_assigned"]["gated_sessions"] == 300
    assert out["arms"]["treatment"]["state"] == "UNMEASURED"
    assert out["arms"]["control"]["state"] == "UNMEASURED"
    assert out["comparison"]["state"] == "UNMEASURED"


def test_ineligible_is_never_compared_against_the_treatment():
    # It could not have carried a count either way — it is not a control.
    out = summarize_compliance([
        row("trial_preview:treatment",  gated=200, acted=30),
        row("trial_preview:ineligible", gated=900, acted=5),
    ])
    assert out["arms"]["ineligible"]["state"] == "MEASURED"
    assert out["comparison"]["state"] == "UNMEASURED"   # no control arm present


def test_by_client_and_arms_are_two_views_of_the_same_sessions():
    # Both partitions are built from the same rows, so they must total the same
    # thing. If they ever diverge, one of the two is dropping sessions.
    rows = [
        row("trial_preview:treatment", "claude", gated=100, continued=90, acted=10),
        row("trial_preview:control",   "grok",   gated=200, continued=50, acted=5),
        row("trial_preview",           None,     gated=300, continued=10, acted=1),
    ]
    out = summarize_compliance(rows)
    for key in ("gated_sessions", "continued_sessions", "acted_sessions"):
        assert (sum(c[key] for c in out["by_client"].values())
                == out["totals"][key] == sum(
                    out["arms"][a][key] for a in out["arms"]))
