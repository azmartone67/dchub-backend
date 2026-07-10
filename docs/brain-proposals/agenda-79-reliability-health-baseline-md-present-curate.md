# Brain proposal — [reliability] HEALTH_BASELINE.md present (curated known-good config + canonical numbers)

> Auto-captured from an **approved** brain agenda item (#79). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-10T04:02:00.352942Z · agenda #79_

## The approved recommendation

Approve ONE of: (A) fund the read-replica connection-pool hardening + baseline-fenced instrumentation as the single reliability investment this cycle (recommended), or (B) defer it in favor of a deploy-pipeline watchdog targeting the cf_pages_deploy_stuck signal (10 occurrences), accepting that the underlying replica flap/staleness root cause identified by prior inspector work remains unaddressed.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
