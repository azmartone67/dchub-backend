<!-- fingerprint:294441f6964b0054f6a99af067015a88 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — iso_metric_count_dropped (observed at: grid_data: iso=CHPD). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100493). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-03T21:35:36.081528Z · inv #100493_

## The approved recommendation

Open routes/iso_orchestrator.py and resolve the aggregate feed code that registers CHPD's metrics, then pull the last-24h ingestion logs for that resolved loop across CHPD, TIDC, TAL, SCL, and GCPD to confirm whether the partial-write failure is a shared upstream API error or per-ISO.

## Rolled-up targets — class `iso_metric_count_dropped` (class collapse, 2026-09-26)

This doc is now the single obligation for **12 occurrences** of
`iso_metric_count_dropped`. The other 11 were closed against it. They are listed here
in full so the collapse loses no target — fixing the class means fixing
every line below, and a fix that only covers this doc's own target has not
discharged the obligation.

- `grid_data: iso=CHPD` — was `inv-100493-iso-metric-count-dropped-observed-at-grid-data.md` (filed 2026-09-03)
- `grid_data: iso=TPWR` — was `inv-100518-iso-metric-count-dropped-observed-at-grid-data.md` (filed 2026-09-04)
- `grid_data: iso=SCL` — was `inv-100523-iso-metric-count-dropped-observed-at-grid-data.md` (filed 2026-09-05)
- `grid_data: iso=DOPD` — was `inv-100568-iso-metric-count-dropped-observed-at-grid-data.md` (filed 2026-09-07)
- `grid_data: iso=SPA` — was `inv-100569-iso-metric-count-dropped-observed-at-grid-data.md` (filed 2026-09-07)
- `grid_data: iso=SEC` — was `inv-100570-iso-metric-count-dropped-observed-at-grid-data.md` (filed 2026-09-07)
- `grid_data: iso=GCPD` — was `inv-100572-iso-metric-count-dropped-observed-at-grid-data.md` (filed 2026-09-07)
- `grid_data: iso=TAL` — was `inv-100573-iso-metric-count-dropped-observed-at-grid-data.md` (filed 2026-09-07)
- `grid_data: iso=TIDC` — was `inv-100575-iso-metric-count-dropped-observed-at-grid-data.md` (filed 2026-09-07)
- `grid_data: iso=TEC` — was `inv-100579-iso-metric-count-dropped-observed-at-grid-data.md` (filed 2026-09-07)
- `grid_data: iso=GVL` — was `inv-100583-iso-metric-count-dropped-observed-at-grid-data.md` (filed 2026-09-08)
- `grid_data: iso=EPE` — was `inv-100610-iso-metric-count-dropped-observed-at-grid-data.md` (filed 2026-09-11)

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
