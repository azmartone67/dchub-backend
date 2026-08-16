<!-- fingerprint:ea39ea9edaca5e5efb183bf4989181a0 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — glama advertises 2,000 deals (canon 1,845) — observed from the none seat on registry: listing prose says 2,000+ deals; the platform's own live canon says 1,845 (over) What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100145). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-15T07:42:43.048986Z · inv #100145_

## The approved recommendation

Choose: (a) confirm PR #187's sync already refreshed the Glama listing and close as fixed; (b) if not, approve the minimal change — swap the hard-coded '2,000+ deals' in the registry listing source for the live deduped deals_phrase() value and add '2,000+ deals' to PINNED['stale_markers']; or (c) first commission a deal-ID diff (advertised set vs canon) to check whether the dedup filter is instead under-counting the canon, which would move the fix to the canon side.

## Triage — 2026-08-16 (spec-debt sweep #2) — CLOSED, exact re-file

Same condition as `docs/brain-proposals/inv-100075-glama-advertises-2-000-deals-canon-1-757-obs.md`, filed earlier and STILL OPEN as
the canonical obligation. The two differ only in the live counts embedded in
the title — identical once numbers are stripped. Closing this copy does not
close the obligation: act on inv-100075-glama-advertises-2-000-deals-canon-1-757-obs.md.

## Human checklist

- [x] Confirm this is still worth doing — the CONDITION is still open — this COPY is not; canonical doc is inv-100075-glama-advertises-2-000-deals-canon-1-757-obs.md
- [x] Scope it to a concrete change (file(s) + approach) — scoping belongs to inv-100075-glama-advertises-2-000-deals-canon-1-757-obs.md, which stays open
- [x] Implement + verify — not applicable to a duplicate — implement against inv-100075-glama-advertises-2-000-deals-canon-1-757-obs.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-16 as an exact re-file of inv-100075-glama-advertises-2-000-deals-canon-1-757-obs.md (spec-debt sweep #2)