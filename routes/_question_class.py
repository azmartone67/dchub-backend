"""
routes/_question_class.py — classify the SHAPE of a caller's free-text question
at the call site (r-question-class 2026-07-27).

Shell #37 lane 4 was RED because `mcp_call_log` recorded WHAT tool ran and with
which params, but never WHAT KIND of question was being asked. So the
2026-07-27 GraphRAG demand read had to reverse-engineer intent from raw params
by hand — see reference_dchub_global_question_demand. This module makes the next
read a lookup.

★ THE BUCKETS ARE FROZEN to the taxonomy that read used, verbatim, because the
whole point is a like-for-like comparison in ~October. Renaming or re-cutting a
bucket silently invalidates the baseline. That baseline, over 1,032 EXTERNAL
free-text calls (self/test platforms excluded):

    entity_lookup      674  65.3%     parametric          14   1.4%
    topical            253  24.5%     thematic             4   0.4%
    noise               84   8.1%     question_local       3   0.3%
                                      global_synthesis     0   0.0%

`global_synthesis` is the only bucket a knowledge graph beats vector RAG at —
questions where no single chunk holds the answer. It was ZERO, but every
free-text front door was 2-24 days old at the time, so the reading was
under-powered rather than conclusive.

★ CONTRACT — this runs inside the fire-and-forget /track callback on every MCP
call, so it must be:
  · PURE — no DB, no network, no LLM. Deterministic for a given input.
  · CHEAP — precompiled regexes, one lowercase, early exit. ~microseconds.
  · FAIL-SOFT — classify() never raises; on any surprise it returns None, and
    None means "no free text present", not "unclassified". A wrong label is
    worse than no label: it would poison the very baseline this exists to keep.

★ It deliberately does NOT try to be a good NER or intent model. It reproduces
one hand-audited taxonomy so two reads months apart are comparable. If it ever
needs to be smarter, version the bucket names — never redefine them in place.
"""
from __future__ import annotations

import re

# The only keys that have ever carried caller free text (verified across
# 509,851 calls on 2026-07-27 — question/prompt/text/topic/search/description
# do not exist in the wild).
_TEXT_KEYS = ("intent", "query", "q")

_NOISE_EXACT = frozenset({
    "test", "test query", "testing", "hello", "hi", "ping", "foo", "bar",
    "expensive problems",
})

# Off-topic traffic that reached us via lead-scraping agents.
_OFFTOPIC = re.compile(r"google\s*map|marketing\s+agenc|leads?\s+from|psychology")

_QUESTION = re.compile(r"^(why|how|what|which|when|where|who)\b|\?\s*$")

# Cross-document synthesis vocabulary — "no single chunk holds this".
_SYNTH = re.compile(
    r"\b(trend|trends|theme|themes|pattern|patterns|across|overall|landscape"
    r"|outlook|summar\w*|emerging|driver|drivers|implication|implications"
    r"|compare|comparison|versus|vs)\b")

# Structured site-selection asks the deterministic planner already serves.
_PARAM = re.compile(
    r"\b(\d+\s*(mw|gw)\b|rank\b|shortlist\b|site\s+selection\b|buildable\b"
    r"|capacity\s+near\b|find\s+\d+)")


def _free_text(params) -> str | None:
    """The caller's free text, or None when no text key was present at all.

    ★ A key that IS present but blank (`{"query": ""}`) returns "" — NOT None.
    The caller invoked a free-text tool and supplied nothing, which is a
    degenerate question worth counting as noise; collapsing it into "no
    question asked" hides 30 real calls in the 07-27 window and breaks the
    baseline comparison. Presence of the key is the signal.
    """
    if not isinstance(params, dict):
        return None
    for k in _TEXT_KEYS:
        v = params.get(k)
        if isinstance(v, str):
            return v.strip()
    return None


def classify(params) -> str | None:
    """Bucket name, or None when there is no free text to classify.

    None is NOT a bucket — it means this call carried no question. Callers must
    keep that distinction: 'unclassified' would quietly become a garbage bucket
    that grows with every new param shape.
    """
    try:
        text = _free_text(params)
        if text is None:
            return None
        t = " ".join(text.lower().split())
        if len(t) < 3 or t in _NOISE_EXACT or _OFFTOPIC.search(t):
            return "noise"
        # Parametric first: "rank markets for a 200 MW campus" is a planner job
        # even though it reads like a question.
        if _PARAM.search(t):
            return "parametric"
        is_q, is_synth = bool(_QUESTION.search(t)), bool(_SYNTH.search(t))
        if is_q and is_synth:
            return "global_synthesis"
        if is_q:
            return "question_local"
        if is_synth:
            return "thematic"
        if len(t.split()) <= 3:
            return "entity_lookup"
        return "topical"
    except Exception:
        # Telemetry must never break a call, and must never guess.
        return None


# Frozen for the shell + tests. Order is the 07-27 baseline's descending volume.
BUCKETS = ("entity_lookup", "topical", "noise", "parametric", "thematic",
           "question_local", "global_synthesis")
