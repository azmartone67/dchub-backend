"""Social posting adapters — fires after /refresh renders the daily image.

Supported out of the box:
    * X  (Twitter v2 API — image upload via v1.1 media/upload, tweet via v2)
    * LinkedIn  (Posts API, image share)
    * Generic webhook  (Buffer, Zapier, Make, Publer, Typefully — anything that
                        takes JSON with an image URL)

All posts are gated on env vars — if credentials for a platform aren't set,
that platform is silently skipped. This lets you roll it out platform by
platform without code changes.

Env vars:
    X_CONSUMER_KEY / X_CONSUMER_SECRET / X_ACCESS_TOKEN / X_ACCESS_SECRET
    LINKEDIN_ACCESS_TOKEN / LINKEDIN_AUTHOR_URN   (e.g. urn:li:person:abc123)
    DAILY_WEBHOOK_URL   (plus optional DAILY_WEBHOOK_SECRET)

    AUTOPOST_THEME      a|b|c|rotate   (default rotate)
    AUTOPOST_SIZE       portrait|square|story   (default square)
    AUTOPOST_ENABLED    "1" to actually post  (default off, for safety)
"""
from __future__ import annotations

# phase57_landing — daily landing URL helper for LinkedIn rich-card preview
def _phase30c_landing_url(d=None):
    """Return canonical /api/v1/social/posts/<date> URL for LinkedIn OG card."""
    import datetime
    if d is None:
        d = datetime.date.today()
    return f"https://dchub.cloud/api/v1/social/posts/{d.isoformat()}"


import datetime
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)


# --- shared copy ------------------------------------------------------------

def daily_caption(date: datetime.date, snap: dict) -> str:
    """Short, evergreen caption for today's image. Keep under Twitter's 280."""
    states = snap.get("states", [])
    totals = {
        "op":  sum(s.get("op", 0)  for s in states),
        "uc":  sum(s.get("uc", 0)  for s in states),
        "ann": sum(s.get("ann", 0) for s in states),
    }
    top3 = sorted(states, key=lambda s: s["op"] + s["uc"] + s["ann"],
                  reverse=True)[:3]
    top_names = ", ".join(s["name"].title() for s in top3)
    total = totals["op"] + totals["uc"] + totals["ann"]
    return (
        f"U.S. Data Center Hubs — {date.isoformat()}\n\n"
        f"{total:,} facilities across the country. "
        f"Top 3: {top_names}.\n\n"
        f"{totals['op']:,} operational · {totals['uc']:,} under construction · "
        f"{totals['ann']:,} announced.\n\n"
        f"#DataCenters #Infrastructure"
    )


# --- X (Twitter) ------------------------------------------------------------

def post_to_x(image_bytes: bytes, caption: str) -> dict:
    """Post image + text to X. Requires OAuth1 user context.

    Uses Tweepy for simplicity — install with `pip install tweepy`.
    """
    ck = os.environ.get("X_CONSUMER_KEY")
    cs = os.environ.get("X_CONSUMER_SECRET")
    at = os.environ.get("X_ACCESS_TOKEN")
    ats = os.environ.get("X_ACCESS_SECRET")
    if not all((ck, cs, at, ats)):
        return {"skipped": "x", "reason": "missing credentials"}

    import tempfile
    import tweepy  # type: ignore

    auth = tweepy.OAuth1UserHandler(ck, cs, at, ats)
    api_v1 = tweepy.API(auth)
    client_v2 = tweepy.Client(
        consumer_key=ck, consumer_secret=cs,
        access_token=at, access_token_secret=ats,
    )

    # v1.1 media upload (v2 upload is in limited availability)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(image_bytes)
        path = f.name
    try:
        media = api_v1.media_upload(filename=path)
    finally:
        os.unlink(path)

    resp = client_v2.create_tweet(text=caption[:280], media_ids=[media.media_id])
    return {"posted": "x", "tweet_id": str(resp.data.get("id"))}


# --- LinkedIn ---------------------------------------------------------------

def post_to_linkedin(image_bytes: bytes, caption: str) -> dict:
    """Post image + text to LinkedIn (personal or page).

    Uses the Posts API with a registered upload. Requires w_member_social or
    w_organization_social scope.
    """
    token = os.environ.get("LINKEDIN_ACCESS_TOKEN")
    author = os.environ.get("LINKEDIN_AUTHOR_URN")
    if not token or not author:
        return {"skipped": "linkedin", "reason": "missing credentials"}

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Restli-Protocol-Version": "2.0.0",
    }
    with httpx.Client(timeout=60, headers=headers) as c:
        # 1. register an image upload
        reg = c.post(
            "https://api.linkedin.com/v2/assets?action=registerUpload",
            json={
                "registerUploadRequest": {
                    "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
                    "owner": author,
                    "serviceRelationships": [{
                        "relationshipType": "OWNER",
                        "identifier": "urn:li:userGeneratedContent",
                    }],
                }
            },
        )
        reg.raise_for_status()
        reg_data = reg.json()["value"]
        upload_url = reg_data["uploadMechanism"][
            "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"
        ]["uploadUrl"]
        asset = reg_data["asset"]

        # 2. PUT the bytes
        up = httpx.put(upload_url, headers={"Authorization": f"Bearer {token}"},
                       content=image_bytes, timeout=60)
        up.raise_for_status()

        # 3. create the share
        share = c.post("https://api.linkedin.com/v2/ugcPosts", json={
            "author": author,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": caption},
                    "shareMediaCategory": "IMAGE",
                    "media": [{
                        "status": "READY",
                        "description": {"text": "Daily data center brief"},
                        "media": asset,
                        "title": {"text": "DC Hub Daily"},
                    }],
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        })
        share.raise_for_status()
        return {"posted": "linkedin", "urn": share.headers.get("x-restli-id")}


# --- generic webhook --------------------------------------------------------

def post_to_webhook(image_url: str, caption: str, meta: dict | None = None) -> dict:
    """Fire a JSON POST to DAILY_WEBHOOK_URL.

    Use with Buffer / Zapier / Make / Publer — they pick up the image URL and
    fan out to IG / TikTok / Threads / whatever you've configured there.
    """
    url = os.environ.get("DAILY_WEBHOOK_URL")
    if not url:
        return {"skipped": "webhook", "reason": "DAILY_WEBHOOK_URL not set"}
    secret = os.environ.get("DAILY_WEBHOOK_SECRET")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Daily-Secret"] = secret
    payload = {"image_url": image_url, "caption": caption, "meta": meta or {}}
    r = httpx.post(url, json=payload, headers=headers, timeout=30)
    r.raise_for_status()
    return {"posted": "webhook", "status": r.status_code}


# --- orchestrator -----------------------------------------------------------

@dataclass
class PostResult:
    x: dict | None = None
    linkedin: dict | None = None
    webhook: dict | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def autopost(date: datetime.date, snap: dict, image_bytes: bytes,
             image_url: str) -> PostResult:
    """Fan out today's image to every configured platform.

    No-ops for any platform whose credentials aren't set.
    Gated by AUTOPOST_ENABLED to prevent accidental posts during deploys/tests.
    """
    if os.environ.get("AUTOPOST_ENABLED") != "1":
        log.info("AUTOPOST_ENABLED != 1 — skipping all social posts")
        return PostResult()

    caption = daily_caption(date, snap)
    meta = {"date": date.isoformat()}
    result = PostResult()

    try:
        result.x = post_to_x(image_bytes, caption)
    except Exception as e:  # noqa: BLE001
        log.error("x post failed: %s", e)
        result.x = {"error": str(e)[:200]}

    try:
        result.linkedin = post_to_linkedin(image_bytes, caption)
    except Exception as e:  # noqa: BLE001
        log.error("linkedin post failed: %s", e)
        result.linkedin = {"error": str(e)[:200]}

    try:
        result.webhook = post_to_webhook(image_url, caption, meta)
    except Exception as e:  # noqa: BLE001
        log.error("webhook post failed: %s", e)
        result.webhook = {"error": str(e)[:200]}

    return result


def pick_autopost_variant(date: datetime.date) -> tuple[str, str]:
    """Which theme + size to post each day (the 'default' share image)."""
    theme_cfg = os.environ.get("AUTOPOST_THEME", "rotate").lower()
    if theme_cfg == "rotate":
        theme = ["a", "b", "c"][date.toordinal() % 3]
    elif theme_cfg in ("a", "b", "c"):
        theme = theme_cfg
    else:
        theme = "a"
    size = os.environ.get("AUTOPOST_SIZE", "square").lower()
    if size not in ("portrait", "square", "story"):
        size = "square"
    return theme, size
