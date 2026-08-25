"""Guards for the 2026-08-24 post-quality pass.

WHAT WAS MEASURED, on the 15 LinkedIn posts that actually shipped (their text
became readable from outside in #3105):

    truncated mid-thought                        4 of 15
    the same 8-word phrase twice in ONE post     2 of 15
    opened a paragraph "The second-order read"  13 of 15   (87%)

★ THE 87% WAS OUR OWN INSTRUCTION. ANALYST_VOICE's structure spec named the
  third section "3. THE SECOND-ORDER READ", the composer's continuing-column
  prompt ended "...or a second-order read the feed hasn't made yet", and the
  marketing_engine voice said "then a non-obvious second-order read". The model
  was following the spec. The spec was the bug — four separate seeds, all
  reworded.

★ BLOCK BREAKAGE, STEER STYLE. Truncation and intra-post repeats are gated at
  publish. The stylistic tic is NOT — blocking 87% of posts to fix a habit
  would silence the feed, which is the failure this program spent August
  digging out of. It goes into the composer prompt as a measured ban list.

★ THE DETECTORS WERE TUNED AGAINST THE LIVE FEED, NOT FIXTURES. The first cut
  flagged 8 of 15, four of them healthy call-to-action lines ending in a URL,
  and it found only 1 of the 4 real truncations because it judged the last LINE
  rather than the last PROSE line. Both live truncations sit mid-post with a
  well-formed footer after them.

Pure: no DB, no network, never imports main.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from routes.media_post_quality import (          # noqa: E402
    truncation_reason, intra_post_repeat, opener_phrases,
    overused_openers, ban_list_block)

_FOOTER = "\n\n(DC Hub data · Aug 24, 2026)"


# ══ 1. truncation — the four real ones ═════════════════════════════════════
def test_the_0824_ledger_post_is_caught():
    """Mid-post cut, followed by a link and a WELL-FORMED footer. A last-line
    check scores this clean — which is why it must judge the last PROSE line."""
    t = ("18,822 distinct facilities now sit in the DC Hub ledger.\n\n"
         "That is the standard failure mode of PDF-era market intel, and it is "
         "not cosmetic. Double-counted facilities in\n\n"
         "→ https://dchub.cloud/whats-new" + _FOOTER)
    # 2026-08-25: the signal is now the DANGLING function word ("... in"),
    # not the absence of a full stop — see test_a_headline_is_not_a_truncation.
    assert "dangles on" in truncation_reason(t)


def test_the_0821_deal_post_severed_label_is_caught():
    t = ("$1.2B, the largest disclosed transaction this week.\n\nSource: D" + _FOOTER)
    assert "severed value" in truncation_reason(t)


def test_the_0819_unclosed_footer_is_caught():
    t = "Olathe posts 66.6/100 on excess power.\n\n(DC Hub data · Aug 19, 2026"
    assert "unclosed" in truncation_reason(t)


def test_a_paragraph_cut_after_a_colon_is_caught():
    t = "Tulsa scores 75/100.\n\nThe second-order read: headroom" + _FOOTER
    # 2026-08-25: now reported as a colon introducing a fragment.
    assert "colon introduces a fragment" in truncation_reason(t)


# ══ 1b. THE PRODUCTION FALSE POSITIVE (2026-08-25) ═════════════════════════
# Measured hours after the first version shipped: /api/v1/media/self-critique
# showed two REAL posts blocked as "broken copy", both of them HEADLINES.
# A headline carries no terminal punctuation by convention, so "ends without a
# full stop" silenced two good slots. Strictly worse than the bug it chased —
# a truncated post is embarrassing; a false positive is silence.
_LIVE_HEADLINES = [
    "Upper Michigan's Cold-Load Corridor Now Competitive with Texas and Oklahoma",
    "Real-Time Infrastructure Intelligence Reaching Mainstream AI",
]


@pytest.mark.parametrize("headline", _LIVE_HEADLINES)
def test_a_headline_is_not_a_truncation(headline):
    """★ THE REGRESSION THAT REACHED PRODUCTION. These exact two strings were
    blocked live on 2026-08-25."""
    assert truncation_reason(headline) == ""
    assert truncation_reason(headline + "\n\nhttps://dchub.cloud/news/x" + _FOOTER) == ""


def test_the_discriminator_is_a_dangling_function_word_not_missing_punctuation():
    """★ WHY THE NEW RULE IS DIFFERENT IN KIND. The bar is now a POSITIVE signal
    of a cut, not the absence of a full stop — otherwise every headline trips."""
    assert truncation_reason("Tulsa clears 75 on excess power in") != ""     # dangles
    assert truncation_reason("Tulsa Clears 75 On Excess Power") == ""        # headline
    assert truncation_reason("Power headroom and land tighten together") == ""


# ══ 2. the false positives that cost four healthy slots ════════════════════
@pytest.mark.parametrize("tail", [
    "Browse the tool surface: https://dchub.cloud/capabilities",
    "Methodology and provenance: https://dchub.cloud/transparency",
    "Closed transactions: https://dchub.cloud/transactions · Index: https://dchub.cloud/dcpi",
])
def test_a_call_to_action_ending_in_a_url_is_not_truncation(tail):
    """★ THE REGRESSION CONTROL. The first detector flagged all three of these
    real, healthy posts. A quality gate whose false positives silence good
    slots is worse than no gate."""
    t = "Gilbert posted 72/100 on the DCPI excess-power index.\n\n" + tail + _FOOTER
    assert truncation_reason(t) == "", f"false positive on {tail!r}"


def test_a_complete_post_passes():
    t = ("Gilbert just posted 72/100 on the DCPI excess-power index, top-5 among "
         "every market we track.\n\nVerify time-to-power before committing capital.\n\n"
         "Full index: https://dchub.cloud/dcpi\n\n#DCPI #DataCenter" + _FOOTER)
    assert truncation_reason(t) == ""


def test_truncation_is_total_on_junk():
    for junk in ("", None, "   ", "\n\n"):
        assert truncation_reason(junk) == ""


# ══ 3. intra-post repeat — the glued-drafts bug ════════════════════════════
def test_the_0821_telemetry_post_said_it_twice():
    """Three drafts concatenated: the Japan/Korea/Brazil announcement appears
    in paragraph one AND paragraph three, with a stray fragment between."""
    t = ("Keyless grid telemetry: Japan (OCCTO), South Korea (KPX) and Brazil "
         "(ONS) just joined the DC Hub scoreboard.\n\n"
         "compare grids for LatAm and APAC siting.\n\n"
         "Five continents now rank on one scale: Japan (OCCTO), South Korea "
         "(KPX) and Brazil (ONS) just joined the scoreboard, keyless." + _FOOTER)
    assert "twice" in intra_post_repeat(t)


def test_a_post_that_does_not_repeat_itself_passes():
    t = ("Gilbert posted 72/100 on the DCPI excess-power index this week.\n\n"
         "Williston, North Dakota reaches a similar score through curtailment "
         "instead, which prices and contracts very differently." + _FOOTER)
    assert intra_post_repeat(t) == ""


def test_short_posts_cannot_trip_the_repeat_check():
    assert intra_post_repeat("18,822 facilities.") == ""


# ══ 4. the ban list — measured, and it must not eat the boilerplate ════════
_POSTS = [
    "Tulsa scores 75.\n\nThe second-order read: the corridor is metro-adjacent now.\n\nSource: DC Hub, the live infrastructure data layer for AI agents.",
    "Olathe scores 66.\n\nThe second-order read: plains power is no longer rural-only.\n\nSource: DC Hub, the live infrastructure data layer for AI agents.",
    "Gilbert scores 72.\n\nThe second-order read: two headroom types price differently.\n\nSource: DC Hub, the live infrastructure data layer for AI agents.",
    "Meta closed 5,000 MW.\n\nMilpitas posts 62.1 on excess power.\n\nSource: DC Hub, the live infrastructure data layer for AI agents.",
]


def test_the_measured_tic_is_surfaced():
    assert "the second order read" in overused_openers(_POSTS, min_count=3)


def test_the_required_source_line_is_never_banned():
    """★ THE BUG THE FIRST VERSION HAD. Counting furniture made the mandatory
    attribution line outrank the real tic — the ban list would have told the
    writer to stop using its own required source line."""
    banned = overused_openers(_POSTS, min_count=3)
    assert not any("source dc hub" in b for b in banned), banned
    assert not any(b.startswith("dc hub data") for b in banned), banned


def test_a_varied_feed_produces_no_ban_list():
    """★ NEGATIVE CONTROL. If this ever returns a list, the detector is
    flagging normal writing and the prompt gets noise instead of a signal."""
    varied = ["Tulsa scores 75.\n\nWhat follows from that is a metro-edge thesis.",
              "Olathe scores 66.\n\nPlains power stopped being a rural story here.",
              "Gilbert scores 72.\n\nCurtailment and headroom are not the same buy."]
    assert overused_openers(varied, min_count=3) == []
    assert ban_list_block(varied, min_count=3) == ""


def test_the_ban_block_names_the_phrase_for_the_model():
    block = ban_list_block(_POSTS, min_count=3)
    assert "second order read" in block
    assert "do not start any paragraph" in block.lower()


def test_openers_skip_furniture_paragraphs():
    t = "Real opening sentence here now.\n\n#DCPI #DataCenter\n\nSource: DC Hub, the live layer."
    assert opener_phrases(t) == ["real opening sentence here"]


# ══ 5. the spec no longer teaches the tic ══════════════════════════════════
def test_no_voice_spec_still_hands_the_model_the_phrase():
    """★★★ THE ROOT CAUSE, PINNED. Four separate live strings named the section
    'second-order read' and the model dutifully used it in 87% of posts. This
    fails if any of them comes back as an INSTRUCTION (comments and the ban
    text itself are fine — they are what keep it from returning)."""
    import re
    from pathlib import Path
    root = Path(ROOT)
    offenders = []
    for rel in ("routes/media_editorial.py", "routes/linkedin_content_engine.py",
                "routes/marketing_engine.py"):
        lines = root.joinpath(rel).read_text().splitlines()
        for i, line in enumerate(lines, 1):
            if "second-order read" not in line.lower():
                continue
            if line.lstrip().startswith("#"):
                continue                      # a comment cannot reach the model
            # The prohibition is itself a string containing the phrase, and it
            # wraps across source lines — judge a small window, not one line.
            window = " ".join(lines[max(0, i - 3):i + 1])
            if re.search(r"banned|never under a fixed label|do not label", window, re.I):
                continue                      # the prohibition itself
            offenders.append(f"{rel}:{i}: {line.strip()[:90]}")
    assert not offenders, "a voice spec is teaching the tic again:\n" + "\n".join(offenders)
