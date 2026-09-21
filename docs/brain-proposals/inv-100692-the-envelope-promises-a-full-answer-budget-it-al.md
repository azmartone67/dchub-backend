<!-- fingerprint:31213ca19d1ffdbfa9db4e87b7e2279f -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — The envelope promises a full-answer budget it also disclaims — observed from the anon seat on mcp: ai_capacity_index call 1: top-level remaining_full_today=1 while quota.full_answers_remaining_today is null with full_answers_unavailable_reason='NOT YET APPLICABLE at... What is the root cause and the smallest correct fi

> Auto-captured from an **approved** brain inv item (#100692). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-20T23:42:46.181304Z · inv #100692_

## The approved recommendation

Open routes/ai_capacity_index.py and locate where the response envelope sets quota.full_answers_remaining_today to null for unbound/anonymous callers; add a mirror so the nested field reports the same effective value as top-level remaining_full_today (or an explicit 0) when full_answers_unavailable_reason='NOT YET APPLICABLE', and add a regression test in tools/qa_superuser/probe_mcp.py asserting the two fields never disagree for an anon seat.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
