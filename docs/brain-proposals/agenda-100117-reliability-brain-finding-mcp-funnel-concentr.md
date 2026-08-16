<!-- fingerprint:d5cec62abe3f7d66978af55697d40c3f -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: mcp_funnel_concentration_top5 @ /api/v1/mcp/funnel (seen x604)

> Auto-captured from an **approved** brain agenda item (#100117). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-19T12:11:01.444793Z · agenda #100117_

## The approved recommendation

Choose one: (A) approve refactoring mcp_funnel_concentration_top5 into a delta/baseline-triggered detector on bot-filtered traffic with substance-gated closure tied to the DDDDD auto-trial rollout, (B) demote it entirely from a finding to a weekly KPI report line with no alerting, or (C) keep it firing as-is as an intentional standing reminder and accept the 604-count noise in the worklist.

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