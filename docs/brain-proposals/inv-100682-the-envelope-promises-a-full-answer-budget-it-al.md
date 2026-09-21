<!-- fingerprint:219012f962932ed664558367bd28f31e -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — The envelope promises a full-answer budget it also disclaims — observed from the anon seat on mcp: get_grid_intelligence call 1: top-level remaining_full_today=1 while quota.full_answers_remaining_today is null with full_answers_unavailable_reason='NOT YET APPLICABL... What is the root cause and the smallest correct fi

> Auto-captured from an **approved** brain inv item (#100682). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-20T11:12:01.724286Z · inv #100682_

## The approved recommendation

Open the dchub-mcp-server response-builder that emits structuredContent.remaining_full_today for get_grid_intelligence and force that field to null whenever quota.full_answers_unavailable_reason is set (mirroring the guard added in PR #461), then re-run tools/qa_superuser/probe_mcp.py from the anon seat to confirm the two fields no longer disagree.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
