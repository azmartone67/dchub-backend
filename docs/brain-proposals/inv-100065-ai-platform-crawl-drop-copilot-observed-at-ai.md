<!-- fingerprint:f591448de6458671b20d204c04a06bb2 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — ai_platform_crawl_drop:copilot (observed at: ai_requests). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100065). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-10T14:37:50.553563Z · inv #100065_

## The approved recommendation

Decide whether to (a) open an ops investigation into the shared crawl surface (CF/WAF rules, robots.txt/llms.txt, deploy timeline vs PR #2519) treating copilot as part of a platform-wide outage, or (b) first verify the ai_requests telemetry pipeline itself is recording correctly, since a logging break would explain all per-platform drop findings at once. No mechanical code fix is proposed because no evidence identifies a single file or unique string responsible for the drop.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-74-reliability-brain-finding-ai-platform-crawl-d.md`, which stays
OPEN as the single obligation for `ai_platform_crawl_drop`. This doc's target —
`ai_requests` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-74-reliability-brain-finding-ai-platform-crawl-d.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-74-reliability-brain-finding-ai-platform-crawl-d.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-74-reliability-brain-finding-ai-platform-crawl-d.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-74-reliability-brain-finding-ai-platform-crawl-d.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-74-reliability-brain-finding-ai-platform-crawl-d.md (class collapse)