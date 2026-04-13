"""
News Digest Publisher Routes — Add to main.py
==============================================
Adds /publish/site, /publish/linkedin, and /publish/all endpoints
to the existing Flask backend. Writes digest HTML to Cloudflare KV
(DCHUB_CACHE) and posts to LinkedIn.

SETUP:
  Add these env vars to Railway:
    - CLOUDFLARE_API_TOKEN     : Token with "Workers KV Storage: Edit" permission
    - CLOUDFLARE_ACCOUNT_ID    : Your Cloudflare account ID
    - CLOUDFLARE_KV_NAMESPACE  : DCHUB_CACHE namespace ID (from Worker bindings)
    - PUBLISH_API_SECRET       : A secret key to protect these endpoints
    (LinkedIn token + URN should already be set from linkedin_poster.py)

HOW TO ADD:
  1. Copy this entire file into your Railway project as publish_routes.py
  2. Add these two lines to main.py (near the other blueprint/route registrations):

     from publish_routes import register_publish_routes
     register_publish_routes(app)

  3. Deploy to Railway. Done!
"""

import os
import json
import hmac
import logging
import requests as http_requests  # renamed to avoid Flask's request collision
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify

log = logging.getLogger("publish_routes")

publish_bp = Blueprint('publish', __name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CLOUDFLARE_API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_KV_NAMESPACE = os.environ.get("CLOUDFLARE_KV_NAMESPACE", "")
PUBLISH_API_SECRET = os.environ.get("PUBLISH_API_SECRET", "")
LINKEDIN_ACCESS_TOKEN = os.environ.get("LINKEDIN_ACCESS_TOKEN", "")
LINKEDIN_PERSON_URN = os.environ.get("LINKEDIN_PERSON_URN", "")
SITE_DOMAIN = "https://dchub.cloud"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def _verify_secret():
    if not PUBLISH_API_SECRET:
        return False
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip()
    if not token:
        return False
    return hmac.compare_digest(token, PUBLISH_API_SECRET)


# ---------------------------------------------------------------------------
# Cloudflare KV — Write
# ---------------------------------------------------------------------------
def _kv_put(key, value, metadata=None):
    """Write a key to Cloudflare KV (DCHUB_CACHE)."""
    if not all([CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_KV_NAMESPACE]):
        return False

    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}"
        f"/storage/kv/namespaces/{CLOUDFLARE_KV_NAMESPACE}/values/{key}"
    )
    headers = {"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"}

    if metadata:
        resp = http_requests.put(
            url, headers=headers,
            files={
                "value": (None, value, "text/html"),
                "metadata": (None, json.dumps(metadata), "application/json"),
            },
            timeout=30,
        )
    else:
        headers["Content-Type"] = "text/html"
        resp = http_requests.put(url, headers=headers, data=value.encode("utf-8"), timeout=30)

    if resp.status_code == 200:
        return resp.json().get("success", False)
    else:
        log.error(f"KV put failed for {key}: {resp.status_code} {resp.text}")
        return False


def _publish_to_site(html_content, slug):
    """Write digest HTML to KV + update latest pointer."""
    if not all([CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_KV_NAMESPACE]):
        return {"ok": False, "error": "Cloudflare KV env vars not configured"}

    metadata = {
        "slug": slug,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "content_type": "text/html",
    }

    ok = _kv_put(f"news:{slug}", html_content, metadata=metadata)
    if not ok:
        return {"ok": False, "error": f"Failed to write news:{slug} to KV"}

    # Update latest pointer
    latest = json.dumps({
        "slug": slug,
        "url": f"{SITE_DOMAIN}/news/{slug}",
        "published_at": datetime.now(timezone.utc).isoformat(),
    })
    _kv_put("news:latest", latest)

    # Date index
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _kv_put(f"news:date:{today}", json.dumps({"slug": slug}))

    page_url = f"{SITE_DOMAIN}/news/{slug}"
    log.info(f"Published digest to KV: {page_url}")
    return {"ok": True, "url": page_url, "slug": slug}


# ---------------------------------------------------------------------------
# LinkedIn — Post
# ---------------------------------------------------------------------------
def _publish_to_linkedin(text, article_url="", article_title=""):
    """Post to LinkedIn."""
    if not all([LINKEDIN_ACCESS_TOKEN, LINKEDIN_PERSON_URN]):
        return {"ok": False, "error": "LinkedIn env vars not configured"}

    url = "https://api.linkedin.com/rest/posts"
    headers = {
        "Authorization": f"Bearer {LINKEDIN_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "LinkedIn-Version": "202501",
        "X-Restli-Protocol-Version": "2.0.0",
    }

    payload = {
        "author": LINKEDIN_PERSON_URN,
        "lifecycleState": "PUBLISHED",
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "commentary": text,
    }

    if article_url:
        payload["content"] = {
            "article": {
                "source": article_url,
                "title": article_title or "DC Industry News Digest",
                "description": "Daily data center industry intelligence — market moves, deals, regulatory shifts, and community sentiment.",
            }
        }

    resp = http_requests.post(url, headers=headers, json=payload, timeout=30)

    if resp.status_code in (200, 201):
        post_id = resp.headers.get("x-restli-id", "unknown")
        log.info(f"LinkedIn post created: {post_id}")
        return {"ok": True, "post_id": post_id}
    else:
        log.error(f"LinkedIn post failed: {resp.status_code} {resp.text}")
        return {"ok": False, "error": resp.text, "status": resp.status_code}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@publish_bp.route('/publish/site', methods=['POST'])
def route_publish_site():
    if not _verify_secret():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    html = data.get("html", "")
    slug = data.get("slug", datetime.now(timezone.utc).strftime("digest-%Y-%m-%d"))

    if not html:
        return jsonify({"error": "html field is required"}), 400

    result = _publish_to_site(html, slug)
    return jsonify(result), 200 if result["ok"] else 502


@publish_bp.route('/publish/linkedin', methods=['POST'])
def route_publish_linkedin():
    if not _verify_secret():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    text = data.get("text", "")
    article_url = data.get("article_url", "")
    article_title = data.get("article_title", "")

    if not text:
        return jsonify({"error": "text field is required"}), 400

    result = _publish_to_linkedin(text, article_url, article_title)
    return jsonify(result), 200 if result["ok"] else 502


@publish_bp.route('/publish/all', methods=['POST'])
def route_publish_all():
    """
    One-shot: publish to site (KV) + LinkedIn.

    JSON body:
    {
      "html": "<full digest HTML>",
      "slug": "digest-2026-04-13",
      "linkedin_text": "The LinkedIn post...",
      "article_url": "https://dchub.cloud/news/digest-2026-04-13",
      "article_title": "DC Industry News Digest — April 13, 2026"
    }
    """
    if not _verify_secret():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    slug = data.get("slug", datetime.now(timezone.utc).strftime("digest-%Y-%m-%d"))

    # 1. Publish to site
    site_result = {"skipped": True}
    if data.get("html"):
        site_result = _publish_to_site(data["html"], slug)

    # 2. Publish to LinkedIn
    li_result = {"skipped": True}
    if data.get("linkedin_text"):
        article_url = data.get("article_url", "")
        if not article_url and site_result.get("ok"):
            article_url = site_result["url"]

        li_result = _publish_to_linkedin(
            data["linkedin_text"],
            article_url,
            data.get("article_title",
                      f"DC Industry News Digest — {datetime.now(timezone.utc).strftime('%B %d, %Y')}"),
        )

    return jsonify({"site": site_result, "linkedin": li_result}), 200


@publish_bp.route('/publish/health', methods=['GET'])
def route_publish_health():
    """Health check for publish routes."""
    return jsonify({
        "status": "ok",
        "kv_configured": bool(CLOUDFLARE_KV_NAMESPACE),
        "linkedin_configured": bool(LINKEDIN_ACCESS_TOKEN and LINKEDIN_PERSON_URN),
        "secret_configured": bool(PUBLISH_API_SECRET),
    })


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------
def register_publish_routes(app):
    """Call this from main.py to register the publish routes."""
    app.register_blueprint(publish_bp)
    log.info("Publish routes registered: /publish/site, /publish/linkedin, /publish/all")
