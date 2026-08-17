<!-- fingerprint:0be160fc59f664df36d06e447be19b8b -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: addressable_demand_unconverted @ tool:analyze_site (seen x245)

> Auto-captured from an **approved** brain agenda item (#100163). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-03T19:53:29.817506Z · agenda #100163_

## The approved recommendation

Approve the two-part structural fix: (1) change the addressable_demand_unconverted detector to a single stateful, cohort-keyed finding with cooldown (closing the 245-instance backlog as duplicates of one condition), and (2) prioritize an instrumentation task on analyze_site to log nudge emission and nudge→bind→key progression using the existing _bind/claim_free_key surfaces — OR reject aggregation and keep per-instance findings if you want instance-level forensics first.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100098-reliability-brain-finding-addressable-demand.md`, which stays
OPEN as the single obligation for `addressable_demand_unconverted`. This doc's target —
`tool:analyze_site` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100098-reliability-brain-finding-addressable-demand.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100098-reliability-brain-finding-addressable-demand.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100098-reliability-brain-finding-addressable-demand.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100098-reliability-brain-finding-addressable-demand.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-100098-reliability-brain-finding-addressable-demand.md (class collapse)