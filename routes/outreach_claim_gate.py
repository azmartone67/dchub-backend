"""routes/outreach_claim_gate.py — nothing over-claiming reaches a partner inbox.

WHY (2026-08-29)
================
`ai_lab_outreach` has been emailing partnerships@ at NVIDIA, Google DeepMind,
Perplexity, Mistral, Groq, CoreWeave, Lambda, TensorWave and Core42 on a daily
autopilot. It works. What it sends is wrong.

Every one of the 45 drafts (5 per target, ALL status='sent', most recent
2026-08-29) carries the same hardcoded pitch:

    "tracking 21,400+ global facilities, 4,000+ tracked M&A deals …
     484K+ AI-agent requests served last 30d led by Claude and Cursor"

Measured the same day:

    claim                        reality
    21,400+ facilities           canon 18,500+                       over
    4,000+ M&A deals             canon 1,900+                        ~2x over
    484K+ requests last 30d      365,457 ALL TIME · 2,203 in 7d      impossible
    "led by Claude and Cursor"   Cursor: 601 all-time, 0 in 7d       false

The 30-day figure claims more traffic than has ever existed. The numbers were
f-string literals in the pitch template, so no amount of canon healing could
reach them — the white-glove machinery that keeps registry listings honest has
no view of this surface, which is the only one that emails a human at a partner
company.

WHAT THIS DOES
--------------
`verify_claims()` is a PURE function (no DB, no network, unit-tested) that reads
a draft body and returns the over-claims in it. `_perform_resend_send` calls it
at the single choke point both send routes flow through, and REFUSES TO SEND on
any violation.

★ IT FAILS CLOSED. If canon cannot be read, that is not permission to send — an
unverifiable claim is exactly the one worth stopping. `resolve_canon()` is
documented to DEGRADE rather than raise (it has returned facilities=400 against
a floor of 18,500), so a resolver hiccup must never be read as "no violations".

★ IT ONLY FLAGS OVER-CLAIMS. Canon figures are FLOOR phrases — "18,500+" means
at least. A draft saying 18,500+ when live is higher is honest and passes. Only
a number ABOVE canon, or a physically impossible one, is blocked. A gate that
cried wolf on correct copy would be routed around within a week.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# A "N+ in the last 30 days" traffic claim, however it is worded.
# ★ The gap classes must allow NEWLINES. The real draft wraps as
#     "484K+ AI-agent\nrequests served last 30d"
# and a [^.\n] gap silently missed the single most serious claim in it — the
# one asserting more traffic in 30 days than has ever existed. Excluding '.'
# still stops the match at a sentence boundary.
_WINDOW_CLAIM = re.compile(
    r'([\d][\d,.]*)\s*([KMB])?\+?\s*[^.]{0,40}?requests[^.]{0,60}?'
    r'(?:last\s*)?30\s*d', re.I)

# A figure attached to the noun it is claiming about.
_NOUN_CLAIM = re.compile(
    r'([\d][\d,]*)\s*\+\s*(?:global\s+)?(facilities|tracked\s+M&A\s+deals|deals|markets)',
    re.I)

_NOUN_KEY = {
    "facilities": "facilities",
    "deals": "deals",
    "tracked m&a deals": "deals",
    "markets": "markets",
}


def _to_int(raw: str, unit: str | None = None) -> int | None:
    try:
        n = float(raw.replace(",", ""))
    except Exception:
        return None
    mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get((unit or "").lower(), 1)
    return int(n * mult)


def _canon_int(phrase) -> int | None:
    """'18,500+' -> 18500. Canon values are floor phrases."""
    if not phrase:
        return None
    m = re.search(r'[\d][\d,]*', str(phrase))
    return _to_int(m.group(0)) if m else None


def verify_claims(body: str, canon: dict, all_time_requests: int | None,
                  stale_markers=()) -> list:
    """Return a list of violation dicts. Empty list == safe to send.

    canon: {"facilities": "18,500+", "deals": "1,900+", ...} — floor phrases.
    all_time_requests: the total ever recorded, or None if unknown.
    """
    violations = []
    if not body:
        return [{"kind": "empty_body",
                 "detail": "refusing to send an empty draft"}]

    # 1. A figure above canon for a noun canon covers.
    for raw, noun in _NOUN_CLAIM.findall(body):
        key = _NOUN_KEY.get(" ".join(noun.lower().split()))
        if not key or key not in canon:
            continue
        claimed = _to_int(raw)
        floor = _canon_int(canon.get(key))
        if claimed is None or floor is None:
            violations.append({
                "kind": "unverifiable", "noun": key, "claimed": raw,
                "detail": f"could not compare '{raw}+ {noun}' against canon "
                          f"{canon.get(key)!r} — refusing rather than guessing"})
            continue
        if claimed > floor:
            violations.append({
                "kind": "over_claim", "noun": key,
                "claimed": claimed, "canon": floor,
                "detail": f"the draft says {raw}+ {noun}; canon is "
                          f"{canon.get(key)}. Outbound copy may never exceed it."})

    # 2. A 30-day traffic figure larger than everything ever recorded.
    for raw, unit in _WINDOW_CLAIM.findall(body):
        claimed = _to_int(raw, unit)
        if claimed is None:
            continue
        if all_time_requests is None:
            violations.append({
                "kind": "unverifiable", "noun": "requests_30d", "claimed": raw,
                "detail": "cannot read the all-time request total, so a 30-day "
                          "claim cannot be checked — refusing rather than guessing"})
        elif claimed > all_time_requests:
            violations.append({
                "kind": "impossible", "noun": "requests_30d",
                "claimed": claimed, "all_time": all_time_requests,
                "detail": f"the draft claims {raw}{unit or ''}+ requests in 30 "
                          f"days; {all_time_requests:,} have EVER been recorded."})

    # 3. Anything already retired by canon.
    for marker in (stale_markers or ()):
        if marker and str(marker) in body:
            violations.append({
                "kind": "retired_marker", "marker": str(marker),
                "detail": f"'{marker}' is on the retired stale-marker list"})
    return violations
