<!-- fingerprint:0085b1bd5079b97ae05da695fed201ab -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — get_news serves 2 FUTURE-dated record(s) — observed from the anon seat on data: worst: articles[0].published_at = '2026-09-21T11:00:00', 48 days in the future; 2 of 21 dated fields are ahead of now+6h What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100022). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-04T17:57:05.742426Z · inv #100022_

## The approved recommendation

Approve the two-part fix (read-path filter on published_at > now+6h in get_news, plus ingestion-time rejection/quarantine of future-dated articles), and decide whether to first pull the 2 raw feed payloads to confirm feed-supplied vs parser-introduced dates before merging — or ship the guard immediately and diagnose after.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
