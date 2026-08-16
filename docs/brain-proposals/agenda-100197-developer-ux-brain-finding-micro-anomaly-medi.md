<!-- fingerprint:a8e05a017a4f56114a26e477b9a5c73f -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: micro_anomaly_medium @ /api/v1/admin/brain/micro-cycle/recent (seen x1)

> Auto-captured from an **approved** brain agenda item (#100197). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-16T03:53:09.207009Z · agenda #100197_

## The approved recommendation

Choose one: (a) approve adding a minimum-sample precondition to the micro-cycle anomaly detector (suppress/downgrade anomaly findings when window call volume is below a floor, e.g., re-labeled as insufficient-data), (b) defer any change and only act if micro_anomaly_medium fires a second time, or (c) treat the single 200 call on an admin route as a possible access anomaly and request the raw request record before deciding. Option (a) is the structural fix; you must also set the volume floor value, which is not derivable from current evidence.

## Triage — 2026-08-16 (spec-debt sweep #2) — CLOSED, exact re-file

Same condition as `docs/brain-proposals/agenda-86-developer-ux-brain-finding-micro-anomaly-medi.md`, filed earlier and STILL OPEN as
the canonical obligation. The two differ only in the live counts embedded in
the title — identical once numbers are stripped. Closing this copy does not
close the obligation: act on agenda-86-developer-ux-brain-finding-micro-anomaly-medi.md.

## Human checklist

- [x] Confirm this is still worth doing — the CONDITION is still open — this COPY is not; canonical doc is agenda-86-developer-ux-brain-finding-micro-anomaly-medi.md
- [x] Scope it to a concrete change (file(s) + approach) — scoping belongs to agenda-86-developer-ux-brain-finding-micro-anomaly-medi.md, which stays open
- [x] Implement + verify — not applicable to a duplicate — implement against agenda-86-developer-ux-brain-finding-micro-anomaly-medi.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-16 as an exact re-file of agenda-86-developer-ux-brain-finding-micro-anomaly-medi.md (spec-debt sweep #2)