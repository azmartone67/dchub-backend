<!-- fingerprint:6b458bd8acb4469b874d41c019c1c55e -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: cross_surface_metric_divergence @ routes/quarterly_report.py:67 (seen x20)

> Auto-captured from an **approved** brain agenda item (#100189). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-11T03:40:27.072911Z · agenda #100189_

## The approved recommendation

Approve one of: (A) fund the systemic fix — repo-wide canonical-literal sweep + CI lint fence + detector dedup (recommended), (B) verify first whether the 2026-06-16 fixes already resolved quarterly_report.py:67 and the 20-count is stale detector noise (cheapest, do this check regardless), or (C) continue per-instance patching (not recommended — this is the approach that produced 20 recurrences).

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
