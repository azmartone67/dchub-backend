<!-- fingerprint:7f9a149c864b0b2d9e74d81452ec56cc -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — These are the measured, currently-failing critical constraints on DC Hub's actuation loop: 32/72 drafts (44.4%) in 30d were refuted because the evidence block lacked what the question needed. ★CORRECTED 2026-07-28: this is NOT 'prior findings cited by id' — live rows show prior_work IS fully inlined as text (89/111 drafts), prior_fixes in 32/111. The real cause is that gather_evidence() took NO ARGUMENTS: 111 distinct questions. 0 MCP-attributed of 4 total conversions/30d — organic_no_mcp_touch=2, web:pricing-page=2. 15 platforms connected and zero agent-attributed dollars: a 16th integration changes nothing until lane 1 closes. Given these, what is the SINGLE highest-leverage change, and what would prove within 7 days that it worked?

> Auto-captured from an **approved** brain inv item (#100559). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-07T20:34:13.122828Z · inv #100559_

## The approved recommendation

Modify the gather_evidence() function signature in the brain investigator pipeline to accept (question_id, question_type, required_source_tags) and route each of the 111 question types to its question-specific source pull, then instrument the draft-refutation logger to tag 'refuted_missing_evidence' and report that rate daily against the 44.4% baseline for 7 days.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
