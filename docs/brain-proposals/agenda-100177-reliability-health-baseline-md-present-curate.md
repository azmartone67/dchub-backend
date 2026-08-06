<!-- fingerprint:73b44b0dc06aafc4f3a22468445b0840 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] HEALTH_BASELINE.md present (curated known-good config + canonical numbers)

> Auto-captured from an **approved** brain agenda item (#100177). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-06T18:20:11.853882Z · agenda #100177_

## The approved recommendation

Choose whether to (a) arm the Reliability-Recovery master shell out of SHADOW mode now, with its first armed lane targeting the /api/jobs/energy-discovery dead cron, or (b) keep it in SHADOW for another review cycle and first pull its shadow-run telemetry to confirm the 496,901 cron_silently_dead signal is real incident volume and not a counter artifact. Option (a) is the recommended single investment; option (b) trades speed for safety against a new automation loop.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
