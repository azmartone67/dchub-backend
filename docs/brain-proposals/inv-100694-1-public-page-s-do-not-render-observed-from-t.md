<!-- fingerprint:ba608dcf42e7f5930ec9f746e5c94459 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 1 public page(s) do not render — observed from the anon seat on web: /pricing -> HTTP 503, 622b What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100694). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-21T23:04:51.719917Z · inv #100694_

## The approved recommendation

Pull the current runtime logs and detector status for the /pricing GET handler at main.py:41236 and check whether render_pipeline_blocked / execution_failed (the signature behind brain_findings/8989 and /9081) is firing for that route; confirm root cause there before any code change.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
