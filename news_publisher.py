"""
DC Industry News Digest — Auto-Publisher
Replit/Railway backend for publishing digests to dchub.cloud (via KV) + LinkedIn.

Architecture:
  - Your site runs on Cloudflare Workers with DCHUB_CACHE KV
  - This script writes digest HTML directly to KV via the Cloudflare API
  - Your Worker serves /news/* routes by reading from KV
  - LinkedIn posts go out via the LinkedIn REST API

Setup:
  1. Add these environment variables (Replit Secrets or Railway Variables):
     - LINKEDIN_ACCESS_TOKEN   : Your LinkedIn OAuth2 access token
     - LINKEDIN_PERSON_URN     : e.g. "urn:li:person:AbCdEf123"
     - CLOUDFLARE_API_TOKEN    : Token with "Workers KV Storage: Edit" permission
     - CLOUDFLARE_ACCOUNT_ID   : Your Cloudflare account ID
     - CLOUDFLARE_KV_NAMESPACE : Your DCHUB_CACHE KV namespace ID
     - API_SECRET              : A secret key to protect these endpoints

  2. pip install flask requests

  3. Run this file — Flask server with endpoints:
     POST /publish/site     — writes digest HTML to Cloudflare KV
     POST /publish/linkedin — posts to LinkedIn
     POST /publish/all      — both in one call
     GET  /health           — health check

  4. Add the /news route handler to your Worker (see worker-news-route.js)

How to find your KV namespace ID:
  Cloudflare dashboard → Workers & Pages → dchub → Settings → Bindings
  Find DCHUB_CACHE → the namespace ID is the long hex string shown there.
  Or: Workers & Pages → KV → click the namespace → ID is in the URL.
"""

import os
import json
import hmac
import logging
from datetime import datetime, timezone

# phase57_landing — daily landing URL helper for LinkedIn rich-card preview
def _phase30c_landing_url(d=None):
    """Return canonical /api/v1/social/posts/<date> URL for LinkedIn OG card."""
    import datetime
    if d is None:
        d = datetime.date.today()
    return f"https://dchub.cloud/api/v1/social/posts/{d.isoformat()}"


import requests
from flask import Flask, request, jsonify

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
LINKEDIN_ACCESS_TOKEN = os.environ.get("LINKEDIN_ACCESS_TOKEN", "")
LINKEDIN_PERSON_URN = os.environ.get("LINKEDIN_PERSON_URN", "")
CLOUDFLARE_API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_KV_NAMESPACE = os.environ.get("CLOUDFLARE_KV_NAMESPACE", "")
API_SECRET = os.environ.get("API_SECRET", "change-me-in-secrets")

SITE_DOMAIN = "https://dchub.cloud"

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("news_publisher")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def verify_api_secret():
    """Check the Authorization header matches our API_SECRET."""
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip()
    if not token:
        return False
    return hmac.compare_digest(token, API_SECRET)


# ---------------------------------------------------------------------------
# Cloudflare KV — Write digest HTML + index
# ---------------------------------------------------------------------------
def kv_put(key: str, value: str, expiration_ttl: int = 0, metadata: dict = None) -> bool:
    """Write a key-value pair to Cloudflare KV."""
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}"
        f"/storage/kv/namespaces/{CLOUDFLARE_KV_NAMESPACE}/values/{key}"
    )
    headers = {
        "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
    }

    # KV API accepts the value as the raw body, metadata as form field
    params = {}
    if expiration_ttl > 0:
        params["expiration_ttl"] = expiration_ttl

    if metadata:
        # Use multipart form: value + metadata
        resp = requests.put(
            url,
            headers=headers,
            params=params,
            files={
                "value": (None, value, "text/html"),
                "metadata": (None, json.dumps(metadata), "application/json"),
            },
            timeout=30,
        )
    else:
        headers["Content-Type"] = "text/html"
        resp = requests.put(url, headers=headers, params=params, data=value.encode("utf-8"), timeout=30)

    if resp.status_code == 200:
        result = resp.json()
        return result.get("success", False)
    else:
        log.error(f"KV put failed for {key}: {resp.status_code} {resp.text}")
        return False


def publish_to_site(html_content: str, slug: str) -> dict:
    """
    Publish digest to dchub.cloud by writing to Cloudflare KV.

    Writes two keys:
      - news:{slug}          → the full HTML page
      - news:latest          → JSON pointer to the latest slug + metadata

    The Worker serves /news/{slug} by reading news:{slug} from KV,
    and /news redirects to the latest.
    """
    if not all([CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_KV_NAMESPACE]):
        return {"ok": False, "error": "Cloudflare KV secrets not configured"}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    metadata = {
        "slug": slug,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "content_type": "text/html",
    }

    # 1. Write the digest HTML
    ok = kv_put(f"news:{slug}", html_content, metadata=metadata)
    if not ok:
        return {"ok": False, "error": f"Failed to write news:{slug} to KV"}

    # 2. Update the latest pointer
    latest_data = json.dumps({
        "slug": slug,
        "url": f"{SITE_DOMAIN}/news/{slug}",
        "published_at": datetime.now(timezone.utc).isoformat(),
    })
    kv_put("news:latest", latest_data)

    # 3. Write to a date-indexed list for archive
    kv_put(f"news:date:{today}", json.dumps({"slug": slug, "published_at": datetime.now(timezone.utc).isoformat()}))

    page_url = f"{SITE_DOMAIN}/news/{slug}"
    log.info(f"Published to KV: {page_url}")
    return {"ok": True, "url": page_url, "slug": slug, "kv_key": f"news:{slug}"}


# ---------------------------------------------------------------------------
# LinkedIn — Share Post
# ---------------------------------------------------------------------------
def publish_to_linkedin(text: str, article_url: str = "", article_title: str = "") -> dict:
    """Post to LinkedIn as the authenticated user."""
    if not all([LINKEDIN_ACCESS_TOKEN, LINKEDIN_PERSON_URN]):
        return {"ok": False, "error": "LinkedIn secrets not configured"}

    url = "https://api.linkedin.com/rest/posts"
    headers = {
        "Authorization": f"Bearer {LINKEDIN_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "LinkedIn-Version": "202401",
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

    # Attach article link card if URL provided
    if article_url:
        payload["content"] = {
            "article": {
                "source": article_url,
                "title": article_title or "DC Industry News Digest",
                "description": "Daily data center industry intelligence — market moves, deals, regulatory shifts, and community sentiment.",
            }
        }

    resp = requests.post(url, headers=headers, json=payload, timeout=30)

    if resp.status_code in (200, 201):
        post_id = resp.headers.get("x-restli-id", "unknown")
        log.info(f"LinkedIn post created: {post_id}")
        return {"ok": True, "post_id": post_id}
    else:
        log.error(f"LinkedIn post failed: {resp.status_code} {resp.text}")
        return {"ok": False, "error": resp.text, "status": resp.status_code}


# ---------------------------------------------------------------------------
# Flask Routes
# ---------------------------------------------------------------------------
# AUTO-REPAIR: duplicate route '/health' also in main.py:3839 — review and remove one
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "news_publisher",
        "kv_configured": bool(CLOUDFLARE_KV_NAMESPACE),
        "linkedin_configured": bool(LINKEDIN_ACCESS_TOKEN and LINKEDIN_PERSON_URN),
    })


@app.route("/publish/site", methods=["POST"])
def route_publish_site():
    if not verify_api_secret():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    html_content = data.get("html", "")
    slug = data.get("slug", datetime.now(timezone.utc).strftime("digest-%Y-%m-%d"))

    if not html_content:
        return jsonify({"error": "html field is required"}), 400

    result = publish_to_site(html_content, slug)
    return jsonify(result), 200 if result["ok"] else 502


@app.route("/publish/linkedin", methods=["POST"])
def route_publish_linkedin():
    if not verify_api_secret():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    text = data.get("text", "")
    article_url = data.get("article_url", "")
    article_title = data.get("article_title", "")

    if not text:
        return jsonify({"error": "text field is required"}), 400

    result = publish_to_linkedin(text, article_url, article_title)
    return jsonify(result), 200 if result["ok"] else 502


@app.route("/publish/all", methods=["POST"])
def route_publish_all():
    """
    One-shot endpoint: publishes to both site and LinkedIn.

    Expected JSON body:
    {
      "html": "<full digest HTML>",
      "slug": "digest-2026-04-13",
      "linkedin_text": "The LinkedIn post text...",
      "article_url": "https://dchub.cloud/news/digest-2026-04-13",
      "article_title": "DC Industry News Digest — April 13, 2026"
    }
    """
    if not verify_api_secret():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    slug = data.get("slug", datetime.now(timezone.utc).strftime("digest-%Y-%m-%d"))

    # 1. Publish to site (KV)
    site_result = {"skipped": True}
    if data.get("html"):
        site_result = publish_to_site(data["html"], slug)

    # 2. Publish to LinkedIn (with link to the published page)
    li_result = {"skipped": True}
    if data.get("linkedin_text"):
        # Auto-fill article_url from the published page if not provided
        article_url = data.get("article_url", "")
        if not article_url and site_result.get("ok"):
            article_url = site_result["url"]

        li_result = publish_to_linkedin(
            data["linkedin_text"],
            article_url,
            data.get("article_title", f"DC Industry News Digest — {datetime.now(timezone.utc).strftime('%B %d, %Y')}"),
        )

    return jsonify({"site": site_result, "linkedin": li_result}), 200


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    log.info(f"Starting news_publisher on port {port}")
    app.run(host="0.0.0.0", port=port)
