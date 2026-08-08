<!-- fingerprint:82a019d9b8f3a0de3156df232f2b3b82 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — Quota meter does NOT move while it still has room to — observed from the anon seat on mcp: get_gas_intelligence: call 1 quota.full_answers_remaining_today=2 ({'state': 'TX'}) then call 2 quota.full_answers_remaining_today=2 ({'state': 'PA'}) — IDENTICAL desp... What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100040). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-08T06:13:44.683718Z · inv #100040_

## The approved recommendation

Decide (1) the product policy question first: should anonymous-seat calls to paid-tier tools like get_gas_intelligence consume 'full answers' at all, or are they previews? If they should consume: approve the read-after-write reorder of the quota meter plus the n→n-1 regression test. If they are previews: approve a display-only fix instead. Also decide whether to pull the quota-code/log inspection forward, since the evidence lacked the execution-flow data needed to confirm the mechanism before shipping.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
