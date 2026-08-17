<!-- fingerprint:005f94c0d665305b13f4ebc6681ae1f3 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: addressable_demand_unconverted @ tool:get_grid_intelligence (seen x250)

> Auto-captured from an **approved** brain agenda item (#100134). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-24T08:17:08.866923Z · agenda #100134_

## The approved recommendation

Approve (a) changing addressable_demand_unconverted from per-user findings to one aggregated per-tool cohort finding with auto-resolution on verified nudge/conversion, and (b) wiring the finding's action to the existing _bind/bind_email nudge surfaces instead of escalation — or decide instead to keep per-user findings and invest in the conversion mechanic (pricing/CTA) itself.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100098-reliability-brain-finding-addressable-demand.md`, which stays
OPEN as the single obligation for `addressable_demand_unconverted`. This doc's target —
`tool:get_grid_intelligence` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100098-reliability-brain-finding-addressable-demand.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100098-reliability-brain-finding-addressable-demand.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100098-reliability-brain-finding-addressable-demand.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100098-reliability-brain-finding-addressable-demand.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-100098-reliability-brain-finding-addressable-demand.md (class collapse)