<!-- fingerprint:781c40847668ebf5322898970e3954e5 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: mcp_tool_zero_conversion @ /admin/per-tool-conversion#rank_markets (seen x1

> Auto-captured from an **approved** brain agenda item (#100194). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-14T19:12:37.367177Z · agenda #100194_

## The approved recommendation

Choose between: (A) redesign the detector — fingerprint dedup + base-rate-aware firing threshold + roll-up to one funnel-level finding (stops recurrence structurally, ~engineering effort on the brain's detector pipeline); (B) a lighter patch — bulk-close the ~1,900 open duplicates and raise the detector's lookback/threshold only (cheap, but risks re-accumulation); or (C) keep the per-tool findings but require the human to confirm the 2026-07-17 fixes' scope first before any new work. Also decide whether the signals→codes 100% drop warrants its own dedicated investigation, since that — not per-tool copy — appears to be the real conversion bottleneck.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-76-reliability-brain-finding-mcp-tool-zero-conve.md`, which stays
OPEN as the single obligation for `mcp_tool_zero_conversion`. This doc's target —
`/admin/per-tool-conversion#rank_markets` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-76-reliability-brain-finding-mcp-tool-zero-conve.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-76-reliability-brain-finding-mcp-tool-zero-conve.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-76-reliability-brain-finding-mcp-tool-zero-conve.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-76-reliability-brain-finding-mcp-tool-zero-conve.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-76-reliability-brain-finding-mcp-tool-zero-conve.md (class collapse)