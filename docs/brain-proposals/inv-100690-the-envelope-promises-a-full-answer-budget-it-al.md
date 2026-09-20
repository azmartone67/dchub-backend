<!-- fingerprint:25bf246e0449f58907048483a41e15f7 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — The envelope promises a full-answer budget it also disclaims — observed from the anon seat on mcp: get_fiber_intel call 1: top-level remaining_full_today=1 while quota.full_answers_remaining_today is null with full_answers_unavailable_reason='NOT YET APPLICABLE at a... What is the root cause and the smallest correct fi

> Auto-captured from an **approved** brain inv item (#100690). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-20T23:43:08.217581Z · inv #100690_

## The approved recommendation

Pull the get_fiber_intel envelope-assembly block in dchub_mcp_server.py (the site named by PATCH 2 in scripts/wire_metro_fiber.py) and change remaining_full_today to derive from the same source as quota.full_answers_remaining_today, emitting null/0 for the top-level counter whenever quota.full_answers_remaining_today is null or full_answers_unavailable_reason='NOT YET APPLICABLE'; then re-run tools/qa_superuser/probe_mcp.py from the anon seat to confirm the two fields no longer diverge.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
