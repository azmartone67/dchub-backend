<!-- fingerprint:73b44b0dc06aafc4f3a22468445b0840 -->
# Brain proposal — [reliability] HEALTH_BASELINE.md present (curated known-good config + canonical numbers)

> Auto-captured from an **approved** brain agenda item (#79). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-10T04:02:00.352942Z · agenda #79_

## The approved recommendation

Approve ONE of: (A) fund the read-replica connection-pool hardening + baseline-fenced instrumentation as the single reliability investment this cycle (recommended), or (B) defer it in favor of a deploy-pipeline watchdog targeting the cf_pages_deploy_stuck signal (10 occurrences), accepting that the underlying replica flap/staleness root cause identified by prior inspector work remains unaddressed.

## Triage — 2026-08-16 (spec-debt sweep #2) — CLOSED, exact re-file

Same condition as `docs/brain-proposals/agenda-63-reliability-health-baseline-md-present-curate.md`, filed earlier and STILL OPEN as
the canonical obligation. The two differ only in the live counts embedded in
the title — identical once numbers are stripped. Closing this copy does not
close the obligation: act on agenda-63-reliability-health-baseline-md-present-curate.md.

## Human checklist

- [x] Confirm this is still worth doing — the CONDITION is still open — this COPY is not; canonical doc is agenda-63-reliability-health-baseline-md-present-curate.md
- [x] Scope it to a concrete change (file(s) + approach) — scoping belongs to agenda-63-reliability-health-baseline-md-present-curate.md, which stays open
- [x] Implement + verify — not applicable to a duplicate — implement against agenda-63-reliability-health-baseline-md-present-curate.md
- [x] Or close this PR if superseded / not worth it — closed 2026-08-16 as an exact re-file of agenda-63-reliability-health-baseline-md-present-curate.md (spec-debt sweep #2)