<!-- fingerprint:01cf8768eb06b9a9ab91bdcea3f0ac05 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — Health probe: is the investigator returning a result right now?

> Auto-captured from an **approved** brain inv item (#100061). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-10T08:51:15.821895Z · inv #100061_

## The approved recommendation

Accept this run as a PASSING health check and log a heartbeat, or escalate to a fuller SLO probe that additionally measures latency, error rates, and queue depth (which this evidence did not cover). Also decide whether this probe class should be explicitly tagged for the existing probe-traffic quarantine so it never enters conversion metrics.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
