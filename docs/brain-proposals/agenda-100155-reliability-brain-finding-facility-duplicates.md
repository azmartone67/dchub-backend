<!-- fingerprint:deacbdf3e562ddf7e1747ba561344438 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: facility_duplicates_unmarked @ /api/v1/admin/facility-dedup/analyze?country

> Auto-captured from an **approved** brain agenda item (#100155). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-31T05:47:30.122993Z · agenda #100155_

## The approved recommendation

Approve one of: (A) schedule facility-dedup apply (all countries, confirm=1) as an automatic post-ingest cron job with auto-merge above the current match threshold, (B) same schedule but merges queued for human confirmation, or (C) invest further and add write-time duplicate flagging at ingestion plus provider/country normalization. Also decide whether the 21 outstanding US duplicates get one manual apply run now while the systemic fix is built.

## Rolled-up targets — class `facility_duplicates_unmarked` (class collapse, 2026-08-17)

This doc is now the single obligation for **6 occurrences** of
`facility_duplicates_unmarked`. The other 5 were closed against it. They are listed here
in full so the collapse loses no target — fixing the class means fixing
every line below, and a fix that only covers this doc's own target has not
discharged the obligation.

- `/api/v1/admin/facility-dedup/analyze?country` — was `agenda-100155-reliability-brain-finding-facility-duplicates.md` (filed 2026-07-31)
- `/api/v1/admin/facility-dedup/analyze?country=US` — was `inv-100068-facility-duplicates-unmarked-observed-at-api.md` (filed 2026-08-10)
- `/api/v1/admin/facility-dedup/analyze?country=DE` — was `inv-100082-facility-duplicates-unmarked-observed-at-api.md` (filed 2026-08-10)
- `/api/v1/admin/facility-dedup/analyze?country=AU` — was `inv-100142-facility-duplicates-unmarked-observed-at-api.md` (filed 2026-08-15)
- `/api/v1/admin/facility-dedup/analyze?country=FR` — was `inv-100153-facility-duplicates-unmarked-observed-at-api.md` (filed 2026-08-15)
- `/api/v1/admin/facility-dedup/analyze?country=SG` — was `inv-100164-facility-duplicates-unmarked-observed-at-api.md` (filed 2026-08-16)

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
