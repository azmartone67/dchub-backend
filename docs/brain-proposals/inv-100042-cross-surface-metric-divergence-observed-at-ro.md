<!-- fingerprint:6c5aae25057fda3134e90bc352ba3f6d -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — cross_surface_metric_divergence (observed at: routes/state_of_power.py:249). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it? If there is no mechanical fix, say so plainly and explain why.

> Auto-captured from an **approved** brain inv item (#100042). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-08T17:28:37.862511Z · inv #100042_

## The approved recommendation

Choose between: (1) approve a small multi-site patch replacing ALL hardcoded market-count literals in routes/state_of_power.py (lines ~225, 249, 468) and agent_hub.py (~898, 909) with canonical_stats.get_canonical_stats()/markets_phrase() reads — the durable fix; or (2) first audit whether the 2026-06-29 fix (brain_findings/8331) already covers line 249 and simply close/reset the 168 stale detector findings. Explicitly reject any literal-to-literal find-and-replace (e.g. 144→312) as a non-fix.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
