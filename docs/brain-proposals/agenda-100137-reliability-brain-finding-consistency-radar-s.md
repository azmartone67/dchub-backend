<!-- fingerprint:820289ed9c4bbcec8549a94afd4fcbe6 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: consistency_radar_scan_partial @ /api/v1/brain/consistency-radar (seen x85)

> Auto-captured from an **approved** brain agenda item (#100137). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-25T04:09:46.078597Z · agenda #100137_

## The approved recommendation

Approve the architectural change: (1) convert the consistency radar from request-time synchronous scanning to a scheduled background job with a cached-snapshot endpoint (accepting bounded staleness), (2) migrate HTTP self-call probes to in-process checks or a separate async probe lane, and (3) set per-detector time budgets. Alternatively, direct a measurement pass first (per-detector latency histogram + occurrence-vs-load clustering for the 85 instances) if you want harder current evidence before committing engineering time.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
