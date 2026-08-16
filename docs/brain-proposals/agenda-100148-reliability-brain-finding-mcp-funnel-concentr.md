<!-- fingerprint:d5cec62abe3f7d66978af55697d40c3f -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: mcp_funnel_concentration_top5 @ /api/v1/mcp/funnel (seen x868)

> Auto-captured from an **approved** brain agenda item (#100148). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-29T02:38:41.555860Z · agenda #100148_

## The approved recommendation

Approve one of: (A) reclassify mcp_funnel_concentration_top5 as a stateful, threshold+hysteresis KPI monitor with re-fire dedup (detector-side fix), (B) keep the detector but gate its closure on measured conversion lift from the top-5 tools via the existing auto-trial/bind_email flow (outcome-side fix), or (C) both. Also decide the concentration/conversion thresholds that define 'structurally resolved'.

## Triage — 2026-08-16 (spec-debt sweep #2) — CLOSED, exact re-file

Same condition as `docs/brain-proposals/agenda-70-reliability-brain-finding-mcp-funnel-concentr.md`, filed earlier and STILL OPEN as
the canonical obligation. The two differ only in the live counts embedded in
the title — identical once numbers are stripped. Closing this copy does not
close the obligation: act on agenda-70-reliability-brain-finding-mcp-funnel-concentr.md.

## Human checklist

- [x] Confirm this is still worth doing — the CONDITION is still open — this COPY is not; canonical doc is agenda-70-reliability-brain-finding-mcp-funnel-concentr.md
- [x] Scope it to a concrete change (file(s) + approach) — scoping belongs to agenda-70-reliability-brain-finding-mcp-funnel-concentr.md, which stays open
- [x] Implement + verify — not applicable to a duplicate — implement against agenda-70-reliability-brain-finding-mcp-funnel-concentr.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-16 as an exact re-file of agenda-70-reliability-brain-finding-mcp-funnel-concentr.md (spec-debt sweep #2)