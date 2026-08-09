<!-- fingerprint:8418e0c35ad44d8c4abfe674a99620f9 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — Is the brain investigator enabled and returning a result?

> Auto-captured from an **approved** brain inv item (#100050). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-09T07:52:46.420358Z · inv #100050_

## The approved recommendation

Accept 'investigator is enabled and returning results' as answered, and decide whether to commission the self-status probe (uptime/latency/writer-error telemetry for the investigator itself, extending the prior instrumentation recommendation) versus treating the populated worklist as sufficient proof of health and taking no further action.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
