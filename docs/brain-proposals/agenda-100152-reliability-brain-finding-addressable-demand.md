<!-- fingerprint:d0e79233a0a689d5c814a5ed5afca3f7 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: addressable_demand_unconverted @ tool:get_fiber_intel (seen x253)

> Auto-captured from an **approved** brain agenda item (#100152). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-30T02:54:46.803255Z · agenda #100152_

## The approved recommendation

Choose the root-cause path: (A) refactor the detector class to stateful/hysteresis KPI semantics with a substance-gate resolution criterion (one open finding per chronic condition, closed only on metric movement), plus wiring the existing _bind/bind_email nudge into get_fiber_intel with per-user once-only state; or (B) keep per-instance firing and only fix the rate-limit backoff (prior approach, which evidence shows did not stop recurrence). Also decide the resolution metric threshold — e.g., what paid-key share among top fiber-tool callers counts as 'converted'.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
