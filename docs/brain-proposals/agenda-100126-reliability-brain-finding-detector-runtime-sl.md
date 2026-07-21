<!-- fingerprint:0296c33ebe6fb4cd99d6d1dd2c149984 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: detector_runtime_slow @ detector:check_surface_health_critical (seen x15)

> Auto-captured from an **approved** brain agenda item (#100126). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-21T18:10:44.268051Z · agenda #100126_

## The approved recommendation

Approve one of: (a) fund the structural refactor (parallel probes + per-probe timeouts + background/cached execution + per-probe telemetry) as a single work item superseding instance patches; (b) cheaper stopgap of raising the detector's runtime threshold and accepting the latency drift; or (c) require an instrumentation-only first phase (per-probe runtime capture across several scans) before committing to the refactor design.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
