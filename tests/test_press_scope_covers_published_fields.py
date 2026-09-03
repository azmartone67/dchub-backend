"""The NESO false claim survived its own correction. Three blind spots, one field.

★ MEASURED LIVE 2026-09-03 on
/api/press-releases/2026-07-17-neso-interconnection-queue-609-gw (HTTP 200):

    title            "... — Great Britain's Backlog in a Single Operator"  CORRECTED
    body             "NESO (National Energy System Operator, UK) ..."       CORRECTED
    meta_description "NESO's 609 GW interconnection queue equals 35% of
                      all US queued load ..."                              STILL FALSE

audit-closure lane G has carried SH52-061 as FAIL ("still published with the
US claim (NESO is the GB operator)") ever since. NESO is the GB system
operator; it holds no share of US queued load. `meta_description` is the field
Google, Bing, LinkedIn, Slack unfurls and AI crawlers render — the most
syndicated text on the page.

THREE INDEPENDENT DEFECTS, EACH OF WHICH ALONE WOULD HAVE LET THIS THROUGH:

  1 THE DETECTOR COULD NOT MATCH THE SENTENCE. check_entity_scope required the
    operator ADJACENT to the noun phrase. Every existing test used that shape
    ("NESO's interconnection queue"). What shipped had a measurement in
    between — "NESO's 609 GW interconnection queue" — and the pattern captured
    nothing. ("GW" is two characters and cannot even satisfy the {2,}
    operator-name quantifier.)

  2 THE REVIEW DID NOT READ THE FIELD. analyst_review scanned
    f"{title}\\n{body}" while being handed a release dict that carried
    meta_description and subheadline.

  3 THE CORRECTION LANE COULD NOT WRITE THE FIELD. /press-integrity/correct —
    added 2026-08-08 and whose own docstring says "the NESO '35% of US'
    corrections motivated this" — accepted title and body only. So the
    operator corrected the two fields the lane could reach, the review
    certified the result clean, and the claim kept travelling.

Together they are self-sealing: a checker that reads a subset of what it
publishes, and a correction tool that writes that same subset, will certify
each other forever.
"""
from __future__ import annotations

import pytest

from routes.media_claim_verify import check_entity_scope
from routes.press_integrity import analyst_review

# The exact bytes served at 2026-09-03, not a paraphrase.
LIVE_META = ("NESO's 609 GW interconnection queue equals 35% of all US queued "
             "load, signaling multi-year time-to-power as US regulators push "
             "fast-connect plans.")
LIVE_TITLE = ("NESO's Interconnection Queue Holds 609 GW of Requested Load — "
              "Great Britain's Backlog in a Single Operator")
LIVE_BODY = (
    "## Highlights\n\nNESO (National Energy System Operator, UK) now holds "
    "**609 GW of requested load** in its interconnection queue. The queue has "
    "grown 18% year-over-year, driven by AI and hyperscale demand concentrated "
    "in southern England. Meanwhile, US regulators this week ordered grid "
    "operators to develop expedited connection pathways for data-center "
    "projects, acknowledging that multi-year queue times have become the "
    "binding constraint on new capacity.")


# ── DEFECT 1: the detector must match the sentence that actually shipped ──

def test_the_published_sentence_is_caught():
    """The regression itself. Before this change: []."""
    hits = check_entity_scope(LIVE_META)
    assert hits, "the sentence the detector exists for must not pass it"
    assert "NESO" in hits[0] and "GB" in hits[0]


@pytest.mark.parametrize("sentence", [
    # adjacent — the only shape the old pattern handled
    "NESO's interconnection queue is 35% of all US queued load.",
    # one modifier
    "NESO's 609 GW interconnection queue is 35% of all US queued load.",
    # several
    "NESO's record 609 GW interconnection queue is 35% of all US queued load.",
    # no possessive
    "The NESO 609 GW transmission queue is 35% of all US queued load.",
])
def test_a_modifier_between_the_operator_and_the_noun_never_hides_it(sentence):
    assert check_entity_scope(sentence), sentence


def test_the_leftmost_match_trap():
    """★ Why this is a look-BACK and not a wider regex. A widened single
    pattern captures "operator" here, consumes through "queue", and NESO is
    never considered — a miss that reads exactly like a pass."""
    assert check_entity_scope(
        "The UK operator NESO's 609 GW interconnection queue is 35% of all "
        "US queued load.")


# ── narrowness: catching more must not mean catching wrongly ──────────

def test_a_us_operator_making_a_us_claim_is_still_fine():
    assert check_entity_scope(
        "ERCOT's 427 GW interconnection queue is 35% of all US queued "
        "load.") == []


def test_a_non_us_operator_with_no_us_share_claim_is_still_fine():
    assert check_entity_scope(
        "NESO's 609 GW interconnection queue holds Great Britain's "
        "backlog.") == []


@pytest.mark.parametrize("sentence", [
    # ★ NESO sits INSIDE the 4-token window but on the far side of a full
    # stop. Without the sentence cut this reads as a NESO claim. An earlier
    # draft of this test put NESO five tokens back, where the token bound
    # alone rejected it — so it passed with the cut removed and proved
    # nothing. The distance has to be short enough that only the cut saves it.
    "The queue belongs to NESO. ERCOT's interconnection queue is 35% of all "
    "US queued load.",
    "Compare with NESO; ERCOT's interconnection queue is 35% of all US "
    "queued load.",
    "Contrast NESO\nERCOT's interconnection queue is 35% of all US queued "
    "load.",
])
def test_a_subject_is_never_borrowed_across_a_sentence_boundary(sentence):
    """★ The misattribution risk an unbounded look-back would create. The
    queue in each of these is ERCOT's; NESO is merely nearby."""
    assert check_entity_scope(sentence) == [], sentence


def test_an_unknown_operator_never_raises_a_violation():
    assert check_entity_scope(
        "Fooland's 9 GW interconnection queue is 1% of all US queued "
        "load.") == []


def test_the_lookback_is_bounded():
    """Far enough away and the word is not the subject of this claim."""
    import routes.media_claim_verify as mcv
    assert 1 <= mcv._SUBJECT_LOOKBACK_TOKENS <= 6


def test_it_never_raises_on_junk():
    for junk in (None, "", 12345, {"a": 1}, "queue " * 400):
        assert isinstance(check_entity_scope(junk), list)


# ── DEFECT 2: the review must read every field it publishes ───────────

def _live_release(**over):
    r = {"slug": "2026-07-17-neso-interconnection-queue-609-gw",
         "title": LIVE_TITLE, "body": LIVE_BODY, "subheadline": "UK grid "
         "operator's backlog signals multi-year time-to-power",
         "meta_description": LIVE_META, "date": "2026-07-17"}
    r.update(over)
    return r


def test_the_live_release_no_longer_reviews_clean():
    """Before this change analyst_review returned issues: [] for exactly this
    dict — which is the certification that let the claim stand."""
    codes = [i["code"] for i in analyst_review(_live_release())["issues"]]
    assert "entity_scope" in codes


def test_a_slip_in_the_subheadline_is_also_seen():
    r = _live_release(meta_description="Great Britain's backlog.",
                      subheadline="NESO's 609 GW interconnection queue is 35% "
                                  "of all US queued load.")
    codes = [i["code"] for i in analyst_review(r)["issues"]]
    assert "entity_scope" in codes


def test_a_release_clean_in_every_field_still_passes():
    """★ NARROWNESS. Scanning more fields must not flag correct releases."""
    r = _live_release(
        meta_description="NESO's 609 GW interconnection queue is Great "
                         "Britain's backlog in a single operator.",
        subheadline="UK grid operator's backlog signals multi-year "
                    "time-to-power")
    codes = [i["code"] for i in analyst_review(r)["issues"]]
    assert "entity_scope" not in codes


def test_an_entity_scope_slip_stays_SOFT():
    """It flags a human; it does not silently unpublish a live release."""
    rev = analyst_review(_live_release())
    scope = [i for i in rev["issues"] if i["code"] == "entity_scope"]
    assert scope and scope[0]["hard"] is False
    assert rev["hard"] is False


def test_review_still_never_raises_on_a_release_missing_the_new_fields():
    """Callers that predate subheadline/meta_description must keep working."""
    rev = analyst_review({"title": LIVE_TITLE, "body": LIVE_BODY,
                          "slug": "x", "date": "2026-07-17"})
    assert isinstance(rev["issues"], list)
    assert rev["ok"] is True


# ── DEFECT 3: the correction lane must write every field ──────────────

def test_the_correction_lane_accepts_the_field_the_claim_lives_in():
    """A correction tool that cannot write a published field guarantees the
    claim survives the correction — which is what happened."""
    import inspect
    import routes.press_integrity as pi
    src = inspect.getsource(pi.correct_endpoint)
    assert 'p.get("meta_description")' in src
    assert 'p.get("subheadline")' in src


def test_the_correction_lane_writes_them_in_the_update():
    import inspect
    import routes.press_integrity as pi
    src = inspect.getsource(pi.correct_endpoint)
    upd = src[src.index("UPDATE press_releases"):]
    for col in ("title=%s", "body=%s", "subheadline=%s", "meta_description=%s"):
        assert col in upd[:200], col


def test_the_correction_lane_re_reviews_what_it_is_about_to_write():
    """Reviewing a subset of what you publish is defect 2 all over again."""
    import inspect
    import routes.press_integrity as pi
    src = inspect.getsource(pi.correct_endpoint)
    call = src[src.index("rev = analyst_review("):]
    call = call[:call.index("})") + 2]
    for key in ("title", "body", "subheadline", "meta_description"):
        assert '"%s":' % key in call, key
