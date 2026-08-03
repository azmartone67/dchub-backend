<!-- fingerprint:62e8067b69e26de57d0206c705328592 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — These are the measured, currently-failing critical constraints on DC Hub's actuation loop: 0 real of 2 total relay_opens — every other row is our own probe traffic (human-simulated / dchub-ops-verify). This is the 77→0 cliff, stated exactly. 81/105 drafts (77.1%) in 30d were refuted because the evidence block lacked what the question needed. ★CORRECTED 2026-07-28: this is NOT 'prior findings cited by id' — live rows show prior_work IS fully inlined as text (89/111 drafts), prior_fixes in 32/111. The real cause is that gather_evidence() took NO ARGUMENTS: 111 distinct question. Given these, what is the SINGLE highest-leverage change, and what would prove within 7 days that it worked?

> Auto-captured from an **approved** brain inv item (#100021). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-03T19:53:19.859280Z · inv #100021_

## The approved recommendation

Approve (or reject) prioritizing a gather_evidence(question, data_needed[]) refactor with a pre-draft sufficiency gate as the single change for this cycle — versus the alternative of instrumenting the relay_open path first — and confirm the 7-day success criteria: evidence-gap refutation rate <40% on new drafts, per-draft 'data needed' coverage audit, and ≥1 non-probe relay_open.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
