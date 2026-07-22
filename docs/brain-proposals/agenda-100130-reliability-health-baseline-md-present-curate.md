<!-- fingerprint:73b44b0dc06aafc4f3a22468445b0840 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] HEALTH_BASELINE.md present (curated known-good config + canonical numbers)

> Auto-captured from an **approved** brain agenda item (#100130). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-22T22:58:07.998535Z · agenda #100130_

## The approved recommendation

Choose the single reliability investment: (A) build the transaction-aware connection pooler with read/write routing (root-cause fix, recommended), (B) instead arm the existing Reliability-Recovery shell out of SHADOW and accept fallback-based recovery as the strategy, or (C) prioritize the cheaper detection fixes first (re-schedule the 2 unscheduled cron canaries + speed up the slow surface-health detector) and defer architecture work until flapping recurrence is actually re-measured.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
