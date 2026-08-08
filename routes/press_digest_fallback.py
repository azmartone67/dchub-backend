"""press_digest_fallback.py — the /news loader must never dead-end (2026-07-18).

WHY THIS EXISTS
===============
The edge worker renders /news/<slug> by fetching /api/press-releases/<slug>;
any non-OK backend response falls through to a hard 404 page ("Digest Not
Found: Could not load digest for \"partnership-partners-2026-w23\""). The
first generation of the pending-drafts digest email linked drafts at their
natural /news/<slug> URL — but that API only serves published=TRUE rows, so
every draft link (and any expired/drifted digest id) was a dead end. The link
builder was already fixed to the token-gated preview (commit ac91e49e); this
module fixes the LOADER side so no historical email link, stale digest id, or
malformed slug can ever land on a dead end again:

  * /api/press-releases/<slug> miss  → 200 with the LATEST news digest
    payload (+ a note naming the requested slug; drafts are flagged
    "pending editorial review" WITHOUT leaking any draft content).
  * /api/press-releases/digest-<bad-or-empty date> → resolves to the latest
    date that actually has articles instead of 400 / an empty page.

The worker's digest renderer keys on the payload having no body/subheadline,
so a fallback payload renders as a normal digest page at the edge.

All helpers take a plain DB cursor (or None) and never raise — a DB blip
degrades to an empty-but-valid digest payload, still never a dead end.
"""
from __future__ import annotations

import re
import logging
import datetime

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def latest_digest_date(cur) -> str | None:
    """Most recent announcements date that actually has articles. None on
    any failure — callers degrade gracefully."""
    if cur is None:
        return None
    try:
        # ★ Never surface a FUTURE-dated digest (2026-08-07): the news feed
        # carries future-dated rows, so an unclamped MAX(published_date)
        # rendered a "September 21, 2026" digest for a missing/draft press
        # link — which reads as broken/amateur. Clamp to <= today so the
        # fallback only ever shows a real, already-published digest.
        cur.execute(
            "SELECT LEFT(published_date, 10) AS d FROM announcements "
            "WHERE published_date IS NOT NULL "
            "AND LEFT(published_date, 10) <= %s "
            "GROUP BY 1 ORDER BY 1 DESC LIMIT 1",
            (datetime.date.today().strftime("%Y-%m-%d"),))
        row = cur.fetchone()
        return str(row[0]) if row and row[0] else None
    except Exception as e:
        logger.warning("[digest_fallback] latest date lookup failed: %s", str(e)[:120])
        return None


def _articles_for_date(cur, d: str) -> list[dict]:
    if cur is None:
        return []
    try:
        cur.execute(
            "SELECT id, title, summary, source_url, source, category, "
            "       published_date::text, image_url "
            "FROM announcements WHERE LEFT(published_date, 10) = %s "
            "ORDER BY published_date DESC LIMIT 200", (d,))
        return [{"id": r[0], "title": r[1], "summary": r[2] or "",
                 "url": r[3] or "", "source": r[4] or "",
                 "category": r[5] or "General", "published_at": str(r[6] or ""),
                 "image_url": r[7] or "", "author": ""}
                for r in cur.fetchall()]
    except Exception as e:
        logger.warning("[digest_fallback] articles(%s) failed: %s", d, str(e)[:120])
        return []


def resolve_digest(cur, requested_date: str | None, note: str | None = None) -> dict:
    """Build the digest payload for `requested_date`, falling back to the
    latest date that has articles when the requested date is malformed,
    missing, or empty (expired retention). Same payload shape as the
    historical /api/press-releases/digest-<date> route, + `note`/`fallback`
    fields when a fallback happened. Never raises."""
    d = (requested_date or "").strip()
    fallback_reason = None
    if not _DATE_RE.match(d):
        fallback_reason = f"unresolvable digest id {d!r}" if d else "no date given"
        d = ""

    articles: list[dict] = []
    if d:
        articles = _articles_for_date(cur, d)
        if not articles:
            fallback_reason = f"no articles retained for {d}"

    if not articles:
        latest = latest_digest_date(cur)
        if latest and latest != d:
            articles = _articles_for_date(cur, latest)
            if articles:
                d = latest
    if not d:
        d = datetime.date.today().strftime("%Y-%m-%d")

    try:
        dt = datetime.datetime.strptime(d, "%Y-%m-%d").date()
    except Exception:
        dt = datetime.date.today()
        d = dt.strftime("%Y-%m-%d")

    cats: dict = {}
    srcs: dict = {}
    for a in articles:
        cats[a["category"]] = cats.get(a["category"], 0) + 1
        srcs[a["source"]] = srcs.get(a["source"], 0) + 1
    prev = (dt - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    nxt = (dt + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    payload = {
        "success": True,
        "slug": f"digest-{d}",
        "date": d,
        "display_date": dt.strftime("%B %d, %Y"),
        "total": len(articles),
        "articles": articles,
        "categories": cats,
        "sources": srcs,
        "nav": {"prev": f"/news/digest-{prev}", "next": f"/news/digest-{nxt}"},
        "backend": "neon/announcements" if articles else "empty",
    }
    if fallback_reason:
        payload["fallback"] = "latest-digest"
        payload["note"] = (note or "") + (
            f"{' — ' if note else ''}{fallback_reason}; showing the latest digest instead")
    elif note:
        payload["note"] = note
    return payload


def slug_fallback_payload(cur, slug: str) -> dict:
    """Fallback for /api/press-releases/<slug> when no PUBLISHED row matches.
    Returns the latest digest payload (200 at the route) so the edge renders a
    digest page instead of the 'Digest Not Found' dead end. If the slug is an
    UNPUBLISHED draft, the note says so — draft CONTENT is never included
    (drafts are only viewable through the token-gated preview). Never raises."""
    note = f"'{slug}' is not a published release"
    try:
        if cur is not None:
            cur.execute(
                "SELECT 1 FROM press_releases WHERE slug = %s AND published = FALSE",
                (slug,))
            if cur.fetchone():
                note = (f"'{slug}' is a draft pending editorial review — "
                        "it will appear here once approved")
    except Exception as e:
        logger.warning("[digest_fallback] draft check failed: %s", str(e)[:120])

    payload = resolve_digest(cur, None, note=note)
    payload["requested_slug"] = slug
    payload["not_found"] = True
    return payload
