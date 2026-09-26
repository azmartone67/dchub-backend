<!-- fingerprint:a915c9ce7cb5d66c3d038cb21fb91e92 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — site_sentinel_unhealthy:/mcp#workos-oauth-challenge (observed at: https://dchub.cloud/mcp#workos-oauth-challenge). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100214). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-18T19:13:35.682597Z · inv #100214_

## The approved recommendation

Decide whether to set DCHUB_OAUTH_CHALLENGE_DISABLE=0 on dchub-mcp-server (restoring the anon 401 OAuth challenge and durable identity) — weighing that against any intentional reason the challenge was disabled, such as reducing friction for anonymous agent traffic. This is an ops/config change; no code find-and-replace fix applies.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-84-reliability-brain-finding-site-sentinel-unhea.md`, which stays
OPEN as the single obligation for `site_sentinel_unhealthy`. This doc's target —
`/mcp#workos-oauth-challenge @ https://dchub.cloud/mcp#workos-oauth-challenge` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-84-reliability-brain-finding-site-sentinel-unhea.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-84-reliability-brain-finding-site-sentinel-unhea.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-84-reliability-brain-finding-site-sentinel-unhea.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-84-reliability-brain-finding-site-sentinel-unhea.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of agenda-84-reliability-brain-finding-site-sentinel-unhea.md (class collapse)