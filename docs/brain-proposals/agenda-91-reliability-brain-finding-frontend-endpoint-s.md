<!-- fingerprint:609572940ddb105e54ccff9a448033bd -->
# Brain proposal — [reliability] Brain finding: frontend_endpoint_slow @ /construction-pipeline (value 6,642)

> Auto-captured from an **approved** brain agenda item (#91). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-13T05:25:29.155482Z · agenda #91_

## The approved recommendation

Decide whether to (a) authorize a profiling pass on the shared pipeline-data path behind /construction-pipeline and /ai-pipeline (with caching/pagination as the likely fix), or (b) first pull HEALTH_BASELINE.md's frontend_endpoint_slow threshold plus recent deploy/change history to rule out an infrastructure or deployment regression before touching application code.

## Triage — 2026-08-16 (spec-debt sweep #2) — CLOSED, exact re-file

Same condition as `docs/brain-proposals/agenda-72-reliability-brain-finding-frontend-endpoint-s.md`, filed earlier and STILL OPEN as
the canonical obligation. The two differ only in the live counts embedded in
the title — identical once numbers are stripped. Closing this copy does not
close the obligation: act on agenda-72-reliability-brain-finding-frontend-endpoint-s.md.

## Human checklist

- [x] Confirm this is still worth doing — the CONDITION is still open — this COPY is not; canonical doc is agenda-72-reliability-brain-finding-frontend-endpoint-s.md
- [x] Scope it to a concrete change (file(s) + approach) — scoping belongs to agenda-72-reliability-brain-finding-frontend-endpoint-s.md, which stays open
- [x] Implement + verify — not applicable to a duplicate — implement against agenda-72-reliability-brain-finding-frontend-endpoint-s.md
- [x] Or close this PR if superseded / not worth it — closed 2026-08-16 as an exact re-file of agenda-72-reliability-brain-finding-frontend-endpoint-s.md (spec-debt sweep #2)