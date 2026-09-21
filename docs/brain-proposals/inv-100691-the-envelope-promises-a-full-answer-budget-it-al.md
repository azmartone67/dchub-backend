<!-- fingerprint:08009cb27a0724ad72f19d23faf691a7 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — The envelope promises a full-answer budget it also disclaims — observed from the anon seat on mcp: rank_markets call 1: top-level remaining_full_today=1 while quota.full_answers_remaining_today is null with full_answers_unavailable_reason='NOT YET APPLICABLE at an a... What is the root cause and the smallest correct fi

> Auto-captured from an **approved** brain inv item (#100691). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-20T23:42:59.729429Z · inv #100691_

## The approved recommendation

Open the envelope-assembly code path that sets structuredContent.remaining_full_today (referenced by probe_mcp.py:749/1154) and make both the top-level remaining_full_today and quota.full_answers_remaining_today derive from the single quota-authority value, returning null on top-level too when the nested field is null with reason NOT_YET_APPLICABLE; add a regression test in the same suite as dchub-mcp-server PR #479 asserting the two fields never diverge for an anon seat.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
