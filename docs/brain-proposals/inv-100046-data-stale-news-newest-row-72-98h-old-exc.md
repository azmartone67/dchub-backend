<!-- fingerprint:fc2e334e4284273ee058ba41d5e29ff1 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — data_stale: 'news' — newest row 72.98h old — exceeds SLA 24h (observed at: dchub://data/news). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it? If there is no mechanical fix, say so plainly and explain why.

> Auto-captured from an **approved** brain inv item (#100046). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-08T19:20:52.778200Z · inv #100046_

## The approved recommendation

Approve a 15-minute diagnostic before any code change: (1) dump dchub-scheduler.py JOBS and identify whether the news refresh job exists and whether it is one of the 2 jobs colliding at '40 5 * * *'; (2) check Railway logs for that job's last run. Then choose: (a) if it's the collision — apply the one-line cron-string change in dchub-scheduler.py (the single-file fix); (b) if the job runs but writes nothing — this is a logic bug in the refresh function, not a mechanical fix, and needs a real PR; (c) if upstream is down — open a provider-outage track instead.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
