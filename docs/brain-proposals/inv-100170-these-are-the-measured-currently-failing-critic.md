<!-- fingerprint:293e355a55b04bb87e70a374aee18966 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — These are the measured, currently-failing critical constraints on DC Hub's actuation loop: 0 real of 2 total relay_opens — every other row is our own probe traffic (human-simulated / dchub-ops-verify). This is the 77→0 cliff, stated exactly. 64/91 drafts (70.3%) in 30d were refuted because the evidence block lacked what the question needed. ★CORRECTED 2026-07-28: this is NOT 'prior findings cited by id' — live rows show prior_work IS fully inlined as text (89/111 drafts), prior_fixes in 32/111. The real cause is that gather_evidence() took NO ARGUMENTS: 111 distinct questions. Given these, what is the SINGLE highest-leverage change, and what would prove within 7 days that it worked?

> Auto-captured from an **approved** brain inv item (#100170). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-17T08:00:32.215264Z · inv #100170_

## The approved recommendation

Approve making 'parameterize gather_evidence(question, sub_questions, data_needed) with per-item source routing' the single change shipped this cycle — deferring all other actuation-loop work — and ratify the 7-day success gate: refuted-for-missing-evidence rate <35% on the next ~20 drafts AND ≥1 relay_open from genuinely external (non-probe, non-dchub-ops-verify) traffic. If only the first criterion passes, authorize a follow-up investigation into a separate relay_opens gate.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
