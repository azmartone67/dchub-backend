<!-- fingerprint:421fb0fc279c4de87a52dd8dc4f7e0ec -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 1 public page(s) never finish redirecting — observed from the anon seat on contract: /press never lands — redirect loop; 8 page(s) landed, 0 unreachable What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100030). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-06T22:38:07.999962Z · inv #100030_

## The approved recommendation

Approve running the diagnostic trace (curl -sIL https://dchub.cloud/press) and, if it shows the fallback loop, trigger `gh workflow run press-rss.yml` in dchub-frontend to rebake press.html — versus escalating straight to an edit of the frontend rewrite rules. Also explicitly confirm the sentinel manifest should NOT be relaxed for /press.

## Triage — 2026-08-16 (spec-debt sweep #2) — CLOSED, exact re-file

Same condition as `docs/brain-proposals/inv-100028-1-public-page-s-never-finish-redirecting-obse.md`, filed earlier and STILL OPEN as
the canonical obligation. The two differ only in the live counts embedded in
the title — identical once numbers are stripped. Closing this copy does not
close the obligation: act on inv-100028-1-public-page-s-never-finish-redirecting-obse.md.

## Human checklist

- [x] Confirm this is still worth doing — the CONDITION is still open — this COPY is not; canonical doc is inv-100028-1-public-page-s-never-finish-redirecting-obse.md
- [x] Scope it to a concrete change (file(s) + approach) — scoping belongs to inv-100028-1-public-page-s-never-finish-redirecting-obse.md, which stays open
- [x] Implement + verify — not applicable to a duplicate — implement against inv-100028-1-public-page-s-never-finish-redirecting-obse.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-16 as an exact re-file of inv-100028-1-public-page-s-never-finish-redirecting-obse.md (spec-debt sweep #2)