"""routes/evidence_status.py — ONE WRITER for the epistemic-status vocabulary.

★ WHY THIS MODULE EXISTS (2026-08-21).

On 2026-08-17 seven AI partners — Grok, Gemini, Perplexity, ChatGPT, Mistral,
Meta and Copilot — were asked to review DC Hub's telemetry. Their answers came
back as reflections of our own dashboard copy, and correcting them exposed a
defect that was ours, not theirs: **our payloads publish measurements and
interpretations in the same shape, so a consumer cannot tell them apart.**

The cost was specific. Four successive wrong root-causes for the relay funnel
were broadcast, and seven systems adopted each one verbatim and hardened
"diagnosed" into "solved" into "fully functional" within a single round. The
*observation* (`human_acted == 0`) never changed across all four. What changed
was the story attached to it — and nothing in the payload marked that as a
story.

ChatGPT proposed the fix and it is better than the two states we had:

    observed    We measured this directly.
    hypothesis  Proposed explanation, not experimentally confirmed.
    verified    An experiment isolated the mechanism.

Two states could not express the failure, because a hypothesis kept getting
promoted straight to "verified" with no experiment in between.

★★ THE POINT IS THAT IT IS MECHANICAL, NOT SOCIAL. The 08-17 handoff wrote:
"A convention in a memo evaporates — that is the whole lesson of this session.
Put status: observed|hypothesis|verified in the response envelopes and /mcp."
It then went into a memo, and evaporated: measured 2026-08-21, four days later,
the funnel payload contained neither "observed" nor "hypothesis" anywhere.
Mistral had asked precisely this — how do we socialize the convention? — and
the answer, recorded and not acted on, was "put it in the envelope, not a memo."
This module is that answer, executed.

★★★ A STAMP IS A CLAIM AND A WRONG ONE IS WORSE THAN NONE. `verified` asserts
that an experiment isolated a mechanism. Stamping an interpretation `verified`
is exactly the failure this vocabulary exists to prevent, and it is worse than
leaving it unstamped, because the stamp is machine-readable and will be
propagated without the hedging that surrounds prose. When unsure, `hypothesis`.

This module is a LEAF on purpose: pure data and validation, no Flask, no DB,
no imports from the app. Anything can import it, including a test.
"""
from __future__ import annotations

# ── the published vocabulary ────────────────────────────────────────────────
OBSERVED = "observed"
HYPOTHESIS = "hypothesis"
VERIFIED = "verified"

EVIDENCE_STATUS_VERSION = 1

EVIDENCE_STATUS_VOCABULARY = {
    OBSERVED: "We measured this directly.",
    HYPOTHESIS: "Proposed explanation, not experimentally confirmed.",
    VERIFIED: "An experiment isolated the mechanism.",
}

# Ordered weakest→strongest claim. Used only for presentation and for the
# guard; nothing promotes a status automatically — promotion requires an
# experiment, which is the entire point.
EVIDENCE_STATUS_ORDER = (OBSERVED, HYPOTHESIS, VERIFIED)

_ORIGIN = (
    "Proposed by ChatGPT during the 2026-08-17 seven-partner telemetry review, "
    "after four successive wrong root-causes for the relay funnel were "
    "broadcast and adopted verbatim. The observation never changed across all "
    "four; only the unmarked story attached to it did."
)


class EvidenceStatusError(ValueError):
    """Raised when a caller stamps something with a status outside the vocabulary."""


def is_valid(status: str) -> bool:
    return status in EVIDENCE_STATUS_VOCABULARY


def stamp(value, status: str, note: str | None = None) -> dict:
    """Wrap a published value with the status of the evidence behind it.

    Raises rather than defaulting. A silent fallback to `observed` would let a
    typo publish an interpretation as a measurement, which is the precise
    failure being fenced — and it would do it in a machine-readable field that
    consumers propagate without hedging.
    """
    if not is_valid(status):
        raise EvidenceStatusError(
            "%r is not an evidence status. Use one of: %s. There is no default: "
            "a mis-stamped claim is worse than an unstamped one, because it is "
            "machine-readable." % (status, ", ".join(EVIDENCE_STATUS_ORDER))
        )
    out = {"value": value, "status": status}
    if note:
        out["note"] = note
    return out


def vocabulary_block() -> dict:
    """The block published in envelopes so consumers can read the convention
    off the wire instead of being told about it once.

    Copies are handed out so a consumer that mutates what it renders cannot
    edit the canon for everybody else in the same worker process.
    """
    return {
        "evidence_status_version": EVIDENCE_STATUS_VERSION,
        "states": dict(EVIDENCE_STATUS_VOCABULARY),
        "origin": _ORIGIN,
        "contract": (
            "Any field carrying a `status` key uses this vocabulary. A value "
            "without a status is UNSTAMPED — treat it as unclassified, never "
            "as observed. Nothing here is promoted automatically: moving a "
            "claim from hypothesis to verified requires an experiment that "
            "isolated the mechanism."
        ),
    }
