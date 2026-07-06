# Brain proposal — [reliability] HEALTH_BASELINE.md present (curated known-good config + canonical numbers)

> Auto-captured from an **approved** brain agenda item (#63). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-06T02:14:31.984668Z · agenda #63_

## The approved recommendation

Approve building a single supervised scheduler/refresh watchdog (auto-restart + baseline-fence escalation covering heartbeat, freshness tables, and dedup pipeline) as the one funded reliability investment — versus the alternative of multi-replica HA with leader election, or first pulling the raw flapping incident logs and HEALTH_BASELINE thresholds to confirm the common-cause hypothesis before committing.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
