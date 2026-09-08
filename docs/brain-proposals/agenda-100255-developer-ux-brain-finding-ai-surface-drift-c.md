<!-- fingerprint:39ed1e7d3f7c687abbcbd1532da36a86 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: ai_surface_drift:chatgpt_instructions:stale_value @ https://dchub.cloud/int

> Auto-captured from an **approved** brain agenda item (#100255). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-08T17:24:08.943597Z · agenda #100255_

## The approved recommendation

In ai_surface_sentinel.py, trace the 'update-from-canon' path (line 158) to its canon source and refactor the chatgpt_instructions() endpoint in main.py:26336 to render instructions.txt from that same canon dict at serve-time, then extend the pattern to the mcp_json/server_card/agents_md surfaces so the *_period and *_tier fields are generated rather than stored.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
