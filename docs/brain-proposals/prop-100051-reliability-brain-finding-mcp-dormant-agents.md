<!-- fingerprint:b6a8b5779703aeb9ba1897aaf132e990 -->
# Brain proposal — [reliability] Brain finding: mcp_dormant_agents_present @ /api/v1/bots/dormant (seen x50)

> Auto-captured from an **approved** brain prop item (#100051). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-17T20:15:27.753786Z · prop #100051_

## The approved recommendation

Approve converting mcp_dormant_agents_present from a re-firing per-instance finding into a stateful lifecycle cohort with (a) pre-dormancy warning tier, (b) automated winback via the existing bind_email channel, and (c) substance-gated closure — OR decide that agent dormancy at this churn rate (99.3% calls→distinct drop) is acceptable natural attrition and downgrade/suppress the detector for all but a defined high-value cohort.

## Triage — 2026-08-16 (spec-debt sweep #2) — CLOSED, exact re-file

Same condition as `docs/brain-proposals/agenda-89-reliability-brain-finding-mcp-dormant-agents.md`, filed earlier and STILL OPEN as
the canonical obligation. The two differ only in the live counts embedded in
the title — identical once numbers are stripped. Closing this copy does not
close the obligation: act on agenda-89-reliability-brain-finding-mcp-dormant-agents.md.

## Human checklist

- [x] Confirm this is still worth doing — the CONDITION is still open — this COPY is not; canonical doc is agenda-89-reliability-brain-finding-mcp-dormant-agents.md
- [x] Scope it to a concrete change (file(s) + approach) — scoping belongs to agenda-89-reliability-brain-finding-mcp-dormant-agents.md, which stays open
- [x] Implement + verify — not applicable to a duplicate — implement against agenda-89-reliability-brain-finding-mcp-dormant-agents.md
- [x] Or close this PR if superseded / not worth it — closed 2026-08-16 as an exact re-file of agenda-89-reliability-brain-finding-mcp-dormant-agents.md (spec-debt sweep #2)