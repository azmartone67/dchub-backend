<!-- fingerprint:d5a67ea7375bfd8ca8d0760d5f8e5657 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — These are the measured, currently-failing critical constraints on DC Hub's actuation loop: 48/81 drafts (59.3%) in 30d were refuted because the evidence block lacked what the question needed. ★CORRECTED 2026-07-28: this is NOT 'prior findings cited by id' — live rows show prior_work IS fully inlined as text (89/111 drafts), prior_fixes in 32/111. The real cause is that gather_evidence() took NO ARGUMENTS: 111 distinct questions. Given these, what is the SINGLE highest-leverage change, and what would prove within 7 days that it worked?

> Auto-captured from an **approved** brain inv item (#100369). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-24T17:13:51.541368Z · inv #100369_

## The approved recommendation

Approve (or reject) changing gather_evidence() to accept (question_text, data_needed[]) as the single change this cycle — versus alternatives like a post-hoc evidence reranker — and ratify the pass/fail threshold: missing-evidence refutation rate <=30% on the next 7 days of drafts with >=90% argument utilization, else roll back and re-diagnose.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
