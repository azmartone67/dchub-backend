# Brain proposal — [reliability] Brain finding: x_publisher_dead @ table:social_media_posts (seen x145)

> Auto-captured from an **approved** brain agenda item (#71). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-09T06:28:25.926188Z · agenda #71_

## The approved recommendation

Decide whether to (a) perform the one-time X developer portal fix (attach the app to a Project) yourself now, and (b) approve the structural changes: persist publish attempt/error status on social_media_posts rows, add twitter_id landing-verification as the publisher success signal, and collapse x_publisher_dead into a single escalating finding instead of one per queued post. Also decide whether to pause the X queue until the portal fix lands to stop the backlog growing.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
