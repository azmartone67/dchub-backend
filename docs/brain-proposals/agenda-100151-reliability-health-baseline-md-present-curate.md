<!-- fingerprint:73b44b0dc06aafc4f3a22468445b0840 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] HEALTH_BASELINE.md present (curated known-good config + canonical numbers)

> Auto-captured from an **approved** brain agenda item (#100151). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-30T02:54:57.777777Z · agenda #100151_

## The approved recommendation

Approve arming the existing Reliability-Recovery master shell (SHADOW → enforcing) scoped to heartbeat-stale surfaces and unscheduled cron re-registration, with backoff caps — OR direct the next investment cycle at the write-path/connection-pool layer instead, which first requires measuring the recurrence rate of the replica read-only-fallback incidents (currently not measured).

## Triage — 2026-08-16 (spec-debt sweep #2) — CLOSED, exact re-file

Same condition as `docs/brain-proposals/agenda-63-reliability-health-baseline-md-present-curate.md`, filed earlier and STILL OPEN as
the canonical obligation. The two differ only in the live counts embedded in
the title — identical once numbers are stripped. Closing this copy does not
close the obligation: act on agenda-63-reliability-health-baseline-md-present-curate.md.

## Human checklist

- [x] Confirm this is still worth doing — the CONDITION is still open — this COPY is not; canonical doc is agenda-63-reliability-health-baseline-md-present-curate.md
- [x] Scope it to a concrete change (file(s) + approach) — scoping belongs to agenda-63-reliability-health-baseline-md-present-curate.md, which stays open
- [x] Implement + verify — not applicable to a duplicate — implement against agenda-63-reliability-health-baseline-md-present-curate.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-16 as an exact re-file of agenda-63-reliability-health-baseline-md-present-curate.md (spec-debt sweep #2)