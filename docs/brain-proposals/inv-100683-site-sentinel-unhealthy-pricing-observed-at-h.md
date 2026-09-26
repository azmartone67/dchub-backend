<!-- fingerprint:2fb1d851147d919487a00ab657a0504b -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — site_sentinel_unhealthy:/pricing (observed at: https://dchub.cloud/pricing). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100683). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-19T17:33:30.200479Z · inv #100683_

## The approved recommendation

Run `curl -i https://dchub.cloud/pricing` and capture the HTTP status, headers, and body, then grep the sentinel/health-check config for the marker it asserts on /pricing to confirm whether the failure is a status code, a missing content string, or a transient flap before touching any source file.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-84-reliability-brain-finding-site-sentinel-unhea.md`, which stays
OPEN as the single obligation for `site_sentinel_unhealthy`. This doc's target —
`/pricing @ https://dchub.cloud/pricing` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-84-reliability-brain-finding-site-sentinel-unhea.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-84-reliability-brain-finding-site-sentinel-unhea.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-84-reliability-brain-finding-site-sentinel-unhea.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-84-reliability-brain-finding-site-sentinel-unhea.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of agenda-84-reliability-brain-finding-site-sentinel-unhea.md (class collapse)