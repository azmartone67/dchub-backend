<!-- fingerprint:5950ce2a33a329dfa18b47e0b09353ff -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: schema_org_coverage_low @ /api/v1/schema-org/missing (seen x76)

> Auto-captured from an **approved** brain agenda item (#100125). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-21T18:10:57.635100Z · agenda #100125_

## The approved recommendation

Choose the intervention level: (A) full fix — shared JSON-LD template layer + CI schema-validation deploy gate + batched, rate-limit-aware remediation with re-crawl-verified close (most work, stops the class); (B) loop-only fix — dedupe the detector and add backoff/verified-close so the 76-count stops churning (cheap, but coverage gaps persist); or (C) request the missing data first — dump the endpoint compliance inventory from /api/v1/schema-org/missing to confirm whether this is few-pages-re-detected vs many distinct gaps before committing engineering time.

## Triage — 2026-08-16 (spec-debt sweep #2) — CLOSED, exact re-file

Same condition as `docs/brain-proposals/agenda-88-reliability-brain-finding-schema-org-coverage.md`, filed earlier and STILL OPEN as
the canonical obligation. The two differ only in the live counts embedded in
the title — identical once numbers are stripped. Closing this copy does not
close the obligation: act on agenda-88-reliability-brain-finding-schema-org-coverage.md.

## Human checklist

- [x] Confirm this is still worth doing — the CONDITION is still open — this COPY is not; canonical doc is agenda-88-reliability-brain-finding-schema-org-coverage.md
- [x] Scope it to a concrete change (file(s) + approach) — scoping belongs to agenda-88-reliability-brain-finding-schema-org-coverage.md, which stays open
- [x] Implement + verify — not applicable to a duplicate — implement against agenda-88-reliability-brain-finding-schema-org-coverage.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-16 as an exact re-file of agenda-88-reliability-brain-finding-schema-org-coverage.md (spec-debt sweep #2)