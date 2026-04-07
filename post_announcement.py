#!/usr/bin/env python3
"""
DC Hub Announcement & LinkedIn Publisher
========================================
Automates posting press releases to dchub.cloud/press
and announcements to the DC Hub LinkedIn company page.

Usage:
    python post_announcement.py

Requirements:
    pip install requests python-dotenv

Setup:
    Create a .env file with your credentials (see .env.example below).
"""

import os
import json
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ────────────────────────────────────────────────────────────

DCHUB_BASE_URL   = "https://dchub.cloud"
DCHUB_API_KEY    = os.getenv("DCHUB_API_KEY")          # From DC Hub Dashboard → API Keys
LINKEDIN_TOKEN   = os.getenv("LINKEDIN_ACCESS_TOKEN")  # From LinkedIn Developer App
LINKEDIN_COMPANY = os.getenv("LINKEDIN_COMPANY_ID", "110894959")  # DC Hub company ID


# ── Press Release Data ───────────────────────────────────────────────────────
# Edit this section for each new announcement

PRESS_RELEASE = {
    "title": "Data Center Industry Veteran Tony Bishop Joins DC Hub as Founding Member",
    "slug": "tony-bishop-founding-member",
    "category": "Press Release",
    "date": "2026-04-07",
    "subheadline": (
        "Former Digital Realty SVP and Equinix VP brings decades of global platform "
        "strategy experience to the AI-native intelligence platform tracking "
        "50,000+ facilities across 140+ countries"
    ),
    "body": """
DC Hub (dchub.cloud), the AI-native data center intelligence platform, today announced
that Tony Bishop has joined as a Founding Member. Bishop brings more than two decades of
senior leadership experience spanning the world's largest data center operators, including
Digital Realty, Equinix, and Morgan Stanley, along with deep expertise in infrastructure
research from his tenure as Chief Strategy Officer at 451 Research.

Tony Bishop's career represents a unique arc through the most influential organizations in
the data center sector. He most recently served as Senior Vice President of Platform,
Growth & Marketing at Digital Realty, where he played a central role in building
PlatformDIGITAL® and shaping the company's global enterprise and hyperscale growth
strategy. Prior to Digital Realty, Bishop spent five years at Equinix as Vice President of
Global Vertical Strategy & Marketing. Earlier in his career, he served as Chief Strategy
Officer at 451 Research and as Managing Director and Global Head of Enterprise Datacenter
Operations & Strategy at Morgan Stanley & Co.

Bishop is a Second Degree Fellow of Infrastructure Masons, the author of Next Generation
Datacenters, and a recipient of Computerworld's Premier 100 IT Leaders award.

Quote from Tony Bishop:
"After spending my career inside the world's largest data center platforms, I've seen
firsthand how critical comprehensive intelligence is to making the right infrastructure
decisions. The convergence of AI demand, energy constraints, and capital deployment
requires a new kind of intelligence layer, and DC Hub is delivering it."

Quote from Jonathan Martone, Founder & CEO, DC Hub:
"Tony is one of the most respected strategic minds in the data center industry. Having
someone of Tony's caliber validate what we're building at DC Hub sends a powerful signal.
We're not just tracking facilities — we're building the intelligence layer that both
humans and AI agents rely on as this industry enters its most transformative era."
""".strip(),
    "meta_description": (
        "Former Digital Realty SVP and Equinix VP Tony Bishop joins DC Hub as Founding Member, "
        "bringing decades of global platform strategy experience to the AI-native intelligence platform."
    ),
}

LINKEDIN_POST = """🚨 Big news for DC Hub — Tony Bishop has joined as a Founding Member.

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

Grateful to have someone of Tony's caliber validate what we're building. This is just the beginning. 🙏

👉 Full announcement: dchub.cloud/news/tony-bishop-founding-member

#DataCenter #Infrastructure #AI #DCHub #SiteSelection #EnergyTransition #DataCenterIntelligence"""


# ── DC Hub Press Release Poster ──────────────────────────────────────────────

def post_to_dchub(release: dict) -> bool:
    """
    POST a new press release to the DC Hub backend.

    This calls the DC Hub admin API endpoint that you'll add to your Flask app.
    See the Flask snippet at the bottom of this file.
    """
    if not DCHUB_API_KEY:
        print("❌  DCHUB_API_KEY not set in .env")
        return False

    endpoint = f"{DCHUB_BASE_URL}/api/admin/press-releases"
    headers  = {
        "Authorization": f"Bearer {DCHUB_API_KEY}",
        "Content-Type":  "application/json",
    }
    payload = {
        "title":            release["title"],
        "slug":             release["slug"],
        "category":         release["category"],
        "date":             release["date"],
        "subheadline":      release["subheadline"],
        "body":             release["body"],
        "meta_description": release["meta_description"],
        "published":        True,
    }

    try:
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=15)
        if resp.status_code in (200, 201):
            data = resp.json()
            print(f"✅  DC Hub press release posted → {DCHUB_BASE_URL}/press/{release['slug']}")
            print(f"    ID: {data.get('id', 'n/a')}")
            return True
        else:
            print(f"❌  DC Hub API error {resp.status_code}: {resp.text}")
            return False
    except requests.RequestException as e:
        print(f"❌  DC Hub request failed: {e}")
        return False


# ── LinkedIn Company Page Poster ─────────────────────────────────────────────

def post_to_linkedin(text: str, article_url: str = None) -> bool:
    """
    POST a text update (with optional article link) to the DC Hub LinkedIn company page.

    Requires a LinkedIn access token with w_organization_social scope.
    Generate one at: https://www.linkedin.com/developers/apps
    """
    if not LINKEDIN_TOKEN:
        print("❌  LINKEDIN_ACCESS_TOKEN not set in .env")
        return False

    endpoint = "https://api.linkedin.com/v2/ugcPosts"
    headers  = {
        "Authorization":   f"Bearer {LINKEDIN_TOKEN}",
        "Content-Type":    "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
    }

    author = f"urn:li:organization:{LINKEDIN_COMPANY}"

    if article_url:
        # Rich post with article preview
        payload = {
            "author":         author,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "ARTICLE",
                    "media": [{
                        "status":      "READY",
                        "originalUrl": article_url,
                    }],
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            },
        }
    else:
        # Plain text post
        payload = {
            "author":         author,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary":    {"text": text},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            },
        }

    try:
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=15)
        if resp.status_code in (200, 201):
            data   = resp.json()
            post_id = data.get("id", "n/a")
            print(f"✅  LinkedIn post published → https://www.linkedin.com/feed/update/{post_id}")
            return True
        else:
            print(f"❌  LinkedIn API error {resp.status_code}: {resp.text}")
            return False
    except requests.RequestException as e:
        print(f"❌  LinkedIn request failed: {e}")
        return False


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("\n🚀  DC Hub Announcement Publisher")
    print("=" * 45)
    print(f"   Title : {PRESS_RELEASE['title'][:60]}...")
    print(f"   Date  : {PRESS_RELEASE['date']}")
    print(f"   Slug  : {PRESS_RELEASE['slug']}")
    print()

    # 1. Post to DC Hub /press
    print("📄  Posting to DC Hub press section...")
    dchub_ok = post_to_dchub(PRESS_RELEASE)

    print()

    # 2. Post to LinkedIn
    print("🔗  Posting to LinkedIn company page...")
    article_url = f"{DCHUB_BASE_URL}/news/{PRESS_RELEASE['slug']}"
    linkedin_ok = post_to_linkedin(LINKEDIN_POST, article_url=article_url)

    print()
    print("─" * 45)
    status = "✅  All done!" if (dchub_ok and linkedin_ok) else "⚠️   Completed with errors — check above."
    print(status)


if __name__ == "__main__":
    main()
