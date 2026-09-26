<!-- fingerprint:a2cdbcc5c5644d51e6a3cad90f96e370 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: mcp_tool_zero_conversion @ /admin/per-tool-conversion#get_market_intel (see

> Auto-captured from an **approved** brain agenda item (#100229). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-26T04:35:26.011698Z · agenda #100229_

## The approved recommendation

Choose one: (A) approve redesigning mcp_tool_zero_conversion into a stateful, base-rate-aware, platform-gated detector (with dedup lifecycle and consistent no-invoice exclusions) and bulk-close the ~2,260 stale per-tool instances; (B) retire the per-tool zero-conversion detector entirely in favor of the existing mcp_funnel_leak platform-gated variant; or (C) first audit whether the 2026-07-17 fixes (brain_findings/9702, 10171) already changed the detector and these counts are pre-fix residue needing only worklist cleanup.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-76-reliability-brain-finding-mcp-tool-zero-conve.md`, which stays
OPEN as the single obligation for `mcp_tool_zero_conversion`. This doc's target —
`/admin/per-tool-conversion#get_market_intel (see` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-76-reliability-brain-finding-mcp-tool-zero-conve.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-76-reliability-brain-finding-mcp-tool-zero-conve.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-76-reliability-brain-finding-mcp-tool-zero-conve.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-76-reliability-brain-finding-mcp-tool-zero-conve.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of agenda-76-reliability-brain-finding-mcp-tool-zero-conve.md (class collapse)