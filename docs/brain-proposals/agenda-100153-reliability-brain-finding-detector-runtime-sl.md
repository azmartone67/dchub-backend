<!-- fingerprint:0296c33ebe6fb4cd99d6d1dd2c149984 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: detector_runtime_slow @ detector:check_surface_health_critical (seen x29)

> Auto-captured from an **approved** brain agenda item (#100153). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-30T19:43:35.816254Z · agenda #100153_

## The approved recommendation

Choose the remediation path: (A) instrumentation-first structural refactor (per-probe telemetry + per-probe timeouts, then concurrency or async/cached execution) — higher effort, stops the class; (B) quick mitigation (raise the detector's threshold or split it into smaller detectors) — low effort, likely recurs; or (C) defer pending a profiling sprint to capture the missing runtime data before committing engineering time.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
