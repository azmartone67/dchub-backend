<!-- fingerprint:2682ed97caaa34e55822608e07e45e0a -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — work-plan: lift the 'now_text_cast' fix-class

> Auto-captured from an **approved** brain agenda item (#100223). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-24T17:13:52.106756Z · agenda #100223_

## The approved recommendation

Choose the single systemic lever: (A) prioritize and resource completing the TEXT->timestamptz migration unblocked by #3128 (removes the root cause at the schema level), or (B) instead make the detector_scout now_text_cast check a blocking CI/pre-merge gate (prevents new instances but leaves the TEXT substrate in place). Recommendation is A with B as follow-on, but A carries migration risk and requires scoping the remaining TEXT datetime columns first.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
