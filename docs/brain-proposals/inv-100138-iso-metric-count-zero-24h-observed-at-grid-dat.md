<!-- fingerprint:e2f4a9d4c60769922bfac54832588da9 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — iso_metric_count_zero_24h (observed at: grid_data: iso=LGEE). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100138). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-15T07:40:55.102832Z · inv #100138_

## The approved recommendation

Decide whether to (a) pull the LGEE collector workflow's last-run logs to confirm root cause (upstream 4xx vs code error vs schedule stall), then (b) restart the collector and authorize a 24h grid_data backfill for iso=LGEE, or (c) defer if the 2026-07-12 fix is confirmed to already cover LGEE and the finding is stale. No code change is recommended until logs identify the failure mode — no mechanical fix is proposed because no verbatim source text is available to edit safely.

## Rolled-up targets — class `iso_metric_count_zero_24h` (class collapse, 2026-09-26)

This doc is now the single obligation for **29 occurrences** of
`iso_metric_count_zero_24h`. The other 28 were closed against it. They are listed here
in full so the collapse loses no target — fixing the class means fixing
every line below, and a fix that only covers this doc's own target has not
discharged the obligation.

- `grid_data: iso=LGEE` — was `inv-100138-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-08-15)
- `grid_data: iso=EU_IT_NORD` — was `inv-100441-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-02)
- `grid_data: iso=EU_IT_SICI` — was `inv-100445-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-02)
- `grid_data: iso=EU_RO` — was `inv-100447-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-02)
- `grid_data: iso=EU_HU` — was `inv-100451-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-02)
- `grid_data: iso=EU_DK_1` — was `inv-100457-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-03)
- `grid_data: iso=SOCO` — was `inv-100476-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-03)
- `grid_data: iso=GVL` — was `inv-100477-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-03)
- `grid_data: iso=PACE` — was `inv-100478-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-03)
- `grid_data: iso=NEVP` — was `inv-100479-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-03)
- `grid_data: iso=FPL` — was `inv-100480-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-03)
- `grid_data: iso=PNM` — was `inv-100481-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-03)
- `grid_data: iso=DUK` — was `inv-100482-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-03)
- `grid_data: iso=GCPD` — was `inv-100483-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-03)
- `grid_data: iso=SC` — was `inv-100484-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-03)
- `grid_data: iso=PSCO` — was `inv-100485-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-03)
- `grid_data: iso=AECI` — was `inv-100486-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-03)
- `grid_data: iso=TIDC` — was `inv-100489-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-03)
- `grid_data: iso=SEC` — was `inv-100490-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-03)
- `grid_data: iso=WAUW` — was `inv-100507-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-04)
- `grid_data: iso=TAL` — was `inv-100535-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-05)
- `grid_data: iso=PJM` — was `inv-100537-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-05)
- `grid_data: iso=TVA` — was `inv-100541-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-05)
- `grid_data: iso=SCEG` — was `inv-100544-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-05)
- `grid_data: iso=FPC` — was `inv-100545-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-05)
- `grid_data: iso=EU_IE_SEM` — was `inv-100556-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-07)
- `grid_data: iso=CPLE` — was `inv-100561-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-07)
- `grid_data: iso=EU_BE` — was `inv-100603-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-11)
- `grid_data: iso=WACM` — was `inv-100604-iso-metric-count-zero-24h-observed-at-grid-dat.md` (filed 2026-09-11)

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
