<!-- fingerprint:139cc51bbda9221a7a266713adb1ed70 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 1 public page(s) never finish redirecting — observed from the anon seat on contract: /pricing never lands — redirect loop; 8 page(s) landed, 0 unreachable What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100681). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-20T11:12:11.188712Z · inv #100681_

## The approved recommendation

Capture the full HTTP redirect chain (status codes + Location headers) for GET /pricing from an anonymous session, then read the handler and any before_request/auth guard at main.py:40583 to identify the hop that redirects back to /pricing and short-circuit it to render 200 for anon users.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
