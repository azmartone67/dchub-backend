"""
Railway FastAPI publish routes — updated to:
  1. Require a shared Bearer secret (set RAILWAY_PUBLISH_SECRET)
  2. Write every digest to Neon `news_digests` (archive + audit trail)
  3. Post to LinkedIn (versioned header auto-generated from current month)
  4. Return a structured result the Worker proxy can forward back to the cron

Drop-in replacement for the existing publish_routes.py. Keeps the
`/publish/all` path the same so the Worker proxy doesn't need to change.

Requires:
  pip install fastapi psycopg[binary] httpx pydantic
  env: DATABASE_URL, RAILWAY_PUBLISH_SECRET, LINKEDIN_ACCESS_TOKEN, LINKEDIN_ACTOR_URN
"""

from __future__ import annotations

# phase57_landing — daily landing URL helper for LinkedIn rich-card preview
def _phase30c_landing_url(d=None):
    """Return canonical /api/v1/social/posts/<date> URL for LinkedIn OG card."""
    import datetime
    if d is None:
        d = datetime.date.today()
    return f"https://dchub.cloud/api/v1/social/posts/{d.isoformat()}"


import os
import json
import logging
from datetime import date, datetime
from typing import Optional

import httpx
import psycopg
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

log = logging.getLogger("dc-hub.publish")
router = APIRouter(prefix="/publish", tags=["publish"])

DATABASE_URL = os.environ["DATABASE_URL"]
PUBLISH_SECRET = os.environ.get("RAILWAY_PUBLISH_SECRET", "")
LINKEDIN_TOKEN = os.environ.get("LINKEDIN_ACCESS_TOKEN")
LINKEDIN_ACTOR = os.environ.get("LINKEDIN_ACTOR_URN")  # e.g. "urn:li:organization:12345"


class DigestPayload(BaseModel):
    slug: str = Field(..., pattern=r"^[a-z0-9-]+$")
    digest_date: date
    title: str
    html: str
    markdown: Optional[str] = None
    linkedin_text: Optional[str] = None
    story_count: Optional[int] = None
    categories: Optional[dict] = None
    sources: Optional[list] = None
    run_source: str = "cron"
    get_news_ok: bool = False
    error_notes: Optional[str] = None


def _require_auth(authorization: str) -> None:
    if not PUBLISH_SECRET:
        raise HTTPException(500, "publish_secret_not_configured")
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if token != PUBLISH_SECRET:
        raise HTTPException(401, "unauthorized")


def _linkedin_version_header() -> str:
    # LinkedIn Marketing API requires YYYYMM of the current month
    return datetime.utcnow().strftime("%Y%m")


async def _post_to_linkedin(text: str) -> tuple[bool, Optional[str], Optional[str]]:
    if not (LINKEDIN_TOKEN and LINKEDIN_ACTOR and text):
        return (False, None, "linkedin_not_configured")
    body = {
        "author": LINKEDIN_ACTOR,
        "commentary": text,
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED"},
        "lifecycleState": "PUBLISHED",
    }
    headers = {
        "Authorization": f"Bearer {LINKEDIN_TOKEN}",
        "Content-Type": "application/json",
        "LinkedIn-Version": _linkedin_version_header(),
        "X-Restli-Protocol-Version": "2.0.0",
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://api.linkedin.com/rest/posts",
                headers=headers,
                json=body,
            )
        if r.status_code in (200, 201):
            urn = r.headers.get("x-restli-id") or ""
            return (True, urn, None)
        return (False, None, f"linkedin_{r.status_code}: {r.text[:200]}")
    except Exception as e:  # noqa: BLE001
        return (False, None, f"linkedin_exception: {e}")


def _upsert_digest(payload: DigestPayload, publish_railway: bool,
                   publish_linkedin: bool, linkedin_urn: Optional[str],
                   error_notes: Optional[str]) -> None:
    sql = """
    INSERT INTO news_digests (
      slug, digest_date, title, html, markdown, linkedin_text,
      story_count, categories, sources, run_source, get_news_ok,
      publish_railway, publish_linkedin, linkedin_post_id, error_notes
    ) VALUES (
      %(slug) ON CONFLICT DO NOTHINGs, %(digest_date)s, %(title)s, %(html)s, %(markdown)s, %(linkedin_text)s,
      %(story_count)s, %(categories)s, %(sources)s, %(run_source)s, %(get_news_ok)s,
      %(publish_railway)s, %(publish_linkedin)s, %(linkedin_post_id)s, %(error_notes)s
    )
    ON CONFLICT (slug) DO UPDATE SET
      title            = EXCLUDED.title,
      html             = EXCLUDED.html,
      markdown         = EXCLUDED.markdown,
      linkedin_text    = EXCLUDED.linkedin_text,
      story_count      = EXCLUDED.story_count,
      categories       = EXCLUDED.categories,
      sources          = EXCLUDED.sources,
      get_news_ok      = EXCLUDED.get_news_ok,
      publish_railway  = news_digests.publish_railway OR EXCLUDED.publish_railway,
      publish_linkedin = news_digests.publish_linkedin OR EXCLUDED.publish_linkedin,
      linkedin_post_id = COALESCE(EXCLUDED.linkedin_post_id, news_digests.linkedin_post_id),
      error_notes      = EXCLUDED.error_notes
    ;
    """
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {
                "slug": payload.slug,
                "digest_date": payload.digest_date,
                "title": payload.title,
                "html": payload.html,
                "markdown": payload.markdown,
                "linkedin_text": payload.linkedin_text,
                "story_count": payload.story_count,
                "categories": json.dumps(payload.categories) if payload.categories else None,
                "sources": json.dumps(payload.sources) if payload.sources else None,
                "run_source": payload.run_source,
                "get_news_ok": payload.get_news_ok,
                "publish_railway": publish_railway,
                "publish_linkedin": publish_linkedin,
                "linkedin_post_id": linkedin_urn,
                "error_notes": error_notes,
            })
        conn.commit()


@router.post("/all")
async def publish_all(payload: DigestPayload,
                      request: Request,
                      authorization: str = Header(default="")):
    _require_auth(authorization)

    log.info("publish received slug=%s date=%s source=%s",
             payload.slug, payload.digest_date, payload.run_source)

    # 1) LinkedIn
    li_ok, li_urn, li_err = await _post_to_linkedin(payload.linkedin_text or "")

    # 2) Neon archive (source of truth)
    try:
        _upsert_digest(
            payload,
            publish_railway=True,
            publish_linkedin=li_ok,
            linkedin_urn=li_urn,
            error_notes=li_err,
        )
        neon_ok = True
    except Exception as e:  # noqa: BLE001
        log.exception("neon upsert failed")
        neon_ok = False
        li_err = (li_err + " | " if li_err else "") + f"neon_error: {e}"

    return {
        "success": neon_ok,
        "slug": payload.slug,
        "neon_ok": neon_ok,
        "linkedin_ok": li_ok,
        "linkedin_urn": li_urn,
        "error_notes": li_err,
        "ts": datetime.utcnow().isoformat() + "Z",
    }


@router.get("/health")
async def publish_health():
    """Quick SELECT to verify Neon connectivity + row count."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*), MAX(digest_date) FROM news_digests"
                )
                count, latest = cur.fetchone()
        return {
            "ok": True,
            "digest_count": count,
            "latest_digest_date": str(latest) if latest else None,
        }
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"neon_unreachable: {e}")
