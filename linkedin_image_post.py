#!/usr/bin/env python3
"""
DC Hub — LinkedIn Rich Post Publisher (with image support)
==========================================================
Posts to the DC Hub LinkedIn company page with an attached image.
Uses the same internal /api/linkedin/* endpoints already on your Railway backend.

Usage:
    python linkedin_image_post.py

Requirements:
    pip install requests python-dotenv Pillow

Image:
    Put your image file path in IMAGE_PATH below (PNG or JPG recommended, min 1200×628px).
    If IMAGE_PATH is empty or the file doesn't exist, falls back to text-only post.
"""

import os
import sys
import json
import requests
from datetime import datetime
from dotenv import load_dotenv

# Phase 30C — daily landing URL (LinkedIn renders rich card from this URL's OG)
def _phase30c_landing_url(d=None):
    import datetime
    if d is None:
        d = datetime.date.today()
    return f"https://dchub.cloud/api/v1/social/posts/{d.isoformat()}"  # phase31_canonical_url

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────

DCHUB_BASE_URL   = "https://dchub-backend-production.up.railway.app"
DCHUB_ADMIN_KEY  = os.getenv("DCHUB_ADMIN_KEY", "")   # X-Admin-Key header
LINKEDIN_COMPANY = "110894959"                          # DC Hub company ID

# ── Post Content ───────────────────────────────────────────────────────────────

POST_TEXT = """Big news for DC Hub — Tony Bishop has joined as a Founding Member.

Tony spent 20+ years at the absolute epicenter of our industry:

→ SVP of Platform, Growth & Marketing at Digital Realty — helped build PlatformDIGITAL® and shape their global hyperscale strategy
→ VP of Global Vertical Strategy & Marketing at Equinix — drove growth across enterprise and service provider markets
→ Chief Strategy Officer at 451 Research — the premier digital infrastructure research firm
→ Managing Director & Global Head of Enterprise Datacenter Operations at Morgan Stanley — led their entire global datacenter transformation

He's a Second Degree Fellow of Infrastructure Masons, literally wrote the book on data center efficiency (Next Generation Datacenters), and has been recognized by Computerworld's Premier 100 IT Leaders.

Now he's joining DC Hub at the most pivotal moment in our industry's history.

Data center power demand is growing ~15% per year. The U.S. faces 100+ GW of new demand through 2035. AI is rewriting every assumption about capacity, energy, and site selection.

We're building the intelligence layer for this era — 50,000+ facilities, 140+ countries, real-time energy infrastructure mapping, M&A deal flow, and the first data center platform natively accessible to AI agents via MCP.

In Tony's own words: "The convergence of AI demand, energy constraints, and capital deployment requires a new kind of intelligence layer, and DC Hub is delivering it."

Grateful to have someone of Tony's caliber validate what we're building. This is just the beginning.

Full announcement: dchub.cloud/news/tony-bishop-founding-member

#DataCenter #Infrastructure #AI #DCHub #SiteSelection #EnergyTransition #DataCenterIntelligence"""

ARTICLE_URL  = "https://dchub.cloud/news/tony-bishop-founding-member"

# Path to image file to attach (PNG/JPG). Leave empty for text-only.
# Recommended: 1200×628px or 1200×1200px for square format
IMAGE_PATH   = ""   # e.g. "tony_bishop_announcement.png"

# URN of the bad/old post to delete before re-posting (set to "" to skip)
DELETE_URN   = "urn:li:share:7447473566431866881"


# ── Helpers ────────────────────────────────────────────────────────────────────

def headers():
    return {
        "Content-Type": "application/json",
        "X-Admin-Key":  DCHUB_ADMIN_KEY,
    }


def delete_post(urn: str) -> bool:
    """Delete a LinkedIn post by URN via DC Hub's internal proxy endpoint."""
    if not urn:
        return True
    endpoint = f"{DCHUB_BASE_URL}/api/linkedin/delete"
    try:
        resp = requests.post(
            endpoint,
            headers=headers(),
            json={"urn": urn},
            timeout=15,
        )
        if resp.status_code in (200, 204):
            print(f"✅  Deleted old post: {urn}")
            return True
        elif resp.status_code == 404:
            print(f"ℹ️   Post not found (already deleted?): {urn}")
            return True
        else:
            print(f"⚠️  Delete returned {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"⚠️  Delete request failed: {e}")
        return False


def upload_image_via_backend(image_path: str) -> str | None:
    """
    Upload an image to LinkedIn via the DC Hub backend's /api/linkedin/upload-image endpoint.
    Returns the LinkedIn asset URN on success, or None if upload fails.
    """
    if not image_path or not os.path.exists(image_path):
        return None

    print(f"📸  Uploading image: {image_path}")
    endpoint = f"{DCHUB_BASE_URL}/api/linkedin/upload-image"

    try:
        with open(image_path, "rb") as f:
            resp = requests.post(
                endpoint,
                headers={"X-Admin-Key": DCHUB_ADMIN_KEY},   # no Content-Type — let requests set multipart
                files={"image": (os.path.basename(image_path), f, "image/png")},
                data={"company_id": LINKEDIN_COMPANY},
                timeout=60,
            )
        if resp.status_code in (200, 201):
            data = resp.json()
            asset_urn = data.get("asset") or data.get("asset_urn")
            if asset_urn:
                print(f"✅  Image uploaded → {asset_urn}")
                return asset_urn
            else:
                print(f"⚠️  Upload succeeded but no asset URN in response: {resp.text[:200]}")
                return None
        else:
            print(f"⚠️  Image upload failed {resp.status_code}: {resp.text[:200]}")
            return None
    except Exception as e:
        print(f"⚠️  Image upload error: {e}")
        return None


def post_to_linkedin(text: str, article_url: str = None, image_asset_urn: str = None) -> bool:
    """Post via DC Hub's internal /api/linkedin/post endpoint."""
    endpoint = f"{DCHUB_BASE_URL}/api/linkedin/post"
    payload  = {"content": text}
    if article_url:
        payload["link_url"] = article_url
    if image_asset_urn:
        payload["image_asset_urn"] = image_asset_urn

    try:
        resp = requests.post(endpoint, headers=headers(), json=payload, timeout=20)
        if resp.status_code in (200, 201):
            data = resp.json()
            urn  = data.get("post_urn") or data.get("id", "n/a")
            print(f"✅  LinkedIn post published → {urn}")
            return True
        else:
            print(f"❌  LinkedIn post error {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        print(f"❌  LinkedIn post request failed: {e}")
        return False


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("\n🚀  DC Hub LinkedIn Rich Post Publisher")
    print("=" * 48)

    if not DCHUB_ADMIN_KEY:
        print("❌  DCHUB_ADMIN_KEY not set. Add it to your .env file or Replit Secrets.")
        sys.exit(1)

    # 1. Delete old/bad post
    if DELETE_URN:
        print(f"\n🗑️   Deleting old post: {DELETE_URN}")
        delete_post(DELETE_URN)
    else:
        print("ℹ️   No post to delete (DELETE_URN is empty)")

    print()

    # 2. Upload image (if provided)
    asset_urn = None
    if IMAGE_PATH:
        asset_urn = upload_image_via_backend(IMAGE_PATH)
        if not asset_urn:
            print("ℹ️   Continuing without image (upload failed or skipped)")
    else:
        print("ℹ️   No image path set — posting text only")

    print()

    # 3. Publish the post
    print("🔗  Publishing LinkedIn post...")
    ok = post_to_linkedin(POST_TEXT, article_url=ARTICLE_URL, image_asset_urn=asset_urn)

    print()
    print("─" * 48)
    print("✅  Done!" if ok else "⚠️   Completed with errors — see above.")


if __name__ == "__main__":
    main()
