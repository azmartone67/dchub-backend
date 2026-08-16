<!-- fingerprint:4df5053bc42dd184c1d206fb0bbd1887 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — work-plan: lift the '(unknown)' fix-class

> Auto-captured from an **approved** brain agenda item (#100178). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-07T03:21:13.905597Z · agenda #100178_

## The approved recommendation

Approve building an ingest-time class/provider normalization gate (canonical alias table + resolve-or-quarantine, plus a 'no verification with unresolved class' rule) as the single systemic fix, versus continuing per-instance cleanup of '(unknown)' records — and decide whether the ~4,100 existing unknown-provider records get a one-time backfill/enrichment pass as part of the same effort.

## Triage — 2026-08-16 (spec-debt sweep #2) — CLOSED, exact re-file

Same condition as `docs/brain-proposals/agenda-100135-work-plan-lift-the-unknown-fix-class.md`, filed earlier and STILL OPEN as
the canonical obligation. The two differ only in the live counts embedded in
the title — identical once numbers are stripped. Closing this copy does not
close the obligation: act on agenda-100135-work-plan-lift-the-unknown-fix-class.md.

## Human checklist

- [x] Confirm this is still worth doing — the CONDITION is still open — this COPY is not; canonical doc is agenda-100135-work-plan-lift-the-unknown-fix-class.md
- [x] Scope it to a concrete change (file(s) + approach) — scoping belongs to agenda-100135-work-plan-lift-the-unknown-fix-class.md, which stays open
- [x] Implement + verify — not applicable to a duplicate — implement against agenda-100135-work-plan-lift-the-unknown-fix-class.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-16 as an exact re-file of agenda-100135-work-plan-lift-the-unknown-fix-class.md (spec-debt sweep #2)