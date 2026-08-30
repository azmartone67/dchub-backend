"""Nothing over-claiming reaches a partner inbox.

ai_lab_outreach emails partnerships@ at NVIDIA, Google DeepMind, Perplexity,
Mistral, Groq, CoreWeave, Lambda, TensorWave and Core42 on a daily autopilot.
On 2026-08-29 all 45 drafts were status='sent' (most recent that same day) and
every one carried the same four false claims. This gate is why that cannot
recur silently.
"""
import sys

sys.path.insert(0, ".")

from routes.outreach_claim_gate import verify_claims  # noqa: E402

CANON = {"facilities": "18,500+", "deals": "1,900+", "markets": "300+"}
ALL_TIME = 365_457
MARKERS = ("4,000+ tracked M&A", "21,000+", "50,000+")

# Verbatim from the draft emailed to Perplexity on 2026-08-29, including the
# line wrap — the newline is load-bearing: an earlier gap class that excluded
# \n silently missed the impossible 30-day claim, the worst one in the mail.
REAL_SENT = """I'm Jonathan Martone, founder of DC Hub (dchub.cloud) — the open
data center intelligence platform tracking 21,400+ global facilities,
4,000+ tracked M&A deals, a live construction-pipeline tracker, and the only
daily-refreshing public scorecard of data center power availability
(DCPI — Data Center Power Index, dchub.cloud/dcpi). 484K+ AI-agent
requests served last 30d led by Claude and Cursor."""

# What the corrected template renders, from canon floor phrases.
CANON_COPY = """I'm Jonathan Martone, founder of DC Hub (dchub.cloud) — the open
data center intelligence platform tracking 18,500+ global facilities,
1,900+ tracked M&A deals, a live construction-pipeline tracker, and the only
daily-refreshing public scorecard of data center power availability
(DCPI — Data Center Power Index, dchub.cloud/dcpi), scored across
300+ markets."""


def _kinds(body, canon=CANON, all_time=ALL_TIME, markers=MARKERS):
    return {v["kind"] for v in verify_claims(body, canon, all_time, markers)}


# ── the draft that actually went out ──────────────────────────────────
def test_the_real_sent_draft_is_blocked():
    v = verify_claims(REAL_SENT, CANON, ALL_TIME, MARKERS)
    assert v, "the mail that reached Perplexity must not pass"
    kinds = {x["kind"] for x in v}
    assert "over_claim" in kinds
    assert "impossible" in kinds
    assert "retired_marker" in kinds


def test_the_impossible_claim_is_caught_across_a_line_wrap():
    """484K+ in 30 days against 365,457 ever recorded. The claim spans a
    newline in the real draft; a gap class excluding \\n missed it entirely."""
    v = [x for x in verify_claims(REAL_SENT, CANON, ALL_TIME, MARKERS)
         if x["kind"] == "impossible"]
    assert len(v) == 1
    assert v[0]["claimed"] == 484_000
    assert v[0]["all_time"] == ALL_TIME


def test_both_over_claims_are_named_with_their_canon():
    v = [x for x in verify_claims(REAL_SENT, CANON, ALL_TIME, MARKERS)
         if x["kind"] == "over_claim"]
    by_noun = {x["noun"]: x for x in v}
    assert by_noun["facilities"]["claimed"] == 21_400
    assert by_noun["facilities"]["canon"] == 18_500
    assert by_noun["deals"]["claimed"] == 4_000
    assert by_noun["deals"]["canon"] == 1_900


# ── it must not cry wolf on honest copy ───────────────────────────────
def test_canon_derived_copy_passes():
    """The gate is worthless if it blocks the corrected template — that is how
    a gate gets routed around instead of fixed."""
    assert verify_claims(CANON_COPY, CANON, ALL_TIME, MARKERS) == []


def test_a_figure_below_canon_is_fine():
    """Canon values are FLOOR phrases. Under-stating is honest."""
    assert _kinds("we track 12,000+ facilities") == set()


def test_exactly_canon_is_fine():
    assert _kinds("we track 18,500+ facilities") == set()


def test_a_believable_30d_figure_passes():
    assert _kinds("2,203+ requests served in the last 30d") == set()


# ── fail closed ───────────────────────────────────────────────────────
def test_unknown_all_time_blocks_a_30d_claim():
    """Not being able to check is not permission to send."""
    assert "unverifiable" in _kinds("484K+ requests served last 30d", all_time=None)


def test_canon_missing_a_noun_does_not_silently_allow_it():
    """A noun canon does not cover is not asserted about — but one it covers
    with an unparseable value must never pass."""
    assert "unverifiable" in _kinds("we track 21,400+ facilities",
                                    canon={"facilities": "not a number"})


def test_empty_body_is_refused():
    assert _kinds("") == {"empty_body"}
