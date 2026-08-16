# Brain proposal — [reliability] Brain finding: mcp_funnel_concentration_top5 @ /api/v1/mcp/funnel (seen x808)

> Auto-captured from an **approved** brain agenda item (#87). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-12T07:47:22.283396Z · agenda #87_

## The approved recommendation

Approve reclassifying mcp_funnel_concentration_top5 from a per-scan alerting detector to a baselined metric with change-only alerting (and dedup of stable-state re-fires), versus keeping it as a recurring alert. If approved, also decide the deviation threshold (e.g., what shift in top-5 share should re-trigger) and confirm the concentration feed should be wired into the existing paywall/trial flow rather than spawning new build work.

## Triage — 2026-08-16 (spec-debt sweep #2) — CLOSED, exact re-file

Same condition as `docs/brain-proposals/agenda-70-reliability-brain-finding-mcp-funnel-concentr.md`, filed earlier and STILL OPEN as
the canonical obligation. The two differ only in the live counts embedded in
the title — identical once numbers are stripped. Closing this copy does not
close the obligation: act on agenda-70-reliability-brain-finding-mcp-funnel-concentr.md.

## Human checklist

- [x] Confirm this is still worth doing — the CONDITION is still open — this COPY is not; canonical doc is agenda-70-reliability-brain-finding-mcp-funnel-concentr.md
- [x] Scope it to a concrete change (file(s) + approach) — scoping belongs to agenda-70-reliability-brain-finding-mcp-funnel-concentr.md, which stays open
- [x] Implement + verify — not applicable to a duplicate — implement against agenda-70-reliability-brain-finding-mcp-funnel-concentr.md
- [x] Or close this PR if superseded / not worth it — closed 2026-08-16 as an exact re-file of agenda-70-reliability-brain-finding-mcp-funnel-concentr.md (spec-debt sweep #2)