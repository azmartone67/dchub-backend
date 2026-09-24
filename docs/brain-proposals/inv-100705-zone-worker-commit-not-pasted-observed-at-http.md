<!-- fingerprint:da5313b94ace9770fbf7a08d0a78232d -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — zone_worker_commit_not_pasted (observed at: https://dchub.cloud/mcp?_=1790211575). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100705). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-24T05:36:27.031287Z · inv #100705_

## The approved recommendation

Open the Cloudflare dashboard for the dchubapiproxy worker and paste the current merged worker.js (version 4.9.76 per PR #5413) into the script editor and deploy, then re-check GET https://dchub.cloud/api/v1/dcpi/scores?limit=1 to confirm the reported worker version matches the merged version and the worker_version_drift finding clears.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
