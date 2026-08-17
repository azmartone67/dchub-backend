<!-- fingerprint:b60d599ffd45996f9cd30859a5614558 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — press_drafting_lag (observed at: /dc-hub-media). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100055). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-09T19:46:51.008920Z · inv #100055_

## The approved recommendation

Approve (or run yourself) the ops action `gh workflow run press-rss.yml` in the dchub-frontend repo to re-bake the press surfaces, and decide whether to open an investigation into why the hourly bake lane went silent (and whether it shares a root cause with the cron_silently_dead cluster). No code merge is requested — no mechanical remedy block was produced because the root cause is operational, not a unique text pattern in one file.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/inv-100043-press-drafting-lag-observed-at-dc-hub-media.md`, which stays
OPEN as the single obligation for `press_drafting_lag`. This doc's target —
`/dc-hub-media` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on inv-100043-press-drafting-lag-observed-at-dc-hub-media.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is inv-100043-press-drafting-lag-observed-at-dc-hub-media.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in inv-100043-press-drafting-lag-observed-at-dc-hub-media.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against inv-100043-press-drafting-lag-observed-at-dc-hub-media.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of inv-100043-press-drafting-lag-observed-at-dc-hub-media.md (class collapse)